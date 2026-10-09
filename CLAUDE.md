# CLAUDE.md

Audio-reactive controller for a Govee Strip Light S (H612 family, 49.2 ft, Wi‑Fi RGBIC).
Goal: whatever the computer is playing (Spotify, YouTube, anything) drives per-segment
animations on the strip with low enough latency that beats land on the beat.

Read `docs/PROTOCOL.md` before touching anything that sends packets.

## Where things stand

Verified (in CI, against `tests/fake_govee.py`, not the real strip):

- Wire format for the real-time "razer" mode is byte-exact with OpenRGB's working
  implementation (`tests/test_govee_rt.py::test_enable_disable_match_openrgb`).
- Discovery, official commands, and real-time frames round-trip through the fake device.
- Audio pipeline: 24/26 kicks detected within ±50 ms, 0 false positives on the synthetic
  128 BPM track; all four modes hold 40 fps from a WAV source.
- BLE transport (`GoveeBle`) against `tests/fake_govee_ble.py`: H617A segment packet layout,
  segment planning, latest-frame-wins pacing on a slow link, encrypted-device refusal.
- Effects engine (17 effects, layers, blend modes, per-param audio modulation), 16 built-in
  presets, render loop and studio HTTP API (`tests/test_effects.py`). Studio UI checked in a
  browser in preview-only mode (`studio --no-send`), desktop and phone widths.

Not yet verified, and only the owner can observe these (Claude cannot see the strip):

- That this SKU accepts `razer` frames at all. OpenRGB confirms the 3 m sibling (H612F);
  the 49.2 ft unit should match but hasn't been tried.
- The segment count. H612F has 12; longer variants probably have more. Unknown until
  `probe` is run and the owner reports what they saw.
- Whether a frame whose N differs from the segment count is ignored, truncated, or resampled.
- Real-world latency from audio to light.
- Which OS the owner runs this on, and therefore which loopback audio path applies.

## Runbook (do these in order, with the owner watching the strip)

1. `pip install -r requirements.txt` (sounddevice needs PortAudio; the macOS/Windows wheels
   bundle it, Linux needs `libportaudio2`).
2. `pytest -q` must pass before and after any change.
3. Owner enables **LAN Control** in the Govee app for the strip (gear icon → LAN Control).
4. `python -m govee_rt scan`. If empty, ask the owner for the strip's IP from the router and
   use `--target <ip>`. Record the SKU from the reply in the "Device record" section below.
5. `python -m govee_rt solid --ip <ip> --rgb 255 40 0`. Ask the owner: did it turn orange?
   This proves LAN Control and the IP; it does not touch real-time mode.
6. `python -m govee_rt probe --ip <ip>`. Ask the owner to describe each step: how many
   distinct color bands they saw for each N, and at which N the strip stopped changing.
   Record the segment count below. If nothing changes at any N, real-time mode is not
   honored on this firmware; go to the "If razer mode fails" section.
7. `python -m govee_rt chase --ip <ip> --segments <N>`. Owner confirms a single dot runs
   end to end smoothly. Note any stutter (frame drops) or lag.
8. Audio capture. Ask which OS. Then:
   - macOS: `brew install blackhole-2ch`, Audio MIDI Setup → create Multi-Output Device with
     the speakers/headphones and BlackHole 2ch, select it as system output. Device name for
     the script is `"BlackHole 2ch"`.
   - Linux (PipeWire/Pulse): use the `Monitor of <sink>` input; `python -m govee_rt devices`
     lists it.
   - Windows: PortAudio in `sounddevice` has no WASAPI loopback. Either install VB-Cable and
     route output through it, or swap `live_blocks()` to `pyaudiowpatch` (it exposes loopback
     devices directly). This is a roadmap item; don't try to make plain sounddevice do it.
9. `python -m govee_rt audio --ip <ip> --segments <N> --device "<name>" --mode spectrum --preview`
   with music playing. The terminal preview shows what's being sent; if the preview moves but
   the strip doesn't, the problem is network; if neither moves, it's audio capture.
10. Tune with the owner: band layout, hue, mode. Then move on to the roadmap.

## Bluetooth runbook (strip not on Wi-Fi)

`GoveeBle` gives every command a `--ble [name|address]` flag. From the Claude Code agent on
macOS, wrap BLE commands in `tools/ble_run.sh` (see docs/PROTOCOL.md "macOS permission");
from the owner's own terminal, run them directly after granting the terminal Bluetooth.

