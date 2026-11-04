"""Scraper do Instagram: navega até o perfil e captura os posts via interceptação
de rede (mesma filosofia do Zeeschuimer — captura o que a própria página carrega,
não faz scraping de DOM nem chama API "por fora" do browser autenticado).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone

from playwright.async_api import BrowserContext, Response

from zeezazum.models import Post
from zeezazum.platforms.base import (
    AuthenticationRequired,
    BaseScraper,
    ProfileUnavailable,
    apply_final_filters,
    should_stop,
)
from zeezazum.platforms.instagram.parser import parse_posts_from_payload

logger = logging.getLogger(__name__)

_API_URL_HINTS = ("graphql", "/api/v1/")
# Endpoints que o Zeeschuimer descarta: conteúdo pré-carregado/ruído que não é da página.
_IGNORED_URL_HINTS = (
    "/api/v1/discover/web/explore_grid/",
    "injected_story_units",
    "logging_client_events",
)
_AUTH_PATHS = ("/accounts/login", "/challenge/", "/checkpoint/", "/accounts/suspended")


def requires_auth(url: str) -> bool:
    from urllib.parse import urlparse

    path = urlparse(url).path
    return any(hint in path for hint in _AUTH_PATHS)


_MAX_STAGNANT_SCROLLS = (
    8  # perfis longos: o Instagram às vezes demora a devolver a próxima página
)
_CONSECUTIVE_OLD_TO_STOP = (
    12  # posts não fixados mais antigos que since_date para encerrar o scroll
)
_CHECKPOINT_EVERY = (
    200  # posts novos capturados entre checkpoints (persistência parcial)
)
_LONG_PAUSE_EVERY = (
    40  # scrolls entre pausas longas (reduz risco de bloqueio em scroll profundo)
)
_LONG_PAUSE_RANGE = (8.0, 20.0)
_OG_STATS_RE = re.compile(
    r"([\d.,]+)\s*([KkMm])?\s+Followers?,\s*([\d.,]+)\s*([KkMm])?\s+Following,\s*([\d.,]+)\s*([KkMm])?\s+Posts?",
    re.I,
)
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


class InstagramScraper(BaseScraper):
    def __init__(
        self,
        *,
        scroll_delay_range: tuple[float, float] = (1.5, 3.5),
        max_stagnant_scrolls: int | None = None,
    ):
        self._scroll_delay_range = scroll_delay_range
        self._max_stagnant_scrolls = max_stagnant_scrolls or _MAX_STAGNANT_SCROLLS
        # estatísticas do perfil da última extração (seguidores, seguindo, total de posts);
        # o orquestrador grava em profile.json para medir cobertura da raspagem.
        self.last_profile_stats: dict | None = None

    async def extract_posts(
        self,
        context: BrowserContext,
        handle: str,
        *,
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,
        on_checkpoint=None,
    ) -> list[Post]:
        """`on_checkpoint(posts)` (opcional, assíncrono) é chamado a cada
        `_CHECKPOINT_EVERY` posts novos, com tudo o que foi capturado até ali — permite
        persistir e baixar mídia durante scrolls longos sem esperar o fim do perfil."""
        self.last_profile_stats = None
        page = await context.new_page()
        captured: dict[str, Post] = {}
        login_redirect_detected = False

        async def on_response(response: Response) -> None:
            nonlocal login_redirect_detected
            url = response.url
            if requires_auth(url) and response.request.is_navigation_request():
                login_redirect_detected = True
                return
            if not any(hint in url for hint in _API_URL_HINTS):
                return
            if any(hint in url for hint in _IGNORED_URL_HINTS):
                return
            # sem filtro por content-type: o Instagram serve o GraphQL da timeline com
            # content-type que nem sempre contém "json" (text/javascript etc.); response.json()
            # já falha silenciosamente abaixo quando o corpo não é JSON.
            try:
                payload = await response.json()
            except Exception:
                # corpo não-JSON, resposta abortada, etc. — ignora silenciosamente
                return
            for post in parse_posts_from_payload(payload, handle):
                captured.setdefault(post.shortcode, post)

        try:
            page.on("response", on_response)

            try:
                await page.goto(
                    f"https://www.instagram.com/{handle}/",
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(
                    2
                )  # dá tempo das primeiras chamadas XHR/GraphQL chegarem
            except Exception as exc:
                if login_redirect_detected or requires_auth(page.url):
                    raise AuthenticationRequired(
                        "Sessão inválida; refaça scripts/local_login.py e copie a sessão."
                    ) from exc
                raise ProfileUnavailable(
                    f"Falha ao abrir perfil @{handle}: {exc}"
                ) from exc

            if login_redirect_detected or requires_auth(page.url):
                raise AuthenticationRequired(
                    f"Sessão inválida/expirada ao acessar @{handle} — refaça o login local "
                    "(scripts/local_login.py) e copie o storage_state atualizado para o servidor."
                )

            self.last_profile_stats = await self._read_profile_stats(page)

            stagnant_rounds = 0
            previous_count = 0
            last_checkpoint = 0
            scrolls = 0

            while not self._should_stop(
                captured, max_posts, since_date, stop_at_post_id
            ):
                # o mouse precisa estar sobre o conteúdo: em (0,0) o wheel cai na barra lateral
                # fixa do Instagram e a grade não rola (bug visto no piloto: só 3 posts por perfil).
                await page.mouse.move(
                    random.uniform(500, 800), random.uniform(350, 550)
                )
                await page.mouse.wheel(0, 3000)
                await asyncio.sleep(random.uniform(*self._scroll_delay_range))
                if login_redirect_detected or requires_auth(page.url):
                    raise AuthenticationRequired(
                        "Sessão expirada ou verificação obrigatória; refaça scripts/local_login.py e copie a sessão para o servidor."
                    )
                scrolls += 1
                if scrolls % _LONG_PAUSE_EVERY == 0:
                    await asyncio.sleep(random.uniform(*_LONG_PAUSE_RANGE))

                if (
                    on_checkpoint is not None
                    and len(captured) - last_checkpoint >= _CHECKPOINT_EVERY
                ):
                    last_checkpoint = len(captured)
                    snapshot = sorted(
                        captured.values(),
                        key=lambda p: p.timestamp or _MIN_DT,
                        reverse=True,
                    )
                    await on_checkpoint(
                        self._apply_final_filters(
                            snapshot, max_posts, since_date, stop_at_post_id
                        )
                    )

                if len(captured) == previous_count:
                    stagnant_rounds += 1
                    if stagnant_rounds >= self._max_stagnant_scrolls:
                        break
                else:
                    stagnant_rounds = 0
                previous_count = len(captured)

            posts = sorted(
                captured.values(), key=lambda p: p.timestamp or _MIN_DT, reverse=True
            )
            return self._apply_final_filters(
                posts, max_posts, since_date, stop_at_post_id
            )

        finally:
            await page.close()

    @staticmethod
    def _should_stop(captured, max_posts, since_date, stop_at_post_id) -> bool:
        return should_stop(captured, max_posts, since_date, stop_at_post_id)

    @staticmethod
    async def _read_profile_stats(page) -> dict | None:
        """Lê seguidores/seguindo/total de posts da meta og:description da página do perfil."""
        try:
            content = await page.locator(
                'meta[property="og:description"]'
            ).first.get_attribute("content", timeout=5000)
        except Exception:
            return None
        m = _OG_STATS_RE.search(content or "")
        if not m:
            return None

        def num(raw: str, suffix: str | None) -> int:
            value = float(raw.replace(",", ""))
            return round(value * {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1))

        return {
            "followers": num(m.group(1), m.group(2)),
            "following": num(m.group(3), m.group(4)),
            "posts_total": num(m.group(5), m.group(6)),
            "followers_rounded": bool(
                m.group(2)
            ),  # "5M" etc.: contagem arredondada pelo Instagram
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _apply_final_filters(posts, max_posts, since_date, stop_at_post_id):
        return apply_final_filters(posts, max_posts, since_date, stop_at_post_id)
