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


# ── wall keep-alive ──────────────────────────────────────────────────

def _run_tick_drain(coord):
    """Run one tick and await any fire-and-forget tasks it spawned
    (the wall-kick / wake campaigns use asyncio.create_task)."""
    async def go():
        await coord.tick()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
    asyncio.run(go())
    return coord.snapshot


def test_wall_keepalive_wakes_sleeping_wall_car():
    # Car plugged into a live 240 V wall but not charging (dozed off). We don't
    # control wall power, but it should be charging — wake + start it.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 241
    fake.cars["V1"].charging = False
    fake.cars["V1"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    s = _run_tick_drain(coord)
    assert fake.wake_count >= 1
    assert fake.start_count >= 1
    assert coord._car_on_wall["V1"] is True
    assert s.groups["a"].want_bus_live is False          # DBS bus stays off


def test_wall_keepalive_skips_car_already_charging():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 241
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run_tick_drain(coord)
    assert fake.start_count == 0                          # already drawing


def test_wall_keepalive_skips_full_car():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 241
    fake.cars["V1"].charging = False
    fake.cars["V1"].charging_state = "Complete"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run_tick_drain(coord)
    assert fake.start_count == 0                          # battery full


def test_wall_flag_sticky_across_voltage_dip():
    # Once seen on the wall, a car stays classified external even when its
    # charge-port voltage dips as it opens its contactor — so we keep it alive
    # and never energize the DBS bus for it.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 241
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)                                       # sees the wall
    fake.cars["V1"].charging = False                      # stops on its own
    fake.cars["V1"].charger_voltage = 2                   # contactor opens
    s = _run_tick_drain(coord)
    assert coord._car_on_wall["V1"] is True
    assert s.groups["a"].want_bus_live is False
    assert fake.start_count >= 1


def test_wall_flag_clears_on_unplug():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 241
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)
    assert coord._car_on_wall["V1"] is True
    fake.cars["V1"].plugged_in = False
    run_tick(coord)
    assert coord._car_on_wall["V1"] is False


def test_dbs_bus_car_not_flagged_wall():
    # ~120 V from our own inverters must never read as an external wall source.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 118
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)
    assert coord._car_on_wall.get("V1") is not True
    assert coord.snapshot.groups["a"].want_bus_live is True


def test_wall_probe_wakes_sleeping_car_at_2v():
    # Asleep on the wall: BLE can't poll it, so voltage reads the stale ~2 V
    # proximity value — we can't *see* the 240 V. Probe blind: wake + start.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 2
    fake.cars["V1"].charging = False
    fake.cars["V1"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=30)        # floored -> DBS bus stays dark
    feed(units["B"], soc=30)
    _run_tick_drain(coord)
    assert fake.wake_count >= 1
    assert fake.start_count >= 1
    assert coord._car_on_wall.get("V1") is not True   # never confirmed by voltage


def test_wall_probe_caps_on_dead_bus():
    # If start never makes it charge (dead/off bus), stop after the cap.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 2
    fake.cars["V1"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=30)
    feed(units["B"], soc=30)
    cap = int(cfg.getf("wall_probe_max_attempts", 3))
    for _ in range(cap + 3):
        fake.cars["V1"].charging = False       # dead bus: it never actually starts
        coord._last_wall_kick["V1"] = 0.0      # defeat the per-VIN throttle
        _run_tick_drain(coord)
    assert fake.start_count == cap             # exactly cap probes, then dormant


def test_wall_probe_resets_when_dbs_bus_energizes():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 2
    fake.cars["V1"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=30)
    feed(units["B"], soc=30)
    cap = int(cfg.getf("wall_probe_max_attempts", 3))
    for _ in range(cap + 2):                   # exhaust probes on the dead bus
        fake.cars["V1"].charging = False
        coord._last_wall_kick["V1"] = 0.0
        _run_tick_drain(coord)
    assert coord._wall_probe_attempts.get("V1", 0) >= cap
    # bus heals and energizes -> probe budget resets
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    fake.cars["V1"].charging = False
    coord._last_wall_kick["V1"] = 0.0
    _run_tick_drain(coord)
    assert coord.snapshot.groups["a"].units_on > 0      # bus is live
    assert coord._wall_probe_attempts.get("V1", 0) <= 1  # reset, then re-probed


