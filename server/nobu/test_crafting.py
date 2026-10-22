"""Crafting: python -m unittest nobu.test_crafting -v"""
import json
import os
import re
import tempfile
import unittest

from nobu import chars, clientmodel as cm, crafting, items, shops, vce

UID = 0x04000471
SMITH = dict(cid=2, uid=UID, slot=0, family="ああ", given="いい", looks=[4, 20, 0, 6, 6] + [0] * 15,
             pos=[110, 100000, 3000, 100000, 0])
KNOBS = ("NOBU_CRAFT_FILE", "NOBU_CRAFTING", "NOBU_CRAFT_ALL_JOBS", "NOBU_CRAFT_MASK",
         "NOBU_CRAFT_REOPEN", "NOBU_CRAFT_GRANT", "NOBU_CRAFT_ANY_ZONE", "NOBU_CRAFT_SEED",
         "NOBU_SHOP_ITEM_PUSH")


def tsv(path):
    with open(path, encoding="utf-8") as fh:
        return {r.split("	")[0]: r.split("	")[2] for r in fh.read().splitlines()[1:]}


# ---------------------------------------------------------------------------- client models
def parse_entry(a):
    """0x1ee760 wire order (15 bytes)."""
    return dict(n=a.u8(), b1=a.u8(), cat=a.u8(), w8=a.u16(), type=a.u16(), flags6=a.u16(),
                id=a.u32(), slot=a.u8(), flags4=a.u8())


def parse_materials(a):
    """0x1eebe0: u8, u8 n (<= 100 entries in 0x640 bytes), n x entry."""
    m = dict(a=a.u8(), n=a.u8())
    if m["n"] > 100:
        raise cm.ParseError("materials overflow")
    m["entries"] = [parse_entry(a) for _ in range(m["n"])]
    return m


def parse_craft(a):
    """0x227850 read half: u8 +0xc, u16 +0xe, bytes(+0x10, max count), count x entry (+0x74),
    0x1eebe0 (+0x6b4), header 0x1eafc0. The object holds 100 entries."""
    o = dict(mode=a.u8(), count=a.u16())
    if o["count"] > 100:
        raise cm.ParseError("count overflows the 100-entry object")
    o["flags"] = a.bytes(o["count"])
    o["products"] = [parse_entry(a) for _ in range(o["count"])]
    o["materials"] = parse_materials(a)
    flags = a.u32()
    a.u16()
    if flags & 1:
        a.u32()
    a.u8()
    return o


def parse_event(payload):
    a = cm.Arc(payload, 1)
    ev = dict(id=a.u8(), n=a.u8(), kind=a.u8())
    if ev["kind"] != 8:
        raise cm.ParseError("not a craft record")
    ev["craft"] = parse_craft(a)
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return ev


def parse_30(payload):
    """0x288570: u8 sub, u16 seq; sub 0xb -> 0x1eebe0, 0xf -> bytes(max 0xd), 0x10 -> 0x227850."""
    a = cm.Arc(payload, 1)
    sub, _ = a.u8(), a.u16()
    if sub == 0x0B:
        out = parse_materials(a)
    elif sub == 0x0F:
        out = a.bytes(0xD)
    elif sub == 0x10:
        out = parse_craft(a)
    else:
        raise cm.ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return out


def mask_apply(entries, mask):
    """0x227a40 (mode 0): bit i set clears entry i +0 bit 7, clear sets it."""
    out = []
    for i, n in enumerate(entries):
        bit = mask[i >> 3] & (1 << (i & 7)) if (i >> 3) < len(mask) else 0
        out.append(n & 0x7F if bit else n | 0x80)
    return out


def client_can_make(materials, backpack):
    """0x6505a0: for every material entry, backpack count of that item (by name; here by id) // need;
    the minimum. 0 -> the client shows 2.277 Missing materials."""
    best = None
    for e in materials["entries"]:
        have = sum(it["count"] for it in backpack if it["id"] == e["id"])
        k = have // e["n"]
        best = k if best is None else min(best, k)
    return best or 0


