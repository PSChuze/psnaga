"""Zone travel: town gates, castle gates and province checkpoints (C>S 28 02 -> S>C 0B 02 WARPED).

KNOWN = read in client code or data; HYPOTHESIS = chosen, not verified live.

Client side (KNOWN)
  28 02 <u16 seq> is header only (builder 0x1c9190, net command 0x82, posted by 0x2073d0). The ○ handler
  0x62ccf0 sends it from 0x62cc50 when no character action applies and gp-0x70fc (the "object attribute" under
  or next to the player) is >= 0. That attribute is set by the player's collision pass 0x5fdd58 (surface
  attributes 60..99, 0x76, 0x77, 0x7c, 0x7d, 400..599) and by map objects (message 0x14 at 0x605200: door /
  gate halves, attributes 0x6f..0x72 hint "○Use", 0x74 hint "○Open", 0x62cbc0). Nothing identifies the object
  on the wire: the server has to find the gate from the player's last position (05 05).
  The client has no parser for a cmd 0x28 reply; the answer is a push.

  0B 02 is NOTIFY_CODE_CHAR_WARPED (handler 0x189760 logs "Recv: NOTIFY_CHAR: NOTIFY_CODE_CHAR_WARPED: next
  zone=%u" with the area byte): area != 0 and != the current area net+0x57 -> UI event 0x142 (UI reset), the
  world scene is torn down (root task message 0xba kind 2), net state 0x18: net+0x50 = map, net+0x53 / +0x57
  = area; state 0x18 (0x18d068) waits for the old scene, posts {kind 2, scene 4} (World.bin, CMAP of the
  map) and returns to the in-world state 0x17, which sends no 04. So the client reloads exactly like after
  login: world-load burst, then 23 5a, which world.py answers with the spawn pushes at the saved position.
  area == the current area (or 0) -> no reload at all: consecutive zones MUST have different area bytes.

  The area byte is the place index of the map (0x21bad0 map id -> linear index; +1 = WDMMSG 0.146 place
  name): provinces 1..21 keep area == map id, Nagoya 110 -> 31, Nagoya Castle 410 -> 56. Evidence: the
  per-area table at 0x3f0be0 (u16 area -> entry) gives each town and its castle the same entry (22/47 ->
  18, 31/56 -> 13), and 0x1c32c0 classes areas 1..0x89 / 0x8a..0xb3. chars.area_for delegates here.

Server side (this module)
  EXITS: per map, trigger points (door halves / checkpoint gates from OLS0 T1) and the arrival point in the
  destination map (the OLS0 T2 point that sits between the destination gate's doors and its 2xx region,
  the same pattern as the town's first T2 points). 28 02 within `r` of an exit's `at` -> warp():
    save [map, x, y, z, dir] to chars.json now, ctx["zone"] = map, hide the character from the old zone
    (players.on_zone_leave), forget the old zone's NPCs, push 0D 06 "Travelling to ..." + 0B 02 WARPED.
  Until the client reports the new area (05 05 y word) or sends 23 5a, 05 05 frames (still from the old
  field) are swallowed so they cannot overwrite the arrival point.

Env:
  NOBU_ZONES=0            off (28 02 falls through to npc.py, which ignores it; chats unhandled)
  NOBU_ZONES_FILE         JSON list of extra exits (same keys as EXITS), put before the built-in ones
  NOBU_ZONE_RANGE         override every exit's trigger radius (world units)
  NOBU_ZONE_ALL_GATES=0   only the gates with proven pairs (Nagoya gate 2 <-> the field's Nagoya gate,
                          castle gate, each home town's id-matched gates); default 1 also sends Nagoya gates
                          1 and 3 (and the other towns' unmatched gates) to the field
  NOBU_TOWNS=0            only Nagoya's exits (towns.py adds the other 13 clans' home towns)
  NOBU_ZONE_PROVINCES=0   no province checkpoints / border markers (Owari's above + province_gates.json)
  NOBU_ZONE_CMDS=0        no "where" / "warp <map> [x z]" say commands
  NOBU_ZONE_DEBUG=1       answer a 28 02 that matches no exit with a system line (map, position)
  NOBU_ZONE_PENDING_SECS  how long 05 05 frames are ignored after a warp (default 30)
"""
import json
import math
import os
import time

from . import chars, misc, players, social, towns, vce

