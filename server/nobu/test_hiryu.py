"""Specialisation (nobu/hiryu.py): python -m unittest nobu.test_hiryu -v"""
import os
import tempfile
import unittest

from nobu import chars, hiryu, items, progression, shops, skills, vce

SAMURAI = [4, 20, 0, 1, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
KNOBS = ("NOBU_HIRYU", "NOBU_SPEC_LEVEL", "NOBU_SPEC_TITLE", "NOBU_SPEC_TIER2_LEVEL", "NOBU_SPEC_TOP_LEVEL",
         "NOBU_SPEC_CHAT", "NOBU_SPEC_DEBUG", "NOBU_SPEC_INFO_PUSH", "NOBU_SKILL_START_PROF")


def tsv(name):
    """{id: the disc's Japanese} of a name table (empty strings before extract/ has run)."""
    return {k: v[0] for k, v in items.tsv_names(name).items()}


def tsv_en(name):
    return {k: v[1] for k, v in items.tsv_names(name).items()}


def row(table, name):
    return {int(k.split(".")[1]): v for k, v in tsv(name).items() if k.startswith(f"{table}.")}


def char(looks=SAMURAI, **kw):
    return dict(cid=2, uid=0x4000471, family="織田", given="信長", looks=list(looks), **kw)


class Env(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)


class Data(Env):
    def test_lines_match_the_disc(self):
        s0, s2, s8 = row(0, "strdat_00_items.tsv"), row(2, "strdat_02_skillbooks.tsv"), row(8, "strdat_08_schools.tsv")
        self.assertEqual([ln[0] for ln in hiryu.LINES], list(range(1, 22)))
        for line, en, job, tiers in hiryu.LINES:
            self.assertIn(line, s8)
            self.assertEqual(job, (line - 1) // 3 + 1)                   # three lines per job, in job order
            for scroll, item in tiers:
                self.assertEqual(s0[item], s2[scroll], (line, scroll))   # the item is the scroll book
        self.assertEqual(sorted(hiryu.TITLE_SLOT), list(range(1, 22)))
        for job in range(1, 8):                                          # each job's lines get its 3 titles
            self.assertEqual(sorted(hiryu.TITLE_SLOT[ln[0]] for ln in hiryu.lines_of_job(job)), [0, 1, 2])

    def test_titles_are_wdmmsg_0_147(self):
        w = {int(k.split(".")[2]): v for k, v in tsv("wdmmsg.tsv").items() if k.startswith("0.147.")}
        for t in range(1, 57):
            self.assertEqual(hiryu.title_name(t, jp=True), w[t - 1], t)   # 0x274010: row title - 1
        en = {int(k.split(".")[2]): v for k, v in tsv_en("wdmmsg.tsv").items() if k.startswith("0.147.")}
        for t in range(1, 57):
            self.assertEqual(hiryu.title_name(t), en[t - 1], t)           # server text: the English row


class Titles(Env):
    def test_title_by_line_and_level(self):
        c = char(level=40)
        self.assertIsNone(hiryu.title(c))
        self.assertEqual(chars.appearance(c)["b6a"], SAMURAI[10])        # unchanged until specialised
        c["spec"] = 2                                                    # Martial
        self.assertEqual((hiryu.title(c), hiryu.title_name(hiryu.title(c))), (8, "Swordsman"))
        self.assertEqual(chars.appearance(c)["b6a"], 8)                  # CHAR_INFO +0x6a -> P+0xa1
        c["level"] = 50
        self.assertEqual(hiryu.title(c), 29)                             # Duelist
        self.assertEqual(hiryu.title_name(29, jp=True), tsv("wdmmsg.tsv")["0.147.28"])
        c["level"] = 60
        self.assertEqual(hiryu.title(c), 50)                             # Kensei
        c.update(spec=16, level=41, looks=[0, 0, 0, 6] + [0] * 16)       # Swords smith
        self.assertEqual(hiryu.title(c), 23)
        os.environ["NOBU_SPEC_TITLE"] = "0"
        self.assertIsNone(hiryu.title(c))

    def test_char_info_carries_the_title(self):
        c = char(level=45, spec=3)                                       # Tactics -> Tactician (9)
        rec = chars.char_info_record(c, 0x4000471, (110, 1, 2, 3, 0))
        from nobu import clientmodel as cm
        frame = chars.notify_char_info([rec])
        self.assertIn(bytes([SAMURAI[9], 9]), frame)                     # +0x69 slider, +0x6a title
        self.assertTrue(cm.parse_notify_char(frame)["records"])


class Gating(Env):
    def test_refusal_bits(self):
        c = char(level=39)
        self.assertEqual(hiryu.refusal(c, 234), hiryu.F_LEVEL)          # Martial Lore I below the unlock level
        c["level"] = 40
        self.assertEqual(hiryu.refusal(c, 234), 0)
        self.assertEqual(hiryu.refusal(c, 235), hiryu.F_PREREQ)         # Before
        self.assertEqual(hiryu.refusal(c, 238), hiryu.F_JOB)            # a monk line
        c["spec"] = 1
        self.assertEqual(hiryu.refusal(c, 234), hiryu.F_LINE)
        self.assertEqual(hiryu.refusal(c, 1), hiryu.F_LEARNED)          # the starter scroll is known
        self.assertEqual(hiryu.refusal_text(hiryu.F_LINE | hiryu.F_LEVEL),
                         "This scroll is not in your specialization line. Cannot learn.")

    def test_shop_goods_flags(self):
        c = char(level=40, spec=1)
        shop = dict(name="Scrolls", items=[dict(id=4142, type=82, price=100, stack=False),
                                           dict(id=4140, type=82, price=100, stack=False),
                                           dict(id=201, type=52, price=10)])
        rec = shops.shop_record(shop, [0] * 50, c)
        r = vce.Reader(rec)
        r.bytes(), r.u8(), r.u8()
        r.length()
        [r.u32() for _ in range(3)]
        n = r.length()
        [r.u32() for _ in range(n)]
        flags = []
        for _ in range(3):
            r.u8(), r.u8(), r.u8(), r.u16(), r.u16()
            flags.append(r.u16())
            r.u32(), r.u8(), r.u8()
        self.assertEqual(flags, [1 | hiryu.F_LINE, 1, 1])               # bit 0 = self-describing entry
        self.assertEqual(shops.shop_record(shop, [0] * 50), shops.shop_record(shop, [0] * 50, None))


class World(Env):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_SKILL_START_PROF"] = "2500"
        from nobu import world
        self.world = world
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.ctx = {"uid": 0x4000471}
        body = chars.write_create(dict(family="織田", given="信長", looks=SAMURAI))
        world.answer(bytes([5, 1]) + vce.put_u32(0x4000471) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(0x4000471) + vce.put_u32(1), self.ctx)
        self.c = self.ctx["char"]

    def tearDown(self):
        self.world.STORE = None
        self.tmp.cleanup()
        super().tearDown()

    def say(self, text):
        return self.world.answer(bytes([0x23, 8]) + vce.put_u16(3) + vce.put_bytes(text.encode("utf-8")), self.ctx)

    def use(self, iid):
        it = next(i for i in items.inventory(self.c)["items"] if i["id"] == iid)
        return self.world.answer(bytes([0x24, 3]) + vce.put_u16(9) + bytes([it["idx"], 0]) + vce.put_u32(iid), self.ctx)

    def test_chat_choice_needs_the_level(self):
        out = self.say("spec 2")
        self.assertIn(b"opens at level 40", out[0])
        self.c["level"] = 40
        out = self.say("spec 2")
        self.assertEqual(out[0][:2], b"\x0b\x0a")                      # CHAR_INFO: P+0xa1 gets the title
        self.assertEqual(self.c["spec"], 2)
        self.assertIn(b"Specialisation: Martial Arts. Title: Swordsman.", out[-1])   # English only
        self.assertIn(b"Say `spec reset` first", self.say("spec 3")[0])
        self.assertEqual(self.say("spec")[0][:2], b"\x0d\x06")

    def test_learning_the_first_line_scroll_fixes_the_line(self):
        self.c["level"] = 40
        self.say("spec scroll 1")                                        # Bushido I (4140) into the backpack
        self.say("spec scroll 2")                                        # Martial Lore I (4142)
        out = self.use(4140)
        self.assertEqual(out[0], bytes([0x24, 3]) + vce.put_u16(9))
        self.assertEqual(self.c["spec"], 1)
        book = skills.state(self.c)["books"][-1]
        self.assertEqual(book, dict(scroll=232, prof=0))
        self.assertTrue(any(p[:2] == b"\x32\x02" for p in out))          # the new book (32 02)
        self.assertTrue(any(p[:2] == b"\x0b\x0a" for p in out))          # title refresh
        self.assertFalse(any(i["id"] == 4140 for i in items.inventory(self.c)["items"]))
        out = self.use(4142)                                             # another line: refused, item kept
        self.assertIn(b"not in your specialization line", out[-1])
        self.assertTrue(any(i["id"] == 4142 for i in items.inventory(self.c)["items"]))
        # the Details panel's "<line>family": 26 04 ends with the line byte (B+0x1a6)
        slot = len(skills.state(self.c)["books"]) - 1
        (rep,) = self.world.answer(bytes([0x26, 4]) + vce.put_u16(5) + vce.put_u32(slot), self.ctx)
        self.assertEqual(rep[-1], 1)
        (rep,) = self.world.answer(bytes([0x26, 4]) + vce.put_u16(5) + vce.put_u32(0), self.ctx)
        self.assertEqual(rep[-1], 0)                                     # the starter scroll has no line

    def test_reset_forgets_line_scrolls(self):
        self.c["level"] = 40
        self.say("spec scroll 1")
        self.use(4140)
        out = self.say("spec reset")
        self.assertNotIn("spec", self.c)
        self.assertEqual([b["scroll"] for b in skills.state(self.c)["books"]], [1])
        self.assertIn(b"1 line scroll", out[-1])

    def test_level_up_note(self):
        self.c.update(level=39, exp=progression.threshold(39))
        progression.award_ctx(self.ctx, exp=progression.need(39))
        lines = b"".join(progression.flush(self.ctx))
        self.assertIn(b"you can specialise now", lines)
        self.assertIn(b"to see the Samurai lines.", lines)                 # English job name


def m_pot_list(p):
    """0x284c40: raw u8 n, n x 0x1eff90 {u8 id, u8 cur, u8 max, u16 cost}, then (bytes remain) n x u8 category."""
    r = vce.Reader(p, 4)
    n = r.u8()
    ents = [dict(id=r.u8(), cur=r.u8(), max=r.u8(), cost=r.u16()) for _ in range(n)]
    if not r.done():
        for e in ents:
            e["cat"] = r.u8()
    assert r.done() and all(e["id"] < 200 for e in ents)
    return ents


def m_pot_raise(p):
    r = vce.Reader(p, 4)
    d = dict(points=r.u16(), id=r.u8(), cur=r.u8(), max=r.u8(), cost=r.u16())
    if not r.done():
        d["cat"] = r.u8()
    assert r.done()
    return d


def m_pot_push(p):
    """0x286258: REC [u8 cat, u8 k, k x (REC, u8 cat)], REC = u16 lv, u16 points, u8, u32 mastery, u32 need."""
    r = vce.Reader(p, 4)

    def rec():
        return dict(lv=r.u16(), points=r.u16(), b=r.u8(), mastery=r.u32(), need=r.u32())
    recs = [rec()]
    if r.done():
        recs[0]["cat"] = 0
    else:
        recs[0]["cat"] = r.u8()
        for _ in range(r.u8()):
            recs.append(rec())
            recs[-1]["cat"] = r.u8()
    assert r.done() and len(recs) <= 3                       # the client buffer holds 0x3c bytes
    return recs


class Potential(World):
    def ask(self, sub, body=b""):
        return self.world.answer(bytes([0x23, sub]) + vce.put_u16(0x21) + body, self.ctx)

    def test_list_and_names(self):
        (rep,) = self.ask(0x1A)
        ents = m_pot_list(rep)
        self.assertEqual([e["id"] for e in ents], list(range(1, 21)))
        s6 = row(6, "strdat_06_traits.tsv")
        for e in ents:
            self.assertEqual(hiryu.pot_name(e["id"], jp=True), s6[e["id"]])
            self.assertEqual(hiryu.pot_name(e["id"]), tsv_en("strdat_06_traits.tsv")[f"6.{e['id']}"])
            self.assertEqual((e["cur"], e["cat"]), (0, 0))
            self.assertEqual(e["cost"], hiryu.POTENTIALS[e["id"]][0])
        self.assertEqual(sum(hiryu.POTENTIALS[1]), 42)       # wiki totals
        self.assertEqual(sum(hiryu.POTENTIALS[5]), 270)
        self.assertEqual(sum(hiryu.POTENTIALS[11]), 310)
        self.assertEqual(sum(hiryu.POTENTIALS[2]), 70)

    def test_raise_needs_level_and_points(self):
        rep, push, line = self.ask(0x1B, b"\x01")
        self.assertIn(b"until Level 30", line)
        self.assertEqual(m_pot_raise(rep)["cur"], 0)
        self.c["level"] = 30
        self.say("potential mastery 300")                        # one potential level = 10 points
        rep, push, line = self.ask(0x1B, b"\x01")                # Leg Strength costs 3
        d = m_pot_raise(rep)
        self.assertEqual((d["points"], d["id"], d["cur"], d["max"], d["cost"], d["cat"]), (7, 1, 1, 10, 3, 0))
        self.assertIn(b"Leg Strength raised to 1/10", line)                # English STRDAT 6 name
        self.assertEqual(m_pot_push(push)[0]["points"], 7)
        rep, push, line = self.ask(0x1B, b"\x0a")                # Versatility costs 50
        self.assertIn(b"Not enough potential points", line)
        self.assertEqual(self.c["potential"]["raised"], {"1": 1})

    def test_battle_prof_feeds_mastery(self):
        self.c.update(level=30, exp=progression.threshold(30))
        res = progression.award_ctx(self.ctx, prof=650)
        self.assertEqual(res["pot"], 2)
        recs = m_pot_push(next(p for p in progression.flush(self.ctx) if p[:2] == b"\x2f\x1e"))
        self.assertEqual([(r["lv"], r["points"], r["mastery"], r["cat"]) for r in recs],
                         [(2, 20, 50, 0), (0, 0, 0, 1), (0, 0, 0, 2)])
        self.c["level"] = 29
        self.assertEqual(hiryu.pot_add_mastery(self.c, 1000), 0)


if __name__ == "__main__":
    unittest.main()
