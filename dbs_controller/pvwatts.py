"""NREL PVWatts v8 — modeled solar output, for a qualitative panel-health check.

Diagnostic only. Never feeds the control loop. Once per day it fetches the hourly
DC output (8760-hour TMY) for each configured array and caches it to disk. At
runtime `expected_w(unit_id)` returns the modeled DC watts for the current hour,
which the dashboard compares against the measured `solar_in_w`.

Each array is configured in `config.yaml` (`pvwatts_arrays`) with its capacity,
tracker type (array_type), and the site ZIP code (`pvwatts_zip`). Needs a free
NREL API key.

PVWatts v8 docs: https://developer.nrel.gov/docs/solar/pvwatts/v8/
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import Config
from .decisions import DecisionLog

API_URL = "https://developer.nrel.gov/api/pvwatts/v8.json"
CACHE_MAX_AGE_S = 30 * 86400  # TMY data is static; refetch monthly at most


class PvWatts:
    def __init__(self, cfg: Config, log: DecisionLog, cache_path: Path):
        self.cfg = cfg
        self.log = log
        self.cache_path = cache_path
        # unit_id -> {"dc": [8760 floats], "ac_annual": float, "fetched": ts, "params": {}}
        self.modeled: dict[str, dict] = {}
        self._stop = asyncio.Event()
        self._load_cache()

    def stop(self) -> None:
        self._stop.set()

    # ---- cache ----

    def _load_cache(self) -> None:
        if self.cache_path.exists():
            try:
                self.modeled = json.loads(self.cache_path.read_text())
            except Exception:
                self.modeled = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self.modeled))
        except Exception as e:
            self.log.log("pvwatts", "cache-write-error", error=str(e))

    # ---- fetch ----

    def _arrays(self) -> list[dict]:
        return self.cfg.get("pvwatts_arrays", []) or []

    def _fetch_array(self, api_key: str, spec: dict) -> dict:
        params = {
            "api_key": api_key,
            "address": str(self.cfg.get("pvwatts_zip", "")),
            "system_capacity": spec.get("system_capacity_kw", 1.44),
            "module_type": spec.get("module_type", 0),
            "losses": spec.get("losses_pct", 14),
            "array_type": spec["array_type"],
            "tilt": spec.get("tilt", 20),
            "azimuth": spec.get("azimuth", 180),
            "timeframe": "hourly",
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        outputs = doc.get("outputs", {})
        return {
            "dc": outputs.get("dc", []),
            "ac_annual": outputs.get("ac_annual"),
            "solrad_annual": outputs.get("solrad_annual"),
            "fetched": time.time(),
            "params": params,
        }

    async def refresh(self, force: bool = False) -> None:
        api_key = self.cfg.get("nrel_api_key")
        if not api_key:
            return
        for spec in self._arrays():
            uid = spec.get("unit_id")
            if not uid:
                continue
            cached = self.modeled.get(uid)
            if (not force and cached and cached.get("dc")
                    and time.time() - cached.get("fetched", 0) < CACHE_MAX_AGE_S):
                continue
            try:
                data = await asyncio.to_thread(self._fetch_array, api_key, spec)
                if data.get("dc"):
                    self.modeled[uid] = data
                    self.log.log("pvwatts", "fetched", unit=uid,
                                  array_type=spec["array_type"],
                                  ac_annual=data.get("ac_annual"))
            except Exception as e:
                self.log.log("pvwatts", "fetch-error", unit=uid, error=str(e))
        self._save_cache()

    async def run(self) -> None:
        """Daily refresh loop."""
        while not self._stop.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=86400)
            except asyncio.TimeoutError:
                pass

    # ---- query ----

    @staticmethod
    def _hour_of_year(when: datetime | None = None) -> int:
        when = when or datetime.now()
        idx = (when.timetuple().tm_yday - 1) * 24 + when.hour
        return min(max(idx, 0), 8759)

    def expected_w(self, unit_id: str, when: datetime | None = None) -> float | None:
        data = self.modeled.get(unit_id)
        if not data or not data.get("dc"):
            return None
        dc = data["dc"]
        idx = self._hour_of_year(when)
        if idx >= len(dc):
            return None
        return float(dc[idx])

    def snapshot(self) -> dict:
        out = {}
        for uid, data in self.modeled.items():
            out[uid] = {
                "expected_w": self.expected_w(uid),
                "ac_annual_kwh": data.get("ac_annual"),
                "fetched": data.get("fetched"),
            }
        return out
