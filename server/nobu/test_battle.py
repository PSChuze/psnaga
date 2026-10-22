"""Battle entry / exit and the debug trigger: python -m unittest nobu.test_battle -v"""
import os
import unittest

from nobu import battle, chars, clientmodel, social, vce

ME = dict(cid=2, uid=0x04000471, slot=0, family="ああ", given="ああ",
          looks=[4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2], pos=[10, 100460, 3455, 95716, 0])
KNOBS = ("NOBU_BATTLE_FIGHT", "NOBU_BATTLE_SUB", "NOBU_BATTLE_B10", "NOBU_BATTLE_B14", "NOBU_BATTLE_IDS", "NOBU_BATTLE_CHAT",
         "NOBU_DEBUG_ENEMY", "NOBU_ENEMY_CID", "NOBU_ENEMY_DX", "NOBU_ENEMY_DZ", "NOBU_ENEMY_DIR",
         "NOBU_ENEMY_FAMILY", "NOBU_ENEMY_JOB", "NOBU_ZONE", "NOBU_AREA", "NOBU_START_TOWN", "NOBU_FORCE_HOME")


def say(text, sub=0x08, seq=0x31):
    return bytes([0x23, sub]) + vce.put_u16(seq) + vce.put_bytes(text.encode("utf-8"))


class Env(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)


class Layouts(Env):
    def test_battle_info_matches_reader_0x1a6770(self):
        p = battle.battle_info(0xA, 7, 100460, 3455, 95716, b10=2, b14=3, ids=[0xCE, 0xCF])
        self.assertEqual(p.hex(), "0f0a" "00000007" "0001886c" "00000d7f" "000175e4"
                                  "0200000003" "02" "00ce00cf")
        d = battle.parse(p)
        self.assertEqual((d["sub"], d["bid"], d["x"], d["y"], d["z"]), (0xA, 7, 100460, 3455, 95716))
        self.assertEqual((d["b10"], d["b14"], d["ids"], d["left"]), (2, 3, [0xCE, 0xCF], 0))

    def test_every_sub_parses_to_the_last_byte(self):
        for p in (battle.battle_info(1), battle.battle_info(0xC, ids=list(range(11))),
                  battle.battle_ref(2, 1, 2, 3, 4), battle.battle_ref(0xB), battle.battle_end(1, 2),
                  battle.battle_u32(9), battle.battle_leave(1, 1), battle.battle_location(1, 2, 3, 4),
                  battle.char_battle_state(2, 1, 5, 6, 7), battle.char_battle_state(2, 2),
                  battle.battle_update(5, 6)):
            self.assertEqual(battle.parse(p)["left"], 0, p.hex())

    def test_limits(self):
        with self.assertRaises(ValueError):
            battle.battle_info(4)
        with self.assertRaises(ValueError):
            battle.battle_info(ids=range(12))
        with self.assertRaises(ValueError):
            battle.parse(bytes([0x0F, 0x0D]))
        with self.assertRaises(vce.WireError):
            battle.parse(bytes.fromhex("0f0a0000"))

    def test_sub8_kind2_has_no_position(self):
        self.assertEqual(battle.char_battle_state(2, 2).hex(), "0f080000000202")
        self.assertNotIn("a", battle.parse(battle.char_battle_state(2, 2)))

    def test_scene_effects(self):
        self.assertEqual(battle.scene_effect(0xA), "battle")
        self.assertIsNone(battle.scene_effect(1, player_active=True))
        self.assertEqual(battle.scene_effect(1, player_active=False), "battle")
        self.assertEqual(battle.scene_effect(4, scene=4), "world")
        self.assertEqual(battle.scene_effect(4, scene=5), "task95")
        self.assertEqual(battle.scene_effect(7, scene=5), None)

    def test_describe(self):
        self.assertIn("BATTLE_INFO_NOW", battle.describe(battle.battle_info(), "S>C"))
        self.assertIn("BATTLE_END", battle.describe(battle.battle_end(), "S>C"))
        self.assertIn("battle-scene say", battle.describe(say("x", 0x78)))
        self.assertIn("debug battle command", battle.describe(say("/s/battle")))
        self.assertIsNone(battle.describe(say("/s/hello")))


class ChatTrigger(Env):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_BATTLE_CHAT"] = "1"

    def test_chat_trigger_is_opt_in(self):
        os.environ.pop("NOBU_BATTLE_CHAT")
        self.assertIsNone(battle.answer(say("/s/battle"), self.ctx()))        # plain live env: off
        os.environ["NOBU_DEBUG_ENEMY"] = "1"
        self.assertIsNotNone(battle.answer(say("/s/battle"), self.ctx()))     # debug rig: on
        os.environ.pop("NOBU_DEBUG_ENEMY")
        os.environ["NOBU_BATTLE_FIGHT"] = "1"
        self.assertIsNotNone(battle.answer(say("/s/battle"), self.ctx()))
        os.environ["NOBU_BATTLE_CHAT"] = "0"
        self.assertIsNone(battle.answer(say("/s/battle"), self.ctx()))        # explicit off wins
        os.environ.pop("NOBU_BATTLE_FIGHT")
        os.environ["NOBU_BATTLE_CHAT"] = "1"
        self.assertIsNotNone(battle.answer(say("/s/battle"), self.ctx()))     # explicit on

    def ctx(self):
        return {"char": dict(ME), "cid": 2, "uid": ME["uid"]}

    def test_parse_command(self):
        self.assertEqual(battle.parse_command("battle"), ("start", []))
        self.assertEqual(battle.parse_command("/battle c 1 2 206"), ("start", [0xC, 1, 2, 206]))
        self.assertEqual(battle.parse_command("ｂａｔｔｌｅ　ｅｎｄ"), ("end", []))
        self.assertEqual(battle.parse_command("！battle"), ("start", []))
        self.assertIsNone(battle.parse_command("battles"))
        self.assertIsNone(battle.parse_command("hello battle"))

    def test_say_battle_sends_battle_info_now(self):
        ctx = self.ctx()
        reps = battle.answer(say("/s/battle"), ctx)
        self.assertEqual(reps[0], social.notify_chat(2, "battle"))           # echo kept
        self.assertEqual(reps[1][:2].hex(), "0d06")                          # system line
        d = battle.parse(reps[2])
        self.assertEqual((d["sub"], d["bid"], (d["x"], d["y"], d["z"])), (0xA, 1, (100460, 3455, 95716)))
        self.assertTrue(ctx["battle"]["active"])
        # second start -> next battle id; end -> 0F 04 with that id and our cid
        battle.answer(say("/s/battle"), ctx)
        end = battle.answer(say("/s/battle end"), ctx)[-1]
        self.assertEqual(end.hex(), "0f04" "00000002" "00000002" "0000")
        self.assertFalse(ctx["battle"]["active"])

    def test_args_and_env(self):
        os.environ["NOBU_BATTLE_SUB"] = "1"
        os.environ["NOBU_BATTLE_IDS"] = "0xce,0xcf"
        d = battle.parse(battle.answer(say("/s/battle"), self.ctx())[-1])
        self.assertEqual((d["sub"], d["ids"]), (1, [0xCE, 0xCF]))
        d = battle.parse(battle.answer(say("/s/battle c 5 6 7"), self.ctx())[-1])
        self.assertEqual((d["sub"], d["b10"], d["b14"], d["ids"]), (0xC, 5, 6, [7]))

    def test_battle_scene_say_is_accepted_without_echo(self):
        reps = battle.answer(say("battle leave", 0x78), self.ctx())
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0f06"])

    def test_other_chat_is_not_ours(self):
        self.assertIsNone(battle.answer(say("/s/hello"), self.ctx()))
        os.environ["NOBU_BATTLE_CHAT"] = "0"
        self.assertIsNone(battle.answer(say("/s/battle"), self.ctx()))

    def test_moved_position_is_used(self):
        ctx = self.ctx()
        ctx["pos"] = dict(x=1, y=2, z=3, dir=0, area=10)
        d = battle.parse(battle.answer(say("/s/battle"), ctx)[-1])
        self.assertEqual((d["x"], d["y"], d["z"]), (1, 2, 3))


