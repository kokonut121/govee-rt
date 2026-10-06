"""
Run with:  pytest -q

Covers the wire format (byte-exact against OpenRGB's known-good frames), discovery and
command delivery against the in-process fake device, and beat-detection accuracy on a
synthetic track with a known kick grid. No real hardware or audio device needed.
"""
import base64
import json
import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import govee_rt  # noqa: E402
from fake_govee import FakeGovee  # noqa: E402
from make_test_wav import synth  # noqa: E402


# ----------------------------------------------------------------------------- wire format

def test_enable_disable_match_openrgb():
    # OpenRGB sends exactly these for B1 on/off (Controllers/GoveeController/GoveeController.cpp)
    assert govee_rt.razer_frame(0xB1, b"\x01") == bytes([0xBB, 0x00, 0x01, 0xB1, 0x01, 0x0A])
    assert govee_rt.razer_frame(0xB1, b"\x00") == bytes([0xBB, 0x00, 0x01, 0xB1, 0x00, 0x0B])


def test_color_frame_layout():
    pt = govee_rt.razer_frame(0xB0, bytes([0x01, 2, 255, 0, 0, 0, 0, 255]))
    assert pt[0] == 0xBB
    assert (pt[1] << 8 | pt[2]) == 2 + 3 * 2
    assert pt[3] == 0xB0 and pt[4] == 0x01 and pt[5] == 2
    assert pt[6:12] == bytes([255, 0, 0, 0, 0, 255])
    assert govee_rt._xor(pt[:-1]) == pt[-1]


def test_json_wrapping_and_null_terminator():
    raw = govee_rt.frame_colors([(1, 2, 3)])
    assert raw.endswith(b"\x00")
    msg = json.loads(raw[:-1])
    pt = base64.b64decode(msg["msg"]["data"]["pt"])
    assert msg["msg"]["cmd"] == "razer"
    assert pt[3] == 0xB0 and pt[5] == 1 and pt[6:9] == bytes([1, 2, 3])


def test_frame_caps_at_255_segments():
    raw = govee_rt.frame_colors([(9, 9, 9)] * 300)
    pt = base64.b64decode(json.loads(raw[:-1])["msg"]["data"]["pt"])
    assert pt[5] == 255


# ----------------------------------------------------------------------------- against the fake

@pytest.fixture(scope="module")
def fake():
    dev = FakeGovee(segments=15).start()
    time.sleep(0.2)
    yield dev
    dev.stop()


def test_scan_finds_fake(fake):
    found = govee_rt.scan(timeout=1.0, targets=["127.0.0.1"])
    assert "127.0.0.1" in found
    assert found["127.0.0.1"]["sku"] == "H612C"


def test_official_commands_and_rt_frames(fake):
    g = govee_rt.GoveeLan("127.0.0.1")
    g.power(True)
    g.brightness(70)
    g.color(10, 20, 30)
    g.rt_enable()
    cols = [(i, 255 - i, 7) for i in range(15)]
    g.rt_frame(cols)
    g.rt_disable()
    time.sleep(0.3)
    assert fake.state["on"] == 1
    assert fake.state["brightness"] == 70
    assert fake.state["color"] == (10, 20, 30)
    assert fake.stats["rt_enable"] >= 1 and fake.stats["rt_disable"] >= 1
    assert fake.stats["razer_bad"] == 0
    assert fake.last_frame == cols


def test_rt_frame_reasserts_enable_after_20s(fake):
    g = govee_rt.GoveeLan("127.0.0.1")
    before = fake.stats["rt_enable"]
    g._last_enable = time.monotonic() - 21
    g.rt_frame([(1, 1, 1)] * 3)
    time.sleep(0.2)
    assert fake.stats["rt_enable"] == before + 1


# ----------------------------------------------------------------------------- audio analysis

def test_beat_detection_on_synthetic_track():
    sr, x, kicks = synth(bpm=128, seconds=12)
    block = 1024
    an = govee_rt.Analyzer(sr, 15, block)
    beats = []
    for i in range(0, len(x) - block, block):
        f = an.process(x[i:i + block])
        if f["beat"]:
            beats.append(i / sr)
    beats = np.array(beats)
    tol = 0.05
    hits = sum(1 for t in kicks if len(beats) and np.any(np.abs(beats - t) < tol))
    false = sum(1 for b in beats if not np.any(np.abs(kicks - b) < tol))
    # first kick lands before the flux history exists, so allow a couple of misses
    assert hits >= len(kicks) - 2, f"hits={hits}/{len(kicks)}"
    assert false == 0, f"false positives: {false}"


def test_spectrum_bands_follow_content():
    sr, x, _ = synth(bpm=128, seconds=4)
    block = 1024
    an = govee_rt.Analyzer(sr, 15, block)
    acc = np.zeros(15)
    n = 0
    for i in range(0, len(x) - block, block):
        acc += an.process(x[i:i + block])["bands"]
        n += 1
    acc /= n
    # the pad chord (220-330 Hz) lives in bands 4-5 with 15 log bands over 40..14 kHz;
    # the top bands (>4 kHz) only see hat noise, so they must average lower
    assert acc[4:6].mean() > acc[10:].mean()


def test_visualizer_modes_return_n_rgb_tuples():
    f = {"bands": np.linspace(0, 1, 15), "bass": 0.7, "beat": True, "beat_env": 1.0}
    for mode in ("spectrum", "pulse", "vu", "wave"):
        out = govee_rt.Visualizer(15, mode).render(f)
        assert len(out) == 15
        assert all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in out)
