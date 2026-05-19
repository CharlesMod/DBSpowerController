"""Core AC on/off decision logic for the fixed-sink model — 1-car and 2-car.

Assertions target `coordinator.snapshot.desired_ac` (what the coordinator wants),
since the actuator runs in dry mode here and never flips the fed `ac_on` state.
"""

import asyncio

from helpers import FakeConfig, feed, make_coordinator


def run_tick(coord):
    asyncio.run(coord.tick())
    return coord.snapshot


# ── one car ──────────────────────────────────────────────────────────

def test_one_car_both_healthy_runs_both():
    coord, units = make_coordinator(FakeConfig())          # _n_cars defaults to 1
    feed(units["A"], soc=60, solar=1400)
    feed(units["B"], soc=70, solar=1400)
    s = run_tick(coord)
    assert s.desired_ac == {"A": True, "B": True}
    assert s.units_on == 2


def test_one_car_one_floored_runs_the_healthy_one():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31, solar=1400)                   # floored
    feed(units["B"], soc=70, solar=1400)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": True}


def test_one_car_both_floored_all_off():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31)
    feed(units["B"], soc=30)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": False}
    assert s.units_on == 0


# ── two cars ─────────────────────────────────────────────────────────

def test_two_cars_both_healthy_runs_both():
    coord, units = make_coordinator(FakeConfig())
    coord._n_cars = 2
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.desired_ac == {"A": True, "B": True}


def test_two_cars_one_floored_holds_all_off():
    # two cars need 2400 W — one 1200 W unit can't serve them; don't overload
    coord, units = make_coordinator(FakeConfig())
    coord._n_cars = 2
    feed(units["A"], soc=31)                               # floored
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": False}
    assert "both units" in s.note


# ── car-count inference ──────────────────────────────────────────────

def test_infers_two_cars_from_bus_output():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=60, ac_out=1200, ac_on=True)
    feed(units["B"], soc=70, ac_out=1200, ac_on=True)      # total 2400 -> 2 cars
    s = run_tick(coord)
    assert s.n_cars == 2
    assert s.n_cars_measured is True


def test_infers_one_car_from_bus_output():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=60, ac_out=1180, ac_on=True)
    feed(units["B"], soc=70, ac_out=0, ac_on=False)        # total ~1200 -> 1 car
    s = run_tick(coord)
    assert s.n_cars == 1


def test_zero_draw_does_not_drop_known_count():
    coord, units = make_coordinator(FakeConfig())
    coord._n_cars = 2
    feed(units["A"], soc=60, ac_out=0, ac_on=True)         # on but no draw
    feed(units["B"], soc=70, ac_out=0, ac_on=True)
    run_tick(coord)
    assert coord._n_cars == 2                              # not clobbered to 0


# ── roles ────────────────────────────────────────────────────────────

def test_offline_unit_omitted_from_desired():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=60, online=False)                 # OFFLINE
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert "A" not in s.desired_ac
    assert s.desired_ac.get("B") is True


def test_override_on_beats_floor():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31)                               # would be floored
    feed(units["B"], soc=70)
    units["A"].request_override(on=True, ttl_h=2, cfg=coord.cfg)
    s = run_tick(coord)
    assert s.desired_ac["A"] is True


def test_override_off_holds_unit_down():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=80)
    feed(units["B"], soc=70)
    units["A"].request_override(on=False, ttl_h=2, cfg=coord.cfg)
    s = run_tick(coord)
    assert s.desired_ac["A"] is False
    assert s.desired_ac["B"] is True


def test_actuator_ready_flag():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.actuator_ready is True                        # dps map has ac_on
