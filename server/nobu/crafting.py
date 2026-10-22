"""Production / crafting: the craft window event, recipe data, materials, proficiency.

Addresses. world.answer() / describe() call answer() / describe() here BEFORE shops.py: shops.on_reply
claims every cmd 0x0a frame, so a craft event answer must be taken first (answer() returns None for 0x0a
frames whose event id is not the open craft event, and shops.py then sees them as before).

The client side (as the client reads it)
  S>C cmd 0x11 event record kind 8 = craft object 0x227780 (0xcf8 bytes; archive 0x227850):
      u8 mode (+0xc: 0 = production craft, 1 = adorn, 2 = Enchant Stoneproduction make stone; window
      titles
         UIMSG 2.270 / 2.271 / 2.269, 0x64ebc0), u16 count (+0xe, <= 100),
      bytes (+0x10, max count; per-entry byte, meaning not traced, sent 0),
      count x goods entry 0x1ee760 (+0x74, 16 bytes each; the same 15-byte wire entry as the shop),
      materials 0x1eebe0 (+0x6b4: u8, u8 n, n x goods entry), event header 0x1eafc0.
  The craft window (World.bin 0x64f020..0x6512ff, texts UIMSG 2.269..2.283 via 0x618e20) reads the
  current event record (0x64eb60 = 0x22e820(UI+0x33740)) and answers it with cmd 0x0a:
      cursor on product i (window msg 0x100c7 -> 0x22f330, answer type 0xb -> 0x199410 / 0x22af10):
          `0a <bytes: id n 08 <u32 0x10000000 | i>>`
      make product i after "production " (2.280) (0x100c8 -> 0x22f2a0, type 0xa -> 0x199340 /
          0x22af30):                              `0a <bytes: id n 08 <u32 i>>`
      close (0x64f370 -> 0x22f5c0, type 0x18 -> 0x1994e0 / 0x22af40): `... 08 ffffffff` (event dropped)
  Types 0xa / 0xb only reset the answer slots afterwards (0x22fbe0): the event stays current and the
  window keeps answering it. Materials come from S>C cmd 0x30:
      30 0b <seq> 0x1eebe0 {u8, u8 n, n x goods entry}  -> UI 0x87 -> waiter msg 0xb2; the window
          (subscribed as 0x100c9 at 0x64f074) copies it into the event +0x6b4 and redraws.
          Entry +0 = quantity needed, +0xc = material item id. The client counts the backpack by
          item NAME (0x6505a0: 0x2002d0 / 0x1eebb0 / 0x273ad0) and refuses "make" itself with
          2.277 "Missing materials." when any material is short.
      30 0f <seq> bytes(max 0xd) = 100-bit mask -> UI 0x8b -> 0x227a40 (mode 0 only): bit i set
          clears bit 7 of entry i +0, clear sets it (HYPOTHESIS: bit 7 = not makeable, greyed).
      30 10 <seq> craft object -> UI 0x8c: the current craft event is replaced (refresh).
  Server errors that exist in the client's table (WDMMSG 0.63.10..13): production,
  Bad materials., Invalid crafting data., That item cannot be crafted. How the
  original server was asked for the list (a craft skill use?) was not found; here a
  workshop NPC opens it (DESIGN).

Server design (DESIGN, not the 2004 rules)
  $NOBU_DATA/crafting.json (seeded from CRAFT_SEED): workshops (NPC keepers) and recipes. A recipe
  belongs to a craft skill (STRDAT table 1 id) and to jobs; the list shows the player's job's recipes.
  Making: materials are checked and consumed, a success roll (base rate + proficiency margin), on
  success the product goes into the backpack; crafted equipment carries the maker's name in the item
  record's string field (+0x19, which this client build ignores: 0x2002f0 is empty) and in the saved
  item ("maker"). Proficiency per craft skill in chars.json "crafting" {skill id: points}.

Env:
  NOBU_CRAFTING=0          no workshop NPC, craft frames left to the other modules
  NOBU_CRAFT_FILE          recipe data (default $NOBU_DATA/crafting.json, seeded from CRAFT_SEED)
  NOBU_CRAFT_ALL_JOBS=1    list every recipe regardless of job
  NOBU_CRAFT_MASK=0        do not push the 30 0f makeable mask
  NOBU_CRAFT_REOPEN=1      push a fresh craft event after each attempt (default: refresh with 30 0b)
  NOBU_CRAFT_GRANT=0       no free trial materials on the first talk
  NOBU_CRAFT_ANY_ZONE=1    outside the towns, spawn the home town's workshop keeper
  NOBU_CRAFT_SEED          seed the success roll (tests)
Every clan's home town (nobu/towns.py) has a Craftsman (CRAFT_SEED, _town_workshops).
"""
import copy
import json
import os
import random

