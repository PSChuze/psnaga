"""Self-test for the world connection: python -m unittest nobu.test_world -v

Set NOBU_RAM_DUMP= to also compare constants with the client.
"""
import asyncio
import json
import os
import struct
import tempfile
import unittest

from nobu import npc, vce, vcecrypto, world


class BlowfishVectors(unittest.TestCase):
    def test_standard_vectors(self):
        # Eric Young's test set (big-endian blocks)
        for key, pt, ct in (("0000000000000000", "0000000000000000", "4ef997456198dd78"),
                            ("ffffffffffffffff", "ffffffffffffffff", "51866fd5b85ecb8a"),
                            ("0123456789abcdef", "1111111111111111", "61f9c3802281b096"),
                            ("fedcba9876543210", "0123456789abcdef", "0aceab0fc6a0a28d")):
            bf = vcecrypto.Blowfish(bytes.fromhex(key))
            self.assertEqual(bf.encrypt_block_be(bytes.fromhex(pt)).hex(), ct)

    def test_vce_word_order(self):
        """VCE loads each half-block little-endian: same cipher as BE on byte-swapped words."""
        bf = vcecrypto.Blowfish(bytes.fromhex("0123456789abcdef"))
        pt = bytes.fromhex("1111111111111111")
        swapped = pt[3::-1] + pt[7:3:-1]
        be = bf.encrypt_block_be(swapped)
        self.assertEqual(bf.encrypt_vce(pt), be[3::-1] + be[7:3:-1])
        self.assertEqual(bf.decrypt_vce(bf.encrypt_vce(b"ABCDEFGH12345678")), b"ABCDEFGH12345678")


class KeyDerivation(unittest.TestCase):
    def test_bn2hex(self):
        self.assertEqual(vcecrypto.bn2hex(0x0ABC), "0ABC")
        self.assertEqual(vcecrypto.bn2hex(0), "0")

    def test_key_is_first_8_bytes_of_hex(self):
        k = int("0A1B2C3D4E5F60718293", 16)
        self.assertEqual(vcecrypto.key_from_shared(k), bytes.fromhex("0A1B2C3D4E5F6071"))


def client_run(host, port, cid="aaaaaa", password="aaaaaa", uid=0x4000471):
    """Python model of the client: 0x12c5a0 hello, 0x12c180 key exchange, 0x12f760 records,
    then CODE_LOGIN (0x196e20) and parse the reply like 0x191068."""
    async def go():
        r, w = await asyncio.open_connection(host, port)
        w.write(vcecrypto.client_hello())
        buf = b""
        while True:
            buf += await r.read(4096)
            got = vcecrypto.parse_server_hello(buf)
            if got:
                break
        status, g, p, Y, used = got
        assert status == 0 and used == len(buf)
        hs = vcecrypto.ClientHandshake()
        xmsg, key = hs.answer(g, p, Y)
        w.write(xmsg)
        bf = vcecrypto.Blowfish(key)
        w.write(vcecrypto.seal(bf, vce.frame(world.encode_login_request(cid, password, uid))))
        await w.drain()
        rr, fr = vcecrypto.RecordReader(bf), vce.FrameReader()
        frames = []
        while not frames:
            frames = fr.feed(rr.feed(await r.read(4096)))
        w.close()
        return key, frames[0]
    return asyncio.run(go())


class EndToEnd(unittest.TestCase):
    def test_handshake_and_login(self):
        with tempfile.TemporaryDirectory() as d:
            world.LOG_DIR = d
            os.makedirs(os.path.join(d, "sessions"))
            box = {}

            async def main():
                srv = await asyncio.start_server(world.session, "127.0.0.1", 0)
                port = srv.sockets[0].getsockname()[1]
                loop = asyncio.get_running_loop()
                box["res"] = await loop.run_in_executor(None, client_run, "127.0.0.1", port)
                srv.close()
                await srv.wait_closed()
            asyncio.run(main())
            key, reply = box["res"]
            self.assertEqual(len(key), 8)
            rd = vce.Reader(reply)
            self.assertEqual((rd.u8(), rd.i32(), rd.u32()), (1, 0, 0x4000471))
            logs = os.listdir(os.path.join(d, "sessions"))
            with open(os.path.join(d, "sessions", logs[0]), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("session key " + key.hex(), text)
            self.assertIn("CODE_LOGIN", text)
            self.assertNotIn("'aaaaaa', 'uid'", text)            # password redacted


class LoginMessage(unittest.TestCase):
    def test_obfuscation_roundtrip(self):
        for seed in (0, 0x5A, 0xFF):
            for s in ("aaaaaa", "p a$ss", ""):
                self.assertEqual(world.obf_decode(world.obf_encode(s, seed)), s)

    def test_encoding_shape(self):
        # "a" -> 3 raw bytes (seed, 'a'^seed, 0^(seed+0x1f)) -> 4 chars
        self.assertEqual(len(world.obf_encode("a", 0)), 4)
        self.assertEqual(world.obf_encode("", 0), "@@@")   # 2 raw bytes (seed, NUL) -> 3 chars
        d = world.decode_login_request(world.encode_login_request("abc", "pw", 7))
        self.assertEqual((d["id"], d["password"], d["uid"], d["version"], d["one"], d["title"]),
                         ("abc", "pw", 7, 20003, 1, 1000201))

    def test_reply_layout(self):
        self.assertEqual(world.encode_login_reply(0, 5, 6, 7).hex(),
                         "01" "00000000" "00000005" "00000006" "00000007")
        self.assertEqual(world.encode_login_reply(-1).hex(), "01ffffffff")

    def test_login_opens_three_slots(self):
        # +8 -> net+0x52; 0x66d630 clamps to 1..3 and 0 meant one slot ("------" on slot 3)
        (rep,) = world.answer(world.encode_login_request("abc", "pw", 7), {})
        self.assertEqual(rep[:5].hex(), "0100000000")
        self.assertEqual(int.from_bytes(rep[13:17], "big"), 3)


@unittest.skipUnless(os.path.exists(os.environ.get("NOBU_RAM_DUMP", "")), "NOBU_RAM_DUMP not set")
class ClientConstants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.environ["NOBU_RAM_DUMP"], "rb") as fh:
            cls.ram = fh.read()

    def test_blowfish_tables(self):
        self.assertEqual(list(struct.unpack_from("<18I", self.ram, 0x416300)), vcecrypto.P_INIT)
        s = struct.unpack_from("<1024I", self.ram, 0x416350)
        self.assertEqual(list(s), sum(vcecrypto.S_INIT, []))

    def test_dh_defaults(self):
        self.assertEqual(self.ram[0x3DDF10:0x3DDF10 + 256].decode(), vcecrypto.DEFAULT_P)
        self.assertEqual(self.ram[0x4612C8:0x4612CA], b"2\0")

    def test_b64_alphabet(self):
        self.assertEqual(self.ram[0x3F16C0:0x3F1700].decode(), world.B64)