def test_confirmed_wall_kick_sets_amps():
    # A confirmed-wall car stuck at 0 A must get its amperage pushed, not just
    # "start" — otherwise it acks and draws nothing.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 244       # confirmed wall
    fake.cars["V1"].charging = False
    fake.cars["V1"].charging_state = "Stopped"
    fake.cars["V1"].set_amps = 0
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run_tick_drain(coord)
    assert fake.set_amps_count >= 1              # amperage pushed
    assert fake.start_count >= 1


def test_unconfirmed_probe_does_not_set_amps():
    # Blind probe (could be a DBS-bus car) must never touch amps — the inverter,
    # not a pilot signal, is the limit there.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 2          # asleep / unconfirmed
    fake.cars["V1"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=30)                      # bus dark
    feed(units["B"], soc=30)
    _run_tick_drain(coord)
    assert fake.set_amps_count == 0


def test_wall_flag_persists_across_restart():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 244
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)
    assert coord._car_on_wall.get("V1") is True
    # New coordinator sharing the same wall_state_path == a service restart.
    coord2, units2 = make_coordinator(cfg)
    assert coord2._car_on_wall.get("V1") is True   # confirmed survived restart


def test_wall_flag_self_corrects_to_dbs():
    # Car physically on the DBS bus, charging at ~118 V, must clear any stale
    # wall flag so we don't push wall amps onto the inverter.
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    coord._car_on_wall["V1"] = True                # stale (was on wall before)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charger_voltage = 118
    fake.cars["V1"].charging = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    run_tick(coord)
    assert coord._car_on_wall.get("V1") is False


# ── stuck-bus watchdog ───────────────────────────────────────────────

def _stuck_setup(cfg):
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging_state = "Stopped"
    fake.cars["V1"].actual_amps = 0
    fake.cars["V1"].charger_voltage = 2
    attach_tesla(coord, cfg, fake)
    # inverters ON, batteries healthy, solar good, but ZERO ac-out (no draw)
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=0)
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=0)
    return coord, units, fake


def test_stuck_bus_detected_when_bus_live_but_no_draw():
    coord, units, fake = _stuck_setup(FakeConfig())
    run_tick(coord)
    assert coord._bus_stuck_since["a"] is not None


def test_stuck_bus_not_flagged_when_drawing():
    # dp158 AC-out is the only reliable "power flowing" signal — the cars' BLE
    # port readings are NOT trustworthy (read ~2V/0A even while charging), so we
    # must NOT cycle while the inverters show real output, or we'd interrupt a
    # car that's actually charging.
    coord, units, fake = _stuck_setup(FakeConfig())
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=1400)  # real draw
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=1400)
    run_tick(coord)
    assert coord._bus_stuck_since["a"] is None


def test_stuck_bus_does_not_cycle_a_charging_car_with_bad_ble():
    # Regression: cars charging (~2.3kW on dp158) but BLE wrongly reports 2V/0A.
    # The watchdog must trust dp158 and NOT mark stuck (cycling would interrupt
    # the live charge).
    coord, units, fake = _stuck_setup(FakeConfig())
    fake.cars["V1"].charger_voltage = 2          # BLE lies: looks idle...
    fake.cars["V1"].actual_amps = 0
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=1150)  # ...but really drawing
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=1200)
    run_tick(coord)
    assert coord._bus_stuck_since["a"] is None   # protected by dp158


def test_stuck_bus_clears_when_car_starts_drawing():
    coord, units, fake = _stuck_setup(FakeConfig())
    run_tick(coord)
    assert coord._bus_stuck_since["a"] is not None
    fake.cars["V1"].actual_amps = 15           # car begins drawing
    _run_tick_drain(coord)
    assert coord._bus_stuck_since["a"] is None
    assert coord._bus_cycle_count["a"] == 0


def test_stuck_bus_carside_kick_before_relay_cycle():
    coord, units, fake = _stuck_setup(FakeConfig())
    run_tick(coord)
    coord._bus_stuck_since["a"] -= 130          # just past detect, before grace
    _run_tick_drain(coord)
    assert coord._bus_carside_done["a"] is True
    assert coord._bus_cycle_count["a"] == 0      # no relay cycle yet


def test_stuck_bus_escalates_to_ac_cycle():
    coord, units, fake = _stuck_setup(FakeConfig(stuck_bus_cycle_dwell_s=0))
    run_tick(coord)
    coord._bus_carside_done["a"] = True          # car-side already tried
    coord._bus_stuck_since["a"] -= 200           # past detect + grace
    _run_tick_drain(coord)
    assert coord._bus_cycle_count["a"] == 1
    assert coord._bus_cycling == set()           # cleaned up after the cycle


