"""Player-to-player trade (cmd 0x27 / 0x33) and player wares (sale prices, 25 07 / 25 08, buying
from a selling player through a shop event).

Static reading of. world.answer() calls answer() after players / party; ○ on a player (28 01)
goes through wares_talk() before npc.resolve_target; world.session calls on_leave() on
disconnect.

Requests (KNOWN layouts: builders 0x1cb640..0x1cbdd0, wrappers 0x195f00.., net ids 0x83..0x8c,
request kinds 0x4a..0x53 in 0x1f9510; the UI caller is the trade window 0x675ea0 / object 0x201770..)
  27 01 <seq>                          start with the current target (no cid on the wire)
  27 02 <seq> u8 bp_idx u8 slot u8 qty ADD_ITEM: backpack index, offer slot 0..7, quantity
  27 03 <seq> u8 slot u8 qty           DELETE_ITEM: offer slot, quantity (the slot's count)
  27 04 <seq> u32 kan u32 mon          SET_MONEY (the window splits mon / 1000, mon % 1000)
  27 05 <seq>                          quit the trade (after UIMSG 2.227 "Cancel the trade?"); no reply parser
  27 06 / 07 / 08 / 09 <seq>           LOCK / UNLOCK / OK / CANCEL (the OK)
  27 0a <seq> u8 u8                    LOOK_ITEM (HYPOTHESIS: side, slot)
Replies `27 <sub> <seq> ...` (parser 0x28bd70, main 0x28cfa0 -> UI 0x5c..0x6d):
  1 struct 0x1f0dc0 (partner) -> window 0x10068 (opens trade window 0x3d) + msg 0xc2
  2 / 3 u8 a, u8 n, n x item record 0x1ec7f0, [n x u16 count] -> msg 0x4b / 0x4c
  4 u32 kan, u32 mon -> msg 0x4d (self object +0x5d4 / +0x5d8)
  6..9 i32 res -> msg 0x4f..0x52: res < 0 shows WDMMSG 0.65.(-1 - res) (0x21e8d0 table 6)
  0xa u8 + item info 0x1ece80 -> msg 0x53
Notifies `33 <sub> <seq> ...` (parser 0x28c5b0, main 0x28d1e0 -> UI 0x5d..0x6e):
  1 struct 0x1f0dc0 (NOTIFY_TRADE_START) -> window 0x10068 + msg 0xc2
  2 / 3 like reply 2 / 3 (partner's offer) -> 0xd0 / 0xd1 (NOTIFY_DEL_ITEM)
  4 u32 kan, u32 mon (NOTIFY_SET_MONEY) -> 0xd2
  5 / 6 / 7 / 8 no body: partner locked / unlocked / OK / OK cancelled -> 0xd4..0xd7
  9 / 0xa u32 kan, u32 mon, u8 n, records, [u16] -> window 0x10069 (close), 0xd3 / 0xd8, then the
    backpack gets money = kan * 1000 + mon and each record upserted at its index (0x208f60)
Self trade object UI+0x154b8, partner UI+0x11a9c (0x2016f0 reset, 8 offer slots of 0xb8):
  reply 2: take rec.count from backpack slot `a`, put the record into offer slot rec.idx (0x2024b0)
  reply 3: take rec.count from offer slot `a`, put it back into backpack slot rec.idx (0x2025f0)
  notify 2 / 3: the same on the partner's offer list (a is ignored for 2).

Wares (KNOWN request layouts, meanings partly HYPOTHESIS):
  24 17 <seq> u8 idx u32 price (kind 0x34, 0x1ff8f0 from the Sale Setup window): item record +0x3c
    is the price the window shows (obj +0xb4). Reply 0x1eee30: u32 res, u8 n, n x u8.
  23 0e kind 5: the Selling / Buying status (social.py stores it).
  25 07 <seq> u32 cid -> shop object 0x226f30 (UI 0x99, target +0xaac): the target's wares.
  25 08 <seq> u32 cid u8 row -> struct 0x1ee160 (record + info + u16) (UI 0x9a).
  Buying (design): ○ on a player with priced items pushes a cmd 0x11 shop event with those items;
    the `0a` buy answer moves money and items between the two characters (seller online).

Sale prices are kept per backpack entry as "price" and mirrored to "w3c", the field
items.item_record already writes at record +0x3c.

Env: NOBU_TRADE=0 disables everything here; NOBU_TRADE_RANGE (default 800) = distance for the
     nearest-player fallback when 27 01 arrives without a player target.
"""
import copy
import itertools
import os

from . import chars, items, misc, npc, players, shops, social, vce

