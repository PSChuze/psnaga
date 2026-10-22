"""Self-test for nobu.vce against the captured client frame and the client code's expectations.

    cd server && python -m unittest nobu.test_vce -v
"""
import struct
import unittest

from nobu import vce

# the client's first frame on 9070, the same on every boot
CAPTURED = bytes.fromhex("0011" "15" "00000001" "000f4309" "00000000" "00004e23")


class Framing(unittest.TestCase):
    def test_split_and_rebuild(self):
        fr = vce.FrameReader()
        self.assertEqual(fr.feed(CAPTURED[:5]), [])
        (payload,) = fr.feed(CAPTURED[5:])
        self.assertEqual(len(payload), 0x11)
        self.assertEqual(vce.frame(payload), CAPTURED)
        self.assertEqual(fr.buf, bytearray())

    def test_two_frames_in_one_read(self):
        fr = vce.FrameReader()
        self.assertEqual(len(fr.feed(CAPTURED + CAPTURED + b"\x00")), 2)
        self.assertEqual(bytes(fr.buf), b"\x00")


class LengthHeader(unittest.TestCase):
    # 0x1287d0: b < 0xFD -> b; 0xFD -> u32be; 0xFE -> u32be flag(0) + u32be; 0xFF -> error
    def test_short_and_long(self):
        for n in (0, 1, 0xFC, 0xFD, 0x1FFF):
            blob = vce.put_bytes(b"x" * n)
            self.assertEqual(vce.Reader(blob).bytes(), b"x" * n)
        self.assertEqual(vce.put_len(0xFC), b"\xfc")
        self.assertEqual(vce.put_len(0xFD), b"\xfd\x00\x00\x00\xfd")

    def test_fe_and_ff(self):
        self.assertEqual(vce.Reader(b"\xfe\0\0\0\0\0\0\0\x02ab").bytes(), b"ab")
        with self.assertRaises(vce.WireError):
            vce.Reader(b"\xfe\0\0\0\1\0\0\0\x02ab").bytes()
        with self.assertRaises(vce.WireError):
            vce.Reader(b"\xff").bytes()

    def test_client_buffer_limit(self):
        with self.assertRaises(vce.WireError):
            vce.Reader(vce.put_bytes(b"y" * 0x101)).bytes(0x100)


class VersionCheck(unittest.TestCase):
    def test_decode_capture(self):
        (payload,) = vce.FrameReader().feed(CAPTURED)
        # 0x18de58: codes[0] = gp-0x7e30 (Code), zeros[0] = gp-0x7e18 word 0, vers[0] = gp-0x7e34
        self.assertEqual(vce.decode_version_check(payload),
                         [(vce.CLIENT_CODE, 0, vce.CLIENT_VERSION)])
        self.assertEqual(vce.encode_version_check([(1000201, 0, 20003)]), payload)


def client_parse_22(payload):
    """Mirror of the client's 0x22 parse (0x1f60d0) and state-0 decision (0x28e6a0).

    Returns the next update state the client moves to (0x28e350 argument).
    """
    r = vce.Reader(payload)
    assert r.u8() == 0x22                      # dispatcher 0x1f5af0: signed cmd byte, table 0x4233a0[4]
    r.u32()                                    # 0x1f60dc  -> +0x0
    r.u32()                                    # 0x1f6124  -> +0x4
    status = r.i32()                           # 0x1f616c  -> +0x8
    total = r.i8()                             # 0x1f61b8  -> +0xc
    seq = r.i8()                               # 0x1f6200  -> +0xd
    text = r.bytes(0x2000)                     # 0x1f6258  -> +0xe, max 0x2000
    host = r.bytes(0x100)                      # 0x1f62b8  -> +0x2014, max 0x100
    assert r.done()
    # 0x28e6a0: status switch; only the no-patch, no-notice branch is modelled
    if status == 0 and seq > 0:                # 0x28e800 signed +0xd; <= 0 -> else
        return 7, host                         # 0x28e80c strcpy(obj+0x6c, host); state 7
    return None, host


class VersionReply(unittest.TestCase):
    def test_exact_bytes(self):
        entries = vce.decode_version_check(CAPTURED[2:])
        rep = vce.up_to_date_reply(entries, "nobol.koei.co.jp/auth")
        host = b"nobol.koei.co.jp/auth"
        want = (b"\x22" + bytes.fromhex("000f4309") + bytes.fromhex("00004e23")
                + bytes.fromhex("00000000") + b"\x01\x01" + b"\x00"
                + bytes([len(host)]) + host)
        self.assertEqual(rep, want)
        self.assertEqual(vce.frame(rep)[:2], struct.pack(">H", 17 + len(host)))

    def test_client_takes_no_patch_path(self):
        rep = vce.up_to_date_reply([(1000201, 0, 20003)], "nobol.koei.co.jp/auth")
        state, host = client_parse_22(rep)
        self.assertEqual(state, 7)                     # 0x29b040: done, go to login
        self.assertEqual(host, b"nobol.koei.co.jp/auth")
        self.assertEqual(vce.decode_version_reply(rep)["status"], vce.STATUS_OK)

    def test_host_limit(self):
        with self.assertRaises(vce.WireError):
            vce.encode_version_reply(0, 0, 0, 1, 1, b"", b"h" * 0x100)


class WorldList(unittest.TestCase):
    # captures: client 0x14 after the login, "14 000f4309 40000000"
    REQ14 = bytes.fromhex("14000f430940000000")

    def test_decode_14(self):
        self.assertEqual(vce.decode_req_14(self.REQ14), (1000201, 0x40000000))

    def test_world_list_bytes(self):
        worlds = vce.parse_worlds("Nobunaga@198.51.100.7:9100")
        count, entry, end = vce.world_list(worlds)
        self.assertEqual(count, b"\x1f\x00\x01")
        self.assertEqual(end, b"\x20")
        self.assertEqual(entry, bytes.fromhex(
            "1e" "0001" "0000" "00000000"             # id, sub, key
            "08") + b"Nobunaga" + bytes.fromhex(
            "00"                                      # +0x48
            "0c") + b"198.51.100.7" + bytes.fromhex(
            "238c"                                    # port 9100
            "00" "0000" "00"))                        # status, +0x8e, empty blob
        d = vce.decode_world_entry(entry)
        self.assertEqual((d["id"], d["host"], d["port"], d["b49"]), (1, b"198.51.100.7", 9100, None))

    def test_client_parse_order(self):
        """Mirror of 0x1f5cc8: u16, u16, u32, bytes(0x7e), u8, bytes(0x40), u16, u8, u16, bytes(0x400)."""
        e = vce.encode_world_entry(3, "織田", "10.0.0.1", 9200, status=1, key=7, sub=2, blob=b"xy", b49=5)
        r = vce.Reader(e, 1)
        got = (r.u16(), r.u16(), r.u32(), r.bytes(0x7e).decode("utf-8"), r.u8(),
               r.bytes(0x40), r.u16(), r.u8(), r.u16(), r.bytes(0x400), r.u8())
        self.assertEqual(got, (3, 2, 7, "織田", 0, b"10.0.0.1", 9200, 1, 0, b"xy", 5))
        self.assertTrue(r.done())

    def test_status_23(self):
        self.assertEqual(vce.encode_world_status(7, 3, 2, 1), bytes.fromhex("23" "00000007" "0003" "0002" "01"))

    def test_limits(self):
        with self.assertRaises(vce.WireError):
            vce.world_list([dict(wid=i, name="w", host="h", port=1) for i in range(17)])
        with self.assertRaises(vce.WireError):
            vce.encode_world_entry(1, "w", "h" * 0x40, 1)
