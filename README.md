# govee-rt

Real-time, per-segment control of Govee RGBIC strips over Wi‑Fi or Bluetooth, a layered
effects engine that syncs to whatever your computer is playing, and a browser studio to
design looks live.

Built for a Govee Strip Light S (H612 family, 49.2 ft). It uses the undocumented real-time
mode that Govee Desktop and Razer Chroma use (plain UDP, no cloud, no fades), which is what
makes beat-accurate animations possible. The same frame format works on other Govee LAN
devices that OpenRGB supports (H618x/H619x strips, H61A0 neon ropes, H6061/H6066 panels).

## Quick start

```bash
pip install -r requirements.txt

# 1. In the Govee app: strip → settings → LAN Control: on
python -m govee_rt scan                               # prints IP and SKU
python -m govee_rt solid --ip 192.168.1.50 --rgb 255 40 0   # sanity check
python -m govee_rt probe --ip 192.168.1.50            # find the segment count by eye
python -m govee_rt chase --ip 192.168.1.50 --segments 15

# 2. Route system audio to a loopback device (see below), then:
python -m govee_rt devices
python -m govee_rt audio --ip 192.168.1.50 --segments 15 --device "BlackHole 2ch" --mode spectrum --preview
```

### Over Bluetooth instead (strip not on Wi‑Fi)

Close the Govee app on your phone first; the strip takes one Bluetooth connection at a time.

```bash
python -m govee_rt connect --ble     # find the strip, read its model/firmware, measure its segments
python -m govee_rt studio            # from now on no flags needed: it uses the saved strip
python -m govee_rt info              # what was detected
```

`connect` reads the model from the strip's Bluetooth name, asks for firmware and hardware
versions, and measures the segment count: it paints each addressable segment a unique test
color and reads the colors back, so slots past the real end show up as missing. It then shows
one rainbow band per segment so you can check the count by eye. The result is saved to
`~/.govee-rt/devices.json` and every command adapts to it. If the count is ever wrong,
`probe --ble` lights segments one at a time and `connect --segments N` fixes it for good.
Running any command with `--ble` against a strip that isn't saved yet does the same detection
automatically.

On macOS your terminal app needs Bluetooth permission (Privacy & Security → Bluetooth).
BLE is slower than Wi‑Fi: each frame costs one 20-byte write per distinct color, and frames
the radio can't keep up with are dropped rather than queued, so lights never lag the music.

`--preview` draws the strip as ASCII in the terminal so you can see what's being sent.
`--wav song.wav` drives it from a file instead of live audio, handy before loopback is set up.

## Studio: design your own looks

```bash
python -m govee_rt studio --ble --device "BlackHole 2ch"     # or --ip IP, or --no-send to just preview
```

Opens a control panel in your browser with a live preview of the strip. A look is a stack of
**layers**; each layer runs one effect on a range of segments and blends onto the layers below.

- **Effects**: solid, paint (set every segment by hand), gradient scroll, rainbow, spectrum bars,
  level meter, beat pulse, comets, sparkle, fire, sine wave, ripples, strobe, breathe, plasma.
- **Per segment**: each layer has a segment range (click the mini strip), reverse, and mirror
  from center, so the halves or thirds of the strip can do different things.
- **Music sync on any knob**: press ♪ next to any slider (including a layer's opacity and the
  master brightness) and pick what it follows: bass, mid, high, energy, beat, or an LFO.
  Example: comet speed follows energy, sparkle opacity follows high, master follows beat.
- **Blend modes**: normal, add, screen, max, multiply.
- **Presets**: ten built-ins to start from; save your own (stored in `~/.govee-rt/presets/`).

With no `--device` it runs a synthetic 124 BPM beat so you can design without music playing.
`python -m govee_rt play --preset "Comet Rush" --ble --device "BlackHole 2ch"` runs a saved look
without the UI.

The strip is controlled per **segment**, not per LED: that's the finest unit Govee's firmware
exposes to any controller. The segment count is found with `probe`.

## Modes (the original `audio` command)

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
- **Linux**: pick the `Monitor of …` source that `python -m govee_rt devices` lists.
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