def test_stuck_bus_caps_cycles():
    cfg = FakeConfig(stuck_bus_max_cycles=2, stuck_bus_cycle_throttle_s=0,
                     stuck_bus_cycle_dwell_s=0)
    coord, units, fake = _stuck_setup(cfg)
    coord._bus_carside_done["a"] = True
    for _ in range(5):
        run_tick(coord)
        coord._bus_stuck_since["a"] -= 200
        _run_tick_drain(coord)
    assert coord._bus_cycle_count["a"] == 2       # capped, not unbounded


def test_stuck_bus_kick_sets_dbs_amp_floor():
    # A stuck DBS car pinned at 0 A must get a conservative amp floor (not the
    # 32 A wall value) so it actually draws.
    cfg = FakeConfig(stuck_bus_cycle_dwell_s=0)
    coord, units, fake = _stuck_setup(cfg)
    fake.cars["V1"].set_amps = 0
    run_tick(coord)
    coord._bus_carside_done["a"] = True
    coord._bus_stuck_since["a"] -= 200
    _run_tick_drain(coord)
    floor = int(cfg.getf("dbs_charge_amps", 16))
    assert fake.cars["V1"].set_amps == floor       # bumped to DBS floor, not 32
    assert fake.start_count >= 1


def test_kick_dbs_cars_skips_amps_for_wall_car():
    # _kick_dbs_cars must not override a wall car's (pilot-clamped) amperage
    # with the DBS floor — the wall keep-alive owns that car's amps.
    cfg = FakeConfig()
    coord, units, fake = _stuck_setup(cfg)
    coord._car_on_wall["V1"] = True
    fake.cars["V1"].set_amps = 32
    asyncio.run(coord._kick_dbs_cars(["V1"], "a"))
    assert fake.cars["V1"].set_amps == 32          # untouched
    assert fake.start_count >= 1                    # still nudged to start


def test_stuck_bus_parks_after_cap_and_does_not_rearm():
    # After the cycle cap the car is wedged (red-ring latch) — only a physical
    # replug clears it, and more AC cycles deepen the latch. So we PARK: hold the
    # bus live + raise the replug alert and STOP cycling. The budget never
    # re-arms on a timer; it refreshes only when the car genuinely recovers.
    cfg = FakeConfig(stuck_bus_max_cycles=2, stuck_bus_cycle_throttle_s=0,
                     stuck_bus_cycle_dwell_s=0, stuck_bus_giveup_cooldown_s=600)
    coord, units, fake = _stuck_setup(cfg)
    coord._bus_carside_done["a"] = True
    for _ in range(4):                      # drive past the cap
        run_tick(coord)
        coord._bus_stuck_since["a"] -= 200
        _run_tick_drain(coord)
    assert coord._bus_cycle_count["a"] == 2
    assert coord._bus_giveup_logged["a"] is True
    assert coord._bus_replug_alert["a"] is not None       # user told to replug
    # cooldown elapses -> STILL parked, NO re-arm (the old harmful behavior)
    coord._bus_giveup_at["a"] -= 700
    coord._bus_stuck_since["a"] -= 200
    _run_tick_drain(coord)
    assert coord._bus_cycle_count["a"] == 2
    assert coord._bus_giveup_logged["a"] is True
    # genuine recovery (the car finally draws) resets the budget + clears alert
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=600)
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=600)
    _run_tick_drain(coord)
    assert coord._bus_cycle_count["a"] == 0
    assert coord._bus_replug_alert["a"] is None


def test_two_candidates_force_both_inverters_on():
    # Overload safety: two cars on the shared bus can't run on one ~1400 W
    # inverter, so a two-candidate load must keep BOTH inverters on even when the
    # SoC gap would otherwise trigger a rebalance duty-cycle of the weak unit.
    cfg = FakeConfig(divergence_trigger_pct=15, divergence_persist_min=0,
                     rebalance_min_strong_soc_pct=60, rebalance_min_dwell_min=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(vins=("V1", "V2"), plugged_in=True)
    fake.cars["V1"].charging_state = "Stopped"
    fake.cars["V2"].charging_state = "Stopped"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=95, solar=600, ac_on=True)   # big gap -> would rebalance
    feed(units["B"], soc=60, solar=600, ac_on=True)
    s = run_tick(coord)
    assert s.groups["a"].balance_state == "REBALANCING"   # rebalance DID engage
    assert s.desired_ac == {"A": True, "B": True}         # but both stay on


