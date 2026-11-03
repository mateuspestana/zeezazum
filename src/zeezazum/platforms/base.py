"""Interface comum para scrapers de plataforma (Instagram é a primeira implementação)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from playwright.async_api import BrowserContext

from zeezazum.models import Post


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
