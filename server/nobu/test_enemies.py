"""Field enemies (nobu/enemies.py): python -m unittest nobu.test_enemies -v"""
import json
import os
import random
import tempfile
import time
import unittest

from nobu import battle, chars, clientmodel as cm, enemies, items, npc, players, social, vce, world

UID = 0x04000471
KNOBS = ("NOBU_ENEMIES", "NOBU_ENEMIES_FILE", "NOBU_ENEMY_TOWN", "NOBU_ENEMY_NAMES", "NOBU_ENEMY_VIEW",
         "NOBU_ENEMY_MAX", "NOBU_ENEMY_RESPAWN", "NOBU_ENEMY_DROPS", "NOBU_ENEMY_WANDER", "NOBU_ENEMY_WANDER_SECS",
         "NOBU_ENEMY_WANDER_RADIUS", "NOBU_ENEMY_AGGRO", "NOBU_ENEMY_AGGRO_RANGE", "NOBU_BATTLE_FIGHT", "NOBU_DATA",
         "NOBU_DEBUG_ENEMY", "NOBU_DEBUG_NPC", "NOBU_TALK_RANGE", "NOBU_ZONE", "NOBU_FORCE_HOME", "NOBU_SHOPS",
         "NOBU_PLAYERS", "NOBU_SPAWN_X", "NOBU_SPAWN_Y", "NOBU_SPAWN_Z", "NOBU_START_TOWN", "NOBU_ENEMY_NIGHT",
         "NOBU_ENEMY_AGGRO_GRACE_SECS", "NOBU_ENEMY_AGGRO_CALM_SECS", "NOBU_ENEMY_AGGRO_TOWN", "NOBU_ENEMY_AGGRO_ALL",
         "NOBU_ENEMY_AGGRO_LEVEL_GAP", "NOBU_ENEMY_WANDER_STEP", "NOBU_ENEMY_WANDER_MOVE", "NOBU_ENEMY_WANDER_STATE",
         "NOBU_ENEMY_WANDER_FREEZE", "NOBU_ENEMY_B6C", "NOBU_CLOCK_OFFSET_HOURS", "NOBU_CLOCK_EPOCH",
         "NOBU_CLOCK_SECS_PER_HOUR", "NOBU_UPKEEP", "NOBU_CLOCK")
GATE = (109600, 85300)                    # CMAP010: the Nagoya town entrance on the Owari field


def me(zone=110, x=103713, y=3200, z=112594, cid=2):
    return dict(cid=cid, uid=UID + cid, slot=0, family="ああ", given="ああ", looks=[4, 20, 0, 6, 6] + [0] * 15,
                pos=[zone, x, y, z, 0])


def move(x, z, y=3200):
    return bytes([5, 5]) + vce.put_u32(x) + vce.put_u32(y) + vce.put_u32(z) + vce.put_u16(0) + b"\0"


