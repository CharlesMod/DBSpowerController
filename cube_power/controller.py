"""The coordinator — system-level brain, per-bus-group, Tesla-first actuation.

Topology: units belong to a `bus_group`. Each group is one electrical bus
feeding one car (one VIN). Today there is one group: "a" with both DBS units
parallel into Tessa. The Anker + the second car will become group "b" later.

Actuation strategy:
  - **Tesla on/off is the primary lever.** When the coordinator wants to halt
    charging it stops the car at the BLE layer (`tesla.stop_charging`) and
    leaves the inverters running at idle. This saves the DBS AC relay from
    cycling every time we'd otherwise pulse the bus.
  - **dp109 (DBS inverter relay) still fires for:**
      a) per-unit floor protection while another unit in the group is healthy,
      b) intra-group rebalance (duty-cycling the weak unit),
      c) idle-shutoff: no car plugged into the group for N minutes -> OFF.
  - **Plug-in edge bypasses min_dwell.** When the car transitions
    unplugged → plugged on a cold bus, the coordinator energizes inverters
    that same tick (skipping the anti-chatter dwell) and immediately issues
    `start_charging` so the car doesn't sit there waiting.

Wake-on-energize: when a group transitions from no-power to power-available
the coordinator runs a bounded wake campaign over BLE — a deeply asleep
Tesla can miss the power-on edge and not start charging on its own.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

from .bus import Bus
from .config import Config
from .decisions import DecisionLog
from .tesla_ble import _norm_state
from .tuya_actuator import set_ac
from .types import BalanceState, CoordinatorSnapshot, GroupSnapshot, UnitRole
from .unit import Unit


class Coordinator:
    def __init__(self, units: dict[str, Unit], cfg: Config, bus: Bus,
                 log: DecisionLog, tesla=None):
        self.units = units
        self.cfg = cfg
        self.bus = bus
        self.log = log
        self.tesla = tesla

        self.snapshot = CoordinatorSnapshot()
        self._stop = asyncio.Event()

        # group_id -> [unit_id, ...]
        self.groups: dict[str, list[str]] = self._build_groups(units)

        # per-unit state
        self._held_off: dict[str, bool] = {uid: False for uid in units}
        self._ac_off_at: dict[str, float] = {uid: 0.0 for uid in units}
        self._ac_commanded: dict[str, bool] = {}
        self._ac_commanded_at: dict[str, float] = {}

        # per-group state
        # multiple VINs can share a bus_group (e.g. Tessa + Meridith both
        # on the DBS bus before the Anker is wired in)
        self._group_vins: dict[str, list[str]] = {g: [] for g in self.groups}
        self._plug_state: dict[str, bool] = {g: False for g in self.groups}
        # last time we saw the car plugged in (epoch); 0 = never
        self._last_plugged_at: dict[str, float] = {g: 0.0 for g in self.groups}
        # last command sent to each VIN's car (charging on/off), keyed by VIN.
        # Multiple VINs may share a group, so each tracks its own state.
        self._charging_commanded: dict[str, bool | None] = {}
        # wake-on-energize per group
        self._wake_campaign_until: dict[str, float] = {g: 0.0 for g in self.groups}
        self._last_wake: dict[str, float] = {g: 0.0 for g in self.groups}
        # Wall keep-alive (per VIN): a car plugged into a live external (wall)
        # source should always be charging — we don't control wall power, but a
        # Tesla can fall asleep and stop on its own, so we wake+start it.
        #
        # We often can't *see* the 240 V: an asleep car can't be polled, so its
        # charger_voltage reads the stale ~2 V proximity value — identical to a
        # car sitting on our switched-off DBS bus. So we probe blind: any plugged
        # car we aren't already serving from DBS gets woken+started. If it's on a
        # live wall it begins charging (confirmed -> keep-alive forever); if it's
        # on a dead bus, start does nothing, so unconfirmed probes are capped and
        # we back off until a reset event (plug-edge / unplug / our bus energizes
        # / a >=150 V sighting).
        # Confirmed-wall is persisted: an asleep car reads a stale ~2 V, so after
        # a restart we'd lose the >=150 V sighting and never re-confirm it (it
        # can't read 240 V until it's charging, and it won't charge until we set
        # amps, which we only do once confirmed). Persisting breaks that loop.
        self._wall_state_path = Path(
            cfg.get("wall_state_path")
            or (Path(__file__).resolve().parent.parent / "wall_state.json"))
        self._car_on_wall: dict[str, bool] = self._load_wall_state()
        # Per-VIN epoch of the last time a car was seen at/above "full" SoC.
        # Drives the dashboard's weekly-100%-recharge watchdog: these LFP packs
        # want a 100% charge ~weekly, so the user needs an alert when a car has
        # gone too long without one. Persisted so "days since 100%" survives
        # restarts.
        self._last_full_path = Path(
            cfg.get("charge_full_path")
            or (Path(__file__).resolve().parent.parent / "charge_full.json"))
        self._last_full_at: dict[str, float] = self._load_last_full()
        self._last_full_save_ts: float = 0.0
        self._last_wall_kick: dict[str, float] = {}
        self._wall_probe_attempts: dict[str, int] = {}  # blind probes since reset
        # most-recent "bus live" sense per group (any unit on & not floored)
        self._bus_was_live: dict[str, bool] = {g: False for g in self.groups}

        # Stuck-bus watchdog: inverters report ON but no power reaches a plugged
        # car (EVSE pilot/contactor handshake wedged). The fix is to re-present
        # the pilot by cycling dp109 off->on. We escalate: a cheap car-side
        # wake+start first (saves the station relay), then an AC cycle if still
        # stuck. Throttled and capped so we don't wear the relay indefinitely.
        self._bus_stuck_since: dict[str, float | None] = {g: None for g in self.groups}
        self._bus_carside_done: dict[str, bool] = {g: False for g in self.groups}
        self._last_bus_cycle: dict[str, float] = {g: 0.0 for g in self.groups}
        self._bus_cycle_count: dict[str, int] = {g: 0 for g in self.groups}
        self._bus_giveup_logged: dict[str, bool] = {g: False for g in self.groups}
        self._bus_giveup_at: dict[str, float] = {g: 0.0 for g in self.groups}
        # Hard-stuck alert: AC cycling recovers a SOFT stall but NOT a wedged
        # handshake (only a physical replug does). After the cycle cap we raise
        # this (message or None) so the user is told to replug, instead of
        # cycling silently.
        self._bus_replug_alert: dict[str, str | None] = {g: None for g in self.groups}
        # unit_ids currently mid-cycle — the main AC loop must not fight these
        self._bus_cycling: set[str] = set()
        # per-VIN keep-awake throttle — gently wake + amp-floor a reachable idle
        # car so it never sleeps into an unreachable 0-amp state.
        self._last_carside: dict[str, float] = {}

        # per-group balance state
        self._balance_state: dict[str, str] = {g: BalanceState.BALANCED for g in self.groups}
        self._weak_unit: dict[str, str | None] = {g: None for g in self.groups}
        self._balance_changed_at: dict[str, float] = {g: 0.0 for g in self.groups}
        self._diverged_since: dict[str, float | None] = {g: None for g in self.groups}
        self._converged_since: dict[str, float | None] = {g: None for g in self.groups}

        self._solar_hist: deque[tuple[float, float]] = deque(maxlen=240)
        # No-solar idle shutdown: epoch since system solar fell dark (None = sun)
        self._solar_dark_since: float | None = None

    @staticmethod
    def _build_groups(units: dict[str, Unit]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for uid, u in units.items():
            groups.setdefault(u.bus_group, []).append(uid)
        return groups

    def _load_wall_state(self) -> dict[str, bool]:
        try:
            data = json.loads(self._wall_state_path.read_text())
            return {str(k): bool(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_wall_state(self) -> None:
        try:
            self._wall_state_path.write_text(json.dumps(self._car_on_wall))
        except Exception as e:
            self.log.log("coordinator", "wall-state-save-error", error=str(e))

    def _load_last_full(self) -> dict[str, float]:
        try:
            data = json.loads(self._last_full_path.read_text())
            return {str(k): float(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_last_full(self) -> None:
        try:
            self._last_full_path.write_text(json.dumps(self._last_full_at))
        except Exception as e:
            self.log.log("coordinator", "last-full-save-error", error=str(e))

    def last_full_at(self, vin: str) -> float | None:
        """Epoch of the last observed full charge for this VIN, or None."""
        return self._last_full_at.get(vin)

    def stop(self) -> None:
        self._stop.set()

    # ────────────────────────────────────────────────────────────
    # main loop
    # ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:
                self.log.log("coordinator", "tick-error", error=str(e))
            await asyncio.sleep(self.cfg.getf("coordinator_tick_s", 15))

    async def tick(self) -> None:
        now = time.time()
        cfg = self.cfg

        # 1. classify + floor hysteresis (per-unit)
        for u in self.units.values():
            u.classify(cfg)
        for uid, u in self.units.items():
            if u.role == UnitRole.FLOORED or u.is_hard_floored(cfg):
                self._held_off[uid] = True
            elif u.at_rehab(cfg):
                self._held_off[uid] = False

        # 2. pull fresh Tesla state into the cache (non-blocking; the Beetle
        #    pushes states continuously, refresh just rebuilds TeslaState).
        if self.tesla is not None:
            try:
                await self.tesla.refresh_all()
            except Exception as e:
                self.log.log("coordinator", "tesla-refresh-error", error=str(e))

        # 2b. weekly-100%-recharge watchdog: stamp each car the moment it's seen
        #     at/above full SoC. Updated in memory every tick (so "days since
        #     full" reads 0 while it sits at 100%); the disk write is persisted
        #     immediately on a fresh full event and otherwise throttled so a car
        #     parked at 100% doesn't churn the file.
        if self.tesla is not None:
            full = cfg.getf("car_full_soc_pct", 99)
            fresh_event = False
            any_full = False
            for vin, c in self.tesla.cars.items():
                soc = c.car_soc_pct
                if soc is not None and soc >= full:
                    any_full = True
                    if now - self._last_full_at.get(vin, 0.0) > 3600:
                        fresh_event = True
                    self._last_full_at[vin] = now
            if fresh_event or (any_full and now - self._last_full_save_ts > 600):
                self._save_last_full()
                self._last_full_save_ts = now

        # 3. update group->vin map from config (config hot-reloads)
        self._refresh_group_vins()

        # 3b. track system solar darkness for the no-solar idle shutdown.
        #     Harvest is measured at the DBS units, so it reads ~0 W in two very
        #     different situations: the sun is actually down, OR the batteries
        #     are full and reject the charge — a full pack pulls 0 A from the
        #     panels even in bright sun (PV voltage is still present). Only the
        #     first is "dark". When every live unit is full a low harvest tells
        #     us nothing about the sun, so we must NOT start the dark timer;
        #     otherwise a 100%-SoC bank trips a false no-solar shutdown at noon.
        live_units = [u for u in self.units.values() if not u.is_stale(cfg)]
        total_solar_now = sum((u.state.solar_in_w or 0.0) for u in live_units)
        full_soc = cfg.getf("solar_full_soc_pct", 99)
        all_full = bool(live_units) and all(
            (u.state.soc_pct or 0.0) >= full_soc for u in live_units)
        if total_solar_now < cfg.getf("solar_dark_w", 50) and not all_full:
            if self._solar_dark_since is None:
                self._solar_dark_since = now
        else:
            self._solar_dark_since = None
        solar_dark = (self._solar_dark_since is not None and
                      (now - self._solar_dark_since) / 60
                      >= cfg.getf("solar_idle_shutoff_min", 5))

        # 4. per-group decisions
        self._solar_dark = solar_dark
        snap = CoordinatorSnapshot(tick_at=now)
        desired_ac: dict[str, bool] = {}
        plug_edges: dict[str, bool] = {}

        for group_id, unit_ids in self.groups.items():
            gsnap, gdesired, plug_edge = self._decide_group(group_id, unit_ids, now)
            snap.groups[group_id] = gsnap
            desired_ac.update(gdesired)
            plug_edges[group_id] = plug_edge

        # 5. actuate dp109 (per unit), bypassing min_dwell when the group
        #    just saw a plug-in edge (so the bus is hot the moment you plug in)
        min_dwell = cfg.getf("min_ac_dwell_s", 300)
        confirm_timeout = cfg.getf("ac_confirm_timeout_s", 90)
        for uid, want in desired_ac.items():
            if uid in self._bus_cycling:
                continue  # a stuck-bus cycle owns this unit right now
            u = self.units[uid]
            commanded = self._ac_commanded.get(uid)
            # seed tracker from reality on cold start
            if (commanded is None and u.state.ac_on is not None
                    and u.state.ac_on == want):
                self._ac_commanded[uid] = want
                self._ac_commanded_at[uid] = now
                continue
            if commanded == want:
                unconfirmed = (u.state.ac_on is not None and u.state.ac_on != want
                               and now - self._ac_commanded_at.get(uid, 0.0)
                               > confirm_timeout)
                if not unconfirmed:
                    continue
            edge = plug_edges.get(u.bus_group, False)
            if want and not edge and (now - self._ac_off_at.get(uid, 0.0) < min_dwell):
                continue  # too soon after last turn-off; plug-edge overrides
            cause = f"plug-edge/{u.bus_group}" if (want and edge) else f"group/{u.bus_group}"
            await set_ac(u, want, cause, cfg, self.log)
            self._ac_commanded[uid] = want
            self._ac_commanded_at[uid] = now
            if not want:
                self._ac_off_at[uid] = now

        # 6. actuate Tesla (per group) — start/stop charging via BLE
        await self._actuate_tesla(snap, now)

        # 6b. wall keep-alive — a car on a live wall source should be charging,
        #     independent of our DBS bus. Wake + start any that has dozed off.
        await self._wall_keepalive(snap, plug_edges, now)

        # 7. wake-on-energize per group (after actuation so any newly-armed
        #    campaign sees the fresh state next tick)
        for group_id in self.groups:
            self._maybe_wake(group_id, snap.groups[group_id], now)

        # 7b. keep reachable idle cars awake + at the amp floor (prevents them
        #     sleeping into an unreachable 0-amp state), then the stuck-bus
        #     watchdog for the heavier bus-dead recovery.
        for group_id in self.groups:
            self._keep_cars_alive(group_id, snap.groups[group_id], now)
        for group_id in self.groups:
            self._stuck_bus_watchdog(group_id, snap.groups[group_id], now)

        # 8. aggregate snapshot
        total_solar = sum((u.state.solar_in_w or 0.0) for u in self.units.values())
        total_out = sum((u.state.ac_out_w or 0.0) for u in self.units.values()
                        if u.state.ac_on and not u.is_stale(cfg))
        snap.total_solar_w = round(total_solar, 1)
        snap.total_ac_out_w = round(total_out, 1)
        snap.desired_ac = desired_ac
        snap.units_on = sum(1 for v in desired_ac.values() if v)
        snap.n_cars = sum(1 for g in snap.groups.values() if g.plugged_in)
        snap.actuator_ready = any("ac_on" in u.dps_map.values()
                                  for u in self.units.values())
        if snap.units_on == 0:
            snap.note = "no units feeding any bus"
        self.snapshot = snap
        self.bus.publish({"type": "coordinator", "snapshot": _snap_dict(snap)})

    # ────────────────────────────────────────────────────────────
    # per-group decision
    # ────────────────────────────────────────────────────────────

    def _refresh_group_vins(self) -> None:
        if self.tesla is None:
            return
        new_map: dict[str, list[str]] = {g: [] for g in self.groups}
        for entry in self.cfg.get("tesla_vins", []) or []:
            vin = entry.get("vin")
            g = entry.get("bus_group", "a")
            if vin and g in new_map:
                new_map[g].append(vin)
        self._group_vins = new_map

    def _decide_group(self, group_id: str, unit_ids: list[str], now: float
                      ) -> tuple[GroupSnapshot, dict[str, bool], bool]:
        cfg = self.cfg
        gunits = {uid: self.units[uid] for uid in unit_ids}
        vins = self._group_vins.get(group_id, [])
        cars = [self.tesla.cars.get(v) for v in vins] if self.tesla else []
        cars = [c for c in cars if c is not None]
        # Primary car kept for back-compat fields on GroupSnapshot.
        primary_car = cars[0] if cars else None
        primary_vin = vins[0] if vins else None

        # plug state + edge detection — "any car in group plugged?"
        any_plug_known = any(c.plugged_in is not None for c in cars)
        plugged_now = any(c.plugged_in is True for c in cars) if any_plug_known else None
        was_plugged = self._plug_state.get(group_id, False)
        plug_edge = bool(plugged_now) and not was_plugged
        if plugged_now is not None:
            self._plug_state[group_id] = bool(plugged_now)
            if plugged_now:
                self._last_plugged_at[group_id] = now

        # External-source detection — per-car. With shared bus_group (Tessa
        # + Meridith both on DBS), one may be on the wall while the other is
        # at our bus. A car is "on external" if it's actively charging at a
        # voltage our inverters can't produce. The bus is held off only if
        # ALL plugged cars are on external (= none are using us).
        ext_min_v = cfg.getf("external_source_min_voltage_v", 150)
        def _wall_live(c) -> bool:
            # Real external AC physically present right now — above what our
            # ~120 V DBS bus can produce. True regardless of whether the car is
            # currently drawing (a stopped/asleep car on the wall still reads it
            # until it opens its contactor).
            return (c.charger_voltage is not None
                    and c.charger_voltage >= ext_min_v)
        plugged_cars = [c for c in cars if c.plugged_in is True]
        # Maintain the sticky wall flag (persisted across restarts):
        #   - set    on any >=150 V sighting while plugged (it's on a wall),
        #   - clear  on unplug, OR when seen actively charging at our DBS-bus
        #            voltage (~100-150 V) — that self-corrects a car physically
        #            moved wall -> DBS while the service was down,
        #   - else   hold (survives the ~2 V contactor-open dip when stopped).
        # Surviving the dip keeps a stopped-on-the-wall car classified external
        # (so we don't energize the DBS bus for it) and drives the keep-alive.
        wall_changed = False
        for c in cars:
            prev = self._car_on_wall.get(c.vin)
            if c.plugged_in is False:
                new = False
            elif _wall_live(c):
                new = True
            elif (c.charging and c.charger_voltage is not None
                  and 100 <= c.charger_voltage < ext_min_v):
                new = False  # confirmed on our internal (DBS) bus
            else:
                new = prev
            if new is not None and new != prev:
                self._car_on_wall[c.vin] = new
                wall_changed = True
        if wall_changed:
            self._save_wall_state()
        def _on_external(c) -> bool:
            return bool(c.plugged_in is True
                        and (self._car_on_wall.get(c.vin) or _wall_live(c)))
        any_on_external = any(_on_external(c) for c in plugged_cars)
        # "Internal" candidates: plugged cars that are NOT on the wall — i.e.
        # they may be on our bus (or available to be).
        internal_plugged = [c for c in plugged_cars if not _on_external(c)]
        on_external_all = bool(plugged_cars) and not internal_plugged
        on_external = on_external_all  # kept name for downstream consumers

        def _wants_charge(c) -> bool:
            # A plugged car still wants power until it reports "Complete". We
            # can't use bus draw here (with the bus off the car draws nothing),
            # so trust the car's own state: Disconnected/NoPower/Stopped/Charging
            # all still want charge; only Complete is done. Unknown -> assume yes.
            cs = (c.charging_state or "").replace(" ", "")
            return cs != "Complete"

        # Overload safety: the inverters parallel into ONE shared bus, and two
        # ~1200 W cars exceed a single ~1400 W inverter. So whenever ≥2 cars on
        # our bus still want charge, BOTH inverters must stay on — this overrides
        # rebalance/duty-cycling (never single-leg the parallel bus under a
        # two-car load). The complementary guard is in _actuate_tesla: never
        # command more cars to charge than inverters are live. Switching a leg
        # while a car draws is also the top red-state trigger (see memory), so
        # the two-car case is doubly off-limits for single-leg.
        charge_candidates = [c for c in internal_plugged if _wants_charge(c)]
        overload_both_on = len(charge_candidates) >= 2

        # bus-live policy: when do we want the inverters energized?
        idle_min = cfg.getf("inverter_idle_shutoff_min", 30)
        last_plug = self._last_plugged_at.get(group_id, 0.0)
        solar_shutdown = False
        # No-solar idle shutdown: with the sun down and nothing actually
        # drawing, running the bus is pure standby waste — and a dark bus can't
        # get a car stuck. Plug-edges override (the user just plugged in to
        # charge); set solar_idle_require_idle: false for a hard shutdown.
        group_out = sum((gunits[uid].state.ac_out_w or 0.0) for uid in unit_ids
                        if gunits[uid].state.ac_on)
        bus_idle = group_out < cfg.getf("stuck_bus_draw_w", 50)
        dark_shutdown = (getattr(self, "_solar_dark", False) and not plug_edge
                         and (bus_idle or not cfg.get("solar_idle_require_idle", True)))
        if not cars:
            # No Tesla wired into this group -> default to "always available";
            # the floor / rebalance / override logic still gates per unit.
            want_bus_live = True
        elif on_external:
            want_bus_live = False
        elif plugged_now is True:
            # A plugged, on-our-bus car is a reason to energize. Only fall to a
            # standby shutdown when the sun is genuinely down (dark_shutdown)
            # AND either the car is done charging (reports "Complete") or the
            # operator chose a hard cutoff (solar_idle_require_idle: false) that
            # overrides even a car still wanting charge. dark_shutdown already
            # excludes a fresh plug-edge and, in the default (soft) mode, an
            # actively-drawing car (via bus_idle); this additionally keeps the
            # bus live for a car that still wants charge but can't draw yet
            # because the bus is off — the exact case a full-bank false-dark, or
            # a car already plugged at startup, used to strand.
            wants_charge = any(_wants_charge(c) for c in internal_plugged)
            hard_cutoff = not cfg.get("solar_idle_require_idle", True)
            if dark_shutdown and (not wants_charge or hard_cutoff):
                want_bus_live = False
                solar_shutdown = True
            else:
                want_bus_live = True
        elif last_plug > 0 and (now - last_plug) / 60 < idle_min:
            # Recently unplugged — keep bus warm in case it's a quick swap.
            want_bus_live = True
        else:
            want_bus_live = False

        # eligibility (NORMAL + not held off by floor latch)
        eligible = {
            uid: (gunits[uid].role == UnitRole.NORMAL
                  and not self._held_off.get(uid, False))
            for uid in unit_ids
        }
        usable = [uid for uid in unit_ids if eligible[uid]]

        # rebalance (intra-group; only meaningful with ≥2 units)
        if len(unit_ids) >= 2:
            self._evaluate_balance(group_id, gunits, now)
        elif self._balance_state[group_id] == BalanceState.REBALANCING:
            self._balance_state[group_id] = BalanceState.BALANCED
            self._weak_unit[group_id] = None
            self._balance_changed_at[group_id] = now

        balance_state = self._balance_state[group_id]
        weak_id = self._weak_unit[group_id]

        # decide desired AC per unit in this group
        desired: dict[str, bool] = {}
        for uid in unit_ids:
            u = gunits[uid]
            if u.role == UnitRole.OVERRIDE:
                desired[uid] = bool(u.override_target)
            elif u.role == UnitRole.OFFLINE:
                continue
            elif not eligible[uid]:
                desired[uid] = False
            elif not want_bus_live:
                desired[uid] = False  # idle-shutoff
            elif (balance_state == BalanceState.REBALANCING and uid == weak_id
                  and not overload_both_on):
                desired[uid] = False  # duty-cycled off to let strong recover
                #                       (suppressed under a two-car load — both
                #                        inverters must stay on for overload safety)
            else:
                desired[uid] = True

        # Overload cap: clear at most one car to charge per live inverter, since
        # two ~1200 W cars overrun a single ~1400 W inverter on the shared bus.
        # Prefer the most-depleted cars (lowest SoC first). Normally both legs
        # are up (overload_both_on), so both cars clear; the cap only bites when
        # a leg is down for safety (SoC-floor) while ≥2 cars want charge — then
        # the extra car is held off (graceful BLE stop in _actuate_tesla) instead
        # of overloading the lone inverter.
        units_on_count = sum(1 for v in desired.values() if v)
        ranked = sorted(charge_candidates,
                        key=lambda c: (c.car_soc_pct if c.car_soc_pct is not None
                                       else 999.0))
        charge_allowed_vins = [c.vin for c in ranked[:units_on_count]]

        # ── group snapshot
        gsnap = GroupSnapshot(
            group_id=group_id,
            vin=primary_vin,
            vins=list(vins),
            car_name=(primary_car.name if primary_car else None),
            plugged_in=plugged_now,
            last_plugged_at=(last_plug or None),
            want_bus_live=want_bus_live,
            units=list(unit_ids),
            units_on=sum(1 for uid in unit_ids if desired.get(uid)),
            balance_state=balance_state,
            weak_unit=weak_id,
            charge_allowed_vins=charge_allowed_vins,
        )

        # Notes — most-useful single line for the dashboard.
        if not usable and unit_ids:
            gsnap.note = "no usable units (all floored/offline/override-off)"
        elif on_external_all:
            ext_car = next((c for c in plugged_cars if _on_external(c)), None)
            v = ext_car.charger_voltage if ext_car else None
            label = ext_car.name if ext_car else "car"
            gsnap.note = f"{label} on external source ({v}V) — bus held off"
        elif any_on_external and internal_plugged:
            wall_names = ", ".join(c.name for c in plugged_cars if _on_external(c))
            ours = ", ".join(c.name for c in internal_plugged)
            gsnap.note = f"{wall_names} on wall; bus serving {ours}"
        elif solar_shutdown:
            gsnap.note = "no-solar shutdown: dark + nothing drawing"
        elif not want_bus_live and plugged_now is False:
            gsnap.note = "idle-shutoff: no car plugged"
        elif plug_edge:
            gsnap.note = "plug-in edge — energizing"
        elif balance_state == BalanceState.REBALANCING:
            gsnap.note = f"rebalancing: holding {weak_id} off"

        return gsnap, desired, plug_edge

    # ────────────────────────────────────────────────────────────
    # tesla on/off + wake
    # ────────────────────────────────────────────────────────────

    async def _actuate_tesla(self, snap: CoordinatorSnapshot, now: float) -> None:
        """Send start/stop charging commands to EVERY plugged-in car in each
        group. With multiple cars on one bus (Tessa + Meridith on DBS), all
        get the same intent — physically only one car draws at a time per
        plug, but commanding both is harmless and keeps state consistent.
        """
        if self.tesla is None:
            return
        for group_id, gsnap in snap.groups.items():
            vins = gsnap.vins or ([gsnap.vin] if gsnap.vin else [])
            if not vins:
                continue
            allowed = set(gsnap.charge_allowed_vins)
            for vin in vins:
                car = self.tesla.cars.get(vin)
                if car is None or car.plugged_in is False:
                    continue  # not plugged → nothing to command
                # Only cars cleared by the overload cap charge; the rest get a
                # graceful BLE stop so a lone inverter never carries two cars.
                want_charging = (vin in allowed) and (car.plugged_in is True)
                commanded = self._charging_commanded.get(vin)
                if commanded is None and not want_charging:
                    # Don't preempt a session we didn't start.
                    continue
                if commanded == want_charging:
                    continue
                try:
                    if want_charging:
                        ok = await self.tesla.start_charging(vin)
                    else:
                        ok = await self.tesla.stop_charging(vin)
                except Exception as e:
                    self.log.log("coordinator", "tesla-actuate-error",
                                 group=group_id, vin=vin,
                                 want=want_charging, error=str(e))
                    continue
                if ok:
                    self._charging_commanded[vin] = want_charging

    async def _wall_keepalive(self, snap: CoordinatorSnapshot,
                              plug_edges: dict[str, bool], now: float) -> None:
        """Keep wall-plugged cars awake and charging.

        We don't actuate wall power, but a Tesla can fall asleep / stop on its
        own while plugged into a live external source — sitting idle for no
        reason (caught on Meridith). The catch: an asleep car can't be polled,
        so we usually *can't see* the 240 V (its charger_voltage reads the stale
        ~2 V proximity value, same as a car on our switched-off DBS bus). So we
        probe blind.

        For each plugged car that isn't charging or full:
          - confirmed on-wall (ever seen >=150 V, or currently charging): wake +
            start on every retry interval, indefinitely — it *should* charge.
          - unconfirmed: wake + start up to `wall_probe_max_attempts` times; if
            it never starts, the bus it's on is dead/off, so go dormant until a
            reset event.

        We do NOT gate on whether our DBS bus is serving the group: cars share a
        bus_group but may be split across the wall and the DBS bus (Tessa on DBS,
        Meridith on the wall), so a group-level "serving" flag can't tell us
        which bus a given car is on. Probing a car already powered by a live bus
        is harmless — start_charging just makes it charge.

        Reset events (clear the probe counter): a plug-in edge, an unplug, our
        DBS bus being live for the group (not a dead bus), or the car actually
        charging. This runs independently of `_actuate_tesla` and never touches
        `_charging_commanded`, so the two can't fight.
        """
        if self.tesla is None or not self.cfg.get("tesla_wake_enabled", True):
            return
        retry = self.cfg.getf("wall_keepalive_retry_s", 180)
        max_probe = int(self.cfg.getf("wall_probe_max_attempts", 3))

        for group_id, gsnap in snap.groups.items():
            vins = gsnap.vins or ([gsnap.vin] if gsnap.vin else [])
            bus_live = gsnap.units_on > 0      # our bus has power on this group
            edge = plug_edges.get(group_id, False)
            for vin in vins:
                car = self.tesla.cars.get(vin)
                if car is None or car.plugged_in is not True:
                    # unplugged / unknown -> reset probe state
                    self._wall_probe_attempts.pop(vin, None)
                    continue
                # Resets: plug-edge, our bus is live (a real power source), or
                # the car is actually charging.
                if edge or bus_live or car.charging is True:
                    self._wall_probe_attempts.pop(vin, None)
                if car.charging is True:
                    continue  # already drawing — nothing to do
                if _norm_state(car.charging_state or "") == "Complete":
                    continue  # battery full — leave it be
                confirmed = bool(self._car_on_wall.get(vin))
                if not confirmed and self._wall_probe_attempts.get(vin, 0) >= max_probe:
                    continue  # dormant: no power on this bus, await a reset
                if now - self._last_wall_kick.get(vin, 0.0) < retry:
                    continue
                self._last_wall_kick[vin] = now
                if not confirmed:
                    self._wall_probe_attempts[vin] = \
                        self._wall_probe_attempts.get(vin, 0) + 1
                asyncio.create_task(self._do_wall_kick(vin, confirmed))

    async def _do_wall_kick(self, vin: str, confirmed: bool) -> None:
        try:
            await self.tesla.wake(vin)
            await asyncio.sleep(self.cfg.getf("tesla_wake_settle_s", 3))
            amps_set = None
            if confirmed:
                # Confirmed on a wall EVSE: a stalled car can sit at 0 A, so
                # push a charge-amp request. The EVSE pilot signal clamps the
                # real draw to the circuit's safe rating, so requesting high is
                # safe here. We do this ONLY for confirmed-wall cars — never the
                # DBS bus, where the 1400 W inverter (not a pilot) is the limit.
                amps = int(self.cfg.getf("wall_charge_amps", 32))
                car = self.tesla.cars.get(vin)
                if car is not None and (car.set_amps or 0) < amps:
                    amps_set = await self.tesla.set_amps(vin, amps)
                    await asyncio.sleep(1)
            ok = await self.tesla.start_charging(vin)
            self.log.log("coordinator", "wall-keepalive", vin=vin,
                         started=bool(ok),
                         mode="confirmed" if confirmed else "probe",
                         amps_set=amps_set,
                         attempt=self._wall_probe_attempts.get(vin, 0))
        except Exception as e:
            self.log.log("coordinator", "wall-keepalive-error",
                         vin=vin, error=str(e))

    def _maybe_wake(self, group_id: str, gsnap: GroupSnapshot, now: float) -> None:
        """Run a bounded wake campaign when this group's bus newly goes live.

        On a fresh energize edge (or plug-in onto a hot bus) a deeply asleep
        Tesla can miss the power-on event and never start drawing. We wake
        every plugged-in VIN in the group a few times over a campaign window,
        ending early once draw is observed.
        """
        if self.tesla is None or not self.cfg.get("tesla_wake_enabled", True):
            return
        vins = gsnap.vins or ([gsnap.vin] if gsnap.vin else [])
        if not vins:
            return

        any_unit_on = gsnap.units_on > 0
        was_live = self._bus_was_live.get(group_id, False)
        # arm on bus-live edge OR on plug-edge while bus is live
        if any_unit_on and (not was_live or (gsnap.plugged_in and gsnap.note.startswith("plug-in edge"))):
            self._wake_campaign_until[group_id] = now + self.cfg.getf(
                "tesla_wake_campaign_s", 480)
        self._bus_was_live[group_id] = any_unit_on

        if now >= self._wake_campaign_until.get(group_id, 0.0):
            return

        # measure draw across this group's units
        group_out = sum((self.units[uid].state.ac_out_w or 0.0)
                        for uid in self.groups[group_id]
                        if self.units[uid].state.ac_on)
        car_w = self.cfg.getf("car_sink_w", 1200)
        if group_out >= 0.4 * car_w:
            self._wake_campaign_until[group_id] = 0.0  # car is drawing — done
            return

        retry = self.cfg.getf("tesla_wake_retry_s", 150)
        if now - self._last_wake.get(group_id, 0.0) < retry:
            return
        self._last_wake[group_id] = now
        # Wake every plugged-in car in the group.
        plugged_vins = [v for v in vins
                        if (c := self.tesla.cars.get(v)) and c.plugged_in is True]
        if not plugged_vins:
            return
        asyncio.create_task(self._do_wake(plugged_vins, group_id))

    async def _do_wake(self, vins: list[str], group_id: str) -> None:
        floor = int(self.cfg.getf("dbs_charge_amps", 16))
        for vin in vins:
            try:
                await self.tesla.wake(vin)
                await asyncio.sleep(2)
                car = self.tesla.cars.get(vin)
                if (car is not None and not self._car_on_wall.get(vin)
                        and (car.set_amps or 0) < floor):
                    await self.tesla.set_amps(vin, floor)
                    await asyncio.sleep(1)
                await self.tesla.start_charging(vin)
                self.log.log("coordinator", "wake-on-energize",
                             group=group_id, vin=vin)
            except Exception as e:
                self.log.log("coordinator", "wake-error", group=group_id, vin=vin,
                             error=str(e))

    # ────────────────────────────────────────────────────────────
    # stuck-bus watchdog
    # ────────────────────────────────────────────────────────────

    def _keep_cars_alive(self, group_id: str, gsnap: GroupSnapshot,
                         now: float) -> None:
        """Gently keep a reachable, plugged, idle car awake + at the amp floor.

        The overnight failure mode: a plugged car not charging goes to sleep,
        its BLE radio drops, we can no longer set its amps, and it stays at 0 A.
        While a car is still REACHABLE we periodically wake + re-assert the amp
        floor + start, so the link never dies and it draws the moment the bus is
        live. This is per-car and never cycles the relay, so it also un-masks a
        second car on a shared bus (the group AC-cycle can't fire while another
        car is drawing). Cars whose link is already down are left to the
        reconnect/AC-cycle paths — waking them here would be futile.
        """
        if self.tesla is None:
            return
        unit_ids = self.groups.get(group_id, [])
        bus_live = (any(self.units[u].state.ac_on for u in unit_ids)
                    and bool(gsnap.want_bus_live))
        if not bus_live:
            return
        retry = self.cfg.getf("tesla_keep_alive_s", 480)
        floor = int(self.cfg.getf("dbs_charge_amps", 16))
        vins = gsnap.vins or ([gsnap.vin] if gsnap.vin else [])
        for vin in vins:
            car = self.tesla.cars.get(vin)
            if car is None or car.plugged_in is not True:
                continue
            if self._car_on_wall.get(vin):
                continue
            if not getattr(car, "reachable", False):
                continue  # link down — reconnect/AC-cycle own this; waking is futile
            if _norm_state(car.charging_state or "") == "Complete":
                continue
            if (car.actual_amps or 0) > 0:
                # Already drawing — only push amps if the level is below target.
                if ((car.set_amps or 0) < floor
                        and now - self._last_carside.get(vin, 0.0) >= retry):
                    self._last_carside[vin] = now
                    self.log.log("coordinator", "amp-correction",
                                 group=group_id, vin=vin,
                                 floor=floor, set_amps=car.set_amps)
                    asyncio.create_task(self.tesla.set_amps(vin, floor))
                continue
            # The shared bus is already delivering power (a car is charging) —
            # don't kick, since BLE port readings are unreliable and we can't be
            # sure THIS car isn't the one drawing.
            grp_units = self.groups.get(group_id, [])
            grp_out = sum((self.units[u].state.ac_out_w or 0.0) for u in grp_units
                          if self.units[u].state.ac_on)
            if grp_out >= self.cfg.getf("stuck_bus_draw_w", 50):
                continue
            if now - self._last_carside.get(vin, 0.0) < retry:
                continue
            self._last_carside[vin] = now
            self.log.log("coordinator", "keep-alive-kick", group=group_id, vin=vin)
            asyncio.create_task(self._kick_dbs_cars([vin], group_id))

    def _stuck_bus_watchdog(self, group_id: str, gsnap: GroupSnapshot,
                            now: float) -> None:
        """Detect a wedged EVSE handshake and re-present power to fix it.

        Symptom: our inverters are ON for a group (dp109 True) and the batteries
        are healthy, but a plugged-in car draws nothing — the AC out is ~0 W and
        the car reads no bus voltage. The J1772/contactor handshake is stuck.
        Sending the car "start" alone won't fix it; the pilot must be
        re-presented by cycling the inverter off->on.

        Escalation (to spare the station relay):
          1. car-side wake+start once,
          2. if still stuck after a grace period, cycle the AC relay,
          3. throttled, and capped at `stuck_bus_max_cycles` — past that we log
             once for manual intervention and stop cycling.
        """
        if self.tesla is None:
            return
        unit_ids = self.groups.get(group_id, [])
        # Reflect any standing replug alert onto this tick's snapshot.
        gsnap.alert = self._bus_replug_alert.get(group_id)
        if any(uid in self._bus_cycling for uid in unit_ids):
            return  # a cycle is in flight; don't evaluate mid-transition

        cfg = self.cfg
        draw_w = cfg.getf("stuck_bus_draw_w", 50)
        on_units = [self.units[uid] for uid in unit_ids if self.units[uid].state.ac_on]
        # The inverter AC-out (dp158) is the ONLY reliable "is power flowing"
        # signal. The cars' own BLE charge telemetry (charger_voltage/current)
        # is NOT trustworthy on this firmware — it reads ~2 V / 0 A even while a
        # car is drawing 10 A (confirmed against dp158). So we must gate the
        # heavy AC cycle on real measured output, never on the car's port
        # reading, or we'd cycle (and interrupt) a car that's actually charging.
        group_out = sum((u.state.ac_out_w or 0.0) for u in on_units)
        bus_live = bool(on_units) and bool(gsnap.want_bus_live)

        vins = gsnap.vins or ([gsnap.vin] if gsnap.vin else [])
        stuck_cars = []
        for vin in vins:
            car = self.tesla.cars.get(vin)
            if car is None or car.plugged_in is not True:
                continue
            if self._car_on_wall.get(vin):
                continue
            if _norm_state(car.charging_state or "") == "Complete":
                continue
            if (car.actual_amps or 0) > 0:
                continue  # BLE says drawing (rarely reliable, but trust a positive)
            stuck_cars.append(vin)

        # Cycle only when the inverters are delivering essentially nothing — that
        # is the one signal that truly means no car is charging.
        stuck = bus_live and bool(stuck_cars) and group_out < draw_w
        if not stuck:
            if self._bus_stuck_since[group_id] is not None:
                self.log.log("coordinator", "stuck-bus-cleared", group=group_id)
            if self._bus_replug_alert.get(group_id):
                self.log.log("coordinator", "stuck-bus-recovered", group=group_id)
            self._bus_stuck_since[group_id] = None
            self._bus_carside_done[group_id] = False
            self._bus_cycle_count[group_id] = 0
            self._bus_giveup_logged[group_id] = False
            self._bus_replug_alert[group_id] = None     # clear the replug alert
            gsnap.alert = None
            return

        if self._bus_stuck_since[group_id] is None:
            self._bus_stuck_since[group_id] = now
        dur = now - self._bus_stuck_since[group_id]

        detect_s = cfg.getf("stuck_bus_detect_s", 120)
        if dur < detect_s:
            return

        # Step 1 — cheap car-side kick first (no relay wear).
        if not self._bus_carside_done[group_id]:
            self._bus_carside_done[group_id] = True
            self.log.log("coordinator", "stuck-bus-carside",
                         group=group_id, vins=stuck_cars, stuck_s=round(dur))
            asyncio.create_task(self._kick_dbs_cars(stuck_cars, group_id))
            return

        # Step 2 — escalate to an AC relay cycle.
        if dur < detect_s + cfg.getf("stuck_bus_carside_s", 45):
            return
        max_cycles = int(cfg.getf("stuck_bus_max_cycles", 4))
        if self._bus_cycle_count[group_id] >= max_cycles:
            # Stop power-cycling after the cap — do NOT re-arm on a timer. A few
            # cycles clear a soft stall; past that the car is wedged (red-ring
            # latch), which only a physical replug clears. More AC cycles can't
            # help and each one is another power-interruption that deepens the
            # latch (see memory: red-state-from-hot-bus-switching). We hold the
            # bus live + raise the replug alert and leave it to the human.
            # The cycle budget only refreshes when the car actually recovers or
            # unplugs (the "not stuck" reset above) — never on elapsed time.
            # Car-side waking (harmless, no power interruption) still runs via
            # _maybe_wake, so an asleep-but-not-wedged car can still come back.
            if not self._bus_giveup_logged[group_id]:
                self._bus_giveup_logged[group_id] = True
                self._bus_giveup_at[group_id] = now
                names = ", ".join(
                    (self.tesla.cars.get(v).name if self.tesla and self.tesla.cars.get(v)
                     else v[-6:]) for v in stuck_cars) or "car"
                alert = (f"Replug needed — {names}: bus is live but no power is "
                         f"drawing after {self._bus_cycle_count[group_id]} AC cycles. "
                         f"Cycling fixes a soft stall; a wedged handshake needs a "
                         f"physical unplug/replug.")
                self._bus_replug_alert[group_id] = alert
                gsnap.alert = alert
                self.log.log("coordinator", "stuck-bus-replug-needed",
                             group=group_id, vins=stuck_cars,
                             cycles=self._bus_cycle_count[group_id], alert=alert)
            return
        if now - self._last_bus_cycle[group_id] < cfg.getf("stuck_bus_cycle_throttle_s", 300):
            return
        self._last_bus_cycle[group_id] = now
        self._bus_cycle_count[group_id] += 1
        cycle_uids = [u.unit_id for u in on_units]
        self.log.log("coordinator", "stuck-bus-cycle",
                     group=group_id, n=self._bus_cycle_count[group_id],
                     units=cycle_uids, vins=stuck_cars, stuck_s=round(dur))
        asyncio.create_task(self._do_bus_cycle(group_id, cycle_uids, stuck_cars))

    async def _do_bus_cycle(self, group_id: str, unit_ids: list[str],
                            vins: list[str]) -> None:
        """Cycle dp109 off->on for the group's live units to re-present the EVSE
        pilot, then wake+start the stuck cars. Units are guarded in
        `_bus_cycling` so the main AC loop won't fight the transition.
        """
        cfg = self.cfg
        # Off-dwell escalates with each cycle: an asleep car may need a longer,
        # unmistakable disconnect before it re-runs the EVSE handshake.
        base = cfg.getf("stuck_bus_cycle_dwell_s", 20)
        step = cfg.getf("stuck_bus_cycle_dwell_step_s", 10)
        dwell_max = cfg.getf("stuck_bus_cycle_dwell_max_s", 60)
        n = self._bus_cycle_count.get(group_id, 1)
        dwell = min(base + max(0, n - 1) * step, dwell_max)
        for uid in unit_ids:
            self._bus_cycling.add(uid)
        try:
            t = time.time()
            for uid in unit_ids:
                await set_ac(self.units[uid], False, f"stuck-bus-cycle-off/{group_id}",
                             cfg, self.log)
                self._ac_commanded[uid] = False
                self._ac_commanded_at[uid] = t
                self._ac_off_at[uid] = t
            await asyncio.sleep(dwell)
            t2 = time.time()
            for uid in unit_ids:
                await set_ac(self.units[uid], True, f"stuck-bus-cycle-on/{group_id}",
                             cfg, self.log)
                self._ac_commanded[uid] = True
                self._ac_commanded_at[uid] = t2
            await asyncio.sleep(2)  # let the bus settle before nudging the car
            await self._kick_dbs_cars(vins, group_id)
        except Exception as e:
            self.log.log("coordinator", "stuck-bus-cycle-error",
                         group=group_id, error=str(e))
        finally:
            for uid in unit_ids:
                self._bus_cycling.discard(uid)

    async def _kick_dbs_cars(self, vins: list[str], group_id: str) -> None:
        """Wake a DBS-bus car, ensure a non-zero charge-amp request, then start.

        A car can sit at 0 A (its charge-current request reset to zero), so even
        a perfectly live bus delivers nothing — re-presenting the pilot won't
        help. We set a floor (`dbs_charge_amps`, default 16 A): on
        the DBS bus the inverter, not an EVSE pilot, is the hard limit, so we
        must never request the 32 A used for wall charging.
        Skips cars flagged on a wall (the wall keep-alive owns their amperage).
        """
        floor = int(self.cfg.getf("dbs_charge_amps", 16))
        for vin in vins:
            try:
                await self.tesla.wake(vin)
                await asyncio.sleep(2)
                car = self.tesla.cars.get(vin)
                if (car is not None and not self._car_on_wall.get(vin)
                        and (car.set_amps or 0) < floor):
                    await self.tesla.set_amps(vin, floor)
                    await asyncio.sleep(1)
                await self.tesla.start_charging(vin)
                self.log.log("coordinator", "stuck-bus-kick",
                             group=group_id, vin=vin, amps_floor=floor)
            except Exception as e:
                self.log.log("coordinator", "stuck-bus-kick-error",
                             group=group_id, vin=vin, error=str(e))

    # ────────────────────────────────────────────────────────────
    # rebalance (per group)
    # ────────────────────────────────────────────────────────────

    def _evaluate_balance(self, group_id: str, gunits: dict[str, Unit],
                          now: float) -> None:
        pair = [u for u in gunits.values()
                if u.state.soc_pct is not None and not u.is_stale(self.cfg)]
        if len(pair) < 2:
            return

        pair.sort(key=lambda u: u.state.soc_pct or 0.0)
        weak, strong = pair[0], pair[-1]
        delta = (strong.state.soc_pct or 0) - (weak.state.soc_pct or 0)

        trigger = self.cfg.getf("divergence_trigger_pct", 15)
        clear = self.cfg.getf("divergence_clear_pct", 5)
        min_strong = self.cfg.getf("rebalance_min_strong_soc_pct", 60)
        min_dwell = self.cfg.getf("rebalance_min_dwell_min", 20) * 60
        dwell_ok = now - self._balance_changed_at[group_id] >= min_dwell
        weak_offline = weak.role == UnitRole.OFFLINE
        strong_high = (strong.state.soc_pct or 0) >= min_strong

        state = self._balance_state[group_id]
        if state == BalanceState.BALANCED:
            # Rebalance exists to prevent the stronger unit from coasting to
            # 100% and clipping incoming solar. Below the min-strong gate
            # there is no clipping risk, so divergence is left alone — the
            # SoC floor handles overdischarge.
            want = (delta >= trigger) and strong_high and not weak_offline
            self._diverged_since[group_id] = (
                self._diverged_since[group_id] or now) if want else None
            persist = self.cfg.getf("divergence_persist_min", 10) * 60
            since = self._diverged_since[group_id]
            if since and now - since >= persist and dwell_ok:
                self._balance_state[group_id] = BalanceState.REBALANCING
                self._weak_unit[group_id] = weak.unit_id
                self._balance_changed_at[group_id] = now
                self._diverged_since[group_id] = None
                self.log.log("coordinator", "rebalance-enter",
                             group=group_id, weak=weak.name, strong=strong.name,
                             delta_soc=round(delta, 1))
        else:  # REBALANCING
            recovered = self.cfg.getf("weak_recovered_pct", 85)
            max_dur = self.cfg.getf("rebalance_max_duration_min", 90) * 60
            weak_unit = gunits.get(self._weak_unit[group_id] or "")
            weak_soc = weak_unit.state.soc_pct if weak_unit else None
            crossed_over = bool(weak_unit) and weak.unit_id != self._weak_unit[group_id]
            abandoned_low = not strong_high

            converged = delta <= clear
            self._converged_since[group_id] = (
                self._converged_since[group_id] or now) if converged else None
            clear_persist = self.cfg.getf("divergence_clear_persist_min", 5) * 60
            since = self._converged_since[group_id]
            exit_now = (
                crossed_over
                or abandoned_low
                or (since and now - since >= clear_persist)
                or (weak_soc is not None and weak_soc >= recovered)
                or (now - self._balance_changed_at[group_id] >= max_dur)
            )
            if exit_now and (dwell_ok or crossed_over):
                cause = ("crossed-over" if crossed_over
                         else "abandoned-low-strong" if abandoned_low
                         else "converged" if since
                         else "recovered" if (weak_soc or 0) >= recovered
                         else "max-duration")
                self.log.log("coordinator", f"rebalance-exit: {cause}",
                             group=group_id, delta_soc=round(delta, 1))
                self._balance_state[group_id] = BalanceState.BALANCED
                self._weak_unit[group_id] = None
                self._balance_changed_at[group_id] = now
                self._converged_since[group_id] = None

    # ────────────────────────────────────────────────────────────
    # introspection
    # ────────────────────────────────────────────────────────────

    @property
    def balance_state(self) -> str:
        """Aggregate balance — for back-compat with anything not yet group-aware."""
        for s in self._balance_state.values():
            if s == BalanceState.REBALANCING:
                return BalanceState.REBALANCING
        return BalanceState.BALANCED

    @property
    def weak_unit_id(self) -> str | None:
        for w in self._weak_unit.values():
            if w:
                return w
        return None

    def state_dict(self) -> dict:
        return _snap_dict(self.snapshot)


def _snap_dict(s: CoordinatorSnapshot) -> dict:
    from dataclasses import asdict

    return asdict(s)
