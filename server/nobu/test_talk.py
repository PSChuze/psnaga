"""○ on an NPC without a target set, replaying the live sequence: the client walks up
(05 05), then sends `23 11 <seq> 00000000` and `28 01 <seq>`, never `25 01`. python
-m unittest nobu.test_talk -v"""
import os
import tempfile
import unittest

from nobu import battle, chars, clientmodel as cm, npc, quests, shops, social, vce, world

UID = 0x4000471
LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
KNOBS = ("NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN",
         "NOBU_WORLD_PUSH", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_QUESTS", "NOBU_PLAYERS",
         "NOBU_SHOPS", "NOBU_SHOPS_FILE", "NOBU_SHOP_REOPEN", "NOBU_SHOP_ITEM_PUSH",
         "NOBU_SHOP_ANY_ZONE", "NOBU_TALK_RANGE", "NOBU_BATTLE_FIGHT", "NOBU_NPC_TALK_ON_TARGET",
         "NOBU_GUILD", "NOBU_GUILD_FILE", "NOBU_CRAFTING", "NOBU_BANK", "NOBU_REPAIR", "NOBU_DOCTOR", "NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER", "NOBU_LIFE_SHRINE", "NOBU_TOWN_GUARDS")


class LiveCircle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        os.environ["NOBU_DEBUG_NPC"] = "1"
        os.environ["NOBU_DEBUG_ENEMY"] = "1"
        os.environ["NOBU_SHOP_ANY_ZONE"] = "1"
        os.environ["NOBU_CRAFTING"] = "0"       # the workshop keeper is tested in test_crafting
        os.environ["NOBU_BANK"] = "0"           # no money changer / storekeeper either
        os.environ["NOBU_REPAIR"] = "0"         # nor the repairer (nobu/repair.py)
        os.environ["NOBU_DOCTOR"] = "0"         # nor the doctor (nobu/doctor.py)
        os.environ["NOBU_TOWN_QUESTS"] = "0"   # nor the quest givers (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        os.environ["NOBU_TOWN_GUARDS"] = "0"    # nor the gate guards (npc.guard_spawn)
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        os.environ["NOBU_GUILD_FILE"] = os.path.join(self.tmp.name, "guild.json")
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        social.SESSIONS.clear()
        self.ctx = {"uid": UID}
        body = chars.write_create(dict(family="織田", given="信長", looks=LOOKS))
        world.answer(bytes([5, 1]) + vce.put_u32(UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(UID) + vce.put_u32(1), self.ctx)
        world.answer(b"\x04", self.ctx)
        self.pushes = world.answer(bytes.fromhex("235a0013"), self.ctx)
        self.seq = 0x40

    def tearDown(self):
        world.STORE = None
        social.SESSIONS.clear()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def npc_named(self, family):
        every = dict(self.ctx.get("npcs", {}))
        every.update(self.ctx.get("enemies", {}))
        return next(n for n in every.values() if n["family"] == family)

    def walk_to(self, n, dx=120):
        """05 05 next to NPC n (the y word carries the area in its high half, like the client)."""
        _, x, y, z, _ = n["pos"]
        area = chars.area_for(self.ctx["zone"])
        world.answer(chars.encode_move(x + dx, y, z, 90, 0, area=area), self.ctx)

    def circle(self, clear=False):
        """The exact live frames for one ○ press: [25 02], 23 11 <seq> 00000000, 28 01 <seq>."""
        out = []
        if clear:
            out += world.answer(bytes.fromhex("2502") + vce.put_u16(self.seq), self.ctx)
            self.seq += 1
        out += world.answer(bytes.fromhex("2311") + vce.put_u16(self.seq) + vce.put_u32(0), self.ctx)
        talk = world.answer(bytes.fromhex("2801") + vce.put_u16(self.seq + 1), self.ctx)
        self.seq += 2
        return out, talk

    def test_all_six_are_spawned_with_their_own_faces(self):
        faces = {}
        for family in ("Guide", "Overseer", "Bandit", "Hall Master", "Shrine Keeper"):
            n = self.npc_named(family)
            self.assertTrue(npc.is_npc_cid(n["cid"]))
            faces[family] = (n["looks"][5], n["looks"][6], n["looks"][8])
        merchants = [n for n in self.ctx["npcs"].values() if n.get("role") == "shop"]
        self.assertTrue(merchants)
        faces["Merchant"] = (merchants[0]["looks"][5], merchants[0]["looks"][6], merchants[0]["looks"][8])
        self.assertEqual(len(set(faces.values())), 6)
        self.assertNotIn((LOOKS[5], LOOKS[6], LOOKS[8]), faces.values())
        recs = [r for p in self.pushes if p[:2] == b"\x0b\x0a"
                for r in cm.parse_notify_char(p)["records"] if r["uid"] == 0]
        self.assertEqual(len(recs), 5 + len(merchants))

    def test_hall_master_offers_a_job_without_25_01(self):
        from nobu import guild
        self.walk_to(self.npc_named("Hall Master"))
        _, talk = self.circle()
        self.assertEqual(self.ctx["target"], guild.master_cid())
        self.assertEqual([t[:2].hex() for t in talk[:2]], ["0d06", "0d05"])
        self.assertIn("3402", [t[:2].hex() for t in talk])                      # job added

    def test_guide_talks_without_25_01(self):
        self.walk_to(self.npc_named("Guide"))
        pre, talk = self.circle()
        self.assertEqual(self.ctx["target"], npc.npc_cid())
        self.assertEqual([t[:2].hex() for t in talk[:2]], ["0d06", "0d05"])      # offer + popup
        self.assertTrue(cm.parse_notify_chat(talk[0])["msg"].startswith("Guide: "))
        self.assertIn("3402", [t[:2].hex() for t in talk])                      # quest added

    def test_overseer_completes_the_quest_without_25_01(self):
        self.walk_to(self.npc_named("Guide"))
        self.circle()
        self.walk_to(self.npc_named("Overseer"))
        _, talk = self.circle(clear=True)                   # live: sometimes 25 02 first
        self.assertEqual(self.ctx["target"], quests.overseer_cid())
        self.assertIn("3403", [t[:2].hex() for t in talk])                      # quest removed
        self.assertEqual(quests.qstate(self.ctx["char"], quests.TUTORIAL), "done")

    def test_merchant_opens_the_shop_without_25_01(self):
        keeper = next(n for n in self.ctx["npcs"].values() if n.get("role") == "shop")
        self.walk_to(keeper)
        _, talk = self.circle()
        self.assertEqual(self.ctx["target"], keeper["cid"])
        self.assertTrue(talk)
        expected = shops.answer(bytes.fromhex("28010099"), dict(self.ctx), world.store)
        self.assertEqual([t[0] for t in talk], [t[0] for t in expected])

    def test_bandit_starts_a_battle_without_25_01(self):
        self.walk_to(self.npc_named("Bandit"))
        _, talk = self.circle()
        self.assertEqual(self.ctx["target"], battle.DEFAULT_ENEMY_CID)
        self.assertIn(0x0F, [t[0] for t in talk])

    def test_nobody_in_range_is_left_alone(self):
        n = self.npc_named("Guide")
        self.walk_to(n, dx=50000)
        _, talk = self.circle()
        self.assertEqual(talk, [])
        self.assertIsNone(self.ctx.get("target"))

    def test_a_valid_target_in_range_is_kept(self):
        g, o = self.npc_named("Guide"), self.npc_named("Overseer")
        self.walk_to(g)
        world.answer(bytes.fromhex("25010031") + vce.put_u32(o["cid"]), self.ctx)   # explicit 25 01
        os.environ["NOBU_TALK_RANGE"] = "5000"
        self.circle()
        self.assertEqual(self.ctx["target"], o["cid"])


class Registry(unittest.TestCase):
    """cmd 0x17 NPC registry (npc.registry_frame): the layout the client readers 0x1a4370 / 0x1a4590 take."""

    def test_records_of_one_npc(self):
        n = dict(cid=0xFEA00003, uid=0, family="Merchant", given="", looks=[5, 20, 0, 6, 6] + [0] * 15,
                 level=1, b6b=npc.TALK_ONLY, pos=[110, 103713, 3200, 112294, 0])
        f = npc.registry_frame({"npcs": {n["cid"]: n}})
        r = vce.Reader(f)
        self.assertEqual((r.u8(), r.u8()), (0x17, 0x17))
        self.assertEqual((r.u16(), r.u32(), r.u32()), (0x0003, 0, 0xFEA00003))
        self.assertEqual((r.bytes(), r.bytes()), (b"Merchant", b""))
        self.assertEqual((r.u8(), r.u16(), r.u8()), (0x19, 0x0003, 0))        # sub 0x19, key, NPC flag
        rec = chars.char_info_record(n, 0, tuple(n["pos"]))
        self.assertEqual(f[r.off:r.off + 15], rec[8:23])                     # location part
        self.assertEqual(f[-6:], bytes.fromhex("00" "0003" "000000"))       # empty list, i16 key, 3 x u8
        body = rec[23 + 1 + len(b"Merchant") + 1:]                           # after family + empty given
        self.assertEqual(f[r.off + 15:-6], body)

    def test_off_switch(self):
        os.environ["NOBU_NPC_REG"] = "0"
        try:
            self.assertIsNone(npc.registry_frame({"npcs": {0xFEA00003: dict(cid=0xFEA00003, pos=[110, 0, 0, 0, 0])}}))
        finally:
            os.environ.pop("NOBU_NPC_REG")


if __name__ == "__main__":
    unittest.main()
