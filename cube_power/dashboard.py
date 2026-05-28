"""Build the JSON payload for the ambient-display dashboard.

One endpoint, one fetch per tick, everything the UI needs in one shot.
Pulls from live coordinator + unit state, the telemetry.csv tail, and
the decisions log. Loss tally is computed inline. Forecast is a simple
PVWatts-shaped bell for now (forecast.solar integration is a TODO).
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
# bucketing. telemetry.csv stores naive local-tz timestamps.
_DISPLAY_TZ = ZoneInfo("America/Chicago")


def _now_local() -> datetime:
    return datetime.now(_DISPLAY_TZ)


def _today_local() -> date:
    return _now_local().date()


def _parse_telemetry_ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=_DISPLAY_TZ)


# ── parameters (overridable from config.yaml) ────────────────────────
# These mirror the user's actual array sizing.
_PEAK_W_PER_ARRAY      = 1440.0
_NOMINAL_EFF           = 0.78         # ~78% of nameplate at clear-sky peak
_PACK_KWH_PER_UNIT     = 1.44
_CAR_MI_PER_KWH        = 4.0
_INVERTER_EFFICIENCY   = 0.92         # 8% AC conversion loss
_IDLE_W_PER_UNIT       = 20.0
_TRACKER_DRIFT_PCT     = 0.40         # threshold for "tracker mismatch"


def _sunrise_sunset_min() -> tuple[int, int]:
    """Approximate sunrise/sunset for ZIP 60084 (Chicago area), May/June."""
    # crude — a real version would use NOAA or astral. For ambient-display
    # purposes, a fixed bracket per season is plenty.
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


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_lifetime(path: Path) -> dict:
    """Sum all telemetry rows ever for lifetime totals."""
    if not path.exists():
        return {"kwh": 0, "miles": 0, "best_day_kwh": 0.0, "best_day_label": "—",
                "streak": 0}
    daily: dict[date, float] = defaultdict(float)
    try:
        with open(path) as fh:
            for r in csv.DictReader(fh):
                try:
                    t = _parse_telemetry_ts(r["iso_time"])
                except (ValueError, KeyError):
                    continue
                w = _safe_float(r["solar_in_w"]) or 0.0
                # telemetry interval is ~60s; integrate as W * (60/3600) Wh
                daily[t.date()] += w / 60.0
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
    # naive streak: count consecutive recent days with >5 kWh
    streak = 0
    d = _today_local()
    while True:
        if daily.get(d, 0) / 1000 >= 5:
            streak += 1
            d = d - timedelta(days=1)
        else:
            break
    # car delivery rough = total * 0.7 (after losses + idle)
    miles = int(total_kwh * 0.7 * _CAR_MI_PER_KWH)
    return {
        "kwh": round(total_kwh, 0),
        "miles": miles,
        "best_day_kwh": round(best_kwh, 1),
        "best_day_label": best_label,
        "streak": streak,
    }


def _compute_today_series(today_rows: list[dict]) -> dict:
    """Aggregate per-minute time series for solar (combined), per-unit SoC."""
    by_t_solar: dict[datetime, float] = defaultdict(float)
    by_t_soc:   dict[datetime, list[float]] = defaultdict(list)
    for r in today_rows:
        # bucket per-minute (telemetry is ~60s, so just use t)
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


def _compute_forecast_series(sky_factor: float = 1.0) -> list[tuple[int, float]]:
    """Synthetic clear-sky forecast bell across the day."""
    sr, ss = _sunrise_sunset_min()
    out = []
    for m in range(0, 1440, 15):
        out.append((m, round(_expected_solar_w(m, n_arrays=2) * sky_factor, 1)))
    return out


def _compute_losses(today_rows: list[dict]) -> dict:
    """Per-bucket kWh losses today.

    - conversion: 8% of all AC out
    - inverter idle: ~20 W per unit per minute that ac_on=True and ac_out=~0
    - clipping: each minute SoC≥99% AND solar>200 W, assume 100% lost
    - tracker drift: each paired minute where min(solar_a, solar_b) < 60% of max
                     while max>300 W → lost = (max - min) * 0.5
    """
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

    # pair by timestamp for tracker drift
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


def _recommendation(units, coord, tesla, sun_w: float, kwh_today: float,
                    kwh_expected_today: float) -> dict:
    """Plain-English advisory based on current state. Phase-2 hook for
    predictive logic."""
    # find primary group
    group = next(iter(coord.groups.keys()), "a")
    gsnap = coord.snapshot.groups.get(group)
    vin = coord._group_vin.get(group) if hasattr(coord, "_group_vin") else None
    car = tesla.cars.get(vin) if (tesla and vin) else None

    avg_soc = None
    socs = [u.state.soc_pct for u in units.values() if u.state.soc_pct is not None]
    if socs:
        avg_soc = sum(socs) / len(socs)

    # External-source detection (240V wall charger).
    if car and car.charging and (car.charger_voltage or 0) >= 150:
        return {"level": "info",
                "text": f"Tessa on external charger ({car.charger_voltage}V) — DBS bus held off"}

    # Clipping warning.
    if avg_soc and avg_soc >= 95 and sun_w > 200 and not (car and car.charging):
        return {"level": "bad",
                "text": "Both units near 100% — plug Tessa in to capture incoming solar"}

    # Below floor.
    if avg_soc and avg_soc <= 35:
        return {"level": "warn",
                "text": f"Units below floor (avg {avg_soc:.0f}%) — waiting for rehab at 40%"}

    # Plugged but waiting.
    if car and car.plugged_in and not car.charging:
        return {"level": "info",
                "text": "Tessa plugged in — waiting for sufficient solar"}

    # Actively charging from us.
    if car and car.charging:
        amps = car.set_amps or 0
        watts = (car.set_amps or 0) * (car.charger_voltage or 120)
        return {"level": "info",
                "text": f"Charging Tessa · {amps} A · {int(watts)} W incoming"}

    # Tessa not plugged in but sun making power.
    if sun_w > 200:
        return {"level": "warn",
                "text": f"Tessa not connected · {int(sun_w)} W going to batteries"}

    # Pre/post sunlight.
    now = _now_local()
    sr, ss = _sunrise_sunset_min()
    mins = now.hour * 60 + now.minute
    if mins < sr:
        return {"level": "info", "text": "Pre-dawn — batteries holding overnight"}
    if mins > ss:
        return {"level": "info", "text": "Past sundown — relying on stored energy"}

    return {"level": "info", "text": "Steady — system idle, sun ramping"}


def _last_action(decisions_path: Path) -> dict:
    """Most recent decision-log entry as 'X ago · source/reason'."""
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
    """Per-system green/warn/bad."""
    out = {}
    for u in units.values():
        out[u.name] = "ok" if not u.is_stale(cfg) else "bad"
    # Beetle: assume tesla.beetles[vin].connected
    beetle_ok = False
    tesla_ok = False
    if tesla and tesla.beetles:
        b = next(iter(tesla.beetles.values()))
        beetle_ok = bool(b.connected)
    if tesla and tesla.cars:
        c = next(iter(tesla.cars.values()))
        tesla_ok = bool(c.reachable)
    out["Beetle BLE"] = "ok" if beetle_ok else "warn"
    out["Tessa link"] = "ok" if tesla_ok else "warn"
    return out


def build_dashboard(units, coord, cfg: Config, tesla, telemetry_path: Path,
                    decisions_path: Path) -> dict:
    sun_w = sum((u.state.solar_in_w or 0.0) for u in units.values())
    sr, ss = _sunrise_sunset_min()
    now = _now_local()
    mins = now.hour * 60 + now.minute
    daylight_rem_min = max(0, ss - mins)

    today_rows = _read_today_telemetry(telemetry_path)
    today_series = _compute_today_series(today_rows)
    forecast = _compute_forecast_series()

    # today's harvested kWh (integrate solar over today rows)
    kwh_today = sum(r["solar_w"] for r in today_rows) / 60.0 / 1000

    # forecast for today (sum of expected)
    kwh_expected = sum(_expected_solar_w(m, n_arrays=2) * 0.25
                       for m in range(sr, ss + 1, 15)) / 1000

    # tracking pill
    expected_so_far = sum(_expected_solar_w(m, n_arrays=2) * 0.25
                          for m in range(sr, min(ss, mins) + 1, 15)) / 1000
    if expected_so_far > 0:
        ratio = kwh_today / expected_so_far
        track = ("ok", "on track") if ratio >= 0.9 else \
                ("warn", "trailing") if ratio >= 0.7 else \
                ("bad", "behind")
    else:
        track = ("info", "—")

    losses = _compute_losses(today_rows)
    lifetime = _read_lifetime(telemetry_path)
    recommendation = _recommendation(units, coord, tesla, sun_w,
                                     kwh_today, kwh_expected)
    health = _health(units, tesla, cfg)
    last_action = _last_action(decisions_path)

    # per-unit
    unit_list = []
    for u in units.values():
        unit_list.append({
            "id": u.unit_id,
            "name": u.name,
            "role": str(u.role),
            "soc_pct": u.state.soc_pct,
            "solar_in_w": u.state.solar_in_w,
            "ac_out_w": u.state.ac_out_w,
            "ac_on": u.state.ac_on,
            "bus_group": u.bus_group,
        })

    # car (first VIN we know about)
    car_dict = None
    if tesla and tesla.cars:
        vin, car = next(iter(tesla.cars.items()))
        car_dict = {
            "vin": vin, "name": car.name,
            "plugged_in": car.plugged_in,
            "charging": car.charging,
            "charging_state": car.charging_state,
            "car_soc_pct": car.car_soc_pct,
            "set_amps": car.set_amps,
            "actual_amps": car.actual_amps,
            "charger_voltage": car.charger_voltage,
            "minutes_to_full": car.minutes_to_full,
            "reachable": car.reachable,
        }

    # groups view (mirrors coordinator snapshot.groups)
    groups_dict = {}
    for gid, g in coord.snapshot.groups.items():
        groups_dict[gid] = asdict(g) if hasattr(g, "__dataclass_fields__") else g

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
        "units": unit_list,
        "car": car_dict,
        "groups": groups_dict,
        "today": {
            "actual": today_series["actual"],          # [[min_of_day, W], ...]
            "soc": today_series["soc"],                # [[min_of_day, %], ...]
            "forecast": forecast,                      # [[min_of_day, W], ...]
            "kwh_today": round(kwh_today, 1),
            "kwh_expected_today": round(kwh_expected, 1),
            "track_level": track[0],
            "track_label": track[1],
            "kwh_delivered_to_car": round(kwh_today * 0.7, 1),
            "miles_to_car": int(kwh_today * 0.7 * _CAR_MI_PER_KWH),
            "miles_expected_today": int(kwh_expected * 0.7 * _CAR_MI_PER_KWH),
        },
        "losses_today": losses,
        "lifetime": lifetime,
        "recommendation": recommendation,
        "health": health,
        "last_action": last_action,
    }
