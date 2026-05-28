"""Per-unit role classification and manual override."""

import time

from helpers import FakeConfig, feed, make_unit
from cube_power.types import UnitRole


def test_classify_normal():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=60, solar=800)
    assert u.classify(cfg) == UnitRole.NORMAL


def test_classify_offline_when_stale():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=60, solar=800, age=120)        # older than state_stale_s (60)
    assert u.classify(cfg) == UnitRole.OFFLINE


def test_classify_offline_when_not_online():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=60, online=False)
    assert u.classify(cfg) == UnitRole.OFFLINE


def test_classify_floored():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=31, solar=800)
    assert u.classify(cfg) == UnitRole.FLOORED


def test_override_beats_floor():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=31, solar=800)
    u.request_override(on=True, ttl_h=4, cfg=cfg)
    assert u.classify(cfg) == UnitRole.OVERRIDE


def test_override_expiry():
    cfg = FakeConfig()
    u = make_unit("A")
    feed(u, soc=60)
    u.request_override(on=False, ttl_h=4, cfg=cfg)
    assert u.override_active()
    u.override_expires_at = time.time() - 1     # force expiry
    assert not u.override_active()
    assert u.classify(cfg) == UnitRole.NORMAL
