"""Clan home towns: which town each clan starts in, its gates to the province field, and where its NPCs stand.

Every per-town system reads this table instead of a Nagoya constant: chars (start town and spawn point), npc /
quests (Guide and Overseer), shops (Merchant), guild (Hall Master), crafting (Craftsman), zones (gate exits),
battle (the town a defeated player is carried to).

Sources
  - home town per clan (KNOWN, client data): WDMMSG 0.146 places 124..137 list the 14 clan seats in clan order
    (STRDAT table 5: 1 Uesugi .. 14 Saika): Kasugayama C. Tsutsujigasaki Odawara C. Sunpu C. Okazaki C. Nagoya
    C. Inabayama C. Kanazawa Gobo Ichijodani C. Odani C. Iga Fort Nijo C. Osaka C. Saika Fort. The town of each
    seat is its castle town (places 22..46 = maps 101..125); Nijo C. is entered from Sakyo (wiki: "Nijo C.
    Sakyo "), so Ashikaga starts in Sakyo (118). Honganji's seat Kanazawa Gobo puts it in Kanazawa (Kaga 13),
    not in its first province (Etchu 12); Hojo's Odawara is in Sagami (6), not Musashi.
  - gates: over the CMAP files (method in its docstring). A town gate and a field gate carrying the same T4 21x
    region id are linked (KNOWN that the ids match in data; that they link is the same HYPOTHESIS as Nagoya's gate 2
    <-> the Owari field). Saika's field gate has no matching id (proven False, paired by geometry). Unpaired town
    gates lead to the field's first gate (proven False), as Nagoya gates 1 and 3 do.
  - spawn point: the arrival point of the town's first linked gate (Nagoya: (103713, 3200, 112594), unchanged).
  - NPC spots: town_spots.json gives a real spot per town and role from the client's map data: overseer = the
    notice board (CMAP region 301), shrine = the temple graveyard (regions 302-308), merchant = the exchange (wiki
    icon on the client's map art), craftsman = Nagoya's forge (region 403). spot() reads it. A role without a spot
    falls back to Nagoya's offsets from the spawn point (npc / quests / shops / guild / crafting data), turned
    with the gate (place(), HYPOTHESIS): the offsets are written for Nagoya, whose linked gate faces inward along
    -z; in a town whose gate faces another way they are rotated by the same angle, and the facing is turned with
    them.

Env:
  NOBU_TOWNS=0       old behaviour: only Nagoya is a town (other clans start in their province field)
  NOBU_TOWN_SPOTS=0  ignore town_spots.json (every NPC on its Nagoya offsets)
  NOBU_CASTLE_GATES=0  no castle gates outside Nagoya (castle_gates.json; unlinked town gates lead to the field)
"""
import json
import math
import os

# gate: (region id, trigger (x, z) in town, inward unit (x, z), arrival (x, y, z) in town,
#        trigger (x, z) in the field or None, arrival (x, y, z) in the field or None, proven)
G = lambda rid, at, inward, arrive, fat=None, farr=None, proven=False: dict(  # noqa: E731
    rid=rid, at=at, inward=inward, arrive=arrive, field_at=fat, field_arrive=farr, proven=proven)

