"""Clan war, province state and diplomacy: the read-only windows.

KNOWN below = how the client reads it; HYPOTHESIS = a guess the client does not settle.

Requests (C>S, header `23 <sub> <u16 seq>`, no body; builders take only the seq)
  23 03  SCMMD_PC_GET_POWER_TABLE   builder 0x1c7170, net 0x53, posted directly (not a request kind)
         at world entry (0x18b880) and on NOTIFY 0F 0c (0x18a83c / 0x18a944). Answered today by
         chars.CMD23_EMPTY (18 empty strings + empty bytes); no reader of the result was found.
  23 04  SCMD_GET_DIPLOMATIC_STATE  builder 0x1cc0e0, net 0x8d, request kind 0xd. No poster of kind 0xd
         is known; the reply is still answered here (it only fills the UI copy).
  23 05  SCMD_GET_FRIENDSHIP_STATE  builder 0x1cc190, net 0x8e, request kind 0x54, posted by 0x2071b0,
         called when the Realm window (menu 2.1.2) opens (0x623280); it waits for window
         message 0x54, which only this reply produces (UI 0xe5).

Replies (parser 0x283e30, sub table 0x4283a0; main 0x2869a0, table 0x428510)
  23 04 -> 0x1f18f0 DIPLOMATIC STATE (0x127 bytes) -> UI 0xe4 (0x1d2930): copied to UI+0x1b2b0, then
           window message 0xd.
  23 05 -> 0x1f1bb0 FRIENDSHIP STATE (0x7e bytes)  -> UI 0xe5 (0x1d2a48): copied to UI+0x1b3d8, then
           window message 0x54.

DIPLOMATIC STATE, wire (read half 0x1f19b0) and struct:
  bytes (max 22)            +0x00  province owners: [province 0..21] = faction (STRDAT 5). The same
                                   22 bytes as the 05 03 account bytes (UI 0x11 copies those to
                                   UI+0x1b2b0 too, 0x1cf888), i.e. chars.PROVINCE_OWNERS.
  14 x bytes (row k max k)  +0x16  relation matrix, 15 x 15, only the lower triangle is sent: row k
                                   (k = 1..14) holds relation(k, j) for j < k at +0x16 + 15k + j.
                                   Lookup 0x1f1860(a, b) = [max][min], a == b -> 0.
  bytes (max 15)            +0x118 per-faction flag; 0x1f1b90(f) = byte > 0. The world map draws a
                                   war whose attacker or defender has it set at the castle marker
                                   (restoration war), HYPOTHESIS: "fallen".
  (not on the wire)         +0xf7  u8 n, +0xf8 n x {u8 attacker, u8 defender, u8 battlefield area,
                                   u8 flags}: the war list, filled from NOTIFY 2f 04 (0x1f1a80).
  Relation values (map colours 0x626ca0: 6 entries; legend 0x6267dc 2.68..2.73; WDMMSG 0.149): 0
  Enemy hostile, 1 Hostile rival, 2 Distant cool, 3 Neutral (the map uses 3 when the viewer has
  no clan), 4 Friendly, 5 allied (legend) / Close (WDMMSG 0.149.5).

FRIENDSHIP STATE, wire (read half 0x1f1ca0) and struct:
  bytes (max 15)       +0x00  per faction, no reader found (HYPOTHESIS: the viewer clan's relation)
  u16 array (max 15)   +0x10  per faction war funds: the Realm window's funds pane (0x624f50,
                              0x625178) lights 6 coins of 166 each (0xa6): 996 = full.
  bytes (max 22)       +0x2e  per province, no reader found (HYPOTHESIS)
  bytes (max 22)       +0x44  per province power, bar max 100 (0x628514 / 0x626f50 clamp).
  u16 array (max 18)   +0x5a  per faction (0x1f1db0), read by 0x622410 for the owner of a province;
                              0x622490 enables the Wanted (2.10.2) tab when any is > 0.
                              HYPOTHESIS: the character's wanted points with that clan.

NOTIFY 2f 04 (cmd 0x2f parser 0x284fd0, case 0x285400; main 0x286cb0 -> UI 0xe3 0x1d2678), header
`2f 04 <u16 seq>`, then u8 type:
  type 1 / 3: 0x1f1440 = str text (max 0x3d), u8 +0x4c, u8 +0x4d, u8 n (+0x4e, keep <= 8: the
              reader has no bound, 8 slots before +0x6f), n x {u8 attacker, u8 defender, u8 area,
              u8 flags}, DIPLOMATIC STATE (old, +0x6f), DIPLOMATIC STATE (new, +0x196).
              UI 0xe3 copies the new state and the war list to UI+0x1b2b0. Type 1 also stores the
              whole record at gp-0x73b0 (0x354fc0, a pending diplomacy notice); type 3 does not.
  type 2:     5 x str (0x1f1790, 0xc8 apart) -> UI+0x1b2ac (HYPOTHESIS: notice text lines).
  War-list flags (map 0x628aa0): bit 0 swaps the arrow direction, bit 7 picks the other arrow
  colour pair (HYPOTHESIS: declared vs running).

Battlefields (world map 0x622620, table 0x6a9e70; 0x6224f0, table 0x6a9e00): the war list names a
battlefield by its place id (WDMMSG 0.146 = the zone "area" byte). 31 border fields between two
provinces (areas 74..104) and 14 castle fields for restoration wars (areas 124..137).

Env:
  NOBU_WAR=0            disable (23 04 / 23 05 fall through to the older empty replies)
  NOBU_WAR_DB           state file (default <data dir>/war.json); read at start through persist.load_json
                        (a corrupt file falls back to the newest backup, else the built-in state), written
                        only by the week scheduler (war_week.py, atomic + rotating backup), never when
                        NOBU_WAR_SCHEDULE=0
  NOBU_WAR_PAIR         "attacker,defender" factions of the scheduled war (default "6,5": Oda attacks Tokugawa at
                        Okehazama, area 88 = map 212, a CMAP2 field where the side logic is KNOWN)
  NOBU_WAR_PUSH=0       do not push `2f 04` type 3 (silent diplomatic state + war list) after the spawn
  NOBU_WAR_SKIRMISH_PUSH=0  do not push `2f 30` (the Profile skirmish record) after the spawn
  NOBU_WAR_BOARD=0      no notice-board NPC
  NOBU_WAR_BOARD_CID    its cid (default 0xfea00020); NOBU_WAR_BOARD_ZONE (default 110 Nagoya),
                        NOBU_WAR_BOARD_DX / _DZ offset from the spawn point (default 300 / -300)
  NOBU_WAR_BOARD_ANY_ZONE=1  spawn the board in every zone
  NOBU_WAR_BOARD_MERIT=1     also send the Top Merit / First Spear page (tag 0x0e; client quirk, off by default)
  NOBU_WAR_CHAT=0       no "war" chat command; NOBU_WAR_DEBUG=1 enables "war score a|d <n>" and "war tick"

Notice board (phase 1; KNOWN transport, display HYPOTHESIS)
  28 01 on the board NPC -> NPC line + `11 <event id> <n> {tag, page}..` (the shop event, cmd 0x11):
  tag 0x0f edict (0x228890), tag 0x13 war HUD (0x229600, only while a war is declared / running /
  just decided), tag 0x0d deeds ranking (0x227dc0), optional tag 0x0e top merit (0x228140).
  Every page ends with the event trailer 0x1eafc0 (shops.event_header). The client closes with
  `0a <id> ff` (consumed here). Wire tags are KNOWN from the tag chain 0x1a0688..0x1a1538
  (tag 13 -> type 15 ranking, 14 -> 16, 15 -> 17 edict, 19 -> 20 HUD; tag 6 -> type 7 = the shop).
"""
import json
import os

