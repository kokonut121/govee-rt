# govee-rt

Make a Govee RGBIC light strip dance to whatever your computer is playing: Spotify, YouTube,
games, anything. Beats, bass and vocals drive the lights in real time, and a browser studio
lets you design your own looks. No cloud, no account, no phone mic.

Works over **Bluetooth** (no setup on the strip) or **Wi-Fi** (LAN Control). Built and tested on
a Govee Strip Light S (H612 family); other Govee RGBIC strips that speak the same protocol
(H617x, H618x, H619x, H61A0 rope lights) should work too.

## Install

**macOS / Linux**, in Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/kokonut121/govee-rt/main/install.sh | sh
```

**Windows**, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/kokonut121/govee-rt/main/install.ps1 | iex"
```

The installer sets up everything it needs (its own Python included, via
[uv](https://docs.astral.sh/uv/)) without touching your system Python. On macOS it also
installs [BlackHole](https://existential.audio/blackhole/), the free driver that lets apps hear
what your Mac plays. Already have Python? `pipx install git+https://github.com/kokonut121/govee-rt`
works too.

## Use

1. Close the Govee app on your phone (the strip only talks to one device at a time).
2. Run:

   ```bash
   govee-rt
   ```

3. Play some music.

The first run finds your strip (Wi-Fi first, then Bluetooth), measures how many segments it
has, offers to route your computer's sound to the lights, and opens the studio in your browser.
After that, `govee-rt` goes straight to the studio with the last look you used.

On macOS, the first Bluetooth use pops up a permission prompt for your terminal app: click
Allow. If you missed it: System Settings, Privacy & Security, Bluetooth, turn your terminal on.

### What the audio setup does (macOS)

To hear your music and send it to the lights at the same time, macOS needs a *Multi-Output
Device*: your speakers plus BlackHole. `govee-rt` creates one called "Govee RT Output" and
selects it when you say yes. Two things to know:

- The volume keys don't work on a Multi-Output Device; set the volume in the app or on the
  speaker.
- To undo, pick your speakers again in the menu bar sound menu.

`govee-rt audio-setup` runs this step again (for example after you switch speakers).

### Troubleshooting

```bash
govee-rt doctor
```

prints what is set up and what is missing. Common fixes:

| Problem | Fix |
|---|---|
| "no Govee light found over Bluetooth" | Close the Govee app on your phone (force quit), make sure the strip is on and within a few meters, run again. |
| Bluetooth "not authorized" | Allow your terminal app in System Settings, Privacy & Security, Bluetooth. |
| Lights run but ignore the music | The sound isn't reaching BlackHole: run `govee-rt audio-setup`, and check the menu bar sound output is "Govee RT Output". |
| Wrong number of segments | `govee-rt probe` lights them one at a time; then `govee-rt connect --segments N`. |
| Colors start from the wrong end | `govee-rt connect --reverse` |
| Linux: no audio input found | Install `libportaudio2`; use your speakers' "Monitor of ..." source (`govee-rt devices`). |
| Windows: no audio input found | Install [VB-Cable](https://vb-audio.com/Cable/), see the Windows note in the installer output. |

Set `GOVEE_RT_DEBUG=1` to see full error tracebacks.

## Update or remove

```bash
uv tool upgrade govee-rt      # update (or re-run the installer)
uv tool uninstall govee-rt    # remove; your saved looks live in ~/.govee-rt
```

## Studio: design your own looks

`govee-rt` opens it for you. To open it directly: `govee-rt studio` (add `--no-send` to design
without a light, `--device NAME` to pick an audio input, `--demo` for a synthetic beat).

It is a control panel in your browser with a live preview of the strip. A look is a stack of
**layers**; each layer runs one effect on a range of segments and blends onto the layers below.

- **Effects**: solid, paint (set every segment by hand), gradient scroll, rainbow, spectrum bars,
  level meter, beat pulse, comets, sparkle, fire, sine wave, ripples, strobe, breathe, plasma.
- **Per segment**: each layer has a segment range (click the mini strip), reverse, and mirror
  from center, so the halves or thirds of the strip can do different things.
- **Music sync on any knob**: press ♪ next to any slider (including a layer's opacity and the
  master brightness) and pick what it follows: bass, mid, high, energy, beat, or an LFO.
  Example: comet speed follows energy, sparkle opacity follows high, master follows beat.
- **Blend modes**: normal, add, screen, max, multiply.
- **Presets**: 14 built-ins to start from (Bass Pulse, Rolling Neon, Spectrum, ...); save your own (stored in `~/.govee-rt/presets/`).

With no loopback input found (or with `--demo`) it runs a synthetic 124 BPM beat so you can
design without music playing. The look on screen when you quit comes back next time.
`govee-rt play --preset "Comet Rush"` runs a saved look without the UI.

The strip is controlled per **segment**, not per LED: that's the finest unit Govee's firmware
exposes to any controller. The segment count is found with `probe`.

## How it works

Audio blocks of 1024 samples (21 ms at 48 kHz) → Hann window → FFT → 15 log-spaced bands
(40 Hz to 14 kHz) → per-band automatic gain control (slowly falling ceiling) → attack/release
envelope (12 ms / 180 ms) → a visual mode renders N RGB tuples → one UDP datagram per frame.

Beats come from spectral flux on the 40–150 Hz band against an adaptive threshold
(mean + 1.5σ over the last 1.5 s) with a 250 ms refractory period. On a synthetic 128 BPM
track that detects 24 of 26 kicks within ±50 ms with no false positives.

Protocol details, including the frame bytes and the Bluetooth fallback, are in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Command reference

Most people only need `govee-rt`. Everything else:

| command | what it does |
|---|---|
| `govee-rt` | find the light, set up audio, open the studio |
| `govee-rt studio` | open the studio directly (`--no-send` to design without a light) |
| `govee-rt play --preset "Bass Pulse"` | run a look without the browser |
| `govee-rt doctor` | check the setup |
| `govee-rt audio-setup` | route the computer's sound to the lights |
| `govee-rt connect [--ble \| --ip IP]` | re-detect the light; `--segments N`, `--reverse` to correct it |
| `govee-rt info` | show the saved light |
| `govee-rt scan` / `ble-scan` | list lights on Wi-Fi / Bluetooth |
| `govee-rt solid --rgb 255 40 0` | one color, a quick connection check |
| `govee-rt probe` | light segments one at a time to count them |
| `govee-rt chase` | a dot running end to end, a smoothness check |
| `govee-rt devices` | list audio inputs |

Every command accepts `-h`. `python -m govee_rt` works the same as `govee-rt`.

### Wi-Fi (LAN) mode

In the Govee app: strip, settings (gear), turn on **LAN Control**. The computer and the strip
must be on the same network. `govee-rt` then finds it on its own; `govee-rt scan` lists it.
Wi-Fi uses Govee's real-time mode (the one Govee Desktop and Razer Chroma use), which is
faster and finer-grained than Bluetooth.

### Bluetooth mode

Each frame costs one small write per color, and frames the radio can't keep up with are dropped
rather than queued, so the lights never lag the music. On strips with Finger Sketch support
the studio uses the strip's own 60-cell pictures and animations; music is synced with fast
whole-strip color and brightness commands.


### Legacy `audio` command modes

| mode | what it does |
|------|--------------|
| `spectrum` | each segment is a frequency band, warm at the bass end, cool at the treble end; the bass end flashes on every kick |
| `pulse` | whole strip one color, hue steps on every beat, brightness follows bass |
| `vu` | bass level fills outward from the center, desaturates on beats |
| `wave` | a color wave that scrolls faster the louder the bass gets |

`--hue` sets the base hue (0 red, 0.33 green, 0.66 blue), `--hue-span` the range across the
strip in spectrum mode, `--fps` the send rate (default 40).

### Getting audio in by hand

govee-rt reads an audio input, so capturing what's playing needs a loopback input. It picks
one automatically (BlackHole, Loopback, Monitor of ..., CABLE Output, Stereo Mix); `--device`
overrides it.

- **macOS**: `govee-rt audio-setup` does this. By hand: `brew install blackhole-2ch`, then in
  Audio MIDI Setup create a Multi-Output Device with your speakers and BlackHole 2ch and set it
  as the system output.
- **Linux**: pick the `Monitor of …` source that `govee-rt devices` lists.
- **Windows**: `sounddevice` can't open WASAPI loopback; use VB-Cable, or see the roadmap
  item for `pyaudiowpatch` in CLAUDE.md.

Spotify's Audio Analysis API was deprecated for new apps in late 2024, so there is no
"beat grid from Spotify" shortcut; loopback capture works for every source anyway.

## Development

```bash
git clone https://github.com/kokonut121/govee-rt && cd govee-rt
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q            # wire format, fake devices, DSP accuracy; no hardware needed
python tests/fake_govee.py     # a fake strip on 127.0.0.1 that decodes every frame
python tests/make_test_wav.py  # 12 s synthetic 128 BPM track with a known kick grid
```

Releases: bump `__version__` in `govee_rt/__init__.py`, then `git tag vX.Y.Z && git push --tags`;
the release workflow publishes to PyPI.

## License

MIT
