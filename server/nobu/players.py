"""Other player characters: who sees whom, movement echo, removal, targeting, as the
client reads them (World.bin loaded, VA == offset).

How the client draws a character that is not the local player
  NOTIFY_CHAR main handler 0x1893f0 sorts every same-area record by uid and cid top byte:
    uid == net+0x44 (ours)            -> the local player (UI 7 / 0xb), never a second actor
    uid != 0, not ours                -> UI 0xc7 / 0xc4 / 0xc6 / 0xc5: all the UI default
                                         0x1d42c8 (nothing). A real uid is never drawn.
    uid 0, cid & 0xff000000 == 0xff   -> 0xc0 / 0xbd (UI default, nothing)
    uid 0, cid & 0xff000000 == 0xfe   -> 0xd5 (0B 01) / 0xd4 (0B 0A): character entry + actor
    uid 0, any other cid              -> 0xa6 / 0xa3 (UI default, nothing)
  (the conditional move at 0x189608 / 0x189c48 picks 0xd5 / 0xd4 only when the top byte is 0xfe.)
  So another player is sent as uid 0 with a cid whose top byte is 0xfe: WIRE_TAG | cid. The
  local player keeps its plain cid (net+0x48 from the SCMD_CHAR_LOAD reply), so it can never be
  confused with an entry. UI 0xd4 (0x1d2df0): 0x1d7400 finds the entry by cid in UI+0x19e90, or
  0x1d6ef0 allocates one; names (0x1d5450), +0xa4 = record +0x40 (job byte), 0x1d96b0 copies the
  CHAR_INFO record (entry +0x10 uid, +0x1c.. x/y/z, +0x28 dir, +0x18 area, level/faction/job/sex,
  PARAMS, appearance) and posts task 0x94 event 0x26 (new) or 0x28 (update). 0x94 event 0x26
  (0x21c6e0): job byte 0 -> nothing, 1 -> 0x21c2a0 (field object path, NOT a person), else
  0x21bd70 -> 0x97/5 event 1 -> 0x60c220 creates the actor (task type 8, id = wire cid).
  Action hint 0x62d3d0: uid-0 entry with cid 0xfe...... -> 0xd "○Look" (0x9 "○Talk" otherwise).

Movement (KNOWN): NOTIFY_CHAR 0B 08 CHAR_LOCATION_UPDATE `u8 count, {u8 kind, u32 cid, u32 x,
  u32 y, u32 z, u16 dir, u8 state}`; main 0x189640: kind 1 + our cid -> UI 0xc (warp us), kind 1
  other -> 0xc8 (nothing), 5 -> 0xc1 (nothing), **6 -> 0xd6** (0x1d2f70: find / create the entry by
  cid, 0x1d9a10 copies x/y/z/dir and +0x16 -> entry +0x2a, task 0x94 event 0x28 -> 0x21c130 ->
  actor task 8/cid event 1), anything else -> 0xa7 (nothing). 0B 09 _F: u16 coordinates << 3,
  kind 6 -> 0xd7 (0x1d9980).

Removal (KNOWN): NOTIFY_CHAR 0B 04 (parser 0x19f288, record 0x1a3ff0) `u32 uid, u32 cid, u8`,
  one record, no count. Main 0x189a30: uid != 0 -> 0xc5 (nothing); uid 0 + cid 0xfe...... ->
  **0xdf** (0x1d2db8: 0x1d7420 finds the entry, task 0x94 event 0x27 (remove actor), entry flag
  0x40, 0x1cd4d0 frees it). 0B 0B (0x19f318) is a list form: `u8 count, {u8 flag, u32 cid}` handed
  to the same main code with uid = (flag == 1). A CHAR_LOCATION / CHAR_INFO record whose area is
  nonzero and not the current area also routes to 0xdf for 0xfe cids (left the area).

Chat (KNOWN): NOTIFY_CHAT 0D 01 `u8 1, u32 cid, str` with cid != ours -> UI 0xca
  -> 0x1d73e0 looks the speaker up in the entry store (UI+0x19e80): the WIRE cid names the line.

Env: NOBU_PLAYERS=0 disables everything here (no broadcast, no peer spawn).
     NOBU_PLAYERS_ATTACKABLE=1 sends record +0x6b = 0 instead of 2 (talk-only).
"""
import os

from . import chars, npc, social, vce

WIRE_TAG = 0xFE000000
CID_MASK = 0x00FFFFFF
# 0xfe...... cids with low 24 bits >= npc.NPC_LOW_MIN belong to NPCs (npc.py / quests.py); player
# cids (store counter) stay below it, so a wire cid is unambiguous.
CHUNK = 16                  # records per 0B 0A frame (a record is ~110 bytes; frames stay small)


def enabled():
    return os.environ.get("NOBU_PLAYERS", "1") == "1"


