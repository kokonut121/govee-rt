"""Audio analysis (FFT bands, bass envelope, onset/beat detection) and audio sources."""

import collections
import math
import queue
import sys
import time
import wave

import numpy as np

from . import macaudio


# --------------------------------------------------------------------------- audio analysis

class Analyzer:
    """
    Turns raw audio blocks into features:
      bands[]  : log-spaced spectrum, AGC'd and envelope-followed, 0..1, len == n_bands
      bass     : 0..1 low-frequency level (40..150 Hz), envelope-followed
      bass_slow: 0..1 raw bass envelope over a slow peak (holds 2 s, then decays over ~20 s).
                 Not AGC'd, so it stays low when the kick stops; drives the dark state.
      beat_share: share of the last onset's spectral rise that sat in the bass band (40..150 Hz)
                 vs 300..8000 Hz: ~0.2..1 for a kick, under 0.1 for a clap or pluck. Held until
                 the next beat, so a render frame that sees `beat` can read it.
      trill    : 0..1, a held tonal note (voice, lead) that trills 3..14 times a second: its pitch
                 alternates (>= ~0.3 semitone either way) or the vocal range pulses >= 8 dB deep
                 while the loud halves stay on one note
      trill_up : True on the upper / louder half of each trill cycle (flips each half cycle)
      mid      : mean of the bands centered 250 Hz..2 kHz (vocals, snare body, synths)
      high     : mean of the bands centered above 4 kHz (hats, cymbals, air)
      energy   : mean of all bands
      beat     : True on the block where an onset was detected (spectral flux on the bass band)
      beat_env : 1.0 at a beat, decays exponentially (use for flashes)
      onset    : True on any bass onset, with a short ONSET_GAP refractory instead of beat's 250 ms,
                 so fast runs of kicks (16ths, rolls) each register; `beat` stays as it was
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
        centers = np.sqrt(edges[:-1] * edges[1:])
        self.mid_bands = np.where((centers >= 250) & (centers < 2000))[0]
        self.high_bands = np.where(centers >= 4000)[0]

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
        self.last_onset = -1.0
        self.beat_env = 0.0
        self.beat_decay = math.exp(-dt / 0.25)
        self.t = 0.0
        self.silence = 1e-4
        self.slow_env = 0.0
        self.slow_peak = self.SLOW_FLOOR
        self.slow_peak_t = 0.0
        self.slow_hold = 2.0
        self.slow_decay = math.exp(-dt / 20.0)

        # trill: pitch of the strongest partial 250..4000 Hz on a quarter-semitone grid
        self.freqs = freqs
        self.v_grid = np.geomspace(250.0, 4000.0, 193)
        self.v_bins = np.where((freqs >= 250) & (freqs < 4000))[0]
        self.v_peak = 3 * self.silence
        self.v_peak_t = 0.0
        self.pitch_hist = collections.deque(maxlen=max(8, int(round(0.5 / dt))))
        self.trill_side = 1
        self.trill_flips = collections.deque()
        self.pulse_hist = collections.deque(maxlen=max(8, int(round(0.35 / dt))))
        self.pulse_note = None      # the note a confirmed pulse trill is locked to
        self.pulse_ok = 0
        self.dt = dt
        self.upper_bins = np.where((freqs >= 300) & (freqs < 8000))[0]
        self.prev_mag = None
        self.beat_share = 1.0

    def process(self, samples: np.ndarray):
        """samples: float32 mono block of length self.block, range -1..1."""
        self.t += self.block / self.sr
        level = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))   # raw RMS, for idle detection
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

        # slow-peak bass: same attack/release as the bass envelope, on the raw level
        k = self.att if bass_raw > self.slow_env else self.rel
        self.slow_env += (bass_raw - self.slow_env) * k
        if self.slow_env >= self.slow_peak:
            self.slow_peak, self.slow_peak_t = self.slow_env, self.t
        elif self.t - self.slow_peak_t > self.slow_hold:
            self.slow_peak *= self.slow_decay
        self.slow_peak = max(self.slow_peak, self.SLOW_FLOOR)
        bass_slow = min(1.0, self.slow_env / self.slow_peak)

        flux = max(0.0, bass_raw - self.prev_bass)
        self.prev_bass = bass_raw
        self.flux_hist.append(flux)
        if len(self.flux_hist) > self.flux_hist_len:
            self.flux_hist.pop(0)
        h = np.array(self.flux_hist)
        thresh = h.mean() + 1.5 * h.std() if len(h) > 8 else float("inf")
        onset = (flux > thresh and bass_raw > 0.15 * self.bass_ceiling
                 and (self.t - self.last_onset) > self.ONSET_GAP and bass_raw > 3 * self.silence)
        beat = onset and (self.t - self.last_beat) > 0.25
        if onset:
            self.last_onset = self.t
            if self.prev_mag is not None:
                rise = np.maximum(0.0, mag - self.prev_mag)
                b, u = float(rise[self.bass_bins].sum()), float(rise[self.upper_bins].sum())
                self.beat_share = b / (b + u + 1e-12)
        if beat:
            self.last_beat = self.t
            self.beat_env = 1.0
        else:
            self.beat_env *= self.beat_decay

        self.prev_mag = mag
        trill, trill_up = self._trill(mag)

        bands = np.clip(self.env, 0, 1)
        return {
            "bands": bands,
            "bass": max(0.0, min(1.0, self.bass_env)),
            "bass_slow": bass_slow,
            "beat_share": self.beat_share,
            "trill": trill,
            "trill_up": trill_up,
            "mid": float(bands[self.mid_bands].mean()) if len(self.mid_bands) else 0.0,
            "high": float(bands[self.high_bands].mean()) if len(self.high_bands) else 0.0,
            "energy": float(bands.mean()),
            "beat": beat,
            "onset": onset,
            "beat_env": self.beat_env,
            "level": level,
        }


    # bass_slow's peak never drops under this raw level (sqrt of FFT magnitude; a mastered kick
    # reads ~0.3..0.5). Without it, after a long silence or at the very start of a song, pad and
    # hat leakage into 40..150 Hz (~0.02..0.05) would read as full bass.
    SLOW_FLOOR = 0.15
    ONSET_GAP = 0.09        # s; one kick's attack can span two blocks, a 16th at 160 BPM is 0.094

    TRILL_MIN_ST = 0.3      # semitones either side of the center that count as a trill
    TRILL_HYST_ST = 0.2     # pitch must pass center +- this to flip sides (no chatter)
    TRILL_RATE = (3.0, 14.0)
    PULSE_HYST_DB = 3.0     # the note's level must pass its median +- this to flip loud/quiet
    PULSE_MIN_DB = 8.0      # peak-to-trough depth that counts as a trill, not a wobble
    PULSE_SAME_NOTE = 0.6   # share of the loud frames whose top partial is the same note
    PULSE_FLIPS = 4         # loud/quiet flips (two cycles) before it counts, then it latches

    def _trill(self, mag):
        """A held note that trills: its pitch swings around its own center, or its loudness
        pulses (the owner's "desireeee" in Ian Asher's Desire: a steady B5 pulsing ~8.6 times a
        second, 20 dB deep), 3..14 times a second. Needs a tonal frame (a clear spectral peak)
        loud against the recent vocal-band peak; noise, hats and steady pads read as 0.
        Returns (strength 0..1, upper/louder half of the cycle)."""
        v_raw = float(np.sqrt(mag[self.v_bins].mean())) if len(self.v_bins) else 0.0
        if v_raw >= self.v_peak:
            self.v_peak, self.v_peak_t = v_raw, self.t
        elif self.t - self.v_peak_t > self.slow_hold:
            self.v_peak *= self.slow_decay
        self.v_peak = max(self.v_peak, 3 * self.silence)
        spec = np.interp(self.v_grid, self.freqs, mag)
        i = int(np.argmax(spec))
        floor = np.median(spec) + 1e-12
        tonal = v_raw >= 0.3 * self.v_peak and spec[i] > 6 * floor and 0 < i < len(spec) - 1
        pulse, loud = self._pulse_trill(spec, i, tonal, floor)
        swing, high = self._pitch_trill(spec, i, tonal)
        if pulse >= swing:
            return pulse, loud
        return swing, high

    def _pulse_trill(self, spec, i, tonal, floor):
        """The loudness of one held note pulsing. The note can sit in a chord (Desire: a B5 over
        other voices that take over the top in every quiet half), so the note is the pitch that
        tops the most frames of the last ~0.35 s, and its own level is followed against its
        median. Needs >= 3 loud/quiet flips at 3..14 Hz spacing, >= PULSE_MIN_DB deep."""
        self.pulse_hist.append((spec, i if tonal else -1))
        tops = [(k, sp[k]) for sp, k in self.pulse_hist if k >= 0]
        if len(tops) < 4:
            self.pulse_note = None
            return 0.0, False
        amps = np.array([a for _, a in tops])
        loud = np.array([k for k, a in tops if a >= np.median(amps)])     # other voices top the dips
        note = int(np.bincount(loud).argmax())
        if np.mean(np.abs(loud - note) <= 2) < self.PULSE_SAME_NOTE:
            self.pulse_note = None
            return 0.0, False                                              # a melody, not one note
        first = next(n for n, (_, k) in enumerate(self.pulse_hist) if k >= 0 and abs(k - note) <= 2)
        lo, hi = max(0, note - 2), note + 3
        lvl = np.array([20 * math.log10(sp[lo:hi].max() + 1e-12)
                        for sp, _ in list(self.pulse_hist)[first:]])          # since the note began
        if len(lvl) < 5:
            return 0.0, False
        center = float(np.median(lvl))
        side, flips = 0, []
        for k, v in enumerate(lvl):
            new = 1 if v > center + self.PULSE_HYST_DB else -1 if v < center - self.PULSE_HYST_DB else side
            if side and new != side:
                flips.append(k)
            side = new
        gaps = [(b - a) * self.dt for a, b in zip(flips, flips[1:])]
        half_lo, half_hi = 0.5 / self.TRILL_RATE[1], 0.5 / self.TRILL_RATE[0]
        recent = bool(flips) and (len(lvl) - 1 - flips[-1]) * self.dt <= half_hi
        depth = float(np.percentile(lvl, 90) - np.percentile(lvl, 10))
        up = lvl[-1] > center
        strength = float(min(1.0, (depth - 4.0) / 10.0))
        still = any(k >= 0 and abs(k - note) <= 2 for _, k in list(self.pulse_hist)[-4:])
        if self.pulse_note is not None and abs(note - self.pulse_note) <= 2 and recent and still:
            return strength, up                                            # latched: ride it out
        regular = (len(flips) >= self.PULSE_FLIPS and recent
                   and all(half_lo * 0.7 <= g <= half_hi for g in gaps))
        if not regular or depth < self.PULSE_MIN_DB:
            self.pulse_note, self.pulse_ok = None, 0
            return 0.0, up
        self.pulse_ok += 1
        if self.pulse_ok < 2:                                              # one frame is a blip
            return 0.0, up
        self.pulse_note = note
        return strength, up

    def _pitch_trill(self, spec, i, tonal):
        """Pitch of the loudest partial swinging around its own center."""
        pitch = None
        if tonal:
            a, b, c = np.log(spec[i - 1:i + 2] + 1e-12)
            den = a - 2 * b + c
            pitch = (i + (0.5 * (a - c) / den if den < 0 else 0.0)) / 4.0   # semitones above 250 Hz
            prev = self.pitch_hist[-1] if self.pitch_hist else None
            if prev is not None and abs(pitch - prev) > 5.0:
                self.pitch_hist.clear()                                    # jumped to another partial
        if pitch is None:
            self.pitch_hist.clear()
            self.trill_flips.clear()
            return 0.0, False
        self.pitch_hist.append(pitch)
        if len(self.pitch_hist) < self.pitch_hist.maxlen * 0.6:
            return 0.0, False
        p = np.array(self.pitch_hist)
        d = pitch - p.mean()
        if self.trill_side > 0 and d < -self.TRILL_HYST_ST:
            self.trill_side = -1
            self.trill_flips.append(self.t)
        elif self.trill_side < 0 and d > self.TRILL_HYST_ST:
            self.trill_side = 1
            self.trill_flips.append(self.t)
        span = len(self.pitch_hist) * self.dt
        while self.trill_flips and self.t - self.trill_flips[0] > span:
            self.trill_flips.popleft()
        rate = len(self.trill_flips) / (2 * span)                          # full cycles per second
        amp = float(np.sqrt(np.mean((p - p.mean()) ** 2)))
        if not (self.TRILL_RATE[0] <= rate <= self.TRILL_RATE[1]) or amp < self.TRILL_MIN_ST:
            return 0.0, self.trill_side > 0
        return float(min(1.0, (amp - 0.2) / 0.6)), self.trill_side > 0


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


STALL_S = 1.0       # no block from the input for this long: reopen the stream
RATE_CHECK_S = 2.0  # how often to compare the capture rate with the default output's


def _sync_rate(device):
    """Move a capture device that mirrors the default output (BlackHole in a Multi-Output
    Device) to the output's rate. Returns the device's rate per CoreAudio, or None if unknown
    (not macOS, not found by name). PortAudio caches rates at startup, so this is the truth."""
    if not isinstance(device, str):
        return None
    dev, want = macaudio.output_rate_for(device)
    if dev is None:
        return None
    have = macaudio.rate(dev)
    if want and have != want and macaudio.set_rate(dev, want):
        print(f"  audio: {device} {have:g} Hz -> {want:g} Hz to match the speaker", file=sys.stderr)
        have = macaudio.rate(dev)
    return int(have) if have else None


def live_blocks(device, block, sr=None):
    """Yield (sr, float32 mono block) from an input device, forever.

    Without an explicit `sr`, the device is kept at the default output's rate when it is part
    of it: a Bluetooth speaker that reconnects at another rate leaves BlackHole mismatched and
    the Multi-Output Device stutters, so the stream is closed, the device moved, and reopened.
    A stream that stops delivering (CoreAudio rebuilt the device under it) is reopened too.
    One that keeps delivering at another buffer size (AUHAL after err -10863) is regrouped
    into exact `block`-sample blocks; the engine drops any other length, which froze the
    analysis with no error. The yielded sr can change between blocks."""
    import sounddevice as sd
    reopen = False
    while True:
        if reopen:
            try:  # re-read PortAudio's device list; it is a snapshot from the first import
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        rate = int(sr) if sr else _sync_rate(device)
        info = sd.query_devices(device, "input")
        rate = rate or int(info["default_samplerate"])
        ch = min(2, int(info["max_input_channels"]))
        q = queue.Queue(maxsize=8)

        def cb(indata, frames, t, status):
            if status:
                print(f"  audio: {status}", file=sys.stderr)
            try:
                q.put_nowait(indata.mean(axis=1).astype(np.float32).copy())
            except queue.Full:
                pass

        with sd.InputStream(device=device, channels=ch, samplerate=rate, blocksize=block,
                            dtype="float32", callback=cb):
            checked = time.monotonic()
            buf, warned = np.zeros(0, np.float32), False
            while True:
                try:
                    x = q.get(timeout=STALL_S)
                except queue.Empty:
                    print(f"  audio: no input for {STALL_S:g} s, reopening", file=sys.stderr)
                    break
                if len(x) != block and not warned:
                    warned = True
                    print(f"  audio: device now delivers {len(x)}-frame buffers, regrouping into "
                          f"{block}", file=sys.stderr)
                buf = np.concatenate((buf, x)) if len(buf) else x
                while len(buf) >= block:
                    out, buf = buf[:block], buf[block:]
                    yield rate, out
                now = time.monotonic()
                if not sr and now - checked >= RATE_CHECK_S:
                    checked = now
                    _, want = macaudio.output_rate_for(device) if isinstance(device, str) else (None, None)
                    if want and int(want) != rate:
                        print(f"  audio: output now {want:g} Hz, reopening", file=sys.stderr)
                        break
        reopen = True
