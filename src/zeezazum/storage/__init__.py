from __future__ import annotations

from zeezazum.config import StorageConfig
from zeezazum.storage.base import Storage
from zeezazum.storage.local import LocalStorage


def build_storage(config: StorageConfig) -> Storage:
    if config.backend == "local":
        return LocalStorage(config.local.base_path)
    if config.backend == "s3":
        from zeezazum.storage.s3 import S3Storage

        return S3Storage(bucket=config.s3.bucket, prefix=config.s3.prefix, region=config.s3.region)
    raise ValueError(f"storage.backend desconhecido: {config.backend}")
