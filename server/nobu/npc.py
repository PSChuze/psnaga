"""Non-player characters: spawn (NOTIFY_CHAR uid 0), targeting (25 01) and talking (28 01).

Spawn
  NOTIFY_CHAR 0B 0A CHAR_INFO with uid 0: main 0x189ac8 posts UI event 0xbd (cid top byte 0xff), 0xd4
  (0xfe) or 0xa3 (any other cid): the conditional move at 0x189c48 keeps 0xd4 only when v1 = (cid &
  0xff000000) == 0xfe000000. 0xbd / 0xa3 hit the UI default (nothing). UI 0xd4 (0x1d2df0) allocates a
  character entry (0x1d6ef0), sets +0x14 = cid, names (0x1d5450), +0xa4 = record +0x40 (the job
  byte), fills it from the record (0x1d96b0: position, level, faction, job, sex, PARAMS, appearance
  words) and posts task 0x94 event 0x26. 0x94 (0x21c6e0): +0xa4 == 0 -> nothing, == 1 -> 0x21c2a0 (a
  different object path), else 0x21bd70 -> World task 0x97/5 event 1 -> 0x60c220 creates the actor
  (task type 8, id = cid), builds the model from the entry's appearance (vt 0x20 -> 0x5ff2d0) and
  drops it onto the ground under x/z (0x16bee0) facing +0x28 degrees. So: uid 0, cid top byte 0xfe,
  job byte not 0 / 1, area == the zone's area. Other players use the same path (players.wire_cid =
  0xfe000000 | cid); NPC cids take the low 24 bits from NPC_LOW_MIN (0xa00000) up so they never
  collide with a player's wire cid. (0B 01 CHAR_LOCATION with uid 0 takes the same road through UI
  0xd5 / 0x1d98c0, without PARAMS or appearance, so CHAR_INFO is used.)

Target (KNOWN layouts)
  Selecting any character runs the target manager (0x611490 -> 0x1d7040 -> 0x20a3c0), which sends
  C>S `25 02 <seq>` (drop the old target, 0x20a220) and `25 01 <seq> <u32 cid>` (SCMD_TARGET_SET,
  kind 0x44, builder 0x1c9240). Reply parser 0x28b190 sub 1 (0x28b2a8): `i32 res`, then if res >= 0
  the struct 0x1f0dc0: u32 cid, str family (max 0x11), str given (max 0x11), u8 level +0x26,
  u8 +0x27 (x 1000 -> target+0xab0), u8 +0x28, u8 +0x29 (-> target+0x957), u8 +0x2a, u8 +0x2b,
  u32 x, u32 y, u32 z, u16 dir. Callback 0x20aa10 -> 0x20a760: res < 0 clears the target;
  else 0x20afb0 copies it, but only if the cid is a known character entry (0x1d7010), otherwise
  it sends 25 02 again. Before this module nothing answered 25 01.

Talk (HYPOTHESIS, strong)
  The on-screen action hint (0x62d3d0) returns 0xd = UIMSG 0.0.13 "○Look" for a uid-0 target
  whose cid top byte is 0xfe (9 "○Talk" for other uid-0 cids, which cannot be spawned by 0B 0A).
  The label does not change the action: the ○ handler 0x62ccf0 first tries the character action
  0x62c990, which only asks the hint (0x62d7d0 -> 0x62d3d0) whether it is 0x2c ("none"); any other
  value, Look included, goes on: target in range (500 units x factor) -> 0x611880 (no hint test):
  stop, face the target
  (0x5fc7b0), commit the target (0x611490, sends 25 01 if it changed) and post net command 0x81 =
  C>S `28 01 <u16 seq>` (builder 0x1c90e0, header only). Otherwise it tries the static-object action
  0x62cc50 (doors etc., gp-0x70fc) = `28 02 <u16 seq>`. The client has no reply parser for cmd 0x28
  (0x18f6e0 returns at once), so the server answers with pushes. Which push the original server used
  for NPC dialogue is not known; we send two proven ones:
    0D 09 <u32 speaker cid> <u32 0> <bytes text>   NOTIFY_CHAT sub 9 (0x19b988): chat line spoken by
        the NPC (UI 0x73 looks the cid up, names from the entry +0x80 / +0x89).
    0D 05 <bytes text>                             NOTIFY_CHAT sub 5 CHAT_MESSAGE: UI 0x71 -> message
        box 0x2c (0x2592b0) -> 0x258bf0(text, style 3), a popup window.

Env:
  NOBU_DEBUG_NPC=1          spawn one NPC "Guide" after the spawn pushes (23 5a), in the home towns only
  NOBU_NPC_FIELDS=1         also spawn it (and the Overseer) beside the player outside the towns
  NOBU_NPC_CID              its cid (default 0xfea00001; must be 0xfe...... with low 24 bits >= 0xa00000)
  NOBU_NPC_DX / NOBU_NPC_DZ offset from the player's spawn point in world units (default 300 / 0)
  NOBU_NPC_DIR              facing in degrees (default 180)
  NOBU_NPC_FAMILY / NOBU_NPC_GIVEN  name parts (default "Guide" / "")
  NOBU_NPC_TEXT             what it says
  NOBU_NPC_POPUP=0          chat line only, no message box
  NOBU_NPC_TALK_ON_TARGET=1 also talk when the NPC is merely targeted (25 01), fallback if 28 01
                            turns out not to be the talk request
"""
import os

