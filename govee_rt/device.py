"""Remembered devices, so commands work with no flags after the first connect.

`python -m govee_rt connect --ble` identifies the strip (model from its Bluetooth name,
firmware/hardware versions, measured segment count) and saves it as the default device in
~/.govee-rt/devices.json. Every other command then uses that device and segment count unless
--ip/--ble/--segments say otherwise.

Segment count, in order of trust: set by hand (`connect --segments N`, e.g. after `probe`),
measured by segment readback, the per-model table below, then 15.
"""

import json
import os
import re
import time

DEVICES_FILE = os.path.join(os.path.expanduser("~"), ".govee-rt", "devices.json")
DEFAULT_SEGMENTS = 15

# Only counts with a source. H612F: OpenRGB. H617A/H6199/H6102: ha-govee-led-ble profiles.
# H6099: same, 14 logical zones.
KNOWN_SEGMENTS = {"H612F": 12, "H617A": 15, "H617E": 15, "H6199": 15, "H6102": 15, "H6099": 14}


def sku_from_name(name):
    """'ihoment_H612C_8A3F' / 'Govee_H612C_8A3F' / 'GBK_H612C_...' -> 'H612C'."""
    m = re.search(r"(?<![0-9A-Z])(H[0-9][0-9A-Z]{3})(?![0-9A-Z])", (name or "").upper())
    return m.group(1) if m else None


def choose_segments(info, manual=None):
    """Return (count, source) from manual override, readback measurement, model table, default."""
    if manual:
        return int(manual), "set by hand"
    if info.get("segments_measured"):
        return int(info["segments_measured"]), "measured"
    sku = info.get("sku")
    if sku in KNOWN_SEGMENTS:
        return KNOWN_SEGMENTS[sku], f"known for {sku}"
    return DEFAULT_SEGMENTS, "default (unconfirmed; run probe and connect --segments N)"


class Devices:
    def __init__(self, path=None):
        self.path = path or DEVICES_FILE

    def _read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, d):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def all(self):
        return self._read().get("devices", {})

    def default(self):
        d = self._read()
        return d.get("devices", {}).get(d.get("default"))

    def find(self, transport, key):
        for p in self.all().values():
            if p.get("transport") == transport and key and p.get("address" if transport == "ble" else "ip") == key:
                return p
        return None

    def save(self, profile, make_default=True):
        d = self._read()
        key = profile.get("address") or profile.get("ip")
        profile = dict(profile, id=key, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        d.setdefault("devices", {})[key] = profile
        if make_default:
            d["default"] = key
        self._write(d)
        return profile


def describe(p):
    if not p:
        return "no device saved yet; run: govee-rt (or govee-rt connect --ble)"
    where = p.get("address") if p.get("transport") == "ble" else p.get("ip")
    lines = [f"{p.get('sku') or 'unknown model'} over {p.get('transport', '?').upper()} ({where})",
             f"  name        {p.get('name')}",
             f"  segments    {p.get('segments')}  ({p.get('segments_source')})"]
    for k in ("firmware", "hardware", "light_count", "mtu", "encryption_marker", "segments_note"):
        if p.get(k) is not None:
            lines.append(f"  {k.replace('_', ' '):<11} {p[k]}")
    return "\n".join(lines)
