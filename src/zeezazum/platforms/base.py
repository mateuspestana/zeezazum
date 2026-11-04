"""Interface comum para scrapers de plataforma (Instagram e TikTok)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from playwright.async_api import BrowserContext

from zeezazum.models import Post

# posts não fixados mais antigos que since_date para encerrar o scroll/paginação
CONSECUTIVE_OLD_TO_STOP = 12
CHECKPOINT_EVERY = 200  # posts novos capturados entre checkpoints (persistência parcial)
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


class AuthenticationRequired(Exception):
    """A sessão exige novo login ou verificação manual."""


class ProfileUnavailable(Exception):
    """Perfil privado, inexistente ou indisponível."""


class BaseScraper(ABC):
    @abstractmethod
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
        """Navega até o perfil e retorna os posts encontrados, do mais novo ao mais antigo.

        A raspagem para assim que qualquer um dos critérios for atingido:
        `max_posts`, `since_date` (posts mais antigos que isso são ignorados) ou
        `stop_at_post_id` (post mais recente já conhecido de uma raspagem anterior —
        usado no modo incremental).
        """


def sort_newest_first(posts) -> list[Post]:
    """Do mais novo ao mais antigo. Posts fixados (antigos) vêm no topo da grade das
    plataformas; sem ordenar, `posts[0]` viraria o marcador incremental errado."""
    return sorted(posts, key=lambda p: p.timestamp or _MIN_DT, reverse=True)


def should_stop(
    captured: dict[str, Post],
    max_posts: int | None,
    since_date: datetime | None,
    stop_at_post_id: str | None,
) -> bool:
    if max_posts is not None and len(captured) >= max_posts:
        return True
    if stop_at_post_id is not None and stop_at_post_id in captured:
        return True
    if since_date is not None:
        # posts fixados no topo podem ser bem antigos e não indicam que a coleta
        # passou do corte — só contam posts não fixados.
        old = sum(
            1
            for p in captured.values()
            if not p.is_pinned and p.timestamp and p.timestamp < since_date
        )
        if old >= CONSECUTIVE_OLD_TO_STOP:
            return True
    return False


def apply_final_filters(
    posts: list[Post],
    max_posts: int | None,
    since_date: datetime | None,
    stop_at_post_id: str | None,
) -> list[Post]:
    """`posts` precisa estar ordenado do mais novo ao mais antigo."""
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
