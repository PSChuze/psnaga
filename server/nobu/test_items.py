"""Inventory (cmd 0x24): python -m unittest nobu.test_items -v

The parsers below are models of how the client reads each reply:
0x287390 (backpack), 0x1ec7f0 (item record), 0x1ecca0 (tail count), 0x1ecf30 (item info),
0x1ee160 (dispinfo). They are written independently of items.py's encoders."""
import os
import tempfile
import unittest

from nobu import chars, items, vce
from nobu.clientmodel import Arc, ParseError, parse_cmd24

# ---------------------------------------------------------------------------- client models
# 0x1ec7f0 read half: (reader, struct offset) in wire order
RECORD_READ = [("u32", 0x00), ("u32", 0x04), ("u8", 0x0C), ("u8", 0x0D), ("u16", 0x08), ("u8", 0x40),
               ("u16", 0x0E), ("u16", 0x0A), ("u8", 0x10), ("u8", 0x11), ("u16", 0x14), ("u8", 0x16),
               ("u16", 0x12), ("u8", 0x17), ("u8", 0x18), ("str21", 0x19), ("u32", 0x3C)]
# 0x1ecf30 read half (0x1ed668..): after u8 +0 and two strings (max 0x11)
INFO_READ = ([("u8", 0x23), ("u16", 0x24), ("u8", 0x26), ("u16", 0x28), ("u8", 0x2A)]
             + [("u8", o) for o in range(0x2E, 0x3A)] + [("u16", 0x3E), ("u16", 0x40)]
             + [("i8", o) for o in range(0x42, 0x4B)]
             + [("u16", 0x4C), ("u16", 0x4E), ("u16", 0x50), ("i16", 0x52), ("u8", 0x54)]
             + [("i8", o) for o in range(0x55, 0x59)]
             + [("u8", 0x59), ("u8", 0x5A), ("i8", 0x5B), ("u8", 0x5C), ("u8", 0x5D), ("u8", 0x5E),
                ("u16", 0x60), ("u32", 0x64), ("u32", 0x68), ("u16", 0x3A), ("u16", 0x3C),
                ("i16", 0x6C), ("i16", 0x6E), ("u8", 0x2B), ("u16", 0x2C), ("u16", 0x62)])


def _get(a, kind):
    if kind in ("u8", "i8"):
        v = a.u8()
        return v - 0x100 if kind == "i8" and v >= 0x80 else v
    if kind in ("u16", "i16"):
        v = a.u16()
        return v - 0x10000 if kind == "i16" and v >= 0x8000 else v
    if kind == "u32":
        return a.u32()
    if kind == "str21":
        return a.str(0x21)
    raise AssertionError(kind)


def read_record(a):
    return {off: _get(a, kind) for kind, off in RECORD_READ}


def read_info(a):
    d = {0: a.u8(), 1: a.str(0x11), 0x12: a.str(0x11)}
    d.update({off: _get(a, kind) for kind, off in INFO_READ})
    return d


def parse_backpack(payload):
    """0x287390: u32, u32, bytes(max 0xc), i8 count, records; if bytes remain u16 A, u16 B and
    count x u16 (0x1ecca0 -> record +0x40). Defaults A = 50, B = 99."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    if sub != 1:
        raise ParseError("not a backpack reply")
    kan, mon = a.u32(), a.u32()
    equip = a.bytes(0xC)
    n = a.u8()
    n = n - 0x100 if n >= 0x80 else n
    recs = [read_record(a) for _ in range(max(n, 0))]
    A, B = 50, 99
    if not a.done():
        A, B = a.u16(), a.u16()
        for rec in recs:
            rec[0x40] = a.u16()
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(seq=seq, kan=kan, mon=mon, equip=equip, records=recs, A=A, B=B)


def parse_info(payload):
    """0x287768 -> 0x1ece80: info struct, then the optional 0x1edda0 part (u16, u16, 10 x i8)."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    d = read_info(a)
    if not a.done():
        d["tail"] = [a.u16(), a.u16()] + [a.u8() for _ in range(10)]
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(sub=sub, seq=seq, info=d)


