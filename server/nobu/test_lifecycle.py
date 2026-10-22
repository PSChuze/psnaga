"""Character lifecycle (nobu/lifecycle.py): python -m unittest nobu.test_lifecycle -v

Revive with the field skill (26 07) between two sessions, ghosts in the other players' CHAR_INFO, the Nagoya
shrine, stat growth on level-up (2F 07), the second scroll with Revive 1, and the Yoriki / Samurai General
exams through the Hall Master. Everything the client sends goes through world.answer."""
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, enemies, guild, items, lifecycle, party, players, skills, social, upkeep
from nobu import vce, world
from nobu.test_guild import model_2f, say, texts

SMITH = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
MONK = [4, 20, 0, 2, 6, 3, 5, 5, 100, 50, 1, 5, 5, 4, 6, 4, 2, 3, 1, 2]      # male Monk, Oda
KNOBS = ("NOBU_DATA", "NOBU_SHOPS", "NOBU_GUILD", "NOBU_CRAFTING", "NOBU_ENEMIES", "NOBU_DEBUG_NPC",
         "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_SPAWN", "NOBU_PLAYERS",
         "NOBU_LIFECYCLE", "NOBU_LIFE_SHRINE", "NOBU_LIFE_SPIRIT_SIGHT", "NOBU_LIFE_GROWTH", "NOBU_LIFE_EXAMS",
         "NOBU_LIFE_SHRINE_FEE", "NOBU_LIFE_REVIVE_RANGE", "NOBU_UPKEEP", "NOBU_SKILL_START_PROF", "NOBU_QUESTS", "NOBU_TOWN_SPOTS")
MASTER = 0xFEA00004
SHRINE = 0xFEA00013


def run(payload, ctx):
    return world.answer(payload, ctx)


def records(frames, uid=None):
    """CHAR_INFO records (0B 0A) in frames, parsed with the client model 0x1a2500."""
    out = []
    for f in frames:
        if f[:2] == b"\x0b\x0a":
            out += [r for r in cm.parse_notify_char(f)["records"] if uid is None or r["uid"] == uid]
    return out


def params_pushes(frames):
    """2F 05 <seq> <u32 cid> 01 <PARAMS(15)> -> (cid, PARAMS)."""
    out = []
    for f in frames:
        if f[:2] == b"\x2f\x05":
            r = vce.Reader(f, 4)
            cid, _one = r.u32(), r.u8()
            out.append((cid, bytes(r.rest()[1:16])))                  # VCE bytes: count 0x0f + 15
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_DATA"] = self.tmp.name
        os.environ["NOBU_TOWN_SPOTS"] = "0"           # talk from the spawn point: NPCs on their offsets
        for k in ("NOBU_SHOPS", "NOBU_CRAFTING", "NOBU_ENEMIES"):
            os.environ[k] = "0"
        self.sessions = dict(social.SESSIONS)
        social.SESSIONS.clear()
        party.reset()
        guild.reset()
        enemies.reset()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))

    def tearDown(self):
        world.STORE = None
        social.SESSIONS.clear()
        social.SESSIONS.update(self.sessions)
        party.reset()
        guild.reset()
        enemies.reset()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def session(self, sid, uid, family, given, looks):
        got = []
        ctx = {"uid": uid, "push": got.append, "got": got}
        body = chars.write_create(dict(family=family, given=given, looks=looks))
        run(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body, ctx)
        cid = world.store().list(uid)[0]["cid"]
        run(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(cid), ctx)
        social.register(sid, ctx)
        run(b"\x04", ctx)
        ctx["spawn"] = run(bytes.fromhex("235a0013"), ctx)
        return ctx

    @staticmethod
    def wire(of, viewer):
        return players.wire_cid(of["char"]["cid"], viewer["char"]["cid"])


