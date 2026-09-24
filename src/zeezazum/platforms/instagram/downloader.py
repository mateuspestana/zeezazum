"""Consumidor assíncrono da fila de mídia — roda em paralelo à raspagem de posts
do próximo candidato, para que baixar imagens/vídeos (lento) não bloqueie a
extração de posts (rápida).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from zeezazum.models import MediaItem, MediaKind
from zeezazum.storage.base import Storage

logger = logging.getLogger(__name__)


@dataclass
class MediaTask:
    candidate_folder: str  # ex: "instagram/<handle>"
    shortcode: str
    item: MediaItem


def _extension_for(item: MediaItem, content_type: str | None) -> str:
    if item.kind == MediaKind.VIDEO:
        return "mp4"
    if content_type and "png" in content_type:
        return "png"
    return "jpg"


class MediaDownloader:
    def __init__(
        self,
        storage: Storage,
        *,
        max_concurrent: int = 4,
        download_images: bool = True,
        download_videos: bool = True,
    ):
        self._storage = storage
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._download_images = download_images
        self._download_videos = download_videos

    async def run(self, queue: "asyncio.Queue[MediaTask | None]") -> None:
        """Consome a fila até receber `None` (sentinela de fim de execução)."""
        pending: list[asyncio.Task] = []
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            while True:
                task = await queue.get()
                if task is None:
                    break
                pending.append(asyncio.create_task(self._download_one(client, task)))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _download_one(self, client: httpx.AsyncClient, task: MediaTask) -> None:
        if task.item.kind == MediaKind.IMAGE and not self._download_images:
            return
        if task.item.kind == MediaKind.VIDEO and not self._download_videos:
            return

        relative_base = f"{task.candidate_folder}/media/{task.shortcode}_{task.item.index}"
        async with self._semaphore:
            try:
                content, content_type = await self._fetch(client, task.item.url)
            except Exception:
                logger.exception("Falha ao baixar mídia %s (%s)", task.shortcode, task.item.url)
                return

        ext = _extension_for(task.item, content_type)
        await asyncio.to_thread(self._storage.write_bytes, f"{relative_base}.{ext}", content)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> tuple[bytes, str | None]:
        response = await client.get(url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type")
