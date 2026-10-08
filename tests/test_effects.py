"""
Effects engine, scenes, presets, the render loop and the studio HTTP API.
No hardware, audio device or browser needed.
"""
import http.client
import json
import os
import sys
import threading
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from govee_rt import effects as fx  # noqa: E402
from govee_rt.engine import DemoBeat, Engine  # noqa: E402
from govee_rt.studio import Presets, serve  # noqa: E402
from fake_govee_ble import FakeBleClient  # noqa: E402
import govee_rt  # noqa: E402


def run_frames(r, frames=200, dt=0.025):
    demo = DemoBeat(124)
    out = None
    for i in range(frames):
        t = (i + 1) * dt
        f, beat = demo.features(t)
        out = r.render(f, t, dt, beat)
    return out


# ----------------------------------------------------------------------------- effects

@pytest.mark.parametrize("name", sorted(fx.EFFECTS))
@pytest.mark.parametrize("n", [1, 12, 15, 60])
def test_every_effect_renders_valid_frames(name, n):
    r = fx.Renderer(n, {"layers": [fx.new_layer(name)]})
    demo = DemoBeat(124)
    for i in range(120):
        t = (i + 1) * 0.025
        f, beat = demo.features(t)
        raw = r.render_float(f, t, 0.025, beat)
        assert raw.shape == (n, 3) and np.isfinite(raw).all()
        assert raw.min() >= 0 and raw.max() <= 1
        out = r.render(f, t, 0.025, beat)
        assert len(out) == n and all(0 <= v <= 255 and isinstance(v, int) for c in out for v in c)


def test_palette_sampling():
    assert fx.rgb_to_hex(fx.sample(["#ff0000", "#0000ff"], np.array([0.0]), wrap=False)[0]) == "#ff0000"
    assert fx.rgb_to_hex(fx.sample(["#ff0000", "#0000ff"], np.array([1.0]), wrap=False)[0]) == "#0000ff"
    mid = fx.sample(["#ff0000", "#0000ff"], np.array([0.5]), wrap=False)[0]
    assert np.allclose(mid, [0.5, 0, 0.5])
    # wrap: halfway between the last stop and the first
    assert np.allclose(fx.sample(["#ff0000", "#0000ff"], np.array([0.75]))[0], [0.5, 0, 0.5])


def test_solid_and_gamma():
    r = fx.Renderer(4, {"layers": [fx.new_layer("solid", params={"color": "#808080"})],
                        "master": {"brightness": 1.0, "gamma": 1.0}})
    assert run_frames(r, 1) == [(128, 128, 128)] * 4
    r.set_scene(dict(r.scene, master={"brightness": 1.0, "gamma": 2.0}))
    assert run_frames(r, 1)[0][0] == round((128 / 255) ** 2 * 255)


def test_layer_span_reverse_and_mirror():
    red = fx.new_layer("solid", params={"color": "#ff0000"}, start=0.0, end=0.5)
    r = fx.Renderer(10, {"layers": [red], "master": {"gamma": 1.0}})
    out = run_frames(r, 1)
    assert out[:5] == [(255, 0, 0)] * 5 and out[5:] == [(0, 0, 0)] * 5

    paint = fx.new_layer("paint", params={"colors": ["#ff0000", "#00ff00", "#0000ff"]})
    r = fx.Renderer(3, {"layers": [dict(paint, reverse=True)], "master": {"gamma": 1.0}})
    assert run_frames(r, 1) == [(0, 0, 255), (0, 255, 0), (255, 0, 0)]

    r = fx.Renderer(6, {"layers": [dict(paint, mirror=True)], "master": {"gamma": 1.0}})
    assert run_frames(r, 1) == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 255), (0, 255, 0), (255, 0, 0)]


def test_blend_modes():
    base = fx.new_layer("solid", params={"color": "#800000"})
    for mode, expect in [("normal", (0, 0, 255)), ("add", (128, 0, 255)), ("max", (128, 0, 255)),
                         ("multiply", (0, 0, 0))]:
        top = fx.new_layer("solid", params={"color": "#0000ff"}, blend=mode)
        r = fx.Renderer(2, {"layers": [base, top], "master": {"gamma": 1.0}})
        assert run_frames(r, 1)[0] == expect, mode
    half = fx.new_layer("solid", params={"color": "#ffffff"}, opacity=0.5)
    r = fx.Renderer(2, {"layers": [half], "master": {"gamma": 1.0}})
    assert run_frames(r, 1)[0] == (128, 128, 128)


