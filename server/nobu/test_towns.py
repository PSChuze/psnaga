"""Every clan's home town (nobu/towns.py): python -m unittest nobu.test_towns -v

For each of the 14 clans a new character is created and played through world.answer: it starts in its town, the
town has its Guide, Overseer, Merchant, Hall Master and Craftsman, the gate next to the spawn point leads to the
province field, the field has enemies near the gate, and the field gate leads back to the town."""
import math
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, crafting, enemies, guild, quests, shops, social, towns, vce, world, zones
from nobu.test_shops import parse_event
from nobu.test_zones import ClientNet, self_location, warped

UID = 0x4000471
KNOBS = ("NOBU_DATA", "NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN", "NOBU_WORLD_PUSH",
         "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_PLAYERS", "NOBU_SHOPS", "NOBU_SHOPS_FILE", "NOBU_ZONES",
         "NOBU_ZONES_FILE", "NOBU_ZONE_RANGE", "NOBU_ZONE_ALL_GATES", "NOBU_ZONE_PROVINCES", "NOBU_ZONE_CMDS",
         "NOBU_ZONE_DEBUG", "NOBU_ZONE_PENDING_SECS", "NOBU_ENEMIES", "NOBU_BATTLE_FIGHT", "NOBU_QUESTS", "NOBU_GUILD",
         "NOBU_GUILD_FILE", "NOBU_CRAFTING", "NOBU_CRAFT_FILE", "NOBU_TOWNS", "NOBU_NPC_TEXT", "NOBU_ENEMY_VIEW",
         "NOBU_ENEMY_TOWN", "NOBU_TALK_RANGE", "NOBU_SHOP_ANY_ZONE", "NOBU_GUILD_ANY_ZONE", "NOBU_CRAFT_ANY_ZONE",
         "NOBU_TOWN_SPOTS", "NOBU_SHOP_VENDORS")
# clan (STRDAT table 5) -> (town map, province map): WDMMSG 0.146.124..137 (the clan seats, in clan order)
HOMES = {1: (101, 1), 2: (104, 4), 3: (106, 6), 4: (107, 7), 5: (109, 9), 6: (110, 10), 7: (111, 11),
         8: (113, 13), 9: (114, 14), 10: (115, 15), 11: (117, 17), 12: (118, 18), 13: (122, 19), 14: (121, 21)}
GUIDE, OVERSEER = 0xFEA00001, 0xFEA00002


def looks(faction, job=6):
    return [4, 20, 0, job, faction, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]


def records(frames):
    return [r for f in frames if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]


def says(frames):
    out = []
    for f in frames:
        if f[:2] == b"\x0d\x06":                               # NPC lines: named system lines
            out.append(vce.Reader(f, 2).bytes().decode("utf-8"))
    return out