CMD = 0x27                 # C>S requests and their replies
NOTIFY = 0x33              # S>C pushes to the partner
SLOTS = 8                  # offer slots per side (0x2016f0 loops 8 x 0xb8)
MAX_PRICE = 999_999_999    # Sale Setup numeric input max (0x3b9ac9ff at 0x6742b4)
RANGE = 800

SUBS = {1: "START", 2: "ADD_ITEM", 3: "DELETE_ITEM", 4: "SET_MONEY", 5: "QUIT", 6: "LOCK",
        7: "UNLOCK", 8: "OK", 9: "CANCEL", 0xA: "LOOK_ITEM"}
NOTIFY_SUBS = {1: "TRADE_START", 2: "ADD_ITEM", 3: "DEL_ITEM", 4: "SET_MONEY", 5: "LOCK",
               6: "UNLOCK", 7: "OK", 8: "CANCEL", 9: "CLOSED", 0xA: "DONE"}
N_LOCK, N_UNLOCK, N_OK, N_CANCEL, N_CLOSED, N_DONE = 5, 6, 7, 8, 9, 0xA

# res codes: the client shows WDMMSG 0.65.(-1 - res) (0x21e700: table 0x41, index -1 - res)
ERR_NO_TARGET, ERR_NOT_PC, ERR_BUSY, ERR_IGNORED, ERR_OFFLINE = -1, -2, -3, -4, -5
ERR_LOCKED, ERR_PARTNER_READY, ERR_PARTNER_UNLOCKED, ERR_NO_DETAIL = -7, -8, -9, -10
ERR_EQUIPPED, ERR_BAD_OFFER, ERR_COMBAT, ERR_TALKING = -11, -12, -13, -14
ERR_MINE_FULL, ERR_THEIRS_FULL, ERR_DEAD = -15, -16, -17
TEXTS = {   # WDMMSG 0.65.n (wdmmsg.tsv), for the system lines we send ourselves
    ERR_NO_TARGET: "No PC targeted.", ERR_NOT_PC: "Target is not a PC.",
    ERR_BUSY: "Target is already trading.", ERR_IGNORED: "Target is ignoring you.",
    ERR_OFFLINE: "Target is not online.", ERR_LOCKED: "Your terms are locked in.",
    ERR_PARTNER_READY: "Partner is ready to trade.",
    ERR_PARTNER_UNLOCKED: "Partner has not locked in terms.",
    ERR_NO_DETAIL: "Cannot get that item's details.", ERR_EQUIPPED: "Tried to offer equipment.",
    ERR_BAD_OFFER: "Invalid offer.", ERR_COMBAT: "Target is in combat.",
    ERR_TALKING: "Target is talking.",
    ERR_MINE_FULL: "Your trade items won't fit in your containers. Carry fewer belongings.",
    ERR_THEIRS_FULL: "Partner's containers can't hold the trade items.",
}

_IDS = itertools.count(1)


class TradeError(Exception):
    def __init__(self, code, text=None):
        super().__init__(text or TEXTS.get(code, "Trade failed."))
        self.code = code


def enabled():
    return os.environ.get("NOBU_TRADE", "1") == "1"


def _store(store):
    return store() if callable(store) else store


def name_of(c):
    return social.char_name(c) or "someone"


# ---------------------------------------------------------------------------- encoders
def reply(sub, seq, body=b""):
    return vce.put_u8(CMD) + vce.put_u8(sub) + vce.put_u16(seq) + body


def notify(sub, body=b"", seq=0):
    return vce.put_u8(NOTIFY) + vce.put_u8(sub) + vce.put_u16(seq) + body


def money_body(mon):
    kan, rest = items.money_split(mon)
    return vce.put_u32(kan) + vce.put_u32(rest)


def records_body(recs):
    """n x item record 0x1ec7f0, then n x u16 full counts (0x1ecca0, read only if bytes remain)."""
    return (vce.put_u8(len(recs)) + b"".join(items.item_record(r) for r in recs)
            + b"".join(vce.put_u16(min(int(r.get("count", 1)), 0xFFFF)) for r in recs))


def items_body(a, recs):
    """Reply / notify 2 and 3: u8 a, u8 n, records, counts."""
    return vce.put_u8(a & 0xFF) + records_body(recs)


def result_body(mon, recs):
    """Notify 9 / 0xa: u32 kan, u32 mon, u8 n, records, counts (0x208f60 upserts each record)."""
    return money_body(mon) + records_body(recs)


def res_reply(sub, seq, res):
    return reply(sub, seq, vce.put_u32(res & 0xFFFFFFFF))


def offer_record(it, slot, qty):
    """The record the client puts into an offer slot: the item with idx = slot, count = qty."""
    r = dict(it, idx=slot, count=qty)
    r.pop("pos", None)
    return r


