# Govee LAN protocol notes

Everything here was pulled from open-source projects that already reverse-engineered Govee's
firmware (OpenRGB, govee2mqtt, Home Assistant `govee_light_local`, DMXCore). Nothing in this
repo has been confirmed on the 49.2 ft Strip Light S yet; see CLAUDE.md for what's verified.

## Hardware

Govee Strip Light S, "Segmented Color Control", 49.2 ft, Wi‑Fi + Bluetooth + Matter.
Family SKU H612E; retail variants H612A/B/C/D/F differ by length. The CSA Matter listing for the
family says Wi‑Fi + Bluetooth transport. H612A/B/C/D/F are all on Home Assistant's LAN-API
supported list. OpenRGB has H612F (the 3 m one) working in real-time mode with 12 segments.
The exact SKU is printed on the controller box sticker and is returned by the LAN `scan` reply.

## Prerequisite

Govee app → the strip → settings (gear) → **LAN Control** toggle on. Without this the strip
never joins the multicast group and ignores port 4003.

## Transport

UDP, same subnet as the strip. Three ports:

| port | direction | purpose |
|------|-----------|---------|
| 4001 | → strip   | discovery; strip listens here and on multicast `239.255.255.250` |
| 4002 | strip →   | replies to scan / devStatus go to the sender's IP on this port |
| 4003 | → strip   | all commands |

Every message is a JSON object `{"msg":{"cmd":..., "data":{...}}}`. OpenRGB appends a NUL byte
after the JSON; the strip tolerates it and some firmwares seem to want it, so we send it too.

### Discovery

Send to `239.255.255.250:4001` (and/or broadcast, and/or unicast to a known IP):

```json
{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}
```

Reply arrives on your `:4002`:

```json
{"msg":{"cmd":"scan","data":{"ip":"192.168.1.50","device":"AA:BB:...","sku":"H612C",
 "bleVersionHard":"3.01.01","bleVersionSoft":"1.00.11","wifiVersionHard":"1.00.10","wifiVersionSoft":"1.02.20"}}}
```

Multicast across Wi‑Fi is unreliable on many routers. If scan finds nothing, unicast the same
packet to the strip's IP (DHCP table on the router shows it as a Govee/Espressif device).

### Official commands (documented by Govee)

```json
{"msg":{"cmd":"turn","data":{"value":1}}}
{"msg":{"cmd":"brightness","data":{"value":100}}}
{"msg":{"cmd":"colorwc","data":{"color":{"r":255,"g":0,"b":0},"colorTemInKelvin":0}}}
{"msg":{"cmd":"devStatus","data":{}}}
```

Whole-strip only, and the firmware applies a fade, so these are useless for audio sync
beyond setting brightness once.

## Real-time ("razer") mode

This is what Govee Desktop and Razer Chroma Connect use. Same port 4003, `cmd` is `razer`,
and `data.pt` is a base64-encoded binary frame:

```
BB  len_hi len_lo  subcmd  <data ...>  xor
```

* `len` = number of data bytes after `subcmd` (big-endian 16-bit)
* `xor` = XOR of every preceding byte, including `BB`

Subcommands:

| subcmd | data | meaning |
|--------|------|---------|
| `B1`   | `01` | enter real-time mode. Full frame: `BB 00 01 B1 01 0A` |
| `B1`   | `00` | leave real-time mode. Full frame: `BB 00 01 B1 00 0B` |
| `B0`   | `01 N (r g b)×N` | set N segment colors. `01` = gradient off (hard edges). |

Wrapped:

```json
{"msg":{"cmd":"razer","data":{"pt":"uwABsQEK"}}}      // enable
```

Behavior notes gathered from the projects above:

* The strip drops back to its own mode after roughly 60 s without an enable, so re-send
  `B1 01` periodically (we do it every 20 s from inside `rt_frame`).
* `B0` frames are applied immediately, no fade.
* ~40 fps is fine; one implementation held 60 Hz on an H618A. Back-to-back datagrams closer
  than ~15 ms can get silently dropped, so pace sends.
* N is capped at 255 by the one-byte count. What N the strip actually honors (its segment
  count) is model-specific and unknown for this unit; `python -m govee_rt probe` finds it by eye.
  What happens for N ≠ segment count (ignored? resampled?) is also unknown; the probe
  will show it.