from . import chars, misc, towns, vce

DEFAULT_TEXT = "Welcome to Nagoya, traveller. I am your guide. More to come soon."
TOWN_TEXT = "Welcome to {town}, traveller. I am your guide. More to come soon."


def guide_text(zone):
    """The Guide's line: NOBU_NPC_TEXT, else the welcome of the town it stands in (towns.py; Nagoya's line is
    DEFAULT_TEXT), else DEFAULT_TEXT."""
    if os.environ.get("NOBU_NPC_TEXT"):
        return os.environ["NOBU_NPC_TEXT"]
    return TOWN_TEXT.format(town=towns.name(zone, "Nagoya"))


def debug_enabled():
    return os.environ.get("NOBU_DEBUG_NPC") == "1"


TALK_ONLY = 2                    # record +0x6b -> entry +0x2c == 2: never attackable (0x2733a0)
NPC_TAG = 0xFE000000             # uid 0 + this top byte -> UI 0xd4 / 0xd5 (entry + actor)
NPC_LOW_MIN = 0xA00000           # low 24 bits reserved for NPCs; player cids stay below this


def is_npc_cid(cid):
    """A cid in the NPC range: 0xfe top byte (the only uid-0 cids the client draws) and low 24
    bits >= NPC_LOW_MIN, so it can never equal players.wire_cid of a real (smaller) player cid."""
    return (cid >> 24) == 0xFE and (cid & 0xFFFFFF) >= NPC_LOW_MIN


def check_npc_cid(cid, name):
    if not is_npc_cid(cid):
        raise ValueError(f"{name}: {cid:#x} is not an NPC cid (0xfe top byte, low 24 bits >= "
                         f"{NPC_LOW_MIN:#x}); other uid-0 cids are not drawn (UI 0xa3)")
    return cid


def npc_cid():
    cid = int(os.environ.get("NOBU_NPC_CID", "0xfea00001"), 0) & 0xFFFFFFFF
    return check_npc_cid(cid, "NOBU_NPC_CID")


def npc_job(job):
    """Job byte = entry +0xa4: 0 creates no actor and 1 takes the other object path (0x21c2a0)."""
    return job if job not in (0, 1) else 6


# Fixed per-NPC appearance (face looks[5] -> record +0x68, hair looks[6]/[7] -> +0x67, body slider
# looks[8] -> +0x56; chars.appearance) so NPCs no longer copy the player's face and hair. Sex, job
# and faction still come from the player (a model that is known to render); faces are 0..8.
NPC_STYLES = {"guide": (1, 2, 50), "overseer": (3, 0, 65), "merchant": (5, 3, 40),
              "bandit": (7, 1, 75), "guildmaster": (6, 2, 85), "guard": (8, 1, 80)}


