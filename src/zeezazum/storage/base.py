"""Interface de armazenamento — implementações trocáveis via config (`storage.backend`)."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class Storage(ABC):
    @abstractmethod
    def write_bytes(self, relative_path: str, data: bytes) -> None:
        ...

    @abstractmethod
    def read_bytes(self, relative_path: str) -> bytes:
        ...

    @abstractmethod
    def exists(self, relative_path: str) -> bool:
        ...

    def write_json(self, relative_path: str, obj: Any) -> None:
        self.write_bytes(relative_path, json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8"))

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.read_bytes(relative_path).decode("utf-8"))
