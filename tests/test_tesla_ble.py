"""tesla_ble: parsing of `tesla-control state charge` protojson output."""

import json

from dbs_controller.tesla_ble import TeslaBle
from dbs_controller.types import TeslaState


def _car():
    return TeslaState(vin="VIN", name="Car")


def test_parse_charging_nested():
    text = json.dumps({"chargeState": {
        "chargingState": {"Charging": {}},
        "batteryLevel": 64,
        "chargingAmps": 10,
        "chargerActualCurrent": 9,
        "chargerVoltage": 121,
    }})
    car = _car()
    TeslaBle._parse_charge(text, car)
    assert car.charging_state == "Charging"
    assert car.plugged_in is True
    assert car.charging is True
    assert car.car_soc_pct == 64
    assert car.set_amps == 10
    assert car.actual_amps == 9
    assert car.charger_voltage == 121


def test_parse_disconnected():
    text = json.dumps({"chargeState": {
        "chargingState": {"Disconnected": {}}, "batteryLevel": 80}})
    car = _car()
    TeslaBle._parse_charge(text, car)
    assert car.charging_state == "Disconnected"
    assert car.plugged_in is False
    assert car.charging is False


def test_parse_stopped_is_plugged_not_charging():
    text = json.dumps({"chargeState": {
        "chargingState": {"Stopped": {}}, "batteryLevel": 55}})
    car = _car()
    TeslaBle._parse_charge(text, car)
    assert car.charging_state == "Stopped"
    assert car.plugged_in is True
    assert car.charging is False


def test_parse_non_nested_chargestate():
    # GetState may emit the charge fields at the top level
    text = json.dumps({"chargingState": {"Charging": {}}, "batteryLevel": 42})
    car = _car()
    TeslaBle._parse_charge(text, car)
    assert car.charging is True
    assert car.car_soc_pct == 42


def test_parse_string_charging_state():
    text = json.dumps({"chargeState": {"chargingState": "Charging", "batteryLevel": 30}})
    car = _car()
    TeslaBle._parse_charge(text, car)
    assert car.charging_state == "Charging"
    assert car.charging is True
