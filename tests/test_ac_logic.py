"""AC on/off decisions per bus-group.

In the group model each group is one car. The 2-cars-on-one-bus scenarios of
the old fixed-sink model don't exist any more; that case is now two groups,
each with one car.
"""

import asyncio

from helpers import FakeConfig, FakeTesla, attach_tesla, feed, make_coordinator


def run_tick(coord):
    asyncio.run(coord.tick())
    return coord.snapshot


# ── single group, two units ──────────────────────────────────────────

def test_no_tesla_both_healthy_runs_both():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=60, solar=1400)
    feed(units["B"], soc=70, solar=1400)
    s = run_tick(coord)
    # No tesla configured for the group -> default-on (back-compat)
    assert s.desired_ac == {"A": True, "B": True}
    assert s.units_on == 2


def test_no_tesla_one_floored_runs_the_healthy_one():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31, solar=1400)                   # floored
    feed(units["B"], soc=70, solar=1400)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": True}


def test_no_tesla_both_floored_all_off():
    coord, units = make_coordinator(FakeConfig())
    feed(units["A"], soc=31)
    feed(units["B"], soc=30)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": False}
    assert s.units_on == 0


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
    assert s.actuator_ready is True


# ── Tesla-first: plug state drives bus-live policy ───────────────────

def test_inverters_off_when_no_car_plugged_and_idle_window_expired():
    # car configured for group but not plugged in; idle-shutoff window is 0
    # so the bus is left off (saves standby loss).
    cfg = FakeConfig(inverter_idle_shutoff_min=0)
    coord, units = make_coordinator(cfg)
    attach_tesla(coord, cfg, FakeTesla(plugged_in=False))
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": False}
    assert s.groups["a"].want_bus_live is False


def test_inverters_on_when_car_plugged():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    attach_tesla(coord, cfg, FakeTesla(plugged_in=True))
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.desired_ac == {"A": True, "B": True}
    assert s.groups["a"].plugged_in is True


def test_plug_in_edge_starts_charging():
    cfg = FakeConfig(inverter_idle_shutoff_min=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=False)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)                                   # tick 1: not plugged, off
    assert fake.start_count == 0
    fake.cars["V1"].plugged_in = True                 # cable goes in
    s = run_tick(coord)                               # tick 2: plug edge
    assert s.desired_ac == {"A": True, "B": True}
    assert fake.start_count >= 1
    assert "plug-in edge" in s.groups["a"].note


def test_plug_in_edge_bypasses_min_dwell():
    # min_ac_dwell_s is normally 300 s; a freshly-off unit normally can't be
    # re-energized for 5 min. The plug-edge should override that.
    import time as _time
    cfg = FakeConfig(min_ac_dwell_s=300, inverter_idle_shutoff_min=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=False)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)                                   # both go OFF
    # backdate the off timestamps so the dwell guard would normally apply
    coord._ac_off_at = {"A": _time.time() - 10, "B": _time.time() - 10}
    fake.cars["V1"].plugged_in = True
    s = run_tick(coord)
    assert s.desired_ac == {"A": True, "B": True}    # dwell bypassed


# ── multi-group ──────────────────────────────────────────────────────

def test_bus_held_off_when_car_charges_from_external_240v_source():
    # Car BLE-reports plugged + charging at 241 V — that's the wall charger,
    # not our 120 V DBS bus. Leave inverters off.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging = True
    fake.cars["V1"].charger_voltage = 241
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": False}
    assert s.groups["a"].want_bus_live is False
    assert "external source" in s.groups["a"].note


def test_does_not_preempt_unowned_charging_session():
    # car is plugged in and already charging (e.g., from grid). Our DBS units
    # are floored, so we have no power to offer. We must NOT stop the car —
    # we never started this session.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=31)                          # floored
    feed(units["B"], soc=30)                          # floored
    run_tick(coord)
    assert fake.stop_count == 0


def test_two_groups_independent():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg, n_units=2, unit_groups=["a", "b"])
    cfg.data["tesla_vins"] = [
        {"vin": "VA", "bus_group": "a"},
        {"vin": "VB", "bus_group": "b"},
    ]
    fake = FakeTesla(vins=("VA", "VB"))
    fake.cars["VA"].plugged_in = True
    fake.cars["VB"].plugged_in = False
    coord.tesla = fake
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = run_tick(coord)
    # group a: plugged -> on; group b: not plugged -> off (idle window starts at 0)
    assert s.desired_ac == {"A": True, "B": False}
    assert s.groups["a"].plugged_in is True
    assert s.groups["b"].plugged_in is False
