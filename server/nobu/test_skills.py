"""Skills, scrolls and training (nobu/skills.py): python -m unittest nobu.test_skills -v

Also runs a fight with a skill through world.answer."""
import os
import tempfile
import unittest

from nobu import battle, chars, items, skills, vce

SMITH = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]          # job 6, Int 3
KNOBS = ("NOBU_SKILLS", "NOBU_SKILL_TRAIN_RATE", "NOBU_SKILL_START_PROF", "NOBU_SKILL_BASE_SLOTS",
         "NOBU_SKILL_STATUS", "NOBU_SKILL_NAMES", "NOBU_SKILL_SCENE", "NOBU_SKILL_CHAT", "NOBU_SKILL_FLAGS",
         "NOBU_SKILL_BATTLE", "NOBU_UPKEEP")


def char(looks=SMITH, **kw):
    return dict(cid=2, uid=0x4000471, family="織田", given="信長", looks=list(looks), **kw)


def tsv(name, col=0):
    """{row: the disc's Japanese (col 0, empty before extract/) or English (col 1)} of a STRDAT table."""
    return {int(k.split(".")[1]): v[col] for k, v in items.tsv_names(name).items()}


class Env(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)


class Data(Env):
    def test_names_are_strdat(self):
        s1, s2 = tsv("strdat_01_skills.tsv"), tsv("strdat_02_skillbooks.tsv")
        for sid, s in skills.SKILLS.items():
            self.assertEqual(skills.name(sid, "jp"), s1[sid] or s["en"], sid)
            self.assertNotIn(sid, (1, 2))                   # the Attack / Flee menu values
            self.assertGreater(s["ki"], 0)
            self.assertIn(s["ttype"], (skills.ENEMY, skills.SELF))
        for scroll, (en, ids) in skills.SCROLLS.items():
            self.assertIn(scroll, s2)
            self.assertTrue(3 <= len(ids) <= skills.MAX_PER_BOOK)       # the client keeps 4 per scroll
            self.assertTrue(all(i in skills.SKILLS for i in ids))
        self.assertEqual(sorted(skills.STARTER), [1, 2, 3, 4, 5, 6, 7])
        every = [i for _, ids in skills.SCROLLS.values() for i in ids
                 if skills.SKILLS[i]["kind"] != "revive"]          # Revive 1 is shared by the reviver lines
        self.assertEqual(len(every), len(set(every)))

    def test_short_texts_fit_the_panel(self):
        for s in skills.SKILLS.values():
            self.assertLessEqual(len(s["text"].encode("cp932")), 0x34, s["en"])
            self.assertLessEqual(len(s["en"].encode("cp932")), 0x18, s["en"])     # 22 33 name
            jp = items.tsv_jp("strdat_01_skills.tsv", f"1.{s['id']}")
            self.assertLessEqual(len(jp.encode("cp932")), 0x18, jp)


class State(Env):
    def test_starter_scroll_and_first_skill(self):
        c = char()
        st = skills.state(c)
        self.assertEqual(st["books"], [dict(scroll=173, prof=2500)])
        self.assertEqual((st["training"], skills.learned(c), skills.bar(c)), (0, [146], {0: 146}))
        self.assertEqual(st["bar"], {"0": skills.packed(0, 0)})
        for job, scroll in skills.STARTER.items():
            lk = list(SMITH)
            lk[3] = job
            self.assertEqual(skills.state(char(lk))["books"][0]["scroll"], scroll)

    def test_slots_grow_with_intellect(self):
        for intel, n in ((3, 3), (25, 4), (74, 5), (999, 10)):
            lk = list(SMITH)
            lk[14] = intel
            self.assertEqual(skills.slot_count(char(lk)), n)
        os.environ["NOBU_SKILL_BASE_SLOTS"] = "1"
        self.assertEqual(skills.slot_count(char()), 1)

    def test_start_prof_knob(self):
        os.environ["NOBU_SKILL_START_PROF"] = "0"
        c = char()
        self.assertEqual((skills.learned(c), skills.bar(c), skills.menu_entries(c)), ([], {}, []))