class CodeChar(unittest.TestCase):
    """Models of the client parsers for CODE_PONG / CODE_CHAR replies."""

    def setUp(self):
        from nobu import chars
        self.chars = chars
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))

    def tearDown(self):
        world.STORE = None
        self.tmp.cleanup()

    def test_pong_echoes_time(self):
        # live: 03 0000bdf4 04000471 ; 0x1911c8 needs len >= 5, reads u32 -> ping ring 0x3bda90
        (rep,) = world.answer(bytes.fromhex("030000bdf404000471"))
        self.assertEqual(rep.hex(), "030000bdf4")

    @staticmethod
    def parse_create(r):
        """0x1ac630 (list struct): str, str, u8, u8, u32, 5 x u8, u32, u8, 13 x u16."""
        names = (r.bytes(0x100).decode(), r.bytes(0x100).decode())
        r.u8(); r.u8(); r.u32()
        [r.u8() for _ in range(5)]
        r.u32(); r.u8()
        [r.u16() for _ in range(13)]
        return names

    @staticmethod
    def parse_summary(r):
        v = [r.u32(), r.u32(), r.u8(), r.u32(), r.u32(), r.u32(), r.u16(),
             r.bytes(0x100).decode(), r.bytes(0x100).decode()]
        [r.u8() for _ in range(6)]
        assert len(r.bytes(0xF)) == 0xF
        r.u16(); r.u8(); r.u16(); r.u8(); r.u8(); r.u16(); r.u8(); r.u8()
        [r.u8() for _ in range(1 + 7 + 1 + 4)]
        n = r.length()
        [r.u32() for _ in range(n)]
        [r.u8() for _ in range(5)]
        assert len(r.bytes(0xA)) == 0xA
        return v

    def parse_list(self, payload):
        """0x19c6e0: res, count < 4, bytes(0x16), per slot u32 cid [create + summary]."""
        r = vce.Reader(payload, 2)
        res, count = r.i32(), r.u32()
        self.assertEqual(res, 0)
        self.assertLess(count, 4)
        self.assertLessEqual(len(r.bytes(0x16)), 0x16)
        slots = []
        for _ in range(count):
            cid = r.u32()
            if cid:
                slots.append((cid, self.parse_create(r), self.parse_summary(r)))
            else:
                slots.append(None)
        self.assertTrue(r.done())
        return slots

    def test_empty_list(self):
        from nobu import clientmodel as cm
        (rep,) = world.answer(bytes.fromhex("050304000471"))
        d = cm.parse_list(rep)
        self.assertEqual((d["res"], d["count"], d["slots"]), (0, 3, [None, None, None]))

    def test_captured_bad_list_is_rejected(self):
        """A 213-byte list the server once sent (+0x6b..+0x6f written although A != 0)."""
        from nobu import clientmodel as cm
        bad = bytes.fromhex("05030000000000000003160001010302020304040506070808090a060b0c0d0d0e000000000000000106e38182e3818206e38182e38182041400000000000305060600000000000006000600080003000300030001000200020000000000000000000000010400047101000000000000000000000000000006e38182e3818206e38182e381820000000000000f0000000000000000000000000000000000000000000000000000000000000000000000000000000300000000000000000000000000000000000a0000000000000000000000000000")
        with self.assertRaises(cm.ParseError):
            cm.parse_list(bad)

    def test_create_then_list_and_load(self):
        from nobu import clientmodel as cm
        looks = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
        body = self.chars.write_create(dict(family="織田", given="信長", looks=looks))
        req = bytes([5, 1]) + vce.put_u32(0x4000471) + vce.put_u16(1) + body
        d = self.chars.decode_create_request(req)
        self.assertEqual((d["family"], d["given"], d["looks"], d["slot"]), ("織田", "信長", looks, 1))
        (rep,) = world.answer(req, {"uid": 0x4000471})
        cr = cm.parse_create_or_load(rep)
        self.assertEqual((cr["res"], cr["a1"], cr["a2"]), (0, 1, 1))
        self.assertEqual((cr["char"]["family"], cr["char"]["given"]), ("織田", "信長"))
        self.assertEqual(cr["char"]["w42"], looks[11:20])
        (lst,) = world.answer(bytes.fromhex("050304000471"))
        ls = cm.parse_list(lst)
        # request slot 1 = the first slot
        self.assertEqual(ls["slots"][1], None)
        self.assertEqual(ls["slots"][0]["cid"], 1)
        self.assertEqual(ls["slots"][0]["summary"]["A"], 1)
        self.assertEqual(ls["slots"][0]["entry"]["family"], "織田")
        self.assertIsNone(ls["extra"])
        # SCMD_CHAR_LOAD: 05 02 u32 uid u32 cid (net 0x18e6d8)
        (ld,) = world.answer(bytes.fromhex("0502") + vce.put_u32(0x4000471) + vce.put_u32(1),
                             {"uid": 0x4000471})
        lr = cm.parse_create_or_load(ld)
        self.assertEqual((lr["res"], lr["a1"]), (0, 1))                 # cid must be a listed cid
        self.assertIn(lr["a1"], [s["cid"] for s in ls["slots"] if s])


