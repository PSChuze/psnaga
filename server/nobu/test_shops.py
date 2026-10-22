"""NPC shops, market and price windows: python -m unittest nobu.test_shops -v"""
import json
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, items, shops, vce

UID = 0x04000471
ME = dict(cid=2, uid=UID, slot=0, family="ああ", given="ああ", looks=[4, 20, 0, 6, 6] + [0] * 15,
          pos=[110, 100000, 3000, 100000, 0])


# ---------------------------------------------------------------------------- client models
def parse_event(payload):
    """0x1a04f8: u8 event id, u8 n (0xff = none), n x {u8 kind, object}; kind 7 = 0x226f30."""
    a = cm.Arc(payload, 1)
    ev = dict(id=a.u8(), n=a.u8(), records=[])
    if ev["n"] == 0xFF:
        return ev
    for _ in range(ev["n"]):
        kind = a.u8()
        if kind != shops.KIND_SHOP:
            raise cm.ParseError(f"kind {kind} not modelled")
        ev["records"].append(parse_shop(a))
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return ev


def parse_shop(a):
    """0x226f30 read half 0x227018 (+ goods 0x1ee760, header 0x1eafc0)."""
    s = dict(name=a.str(0x21), count=a.u8(), mode=a.u8())
    if s["count"] > 50:
        raise cm.ParseError("count overflows the 50-entry object")
    s["prices"] = a.u32s(s["count"])
    s["sells"] = a.u32s(50)
    s["goods"] = []
    for _ in range(s["count"]):
        g = dict(stock=a.u8(), b1=a.u8(), cat=a.u8(), w8=a.u16(), type=a.u16(), flags6=a.u16(),
                 id=a.u32(), slot=a.u8(), flags4=a.u8())
        s["goods"].append(g)
    flags = a.u32()
    a.u16()
    if flags & 1:
        a.u32()
    a.u8()
    return s


def parse_trend(a):                                   # 0x1f2ca0
    a.u8()
    for _ in range(31):
        a.str(0x21), a.u8(), a.u32(), a.u32(), a.u32()


def parse_price(payload):
    """0x283af0: u8 sub, u16 seq, struct by sub."""
    a = cm.Arc(payload, 1)
    sub, _ = a.u8(), a.u16()
    if sub == 0x0A:                                   # 0x1f2930
        a.u8(), a.u8(), a.u32()
        n = a._wrap(a.r.length)
        if n > 30:
            raise cm.ParseError("u16 array > 30")
        for _ in range(n):
            a.u16()
    elif sub == 0x0C:
        parse_trend(a)
    elif sub == 0x0B:                                 # 0x1f2d60
        n = a.u8()
        a.bytes(n)
        parse_trend(a)
        if a.u32() & 1:
            a.u32()
        a.u16(), a.u8()
    return a.done()


def parse_market(payload):
    """0x19aa90 subs 1..9 (res >= 0 path, empty lists). Returns True when the callback is reached."""
    a = cm.Arc(payload, 1)
    sub, _ = a.u8(), a.u16()
    res = a.u32()
    if sub in (2, 3):
        return a.done()
    if res >= 0x80000000:
        return False                                  # res < 0: dropped, no callback
    if sub == 1:
        a.u32()
        na, nb, _ = a.u16(), a.u16(), a.u16()        # 0x1aa240 (empty lists only)
        assert na == nb == 0
        a.u8(), a.str(0x3d), a.str(0x21), a.u16(), a.u8(), a.u32()      # 0x1a8780
        n, _ = a.u16(), a.u16()                       # 0x1a99d0
        assert n == 0
    elif sub == 4:
        a.u32(), a.u16(), a.u8()
    elif sub == 5:
        a.u32(), a.u32(), a.u32(), a.str(0x21)
    elif sub == 6:
        a.u32()
    elif sub in (7, 8):
        for _ in range(5):
            a.u32()
    elif sub == 9:
        x, y = a.u8(), a.u8()
        assert x == y == 0
    return a.done()


