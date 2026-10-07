"""Layered, music-reactive effects.

A scene is a stack of layers. Each layer runs one effect on a slice of the strip, and its
output is blended onto the layers below it. Every numeric parameter (and each layer's opacity,
and the master brightness) can be modulated by an audio feature or an LFO:

    value = clamp(base + amount * source * (max - min), min, max)

Scenes are plain JSON so the studio UI, presets on disk and the CLI share one format:

    {"layers": [{"effect": "gradient", "params": {"palette": "neon", "speed": 0.1},
                 "mod": {"speed": {"src": "bass", "amt": 0.5}},
                 "blend": "normal", "opacity": 1.0, "start": 0.0, "end": 1.0,
                 "reverse": false, "mirror": false, "enabled": true}],
     "master": {"brightness": 1.0, "gamma": 1.8, "mod": {}}}

Effects render n segments as float RGB 0..1 (perceptual) plus alpha 0..1; gamma is applied once
at the end. Govee strips expose a dozen or so segments, so effects are written to read well at
that resolution: anti-aliased positions, no single-pixel detail.
"""

import copy
import math
import zlib

import numpy as np

# --------------------------------------------------------------------------- palettes

PALETTES = {
    "rainbow": ["#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff"],
    "neon": ["#ff00aa", "#00ffe1", "#7b00ff"],
    # 8 neon colors ordered so neighbours contrast: for stepping a color per hit (Neon Bass)
    "party": ["#ff00aa", "#00e5ff", "#8000ff", "#39ff14", "#ff5a00", "#0050ff", "#ffe600", "#00ff9d"],
    "synthwave": ["#2b0057", "#ff00c8", "#00e0ff", "#ffb000"],
    "sunset": ["#ff3c00", "#ff0055", "#7a00ff"],
    "ocean": ["#001a40", "#0066ff", "#00e5ff"],
    "ice": ["#ffffff", "#7fdfff", "#0040ff"],
    "forest": ["#003300", "#22aa22", "#aaff00"],
    "lava": ["#300000", "#ff0000", "#ff6a00"],
    "fire": ["#000000", "#ff2000", "#ff8000", "#ffd060"],
    "meter": ["#00ff40", "#ffee00", "#ff0000"],
    "pastel": ["#ffb3ba", "#ffdfba", "#baffc9", "#bae1ff"],
    "white": ["#ffffff"],
}

SOURCES = ("bass", "mid", "high", "energy", "beat", "lfo", "lfo_slow")
BLENDS = ("normal", "add", "screen", "max", "multiply")


def hex_to_rgb(h):
    h = str(h).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"bad color {h!r}")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


def rgb_to_hex(c):
    r, g, b = (int(round(max(0.0, min(1.0, float(v))) * 255)) for v in c)
    return f"#{r:02x}{g:02x}{b:02x}"


def palette_stops(p):
    """p: a palette name or a list of hex colors -> (k, 3) array."""
    stops = PALETTES.get(p, None) if isinstance(p, str) else p
    if not stops:
        stops = PALETTES["rainbow"]
    return np.array([hex_to_rgb(s) for s in stops])


def sample(p, x, wrap=True):
    """Colors at positions x (array, 0..1) along palette p. wrap: last stop blends back to the first."""
    stops = palette_stops(p)
    x = np.asarray(x, dtype=np.float64)
    k = len(stops)
    if k == 1:
        return np.repeat(stops, x.size, axis=0).reshape(x.shape + (3,))
    if wrap:
        pos = (x % 1.0) * k
        i0 = np.floor(pos).astype(int) % k
        i1 = (i0 + 1) % k
    else:
        pos = np.clip(x, 0.0, 1.0) * (k - 1)
        i0 = np.minimum(np.floor(pos).astype(int), k - 2)
        i1 = i0 + 1
    f = (pos - np.floor(pos))[..., None] if wrap else (pos - i0)[..., None]
    return stops[i0] * (1 - f) + stops[i1] * f


def hsv_array(h, s, v):
    """Vectorized HSV -> RGB, all inputs broadcastable, returns (..., 3)."""
    h = np.asarray(h, dtype=np.float64) % 1.0
    s = np.clip(np.asarray(s, dtype=np.float64), 0, 1)
    v = np.clip(np.asarray(v, dtype=np.float64), 0, 1)
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


