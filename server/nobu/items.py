"""Inventory (cmd 0x24): backpack, item info, equip / unequip / drop / sort / use, money.

Addresses. world.answer() / describe() call answer() / describe() here BEFORE misc.py (which only has
the old empty-backpack echoes). answer() returns None when the session has no loaded character, so the
misc.py / chars.WORLDLOAD_EMPTY fallbacks still answer in that case.

Reply parser 0x287270 (u8 sub, u16 seq, table 0x4285e0) -> main 0x289800 -> UI events 0x15.. ->
UI message (0x1fc400) = the request "kind" of the waiter that sent it (seq checked):
  sub 1 GET_BACKPACK      kind 2    (0x206a70)  callback 0x2078b0 -> 0x2081e0 stores the backpack
  sub 2 GET_ITEM_INFO     kind 0    (0x1ff930)  callback 0x1ffb80 -> 0x1ffe30 (extended info)
  sub 3 use item          kind 0x2e (0x206b70)  reply has no body
  sub 4 EQUIP_ITEM        kind 0x2f (0x206ab0)  callback -> 0x208910 -> 0x209870
  sub 5 UNEQUIP_ITEM      kind 0x30 (0x206b30)  callback -> 0x208920 -> 0x2098b0
  sub 6 DROP_ITEM         kind 0x31 (0x206ca0)  callback -> 0x2084e0
  sub 7 SORT_ITEM         kind 0x32 (0x206d20)  callback -> 0x208670 (client-side sort 0x200e30)
  sub 8 GET_ITEM_DISPINFO kind 0x33 (0x1ff970)  callback 0x1ffb40 -> 0x1ffde0

Item record (0x1ec7f0, 0x44 bytes in memory; wire order; -> item object by 0x1ffc70):
  u32 +0x00 item id (STRDAT table 0; 0x200290 -> 0x273f90 name lookup)         -> obj+0x18
  u32 +0x04 ? (0 here)                                                          -> obj+0x38
  u8  +0x0c backpack index (sent back in 24 02/03/04/06; match key 0x1ffc40)     -> obj+0x1e
  u8  +0x0d flags: bit 0 stackable (merge / sort tie), bit 1 "unlimited" (0x1ffd30) -> obj+0x1f
  u16 +0x08 item type -> obj+0x1c
  u8  +0x40 count, low byte (the tail u16 below overrides it)                   -> obj+0x20
  u16 +0x0e use effect: STRDAT table 1 skill id, 0 = none                       -> obj+0x68
      KNOWN: the item window's Use (0x368c80 case 1) asks 0x367f70 (obj+0x68 != 0) and only then
      sends 24 03; with 0 it prints UIMSG 0.281 "This item cannot be used." and sends nothing. The
      info window names the skill under 0.40 "Special" (0x36b844 / 0x36c0e4 -> 0x273ed0). USE_EFFECT.
  u16 +0x0a ?                                                                    -> obj+0xa0
  u8  +0x10 equipment category (0 = not equipment; see POS_CATEGORY)             -> obj+0x33
  u8  +0x11 sort sub-key inside a category (0x200970)                           -> obj+0x3c
  u16 +0x14 job mask: bit j-1 = job j may equip (info +0x24; icons 0x36ade0)   -> obj+0x50
  u8  +0x16 flags, bits 0/1 pick the "Tough" icon (info +0x26; 0x36ac0c)        -> obj+0x52
  u16 +0x12 required level, >= 2 shown as "Lv%d+" (info +0x28; 0x36ae34)        -> obj+0x54
  u8  +0x17 ?                                                                    -> obj+0x34
  u8  +0x18 charges (type 60 containers -> +0x60, category 5 -> +0x63, else +0x64)
  str +0x19 (max 0x21; the client ignores it, 0x2002f0 is an empty function)
  u32 +0x3c sale price "w3c" (Sale Setup shows obj+0xb4; 24 17 in trade.py)    -> obj+0xb4
Backpack reply (sub 1, 0x287390): u32 kan, u32 mon (money = kan*1000 + mon, 0x381f5c / 0x2015a0),
  bytes(max 0xc) equipment: byte p = backpack index worn in position p, 0xff = empty (0x2081e0),
  i8 count, count x record, then only if bytes remain: u16 A (default 50), u16 B (default 99,
  -> S+0x5468, the sort call's third argument), count x u16 full counts (0x1ecca0 -> +0x40).
Position p accepts category POS_CATEGORY[p] (0x200170, table 0x423430).
"""
import csv
import os

from . import vce