# ---------------------------------------------------------------------------- fixtures
class Base(unittest.TestCase):
    ME = SMITH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.pop(k, None) for k in KNOBS}
        os.environ["NOBU_CRAFT_FILE"] = os.path.join(self.tmp.name, "crafting.json")
        os.environ["NOBU_CRAFT_SEED"] = "1"
        self.store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.store.data["chars"][str(UID)] = [json.loads(json.dumps(self.ME))]
        self.store.save()
        self.ctx = {"char": self.store.find(UID, 2), "cid": 2, "uid": UID}

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        for k, v in self.old.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def inv(self):
        return items.inventory(self.ctx["char"], self.store)

    def talk(self):
        self.assertEqual(len(crafting.spawn_pushes(self.ctx)), 1)
        self.ctx["target"] = 0xFEA00010
        return crafting.answer(bytes.fromhex("28010031"), self.ctx, self.store)

    def reply(self, index, select=False):
        eid = self.ctx["craft_event"]["id"]
        return shops.encode_event_reply(eid, [(crafting.ANSWER_CRAFT, crafting.answer_value(index, select))])

    def recipe_index(self, key):
        return self.ctx["craft_event"]["keys"].index(key)


# ---------------------------------------------------------------------------- tests
class Data(Base):
    def test_seed_ids_match_strdat(self):
        data = crafting.load_data()
        self.assertTrue(os.path.exists(os.environ["NOBU_CRAFT_FILE"]))
        names = items.tsv_names("strdat_00_items.tsv")
        jobs = set()
        for r in data["recipes"]:
            self.assertIn(f"0.{r['product']['id']}", names, r["key"])
            self.assertEqual(items.item_name(r["product"]["id"]), r["product"]["name"], r["key"])   # English by id
            self.assertEqual(items.skill_name(r["skill"]), r["skill_name"], r["key"])
            for m in r["materials"]:
                self.assertEqual(items.item_name(m["id"]), m["name"], r["key"])
            jobs.update(r["jobs"])
        self.assertTrue({3, 6, 7} <= jobs)                       # Priest, Smith, Physician
        self.assertIn(201, [r["product"]["id"] for r in data["recipes"]])   # Heal Powder
        for job, mats in data["grants"].items():
            for mid, name, _n in mats:
                self.assertEqual(items.item_name(mid), name)
        cid = shops.parse_cid(data["workshops"]["nagoya_workshop"]["npc"]["cid"])
        self.assertEqual(cid, 0xFEA00010)
        self.assertTrue(crafting.npc.is_npc_cid(cid))


class Event(Base):
    def test_talk_opens_the_craft_event_with_the_smith_list(self):
        out = self.talk()
        kinds = [o[:2].hex() for o in out]
        self.assertEqual(kinds[0], "0d06")                        # greeting
        self.assertIn("3003", kinds)                              # trial materials pushed
        ev_raw = next(o for o in out if o[0] == 0x11)
        self.assertEqual(ev_raw[:4], bytes.fromhex("11010108"))   # id 1, 1 record, kind 8
        ev = parse_event(ev_raw)
        c = ev["craft"]
        self.assertEqual((c["mode"], c["count"]), (0, 3))
        self.assertEqual([p["id"] for p in c["products"]], [1621, 1636, 1611])
        self.assertTrue(all(p["flags6"] == 1 and p["slot"] == 0xFF for p in c["products"]))
        self.assertTrue(all(p["n"] == 1 for p in c["products"]))
        self.assertEqual(c["materials"]["n"], 0)
        self.assertIn("craft mode=0 products=3", crafting.describe(ev_raw, "S>C"))
        mask = parse_30(out[-1])
        # trial materials cover Shinai and Club, not Metezashi (no Wood Handle / Whetstone)
        self.assertEqual(mask_apply([1, 1, 1], mask), [1, 1, 0x81])

    def test_first_bytes(self):
        r = crafting.CRAFT_SEED["recipes"][0]
        ev = crafting.craft_event(5, [r])
        self.assertEqual(ev.hex(),
                         "11050108" "00" "0001" "01" "00"
                         "01" "00" "01" "0000" "0003" "0001" "00000655" "ff" "00"
                         "00" "00" "00000000" "0000" "00")
        parse_event(ev)

    def test_select_sends_the_materials(self):
        self.talk()
        out = crafting.answer(self.reply(0, select=True), self.ctx, self.store)
        self.assertEqual(len(out), 1)
        m = parse_30(out[0])
        self.assertEqual([(e["id"], e["n"]) for e in m["entries"]], [(502, 2), (1246, 1)])
        backpack = self.inv()["items"]
        self.assertEqual(client_can_make(m, backpack), 2)         # 4 Bamboo / 2, 2 Polish Grass / 1
        self.assertIn("select index=0", crafting.describe(self.reply(0, True)))

    def test_other_npc_disabled_and_zone(self):
        crafting.spawn_pushes(self.ctx)
        self.ctx["npcs"][0xFEA00003] = dict(cid=0xFEA00003, role="shop")
        self.ctx["target"] = 0xFEA00003
        self.assertIsNone(crafting.answer(bytes.fromhex("28010031"), self.ctx, self.store))
        os.environ["NOBU_CRAFTING"] = "0"
        self.assertEqual(crafting.spawn_pushes(self.ctx), [])
        os.environ.pop("NOBU_CRAFTING")
        self.ctx["char"]["pos"] = [112, 0, 0, 0, 0]              # Toyama: a town, but no clan's home (towns.py)
        self.assertEqual(crafting.spawn_pushes(self.ctx), [])
        self.ctx["char"]["pos"] = [111, 0, 0, 0, 0]              # Inabayama (Saito's home): its own Craftsman
        self.ctx["npcs"] = {}
        self.assertEqual(len(crafting.spawn_pushes(self.ctx)), 1)
        self.assertEqual(list(self.ctx["npcs"]), [shops.town_cid(111, 0x10)])

    def test_foreign_event_ids_go_to_shops(self):
        self.assertIsNone(crafting.answer(shops.encode_event_reply(9, [(6, 0x40000101)]), self.ctx,
                                          self.store))
        self.talk()
        self.assertIsNone(crafting.answer(shops.encode_event_reply(9, [(6, 0x40000101)]), self.ctx,
                                          self.store))


