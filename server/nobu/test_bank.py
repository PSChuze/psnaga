"""Money changer / bank and item storage: python -m unittest nobu.test_bank -v

The client models below are models of how the client reads each reply, not from bank.py:
0x1a0958 / 0x227160 (event record, wire kind 7), 0x22ae50 / 0x22ae70 / 0x22ae90 / 0x22aed0 / 0x22af40
(answer values), 0x287d48 (24 0f storage list), 0x1ee3d0 (30 05 move), 0x288bb8 (30 07 transfer),
0x2099f0 (money transfer), 0x208b60 + 0x2006c0 + 0x200840 (move by array position)."""
import copy
import json
import os
import random
import tempfile
import unittest

from nobu import bank, battle, chars, items, shops, vce, world
from nobu.clientmodel import Arc, ParseError
from nobu.test_items import read_info, read_record

UID = 0x04000471
ME = dict(cid=2, uid=UID, slot=0, family="ああ", given="いい", looks=[4, 20, 0, 6, 6] + [0] * 15,
          pos=[110, 100000, 3000, 100000, 0], level=10)
CHANGER, KEEPER = 0xFEA00011, 0xFEA00012
KNOBS = ("NOBU_BANK", "NOBU_REPAIR", "NOBU_DOCTOR", "NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER", "NOBU_BANK_CID", "NOBU_BANK_KEEPER_CID", "NOBU_BANK_KEEPER", "NOBU_BANK_ANY_ZONE",
         "NOBU_BANK_RESYNC", "NOBU_BANK_REOPEN", "NOBU_BANK_STACK_ONLY", "NOBU_BANK_CHAT", "NOBU_START_MONEY", "NOBU_DEATH_MIN_LEVEL",
         "NOBU_DEATH_MONEY_PCT", "NOBU_SHOPS", "NOBU_CRAFTING", "NOBU_GUILD", "NOBU_QUESTS", "NOBU_DATA")


# ---------------------------------------------------------------------------- client models
def parse_event(payload):
    """0x1a04f8 with wire kind 7 (0x1a0958): object 0x1eaf50, archive 0x227160 = header 0x1eafc0
    (u32 flags, u16, u32 if flags & 1, u8) then u8 variant only while bytes remain."""
    a = Arc(payload, 1)
    ev = dict(id=a.u8(), n=a.u8(), kind=a.u8())
    if ev["kind"] != 7:
        raise ParseError("wire kind 7 is the bank; 6 is the shop")
    flags = a.u32()
    a.u16()
    if flags & 1:
        a.u32()
    a.u8()
    ev["variant"] = 0 if a.done() else a.u8()
    if not a.done():
        raise ParseError("trailing bytes")
    return ev


def client_money_value(deposit, amount):
    """0x61a4f0: kan = amount / 1000, mon = amount % 1000 -> 0x22ae90 / 0x22aed0:
    0x80000000 | (0x40000000 deposit) | (mon % 1000 & 0x3ff) << 20 | (kan << 44 >> 44)."""
    kan, mon = amount // 1000, amount % 1000
    v = 0x80000000 | ((mon % 1000) & 0x3FF) << 20 | (kan & 0xFFFFF)
    return v | 0x40000000 if deposit else v


def client_item_value(take, index, qty):
    """0x22ae70 (take, list row) / 0x22ae50 (store, backpack +0x1e)."""
    v = (qty & 0xFFF) << 8 | (index & 0xFF)
    return v | 0x40000000 if take else v


def client_answer(event_id, v):
    """0x198ac0: 0a <bytes: id n 07 u32> (0x198e70 / 0x198f80 / 0x199090 all write tag 7)."""
    blob = bytes([event_id, 1, 7]) + vce.put_u32(v)
    return bytes([0x0A]) + vce.put_bytes(blob)


