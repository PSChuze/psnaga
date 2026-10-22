"""Money changer / bank and item storage: deposit / withdraw money, store / take items.

Addresses. world.answer() calls answer() here before crafting / shops (shops.on_reply claims every
`0a`), and world.describe() calls describe() before crafting / shops. world_ready_pushes() appends
spawn_pushes().

How the bank window opens (as the client reads it)
  cmd 0x11 event record with WIRE KIND 7 (0x1a0958: object of 0x10 bytes, ctor 0x1eaf50, archive
  0x227160 = event header 0x1eafc0 then an optional u8 "variant" at +0xc), stored as record type 6 ->
  waiter 0x91 (S+0x10208, 0x206058) -> window message 0x100cf (0x6458b0) -> 0x642560 creates the bank
  window (type 0x9f, handler 0x61a190, open 0x61b0e0). Variant 0 = exchange "Exchange" (UIMSG 2.46,
  money + items), 1 =, 2 = fief: 1 / 2 list other containers (S+0x48a4 / +0x48d0) and are not served
  here. NOTE: wire kind 6 is the shop (type 7); shops.py sent 7.
  Menu (0x61ce20, UIMSG 2.11): Withdraw, Deposit, Take, Store, Discard. Client checks (0x61ceb0 /
  0x61a5b0 / 0x61a6b8): nothing banked -> 2.332, carried + banked capped at 999,999,999 (2.333 / 2.335),
  nothing carried -> 2.334, storage empty -> 2.336, backpack empty -> 2.337. Take (0x61bb60) needs room
  in the backpack AS THE CLIENT COUNTS IT: the first N slots, N = +0x60 of the item worn in equipment
  position 7 (a type 60 container; record +0x18 "charges"); no container -> 2.331 "Cannot carry more".
  The shop's buy check 0x382eb8 is the same. Every character wears a starter bag.
Answers (KNOWN, 0x22f080 / 0x22f150 -> 0x198f80 / 0x198e70): `0a <bytes: id n 07 <u32 v>>`
  money (type 7):  v = 0x80000000 | 0x40000000 if deposit | (mon % 1000) << 20 | (kan & 0xfffff)
  items (type 6):  v = 0x40000000 if take | (qty & 0xfff) << 8 | (index & 0xff)
                   take: index = storage idx (the list row); store: index = backpack idx (item +0x1e)
  close (type 0x16, 0x22f550 -> 0x199090): v = 0xffffffff; the client drops the event (0x22e920).
  After a money / item answer 0x22fbe0 only clears the answer slots: the window stays answerable.
Client state (item manager S = UI+0x2c):
  storage = 50 slots at S+0x24ac (0xb8 each), banked money S+0x489c kan / +0x48a0 mon.
  24 0f (world load request; reply UI 0x1f -> 0x208990, no waiter): u32 kan, u32 mon, i8 count,
    count x record 0x1ec7f0, [u16 A, u16 B = stack cap -> S+0x546a, count x u16 full count]. Clears
    and reloads the whole storage and the banked money: our absolute resync.
  30 07 (UI 0x37 -> 0x2099f0): u8 dir, u32 mon; dir 1: bank -= v, carried += v; 0: the reverse.
    Then waiter 0xc9 -> the bank window refreshes (0x100cc).
  30 05 (struct 0x1ee3d0, UI 0x24 -> 0x208b60): u8 dir (0 backpack -> storage, 1 storage -> backpack),
    u8 n, src {u8 idx, u8 qty}, n x dest {u8 slot, u8 qty}, [u16 src qty, n x u16 dest qty],
    [u32 sale price]. Takes qty from the source slot whose +0x1e == idx, adds each dest qty into
    array slot `slot` (new item or merge into the same stackable id, 0x200840); failures make the
    client re-request (dir 0: storage, dir 1: 24 01 backpack). Then waiters 0xc9 / 0xbd (refresh).
  24 10 u8 idx -> info struct 0x1ece80 (as 24 02); 24 11 u8 idx, u8 n -> u8 idx, u8 n [, u16 k]
    (0x2084e0 on the storage: slot idx loses k); 24 12 -> header only (the client sorts its storage
    0x200e30); 24 13 u8, u8 has no reply parser.

State (DESIGN): per character in chars.json, c["bank"] = {"money": mon, "items": [item dicts in the
items.py format, idx = storage slot 0..49]}. Every operation is planned on the current state, applied
to the inventory and the bank in memory only if it succeeds, and saved once (CharStore.save writes the
whole file through a temp file + os.replace). The death penalty (battle.py) only touches
inventory["money"], so banked money and stored items are safe.

Env:
  NOBU_BANK=0               no bank NPCs, 24 0f / 10 / 11 / 12 left to the old empty replies
  NOBU_BANK_CID             the Money Changer's cid (default 0xfea00011)
  NOBU_BANK_KEEPER_CID      the Storekeeper's cid (default 0xfea00012); NOBU_BANK_KEEPER=0: no keeper
  NOBU_BANK_ANY_ZONE=1      spawn them in every zone (default: every clan's home town, towns.py)
  NOBU_BANK_TOWNS=0         Nagoya (map 110) only
  NOBU_BANK_RESYNC=0        no 24 0f + 30 07 (0) resync after an operation
  NOBU_BANK_REOPEN=1        push a fresh bank event after each operation
  NOBU_BANK_STACK_ONLY=1    refuse non-stackable items (UIMSG 0.291)
  NOBU_BANK_CHAT=0          no chat commands ("bank", "bank deposit N", "bank withdraw N",
                            "bank store <idx> [n]", "bank take <idx> [n]")
"""
import copy
import os
import unicodedata

