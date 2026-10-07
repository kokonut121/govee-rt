"""
Generate a synthetic test track with a known beat grid so beat detection can be scored.

    python tests/make_test_wav.py [out.wav] [--bpm 128] [--seconds 12]

Content: a kick (150 -> 50 Hz sine sweep) on every beat, a noise hat on off-beats,
and a slowly breathing three-note pad (220/277/330 Hz). 48 kHz, stereo, 16-bit.
"""
import argparse
import wave

import numpy as np


def synth(bpm=128, seconds=12.0, sr=48000, seed=0, kick_gap=None, kick_gains=(1.0,)):
    """kick_gap=(t0, t1) drops the kicks that start in that window (a build-up); the hats and
    the pad keep playing. kick_gains is cycled over the kicks (e.g. (1.0, 0.4) alternates loud
    and soft). Returns (sr, x, kick times actually played)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * seconds)) / sr
    beat = 60.0 / bpm
    x = np.zeros_like(t)
    kicks = []
    for k in range(int(seconds / beat)):
        t0 = k * beat
        if not (kick_gap and kick_gap[0] <= t0 < kick_gap[1]):
            kicks.append(t0)
            idx = (t >= t0) & (t < t0 + 0.25)
            tt = t[idx] - t0
            f = 50 + 100 * np.exp(-tt * 25)
            x[idx] += 0.9 * kick_gains[k % len(kick_gains)] * np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-tt * 8)
        if k % 2 == 1:
            idx = (t >= t0) & (t < t0 + 0.06)
            x[idx] += 0.25 * rng.standard_normal(idx.sum()) * np.exp(-(t[idx] - t0) * 60)
    for f0 in (220, 277, 330):
        x += 0.08 * np.sin(2 * np.pi * f0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.25 * t))
    x /= np.abs(x).max() * 1.05
    return sr, x.astype(np.float32), np.array(kicks)


def write_wav(path, sr, x):
    pcm = (x * 32767).astype(np.int16)
    st = np.stack([pcm, pcm], axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(st.tobytes())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", nargs="?", default="test128.wav")
    p.add_argument("--bpm", type=float, default=128)
    p.add_argument("--seconds", type=float, default=12)
    a = p.parse_args()
    sr, x, kicks = synth(a.bpm, a.seconds)
    write_wav(a.out, sr, x)
    print(f"wrote {a.out}: {a.seconds}s at {a.bpm} BPM, {len(kicks)} kicks")
