# govee-rt

Real-time, per-segment control of Govee Wi‑Fi RGBIC strips over the LAN, and an
audio-reactive streamer that lights the strip from whatever your computer is playing.

Built for a Govee Strip Light S (H612 family, 49.2 ft). It uses the undocumented real-time
mode that Govee Desktop and Razer Chroma use (plain UDP, no cloud, no fades), which is what
makes beat-accurate animations possible. The same frame format works on other Govee LAN
devices that OpenRGB supports (H618x/H619x strips, H61A0 neon ropes, H6061/H6066 panels).

## Quick start

```bash
pip install -r requirements.txt

# 1. In the Govee app: strip → settings → LAN Control: on
python govee_rt.py scan                               # prints IP and SKU
python govee_rt.py solid --ip 192.168.1.50 --rgb 255 40 0   # sanity check
python govee_rt.py probe --ip 192.168.1.50            # find the segment count by eye
python govee_rt.py chase --ip 192.168.1.50 --segments 15

# 2. Route system audio to a loopback device (see below), then:
python govee_rt.py devices
python govee_rt.py audio --ip 192.168.1.50 --segments 15 --device "BlackHole 2ch" --mode spectrum --preview
```

`--preview` draws the strip as ASCII in the terminal so you can see what's being sent.
`--wav song.wav` drives it from a file instead of live audio, handy before loopback is set up.

## Modes

| mode | what it does |
|------|--------------|
| `spectrum` | each segment is a frequency band, warm at the bass end, cool at the treble end; the bass end flashes on every kick |
| `pulse` | whole strip one color, hue steps on every beat, brightness follows bass |
| `vu` | bass level fills outward from the center, desaturates on beats |
| `wave` | a color wave that scrolls faster the louder the bass gets |

`--hue` sets the base hue (0 red, 0.33 green, 0.66 blue), `--hue-span` the range across the
strip in spectrum mode, `--fps` the send rate (default 40).

## Getting audio into it

The script reads an input device. To capture what's playing, that input has to be a loopback:

- **macOS**: `brew install blackhole-2ch`; in Audio MIDI Setup create a Multi-Output Device
  containing your speakers and BlackHole 2ch, set it as the system output, then
  `--device "BlackHole 2ch"`.
- **Linux**: pick the `Monitor of …` source that `python govee_rt.py devices` lists.
- **Windows**: `sounddevice` can't open WASAPI loopback; use VB-Cable, or see the roadmap
  item for `pyaudiowpatch` in CLAUDE.md.

Spotify's Audio Analysis API was deprecated for new apps in late 2024, so there is no
"beat grid from Spotify" shortcut; loopback capture works for every source anyway.

## How it works

Audio blocks of 1024 samples (21 ms at 48 kHz) → Hann window → FFT → 15 log-spaced bands
(40 Hz to 14 kHz) → per-band automatic gain control (slowly falling ceiling) → attack/release
envelope (12 ms / 180 ms) → a visual mode renders N RGB tuples → one UDP datagram per frame.

Beats come from spectral flux on the 40–150 Hz band against an adaptive threshold
(mean + 1.5σ over the last 1.5 s) with a 250 ms refractory period. On a synthetic 128 BPM
track that detects 24 of 26 kicks within ±50 ms with no false positives.

Protocol details, including the frame bytes and the Bluetooth fallback, are in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Development

```bash
pytest -q                      # wire format, fake device round-trip, DSP accuracy
python tests/fake_govee.py     # a fake strip on 127.0.0.1 that decodes every frame
python tests/make_test_wav.py  # 12 s synthetic 128 BPM track with a known kick grid
```

Nothing in the test suite needs the hardware or an audio device.
