# Prompt: bass-synced party lighting on the H612D

Paste everything below the line into a Claude Code session opened in `~/work/govee-rt`,
with the owner at the keyboard, the strip in view, and the Marshall speaker on.

---

You are working in `govee-rt` on the owner's Mac. Read `CLAUDE.md` and `docs/PROTOCOL.md`
before you touch anything. Your job: make the Govee H612D strip follow the bass of whatever
the Mac is playing, tuned for a party. "Best" is judged by the owner watching the strip, not by
you. Nothing below is theoretical; every rate and timing comes from measurements on this strip.

## What "best" means (the owner's taste, already observed or stated)

The strip has exactly three states, and it snaps between them:

- **Full** on a bass hit. A hit is a clean two-level snap: rest, full, rest. Smoothed envelopes
  that follow the bass wobble read as flicker. Do not add smoothing between detection and the
  light. The color steps to the next party color at the same instant as the flash.
- **Rest** at roughly 40% while the bass is present but not hitting, so the room is lit
  between kicks.
- **Dark** when the bass falls below a low threshold: build-ups, breakdowns, the bars right
  before a drop. Fully dark, no faint glow, no fade. It stays dark until the first kick of the
  drop, which comes back at full on the beat. The drop is the moment this whole project is for.

Also:

- Flashes land on the kick drum. A late flash, or a flash during a quiet section, is worse
  than no flash.
- Spatial motion along the strip is wanted, but only if it does not cost beat accuracy.
- The owner wants to leave this running for hours without touching it.

## Hard facts about this hardware (do not re-test these)

- Strip: H612D, BLE only, saved profile in `~/.govee-rt/devices.json` (20 static segments,
  60 Finger Sketch cells, `reverse: true`, `ble_mode: stream`). `play` and `studio` need no
  device flags. From the Claude Code agent, wrap every BLE command in `tools/ble_run.sh`.
- Audio comes only from BlackHole 2ch (`--device "BlackHole 2ch"`). The strip's own microphone
  and all Govee music modes are off the table. Never send a music-mode select (`33 05 13`): it
  hands control to the strip's mic and the strip stops listening to you.
- Channels to the strip, with what each one can do per second with the speaker playing:
  - Stream `a5 02 83 r g b sum`: whole-strip color, no ACK, ~29 frames/s, proven in sync with
    the speaker with no offset. Black is just `0 0 0`. This is the baseline.
  - Brightness `33 04 pct`: whole strip, ACKed in ~116 ms, ~10.8 writes/s when paced on ACKs.
    Writes sent while the strip is busy are dropped (20/s unpaced: 3 of 60 ACKed). The lowest
    value the command takes is 1; whether 1% reads as dark is unknown.
  - Finger Sketch upload (`A3` type 03, 60 cells) plus select: ~3 pictures/s. Too slow for
    beats, fine for a pattern uploaded once.
  - Strip-side animation of a sketch: effect `0x13` (gradient) fades smoothly, effect `0x09`
    (roll) moves one cell at a time and looked jerky at low speed. `show_sketch()` does this.
  - Razer `BB` frames over BLE: ignored. Per-segment static color: ~173 ms per write, too slow.
- The owner plays through a Bluetooth speaker (Marshall Emberton II, A2DP from the same Mac).
  A2DP starves the Govee link: the ACK-paced `hybrid` mode fell to ~2 frames/s with timeouts
  while stream mode held ~29/s. Any design that depends on ACKed writes during playback must be
  re-verified with the speaker playing at party volume, not with the Mac muted.
- Analyzer (`govee_rt/audio.py`): 1024-sample blocks at 48 kHz (21 ms), bass band 40..150 Hz,
  12 ms attack / 180 ms release envelope, per-band AGC with a ceiling that falls over 4 s, onset
  detector on bass spectral flux (threshold mean + 1.5 std over 1.5 s, 250 ms refractory).
  A detected beat wakes the render loop immediately; the loop otherwise runs at 30 fps in
  stream mode with a 15 ms minimum gap between sends.
- The `bass` feature is AGC-normalized. Within about 4 s of the kick stopping it climbs back
  toward 1 on whatever low content remains (a pad, a bass guitar). It cannot drive the dark
  gate, and it makes the first kick after a build look small to a relative threshold. The dark
  gate needs the raw bass level against a slow peak, and the drop needs the onset detector.