class Make(Base):
    def test_names_come_from_the_ids_not_the_file(self):
        data = crafting.load_data()                     # a crafting.json with other names in it
        for r in data["recipes"]:
            r["skill_name"] = "?"
            for g in [r["product"]] + r["materials"]:
                g["name"] = "?"
        with open(os.environ["NOBU_CRAFT_FILE"], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        jp = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
        out = self.talk()
        lines = [o[3:].decode("utf-8") for o in out if o[:2] == b"\x0d\x06"]
        self.assertTrue(any("The craftsman hands you trial materials: Bamboo x4," in t for t in lines), lines)
        data = crafting.load_data()
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "metezashi"))
        self.assertEqual(res["text"], "You need Mk Katana 1 Lv 10 to make that.")
        self.ctx["char"]["crafting"] = {"505": 10}
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "metezashi"))
        self.assertTrue(res["text"].startswith("Missing materials: Wood Handle 0/1"), res["text"])
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "shinai"))
        self.assertIn("Shinai", res["text"])
        self.assertIn("Mk Katana 1: Lv", res["text"])
        maker = crafting.maker_name(self.ctx["char"])          # the player's own (Japanese) name is theirs
        for t in lines + [res["text"].replace(maker, "")]:
            self.assertIsNone(jp.search(t), t)

    def test_make_shinai_consumes_and_adds_with_maker(self):
        self.talk()
        inv = self.inv()
        bamboo = sum(it["count"] for it in inv["items"] if it["id"] == 502)
        i = self.recipe_index("shinai")
        crafting.load_data()
        out = crafting.answer(self.reply(i), self.ctx, self.store)
        kinds = [o[:2].hex() for o in out]
        self.assertEqual(kinds[:4], ["3004", "3003", "0d06", "300b"])
        made = [it for it in inv["items"] if it["id"] == 1621]
        log = self.ctx["craft_log"][-1]
        if log[1]:
            self.assertEqual(len(made), 1)
            self.assertEqual(made[0]["maker"], "ああ いい")
            rec = items.item_record(made[0])
            self.assertIn("ああ いい".encode("utf-8"), rec)       # record +0x19 string
            self.assertEqual(sum(it["count"] for it in inv["items"] if it["id"] == 502), bamboo - 2)
            self.assertEqual(crafting.proficiency(self.ctx["char"], 505), 3)
        else:
            self.assertEqual(made, [])
            self.assertEqual(crafting.proficiency(self.ctx["char"], 505), 1)
        self.assertIn("make index=0", crafting.describe(self.reply(i)))
        with open(self.store.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertIn("crafting", saved["chars"][str(UID)][0])

    def test_success_and_failure_rolls(self):
        self.talk()
        data = crafting.load_data()
        r = crafting.by_key(data, "konbou")
        inv = self.inv()
        crafting.give(inv, dict(id=1477, stack=True), 20)
        crafting.give(inv, dict(id=1473, stack=True), 20)
        crafting.give(inv, dict(id=420, stack=True), 20)
        results = []
        for _ in range(6):
            res = crafting.make(self.ctx, self.store, data, r)
            self.assertTrue(res["ok"])
            results.append(res["success"])
        self.assertIn(True, results)
        wins = results.count(True)
        fails = results.count(False)
        self.assertEqual(crafting.proficiency(self.ctx["char"], 502), 3 * wins + 1 * fails)
        # failure keeps half of each 2-count material: 1 consumed instead of 2
        used = 2 * (wins + 1) + 1 * fails        # trial grant had 4 Soft Charcoal + 20 added
        self.assertEqual(crafting.held(inv, 1477), 24 - used + 2)

    def test_rate_curve(self):
        data = crafting.load_data()
        r = crafting.by_key(data, "metezashi")
        self.assertAlmostEqual(crafting.success_rate(data, r, 10), 0.75)
        self.assertAlmostEqual(crafting.success_rate(data, r, 30), 0.95)
        self.assertAlmostEqual(crafting.success_rate(data, r, 1000), 0.95)

    def test_missing_materials_level_and_job(self):
        os.environ["NOBU_CRAFT_GRANT"] = "0"
        self.talk()
        out = crafting.answer(self.reply(self.recipe_index("shinai")), self.ctx, self.store)
        self.assertIn(b"Missing materials", b"".join(out))
        data = crafting.load_data()
        inv = self.inv()
        for mid in (442, 1477, 1473, 437):
            crafting.give(inv, dict(id=mid, stack=True), 10)
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "metezashi"))
        self.assertFalse(res["ok"])
        self.assertIn("You need Mk Katana 1 Lv 10 to make that.", res["text"])   # English STRDAT 1 name
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "jishinko"))
        self.assertEqual(res["text"], "That cannot be crafted.")          # a Physician recipe

    def test_backpack_full(self):
        os.environ["NOBU_CRAFT_GRANT"] = "0"
        data = crafting.load_data()
        inv = self.inv()
        crafting.give(inv, dict(id=502, stack=True), 3)        # stacks stay after the take,
        crafting.give(inv, dict(id=1246, stack=True), 2)       # so no slot is freed
        n = 0
        while crafting.give(inv, dict(id=1605, stack=False), 1):
            n += 1
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "shinai"))
        self.assertFalse(res["ok"])
        self.assertIn("carry", res["text"])
        self.assertEqual(crafting.held(inv, 502), 3)                      # nothing consumed

    def test_close_and_reopen(self):
        self.talk()
        eid = self.ctx["craft_event"]["id"]
        out = crafting.answer(shops.encode_event_reply(eid, [(8, crafting.CLOSE)]), self.ctx, self.store)
        self.assertEqual(out, [])
        self.assertIsNone(self.ctx["craft_event"])
        self.talk()
        eid = self.ctx["craft_event"]["id"]
        self.assertEqual(crafting.answer(shops.encode_event_reply(eid, None), self.ctx, self.store), [])
        os.environ["NOBU_CRAFT_REOPEN"] = "1"
        self.talk()
        out = crafting.answer(self.reply(0), self.ctx, self.store)
        self.assertEqual(sum(1 for o in out if o[:2] == bytes.fromhex("1102")), 0)
        self.assertEqual(sum(1 for o in out if o[0] == 0x11), 1)
        parse_event(next(o for o in out if o[0] == 0x11))

    def test_refresh_notify_parses(self):
        data = crafting.load_data()
        recs = data["recipes"][:2]
        self.assertEqual(parse_30(crafting.notify_refresh(recs, selected=recs[0]))["materials"]["n"], 2)


