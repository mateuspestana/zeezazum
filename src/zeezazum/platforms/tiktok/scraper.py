"""Scraper do TikTok: não exige login.

Por que não é igual ao do Instagram: o navegador anônimo recebe 200 com corpo vazio
na lista de vídeos do perfil (`/api/post/item_list/`), então a lista vem do yt-dlp.
O navegador anônimo só serve para ler os dados do perfil (existência, privacidade,
total de vídeos), que o TikTok embute na página.

O TikTok é intermitente com clientes anônimos (429, página de desafio). Medido em
2026-11: ~40% de sucesso por tentativa na abertura do perfil. Por isso a abertura é
repetida com espera crescente, e uma falha no meio da paginação reinicia a listagem
pulando o que já foi visto. Uma lista incompleta nunca é devolvida como completa:
se as tentativas acabam no meio, a exceção sobe (os posts já capturados foram
persistidos pelos checkpoints) e o orquestrador tenta de novo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from contextlib import ExitStack
from datetime import datetime, timezone

import yt_dlp

from zeezazum.models import Post
from zeezazum.platforms.base import (
    CHECKPOINT_EVERY,
    BaseScraper,
    ProfileUnavailable,
    apply_final_filters,
    should_stop,
    sort_newest_first,
)
from zeezazum.platforms.tiktok.parser import parse_entry, parse_user_detail

logger = logging.getLogger(__name__)

_USER_NOT_FOUND = 10221  # statusCode de webapp.user-detail para perfil inexistente/banido
_END = object()


class ListingError(Exception):
    """Falha vinda do yt-dlp ao abrir ou paginar o perfil. É a única coisa que a
    listagem repete: erros de gravação do checkpoint (`PersistenceFailure`) e
    afins passam direto, para não virarem "falha de listagem"."""


class _YdlLogger:
    """Roteia as mensagens do yt-dlp para o logging (senão ele imprime no stderr)."""

    def debug(self, msg):
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg):
        logger.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        logger.debug("yt-dlp: %s", msg)

    def error(self, msg):
        logger.debug("yt-dlp: %s", msg)


def default_ydl_factory() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(
        {
            "quiet": True,
            "no_warnings": True,
            "logger": _YdlLogger(),
            "extractor_retries": 5,  # retries por página, dentro da paginação
            "retry_sleep_functions": {"extractor": lambda n: min(3 * n, 30)},
        }
    )


class TikTokScraper(BaseScraper):
    def __init__(
        self,
        *,
        ydl_factory=default_ydl_factory,
        listing_attempts: int = 10,
        retry_delay_range: tuple[float, float] = (3.0, 6.0),
        profile_timeout: float = 15.0,
    ):
        self._ydl_factory = ydl_factory
        self._listing_attempts = listing_attempts
        self._retry_delay_range = retry_delay_range
        self._profile_timeout = profile_timeout
        # mesmo formato do Instagram: o orquestrador grava em profile.json
        self.last_profile_stats: dict | None = None

    async def extract_posts(
        self,
        context,
        handle: str,
        *,
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,
        on_checkpoint=None,
    ) -> list[Post]:
        self.last_profile_stats = None
        profile = await self._read_profile(context, handle)
        if profile:
            self._check_profile(handle, profile)
            self.last_profile_stats = {
                "followers": profile["followers"],
                "following": profile["following"],
                "posts_total": profile["posts_total"],
                "followers_rounded": False,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }

        captured: dict[str, Post] = {}
        await self._list_with_retries(
            handle,
            captured,
            max_posts=max_posts,
            since_date=since_date,
            stop_at_post_id=stop_at_post_id,
            on_checkpoint=on_checkpoint,
            profile_verified=profile is not None,
        )
        return apply_final_filters(
            sort_newest_first(captured.values()), max_posts, since_date, stop_at_post_id
        )

    # -- perfil (navegador anônimo) -------------------------------------------------

    async def _read_profile(self, context, handle: str) -> dict | None:
        """Dados do perfil embutidos na página. None se não houver navegador, ou se o
        TikTok não entregar o bloco a tempo (a coleta segue sem classificação)."""
        if context is None:
            return None
        page = await context.new_page()
        try:
            await page.goto(
                f"https://www.tiktok.com/@{handle}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._profile_timeout
            while loop.time() < deadline:
                try:
                    text = await page.evaluate(
                        "() => document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent || null"
                    )
                except Exception:
                    text = None  # a página recarregou (desafio JS): tenta de novo
                if text:
                    try:
                        parsed = parse_user_detail(json.loads(text))
                    except ValueError:
                        parsed = None
                    if parsed:
                        return parsed
                await asyncio.sleep(1)
        except Exception as exc:
            logger.debug("Perfil @%s: leitura da página falhou: %s", handle, exc)
        finally:
            await page.close()
        logger.info("Perfil @%s: TikTok não entregou os dados da página", handle)
        return None

    @staticmethod
    def _check_profile(handle: str, profile: dict) -> None:
        status = profile.get("status_code")
        if status == _USER_NOT_FOUND:
            raise ProfileUnavailable(f"@{handle} não existe ou foi banido no TikTok")
        if profile.get("private"):
            raise ProfileUnavailable(f"@{handle} é um perfil privado no TikTok")
        if status not in (0, None):
            logger.warning("Perfil @%s: statusCode %s inesperado; seguindo", handle, status)

    # -- listagem (yt-dlp) ---------------------------------------------------------

    async def _list_with_retries(
        self,
        handle: str,
        captured: dict[str, Post],
        *,
        max_posts,
        since_date,
        stop_at_post_id,
        on_checkpoint,
        profile_verified: bool,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._listing_attempts + 1):
            try:
                await self._iterate(
                    handle,
                    captured,
                    max_posts=max_posts,
                    since_date=since_date,
                    stop_at_post_id=stop_at_post_id,
                    on_checkpoint=on_checkpoint,
                )
                return
            except ListingError as exc:
                last_error = exc
                logger.info(
                    "Listagem de @%s falhou (%d/%d, %d posts até aqui): %s",
                    handle,
                    attempt,
                    self._listing_attempts,
                    len(captured),
                    exc,
                )
                if attempt < self._listing_attempts:
                    delay = random.uniform(*self._retry_delay_range) * min(attempt, 6)
                    await asyncio.sleep(delay)

        if captured or profile_verified:
            # lista incompleta ou bloqueio temporário: erro comum, o orquestrador repete
            raise RuntimeError(
                f"TikTok não concluiu a lista de @{handle} após {self._listing_attempts} "
                f"tentativas ({len(captured)} posts capturados): {last_error}"
            ) from last_error
        raise ProfileUnavailable(
            f"Não foi possível listar @{handle} após {self._listing_attempts} tentativas: "
            "perfil inexistente, privado ou bloqueio temporário do TikTok "
            f"({last_error})"
        ) from last_error

    async def _iterate(
        self,
        handle: str,
        captured: dict[str, Post],
        *,
        max_posts,
        since_date,
        stop_at_post_id,
        on_checkpoint,
    ) -> None:
        """Uma passada pela listagem, em thread para não travar o loop. Em retomada, o
        que já está em `captured` é ignorado.

        `process=False` é essencial: com o processamento normal o yt-dlp percorre a
        playlist INTEIRA dentro de `extract_info` (mesmo com `lazy_playlist`) e devolve
        uma lista; aí `max_posts`, `stop_at_post_id` e os checkpoints não economizariam
        nada. Sem processar, `entries` é o gerador cru do extrator, que pagina sob demanda."""
        with ExitStack() as stack:
            ydl = stack.enter_context(self._ydl_factory())
            try:
                info = await asyncio.to_thread(
                    ydl.extract_info,
                    f"https://www.tiktok.com/@{handle}",
                    download=False,
                    process=False,
                )
            except Exception as exc:
                raise ListingError(str(exc)) from exc
            if not info:
                raise ListingError("TikTok não devolveu o perfil (resposta vazia)")
            entries = iter(info.get("entries") or [])
            last_checkpoint = len(captured)
            while True:
                try:
                    entry = await asyncio.to_thread(next, entries, _END)
                except Exception as exc:  # falha no meio da paginação
                    raise ListingError(str(exc)) from exc
                if entry is _END:
                    return
                post = parse_entry(entry)
                if post is None or post.shortcode in captured:
                    continue
                captured[post.shortcode] = post
                if (
                    on_checkpoint is not None
                    and len(captured) - last_checkpoint >= CHECKPOINT_EVERY
                ):
                    last_checkpoint = len(captured)
                    await on_checkpoint(
                        apply_final_filters(
                            sort_newest_first(captured.values()),
                            max_posts,
                            since_date,
                            stop_at_post_id,
                        )
                    )
                if should_stop(captured, max_posts, since_date, stop_at_post_id):
                    return