def test_overload_cap_clears_one_car_when_a_leg_is_floored():
    # If a unit drops for SoC-floor safety while two cars want charge, only the
    # most-depleted car is cleared to charge (a lone inverter can't carry two).
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(vins=("V1", "V2"), plugged_in=True)
    fake.cars["V1"].charging_state = "Stopped"; fake.cars["V1"].car_soc_pct = 30
    fake.cars["V2"].charging_state = "Stopped"; fake.cars["V2"].car_soc_pct = 70
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=31, solar=600, ac_on=True)   # at floor -> dropped
    feed(units["B"], soc=80, solar=600, ac_on=True)
    s = run_tick(coord)
    assert s.desired_ac == {"A": False, "B": True}        # one leg down for safety
    assert s.groups["a"].charge_allowed_vins == ["V1"]    # only the depleted car


# ── keep-cars-alive (prevent the sleep->unreachable->0A trap) ─────────

def _keepalive_setup(cfg, reachable=True):
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    car = fake.cars["V1"]
    car.charging_state = "Stopped"
    car.actual_amps = 0
    car.reachable = reachable
    car.data_fresh = reachable
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=0)   # live bus
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=0)
    return coord, units, fake


def test_keepalive_kicks_reachable_idle_car():
    coord, units, fake = _keepalive_setup(FakeConfig())
    _run_tick_drain(coord)
    assert "V1" in coord._last_carside          # keep-alive's exclusive marker
    assert fake.cars["V1"].set_amps == int(coord.cfg.getf("dbs_charge_amps", 16))


def test_keepalive_skips_unreachable_car():
    # Link down -> waking is futile; reconnect/AC-cycle own recovery.
    coord, units, fake = _keepalive_setup(FakeConfig(), reachable=False)
    _run_tick_drain(coord)
    assert "V1" not in coord._last_carside


def test_keepalive_skips_drawing_car_at_target():
    # Car drawing at or above the floor — no action needed.
    coord, units, fake = _keepalive_setup(FakeConfig())
    floor = int(coord.cfg.getf("dbs_charge_amps", 16))
    fake.cars["V1"].actual_amps = floor
    fake.cars["V1"].set_amps = floor
    _run_tick_drain(coord)
    assert "V1" not in coord._last_carside


def test_keepalive_corrects_amps_while_drawing():
    # Car drawing but set_amps below target — push correction.
    coord, units, fake = _keepalive_setup(FakeConfig())
    floor = int(coord.cfg.getf("dbs_charge_amps", 16))
    fake.cars["V1"].actual_amps = 10
    fake.cars["V1"].set_amps = 10          # below 16 A floor
    _run_tick_drain(coord)
    assert fake.cars["V1"].set_amps == floor
    assert "V1" in coord._last_carside     # correction path stamped the timer


def test_keepalive_throttled():
    coord, units, fake = _keepalive_setup(FakeConfig())
    _run_tick_drain(coord)
    assert "V1" in coord._last_carside
    ts = coord._last_carside["V1"]
    _run_tick_drain(coord)                   # immediate second tick
    assert coord._last_carside["V1"] == ts   # throttled, not re-kicked


def test_keepalive_skips_when_bus_off():
    cfg = FakeConfig()
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging_state = "Stopped"
    fake.cars["V1"].reachable = True
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=30, solar=0, ac_on=False, ac_out=0)    # floored, bus dark
    feed(units["B"], soc=30, solar=0, ac_on=False, ac_out=0)
    _run_tick_drain(coord)
    assert "V1" not in coord._last_carside


def test_stuck_bus_raises_replug_alert_at_cap():
    # After the cycle cap, a hard-stuck bus must raise a clear replug alert on
    # the snapshot (cycling won't fix a wedged handshake — only a replug does).
    cfg = FakeConfig(stuck_bus_max_cycles=2, stuck_bus_cycle_throttle_s=0,
                     stuck_bus_cycle_dwell_s=0, stuck_bus_giveup_cooldown_s=9999)
    coord, units, fake = _stuck_setup(cfg)
    coord._bus_carside_done["a"] = True
    for _ in range(5):
        run_tick(coord)
        coord._bus_stuck_since["a"] -= 300
        _run_tick_drain(coord)
    assert coord._bus_replug_alert["a"]                       # alert raised
    assert "Replug" in coord._bus_replug_alert["a"]
    assert coord.snapshot.groups["a"].alert is not None       # on the snapshot