* Brightness is still governed by the official `brightness` command; real-time frames are
  scaled by it. Set it to 100 once and do dimming in the frame data.

## Bluetooth (BLE)

Implemented as `GoveeBle` (`--ble` on every command). Used when the strip isn't on Wi‑Fi,
or as a fallback if razer mode fails. Slower than UDP: every packet is a write-without-response
paced by the BLE connection interval.

Known from govee_btled, govee-led-wez, generalzero's notes and ha-govee-led-ble (MIT; H617A,
15 segments, hardware-verified there):

* GATT service `00010203-0405-0607-0809-0a0b0c0d1910`
* write characteristic `...2b11`, notify `...2b10`, encryption marker `...2b12` (newer models)
* 20-byte packets. Byte 0 = `0x33` for commands, `0xAA` for queries. Last byte = XOR of the
  first 19. The app sends `AA 01` (power query) every ~2 s as a keep-alive; we do too when idle.
* `33 01 01|00` power, `33 04 <0..100>` brightness percent.
* Per-segment static color (H617A family), one color per write onto a 16-bit segment mask:

  ```
  33 05 15 01  r g b  <kelvin u16 BE, 0 for RGB>  r g b  <mask u16 LE>  00…  xor
  ```

  bit 0 = segment 1; H617A's whole-strip mask is `0x7FFF`. `33 05 15 02 <pct> <mask>` is
  per-segment brightness. A frame therefore costs one write per distinct color.
  `ble_segment_packets()` groups equal colors into one mask, skips segments that didn't change,
  and re-sends one segment per frame round robin so a dropped write heals.
* Older, non-segmented strips use `33 05 02 r g b` (or `0D`/`0B` variants) for a solid color.
* `--ble-mode razer` writes the LAN `BB …` frames straight to `2b11`. The H612D ignores them
  (see below); kept for models that might not.

Verified on the H612D (20 segments, 2026-10-06):

* `0x15` static segment color works; the mask continues past 16 bits into byte 14 (bits 16-19
  reached segments 17-20). The code treats bytes 12..18 as one little-endian mask (56 bits).
* Every `33` command is ACKed with `33 <cmd> 00 …` on the notify characteristic. Static
  segment writes take ~173 ms to ACK (link RTT ~86 ms on macOS), and writes sent while the
  strip is busy are dropped, so `GoveeBle` waits for each ACK. That caps BLE animation at a
  handful of writes per second: fine for still looks, too slow for beat-synced effects.
* Segment readback `AA A5 <page>` returns four records (`brightness r g b`) per page on this
  model (three plus a zero slot on the H617A) and an all-zero page past the end. Painting all
  56 mask bits one test color and counting the records that hold it gives the segment count.
* `AA 06` firmware text, `AA 07 03` hardware text; `AA 05 01` reports the color mode byte
  (`15` segments, `02`/`0d` solid).
* Razer `BB` frames over BLE are ignored (no ACK, no mode change, nothing visible).

### Music modes over BLE (research, 2026-10-06; not yet tried on the H612D)

Govee lists the Strip Light S as "Music Sync from phone and device microphone". Two paths:

* **Device mic**: the strip listens with its own microphone and renders its built-in music
  effects. Selected with `33 05 13 <mode> <sensitivity 0..99> …` (H6199 uses `33 05 0c`;
  the old H6127 used `33 05 01 <mode>`). Mode ids from ha-govee-led-ble's APK analysis:
  `03` rhythm, `04` spectrum, `05` energetic, `06` rolling, `30` bloom, `31` shiny,
  `32` separation, `33` hopping, `34` piano keys, `35` fountain, `37` day and night.
  Legacy modes (rhythm, spectrum, energetic, rolling) carry `style, has_fixed_colour, r g b`
  after the sensitivity; newer ones take palettes and tails through an A3 upload
  (`01 <lines> 41 <mode> <n_palette> rgb… <tail>`, see `music_body.ksy` there).
* **Phone mic**: the app analyzes the phone's microphone itself and streams 7-byte frames
  `a5 02 83 r g b sum`, where the last byte is the low 8 bits of the sum of the first six
  (H617A and H6199 capture: `a5 02 83 56 00 00 80`). What the three payload bytes mean, and
  whether `83` is "phone mic + mode 03 (rhythm)", is unknown. The owner reports that switching
  the app to phone mic loses the device-mic animations (2026-10-06): the phone sends a color,
  the strip shows it. So injecting this stream does not drive the built-in effects; it is
  only interesting as a cheap whole-strip color channel driven by the computer's audio.
  The built-in effects follow the strip's own microphone only, which the owner rejects
  (poor sync, noise), so this project never uses device-mic modes.