def test_modulation_follows_source_and_clamps():
    src = {"bass": 1.0}
    assert fx.modulate(0.2, 0.0, 1.0, {"src": "bass", "amt": 0.5}, src) == pytest.approx(0.7)
    assert fx.modulate(0.8, 0.0, 1.0, {"src": "bass", "amt": 1.0}, src) == 1.0
    assert fx.modulate(0.2, 0.0, 1.0, {"src": "bass", "amt": -1.0}, src) == 0.0
    assert fx.modulate(0.2, 0.0, 1.0, None, src) == 0.2
    # opacity tied to the beat: dark between beats, lit on the beat
    layer = fx.new_layer("solid", params={"color": "#ffffff"}, opacity=0.0,
                         mod={"opacity": {"src": "beat", "amt": 1.0}})
    r = fx.Renderer(1, {"layers": [layer], "master": {"gamma": 1.0}})
    assert r.render({"beat_env": 1.0}, 0, 0.02, True)[0] == (255, 255, 255)
    assert r.render({"beat_env": 0.0}, 0, 0.02, False)[0] == (0, 0, 0)


def test_beat_driven_effects_react_to_beats():
    for name in ("pulse", "ripple", "strobe", "sparkle"):
        r = fx.Renderer(15, {"layers": [fx.new_layer(name)], "master": {"gamma": 1.0}})
        quiet = {"bands": np.zeros(24), "beat_env": 0.0}
        before = sum(map(sum, r.render(quiet, 1.0, 0.02, False)))
        after = sum(map(sum, r.render(dict(quiet, beat_env=1.0), 1.02, 0.02, True)))
        assert after > before, name


def test_effect_state_survives_param_edits():
    layer = fx.new_layer("gradient", params={"speed": 1.0})
    layer["id"] = "a"
    r = fx.Renderer(8, {"layers": [layer]})
    run_frames(r, 10)
    inst = r._inst["a"][1]
    phase = inst.phase
    layer2 = dict(layer, params=dict(layer["params"], spread=2.0))
    r.set_scene({"layers": [layer2]})
    run_frames(r, 1)
    assert r._inst["a"][1] is inst and inst.phase > phase


# ----------------------------------------------------------------------------- scenes + presets

def test_normalize_rejects_and_clamps():
    with pytest.raises(ValueError):
        fx.normalize_scene({"layers": [{"effect": "nope"}]})
    with pytest.raises(ValueError):
        fx.normalize_scene({"layers": [{"effect": "solid", "params": {"color": "red"}}]})
    s = fx.normalize_scene({"layers": [{"effect": "comet", "params": {"speed": 999, "junk": 1},
                                        "mod": {"speed": {"src": "bass", "amt": 7}, "x": {"src": "bass"}},
                                        "blend": "weird", "opacity": 3, "start": 0.8, "end": 0.2}]})
    l0 = s["layers"][0]
    assert l0["params"]["speed"] == 60.0 and "junk" not in l0["params"]
    assert l0["mod"] == {"speed": {"src": "bass", "amt": 1.0}}
    assert l0["blend"] == "normal" and l0["opacity"] == 1.0
    assert (l0["start"], l0["end"]) == (0.0, 1.0)


def test_scene_json_round_trip():
    s = fx.normalize_scene(fx.default_scene())
    assert fx.normalize_scene(json.loads(json.dumps(s))) == s


def test_builtin_presets_load_and_render():
    p = Presets(user_dir="/nonexistent")
    names = [x["name"] for x in p.list()]
    assert len(names) >= 8
    for name in names:
        r = fx.Renderer(15, p.load(name))
        out = run_frames(r, 80)
        assert len(out) == 15


def test_user_presets_save_load_delete(tmp_path):
    p = Presets(user_dir=str(tmp_path))
    p.save("my look", fx.default_scene())
    assert {"name": "my look", "builtin": False} in p.list()
    assert p.load("my look")["name"] == "my look"
    with pytest.raises(ValueError):
        p.save("../escape", fx.default_scene())
    p.delete("my look")
    with pytest.raises(KeyError):
        p.load("my look")