# --------------------------------------------------------------------------- parameters

def F(name, default, lo, hi, step=0.01, label=None):
    return {"name": name, "kind": "float", "default": default, "min": lo, "max": hi, "step": step,
            "label": label or name.replace("_", " ")}


def COLOR(name, default, label=None):
    return {"name": name, "kind": "color", "default": default, "label": label or name}


def PAL(name="palette", default="rainbow", label=None):
    return {"name": name, "kind": "palette", "default": default, "label": label or name}


def BOOL(name, default, label=None):
    return {"name": name, "kind": "bool", "default": default, "label": label or name.replace("_", " ")}


def CHOICE(name, default, options, label=None):
    return {"name": name, "kind": "choice", "default": default, "options": list(options),
            "label": label or name}


def SEGMENTS(name, default, label=None):
    return {"name": name, "kind": "segments", "default": default, "label": label or name}


class Ctx:
    """What an effect sees each frame."""

    def __init__(self, t, dt, f, beat):
        self.t = t          # seconds since start
        self.dt = dt        # seconds since previous frame
        self.f = f          # features: bass, mid, high, energy, beat_env (0..1), bands (array)
        self.beat = beat    # True on the frame a beat landed


class Effect:
    name = ""
    label = ""
    params = []
    reactive = False    # driven by the music: dark or frozen without it (see scene_idles)

    def __init__(self, n, seed=0):
        self.n = n
        self.rng = np.random.default_rng(seed)
        self.i = np.arange(n, dtype=np.float64)
        self.reset()

    def reset(self):
        pass

    def render(self, c, p):
        """Return rgb (n, 3) in 0..1, or (rgb, alpha) with alpha (n,) in 0..1."""
        raise NotImplementedError


EFFECTS = {}


def register(cls):
    EFFECTS[cls.name] = cls
    return cls


def follow(cur, target, dt, rise, fall):
    """One-pole smoothing with separate rise and fall times (seconds; 0 = jump)."""
    tau = rise if target > cur else fall
    if tau <= 0:
        return target
    return cur + (target - cur) * (1 - math.exp(-dt / tau))


# --------------------------------------------------------------------------- effects

@register
class Solid(Effect):
    name, label = "solid", "Solid color"
    params = [COLOR("color", "#ff6000")]

    def render(self, c, p):
        return np.tile(hex_to_rgb(p["color"]), (self.n, 1))


@register
class Paint(Effect):
    name, label = "paint", "Paint segments"
    params = [SEGMENTS("colors", ["#ff0000", "#000000"], label="segment colors")]

    def render(self, c, p):
        cols = p["colors"] or ["#000000"]
        m = len(cols)
        idx = np.minimum(m - 1, ((self.i + 0.5) * m / self.n).astype(int))
        return np.array([hex_to_rgb(cols[k]) for k in idx])


@register
class Gradient(Effect):
    name, label = "gradient", "Gradient scroll"
    params = [PAL(default="sunset"), F("spread", 1.0, 0.1, 4.0, label="cycles across strip"),
              F("speed", 0.1, -2.0, 2.0, label="scroll (cycles/s)")]

    def reset(self):
        self.phase = 0.0

    def render(self, c, p):
        self.phase += p["speed"] * c.dt
        return sample(p["palette"], self.i / self.n * p["spread"] + self.phase)


@register
class Rainbow(Effect):
    name, label = "rainbow", "Rainbow"
    params = [F("spread", 1.0, 0.0, 4.0, label="cycles across strip"),
              F("speed", 0.2, -2.0, 2.0, label="hue speed (cycles/s)"),
              F("saturation", 1.0, 0.0, 1.0)]

    def reset(self):
        self.phase = 0.0

    def render(self, c, p):
        self.phase += p["speed"] * c.dt
        return hsv_array(self.i / self.n * p["spread"] + self.phase, p["saturation"], 1.0)