class StatTable(unittest.TestCase):
    def test_reply_to_live_request(self):
        # live: C>S 23 01 004f 06 06 (seq 0x4f, job 6, faction 6)
        (rep,) = world.answer(bytes.fromhex("2301004f0606"))
        r = vce.Reader(rep)
        self.assertEqual((r.u8(), r.u8(), r.u16()), (0x23, 1, 0x4F))
        rec = [r.u8() for _ in range(20)]                  # 0x1eb760: 20 x u8
        self.assertTrue(r.done())
        self.assertEqual(rec[:2], [6, 6])                  # job, faction -> table indexes (0x661264)
        self.assertEqual(len(rec[2:11]) + len(rec[11:]), 18)
        self.assertIn("job=6 faction=6", world.describe(bytes.fromhex("2301004f0606")))


class StatAllocation(unittest.TestCase):
    def test_every_stat_can_take_points(self):
        """0x66b898: '+' needs job[i] + faction[i] + allocated < 10 for each of the 5 stats."""
        from nobu import chars
        for job in range(1, 8):
            for faction in range(1, 15):
                (rep,) = world.answer(bytes([0x23, 1, 0, 1, job, faction]))
                rec = rep[4:]
                self.assertEqual(len(rec), 20)
                for i in range(5):
                    self.assertLess(rec[2 + i] + rec[11 + i], chars.STAT_CAP - 1)


class EnterWorld(unittest.TestCase):
    def test_cmd4_reply(self):
        # live: C>S 04 (empty) after the load reply; 0x1913f8 reads one u32 (res)
        rep = world.answer(b"\x04")[0]      # [1] is the 0x0B/02 zone-enter push
        r = vce.Reader(rep)
        self.assertEqual((r.u8(), r.i32()), (4, 0))
        self.assertTrue(r.done())

    def test_notify_layouts(self):
        # Re-derived from the world object's own cmd 0x0C branch (0x19f658); the earlier
        # layouts (from 0x191b58: name first, date u16 first) were wrong for this object.
        from nobu import chars, clientmodel as cm
        d = cm.parse_notify_field(chars.notify_world_date(1560, 5, 19, 12, 1))
        self.assertEqual((d["year"], d["month"], d["day"], d["hour"], d["weather"]),
                         (1560, 5, 19, 12, 1))
        self.assertEqual(chars.notify_world_date(1560, 5, 19, 12, 1).hex(), "0c050106180513" "0c")
        d = cm.parse_notify_field(chars.notify_field_info("Owari", 1, 2, 3))
        self.assertEqual((d["name"], d["pos"]), ("Owari", (1, 2, 3)))
        d = cm.parse_notify_field(chars.notify_world_time(7, 8))
        self.assertEqual((d["date"], d["time"]), (7, 8))


class ListEntryMapping(unittest.TestCase):
    def test_screen_ground_truth(self):
        """Live screen: level at +0x22, birth month/day at +0x28/+0x29, 13 u16 = Life, Ki,
        Attack, Defense, 5 stats, 4 elements."""
        from nobu import chars, clientmodel as cm
        looks = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
        c = dict(cid=1, uid=5, slot=0, family="a", given="b", looks=looks)
        e = cm.list_entry(cm.Arc(chars.write_list_entry(c), 0))
        self.assertEqual((e["b22"], e["b28"], e["b29"], e["b2a"], e["b2c"], e["b2b"]),
                         (1, 4, 20, 0, 6, 6))
        self.assertEqual(e["w36"][4:], [6, 6, 8, 3, 3, 3, 1, 2, 2])


class Cmd23Burst(unittest.TestCase):
    def test_empty_replies(self):
        for req, body in (("23030009", "00" * 19), ("23180008", "0000"), ("231a0005", "00")):
            (rep,) = world.answer(bytes.fromhex(req))
            self.assertEqual(rep.hex(), req + body)

    def test_quit_wait_is_answered(self):
        # a quit: 23 0d 000c; the u32 reply -> UI 0x48 ends the wait
        rep, push = world.answer(bytes.fromhex("230d000c"), {"uid": 0x04000471, "cid": 2})
        self.assertEqual(rep.hex(), "230d000c00000000")
        # then 0B 05 (parser 0x19f430: 3 raw u32), res (2nd) 0 -> UI 0x47 completes the quit waiter 0x1c
        self.assertEqual(push.hex(), "0b05" + "04000471" + "00000000" + "00000002")