# ----------------------------------------------------------------------------- engine

def test_engine_streams_frames_to_light():
    fake = FakeBleClient(segments=15)
    eng = Engine(15, lambda: govee_rt.GoveeBle(segments=15, client_factory=lambda: fake), fps=40,
                 scene={"layers": [fx.new_layer("rainbow", params={"speed": 1.0})]})
    eng.use_demo(124)
    eng.connect()
    eng.start()
    time.sleep(0.6)
    eng.stop()
    assert eng.stats["frames"] >= 15 and eng.stats["sent"] >= 15
    assert fake.stats["segment_color"] > 0 and fake.bad == 0
    assert not fake.is_connected                       # stop() closed the link


def test_engine_audio_thread_latches_beats():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=4)
    block = 1024

    def blocks():
        for i in range(0, len(x) - block, block):
            yield sr, x[i:i + block].astype(np.float32)
    eng = Engine(15, scene={"layers": [fx.new_layer("pulse")]})
    eng.use_audio(blocks(), block)
    beats = 0
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and eng.source == "audio":
        _, beat = eng._features(0.0)
        beats += beat
        time.sleep(0.002)
    assert beats >= 3


# ----------------------------------------------------------------------------- studio HTTP

@pytest.fixture()
def studio(tmp_path):
    eng = Engine(12, fps=30)
    eng.use_demo(120)
    eng.start()
    httpd = serve(eng, Presets(user_dir=str(tmp_path)), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], eng
    eng.stop()
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, ctype="application/json", host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Host": host}
    if data is not None:
        headers["Content-Type"] = ctype
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"null")


def test_studio_catalog_scene_and_presets(studio):
    port, eng = studio
    st, cat = call(port, "GET", "/api/catalog")
    assert st == 200 and cat["segments"] == 12 and {e["name"] for e in cat["effects"]} == set(fx.EFFECTS)
    scene = {"name": "t", "layers": [fx.new_layer("solid", params={"color": "#00ff00"})],
             "master": {"gamma": 1.0}}
    st, got = call(port, "PUT", "/api/scene", scene)
    assert st == 200 and got["layers"][0]["params"]["color"] == "#00ff00"
    assert eng.get_scene()["layers"][0]["effect"] == "solid"
    st, _ = call(port, "PUT", "/api/presets/green", got)
    assert st == 200
    st, loaded = call(port, "GET", "/api/presets/green")
    assert st == 200 and loaded["layers"][0]["params"]["color"] == "#00ff00"
    st, lst = call(port, "DELETE", "/api/presets/green")
    assert st == 200 and "green" not in [p["name"] for p in lst["presets"]]


def test_studio_rejects_bad_input_and_foreign_hosts(studio):
    port, _ = studio
    assert call(port, "PUT", "/api/scene", {"layers": [{"effect": "nope"}]})[0] == 400
    assert call(port, "PUT", "/api/scene", {}, ctype="text/plain")[0] == 400
    assert call(port, "GET", "/api/scene", host="evil.example")[0] == 403
    assert call(port, "PUT", "/api/presets/..%2Fx", fx.default_scene())[0] == 400


def test_studio_strobe_endpoint_holds_then_times_out(studio):
    port, eng = studio
    assert call(port, "POST", "/api/strobe", {"on": True}) == (200, {"strobe": True, "hz": 10.0})
    assert eng.strobing()
    time.sleep(0.8)
    assert not eng.strobing()                               # no refresh: stops by itself
    call(port, "POST", "/api/strobe", {"on": True})
    assert call(port, "POST", "/api/strobe", {"on": False, "hz": 40}) == (200, {"strobe": False, "hz": 15.0})
    assert call(port, "POST", "/api/strobe", {"on": True}, host="evil.example")[0] == 403
    assert call(port, "PUT", "/api/strobe", {"on": True})[0] == 404   # POST only


