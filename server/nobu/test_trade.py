"""Player trade (cmd 0x27 / 0x33) and wares, two sessions through world.answer:
python -m unittest nobu.test_trade -v

The parsers below are models of how the client reads each reply: 0x28bd70 (cmd 0x27
replies, main 0x28cfa0) and 0x28c5b0 (cmd 0x33 notifies, main 0x28d1e0); records use the
0x1ec7f0 / 0x1ecca0 models of test_items, the partner struct 0x1f0dc0 clientmodel.target_info,
the wares object 0x226f30 test_shops.parse_shop."""
import json
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, items, party, players, shops, social, trade, vce, world
from nobu.test_items import parse_backpack, parse_dispinfo, read_info, read_record
from nobu.test_shops import parse_event, parse_shop

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
KNOBS = ("NOBU_DEBUG_NPC", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN", "NOBU_WORLD_PUSH",
         "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_PLAYERS", "NOBU_SHOPS", "NOBU_GUILD",
         "NOBU_CRAFTING", "NOBU_TRADE", "NOBU_TRADE_RANGE", "NOBU_DEBUG_ENEMY", "NOBU_QUESTS",
         "NOBU_ENEMIES")
POWDER, PILL, DANGO = 201, 202, 279


# ---------------------------------------------------------------------------- client models
def _i32(v):
    return v - (1 << 32) if v & 0x80000000 else v


def _records(a):
    """u8 n, n x record (0x1ec7f0), then n x u16 (0x1ecca0) only while bytes remain."""
    n = a.u8()
    recs = [read_record(a) for _ in range(n)]
    if not a.done():
        for r in recs:
            r[0x40] = a.u16()
    return recs


def parse_trade_reply(payload):
    """0x28bd70: u8 sub (< 0xb), u16 seq, then by sub (table 0x428c20)."""
    a = cm.Arc(payload, 1)
    if payload[0] != 0x27:
        raise cm.ParseError("not cmd 0x27")
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 1:
        d["partner"] = cm.target_info(a)
    elif sub in (2, 3):
        d["a"] = a.u8()
        d["records"] = _records(a)
    elif sub == 4:
        d["kan"], d["mon"] = a.u32(), a.u32()
    elif sub in (6, 7, 8, 9):
        d["res"] = _i32(a.u32())
    elif sub == 0xA:
        d["x"] = a.u8()
        d["info"] = read_info(a)
    else:
        raise cm.ParseError(f"sub {sub}: no parser (0x28c568)")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return d


def parse_trade_notify(payload):
    """0x28c5b0: u8 sub (< 0xb), u16 seq, then by sub (table 0x428c50); 5..8 read nothing."""
    a = cm.Arc(payload, 1)
    if payload[0] != 0x33:
        raise cm.ParseError("not cmd 0x33")
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 1:
        d["partner"] = cm.target_info(a)
    elif sub in (2, 3):
        d["a"] = a.u8()
        d["records"] = _records(a)
    elif sub == 4:
        d["kan"], d["mon"] = a.u32(), a.u32()
    elif sub in (9, 0xA):
        d["kan"], d["mon"] = a.u32(), a.u32()
        d["records"] = _records(a)
    elif sub not in (5, 6, 7, 8):
        raise cm.ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return d


def run(payload, ctx):
    return world.answer(payload, ctx)


def texts(frames):
    return [vce.Reader(f, 2).bytes().decode("utf-8") for f in frames if f[:2] == b"\x0d\x06"]


