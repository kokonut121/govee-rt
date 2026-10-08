"""Command line interface: govee-rt [command] ...  (or python -m govee_rt)"""

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
import webbrowser

from . import __version__
from .audio import Analyzer, live_blocks, wav_blocks
from .ble import BLE_MAX_SEGMENTS, STREAM_FPS, GoveeBle, ble_scan
from .colors import hsv
from .device import DEVICES_FILE, Devices, choose_segments, describe, sku_from_name
from .effects import default_scene, normalize_scene
from .engine import Engine
from .protocol import GoveeLan, scan
from .quickstart import bluetooth_hint, debug, doctor, find_loopback, setup_audio
from .visuals import Visualizer, preview_line

LAST_SCENE = os.path.join(os.path.dirname(DEVICES_FILE), "last_scene.json")
USAGE = """\
Music-reactive control for Govee RGBIC light strips.

Just run `govee-rt`: it finds your light (Wi-Fi, then Bluetooth), sets up audio capture,
and opens the studio in your browser. Everything after that happens in the browser.

Other commands, for when you want them:
  govee-rt doctor          what's set up and what's missing
  govee-rt audio-setup     route your computer's sound to the lights (macOS: BlackHole)
  govee-rt connect         re-detect the light (--segments N to fix the count)
  govee-rt play --preset "Bass Pulse"    run a look without the browser
  govee-rt <command> -h    options for any command
"""


# --------------------------------------------------------------------------- commands

def cmd_scan(a):
    print("scanning (multicast 239.255.255.250:4001, replies on :4002)...")
    found = scan(a.timeout, a.target)
    if not found:
        print("nothing answered. Check: LAN Control toggled on in the Govee app; same Wi-Fi/VLAN as "
              "the strip; firewall allows inbound UDP 4002; or pass --target <strip-ip>.")
        return
    for ip, d in found.items():
        print(f"  {ip:15s}  sku={d.get('sku')}  device={d.get('device')}  "
              f"wifi_fw={d.get('wifiVersionSoft')}  ble_fw={d.get('bleVersionSoft')}")


def open_light(a, segments=None):
    """GoveeLan for --ip, GoveeBle for --ble. Reuses the connection made by ensure_profile()."""
    pre = getattr(a, "_open_light", None)
    if pre is not None:
        a._open_light = None
        return pre
    prof = getattr(a, "profile", None) or {}
    if getattr(a, "ble", None):
        mode = a.ble_mode or "segments"
        if mode in ("sketch", "hybrid", "stream"):
            static = prof.get("segments") or 15
            cells = prof.get("cells") or (segments or a.segments if mode != "stream" else None)
        else:
            static = segments or a.segments or prof.get("segments") or 15
            cells = None
        print(f"connecting over Bluetooth ({a.ble}) ...", flush=True)
        g = GoveeBle(a.ble, segments=min(static, BLE_MAX_SEGMENTS), mode=mode, cells=cells,
                     reverse=bool(prof.get("reverse")))
        print(f"connected to {g.name or a.ble}, mtu {g.mtu}, mode {mode}", flush=True)
        return g
    g = GoveeLan(a.ip)
    g.reverse = bool(prof.get("reverse"))
    return g


def identify_and_save(a, g=None, manual=None, measure=True):
    """Ask the light about itself, pick a segment count, remember it as the default device."""
    if getattr(a, "ble", None):
        g = g or GoveeBle(a.ble, segments=BLE_MAX_SEGMENTS, mode="segments")
        print("reading model, firmware and segment layout from the strip ...", flush=True)
        info = g.identify(measure=measure and not manual)
        info["sku"] = sku_from_name(info.get("name"))
    else:
        reply = scan(timeout=2.0, targets=[a.ip]).get(a.ip, {})
        info = {"transport": "lan", "ip": a.ip, "sku": reply.get("sku"), "mac": reply.get("device"),
                "firmware": reply.get("wifiVersionSoft"), "hardware": reply.get("wifiVersionHard"),
                "ble_firmware": reply.get("bleVersionSoft")}
    info["segments"], info["segments_source"] = choose_segments(info, manual)
    prev = Devices().find(info["transport"], info.get("address") or info.get("ip")) or {}
    info["reverse"] = prev.get("reverse", False) if getattr(a, "reverse", None) is None else a.reverse
    if info["transport"] == "ble":
        info["cells"] = getattr(a, "cells", None) or sketch_cells(info)
        info["ble_mode"] = a.ble_mode or ("sketch" if info["cells"] else "segments")
    profile = Devices().save(info)
    if g is not None:
        g.segments = min(profile["segments"], BLE_MAX_SEGMENTS)
        g.cells = profile.get("cells") or g.cells
        g.mode = profile.get("ble_mode", g.mode)
        g.reverse = profile["reverse"]
    return profile, g


