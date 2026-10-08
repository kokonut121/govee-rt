"""Bring-up experiments for music sync over BLE. Run each phase with the owner watching.

    tools/ble_run.sh .venv/bin/python tools/music_probe.py rates       # no eyes needed
    tools/ble_run.sh .venv/bin/python tools/music_probe.py inject      # phone-mic stream guesses
    tools/ble_run.sh .venv/bin/python tools/music_probe.py pulse       # beat pulses via brightness
    tools/ble_run.sh .venv/bin/python tools/music_probe.py rollbar     # one bar rolled by the strip

Address comes from ~/.govee-rt/devices.json (run `connect` first). See docs/PROTOCOL.md,
"Music modes over BLE", for what each phase is testing.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import govee_rt  # noqa: E402
from govee_rt.ble import ble_packet, sketch_select  # noqa: E402
from govee_rt.device import Devices  # noqa: E402

def connect():
    prof = Devices().default()
    if not prof or prof.get("transport") != "ble":
        sys.exit("no saved BLE device; run: python -m govee_rt connect --ble")
    g = govee_rt.GoveeBle(prof["address"], segments=prof["segments"], cells=prof.get("cells"),
                          reverse=bool(prof.get("reverse")))
    print("connected", g.name, flush=True)
    return g


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def stream_frame(sub, a, b, c):
    p = bytes([0xA5, 0x02, sub, a & 255, b & 255, c & 255])
    return p + bytes([sum(p) & 0xFF])


def beat_level(t, bpm=120):
    ph = (t * bpm / 60) % 1
    return math.exp(-ph * 6)              # 1.0 on the beat, decays


def rates(g):
    """How many whole-strip commands per second survive (ACKed), with and without pacing."""
    for label, mk in (("brightness 33 04", lambda k: ble_packet(0x33, 0x04, 30 + (k * 7) % 70)),
                      ("solid 33 05 0d", lambda k: ble_packet(0x33, 0x05, 0x0D, *govee_rt.hsv(k / 40, 1, 1)))):
        for hz in (5, 10, 20):
            n0 = g.stats["acks"]
            t0 = time.monotonic()
            for k in range(hz * 3):
                g._run(g._write(mk(k)))
                time.sleep(max(0.0, t0 + (k + 1) / hz - time.monotonic()))
            time.sleep(0.6)
            acks = g.stats["acks"] - n0
            say(f"{label} at {hz:2d} Hz for 3 s: {acks}/{hz * 3} acked")
        t0 = time.monotonic()
        for k in range(30):
            g._run(g._write_acked(mk(k), 0.5))
        say(f"{label} ACK-paced: {30 / (time.monotonic() - t0):.1f} per second")


def inject_select(g):
    inject(g, variants=(("after 33 05 13 03 (music mode select)", 0x03),))


def inject(g, variants=(("stream only", None), ("after 33 05 13 03 (music mode select)", 0x03))):
    """The phone-mic stream with computer-made colors: first as-is, then after a music-mode
    select in case the strip only listens to the stream in that mode."""
    hue = [0.0]

    def color(v):                                   # hue steps each beat, brightness = pulse
        if v > 0.97:
            hue[0] += 0.137
        return govee_rt.hsv(hue[0], 1, 0.08 + 0.92 * v)
    for name, select in variants:
        if select is not None:
            g._run(g._write_acked(ble_packet(0x33, 0x05, 0x13, select, 99), 1.0))
        sub, payload = 0x83, color
        say(f"INJECT {name}: 10 s of a 120 BPM pulse at 25 Hz (watch for 2 flashes per second)")
        t0 = time.monotonic()
        k = 0
        while time.monotonic() - t0 < 10:
            g._run(g._write(stream_frame(sub, *payload(beat_level(time.monotonic() - t0)))))
            k += 1
            time.sleep(0.04)
        replies = [x.hex(" ")[:14] for x in g.notifications[-10:] if x[0] == 0xA5]
        say(f"   sent {k} frames, a5 replies: {replies}")


def pulse(g):
    """Beat pulses with the brightness command over a static picture: 120 BPM, 10 s."""
    g.color(255, 40, 0)
    say("PULSE: orange strip, brightness 100 on each beat fading to 15, 120 BPM, 10 s")
    t0 = time.monotonic()
    k = 0
    while time.monotonic() - t0 < 10:
        lvl = 15 + int(85 * beat_level(time.monotonic() - t0))
        g._run(g._write_acked(ble_packet(0x33, 0x04, lvl), 0.4))
        k += 1
    say(f"   {k} brightness updates in 10 s ({k / 10:.1f}/s)")
    g._run(g._write_acked(ble_packet(0x33, 0x04, 100), 0.5))


def rollbar(g, bar=6, color=(255, 0, 170)):
    """A traveling bar per hit, done by the strip: a picture that is one bar at cell 1 (the
    owner's right end) and black elsewhere, rolled by sketch effect 0x09. Questions for the
    owner: which way and how fast does it roll at each speed, and does re-sending only the
    mode select (one write, as a kick would) put the bar back at the right end?"""
    cells = g.cells or 60
    pic = [color if i < bar else (0, 0, 0) for i in range(cells)]
    for speed in (100, 60):
        say(f"ROLLBAR speed {speed}: {bar}-cell bar at the right end, strip rolls it, 6 s")
        g.show_sketch(pic, 0x09, speed)
        time.sleep(6)
    say("RESTART: speed 100, re-sending only the mode select every 1.5 s, 6 times")
    g.show_sketch(pic, 0x09, 100)
    for k in range(6):
        time.sleep(1.5)
        t = time.monotonic()
        ack = g._run(g._write_acked(sketch_select(g.sketch_code)))
        say(f"   restart {k + 1}: select ACKed {'yes' if ack is not None else 'no'} in {1000 * (time.monotonic() - t):.0f} ms")
    time.sleep(1.5)
    g._run(g._write_acked(ble_packet(0x33, 0x04, 100), 0.5))   # leave brightness at 100


def rollrestart(g, bar=6, color=(255, 0, 170), speed=60, every=3.0, count=5):
    """Only the restart question, slow enough to see: roll 6 s untouched, then re-send the mode
    select every `every` s. Ask: does the bar snap back to the right end at each one?"""
    cells = g.cells or 60
    pic = [color if i < bar else (0, 0, 0) for i in range(cells)]
    say(f"ROLL speed {speed}, no restarts, 6 s")
    g.show_sketch(pic, 0x09, speed)
    time.sleep(6)
    for k in range(count):
        t = time.monotonic()
        ack = g._run(g._write_acked(sketch_select(g.sketch_code)))
        say(f"   RESTART {k + 1}: ACK {'yes' if ack is not None else 'no'} in {1000 * (time.monotonic() - t):.0f} ms")
        time.sleep(every)
    g._run(g._write_acked(ble_packet(0x33, 0x04, 100), 0.5))


if __name__ == "__main__":
    phases = {"rates": rates, "inject": inject, "inject_select": inject_select, "pulse": pulse,
              "rollbar": rollbar, "rollrestart": rollrestart}
    todo = sys.argv[1:] or list(phases)
    g = connect()
    try:
        for name in todo:
            phases[name](g)
    finally:
        g.close()
