"""Build the JSON payload for the ambient-display dashboard.

One endpoint, one fetch per tick, everything the UI needs in one shot.
Pulls from live coordinator + unit state, the telemetry.csv tail, and
the decisions log. Loss tally is computed inline. The "expected" forecast
curve comes from solar_forecast.SolarForecast (Open-Meteo, weather-aware) when
available, falling back to a synthetic clear-sky bell (_expected_solar_w) when
the forecast hasn't been fetched or the API is unreachable.

Multi-car layout: each bus_group becomes a "cell" in the response, with
its own units, car (if any VIN is bound), recommendation, harvested kWh,
delivered miles, etc. The UI iterates `cells` to render one column per
group. Aggregate totals stay at the top level for headers/footers.
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config


# Display timezone for clock, sunrise/sunset minutes, and "today" telemetry
# bucketing. telemetry.csv stores naive timestamps in America/Chicago — the
# writer (app.py) does `datetime.now(America/Chicago).replace(tzinfo=None)`,
# NOT system/UTC time — so we parse them as display-local with no conversion.
# (The host runs in UTC; the bug that pushed "best day" off by one was using a
# UTC `date.today()` for the filter, not the telemetry tz — fixed by reading
# "today" from _today_local() below.)
_DISPLAY_TZ = ZoneInfo("America/Chicago")
_TELEMETRY_TZ = _DISPLAY_TZ   # telemetry.csv is written in display-local time


def _now_local() -> datetime:
    return datetime.now(_DISPLAY_TZ)


def _today_local() -> date:
    return _now_local().date()


def _parse_telemetry_ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=_TELEMETRY_TZ)


# ── parameters (overridable from config.yaml) ────────────────────────
# These mirror the user's actual array sizing.
_PEAK_W_PER_ARRAY      = 1440.0
_NOMINAL_EFF           = 0.78         # ~78% of nameplate at clear-sky peak
_PACK_KWH_PER_UNIT     = 1.44
_CAR_MI_PER_KWH        = 4.0          # pack-side rated efficiency (Model 3)
# Onboard-charger AC→DC conversion + charging overhead. ac_out_w is measured at
# our inverter (AC side); only ~90% reaches the pack as usable range, so apply
# this when converting delivered AC energy to drivable miles. (kWh "delivered to
# car" stays the gross AC figure — that's genuinely what left our system.)
_CHARGE_EFFICIENCY     = 0.90
_INVERTER_EFFICIENCY   = 0.92         # 8% AC conversion loss
_IDLE_W_PER_UNIT       = 20.0
_TRACKER_DRIFT_PCT     = 0.40         # threshold for "tracker mismatch"


def _sunrise_sunset_min() -> tuple[int, int]:
    """Approximate sunrise/sunset for ZIP 60084 (Chicago area), May/June."""
    return (5 * 60 + 20, 20 * 60)


def _bell(mins: float, peak: float = None, half_w: float = 320.0) -> float:
    if peak is None:
        sr, ss = _sunrise_sunset_min()
        peak = (sr + ss) / 2
    x = (mins - peak) / half_w
    return math.exp(-x * x * 1.4)


def _expected_solar_w(mins: int, n_arrays: int = 2) -> float:
    """Clear-sky expected solar W at a given minute-of-day."""
    sr, ss = _sunrise_sunset_min()
    if mins < sr or mins > ss:
        return 0.0
    return _PEAK_W_PER_ARRAY * _bell(mins) * _NOMINAL_EFF * n_arrays


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_today_telemetry(path: Path) -> list[dict]:
    """Return today's rows from telemetry.csv (since local midnight)."""
    if not path.exists():
        return []
    today = _today_local()
    rows = []
    try:
        with open(path) as fh:
            for r in csv.DictReader(fh):
                try:
                    t = _parse_telemetry_ts(r["iso_time"])
                    if t.date() != today:
                        continue
                except (ValueError, KeyError):
                    continue
                rows.append({
                    "t": t, "name": r["name"], "role": r["role"],
                    "soc": _safe_float(r["soc_pct"]),
                    "solar_w": _safe_float(r["solar_in_w"]) or 0.0,
                    "ac_out_w": _safe_float(r["ac_out_w"]) or 0.0,
                    "ac_on": r["ac_on"] == "True",
                })
    except Exception:
        return []
    return rows