class Physician(Base):
    ME = dict(SMITH, looks=[4, 20, 0, 7, 6] + [0] * 15)

    def test_physician_makes_jishinko(self):
        os.environ["NOBU_CRAFT_SEED"] = "3"
        out = self.talk()
        ev = parse_event(next(o for o in out if o[0] == 0x11))
        self.assertEqual([p["id"] for p in ev["craft"]["products"]], [693, 201])
        inv = self.inv()
        before = crafting.held(inv, 201)                         # starter kit has 5 Heal Powder
        data = crafting.load_data()
        r = crafting.by_key(data, "jishinko")
        for mid, n in ((413, 40), (1283, 100), (386, 50)):
            crafting.give(inv, dict(id=mid, stack=True), n)
        wins = 0
        for _ in range(5):
            wins += crafting.make(self.ctx, self.store, data, r)["success"]
        self.assertGreater(wins, 0)
        self.assertEqual(crafting.held(inv, 201), before + wins)
        stack = [it for it in inv["items"] if it["id"] == 201]
        self.assertTrue(all("maker" not in it for it in stack))   # stackables merge, no maker


class Priest(Base):
    ME = dict(SMITH, looks=[4, 20, 0, 3, 6] + [0] * 15)

    def test_priest_arrows_make_ten(self):
        self.talk()
        data = crafting.load_data()
        res = crafting.make(self.ctx, self.store, data, crafting.by_key(data, "ya"))
        self.assertTrue(res["ok"])
        if res["success"]:
            self.assertEqual(crafting.held(self.inv(), 65), 10)