def ensure_profile(a):
    """First connection to a light with no saved profile: identify it before anything renders,
    so the segment count is right from the first frame. Keeps the connection for reuse."""
    if getattr(a, "no_send", False) or getattr(a, "profile", None) or not (a.ip or a.ble):
        return
    profile, g = identify_and_save(a)
    a._open_light = g
    a.profile = profile
    if getattr(a, "ble", None) and not a.ble_mode:
        a.ble_mode = profile.get("ble_mode")
    if a.segments is None:
        a.segments = render_count(profile, a)
    print(describe(profile), flush=True)


def sketch_cells(info):
    """Finger Sketch resolution. H612x strips show 3 cells per static segment in the Govee app
    (H612D: 20 segments, 60 cells, verified). Other models: unknown, so no sketch mode."""
    sku = info.get("sku") or ""
    if sku.startswith("H612") and info.get("segments"):
        return min(255, 3 * int(info["segments"]))
    return None


def render_count(profile, a):
    """How many colors effects should render: sketch cells, else static segments."""
    mode = getattr(a, "ble_mode", None) or profile.get("ble_mode")
    if profile.get("transport") == "ble":
        if mode == "stream":
            return 1                      # one color for the whole strip: preview what it shows
        if mode in ("sketch", "hybrid") and profile.get("cells"):
            return profile["cells"]
    return profile["segments"]


def cmd_connect(a):
    if not (a.ip or a.ble):
        a.ble = "auto"
    profile, g = identify_and_save(a, manual=a.segments, measure=not a.no_measure)
    print(describe(profile))
    if g is None:
        g = open_light(a, segments=profile["segments"])
    try:
        n = render_count(profile, a)
        g.power(True)
        g.brightness(100)
        g.rt_enable()
        g.rt_frame([hsv(i / n, 1, 1) for i in range(n)])
        if hasattr(g, "flush"):
            g.flush(timeout=0.25 * n + 3)   # static writes are ACK-paced, ~0.2 s each
        print(f"\nThe strip should now show {n} color bands, red at segment 1 (the end you want the\n"
              f"strip to start from; the studio preview shows it on the left). If the count is off,\n"
              f"run probe to find it by eye, then: python -m govee_rt connect --segments <N>\n"
              f"If red is at the wrong end: python -m govee_rt connect --reverse (or --no-reverse)")
        time.sleep(1.0)
    finally:
        g.close()
    print("saved as the default device; other commands no longer need --ble/--ip/--segments.")


def cmd_info(a):
    devs = Devices()
    default = devs.default()
    print(describe(default))
    others = [p for k, p in devs.all().items() if not default or k != default.get("id")]
    for p in others:
        print("\n(also saved)\n" + describe(p))


def where(a):
    return f"BLE {a.ble}" if getattr(a, "ble", None) else a.ip


def cmd_ble_scan(a):
    print(f"scanning Bluetooth for {a.timeout:.0f}s ...")
    found = asyncio.run(ble_scan(a.timeout))
    if not found:
        print("no Govee lights advertising. Close the Govee app on your phone (the strip accepts one\n"
              "connection at a time and stops advertising while connected) and try again.")
    for rssi, d, name in found:
        print(f"  {rssi:4d} dBm  {d.address}  {name}")


def cmd_solid(a):
    ensure_profile(a)
    g = open_light(a)
    try:
        g.power(True)
        g.brightness(a.brightness)
        g.color(*a.rgb)
        print(f"sent power/brightness/color to {where(a)}")
    finally:
        g.close()