def npc_looks(player, style, job=None):
    """The player's looks with a fixed face / hair / build for NPC style `style` and a job byte
    that makes a person (npc_job)."""
    lk = (list(player.get("looks", [])) + [0] * 20)[:20]
    face, hair, body = NPC_STYLES.get(style, (0, 0, 50))
    lk[5], lk[6], lk[7], lk[8], lk[9] = face, hair, hair, body, 50
    lk[3] = npc_job(lk[3] if job is None else job)
    return lk


def debug_npc(player):
    """The guide: a char dict shaped like chars.json entries (sex / job / faction from the player,
    face / hair / build fixed by NPC_STYLES["guide"]). Its line names the town it stands in."""
    lk = npc_looks(player, "guide")
    return dict(cid=npc_cid(), uid=0, b6b=TALK_ONLY, family=os.environ.get("NOBU_NPC_FAMILY", "Guide"),
                given=os.environ.get("NOBU_NPC_GIVEN", ""), looks=lk, level=player.get("level", 1),
                text=guide_text(chars.char_position(player)[0]))


def town_spot(player, dx, dz, d, role=None):
    """(zone, x, y, z, dir) for an NPC: in a home town (towns.py) the town's own spot for `role` when
    town_spots.json has one (towns.spot), else the Nagoya offsets (dx, dz, dir) from the town's spawn point turned
    with its gate (towns.place; Nagoya = unchanged), so every town's NPCs stand in the same place whoever logs in
    where; elsewhere the offsets from the player's position."""
    zone, x, y, z, _ = chars.char_position(player)
    if towns.is_town(zone):
        return towns.spot(zone, role) or towns.place(zone, dx, dz, d)
    return towns.spot(zone, role) or (zone, x + dx, y, z + dz, d % 360)        # a castle's own spot, if any


def npc_position(player):
    dx = int(os.environ.get("NOBU_NPC_DX", "300"))
    dz = int(os.environ.get("NOBU_NPC_DZ", "0"))
    d = int(os.environ.get("NOBU_NPC_DIR", "180")) % 360
    return town_spot(player, dx, dz, d, role="guide")


GUARD_ROLE = 0x40                # per-town cid role bytes 0x40.. (shops.town_cid scheme; 3 / 4 / 0x10 are keepers)
GUARD_TEXT = "The gate of {town}. Keep the peace."


def guards_enabled():
    return os.environ.get("NOBU_TOWN_GUARDS", "1") != "0"


def guard_cid(zone, i):
    return 0xFEA00000 | (int(zone) & 0xFFF) << 8 | ((GUARD_ROLE + i) & 0xFF)


def guard_spawn(ctx):
    """The gate guards of the player's home town, two inside every gate (towns.guard_spots).
    Talk-only; registered in ctx["npcs"] so 25 01 / 28 01 answer them. NOBU_TOWN_GUARDS=0: none."""
    player = ctx.get("char")
    if not player or not guards_enabled():
        return []
    zone = chars.char_position(player)[0]
    out = []
    for i, (role, pos) in enumerate(towns.guard_spots(zone)[:0x40]):
        lk = npc_looks(player, "guard")
        g = dict(cid=guard_cid(zone, i), uid=0, b6b=TALK_ONLY, family="Gate Guard", given="", looks=lk,
                 level=player.get("level", 1), role="guard", text=GUARD_TEXT.format(town=towns.name(zone)))
        g["pos"] = list(pos)
        ctx.setdefault("npcs", {})[g["cid"]] = g
        out += spawn_pushes(g, pos)
    return out


REG_LOC_LEN = 15                 # CHAR_INFO location part after uid / cid: u8 area, u32 x, y, z, u16 dir