class CharDelete(unittest.TestCase):
    def test_delete_answers_05_04_res(self):
        # reply = 05 04 u32 res (first-stage parser 0x1912e0 -> main 0x188e48 SCMD_CHAR_DELETE);
        # the 05 03 list we sent first hung the select screen
        from nobu import chars
        with tempfile.TemporaryDirectory() as tmp:
            world.STORE = chars.CharStore(os.path.join(tmp, "chars.json"))
            try:
                a = world.STORE.create(7, 0, "a", "b", [0] * 20)
                b = world.STORE.create(7, 1, "c", "d", [0] * 20)
                # another account cannot delete it
                (rep,) = world.answer(bytes([5, 4]) + vce.put_u32(8) + vce.put_u32(a["cid"]), {"uid": 8})
                self.assertEqual(rep.hex(), "0504ffffffff")
                self.assertEqual(len(world.STORE.list(7)), 2)
                (rep,) = world.answer(bytes([5, 4]) + vce.put_u32(7) + vce.put_u32(a["cid"]), {"uid": 7})
                self.assertEqual(rep.hex(), "050400000000")
                self.assertEqual([c["cid"] for c in chars.CharStore(world.STORE.path).list(7)], [b["cid"]])
            finally:
                world.STORE = None


class WorldLoadBurst(unittest.TestCase):
    """The burst after `04 00000000`."""

    def test_replies_echo_header_and_have_spec_sizes(self):
        from . import world
        cases = {
            "2a64000100": 12, "24010002": 14, "240f0003": 13, "241a0004": 5,
            "26020006": 8, "26010007": 4 + 1 + 550,
        }
        from . import world as _w
        for req, rep in {"0508": "05080000000000000000", "050c": "050c0000000000000000"}.items():
            self.assertEqual([x.hex() for x in _w.answer(bytes.fromhex(req), {"uid": 1})], [rep])
        for req, size in cases.items():
            p = bytes.fromhex(req)
            reps = world.answer(p, {"uid": 0x4000471})
            self.assertEqual(len(reps), 1, req)
            self.assertEqual(reps[0][:4], p[:4], req)
            self.assertEqual(len(reps[0]), size, req)


class ZoneEnter(unittest.TestCase):
    def test_enter_reply_then_zone_enter(self):
        from . import world
        reps = world.answer(bytes.fromhex("04"), {"uid": 1})
        self.assertEqual(reps[0].hex(), "0400000000")
        self.assertEqual(reps[1].hex(), "0b02" "01" "0001" "00" "00" "00")