# ---------------------------------------------------------------------------- trade state
def trade_of(ctx):
    t = (ctx or {}).get("trade")
    return t if t and not t.get("closed") else None


def side(t, ctx):
    return t["sides"][0] if t["sides"][0]["ctx"] is ctx else t["sides"][1]


def other(t, ctx):
    return t["sides"][1] if t["sides"][0]["ctx"] is ctx else t["sides"][0]


def new_side(ctx):
    return dict(ctx=ctx, offer={}, money=0, locked=False, ok=False)


def offered(sd, idx, skip_slot=None):
    return sum(o["qty"] for s, o in sd["offer"].items() if o["idx"] == idx and s != skip_slot)


def reserved(ctx, idx):
    """Quantity of backpack idx sitting in ctx's open trade offer."""
    t = trade_of(ctx)
    return offered(side(t, ctx), idx) if t else 0


def partner_target(ctx):
    """Session of the player ctx wants to trade with: the player target (25 01 on a 0xfe cid),
    else (HYPOTHESIS fallback, ○ may skip 25 01) the nearest ready player within NOBU_TRADE_RANGE."""
    t = ctx.get("target")
    if t is not None and npc.is_npc_cid(t):
        raise TradeError(ERR_NOT_PC)
    if t is not None and players.is_player_wire(t):
        o = players.session_by_cid(players.real_cid(t))
        if o is None or not players.ready(o) or players.zone_of(o) != players.zone_of(ctx):
            raise TradeError(ERR_OFFLINE)
        return o
    peers = players.peers(ctx)
    if not peers:
        raise TradeError(ERR_NO_TARGET)
    _, x, _, z, _ = players.position(ctx)

    def dist(o):
        _, ox, _, oz, _ = players.position(o)
        return ((ox - x) ** 2 + (oz - z) ** 2) ** 0.5

    best = min(peers, key=dist)
    if dist(best) > int(os.environ.get("NOBU_TRADE_RANGE", RANGE)):
        raise TradeError(ERR_NO_TARGET)
    return best


def partner_struct(of_ctx, viewer_ctx):
    """Struct 0x1f0dc0 of of_ctx's character with the cid viewer_ctx's client knows it by."""
    c = of_ctx["char"]
    wire = players.wire_cid(c["cid"], players.my_cid(viewer_ctx))
    return npc.target_info(dict(c, cid=wire), players.position(of_ctx))


def start(payload, ctx, store):
    seq = vce.Reader(payload, 2).u16()
    if trade_of(ctx):
        raise TradeError(ERR_BUSY, "You are already trading.")
    o = partner_target(ctx)
    if o is ctx or players.my_cid(o) == players.my_cid(ctx):
        raise TradeError(ERR_NOT_PC)
    if trade_of(o):
        raise TradeError(ERR_BUSY)
    me = players.my_cid(ctx)
    if any(x.get("cid") == me for x in (o["char"].get("ignores") or [])):
        raise TradeError(ERR_IGNORED)
    t = dict(id=next(_IDS), sides=[new_side(ctx), new_side(o)], closed=False)
    ctx["trade"] = o["trade"] = t
    players.push(o, notify(1, partner_struct(ctx, o)))
    players.push(o, misc.notify_system(f"{name_of(ctx['char'])} wants to trade with you."))
    return [reply(1, seq, partner_struct(o, ctx))]


def need_open(ctx, locked_ok=False):
    t = trade_of(ctx)
    if not t:
        raise TradeError(ERR_BAD_OFFER, "You are not trading.")
    if not locked_ok and side(t, ctx)["locked"]:
        raise TradeError(ERR_LOCKED)
    return t


def add_item(payload, ctx, store):
    r = vce.Reader(payload, 2)
    seq, idx, slot, qty = r.u16(), r.u8(), r.u8(), r.u8()
    try:
        t = need_open(ctx)
        sd = side(t, ctx)
        inv = items.inventory(ctx["char"], store)
        it = items.by_index(inv, idx)
        if it is None:
            raise TradeError(ERR_BAD_OFFER)
        if it.get("pos") is not None:
            raise TradeError(ERR_EQUIPPED)
        free = int(it.get("count", 1)) - offered(sd, idx)
        if qty <= 0 or qty > free:
            raise TradeError(ERR_BAD_OFFER)
        if not 0 <= slot < SLOTS or slot in sd["offer"]:
            slot = next((s for s in range(SLOTS) if s not in sd["offer"]), None)
            if slot is None:
                raise TradeError(ERR_BAD_OFFER, "Cannot trade any more items.")
    except TradeError as e:
        return [reply(2, seq, items_body(idx, [])), misc.notify_system(str(e))]
    sd["offer"][slot] = dict(idx=idx, qty=qty, id=it["id"])
    rec = offer_record(it, slot, qty)
    players.push(other(t, ctx)["ctx"], notify(2, items_body(idx, [rec])))
    return [reply(2, seq, items_body(idx, [rec]))]


