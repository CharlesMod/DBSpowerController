"""
Phase 2 probe: discover the DPS map for each device.

Usage:
    python probe.py            # snapshot once
    python probe.py watch      # loop, print on change

Run snapshots under known conditions (AC off, AC on, under load, solar
present) and diff to identify each DP. Update dps_map.py accordingly.
"""

import json
import sys
import time
import tinytuya

DEVICES_PATH = "devices.json"


def open_device(unit):
    d = tinytuya.Device(
        unit["id"], unit["ip"], unit["key"], version=float(unit["version"])
    )
    d.set_socketTimeout(5)
    return d


def snapshot(unit):
    d = open_device(unit)
    status = d.status()
    avail = d.detect_available_dps()
    return {"status": status, "available": avail}


def main():
    units = json.load(open(DEVICES_PATH))
    watch = len(sys.argv) > 1 and sys.argv[1] == "watch"
    last = {}
    while True:
        for u in units:
            try:
                snap = snapshot(u)
            except Exception as e:
                print(f"[{u['name']}] error: {e}")
                continue
            key = u["name"]
            if not watch:
                print(f"\n=== {key} ({u['ip']}) ===")
                print(json.dumps(snap, indent=2, default=str))
            else:
                cur = snap.get("status", {}).get("dps", {})
                prev = last.get(key, {})
                diff = {k: (prev.get(k), v) for k, v in cur.items() if prev.get(k) != v}
                if diff:
                    print(f"[{time.strftime('%H:%M:%S')}] {key}: {diff}")
                last[key] = cur
        if not watch:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