# English place names by area (WDMMSG 0.146.<area>, wdmmsg.tsv)
PLACE_NAMES = [
    "None", "Echigo", "Kozuke", "Musashi", "Kai", "Shinano", "Sagami", "Suruga", "Totomi", "Mikawa", "Owari",
    "Mino", "Etchu", "Kaga", "Echizen", "Omi", "Ise", "Iga", "Yamashiro", "Settsu", "Yamato", "Kii",
    "Kasugayama", "Maebashi", "Kawagoe", "Kofu", "Matsumoto", "Odawara", "Sunpu", "Hamamatsu", "Okazaki",
    "Nagoya", "Inabayama", "Toyama", "Kanazawa", "Ichijodani", "Odani", "Nagashima", "Iga Village", "Sakyo",
    "Ukyo", "Nara", "Saika Village", "Osaka", "Sakai", "Bandit Town", "Pirate Town", "Kasugayama Castle",
    "Maebashi Castle", "Kawagoe Castle", "Tsutsujigasaki", "Matsumoto Castle", "Odawara Castle",
    "Sunpu Castle", "Hamamatsu Castle", "Okazaki Castle", "Nagoya Castle", "Inabayama Castle",
    "Toyama Castle", "Kanazawa Gobo",
]


def map_index(zone):
    """Client 0x21bad0 (and 0x160fa0): map id -> linear map index, or -1 when out of range."""
    z = int(zone)
    if z >= 0x259:
        i = z - 0x1E3
    elif z >= 0x1F5:
        i = z - 0x18A
    elif z >= 0x191:
        i = z - 0x163 - (1 if z >= 0x1A3 else 0)       # the gap at 419
    elif z >= 0x12D:
        i = z - 0xD5
    elif z >= 0xC9:
        i = z - 0x7D
    elif z >= 0x65:
        i = z - 0x50
    else:
        i = z - 1
    return i if 0 <= i < 0x83 else -1


def area_of(zone):
    """Area byte for a map = its place index (map index + 1); 1 for maps outside the table."""
    i = map_index(zone)
    return i + 1 if i >= 0 else 1


def place_name(zone):
    a = area_of(zone)
    return PLACE_NAMES[a] if a < len(PLACE_NAMES) else f"map {zone}"


