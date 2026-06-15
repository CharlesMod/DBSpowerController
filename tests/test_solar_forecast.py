"""solar_forecast: coefficient chain — optical-density ensemble, temperature
derate, equipment derate, watts conversion, and graceful fallback.

Network is never touched — tests inject `self.data` directly (the shape
`_fetch()` produces) and exercise the static helpers with synthetic inputs.
"""

import math

from datetime import datetime

from helpers import FakeConfig
from cube_power.bus import Bus
from cube_power.decisions import DecisionLog
from cube_power.solar_forecast import SolarForecast


def _fc(tmp_path, **sf):
    log = DecisionLog(tmp_path / "dec.jsonl", Bus())
    cfg = FakeConfig(solar_forecast=sf) if sf else FakeConfig()
    return SolarForecast(cfg, log, tmp_path / "fc_cache.json")


def _today_iso():
    from cube_power import solar_forecast as m
    return m._today_local().isoformat()


def _sample(minute, ghi=0.0, toa=0.0, trans=0.0, eta_temp=1.0, temp_c=None,
            wind=None, cloud=0.0):
    return {"min": minute, "toa": toa, "trans": trans, "ghi": ghi,
            "temp_c": temp_c, "wind": wind, "eta_temp": eta_temp, "cloud": cloud}


# ---- optical-density ensemble ----

def test_ensemble_transmittance_median_optical_density():
    # TOA 1000. Three models: Kt .5, .55, and a blunder-clear .8 -> taus
    # -ln(.5)=.693, -ln(.55)=.598, -ln(.8)=.223. median tau = .598 ->
    # transmittance exp(-.598) = .55. The clear outlier is rejected.
    models = ["a", "b", "c"]
    hourly = {
        "shortwave_radiation_a": [500.0], "terrestrial_radiation_a": [1000.0],
        "shortwave_radiation_b": [550.0], "terrestrial_radiation_b": [1000.0],
        "shortwave_radiation_c": [800.0], "terrestrial_radiation_c": [1000.0],
        "cloud_cover_a": [85], "cloud_cover_b": [80], "cloud_cover_c": [5],
    }
    trans, cloud = SolarForecast._ensemble_transmittance(hourly, models, 1)
    assert round(trans[0], 3) == round(math.exp(-(-math.log(0.55))), 3) == 0.55
    assert cloud[0] == 80


def test_ensemble_transmittance_clamps_and_skips_missing():
    models = ["a", "b"]
    hourly = {
        "shortwave_radiation_a": [9000.0], "terrestrial_radiation_a": [1000.0],  # Kt clamps .85
        "shortwave_radiation_b": [None], "terrestrial_radiation_b": [1000.0],    # skipped
        "cloud_cover_a": [0],
    }
    trans, _ = SolarForecast._ensemble_transmittance(hourly, models, 1)
    assert round(trans[0], 3) == round(0.85, 3)


# ---- temperature derate ----

def test_eta_temp_hot_loses_power(tmp_path):
    fc = _fc(tmp_path, temperature={"enabled": True, "gamma_per_c": -0.004,
                                    "faiman_u0": 25.0, "faiman_u1": 6.84})
    # Hot, sunny, calm: T_cell = 35 + 900/25 = 71C -> eta = 1 - .004*46 = .816
    eta = fc._eta_temp(900.0, 35.0, 0.0)
    assert abs(eta - (1 + -0.004 * (35 + 900 / 25 - 25))) < 1e-9
    assert eta < 0.85

def test_eta_temp_disabled_or_missing(tmp_path):
    fc = _fc(tmp_path, temperature={"enabled": False})
    assert fc._eta_temp(900.0, 35.0, 1.0) == 1.0
    fc2 = _fc(tmp_path)                       # no temp data -> neutral
    assert fc2._eta_temp(900.0, None, None) == 1.0


# ---- watts chain ----

def test_watts_full_chain(tmp_path):
    fc = _fc(tmp_path, equipment_derate=0.9, default_system_capacity_kw=2.0,
             default_plane_factor=1.0)
    # 2kW * GHI/1000 * plane 1.0 * eta_temp .8 * equip .9, GHI 1000 -> 1440
    s = _sample(720, ghi=1000.0, eta_temp=0.8)
    assert fc._watts(s, [fc._array_for("X")]) == 2000.0 * 0.8 * 0.9


def test_array_for_resolves_plane_factor(tmp_path):
    fc = _fc(tmp_path, arrays=[
        {"unit_id": "A", "system_capacity_kw": 1.44, "plane_factor": 1.3}],
        default_plane_factor=1.0, default_system_capacity_kw=1.0)
    assert fc._array_for("A") == {"capacity_kw": 1.44, "plane_factor": 1.3}
    assert fc._array_for("B") == {"capacity_kw": 1.0, "plane_factor": 1.0}


def test_queries_when_available(tmp_path):
    fc = _fc(tmp_path, equipment_derate=1.0, default_system_capacity_kw=1.0,
             default_plane_factor=1.0)
    fc.data = {"date": _today_iso(), "fetched": 0,
               "samples": [_sample(0), _sample(720, ghi=1000.0, eta_temp=1.0),
                           _sample(1425)]}
    assert fc.available()
    assert fc.expected_w_for_units(["X"], datetime(2026, 1, 1, 12, 7)) == 1000.0
    series = fc.series_w_for_units(["X"])
    assert (720, 1000.0) in series and len(series) == 3
    assert round(fc.kwh_expected_for_units(["X"]), 3) == 0.25
    assert fc.kwh_expected_for_units(["X"], upto_min=100) == 0.0


def test_stale_schema_cache_is_discarded(tmp_path):
    # A cache written by an older model version (no/old "v") must not be loaded.
    from cube_power import solar_forecast as m
    p = tmp_path / "fc_cache.json"
    p.write_text('{"date": "%s", "fetched": 0, "samples": [{"min": 720, "ghi": 960}]}'
                 % _today_iso())                 # legacy shape, no "v"
    fc = _fc(tmp_path)
    assert fc.data == {}                          # rejected on load
    assert not fc.available()
    # a current-schema cache loads fine
    p.write_text('{"v": %d, "date": "%s", "fetched": 0, "samples": []}'
                 % (m._SCHEMA_VERSION, _today_iso()))
    fc2 = _fc(tmp_path)
    assert fc2.data.get("v") == m._SCHEMA_VERSION


def test_unavailable_returns_none(tmp_path):
    fc = _fc(tmp_path)
    assert not fc.available()
    assert fc.series_w_for_units(["X"]) is None
    assert fc.expected_w_for_units(["X"]) is None
    assert fc.kwh_expected_for_units(["X"]) is None


def test_snapshot_exposes_coefficient_stack(tmp_path):
    fc = _fc(tmp_path, equipment_derate=0.9)
    fc.data = {"date": _today_iso(), "fetched": 1.0, "samples": [
        _sample(0), _sample(1425),
        # ensure a sample lands on the current 15-min bucket
    ]}
    # inject a sample at the current bucket so breakdown_now is populated
    from cube_power import solar_forecast as m
    now = m._now_local()
    bucket = (now.hour * 60 + now.minute) // 15 * 15
    fc.data["samples"].append(_sample(bucket, toa=980.0, trans=0.55, ghi=539.0,
                                      eta_temp=0.93, temp_c=22.0, cloud=70))
    snap = fc.snapshot()
    assert snap["available"] is True
    b = snap["breakdown_now"]
    assert b["toa_wm2"] == 980.0 and b["transmittance"] == 0.55
    assert b["eta_temp"] == 0.93 and b["equipment_derate"] == 0.9
