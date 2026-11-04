"""Scraper de hashtags do TikTok (`tiktok.com/tag/<nome>`).

Ao contrário dos perfis, a lista de uma tag não passa pelo yt-dlp (o extrator de tags
dele está marcado como quebrado, `_WORKING = False`). Aqui vale a filosofia do
Zeeschuimer: abrir a página, rolar, e capturar o que a própria página carrega em
`/api/challenge/item_list/`.

Exige a sessão do captcha resolvido à mão (`zeezazum tiktok-session --tag <nome>`).
Medido em 2026-11: SEM sessão a página carrega, mas `item_list` volta 200 com 0 bytes e
aparece o captcha (5 de 5 aberturas); COM sessão, 60 posts em headless na 1ª tentativa.

Ordem e incremental: a lista de uma tag não é cronológica, então não há marcador
"parar no post já conhecido" nem corte antecipado por `since_date`. Rola-se até
`max_posts`, até o TikTok avisar o fim da lista (`hasMore: false`) ou até a página
parar de trazer posts novos; o corte por data e o limite
são aplicados no final, sobre o que foi capturado (o orquestrador já ignora o que está
salvo, por shortcode).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from urllib.parse import quote, quote_plus, unquote_plus

from zeezazum.models import Post
from zeezazum.platforms.base import (
    CHECKPOINT_EVERY,
    BaseScraper,
    apply_final_filters,
    sort_newest_first,
)
from zeezazum.platforms.tiktok.parser import parse_item
from zeezazum.platforms.tiktok.scraper import CaptchaRequired
from zeezazum.platforms.tiktok.session import CAPTCHA_JS

logger = logging.getLogger(__name__)

_ITEM_LIST = "/api/challenge/item_list"
_SEARCH_ITEMS = "/api/search/item/full"
# Rolagem por JS, não por `mouse.wheel`: o modal de login do TikTok prende a roda do mouse
# (medido: a tag parava em 2 lotes, 60 posts, com `scrollTop` em 0). Além de rolar o
# documento (tags), traz o último vídeo para a vista: na busca o documento não rola
# (altura = janela) e o grid rola num contêiner interno; só isso dispara as páginas
# seguintes (medido: 24 posts com `scrollTo`, 144 com `scrollIntoView`).
SCROLL_JS = """() => {
  const e = document.scrollingElement;
  e.scrollTo(0, e.scrollHeight);
  const links = document.querySelectorAll('a[href*="/video/"]');
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
}"""
DEFAULT_STAGNANT_SCROLLS = 8


class TikTokTagScraper(BaseScraper):
    """Lê uma lista paginada do TikTok rolando a página e capturando a API que a alimenta.
    As subclasses mudam só a URL, o endpoint e os textos (tag x busca)."""

    _api_path = _ITEM_LIST

    def _page_url(self, handle: str) -> str:
        return f"https://www.tiktok.com/tag/{quote(handle)}"

    def _describe(self, handle: str) -> str:
        return f"a tag #{handle}"

    def _session_hint(self, handle: str) -> str:
        return f"uv run zeezazum tiktok-session --tag {handle}"

    def __init__(
        self,
        *,
        session_file=None,
        max_stagnant_scrolls: int | None = None,
        scroll_delay: float = 2.5,
        first_load_timeout: float = 12.0,
    ):
        self._has_session = bool(session_file)
        self._session_file = session_file
        self._max_stagnant = max_stagnant_scrolls or DEFAULT_STAGNANT_SCROLLS
        self._scroll_delay = scroll_delay
        self._first_load_timeout = first_load_timeout
        self.last_profile_stats: dict | None = None  # tags não têm seguidores

    async def extract_posts(
        self,
        context,
        handle: str,
        *,
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,  # ignorado: a lista da tag não é cronológica
        on_checkpoint=None,
    ) -> list[Post]:
        self.last_profile_stats = None
        captured: dict[str, Post] = {}
        end = {"reached": False}  # o TikTok avisou `hasMore: false`: acabou a lista

        async def on_response(response):
            if self._api_path not in response.url:
                return
            try:
                data = await response.json()
            except Exception:
                return  # corpo vazio (bloqueio) ou ilegível
            # tag: `hasMore` (bool); busca: `has_more` (0/1)
            more = (data or {}).get("hasMore", (data or {}).get("has_more"))
            if more is not None and not more:
                end["reached"] = True
            for raw in (data or {}).get("itemList") or (data or {}).get("item_list") or []:
                post = parse_item(raw)
                if post is not None:
                    captured.setdefault(post.shortcode, post)

        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto(
                self._page_url(handle),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await self._scroll(page, captured, end, max_posts, since_date, on_checkpoint)
            if not captured:
                await self._explain_empty(page, handle)
            elif end["reached"]:
                logger.info("%s: fim da lista segundo o TikTok (%d posts)", self._describe(handle), len(captured))
            else:
                logger.warning(
                    "%s: coleta parou em %d posts sem o TikTok avisar o fim da lista",
                    self._describe(handle), len(captured),
                )
        finally:
            await page.close()

        return apply_final_filters(
            sort_newest_first(captured.values()), max_posts, since_date, None
        )

    async def _scroll(self, page, captured, end, max_posts, since_date, on_checkpoint) -> None:
        loop = asyncio.get_running_loop()
        first_deadline = loop.time() + self._first_load_timeout
        stagnant = 0
        last = 0
        last_checkpoint = 0
        while True:
            await asyncio.sleep(self._scroll_delay)
            count = len(captured)
            if on_checkpoint is not None and count - last_checkpoint >= CHECKPOINT_EVERY:
                last_checkpoint = count
                await on_checkpoint(
                    apply_final_filters(
                        sort_newest_first(captured.values()), max_posts, since_date, None
                    )
                )
            if max_posts is not None and count >= max_posts:
                return
            if end["reached"]:
                return
            if count == last:
                # a primeira carga pode demorar: só conta como estagnação depois dela
                if count == 0 and loop.time() < first_deadline:
                    continue
                stagnant += 1
                if stagnant >= self._max_stagnant:
                    return
            else:
                stagnant, last = 0, count
            await page.evaluate(SCROLL_JS)

    async def _explain_empty(self, page, handle: str) -> None:
        """Nenhum post chegou: se o captcha está na tela, o usuário precisa resolvê-lo;
        senão a tag pode simplesmente estar vazia."""
        try:
            blocked = await page.evaluate(CAPTCHA_JS) is True
        except Exception:
            blocked = False
        if not blocked:
            logger.warning("%s: nenhum vídeo entregue pelo TikTok", self._describe(handle))
            return
        motivo = (
            f"A sessão salva em {self._session_file} não bastou (pode ter expirado)."
            if self._has_session
            else "Para isso a sessão com o captcha resolvido é necessária."
        )
        raise CaptchaRequired(
            f"O TikTok não entregou os vídeos d{self._describe(handle)} (captcha na tela). {motivo} "
            f"Resolva o captcha à mão: rode `{self._session_hint(handle)}`, deslize o "
            "quebra-cabeça na janela que abrir (sem fechar o modal pelo X); depois repita "
            "o comando anterior."
        )


def query_from_handle(handle: str) -> str:
    """O `handle` de uma busca é o texto com espaços como `+` (e `%XX` nos demais
    caracteres reservados): `jair+bolsonaro` -> `jair bolsonaro`."""
    return unquote_plus(handle)


class TikTokSearchScraper(TikTokTagScraper):
    """Busca de vídeos (`tiktok.com/search/video?q=...`), lida de `/api/search/item/full`."""

    _api_path = _SEARCH_ITEMS

    def _page_url(self, handle: str) -> str:
        return f"https://www.tiktok.com/search/video?q={quote_plus(query_from_handle(handle))}"

    def _describe(self, handle: str) -> str:
        return f"a busca «{query_from_handle(handle)}»"

    def _session_hint(self, handle: str) -> str:
        return f'uv run zeezazum tiktok-session --search "{query_from_handle(handle)}"'
