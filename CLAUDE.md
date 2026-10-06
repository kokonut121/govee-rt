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
4. `python govee_rt.py scan`. If empty, ask the owner for the strip's IP from the router and
   use `--target <ip>`. Record the SKU from the reply in the "Device record" section below.
5. `python govee_rt.py solid --ip <ip> --rgb 255 40 0`. Ask the owner: did it turn orange?
   This proves LAN Control and the IP; it does not touch real-time mode.
6. `python govee_rt.py probe --ip <ip>`. Ask the owner to describe each step: how many
   distinct color bands they saw for each N, and at which N the strip stopped changing.
   Record the segment count below. If nothing changes at any N, real-time mode is not
   honored on this firmware; go to the "If razer mode fails" section.
7. `python govee_rt.py chase --ip <ip> --segments <N>`. Owner confirms a single dot runs
   end to end smoothly. Note any stutter (frame drops) or lag.
8. Audio capture. Ask which OS. Then:
   - macOS: `brew install blackhole-2ch`, Audio MIDI Setup → create Multi-Output Device with
     the speakers/headphones and BlackHole 2ch, select it as system output. Device name for
     the script is `"BlackHole 2ch"`.
   - Linux (PipeWire/Pulse): use the `Monitor of <sink>` input; `python govee_rt.py devices`
     lists it.
   - Windows: PortAudio in `sounddevice` has no WASAPI loopback. Either install VB-Cable and
     route output through it, or swap `live_blocks()` to `pyaudiowpatch` (it exposes loopback
     devices directly). This is a roadmap item; don't try to make plain sounddevice do it.
9. `python govee_rt.py audio --ip <ip> --segments <N> --device "<name>" --mode spectrum --preview`
   with music playing. The terminal preview shows what's being sent; if the preview moves but
   the strip doesn't, the problem is network; if neither moves, it's audio capture.
10. Tune with the owner: band layout, hue, mode. Then move on to the roadmap.

## Device record (fill in once known)

- SKU: _unknown_
- IP: _unknown_ (consider a DHCP reservation)
- Segment count: _unknown_
- Behavior when N ≠ segment count: _unknown_
- Firmware (wifiVersionSoft / bleVersionSoft): _unknown_
- Owner OS / audio device name: _unknown_

## Working rules

- Hardware observations come from the owner. When a step needs eyes on the strip, stop and
  ask a specific question ("how many bands did you see at N=30?"), don't guess and continue.
- Never stream real-time frames to the broadcast or multicast address; unicast to the strip.
- Keep datagrams ≥ 15 ms apart (the fps cap already does this). Don't raise `--fps` above 60.
- `frame_colors()` must never emit N > 255 or a bad checksum. The fake device counts
  `razer_bad`; it must stay 0 in tests.
- Always send `rt_disable()` on exit paths so the strip returns to normal mode.
- DSP stays numpy-only unless a feature truly needs more. No librosa/torch for a 15-band FFT.
- `govee_rt.py` is one file on purpose for now. Split into a package (`govee_rt/protocol.py`,
  `audio.py`, `visuals.py`, `cli.py`) when a second real feature lands, not before.
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
- [ ] `config.json` (ip, sku, segments, audio device, defaults) so commands don't need flags.
- [ ] Windows loopback via `pyaudiowpatch` behind the same `live_blocks()` interface.
- [ ] Latency measurement: play a click track, film strip + speaker at 120/240 fps, or use
      a photodiode on the strip into the audio input; target < 60 ms audio-to-light.
- [ ] Tempo tracking (autocorrelation of the onset envelope over ~6 s) and beat phase
      prediction, so effects can anticipate the beat instead of reacting ~25 ms late.
- [ ] Smarter spectrum: perceptual (Bark/mel) band edges; per-band compression that keeps
      quiet highs visible; configurable palettes (two-color gradients, album-art palette).
- [ ] More modes: strobe-on-snare, bass-driven "breathing" with hue drift, left/right stereo
      split across the strip halves, "energy meter" with peak-hold.
- [ ] Spotify metadata (not audio; the Audio Analysis endpoint is deprecated for new apps):
      current track via the Web API to pick palettes from album art and reset state on
      track change.
- [ ] Multiple strips: one audio analysis, N `GoveeLan` targets, optional per-device offset.
- [ ] Idle behavior: after 10 s of silence fall back to a slow ambient animation; disable
      real-time mode after a configurable timeout so the app's scenes come back.
- [ ] Package as a small menu-bar/tray app or a `govee-rt` console script (pyproject exists).
- [ ] Optional BLE transport via `bleak` with the same `rt_frame()` interface.
