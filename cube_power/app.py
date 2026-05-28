"""FastAPI app: lifespan task wiring, HTTP/WebSocket API, dashboard mount."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dps_map import MAPS  # noqa: E402

from .bus import Bus  # noqa: E402
from .config import Config  # noqa: E402
from .controller import Coordinator  # noqa: E402
from .dashboard import build_dashboard  # noqa: E402
from .decisions import DecisionLog  # noqa: E402
from .pvwatts import PvWatts  # noqa: E402
from .tesla_ble import TeslaBle  # noqa: E402
from .tuya_poller import poll_unit  # noqa: E402
from .unit import Unit  # noqa: E402

# Phase 1 uses tesla_ble.py only for the wake-on-energize hook (waking an asleep
# car when the bus goes live). Full amperage control is still deferred to Phase 2.

CONFIG_PATH = ROOT / "config.yaml"
DEVICES_PATH = ROOT / "devices.json"
DECISIONS_LOG = ROOT / "decisions.jsonl"
PVWATTS_CACHE = ROOT / "pvwatts_cache.json"
TELEMETRY_LOG = ROOT / "telemetry.csv"
STATIC_DIR = ROOT / "static"
TELEMETRY_HEADER = (
    "iso_time,unit_id,name,role,soc_pct,solar_in_w,ac_out_w,ac_in_w,ac_on,mode\n"
)


def load_units() -> dict[str, Unit]:
    if not DEVICES_PATH.exists():
        return {}
    specs = json.loads(DEVICES_PATH.read_text())
    units: dict[str, Unit] = {}
    for s in specs:
        model = s.get("model", "DBS1400Pro")
        dps_map = MAPS.get(model, MAPS["DBS1400Pro"])
        units[s["id"]] = Unit(s, dps_map)
    return units


async def _telemetry_loop(units: dict, cfg: Config, stop: asyncio.Event) -> None:
    """Append one CSV row per unit every `telemetry_interval_s` (default 60).

    Lightweight time series so we have real data for capacity / efficiency
    studies. Header written on first run; thereafter pure append.
    """
    import csv
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _tz = ZoneInfo("America/Chicago")
    new = not TELEMETRY_LOG.exists()
    if new:
        with open(TELEMETRY_LOG, "w") as f:
            f.write(TELEMETRY_HEADER)
    while not stop.is_set():
        try:
            iso = datetime.now(_tz).replace(tzinfo=None).isoformat(timespec="seconds")
            with open(TELEMETRY_LOG, "a", newline="") as f:
                w = csv.writer(f)
                for u in units.values():
                    s = u.state
                    w.writerow([iso, u.unit_id, u.name, u.role,
                                s.soc_pct, s.solar_in_w, s.ac_out_w,
                                s.ac_in_w, s.ac_on, s.mode])
        except Exception:
            pass
        try:
            interval = cfg.getf("telemetry_interval_s", 60)
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _config_watcher(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            cfg.reload()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config(CONFIG_PATH)
    bus = Bus()
    log = DecisionLog(DECISIONS_LOG, bus)
    units = load_units()
    tesla = TeslaBle(cfg, log)
    coordinator = Coordinator(units, cfg, bus, log, tesla=tesla)
    pvwatts = PvWatts(cfg, log, PVWATTS_CACHE)

    app.state.cfg = cfg
    app.state.bus = bus
    app.state.units = units
    app.state.coordinator = coordinator
    app.state.pvwatts = pvwatts

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    await tesla.start()
    for u in units.values():
        tasks.append(asyncio.create_task(poll_unit(u, cfg, bus, stop), name=f"poll-{u.name}"))
    if units:
        tasks.append(asyncio.create_task(coordinator.run(), name="coordinator"))
    tasks.append(asyncio.create_task(pvwatts.run(), name="pvwatts"))
    tasks.append(asyncio.create_task(_config_watcher(cfg, stop), name="config-watcher"))
    if units:
        tasks.append(asyncio.create_task(
            _telemetry_loop(units, cfg, stop), name="telemetry"))

    try:
        yield
    finally:
        stop.set()
        coordinator.stop()
        pvwatts.stop()
        await tesla.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(lifespan=lifespan, title="cube-power")


def _snapshot() -> dict:
    units = app.state.units
    return {
        "devices": [u.snapshot() for u in units.values()],
        "coordinator": app.state.coordinator.state_dict(),
        "pvwatts": app.state.pvwatts.snapshot(),
        "config": app.state.cfg.data,
        "now": time.time(),
    }


@app.get("/api/state")
async def get_state():
    if not app.state.units:
        return JSONResponse({"devices": [], "note": "no devices.json — see README"})
    return _snapshot()


@app.get("/api/dashboard")
async def get_dashboard():
    """Single-fetch bundle for the ambient display."""
    if not app.state.units:
        return JSONResponse({"error": "no devices.json — see README"}, status_code=503)
    try:
        return build_dashboard(
            units=app.state.units,
            coord=app.state.coordinator,
            cfg=app.state.cfg,
            tesla=getattr(app.state.coordinator, "tesla", None),
            telemetry_path=TELEMETRY_LOG,
            decisions_path=DECISIONS_LOG,
        )
    except Exception as e:
        # Don't kill the dashboard tick if backend hiccups — return partial.
        return JSONResponse({"error": str(e), "ts": time.time()}, status_code=500)


@app.get("/api/config")
async def get_config():
    return app.state.cfg.data


@app.post("/api/{unit_id}/override")
async def post_override(unit_id: str, payload: dict):
    units = app.state.units
    if unit_id not in units:
        raise HTTPException(404, "unknown unit")
    if "on" not in payload:
        raise HTTPException(400, "missing 'on'")
    return units[unit_id].request_override(
        bool(payload["on"]), payload.get("ttl_h"), app.state.cfg)


@app.delete("/api/{unit_id}/override")
async def delete_override(unit_id: str):
    units = app.state.units
    if unit_id not in units:
        raise HTTPException(404, "unknown unit")
    units[unit_id].release_override()
    return {"ok": True}


@app.get("/api/log")
async def get_log(source: str | None = None, limit: int = 200):
    if not DECISIONS_LOG.exists():
        return []
    lines = DECISIONS_LOG.read_text().strip().split("\n")[-limit * 4:]
    out = []
    for ln in reversed(lines):
        try:
            entry = json.loads(ln)
        except Exception:
            continue
        if source and entry.get("source") != source:
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


@app.websocket("/api/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    q = app.state.bus.subscribe()
    try:
        await socket.send_json({"type": "snapshot", "data": _snapshot()})
        while True:
            event = await q.get()
            await socket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.bus.unsubscribe(q)


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
