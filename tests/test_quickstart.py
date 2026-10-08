"""First-run path: bare `govee-rt` runs the wizard, loopback input detection, error hints, and
the last look coming back on the next start. No audio hardware or light needed."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from govee_rt import __version__, cli, quickstart  # noqa: E402


def dev(name, inputs=2):
    return {"name": name, "max_input_channels": inputs}


@pytest.mark.parametrize("devices,want", [
    ([dev("MacBook Pro Microphone", 1), dev("BlackHole 2ch")], "BlackHole 2ch"),
    ([dev("BlackHole 2ch", 0), dev("MacBook Pro Microphone", 1)], None),      # output side only
    ([dev("Built-in Audio Analog Stereo"), dev("Monitor of Built-in Audio Analog Stereo")],
     "Monitor of Built-in Audio Analog Stereo"),
    ([dev("Microphone (Realtek)"), dev("CABLE Output (VB-Audio Virtual Cable)")],
     "CABLE Output (VB-Audio Virtual Cable)"),
    ([dev("Monitor of Speakers"), dev("BlackHole 16ch")], "BlackHole 16ch"),  # BlackHole first
    ([], None),
])
def test_find_loopback(devices, want):
    assert quickstart.find_loopback(devices) == want


def test_bluetooth_hints():
    assert "Privacy & Security" in quickstart.bluetooth_hint(Exception("BLE is not authorized")) or \
        sys.platform != "darwin"
    assert "Bluetooth on" in quickstart.bluetooth_hint(Exception("Bluetooth device is turned off"))
    assert "Govee app" in quickstart.bluetooth_hint(RuntimeError("no Govee light found over Bluetooth."))
    assert quickstart.bluetooth_hint(ValueError("something else")) is None


def test_bare_command_runs_the_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "Devices", lambda: cli_devices(tmp_path))
    seen = {}
    monkeypatch.setattr(cli, "cmd_start", lambda a: seen.update(cmd=a.cmd, no_send=a.no_send))
    cli.main([])
    assert seen == {"cmd": "start", "no_send": False}
    cli.main(["--no-send"])
    assert seen == {"cmd": "start", "no_send": True}


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_errors_print_a_fix_not_a_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "Devices", lambda: cli_devices(tmp_path))
    monkeypatch.delenv("GOVEE_RT_DEBUG", raising=False)

    def fail(a):
        raise RuntimeError("no Govee light found over Bluetooth. Is the phone app still connected?")
    monkeypatch.setattr(cli, "cmd_start", fail)
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "error: no Govee light found" in err and "fix: close the Govee app" in err
    assert "Traceback" not in err


def test_last_scene_comes_back(monkeypatch, tmp_path):
    from govee_rt.studio import Presets
    monkeypatch.setattr(cli, "LAST_SCENE", str(tmp_path / "sub" / "last_scene.json"))
    args = type("A", (), {"scene": None, "preset": None})()
    presets = Presets(user_dir=str(tmp_path / "presets"))
    assert cli.load_scene(args, presets)["name"] == cli.default_scene()["name"]
    scene = presets.load("Bass Pulse")
    cli.save_last_scene(scene)
    assert cli.load_scene(args, presets)["name"] == "Bass Pulse"
    (tmp_path / "sub" / "last_scene.json").write_text("{not json")
    assert cli.load_scene(args, presets)["name"] == cli.default_scene()["name"]
    json.dumps(scene)


def test_builtin_presets_ship_with_the_package():
    from govee_rt.studio import BUILTIN_PRESETS
    names = {f[:-5] for f in os.listdir(BUILTIN_PRESETS) if f.endswith(".json")}
    assert {"Bass Pulse", "Rolling Neon", "Spectrum"} <= names


def cli_devices(tmp_path):
    from govee_rt.device import Devices
    return Devices(str(tmp_path / "devices.json"))
