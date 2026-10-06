"""
Fake Govee LAN device for developing without the real strip.

It behaves like a strip with LAN Control enabled:
  * joins multicast 239.255.255.250 and answers "scan" on :4001 with a reply to the sender's :4002
  * accepts commands on :4003, decodes official JSON commands and razer/pt frames,
    validates the frame checksum and length, and keeps counters

Run standalone:        python tests/fake_govee.py            (prints counters every 2 s)
Use from tests/code:   dev = FakeGovee(); dev.start(); ...; dev.stop()
"""
import base64
import collections
import json
import socket
import struct
import sys
import threading
import time

MCAST_GRP = "239.255.255.250"


def xor(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


class FakeGovee:
    def __init__(self, ip="127.0.0.1", sku="H612C", segments=15):
        self.ip = ip
        self.sku = sku
        self.segments = segments
        self.stats = collections.Counter()
        self.last_frame = None          # list of (r, g, b) from the last valid B0 frame
        self.frame_counts = []          # N from every B0 frame, in order
        self.rt_mode = False
        self.state = {"on": 0, "brightness": 100, "color": (0, 0, 0)}
        self.errors = []
        self._stop = threading.Event()
        self._threads = []

    # ---- lifecycle
    def start(self):
        self._scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._scan_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self._scan_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._scan_sock.bind(("", 4001))
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton("0.0.0.0"))
            self._scan_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:  # multicast may be unavailable in sandboxes; unicast still works
            self.errors.append(f"multicast join failed: {e}")
        self._scan_sock.settimeout(0.2)

        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd_sock.bind(("", 4003))
        self._cmd_sock.settimeout(0.2)

        for fn in (self._scan_loop, self._cmd_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1)
        self._scan_sock.close()
        self._cmd_sock.close()

    # ---- listeners
    def _scan_loop(self):
        while not self._stop.is_set():
            try:
                raw, addr = self._scan_sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                msg = json.loads(raw.rstrip(b"\x00"))
            except ValueError:
                self.errors.append(f"bad scan json from {addr}")
                continue
            if msg.get("msg", {}).get("cmd") != "scan":
                continue
            self.stats["scan"] += 1
            reply = {"msg": {"cmd": "scan", "data": {
                "ip": self.ip, "device": "AA:BB:CC:DD:EE:FF:00:11", "sku": self.sku,
                "bleVersionHard": "3.01.01", "bleVersionSoft": "1.00.11",
                "wifiVersionHard": "1.00.10", "wifiVersionSoft": "1.02.20"}}}
            r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            r.sendto(json.dumps(reply).encode(), (addr[0], 4002))
            r.close()

    def _cmd_loop(self):
        while not self._stop.is_set():
            try:
                raw, addr = self._cmd_sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                msg = json.loads(raw.rstrip(b"\x00"))["msg"]
            except (ValueError, KeyError):
                self.errors.append("bad command json")
                continue
            cmd, data = msg.get("cmd"), msg.get("data", {})
            self.stats[cmd] += 1
            if cmd == "turn":
                self.state["on"] = data.get("value")
            elif cmd == "brightness":
                self.state["brightness"] = data.get("value")
            elif cmd == "colorwc":
                c = data.get("color", {})
                self.state["color"] = (c.get("r"), c.get("g"), c.get("b"))
            elif cmd == "devStatus":
                reply = {"msg": {"cmd": "devStatus", "data": {
                    "onOff": self.state["on"], "brightness": self.state["brightness"],
                    "color": dict(zip("rgb", self.state["color"])), "colorTemInKelvin": 0}}}
                r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                r.sendto(json.dumps(reply).encode(), (addr[0], 4002))
                r.close()
            elif cmd == "razer":
                self._razer(data.get("pt", ""))

    def _razer(self, pt_b64):
        try:
            pt = base64.b64decode(pt_b64)
        except Exception:
            self.stats["razer_bad"] += 1
            return
        if len(pt) < 5 or pt[0] != 0xBB or xor(pt[:-1]) != pt[-1]:
            self.stats["razer_bad"] += 1
            self.errors.append(f"bad razer frame {pt.hex()}")
            return
        length = (pt[1] << 8) | pt[2]
        sub, body = pt[3], pt[4:-1]
        if len(body) != length:
            self.stats["razer_bad"] += 1
            self.errors.append(f"length mismatch {pt.hex()}")
            return
        if sub == 0xB1:
            self.rt_mode = body[0] == 1
            self.stats["rt_enable" if self.rt_mode else "rt_disable"] += 1
        elif sub == 0xB0:
            n = body[1]
            if len(body) != 2 + 3 * n:
                self.stats["razer_bad"] += 1
                self.errors.append(f"B0 body length mismatch n={n} {pt.hex()}")
                return
            self.stats["frames"] += 1
            self.frame_counts.append(n)
            self.last_frame = [tuple(body[2 + 3 * i: 5 + 3 * i]) for i in range(n)]
        else:
            self.stats[f"razer_sub_{sub:02x}"] += 1


if __name__ == "__main__":
    dev = FakeGovee().start()
    print("fake govee up on 127.0.0.1 (scan :4001 -> reply :4002, commands :4003)")
    t0 = time.time()
    try:
        while True:
            time.sleep(2)
            print(f"[{time.time() - t0:5.1f}s] {dict(dev.stats)} "
                  f"counts={sorted(set(dev.frame_counts[-200:]))} rt={dev.rt_mode}", flush=True)
            for e in dev.errors[-3:]:
                print("   ", e, file=sys.stderr)
    except KeyboardInterrupt:
        dev.stop()