@register
class Spectrum(Effect):
    name, label = "spectrum", "Spectrum bars"
    reactive = True
    params = [PAL(default="rainbow"), F("gain", 1.2, 0.2, 4.0), F("floor", 0.0, 0.0, 0.5),
              BOOL("bass_flash", True, label="flash bass end on beat")]

    def render(self, c, p):
        bands = np.asarray(c.f.get("bands", np.zeros(1)), dtype=np.float64)
        x = (self.i + 0.5) / self.n * len(bands) - 0.5
        lvl = np.interp(x, np.arange(len(bands)), bands)
        lvl = np.clip(lvl * p["gain"], 0, 1) ** 1.4
        if p["bass_flash"]:
            k = max(1, self.n // 6)
            lvl[:k] = np.maximum(lvl[:k], c.f.get("beat_env", 0.0))
        lvl = p["floor"] + (1 - p["floor"]) * lvl
        return sample(p["palette"], self.i / max(1, self.n), wrap=False) * lvl[:, None]


@register
class Meter(Effect):
    name, label = "meter", "Level meter"
    reactive = True
    params = [CHOICE("source", "bass", ("bass", "mid", "high", "energy")), PAL(default="meter"),
              F("gain", 1.0, 0.2, 3.0), BOOL("peak", True, label="peak hold"),
              F("background", 0.04, 0.0, 0.5)]

    def reset(self):
        self.level = 0.0
        self.peak = 0.0

    def render(self, c, p):
        target = min(1.0, c.f.get(p["source"], 0.0) * p["gain"])
        self.level += (target - self.level) * min(1.0, c.dt / (0.03 if target > self.level else 0.15))
        self.peak = max(target, self.peak - 0.6 * c.dt)
        fill = np.clip(self.level * self.n - self.i, 0, 1)
        rgb = sample(p["palette"], self.i / max(1, self.n - 1), wrap=False)
        a = np.maximum(fill, p["background"])
        if p["peak"] and self.peak > 0.02:
            pk = min(self.n - 1, int(self.peak * self.n))
            a[pk] = 1.0
        return rgb, a


@register
class Pulse(Effect):
    name, label = "pulse", "Beat pulse"
    reactive = True
    params = [PAL(default="neon"), F("decay", 0.3, 0.03, 1.5, label="decay (s)"),
              F("attack", 0.03, 0.0, 0.2, label="rise (s)"),
              F("step", 0.17, 0.0, 0.5, label="palette step per beat"),
              F("glide", 0.25, 0.0, 1.5, label="color glide (s)"), F("floor", 0.1, 0.0, 1.0)]

    def reset(self):
        self.pos = self.target = 0.0
        self.kick = self.env = 0.0

    def render(self, c, p):
        if c.beat:
            self.target += p["step"]
            self.kick = 1.0
        else:
            self.kick *= math.exp(-c.dt / p["decay"])
        self.env = follow(self.env, self.kick, c.dt, p["attack"], 0.0)
        self.pos = follow(self.pos, self.target, c.dt, p["glide"], p["glide"])
        v = p["floor"] + (1 - p["floor"]) * self.env
        return np.tile(sample(p["palette"], np.array([self.pos]))[0] * v, (self.n, 1))


@register
class BassPulse(Effect):
    """Clean two-level pulse on bass hits: rests at `floor`, snaps to full on a hit, holds at
    least `hold`, snaps back. The owner found smoothed envelopes read as flicker (they follow
    every wobble of the bass), so by default nothing is in between and the strip only gets a
    new color when the state flips. The color steps to the next palette stop on each hit
    (`color_every`), at the same instant as the flash.

    The analyzer auto-levels the bass band, so most music keeps it in a narrow band near the
    top (0.7..1.0 on the owner's tracks). A hit is the bass reaching the top part (`threshold`)
    of its own recent low..high range; it must fall back below threshold - HYSTERESIS to end,
    so a level hovering at the threshold can't chatter.

    Dark state: when the analyzer's `bass_slow` (raw bass over a slow peak, which does not
    inflate when the kick stops) stays under `dark_below` for `dark_after` seconds, the output
    snaps to black whatever the floor is (a build-up). It leaves dark on a detected beat with
    real bass behind it, or when `bass_slow` climbs past `dark_below` + 0.1; that first kick of
    the drop is a hit (full, color step, same frame) and the recent low/high followers restart
    at the current bass so the next kick triggers too.

    Three tiers (owner, 2026-10-07): with `strong` > 0 a hit is full only when `bass_slow` is
    at least `strong` (a hit as loud as the recent loudest kicks); softer hits light at `soft`.
    The tier is picked on the hit frame and can only step up during the hold, never down.
    With `floor` 0 that gives off between kicks, mid on soft kicks, full on strong ones."""
    name, label = "bass_pulse", "Bass pulse"
    reactive = True
    params = [PAL(default="sunset"),
              F("threshold", 0.70, 0.0, 0.95, label="only the top part of the bass (0..1)"),
              F("hold", 0.12, 0.03, 0.6, label="minimum flash length (s)"),
              F("floor", 0.45, 0.0, 0.9, label="brightness between hits"),
              F("strong", 0.0, 0.0, 1.0, label="full only if slow bass is over (0 = every hit full)"),
              F("soft", 0.45, 0.0, 1.0, label="brightness of hits under `strong`"),
              F("release", 0.0, 0.0, 1.0, label="fade back (s), 0 = instant"),
              BOOL("kick_boost", True, label="also flash on every detected beat"),
              F("color_every", 1, 0, 16, 1, label="next palette color every N hits (0 = never)"),
              F("dark_below", 0.25, 0.0, 0.9, label="go dark when slow bass is under (0 = never)"),
              F("dark_after", 0.3, 0.0, 3.0, label="after this long under it (s)"),
              F("drift", 0.0, 0.0, 0.3, label="color drift (cycles/s)"),
              F("spread", 0.0, 0.0, 1.0, label="palette across strip")]

    LOW_RISE, LOW_FALL = 0.6, 0.03     # recent low: drops at once, creeps back up
    HIGH_RISE, HIGH_FALL = 0.01, 1.5   # recent high: jumps to peaks, sinks slowly
    MIN_SPAN = 0.06
    HYSTERESIS = 0.2
    MIN_GAP = 0.06                     # seconds off before the next flash can start
    DARK_EXIT = 0.1                    # bass_slow above dark_below + this leaves dark

    def reset(self):
        self.level = self.phase = self.t = 0.0
        self.lo = self.hi = None
        self.on = self.dark = False
        self.on_since = self.off_since = -1e9
        self.below_since = None
        self.hits = self.color_idx = 0
        self.top = 1.0

    def _tier(self, p, slow):
        return 1.0 if p["strong"] <= 0 or slow >= p["strong"] else p["soft"]

    def _hit(self, p, slow):
        self.on, self.on_since = True, self.t
        self.top = self._tier(p, slow)
        self.hits += 1
        every = int(round(p["color_every"]))
        if every > 0 and self.hits % every == 0:
            self.color_idx += 1              # the color flips together with the flash: one clean step

    def render(self, c, p):
        self.t += c.dt
        bass = c.f.get("bass", 0.0)
        if self.lo is None:
            self.lo = self.hi = bass
        self.lo = follow(self.lo, bass, c.dt, self.LOW_RISE, self.LOW_FALL)
        self.hi = follow(self.hi, bass, c.dt, self.HIGH_RISE, self.HIGH_FALL)
        pos = (bass - self.lo) / max(self.MIN_SPAN, self.hi - self.lo)   # 0 at recent low, 1 at recent high
        thr = p["threshold"]
        slow, below = c.f.get("bass_slow", 1.0), p["dark_below"]
        if self.dark:
            if (c.beat and slow >= below) or slow >= below + self.DARK_EXIT:
                self.dark, self.below_since = False, None
                self.lo = self.hi = bass       # re-arm the level trigger for the next kick
                self._hit(p, slow)             # the first kick of the drop comes back at full
        else:
            if below > 0 and slow < below:
                if self.below_since is None:
                    self.below_since = self.t
                if self.t - self.below_since >= p["dark_after"]:
                    self.dark, self.on, self.level = True, False, 0.0
            else:
                self.below_since = None
            trigger = pos >= thr or (c.beat and p["kick_boost"])
            if not self.dark:
                if not self.on and trigger and self.t - self.off_since >= self.MIN_GAP:
                    self._hit(p, slow)
                elif self.on and self._tier(p, slow) > self.top:
                    self.top = self._tier(p, slow)   # a kick still rising: step up, never down
                elif self.on and self.t - self.on_since >= p["hold"] and pos < thr - self.HYSTERESIS:
                    self.on, self.off_since = False, self.t
        if self.on or p["release"] <= 0:
            self.level = 1.0 if self.on else 0.0
        else:
            self.level = follow(self.level, 0.0, c.dt, 0.0, p["release"])
        self.phase += p["drift"] * c.dt
        v = 0.0 if self.dark else p["floor"] + (self.top - p["floor"]) * self.level
        stops = len(palette_stops(p["palette"]))
        x0 = self.phase + (self.color_idx / stops if int(round(p["color_every"])) > 0 else 0.0)
        return sample(p["palette"], x0 + p["spread"] * self.i / max(1, self.n)) * v


@register
class Comet(Effect):
    name, label = "comet", "Comets"
    params = [PAL(default="ice"), F("speed", 8.0, -60.0, 60.0, 0.5, label="speed (segments/s)"),
              F("tail", 0.25, 0.0, 1.0, label="tail length"), F("count", 1, 1, 6, 1),
              BOOL("bounce", False), F("beat_kick", 0.0, 0.0, 30.0, 0.5, label="jump on beat (segments)")]

    def reset(self):
        self.pos = 0.0

    def render(self, c, p):
        n = self.n
        self.pos += p["speed"] * c.dt + (p["beat_kick"] if c.beat else 0.0) * (1 if p["speed"] >= 0 else -1)
        count = max(1, int(round(p["count"])))
        tl = 0.3 + p["tail"] * n
        rgb = np.zeros((n, 3))
        a = np.zeros(n)
        for k in range(count):
            if p["bounce"]:
                span = max(1e-6, n - 1)
                u = (self.pos + k * 2 * span / count) % (2 * span)
                head, direction = (u, 1) if u < span else (2 * span - u, -1)
            else:
                head = (self.pos + k * n / count) % n
                direction = 1 if p["speed"] >= 0 else -1
            d = (head - self.i) * direction          # distance behind the head
            if not p["bounce"]:
                d = (d + n / 2) % n - n / 2
            v = np.where(d >= 0, np.exp(-d / tl), np.clip(1 + d, 0, 1))
            col = sample(p["palette"], np.array([k / count]))[0]
            take = v > a
            rgb[take] = col
            a = np.maximum(a, v)
        return rgb, a


@register
class Sparkle(Effect):
    name, label = "sparkle", "Sparkle"
    params = [PAL(default="white"), F("rate", 6.0, 0.0, 60.0, 0.5, label="sparks per second"),
              F("decay", 0.3, 0.05, 2.0, label="fade (s)"), F("burst", 3, 0, 16, 1, label="sparks on beat")]

    def reset(self):
        self.level = np.zeros(self.n)
        self.hue = np.zeros(self.n)

    def render(self, c, p):
        self.level *= math.exp(-c.dt / p["decay"])
        k = self.rng.poisson(p["rate"] * c.dt) + (int(p["burst"]) if c.beat else 0)
        for _ in range(k):
            j = self.rng.integers(self.n)
            self.level[j] = 1.0
            self.hue[j] = self.rng.random()
        return sample(p["palette"], self.hue), self.level.copy()


@register
class Fire(Effect):
    name, label = "fire", "Fire"
    params = [PAL(default="fire"), F("cooling", 0.55, 0.1, 1.5), F("sparking", 0.6, 0.0, 1.0),
              BOOL("from_both_ends", False)]

    def reset(self):
        self.heat = np.zeros(self.n)
        self.acc = 0.0

    def _step(self, p):
        n = self.n
        h = self.heat
        h -= self.rng.uniform(0, p["cooling"] * 0.12 * 15 / max(n, 4), n)
        np.clip(h, 0, 1, out=h)
        if n >= 3:
            h[2:] = (h[1:-1] + 2 * h[:-2]) / 3.0       # heat rises away from the base
        if self.rng.random() < p["sparking"]:
            j = self.rng.integers(0, max(1, min(3, n)))
            h[j] = min(1.0, h[j] + self.rng.uniform(0.5, 0.9))

    def render(self, c, p):
        self.acc += c.dt
        while self.acc > 1 / 60:                       # fixed 60 Hz simulation step
            self._step(p)
            self.acc -= 1 / 60
        h = self.heat
        if p["from_both_ends"]:
            half = (self.n + 1) // 2
            h = np.concatenate([self.heat[:half], self.heat[:self.n - half][::-1]])
        return sample(p["palette"], h, wrap=False)


@register
class Wave(Effect):
    name, label = "wave", "Sine wave"
    params = [PAL(default="ocean"), F("wavelength", 8.0, 1.0, 60.0, 0.5, label="wavelength (segments)"),
              F("speed", 0.5, -4.0, 4.0, label="speed (waves/s)"), F("sharpness", 2.0, 1.0, 8.0, 0.5)]

    def reset(self):
        self.phase = 0.0

    def render(self, c, p):
        self.phase += p["speed"] * c.dt
        v = (0.5 + 0.5 * np.sin(2 * np.pi * (self.i / p["wavelength"] - self.phase))) ** p["sharpness"]
        rgb = sample(p["palette"], self.i / max(1, self.n) + 0.1 * self.phase)
        return rgb, v


@register
class Ripple(Effect):
    name, label = "ripple", "Ripples"
    reactive = True
    params = [PAL(default="synthwave"), F("speed", 12.0, 1.0, 60.0, 0.5, label="speed (segments/s)"),
              F("width", 1.2, 0.3, 6.0, 0.1), F("decay", 0.8, 0.1, 3.0, label="fade (s)"),
              CHOICE("origin", "random", ("random", "center", "start")),
              F("rate", 0.0, 0.0, 8.0, 0.1, label="extra ripples per second")]

    def reset(self):
        self.ripples = []   # (center, age, palette position)
        self.count = 0

    def _spawn(self, origin):
        center = {"center": (self.n - 1) / 2, "start": 0.0}.get(origin, self.rng.uniform(0, self.n - 1))
        self.ripples.append([center, 0.0, (self.count * 0.23) % 1])
        self.count += 1

    def render(self, c, p):
        if c.beat:
            self._spawn(p["origin"])
        if self.rng.random() < p["rate"] * c.dt:
            self._spawn(p["origin"])
        rgb = np.zeros((self.n, 3))
        a = np.zeros(self.n)
        alive = []
        for r in self.ripples:
            r[1] += c.dt
            fade = math.exp(-r[1] / p["decay"])
            if fade < 0.02:
                continue
            alive.append(r)
            radius = p["speed"] * r[1]
            v = np.exp(-(((np.abs(self.i - r[0]) - radius) / p["width"]) ** 2)) * fade
            take = v > a
            rgb[take] = sample(p["palette"], np.array([r[2]]))[0]
            a = np.maximum(a, v)
        self.ripples = alive[-12:]
        return rgb, a


@register
class Strobe(Effect):
    name, label = "strobe", "Strobe"
    reactive = True
    params = [COLOR("color", "#ffffff"), BOOL("on_beat", True, label="flash on beat (else free-run)"),
              F("rate", 8.0, 1.0, 20.0, 0.5, label="free-run rate (Hz)"),
              F("hold", 0.06, 0.02, 0.3, label="flash length (s)")]

    def reset(self):
        self.since = 1e9

    def render(self, c, p):
        self.since = 0.0 if c.beat else self.since + c.dt
        if p["on_beat"]:
            on = self.since < p["hold"]
        else:
            on = (c.t * p["rate"]) % 1.0 < p["hold"] * p["rate"]
        return np.tile(hex_to_rgb(p["color"]), (self.n, 1)), np.full(self.n, 1.0 if on else 0.0)


@register
class Breathe(Effect):
    name, label = "breathe", "Breathe"
    params = [PAL(default="ocean"), F("period", 4.0, 0.5, 20.0, 0.1, label="period (s)"),
              F("floor", 0.1, 0.0, 1.0), F("drift", 0.05, 0.0, 0.5, label="palette drift (cycles/s)")]

    def render(self, c, p):
        v = p["floor"] + (1 - p["floor"]) * (0.5 - 0.5 * math.cos(2 * math.pi * c.t / p["period"]))
        return np.tile(sample(p["palette"], np.array([c.t * p["drift"]]))[0] * v, (self.n, 1))


@register
class Plasma(Effect):
    name, label = "plasma", "Plasma"
    params = [PAL(default="synthwave"), F("scale", 1.0, 0.1, 4.0), F("speed", 0.3, 0.0, 3.0)]

    def reset(self):
        self.phase = 0.0

    def render(self, c, p):
        self.phase += p["speed"] * c.dt
        x = self.i / max(1, self.n) * 6.0 * p["scale"]
        ph = self.phase
        v = (np.sin(x * 1.3 + ph * 2.1) + np.sin(x * 2.9 - ph * 1.7 + 1.3) + np.sin(x * 0.7 + ph * 0.9 + 2.2))
        return sample(p["palette"], v / 6.0 + 0.5 + ph * 0.05)


# --------------------------------------------------------------------------- scenes

LAYER_DEFAULTS = {"blend": "normal", "opacity": 1.0, "start": 0.0, "end": 1.0,
                  "reverse": False, "mirror": False, "enabled": True}


def default_params(effect):
    return {s["name"]: copy.deepcopy(s["default"]) for s in EFFECTS[effect].params}


def new_layer(effect, **kw):
    layer = {"effect": effect, "params": default_params(effect), "mod": {}}
    layer.update(copy.deepcopy(LAYER_DEFAULTS))
    for k, v in kw.items():
        if k == "params":
            layer["params"].update(v)
        else:
            layer[k] = v
    return layer


def default_scene():
    return {"name": "Bass Pulse",
            "layers": [new_layer("bass_pulse")],
            "master": {"brightness": 1.0, "gamma": 1.8, "mod": {}}}


def _clean_mod(mod, allowed):
    out = {}
    for k, m in (mod or {}).items():
        if k in allowed and isinstance(m, dict) and m.get("src") in SOURCES:
            out[k] = {"src": m["src"], "amt": max(-1.0, min(1.0, float(m.get("amt", 0.0))))}
    return out


def _clean_value(spec, v):
    kind = spec["kind"]
    if kind == "float":
        return max(spec["min"], min(spec["max"], float(v)))
    if kind == "bool":
        return bool(v)
    if kind == "color":
        hex_to_rgb(v)
        return str(v)
    if kind == "palette":
        if isinstance(v, str):
            return v if v in PALETTES else spec["default"]
        cols = [str(x) for x in v][:16]
        for x in cols:
            hex_to_rgb(x)
        return cols or spec["default"]
    if kind == "choice":
        return v if v in spec["options"] else spec["default"]
    if kind == "segments":
        cols = [str(x) for x in v][:255]
        for x in cols:
            hex_to_rgb(x)
        return cols
    raise ValueError(kind)


def scene_idles(scene):
    """Whether the engine may swap this scene for the idle rainbow after silence. Only scenes made
    entirely of music-driven effects (party modes that sit dark or frozen without music); a
    scene with a plasma, gradient, paint etc. keeps showing itself. `"idle": true/false` in the
    scene overrides."""
    if isinstance(scene.get("idle"), bool):
        return scene["idle"]
    layers = [l for l in scene.get("layers", []) if l.get("enabled", True)]
    return bool(layers) and all(getattr(EFFECTS.get(l.get("effect")), "reactive", False) for l in layers)


def normalize_scene(scene):
    """Validate and fill defaults. Raises ValueError on unknown effects or malformed values."""
    if not isinstance(scene, dict) or not isinstance(scene.get("layers", []), list):
        raise ValueError("scene must be an object with a layers list")
    out = {"name": str(scene.get("name", "untitled"))[:80], "layers": []}
    if isinstance(scene.get("idle"), bool):
        out["idle"] = scene["idle"]
    for raw in scene.get("layers", [])[:16]:
        eff = raw.get("effect")
        if eff not in EFFECTS:
            raise ValueError(f"unknown effect {eff!r}")
        specs = {s["name"]: s for s in EFFECTS[eff].params}
        params = default_params(eff)
        for k, v in (raw.get("params") or {}).items():
            if k in specs:
                params[k] = _clean_value(specs[k], v)
        floats = {k for k, s in specs.items() if s["kind"] == "float"} | {"opacity"}
        layer = {"effect": eff, "params": params, "mod": _clean_mod(raw.get("mod"), floats)}
        for k, d in LAYER_DEFAULTS.items():
            v = raw.get(k, d)
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, float):
                v = max(0.0, min(1.0, float(v)))
            else:
                v = v if v in BLENDS else d
            layer[k] = v
        if layer["end"] <= layer["start"]:
            layer["start"], layer["end"] = 0.0, 1.0
        if "id" in raw:
            layer["id"] = str(raw["id"])[:40]
        out["layers"].append(layer)
    m = scene.get("master") or {}
    out["master"] = {"brightness": max(0.0, min(1.0, float(m.get("brightness", 1.0)))),
                     "gamma": max(1.0, min(3.0, float(m.get("gamma", 1.8)))),
                     "mod": _clean_mod(m.get("mod"), {"brightness"})}
    return out