# ---------------------------------------------------------------------------- fixtures
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("NOBU_SHOPS_FILE", "NOBU_SHOPS", "NOBU_SHOP_REOPEN",
                                                   "NOBU_SHOP_ITEM_PUSH", "NOBU_SHOP_ANY_ZONE", "NOBU_SHOP_VENDORS")}
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        os.environ["NOBU_SHOP_VENDORS"] = "0"   # the Merchant alone (Vendors below turns the vendors back on)
        for k in ("NOBU_SHOPS", "NOBU_SHOP_REOPEN", "NOBU_SHOP_ITEM_PUSH", "NOBU_SHOP_ANY_ZONE"):
            os.environ.pop(k, None)
        self.store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.store.data["chars"][str(UID)] = [json.loads(json.dumps(ME))]
        self.store.save()
        self.ctx = {"char": self.store.find(UID, 2), "cid": 2, "uid": UID}

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def inv(self):
        return items.inventory(self.ctx["char"], self.store)

    def open(self):
        pushes = shops.spawn_pushes(self.ctx)
        self.assertEqual(len(pushes), 1)
        self.ctx["target"] = 0xFEA00003
        return shops.answer(bytes.fromhex("28010031"), self.ctx, self.store)

    def reply(self, eid, buy, index, qty):
        return shops.encode_event_reply(eid, [(shops.ANSWER_SHOP, shops.trade_value(buy, index, qty))])


# ---------------------------------------------------------------------------- tests
class ShopData(Base):
    def test_seed_file_is_written_and_valid(self):
        data = shops.load_shops()
        self.assertTrue(os.path.exists(os.environ["NOBU_SHOPS_FILE"]))
        shop = data["shops"]["nagoya_general"]
        self.assertEqual(shop["zone"], 110)
        names = items.tsv_names("strdat_00_items.tsv")
        for sh in data["shops"].values():
            for g in sh["items"]:
                self.assertIn(f"0.{g['id']}", names, g)                 # ids are STRDAT rows
                self.assertEqual(items.item_name(g["id"]), g["name"], g)      # name = the English STRDAT name
        self.assertTrue(shops.npc.is_npc_cid(shops.parse_cid(shop["npc"]["cid"])))


class ShopEvent(Base):
    def test_talk_opens_the_shop_event(self):
        out = self.open()
        self.assertEqual(out[0][:2], bytes.fromhex("0d06"))            # greeting line
        ev = out[1]
        self.assertEqual(ev[:4], bytes.fromhex("11010106"))            # id 1, 1 record, tag 6 (type 7)
        p = parse_event(ev)
        s = p["records"][0]
        self.assertEqual(s["name"], "Merchant")
        self.assertEqual(s["count"], 11)
        self.assertEqual(s["prices"][:3], [5, 8, 8])
        self.assertEqual([g["flags6"] for g in s["goods"]], [1] * 11)  # self-describing entries
        self.assertTrue(all(g["stock"] > 0 for g in s["goods"]))       # 0 would hide the row
        inv = self.inv()
        dango = next(it for it in inv["items"] if it["id"] == 279)
        self.assertEqual(s["sells"][dango["idx"]], 2)                  # 4 mon x 0.5
        self.assertEqual(self.ctx["shop_event"], dict(id=1, shop="nagoya_general"))
        self.assertIn("shop 'Merchant' goods=11", shops.describe(ev, "S>C"))

    def test_first_bytes(self):
        shop = dict(name="A", items=[dict(id=420, price=8, type=62)])
        ev = shops.shop_event(5, shop, [0] * 50)
        # 11 05 01 06 | str "A" | count 1 | mode 0 | prices [8] | sells 50 x 0 | entry | header
        self.assertEqual(ev[:14].hex(), "11050106" "0141" "01" "00" "01" "00000008" "32")
        self.assertEqual(ev[14 + 200:14 + 200 + 15].hex(),
                         "63" "00" "00" "0000" "003e" "0001" "000001a4" "ff" "01")
        self.assertEqual(ev[-7:].hex(), "00000000" "0000" "00")
        parse_event(ev)

    def test_other_npc_and_disabled(self):
        shops.spawn_pushes(self.ctx)
        self.ctx["npcs"][0xFEA00001] = dict(cid=0xFEA00001, role="guide")
        self.ctx["target"] = 0xFEA00001
        self.assertIsNone(shops.answer(bytes.fromhex("28010031"), self.ctx, self.store))
        os.environ["NOBU_SHOPS"] = "0"
        self.ctx["target"] = 0xFEA00003
        self.assertIsNone(shops.answer(bytes.fromhex("28010031"), self.ctx, self.store))
        self.assertEqual(shops.spawn_pushes(self.ctx), [])

    def test_spawn_only_in_the_shop_zone(self):
        self.ctx["char"]["pos"] = [10, 1, 2, 3, 0]
        self.assertEqual(shops.spawn_pushes(self.ctx), [])
        os.environ["NOBU_SHOP_ANY_ZONE"] = "1"
        self.assertEqual(len(shops.spawn_pushes(self.ctx)), 1)