from . import chars, items, misc, npc, shops, social, towns, vce

# ---------------------------------------------------------------------------- constants
KIND_BANK = 7                # wire kind in cmd 0x11 (0x1a0958 -> type 6 -> waiter 0x91)
ANSWER_BANK = 7              # answer tag of types 6 / 7 / 0x16 (0x198e70 / 0x198f80 / 0x199090)
VARIANT_EXCHANGE, VARIANT_KEEPER, VARIANT_FIEF = 0, 1, 2
MONEY = 0x80000000           # 0x22ae90 / 0x22aed0
IN = 0x40000000              # money: deposit; items: take (list row)
CLOSE = 0xFFFFFFFF           # type 0x16 (0x22af40)
SLOTS = 50                   # storage slots S+0x24ac (0x208990 clears 50 x 0xb8)
MAX_STACK = 99               # 24 0f tail B -> S+0x546a (stack cap of 0x200840)
MONEY_CAP = 999_999_999      # 0x61cf98 / 0x61a628: carried + banked is capped here
T_TRADE_GOODS = items.T_TRADE_GOODS   # Trade Goods (the shop's sell check 0x666e70 tests the same type)
SUB_LIST, SUB_INFO, SUB_DISCARD, SUB_SORT, SUB_13 = 0x0F, 0x10, 0x11, 0x12, 0x13
SUBS = {SUB_LIST: "STORAGE_LIST", SUB_INFO: "STORAGE_INFO", SUB_DISCARD: "STORAGE_DISCARD",
        SUB_SORT: "STORAGE_SORT", SUB_13: "STORAGE_13"}
PUSH_MOVE, PUSH_XFER = 5, 7  # 30 05 / 30 07

NPCS = [
    dict(key="changer", env="NOBU_BANK_CID", cid="0xfea00011", family="Money Changer",
         greeting="Money and goods are safe with me. What will it be?", dx=300, dz=-300, dir=315,
         style=(2, 1, 60)),
    dict(key="keeper", env="NOBU_BANK_KEEPER_CID", cid="0xfea00012", family="Storekeeper",
         greeting="Leave your goods here; nothing is lost when you fall in battle.", dx=300, dz=300,
         dir=225, style=(4, 2, 70)),
]
ZONE = 110                   # Nagoya: the changer / keeper keep their NPCS cids there
ROLE = "bank"
TOWN_ROLE = {"changer": 0x11, "keeper": 0x12}   # per-town cid role bytes elsewhere (shops.town_cid scheme)


def enabled():
    return os.environ.get("NOBU_BANK", "1") == "1"


# ---------------------------------------------------------------------------- state
def bank_of(c):
    """c["bank"] = {"money": mon, "items": [...]}, created empty on first use."""
    b = c.get("bank")
    if not isinstance(b, dict):
        b = c["bank"] = dict(money=0, items=[])
    b.setdefault("money", 0)
    b.setdefault("items", [])
    return b


