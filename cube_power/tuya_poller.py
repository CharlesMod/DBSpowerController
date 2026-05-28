"""Per-unit Tuya poller (TCP/6668).

Polls each unit with a SHORT-LIVED connection every `poll_interval_s`, holding
the unit's io_lock so it never collides with an actuator write — the DBS units
accept only one connection at a time. Read-only.

dp158 is a structured status string; `parse_dp158()` extracts the power values.
It lags badly (tens of seconds) — fine here, since control is gated by SoC and
AC state, not by watts.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict
from typing import Any

import tinytuya

from .bus import Bus
from .config import Config
from .types import DeviceState
from .unit import Unit

_BRACE = re.compile(r"\{([^}]*)\}")


def _num(field: str) -> float:
    cleaned = re.sub(r"[^0-9.]", "", field)
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def parse_dp158(s: str) -> dict[str, float]:
    """Extract power/voltage values from the dp158 telemetry string.

    Five brace-delimited segments in a firmware-fixed order: AC input, AC output,
    PV, INV battery terminal, temps. Returns ac_in_w, ac_out_w, solar_in_w,
    battery_v, battery_w; missing/garbled segments are omitted.
    """
    groups = _BRACE.findall(s or "")
    out: dict[str, float] = {}
    if len(groups) >= 1:
        f = groups[0].split(",")
        if len(f) >= 3:
            out["ac_in_w"] = _num(f[2])
    if len(groups) >= 2:
        f = groups[1].split(",")
        if len(f) >= 3:
            out["ac_out_w"] = _num(f[2])
    if len(groups) >= 3:
        f = groups[2].split(",")
        if len(f) >= 3:
            out["solar_in_w"] = _num(f[2])
    if len(groups) >= 4:
        f = groups[3].split(",")
        if len(f) >= 3:
            out["battery_v"] = _num(f[0])
            out["battery_w"] = _num(f[2])
    return out


def normalize(unit: Unit, dps: dict[str, Any]) -> DeviceState:
    # Carry prior values forward so a partial dps payload never nulls a field
    # it simply didn't include.
    prev = unit.state
    s = DeviceState(
        unit_id=unit.unit_id, name=unit.name, ip=unit.ip, online=True,
        updated_at=time.time(),
        soc_pct=prev.soc_pct, solar_in_w=prev.solar_in_w, ac_out_w=prev.ac_out_w,
        ac_in_w=prev.ac_in_w, ac_on=prev.ac_on, temp_c=prev.temp_c, mode=prev.mode,
        raw_dps={**prev.raw_dps, **{str(k): v for k, v in dps.items()}},
    )
    for dp_id, value in dps.items():
        try:
            key = unit.dps_map.get(int(dp_id))
        except (TypeError, ValueError):
            continue
        if not key:
            continue
        try:
            if key == "soc_pct":
                s.soc_pct = float(value)
            elif key == "solar_in_w":
                s.solar_in_w = float(value)
            elif key == "ac_out_w":
                s.ac_out_w = float(value)
            elif key == "ac_in_w":
                s.ac_in_w = float(value)
            elif key == "ac_on":
                s.ac_on = bool(value)
            elif key == "temp_c":
                s.temp_c = float(value)
            elif key == "mode":
                s.mode = str(value)
            elif key == "telemetry":
                tele = parse_dp158(str(value))
                if "solar_in_w" in tele:
                    s.solar_in_w = tele["solar_in_w"]
                if "ac_out_w" in tele:
                    s.ac_out_w = tele["ac_out_w"]
                if "ac_in_w" in tele:
                    s.ac_in_w = tele["ac_in_w"]
        except (TypeError, ValueError):
            continue
    return s


def _read_status(unit: Unit) -> Any:
    d = tinytuya.Device(unit.unit_id, unit.ip, unit.spec["key"], version=unit.version)
    d.set_socketTimeout(5)
    try:
        return d.status()
    finally:
        try:
            d.close()
        except Exception:
            pass


async def poll_unit(unit: Unit, cfg: Config, bus: Bus, stop: asyncio.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        ok = False
        try:
            async with unit.io_lock:
                data = await asyncio.to_thread(_read_status, unit)
            if isinstance(data, dict) and "dps" in data:
                state = normalize(unit, data["dps"])
                unit.record(state)
                bus.publish({"type": "state", "unit": unit.unit_id,
                             "state": asdict(state)})
                ok = True
                backoff = 1.0
            else:
                raise RuntimeError(f"bad status response: {data}")
        except Exception as e:
            offline = DeviceState(unit_id=unit.unit_id, name=unit.name, ip=unit.ip,
                                  online=False, updated_at=time.time())
            unit.record(offline)
            bus.publish({"type": "state", "unit": unit.unit_id,
                         "state": asdict(offline), "error": str(e)})
            backoff = min(backoff * 2, 60.0)

        delay = cfg.getf("poll_interval_s", 10) if ok else backoff
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
