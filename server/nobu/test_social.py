"""Chat / find / friends: python -m unittest nobu.test_social -v"""
import os
import tempfile
import unittest

from nobu import chars, social, vce, world

ME = dict(cid=2, uid=0x04000471, slot=0, family="ああ", given="ああ", looks=[0] * 20)


class Chat(unittest.TestCase):
    def setUp(self):
        social.SESSIONS.clear()

    def test_live_say_line_is_echoed_as_notify_say(self):
        # captured: C>S 23 08 <seq 31> 12
        req = bytes.fromhex("2308001f12") + "/s/あああああ".encode("utf-8")
        self.assertEqual(req[4], 0x12)
        d = social.decode_chat(req)
        self.assertEqual((d["seq"], d["channel"], d["text"]), (31, "s", "あああああ"))
        ctx = {"char": dict(ME), "cid": 2, "uid": ME["uid"]}
        reps = world.answer(req, ctx)
        text = "あああああ".encode("utf-8")
        self.assertEqual(reps, [bytes.fromhex("0d010100000002") + bytes([len(text)]) + text])
        # the record parses like 0x1a62a0: u8 kind, u32 cid, str msg (max 0x191)
        r = vce.Reader(reps[0], 2)
        self.assertEqual((r.u8(), r.u32(), r.bytes(0x191).decode("utf-8")), (1, 2, "あああああ"))
        self.assertTrue(r.done())
        self.assertIn("CHAT say", world.describe(req))
        self.assertIn("NOTIFY_CHAT SAY", world.describe(reps[0], "S>C"))

    def test_other_channels_are_not_echoed(self):
        self.assertEqual(social.parse_line("/bt_s/x"), ("bt_s", "x"))
        req = bytes.fromhex("23080001") + vce.put_bytes(b"/n/party")
        self.assertEqual(world.answer(req, {"cid": 2}), [])

    def test_target_clear_is_acknowledged(self):
        self.assertEqual(world.answer(bytes.fromhex("2502001e"), {}), [bytes.fromhex("2502001e")])

    def test_friendship_state_empty(self):
        # war.py answers 23 05 with the realm state (test_war); NOBU_WAR=0 keeps the empty reply
        old = os.environ.get("NOBU_WAR")
        os.environ["NOBU_WAR"] = "0"
        try:
            self.assertEqual(world.answer(bytes.fromhex("23050024"), {}),
                             [bytes.fromhex("23050024") + b"\x00" * 5])
        finally:
            if old is None:
                os.environ.pop("NOBU_WAR", None)
            else:
                os.environ["NOBU_WAR"] = old

    def test_registry_and_broadcast(self):
        got = []
        a = {"char": dict(ME), "zone": 10, "push": got.append}
        b = {"char": dict(ME, cid=3), "zone": 10, "push": got.append}
        social.register("a", a)
        social.register("b", b)
        self.assertEqual(social.broadcast(b"x", zone=10, exclude=a), 1)
        self.assertEqual(got, [b"x"])
        social.unregister("a")
        self.assertEqual(len(social.online()), 1)

    def test_find_all_lists_the_requester(self):
        ctx = {"char": dict(ME), "cid": 2}
        reps = world.answer(bytes.fromhex("2312002200"), ctx)
        self.assertEqual(reps[0], social.notify_system("Online: 1"))
        self.assertEqual(reps[1], social.notify_system("ああ ああ"))


