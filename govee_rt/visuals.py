"""The original fixed visual modes (spectrum, pulse, vu, wave) and the terminal preview."""

import math

from .colors import gamma, hsv


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


def preview_line(colors, f):
    ramp = " .:-=+*#%@"
    bar = "".join(ramp[min(9, int(sum(c) / 765 * 9.99))] for c in colors)
    flag = "BEAT" if f["beat"] else "    "
    return f"\r{flag} bass={f['bass']:.2f} |{bar}|"
