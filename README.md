# DBSpowerController

Autonomous solar-routing scheduler for **Dabbsson DBS-series portable power
stations**. It watches one or two units over the Tuya local protocol and
switches their AC inverter output to drain stored + incoming solar energy into a
connected load (e.g. an EV charger) — without ever discharging a unit below a
configurable safety floor. Runs as a small service with a live web dashboard.

> Built and tested against two **DBS1400 Pro** units (firmware `P14PL-*`, Tuya
> protocol 3.5). Other DBS models can be added via `dps_map.py`.

## The problem it solves

A DBS power station charged by solar makes a decent "solar router": feed its AC
output into a load and you move energy through it. But the built-in scheduling
is crude — it can't track state of charge, can't coordinate two paralleled
units, and will happily either over-discharge or sit full and waste solar.

DBSpowerController replaces that with a closed loop:

- **Drain to a floor, not to empty.** Each unit feeds the load until it hits a
  configurable SoC floor (default 33%), then drops off and recharges from its
  own solar. A rehab band (default: rejoin at 40%) prevents chatter.
- **Coordinate two paralleled units.** If their AC outputs are tied onto one bus
  (via the vendor parallel kit), the controller treats them as a system: a load
  that needs both units' capacity only runs when both are healthy; a smaller
  load can run on one.
- **Balance the pair.** When two units feed a shared bus, uneven solar makes one
  drift toward the floor. The controller duty-cycles the weaker unit off so it
  recharges while the stronger one carries the load.

## Control model (Phase 1)

The load is treated as a **fixed sink** — it pulls a known wattage (`car_sink_w`,
default 1200 W) whenever it sees a live bus. The only control lever is each
unit's **AC inverter on/off** (`dp109`).

Each tick (~15 s) the coordinator:

1. classifies each unit — `NORMAL` / `FLOORED` / `OVERRIDE` / `OFFLINE`;
2. infers how many loads are connected from measured bus output;
3. applies SoC-floor eligibility with rehab hysteresis;
4. decides which units' AC to switch on:
   - **one load**: run any eligible unit; duty-cycle the weaker one off to
     balance SoC when the pair diverges;
   - **two loads**: run *both* units or neither — one unit can't safely carry a
     double load;
5. actuates over Tuya, with idempotency keyed on the last *commanded* value and
   an anti-chatter dwell.

> Closed-loop amperage control (modulating an EV's charge current to track solar
> exactly) is a planned **Phase 2**. The `dbs_controller/tesla_ble.py` module is
> present but dormant.

## Requirements

- Python 3.11+
- One or two Dabbsson DBS power stations on your LAN, paired to Tuya.
- The units reachable on TCP/6668 from the host running this.

## Install

```bash
git clone https://github.com/<you>/DBSpowerController.git
cd DBSpowerController
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Setup

### 1. Get each unit's local key

The controller talks to the units **locally**, but Tuya only hands out the
per-device `local_key` via its cloud once:

1. Pair each unit to the **Tuya Smart** app.
2. Create a free Cloud Project at <https://iot.tuya.com> (Smart Home dev method;
   subscribe it to *IoT Core* + *Authorization*), and link your app account to
   it by QR code.
3. Run the key wizard: `pip install tinytuya && python -m tinytuya wizard`.

This produces a `devices.json`. See `devices.json.example` for the shape — fill
in each unit's `id`, `key`, `ip`, protocol `version` (try `3.5`, `3.4`, `3.3`),
and `model`.

### 2. Verify the DPS map

`dps_map.py` holds each model's data-point map. The bundled `DBS1400Pro` map was
verified live. For a different model, run `python probe.py` (and
`python probe.py watch`) to discover yours, and **confirm the AC-output DP is
remotely writable** before trusting the controller.

### 3. Run

```bash
. .venv/bin/activate
python server.py            # dashboard + API on :8787
```

`config.yaml` ships with `dry_run: true` — the controller logs every decision to
`decisions.jsonl` without switching anything. Watch it for a while, then set
`dry_run: false`.

To run it as a service that survives reboot, see
`systemd/DBSpowerController.service`.

## Configuration

All tunables live in `config.yaml` and **hot-reload within ~5 s** — no restart.
Key ones: `soc_floor_pct`, `soc_rehab_band_pct`, `car_sink_w`,
`coordinator_tick_s`, the `divergence_*` balancing thresholds, and `dry_run`.

## Dashboard

`http://<host>:8787/` — live per-unit SoC / solar / AC state, the coordinator's
decision, a decision log, and per-unit manual **Force ON/OFF** override (48 h
hold, releasable).

## DBS1400 Pro data points (verified)

| DP   | Meaning                          | Notes |
|------|----------------------------------|-------|
| 1    | battery SoC %                    | Tuya-cloud confirmed |
| 10   | temperature °C                   | Tuya-cloud confirmed |
| 109  | **AC inverter output on/off**    | the control lever |
| 111  | 12 V DC output on/off            | |
| 127  | working mode string              | |
| 134  | status flag (0 = AC on)          | |
| 158  | telemetry string (AC/PV/battery) | **lags tens of seconds** |

The DBS1400 Pro exposes most data as private (undocumented) DPs. `dp158` is the
only source of live wattages and it updates slowly — fine here, since control is
gated on SoC and AC state, not on watts.

## Testing

```bash
pip install -e '.[dev]'
pytest
```

The suite covers the control logic (one-load and two-load scenarios), the SoC
floor + rehab hysteresis, the balancing state machine, role classification, and
the `dp158` parser — no hardware required.

## Safety

This software switches mains-voltage inverter output on real hardware. It is
provided **as-is, with no warranty** (see `LICENSE`). Soak-test with
`dry_run: true`, verify your DPS map, and understand your units' and load's
limits before enabling live control.

## License

MIT — see `LICENSE`.
