"""Level / EXP progression and the 2F status pushes: python -m unittest nobu.test_progression -v"""
import os
import tempfile
import unittest

from nobu import chars, progression, social, vce, world

KNOBS = ("NOBU_LEVEL_CAP", "NOBU_EXP_BASE", "NOBU_EXP_POW", "NOBU_PROG_PUSH", "NOBU_PROG_FAME_PUSH",
         "NOBU_PROG_MERIT_PUSH", "NOBU_PROG_CHAT")


# ---------------------------------------------------------------------------- client models
def model_2f(payload):
    """Parser 0x284fd0 (u8 sub, u16 seq) + the archive of the sub, then the UI handler's P writes."""
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    P = {}
    if sub == 0x0D:                                    # 0x1f21e0 -> UI 0x4b -> 0x209620
        lv, pct, w, exp, nxt = r.u8(), r.u8(), r.u16(), r.u32(), r.u32()
        P.update({0x2f: lv, 0x102c8: pct, 0x10c68: w, 0x102cc: exp, 0x102d0: nxt})
        ui = 0x4B
    elif sub == 0x0C:                                  # 0x1ebde0 -> UI 3 -> 0x207ef0
        fac, rank, fame, _x = r.u8(), r.u8(), r.u16(), r.u16()
        P.update({0x30: fac, 0x102c6: rank, 0x102c2: fame})
        ui = 3
    elif sub == 0x0E:                                  # 0x1f20d0 -> UI 0x4a -> 0x2095e0
        rank, b, merit = r.u8(), r.u8(), r.u16()
        P.update({0x102c6: rank, 0x102c7: b, 0x102d4: merit})
        ui = 0x4A
    else:
        raise AssertionError(f"sub {sub} not modelled")
    assert r.done(), "bytes left over"
    return dict(sub=sub, seq=seq, ui=ui, P=P)


def gauge(P, cap=60):
    """0x20ba40: 100 at the cap, else P+0x102c8."""
    return 100 if P[0x2f] >= cap else P[0x102c8]


class Env(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)