# ---------------------------------------------------------------------------- constants
SUBS = {1: "GET_BACKPACK", 2: "GET_ITEM_INFO", 3: "USE_ITEM", 4: "EQUIP_ITEM", 5: "UNEQUIP_ITEM",
        6: "DROP_ITEM", 7: "SORT_ITEM", 8: "GET_ITEM_DISPINFO"}
# KNOWN (0x200170 / table 0x423430): equipment position -> the item category it accepts.
POS_CATEGORY = [1, 2, 3, 4, 5, 5, 6, 7, 9, 8, 0xB, 0xA]
# HYPOTHESIS: category names in UIMSG 0.155..0.167 order (0.161 "Slot" / 0.162 "Wt. Reduce" skipped).
CATEGORY_NAMES = {1: "weapon", 2: "head", 3: "neck", 4: "body", 5: "waist", 6: "bag", 7: "arms",
                  8: "feet", 9: "charm", 0xA: "crest", 0xB: "special"}
POSITIONS = 12
CAPACITY = 50            # client slots (0x2081e0 clears 50 x 0xb8) and the reply's default A
MAX_STACK = 99           # reply default B
FLAG_STACK = 1

# Item types (STRDAT table 3) used below.
T_DAGGER, T_BATON, T_KATANA, T_AXE, T_CLUB = 1, 2, 3, 5, 8
T_HARAATE, T_NINJA_GARB, T_MONK_ROBE, T_VESTMENTS, T_SUIKAN, T_NOSHI = 19, 24, 31, 32, 34, 49
T_MEDICINE, T_FOOD = 52, 69
T_CONTAINER = 60                                   # Container
T_TRADE_GOODS = 72                                 # Trade Goods

# The worn container (Container). KNOWN (0x61bb60 storage Take, 0x382eb8 shop Buy): the client
# counts free backpack room only in the first N slots, N = item obj+0x60 of the item worn in
# equipment position 7 (S+0x5479); obj+0x60 is filled from record +0x18 ("charges") only for item
# type 60. No worn container -> N = 0: "Cannot carry any more items" and nothing is sent. Every
# character therefore wears one. Starter bag: Plain Twin Bag (STRDAT 188), capacity 30 (a beginner
# guide names " Split Bag (30)" as the initial equipment; current-era source, the item exists in the
# 2004 client).
BAG_POS = 7
STARTER_BAG = (188, 30)


def _equip(item_id, itype, cat, dur=None):
    """A fresh piece of equipment: durability from the item data (wiki durability, NOBU_ITEM_WIKI_DUR=0
    -> 100)."""
    if dur is None:
        dur = new_durability(item_id)
    return dict(id=item_id, type=itype, cat=cat, count=1, flags=0, dur=dur, dur_max=dur)


def new_durability(item_id, default=100):
    if os.environ.get("NOBU_ITEM_WIKI_DUR", "1") == "0":
        return default
    d = int((stats_data().get(item_id) or {}).get("dur", 0) or 0)
    return max(1, min(d, 0xFF)) if d else default


def _stack(item_id, itype, count):
    return dict(id=item_id, type=itype, cat=0, count=count, flags=FLAG_STACK, dur=0, dur_max=0)


# Starter kit per job (UIMSG 0.444.x: 1 Samurai, 2 Monk, 3 Priest, 4 Onmyoji, 5 Ninja, 6 Smith,
# 7 Physician). Ids are STRDAT table 0 entries; the type numbers for the clothes are INVENTED
# (the names table has no type column), the categories follow POS_CATEGORY.
STARTER_WEAPON = {
    1: (1605, T_KATANA),     # Kogatana
    2: (1636, T_CLUB),       # Club
    3: (1606, T_BATON),      # Bamboo Rod bamboo staff
    4: (1601, T_DAGGER),     # Knife
    5: (1603, T_DAGGER),     # Aikuchi dagger
    6: (1647, T_AXE),        # Hatchet hand axe
    7: (1602, T_DAGGER),     # Kozuka
}
STARTER_BODY = {
    1: (1671, T_HARAATE),    # Cloth Haraate
    2: (1698, T_MONK_ROBE),  # Monk Robe
    3: (1716, T_VESTMENTS),  # Joe Robe white vestments
    4: (1709, T_SUIKAN),     # Kariginu
    5: (1685, T_NINJA_GARB), # Ragged Ninja Gi ragged ninja garb
    6: (1706, T_NOSHI),      # Commoner Garb commoner's clothes
    7: (1697, T_NOSHI),      # Kosode
}
STARTER_SUPPLIES = [(201, T_MEDICINE, 5),   # Heal Powder healing powder
                    (202, T_MEDICINE, 3),   # Heal Pill healing pill
                    (279, T_FOOD, 3)]       # Dango