1. Owner closes the Govee app on the phone (one BLE connection at a time; the strip stops
   advertising while connected). GoveeBLE must be on in Privacy & Security → Bluetooth.
2. `tools/ble_run.sh .venv/bin/python -m govee_rt connect --ble`. It identifies the strip,
   measures segments by readback, saves `~/.govee-rt/devices.json`, and shows one rainbow band
   per segment. Ask: how many bands, is red at the controller end? Record the output below.
   If it fails with "encrypted BLE protocol", implement the v1/v2 handshake (see
   docs/PROTOCOL.md) before anything else. If readback didn't answer, the count came from the
   model table or the default; confirm with step 4.
3. `... -m govee_rt solid --ble --rgb 255 40 0`. Ask: did it turn orange? If not, the `0x15`
   segment command isn't honored; try `33 05 02`/`0D`/`0B` solid variants.
4. `... -m govee_rt probe --ble`. Ask: which of segments 1..16 lit a new spot, and does 1 sit
   at the controller end? If it differs from step 2: `connect --segments N`. More than 16
   needs a wider mask.
5. `... -m govee_rt chase --ble --segments <N>`. Ask about smoothness; note the `BLE:` stats
   line (frames_sent vs frames_dropped) for the real frame rate.
6. Optionally `chase --ble --ble-mode razer --segments 12`: if it animates, razer frames work
   over BLE too and cost one write per frame instead of one per color.
7. Audio as in the Wi-Fi runbook, with `--ble` in place of `--ip`.
8. `tools/ble_run.sh .venv/bin/python -m govee_rt studio --device "<name>"` and let the owner
   design looks in the browser. Device and segment count come from the saved profile.

## Music sync on this strip (decided with the owner, 2026-10-06)

The **only** audio input is the computer's own playback via BlackHole. The owner rejects the
strip's built-in microphone outright (bad sync, wrecked by background noise), so Govee's native
music effects (Rolling etc.) are off the table: they only listen to that mic. Every animation
is computed on the Mac from BlackHole and sent to the strip. Over BLE the channels are:

- Finger Sketch uploads: 60 cells, ~3 pictures/s (verified).
- `a5 02 83 r g b sum` phone-mic stream: whole-strip color, built by Govee for continuous
  real-time streaming; in the app's phone-mic mode the owner saw the strip "pulse while changing
  colors pretty much to the song". Rate, latency, and whether it needs a mode select first are
  untested (`tools/music_probe.py inject`).
- Brightness `33 04` (~116 ms ACK) as a beat pulse on top of a sketch picture (`pulse`).

Measured on hardware (2026-10-06, owner watching, music from BlackHole):

- `a5 02 83` stream works with no mode select (owner: "great"); selecting music mode first
  hands control to the strip's mic (stayed green), so never do that.
- Brightness pulses over a static picture: ~10.8/s, owner: "great". Whole-strip `33 04` and
  `33 05 0d` top out at ~11-12/s ACK-paced; 20/s unpaced collapses (3 of 60 ACKed).
- Owner plays through a **Bluetooth speaker** (Marshall Emberton II, primary in the
  Multi-Output Device). A2DP to the speaker starves the Govee BLE link: hybrid delivered ~2
  frames/s with ACK timeouts; stream delivered ~29/s. The saved profile defaults to
  `ble_mode: stream`. Owner reports stream mode in sync with the speaker, no offset needed.

Next up (paused 2026-10-07, owner had guests): a custom "Rolling" synced to bass. Plan: upload a
party-color block pattern once and let the strip animate it (Finger Sketch effect `0x09` roll or
`0x13` gradient flow), with the Mac sending only two-level brightness (`33 04`) on bass hits
(Bass Pulse logic). Test run 00:07 showed 40/40 brightness writes ACKed while the strip animated;
the owner hasn't said yet which motion (roll vs gradient, speed 60 vs 100) looked best.

Bass sync session (2026-10-07, brief in `docs/BASS_SYNC_PROMPT.md`):

- Dark state built: `bass_slow` in the analyzer (raw bass envelope over a slow peak, 2 s hold,
  ~20 s decay), `BassPulse` `dark_below` 0.25 / `dark_after` 0.3 s, `kick_boost` now on by
  default. A beat only exits dark if `bass_slow >= dark_below` (on the synthetic track, hat
  onsets fire `beat` in the gap at `bass_slow` ~0.1). CI test on `synth(kick_gap=...)`: drop is
  full in the same 21 ms block as the first kick.
