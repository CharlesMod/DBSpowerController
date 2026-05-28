"""tesla_ble: refresh() turns cached Beetle entity state into a TeslaState."""

from types import SimpleNamespace

import pytest

from cube_power.tesla_ble import TeslaBle, _Beetle
from cube_power.types import TeslaState


class _FakeBeetle:
    """Stand-in for _Beetle that returns canned entity values."""

    def __init__(self, values: dict, connected: bool = True):
        self.connected = connected
        self._values = values
        self.vin = "VIN"
        self.keys = {k: i for i, k in enumerate(values)}

    def get(self, name):
        return self._values.get(name)


class _StubLog:
    def log(self, *a, **k):
        pass


def _tesla(values, connected=True):
    cfg = SimpleNamespace(
        get=lambda k, d=None: d,
        getf=lambda k, d=None: d,
    )
    t = TeslaBle.__new__(TeslaBle)
    t.cfg = cfg
    t.log = _StubLog()
    t.cars = {"VIN": TeslaState(vin="VIN", name="Car")}
    t.beetles = {"VIN": _FakeBeetle(values, connected=connected)}
    return t


@pytest.mark.anyio
async def test_refresh_charging():
    t = _tesla({
        "asleep": False,
        "charger_binary": True,
        "charging": "Charging",
        "battery": 64.0,
        "charging_amps": 10.0,
        "charger_current": 9.0,
        "charger_voltage": 121.0,
    })
    car = await t.refresh("VIN")
    assert car.reachable is True
    assert car.awake is True
    assert car.plugged_in is True
    assert car.charging is True
    assert car.charging_state == "Charging"
    assert car.car_soc_pct == 64.0
    assert car.set_amps == 10
    assert car.actual_amps == 9
    assert car.charger_voltage == 121


@pytest.mark.anyio
async def test_refresh_disconnected():
    t = _tesla({
        "asleep": True,
        "charger_binary": False,
        "charging": "Disconnected",
        "battery": 80.0,
    })
    car = await t.refresh("VIN")
    assert car.charging_state == "Disconnected"
    assert car.plugged_in is False
    assert car.charging is False
    assert car.awake is False


@pytest.mark.anyio
async def test_refresh_stopped_is_plugged_not_charging():
    t = _tesla({
        "asleep": False,
        "charger_binary": True,
        "charging": "Stopped",
        "battery": 55.0,
    })
    car = await t.refresh("VIN")
    assert car.charging_state == "Stopped"
    assert car.plugged_in is True
    assert car.charging is False


@pytest.mark.anyio
async def test_refresh_when_beetle_disconnected():
    t = _tesla({}, connected=False)
    car = await t.refresh("VIN")
    assert car.reachable is False
    assert car.last_error == "beetle disconnected"


@pytest.mark.anyio
async def test_refresh_no_power_state_is_plugged():
    # yoziru's firmware emits "No Power" with a space — normalization must
    # still recognize it as plugged-in.
    t = _tesla({
        "asleep": False,
        "charging": "No Power",
        "battery": 91.0,
    })
    car = await t.refresh("VIN")
    assert car.charging_state == "No Power"
    assert car.plugged_in is True
    assert car.charging is False


@pytest.mark.anyio
async def test_refresh_unknown_vin_is_none():
    t = _tesla({})
    assert await t.refresh("MISSING") is None
