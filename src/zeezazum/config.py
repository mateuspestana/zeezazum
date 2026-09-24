"""Carregamento e validação do config.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class InputConfig(BaseModel):
    path: str
    url_column: str
    id_column: str


class RetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_seconds: int = 30


class ScrapingConfig(BaseModel):
    browser: Literal["chromium", "firefox"] = "chromium"
    max_posts: int | None = 60
    since_date: str | None = None
    headless: bool = True
    session_file: str = "sessions/instagram_storage_state.json"
    delay_between_profiles_seconds: tuple[float, float] = (4, 10)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class LocalStorageConfig(BaseModel):
    base_path: str = "output"


class S3StorageConfig(BaseModel):
    bucket: str | None = None
    prefix: str = "zeezazum/"
    region: str = "us-east-1"


class StorageConfig(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local: LocalStorageConfig = Field(default_factory=LocalStorageConfig)
    s3: S3StorageConfig = Field(default_factory=S3StorageConfig)


class MediaConfig(BaseModel):
    download_images: bool = True
    download_videos: bool = True
    max_concurrent_downloads: int = 4


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/zeezazum.log"


class ZeezazumConfig(BaseModel):
    platform: Literal["instagram"] = "instagram"
    input: InputConfig
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path) -> ZeezazumConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return ZeezazumConfig.model_validate(raw)
