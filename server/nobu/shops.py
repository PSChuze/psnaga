"""NPC shops (buy / sell), plus the market (cmd 0x1b) and market-price (23 0a..0c) windows.

Addresses. world.answer() / describe() call answer() / describe() here before npc.py (answer() returns
None for frames that are not ours); world_ready_pushes() appends spawn_pushes(ctx).

How an NPC shop opens (KNOWN)
  There is no shop request. The server pushes S>C cmd 0x11, an "event" of typed records (DoResponse
  0x19e7b0 -> 0x1a04f8): `11 <u8 event id> <u8 n> n x {<u8 kind> <archive object>}` (n == 0xff: no
  records, UI 0x84). Main handler 0x18af38 -> UI 0x7f -> 0x258c20: the event is kept in the queue
  UI+0x33740 (0x22e830) and each record is posted to the UI waiters by kind (table 0x4276e0). Wire
  tag 6 = page type 7 = shop: 0x2087f0 copies its sell-price table into the backpack items (item
  +0x160), then message 0x92. The shop window (World.bin, parent class vt 0x45dee0 at 0x665a20,
  goods list vt 0x45de50 at 0x6663c0, opened by window message 0x100d0 with {record id, shop})
  copies the shop to 0x6f9950 and lists it.
Buy / sell (KNOWN)
  Buy (list vt 0x5c = 0x666b00 -> 0x100d4) and sell (backpack vt 0x94 = 0x666dd0 -> 0x100d5) call
  0x22ef30(queue, record id, buy, index, qty), which answers the record (type 8, once per record)
  and, when every record of the event is answered, 0x22e970 sends
  C>S `0a <bytes blob>` (0x198ac0), blob = `<u8 event id> <u8 n>` + one answer per record; the shop
  answer (0x199160) is `06 <u32 v>`, v = 0x40000000 (buy) | (qty & 0xfff) << 8 | (index & 0xff).
  Buy index = row in the shop list, sell index = the backpack item's +0x1e byte (record +0x0c, the
  "idx" of items.py). Closing without a trade sends `0a <bytes: id ff>` (0x22e9f0). There is no
  S>C 0x0a parser: the server answers with pushes.
Money (KNOWN): S>C `30 06 <u16 seq> <u32 total mon>` -> UI 0x36 -> 0x2099c0 sets kan = v / 1000,
  mon = v % 1000 (the buy check 0x382e0c compares kan * 1000 + mon with price * qty).

Shop record (0x226f30 read half 0x227018):
  str name (max 0x21, shown as "%1$s's Shop", UIMSG 0.234), u8 count (<= 50), u8 mode (+0x4e0,
  stored only), u32 array prices (max count, mon per unit), u32 array sell prices (max 50, indexed
  by backpack index), count x goods entry, event header.
  Goods entry (0x1ee760, 16 bytes): u8 +0 stock (0 hides the row), u8 +1, u8 +3 equipment category,
  u16 +8, u16 +0xa item type (STRDAT table 3; 72 = trade goods, refused by the sell check
  0x666e70), u16 +6 flags (bit 0 = self-describing entry; else +2 names a backpack slot),
  u32 +0xc item id (STRDAT table 0), u8 +2 backpack slot, u8 +4 item flags (bit 0 stackable).
  Event header (0x1eafc0): u32 flags, u16 id, u32 only if flags & 1, u8.

Env:
  NOBU_SHOPS=0              no shop NPCs, 28 01 / 0a left to npc.py
  NOBU_SHOPS_FILE           shop data (default $NOBU_DATA/shops.json, seeded from SHOPS_SEED)
  NOBU_SHOP_REOPEN=0        do not push a fresh shop event after a trade
  NOBU_SHOP_ITEM_PUSH=0     do not push 30 03 / 30 04 item changes after a trade (HYPOTHESIS layouts)
  NOBU_SHOP_ANY_ZONE=1      outside the towns, spawn the home town's shopkeepers (default: only in their "zone")
  NOBU_SHOP_VENDORS=0       no equipment vendors (the shops whose keeper has a "shop_<vendor>" spot)
Every clan's home town (nobu/towns.py) has a general Merchant (SHOPS_SEED, _town_shops); the keepers stand at
Nagoya's offsets from the town's spawn point, turned with the town's gate (npc.town_spot).
"""
import copy
import json
import os

from . import persist
from . import chars, items, misc, npc, towns, vce

