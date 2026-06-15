"""Read-only snapshot of the Beetle's diagnostic entities (RSSI, BLE
connection, key charge state) for both cars. Does not send commands."""
import asyncio
import re
from pathlib import Path

from aioesphomeapi import APIClient

HOST = "192.168.86.22"
SECRETS = Path("/home/cmod/esphome-tesla-ble/secrets.yaml")


def load_psk():
    m = re.search(r'^api_encryption_key:\s*"([^"]+)"', SECRETS.read_text(), re.M)
    return m.group(1)


async def main():
    cli = APIClient(HOST, 6053, "", noise_psk=load_psk())
    await cli.connect(login=True)
    entities, _ = await cli.list_entities_services()
    by_key = {e.key: e for e in entities}

    states = {}
    done = asyncio.Event()

    def on_state(s):
        states[s.key] = getattr(s, "state", None)

    cli.subscribe_states(on_state)
    await asyncio.sleep(4)  # let states stream in

    rows = []
    for e in entities:
        oid = getattr(e, "object_id", "")
        if any(t in oid for t in ("ble", "signal", "connect", "charg", "asleep", "battery", "voltage")):
            rows.append((oid, states.get(e.key)))
    for oid, val in sorted(rows):
        print(f"  {oid:42s} = {val!r}")
    await cli.disconnect()


asyncio.run(main())
