"""ISOLATED AC-cycle experiment.

Question: does cycling dp109 (AC relay) off->on, with NO BLE wake/start/amps,
recover charging — or does it strand the cars?

Run with cube-power STOPPED so only this script touches the relay. Measures
dp158 AC-output (ground truth for power flow) before, during, and after.
"""
import json
import re
import time

import tinytuya

DEVS = json.load(open("devices.json"))


def conn(u):
    d = tinytuya.Device(u["id"], u["ip"], u["key"], version=float(u["version"]))
    d.set_socketTimeout(6)
    return d


def watts():
    total = 0
    for u in DEVS:
        try:
            dps = conn(u).status().get("dps", {})
            for l in dps.get("158", "").split("\n"):
                if "AC输出" in l:
                    m = re.search(r"(\d+)W", l)
                    if m:
                        total += int(m.group(1))
        except Exception:
            pass
    return total


def set_ac(on):
    for u in DEVS:
        try:
            d = conn(u)
            d.set_value(109, on)
            d.close()
        except Exception as e:
            print("  set_ac error", u["name"], e)


def stamp(label):
    print(f"  t+{int(time.time()-T0):>3}s  {watts():>5}W   {label}", flush=True)


T0 = time.time()
print("=== ISOLATED AC-CYCLE EXPERIMENT (no BLE actuation) ===", flush=True)
stamp("BASELINE (cars charging?)")
print("  -> dp109 OFF both units", flush=True)
set_ac(False)
time.sleep(5)
stamp("after OFF (should drop to ~0)")
print("  -> holding off 25s", flush=True)
time.sleep(25)
print("  -> dp109 ON both units", flush=True)
set_ac(True)
time.sleep(5)
stamp("just after ON")
# Watch for recovery for ~3 min
for _ in range(12):
    time.sleep(15)
    stamp("recovery watch")
print("=== DONE — if watts climbed back to baseline, the cycle ALONE recovered "
      "charging; if it stayed ~0, the cycle strands the cars ===", flush=True)
