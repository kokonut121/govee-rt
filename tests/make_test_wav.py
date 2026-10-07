"""
Generate a synthetic test track with a known beat grid so beat detection can be scored.

    python tests/make_test_wav.py [out.wav] [--bpm 128] [--seconds 12]

Content: a kick (150 -> 50 Hz sine sweep) on every beat, a noise hat on off-beats,
and a slowly breathing three-note pad (220/277/330 Hz). 48 kHz, stereo, 16-bit.
"""
import argparse
import wave

import numpy as np


def synth(bpm=128, seconds=12.0, sr=48000, seed=0, kick_gap=None, kick_gains=(1.0,), trill=None,
           kick_every=1, claps=0.0, bass_notes=(), kick_rolls=(), pulse=None, melody=None):
    """kick_gap=(t0, t1) drops the kicks that start in that window (a build-up); the hats and
    the pad keep playing. kick_gains is cycled over the kicks (e.g. (1.0, 0.4) alternates loud
    and soft). kick_every=4 keeps one kick per bar (slow bass). trill=(t0, t1, f0, semitones,
    rate) adds a sung-like note (5 harmonics) from t0 to t1 whose pitch alternates between f0
    and f0 + semitones, rate times a second; rate 0 holds the note still. claps=a adds
    16th-note noise bursts with a 180 Hz body at amplitude a (busy highs over the beat).
    bass_notes=((t0, length), ...) adds held 808 notes (pitch drop into a held 55 Hz).
    kick_rolls=((t0, count, spacing), ...) adds count kicks spacing seconds apart (a fast run).
    pulse=(t0, t1, f0, rate) adds a held note whose loudness pulses rate times a second, ~20 dB
    deep, over two steady quieter voices (like the "desireeee" in Desire: a B5 over a chord).
    melody=(t0, t1, step) adds a sung line that changes note every `step` seconds.
    Returns (sr, x, kick times actually played)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * seconds)) / sr
    beat = 60.0 / bpm
    x = np.zeros_like(t)
    kicks = []
    for k in range(int(seconds / beat)):
        t0 = k * beat
        if k % kick_every == 0 and not (kick_gap and kick_gap[0] <= t0 < kick_gap[1]):
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
    if claps:
        for k in range(int(seconds / (beat / 4))):
            t0 = k * beat / 4
            idx = (t >= t0) & (t < t0 + 0.08)
            tt = t[idx] - t0
            x[idx] += claps * (rng.standard_normal(idx.sum()) * np.exp(-tt * 40)
                               + 0.7 * np.sin(2 * np.pi * 180 * tt) * np.exp(-tt * 30))
    for r0, count, spacing in kick_rolls:
        for j in range(count):
            t0 = r0 + j * spacing
            idx = (t >= t0) & (t < t0 + 0.25)
            tt = t[idx] - t0
            x[idx] += 0.9 * np.sin(2 * np.pi * np.cumsum(50 + 100 * np.exp(-tt * 25)) / sr) * np.exp(-tt * 8)
    for t0, length in bass_notes:
        idx = (t >= t0) & (t < t0 + length)
        tt = t[idx] - t0
        f = 55 + 95 * np.exp(-tt * 25)                     # 808: kick-like pitch drop, then a held 55 Hz
        x[idx] += (0.9 * np.sin(2 * np.pi * np.cumsum(f) / sr) * np.minimum(1, tt / 0.003)
                   * np.exp(-tt * 0.3) * np.minimum(1, (length - tt) / 0.03))
    def voice(idx, f, amp):
        ph = 2 * np.pi * np.cumsum(np.broadcast_to(f, idx.sum())) / sr
        return amp * sum(np.sin(h * ph) / h for h in range(1, 6))
    if pulse:
        a, b, f0, rate = pulse
        idx = (t >= a) & (t < b)
        tt = t[idx] - a
        env = np.minimum(1, tt / 0.03) * np.minimum(1, (b - a - tt) / 0.03)
        am = 0.1 + 0.9 * (0.5 + 0.5 * np.cos(2 * np.pi * rate * tt)) ** 2
        x[idx] += env * (voice(idx, f0, 0.35) * am + voice(idx, f0 * 0.43, 0.08) + voice(idx, f0 * 1.26, 0.05))
    if melody:
        a, b, step = melody
        notes = (440, 523, 659, 587, 698, 494, 784, 554)
        idx = (t >= a) & (t < b)
        tt = t[idx] - a
        f = np.array(notes)[(tt // step).astype(int) % len(notes)]
        x[idx] += np.minimum(1, tt / 0.03) * voice(idx, f, 0.3) * (0.6 + 0.4 * np.cos(2 * np.pi * tt / step))
    if trill:
        a, b, f0, st, rate = trill
        idx = (t >= a) & (t < b)
        tt = t[idx] - a
        side = 0.5 + 0.5 * np.tanh(4 * np.sin(2 * np.pi * rate * tt)) if rate else 0.0
        f = f0 * 2 ** (st * side / 12)
        ph = 2 * np.pi * np.cumsum(f) / sr
        env = np.minimum(1, tt / 0.05) * np.minimum(1, (b - a - tt) / 0.05)
        x[idx] += 0.3 * env * sum(np.sin(h * ph) / h for h in range(1, 6))
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