def _read_lifetime(path: Path) -> dict:
    """Sum all telemetry rows ever for lifetime totals."""
    if not path.exists():
        return {"kwh": 0, "miles": 0, "best_day_kwh": 0.0, "best_day_label": "—",
                "streak": 0}
    daily: dict[date, float] = defaultdict(float)
    daily_out: dict[date, float] = defaultdict(float)
    try:
        with open(path) as fh:
            for r in csv.DictReader(fh):
                try:
                    t = _parse_telemetry_ts(r["iso_time"])
                except (ValueError, KeyError):
                    continue
                # telemetry interval is ~60s; integrate as W * (60/3600) Wh
                daily[t.date()] += (_safe_float(r["solar_in_w"]) or 0.0) / 60.0
                daily_out[t.date()] += (_safe_float(r.get("ac_out_w")) or 0.0) / 60.0
    except Exception:
        pass
    total_wh = sum(daily.values())
    total_kwh = total_wh / 1000
    if daily:
        best_day, best_wh = max(daily.items(), key=lambda kv: kv[1])
        best_label = best_day.strftime("%a")
        best_kwh = best_wh / 1000
    else:
        best_label, best_kwh = "—", 0.0
    streak = 0
    d = _today_local()
    while True:
        if daily.get(d, 0) / 1000 >= 5:
            streak += 1
            d = d - timedelta(days=1)
        else:
            break
    miles = int(sum(daily_out.values()) / 1000 * _CHARGE_EFFICIENCY * _CAR_MI_PER_KWH)
    return {
        "kwh": round(total_kwh, 0),
        "miles": miles,
        "best_day_kwh": round(best_kwh, 1),
        "best_day_label": best_label,
        "streak": streak,
    }


def _compute_today_series(today_rows: list[dict],
                          filter_unit_names: set[str] | None = None) -> dict:
    """Aggregate per-minute time series. Optionally filter to specific unit names."""
    by_t_solar: dict[datetime, float] = defaultdict(float)
    by_t_soc:   dict[datetime, list[float]] = defaultdict(list)
    for r in today_rows:
        if filter_unit_names is not None and r["name"] not in filter_unit_names:
            continue
        by_t_solar[r["t"]] += r["solar_w"]
        if r["soc"] is not None:
            by_t_soc[r["t"]].append(r["soc"])
    actual = sorted(
        ((t.hour * 60 + t.minute, round(w, 1)) for t, w in by_t_solar.items()),
        key=lambda x: x[0]
    )
    soc = sorted(
        ((t.hour * 60 + t.minute, round(sum(v) / len(v), 1))
         for t, v in by_t_soc.items() if v),
        key=lambda x: x[0]
    )
    return {"actual": actual, "soc": soc}


def _compute_forecast_series(n_arrays: int = 2,
                              sky_factor: float = 1.0) -> list[tuple[int, float]]:
    """Synthetic clear-sky forecast bell across the day."""
    out = []
    for m in range(0, 1440, 15):
        out.append((m, round(_expected_solar_w(m, n_arrays=n_arrays) * sky_factor, 1)))
    return out