class Env(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_DATA"] = self.tmp.name
        os.environ["NOBU_BATTLE_FIGHT"] = "1"
        os.environ["NOBU_ENEMY_NIGHT"] = "0"          # night-only enemies at any hour unless a test says so
        self.sessions = dict(social.SESSIONS)
        social.SESSIONS.clear()
        enemies.reset()

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        social.SESSIONS.clear()
        social.SESSIONS.update(self.sessions)
        enemies.reset()
        self.tmp.cleanup()

    def session(self, sid, zone=110, x=103713, z=112594, cid=2):
        sent = []
        ctx = {"char": me(zone, x, 3200, z, cid), "cid": cid, "uid": UID + cid, "zone": zone,
               "push": sent.append}
        social.register(sid, ctx)
        return ctx, sent

    @staticmethod
    def records(frames):
        out = []
        for f in frames:
            if f[:2] == b"\x0b\x0a":
                out += cm.parse_notify_char(f)["records"]
        return out

    @staticmethod
    def deleted(frames):
        return [vce.Reader(f, 6).u32() for f in frames if f[:2] == b"\x0b\x04"]


class Data(Env):
    def test_seed_values_are_labelled_and_consistent(self):
        d = enemies.seed()
        self.assertGreaterEqual(len(d["enemies"]), 30)
        names = items.names()
        for eid, e in d["enemies"].items():
            for k in ("name", "name_jp", "level", "count", "life", "ki", "rewards", "drops", "looks"):
                self.assertIn(k, e, eid)
            for k in ("level", "count", "life", "rewards", "cells"):
                self.assertIn(e["src"][k].split()[0], ("wiki", "disc", "guess", "estimate"), (eid, k))
            for dr in e["drops"]:
                self.assertEqual(items.item_name(dr["id"]), dr["name"], (eid, dr))   # the STRDAT 0 row
                self.assertIn("disc", dr["src"])
            self.assertLessEqual(len(enemies.display_name(e).encode("cp932")), enemies.NAME_MAX)
            self.assertNotIn(e["looks"].get("job"), (0, 1))                     # 0 / 1 create no person actor
        for sp in d["spawns"]:
            self.assertIn(sp["enemy"], d["enemies"])
            self.assertIn(sp["src"].split()[0], ("cell", "guess"))

    def test_owari_spawns_sit_in_their_wiki_cells(self):
        d = enemies.seed()
        for sp in d["spawns"]:
            if sp["zone"] != 10:
                continue
            x, _, z = sp["pos"]
            cell = "r%dc%d" % (z // 20000, x // 20000)
            self.assertEqual(cell, sp["cell"])
            wiki = d["enemies"][sp["enemy"]]["cells"]
            if "field" not in wiki and "castle" not in wiki:
                self.assertIn(cell, wiki, sp)
            if "castle" in wiki:                                               # = around the Nagoya gate
                self.assertLess(((x - GATE[0]) ** 2 + (z - GATE[1]) ** 2) ** 0.5, 12500)
        self.assertEqual("r%dc%d" % (GATE[1] // 20000, GATE[0] // 20000), "r4c5")  # wiki: Nagoya -

    def test_cids_stay_in_their_own_range(self):
        d = enemies.seed()
        for i in range(len(d["spawns"])):
            cid = enemies.ENEMY_BASE + 1 + i
            self.assertTrue(enemies.is_enemy_cid(cid))
            self.assertTrue(npc.is_npc_cid(cid))                               # drawn (0xfe, low >= 0xa00000)
            self.assertFalse(players.is_player_wire(cid))
            self.assertNotIn(cid, (battle.DEFAULT_ENEMY_CID, 0xFEA00001, 0xFEA00002, 0xFEA00003))
        self.assertFalse(enemies.is_enemy_cid(battle.DEFAULT_ENEMY_CID))
        self.assertFalse(enemies.is_enemy_cid(players.wire_cid(5)))

    def test_data_file_is_created_then_reseeded(self):
        path = enemies.data_path()
        self.assertEqual(path, os.path.join(self.tmp.name, "enemies.json"))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(enemies.load_data()["seed_version"], enemies.seed()["seed_version"])
        self.assertTrue(os.path.exists(path))
        old = dict(enemies.seed(), seed_version=0)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        enemies.reset()
        self.assertEqual(enemies.load_data()["seed_version"], enemies.seed()["seed_version"])
        self.assertTrue(os.path.exists(path + ".v0.bak"))
        custom = dict(old, custom=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(custom, fh)
        enemies.reset()
        self.assertEqual(enemies.load_data()["seed_version"], 0)             # "custom": true is kept

    def test_broken_file_falls_back_to_the_seed(self):
        with open(enemies.data_path(), "w") as fh:
            fh.write("{nope")
        self.assertEqual(len(enemies.load_data()["enemies"]), len(enemies.seed()["enemies"]))


class Spawning(Env):
    def test_off_unless_the_fight_is_on(self):
        ctx, _ = self.session("a")
        os.environ.pop("NOBU_BATTLE_FIGHT")
        self.assertEqual(enemies.spawn_pushes(ctx), [])
        os.environ["NOBU_ENEMIES"] = "1"
        self.assertTrue(enemies.spawn_pushes(ctx))
        os.environ["NOBU_ENEMIES"] = "0"
        os.environ["NOBU_BATTLE_FIGHT"] = "1"
        self.assertEqual(enemies.spawn_pushes(ctx), [])

    def test_town_test_spawns_are_attackable_people(self):
        ctx, _ = self.session("a")
        recs = self.records(enemies.spawn_pushes(ctx))
        self.assertEqual(len(recs), 3)
        for r in recs:
            self.assertTrue(cm.npc_actor_created(r))                          # UI 0xd4 + an actor
            self.assertEqual((r["uid"], r["power"]), (0, 0))                  # faction 0: attackable
            self.assertNotEqual(r["b6b"], npc.TALK_ONLY)
            self.assertTrue(enemies.is_enemy_cid(r["cid"]))
            self.assertIn(r["cid"], ctx["enemies"])
            self.assertEqual(r["area"], chars.area_for(110))
        self.assertEqual({ctx["enemies"][r["cid"]]["family"] for r in recs}, {"Rat", "Snake", "Petty Thief"})
        os.environ["NOBU_ENEMY_TOWN"] = "0"
        enemies.reset()
        self.assertEqual(enemies.spawn_pushes(ctx), [])
        self.assertFalse(any(enemies.is_enemy_cid(c) for c in ctx["enemies"]))

    def test_japanese_names(self):
        os.environ["NOBU_ENEMY_NAMES"] = "jp"
        ctx, _ = self.session("a")
        enemies.spawn_pushes(ctx)
        self.assertIn(enemies.seed()["enemies"]["rat"]["name_jp"], {e["family"] for e in ctx["enemies"].values()})

    def test_owari_field_near_nagoya_and_view_limits(self):
        ctx, _ = self.session("a", zone=10, x=GATE[0], z=GATE[1] + 2000)
        frames = enemies.spawn_pushes(ctx)
        recs = self.records(frames)
        kinds = {ctx["enemies"][r["cid"]]["enemy"] for r in recs}
        self.assertTrue({"rat", "snake", "nezu_bat"} <= kinds, kinds)
        for f in frames:
            self.assertLessEqual(cm.parse_notify_char(f).get("records", []).__len__(), players.CHUNK)
        for r in recs:
            self.assertLessEqual(((r["pos"][0] - GATE[0]) ** 2 + (r["pos"][2] - GATE[1] - 2000) ** 2) ** 0.5, 25000)
        os.environ["NOBU_ENEMY_MAX"] = "4"
        ctx2, _ = self.session("b", zone=10, x=GATE[0], z=GATE[1] + 2000, cid=3)
        self.assertEqual(len(self.records(enemies.spawn_pushes(ctx2))), 4)

    def test_zone_reload_forgets_old_entries_but_keeps_the_debug_bandit(self):
        ctx, _ = self.session("a")
        ctx["enemies"] = {battle.DEFAULT_ENEMY_CID: {"cid": battle.DEFAULT_ENEMY_CID, "pos": [110, 0, 0, 0, 0]}}
        enemies.spawn_pushes(ctx)
        ctx["zone"] = 10
        ctx["char"]["pos"] = [10, GATE[0], 3952, GATE[1] + 2000, 0]
        enemies.spawn_pushes(ctx)
        self.assertIn(battle.DEFAULT_ENEMY_CID, ctx["enemies"])
        self.assertTrue(all(enemies.spawn(c)["zone"] == 10 for c in ctx["enemies"] if enemies.is_enemy_cid(c)))

    def test_get_scales_with_level(self):
        ctx, _ = self.session("a")
        enemies.spawn_pushes(ctx)
        cid = next(c for c, e in ctx["enemies"].items() if e["enemy"] == "petty_thief")
        st = enemies.get(cid)
        self.assertEqual((st["name"], st["level"], st["life"], st["ki"]), ("Petty Thief", 4, 110, 52))
        self.assertEqual(st["rewards"], dict(exp=28, prof=11, fame=2, money=60))
        s = enemies.spawn(cid)
        s["level"] = 6
        self.assertEqual(enemies.get(cid)["life"], 150)
        self.assertEqual(enemies.get(cid)["rewards"]["money"], 80)
        self.assertIsNone(enemies.get(battle.DEFAULT_ENEMY_CID))
        rat = enemies.get(next(c for c, e in ctx["enemies"].items() if e["enemy"] == "rat"))
        self.assertEqual(rat["rewards"], dict(exp=10, prof=5, fame=1, money=0))   # Lv1 = the debug Bandit's Exp/Prof


class Movement(Env):
    def test_moving_away_and_back(self):
        ctx, _ = self.session("a", zone=10, x=GATE[0], z=GATE[1] + 2000)
        enemies.spawn_pushes(ctx)
        near = set(c for c in ctx["enemies"] if enemies.is_enemy_cid(c))
        far = enemies.on_move(ctx, dict(x=190000, y=6000, z=260000, dir=0))
        self.assertTrue(near <= set(self.deleted(far)))
        back = enemies.on_move(ctx, dict(x=GATE[0], y=3952, z=GATE[1] + 2000, dir=0))
        self.assertTrue(near <= {r["cid"] for r in self.records(back)})
        self.assertEqual(enemies.on_move(ctx, dict(x=GATE[0] + 100, y=3952, z=GATE[1] + 2000, dir=0)), [])

    def test_world_hooks(self):
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        try:
            c = world.STORE.create(UID, 0, "ああ", "いい", [4, 20, 0, 6, 6] + [0] * 15)
            ctx = {"char": c, "cid": c["cid"], "uid": UID, "zone": 110}
            recs = self.records(world.world_ready_pushes(ctx))
            self.assertTrue(any(enemies.is_enemy_cid(r["cid"]) for r in recs))
            self.assertEqual(world.answer(move(103713, 112594), ctx), [])
            gone = world.answer(move(10000, 10000), ctx)
            self.assertEqual(len(self.deleted(gone)), 3)
        finally:
            world.STORE = None

    def test_circle_resolves_to_the_nearest_enemy(self):
        ctx, _ = self.session("a")
        enemies.spawn_pushes(ctx)
        cid = next(c for c, e in ctx["enemies"].items() if e["enemy"] == "rat")
        s = enemies.spawn(cid)
        ctx["pos"] = dict(x=s["pos"][1] + 100, y=3200, z=s["pos"][3], dir=0)
        self.assertEqual(npc.resolve_target(ctx), cid)

    def test_wander_pushes_kind6_moves(self):
        ctx, sent = self.session("a")
        enemies.spawn_pushes(ctx)
        ctx["enemies_zone"] = 110
        moved = enemies.wander_step(110, random.Random(3))
        self.assertTrue(moved)
        ups = [cm.parse_notify_char(f)["records"][0] for f in sent if f[:2] == b"\x0b\x08"]
        self.assertEqual({u["cid"] for u in ups}, set(moved))
        self.assertTrue(all(u["kind"] == 6 for u in ups))
        for cid in moved:
            s = enemies.spawn(cid)
            self.assertLessEqual(((s["pos"][1] - s["home"][0]) ** 2 + (s["pos"][3] - s["home"][2]) ** 2) ** 0.5, 601)
            self.assertIs(ctx["enemies"][cid]["pos"], s["pos"])               # views share the position


class BattleInterface(Env):
    def setUp(self):
        super().setUp()
        self.a, self.sent_a = self.session("a")
        self.b, self.sent_b = self.session("b", cid=3)
        enemies.spawn_pushes(self.a)
        enemies.spawn_pushes(self.b)
        self.cid = next(c for c, e in self.a["enemies"].items() if e["enemy"] == "petty_thief")

    def test_unpatched_battle_py_targets_and_starts(self):
        rep = battle.answer(bytes.fromhex("25010040") + vce.put_u32(self.cid), self.a)
        r = vce.Reader(rep[0], 2)
        self.assertEqual((r.u16(), r.i32(), r.u32()), (0x40, 0, self.cid))
        frames = battle.answer(bytes.fromhex("28010041"), self.a)
        self.assertEqual(battle.parse(frames[0])["sub"], 0xA)
        self.assertEqual(self.a["battle_target"], self.cid)
        ui = [battle.parse22(f) for f in frames[1:] if f[:2] == b"\x22\x1f"]
        self.assertIn("Petty Thief", [u["ui"]["family"] for u in ui])        # the enemy's name reaches the battle

    def test_engage_hides_it_from_the_others_and_release_shows_it(self):
        self.assertTrue(enemies.engage(self.a, self.cid))
        self.assertIn(self.cid, self.deleted(self.sent_b))
        self.assertNotIn(self.cid, self.b["enemies"])
        self.assertFalse(enemies.engage(self.b, self.cid))
        self.assertTrue(enemies.engage(self.a, self.cid))                      # same session: fine
        del self.sent_b[:]
        enemies.release(self.a, self.cid)
        self.assertIn(self.cid, {r["cid"] for r in self.records(self.sent_b)})
        self.assertTrue(enemies.engage(self.b, self.cid))

    def test_engaged_by_a_closed_session_is_free(self):
        self.assertTrue(enemies.engage(self.a, self.cid))
        social.unregister("a")
        self.assertTrue(enemies.engage(self.b, self.cid))

    def test_defeat_respawn_and_drops(self):
        os.environ["NOBU_ENEMY_RESPAWN"] = "30"
        store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        enemies.engage(self.a, self.cid)
        random.seed(1)
        with unittest.mock.patch.object(random, "random", return_value=0.0):
            out = enemies.on_defeated(self.a, self.cid, store)
        self.assertEqual(self.deleted(out), [self.cid])
        self.assertIn("Obtained Small Knife.", [f[3:].decode("utf-8") for f in out if f[:2] == b"\x0d\x06"])
        inv = items.inventory(self.a["char"])
        self.assertIn(785, [i["id"] for i in inv["items"]])
        self.assertNotIn(self.cid, self.a["enemies"])
        self.assertFalse(enemies.engage(self.b, self.cid))                     # dead
        self.assertNotIn(self.cid, enemies.visible(self.a))
        s = enemies.spawn(self.cid)
        self.assertAlmostEqual(s["dead_until"] - time.monotonic(), 30, delta=2)
        s["dead_until"] = time.monotonic() - 1                                 # timer ran out
        del self.sent_b[:]
        back = enemies.on_move(self.a, dict(x=103713, y=3200, z=112594, dir=0))
        self.assertIn(self.cid, {r["cid"] for r in self.records(back)})
        self.assertIn(self.cid, {r["cid"] for r in self.records(self.sent_b)})   # pushed to the others too
        self.assertEqual(s["gen"], 1)

    def test_drop_stacking_and_full_backpack(self):
        c = self.a["char"]
        inv = items.inventory(c)
        tail = dict(id=601, name="Rat Tail", type=54, stack=True)
        enemies.give_drops(self.a, None, [tail, tail])
        self.assertEqual([i["count"] for i in inv["items"] if i["id"] == 601], [2])
        inv["items"] = [dict(id=1, type=1, cat=1, count=1, flags=0, idx=i) for i in range(items.CAPACITY)]
        out = enemies.give_drops(self.a, None, [dict(id=785, name="Small Knife", type=1, stack=False)])
        self.assertIn("backpack full", out[0][3:].decode("utf-8"))

    @unittest.skipIf(hasattr(battle, "field_enemies"), "the patched battle.despawn keeps field enemies")
    def test_post_battle_resync_after_the_debug_despawn(self):
        before = set(c for c in self.a["enemies"] if enemies.is_enemy_cid(c))
        self.a["battle"] = {"bid": 1, "phase": "done", "won": True}
        again = enemies.on_move(self.a, dict(x=103713, y=3200, z=112594, dir=0))
        self.assertEqual({r["cid"] for r in self.records(again)}, before)
        self.assertEqual(enemies.on_move(self.a, dict(x=103713, y=3200, z=112594, dir=0)), [])

    def test_aggro(self):
        os.environ["NOBU_ENEMY_AGGRO"] = "1"
        os.environ["NOBU_ENEMY_AGGRO_GRACE_SECS"] = "0"
        ctx, _ = self.session("c", zone=10, x=GATE[0], z=GATE[1] + 2000, cid=4)
        enemies.spawn_pushes(ctx)
        suppa = next(s for s in enemies.WORLD[10].values() if s["enemy"] == "wander_suppa")
        self.assertTrue(enemies.definition("wander_suppa")["aggressive"])
        out = enemies.on_move(ctx, dict(x=suppa["pos"][1] + 100, y=4000, z=suppa["pos"][3], dir=0))
        self.assertIn(suppa["cid"], ctx["enemies"])
        start = [f for f in out if f[:1] == bytes([0x0F])]
        self.assertEqual(battle.parse(start[0])["sub"], 0xA)
        self.assertIn(b"attacks!", b"".join(f for f in out if f[:2] == bytes([0x0D, 0x06])))
        self.assertEqual(ctx["battle_target"], suppa["cid"])
        self.assertEqual(suppa["engaged"], id(ctx))
        os.environ["NOBU_ENEMY_AGGRO"] = "0"
        ctx2, _ = self.session("d", zone=10, x=GATE[0], z=GATE[1] + 2000, cid=5)
        enemies.spawn_pushes(ctx2)
        self.assertFalse(any(f[:1] == b"\x0f" for f in enemies.on_move(
            ctx2, dict(x=suppa["pos"][1] + 100, y=4000, z=suppa["pos"][3], dir=0))))


@unittest.skipUnless(hasattr(battle, "field_enemies"), "battle.py without the field enemy hook")
class PatchedBattle(Env):
    """Active once battle.py carries the hook patch: the fight uses the enemy's name / Life / rewards and a win
    kills only that enemy (respawn timer, drops), a flight frees it."""

    def setUp(self):
        super().setUp()
        self.a, _ = self.session("a")
        self.b, self.sent_b = self.session("b", cid=3)
        enemies.spawn_pushes(self.a)
        enemies.spawn_pushes(self.b)
        self.cid = next(c for c, e in self.a["enemies"].items() if e["enemy"] == "petty_thief")
        self.a["target"] = self.cid

    def test_win(self):
        store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        frames = battle.answer(bytes.fromhex("28010041"), self.a, store)
        joins = [battle.parse22(f) for f in frames if f[:2] == bytes([0x22, 0x1A])]
        foe = next(j for j in joins if j["ui"]["family"] == "Petty Thief")
        self.assertEqual((foe["us"]["life"], foe["us"]["ki"]), (110, 52))
        self.assertIn(self.cid, self.deleted(self.sent_b))                    # engaged: hidden from b
        self.assertFalse(enemies.engage(self.b, self.cid))
        cmd = bytes([0x22, 0x01]) + vce.put_u32(self.cid) + bytes(12) + vce.put_u16(1) + bytes(2)
        os.environ["NOBU_BATTLE_ONE_HIT"] = "1"                 # battle.py rounds: one decisive hit
        try:
            out = battle.fight_answer(cmd, self.a, store)
        finally:
            os.environ.pop("NOBU_BATTLE_ONE_HIT", None)
        if not any(f[:2] == bytes([0x22, 0x25]) for f in out):   # a group: the others fall in later rounds
            os.environ["NOBU_BATTLE_ONE_HIT"] = "1"
            try:
                for _ in range(10):
                    out = battle.fight_answer(cmd, self.a, store)
                    if any(f[:2] == bytes([0x22, 0x25]) for f in out):
                        break
            finally:
                os.environ.pop("NOBU_BATTLE_ONE_HIT", None)
        res = next(battle.parse22(f) for f in out if f[:2] == bytes([0x22, 0x25]))
        self.assertEqual((res["exp"], res["prof"], res["fame"], res["money"]), (28, 11, 2, 60))
        fin = battle.finish(self.a)
        self.assertEqual(self.deleted(fin), [self.cid])                       # only the defeated one
        self.assertFalse(enemies.spawn(self.cid)["alive"])
        self.assertTrue(any(enemies.is_enemy_cid(c) for c in self.a["enemies"]))

    def test_flee_frees_it(self):
        battle.answer(bytes.fromhex("28010041"), self.a)
        del self.sent_b[:]
        battle.finish(self.a)
        self.assertTrue(enemies.spawn(self.cid)["alive"])
        self.assertIn(self.cid, {r["cid"] for r in self.records(self.sent_b)})

    def test_busy_enemy(self):
        view = self.a["enemies"][self.cid]
        self.assertTrue(enemies.engage(self.b, self.cid))
        self.assertNotIn(self.cid, self.a["enemies"])                         # hidden: 28 01 is not ours
        self.assertIsNone(battle.answer(bytes.fromhex("28010041"), self.a))
        self.a["enemies"][self.cid] = view                                    # a 28 01 that raced the 0B 04
        (line,) = battle.answer(bytes.fromhex("28010041"), self.a)
        self.assertEqual(line[:2], bytes([0x0D, 0x06]))


import unittest.mock  # noqa: E402  (patch.object above)

if __name__ == "__main__":
    unittest.main()
