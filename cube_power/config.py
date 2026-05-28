"""Hot-reloaded YAML config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Config:
    def __init__(self, path: Path):
        self.path = path
        self._mtime: float = 0.0
        self.data: dict[str, Any] = {}
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        mt = self.path.stat().st_mtime
        if not force and mt == self._mtime:
            return False
        with open(self.path) as f:
            self.data = yaml.safe_load(f) or {}
        self._mtime = mt
        return True

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def getf(self, key: str, default: float) -> float:
        v = self.data.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
