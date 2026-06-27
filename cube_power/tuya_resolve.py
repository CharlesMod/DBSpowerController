"""Locate a Tuya unit's current IP by its device ID when its cached IP fails.

Why this exists: the DBS units have no router-independent name. Their firmware
does not advertise mDNS, this mesh drops Tuya's UDP discovery broadcast (so
tinytuya's scanner finds nothing), and the router does not serve DNS names. The
one stable, router-independent identifier is the Tuya **device id** (in
devices.json). So when a unit's cached IP stops answering — e.g. after a router
swap hands out new DHCP leases — we sweep the local /24(s) for hosts with the
Tuya local port open and confirm identity by decrypting a status() with the
unit's key (only the right device decrypts). The found IP is written back to
devices.json so the next restart starts from the right place.
"""

from __future__ import annotations

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tinytuya

from .config import Config
from .unit import Unit

TUYA_PORT = 6668
DEVICES_PATH = Path(__file__).resolve().parent.parent / "devices.json"


def _primary_prefix() -> str | None:
    """The /24 prefix (e.g. '192.168.68.') of cube's default-route source IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks the src addr
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip.rsplit(".", 1)[0] + "."
    except OSError:
        return None


def _scan_prefixes(cfg: Config) -> list[str]:
    """Subnet prefixes to sweep. Configurable via `tuya_scan_subnets`; defaults
    to cube's own /24 (where DHCP pools nearly always land)."""
    configured = cfg.get("tuya_scan_subnets")
    if configured:
        return [p if p.endswith(".") else p + "." for p in configured]
    prefix = _primary_prefix()
    return [prefix] if prefix else []


def _port_open(ip: str, port: int, timeout: float) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((ip, port)) == 0


def _open_hosts(prefix: str, timeout: float = 0.3, workers: int = 128) -> list[str]:
    ips = [f"{prefix}{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(lambda ip: ip if _port_open(ip, TUYA_PORT, timeout) else None, ips)
    return [ip for ip in results if ip]


def _identity_match(unit: Unit, ip: str) -> bool:
    """True iff the device at `ip` answers to this unit's id+key (decrypts)."""
    d = tinytuya.Device(unit.unit_id, ip, unit.spec["key"], version=unit.version)
    d.set_socketTimeout(3)
    try:
        st = d.status()
        return isinstance(st, dict) and "dps" in st
    except Exception:
        return False
    finally:
        try:
            d.close()
        except Exception:
            pass


def resolve_unit_ip(unit: Unit, cfg: Config) -> str | None:
    """Sweep the local subnet(s) for the host that answers to this unit's Tuya
    id+key. Returns the matching IP (possibly unchanged) or None if not found."""
    for prefix in _scan_prefixes(cfg):
        # Try the cached IP first so a still-valid lease resolves instantly.
        candidates = _open_hosts(prefix)
        if unit.ip in candidates:
            candidates = [unit.ip] + [c for c in candidates if c != unit.ip]
        for ip in candidates:
            if _identity_match(unit, ip):
                return ip
    return None


def persist_unit_ip(unit_id: str, ip: str, path: Path = DEVICES_PATH) -> bool:
    """Rewrite devices.json with the unit's new IP, preserving all other fields."""
    try:
        specs = json.loads(path.read_text())
    except Exception:
        return False
    changed = False
    for s in specs:
        if s.get("id") == unit_id and s.get("ip") != ip:
            s["ip"] = ip
            changed = True
    if changed:
        path.write_text(json.dumps(specs, indent=2) + "\n")
    return changed
