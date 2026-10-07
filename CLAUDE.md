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
- Effects engine (15 effects, layers, blend modes, per-param audio modulation), 10 built-in
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
- Still open from the baseline: some kicks missed (knob: `threshold` toward 0.6) and flashes "a
  little late" (try `--block 512`). Stage 2 not started: owner has not yet confirmed hits on the
  beat and the drop out of black at full.

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
- Sketch effects animated by the strip itself: `0x09` clockwise moves one cell at a time (owner:
  jerky); `0x13` gradient fades smoothly (owner's pick for idle, speed 50); `0x02` cycle less smooth.
- Idle: after 3 s of silence on BlackHole (RMS < 1e-3) the engine shows a 60-cell rainbow with
  `0x13` speed 50 and stops streaming; any audio hands control straight back.
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
  (browser UI, stdlib HTTP, 127.0.0.1 only), `visuals.py` (legacy `audio` modes), `cli.py`.
  Run as `python -m govee_rt`. Built-in presets live in `govee_rt/presets/`, user presets in
  `~/.govee-rt/presets/`.
- New effects: subclass `Effect` in `effects.py` with `@register`; declare params with the
  `F/COLOR/PAL/BOOL/CHOICE/SEGMENTS` helpers so the studio builds the editor automatically.
  `tests/test_effects.py` renders every registered effect, so a new one is tested for free.
- The UI is one static `web/index.html` with vanilla JS and no build step. Keep it that way.
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
- [ ] Remember the audio device and last preset too.
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
- [ ] Package as a small menu-bar/tray app or a `govee-rt` console script (pyproject exists).
- [x] BLE transport via `bleak` with the same `rt_frame()` interface (`--ble`); needs hardware
      confirmation per the Bluetooth runbook.
- [ ] Govee encrypted BLE (v1 AES-ECB+RC4, v2 AES-GCM) if this strip's 2b12 marker says so.