def test_engine_strobe_one_color_full_and_off_then_back():
    eng = Engine(4, fps=30, scene={"layers": [fx.new_layer("solid", params={"color": "#802000"})],
                                   "master": {"gamma": 1.0}})
    before = eng.step(0.0, 1 / 30)
    eng.strobe(True, hold=5)
    frames = [eng.step(k / 30, 1 / 30) for k in range(1, 31)]  # 1 s at 30 fps
    full = (255, 64, 0)                                     # same hue, raised to full
    assert {f[0] for f in frames} == {full, (0, 0, 0)} and all(len(set(f)) == 1 for f in frames)
    assert all(a != b for a, b in zip(frames, frames[1:]))  # flips every frame: 15 flashes/s
    eng.strobe(False)
    assert eng.step(2.0, 1 / 30) == before                  # back to the scene


def test_strobe_runs_on_its_own_even_clock_while_beats_keep_coming():
    sent = []

    class Light:
        def power(self, on): pass
        def rt_enable(self): pass
        def rt_disable(self): pass
        def close(self): pass
        def rt_frame(self, colors): sent.append((time.monotonic(), colors[0]))

    eng = Engine(1, Light, fps=40, scene={"layers": [fx.new_layer("solid", params={"color": "#00ff00"})]})
    eng.use_demo(160)                                       # demo beats wake the loop early
    eng.connect()
    eng.start()
    time.sleep(0.3)
    eng.strobe(True, hold=5, hz=10)
    t0 = time.monotonic()
    time.sleep(1.0)
    eng.stop()
    flashes = [(t, c) for t, c in sent if t >= t0 + 0.05]
    gaps = [b[0] - a[0] for a, b in zip(flashes, flashes[1:]) if b[0] < t0 + 0.95]
    assert 17 <= len(gaps) <= 20                            # 20 flips/s for a 10/s strobe
    assert max(abs(g - 0.05) for g in gaps) < 0.015         # evenly spaced, not 25/50 ms mixed
    cols = [c for t, c in flashes if t < t0 + 0.95]
    assert all(a != b for a, b in zip(cols, cols[1:])) and set(cols) == {(0, 255, 0), (0, 0, 0)}


def test_studio_live_stream(studio):
    port, _ = studio
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/live", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    assert r.getheader("Content-Type") == "text/event-stream"
    line = b""
    while not line.startswith(b"data: "):
        line = r.fp.readline()
    d = json.loads(line[6:])
    assert len(d["colors"]) == 12 and "bass" in d["features"]
    c.close()


# ----------------------------------------------------------------------------- bass pulse, smoothing, idle

def test_bass_pulse_ignores_everything_but_bass_jumps():
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={"floor": 0.0, "drift": 0.0})],
                        "master": {"gamma": 1.0}})
    f = {"bass": 0.0, "mid": 1.0, "high": 1.0, "energy": 1.0, "beat_env": 1.0}
    t = 0.0
    for k in range(30):                                     # loud mids/highs and beats: nothing
        t += 1 / 30
        out = r.render(f, t, 1 / 30, k % 15 == 0)
    assert max(out[0]) <= 2
    for k in range(90):                                     # a steady loud bass bed: adapts, goes dark
        t += 1 / 30
        out = r.render(dict(f, bass=0.85), t, 1 / 30, False)
    assert max(out[0]) <= 10
    peak = 0
    for k in range(4):                                      # a kick: bass jumps above its average
        t += 1 / 30
        peak = max(peak, max(r.render(dict(f, bass=1.0 if k < 2 else 0.85), t, 1 / 30, False)[0]))
    assert peak > 120                                       # clearly lit vs <= 10 dark


def test_pulse_colors_glide_instead_of_jumping():
    r = fx.Renderer(1, {"layers": [fx.new_layer("pulse", params={"palette": ["#ff0000", "#0000ff"], "step": 0.5,
                                                                "floor": 1.0, "glide": 0.25})],
                        "master": {"gamma": 1.0}})
    f = {"beat_env": 0.0}
    r.render(f, 0, 1 / 30, False)
    first = r.render(f, 1 / 30, 1 / 30, True)[0]          # beat: palette target moves half way
    assert first[0] > 150 and first[2] < 100               # still mostly red one frame later
    for k in range(30):
        last = r.render(f, (k + 2) / 30, 1 / 30, False)[0]
    assert last[2] > 200                                   # arrived at blue after ~1 s