# ---------------------------------------------------------------------------- exits at = trigger centre
# (x, z), r = radius; to = destination map; arrive = (x, y, z, dir) there. kind: gate (town / castle
# doors), checkpoint (province gate, T1 link N1/N3), border (T1 link N0 marker). Coordinates: <map> (T1
# rows, T2 points, T4 2xx regions). dir 0: the unit of the location dir field is not decoded (HYPOTHESIS:
# degrees, as for NPC facing).
EXITS = [
    # Nagoya town (CMAP110). Gate 2: doors T1 4..11 (x 102480..103520, z 113000 / 115000), region 212 at
    # z 111092..112500, arrival pair T2 0/1 at z 112594. The Owari field has the only other region 212, at
    # its Nagoya gate (doors T1 9..16 x 109130..110169, z 84349 / 86320; arrival pair T2 at z 86525):
    # same id, same doors -> 212 -> 301 layout. KNOWN (data) that the ids match; HYPOTHESIS that they link.
    dict(id="nagoya-gate2", map=110, at=(103000, 114000), r=1500, to=10, arrive=(110137, 3952, 86527, 0),
         kind="gate", proven=True),
    # Gate 1: doors T1 0..3, 12..15 (x 105480..106520, z 75000 / 77000), region 211, arrival T2 2/3. Gate 3:
    # doors T1 16..27 (x 77500 / 79500, z 107980..109021), region 213, no arrival pair. The Owari field has no
    # region 211 / 213. Gate 1 faces the castle compound on the town's map art (the wiki's Nagoya.jpg, castle at
    # the top) and is where the other 13 towns' castle gates are (castle_gates.json: a 21x gate region): it leads
    # to Nagoya Castle, beside the proven door below. HYPOTHESIS. Gate 3 is sent to the field's Nagoya gate so
    # every gate works (NOBU_ZONE_ALL_GATES=0 turns both off).
    dict(id="nagoya-gate1", map=110, at=(106000, 76000), r=1500, to=410, arrive=(25325, 3200, 19114, 0),
         kind="gate", proven=False),
    dict(id="nagoya-gate3", map=110, at=(78500, 108500), r=1500, to=10, arrive=(110137, 3952, 86527, 0),
         kind="gate", proven=False),
    # Castle gate: the single T1 type-3 object at (111700, 106372) -> Nagoya Castle (map 410, place 0.146.56),
    # arrival castle T2 0 between its gate doors (z 19500 / 21500) and its region 212.
    dict(id="nagoya-castle", map=110, at=(111700, 106372), r=1200, to=410, arrive=(25325, 3200, 19114, 0),
         kind="gate", proven=True),
    dict(id="castle-gate", map=410, at=(26000, 20500), r=1500, to=110, arrive=(111385, 3200, 106579, 0),
         kind="gate", proven=True),
    # Owari field (CMAP010): the Nagoya gate -> town arrival T2 0 (the spawn point).
    dict(id="owari-nagoya", map=10, at=(109650, 85335), r=1500, to=110, arrive=(103713, 3200, 112594, 0),
         kind="gate", proven=True),
    # Province checkpoints (T1 link N1 / N3 pairs, next to region 22x) and border markers (T1 link N0, next
    # to region 24x at the map edge). The side digit of 22x / 24x: 1 low z, 2 high z, 3 low x, 4 high x.
    # Owari: E (link 10..13) -> Mikawa, N (20..23) -> Mino, W (30..33) -> Ise; each arrives at the
    # neighbour's checkpoint on the facing side. The pairing is geography: HYPOTHESIS.
    dict(id="owari-cp-e", map=10, at=(192426, 147904), r=1500, to=9, arrive=(53172, 4278, 73728, 0),
         kind="checkpoint", proven=False),
    dict(id="owari-border-e", map=10, at=(190246, 193129), r=2500, to=9, arrive=(53172, 4278, 73728, 0),
         kind="border", proven=False),
    dict(id="owari-cp-n", map=10, at=(87149, 18026), r=1500, to=11, arrive=(120988, 5519, 155655, 0),
         kind="checkpoint", proven=False),
    dict(id="owari-border-n", map=10, at=(132367, 33087), r=2500, to=11, arrive=(120988, 5519, 155655, 0),
         kind="border", proven=False),
    dict(id="owari-cp-w", map=10, at=(21548, 174306), r=1500, to=16, arrive=(136518, 2903, 26856, 0),
         kind="checkpoint", proven=False),
    dict(id="owari-border-w", map=10, at=(17236, 81167), r=2500, to=16, arrive=(136518, 2903, 26856, 0),
         kind="border", proven=False),
    dict(id="mikawa-cp-w", map=9, at=(52748, 73230), r=1500, to=10, arrive=(191897, 5026, 148050, 0),
         kind="checkpoint", proven=False),
    dict(id="mikawa-border-w", map=9, at=(52435, 84275), r=2500, to=10, arrive=(191897, 5026, 148050, 0),
         kind="border", proven=False),
    dict(id="mino-cp-s", map=11, at=(120407, 155808), r=1500, to=10, arrive=(87055, 4762, 18431, 0),
         kind="checkpoint", proven=False),
    dict(id="mino-border-s", map=11, at=(193832, 176045), r=2500, to=10, arrive=(87055, 4762, 18431, 0),
         kind="border", proven=False),
    dict(id="ise-cp-n", map=16, at=(136705, 27309), r=1500, to=10, arrive=(21523, 5107, 174824, 0),
         kind="checkpoint", proven=False),
    dict(id="ise-border-n", map=16, at=(134247, 14395), r=2500, to=10, arrive=(21523, 5107, 174824, 0),
         kind="border", proven=False),
]


PROVINCE_FILE = os.path.join(misc.assets_dir(), "maps", "province_gates.json")     # built by extract/
_province = None


def province_exits():
    """Checkpoints and border markers of every province field (province_gates.json, which extract/ builds from
    the CMAP T1 checkpoint groups, paired by geography: HYPOTHESIS); none until it has run. Ids already in EXITS (Owari and its neighbours' facing gates,
    written out above) keep the hand-written entry."""
    global _province
    if _province is None:
        try:
            with open(PROVINCE_FILE, encoding="utf-8") as fh:
                rows = json.load(fh).get("exits", [])
        except (OSError, ValueError):
            rows = []
        own = {e["id"] for e in EXITS}
        _province = [dict(e, at=tuple(e["at"]), arrive=tuple(e["arrive"])) for e in rows if e["id"] not in own]
    return _province


