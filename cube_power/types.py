"""Pure data types for cube-power. No logic, no I/O."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class UnitRole(StrEnum):
    """Per-unit status — a reporting enum, not a controller."""

    NORMAL = "NORMAL"      # healthy, available to feed the bus
    OVERRIDE = "OVERRIDE"  # manual hold
    FLOORED = "FLOORED"    # SoC at/below floor, dropped from the bus
    OFFLINE = "OFFLINE"    # poller has no fresh data


class BalanceState(StrEnum):
    BALANCED = "BALANCED"
    REBALANCING = "REBALANCING"


@dataclass
class DeviceState:
    """Normalized snapshot of one DBS unit from the Tuya poller."""

    unit_id: str
    name: str
    ip: str
    online: bool = False
    soc_pct: float | None = None
    solar_in_w: float | None = None
    ac_out_w: float | None = None
    ac_in_w: float | None = None
    ac_on: bool | None = None
    temp_c: float | None = None
    mode: str | None = None          # dp127 working mode (standby_mode, ...)
    raw_dps: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class TeslaState:
    """Latest known state of one Tesla, read over BLE via `tesla-control state charge`."""

    vin: str
    name: str
    reachable: bool = False             # any BLE command reached the car
    awake: bool = False                 # infotainment awake (a `state` read succeeded)
    charging_state: str | None = None   # Charging|Disconnected|Stopped|Complete|NoPower|...
    plugged_in: bool | None = None
    charging: bool | None = None
    set_amps: int | None = None         # charging_amps — the current we control
    actual_amps: int | None = None      # charger_actual_current — what's really flowing
    charger_voltage: int | None = None
    car_soc_pct: float | None = None    # battery_level
    minutes_to_full: int | None = None  # from Tesla protocol charge_state.minutes_to_full_charge
    updated_at: float = 0.0
    last_error: str | None = None


@dataclass
class GroupSnapshot:
    """Per bus-group view: which car, plug state, balance state, units feeding it."""

    group_id: str
    vin: str | None = None
    car_name: str | None = None
    plugged_in: bool | None = None
    last_plugged_at: float | None = None    # epoch; None if never seen plugged
    want_bus_live: bool = False             # coordinator wants inverters on
    units: list[str] = field(default_factory=list)
    units_on: int = 0
    balance_state: str = BalanceState.BALANCED
    weak_unit: str | None = None
    charging_commanded: bool | None = None  # last Tesla on/off intent (None = no-op)
    note: str = ""


@dataclass
class CoordinatorSnapshot:
    """What the coordinator decided this tick — surfaced to the dashboard.

    Cars are one-per-bus-group. Each group is an independent subsystem (today:
    two DBS units feeding Tessa; later a second group will land for the Anker
    + the other car). The aggregate fields below sum across all groups for the
    top-line dashboard; `groups` holds per-group detail.
    """

    tick_at: float = field(default_factory=time.time)
    n_cars: int = 0                 # number of plugged-in cars across all groups
    units_on: int = 0               # units the coordinator wants feeding any bus
    total_solar_w: float = 0.0
    total_ac_out_w: float = 0.0
    desired_ac: dict[str, bool] = field(default_factory=dict)  # unit_id -> AC on
    actuator_ready: bool = False    # the ac_on DP is mapped/writable
    groups: dict[str, GroupSnapshot] = field(default_factory=dict)
    note: str = ""