class WorldSpawn(unittest.TestCase):
    """23 5a (world UI ready) -> CHAR_LOCATION + SCMD_CHAR_LOAD + CHAR_LOCATION; 05 05 moves.
    These pin the province-field start (NOBU_START_TOWN=0); TownStart covers the town start."""

    @classmethod
    def setUpClass(cls):
        cls._prev_start_town = os.environ.get("NOBU_START_TOWN")
        os.environ["NOBU_START_TOWN"] = "0"

    @classmethod
    def tearDownClass(cls):
        if cls._prev_start_town is None:
            os.environ.pop("NOBU_START_TOWN", None)
        else:
            os.environ["NOBU_START_TOWN"] = cls._prev_start_town

    UID = 0x4000471
    LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]   # male Smith, Oda

    def setUp(self):
        from nobu import chars
        self.chars = chars
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.env = {k: os.environ.pop(k) for k in list(os.environ)
                    if k in ("NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN", "NOBU_WORLD_PUSH", "NOBU_TOWN_QUESTS",
                             "NOBU_FIELD_NPCS", "NOBU_ELDER")}
        os.environ["NOBU_TOWN_QUESTS"] = "0"           # no quest givers in the field (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        body = chars.write_create(dict(family="織田", given="信長", looks=self.LOOKS))
        self.ctx = {"uid": self.UID}
        world.answer(bytes([5, 1]) + vce.put_u32(self.UID) + vce.put_u16(1) + body, self.ctx)
        (lst,) = world.answer(bytes([5, 3]) + vce.put_u32(self.UID), self.ctx)
        from nobu import clientmodel as cm
        self.cids = [s["cid"] for s in cm.parse_list(lst)["slots"] if s]
        (self.load,) = world.answer(bytes([5, 2]) + vce.put_u32(self.UID) + vce.put_u32(1), self.ctx)

    def tearDown(self):
        world.STORE = None
        for k in ("NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER"):
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def test_cmd23_sub5a_has_no_client_parser(self):
        from nobu import clientmodel as cm
        self.assertFalse(cm.cmd23_has_parser(0x5A))
        self.assertTrue(cm.cmd23_has_parser(1))
        self.assertIn("world UI ready", world.describe(bytes.fromhex("235a0013")))

    def test_load_reply_carries_the_position(self):
        from nobu import clientmodel as cm
        lr = cm.parse_create_or_load(self.load)
        c = lr["char"]                               # 0x1acce0: u8 area, u32 x, y, z first
        self.assertEqual((c["b0"], c["u4"], c["u8"], c["uc"]), (10,) + self.chars.FIELD_SPAWN[10])
        self.assertEqual(c["b36"], [4, 20, 0, 6, 6])

    def test_oda_enters_owari_and_spawns(self):
        from nobu import clientmodel as cm
        reps = world.answer(b"\x04", self.ctx)
        self.assertEqual(reps[1].hex(), "0b02" "0a" "000a" "000000")       # area 10, map 10 Owari
        pushes = world.answer(bytes.fromhex("235a0013"), self.ctx)
        self.assertEqual([p[:2].hex() for p in pushes], ["0b01", "0b0a", "0502", "0b01"])
        loc = cm.parse_notify_char(pushes[0])["records"]
        self.assertEqual(len(loc), 1)
        r = loc[0]
        self.assertEqual((r["uid"], r["cid"], r["area"]), (self.UID, 1, 10))  # uid == net+0x44
        self.assertEqual(r["pos"], self.chars.FIELD_SPAWN[10])
        self.assertEqual((r["level"], r["power"], r["job"], r["sex"]), (1, 6, 6, 0))
        self.assertEqual((r["family"], r["given"]), ("織田", "信長"))
        info = cm.parse_notify_char(pushes[1])["records"][0]     # 0x1a2500 parses to the end
        self.assertEqual((info["uid"], info["cid"], info["area"]), (self.UID, 1, 10))
        self.assertEqual(info["params"][8], 100)         # move speed -> run speed 10.0 (0x5f7de4)
        # +0x54 / +0x58 feed the appearance struct (P+0x58 / P+0x60 -> 0x1db0d0): must stay 0,
        # and the u32[3] at +0x88 must be sent in full (memcpy of 12 bytes to P+0xa4)
        self.assertEqual((info["w54"], info["w58"], info["a88"]), (0, 0, [0, 0, 0]))
        self.assertEqual(len(info["params"]), 15)
        self.assertIn("CHAR_INFO", world.describe(pushes[1], "S>C"))
        lr = cm.parse_create_or_load(pushes[2])
        self.assertEqual(lr["res"], 0)
        self.assertIn(lr["a1"], self.cids)          # 0x188980 looks the cid up in the list
        self.assertIn("CHAR_LOCATION", world.describe(pushes[0], "S>C"))

    def test_no_character_no_spawn(self):
        self.assertEqual(world.answer(bytes.fromhex("235a0013"), {"uid": 1}), [])

    def test_move_is_recorded_and_used_next_time(self):
        mv = self.chars.encode_move(101000, 3500, 96000, 90, 1)
        self.assertEqual(len(mv), 17)           # 05 05 + 3 x u32 + u16 + u8
        world.answer(b"\x04", self.ctx)
        self.assertEqual(world.answer(mv, self.ctx), [])      # no reply parser for sub 5
        self.assertIn("SCMD_CHAR_MOVE", world.describe(mv))
        c = world.STORE.find(self.UID, 1)
        self.assertEqual(c["pos"], [10, 101000, 3500, 96000, 90])
        from nobu import clientmodel as cm
        loc = cm.parse_notify_char(self.chars.spawn_pushes(c, self.UID)[0])["records"][0]
        self.assertEqual((loc["pos"], loc["dir"]), ((101000, 3500, 96000), 90))

    def test_zone_override_and_world_push(self):
        os.environ["NOBU_ZONE"] = "1"
        os.environ["NOBU_WORLD_PUSH"] = "1"
        try:
            reps = world.answer(b"\x04", self.ctx)
            self.assertEqual(reps[1][3:5].hex(), "0001")
            pushes = world.answer(bytes.fromhex("235a0014"), self.ctx)
            from nobu import clientmodel as cm
            self.assertEqual(len(pushes), 7)
            self.assertEqual(cm.parse_notify_field(pushes[6])["name"], "Owari")
            self.assertEqual(cm.parse_notify_char(pushes[0])["records"][0]["pos"],
                             self.chars.FIELD_SPAWN[1])
        finally:
            os.environ.pop("NOBU_ZONE")
            os.environ.pop("NOBU_WORLD_PUSH")


class CharInfoParams(unittest.TestCase):
    def test_params_knobs_and_nonself_branch(self):
        from nobu import chars, clientmodel as cm
        c = dict(cid=2, uid=1, slot=0, family="a", given="b", looks=[0] * 20)
        os.environ["NOBU_MOVE_SPEED"] = "35"
        try:
            rec = cm.parse_notify_char(chars.notify_char_info(
                [chars.char_info_record(c, 1, (10, 1, 2, 3, 0))]))["records"][0]
            self.assertEqual(rec["params"][8], 35)
            os.environ["NOBU_PARAMS"] = "00" * 8 + "64" + "00" * 6
            rec = cm.parse_notify_char(chars.notify_char_info(
                [chars.char_info_record(c, 0, (10, 1, 2, 3, 0))]))["records"][0]   # uid 0 branch
            self.assertEqual((rec["params"][8], rec["uid"]), (100, 0))
        finally:
            os.environ.pop("NOBU_MOVE_SPEED", None)
            os.environ.pop("NOBU_PARAMS", None)

    def test_appearance_from_create_looks(self):
        # live create: face 3, hair 5 (x2), sliders 100 / 50, voice? 1 (chars.appearance)
        from nobu import chars, clientmodel as cm
        looks = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
        c = dict(cid=2, uid=1, slot=0, family="a", given="b", looks=looks)
        rec = cm.parse_notify_char(chars.notify_char_info(
            [chars.char_info_record(c, 1, (10, 1, 2, 3, 0))]))["records"][0]
        self.assertEqual((rec["b68"], rec["b67"], rec["b56"], rec["b69"], rec["b6a"]),
                         (3, 5, 100, 50, 1))
        self.assertEqual((rec["w54"], rec["w58"]), (0, 0))      # nonzero = creature model / invisible
        npc = cm.parse_notify_char(chars.notify_char_info(
            [chars.char_info_record(c, 0, (10, 1, 2, 3, 0))]))["records"][0]   # no +0x69 / +0x6a
        self.assertEqual((npc["b68"], npc["b67"], npc["b56"]), (3, 5, 100))
        (slot,) = [s for s in cm.parse_list(chars.char_list_reply([c]))["slots"] if s]
        s = slot["summary"]
        self.assertEqual((s["b67"], s["b56"], s["b69"]), ([5, 3], 100, [50, 1]))


