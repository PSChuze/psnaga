"""Houses, cmd 0x1b and the 18 01 push (nobu/house.py): python -m unittest nobu.test_house -v

The parsers below model the client's read halves: 0x19aa90 (1b replies), 0x19a740 (18 01), 0x1aa500 (house state =
0x1aa240 layout, 0x1a8780 info, 0x1a99d0 barn list)."""
import os
import tempfile
import unittest

from nobu import chars, house, items, shops, vce

SMITH = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
KNOBS = ("NOBU_HOUSE", "NOBU_HOUSE_STRICT", "NOBU_HOUSE_TRACK", "NOBU_HOUSE_STARTER", "NOBU_HOUSE_VIEW",
         "NOBU_HOUSE_PUSH", "NOBU_HOUSE_ENTER_PUSH", "NOBU_HOUSE_ANY_ITEM", "NOBU_HOUSE_DEBUG", "NOBU_HOUSE_CHAT")


def m_str(r, maxlen):
    b = r.bytes()
    assert len(b) < maxlen, (len(b), maxlen)        # the client's max counts the NUL
    return b.decode("utf-8")


def m_layout(r):
    a, b, w = r.u16(), r.u16(), r.u16()
    assert a <= 10 and b <= 510
    big = [[r.u16(), r.u8(), r.u8(), r.u8(), r.u8(), r.u16()] for _ in range(a)]
    small = [[r.u16(), r.u8(), r.u8(), r.u8(), r.u8()] for _ in range(b)]
    return dict(wall=w, big=big, small=small)


def m_state(r):
    layout = m_layout(r)
    info = dict(kind=r.u8(), name=m_str(r, 0x3d), owner=m_str(r, 0x21), grade=r.u16(), access=r.u8(), x=r.u32())
    n, _ = r.u16(), r.u16()
    assert n <= 100                                     # rows that fit the object (0x68f780: index < 100)
    barn = [[r.u16(), r.u16()] for _ in range(n)]
    assert all(cnt < 999 for _, cnt in barn)
    return dict(layout=layout, info=info, barn=barn)


def m_reply(p):
    """0x19aa90: u8 sub (< 0xe), u16 seq, then per sub."""
    r = vce.Reader(p, 1)
    sub, seq = r.u8(), r.u16()
    assert p[0] == 0x1B and sub < 0xE
    d = dict(sub=sub, seq=seq, res=r.i32())
    if sub == 1 and d["res"] >= 0:
        d["u32"] = r.u32()
        d.update(m_state(r))
    elif sub == 4:
        d.update(item=r.u32(), count=r.u16(), flag=r.u8())
    elif sub == 9:
        a, b = r.u8(), r.u8()
        d["entries"] = [dict(cid=r.u32(), uid=r.u32(), z=r.u32(), name=m_str(r, 0x21)) for _ in range(a + b)]
        d["a"], d["b"] = a, b
    assert r.done(), r.rest().hex()
    return d


def m_enter(p):
    """0x19a740: 18 <u8 sub == 1> <u16 seq> i32 res, u32, state."""
    r = vce.Reader(p, 1)
    assert p[0] == 0x18 and r.u8() == 1
    r.u16()
    d = dict(res=r.i32(), u32=r.u32())
    d.update(m_state(r))
    assert r.done()
    return d


def layout_bytes(big=(), small=(), wall=0):
    out = vce.put_u16(len(big)) + vce.put_u16(len(small)) + vce.put_u16(wall)
    for e in big:
        out += vce.put_u16(e[0]) + bytes(e[1:5]) + vce.put_u16(e[5])
    for e in small:
        out += vce.put_u16(e[0]) + bytes(e[1:5])
    return out


def req(sub, body=b"", seq=0x40):
    return bytes([0x1B, sub]) + vce.put_u16(seq) + body