class Wire(Env):
    def test_booklist_matches_reader_0x1ef570(self):
        c = char()
        d = skills.parse(skills.booklist_reply(0x13, c))
        self.assertEqual((d["seq"], d["training"], d["scrolls"], d["left"]), (0x13, 0, [173], 0))
        self.assertEqual(d["books"], [dict(slot=0, b2=0, b3=0, prof=2500, scroll=173, skills=[146, 202, 221])])
        skills.state(c)["training"] = None
        self.assertEqual(skills.parse(skills.booklist_reply(1, c))["training"], 0xFFFF)

    def test_standby_list_matches_reader_0x1efd00(self):
        c = char()
        rep = skills.standby_reply(0x14, c)
        self.assertEqual(len(rep), 4 + 1 + 11 + 50 * 11)
        d = skills.parse(rep)
        self.assertEqual(d["cur"], [dict(slot=0, book=0, idx=0, r=dict(id=146, flags=0x10, b6=0, b7=0))])
        self.assertTrue(all(e["r"]["id"] == 0 for row in d["grid"] for e in row))
        self.assertEqual(d["left"], 0)

    def test_skill_and_book_info(self):
        c = char()
        d = skills.parse(skills.skill_info_reply(5, 146, c))
        self.assertEqual((d["res"], d["id"], d["label"], d["ki"], d["label_fits"], d["text_fits"], d["left"]),
                         (0, 146, "Strike", 7, True, True, 0))
        self.assertEqual(skills.parse(skills.skill_info_reply(5, 9999, c))["res"], -1)
        d = skills.parse(skills.book_info_reply(6, 0, c))
        self.assertEqual(([s["id"] for s in d["skills"]], d["b24"], d["left"]), ([146, 202, 221, 0], 0, 0))
        self.assertEqual(skills.parse(skills.book_info_reply(6, 3, c))["res"], -1)

    def test_status_push_sets_the_slot_count(self):
        lk = list(SMITH)
        lk[14] = 50
        d = skills.parse(skills.status_push(char(lk)))           # upkeep.stats_push (field Life / Ki / weight)
        self.assertEqual((d["cmd"], d["sub"], d["slots"], d["stats"][:5], d["left"]), (0x2F, 7, 5, [6, 6, 8, 50, 3], 0))
        from nobu import upkeep
        self.assertEqual(skills.parse(upkeep.stats_push(char(lk)))["slots"], 5)   # its own pushes keep the count
        os.environ["NOBU_UPKEEP"] = "0"                                             # the fallback block
        d = skills.parse(skills.status_push(char(lk)))
        self.assertEqual((d["slots"], d["left"]), (5, 0))
        pc = battle.player_combat(char(lk))
        self.assertEqual(d["tail"], [pc["life"], pc["life"], pc["ki"], pc["ki"], pc["atk"], pc["dfn"], 0, 0])

    def test_requests(self):
        c = char()
        ctx, store = {"char": c}, None
        std = skills.answer(bytes.fromhex("26010007"), ctx, store)
        self.assertEqual([f[:2].hex() for f in std], ["2601", "2f07"])
        os.environ["NOBU_SKILL_STATUS"] = "0"
        self.assertEqual(len(skills.answer(bytes.fromhex("26010007"), ctx, store)), 1)
        (bl,) = skills.answer(bytes.fromhex("26020006"), ctx, store)
        self.assertEqual(skills.parse(bl)["books"][0]["scroll"], 173)
        (si,) = skills.answer(bytes.fromhex("26030008") + vce.put_u32(202), ctx, store)
        self.assertEqual(skills.parse(si)["ki"], 5)
        (bi,) = skills.answer(bytes.fromhex("26040009") + vce.put_u32(0), ctx, store)
        self.assertEqual(skills.parse(bi)["res"], 0)
        tr = skills.answer(bytes.fromhex("2608000a") + vce.put_u32(0), ctx, store)
        self.assertEqual(skills.parse(tr[0])["training"], 0)
        self.assertIn(b"Training scroll", tr[1])
        # equip: a learned skill goes in, an unlearned one is refused (u32 0: 0x203a60 ignores it)
        (eq,) = skills.answer(bytes.fromhex("2605000b") + b"\x02" + vce.put_u32(skills.packed(0, 0)), ctx, store)
        self.assertEqual(skills.parse(eq), dict(cmd=0x26, sub=5, seq=0xb, slot=2, packed=1, left=0))
        self.assertEqual(skills.bar(c), {2: 146})
        bad = skills.answer(bytes.fromhex("2605000c") + b"\x01" + vce.put_u32(skills.packed(0, 1)), ctx, store)
        self.assertEqual((skills.parse(bad[0])["packed"], len(bad)), (0, 2))
        (un,) = skills.answer(bytes.fromhex("2606000d02"), ctx, store)
        self.assertEqual((un.hex(), skills.bar(c)), ("2606000d02", {}))
        # swap setup: code 0 stores the current bar as set 1 (0x203270), which the standby grid then carries
        skills.answer(bytes.fromhex("2605000e") + b"\x00" + vce.put_u32(1), ctx, store)
        (sw,) = skills.answer(bytes.fromhex("260a0047") + bytes([1, 0]) + vce.put_u16(0), ctx, store)
        self.assertEqual(sw.hex(), "260a0047" + "00000000" + "01000000")
        grid = skills.parse(skills.standby_reply(1, c))["grid"]
        self.assertEqual((grid[1][0]["r"]["id"], grid[0][0]["r"]["id"]), (146, 0))
        self.assertIsNone(skills.answer(bytes.fromhex("26010007"), {}, store))    # no character: fallback

    def test_training_refuses_a_mastered_scroll(self):
        c = char()
        st = skills.state(c)
        st["books"][0]["prof"] = 7500
        st["training"] = None
        (tr,) = skills.answer(bytes.fromhex("26080001") + vce.put_u32(0), {"char": c}, None)
        self.assertEqual(skills.parse(tr)["training"], 0xFFFF)

    def test_describe(self):
        from nobu import world
        self.assertEqual(world.describe(bytes.fromhex("26020006"), "C>S"), "SKILL BOOKLIST seq=6 ")
        self.assertIn("PROF_ADD", world.describe(skills.notify_prof(0, 125), "S>C"))
        self.assertIn("slots=3", skills.describe(skills.status_push(char()), "S>C"))   # world: upkeep.describe first


