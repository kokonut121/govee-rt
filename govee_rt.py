#!/usr/bin/env python3
"""
govee_rt.py  --  real-time, per-segment control of Govee Wi-Fi lights over the LAN,
                 plus an audio-reactive streamer that listens to whatever your computer
                 is playing (Spotify, YouTube, anything).

Transport: Govee's LAN API (UDP) and the undocumented "razer" / DreamView real-time
mode that Govee Desktop + Razer Chroma use.  Same framing OpenRGB uses:

    pt = BB <len_hi> <len_lo> <subcmd> <data...> <xor of all preceding bytes>
    wrapped as  {"msg":{"cmd":"razer","data":{"pt":"<base64(pt)>"}}}  -> UDP :4003

    subcmd B1 01      enter real-time mode (times out after ~1 min of silence)
    subcmd B1 00      leave real-time mode
    subcmd B0 01 N (r g b)*N   set N segments, no firmware fade

Prereq: in the Govee app, open the strip -> settings -> turn on "LAN Control".

Usage
    python govee_rt.py scan                               # find lights, print IP + SKU
    python govee_rt.py solid  --ip IP --rgb 255 40 0      # sanity check (official LAN cmd)
    python govee_rt.py probe  --ip IP                     # find how many segments it accepts
    python govee_rt.py chase  --ip IP --segments 15       # real-time mode sanity check
    python govee_rt.py devices                            # list audio input devices
    python govee_rt.py audio  --ip IP --segments 15 --device "BlackHole 2ch" --mode spectrum
    python govee_rt.py audio  --ip IP --segments 15 --wav song.wav --preview   # test from a file

Deps: numpy; sounddevice only for live audio (pip install numpy sounddevice).
"""

import argparse
import base64
import colorsys
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time
import wave

import numpy as np

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


def frame_colors(colors) -> bytes:
    """colors: iterable of (r, g, b) ints 0..255, one per segment, max 255 segments."""
    colors = list(colors)[:255]
    data = bytes([0x01, len(colors)])  # 0x01 = gradient off (hard edges between segments)
    data += b"".join(bytes((int(r) & 255, int(g) & 255, int(b) & 255)) for r, g, b in colors)
    return razer_json(razer_frame(0xB0, data))


def lan_json(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}, separators=(",", ":")).encode() + b"\x00"


class GoveeLan:
    """One UDP socket per light. All sends are fire-and-forget datagrams."""

    def __init__(self, ip: str):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._last_enable = 0.0

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
        self._send(frame_colors(colors))


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


# --------------------------------------------------------------------------- color helpers

def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def gamma(v, g=2.2):
    return max(0.0, min(1.0, v)) ** g


# --------------------------------------------------------------------------- audio analysis