def reg_enabled():
    return os.environ.get("NOBU_NPC_REG", "1") != "0"


def register_frame(npc, pos):  # records of one NPC: sub 0x17 + sub 0x19 (no cmd byte)
    """S>C cmd 0x17: records {u8 sub, body} up to the end of the frame (parser 0x1a1a38). Without it the
    client knows no NPC name: the NPC say handler (UI 0x73, 0x1d09e8) and the target window look the speaker
    up in the character registry UI+0x19e80 (0x1d73e0), which only this message fills, and print UIMSG 0.57
    "??????" when it misses.
      sub 0x17 (reader 0x1a4370 -> UI 0xb0 -> 0x3c2220): i16 key, u32 uid, u32 cid, str family, str given
               = the registry item (the target window shows its family name).
      sub 0x19 (reader 0x1a4590 -> UI 0xb2 -> 0x3c28e0): i16 key, u8 player flag (1 = the uid branch), the
               CHAR_INFO body without uid / cid / names, u8 n + n x {u8, u16} (0x1a2380), i16, u8, u8, u8
               (0x1a5850). Its handler creates the item's character object (0x3c1b40; uid 0 -> NPC object
               0x273060) and fills it from the item's names + this body (0x3c1d00 -> 0x1d96b0): the NPC say
               line then carries the name (the chat window shows its first 8 bytes).
    key = the low 15 bits of the cid (NPC cids are unique there)."""
    key = npc["cid"] & 0x7FFF
    rec = chars.char_info_record(npc, 0, pos)
    off = 8 + REG_LOC_LEN
    for _ in range(2):                                     # skip family, given: the item carries them
        off += 1 + rec[off]
    family = npc.get("family", "").encode("utf-8")
    given = npc.get("given", "").encode("utf-8")
    sub17 = (vce.put_u16(key) + vce.put_u32(npc.get("uid", 0)) + vce.put_u32(npc["cid"])
             + vce.put_bytes(family) + vce.put_bytes(given))
    sub19 = (vce.put_u16(key) + vce.put_u8(0) + rec[8:8 + REG_LOC_LEN] + rec[off:]
             + vce.put_u8(0) + vce.put_u16(key) + vce.put_u8(0) + vce.put_u8(0) + vce.put_u8(0))
    return vce.put_u8(0x17) + sub17 + vce.put_u8(0x19) + sub19


REG_PER_FRAME = 8                # NPCs per cmd 0x17 frame: ~1 KB, the size of the 0B 0A bursts seen live


def registry_frames(ctx):
    """cmd 0x17 frames registering every NPC of the session (ctx["npcs"], with their spawn "pos"), at most
    REG_PER_FRAME NPCs each ([] when off). world.world_ready_pushes puts them after the spawn pushes."""
    if not reg_enabled():
        return []
    recs = [register_frame(n, tuple(n["pos"])) for n in ctx.get("npcs", {}).values()
            if n.get("pos") and is_npc_cid(n.get("cid", 0))]
    return [vce.put_u8(0x17) + b"".join(recs[i:i + REG_PER_FRAME]) for i in range(0, len(recs), REG_PER_FRAME)]


def registry_frame(ctx):
    """The first registry frame, or None (tests / single-NPC callers)."""
    fr = registry_frames(ctx)
    return fr[0] if fr else None


def spawn_pushes(npc, pos):
    """0B 0A with one uid-0 record (UI 0xd4 -> actor creation). pos = (zone, x, y, z, dir)."""
    out = [chars.notify_char_info([chars.char_info_record(npc, 0, pos)])]
    return out


def debug_spawn(ctx):
    """Pushes for NOBU_DEBUG_NPC=1 (appended to world_ready_pushes). Remembers the NPC in ctx."""
    player = ctx.get("char")
    if not player or not debug_enabled():
        return []
    if not towns.is_town(chars.char_position(player)[0]) and os.environ.get("NOBU_NPC_FIELDS") != "1":
        # Outside the towns the Guide (and the Overseer, quests.world_ready) stood next to the player
        # wherever they went and read as the province's only NPCs
        return []
    npc = debug_npc(player)
    pos = npc_position(player)
    npc["pos"] = list(pos)
    ctx.setdefault("npcs", {})[npc["cid"]] = npc
    return spawn_pushes(npc, pos)