from . import chars, misc, npc, persist, shops, social, vce, zones

FACTIONS = 15                   # 0..14 (STRDAT 5: 0 none, 1 Uesugi .. 14 Saika); matrix is 15 x 15
PROVINCES = 22                  # 0..21 (STRDAT 4: 0 none, 1 Echigo .. 21 Kii)
WANTED_SLOTS = 18               # friendship +0x5a u16 array max (0x1f1db0 bound)
WAR_SLOTS = 8                   # +0xf8 list: 0x20 bytes
TEXT_MAX = 0x3D                 # 2f 04 text field (SJIS bytes incl. NUL)
FUNDS_FULL = 6 * 0xA6           # 996: six coins of 166 (0x625178)
POWER_MAX = 100                 # Bar max (0x6284f8)

HOSTILE, RIVAL, COOL, NEUTRAL, FRIENDLY, ALLIED = range(6)
RELATION_NAMES = ["hostile", "rival", "cool", "neutral", "friendly", "allied"]

FACTION_NAMES = ["none", "Uesugi", "Takeda", "Hojo", "Imagawa", "Tokugawa", "Oda", "Saito",
                 "Honganji", "Asakura", "Azai", "Iga", "Ashikaga", "Miyoshi", "Saika"]
PROVINCE_NAMES = ["none", "Echigo", "Kozuke", "Musashi", "Kai", "Shinano", "Sagami", "Suruga",
                  "Totomi", "Mikawa", "Owari", "Mino", "Etchu", "Kaga", "Echizen", "Omi", "Ise",
                  "Iga", "Yamashiro", "Settsu-Izumi", "Yamato", "Kii"]