def cmd_probe(a):
    """Find the segment count by eye.

    LAN / razer: rainbow frames with increasing N; the largest N that still shows N bands wins.
    BLE segments: light one mask bit at a time; the owner reports which steps light a new band.
    """
    if getattr(a, "ble", None) and a.ble_mode != "razer":
        a.ble_mode = "segments"          # walk the static segment mask
    ble_seg = getattr(a, "ble", None) and a.ble_mode == "segments"
    n_probe = a.max_segments or ((a.profile or {}).get("segments", 20) + 4)
    a.segments = min(n_probe, BLE_MAX_SEGMENTS)
    g = open_light(a, segments=a.segments)
    g.power(True)
    g.brightness(a.brightness)
    g.rt_enable()
    time.sleep(0.2)
    try:
        if ble_seg:
            n = a.segments
            print("Watch the strip. Each step lights segment k white on a dim blue background.\n"
                  "Note the last k that lights a new spot, and whether k=1 is at the controller end.")
            for k in range(n):
                cols = [(0, 0, 40)] * n
                cols[k] = (255, 255, 255)
                print(f"  segment {k + 1:2d}", flush=True)
                g.rt_frame(cols)
                time.sleep(a.hold)
            print(f"  rainbow across all {n} mask bits")
            g.rt_frame([hsv(i / n, 1, 1) for i in range(n)])
            time.sleep(a.hold)
            return
        counts = a.counts or [6, 10, 12, 15, 18, 20, 24, 30, 36, 40, 48, 60, 72, 90, 120]
        print("Watch the strip. Each step draws a rainbow with N distinct bands; the largest N that\n"
              "still shows N bands (and isn't ignored) is your segment count. Ctrl-C to stop.")
        for n in counts:
            cols = [hsv(i / n, 1, 1) for i in range(n)]
            print(f"  N = {n:3d}  ", end="", flush=True)
            t_end = time.monotonic() + a.hold
            while time.monotonic() < t_end:
                g.rt_frame(cols)
                time.sleep(0.05)
            print("ok")
    except KeyboardInterrupt:
        pass
    finally:
        g.rt_disable()
        g.close()


def cmd_chase(a):
    ensure_profile(a)
    n = a.segments
    g = open_light(a, segments=n)
    g.power(True)
    g.brightness(a.brightness)
    g.rt_enable()
    print(f"chasing one lit segment across {n} segments at {a.fps} fps. Ctrl-C to stop.")
    i = 0
    try:
        while True:
            cols = [(0, 0, 0)] * n
            cols[i % n] = hsv((i / n) % 1.0, 1, 1)
            cols[(i - 1) % n] = hsv(((i - 1) / n) % 1.0, 1, 0.25)
            g.rt_frame(cols)
            i += 1
            time.sleep(1.0 / a.fps)
    except KeyboardInterrupt:
        pass
    finally:
        if isinstance(g, GoveeBle):
            print(f"\nBLE: {dict(g.stats)}")
        g.rt_disable()
        g.close()


def light_factory(a, segments):
    """Callable that opens and configures the light, or None for preview-only runs."""
    if getattr(a, "no_send", False) or not (a.ip or a.ble):
        return None

    def make():
        g = open_light(a, segments=segments)
        g.power(True)
        g.brightness(a.brightness)
        return g
    return make


def attach_audio(engine, a):
    if a.device is None and not a.wav and not getattr(a, "demo", False):
        a.device = find_loopback()        # None: no loopback input, run the demo beat
    if a.wav:
        def looped():
            while True:
                yield from wav_blocks(a.wav, a.block)
        engine.use_audio(looped(), a.block, name=f"wav {a.wav}")
    elif a.device is not None:
        engine.use_audio(live_blocks(a.device, a.block, a.samplerate), a.block, name=f"input {a.device}")
    else:
        engine.use_demo(a.demo_bpm)


