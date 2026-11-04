"""Orquestra a raspagem: itera candidatos sequencialmente (com delay/jitter),
decide entre raspagem completa ou incremental via StateStore, e roda o download
de mídia em paralelo (fila assíncrona) enquanto o próximo perfil é raspado.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import random
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from playwright.async_api import BrowserContext, async_playwright

from zeezazum.config import ZeezazumConfig
from zeezazum.models import CandidateState, Post, SocialAccount, MediaItem, MediaKind
from zeezazum.platforms.base import AuthenticationRequired, ProfileUnavailable
from zeezazum.platforms import REQUIRES_LOGIN, TIKTOK_PLATFORMS, build_scraper
from zeezazum.platforms.base import BaseScraper
from zeezazum.platforms.downloader import MediaDownloader, MediaTask
from zeezazum.pipeline.state import StateStore
from zeezazum.storage import build_storage
from zeezazum.storage.base import Storage

logger = logging.getLogger(__name__)

_STATE_KEY = "_state.duckdb"  # caminho relativo do estado dentro do Storage configurado


def _open_state(
    config: ZeezazumConfig, storage: Storage
) -> tuple[StateStore, Path | None]:
    """DuckDB é embutido e precisa de um arquivo local pra abrir — com
    `storage.backend: local` isso já É o arquivo final. Com `s3`, baixamos o
    estado existente (se houver) pra um arquivo temporário, trabalhamos nele,
    e devolvemos o caminho pra fazer upload de volta no final — assim o estado
    de resume/raspagem incremental viaja com os dados no bucket, não fica preso
    ao disco de uma instância EC2 específica."""
    if config.storage.backend != "s3":
        local_path = Path(config.storage.local.base_path) / _STATE_KEY
        return StateStore(local_path), None

    tmp_path = Path(tempfile.gettempdir()) / f"zeezazum_state_{uuid.uuid4().hex}.duckdb"
    if storage.exists(_STATE_KEY):
        tmp_path.write_bytes(storage.read_bytes(_STATE_KEY))
    return StateStore(tmp_path), tmp_path


def _parse_since_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _posts_to_dicts(posts: list[Post], collected_at: str | None = None) -> list[dict]:
    collected_at = collected_at or datetime.now(timezone.utc).isoformat()
    return [
        {
            "shortcode": p.shortcode,
            "post_url": p.post_url,
            "timestamp": p.timestamp.isoformat() if p.timestamp else None,
            "caption": p.caption,
            "media": [
                {"kind": m.kind.value, "url": m.url, "index": m.index} for m in p.media
            ],
            "is_pinned": p.is_pinned,
            # engajamento é uma fotografia do momento da coleta — por isso o carimbo por post
            "collected_at": collected_at,
            **p.metrics,
        }
        for p in posts
    ]


def _persist_raw(
    storage: Storage,
    candidate_folder: str,
    fresh_shortcodes: set[str],
    posts: list[Post],
) -> None:
    """Guarda o JSON bruto de cada post novo num chunk gzip (`raw/<ts>.jsonl.gz`),
    sem inchar posts.json. Cada chamada grava um chunk novo (sem reescrever os antigos)."""
    lines = [
        json.dumps(
            {"shortcode": p.shortcode, "raw": p.raw}, ensure_ascii=False, default=str
        )
        for p in posts
        if p.shortcode in fresh_shortcodes
    ]
    if not lines:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    storage.write_bytes(
        f"{candidate_folder}/raw/{stamp}.jsonl.gz",
        gzip.compress("\n".join(lines).encode("utf-8")),
    )


def _persist_posts(
    storage: Storage, candidate_folder: str, new_posts: list[Post]
) -> tuple[list[dict], int]:
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
            raise PersistenceFailure(
                f"posts.json ilegível em {candidate_folder}; preserve e restaure o arquivo"
            )

    # Refresh CDN URLs without rewriting the engagement snapshot of known posts.
    current = {d["shortcode"]: d for d in new_dicts}
    for saved in existing_dicts:
        if saved["shortcode"] in current:
            saved["media"] = current[saved["shortcode"]]["media"]
    seen = {d["shortcode"] for d in existing_dicts}
    fresh = [d for d in new_dicts if d["shortcode"] not in seen]
    merged = existing_dicts + fresh

    storage.write_json(json_path, merged)

    buffer = io.BytesIO()
    pl.DataFrame(merged, infer_schema_length=None).write_parquet(buffer)
    storage.write_bytes(f"{candidate_folder}/posts.parquet", buffer.getvalue())

    _persist_raw(storage, candidate_folder, {d["shortcode"] for d in fresh}, new_posts)
    return fresh, len(merged)


def _count_persisted(storage: Storage, candidate_folder: str) -> int:
    path = f"{candidate_folder}/posts.json"
    return len(storage.read_json(path)) if storage.exists(path) else 0


def _write_profile_stats(
    storage: Storage,
    candidate_folder: str,
    account: SocialAccount,
    stats: dict | None,
    scraped_total: int,
) -> None:
    """profile.json: seguidores/total de posts no momento da coleta + quantos posts foram
    raspados (base para medir cobertura da raspagem)."""
    storage.write_json(
        f"{candidate_folder}/profile.json",
        {
            "handle": account.handle,
            "row_id": account.row_id,
            "scraped_posts": scraped_total,
            **(stats or {}),
        },
    )


def _carry_forward_state(
    account: SocialAccount, existing: CandidateState | None, status: str
) -> CandidateState:
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
        platform=account.platform,
    )


async def _scrape_one(
    *,
    account: SocialAccount,
    context: BrowserContext,
    scraper: BaseScraper,
    storage: Storage,
    state: StateStore,
    queue: "MediaQueue",
    max_posts: int | None,
    since_date: datetime | None,
    max_attempts: int,
    backoff_seconds: int,
    extra_posts: int | None = None,
    fetch_all: bool = False,
) -> None:
    """`fetch_all=True`: histórico completo — ignora `max_posts`, `since_date` e o
    early-stop, e segue até a plataforma parar de entregar posts.
    `extra_posts=None` (padrão): modo incremental — para assim que reencontra o
    post mais recente já conhecido (seguro pra cron, só traz posts novos).
    `extra_posts=N`: modo backfill — ignora esse early-stop e raspa até acumular
    `total_já_conhecido + N` posts, aprofundando no histórico de contas já raspadas."""
    existing = state.get(account.row_id, account.handle, account.platform)
    platform = account.platform
    candidate_folder = f"{platform}/{account.handle}"
    if existing:
        existing.total_posts_seen = _count_persisted(storage, candidate_folder)

    if fetch_all:
        stop_at_post_id = None
        effective_max_posts = None
        since_date = None
    elif extra_posts is None:
        stop_at_post_id = existing.newest_post_id if existing else None
        effective_max_posts = max_posts
    else:
        stop_at_post_id = None
        effective_max_posts = (
            (existing.total_posts_seen + extra_posts) if existing else max_posts
        )

    media_enqueued = 0

    async def flush(batch: list[Post]) -> list[dict]:
        nonlocal media_enqueued
        queue.checkpoints.check()
        try:
            fresh_dicts, _ = _persist_posts(storage, candidate_folder, batch)
        except Exception as exc:
            raise PersistenceFailure("Falha ao persistir posts") from exc
        # Even known posts may carry refreshed CDN URLs for pending downloads.
        for post in batch:
            for item in post.media:
                state.register_media(
                    account.handle,
                    post.shortcode,
                    item,
                    exists=_media_exists(
                        storage, candidate_folder, post.shortcode, item.index
                    ),
                    platform=platform,
                )
        media_enqueued += await queue.enqueue_pending(account.handle, platform)
        return fresh_dicts

    posts: list[Post] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            posts = await scraper.extract_posts(
                context,
                account.handle,
                max_posts=effective_max_posts,
                since_date=since_date,
                stop_at_post_id=stop_at_post_id,
                on_checkpoint=flush,
            )
            break
        except AuthenticationRequired:
            raise
        except ProfileUnavailable as exc:
            logger.error("Perfil indisponível @%s: %s", account.handle, exc)
            state.upsert(_carry_forward_state(account, existing, status="error"))
            return
        except PersistenceFailure:
            raise
        except Exception:
            logger.exception(
                "Erro raspando @%s (tentativa %s/%s)",
                account.handle,
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                state.upsert(_carry_forward_state(account, existing, status="error"))
                return
            await asyncio.sleep(backoff_seconds)

    if not posts:
        logger.info("Nenhum post novo para @%s", account.handle)
        state.upsert(_carry_forward_state(account, existing, status="done"))
        return

    await flush(posts)  # posts já persistidos em checkpoints não contam como novos
    total_after_merge = _count_persisted(storage, candidate_folder)
    stats = scraper.last_profile_stats
    if fetch_all and stats and total_after_merge < stats.get("posts_total", 0):
        logger.warning(
            "@%s: %d posts salvos de %d declarados no perfil; rode `backfill --all` de novo para tentar completar",
            account.handle,
            total_after_merge,
            stats["posts_total"],
        )
    _write_profile_stats(
        storage,
        candidate_folder,
        account,
        scraper.last_profile_stats,
        total_after_merge,
    )

    newest = posts[0]  # extract_posts já retorna do mais novo pro mais antigo
    state.upsert(
        CandidateState(
            candidate_id=account.row_id,
            handle=account.handle,
            status="done",
            newest_post_id=newest.shortcode,
            newest_post_timestamp=newest.timestamp,
            total_posts_seen=total_after_merge,
            last_run_at=datetime.now(timezone.utc),
            media_pending=len(
                state.pending_media(
                    account.handle,
                    queue.config.media.download_images,
                    queue.config.media.download_videos,
                    platform,
                )
            ),
            platform=platform,
        )
    )
    logger.info(
        "@%s: %d itens de mídia enfileirados (total: %d posts persistidos)",
        account.handle,
        media_enqueued,
        total_after_merge,
    )


class PersistenceFailure(RuntimeError):
    pass


def _media_exists(storage, folder, shortcode, index):
    return any(
        storage.exists(f"{folder}/media/{shortcode}_{index}.{ext}")
        for ext in ("jpg", "png", "mp4")
    )


class MediaQueue:
    """Durable ledger plus bounded in-memory scheduling, once per item per run."""

    def __init__(self, config, storage, state):
        self.config, self.storage, self.state = config, storage, state
        self.queue = asyncio.Queue(maxsize=config.media.max_concurrent_downloads * 2)
        self.scheduled = set()
        self.downloader = MediaDownloader(
            storage,
            state=state,
            max_concurrent=config.media.max_concurrent_downloads,
            download_images=config.media.download_images,
            download_videos=config.media.download_videos,
            tiktok_video_format=config.media.tiktok_video_format,
        )
        self.consumer = asyncio.create_task(self.downloader.run(self.queue))

    async def enqueue_pending(self, handle, platform="instagram"):
        count = 0
        for code, index, kind, url in self.state.pending_media(
            handle,
            self.config.media.download_images,
            self.config.media.download_videos,
            platform,
        ):
            key = (platform, handle, code, index)
            if key in self.scheduled:
                continue
            self.scheduled.add(key)
            await self.queue.put(
                MediaTask(
                    f"{platform}/{handle}", code, MediaItem(MediaKind(kind), url, index)
                )
            )
            count += 1
        return count

    async def reconcile(self, accounts):
        queued = {}
        for platform, handle in dict.fromkeys(
            (a.platform, a.handle.lower()) for a in accounts
        ):
            folder = f"{platform}/{handle}"
            if self.storage.exists(f"{folder}/posts.json"):
                for post in self.storage.read_json(f"{folder}/posts.json"):
                    for m in post.get("media", []):
                        item = MediaItem(MediaKind(m["kind"]), m["url"], m["index"])
                        self.state.register_media(
                            handle,
                            post["shortcode"],
                            item,
                            exists=_media_exists(
                                self.storage, folder, post["shortcode"], item.index
                            ),
                            platform=platform,
                        )
            queued[handle] = await self.enqueue_pending(handle, platform)
        return queued

    async def close(self):
        for _ in range(self.config.media.max_concurrent_downloads):
            await self.queue.put(None)
        await self.consumer
        self.state.refresh_pending(
            self.config.media.download_images, self.config.media.download_videos
        )
        logger.info("Mídia: %s", self.downloader.results)

    def put(self, task):
        return self.queue.put(task)


class RemoteCheckpoints:
    def __init__(self, config, storage, state, path):
        self.config, self.storage, self.state, self.path = config, storage, state, path
        self.error = None
        self.lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.run()) if path else None

    async def sync(self):
        if self.path is None:
            return
        async with self.lock:
            self.state.refresh_pending(
                self.config.media.download_images, self.config.media.download_videos
            )
            # No await: the event loop owns all writes to this connection.
            data = self.state.snapshot()
            await asyncio.to_thread(self.storage.write_bytes, _STATE_KEY, data)

    async def run(self):
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self.stop.wait(),
                        self.config.storage.s3.state_sync_interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    await self.sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            logger.exception(
                "Checkpoint S3 falhou; estado local preservado em %s", self.path
            )

    def check(self):
        if self.error:
            raise PersistenceFailure(
                f"Checkpoint S3 falhou; estado local: {self.path}"
            ) from self.error

    async def close(self):
        if self.task:
            # Wait for any active upload before final upload, avoiding stale overwrites.
            self.stop.set()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            await self.sync()
        except Exception as exc:
            logger.exception("Estado preservado para recuperação em %s", self.path)
            raise PersistenceFailure(
                f"Upload do estado falhou; arquivo local preservado em {self.path}"
            ) from exc
        else:
            if self.path:
                self.path.unlink(missing_ok=True)


def _session_file(config, platform: str) -> Path | None:
    """Arquivo de sessão da plataforma, se existir em disco."""
    if platform in TIKTOK_PLATFORMS:
        path = Path(config.scraping.tiktok_session_file)
    elif REQUIRES_LOGIN.get(platform):
        path = Path(config.scraping.session_file)
    else:
        return None
    return path if path.exists() else None


async def _run(config, accounts, extra_posts=None, media_only=False, fetch_all=False):
    storage = build_storage(config.storage)
    state, path = _open_state(config, storage)
    media = MediaQueue(config, storage, state)
    checkpoints = RemoteCheckpoints(config, storage, state, path)
    media.checkpoints = checkpoints
    queued = {}
    try:
        queued = await media.reconcile(accounts)
        checkpoints.check()
        if not media_only:
            async with async_playwright() as pw:
                browser = await getattr(pw, config.scraping.browser).launch(
                    headless=config.scraping.headless
                )
                try:
                    contexts: dict = {}
                    scrapers: dict = {}

                    async def context_for(platform):
                        # Instagram: login. TikTok: anônimo, ou com a sessão do captcha
                        # resolvido à mão, se existir (`zeezazum tiktok-session`)
                        if platform not in contexts:
                            session = _session_file(config, platform)
                            contexts[platform] = await browser.new_context(
                                storage_state=str(session) if session else None,
                                viewport={"width": 1280, "height": 900},
                            )
                        return contexts[platform]

                    for i, account in enumerate(accounts):
                        if i:
                            await asyncio.sleep(
                                random.uniform(
                                    *config.scraping.delay_between_profiles_seconds
                                )
                            )
                        checkpoints.check()
                        platform = account.platform
                        if platform not in scrapers:
                            scrapers[platform] = build_scraper(
                                platform,
                                fetch_all=fetch_all,
                                session_file=_session_file(config, platform),
                            )
                        await _scrape_one(
                            account=account,
                            context=await context_for(platform),
                            scraper=scrapers[platform],
                            storage=storage,
                            state=state,
                            queue=media,
                            max_posts=config.scraping.max_posts,
                            since_date=_parse_since_date(config.scraping.since_date),
                            max_attempts=config.scraping.retry.max_attempts,
                            backoff_seconds=config.scraping.retry.backoff_seconds,
                            extra_posts=extra_posts,
                            fetch_all=fetch_all,
                        )
                finally:
                    await browser.close()
        checkpoints.check()
    finally:
        try:
            await media.close()
        finally:
            try:
                await checkpoints.close()
            finally:
                state.close()
    checkpoints.check()
    results = media.downloader.results
    print(
        f"Mídia: {results['downloaded']} baixadas, {results['existing']} existentes, {results['failed']} falhas."
    )
    if results["failed"]:
        raise PersistenceFailure(
            "Downloads pendentes; execute media-sync. URLs expiradas exigem reencontrar o post em nova coleta/backfill."
        )
    return queued


async def run_scrape(
    config: ZeezazumConfig,
    accounts: list[SocialAccount],
    *,
    extra_posts=None,
    fetch_all=False,
):
    await _run(config, accounts, extra_posts=extra_posts, fetch_all=fetch_all)


async def run_media_sync(
    config: ZeezazumConfig, accounts: list[SocialAccount]
) -> dict[str, int]:
    return await _run(config, accounts, media_only=True)
