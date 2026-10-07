"""Audio analysis (FFT bands, bass envelope, onset/beat detection) and audio sources."""

import math
import queue
import sys
import time
import wave

import numpy as np


# --------------------------------------------------------------------------- audio analysis

class Analyzer:
    """
    Turns raw audio blocks into features:
      bands[]  : log-spaced spectrum, AGC'd and envelope-followed, 0..1, len == n_bands
      bass     : 0..1 low-frequency level (40..150 Hz), envelope-followed
      bass_slow: 0..1 raw bass envelope over a slow peak (holds 2 s, then decays over ~20 s).
                 Not AGC'd, so it stays low when the kick stops; drives the dark state.
      mid      : mean of the bands centered 250 Hz..2 kHz (vocals, snare body, synths)
      high     : mean of the bands centered above 4 kHz (hats, cymbals, air)
      energy   : mean of all bands
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
        self.beat_env = 0.0
        self.beat_decay = math.exp(-dt / 0.25)
        self.t = 0.0
        self.silence = 1e-4
        self.slow_env = 0.0
        self.slow_peak = 3 * self.silence
        self.slow_peak_t = 0.0
        self.slow_hold = 2.0
        self.slow_decay = math.exp(-dt / 20.0)

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
        self.slow_peak = max(self.slow_peak, 3 * self.silence)
        bass_slow = min(1.0, self.slow_env / self.slow_peak)

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

        bands = np.clip(self.env, 0, 1)
        return {
            "bands": bands,
            "bass": max(0.0, min(1.0, self.bass_env)),
            "bass_slow": bass_slow,
            "mid": float(bands[self.mid_bands].mean()) if len(self.mid_bands) else 0.0,
            "high": float(bands[self.high_bands].mean()) if len(self.high_bands) else 0.0,
            "energy": float(bands.mean()),
            "beat": beat,
            "beat_env": self.beat_env,
            "level": level,
        }


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
