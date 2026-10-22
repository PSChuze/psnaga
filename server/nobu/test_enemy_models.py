"""Creature models for field enemies (enemies.CREATURE_MODELS, chars.char_info_record +0x54 /
+0x56): python -m unittest nobu.test_enemy_models -v"""
import json
import os

from nobu import battle, chars, clientmodel as cm, enemies, vce
from nobu.test_enemies import Env

MODEL_KNOBS = ("NOBU_ENEMY_MODELS", "NOBU_ENEMY_MODEL_SCALE", "NOBU_ENEMY_MODEL_KINDS", "NOBU_INFO_W54")


class Base(Env):
    def setUp(self):
        super().setUp()
        self.saved_m = {k: os.environ.pop(k) for k in MODEL_KNOBS if k in os.environ}

    def tearDown(self):
        for k in MODEL_KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved_m)
        super().tearDown()

    def spawn_of(self, eid, zone=110):
        enemies.populate(zone)
        for s in enemies.WORLD[zone].values():
            if s["enemy"] == eid:
                return s
        raise AssertionError(eid)

    def record(self, s, viewer=None):
        rec = chars.char_info_record(enemies.view(s, viewer or {}), 0, tuple(s["pos"]))
        return cm.parse_notify_char(chars.notify_char_info([rec]))["records"][0]


