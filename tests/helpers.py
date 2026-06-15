"""Shared test helpers: a dict-backed config, fake units, a coordinator builder.

The controller carries sane inline defaults in every `cfg.getf(key, default)`
call, so `FakeConfig()` with no data exercises the code's own defaults; pass
overrides only where a test cares. FakeConfig has no `dry_run` key, so it
resolves to True — the actuator logs instead of doing real Tuya I/O.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from cube_power.bus import Bus
from cube_power.controller import Coordinator
from cube_power.decisions import DecisionLog
from cube_power.types import DeviceState, TeslaState
from cube_power.unit import Unit

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


def make_unit(uid: str, name: str | None = None,
              bus_group: str = "a", max_out_w: int = 1200) -> Unit:
    return Unit({
        "id": uid, "name": name or uid, "ip": "0.0.0.0",
        "bus_group": bus_group, "max_out_w": max_out_w,
    }, dict(DPS))


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


def make_coordinator(cfg: FakeConfig, n_units: int = 2, log_path: Path | None = None,
                     unit_groups: list[str] | None = None):
    """Build a Coordinator with `n_units` fake units (ids A, B, …).

    `unit_groups` lets a test put units in distinct bus groups; default is
    all in group 'a'.
    """
    groups = unit_groups or ["a"] * n_units
    units = {chr(ord("A") + i): make_unit(chr(ord("A") + i),
                                          f"DBS {chr(ord('A') + i)}",
                                          bus_group=groups[i])
             for i in range(n_units)}
    bus = Bus()
    log = DecisionLog(log_path or Path("/tmp/cp_pytest.jsonl"), bus)
    # Isolate persisted wall-state per coordinator so tests don't bleed into
    # each other (or into the real repo wall_state.json).
    cfg.data.setdefault("wall_state_path",
                        str(Path(tempfile.mkdtemp()) / "wall_state.json"))
    coord = Coordinator(units, cfg, bus, log)
    return coord, units


class FakeTesla:
    """Stand-in for TeslaBle — tracks calls, supports plug-state mutation.

    Cars are addressed by VIN. `bus_group` per VIN is read by the controller
    out of `cfg.tesla_vins`; tests should set that on FakeConfig accordingly.
    """

    def __init__(self, *, vins=("V1",), group="a", plugged_in=False):
        self.cars: dict[str, TeslaState] = {}
        for v in vins:
            self.cars[v] = TeslaState(vin=v, name=v, plugged_in=plugged_in)
        self.wake_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.set_amps_count = 0
        self._group = group

    async def refresh_all(self):
        # Cache is mutated externally by tests; refresh is a no-op.
        pass

    async def refresh(self, vin):
        return self.cars.get(vin)

    def get(self, vin):
        return self.cars.get(vin)

    async def wake(self, vin):
        self.wake_count += 1
        return True

    async def start_charging(self, vin):
        self.start_count += 1
        if (car := self.cars.get(vin)):
            car.charging = True
        return True

    async def stop_charging(self, vin):
        self.stop_count += 1
        if (car := self.cars.get(vin)):
            car.charging = False
        return True

    async def set_amps(self, vin, amps):
        self.set_amps_count += 1
        if (car := self.cars.get(vin)):
            car.set_amps = amps
        return True

    async def wake_all_and_charge(self):
        # legacy path; no current caller, kept for symmetry
        for vin in self.cars:
            await self.wake(vin)
            await self.start_charging(vin)


def attach_tesla(coord: Coordinator, cfg: FakeConfig, fake: FakeTesla,
                 group: str = "a") -> None:
    """Wire a FakeTesla onto a coordinator with one VIN bound to one group."""
    vins = list(fake.cars.keys())
    cfg.data["tesla_vins"] = [{"vin": v, "bus_group": group} for v in vins]
    coord.tesla = fake
