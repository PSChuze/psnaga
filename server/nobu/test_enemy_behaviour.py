"""Enemy behaviour (wandering, red-name aggro, night-only spawns) and item stats (info window, battle bonus):
python -m unittest nobu.test_enemy_behaviour -v"""
import json
import math
import os
import random
import time
import unittest

from nobu import battle, chars, enemies, items, players, social, vce
from nobu import clientmodel as cm
from nobu.test_enemies import Env, GATE
from nobu.test_items import parse_backpack, parse_info


def night(on):
    """Freeze the upkeep clock at 23:00 (night) or 12:00 (day)."""
    os.environ["NOBU_CLOCK_EPOCH"] = str(int(time.time()))
    os.environ["NOBU_CLOCK_SECS_PER_HOUR"] = "1e9"
    os.environ["NOBU_CLOCK_OFFSET_HOURS"] = "23" if on else "12"


class Heading(unittest.TestCase):
    def test_degrees_from_plus_z(self):
        self.assertEqual([enemies.heading(0, 1), enemies.heading(1, 0), enemies.heading(0, -1),
                          enemies.heading(-1, 0)], [0, 90, 180, 270])


class Wander(Env):
    def setUp(self):
        super().setUp()
        self.ctx, self.sent = self.session("a")
        enemies.spawn_pushes(self.ctx)

    def test_small_steps_facing_the_way_and_state_byte(self):
        os.environ["NOBU_ENEMY_WANDER_MOVE"] = "1"
        os.environ["NOBU_ENEMY_WANDER_STEP"] = "100"
        os.environ["NOBU_ENEMY_WANDER_STATE"] = "0x80"
        before = {cid: list(enemies.spawn(cid)["pos"]) for cid in self.ctx["enemies"]}
        moved = enemies.wander_step(110, random.Random(7))
        self.assertTrue(moved)
        for cid in moved:
            s, p = enemies.spawn(cid), before[cid]
            dx, dz = s["pos"][1] - p[1], s["pos"][3] - p[3]
            self.assertLessEqual(math.hypot(dx, dz), 101)
            self.assertEqual(s["pos"][4], enemies.heading(dx, dz))
        ups = [f for f in self.sent if f[:2] == b"\x0b\x08"]
        self.assertTrue(ups and all(f[-1] == 0x80 for f in ups))              # u8 state after the u16 dir
        rec = cm.parse_notify_char(ups[0])["records"][0]
        self.assertEqual(rec["kind"], 6)

    def test_walks_to_its_goal_and_stays_near_home(self):
        os.environ["NOBU_ENEMY_WANDER_MOVE"] = "1"
        rng = random.Random(1)
        for _ in range(40):
            enemies.wander_step(110, rng)
        for cid in self.ctx["enemies"]:
            s = enemies.spawn(cid)
            self.assertLessEqual(math.hypot(s["pos"][1] - s["home"][0], s["pos"][3] - s["home"][2]), 601)

    def test_no_move_when_engaged_or_near_a_fight(self):
        os.environ["NOBU_ENEMY_WANDER_MOVE"] = "1"
        cid = next(iter(self.ctx["enemies"]))
        enemies.engage(self.ctx, cid)
        self.assertNotIn(cid, enemies.wander_step(110, random.Random(2)))
        enemies.release(self.ctx, cid)
        other, _ = self.session("b", cid=3)
        other["enemies_zone"] = 110
        s = enemies.spawn(cid)
        other["enemies_xz"] = (s["pos"][1], s["pos"][3])
        other["battle"] = {"phase": "turns"}
        self.assertNotIn(cid, enemies.wander_step(110, random.Random(2)))