class MoveReport(unittest.TestCase):
    def test_live_move_y_word_carries_area(self):
        from nobu import chars
        # live 05 05 after CHAR_INFO: y word 659415 = 0x000a0fd7 (area 10, y 4055)
        p = bytes.fromhex("0505") + vce.put_u32(104346) + vce.put_u32(659415) + vce.put_u32(98049)             + vce.put_u16(40) + vce.put_u8(9)
        m = chars.decode_move(p)
        self.assertEqual((m["x"], m["y"], m["area"], m["z"], m["dir"], m["flag"]),
                         (104346, 4055, 10, 98049, 40, 9))
        self.assertEqual(chars.decode_move(bytes.fromhex("0505") + vce.put_u32(1) + vce.put_u32(68992)
                                           + vce.put_u32(2) + vce.put_u16(0) + vce.put_u8(0))["y"], 3456)


class MiscFeatures(unittest.TestCase):
    """News (2a 05), support reports (09 01) and inventory actions (24 03..07): nobu/misc.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("NOBU_DATA")
        os.environ["NOBU_DATA"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("NOBU_DATA", None)
        else:
            os.environ["NOBU_DATA"] = self.old
        self.tmp.cleanup()

    def test_news_request_live_frame(self):
        from . import clientmodel as cm, misc
        req = bytes.fromhex("2a05002600004e23")
        self.assertIn("version=20003", world.describe(req))
        reps = world.answer(req, {"uid": 1})
        self.assertEqual(reps[0].hex(), "2a050026" + "00" * 8)
        self.assertEqual(cm.parse_cmd2a(reps[0])["seq"], 0x26)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "news.json")))
        texts = [vce.Reader(r, 2).bytes().decode() for r in reps[1:]]
        self.assertTrue(any("Welcome" in t for t in texts))
        self.assertTrue(all(r[0] == 0x0D and r[1] in (5, 6) for r in reps[1:]))
        self.assertEqual(reps[-1][1], 5)                            # newest entry as a message box

    def test_news_file_is_used_newest_first(self):
        from . import misc
        with open(os.path.join(self.tmp.name, "news.json"), "w", encoding="utf-8") as fh:
            json.dump([{"date": "2026-01-01", "title": "old", "body": "a"},
                       {"date": "2030-09-30", "title": "新しい", "body": "b"}], fh)
        reps = world.answer(bytes.fromhex("2a05000100004e23"), {})
        texts = [vce.Reader(r, 2).bytes().decode() for r in reps[1:]]
        self.assertEqual(texts[1], "[2030-09-30] 新しい")

    def test_sub100_keeps_empty_reply(self):
        self.assertEqual([r.hex() for r in world.answer(bytes.fromhex("2a64000100"), {})],
                         ["2a640001" + "00" * 8])

    def test_bug_report_live_frame(self):
        from . import clientmodel as cm
        req = bytes.fromhex("0901000000030fe38182e38182e38182e38182e38182044c443a0a")
        ctx = {"uid": 0x4000471, "cid": 2, "char": {"cid": 2, "family": "ああ", "given": "ああ"}}
        (rep,) = world.answer(req, ctx)
        d = cm.parse_cmd9(rep)
        self.assertEqual((d["sub"], d["res"], d["ticket"], d["c"]), (1, 0, 0, 0))
        with open(os.path.join(self.tmp.name, "reports.jsonl"), encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual((rec["kind"], rec["kind_name"], rec["text"], rec["extra"], rec["cid"],
                          rec["name"]), (3, "bug report", "あああああ", "LD:\n", 2, "ああ ああ"))
        self.assertIn("bug report", world.describe(req))

    def test_support_followups_ack(self):
        from . import clientmodel as cm
        for req, sub in (("0902" + "00000000" + "00000000" + "03616263", 2),
                         ("0903" + "00000000" + "0161", 3), ("0904" + "00000000", 4),
                         ("0906" + "00000000", 6)):
            (rep,) = world.answer(bytes.fromhex(req), {})
            self.assertEqual(cm.parse_cmd9(rep), {"sub": sub, "res": 0})

    def test_inventory_actions(self):
        from . import clientmodel as cm
        for req, rep in (("2407003c", "2407003c00"), ("24030001" + "0102" + "00000005", "24030001"),
                         ("24040002" + "0304", "240400020304"), ("24050003" + "07", "2405000307"),
                         ("24060004" + "0201" + "00000001", "240600040201")):
            (got,) = world.answer(bytes.fromhex(req), {})
            self.assertEqual(got.hex(), rep)
            cm.parse_cmd24(got)
        # backpack / storage / 1a replies still come from chars.WORLDLOAD_EMPTY
        self.assertEqual(len(world.answer(bytes.fromhex("24010002"), {})[0]), 14)


class SkillSubs(unittest.TestCase):
    """cmd 0x26 subs 3..0xc (nobu/misc.py skill_reply); the live Swap Setup frame `26 0a 0047 00000000`."""

    def test_all_subs_parse(self):
        from . import clientmodel as cm
        cases = {"26030001" + "00000005": "2603000 1ff".replace(" ", ""),
                 "26040002" + "00000005": "26040002ff",
                 "26050003" + "02" + "00000105": "26050003" + "0200000105",
                 "26060004" + "02": "2606000402", "26070005" + "01": "26070005",
                 "26080006" + "00000001": "260800060000", "26090007" + "03": "26090007" + "00000000" + "03",
                 "260a0047" + "00000000": "260a0047" + "00000000" + "00000000",
                 "260b0008" + "01": "260b000801", "260c0009" + "010203040506": "260c0009010203040506"}
        store = type("S", (), {"n": 0, "save": lambda self: setattr(self, "n", self.n + 1)})()
        ctx = {"char": {"cid": 2}}
        for req, rep in cases.items():
            got = misc_answer(bytes.fromhex(req), ctx, store)
            self.assertEqual(got.hex(), rep, req)
            cm.parse_cmd26(got)
            self.assertTrue(world.describe(bytes.fromhex(req)).startswith("SKILL"))
        self.assertEqual(ctx["char"]["skills"], {"bar": {}, "swap": [0, 0, 0], "config": [1, 2, 3, 4, 5, 6]})
        self.assertGreater(store.n, 0)

    def test_standby_list_unchanged(self):
        self.assertEqual(len(world.answer(bytes.fromhex("26010007"), {})[0]), 4 + 1 + 550)


def misc_answer(p, ctx, store):
    from . import misc
    (rep,) = misc.answer(p, ctx, store)
    return rep


class TownStart(unittest.TestCase):
    def test_oda_starts_in_nagoya(self):
        from nobu import chars
        c = {"looks": [4, 20, 0, 6, 6]}
        self.assertEqual(chars.home_zone(c), 110)
        self.assertEqual(chars.area_for(110), 31)              # place index 0.146.31 Nagoya (zones.area_of)
        self.assertEqual(chars.char_position(c)[:4], (110, 103713, 3200, 112594))


class NpcDebug(unittest.TestCase):
    """NOBU_DEBUG_NPC=1: uid-0 CHAR_INFO after the spawn, 25 01 target set, 28 01 talk."""
    UID = 0x4000471
    LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]   # male Smith, Oda
    KNOBS = ("NOBU_DEBUG_NPC", "NOBU_NPC_CID", "NOBU_NPC_DX", "NOBU_NPC_DZ", "NOBU_NPC_DIR",
             "NOBU_NPC_TEXT", "NOBU_NPC_POPUP", "NOBU_NPC_TALK_ON_TARGET", "NOBU_ZONE", "NOBU_AREA",
             "NOBU_SPAWN", "NOBU_WORLD_PUSH", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_QUESTS",
             "NOBU_SHOPS", "NOBU_TALK_RANGE", "NOBU_GUILD", "NOBU_CRAFTING", "NOBU_BANK", "NOBU_REPAIR", "NOBU_DOCTOR", "NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER", "NOBU_LIFE_SHRINE", "NOBU_TOWN_GUARDS",
             "NOBU_TOWN_SPOTS")

    def setUp(self):
        from nobu import chars, clientmodel, npc
        self.chars, self.cm, self.npc = chars, clientmodel, npc
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in self.KNOBS}
        os.environ["NOBU_DEBUG_NPC"] = "1"
        os.environ["NOBU_SHOPS"] = "0"          # no Merchant: these tests count the spawn pushes
        os.environ["NOBU_GUILD"] = "0"          # no Hall Master either
        os.environ["NOBU_LIFE_SHRINE"] = "0"    # nor the shrine (lifecycle.py)
        os.environ["NOBU_CRAFTING"] = "0"       # no workshop keeper either
        os.environ["NOBU_BANK"] = "0"           # no money changer / storekeeper either
        os.environ["NOBU_REPAIR"] = "0"         # nor the repairer (nobu/repair.py)
        os.environ["NOBU_DOCTOR"] = "0"         # nor the doctor (nobu/doctor.py)
        os.environ["NOBU_TOWN_QUESTS"] = "0"   # nor the quest givers (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        os.environ["NOBU_TOWN_GUARDS"] = "0"    # nor the gate guards (npc.guard_spawn)
        os.environ["NOBU_TOWN_SPOTS"] = "0"     # the Guide on NOBU_NPC_DX / DZ offsets
        self.ctx = {"uid": self.UID}
        self.make_char(self.LOOKS)

    def make_char(self, looks):
        body = self.chars.write_create(dict(family="織田", given="信長", looks=looks))
        world.answer(bytes([5, 1]) + vce.put_u32(self.UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(self.UID) + vce.put_u32(1), self.ctx)
        world.answer(b"\x04", self.ctx)

    def tearDown(self):
        world.STORE = None
        for k in self.KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def spawn(self):
        return world.answer(bytes.fromhex("235a0013"), self.ctx)

    def test_npc_spawns_after_the_player(self):
        pushes = self.spawn()
        # player spawn, the guide, then the quest Overseer (quests.world_ready; no 34 01 without quests)
        self.assertEqual([p[:2].hex() for p in pushes], ["0b01", "0b0a", "0502", "0b01", "0b0a", "0b0a", "1717"])
        mine = self.cm.parse_notify_char(pushes[1])["records"][0]
        (rec,) = self.cm.parse_notify_char(pushes[4])["records"]      # 0x1a2500 to the last byte
        self.assertEqual((rec["uid"], rec["cid"]), (0, 0xFEA00001))
        self.assertEqual(self.cm.npc_ui_event(rec, 0xA), 0xD4)       # 0x189ac8 -> UI 0x1d2df0
        self.assertTrue(self.cm.npc_actor_created(rec))              # job byte 6 -> 0x60c220
        self.assertEqual((rec["family"], rec["given"]), ("Guide", ""))
        self.assertEqual(rec["area"], mine["area"])                  # same area or UI 0xc5 drops it
        x, y, z = mine["pos"]
        self.assertEqual(rec["pos"], (x + 300, y, z))
        self.assertEqual(rec["dir"], 180)
        # appearance copied from the visible player record
        self.assertEqual((rec["b68"], rec["b67"], rec["b56"]), (1, 2, 50))  # own face / hair / build
        for k in ("power", "job", "sex", "level", "w54", "w58", "b5a", "b5b", "w5c", "b60"):
            self.assertEqual(rec[k], mine[k], k)
        self.assertEqual(rec["b6b"], 2)                              # uid-0 branch; talk-only (0x2733a0)
        self.assertEqual(self.cm.action_hint(rec), 0xD)             # "○Look"; ○ still sends 28 01
        self.assertIn(0xFEA00001, self.ctx["npcs"])

    def test_off_by_default(self):
        os.environ.pop("NOBU_DEBUG_NPC")
        self.assertEqual(len(self.spawn()), 4)

    def test_samurai_player_gives_a_person_npc(self):
        self.ctx = {"uid": self.UID}
        world.STORE = self.chars.CharStore(os.path.join(self.tmp.name, "c2.json"))
        self.make_char([4, 20, 1, 1, 6] + self.LOOKS[5:])          # female Samurai: job 1
        rec = self.cm.parse_notify_char(self.spawn()[4])["records"][0]
        self.assertEqual((rec["job"], rec["sex"]), (6, 1))           # job 1 = the 0x21c2a0 path
        self.assertTrue(self.cm.npc_actor_created(rec))

    def test_bad_cid_rejected(self):          # 0xfe top byte and the NPC range are required
        os.environ["NOBU_NPC_CID"] = "0xfe000001"
        with self.assertRaises(ValueError):
            self.npc.npc_cid()
        os.environ["NOBU_NPC_CID"] = "0x00a00001"               # not 0xfe: UI 0xa3, never drawn
        with self.assertRaises(ValueError):
            self.npc.npc_cid()

    def test_target_set_and_talk(self):
        os.environ["NOBU_QUESTS"] = "0"         # the plain NPC line; quest talk: test_quests.py
        self.spawn()
        cid = 0xFEA00001
        req = bytes.fromhex("25010031") + vce.put_u32(cid)          # builder 0x1c9240
        self.assertIn("SCMD_TARGET_SET", world.describe(req))
        (rep,) = world.answer(req, self.ctx)
        d = self.cm.parse_cmd25(rep)
        self.assertEqual((d["sub"], d["seq"], d["res"]), (1, 0x31, 0))
        t = d["target"]
        self.assertEqual((t["cid"], t["family"], t["given"], t["level"]), (cid, "Guide", "", 1))
        self.assertEqual(t["pos"], tuple(self.ctx["npcs"][cid]["pos"][1:4]))
        self.assertEqual(t["dir"], 180)
        self.assertEqual(self.ctx["target"], cid)
        talk = bytes.fromhex("28010032")                            # builder 0x1c90e0
        self.assertIn("talk", world.describe(talk))
        say, box = world.answer(talk, self.ctx)
        s = self.cm.parse_notify_chat(say)
        self.assertEqual(s["sub"], 6)                               # named system line (npc.notify_npc_say)
        self.assertTrue(s["msg"].startswith("Guide: Welcome"))
        b = self.cm.parse_notify_chat(box)
        self.assertEqual((b["sub"], "Guide: " + b["msg"]), (5, s["msg"]))

    def test_talk_text_knobs(self):
        os.environ["NOBU_NPC_TEXT"] = "Hello."
        os.environ["NOBU_NPC_POPUP"] = "0"
        os.environ["NOBU_NPC_TALK_ON_TARGET"] = "1"
        self.spawn()
        reps = world.answer(bytes.fromhex("25010001") + vce.put_u32(0xFEA00001), self.ctx)
        self.assertEqual([r[:2].hex() for r in reps], ["2501", "0d06"])
        self.assertEqual(reps[1], npc.notify_npc_say(0xFEA00001, "Hello.", name="Guide"))

    def test_unknown_target_and_clear(self):
        self.spawn()
        (rep,) = world.answer(bytes.fromhex("25010005") + vce.put_u32(0x12345), self.ctx)
        self.assertEqual(rep.hex(), "25010005ffffffff")
        self.assertEqual(self.cm.parse_cmd25(rep)["res"], -1)
        os.environ["NOBU_TALK_RANGE"] = "10"                        # nobody within reach
        self.assertEqual(world.answer(bytes.fromhex("28010006"), self.ctx), [])   # no NPC target
        self.assertEqual(world.answer(bytes.fromhex("25020007"), self.ctx), [bytes.fromhex("25020007")])
        self.assertIsNone(self.ctx["target"])
        self.assertEqual(world.answer(bytes.fromhex("28020008"), self.ctx), [])   # object use: log
        self.assertIn("use object", world.describe(bytes.fromhex("28020008")))

    def test_player_target_uses_the_store(self):
        self.spawn()
        (rep,) = world.answer(bytes.fromhex("25010009") + vce.put_u32(1), self.ctx)
        t = self.cm.parse_cmd25(rep)["target"]
        self.assertEqual((t["cid"], t["family"], t["given"]), (1, "織田", "信長"))