# ---------------------------------------------------------------------------- PARAMS: ghosts and spirit sight
class Params(Base):
    def setUp(self):
        super().setUp()
        self.monk = self.session("m", 0x4000481, "三好", "長慶", MONK)
        self.smith = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)

    def test_own_spawn_record_carries_spirit_sight(self):
        own = records(self.monk["spawn"], uid=0x4000481)
        self.assertEqual(own[0]["params"][:2], b"\x00\x01")          # PARAMS[1] = 1: sees ghosts (0x1da540)
        own = records(self.smith["spawn"], uid=0x4000482)
        self.assertEqual(own[0]["params"][:2], b"\x00\x00")

    def test_spirit_sight_modes(self):
        os.environ["NOBU_LIFE_SPIRIT_SIGHT"] = "none"
        self.assertEqual(chars.char_params(self.monk["char"])[1], 0)
        os.environ["NOBU_LIFE_SPIRIT_SIGHT"] = "all"
        self.assertEqual(chars.char_params(self.smith["char"])[1], 1)
        os.environ["NOBU_LIFE_SPIRIT_SIGHT"] = "ghosts"
        self.assertEqual(chars.char_params(self.monk["char"])[1], 0)
        self.smith["char"]["ghost"] = True
        self.assertEqual(chars.char_params(self.smith["char"])[:2], b"\x02\x01")
        os.environ["NOBU_LIFECYCLE"] = "0"
        self.assertEqual(chars.char_params(self.smith["char"]), chars.char_params())

    def test_other_players_see_the_ghost(self):
        self.monk["got"].clear()
        self.smith["char"]["ghost"] = True
        self.assertEqual(players.refresh(self.smith), 1)
        recs = records(self.monk["got"])
        self.assertEqual(recs[0]["cid"], self.wire(self.smith, self.monk))
        self.assertEqual(recs[0]["params"][0], 2)                      # entry+0x98 = 0 -> ghost (0x273570)
        self.assertEqual(recs[0]["uid"], 0)

    def test_late_joiner_sees_the_ghost(self):
        self.smith["char"]["ghost"] = True
        late = self.session("l", 0x4000483, "柴田", "勝家", SMITH)
        recs = [r for r in records(late["spawn"]) if r["cid"] == self.wire(self.smith, late)]
        self.assertEqual(recs[0]["params"][0], 2)