def _compute_losses(today_rows: list[dict]) -> dict:
    ac_out_wh = sum(r["ac_out_w"] for r in today_rows) / 60.0
    conversion = ac_out_wh * (1 - _INVERTER_EFFICIENCY)

    idle_wh = 0.0
    for r in today_rows:
        if r["ac_on"] and r["ac_out_w"] < 30:
            idle_wh += _IDLE_W_PER_UNIT / 60.0

    clipping_wh = 0.0
    for r in today_rows:
        if r["soc"] is not None and r["soc"] >= 99 and r["solar_w"] > 200:
            clipping_wh += r["solar_w"] / 60.0

    by_t: dict[datetime, dict[str, float]] = defaultdict(dict)
    for r in today_rows:
        by_t[r["t"]][r["name"]] = r["solar_w"]
    drift_wh = 0.0
    for t, m in by_t.items():
        if len(m) < 2:
            continue
        vals = list(m.values())
        a, b = vals[0], vals[1]
        if max(a, b) >= 300 and min(a, b) < _TRACKER_DRIFT_PCT * max(a, b):
            drift_wh += (max(a, b) - min(a, b)) * 0.5 / 60.0

    return {
        "clipping":   round(clipping_wh / 1000, 2),
        "conversion": round(conversion / 1000, 2),
        "idle":       round(idle_wh / 1000, 2),
        "drift":      round(drift_wh / 1000, 2),
        "total":      round((clipping_wh + conversion + idle_wh + drift_wh) / 1000, 2),
    }


def _recommendation_for_group(group_units: list, car, sun_w: float,
                              car_label: str, cfg: Config) -> dict:
    """Plain-English advisory based on one group's state. Phase-2 hook
    for predictive logic per-group."""
    floor = cfg.getf("soc_floor_pct", 33)
    rehab = floor + cfg.getf("soc_rehab_band_pct", 7)
    avg_soc = None
    socs = [u.state.soc_pct for u in group_units if u.state.soc_pct is not None]
    if socs:
        avg_soc = sum(socs) / len(socs)

    if car and car.charging and (car.charger_voltage or 0) >= 150:
        return {"level": "info",
                "text": f"{car_label} on external charger ({car.charger_voltage}V) — bus held off"}

    if avg_soc and avg_soc >= 95 and sun_w > 200 and not (car and car.charging):
        return {"level": "bad",
                "text": f"Batteries near 100% — plug {car_label} in to capture incoming solar"}

    if avg_soc and avg_soc <= floor:
        return {"level": "warn",
                "text": f"Below floor (avg {avg_soc:.0f}%) — waiting for rehab at {rehab:.0f}%"}

    if car and car.plugged_in and not car.charging:
        return {"level": "info",
                "text": f"{car_label} plugged in — waiting for sufficient solar"}

    if car and car.charging:
        amps = car.set_amps or 0
        watts = (car.set_amps or 0) * (car.charger_voltage or 120)
        return {"level": "info",
                "text": f"Charging {car_label} · {amps} A · {int(watts)} W"}

    if sun_w > 200:
        return {"level": "warn",
                "text": f"{car_label} not connected · {int(sun_w)} W going to batteries"}

    now = _now_local()
    sr, ss = _sunrise_sunset_min()
    mins = now.hour * 60 + now.minute
    if mins < sr:
        return {"level": "info", "text": f"Pre-dawn — {car_label} bus idle"}
    if mins > ss:
        return {"level": "info", "text": f"Past sundown — {car_label} bus on stored energy"}

    return {"level": "info", "text": f"{car_label} group steady"}