from . import persist
from . import chars, items, misc, npc, shops, towns, vce

KIND_CRAFT = 8               # event record kind (0x1a0b00 -> object 0x227780 -> waiter 0x94)
ANSWER_CRAFT = 8             # answer tag of types 0xa / 0xb / 0x18 (0x199340 / 0x199410 / 0x1994e0)
SELECT = 0x10000000          # type 0xb flag (0x22af10): cursor moved to a product
CLOSE = 0xFFFFFFFF           # type 0x18 (0x22af40): window closed
MODE_CRAFT, MODE_ADORN, MODE_STONE = 0, 1, 2
MAX_PRODUCTS = 100           # object holds 100 entries; WDMMSG 0.63.10 when more
MAX_MATERIALS = 100          # 0x1eebe0 holds 100 entries (0x640 bytes copied by 0x64ef1c)
MASK_BYTES = 13              # 30 0f bytes(max 0xd)
NOT_MAKEABLE = 0x80          # entry +0 bit 7 (0x227a40)
SUB_MATERIALS, SUB_MASK, SUB_REFRESH = 0x0B, 0x0F, 0x10

T_KATANA, T_CLUB, T_ARROW, T_HAMAYA, T_MEDICINE = 3, 8, 17, 44, 52
T_MATERIAL, T_LUMBER, T_PLANT, T_MINERAL, T_FOOD = 54, 62, 63, 64, 69

