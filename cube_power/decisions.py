"""Append-only decision/audit log, mirrored to the bus for the dashboard."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .bus import Bus


class DecisionLog:
    def __init__(self, path: Path, bus: Bus):
        self.path = path
        self.bus = bus

    def log(self, source: str, reason: str, **extra) -> dict:
        entry = {"t": time.time(), "source": source, "reason": reason, **extra}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self.bus.publish({"type": "decision", "entry": entry})
        return entry