# 0x6a9e70: 31 x {u16 province, u16 province, u16 area}; names WDMMSG 0.146.<area>
BORDER_FIELDS = [
    (1, 5, 74, "Kawanakajima"), (1, 12, 75, "Itoigawa"), (1, 2, 76, "Mikuni Pass"),
    (2, 5, 77, "Torii Pass"), (2, 3, 78, "Tone River"), (3, 4, 79, "Mimase Pass"),
    (3, 6, 80, "Iruma River"), (4, 5, 81, "Uedahara"), (4, 7, 82, "Fuji River"),
    (5, 8, 87, "Nagashino"), (5, 11, 84, "Kiso River"), (6, 7, 85, "Off Izu"),
    (7, 8, 86, "Oi River"), (8, 9, 83, "Mikatagahara"), (9, 10, 88, "Okehazama"),
    (10, 11, 89, "Mt. Komaki"), (10, 16, 90, "Off Kiso"), (11, 14, 91, "Anegawa"),
    (11, 15, 92, "Sekigahara"), (12, 13, 93, "Tedori River"), (13, 14, 94, "Sakai Plain"),
    (14, 15, 95, "Shizugatake"), (15, 16, 96, "Koka"), (15, 18, 97, "Otsu"),
    (16, 17, 98, "Suzuka Pass"), (17, 20, 99, "Mt. Kasagi"), (18, 19, 100, "Mt. Tenno"),
    (18, 20, 101, "Uji"), (19, 20, 102, "Mt. Kongo"), (19, 21, 103, "Takurazaki"),
    (20, 21, 104, "Yoshino"),
]
# 0x6a9e00: 14 x {u16 area, u16 province, u16 map x, u16 map y}: castle fields (restoration wars)
CASTLE_FIELDS = [(124, 1), (125, 4), (126, 6), (127, 7), (128, 9), (129, 10), (130, 11), (131, 13),
                 (132, 14), (133, 15), (134, 17), (135, 18), (136, 19), (137, 21)]


def map_to_place(map_id):
    """0x21bad0: map id -> place index + 1 = the zone area byte (WDMMSG 0.146); zones.area_of."""
    return zones.area_of(map_id)


def place_to_map(area):
    """Inverse of map_to_place over the map ids the install ships (CMAP0..CMAP6); None if none."""
    for m in SHIPPED_MAPS:
        if map_to_place(m) == area:
            return m
    return None


# CMAP files in the install
SHIPPED_MAPS = (list(range(1, 22)) + list(range(101, 126)) + list(range(201, 213))
                + list(range(301, 317)) + list(range(401, 419)) + list(range(420, 432))
                + list(range(500, 505)) + [600, 601] + list(range(611, 617)) + list(range(621, 627)))


def border_field(p1, p2):
    """The battlefield area between two provinces, or None."""
    for a, b, area, _ in BORDER_FIELDS:
        if {a, b} == {p1, p2}:
            return area
    return None


def enabled():
    return os.environ.get("NOBU_WAR", "1") != "0"


