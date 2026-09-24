"""Orquestra a raspagem: itera candidatos sequencialmente (com delay/jitter),
decide entre raspagem completa ou incremental via StateStore, e roda o download
de mídia em paralelo (fila assíncrona) enquanto o próximo perfil é raspado.
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from playwright.async_api import BrowserContext, async_playwright

from zeezazum.config import ZeezazumConfig
from zeezazum.models import CandidateState, Post, SocialAccount
from zeezazum.platforms.base import ProfileUnavailable
from zeezazum.platforms.instagram.downloader import MediaDownloader, MediaTask
from zeezazum.platforms.instagram.scraper import InstagramScraper
from zeezazum.pipeline.state import StateStore
from zeezazum.storage import build_storage
from zeezazum.storage.base import Storage

logger = logging.getLogger(__name__)


def _parse_since_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _posts_to_dicts(posts: list[Post]) -> list[dict]:
    return [
        {
            "shortcode": p.shortcode,
            "post_url": p.post_url,
            "timestamp": p.timestamp.isoformat() if p.timestamp else None,
            "caption": p.caption,
            "media": [{"kind": m.kind.value, "url": m.url, "index": m.index} for m in p.media],
        }
        for p in posts
    ]


def _persist_posts(storage: Storage, candidate_folder: str, new_posts: list[Post]) -> tuple[list[dict], int]:
    """Faz merge dos posts novos com o `posts.json` já existente e reescreve
    `posts.json` + `posts.parquet`. Retorna (posts_realmente_novos, total_após_merge) —
    em modo backfill, `new_posts` normalmente inclui posts já conhecidos (a raspagem
    reprocessa do topo), então o merge é o que decide o que é de fato inédito."""
    new_dicts = _posts_to_dicts(new_posts)

    existing_dicts: list[dict] = []
    json_path = f"{candidate_folder}/posts.json"
    if storage.exists(json_path):
        try:
            existing_dicts = storage.read_json(json_path)
        except Exception:
            logger.exception("posts.json corrompido em %s, será sobrescrito", candidate_folder)

    seen = {d["shortcode"] for d in existing_dicts}
    fresh = [d for d in new_dicts if d["shortcode"] not in seen]
    merged = existing_dicts + fresh

    storage.write_json(json_path, merged)

    buffer = io.BytesIO()
    pl.DataFrame(merged).write_parquet(buffer)
    storage.write_bytes(f"{candidate_folder}/posts.parquet", buffer.getvalue())

    return fresh, len(merged)


def _carry_forward_state(account: SocialAccount, existing: CandidateState | None, status: str) -> CandidateState:
    """Estado sem progresso novo (erro, ou nenhum post novo encontrado)."""
    return CandidateState(
        candidate_id=account.row_id,
        handle=account.handle,
        status=status,
        newest_post_id=existing.newest_post_id if existing else None,
        newest_post_timestamp=existing.newest_post_timestamp if existing else None,
        total_posts_seen=existing.total_posts_seen if existing else 0,
        last_run_at=datetime.now(timezone.utc),
        media_pending=existing.media_pending if existing else 0,
    )


async def _scrape_one(
    *,
    account: SocialAccount,
    context: BrowserContext,
    scraper: InstagramScraper,
    storage: Storage,
    state: StateStore,
    queue: "asyncio.Queue[MediaTask | None]",
    max_posts: int | None,
    since_date: datetime | None,
    max_attempts: int,
    backoff_seconds: int,
    extra_posts: int | None = None,
) -> None:
    """`extra_posts=None` (padrão): modo incremental — para assim que reencontra o
    post mais recente já conhecido (seguro pra cron, só traz posts novos).
    `extra_posts=N`: modo backfill — ignora esse early-stop e raspa até acumular
    `total_já_conhecido + N` posts, aprofundando no histórico de contas já raspadas."""
    existing = state.get(account.row_id)
    candidate_folder = f"instagram/{account.handle}"

    if extra_posts is None:
        stop_at_post_id = existing.newest_post_id if existing else None
        effective_max_posts = max_posts
    else:
        stop_at_post_id = None
        effective_max_posts = (existing.total_posts_seen + extra_posts) if existing else max_posts

    posts: list[Post] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            posts = await scraper.extract_posts(
                context,
                account.handle,
                max_posts=effective_max_posts,
                since_date=since_date,
                stop_at_post_id=stop_at_post_id,
            )
            break
        except ProfileUnavailable as exc:
            logger.error("Perfil indisponível @%s: %s", account.handle, exc)
            state.upsert(_carry_forward_state(account, existing, status="error"))
            return
        except Exception:
            logger.exception("Erro raspando @%s (tentativa %s/%s)", account.handle, attempt, max_attempts)
            if attempt >= max_attempts:
                state.upsert(_carry_forward_state(account, existing, status="error"))
                return
            await asyncio.sleep(backoff_seconds)

    if not posts:
        logger.info("Nenhum post novo para @%s", account.handle)
        state.upsert(_carry_forward_state(account, existing, status="done"))
        return

    fresh_dicts, total_after_merge = _persist_posts(storage, candidate_folder, posts)
    fresh_shortcodes = {d["shortcode"] for d in fresh_dicts}

    media_enqueued = 0
    for post in posts:
        if post.shortcode not in fresh_shortcodes:
            continue  # já persistido antes (comum em modo backfill) — não rebaixa mídia
        for media_item in post.media:
            await queue.put(MediaTask(candidate_folder=candidate_folder, shortcode=post.shortcode, item=media_item))
            media_enqueued += 1

    if not fresh_dicts:
        logger.info("Nenhum post novo para @%s", account.handle)
        state.upsert(_carry_forward_state(account, existing, status="done"))
        return

    newest = posts[0]  # extract_posts já retorna do mais novo pro mais antigo
    state.upsert(CandidateState(
        candidate_id=account.row_id,
        handle=account.handle,
        status="done",
        newest_post_id=newest.shortcode,
        newest_post_timestamp=newest.timestamp,
        total_posts_seen=total_after_merge,
        last_run_at=datetime.now(timezone.utc),
        media_pending=(existing.media_pending if existing else 0) + media_enqueued,
    ))
    logger.info("@%s: %d posts novos, %d itens de mídia enfileirados (total: %d posts)",
                account.handle, len(fresh_dicts), media_enqueued, total_after_merge)


async def run_scrape(
    config: ZeezazumConfig, accounts: list[SocialAccount], *, extra_posts: int | None = None
) -> None:
    storage = build_storage(config.storage)
    state_path = Path(config.storage.local.base_path) / "_state.duckdb"
    state = StateStore(state_path)
    since_date = _parse_since_date(config.scraping.since_date)

    queue: "asyncio.Queue[MediaTask | None]" = asyncio.Queue()
    downloader = MediaDownloader(
        storage,
        max_concurrent=config.media.max_concurrent_downloads,
        download_images=config.media.download_images,
        download_videos=config.media.download_videos,
    )
    consumer_task = asyncio.create_task(downloader.run(queue))

    try:
        async with async_playwright() as pw:
            browser_type = getattr(pw, config.scraping.browser)
            browser = await browser_type.launch(headless=config.scraping.headless)

            session_file = Path(config.scraping.session_file)
            if not session_file.exists():
                logger.warning(
                    "Nenhum storage_state em %s — rode scripts/local_login.py e copie o "
                    "arquivo antes de raspar perfis que exigem login.",
                    session_file,
                )
            context = await browser.new_context(
                storage_state=str(session_file) if session_file.exists() else None
            )
            scraper = InstagramScraper(scroll_delay_range=(1.5, 3.5))

            for i, account in enumerate(accounts):
                if i > 0:
                    delay = random.uniform(*config.scraping.delay_between_profiles_seconds)
                    await asyncio.sleep(delay)
                await _scrape_one(
                    account=account,
                    context=context,
                    scraper=scraper,
                    storage=storage,
                    state=state,
                    queue=queue,
                    max_posts=config.scraping.max_posts,
                    since_date=since_date,
                    max_attempts=config.scraping.retry.max_attempts,
                    backoff_seconds=config.scraping.retry.backoff_seconds,
                    extra_posts=extra_posts,
                )

            await context.close()
            await browser.close()
    finally:
        await queue.put(None)
        await consumer_task
        state.close()