EVENT = 0x11                 # S>C event (0x1a04f8)
EVENT_REPLY = 0x0A           # C>S event answers (0x198ac0)
KIND_SHOP = 6                # wire tag in cmd 0x11: tag 6 -> page type 7 = the shop (0x1a09e0; tag 7 is a
                             # 0x10-byte object 0x1eaf50, read by 0x227160: the bank record, bank.py). Type 7 -> UI message 0x92
ANSWER_SHOP = 6              # answer kind in cmd 0x0a (0x199160)
BUY = 0x40000000
SELL_SLOTS = 50              # sell-price table size (u32 array max 50, 0x2087f0 loops 50)
MAX_GOODS = 50               # the object holds 50 entries; the parser does not bound count
NO_RECORDS = 0xFF
# A shop answer with index -1 and qty -1 (masked to 0xff / 0xfff on the wire, v = 0x400fffff) is the
# goods list's buy post (0x666b00 -> 0x100d4 -> 0x22ef30) sent with no row chosen. 0.6 s after every
# shop event; it is not a purchase. Answering it with a refusal and a reopened shop looped forever
# ("That item is not for sale." every 0.6 s).
NO_ROW = 0xFF
NO_QTY = 0xFFF
NAME_MAX = 0x21

# ---------------------------------------------------------------------------- data Nagoya
# general goods. Item ids = STRDAT table 0 (strdat_00_items.tsv). Prices (mon),
# the wiki (current prices; the pre-2005 tables do not cover these goods): general store
# Polish Grass 5, Lumber 8, Bamboo 8, Curse Talisman 25, Leather 50, Washi Paper 200; teahouse
# Water 5, Dango 4, Rice Ball 4, Doburoku 15; medicine seller Heal Powder 800. Item types (STRDAT
# table 3) are INVENTED (the client has no type column): 52 medicine, 54 material, 55 sake, 62
# lumber, 69 food.
SHOPS_SEED = {
    "sell_rate": 0.5,
    "sell_default": 1,
    "shops": {
        "nagoya_general": {
            "name": "Merchant",
            "zone": 110,
            "mode": 0,
            "greeting": "Welcome! Take a look at my wares.",
            "npc": {"cid": "0xfea00003", "family": "Merchant", "given": "", "dx": 0, "dz": -300,
                    "dir": 0},
            "items": [
                {"id": 1246, "name": "Polish Grass", "type": 54, "price": 5, "src": "B general store"},
                {"id": 420, "name": "Lumber", "type": 62, "price": 8, "src": "B general store"},
                {"id": 502, "name": "Bamboo", "type": 62, "price": 8, "src": "B general store"},
                {"id": 226, "name": "Curse Talisman", "type": 54, "price": 25, "src": "B general store"},
                {"id": 430, "name": "Leather", "type": 54, "price": 50, "src": "B general store"},
                {"id": 419, "name": "Washi Paper", "type": 54, "price": 200, "src": "B general store"},
                {"id": 428, "name": "Water", "type": 69, "price": 5, "src": "B teahouse"},
                {"id": 279, "name": "Dango", "type": 69, "price": 4, "src": "B teahouse"},
                {"id": 282, "name": "Rice Ball", "type": 69, "price": 4, "src": "B teahouse"},
                {"id": 278, "name": "Doburoku", "type": 55, "price": 15, "src": "B teahouse"},
                {"id": 201, "name": "Heal Powder", "type": 52, "price": 800, "src": "B medicine seller"},
            ],
        },
    },
}


def town_cid(zone, role):
    """NPC cid of a per-town keeper outside Nagoya (whose keepers keep their old cids): 0xfea0 | map << 8 | role, in
    the NPC range (npc.is_npc_cid) and distinct per town, so a town's keepers never share a cid with another's."""
    return 0xFEA00000 | (int(zone) & 0xFFF) << 8 | (int(role) & 0xFF)


def _town_shops():
    """One general Merchant per home town (towns.py) other than Nagoya: Nagoya's goods (the wiki's stock is the
    same in every town, from the wiki) plus the town's local foods / sake (the wiki's province page, towns.FOOD).
    Same offsets as Nagoya's Merchant, turned with the town's gate."""
    base = SHOPS_SEED["shops"]["nagoya_general"]
    out = {}
    for m, t in sorted(towns.TOWNS.items()):
        if m == towns.NAGOYA:
            continue
        out[f"{t['key']}_general"] = {
            "name": "Merchant", "zone": m, "mode": 0,
            "greeting": f"Welcome to {t['name']}! Take a look at my wares.",
            "npc": dict(base["npc"], cid=f"{town_cid(m, 3):#x}"),
            "items": copy.deepcopy(base["items"]) + towns.goods(m),
        }
    return out


