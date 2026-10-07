"""CoreAudio sample-rate helpers (macOS only, ctypes, no extra dependencies).

Why: the capture device (BlackHole) sits inside a Multi-Output Device whose clock is a
Bluetooth speaker. When the speaker reconnects it can come back at a different rate (the
Marshall only does 44.1 kHz) while BlackHole stays where it was, and the Multi-Output Device
then resamples against a mismatched clock: the speaker stutters. `output_rate_for()` says what
rate the capture device should run at; `set_rate()` moves it there.

Every function returns None / False off macOS or when CoreAudio says no, so callers can use
them unconditionally.
"""

import ctypes
import ctypes.util
import struct
import sys

_ca = _cf = None
if sys.platform == "darwin":
    try:
        _ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    except OSError:
        _ca = _cf = None


def _fourcc(s):
    return struct.unpack(">I", s.encode())[0]


SYSTEM = 1
GLOBAL, INPUT, OUTPUT = _fourcc("glob"), _fourcc("inpt"), _fourcc("outp")
DEVICES = _fourcc("dev#")
DEFAULT_OUTPUT = _fourcc("dOut")
NAME = _fourcc("lnam")
NOMINAL_RATE = _fourcc("nsrt")
AVAILABLE_RATES = _fourcc("nsr#")
ACTIVE_SUBDEVICES = _fourcc("agrp")
STREAMS = _fourcc("stm#")


class _Addr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _get(obj, sel, ctype, scope=GLOBAL):
    if _ca is None:
        return None
    a = _Addr(sel, scope, 0)
    v = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(v))
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(v)):
        return None
    return v


def _get_array(obj, sel, ctype, scope=GLOBAL):
    if _ca is None:
        return []
    a = _Addr(sel, scope, 0)
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(a), 0, None, ctypes.byref(size)):
        return []
    n = size.value // ctypes.sizeof(ctype)
    if n == 0:
        return []
    arr = (ctype * n)()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None, ctypes.byref(size), arr):
        return []
    return list(arr)


def _name(dev):
    ref = _get(dev, NAME, ctypes.c_void_p)
    if ref is None or not ref.value:
        return None
    buf = ctypes.create_string_buffer(256)
    ok = _cf.CFStringGetCString(ref.value, buf, 256, 0x08000100)  # kCFStringEncodingUTF8
    _cf.CFRelease(ref.value)
    return buf.value.decode() if ok else None


def find_device(name, scope=INPUT):
    """AudioObjectID of the device whose name contains `name` and has streams in `scope`."""
    for dev in _get_array(SYSTEM, DEVICES, ctypes.c_uint32):
        n = _name(dev)
        if n and name.lower() in n.lower() and _get_array(dev, STREAMS, ctypes.c_uint32, scope):
            return dev
    return None


def rate(dev):
    v = _get(dev, NOMINAL_RATE, ctypes.c_double)
    return v.value if v else None


def available_rates(dev):
    pairs = _get_array(dev, AVAILABLE_RATES, ctypes.c_double)
    return [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)]


def set_rate(dev, sr):
    if _ca is None or dev is None:
        return False
    if not any(lo <= sr <= hi for lo, hi in available_rates(dev)):
        return False
    a = _Addr(NOMINAL_RATE, GLOBAL, 0)
    v = ctypes.c_double(sr)
    return _ca.AudioObjectSetPropertyData(dev, ctypes.byref(a), 0, None, ctypes.sizeof(v), ctypes.byref(v)) == 0


def output_rate_for(input_name):
    """(device id, rate) the capture device should run at, or (id, None) if nothing to match.

    Only matches when the capture device is part of the default output (a Multi-Output or
    aggregate device), i.e. the system is mirroring its playback into it."""
    dev = find_device(input_name)
    out = _get(SYSTEM, DEFAULT_OUTPUT, ctypes.c_uint32)
    if dev is None or out is None:
        return dev, None
    if dev not in _get_array(out.value, ACTIVE_SUBDEVICES, ctypes.c_uint32):
        return dev, None
    return dev, rate(out.value)
