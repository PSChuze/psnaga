"""Town quests (nobu/townquests.py): python -m unittest nobu.test_townquests -v"""
import os
import unittest

from nobu import clientmodel as cm, items, townquests as tq, towns

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # faction byte [4] = 6 (Oda)
KNOBS = ("NOBU_TOWN_QUESTS", "NOBU_TOWN_SPOTS")


def records(frames):
    return [r for f in frames if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]


def removed(frames):
    return [int.from_bytes(f[4:8], "big") for f in frames if f[:2] == b"\x34\x03"]


class TownQuests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.c = {"pos": [110, 0, 0, 0, 0], "looks": list(LOOKS), "level": 20, "fame": 500}
        self.inv = items.inventory(self.c)
        self.ctx = {"char": self.c}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)

    def go(self, zone):
        self.c["pos"] = [zone, 0, 0, 0, 0]
        self.ctx["npcs"] = {}
        return tq.world_ready(self.ctx)

    def talk(self, key):
        n = next(n for n in self.ctx["npcs"].values() if n.get("key") == key)
        self.ctx["target"] = n["cid"]
        return tq.answer(bytes.fromhex("28010031"), self.ctx)

    def test_every_npc_has_a_spot_and_a_cid(self):
        cids = {tq.npc_cid(k) for k in tq.NPCS}
        self.assertEqual(len(cids), len(tq.NPCS))
        for k in tq.NPCS:
            self.assertIsNotNone(tq.spot(k), k)
        for q in tq.QUESTS.values():
            self.assertIn(q["giver"], tq.NPCS)
            for s in q["steps"]:
                self.assertIn(s["npc"], tq.NPCS)

    def test_nagoya_spawns_its_givers(self):
        recs = records(self.go(110))
        self.assertEqual(len(recs), sum(1 for v in tq.NPCS.values() if v[0] == 110))
        self.assertEqual(records(self.go(102)), [])
        os.environ["NOBU_TOWN_QUESTS"] = "0"
        self.assertEqual(self.go(110), [])

    def test_talk_errand_with_a_field_step(self):
        self.go(110)
        out = self.talk("onui")
        self.assertTrue(any(f[:2] == b"\x34\x02" for f in out))
        self.assertIn("3007", self.c["tq"])
        self.go(10)
        self.talk("matahachi")
        self.assertEqual(self.c["tq"]["3007"]["step"], 1)
        self.go(110)
        money = self.inv.get("money", 0)
        out = self.talk("onui")
        self.assertEqual(removed(out), [3007])
        self.assertNotIn("3007", self.c["tq"])
        self.assertEqual(self.inv["money"], money + 300)
        self.talk("onui")                                   # done for good: no second offer
        self.assertNotIn("3007", self.c["tq"])

    def test_paid_errand(self):
        self.go(110)
        self.talk("nihei")
        self.go(107)
        self.inv["money"] = 1000
        self.talk("saito")                                  # not enough money: nothing changes
        self.assertEqual(self.c["tq"]["3004"]["step"], 0)
        self.inv["money"] = 5000
        self.talk("saito")
        self.assertEqual(self.inv["money"], 1000)
        self.assertEqual(tq.have(self.c, 651), 1)
        self.go(110)
        self.talk("nihei")
        self.assertEqual(tq.have(self.c, 651), 0)
        self.assertEqual(self.inv["money"], 5600)

    def test_bring_with_an_item_reward(self):
        self.go(110)
        self.talk("sosuke")
        self.talk("sosuke")                                 # no comb yet: still open
        self.assertIn("3002", self.c["tq"])
        tq._give(self.c, 775, 1)
        out = self.talk("sosuke")
        self.assertEqual(removed(out), [3002])
        self.assertEqual(tq.have(self.c, 775), 0)
        self.assertEqual(tq.have(self.c, 729), 1)
        self.assertFalse(tq.open_for(self.c, 3002))         # every 2 days
        self.c["tq_done"]["3002"] = "2000-01-01T00:00:00"
        self.assertTrue(tq.open_for(self.c, 3002))

    def test_kill_step_counts_logged_kills(self):
        self.go(110)
        self.talk("sansuke")
        self.go(410)
        self.talk("geni")
        self.assertEqual(tq.step_of(self.c, 3006)["kind"], "kill")
        self.ctx["enemies_log"] = [("defeated", 1, "kiyosu_bandit", 60), ("defeated", 2, "rat", 60)]
        tq.sync_log(self.ctx)
        self.talk("geni")
        self.assertIn("3006", self.c["tq"])
        self.ctx["enemies_log"] += [("defeated", 3, "kiyosu_bandit", 60), ("defeated", 4, "kiyosu_bandit", 60)]
        out = self.talk("geni")
        self.assertEqual(removed(out), [3006])
        self.assertEqual(tq.have(self.c, 779), 1)

    def test_gates(self):
        c = {"pos": [110, 0, 0, 0, 0], "looks": list(LOOKS), "level": 1, "fame": 0}
        self.assertFalse(tq.open_for(c, 3001))              # Lv 3
        self.assertFalse(tq.open_for(c, 3003))              # fame 300
        c.update(level=20)
        c["looks"][4] = 1
        self.assertFalse(tq.open_for(c, 3006))              # Oda only
        self.assertTrue(tq.open_for(c, 3001))

    def test_detail_request(self):
        self.go(110)
        self.talk("onui")
        out = tq.answer(bytes.fromhex("2a030700") + (3007).to_bytes(4, "big"), self.ctx)
        self.assertEqual(out[0][:4], bytes.fromhex("34020700"))
        out = tq.answer(bytes.fromhex("2a030800") + (3001).to_bytes(4, "big"), self.ctx)
        self.assertEqual(out[0][:4], bytes.fromhex("34030800"))
        self.assertIsNone(tq.answer(bytes.fromhex("2a030900") + (1001).to_bytes(4, "big"), self.ctx))

    def test_spots_are_in_their_zones(self):
        for k, (zone, *_rest) in tq.NPCS.items():
            z, x, y, zz, d = tq.spot(k)
            self.assertEqual(z, zone)
            if towns.is_town(zone):
                sx, sy, sz = towns.spawn(zone)
                self.assertLess(abs(x - sx) + abs(zz - sz), 100000, k)


if __name__ == "__main__":
    unittest.main()