def load_scene(a, presets):
    if a.scene:
        with open(a.scene) as f:
            return json.load(f)
    if a.preset:
        return presets.load(a.preset)
    try:                                  # the look the studio was showing when it last closed
        with open(LAST_SCENE) as f:
            return normalize_scene(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return default_scene()


def save_last_scene(scene):
    try:
        os.makedirs(os.path.dirname(LAST_SCENE), exist_ok=True)
        with open(LAST_SCENE, "w") as f:
            json.dump(scene, f, indent=1)
    except OSError:
        pass


def make_engine(a, scene):
    fps = a.fps or (STREAM_FPS if getattr(a, "ble", None) and a.ble_mode == "stream" else 40)
    idle = a.idle_after if a.idle_after and a.idle_after > 0 else None
    return Engine(a.segments, light_factory(a, a.segments), fps=fps, scene=scene,
                  idle_after=idle, idle_speed=a.idle_speed)


def cmd_studio(a):
    from .studio import Presets, serve
    ensure_profile(a)
    presets = Presets()
    engine = make_engine(a, load_scene(a, presets))
    attach_audio(engine, a)
    for port in range(a.port, a.port + 20):   # another studio may hold the default port
        try:
            httpd = serve(engine, presets, port, device=getattr(a, "profile", None))
            break
        except OSError:
            if port == a.port + 19:
                raise
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"studio at {url}  ({engine.source}; Ctrl-C to stop)", flush=True)
    if not a.no_open:
        webbrowser.open(url)
    engine.start()
    threading.Thread(target=engine.connect, daemon=True).start()   # BLE scan can take a while
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        httpd.server_close()
        save_last_scene(engine.get_scene())
        print(f"\nstopped. {dict(engine.stats)}")


def cmd_start(a):
    """No command given: find the light (saved, then Wi-Fi, then Bluetooth), make sure the
    computer's audio reaches a loopback input, open the studio."""
    print(f"govee-rt {__version__}: music-reactive Govee lights\n", flush=True)
    if not (a.ip or a.ble or a.no_send):
        print("looking for your light on Wi-Fi ...", flush=True)
        try:
            found = scan(timeout=2.0)
        except OSError:
            found = {}
        a.segments = a.segments_given     # resolve_device filled in a preview-only count
        if found:
            a.ip, info = next(iter(found.items()))
            print(f"  found {info.get('sku') or 'a Govee light'} at {a.ip}", flush=True)
        else:
            a.ble = "auto"
            print("  none on Wi-Fi; searching Bluetooth (close the Govee app on your phone first) ...",
                  flush=True)
    elif a.profile:
        print(describe(a.profile).splitlines()[0], flush=True)
    if a.wav is None and a.device is None and not a.demo:
        print("checking audio ...", flush=True)
        a.device = setup_audio(interactive=True)
        print(f"  listening to {a.device}" if a.device else "  no loopback input: running a demo beat",
              flush=True)
    cmd_studio(a)


def cmd_doctor(a):
    doctor(describe(Devices().default()), __version__)


def cmd_audio_setup(a):
    name = setup_audio(interactive=True)
    print(f"audio input for the lights: {name}" if name else "audio not set up yet")


def cmd_play(a):
    from .studio import Presets
    ensure_profile(a)
    engine = make_engine(a, load_scene(a, Presets()))
    attach_audio(engine, a)
    engine.connect()
    if engine.light_error:
        print(f"light: {engine.light_error} (retrying every 5 s)", file=sys.stderr)
    print(f"playing {engine.get_scene()['name']} on {a.segments} segments from {engine.source}. Ctrl-C to stop.")
    try:
        engine.run()
    finally:
        engine.stop()
        print(f"\n{dict(engine.stats)}")


def cmd_devices(a):
    import sounddevice as sd
    print(sd.query_devices())
    print("\nmacOS: install BlackHole (brew install blackhole-2ch), make a Multi-Output Device in\n"
          "Audio MIDI Setup (speakers + BlackHole), set it as system output, then --device 'BlackHole 2ch'.\n"
          "Linux: use the PipeWire/Pulse 'Monitor of ...' source.\n"
          "Windows: PortAudio has no loopback; use pyaudiowpatch, or VB-Cable as the device.")