- Stage 1 baseline, `Bass Pulse` preset with those defaults (threshold 0.70, hold 0.12, floor
  0.45, color_every 1), stream mode, four-on-the-floor track through the Marshall:
  - Q1 every kick flashed? "pretty good but some are missed".
  - Q2 flashes between kicks or on bass notes? "not rlly its pretty good rn".
  - Q3 early, late, on the beat? "a little late" (no offset, block 1024).
  - Q4 dark enough between hits? "not dark enough. i think there need to be 3 tiers of
    brightness: off mid and bright. right now its just alternating between bright for hits and
    mid for everything else". (Dark state exists but needs `bass_slow` < 0.25 for 0.3 s, which
    a steady four-on-the-floor track never reaches, so the owner only saw two tiers.)
  - Q5 color change part of the hit or separate? "part of the hit".
  - Baseline run stats: 15245 frames rendered, 15213 sent (~8.5 min at 30 fps).
- Change 1 (owner's pick of three tiers: "mid on soft kicks"): `BassPulse` gains `strong` and
  `soft`. A hit is full when `bass_slow` >= `strong` on the hit frame (may step up during the
  hold, never down), else `soft`. Preset now `floor` 0, `strong` 0.75, `soft` 0.45: off between
  kicks, mid on soft kicks, full on strong kicks. On the synthetic track 0.4-gain kicks read
  mid, 0.6+ read full (bass level is sqrt-compressed).
  - Owner on the strip, same four-on-the-floor track: "ok this is great".
- Dark state on a track with a real build and drop:
  - Q6 went fully dark in the build? "it was good", time after the kick stopped not observed.
  - Q7 (drop back at full, on the beat) and Q8 (dark while the kick still ran) not asked: owner
    took the strip back for the studio ("i think its good now").
- Owner's `~/.govee-rt/presets/Neon Bass.json` moved to the three tiers (floor 0, strong 0.75,
  soft 0.45, kick_boost on, dark 0.25 / 0.3 s), palette kept; original saved next to it as
  `Neon Bass.json.bak-2026-10-07`.
- Owner: dark and idle only for the party modes that react to music. Dark already lives only
  in `bass_pulse`. Idle rainbow now only when `scene_idles(scene)`: every enabled layer is a
  `reactive` effect (spectrum, meter, pulse, bass_pulse, ripple, strobe), overridable with
  `"idle": true/false` in the scene. Of the presets, Bass Pulse, Neon Bass and Spectrum idle;
  plasma/gradient/paint/fire based ones keep showing through silence.
- 2026-10-08, owner asks (song: "Desire", Ian Asher): (1) the trilled high note in the intro
  ("is it desireeee") should flicker the lights fast between mid and high; (2) slow bass with
  busy high beats gives an on/off "epilepsy" flicker; (3) held-out beats should hold the light
  as long as they ring. Built as a separate user preset `Neon Bass Desire` (Neon Bass +
  `hit_min` 0.5, `sustain` 0.5, `trill` 0.3) so the owner can compare; all three default off.
  - (2) cause, reproduced on a synth track (one kick per bar + 16th claps): claps fire `beat`
    at `bass_slow` 0.31..0.47 and `kick_boost` flashed each one from off (4.9 flips/s vs 1.2
    expected). `beat_share` (bass share of the onset's spectral rise: kicks 0.16..1.0, claps
    0.05..0.09) gates beats when `hit_min` > 0, so kicks still light on their first block (no
    added latency, checked); level hits need `bass_slow` >= `hit_min`. Result 1.1 flips/s.
  - (3) a kick with a held 808 tail went dark after ~0.2 s; `sustain` 0.5 keeps it lit for
    the note's length (1.2/0.4/0.8 s notes lit 1.22/0.47/0.83 s); plain kicks 0.23 -> 0.30 s.
  - (1) analyzer `trill`: pitch of the loudest partial 250..4000 Hz on a quarter-semitone grid,
    trill when it swings >= 0.3 st around its center 3..14 times/s on a tonal frame. ~0.5 s to
    lock on, then flips with every half cycle. Synth: 7 Hz trill detected, held note / 1.5 Hz
    alternation / kicks / claps never. Overrides the dark state, mid <-> full, never off.
  - `bass_slow` peak now floored at raw 0.15 (`SLOW_FLOOR`, a mastered kick reads ~0.4): with
    the old silence floor, a song starting after silence read pad/hat leakage as full bass.
    Applies to every preset that uses `bass_slow`.
  - Owner on the strip: "the desire intro just shows a solid color for the whole time n for
    some of the trill its just dark too"; quick beats in succession "registers as one ... holds
    a solid color for the whole succession". Owner's rule: quick beats alternate mid <-> bright
    on each hit, distinct beats go off <-> bright.
