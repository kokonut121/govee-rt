"""Real-time loop shared by `play` and `studio`: audio -> features -> scene -> frame -> light.

Audio analysis runs on its own thread and only publishes the latest features. The render loop
runs at --fps, but a detected beat wakes it immediately (respecting the 15 ms minimum gap
between sends), so flashes land on the kick instead of up to one frame late.
"""

import collections
import copy
import math
import threading
import time

import numpy as np

from .audio import Analyzer
from .effects import Renderer, normalize_scene, palette_stops, rgb_to_hex, sample, scene_idles

N_BANDS = 24
MIN_GAP = 0.015  # seconds between frames to the light (docs/PROTOCOL.md)
SILENCE_RMS = 1e-3   # about -60 dBFS: below this the computer is playing nothing
# Finger Sketch "gradient": the strip itself fades each cell smoothly through the picture's
# colors, so the idle rainbow flows without the cell-by-cell jumps of "clockwise" (0x09).
# Owner's pick on the H612D: speed 50 (2026-10-06).
IDLE_EFFECT = 0x13
STROBE_HZ = 10.0     # hold-to-strobe default (flashes/s). It runs on its own even clock: on the
                     # 40 fps frame clock a 15/s strobe came out as uneven 1- and 2-frame flashes
                     # and the owner saw it as laggy. Each flash is one stream write, so 10/s is
                     # 20 writes/s, inside what the BLE link delivered with the speaker playing.
STROBE_MAX_HZ = 15.0
STROBE_HOLD = 0.6    # s the strobe stays on after the last request (the page re-sends while held)
IDLE_SPEED = 50


def blank_features():
    return {"bands": np.zeros(N_BANDS), "bass": 0.0, "mid": 0.0, "high": 0.0, "energy": 0.0,
            "beat": False, "beat_env": 0.0}


class DemoBeat:
    """Synthetic music features (four-on-the-floor kick, off-beat hats, a drifting mid) so
    scenes can be designed and tested with no audio routed."""

    def __init__(self, bpm=124.0):
        self.period = 60.0 / bpm
        self.last_beat_index = -1
        self.beat_env = 0.0
        self.last_t = 0.0

    def features(self, t):
        ph = (t / self.period) % 1.0
        idx = int(t / self.period)
        beat = idx != self.last_beat_index
        self.last_beat_index = idx
        self.beat_env = 1.0 if beat else self.beat_env * math.exp(-(t - self.last_t) / 0.25)
        self.last_t = t
        kick = math.exp(-ph * 7)
        hat = math.exp(-((ph - 0.5) % 1.0) * 14)
        mid = 0.45 + 0.3 * math.sin(2 * math.pi * t * 0.11) + 0.1 * math.sin(2 * math.pi * t * 0.7)
        x = np.linspace(0, 1, N_BANDS)
        bands = np.clip(kick * np.exp(-x * 8) + mid * np.exp(-((x - 0.45) / 0.18) ** 2)
                        + 0.8 * hat * np.clip((x - 0.6) * 2.5, 0, 1), 0, 1)
        f = {"bands": bands, "bass": 0.15 + 0.85 * kick, "mid": float(np.clip(mid, 0, 1)),
             "high": 0.1 + 0.8 * hat, "energy": float(bands.mean()), "beat": beat,
             "beat_env": self.beat_env}
        return f, beat