- `BassPulse` (`govee_rt/effects.py`) already implements full and rest: relative threshold 0.7
  over the recent low..high range of the bass, hysteresis 0.2, minimum hold 120 ms, 60 ms
  minimum gap, color step per hit, instant release. The `Bass Pulse` preset uses it with floor
  0.45. It has no dark state yet.
- Idle: after 3 s of true silence on BlackHole (RMS under 1e-3) the engine uploads a 60-cell
  rainbow with effect `0x13` speed 50 and stops streaming; audio hands control back at once.
  Keep this. Dark is not idle: the music is still playing, only the bass is gone.

## The recipe, in order

### Stage 1: lock in the baseline, then build the dark state

Stream mode plus `Bass Pulse`. One color on the whole strip, snapping to full on each kick.

```
tools/ble_run.sh .venv/bin/python -m govee_rt play --preset "Bass Pulse" --device "BlackHole 2ch"
```

Run it with a four-on-the-floor track at party volume through the speaker and ask the owner
questions 1 to 5 below. Then build the dark state. It is pure DSP, needs no hardware, and must
land with a CI test before the owner sees it:

- `Analyzer` adds `bass_slow`: the raw bass level divided by a slow peak that holds and then
  decays over about 20 s, floored at the silence level. 0 means no bass for a long time, 1 means
  as loud as the bass has been recently. Unlike `bass`, it does not inflate when the kick stops.
- `BassPulse` adds `dark_below` (default 0.25) and `dark_after` (default 0.3 s). When
  `bass_slow` stays under `dark_below` for `dark_after`, the effect outputs black, whatever the
  floor is. The delay is so a half-bar gap in the kick does not black out the room; a real
  build-up lasts seconds. Entering dark is a snap, not a fade.
- Leaving dark: a detected onset (`c.beat`), or `bass_slow` crossing `dark_below + 0.1`. The
  first kick of a drop counts as a hit: full brightness, color step, same instant. On leaving
  dark, reset the effect's recent low/high followers to the current bass so the level trigger
  is re-armed and does not swallow the second kick either. Make `kick_boost` default on, because
  the onset detector is what catches the drop.
- Test: give `tests/make_test_wav.synth()` a `kick_gap=(t0, t1)` option that removes the kicks
  in that window while the pad and hats keep playing. Render `BassPulse` over the analyzer's
  output and assert: never black while the kicks run, black for the gap after `dark_after`, full
  within 50 ms of the first kick after the gap, and no flash inside the gap.

Then play a track the owner knows that has a real build and drop, and ask questions 6 to 8.
Tune only these, one at a time, and write down what changed:

- `dark_below` 0.25: raise if the build still glows, lower if the strip goes dark during a
  verse where the kick is merely soft.
- `dark_after` 0.3 s: raise if normal breaks between phrases black out, lower if the owner
  wants the build to go dark sooner.
- `threshold` 0.70: raise toward 0.8 if sustained bass notes trigger extra flashes, lower
  toward 0.6 if soft kicks are missed.
- `kick_boost` on: turn it off only if the owner sees double flashes on long bass notes, and
  then make sure the drop still exits dark.
- `hold` 0.12 s: 0.10 to 0.15 is the usable range. Shorter is invisible, longer smears into
  the next kick above 140 BPM.
- `floor` 0.45: 0.3 to 0.5. Lower is more dramatic, higher keeps the room lit.
- `color_every` 1 for drops; 4 (one bar) reads calmer for verses. Do not animate `drift`
  during a drop; the color change should be the hit.

Do not continue to Stage 2 until the owner says the hits land on the beat and the drop comes
out of black at full.

### Stage 2: add motion without touching the beat path

Split the work: the strip animates a color pattern by itself, the Mac sends only the three
states as brightness. This is the "custom Rolling" plan from `CLAUDE.md`.

- Pattern: 60 cells, 4 to 6 saturated party colors in blocks of 10 to 15 cells (for example
  magenta, cyan, orange, lime, violet), uploaded once with `show_sketch(colors, effect, speed)`.
  Candidates: `0x13` at speed 60 and `0x09` at speed 100. The owner has not yet said which looks
  better; show both for 30 s each and ask.
- States as brightness: full is 100, rest is 35 to 45, dark is 1 (the lowest the command
  takes). On each hit write 100, after `hold` write the rest value. Two ACK-paced writes per hit
  means at most ~5 hits/s; that covers single kicks up to about 150 BPM. If hits come faster,
  skip the oldest, never queue. Latest state wins.