def delete_item(payload, ctx, store):
    r = vce.Reader(payload, 2)
    seq, slot, qty = r.u16(), r.u8(), r.u8()
    try:
        t = need_open(ctx)
        sd = side(t, ctx)
        o = sd["offer"].get(slot)
        if o is None:
            raise TradeError(ERR_BAD_OFFER)
    except TradeError as e:
        return [reply(3, seq, items_body(slot, [])), misc.notify_system(str(e))]
    qty = o["qty"] if qty <= 0 or qty > o["qty"] else qty
    o["qty"] -= qty
    if o["qty"] <= 0:
        del sd["offer"][slot]
    inv = items.inventory(ctx["char"], store)
    it = items.by_index(inv, o["idx"]) or dict(id=o["id"], idx=o["idx"])
    back = dict(it, idx=o["idx"], count=qty)          # rec.idx = backpack slot it returns to
    gone = dict(it, idx=slot, count=qty)
    players.push(other(t, ctx)["ctx"], notify(3, items_body(slot, [gone])))
    return [reply(3, seq, items_body(slot, [back]))]


def set_money(payload, ctx, store):
    r = vce.Reader(payload, 2)
    seq, kan, mon = r.u16(), r.u32(), r.u32()
    total = kan * 1000 + mon
    t = trade_of(ctx)
    cur = side(t, ctx)["money"] if t else 0
    try:
        t = need_open(ctx)
        if total > int(items.inventory(ctx["char"], store).get("money", 0)):
            raise TradeError(ERR_BAD_OFFER, "Not enough money.")
    except TradeError as e:
        return [reply(4, seq, money_body(cur)), misc.notify_system(str(e))]
    side(t, ctx)["money"] = total
    players.push(other(t, ctx)["ctx"], notify(4, money_body(total)))
    return [reply(4, seq, money_body(total))]


def flag(payload, ctx, store):
    sub = payload[1]
    seq = vce.Reader(payload, 2).u16()
    t = trade_of(ctx)
    if not t:
        return [res_reply(sub, seq, ERR_BAD_OFFER)]
    me, him = side(t, ctx), other(t, ctx)
    peer = him["ctx"]
    out = []
    if sub == 6:                                      # LOCK
        if not me["locked"]:
            me["locked"] = True
            players.push(peer, notify(N_LOCK))
        return [res_reply(6, seq, 0)]
    if sub == 7:                                      # UNLOCK
        if me["ok"]:
            return [res_reply(7, seq, ERR_LOCKED)]
        if him["ok"]:
            return [res_reply(7, seq, ERR_PARTNER_READY)]
        if me["locked"]:
            me["locked"] = False
            players.push(peer, notify(N_UNLOCK))
        return [res_reply(7, seq, 0)]
    if sub == 8:                                      # OK
        if not him["locked"]:
            return [res_reply(8, seq, ERR_PARTNER_UNLOCKED)]
        if not me["locked"]:                          # the window locks first (0x677010); be lenient
            me["locked"] = True
            players.push(peer, notify(N_LOCK))
        if not me["ok"]:
            me["ok"] = True
            players.push(peer, notify(N_OK))
        out.append(res_reply(8, seq, 0))
        if him["ok"]:
            out += execute(t, ctx, store)
        return out
    if sub == 9:                                      # CANCEL (the OK)
        if me["ok"]:
            me["ok"] = False
            players.push(peer, notify(N_CANCEL))
        return [res_reply(9, seq, 0)]
    return None


def look_item(payload, ctx, store):
    """27 0a <seq> u8 side u8 slot (HYPOTHESIS: 0 = own offer, 1 = partner's) -> u8, info 0x1ece80."""
    r = vce.Reader(payload, 2)
    seq, which, slot = r.u16(), r.u8(), r.u8()
    t = trade_of(ctx)
    it = None
    if t:
        sd = side(t, ctx) if which == 0 else other(t, ctx)
        o = sd["offer"].get(slot)
        if o:
            it = items.by_index(items.inventory(sd["ctx"]["char"], store), o["idx"])
    if it is None:
        return [reply(0xA, seq, vce.put_u8(0) + items.info_struct(None, 0xFF)),
                misc.notify_system(TEXTS[ERR_NO_DETAIL])]
    return [reply(0xA, seq, vce.put_u8(0) + items.info_struct(it))]