# ---------------------------------------------------------------------------- target without 25 01
# Live: ○ on a drawn NPC sends only `23 11 <seq> 00000000` then `28 01 <seq>`, sometimes `25 02`
# first, and never `25 01`. Static reading: 0x611880 -> 0x611490 sends the target set only through
# 0x1d7040(UI, pending cid, 1) -> 0x1d7010 (lookup in UI+0x19e80, node +0x30) -> 0x20a3c0 (25 02 via
# 0x20a220, then 25 01 kind 0x44). With pending cid gp-0x7acc == -1 it calls 0x1d7270 instead (25 02
# only); the cursor selection 0x611770 calls 0x1d7040 with flag 0, which never sends 25 01. So the
# server picks the target itself.
TALK_RANGE = 800                # world units; the client acts within 500 x factor (0x62ca04)


def player_xz(ctx):
    p = ctx.get("pos")
    if isinstance(p, dict) and "x" in p:
        return p["x"], p["z"]
    if ctx.get("players_pos"):
        return ctx["players_pos"][1], ctx["players_pos"][3]
    _, x, _, z, _ = chars.char_position(ctx.get("char"))
    return x, z


def resolve_target(ctx):
    """Before 28 01: keep ctx["target"] if it is a spawned NPC / enemy in range, else set it to the
    nearest one within NOBU_TALK_RANGE of the player's last 05 05 position. Returns the cid or None."""
    known = dict(ctx.get("npcs") or {})
    known.update(ctx.get("enemies") or {})
    if not known or not ctx.get("char"):
        return None
    x, z = player_xz(ctx)
    rng = int(os.environ.get("NOBU_TALK_RANGE", TALK_RANGE))

    def dist(n):
        p = n["pos"]
        return ((p[1] - x) ** 2 + (p[3] - z) ** 2) ** 0.5

    t = ctx.get("target")
    if t in known and dist(known[t]) <= rng:
        return t
    best = min(known.values(), key=dist)
    if dist(best) > rng:
        return None
    ctx["target"] = best["cid"]
    ctx["target_auto"] = best["cid"]
    return best["cid"]


# ---------------------------------------------------------------------------- target (25 01)
def decode_target_set(payload):
    """25 01 <u16 seq> <u32 cid> (builder 0x1c9240)."""
    r = vce.Reader(payload, 2)
    seq = r.u16()
    return dict(seq=seq, cid=r.u32() if len(r.rest()) >= 4 else 0)


def target_info(c, pos, level=None):
    """Struct 0x1f0dc0 for a char dict c at pos (zone, x, y, z, dir)."""
    _, x, y, z, d = pos
    w = chars.W().u32(c["cid"]).str(c.get("family", "")).str(c.get("given", ""))
    w.u8(c.get("level", 1) if level is None else level)
    for _ in range(5):                                  # +0x27..+0x2b (meaning not decoded)
        w.u8(0)
    w.u32(x).u32(y).u32(z).u16(d)
    return w.bytes()


def target_reply(seq, res, info=b""):
    """25 01 <seq> i32 res [struct 0x1f0dc0 if res >= 0]."""
    out = vce.put_u8(0x25) + vce.put_u8(1) + vce.put_u16(seq) + vce.put_u32(res)
    return out + (bytes(info) if res >= 0 else b"")


def find_target(cid, ctx, store=None):
    """(char dict, pos) for an NPC we spawned or a stored player character, else None."""
    npc = ctx.get("npcs", {}).get(cid)
    if npc:
        return npc, tuple(npc["pos"])
    st = store() if callable(store) else store
    if st is not None:
        for cs in st.data["chars"].values():
            for c in cs:
                if c["cid"] == cid:
                    return c, chars.char_position(c)
    return None


