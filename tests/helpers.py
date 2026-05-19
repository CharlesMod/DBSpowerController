"""Shared test helpers: a dict-backed config, fake units, a coordinator builder.

The controller carries sane inline defaults in every `cfg.getf(key, default)`
call, so `FakeConfig()` with no data exercises the code's own defaults; pass
overrides only where a test cares. FakeConfig has no `dry_run` key, so it
resolves to True — the actuator logs instead of doing real Tuya I/O.
"""

from __future__ import annotations

import time
from pathlib import Path

from dbs_controller.bus import Bus
from dbs_controller.controller import Coordinator
from dbs_controller.decisions import DecisionLog
from dbs_controller.types import DeviceState
from dbs_controller.unit import Unit

DPS = {1: "soc_pct", 103: "solar_in_w", 108: "ac_out_w", 109: "ac_on"}


class FakeConfig:
    def __init__(self, **data):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def getf(self, key, default):
        try:
            return float(self.data.get(key, default))
        except (TypeError, ValueError):
            return default

    def reload(self, force=False):
        return False


def make_unit(uid: str, name: str | None = None) -> Unit:
    return Unit({"id": uid, "name": name or uid, "ip": "0.0.0.0"}, dict(DPS))


def feed(unit: Unit, soc=None, solar=0.0, ac_out=0.0,
         ac_on=False, online=True, age=0.0) -> DeviceState:
    """Record one DeviceState on a unit. `age` backdates updated_at (seconds)."""
    s = DeviceState(
        unit_id=unit.unit_id, name=unit.name, ip=unit.ip, online=online,
        soc_pct=soc, solar_in_w=solar, ac_out_w=ac_out, ac_on=ac_on,
        updated_at=time.time() - age,
    )
    unit.record(s)
    return s


def make_coordinator(cfg: FakeConfig, n_units: int = 2, log_path: Path | None = None):
    """Build a Coordinator with `n_units` fake units (ids A, B, …)."""
    units = {chr(ord("A") + i): make_unit(chr(ord("A") + i), f"DBS {chr(ord('A') + i)}")
             for i in range(n_units)}
    bus = Bus()
    log = DecisionLog(log_path or Path("/tmp/cp_pytest.jsonl"), bus)
    coord = Coordinator(units, cfg, bus, log)
    return coord, units