def test_idle_rainbow_after_silence_and_back_on_audio():
    fake = FakeBleClient(segments=20)
    level = {"v": 0.0}

    def blocks():
        while True:
            yield 48000, np.full(1024, level["v"], dtype=np.float32) * np.sin(np.arange(1024) * 0.05).astype(np.float32)
            time.sleep(1024 / 48000)
    eng = Engine(1, lambda: govee_rt.GoveeBle(segments=20, mode="stream", cells=60, client_factory=lambda: fake),
                 fps=30, scene={"layers": [fx.new_layer("bass_pulse")]}, idle_after=0.4, idle_speed=12)
    eng.use_audio(blocks(), 1024)
    eng.connect()
    eng.start()
    time.sleep(1.2)
    assert eng.idle and fake.mode == 0x0A and fake.sketch_effect == 0x13   # strip animates a rainbow itself
    assert len(set(fake.shown)) > 30
    streamed = len(fake.streamed)
    time.sleep(0.3)
    assert len(fake.streamed) == streamed                                    # nothing streamed while idle
    level["v"] = 0.3
    time.sleep(0.5)
    eng.stop()
    assert not eng.idle and len(fake.streamed) > streamed and fake.bad == 0



def test_bass_pulse_is_two_clean_levels_without_chatter():
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={"palette": "white", "color_every": 0})],
                        "master": {"gamma": 1.0}})
    rng = np.random.default_rng(1)
    t, seen, flips, prev = 0.0, set(), 0, None
    for k in range(300):                                    # 10 s: noisy bed with a kick every 0.5 s
        t += 1 / 30
        bass = 1.0 if k % 15 < 2 else 0.8 + 0.03 * rng.standard_normal()   # analyzer output is smoothed
        v = r.render({"bass": bass, "beat_env": 0.0}, t, 1 / 30, False)[0][0]
        seen.add(v)
        flips += prev is not None and v != prev
        prev = v
    assert seen == {round(0.45 * 255), 255}                 # only rest and full, nothing between
    assert 38 <= flips <= 42                                # 2 flips per kick, no chatter



def test_bass_pulse_steps_palette_colors_on_hits():
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={"palette": ["#ff0000", "#00ff00", "#0000ff"]})],
                        "master": {"gamma": 1.0}})
    t, flash_colors, prev = 0.0, [], None
    for k in range(90):                                     # 3 s, a kick every 0.5 s
        t += 1 / 30
        c = r.render({"bass": 1.0 if k % 15 < 2 else 0.8, "beat_env": 0.0}, t, 1 / 30, False)[0]
        if max(c) == 255 and (prev is None or max(prev) != 255):
            flash_colors.append(c)
        prev = c
    hues = [max(range(3), key=lambda i: c[i]) for c in flash_colors]
    assert len(hues) >= 5 and all(hues[i] != hues[i + 1] for i in range(len(hues) - 1))   # new color each hit


def test_bass_pulse_goes_dark_in_a_build_and_drops_back_at_full():
    from make_test_wav import synth
    from govee_rt.audio import Analyzer
    gap = (5.0, 9.0)
    sr, x, kicks = synth(bpm=128, seconds=14, kick_gap=gap)
    block = 1024
    an = Analyzer(sr, 15, block)
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={"palette": "white", "color_every": 0})],
                        "master": {"gamma": 1.0}})
    dt = block / sr
    frames = []                                             # (block end time, brightness)
    for i in range(0, len(x) - block, block):
        f = an.process(x[i:i + block])
        frames.append((an.t, max(r.render(f, an.t, dt, f["beat"])[0])))
    last = max(k for k in kicks if k < gap[0])
    drop = min(k for k in kicks if k >= gap[1])
    hold = 0.12

    running = [v for t, v in frames if 2.0 <= t < last + 0.25 or drop <= t < kicks[-1] + 0.25]
    assert min(running) > 0                                 # never black while the kicks run
    dark = [v for t, v in frames if last + 0.25 + 0.3 + 0.35 <= t < drop]
    assert dark and max(dark) == 0                          # black for the build
    went_dark = min(t for t, v in frames if last < t < drop and v == 0)
    assert went_dark - last < 1.2                           # kick tail + dark_after, not seconds later
    assert max(v for t, v in frames if last + hold + 0.1 <= t < drop) < 255   # no flash in the gap
    first_full = min(t for t, v in frames if t >= drop and v == 255)   # block end time
    assert first_full - drop <= 0.05                        # the drop comes back at full, on the kick
    after = [t for t, v in frames if drop + 0.3 <= t < drop + 1.5 and v == 255]
    assert after                                            # and the next kicks still flash