def mod_sources(f, t):
    return {"bass": f.get("bass", 0.0), "mid": f.get("mid", 0.0), "high": f.get("high", 0.0),
            "energy": f.get("energy", 0.0), "beat": f.get("beat_env", 0.0),
            "lfo": 0.5 + 0.5 * math.sin(2 * math.pi * 0.5 * t),
            "lfo_slow": 0.5 + 0.5 * math.sin(2 * math.pi * 0.05 * t)}


def modulate(base, lo, hi, m, src):
    if not m:
        return base
    return max(lo, min(hi, base + m["amt"] * src[m["src"]] * (hi - lo)))


def blend(dst, rgb, a, mode):
    a = a[:, None]
    if mode == "add":
        return np.minimum(1.0, dst + rgb * a)
    if mode == "screen":
        return 1 - (1 - dst) * (1 - rgb * a)
    if mode == "max":
        return np.maximum(dst, rgb * a)
    if mode == "multiply":
        return dst * (1 - a) + dst * rgb * a
    return dst * (1 - a) + rgb * a


class Renderer:
    """Turns a scene + audio features into n RGB tuples per frame. Not thread-safe; the engine
    serializes calls."""

    def __init__(self, n, scene=None):
        self.n = n
        self._inst = {}
        self.set_scene(scene or default_scene())

    def set_scene(self, scene):
        self.scene = normalize_scene(scene)

    def _span(self, layer):
        i0 = int(round(layer["start"] * self.n))
        i1 = max(i0 + 1, int(round(layer["end"] * self.n)))
        return i0, min(i1, self.n)

    def _instance(self, idx, layer, m):
        key = layer.get("id", idx)
        sig = (layer["effect"], m)
        inst = self._inst.get(key)
        if inst is None or inst[0] != sig:
            inst = (sig, EFFECTS[layer["effect"]](m, seed=zlib.crc32(str(key).encode())))
            self._inst[key] = inst
        return inst[1]

    def render_float(self, f, t, dt, beat):
        """Composited frame as (n, 3) floats 0..1 before master brightness and gamma."""
        n = self.n
        out = np.zeros((n, 3))
        src = mod_sources(f, t)
        c = Ctx(t, dt, f, beat)
        live = set()
        for idx, layer in enumerate(self.scene["layers"]):
            if not layer["enabled"]:
                continue
            i0, i1 = self._span(layer)
            span = i1 - i0
            m = (span + 1) // 2 if layer["mirror"] else span
            eff = self._instance(idx, layer, m)
            live.add(layer.get("id", idx))
            specs = EFFECTS[layer["effect"]].params
            p = dict(layer["params"])
            for s in specs:
                if s["kind"] == "float":
                    p[s["name"]] = modulate(p[s["name"]], s["min"], s["max"], layer["mod"].get(s["name"]), src)
            res = eff.render(c, p)
            rgb, a = res if isinstance(res, tuple) else (res, np.ones(m))
            if layer["mirror"]:
                rgb = np.concatenate([rgb, rgb[:span - m][::-1]])
                a = np.concatenate([a, a[:span - m][::-1]])
            if layer["reverse"]:
                rgb, a = rgb[::-1], a[::-1]
            opacity = modulate(layer["opacity"], 0.0, 1.0, layer["mod"].get("opacity"), src)
            out[i0:i1] = blend(out[i0:i1], np.clip(rgb, 0, 1), np.clip(a, 0, 1) * opacity, layer["blend"])
        for k in list(self._inst):
            if k not in live:
                del self._inst[k]
        return out

    def render(self, f, t, dt, beat):
        """Final frame: list of n (r, g, b) ints 0..255 with master brightness and gamma applied."""
        ms = self.scene["master"]
        b = modulate(ms["brightness"], 0.0, 1.0, ms["mod"].get("brightness"), mod_sources(f, t))
        out = np.clip(self.render_float(f, t, dt, beat) * b, 0, 1) ** ms["gamma"]
        return [tuple(int(v) for v in row) for row in np.round(out * 255)]


def catalog():
    """Everything the UI needs to build editors."""
    return {"effects": [{"name": c.name, "label": c.label, "params": c.params} for c in EFFECTS.values()],
            "palettes": PALETTES, "sources": list(SOURCES), "blends": list(BLENDS),
            "layer_defaults": LAYER_DEFAULTS}