class Clan(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_DATA"] = self.tmp.name
        os.environ["NOBU_DEBUG_NPC"] = "1"                      # the Guide (and so the Overseer), as on the live rig
        os.environ["NOBU_ENEMIES"] = "1"
        os.environ["NOBU_TOWN_SPOTS"] = "0"                     # talk from the spawn point: NPCs on their offsets
        os.environ["NOBU_SHOP_VENDORS"] = "0"                   # one Merchant per town
        self.sessions = dict(social.SESSIONS)
        social.SESSIONS.clear()
        enemies.reset()
        guild.reset()
        self.seq = 0x100

    def tearDown(self):
        world.STORE = None
        social.SESSIONS.clear()
        social.SESSIONS.update(self.sessions)
        enemies.reset()
        guild.reset()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        self.tmp.cleanup()

    # ------------------------------------------------------------------ helpers
    def login(self, faction):
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, f"chars{faction}.json"))
        self.net = ClientNet()
        self.ctx = {"uid": UID, "push": lambda f: None}
        body = chars.write_create(dict(family="新参", given="者", looks=looks(faction)))
        world.answer(bytes([5, 1]) + vce.put_u32(UID) + vce.put_u16(1) + body, self.ctx)
        cid = world.store().list(UID)[0]["cid"]
        world.answer(bytes([5, 2]) + vce.put_u32(UID) + vce.put_u32(cid), self.ctx)
        social.register("t", self.ctx)
        out = world.answer(b"\x04", self.ctx)
        self.assertTrue(self.net.feed(out[1]))
        return self.ready()

    def ready(self):
        return world.answer(bytes.fromhex("235a0013"), self.ctx)

    def move(self, x, z, y=3200):
        return world.answer(chars.encode_move(x, y, z, 0, 0, area=chars.area_for(self.ctx["zone"])), self.ctx)

    def travel(self, x, z, y=3200):
        self.move(x, z, y)
        self.seq += 1
        out = world.answer(bytes.fromhex("2802") + vce.put_u16(self.seq), self.ctx)
        w = warped(out)
        self.assertEqual(len(w), 1, [f.hex() for f in out])
        self.assertTrue(self.net.feed(w[0]))
        return self.ready()

    def talk(self, cid):
        world.answer(bytes.fromhex("25010031") + vce.put_u32(cid), self.ctx)
        self.seq += 1
        return world.answer(bytes.fromhex("2801") + vce.put_u16(self.seq), self.ctx)

    # ------------------------------------------------------------------ the whole loop, per clan
    def test_every_clan_starts_home_with_npcs_a_gate_and_enemies(self):
        for faction, (town, prov) in sorted(HOMES.items()):
            with self.subTest(clan=faction, town=town):
                self.clan(faction, town, prov)

    def clan(self, faction, town, prov):
        t = towns.TOWNS[town]
        spawn = self.login(faction)
        # 1. starts in its town, at the arrival point of the gate to its field
        self.assertEqual((self.net.map, self.net.area), (town, zones.area_of(town)))
        self.assertEqual(self_location(spawn), (zones.area_of(town), towns.spawn(town)))
        self.assertEqual(chars.home_zone(self.ctx["char"]), town)
        # 2. its NPCs, all in the town's area and a few steps from the spawn point
        recs = {r["cid"]: r for r in records(spawn) if r["uid"] == 0}
        roles = {n.get("role"): cid for cid, n in self.ctx["npcs"].items()}
        need = {"guide", "overseer", "shop", "guild", "workshop"}
        self.assertLessEqual(need | {"shrine"}, set(roles))                   # Nagoya also has its bank NPCs
        roles = {k: v for k, v in roles.items() if k in need}                 # the shrine stands 850 away
        self.assertEqual(roles["guide"], GUIDE)
        self.assertEqual(roles["overseer"], OVERSEER)
        if town != towns.NAGOYA:
            self.assertEqual(roles["shop"], shops.town_cid(town, 3))
            self.assertEqual(roles["guild"], shops.town_cid(town, 4))
            self.assertEqual(roles["workshop"], shops.town_cid(town, 0x10))
        sx, _, sz = towns.spawn(town)
        for cid in roles.values():
            r = recs[cid]
            self.assertEqual(r["area"], zones.area_of(town), hex(cid))
            self.assertLess(math.hypot(r["pos"][0] - sx, r["pos"][2] - sz), 600, hex(cid))
            self.assertTrue(cm.npc_actor_created(r), hex(cid))
        # 3. the tutorial names the town and its house
        offer = says(self.talk(GUIDE))
        self.assertIn(f"Welcome to {t['name']}.", offer[0])
        finish = says(self.talk(OVERSEER))
        self.assertIn(f"register of the {t['house']} house", finish[0])
        self.assertEqual(quests.qstate(self.ctx["char"], quests.TUTORIAL), "done")
        # 4. the Merchant sells Nagoya's goods plus the town's own
        ev = next(parse_event(f) for f in self.talk(roles["shop"]) if f[0] == shops.EVENT)
        ids = [g["id"] for g in ev["records"][0]["goods"]]
        self.assertIn(201, ids)                                              # Heal Powder, sold everywhere
        self.assertTrue(all(g["id"] in ids for g in towns.goods(town)))
        if town != towns.NAGOYA:
            self.assertTrue(towns.goods(town))
        # 5. the Hall Master offers a job on the town's own province enemies
        out = self.talk(roles["guild"])
        job = guild.active(self.ctx["char"])
        self.assertIsNotNone(job, says(out))
        self.assertEqual(job.get("zone"), town)
        self.assertEqual(job["kind"], "kill")
        self.assertTrue(all(any(s["enemy"] == e and s["zone"] == prov for s in enemies.seed()["spawns"])
                            for e in job["enemies"]))
        # 6. the Craftsman opens the workshop (a Smith has recipes)
        self.assertTrue(any(f[0] == shops.EVENT for f in self.talk(roles["workshop"])))
        # 7. the gate next to the spawn point leads to the province field, which has enemies near the gate
        g = towns.linked_gate(town)
        field = self.travel(*g["at"])
        self.assertEqual((self.net.map, self.net.area), (prov, prov))
        area, pos = self_location(field)
        self.assertEqual((area, pos), (prov, tuple(g["field_arrive"])))
        foes = [r for r in records(field) if enemies.is_enemy_cid(r["cid"])]
        self.assertTrue(foes, "no enemies near the town gate")
        near = [r for r in foes if math.hypot(r["pos"][0] - pos[0], r["pos"][2] - pos[2]) < 15000
                and r["level"] <= 5]
        self.assertTrue(near, "no Lv 1-5 enemy within 15000 of the gate")
        self.assertTrue(all(enemies.spawn(r["cid"])["zone"] == prov for r in foes))
        self.assertFalse(any(n.get("role") == "shop" for n in self.ctx.get("npcs", {}).values()))   # keepers stay home
        self.assertFalse(any(n.get("role") in ("guide", "overseer")                 # no Guide / Overseer in the field
                             for n in self.ctx.get("npcs", {}).values()))
        # 8. and the field gate leads back to the town's spawn point
        back = self.travel(*g["field_at"])
        self.assertEqual((self.net.map, self.net.area), (town, zones.area_of(town)))
        self.assertEqual(self_location(back), (zones.area_of(town), towns.spawn(town)))