# ---------------------------------------------------------------------------- data Item ids =
# STRDAT table 0 (strdat_00_items.tsv), skills = STRDAT table 1
# (strdat_01_skills.tsv). Recipes: wiki.ohmynobu.net/nol, pages swordsmith (Shinai, Metezashi),
# doctor (Cure Water, Heal Powder), bowyer (Arrow, Hamaya Arrow). CAUTION: those pages describe
# the later system (2006+), not the 2004 job craft skills; the materials and counts are that
# era's, the skill links (2004 skill names in STRDAT 1) and levels are our mapping, the success
# rates / proficiency numbers are DESIGN. Material item types are INVENTED like the shop's.
CRAFT_SEED = {
    "base_rate": 0.75,           # success chance at the recipe's level
    "rate_per_point": 0.01,      # + per proficiency point above the level
    "min_rate": 0.25,
    "max_rate": 0.95,
    "fail_keeps": 0.5,           # on failure this share of each material (rounded down) is kept
    "workshops": {
        "nagoya_workshop": {
            "name": "Workshop",
            "zone": 110,
            "greeting": "The forge, the mortar and the fletching bench are yours. What will you make?",
            "npc": {"cid": "0xfea00010", "family": "Craftsman", "given": "", "dx": -450, "dz": 0,
                    "dir": 90},
        },
    },
    # Free trial materials (NOBU_CRAFT_GRANT): once per character, per job, enough for a few tries.
    "grants": {
        "6": [[502, "Bamboo", 4], [1246, "Polish Grass", 2], [1477, "Soft Charcoal", 4], [1473, "Iron Sand", 4],
              [420, "Lumber", 2]],
        "7": [[1283, "Sansho Berry", 16], [428, "Water", 4], [413, "Fukujuso", 4], [386, "Otogiri", 5]],
        "3": [[502, "Bamboo", 2], [465, "Arrowhead", 2], [419, "Washi Paper", 2], [429, "Pure Water", 5],
              [2235, "Phoenix Bamboo", 5]],
    },
    "recipes": [
        # Smith (job 6) --------------------------------------------------------------------------
        {"key": "shinai", "skill": 505, "skill_name": "Mk Katana 1", "jobs": [6], "level": 0, "exp": 3,
         "product": {"id": 1621, "name": "Shinai", "type": T_KATANA, "cat": 1, "stack": False, "count": 1,
                     "dur": 60},
         "materials": [{"id": 502, "name": "Bamboo", "n": 2, "type": T_LUMBER},
                       {"id": 1246, "name": "Polish Grass", "n": 1, "type": T_MATERIAL}],
         "src": "W swordsmith (lv 1)"},
        {"key": "konbou", "skill": 502, "skill_name": "Mk Club 1", "jobs": [6], "level": 0, "exp": 3,
         "product": {"id": 1636, "name": "Club", "type": T_CLUB, "cat": 1, "stack": False, "count": 1,
                     "dur": 100},
         "materials": [{"id": 1477, "name": "Soft Charcoal", "n": 2, "type": T_MINERAL},
                       {"id": 1473, "name": "Iron Sand", "n": 2, "type": T_MINERAL},
                       {"id": 420, "name": "Lumber", "n": 2, "type": T_LUMBER}],
         "src": "W staff maker (lv 1)"},
        {"key": "metezashi", "skill": 505, "skill_name": "Mk Katana 1", "jobs": [6], "level": 10, "exp": 5,
         "product": {"id": 1611, "name": "Metezashi", "type": T_KATANA, "cat": 1, "stack": False, "count": 1,
                     "dur": 100},
         "materials": [{"id": 442, "name": "Wood Handle", "n": 1, "type": T_MATERIAL},
                       {"id": 1477, "name": "Soft Charcoal", "n": 4, "type": T_MINERAL},
                       {"id": 1473, "name": "Iron Sand", "n": 4, "type": T_MINERAL},
                       {"id": 437, "name": "Whetstone", "n": 1, "type": T_MINERAL}],
         "src": "W swordsmith (no level given; ours 10)"},
        # Physician (job 7) ----------------------------------------------------------------------
        {"key": "jishinsui", "skill": 233, "skill_name": "Mix Elixir I", "jobs": [7], "level": 0, "exp": 2,
         "product": {"id": 693, "name": "Cure Water", "type": T_MEDICINE, "cat": 0, "stack": True, "count": 1},
         "materials": [{"id": 1283, "name": "Sansho Berry", "n": 3, "type": T_PLANT},
                       {"id": 428, "name": "Water", "n": 4, "type": T_FOOD}],
         "src": "W doctor (lv 1)"},
        {"key": "jishinko", "skill": 233, "skill_name": "Mix Elixir I", "jobs": [7], "level": 0, "exp": 4,
         "product": {"id": 201, "name": "Heal Powder", "type": T_MEDICINE, "cat": 0, "stack": True, "count": 1},
         "materials": [{"id": 413, "name": "Fukujuso", "n": 4, "type": T_PLANT},
                       {"id": 1283, "name": "Sansho Berry", "n": 10, "type": T_PLANT},
                       {"id": 386, "name": "Otogiri", "n": 5, "type": T_PLANT}],
         "src": "W doctor (lv 1)"},
        # Priest (job 3) -------------------------------------------------------------------------
        {"key": "ya", "skill": 62, "skill_name": "Make Arrow I", "jobs": [3], "level": 0, "exp": 2,
         "product": {"id": 65, "name": "Arrow", "type": T_ARROW, "cat": 0, "stack": True, "count": 10},
         "materials": [{"id": 502, "name": "Bamboo", "n": 1, "type": T_LUMBER},
                       {"id": 465, "name": "Arrowhead", "n": 1, "type": T_MATERIAL}],
         "src": "W bowyer (Arrow, makes 10)"},
        {"key": "hamaya", "skill": 281, "skill_name": "Ward Arrow I", "jobs": [3], "level": 0, "exp": 4,
         "product": {"id": 68, "name": "Hamaya Arrow", "type": T_HAMAYA, "cat": 0, "stack": True, "count": 10},
         "materials": [{"id": 419, "name": "Washi Paper", "n": 2, "type": T_MATERIAL},
                       {"id": 429, "name": "Pure Water", "n": 5, "type": T_FOOD},
                       {"id": 2235, "name": "Phoenix Bamboo", "n": 5, "type": T_LUMBER}],
         "src": "W bowyer (Hamaya Arrow, makes 10)"},
    ],
}


