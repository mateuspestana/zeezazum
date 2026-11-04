"""Registro das plataformas suportadas."""

from __future__ import annotations

PLATFORMS = ("instagram", "tiktok")

DISPLAY_NAMES = {"instagram": "Instagram", "tiktok": "TikTok"}

# Plataformas que exigem sessão autenticada (login feito localmente).
REQUIRES_LOGIN = {"instagram": True, "tiktok": False}

# Em "baixar tudo" o scroll do Instagram só desiste após muito mais rodadas sem novidade.
FETCH_ALL_STAGNANT_SCROLLS = 25


def build_scraper(platform: str, *, fetch_all: bool = False):
    """Instancia o scraper da plataforma."""
    if platform == "instagram":
        from zeezazum.platforms.instagram.scraper import InstagramScraper

        # em "baixar tudo" o scroll só desiste após muito mais rodadas sem novidade
        return InstagramScraper(
            scroll_delay_range=(1.5, 3.5),
            max_stagnant_scrolls=FETCH_ALL_STAGNANT_SCROLLS if fetch_all else None,
        )
    if platform == "tiktok":
        from zeezazum.platforms.tiktok.scraper import TikTokScraper

        return TikTokScraper()
    raise ValueError(f"Plataforma não suportada: {platform}")
