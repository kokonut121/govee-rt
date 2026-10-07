"""Bluetooth LE transport with the same interface as GoveeLan. See docs/PROTOCOL.md."""

import asyncio
import collections
import threading
import time

from .protocol import _xor, razer_colors, razer_frame


# --------------------------------------------------------------------------- Bluetooth LE
#
# For when the strip isn't on Wi-Fi. GATT write characteristic ...2b11, notify ...2b10.
# Packets are 20 bytes: 33 <cmd> <data>, zero padded, last byte = XOR of the first 19.
# Per-segment color uses the "static color" command, one color per write, applied to a
# little-endian segment mask that starts at byte 12 (bit 0 = segment 1):
#
#     33 05 15 01  r g b  00 00  r g b  mask (LE, bytes 12..18)  xor
#
# H617A (15 segments) only needs two mask bytes; the H612D (20 segments) takes a third.
# The strip ACKs every 33 command with "33 <cmd> 00 ..." and drops writes that arrive while
# it is still busy (~90 ms per static color on the H612D, on top of ~85 ms link round trip
# from macOS), so writes are paced on ACKs. See docs/PROTOCOL.md.

BLE_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"
BLE_NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
BLE_ENC_MARKER = "00010203-0405-0607-0809-0a0b0c0d2b12"
MASK_BYTES = 7                       # packet bytes 12..18
BLE_MAX_SEGMENTS = 8 * MASK_BYTES    # 56


def ble_packet(*body: int) -> bytes:
    b = bytes(body)
    if len(b) > 19:
        raise ValueError(f"BLE command body is {len(b)} bytes, max 19")
    b += bytes(19 - len(b))
    return b + bytes([_xor(b)])


def ble_power(on: bool) -> bytes:
    return ble_packet(0x33, 0x01, 0x01 if on else 0x00)


def ble_brightness(pct: int) -> bytes:
    return ble_packet(0x33, 0x04, max(1, min(100, int(pct))))


def ble_keepalive() -> bytes:
    return ble_packet(0xAA, 0x01)  # power-state query; the app sends one every ~2 s


def ble_segment_color(mask: int, r: int, g: int, b: int) -> bytes:
    if not 0 < mask < (1 << BLE_MAX_SEGMENTS):
        raise ValueError(f"segment mask {mask:#x} out of range")
    r, g, b = int(r) & 255, int(g) & 255, int(b) & 255
    return ble_packet(0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, r, g, b, *mask.to_bytes(MASK_BYTES, "little"))


def resample(colors, n):
    """Map len(colors) colors onto n slots, nearest neighbour from each slot's center."""
    m = len(colors)
    return [colors[min(m - 1, int((i + 0.5) * m / n))] for i in range(n)]


def ble_segment_packets(colors, n_seg, prev=None, tol=6, force=None):
    """Plan the writes that take the strip from `prev` to `colors` on n_seg segments.

    Segments within `tol` (max channel difference) of what was last sent are skipped, except
    `force` (one index per frame, round robin, so a dropped write heals within n_seg frames).
    Segments that end up the same color share one write. Returns (packets, new_state).
    """
    if not 0 < n_seg <= BLE_MAX_SEGMENTS:
        raise ValueError(f"BLE segment mode supports 1..{BLE_MAX_SEGMENTS} segments, got {n_seg}")
    target = [tuple(int(c) & 255 for c in col) for col in resample(list(colors), n_seg)]
    state = list(prev) if prev and len(prev) == n_seg else [None] * n_seg
    groups = {}
    for i, col in enumerate(target):
        old = state[i]
        if i != force and old is not None and max(abs(a - b) for a, b in zip(col, old)) <= tol:
            continue
        groups[col] = groups.get(col, 0) | (1 << i)
        state[i] = col
    return [ble_segment_color(mask, *col) for col, mask in groups.items()], state


# ---- Finger Sketch ("DIY type 0x03") uploads: per-cell color at 3x the static segment count.
#
# Body (after the A3 envelope, see a3_frames): 03 effect speed brightness bg_rgb n_groups
# then per group: n_cells fill_rgb cell_index... The strip stores the upload and shows it once
# selected with 33 05 0a <diy code u16 LE>. Verified on the H612D: 60 cells, ~3 updates/s with
# upload + select per frame, "clockwise" at speed 0 holds the picture still.