class Env(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        from nobu import world
        self.world = world
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.ctx = {"uid": 0x4000471}
        body = chars.write_create(dict(family="織田", given="信長", looks=SMITH))
        world.answer(bytes([5, 1]) + vce.put_u32(0x4000471) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(0x4000471) + vce.put_u32(1), self.ctx)
        self.c = self.ctx["char"]
        house.state(self.c, world.store)                       # the house (and its starter pieces)

    def tearDown(self):
        self.world.STORE = None
        self.tmp.cleanup()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def ask(self, p):
        return self.world.answer(p, self.ctx)

    def say(self, text):
        return self.ask(bytes([0x23, 8]) + vce.put_u16(3) + vce.put_bytes(text.encode("utf-8")))

    def inv(self):
        return items.inventory(self.c)

    def held(self, iid):
        return sum(int(it.get("count", 1)) for it in self.inv()["items"] if it["id"] == iid)

    def reload(self):
        """A fresh session on the same store (what a relog sees)."""
        self.world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.ctx = {"uid": 0x4000471}
        self.world.answer(bytes([5, 2]) + vce.put_u32(0x4000471) + vce.put_u32(1), self.ctx)
        self.c = self.ctx["char"]


class Get(Env):
    def test_get_is_a_parsable_house(self):
        (rep,) = self.ask(req(1))
        d = m_reply(rep)
        self.assertEqual((d["sub"], d["seq"], d["res"], d["u32"]), (1, 0x40, 0, self.c["cid"]))
        self.assertEqual(d["layout"], dict(wall=0, big=[], small=[]))
        self.assertEqual((d["info"]["name"], d["info"]["owner"], d["info"]["access"]), ("織田 信長's House", "織田信長", 1))
        self.assertEqual(d["barn"], [])
        self.c["house"]["name"] = "あああ"                       # a name the player chose is kept
        self.assertEqual(house.state(self.c)["name"], "あああ")
        self.assertEqual(house.default_name(dict(family="A" * 40, given="B" * 40))[-8:], "'s House")
        self.assertLessEqual(len(house.default_name(dict(family="A" * 40, given="B" * 40))), house.NAME_MAX - 1)
        # the starter furniture is in the backpack (it goes into the barn with 1b 04 first)
        for iid, n in house.STARTER_BARN:
            self.assertEqual(self.held(iid), n)

    def test_market_fallback_with_house_off(self):
        os.environ["NOBU_HOUSE"] = "0"
        (rep,) = self.ask(req(1))
        self.assertEqual(rep, shops.market_reply(req(1)))

    def test_describe(self):
        self.assertIn("HOUSE request BARN idx=2 count=1 dir=1",
                      house.describe(req(4, vce.put_u16(2) + vce.put_u16(1) + b"\x01")))
        (rep,) = self.ask(req(1))
        self.assertIn("HOUSE reply GET", self.world.describe(rep, "S>C"))
        self.assertIn("18 01 push", house.describe(house.enter_push(self.c, house.state(self.c)), "S>C"))


class Barn(Env):
    def deposit(self, iid, n=1):
        it = next(i for i in self.inv()["items"] if i["id"] == iid)
        return self.ask(req(4, vce.put_u16(it["idx"]) + vce.put_u16(n) + b"\x01"))

    def test_deposit_and_withdraw(self):
        rep, push, line = self.deposit(4688, 2)                 # Andon Lamp x2
        d = m_reply(rep)
        self.assertEqual((d["res"], d["item"], d["count"], d["flag"]), (0, 4688, 2, 1))
        self.assertEqual(push[:2], b"\x30\x04")
        self.assertEqual(self.held(4688), 0)
        self.assertEqual(house.state(self.c)["barn"], [[4688, 2]])
        self.deposit(4685)                                      # Hearth
        self.assertEqual(self.ctx["house_view"], [[4688, 2], [4685, 1]])
        rep, push, line = self.ask(req(4, vce.put_u16(1) + vce.put_u16(1) + b"\x00"))   # barn row 1
        d = m_reply(rep)
        self.assertEqual((d["res"], d["item"], d["count"], d["flag"]), (0, 4685, 1, 0))
        self.assertEqual(push[:2], b"\x30\x03")
        self.assertEqual((self.held(4685), house.state(self.c)["barn"]), (1, [[4688, 2]]))
        (rep,) = self.ask(req(1))
        self.assertEqual(m_reply(rep)["barn"], [[4688, 2]])     # persisted server side

    def test_refusals(self):
        weapon = next(i for i in self.inv()["items"] if i.get("pos") is None and i.get("type") != 89)
        rep, line = self.ask(req(4, vce.put_u16(weapon["idx"]) + vce.put_u16(1) + b"\x01"))
        self.assertEqual(m_reply(rep)["res"], -1)
        self.assertIn(b"Only furniture", line)
        rep, line = self.ask(req(4, vce.put_u16(0) + vce.put_u16(1) + b"\x00"))       # empty client barn view
        self.assertEqual(m_reply(rep)["res"], -1)

    def test_view_starts_empty_each_world_load(self):
        self.deposit(4688, 1)
        self.world.world_ready_pushes(self.ctx)
        self.assertNotIn("house_view", self.ctx)
        rep, line = self.ask(req(4, vce.put_u16(0) + vce.put_u16(1) + b"\x00"))
        self.assertEqual(m_reply(rep)["res"], -1)              # the client lost its rows with the model
        os.environ["NOBU_HOUSE_VIEW"] = "full"
        self.ctx.pop("house_view", None)
        rep, push, line = self.ask(req(4, vce.put_u16(0) + vce.put_u16(1) + b"\x00"))
        self.assertEqual(m_reply(rep)["item"], 4688)

    def test_take_by_chat(self):
        self.deposit(4688, 2)
        self.world.world_ready_pushes(self.ctx)
        out = self.say("house take 4688 1")
        self.assertEqual(out[0][:2], b"\x30\x03")
        self.assertEqual((self.held(4688), house.barn_count(house.state(self.c), 4688)), (1, 1))


class Layout(Env):
    def test_save_persists_and_moves_pieces(self):
        for iid in (4688, 4682):
            it = next(i for i in self.inv()["items"] if i["id"] == iid)
            self.ask(req(4, vce.put_u16(it["idx"]) + vce.put_u16(2) + b"\x01"))
        big = [[4682, 3, 4, 1, 0, 7]]
        small = [[4688, 10, 12, 2, 0]]
        (rep,) = self.ask(req(3, vce.put_bytes(layout_bytes(big, small, wall=5))))
        self.assertEqual(m_reply(rep)["res"], 0)
        h = house.state(self.c)
        self.assertEqual(h["barn"], [[4688, 1], [4682, 1]])      # one of each is placed now
        self.assertEqual(self.ctx["house_view"], [[4688, 1], [4682, 1]])
        self.reload()
        (rep,) = self.ask(req(1))
        d = m_reply(rep)
        self.assertEqual(d["layout"], dict(wall=5, big=big, small=small))
        # stow everything: the pieces come back to the barn
        (rep,) = self.ask(req(3, vce.put_bytes(layout_bytes())))
        self.assertEqual(house.state(self.c)["barn"], [[4688, 2], [4682, 2]])

    def test_check_does_not_save_and_strict_refuses(self):
        (rep,) = self.ask(req(2, vce.put_bytes(layout_bytes(small=[[4688, 1, 1, 0, 0]]))))
        self.assertEqual(m_reply(rep)["res"], 0)
        self.assertEqual(house.state(self.c)["layout"]["small"], [])
        os.environ["NOBU_HOUSE_STRICT"] = "1"
        rep, line = self.ask(req(3, vce.put_bytes(layout_bytes(small=[[4688, 1, 1, 0, 0]]))))
        self.assertEqual(m_reply(rep)["res"], -1)
        self.assertIn(b"not saved", line)

    def test_too_many_entries_is_malformed(self):
        body = vce.put_u16(11) + vce.put_u16(0) + vce.put_u16(0) + bytes(88)
        self.assertIsNone(house.answer(req(3, vce.put_bytes(body)), self.ctx))


class Access(Env):
    def test_chat_lists_and_1b_09(self):
        self.say("house allow 足利義輝")
        self.say("house ban 松永久秀")
        out = self.say("house limited")
        self.assertEqual(out[-1][:2], b"\x18\x01")              # the chat refreshes the client's house model
        e = m_enter(out[-1])
        self.assertEqual(e["info"]["access"], 2)
        (rep,) = self.ask(req(9))
        d = m_reply(rep)
        self.assertEqual((d["a"], d["b"]), (1, 1))
        self.assertEqual([x["name"] for x in d["entries"]], ["足利義輝", "松永久秀"])
        self.say("house allow 松永久秀")                        # moves him from banned to allowed
        h = house.state(self.c)
        self.assertEqual(([e["name"] for e in h["allowed"]], h["banned"]), (["足利義輝", "松永久秀"], []))

    def test_name_and_unposted_subs(self):
        self.say("house name あああ")
        (rep,) = self.ask(req(1))
        self.assertEqual(m_reply(rep)["info"]["name"], "あああ")
        self.assertEqual(self.ask(req(0xF, vce.put_u32(3))), [])
        self.assertEqual(house.state(self.c)["access"], 3)
        for sub in (5, 6, 7, 8):
            (rep,) = self.ask(req(sub, vce.put_bytes(b"x") + bytes(16)))
            self.assertEqual(rep[:4], req(sub)[:4])

    def test_world_ready_push(self):
        self.assertFalse(any(p[:2] == b"\x18\x01" for p in self.world.world_ready_pushes(self.ctx)))
        os.environ["NOBU_HOUSE_PUSH"] = "1"
        pushes = [p for p in self.world.world_ready_pushes(self.ctx) if p[:2] == b"\x18\x01"]
        self.assertEqual(m_enter(pushes[0])["info"]["owner"], "織田信長")


if __name__ == "__main__":
    unittest.main()