# ---------------------------------------------------------------------------- closing
def restore_records(ctx, sd, store):
    """Backpack records at their full server count for every offered index (the client took the
    offered quantity out of its backpack view when the offer was made)."""
    inv = items.inventory(ctx["char"], store)
    seen, out = set(), []
    for o in sd["offer"].values():
        if o["idx"] in seen:
            continue
        seen.add(o["idx"])
        it = items.by_index(inv, o["idx"])
        if it is not None:
            out.append(it)
    return out


def close(t, store, reason=None, texts=None, me=None):
    """End trade t without an exchange: 33 09 to both sides (window closed, offered items and the
    unchanged money back in the backpack view). Frames for `me` are returned (so they follow any
    reply in order), the other side's are pushed."""
    if t.get("closed"):
        return []
    t["closed"] = True
    mine = []
    for sd in t["sides"]:
        c = sd["ctx"]
        if c.get("trade") is t:
            c.pop("trade", None)
        if not c.get("char"):
            continue
        inv = items.inventory(c["char"], store)
        frames = [notify(N_CLOSED, result_body(inv.get("money", 0), restore_records(c, sd, store)))]
        text = (texts or {}).get(id(c), reason)
        if text:
            frames.append(misc.notify_system(text))
        if c is me:
            mine += frames
        else:
            for f in frames:
                players.push(c, f)
    return mine


def quit_trade(ctx, store, text_self="Trade cancelled.", text_peer=None):
    """Close ctx's trade; returns ctx's own frames (33 09 + text), the partner's are pushed."""
    t = trade_of(ctx)
    if not t:
        return []
    peer = other(t, ctx)["ctx"]
    who = name_of(ctx["char"]) if ctx.get("char") else "Your partner"
    return close(t, store, me=ctx, texts={id(ctx): text_self,
                                          id(peer): text_peer or f"{who} declined the trade."})


def on_leave(ctx, store=None):
    """Session end (world.session) or zone change: close the trade; nothing was moved yet, so the
    partner gets the offered items back in view and both inventories stay as they are."""
    if not ctx or not trade_of(ctx):
        return False
    from . import world
    quit_trade(ctx, store if store is not None else world.store(), text_self=None,
               text_peer="Your trade partner left. The trade was cancelled.")
    return True                                       # own frames dropped: the socket is gone


# ---------------------------------------------------------------------------- the exchange
def take(inv, gives):
    """Remove {idx: qty} from a copy of inv; returns the given item copies (count = qty)."""
    out = []
    for idx, qty in sorted(gives.items()):
        it = items.by_index(inv, idx)
        if it is None or it.get("pos") is not None or int(it.get("count", 1)) < qty or qty <= 0:
            raise TradeError(ERR_BAD_OFFER)
        g = dict(it, count=qty)
        for k in ("pos", "idx", "price", "w3c"):
            g.pop(k, None)
        out.append(g)
        it["count"] = int(it.get("count", 1)) - qty
        if it["count"] <= 0:
            inv["items"].remove(it)
    return out


def put(inv, goods, full_code):
    """Add goods to inv (stack onto same-id stackables up to MAX_STACK, then the lowest free
    indexes). Returns the touched entries. Raises TradeError(full_code) if they do not fit."""
    touched = []
    for g in goods:
        left = int(g.get("count", 1))
        stack = bool(g.get("flags", 0) & items.FLAG_STACK)
        if stack:
            for it in inv["items"]:
                if left and it["id"] == g["id"] and it.get("flags", 0) & items.FLAG_STACK:
                    n = min(items.MAX_STACK - int(it.get("count", 1)), left)
                    if n > 0:
                        it["count"] = int(it.get("count", 1)) + n
                        left -= n
                        if it not in touched:
                            touched.append(it)
        per = items.MAX_STACK if stack else 1
        while left > 0:
            used = {it.get("idx") for it in inv["items"]}
            free = next((i for i in range(items.CAPACITY) if i not in used), None)
            if free is None:
                raise TradeError(full_code)
            n = min(left, per)
            it = dict(g, count=n, idx=free)
            inv["items"].append(it)
            touched.append(it)
            left -= n
    return touched


def plan(inv_a, inv_b, give_a, give_b, money_a, money_b):
    """Pure: new (inventory, touched) for both sides, or TradeError. Inputs are not modified."""
    na, nb = copy.deepcopy(inv_a), copy.deepcopy(inv_b)
    if money_a < 0 or money_b < 0 or money_a > int(na.get("money", 0)) \
            or money_b > int(nb.get("money", 0)):
        raise TradeError(ERR_BAD_OFFER, "Not enough money.")
    goods_a, goods_b = take(na, give_a), take(nb, give_b)
    left_a = {it.get("idx") for it in na["items"]}
    left_b = {it.get("idx") for it in nb["items"]}
    touched_a = put(na, goods_b, ERR_MINE_FULL)
    touched_b = put(nb, goods_a, ERR_THEIRS_FULL)
    na["money"] = int(na.get("money", 0)) - money_a + money_b
    nb["money"] = int(nb.get("money", 0)) - money_b + money_a
    # records the client needs: received / topped-up entries, plus partly given stacks
    part_a = [it for it in na["items"] if it.get("idx") in give_a and it.get("idx") in left_a]
    part_b = [it for it in nb["items"] if it.get("idx") in give_b and it.get("idx") in left_b]
    return (na, _uniq(touched_a + part_a)), (nb, _uniq(touched_b + part_b))


