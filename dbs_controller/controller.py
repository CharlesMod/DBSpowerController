"""The coordinator — the system-level brain (Phase 1: fixed-sink model).

Tesla amperage control is deferred to Phase 2. For now each car is a fixed
~1200 W sink that pulls power whenever it sees a live bus. The only control
lever is each DBS unit's AC inverter output (dp109) on/off.

Objective: drain the power stations into the connected car(s) and pass solar
through, while never discharging either unit below the 33 % SoC floor.

Each tick the coordinator:
  1. classifies both units (NORMAL / FLOORED / OVERRIDE / OFFLINE)
  2. infers how many cars are connected from measured bus output
  3. decides which units' AC should be on, honoring:
       - the SoC floor (with rehab hysteresis so it doesn't chatter)
       - the bus rule: two cars (2400 W) need both units; one unit alone
         (1200 W cap) can only serve one car
  4. duty-cycles the weaker unit off the bus to balance SoC (one-car case)
  5. drives each unit's AC via the Tuya actuator
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .bus import Bus
from .config import Config
from .decisions import DecisionLog
from .tuya_actuator import set_ac
from .types import BalanceState, CoordinatorSnapshot, UnitRole
from .unit import Unit


class Coordinator:
    def __init__(self, units: dict[str, Unit], cfg: Config, bus: Bus, log: DecisionLog):
        self.units = units
        self.cfg = cfg
        self.bus = bus
        self.log = log

        self.snapshot = CoordinatorSnapshot()
        self._stop = asyncio.Event()

        # car-count inference (persists across AC-off periods; cars rarely change)
        self._n_cars = 1
        self._n_cars_measured = False

        # SoC-floor hysteresis: once a unit floors it stays held off until rehab
        self._held_off: dict[str, bool] = {uid: False for uid in units}
        # anti-chatter: when we last commanded a unit OFF
        self._ac_off_at: dict[str, float] = {uid: 0.0 for uid in units}
        # what we last *commanded* per unit (drives idempotency, not polled
        # state — Tuya readback can lag) + when, for unconfirmed-retry
        self._ac_commanded: dict[str, bool] = {}
        self._ac_commanded_at: dict[str, float] = {}

        # balancing sub-state
        self.balance_state = BalanceState.BALANCED
        self.weak_unit_id: str | None = None
        self._balance_changed_at = 0.0
        self._diverged_since: float | None = None
        self._converged_since: float | None = None

        self._solar_hist: deque[tuple[float, float]] = deque(maxlen=240)

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
        snap = CoordinatorSnapshot(tick_at=now)

        # 1. classify
        for u in self.units.values():
            u.classify(cfg)

        # update SoC-floor hysteresis latches
        for uid, u in self.units.items():
            if u.role == UnitRole.FLOORED or u.is_hard_floored(cfg):
                self._held_off[uid] = True
            elif u.at_rehab(cfg):
                self._held_off[uid] = False

        # 2. infer car count from measured bus output
        car_w = cfg.getf("car_sink_w", 1200)
        on_units = [u for u in self.units.values()
                    if u.state.ac_on and not u.is_stale(cfg)]
        total_out = sum((u.state.ac_out_w or 0.0) for u in on_units)
        total_solar = sum((u.state.solar_in_w or 0.0) for u in self.units.values())
        if on_units:
            measured = 0 if total_out < 0.4 * car_w else round(total_out / car_w)
            measured = min(max(measured, 0), 2)
            if measured >= 1:
                self._n_cars = measured
                self._n_cars_measured = True
        need = self._n_cars if self._n_cars >= 1 else 1

        # 3. eligibility (NORMAL role + not held off by floor hysteresis)
        eligible = {
            uid: (u.role == UnitRole.NORMAL and not self._held_off.get(uid, False))
            for uid, u in self.units.items()
        }
        usable = [uid for uid, e in eligible.items() if e]

        # 4. balancing (a one-car concept — both units must be on for two cars)
        if need == 1:
            self._evaluate_balance(now)
        elif self.balance_state == BalanceState.REBALANCING:
            self.balance_state = BalanceState.BALANCED
            self.weak_unit_id = None
            self._balance_changed_at = now

        # 5. desired AC per unit
        desired: dict[str, bool] = {}
        for uid, u in self.units.items():
            if u.role == UnitRole.OVERRIDE:
                desired[uid] = bool(u.override_target)
            elif u.role == UnitRole.OFFLINE:
                continue  # cannot actuate a unit we can't see
            elif not eligible[uid]:
                desired[uid] = False
            elif need >= 2:
                # two cars need 2400 W — only run if BOTH units are usable
                desired[uid] = len(usable) >= 2
            else:  # one car
                if (self.balance_state == BalanceState.REBALANCING
                        and uid == self.weak_unit_id):
                    desired[uid] = False  # duty-cycled off to recharge
                else:
                    desired[uid] = True

        # 6. actuate. Idempotency is keyed on the last *commanded* value, not on
        # polled ac_on (Tuya readback lags). A command is re-issued only on a
        # change of intent, or once if it stays unconfirmed past a timeout.
        min_dwell = cfg.getf("min_ac_dwell_s", 300)
        confirm_timeout = cfg.getf("ac_confirm_timeout_s", 90)
        for uid, want in desired.items():
            u = self.units[uid]
            commanded = self._ac_commanded.get(uid)
            # seed the tracker from reality so a fresh start issues no no-op write
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
                    continue  # already commanded, nothing to do
            if want and (now - self._ac_off_at.get(uid, 0.0) < min_dwell):
                continue  # too soon after the last turn-off
            await set_ac(u, want, f"coordinator/need={need}", cfg, self.log)
            self._ac_commanded[uid] = want
            self._ac_commanded_at[uid] = now
            if not want:
                self._ac_off_at[uid] = now

        # 7. snapshot
        snap.n_cars = need
        snap.n_cars_measured = self._n_cars_measured
        snap.units_on = sum(1 for v in desired.values() if v)
        snap.total_solar_w = round(total_solar, 1)
        snap.total_ac_out_w = round(total_out, 1)
        snap.balance_state = self.balance_state
        snap.weak_unit = self.weak_unit_id
        snap.desired_ac = desired
        snap.actuator_ready = any("ac_on" in u.dps_map.values()
                                  for u in self.units.values())
        if need >= 2 and snap.units_on < 2:
            snap.note = "two cars need both units — holding off"
        elif snap.units_on == 0:
            snap.note = "no units feeding the bus"
        self.snapshot = snap
        self.bus.publish({"type": "coordinator", "snapshot": _snap_dict(snap)})

    # ────────────────────────────────────────────────────────────
    # balancing
    # ────────────────────────────────────────────────────────────

    def _evaluate_balance(self, now: float) -> None:
        pair = [u for u in self.units.values()
                if u.state.soc_pct is not None and not u.is_stale(self.cfg)]
        if len(pair) < 2:
            return

        pair.sort(key=lambda u: u.state.soc_pct or 0.0)
        weak, strong = pair[0], pair[-1]
        delta = (strong.state.soc_pct or 0) - (weak.state.soc_pct or 0)

        trigger = self.cfg.getf("divergence_trigger_pct", 15)
        clear = self.cfg.getf("divergence_clear_pct", 5)
        floor = self.cfg.getf("soc_floor_pct", 33)
        proximity = self.cfg.getf("floor_proximity_pct", 5)
        min_dwell = self.cfg.getf("rebalance_min_dwell_min", 20) * 60
        dwell_ok = now - self._balance_changed_at >= min_dwell
        weak_offline = weak.role == UnitRole.OFFLINE

        if self.balance_state == BalanceState.BALANCED:
            want = ((delta >= trigger)
                    or ((weak.state.soc_pct or 999) <= floor + proximity)) \
                and not weak_offline
            self._diverged_since = (self._diverged_since or now) if want else None
            persist = self.cfg.getf("divergence_persist_min", 10) * 60
            if (self._diverged_since and now - self._diverged_since >= persist
                    and dwell_ok):
                self.balance_state = BalanceState.REBALANCING
                self.weak_unit_id = weak.unit_id
                self._balance_changed_at = now
                self._diverged_since = None
                self.log.log("coordinator", "rebalance-enter",
                              weak=weak.name, strong=strong.name,
                              delta_soc=round(delta, 1))
        else:  # REBALANCING
            recovered = self.cfg.getf("weak_recovered_pct", 85)
            max_dur = self.cfg.getf("rebalance_max_duration_min", 90) * 60
            weak_unit = self.units.get(self.weak_unit_id or "")
            weak_soc = weak_unit.state.soc_pct if weak_unit else None

            converged = delta <= clear
            self._converged_since = (self._converged_since or now) if converged else None
            clear_persist = self.cfg.getf("divergence_clear_persist_min", 5) * 60
            exit_now = (
                (self._converged_since and now - self._converged_since >= clear_persist)
                or (weak_soc is not None and weak_soc >= recovered)
                or (now - self._balance_changed_at >= max_dur)
            )
            if exit_now and dwell_ok:
                cause = ("converged" if self._converged_since
                         else "recovered" if (weak_soc or 0) >= recovered
                         else "max-duration")
                self.log.log("coordinator", f"rebalance-exit: {cause}",
                              delta_soc=round(delta, 1))
                self.balance_state = BalanceState.BALANCED
                self.weak_unit_id = None
                self._balance_changed_at = now
                self._converged_since = None

    def state_dict(self) -> dict:
        return _snap_dict(self.snapshot)


def _snap_dict(s: CoordinatorSnapshot) -> dict:
    from dataclasses import asdict

    return asdict(s)