class DebugEnemy(Env):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_DEBUG_ENEMY"] = "1"
        self.ctx = {"char": dict(ME), "cid": 2, "uid": ME["uid"]}

    def test_disabled_by_default(self):
        os.environ.pop("NOBU_DEBUG_ENEMY")
        self.assertEqual(battle.debug_spawn(self.ctx), [])

    def test_enemy_is_an_attackable_uid0_actor(self):
        (push,) = battle.debug_spawn(self.ctx)
        (rec,) = clientmodel.parse_notify_char(push)["records"]            # 0x1a2500 to the last byte
        self.assertEqual((rec["uid"], rec["cid"], rec["power"]), (0, battle.DEFAULT_ENEMY_CID, 0))
        self.assertTrue(clientmodel.npc_actor_created(rec))
        self.assertNotEqual(rec["b6b"], 2)                                  # +0x6b == 2: never attackable
        self.assertEqual(rec["pos"], (100460 - 300, 3455, 95716))
        self.assertEqual(rec["area"], chars.area_for(10))

    def test_target_then_circle_starts_the_battle(self):
        battle.debug_spawn(self.ctx)
        cid = battle.DEFAULT_ENEMY_CID
        rep = battle.answer(bytes.fromhex("25010040") + vce.put_u32(cid), self.ctx)
        r = vce.Reader(rep[0], 2)
        self.assertEqual((r.u16(), r.i32(), r.u32()), (0x40, 0, cid))
        (start,) = battle.answer(bytes.fromhex("28010041"), self.ctx)
        self.assertEqual(battle.parse(start)["sub"], 0xA)
        self.assertEqual(self.ctx["battle_target"], cid)

    def test_other_targets_fall_through(self):
        battle.debug_spawn(self.ctx)
        self.assertIsNone(battle.answer(bytes.fromhex("25010040") + vce.put_u32(0xA00001), self.ctx))
        self.ctx["target"] = 0xA00001
        self.assertIsNone(battle.answer(bytes.fromhex("28010041"), self.ctx))

    def test_bad_cid_rejected(self):
        os.environ["NOBU_ENEMY_CID"] = "0xff000001"
        with self.assertRaises(ValueError):
            battle.debug_spawn(self.ctx)


