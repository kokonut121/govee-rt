"""First-run experience: `govee-rt` with no command finds the light, finds an audio input that
hears what the computer plays, and opens the studio. `doctor` reports the same checks,
`audio-setup` fixes the audio part.
"""

import os
import platform
import shutil
import subprocess
import sys

from . import macaudio

# Loopback inputs, best first. Matched case-insensitively against input device names.
LOOPBACK_NAMES = ("blackhole", "loopback audio", "soundflower", "monitor of", "cable output",
                  "stereo mix", "vb-audio")
BLACKHOLE_URL = "https://existential.audio/blackhole/"
VBCABLE_URL = "https://vb-audio.com/Cable/"


def find_loopback(devices=None):
    """Name of the best loopback input device, or None."""
    if devices is None:
        try:
            import sounddevice as sd
            devices = sd.query_devices()
        except Exception:
            return None
    inputs = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
    for pattern in LOOPBACK_NAMES:
        for n in inputs:
            if pattern in n.lower():
                return n
    return None


def ask(question, default=True):
    """Yes/no prompt; returns the default when nobody is at the terminal."""
    if not sys.stdin.isatty():
        return default
    hint = "Y/n" if default else "y/N"
    try:
        reply = input(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        return default
    return default if not reply else reply.startswith("y")


def loopback_help():
    system = platform.system()
    if system == "Darwin":
        return ("install BlackHole so the lights can hear your Mac:\n"
                "    brew install blackhole-2ch      (or download it from " + BLACKHOLE_URL + ")\n"
                "  then run: govee-rt audio-setup")
    if system == "Windows":
        return ("install VB-Cable (" + VBCABLE_URL + "), set 'CABLE Input' as the playback device\n"
                "  and enable 'Listen to this device' on 'CABLE Output' so you still hear it")
    return ("pick your speakers' 'Monitor of ...' source (PipeWire/PulseAudio); if none shows up,\n"
            "  install libportaudio2 and pulseaudio-utils, then check: govee-rt devices")


def install_blackhole():
    """brew install blackhole-2ch, after asking. True if it ran and succeeded."""
    brew = shutil.which("brew")
    if not brew:
        print(f"  Homebrew not found. Download BlackHole 2ch from {BLACKHOLE_URL}, install it,\n"
              f"  then run govee-rt audio-setup again.")
        return False
    if not ask("  Install BlackHole 2ch with Homebrew now? (it may ask for your password)"):
        return False
    return subprocess.call([brew, "install", "blackhole-2ch"]) == 0


def setup_audio(interactive=True):
    """Make sure system audio reaches a loopback input. Returns its name, or None."""
    if platform.system() != "Darwin":
        name = find_loopback()
        if not name:
            print("  no loopback audio input found; " + loopback_help())
        return name
    state, output = macaudio.routing_status()
    if state == "missing":
        print("  BlackHole (the free driver that lets apps hear your Mac's audio) is not installed.")
        if not (interactive and install_blackhole()):
            print("  " + loopback_help())
            return None
        state, output = macaudio.routing_status()
        if state == "missing":
            print("  BlackHole installed; macOS may need a moment (or a restart of the app) to show it.")
            return None
    if state == "unrouted":
        print(f"  your Mac plays to '{output}', so the lights can't hear it yet.")
        if interactive and ask(f"  Play to '{output}' and BlackHole at the same time (a Multi-Output "
                               f"Device)?"):
            ok, msg = macaudio.route_to_capture()
            print("  " + msg)
        else:
            print("  the lights only react to audio sent to BlackHole; run govee-rt audio-setup any time.")
    return find_loopback()


def doctor(profile_text, version):
    """Print what's set up and what's missing."""
    print(f"govee-rt {version}, Python {platform.python_version()} on {platform.system()} "
          f"{platform.machine()}")
    for mod in ("numpy", "sounddevice", "bleak"):
        try:
            __import__(mod)
            print(f"  ok    {mod}")
        except Exception as e:
            print(f"  FAIL  {mod}: {e}")
    print("\nlight:\n  " + profile_text.replace("\n", "\n  "))
    print("\naudio:")
    name = find_loopback()
    print(f"  {'ok   ' if name else 'FAIL '} loopback input: {name or 'none found'}")
    if platform.system() == "Darwin":
        state, output = macaudio.routing_status()
        msg = {"routed": f"system output '{output}' feeds BlackHole",
               "unrouted": f"system output '{output}' does not feed BlackHole (run govee-rt audio-setup)",
               "missing": "BlackHole not installed (run govee-rt audio-setup)"}.get(state, state)
        print(f"  {'ok   ' if state == 'routed' else 'FAIL '} {msg}")
    elif not name:
        print("  " + loopback_help())
    print("\nIf the light isn't found over Bluetooth: close the Govee app on your phone (the strip\n"
          "takes one connection at a time) and allow your terminal app in System Settings ->\n"
          "Privacy & Security -> Bluetooth.")


def bluetooth_hint(err):
    """A one-line fix for common Bluetooth failures, or None."""
    msg = str(err).lower()
    if "not authorized" in msg or "unauthorized" in msg:
        if platform.system() == "Darwin":
            return ("allow your terminal app in System Settings -> Privacy & Security -> Bluetooth, "
                    "then run it again")
        return "this user may not use Bluetooth; check your OS's Bluetooth permissions"
    if "turned off" in msg or "powered off" in msg or "no bluetooth adapters" in msg:
        return "turn Bluetooth on and run it again"
    if "no govee light found" in msg:
        return ("close the Govee app on your phone, make sure the strip is powered and nearby, "
                "then run it again")
    return None


def debug():
    return bool(os.environ.get("GOVEE_RT_DEBUG"))
