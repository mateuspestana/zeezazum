"""Scraper do Instagram: navega até o perfil e captura os posts via interceptação
de rede (mesma filosofia do Zeeschuimer — captura o que a própria página carrega,
não faz scraping de DOM nem chama API "por fora" do browser autenticado).
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from playwright.async_api import BrowserContext, Response

from zeezazum.models import Post
from zeezazum.platforms.base import BaseScraper, ProfileUnavailable
from zeezazum.platforms.instagram.parser import parse_posts_from_payload

logger = logging.getLogger(__name__)

_API_URL_HINTS = ("graphql", "/api/v1/")
_LOGIN_URL_HINT = "/accounts/login"
_MAX_STAGNANT_SCROLLS = 4
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


class InstagramScraper(BaseScraper):
    def __init__(self, *, scroll_delay_range: tuple[float, float] = (1.5, 3.5)):
        self._scroll_delay_range = scroll_delay_range

    async def extract_posts(
        self,
        context: BrowserContext,
        handle: str,
        *,
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,
    ) -> list[Post]:
        page = await context.new_page()
        captured: dict[str, Post] = {}
        login_redirect_detected = False

        async def on_response(response: Response) -> None:
            nonlocal login_redirect_detected
            url = response.url
            if _LOGIN_URL_HINT in url:
                login_redirect_detected = True
                return
            if not any(hint in url for hint in _API_URL_HINTS):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                payload = await response.json()
            except Exception:
                # corpo não-JSON, resposta abortada, etc. — ignora silenciosamente
                return
            for post in parse_posts_from_payload(payload):
                captured.setdefault(post.shortcode, post)

        page.on("response", on_response)

        try:
            await page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded")
            await asyncio.sleep(2)  # dá tempo das primeiras chamadas XHR/GraphQL chegarem
        except Exception as exc:
            await page.close()
            raise ProfileUnavailable(f"Falha ao abrir perfil @{handle}: {exc}") from exc

        if login_redirect_detected or "/accounts/login" in page.url:
            await page.close()
            raise ProfileUnavailable(
                f"Sessão inválida/expirada ao acessar @{handle} — refaça o login local "
                "(scripts/local_login.py) e copie o storage_state atualizado para o servidor."
            )

        stagnant_rounds = 0
        previous_count = 0

        while not self._should_stop(captured, max_posts, since_date, stop_at_post_id):
            await page.mouse.wheel(0, 3000)
            await asyncio.sleep(random.uniform(*self._scroll_delay_range))

            if len(captured) == previous_count:
                stagnant_rounds += 1
                if stagnant_rounds >= _MAX_STAGNANT_SCROLLS:
                    break
            else:
                stagnant_rounds = 0
            previous_count = len(captured)

        await page.close()

        posts = sorted(captured.values(), key=lambda p: p.timestamp or _MIN_DT, reverse=True)
        return self._apply_final_filters(posts, max_posts, since_date, stop_at_post_id)

    @staticmethod
    def _should_stop(
        captured: dict[str, Post],
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,
    ) -> bool:
        if max_posts is not None and len(captured) >= max_posts:
            return True
        if stop_at_post_id is not None and stop_at_post_id in captured:
            return True
        if since_date is not None and any(
            p.timestamp and p.timestamp < since_date for p in captured.values()
        ):
            return True
        return False

    @staticmethod
    def _apply_final_filters(
        posts: list[Post],
        max_posts: int | None,
        since_date: datetime | None,
        stop_at_post_id: str | None,
    ) -> list[Post]:
        result: list[Post] = []
        for post in posts:
            if stop_at_post_id is not None and post.shortcode == stop_at_post_id:
                break
            if since_date is not None and post.timestamp and post.timestamp < since_date:
                break
            result.append(post)
            if max_posts is not None and len(result) >= max_posts:
                break
        return result