class Vendors(Base):
    """Nagoya's 2004 equipment vendors (vendors_2004.json): eight more keepers, gear bought as single pieces."""
    def setUp(self):
        super().setUp()
        os.environ.pop("NOBU_SHOP_VENDORS", None)

    def test_vendors_spawn_and_sell_gear(self):
        pushes = shops.spawn_pushes(self.ctx)
        self.assertEqual(len(pushes), 12)                              # the Merchant + 11 vendors
        sword = shops.parse_cid(shops.load_shops()["shops"]["nagoya_sword"]["npc"]["cid"])
        self.assertIn(sword, self.ctx["npcs"])
        self.ctx["target"] = sword
        out = shops.answer(bytes.fromhex("28010031"), self.ctx, self.store)
        ev = next(o for o in out if o[:2] == bytes.fromhex("1101"))      # after the greeting line
        rec = parse_event(ev)["records"][0]
        self.assertEqual(rec["name"], "Swordsmith")
        self.assertEqual(rec["prices"][0], 65)
        money = self.inv()["money"]
        out = shops.answer(self.reply(1, True, 0, 1), self.ctx, self.store)   # row 0: Rusty Sword, 65 mon in 2004
        inv = self.inv()
        self.assertEqual(inv["money"], money - 65)
        blade = [it for it in inv["items"] if it["id"] == 1616]
        self.assertEqual(len(blade), 1)
        self.assertEqual((blade[0]["cat"], blade[0]["count"], blade[0]["flags"]), (1, 1, 0))
        self.assertGreater(blade[0]["dur"], 0)

    def buy(self, shop_key, row, qty=1):
        shops.spawn_pushes(self.ctx)
        self.ctx["target"] = shops.parse_cid(shops.load_shops()["shops"][shop_key]["npc"]["cid"])
        shops.answer(bytes.fromhex("28010031"), self.ctx, self.store)
        return shops.answer(self.reply(1, True, row, qty), self.ctx, self.store)

    def test_medicine_seller_sells_stacks(self):
        self.inv()["money"] = 5000
        before = sum(it["count"] for it in self.inv()["items"] if it["id"] == 201)
        self.buy("nagoya_medicine", 0, 2)                              # row 0: Heal Powder, 800 mon each
        inv = self.inv()
        self.assertEqual(inv["money"], 5000 - 1600)
        stack = [it for it in inv["items"] if it["id"] == 201]
        self.assertEqual(sum(it["count"] for it in stack), before + 2)
        self.assertEqual(stack[0]["type"], 52)

    def test_jeweller_sells_a_hair_ornament(self):
        self.inv()["money"] = 100
        self.buy("nagoya_jeweller", 0)                                 # row 0: Wood Hairpin, 60 mon (2004)
        pin = [it for it in self.inv()["items"] if it["id"] == 1737]
        self.assertEqual(len(pin), 1)
        self.assertEqual((pin[0]["cat"], pin[0]["type"]), (2, 37))

    def test_ninja_seller_in_a_castle(self):
        from nobu import towns
        self.ctx["char"]["pos"] = [404, 0, 0, 0, 0]                     # Kofu Castle
        pushes = shops.spawn_pushes(self.ctx)
        self.assertEqual(len(pushes), 1)
        key = shops.load_shops()["shops"]["kofu_castle_ninja"]
        cid = shops.parse_cid(key["npc"]["cid"])
        self.assertEqual(tuple(self.ctx["npcs"][cid]["pos"]), towns.spot(404, "shop_ninja"))
        self.inv()["money"] = 1000
        self.ctx["target"] = cid
        shops.answer(bytes.fromhex("28010031"), self.ctx, self.store)
        shops.answer(self.reply(1, True, 0, 1), self.ctx, self.store)   # row 0: Ninja Metezashi, 120 mon
        self.assertEqual([it["cat"] for it in self.inv()["items"] if it["id"] == 1660], [1])

    def test_mino_only_stock_is_left_out(self):
        sword = shops.load_shops()["shops"]["nagoya_sword"]["items"]
        self.assertNotIn(720, [g["id"] for g in sword])                  # Kanenori, sold only in Mino

    def test_switch_off(self):
        os.environ["NOBU_SHOP_VENDORS"] = "0"
        self.assertEqual(len(shops.spawn_pushes(self.ctx)), 1)


