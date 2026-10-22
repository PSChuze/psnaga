"""The doctor (nobu/doctor.py): python -m unittest nobu.test_doctor -v"""
import os
import unittest

from nobu import clientmodel as cm, doctor, items, towns, upkeep

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]
KNOBS = ("NOBU_DOCTOR", "NOBU_DOCTOR_RATE", "NOBU_TOWN_SPOTS", "NOBU_FORCE_HOME")


def records(frames):
    return [r for f in frames if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]


class Doctor(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.c = {"pos": [110, 0, 0, 0, 0], "looks": LOOKS, "level": 5}
        self.inv = items.inventory(self.c)
        self.mx_life, self.mx_ki = upkeep.maxima(self.c)[:2]

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)

    def hurt(self, life, ki):
        s = upkeep.state(self.c)
        s["life"], s["ki"] = self.mx_life - life, self.mx_ki - ki

    def test_healthy(self):
        ok, line, cost = doctor.treat(self.c)
        self.assertEqual((ok, cost), (False, 0))

    def test_treats_and_charges(self):
        self.hurt(10, 6)
        self.inv["money"] = 1000
        ok, line, cost = doctor.treat(self.c)
        self.assertTrue(ok)
        self.assertEqual(cost, 8)                                       # (10 + 6) x 0.5
        self.assertEqual(self.inv["money"], 992)
        self.assertEqual(doctor.missing(self.c), (0, 0))

    def test_no_money_names_the_price(self):
        self.hurt(10, 0)
        self.inv["money"] = 0
        ok, line, cost = doctor.treat(self.c)
        self.assertFalse(ok)
        self.assertIn("costs", line)
        self.assertEqual(doctor.missing(self.c)[0], 10)

    def test_ghost_goes_to_the_shrine(self):
        self.hurt(10, 0)
        self.c["ghost"] = True
        ok, line, cost = doctor.treat(self.c)
        self.assertFalse(ok)
        self.assertIn("shrine", line)

    def test_a_doctor_in_every_home_town(self):
        cids = set()
        for m in towns.TOWNS:
            ctx = {"char": {"pos": [m, 0, 0, 0, 0], "looks": LOOKS, "level": 3}}
            recs = records(doctor.spawn_pushes(ctx))
            self.assertEqual(len(recs), 1, m)
            self.assertEqual(ctx["npcs"][recs[0]["cid"]]["role"], doctor.ROLE)
            cids.add(recs[0]["cid"])
        self.assertEqual(len(cids), len(towns.TOWNS))

    def test_talk_treats(self):
        ctx = {"char": self.c}
        doctor.spawn_pushes(ctx)
        ctx["target"] = next(iter(ctx["npcs"]))
        self.hurt(4, 4)
        self.inv["money"] = 100
        out = doctor.answer(bytes.fromhex("28010031"), ctx)
        self.assertTrue(any(o[:2] == bytes.fromhex("3006") for o in out))
        self.assertEqual(doctor.missing(self.c), (0, 0))


if __name__ == "__main__":
    unittest.main()