class Aggro(Env):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_ENEMY_AGGRO"] = "1"
        os.environ["NOBU_ENEMY_AGGRO_GRACE_SECS"] = "0"
        os.environ["NOBU_ENEMY_AGGRO_RANGE"] = "600"
        self.ctx, _ = self.session("c", zone=10, x=GATE[0], z=GATE[1] + 2000, cid=4)
        enemies.spawn_pushes(self.ctx)
        self.s = next(s for s in enemies.WORLD[10].values() if s["enemy"] == "wander_suppa")
        self.s["pos"][4] = 0                                                     # it faces +z

    def near(self, dx, dz):
        x, z = self.s["pos"][1] + dx, self.s["pos"][3] + dz
        return enemies.on_move(self.ctx, dict(x=x, y=4000, z=z, dir=0))

    def started(self, out):
        return any(f[:2] == b"\x0f\x0a" for f in out)

    def test_arc_front_side_back(self):
        s = self.s
        x, z = s["pos"][1], s["pos"][3]
        self.assertEqual(enemies.arc_factor(s, x, z + 300), 1.0)
        self.assertEqual(enemies.arc_factor(s, x + 300, z), 0.5)
        self.assertAlmostEqual(enemies.arc_factor(s, x, z - 300), 1 / 3)

    def test_level_factor(self):
        self.assertEqual(enemies.level_factor(5, 7), 1.0)
        self.assertEqual(enemies.level_factor(8, 7), 0.5 * 0.9)
        self.assertEqual(enemies.level_factor(17, 7), 0.0)

    def test_behind_it_is_safe_in_front_it_attacks(self):
        self.assertFalse(self.started(self.near(0, -250)))                     # back: 600 / 3 = 200
        self.assertFalse(self.started(self.near(350, 0)))                      # side: 300
        self.s["pos"][4] = 30                                                  # still the front arc
        out = self.near(0, 500)                                                # front: 600
        self.assertTrue(self.started(out))
        self.assertEqual(self.ctx["battle_target"], self.s["cid"])
        self.assertEqual(self.s["pos"][4], 0)                                  # turned to the player

    def test_out_levelled_it_leaves_you_alone(self):
        self.ctx["char"]["level"] = 17
        self.assertFalse(self.started(self.near(0, 100)))
        self.ctx["char"]["level"] = 9                                          # 0.5 * 0.8 * 600 = 240
        self.assertFalse(self.started(self.near(0, 300)))
        self.assertTrue(self.started(self.near(0, 200)))

    def test_blocks(self):
        self.ctx["char"]["ghost"] = True
        self.assertEqual(enemies.aggro_blocked(self.ctx), "ghost")
        self.assertFalse(self.started(self.near(0, 100)))
        self.ctx["char"]["ghost"] = False
        self.ctx["battle"] = {"phase": "turns", "bid": 1}
        self.assertEqual(enemies.aggro_blocked(self.ctx), "fighting")
        self.ctx["battle"] = {"phase": "done", "bid": 1}
        self.assertFalse(self.started(self.near(0, 100)))                      # just after a battle: calm
        self.assertEqual(enemies.aggro_blocked(self.ctx), "calm")
        self.ctx["enemies_calm_until"] = 0
        self.assertIsNone(enemies.aggro_blocked(self.ctx))
        os.environ["NOBU_ENEMY_AGGRO_GRACE_SECS"] = "60"
        enemies.spawn_pushes(self.ctx)
        self.assertEqual(enemies.aggro_blocked(self.ctx), "grace")
        os.environ["NOBU_ENEMY_AGGRO"] = "0"
        self.assertEqual(enemies.aggro_blocked(self.ctx), "off")

    def test_yellow_enemies_never_start_it(self):
        os.environ["NOBU_ENEMY_AGGRO_RANGE"] = "60"                            # only the one we stand on
        y = next(s for s in enemies.WORLD[10].values() if not enemies.definition(s["enemy"])["aggressive"])
        where = dict(x=y["pos"][1], y=4000, z=y["pos"][3] + 20, dir=0)
        enemies.on_move(self.ctx, dict(where, x=where["x"] + 1000))            # bring it into view first
        self.assertIn(y["cid"], self.ctx["enemies"])
        self.assertFalse(self.started(enemies.on_move(self.ctx, where)))
        os.environ["NOBU_ENEMY_AGGRO_ALL"] = "1"
        self.assertTrue(self.started(enemies.on_move(self.ctx, where)))
        self.assertEqual(self.ctx["battle_target"], y["cid"])

    def test_not_in_town(self):
        ctx, _ = self.session("t")
        enemies.spawn_pushes(ctx)
        self.assertFalse(enemies.is_field(110))
        self.assertTrue(enemies.is_field(10))
        self.assertEqual(enemies.aggro_blocked(ctx), "town")
        os.environ["NOBU_ENEMY_AGGRO_TOWN"] = "1"
        self.assertIsNone(enemies.aggro_blocked(ctx))


