"""Wake + force-poll BOTH cars, then read fresh per-car charge detail.
Identifies which car is drawing and the other's real amp/voltage/limit."""
import asyncio
import re
from pathlib import Path

from aioesphomeapi import APIClient

HOST = "tesla-ble-07a398.local"
SECRETS = Path("/home/cmod/esphome-tesla-ble/secrets.yaml")


def load_psk():
    return re.search(r'^api_encryption_key:\s*"([^"]+)"', SECRETS.read_text(), re.M).group(1)


async def main():
    cli = APIClient(HOST, 6053, "", noise_psk=load_psk())
    await cli.connect(login=True)
    entities, _ = await cli.list_entities_services()
    obj = {e.object_id: e.key for e in entities}
    states = {}
    cli.subscribe_states(lambda s: states.__setitem__(s.key, getattr(s, "state", None)))

    # wake + force update both
    for car in ("tessa", "meridith"):
        for btn in (f"{car}_wake_up", f"{car}_force_data_update"):
            if btn in obj:
                cli.button_command(obj[btn])
                await asyncio.sleep(0.4)
    await asyncio.sleep(6)  # let fresh data stream in

    for car in ("tessa", "meridith"):
        print(f"--- {car} ---")
        for f in ("asleep", "charging", "charger_voltage", "charger_current",
                  "charging_amps", "charging_limit", "battery", "ble_connection"):
            k = obj.get(f"{car}_{f}")
            print(f"   {f:16s} = {states.get(k)!r}")
    await cli.disconnect()


asyncio.run(main())
