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
import time
from collections import deque

from .bus import Bus
from .config import Config
from .decisions import DecisionLog
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
        self._group_vin: dict[str, str | None] = {g: None for g in self.groups}
        self._plug_state: dict[str, bool] = {g: False for g in self.groups}
        # last time we saw the car plugged in (epoch); 0 = never
        self._last_plugged_at: dict[str, float] = {g: 0.0 for g in self.groups}
        # last command sent to the car for this group's VIN (charging on/off)
        self._charging_commanded: dict[str, bool | None] = {g: None for g in self.groups}
        # wake-on-energize per group
        self._wake_campaign_until: dict[str, float] = {g: 0.0 for g in self.groups}
        self._last_wake: dict[str, float] = {g: 0.0 for g in self.groups}
        # most-recent "bus live" sense per group (any unit on & not floored)
        self._bus_was_live: dict[str, bool] = {g: False for g in self.groups}

        # per-group balance state
        self._balance_state: dict[str, str] = {g: BalanceState.BALANCED for g in self.groups}
        self._weak_unit: dict[str, str | None] = {g: None for g in self.groups}
        self._balance_changed_at: dict[str, float] = {g: 0.0 for g in self.groups}
        self._diverged_since: dict[str, float | None] = {g: None for g in self.groups}
        self._converged_since: dict[str, float | None] = {g: None for g in self.groups}

        self._solar_hist: deque[tuple[float, float]] = deque(maxlen=240)

    @staticmethod
    def _build_groups(units: dict[str, Unit]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for uid, u in units.items():
            groups.setdefault(u.bus_group, []).append(uid)
        return groups

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

        # 3. update group->vin map from config (config hot-reloads)
        self._refresh_group_vins()

        # 4. per-group decisions
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

        # 7. wake-on-energize per group (after actuation so any newly-armed
        #    campaign sees the fresh state next tick)
        for group_id in self.groups:
            self._maybe_wake(group_id, snap.groups[group_id], now)

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
        new_map: dict[str, str | None] = {g: None for g in self.groups}
        for entry in self.cfg.get("tesla_vins", []) or []:
            vin = entry.get("vin")
            g = entry.get("bus_group", "a")
            if vin and g in new_map:
                new_map[g] = vin
        self._group_vin = new_map

    def _decide_group(self, group_id: str, unit_ids: list[str], now: float
                      ) -> tuple[GroupSnapshot, dict[str, bool], bool]:
        cfg = self.cfg
        gunits = {uid: self.units[uid] for uid in unit_ids}
        vin = self._group_vin.get(group_id)
        car = self.tesla.cars.get(vin) if (self.tesla is not None and vin) else None

        # plug state + edge detection
        plugged_now = bool(car.plugged_in) if (car and car.plugged_in is not None) else None
        was_plugged = self._plug_state.get(group_id, False)
        plug_edge = bool(plugged_now) and not was_plugged
        if plugged_now is not None:
            self._plug_state[group_id] = bool(plugged_now)
            if plugged_now:
                self._last_plugged_at[group_id] = now

        # If the car reports actively charging at a voltage our inverters can't
        # produce (~120 V), it's on a different source (e.g., 240 V house
        # wall). We share BLE plug-detection with that other source, so just
        # "plugged_in=True" isn't enough — energizing our bus would waste
        # standby on inverters with no load (the car has one port, not ours).
        ext_min_v = cfg.getf("external_source_min_voltage_v", 150)
        on_external = bool(car and car.charging
                           and car.charger_voltage is not None
                           and car.charger_voltage >= ext_min_v)

        # bus-live policy: when do we want the inverters energized?
        idle_min = cfg.getf("inverter_idle_shutoff_min", 30)
        last_plug = self._last_plugged_at.get(group_id, 0.0)
        if car is None:
            # No Tesla wired into this group -> default to "always available";
            # the floor / rebalance / override logic still gates per unit.
            want_bus_live = True
        elif on_external:
            want_bus_live = False
        elif plugged_now is True:
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
            elif (balance_state == BalanceState.REBALANCING and uid == weak_id):
                desired[uid] = False  # duty-cycled off to let strong recover
            else:
                desired[uid] = True

        # ── group snapshot
        gsnap = GroupSnapshot(
            group_id=group_id,
            vin=vin,
            car_name=(car.name if car else None),
            plugged_in=plugged_now,
            last_plugged_at=(last_plug or None),
            want_bus_live=want_bus_live,
            units=list(unit_ids),
            units_on=sum(1 for uid in unit_ids if desired.get(uid)),
            balance_state=balance_state,
            weak_unit=weak_id,
        )

        # Notes — most-useful single line for the dashboard.
        if not usable and unit_ids:
            gsnap.note = "no usable units (all floored/offline/override-off)"
        elif on_external:
            v = car.charger_voltage if car else None
            gsnap.note = f"car on external source ({v}V) — bus held off"
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
        if self.tesla is None:
            return
        for group_id, gsnap in snap.groups.items():
            vin = gsnap.vin
            if not vin:
                continue
            car = self.tesla.cars.get(vin)
            if car is None or car.plugged_in is False:
                # not plugged → nothing to command at the car
                continue

            any_unit_on = gsnap.units_on > 0
            want_charging = any_unit_on and (car.plugged_in is True)
            commanded = self._charging_commanded.get(group_id)
            if commanded is None and not want_charging:
                # Don't preempt a session we didn't start. The user could be
                # charging from grid, from another bus, or it's a leftover
                # from before cube-power booted — leave it alone until we
                # explicitly start it.
                continue
            if commanded == want_charging:
                continue  # no change of intent
            try:
                if want_charging:
                    ok = await self.tesla.start_charging(vin)
                else:
                    ok = await self.tesla.stop_charging(vin)
            except Exception as e:
                self.log.log("coordinator", "tesla-actuate-error",
                             group=group_id, vin=vin, want=want_charging, error=str(e))
                continue
            if ok:
                self._charging_commanded[group_id] = want_charging
                gsnap.charging_commanded = want_charging

    def _maybe_wake(self, group_id: str, gsnap: GroupSnapshot, now: float) -> None:
        """Run a bounded wake campaign when this group's bus newly goes live.

        On a fresh energize edge (or plug-in onto a hot bus) a deeply asleep
        Tesla can miss the power-on event and never start drawing. We wake
        the car a few times over a campaign window, ending early once draw
        is observed.
        """
        if self.tesla is None or not self.cfg.get("tesla_wake_enabled", True):
            return
        vin = gsnap.vin
        if not vin:
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
        asyncio.create_task(self._do_wake(vin, group_id))

    async def _do_wake(self, vin: str, group_id: str) -> None:
        try:
            await self.tesla.wake(vin)
            await asyncio.sleep(2)
            await self.tesla.start_charging(vin)
            self.log.log("coordinator", "wake-on-energize", group=group_id, vin=vin)
        except Exception as e:
            self.log.log("coordinator", "wake-error", group=group_id, vin=vin,
                         error=str(e))

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