STARTER_MONEY = int(os.environ.get("NOBU_START_MONEY", "1500"))   # 1 kan 500 mon
CONSUMABLE_TYPES = {T_MEDICINE, 53, 55, T_FOOD}                    # medicine, poison, sake, food
# Record +0x0e per consumable type: the client offers Use only for a nonzero value (module doc). Skill ids
# are STRDAT table 1 (1.255 Meal, 1.252 Sake, 1.705 Heal); poison has no skill of its own here, so it
# shares Heal's id just to be usable (the server decides the effect). it["w0e"] overrides.
USE_EFFECT = {T_FOOD: 255, 55: 252, T_MEDICINE: 705, 53: 705}


def starter_kit(job):
    """Fresh inventory dict for a character of this job (unknown jobs get the Samurai kit)."""
    wid, wtype = STARTER_WEAPON.get(job, STARTER_WEAPON[1])
    bid, btype = STARTER_BODY.get(job, STARTER_BODY[1])
    weapon = dict(_equip(wid, wtype, 1), pos=0)
    body = dict(_equip(bid, btype, 4), pos=3)
    items = [weapon, body] + [_stack(i, t, n) for i, t, n in STARTER_SUPPLIES] + [starter_bag()]
    inv = dict(money=STARTER_MONEY, items=items)
    renumber(inv)
    return inv


def starter_bag():
    """The worn starter container, built like the rest of the kit (_equip) but with no durability: bags
    are not wear / repair targets (patch notes: "Armor Repair" 、Container), so upkeep's battle wear,
    which skips dur_max 0, can never break or unequip it. Record +0x18 charges = the slot count the
    client uses; capacity = the same number for the item window's Cap. row (info +0x33)."""
    bid, slots = STARTER_BAG
    return dict(_equip(bid, T_CONTAINER, POS_CATEGORY[BAG_POS], dur=0), dur_max=0, charges=slots,
                capacity=slots, pos=BAG_POS)


def worn_container(inv):
    return next((it for it in inv["items"] if it.get("pos") == BAG_POS
                 and int(it.get("type", 0)) == T_CONTAINER and int(it.get("charges", 0)) > 0), None)


def bag_slots(inv):
    """Backpack slots the client uses (the worn container's capacity, at most CAPACITY)."""
    bag = worn_container(inv)
    return min(int(bag["charges"]), CAPACITY) if bag else 0


def ensure_container(c, inv, store=None):
    """Migration for characters created before the starter bag: once per character (flag
    c["starter_bag"]), if nothing is worn in position 7, add the starter bag worn there in a free
    backpack slot. Returns the added item or None. Never runs twice, so an unequipped or sold bag is
    not replaced."""
    if c.get("starter_bag"):
        return None
    if any(it.get("pos") == BAG_POS for it in inv["items"]):
        c["starter_bag"] = True
        save(store)
        return None
    used = {it.get("idx") for it in inv["items"]}
    free = next((i for i in range(CAPACITY) if i not in used), None)
    if free is None or len(inv["items"]) >= CAPACITY:
        return None                                # full backpack: retried on the next 24 01
    bag = dict(starter_bag(), idx=free)
    inv["items"].append(bag)
    c["starter_bag"] = True
    save(store)
    return bag


# ---------------------------------------------------------------------------- item names
_NAMES = None


def text_dir():
    """Where the name tables are: NOBU_TEXT_DIR, else the ones extract/ built from the disc (assets/text, the
    disc's text with our English), else the repository's overlay/text (our English only)."""
    from . import misc
    if os.environ.get("NOBU_TEXT_DIR"):
        return os.environ["NOBU_TEXT_DIR"]
    built = os.path.join(misc.assets_dir(), "text")
    if os.path.isdir(built):
        return built
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "overlay", "text")


def names():
    """STRDAT table 0 names (strdat_00_items.tsv in text_dir()), id -> (jp, en)."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {}
        path = os.environ.get("NOBU_ITEM_NAMES") or os.path.join(text_dir(), "strdat_00_items.tsv")
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    try:
                        _NAMES[int(row["id"].split(".")[1])] = (row.get("jp") or "", row.get("en") or "")
                    except (KeyError, ValueError, IndexError):
                        continue
        except OSError:
            pass
    return _NAMES


def item_name(item_id):
    """The English STRDAT 0 name (what the patched client shows for this id); the Japanese only when the row has
    no English, f"item {id}" when there is no row. Server text names items by id through this, never by a data
    file's "name" (those are Japanese provenance notes)."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return f"item {item_id}"
    jp, en = names().get(item_id, ("", ""))
    return en or jp or f"item {item_id}"


