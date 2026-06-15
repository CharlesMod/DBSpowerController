# Adding a second Tesla (Meridith) + Anker bus

The whole system is per-`bus_group`. Group "a" today: 2× DBS → Tessa.
Group "b" planned: 1× Anker → Meridith. To bring it online:

## 1. Pair Meridith's Beetle

Same flow as Tessa's Beetle (cube/esphome-tesla-ble project):

1. Flash listener firmware to a fresh Beetle ESP32-C6 with Meridith's
   VIN as `tesla_vin` in `secrets.yaml`.
2. Move it within BLE range of Meridith; tail logs until you see the
   "Found Tesla vehicle | MAC: XX:XX:..." line.
3. Update `ble_mac_address` in `secrets.yaml` to that MAC.
4. Flash the client firmware (`tesla-ble-beetle-esp32-c6.yml`).
5. Set Meridith's static DHCP reservation (e.g. 192.168.86.23).
6. Run `pair-beetle.py` adjusted with the new IP + the same PSK; tap
   the key card on Meridith's center console.

## 2. Wire her into cube-power

In `config.yaml`, uncomment the Meridith block under `tesla_vins`:

```yaml
tesla_vins:
  - vin: "5YJ3E1EA2PF433840"
    name: "Tessa"
    beetle_host: "192.168.86.22"
    bus_group: "a"
  - vin: "<her VIN>"
    name: "Meridith"
    beetle_host: "192.168.86.23"      # whatever you reserved
    bus_group: "b"
```

Restart: `systemctl --user restart cube-power`. Coordinator will
spin up a second Beetle connection, refresh state, and the UI's
second column should turn live.

## 3. Add the Anker

Once it arrives, add the Anker as a unit in `devices.json` with
`bus_group: "b"`. Template:

```json
{
  "name": "Anker SOLIX X1",
  "id": "<tuya-device-id>",
  "key": "<local-key>",
  "version": "3.5",
  "ip": "192.168.86.XX",
  "model": "Anker_SOLIX_X1",
  "bus_group": "b",
  "max_out_w": 6000
}
```

Note: most Anker portable units are NOT on the Tuya protocol — they
typically use Anker's own cloud + BLE. If that's the case for the
unit you ordered, we'll need:

- A new poller in `cube_power/anker_poller.py` (BLE or local-API based)
- A new actuator equivalent to `tuya_actuator.py` for AC on/off
- A model definition in `dps_map.py` (or its equivalent) for the
  AC inverter relay, SoC, and solar-in DPs / characteristics

If the unit IS Tuya-compatible (some larger Anker home batteries
are), we can extend `tuya_poller.py` with the right DPS map.