def _town_workshops():
    """A Craftsman in every home town (towns.py) other than Nagoya, with Nagoya's greeting and offsets (turned with
    the town's gate by npc.town_spot) and a per-town cid (shops.town_cid role 0x10)."""
    base = CRAFT_SEED["workshops"]["nagoya_workshop"]
    return {f"{t['key']}_workshop": dict(copy.deepcopy(base), zone=m,
                                         npc=dict(base["npc"], cid=f"{shops.town_cid(m, 0x10):#x}"))
            for m, t in sorted(towns.TOWNS.items()) if m != towns.NAGOYA}


CRAFT_SEED["workshops"].update(_town_workshops())


def _english_recipes(recipes):
    """Seed recipes: English product / material names by item id and the English STRDAT 1 skill name, the
    Japanese kept as name_jp / skill_name_jp (provenance)."""
    for r in recipes:
        items.english_rows([r["product"]] + r["materials"])
        if "skill_name_jp" not in r:
            r["skill_name_jp"] = r.get("skill_name", "")
            r["skill_name"] = items.skill_name(r["skill"])
    return recipes


_english_recipes(CRAFT_SEED["recipes"])


def enabled():
    return os.environ.get("NOBU_CRAFTING", "1") == "1"


def craft_path():
    return os.environ.get("NOBU_CRAFT_FILE") or os.path.join(misc.data_dir(), "crafting.json")