- If the owner says 1% still glows, try power off (`33 01 00`) for dark and power on at the
  drop, both ACKed in ~86 ms, and ask whether the strip comes back showing the same pattern at
  the right brightness. If it does not, dark stays at 1% in this mode and the owner decides
  whether the glow is acceptable or Stage 1 is the party mode.
- Implement it as a new `GoveeBle` mode (name it `pattern`): `rt_enable()` uploads the pattern
  and selects it, `rt_frame()` maps the frame's peak to the three brightness levels like
  `_send_hybrid()` does, but never re-uploads the picture. Re-upload only on `rt_enable()`,
  on idle exit, and if the brightness ACK stops arriving. `rt_disable()` restores brightness
  100. Add a test against `tests/fake_govee_ble.py` that counts uploads (expect 1) and
  brightness writes per hit (expect 2), and checks that a black frame writes brightness 1.
- The scene for this mode is `Bass Pulse` with `floor` 0.4 and `release` 0; the palette is
  irrelevant because the strip owns the color. Save it as a preset named `Party Bass`.
- First verification is the A2DP question: 60 s of music at party volume through the Marshall,
  then read the `BLE:` stats line. Pass: at least 90% of brightness writes ACKed within 0.4 s
  and the owner says the flash still lands on the kick. Fail: ACK rate under 50% or the owner
  sees flashes drifting late. On fail, Stage 1 is the shipped mode; record the numbers in
  `CLAUDE.md` and stop trying to make ACKed writes work under A2DP.

### Stage 3: polish, only after Stage 2 is decided

- Energy-adaptive color stepping: `color_every` 1 right after leaving dark and while energy is
  high, 4 otherwise. Keep it a single rule; no mood detection.
- Offset knob `--offset-ms` (positive delays the light). Default 0. Add it only if the owner
  reports the lights consistently early or late. Physics: the analyzer adds 0 to 21 ms, the BLE
  stream path tens of ms, the A2DP speaker typically 150 to 250 ms. Today they cancel to the
  owner's eye; do not "fix" latency without the owner's report.
- If the owner says the lights are late with no offset: try `--block 512` (11 ms analysis),
  then check the `fps` field in the studio status stays at 30.

## Rules that are not negotiable

- Hardware observations come from the owner. When a step needs eyes on the strip, stop and ask
  one specific question; do not guess and continue.
- One tuning change at a time, then ask again. Record each result in `CLAUDE.md` under
  "Music sync on this strip" with the date.
- Stream mode: one 7-byte write per rendered frame at an even 30 fps, never bursts. Brightness:
  ACK-paced, at most 8 writes/s, skip rather than queue.
- Never select a music mode. Never send razer frames over BLE. Always `rt_disable()` on every
  exit path and leave brightness at 100.
- DSP stays numpy-only. `pytest -q` passes before and after. Anything protocol-level or
  DSP-level gets a test against the fake devices or the synthetic track; CI never needs
  hardware or audio.
- No em dashes in docs or comments.

## Questions to ask the owner, verbatim

Stage 1 baseline, after 60 s of a track they know:

1. Does every kick get a flash, or are some missed?
2. Any flashes between kicks, or on bass notes that are not kicks?
3. Are the flashes early, late, or on the beat? If off, by a little or a lot?
4. Is the strip dark enough between hits, or too dark?
5. Does the color change feel like part of the hit, or like a separate event?

Stage 1 dark state, on a track with a real build and drop:

6. In the build-up, did the strip go fully dark? How long after the kick stopped?
7. Did the first kick of the drop come back at full, on the beat?
8. Did it ever go dark while the kick was still going?

Stage 2, after 30 s of each motion:

9. Gradient flow at speed 60 or roll at speed 100: which one looks like a party?
10. With the motion running, does the flash still land on the kick as well as in Stage 1?
11. Did the motion stutter or pause when a flash happened?
12. In the build, is 1% brightness dark, or does the strip glow?

Sign-off:

13. Would you leave this running for a three hour party? What is the first thing that would
    annoy you?

## Deliverables

- The dark state: `bass_slow` in `audio.py`, `dark_below` / `dark_after` in `BassPulse`, the
  `kick_gap` option in `tests/make_test_wav.py`, and the test described above.
- `Party Bass` preset in `govee_rt/presets/`, the `pattern` BLE mode in `govee_rt/ble.py` with
  tests in `tests/test_ble.py`.
- `CLAUDE.md` "Music sync on this strip" updated with every number the owner reported, the
  answer to the A2DP question, and whether 1% brightness reads as dark, so the next session
  does not repeat the experiments.
- A one-line start command in the `README.md` that the owner can run before guests arrive.