- Round 2 (2026-10-08), tuned on a BlackHole recording of Desire (scratchpad only, not kept):
  - The "trill" is not a pitch trill: the held "desireeee" is a steady B5 (~990 Hz, 66.13 to
    66.75 s from play) whose loudness pulses 8.6 times/s, ~20 dB deep, over other voices that
    take the top in every quiet half. Same motif returns at ~179 s (C6). Analyzer `trill` now
    also detects that: the note that tops most loud frames over 0.35 s, its own level vs its
    median, >= 4 regular flips (two pulses) >= 8 dB deep, then latched until the note stops.
    On the recording it fires only on those two notes (66.47..66.83, 179.43..179.75) among the
    dark sections; a fast melody at 92 s and arpeggios elsewhere do not. Lock-on needs two
    pulses (~0.23 s), so the first third of the note stays dark.
  - Effect only follows the trill in the dark state; with bass present, arps and gated synths
    also pulse and would strobe.
  - Solid color: `sustain` 0.5 held the light through the whole drop because this song's sub
    sits at ~0.6 of the kick peak between kicks (100% lit 67..90 s). Now `sustain` needs the
    level within that fraction of the hit's peak and no new onset; preset 0.75.
  - Quick beats: analyzer `onset` (90 ms refractory; `beat` keeps 250 ms), engine latches it.
    `run_gap` 0.3: kicks closer than that toggle full <-> soft and the light stays up through
    the run; any kick landing while lit toggles. Recording: busy section 68..90 s 4.6 level
    changes/s, off 5%, mid 31%, full 65%; spaced kicks 124..140 s off/full like Neon Bass.
  - Synthetic 8-kick roll at 0.15 s: 7 of 8 kicks detected; at 0.117 s (16ths at 128) 6 of 8.
  - Preset `Neon Bass Desire`: hit_min 0.5, sustain 0.75, trill 0.3, run_gap 0.3.
- Owner (2026-10-08) on the trill flicker: "it should flicker faster like a strobe and when its
  doing that it shouldnt be changing color" (asked: this is the high-note trill, not quick
  beats). New `trill_strobe` (flashes/s): during a trill in the dark, full / off at that rate
  in the current color, no mid level (dim levels shift hue on the strip). Preset 15/s, the
  ceiling at 30 rendered frames/s. Recording: 6 flashes on the "desireeee" note, 3 on the
  179 s repeat, one color each. Not yet checked on the strip.
- Owner: the strobe fired on "is it", not "desireeee", and not at all on the second one. Owner's
  timing (song clock): "is it" is the pulsing B5 at ~0:03.6-0:04.2 (recording 66.1-66.75 s,
  recording = song + 62.5 s), "desireeee" starts at 0:05 as the bass drops in, second at ~0:12.
  The trill there is real pitch alternation: A5 (~885 Hz, drifting like a voice) glides up,
  then C6 (~1050) <-> A5/G#5 (~820-890) every ~0.1 s, over the drop. Tried on the recording,
  none usable: top-partial two-note alternation (1024 and 2048 FFT, 750-1400 Hz) also fires
  on ~40 synth-arpeggio spots per 2 min (the synths play 16ths in the same range, 8.6/s =
  16ths at 128 BPM); balance / run-length / pitch-wobble / center-channel (L/R corr 0.77)
  filters either kept the arps or dropped the vocal. Per-bin 6-12 Hz AM fires everywhere.
  Separating this vocal from the arps needs source separation (ML), outside numpy-only.
- So the drop-time trill is manual: studio "Hold to strobe" button + space bar
  (`POST /api/strobe`, engine `strobe()`): full / off up to 15/s, color locked at press,
  stops by itself 0.6 s after the page stops re-sending. Auto trill strobe on "is it" kept
  (owner: "keep the auto strobe for now").
- Owner: "for the desire at 0.36 the light just off too". 0:34.5-0:38 has bass at ~half the
  drop (`bass_slow` 0.47-0.55) so `hit_min` 0.5 dropped most hits (35% lit; Neon Bass 80%).
  Preset `hit_min` 0.4: 54% lit there; clap test 5 flashes for 4 kicks (0.5: 4, 0: 10).
