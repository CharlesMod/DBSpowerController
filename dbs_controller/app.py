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
from .decisions import DecisionLog  # noqa: E402
from .pvwatts import PvWatts  # noqa: E402
from .tuya_poller import poll_unit  # noqa: E402
from .unit import Unit  # noqa: E402

# Tesla amperage control is deferred to Phase 2. dbs_controller/tesla_ble.py remains
# in the tree, dormant — not wired into the service.

CONFIG_PATH = ROOT / "config.yaml"
DEVICES_PATH = ROOT / "devices.json"
DECISIONS_LOG = ROOT / "decisions.jsonl"
PVWATTS_CACHE = ROOT / "pvwatts_cache.json"
STATIC_DIR = ROOT / "static"


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
    coordinator = Coordinator(units, cfg, bus, log)
    pvwatts = PvWatts(cfg, log, PVWATTS_CACHE)

    app.state.cfg = cfg
    app.state.bus = bus
    app.state.units = units
    app.state.coordinator = coordinator
    app.state.pvwatts = pvwatts

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    for u in units.values():
        tasks.append(asyncio.create_task(poll_unit(u, cfg, bus, stop), name=f"poll-{u.name}"))
    if units:
        tasks.append(asyncio.create_task(coordinator.run(), name="coordinator"))
    tasks.append(asyncio.create_task(pvwatts.run(), name="pvwatts"))
    tasks.append(asyncio.create_task(_config_watcher(cfg, stop), name="config-watcher"))

    try:
        yield
    finally:
        stop.set()
        coordinator.stop()
        pvwatts.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(lifespan=lifespan, title="DBSpowerController")


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