# ---------------------------------------------------------------------------- revive with the field skill
class FieldRevive(Base):
    def setUp(self):
        super().setUp()
        self.monk = self.session("m", 0x4000481, "三好", "長慶", MONK)
        self.smith = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        st = skills.state(self.monk["char"])
        st["books"].append(dict(scroll=49, prof=2500))                   # Devotions II, Revive 1 learned
        st["bar"]["1"] = skills.packed(1, 0)
        self.smith["char"]["ghost"] = True
        upkeep.state(self.smith["char"])["life"] = 0
        for c in (self.monk, self.smith):
            c["got"].clear()

    def use(self, slot=1, seq=0x55):
        return run(bytes([0x26, 7]) + vce.put_u16(seq) + bytes([slot]), self.monk)

    def test_revive_the_targeted_ghost(self):
        run(bytes.fromhex("25010005") + vce.put_u32(self.wire(self.smith, self.monk)), self.monk)
        ki = upkeep.state(self.monk["char"])["ki"]
        rep = self.use()
        self.assertEqual(rep[0].hex(), "26070055")                     # the bare reply (no body parser)
        cm.parse_cmd26(rep[0])
        self.assertIn("You revived 国友 善兵衛.", texts(rep))
        self.assertEqual(upkeep.state(self.monk["char"])["ki"], ki - 12)
        self.assertIn(b"\x2f\x08", [f[:2] for f in rep])               # caster's Ki
        self.assertNotIn("ghost", self.smith["char"])
        got = self.smith["got"]
        self.assertEqual(params_pushes(got)[0], (self.smith["char"]["cid"], lifecycle.field_params(self.smith["char"])))
        self.assertEqual(params_pushes(got)[0][1][0], 0)
        self.assertIn("2f0b00000000", [f.hex() for f in got])          # "Revived!" (dead flag cleared)
        life = upkeep.state(self.smith["char"])["life"]
        self.assertEqual(life, upkeep.maxima(self.smith["char"])[0] * 25 // 100)
        self.assertIn("三好 長慶 revived you with 《Revive I》.", texts(got))
        recs = records(self.monk["got"])                               # the zone sees the living body again
        self.assertEqual((recs[-1]["cid"], recs[-1]["params"][0]), (self.wire(self.smith, self.monk), 0))
        self.assertEqual(world.describe(bytes.fromhex("2607005501"))[:5], "SKILL")

    def test_nearest_ghost_without_a_target(self):
        rep = self.use()
        self.assertIn("You revived 国友 善兵衛.", texts(rep))

    def test_out_of_range(self):
        self.smith["players_pos"] = (110, 99999, 0, 99999, 0)
        rep = self.use()
        self.assertEqual(texts(rep), ["There is no fallen ally nearby."])
        self.assertTrue(self.smith["char"]["ghost"])

    def test_not_enough_ki(self):
        upkeep.state(self.monk["char"])["ki"] = 3
        rep = self.use()
        self.assertEqual(texts(rep), ["Not enough Ki (3 / 12)."])
        self.assertTrue(self.smith["char"]["ghost"])

    def test_other_slot_is_refused(self):
        rep = self.use(slot=0)                                         # Heal I, a combat skill
        self.assertEqual(texts(rep), ["That skill cannot be used here."])
        self.assertEqual(rep[0].hex(), "26070055")

    def test_party_member_first(self):
        other = self.session("o", 0x4000483, "柴田", "勝家", SMITH)
        other["char"]["ghost"] = True
        z, x, y, zz, d = players.position(self.monk)
        self.smith["players_pos"] = (z, x + 50, y, zz, d)              # farther than the other ghost
        self.assertIs(lifecycle.revive_target(self.monk), other)       # nearest without a party
        party.PARTIES[1] = {"leader": self.monk["char"]["cid"],
                            "members": [self.monk["char"]["cid"], self.smith["char"]["cid"]]}
        self.use()
        self.assertNotIn("ghost", self.smith["char"])
        self.assertTrue(other["char"]["ghost"])


# ---------------------------------------------------------------------------- the skill data
class Scrolls(Base):
    def test_revive_flags_and_menu(self):
        self.assertEqual(skills.w_r(522)[4:6], b"\x00\x20")            # field skill: 0x667cf0 wants 0x20
        self.assertEqual(skills.w_r(168)[4:6], b"\x00\x10")
        c = dict(cid=5, looks=list(MONK))
        st = skills.state(c)
        st["books"].append(dict(scroll=49, prof=2500))
        self.assertIn(522, skills.learned(c))
        self.assertNotIn(522, [e["value"] for e in skills.menu_entries(c)])

    def test_mastering_the_first_scroll_hands_out_the_second(self):
        m = self.session("m", 0x4000481, "三好", "長慶", MONK)
        res = skills.train(m, 10000, rate=1)
        self.assertTrue(res["mastered"])
        st = skills.state(m["char"])
        self.assertEqual([b["scroll"] for b in st["books"]], [48, 49])
        self.assertEqual(st["training"], 1)
        pend = skills.flush(m)
        subs = [(f[0], f[1]) for f in pend]
        self.assertIn((0x32, 2), subs)
        d = skills.parse([f for f in pend if f[:2] == b"\x32\x02"][0])
        self.assertEqual((d["book"]["slot"], d["book"]["scroll"], d["book"]["skills"][0]), (1, 49, 522))
        self.assertIn((0x32, 4), subs)
        skills.train(m, 10000, rate=1)                                 # 49 has no successor
        self.assertEqual(len(st["books"]), 2)

    def test_smith_has_no_second_scroll(self):
        s = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        skills.train(s, 10000, rate=1)
        self.assertEqual(len(skills.state(s["char"])["books"]), 1)


# ---------------------------------------------------------------------------- the shrine
class Shrine(Base):
    def setUp(self):
        super().setUp()
        self.s = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        self.n = self.s["npcs"][SHRINE]
        self.s["char"]["ghost"] = True
        self.s["char"]["level"] = 5
        items.inventory(self.s["char"])["money"] = 2000
        upkeep.state(self.s["char"])["life"] = 0

    def at_shrine(self):
        p = self.n["pos"]
        self.s["pos"] = {"x": p[1] - 100, "y": p[2], "z": p[3], "dir": 0}

    def test_spawned_in_nagoya(self):
        rec = [r for r in records(self.s["spawn"]) if r["cid"] == SHRINE][0]
        self.assertEqual((rec["family"], rec["job"], rec["b6b"]), ("Shrine Keeper", 3, 2))

    def test_paid_revive(self):
        self.at_shrine()
        out = run(bytes.fromhex("28010040"), self.s)
        self.assertNotIn("ghost", self.s["char"])
        self.assertEqual(items.inventory(self.s["char"])["money"], 1500)
        self.assertIn("2f0b00000000", [f.hex() for f in out])
        self.assertIn("The shrine revived you for 0 kan 500 mon.", texts(out))
        self.assertEqual(upkeep.state(self.s["char"])["life"], upkeep.maxima(self.s["char"])[0] // 2)

    def test_poor_ghost_is_refused(self):
        self.at_shrine()
        items.inventory(self.s["char"])["money"] = 100
        out = run(bytes.fromhex("28010040"), self.s)
        self.assertTrue(self.s["char"]["ghost"])
        self.assertEqual(texts(out)[-1], "Walk away from the shrine and press ○ to Pass On.")

    def test_away_from_the_shrine_is_pass_on(self):
        out = run(bytes.fromhex("28010040"), self.s)                   # at the spawn point: 848 units away
        self.assertEqual(out[0].hex(), "2f0b00000400")                 # battle.pass_on
        self.assertNotIn("ghost", self.s["char"])

    def test_living_player_gets_a_line(self):
        del self.s["char"]["ghost"]
        self.at_shrine()
        out = run(bytes.fromhex("28010040"), self.s)
        self.assertEqual([f[:2].hex() for f in out[:2]], ["0d06", "0d05"])


# ---------------------------------------------------------------------------- stat growth
class Growth(Base):
    def test_formula(self):
        c = dict(level=10, looks=list(SMITH))
        # Smith job base [4,5,3,2,2,3,1,2,2], creation points [2,1,5,1,1,0,0,0,0]:
        # rates 0.5 0.55 0.55 0.25 0.25 0.3 0.1 0.2 0.2 x 9 levels
        self.assertEqual(lifecycle.stats_at(c, 10), [10, 10, 12, 5, 5, 5, 1, 3, 3])
        self.assertEqual(lifecycle.stats_at(c, 1), SMITH[11:20])
        self.assertEqual(len(lifecycle.grow(c)), 8)
        self.assertEqual(c["looks"][11:20], [10, 10, 12, 5, 5, 5, 1, 3, 3])
        self.assertEqual(lifecycle.grow(c), [])                        # idempotent
        self.assertEqual(c["stat_growth"]["base"], SMITH[11:20])

    def test_level_up_pushes_the_stats_block(self):
        s = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        out = run(say("exp 2000"), s)
        blocks = [skills.parse(f) for f in out if f[:2] == b"\x2f\x07"]
        lv = s["char"]["level"]
        self.assertGreater(lv, 5)
        self.assertEqual(blocks[-1]["stats"], lifecycle.stats_at(s["char"], lv))    # P+0x10294.. (0x209280)
        self.assertEqual(blocks[-1]["left"], 0)
        line = [t for t in texts(out) if t.startswith("Your stats grew")]
        self.assertTrue(line)
        self.assertLess(texts(out).index(f"Level up! You are now level {lv}."), texts(out).index(line[0]))
        again = run(say("exp 1"), s)
        self.assertFalse([f for f in again if f[:2] == b"\x2f\x07"])
        saved = chars.CharStore(world.STORE.path).find(0x4000482, s["char"]["cid"])
        self.assertEqual(saved["looks"][11:20], lifecycle.stats_at(s["char"], lv))

    def test_growth_off(self):
        os.environ["NOBU_LIFE_GROWTH"] = "0"
        c = dict(level=30, looks=list(SMITH))
        self.assertEqual(lifecycle.grow(c), [])


# ---------------------------------------------------------------------------- promotion exams
class Exams(Base):
    def setUp(self):
        super().setUp()
        self.s = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        self.c = self.s["char"]
        self.c.update(level=15, rank=3, merit=1800)

    def talk(self, seq=0x40):
        run(bytes.fromhex("25010031") + vce.put_u32(MASTER), self.s)
        return run(bytes.fromhex("2801") + vce.put_u16(seq), self.s)

    def kill(self, kind, cid):
        self.s.setdefault("enemies_log", []).append(("defeated", cid, kind, 0))

    def ranks(self, frames):
        m = {}
        for f in frames:
            if f[0] == 0x2F:
                m.update(model_2f(f))
        return m.get(0x102c6)

    def test_yoriki_exam(self):
        out = self.talk()
        adds = [f for f in out if f[:2] == b"\x34\x02"]
        self.assertTrue(adds)
        self.assertEqual(vce.Reader(adds[0], 4).u32(), 3101)
        self.assertIn("Promotion exam: Exam: the bandit chief.", texts(out))
        self.assertEqual(self.c["exam"]["rank"], 4)
        self.kill("robber", 0x100)
        self.kill("rat", 0x101)                                        # not a bandit
        out = self.talk()
        self.assertIn("Exam: the bandit chief: 1/3 bandits defeated.", texts(out))
        self.assertEqual(self.c["rank"], 3)
        self.kill("highwayman", 0x102)
        self.kill("petty_thief", 0x103)
        out = self.talk()
        self.assertEqual(self.c["rank"], 4)
        self.assertNotIn("exam", self.c)
        self.assertEqual(self.ranks(out), 4)                           # 2F 0C / 0E -> P+0x102c6 (Status: Yoriki)
        self.assertIn("Promotion! Your rank is now Yoriki.", texts(out))
        self.assertIn("3403", [f[:2].hex() for f in out])
        out = self.talk()                                              # back to ordinary jobs
        self.assertNotIn("exam", self.c)

    def test_samurai_general_exam_takes_the_fangs(self):
        self.c.update(rank=4, merit=3500)
        self.talk()
        self.assertEqual(self.c["exam"]["rank"], 5)
        inv = items.inventory(self.c)
        used = {i["idx"] for i in inv["items"]}
        idx = next(i for i in range(items.CAPACITY) if i not in used)
        inv["items"].append(dict(id=604, type=54, cat=0, count=6, flags=items.FLAG_STACK, dur=0, dur_max=0, idx=idx))
        out = self.talk()
        self.assertEqual(self.c["rank"], 5)
        self.assertEqual(sum(i["count"] for i in inv["items"] if i["id"] == 604), 1)
        self.assertEqual(self.ranks(out), 5)

    def test_not_due_below_the_merit(self):
        self.c["merit"] = 1799
        out = self.talk()
        self.assertNotIn("exam", self.c)
        self.assertEqual(vce.Reader([f for f in out if f[:2] == b"\x34\x02"][0], 4).u32(), 2001)   # a guild job

    def test_active_job_first(self):
        self.c["guild"] = {"job": 2001, "kills": 0, "done": {}}
        self.talk()
        self.assertNotIn("exam", self.c)

    def test_detail_and_relogin(self):
        self.talk()
        rep = run(bytes([0x2A, 0x03]) + vce.put_u16(0x77) + vce.put_u32(3101), self.s)
        self.assertEqual(rep[0][:4].hex(), "34020077")
        out = run(bytes.fromhex("235a0014"), self.s)
        self.assertIn(3101, [vce.Reader(f, 4).u32() for f in out if f[:2] == b"\x34\x02"])

    def test_chat_and_cancel(self):
        out = run(say("exam"), self.s)
        self.assertIn("The Yoriki exam is open: talk to the Hall Master in Nagoya.", texts(out))
        self.talk()
        out = run(say("exam cancel"), self.s)
        self.assertNotIn("exam", self.c)
        self.assertIn("3403", [f[:2].hex() for f in out])

    def test_merit_line_points_to_the_master(self):
        from nobu import progression
        self.c.update(rank=3, merit=0)
        self.c.pop("exam_ready", None)
        res = progression.award(self.c, merit=1800)
        self.assertIn("talk to the Hall Master in Nagoya", progression.level_up_lines(res)[-1])


def patched_battle():
    from nobu import battle
    return battle


class PatchedBattle(Base):
    def setUp(self):
        super().setUp()
        self.pb = patched_battle()
        self.monk = self.session("m", 0x4000481, "三好", "長慶", MONK)
        self.smith = self.session("s", 0x4000482, "国友", "善兵衛", SMITH)
        for c in (self.monk, self.smith):
            c["got"].clear()

    def test_first_death_is_shown_to_the_zone(self):
        out = self.pb.ghost_frames(self.monk)
        self.assertEqual(params_pushes(out)[0][1][:2], b"\x02\x01")    # ghost, and still sees ghosts
        recs = records(self.smith["got"])
        self.assertEqual((recs[0]["cid"], recs[0]["params"][0]), (self.wire(self.monk, self.smith), 2))
        self.smith["got"].clear()
        self.pb.revive(self.monk)
        self.assertEqual(records(self.smith["got"])[0]["params"][0], 0)
        self.assertEqual(self.pb.ghost_params(False, self.monk["char"])[:2], b"\x00\x01")

    def test_exam_kill_hook(self):
        self.smith["char"].update(level=15, rank=3, merit=1800, exam=dict(rank=4, kills=0))
        src = getattr(self.pb, "SOURCE", None) or open(self.pb.__file__, encoding="utf-8").read()
        self.assertIn("out += lifecycle.on_kill(ctx, S.get(\"target\"), S.get(\"store\"))", src)
        out = lifecycle.on_kill(self.smith, "robber")
        self.assertEqual(self.smith["char"]["exam"]["kills"], 1)
        self.assertIn(b"\x34\x04", [f[:2] for f in out])


if __name__ == "__main__":
    unittest.main()