- Owner: "the strobe is pretty laggy". Measured on the strip: the studio renders at 40 fps
  (profile-based BLE stream does not pick STREAM_FPS 30), the 15/s strobe flipped on a 33 ms
  threshold over 25 ms frames, so flashes were 1 or 2 frames long, and only 27 of 40 frames/s
  became writes. Now the hold strobe runs on its own clock in `run()` (one frame per flip,
  beat wake-ups ignored while strobing), default 10/s with a 4..15/s slider next to the
  button. Measured: 20.5 frames/s = 20.5 writes/s, evenly spaced. Trill strobe flips offset
  by a quarter period so frame jitter can't make uneven flashes; preset trill_strobe 10/s.
- 2026-10-08 owner: "the strip stopped syncing to music ... but i can still turn it on with the
  strobe button". The running studio predated the `live_blocks()` stall recovery: PortAudio
  logged `PaMacCore (AUHAL) err='-10863'`, the audio features froze (identical in every
  /api/live sample while frames kept counting), no hits, strip black. Restarting on the current
  code fixed it (features moving, colors flashing). Two sessions edited this repo that day.
- Rolling bar per hit (owner ask): BLE can't move a bar frame by frame (stream is whole-strip,
  sketch uploads ~3/s), so the plan is a 60-cell picture with one bar at cell 1 rolled by the
  strip (effect 0x09) and a mode select per kick to restart it. `tools/music_probe.py rollbar`
  ran 2026-10-08 14:16 (speed 100, speed 60, then 6 restarts 1.5 s apart): every select ACKed
  in 107..191 ms with the speaker playing. Owner: "the first few rolling animations looked
  really good and it was in the direction i wanted too" (right to left, speeds 100 and 60).
  Restart rerun (`rollrestart`, speed 60, 6 s untouched then a select every 3 s, ACKs 99..128
  ms): owner "keeps snapping back. speed 100 pls 60 is too slow".
- Built: BLE mode `roll` (`GoveeBle._send_roll`): upload the bar picture once (cell 1 = owner's
  right end, magenta, effect 0x09 speed 100), a mode select on every hit (frame turns lit, or a
  lit frame steps >= 0.3, so Bass Pulse run toggles count), max one per 0.15 s, skip not queue;
  1 s of dark frames sends one stream black frame (stream frames take over from the sketch);
  rt_enable / idle rainbow / show_sketch force a re-upload. Scenes may carry `"ble_mode":
  "roll"`; the engine switches the light to it and back to the profile's mode for other scenes,
  and uses the profile's mode while the hold strobe runs. Built-in preset `Rolling Bar`
  (bass_pulse, hit_min 0.4, run_gap 0.3, trill off). Desire recording: 0 bars/s in the intro,
  3.0/s in the drop, 0.9/s in the 0:29-0:38 breakdown, median 0.375 s apart. Bar color is fixed
  (no per-hit color: a re-upload costs ~0.3 s). Not yet seen on the strip; also unverified:
  whether a select after the stream black brings the bar back.
- Owner on the strip: "right now its a pink line with an orange head ... theres no end to the
  bar it just extends out. go back to the previous magenta bar thing that was great", and "the
  bar resetting kills the old bar. i want the old bars to go fully from right to left and a hit
  just adds a new bar". Likely the strip's draw-in of effect 0x09 cut off by ~3 selects/s
  (unconfirmed). Reverted `roll` to upload once and leave it rolling (no per-hit select, no
  stream black). Several bars at once needs a re-upload per hit with the old bars at their
  current positions, which needs the roll speed in cells/s at speed 100 (unmeasured).