class Night(Env):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_ENEMY_NIGHT"] = "1"
        enemies.populate(10)
        self.s = next(s for s in enemies.WORLD[10].values() if s["enemy"] == "white_oni_snake")
        self.ctx, self.sent = self.session("n", zone=10, x=self.s["pos"][1], z=self.s["pos"][3] + 1000, cid=6)

    def test_night_only_enemies_follow_the_clock(self):
        night(False)
        self.assertFalse(enemies.is_night())
        enemies.spawn_pushes(self.ctx)
        self.assertNotIn(self.s["cid"], self.ctx["enemies"])
        self.assertFalse(enemies.engage(self.ctx, self.s["cid"]))
        night(True)
        self.assertTrue(enemies.is_night())
        out = enemies.on_move(self.ctx, dict(x=self.s["pos"][1] + 10, y=4000, z=self.s["pos"][3] + 1000, dir=0))
        self.assertIn(self.s["cid"], {r["cid"] for r in self.records(out + self.sent)})
        self.assertIn(self.s["cid"], self.ctx["enemies"])
        night(False)                                                           # dawn: it leaves
        out = enemies.on_move(self.ctx, dict(x=self.s["pos"][1] + 20, y=4000, z=self.s["pos"][3] + 1000, dir=0))
        self.assertIn(self.s["cid"], self.deleted(out + self.sent))
        self.assertNotIn(self.s["cid"], self.ctx["enemies"])

    def test_a_fight_at_dawn_is_not_cut(self):
        night(True)
        enemies.spawn_pushes(self.ctx)
        self.assertTrue(enemies.engage(self.ctx, self.s["cid"]))
        night(False)
        self.assertTrue(enemies.active(self.s))
        self.assertIn(self.s["cid"], self.ctx["enemies"])

    def test_off_means_any_time(self):
        os.environ["NOBU_ENEMY_NIGHT"] = "0"
        night(False)
        enemies.spawn_pushes(self.ctx)
        self.assertIn(self.s["cid"], self.ctx["enemies"])


class NameByte(Env):
    def test_b6c_knob(self):
        red, yellow = enemies.definition("wander_suppa"), enemies.definition("rat")
        self.assertEqual((enemies.name_byte(red), enemies.name_byte(yellow)), (0, 0))
        os.environ["NOBU_ENEMY_B6C"] = "0,1"
        self.assertEqual((enemies.name_byte(red), enemies.name_byte(yellow)), (0, 1))


