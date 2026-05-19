"""Tesla control over local BLE, via the `tesla-control` Go binary.

`tesla-control` (teslamotors/vehicle-command) both sends commands and reads state
over BLE. Verified command surface used here:

  - `state charge`        -> full charge telemetry (SoC, charging state, set amps,
                             actual current, voltage). Requires infotainment AWAKE.
  - `body-controller-state` -> works when asleep; reachability only, no charge data.
  - `wake`                -> wake infotainment so `state charge` works.
  - `charging-set-amps N`, `charging-start`, `charging-stop` -> the control writes.

CLI flags (from pkg/cli/config.go): `-ble`, `-vin`, `-key-file`, `-key-name`,
`-session-cache`, `-connect-timeout`. Pairing (one-time, Phase 0):
    tesla-control -ble -vin VIN add-key-request <public_key.pem> charging_manager cloud_key
then tap an NFC key card to the car. `charging_manager` is the least-privilege role
that can control charging.

All BLE flakiness is contained here; every call runs the binary as a subprocess
with a hard timeout so the coordinator tick never blocks on Bluetooth.
"""

from __future__ import annotations

import json
import time

import anyio

from .config import Config
from .decisions import DecisionLog
from .types import TeslaState

# charging_state oneof values that mean a cable is physically connected
_PLUGGED_STATES = {"Charging", "Starting", "Stopped", "Complete", "NoPower", "Calibrating"}
_ACTIVE_STATES = {"Charging", "Starting"}


class TeslaBle:
    def __init__(self, cfg: Config, log: DecisionLog):
        self.cfg = cfg
        self.log = log
        self.cars: dict[str, TeslaState] = {}
        for entry in cfg.get("tesla_vins", []) or []:
            vin = entry["vin"]
            self.cars[vin] = TeslaState(vin=vin, name=entry.get("name", vin[-6:]))

    # ---- subprocess plumbing ----

    def _base_args(self, vin: str) -> list[str]:
        args = [self.cfg.get("tesla_control_bin", "tesla-control"), "-ble", "-vin", vin]
        if key_file := self.cfg.get("tesla_key_file"):
            args += ["-key-file", str(key_file)]
        if key_name := self.cfg.get("tesla_key_name"):
            args += ["-key-name", str(key_name)]
        if cache := self.cfg.get("tesla_session_cache"):
            args += ["-session-cache", str(cache)]
        connect = int(self.cfg.getf("tesla_connect_timeout_s", 20))
        args += ["-connect-timeout", f"{connect}s"]
        return args

    async def _run(self, vin: str, *cmd: str) -> tuple[bool, str]:
        argv = self._base_args(vin) + list(cmd)
        timeout = self.cfg.getf("tesla_ble_timeout_s", 35)
        try:
            with anyio.fail_after(timeout):
                result = await anyio.run_process(argv, check=False)
            text = (result.stdout or b"").decode("utf-8", "replace").strip()
            err = (result.stderr or b"").decode("utf-8", "replace").strip()
            ok = result.returncode == 0
            return ok, text if ok else (err or text or f"exit {result.returncode}")
        except TimeoutError:
            return False, "ble timeout"
        except FileNotFoundError:
            return False, f"{argv[0]} not found"
        except Exception as e:
            return False, str(e)

    # ---- state read ----

    @staticmethod
    def _parse_charge(text: str, car: TeslaState) -> None:
        """Populate `car` from `state charge` protojson output."""
        doc = json.loads(text)
        cs = doc.get("chargeState", doc)  # GetState may or may not nest it

        raw_state = cs.get("chargingState")
        if isinstance(raw_state, dict) and raw_state:
            car.charging_state = next(iter(raw_state))
        elif isinstance(raw_state, str):
            car.charging_state = raw_state

        if car.charging_state:
            car.plugged_in = car.charging_state not in ("Disconnected", "Unknown")
            car.charging = car.charging_state in _ACTIVE_STATES

        if (lvl := cs.get("batteryLevel")) is not None:
            car.car_soc_pct = float(lvl)
        if (amps := cs.get("chargingAmps")) is not None:
            car.set_amps = int(amps)
        if (act := cs.get("chargerActualCurrent")) is not None:
            car.actual_amps = int(act)
        if (volt := cs.get("chargerVoltage")) is not None:
            car.charger_voltage = int(volt)

    async def refresh(self, vin: str) -> TeslaState | None:
        """Read charge state for one car. Wakes infotainment if asleep, once."""
        car = self.cars.get(vin)
        if car is None:
            return None
        car.updated_at = time.time()

        ok, text = await self._run(vin, "state", "charge")
        if not ok and self.cfg.get("tesla_wake_for_state", True):
            await self._run(vin, "wake")
            ok, text = await self._run(vin, "state", "charge")

        if ok:
            try:
                self._parse_charge(text, car)
                car.reachable = True
                car.awake = True
                car.last_error = None
                return car
            except (ValueError, KeyError) as e:
                car.last_error = f"parse error: {e}"

        # state read failed — fall back to a reachability probe
        reach, btext = await self._run(vin, "body-controller-state")
        car.reachable = reach
        car.awake = False
        car.charging = None
        car.plugged_in = None
        if not ok:
            car.last_error = text
        elif not reach:
            car.last_error = btext
        return car

    async def refresh_all(self) -> None:
        async with anyio.create_task_group() as tg:
            for vin in self.cars:
                tg.start_soon(self.refresh, vin)

    def get(self, vin: str) -> TeslaState | None:
        return self.cars.get(vin)

    # ---- commands (honor tesla_dry_run) ----

    def _dry(self) -> bool:
        return bool(self.cfg.get("tesla_dry_run", self.cfg.get("dry_run", True)))

    async def set_amps(self, vin: str, amps: int) -> bool:
        if self._dry():
            self.log.log("tesla", "set-amps", vin=vin, amps=amps,
                          applied=False, dry_run=True)
            return False
        ok, text = await self._run(vin, "charging-set-amps", str(amps))
        self.log.log("tesla", "set-amps", vin=vin, amps=amps,
                      applied=ok, error=None if ok else text)
        if ok and (car := self.cars.get(vin)):
            car.set_amps = amps
        return ok

    async def start_charging(self, vin: str) -> bool:
        if self._dry():
            self.log.log("tesla", "start", vin=vin, applied=False, dry_run=True)
            return False
        ok, text = await self._run(vin, "charging-start")
        self.log.log("tesla", "start", vin=vin, applied=ok,
                      error=None if ok else text)
        if ok and (car := self.cars.get(vin)):
            car.charging = True
        return ok

    async def stop_charging(self, vin: str) -> bool:
        if self._dry():
            self.log.log("tesla", "stop", vin=vin, applied=False, dry_run=True)
            return False
        ok, text = await self._run(vin, "charging-stop")
        self.log.log("tesla", "stop", vin=vin, applied=ok,
                      error=None if ok else text)
        if ok and (car := self.cars.get(vin)):
            car.charging = False
        return ok