# ---------------------------------------------------------------------------- talk
def notify_npc_say(cid, text, extra=0, name=None):
    """An NPC's chat line. With `name` (default): `0D 06` system line "name: text". `0D 09 <u32 speaker cid>
    <u32 extra> <bytes text>` printed every NPC as "？？？？？？: text". Its parser 0x19b988 puts the cid at
    record +4 and UI 0x73 (0x1d09e8) looks it up with 0x1d73e0 in UI+0x19e80 (the target table); NPC entries
    live in UI+0x19e90 (0x1d7400, the spawn path), so the lookup misses, the line gets the empty names at
    0x422b08 and the chat log (0x3921dc: entry +4 null or "") draws UIMSG 0.57. NOBU_NPC_SAY=9, or no name:
    the 0D 09 form."""
    mode = os.environ.get("NOBU_NPC_SAY", "6")
    if name and mode != "9":
        return misc.notify_system(f"{name}: {text}")
    return (vce.put_u8(0x0D) + vce.put_u8(9) + vce.put_u32(cid) + vce.put_u32(extra)
            + vce.put_bytes(misc.clip_sjis(text, 0x190).encode("utf-8")))


def npc_name(n):
    """The display name of NPC dict n: family and given, as the client would join them."""
    return " ".join(p for p in (n.get("family"), n.get("given")) if p) or None


def talk_replies(npc):
    out = [notify_npc_say(npc["cid"], npc.get("text", DEFAULT_TEXT), name=npc_name(npc))]
    if os.environ.get("NOBU_NPC_POPUP", "1") == "1":
        out.append(misc.notify_message(npc.get("text", DEFAULT_TEXT)))
    return out


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Replies for 25 01 / 28 01 / 28 02, or None (25 02 only updates ctx and falls through to
    social.py, which echoes it)."""
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x25 and sub == 2:
        ctx["target"] = None
        return None
    if cmd == 0x25 and sub == 1:
        d = decode_target_set(payload)
        ctx["target"] = d["cid"]
        hit = find_target(d["cid"], ctx, store)
        if hit is None:
            return [target_reply(d["seq"], -1)]
        c, pos = hit
        out = [target_reply(d["seq"], 0, target_info(c, pos))]
        if (d["cid"] in ctx.get("npcs", {})
                and os.environ.get("NOBU_NPC_TALK_ON_TARGET") == "1"):
            out += talk_replies(c)
        return out
    if cmd == 0x28 and sub == 1:
        npc = ctx.get("npcs", {}).get(ctx.get("target"))
        ctx.setdefault("actions", []).append(ctx.get("target"))
        return talk_replies(npc) if npc else []
    if cmd == 0x28:
        return []                                       # 28 02: static object use; logged only
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if direction == "C>S" and cmd == 0x25 and sub == 1 and len(payload) >= 8:
            d = decode_target_set(payload)
            return f"CODE_CHONOBU_PC SCMD_TARGET_SET seq={d['seq']} cid={d['cid']:#x}"
        if direction == "S>C" and cmd == 0x25 and sub == 1 and len(payload) >= 8:
            r = vce.Reader(payload, 2)
            seq, res = r.u16(), r.i32()
            return f"CODE_CHONOBU_PC SCMD_TARGET_SET reply seq={seq} res={res}"
        if direction == "C>S" and cmd == 0x28 and len(payload) >= 4:
            what = {1: "act on target (talk)", 2: "use object"}.get(sub, f"sub {sub}")
            return f"CMD_28 sub {sub} ({what}) seq={vce.Reader(payload, 2).u16()}"
        if direction == "S>C" and cmd == 0x0D and sub == 9:
            r = vce.Reader(payload, 2)
            cid, extra = r.u32(), r.u32()
            return f"NOTIFY_CHAT sub 9 (NPC say) cid={cid:#x} {r.bytes().decode('utf-8', 'replace')!r}"
    except vce.WireError:
        return None
    return None
