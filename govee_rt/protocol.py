"""Govee LAN API (UDP) and the real-time "razer" frame format. See docs/PROTOCOL.md."""

import base64
import json
import socket
import sys
import time

MCAST_GRP = "239.255.255.250"
PORT_SCAN = 4001      # devices listen here for "scan"
PORT_REPLY = 4002     # devices answer here
PORT_CMD = 4003       # devices accept commands here


# --------------------------------------------------------------------------- protocol

def _xor(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def razer_frame(subcmd: int, data: bytes) -> bytes:
    pkt = bytes([0xBB, (len(data) >> 8) & 0xFF, len(data) & 0xFF, subcmd]) + data
    return pkt + bytes([_xor(pkt)])


def razer_json(pt: bytes) -> bytes:
    msg = {"msg": {"cmd": "razer", "data": {"pt": base64.b64encode(pt).decode()}}}
    return json.dumps(msg, separators=(",", ":")).encode() + b"\x00"


def frame_enable() -> bytes:
    return razer_json(razer_frame(0xB1, b"\x01"))


def frame_disable() -> bytes:
    return razer_json(razer_frame(0xB1, b"\x00"))


def razer_colors(colors) -> bytes:
    """Raw B0 frame. colors: iterable of (r, g, b) ints 0..255, one per segment, max 255."""
    colors = list(colors)[:255]
    data = bytes([0x01, len(colors)])  # 0x01 = gradient off (hard edges between segments)
    data += b"".join(bytes((int(r) & 255, int(g) & 255, int(b) & 255)) for r, g, b in colors)
    return razer_frame(0xB0, data)


def frame_colors(colors) -> bytes:
    """colors: iterable of (r, g, b) ints 0..255, one per segment, max 255 segments."""
    return razer_json(razer_colors(colors))


def lan_json(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode() + b"\x00"


class GoveeLan:
    """One UDP socket per light. All sends are fire-and-forget datagrams."""

    def __init__(self, ip: str):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._last_enable = 0.0
        self.reverse = False  # segment 1 at the far end

    def _send(self, payload: bytes):
        self.sock.sendto(payload, (self.ip, PORT_CMD))

    # official LAN API
    def power(self, on: bool):
        self._send(lan_json("turn", {"value": 1 if on else 0}))

    def brightness(self, pct: int):
        self._send(lan_json("brightness", {"value": max(1, min(100, int(pct)))}))

    def color(self, r, g, b):
        self._send(lan_json("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}))

    # real-time mode
    def rt_enable(self):
        self._send(frame_enable())
        self._last_enable = time.monotonic()

    def rt_disable(self):
        self._send(frame_disable())

    def rt_frame(self, colors):
        # the device drops out of real-time mode after ~60 s without an enable; re-assert every 20 s
        if time.monotonic() - self._last_enable > 20:
            self.rt_enable()
        colors = list(colors)
        self._send(frame_colors(colors[::-1] if self.reverse else colors))

    def close(self):
        self.sock.close()


def scan(timeout: float = 3.0, targets=None):
    """Multicast a scan and collect replies on :4002. targets = extra unicast IPs/broadcasts."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    rx.bind(("", PORT_REPLY))
    rx.settimeout(0.5)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    msg = lan_json("scan", {"account_topic": "reserve"})
    for dst in [MCAST_GRP, "255.255.255.255"] + list(targets or []):
        try:
            tx.sendto(msg, (dst, PORT_SCAN))
        except OSError as e:
            print(f"  (could not send to {dst}: {e})", file=sys.stderr)

    found = {}
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        try:
            raw, addr = rx.recvfrom(4096)
        except socket.timeout:
            continue
        try:
            d = json.loads(raw.rstrip(b"\x00").decode(errors="replace"))["msg"]["data"]
        except Exception:
            continue
        found[d.get("ip", addr[0])] = d
    rx.close()
    tx.close()
    return found