def _last_action(decisions_path: Path) -> dict:
    if not decisions_path.exists():
        return {"age_seconds": None, "text": "no decisions yet"}
    try:
        with open(decisions_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = 4096
            fh.seek(max(0, size - chunk))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return {"age_seconds": None, "text": "—"}
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            age = time.time() - d.get("t", 0)
            return {
                "age_seconds": int(age),
                "text": f"{d.get('source','?')}/{d.get('reason','?')}",
            }
        except Exception:
            continue
    return {"age_seconds": None, "text": "—"}


def _health(units, tesla, cfg: Config) -> dict:
    """Per-system green/warn/bad. One entry per unit + one per Beetle/car link."""
    out = {}
    for u in units.values():
        out[u.name] = "ok" if not u.is_stale(cfg) else "bad"
    if tesla:
        for vin, b in tesla.beetles.items():
            label = f"Beetle · {tesla.cars[vin].name}" if vin in tesla.cars else f"Beetle · {vin[-6:]}"
            out[label] = "ok" if b.connected else "warn"
        for vin, c in tesla.cars.items():
            out[f"{c.name} link"] = "ok" if c.reachable else "warn"
    return out


def _build_cell(coord, units, tesla, today_rows: list[dict],
                group_id: str, vin: str | None = None, forecast=None) -> dict:
    """Build a per-VIN-in-group "cell" dict for the UI.

    Each call returns one cell bound to one car (or a no-car cell when
    `vin` is None). Cells sharing a group also share their `units`,
    `sun_w`, `stored_kwh`, and today's harvest figures — those are
    bus-level facts. Caller iterates VINs per group to emit one cell per car.
    """
    unit_ids = coord.groups.get(group_id, [])
    group_units = [units[uid] for uid in unit_ids if uid in units]
    unit_names = {u.name for u in group_units}

    car = tesla.cars.get(vin) if (tesla and vin) else None
    car_label = car.name if car else f"Group {group_id.upper()}"

    sun_w = sum((u.state.solar_in_w or 0.0) for u in group_units)
    ac_out_w = sum((u.state.ac_out_w or 0.0) for u in group_units
                   if u.state.ac_on and not u.is_stale(coord.cfg))

    # per-unit shaped for UI
    unit_list = []
    for u in group_units:
        unit_list.append({
            "id": u.unit_id,
            "name": u.name,
            "role": str(u.role),
            "soc_pct": u.state.soc_pct,
            "solar_in_w": u.state.solar_in_w,
            "ac_out_w": u.state.ac_out_w,
            "ac_on": u.state.ac_on,
            "max_out_w": u.max_out_w,
        })

    # stored kWh for this group's batteries (sum of unit packs)
    stored_kwh = sum((u.state.soc_pct or 0) / 100 * _PACK_KWH_PER_UNIT
                     for u in group_units)
    capacity_kwh = _PACK_KWH_PER_UNIT * len(group_units)

    # today's curves filtered to this group's units
    series = _compute_today_series(today_rows, filter_unit_names=unit_names)

    # harvested today (this group only)
    kwh_today = sum(r["solar_w"] for r in today_rows if r["name"] in unit_names) / 60.0 / 1000
    # delivered to car today: actual AC inverter output, not a solar-fraction guess.
    # solar_in_w overstates delivery when batteries are full or car isn't plugged in.
    kwh_ac_out = sum(r["ac_out_w"] for r in today_rows if r["name"] in unit_names) / 60.0 / 1000

    # forecast for this group: prefer the real weather-aware curve (Open-Meteo,
    # per this group's actual units), fall back to the synthetic clear-sky bell
    # whenever the forecast hasn't been fetched / the API is unreachable.
    sr, ss = _sunrise_sunset_min()
    mins_now = _now_local().hour * 60 + _now_local().minute
    fc_series = forecast.series_w_for_units(unit_ids) if forecast else None
    if fc_series is not None:
        forecast_series = fc_series
        kwh_expected = forecast.kwh_expected_for_units(unit_ids)
        expected_so_far = forecast.kwh_expected_for_units(unit_ids, upto_min=mins_now)
        forecast_source = "open-meteo"
    else:
        forecast_series = _compute_forecast_series(n_arrays=len(group_units))
        kwh_expected = sum(_expected_solar_w(m, n_arrays=len(group_units)) * 0.25
                           for m in range(sr, ss + 1, 15)) / 1000
        expected_so_far = sum(_expected_solar_w(m, n_arrays=len(group_units)) * 0.25
                              for m in range(sr, min(ss, mins_now) + 1, 15)) / 1000
        forecast_source = "bell"
    if expected_so_far > 0:
        ratio = kwh_today / expected_so_far
        track = ("ok", "on track") if ratio >= 0.9 else \
                ("warn", "trailing") if ratio >= 0.7 else \
                ("bad", "behind")
    else:
        track = ("info", "—")

    # delivered to car: AC inverter output is the direct measure. Miles reflect
    # range banked in the pack, so de-rate by charge efficiency (AC→pack loss).
    # round() rather than int() so a low morning shows the nearest mile.
    kwh_to_car = kwh_ac_out
    miles_to_car = round(kwh_to_car * _CHARGE_EFFICIENCY * _CAR_MI_PER_KWH)

    recommendation = _recommendation_for_group(group_units, car, sun_w, car_label, coord.cfg)

    # car payload for this cell
    car_dict = None
    if car:
        car_dict = {
            "vin": vin, "name": car.name,
            "plugged_in": car.plugged_in,
            "charging": car.charging,
            "charging_state": car.charging_state,
            "car_soc_pct": car.car_soc_pct,
            "set_amps": car.set_amps,
            "actual_amps": car.actual_amps,
            "charger_voltage": car.charger_voltage,
            "minutes_to_full": getattr(car, "minutes_to_full", None),
            "reachable": car.reachable,
            "rssi": getattr(car, "rssi", None),
            "ble_connected": getattr(car, "ble_connected", None),
            "data_fresh": getattr(car, "data_fresh", True),
        }

    # group coordinator snapshot (plug edge, balance state, etc.)
    gsnap = coord.snapshot.groups.get(group_id)
    group_state = asdict(gsnap) if (gsnap and hasattr(gsnap, "__dataclass_fields__")) else None

    return {
        "group_id": group_id,
        "car": car_dict,
        "car_label": car_label,
        "units": unit_list,
        "sun_w": round(sun_w, 0),
        "ac_out_w": round(ac_out_w, 0),
        "stored_kwh": round(stored_kwh, 2),
        "stored_capacity_kwh": round(capacity_kwh, 2),
        "recommendation": recommendation,
        "today": {
            "actual": series["actual"],
            "soc": series["soc"],
            "forecast": forecast_series,
            "forecast_source": forecast_source,
            "kwh_today": round(kwh_today, 1),
            "kwh_expected_today": round(kwh_expected, 1),
            # expected harvest expressed as deliverable car-miles, same basis as
            # miles_to_car (0.7 to-car factor × charge eff × _CAR_MI_PER_KWH).
            "mi_expected_today": round(kwh_expected * 0.7 * _CHARGE_EFFICIENCY * _CAR_MI_PER_KWH),
            "track_level": track[0],
            "track_label": track[1],
            "kwh_delivered_to_car": round(kwh_to_car, 1),
            "miles_to_car": miles_to_car,
        },
        "group_state": group_state,
    }


def _planned_cells_from_config(cfg: Config, tesla, present: set[str]) -> list[dict]:
    """Return cells for groups configured in cfg but not yet provisioned.

    If the car's BLE link is alive (tesla.cars[vin].reachable), surface the
    live car payload so the UI can render her car icon, SoC, plug state etc.
    even before the group's batteries (e.g., Anker) are wired in. The cell
    stays flagged as placeholder so the UI still styles it as the pending
    side of the dashboard.
    """
    out = []
    for entry in cfg.get("tesla_vins", []) or []:
        gid = entry.get("bus_group", "a")
        if gid in present:
            continue
        if not entry.get("vin") and not entry.get("name"):
            continue

        vin = entry.get("vin")
        car_label = entry.get("name") or f"Group {gid.upper()}"
        car = tesla.cars.get(vin) if (tesla and vin) else None
        car_dict = None
        rec_text = f"Awaiting {car_label}"
        if car:
            car_dict = {
                "vin": vin, "name": car.name,
                "plugged_in": car.plugged_in,
                "charging": car.charging,
                "charging_state": car.charging_state,
                "car_soc_pct": car.car_soc_pct,
                "set_amps": car.set_amps,
                "actual_amps": car.actual_amps,
                "charger_voltage": car.charger_voltage,
                "minutes_to_full": getattr(car, "minutes_to_full", None),
                "reachable": car.reachable,
                "rssi": getattr(car, "rssi", None),
                "ble_connected": getattr(car, "ble_connected", None),
                "data_fresh": getattr(car, "data_fresh", True),
            }
            if car.reachable:
                rec_text = f"{car_label} connected · awaiting bus hardware"

        out.append({
            "group_id": gid,
            "placeholder": True,
            "car_label": car_label,
            "car": car_dict,
            "units": [],
            "sun_w": 0,
            "stored_kwh": 0,
            "stored_capacity_kwh": 0,
            "recommendation": {"level": "info", "text": rec_text},
            "today": {"actual": [], "soc": [], "forecast": [],
                      "kwh_today": 0, "kwh_expected_today": 0,
                      "track_level": "info", "track_label": "—",
                      "kwh_delivered_to_car": 0, "miles_to_car": 0},
            "group_state": None,
        })
    return out


def build_dashboard(units, coord, cfg: Config, tesla, telemetry_path: Path,
                    decisions_path: Path, forecast=None) -> dict:
    sun_w = sum((u.state.solar_in_w or 0.0) for u in units.values())
    sr, ss = _sunrise_sunset_min()
    now = _now_local()
    mins = now.hour * 60 + now.minute
    daylight_rem_min = max(0, ss - mins)

    today_rows = _read_today_telemetry(telemetry_path)

    # Cells are per-car: one cell per VIN in each provisioned group.
    # When two VINs share a bus_group (Tessa + Meridith both on DBS),
    # each gets its own cell — they share batteries/sun/stored, differ
    # in the car-specific fields. Groups with units but no VIN still
    # emit one cell so the UI renders the bus.
    vins_by_group: dict[str, list[str]] = {g: [] for g in coord.groups}
    for entry in (cfg.get("tesla_vins", []) or []):
        v = entry.get("vin")
        g = entry.get("bus_group", "a")
        if v and g in vins_by_group:
            vins_by_group[g].append(v)

    cells: list[dict] = []
    for gid in coord.groups:
        vins = vins_by_group.get(gid, [])
        if not vins:
            cells.append(_build_cell(coord, units, tesla, today_rows, gid,
                                     forecast=forecast))
        else:
            for v in vins:
                cells.append(_build_cell(coord, units, tesla, today_rows, gid,
                                         vin=v, forecast=forecast))

    # Plus placeholders for groups configured but not yet provisioned
    cells.extend(_planned_cells_from_config(cfg, tesla, set(coord.groups.keys())))

    losses = _compute_losses(today_rows)
    lifetime = _read_lifetime(telemetry_path)
    health = _health(units, tesla, cfg)
    last_action = _last_action(decisions_path)

    # back-compat: keep the old single-car/single-group fields for any
    # client still consuming them. New UI should use `cells`.
    primary = cells[0] if cells else None

    return {
        "ts": time.time(),
        "clock_iso": now.isoformat(timespec="seconds"),
        "sunrise_min": sr,
        "sunset_min": ss,
        "daylight_remaining_min": daylight_rem_min,
        "sun_now_w": round(sun_w, 0),
        "stored_kwh": round(sum((u.state.soc_pct or 0) / 100 * _PACK_KWH_PER_UNIT
                                 for u in units.values()), 2),
        "stored_capacity_kwh": round(_PACK_KWH_PER_UNIT * len(units), 2),
        # NEW: per-group cells the UI iterates to render columns
        "cells": cells,
        # legacy fields (first cell's values, for v1 UI compatibility)
        "units": (primary or {}).get("units", []),
        "car": (primary or {}).get("car"),
        "groups": {c["group_id"]: c.get("group_state") for c in cells if not c.get("placeholder")},
        # actionable alerts surfaced prominently for the ambient display
        "alerts": [g.alert for g in coord.snapshot.groups.values()
                   if getattr(g, "alert", None)],
        "today": (primary or {}).get("today", {
            "actual": [], "soc": [], "forecast": [],
            "kwh_today": 0, "kwh_expected_today": 0,
            "track_level": "info", "track_label": "—",
            "kwh_delivered_to_car": 0, "miles_to_car": 0,
        }),
        "losses_today": losses,
        "lifetime": lifetime,
        "recommendation": (primary or {}).get("recommendation",
                                              {"level": "info", "text": "—"}),
        "health": health,
        "last_action": last_action,
    }
