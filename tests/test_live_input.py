"""live_blocks(): rate sync with the speaker and stall recovery, against a fake sounddevice
and a fake CoreAudio (no audio hardware in CI)."""

import os
import sys
import threading
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from govee_rt import audio  # noqa: E402

BLOCK = 256


class FakeMac:
    """BlackHole (id 7) inside a Multi-Output Device clocked by a speaker at `out_rate`."""

    def __init__(self, bh_rate, out_rate):
        self.bh_rate, self.out_rate, self.sets = bh_rate, out_rate, []

    def output_rate_for(self, name):
        return 7, self.out_rate

    def rate(self, dev):
        return self.bh_rate

    def set_rate(self, dev, sr):
        self.sets.append(sr)
        self.bh_rate = sr
        return True


class FakeSd(types.ModuleType):
    """InputStream feeds blocks from a thread; `stall_after` blocks then goes silent.
    `frames`: deliver runs of this many frames instead of the requested blocksize, as AUHAL
    does after CoreAudio rebuilds the device (err -10863). Samples count up from 0."""

    def __init__(self, default_rate, stall_after=None, frames=None):
        super().__init__("sounddevice")
        self.default_rate, self.stall_after, self.frames = default_rate, stall_after, frames
        self.opened, self.reinits = [], 0
        sd = self

        class InputStream:
            def __init__(self, device, channels, samplerate, blocksize, dtype, callback):
                self.cb, self.n, self.stop = callback, blocksize, threading.Event()
                sd.opened.append(samplerate)

            def __enter__(self):
                limit = sd.stall_after if len(sd.opened) == 1 else None

                def feed():
                    i, t, n = 0, 0, sd.frames or self.n
                    while not self.stop.wait(0.002) and (limit is None or i < limit):
                        ramp = np.arange(t, t + n, dtype=np.float32)
                        self.cb(np.stack([ramp, ramp], axis=1), n, None, None)
                        i, t = i + 1, t + n
                threading.Thread(target=feed, daemon=True).start()
                return self

            def __exit__(self, *a):
                self.stop.set()

        self.InputStream = InputStream

    def query_devices(self, device, kind):
        return {"default_samplerate": self.default_rate, "max_input_channels": 2}

    def _terminate(self):
        self.reinits += 1

    def _initialize(self):
        pass


def _run(monkeypatch, sd, mac, n, sr=None, **knobs):
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(audio, "macaudio", mac)
    for k, v in knobs.items():
        monkeypatch.setattr(audio, k, v)
    gen = audio.live_blocks("BlackHole 2ch", BLOCK, sr)
    out = [next(gen)[0] for _ in range(n)]
    gen.close()
    return out


def test_capture_moves_to_speaker_rate_before_opening(monkeypatch):
    # speaker reconnected at 44.1 kHz, BlackHole left at 48 kHz, PortAudio's cache says 48 kHz
    mac, sd = FakeMac(48000.0, 44100.0), FakeSd(48000.0)
    rates = _run(monkeypatch, sd, mac, 5)
    assert mac.sets == [44100.0]
    assert sd.opened == [44100]
    assert rates == [44100] * 5


def test_speaker_rate_change_mid_stream_reopens(monkeypatch):
    mac, sd = FakeMac(44100.0, 44100.0), FakeSd(44100.0)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(audio, "macaudio", mac)
    monkeypatch.setattr(audio, "RATE_CHECK_S", 0.0)
    gen = audio.live_blocks("BlackHole 2ch", BLOCK)
    assert next(gen)[0] == 44100
    mac.out_rate = 48000.0                   # speaker swapped for one that runs at 48 kHz
    rates = [next(gen)[0] for _ in range(4)]
    gen.close()
    assert rates[-1] == 48000
    assert sd.opened == [44100, 48000] and mac.sets == [48000.0]
    assert sd.reinits == 1                   # PortAudio's device list re-read on reopen


def test_stalled_stream_is_reopened(monkeypatch):
    mac, sd = FakeMac(44100.0, 44100.0), FakeSd(44100.0, stall_after=3)
    rates = _run(monkeypatch, sd, mac, 6, STALL_S=0.05)
    assert len(sd.opened) == 2 and rates == [44100] * 6


def test_explicit_samplerate_is_left_alone(monkeypatch):
    mac, sd = FakeMac(48000.0, 44100.0), FakeSd(48000.0)
    rates = _run(monkeypatch, sd, mac, 3, sr=48000, RATE_CHECK_S=0.0)
    assert mac.sets == [] and sd.opened == [48000] and rates == [48000] * 3


def test_odd_sized_callbacks_are_regrouped_into_blocks(monkeypatch):
    # After a CoreAudio device rebuild the stream kept running with another buffer size; the
    # engine drops blocks that aren't `block` long, so the meters froze with no stall logged.
    mac, sd = FakeMac(44100.0, 44100.0), FakeSd(44100.0, frames=100)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(audio, "macaudio", mac)
    gen = audio.live_blocks("BlackHole 2ch", BLOCK)
    blocks = [next(gen)[1] for _ in range(5)]
    gen.close()
    assert all(len(b) == BLOCK for b in blocks)
    assert np.array_equal(np.concatenate(blocks), np.arange(5 * BLOCK, dtype=np.float32))