* Whole-strip commands are much cheaper than sketch uploads: the H612D ACKs brightness
  (`33 04`) in ~116 ms against ~86 ms link RTT, and bartekpacia/govee drives other models at
  20 updates/s with `33 05 0d r g b`. Beat pulses can ride on these while a sketch upload
  (~3/s) holds the spatial pattern.

To remove the guesswork, capture the Govee app in phone-mic mode: install Apple's
"Bluetooth for iOS" logging profile, run phone-mic music for ~30 s, take a sysdiagnose, and
read the ATT writes to `2b11` from its `.pklg`.

### Encrypted BLE on newer Govee models

Some 2024+ models (H66A0, H1A42 with AES-GCM "v2"; H61F5 with AES-ECB+RC4 "v1") reject
plaintext. Detection: read `2b12`; byte 1 is the version, 0 = plaintext. The keys are
app-global constants plus SKU and MAC, so it is implementable; see
`custom_components/ha_govee_led_ble/govee_encryption/` in teh-hippo/ha-govee-led-ble.
`GoveeBle` refuses to connect with a clear error if the marker says encrypted. H612x is not
on anyone's encrypted list yet.

### macOS permission

CoreBluetooth needs a Bluetooth grant for the *responsible* app. From Terminal/iTerm that's
the terminal app (System Settings → Privacy & Security → Bluetooth). A process started from a
tool without an `Info.plist` usage string (for example the Claude Code agent) is killed with
SIGABRT, exit 134. `tools/ble_run.sh` runs the command under `build/GoveeBLE.app`, a tiny
launcher that declares Bluetooth usage, so the grant attaches to "GoveeBLE" instead.

## If you need to capture traffic

Prefer the UDP capture; it needs no hardware.

* **Wi‑Fi**: install Govee Desktop, enable its music mode for the strip, run Wireshark with
  filter `udp.port == 4003`, base64-decode the `pt` fields and compare with the frames above.
* **BLE from the phone app**: Android → Developer options → "Enable Bluetooth HCI snoop log",
  drive the strip from the Govee app, pull the log with `adb bugreport`, open in Wireshark
  and filter on ATT writes to handle of `...2b11`. iPhone → install the Bluetooth logging
  profile from Apple's developer site and read it with PacketLogger (Xcode additional tools).
  A Mac can also capture its own BLE traffic with PacketLogger if you drive the strip from a
  Mac-side script.
* An nRF52840 dongle with Wireshark's nRF Sniffer plugin is the hardware route if you need
  to see the phone↔strip air traffic directly; it's overkill here.

## Sources

* OpenRGB Govee controller (frame format, H612F with 12 segments):
  https://gitlab.com/CalcProgrammer1/OpenRGB/-/merge_requests/2172
  (mirror: https://github.com/CalcProgrammer1/OpenRGB, `Controllers/GoveeController/`)
* DMXCore Govee plugin (40 fps streaming, 15 ms pacing, keepalive):
  https://www.nuget.org/packages/DMXCore.Plugin.Govee
* govee2mqtt LAN docs: https://github.com/wez/govee2mqtt/blob/main/docs/LAN.md
* Home Assistant govee_light_local supported models:
  https://www.home-assistant.io/integrations/govee_light_local/
* Govee supported models (Platform API): https://developer.govee.com/docs/support-product-model
* Govee official LAN guide (JS-rendered page): https://app-h5.govee.com/user-manual/wlan-guide
* homebridge-govee LAN wiki: https://github.com/homebridge-plugins/homebridge-govee/wiki/LAN-Control
* CSA Matter listing (H612E family): https://csa-iot.org/pt/csa_product/govee-strip-light-s-4/
* BLE notes: https://notes.generalzero.org/Reverse%20Engineering/Signals/Bluetooth/Govee_lights.html
  and https://github.com/chvolkmann/govee_btled
* H617A segment command, encryption marker and handshakes:
  https://github.com/teh-hippo/ha-govee-led-ble (`light_commands.py`, `transport.py`,
  `tools/ble/kaitai/command_write.ksy`, issue #257)