class Friends(unittest.TestCase):
    def test_lookup_add_get(self):
        with tempfile.TemporaryDirectory() as d:
            st = chars.CharStore(os.path.join(d, "chars.json"))
            me = st.create(1, 0, "ああ", "ああ", [0] * 20)
            him = st.create(2, 0, "いい", "うう", [0] * 20)
            ctx = {"char": me, "cid": me["cid"], "uid": 1}
            # captured: 05 10 09
            miss = social.answer(bytes.fromhex("051009") + "あああ".encode("utf-8"), ctx, st)
            self.assertEqual(miss[0][:6], bytes.fromhex("0510ffffffff"))
            hit = social.answer(bytes.fromhex("051006") + "いい".encode("utf-8"), ctx, st)[0]
            r = vce.Reader(hit, 2)
            self.assertEqual((r.i32(), r.u32(), r.u32()), (0, 2, him["cid"]))
            self.assertEqual((r.bytes(0x20).decode(), r.bytes(0x20).decode()), ("いい", "うう"))
            add = (bytes.fromhex("0509") + vce.put_u32(2) + vce.put_u32(him["cid"])
                   + vce.put_bytes("いい".encode()) + vce.put_bytes("うう".encode()))
            self.assertEqual(social.answer(add, ctx, st), [bytes.fromhex("050900000000")])
            got = social.answer(bytes.fromhex("0508"), ctx, st)[0]
            r = vce.Reader(got, 2)
            self.assertEqual((r.u32(), r.u32(), r.u32(), r.u32()), (0, 1, 2, him["cid"]))
            self.assertEqual(chars.CharStore(st.path).find(1, me["cid"])["friends"][0]["given"], "うう")

    def test_block_flow_and_self_block_refused(self):
        with tempfile.TemporaryDirectory() as d:
            st = chars.CharStore(os.path.join(d, "chars.json"))
            me = st.create(1, 0, "ああ", "ああ", [0] * 20)
            him = st.create(2, 0, "いい", "うう", [0] * 20)
            ctx = {"char": me, "cid": me["cid"], "uid": 1}
            # 05 0f 06 = the player's own name -> refused (not found)
            own = social.answer(bytes.fromhex("050f06e38182e38182"), ctx, st)[0]
            self.assertEqual(own, bytes.fromhex("050f") + bytes.fromhex("ffffffff") + bytes(12) + bytes(2))
            hit = social.answer(bytes.fromhex("050f06") + "いい".encode(), ctx, st)[0]
            r = vce.Reader(hit, 2)
            self.assertEqual([r.u32() for _ in range(4)], [0, 2, him["cid"], 0])
            self.assertEqual((r.bytes(0x20).decode(), r.bytes(0x20).decode()), ("いい", "うう"))
            self.assertTrue(r.done())
            add = (bytes.fromhex("050d") + vce.put_u32(2) + vce.put_u32(him["cid"])
                   + vce.put_bytes("いい".encode()) + vce.put_bytes("うう".encode()))
            self.assertEqual(social.answer(add, ctx, st), [bytes.fromhex("050d00000000")])
            selfadd = (bytes.fromhex("050d") + vce.put_u32(1) + vce.put_u32(me["cid"])
                       + vce.put_bytes(b"") + vce.put_bytes(b""))
            self.assertEqual(social.answer(selfadd, ctx, st), [bytes.fromhex("050dffffffff")])
            got = social.answer(bytes.fromhex("050c"), ctx, st)[0]
            r = vce.Reader(got, 2)
            self.assertEqual([r.u32() for _ in range(5)], [0, 1, 2, him["cid"], 0])
            dele = bytes.fromhex("050e") + vce.put_u32(him["cid"])
            self.assertEqual(social.answer(dele, ctx, st), [bytes.fromhex("050e00000000")])
            self.assertEqual(chars.CharStore(st.path).find(1, me["cid"])["ignores"], [])
            # with the list empty again world.py answers the empty IGNORE_GET
            self.assertEqual(world.answer(bytes.fromhex("050c"), {}), [bytes.fromhex("050c0000000000000000")])

    def test_friend_delete(self):
        with tempfile.TemporaryDirectory() as d:
            st = chars.CharStore(os.path.join(d, "chars.json"))
            me = st.create(1, 0, "ああ", "ああ", [0] * 20)
            me["friends"] = [dict(uid=2, cid=9, family="x", given="y")]
            ctx = {"char": me, "cid": me["cid"]}
            self.assertEqual(social.answer(bytes.fromhex("050a") + vce.put_u32(2), ctx, st),
                             [bytes.fromhex("050a00000000")])
            self.assertEqual(me["friends"], [])

    def test_empty_friend_list_still_answered_by_world(self):
        self.assertEqual(world.answer(bytes.fromhex("0508"), {}), [bytes.fromhex("05080000000000000000")])


if __name__ == "__main__":
    unittest.main()


class Profile(unittest.TestCase):
    def test_bio_set_then_get(self):
        with tempfile.TemporaryDirectory() as d:
            st = chars.CharStore(os.path.join(d, "chars.json"))
            me = st.create(1, 0, "ああ", "ああ", [0] * 20)
            ctx = {"char": me, "cid": me["cid"], "uid": 1}
            # captured: 23 14 <seq 22> 09
            req = bytes.fromhex("2314001609") + "あああ".encode()
            rep = social.answer(req, ctx, st)
            self.assertEqual(rep, [bytes.fromhex("23140016 00000000 09").replace(b" ", b"")
                                   + "あああ".encode()])
            self.assertEqual(chars.CharStore(st.path).find(1, me["cid"])["profile"]["bio"], "あああ")
            got = social.answer(bytes.fromhex("23160030") + vce.put_u32(me["cid"]), {}, st)
            self.assertEqual(got[1], social.profile_reply(0x17, 0x30, "あああ"))
            self.assertEqual(got[0], bytes.fromhex("231600300000000000"))
            self.assertIn("PROFILE set bio", world.describe(req))

    def test_status_is_stored_without_reply(self):
        with tempfile.TemporaryDirectory() as d:
            st = chars.CharStore(os.path.join(d, "chars.json"))
            me = st.create(1, 0, "ああ", "ああ", [0] * 20)
            ctx = {"char": me, "cid": me["cid"]}
            self.assertEqual(social.answer(bytes.fromhex("230e00190500000004"), ctx, st), [])
            self.assertEqual(chars.CharStore(st.path).find(1, me["cid"])["status"], {"5": 4})
            self.assertIn("kind=5 value=4", world.describe(bytes.fromhex("230e00190500000004")))


class PackedY(unittest.TestCase):
    def test_old_packed_y_is_unpacked(self):
        c = dict(ME, pos=[10, 100460, 659382, 95716, 0])     # 659382 = 10 << 16 | 4022
        self.assertEqual(chars.char_position(c), (10, 100460, 659382 & 0xFFFF, 95716, 0))
        self.assertEqual(chars.char_position(dict(ME, pos=[10, 1, 3455, 2, 0]))[2], 3455)