# ---------------------------------------------------------------------------- fixtures
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "chars.json")
        world.STORE = chars.CharStore(self.path)
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        for k in ("NOBU_SHOPS", "NOBU_GUILD", "NOBU_CRAFTING", "NOBU_ENEMIES"):
            os.environ[k] = "0"
        social.SESSIONS.clear()
        party.reset()
        self.a = self.session("a", 0x4000471, "織田", "信長")
        self.b = self.session("b", 0x4000472, "明智", "光秀")
        for c in (self.a, self.b):
            run(b"\x04", c)
            run(bytes.fromhex("235a0013"), c)
            run(bytes.fromhex("24010001"), c)          # creates the starter inventory
            c["got"].clear()

    def tearDown(self):
        social.SESSIONS.clear()
        party.reset()
        world.STORE = None
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def session(self, sid, uid, family, given):
        got = []
        ctx = {"uid": uid, "push": got.append, "got": got}
        body = chars.write_create(dict(family=family, given=given, looks=LOOKS))
        run(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body, ctx)
        cid = world.store().list(uid)[0]["cid"]
        run(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(cid), ctx)
        social.register(sid, ctx)
        return ctx

    @staticmethod
    def wire(of, viewer):
        return players.wire_cid(of["char"]["cid"], viewer["char"]["cid"])

    def target(self, ctx, other):
        return run(bytes.fromhex("25010005") + vce.put_u32(self.wire(other, ctx)), ctx)

    def open_trade(self):
        self.target(self.a, self.b)
        rep = run(bytes.fromhex("27010010"), self.a)
        self.b["got"].clear()
        return rep

    @staticmethod
    def inv(ctx):
        return ctx["char"]["inventory"]

    def idx_of(self, ctx, iid):
        return next(it["idx"] for it in self.inv(ctx)["items"] if it["id"] == iid)

    def count(self, ctx, iid):
        return sum(int(it.get("count", 1)) for it in self.inv(ctx)["items"] if it["id"] == iid)

    def disk(self):
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {c["cid"]: c for cs in data["chars"].values() for c in cs}

    def add(self, ctx, iid, slot, qty, seq=0x20):
        idx = self.idx_of(ctx, iid)
        return run(bytes([0x27, 2]) + vce.put_u16(seq) + bytes([idx, slot, qty]), ctx)

    def flag(self, ctx, sub, seq=0x30):
        return run(bytes([0x27, sub]) + vce.put_u16(seq), ctx)

    def money(self, ctx, mon, seq=0x40):
        return run(bytes([0x27, 4]) + vce.put_u16(seq) + vce.put_u32(mon // 1000)
                   + vce.put_u32(mon % 1000), ctx)


# ---------------------------------------------------------------------------- tests
class Start(Base):
    def test_request_opens_both_windows(self):
        rep = self.open_trade()
        d = parse_trade_reply(rep[0])
        self.assertEqual((d["sub"], d["seq"]), (1, 0x10))
        self.assertEqual(d["partner"]["cid"], self.wire(self.b, self.a))
        self.assertEqual(d["partner"]["family"], "明智")
        self.assertTrue(trade.trade_of(self.a) is trade.trade_of(self.b))

    def test_partner_gets_trade_start(self):
        self.target(self.a, self.b)
        self.b["got"].clear()
        run(bytes.fromhex("27010010"), self.a)
        n = parse_trade_notify(self.b["got"][0])
        self.assertEqual(n["sub"], 1)
        self.assertEqual(n["partner"]["cid"], self.wire(self.a, self.b))
        self.assertEqual(n["partner"]["given"], "信長")

    def test_no_target_falls_back_to_nearest_player(self):
        rep = run(bytes.fromhex("27010010"), self.a)     # no 25 01: both stand at the spawn point
        self.assertEqual(parse_trade_reply(rep[0])["partner"]["cid"], self.wire(self.b, self.a))

    def test_npc_target_is_refused(self):
        self.a["target"] = 0xFEA00003
        rep = run(bytes.fromhex("27010010"), self.a)
        self.assertEqual(texts(rep), ["Target is not a PC."])
        self.assertIsNone(trade.trade_of(self.a))

    def test_busy_partner_is_refused(self):
        self.open_trade()
        c = self.session("c", 0x4000473, "武田", "信玄")
        run(b"\x04", c)
        run(bytes.fromhex("235a0013"), c)
        c["target"] = self.wire(self.a, c)
        rep = run(bytes.fromhex("27010011"), c)
        self.assertEqual(texts(rep), ["Target is already trading."])

    def test_ignored_is_refused(self):
        self.b["char"]["ignores"] = [dict(uid=0, cid=self.a["char"]["cid"])]
        self.target(self.a, self.b)
        self.assertEqual(texts(run(bytes.fromhex("27010010"), self.a)),
                         ["Target is ignoring you."])


class Offers(Base):
    def test_add_item(self):
        self.open_trade()
        idx = self.idx_of(self.a, DANGO)
        rep = self.add(self.a, DANGO, 0, 2)
        d = parse_trade_reply(rep[0])
        self.assertEqual((d["sub"], d["a"]), (2, idx))       # a = backpack index taken from
        rec = d["records"][0]
        self.assertEqual((rec[0x00], rec[0x0C], rec[0x40]), (DANGO, 0, 2))  # id, offer slot, qty
        n = parse_trade_notify(self.b["got"][0])
        self.assertEqual((n["sub"], n["records"][0][0x0C], n["records"][0][0x40]), (2, 0, 2))
        self.assertEqual(self.count(self.a, DANGO), 3)       # nothing moves before the exchange

    def test_add_more_than_owned_or_equipped(self):
        self.open_trade()
        rep = self.add(self.a, DANGO, 0, 4)
        self.assertEqual(parse_trade_reply(rep[0])["records"], [])
        self.assertEqual(texts(rep), ["Invalid offer."])
        self.add(self.a, DANGO, 0, 2)
        rep = self.add(self.a, DANGO, 1, 2)                   # only 1 left un-offered
        self.assertEqual(parse_trade_reply(rep[0])["records"], [])
        weapon = next(it for it in self.inv(self.a)["items"] if it.get("pos") == 0)
        rep = run(bytes([0x27, 2, 0, 0x21, weapon["idx"], 1, 1]), self.a)
        self.assertEqual(texts(rep), ["Tried to offer equipment."])

    def test_taken_slot_moves_to_a_free_one(self):
        self.open_trade()
        self.add(self.a, DANGO, 0, 1)
        d = parse_trade_reply(self.add(self.a, POWDER, 0, 1)[0])
        self.assertEqual(d["records"][0][0x0C], 1)

    def test_delete_item(self):
        self.open_trade()
        self.add(self.a, DANGO, 3, 2)
        self.b["got"].clear()
        rep = run(bytes([0x27, 3, 0, 0x22, 3, 2]), self.a)
        d = parse_trade_reply(rep[0])
        self.assertEqual((d["sub"], d["a"]), (3, 3))          # a = offer slot
        self.assertEqual((d["records"][0][0x0C], d["records"][0][0x40]),
                         (self.idx_of(self.a, DANGO), 2))      # back to its backpack index
        n = parse_trade_notify(self.b["got"][0])
        self.assertEqual((n["sub"], n["a"], n["records"][0][0x40]), (3, 3, 2))
        self.assertEqual(trade.trade_of(self.a)["sides"][0]["offer"], {})

    def test_money(self):
        self.open_trade()
        d = parse_trade_reply(self.money(self.a, 1234)[0])
        self.assertEqual((d["sub"], d["kan"], d["mon"]), (4, 1, 234))
        self.assertEqual(parse_trade_notify(self.b["got"][0])["kan"], 1)
        rep = self.money(self.a, 999999)
        self.assertEqual((parse_trade_reply(rep[0])["kan"], texts(rep)), (1, ["Not enough money."]))

    def test_changes_refused_while_locked(self):
        self.open_trade()
        self.assertEqual(parse_trade_reply(self.flag(self.a, 6)[0])["res"], 0)
        self.assertEqual(parse_trade_notify(self.b["got"][0])["sub"], 5)
        rep = self.add(self.a, DANGO, 0, 1)
        self.assertEqual(texts(rep), ["Your terms are locked in."])
        self.assertEqual(parse_trade_reply(self.flag(self.a, 7)[0])["res"], 0)
        self.assertEqual(parse_trade_reply(self.add(self.a, DANGO, 0, 1)[0])["records"][0][0x40], 1)

    def test_look_item(self):
        self.open_trade()
        self.add(self.a, POWDER, 2, 1)
        d = parse_trade_reply(run(bytes([0x27, 0xA, 0, 0x50, 1, 2]), self.b)[0])
        self.assertEqual((d["sub"], d["info"][0]), (0xA, 0))
        d = parse_trade_reply(run(bytes([0x27, 0xA, 0, 0x51, 0, 7]), self.b)[0])
        self.assertEqual(d["info"][0], 0xFF)


class Exchange(Base):
    def setup_offers(self):
        self.open_trade()
        self.add(self.a, DANGO, 0, 2)
        self.money(self.a, 100)
        self.add(self.b, POWDER, 0, 1)
        self.money(self.b, 500)
        for c in (self.a, self.b):
            c["got"].clear()

    def totals(self):
        out = {}
        for c in (self.a, self.b):
            for it in self.inv(c)["items"]:
                out[it["id"]] = out.get(it["id"], 0) + int(it.get("count", 1))
        return out, sum(self.inv(c)["money"] for c in (self.a, self.b))

    def test_ok_needs_partner_lock(self):
        self.setup_offers()
        self.assertEqual(parse_trade_reply(self.flag(self.a, 8)[0])["res"],
                         trade.ERR_PARTNER_UNLOCKED)

    def test_full_exchange_is_atomic_and_saved(self):
        self.setup_offers()
        before_items, before_money = self.totals()
        a_money, b_money = self.inv(self.a)["money"], self.inv(self.b)["money"]
        self.flag(self.a, 6)
        self.flag(self.b, 6)
        self.assertEqual(parse_trade_reply(self.flag(self.a, 8)[0])["res"], 0)
        self.assertEqual(parse_trade_notify(self.b["got"][-1])["sub"], 7)   # partner OK
        self.b["got"].clear()
        rep = self.flag(self.b, 8)
        self.assertEqual(parse_trade_reply(rep[0])["res"], 0)             # reply before the result
        done = parse_trade_notify(rep[1])
        self.assertEqual(done["sub"], 0xA)
        self.assertEqual(done["kan"] * 1000 + done["mon"], b_money - 500 + 100)
        self.assertIn(DANGO, [r[0x00] for r in done["records"]])
        other = parse_trade_notify(self.a["got"][-2])
        self.assertEqual(other["kan"] * 1000 + other["mon"], a_money - 100 + 500)
        self.assertEqual(texts(self.a["got"]), ["Trade complete."])
        self.assertEqual(self.count(self.a, DANGO), 1)
        self.assertEqual(self.count(self.b, DANGO), 5)                     # stacked onto its 3
        self.assertEqual(self.count(self.a, POWDER), 6)
        self.assertEqual(self.count(self.b, POWDER), 4)
        self.assertEqual(self.totals(), (before_items, before_money))      # nothing made or lost
        disk = self.disk()
        self.assertEqual(disk[self.a["char"]["cid"]]["inventory"]["money"], a_money + 400)
        self.assertEqual(disk[self.b["char"]["cid"]]["inventory"]["money"], b_money - 400)
        self.assertIsNone(trade.trade_of(self.a))
        # a late OK cannot run the exchange twice
        self.assertEqual(parse_trade_reply(self.flag(self.a, 8)[0])["res"], trade.ERR_BAD_OFFER)
        self.assertEqual(self.totals(), (before_items, before_money))

    def test_unlock_after_ok_is_refused(self):
        self.setup_offers()
        self.flag(self.a, 6)
        self.flag(self.b, 6)
        self.flag(self.a, 8)
        self.assertEqual(parse_trade_reply(self.flag(self.a, 7)[0])["res"], trade.ERR_LOCKED)
        self.assertEqual(parse_trade_reply(self.flag(self.b, 7)[0])["res"],
                         trade.ERR_PARTNER_READY)
        self.assertEqual(parse_trade_reply(self.flag(self.a, 9)[0])["res"], 0)   # cancel the OK
        self.assertEqual(parse_trade_notify(self.b["got"][-1])["sub"], 8)
        self.assertEqual(parse_trade_reply(self.flag(self.a, 7)[0])["res"], 0)

    def test_full_backpack_cancels_without_moving_anything(self):
        inv = self.inv(self.b)
        for i in range(items.CAPACITY - len(inv["items"])):
            inv["items"].append(dict(id=1700 + i, type=19, cat=4, count=1, flags=0,
                                     idx=len(inv["items"])))
        self.inv(self.a)["items"].append(dict(id=1697, type=49, cat=4, count=1, flags=0,
                                              idx=len(self.inv(self.a)["items"])))
        self.open_trade()
        self.add(self.a, 1697, 0, 1)
        snap = json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True)
        self.flag(self.a, 6)
        self.flag(self.b, 6)
        self.flag(self.a, 8)
        self.a["got"].clear()
        rep = self.flag(self.b, 8)
        self.assertEqual(parse_trade_notify(rep[1])["sub"], 9)             # closed, no exchange
        self.assertEqual(texts(rep), [trade.TEXTS[trade.ERR_MINE_FULL]])
        self.assertEqual(texts(self.a["got"]), [trade.TEXTS[trade.ERR_THEIRS_FULL]])
        self.assertEqual(parse_trade_notify(self.a["got"][0])["sub"], 7)   # b's OK first
        restore = parse_trade_notify(self.a["got"][1])
        self.assertEqual([r[0x00] for r in restore["records"]], [1697])    # back in a's view
        self.assertEqual(json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True), snap)
        self.assertIsNone(trade.trade_of(self.b))

    def test_quit_returns_offers(self):
        self.setup_offers()
        snap = json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True)
        rep = run(bytes.fromhex("27050060"), self.a)
        mine = parse_trade_notify(rep[0])
        self.assertEqual(mine["sub"], 9)
        self.assertEqual([(r[0x00], r[0x40]) for r in mine["records"]], [(DANGO, 3)])
        theirs = parse_trade_notify(self.b["got"][0])
        self.assertEqual([(r[0x00], r[0x40]) for r in theirs["records"]], [(POWDER, 5)])
        self.assertEqual(texts(self.b["got"]), ["織田 信長 declined the trade."])
        self.assertEqual(json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True), snap)
        self.assertIsNone(trade.trade_of(self.a))
        self.assertIsNone(trade.trade_of(self.b))

    def test_disconnect_mid_trade(self):
        self.setup_offers()
        self.flag(self.a, 6)
        self.flag(self.b, 6)
        self.flag(self.a, 8)
        snap = json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True)
        self.b["got"].clear()
        self.a["got"].clear()
        self.assertTrue(trade.on_leave(self.a))           # world.session's finally
        social.unregister("a")
        self.assertEqual(self.a["got"], [])               # nothing written to the dead socket
        self.assertEqual(parse_trade_notify(self.b["got"][0])["sub"], 9)
        self.assertEqual(json.dumps([self.inv(self.a), self.inv(self.b)], sort_keys=True), snap)
        self.assertEqual(parse_trade_reply(self.flag(self.b, 8)[0])["res"], trade.ERR_BAD_OFFER)

    def test_zone_change_cancels(self):
        self.setup_offers()
        run(b"\x04", self.b)
        self.assertIsNone(trade.trade_of(self.a))
        closed = [parse_trade_notify(f) for f in self.a["got"] if f[0] == 0x33]   # after 0B 04
        self.assertEqual([n["sub"] for n in closed], [9])

    def test_plan_is_pure(self):
        inv_a = items.starter_kit(1)
        inv_b = items.starter_kit(2)
        snap = json.dumps([inv_a, inv_b], sort_keys=True)
        trade.plan(inv_a, inv_b, {4: 1}, {2: 5}, 10, 20)
        self.assertEqual(json.dumps([inv_a, inv_b], sort_keys=True), snap)
        with self.assertRaises(trade.TradeError):
            trade.plan(inv_a, inv_b, {4: 9}, {}, 0, 0)