class Fight(Env):
    """NOBU_BATTLE_FIGHT=1: the 1v1 sequence through world.answer (minimum viable fight)."""
    UID = 0x4000471
    MORE = ("NOBU_BATTLE_INTRO", "NOBU_BATTLE_FIGHT", "NOBU_BATTLE_OWN_SIDE", "NOBU_BATTLE_ATTACK_ID", "NOBU_BATTLE_END_ACTION",
            "NOBU_BATTLE_AC_HEX", "NOBU_BATTLE_END_AC_HEX", "NOBU_BATTLE_TICK", "NOBU_BATTLE_TICK_DELAY",
            "NOBU_BATTLE_EXP", "NOBU_BATTLE_MONEY", "NOBU_BATTLE_BANNER", "NOBU_DEBUG_NPC", "NOBU_SPAWN",
            "NOBU_BATTLE_ONE_HIT", "NOBU_BATTLE_LIFE", "NOBU_BATTLE_KI", "NOBU_BATTLE_ENEMY_LIFE",
            "NOBU_BATTLE_ENEMY_KI", "NOBU_BATTLE_SEED", "NOBU_BATTLE_FLEE", "NOBU_BATTLE_ENEMY_ATK",
            "NOBU_BATTLE_ENEMY_DEF", "NOBU_BATTLE_DEATH_PENALTY", "NOBU_BATTLE_ATTACK_CODE", "NOBU_BATTLE_MENU_ROOT",
            "NOBU_BATTLE_WINDOW", "NOBU_AREA", "NOBU_ZONE", "NOBU_BATTLE_GHOST", "NOBU_DEATH_MONEY_PCT",
            "NOBU_DEATH_FAME_PCT", "NOBU_DEATH_MIN_LEVEL", "NOBU_BATTLE_CHAT", "NOBU_GUILD", "NOBU_SKILL_START_PROF")

    def setUp(self):
        super().setUp()
        import tempfile
        from nobu import world
        self.world = world
        self.more = {k: os.environ.pop(k) for k in list(os.environ) if k in self.MORE}
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        os.environ.update(NOBU_BATTLE_FIGHT="1", NOBU_DEBUG_ENEMY="1", NOBU_BATTLE_ONE_HIT="1", NOBU_BATTLE_LIFE="100",
                          NOBU_BATTLE_KI="50", NOBU_BATTLE_ENEMY_LIFE="100", NOBU_BATTLE_ENEMY_KI="50",
                          NOBU_SKILL_START_PROF="0")               # no skill learned: no Skills items in the menu
        self.ctx = {"uid": self.UID}
        body = chars.write_create(dict(family="織田", given="信長", looks=ME["looks"]))
        world.answer(bytes([5, 1]) + vce.put_u32(self.UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(self.UID) + vce.put_u32(1), self.ctx)
        world.answer(b"\x04", self.ctx)
        self.spawn = world.answer(bytes.fromhex("235a0013"), self.ctx)

    def tearDown(self):
        self.world.STORE = None
        for k in self.MORE:
            os.environ.pop(k, None)
        os.environ.update(self.more)
        self.tmp.cleanup()
        super().tearDown()

    def ans(self, hexstr):
        return self.world.answer(bytes.fromhex(hexstr), self.ctx)

    def start(self):
        cid = battle.DEFAULT_ENEMY_CID
        self.world.answer(bytes.fromhex("25010040") + vce.put_u32(cid), self.ctx)
        return self.ans("28010041")

    def test_full_sequence(self):
        me = self.ctx["cid"]
        bandit = battle.DEFAULT_ENEMY_CID
        self.assertIn("0b0a", [p[:2].hex() for p in self.spawn])          # the Bandit was spawned
        frames = self.start()
        self.assertEqual(battle.parse(frames[0])["sub"], 0xA)            # 0F 0a first
        parsed = [battle.parse22(f) for f in frames[1:]]
        self.assertEqual([p["type"] for p in parsed],
                         [0x10, 0x02, 0x33, 0x33, 0x1F, 0x1F, 0x1A, 0x1A, 0x1C, 0x1C, 0x6E, 0x3C, 0x66, 0x65])
        self.assertEqual([(p["slot"], p["before"]["life"], p["after"]["life"]) for p in parsed[8:10]],
                         [(0, 100, 100), (7, 100, 100)])                 # current record's unit grids
        self.assertEqual(parsed[1]["bi"]["ids"], parsed[0]["bi"]["ids"])  # staging carries the ids
        self.assertTrue(all(p["left"] == 0 for p in parsed))
        cur = parsed[0]                                                  # record 1 = the command window
        self.assertEqual((cur["clock"], cur["rec"], cur["start"], cur["state"], cur["b5"]), (1, 1, 1, 0, 1))
        self.assertEqual((cur["bi"]["kind"], cur["bi"]["code"], cur["bi"]["dur"], cur["ar1"]["root"],
                          cur["ar2"]["root"]), (1, 0x157E, 3, 0, 0x11))                # intro, then menu
        self.assertEqual(battle.parse(frames[0])["b10"], 1)             # stage 1
        ids = cur["bi"]["ids"]
        self.assertEqual((ids[0], ids[7]), (me, bandit))
        self.assertEqual((parsed[2]["value"], parsed[2]["key"], parsed[2]["ttype"], parsed[2]["cost"],
                          parsed[2]["item_name"]), (1, 1, 1, 0, "Attack"))     # ttype 0 = greyed
        self.assertEqual([p["us"]["flags"] for p in parsed[6:8]], [0x0B, 0x0C])           # 22 1a target flags
        self.assertEqual([(p["before"]["flags"], p["after"]["flags"]) for p in parsed[8:10]],
                         [(0x0B, 0x0B), (0x0C, 0x0C)])                                    # 22 1c target flags
        self.assertEqual((parsed[3]["value"], parsed[3]["key"], parsed[3]["ttype"], parsed[3]["item_name"]),
                         (2, 4, 3, "Flee"))
        self.assertEqual(parsed[4]["ui"]["family"], "織田")
        self.assertEqual(parsed[6]["us"]["life"], 100)
        self.assertEqual(self.ctx["battle"]["ticker"], "no loop")        # no asyncio loop in tests
        self.assertEqual(self.ans("2262"), [])                           # logged only
        (info,) = [battle.parse22(f) for f in self.ans("22420001")]      # unlocks menu+0x81
        self.assertEqual((info["type"], info["key"], info["item_name"], info["left"]), (0x44, 1, "Attack", 0))
        ready = [battle.parse22(f) for f in self.ans("2232")]
        self.assertEqual([p["type"] for p in ready], [0x03, 0x03, 0x18, 0x11])
        self.assertEqual((ready[0]["cid"], ready[-1]["secs"]), (me, 1))  # clock starts at t0
        cmd = "2201" + bandit.to_bytes(4, "big").hex() + "00" * 12 + "0001" + "0000"
        self.assertIn("action=0x1", self.world.describe(bytes.fromhex(cmd), "C>S"))
        p = [battle.parse22(f) for f in self.ans(cmd)]
        self.assertEqual([x["type"] for x in p],
                         [0x03, 0x02, 0x0F, 0x0F, 0x1E, 0x3C, 0x02, 0x0F, 0x0F, 0x1E, 0x5D, 0x25])   # no 22 5c
        self.assertTrue(all(x["left"] == 0 for x in p))
        self.assertEqual((p[0]["cid"], p[0]["b"][2]), (me, 1))                           # command entered
        self.assertEqual(p[5]["slots"][0], (me, 8, 2))                                  # the bar: me, 8 s, rec 2
        p = p[1:5] + p[6:]
        hit, end = p[0], p[4]
        self.assertEqual((hit["rec"], hit["start"], hit["state"]), (2, 2, 1))              # clock 1 + 1, timed
        self.assertEqual((hit["bi"]["code"], hit["bi"]["kind"], hit["bi"]["dur"], hit["bi"]["actor"],
                          hit["bi"]["target"], hit["bi"]["third"], hit["bi"]["cam"]), (4, 1, 8, 0, 7, 0xFF, 0))
        self.assertEqual((hit["bi"]["list_a"][:2], hit["bi"]["list_b"][0]), ([7, 0xFF], 0xFF))
        self.assertEqual([(x["slot"], x["before"]["life"], x["after"]["life"]) for x in (p[1], p[2], p[5], p[6])],
                         [(0, 100, 100), (7, 100, 0), (0, 100, 100), (7, 0, 0)])
        self.assertEqual((p[2]["before"]["flags"], p[2]["after"]["flags"], p[2]["after"]["status"]), (0x0C, 0x14, 1))
        self.assertEqual((p[1]["after"]["ki"], p[1]["after"]["max_ki"], p[1]["after"]["max_life"]), (50, 50, 100))
        self.assertEqual((end["rec"], end["start"], end["state"], end["bi"]["code"], end["bi"]["kind"],
                          end["bi"]["dur"], end["bi"]["target"]), (3, 11, 1, 0x157F, 1, 5, 0xFF))
        r = p[9]
        self.assertEqual((r["exp"], r["prof"], r["fame"], r["money"], r["win"], r["w32"]), (10, 5, 1, 30, 1, 0))
        c = self.world.store().find(self.UID, me)
        money0 = c["inventory"]["money"] - 30
        self.assertEqual((c["exp"], c["prof"], c["fame"]), (10, 5, 1))  # persisted
        from nobu import items
        self.assertEqual(items.money_split(c["inventory"]["money"]), items.money_split(money0 + 30))
        self.assertEqual(self.ans("28010041"), [])                        # no second battle meanwhile
        self.assertEqual(battle.parse22(self.ans("225e0000")[0])["key"], 0)
        self.assertEqual(battle.parse22(self.ans("223d03")[0])["key"], 3)
        self.assertEqual(self.ans("2256"), [])
        self.assertEqual(self.ans("2253"), [])
        self.assertEqual(self.ans("2254"), [bytes.fromhex("2247")])
        done = self.ans("2255")                                           # no loop: 0F 06 in the reply
        self.assertEqual(done[0], bytes.fromhex("2267"))
        self.assertEqual(battle.parse(done[1])["sub"], 6)
        self.assertEqual(done[2][:2].hex(), "0b04")                       # Bandit removed from the field
        self.assertEqual(self.ctx["battle"]["phase"], "done")
        self.assertEqual(self.ans("2268"), [])                            # already ended
        self.assertIn("BATTLE_PKT C>S 22 68 END_PH9 hex=2268", self.world.describe(bytes.fromhex("2268"), "C>S"))
        # back in the field: the Bandit stays away until NOBU_BATTLE_RESPAWN has passed
        self.assertEqual(battle.debug_spawn(self.ctx), [])
        self.ctx["enemy_defeated_at"] -= 31
        self.assertEqual(battle.debug_spawn(self.ctx)[0][:2].hex(), "0b0a")
        self.assertEqual(len(self.start()), 15)                          # a new battle can start

    def test_win_levels_up(self):
        from nobu import progression
        os.environ["NOBU_BATTLE_EXP"] = "100000"
        self.MORE += ("NOBU_BATTLE_EXP",)
        me = self.ctx["cid"]
        self.start()
        self.ans("2232")
        p = [battle.parse22(f) for f in self.ans("2201" + battle.DEFAULT_ENEMY_CID.to_bytes(4, "big").hex()
                                                  + "00" * 12 + "0001" + "0000")]
        self.assertEqual(p[-1]["exp"], 100000)
        c = self.world.store().find(self.UID, me)
        self.assertEqual(c["level"], progression.level_for(100000))
        self.assertGreater(c["level"], 1)
        done = self.ans("2255")                                          # exit: 0F 06, 0B 04, then status
        kinds = [f[:2].hex() for f in done]
        self.assertIn("2f0d", kinds)
        self.assertIn(f"Level up! You are now level {c['level']}.".encode("utf-8"), b"".join(done))

    def attack(self):
        cmd = "2201" + battle.DEFAULT_ENEMY_CID.to_bytes(4, "big").hex() + "00" * 12 + "0001" + "0000"
        return [battle.parse22(f) if f[0] == 0x22 else f for f in self.ans(cmd)]

    def test_rounds_until_the_enemy_falls(self):
        os.environ.pop("NOBU_BATTLE_ONE_HIT")
        os.environ.update(NOBU_BATTLE_SEED="7", NOBU_BATTLE_ENEMY_ATK="1", NOBU_BATTLE_ENEMY_LIFE="120")
        self.start()
        self.ans("2232")
        S = self.ctx["battle"]["S"]
        me, foe = S["units"]
        self.assertEqual((me["c"]["atk"] > 0, foe["c"]["atk"], foe["hp"]), (True, 1, 120))
        rounds, ends = 0, None
        while rounds < 30:
            rounds += 1
            p = self.attack()
            recs = [x for x in p if isinstance(x, dict) and x["type"] == 0x02]
            if any(isinstance(x, dict) and x["type"] == 0x25 for x in p):
                ends = p
                break
            self.assertEqual(len(recs), 2)                                  # player, then enemy
            hero, foe = recs
            self.assertEqual((hero["bi"]["actor"], hero["bi"]["target"], foe["bi"]["actor"], foe["bi"]["target"]),
                             (0, 7, 7, 0))
            self.assertEqual((hero["ar2"]["root"], foe["ar2"]["root"]), (0, 0x11))   # menu after the enemy
            self.assertEqual(foe["start"], hero["start"] + hero["bi"]["dur"])        # back to back
            self.assertIn(hero["bi"]["code"], (1, 3, 5, 0xA, 0xF))
            self.assertEqual(p[-1]["type"], 0x61)                                    # menu lock cleared
        self.assertIsNotNone(ends)
        self.assertGreater(rounds, 1)                                               # 120 Life: several rounds
        res = next(x for x in ends if isinstance(x, dict) and x["type"] == 0x25)
        self.assertEqual((res["win"], res["exp"]), (1, 10))
        kill = [x for x in ends if isinstance(x, dict) and x["type"] == 0x02][0]
        self.assertEqual(kill["bi"]["code"], 4)
        states = [x for x in ends if isinstance(x, dict) and x["type"] == 0x0F]
        self.assertEqual(states[1]["after"]["life"], 0)
        hp = [r["target_hp"] for r in self.ctx["battle_rounds"] if r["who"] == "player"]
        self.assertEqual(hp, sorted(hp, reverse=True))

    def lose(self):
        os.environ.pop("NOBU_BATTLE_ONE_HIT", None)
        os.environ.update(NOBU_BATTLE_SEED="1", NOBU_BATTLE_ENEMY_ATK="500", NOBU_BATTLE_ENEMY_LIFE="9999")
        self.start()
        self.ans("2232")
        for _ in range(10):
            p = self.attack()
            if any(isinstance(x, dict) and x["type"] == 0x25 for x in p):
                break
        self.ans("2255")
        self.ctx["zone"] = 10                                   # the fight was in the Owari field (map 10)
        self.assertIsNone(battle.answer(bytes.fromhex("235a0020"), self.ctx, self.world.store))
        return self.ctx.pop("defeat_frames")

    def test_loss_makes_a_ghost_then_pass_on(self):
        from nobu import items
        os.environ.update(NOBU_DEATH_MONEY_PCT="50", NOBU_DEATH_FAME_PCT="10", NOBU_DEATH_MIN_LEVEL="5")
        frames = self.lose()
        self.assertEqual(frames[0].hex(), "2f0b00000201")                             # "Died of battle."
        r = vce.Reader(frames[1], 2)
        self.assertEqual((r.u16(), r.u32(), r.u8()), (0, self.ctx["cid"], 1))
        params = r.bytes(0xF)
        self.assertEqual((params[0], params[6], len(params)), (2, 100, 15))          # ghost, slope kept
        me = self.ctx["cid"]
        c = self.world.store().find(self.UID, me)
        self.assertTrue(c["ghost"])
        # a map reload re-sends only the PARAMS (the spawn CHAR_INFO has PARAMS[0] = 0)
        self.assertIsNone(battle.answer(bytes.fromhex("235a0021"), self.ctx, self.world.store))
        again = self.ctx.pop("defeat_frames")
        self.assertEqual([f[:2].hex() for f in again], ["2f0b", "2f05"])
        self.assertEqual(again[0][-2:].hex(), "0401")                                 # silent
        self.assertEqual(self.world.answer(say("/s/battle"), self.ctx)[-1][:2].hex(), "0d06")   # cannot fight
        c["level"], c["fame"] = 6, 100
        money = items.inventory(c)["money"]
        out = self.ans("28010050")                                                     # ○ = Pass On
        self.assertEqual(out[0].hex(), "2f0b00000400")
        self.assertEqual(vce.Reader(out[1], 9).bytes(0xF)[0], 0)
        self.assertIn(b"graveyard", out[2])
        self.assertEqual(out[-1][:2].hex(), "0b02")                                    # to Nagoya
        self.assertEqual(int.from_bytes(out[-1][3:5], "big"), 110)
        self.assertNotIn("ghost", c)
        self.assertEqual((items.inventory(c)["money"], c["fame"]), (money - money // 2, 90))

    def test_revive_command(self):
        os.environ["NOBU_BATTLE_CHAT"] = "1"
        self.lose()
        out = self.world.answer(say("/s/battle revive"), self.ctx)
        kinds = [f[:2].hex() for f in out]
        self.assertIn("2f05", kinds)
        self.assertIn("2f0b", kinds)
        self.assertEqual(out[kinds.index("2f0b")].hex(), "2f0b00000000")                # "Revived!"
        self.assertNotIn("ghost", self.world.store().find(self.UID, self.ctx["cid"]))

    def test_defeat_goes_back_to_town(self):
        from nobu import items
        os.environ["NOBU_BATTLE_GHOST"] = "0"                   # fallback: the instant respawn
        os.environ.pop("NOBU_BATTLE_ONE_HIT")
        os.environ.update(NOBU_BATTLE_SEED="1", NOBU_BATTLE_ENEMY_ATK="500", NOBU_BATTLE_ENEMY_LIFE="9999",
                          NOBU_BATTLE_DEATH_PENALTY="10")
        self.start()
        self.ans("2232")
        p = []
        for _ in range(10):
            p = self.attack()
            if any(isinstance(x, dict) and x["type"] == 0x25 for x in p):
                break
        res = next(x for x in p if isinstance(x, dict) and x["type"] == 0x25)
        self.assertEqual((res["win"], res["exp"], res["money"]), (0, 0, 0))            # defeat, no rewards
        end = [x for x in p if isinstance(x, dict) and x["type"] == 0x02][-1]
        self.assertEqual((end["bi"]["code"], end["bi"]["actor"]), (0x158F, 7))
        me = self.ctx["cid"]
        c = self.world.store().find(self.UID, me)
        money = items.inventory(c)["money"]
        self.assertNotIn("exp", c)
        done = self.ans("2255")
        self.assertEqual(battle.parse(done[1])["sub"], 6)
        self.assertTrue(self.ctx["defeat_pending"])
        self.ctx["zone"] = 10                                   # the fight was in the Owari field (map 10)
        self.assertIsNone(battle.answer(bytes.fromhex("235a0020"), self.ctx, self.world.store))
        frames = self.ctx.pop("defeat_frames")
        self.assertIn(b"defeated", frames[0])
        self.assertEqual(frames[-1][:2].hex(), "0b02")                                   # WARPED to town
        self.assertEqual(int.from_bytes(frames[-1][3:5], "big"), 110)
        self.assertEqual(items.inventory(c)["money"], money - money // 10)
        self.assertEqual(self.world.store().find(self.UID, me)["pos"][0], 110)

    def test_flee(self):
        self.start()
        self.ans("2232")
        cmd = "2201" + battle.DEFAULT_ENEMY_CID.to_bytes(4, "big").hex() + "00" * 12 + "0002" + "0000"
        out = self.ans(cmd)
        self.assertIn(b"fled", out[0])
        self.assertEqual(battle.parse(out[1])["sub"], 6)
        self.assertEqual(self.ctx["battle"]["phase"], "done")
        self.assertFalse(self.ctx.get("defeat_pending"))

    def test_formula(self):
        import random
        a = dict(level=5, dex=5, atk=30, dfn=10)
        d = dict(level=5, dex=5, atk=10, dfn=10)
        rng = random.Random(3)
        rolls = [battle.roll_attack(rng, a, d) for _ in range(400)]
        hits = [x for o, x in rolls if o in ("hit", "crit")]
        self.assertTrue(0.75 < len(hits) / 400 < 0.95)                              # 85 % chance
        self.assertTrue(all(22 <= x <= 42 for x in hits))                            # 30*U(.9,1.1)-5, x1.5
        self.assertEqual({o for o, _ in rolls} - {"hit", "crit"}, {"dodge", "block", "parry"})
        self.assertEqual(battle.scene_code("hit", True), 4)
        self.assertEqual([battle.scene_code(o, False) for o in ("hit", "dodge", "block", "parry")],
                         [1, 5, 0xA, 0xF])

    def test_knobs(self):
        os.environ["NOBU_BATTLE_OWN_SIDE"] = "b"
        os.environ["NOBU_BATTLE_ATTACK_CODE"] = "0x11"
        os.environ["NOBU_BATTLE_END_ACTION"] = "0x1591"
        os.environ["NOBU_BATTLE_BANNER"] = "0"
        os.environ["NOBU_BATTLE_MENU_ROOT"] = "0x12"
        os.environ["NOBU_BATTLE_WINDOW"] = "30"
        os.environ["NOBU_BATTLE_INTRO"] = "1"
        os.environ["NOBU_BATTLE_ATTACK_ID"] = "0x40"
        frames = self.start()
        cur = battle.parse22(frames[1])
        self.assertEqual((cur["bi"]["ids"][0], cur["bi"]["ids"][7]), (battle.DEFAULT_ENEMY_CID, self.ctx["cid"]))
        self.assertEqual((cur["ar2"]["root"], cur["bi"]["dur"]), (0x12, 3))
        self.assertEqual([battle.parse22(f)["type"] for f in frames[1:4]], [0x10, 0x02, 0x33])
        self.assertEqual(battle.parse22(frames[3])["value"], 0x40)
        self.ans("2232")
        p = [battle.parse22(f) for f in self.ans("2201" + "00" * 20)]
        self.assertEqual([x["type"] for x in p], [0x03, 0x02, 0x0F, 0x0F, 0x1E, 0x3C, 0x02, 0x0F, 0x0F, 0x1E, 0x25])
        p = p[1:5] + p[6:]
        self.assertEqual((p[0]["bi"]["code"], p[0]["bi"]["actor"], p[0]["bi"]["target"]), (0x11, 7, 0))
        self.assertEqual(p[4]["bi"]["code"], 0x1591)
        self.ans("2268")                                                  # leave, fight again
        os.environ["NOBU_BATTLE_AC_HEX"] = "ff" * 24
        self.start()
        self.ans("2232")
        self.assertEqual(battle.parse22(self.ans("2201" + "00" * 20)[4])["ac1"], b"\xff" * 24)

    def test_exit_chat_and_flag_off(self):
        self.start()
        reps = self.world.answer(say("/s/battle exit"), self.ctx)
        self.assertEqual(battle.parse(reps[-1])["sub"], 6)
        self.assertEqual(self.ctx["battle"]["phase"], "done")
        os.environ["NOBU_BATTLE_FIGHT"] = "0"
        self.assertEqual(len(self.start()), 1)                            # bare 0F 0a
        self.assertIsNone(battle.answer(bytes.fromhex("2232"), self.ctx))

    def test_ticker_pushes_turn_until_ready(self):
        import asyncio
        os.environ["NOBU_BATTLE_TICK"] = "0.01"
        os.environ["NOBU_BATTLE_TICK_DELAY"] = "0.01"
        pushed = []
        self.ctx["push"] = pushed.append

        async def run():
            self.start()
            await asyncio.sleep(0.2)
            self.ans("2232")
            n = pushed.count(bytes.fromhex("2218"))
            await asyncio.sleep(0.1)
            self.ans("2201" + "00" * 20)
            self.ans("2268")                                              # battle over: ticker stops
            m = len(pushed)
            await asyncio.sleep(0.05)
            return n, m
        n, m = asyncio.run(run())
        self.assertGreaterEqual(n, 2)
        self.assertEqual(pushed.count(bytes.fromhex("2218")), n)         # 22 18 stopped on 22 32
        self.assertGreaterEqual(sum(p[:2] == bytes.fromhex("2211") for p in pushed), 3)  # clock kept going
        self.assertEqual(len(pushed), m)                                 # everything stopped on 0F 06

    def test_ticker_stops_when_push_fails(self):
        import asyncio
        os.environ["NOBU_BATTLE_TICK"] = "0.01"
        os.environ["NOBU_BATTLE_TICK_DELAY"] = "0.01"

        def boom(_):
            raise ConnectionResetError("gone")
        self.ctx["push"] = boom

        async def run():
            self.start()
            await asyncio.sleep(0.05)
        asyncio.run(run())
        self.assertTrue(self.ctx["battle"]["ticker"].startswith("stopped"))


class PartyBattle(Env):
    """Two sessions in one party: both enter the same battle, commands are collected, one shared round."""
    MORE = ("NOBU_BATTLE_FIGHT", "NOBU_BATTLE_ONE_HIT", "NOBU_BATTLE_SEED", "NOBU_BATTLE_ENEMY_ATK",
            "NOBU_BATTLE_ENEMY_LIFE", "NOBU_BATTLE_LIFE", "NOBU_BATTLE_KI", "NOBU_SPAWN", "NOBU_DEBUG_NPC",
            "NOBU_BATTLE_ENEMY_COUNT")

    def setUp(self):
        super().setUp()
        import tempfile
        from nobu import party, world
        self.world, self.party = world, party
        self.more = {k: os.environ.pop(k) for k in list(os.environ) if k in self.MORE}
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        os.environ.update(NOBU_BATTLE_FIGHT="1", NOBU_DEBUG_ENEMY="1", NOBU_BATTLE_SEED="5",
                          NOBU_BATTLE_ENEMY_ATK="1", NOBU_BATTLE_ENEMY_LIFE="500")
        social.SESSIONS.clear()
        party.reset()
        self.a = self.login("aaa", 0x4000471, "織田", "信長", 1)
        self.b = self.login("bbb", 0x4000472, "柴田", "勝家", 2)
        party.PARTIES[1] = dict(leader=self.a["cid"], members=[self.a["cid"], self.b["cid"]])

    def login(self, sid, uid, family, given, cid):
        ctx = {"uid": uid}
        ctx["push"] = ctx.setdefault("pushed", []).append
        body = chars.write_create(dict(family=family, given=given, looks=ME["looks"]))
        self.world.answer(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body, ctx)
        self.world.answer(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(cid), ctx)
        self.world.answer(b"\x04", ctx)
        social.register(sid, ctx)
        self.world.answer(bytes.fromhex("235a0013"), ctx)
        del ctx["pushed"][:]
        return ctx

    def tearDown(self):
        from nobu import party
        self.world.STORE = None
        social.SESSIONS.clear()
        party.reset()
        for k in self.MORE:
            os.environ.pop(k, None)
        os.environ.update(self.more)
        self.tmp.cleanup()
        super().tearDown()

    def ans(self, ctx, hexstr):
        return self.world.answer(bytes.fromhex(hexstr), ctx)

    def attack(self, ctx):
        return self.ans(ctx, "2201" + battle.DEFAULT_ENEMY_CID.to_bytes(4, "big").hex() + "00" * 12 + "0001" + "0000")

    def engage(self):
        cid = battle.DEFAULT_ENEMY_CID
        self.world.answer(bytes.fromhex("25010040") + vce.put_u32(cid), self.a)
        return self.ans(self.a, "28010041")

    def test_party_joins_and_shares_rounds(self):
        from nobu import players
        mine = self.engage()
        theirs = self.b["pushed"]
        self.assertEqual(battle.parse(mine[0])["sub"], 0xA)
        self.assertEqual(battle.parse(theirs[0])["sub"], 0xA)                     # B was pulled in
        S = self.a["battle"]["S"]
        self.assertIs(self.b["battle"]["S"], S)
        ca, cb = self.a["cid"], self.b["cid"]
        ida = battle.parse22(mine[1])["bi"]["ids"]
        idb = battle.parse22(theirs[1])["bi"]["ids"]
        self.assertEqual((ida[0], ida[1], ida[7]), (ca, players.wire_cid(cb), battle.DEFAULT_ENEMY_CID))
        self.assertEqual((idb[0], idb[1], idb[7]), (players.wire_cid(ca), cb, battle.DEFAULT_ENEMY_CID))
        joins = [battle.parse22(f) for f in mine if f[:2] == bytes([0x22, 0x1A])]
        self.assertEqual([j["slot"] for j in joins], [0, 1, 7])
        self.ans(self.a, "2232")
        self.ans(self.b, "2232")
        del self.b["pushed"][:]
        waiting = self.attack(self.a)                                                # waits for B
        self.assertEqual([f[:2].hex() for f in waiting], ["2203"])                   # only A's gauge
        out_b = self.attack(self.b)                                                  # B completes the round
        recs = [battle.parse22(f) for f in out_b if f[:2] == bytes([0x22, 0x02])]
        self.assertEqual(len(recs), 3)                                               # A, B, the Bandit
        self.assertEqual(sorted(r["bi"]["actor"] for r in recs), [0, 1, 7])
        self.assertEqual(recs[-1]["ar2"]["root"], 0x11)
        states = [f for f in out_b if f[:2] == bytes([0x22, 0x0F])]
        self.assertEqual(len(states), 9)                                             # 3 units per record
        pushed = [battle.parse22(f) for f in self.a["pushed"] if f[:2] == bytes([0x22, 0x02])]
        self.assertEqual([r["rec"] for r in pushed], [r["rec"] for r in recs])       # A saw the same round
        self.assertEqual(pushed[0]["bi"]["ids"][0], ca)                               # in its own ids

    def test_group_win_rewards_everyone_standing(self):
        os.environ["NOBU_BATTLE_ONE_HIT"] = "1"
        os.environ["NOBU_BATTLE_ENEMY_COUNT"] = "2"
        mine = self.engage()
        key = self.a["battle"]["S"]["key"]
        joins = [battle.parse22(f) for f in mine if f[:2] == bytes([0x22, 0x1A])]
        self.assertEqual([j["slot"] for j in joins], [0, 1, 7, 8])                    # a group of two
        self.ans(self.a, "2232")
        self.ans(self.b, "2232")
        self.attack(self.a)
        out_b = self.attack(self.b)                                                  # each kills one
        res = [battle.parse22(f) for f in out_b if f[:2] == bytes([0x22, 0x25])]
        self.assertEqual((res[0]["win"], res[0]["exp"]), (1, 10))
        res_a = [battle.parse22(f) for f in self.a["pushed"] if f[:2] == bytes([0x22, 0x25])]
        self.assertEqual((res_a[0]["win"], res_a[0]["exp"]), (1, 10))
        st = self.world.store()
        self.assertEqual((st.find(0x4000471, self.a["cid"])["exp"], st.find(0x4000472, 2)["exp"]),
                         (10, 10))
        first = self.ans(self.a, "2268")
        self.assertEqual([f[:2].hex() for f in first][:1], ["0f06"])
        self.assertNotIn("0b04", [f[:2].hex() for f in first])                       # B still inside
        last = self.ans(self.b, "2268")
        self.assertIn("0b04", [f[:2].hex() for f in self.a["pushed"]] + [f[:2].hex() for f in last])
        self.assertNotIn(key, battle.BATTLES)


class CombatDepth(PartyBattle):
    def start_both(self):
        mine = self.engage()
        self.ans(self.a, "2232")
        self.ans(self.b, "2232")
        del self.a["pushed"][:], self.b["pushed"][:]
        return mine

    def test_setup_leaders_and_aid_button(self):
        mine = self.engage()
        cur_a = battle.parse22(mine[1])
        cur_b = battle.parse22(self.b["pushed"][1])
        self.assertEqual(cur_a["ap3"]["leaders"], (self.a["cid"], battle.DEFAULT_ENEMY_CID))   # Standard bearer per side
        self.assertEqual((cur_a["b5"], cur_b["b5"]), (1, 0))            # Call Aid / End Call: the leader only
        atk = next(battle.parse22(f) for f in mine if f[:2] == bytes([0x22, 0x33]))
        self.assertEqual(atk["cost"], 0)

    def test_combo_joins_the_charging_skill(self):
        self.start_both()
        skill = "2201" + battle.DEFAULT_ENEMY_CID.to_bytes(4, "big").hex() + "00" * 12 + "0092" + "0000"
        self.ans(self.a, skill)                                                      # A charges Adamant Blow
        self.assertIn(bytes.fromhex("2239"), self.b["pushed"])                       # B's menu gets Combo
        out = self.ans(self.b, "2201" + "00" * 16 + "0000" + "0100")                 # Combo: all zero, combo 1
        recs = [battle.parse22(f) for f in out if f[:2] == bytes([0x22, 0x02])]
        self.assertEqual([r["bi"]["actor"] for r in recs][:1], [0])                   # A leads
        self.assertNotIn(1, [r["bi"]["actor"] for r in recs])                        # B has no own action
        self.assertEqual(recs[0]["bi"]["list_b"][:2], [1, 0xFF])                      # B joins as helper
        self.assertIn(bytes.fromhex("223a"), out)
        bar = next(battle.parse22(f) for f in out if f[:2] == bytes([0x22, 0x3C]))
        self.assertEqual(bar["slots"][0][2], recs[0]["rec"] & 0xFF)                  # anchored on the round
        self.assertEqual(bar["slots"][0][1], recs[0]["bi"]["dur"])

    def test_leader_flees_side_routed(self):
        self.start_both()
        out = self.ans(self.a, "2201" + "00" * 16 + "0002" + "0000")                  # A (leader) flees
        self.assertEqual(battle.parse(out[1])["sub"], 6)
        rout = [battle.parse22(f) for f in self.b["pushed"] if f[:2] == bytes([0x22, 0x02])]
        self.assertEqual(rout[0]["bi"]["rout"], [2, 0])                               # 3.37 for B
        self.assertEqual((rout[0]["ar1"]["root"], rout[0]["ar2"]["root"]), (0x28, 0x28))   # Flee only
        self.assertEqual(rout[0]["ap2"]["routed"], (1, 0))

    def test_enemy_leader_falls_the_group_flees(self):
        os.environ["NOBU_BATTLE_ENEMY_COUNT"] = "2"
        self.start_both()
        S = self.a["battle"]["S"]
        lead = S["units"][2]
        lead["hp"] = 1
        for u in S["units"][:2]:
            u["c"]["dex"] = 60                                                        # they act (and hit) first
        out = []
        for _ in range(6):
            self.attack(self.a)
            out = self.attack(self.b)
            if S["over"]:
                break
        self.assertTrue(S["over"] and S["won"])
        self.assertGreater(S["units"][3]["hp"], 0)                                    # the second one fled
        recs = [battle.parse22(f) for f in out if f[:2] == bytes([0x22, 0x02])]
        self.assertIn([0, 1], [r["bi"]["rout"] for r in recs])                        # 3.38 for us

    def test_call_aid_and_a_helper_joins(self):
        c = self.login("ccc", 0x4000473, "前田", "利家", 3)
        self.start_both()
        self.assertEqual(self.ans(self.a, "2240")[0][:2].hex(), "0d06")              # Call Aid
        self.assertIn(b"calls for aid", b"".join(c["pushed"]))
        joined = self.world.answer(say("/s/help"), c)
        info = [f for f in joined if f[:1] == b"\x0f"]
        self.assertEqual(battle.parse(info[0])["sub"], 0xC)                            # 0F 0c: join mode
        items = [battle.parse22(f) for f in joined if f[:2] == bytes([0x22, 0x33])]
        self.assertIn((battle.JOIN_ACTION, 0x15, 3), [(i["value"], i["key"], i["ttype"]) for i in items])
        del self.a["pushed"][:]
        conf = self.ans(c, "2201" + (3).to_bytes(4, "big").hex() + "00" * 12 + "0158" + "00ff")
        self.assertEqual([f[:2].hex() for f in conf][:3], ["221f", "221a", "221c"])
        self.assertEqual(conf[0][2], 2)                                                # slot 2
        self.assertIn(bytes([0x22, 0x1F, 2]), [f[:3] for f in self.a["pushed"]])        # A sees the helper
        S = self.a["battle"]["S"]
        self.assertEqual(sorted(u["slot"] for u in S["units"] if u["side"] == "p"), [0, 1, 2])
        self.assertEqual(self.ans(self.a, "226a")[0][:2].hex(), "0d06")                # End Call
        self.assertFalse(S["aid"])


class PartyDeath(PartyBattle):
    def test_fallen_member_gets_no_reward_and_turns_ghost(self):
        self.engage()
        self.ans(self.a, "2232")
        self.ans(self.b, "2232")
        S = self.a["battle"]["S"]
        ub = next(u for u in S["units"] if u["cid"] == self.b["cid"])
        ub["hp"] = 0                                                   # B fell during the fight
        for u in S["units"]:
            if u["side"] == "e":
                u["hp"] = 0
        del self.b["pushed"][:]
        mine = battle.end_round(S, self.a, True, 10)
        ra = next(battle.parse22(f) for f in mine if f[:2] == bytes([0x22, 0x25]))
        rb = next(battle.parse22(f) for f in self.b["pushed"] if f[:2] == bytes([0x22, 0x25]))
        self.assertEqual((ra["win"], ra["exp"], rb["win"], rb["exp"]), (1, 10, 0, 0))
        st = self.world.store()
        self.assertEqual(st.find(0x4000471, 1).get("exp"), 10)
        self.assertIsNone(st.find(0x4000472, 2).get("exp"))
        self.ans(self.a, "2268")
        self.ans(self.b, "2268")
        self.assertTrue(self.b.get("defeat_pending"))
        self.assertFalse(self.a.get("defeat_pending"))
        self.assertIsNone(battle.answer(bytes.fromhex("235a0020"), self.b, self.world.store))
        self.assertEqual(self.b.pop("defeat_frames")[0].hex(), "2f0b00000201")


class LiveLoop(Env):
    """The fight through the real world.session over TCP (encrypted records, Session.run, ctx["push"] from the
    asyncio ticker): the 22 18 / 22 11 pushes must keep coming after 22 32 until the battle ends."""
    MORE = ("NOBU_BATTLE_FIGHT", "NOBU_BATTLE_TICK", "NOBU_BATTLE_TICK_DELAY", "NOBU_SPAWN", "NOBU_DEBUG_NPC")

    def setUp(self):
        super().setUp()
        import tempfile
        from nobu import world
        self.world = world
        self.more = {k: os.environ.pop(k) for k in list(os.environ) if k in self.MORE}
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (world.LOG_DIR, world.STORE)
        world.LOG_DIR = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "sessions"))
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        os.environ.update(NOBU_BATTLE_FIGHT="1", NOBU_BATTLE_TICK="0.05", NOBU_BATTLE_TICK_DELAY="0.05")

    def tearDown(self):
        self.world.LOG_DIR, self.world.STORE = self.old
        for k in self.MORE:
            os.environ.pop(k, None)
        os.environ.update(self.more)
        self.tmp.cleanup()
        super().tearDown()

    def test_ticker_over_the_real_session(self):
        import asyncio
        from nobu import vcecrypto, world
        uid = 0x4000471
        got = []

        async def client(port):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(vcecrypto.client_hello())
            buf = b""
            while not vcecrypto.parse_server_hello(buf):
                buf += await r.read(4096)
            status, g, p, Y, used = vcecrypto.parse_server_hello(buf)
            xmsg, key = vcecrypto.ClientHandshake().answer(g, p, Y)
            w.write(xmsg)
            bf = vcecrypto.Blowfish(key)
            rr, fr = vcecrypto.RecordReader(bf), vce.FrameReader()

            async def send(payload):
                w.write(vcecrypto.seal(bf, vce.frame(payload)))
                await w.drain()

            async def read_for(secs):
                end = asyncio.get_running_loop().time() + secs
                while True:
                    left = end - asyncio.get_running_loop().time()
                    if left <= 0:
                        return
                    try:
                        data = await asyncio.wait_for(r.read(65536), left)
                    except asyncio.TimeoutError:
                        return
                    if not data:
                        return
                    got.extend(fr.feed(rr.feed(data)))

            await send(world.encode_login_request("aaaaaa", "aaaaaa", uid))
            body = chars.write_create(dict(family="織田", given="信長", looks=ME["looks"]))
            await send(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body)
            await send(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(1))
            await send(b"\x04")
            await send(bytes.fromhex("235a0013"))
            await read_for(0.2)
            await send(say("/s/battle"))
            await read_for(0.8)                                          # 22 18 pushes before 22 32
            got.append(b"--ready--")
            await send(bytes.fromhex("2232"))
            await read_for(1.0)                                          # 22 11 pushes after 22 32
            got.append(b"--exit--")
            await send(say("/s/battle exit"))
            await read_for(0.3)
            w.close()

        async def main():
            srv = await asyncio.start_server(world.session, "127.0.0.1", 0)
            await client(srv.sockets[0].getsockname()[1])
            srv.close()
            await srv.wait_closed()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):                  # world.event prints every frame
            asyncio.run(main())
        i, j = got.index(b"--ready--"), got.index(b"--exit--")
        before, after, tail = got[:i], got[i + 1:j], got[j + 1:]
        self.assertTrue(any(f[:2] == b"\x0f\x0a" for f in before))
        self.assertGreaterEqual(before.count(bytes.fromhex("2218")), 2)          # waiting for 22 32
        clock = [f for f in after if f[:2] == b"\x22\x11"]
        self.assertGreaterEqual(len(clock), 4)                                     # reply + repeated pushes
        self.assertTrue(any(f[:2] == b"\x0f\x06" for f in tail))
        self.assertLessEqual(sum(f[:2] == b"\x22\x11" for f in tail[1:]), 1)       # ticker stopped


if __name__ == "__main__":
    unittest.main()