def enabled():
    return os.environ.get("NOBU_ZONES", "1") != "0"


def exits():
    """The active exit list: NOBU_ZONES_FILE entries first, then the built-in ones the knobs allow."""
    out = []
    path = os.environ.get("NOBU_ZONES_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for e in json.load(fh):
                out.append(dict(e, at=tuple(e["at"]), arrive=tuple(e["arrive"])))
    for e in EXITS + towns.exits() + province_exits():  # towns.exits: every other clan's home town <-> its field
        if e["kind"] == "gate" and not e["proven"] and os.environ.get("NOBU_ZONE_ALL_GATES", "1") == "0":
            continue
        if e["kind"] in ("checkpoint", "border") and os.environ.get("NOBU_ZONE_PROVINCES", "1") == "0":
            continue
        out.append(e)
    return out


def current_zone(ctx):
    return ctx.get("zone", chars.char_position(ctx.get("char"))[0])


def player_pos(ctx):
    """(x, z) of the last 05 05, else the saved / home position."""
    p = ctx.get("pos")
    if isinstance(p, dict) and "x" in p:
        return p["x"], p["z"]
    _, x, _, z, _ = chars.char_position(ctx.get("char"))
    return x, z


def find_exit(zone, x, z):
    """Nearest exit of map `zone` whose trigger circle contains (x, z), else None."""
    rng = os.environ.get("NOBU_ZONE_RANGE")
    best, bd = None, None
    for e in exits():
        if int(e["map"]) != int(zone):
            continue
        d = math.hypot(e["at"][0] - x, e["at"][1] - z)
        if d <= (int(rng) if rng else e["r"]) and (bd is None or d < bd):
            best, bd = e, d
    return best


# ---------------------------------------------------------------------------- warp
def in_battle(ctx):
    return (ctx.get("battle") or {}).get("phase") in ("setup", "turns", "end")


def save_pos(ctx, store, pos):
    """Write [map, x, y, z, dir] to the character now (chars.json), not throttled."""
    c = ctx.get("char")
    st = store() if callable(store) else store
    hit = st.set_pos(ctx.get("uid", c.get("uid", 0)), c["cid"], pos, min_interval=0) if st else None
    if hit is None:
        c["pos"] = [int(v) for v in pos]


def warp(ctx, store, to, x, y, z, d=0, why=""):
    """Frames that move the player to map `to` at (x, y, z). Another map -> 0B 02 WARPED (full reload);
    the same map -> 0B 08 kind 1 (UI event 0xc moves the local player, KNOWN in chars.notify_location_update)."""
    c = ctx.get("char")
    if not c:
        return []
    zone = current_zone(ctx)
    to, x, y, z, d = int(to), int(x), int(y), int(z), int(d)
    if to == zone:
        save_pos(ctx, store, [to, x, y, z, d])
        ctx["pos"] = dict(x=x, y=y, z=z, dir=d, area=chars.area_for(to), flag=0)
        return [chars.notify_location_update(ctx.get("cid", c["cid"]), x, y, z, d, kind=1)]
    old_area, area = chars.area_for(zone), chars.area_for(to)
    if area == old_area:
        # 0x189760 ignores a WARPED with the current area: the client would never reload
        return [social.notify_system(f"Cannot travel to {place_name(to)}: both maps use area {area} "
                                     "(unset NOBU_AREA)")]
    save_pos(ctx, store, [to, x, y, z, d])
    players.on_zone_leave(ctx)                    # 0B 04 to the old zone's players
    ctx["zone"] = to
    ctx["pos"] = dict(x=x, y=y, z=z, dir=d, area=area, flag=0)
    ctx.pop("npcs", None)                         # the reload drops every entry; 23 5a spawns the new ones
    ctx["target"] = None
    ctx["zone_pending"] = dict(zone=to, area=area, t=time.monotonic())
    ctx.setdefault("warps", []).append(dict(frm=zone, to=to, why=why))
    return [social.notify_system(f"Travelling to {place_name(to)}..."), chars.zone_enter(area, to)]


def use_object(ctx, store):
    """28 02: the nearest exit in range of the player's last position, else nothing."""
    if not ctx.get("char") or in_battle(ctx):
        return []
    zone = current_zone(ctx)
    x, z = player_pos(ctx)
    e = find_exit(zone, x, z)
    ctx["last_use"] = dict(zone=zone, x=x, z=z, exit=e["id"] if e else None)
    if e is None:
        if os.environ.get("NOBU_ZONE_DEBUG") == "1":
            return [social.notify_system(f"No exit here (map {zone}, x {x}, z {z})")]
        return []
    ax, ay, az, ad = e["arrive"]
    return warp(ctx, store, e["to"], ax, ay, az, ad, why=e["id"])


def pending(ctx):
    p = ctx.get("zone_pending")
    if not p:
        return None
    if time.monotonic() - p["t"] > float(os.environ.get("NOBU_ZONE_PENDING_SECS", "30")):
        ctx.pop("zone_pending", None)
        return None
    return p


# ---------------------------------------------------------------------------- chat commands
def command(text, ctx, store):
    """Say-chat commands: "where" (map, area, position, nearest exit) and "warp <map> [x z]"."""
    words = text.strip().split()
    if not words or words[0].lower() not in ("where", "warp", "/where", "/warp"):
        return None
    if not ctx.get("char"):
        return []
    zone = current_zone(ctx)
    if words[0].lower().lstrip("/") == "where":
        x, z = player_pos(ctx)
        e = min((e for e in exits() if int(e["map"]) == zone),
                key=lambda e: math.hypot(e["at"][0] - x, e["at"][1] - z), default=None)
        near = (f"; nearest exit {e['id']} -> {place_name(e['to'])} at "
                f"{int(math.hypot(e['at'][0] - x, e['at'][1] - z))}" if e else "")
        return [social.notify_system(f"{place_name(zone)} (map {zone}, area {chars.area_for(zone)}) "
                                     f"x {x} z {z}{near}")]
    try:
        to = int(words[1])
        if map_index(to) < 0:
            return [social.notify_system(f"warp: map {to} is not a valid map id")]
        if len(words) >= 4:
            x, z = int(words[2]), int(words[3])
            y = 0
        else:
            x, y, z = chars.FIELD_SPAWN.get(to, (None, None, None))
            if x is None:
                arr = next((e["arrive"] for e in exits() if int(e["to"]) == to), None)
                if arr is None:
                    return [social.notify_system(f"warp: no known point in map {to}; use warp {to} <x> <z>")]
                x, y, z = arr[:3]
    except (IndexError, ValueError):
        return [social.notify_system("usage: warp <map> [x z]")]
    return warp(ctx, store, to, x, y, z, 0, why="chat")


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Called first in world.answer. Replies for 28 02 and the zone chat commands; swallows stale 05 05
    during a warp; None for everything else (23 5a is only observed)."""
    if not enabled() or not payload or ctx is None:
        return None
    cmd = payload[0]
    if cmd == 0x28 and len(payload) >= 4 and payload[1] == 2:
        return use_object(ctx, store)
    if cmd == 0x23 and len(payload) >= 4 and payload[1] == 0x5A:
        ctx.pop("zone_pending", None)                # the new field is up; world.py spawns the player
        return None
    if cmd == 5 and len(payload) >= 16 and payload[1] == 5:
        p = pending(ctx)
        if p is None:
            return None
        if chars.decode_move(payload)["area"] == p["area"]:
            ctx.pop("zone_pending", None)            # already reporting from the new field
            return None
        return []                                   # an old-field position: do not store it
    if (cmd == 0x23 and len(payload) >= 4 and payload[1] == 0x08
            and os.environ.get("NOBU_ZONE_CMDS", "1") != "0"):
        try:
            d = social.decode_chat(payload)
        except vce.WireError:
            return None
        if d["channel"] in ("", "s"):
            return command(d["text"], ctx, store)
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    try:
        if direction == "C>S" and payload[0] == 0x28 and payload[1] == 2 and len(payload) >= 4:
            return f"CMD_28 sub 2 (use object: gate / exit) seq={vce.Reader(payload, 2).u16()}"
        if direction == "S>C" and payload[0] == 0x0B and payload[1] == 2 and len(payload) >= 5:
            r = vce.Reader(payload, 2)
            area, zone = r.u8(), r.u16()
            return (f"NOTIFY_CHAR WARPED (zone enter) area={area} zone={zone} ({place_name(zone)}) "
                    f"{r.rest().hex()}")
    except vce.WireError:
        return None
    return None
