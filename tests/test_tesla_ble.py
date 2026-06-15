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
    t._last_reconnect = {}
    t._reconnect_tries = {}
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


class _FakeBeetleTS(_FakeBeetle):
    """_FakeBeetle with a controllable freshness heartbeat + reconnect capture."""

    def __init__(self, values, connected=True, age=0.0):
        super().__init__(values, connected=connected)
        self._age = age
        self.client = self
        self.switch_calls = []

    def last_data_at(self):
        import time
        return time.time() - self._age

    # stand in for APIClient.switch_command
    def switch_command(self, key, on):
        self.switch_calls.append((key, on))


def _tesla_ts(values, age=0.0, dry=False):
    cfg = SimpleNamespace(
        get=lambda k, d=None: (False if k == "tesla_dry_run" and not dry else
                               (True if k == "tesla_force_refresh_on_read" else d)),
        getf=lambda k, d=None: d,
    )
    t = TeslaBle.__new__(TeslaBle)
    t.cfg = cfg
    t.log = _StubLog()
    t.cars = {"VIN": TeslaState(vin="VIN", name="Car")}
    b = _FakeBeetleTS(values, age=age)
    b.keys["ble_connection"] = 777
    t.beetles = {"VIN": b}
    t._last_reconnect = {}
    t._reconnect_tries = {}
    return t, b


@pytest.mark.anyio
async def test_nan_rssi_is_rejected():
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0,
        "ble_signal": float("nan"), "ble_connection": False,
    }, age=5.0)
    car = await t.refresh("VIN")
    assert car.rssi is None              # NaN must not become an int
    assert car.data_fresh is False


@pytest.mark.anyio
async def test_reconnect_backs_off_exponentially():
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0, "ble_connection": False,
    }, age=5.0)
    # first refresh: link down -> one reconnect toggle, tries -> 1
    await t.refresh("VIN")
    assert t._reconnect_tries["VIN"] == 1
    first = len(b.switch_calls)
    assert first >= 2                     # off + on
    # immediate second refresh: throttle now doubled, so NO new toggle
    await t.refresh("VIN")
    assert len(b.switch_calls) == first   # backed off, did not thrash
    assert t._reconnect_tries["VIN"] == 1


@pytest.mark.anyio
async def test_reconnect_tries_reset_on_healthy_link():
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0, "ble_connection": False,
    }, age=5.0)
    await t.refresh("VIN")
    assert t._reconnect_tries["VIN"] == 1
    # link recovers
    b._values["ble_connection"] = True
    car = await t.refresh("VIN")
    assert car.reachable is True
    assert t._reconnect_tries["VIN"] == 0   # backoff cleared


@pytest.mark.anyio
async def test_fresh_link_marks_reachable_and_rssi():
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0, "charger_voltage": 118.0,
        "ble_signal": -72.0, "ble_connection": True,
    }, age=5.0)
    car = await t.refresh("VIN")
    assert car.reachable is True
    assert car.data_fresh is True
    assert car.rssi == -72
    assert car.ble_connected is True
    assert b.switch_calls == []           # healthy link → no reconnect


@pytest.mark.anyio
async def test_connected_but_stale_does_not_reconnect():
    # ble_connection True but timestamps old (idle car stops pushing). The link
    # is UP — must stay reachable and must NOT reconnect (that was the churn bug).
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0, "charger_voltage": 118.0,
        "ble_signal": float("nan"), "ble_connection": True,
    }, age=400.0)                          # way past stale_after default 150
    car = await t.refresh("VIN")
    assert car.reachable is True           # link up -> reachable
    assert car.data_fresh is False         # but data is old (re-poll, not reconnect)
    assert b.switch_calls == []            # NO reconnect on mere staleness


@pytest.mark.anyio
async def test_ble_connection_false_reconnects():
    t, b = _tesla_ts({
        "charging": "Charging", "battery": 60.0,
        "ble_signal": -90.0, "ble_connection": False,
    }, age=5.0)                            # data young, but link reports down
    car = await t.refresh("VIN")
    assert car.ble_connected is False
    assert car.data_fresh is False
    assert car.reachable is False
    assert (777, False) in b.switch_calls and (777, True) in b.switch_calls