def load_data(path=None):
    """crafting.json (created from CRAFT_SEED when missing). A file without "custom": true gets the seed workshops
    it lacks (the per-town Craftsmen came after the first crafting.json files)."""
    path = path or craft_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            persist.atomic_write_json(path, CRAFT_SEED, ensure_ascii=False, indent=1)
        except OSError:
            return copy.deepcopy(CRAFT_SEED)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        persist.warn_once((path, repr(e)), f"{path} does not parse ({e!r}); using the built-in seed")
        return copy.deepcopy(CRAFT_SEED)
    if not isinstance(data, dict) or not isinstance(data.get("recipes"), list):
        return copy.deepcopy(CRAFT_SEED)
    have = data.setdefault("workshops", {}) if isinstance(data.get("workshops", {}), dict) else None
    missing = {k: v for k, v in CRAFT_SEED["workshops"].items() if have is not None and k not in have}
    if missing and not data.get("custom"):
        have.update(copy.deepcopy(missing))
        try:
            persist.atomic_write_json(path, data, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return data


def job_of(c):
    return (list(c.get("looks", [])) + [0] * 5)[3]


def recipes_for(data, c):
    """The recipes the character's job may make, in data order (at most MAX_PRODUCTS)."""
    every = os.environ.get("NOBU_CRAFT_ALL_JOBS") == "1"
    job = job_of(c)
    out = [r for r in data["recipes"] if every or job in r.get("jobs", [])]
    return out[:MAX_PRODUCTS]


# ---------------------------------------------------------------------------- proficiency
def proficiency(c, skill):
    return int((c.get("crafting") or {}).get(str(skill), 0))


def add_proficiency(c, skill, pts):
    prof = c.setdefault("crafting", {})
    prof[str(skill)] = int(prof.get(str(skill), 0)) + int(pts)
    return prof[str(skill)]


def success_rate(data, recipe, prof):
    """DESIGN: base at the recipe level, +rate_per_point above it, clamped."""
    margin = prof - int(recipe.get("level", 0))
    rate = float(data.get("base_rate", 0.75)) + margin * float(data.get("rate_per_point", 0.01))
    return max(float(data.get("min_rate", 0.25)), min(float(data.get("max_rate", 0.95)), rate))


def _rng(ctx):
    r = ctx.get("craft_rng")
    if r is None:
        seed = os.environ.get("NOBU_CRAFT_SEED")
        r = random.Random(int(seed)) if seed is not None else random.Random()
        ctx["craft_rng"] = r
    return r


# ---------------------------------------------------------------------------- backpack helpers
def held(inv, item_id):
    return sum(int(it.get("count", 1)) for it in inv["items"] if it["id"] == item_id)


def missing(inv, recipe):
    """[(material, have)] that the backpack lacks."""
    return [(m, held(inv, m["id"])) for m in recipe["materials"] if held(inv, m["id"]) < int(m["n"])]


def makeable(inv, recipe):
    return not missing(inv, recipe)


def take(inv, item_id, n):
    """Remove n of item_id (unequipped stacks, smallest first). Returns [(item, new count)]."""
    out = []
    for it in sorted([i for i in inv["items"] if i["id"] == item_id and i.get("pos") is None],
                     key=lambda i: int(i.get("count", 1))):
        if n <= 0:
            break
        have = int(it.get("count", 1))
        k = min(have, n)
        it["count"] = have - k
        n -= k
        out.append((it, it["count"]))
        if it["count"] <= 0:
            inv["items"].remove(it)       # the index stays a gap until the next 24 01 (as shops.py)
    return out


def give(inv, proto, qty, maker=None):
    """Add qty of proto (a recipe product) to the backpack: top up stacks, then free indexes.
    Returns the changed item dicts, or None when it does not fit (nothing is changed then)."""
    stack = proto.get("stack", True)
    left, topups = qty, []
    if stack:
        for it in inv["items"]:
            if left and it["id"] == proto["id"] and it.get("flags", 0) & items.FLAG_STACK:
                k = min(items.MAX_STACK - int(it.get("count", 1)), left)
                if k > 0:
                    topups.append((it, k))
                    left -= k
    per = items.MAX_STACK if stack else 1
    need = -(-left // per)
    used = {it.get("idx") for it in inv["items"]}
    free = [i for i in range(items.CAPACITY) if i not in used]
    if need > len(free):
        return None
    changed = []
    for it, k in topups:
        it["count"] = int(it.get("count", 1)) + k
        changed.append(it)
    for idx in free[:need]:
        n = min(left, per)
        it = dict(id=proto["id"], type=proto.get("type", 0), cat=proto.get("cat", 0), count=n,
                  flags=items.FLAG_STACK if stack else 0, dur=proto.get("dur", 0),
                  dur_max=proto.get("dur", 0), idx=idx)
        if maker and not stack:
            it["maker"] = maker
            it["label"] = misc.clip_sjis(maker, 0x20)     # record +0x19 string (max 0x21)
        inv["items"].append(it)
        changed.append(it)
        left -= n
    return changed


def maker_name(c):
    return (str(c.get("family", "")) + " " + str(c.get("given", ""))).strip()


# ---------------------------------------------------------------------------- encoders
def product_entry(recipe):
    """Goods entry 0x1ee760 for a product row: +0 = count made (1..0x7f; bit 7 is left to the
    30 0f mask, which is the client's own way to set it), +6 = 1 (self-describing), +2 = 0xff,
    +0xc = item id, +0xa = type, +3 = category."""
    p = recipe["product"]
    n = max(1, min(int(p.get("count", 1)), 0x7F))
    return (vce.put_u8(n) + vce.put_u8(0) + vce.put_u8(p.get("cat", 0)) + vce.put_u16(0)
            + vce.put_u16(p.get("type", 0)) + vce.put_u16(1) + vce.put_u32(p["id"]) + vce.put_u8(0xFF)
            + vce.put_u8(items.FLAG_STACK if p.get("stack", True) else 0))


def material_entry(m):
    """Goods entry for one material: +0 = quantity needed (0x6505a0 divides the backpack count by
    it), +0xc = item id (matched by name), +0xa = type."""
    return (vce.put_u8(max(1, min(int(m["n"]), 0xFF))) + vce.put_u8(0) + vce.put_u8(0) + vce.put_u16(0)
            + vce.put_u16(m.get("type", 0)) + vce.put_u16(1) + vce.put_u32(m["id"]) + vce.put_u8(0xFF)
            + vce.put_u8(items.FLAG_STACK))


def materials_struct(recipe=None):
    """0x1eebe0: u8 (+0, meaning not traced: 0), u8 n, n x goods entry."""
    mats = (recipe or {}).get("materials", [])[:MAX_MATERIALS]
    return vce.put_u8(0) + vce.put_u8(len(mats)) + b"".join(material_entry(m) for m in mats)


def craft_object(recs, mode=MODE_CRAFT, selected=None):
    """0x227850 wire order: u8 mode, u16 count, bytes(count), count x entry, materials, header."""
    recs = recs[:MAX_PRODUCTS]
    return (vce.put_u8(mode) + vce.put_u16(len(recs)) + vce.put_bytes(bytes(len(recs)))
            + b"".join(product_entry(r) for r in recs) + materials_struct(selected)
            + shops.event_header())


def craft_event(event_id, recs):
    """11 <event id> 01 08 <craft object>."""
    return (vce.put_u8(shops.EVENT) + vce.put_u8(event_id) + vce.put_u8(1) + vce.put_u8(KIND_CRAFT)
            + craft_object(recs))


def notify_materials(recipe, seq=0):
    """30 0b <seq> materials struct (-> UI 0x87 -> msg 0xb2 -> craft window 0x100c9)."""
    return vce.put_u8(0x30) + vce.put_u8(SUB_MATERIALS) + vce.put_u16(seq) + materials_struct(recipe)


def mask_bytes(recs, inv):
    out = bytearray(MASK_BYTES)
    for i, r in enumerate(recs[:MAX_PRODUCTS]):
        if makeable(inv, r):
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def notify_mask(recs, inv, seq=0):
    """30 0f <seq> bytes(13): bit i set = product i makeable (-> UI 0x8b -> 0x227a40)."""
    return vce.put_u8(0x30) + vce.put_u8(SUB_MASK) + vce.put_u16(seq) + vce.put_bytes(mask_bytes(recs, inv))


def notify_refresh(recs, seq=0, selected=None):
    """30 10 <seq> craft object (-> UI 0x8c: replaces the current craft event's object)."""
    return (vce.put_u8(0x30) + vce.put_u8(SUB_REFRESH) + vce.put_u16(seq)
            + craft_object(recs, selected=selected))


def answer_value(index, select=False):
    return (SELECT if select else 0) | (index & 0xFF)


def decode_reply(payload):
    """`0a <bytes: id n {08 u32}>` -> dict(event, n, value) (value None when not a craft answer)."""
    r = vce.Reader(payload, 1)
    b = vce.Reader(r.bytes(0x400))
    d = dict(event=b.u8(), n=b.u8(), value=None)
    if d["n"] != shops.NO_RECORDS and d["n"] and not b.done() and b.u8() == ANSWER_CRAFT:
        d["value"] = b.u32()
    return d


# ---------------------------------------------------------------------------- workshop NPCs
def workshop_npcs(data):
    out = []
    for wid, w in (data.get("workshops") or {}).items():
        n = w.get("npc")
        if not n:
            continue
        cid = shops.parse_cid(n.get("cid", 0))
        if npc.is_npc_cid(cid):
            out.append((wid, w, cid))
    return out


def spawn_pushes(ctx):
    """CHAR_INFO pushes for the workshop keepers of the player's zone (after shops.spawn_pushes)."""
    player = ctx.get("char")
    if not player or not enabled():
        return []
    data = load_data()
    zone = chars.char_position(player)[0]
    out = []
    any_zone = os.environ.get("NOBU_CRAFT_ANY_ZONE") == "1"
    for wid, w, cid in shops.local_keepers(workshop_npcs(data), zone, player, any_zone):
        n = w["npc"]
        k = dict(cid=cid, uid=0, b6b=npc.TALK_ONLY, family=n.get("family", w.get("name", "Workshop")),
                 # "craftsman" is not in npc.NPC_STYLES: face 0 / hair 0, unlike the other NPCs
                 given=n.get("given", ""), looks=npc.npc_looks(player, "craftsman"),
                 level=player.get("level", 1), role="workshop", workshop=wid, text=w.get("greeting", ""))
        pos = npc.town_spot(player, int(n.get("dx", 0)), int(n.get("dz", 0)), int(n.get("dir", 0)) % 360,
                            role=n.get("spot", "craftsman"))
        k["pos"] = list(pos)
        ctx.setdefault("npcs", {})[cid] = k
        out += npc.spawn_pushes(k, pos)
    return out


def grant(ctx, store, data):
    """Trial materials once per character (NOBU_CRAFT_GRANT, default on). Returns pushes."""
    c = ctx["char"]
    if os.environ.get("NOBU_CRAFT_GRANT", "1") != "1" or c.get("craft_grant"):
        return []
    mats = (data.get("grants") or {}).get(str(job_of(c)))
    c["craft_grant"] = True
    if not mats:
        items.save(store)
        return []
    inv = items.inventory(c, store)
    types = {m["id"]: m.get("type", T_MATERIAL) for r in data["recipes"] for m in r["materials"]}
    changed = []
    for mid, _name, n in mats:
        got = give(inv, dict(id=mid, type=types.get(mid, T_MATERIAL), stack=True), int(n))
        if got:
            changed += [it for it in got if it not in changed]
    items.save(store)
    names = ", ".join(f"{items.item_name(mid)} x{n}" for mid, _name, n in mats)   # by id: the data's is Japanese
    out = [shops.notify_items(changed)] if changed else []
    return out + [misc.notify_system(f"The craftsman hands you trial materials: {names}.")]


def open_workshop(ctx, store, data=None):
    data = data or load_data()
    c = ctx["char"]
    inv = items.inventory(c, store)
    recs = recipes_for(data, c)
    eid = shops.next_event_id(ctx)
    ctx["craft_event"] = dict(id=eid, keys=[r["key"] for r in recs], selected=None)
    out = [craft_event(eid, recs)]
    if recs and os.environ.get("NOBU_CRAFT_MASK", "1") == "1":
        out.append(notify_mask(recs, inv))
    return out


def talk(ctx, store, k):
    data = load_data()
    out = []
    if k.get("text"):
        out.append(npc.notify_npc_say(k["cid"], k["text"], name=npc.npc_name(k)))
    out += grant(ctx, store, data)
    if not recipes_for(data, ctx["char"]):
        out.append(misc.notify_system("You know no craft that this workshop serves."))
    out += open_workshop(ctx, store, data)
    return out


# ---------------------------------------------------------------------------- crafting
def by_key(data, key):
    return next((r for r in data["recipes"] if r.get("key") == key), None)


def skill_label(recipe):
    """The recipe's craft skill as the client names it (English STRDAT 1); crafting.json files written before the
    English seed carry the Japanese in skill_name."""
    try:
        return items.skill_name(int(recipe["skill"]))
    except (KeyError, TypeError, ValueError):
        return str(recipe.get("skill_name", "craft"))


def make(ctx, store, data, recipe):
    """One attempt. Returns dict(ok, success, text, taken [(item, count)], changed [items])."""
    c = ctx["char"]
    inv = items.inventory(c, store)
    if recipe not in recipes_for(data, c):
        return dict(ok=False, text="That cannot be crafted.", taken=[], changed=[])
    prof = proficiency(c, recipe["skill"])
    if prof < int(recipe.get("level", 0)):
        return dict(ok=False, text=f"You need {skill_label(recipe)} Lv {recipe['level']} to make that.",
                    taken=[], changed=[])
    lack = missing(inv, recipe)
    if lack:
        what = ", ".join(f"{items.item_name(m['id'])} {have}/{m['n']}" for m, have in lack)
        return dict(ok=False, text=f"Missing materials: {what}.", taken=[], changed=[])
    p = recipe["product"]
    # room check before anything is consumed (materials leaving can free a slot, so plan on a copy)
    trial = copy.deepcopy(inv)
    for m in recipe["materials"]:
        take(trial, m["id"], int(m["n"]))
    if give(trial, p, int(p.get("count", 1))) is None:
        return dict(ok=False, text="Cannot carry any more items.", taken=[], changed=[])
    rate = success_rate(data, recipe, prof)
    success = _rng(ctx).random() < rate
    keep = 0.0 if success else float(data.get("fail_keeps", 0.5))
    taken = []
    for m in recipe["materials"]:
        n = int(m["n"])
        taken += take(inv, m["id"], n - int(n * keep))
    changed = []
    maker = maker_name(c)
    if success:
        changed = give(inv, p, int(p.get("count", 1)), maker=maker) or []
        gain = int(recipe.get("exp", 1))
    else:
        gain = max(1, int(recipe.get("exp", 1)) // 3)
    level = add_proficiency(c, recipe["skill"], gain)
    items.save(store)
    name = items.item_name(p["id"])                   # by id: crafting.json files from before carry Japanese names
    count = int(p.get("count", 1))
    made = f"{name} x{count}" if count > 1 else name
    if success:
        text = f"You made {made}"
        text += f" (maker: {maker})." if not p.get("stack", True) else "."
    else:
        text = f"The attempt at {name} failed; some materials were lost."
    text += f" {skill_label(recipe)}: Lv {level} (+{gain})."
    ctx.setdefault("craft_log", []).append((recipe["key"], success, level))
    return dict(ok=True, success=success, text=text, taken=taken, changed=changed, rate=rate)


def on_reply(payload, ctx, store):
    d = decode_reply(payload)
    ev = ctx.get("craft_event")
    if not ev or ev["id"] != d["event"]:
        return None                                   # not ours: shops.py / others decide
    if d["n"] == shops.NO_RECORDS or d["value"] is None or d["value"] == CLOSE:
        ctx["craft_event"] = None
        return []
    v = d["value"]
    index = v & 0xFF
    if not ctx.get("char") or not 0 <= index < len(ev["keys"]):
        return [misc.notify_system("Invalid crafting data.")]
    data = load_data()
    recipe = by_key(data, ev["keys"][index])
    if recipe is None:
        return [misc.notify_system("Invalid crafting data.")]
    if v & SELECT:
        ev["selected"] = index
        return [notify_materials(recipe)]
    res = make(ctx, store, data, recipe)
    out = []
    if res["taken"] and os.environ.get("NOBU_SHOP_ITEM_PUSH", "1") == "1":
        out.append(shops.notify_counts([(it["idx"], max(0, n)) for it, n in res["taken"]]))
    if res["changed"] and os.environ.get("NOBU_SHOP_ITEM_PUSH", "1") == "1":
        out.append(shops.notify_items(res["changed"]))
    out.append(misc.notify_system(res["text"]))
    inv = items.inventory(ctx["char"], store)
    recs = recipes_for(data, ctx["char"])
    if os.environ.get("NOBU_CRAFT_REOPEN") == "1":
        out += open_workshop(ctx, store, data)
    else:
        out.append(notify_materials(recipe))           # the "can make" count follows the backpack
        if os.environ.get("NOBU_CRAFT_MASK", "1") == "1":
            out.append(notify_mask(recs, inv))
    return out


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """28 01 on a workshop keeper, 0a answers to the open craft event; else None."""
    if not enabled() or len(payload) < 2 or not isinstance(ctx, dict):
        return None
    try:
        if payload[0] == 0x28 and payload[1] == 1 and len(payload) >= 4:
            k = ctx.get("npcs", {}).get(ctx.get("target"))
            if not k or k.get("role") != "workshop" or not ctx.get("char"):
                return None
            ctx.setdefault("actions", []).append(ctx.get("target"))
            return talk(ctx, store, k)
        if payload[0] == shops.EVENT_REPLY and ctx.get("craft_event"):
            return on_reply(payload, ctx, store)
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
                return None                             # shops.describe covers the rest
            v = d["value"]
            if v == CLOSE:
                return f"CRAFT reply id={d['event']} close"
            what = "select" if v & SELECT else "make"
            return f"CRAFT reply id={d['event']} {what} index={v & 0xFF}"
        if direction == "S>C" and cmd == shops.EVENT and len(payload) >= 4 and payload[3] == KIND_CRAFT:
            r = vce.Reader(payload, 4)
            mode, n = r.u8(), r.u16()
            return f"EVENT id={payload[1]} n={payload[2]} craft mode={mode} products={n}"
        if direction == "S>C" and cmd == 0x30 and sub in (SUB_MATERIALS, SUB_MASK, SUB_REFRESH):
            r = vce.Reader(payload, 4)
            if sub == SUB_MATERIALS:
                r.u8()
                return f"NOTIFY_ITEM craft materials n={r.u8()}"
            if sub == SUB_MASK:
                return f"NOTIFY_ITEM craft mask {r.bytes(MASK_BYTES).hex()}"
            return "NOTIFY_ITEM craft list refresh"
    except vce.WireError:
        return None
    return None
