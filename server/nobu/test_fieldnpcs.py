"""Field people (nobu/fieldnpcs.py) and the old sage (nobu/elder.py): python -m unittest nobu.test_fieldnpcs -v"""
import os
import unittest

from nobu import clientmodel as cm, elder, fieldnpcs as fn, items, towns, townquests

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
KNOBS = ("NOBU_FIELD_NPCS", "NOBU_FERRY_FARE", "NOBU_ELDER", "NOBU_TOWN_SPOTS")
TALK = bytes.fromhex("28010031")


def records(frames):
    return [r for f in frames if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]


class Field(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.c = {"pos": [10, 70000, 2900, 172000, 0], "looks": LOOKS, "level": 5, "cid": 1}
        self.inv = items.inventory(self.c)
        self.ctx = {"char": self.c, "zone": 10}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)

    def spawn(self, night=False):
        self.ctx["npcs"] = {}
        return records(fn.spawn_pushes(self.ctx, night=night))

    def target(self, role):
        n = next(n for n in self.ctx["npcs"].values() if n.get("spot") == role)
        self.ctx["target"] = n["cid"]
        return n

    def test_every_route_has_both_ends(self):
        sp = fn.load()
        for m, row in sp.items():
            for role in row:
                if role.startswith("ferry_"):
                    self.assertIn("ferry_%s" % m, sp[role.split("_")[1]], (m, role))

    def test_owari_people(self):
        day = self.spawn()
        roles = {n["spot"] for n in self.ctx["npcs"].values()}
        self.assertTrue({"ferry_7", "ferry_19", "headman_0"} <= roles)
        self.assertTrue(any(r.startswith("cp_") for r in roles))
        self.assertEqual(len({r["cid"] for r in day}), len(day))
        self.assertNotEqual(len(day), len(self.spawn(night=True)))   # farmers go home at night
        os.environ["NOBU_FIELD_NPCS"] = "0"
        self.assertEqual(self.spawn(), [])

    def test_ferry_charges_and_crosses(self):
        self.spawn()
        n = self.target("ferry_7")
        self.inv["money"] = 100
        out = fn.answer(TALK, self.ctx)
        self.assertFalse(any(f[:2] == b"\x0b\x02" for f in out))     # no fare, no crossing
        self.inv["money"] = 600
        out = fn.answer(TALK, self.ctx)
        self.assertEqual(self.inv["money"], 100)
        self.assertTrue(any(f[:2] == b"\x0b\x02" for f in out))
        self.assertEqual(int(self.c["pos"][0]), 7)
        self.assertEqual(n["ferry_to"], 7)

    def test_villager_talks(self):
        self.spawn()
        self.target("headman_0")
        out = fn.answer(TALK, self.ctx)
        self.assertTrue(any(b"Village Headman" in f for f in out))


class Elder(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)

    def test_one_in_every_home_town(self):
        for m in towns.TOWNS:
            ctx = {"char": {"pos": [m, 0, 0, 0, 0], "looks": LOOKS, "level": 3}}
            self.assertEqual(len(records(elder.spawn_pushes(ctx))), 1, m)
            self.assertIsNotNone(towns.spot(m, "elder"), m)
        self.assertEqual(elder.spawn_pushes({"char": {"pos": [10, 0, 0, 0, 0], "looks": LOOKS}}), [])

    def test_repairs_stale_records(self):
        c = {"pos": [110, 0, 0, 0, 0], "looks": LOOKS, "level": 3,
             "tq": {"3999": {"step": 0}, "3007": {"step": 0, "kills": 0}}, "guild": {"job": 2998, "kills": 1}}
        ctx = {"char": c}
        elder.spawn_pushes(ctx)
        ctx["target"] = next(iter(ctx["npcs"]))
        out = elder.answer(TALK, ctx)
        self.assertEqual(set(c["tq"]), {"3007"})
        self.assertIsNone(c["guild"]["job"])
        self.assertTrue(any(f[:2] == b"\x34\x01" for f in out))
        self.assertIn(3007, townquests.active(c))
        out = elder.answer(TALK, ctx)                         # nothing left to fix: a tip
        self.assertEqual(c["elder_tip"], 1)


if __name__ == "__main__":
    unittest.main()
