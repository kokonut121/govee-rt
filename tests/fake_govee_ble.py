"""
Fake Govee BLE light for developing GoveeBle without hardware or bleak.

Stands in for bleak.BleakClient (pass `lambda: FakeBleClient()` as GoveeBle's client_factory).
It decodes the 20-byte command packets the way an H617A-class strip does, keeps per-segment
state, rejects bad lengths/checksums (counted in `bad`, which must stay 0), and answers
AA queries on the notify characteristic.
"""
import asyncio
import collections
import types

WRITE = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY = "00010203-0405-0607-0809-0a0b0c0d2b10"
ENC = "00010203-0405-0607-0809-0a0b0c0d2b12"


def xor(data) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


class FakeBleClient:
    def __init__(self, segments=15, mtu=185, enc_marker=None, write_delay=0.0,
                 name="ihoment_H612C_8A3F", address="FAKE-0000-ADDR", group_size=3, readback=True,
                 firmware="1.00.11", hardware="3.01.01", light_count=48):
        self.n = segments
        self.name = name
        self.address = address
        self.group_size = group_size      # segment records per AA A5 page (H617A: 3)
        self.readback = readback
        self.firmware = firmware
        self.hardware = hardware
        self.light_count = light_count
        self.mtu_size = mtu
        self.enc_marker = enc_marker
        self.write_delay = write_delay  # seconds per write, to simulate a slow radio
        self.is_connected = False
        self.on = False
        self.brightness = 100
        self.segs = [(0, 0, 0)] * segments
        self.rt_mode = False
        self.mode = 0x15
        self.cells = 3 * segments          # Finger Sketch resolution
        self.sketch = None                 # last uploaded picture: list of cell colors
        self.shown = None                  # what the strip displays in sketch mode
        self.sketch_effect = None
        self._a3 = []
        self.streamed = []                 # colors received on the a5 02 83 stream
        self.razer_frames = []
        self.writes = []
        self.bad = 0
        self.stats = collections.Counter()
        self._notify = None
        chars = [types.SimpleNamespace(uuid=WRITE), types.SimpleNamespace(uuid=NOTIFY)]
        if enc_marker is not None:
            chars.append(types.SimpleNamespace(uuid=ENC))
        self.services = [types.SimpleNamespace(uuid="00010203-0405-0607-0809-0a0b0c0d1910",
                                               characteristics=chars)]

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, cb):
        assert uuid == NOTIFY
        self._notify = cb

    async def read_gatt_char(self, uuid):
        assert uuid == ENC
        return bytearray(self.enc_marker)

    async def write_gatt_char(self, uuid, data, response=False):
        assert uuid == WRITE and self.is_connected
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        data = bytes(data)
        self.writes.append(data)
        if data[:1] == b"\xBB":
            self._razer(data)
            return
        if data[:3] == b"\xA5\x02\x83":                    # phone-mic style color stream
            if len(data) != 7 or sum(data[:6]) & 0xFF != data[6]:
                self.bad += 1
                return
            self.streamed.append(tuple(data[3:6]))
            self.mode = 0x83
            return
        if len(data) != 20 or xor(data[:19]) != data[19]:
            self.bad += 1
            return
        head, cmd = data[0], data[1]
        if head == 0xA3:
            self._a3_frame(data)
            return
        if head == 0xAA:
            self.stats["query"] += 1
            reply = self._query_reply(cmd, data)
            if reply is not None and self._notify:
                reply = (reply + bytes(19))[:19]
                self._notify(None, bytearray(reply + bytes([xor(reply)])))
        elif head == 0x33 and cmd == 0x01:
            self.on = bool(data[2])
            self.stats["power"] += 1
            self._ack(cmd)
        elif head == 0x33 and cmd == 0x04:
            if not 0 <= data[2] <= 100:
                self.bad += 1
            self.brightness = data[2]
            self.stats["brightness"] += 1
            self._ack(cmd)
        elif head == 0x33 and cmd == 0x05 and data[2] == 0x0A:      # select DIY / Finger Sketch
            self.mode = 0x0A
            self.shown = list(self.sketch) if self.sketch else None
            self.stats["sketch_select"] += 1
            self._ack(cmd)
        elif head == 0x33 and cmd == 0x05 and data[2:4] == b"\x15\x01":
            rgb = tuple(data[4:7])
            mask = int.from_bytes(data[12:19], "little")
            if data[9:12] != data[4:7] or not mask:
                self.bad += 1
                return
            for i in range(self.n):
                if mask >> i & 1:
                    self.segs[i] = rgb
            self.stats["segment_color"] += 1
            self._ack(cmd)
        else:
            self.stats["unknown"] += 1

    def _a3_frame(self, data):
        idx = data[1]
        if idx != 0xFF:
            if idx != len(self._a3):
                self.bad += 1
                self._a3 = []
                return
            self._a3.append(data[2:19])
            return
        payload, self._a3 = b"".join(self._a3), []
        if not payload or payload[0] != 0x01 or payload[1] != -(-len(payload) // 17) + 1:
            self.bad += 1
            return
        body = payload[2:]
        if body[0] != 0x03:
            self.stats["a3_other"] += 1
            return
        effect = body[1]
        bg = tuple(body[4:7])
        pic = [bg] * self.cells
        k, pos = body[7], 8
        for _ in range(k):
            n, fill = body[pos], tuple(body[pos + 1:pos + 4])
            for c in body[pos + 4:pos + 4 + n]:
                if c >= self.cells:
                    self.bad += 1
                    return
                pic[c] = fill
            pos += 4 + n
        self.sketch, self.sketch_effect = pic, effect
        self.stats["sketch_upload"] += 1
        if self._notify:
            reply = bytes([0xA3, 0x03, 0x00]) + bytes(16)
            self._notify(None, bytearray(reply + bytes([xor(reply)])))

    def _ack(self, cmd):
        """The real strip answers every 33 command with 33 <cmd> 00 (status ok)."""
        if self._notify:
            reply = bytes([0x33, cmd]) + bytes(17)
            self._notify(None, bytearray(reply + bytes([xor(reply)])))

    def _query_reply(self, cmd, data):
        if cmd == 0x01:
            return bytes([0xAA, 0x01, 1 if self.on else 0])
        if cmd == 0x04:
            return bytes([0xAA, 0x04, self.brightness])
        if cmd == 0x05:
            return bytes([0xAA, 0x05, self.mode])
        if cmd == 0x06:
            return bytes([0xAA, 0x06]) + self.firmware.encode()
        if cmd == 0x07 and data[2] == 0x03:
            return bytes([0xAA, 0x07, 0x03]) + self.hardware.encode()
        if cmd == 0x0F:
            return bytes([0xAA, 0x0F, self.light_count])
        if cmd == 0xA5 and self.readback:
            g = data[2]
            first = (g - 1) * self.group_size
            if g < 1 or first >= self.n:
                return None
            recs = b""
            for k in range(4):
                i = first + k
                recs += bytes([100, *self.segs[i]]) if k < self.group_size and i < self.n else bytes(4)
            return bytes([0xAA, 0xA5, g]) + recs
        return None

    def _razer(self, data):
        n = data[1] << 8 | data[2]
        if len(data) != n + 5 or xor(data[:-1]) != data[-1] or len(data) > self.mtu_size - 3:
            self.bad += 1
            return
        if data[3] == 0xB1:
            self.rt_mode = bool(data[4])
        elif data[3] == 0xB0:
            k = data[5]
            self.razer_frames.append([tuple(data[6 + 3 * i: 9 + 3 * i]) for i in range(k)])