def _uniq(lst):
    out = []
    for it in lst:
        if all(it is not x for x in out):
            out.append(it)
    return out


def gives(sd):
    out = {}
    for o in sd["offer"].values():
        out[o["idx"]] = out.get(o["idx"], 0) + o["qty"]
    return out


def execute(t, ctx, store):
    """Both sides confirmed: check everything again, swap in memory, save chars.json once, then
    33 0a to both. Any failure closes the trade with 33 09 (nothing moves)."""
    a, b = t["sides"]
    ca, cb = a["ctx"], b["ctx"]
    inv_a = items.inventory(ca["char"], store)
    inv_b = items.inventory(cb["char"], store)
    try:
        if players.zone_of(ca) != players.zone_of(cb):
            raise TradeError(ERR_OFFLINE)
        (na, ta), (nb, tb) = plan(inv_a, inv_b, gives(a), gives(b), a["money"], b["money"])
    except TradeError as e:
        # the codes name the receiver's side from a's point of view; swap for b
        swap = {ERR_MINE_FULL: ERR_THEIRS_FULL, ERR_THEIRS_FULL: ERR_MINE_FULL}
        return close(t, store, me=ctx, texts={
            id(ca): str(e), id(cb): TEXTS.get(swap.get(e.code, e.code), str(e))})
    t["closed"] = True                                # from here on nothing can run twice
    inv_a["items"][:] = na["items"]
    inv_a["money"] = na["money"]
    inv_b["items"][:] = nb["items"]
    inv_b["money"] = nb["money"]
    items.save(store)
    for c in (ca, cb):
        if c.get("trade") is t:
            c.pop("trade", None)
    log = dict(a=players.my_cid(ca), b=players.my_cid(cb), a_gave=gives(a), b_gave=gives(b),
               a_money=a["money"], b_money=b["money"])
    ca.setdefault("trade_log", []).append(log)
    cb.setdefault("trade_log", []).append(log)
    mine = []
    for c, inv, touched in ((ca, inv_a, ta), (cb, inv_b, tb)):
        frames = [notify(N_DONE, result_body(inv["money"], touched)),
                  misc.notify_system("Trade complete.")]
        if c is ctx:
            mine += frames
        else:
            for f in frames:
                players.push(c, f)
    return mine


# ---------------------------------------------------------------------------- wares
def wares(c):
    """[(backpack item, price)] of c's items with a sale price (not equipped)."""
    inv = c.get("inventory")
    if not isinstance(inv, dict):
        return []
    return [(it, int(it.get("price", 0))) for it in inv.get("items", [])
            if int(it.get("price", 0)) > 0 and it.get("pos") is None][:shops.MAX_GOODS]


def wares_shop(c, reserved_of=None):
    goods = []
    for it, price in wares(c):
        n = int(it.get("count", 1)) - (reserved_of(it["idx"]) if reserved_of else 0)
        goods.append(dict(id=it["id"], type=it.get("type", 0), cat=it.get("cat", 0), price=price,
                          stack=bool(it.get("flags", 0) & items.FLAG_STACK),
                          flags=it.get("flags", 0), stock=max(1, min(n, 0xFF)), idx=it["idx"],
                          name=items.item_name(it["id"])))
    return dict(name=name_of(c), mode=0, items=goods)


def set_price(payload, ctx, store):
    """24 17 <seq> u8 idx u32 price -> 24 17 <seq> u32 res, u8 n, n x u8 (0x1eee30)."""
    r = vce.Reader(payload, 2)
    seq, idx, price = r.u16(), r.u8(), r.u32()
    head = vce.put_u8(0x24) + vce.put_u8(0x17) + vce.put_u16(seq)
    inv = items.inventory(ctx["char"], store)
    it = items.by_index(inv, idx)
    if it is None:
        return [head + vce.put_u32(0xFFFFFFFF) + vce.put_u8(0)]
    price = min(int(price), MAX_PRICE)
    if price:
        it["price"] = it["w3c"] = price               # w3c = item record +0x3c (items.item_record)
    else:
        it.pop("price", None)
        it.pop("w3c", None)
    items.save(store)
    return [head + vce.put_u32(0) + vce.put_u8(1) + vce.put_u8(idx)]