def renumber(bank):
    for i, it in enumerate(bank["items"]):
        it["idx"] = i


def by_index(lst, idx):
    return next((it for it in lst if it.get("idx") == idx), None)


def stackable(it):
    return bool(int(it.get("flags", 0)) & items.FLAG_STACK)


def total(inv_or_bank):
    return sum(int(it.get("count", 1)) for it in inv_or_bank["items"])


# ---------------------------------------------------------------------------- encoders
def event_record(variant=VARIANT_EXCHANGE):
    """0x227160: event header 0x1eafc0, then u8 variant (+0xc; optional on the wire)."""
    return shops.event_header() + vce.put_u8(variant)


def bank_event(event_id, variant=VARIANT_EXCHANGE):
    """11 <event id> 01 07 <record>."""
    return (vce.put_u8(shops.EVENT) + vce.put_u8(event_id) + vce.put_u8(1) + vce.put_u8(KIND_BANK)
            + event_record(variant))


def money_value(deposit, amount):
    kan, mon = divmod(max(0, int(amount)), 1000)
    return MONEY | (IN if deposit else 0) | ((mon & 0x3FF) << 20) | (kan & 0xFFFFF)


def item_value(take, index, qty):
    return (IN if take else 0) | ((qty & 0xFFF) << 8) | (index & 0xFF)


def decode_value(v):
    """The answer u32 -> dict(op=close|withdraw|deposit|take|store, amount / index, qty)."""
    if v == CLOSE:
        return dict(op="close")
    if v & MONEY:
        amount = (v & 0xFFFFF) * 1000 + ((v >> 20) & 0x3FF)
        return dict(op="deposit" if v & IN else "withdraw", amount=amount)
    return dict(op="take" if v & IN else "store", index=v & 0xFF, qty=(v >> 8) & 0xFFF)


def decode_reply(payload):
    """`0a <bytes: id n {07 u32}>` -> dict(event, n, value) (value None when not a bank answer)."""
    r = vce.Reader(payload, 1)
    b = vce.Reader(r.bytes(0x400))
    d = dict(event=b.u8(), n=b.u8(), value=None)
    if d["n"] != shops.NO_RECORDS and d["n"] and not b.done() and b.u8() == ANSWER_BANK:
        d["value"] = b.u32()
    return d


def storage_list(bank, head):
    """24 0f reply body after head: u32 kan, u32 mon, i8 count, records, u16 A, u16 B, u16 counts."""
    renumber(bank)                                    # what the client sees is always compact
    its = bank["items"][:SLOTS]
    kan, mon = items.money_split(bank.get("money", 0))
    out = head + vce.put_u32(kan) + vce.put_u32(mon) + vce.put_u8(len(its))
    out += b"".join(items.item_record(it) for it in its)
    out += vce.put_u16(SLOTS) + vce.put_u16(MAX_STACK)
    out += b"".join(vce.put_u16(min(int(it.get("count", 1)), 0xFFFF)) for it in its)
    return out


def push_list(bank, seq=0):
    """Unsolicited 24 0f (UI 0x1f is not a waiter: 0x208990 applies it whatever the seq)."""
    return storage_list(bank, vce.put_u8(0x24) + vce.put_u8(SUB_LIST) + vce.put_u16(seq))


def notify_transfer(withdraw, amount, seq=0):
    """30 07 <seq> u8 dir (1 = withdraw) u32 mon."""
    return (vce.put_u8(0x30) + vce.put_u8(PUSH_XFER) + vce.put_u16(seq) + vce.put_u8(1 if withdraw else 0)
            + vce.put_u32(max(0, int(amount))))


def notify_move(take, src_idx, src_qty, dests, price=0, seq=0):
    """30 05 <seq> 0x1ee3d0: u8 dir, u8 n, {u8 idx, u8 qty}, n x {u8 slot, u8 qty}, u16 src qty,
    n x u16 qty, u32 price. dests = [(slot, qty added)]."""
    out = (vce.put_u8(0x30) + vce.put_u8(PUSH_MOVE) + vce.put_u16(seq) + vce.put_u8(1 if take else 0)
           + vce.put_u8(len(dests)) + vce.put_u8(src_idx & 0xFF) + vce.put_u8(min(src_qty, 0xFF)))
    for slot, n in dests:
        out += vce.put_u8(slot & 0xFF) + vce.put_u8(min(n, 0xFF))
    out += vce.put_u16(min(src_qty, 0xFFFF)) + b"".join(vce.put_u16(min(n, 0xFFFF)) for _, n in dests)
    return out + vce.put_u32(price & 0xFFFFFFFF)