def test_bass_pulse_three_tiers_off_soft_full():
    from make_test_wav import synth
    from govee_rt.audio import Analyzer
    sr, x, kicks = synth(bpm=128, seconds=8, kick_gains=(1.0, 0.4))   # loud, soft, loud, soft ...
    block = 1024
    an = Analyzer(sr, 15, block)
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={
        "palette": "white", "color_every": 0, "floor": 0.0, "strong": 0.75, "soft": 0.45})],
        "master": {"gamma": 1.0}})
    frames = []
    for i in range(0, len(x) - block, block):
        f = an.process(x[i:i + block])
        frames.append((an.t, max(r.render(f, an.t, block / sr, f["beat"])[0])))
    soft = round(0.45 * 255)
    for j, k in enumerate(kicks):
        if k < 2:
            continue
        peak = max(v for t, v in frames if k <= t < k + 0.2)
        assert peak == (255 if j % 2 == 0 else soft)                     # tier by kick strength
        assert min(t for t, v in frames if t >= k and v > 0) - k <= 0.05  # lit on the kick
    between = [v for t, v in frames if t > 2 and all(not (k - 0.01 <= t < k + 0.3) for k in kicks)]
    assert max(between) == 0                                             # off between kicks


def test_only_music_driven_scenes_go_idle(tmp_path):
    from govee_rt.studio import Presets
    idles = {name: fx.scene_idles(fx.normalize_scene(Presets(user_dir=str(tmp_path)).load(name)))
             for name in ("Bass Pulse", "Spectrum", "Chill Plasma", "Paint", "Neon Pulse", "Comet Rush")}
    assert idles == {"Bass Pulse": True, "Spectrum": True, "Chill Plasma": False, "Paint": False,
                     "Neon Pulse": False, "Comet Rush": False}
    assert fx.scene_idles(fx.normalize_scene({"layers": [fx.new_layer("plasma")], "idle": True}))
    assert not fx.scene_idles(fx.normalize_scene({"layers": [fx.new_layer("bass_pulse")], "idle": False}))


def test_non_reactive_scene_keeps_showing_in_silence():
    fake = FakeBleClient(segments=20)

    def blocks():
        while True:
            yield 48000, np.zeros(1024, dtype=np.float32)
            time.sleep(1024 / 48000)
    eng = Engine(1, lambda: govee_rt.GoveeBle(segments=20, mode="stream", cells=60, client_factory=lambda: fake),
                 fps=30, scene={"layers": [fx.new_layer("plasma")]}, idle_after=0.3, idle_speed=12)
    eng.use_audio(blocks(), 1024)
    eng.connect()
    eng.start()
    time.sleep(0.8)
    assert not eng.idle and fake.mode != 0x0A               # no rainbow sketch
    streamed = len(fake.streamed)
    time.sleep(0.3)
    assert len(fake.streamed) > streamed                    # still streaming the plasma
    eng.set_scene({"layers": [fx.new_layer("bass_pulse")]})   # a party mode: idles after silence
    time.sleep(0.8)
    eng.stop()
    assert fake.sketch_effect == 0x13 and fake.bad == 0


# ----------------------------------------------------------------------------- Desire tuning

def _bass_pulse_run(x, sr, **params):
    from govee_rt.audio import Analyzer
    block = 1024
    an = Analyzer(sr, 15, block)
    base = {"palette": "white", "color_every": 0, "floor": 0.0, "strong": 0.75, "soft": 0.45}
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params=dict(base, **params))],
                        "master": {"gamma": 1.0}})
    frames = []
    for i in range(0, len(x) - block, block):
        f = an.process(x[i:i + block])
        frames.append((an.t, max(r.render(f, an.t, block / sr, f["beat"])[0]), f))
    return frames


def _lit_runs(frames, t0=2.0):
    runs, start, armed = [], None, False
    for t, v, _ in frames:
        if t < t0:
            continue
        if not armed:                                      # skip a flash already lit at t0
            armed = v == 0
            continue
        if v > 0 and start is None:
            start = t
        elif v == 0 and start is not None:
            runs.append((start, t))
            start = None
    return runs


