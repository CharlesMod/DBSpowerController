"""Tesla control via an ESPHome BLE proxy (yoziru/esphome-tesla-ble on an ESP32).

cube's onboard Bluetooth can't reach the car at its parking spot, so a Beetle
ESP32-C6 sits closer to the garage running the yoziru firmware. We talk to it
over the ESPHome native API (port 6053, noise-encrypted). The firmware does the
BLE work: it polls the car every ~10 s and pushes entity states to subscribers,
and accepts commands as button/switch/number writes.

Entity surface used here (object_id on the device):
  - sensor       battery, charger_current, charger_voltage     -> read state
  - text_sensor  charging                                      -> charging state
  - binary_sensor charger, asleep, status                      -> plugged / awake / online
  - number       charging_amps                                 -> set amps
  - switch       charger                                       -> start/stop
  - button       wake_up, force_data_update                    -> nudges

Each VIN maps to one Beetle host. Public API matches the old subprocess module
so the coordinator and lifespan code don't change.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from aioesphomeapi import APIClient, ReconnectLogic

from .config import Config
from .decisions import DecisionLog
from .types import TeslaState

_ACTIVE_STATES = {"Charging", "Starting"}
_DISCONNECTED_STATES = {"Disconnected", "Unknown", ""}
# charging_state values that all imply a cable is physically connected.
# The firmware's `charger` binary sensor only reflects active charging, so
# the text state is what we trust for plug detection. Compared after
# whitespace is stripped — yoziru's firmware emits "No Power" (with space).
_PLUGGED_STATES = {"Charging", "Starting", "Stopped", "Complete", "NoPower",
                   "Calibrating"}


def _norm_state(s: str) -> str:
    """Normalize a charging_state string for comparison (drop whitespace)."""
    return "".join(s.split()) if isinstance(s, str) else s

# Entity keys are firmware-version dependent. We resolve them by object_id at
# connect time, but keep these defaults as the values observed on the deployed
# Beetle (esphome-tesla-ble 2026.1.3) for documentation.
_DEFAULT_KEYS = {
    "battery": 2326215132,
    "charger_current": 1026285647,
    "charger_voltage": 3248213610,
    "charging": 3828622232,           # text_sensor
    "charger_binary": 3146088653,     # binary_sensor (plugged in)
    "charger_switch": 3146088653,     # switch (same key — both share id)
    "asleep": 3778954319,
    "status": 939730931,
    "charging_amps": 3848971258,
    "wake_up": 1291771791,
    "force_data_update": 439675811,
    "time_to_full": 994081306,        # sensor, minutes (charge_state.minutes_to_full_charge)
    "ble_signal": 0,                  # sensor, RSSI dBm (link health heartbeat)
    "ble_connection": 0,              # switch, ble_client connected state / reconnect lever
}


def _load_psk(secrets_path: Path) -> str:
    text = Path(secrets_path).read_text()
    m = re.search(r'^api_encryption_key:\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError(f"api_encryption_key not found in {secrets_path}")
    return m.group(1)


class _BeetleHost:
    """One persistent ESPHome connection per device, with auto-reconnect.

    ESPHome only tolerates one noise-encrypted API connection per client,
    so when two VINs live on the same Beetle (dual-VIN firmware) they
    share this connection. The state cache holds EVERY entity from the
    device; each _Beetle resolves its own VIN's subset by name slug.
    """

    def __init__(self, host: str, psk: str):
        self.host = host
        self.psk = psk
        self.client = APIClient(host, 6053, "", noise_psk=psk)
        self._reconnect: ReconnectLogic | None = None
        self.connected = False
        self.states: dict[int, object] = {}        # key -> EntityState (all VINs)
        self.state_ts: dict[int, float] = {}       # key -> last update epoch (freshness)
        self.entities: list = []                   # populated on connect
        self._views: list = []                     # per-VIN _Beetle views

    def attach(self, view: "_Beetle") -> None:
        self._views.append(view)

    async def start(self) -> None:
        async def on_connect() -> None:
            self.connected = True
            try:
                entities, _ = await self.client.list_entities_services()
                self.entities = entities
                for v in self._views:
                    v.resolve_keys(entities)
                self.client.subscribe_states(self._on_state)
            except Exception:
                pass

        async def on_disconnect(expected_disconnect: bool) -> None:
            self.connected = False

        self._reconnect = ReconnectLogic(
            client=self.client,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            zeroconf_instance=None,
        )
        await self._reconnect.start()

    async def stop(self) -> None:
        if self._reconnect:
            await self._reconnect.stop()
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def _on_state(self, state) -> None:
        self.states[state.key] = state
        self.state_ts[state.key] = time.time()


class _Beetle:
    """Per-VIN view of a shared _BeetleHost connection.

    Each view holds its own entity-key map (resolved from the host's
    entity list by prefixing every default object_id with this VIN's
    name slug, with legacy unprefixed fallback). Commands are sent via
    the host's shared APIClient.
    """

    def __init__(self, host: _BeetleHost, vin: str, name: str = ""):
        self.host_obj = host
        self.vin = vin
        # Slug used to resolve the prefixed object_ids emitted by the
        # dual-VIN firmware (e.g. "tessa_battery"). On the original
        # single-VIN firmware the slug is empty and we resolve the
        # unprefixed ids ("battery") unchanged.
        self.name_slug = name.lower().replace(" ", "_") if name else ""
        self.keys: dict[str, int] = dict(_DEFAULT_KEYS)
        host.attach(self)

    @property
    def connected(self) -> bool:
        return self.host_obj.connected

    @property
    def client(self) -> APIClient:
        return self.host_obj.client

    @property
    def host(self) -> str:
        return self.host_obj.host

    def resolve_keys(self, entities) -> None:
        obj_to_key = {e.object_id: e.key for e in entities}
        prefix = (self.name_slug + "_") if self.name_slug else ""
        for name in list(self.keys.keys()):
            obj = "charger" if name in ("charger_binary", "charger_switch") else name
            k = obj_to_key.get(prefix + obj)
            if k is None and prefix:
                k = obj_to_key.get(obj)   # legacy fallback
            if k is not None:
                self.keys[name] = k

    def get(self, name: str):
        key = self.keys.get(name)
        if key is None:
            return None
        s = self.host_obj.states.get(key)
        return getattr(s, "state", None) if s is not None else None

    def last_data_at(self) -> float:
        """Epoch of the most recent push for any of this VIN's polled entities.

        RSSI / voltage / battery are polled on intervals, so a healthy link
        refreshes at least one of them every minute. A timestamp going cold is
        our staleness heartbeat — distinct from a value that just hasn't
        changed. Returns 0.0 if nothing has ever arrived (cold start)."""
        ts = self.host_obj.state_ts
        best = 0.0
        for name in ("ble_signal", "charger_voltage", "battery", "charging",
                     "charger_current"):
            key = self.keys.get(name)
            if key is not None:
                best = max(best, ts.get(key, 0.0))
        return best


class TeslaBle:
    def __init__(self, cfg: Config, log: DecisionLog):
        self.cfg = cfg
        self.log = log
        self.cars: dict[str, TeslaState] = {}
        # Shared per-host connections — multiple VINs on one Beetle
        # share a single ESPHome API connection (the device only
        # tolerates one noise-encrypted session per client).
        self.hosts: dict[str, _BeetleHost] = {}
        self.beetles: dict[str, _Beetle] = {}
        self._last_reconnect: dict[str, float] = {}   # per-VIN reconnect throttle
        self._reconnect_tries: dict[str, int] = {}    # consecutive fails -> backoff

        secrets_path = cfg.get(
            "tesla_beetle_secrets_path",
            "/home/cmod/esphome-tesla-ble/secrets.yaml",
        )
        default_host = cfg.get("tesla_beetle_host")
        psk: str | None = None

        for entry in cfg.get("tesla_vins", []) or []:
            vin = entry["vin"]
            car_name = entry.get("name", vin[-6:])
            self.cars[vin] = TeslaState(vin=vin, name=car_name)
            host = entry.get("beetle_host", default_host)
            if not host:
                continue  # logged when refresh() is called
            if psk is None:
                try:
                    psk = _load_psk(Path(secrets_path))
                except Exception as e:
                    log.log("tesla", "init-error", error=f"psk: {e}")
                    return
            if host not in self.hosts:
                self.hosts[host] = _BeetleHost(host, psk)
            self.beetles[vin] = _Beetle(self.hosts[host], vin, name=car_name)

    # ---- lifecycle ----

    async def start(self) -> None:
        for h in self.hosts.values():
            try:
                await h.start()
            except Exception as e:
                self.log.log("tesla", "connect-error", host=h.host, error=str(e))

    async def stop(self) -> None:
        for h in self.hosts.values():
            try:
                await h.stop()
            except Exception:
                pass

    # ---- state read ----

    async def refresh(self, vin: str) -> TeslaState | None:
        car = self.cars.get(vin)
        if car is None:
            return None
        car.updated_at = time.time()
        b = self.beetles.get(vin)
        if b is None:
            car.last_error = "no beetle configured"
            car.reachable = False
            return car
        if not b.connected:
            car.reachable = False
            car.awake = False
            car.data_fresh = False
            car.last_error = "beetle disconnected"
            return car

        now = time.time()
        last_data = b.last_data_at() if hasattr(b, "last_data_at") else 0.0
        age = (now - last_data) if last_data > 0 else None
        stale_after = self.cfg.getf("tesla_stale_after_s", 150)

        # Adaptive force-refresh: nudge a re-poll only when the data has aged
        # past a small floor, instead of every read. On a weak/flaky link
        # (Meridith runs ~-80 dBm) constant force_data_update adds chatter that
        # can worsen drops; this backs off when data is already flowing.
        min_age = self.cfg.getf("tesla_refresh_min_age_s", 25)
        if (self.cfg.get("tesla_force_refresh_on_read", False)
                and (age is None or age >= min_age)):
            try:
                b.client.button_command(b.keys["force_data_update"])
                await asyncio.sleep(1.5)
                last_data = b.last_data_at() if hasattr(b, "last_data_at") else last_data
                age = (now - last_data) if last_data > 0 else age
            except Exception:
                pass

        # Link health. `ble_connection` (the firmware's GATT connection state)
        # is AUTHORITATIVE for "can we reach the car" — far more reliable than a
        # timestamp heartbeat. ESPHome only PUSHES entity state on change, so a
        # healthy-but-idle car stops pushing and looks "stale" by timestamp; the
        # earlier version reconnected on that false staleness and dropped good
        # links (self-inflicted churn). RSSI (ble_signal) reads NaN on this
        # ESP32-C6 build, so it is not a usable heartbeat either.
        rssi = b.get("ble_signal")
        car.rssi = (int(rssi) if (rssi is not None and rssi == rssi
                                  and not isinstance(rssi, bool)) else None)
        conn = b.get("ble_connection")
        car.ble_connected = bool(conn) if conn is not None else None
        link_up = car.ble_connected if car.ble_connected is not None else b.connected

        if not link_up:
            # Genuine BLE disconnect — reconnect (with backoff).
            car.reachable = False
            car.data_fresh = False
            car.last_error = "ble link down"
            await self._maybe_reconnect(vin, b, now)
            return car

        car.reachable = True
        car.last_error = None
        self._reconnect_tries[vin] = 0          # link healthy → reset backoff
        # Link is up; data can still lag if the car is asleep, but that's a
        # re-poll concern (force_data_update above), NOT a reconnect one.
        car.data_fresh = (age is None) or (age < stale_after)

        asleep = b.get("asleep")
        car.awake = (asleep is False) if asleep is not None else False

        # Pull voltage first; we use it as a plug-detection signal too.
        # User-confirmed semantics:
        #   ~0 V  = no cable at the port
        #   ~2 V  = cable plugged but supply unpowered (Tesla pilot resistor
        #           on the proximity pin, ~5 V × divider)
        #   120 V = on our DBS bus
        #   240 V = on a wall charger
        if (volt := b.get("charger_voltage")) is not None:
            try:
                car.charger_voltage = int(volt)
            except (TypeError, ValueError):
                pass
        voltage_plugged = (car.charger_voltage is not None and car.charger_voltage > 1)

        cs = b.get("charging")
        if isinstance(cs, str) and cs:
            car.charging_state = cs
            n = _norm_state(cs)
            car.charging = n in _ACTIVE_STATES
            if n in _PLUGGED_STATES:
                car.plugged_in = True
            elif voltage_plugged:
                # Firmware reports Disconnected but voltage shows the cable
                # is physically plugged — bus is just off. Trust the wire.
                car.plugged_in = True
            elif n in _DISCONNECTED_STATES:
                car.plugged_in = False
        elif voltage_plugged:
            car.plugged_in = True
        elif (plugged := b.get("charger_binary")) is not None:
            car.plugged_in = bool(plugged)

        if (lvl := b.get("battery")) is not None:
            try:
                car.car_soc_pct = float(lvl)
            except (TypeError, ValueError):
                pass
        if (amps := b.get("charging_amps")) is not None:
            try:
                car.set_amps = int(amps)
            except (TypeError, ValueError):
                pass
        if (act := b.get("charger_current")) is not None:
            try:
                car.actual_amps = int(act)
            except (TypeError, ValueError):
                pass
        if (ttf := b.get("time_to_full")) is not None:
            try:
                car.minutes_to_full = int(ttf)
            except (TypeError, ValueError):
                pass
        return car

    async def refresh_all(self) -> None:
        await asyncio.gather(*(self.refresh(v) for v in self.cars), return_exceptions=True)

    async def _maybe_reconnect(self, vin: str, b, now: float) -> None:
        """Force the ble_client to reconnect when a VIN's link is down.

        Toggling the per-VIN 'BLE Connection' switch off->on makes the firmware
        re-establish the link. But a car can be ASLEEP with its BLE radio off —
        then no amount of toggling connects, so we must NOT thrash it (a prior
        bug logged 1000+ reconnects in one night). Exponential backoff: the gap
        doubles each consecutive failure up to a cap, and resets the moment the
        link reports healthy again.
        """
        if self._dry():
            return
        base = self.cfg.getf("tesla_reconnect_throttle_s", 90)
        cap = self.cfg.getf("tesla_reconnect_max_throttle_s", 900)
        tries = self._reconnect_tries.get(vin, 0)
        throttle = min(base * (2 ** tries), cap)
        if now - self._last_reconnect.get(vin, 0.0) < throttle:
            return
        conn_key = b.keys.get("ble_connection")
        if not conn_key:          # unresolved (placeholder 0) — can't toggle
            return
        self._last_reconnect[vin] = now
        self._reconnect_tries[vin] = tries + 1
        try:
            b.client.switch_command(conn_key, False)
            await asyncio.sleep(1.0)
            b.client.switch_command(conn_key, True)
            self.log.log("tesla", "ble-reconnect", vin=vin,
                         attempt=tries + 1, next_gap_s=int(min(base * (2 ** (tries + 1)), cap)))
        except Exception as e:
            self.log.log("tesla", "ble-reconnect-error", vin=vin, error=str(e))

    def get(self, vin: str) -> TeslaState | None:
        return self.cars.get(vin)

    # ---- commands (honor tesla_dry_run) ----

    def _dry(self) -> bool:
        return bool(self.cfg.get("tesla_dry_run", self.cfg.get("dry_run", True)))

    def _send(self, vin: str, fn, *args) -> tuple[bool, str | None]:
        b = self.beetles.get(vin)
        if b is None:
            return False, "no beetle configured"
        if not b.connected:
            return False, "beetle disconnected"
        try:
            fn(*args)
            return True, None
        except Exception as e:
            return False, str(e)

    async def set_amps(self, vin: str, amps: int) -> bool:
        if self._dry():
            self.log.log("tesla", "set-amps", vin=vin, amps=amps,
                         applied=False, dry_run=True)
            return False
        b = self.beetles.get(vin)
        if b is None:
            self.log.log("tesla", "set-amps", vin=vin, amps=amps,
                         applied=False, error="no beetle configured")
            return False
        ok, err = self._send(vin, b.client.number_command,
                             b.keys["charging_amps"], float(amps))
        self.log.log("tesla", "set-amps", vin=vin, amps=amps,
                     applied=ok, error=err)
        if ok and (car := self.cars.get(vin)):
            car.set_amps = amps
        return ok

    async def start_charging(self, vin: str) -> bool:
        if self._dry():
            self.log.log("tesla", "start", vin=vin, applied=False, dry_run=True)
            return False
        b = self.beetles.get(vin)
        if b is None:
            self.log.log("tesla", "start", vin=vin, applied=False,
                         error="no beetle configured")
            return False
        ok, err = self._send(vin, b.client.switch_command,
                             b.keys["charger_switch"], True)
        self.log.log("tesla", "start", vin=vin, applied=ok, error=err)
        if ok and (car := self.cars.get(vin)):
            car.charging = True
        return ok

    async def stop_charging(self, vin: str) -> bool:
        if self._dry():
            self.log.log("tesla", "stop", vin=vin, applied=False, dry_run=True)
            return False
        b = self.beetles.get(vin)
        if b is None:
            self.log.log("tesla", "stop", vin=vin, applied=False,
                         error="no beetle configured")
            return False
        ok, err = self._send(vin, b.client.switch_command,
                             b.keys["charger_switch"], False)
        self.log.log("tesla", "stop", vin=vin, applied=ok, error=err)
        if ok and (car := self.cars.get(vin)):
            car.charging = False
        return ok

    async def wake(self, vin: str) -> bool:
        if self._dry():
            self.log.log("tesla", "wake", vin=vin, applied=False, dry_run=True)
            return False
        b = self.beetles.get(vin)
        if b is None:
            self.log.log("tesla", "wake", vin=vin, applied=False,
                         error="no beetle configured")
            return False
        ok, err = self._send(vin, b.client.button_command, b.keys["wake_up"])
        self.log.log("tesla", "wake", vin=vin, applied=ok, error=err)
        return ok

    async def wake_all_and_charge(self) -> None:
        """Wake every configured car, then nudge it to charge.

        The wake-on-energize hook: when the DBS bus goes live, a deeply asleep
        Tesla can miss the power-on edge and sit there not charging. Waking it
        makes it re-evaluate, see live power, and start. `start_charging` is a
        belt-and-suspenders nudge (harmless if it's already charging).
        """
        for vin in self.cars:
            if await self.wake(vin):
                await asyncio.sleep(2)
                await self.start_charging(vin)
