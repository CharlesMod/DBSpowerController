"""Tuya write path: switch a unit's AC inverter (dp109) on/off.

Uses a short-lived connection held under the unit's io_lock, so it never
collides with the poller — the DBS units accept only one connection at a time.
The Tuya `set_value` response is inspected for an error payload, so a silently
failed write is reported as not-applied (not a false success).

This function does NOT decide *whether* to write — the coordinator owns that
(it tracks the last-commanded value). set_ac just performs the write.
"""

from __future__ import annotations

import asyncio
from typing import Any

import tinytuya

from .config import Config
from .decisions import DecisionLog
from .unit import Unit


def _ac_dp(unit: Unit) -> int | None:
    return next((k for k, v in unit.dps_map.items() if v == "ac_on"), None)


def _write(unit: Unit, dp: int, on: bool) -> Any:
    d = tinytuya.Device(unit.unit_id, unit.ip, unit.spec["key"], version=unit.version)
    d.set_socketTimeout(5)
    try:
        return d.set_value(dp, on)
    finally:
        try:
            d.close()
        except Exception:
            pass


async def set_ac(unit: Unit, on: bool, reason: str, cfg: Config, log: DecisionLog) -> bool:
    """Drive the unit's AC inverter to `on`. Returns True if the write landed."""
    if cfg.get("dry_run", True):
        log.log("actuator", reason, unit=unit.unit_id, name=unit.name,
                target=on, applied=False, dry_run=True)
        return False

    ac_dp = _ac_dp(unit)
    if ac_dp is None:
        log.log("actuator", reason, unit=unit.unit_id, name=unit.name,
                target=on, applied=False, error="ac_on DP not mapped")
        return False

    try:
        async with unit.io_lock:
            res = await asyncio.to_thread(_write, unit, ac_dp, on)
        err = res.get("Error") if isinstance(res, dict) else None
        applied = err is None
        log.log("actuator", reason, unit=unit.unit_id, name=unit.name,
                target=on, applied=applied, error=err)
        return applied
    except Exception as e:
        log.log("actuator", reason, unit=unit.unit_id, name=unit.name,
                target=on, applied=False, error=str(e))
        return False
