"""dp158 telemetry-string parsing (the DBS1400 Pro sensor source)."""

from dbs_controller.tuya_poller import parse_dp158, normalize
from dbs_controller.unit import Unit
from dps_map import MAPS

STANDBY = ("AC输入{0.00V,0.00A,0W,60HZ,使能:1}\n"
           "AC输出{0.00V,0.00A,0W}\n"
           "PV{0.00V,0.00A,0W}\n"
           "INV电池端{52.6V,0.0A,0W,设置:526W}\n"
           "温度{转速:0,INV:36℃,BMS:29℃}\n")

ACTIVE = ("AC输入{0.00V,0.00A,0W,60HZ,使能:1}\n"
          "AC输出{121.4V,9.80A,1190W}\n"
          "PV{38.20V,33.10A,1264W}\n"
          "INV电池端{51.8V,1.4A,74W,设置:526W}\n"
          "温度{转速:1200,INV:48℃,BMS:31℃}\n")


def test_parse_standby_all_zero():
    t = parse_dp158(STANDBY)
    assert t["ac_in_w"] == 0.0
    assert t["ac_out_w"] == 0.0
    assert t["solar_in_w"] == 0.0
    assert t["battery_v"] == 52.6


def test_parse_under_load():
    t = parse_dp158(ACTIVE)
    assert t["solar_in_w"] == 1264.0
    assert t["ac_out_w"] == 1190.0
    assert t["ac_in_w"] == 0.0
    assert t["battery_v"] == 51.8
    assert t["battery_w"] == 74.0


def test_parse_garbage_is_safe():
    assert parse_dp158("") == {}
    assert parse_dp158("not a telemetry string") == {}


def test_normalize_fills_devicestate_from_dp158():
    u = Unit({"id": "A", "name": "A", "ip": "0.0.0.0"}, dict(MAPS["DBS1400Pro"]))
    s = normalize(u, {"1": 64, "10": 31, "127": "inverter_mode", "158": ACTIVE})
    assert s.soc_pct == 64
    assert s.temp_c == 31
    assert s.mode == "inverter_mode"
    assert s.solar_in_w == 1264.0
    assert s.ac_out_w == 1190.0
    assert s.online is True


def test_normalize_merges_partial_push():
    # the DBS pushes partial dps — a push missing SoC must not null it
    u = Unit({"id": "A", "name": "A", "ip": "0.0.0.0"}, dict(MAPS["DBS1400Pro"]))
    u.record(normalize(u, {"1": 50, "10": 30, "109": True, "158": STANDBY}))
    partial = normalize(u, {"10": 31})        # temperature-only push
    assert partial.soc_pct == 50              # carried forward
    assert partial.ac_on is True              # carried forward
    assert partial.temp_c == 31               # updated