class Table(Env):
    def test_thresholds_increase_and_level_for_inverts_them(self):
        for lv in range(1, 60):
            self.assertLess(progression.threshold(lv), progression.threshold(lv + 1))
            self.assertEqual(progression.level_for(progression.threshold(lv)), lv)
            self.assertEqual(progression.level_for(progression.threshold(lv + 1) - 1), lv)
        self.assertEqual(progression.threshold(1), 0)
        self.assertEqual(progression.threshold(2), 30)

    def test_cap(self):
        self.assertEqual(progression.level_for(10 ** 9), 60)
        os.environ["NOBU_LEVEL_CAP"] = "50"
        self.assertEqual(progression.level_for(10 ** 9), 50)

    def test_percent(self):
        lo, hi = progression.threshold(3), progression.threshold(4)
        self.assertEqual(progression.exp_percent(lo, 3), 0)
        self.assertIn(progression.exp_percent((lo + hi) // 2, 3), (49, 50))
        self.assertEqual(progression.exp_percent(hi - 1, 3), 99)
        self.assertEqual(progression.exp_percent(10 ** 9, 60), 100)


class Award(Env):
    def test_one_level(self):
        c = {}
        res = progression.award(c, exp=10, prof=5, fame=1)
        self.assertEqual((res["old"], res["new"], res["levels"]), (1, 1, []))
        res = progression.award(c, exp=20)
        self.assertEqual((res["new"], res["levels"], c["level"], c["exp"]), (2, [2], 2, 30))
        self.assertEqual((c["prof"], c["fame"]), (5, 1))

    def test_several_levels_and_fame_cap(self):
        c = dict(level=1, exp=0, fame=65530)
        res = progression.award(c, exp=progression.threshold(5), fame=100)
        self.assertEqual(res["levels"], [2, 3, 4, 5])
        self.assertEqual(c["fame"], 0xFFFF)
        self.assertEqual((res["ranks"], c["rank"]), ([1], 1))              # level 5 -> Ashigaru
        self.assertEqual(len(progression.level_up_lines(res)), 5)          # 4 level-ups + the promotion

    def test_never_loses_a_level(self):
        c = dict(level=7, exp=0)
        self.assertEqual(progression.award(c, exp=1)["new"], 7)


class Pushes(Env):
    C = dict(cid=2, uid=7, family="a", given="b", looks=[4, 20, 0, 6, 6] + [0] * 15, level=3,
             exp=200, fame=12, rank=1, pvp=0)

    def test_level_push_matches_the_client_parser(self):
        p = progression.push_level(3, 42, 200, 351, 0)
        self.assertEqual(p.hex(), "2f0d0000" "03" "2a" "0000" "000000c8" "0000015f")
        m = model_2f(p)
        self.assertEqual((m["ui"], m["P"][0x2f], m["P"][0x102cc], m["P"][0x102d0], gauge(m["P"])),
                         (0x4B, 3, 200, 351, 42))

    def test_fame_and_merit_pushes(self):
        m = model_2f(progression.push_fame(6, 1, 12))
        self.assertEqual((m["ui"], m["P"][0x30], m["P"][0x102c6], m["P"][0x102c2]), (3, 6, 1, 12))
        m = model_2f(progression.push_merit(1, 0, 350))
        self.assertEqual((m["ui"], m["P"][0x102d4]), (0x4A, 350))

    def test_status_pushes_use_the_character(self):
        c = dict(self.C)
        out = progression.status_pushes(c)
        self.assertEqual([p[:2] for p in out], [b"\x2f\x0d", b"\x2f\x0c", b"\x2f\x0e"])
        lv = model_2f(out[0])["P"]
        self.assertEqual((lv[0x2f], lv[0x102cc], lv[0x102d0]), (3, 200, progression.threshold(4)))
        self.assertEqual(lv[0x102c8], progression.exp_percent(200, 3))
        self.assertEqual(model_2f(out[1])["P"][0x30], 6)                 # faction kept (looks[4])
        os.environ["NOBU_PROG_FAME_PUSH"] = "0"
        os.environ["NOBU_PROG_MERIT_PUSH"] = "1"
        self.assertEqual([p[:2] for p in progression.status_pushes(c)], [b"\x2f\x0d", b"\x2f\x0e"])
        os.environ["NOBU_PROG_PUSH"] = "0"
        self.assertEqual(progression.status_pushes(c), [])

    def test_flush_sends_the_queued_level_up_line_once(self):
        c = dict(self.C, level=1, exp=0)
        ctx = {"char": c}
        res = progression.award_ctx(ctx, exp=30)
        self.assertEqual(res["levels"], [2])
        out = progression.flush(ctx)
        self.assertEqual(model_2f(out[0])["P"][0x2f], 2)
        self.assertIn(social.notify_system("Level up! You are now level 2."), out)
        self.assertNotIn(social.notify_system("Level up! You are now level 2."), progression.flush(ctx))

    def test_describe(self):
        self.assertIn("level=3", progression.describe(progression.push_level(3, 1, 2, 3), "S>C"))
        self.assertIsNone(progression.describe(b"\x2f\x0d\x00\x00", "C>S"))


def say(text, seq=0x31):
    return bytes([0x23, 0x08]) + vce.put_u16(seq) + vce.put_bytes(text.encode("utf-8"))


class ThroughWorld(Env):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.c = world.STORE.create(7, 0, "織田", "太郎", [4, 20, 0, 6, 6] + [0] * 15)
        self.ctx = {"uid": 7, "cid": self.c["cid"], "char": self.c}

    def tearDown(self):
        world.STORE = None
        self.tmp.cleanup()
        super().tearDown()

    def test_chat_exp_levels_up_and_persists(self):
        out = world.answer(say("exp 100"), self.ctx)
        lv = model_2f(out[0])["P"][0x2f]
        self.assertEqual(lv, progression.level_for(100))
        self.assertGreater(lv, 1)
        self.assertIn(social.notify_system(f"Level up! You are now level {lv}."), out)
        self.assertIn("PROGRESS 2F 0D", world.describe(out[0], "S>C"))
        saved = chars.CharStore(world.STORE.path).find(7, self.c["cid"])
        self.assertEqual((saved["exp"], saved["level"]), (100, lv))

    def test_chat_level_shows_status_without_changes(self):
        out = world.answer(say("level"), self.ctx)
        self.assertEqual(out[0][:2], b"\x2f\x0d")
        self.assertEqual(self.c.get("exp", 0), 0)

    def test_other_chat_is_not_taken(self):
        self.assertIsNone(progression.answer(say("hello"), self.ctx))

    def test_world_ready_pushes_include_the_status(self):
        self.assertFalse([p for p in world.world_ready_pushes(self.ctx) if p[:1] == b"\x2f"])   # new char
        self.c["exp"] = 45
        out = world.world_ready_pushes(self.ctx)
        self.assertIn(progression.status_pushes(self.c)[0], out)


if __name__ == "__main__":
    unittest.main()
