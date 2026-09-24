from __future__ import annotations

from pathlib import Path

from zeezazum.storage.base import Storage


class LocalStorage(Storage):
    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        full = self.base_path / relative_path
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def write_bytes(self, relative_path: str, data: bytes) -> None:
        self._resolve(relative_path).write_bytes(data)

    def read_bytes(self, relative_path: str) -> bytes:
        return (self.base_path / relative_path).read_bytes()

    def exists(self, relative_path: str) -> bool:
        return (self.base_path / relative_path).exists()
