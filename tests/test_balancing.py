"""Charge-balancing sub-state-machine: enter/exit, persistence, hysteresis.

`_evaluate_balance(now)` takes the clock explicitly, so these tests drive time
deterministically without sleeping.
"""

from helpers import FakeConfig, feed, make_coordinator
from dbs_controller.types import BalanceState


def _classify(units, cfg):
    for u in units.values():
        u.classify(cfg)


def _setup(cfg, soc_a, soc_b):
    coord, units = make_coordinator(cfg)
    feed(units["A"], soc=soc_a, solar=1400, ac_out=400)
    feed(units["B"], soc=soc_b, solar=1400, ac_out=400)
    _classify(units, cfg)
    return coord, units


def test_enter_rebalancing_on_divergence():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, _ = _setup(cfg, 75, 55)            # delta 20 >= 15
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    assert coord.weak_unit_id == "B"


def test_no_enter_when_within_threshold():
    cfg = FakeConfig(divergence_persist_min=0)
    coord, _ = _setup(cfg, 70, 62)            # delta 8 < 15
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.BALANCED


def test_divergence_must_persist():
    cfg = FakeConfig(divergence_persist_min=10, rebalance_min_dwell_min=0)
    coord, _ = _setup(cfg, 75, 55)
    coord._evaluate_balance(1000.0)           # diverged_since starts now
    assert coord.balance_state == BalanceState.BALANCED
    coord._evaluate_balance(1100.0)           # +100 s, not yet 600 s
    assert coord.balance_state == BalanceState.BALANCED
    coord._evaluate_balance(1700.0)           # +700 s -> persisted
    assert coord.balance_state == BalanceState.REBALANCING


def test_transient_divergence_resets_persist():
    cfg = FakeConfig(divergence_persist_min=10, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 75, 55)
    coord._evaluate_balance(1000.0)
    feed(units["B"], soc=70, solar=1400, ac_out=400)   # converged again
    _classify(units, cfg)
    coord._evaluate_balance(1100.0)           # clears diverged_since
    assert coord._diverged_since is None
    assert coord.balance_state == BalanceState.BALANCED


def test_floor_proximity_triggers_even_when_aligned():
    # small delta, but the weak unit sits within floor_proximity of the floor
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, _ = _setup(cfg, 40, 37)            # delta 3, but 37 <= 33 + 5
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    assert coord.weak_unit_id == "B"


def test_weak_unit_ac_commanded_off_during_rebalance():
    import asyncio
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, _ = _setup(cfg, 75, 55)
    asyncio.run(coord.tick())                 # one car (default) -> balancing applies
    assert coord.balance_state == BalanceState.REBALANCING
    desired = coord.snapshot.desired_ac
    assert desired["B"] is False              # weak unit dropped from the bus
    assert desired["A"] is True


def test_exit_on_convergence():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0,
                     divergence_clear_persist_min=0)
    coord, units = _setup(cfg, 75, 55)
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    feed(units["B"], soc=72, solar=1400, ac_out=400)   # delta 3 <= 5
    _classify(units, cfg)
    coord._evaluate_balance(1100.0)
    assert coord.balance_state == BalanceState.BALANCED
    assert coord.weak_unit_id is None


def test_min_dwell_blocks_immediate_exit():
    # uses a real-scale clock: min-dwell compares against _balance_changed_at,
    # which starts at 0.0, so the first transition needs a large `now`.
    t0 = 2_000_000_000.0
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=20,
                     divergence_clear_persist_min=0)
    coord, units = _setup(cfg, 75, 55)
    coord._evaluate_balance(t0)               # enter (dwell starts)
    assert coord.balance_state == BalanceState.REBALANCING
    feed(units["B"], soc=74, solar=1400, ac_out=400)   # converged
    _classify(units, cfg)
    coord._evaluate_balance(t0 + 100)         # +100 s, dwell is 1200 s
    assert coord.balance_state == BalanceState.REBALANCING
    coord._evaluate_balance(t0 + 1300)        # +1300 s -> dwell satisfied
    assert coord.balance_state == BalanceState.BALANCED


def test_exit_on_max_duration():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0,
                     rebalance_max_duration_min=90)
    coord, _ = _setup(cfg, 75, 55)            # still diverged the whole time
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    coord._evaluate_balance(1000.0 + 91 * 60)
    assert coord.balance_state == BalanceState.BALANCED


def test_no_balancing_with_single_unit():
    cfg = FakeConfig(divergence_persist_min=0)
    coord, units = make_coordinator(cfg, n_units=1)
    feed(units["A"], soc=75, solar=1400, ac_out=400)
    units["A"].classify(cfg)
    coord._evaluate_balance(1000.0)
    assert coord.balance_state == BalanceState.BALANCED