def test_replug_alert_clears_when_draw_returns():
    cfg = FakeConfig(stuck_bus_max_cycles=2, stuck_bus_cycle_throttle_s=0,
                     stuck_bus_cycle_dwell_s=0, stuck_bus_giveup_cooldown_s=9999)
    coord, units, fake = _stuck_setup(cfg)
    coord._bus_carside_done["a"] = True
    for _ in range(5):
        run_tick(coord)
        coord._bus_stuck_since["a"] -= 300
        _run_tick_drain(coord)
    assert coord._bus_replug_alert["a"]
    # a car starts drawing (dp158 shows real output)
    feed(units["A"], soc=60, solar=600, ac_on=True, ac_out=1200)
    feed(units["B"], soc=70, solar=600, ac_on=True, ac_out=1200)
    run_tick(coord)
    assert coord._bus_replug_alert["a"] is None              # cleared
    assert coord.snapshot.groups["a"].alert is None


# ── no-solar idle shutdown ───────────────────────────────────────────

def _dark_setup(cfg, ac_out=0, charging_state="Complete"):
    # Default car is "Complete" (done charging): the canonical standby-shutdown
    # case. A car that still wants charge is now served even when dark — see
    # test_no_solar_keeps_serving_car_that_wants_charge.
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging_state = charging_state
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60, solar=0, ac_on=True, ac_out=ac_out)   # sun down
    feed(units["B"], soc=70, solar=0, ac_on=True, ac_out=ac_out)
    return coord, units


def test_no_solar_keeps_serving_car_that_wants_charge():
    # Sun genuinely down, pack has headroom, car plugged but not drawing (the
    # bus was off so it can't). It still wants charge -> keep the bus live and
    # give it the chance to draw; don't standby-shutdown a needy car.
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50)
    coord, units = _dark_setup(cfg, charging_state="Stopped")
    run_tick(coord)
    coord._solar_dark_since -= 400                    # 6+ min of dark
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is True


def test_full_battery_zero_harvest_is_not_dark():
    # A 100%-SoC pack pulls ~0 W from the panels even in bright sun, so a low
    # solar-in reading must NOT be read as darkness. Car is "Complete" here to
    # isolate the darkness fix from the wants-charge fix.
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50,
                     solar_full_soc_pct=99)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    fake.cars["V1"].charging_state = "Complete"
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=100, solar=0, ac_on=True)
    feed(units["B"], soc=100, solar=0, ac_on=True)
    run_tick(coord)
    assert coord._solar_dark_since is None            # full bank != dark
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is True


def test_no_solar_shutdown_after_dark_window():
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50)
    coord, units = _dark_setup(cfg)
    run_tick(coord)                                  # starts the dark timer
    assert coord.snapshot.groups["a"].want_bus_live is True   # not dark long enough
    coord._solar_dark_since -= 400                   # 6+ min of dark
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is False
    assert "no-solar shutdown" in coord.snapshot.groups["a"].note
    assert coord.snapshot.desired_ac == {"A": False, "B": False}


def test_no_solar_shutdown_does_not_interrupt_active_charge():
    # Sun down, but a car is drawing from the battery — require_idle (default)
    # must keep the bus live and not cut a working charge.
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50)
    coord, units = _dark_setup(cfg, ac_out=1400)     # real draw
    run_tick(coord)
    coord._solar_dark_since -= 400
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is True


def test_no_solar_hard_shutdown_when_require_idle_false():
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50,
                     solar_idle_require_idle=False)
    coord, units = _dark_setup(cfg, ac_out=1400)     # drawing, but hard cutoff
    run_tick(coord)
    coord._solar_dark_since -= 400
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is False


def test_solar_present_keeps_bus_live():
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50)
    coord, units = make_coordinator(cfg)
    attach_tesla(coord, cfg, FakeTesla(plugged_in=True))
    feed(units["A"], soc=60, solar=800, ac_on=True)  # sun up
    feed(units["B"], soc=70, solar=800, ac_on=True)
    run_tick(coord)
    assert coord._solar_dark_since is None
    assert coord.snapshot.groups["a"].want_bus_live is True


def test_solar_dark_clears_when_sun_returns():
    cfg = FakeConfig(solar_idle_shutoff_min=5, solar_dark_w=50)
    coord, units = _dark_setup(cfg)
    run_tick(coord)
    coord._solar_dark_since -= 400
    run_tick(coord)
    assert coord.snapshot.groups["a"].want_bus_live is False
    feed(units["A"], soc=60, solar=800, ac_on=True)  # sun returns
    feed(units["B"], soc=70, solar=800, ac_on=True)
    run_tick(coord)
    assert coord._solar_dark_since is None
    assert coord.snapshot.groups["a"].want_bus_live is True