class Models(Base):
    def test_table_covers_the_seed_animals_only(self):
        d = enemies.seed()
        for eid, m in enemies.CREATURE_MODELS.items():
            self.assertIn(eid, d["enemies"])
            self.assertTrue(66 <= m["model"] <= 145, eid)       # CHAROFFS rows 66+ are creatures
            self.assertIn("MOBLOW", m["src"])
            self.assertNotEqual(d["enemies"][eid]["race"], 1, eid)    # never a human (STRDAT 9 race 1)
        for eid, e in d["enemies"].items():
            if e["race"] == 1:
                self.assertEqual(enemies.model_of(eid), (0, 0), eid)

    def test_rat_record_carries_the_model(self):
        r = self.record(self.spawn_of("rat"))
        self.assertEqual((r["w54"], r["b56"]), (enemies.RAT, 50))
        self.assertEqual(r["w58"], 0)
        self.assertEqual(r["cid"] >> 24, 0xFE)                     # still the drawn uid-0 path

    def test_person_enemy_is_unchanged(self):
        s = self.spawn_of("petty_thief")
        r = self.record(s, {"looks": [4, 20, 0, 6, 6] + [0] * 15})
        v = enemies.view(s, {"looks": [4, 20, 0, 6, 6] + [0] * 15})
        self.assertEqual(v["model"], 0)
        self.assertEqual(r["w54"], 0)
        self.assertEqual(r["b56"], chars.appearance(v)["b56"])

    def test_knob_off_draws_people(self):
        os.environ["NOBU_ENEMY_MODELS"] = "0"
        r = self.record(self.spawn_of("snake"))
        self.assertEqual(r["w54"], 0)
        self.assertEqual(enemies.get(self.spawn_of("snake")["cid"])["model"], 0)

    def test_scale_knob_and_data_override(self):
        os.environ["NOBU_ENEMY_MODEL_SCALE"] = "70"
        self.assertEqual(enemies.model_of("rat"), (enemies.RAT, 70))
        del os.environ["NOBU_ENEMY_MODEL_SCALE"]
        self.assertEqual(enemies.model_of("rat", {"model": {"id": 69, "scale": 40}}), (69, 40))
        self.assertEqual(enemies.model_of("rat", {"model": 0}), (0, 0))       # data can force a person
        self.assertEqual(enemies.model_of("petty_thief", {"model": 71}), (71, 50))

    def test_get_exposes_the_model_for_battle(self):
        st = enemies.get(self.spawn_of("snake")["cid"])
        self.assertEqual((st["model"], st["model_scale"]), (enemies.SNAKE, 50))
        st = enemies.get(self.spawn_of("petty_thief")["cid"])
        self.assertEqual(st["model"], 0)

    def test_players_and_npcs_keep_w54_zero(self):
        c = dict(cid=2, uid=0, family="a", given="b", looks=[4, 20, 0, 6, 6] + [0] * 15)
        rec = cm.parse_notify_char(chars.notify_char_info([chars.char_info_record(c, 0, (110, 1, 2, 3, 0))]))
        self.assertEqual(rec["records"][0]["w54"], 0)

    def custom_data(self, kinds):
        d = enemies.seed()
        d["custom"] = True
        d["spawns"] = [dict(enemy=eid, zone=110, pos=[103713 + 100 * (i + 1), 3200, 112594], dir=0, src="guess")
                       for i, eid in enumerate(kinds)]
        with open(enemies.data_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        enemies.reset()

    def test_at_most_three_creature_models_in_view(self):
        # nearest first: rat, snake, bat, spider (a 4th model), thief (person), viper (snake again)
        self.custom_data(["rat", "snake", "nezu_bat", "poison_spider", "petty_thief", "viper"])
        ctx, _ = self.session("a")
        recs = self.records(enemies.spawn_pushes(ctx))
        models = [r["w54"] for r in recs]
        self.assertEqual(sorted(set(m for m in models if m)), sorted({enemies.RAT, enemies.SNAKE, enemies.BAT}))
        self.assertEqual(len(recs), 5)                              # the spider waits; thief and viper shown
        os.environ["NOBU_ENEMY_MODEL_KINDS"] = "0"
        enemies.reset()
        self.custom_data(["rat", "snake", "nezu_bat", "poison_spider"])
        ctx2, _ = self.session("b")
        self.assertEqual(len(self.records(enemies.spawn_pushes(ctx2))), 4)


class BattleIdentity(Base):
    """Needs applied to battle.py (skipped before)."""
    def setUp(self):
        if "model" not in battle.w_ui.__code__.co_varnames:     # before Env.setUp: a skip skips tearDown
            self.skipTest("battle.py without the creature models hook")
        super().setUp()

    def test_identity_model_and_scale_offsets(self):
        ui = battle.w_ui("Rat", "", model=enemies.RAT, scale=50)
        r = vce.Reader(ui, 0)
        r.bytes(0x11), r.bytes(0x11)
        self.assertEqual([r.u8() for _ in range(4)], [0, 0, 0, 0])
        self.assertEqual(r.u16(), enemies.RAT)                      # I+0x26 -> unit+0x20 -> A[0..1]
        r.u8(), r.u8(), r.u16(), r.u8(), r.u8(), r.u16()
        self.assertEqual(r.u8(), 50)                                # I+0x30 -> A[0xa] scale
        self.assertEqual(len(battle.w_ui("Rat", "")), len(ui))
        self.assertEqual(battle.w_ui("Rat", "")[-12:], bytes(12))   # people unchanged

    def test_person_identity_carries_the_looks(self):
        # same A bytes as the world (0x1db0d0): +0x24 A[2] sex == 1, +0x25 A[3] job, +0x30..+0x33 sliders,
        # hair, face; without them every person in battle was the default male body
        lk = [1, 1, 1, 4, 2, 6, 3, 3, 70, 40] + [0] * 10
        r = vce.Reader(battle.w_ui("Ann", "", looks=lk), 0)
        r.bytes(0x11), r.bytes(0x11)
        self.assertEqual([r.u8() for _ in range(4)], [0, 0, 1, 4])
        self.assertEqual(r.u16(), 0)
        r.u8(), r.u8(), r.u16(), r.u8(), r.u8(), r.u16()
        self.assertEqual([r.u8() for _ in range(12)], [70, 40, 3, 6] + [0] * 8)
        r = vce.Reader(battle.w_ui("Bob", "", looks=[1, 1, 0, 0] + [0] * 16), 0)
        r.bytes(0x11), r.bytes(0x11)
        self.assertEqual([r.u8() for _ in range(4)], [0, 0, 0, 1])  # male, job 0 -> 1 like the world
        self.assertEqual(battle.w_ui("Rat", "", model=enemies.RAT, scale=50, looks=lk)[-12:],
                         bytes([50]) + bytes(11))                   # creatures ignore looks

    def test_battle_info_preloads_the_enemy_model(self):
        s = self.spawn_of("rat")
        ctx, _ = self.session("a")
        ctx["battle_target"] = s["cid"]
        f = battle.start_frames(ctx)[0]
        self.assertEqual(battle.parse(f)["ids"], [enemies.RAT])
