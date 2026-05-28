"""Wake-on-energize campaign — nudging an asleep Tesla when its group's bus
goes live. Now per-group: each bus_group has its own arm/expire/retry state.
"""

import asyncio

from helpers import FakeConfig, FakeTesla, attach_tesla, feed, make_coordinator


def _run(coro_fn):
    async def go():
        await coro_fn()
        await asyncio.sleep(0)        # let the spawned _do_wake task run
    asyncio.run(go())


def test_wake_fires_on_energize_when_no_draw():
    cfg = FakeConfig(tesla_wake_retry_s=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)          # NORMAL, AC off, no draw yet
    feed(units["B"], soc=70)
    _run(coord.tick)
    assert fake.wake_count >= 1
    assert coord._wake_campaign_until["a"] > 0


def test_no_wake_when_a_car_is_already_drawing():
    cfg = FakeConfig(tesla_wake_retry_s=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60, ac_out=1200, ac_on=True)   # car already charging
    feed(units["B"], soc=70, ac_out=0, ac_on=True)
    _run(coord.tick)
    assert fake.wake_count == 0
    assert coord._wake_campaign_until["a"] == 0.0       # campaign ended early


def test_no_wake_when_steady_and_campaign_expired():
    cfg = FakeConfig(tesla_wake_retry_s=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    attach_tesla(coord, cfg, fake)
    import time as _time
    now = _time.time()
    feed(units["A"], soc=60, ac_on=True)
    feed(units["B"], soc=70, ac_on=True)
    coord._ac_commanded = {"A": True, "B": True}
    coord._ac_commanded_at = {"A": now, "B": now}
    coord._bus_was_live["a"] = True              # bus was already live
    coord._plug_state["a"] = True                # car already plugged (no edge)
    coord._wake_campaign_until["a"] = 1.0        # long-expired campaign
    _run(coord.tick)
    assert fake.wake_count == 0


def test_wake_disabled_by_config():
    cfg = FakeConfig(tesla_wake_enabled=False, tesla_wake_retry_s=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run(coord.tick)
    assert fake.wake_count == 0


def test_retry_interval_throttles_repeat_wakes():
    cfg = FakeConfig(tesla_wake_retry_s=150)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=True)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run(coord.tick)                 # first energize -> one wake
    _run(coord.tick)                 # immediately after -> throttled
    assert fake.wake_count == 1


def test_no_wake_when_car_not_plugged():
    # car not plugged -> bus stays off -> no energize -> no wake.
    cfg = FakeConfig(tesla_wake_retry_s=0, inverter_idle_shutoff_min=0)
    coord, units = make_coordinator(cfg)
    fake = FakeTesla(plugged_in=False)
    attach_tesla(coord, cfg, fake)
    feed(units["A"], soc=60)
    feed(units["B"], soc=70)
    _run(coord.tick)
    assert fake.wake_count == 0