SKETCH_EFFECT_STILL = 0x09   # clockwise; at speed 0 nothing moves
SKETCH_CODE = 0x0320         # what the H612D reports (AA 05 01 -> 0a 20 03) while in Finger Sketch


def a3_frames(type_byte, body):
    """A3 multi-packet envelope: 01 <line count> <type> <body>, 17 bytes per frame, then A3 FF."""
    data = bytes([type_byte]) + bytes(body)
    n = -(-(len(data) + 2) // 17)
    payload = bytes([0x01, n + 1]) + data
    out = []
    for idx in range(n):
        p = (bytes([0xA3, idx]) + payload[17 * idx:17 * idx + 17] + bytes(17))[:19]
        out.append(p + bytes([_xor(p)]))
    term = bytes([0xA3, 0xFF]) + bytes(17)
    return out + [term + bytes([_xor(term)])]


def sketch_body(background, groups, effect=SKETCH_EFFECT_STILL, speed=0, brightness=100):
    b = bytes([effect, speed, brightness, *background, len(groups)])
    for fill, cells in groups:
        b += bytes([len(cells), *fill, *cells])
    return b


def sketch_select(code=SKETCH_CODE):
    return ble_packet(0x33, 0x05, 0x0A, code & 0xFF, code >> 8)


def sketch_plan(colors, cells, quant=8, effect=SKETCH_EFFECT_STILL, speed=0):
    """Colors -> (A3 frames, picture). Channels are rounded to `quant` steps so near-equal colors
    share a group; the most common color becomes the background and costs nothing.
    effect/speed: the strip's own animation of the picture (speed 0 = hold still)."""
    if not 0 < cells <= 255:
        raise ValueError(f"sketch mode supports 1..255 cells, got {cells}")
    q = lambda v: min(255, int(round(int(v) / quant) * quant))  # noqa: E731
    pic = [tuple(q(c) for c in col) for col in resample(list(colors), cells)]
    by_color = {}
    for i, col in enumerate(pic):
        by_color.setdefault(col, []).append(i)
    background = max(by_color, key=lambda c: len(by_color[c]))
    groups = [(col, idx) for col, idx in by_color.items() if col != background]
    return a3_frames(0x03, sketch_body(background, groups, effect, speed)), tuple(pic)


# ---- Whole-strip color stream: the packet the Govee app sends in phone-mic music mode,
# here fed from the computer's audio instead. 7 bytes, no ACK, no mode select needed:
#
#     a5 02 83 r g b (sum of the first six bytes & 0xff)
#
# H612D (2026-10-06): the strip shows each color as it arrives; 25/s looked right to the
# owner. The engine renders at STREAM_FPS so every frame is sent at an even cadence;
# STREAM_MIN_GAP only guards against bursts (a beat wakes the engine early).

STREAM_FPS = 30
STREAM_MIN_GAP = 1 / 45

# ---- Rolling bar ("roll": one bar rolling; "roll_hits": a bar per hit). The Mac can't move a bar along the strip over BLE (the
# stream is one color for the whole strip, sketch uploads manage ~3/s), so the strip moves it:
# a picture that is one bar at cell 1 (the owner's right end), rolled by sketch effect 0x09.
# Re-sending only the mode select restarts the roll from the start: the owner saw the bar snap
# back to the right end on every select (2026-10-08, speed 60, every 3 s), and liked the
# direction (right to left) and speed 100. A bare select on every hit (~3/s) killed the bar
# in flight and looked broken, and the owner wants every hit to ADD a bar while the old ones
# finish their pass. The strip only rolls one stored picture, so each hit uploads a new one:
# every bar in flight drawn where it is when the strip starts rolling the new picture, plus
# the new bar (drawn a little in from the end, where it would be had it started on the kick).
# A bar about to reach the left end is dropped by another upload, or the roll would wrap it
# back to the right end. Positions come from ROLL_CELLS_PER_S and the measured upload delay.

ROLL_EFFECT = 0x09
ROLL_SPEED = 100
ROLL_CELLS_PER_S = 48.0        # owner: a pass takes "1-1.5 seconds" at speed 100; 60 cells / 1.25 s
ROLL_BAR = 6                   # lit cells per bar
ROLL_COLOR = (255, 0, 170)     # the magenta the owner watched
ROLL_LIT = 0.2                 # frame peak (0..1) above which the scene is "lit"
ROLL_STEP = 0.3                # a lit frame this much brighter or dimmer than the last is a new hit
ROLL_MIN_GAP = 0.12            # s between bars
ROLL_DELAY0 = 0.2              # first guess of upload start -> strip rolling, then measured


ROLL_FLASH_BARS = 4            # "roll_flash": evenly spaced bars, one palette color each
ROLL_FLASH_STEP = 4            # brightness % change worth an ACKed write


def roll_picture(cells, positions=(0,), bar=ROLL_BAR, color=ROLL_COLOR, colors=None):
    """60-cell picture with a bar starting at each position (cell 0 = the right end).
    colors: one per bar (cycled), else all `color`."""
    pic = [(0, 0, 0)] * cells
    for k, p in enumerate(positions):
        c = colors[k % len(colors)] if colors else color
        for i in range(max(0, int(p)), min(cells, int(p) + bar)):
            pic[i] = c
    return pic


def roll_flash_picture(cells, colors, bars=ROLL_FLASH_BARS, bar=ROLL_BAR):
    """Bars spread evenly around the strip, colors picked evenly from the palette."""
    colors = list(colors) or [ROLL_COLOR]
    picks = [colors[(k * len(colors)) // bars] for k in range(bars)]
    return roll_picture(cells, [round(k * cells / bars) for k in range(bars)], bar=bar, colors=picks)


def stream_frame(r, g, b):
    p = bytes([0xA5, 0x02, 0x83, int(r) & 255, int(g) & 255, int(b) & 255])
    return p + bytes([sum(p) & 0xFF])


def strip_color(colors):
    """One color that stands for a whole frame: hue from the lit cells (weighted by how lit
    they are), brightness from the brighter part of the frame, so a small bright comet still
    reads as a flash instead of averaging away."""
    lit = [(max(c), c) for c in colors]
    total = sum(w for w, _ in lit)
    if total == 0:
        return (0, 0, 0)
    avg = [sum(w * c[k] for w, c in lit) / total for k in range(3)]
    peak = max(avg) or 1
    levels = sorted(w for w, _ in lit)
    target = levels[int(0.75 * (len(levels) - 1))] * 0.5 + levels[-1] * 0.5
    return tuple(int(round(v / peak * target)) for v in avg)


# Test color for segment readback: unusual enough not to be left over from a scene.
MEASURE_CODE = (0x31, 0xC7, 0x5A)


def _reply_key(pkt):
    """Replies echo the query's domain byte; segment pages (A5) also echo the group.
    ACKs to 33 commands ("33 <cmd> <status>") are keyed ("ack", cmd)."""
    if pkt[0] == 0x33:
        return ("ack", pkt[1])
    return (pkt[1], pkt[2]) if pkt[1] == 0xA5 else (pkt[1],)


def _is_govee_name(name):
    n = (name or "").lower()
    return any(k in n for k in ("govee", "ihoment", "gbk_", "minger")) or n.startswith("h6")


async def ble_scan(timeout=8.0):
    """[(rssi, BLEDevice, name)] for advertising Govee lights, strongest first."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = [(adv.rssi, d, adv.local_name or d.name or "") for d, adv in found.values()
           if _is_govee_name(adv.local_name or d.name)]
    return sorted(out, key=lambda x: -x[0])


class GoveeBle:
    """Same interface as GoveeLan, over Bluetooth LE (bleak).

    bleak is asyncio-only, so it gets its own event-loop thread. rt_frame() never blocks: it
    replaces the pending frame and the sender writes the newest one when the radio is free,
    so a slow link drops frames instead of building up lag.

    mode "segments": per-segment static color writes (documented for H617A-class strips).
    mode "razer":    the LAN real-time BB frames written raw to the GATT characteristic.
                     Unconfirmed over BLE; needs a frame that fits in the negotiated MTU.
    """

    def __init__(self, target="auto", segments=15, mode="segments", scan_timeout=10.0,
                 client_factory=None, cells=None, reverse=False, sketch_code=SKETCH_CODE):
        if mode == "segments" and not 0 < segments <= BLE_MAX_SEGMENTS:
            raise ValueError(f"BLE segment mode supports 1..{BLE_MAX_SEGMENTS} segments")
        self.target = target or "auto"
        self.segments = segments
        self.mode = mode
        self.cells = cells or 3 * segments          # sketch resolution
        self.reverse = reverse                      # cell/segment 1 at the far end
        self.sketch_code = sketch_code
        self._sketch_pic = None
        self._last_stream = 0.0
        self._stream_sent = None
        self._bright_sent = None
        self._last_upload = 0.0
        self.scan_timeout = scan_timeout
        self.client_factory = client_factory
        self.name = None
        self.address = None
        self.enc_marker = None
        self.error = None
        self.stats = collections.Counter()
        self.notifications = []
        self._waiters = {}  # reply key -> [futures], see _reply_key()
        self._pending = None
        self._plock = threading.Lock()  # guards _pending between caller and loop thread
        self._state = None
        self._rr = 0
        self._last_enable = 0.0
        self.default_mode = mode
        self._roll_reset()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        try:
            self._run(self._connect(), timeout=scan_timeout + 40)
        except BaseException:
            self._stop_loop()
            raise

    # ---- plumbing
    def _run(self, coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _stop_loop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)

    async def _resolve(self):
        from bleak import BleakScanner
        t = self.target
        if t != "auto" and (len(t) == 36 and t.count("-") == 4 or len(t) == 17 and t.count(":") == 5):
            return await BleakScanner.find_device_by_address(t, timeout=self.scan_timeout)
        for _, d, name in await ble_scan(self.scan_timeout):
            if t == "auto" or t.lower() in name.lower():
                return d
        return None

    async def _connect(self):
        if self.client_factory:
            self.client = self.client_factory()
            self.name = getattr(self.client, "name", None)
            self.address = getattr(self.client, "address", None)
        else:
            from bleak import BleakClient
            dev = await self._resolve()
            if dev is None:
                raise RuntimeError("no Govee light found over Bluetooth. Is the phone app still "
                                   "connected to it? The strip takes one connection at a time.")
            self.name = dev.name
            self.address = dev.address
            self.client = BleakClient(dev, timeout=20.0)
        await self.client.connect()
        self.mtu = getattr(self.client, "mtu_size", 23)
        chars = {c.uuid.lower(): c for s in self.client.services for c in s.characteristics}
        if BLE_ENC_MARKER in chars:
            marker = bytes(await self.client.read_gatt_char(BLE_ENC_MARKER))
            self.enc_marker = marker.hex()
            if len(marker) >= 2 and marker[1] != 0:
                await self.client.disconnect()
                raise RuntimeError(f"this light uses Govee's encrypted BLE protocol (v{marker[1]}, "
                                   f"marker {marker.hex()}); plaintext packets would be ignored")
        if BLE_NOTIFY in chars:
            await self.client.start_notify(BLE_NOTIFY, self._on_notify)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._sender_task = asyncio.get_running_loop().create_task(self._sender())

    def _on_notify(self, _char, data):
        data = bytes(data)
        self.stats["notifications"] += 1
        if data[:1] == b"\x33":
            self.stats["acks"] += 1
        self.notifications.append(data)
        del self.notifications[:-50]
        if len(data) == 20 and data[0] in (0xAA, 0x33) and _xor(data[:19]) == data[19]:
            for fut in self._waiters.pop(_reply_key(data), []):
                if not fut.done():
                    fut.set_result(data)

    async def _request(self, pkt, timeout):
        """Write pkt and wait for the matching notification (None on timeout)."""
        key = _reply_key(pkt)
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        await self._write(pkt)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.stats["timeouts"] += 1
            return None
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)

    async def _query(self, *body, timeout=1.0):
        """Send an AA query and wait for its reply (None on timeout)."""
        return await self._request(ble_packet(0xAA, *body), timeout)

    async def _write_acked(self, pkt, timeout=0.5):
        """Write a 33 command and wait for the strip's ACK, so the next write isn't dropped."""
        ack = await self._request(pkt, timeout)
        if ack is not None and ack[2] != 0:
            self.stats["nacks"] += 1
        return ack

    def query(self, *body, timeout=1.0):
        return self._run(self._query(*body, timeout=timeout), timeout + 2)

    async def _measure_segments(self):
        """Paint every mask bit one test color, read the segment pages back and count the records
        holding it: slots past the real end of the strip don't store a color. (None, why) if the
        strip doesn't answer readback."""
        ack = await self._write_acked(ble_segment_color((1 << BLE_MAX_SEGMENTS) - 1, *MEASURE_CODE), 1.0)
        if ack is None:
            return None, "no ACK to the all-segments test color"
        count, pages = 0, 0
        for g in range(1, BLE_MAX_SEGMENTS // 3 + 2):
            r = await self._query(0xA5, g, timeout=0.8)
            if r is None:
                break
            pages += 1
            hits = sum(1 for k in range(4) if tuple(r[3 + 4 * k + 1: 3 + 4 * k + 4]) == MEASURE_CODE)
            if not hits:
                break
            count += hits
        if not pages:
            return None, "no reply to segment readback (AA A5)"
        if not count:
            return None, f"segment readback ({pages} pages) didn't echo the test color"
        return count, f"readback: {count} segments hold the test color ({pages} pages)"

    def identify(self, measure=True):
        """What the strip reports about itself, plus a measured segment count."""
        def text(r, skip):
            return r[skip:19].split(b"\0")[0].decode("ascii", "replace") if r else None
        info = {"transport": "ble", "address": self.address, "name": self.name, "mtu": self.mtu,
                "encryption_marker": self.enc_marker,
                "firmware": text(self.query(0x06), 2),
                "hardware": text(self.query(0x07, 0x03), 3)}
        lc = self.query(0x0F)
        info["light_count"] = lc[2] if lc and 0 < lc[2] <= 127 else None
        if measure:
            n, how = self._run(self._measure_segments(), 15)
            info["segments_measured"], info["segments_note"] = n, how
            self._state = None
        return info

    async def _write(self, data: bytes):
        async with self._lock:
            await self.client.write_gatt_char(BLE_WRITE, data, response=False)
        self.stats["writes"] += 1

    def _frame_packets(self, colors):
        if self.mode == "razer":
            pkts = []
            if time.monotonic() - self._last_enable > 20:
                pkts.append(razer_frame(0xB1, b"\x01"))
                self._last_enable = time.monotonic()
            pt = razer_colors(colors)
            if len(pt) > self.mtu - 3:
                raise RuntimeError(f"razer frame of {len(pt)} bytes doesn't fit MTU {self.mtu}; "
                                   f"use fewer segments or --ble-mode segments")
            return pkts + [pt]
        if self.mode == "sketch":
            frames, pic = sketch_plan(colors, self.cells)
            if pic == self._sketch_pic:
                return []
            self._sketch_pic = pic
            return frames + [sketch_select(self.sketch_code)]
        pkts, self._state = ble_segment_packets(colors, self.segments, self._state, force=self._rr)
        self._rr = (self._rr + 1) % self.segments
        return pkts

    async def _sender(self):
        last_tx = time.monotonic()
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                with self._plock:
                    colors, self._pending = self._pending, None
                if colors is None:
                    if time.monotonic() - last_tx > 2.0:
                        await self._write(ble_keepalive())
                        last_tx = time.monotonic()
                    continue
                if self.mode == "stream":
                    await self._send_stream(colors)
                elif self.mode in ("roll", "roll_hits", "roll_flash"):
                    await self._send_roll(colors)
                elif self.mode == "hybrid":
                    await self._send_hybrid(colors)
                else:
                    for p in self._frame_packets(colors):
                        if p[0] == 0x33:
                            await self._write_acked(p)   # the strip drops writes sent while busy
                        else:
                            await self._write(p)
                self.stats["frames_sent"] += 1
                last_tx = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # disconnects etc.; surfaced on the next rt_frame()
            self.error = e

    async def _send_stream(self, colors):
        wait = self._last_stream + STREAM_MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        col = strip_color(colors)
        if col == self._stream_sent and time.monotonic() - self._last_stream < 0.5:
            return
        await self._write(stream_frame(*col))
        self._stream_sent, self._last_stream = col, time.monotonic()

    def _roll_reset(self):
        self._roll_ready = False       # a picture is uploaded and rolling
        self._roll_bars = []           # launch times (monotonic) of the bars in that picture
        self._roll_hits = []           # hit times from rt_frame, not yet drawn (under _plock)
        self._roll_prev = 0.0
        self._roll_last_hit = -1e9
        self._roll_delay = ROLL_DELAY0
        if not hasattr(self, "_roll_colors"):
            self._roll_colors = [ROLL_COLOR]

    def _roll_hit(self, colors):
        """Called from rt_frame (every frame, so dropped frames can't hide a kick). A hit is a
        frame that turns lit, or a lit frame that steps by ROLL_STEP (Bass Pulse runs toggle)."""
        peak = max((max(c) for c in colors), default=0) / 255.0
        prev, self._roll_prev = self._roll_prev, peak
        now = time.monotonic()
        if (peak >= ROLL_LIT and (prev < ROLL_LIT or abs(peak - prev) >= ROLL_STEP)
                and now - self._roll_last_hit >= ROLL_MIN_GAP):
            self._roll_last_hit = now
            self._roll_hits.append(now)

    def _roll_pos(self, launch, t):
        return ROLL_CELLS_PER_S * (t - launch)

    async def _send_roll(self, colors):
        """"roll": one bar, uploaded once and left rolling (the owner's favourite on its own).
        "roll_hits": a bar per hit; old bars finish their pass."""
        if self.mode == "roll":
            if not self._roll_ready:
                await self._roll_upload([], positions=[0])
            return
        if self.mode == "roll_flash":
            await self._send_roll_flash(colors)
            return
        with self._plock:
            hits, self._roll_hits = self._roll_hits, []
        now, d, end = time.monotonic(), self._roll_delay, self.cells - ROLL_BAR
        # a bar must be gone before its head reaches the end, or the roll wraps it to the start
        leaving = any(self._roll_pos(b, now + 2 * d) > end for b in self._roll_bars)
        if hits or leaving or not self._roll_ready:
            await self._roll_upload(self._roll_bars + hits)

    def set_roll_colors(self, colors):
        """Palette for the "roll_flash" bars (0..255 RGB tuples); a change re-uploads them."""
        colors = [tuple(int(v) for v in c) for c in colors][:8] or [ROLL_COLOR]
        if colors != self._roll_colors:
            self._roll_colors = colors
            self._roll_ready = False

    async def _send_roll_flash(self, colors):
        """Bars + flash: the colored bars roll on the strip untouched; the scene's brightness
        (Bass Pulse: full on a kick, rest between, ~off when dark) rides on the brightness
        command, ACK-paced, latest wins. Brightness changes don't interrupt the roll."""
        if not self._roll_ready:
            pic = roll_flash_picture(self.cells, self._roll_colors)
            frames, _ = sketch_plan(pic, self.cells, effect=ROLL_EFFECT, speed=ROLL_SPEED)
            for f in frames:
                await self._write(f)
            await self._write_acked(sketch_select(self.sketch_code))
            self._roll_ready = True
            self._sketch_pic = self._stream_sent = None
            self.stats["roll_uploads"] += 1
        peak = max((max(c) for c in colors), default=0)
        pct = max(1, round(peak / 255 * 100))
        sent = self._bright_sent
        if sent is None or abs(pct - sent) >= ROLL_FLASH_STEP or (pct == 1) != (sent == 1) or (pct == 100) != (sent == 100):
            await self._write_acked(ble_brightness(pct), 0.4)
            self._bright_sent = pct
            self.stats["roll_flashes"] += 1

    async def _roll_upload(self, bars, positions=None):
        t0 = time.monotonic()
        d = self._roll_delay
        t_roll = t0 + d                                    # when the strip will start this picture
        keep = [b for b in bars if self._roll_pos(b, t_roll + d) <= self.cells - ROLL_BAR]
        if positions is None:
            positions = [round(self._roll_pos(b, t_roll)) for b in keep]
        pic = roll_picture(self.cells, positions)
        frames, _ = sketch_plan(pic, self.cells, effect=ROLL_EFFECT, speed=ROLL_SPEED)
        for f in frames:
            await self._write(f)
        t_sel = time.monotonic()
        ack = await self._write_acked(sketch_select(self.sketch_code), 0.4)
        if ack is not None:                                # the strip acts on the select ~half an RTT in
            took = (t_sel - t0) + (time.monotonic() - t_sel) / 2
            self._roll_delay = 0.7 * d + 0.3 * took
        self._roll_bars, self._roll_ready = keep, True
        self._sketch_pic = self._stream_sent = None
        self.stats["roll_uploads"] += 1
        self.stats["roll_bars"] = len(keep)

    def set_mode(self, mode):
        """Switch how frames are sent (e.g. a scene that wants "roll"), effective next frame."""
        if mode and mode != self.mode:
            self.mode = mode
            self.rt_enable()

    async def _send_hybrid(self, colors):
        """Brightness command carries the beat (~10/s, ACKed); a sketch upload carries the
        picture, normalized to full brightness, at most ~3/s and only when it changed."""
        peak = max((max(c) for c in colors), default=0)
        pct = max(1, round(peak / 255 * 100))
        if self._bright_sent is None or abs(pct - self._bright_sent) >= 3 or (pct == 1) != (self._bright_sent == 1):
            await self._write_acked(ble_brightness(pct), 0.4)
            self._bright_sent = pct
        now = time.monotonic()
        if peak < 3 or now - self._last_upload < 0.33:
            return
        scale = 255.0 / peak
        pattern = [tuple(min(255, int(v * scale)) for v in c) for c in colors]
        frames, pic = sketch_plan(pattern, self.cells, quant=16)
        if pic == self._sketch_pic:
            return
        for f in frames:
            await self._write(f)
        await self._write_acked(sketch_select(self.sketch_code))
        self._sketch_pic, self._last_upload = pic, time.monotonic()

    # ---- GoveeLan-compatible interface
    def power(self, on: bool):
        self._run(self._write_acked(ble_power(on)))

    def brightness(self, pct: int):
        self._run(self._write_acked(ble_brightness(pct)))

    def color(self, r, g, b):
        async def go():
            mask = (1 << min(self.segments, BLE_MAX_SEGMENTS)) - 1
            await self._write_acked(ble_segment_color(mask, r, g, b))
            self._state = self._sketch_pic = None  # strip no longer shows the last frame
        self._run(go())

    def show_sketch(self, colors, effect=SKETCH_EFFECT_STILL, speed=0):
        """Put a picture on the strip and let the strip animate it itself (effect/speed), e.g.
        an idle rainbow. Blocks ~0.5 s. Frames sent afterwards take over again."""
        async def go():
            if self._bright_sent not in (None, 100):
                await self._write_acked(ble_brightness(100), 0.4)
            frames, _ = sketch_plan(colors, self.cells, effect=effect, speed=speed)
            for f in frames:
                await self._write(f)
            await self._write_acked(sketch_select(self.sketch_code))
            self._state = self._sketch_pic = self._stream_sent = None
            self._bright_sent = 100
            self._roll_ready = False    # the bar picture was replaced
        self._run(go(), 10)

    def rt_enable(self):
        self._state = self._sketch_pic = self._stream_sent = self._bright_sent = None
        self._roll_reset()          # the strip may show something else now: re-upload the bar
        if self.mode == "razer":
            self._run(self._write(razer_frame(0xB1, b"\x01")))
            self._last_enable = time.monotonic()

    def rt_disable(self):
        if self.error:
            return
        try:
            if self.mode == "razer":
                self._run(self._write(razer_frame(0xB1, b"\x00")))
            elif self.mode in ("hybrid", "roll_flash") and self._bright_sent not in (None, 100):
                self._run(self._write_acked(ble_brightness(100)))   # don't leave the strip dimmed
                self._bright_sent = 100
        except Exception:
            pass

    def rt_frame(self, colors):
        if self.error:
            raise RuntimeError(f"BLE link failed: {self.error}")
        colors = list(colors)
        if self.reverse:
            colors.reverse()
        with self._plock:
            self.stats["frames_in"] += 1
            if self._pending is not None:
                self.stats["frames_dropped"] += 1
            self._pending = colors
            if self.mode == "roll_hits":
                self._roll_hit(colors)
        self._loop.call_soon_threadsafe(self._wake.set)

    def flush(self, timeout=2.0):
        """Wait until the pending frame has been written (used by tests and short commands)."""
        t_end = time.monotonic() + timeout
        s = self.stats
        while (s["frames_sent"] + s["frames_dropped"] < s["frames_in"] and not self.error
               and time.monotonic() < t_end):
            time.sleep(0.005)

    def close(self):
        async def go():
            self._sender_task.cancel()
            try:
                await self._sender_task
            except BaseException:
                pass
            if self.client.is_connected:
                await self.client.disconnect()
        try:
            self._run(go())
        finally:
            self._stop_loop()