class Data(unittest.TestCase):
    def test_every_clan_has_a_town_and_its_seat(self):
        self.assertEqual(sorted(towns.FACTION_TOWN), list(range(1, 15)))
        for faction, (town, prov) in HOMES.items():
            self.assertEqual(towns.FACTION_TOWN[faction], town)
            self.assertEqual(towns.TOWNS[town]["province"], prov)
            self.assertEqual(chars.FIELD_SPAWN[town], towns.spawn(town))
            self.assertNotEqual(zones.area_of(town), zones.area_of(prov))      # or the warp could not reload

    def test_nagoya_is_unchanged(self):
        self.assertEqual(towns.spawn(110), (103713, 3200, 112594))
        self.assertEqual(towns.place(110, 300, 0, 180), (110, 104013, 3200, 112594, 180))
        self.assertEqual(towns.place(110, -300, 300, 90), (110, 103413, 3200, 112894, 90))
        self.assertFalse([e for e in towns.exits() if e["map"] == 110 or e["to"] == 110])   # zones.EXITS has them

    def test_offsets_turn_with_the_gate(self):
        # Kasugayama's linked gate faces inward along +z (the opposite of Nagoya's): offsets and facing turn 180
        self.assertEqual(towns.linked_gate(101)["inward"], (0, 1))
        x, y, z = towns.spawn(101)
        self.assertEqual(towns.place(101, 0, -300, 0), (101, x, y, z + 300, 180))
        # Okazaki's faces +x: Nagoya's "300 inward" (dz -300) becomes dx +300
        x, y, z = towns.spawn(109)
        self.assertEqual(towns.place(109, 0, -300, 0)[1:4], (x + 300, y, z))

    def test_spots_come_from_the_town_spot_table(self):
        os.environ.pop("NOBU_TOWN_SPOTS", None)
        # Nagoya: the notice board (region 301), the graveyard, the exchange, the forge (region 403)
        self.assertEqual(towns.spot(110, "overseer")[:4], (110, 103250, 3200, 110250))
        self.assertEqual(towns.spot(110, "craftsman")[:4], (110, 99696, 3200, 89844))
        self.assertEqual(towns.spot(110, "guildmaster")[0], 110)               # the town centre (no hall located)
        self.assertEqual(towns.spot(118, "shrine")[0], 118)                  # no graveyard: near the town centre
        self.assertIsNone(towns.spot(102, "overseer"))                       # not a home town
        os.environ["NOBU_TOWN_SPOTS"] = "0"
        try:
            self.assertIsNone(towns.spot(110, "overseer"))
        finally:
            del os.environ["NOBU_TOWN_SPOTS"]

    def test_every_spot_is_inside_its_town(self):
        from nobu import npc
        for m, row in ((k, v) for k, v in towns._load_spots().items() if int(k) in towns.TOWNS):
            for role, s in row.items():
                zone, x, y, z, d = towns.spot(int(m), role)
                self.assertTrue(0 <= d < 360)
                self.assertTrue(3000 <= y <= 4500, (m, role, y))                 # the town's floor heights
                # within the town's walls (CMAP header extent), with a margin for gate areas
                sx, sy, sz = towns.spawn(int(m))
                self.assertLess(math.hypot(x - sx, z - sz), 100000, (m, role))
                if role == "shrine":    # a ghost at the spawn point must be out of the shrine's range (Pass On)
                    self.assertGreater(math.hypot(x - sx, z - sz), npc.TALK_RANGE, m)

    def test_every_home_town_has_its_npcs_apart(self):
        # Every role has a spot (none falls back onto the gate's arrival point), and no two NPCs stand within 600
        # units, except Nagoya's smith-hall sellers, who ring the Craftsman at the forge on purpose.
        os.environ.pop("NOBU_TOWN_SPOTS", None)
        smith = {"craftsman", "repairer", "shop_sword", "shop_spear", "shop_club", "shop_armour", "shop_helmet"}
        for m, row in ((k, v) for k, v in towns._load_spots().items() if int(k) in towns.TOWNS):
            for role in ("guide", "overseer", "shrine", "merchant", "craftsman", "guildmaster", "bank_changer",
                         "bank_keeper"):
                self.assertIsNotNone(towns.spot(int(m), role), (m, role))
            roles = sorted(row)
            for i, a in enumerate(roles):
                for b in roles[i + 1:]:
                    if {a, b} <= smith or {a, b} == {"bank_changer", "bank_keeper"}:      # one counter each
                        continue
                    if a.startswith("shop_") or b.startswith("shop_"):   # vendors cluster at their buildings
                        continue
                    pa, pb = row[a]["pos"], row[b]["pos"]
                    self.assertGreaterEqual(math.hypot(pa[0] - pb[0], pa[2] - pb[2]), 600, (m, a, b))

    def test_bank_counter_in_every_home_town(self):
        from nobu import bank, chars
        for k in ("NOBU_TOWN_SPOTS", "NOBU_BANK", "NOBU_BANK_TOWNS", "NOBU_BANK_ANY_ZONE", "NOBU_FORCE_HOME"):
            os.environ.pop(k, None)
        cids = set()
        for m in towns.TOWNS:
            ctx = {"char": {"pos": [m, 0, 0, 0, 0], "looks": looks(6), "level": 3}}
            recs = records(bank.spawn_pushes(ctx))
            self.assertEqual(len(recs), 2, m)
            for r in recs:
                self.assertTrue(cm.npc_actor_created(r), hex(r["cid"]))
                self.assertEqual(ctx["npcs"][r["cid"]]["role"], bank.ROLE)        # 28 01 on it opens the bank
                self.assertNotIn(r["cid"], cids)
                cids.add(r["cid"])
                self.assertEqual(tuple(r["pos"]), tuple(towns.spot(m, ctx["npcs"][r["cid"]].get("spot_role")
                                                                  or ("bank_changer" if r["cid"] & 0xFF == 0x11
                                                                      else "bank_keeper"))[1:4]))
        self.assertIn(0xFEA00011, cids)                                           # Nagoya keeps its old cids
        os.environ["NOBU_BANK_TOWNS"] = "0"
        try:
            self.assertEqual(bank.spawn_pushes({"char": {"pos": [104, 0, 0, 0, 0], "looks": looks(6)}}), [])
        finally:
            del os.environ["NOBU_BANK_TOWNS"]

    def test_castle_npcs_in_nagoya_castle(self):
        from nobu import castle
        for k in ("NOBU_TOWN_SPOTS", "NOBU_CASTLE_NPCS", "NOBU_FORCE_HOME"):
            os.environ.pop(k, None)
        ctx = {"char": {"pos": [410, 0, 0, 0, 0], "looks": looks(6), "level": 3}}
        recs = records(castle.spawn_pushes(ctx))
        self.assertEqual(len(recs), len(castle.CASTLES[410]))
        self.assertEqual(len({r["cid"] for r in recs}), len(recs))
        for r in recs:
            self.assertTrue(cm.npc_actor_created(r), hex(r["cid"]))
        names = {(n["family"], n["given"]) for n in ctx["npcs"].values()}
        self.assertIn(("Oda", "Nobunaga"), names)
        self.assertEqual(castle.spawn_pushes({"char": {"pos": [110, 0, 0, 0, 0], "looks": looks(6)}}), [])
        os.environ["NOBU_CASTLE_NPCS"] = "0"
        try:
            self.assertEqual(castle.spawn_pushes({"char": {"pos": [410, 0, 0, 0, 0], "looks": looks(6)}}), [])
        finally:
            del os.environ["NOBU_CASTLE_NPCS"]

    def test_every_home_town_has_a_castle_with_its_lord(self):
        from nobu import castle
        for k in ("NOBU_TOWN_SPOTS", "NOBU_CASTLE_NPCS", "NOBU_CASTLE_GATES", "NOBU_FORCE_HOME"):
            os.environ.pop(k, None)
        ex = towns.exits()
        cids = set()
        for m, t in towns.TOWNS.items():
            cm_ = m + 300
            if m != towns.NAGOYA:                     # Nagoya's castle exits are written out in zones.EXITS
                self.assertTrue([e for e in ex if e["map"] == m and e["to"] == cm_], m)
                self.assertTrue([e for e in ex if e["map"] == cm_ and e["to"] == m], m)
            ctx = {"char": {"pos": [cm_, 0, 0, 0, 0], "looks": looks(6), "level": 3}}
            recs = records(castle.spawn_pushes(ctx))
            self.assertGreaterEqual(len(recs), 5, cm_)                     # lord, retainer / generals, castellan, guards
            lord = [n for n in ctx["npcs"].values() if n["family"] == castle.LORDS[t["faction"]][0][0]]
            self.assertTrue(lord, cm_)
            for r in recs:
                self.assertNotIn(r["cid"], cids)
                cids.add(r["cid"])
        os.environ["NOBU_CASTLE_GATES"] = "0"
        try:
            self.assertFalse([e for e in towns.exits() if e["to"] > 400 or e["map"] > 400])
        finally:
            del os.environ["NOBU_CASTLE_GATES"]

    def test_hall_master_at_the_job_hall(self):
        from nobu import guild
        tmp = tempfile.TemporaryDirectory()
        saved = {k: os.environ.pop(k) for k in ("NOBU_TOWN_SPOTS", "NOBU_GUILD_JOB_HALLS", "NOBU_GUILD_FILE",
                                                 "NOBU_GUILD", "NOBU_FORCE_HOME", "NOBU_GUILD_ANY_ZONE")
                 if k in os.environ}
        os.environ["NOBU_GUILD_FILE"] = os.path.join(tmp.name, "guild.json")
        try:
            def master_in(zone, job):
                ctx = {"char": {"pos": [zone, 0, 0, 0, 0], "looks": looks(6, job), "level": 3}}
                guild.world_ready(ctx)
                return [n for n in ctx.get("npcs", {}).values() if n.get("role") == guild.ROLE]
            smith = master_in(110, 6)                                       # Nagoya smith: at the smith hall
            self.assertEqual(tuple(smith[0]["pos"]), towns.spot(110, "hall_6"))
            self.assertEqual(master_in(110, 1), [])                         # Nagoya samurai: not in town ...
            samurai = master_in(410, 1)                                     # ... but at the castle's samurai hall
            self.assertEqual(samurai[0]["town"], 110)
            self.assertEqual(samurai[0]["pos"][1:4], list(towns._load_spots()["410"]["hall_1"]["pos"]))
            monk = master_in(107, 2)                                        # Sunpu: no hall located -> centre
            self.assertEqual(tuple(monk[0]["pos"]), towns.spot(107, "guildmaster"))
            monk = master_in(114, 2)                                        # Ichijodani: the hand-read temple
            self.assertEqual(tuple(monk[0]["pos"]), towns.spot(114, "hall_2"))
            kofu = master_in(104, 2)                                        # Kofu: the hand-read temple
            self.assertEqual(tuple(kofu[0]["pos"]), towns.spot(104, "hall_2"))
            self.assertEqual(master_in(104, 1), [])                         # Kofu samurai: in Kofu Castle (404)
            self.assertEqual(master_in(404, 1)[0]["pos"][1:4], list(towns._load_spots()["404"]["hall_1"]["pos"]))
            os.environ["NOBU_GUILD_JOB_HALLS"] = "0"
            self.assertEqual(len(master_in(110, 1)), 1)                     # off: the samurai's master in town
        finally:
            os.environ.pop("NOBU_GUILD_JOB_HALLS", None)
            os.environ.pop("NOBU_GUILD_FILE", None)
            os.environ.update(saved)
            tmp.cleanup()

    def test_gate_guards_in_every_home_town(self):
        from nobu import npc
        for k in ("NOBU_TOWN_SPOTS", "NOBU_TOWN_GUARDS", "NOBU_FORCE_HOME"):
            os.environ.pop(k, None)
        cids = set()
        for m in towns.TOWNS:
            spots = towns.guard_spots(m)
            self.assertGreaterEqual(len(spots), 2, m)
            self.assertEqual(len(spots) % 2, 0, m)                           # a pair per gate
            ctx = {"char": {"pos": [m, 0, 0, 0, 0], "looks": looks(6), "level": 3}}
            pushes = npc.guard_spawn(ctx)
            recs = records(pushes)
            self.assertEqual(len(recs), len(spots), m)
            for r in recs:
                self.assertEqual(r["uid"], 0)
                self.assertTrue(cm.npc_actor_created(r), hex(r["cid"]))
                self.assertNotIn(r["cid"], cids)
                cids.add(r["cid"])
            self.assertEqual(set(ctx["npcs"]), {r["cid"] for r in recs})
        os.environ["NOBU_TOWN_GUARDS"] = "0"
        try:
            self.assertEqual(npc.guard_spawn({"char": {"pos": [110, 0, 0, 0, 0], "looks": looks(6)}}), [])
        finally:
            del os.environ["NOBU_TOWN_GUARDS"]

    def test_every_town_exit_has_a_way_back(self):
        ex = towns.exits()
        self.assertTrue(ex)
        for e in ex:
            back = [b for b in ex if b["map"] == e["to"] and b["to"] == e["map"]]
            self.assertTrue(back, e["id"])
            ax, _, az, _ = e["arrive"]
            self.assertTrue(any(math.hypot(b["at"][0] - ax, b["at"][1] - az) <= b["r"] + 500 for b in back), e["id"])
            self.assertGreaterEqual(zones.map_index(e["map"]), 0)

    def test_towns_off_keeps_the_old_start(self):
        saved = {k: os.environ.pop(k) for k in ("NOBU_TOWNS", "NOBU_ZONE", "NOBU_START_TOWN") if k in os.environ}
        try:
            os.environ["NOBU_TOWNS"] = "0"
            self.assertEqual(chars.home_zone(dict(looks=looks(1))), 1)            # Uesugi: its first province
            self.assertEqual(chars.home_zone(dict(looks=looks(6))), 110)          # Oda: Nagoya as before
            self.assertFalse(towns.exits())
        finally:
            os.environ.pop("NOBU_TOWNS", None)
            os.environ.update(saved)
        self.assertEqual(chars.home_zone(dict(looks=looks(1))), 101)

    def test_seed_data_per_town(self):
        shop_zones = {s.get("zone") for s in shops.SHOPS_SEED["shops"].values()}
        craft_zones = {w.get("zone") for w in crafting.CRAFT_SEED["workshops"].values()}
        guild_zones = {m.get("zone") for m in guild.GUILD_SEED["masters"]}
        job_zones = {j.get("zone") for j in guild.GUILD_SEED["jobs"]}
        for town in towns.TOWNS:
            for have in (shop_zones, craft_zones, guild_zones, job_zones):
                self.assertIn(town, have)
        cids = [shops.parse_cid(s["npc"]["cid"]) for s in shops.SHOPS_SEED["shops"].values()]
        cids += [shops.parse_cid(w["npc"]["cid"]) for w in crafting.CRAFT_SEED["workshops"].values()]
        cids += [shops.parse_cid(m["cid"]) for m in guild.GUILD_SEED["masters"]]
        self.assertEqual(len(cids), len(set(cids)))
        self.assertNotIn(GUIDE, cids)
        self.assertNotIn(OVERSEER, cids)
        ids = [j["id"] for j in guild.GUILD_SEED["jobs"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_old_data_files_get_the_new_keepers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shops.json")
            old = dict(shops.SHOPS_SEED, shops={"nagoya_general": shops.SHOPS_SEED["shops"]["nagoya_general"]})
            import json
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(old, fh)
            self.assertIn("kasugayama_general", shops.load_shops(path)["shops"])
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(dict(old, custom=True), fh)
            self.assertNotIn("kasugayama_general", shops.load_shops(path)["shops"])


if __name__ == "__main__":
    unittest.main()