def parse_dispinfo(payload):
    """0x287b60 -> 0x1ee160: record, info struct, [u16 count, [0x1edda0 part]]."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    rec, info = read_record(a), read_info(a)
    if not a.done():
        rec[0x40] = a.u16()
        if not a.done():
            info["tail"] = [a.u16(), a.u16()] + [a.u8() for _ in range(10)]
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(sub=sub, seq=seq, record=rec, info=info)


def fits(cat, pos):
    """0x200170 with table 0x423430."""
    return 0 <= pos < 12 and cat == [1, 2, 3, 4, 5, 5, 6, 7, 9, 8, 0xB, 0xA][pos]


# ---------------------------------------------------------------------------- tests
class Items(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        looks = [5, 20, 0, 1, 6] + [0] * 15                   # male Samurai (job 1), Oda
        self.c = self.store.create(0x04000471, 0, "織田", "太郎", looks)
        self.ctx = {"uid": 0x04000471, "cid": self.c["cid"], "char": self.c}

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, hexreq):
        reps = items.answer(bytes.fromhex(hexreq), self.ctx, lambda: self.store)
        self.assertIsNotNone(reps)
        self.assertEqual(len(reps), 1)
        return reps[0]

    def reload(self):
        return chars.CharStore(self.store.path).find(0x04000471, self.c["cid"])

    def backpack(self):
        return parse_backpack(self.ask("24010005"))

    def test_no_character_falls_through(self):
        self.assertIsNone(items.answer(bytes.fromhex("24010002"), {}, None))
        self.assertIsNone(items.answer(bytes.fromhex("240f0002"), self.ctx, None))
        self.assertIsNone(items.answer(bytes.fromhex("26010002"), self.ctx, None))

    def test_backpack_starter_kit_parses(self):
        bp = self.backpack()
        self.assertEqual(bp["seq"], 5)
        self.assertEqual((bp["kan"], bp["mon"]), (1, 500))              # 1500 mon
        self.assertEqual((bp["A"], bp["B"]), (50, 99))
        ids = [r[0] for r in bp["records"]]
        self.assertEqual(ids, [1605, 1671, 201, 202, 279, 188])
        self.assertEqual([r[0x0C] for r in bp["records"]], [0, 1, 2, 3, 4, 5])
        self.assertEqual([r[0x40] for r in bp["records"]], [1, 1, 5, 3, 3, 1])
        self.assertEqual(bp["equip"], bytes([0, 0xFF, 0xFF, 1, 0xFF, 0xFF, 0xFF, 5] + [0xFF] * 4))
        bag = bp["records"][5]                                          # worn container (0x61bb60)
        self.assertEqual((bag[0x08], bag[0x10], bag[0x18]), (60, 7, 30))
        self.assertTrue(fits(bag[0x10], 7))
        weapon, body = bp["records"][0], bp["records"][1]
        self.assertTrue(fits(weapon[0x10], 0) and fits(body[0x10], 3))
        self.assertEqual(bp["records"][2][0x0D] & 1, 1)                 # potions stack
        self.assertEqual(items.item_name(1605), "Kogatana")
        # persisted in chars.json
        self.assertEqual([i["id"] for i in self.reload()["inventory"]["items"]], ids)

    def test_every_job_gets_a_valid_kit(self):
        for job in range(0, 9):
            inv = items.starter_kit(job)
            worn = {it["pos"]: it for it in inv["items"] if it.get("pos") is not None}
            self.assertEqual(sorted(worn), [0, 3, 7])
            self.assertEqual(items.bag_slots(inv), 30)
            for pos, it in worn.items():
                self.assertTrue(fits(it["cat"], pos), (job, pos, it))
                self.assertIn(it["id"], items.names())

    def test_big_counts_use_the_tail(self):
        inv = items.inventory(self.c)
        inv["items"][2]["count"] = 300
        bp = self.backpack()
        self.assertEqual(bp["records"][2][0x40], 300)

    def test_item_info_and_dispinfo(self):
        self.backpack()
        info = parse_info(self.ask("24020006" + "00"))
        self.assertEqual((info["sub"], info["seq"], info["info"][0]), (2, 6, 0))
        self.assertEqual(info["info"][0x24], 0x7F)                      # Kogatana: all jobs (wiki), = record +0x14
        self.assertEqual(self.backpack()["records"][0][0x14], 0x7F)
        self.assertEqual((info["info"][0x2F], info["info"][0x30], info["info"][0x31]), (65, 65, 65))  # Durability65
        self.assertEqual((info["info"][0x35], info["info"][0x36], info["info"][0x38]), (10, 32, 32))  # Value, attack
        self.assertEqual(parse_info(self.ask("24020007" + "31"))["info"][0], 0xFF)
        d = parse_dispinfo(self.ask("24080008" + "000000c9"))           # Heal Powder
        self.assertEqual((d["record"][0], d["record"][0x40], d["record"][0x08]), (201, 5, 52))
        d = parse_dispinfo(self.ask("24080009" + "00000001"))           # not owned: generic
        self.assertEqual((d["record"][0], d["record"][0x40]), (1, 1))

    def test_equip_unequip(self):
        self.backpack()
        got = parse_cmd24(self.ask("2405000a" + "00"))                  # take the weapon off
        self.assertEqual(got["vals"], [0])
        self.assertEqual(self.backpack()["equip"][0], 0xFF)
        got = parse_cmd24(self.ask("2404000b" + "0000"))                # put it back on
        self.assertEqual(got["vals"], [0, 0])
        self.assertEqual(self.backpack()["equip"][0], 0)
        self.assertEqual(self.reload()["inventory"]["items"][0]["pos"], 0)
        # wrong position for the category, unknown index -> index 0xff (client error path 0x2077a0)
        self.assertEqual(parse_cmd24(self.ask("2404000c" + "0003"))["vals"], [0xFF, 3])
        self.assertEqual(parse_cmd24(self.ask("2404000d" + "2a00"))["vals"], [0xFF, 0])
        self.assertEqual(parse_cmd24(self.ask("2404000e" + "0200"))["vals"], [0xFF, 0])  # potion

    def test_drop(self):
        self.backpack()
        got = parse_cmd24(self.ask("2406000f" + "0202" + "000000c9"))   # drop 2 of 5 powders
        self.assertEqual(got["vals"], [2, 2, 2])
        self.assertEqual(self.backpack()["records"][2][0x40], 3)
        got = parse_cmd24(self.ask("24060010" + "0409" + "00000117"))   # all 3 dango (asked 9)
        self.assertEqual(got["vals"], [4, 9, 3])
        self.assertEqual([r[0] for r in self.backpack()["records"]], [1605, 1671, 201, 202, 188])
        # equipped, wrong id, unknown index -> index 0xff (the client re-fetches the backpack)
        for req in ("0001" + "00000645", "0301" + "00000001", "3001" + "00000001"):
            self.assertEqual(parse_cmd24(self.ask("24060011" + req))["vals"][0], 0xFF)

    def test_use_consumes(self):
        self.backpack()
        self.assertEqual(self.ask("24030012" + "0300" + "000000ca").hex(), "24030012")
        self.assertEqual(self.backpack()["records"][3][0x40], 2)
        self.ask("24030013" + "0000" + "00000645")                      # weapon: not consumed
        self.assertEqual(self.backpack()["records"][0][0x40], 1)

    def test_consumables_carry_a_use_effect(self):
        # record +0x0e -> obj+0x68: the client's Use (0x367f70) needs it nonzero or it shows "cannot be used"
        recs = {r[0]: r for r in self.backpack()["records"]}
        self.assertEqual(recs[279][0x0E], 255)                          # dango: 1.255 Meal
        self.assertEqual(recs[201][0x0E], 705)                          # healing powder: 1.705 Heal
        self.assertEqual(recs[1605][0x0E], 0)                           # weapon / bag: not usable
        self.assertEqual(recs[188][0x0E], 0)

    def test_sort_renumbers_and_returns_equipment(self):
        inv = items.inventory(self.c)
        inv["items"].reverse()                                          # dango first, weapon last
        items.renumber(inv)
        self.assertEqual(items.equip_bytes(inv)[0], 5)
        got = parse_cmd24(self.ask("24070014"))
        self.assertEqual(got["order"], bytes([0, 0xFF, 0xFF, 1, 0xFF, 0xFF, 0xFF, 2] + [0xFF] * 4))
        self.assertEqual([r[0] for r in self.backpack()["records"]], [1605, 1671, 188, 201, 202, 279])

    def test_old_character_gets_the_bag_once_on_24_01(self):
        inv = items.inventory(self.c)
        inv["items"] = [it for it in inv["items"] if it["id"] != 188]      # a pre-bag chars.json entry
        self.c.pop("starter_bag")
        reps = items.answer(bytes.fromhex("24010005"), self.ctx, lambda: self.store)
        self.assertEqual(len(reps), 2)                                  # backpack + a system line
        bp = parse_backpack(reps[0])
        self.assertEqual(bp["equip"][7], [r[0] for r in bp["records"]].index(188))
        self.assertEqual(self.ctx["item_log"][-1], ("starter_bag", 188))
        self.assertEqual(reps[1][:2], b"\x0d\x06")
        saved = self.reload()
        self.assertTrue(saved["starter_bag"])
        self.assertEqual(sum(1 for it in saved["inventory"]["items"] if it["id"] == 188), 1)
        # repeat loads never add another, even after the bag is taken off
        self.assertEqual(len(items.answer(bytes.fromhex("24010006"), self.ctx, lambda: self.store)), 1)
        self.ask("24050007" + "07")
        self.backpack()
        self.assertEqual(sum(1 for it in items.inventory(self.c)["items"] if it["id"] == 188), 1)

    def test_migration_keeps_a_worn_container_and_waits_when_full(self):
        inv = items.inventory(self.c)
        inv["items"] = [it for it in inv["items"] if it["id"] != 188]
        self.c.pop("starter_bag")
        inv["items"].append(dict(id=1755, type=60, cat=7, count=1, flags=0, charges=50, idx=9, pos=7))
        self.backpack()
        self.assertEqual([it["id"] for it in inv["items"] if it.get("pos") == 7], [1755])
        self.assertTrue(self.c["starter_bag"])
        c2 = self.store.create(0x04000471, 1, "織田", "次郎", [5, 20, 0, 1, 6] + [0] * 15)
        inv2 = items.inventory(c2)
        inv2["items"] = [dict(id=201, type=52, count=1, flags=1, idx=i) for i in range(items.CAPACITY)]
        c2.pop("starter_bag")
        self.assertIsNone(items.ensure_container(c2, inv2))
        self.assertNotIn("starter_bag", c2)                             # retried next time

    def test_describe(self):
        self.assertIn("EQUIP_ITEM", items.describe(bytes.fromhex("24040001" + "0003")))
        self.assertIn("pos=3", items.describe(bytes.fromhex("24040001" + "0003")))
        self.assertIn("id=201", items.describe(bytes.fromhex("24080001" + "000000c9")))
        s = items.describe(self.ask("24010005"), "S>C")
        self.assertIn("money=1kan500mon", s)
        self.assertIsNone(items.describe(bytes.fromhex("23010001")))


if __name__ == "__main__":
    unittest.main()