class Training(Env):
    def test_prof_teaches_and_masters(self):
        c = char()
        ctx = {"char": c}
        os.environ["NOBU_SKILL_TRAIN_RATE"] = "100"
        res = skills.train(ctx, 25)                          # 2500: the second skill
        self.assertEqual((res["add"], res["learned"], res["mastered"]), (2500, [202], False))
        out = skills.flush(ctx)
        self.assertEqual(skills.parse(out[0]), dict(cmd=0x32, sub=1, seq=0, slot=0, add=2500, left=0))
        self.assertIn("Learned skill 《Weapon Mastery》.".encode("utf-8"), out[1])
        self.assertEqual(skills.bar(c), {0: 146, 1: 202})   # auto-equipped into the next free slot
        res = skills.train(ctx, 1000)                        # capped at mastery
        self.assertEqual((res["add"], res["prof"], res["mastered"], res["learned"]), (2500, 7500, True, [221]))
        out = skills.flush(ctx)
        self.assertEqual(skills.parse(out[2])["training"], 0xFFFF)
        self.assertIn(b"mastered!", out[3])
        self.assertIsNone(skills.train(ctx, 10))             # no training scroll any more
        self.assertEqual(skills.flush(ctx), [])

    def test_default_rate(self):
        ctx = {"char": char()}
        self.assertEqual(skills.train(ctx, 5)["add"], 125)