class Analyzer:
    """
    Turns raw audio blocks into per-segment features:
      bands[]  : log-spaced spectrum, AGC'd and envelope-followed, 0..1, len == n_segments
      bass     : 0..1 low-frequency level (40..150 Hz), envelope-followed
      beat     : True on the block where an onset was detected (spectral flux on the bass band)
      beat_env : 1.0 at a beat, decays exponentially (use for flashes)
    """

    def __init__(self, sr, n_bands, block, f_lo=40.0, f_hi=14000.0):
        self.sr = sr
        self.n = n_bands
        self.block = block
        self.window = np.hanning(block).astype(np.float32)
        freqs = np.fft.rfftfreq(block, 1.0 / sr)
        edges = np.geomspace(f_lo, f_hi, n_bands + 1)
        self.bins = [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(n_bands)]
        # make sure every band has at least one bin (low bands at small FFT sizes)
        for i, idx in enumerate(self.bins):
            if len(idx) == 0:
                self.bins[i] = np.array([min(len(freqs) - 1, int(edges[i] / (sr / block)))])
        self.bass_bins = np.where((freqs >= 40) & (freqs < 150))[0]

        dt = block / sr
        self.att = 1 - math.exp(-dt / 0.012)   # ~12 ms attack
        self.rel = 1 - math.exp(-dt / 0.180)   # ~180 ms release
        self.agc_rel = 1 - math.exp(-dt / 4.0)  # AGC ceiling falls over ~4 s
        self.env = np.zeros(n_bands, dtype=np.float32)
        self.ceiling = np.full(n_bands, 1e-3, dtype=np.float32)

        self.bass_env = 0.0
        self.bass_ceiling = 1e-3
        self.prev_bass = 0.0
        self.flux_hist = []
        self.flux_hist_len = int(1.5 / dt)
        self.last_beat = -1.0
        self.beat_env = 0.0
        self.beat_decay = math.exp(-dt / 0.25)
        self.t = 0.0
        self.silence = 1e-4

    def process(self, samples: np.ndarray):
        """samples: float32 mono block of length self.block, range -1..1."""
        self.t += self.block / self.sr
        x = samples * self.window
        mag = np.abs(np.fft.rfft(x)).astype(np.float32)
        mag = mag * (2.0 / self.block)

        raw = np.array([mag[idx].mean() for idx in self.bins], dtype=np.float32)
        raw = np.sqrt(raw)  # compress dynamic range a bit before AGC

        # per-band AGC: track a slowly falling ceiling, normalize against it
        self.ceiling = np.maximum(raw, self.ceiling * (1 - self.agc_rel))
        self.ceiling = np.maximum(self.ceiling, self.silence)
        norm = raw / self.ceiling
        # attack/release envelope so the lights don't flicker between FFT frames
        up = norm > self.env
        self.env = np.where(up, self.env + (norm - self.env) * self.att,
                            self.env + (norm - self.env) * self.rel)

        # bass + onset detection
        bass_raw = float(np.sqrt(mag[self.bass_bins].mean())) if len(self.bass_bins) else 0.0
        self.bass_ceiling = max(bass_raw, self.bass_ceiling * (1 - self.agc_rel), self.silence)
        bass_norm = bass_raw / self.bass_ceiling
        k = self.att if bass_norm > self.bass_env else self.rel
        self.bass_env += (bass_norm - self.bass_env) * k

        flux = max(0.0, bass_raw - self.prev_bass)
        self.prev_bass = bass_raw
        self.flux_hist.append(flux)
        if len(self.flux_hist) > self.flux_hist_len:
            self.flux_hist.pop(0)
        h = np.array(self.flux_hist)
        thresh = h.mean() + 1.5 * h.std() if len(h) > 8 else float("inf")
        beat = (flux > thresh and bass_raw > 0.15 * self.bass_ceiling
                and (self.t - self.last_beat) > 0.25 and bass_raw > 3 * self.silence)
        if beat:
            self.last_beat = self.t
            self.beat_env = 1.0
        else:
            self.beat_env *= self.beat_decay

        return {
            "bands": np.clip(self.env, 0, 1),
            "bass": max(0.0, min(1.0, self.bass_env)),
            "beat": beat,
            "beat_env": self.beat_env,
        }


# --------------------------------------------------------------------------- visual modes

class Visualizer:
    def __init__(self, n_segments, mode="spectrum", hue=0.0, hue_span=0.75):
        self.n = n_segments
        self.mode = mode
        self.hue = hue
        self.hue_span = hue_span
        self.beat_count = 0
        self.phase = 0.0

    def render(self, f):
        n = self.n
        if f["beat"]:
            self.beat_count += 1
        bands = f["bands"]

        if self.mode == "spectrum":
            # each segment is a frequency band, low (warm) at one end, high (cool) at the other
            # bass end gets a per-beat flash so the kick is visible even in a dense mix
            out = []
            for i in range(n):
                h = self.hue + self.hue_span * i / max(1, n - 1)
                v = bands[i] ** 1.4
                if i < max(1, n // 6):
                    v = max(v, f["beat_env"])
                out.append(hsv(h, 1.0, gamma(v, 1.8)))
            return out

        if self.mode == "pulse":
            # whole strip one color; hue steps every beat, brightness follows bass + beat flash
            h = self.hue + 0.137 * self.beat_count
            v = 0.15 + 0.85 * max(f["bass"] ** 1.5, f["beat_env"])
            c = hsv(h, 1.0, gamma(v, 1.6))
            return [c] * n

        if self.mode == "vu":
            # bass level fills outward from the center; hue drifts slowly, flashes white-ish on beat
            self.phase += 0.0015
            lvl = max(f["bass"] ** 1.3, 0.6 * f["beat_env"])
            half = n / 2.0
            out = []
            for i in range(n):
                d = abs(i + 0.5 - half) / half  # 0 at center .. 1 at ends
                v = max(0.0, min(1.0, (lvl - d) * 4 + 0.5)) if lvl > 0.02 else 0.0
                s = 1.0 - 0.6 * f["beat_env"]
                out.append(hsv(self.hue + self.phase + 0.15 * d, s, gamma(v, 1.5)))
            return out

        if self.mode == "wave":
            # a bass-driven color wave that scrolls along the strip
            self.phase += 0.02 + 0.08 * f["bass"]
            out = []
            for i in range(n):
                p = i / max(1, n - 1)
                v = 0.5 + 0.5 * math.sin(2 * math.pi * (p * 1.5 - self.phase))
                v = v * (0.25 + 0.75 * f["bass"]) + f["beat_env"] * 0.4
                out.append(hsv(self.hue + 0.3 * p + 0.05 * self.beat_count, 1.0, gamma(min(1, v), 1.5)))
            return out

        raise ValueError(f"unknown mode {self.mode}")


# --------------------------------------------------------------------------- audio sources

def wav_blocks(path, block):
    """Yield (sr, float32 mono block) from a 16/24/32-bit PCM wav in real time."""
    w = wave.open(path, "rb")
    sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sw)
    scale = float(2 ** (8 * sw - 1))
    t0 = time.monotonic()
    i = 0
    while True:
        raw = w.readframes(block)
        if len(raw) < block * ch * sw:
            break
        if sw == 3:
            a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            x = (a[:, 0].astype(np.int32) | (a[:, 1].astype(np.int32) << 8)
                 | (a[:, 2].astype(np.int8).astype(np.int32) << 16)).astype(np.float32) / scale
        else:
            x = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
        x = x.reshape(-1, ch).mean(axis=1)
        i += 1
        # pace to real time
        target = t0 + i * block / sr
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        yield sr, x