def english_rows(rows):
    """Seed rows {"id", "name": <Japanese>, ...} -> "name" = item_name(id), the Japanese kept as "name_jp" (data
    provenance, like enemies' name / name_jp). Rows that already carry name_jp are left alone. Returns rows."""
    for r in rows:
        if not isinstance(r, dict) or "id" not in r or "name_jp" in r:
            continue
        jp = r.get("name")
        r["name"] = item_name(r["id"])
        if jp and jp != r["name"]:
            r["name_jp"] = jp
    return rows


_TSV = {}


def tsv_names(filename):
    """<filename> in text_dir() as {id: (jp, en)} (ids as written, e.g. "1.505" or "0.147.7"); {} when missing."""
    if filename not in _TSV:
        out = {}
        path = os.path.join(text_dir(), filename)
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    if row.get("id"):
                        out[row["id"]] = (row.get("jp") or "", row.get("en") or "")
        except OSError:
            pass
        _TSV[filename] = out
    return _TSV[filename]


def tsv_name(filename, key, default=""):
    """English of row `key` in <filename>, else its Japanese, else default."""
    jp, en = tsv_names(filename).get(key, ("", ""))
    return en or jp or default


def tsv_jp(filename, key, default=""):
    """The disc's Japanese of row `key` in <filename> (only there once extract/ has run), else default."""
    return tsv_names(filename).get(key, ("", ""))[0] or default


def skill_name(skill_id):
    """The English STRDAT 1 name of skill id (what the client's own skill window shows)."""
    return tsv_name("strdat_01_skills.tsv", f"1.{int(skill_id)}", f"skill {skill_id}")


# ---------------------------------------------------------------------------- item stats itemstats_seed.json:
# 1244 STRDAT 0 ids with the wiki's value (value grade), attack / defence, durability, bonuses (stat bonuses)
# and jobs (job mask), each labelled with its wiki family page. $NOBU_DATA/itemstats.json (or
# NOBU_ITEM_STATS_FILE), when present, is merged over it ({"items": {id: {...}}}) so an admin can correct or
# add items without touching the seed.
STATS_SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "itemstats_seed.json")
_STATS = {"key": None, "data": None}
STAT_KEYS = ("str", "sta", "dex", "int", "cha", "earth", "water", "fire", "wind")
# info struct offsets (KNOWN, item window 0x36a7f0 + copy 0x1ffe30)
INFO_OF = {"life": 0x3E, "ki": 0x40, "str": 0x42, "sta": 0x43, "dex": 0x44, "int": 0x45, "cha": 0x46,
           "earth": 0x47, "water": 0x48, "fire": 0x49, "wind": 0x4A, "delay": 0x56, "accuracy": 0x57,
           "evasion": 0x58, "phys_res": 0x6C, "mag_res": 0x6E}


def _stats_override_path():
    from . import misc
    return os.environ.get("NOBU_ITEM_STATS_FILE") or os.path.join(misc.data_dir(), "itemstats.json")


def stats_data():
    """id -> stats entry (seed + override file). Cached until the override file changes."""
    import json
    over = _stats_override_path()
    try:
        key = (over, os.path.getmtime(over))
    except OSError:
        key = (over, None)
    if _STATS["key"] == key and _STATS["data"] is not None:
        return _STATS["data"]
    data = {}
    try:
        with open(STATS_SEED, encoding="utf-8") as fh:
            data = {int(k): v for k, v in json.load(fh).get("items", {}).items()}
    except (OSError, ValueError):
        data = {}
    if key[1] is not None:
        try:
            with open(over, encoding="utf-8") as fh:
                for k, v in (json.load(fh).get("items") or {}).items():
                    data[int(k)] = dict(data.get(int(k), {}), **v)
        except (OSError, ValueError, AttributeError):
            pass
    _STATS.update(key=key, data=data)
    return data


