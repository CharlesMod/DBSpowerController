"""Open-Meteo solar forecast — weather-only expected PV output.

A *pure forecast*: it never reads panel output. "Expected" is the independent
yardstick that makes inefficiencies (soiling, a parked tracker, string mismatch,
shading, degradation) visible as actual-vs-expected gaps. Feeding measurements
back in (bias correction) would erase exactly that signal, so we don't.

Expected DC power is a multiplicative chain of named, physically-meaningful
coefficients (each surfaced in snapshot() so a divergence can be reasoned about
term by term):

    expected_W = TOA                      # top-of-atmosphere GHI — pure geometry
               × transmittance            # multimodal effective optical density
               × plane_factor             # transposition: flat / tilt / tracking
               × eta_temp                 # dynamic cell-temperature derate
               × equipment_derate         # inherent: wiring, mismatch, MPPT,
               × capacity_kW              #   + baseline soiling/degradation

Two upgrades over a naive single-model forecast:

  1. Effective optical density (cloud derating). Open-Meteo's "best_match" is
     unreliable (it chose GFS on an overcast day and predicted full sun). We
     pull several global models, and per hour convert each to optical density
     τ = −ln(Kt) where Kt = GHI_model/TOA, take the MEDIAN τ (optical depths add,
     so the median lives in the physically additive space and rejects the model
     that blunders), then transmittance = exp(−median τ). No hand-tuned weights.

  2. Dynamic temperature. Cell temp from Open-Meteo air temp + wind (Faiman),
     eta_temp = 1 + γ·(T_cell−25). An 8–15% swing on a hot clear day that a flat
     derate can't capture — and an *inherent* loss, so it belongs in expected.

`plane_factor` is per-array: 1.0 for panels flat / pointed up. `equipment_derate`
deliberately holds soiling near a clean baseline so real soiling shows in the gap.

Two cheap calls per refresh (15-min geometry/temp, hourly multi-model), cached to
disk, refreshed every `refresh_min`. Falls back to the dashboard's synthetic bell
when unavailable. https://open-meteo.com/en/docs
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .decisions import DecisionLog

_DISPLAY_TZ = ZoneInfo("America/Chicago")

API_URL = "https://api.open-meteo.com/v1/forecast"
_STEP_MIN = 15
_HOURS_PER_STEP = _STEP_MIN / 60.0   # 0.25 h — for Wh integration

# Bump when the cached sample shape changes, so a stale cache from an older
# model version is discarded instead of being read with mismatched fields.
_SCHEMA_VERSION = 2

_DEFAULT_MODELS = [
    "ecmwf_ifs025", "icon_seamless", "gfs_seamless",
    "gem_seamless", "meteofrance_seamless",
]
# Clearness index bounds (clear-sky tops out ~0.85 of TOA; floor avoids -ln(0)).
_KT_MIN, _KT_MAX = 0.02, 0.85

# Faiman cell-temperature model defaults (W/m²K, W·s/m³·K).
_FAIMAN_U0, _FAIMAN_U1 = 25.0, 6.84

_DEFAULT_LAT = 42.2658
_DEFAULT_LON = -88.1395


def _now_local() -> datetime:
    return datetime.now(_DISPLAY_TZ)


def _today_local() -> date:
    return _now_local().date()


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class SolarForecast:
    def __init__(self, cfg: Config, log: DecisionLog, cache_path: Path):
        self.cfg = cfg
        self.log = log
        self.cache_path = cache_path
        # {"date","fetched","samples":[{min,toa,trans,ghi,temp_c,wind,eta_temp,cloud}]}
        self.data: dict = {}
        self._stop = asyncio.Event()
        self._load_cache()

    def stop(self) -> None:
        self._stop.set()

    # ---- config ----

    def _sf(self) -> dict:
        return self.cfg.get("solar_forecast", {}) or {}

    def enabled(self) -> bool:
        return bool(self._sf().get("enabled", True))

    def _models(self) -> list[str]:
        return self._sf().get("models", _DEFAULT_MODELS) or _DEFAULT_MODELS

    def _equipment_derate(self) -> float:
        # Inherent electrical losses + baseline soiling/degradation. Temperature
        # is NOT in here — it's the separate dynamic eta_temp term.
        return float(self._sf().get("equipment_derate", 0.92))

    def _temp_cfg(self) -> dict:
        return self._sf().get("temperature", {}) or {}

    def _array_for(self, unit_id: str) -> dict:
        sf = self._sf()
        for a in (sf.get("arrays", []) or []):
            if a.get("unit_id") == unit_id:
                return {
                    "capacity_kw": float(a.get("system_capacity_kw",
                                               sf.get("default_system_capacity_kw", 1.44))),
                    "plane_factor": float(a.get("plane_factor",
                                                sf.get("default_plane_factor", 1.0))),
                }
        return {
            "capacity_kw": float(sf.get("default_system_capacity_kw", 1.44)),
            "plane_factor": float(sf.get("default_plane_factor", 1.0)),
        }

    # ---- cache ----

    def _load_cache(self) -> None:
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
                # Discard caches written by an older sample schema.
                self.data = data if data.get("v") == _SCHEMA_VERSION else {}
            except Exception:
                self.data = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self.data))
        except Exception as e:
            self.log.log("solar_forecast", "cache-write-error", error=str(e))

    # ---- physics ----

    @staticmethod
    def _ensemble_transmittance(hourly: dict, models: list[str],
                                n: int) -> tuple[list[float], list[float]]:
        """Per-hour effective transmittance (via median optical density) and
        median total cloud across models."""
        trans_by_hour: list[float] = []
        cloud_by_hour: list[float] = []
        for i in range(n):
            taus, clouds = [], []
            for m in models:
                rad = _num(hourly.get(f"shortwave_radiation_{m}", [None] * n)[i])
                toa = _num(hourly.get(f"terrestrial_radiation_{m}", [None] * n)[i])
                if rad is not None and toa and toa > 5:
                    kt = min(max(rad / toa, _KT_MIN), _KT_MAX)
                    taus.append(-math.log(kt))           # optical density (additive)
                c = _num(hourly.get(f"cloud_cover_{m}", [None] * n)[i])
                if c is not None:
                    clouds.append(c)
            trans_by_hour.append(math.exp(-statistics.median(taus)) if taus else 0.0)
            cloud_by_hour.append(statistics.median(clouds) if clouds else 0.0)
        return trans_by_hour, cloud_by_hour

    def _eta_temp(self, poa: float, t_air: float | None, wind: float | None) -> float:
        """Dynamic temperature derate from POA irradiance + air temp + wind."""
        tc = self._temp_cfg()
        if not tc.get("enabled", True) or t_air is None:
            return 1.0
        u0 = float(tc.get("faiman_u0", _FAIMAN_U0))
        u1 = float(tc.get("faiman_u1", _FAIMAN_U1))
        gamma = float(tc.get("gamma_per_c", -0.004))
        t_cell = t_air + poa / (u0 + u1 * (wind or 0.0))
        eta = 1.0 + gamma * (t_cell - 25.0)
        return min(max(eta, 0.70), 1.05)

    # ---- fetch ----

    def _get(self, params: dict) -> dict:
        url = API_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _fetch(self) -> dict:
        sf = self._sf()
        lat, lon = sf.get("lat", _DEFAULT_LAT), sf.get("lon", _DEFAULT_LON)
        models = self._models()

        # Call A: 15-min geometry (TOA), air temp + wind, cloud for display.
        a = self._get({
            "latitude": lat, "longitude": lon,
            "minutely_15": "terrestrial_radiation,temperature_2m,wind_speed_10m,cloud_cover",
            "wind_speed_unit": "ms",
            "timezone": "America/Chicago", "forecast_days": 1,
        })["minutely_15"]
        times = a.get("time", [])
        toa15 = a.get("terrestrial_radiation", [])
        temp15 = a.get("temperature_2m", [])
        wind15 = a.get("wind_speed_10m", [])
        cloud15 = a.get("cloud_cover", [])

        # Call B: hourly multi-model radiation + TOA -> ensemble optical density.
        b = self._get({
            "latitude": lat, "longitude": lon,
            "hourly": "shortwave_radiation,terrestrial_radiation,cloud_cover",
            "models": ",".join(models),
            "timezone": "America/Chicago", "forecast_days": 1,
        })["hourly"]
        nh = len(b.get("time", []))
        trans_hour, _cloud_hour = self._ensemble_transmittance(b, models, nh)

        samples = []
        day = None
        for i, t in enumerate(times):
            day = day or t[:10]
            hh, mm = int(t[11:13]), int(t[14:16])
            toa = _num(toa15[i] if i < len(toa15) else None) or 0.0
            trans = trans_hour[hh] if hh < len(trans_hour) else 0.0
            ghi = toa * trans                                   # all-sky GHI (flat plane)
            t_air = _num(temp15[i] if i < len(temp15) else None)
            wind = _num(wind15[i] if i < len(wind15) else None)
            eta_temp = self._eta_temp(ghi, t_air, wind)
            samples.append({
                "min": hh * 60 + mm,
                "toa": round(toa, 1),
                "trans": round(trans, 3),
                "ghi": round(ghi, 1),
                "temp_c": round(t_air, 1) if t_air is not None else None,
                "wind": round(wind, 1) if wind is not None else None,
                "eta_temp": round(eta_temp, 3),
                "cloud": _num(cloud15[i] if i < len(cloud15) else None) or 0.0,
            })
        return {"v": _SCHEMA_VERSION, "date": day, "fetched": time.time(),
                "samples": samples}

    async def refresh(self, force: bool = False) -> None:
        if not self.enabled():
            return
        today = _today_local().isoformat()
        cur = self.data
        fresh = (cur and cur.get("date") == today and cur.get("samples")
                 and time.time() - cur.get("fetched", 0)
                 < float(self._sf().get("refresh_min", 60)) * 60)
        if fresh and not force:
            return
        try:
            data = await asyncio.to_thread(self._fetch)
            if data.get("samples") and data.get("date") == today:
                self.data = data
                self._save_cache()
                trans_peak = max((s["trans"] for s in data["samples"]), default=0.0)
                self.log.log("solar_forecast", "fetched", date=data["date"],
                             samples=len(data["samples"]), trans_peak=round(trans_peak, 2))
            else:
                self.log.log("solar_forecast", "stale-response",
                             got=data.get("date"), want=today)
        except Exception as e:
            self.log.log("solar_forecast", "fetch-error", error=str(e))

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.refresh()
            wait_s = float(self._sf().get("refresh_min", 60)) * 60
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass

    # ---- query ----

    def available(self) -> bool:
        return bool(self.data.get("samples")
                    and self.data.get("date") == _today_local().isoformat())

    def _watts(self, s: dict, arrays: list[dict]) -> float:
        """Total expected DC watts at one sample: the full coefficient chain."""
        eq = self._equipment_derate()
        ghi = s["ghi"]
        eta_t = s.get("eta_temp", 1.0)
        return sum(a["capacity_kw"] * 1000.0 * (ghi / 1000.0)
                   * a["plane_factor"] * eta_t * eq
                   for a in arrays)

    def series_w_for_units(self, unit_ids: list[str]) -> list[tuple[int, float]] | None:
        if not self.available():
            return None
        arrays = [self._array_for(u) for u in unit_ids]
        return [(s["min"], round(self._watts(s, arrays), 1))
                for s in self.data["samples"]]

    def expected_w_for_units(self, unit_ids: list[str],
                             when: datetime | None = None) -> float | None:
        if not self.available():
            return None
        when = when or _now_local()
        bucket = (when.hour * 60 + when.minute) // _STEP_MIN * _STEP_MIN
        arrays = [self._array_for(u) for u in unit_ids]
        for s in self.data["samples"]:
            if s["min"] == bucket:
                return round(self._watts(s, arrays), 1)
        return None

    def kwh_expected_for_units(self, unit_ids: list[str],
                               upto_min: int | None = None) -> float | None:
        if not self.available():
            return None
        arrays = [self._array_for(u) for u in unit_ids]
        wh = 0.0
        for s in self.data["samples"]:
            if upto_min is not None and s["min"] > upto_min:
                continue
            wh += self._watts(s, arrays) * _HOURS_PER_STEP
        return wh / 1000.0

    def _sample_now(self) -> dict | None:
        if not self.available():
            return None
        bucket = (_now_local().hour * 60 + _now_local().minute) // _STEP_MIN * _STEP_MIN
        return next((s for s in self.data["samples"] if s["min"] == bucket), None)

    def cloud_now_pct(self) -> float | None:
        s = self._sample_now()
        return round(s["cloud"], 0) if s else None

    def snapshot(self) -> dict:
        s = self._sample_now()
        trans_peak = max((x["trans"] for x in self.data.get("samples", [])), default=None)
        # Expose the coefficient stack at "now" so a divergence is diagnosable.
        breakdown = None
        if s:
            breakdown = {
                "toa_wm2": s["toa"],
                "transmittance": s["trans"],
                "ghi_wm2": s["ghi"],
                "air_temp_c": s["temp_c"],
                "eta_temp": s["eta_temp"],
                "equipment_derate": self._equipment_derate(),
            }
        return {
            "available": self.available(),
            "date": self.data.get("date"),
            "fetched": self.data.get("fetched"),
            "samples": len(self.data.get("samples", [])),
            "cloud_now_pct": self.cloud_now_pct(),
            "trans_peak": round(trans_peak, 2) if trans_peak is not None else None,
            "models": self._models(),
            "breakdown_now": breakdown,
        }