SHOPS_SEED["shops"].update(_town_shops())

VENDORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendors_2004.json")
VENDOR_ROLE = 0x20                 # per-town cid role bytes 0x20.. (shops.town_cid scheme)
VENDOR_ORDER = ("sword", "spear", "club", "armour", "helmet", "bow", "kosode", "suikan", "teahouse", "medicine",
                "jeweller")


MINO = 111                         # Inabayama: the vendors' Mino only stock is sold here only


def _town_vendors():
    """Every home town's vendors with their stock (vendors_2004.json): weapons and armour at the smith hall, bows
    at the shrine, clothes at the clothier (2004 FAQ, 2004 prices), food and drink at the teahouse and medicine at
    the apothecary (current prices), hair ornaments at the jeweller (2004 prices). A vendor exists in a town only
    where town_spots.json has its spot (shop_<vendor>: Nagoya's buildings, and the other towns' hand-read ones), so
    none falls back onto the spawn point. Mino-only stock only in Inabayama."""
    try:
        with open(VENDORS_FILE, encoding="utf-8") as fh:
            vendors = json.load(fh).get("vendors", {})
    except (OSError, ValueError):
        return {}
    spots = towns._load_spots()
    out = {}
    for m, t in sorted(towns.TOWNS.items()):
        for i, role in enumerate(VENDOR_ORDER):
            v = vendors.get(role)
            if not v or "shop_" + role not in spots.get(str(m), {}):
                continue
            stock = [{k: g[k] for k in ("id", "name", "type", "cat", "dur", "stack", "price") if k in g}
                     | {"src": "wiki %s (before 2005-05-25)" % v.get("page", role)}
                     for g in v.get("items", []) if not g.get("mino") or m == MINO]
            out["%s_%s" % (t["key"], role)] = {
                "name": v["name"], "zone": m, "mode": 0,
                "greeting": "Take a look.",
                "npc": {"cid": f"{town_cid(m, VENDOR_ROLE + i):#x}", "family": v["name"], "given": "",
                        "dx": 0, "dz": -300, "dir": 0, "spot": "shop_" + role},
                "items": stock,
            }
    return out


SHOPS_SEED["shops"].update(_town_vendors())

CASTLE_VENDOR_ROLE = 0x30          # per-castle cid role byte of the ninja house's seller


def _castle_vendors():
    """The ninja house's weapon seller in every castle with a spot for it (shop_ninja, the ninja house: Nagoya
    Castle and the hand-read castles)."""
    try:
        with open(VENDORS_FILE, encoding="utf-8") as fh:
            v = json.load(fh).get("vendors", {}).get("ninja")
    except (OSError, ValueError):
        return {}
    if not v:
        return {}
    spots = towns._load_spots()
    out = {}
    for m, t in sorted(towns.TOWNS.items()):
        cm = m + 300
        if "shop_ninja" not in spots.get(str(cm), {}):
            continue
        out["%s_castle_ninja" % t["key"]] = {
            "name": v["name"], "zone": cm, "mode": 0, "greeting": "Quietly now. Take a look.",
            "npc": {"cid": f"{town_cid(cm, CASTLE_VENDOR_ROLE):#x}", "family": v["name"], "given": "",
                    "dx": 0, "dz": -300, "dir": 0, "spot": "shop_ninja"},
            "items": [dict(g, src="wiki %s" % v.get("page", "ninja tool shop")) for g in v["items"]],
        }
    return out


SHOPS_SEED["shops"].update(_castle_vendors())
for _shop in SHOPS_SEED["shops"].values():          # English "name" by id, the wiki's Japanese kept as "name_jp"
    items.english_rows(_shop["items"])


def shops_path():
    return os.environ.get("NOBU_SHOPS_FILE") or os.path.join(misc.data_dir(), "shops.json")