def live_blocks(device, block, sr=None):
    import sounddevice as sd
    info = sd.query_devices(device, "input")
    sr = int(sr or info["default_samplerate"])
    ch = min(2, int(info["max_input_channels"]))
    q = queue.Queue(maxsize=8)

    def cb(indata, frames, t, status):
        if status:
            print(f"  audio: {status}", file=sys.stderr)
        try:
            q.put_nowait(indata.mean(axis=1).astype(np.float32).copy())
        except queue.Full:
            pass

    with sd.InputStream(device=device, channels=ch, samplerate=sr, blocksize=block,
                        dtype="float32", callback=cb):
        while True:
            yield sr, q.get()


def preview_line(colors, f):
    ramp = " .:-=+*#%@"
    bar = "".join(ramp[min(9, int(sum(c) / 765 * 9.99))] for c in colors)
    flag = "BEAT" if f["beat"] else "    "
    return f"\r{flag} bass={f['bass']:.2f} |{bar}|"


# --------------------------------------------------------------------------- commands

def cmd_scan(a):
    print("scanning (multicast 239.255.255.250:4001, replies on :4002)...")
    found = scan(a.timeout, a.target)
    if not found:
        print("nothing answered. Check: LAN Control toggled on in the Govee app; same Wi-Fi/VLAN as "
              "the strip; firewall allows inbound UDP 4002; or pass --target <strip-ip>.")
        return
    for ip, d in found.items():
        print(f"  {ip:15s}  sku={d.get('sku')}  device={d.get('device')}  "
              f"wifi_fw={d.get('wifiVersionSoft')}  ble_fw={d.get('bleVersionSoft')}")


def cmd_solid(a):
    g = GoveeLan(a.ip)
    g.power(True)
    g.brightness(a.brightness)
    g.color(*a.rgb)
    print(f"sent turn/brightness/colorwc to {a.ip}")


def cmd_probe(a):
    """Send rainbow frames with increasing segment counts; watch which N gives the finest bands."""
    g = GoveeLan(a.ip)
    g.power(True)
    g.brightness(a.brightness)
    g.rt_enable()
    time.sleep(0.2)
    counts = a.counts or [6, 10, 12, 15, 18, 20, 24, 30, 36, 40, 48, 60, 72, 90, 120]
    print("Watch the strip. Each step draws a rainbow with N distinct bands; the largest N that\n"
          "still shows N bands (and isn't ignored) is your segment count. Ctrl-C to stop.")
    try:
        for n in counts:
            cols = [hsv(i / n, 1, 1) for i in range(n)]
            print(f"  N = {n:3d}  ", end="", flush=True)
            t_end = time.monotonic() + a.hold
            while time.monotonic() < t_end:
                g.rt_frame(cols)
                time.sleep(0.05)
            print("ok")
    except KeyboardInterrupt:
        pass
    finally:
        g.rt_disable()


def cmd_chase(a):
    g = GoveeLan(a.ip)
    g.power(True)
    g.brightness(a.brightness)
    g.rt_enable()
    n = a.segments
    print(f"chasing one lit segment across {n} segments at {a.fps} fps. Ctrl-C to stop.")
    i = 0
    try:
        while True:
            cols = [(0, 0, 0)] * n
            cols[i % n] = hsv((i / n) % 1.0, 1, 1)
            cols[(i - 1) % n] = hsv(((i - 1) / n) % 1.0, 1, 0.25)
            g.rt_frame(cols)
            i += 1
            time.sleep(1.0 / a.fps)
    except KeyboardInterrupt:
        pass
    finally:
        g.rt_disable()