class Engine:
    def __init__(self, n, light_factory=None, fps=40.0, scene=None, idle_after=None, idle_speed=IDLE_SPEED):
        self.n = n
        self.idle_after = idle_after      # seconds of silence before the idle rainbow; None = never
        self.idle_speed = idle_speed      # 0 = still rainbow, >0 = the strip animates it itself
        self.idle = False
        self._quiet_since = None
        self.fps = min(60.0, float(fps))
        self.light_factory = light_factory
        self.light = None
        self.light_error = None
        self.renderer = Renderer(n, scene)
        self.stats = collections.Counter()
        self.source = "silence"
        self._lock = threading.Lock()
        self._feat = blank_features()
        self._beat = False
        self._onset = False
        self._demo = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._live = {"colors": ["#000000"] * n, "bands": [0.0] * N_BANDS, "beat": False,
                      "features": dict.fromkeys(("bass", "mid", "high", "energy", "beat_env"), 0.0)}
        self._threads = []
        self._strobe_until = 0.0
        self._strobe_color = None
        self._strobe_on = False
        self.strobe_hz = STROBE_HZ
        self._last_lit = (255, 255, 255)

    # ---- scene
    def get_scene(self):
        with self._lock:
            return copy.deepcopy(self.renderer.scene)

    def set_scene(self, scene):
        s = normalize_scene(scene)
        with self._lock:
            self.renderer.scene = s
        return s

    def _scene_palette(self):
        """The first enabled layer's palette as 0..255 RGB (bar colors for "roll_flash")."""
        scene = self.renderer.scene
        if getattr(self, "_pal_scene", None) is not scene:
            pal = next((l["params"]["palette"] for l in scene.get("layers", [])
                        if l.get("enabled", True) and "palette" in l.get("params", {})), "neon")
            self._pal_cache = [tuple(int(round(255 * v)) for v in c) for c in palette_stops(pal)]
            self._pal_scene = scene
        return self._pal_cache

    # ---- hold-to-strobe
    def strobe(self, on=True, hold=STROBE_HOLD, hz=None):
        """Strobe full / off in one color while held. Each call with on=True keeps it going for
        `hold` more seconds, so a page that drops its connection can't leave the room strobing.
        While it runs, run() renders on the strobe's own clock: one frame per flip, evenly."""
        with self._lock:
            if hz:
                self.strobe_hz = max(1.0, min(STROBE_MAX_HZ, float(hz)))
            self._strobe_until = time.monotonic() + hold if on else 0.0
        self._wake.set()

    def strobing(self):
        return time.monotonic() < self._strobe_until

    def _strobe_frame(self, colors):
        """Replace a rendered frame with the strobe, flipping full / off on every frame (run()
        paces strobe frames at 2 x strobe_hz). The color is locked on the first frame: the
        brightest one showing, or the last lit one if the strip is dark, raised to full."""
        if self._strobe_color is None:
            c = max(colors, key=max)
            c = c if max(c) > 0 else self._last_lit
            k = 255.0 / max(1, max(c))
            self._strobe_color = tuple(int(round(v * k)) for v in c)
            self._strobe_on = True
        else:
            self._strobe_on = not self._strobe_on
        return [self._strobe_color if self._strobe_on else (0, 0, 0)] * self.n

    # ---- audio sources
    def use_audio(self, blocks, block, name="audio"):
        """blocks: iterator of (samplerate, float32 mono block). Analyzed on a worker thread."""
        self.source = name

        def run():
            an = None
            try:
                for sr, x in blocks:
                    if self._stop.is_set():
                        return
                    if an is None or an.sr != sr:  # live input reopened at a new rate
                        an = Analyzer(sr, N_BANDS, block)
                    if len(x) != block:
                        continue
                    f = an.process(x)
                    with self._lock:
                        self._feat = f
                        if f["beat"]:
                            self._beat = True
                        if f.get("onset"):
                            self._onset = True
                    if f["beat"] or f.get("onset"):
                        self._wake.set()
            except Exception as e:  # device unplugged etc.
                self.source = f"{name} (stopped: {e})"
            else:
                self.source = f"{name} (ended)"
            with self._lock:
                self._feat = blank_features()

        self._spawn(run)

    def use_demo(self, bpm=124.0):
        self._demo = DemoBeat(bpm)
        self.source = f"demo beat {bpm:g} BPM"

    def _features(self, t):
        if self._demo:
            return self._demo.features(t)
        with self._lock:
            f, beat, self._beat = self._feat, self._beat, False
            onset, self._onset = self._onset, False
        if onset and not f.get("onset"):
            f = dict(f, onset=True)          # an onset block was overwritten before this frame
        return f, beat

    # ---- light
    def connect(self):
        if not self.light_factory:
            return
        try:
            light = self.light_factory()
            light.power(True)
            light.rt_enable()
            self.light, self.light_error = light, None
        except Exception as e:
            self.light_error = str(e)
            self._spawn(self._reconnect)

    def _reconnect(self):
        while not self._stop.wait(5.0):
            try:
                light = self.light_factory()
                light.rt_enable()
                self.light, self.light_error = light, None
                self.stats["reconnects"] += 1
                return
            except Exception as e:
                self.light_error = str(e)

    def _send(self, colors):
        light = self.light
        if light is None or (self.idle and hasattr(light, "show_sketch")):
            return
        try:
            if hasattr(light, "set_mode"):        # e.g. the Rolling Bar preset wants "roll"
                want = getattr(light, "default_mode", None)
                if not self.strobing():               # the hold strobe needs whole-strip frames
                    want = self.renderer.scene.get("ble_mode") or want
                if want and want != light.mode:
                    light.set_mode(want)
                if light.mode == "roll_flash" and hasattr(light, "set_roll_colors"):
                    light.set_roll_colors(self._scene_palette())
            light.rt_frame(colors)
            self.stats["sent"] += 1
        except Exception as e:
            self.light_error = str(e)
            self.light = None
            try:
                light.close()
            except Exception:
                pass
            if self.light_factory:
                self._spawn(self._reconnect)

    # ---- loop
    def _update_idle(self, f, t):
        """Idle = real audio input silent for idle_after seconds, on a music-driven scene only."""
        if self.idle_after is None or self._demo:
            return
        quiet = f.get("level", 0.0) < SILENCE_RMS and not self.strobing()
        if not quiet or not scene_idles(self.renderer.scene):
            self._quiet_since = None
            if self.idle:
                self.idle = False
                self.stats["idle_exits"] += 1
                light = self.light
                if light is not None:
                    light.rt_enable()                 # frames repaint from scratch
            return
        if self._quiet_since is None:
            self._quiet_since = t
        if not self.idle and t - self._quiet_since >= self.idle_after:
            self.idle = True
            self.stats["idle_entries"] += 1
            light = self.light
            if light is not None and hasattr(light, "show_sketch"):
                cells = getattr(light, "cells", self.n)
                rainbow = [tuple(int(v * 255) for v in c)
                           for c in sample("rainbow", np.arange(cells) / cells)]
                self._spawn(lambda: self._safe(light.show_sketch, rainbow, IDLE_EFFECT, self.idle_speed))

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            self.light_error = str(e)

    def step(self, t, dt):
        """Render one frame (also used by tests). Returns the n RGB tuples."""
        f, beat = self._features(t)
        self._update_idle(f, t)
        with self._lock:
            if self.idle:
                # preview of the idle rainbow; with show_sketch the strip animates it itself
                x = np.arange(self.n) / max(1, self.n) + 0.02 * self.idle_speed * t / 10
                colors = [tuple(int(v * 255) for v in c) for c in sample("rainbow", x)]
            else:
                colors = self.renderer.render(f, t, dt, beat)
                lit = max(colors, key=max)
                if max(lit) > 0:
                    self._last_lit = lit
            if self.strobing() and not self.idle:
                colors = self._strobe_frame(colors)
            else:
                self._strobe_color = None
            self._live = {"colors": [rgb_to_hex(np.array(c) / 255.0) for c in colors],
                          "features": {k: round(float(f.get(k, 0.0)), 3)
                                       for k in ("bass", "bass_slow", "mid", "high", "energy", "beat_env")},
                          "bands": [round(float(b), 3) for b in f.get("bands", [])],
                          "beat": bool(beat)}
        self.stats["frames"] += 1
        return colors

    def run(self):
        """Blocking render loop until stop() or Ctrl-C."""
        t0 = last = last_send = time.monotonic()
        next_frame = t0
        fps_t, fps_n = t0, 0
        strobe_clock = False
        try:
            while not self._stop.is_set():
                timeout = max(0.0, next_frame - time.monotonic())
                woke = self._wake.wait(timeout)
                self._wake.clear()
                if self.strobing():
                    if not strobe_clock:                 # first flash now, then an even clock
                        strobe_clock, woke = True, False
                        next_frame = time.monotonic()
                    elif woke:
                        continue                         # beats wait: flashes stay evenly spaced
                elif strobe_clock:
                    strobe_clock = False
                now = time.monotonic()
                if now - last_send < MIN_GAP:
                    time.sleep(MIN_GAP - (now - last_send))
                    now = time.monotonic()
                colors = self.step(now - t0, now - last)
                self._send(colors)
                last = last_send = now
                fps_n += 1
                if now - fps_t >= 1.0:
                    self._live_fps = fps_n / (now - fps_t)
                    fps_t, fps_n = now, 0
                if not woke:
                    next_frame += 0.5 / self.strobe_hz if strobe_clock else 1.0 / self.fps
                    if now - next_frame > 0.25:   # fell far behind (sleep, debugger): resync
                        next_frame = now
        except KeyboardInterrupt:
            pass

    def live(self):
        with self._lock:
            d = dict(self._live)
        d["fps"] = round(getattr(self, "_live_fps", 0.0), 1)
        d["source"] = self.source
        d["light"] = type(self.light).__name__ if self.light else None
        d["light_error"] = self.light_error
        d["idle"] = self.idle
        d["strobe"] = self.strobing()
        d["strobe_hz"] = self.strobe_hz
        d["stats"] = dict(self.stats)
        if self.light is not None and hasattr(self.light, "stats"):
            d["link"] = dict(self.light.stats)
        return d

    def _spawn(self, fn):
        th = threading.Thread(target=fn, daemon=True)
        th.start()
        self._threads.append(th)

    def start(self):
        """Run the loop on a background thread (studio mode)."""
        self._run_thread = threading.Thread(target=self.run, daemon=True)
        self._run_thread.start()

    def stop(self):
        """Stop the loop, then leave real-time mode so the strip returns to its own mode."""
        self._stop.set()
        self._wake.set()
        th = getattr(self, "_run_thread", None)
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2)
        light, self.light = self.light, None
        if light is not None:
            try:
                light.rt_disable()
            finally:
                light.close()