def stats(it):
    """What the item window shows and battle uses for one item (inventory dict, or a bare id): the data entry
    with the item's own fields winning (an instance may carry "attack", "defence", "mods", "value", "jobs",
    "req_level", "temper", "capacity", "wt_reduce"). Unknown items give zeros and known=False.
    Returns {id, known, attack, defence, value, jobs, req_level, dur, temper, capacity, wt_reduce, mods, page}."""
    if not isinstance(it, dict):
        it = {"id": int(it or 0)}
    base = stats_data().get(int(it.get("id", 0) or 0)) or {}
    mods = dict(base.get("mods") or {})
    mods.update(it.get("mods") or {})

    def pick(k, default=0):
        return int(it[k]) if it.get(k) is not None else int(base.get(k, default) or 0)
    return dict(id=int(it.get("id", 0) or 0), known=bool(base), attack=pick("attack"), defence=pick("defence"),
                value=pick("value"), jobs=pick("jobs"), req_level=pick("req_level"), dur=int(base.get("dur", 0)),
                temper=pick("temper"), capacity=pick("capacity"), wt_reduce=pick("wt_reduce"),
                mods={k: int(v) for k, v in mods.items()}, page=base.get("page", ""))


def _scale(v, env, default):
    try:
        f = float(os.environ.get(env, default))
    except ValueError:
        f = float(default)
    return int(round(v * f))


def combat_bonus(c, store=None, weapon_power=None, default_power=3, bare_hands=1):
    """What the worn equipment adds to battle.player_combat (DESIGN; uses it):
      weapon = the pos-0 weapon's wiki attack x NOBU_ITEM_ATK_SCALE (0.25: Kogatana 32 -> 8, Hatchet 23 -> 6),
      at least 1;
               a weapon without data falls back to weapon_power[type] / default_power (battle's old table);
               bare hands = bare_hands.
      armour = sum over the other worn items of defence x NOBU_ITEM_DEF_SCALE (0.25), items without data 3 for
      the
               body position (3) and 1 elsewhere (battle's old rule); broken items (it["broken"]) add nothing.
      mods   = the summed bonuses {str, sta, dex, int, cha, earth, water, fire, wind, life, ki, ...}.
    NOBU_ITEM_STATS_BATTLE=0 turns the data off (old type table / flat armour, no mods)."""
    weapon_power = weapon_power or {}
    use = os.environ.get("NOBU_ITEM_STATS_BATTLE", "1") != "0"
    weapon, armour, mods = bare_hands, 0, {}
    if not c:
        return dict(weapon=weapon, armour=armour, mods=mods)
    for it in inventory(c, store).get("items", []):
        pos = it.get("pos")
        if pos is None or int(it.get("type", 0) or 0) == T_CONTAINER:
            continue                                # the worn bag is no armour
        st = stats(it) if use else dict(known=False, attack=0, defence=0, mods={})
        if it.get("broken"):
            if pos == 0:
                weapon = bare_hands
            continue
        if pos == 0:
            if st["known"] and st["attack"]:
                weapon = max(1, _scale(st["attack"], "NOBU_ITEM_ATK_SCALE", "0.25"))
            else:
                weapon = weapon_power.get(it.get("type"), default_power)
        elif st["known"] and st["defence"]:
            armour += max(1, _scale(st["defence"], "NOBU_ITEM_DEF_SCALE", "0.25"))
        else:
            armour += 3 if pos == 3 else 1
        for k, v in st["mods"].items():
            mods[k] = mods.get(k, 0) + v
    return dict(weapon=weapon, armour=armour, mods=mods)


# ---------------------------------------------------------------------------- inventory state
def inventory(c, store=None):
    """The character's inventory, created with the job's starter kit on first use (and saved)."""
    inv = c.get("inventory")
    if not isinstance(inv, dict) or "items" not in inv:
        job = (list(c.get("looks", [])) + [0] * 5)[3]
        inv = starter_kit(job)
        c["inventory"] = inv
        c["starter_bag"] = True
        save(store)
    return inv


def save(store):
    st = store() if callable(store) else store
    if st is not None:
        st.save()


def renumber(inv):
    """Indexes = list positions 0..n-1 (the client puts record k in slot k and, after 24 07, renumbers
    the same way, 0x200e30 / 0x200110)."""
    for i, it in enumerate(inv["items"]):
        it["idx"] = i


def by_index(inv, idx):
    for it in inv["items"]:
        if it.get("idx") == idx:
            return it
    return None


def equip_bytes(inv):
    """12 bytes: position p -> index of the item worn there, 0xff = empty."""
    out = [0xFF] * POSITIONS
    for it in inv["items"]:
        p = it.get("pos")
        if p is not None and 0 <= p < POSITIONS:
            out[p] = it["idx"] & 0xFF
    return bytes(out)


def sort_key(it):
    """The client's comparator 0x200970: equipment (category != 0) first by category then the
    +0x11 sub-key, the rest after; then item type, item id, index."""
    cat = it.get("cat", 0)
    return (0 if cat else 1, cat, it.get("sub", 0) if cat else 0, it.get("type", 0), it["id"],
            it.get("idx", 0))