class BattleInterface(Env):
    def test_menu_items_are_skills_node_children(self):
        c = char()
        (item,) = [battle.parse22(f) for f in skills.menu_items(c)]
        self.assertEqual((item["value"], item["key"], item["ttype"], item["cost"], item["cost_kind"],
                          item["item_name"], item["left"]), (146, 5, 1, 7, 0, "Adamant Blow I", 0))
        os.environ["NOBU_SKILL_NAMES"] = "jp"
        self.assertEqual(battle.parse22(skills.menu_items(c)[0])["item_name"],
                         items.tsv_jp("strdat_01_skills.tsv", "1.146", "Adamant Blow I"))
        os.environ["NOBU_SKILL_BATTLE"] = "0"
        self.assertEqual(skills.menu_items(c), [])

    def test_resolve_and_apply(self):
        import random
        c = char()
        skills.state(c)["books"][0]["prof"] = 7500
        me = dict(side="p", hp=50, mhp=100, ki=20, mki=20, c=dict(level=1, atk=20, dfn=10, dex=3))
        foe = dict(side="e", hp=60, mhp=60, ki=0, mki=0, c=dict(level=1, atk=8, dfn=4, dex=4))
        rng = random.Random(1)
        eff = skills.resolve(c, 146, me, foe, rng)
        self.assertEqual((eff["ok"], eff["kind"], eff["self"], eff["scene"]), (True, "damage", False, skills.SKILLS[146]["scene"]))
        self.assertTrue(20 <= eff["dmg"] <= 30)
        tgt = skills.apply(eff, me, foe)
        self.assertIs(tgt, foe)
        self.assertEqual((me["ki"], foe["hp"]), (13, 60 - eff["dmg"]))
        eff = skills.resolve(c, 221, me, foe, rng)                # Nio Stance: Def +40 %
        self.assertIs(skills.apply(eff, me, foe), me)
        self.assertEqual((me["c"]["dfn"], me["ki"], eff["scene"]), (14, 6, skills.SKILLS[221]["scene"]))
        self.assertEqual(skills.resolve(c, 202, me, foe, rng)["reason"], "")
        self.assertEqual(skills.resolve(c, 146, me, foe, rng)["reason"], "ki")   # 6 Ki < 7
        self.assertIsNone(skills.resolve(c, 1, me, foe, rng))                   # Attack is not a skill
        self.assertIsNone(skills.resolve(c, 168, me, foe, rng))                 # not this character's
        self.assertEqual(skills.info(c, 146), ("Adamant Blow I", "A smith's hammer blow. Ki 7."))

    def test_heal_and_drain(self):
        import random
        lk = list(SMITH)
        lk[3], lk[14] = 7, 10                                    # a Physician with Int 10
        c = char(lk)
        skills.state(c)["books"][0]["prof"] = 5000
        me = dict(side="p", hp=10, mhp=100, ki=30, mki=30, c=dict(level=2, atk=5, dfn=5, dex=3))
        foe = dict(side="e", hp=60, mhp=60, ki=0, mki=0, c=dict(level=1, atk=8, dfn=4, dex=4))
        eff = skills.resolve(c, 210, me, foe, random.Random(2))
        self.assertEqual(eff["heal"], 35 + 20)
        skills.apply(eff, me, foe)
        self.assertEqual(me["hp"], 65)
        eff = skills.resolve(c, 288, me, foe, random.Random(2))
        self.assertEqual(eff["heal"], eff["dmg"] // 2)


def patched_battle():
    from nobu import battle
    return battle


class PatchedBattle(Env):
    """A Smith fights the debug Bandit with Adamant Blow I through world.answer and battle.py."""
    MORE = ("NOBU_BATTLE_FIGHT", "NOBU_DEBUG_ENEMY", "NOBU_BATTLE_LIFE", "NOBU_BATTLE_KI", "NOBU_BATTLE_ENEMY_LIFE",
            "NOBU_BATTLE_ENEMY_ATK", "NOBU_BATTLE_SEED", "NOBU_GUILD", "NOBU_ZONE", "NOBU_AREA", "NOBU_START_TOWN",
            "NOBU_FORCE_HOME", "NOBU_BATTLE_ONE_HIT", "NOBU_BATTLE_CHAT", "NOBU_BATTLE_PROF")

    def setUp(self):
        super().setUp()
        from nobu import world
        self.world = world
        self.pb = patched_battle()                           # before any env / store change (fails cleanly)
        self.more = {k: os.environ.pop(k) for k in list(os.environ) if k in self.MORE}
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        os.environ.update(NOBU_BATTLE_FIGHT="1", NOBU_DEBUG_ENEMY="1", NOBU_BATTLE_LIFE="100", NOBU_BATTLE_KI="50",
                          NOBU_BATTLE_ENEMY_LIFE="40", NOBU_BATTLE_ENEMY_ATK="1", NOBU_BATTLE_SEED="3",
                          NOBU_GUILD="0", NOBU_SKILL_TRAIN_RATE="500")
        self.real = world.battle
        world.battle = self.pb
        self.ctx = {"uid": 0x4000471}
        body = chars.write_create(dict(family="織田", given="信長", looks=SMITH))
        world.answer(bytes([5, 1]) + vce.put_u32(0x4000471) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(0x4000471) + vce.put_u32(1), self.ctx)
        world.answer(b"\x04", self.ctx)
        world.answer(bytes.fromhex("235a0013"), self.ctx)

    def tearDown(self):
        self.world.battle = self.real
        self.world.STORE = None
        for k in self.MORE:
            os.environ.pop(k, None)
        os.environ.update(self.more)
        self.tmp.cleanup()
        super().tearDown()

    def ans(self, h):
        return self.world.answer(bytes.fromhex(h), self.ctx)

    def test_skill_in_battle(self):
        pb, bandit = self.pb, self.pb.DEFAULT_ENEMY_CID
        self.world.answer(bytes.fromhex("25010040") + vce.put_u32(bandit), self.ctx)
        setup = [pb.parse22(f) for f in self.ans("28010041")[1:]]
        items = [(p["value"], p["key"], p["item_name"], p["cost"]) for p in setup if p["type"] == 0x33]
        self.assertEqual(items, [(1, 1, "Attack", 0), (2, 4, "Flee", 0), (146, 5, "Adamant Blow I", 7)])
        (info,) = [pb.parse22(f) for f in self.ans("22420092")]            # 22 42 on the skill (0x92 = 146)
        self.assertEqual((info["type"], info["key"], info["item_name"]), (0x44, 146, "Adamant Blow I"))
        self.ans("2232")
        cmd = "2201" + bandit.to_bytes(4, "big").hex() + "00" * 12 + "0092" + "0000"
        rounds = 0
        while not self.ctx["battle"]["S"]["over"] and rounds < 10:
            rounds += 1
            p = [pb.parse22(f) for f in self.ans(cmd) if f[0] == 0x22]
            hero = next(x for x in p if x["type"] == 0x02)
            self.assertEqual((hero["type"], hero["bi"]["actor"], hero["bi"]["target"]), (0x02, 0, 7))
            self.assertIn(hero["bi"]["code"], (skills.SKILLS[146]["scene"], skills.SKILLS[146]["kill_scene"]))
        log = [r for r in self.ctx["battle_rounds"] if r.get("outcome") == "skill"]
        self.assertTrue(log and all(r["skill"] == 146 for r in log))
        self.assertEqual(log[0]["ki"], 50 - 7)
        self.ans("2254")
        done = self.ans("2255")
        kinds = [f[:2].hex() for f in done]
        self.assertIn("3201", kinds)                                         # proficiency push
        push = skills.parse(next(f for f in done if f[:2] == b"\x32\x01"))
        self.assertEqual((push["slot"], push["add"]), (0, 2500))             # Prof. 5 x 500
        self.assertIn("Learned skill 《Weapon Mastery》.".encode("utf-8"), b"".join(done))
        c = self.world.store().find(0x4000471, self.ctx["cid"])
        self.assertEqual(c["skills"]["books"][0]["prof"], 5000)

    def test_not_enough_ki_falls_back_to_attack(self):
        os.environ["NOBU_BATTLE_KI"] = "3"
        pb, bandit = self.pb, self.pb.DEFAULT_ENEMY_CID
        self.world.answer(bytes.fromhex("25010040") + vce.put_u32(bandit), self.ctx)
        self.ans("28010041")
        self.ans("2232")
        self.ans("2201" + bandit.to_bytes(4, "big").hex() + "00" * 12 + "0092" + "0000")
        log = self.ctx["battle"]["S"]["log"]
        self.assertEqual((log[0]["skill"], log[0]["refused"]), (146, "ki"))       # 3 Ki < 7
        self.assertIn(log[1]["outcome"], ("hit", "crit", "dodge", "block", "parry"))
        self.assertEqual(self.ctx["battle"]["S"]["units"][0]["ki"], 3)


class WorldHooks(Env):
    def setUp(self):
        super().setUp()
        from nobu import world
        self.world = world
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.ctx = {"uid": 0x4000471}
        body = chars.write_create(dict(family="織田", given="信長", looks=SMITH))
        world.answer(bytes([5, 1]) + vce.put_u32(0x4000471) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(0x4000471) + vce.put_u32(1), self.ctx)

    def tearDown(self):
        self.world.STORE = None
        self.tmp.cleanup()
        super().tearDown()

    def say(self, text):
        return self.world.answer(bytes([0x23, 8]) + vce.put_u16(3) + vce.put_bytes(text.encode("utf-8")), self.ctx)

    def test_world_entry_lists(self):
        (bl,) = self.world.answer(bytes.fromhex("26020001"), self.ctx)
        self.assertEqual(skills.parse(bl)["books"][0]["skills"], [146, 202, 221])
        sb, st = self.world.answer(bytes.fromhex("26010002"), self.ctx)
        self.assertEqual((skills.parse(sb)["cur"][0]["r"]["id"], skills.parse(st)["slots"]), (146, 3))
        c = self.world.store().find(0x4000471, self.ctx["cid"])
        self.assertEqual(c["skills"]["books"], [dict(scroll=173, prof=2500)])     # persisted

    def test_chat(self):
        out = self.say("skill")
        self.assertIn(b"Way of the Smith I (training): 2500/7500, learned 1/3", b"".join(out))
        out = self.say("skill prof 2500")
        self.assertEqual(out[0][:2].hex(), "3201")
        self.assertIn(b"Learned skill", out[1])
        self.assertIn(b"Adamant Blow I (Ki 7), Weapon Mastery (Ki 5)", b"".join(out))
        self.say("skill reset")
        c = self.world.store().find(0x4000471, self.ctx["cid"])
        self.assertEqual(c["skills"]["books"][0]["prof"], 2500)
        self.assertIsNone(skills.answer(bytes([0x23, 8]) + vce.put_u16(3) + vce.put_bytes(b"hello"), self.ctx))


if __name__ == "__main__":
    unittest.main()