# ---------------------------------------------------------------------------- planning
def plan_add(lst, proto, qty, limit):
    """Where qty of proto would go in item list lst (at most `limit` slots, indexes = slots):
    [(existing item or None, slot, n)] or None when it does not fit. Stackables top up stacks of the
    same id first (cap MAX_STACK), then use the lowest free slots; others take one slot each."""
    plan, left = [], qty
    if stackable(proto):
        for it in lst:
            if left and it["id"] == proto["id"] and stackable(it):
                n = min(MAX_STACK - int(it.get("count", 1)), left)
                if n > 0:
                    plan.append((it, it["idx"], n))
                    left -= n
    per = MAX_STACK if stackable(proto) else 1
    used = {it.get("idx") for it in lst}
    free = [i for i in range(limit) if i not in used]
    need = -(-left // per)
    if need > len(free):
        return None
    for slot in free[:need]:
        n = min(left, per)
        plan.append((None, slot, n))
        left -= n
    return plan


def apply_add(lst, proto, plan):
    changed = []
    for it, slot, n in plan:
        if it is not None:
            it["count"] = int(it.get("count", 1)) + n
            changed.append(it)
        else:
            new = copy.deepcopy(proto)
            new.pop("pos", None)
            new.update(count=n, idx=slot)
            lst.append(new)
            changed.append(new)
    return changed


def take_from(lst, it, qty):
    it["count"] = int(it.get("count", 1)) - qty
    if it["count"] <= 0:
        lst.remove(it)


# ---------------------------------------------------------------------------- operations
def deposit(c, amount, store=None):
    """(ok, text, amount) carried -> bank."""
    inv, bank = items.inventory(c, store), bank_of(c)
    carried, banked = int(inv.get("money", 0)), int(bank.get("money", 0))
    amount = int(amount)
    if carried <= 0:
        return False, "You have no money.", 0                       # UIMSG 2.334
    if amount <= 0 or amount > carried:
        return False, "You do not have that much money.", 0         # 2.338
    if banked + amount > MONEY_CAP:
        return False, "Cannot deposit more.", 0                     # 2.335
    inv["money"], bank["money"] = carried - amount, banked + amount
    items.save(store)
    return True, f"Deposited {money_text(amount)}. Balance {money_text(bank['money'])}.", amount


def withdraw(c, amount, store=None):
    inv, bank = items.inventory(c, store), bank_of(c)
    carried, banked = int(inv.get("money", 0)), int(bank.get("money", 0))
    amount = int(amount)
    if banked <= 0:
        return False, "No money deposited.", 0                      # 2.332
    if amount <= 0 or amount > banked:
        return False, "You have not deposited that much.", 0
    if carried + amount > MONEY_CAP:
        return False, "Cannot hold more money.", 0                  # 2.333
    inv["money"], bank["money"] = carried + amount, banked - amount
    items.save(store)
    return True, f"Withdrew {money_text(amount)}. Balance {money_text(bank['money'])}.", amount


def store_item(c, idx, qty, store=None):
    """Backpack idx -> storage. Returns dict(ok, text, src, qty, dests [(slot, n)], left)."""
    inv, bank = items.inventory(c, store), bank_of(c)
    it = items.by_index(inv, idx)
    fail = lambda text: dict(ok=False, text=text, src=idx, qty=0, dests=[], left=None)
    if it is None:
        return fail("That item is no longer there.")               # 0.279
    if it.get("pos") is not None:
        return fail("Can't deposit equipment.")                     # 0.288 (worn items)
    if int(it.get("type", 0)) == T_TRADE_GOODS:
        return fail("Can't deposit trade goods.")                   # 0.289
    if not stackable(it) and os.environ.get("NOBU_BANK_STACK_ONLY") == "1":
        return fail("Non-stackable items can't be deposited.")      # 0.291
    have = int(it.get("count", 1))
    qty = have if qty <= 0 or qty > have else int(qty)
    plan = plan_add(bank["items"], it, qty, SLOTS)
    if plan is None:
        return fail("Cannot deposit any more.")                     # 0.290
    apply_add(bank["items"], it, plan)
    take_from(inv["items"], it, qty)
    renumber(bank)                                   # no-op: new slots are appended at the end
    items.save(store)
    name = items.item_name(it["id"])
    return dict(ok=True, text=f"Stored {name} x{qty}.", src=idx, qty=qty,
                dests=[(slot, n) for _, slot, n in plan], left=max(0, int(it.get("count", 0))))


def take_item(c, idx, qty, store=None):
    """Storage idx -> backpack."""
    inv, bank = items.inventory(c, store), bank_of(c)
    it = by_index(bank["items"], idx)
    fail = lambda text: dict(ok=False, text=text, src=idx, qty=0, dests=[], left=None)
    if it is None:
        return fail("No items deposited there.")                     # 2.336
    have = int(it.get("count", 1))
    qty = have if qty <= 0 or qty > have else int(qty)
    plan = plan_add(inv["items"], it, qty, items.bag_slots(inv))   # the client's room check (0x61bb60)
    if plan is None:
        return fail("Cannot carry more.")                            # 2.331
    apply_add(inv["items"], it, plan)
    take_from(bank["items"], it, qty)
    renumber(bank)                                   # the 24 0f resync tells the client
    items.save(store)
    return dict(ok=True, text=f"Took {items.item_name(it['id'])} x{qty}.", src=idx, qty=qty,
                dests=[(slot, n) for _, slot, n in plan], left=max(0, int(it.get("count", 0))),
                price=int(it.get("w3c", 0)))


def discard_item(c, idx, n, store=None):
    """24 11 (HYPOTHESIS: the Discard entry): storage idx loses n (0 = all). Returns removed."""
    bank = bank_of(c)
    it = by_index(bank["items"], idx)
    if it is None:
        return 0
    have = int(it.get("count", 1))
    k = have if n <= 0 or n >= have else n
    take_from(bank["items"], it, k)
    renumber(bank)
    items.save(store)
    return k


def money_text(v):
    kan, mon = items.money_split(v)
    return f"{kan} kan {mon} mon" if kan else f"{mon} mon"


# ---------------------------------------------------------------------------- NPCs
def npc_defs():
    out = []
    for n in NPCS:
        if n["key"] == "keeper" and os.environ.get("NOBU_BANK_KEEPER", "1") != "1":
            continue
        cid = shops.parse_cid(os.environ.get(n["env"]) or n["cid"])
        if npc.is_npc_cid(cid):
            out.append((n, cid))
    return out


def bank_zone(zone):
    """A money changer stands in every clan's home town (2004 FAQ: every town has its exchange), or Nagoya only
    with NOBU_BANK_TOWNS=0."""
    if zone == ZONE:
        return True
    return os.environ.get("NOBU_BANK_TOWNS", "1") != "0" and towns.is_town(zone)


def spawn_pushes(ctx):
    """The Money Changer and Storekeeper of the player's town: at its exchange (town_spots.json roles bank_changer /
    bank_keeper), else at their offsets. Outside Nagoya each town has its own cids (role bytes 0x11 / 0x12). Storage
    is per character, so every town's counter opens the same bank."""
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone, x, y, z, _ = chars.char_position(player)
    if not bank_zone(zone) and os.environ.get("NOBU_BANK_ANY_ZONE") != "1":
        return []
    out = []
    for n, cid in npc_defs():
        if zone != ZONE and towns.is_town(zone):
            cid = shops.town_cid(zone, TOWN_ROLE[n["key"]])
        lk = npc.npc_looks(player, "merchant")
        lk[5], lk[6], lk[7], lk[8] = n["style"][0], n["style"][1], n["style"][1], n["style"][2]
        k = dict(cid=cid, uid=0, b6b=npc.TALK_ONLY, family=n["family"], given="", looks=lk,
                 level=player.get("level", 1), role=ROLE, text=n["greeting"])
        if towns.is_town(zone):
            pos = npc.town_spot(player, n["dx"], n["dz"], n["dir"] % 360, role="bank_" + n["key"])
        else:
            pos = (zone, x + n["dx"], y, z + n["dz"], n["dir"] % 360)
        k["pos"] = list(pos)
        ctx.setdefault("npcs", {})[cid] = k
        out += npc.spawn_pushes(k, pos)
    return out


def open_bank(ctx, store):
    c = ctx["char"]
    eid = shops.next_event_id(ctx)
    ctx["bank_event"] = dict(id=eid)
    return [push_list(bank_of(c)), bank_event(eid)]


def talk(ctx, store, k):
    out = []
    if k.get("text"):
        out.append(npc.notify_npc_say(k["cid"], k["text"], name=npc.npc_name(k)))
    items.inventory(ctx["char"], store)
    bank_of(ctx["char"])
    return out + open_bank(ctx, store)


# ---------------------------------------------------------------------------- answers
def resync(ctx):
    """Absolute storage + banked money (24 0f), then a no-op 30 07 so the window refreshes (0xc9)."""
    if os.environ.get("NOBU_BANK_RESYNC", "1") != "1":
        return []
    return [push_list(bank_of(ctx["char"])), notify_transfer(False, 0)]


def money_pushes(ctx, withdraw_, amount):
    inv = items.inventory(ctx["char"])
    return [notify_transfer(withdraw_, amount), shops.notify_money(inv.get("money", 0))]


def do_op(ctx, store, op):
    """One decoded answer / chat command. Returns pushes."""
    c = ctx["char"]
    log = ctx.setdefault("bank_log", [])
    if op["op"] in ("deposit", "withdraw"):
        fn = deposit if op["op"] == "deposit" else withdraw
        ok, text, amount = fn(c, op["amount"], store)
        log.append((op["op"], op["amount"], ok))
        out = money_pushes(ctx, op["op"] == "withdraw", amount) if ok else []
        return out + resync(ctx) + [misc.notify_system(text)]
    if op["op"] in ("store", "take"):
        fn = store_item if op["op"] == "store" else take_item
        res = fn(c, op["index"], op["qty"], store)
        log.append((op["op"], op["index"], op["qty"], res["ok"]))
        out = []
        if res["ok"]:
            out.append(notify_move(op["op"] == "take", res["src"], res["qty"], res["dests"],
                                   res.get("price", 0)))
        return out + resync(ctx) + [misc.notify_system(res["text"])]
    return []


def on_reply(payload, ctx, store):
    d = decode_reply(payload)
    ev = ctx.get("bank_event")
    if not ev or ev["id"] != d["event"]:
        return None                                   # not ours: crafting / shops decide
    if d["n"] == shops.NO_RECORDS or d["value"] is None or d["value"] == CLOSE:
        ctx["bank_event"] = None
        return []
    if not ctx.get("char"):
        return []
    out = do_op(ctx, store, decode_value(d["value"]))
    if os.environ.get("NOBU_BANK_REOPEN") == "1":
        out += open_bank(ctx, store)
    return out


def storage_request(payload, ctx, store):
    """24 0f / 10 / 11 / 12 with a loaded character."""
    sub, head = payload[1], bytes(payload[:4])
    c = ctx["char"]
    bank = bank_of(c)
    r = vce.Reader(payload, 4)
    if sub == SUB_LIST:
        renumber(bank)
        return [storage_list(bank, head)]
    if sub == SUB_INFO:
        idx = r.u8() if len(r.rest()) else 0xFF
        it = by_index(bank["items"], idx)
        return [head + items.info_struct(it, 0 if it else 0xFF)]
    if sub == SUB_DISCARD:
        idx = r.u8() if len(r.rest()) else 0xFF
        n = r.u8() if len(r.rest()) else 0
        k = discard_item(c, idx, n, store)
        ctx.setdefault("bank_log", []).append(("discard", idx, n, k))
        return [head + vce.put_u8(idx if k else 0xFF) + vce.put_u8(n) + vce.put_u16(k)]
    if sub == SUB_SORT:
        bank["items"].sort(key=items.sort_key)
        renumber(bank)
        items.save(store)
        return [head]
    return []                                          # 24 13: no reply parser in the client


# ---------------------------------------------------------------------------- chat
def _chat_text(payload):
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    _, text = social.parse_line(line)
    return unicodedata.normalize("NFKC", text or "").strip().lower().lstrip("/!").strip()


def status_lines(c):
    inv, bank = items.inventory(c), bank_of(c)
    lines = [f"Bank: {money_text(bank['money'])} deposited, {money_text(inv.get('money', 0))} carried."]
    if bank["items"]:
        lines.append("Stored: " + ", ".join(f"[{it['idx']}] {items.item_name(it['id'])} x{it.get('count', 1)}"
                                            for it in bank["items"]))
    else:
        lines.append("No items deposited.")
    return [misc.notify_system(t) for t in lines]


def chat(ctx, text, store=None):
    toks = (text or "").split()
    if not toks or toks[0] != "bank":
        return None
    if len(toks) == 1:
        return status_lines(ctx["char"])
    verb, args = toks[1], toks[2:]
    try:
        nums = [int(a) for a in args]
    except ValueError:
        nums = None
    if verb in ("deposit", "withdraw") and nums and len(nums) == 1:
        return do_op(ctx, store, dict(op=verb, amount=nums[0]))
    if verb in ("store", "take") and nums and len(nums) in (1, 2):
        return do_op(ctx, store, dict(op=verb, index=nums[0], qty=nums[1] if len(nums) > 1 else 0))
    return [misc.notify_system('Say "bank", "bank deposit <mon>", "bank withdraw <mon>", '
                               '"bank store <backpack idx> [n]" or "bank take <storage idx> [n]".')]


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """28 01 on a bank NPC, 0a answers to the open bank event, 24 0f..12, "bank" chat; else None."""
    if not enabled() or len(payload) < 2 or not isinstance(ctx, dict):
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if cmd == 0x28 and sub == 1 and len(payload) >= 4:
            k = ctx.get("npcs", {}).get(ctx.get("target"))
            if not k or k.get("role") != ROLE or not ctx.get("char"):
                return None
            ctx.setdefault("actions", []).append(ctx.get("target"))
            return talk(ctx, store, k)
        if cmd == shops.EVENT_REPLY and ctx.get("bank_event"):
            return on_reply(payload, ctx, store)
        if cmd == 0x24 and sub in SUBS and len(payload) >= 4 and ctx.get("char"):
            return storage_request(payload, ctx, store)
        if (cmd == 0x23 and sub == 8 and ctx.get("char")
                and os.environ.get("NOBU_BANK_CHAT", "1") == "1"):
            text = _chat_text(payload)
            return chat(ctx, text, store) if text is not None else None
    except vce.WireError:
        return None
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if direction == "C>S" and cmd == shops.EVENT_REPLY:
            d = decode_reply(payload)
            if d["value"] is None:
                return None
            op = decode_value(d["value"])
            detail = " ".join(f"{k}={v}" for k, v in op.items() if k != "op")
            return f"BANK reply id={d['event']} {op['op']} {detail}".rstrip()
        if direction == "S>C" and cmd == shops.EVENT and len(payload) >= 4 and payload[3] == KIND_BANK:
            return f"EVENT id={payload[1]} n={payload[2]} bank variant={payload[-1]}"
        if cmd == 0x24 and sub in SUBS and len(payload) >= 4:
            r = vce.Reader(payload, 2)
            seq = r.u16()
            label = f"ITEM {SUBS[sub]} seq={seq}"
            if direction == "S>C" and sub == SUB_LIST:
                kan, mon, n = r.u32(), r.u32(), r.u8()
                return f"{label} banked={kan}kan{mon}mon items={n}"
            return f"{label} {r.rest().hex()}".rstrip()
        if direction == "S>C" and cmd == 0x30 and sub in (PUSH_MOVE, PUSH_XFER) and len(payload) >= 4:
            r = vce.Reader(payload, 4)
            if sub == PUSH_XFER:
                d = r.u8()
                return f"NOTIFY_ITEM bank {'withdraw' if d == 1 else 'deposit'} {r.u32()} mon"
            d, n, src, q = r.u8(), r.u8(), r.u8(), r.u8()
            return f"NOTIFY_ITEM bank {'take' if d else 'store'} src={src} qty={q} dests={n}"
    except vce.WireError:
        return None
    return None