def money_split(money):
    money = max(0, int(money))
    return money // 1000, money % 1000


# ---------------------------------------------------------------------------- encoders
def item_record(it):
    """The 0x44-byte record as written by 0x1ec7f0 (wire order, see the module doc)."""
    count = max(0, int(it.get("count", 1)))
    name = str(it.get("label", "")).encode("utf-8")[:0x20]
    if it.get("jobs") is None or it.get("req_level") is None:
        st = stats(it)                              # +0x14 job mask / +0x12 level from the item data
        it = dict(it, jobs=it.get("jobs", st["jobs"] if jobs_enabled() else 0),
                  req_level=it.get("req_level", st["req_level"]))
    return (vce.put_u32(it["id"]) + vce.put_u32(it.get("serial", 0))
            + vce.put_u8(it.get("idx", 0)) + vce.put_u8(it.get("flags", 0))
            + vce.put_u16(it.get("type", 0)) + vce.put_u8(min(count, 0xFF))
            + vce.put_u16(it.get("w0e", USE_EFFECT.get(int(it.get("type", 0)), 0))) + vce.put_u16(it.get("w0a", 0))
            + vce.put_u8(it.get("cat", 0)) + vce.put_u8(it.get("sub", 0))
            + vce.put_u16(it.get("jobs", 0)) + vce.put_u8(it.get("b16", 0))
            + vce.put_u16(it.get("req_level", 0)) + vce.put_u8(it.get("b17", 0))
            + vce.put_u8(it.get("charges", 0)) + vce.put_bytes(name) + vce.put_u32(it.get("w3c", 0)))


def backpack_reply(payload, inv):
    head = bytes(payload[:4])
    items = inv["items"][:CAPACITY]
    kan, mon = money_split(inv.get("money", 0))
    out = head + vce.put_u32(kan) + vce.put_u32(mon) + vce.put_bytes(equip_bytes(inv))
    out += vce.put_u8(len(items)) + b"".join(item_record(it) for it in items)
    out += vce.put_u16(CAPACITY) + vce.put_u16(MAX_STACK)
    out += b"".join(vce.put_u16(min(int(it.get("count", 1)), 0xFFFF)) for it in items)
    return out


# Extended item info (0x1ecf30; GET_ITEM_INFO reply, DISPINFO after the record). Wire order with the
# client's struct offsets; names HYPOTHESIS except res (+0x00, 0x1ffaf0 tests it as signed).
INFO_FIELDS = (
    [("B", 0x23), ("H", 0x24), ("B", 0x26), ("H", 0x28), ("B", 0x2A)]
    + [("B", o) for o in range(0x2E, 0x3A)]
    + [("H", 0x3E), ("H", 0x40)] + [("b", o) for o in range(0x42, 0x4B)]
    + [("H", 0x4C), ("H", 0x4E), ("H", 0x50), ("h", 0x52), ("B", 0x54)]
    + [("b", o) for o in range(0x55, 0x59)]
    + [("B", 0x59), ("B", 0x5A), ("b", 0x5B), ("B", 0x5C), ("B", 0x5D), ("B", 0x5E), ("H", 0x60),
       ("I", 0x64), ("I", 0x68), ("H", 0x3A), ("H", 0x3C), ("h", 0x6C), ("h", 0x6E), ("B", 0x2B),
       ("H", 0x2C), ("H", 0x62)])


def jobs_enabled():
    return os.environ.get("NOBU_ITEM_JOBS", "1") != "0"


def info_values(it):
    """Info struct offset -> value for one item (the item window rows, KNOWN from 0x1ffe30 + 0x36a7f0; the
    numbers come from stats(it), labelled in itemstats_seed.json). it["info"] {offset: value} wins."""
    vals = dict(it.get("info", {}))
    st = stats(it)
    vals.setdefault(0x24, it.get("jobs", st["jobs"] if jobs_enabled() else 0))   # Can Equip icons (obj+0x50)
    vals.setdefault(0x26, it.get("b16", 0))
    vals.setdefault(0x28, it.get("req_level", st["req_level"]))                   # "Lv%d+" when >= 2
    vals.setdefault(0x2E, min(st["temper"], 15))                                  # Temper n / 15
    vals.setdefault(0x2F, it.get("dur_orig", it.get("dur_max", 0)))
    vals.setdefault(0x30, it.get("dur_max", 0))
    vals.setdefault(0x31, it.get("dur", 0))
    vals.setdefault(0x33, min(st["capacity"], 0xFF))                              # Cap. (shown when > 0)
    vals.setdefault(0x34, min(st["wt_reduce"], 0xFF))                             # Wt. Reduce % (when > 0)
    vals.setdefault(0x35, min(st["value"], 0xFF))                                 # Value
    atk, dfn = min(st["attack"], 0xFF), min(st["defence"], 0xFF)
    vals.setdefault(0x36, atk)                                                    # Attack a / b (obj+0x63/65)
    vals.setdefault(0x38, atk)
    vals.setdefault(0x37, dfn)                                                    # Defense a / b (obj+0x64/66)
    vals.setdefault(0x39, dfn)
    from . import upkeep                        # lazy: upkeep imports items
    vals.setdefault(0x3A, upkeep.info_weight(it))                                 # Weight
    for k, off in INFO_OF.items():                                                # Life, Ki, 9 stats, ...
        if st["mods"].get(k):
            vals.setdefault(off, st["mods"][k])
    return vals