# town map -> data. faction = STRDAT table 5 id; house = the clan (English, strdat_05_factions.tsv);
# food = local goods for the Merchant: [STRDAT 0 id, name, item type (69 food / 55 sake), price in mon, source]
TOWNS = {
    101: dict(key="kasugayama", name="Kasugayama", province=1, faction=1, house="Uesugi", gates=[
        G(211, (42100, 11580), (0, 1), (42559, 3200, 12839), (49733, 231112), (51240, 5070, 230799), True),
        G(212, (23100, 49590), (0, -1), (22637, 3200, 48330)),
        G(214, (53970, 20600), (-1, 0), (51000, 3200, 23500))]),
    104: dict(key="kofu", name="Kofu", province=4, faction=2, house="Takeda", gates=[
        G(211, (26100, 11610), (0, 1), (26785, 3200, 12942)),
        G(214, (47600, 30110), (-1, 0), (46226, 3200, 29438), (79612, 121661), (81006, 3950, 121043), True)]),
    106: dict(key="odawara", name="Odawara", province=6, faction=3, house="Hojo", gates=[
        G(211, (28500, 15000), (0, 1), (27804, 3200, 16445), (54258, 146121), (53627, 3550, 144758), True),
        G(213, (13000, 23500), (1, 0), (14407, 3200, 22943)),
        G(214, (51000, 43500), (-1, 0), (49562, 3200, 44184), (61108, 146461), (62567, 3550, 145904), True)]),
    107: dict(key="sunpu", name="Sunpu", province=7, faction=4, house="Imagawa", gates=[
        G(212, (19100, 67500), (0, -1), (18505, 3200, 66182), (64489, 206110), (65402, 2600, 207662), True),
        G(214, (53600, 24000), (-1, 0), (52221, 3200, 23487), (70423, 202279), (71992, 2600, 201652), True),
        G(215, (37600, 40000), (-1, 0), (36191, 3200, 39377)),
        G(216, (37600, 60000), (-1, 0), (36193, 3200, 59446))]),
    109: dict(key="okazaki", name="Okazaki", province=9, faction=5, house="Tokugawa", gates=[
        G(213, (13000, 40500), (1, 0), (14442, 3200, 39821), (78260, 99875), (77224, 4900, 98588), True),
        G(214, (52000, 19500), (-1, 0), (50557, 3200, 20073), (86903, 104515), (88598, 4900, 104549), True),
        G(215, (53000, 32500), (-1, 0), (51567, 3200, 31896))]),
    110: dict(key="nagoya", name="Nagoya", province=10, faction=6, house="Oda", gates=[
        # exits of Nagoya itself stay in zones.EXITS (ids nagoya-gate1/2/3, nagoya-castle, owari-nagoya)
        G(211, (106000, 76000), (0, 1), (105303, 3200, 77392)),
        G(212, (103000, 114000), (0, -1), (103713, 3200, 112594), (109650, 85335), (110137, 3952, 86527), True),
        G(213, (78500, 108500), (1, 0), (81556, 3200, 108752))]),
    111: dict(key="inabayama", name="Inabayama", province=11, faction=7, house="Saito", gates=[
        G(211, (42600, 24500), (0, 1), (43321, 3200, 25692)),
        G(212, (20600, 56500), (0, -1), (20132, 3200, 55258), (139803, 130030), (139303, 5000, 131322), True)]),
    113: dict(key="kanazawa", name="Kanazawa", province=13, faction=8, house="Honganji", gates=[
        G(213, (13000, 41500), (1, 0), (14356, 3200, 40946), (107815, 235334), (106445, 3900, 235976), True),
        G(214, (33000, 47500), (-1, 0), (31643, 3200, 46937))]),
    114: dict(key="ichijodani", name="Ichijodani", province=14, faction=9, house="Asakura", gates=[
        G(211, (39600, 18600), (0, 1), (40158, 3200, 19950)),
        G(212, (41600, 42600), (0, -1), (42171, 3200, 41241)),
        G(213, (11100, 33100), (1, 0), (12438, 3200, 32557), (94495, 96958), (93132, 4085, 96352), True)]),
    115: dict(key="odani", name="Odani", province=15, faction=10, house="Azai", gates=[
        G(211, (37500, 13000), (0, 1), (36933, 3200, 14345), (145319, 111445), (145936, 3550, 110099), True),
        G(212, (36500, 51000), (0, -1), (35931, 3200, 49652), (145308, 121252), (145957, 3550, 122593), True),
        G(214, (43000, 42500), (-1, 0), (41653, 3200, 41938))]),
    117: dict(key="iga", name="Iga Village", province=17, faction=11, house="Iga", gates=[
        G(211, (22500, 51000), (0, 1), (22941, 3500, 52096), (48979, 68502), (49899, 4650, 69641), True),
        G(213, (25000, 39500), (1, 0), (26088, 3500, 39941)),
        G(214, (45000, 42500), (-1, 0), (43871, 3201, 42936))]),
    118: dict(key="sakyo", name="Sakyo", province=18, faction=12, house="Ashikaga", gates=[
        G(211, (37000, 18794), (0, 1), (37568, 3200, 20114), (80350, 70824), (81011, 5800, 72113), True),
        G(213, (28800, 26994), (1, 0), (30120, 3200, 26438), (86124, 67072), (87375, 5800, 66192), True),
        G(214, (62800, 27294), (-1, 0), (61478, 3200, 26711)),
        G(215, (28800, 43594), (1, 0), (30122, 3200, 43022))]),
    121: dict(key="saika", name="Saika Village", province=21, faction=14, house="Saika", gates=[
        G(214, (49000, 25500), (-1, 0), (47678, 3200, 24924), (49732, 57149), (51048, 3270, 56591), False),
        G(212, (47500, 49000), (0, -1), (46919, 3200, 47680))]),
    122: dict(key="osaka", name="Osaka", province=19, faction=13, house="Miyoshi", gates=[
        G(212, (36000, 52004), (0, -1), (36568, 3200, 50680), (131760, 97822), (130935, 3300, 99104), True),
        G(213, (13500, 40504), (1, 0), (14820, 3200, 39941), (125679, 93981), (124367, 3300, 93431), True),
        G(214, (53500, 39504), (-1, 0), (52175, 3200, 38934), (137357, 93867), (138662, 3300, 94570), True),
        G(211, (39000, 14004), (0, 1), (38436, 3200, 15326))]),
}

