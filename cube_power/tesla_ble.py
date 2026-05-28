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
}


def _load_psk(secrets_path: Path) -> str:
    text = Path(secrets_path).read_text()
    m = re.search(r'^api_encryption_key:\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError(f"api_encryption_key not found in {secrets_path}")
    return m.group(1)


class _Beetle:
    """One persistent connection to one Beetle, with auto-reconnect."""

    def __init__(self, vin: str, host: str, psk: str):
        self.vin = vin
        self.host = host
        self.psk = psk
        self.client = APIClient(host, 6053, "", noise_psk=psk)
        self._reconnect: ReconnectLogic | None = None
        self.connected = False
        self.states: dict[int, object] = {}        # key -> EntityState
        self.keys: dict[str, int] = dict(_DEFAULT_KEYS)
        self._subscribe_unsub = None

    async def start(self) -> None:
        async def on_connect() -> None:
            self.connected = True
            try:
                entities, _ = await self.client.list_entities_services()
                obj_to_key = {e.object_id: e.key for e in entities}
                # only override defaults we can resolve; ignore missing entities
                for name in list(self.keys.keys()):
                    obj = "charger" if name in ("charger_binary", "charger_switch") else name
                    if (k := obj_to_key.get(obj)) is not None:
                        self.keys[name] = k
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

    def get(self, name: str):
        key = self.keys.get(name)
        if key is None:
            return None
        s = self.states.get(key)
        return getattr(s, "state", None) if s is not None else None


class TeslaBle:
    def __init__(self, cfg: Config, log: DecisionLog):
        self.cfg = cfg
        self.log = log
        self.cars: dict[str, TeslaState] = {}
        self.beetles: dict[str, _Beetle] = {}

        secrets_path = cfg.get(
            "tesla_beetle_secrets_path",
            "/home/cmod/esphome-tesla-ble/secrets.yaml",
        )
        default_host = cfg.get("tesla_beetle_host")
        psk: str | None = None

        for entry in cfg.get("tesla_vins", []) or []:
            vin = entry["vin"]
            self.cars[vin] = TeslaState(vin=vin, name=entry.get("name", vin[-6:]))
            host = entry.get("beetle_host", default_host)
            if not host:
                continue  # logged when refresh() is called
            if psk is None:
                try:
                    psk = _load_psk(Path(secrets_path))
                except Exception as e:
                    log.log("tesla", "init-error", error=f"psk: {e}")
                    return
            self.beetles[vin] = _Beetle(vin, host, psk)

    # ---- lifecycle ----

    async def start(self) -> None:
        for b in self.beetles.values():
            try:
                await b.start()
            except Exception as e:
                self.log.log("tesla", "connect-error", vin=b.vin, error=str(e))

    async def stop(self) -> None:
        for b in self.beetles.values():
            try:
                await b.stop()
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
            car.last_error = "beetle disconnected"
            return car

        # Optional nudge: ask the Beetle to re-poll the car now.
        if self.cfg.get("tesla_force_refresh_on_read", False):
            try:
                b.client.button_command(b.keys["force_data_update"])
                await asyncio.sleep(1.5)
            except Exception:
                pass

        car.reachable = True
        car.last_error = None

        asleep = b.get("asleep")
        car.awake = (asleep is False) if asleep is not None else False

        cs = b.get("charging")
        if isinstance(cs, str) and cs:
            car.charging_state = cs
            n = _norm_state(cs)
            car.charging = n in _ACTIVE_STATES
            if n in _PLUGGED_STATES:
                car.plugged_in = True
            elif n in _DISCONNECTED_STATES:
                car.plugged_in = False
        elif (plugged := b.get("charger_binary")) is not None:
            # No charging_state text yet (early in connection lifecycle) —
            # fall back to the binary sensor.
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
        if (volt := b.get("charger_voltage")) is not None:
            try:
                car.charger_voltage = int(volt)
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