def info_struct(it, res=0):
    """u8 res, str (max 0x11), str (max 0x11), then INFO_FIELDS. +0x24 / +0x26 / +0x28 carry the same
    values as the record's +0x14 / +0x16 / +0x12 (both land in item obj+0x50 / +0x52 / +0x54).
    KNOWN (0x1ffe30, item window 0x36a7f0): +0x2f / +0x30 / +0x31 = original max / max / current
    durability (obj+0x5b / +0x5c / +0x5d), +0x3a = weight (obj+0x5e); the other rows: info_values()."""
    vals = info_values(it) if it else {}
    out = vce.put_u8(res & 0xFF) + vce.put_bytes(b"") + vce.put_bytes(b"")
    for kind, off in INFO_FIELDS:
        v = int(vals.get(off, 0))
        if kind in "Bb":
            out += vce.put_u8(v & 0xFF)
        elif kind in "Hh":
            out += vce.put_u16(v & 0xFFFF)
        else:
            out += vce.put_u32(v & 0xFFFFFFFF)
    return out


# ---------------------------------------------------------------------------- handlers
def _system(text):
    from . import misc                             # lazy: misc is a leaf, keep items import-light
    return misc.notify_system(text)



def _fields(payload):
    return vce.Reader(payload, 4)


def handle(payload, ctx, store):
    sub, head = payload[1], bytes(payload[:4])
    c = ctx["char"]
    inv = inventory(c, store)
    r = _fields(payload)
    if sub == 1:                                    # GET_BACKPACK: fresh, compact indexes
        bag = ensure_container(c, inv, store)       # old characters: worn starter bag (migration)
        renumber(inv)
        out = [backpack_reply(payload, inv)]
        if bag is not None:
            ctx.setdefault("item_log", []).append(("starter_bag", bag["id"]))
            print(f"[items] cid={c.get('cid')} starter bag {bag['id']} added, worn in position {BAG_POS}",
                  flush=True)
            out.append(_system(f"You now carry a {item_name(bag['id'])} ({bag['charges']} slots) as your container."))
        return out
    if sub == 2:                                    # GET_ITEM_INFO u8 index -> info struct
        idx = r.u8() if len(r.rest()) else 0xFF
        it = by_index(inv, idx)
        return [head + info_struct(it, 0 if it else 0xFF)]
    if sub == 3:                                    # use: u8 index, u8 ?, u32 id -> header only
        idx = r.u8() if len(r.rest()) else 0xFF
        if len(r.rest()):
            r.u8()                                  # second u8 (target?), unused
        iid = r.u32() if len(r.rest()) >= 4 else None
        it = by_index(inv, idx)
        if it and (iid is None or it["id"] == iid) and it.get("type") in CONSUMABLE_TYPES:
            it["count"] = int(it.get("count", 1)) - 1
            ctx.setdefault("item_log", []).append(("use", it["id"]))
            if it["count"] <= 0:
                inv["items"].remove(it)             # index stays a gap until the next 24 01
            save(store)
            from . import upkeep                    # food -> satiety, medicine -> Life (2F pushes)
            return [head] + upkeep.on_item_used(ctx, it, store)
        return [head]
    if sub == 4:                                    # EQUIP u8 index, u8 pos -> u8 index, u8 pos
        idx = r.u8() if len(r.rest()) else 0xFF
        pos = r.u8() if len(r.rest()) else 0xFF
        it = by_index(inv, idx)
        if it is None or not 0 <= pos < POSITIONS or it.get("cat", 0) != POS_CATEGORY[pos]:
            return [head + vce.put_u8(0xFF) + vce.put_u8(pos & 0xFF)]
        for other in inv["items"]:
            if other is not it and other.get("pos") == pos:
                other["pos"] = None
        it["pos"] = pos
        save(store)
        return [head + vce.put_u8(idx) + vce.put_u8(pos)]
    if sub == 5:                                    # UNEQUIP u8 pos -> u8 pos
        pos = r.u8() if len(r.rest()) else 0xFF
        for it in inv["items"]:
            if it.get("pos") == pos:
                it["pos"] = None
        save(store)
        return [head + vce.put_u8(pos)]
    if sub == 6:                                    # DROP u8 index, u8 n, u32 id -> u8, u8, u16 n
        idx = r.u8() if len(r.rest()) else 0xFF
        n = r.u8() if len(r.rest()) else 0
        iid = r.u32() if len(r.rest()) >= 4 else None
        it = by_index(inv, idx)
        if it is None or (iid is not None and it["id"] != iid) or it.get("pos") is not None:
            # no slot matches index 0xff -> the client logs it and fetches the backpack again
            return [head + vce.put_u8(0xFF) + vce.put_u8(n) + vce.put_u16(0)]
        have = int(it.get("count", 1))
        take = have if n == 0 or n >= have else n
        it["count"] = have - take
        if it["count"] <= 0:
            inv["items"].remove(it)                 # index stays a gap until the next 24 01
        save(store)
        return [head + vce.put_u8(idx) + vce.put_u8(n) + vce.put_u16(take)]
    if sub == 7:                                    # SORT -> bytes(12) new equipment indexes
        inv["items"].sort(key=sort_key)
        renumber(inv)
        save(store)
        return [head + vce.put_bytes(equip_bytes(inv))]
    if sub == 8:                                    # DISPINFO u32 id -> record + info + u16 count
        iid = r.u32() if len(r.rest()) >= 4 else 0
        it = next((i for i in inv["items"] if i["id"] == iid), None)
        shown = dict(it) if it else dict(id=iid, count=1, idx=0)
        return [head + item_record(shown) + info_struct(shown)
                + vce.put_u16(min(int(shown.get("count", 1)), 0xFFFF))]
    return None


