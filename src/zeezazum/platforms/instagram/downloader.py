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
        state=None,
    ):
        self._state = state
        self._workers = max_concurrent
        self.results = {"downloaded": 0, "existing": 0, "failed": 0}
        self._storage = storage
        self._download_images = download_images
        self._download_videos = download_videos

    async def run(self, queue: "asyncio.Queue[MediaTask | None]") -> None:
        """Consome a fila até receber `None` (sentinela de fim de execução)."""
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:

            async def worker():
                while True:
                    task = await queue.get()
                    try:
                        if task is None:
                            return
                        try:
                            await self._download_one(client, task)
                        except Exception as exc:
                            self.results["failed"] += 1
                            if self._state:
                                self._state.finish_media(
                                    task.candidate_folder.split("/")[-1],
                                    task.shortcode,
                                    task.item.index,
                                    str(exc),
                                )
                            logger.exception(
                                "Falha na mídia %s: pendente para retomada",
                                task.shortcode,
                            )
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(self._workers)]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker_task in workers:
                    worker_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def _download_one(self, client: httpx.AsyncClient, task: MediaTask) -> None:
        if task.item.kind == MediaKind.IMAGE and not self._download_images:
            return
        if task.item.kind == MediaKind.VIDEO and not self._download_videos:
            return

        relative_base = (
            f"{task.candidate_folder}/media/{task.shortcode}_{task.item.index}"
        )
        handle = task.candidate_folder.split("/")[-1]
        if any(
            self._storage.exists(f"{relative_base}.{ext}")
            for ext in ("jpg", "png", "mp4")
        ):
            self.results["existing"] += 1
        else:
            content, content_type = await self._fetch(client, task.item.url)
            ext = _extension_for(task.item, content_type)
            await asyncio.to_thread(
                self._storage.write_bytes, f"{relative_base}.{ext}", content
            )
            self.results["downloaded"] += 1
        if self._state:
            self._state.finish_media(handle, task.shortcode, task.item.index)

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30)
    )
    async def _fetch(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[bytes, str | None]:
        response = await client.get(url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type")
