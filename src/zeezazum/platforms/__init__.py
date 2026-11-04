"""Registro das plataformas suportadas."""

from __future__ import annotations

PLATFORMS = ("instagram", "tiktok", "tiktok_tag", "tiktok_search")

# Plataformas do TikTok: sem login, vídeo pelo yt-dlp, sessão opcional do captcha.
TIKTOK_PLATFORMS = ("tiktok", "tiktok_tag", "tiktok_search")

DISPLAY_NAMES = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "tiktok_tag": "TikTok (hashtags)",
    "tiktok_search": "TikTok (buscas)",
}

# Plataformas que exigem sessão autenticada (login feito localmente).
REQUIRES_LOGIN = {"instagram": True, "tiktok": False, "tiktok_tag": False, "tiktok_search": False}

# Em "baixar tudo" o scroll do Instagram só desiste após muito mais rodadas sem novidade.
FETCH_ALL_STAGNANT_SCROLLS = 25


def build_scraper(platform: str, *, fetch_all: bool = False, session_file=None):
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

        return TikTokScraper(session_file=session_file)
    if platform == "tiktok_tag":
        from zeezazum.platforms.tiktok.tag_scraper import TikTokTagScraper

        return TikTokTagScraper(
            session_file=session_file,
            max_stagnant_scrolls=FETCH_ALL_STAGNANT_SCROLLS if fetch_all else None,
        )
    if platform == "tiktok_search":
        from zeezazum.platforms.tiktok.tag_scraper import TikTokSearchScraper

        return TikTokSearchScraper(
            session_file=session_file,
            max_stagnant_scrolls=FETCH_ALL_STAGNANT_SCROLLS if fetch_all else None,
        )
    raise ValueError(f"Plataforma não suportada: {platform}")