def parse_list(payload):
    """0x287d48: u32 kan, u32 mon, i8 count, count x 0x1ec7f0, [u16 A -> +0x16, u16 B -> +0x18,
    count x u16 (0x1ecca0)]."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    if sub != 0x0F:
        raise ParseError("not 24 0f")
    kan, mon = a.u32(), a.u32()
    n = a.u8()
    n = n - 0x100 if n >= 0x80 else n
    recs = [read_record(a) for _ in range(max(n, 0))]
    A, B = 50, 99
    if not a.done():
        A, B = a.u16(), a.u16()
        for r in recs:
            r[0x40] = a.u16()
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(seq=seq, banked=kan * 1000 + mon, records=recs, A=A, B=B)


def parse_30(payload):
    """0x288570: 30 05 -> 0x1ee3d0, 30 07 -> u8, u32 (0x288bb8), 30 06 -> u32, 30 03 records."""
    a = Arc(payload, 1)
    sub, _ = a.u8(), a.u16()
    if sub == 5:
        d = dict(dir=a.u8(), n=a.u8())
        d["src"] = [a.u8(), a.u8()]
        d["dests"] = [[a.u8(), a.u8()] for _ in range(d["n"])]
        if not a.done():
            d["src"][1] = a.u16()
            for e in d["dests"]:
                e[1] = a.u16()
        d["price"] = a.u32() if not a.done() else 0xFFFFFFFF
    elif sub == 7:
        d = dict(dir=a.u8(), amount=a.u32())
    elif sub == 6:
        d = dict(money=a.u32())
    elif sub == 3:
        d = dict(records=[read_record(a) for _ in range(a.u8())])
    else:
        raise ParseError(f"30 {sub:02x} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    d["sub"] = sub
    return d


class ClientBank:
    """The client's item manager S, reduced to what the bank touches: carried / banked money,
    backpack and storage as 50-slot arrays of {id, idx (+0x1e), count (+0x20), stack}."""

    def __init__(self):
        self.carried = self.banked = 0
        self.storage = [None] * 50

    def load_list(self, d):                       # 0x208990: clear 50 slots, reload, money
        self.storage = [None] * 50
        for k, r in enumerate(d["records"]):
            self.storage[k] = dict(id=r[0], idx=r[0x0C], count=r[0x40], stack=r[0x0D] & 1)
        self.banked = d["banked"]

    def transfer(self, d):                        # 0x2099f0
        if d["dir"] == 1:
            self.banked, self.carried = self.banked - d["amount"], self.carried + d["amount"]
        elif d["dir"] == 0:
            self.banked, self.carried = self.banked + d["amount"], self.carried - d["amount"]

    @staticmethod
    def remove(arr, idx, qty):                    # 0x2006c0: slot whose +0x1e == idx loses qty
        for k, it in enumerate(arr):
            if it and it["idx"] == idx:
                q = min(qty, it["count"])
                it["count"] -= q
                if it["count"] <= 0:
                    arr[k] = None
                return dict(it, count=q)
        return None

    @staticmethod
    def place(arr, slot, rec, n, cap=99):         # 0x200840: by array POSITION
        cur = arr[slot]
        if cur is None:
            arr[slot] = dict(rec, idx=slot, count=n)
            return True
        if cur["stack"] and cur["id"] == rec["id"] and cur["count"] + n <= cap:
            cur["count"] += n
            return True
        return False

    def move(self, d, backpack):                  # 0x208b60 (dir 0: backpack -> storage)
        src, dst = (backpack, self.storage) if d["dir"] == 0 else (self.storage, backpack)
        rec = self.remove(src, d["src"][0], d["src"][1])
        assert rec is not None, "source slot not found"
        return all(self.place(dst, slot, rec, n) for slot, n in d["dests"])


# ---------------------------------------------------------------------------- fixtures
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.pop(k, None) for k in KNOBS}
        os.environ["NOBU_DATA"] = self.tmp.name
        self.path = os.path.join(self.tmp.name, "chars.json")
        self.store = chars.CharStore(self.path)
        self.store.data["chars"][str(UID)] = [json.loads(json.dumps(ME))]
        self.store.save()
        self.ctx = {"char": self.store.find(UID, 2), "cid": 2, "uid": UID}
        self.inv = items.inventory(self.ctx["char"], self.store)

    def tearDown(self):
        for k in KNOBS:
            os.environ.pop(k, None)
        for k, v in self.old.items():
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def ans(self, payload):
        return bank.answer(payload, self.ctx, self.store)

    def open(self):
        bank.spawn_pushes(self.ctx)
        self.ctx["target"] = CHANGER
        out = self.ans(bytes.fromhex("28010001") + vce.put_u32(CHANGER))
        ev = next(parse_event(p) for p in out if p[0] == 0x11)
        return ev, out

    def item(self, iid):
        return next((it for it in self.inv["items"] if it["id"] == iid), None)

    def stored(self, iid):
        return next((it for it in bank.bank_of(self.ctx["char"])["items"] if it["id"] == iid), None)

    def reload(self):
        return chars.CharStore(self.path).find(UID, 2)


# ---------------------------------------------------------------------------- tests
class Wire(unittest.TestCase):
    def test_event_record_is_wire_kind_7_and_the_shop_is_6(self):
        ev = parse_event(bank.bank_event(9))
        self.assertEqual((ev["id"], ev["n"], ev["kind"], ev["variant"]), (9, 1, 7, 0))
        self.assertEqual(bank.bank_event(9).hex(), "110901070000000000000000")
        self.assertEqual(shops.KIND_SHOP, 6)
        self.assertNotEqual(shops.KIND_SHOP, bank.KIND_BANK)

    def test_answer_values_match_the_client_encoders(self):
        for amount in (0, 1, 999, 1000, 1500, 123456789, bank.MONEY_CAP):
            for dep in (False, True):
                v = client_money_value(dep, amount)
                self.assertEqual(bank.money_value(dep, amount), v)
                self.assertEqual(bank.decode_value(v),
                                 dict(op="deposit" if dep else "withdraw", amount=amount))
        for take in (False, True):
            v = client_item_value(take, 7, 99)
            self.assertEqual(bank.item_value(take, 7, 99), v)
            self.assertEqual(bank.decode_value(v), dict(op="take" if take else "store", index=7, qty=99))
        self.assertEqual(bank.decode_value(0xFFFFFFFF), dict(op="close"))

    def test_decode_reply(self):
        d = bank.decode_reply(client_answer(3, client_money_value(True, 2500)))
        self.assertEqual((d["event"], d["n"], d["value"]), (3, 1, 0xC0000002 | 500 << 20))
        self.assertIsNone(bank.decode_reply(bytes([0x0A]) + vce.put_bytes(bytes([3, 1, 6, 0, 0, 0, 1])))["value"])

    def test_pushes_parse(self):
        self.assertEqual(parse_30(bank.notify_transfer(True, 1234)), dict(sub=7, dir=1, amount=1234))
        d = parse_30(bank.notify_move(False, 4, 300, [(0, 99), (1, 99), (2, 99), (3, 3)], 0))
        self.assertEqual((d["dir"], d["src"], d["dests"]), (0, [4, 300], [[0, 99], [1, 99], [2, 99], [3, 3]]))
        lst = parse_list(bank.push_list(dict(money=4321, items=[dict(id=201, type=52, count=150, flags=1)])))
        self.assertEqual((lst["banked"], lst["A"], lst["B"], lst["records"][0][0x40]), (4321, 50, 99, 150))


class Npcs(Base):
    def test_two_npcs_in_nagoya_only(self):
        self.assertEqual(len(bank.spawn_pushes(self.ctx)), 2)
        roles = {cid: k["role"] for cid, k in self.ctx["npcs"].items()}
        self.assertEqual(roles, {CHANGER: "bank", KEEPER: "bank"})
        os.environ["NOBU_BANK_KEEPER"] = "0"
        self.ctx["npcs"] = {}
        self.assertEqual(len(bank.spawn_pushes(self.ctx)), 1)
        self.ctx["char"]["pos"][0] = 10
        self.assertEqual(bank.spawn_pushes(self.ctx), [])
        os.environ["NOBU_BANK_ANY_ZONE"] = "1"
        self.assertEqual(len(bank.spawn_pushes(self.ctx)), 1)
        os.environ["NOBU_BANK"] = "0"
        os.environ["NOBU_REPAIR"] = "0"         # nor the repairer (nobu/repair.py)
        os.environ["NOBU_DOCTOR"] = "0"         # nor the doctor (nobu/doctor.py)
        os.environ["NOBU_TOWN_QUESTS"] = "0"   # nor the quest givers (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        self.assertEqual(bank.spawn_pushes(self.ctx), [])

    def test_talk_opens_the_exchange_window(self):
        ev, out = self.open()
        self.assertEqual((ev["kind"], ev["variant"]), (7, 0))
        self.assertEqual(self.ctx["bank_event"]["id"], ev["id"])
        kinds = [p[:2].hex() for p in out]
        self.assertEqual(kinds[0], "0d06")                        # greeting
        lst = parse_list(next(p for p in out if p[:2] == b"\x24\x0f"))
        self.assertEqual((lst["banked"], lst["records"]), (0, []))
        # the list comes before the event so the window reads fresh storage
        self.assertLess([p[:2] for p in out].index(b"\x24\x0f"), [p[0] for p in out].index(0x11))

    def test_starter_bag_is_worn_and_the_bank_grants_nothing(self):
        bag = items.worn_container(self.inv)
        self.assertEqual((bag["id"], bag["type"], bag["pos"], bag["charges"]), (188, 60, 7, 30))
        n = len(self.inv["items"])
        _, out = self.open()
        self.assertEqual(len(self.inv["items"]), n)
        self.assertFalse(any(p[:2] == bytes([0x30, 0x03]) for p in out))

    def test_take_needs_room_in_the_bag(self):
        ev, _ = self.open()
        b = bank.bank_of(self.ctx["char"])
        b["items"] = [dict(id=1246, type=54, count=5, flags=1, idx=0)]
        items.worn_container(self.inv)["charges"] = len(self.inv["items"])   # every bag slot used
        self.ans(client_answer(ev["id"], client_item_value(True, 0, 5)))
        self.assertIsNone(self.item(1246))
        self.assertEqual(b["items"][0]["count"], 5)

class Money(Base):
    def test_deposit_and_withdraw(self):
        ev, _ = self.open()
        cb = ClientBank()
        cb.carried = self.inv["money"]
        out = self.ans(client_answer(ev["id"], client_money_value(True, 1200)))
        self.assertEqual((self.inv["money"], bank.bank_of(self.ctx["char"])["money"]), (300, 1200))
        pushes = [parse_30(p) if p[0] == 0x30 else p for p in out]
        self.assertEqual(pushes[0], dict(sub=7, dir=0, amount=1200))
        cb.transfer(pushes[0])
        self.assertEqual(pushes[1], dict(sub=6, money=300))
        cb.carried = pushes[1]["money"]
        cb.load_list(parse_list(out[2]))
        self.assertEqual((cb.carried, cb.banked), (300, 1200))
        self.assertEqual(parse_30(out[3]), dict(sub=7, dir=0, amount=0))   # refresh, no change
        self.assertEqual(out[-1][:2], b"\x0d\x06")
        c = self.reload()                                                    # saved together
        self.assertEqual((c["inventory"]["money"], c["bank"]["money"]), (300, 1200))
        # the event stays answerable (0x22fbe0 only clears the answer slots)
        self.ans(client_answer(ev["id"], client_money_value(False, 1000)))
        self.assertEqual((self.inv["money"], bank.bank_of(self.ctx["char"])["money"]), (1300, 200))

    def test_refusals_change_nothing(self):
        ev, _ = self.open()
        for v in (client_money_value(False, 1),                  # nothing banked
                  client_money_value(True, 999999)):              # more than carried
            out = self.ans(client_answer(ev["id"], v))
            self.assertEqual(self.inv["money"], 1500)
            self.assertEqual(bank.bank_of(self.ctx["char"])["money"], 0)
            self.assertFalse(any(p[:2] == b"\x30\x07" and parse_30(p)["amount"] for p in out))
        bank.bank_of(self.ctx["char"])["money"] = bank.MONEY_CAP
        self.ans(client_answer(ev["id"], client_money_value(True, 1)))
        self.assertEqual(self.inv["money"], 1500)

    def test_death_penalty_only_takes_carried_money(self):
        ev, _ = self.open()
        self.ans(client_answer(ev["id"], client_money_value(True, 1000)))
        lost, _ = battle.death_penalty(self.ctx["char"], self.store)
        self.assertEqual(lost, 250)
        self.assertEqual((self.inv["money"], bank.bank_of(self.ctx["char"])["money"]), (250, 1000))
        c = self.reload()
        self.assertEqual((c["inventory"]["money"], c["bank"]["money"]), (250, 1000))


class Storage(Base):
    def test_store_merge_and_take(self):
        ev, _ = self.open()
        cb = ClientBank()
        powder = self.item(201)                                   # Heal Powder x5, stackable
        idx = powder["idx"]
        backpack = [None] * 50
        for it in self.inv["items"]:
            backpack[it["idx"]] = dict(id=it["id"], idx=it["idx"], count=it["count"], stack=it["flags"] & 1)
        out = self.ans(client_answer(ev["id"], client_item_value(False, idx, 3)))
        mv = parse_30(out[0])
        self.assertEqual((mv["dir"], mv["src"], mv["dests"]), (0, [idx, 3], [[0, 3]]))
        self.assertTrue(cb.move(mv, backpack))
        self.assertEqual((powder["count"], self.stored(201)["count"]), (2, 3))
        self.assertEqual(backpack[idx]["count"], 2)
        cb.load_list(parse_list(out[1]))
        self.assertEqual(cb.storage[0]["count"], 3)
        # the rest merges into the same stack; the backpack entry disappears
        out = self.ans(client_answer(ev["id"], client_item_value(False, idx, 2)))
        mv = parse_30(out[0])
        self.assertEqual(mv["dests"], [[0, 2]])
        self.assertTrue(cb.move(mv, backpack))
        self.assertIsNone(self.item(201))
        self.assertIsNone(backpack[idx])
        self.assertEqual(self.stored(201)["count"], 5)
        # take 4 back
        out = self.ans(client_answer(ev["id"], client_item_value(True, 0, 4)))
        mv = parse_30(out[0])
        self.assertEqual((mv["dir"], mv["src"][1]), (1, 4))
        self.assertTrue(cb.move(mv, backpack))
        self.assertEqual((self.item(201)["count"], self.stored(201)["count"]), (4, 1))
        slot = mv["dests"][0][0]
        self.assertEqual((backpack[slot]["id"], backpack[slot]["count"]), (201, 4))
        c = self.reload()
        self.assertEqual(sum(it["count"] for it in c["bank"]["items"] if it["id"] == 201), 1)
        self.assertEqual(sum(it["count"] for it in c["inventory"]["items"] if it["id"] == 201), 4)

    def test_refusals(self):
        ev, _ = self.open()
        weapon = next(it for it in self.inv["items"] if it.get("pos") == 0)
        before = copy.deepcopy(self.inv["items"])
        out = self.ans(client_answer(ev["id"], client_item_value(False, weapon["idx"], 1)))
        self.assertEqual(self.inv["items"], before)
        self.assertFalse(any(p[:2] == b"\x30\x05" for p in out))
        goods = dict(id=900, type=72, cat=0, count=1, flags=1, idx=40)
        self.inv["items"].append(goods)
        self.ans(client_answer(ev["id"], client_item_value(False, 40, 1)))
        self.assertIn(goods, self.inv["items"])
        self.assertEqual(bank.bank_of(self.ctx["char"])["items"], [])
        self.ans(client_answer(ev["id"], client_item_value(True, 0, 1)))       # empty storage
        self.assertEqual(bank.bank_of(self.ctx["char"])["items"], [])

    def test_full_storage_keeps_everything(self):
        ev, _ = self.open()
        b = bank.bank_of(self.ctx["char"])
        b["items"] = [dict(id=1000 + i, type=54, count=1, flags=0, idx=i) for i in range(bank.SLOTS)]
        powder = self.item(201)
        out = self.ans(client_answer(ev["id"], client_item_value(False, powder["idx"], 5)))
        self.assertEqual(powder["count"], 5)
        self.assertEqual(len(b["items"]), bank.SLOTS)
        self.assertIn("Cannot deposit any more.", b"".join(out).decode("utf-8", "replace"))

    def test_non_stackables_one_slot_each_and_stack_only_knob(self):
        ev, _ = self.open()
        sword = dict(id=1605, type=3, cat=1, count=1, flags=0, dur=80, dur_max=100, idx=41, maker="X")
        self.inv["items"].append(sword)
        self.ans(client_answer(ev["id"], client_item_value(False, 41, 1)))
        st = self.stored(1605)
        self.assertEqual((st["dur"], st["maker"], st["count"]), (80, "X", 1))
        self.assertNotIn("pos", st)
        os.environ["NOBU_BANK_STACK_ONLY"] = "1"
        sword2 = dict(sword, idx=42)
        self.inv["items"].append(sword2)
        self.ans(client_answer(ev["id"], client_item_value(False, 42, 1)))
        self.assertIn(sword2, self.inv["items"])

    def test_random_operations_conserve_items_and_money(self):
        ev, _ = self.open()
        rng = random.Random(7)
        self.inv["items"].append(dict(id=1246, type=54, cat=0, count=150, flags=1, idx=30))
        self.inv["items"].append(dict(id=1246, type=54, cat=0, count=99, flags=1, idx=31))

        def totals():
            c = self.ctx["char"]
            cnt = {}
            for lst in (c["inventory"]["items"], bank.bank_of(c)["items"]):
                for it in lst:
                    cnt[it["id"]] = cnt.get(it["id"], 0) + int(it["count"])
            return cnt, c["inventory"]["money"] + bank.bank_of(c)["money"]

        start = totals()
        for _ in range(300):
            op = rng.randrange(4)
            if op == 0:
                v = client_money_value(True, rng.randrange(0, 2000))
            elif op == 1:
                v = client_money_value(False, rng.randrange(0, 2000))
            elif op == 2:
                live = [it["idx"] for it in self.inv["items"]] or [0]
                v = client_item_value(False, rng.choice(live + [49]), rng.randrange(0, 120))
            else:
                live = [it["idx"] for it in bank.bank_of(self.ctx["char"])["items"]] or [0]
                v = client_item_value(True, rng.choice(live + [49]), rng.randrange(0, 120))
            self.ans(client_answer(ev["id"], v))
            self.assertEqual(totals(), start)
            for it in self.inv["items"] + bank.bank_of(self.ctx["char"])["items"]:
                self.assertLessEqual(int(it["count"]), 150 if it.get("idx") == 30 else 99)
                self.assertGreater(int(it["count"]), 0)
            self.assertEqual([it["idx"] for it in bank.bank_of(self.ctx["char"])["items"]],
                             list(range(len(bank.bank_of(self.ctx["char"])["items"]))))
        c = self.reload()
        cnt = {}
        for lst in (c["inventory"]["items"], c["bank"]["items"]):
            for it in lst:
                cnt[it["id"]] = cnt.get(it["id"], 0) + int(it["count"])
        self.assertEqual((cnt, c["inventory"]["money"] + c["bank"]["money"]), start)


class Requests(Base):
    def test_list_info_discard_sort(self):
        b = bank.bank_of(self.ctx["char"])
        b["money"] = 2345
        b["items"] = [dict(id=279, type=69, count=3, flags=1, idx=0), dict(id=1605, type=3, cat=1, count=1,
                                                                             flags=0, idx=1, dur=50, dur_max=100)]
        (rep,) = self.ans(bytes.fromhex("240f0007"))
        lst = parse_list(rep)
        self.assertEqual((lst["seq"], lst["banked"], [r[0] for r in lst["records"]]), (7, 2345, [279, 1605]))
        (rep,) = self.ans(bytes.fromhex("24100008") + b"\x01")
        a = Arc(rep, 4)
        info = read_info(a)
        self.assertEqual((info[0], info[0x31], info[0x30]), (0, 50, 100))
        (rep,) = self.ans(bytes.fromhex("24120009"))
        self.assertEqual(rep.hex(), "24120009")
        self.assertEqual([it["id"] for it in b["items"]], [1605, 279])      # equipment first (0x200970)
        (rep,) = self.ans(bytes.fromhex("2411000a") + b"\x01\x02")
        self.assertEqual(rep.hex(), "2411000a" + "01" + "02" + "0002")
        self.assertEqual(b["items"][1]["count"], 1)
        (rep,) = self.ans(bytes.fromhex("2411000b") + b"\x05\x00")
        self.assertEqual(rep[4], 0xFF)
        self.assertEqual(self.ans(bytes.fromhex("2413000c") + b"\x00\x00"), [])

    def test_without_a_character_or_disabled(self):
        self.assertIsNone(bank.answer(bytes.fromhex("240f0001"), {}, self.store))
        os.environ["NOBU_BANK"] = "0"
        os.environ["NOBU_REPAIR"] = "0"         # nor the repairer (nobu/repair.py)
        os.environ["NOBU_DOCTOR"] = "0"         # nor the doctor (nobu/doctor.py)
        os.environ["NOBU_TOWN_QUESTS"] = "0"   # nor the quest givers (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        self.assertIsNone(self.ans(bytes.fromhex("240f0001")))


class Events(Base):
    def test_close_and_foreign_events(self):
        ev, _ = self.open()
        self.assertIsNone(self.ans(client_answer(ev["id"] + 1, client_money_value(True, 10))))
        self.assertEqual(self.ans(client_answer(ev["id"], 0xFFFFFFFF)), [])
        self.assertIsNone(self.ctx.get("bank_event"))
        self.assertIsNone(self.ans(client_answer(ev["id"], client_money_value(True, 10))))
        self.assertEqual(self.inv["money"], 1500)

    def test_reopen_knob(self):
        ev, _ = self.open()
        os.environ["NOBU_BANK_REOPEN"] = "1"
        out = self.ans(client_answer(ev["id"], client_money_value(True, 10)))
        self.assertEqual(parse_event(out[-1])["kind"], 7)
        os.environ["NOBU_BANK_RESYNC"] = "0"
        os.environ.pop("NOBU_BANK_REOPEN")
        out = self.ans(client_answer(self.ctx["bank_event"]["id"], client_money_value(True, 10)))
        self.assertFalse(any(p[:2] == b"\x24\x0f" for p in out))

    def test_chat(self):
        say = lambda t: bytes([0x23, 0x08]) + vce.put_u16(1) + vce.put_bytes(t.encode("utf-8"))
        self.ans(say("bank deposit 500"))
        self.assertEqual((self.inv["money"], bank.bank_of(self.ctx["char"])["money"]), (1000, 500))
        powder = self.item(201)
        self.ans(say(f"bank store {powder['idx']} 2"))
        self.assertEqual(self.stored(201)["count"], 2)
        out = self.ans(say("bank"))
        self.assertIn("500 mon deposited", b"".join(out).decode("utf-8", "replace"))
        self.assertIsNone(self.ans(say("hello")))


class World(Base):
    def setUp(self):
        super().setUp()
        self._store = world.STORE
        world.STORE = self.store

    def tearDown(self):
        world.STORE = self._store
        super().tearDown()

    def test_world_routes_bank_before_shops(self):
        pushes = world.world_ready_pushes(self.ctx)
        self.assertIn(CHANGER, self.ctx["npcs"])
        self.assertTrue(pushes)
        self.ctx["target"] = CHANGER
        out = world.answer(bytes.fromhex("28010001") + vce.put_u32(CHANGER), self.ctx)
        ev = parse_event(next(p for p in out if p[0] == 0x11))
        req = client_answer(ev["id"], client_money_value(True, 700))
        self.assertIn("BANK reply", world.describe(req))
        out = world.answer(req, self.ctx)
        self.assertEqual(bank.bank_of(self.ctx["char"])["money"], 700)
        self.assertTrue(world.describe(out[0], "S>C").startswith("NOTIFY_ITEM bank deposit"))
        self.assertIn("banked=0kan700mon", world.describe(out[2], "S>C"))
        self.assertIn("bank variant=0", world.describe(bank.bank_event(1), "S>C"))


if __name__ == "__main__":
    unittest.main()
