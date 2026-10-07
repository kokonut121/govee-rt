"""
Device identification and remembered profiles: segment readback, model table fallback, and the
CLI's first-connect flow. Uses tests/fake_govee_ble.py; no Bluetooth needed.
"""
import argparse
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import govee_rt  # noqa: E402
from govee_rt import cli, device  # noqa: E402
from fake_govee_ble import FakeBleClient  # noqa: E402


def ble_with(fake):
    return govee_rt.GoveeBle(segments=16, client_factory=lambda: fake)


@pytest.mark.parametrize("segments,group", [(15, 3), (20, 4), (12, 4), (10, 3), (1, 3)])
def test_identify_measures_segment_count(segments, group):
    fake = FakeBleClient(segments=segments, group_size=group)
    g = ble_with(fake)
    try:
        info = g.identify()
    finally:
        g.close()
    assert info["segments_measured"] == segments
    assert info["firmware"] == "1.00.11" and info["hardware"] == "3.01.01"
    assert info["light_count"] == 48 and info["name"] == "ihoment_H612C_8A3F"
    assert fake.bad == 0


def test_identify_measures_long_strips():
    g = ble_with(FakeBleClient(segments=48, group_size=4))
    try:
        assert g.identify()["segments_measured"] == 48
    finally:
        g.close()


def test_identify_without_readback_falls_back():
    g = ble_with(FakeBleClient(readback=False, name="Govee_H612F_1234"))
    try:
        info = g.identify()
    finally:
        g.close()
    assert info["segments_measured"] is None and "no reply" in info["segments_note"]
    info["sku"] = device.sku_from_name(info["name"])
    assert device.choose_segments(info) == (12, "known for H612F")


def test_sku_from_name():
    assert device.sku_from_name("ihoment_H612C_8A3F") == "H612C"
    assert device.sku_from_name("Govee_H617A_ABCD") == "H617A"
    assert device.sku_from_name("GBK_H6199_0001") == "H6199"
    assert device.sku_from_name("Some Speaker") is None
    assert device.sku_from_name(None) is None


def test_choose_segments_order():
    assert device.choose_segments({"segments_measured": 14, "sku": "H612F"}, manual=20) == (20, "set by hand")
    assert device.choose_segments({"segments_measured": 14, "sku": "H612F"}) == (14, "measured")
    n, why = device.choose_segments({"sku": "H612C"})
    assert n == 15 and why.startswith("default")


def test_devices_store(tmp_path):
    d = device.Devices(str(tmp_path / "devices.json"))
    assert d.default() is None
    d.save({"transport": "ble", "address": "A", "segments": 15})
    d.save({"transport": "lan", "ip": "10.0.0.5", "segments": 12}, make_default=False)
    assert d.default()["address"] == "A"
    assert d.find("lan", "10.0.0.5")["segments"] == 12
    assert d.find("ble", "nope") is None


# ----------------------------------------------------------------------------- CLI flow

@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "DEVICES_FILE", str(tmp_path / "devices.json"))
    fake = FakeBleClient(segments=12, group_size=4, address="AA-BB")
    made = []

    def fake_ble(target, segments=15, mode="segments", **kw):
        made.append(target)
        return govee_rt.GoveeBle(target, segments=segments, mode=mode,
                                 client_factory=lambda: fake)
    monkeypatch.setattr(cli, "GoveeBle", fake_ble)
    return fake, made


def parse(*args):
    ns = argparse.Namespace(ip=None, ble=None, ble_mode=None, segments=None, no_send=False, reverse=None, cells=None,
                            brightness=100, no_measure=False)
    for k, v in args:
        setattr(ns, k, v)
    return ns


def test_first_connect_identifies_then_everything_uses_the_profile(cli_env, capsys):
    fake, made = cli_env
    p = argparse.ArgumentParser()

    # first run: `studio --ble` with nothing saved -> identify, measure 12, save as default
    a = parse(("cmd", "studio"), ("ble", "auto"))
    cli.resolve_device(a, p)
    assert a.profile is None and a.segments is None
    cli.ensure_profile(a)
    assert a.profile["segments"] == 12 and a.profile["segments_source"] == "measured"
    assert a.profile["sku"] == "H612C" and a.profile["address"] == "AA-BB"
    assert a.profile["cells"] == 36 and a.ble_mode == "sketch"   # H612x: 3 sketch cells per segment
    assert a.segments == 36                                      # effects render per cell
    light = cli.open_light(a)              # reuses the identify connection
    assert len(made) == 1 and light.segments == 12 and light.mode == "sketch" and light.cells == 36
    light.close()

    # later: `play` with no flags at all -> default device and its segment count
    b = parse(("cmd", "play"))
    cli.resolve_device(b, p)
    assert b.ble == "AA-BB" and b.segments == 36 and b.profile["sku"] == "H612C"
    cli.ensure_profile(b)                  # already known: no second identify
    assert len(made) == 1

    # an explicit --segments still wins
    c = parse(("cmd", "chase"), ("segments", 6))
    cli.resolve_device(c, p)
    assert c.segments == 6


def test_connect_with_manual_segments(cli_env, capsys):
    fake, _ = cli_env
    a = parse(("cmd", "connect"), ("ble", "auto"), ("segments", 9), ("ble_mode", "segments"), ("reverse", None))
    cli.resolve_device(a, argparse.ArgumentParser())
    cli.cmd_connect(a)
    prof = device.Devices().default()
    assert prof["segments"] == 9 and prof["segments_source"] == "set by hand"
    assert fake.segs[:9] == [tuple(govee_rt.hsv(i / 9, 1, 1)) for i in range(9)]   # 9-band rainbow
    assert "9 color bands" in capsys.readouterr().out


def test_no_device_and_no_flags_is_a_clear_error(cli_env):
    p = argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        cli.resolve_device(parse(("cmd", "play")), p)
    a = parse(("cmd", "studio"))           # studio without a device: preview only
    cli.resolve_device(a, p)
    assert a.segments == 15 and not a.ble