class WorldWiring(unittest.TestCase):
    ENV = ("NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN",
           "NOBU_WORLD_PUSH", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_QUESTS", "NOBU_PLAYERS",
           "NOBU_SHOPS", "NOBU_SHOPS_FILE", "NOBU_TOWN_SPOTS") + KNOBS

    def setUp(self):
        from nobu import social, world
        self.world, self.social = world, social
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in self.ENV}
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        os.environ["NOBU_CRAFT_FILE"] = os.path.join(self.tmp.name, "crafting.json")
        os.environ["NOBU_CRAFT_ALL_JOBS"] = "1"
        os.environ["NOBU_TOWN_SPOTS"] = "0"     # talk from the spawn point: the Craftsman on its offsets
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        social.SESSIONS.clear()
        self.ctx = {"uid": UID}
        body = chars.write_create(dict(family="織田", given="信長", looks=SMITH["looks"]))
        world.answer(bytes([5, 1]) + vce.put_u32(UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(UID) + vce.put_u32(1), self.ctx)
        world.answer(bytes([4]), self.ctx)

    def tearDown(self):
        self.world.STORE = None
        self.social.SESSIONS.clear()
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def test_workshop_in_nagoya_talk_select_make(self):
        w = self.world
        pushes = w.answer(bytes.fromhex("235a0013"), self.ctx)
        recs = [r for p in pushes if p[:2] == bytes.fromhex("0b0a")
                for r in cm.parse_notify_char(p)["records"]]
        self.assertEqual(len([r for r in recs if r["cid"] == 0xFEA00010]), 1)
        self.assertEqual(len([r for r in recs if r["cid"] == 0xFEA00003]), 1)   # merchant too
        tgt = w.answer(bytes.fromhex("25010030fea00010"), self.ctx)
        self.assertEqual(tgt[0][:8], bytes.fromhex("2501003000000000"))
        out = w.answer(bytes.fromhex("28010031"), self.ctx)
        ev = next(o for o in out if o[0] == 0x11)
        self.assertEqual(parse_event(ev)["craft"]["count"], 7)        # all jobs listed
        eid = ev[1]
        sel = w.answer(shops.encode_event_reply(eid, [(8, crafting.SELECT | 0)]), self.ctx)
        self.assertEqual(sel[0][:2], bytes.fromhex("300b"))
        self.assertIn("craft materials n=2", w.describe(sel[0], "S>C"))
        made = w.answer(shops.encode_event_reply(eid, [(8, 0)]), self.ctx)
        self.assertIn(bytes.fromhex("0d06"), [m[:2] for m in made])
        # the shop still gets its own 0a answers
        w.answer(bytes.fromhex("25010032fea00003"), self.ctx)
        w.answer(bytes.fromhex("28010033"), self.ctx)
        sid = self.ctx["shop_event"]["id"]
        rep = w.answer(shops.encode_event_reply(sid, [(6, shops.trade_value(True, 1, 1))]), self.ctx)
        self.assertEqual(rep[0][:2], bytes.fromhex("3006"))


if __name__ == "__main__":
    unittest.main()