# ---------------------------------------------------------------------------- cid mapping
def wire_cid(cid, viewer_cid=None):
    """The cid a viewer's client must see for character cid: its own plain cid for itself (the
    load reply / net+0x48), WIRE_TAG | cid for everybody else (entry path UI 0xd4 / 0xd6 / 0xdf)."""
    if viewer_cid is not None and cid == viewer_cid:
        return cid
    if cid & CID_MASK >= npc.NPC_LOW_MIN:
        raise ValueError(f"player cid {cid:#x} reaches the NPC cid range")
    return WIRE_TAG | (cid & CID_MASK)


def is_player_wire(wire):
    """A 0xfe cid of another player (not an NPC)."""
    return (wire >> 24) == 0xFE and not npc.is_npc_cid(wire)


def real_cid(wire):
    """Inverse of wire_cid (a cid the client sends back, e.g. in 25 01 or 07 01). NPC cids and
    plain cids come back unchanged."""
    return wire & CID_MASK if is_player_wire(wire) else wire


def my_cid(ctx):
    c = (ctx or {}).get("char")
    return c["cid"] if c else (ctx or {}).get("cid")


# ---------------------------------------------------------------------------- registry views
def ready(ctx):
    """In the world (after 23 5a) with a character: can see and be seen."""
    return bool(ctx and ctx.get("char") and ctx.get("players_ready"))


def zone_of(ctx):
    return ctx.get("players_zone", ctx.get("zone"))


def peers(ctx, same_zone=True):
    """Other ready sessions (in the same zone unless same_zone=False)."""
    z = zone_of(ctx)
    return [c for c in social.SESSIONS.values()
            if c is not ctx and ready(c) and (not same_zone or zone_of(c) == z)]


def session_by_cid(cid):
    """Live session ctx whose character has this (real) cid, else None."""
    for c in social.SESSIONS.values():
        ch = c.get("char")
        if ch and ch.get("cid") == cid:
            return c
    return None


def push(ctx, payload):
    fn = ctx.get("push") if ctx else None
    if fn:
        fn(payload)
        return True
    return False


# ---------------------------------------------------------------------------- positions / records
def position(ctx):
    """(zone, x, y, z, dir) of a session's character: the last 05 05 seen here, else the saved
    / home position, with the zone of the current field (ctx zone set by the 04 reply)."""
    if ctx.get("players_pos"):
        return tuple(ctx["players_pos"])
    zone, x, y, z, d = chars.char_position(ctx["char"])
    return (zone_of(ctx) or zone, x, y, z, d)


def info_record(ctx, viewer_cid):
    """0x1a2500 CHAR_INFO record for the character of session ctx as the viewer sees it: uid 0,
    wire cid (0xfe......), the same looks / PARAMS / appearance the owner gets for itself.
    +0x6b = 2 (npc.TALK_ONLY): never attackable (0x2733a0), so the hint is Look; with
    NOBU_PLAYERS_ATTACKABLE=1 it is 0 (any nonzero-faction player is then attackable)."""
    c = ctx["char"]
    b6b = 0 if os.environ.get("NOBU_PLAYERS_ATTACKABLE") == "1" else npc.TALK_ONLY
    return chars.char_info_record(dict(c, cid=wire_cid(c["cid"], viewer_cid), b6b=b6b), 0,
                                  position(ctx))


def notify_infos(records):
    out = []
    for i in range(0, len(records), CHUNK):
        out.append(chars.notify_char_info(records[i:i + CHUNK]))
    return out


def notify_update(wcid, pos, state=0, kind=6):
    """0B 08 with one record, kind 6 (entry update, UI 0xd6)."""
    _, x, y, z, d = pos
    return chars.notify_location_update(wcid, x, y, z, d, kind=kind, b16=state)


def notify_delete(wcid):
    """0B 04 u32 uid 0, u32 cid, u8 0 (record 0x1a3ff0): uid 0 + 0xfe cid -> UI 0xdf (remove)."""
    return vce.put_u8(0x0B) + vce.put_u8(4) + vce.put_u32(0) + vce.put_u32(wcid) + vce.put_u8(0)


# ---------------------------------------------------------------------------- hooks
def on_spawn(ctx):
    """Hook for world.world_ready_pushes (23 5a): mark the session visible, show it to everyone
    in its zone (pushed to their sessions now) and return the CHAR_INFO frames that make the
    zone's other players appear for this session (append them after the spawn pushes)."""
    if not enabled() or not (ctx and ctx.get("char")):
        return []
    old = ctx.get("players_zone")
    if ctx.get("players_ready") and old is not None and old != ctx.get("zone"):
        _hide(ctx)                                      # zone change without a 04 we saw
    ctx["players_ready"] = True
    ctx["players_zone"] = ctx.get("zone", position(ctx)[0])
    zone, x, y, z, d = chars.char_position(ctx["char"])
    if ctx.get("players_pos") is None or ctx["players_pos"][0] != ctx["players_zone"]:
        ctx["players_pos"] = (ctx["players_zone"], x, y, z, d)
    others = peers(ctx)
    me = my_cid(ctx)
    for o in others:
        for f in notify_infos([info_record(ctx, my_cid(o))]):
            push(o, f)
    return notify_infos([info_record(o, me) for o in others])


