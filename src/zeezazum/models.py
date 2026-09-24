"""Estruturas de dados compartilhadas pelo pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True)
class SocialAccount:
    """Uma conta de rede social extraída da coluna de texto livre de um input."""

    row_id: str
    platform: str
    handle: str
    url: str


@dataclass(frozen=True)
class MediaItem:
    kind: MediaKind
    url: str
    index: int  # posição dentro do post (carrossel)


@dataclass(frozen=True)
class Post:
    shortcode: str
    post_url: str
    timestamp: datetime | None
    caption: str | None
    media: tuple[MediaItem, ...]
    raw: dict = field(default_factory=dict)


@dataclass
class CandidateState:
    candidate_id: str
    handle: str
    status: str  # "pending" | "in_progress" | "done" | "error"
    newest_post_id: str | None = None
    newest_post_timestamp: datetime | None = None
    total_posts_seen: int = 0
    last_run_at: datetime | None = None
    media_pending: int = 0