- Owner timing of one pass at speed 100: first "2-3 seconds", then "actually its more like
  1-1.5 seconds" -> `ROLL_CELLS_PER_S` 48. Owner: "make it a new preset". `roll` = one bar
  uploaded once (preset `Rolling Bar`, kept as the owner liked it); `roll_hits` = a bar per
  hit (preset `Rolling Hits`): each hit uploads a picture with the bars in flight where the
  roll has carried them when the strip starts it (upload delay measured per upload, select
  counted at half its ACK RTT) plus the new bar drawn v x delay cells in; a bar is dropped
  before its head reaches the end (the roll would wrap it). First run with music: 17 uploads
  in 5 s, 3 bars in flight, 1 ACK timeout. Owner: "they're like rlly laggy" (each upload
  restarts the strip's roll, same as the 3/s selects). Not fixable over BLE; options offered.
- Owner picked "bars + flash", "with multicolored color themes like neon". BLE mode
  `roll_flash`: 4 bars spread evenly, colors picked evenly from the scene's first palette
  (engine `_scene_palette()` -> `set_roll_colors()`, a palette change re-uploads), uploaded once
  and rolled by the strip (0x09, speed 100); the scene's level rides on brightness `33 04`
  (ACK-paced, >= 4 % steps, latest wins), `rt_disable()` restores 100. Preset `Rolling Neon`
  (bass_pulse, neon palette, floor 0.3, soft 0.6, gamma 1.0: rest 30 %, run toggles 100/60,
  dark 1 %). First run with music through the Marshall: 30 brightness writes in 6 s, all 30
  ACKed, 0 timeouts, 0 re-uploads. Owner: "ok great", then stopped for the day (studio exited
  through stop(): rt_disable restores brightness 100). Still to ask: is the 30 % rest level
  right, and do the bars stay smooth while the brightness flashes.
- Still open from the baseline: some kicks missed (knob: `threshold` toward 0.6) and flashes "a
  little late" (try `--block 512`). Stage 2 not started: owner has not yet confirmed hits on the
  beat and the drop out of black at full.

Chill mode and Rio colors (2026-10-09, owner: "a mode that still syncs to music like the party
mode but is less energetic for a more chill vibe where ppl could also get some work done", and
"a rio color theme of like green and yellow"):

- Palette `rio`: pure green `#00ff00` and gold `#ffd200` only (owner 2026-10-08: "just be a
  green and yellow gradient flow"; it first also had turquoise `#00e0b0` and blue `#0040ff`).
  The blend passes lime, never gray. In `Rolling Rio` the 4 bars alternate green / gold.
  Side-by-side test on the strip (still sketch, 5 sections): `#00c040`, `#00e0b0` and `#0000ff`
  all read blue to the owner; only `#00ff00` and `#ffd200` looked right. A green needs zero
  blue on this strip.
- Effect `chill` ("Chill flow"): palette drifts slowly (0.02 cycles/s); each beat lifts the level
  by `swell` 0.4 with a 0.04 s rise and 0.5 s fall over `floor` 0.3, plus `body` 0.15 from
  `bass_slow` smoothed over 1 s up / 3 s down. Synthetic 128 BPM track: 0.56..0.76 while the
  kick runs (vs Bass Pulse off/full), crest ~70 ms after the kick, breakdown ~0.5, silence 0.3.
  Never dark, no color steps, no strobe; not `reactive`, so no idle rainbow while people work.
- BLE mode `flow`: the palette as one smooth 60-cell cycle, uploaded once with the strip's own
  gradient effect `0x13` at speed 50 (the idle look the owner liked), the scene's level on
  brightness `33 04` in 2 % steps (ACK-paced, latest wins). Same mechanism as `roll_flash`.
- Presets: `Chill Flow` (chill, rio, `ble_mode: flow`, gamma 1.0 so level = brightness %) and
  `Rolling Rio` (Rolling Neon with the rio palette).
- Fix: switching from a scene that dims the strip (`roll_flash`, `flow`, `hybrid`) to one that
  doesn't left the brightness where it was (Rolling Neon at 30 % then Neon Bass ran at 30 %).
  `rt_enable()` keeps `_bright_sent` and the sender restores 100 before the first frame of a
  non-dimming mode.
- Not yet seen on the strip. Ask: is speed 50 calm enough for work (or slower), is the beat
  swell visible but not distracting, is 30 % rest bright enough, do the flow stats show
  timeouts with the Marshall playing (`flow_levels` vs link `timeouts`).

Full-fidelity per-segment animation at 40 fps needs the Wi-Fi razer path; test it with the
strip and Mac on an iPhone hotspot (Maximize Compatibility) before buying anything.

## Device record (fill in once known)

- SKU: H612D (BLE name `Govee_H612D_7A61`, macOS BLE address `53B339CE-C5B8-49C3-BFC5-EDBF2340CD72`)
- IP: _none yet_; strip has no Wi-Fi configured (owner drives it over BLE). Consider a DHCP reservation once it does.
- Segment count over BLE: 20 (static color command, 3-byte mask; measured by readback, owner saw the
  20-band rainbow 2026-10-06). Hardware is ~10 IC segments per meter per Govee's spec, so
  ~150 on this 15 m strip; only reachable at that resolution via LAN razer mode (unverified).
- Behavior when N ≠ segment count: _unknown_ (needs LAN razer probe)
- Firmware: BLE 1.01.10, hardware 3.05.00 (AA 06 / AA 07 03). No encryption. MTU 512.
- BLE razer (`BB` frames to 2b11): ignored. No ACK, no mode change, owner saw no change twice
  (2026-10-06, N=20 and N=150).
- BLE timing (macOS): query RTT ~86 ms; ACK after power ~86 ms, brightness ~116 ms, static
  segment color ~173 ms. Bursts without waiting for ACKs get dropped (8 of 60 acked).
- `AA 11` replies `00 1e 0f 0f 00 ff 32 ...`, `AA 12` replies `ff 64 00 00 80 0a 00 ff ae 54`:
  meaning unknown. `AA 0F` (light count) gets no reply.
- Owner OS / audio device name: macOS (Darwin 25.6, Apple Silicon); `"BlackHole 2ch"` via a
  Multi-Output Device (speakers + BlackHole), verified capturing 2026-10-06
- Finger Sketch over BLE: 60 cells (owner screenshot: 2 rows of 30), upload `A3` type 0x03 then
  select `33 05 0a 20 03`; owner saw the 60-cell pattern; ~3 updates/s. Effect `0x09` speed 0
  holds still. Cell 1 is at the owner's right, so the profile sets `reverse`.
- Color: any blue in a green takes over. `#00c040` (green with 25 % blue) and turquoise
  `#00e0b0` read blue to the owner next to pure `#0000ff`; pure `#00ff00` reads green.
- Sketch effects animated by the strip itself: `0x09` clockwise moves one cell at a time (owner:
  jerky); `0x13` gradient fades smoothly (owner's pick for idle, speed 50); `0x02` cycle less smooth.
- Idle: after 3 s of silence on BlackHole (RMS < 1e-3) the engine shows a 60-cell rainbow with
  `0x13` speed 50 and stops streaming; any audio hands control straight back.
- Speaker lag after the Marshall reconnects (2026-10-07): it comes back at 44.1 kHz (its only
  rate) while BlackHole stayed at 48 kHz, so the Multi-Output Device resampled against a
  mismatched clock and the speaker stuttered. Setting BlackHole to 44.1 kHz fixed it (owner: "its
  good now"). `live_blocks()` now does this itself via `macaudio.py` (CoreAudio over ctypes): a
  capture device inside the default output is kept at the output's rate, checked every 2 s, and
  a stream that delivers nothing for 1 s is reopened (changing the rate under an open stream
  froze the old studio's capture). An explicit `--samplerate` disables the sync.
- Bring-up notes: the Mac's Wi-Fi is WPA2-Enterprise 10.10.0.0/18, which the strip can't join.
  The Claude app needs macOS Local Network permission, or every LAN send fails with errno 65.

## Working rules

- Hardware observations come from the owner. When a step needs eyes on the strip, stop and
  ask a specific question ("how many bands did you see at N=30?"), don't guess and continue.
- Never stream real-time frames to the broadcast or multicast address; unicast to the strip.
- Keep datagrams ≥ 15 ms apart (the fps cap already does this). Don't raise `--fps` above 60.
- `frame_colors()` must never emit N > 255 or a bad checksum. The fake device counts
  `razer_bad`; it must stay 0 in tests.
- Always send `rt_disable()` on exit paths so the strip returns to normal mode.
- DSP stays numpy-only unless a feature truly needs more. No librosa/torch for a 15-band FFT.
- Package layout: `protocol.py` (LAN), `ble.py`, `audio.py` (analysis + sources), `effects.py`
  (effects, palettes, scenes, renderer), `engine.py` (real-time loop), `studio.py` + `web/`
  (browser UI, stdlib HTTP, 127.0.0.1 only), `visuals.py` (legacy `audio` modes), `cli.py`,
  `quickstart.py` (first-run wizard, loopback detection, `doctor`), `macaudio.py` (CoreAudio:
  rate sync, Multi-Output creation). Run as `govee-rt` or `python -m govee_rt`.
- Distribution (2026-10-07): `install.sh` / `install.ps1` install uv, then `uv tool install`
  from the git repo (switch `SOURCE` to `govee-rt` once on PyPI); `release.yml` publishes on a
  `v*` tag via PyPI trusted publishing (pending publisher not set up yet). Version lives in
  `govee_rt/__init__.py`. Bare `govee-rt` = `start`: saved light, else LAN scan, else BLE auto;
  loopback auto-pick; offers BlackHole install and the "Govee RT Output" Multi-Output Device
  (`macaudio.route_to_capture`, only on the user's yes); studio reopens the last look
  (`~/.govee-rt/last_scene.json`). Python 3.9 is supported and in CI: watch for 3.10+ syntax
  and asyncio differences (wait_for swallowing cancel before 3.12).
- Never call `route_to_capture` / `set_default_output` from tests or agent sessions: it changes
  the owner's system output. Test CoreAudio code with `create_multi_output(private=True)`. Built-in presets live in `govee_rt/presets/`, user presets in
  `~/.govee-rt/presets/`.
- New effects: subclass `Effect` in `effects.py` with `@register`; declare params with the
  `F/COLOR/PAL/BOOL/CHOICE/SEGMENTS` helpers so the studio builds the editor automatically.
  `tests/test_effects.py` renders every registered effect, so a new one is tested for free.
- The UI is one static `web/index.html` with vanilla JS and no build step. Keep it that way.
  Look (2026-10-07 revamp): monochrome glass chrome, the only color on screen comes from the
  lights (strip preview + room glow from `--spill`). System font, no web fonts, white for
  selected/on states, green only for switches. Motion uses the `--spring` easing (critically
  damped, 0.4 s); press feedback is instant on pointer-down.
- Add a test for anything protocol-level or DSP-level. Use the fake device and
  `tests/make_test_wav.synth()`; nothing in CI may need hardware or an audio device.
- No em dashes in docs or comments.

## If razer mode fails on this SKU

1. Owner installs Govee Desktop and enables its music mode for the strip; if that works,
   the strip does support real-time control and only our frames differ.
2. Owner runs Wireshark with `udp.port == 4003` while Govee Desktop drives the strip; export
   a few packets. Decode `data.pt` (base64) and diff against `razer_frame()` output.
3. Only if the desktop app also can't do it: BLE via `bleak`, see `docs/PROTOCOL.md`.

## Roadmap

- [ ] Hardware bring-up per the runbook; fill in the device record.
- [x] Remembered device profile (`connect`, `~/.govee-rt/devices.json`): transport, address/IP,
      SKU, firmware, measured segment count. Commands need no flags after the first connect.
- [x] Remember the last look (`last_scene.json`); audio device is auto-detected instead.
- [ ] Windows loopback via `pyaudiowpatch` behind the same `live_blocks()` interface.
- [ ] Latency measurement: play a click track, film strip + speaker at 120/240 fps, or use
      a photodiode on the strip into the audio input; target < 60 ms audio-to-light.
- [ ] Tempo tracking (autocorrelation of the onset envelope over ~6 s) and beat phase
      prediction, so effects can anticipate the beat instead of reacting ~25 ms late.
- [ ] Smarter spectrum: perceptual (Bark/mel) band edges; per-band compression that keeps
      quiet highs visible; configurable palettes (two-color gradients, album-art palette).
- [x] Effects engine + studio UI: layers, palettes, per-segment paint, audio modulation, presets.
- [ ] More effects: strobe-on-snare (needs a snare detector), left/right stereo split across the
      strip halves (needs stereo analysis).
- [ ] Spotify metadata (not audio; the Audio Analysis endpoint is deprecated for new apps):
      current track via the Web API to pick palettes from album art and reset state on
      track change.
- [ ] Multiple strips: one audio analysis, N `GoveeLan` targets, optional per-device offset.
- [ ] Idle behavior: after 10 s of silence fall back to a slow ambient animation; disable
      real-time mode after a configurable timeout so the app's scenes come back.
- [x] `govee-rt` console script, one-line installers, CI, PyPI release workflow.
- [ ] Publish: make the repo public (scrub personal details from CLAUDE.md/docs first), set up
      the PyPI pending publisher, tag v0.2.0.
- [ ] Double-clickable macOS app (menu bar), so non-terminal users can run it.
- [x] BLE transport via `bleak` with the same `rt_frame()` interface (`--ble`); needs hardware
      confirmation per the Bluetooth runbook.
- [ ] Govee encrypted BLE (v1 AES-ECB+RC4, v2 AES-GCM) if this strip's 2b12 marker says so.
