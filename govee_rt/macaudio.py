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


# --------------------------------------------------------------------------- routing setup
# What a new user otherwise does by hand in Audio MIDI Setup: a Multi-Output Device with the
# speakers (clock) and BlackHole (drift-corrected), set as the system output, so whatever plays
# is heard and captured at once. Only ever run when the user asks for it (`govee-rt audio-setup`
# or yes at the first-run prompt).

DEVICE_UID = _fourcc("uid ")
MULTI_OUTPUT_NAME = "Govee RT Output"
MULTI_OUTPUT_UID = "com.govee-rt.multi-output"
_UTF8 = 0x08000100

if _cf is not None:
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFNumberCreate.restype = ctypes.c_void_p
    _cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    _cf.CFArrayCreate.restype = ctypes.c_void_p
    _cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    _cf.CFDictionaryCreate.restype = ctypes.c_void_p
    _cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                                       ctypes.c_void_p, ctypes.c_void_p]
if _ca is not None:
    _ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]


def uid(dev):
    ref = _get(dev, DEVICE_UID, ctypes.c_void_p)
    if ref is None or not ref.value:
        return None
    buf = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(ref.value, buf, 512, _UTF8)
    _cf.CFRelease(ref.value)
    return buf.value.decode() if ok else None


def default_output():
    v = _get(SYSTEM, DEFAULT_OUTPUT, ctypes.c_uint32)
    return v.value if v else None


def name(dev):
    return _name(dev)


def subdevices(dev):
    return _get_array(dev, ACTIVE_SUBDEVICES, ctypes.c_uint32)


def _cf_value(v, owned):
    """Python int/str/list/dict -> CF object; every created ref is appended to `owned`."""
    if isinstance(v, int):
        n = ctypes.c_int32(v)
        ref = _cf.CFNumberCreate(None, 3, ctypes.byref(n))           # kCFNumberSInt32Type
    elif isinstance(v, str):
        ref = _cf.CFStringCreateWithCString(None, v.encode(), _UTF8)
    elif isinstance(v, list):
        items = (ctypes.c_void_p * len(v))(*[_cf_value(x, owned) for x in v])
        cb = ctypes.addressof(ctypes.c_char.in_dll(_cf, "kCFTypeArrayCallBacks"))
        ref = _cf.CFArrayCreate(None, items, len(v), cb)
    elif isinstance(v, dict):
        keys = (ctypes.c_void_p * len(v))(*[_cf_value(k, owned) for k in v])
        vals = (ctypes.c_void_p * len(v))(*[_cf_value(x, owned) for x in v.values()])
        kcb = ctypes.addressof(ctypes.c_char.in_dll(_cf, "kCFTypeDictionaryKeyCallBacks"))
        vcb = ctypes.addressof(ctypes.c_char.in_dll(_cf, "kCFTypeDictionaryValueCallBacks"))
        ref = _cf.CFDictionaryCreate(None, keys, vals, len(v), kcb, vcb)
    else:
        raise TypeError(type(v))
    owned.append(ref)
    return ref


def create_multi_output(main_dev, capture_dev, label=MULTI_OUTPUT_NAME, dev_uid=MULTI_OUTPUT_UID,
                        private=False):
    """Make a Multi-Output Device (stacked aggregate) playing to `main_dev` (its clock) and
    `capture_dev` (drift-corrected). A private one is visible only to this process and goes
    away with it. Returns the new device id, or None."""
    if _ca is None:
        return None
    main_uid, cap_uid = uid(main_dev), uid(capture_dev)
    if not (main_uid and cap_uid):
        return None
    owned = []
    desc = _cf_value({"uid": dev_uid, "name": label, "stacked": 1, "private": int(private),
                      "master": main_uid, "clock": main_uid,
                      "subdevices": [{"uid": main_uid, "drift": 0}, {"uid": cap_uid, "drift": 1}]},
                     owned)
    out = ctypes.c_uint32(0)
    err = _ca.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(out))
    for ref in owned:
        _cf.CFRelease(ref)
    return out.value if err == 0 and out.value else None


def destroy(dev):
    return _ca is not None and _ca.AudioHardwareDestroyAggregateDevice(dev) == 0


def set_default_output(dev):
    if _ca is None or dev is None:
        return False
    a = _Addr(DEFAULT_OUTPUT, GLOBAL, 0)
    v = ctypes.c_uint32(dev)
    return _ca.AudioObjectSetPropertyData(SYSTEM, ctypes.byref(a), 0, None, ctypes.sizeof(v), ctypes.byref(v)) == 0


def routing_status(capture="BlackHole"):
    """(state, output name): 'unsupported' off macOS, 'missing' with no capture driver,
    'routed' when the system output already plays into it, else 'unrouted'."""
    if _ca is None:
        return "unsupported", None
    cap = find_device(capture)
    if cap is None:
        return "missing", None
    out = default_output()
    label = name(out) if out is not None else None
    if out is not None and (out == cap or cap in subdevices(out)):
        return "routed", label
    return "unrouted", label


def route_to_capture(capture="BlackHole"):
    """Make the system output play to the current speakers and the capture device at once.
    Reuses our Multi-Output Device when it already wraps these speakers. Returns (ok, message)."""
    state, label = routing_status(capture)
    if state == "routed":
        return True, f"the system output '{label}' already feeds {capture}"
    if state == "missing":
        return False, f"{capture} is not installed"
    if state == "unsupported":
        return False, "automatic audio routing is macOS only"
    cap, out = find_device(capture), default_output()
    for dev in _get_array(SYSTEM, DEVICES, ctypes.c_uint32):
        if uid(dev) == MULTI_OUTPUT_UID:
            if out in subdevices(dev) and set_default_output(dev):
                return True, f"system output set to '{MULTI_OUTPUT_NAME}'"
            destroy(dev)                      # built around other speakers: rebuild it
            break
    dev = create_multi_output(out, cap)
    if dev is None:
        return False, "CoreAudio refused to create the Multi-Output Device"
    if not set_default_output(dev):
        destroy(dev)
        return False, f"could not select '{MULTI_OUTPUT_NAME}' as the system output"
    return True, (f"system output is now '{MULTI_OUTPUT_NAME}' ({label} + {name(cap)}). To undo, "
                  f"pick {label} in the menu bar sound menu. Volume keys don't work on a "
                  f"Multi-Output Device; set the volume on the speaker or in the app.")
