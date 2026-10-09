"""
BLE transport: packet layout, segment planning, and GoveeBle against tests/fake_govee_ble.py.
No bleak, Bluetooth adapter, or hardware needed.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import govee_rt  # noqa: E402
from fake_govee_ble import FakeBleClient  # noqa: E402


def rainbow(n):
    return [govee_rt.hsv(i / n, 1, 1) for i in range(n)]


# ----------------------------------------------------------------------------- packets

def test_packet_is_20_bytes_with_xor():
    p = govee_rt.ble_power(True)
    assert len(p) == 20 and p[:3] == bytes([0x33, 0x01, 0x01])
    assert govee_rt._xor(p[:19]) == p[19]
    with pytest.raises(ValueError):
        govee_rt.ble_packet(*range(20))


def test_segment_color_matches_h617a_layout():
    # 33 05 15 01 | rgb | kelvin u16 = 0 | rgb preview | mask u16le | pad | xor
    p = govee_rt.ble_segment_color(0x7FFF, 255, 0, 0)
    assert p[:19] == bytes([0x33, 0x05, 0x15, 0x01, 255, 0, 0, 0, 0, 255, 0, 0, 0xFF, 0x7F]) + bytes(5)
    assert govee_rt._xor(p[:19]) == p[19]
    with pytest.raises(ValueError):
        govee_rt.ble_segment_color(0, 1, 2, 3)
    with pytest.raises(ValueError):
        govee_rt.ble_segment_color(1 << 56, 1, 2, 3)
    # segments 17-20 (H612D) go in the third mask byte
    assert govee_rt.ble_segment_color(0xF0000, 1, 2, 3)[12:15] == bytes([0, 0, 0x0F])


def test_brightness_is_clamped_to_percent():
    assert govee_rt.ble_brightness(250)[2] == 100
    assert govee_rt.ble_brightness(0)[2] == 1


def test_resample():
    assert govee_rt.resample([1, 2, 3], 3) == [1, 2, 3]
    assert govee_rt.resample([1, 2], 4) == [1, 1, 2, 2]
    assert govee_rt.resample(list(range(30)), 15) == list(range(1, 30, 2))


# ----------------------------------------------------------------------------- planning

def masks(packets):
    return [p[12] | p[13] << 8 for p in packets]


def test_plan_full_frame_covers_every_segment_once():
    pkts, state = govee_rt.ble_segment_packets(rainbow(15), 15)
    m = masks(pkts)
    assert len(pkts) == 15
    assert sum(m) == 0x7FFF and all(x & y == 0 for i, x in enumerate(m) for y in m[i + 1:])
    assert state == [tuple(c) for c in rainbow(15)]


def test_plan_groups_equal_colors():
    cols = [(255, 0, 0)] * 5 + [(0, 0, 255)] * 10
    pkts, _ = govee_rt.ble_segment_packets(cols, 15)
    assert sorted(masks(pkts)) == [0b11111, 0x7FFF ^ 0b11111]


def test_plan_skips_unchanged_but_forces_round_robin():
    cols = rainbow(15)
    _, state = govee_rt.ble_segment_packets(cols, 15)
    pkts, _ = govee_rt.ble_segment_packets(cols, 15, prev=state)
    assert pkts == []
    pkts, _ = govee_rt.ble_segment_packets(cols, 15, prev=state, force=7)
    assert masks(pkts) == [1 << 7]
    # small changes under tol are skipped, larger ones are sent
    nudged = [(r, g, b) for r, g, b in cols]
    nudged[3] = (cols[3][0] ^ 4, cols[3][1], cols[3][2])
    nudged[9] = (0, 0, 0) if cols[9] != (0, 0, 0) else (255, 255, 255)
    pkts, _ = govee_rt.ble_segment_packets(nudged, 15, prev=state)
    assert masks(pkts) == [1 << 9]


def test_plan_handles_20_segments_and_rejects_more_than_56():
    pkts, _ = govee_rt.ble_segment_packets(rainbow(20), 20)
    assert sum(int.from_bytes(p[12:19], "little") for p in pkts) == (1 << 20) - 1
    with pytest.raises(ValueError):
        govee_rt.ble_segment_packets(rainbow(60), 60)


# ----------------------------------------------------------------------------- GoveeBle + fake

def make(segments=15, mode="segments", **fake_kw):
    fake = FakeBleClient(segments=segments, **fake_kw)
    g = govee_rt.GoveeBle(segments=segments, mode=mode, client_factory=lambda: fake)
    return g, fake


def test_commands_reach_fake():
    g, fake = make()
    try:
        g.power(True)
        g.brightness(80)
        g.color(10, 20, 30)
        assert fake.on and fake.brightness == 80
        assert fake.segs == [(10, 20, 30)] * 15
        assert fake.bad == 0
    finally:
        g.close()
    assert not fake.is_connected


def test_frames_land_and_slow_link_drops_instead_of_lagging():
    g, fake = make(write_delay=0.004)  # ~15 writes per full frame -> ~60 ms per frame
    try:
        g.rt_enable()
        last = None
        for i in range(60):          # offered at ~200 fps, far faster than the link
            last = rainbow(15)[i % 15:] + rainbow(15)[:i % 15]
            g.rt_frame(last)
            time.sleep(0.005)
        g.flush(timeout=3)
        assert fake.segs == [tuple(c) for c in last]
        assert g.stats["frames_dropped"] > 0
        assert g.stats["frames_sent"] + g.stats["frames_dropped"] == g.stats["frames_in"] == 60
        assert fake.bad == 0
    finally:
        g.close()


def test_frame_with_more_colors_is_resampled_onto_segments():
    g, fake = make(segments=12)
    try:
        g.rt_frame(rainbow(24))
        g.flush()
        assert fake.segs == [tuple(c) for c in govee_rt.resample(rainbow(24), 12)]
    finally:
        g.close()


def test_keepalive_when_idle():
    g, fake = make()
    try:
        time.sleep(3.3)
        assert fake.stats["query"] >= 1
        assert g.notifications and g.notifications[-1][0] == 0xAA
    finally:
        g.close()


def test_encrypted_device_is_refused():
    fake = FakeBleClient(enc_marker=bytes([2, 2, 0, 0, 0, 0]))
    with pytest.raises(RuntimeError, match="encrypted"):
        govee_rt.GoveeBle(client_factory=lambda: fake)
    assert not fake.is_connected


def test_plaintext_marker_is_accepted():
    g, fake = make(enc_marker=bytes([1, 0]))
    g.close()


def test_razer_mode_writes_bb_frames():
    g, fake = make(mode="razer")
    try:
        g.rt_enable()
        assert fake.rt_mode
        g.rt_frame(rainbow(15))
        g.flush()
        assert fake.razer_frames[-1] == [tuple(c) for c in rainbow(15)]
        g.rt_disable()
        assert not fake.rt_mode
        assert fake.bad == 0
    finally:
        g.close()


def test_razer_frame_too_big_for_mtu_is_reported():
    g, fake = make(mode="razer", mtu=23)
    try:
        g.rt_frame(rainbow(15))
        g.flush()
        with pytest.raises(RuntimeError, match="MTU"):
            g.rt_frame(rainbow(15))
    finally:
        g.close()


# ----------------------------------------------------------------------------- Finger Sketch

def test_a3_envelope_matches_h612d_capture():
    # the exact upload the H612D accepted and displayed (60 cells, red/green/blue repeating)
    groups = [((255, 0, 0), list(range(0, 60, 3))), ((0, 255, 0), list(range(1, 60, 3))),
              ((0, 0, 255), list(range(2, 60, 3)))]
    frames = govee_rt.ble.a3_frames(0x03, govee_rt.ble.sketch_body((0, 0, 0), groups, effect=0x14, speed=50))
    assert frames[0].hex() == "a30001060314326400000003" "14ff0000000306" "08"
    assert frames[-1].hex() == "a3ff" + "00" * 17 + "5c"
    assert all(len(f) == 20 and govee_rt._xor(f[:19]) == f[19] for f in frames)


def test_sketch_plan_uses_background_and_groups():
    cols = [(255, 0, 0)] * 40 + [(0, 0, 255)] * 20
    frames, pic = govee_rt.ble.sketch_plan(cols, 60)
    body = b"".join(f[2:19] for f in frames[:-1])[2:]
    assert body[0] == 0x03 and body[1] == govee_rt.ble.SKETCH_EFFECT_STILL and body[2] == 0
    assert tuple(body[4:7]) == (255, 0, 0)            # most common color is the background
    assert body[7] == 1 and body[8] == 20            # one group of 20 blue cells
    assert len(pic) == 60


def test_sketch_mode_streams_pictures():
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="sketch", cells=60, client_factory=lambda: fake)
    try:
        frame = rainbow(60)
        g.rt_frame(frame)
        g.flush(timeout=3)
        q = lambda c: tuple(min(255, round(v / 8) * 8) for c2 in [c] for v in c2)  # noqa: E731
        assert fake.mode == 0x0A and fake.shown == [q(c) for c in frame]
        sel = fake.stats["sketch_select"]
        g.rt_frame(frame)                                # unchanged picture: nothing sent
        g.flush(timeout=3)
        assert fake.stats["sketch_select"] == sel and fake.bad == 0
    finally:
        g.close()


def test_reverse_flips_frames():
    fake = FakeBleClient(segments=4)
    g = govee_rt.GoveeBle(segments=4, mode="segments", reverse=True, client_factory=lambda: fake)
    try:
        g.rt_frame([(255, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 255)])
        g.flush(timeout=3)
        assert fake.segs == [(0, 0, 255), (0, 0, 0), (0, 0, 0), (255, 0, 0)]
    finally:
        g.close()


# ----------------------------------------------------------------------------- stream / hybrid

def test_stream_frame_matches_app_capture():
    assert govee_rt.ble.stream_frame(0x56, 0, 0).hex() == "a5028356000080"


def test_strip_color_keeps_flashes_visible():
    sc = govee_rt.ble.strip_color
    assert sc([(255, 0, 0)] * 10) == (255, 0, 0)
    assert sc([(0, 0, 0)] * 10) == (0, 0, 0)
    assert sc([(255, 0, 0), (0, 0, 255)]) == (255, 0, 255)
    assert max(sc([(0, 0, 0)] * 9 + [(255, 255, 255)])) >= 120     # one bright cell still flashes


def test_stream_mode_sends_one_paced_color():
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="stream", client_factory=lambda: fake)
    try:
        t0 = time.monotonic()
        for k in range(40):
            g.rt_frame([govee_rt.hsv(k / 40, 1, 1)] * 20)
            time.sleep(0.005)
        g.rt_frame([(0, 0, 255)] * 20)
        g.flush(timeout=3)
        el = time.monotonic() - t0
        assert fake.streamed[-1] == (0, 0, 255) and fake.bad == 0
        assert len(fake.streamed) <= el / govee_rt.ble.STREAM_MIN_GAP + 2   # paced, latest wins
        assert fake.stats["segment_color"] == 0 and fake.stats["sketch_upload"] == 0
    finally:
        g.close()


def test_hybrid_mode_beats_on_brightness_picture_on_sketch():
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="hybrid", cells=60, client_factory=lambda: fake)
    try:
        pic = [govee_rt.hsv(i / 60, 1, 1) for i in range(60)]
        g.rt_frame(pic)                                          # full brightness picture
        g.flush(timeout=3)
        assert fake.brightness == 100 and fake.mode == 0x0A and fake.shown is not None
        uploads = fake.stats["sketch_upload"]
        g.rt_frame([tuple(v // 2 for v in c) for c in pic])      # same picture at half level
        g.flush(timeout=3)
        assert fake.brightness == 50 and fake.stats["sketch_upload"] == uploads   # no re-upload
        g.rt_disable()
        assert fake.brightness == 100 and fake.bad == 0
    finally:
        g.close()


def _bars(pic, bar):
    lit = [i for i, c in enumerate(pic) if max(c) > 0]
    return [i for i in lit if i - 1 not in lit] if lit else []      # where each bar starts


def test_roll_mode_adds_a_bar_per_hit_and_old_bars_keep_going():
    ble = govee_rt.ble
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="roll_hits", cells=60, reverse=True, client_factory=lambda: fake)
    pictures = []
    try:
        def frame(level, wait=0.0):
            g.rt_frame([(int(255 * level), 0, int(170 * level))] * 20)
            g.flush(timeout=2)
            if fake.sketch is not None and (not pictures or fake.sketch != pictures[-1][1]):
                pictures.append((time.monotonic(), list(fake.sketch)))
            time.sleep(wait)
        frame(0, 0.05)
        assert _bars(fake.sketch, ble.ROLL_BAR) == [] and fake.sketch_effect == ble.ROLL_EFFECT
        t1 = time.monotonic()
        frame(1.0, 0.45)                                   # kick 1
        frame(0, 0.05)
        t2 = time.monotonic()
        frame(1.0)                                         # kick 2, 0.5 s later
        bars = _bars(fake.sketch, ble.ROLL_BAR)
        v, d = ble.ROLL_CELLS_PER_S, g._roll_delay
        assert len(bars) == 2                              # a hit adds a bar, the old one stays
        assert abs(bars[0] - v * d) <= 3                   # new bar: where it is on the strip by now
        assert abs(bars[1] - v * (t2 - t1 + d)) <= 3       # old bar: where the roll has carried it
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline and _bars(fake.sketch, ble.ROLL_BAR):
            frame(0, 0.02)
        assert _bars(fake.sketch, ble.ROLL_BAR) == []      # both finished their pass and left
        for _, pic in pictures:                            # never drawn where the roll would wrap
            assert all(p + ble.ROLL_BAR <= 60 for p in _bars(pic, ble.ROLL_BAR))
        assert fake.stats["segment_color"] == 0 and fake.bad == 0
    finally:
        g.close()


def test_roll_mode_is_one_bar_uploaded_once():
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="roll", cells=60, client_factory=lambda: fake)
    try:
        for level in (0, 1.0, 0, 1.0, 0.45, 1.0, 0):        # kicks and a run change nothing
            g.rt_frame([(int(255 * level), 0, 0)] * 20)
            g.flush(timeout=2)
        assert fake.stats["sketch_upload"] == 1 and fake.stats["sketch_select"] == 1
        assert _bars(fake.sketch, govee_rt.ble.ROLL_BAR) == [0] and not fake.streamed
    finally:
        g.close()


def test_roll_picture_marks_each_bar():
    pic = govee_rt.ble.roll_picture(60, [0, 20, 57], bar=6)
    assert _bars(pic, 6) == [0, 20, 57] and sum(1 for c in pic if max(c)) == 6 + 6 + 3


def test_scene_switches_ble_mode_and_strobe_uses_the_default():
    from govee_rt.engine import Engine
    import govee_rt.effects as fx
    fake = FakeBleClient(segments=20)
    eng = Engine(1, lambda: govee_rt.GoveeBle(segments=20, mode="stream", cells=60, client_factory=lambda: fake),
                 fps=30, scene={"layers": [fx.new_layer("solid")]})
    eng.connect()
    try:
        eng._send(eng.step(0.0, 1 / 30))
        assert eng.light.mode == "stream"
        eng.set_scene({"ble_mode": "roll", "layers": [fx.new_layer("bass_pulse")]})
        eng._send(eng.step(0.1, 1 / 30))
        assert eng.light.mode == "roll"
        eng.strobe(True, hold=5)
        eng._send(eng.step(0.2, 1 / 30))
        assert eng.light.mode == "stream"                   # strobe flashes are whole-strip frames
        eng.strobe(False)
        eng._send(eng.step(0.3, 1 / 30))
        assert eng.light.mode == "roll"
        eng.set_scene({"layers": [fx.new_layer("solid")]})
        eng._send(eng.step(0.4, 1 / 30))
        assert eng.light.mode == "stream"                   # back to the profile's mode
        eng.light.flush()
        assert fake.bad == 0
    finally:
        eng.stop()


def test_roll_flash_rolls_palette_bars_and_flashes_brightness_on_hits():
    ble = govee_rt.ble
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="roll_flash", cells=60, client_factory=lambda: fake)
    neon = [(248, 0, 168), (0, 224, 248), (128, 0, 248), (56, 248, 16)]   # on the sketch's 8-step grid
    try:
        g.set_roll_colors(neon)
        levels = []
        for level in (0.3, 1.0, 0.3, 1.0, 0.6, 1.0, 0.3, 0.0):   # rest, kicks, a run, dark
            g.rt_frame([(int(255 * level), 0, 0)] * 20)
            g.flush(timeout=2)
            levels.append(fake.brightness)
        assert levels == [30, 100, 30, 100, 60, 100, 30, 1]     # the flash rides on brightness
        assert fake.stats["sketch_upload"] == 1                 # bars never re-uploaded on hits
        bars = _bars(fake.sketch, ble.ROLL_BAR)
        assert bars == [0, 15, 30, 45] and fake.sketch_effect == ble.ROLL_EFFECT
        assert [fake.sketch[b] for b in bars] == neon           # one palette color per bar
        g.set_roll_colors(neon[::-1])                           # palette changed in the studio
        g.rt_frame([(255, 0, 0)] * 20)
        g.flush(timeout=2)
        assert fake.stats["sketch_upload"] == 2 and fake.sketch[0] == neon[-1]
        g.rt_disable()
        assert fake.brightness == 100 and fake.bad == 0         # never left dimmed
    finally:
        g.close()


def test_engine_feeds_the_scene_palette_to_roll_flash():
    from govee_rt.engine import Engine
    import govee_rt.effects as fx
    fake = FakeBleClient(segments=20)
    eng = Engine(1, lambda: govee_rt.GoveeBle(segments=20, mode="stream", cells=60, client_factory=lambda: fake),
                 fps=30, scene={"ble_mode": "roll_flash", "layers": [fx.new_layer("bass_pulse", params={
                     "palette": ["#ff0000", "#00ff00"]})]})
    eng.connect()
    try:
        eng._send(eng.step(0.0, 1 / 30))
        eng.light.flush()
        assert eng.light.mode == "roll_flash"
        bars = _bars(fake.sketch, govee_rt.ble.ROLL_BAR)
        assert [fake.sketch[b] for b in bars] == [(255, 0, 0), (255, 0, 0), (0, 255, 0), (0, 255, 0)]
    finally:
        eng.stop()


def test_flow_uploads_the_palette_gradient_once_and_glides_brightness():
    ble = govee_rt.ble
    fake = FakeBleClient(segments=20)
    g = govee_rt.GoveeBle(segments=20, mode="flow", cells=60, client_factory=lambda: fake)
    pal = [(0, 192, 64), (255, 208, 0), (0, 224, 176), (0, 64, 255)]    # on the sketch's 8-step grid
    try:
        g.set_roll_colors(pal)
        levels = []
        for pct in (30, 70, 62, 55, 50, 31, 30):              # rest, a beat's swell and fall
            g.rt_frame([(round(pct * 2.55), 0, 0)] * 20)
            g.flush(timeout=2)
            levels.append(fake.brightness)
        assert levels == [30, 70, 62, 55, 50, 31, 31]         # 2 % steps; a 1 % wobble costs no write
        assert fake.stats["sketch_upload"] == 1               # the strip animates, the Mac only dims
        assert fake.sketch_effect == ble.FLOW_EFFECT and fake.sketch_speed == ble.FLOW_SPEED
        pic = fake.sketch
        assert [pic[i] for i in (0, 15, 30, 45)] == pal       # one cycle through the palette
        assert max(max(abs(a - b) for a, b in zip(c, d)) for c, d in zip(pic, pic[1:] + pic[:1])) <= 32
        g.set_roll_colors(pal[::-1])                          # palette changed in the studio
        g.rt_frame([(255, 0, 0)] * 20)
        g.flush(timeout=2)
        assert fake.stats["sketch_upload"] == 2 and fake.sketch[0] == pal[-1]
        g.rt_disable()
        assert fake.brightness == 100 and fake.bad == 0
    finally:
        g.close()


def test_leaving_a_dimming_scene_restores_full_brightness():
    from govee_rt.engine import Engine
    import govee_rt.effects as fx
    fake = FakeBleClient(segments=20)
    eng = Engine(20, lambda: govee_rt.GoveeBle(segments=20, mode="stream", cells=60, client_factory=lambda: fake),
                 fps=30, scene={"ble_mode": "flow", "layers": [fx.new_layer("chill")], "master": {"gamma": 1.0}})
    eng.connect()
    try:
        eng._send(eng.step(0.0, 1 / 30))
        eng.light.flush()
        assert eng.light.mode == "flow" and fake.sketch_effect == govee_rt.ble.FLOW_EFFECT
        assert fake.brightness == 30                          # Chill flow resting in silence
        eng.set_scene({"layers": [fx.new_layer("bass_pulse")]})   # back to a party scene (stream)
        eng._send(eng.step(0.1, 1 / 30))
        eng.light.flush()
        assert eng.light.mode == "stream" and fake.brightness == 100 and fake.streamed
        assert fake.bad == 0
    finally:
        eng.stop()