# ---------------------------------------------------------------------------- item stats
class ItemStats(unittest.TestCase):
    KEYS = ("NOBU_ITEM_STATS_FILE", "NOBU_ITEM_STATS_BATTLE", "NOBU_ITEM_ATK_SCALE", "NOBU_ITEM_DEF_SCALE",
            "NOBU_ITEM_JOBS", "NOBU_ITEM_WIKI_DUR", "NOBU_DATA")

    def setUp(self):
        import tempfile
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in self.KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_DATA"] = self.tmp.name
        items._STATS.update(key=None, data=None)

    def tearDown(self):
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        items._STATS.update(key=None, data=None)
        self.tmp.cleanup()

    def char(self, job=6):
        return dict(cid=9, looks=[4, 20, 0, job, 6] + [0] * 15, level=1)

    def test_wiki_rows(self):
        st = items.stats({"id": 1605})                                          # Kogatana: value10 attack32 durability65 all jobs
        self.assertEqual((st["value"], st["attack"], st["dur"], st["jobs"]), (10, 32, 65, 0x7F))
        st = items.stats(1716)                                                  # Joe Robe: defence36 +3, Deity only
        self.assertEqual((st["defence"], st["jobs"], st["mods"]["fire"]), (36, 1 << 2, 3))
        self.assertFalse(items.stats(201)["known"])                             # medicine: no row
        self.assertEqual(items.stats({"id": 1605, "attack": 40})["attack"], 40)    # the instance wins

    def test_info_window_fields(self):
        c = self.char(3)
        inv = items.inventory(c)
        body = next(it for it in inv["items"] if it.get("pos") == 3)            # Joe Robe
        rep = items.answer(bytes.fromhex("24020001") + bytes([body["idx"]]), {"char": c})[0]
        info = parse_info(rep)["info"]
        self.assertEqual((info[0x35], info[0x37], info[0x39], info[0x36]), (10, 36, 36, 0))
        self.assertEqual([info[o] for o in (0x47, 0x48, 0x49, 0x4A)], [3, 3, 3, 3])   # Earth .. Wind
        self.assertEqual((info[0x30], info[0x31], info[0x24]), (40, 40, 4))
        bp = parse_backpack(items.answer(bytes.fromhex("24010002"), {"char": c})[0])
        self.assertEqual(bp["records"][body["idx"]][0x14], 4)                   # record job mask
        os.environ["NOBU_ITEM_JOBS"] = "0"
        bp = parse_backpack(items.answer(bytes.fromhex("24010003"), {"char": c})[0])
        self.assertEqual(bp["records"][body["idx"]][0x14], 0)

    def test_negative_mods_are_signed(self):
        it = dict(id=1605, idx=0, count=1, mods={"str": -2, "life": 30})
        info = parse_info(bytes.fromhex("240200") + b"\x01" + items.info_struct(it))["info"]
        self.assertEqual((info[0x42], info[0x3E]), (-2, 30))

    def test_combat_bonus(self):
        c = self.char(6)                                                        # Smith: Hatchet 23, Commoner Garb 6
        eq = items.combat_bonus(c, None, {5: 6}, 3, 1)
        self.assertEqual((eq["weapon"], eq["armour"], eq["mods"]), (6, 2, {}))
        os.environ["NOBU_ITEM_ATK_SCALE"] = "1"
        self.assertEqual(items.combat_bonus(c)["weapon"], 23)
        os.environ["NOBU_ITEM_STATS_BATTLE"] = "0"                              # battle's old rules
        self.assertEqual(items.combat_bonus(c, None, {5: 6}, 3, 1)["armour"], 3)
        os.environ.pop("NOBU_ITEM_STATS_BATTLE")
        weapon = next(it for it in c["inventory"]["items"] if it.get("pos") == 0)
        weapon["broken"] = True
        self.assertEqual(items.combat_bonus(c, None, {}, 3, 1)["weapon"], 1)
        c3 = self.char(3)                                                       # Joe Robe: all elements +3
        self.assertEqual(items.combat_bonus(c3)["mods"]["earth"], 3)

    def test_override_file_and_starter_durability(self):
        with open(os.path.join(self.tmp.name, "itemstats.json"), "w", encoding="utf-8") as fh:
            json.dump({"items": {"1647": {"attack": 50, "dur": 90}}}, fh)
        self.assertEqual((items.stats(1647)["attack"], items.stats(1647)["value"]), (50, 4))
        kit = items.starter_kit(6)
        self.assertEqual(kit["items"][0]["dur_max"], 90)
        os.environ["NOBU_ITEM_WIKI_DUR"] = "0"
        self.assertEqual(items.starter_kit(6)["items"][0]["dur_max"], 100)

    def test_patched_player_combat(self):
        import inspect
        if "combat_bonus" not in inspect.getsource(battle.player_combat):
            self.skipTest("battle.py without the item stats hook")
        c = self.char(3)                                                        # Priest: Bamboo Rod 6, Joe Robe 36
        c["looks"][11:16] = [5, 5, 5, 5, 5]
        pc = battle.player_combat(c)
        self.assertEqual((pc["atk"], pc["dfn"]), (2 * 5 + 2 + 2, 2 * 5 + 1 + 9))   # 6 * 0.25 -> 2, 36 * 0.25

    def test_enemy_equipment_drop_can_be_worn(self):
        c = self.char()
        items.inventory(c)
        enemies.give_drops({"char": c}, None, [dict(id=2056, name="Dull Club", type=8, stack=False)])
        it = next(i for i in c["inventory"]["items"] if i["id"] == 2056)
        st = items.stats(2056)
        self.assertEqual((it["cat"], it["dur_max"]), (1 if st["known"] else 0, st["dur"] or 100))


if __name__ == "__main__":
    unittest.main()
