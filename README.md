# cube-power

Solar-tracking Tesla charge controller for two Dabbsson DBS1400 Pro power stations.
Runs on `cube`. Reads the units over the Tuya local protocol (TCP/6668), controls
Tesla charge amperage over local BLE, and serves a dashboard on `:8787`.

The units' AC outputs are tied by a parallel box into one bus feeding two 120 V
Tesla chargers. AC stays **on** all day; the continuous control lever is Tesla
charge amperage. A system-level coordinator sizes total car draw to available
solar, holds each unit above a 33 % SoC floor, and duty-cycles the weaker unit off
the bus to keep the two batteries balanced.

See `~/.claude/plans/do-you-recall-us-delightful-river.md` for the full design.

## Layout

```
cube_power/            # the service package
  types.py             # dataclasses + enums
  config.py  bus.py    # hot-reloaded config; in-memory pub/sub
  tuya_poller.py       # persistent Tuya poller -> DeviceState
  tuya_actuator.py     # the only write: AC inverter on/off
  unit.py              # per-unit sensor + safety (SoC floor, dusk, override)
  tesla_ble.py         # wrapper around the `tesla-control` binary
  controller.py        # the coordinator: solar->amps + charge balancing
  pvwatts.py           # NREL PVWatts diagnostic (modeled vs actual)
  app.py               # FastAPI app, task wiring, dashboard
server.py              # thin entrypoint (uvicorn cube_power.app:app)
probe.py  dps_map.py   # DPS discovery tool + model->DPS map
config.yaml            # all tunables (hot-reloaded ~5 s)
devices.json           # Tuya local keys + IPs (gitignored)
```

## Bootstrap

### 1. Python env

```bash
sudo apt install -y python3-pip python3-venv     # if missing
cd ~/cube-power
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

### 2. Pair the DBS units to Tuya (Phase 0)

Pair each unit to the **Tuya Smart** app (not the Dabbsson app — they become
mutually exclusive), then run `python -m tinytuya wizard` from any machine to
obtain device IDs + local keys. Copy the wizard's `devices.json` to
`~/cube-power/devices.json` and fill in `ip` + `model` per the
`devices.json.example` template.

### 3. Pair cube to the Teslas over BLE (Phase 0)

Install the `tesla-control` binary (from `teslamotors/vehicle-command`). Generate
a key pair and enroll the public key on each car with the least-privilege role:

```bash
tesla-control -ble -vin <VIN> add-key-request tesla_public_key.pem charging_manager cloud_key
# then tap an NFC key card to the car when prompted
```

Point `config.yaml` at the private key (`tesla_key_file`) and list each car under
`tesla_vins`. cube needs a Bluetooth radio within range of the parked cars.

### 4. Verify the DPS map (Phase 1)

The DBS1400 Pro DPS map in `dps_map.py` is an unverified guess. Run `probe.py`
under known conditions (AC off/on, loaded, sunlit) and correct `dps_map.py`.
Critically confirm `ac_on` is remotely writable and `soc_pct` is a 0–100 scale.

```bash
python probe.py            # one-shot DPS snapshot
python probe.py watch      # live diff
```

## Run

```bash
. .venv/bin/activate
python server.py           # foreground, :8787
```

Or as a systemd service:

```bash
sudo cp systemd/cube-power.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cube-power
journalctl -u cube-power -f
```

Dashboard: `http://<cube-tailnet-ip>:8787/`.

## Operation

- **Two safety gates in `config.yaml`:** `dry_run` (Tuya AC writes) and
  `tesla_dry_run` (Tesla BLE commands). Both default `true` — the service logs
  every decision to `decisions.jsonl` without acting. Soak, eyeball the log, then
  flip them off one at a time (Tesla writes first, on one car).
- `config.yaml` hot-reloads within ~5 s; no restart needed.
- **Manual override** per unit via the dashboard "Force ON/OFF" buttons (48 h
  hold; "Release" returns control to the coordinator).
- The coordinator only ever sizes the budget from units it can see feeding the
  bus, so it is safe to run while a unit is offline.