# Local goods. Prices: the wiki gives only Kofu's local only table (Korogaki 4, Houtou 616); the rest are DESIGN
# (food 8 mon, sake 20 mon, like the tea house's Dango 4 / Doburoku 15). Ids above 2900 exist in the 2004 STRDAT
# but may have been sold only later.
FOOD = {   # town map -> [(item id, type, price[, source])]
    101: [(300, 69, 8), (2471, 69, 8), (490, 69, 8), (1497, 55, 20)],
    104: [(581, 69, 4, "wiki Kofu local only (4 mon)"), (538, 69, 8),
          (539, 69, 616, "wiki Kofu local only (616 mon)"), (568, 55, 20)],
    106: [(544, 69, 8), (2465, 69, 8), (547, 69, 8), (3743, 69, 8),
          (546, 55, 20)],
    107: [(580, 69, 8), (500, 69, 8), (499, 69, 8), (564, 55, 20)],
    109: [(576, 69, 8), (497, 69, 8), (498, 69, 8), (562, 55, 20)],
    111: [(573, 69, 8), (284, 69, 8), (285, 69, 8)],
    113: [(2992, 69, 8), (2993, 69, 8), (2994, 69, 8), (2995, 55, 20)],
    114: [(2996, 69, 8), (2997, 69, 8), (2998, 69, 8)],
    115: [(3000, 69, 8), (3001, 69, 8), (3002, 69, 8), (3003, 55, 20)],
    117: [(3983, 69, 8), (3984, 69, 8), (3985, 69, 8), (3986, 55, 20)],
    118: [(3987, 69, 8), (3988, 69, 8), (4235, 69, 8), (574, 69, 8),
          (3989, 55, 20)],
    121: [(3990, 69, 8), (3991, 69, 8), (3992, 69, 8), (3993, 55, 20)],
    122: [(3998, 69, 8), (3999, 69, 8), (4000, 69, 8), (4001, 55, 20)],
}
PROVINCE_EN = {1: "Echigo", 4: "Kai", 6: "Sagami", 7: "Suruga", 9: "Mikawa", 10: "Owari", 11: "Mino", 13: "Kaga",
               14: "Echizen", 15: "Omi", 17: "Iga", 18: "Yamashiro", 19: "Settsu", 21: "Kii"}   # zones.PLACE_NAMES

NAGOYA = 110
NPC_FRAME = (0, -1)          # Nagoya's linked gate faces inward along -z: the frame the NPC offsets are written in
FACTION_TOWN = {t["faction"]: m for m, t in TOWNS.items()}


def enabled():
    return os.environ.get("NOBU_TOWNS", "1") != "0"


def town(zone):
    """The town dict of map `zone`, or None (NOBU_TOWNS=0: only Nagoya)."""
    try:
        zone = int(zone)
    except (TypeError, ValueError):
        return None
    if not enabled() and zone != NAGOYA:
        return None
    return TOWNS.get(zone)


def is_town(zone):
    return town(zone) is not None


def towns():
    """{map: town} of the active towns."""
    return {m: t for m, t in TOWNS.items() if enabled() or m == NAGOYA}


def home_town(faction):
    """The start town (map id) of clan `faction`, or None."""
    m = FACTION_TOWN.get(int(faction or 0))
    return m if m is not None and is_town(m) else None


def province_town(prov):
    """The home town of province map `prov` (the town a clan starts in), or None."""
    return next((m for m, t in towns().items() if t["province"] == int(prov)), None)


def name(zone, default=None):
    t = town(zone)
    return t["name"] if t else default


def linked_gate(zone):
    """The first gate of the town that leads to its province field (the spawn gate)."""
    t = town(zone)
    if not t:
        return None
    return next((g for g in t["gates"] if g["field_at"]), t["gates"][0])


def spawn(zone):
    """(x, y, z) of the town's spawn point = the arrival point of its first linked gate."""
    g = linked_gate(zone)
    return tuple(g["arrive"]) if g else None


def place(zone, dx, dz, d=0, anchor=None):
    """(zone, x, y, z, dir) for an NPC written as Nagoya offsets (dx, dz, dir) from the spawn point, turned with the
    town's gate (HYPOTHESIS, see the module doc). anchor: (x, y, z) to use instead of the spawn point."""
    ax, ay, az = anchor or spawn(zone)
    g = linked_gate(zone)
    ux, uz = g["inward"] if g else NPC_FRAME
    rot = complex(ux, uz) / complex(*NPC_FRAME)
    off = complex(dx, dz) * rot
    turn = round(math.degrees(math.atan2(rot.imag, rot.real)))
    return (int(zone), int(round(ax + off.real)), int(ay), int(round(az + off.imag)), int(d + turn) % 360)


SPOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "town_spots.json")
_spots = None


