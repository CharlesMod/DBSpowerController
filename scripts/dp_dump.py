import json
import tinytuya

devs = json.load(open("devices.json"))
for u in devs:
    name, ip = u["name"], u["ip"]
    try:
        d = tinytuya.Device(u["id"], ip, u["key"], version=float(u["version"]))
        d.set_socketTimeout(6)
        st = d.status()
        dps = st.get("dps", st) if isinstance(st, dict) else st
        print("=== {} ({}) ===".format(name, ip))
        if not isinstance(dps, dict):
            print("  unexpected:", st)
            continue
        for k in sorted(dps, key=lambda x: int(x)):
            v = dps[k]
            if isinstance(v, str) and len(v) > 70:
                v = v[:70] + "..."
            print("  dp{}: {!r}".format(k, v))
        try:
            d.close()
        except Exception:
            pass
    except Exception as e:
        print("=== {} ERROR: {} ===".format(name, e))