def seller_session(wire):
    if not players.is_player_wire(wire):
        return None
    return players.session_by_cid(players.real_cid(wire))


def look_wares(payload, ctx, store):
    """25 07 <seq> u32 cid -> 25 07 <seq> shop object (the seller's priced items)."""
    r = vce.Reader(payload, 2)
    seq, wire = r.u16(), r.u32()
    o = seller_session(wire)
    shop = wares_shop(o["char"], lambda i: reserved(o, i)) if o and o.get("char") \
        else dict(name="", mode=0, items=[])
    return [payload[:4] + shops.shop_record(shop, [0] * shops.SELL_SLOTS)]


def look_ware(payload, ctx, store):
    """25 08 <seq> u32 cid u8 row -> record 0x1ec7f0 + info 0x1ecf30 + u16 count (0x1ee160)."""
    r = vce.Reader(payload, 2)
    seq, wire, row = r.u16(), r.u32(), r.u8()
    o = seller_session(wire)
    lst = wares(o["char"]) if o and o.get("char") else []
    it = lst[row][0] if row < len(lst) else dict(id=0, count=0, idx=0)
    return [payload[:4] + items.item_record(it) + items.info_struct(it)
            + vce.put_u16(min(int(it.get("count", 1)), 0xFFFF))]


def wares_talk(ctx, store):
    """28 01 with a player target that has wares: push the wares as a shop event (the buyer's
    `0a` answer is handled by wares_reply). None when the target is not a selling player."""
    if not enabled() or not ctx or not ctx.get("char"):
        return None
    wire = ctx.get("target")
    if wire is None or not players.is_player_wire(wire):
        return None
    o = seller_session(wire)
    if o is None or not o.get("char") or o is ctx or not wares(o["char"]):
        return None
    if players.zone_of(o) != players.zone_of(ctx):
        return None
    return [open_wares(ctx, o)]


def open_wares(ctx, o):
    shop = wares_shop(o["char"], lambda i: reserved(o, i))
    eid = shops.next_event_id(ctx)
    ctx["wares_event"] = dict(id=eid, seller=players.my_cid(o),
                              rows=[(g["idx"], g["id"], g["price"]) for g in shop["items"]])
    return shops.shop_event(eid, shop, [0] * shops.SELL_SLOTS)


def buy_from(ctx, o, row, qty, ev, store):
    """Buyer ctx buys qty of ev row from seller o. All or nothing; returns (buyer touched, seller
    item after, cost, name)."""
    if row >= len(ev["rows"]) or qty <= 0:
        raise TradeError(ERR_BAD_OFFER, "That item is not for sale.")
    idx, iid, price = ev["rows"][row]
    inv_s = items.inventory(o["char"], store)
    it = items.by_index(inv_s, idx)
    if it is None or it["id"] != iid or int(it.get("price", 0)) != price or it.get("pos") is not None:
        raise TradeError(ERR_BAD_OFFER, "That item is no longer for sale.")
    if int(it.get("count", 1)) - reserved(o, idx) < qty:
        raise TradeError(ERR_BAD_OFFER, "Not that many for sale.")
    cost = price * qty
    inv_b = items.inventory(ctx["char"], store)
    (nb, tb), (ns, _) = plan(inv_b, inv_s, {}, {idx: qty}, cost, 0)
    inv_b["items"][:] = nb["items"]
    inv_b["money"] = nb["money"]
    inv_s["items"][:] = ns["items"]
    inv_s["money"] = ns["money"]
    items.save(store)
    after = items.by_index(inv_s, idx)
    return tb, (after if after is not None else dict(it, count=0)), cost, items.item_name(iid)


