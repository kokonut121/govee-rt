"""
govee_rt  --  real-time, per-segment control of Govee lights over Wi-Fi (LAN) or Bluetooth,
            plus an audio-reactive streamer that listens to whatever your computer
            is playing (Spotify, YouTube, anything).

Transport: Govee's LAN API (UDP) and the undocumented "razer" / DreamView real-time
mode that Govee Desktop + Razer Chroma use.  Same framing OpenRGB uses:

    pt = BB <len_hi> <len_lo> <subcmd> <data...> <xor of all preceding bytes>
    wrapped as  {"msg":{"cmd":"razer","data":{"pt":"<base64(pt)>"}}}  -> UDP :4003

    subcmd B1 01      enter real-time mode (times out after ~1 min of silence)
    subcmd B1 00      leave real-time mode
    subcmd B0 01 N (r g b)*N   set N segments, no firmware fade

Prereq: in the Govee app, open the strip -> settings -> turn on "LAN Control".

Usage
    govee-rt                                              # find the light, set up audio, open the studio
    python -m govee_rt scan                               # find lights, print IP + SKU
    python -m govee_rt solid  --ip IP --rgb 255 40 0      # sanity check (official LAN cmd)
    python -m govee_rt probe  --ip IP                     # find how many segments it accepts
    python -m govee_rt chase  --ip IP --segments 15       # real-time mode sanity check
    python -m govee_rt devices                            # list audio input devices
    python -m govee_rt audio  --ip IP --segments 15 --device "BlackHole 2ch" --mode spectrum
    python -m govee_rt audio  --ip IP --segments 15 --wav song.wav --preview   # test from a file

Deps: numpy, sounddevice (live audio), bleak (Bluetooth).
"""

__version__ = "0.2.0"

from .audio import Analyzer, live_blocks, wav_blocks
from .ble import (BLE_ENC_MARKER, BLE_MAX_SEGMENTS, BLE_NOTIFY, BLE_WRITE, GoveeBle, ble_brightness,
                  ble_keepalive, ble_packet, ble_power, ble_scan, ble_segment_color,
                  ble_segment_packets, resample)
from .cli import main
from .colors import gamma, hsv
from .protocol import (GoveeLan, _xor, frame_colors, frame_disable, frame_enable, lan_json,
                       razer_colors, razer_frame, razer_json, scan)
from .visuals import Visualizer, preview_line