def test_hit_min_stops_clap_flicker_over_a_sparse_kick():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=10, kick_every=4, claps=0.35)
    before = _lit_runs(_bass_pulse_run(x, sr))
    after = _lit_runs(_bass_pulse_run(x, sr, hit_min=0.5))
    kicks_after_2s = [k for k in kicks if k >= 2.0]
    assert len(before) > 2 * len(kicks_after_2s)            # the flicker the owner saw
    assert len(after) == len(kicks_after_2s)                # one flash per kick, nothing else
    assert all(min(abs(a - k) for k in kicks) < 0.05 for a, _ in after)


def test_hit_min_costs_no_latency_on_kicks():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=8, kick_gains=(1.0, 0.4))
    def onsets(frames):
        return [min(t for t, v, _ in frames if t >= k and v > 0) - k for k in kicks if k >= 2]
    assert onsets(_bass_pulse_run(x, sr, hit_min=0.5)) == onsets(_bass_pulse_run(x, sr))


def test_sustain_holds_a_held_bass_note():
    from make_test_wav import synth
    b = 60 / 128
    notes = ((11 * b, 1.2), (15 * b, 0.4), (17 * b, 0.8))
    sr, x, kicks = synth(bpm=128, seconds=10, kick_gap=(4.9, 10), bass_notes=notes)
    def lit_for(frames):
        runs = _lit_runs(frames)
        return [next(e - s for s, e in runs if abs(s - t0) < 0.05) for t0, _ in notes]
    held = lit_for(_bass_pulse_run(x, sr, hit_min=0.5, sustain=0.75))
    for (_, length), lit in zip(notes, held):
        assert length - 0.05 <= lit <= length + 0.15        # held as long as the note rings


def test_sustain_does_not_hold_through_a_sub_bass_wall():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=8)
    t = np.arange(len(x)) / sr
    x = x + (0.25 * np.sin(2 * np.pi * 55 * t)).astype(np.float32)   # steady sub under every kick
    x /= np.abs(x).max() * 1.05
    runs = _lit_runs(_bass_pulse_run(x, sr, hit_min=0.5, sustain=0.75, run_gap=0.3))
    assert len(runs) >= len([k for k in kicks if k > 2.2]) - 1   # off between kicks, not one block


def test_trill_detected_only_on_a_trilled_note():
    from make_test_wav import synth
    from govee_rt.audio import Analyzer
    for trill, expect in (((3, 6, 620, 2, 7), True), ((3, 6, 620, 0, 0), False),
                          ((3, 6, 620, 2, 1.5), False)):
        sr, x, _ = synth(bpm=128, seconds=8, trill=trill, claps=0.2)
        an = Analyzer(sr, 15, 1024)
        out = []
        for i in range(0, len(x) - 1024, 1024):
            f = an.process(x[i:i + 1024])
            out.append((an.t, f["trill"]))
        inside = [v for t, v in out if 3.7 < t < 6]
        assert max(v for t, v in out if t < 2.9 or t > 6.3) == 0     # kicks, claps, pad: never
        assert (min(inside) > 0.5) if expect else (max(inside) == 0)


def test_trill_strobe_is_fast_on_off_in_one_color():
    from make_test_wav import synth
    sr, x, _ = synth(bpm=128, seconds=7, kick_gap=(0, 7), pulse=(3.0, 4.0, 1000, 8.6))
    from govee_rt.audio import Analyzer
    an = Analyzer(sr, 15, 1024)
    r = fx.Renderer(1, {"layers": [fx.new_layer("bass_pulse", params={
        "palette": ["#ff0000", "#00ff00", "#0000ff"], "floor": 0.0, "strong": 0.75, "soft": 0.45,
        "hit_min": 0.5, "trill": 0.3, "trill_strobe": 15})], "master": {"gamma": 1.0}})
    block, t, frames = 1024, 0.0, []
    f = an.process(x[:block])
    i = block
    while t < 6.5:                                          # render at 30 fps, audio at 47 blocks/s
        t += 1 / 30
        while i + block <= len(x) and an.t < t:
            f = an.process(x[i:i + block])
            i += block
        frames.append((t, tuple(r.render(f, t, 1 / 30, f["beat"])[0])))
    lit = [(tt, c) for tt, c in frames if 3.0 < tt < 4.2]
    on = [tt for tt, c in lit if max(c) > 0]
    assert on and min(on) < 3.4
    strobe = [c for tt, c in frames if min(on) <= tt <= max(on)]
    assert {max(c) for c in strobe} == {0, 255}             # full and off, nothing between
    assert len({c for c in strobe if max(c) > 0}) == 1      # one color the whole time
    flips = sum((max(a) > 0) != (max(b) > 0) for a, b in zip(strobe, strobe[1:]))
    assert flips >= 0.9 * 30 * (max(on) - min(on))          # on/off every frame: 15 flashes/s
    assert max(max(c) for tt, c in frames if tt > 4.3) == 0   # stops with the note


