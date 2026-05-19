"""pvwatts: hour-of-year indexing and the modeled-output lookup."""

from datetime import datetime

from helpers import FakeConfig
from dbs_controller.decisions import DecisionLog
from dbs_controller.bus import Bus
from dbs_controller.pvwatts import PvWatts


def _pv(tmp_path):
    log = DecisionLog(tmp_path / "dec.jsonl", Bus())
    return PvWatts(FakeConfig(), log, tmp_path / "pvwatts_cache.json")


def test_hour_of_year_bounds():
    assert PvWatts._hour_of_year(datetime(2026, 1, 1, 0)) == 0
    assert PvWatts._hour_of_year(datetime(2026, 1, 2, 5)) == 29       # (2-1)*24 + 5
    assert PvWatts._hour_of_year(datetime(2026, 12, 31, 23)) == 8759


def test_expected_w_reads_modeled_hour(tmp_path):
    pv = _pv(tmp_path)
    dc = [0.0] * 8760
    dc[29] = 870.0
    pv.modeled = {"A": {"dc": dc}}
    assert pv.expected_w("A", datetime(2026, 1, 2, 5)) == 870.0


def test_expected_w_none_when_no_data(tmp_path):
    pv = _pv(tmp_path)
    assert pv.expected_w("A") is None                # unit not modeled
    pv.modeled = {"A": {"dc": []}}
    assert pv.expected_w("A") is None                # empty series


def test_snapshot_shape(tmp_path):
    pv = _pv(tmp_path)
    pv.modeled = {"A": {"dc": [100.0] * 8760, "ac_annual": 2100.0, "fetched": 1.0}}
    snap = pv.snapshot()
    assert snap["A"]["ac_annual_kwh"] == 2100.0
    assert snap["A"]["expected_w"] == 100.0
