"""The repairer (nobu/repair.py): python -m unittest nobu.test_repair -v"""
import os
import tempfile
import unittest

from nobu import clientmodel as cm, items, repair, shops, towns

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
KNOBS = ("NOBU_REPAIR", "NOBU_REPAIR_RATE", "NOBU_SHOPS_FILE", "NOBU_TOWN_SPOTS", "NOBU_FORCE_HOME")


def records(frames):
    return [r for f in frames if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]


class Repair(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        self.c = {"pos": [110, 0, 0, 0, 0], "looks": LOOKS, "level": 3}
        self.inv = items.inventory(self.c)
        self.gear = [it for it in self.inv["items"] if int(it.get("dur_max", 0) or 0) > 0]

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def wear(self, it, lost):
        it["dur"] = int(it["dur_max"]) - lost

    def test_nothing_to_mend(self):
        ok, line, cost = repair.repair_all(self.c)
        self.assertEqual((ok, cost), (False, 0))
        self.assertIn("Nothing", line)

    def test_mends_everything_and_charges(self):
        self.assertTrue(self.gear)
        it = self.gear[0]
        self.wear(it, int(it["dur_max"]))
        it["broken"] = True
        self.inv["money"] = 100000
        pieces, quoted = repair.quote(self.c)
        ok, line, cost = repair.repair_all(self.c)
        self.assertTrue(ok)
        self.assertEqual(cost, quoted)
        self.assertGreaterEqual(cost, 1)
        self.assertEqual(it["dur"], it["dur_max"])
        self.assertNotIn("broken", it)
        self.assertEqual(self.inv["money"], 100000 - cost)
        self.assertEqual(repair.worn(self.inv), [])

    def test_fee_is_half_the_shop_price_per_lost_share(self):
        cat = {1616: 65}                                    # Rusty Sword, 65 mon in 2004
        it = dict(id=1616, dur=10, dur_max=20)
        self.assertEqual(repair.fee(it, cat), 16)           # 65 x 0.5 x 10/20 = 16.25
        self.assertEqual(repair.fee(dict(id=9999, dur=5, dur_max=20), cat), 15)     # unsold gear: 1 mon a point

    def test_not_enough_money_quotes_and_changes_nothing(self):
        it = self.gear[0]
        self.wear(it, 5)
        before = int(it["dur"])
        self.inv["money"] = 0
        ok, line, cost = repair.repair_all(self.c)
        self.assertFalse(ok)
        self.assertIn("costs", line)
        self.assertEqual(int(it["dur"]), before)

    def test_a_repairer_in_every_home_town(self):
        cids = set()
        for m in towns.TOWNS:
            ctx = {"char": {"pos": [m, 0, 0, 0, 0], "looks": LOOKS, "level": 3}}
            recs = records(repair.spawn_pushes(ctx))
            self.assertEqual(len(recs), 1, m)
            self.assertTrue(cm.npc_actor_created(recs[0]))
            self.assertEqual(ctx["npcs"][recs[0]["cid"]]["role"], repair.ROLE)
            cids.add(recs[0]["cid"])
        self.assertEqual(len(cids), len(towns.TOWNS))
        self.assertEqual(repair.spawn_pushes({"char": {"pos": [10, 0, 0, 0, 0], "looks": LOOKS}}), [])  # a field

    def test_talk_repairs(self):
        ctx = {"char": self.c}
        repair.spawn_pushes(ctx)
        ctx["target"] = next(iter(ctx["npcs"]))
        self.wear(self.gear[0], 3)
        self.inv["money"] = 100000
        out = repair.answer(bytes.fromhex("28010031"), ctx)
        self.assertTrue(any(o[:2] == bytes.fromhex("3006") for o in out))     # the money total
        self.assertEqual(repair.worn(self.inv), [])
        self.assertIsNone(repair.answer(bytes.fromhex("28010031"), {"char": self.c, "target": 1}))


if __name__ == "__main__":
    unittest.main()