class Trades(Base):
    def test_client_reply_encoding(self):
        req = self.reply(1, True, 1, 3)
        self.assertEqual(req.hex(), "0a07" "0101" "06" "40000301")
        d = shops.decode_event_reply(req)
        self.assertEqual(d["answers"][0], dict(kind=6, value=0x40000301, buy=True, qty=3, index=1))
        self.assertEqual(shops.encode_event_reply(1, None).hex(), "0a020 1ff".replace(" ", ""))
        self.assertIn("buy index=1 qty=3", shops.describe(req))

    def test_names_come_from_the_ids_not_the_file(self):
        data = shops.load_shops()                       # a shops.json with other names in it
        for g in data["shops"]["nagoya_general"]["items"]:
            g["name"] = "?"
        with open(os.environ["NOBU_SHOPS_FILE"], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        self.assertEqual(shops.load_shops()["shops"]["nagoya_general"]["items"][1]["name"], "?")
        self.open()
        out = shops.answer(self.reply(1, True, 1, 3), self.ctx, self.store)
        self.assertIn("Bought Lumber x3 for 24 mon.", out[2].decode("utf-8"))

    def test_buy_lumber(self):
        self.open()
        money = self.inv()["money"]
        out = shops.answer(self.reply(1, True, 1, 3), self.ctx, self.store)
        inv = chars.CharStore(self.store.path).find(UID, 2)["inventory"]     # saved to disk
        self.assertEqual(inv["money"], money - 24)
        wood = [it for it in inv["items"] if it["id"] == 420]
        self.assertEqual(len(wood), 1)
        self.assertEqual((wood[0]["count"], wood[0]["type"], wood[0]["flags"]), (3, 62, 1))
        self.assertEqual(out[0], shops.notify_money(money - 24))
        self.assertEqual(out[0].hex(), "30060000" + f"{money - 24:08x}")
        self.assertEqual(out[1][:5], bytes.fromhex("3003000001"))
        self.assertIn("Bought Lumber x3 for 24 mon.", out[2].decode("utf-8", "replace"))
        self.assertEqual(out[3][:2], bytes.fromhex("1102"))            # reopened with id 2
        s = parse_event(out[3])["records"][0]
        self.assertEqual(s["sells"][wood[0]["idx"]], 4)                # 8 x 0.5

    def test_buy_tops_up_a_stack(self):
        self.open()
        before = next(it for it in self.inv()["items"] if it["id"] == 279)["count"]
        out = shops.answer(self.reply(1, True, 7, 2), self.ctx, self.store)   # Dango row 7
        after = [it for it in self.inv()["items"] if it["id"] == 279]
        self.assertEqual([it["count"] for it in after], [before + 2])
        self.assertEqual(len([o for o in out if o[:2] == b"\x30\x03"]), 1)

    def test_not_enough_money(self):
        self.open()
        self.inv()["money"] = 100
        out = shops.answer(self.reply(1, True, 10, 1), self.ctx, self.store)   # Heal Powder 800
        self.assertEqual(self.inv()["money"], 100)
        self.assertEqual(out[0], shops.notify_money(100))
        self.assertIn("Not enough money.", out[1].decode("utf-8", "replace"))

    def test_backpack_full(self):
        self.open()
        inv = self.inv()
        inv["items"] = [dict(id=5000 + i, type=54, count=1, flags=0, idx=i) for i in range(50)]
        out = shops.answer(self.reply(1, True, 1, 1), self.ctx, self.store)
        self.assertEqual(len(inv["items"]), 50)
        self.assertIn("Cannot carry", out[1].decode("utf-8", "replace"))

    def test_sell_dango(self):
        self.open()
        inv = self.inv()
        dango = next(it for it in inv["items"] if it["id"] == 279)
        money, have = inv["money"], dango["count"]
        out = shops.answer(self.reply(1, False, dango["idx"], 2), self.ctx, self.store)
        self.assertEqual(inv["money"], money + 4)
        self.assertEqual(dango["count"], have - 2)
        self.assertEqual(out[1].hex(), f"30040000 01 {dango['idx']:02x} {have - 2:02x}".replace(" ", ""))
        self.assertIn("Sold", out[2].decode("utf-8", "replace"))

    def test_sell_all_removes_the_item(self):
        self.open()
        inv = self.inv()
        dango = next(it for it in inv["items"] if it["id"] == 279)
        shops.answer(self.reply(1, False, dango["idx"], dango["count"]), self.ctx, self.store)
        self.assertFalse(any(it["id"] == 279 for it in inv["items"]))

    def test_equipped_item_is_refused(self):
        self.open()
        inv = self.inv()
        worn = next(it for it in inv["items"] if it.get("pos") is not None)
        money = inv["money"]
        out = shops.answer(self.reply(1, False, worn["idx"], 1), self.ctx, self.store)
        self.assertEqual(inv["money"], money)
        self.assertIn("cannot be sold", out[1].decode("utf-8", "replace"))

    def test_cancel_and_stale_ids(self):
        self.open()
        self.assertEqual(shops.answer(self.reply(9, True, 1, 1), self.ctx, self.store), [])
        self.assertEqual(shops.answer(shops.encode_event_reply(1, None), self.ctx, self.store), [])
        self.assertIsNone(self.ctx["shop_event"])
        self.assertEqual(shops.answer(self.reply(1, True, 1, 1), self.ctx, self.store), [])

    def test_no_choice_answer_is_silent(self):
        # The client answered every shop event with v = 0x400fffff (buy, index -1, qty -1); the
        # refusal + reopen looped "That item is not for sale." every 0.6 s
        self.open()
        money = self.inv()["money"]
        req = shops.encode_event_reply(1, [(shops.ANSWER_SHOP, 0x400FFFFF)])
        self.assertEqual(req.hex(), "0a07" "0101" "06" "400fffff")
        self.assertEqual(shops.answer(req, self.ctx, self.store), [])
        self.assertIsNone(self.ctx["shop_event"])
        self.assertEqual(self.inv()["money"], money)
        self.assertEqual(shops.answer(self.reply(1, False, 0xFF, 0xFFF), self.ctx, self.store), [])

    def test_bad_row_is_refused_without_reopening(self):
        self.open()
        out = shops.answer(self.reply(1, True, 40, 1), self.ctx, self.store)
        self.assertEqual([o[:2].hex() for o in out], ["3006", "0d06"])
        self.assertIn("not for sale", out[1].decode("utf-8", "replace"))
        self.assertIsNone(self.ctx["shop_event"])

    def test_refusals_still_reopen(self):
        self.open()
        self.inv()["money"] = 100
        out = shops.answer(self.reply(1, True, 10, 1), self.ctx, self.store)
        self.assertEqual(out[-1][:2], bytes.fromhex("1102"))

    def test_no_reopen_no_item_push(self):
        os.environ["NOBU_SHOP_REOPEN"] = "0"
        os.environ["NOBU_SHOP_ITEM_PUSH"] = "0"
        self.open()
        out = shops.answer(self.reply(1, True, 1, 1), self.ctx, self.store)
        self.assertEqual([o[:2].hex() for o in out], ["3006", "0d06"])


class MarketAndPrices(Base):
    def test_market_replies_reach_the_callback(self):
        for sub in range(1, 10):
            req = bytes([0x1B, sub]) + (0x40 + sub).to_bytes(2, "big")
            out = shops.answer(req, {}, None)
            self.assertEqual(len(out), 1, sub)
            self.assertEqual(out[0][:4], req[:4])
            self.assertTrue(parse_market(out[0]), sub)
        self.assertEqual(shops.answer(bytes.fromhex("1b0e0001") + vce.put_bytes(b"x"), {}, None), [])
        self.assertEqual(shops.answer(bytes.fromhex("1b0f000100000001"), {}, None), [])
        self.assertIn("MARKET request open/search", shops.describe(bytes.fromhex("1b010007")))

    def test_market_sub1_bytes(self):
        out = shops.answer(bytes.fromhex("1b010007"), {}, None)[0]
        self.assertEqual(out.hex(), "1b010007" + "00" * 8 + "00" * 6 + "000000" + "0000" + "00"
                         + "00000000" + "00" * 4)

    def test_price_windows(self):
        for sub, req in ((0x0A, "230a00200105"), (0x0B, "230b0021"), (0x0C, "230c002201")):
            out = shops.answer(bytes.fromhex(req), {}, None)
            self.assertEqual(out[0][:4], bytes.fromhex(req)[:4])
            self.assertTrue(parse_price(out[0]), sub)
        self.assertEqual(shops.answer(bytes.fromhex("230a00200105"), {}, None)[0].hex(),
                         "230a0020" "0000" "00000000" "00")
        self.assertIn("SCMD_GET_QUOTATION", shops.describe(bytes.fromhex("230a00200105")))

    def test_not_ours(self):
        for p in ("2308000100", "24010001", "25010001fea00003", "0505"):
            self.assertIsNone(shops.answer(bytes.fromhex(p), dict(self.ctx), self.store), p)


class WorldWiring(unittest.TestCase):
    """The world.py hooks: the Merchant spawns in Nagoya and 28 01 on it opens the shop."""
    KNOBS = ("NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN",
             "NOBU_WORLD_PUSH", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_QUESTS", "NOBU_PLAYERS",
             "NOBU_SHOPS", "NOBU_SHOPS_FILE", "NOBU_SHOP_REOPEN", "NOBU_SHOP_ITEM_PUSH",
             "NOBU_SHOP_ANY_ZONE", "NOBU_TOWN_SPOTS", "NOBU_SHOP_VENDORS")

    def setUp(self):
        from nobu import social, world
        self.world, self.social = world, social
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in self.KNOBS}
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        os.environ["NOBU_TOWN_SPOTS"] = "0"     # talk from the spawn point: the Merchant on its offsets
        os.environ["NOBU_SHOP_VENDORS"] = "0"   # the Merchant alone
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        social.SESSIONS.clear()
        self.ctx = {"uid": UID}
        body = chars.write_create(dict(family="織田", given="信長", looks=ME["looks"]))
        world.answer(bytes([5, 1]) + vce.put_u32(UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(UID) + vce.put_u32(1), self.ctx)
        world.answer(bytes([4]), self.ctx)

    def tearDown(self):
        self.world.STORE = None
        self.social.SESSIONS.clear()
        for k in self.KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def test_merchant_in_nagoya_and_talk_opens_the_shop(self):
        w = self.world
        self.assertEqual(self.ctx.get("zone"), 110)                    # Oda starts in Nagoya
        pushes = w.answer(bytes.fromhex("235a0013"), self.ctx)
        recs = [r for p in pushes if p[:2] == bytes.fromhex("0b0a")
                for r in cm.parse_notify_char(p)["records"]]
        merchant = [r for r in recs if r["cid"] == 0xFEA00003]
        self.assertEqual(len(merchant), 1)
        self.assertEqual((merchant[0]["uid"], merchant[0]["area"]), (0, 31))       # Nagoya = place 31 (zones.area_of)
        tgt = w.answer(bytes.fromhex("25010030fea00003"), self.ctx)
        self.assertEqual(tgt[0][:8], bytes.fromhex("2501003000000000"))  # res 0: known NPC
        out = w.answer(bytes.fromhex("28010031"), self.ctx)
        self.assertEqual([o[:2].hex() for o in out], ["0d06", "1101"])
        self.assertEqual(parse_event(out[1])["records"][0]["name"], "Merchant")
        self.assertIn("shop 'Merchant'", w.describe(out[1], "S>C"))
        money = self.ctx["char"]["inventory"]["money"]
        rep = w.answer(shops.encode_event_reply(1, [(6, shops.trade_value(True, 1, 2))]), self.ctx)
        self.assertEqual(rep[0], shops.notify_money(money - 16))
        self.assertIn("buy index=1 qty=2",
                      w.describe(shops.encode_event_reply(1, [(6, shops.trade_value(True, 1, 2))])))

    def test_no_merchant_outside_its_zone_or_when_off(self):
        os.environ["NOBU_SHOPS"] = "0"
        pushes = self.world.answer(bytes.fromhex("235a0013"), self.ctx)
        cids = [r["cid"] for p in pushes if p[:2] == bytes.fromhex("0b0a")
                for r in cm.parse_notify_char(p)["records"]]
        self.assertNotIn(0xFEA00003, cids)


if __name__ == "__main__":
    unittest.main()