class Wares(Base):
    def price(self, ctx, iid, price, seq=0x70):
        idx = self.idx_of(ctx, iid)
        return run(bytes([0x24, 0x17]) + vce.put_u16(seq) + vce.put_u8(idx) + vce.put_u32(price), ctx)

    def test_set_price_reply_and_backpack_field(self):
        rep = self.price(self.a, DANGO, 30)
        a = cm.Arc(rep[0], 1)
        self.assertEqual((a.u8(), a.u16(), a.u32(), a.u8(), a.u8()),
                         (0x17, 0x70, 0, 1, self.idx_of(self.a, DANGO)))
        self.assertTrue(a.done())
        bp = parse_backpack(run(bytes.fromhex("24010002"), self.a)[0])
        rec = next(r for r in bp["records"] if r[0x00] == DANGO)
        self.assertEqual(rec[0x3C], 30)                 # record +0x3c -> obj +0xb4 (sale price)
        self.assertEqual(self.disk()[self.a["char"]["cid"]]["inventory"]["items"][
            self.idx_of(self.a, DANGO)]["price"], 30)
        self.price(self.a, DANGO, 0)
        dango = next(it for it in self.inv(self.a)["items"] if it["id"] == DANGO)
        self.assertNotIn("price", dango)
        self.assertNotIn("w3c", dango)

    def test_look_at_wares(self):
        self.price(self.a, DANGO, 30)
        self.price(self.a, POWDER, 700)
        rep = run(bytes.fromhex("25070071") + vce.put_u32(self.wire(self.a, self.b)), self.b)
        a = cm.Arc(rep[0], 1)
        self.assertEqual((a.u8(), a.u16()), (7, 0x71))
        shop = parse_shop(a)
        self.assertTrue(a.done())
        self.assertEqual(shop["name"], "織田 信長")
        self.assertEqual(sorted(zip([g["id"] for g in shop["goods"]], shop["prices"])),
                         [(POWDER, 700), (DANGO, 30)])
        self.assertEqual({g["id"]: g["stock"] for g in shop["goods"]}, {DANGO: 3, POWDER: 5})
        d = parse_dispinfo(run(bytes.fromhex("25080072") + vce.put_u32(self.wire(self.a, self.b))
                               + b"\x00", self.b)[0])
        self.assertIn(d["record"][0x00], (DANGO, POWDER))
        empty = run(bytes.fromhex("25070073") + vce.put_u32(0xFE000999), self.b)
        self.assertEqual(parse_shop(cm.Arc(empty[0], 4))["count"], 0)

    def test_buy_from_a_selling_player(self):
        self.price(self.a, DANGO, 30)
        a_money, b_money = self.inv(self.a)["money"], self.inv(self.b)["money"]
        self.target(self.b, self.a)
        ev = run(bytes.fromhex("28010080"), self.b)
        e = parse_event(ev[0])
        self.assertEqual(e["records"][0]["name"], "織田 信長")
        self.assertEqual(e["records"][0]["prices"], [30])
        self.a["got"].clear()
        rep = run(shops.encode_event_reply(e["id"], [(6, shops.trade_value(True, 0, 2))]), self.b)
        self.assertEqual(rep[0], shops.notify_money(b_money - 60))
        self.assertIn("Bought", texts(rep)[0])
        self.assertEqual(self.count(self.b, DANGO), 5)
        self.assertEqual(self.count(self.a, DANGO), 1)
        self.assertEqual(self.inv(self.a)["money"], a_money + 60)
        self.assertEqual(self.a["got"][0], shops.notify_money(a_money + 60))
        self.assertIn("bought", texts(self.a["got"])[0])
        disk = self.disk()
        self.assertEqual(disk[self.b["char"]["cid"]]["inventory"]["money"], b_money - 60)
        # the fresh event lists the 1 left; buying 2 more is refused, nothing moves
        e2 = parse_event(rep[-1])
        rep = run(shops.encode_event_reply(e2["id"], [(6, shops.trade_value(True, 0, 2))]), self.b)
        self.assertEqual(texts(rep), ["Not that many for sale."])
        self.assertEqual(self.count(self.a, DANGO), 1)

    def test_buy_needs_money_and_an_online_seller(self):
        self.price(self.a, POWDER, 999999)
        self.target(self.b, self.a)
        e = parse_event(run(bytes.fromhex("28010080"), self.b)[0])
        rep = run(shops.encode_event_reply(e["id"], [(6, shops.trade_value(True, 0, 1))]), self.b)
        self.assertEqual(texts(rep), ["Not enough money."])
        e = parse_event(rep[-1])
        social.unregister("a")
        rep = run(shops.encode_event_reply(e["id"], [(6, shops.trade_value(True, 0, 1))]), self.b)
        self.assertEqual(texts(rep), ["The seller is no longer here."])
        self.assertEqual(self.count(self.a, POWDER), 5)

    def test_offered_items_are_reserved(self):
        self.price(self.a, DANGO, 30)
        self.open_trade()
        self.add(self.a, DANGO, 0, 3)
        self.b["target"] = self.wire(self.a, self.b)
        ev = run(bytes.fromhex("28010080"), self.b)
        e = parse_event(ev[0])
        rep = run(shops.encode_event_reply(e["id"], [(6, shops.trade_value(True, 0, 1))]), self.b)
        self.assertEqual(texts(rep), ["Not that many for sale."])

    def test_player_without_wares_is_normal_talk(self):
        self.target(self.b, self.a)
        self.assertIsNone(trade.wares_talk(self.b, world.store))


class Wiring(unittest.TestCase):
    def test_describe(self):
        self.assertEqual(world.describe(bytes.fromhex("270200200501" "02")),
                         "TRADE ADD_ITEM seq=32 bp_idx=5 slot=1 qty=2")
        self.assertTrue(world.describe(trade.notify(0xA, trade.result_body(1500, [])), "S>C")
                        .startswith("NOTIFY_TRADE DONE"))
        self.assertTrue(world.describe(bytes.fromhex("24170001" "03" "0000001e"))
                        .startswith("WARES set price"))

    def test_disabled(self):
        os.environ["NOBU_TRADE"] = "0"
        try:
            self.assertIsNone(trade.answer(bytes.fromhex("27010010"), {"char": {}}))
        finally:
            os.environ.pop("NOBU_TRADE")


if __name__ == "__main__":
    unittest.main()