def wares_reply(payload, ctx, store):
    """`0a` answer to our wares event: buy; sell answers are refused, close clears the event."""
    d = shops.decode_event_reply(payload)
    ev = ctx.get("wares_event")
    if not ev or ev["id"] != d["event"]:
        return None
    ctx["wares_event"] = None
    if d["n"] == shops.NO_RECORDS or not d["answers"] or "value" not in d["answers"][0]:
        return []
    a = d["answers"][0]
    o = players.session_by_cid(ev["seller"])
    if o is None or not players.ready(o) or players.zone_of(o) != players.zone_of(ctx):
        return [misc.notify_system("The seller is no longer here.")]
    if not a["buy"]:
        out = [misc.notify_system(f"{name_of(o['char'])} is selling, not buying.")]
        return out + ([open_wares(ctx, o)] if wares(o["char"]) else [])
    try:
        touched, left, cost, name = buy_from(ctx, o, a["index"], a["qty"], ev, store)
    except TradeError as e:
        out = [misc.notify_system(str(e))]
        return out + ([open_wares(ctx, o)] if wares(o["char"]) else [])
    inv_b = items.inventory(ctx["char"], store)
    inv_s = items.inventory(o["char"], store)
    log = dict(buyer=players.my_cid(ctx), seller=ev["seller"], item=left["id"], qty=a["qty"],
               cost=cost)
    ctx.setdefault("trade_log", []).append(log)
    o.setdefault("trade_log", []).append(log)
    players.push(o, shops.notify_money(inv_s.get("money", 0)))
    if os.environ.get("NOBU_SHOP_ITEM_PUSH", "1") == "1":
        players.push(o, shops.notify_counts([(left["idx"], max(0, int(left.get("count", 0))))]))
    players.push(o, misc.notify_system(
        f"{name_of(ctx['char'])} bought {name} x{a['qty']} from you for {cost} mon."))
    out = [shops.notify_money(inv_b.get("money", 0))]
    if os.environ.get("NOBU_SHOP_ITEM_PUSH", "1") == "1" and touched:
        out.append(shops.notify_items(touched))
    out.append(misc.notify_system(f"Bought {name} x{a['qty']} for {cost} mon."))
    if wares(o["char"]):
        out.append(open_wares(ctx, o))
    return out


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Replies for cmd 0x27, 24 17, 25 07 / 25 08 and the `0a` answer to a wares event; observes
    04 / 23 5a (zone change closes the trade). None for everything else."""
    if not enabled() or not payload or ctx is None:
        return None
    cmd = payload[0]
    sub = payload[1] if len(payload) > 1 else None
    try:
        if cmd == 4 or (cmd == 0x23 and sub == 0x5A):
            if trade_of(ctx):                         # own frames dropped: the field is reloading
                quit_trade(ctx, store, text_self=None,
                           text_peer="Your trade partner moved away. The trade was cancelled.")
            return None
        if cmd == shops.EVENT_REPLY:
            return wares_reply(payload, ctx, store) if ctx.get("wares_event") else None
        if not ctx.get("char") or len(payload) < 4:
            return None
        if cmd == 0x24 and sub == 0x17:
            return set_price(payload, ctx, store)
        if cmd == 0x25 and sub == 7:
            return look_wares(payload, ctx, store)
        if cmd == 0x25 and sub == 8:
            return look_ware(payload, ctx, store)
        if cmd != CMD:
            return None
        if sub == 1:
            try:
                return start(payload, ctx, store)
            except TradeError as e:
                return [misc.notify_system(str(e))]
        if sub == 2:
            return add_item(payload, ctx, store)
        if sub == 3:
            return delete_item(payload, ctx, store)
        if sub == 4:
            return set_money(payload, ctx, store)
        if sub == 5:
            return quit_trade(ctx, store)
        if sub in (6, 7, 8, 9):
            return flag(payload, ctx, store)
        if sub == 0xA:
            return look_item(payload, ctx, store)
    except vce.WireError:
        return None
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        r = vce.Reader(payload, 2)
        seq = r.u16()
        if cmd == CMD:
            label = f"TRADE {SUBS.get(sub, f'sub {sub}')} {'reply' if direction == 'S>C' else ''}".rstrip()
            rest = r.rest()
            if direction == "C>S":
                if sub == 2 and len(rest) >= 3:
                    return f"{label} seq={seq} bp_idx={rest[0]} slot={rest[1]} qty={rest[2]}"
                if sub == 3 and len(rest) >= 2:
                    return f"{label} seq={seq} slot={rest[0]} qty={rest[1]}"
                if sub == 4 and len(rest) >= 8:
                    return f"{label} seq={seq} kan={r.u32()} mon={r.u32()}"
                return f"{label} seq={seq} {rest.hex()}"
            if sub in (6, 7, 8, 9) and len(rest) >= 4:
                return f"{label} seq={seq} res={r.i32()}"
            return f"{label} seq={seq} +{len(rest)} bytes"
        if cmd == NOTIFY and direction == "S>C":
            return f"NOTIFY_TRADE {NOTIFY_SUBS.get(sub, f'sub {sub}')} +{len(r.rest())} bytes"
        if cmd == 0x24 and sub == 0x17:
            if direction == "C>S" and len(r.rest()) >= 5:
                return f"WARES set price seq={seq} idx={r.u8()} price={r.u32()}"
            return f"WARES set price reply seq={seq} {r.rest().hex()}"
        if cmd == 0x25 and sub in (7, 8):
            what = "look at wares" if sub == 7 else "ware detail"
            if direction == "C>S" and len(r.rest()) >= 4:
                return f"WARES {what} seq={seq} cid={r.u32():#x}"
            return f"WARES {what} reply seq={seq} +{len(r.rest())} bytes"
    except vce.WireError:
        return None
    return None