def answer(payload, ctx, store=None):
    """Replies for cmd 0x24 subs 1..8 of a session with a loaded character, else None."""
    if len(payload) < 4 or payload[0] != 0x24 or payload[1] not in SUBS:
        return None
    if not isinstance(ctx, dict) or not ctx.get("char"):
        return None
    try:
        return handle(payload, ctx, store)
    except vce.WireError:
        return None


# ---------------------------------------------------------------------------- logging
# readers for a record after its u32 id (for describe only)
RECORD_REST = [vce.Reader.u32, vce.Reader.u8, vce.Reader.u8, vce.Reader.u16, vce.Reader.u8,
               vce.Reader.u16, vce.Reader.u16, vce.Reader.u8, vce.Reader.u8, vce.Reader.u16,
               vce.Reader.u8, vce.Reader.u16, vce.Reader.u8, vce.Reader.u8,
               lambda r: r.bytes(0x21), vce.Reader.u32]


def describe(payload, direction="C>S"):
    if len(payload) < 4 or payload[0] != 0x24 or payload[1] not in SUBS:
        return None
    sub = payload[1]
    r = vce.Reader(payload, 2)
    try:
        seq = r.u16()
        label = f"ITEM {SUBS[sub]} seq={seq}"
        rest = r.rest()
        if direction == "C>S":
            if sub == 2 and len(rest) >= 1:
                return f"{label} index={rest[0]}"
            if sub in (3, 6) and len(rest) >= 6:
                iid = int.from_bytes(rest[2:6], "big")
                return f"{label} index={rest[0]} n={rest[1]} id={iid} ({item_name(iid)})"
            if sub == 4 and len(rest) >= 2:
                return f"{label} index={rest[0]} pos={rest[1]}"
            if sub == 5 and len(rest) >= 1:
                return f"{label} pos={rest[0]}"
            if sub == 8 and len(rest) >= 4:
                iid = int.from_bytes(rest[:4], "big")
                return f"{label} id={iid} ({item_name(iid)})"
            return f"{label} {rest.hex()}"
        if sub == 1 and len(rest) >= 8:
            kan, mon = r.u32(), r.u32()
            eq = r.bytes(0xC)
            n = r.u8()
            ids = []
            for _ in range(n):
                ids.append(r.u32())
                for get in RECORD_REST:
                    get(r)
            return (f"{label} money={kan}kan{mon}mon equip={eq.hex()} items="
                    + ", ".join(item_name(i) for i in ids))
        return f"{label} {rest.hex()}"
    except vce.WireError:
        return None
