"""SoC floor: classification thresholds and the rehab hysteresis latch."""

import asyncio

from helpers import FakeConfig, feed, make_coordinator, make_unit


def test_floored_and_hard_floored_thresholds():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=33.0)
    assert u.is_floored(cfg) and not u.is_hard_floored(cfg)
    feed(u, soc=30.0)
    assert u.is_floored(cfg) and u.is_hard_floored(cfg)
    feed(u, soc=40.0)
    assert not u.is_floored(cfg) and not u.is_hard_floored(cfg)


def test_at_rehab_threshold():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=39.0)
    assert not u.at_rehab(cfg)          # below floor(33) + rehab band(7) = 40
    feed(u, soc=40.0)
    assert u.at_rehab(cfg)


def test_rehab_hysteresis_holds_unit_off_until_recovered():
    # A floors, then recovers to 36% (role NORMAL again) — but must stay off
    # until it crosses the 40% rehab line.
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31)            # floored
    feed(units["B"], soc=70)
    asyncio.run(coord.tick())
    assert coord._held_off["A"] is True
    assert coord.snapshot.desired_ac["A"] is False

    feed(units["A"], soc=36)            # recovered above floor, below rehab
    asyncio.run(coord.tick())
    assert coord._held_off["A"] is True
    assert coord.snapshot.desired_ac["A"] is False   # still held off

    feed(units["A"], soc=41)            # crossed the rehab line
    asyncio.run(coord.tick())
    assert coord._held_off["A"] is False
    assert coord.snapshot.desired_ac["A"] is True


def test_hard_floor_latches_held_off():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=29)            # hard-floored
    feed(units["B"], soc=70)
    asyncio.run(coord.tick())
    assert coord._held_off["A"] is True