def cmd_audio(a):
    ensure_profile(a)
    n = a.segments
    g = None if a.no_send else open_light(a, segments=min(n, BLE_MAX_SEGMENTS))
    if g:
        g.power(True)
        g.brightness(a.brightness)
        g.rt_enable()

    src = wav_blocks(a.wav, a.block) if a.wav else live_blocks(a.device, a.block, a.samplerate)
    an = None
    vis = Visualizer(n, a.mode, a.hue, a.hue_span)
    frame_dt = 1.0 / a.fps
    next_send = 0.0
    pending_beat = False
    frames = 0
    t0 = time.monotonic()
    print(f"streaming {a.mode} to {'(nothing)' if a.no_send else where(a)} on {n} segments, "
          f"max {a.fps} fps. Ctrl-C to stop.")
    try:
        for sr, block in src:
            if an is None:
                an = Analyzer(sr, n, a.block)
            if len(block) != a.block:
                continue
            f = an.process(block)
            if f["beat"]:
                pending_beat = True   # never let a beat fall between two sent frames
            now = time.monotonic()
            if now < next_send and not pending_beat:
                continue
            # schedule from the previous slot, not from now, so audio blocks (21 ms) and
            # frames (25 ms) interleave instead of beating against each other
            next_send = max(next_send + frame_dt, now - frame_dt)
            if pending_beat:
                f = dict(f, beat=True)
                pending_beat = False
            cols = vis.render(f)
            if g:
                g.rt_frame(cols)
            frames += 1
            if a.preview:
                print(preview_line(cols, f), end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        el = time.monotonic() - t0
        print(f"\n{frames} frames in {el:.1f}s ({frames / max(el, 1e-6):.1f} fps)")
        if g:
            if isinstance(g, GoveeBle):
                print(f"BLE: {dict(g.stats)}")
            g.rt_disable()
            g.close()


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "start")          # bare `govee-rt` (or `govee-rt --no-send`) is the wizard
    p = argparse.ArgumentParser(prog="govee-rt", description=USAGE,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"govee-rt {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("scan")
    s.add_argument("--timeout", type=float, default=3.0)
    s.add_argument("--target", action="append", help="extra unicast/broadcast IP to poke (repeatable)")
    s.set_defaults(fn=cmd_scan)

    def target(sp):
        sp.add_argument("--ip", help="strip IP (Wi-Fi LAN control)")
        sp.add_argument("--ble", nargs="?", const="auto", metavar="NAME|ADDRESS",
                        help="use Bluetooth instead; optional name substring or address")
        sp.add_argument("--ble-mode", choices=["stream", "hybrid", "sketch", "segments", "razer", "roll", "roll_hits", "roll_flash"],
                        help="BLE output: stream (whole strip, ~30/s), hybrid (60-cell picture ~3/s + "
                             "brightness beats ~10/s), sketch (60-cell pictures ~3/s), segments, razer, "
                             "roll (one bar rolled by the strip), roll_hits (a new rolling bar per hit), "
                             "roll_flash (palette bars rolled by the strip, brightness on hits)")

    def common(sp):
        target(sp)
        sp.add_argument("--brightness", type=int, default=100)

    s = sub.add_parser("connect", help="identify the strip and remember it as the default device")
    target(s)
    s.add_argument("--segments", type=int, help="set the segment count by hand (e.g. after probe)")
    s.add_argument("--no-measure", action="store_true", help="skip the segment readback test")
    s.add_argument("--cells", type=int, help="Finger Sketch cell count, if not 3x segments")
    s.add_argument("--reverse", dest="reverse", action="store_true", default=None,
                   help="segment 1 is at the far end from where you want the strip to start")
    s.add_argument("--no-reverse", dest="reverse", action="store_false")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("info", help="show the remembered device(s)")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("ble-scan")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(fn=cmd_ble_scan)

    s = sub.add_parser("solid"); common(s)
    s.add_argument("--rgb", type=int, nargs=3, default=[255, 60, 0])
    s.set_defaults(fn=cmd_solid)

    s = sub.add_parser("probe"); common(s)
    s.add_argument("--counts", type=int, nargs="*")
    s.add_argument("--hold", type=float, default=2.5, help="seconds per step")
    s.add_argument("--max-segments", type=int, help="BLE: how many mask bits to walk (default saved count + 4)")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("chase"); common(s)
    s.add_argument("--segments", type=int)
    s.add_argument("--fps", type=float, default=30)
    s.set_defaults(fn=cmd_chase)

    def engine_args(sp):
        target(sp)
        sp.add_argument("--no-send", action="store_true", help="preview only, don't talk to a light")
        sp.add_argument("--brightness", type=int, default=100, help="hardware brightness %%")
        sp.add_argument("--segments", type=int, help="segments to render (default: the saved device's)")
        sp.add_argument("--fps", type=float, help="render rate (default 30 in BLE stream mode, else 40)")
        sp.add_argument("--idle-after", type=float, default=3.0,
                        help="seconds of silence before the idle rainbow (0 = never)")
        sp.add_argument("--idle-speed", type=int, default=50,
                        help="idle rainbow flow speed, animated by the strip itself (0 = still)")
        sp.add_argument("--preset", help="start from a saved/built-in preset by name")
        sp.add_argument("--scene", help="start from a scene JSON file")
        sp.add_argument("--device", help="audio input name/index (loopback device, e.g. 'BlackHole 2ch')")
        sp.add_argument("--wav", help="loop a wav file as the audio source")
        sp.add_argument("--demo-bpm", type=float, default=124, help="synthetic beat when no audio source")
        sp.add_argument("--samplerate", type=int)
        sp.add_argument("--block", type=int, default=1024)

    def studio_args(sp):
        engine_args(sp)
        sp.add_argument("--demo", action="store_true", help="synthetic beat even if a loopback input exists")
        sp.add_argument("--port", type=int, default=8765)
        sp.add_argument("--no-open", action="store_true", help="don't open a browser")

    s = sub.add_parser("start", help="find the light, set up audio, open the studio (the default)")
    studio_args(s)
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("studio", help="browser control panel with live preview")
    studio_args(s)
    s.set_defaults(fn=cmd_studio)

    s = sub.add_parser("doctor", help="check what's set up and what's missing")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("audio-setup", help="route the computer's sound to a loopback input")
    s.set_defaults(fn=cmd_audio_setup)

    s = sub.add_parser("play", help="run a preset headless")
    engine_args(s)
    s.add_argument("--demo", action="store_true", help="synthetic beat even if a loopback input exists")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("devices")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("audio")
    target(s)
    s.add_argument("--no-send", action="store_true", help="analysis only, no packets")
    s.add_argument("--brightness", type=int, default=100)
    s.add_argument("--segments", type=int)
    s.add_argument("--mode", choices=["spectrum", "pulse", "vu", "wave"], default="spectrum")
    s.add_argument("--hue", type=float, default=0.0, help="base hue 0..1 (0 red, .33 green, .66 blue)")
    s.add_argument("--hue-span", type=float, default=0.75, help="spectrum mode: hue range across strip")
    s.add_argument("--fps", type=float, default=40)
    s.add_argument("--block", type=int, default=1024, help="FFT/audio block size (1024 @ 48k = 21 ms)")
    s.add_argument("--device", help="sounddevice input name/index (loopback device)")
    s.add_argument("--samplerate", type=int)
    s.add_argument("--wav", help="drive from a wav file instead of live audio")
    s.add_argument("--preview", action="store_true", help="ASCII preview of the strip in the terminal")
    s.set_defaults(fn=cmd_audio)

    a = p.parse_args(argv)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):   # closed terminal: clean exit
        if sig is not None:
            signal.signal(sig, _interrupt)
    a.segments_given = getattr(a, "segments", None)
    if hasattr(a, "ble"):
        if a.ip and a.ble:
            p.error("use --ip or --ble, not both")
        resolve_device(a, p)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if debug():
            raise
        hint = bluetooth_hint(e)
        print(f"\nerror: {e}" + (f"\n  fix: {hint}" if hint else "") +
              "\n  (set GOVEE_RT_DEBUG=1 for the full traceback; govee-rt doctor checks the setup)",
              file=sys.stderr)
        sys.exit(1)


def resolve_device(a, p):
    """Fill in --ble/--ip and --segments from the remembered device when not given."""
    devs = Devices()
    default = devs.default()
    a.profile = None
    if a.cmd == "connect":
        return
    if not (a.ip or a.ble) and not getattr(a, "no_send", False):
        if default:
            if default["transport"] == "ble":
                a.ble = default["address"]
            else:
                a.ip = default["ip"]
        elif a.cmd not in ("studio", "start"):
            p.error(f"{a.cmd} needs --ble or --ip (or run govee-rt to find your light)")
    if a.ble == "auto" and default and default["transport"] == "ble":
        a.ble = default["address"]
    if a.ble:
        a.profile = devs.find("ble", a.ble)
    elif a.ip:
        a.profile = devs.find("lan", a.ip)
    prof = a.profile or (default if getattr(a, "no_send", False) else None)
    if hasattr(a, "ble_mode") and not a.ble_mode and prof and prof.get("transport") == "ble":
        a.ble_mode = prof.get("ble_mode")
    if hasattr(a, "segments") and a.segments is None:
        if prof:
            a.segments = render_count(prof, a)
        elif not (a.ip or a.ble):
            a.segments = 15