def load_shops(path=None):
    """shops.json (created from SHOPS_SEED when missing). Returns the whole dict. A file without "custom": true gets
    the seed shops it lacks (the per-town Merchants were added after the first shops.json files were written)."""
    path = path or shops_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            persist.atomic_write_json(path, SHOPS_SEED, ensure_ascii=False, indent=1)
        except OSError:
            return copy.deepcopy(SHOPS_SEED)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        persist.warn_once((path, repr(e)), f"{path} does not parse ({e!r}); using the built-in seed")
        return copy.deepcopy(SHOPS_SEED)
    if not isinstance(data, dict) or not isinstance(data.get("shops"), dict):
        return copy.deepcopy(SHOPS_SEED)
    missing = {k: v for k, v in SHOPS_SEED["shops"].items() if k not in data["shops"]}
    if missing and not data.get("custom"):
        data["shops"].update(copy.deepcopy(missing))
        try:
            persist.atomic_write_json(path, data, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return data


def enabled():
    return os.environ.get("NOBU_SHOPS", "1") == "1"


def parse_cid(v):
    return int(v, 0) & 0xFFFFFFFF if isinstance(v, str) else int(v) & 0xFFFFFFFF


def catalog(data):
    """item id -> the lowest list price over all shops (used for sell prices)."""
    out = {}
    for shop in data["shops"].values():
        for g in shop.get("items", []):
            p = int(g.get("price", 0))
            if p > 0 and (g["id"] not in out or p < out[g["id"]]):
                out[g["id"]] = p
    for k, v in (data.get("prices") or {}).items():
        out[int(k)] = int(v)
    return out


def sell_price(data, shop, it):
    """Mon the shop pays for one unit of backpack item it (0 = it does not buy it)."""
    if it is None:
        return 0
    if it.get("type") == 72 and not shop.get("buys_trade_goods"):
        return 0
    base = catalog(data).get(it["id"])
    rate = float(shop.get("sell_rate", data.get("sell_rate", 0.5)))
    if base is None:
        return int(shop.get("sell_default", data.get("sell_default", 1)))
    return max(1, int(base * rate))


def sell_prices(data, shop, inv):
    """50 u32: index = backpack index (item +0x1e), as 0x2087f0 reads the table."""
    out = [0] * SELL_SLOTS
    for it in inv["items"]:
        i = it.get("idx")
        if isinstance(i, int) and 0 <= i < SELL_SLOTS:
            out[i] = sell_price(data, shop, it)
    return out


# ---------------------------------------------------------------------------- encoders
def event_header(flags=0, eid=0, extra=0, tail=0):
    """0x1eafc0: u32 flags, u16 id, u32 only when flags & 1, u8."""
    out = vce.put_u32(flags) + vce.put_u16(eid)
    if flags & 1:
        out += vce.put_u32(extra)
    return out + vce.put_u8(tail)


def goods_entry(g, extra=0):
    """0x1ee760 wire order: u8 +0, u8 +1, u8 +3, u16 +8, u16 +0xa, u16 +6, u32 +0xc, u8 +2, u8 +4.
    extra = more +6 bits (-> item +0xaa): the scroll refusal bits of hiryu.goods_flags."""
    stock = max(1, min(int(g.get("stock", 99 if g.get("stack", True) else 1)), 0xFF))
    iflags = int(g.get("flags", items.FLAG_STACK if g.get("stack", True) else 0))
    return (vce.put_u8(stock) + vce.put_u8(g.get("b1", 0)) + vce.put_u8(g.get("cat", 0))
            + vce.put_u16(g.get("w8", 0)) + vce.put_u16(g.get("type", 0)) + vce.put_u16(1 | (extra & 0xFFFE))
            + vce.put_u32(g["id"]) + vce.put_u8(0xFF) + vce.put_u8(iflags))


def shop_record(shop, sells, c=None):
    from . import hiryu
    goods = shop.get("items", [])[:MAX_GOODS]
    name = misc.clip_sjis(shop.get("name", "Shop"), NAME_MAX - 1).encode("utf-8")
    w = chars.W()
    w.b += vce.put_bytes(name)
    w.u8(len(goods)).u8(shop.get("mode", 0))
    w.u32s([int(g.get("price", 0)) for g in goods])
    w.u32s(list(sells)[:SELL_SLOTS])
    for g in goods:
        w.b += goods_entry(g, hiryu.goods_flags(c, g))
    w.b += event_header()
    return w.bytes()


def shop_event(event_id, shop, sells, c=None):
    """11 <event id> 01 06 <shop record>."""
    return (vce.put_u8(EVENT) + vce.put_u8(event_id) + vce.put_u8(1) + vce.put_u8(KIND_SHOP)
            + shop_record(shop, sells, c))


def trade_value(buy, index, qty):
    return (BUY if buy else 0) | ((qty & 0xFFF) << 8) | (index & 0xFF)


def encode_event_reply(event_id, answers):
    """What the client sends (0x198a40 / 0x199160 / 0x198ac0). answers = list of (kind, u32) or
    None for the cancel form (n = 0xff)."""
    if answers is None:
        blob = bytes([event_id & 0xFF, NO_RECORDS])
    else:
        blob = bytes([event_id & 0xFF, len(answers)])
        for kind, v in answers:
            blob += vce.put_u8(kind) + vce.put_u32(v)
    return vce.put_u8(EVENT_REPLY) + vce.put_bytes(blob)


def decode_event_reply(payload):
    r = vce.Reader(payload, 1)
    blob = r.bytes(0x400)
    b = vce.Reader(blob)
    d = dict(event=b.u8(), n=b.u8(), answers=[])
    if d["n"] == NO_RECORDS:
        return d
    for _ in range(d["n"]):
        if b.done():
            break
        kind = b.u8()
        if kind != ANSWER_SHOP:                        # other answer kinds are not decoded
            d["answers"].append(dict(kind=kind, raw=b.rest().hex()))
            break
        v = b.u32()
        d["answers"].append(dict(kind=kind, value=v, buy=bool(v & BUY), qty=(v >> 8) & 0xFFF,
                                 index=v & 0xFF))
    return d


def notify_money(money, seq=0):
    """30 06 <seq> <u32 total mon> (parser 0x288b38 -> UI 0x36 -> 0x2099c0)."""
    return vce.put_u8(0x30) + vce.put_u8(6) + vce.put_u16(seq) + vce.put_u32(max(0, int(money)))


def notify_items(recs, seq=0):
    """30 03 <seq> <u8 n> n x item record 0x1ec7f0 (-> UI 0x1b -> message 0xb8). HYPOTHESIS: adds
    or replaces backpack items by their index."""
    return (vce.put_u8(0x30) + vce.put_u8(3) + vce.put_u16(seq) + vce.put_u8(len(recs))
            + b"".join(items.item_record(it) for it in recs))


def notify_counts(pairs, seq=0):
    """30 04 <seq> <u8 n> n x {u8 index, u8 count} (0x1ecd30 -> UI 0x1c -> message 0xb9).
    HYPOTHESIS: new stack size, 0 = gone."""
    return (vce.put_u8(0x30) + vce.put_u8(4) + vce.put_u16(seq) + vce.put_u8(len(pairs))
            + b"".join(vce.put_u8(i & 0xFF) + vce.put_u8(min(n, 0xFF)) for i, n in pairs))


# ---------------------------------------------------------------------------- shop NPCs
def shop_npcs(data):
    """[(shop id, shop, npc cid)] for shops that have a keeper."""
    out = []
    vendors = os.environ.get("NOBU_SHOP_VENDORS", "1") != "0"
    for sid, shop in data["shops"].items():
        n = shop.get("npc")
        if not n or (not vendors and str(n.get("spot", "")).startswith("shop_")):
            continue
        cid = parse_cid(n.get("cid", 0))
        if npc.is_npc_cid(cid):
            out.append((sid, shop, cid))
    return out


def local_keepers(rows, zone, player, any_zone=False):
    """The (id, data, cid) rows whose "zone" is `zone` (or unset). any_zone (the NOBU_*_ANY_ZONE knobs): where no row
    belongs to `zone`, the rows of the player's home town (chars.home_town; Nagoya when it has none) follow the
    player instead, so a test rig outside the towns still sees one set of keepers."""
    here = [r for r in rows if r[1].get("zone") in (None, zone)]
    if here or not any_zone:
        return here
    home = chars.home_town(player) or towns.NAGOYA
    return [r for r in rows if r[1].get("zone") == home]


def keeper(player, shop, sid, cid):
    lk = npc.npc_looks(player, "merchant")
    n = shop["npc"]
    return dict(cid=cid, uid=0, b6b=npc.TALK_ONLY, family=n.get("family", shop.get("name", "Shop")),
                given=n.get("given", ""), looks=lk, level=player.get("level", 1), role="shop",
                shop=sid, text=shop.get("greeting", ""))


def spawn_pushes(ctx):
    """CHAR_INFO pushes for the shopkeepers of the player's zone (appended to world_ready_pushes).
    Each keeper is registered in ctx["npcs"] so npc.py answers its 25 01 target set."""
    player = ctx.get("char")
    if not player or not enabled():
        return []
    data = load_shops()
    zone = chars.char_position(player)[0]
    out = []
    for sid, shop, cid in local_keepers(shop_npcs(data), zone, player, os.environ.get("NOBU_SHOP_ANY_ZONE") == "1"):
        n = shop["npc"]
        k = keeper(player, shop, sid, cid)
        pos = npc.town_spot(player, int(n.get("dx", 0)), int(n.get("dz", 0)), int(n.get("dir", 0)) % 360,
                            role=n.get("spot", "merchant"))
        k["pos"] = list(pos)
        ctx.setdefault("npcs", {})[cid] = k
        out += npc.spawn_pushes(k, pos)
    return out


def next_event_id(ctx):
    eid = ctx.get("event_id", 0) % 0xFE + 1           # 1..254 (0xff is the no-records marker)
    ctx["event_id"] = eid
    return eid


def open_shop(ctx, store, sid, data=None):
    """The shop event for shop sid (sell prices from the current backpack)."""
    data = data or load_shops()
    shop = data["shops"][sid]
    inv = items.inventory(ctx["char"], store)
    eid = next_event_id(ctx)
    ctx["shop_event"] = dict(id=eid, shop=sid)
    return shop_event(eid, shop, sell_prices(data, shop, inv), ctx["char"])


def talk(ctx, store, k):
    out = []
    if k.get("text"):
        out.append(npc.notify_npc_say(k["cid"], k["text"], name=npc.npc_name(k)))
    out.append(open_shop(ctx, store, k["shop"]))
    return out


# ---------------------------------------------------------------------------- trades
def buy(ctx, store, data, shop, index, qty):
    """(ok, text, changed items) for buying qty of shop row index."""
    goods = shop.get("items", [])
    if not 0 <= index < min(len(goods), MAX_GOODS) or qty <= 0:
        return False, "That item is not for sale.", []
    g = goods[index]
    inv = items.inventory(ctx["char"], store)
    cost = int(g.get("price", 0)) * qty
    if int(inv.get("money", 0)) < cost:
        return False, "Not enough money.", []
    stack = g.get("stack", True)
    # plan first (top up existing stacks, then new slots), apply only if everything fits
    left, topups = qty, []
    if stack:
        for it in inv["items"]:
            if left and it["id"] == g["id"] and it.get("flags", 0) & items.FLAG_STACK:
                take = min(items.MAX_STACK - int(it.get("count", 1)), left)
                if take > 0:
                    topups.append((it, take))
                    left -= take
    per = items.MAX_STACK if stack else 1
    need = -(-left // per)
    used = {it.get("idx") for it in inv["items"]}
    free = [i for i in range(items.CAPACITY) if i not in used]
    if need > len(free):
        return False, "Cannot carry any more items.", []
    changed = []
    for it, take in topups:
        it["count"] = int(it.get("count", 1)) + take
        changed.append(it)
    for idx in free[:need]:
        n = min(left, per)
        it = dict(id=g["id"], type=g.get("type", 0), cat=g.get("cat", 0), count=n,
                  flags=items.FLAG_STACK if stack else 0, dur=g.get("dur", 0),
                  dur_max=g.get("dur", 0), idx=idx)
        inv["items"].append(it)
        changed.append(it)
        left -= n
    inv["money"] = int(inv.get("money", 0)) - cost
    items.save(store)
    # by id, not g["name"]: shops.json rows written before the English seed carry the wiki's Japanese name
    return True, f"Bought {items.item_name(g['id'])} x{qty} for {cost} mon.", changed


def sell(ctx, store, data, shop, slot, qty):
    inv = items.inventory(ctx["char"], store)
    it = items.by_index(inv, slot)
    if it is None or it.get("pos") is not None:
        return False, "That item cannot be sold.", [], None
    price = sell_price(data, shop, it)
    have = int(it.get("count", 1))
    qty = have if qty <= 0 or qty > have else qty
    if price <= 0:
        return False, "I do not buy that.", [], None
    it["count"] = have - qty
    if it["count"] <= 0:
        inv["items"].remove(it)
    inv["money"] = int(inv.get("money", 0)) + price * qty
    items.save(store)
    return True, f"Sold {items.item_name(it['id'])} x{qty} for {price * qty} mon.", [it], it


def item_pushes(changed, sold=False):
    if os.environ.get("NOBU_SHOP_ITEM_PUSH", "1") != "1" or not changed:
        return []
    if sold:
        return [notify_counts([(it["idx"], max(0, int(it.get("count", 0)))) for it in changed])]
    return [notify_items(changed)]


def on_reply(payload, ctx, store):
    d = decode_event_reply(payload)
    ev = ctx.get("shop_event")
    if not ev or ev["id"] != d["event"]:
        return []                                    # an event that is not a shop of ours
    ctx["shop_event"] = None
    if d["n"] == NO_RECORDS or not d["answers"] or "value" not in d["answers"][0]:
        return []                                    # window closed without a trade
    a = d["answers"][0]
    if a["index"] == NO_ROW or a["qty"] == NO_QTY:
        return []                                    # answer (-1, -1): left without choosing, no trade
    data = load_shops()
    shop = data["shops"].get(ev["shop"])
    if shop is None or not ctx.get("char"):
        return []
    if a["buy"]:
        ok, text, changed = buy(ctx, store, data, shop, a["index"], a["qty"])
        pushes = item_pushes(changed) if ok else []
    else:
        ok, text, changed, _ = sell(ctx, store, data, shop, a["index"], a["qty"])
        pushes = item_pushes(changed, sold=True) if ok else []
    inv = items.inventory(ctx["char"], store)
    ctx.setdefault("shop_log", []).append((ev["shop"], "buy" if a["buy"] else "sell", a["index"],
                                           a["qty"], ok))
    out = [notify_money(inv.get("money", 0))] + pushes + [misc.notify_system(text)]
    if (os.environ.get("NOBU_SHOP_REOPEN", "1") == "1"
            and (ok or not a["buy"] or 0 <= a["index"] < len(shop.get("items", [])))):
        out.append(open_shop(ctx, store, ev["shop"], data))
    return out


# ---------------------------------------------------------------------------- market (cmd 0x1b)
# Reply parser 0x19aa90: u8 sub (< 0xe), u16 seq, table 0x420fb0; main 0x18b128 -> UI 0x12a.. ->
# waiter messages 0x66.. (request kinds). Subs 2 and 3 always call back; subs 1 and 4..9 read u32
# res first and drop the reply (no callback: the window waits) when res < 0. So every request
# gets res 0 with an empty body. Requests 1b 0e / 0f have no reply parser: not answered. cmd 0x1b
# is the House family: house.py answers it first (world.py); these empty bodies stay the fallback
# with NOBU_HOUSE=0.
MARKET_EMPTY = {
    # 1: res, u32, criteria 0x1aa240 (u16 a, u16 b, u16 c, a x 0x1a9b10, b x 0x1a9d20),
    #    0x1a8780 (u8, str max 0x3d, str max 0x21, u16, u8, u32), 0x1a99d0 (u16 n, u16, n x 0x1a8b90)
    1: (vce.put_u32(0) + vce.put_u32(0) + b"\x00" * 6
        + b"\x00" + b"\x00" + b"\x00" + b"\x00\x00" + b"\x00" + vce.put_u32(0) + b"\x00" * 4),
    2: vce.put_u32(0),
    3: vce.put_u32(0),
    4: vce.put_u32(0) + vce.put_u32(0) + vce.put_u16(0) + vce.put_u8(0),
    5: vce.put_u32(0) + vce.put_u32(0) * 3 + b"\x00",            # + 0x1a9f60 (u32 x3, str max 0x21)
    6: vce.put_u32(0) + vce.put_u32(0),
    7: vce.put_u32(0) * 6,
    8: vce.put_u32(0) * 6,
    9: vce.put_u32(0) + b"\x00\x00",                              # + 0x1aa0c0 (u8 a, u8 b, 0 records)
}
MARKET_NAMES = {1: "open/search", 2: "list (criteria)", 3: "list (criteria)", 4: "page",
                5: "offer (str,u32,u32)", 6: "offer (str,u32,u32)", 7: "offer (str,u32 x4)",
                8: "offer (str,u32 x4)", 9: "status", 0xE: "str", 0xF: "u32"}


def market_reply(payload):
    sub = payload[1]
    body = MARKET_EMPTY.get(sub)
    return None if body is None else bytes(payload[:4]) + body


# ---------------------------------------------------------------------------- prices (23 0a..0c)
# 0x283af0 (from 0x284188): 23 <sub> <seq>, then sub 0xa SCMD_GET_QUOTATION struct 0x1f2930
# (u8, u8, u32, u16 array max 30), sub 0xc SCMD_GET_TRADING_TREND 0x1f2ca0 (u8 + 31 x 0x1f2af0 =
# {str max 0x21, u8, u32, u32, u32}), sub 0xb 0x1f2d60 (u8 n, bytes max n, trend, event header).
# The callback (gp-0x7d44) runs whenever the struct parses. Requests: 23 0a u8 u8, 23 0b -, 23 0c u8.
TREND_EMPTY = b"\x00" + (b"\x00" + b"\x00" + vce.put_u32(0) * 3) * 31
PRICE_EMPTY = {
    0x0A: b"\x00\x00" + vce.put_u32(0) + b"\x00",
    0x0B: b"\x00" + b"\x00" + TREND_EMPTY + event_header(),
    0x0C: TREND_EMPTY,
}
PRICE_NAMES = {0x0A: "SCMD_GET_QUOTATION", 0x0B: "market trend (0x1f2d60)",
               0x0C: "SCMD_GET_TRADING_TREND"}


def price_reply(payload):
    body = PRICE_EMPTY.get(payload[1])
    return None if body is None else bytes(payload[:4]) + body


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Replies for 28 01 on a shopkeeper, 0a event answers, 1b market and 23 0a..0c; else None."""
    if not enabled() or len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if cmd == 0x28 and sub == 1 and len(payload) >= 4:
            k = ctx.get("npcs", {}).get(ctx.get("target"))
            if not k or k.get("role") != "shop" or not ctx.get("char"):
                return None
            if k.get("shop") not in load_shops()["shops"]:
                return None
            ctx.setdefault("actions", []).append(ctx.get("target"))
            return talk(ctx, store, k)
        if cmd == EVENT_REPLY:
            return on_reply(payload, ctx, store)
        if cmd == 0x1B and len(payload) >= 4:
            rep = market_reply(payload)
            return [rep] if rep else []
        if cmd == 0x23 and sub in PRICE_EMPTY and len(payload) >= 4:
            return [price_reply(payload)]
    except vce.WireError:
        return None
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if direction == "C>S" and cmd == EVENT_REPLY:
            d = decode_event_reply(payload)
            if d["n"] == NO_RECORDS:
                return f"EVENT reply id={d['event']} closed"
            parts = []
            for a in d["answers"]:
                if "value" in a:
                    parts.append(f"{'buy' if a['buy'] else 'sell'} index={a['index']} qty={a['qty']}")
                else:
                    parts.append(f"kind {a['kind']} {a['raw']}")
            return f"EVENT reply id={d['event']} n={d['n']} " + "; ".join(parts)
        if direction == "S>C" and cmd == EVENT:
            r = vce.Reader(payload, 1)
            eid, n = r.u8(), r.u8()
            if n == NO_RECORDS:
                return f"EVENT id={eid} (no records)"
            kinds = []
            if n and r.u8() == KIND_SHOP:
                name = r.bytes(0x100).decode("utf-8", "replace")
                kinds.append(f"shop {name!r} goods={r.u8()}")
            return f"EVENT id={eid} n={n} " + ", ".join(kinds)
        if cmd == 0x1B and len(payload) >= 4:
            seq = vce.Reader(payload, 2).u16()
            what = MARKET_NAMES.get(sub, f"sub {sub}")
            return f"MARKET {'reply' if direction == 'S>C' else 'request'} {what} seq={seq}"
        if cmd == 0x23 and sub in PRICE_EMPTY and len(payload) >= 4:
            seq = vce.Reader(payload, 2).u16()
            return f"CMD_23 {PRICE_NAMES[sub]} {'reply' if direction == 'S>C' else 'request'} seq={seq}"
        if direction == "S>C" and cmd == 0x30 and len(payload) >= 4:
            r = vce.Reader(payload, 2)
            seq = r.u16()
            if sub == 6:
                return f"NOTIFY_ITEM money={r.u32()} mon"
            if sub in (3, 4):
                return f"NOTIFY_ITEM {'add' if sub == 3 else 'count'} n={r.u8()}"
    except vce.WireError:
        return None
    return None
