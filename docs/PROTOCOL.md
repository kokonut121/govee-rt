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
  count) is model-specific and unknown for this unit; `govee_rt.py probe` finds it by eye.
  What happens for N ≠ segment count (ignored? resampled?) is also unknown; the probe
  will show it.
* Brightness is still governed by the official `brightness` command; real-time frames are
  scaled by it. Set it to 100 once and do dimming in the frame data.

## Bluetooth fallback

Only needed if the Wi‑Fi real-time path turns out not to work on this SKU. It is slower
(write-without-response at the BLE connection interval, realistically 10–20 fps) and the
per-segment packet formats vary by model, so prefer UDP.

Known from govee_btled, govee-led-wez and generalzero's notes:

* GATT service `00010203-0405-0607-0809-0a0b0c0d1910`
* write characteristic `00010203-0405-0607-0809-0a0b0c0d2b11`, notify `...2b10`
* 20-byte packets. Byte 0 = `0x33` for commands, `0xAA` for keep-alive/status queries.
  Byte 1 = command (`01` power, `04` brightness, `05` color). Last byte = XOR of the first 19.
* Solid color on older strips: `33 05 02 rr gg bb ...`. RGBIC segment commands use other
  sub-codes (`33 05 15 01 rr gg bb <segment bitmask>` on some models) and that's the part
  you'd have to confirm by capture for this SKU.
* Python: `bleak` can do all of this from macOS/Linux/Windows.

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