def refresh(ctx):
    """Re-send this session's CHAR_INFO to the other players of its zone (entry update, UI 0xd4 ->
    0x1d96b0), e.g. after it became a ghost or was revived: PARAMS[0] != 0 is the ghost flag other
    clients keep (entry vt+0x78 0x273570). Returns the count."""
    if not enabled() or not ready(ctx):
        return 0
    n = 0
    for o in peers(ctx):
        for f in notify_infos([info_record(ctx, my_cid(o))]):
            n += push(o, f)
    return n


def on_move(ctx, m):
    """05 05 (decoded by chars.decode_move) -> 0B 08 kind 6 to the other players in the zone."""
    if not enabled() or not ready(ctx):
        return 0
    ctx["players_pos"] = (zone_of(ctx), m["x"], m["y"], m["z"], m["dir"])
    n = 0
    for o in peers(ctx):
        n += push(o, notify_update(wire_cid(my_cid(ctx), my_cid(o)), ctx["players_pos"]))
    return n


def _hide(ctx):
    """Remove this session's character from the clients of its (old) zone."""
    n = 0
    for o in peers(ctx):
        n += push(o, notify_delete(wire_cid(my_cid(ctx), my_cid(o))))
    return n


def on_zone_leave(ctx):
    """The client asked to enter a field again (04): hide it until the next 23 5a."""
    if not ready(ctx):
        return 0
    n = _hide(ctx)
    ctx["players_ready"] = False
    ctx["players_pos"] = None
    return n


def on_leave(ctx):
    """Hook for the session's end (before social.unregister): hide the character and drop it
    from its party."""
    if not ctx:
        return 0
    from . import party                                 # party imports players
    party.on_leave(ctx)
    n = on_zone_leave(ctx) if enabled() else 0
    ctx["players_ready"] = False
    return n


def say_to_zone(ctx, text, sub=1):
    """NOTIFY_CHAT 0D <sub> 01 <wire cid> <text> to the other players of the zone (each gets the
    speaker's wire cid so UI 0xca finds its entry and names the line). Returns the count."""
    if not enabled() or not ready(ctx):
        return 0
    n = 0
    for o in peers(ctx):
        n += push(o, social.notify_chat(wire_cid(my_cid(ctx), my_cid(o)), text, sub=sub))
    return n


# ---------------------------------------------------------------------------- requests
def target_answer(payload, ctx):
    """25 01 <seq> <u32 cid> for a player's wire cid: res 0 + struct 0x1f0dc0 (npc.target_info)."""
    d = npc.decode_target_set(payload)
    if not is_player_wire(d["cid"]):
        return None                                     # NPCs and unknown cids: npc.py / quests.py
    o = session_by_cid(real_cid(d["cid"]))
    ctx["target"] = d["cid"]
    if o is None or not ready(o) or zone_of(o) != zone_of(ctx):
        return [npc.target_reply(d["seq"], -1)]
    info = npc.target_info(dict(o["char"], cid=d["cid"]), position(o))
    return [npc.target_reply(d["seq"], 0, info)]


def answer(payload, ctx, store=None):
    """Called first in world.answer. Returns replies for 25 01 on a player, else None (04 and
    05 05 are observed here, the world keeps answering them)."""
    if not enabled() or not payload:
        return None
    cmd = payload[0]
    if cmd == 4:
        on_zone_leave(ctx)
        return None
    if cmd == 5 and len(payload) >= 16 and payload[1] == 5:
        on_move(ctx, chars.decode_move(payload))
        return None
    if cmd == 0x25 and len(payload) >= 8 and payload[1] == 1:
        return target_answer(payload, ctx)
    return None


def describe(payload, direction="S>C"):
    if len(payload) < 2 or payload[0] != 0x0B:
        return None
    try:
        r = vce.Reader(payload, 2)
        if payload[1] == 4 and len(payload) >= 11:
            uid, cid = r.u32(), r.u32()
            return f"NOTIFY_CHAR CHAR_DELETE uid={uid:#x} cid={cid:#x}"
        if payload[1] == 8 and len(payload) >= 3:
            n = r.u8()
            recs = []
            for _ in range(n):
                kind, cid, x, y, z, d, st = r.u8(), r.u32(), r.u32(), r.u32(), r.u32(), r.u16(), r.u8()
                recs.append(f"kind={kind} cid={cid:#x} pos=({x},{y},{z}) dir={d}")
            return "NOTIFY_CHAR CHAR_LOCATION_UPDATE " + "; ".join(recs)
    except vce.WireError:
        return None
    return None