def _load_spots():
    global _spots
    if _spots is None:
        try:
            with open(SPOTS_FILE, encoding="utf-8") as fh:
                _spots = json.load(fh).get("towns", {})
        except (OSError, ValueError):
            _spots = {}
    return _spots


def is_castle(zone):
    """A clan's castle: its home town's map + 300 (zones.EXITS / castle_gates.json lead there)."""
    try:
        return is_town(int(zone) - 300)
    except (TypeError, ValueError):
        return False


def spot(zone, role):
    """(zone, x, y, z, dir) where NPC `role` stands in town or castle `zone` (town_spots.json), or None (no entry,
    neither a town nor a castle, or NOBU_TOWN_SPOTS=0): the caller then uses place()."""
    if not role or os.environ.get("NOBU_TOWN_SPOTS", "1") == "0" or not (is_town(zone) or is_castle(zone)):
        return None
    s = _load_spots().get(str(int(zone)), {}).get(role)
    if not s:
        return None
    x, y, z = s["pos"]
    return (int(zone), int(x), int(y), int(z), int(s.get("dir", 0)) % 360)


def guard_spots(zone):
    """[(role, (zone, x, y, z, dir))] of the town's gate guards (town_spots.json roles guard_<gate region>_<n>), in a
    stable order; [] when the town has none or NOBU_TOWN_SPOTS=0."""
    if os.environ.get("NOBU_TOWN_SPOTS", "1") == "0" or not is_town(zone):
        return []
    roles = sorted(r for r in _load_spots().get(str(int(zone)), {}) if r.startswith("guard_"))
    return [(r, spot(zone, r)) for r in roles]


def field_spawns():
    """{map: (x, y, z)} for chars.FIELD_SPAWN: every active town's spawn point."""
    return {m: spawn(m) for m in towns()}


CASTLE_GATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "castle_gates.json")
_castle_gates = None


def castle_gates():
    """{town map: castle gate data} (castle_gates.json); {} with NOBU_CASTLE_GATES=0."""
    global _castle_gates
    if os.environ.get("NOBU_CASTLE_GATES", "1") == "0":
        return {}
    if _castle_gates is None:
        try:
            with open(CASTLE_GATES_FILE, encoding="utf-8") as fh:
                _castle_gates = {int(k): v for k, v in json.load(fh).get("castles", {}).items()}
        except (OSError, ValueError):
            _castle_gates = {}
    return _castle_gates


def exits():
    """Exit dicts (zones.EXITS shape) for every town except Nagoya, whose exits are written out in zones.EXITS:
    town gate -> field arrival (linked gates; unlinked gates -> the first linked gate's field arrival) and field
    gate -> town arrival; the town's castle gate (castle_gates.json) -> its castle (map + 300) and back."""
    out = []
    cg = castle_gates()
    for m, t in sorted(towns().items()):
        if m == NAGOYA:
            continue
        first = linked_gate(m)
        c = cg.get(m)
        if c:
            out.append(dict(id=f"{t['key']}-castle", map=m, at=tuple(c["town_at"]), r=1500, to=c["castle"],
                            arrive=tuple(c["castle_arrive"]) + (0,), kind="gate", proven=False, town=True))
            out.append(dict(id=f"{t['key']}-castle-gate", map=c["castle"], at=tuple(c["castle_at"]), r=1500, to=m,
                            arrive=tuple(c["town_arrive"]) + (0,), kind="gate", proven=False, town=True))
        for g in t["gates"]:
            if c and g["rid"] == c["town_rid"]:
                continue                                  # leads into the castle, not out to the field
            dest = g if g["field_arrive"] else first
            out.append(dict(id=f"{t['key']}-gate{g['rid']}", map=m, at=tuple(g["at"]), r=1500, to=t["province"],
                            arrive=tuple(dest["field_arrive"]) + (0,), kind="gate", proven=bool(g["proven"]),
                            town=True))
            if g["field_at"]:
                out.append(dict(id=f"{t['key']}-field{g['rid']}", map=t["province"], at=tuple(g["field_at"]), r=1500,
                                to=m, arrive=tuple(g["arrive"]) + (0,), kind="gate", proven=bool(g["proven"]),
                                town=True))
    return out


def goods(zone):
    """The town's local goods as shop item dicts (shops.py), [] for none. name = the English STRDAT 0 name."""
    from . import items
    t = town(zone)
    if not t:
        return []
    page = PROVINCE_EN.get(t["province"], "")
    out = []
    for row in FOOD.get(int(zone), []):
        iid, typ, price = row[:3]
        src = row[3] if len(row) > 3 else f"wiki {page} local food (name); price DESIGN"
        out.append({"id": iid, "name": items.item_name(iid), "name_jp": items.names().get(iid, ("", ""))[0],
                    "type": typ, "price": price, "src": src})
    return out
