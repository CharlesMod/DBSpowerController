"""Per-unit object: latest state, rolling history, role + safety classification.

A Unit is a sensor + safety classifier. It never writes to the device and never
decides AC scheduling — that is the coordinator's job. It only answers "what role
is this unit in right now" so the coordinator can act.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .config import Config
from .types import DeviceState, UnitRole


class Unit:
    def __init__(self, spec: dict, dps_map: dict[int, str]):
        self.spec = spec
        self.unit_id: str = spec["id"]
        self.name: str = spec["name"]
        self.ip: str = spec["ip"]
        self.version: float = float(spec.get("version", 3.3))
        self.model: str = spec.get("model", "DBS1400Pro")
        # Bus group: units in the same group share an electrical bus feeding
        # one car. Today both DBS units are group "a" (parallel into Tessa);
        # the Anker will become group "b".
        self.bus_group: str = str(spec.get("bus_group", "a"))
        # Per-unit AC output capacity (W) — controller uses sum of usable
        # units' max_out_w in a group to size what a car can draw.
        self.max_out_w: int = int(spec.get("max_out_w", 1200))
        self.dps_map = dps_map

        self.state = DeviceState(unit_id=self.unit_id, name=self.name, ip=self.ip)
        self.history: deque[DeviceState] = deque(maxlen=720)  # ~2h @ 10s

        self.role: UnitRole = UnitRole.OFFLINE
        self.override_target: bool | None = None
        self.override_expires_at: float | None = None

        # serializes all Tuya I/O to this unit — the device allows only one
        # connection at a time, so the poller and actuator must take turns.
        self.io_lock = asyncio.Lock()

    # ---- ingestion ----

    def record(self, state: DeviceState) -> None:
        self.state = state
        self.history.append(state)

    # ---- safety / role ----

    def is_stale(self, cfg: Config) -> bool:
        max_age = cfg.getf("state_stale_s", 60)
        return (not self.state.online) or (time.time() - self.state.updated_at > max_age)

    def is_floored(self, cfg: Config) -> bool:
        soc = self.state.soc_pct
        return soc is not None and soc <= cfg.getf("soc_floor_pct", 33)

    def is_hard_floored(self, cfg: Config) -> bool:
        soc = self.state.soc_pct
        return soc is not None and soc <= cfg.getf("soc_hard_floor_pct", 30)

    def at_rehab(self, cfg: Config) -> bool:
        """SoC has recovered far enough to come back online after a floor cutoff."""
        soc = self.state.soc_pct
        if soc is None:
            return False
        return soc >= cfg.getf("soc_floor_pct", 33) + cfg.getf("soc_rehab_band_pct", 7)

    def override_active(self) -> bool:
        if self.override_expires_at is None:
            return False
        if time.time() >= self.override_expires_at:
            self.override_target = None
            self.override_expires_at = None
            return False
        return True

    def classify(self, cfg: Config) -> UnitRole:
        """Recompute and return this unit's role. Called once per coordinator tick."""
        if self.override_active():
            role = UnitRole.OVERRIDE
        elif self.is_stale(cfg):
            role = UnitRole.OFFLINE
        elif self.is_floored(cfg):
            role = UnitRole.FLOORED
        else:
            role = UnitRole.NORMAL
        self.role = role
        return role

    # ---- override controls ----

    def request_override(self, on: bool, ttl_h: float | None, cfg: Config) -> dict:
        default = cfg.getf("override_default_ttl_h", 48)
        max_ttl = cfg.getf("override_max_ttl_h", 168)
        ttl = min(float(ttl_h) if ttl_h else default, max_ttl)
        self.override_target = on
        self.override_expires_at = time.time() + ttl * 3600
        return {"on": on, "ttl_h": ttl, "expires_at": self.override_expires_at}

    def release_override(self) -> None:
        self.override_target = None
        self.override_expires_at = None

    def snapshot(self) -> dict:
        from dataclasses import asdict

        return {
            "state": asdict(self.state),
            "role": self.role,
            "override_target": self.override_target,
            "override_expires_at": self.override_expires_at,
        }