# ---------------------------------------------------------------------------- state
class WarState:
    """The world's clan state. Read-only in this phase: seeded from chars.PROVINCE_OWNERS, a
    scheduled war pair and optional JSON overrides; nothing here writes to disk."""

    def __init__(self, data=None):
        data = data or {}
        owners = list(data.get("owners", chars.PROVINCE_OWNERS))
        self.owners = (owners + [0] * PROVINCES)[:PROVINCES]
        self.relations = [[NEUTRAL] * FACTIONS for _ in range(FACTIONS)]
        for i in range(FACTIONS):
            self.relations[i][i] = 0
        self.funds = (list(data.get("funds", [0] + [FUNDS_FULL // 2] * (FACTIONS - 1)))
                      + [0] * FACTIONS)[:FACTIONS]
        self.power = (list(data.get("power", [0] + [POWER_MAX] * (PROVINCES - 1)))
                      + [0] * PROVINCES)[:PROVINCES]
        self.wars = []
        self.week = dict(data.get("week") or {})
        self.last_war = dict(data.get("last_war") or {})
        self.deeds = {str(k): int(v) for k, v in (data.get("deeds") or {}).items()}
        self.history = list(data.get("history") or [])[-20:]
        for a, b, rel in data.get("relations", []):
            self.set_relation(a, b, rel)
        pair = data.get("war_pair")
        if pair is None:
            env = os.environ.get("NOBU_WAR_PAIR", "6,5")
            pair = [int(x) for x in env.split(",")] if env else []
        self.pair = [int(pair[0]), int(pair[1])] if len(pair) == 2 else []
        self.area = data.get("war_area")
        if self.pair:
            self.declare(self.pair[0], self.pair[1], self.area, data.get("war_flags", 0))
            self.area = self.wars[-1][2]
        if "wars" in data:                                  # the scheduler's saved list wins
            self.wars = [tuple(int(v) for v in w)[:4] for w in data["wars"]][:WAR_SLOTS]

    def to_data(self):
        """JSON form (war.json)."""
        rel = [[a, b, self.relations[a][b]] for a in range(1, FACTIONS) for b in range(1, a)
               if self.relations[a][b] != NEUTRAL]
        return dict(owners=list(self.owners), funds=list(self.funds), power=list(self.power),
                    relations=rel, war_pair=list(self.pair), war_area=self.area,
                    wars=[list(w) for w in self.wars], week=self.week, last_war=self.last_war,
                    deeds=self.deeds, history=self.history[-20:])

    def field_provinces(self, area=None):
        """(attacker province, defender province) of the war field (border table), else (0, 0)."""
        area = self.area if area is None else area
        for p, q, a, _ in BORDER_FIELDS:
            if a == area and self.pair:
                att, dfn = self.pair
                if self.owners[p] == att and self.owners[q] == dfn:
                    return p, q
                if self.owners[q] == att and self.owners[p] == dfn:
                    return q, p
                return p, q
        return 0, 0

    def set_relation(self, a, b, rel):
        if 0 <= a < FACTIONS and 0 <= b < FACTIONS and a != b:
            self.relations[a][b] = self.relations[b][a] = rel

    def relation(self, a, b):
        """0x1f1860: 0 for a == b or out of range, else the matrix entry."""
        if not (0 <= a < FACTIONS and 0 <= b < FACTIONS) or a == b:
            return 0
        return self.relations[max(a, b)][min(a, b)]

    def provinces_of(self, f):
        return [p for p in range(1, PROVINCES) if self.owners[p] == f]

    def fallen(self, f):
        return 1 if f and not self.provinces_of(f) else 0

    def declare(self, attacker, defender, area=None, flags=0):
        """Add a war to the list (both sides become hostile). area defaults to the border field
        between a province of each side, else the defender's castle field."""
        if area is None:
            for p in self.provinces_of(attacker):
                for q in self.provinces_of(defender):
                    area = area or border_field(p, q)
        if area is None:
            home = [a for a, p in CASTLE_FIELDS if self.owners[p] == defender]
            area = home[0] if home else 0
        self.set_relation(attacker, defender, HOSTILE)
        self.wars = (self.wars + [(attacker, defender, area, flags)])[:WAR_SLOTS]

    # --- encoders -----------------------------------------------------------------------
    def diplomatic_state(self):
        """0x1f18f0 wire form (read half 0x1f19b0)."""
        out = vce.put_bytes(bytes(o & 0xFF for o in self.owners))
        for k in range(1, FACTIONS):
            out += vce.put_bytes(bytes(self.relations[k][j] for j in range(k)))
        out += vce.put_bytes(bytes(self.fallen(f) for f in range(FACTIONS)))
        return out

    def friendship_state(self, viewer=0, wanted=None):
        """0x1f1bb0 wire form (read half 0x1f1ca0)."""
        wanted = (list(wanted or []) + [0] * WANTED_SLOTS)[:WANTED_SLOTS]
        out = vce.put_bytes(bytes(self.relation(viewer, f) if viewer else NEUTRAL
                                  for f in range(FACTIONS)))
        out += u16_array(self.funds)
        out += vce.put_bytes(bytes(PROVINCES))                       # +0x2e: unknown, zeros
        out += vce.put_bytes(bytes(max(0, min(POWER_MAX, p)) for p in self.power))
        out += u16_array(wanted)
        return out


def u16_array(vals):
    """u16 array field (0x129140): length header = count, then big-endian u16s."""
    vals = list(vals)
    return vce.put_len(len(vals)) + b"".join(vce.put_u16(v) for v in vals)


_STATE = None


def state():
    global _STATE
    if _STATE is None:
        path = db_path()
        data = None
        if path:
            try:                                           # corrupt file -> newest good backup (persist)
                data = persist.load_json(path, default=None, validate=lambda d: isinstance(d, dict))
            except persist.PersistError as e:
                persist.warn_once(("war-load", path), f"{e}; using the built-in war state")
                data = None
        _STATE = WarState(data)
    return _STATE


def reset_state(data=None):
    """Tests / reload: replace the module state."""
    global _STATE
    _STATE = WarState(data) if data is not None else None
    return _STATE


def db_path():
    return os.environ.get("NOBU_WAR_DB") or os.path.join(misc.data_dir(), "war.json")


def save(st=None):
    """Write war.json atomically (persist.atomic_write_json; the scheduler only) and keep a rotating
    backup (tag auto, deduplicated). Errors are logged: the game goes on."""
    st = st or state()
    path = db_path()
    try:
        text = persist.atomic_write_json(path, st.to_data())
        persist.backup_text(path, text, tag="auto")
        return True
    except (OSError, TypeError, ValueError) as e:
        persist.warn_once(("war-save", path), f"cannot write {path}: {e!r}")
        return False


def char_faction(c):
    return (list((c or {}).get("looks", [])) + [0] * 5)[4]


# ---------------------------------------------------------------------------- messages
def diplomatic_reply(seq, st=None):
    st = st or state()
    return vce.put_u8(0x23) + vce.put_u8(4) + vce.put_u16(seq) + st.diplomatic_state()


def friendship_reply(seq, st=None, viewer=0, wanted=None):
    st = st or state()
    return vce.put_u8(0x23) + vce.put_u8(5) + vce.put_u16(seq) + st.friendship_state(viewer, wanted)


def notify_diplomacy(st=None, kind=3, text="", old=None, b4c=0, b4d=0, seq=0):
    """NOTIFY 2f 04 type 1 / 3: text, two bytes, the war list, old state, new state."""
    st = st or state()
    wars = st.wars[:WAR_SLOTS]
    out = vce.put_u8(0x2F) + vce.put_u8(4) + vce.put_u16(seq) + vce.put_u8(kind)
    out += vce.put_bytes(text.encode("utf-8")) + vce.put_u8(b4c) + vce.put_u8(b4d)
    out += vce.put_u8(len(wars))
    for w in wars:
        out += bytes(v & 0xFF for v in w)
    out += (old or st).diplomatic_state() + st.diplomatic_state()
    return out


# ---------------------------------------------------------------------------- pages (cmd 0x11)
TAG_RANKING, TAG_MERIT, TAG_EDICT, TAG_HUD = 0x0D, 0x0E, 0x0F, 0x13
TAG_NAMES = {TAG_RANKING: "ranking", TAG_MERIT: "top merit", TAG_EDICT: "edict", TAG_HUD: "war HUD"}
HUD_PREPARING, HUD_OVER = 1, 2          # +0x38 bit 0 / bit 1 (neither = a round is running)
RANK_ROWS = 20
NAME_MAX = 0x21


def _name(text):
    return vce.put_bytes(misc.clip_sjis(text or "", NAME_MAX - 1).encode("utf-8"))


def edict_page(own, last=(0, 0, 0), current=(0, 0, 0), nxt=(0, 0, 0), watch=0, foster=0,
               build=0, build_off=0):
    """Tag 0x0f (reader 0x228890; draw 0x653060). last = (result 0 draw / 1 win / 2 loss, flag
    +0x11 restored-or-fell, place); current = (we attack, place, enemy); nxt = (we attack, place,
    enemy). Wire: u32 +0xc, u8 +0x10 +0x11 +0x12, u32 +0x14, u8 +0x18 +0x19 +0x1a, u32 +0x1c,
    u8 +0x20 +0x21, u32 +0x24 +0x28 +0x2c +0x30, u8 +0x34..+0x37, trailer."""
    out = vce.put_u32(own) + bytes(v & 0xFF for v in last) + vce.put_u32(0)
    out += vce.put_u8(0) + vce.put_u8(current[0]) + vce.put_u8(current[1]) + vce.put_u32(current[2])
    out += vce.put_u8(nxt[0]) + vce.put_u8(nxt[1]) + vce.put_u32(nxt[2])
    out += vce.put_u32(watch) + vce.put_u32(foster) + vce.put_u32(build)
    out += vce.put_u8(build_off) + b"\x00\x00\x00"
    return out + shops.event_header()


def hud_page(place, attacker, defender, score_a=0, score_d=0, camps_a=b"", camps_d=b"",
             hq_a=0, hq_d=0, rounds=0, wins_a=0, wins_d=0, flags=1, next_start=0,
             round_start=0, now=0):
    """Tag 0x13 war-state object (reader 0x229600; draw 0x67f440). The three times are read as u32
    (0x1ad790 zero-extends into the u64 fields) in seconds; only differences are shown (h:m)."""
    out = vce.put_u8(place) + vce.put_u32(attacker) + vce.put_u32(defender)
    out += vce.put_u32(score_a) + vce.put_u32(score_d)
    out += vce.put_bytes(bytes(camps_a)[:9]) + vce.put_bytes(bytes(camps_d)[:9])
    out += bytes([hq_a & 0xFF, hq_d & 0xFF, rounds & 0xFF, wins_a & 0xFF, wins_d & 0xFF])
    out += vce.put_u32(flags)
    for t in (next_start, round_start, now):
        out += vce.put_u32(int(t) & 0xFFFFFFFF)
    return out + shops.event_header()


def ranking_page(category, rows, highlight=0, base=0):
    """Tag 0x0d (reader 0x227dc0; draw 0x651d70): u8 category (1 war, 2 power, 3 funds, 4 skirmish),
    u8 highlighted row, u8 base rank, 20 x str (0x21), u32 array (max 20), trailer."""
    rows = list(rows)[:RANK_ROWS]
    out = bytes([category & 0xFF, highlight & 0xFF, base & 0xFF])
    for i in range(RANK_ROWS):
        out += _name(rows[i][0] if i < len(rows) else "")
    out += vce.put_len(len(rows)) + b"".join(vce.put_u32(max(0, int(v))) for _, v in rows)
    return out + shops.event_header()


def merit_page(names, first_spear=""):
    """Tag 0x0e (reader 0x228140). The reader takes u8 +0xc, u8 count +0x157, then 33 strings into
    +0xd (loop bound 0x21): string #10 overwrites the count, so it is sent empty (count 0) and no
    pair strings follow. Then str +0x578, u8 +0x5b3, bytes[9] +0x599, bytes[16] +0x5a3, u8 +0x5a2."""
    names = (list(names) + [""] * 10)[:10]
    out = bytes([0, 0])
    for i in range(33):
        out += _name(names[i] if i < 10 else "")
    out += _name(first_spear) + b"\x00" + vce.put_bytes(b"") + vce.put_bytes(b"") + b"\x00"
    return out + shops.event_header()


def board_event(event_id, pages):
    """11 <event id> <n> n x {<u8 tag> <page>} (0x1a04f8)."""
    return (vce.put_u8(shops.EVENT) + vce.put_u8(event_id) + vce.put_u8(len(pages))
            + b"".join(vce.put_u8(t) + body for t, body in pages))


def notify_skirmish(faction=0, rec=None, seq=0):
    """2F 30 (parser 0x1f3af0 -> UI 0x4d -> 0x2091a0, Profile Skirmish tab): u8 x 9 (+0 clan, +3
    streak, +4 best streak, +7 / +8 last-100 wins / losses), u32 +0xc, +0x10 valor earned,
    +0x14 valor total, +0x18 unspent."""
    r = dict(rec or {})
    b = [faction, r.get("b1", 0), r.get("b2", 0), r.get("streak", 0), r.get("best", 0), r.get("b5", 0),
         r.get("b6", 0), r.get("wins", 0), r.get("losses", 0)]
    out = vce.put_u8(0x2F) + vce.put_u8(0x30) + vce.put_u16(seq)
    out += bytes(max(0, min(255, int(v))) for v in b)
    for k in ("u0c", "earned", "total", "unspent"):
        out += vce.put_u32(int(r.get(k, 0)))
    return out


# ---------------------------------------------------------------------------- board content
def week_view(st=None, now=None):
    """The scheduler's view of the week (war_week.view)."""
    from . import war_week
    return war_week.view(st or state(), now)


def edict_for(faction, st=None, view=None):
    st = st or state()
    view = view if view is not None else week_view(st)
    last = (0, 0, 0)
    lw = st.last_war
    if lw and faction and faction in (lw.get("attacker"), lw.get("defender")):
        win = lw.get("winner", 0)
        res = 0 if not win else (1 if win == faction else 2)
        last = (res, 1 if lw.get("transfer") and res else 0, lw.get("area", 0))
    current = nxt = (0, 0, 0)
    if st.pair and faction and faction in st.pair:
        att, dfn = st.pair
        enemy = dfn if faction == att else att
        mine = 1 if faction == att else 0
        if view["phase"] == "declared":
            nxt = (mine, st.area or 0, enemy)
        elif view["phase"] in ("round", "break"):
            current = (mine, st.area or 0, enemy)
    build = 0
    if view["phase"] == "build" and faction:
        weak = [p for p in st.provinces_of(faction) if st.power[p] < POWER_MAX]
        build = weak[0] if weak else 0
    return edict_page(faction, last, current, nxt, build=build)


def hud_for(st=None, view=None):
    """The HUD page while a war is declared, running, or decided this week; else None."""
    st = st or state()
    view = view if view is not None else week_view(st)
    if not st.pair or view["phase"] not in ("declared", "round", "break", "results"):
        return None
    att, dfn = st.pair
    wk = st.week
    rounds = [r for r in wk.get("rounds", []) if "result" in r]
    wins_a = sum(1 for r in rounds if r["result"] == "a")
    wins_d = sum(1 for r in rounds if r["result"] == "d")
    flags = {"round": 0, "results": HUD_OVER}.get(view["phase"], HUD_PREPARING)
    sa, sd = wk.get("score", [0, 0]) if view["phase"] == "round" else (0, 0)
    return hud_page(st.area or 0, att, dfn, sa, sd, rounds=len(rounds), wins_a=wins_a, wins_d=wins_d,
                    flags=flags, next_start=view.get("next_start", 0),
                    round_start=view.get("round_start", 0), now=view.get("now", 0))


def ranking_for(faction, store=None, viewer_cid=None, st=None):
    """War deeds of this week for the viewer's clan (chars.json), merit as the tie-break."""
    st = st or state()
    rows = []
    if store is not None:
        s = store() if callable(store) else store
        for cs in getattr(s, "data", {}).get("chars", {}).values():
            for c in cs:
                if char_faction(c) == faction:
                    rows.append((st.deeds.get(str(c["cid"]), 0), int(c.get("merit", 0)),
                                 f"{c.get('family', '')}{c.get('given', '')}", c["cid"]))
    rows.sort(key=lambda r: (-r[0], -r[1], r[3]))
    rows = rows[:RANK_ROWS]
    hi = next((i for i, r in enumerate(rows) if r[3] == viewer_cid), 0)
    return ranking_page(1, [(r[2], r[0]) for r in rows], highlight=hi)


def board_pages(ctx, store=None, st=None, view=None):
    st = st or state()
    view = view if view is not None else week_view(st)
    c = ctx.get("char") or {}
    f = char_faction(c)
    pages = [(TAG_EDICT, edict_for(f, st, view))]
    hud = hud_for(st, view)
    if hud is not None:
        pages.append((TAG_HUD, hud))
    pages.append((TAG_RANKING, ranking_for(f, store, c.get("cid"), st)))
    if os.environ.get("NOBU_WAR_BOARD_MERIT") == "1":
        pages.append((TAG_MERIT, merit_page([])))
    return pages


def open_board(ctx, store=None, now=None):
    eid = shops.next_event_id(ctx)
    ctx["war_event"] = dict(id=eid)
    return board_event(eid, board_pages(ctx, store, view=week_view(None, now)))


def other_event_open(ctx):
    """A shop / craft / trade event is waiting for its answer: do not push a board over it."""
    return any(ctx.get(k) for k in ("shop_event", "craft_event", "trade_event", "wares_event"))


# ---------------------------------------------------------------------------- the board NPC
BOARD_TEXT = "The clan notice board: edicts, this week's war and the deeds ranking."


def board_enabled():
    return enabled() and os.environ.get("NOBU_WAR_BOARD", "1") != "0"


def board_cid():
    return int(os.environ.get("NOBU_WAR_BOARD_CID", "0xfea00020"), 0) & 0xFFFFFFFF


def board_npc(player):
    lk = npc.npc_looks(player, "herald")
    lk[5], lk[6], lk[7], lk[8] = 4, 1, 1, 60          # own face / hair / build
    return dict(cid=board_cid(), uid=0, b6b=npc.TALK_ONLY, family="Notice Board", given="",
                looks=lk, level=player.get("level", 1), role="war_board", text=BOARD_TEXT)


def board_spawn(ctx):
    player = ctx.get("char")
    if not player or not board_enabled() or not npc.is_npc_cid(board_cid()):
        return []
    zone, x, y, z, _ = chars.char_position(player)
    if (zone != int(os.environ.get("NOBU_WAR_BOARD_ZONE", "110"))
            and os.environ.get("NOBU_WAR_BOARD_ANY_ZONE") != "1"):
        return []
    b = board_npc(player)
    pos = (zone, x + int(os.environ.get("NOBU_WAR_BOARD_DX", "300")), y,
           z + int(os.environ.get("NOBU_WAR_BOARD_DZ", "-300")), 0)
    b["pos"] = list(pos)
    ctx.setdefault("npcs", {})[b["cid"]] = b
    return npc.spawn_pushes(b, pos)


# ---------------------------------------------------------------------------- chat
def chat_answer(payload, ctx):
    if os.environ.get("NOBU_WAR_CHAT", "1") == "0" or not ctx.get("char"):
        return None
    try:
        d = social.decode_chat(payload)
    except vce.WireError:
        return None
    t = (d["text"] or "").strip().lower().lstrip("/!").split()
    if not t or t[0] != "war":
        return None
    from . import war_week
    out = social.chat_replies(d, ctx)
    debug = os.environ.get("NOBU_WAR_DEBUG") == "1"
    if debug and len(t) >= 4 and t[1] == "score" and t[2] in ("a", "d"):
        try:
            war_week.add_score(0 if t[2] == "a" else 1, int(t[3]))
        except ValueError:
            pass
    elif debug and len(t) >= 2 and t[1] == "tick":
        war_week.tick()
    out += [misc.notify_system(line) for line in war_week.status_lines()]
    return out


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """23 04 / 23 05, 28 01 on the board, 0a answers to a board event, chat "war"; else None."""
    if not enabled() or len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x23 and sub in (4, 5) and len(payload) >= 4:
        seq = vce.Reader(payload, 2).u16()
        if sub == 4:
            return [diplomatic_reply(seq)]
        c = ctx.get("char")
        return [friendship_reply(seq, viewer=char_faction(c), wanted=(c or {}).get("wanted"))]
    if cmd == 0x28 and sub == 1 and board_enabled():
        b = ctx.get("npcs", {}).get(ctx.get("target"))
        if not b or b.get("role") != "war_board" or not ctx.get("char"):
            return None
        ctx.setdefault("actions", []).append(ctx.get("target"))
        return [npc.notify_npc_say(b["cid"], b.get("text", BOARD_TEXT), name=npc.npc_name(b)), open_board(ctx, store)]
    if cmd == shops.EVENT_REPLY and ctx.get("war_event"):
        try:
            d = shops.decode_event_reply(payload)
        except vce.WireError:
            return None
        if d["event"] != ctx["war_event"]["id"]:
            return None
        ctx["war_event"] = None                        # closed (n == 0xff) or answered: nothing to do
        return []
    if cmd == 0x23 and sub == 8 and len(payload) >= 4:
        return chat_answer(payload, ctx)
    return None


def spawn_frames(ctx):
    """Board NPC (registered in ctx["npcs"] here), 2F 04 type 3 (silent diplomatic state + war list),
    2F 30 (skirmish record)."""
    out = board_spawn(ctx)
    if os.environ.get("NOBU_WAR_PUSH", "1") != "0":
        out.append(notify_diplomacy(kind=3))
    c = ctx.get("char")
    if c and os.environ.get("NOBU_WAR_SKIRMISH_PUSH", "1") != "0":
        out.append(notify_skirmish(char_faction(c), c.get("skirmish")))
    return out


def world_ready(ctx):
    """After the spawn (23 5a). (The week scheduler is started by world.main, not here.) The
    frames of spawn_frames are pushed NOBU_WAR_SPAWN_DELAY s (default 1) later through ctx["push"],
    after the spawn burst, like upkeep's first tick; NOBU_WAR_INLINE=1 returns them inline instead
    (tests, or a rig without a loop). Without a loop and push nothing is sent (unit tests)."""
    if not enabled() or not ctx.get("char"):
        return []
    if os.environ.get("NOBU_WAR_INLINE") == "1":
        return spawn_frames(ctx)
    import asyncio
    push = ctx.get("push")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return []
    if push is None:
        return []

    def later():
        if not any(v is ctx for v in social.SESSIONS.values()) and social.SESSIONS:
            return                                     # the session closed meanwhile
        try:
            for f in spawn_frames(ctx):
                push(f)
        except Exception:                              # writer closed
            pass
    loop.call_later(max(0.0, float(os.environ.get("NOBU_WAR_SPAWN_DELAY", "1"))), later)
    return []


def describe(payload, direction="C>S"):
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    seq = int.from_bytes(payload[2:4], "big")
    if cmd == 0x23 and sub in (3, 4, 5):
        name = {3: "SCMMD_PC_GET_POWER_TABLE", 4: "SCMD_GET_DIPLOMATIC_STATE",
                5: "SCMD_GET_FRIENDSHIP_STATE"}[sub]
        if direction == "C>S":
            return f"WAR {name} request seq={seq}"
        if sub == 3:
            return None
        try:
            d = parse_diplomatic(vce.Reader(payload, 4)) if sub == 4 else \
                parse_friendship(vce.Reader(payload, 4))
        except vce.WireError as e:
            return f"WAR {name} reply seq={seq} (malformed: {e})"
        if sub == 4:
            return f"WAR {name} reply seq={seq} owners={d['owners'].hex()} fallen={d['flags'].hex()}"
        return (f"WAR {name} reply seq={seq} funds={d['funds']} "
                f"power={list(d['power'])}")
    if cmd == 0x2F and sub == 4 and direction == "S>C" and len(payload) >= 5:
        return f"WAR NOTIFY diplomacy type={payload[4]} seq={seq} len={len(payload)}"
    if cmd == 0x2F and sub == 0x30 and direction == "S>C" and len(payload) >= 13:
        return f"WAR 2F 30 skirmish record clan={payload[4]} {payload[5:].hex()}"
    if (cmd == shops.EVENT and direction == "S>C" and len(payload) >= 4 and payload[2] != 0xFF
            and payload[3] in TAG_NAMES):
        return f"WAR board event id={payload[1]} pages={payload[2]} first={TAG_NAMES[payload[3]]}"
    return None


# ---------------------------------------------------------------------------- readers (describe)
def parse_diplomatic(r):
    d = dict(owners=r.bytes(22), rows=[r.bytes(k) for k in range(1, FACTIONS)])
    d["flags"] = r.bytes(15)
    return d


def parse_friendship(r):
    def u16s(maxn):
        n = r.length()
        if n > maxn:
            raise vce.WireError(f"u16 array of {n} > {maxn}")
        return [r.u16() for _ in range(n)]
    return dict(rel=r.bytes(15), funds=u16s(15), prov2e=r.bytes(22), power=r.bytes(22),
                wanted=u16s(18))
