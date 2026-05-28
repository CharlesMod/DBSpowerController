"""Charge-balancing sub-state-machine: enter/exit, persistence, hysteresis.

`_evaluate_balance(group_id, gunits, now)` takes the clock explicitly, so these
tests drive time deterministically without sleeping. All units here live in the
default bus group "a"; balancing is intra-group only.
"""

from helpers import FakeConfig, feed, make_coordinator
from cube_power.types import BalanceState


def _classify(units, cfg):
    for u in units.values():
        u.classify(cfg)


def _setup(cfg, soc_a, soc_b):
    coord, units = make_coordinator(cfg)
    feed(units["A"], soc=soc_a, solar=1400, ac_out=400)
    feed(units["B"], soc=soc_b, solar=1400, ac_out=400)
    _classify(units, cfg)
    return coord, units


def _eval(coord, units, now):
    coord._evaluate_balance("a", dict(units), now)


def test_enter_rebalancing_on_divergence():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 75, 55)            # delta 20 >= 15
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    assert coord.weak_unit_id == "B"


def test_no_enter_when_within_threshold():
    cfg = FakeConfig(divergence_persist_min=0)
    coord, units = _setup(cfg, 70, 62)            # delta 8 < 15
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.BALANCED


def test_divergence_must_persist():
    cfg = FakeConfig(divergence_persist_min=10, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 75, 55)
    _eval(coord, units, 1000.0)                   # diverged_since starts now
    assert coord.balance_state == BalanceState.BALANCED
    _eval(coord, units, 1100.0)                   # +100 s, not yet 600 s
    assert coord.balance_state == BalanceState.BALANCED
    _eval(coord, units, 1700.0)                   # +700 s -> persisted
    assert coord.balance_state == BalanceState.REBALANCING


def test_transient_divergence_resets_persist():
    cfg = FakeConfig(divergence_persist_min=10, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 75, 55)
    _eval(coord, units, 1000.0)
    feed(units["B"], soc=70, solar=1400, ac_out=400)   # converged again
    _classify(units, cfg)
    _eval(coord, units, 1100.0)                   # clears diverged_since
    assert coord._diverged_since["a"] is None
    assert coord.balance_state == BalanceState.BALANCED


def test_small_delta_near_floor_does_not_trigger():
    # Floor-proximity preemptive trigger removed (caused morning relay churn):
    # while a unit dips near the floor, divergence alone decides. The 33% floor
    # + 40% rehab band in unit.classify() protects from overdischarge.
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 40, 37)            # delta 3, weak near floor
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.BALANCED


def test_weak_unit_ac_commanded_off_during_rebalance():
    import asyncio
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, _ = _setup(cfg, 75, 55)
    asyncio.run(coord.tick())
    assert coord.balance_state == BalanceState.REBALANCING
    desired = coord.snapshot.desired_ac
    assert desired["B"] is False              # weak unit dropped from the bus
    assert desired["A"] is True


def test_exit_on_convergence():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0,
                     divergence_clear_persist_min=0)
    coord, units = _setup(cfg, 75, 55)
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    feed(units["B"], soc=72, solar=1400, ac_out=400)   # delta 3 <= 5
    _classify(units, cfg)
    _eval(coord, units, 1100.0)
    assert coord.balance_state == BalanceState.BALANCED
    assert coord.weak_unit_id is None


def test_min_dwell_blocks_immediate_exit():
    t0 = 2_000_000_000.0
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=20,
                     divergence_clear_persist_min=0)
    coord, units = _setup(cfg, 75, 55)
    _eval(coord, units, t0)                       # enter (dwell starts)
    assert coord.balance_state == BalanceState.REBALANCING
    feed(units["B"], soc=74, solar=1400, ac_out=400)   # converged
    _classify(units, cfg)
    _eval(coord, units, t0 + 100)                 # +100 s, dwell is 1200 s
    assert coord.balance_state == BalanceState.REBALANCING
    _eval(coord, units, t0 + 1300)                # +1300 s -> dwell satisfied
    assert coord.balance_state == BalanceState.BALANCED


def test_exit_on_max_duration():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0,
                     rebalance_max_duration_min=90)
    coord, units = _setup(cfg, 75, 55)            # still diverged the whole time
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    _eval(coord, units, 1000.0 + 91 * 60)
    assert coord.balance_state == BalanceState.BALANCED


def test_no_rebalance_when_strong_below_threshold():
    # Real divergence, but the strong unit is below the rebalance band — there's
    # no 100%-clipping risk to defend against, so leave both units running.
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 50, 30)            # delta 20, strong=50 < 60
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.BALANCED


def test_rebalance_abandons_when_strong_drops_below_threshold():
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=0)
    coord, units = _setup(cfg, 65, 45)
    _eval(coord, units, 1000.0)
    assert coord.balance_state == BalanceState.REBALANCING
    feed(units["A"], soc=58, solar=1400, ac_out=400)    # strong fell below 60
    _classify(units, cfg)
    _eval(coord, units, 1100.0)
    assert coord.balance_state == BalanceState.BALANCED


def test_crossover_exits_rebalance_immediately():
    # Enter with B as weak, swap SoCs so A is now weak — rebalance exits and
    # bypasses min_dwell.
    t0 = 2_000_000_000.0
    cfg = FakeConfig(divergence_persist_min=0, rebalance_min_dwell_min=20,
                     divergence_clear_persist_min=0)
    coord, units = _setup(cfg, 75, 55)         # A=75, B=55 -> weak=B
    _eval(coord, units, t0)
    assert coord.balance_state == BalanceState.REBALANCING
    assert coord.weak_unit_id == "B"
    feed(units["A"], soc=50, solar=1400, ac_out=400)
    feed(units["B"], soc=80, solar=1400, ac_out=400)
    _classify(units, cfg)
    _eval(coord, units, t0 + 60)               # well inside the 20-min dwell
    assert coord.balance_state == BalanceState.BALANCED
    assert coord.weak_unit_id is None


def test_no_balancing_with_single_unit():
    cfg = FakeConfig(divergence_persist_min=0)
    coord, units = make_coordinator(cfg, n_units=1)
    feed(units["A"], soc=75, solar=1400, ac_out=400)
    units["A"].classify(cfg)
    coord._evaluate_balance("a", {"A": units["A"]}, 1000.0)
    assert coord.balance_state == BalanceState.BALANCED