def cmd_devices(a):
    import sounddevice as sd
    print(sd.query_devices())
    print("\nmacOS: install BlackHole (brew install blackhole-2ch), make a Multi-Output Device in\n"
          "Audio MIDI Setup (speakers + BlackHole), set it as system output, then --device 'BlackHole 2ch'.\n"
          "Linux: use the PipeWire/Pulse 'Monitor of ...' source.\n"
          "Windows: PortAudio has no loopback; use pyaudiowpatch, or VB-Cable as the device.")


def cmd_audio(a):
    n = a.segments
    g = None if a.no_send else GoveeLan(a.ip)
    if g:
        g.power(True)
        g.brightness(a.brightness)
        g.rt_enable()

    src = wav_blocks(a.wav, a.block) if a.wav else live_blocks(a.device, a.block, a.samplerate)
    an = None
    vis = Visualizer(n, a.mode, a.hue, a.hue_span)
    frame_dt = 1.0 / a.fps
    next_send = 0.0
    pending_beat = False
    frames = 0
    t0 = time.monotonic()
    print(f"streaming {a.mode} to {'(nothing)' if a.no_send else a.ip} on {n} segments, "
          f"max {a.fps} fps. Ctrl-C to stop.")
    try:
        for sr, block in src:
            if an is None:
                an = Analyzer(sr, n, a.block)
            if len(block) != a.block:
                continue
            f = an.process(block)
            if f["beat"]:
                pending_beat = True   # never let a beat fall between two sent frames
            now = time.monotonic()
            if now < next_send and not pending_beat:
                continue
            # schedule from the previous slot, not from now, so audio blocks (21 ms) and
            # frames (25 ms) interleave instead of beating against each other
            next_send = max(next_send + frame_dt, now - frame_dt)
            if pending_beat:
                f = dict(f, beat=True)
                pending_beat = False
            cols = vis.render(f)
            if g:
                g.rt_frame(cols)
            frames += 1
            if a.preview:
                print(preview_line(cols, f), end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        el = time.monotonic() - t0
        print(f"\n{frames} frames in {el:.1f}s ({frames / max(el, 1e-6):.1f} fps)")
        if g:
            g.rt_disable()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--timeout", type=float, default=3.0)
    s.add_argument("--target", action="append", help="extra unicast/broadcast IP to poke (repeatable)")
    s.set_defaults(fn=cmd_scan)

    def common(sp):
        sp.add_argument("--ip", required=True)
        sp.add_argument("--brightness", type=int, default=100)

    s = sub.add_parser("solid"); common(s)
    s.add_argument("--rgb", type=int, nargs=3, default=[255, 60, 0])
    s.set_defaults(fn=cmd_solid)

    s = sub.add_parser("probe"); common(s)
    s.add_argument("--counts", type=int, nargs="*")
    s.add_argument("--hold", type=float, default=2.5, help="seconds per step")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("chase"); common(s)
    s.add_argument("--segments", type=int, default=15)
    s.add_argument("--fps", type=float, default=30)
    s.set_defaults(fn=cmd_chase)

    s = sub.add_parser("devices")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("audio")
    s.add_argument("--ip")
    s.add_argument("--no-send", action="store_true", help="analysis only, no packets")
    s.add_argument("--brightness", type=int, default=100)
    s.add_argument("--segments", type=int, default=15)
    s.add_argument("--mode", choices=["spectrum", "pulse", "vu", "wave"], default="spectrum")
    s.add_argument("--hue", type=float, default=0.0, help="base hue 0..1 (0 red, .33 green, .66 blue)")
    s.add_argument("--hue-span", type=float, default=0.75, help="spectrum mode: hue range across strip")
    s.add_argument("--fps", type=float, default=40)
    s.add_argument("--block", type=int, default=1024, help="FFT/audio block size (1024 @ 48k = 21 ms)")
    s.add_argument("--device", help="sounddevice input name/index (loopback device)")
    s.add_argument("--samplerate", type=int)
    s.add_argument("--wav", help="drive from a wav file instead of live audio")
    s.add_argument("--preview", action="store_true", help="ASCII preview of the strip in the terminal")
    s.set_defaults(fn=cmd_audio)

    a = p.parse_args()
    if a.cmd == "audio" and not a.no_send and not a.ip:
        p.error("audio needs --ip (or --no-send)")
    a.fn(a)


if __name__ == "__main__":
    main()