def test_pulse_trill_on_a_held_note_in_a_chord_but_not_on_a_melody():
    from make_test_wav import synth
    from govee_rt.audio import Analyzer
    def trill_times(**kw):
        sr, x, _ = synth(bpm=128, seconds=8, kick_gap=(0, 8), **kw)
        an = Analyzer(sr, 15, 1024)
        out = []
        for i in range(0, len(x) - 1024, 1024):
            f = an.process(x[i:i + 1024])
            out.append((an.t, f["trill"], f["trill_up"]))
        return out
    out = trill_times(pulse=(3.0, 4.0, 1000, 8.6))           # Desire's B5: 8.6 pulses/s
    on = [t for t, v, _ in out if v > 0]
    assert on and 3.0 < min(on) < 3.35 and max(on) > 3.85   # locks on within two pulses, rides it out
    assert all(3.0 < t < 4.1 for t in on)                    # nothing outside the note
    ups = [u for t, v, u in out if v > 0]
    flips = sum(a != b for a, b in zip(ups, ups[1:]))
    assert flips >= 0.7 * 2 * 8.6 * (max(on) - min(on))      # follows each pulse
    melody = trill_times(melody=(3.0, 5.0, 0.07))            # a fast run of different notes
    assert max(v for t, v, _ in melody) == 0


def test_trill_flickers_mid_to_full_through_the_dark():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=9, kick_gap=(0, 6.5), trill=(2.5, 5.5, 620, 2, 7))
    frames = _bass_pulse_run(x, sr, hit_min=0.5, sustain=0.5, trill=0.3)
    before = [v for t, v, _ in frames if 1.5 < t < 2.5]
    during = [v for t, v, _ in frames if 3.2 < t < 5.4]
    after = [v for t, v, _ in frames if 5.8 < t < 6.4]
    assert max(before) == 0 and max(after) == 0             # no bass, no trill: dark
    assert set(during) == {255, round(0.45 * 255)}          # only mid and full, never off
    flips = sum(a != b for a, b in zip(during, during[1:]))
    assert 10 <= flips / 2.2 <= 18                          # follows the 7 Hz trill (2 flips a cycle)
    plain = _bass_pulse_run(x, sr, hit_min=0.5, sustain=0.5)
    assert max(v for t, v, _ in plain if 3.2 < t < 5.4) == 0   # trill off: the intro stays dark


def test_run_gap_shows_every_kick_of_a_fast_run():
    from make_test_wav import synth
    sr, x, kicks = synth(bpm=128, seconds=9, kick_gap=(4.0, 9), kick_rolls=((5.0, 8, 0.15),))
    def states(frames):
        seq = []
        for t, v, _ in frames:
            if 4.95 < t < 6.6 and (not seq or seq[-1] != v):
                seq.append(v)
        return seq
    toggled = states(_bass_pulse_run(x, sr, hit_min=0.5, sustain=0.75, run_gap=0.3))
    assert toggled[:2] == [0, 255] and toggled[-1] == 0
    run = toggled[1:-1]
    assert len(run) >= 6 and set(run) == {255, round(0.45 * 255)}   # full, soft, full, ... no off
    assert all(a != b for a, b in zip(run, run[1:]))
    spaced = _lit_runs(_bass_pulse_run(x, sr, hit_min=0.5, sustain=0.75, run_gap=0.3))
    assert sum(1 for s, e in spaced if 2.0 < s < 3.9) >= 3  # beats with space still go off between
