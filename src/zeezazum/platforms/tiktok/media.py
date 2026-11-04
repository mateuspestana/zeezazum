"""Download de vídeos do TikTok via yt-dlp (sem login).

As URLs diretas do CDN expiram em horas e exigem cabeçalhos/cookies, então guardamos
a URL da página do post (permanente) e deixamos o yt-dlp resolver o arquivo na hora.
O download é bloqueante: quem chama deve usá-lo dentro de `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import random
import tempfile
import time
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)

DEFAULT_FORMAT = "best"  # maior qualidade disponível (costuma ser HEVC/bytevc1)


def download_video(
    url: str,
    *,
    video_format: str = DEFAULT_FORMAT,
    attempts: int = 4,
    retry_delay: tuple[float, float] = (2.0, 6.0),
) -> bytes:
    """Baixa o vídeo do post e devolve os bytes do mp4. O TikTok responde de forma
    intermitente (429, página de desafio), então tenta algumas vezes antes de falhar."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory(prefix="zeezazum_tt_") as tmp:
            options = {
                "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
                "format": video_format,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
            }
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.extract_info(url, download=True)
                files = [f for f in Path(tmp).iterdir() if f.is_file()]
                if not files:
                    raise RuntimeError("yt-dlp não produziu arquivo")
                return files[0].read_bytes()
            except (DownloadError, RuntimeError) as exc:
                last_error = exc
                logger.warning(
                    "Download TikTok %s falhou (%d/%d): %s", url, attempt, attempts, exc
                )
        if attempt < attempts:
            time.sleep(random.uniform(*retry_delay) * attempt)
    raise RuntimeError(f"Download do vídeo falhou após {attempts} tentativas: {last_error}")
