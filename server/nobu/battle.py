"""Battle entry / exit (server cmd 0x0F NOTIFY_BATTLE) and a debug trigger to force the BATTLE scene.

Addresses. world.answer() / describe() call answer() / describe() first; None = not ours.

S>C cmd 0x0F (KNOWN layouts; parser branch 0x19ff60 in DoResponse 0x19e7b0, u8 sub read signed,
sub < 0xd, table 0x421160; every sub builds its struct through the archive class and calls
obj+0x28(cmd, sub, struct) -> main handler 0x188530 branch 0x18a590, table 0x4205e0):
  sub 1, 0xa, 0xc  reader 0x1a6770  BATTLE_INFO: u32 +0, u32 +4, u32 +8, u32 +0xc, u8 +0x10, +0x11,
                                    +0x12, +0x13, +0x14, u8 n, n x u16 +0x16.. (n <= 11)
  sub 2, 3, 4, 0xb reader 0x1a6b30  u32 +0, u32 +4, u8 +8, u8 +9
  sub 5            reader 0x1a6c80  u32
  sub 6            reader 0x1a6cf0  u32, u8
  sub 7            reader 0x1a6dc0  u32 x4 (BATTLE_LOCATION, log "you are already in battle at (%d:%d,%d,%d)")
  sub 8            reader 0x1a64f0  u32 cid, u8 kind, [u32, u32, u32 unless kind == 2]
  sub 9            reader 0x1a66a0  u32, u32
Main handler per sub:
  1    log NOTIFY_CODE_BATTLE_INFO; net+0x54 = +0x10, net+0x55 = +0x14, net+0x5a.. = the 11 u16;
       0x168650 (clears the battle scratch gp-0x7870..); if the local player task (type 8, id gp-0x7100)
       exists with flag bit 0 (+0xa & 1, set in world-invisible: 0x4c83) -> nothing more; else if the
       scene is already 5 and gp-0x7870 == 0 -> net state 0x19; else root task {1, kind 2, event 5}
       = switch to scene 5 (BATTLE).
  0xa  same copy, 0x168650, then root {1, kind 2, 5} UNCONDITIONALLY -> BATTLE scene. Used by the
       debug trigger.
  0xc  same copy, 0x1af150 (flag at 0x4a858c = 1); player task alive -> player event 0x14 data 0x53
       (a motion) + transition task (type 0xa) event 0x7d4 carrying UI 0x145; else like sub 1.
       HYPOTHESIS: the "encounter" start with an animation before the switch.
  4    0x1af100 (flag at 0x4a858c = 0); battle task 0x95 exists -> task 0x95 event 1 with the struct;
       else (if +4 == UI+0x40, our cid?) UI / net resets and root {1, kind 2, 4} = back to WORLD.
  6    task 0x95 exists -> task 0x95 event 2; else root {1, kind 2, 4} = back to WORLD.
  7    log BATTLE_LOCATION; scene != 5 -> root {1, kind 2, 5}.
  8    UI 0xdd -> 0x1d9aa0: character entry (by cid) +0x3c = +4, +0x40 = +0xc (0 when kind 2),
       0x1d9c50(entry, +8, kind). The same fields come from CHAR_INFO +0x70/+0x74/+0x78.
  9    UI 0xdc: every entry with +0x3c == first u32 gets +0x40 = second u32.
  0xb  task 0x95 exists -> task 0x95 event 1 with the struct; 0x168650.
  2, 3, 5: no main-handler case (parsed and dropped).
Scene 5 start 0x158700 (root kind 2 event 5, after the overlay loader 0x158930 loaded BATTLE.EBN):
creates task 0x95 and sends it {kind 2, event 1, net+0x54, net+0x55, zone net+0x50, u16[11] net+0x5a},
then posts the net task {kind 2, event 5}.

C>S (KNOWN): there is no 0x0F builder in the main ELF. Engaging a target uses the same requests as
talking: `25 01 <seq> <u32 cid>` (target set) then ○ in range -> 0x611880 -> net 0x81 -> `28 01 <seq>`.
Whether the target is attackable is decided by the client from the character entry (vt 0x40 =
0x2733a0): record +0x6b (entry +0x2c) == 2 -> never; faction (record +0x3f, entry +0x30) & 0x7f == 0 ->
attackable; zone 200..299 and a different +0x42 -> attackable; else by record +0x6c (entry +0x2b):
0 -> yes, 1 -> only if +0x6b == 0, 2..5 -> no. The action hint 0x62d3d0 then shows "○Attack" (0x0a).
In the battle scene a say line goes through net 0x4d -> 0x195290 -> builder 0x1cc2f0:
`23 78 <u16 seq> <str line>` (max 0x1fd), not 23 08.

Debug trigger (this module):
  chat line "battle" (also "/battle", "!battle", "battle", "battle") in 23 08 or 23 78:
      battle            -> 0F <NOBU_BATTLE_SUB, default 0a> BATTLE_INFO (loads BATTLE.EBN)
      battle 1 | a | c  -> that BATTLE_INFO sub; further numbers: +0x10, +0x14, then the u16 ids
      battle end        -> 0F 04   battle leave -> 0F 06   battle loc -> 0F 07
  NOBU_DEBUG_ENEMY=1: debug_spawn(ctx) spawns a faction-0 uid-0 character ("Bandit") next to the
      player; 25 01 on it is answered, 28 01 (○) on it starts the battle like the chat command.
Env: NOBU_BATTLE_SUB (1 / 0xa / 0xc), NOBU_BATTLE_B10, NOBU_BATTLE_B14, NOBU_BATTLE_IDS (comma list),
     NOBU_BATTLE_CHAT=1 enables the chat trigger, NOBU_ENEMY_CID / _DX / _DZ / _DIR / _FAMILY
     / _JOB.
"""
import os
import unicodedata

from . import chars, npc, social, vce
from . import enemies as field_enemies              # field enemies

CMD = 0x0F
SUB_NAMES = {1: "BATTLE_INFO", 2: "sub 2", 3: "sub 3", 4: "BATTLE_END", 5: "sub 5", 6: "BATTLE_LEAVE",
             7: "BATTLE_LOCATION", 8: "CHAR_BATTLE_STATE", 9: "BATTLE_UPDATE", 0xA: "BATTLE_INFO_NOW",
             0xB: "BATTLE_EVENT", 0xC: "BATTLE_INFO_ENCOUNTER"}
INFO_SUBS = (1, 0xA, 0xC)
REF_SUBS = (2, 3, 4, 0xB)
MAX_IDS = 11
TRIGGERS = ("battle",)
DEFAULT_ENEMY_CID = 0xFEB00001     # NPC cid range (npc.is_npc_cid), apart from 0xfea0.... NPCs


# ---------------------------------------------------------------------------- encoders (S>C)
def battle_info(sub=0xA, bid=1, x=0, y=0, z=0, b10=0, b11=0, b12=0, b13=0, b14=0, ids=()):
    """0F <1|a|c> u32 x4, u8 x5, u8 n, n x u16 (reader 0x1a6770)."""
    if sub not in INFO_SUBS:
        raise ValueError(f"sub {sub:#x} is not a BATTLE_INFO sub")
    ids = list(ids)
    if len(ids) > MAX_IDS:
        raise ValueError("at most 11 u16 ids")
    out = vce.put_u8(CMD) + vce.put_u8(sub)
    for v in (bid, x, y, z):
        out += vce.put_u32(v & 0xFFFFFFFF)
    for v in (b10, b11, b12, b13, b14):
        out += vce.put_u8(v & 0xFF)
    out += vce.put_u8(len(ids))
    for v in ids:
        out += vce.put_u16(v & 0xFFFF)
    return out


def battle_ref(sub, a=0, b=0, c8=0, c9=0):
    """0F <2|3|4|b> u32, u32, u8, u8 (reader 0x1a6b30)."""
    if sub not in REF_SUBS:
        raise ValueError(f"sub {sub:#x} does not use the 0x1a6b30 layout")
    return (vce.put_u8(CMD) + vce.put_u8(sub) + vce.put_u32(a) + vce.put_u32(b)
            + vce.put_u8(c8) + vce.put_u8(c9))


def battle_end(bid=1, cid=0):
    """0F 04: back to the field (task 0x95 absent) or event 1 to the battle task."""
    return battle_ref(4, bid, cid)


def battle_leave(bid=1, b4=0):
    """0F 06 u32, u8 (reader 0x1a6cf0)."""
    return vce.put_u8(CMD) + vce.put_u8(6) + vce.put_u32(bid) + vce.put_u8(b4)


def battle_u32(v=0):
    """0F 05 u32 (reader 0x1a6c80)."""
    return vce.put_u8(CMD) + vce.put_u8(5) + vce.put_u32(v)


def battle_location(bid, x, y, z):
    """0F 07 u32 x4 (reader 0x1a6dc0), log "(%d:%d,%d,%d)" = id:x,y,z."""
    return vce.put_u8(CMD) + vce.put_u8(7) + b"".join(vce.put_u32(v & 0xFFFFFFFF) for v in (bid, x, y, z))


def char_battle_state(cid, kind, a=0, b=0, c=0):
    """0F 08 u32 cid, u8 kind, [u32 a (-> entry +0x3c), u32 b, u32 c (-> +0x40)] unless kind == 2."""
    out = vce.put_u8(CMD) + vce.put_u8(8) + vce.put_u32(cid) + vce.put_u8(kind)
    if kind != 2:
        out += vce.put_u32(a) + vce.put_u32(b) + vce.put_u32(c)
    return out


def battle_update(a, b):
    """0F 09 u32, u32 (reader 0x1a66a0): entries with +0x3c == a get +0x40 = b."""
    return vce.put_u8(CMD) + vce.put_u8(9) + vce.put_u32(a) + vce.put_u32(b)


# ---------------------------------------------------------------------------- client model
def parse(payload):
    """Model of the client's cmd 0x0F parse (0x19ff60 + readers). Raises vce.WireError when the
    client would fail (short read) and ValueError for a sub it drops."""
    r = vce.Reader(payload, 1)
    sub = r.i8()
    if not 0 < sub < 0xD:
        raise ValueError(f"sub {sub} is not parsed")
    d = dict(sub=sub, name=SUB_NAMES[sub])
    if sub in INFO_SUBS:
        d.update(bid=r.u32(), x=r.u32(), y=r.u32(), z=r.u32())
        d["b10"], d["b11"], d["b12"], d["b13"], d["b14"] = (r.u8() for _ in range(5))
        n = r.u8()
        d["ids"] = [r.u16() for _ in range(min(n, MAX_IDS))]
    elif sub in REF_SUBS:
        d.update(a=r.u32(), b=r.u32(), c8=r.u8(), c9=r.u8())
    elif sub == 5:
        d.update(a=r.u32())
    elif sub == 6:
        d.update(a=r.u32(), b4=r.u8())
    elif sub == 7:
        d.update(bid=r.u32(), x=r.u32(), y=r.u32(), z=r.u32())
    elif sub == 8:
        d.update(cid=r.u32(), kind=r.u8())
        if d["kind"] != 2:
            d.update(a=r.u32(), b=r.u32(), c=r.u32())
    elif sub == 9:
        d.update(a=r.u32(), b=r.u32())
    d["left"] = len(r.rest())
    return d


def scene_effect(sub, player_active=True, scene=4):
    """What the main handler does with a parsed sub (0x18a590): 'battle' = root scene 5,
    'world' = root scene 4 (when no battle task exists), 'task95' = forwarded to the battle task,
    'ui' = a UI event, None = stored only."""
    if sub == 0xA:
        return "battle"
    if sub in (1, 0xC):
        if sub == 1 and player_active:
            return None
        if scene == 5:
            return "state19"
        return "battle" if sub == 1 else "transition"
    if sub == 7:
        return None if scene == 5 else "battle"
    if sub in (4, 6):
        return "task95" if scene == 5 else "world"
    if sub == 0xB:
        return "task95" if scene == 5 else None
    if sub in (8, 9):
        return "ui"
    return None


# ---------------------------------------------------------------------------- debug trigger
def _num(tok):
    return int(tok, 16) if tok.lower().startswith("0x") or any(c in "abcdef" for c in tok.lower()) \
        else int(tok)


def parse_command(text):
    """'battle', '/battle c 1 0 206', '！ｂａｔｔｌｅ end' -> (action, args) or None."""
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    t = t.lstrip("/!").strip()
    if not t:
        return None
    toks = t.split()
    if toks[0] not in TRIGGERS:
        return None
    rest = toks[1:]
    if rest and rest[0] in ("end", "leave", "flee", "exit", "loc", "state", "revive"):
        return rest[0], rest[1:]
    try:
        return "start", [_num(x) for x in rest]
    except ValueError:
        return None


def _env_int(name, default):
    v = os.environ.get(name)
    return default if v in (None, "") else int(v, 0)


def _position(ctx):
    m = ctx.get("pos")
    if m:
        return m["x"], m["y"], m["z"]
    _, x, y, z, _ = chars.char_position(ctx.get("char"))
    return x, y, z


def start_frames(ctx, args=()):
    """BATTLE_INFO that loads BATTLE.EBN. args = [sub, b10, b14, ids...] (all optional)."""
    args = list(args)
    sub = args.pop(0) if args else _env_int("NOBU_BATTLE_SUB", 0xA)
    if sub not in INFO_SUBS:
        sub = 0xA
    b10 = args.pop(0) if args else _env_int("NOBU_BATTLE_B10", 1)     # stage number 1..213 (0 is clamped to 1)
    b14 = args.pop(0) if args else _env_int("NOBU_BATTLE_B14", 0)
    ids = args or [int(v, 0) for v in os.environ.get("NOBU_BATTLE_IDS", "").split(",") if v.strip()]
    if not ids:                                     # preload the field enemy's creature model (u16[11] = model ids)
        m = (field_enemies.get(ctx.get("battle_target")) or {}).get("model", 0)
        ids = [m] if m else []
    b = ctx.setdefault("battle", {})
    bid = b.get("bid", 0) + 1
    x, y, z = _position(ctx)
    b.update(bid=bid, sub=sub, active=True, pos=(x, y, z))
    return [battle_info(sub, bid, x, y, z, b10=b10, b14=b14, ids=ids[:MAX_IDS])]


def command_frames(action, args, ctx):
    b = ctx.setdefault("battle", {})
    bid = b.get("bid", 1)
    cid = ctx.get("cid") or (ctx.get("char") or {}).get("cid", 0)
    if action == "revive":
        return revive(ctx), "debug battle: revive"
    if action == "start" and (ctx.get("char") or {}).get("ghost"):
        return [], "debug battle: ghosts cannot fight (Pass On or revive first)"
    if action == "start" and in_battle(ctx):
        return [], "debug battle: a battle is already running (say 'battle exit' first)"
    if action == "start":
        frames = start_frames(ctx, args) + (fight_setup(ctx) if fight_enabled() else [])
        note = f"debug battle: sent 0F {frames[0][1]:02x} BATTLE_INFO (battle id {ctx['battle']['bid']})"
    elif action == "end":
        b["active"] = False
        frames, note = [battle_end(bid, cid)], "debug battle: sent 0F 04 (end)"
    elif action in ("leave", "flee", "exit"):
        frames = finish(ctx) or [battle_leave(bid)]
        note = "debug battle: sent 0F 06 (exit)"
    elif action == "loc":
        x, y, z = _position(ctx)
        frames, note = [battle_location(bid, x, y, z)], "debug battle: sent 0F 07 (location)"
    else:                                               # state: mark ourselves in battle
        frames, note = [char_battle_state(cid, 1, bid, 0, 0)], "debug battle: sent 0F 08"
    return frames, note


def decode_chat_any(payload):
    """23 08 (field say) and 23 78 (battle-scene say, builder 0x1cc2f0): u16 seq, str line."""
    r = vce.Reader(payload, 2)
    seq = r.u16()
    line = r.bytes(0x200).decode("utf-8", "replace")
    ch, text = social.parse_line(line)
    return dict(seq=seq, line=line, channel=ch, text=text)


# ---------------------------------------------------------------------------- debug enemy
def chat_enabled():
    """The say-chat trigger ("battle", "battle exit", ...) is a debug tool: explicit NOBU_BATTLE_CHAT=1 / 0 wins,
    otherwise it is on only on debug rigs (NOBU_BATTLE_FIGHT=1 or NOBU_DEBUG_ENEMY=1)."""
    v = os.environ.get("NOBU_BATTLE_CHAT")
    if v in ("0", "1"):
        return v == "1"
    return os.environ.get("NOBU_BATTLE_FIGHT") == "1" or os.environ.get("NOBU_DEBUG_ENEMY") == "1"


def in_battle(ctx):
    return (ctx.get("battle") or {}).get("phase") in ("setup", "turns", "end")


def debug_enabled():
    return os.environ.get("NOBU_DEBUG_ENEMY") == "1"


def debug_enemy(player):
    lk = npc.npc_looks(player, "bandit", job=_env_int("NOBU_ENEMY_JOB", (list(player.get("looks", []))
                                                                         + [0] * 5)[3]))
    lk[4] = 0                                        # faction 0 -> attackable (0x2733a0)
    cid = _env_int("NOBU_ENEMY_CID", DEFAULT_ENEMY_CID) & 0xFFFFFFFF
    npc.check_npc_cid(cid, "NOBU_ENEMY_CID")        # uid 0 is drawn only with a 0xfe cid (UI 0xd4)
    return dict(cid=cid, uid=0, family=os.environ.get("NOBU_ENEMY_FAMILY", "Bandit"), given="",
                looks=lk, level=player.get("level", 1))


def debug_spawn(ctx):
    """NOBU_DEBUG_ENEMY=1: CHAR_INFO for one attackable uid-0 character beside the player
    (append to world.world_ready_pushes). Remembers it in ctx["enemies"]."""
    player = ctx.get("char")
    if not player or not debug_enabled():
        return []
    import time
    t = ctx.get("enemy_defeated_at")
    if t is not None:
        left = _env_int("NOBU_BATTLE_RESPAWN", 30) - (time.monotonic() - t)
        if left > 0:
            # defeated: stays away for NOBU_BATTLE_RESPAWN s, then comes back through the session push
            if not ctx.get("enemy_respawn_pending") and schedule(ctx, left, lambda: respawn(ctx)):
                ctx["enemy_respawn_pending"] = True
            return []
        ctx.pop("enemy_defeated_at", None)
    e = debug_enemy(player)
    zone, x, y, z, _ = chars.char_position(player)
    pos = (zone, x + _env_int("NOBU_ENEMY_DX", -300), y, z + _env_int("NOBU_ENEMY_DZ", 0),
           _env_int("NOBU_ENEMY_DIR", 0) % 360)
    e["pos"] = list(pos)
    ctx.setdefault("enemies", {})[e["cid"]] = e
    return [chars.notify_char_info([chars.char_info_record(e, 0, pos)])]


def respawn(ctx):
    ctx.pop("enemy_respawn_pending", None)
    ctx.pop("enemy_defeated_at", None)
    if in_battle(ctx):
        return []
    return debug_spawn(ctx)


def despawn(ctx):
    """0B 04 CHAR_DELETE for every debug enemy (players.notify_delete: uid 0 + 0xfe cid -> UI 0xdf)."""
    from . import players
    return [players.notify_delete(cid) for cid in (ctx.get("enemies") or {})
            if not field_enemies.is_enemy_cid(cid)]           # field enemies: enemies.on_defeated


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Frames for the battle-related client frames, or None when not ours."""
    if len(payload) >= 2 and payload[0] == PKT and fight_enabled():
        return fight_answer(payload, ctx, store)
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x23 and sub == 0x5A and (ctx.pop("defeat_pending", False) or (ctx.get("char") or {}).get("ghost")):
        # back in the field after a lost battle (or a ghost reloading a map): world.answer still spawns the
        # player; the death frames follow a moment later (after the spawn CHAR_INFO, whose PARAMS they override)
        fn = (lambda: ghost_frames(ctx, store)) if ghost_enabled() else (lambda: defeat(ctx, store))
        if not schedule(ctx, float(os.environ.get("NOBU_BATTLE_DEATH_DELAY", "1")), fn):
            ctx["defeat_frames"] = fn()                  # no session loop (tests): kept for the caller
        return None
    if cmd == 0x28 and sub == 1 and (ctx.get("char") or {}).get("ghost") and ghost_enabled():
        return pass_on(ctx, store)                       # 28 01 as a ghost = Pass On (hint 8, 0x62cac0)
    if cmd == 0x23 and sub == 0x08 and len(payload) >= 4 and not in_battle(ctx):
        try:
            d0 = decode_chat_any(payload)
        except vce.WireError:
            d0 = None
        if d0 and unicodedata.normalize("NFKC", d0["text"]).strip().lower() == "help":
            S = open_aid(ctx)
            if S is not None:
                return social.chat_replies(d0, ctx) + join_frames(S, ctx)
    if cmd == 0x23 and sub in (0x08, 0x78) and chat_enabled():
        try:
            d = decode_chat_any(payload)
        except vce.WireError:
            return None
        got = parse_command(d["text"])
        if got is None:
            return None
        frames, note = command_frames(got[0], got[1], ctx)
        ctx.setdefault("battle_log", []).append((d["line"], [f.hex() for f in frames]))
        out = []
        if sub == 0x08:
            out += social.chat_replies(d, ctx)          # keep the say echo
        return out + [social.notify_system(note)] + frames
    enemies = ctx.get("enemies") or {}
    if cmd == 0x25 and sub == 1 and len(payload) >= 8:
        t = npc.decode_target_set(payload)
        e = enemies.get(t["cid"])
        if e is None:
            return None
        ctx["target"] = t["cid"]
        return [npc.target_reply(t["seq"], 0, npc.target_info(e, tuple(e["pos"])))]
    if cmd == 0x28 and sub == 1 and ctx.get("target") in enemies:
        if in_battle(ctx):
            return []                                    # one battle at a time
        if field_enemies.is_enemy_cid(ctx["target"]) and not field_enemies.engage(ctx, ctx["target"]):
            return [social.notify_system("Someone is already fighting that enemy.")]
        ctx["battle_target"] = ctx["target"]
        return start_frames(ctx) + (fight_setup(ctx) if fight_enabled() else [])
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if cmd == CMD:
            d = parse(payload)
            fields = " ".join(f"{k}={v:#x}" if isinstance(v, int) else f"{k}={v}"
                              for k, v in d.items() if k not in ("sub", "name", "left"))
            return f"NOTIFY_BATTLE {d['name']} (sub {sub:#x}) {fields}"
        if cmd == PKT:
            return describe22(payload, direction)
        if direction == "C>S" and cmd == 0x23 and sub == 0x78 and len(payload) >= 4:
            d = decode_chat_any(payload)
            return f"CMD_23 sub 120 (battle-scene say) seq={d['seq']} {d['line']!r}"
        if direction == "C>S" and cmd == 0x23 and sub == 0x08 and len(payload) >= 4:
            d = decode_chat_any(payload)
            got = parse_command(d["text"])
            if got:
                return f"CHAT debug battle command {got[0]} {got[1]} seq={d['seq']} {d['line']!r}"
    except (vce.WireError, ValueError):
        return None
    return None


# ============================================================================ 1v1 fight (NOBU_BATTLE_FIGHT=1)
# "Minimum viable fight". Layouts are KNOWN (0x22 catalog); the sequence and the field meanings (Life/Ki,
# EXP, which side is ours, the action / end action codes) are HYPOTHESIS, hence the knobs:
#   NOBU_BATTLE_FIGHT=1        use this flow (0 = bare 0F 0a trigger only)
#   NOBU_BATTLE_OWN_SIDE       a (default: player slot 0, Bandit slot 7) | b (player slot 7, Bandit slot 0)
#   NOBU_BATTLE_END_ACTION     end action code played by the loser (default 0x157f; 0x158f / 0x1591)
#   NOBU_BATTLE_AC_HEX         override the whole 24-byte AC of the attack (hex)
#   NOBU_BATTLE_END_AC_HEX     override the whole 24-byte AC of the end action (hex)
#   NOBU_BATTLE_TICK           seconds between 22 18 while waiting for 22 32 (default 1.0)
#   NOBU_BATTLE_TICK_DELAY     seconds before the first 22 18 (default 2.0)
#   NOBU_BATTLE_LIFE / _KI     Life / Ki of both units (default 100 / 50)
#   NOBU_BATTLE_BANNER         22 5d seconds (default 5; 0 = no 22 5d)
#   NOBU_BATTLE_CLOCK          1 (default) = send 22 11 battle clock at setup, on 22 32 and every tick
#   NOBU_BATTLE_T0             battle clock start (22 10 / 22 02 first i32, default 1)
#   NOBU_BATTLE_CLOCK_LEAD     seconds the 22 11 clock runs ahead of real time (default 0; clock starts at 22 32)
#   NOBU_BATTLE_WINDOW         seconds the command menu stays open (record 1 duration, default 200, max 255)
#   NOBU_BATTLE_MENU_ROOT      command tree node of the menu (default 0x11, table 0x3e7e20)
#   NOBU_BATTLE_MENU_B5        22 10 u8@5 (menu+0x82, item types 7/8; default 1)
#   NOBU_BATTLE_ATTACK_ID      22 33 item value (default 1; comes back as the 22 01 action, HYPOTHESIS)
#   NOBU_BATTLE_ATTACK_KEY     22 33 item key (default 1 = the Attack entry of node 0x11)
#   NOBU_BATTLE_INTRO          1 (default) = record 1 is an intro action (menu opens in its phase B); 0 = window
#   NOBU_BATTLE_INTRO_CODE     intro action code (default 0x157e, the client's fallback camera scene)
#   NOBU_BATTLE_INTRO_SECS     intro duration (default 3)
#   NOBU_BATTLE_INFO_REPLY     1 (default) = answer 22 42 / 22 41 with 22 44 (unlocks the menu)
#   NOBU_BATTLE_INFO_TEXT      description text in that reply
#   NOBU_BATTLE_B10            stage number in 0F 0a (default 1; 1..213)
#   NOBU_BATTLE_ATTACK_TTYPE   22 33 target type (default 1 = single enemy; 0 greys the entry)
#   NOBU_BATTLE_PLAYER_FLAGS / _ENEMY_FLAGS / _DEAD_FLAGS   US u16@10 target flags (0x0b / 0x0c / 0x14)
#   NOBU_BATTLE_PROF / _FAME   result u32[1] Prof. / u32[2] Fame (default 5 / 1); _EXP u32[0] (10);
#                              _MONEY u32[4] in mon (default 30)
#   NOBU_BATTLE_AWARD          1 (default) = add exp / prof / fame to chars.json and money to inventory
#   NOBU_BATTLE_ATTACK_CODE    scene code of the attack record (default 4 = kill; 1 = damage, 0x1f / 0x34 other
#                              weapon groups); NOBU_BATTLE_ATTACK_VARIANT camera variant 0..3 (default 0)
#   NOBU_BATTLE_ACT_SECS / _END_SECS / _END_GAP / _ACT_LEAD   attack dur 8, end dur 5, gap 1, start clock + 1
#   NOBU_BATTLE_REC_STATE      attack record state (default 1 = timed)
#   NOBU_BATTLE_DEAD_STATUS    US status byte of the dead Bandit (default 1 = KO bit)
#   NOBU_BATTLE_AC_HEX / _END_AC_HEX   send a 22 5c with this 24-byte AC (default: no 22 5c; byte 0 must be 0)
#   NOBU_BATTLE_RESULT_DELAY   seconds between the attack records and 22 5d / 22 25 (default 0 = same burst)
#   NOBU_BATTLE_EXIT_DELAY     seconds from 22 67 to the server's 0F 06 (default 2; 0 = immediately)
#   NOBU_BATTLE_RESPAWN        seconds before a defeated debug enemy comes back (default 30)
#   NOBU_BATTLE_SETUP_CUR_UNITS 1 (default) = 22 1c for both units at setup (current record's unit grids)
#   NOBU_BATTLE_SETUP_STAGING  1 (default) = 22 02 staging record with the unit ids at setup (0 = omit)
PKT = 0x22
MAX_TICKS = 1800

C2S_NAMES = {0x01: "COMMAND", 0x26: "ITEM_INFO", 0x28: "LOOT_PICK", 0x32: "READY", 0x3d: "END_PH3", 0x3f: "MENU3",
             0x40: "MENU2", 0x41: "INFO_ITEM", 0x42: "INFO_ACTION", 0x4d: "DISCARD_UNIQUE", 0x4f: "DISCARD",
             0x51: "ITEM_INFO_UID", 0x53: "LOOT_BEGIN", 0x54: "LOOT_END", 0x55: "LOOT_CONFIRM",
             0x56: "END_PH6", 0x5e: "END_PH2", 0x62: "SCENE_LOADED", 0x68: "END_PH9", 0x6a: "MENU6"}
S2C_NAMES = {0x02: "SIDE_B", 0x03: "UNIT_READY", 0x0f: "RESULT_STATE", 0x10: "INIT_SIDE_A", 0x11: "TIMER",
             0x18: "TURN", 0x1a: "UNIT_JOIN", 0x1c: "RESULT_STATE2", 0x1e: "SIDE_B_DONE", 0x1f: "UNIT_ID",
             0x25: "RESULT", 0x27: "ITEM_DETAIL", 0x3c: "SLOT_DATA", 0x3e: "END_MSG", 0x47: "LOOT_DONE",
             0x52: "END_TO_PH6", 0x5b: "ACTIONS2", 0x5c: "ACTIONS", 0x5d: "BANNER", 0x5f: "END_PH2_REPLY",
             0x65: "SIDE_B_COMPLETE", 0x66: "SIDE_A_COMPLETE", 0x67: "END_TO_PH8", 0x6e: "SIDE_NAMES"}


def fight_enabled():
    return os.environ.get("NOBU_BATTLE_FIGHT", "0") == "1"


def pkt(t, body=b""):
    return bytes([PKT, t]) + bytes(body)


def _clip(text, n):
    from . import misc
    return misc.clip_sjis(text or "", n)


# ---------------------------------------------------------------------------- struct writers
def w_bi(unit_ids, code=0, kind=0, dur=0, prog=0, actor=0, target=0, third=0xFF, cam=0, name="", list_a=(),
         list_b=(), rout=(0, 0)):
    """BI 0x1b3dc0: u16 x5, u8 x11, u32 unit_id[14], u8[14], u8[14], str(0x21), u8, u8, u16 x4, u8[2], u8[2].
    Built into the record's R by 0x1b9a10 (as the client reads it): u16@0 = action code (camera-scene
    table index), u8@10 = kind (1..3 = action playback), u8@11 = duration in seconds, u8@12 = progress,
    u8@13/@14/@15 = slots of the actor / target / third unit (third 0xff = none), u8@16 = camera variant (0..3),
    then u32 unit_id[14] and two u8[14] slot lists, A (units hit, scene roles 7..) and B (helpers, roles 1..6),
    each terminated by a value >= 14 (KNOWN, 0x1671e0 / 0x614360: zeros meant 14 x slot 0 = the player)."""
    ids = (list(unit_ids) + [0] * 14)[:14]
    la = (list(list_a) + [0xFF] * 14)[:14]
    lb = (list(list_b) + [0xFF] * 14)[:14]
    out = vce.put_u16(code) + bytes(8) + bytes([kind, dur, prog, actor, target, third, cam, 0, 0, 0, 0])
    out += b"".join(vce.put_u32(v) for v in ids) + bytes(la) + bytes(lb)
    # tail: u8, u8, u16 x4, u8[2] A, u8[2] B. B[side] = 1 leader fallen / 2 fled: at the end of this record the
    # side is routed and the battle log shows UIMSG 3.36 / 3.37 (own side) or 3.38 / 3.39 (enemy) (0x164ea0, KNOWN)
    return out + vce.put_bytes(_clip(name, 0x20).encode("utf-8")) + bytes(2 + 8 + 2) + bytes(list(rout)[:2])


def w_ap(leader0=0, leader1=0, routed0=0, routed1=0):
    """AP 0x1b3b30 (10 B): u32 leader id side 0, u32 side 1, u8 routed side 0, u8 side 1 (M+0x14054 / +0x1405c:
    22 10 AP3 at setup, record AP1 at start / AP2 at end; KNOWN, "Rout")."""
    return vce.put_u32(leader0) + vce.put_u32(leader1) + bytes([routed0, routed1])


def w_ar(root=0, mask=0):
    """AR 0x1b3920 (23 B): u16 x7, i8 x5, u32. u16[0] = command-menu root node (record +0x10 / +0x14, applied at
    phase A / B entry, 0x1b6d80 / 0x1b7050), u16[1] = status mask (menu+0x7c). KNOWN."""
    return vce.put_u16(root) + vce.put_u16(mask) + bytes(19)


AR = w_ar()         # 0x1b3920: u16 x7, i8 x5, u32
AP = bytes(10)      # 0x1b3b30: u32 x2, u8 x2


def w_ui(family, given, model=0, scale=0, looks=None):
    """UI 0x1b2d90: str(0x11), str(0x11), u8 x4, u16, u8 x2, u16, u8 x2, u16, u8 x12. KNOWN (0x2119a0 copies it to
    the unit, 0x624270 + 0x61c8c0 build the appearance struct A of the participant): u16 +0x26 -> unit+0x20 ->
    A[0..1] = the model (0 = person; else the MOB model id, loaded on demand by 0x162ea0 -> 0x5f8190), u8 +0x30
    (first of the 12) -> A[0xa] = the creature's scale (50 = 1.0, 0x255ed0). A person (model 0) takes the same A
    bytes the world builds from CHAR_INFO (0x1db0d0): +0x24 -> A[2] = (sex == 1), +0x25 -> A[3] = job (0 -> 1),
    +0x30 / +0x31 -> A[0xa] / A[0xb] body sliders, +0x32 -> A[0xc] hair, +0x33 -> A[0xd] face (chars.appearance).
    All 0 is the default male body every unit used to get."""
    mid, tail = bytes(2), bytes([scale & 0xFF if model else 0]) + bytes(11)
    if not model and looks:
        lk = (list(looks) + [0] * 20)[:20]
        mid = bytes([1 if int(lk[2]) == 1 else 0, (int(lk[3]) & 0xFF) or 1])
        tail = bytes(int(v) & 0xFF for v in (lk[8], lk[9], lk[6], lk[5])) + bytes(8)
    return (vce.put_bytes(_clip(family, 0x10).encode("utf-8")) + vce.put_bytes(_clip(given, 0x10).encode("utf-8"))
            + bytes(2) + mid + vce.put_u16(model & 0xFFFF) + bytes(2 + 2 + 2 + 2) + tail)


def w_us(life, max_life, ki, max_ki, flags=0, status=0):
    """US 0x1b3130 (29 B): u16 Life?, u16 max Life?, u8, u8, u16 Ki?, u16 max Ki?, u16 flags, i8, u8, u8 status,
    u8, u16, u16, u8 x4, u8[5]. u16@10 = unit+0x18 = the target-slot flags the command menu filters on
    (0x1b2790 / 0x211830 / 0x2175e0, KNOWN): 1 self?, 2 ally?, 4 enemy?, 8 alive / targetable?, 0x10 KO?
    (bit meanings HYPOTHESIS). Target type 1 (single enemy) keeps slots with flags & 4 and flags & 8."""
    # pairing KNOWN (0x211390 / 0x211420): Life = min(u16@0, u16@6), Ki = min(u16@2, u16@8); status u8@14
    # bit0 = KO (0x1671e0 shows the KO event only when the after-state has it)
    return (vce.put_u16(life) + vce.put_u16(ki) + b"\0\0" + vce.put_u16(max_life) + vce.put_u16(max_ki)
            + vce.put_u16(flags) + b"\0\0" + bytes([status]) + bytes(14))


def w_ac(action, target_slot=0, override=None):
    """AC 0x1b3540 (24 B): u8 x3, u16, u8 x2, u16, u8 x2, u16, u8 x11. HYPOTHESIS: u8 +0 = target slot,
    u16 (+4 in the object, wire bytes 3..4) = action id. NOBU_BATTLE_AC_HEX / _END_AC_HEX override it."""
    if override:
        return (bytes.fromhex(override) + bytes(24))[:24]
    return bytes([target_slot & 0xFF, 0, 0]) + vce.put_u16(action) + bytes(19)


# ---------------------------------------------------------------------------- S>C packets
def p_init_side_a(clock, bi, rec=0, start=0, state=0, ar1=AR, ar2=AR, b5=0, ap3=None):
    """22 10 = the CURRENT turn record (KNOWN): i32 clock (M+0), u8 (model+4), u8 b5 (menu+0x82, enables item
    types 7/8), u8, i32 record number, u32 start second, u8 state (0/2 immediate, 1/3 timed), u8, BI, AR1, AR2,
    AP, AP, AP, u8, i8. Resets the menu (0x2161a0): send 22 33 items after it."""
    return pkt(0x10, vce.put_u32(clock) + bytes([0, b5, 0]) + vce.put_u32(rec) + vce.put_u32(start)
               + bytes([state, 0]) + bi + ar1 + ar2 + AP + AP + (ap3 or AP) + bytes(2))


def p_side_b(rec, bi, start=0, state=0, ar1=AR, ar2=AR, ap1=None, ap2=None):
    """22 02 = the STAGING record (KNOWN): i32 record number, u32 start, u8 state, u8, BI, AR1, AR2, AP, AP,
    u8, i8. 22 0f / 22 5c write its unit states; 22 1e queues it."""
    return pkt(0x02, vce.put_u32(rec) + vce.put_u32(start) + bytes([state, 0]) + bi + ar1 + ar2 + (ap1 or AP)
               + (ap2 or AP) + bytes(2))


def p_menu_item(action_id, key, name, extra=0, ttype=1, cost_kind=0, cost=0, wait=0):
    """22 33 command list entry (KNOWN, parser 0x1c1230, copy 0x215700): u32 value (entry+0, != 0), u32 link
    (+0x14), u8 key (+4; type-2 menu entries show when an item with key == their value exists; Attack = 1),
    u8 (+8), u8 target type (+0xc; row of table 0x3e7d80; 0 = no targets = always greyed, 1 = single enemy),
    u8 cost kind (+0x10), u8 (+0x18), u8 (+0x19), u16 extra cost (+0x1a), u8 x3 (+0x1c..+0x1e), str name, u8."""
    return pkt(0x33, vce.put_u32(action_id) + vce.put_u32(extra) + bytes([key, 0, ttype, cost_kind, 0, 0])
               + vce.put_u16(cost) + bytes([wait & 0xFF, 0, 0]) + vce.put_bytes(_clip(name, 0x18).encode("utf-8"))
               + b"\0")   # +0x1c = wait seconds (0x21b500: the command window's 20-pip gauge, KNOWN)


def p_unit_id(slot, ui):
    return pkt(0x1F, bytes([slot]) + ui)


def p_unit_join(slot, ui, us):
    return pkt(0x1A, bytes([slot]) + ui + us)


def p_side_names(a, b):
    return pkt(0x6E, vce.put_bytes(_clip(a, 0x29).encode("utf-8")) + vce.put_bytes(_clip(b, 0x29).encode("utf-8"))
               + b"\0\0")


def p_slot_data(unit_ids):
    ids = (list(unit_ids) + [0] * 14)[:14]
    return pkt(0x3C, b"".join(vce.put_u32(v) + b"\0\0" for v in ids))


def p_order(entries):
    """22 3c: the timing bar (0x60c020, KNOWN): 14 x {u32 actor unit id, u8 seconds (16 px each), u8 record
    number & 0xff}, nonzero entries first; the entry whose byte equals the CURRENT record's number anchors the bar
    at the execute line; + u8[14] (0)."""
    ent = (list(entries) + [(0, 0, 0)] * 14)[:14]
    return pkt(0x3C, b"".join(vce.put_u32(i) + bytes([sec & 0xFF, rec & 0xFF]) for i, sec, rec in ent) + bytes(14))


def p_unit_state(uid, a=0, b=0, c=0):
    """22 03 <u32 id> a b c -> participant +0x6c..+0x6e (KNOWN): for the own unit b = wait pips on the command
    window, c != 0 = command entered; c == 0 clears the own menu lock."""
    return pkt(0x03, vce.put_u32(uid) + bytes([a, b, c]))


def p_unit_ready(cid):
    return pkt(0x03, vce.put_u32(cid) + b"\x01\x01\x01")


def p_actions(slot, ac1, ac2):
    return pkt(0x5C, bytes([slot]) + ac1 + ac2)


def p_result_state(slot, us_before, us_after):
    return pkt(0x0F, bytes([slot]) + us_before + us_after)


def p_cur_state(slot, us_a, us_b):
    """22 1c: u8 slot, US, US (0x1b7a70): copies G0[slot] (id, identity) into the CURRENT record's unit grids
    G3 / G4 (M+0x644 / +0xc2c) and applies US#1 / US#2. Needed because the phase
    entries 0x1b6d80 / 0x1b7050 clear the participant grid M+0x12fc and rebuild it from G3 / G4 only, so a record
    without them has no participants (gp-0x7164 = 0x13, no models, black screen)."""
    return pkt(0x1C, bytes([slot]) + us_a + us_b)


def p_info_reply(key, name="", text=""):
    """22 44: u16, MSG (u32, u8, u16, u16, u8, str(0x19), str(0x21), str(0x69)). Handler 0x1b0060 copies MSG to
    model+0x17f54 and UI 0x219f70 clears menu+0x78 and menu+0x81 (KNOWN): the answer that unlocks the menu
    after C>S 22 42 <u16 action> (0x21afa0 stored the action in menu+0x78 and set menu+0x81 = 1)."""
    return pkt(0x44, vce.put_u16(key) + vce.put_u32(0) + b"\0" + bytes(4) + b"\0"
               + vce.put_bytes(b"") + vce.put_bytes(_clip(name, 0x20).encode("utf-8"))
               + vce.put_bytes(_clip(text, 0x68).encode("utf-8")))


def p_banner(secs):
    return pkt(0x5D, vce.put_u16(secs))


def p_result(exp, prof=0, fame=0, money=0, win=1, u3=0):
    """22 25: u32 x5, u8 win, u32, u16, u16, u8, u8, u32, u8, u8, u16 (0 = skip end phase 2), i16[18],
    u8[50], u32, u8. Results window 0x604240 (KNOWN, rows by UIMSG label index): u32[0] Exp (getter 0x1b1a90,
    live screenshot: Exp 10), trailing u32 (model+0x18004) and u32 (+0x1800c) = two mastery rows shown only
    when enabled, u32[1] Prof. (0x1b1ad0, screenshot: Prof. 5), u32[2] Fame (0x1b1ae0), u32[4] Money in mon
    (0x1b1af0, shown as u32/1000 kan and u32%1000 mon). u32[3] has no getter here."""
    body = (b"".join(vce.put_u32(v) for v in (exp, prof, fame, u3, money)) + bytes([win]) + vce.put_u32(0)
            + bytes(6) + vce.put_u32(0) + bytes(2) + vce.put_u16(0) + bytes(36) + bytes(50)
            + vce.put_u32(0) + bytes(1))
    return pkt(0x25, body)


def p_ph2_reply(key):
    return pkt(0x5F, vce.put_u16(key) + vce.put_bytes(b""))


def p_end_msg(key):
    """22 3e: u8 key, MSG (u32, u8, u16, u16, u8, str, str, str)."""
    return pkt(0x3E, bytes([key]) + vce.put_u32(0) + b"\0" + bytes(4) + b"\0" + b"\0\0\0")


# ---------------------------------------------------------------------------- client models (S>C readers)
def _skip(r, n):
    for _ in range(n):
        r.u8()


def _r_bi(r):
    code = r.u16()
    _skip(r, 8)
    kind, dur, prog, actor, target, third, cam = (r.u8() for _ in range(7))
    _skip(r, 4)
    ids = [r.u32() for _ in range(14)]
    list_a = [r.u8() for _ in range(14)]
    list_b = [r.u8() for _ in range(14)]
    name = r.bytes(0x200).decode("utf-8")
    _skip(r, 10)
    tail_a, rout = [r.u8(), r.u8()], [r.u8(), r.u8()]
    return dict(ids=ids, name=name, code=code, kind=kind, dur=dur, prog=prog, actor=actor, target=target,
                third=third, cam=cam, list_a=list_a, list_b=list_b, rout=rout)


def _r_ar(r):
    root, mask = r.u16(), r.u16()
    _skip(r, 19)
    return dict(root=root, mask=mask)


def _r_ui(r):
    fam, giv = r.bytes(0x200).decode("utf-8"), r.bytes(0x200).decode("utf-8")
    _skip(r, 26)
    return dict(family=fam, given=giv)


def _r_us(r):
    life, ki = r.u16(), r.u16()
    _skip(r, 2)
    mlife, mki = r.u16(), r.u16()
    flags = r.u16()
    _skip(r, 2)
    status = r.u8()
    _skip(r, 14)
    return dict(life=life, max_life=mlife, ki=ki, max_ki=mki, flags=flags, status=status)


def parse22(payload):
    """Client read model of the S>C 0x22 packets this module sends (layouts from the 0x169830
    deserializers). Raises vce.WireError on a short read; 'left' = unread bytes (must be 0)."""
    r = vce.Reader(payload, 1)
    t = r.u8()
    d = dict(type=t, name=S2C_NAMES.get(t, f"type {t:#04x}"))
    if t == 0x10:
        d["bid"] = d["clock"] = r.i32()
        d["b4"], d["b5"], d["b6"] = r.u8(), r.u8(), r.u8()
        d["rec"], d["start"], d["state"] = r.i32(), r.u32(), r.u8()
        r.u8()
        d["bi"], d["ar1"], d["ar2"] = _r_bi(r), _r_ar(r), _r_ar(r)
        _skip(r, 20)
        d["ap3"] = dict(leaders=(r.u32(), r.u32()), routed=(r.u8(), r.u8()))
        _skip(r, 2)
    elif t == 0x02:
        d["rec"], d["start"], d["state"] = r.i32(), r.u32(), r.u8()
        r.u8()
        d["bi"], d["ar1"], d["ar2"] = _r_bi(r), _r_ar(r), _r_ar(r)
        d["ap1"] = dict(leaders=(r.u32(), r.u32()), routed=(r.u8(), r.u8()))
        d["ap2"] = dict(leaders=(r.u32(), r.u32()), routed=(r.u8(), r.u8()))
        _skip(r, 2)
    elif t == 0x33:
        d["value"], d["extra"], d["key"] = r.u32(), r.u32(), r.u8()
        r.u8()
        d["ttype"], d["cost_kind"] = r.u8(), r.u8()
        _skip(r, 2)
        d["cost"] = r.u16()
        _skip(r, 3)
        d["item_name"] = r.bytes(0x200).decode("utf-8")
        r.u8()
    elif t == 0x1F:
        d.update(slot=r.u8(), ui=_r_ui(r))
    elif t == 0x1A:
        d.update(slot=r.u8(), ui=_r_ui(r), us=_r_us(r))
        if not r.done():
            _skip(r, 10)
    elif t == 0x6E:
        d.update(a=r.bytes(0x200).decode("utf-8"), b=r.bytes(0x200).decode("utf-8"), c=r.u8(), d=r.u8())
    elif t == 0x3C:
        d["slots"] = [(r.u32(), r.u8(), r.u8()) for _ in range(14)]
        if not r.done():
            _skip(r, 14)
    elif t == 0x03:
        d.update(cid=r.u32(), b=(r.u8(), r.u8(), r.u8()))
    elif t in (0x5B, 0x5C):
        d.update(slot=r.u8(), ac1=bytes(r.u8() for _ in range(24)), ac2=bytes(r.u8() for _ in range(24)))
        if not r.done():
            _skip(r, 6)
    elif t in (0x0F, 0x1C):
        d.update(slot=r.u8(), before=_r_us(r), after=_r_us(r))
        if not r.done():
            _skip(r, 20)
    elif t == 0x5D:
        d["secs"] = r.u16()
    elif t == 0x25:
        d["u32"] = [r.u32() for _ in range(5)]
        d["exp"], d["prof"], d["fame"], d["money"] = d["u32"][0], d["u32"][1], d["u32"][2], d["u32"][4]
        d["win"] = r.u8()
        r.u32(), r.u16(), r.u16(), r.u8(), r.u8(), r.u32(), r.u8(), r.u8()
        d["w32"] = r.u16()
        _skip(r, 36 + 50)
        r.u32(), r.u8()
        if not r.done():
            r.u32()
        if not r.done():
            r.u32()
    elif t == 0x44:
        d["key"] = r.u16()
        r.u32(), r.u8(), r.u16(), r.u16(), r.u8()
        r.bytes(0x200)
        d["item_name"], d["text"] = r.bytes(0x200).decode("utf-8"), r.bytes(0x200).decode("utf-8")
    elif t == 0x5F:
        d.update(key=r.u16(), name=r.bytes(0x200).decode("utf-8"))
    elif t == 0x3E:
        d["key"] = r.u8()
        r.u32(), r.u8(), r.u16(), r.u16(), r.u8(), r.bytes(0x200), r.bytes(0x200), r.bytes(0x200)
    elif t == 0x11:
        d["secs"] = r.i32()
    elif t == 0x61:
        d["value"] = r.u32()
    elif t in (0x18, 0x1e, 0x39, 0x3a, 0x47, 0x52, 0x65, 0x66, 0x67):
        pass
    else:
        raise ValueError(f"S>C 22 {t:#04x} not modelled")
    d["left"] = len(r.rest())
    return d


def decode_command(payload):
    """C>S 22 01 (serializer 0x1c0870): u32 id, u32 key[3], u16 action, u8 combo, u8 item_flag."""
    r = vce.Reader(payload, 2)
    return dict(id=r.u32(), key=[r.u32(), r.u32(), r.u32()], action=r.u16(), combo=r.u8(), item=r.u8())


def describe22(payload, direction):
    t = payload[1]
    names = C2S_NAMES if direction == "C>S" else S2C_NAMES
    extra = ""
    if direction == "C>S" and t == 0x01 and len(payload) >= 22:
        d = decode_command(payload)
        extra = (f" target={d['id']:#x} action={d['action']:#x} key={d['key']} combo={d['combo']}"
                 f" item={d['item']}")
    return f"BATTLE_PKT {direction} 22 {t:02x} {names.get(t, '?')}{extra} hex={payload.hex()}"


# ---------------------------------------------------------------------------- flow
def _units(ctx):
    """(player slot, bandit slot, unit_ids[14], player cid, bandit cid)."""
    c = ctx.get("char") or {}
    me = ctx.get("cid") or c.get("cid", 0)
    enemy = ctx.get("battle_target") or next(iter(ctx.get("enemies") or {}), DEFAULT_ENEMY_CID)
    own_b = os.environ.get("NOBU_BATTLE_OWN_SIDE", "a").lower() == "b"
    ps, es = (7, 0) if own_b else (0, 7)
    ids = [0] * 14
    ids[ps], ids[es] = me, enemy
    return ps, es, ids, me, enemy


def clock_enabled():
    return os.environ.get("NOBU_BATTLE_CLOCK", "1") == "1"


def p_timer(sec):
    """22 11 i32: battle clock in seconds (handler 0x1686f0: gp-0x789c = value)."""
    return pkt(0x11, vce.put_u32(sec & 0xFFFFFFFF))


def schedule(ctx, delay, fn):
    """Run fn() after delay s on the session loop and push its frames through ctx["push"]. False when no
    loop / push is available (tests, or a caller outside the session): the caller sends the frames itself."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    push = ctx.get("push")
    if push is None:
        return False

    def run():
        try:
            for f in fn():
                push(f)
        except Exception as ex:                          # session gone
            ctx.setdefault("battle_log", []).append(("scheduled push failed", repr(ex)))
    loop.call_later(delay, run)
    return True


def award(ctx, store, rw):
    """Persist the rewards shown in the results window: chars.json exp / prof / fame on the character and the
    money (in mon) in inventory["money"] (items.py format, reported by the 24 01 backpack as kan / mon).
    NOBU_BATTLE_AWARD=0 only logs them. Level-up is not implemented (no level table or client reply decoded)."""
    ctx.setdefault("battle_log", []).append(("award", dict(rw)))
    c = ctx.get("char")
    if not c or os.environ.get("NOBU_BATTLE_AWARD", "1") != "1":
        return
    from . import items, progression
    res = progression.award(c, rw["exp"], rw["prof"], rw["fame"])
    progression.queue(ctx, res)
    ctx["battle_log"].append(("progress", res))
    from . import skills                                 # battle Prof. trains the scroll set for training
    ctx["battle_log"].append(("training", skills.train(ctx, rw["prof"], store)))
    inv = items.inventory(c, store)
    inv["money"] = int(inv.get("money", 0)) + rw["money"]
    items.save(store)


# ============================================================================ combat rounds (design)
# DESIGN (not from the client: the real formulas were server-side and are unknown). "Combat".
#   player Life = 40 + 5*Sta + 10*(level-1), Ki = 20 + 5*Int      (chars.derived_stats + a level term)
#   player attack = 2*Str + 2*level + weapon power (equipped pos 0: by item type), defence = 2*Sta + level +
#                   3 for body armour (pos 3) + 1 per other worn armour piece
#   enemy  attack / defence / level / Life from enemies.get() (attack / defence data keys, else 4+2*lv / 1+lv);
#          the debug Bandit: the player's level, NOBU_BATTLE_ENEMY_ATK / _DEF (default 4+2*lv / 1+lv)
#   hit chance % = clamp(85 + 2*(atk_lv - def_lv) + (atk_dex - def_dex), 50, 97)   (enemy dex = 3 + lv)
#   damage = max(1, round(atk * U(0.9, 1.1) - def / 2)), critical (8 %) x 1.5
#   a miss is a dodge (50 %), block (30 %) or parry (20 %)
WEAPON_POWER = {1: 4, 2: 3, 3: 6, 5: 6, 8: 5}          # items.T_DAGGER / BATON / KATANA / AXE / CLUB
DEFAULT_WEAPON_POWER, BARE_HANDS = 3, 1


def _stats(c):
    lk = (list((c or {}).get("looks", [])) + [0] * 20)[:20]
    return lk[11:16]                                     # Str, Sta, Dex, Int, Cha (chars.py)


def player_combat(c, store=None):
    from . import items
    st, sta, dex, intel, _ = _stats(c)
    lv = int((c or {}).get("level", 1)) or 1
    # worn equipment (items.combat_bonus): the wiki attack / defence scaled into these formulas,
    # bonuses stat bonuses; items without data keep the old type table / flat armour
    eq = items.combat_bonus(c, store, WEAPON_POWER, DEFAULT_WEAPON_POWER, BARE_HANDS)
    weapon, armour, mods = eq["weapon"], eq["armour"], eq["mods"]
    st, sta, dex, intel = (st + mods.get("str", 0), sta + mods.get("sta", 0), dex + mods.get("dex", 0),
                           intel + mods.get("int", 0))
    life = _env_int("NOBU_BATTLE_LIFE", 40 + 5 * sta + 10 * (lv - 1) + mods.get("life", 0))
    ki = _env_int("NOBU_BATTLE_KI", 20 + 5 * intel + mods.get("ki", 0))
    return dict(level=lv, life=life, ki=ki, dex=dex, atk=2 * st + 2 * lv + weapon, dfn=2 * sta + lv + armour)


def enemy_combat(st, player_level):
    lv = int(st.get("level", player_level) or player_level)
    return dict(level=lv, dex=3 + lv,
                atk=_env_int("NOBU_BATTLE_ENEMY_ATK", st.get("attack", 4 + 2 * lv)),
                dfn=_env_int("NOBU_BATTLE_ENEMY_DEF", st.get("defence", 1 + lv)))


def _rng(b):
    import random
    r = b.get("rng")
    if r is None:
        seed = os.environ.get("NOBU_BATTLE_SEED")
        r = b["rng"] = random.Random(int(seed, 0) if seed else None)
    return r


def roll_attack(rng, a, d):
    """(outcome, damage): outcome hit / crit / dodge / block / parry (DESIGN, see above)."""
    chance = max(50, min(97, 85 + 2 * (a["level"] - d["level"]) + (a["dex"] - d["dex"])))
    if rng.random() * 100 >= chance:
        m = rng.random()
        return ("dodge" if m < 0.5 else "block" if m < 0.8 else "parry"), 0
    dmg = max(1, round(a["atk"] * rng.uniform(0.9, 1.1) - d["dfn"] / 2))
    if rng.random() < _env_int("NOBU_BATTLE_CRIT_PCT", 8) / 100:
        return "crit", max(1, round(dmg * 1.5))
    return "hit", dmg


def scene_code(outcome, fatal):
    """Scene code by outcome (BTLSLST scene group 0x00..0x19, HYPOTHESIS): 0..3 damage, 4 kill, 5..9 dodge,
    0xa..0xe block, 0xf..0x15 parry."""
    if fatal:
        return _env_int("NOBU_BATTLE_ATTACK_CODE", 4)
    return {"hit": _env_int("NOBU_BATTLE_CODE_HIT", 1), "crit": _env_int("NOBU_BATTLE_CODE_CRIT", 3),
            "dodge": _env_int("NOBU_BATTLE_CODE_DODGE", 5), "block": _env_int("NOBU_BATTLE_CODE_BLOCK", 0xA),
            "parry": _env_int("NOBU_BATTLE_CODE_PARRY", 0xF)}[outcome]


def defeat(ctx, store):
    """After a lost battle: money penalty (NOBU_BATTLE_DEATH_PENALTY % of the purse, default 10) and a trip back
    to the home town (NOBU_BATTLE_DEATH_TOWN, default the clan's town, towns.py) at its spawn point through zones.warp
    (0B 02 WARPED; the reload respawns the player there with full Life - Life is per battle). Returns frames."""
    from . import items, zones
    c = ctx.get("char")
    if not c:
        return []
    inv = items.inventory(c, store)
    pct = _env_int("NOBU_BATTLE_DEATH_PENALTY", 10)
    lost = int(inv.get("money", 0)) * pct // 100
    inv["money"] = int(inv.get("money", 0)) - lost
    items.save(store)
    town = _env_int("NOBU_BATTLE_DEATH_TOWN", chars.home_town(c) or 110)     # the clan's home town (towns.py)
    x, y, z = chars.FIELD_SPAWN.get(town, chars.FIELD_SPAWN[110])
    kan, mon = items.money_split(lost)
    out = [social.notify_system(f"You were defeated and carried back to town. Lost {kan} kan {mon} mon.")]
    if zones.current_zone(ctx) == town:
        return out
    return out + zones.warp(ctx, store, town, x, y, z, 0, why="defeat")


# ============================================================================ shared battles (party + groups)
# One battle state S is shared by every member session (ctx["battle"]["S"]); each session keeps its own
# per-client flags (phase, ready, ticker). Units: side 0 = players (slots 0..6, the initiator first, then the
# party members in the same zone), side 1 = enemies (slots 7..13: the target and its group). Unit ids on the
# wire are per viewer: a player sees itself with its plain cid and the others as players.wire_cid (0xfe tag),
# the same ids its field entries use. NOBU_BATTLE_OWN_SIDE=b swaps the sides.
BATTLES = {}
_NEXT_BID = [0]


def _cid(ctx):
    return ctx.get("cid") or (ctx.get("char") or {}).get("cid", 0)


def _party_members(ctx):
    """Other live sessions of the initiator's party in the same zone, not already fighting (party.py)."""
    if os.environ.get("NOBU_BATTLE_PARTY", "1") != "1":
        return []
    from . import party, players
    pid = party.party_of(_cid(ctx))
    if not pid:
        return []
    zone = ctx.get("zone")
    out = []
    for cid in party.PARTIES.get(pid, {}).get("members", []):
        m = players.session_by_cid(cid)
        if m is None or m is ctx or in_battle(m) or not m.get("char"):
            continue
        if zone is not None and m.get("zone") not in (None, zone):
            continue
        out.append(m)
    return out[:6]


def unit_wire_id(u, viewer):
    if u["side"] == "e":
        return u["cid"]
    from . import players
    return players.wire_cid(u["cid"], _cid(viewer))


def slot_ids(S, viewer):
    ids = [0] * 14
    for u in S["units"]:
        ids[u["slot"]] = unit_wire_id(u, viewer)
    return ids


def unit_us(S, u, hp=None):
    hp = u["hp"] if hp is None else hp
    dead = hp <= 0
    fl = _env_int("NOBU_BATTLE_DEAD_FLAGS", 0x14) if dead else u["flags"]
    return w_us(max(0, hp), u["mhp"], u["ki"], u["mki"], fl, _env_int("NOBU_BATTLE_DEAD_STATUS", 1) if dead else 0)


def _enemy_units(ctx, player_level):
    """The target enemy and its group (enemies.get(cid)["count"], NOBU_BATTLE_ENEMY_COUNT for the debug Bandit).
    Extra group units get ids target + 0x1000*k (HYPOTHESIS: battle units need no field entry)."""
    tgt = ctx.get("battle_target") or next(iter(ctx.get("enemies") or {}), DEFAULT_ENEMY_CID)
    e = (ctx.get("enemies") or {}).get(tgt, {})
    st = field_enemies.get(tgt) or {}
    count = st.get("count", _env_int("NOBU_BATTLE_ENEMY_COUNT", 1)) if os.environ.get("NOBU_BATTLE_GROUP", "1") == "1" else 1
    count = max(1, min(7, int(count or 1)))
    name = st.get("name") or e.get("family", "Bandit")
    out = []
    for k in range(count):
        ec = enemy_combat(st, player_level)
        life = st.get("life", _env_int("NOBU_BATTLE_ENEMY_LIFE", 60))
        ki = st.get("ki", _env_int("NOBU_BATTLE_ENEMY_KI", 30))
        out.append(dict(side="e", cid=(tgt + 0x1000 * k) & 0xFFFFFFFF, name=name,
                        given=e.get("given", "") if k == 0 else "", hp=life, mhp=life, ki=ki, mki=ki, c=ec,
                        flags=_env_int("NOBU_BATTLE_ENEMY_FLAGS", 0x0C), model=st.get("model", 0),
                        scale=st.get("model_scale", 0), looks=e.get("looks")))
    return tgt, st, out


def _player_unit(m):
    c = m.get("char") or {}
    pc = player_combat(c)
    return dict(side="p", cid=_cid(m), name=c.get("family", ""), given=c.get("given", ""), hp=pc["life"],
                mhp=pc["life"], ki=pc["ki"], mki=pc["ki"], c=pc, flags=_env_int("NOBU_BATTLE_PLAYER_FLAGS", 0x0B),
                model=0, scale=0, looks=c.get("looks"), ctx=m)


def setup_frames(S, viewer):
    """The setup burst for one member (same content for all, unit ids per viewer)."""
    ids = slot_ids(S, viewer)
    t0 = S["t0"]
    root = _env_int("NOBU_BATTLE_MENU_ROOT", 0x11)
    p0 = next(u for u in S["units"] if u["side"] == "p")
    e0 = next(u for u in S["units"] if u["side"] == "e")
    if os.environ.get("NOBU_BATTLE_INTRO", "1") == "1":
        rec1 = w_bi(ids, code=_env_int("NOBU_BATTLE_INTRO_CODE", 0x157E), kind=1,
                    dur=_env_int("NOBU_BATTLE_INTRO_SECS", 3), actor=p0["slot"], target=e0["slot"])
        ar1, ar2 = w_ar(0), w_ar(root)
    else:
        rec1, ar1, ar2 = w_bi(ids, dur=_env_int("NOBU_BATTLE_WINDOW", 200) & 0xFF), w_ar(root), w_ar(0)
    lead = leader_of(S, "p")
    b5 = _env_int("NOBU_BATTLE_MENU_B5", 1) if (lead is None or lead.get("ctx") is viewer) else 0
    out = [p_init_side_a(t0, rec1, rec=1, start=t0, state=0, ar1=ar1, ar2=ar2, b5=b5, ap3=leaders_ap(S, viewer))]
    if os.environ.get("NOBU_BATTLE_SETUP_STAGING", "1") == "1":
        out.append(p_side_b(1, w_bi(ids), start=t0))
    out.append(p_menu_item(_env_int("NOBU_BATTLE_ATTACK_ID", 1), _env_int("NOBU_BATTLE_ATTACK_KEY", 1), "Attack",
                           ttype=_env_int("NOBU_BATTLE_ATTACK_TTYPE", 1), wait=_env_int("NOBU_BATTLE_ATTACK_WAIT", 4)))
    if os.environ.get("NOBU_BATTLE_FLEE", "1") == "1":
        out.append(p_menu_item(_env_int("NOBU_BATTLE_FLEE_ID", 2), _env_int("NOBU_BATTLE_FLEE_KEY", 4), "Flee",
                               ttype=_env_int("NOBU_BATTLE_FLEE_TTYPE", 3), wait=_env_int("NOBU_BATTLE_FLEE_WAIT", 2)))
    if viewer.get("battle_joining"):                 # join mode: node 0x1d = the single entry (2, key 0x15)
        out.append(p_menu_item(JOIN_ACTION, 0x15, "Join", ttype=3))
    from . import skills                                 # the Skills node 0xc children: 22 33 category 5 items
    out += skills.menu_items(viewer.get("char"))         # (value = skill id, Ki cost; nobu/skills.py)
    uis = {u["slot"]: w_ui(u["name"], u["given"], model=u["model"], scale=u["scale"], looks=u.get("looks"))
           for u in S["units"]}
    out += [p_unit_id(u["slot"], uis[u["slot"]]) for u in S["units"]]
    out += [p_unit_join(u["slot"], uis[u["slot"]], unit_us(S, u)) for u in S["units"]]
    if os.environ.get("NOBU_BATTLE_SETUP_CUR_UNITS", "1") == "1":
        out += [p_cur_state(u["slot"], unit_us(S, u), unit_us(S, u)) for u in S["units"]]
    me = next((u for u in S["units"] if u.get("ctx") is viewer), p0)
    out += [p_side_names(social.char_name(me["ctx"].get("char") or {}) or "Player", S["side_name"]),
            p_slot_data(ids), pkt(0x66), pkt(0x65)]
    return out


def fight_setup(ctx):
    """Frames right after 0F 0a for the initiator; party members in the same zone get 0F 0a + the same setup
    pushed (start_frames with the initiator's target). One shared S per battle (BATTLES[bid])."""
    import time
    b = ctx.setdefault("battle", {})
    members = [ctx] + _party_members(ctx)
    players = [_player_unit(m) for m in members]
    lead = players[0]["c"]["level"]
    tgt, st, foes = _enemy_units(ctx, lead)
    own_b = os.environ.get("NOBU_BATTLE_OWN_SIDE", "a").lower() == "b"
    pbase, ebase = (7, 0) if own_b else (0, 7)
    for i, u in enumerate(players):
        u["slot"] = pbase + i
    for i, u in enumerate(foes):
        u["slot"] = ebase + i
    for k in [k for k, v in BATTLES.items() if time.monotonic() - v.get("created", 0) > 3600]:
        BATTLES.pop(k, None)                             # sessions that died mid-battle
    _NEXT_BID[0] += 1
    S = dict(key=_NEXT_BID[0], bid=b.get("bid", 1), units=players + foes, members=members, origin=ctx, target=tgt,
             side_name=st.get("side") or "Bandits", rewards=st.get("rewards"), t0=_env_int("NOBU_BATTLE_T0", 1),
             clock_start=None, cursor=0, rec=1, round=0, pending={}, over=False, won=False, rng=None, store=None,
             routed={}, aid=False, combo=None,
             created=time.monotonic())
    BATTLES[S["key"]] = S
    out = []
    for m in members:
        mb = m.setdefault("battle", {}) if m is not ctx else b
        if m is not ctx:
            m["battle_target"] = tgt
            frames = start_frames(m)                     # 0F 0a (own bid counter per session)
        else:
            frames = []
        mb.update(S=S, phase="setup", ready=False, ticks=0, won=False, lost=False, fled=False, me=_cid(m))
        frames += setup_frames(S, m)
        if m is ctx:
            out += frames
        else:
            _push(m, frames)
        start_ticker(m)
    return out


def _push(m, frames):
    """Deliver frames to another member: ctx["push"] (world.Session), else an outbox (tests)."""
    if not frames:
        return
    push = m.get("push")
    if push is None:
        m.setdefault("battle_outbox", []).extend(frames)
        return
    for f in frames:
        try:
            push(f)
        except Exception as ex:
            m.setdefault("battle_log", []).append(("push failed", repr(ex)))


def deliver(S, build, origin):
    """build(viewer) -> frames for each member still in the battle; the origin's are returned, the rest pushed."""
    mine = []
    for m in S["members"]:
        if (m.get("battle") or {}).get("S") is not S:
            continue
        f = build(m)
        if m is origin:
            mine = f
        else:
            _push(m, f)
    return mine


def start_ticker(ctx):
    """Push 22 18 every NOBU_BATTLE_TICK s until this client's 22 32, and the shared 22 11 battle clock after it
    (the client waits for 22 18 in state 0x11, and runs its timeline only up to 60 x the 22 11 value). Uses
    ctx["push"] on the running asyncio loop; stops on battle end, a newer ticker, a failed push, the session
    closing or MAX_TICKS. No loop (unit tests): nothing."""
    import asyncio
    b = ctx["battle"]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        b["ticker"] = "no loop"
        return
    push = ctx.get("push")
    if push is None:
        b["ticker"] = "no push"
        return
    interval = float(os.environ.get("NOBU_BATTLE_TICK", "1.0"))
    gen = b["ticker_gen"] = b.get("ticker_gen", 0) + 1
    registered = any(v is ctx for v in social.SESSIONS.values())

    def tick():
        if b.get("phase") not in ("setup", "turns", "end") or b.get("ticker_gen") != gen:
            return
        if registered and not any(v is ctx for v in social.SESSIONS.values()):
            b["ticker"] = "stopped: session closed"
            return
        if b["ticks"] >= MAX_TICKS:
            b["ticker"] = "gave up"
            return
        b["ticks"] += 1
        try:
            if not b.get("ready"):
                push(pkt(0x18))
            if clock_enabled() and b.get("ready"):
                push(p_timer(battle_clock(b)))
        except Exception as ex:
            b["ticker"] = f"stopped: {ex!r}"
            return
        loop.call_later(interval, tick)

    b["ticker"] = "running"
    loop.call_later(float(os.environ.get("NOBU_BATTLE_TICK_DELAY", "2.0")), tick)


def battle_clock(b):
    """Shared battle time in seconds for 22 11: t0 + seconds since the first member's 22 32 +
    NOBU_BATTLE_CLOCK_LEAD (default 0). Accepts a session b (with S) or S itself."""
    import time
    S = b.get("S", b)
    t = S.get("clock_start")
    return S.get("t0", 1) + (int(time.monotonic() - t) if t else 0) + _env_int("NOBU_BATTLE_CLOCK_LEAD", 0)


def fight_answer(payload, ctx, store=None):
    """Replies to C>S 22 frames during a fight. Anything else is logged (describe22) and answered with []."""
    import time
    t = payload[1] if len(payload) > 1 else -1
    b = ctx.setdefault("battle", {})
    ctx.setdefault("battle_rx", []).append(payload.hex())
    S = b.get("S")
    if S is None:
        return []
    if t == 0x32:
        b.update(ready=True, phase="turns")
        if S["clock_start"] is None:
            S["clock_start"] = time.monotonic()
        out = [p_unit_ready(unit_wire_id(u, ctx)) for u in S["units"]] + [pkt(0x18)]
        return out + ([p_timer(battle_clock(b))] if clock_enabled() else [])
    if t == 0x40:                                    # Call Aid (T7, header only, KNOWN C>S; no reply)
        return call_aid(S, ctx, True)
    if t == 0x6A:                                    # End Call (T8)
        return call_aid(S, ctx, False)
    if t == 0x01 and len(payload) >= 22 and b.get("phase") in ("turns", "setup") and not S["over"]:
        return command(ctx, decode_command(payload), store)
    if t in (0x41, 0x42) and os.environ.get("NOBU_BATTLE_INFO_REPLY", "1") == "1":
        key = vce.Reader(payload, 2).u16() if t == 0x42 and len(payload) >= 4 else 0
        from . import skills
        si = skills.info(ctx.get("char"), key) if t == 0x42 else None
        if si:                                           # a skill: its name and description (Ki cost)
            return [p_info_reply(key, si[0], si[1])]
        return [p_info_reply(key, "Attack" if key == _env_int("NOBU_BATTLE_ATTACK_ID", 1) else "",
                             os.environ.get("NOBU_BATTLE_INFO_TEXT", "Strike the target with your weapon."))]
    if t == 0x5E and len(payload) >= 4:
        return [p_ph2_reply(vce.Reader(payload, 2).u16())]
    if t == 0x3D and len(payload) >= 3:
        return [p_end_msg(payload[2])]
    if t == 0x54:
        return [pkt(0x47)]
    if t == 0x55:
        # end phase 8 waits for 0x0F; the server ends the battle for this member (0F 06) after the exit delay
        out = [pkt(0x67)]
        delay = float(os.environ.get("NOBU_BATTLE_EXIT_DELAY", "2"))
        if delay <= 0 or not schedule(ctx, delay, lambda: finish(ctx)):
            out += finish(ctx)
        return out
    if t == 0x68:
        return finish(ctx)
    return []


def command(ctx, cmd, store=None):
    """A member's 22 01. Flee ends the battle for that member. Otherwise the command waits until every living
    member has chosen (or NOBU_BATTLE_TURN_WAIT s after the first command), then the round resolves."""
    S = ctx["battle"]["S"]
    S["store"] = S["store"] or store
    me = _cid(ctx)
    if cmd["action"] == JOIN_ACTION and cmd["item"] == 0xFF:
        return join_confirm(S, ctx)                  # a helper's confirm in join mode (0x601574..0x601600)
    if cmd["action"] == _env_int("NOBU_BATTLE_FLEE_ID", 2) and os.environ.get("NOBU_BATTLE_FLEE", "1") == "1":
        ctx["battle"]["fled"] = True
        ctx.setdefault("battle_log", []).append(("fled", cmd))
        u = next((u for u in S["units"] if u.get("ctx") is ctx), None)
        out = [social.notify_system("You fled from the battle.")]
        if u:
            u["fled"] = True
            if u is leader_of(S, "p") and _standing(S, "p"):
                rout_side(S, ctx, "p", 2)            # the leader fled: the others can only flee (3.37)
        S["pending"].pop(me, None)
        return out + finish(ctx)
    if cmd.get("combo"):
        c = S.get("combo")
        if c and c["initiator"] != me and me not in c["joiners"]:
            c["joiners"].append(me)                  # joins the ally's charging skill (22 39 advertised it)
        cmd = dict(cmd, joined=bool(c))
    S["pending"][me] = cmd
    # the command window gauge / "entered" state for everyone (22 03 b = wait s, c = 1)
    unit = next((u for u in S["units"] if u.get("ctx") is ctx), None)
    shown = []
    if unit is not None:
        w = action_wait(S, unit, cmd)
        unit["wait"] = w
        shown = deliver(S, lambda v: [p_unit_state(unit_wire_id(unit, v), 0, min(20, int(w)), 1)], ctx)
    if _is_charging(ctx, cmd) and not S.get("combo") and os.environ.get("NOBU_BATTLE_COMBO", "1") == "1":
        S["combo"] = dict(initiator=me, joiners=[])
        others = [u for u in _standing(S, "p") if u["cid"] != me and u["cid"] not in S["pending"]]
        for u in others:                              # 22 39: the Combo token appears in their menus
            if u["ctx"] is not ctx:
                _push(u["ctx"], [pkt(0x39)])
    alive = [u for u in S["units"] if u["side"] == "p" and u["hp"] > 0 and not u.get("fled")]
    if all(u["cid"] in S["pending"] for u in alive):
        return shown + resolve_round(S, ctx)
    if len(S["pending"]) == 1:
        rnd = S["round"]
        schedule(ctx, float(os.environ.get("NOBU_BATTLE_TURN_WAIT", "20")),
                 lambda: resolve_round(S, ctx) if S["round"] == rnd and not S["over"] else [])
    return shown


def _speed(u):
    return 10 + int(u["c"].get("dex", 0))


def resolve_round(S, origin):
    """Resolve one round: every living unit acts once, in wait-time order (wait = 100 / (10 + Dex), players
    first on ties; DESIGN). Players attack their chosen target (or the first living enemy), enemies attack a
    random living player. One timed record per action (22 02 + 22 0f for every unit + 22 1e), back to back;
    the round's last record opens the menu in its phase B; 22 61 00000000 clears each client's menu lock.
    Ends with the win / loss end record when a side is wiped out."""
    rng = _rng(S)
    now = battle_clock(S) if clock_enabled() else S["t0"]
    t = max(now + _env_int("NOBU_BATTLE_ACT_LEAD", 1), S["cursor"])
    t_first = t
    hit_secs, kill_secs = _env_int("NOBU_BATTLE_HIT_SECS", 7), _env_int("NOBU_BATTLE_ACT_SECS", 8)
    root = _env_int("NOBU_BATTLE_MENU_ROOT", 0x11)
    log = S.setdefault("log", [])
    combo = S.pop("combo", None) or {}
    joiners = [j for j in combo.get("joiners", []) if j in S["pending"]]
    for u in S["units"]:                             # every unit's wait this round (DESIGN, action_wait)
        if u["hp"] > 0 and not u.get("fled"):
            u["wait"] = action_wait(S, u, S["pending"].get(u["cid"]) if u["side"] == "p" else None)
    order = sorted((u for u in S["units"] if u["hp"] > 0 and not u.get("fled") and u["cid"] not in joiners),
                   key=lambda u: (u["wait"], u["side"] != "p", u["slot"]))
    charging = {u["cid"] for u in order if u["side"] == "p" and _is_charging(u.get("ctx") or {},
                                                                          S["pending"].get(u["cid"]))}
    rout = {}                                          # record index -> (side, kind) of a leader who fell
    actions = []                                       # (actor, target, code, dur, hp snapshot before / after)
    one_hit = os.environ.get("NOBU_BATTLE_ONE_HIT") == "1"
    for a in order:
        charging.discard(a["cid"])                       # its charge is released when it fires
        if a["hp"] <= 0:
            continue
        foes = [u for u in S["units"] if u["side"] != a["side"] and u["hp"] > 0 and not u.get("fled")]
        if not foes:
            break
        if a["side"] == "p":
            want = S["pending"].get(a["cid"])
            tgt = None
            if want:
                tgt = next((u for u in foes if want["id"] in (u["cid"], unit_wire_id(u, a["ctx"]))), None)
            tgt = tgt or foes[0]
        else:
            tgt = rng.choice(foes)
        before = {u["slot"]: u["hp"] for u in S["units"]}
        eff = None
        if a["side"] == "p" and want:                    # a Skills entry: its value is the skill id (skills.py)
            from . import skills
            eff = skills.resolve((a.get("ctx") or {}).get("char"), want["action"], a, tgt, rng)
            if eff is not None and not eff["ok"]:        # not enough Ki: a plain attack instead
                log.append(dict(who="player", actor=a["cid"], skill=eff["id"], refused=eff["reason"]))
                eff = None
        if eff and a["cid"] == combo.get("initiator") and joiners and eff.get("dmg"):
            bonus = _env_int("NOBU_BATTLE_COMBO_BONUS", 25) * len(joiners)
            eff = dict(eff, dmg=round(eff["dmg"] * (100 + bonus) / 100))       # +25 % per joiner (DESIGN)
            log.append(dict(who="combo", actor=a["cid"], joiners=list(joiners), bonus=bonus))
        if eff:
            tgt = skills.apply(eff, a, tgt)              # Ki, Life, stat changes; self skills target the actor
            fatal = tgt["side"] == "e" and tgt["hp"] == 0
            log.append(dict(who="player", actor=a["cid"], target=tgt["cid"], outcome="skill", skill=eff["id"],
                            dmg=eff["dmg"], heal=eff["heal"], ki=a["ki"], target_hp=tgt["hp"]))
            after = {u["slot"]: u["hp"] for u in S["units"]}
            ks = skills.SKILLS.get(eff["id"], {}).get("kill_scene")
            actions.append((a, tgt, (ks or scene_code("hit", True)) if fatal else eff["scene"],
                            max(kill_secs, eff["dur"]) if fatal else eff["dur"], before, after))
            continue
        outcome, dmg = roll_attack(rng, a["c"], tgt["c"])
        if a["side"] == "e" and tgt["cid"] in charging and dmg:
            # hit while charging: extra damage and Ki drain (DESIGN; the client shows both through 22 0f)
            dmg = round(dmg * (100 + _env_int("NOBU_BATTLE_CHARGE_HIT_PCT", 50)) / 100)
            tgt["ki"] = max(0, tgt["ki"] - _env_int("NOBU_BATTLE_CHARGE_KI", 5))
            log.append(dict(who="charge_hit", target=tgt["cid"]))
        if one_hit and a["side"] == "p":
            outcome, dmg = "hit", tgt["hp"]
            for u in S["units"]:                         # NOBU_BATTLE_ONE_HIT: one decisive blow ends the
                if u["side"] == "e" and u is not tgt:     # fight (tests / demos), the whole group falls
                    u["hp"] = 0
        tgt["hp"] = max(0, tgt["hp"] - dmg)
        fatal = tgt["hp"] == 0
        log.append(dict(who="player" if a["side"] == "p" else "enemy", actor=a["cid"], target=tgt["cid"],
                        outcome=outcome, dmg=dmg, target_hp=tgt["hp"]))
        if fatal and tgt is leader_of(S, tgt["side"]) and _standing(S, tgt["side"]) and not S["routed"].get(tgt["side"]) \
                and os.environ.get("NOBU_BATTLE_ROUT", "1") == "1":
            rout[len(actions)] = (tgt["side"], 1)       # Standard bearer fell: the rest of that side is routed
            S["routed"][tgt["side"]] = True
            if tgt["side"] == "e":                       # routed enemies flee: the battle is won
                for u in _standing(S, "e"):
                    u["fled"] = True
        after = {u["slot"]: u["hp"] for u in S["units"]}
        actions.append((a, tgt, scene_code(outcome, fatal), kill_secs if fatal else hit_secs, before, after))
    S["pending"] = {}
    S["round"] += 1
    players_alive = any(u["hp"] > 0 and not u.get("fled") for u in S["units"] if u["side"] == "p")
    enemies_alive = any(u["hp"] > 0 and not u.get("fled") for u in S["units"] if u["side"] == "e")
    over = not players_alive or not enemies_alive
    start_rec = S["rec"]
    plan, tt = [], t
    for i, (a, tgt, code, dur, before, after) in enumerate(actions):
        last = i == len(actions) - 1
        r = rout.get(i)
        plan.append((start_rec + 1 + i, tt, a, tgt, code, dur, before, after, root if last and not over else 0, r))
        tt += dur
    S["rec"] = start_rec + len(actions)
    S["cursor"] = tt
    for m in S["members"]:                             # the per-session round log the tests / logs read
        m.setdefault("battle_rounds", []).extend(log[-len(actions):] if actions else [])

    def build(viewer):
        ids = slot_ids(S, viewer)
        out = []
        routed_view = S["routed"].get(_viewer_side(S, viewer))
        for rec, start, a, tgt, code, dur, before, after, ar2, r in plan:
            side_i = {"p": 0, "e": 1}
            trailer = [0, 0]
            if r:
                trailer[_abs_side(S, r[0])] = r[1]
            if ar2 and routed_view:
                ar2 = ROUT_ROOT                           # routed side: node 0x28 = Flee only (KNOWN node)
            helpers = [u["slot"] for u in S["units"] if u["cid"] in joiners] if a["cid"] == combo.get("initiator") else []
            out += [p_side_b(rec, w_bi(ids, code=code, kind=1, dur=dur, actor=a["slot"], target=tgt["slot"],
                                       cam=_env_int("NOBU_BATTLE_ATTACK_VARIANT", 0) & 3, list_a=[tgt["slot"]],
                                       list_b=helpers, rout=trailer),
                             start=start, state=_env_int("NOBU_BATTLE_REC_STATE", 1), ar2=w_ar(ar2),
                             ap1=leaders_ap(S, viewer), ap2=leaders_ap(S, viewer))]
            out += [p_result_state(u["slot"], unit_us(S, u, before[u["slot"]]), unit_us(S, u, after[u["slot"]]))
                    for u in S["units"]]
            if os.environ.get("NOBU_BATTLE_AC_HEX"):
                ac = w_ac(0, 0, os.environ["NOBU_BATTLE_AC_HEX"])
                out.append(p_actions(a["slot"], ac, ac))
            out.append(pkt(0x1E))
        # the timing bar: the round's actions in order, anchored on the first record (22 3c, per viewer)
        out.append(p_order([(unit_wire_id(a, viewer), dur, rec) for rec, _, a, _, _, dur, *_ in plan]))
        if combo:
            out.append(pkt(0x3A))                         # the Combo token goes away
        return out

    if over:
        won = enemies_alive is False and players_alive
        mine = deliver(S, build, origin)
        return mine + end_round(S, origin, won, tt)
    mine = deliver(S, build, origin)
    # the menu lock (menu+0x10) is cleared by 22 61 00000000 (0x219fd0, KNOWN) once the first record plays
    unlock = [pkt(0x61, vce.put_u32(0))]
    delay = max(0.0, t_first - now + 0.5)
    for m in S["members"]:
        if (m.get("battle") or {}).get("S") is not S:
            continue
        if not schedule(m, delay, lambda: unlock):
            if m is origin:
                mine += unlock
            else:
                _push(m, unlock)
    return mine


def end_round(S, origin, won, t):
    """End record (0x157f win / NOBU_BATTLE_LOSE_ACTION loss), then 22 5d / 22 25 per member: rewards only for
    members still standing on a win. Members at Life 0 are marked for the post-battle death path."""
    S["over"], S["won"] = True, won
    end_code = _env_int("NOBU_BATTLE_END_ACTION", 0x157F) if won else _env_int("NOBU_BATTLE_LOSE_ACTION", 0x158F)
    winner = next((u for u in S["units"] if u["hp"] > 0 and u["side"] == ("p" if won else "e")), S["units"][0])
    rec = S["rec"] + 1
    S["rec"] = rec
    t += _env_int("NOBU_BATTLE_END_GAP", 1)
    base = S.get("rewards") or {}
    rw_win = dict(exp=_env_int("NOBU_BATTLE_EXP", base.get("exp", 10)),
                  prof=_env_int("NOBU_BATTLE_PROF", base.get("prof", 5)),
                  fame=_env_int("NOBU_BATTLE_FAME", base.get("fame", 1)),
                  money=_env_int("NOBU_BATTLE_MONEY", base.get("money", 30)))
    zero = dict(exp=0, prof=0, fame=0, money=0)
    for u in S["units"]:
        if u["side"] != "p" or u.get("fled"):
            continue
        m = u["ctx"]
        mb = m.get("battle") or {}
        standing = u["hp"] > 0
        mb.update(phase="end", won=won and standing, lost=not standing, rw=rw_win if (won and standing) else zero)
        if won and standing:
            award(m, S["store"], rw_win)

    def build(viewer):
        ids = slot_ids(S, viewer)
        out = [p_side_b(rec, w_bi(ids, code=end_code, kind=1, dur=_env_int("NOBU_BATTLE_END_SECS", 5),
                                  actor=winner["slot"], target=0xFF), start=t, state=1)]
        out += [p_result_state(u["slot"], unit_us(S, u), unit_us(S, u)) for u in S["units"]]
        out.append(pkt(0x1E))
        rw = (viewer.get("battle") or {}).get("rw") or zero
        tail = []
        banner = _env_int("NOBU_BATTLE_BANNER", 5)
        if banner:
            tail.append(p_banner(banner))
        me_won = (viewer.get("battle") or {}).get("won")
        tail.append(p_result(rw["exp"], rw["prof"], rw["fame"], rw["money"], 1 if me_won else 0))
        delay = float(os.environ.get("NOBU_BATTLE_RESULT_DELAY", "0"))
        if delay > 0 and schedule(viewer, delay, lambda: tail):
            return out
        return out + tail

    return deliver(S, build, origin)


def finish(ctx):
    """End the battle for one member: 0F 06 (the task exits unconditionally, 0x18acd0 -> task 0x95 event 2),
    session state cleared (its ticker stops). The shared side effects run once, when the last member leaves:
    field enemy on_defeated (win) / release (loss, flee), the debug Bandit's respawn timer. A member who ended at
    Life 0 goes through the death path on its next 23 5a (ghost_or_defeat)."""
    import time
    b = ctx.setdefault("battle", {})
    if b.get("phase") == "done":
        return []
    bid = b.get("bid", 1)
    S = b.get("S") or {}
    won, lost = b.get("won"), b.get("lost")
    if lost:
        ctx["defeat_pending"] = True
    b.clear()
    b.update(bid=bid, phase="done", active=False, won=won)
    ctx.pop("battle_target", None)
    out = [battle_leave(bid)]
    left = [m for m in S.get("members", []) if (m.get("battle") or {}).get("S") is S]
    if S and not left:                               # the last one out settles the enemy side
        BATTLES.pop(S.get("key"), None)
        tgt, origin = S.get("target"), S.get("origin")
        if field_enemies.is_enemy_cid(tgt):
            if S.get("won"):
                out += field_enemies.on_defeated(origin, tgt, S.get("store")) if origin is ctx else []
                if origin is not ctx:
                    _push(origin, field_enemies.on_defeated(origin, tgt, S.get("store")))
            else:
                fr = field_enemies.release(origin, tgt)
                if origin is ctx:
                    out += fr
                else:
                    _push(origin, fr)
        elif S.get("won"):
            origin["enemy_defeated_at"] = time.monotonic()
            if not origin.get("enemy_respawn_pending") and schedule(
                    origin, _env_int("NOBU_BATTLE_RESPAWN", 30), lambda: respawn(origin)):
                origin["enemy_respawn_pending"] = True
            fr = despawn(origin)
            if origin is ctx:
                out += fr
            else:
                _push(origin, fr)
    if won:                         # guild job kill count + status pushes / level-up lines, per standing member
        from . import guild, progression
        out += guild.on_kill(ctx, S.get("target"), S.get("store"))
        from . import lifecycle
        out += lifecycle.on_kill(ctx, S.get("target"), S.get("store"))    # promotion exam kills (lifecycle.py)
        out += progression.flush(ctx)
        from . import skills
        out += skills.flush(ctx)                     # 32 01 proficiency (+ 32 04) and "Learned skill" lines
    from . import upkeep; out += upkeep.on_battle_end(ctx, S.get("store"), S)   # Life / Ki, durability (upkeep.py)
    return out


# ============================================================================ death: ghost, Pass On, revive KNOWN
# (static, world-invisible / battle-grey; "Death"): a ghost is PARAMS[0] == 2 (P+0x10fa4, vt+0x28 getter 0x209c80): the
# model turns 30 % opaque (0x5f7400 / 0x5fa6b8), the ○ hint becomes "○Pass On" (0x62d7d0) and ○ sends a plain 28 01
# (0x62cac0 -> 0x611880), skills / trade / follow are refused (UIMSG 2.292, 2.305). Movement and chat stay. PARAMS are
# pushed with `2F 05 <u16 seq> <u32 cid> <u8 1> <bytes PARAMS>` (0x284fd0 -> 0x286ed8 -> UI 5 -> P vt+0x48, all 15
# bytes). `2F 0B <u16 seq> <u8 cause> <u8 dead>` sets / clears the dead flag P+0x10d94 with the box "Died of battle."
# (cause 2) / "Revived!" (cause 4 = no box). Nothing in the client carries a battle's Life 0 into the field, so the
# server pushes both after the 23 5a. The Pass On penalty and the town spawn are rules; the numbers are knobs.
def ghost_enabled():
    return os.environ.get("NOBU_BATTLE_GHOST", "1") == "1"


def p_params(cid, params, seq=0):
    return (vce.put_u8(0x2F) + vce.put_u8(5) + vce.put_u16(seq) + vce.put_u32(cid) + vce.put_u8(1)
            + chars.W().raw(bytes(params), 0xF).bytes())


def p_dead(dead, cause=2, seq=0):
    return vce.put_u8(0x2F) + vce.put_u8(0x0B) + vce.put_u16(seq) + vce.put_u8(cause) + vce.put_u8(1 if dead else 0)


def ghost_params(ghost, c=None):
    """PARAMS with [0] = 2 (ghost) or 0; with the character c also [1] = spirit sight (lifecycle.py)."""
    p = bytearray(chars.char_params(c))
    p[0] = 2 if ghost else 0
    return bytes(p)


def ghost_frames(ctx, store=None):
    """Make the player a ghost (first time: persist c["ghost"] and show "Died of battle."); on later map loads
    only the PARAMS push (the spawn CHAR_INFO carries PARAMS[0] = 0)."""
    c = ctx.get("char")
    if not c:
        return []
    first = not c.get("ghost")
    if first:
        c["ghost"] = True
        from . import items
        items.save(store)
    out = [p_dead(True, 2 if first else 4), p_params(_cid(ctx), ghost_params(True, c))]
    if first:
        out.append(social.notify_system("You have fallen. Wait for someone to revive you, or press ○ to Pass On."))
        from . import players
        players.refresh(ctx)                             # the zone sees the ghost (PARAMS[0] != 0, lifecycle.py)
    return out


def death_penalty(c, store=None):
    """Pass On penalty (rules: half the carried money and 10 % of fame, nothing below level 5); knobs
    NOBU_DEATH_MONEY_PCT (50), NOBU_DEATH_FAME_PCT (10), NOBU_DEATH_MIN_LEVEL (5). Returns (money, fame) lost."""
    from . import items
    if int(c.get("level", 1) or 1) < _env_int("NOBU_DEATH_MIN_LEVEL", 5):
        return 0, 0
    inv = items.inventory(c, store)
    money = int(inv.get("money", 0)) * _env_int("NOBU_DEATH_MONEY_PCT", 50) // 100
    fame = int(c.get("fame", 0)) * _env_int("NOBU_DEATH_FAME_PCT", 10) // 100
    inv["money"] = int(inv.get("money", 0)) - money
    c["fame"] = int(c.get("fame", 0)) - fame
    items.save(store)
    return money, fame


def pass_on(ctx, store=None):
    """28 01 as a ghost: penalty, the ghost state cleared (2F 0B silent + 2F 05 PARAMS[0] = 0) and back to the
    home town (NOBU_BATTLE_DEATH_TOWN, default the clan's town, towns.py) through zones.warp."""
    from . import items, zones
    c = ctx.get("char")
    if not c:
        return []
    money, fame = death_penalty(c, store)
    c.pop("ghost", None)
    items.save(store)
    town = _env_int("NOBU_BATTLE_DEATH_TOWN", chars.home_town(c) or 110)     # the clan's home town (towns.py)
    kan, mon = items.money_split(money)
    out = [p_dead(False, 4), p_params(_cid(ctx), ghost_params(False, c)),
           social.notify_system(f"You passed on and returned to the {zones.place_name(town)} graveyard."
                                + (f" Lost {kan} kan {mon} mon and {fame} fame." if money or fame else ""))]
    x, y, z = chars.FIELD_SPAWN.get(town, chars.FIELD_SPAWN[110])
    return out + zones.warp(ctx, store, town, x, y, z, 0, why="pass on")


def revive(ctx, store=None):
    """Bring a ghost back where it stands (a reviver's skill later; now the chat command "battle revive" on a
    debug rig): 2F 05 PARAMS[0] = 0 + 2F 0B "Revived!"."""
    from . import items
    c = ctx.get("char")
    if not c or not c.get("ghost"):
        return []
    c.pop("ghost", None)
    items.save(store)
    from . import players
    players.refresh(ctx)                                 # the zone sees the living body again (lifecycle.py)
    return [p_params(_cid(ctx), ghost_params(False, c)), p_dead(False, 0)]


# ============================================================================ waits, combos, leaders, aid
JOIN_ACTION = 0x158      # the join-mode confirm sends 22 01 <own id> 0 0 0 0158 00 ff (0x601574, KNOWN)
ROUT_ROOT = 0x28         # command node 0x28 = the single entry (2, 4) = Flee (KNOWN, node table 0x3e7e20)


def action_wait(S, u, cmd=None):
    """Seconds until the unit's action fires (DESIGN): the command's base wait (22 33 wait byte: Attack
    NOBU_BATTLE_ATTACK_WAIT 4, a skill 3 + Ki / 4, the enemy NOBU_BATTLE_ENEMY_WAIT 5) scaled by 20 / (10 + Dex)."""
    base = _env_int("NOBU_BATTLE_ENEMY_WAIT", 5) if u["side"] == "e" else _env_int("NOBU_BATTLE_ATTACK_WAIT", 4)
    if cmd and u["side"] == "p":
        from . import skills
        sk = skills.SKILLS.get(cmd.get("action"))
        if sk:
            base = sk.get("wait") or 3 + int(sk.get("ki", 0)) // 4
    return round(base * 20.0 / (10 + int(u["c"].get("dex", 0))), 2)


def _is_charging(ctx, cmd):
    """A skill with damage charges (DESIGN): allies may join it (combo) and a hit before it fires hurts more."""
    if not cmd:
        return False
    from . import skills
    sk = skills.SKILLS.get(cmd.get("action"))
    return bool(sk and sk.get("kind") in ("damage", "arts", "drain"))


def _standing(S, side):
    return [u for u in S["units"] if u["side"] == side and u["hp"] > 0 and not u.get("fled")]


def leader_of(S, side):
    """The side's standard bearer: the party leader if fighting (party.PARTIES), else the first unit
    of the side."""
    units = [u for u in S["units"] if u["side"] == side]
    if not units:
        return None
    if side == "p":
        from . import party
        pid = party.party_of(units[0]["cid"])
        lid = party.PARTIES.get(pid, {}).get("leader") if pid else None
        for u in units:
            if u["cid"] == lid:
                return u
    return units[0]


def _abs_side(S, side):
    """0 / 1 as the client numbers them: side 0 = slots 0..6."""
    u = next((u for u in S["units"] if u["side"] == side), None)
    return 0 if u is None or u["slot"] < 7 else 1


def _viewer_side(S, viewer):
    return "p"


def leaders_ap(S, viewer):
    lp, le = leader_of(S, "p"), leader_of(S, "e")
    ids = {_abs_side(S, "p"): unit_wire_id(lp, viewer) if lp else 0, _abs_side(S, "e"): unit_wire_id(le, viewer) if le else 0}
    rt = {_abs_side(S, k): 1 if S["routed"].get(k) else 0 for k in ("p", "e")}
    return w_ap(ids.get(0, 0), ids.get(1, 0), rt.get(0, 0), rt.get(1, 0))


def rout_side(S, origin, side, kind):
    """The leader of `side` fled (kind 2) or fell (1) while others stand: a 1 s record whose end routes the side
    (BI trailer B, AP2 routed; UIMSG 3.36 - 3.39 in the battle log) and whose roots leave only Flee (node 0x28)."""
    S["routed"][side] = True
    rec = S["rec"] + 1
    S["rec"] = rec
    now = battle_clock(S) if clock_enabled() else S["t0"]
    t = max(now + 1, S["cursor"])
    S["cursor"] = t + 1
    trailer = [0, 0]
    trailer[_abs_side(S, side)] = kind

    def build(viewer):
        ids = slot_ids(S, viewer)
        return ([p_side_b(rec, w_bi(ids, dur=1, rout=trailer), start=t, state=1, ar1=w_ar(ROUT_ROOT),
                          ar2=w_ar(ROUT_ROOT), ap1=leaders_ap(S, viewer), ap2=leaders_ap(S, viewer))]
                + [p_result_state(u["slot"], unit_us(S, u), unit_us(S, u)) for u in S["units"]] + [pkt(0x1E)])
    return deliver(S, build, origin)


def call_aid(S, ctx, on):
    """22 40 Call Aid / 22 6a End Call (the leader's T7 / T8). An open call lets players in the zone who are not in
    the battle join it by saying "help" (DESIGN: the client's field-side discovery of a battle to join is not
    traced). The joiner gets 0F 0c (join mode, KNOWN client side) and the setup."""
    S["aid"] = bool(on)
    name = social.char_name(ctx.get("char") or {}) or "Someone"
    if on:
        for m in social.online(ctx.get("zone")):
            if m is ctx or in_battle(m) or m in S["members"]:
                continue
            _push(m, [social.notify_system(f"{name} calls for aid in battle! Say 'help' to join.")])
        return [social.notify_system("You called for aid.")]
    return [social.notify_system("You ended the call for aid.")]


def open_aid(ctx):
    """The battle in this zone with an open call for aid, else None."""
    zone = ctx.get("zone")
    for S in BATTLES.values():
        if S.get("aid") and not S["over"] and (S["origin"].get("zone") == zone):
            return S
    return None


def join_frames(S, ctx):
    """A helper answers the call: 0F 0c (join mode: menu node 0x1d = our 22 33 key 0x15 entry) + the setup."""
    if in_battle(ctx) or len([u for u in S["units"] if u["side"] == "p"]) >= 7:
        return [social.notify_system("There is no room in that battle.")]
    ctx["battle_target"] = S["target"]
    ctx["battle_joining"] = True
    frames = start_frames(ctx, [0xC])                 # 0F 0c: join mode (model+0x1937c = 1)
    b = ctx["battle"]
    b.update(S=S, phase="setup", ready=False, ticks=0, won=False, lost=False, fled=False, me=_cid(ctx))
    S["members"].append(ctx)
    frames += setup_frames(S, ctx)
    start_ticker(ctx)
    return frames


def join_confirm(S, ctx):
    """The helper's join confirm: its unit takes the next free slot of the player side; every member gets its
    identity (22 1f), state (22 1a) and the current record's grids (22 1c: phase entries rebuild participants
    from them)."""
    ctx.pop("battle_joining", None)
    if any(u.get("ctx") is ctx for u in S["units"]):
        return []
    u = _player_unit(ctx)
    used = {x["slot"] for x in S["units"]}
    base = 0 if _abs_side(S, "p") == 0 else 7
    u["slot"] = next(i for i in range(base, base + 7) if i not in used)
    S["units"].append(u)
    ui = w_ui(u["name"], u["given"], looks=u.get("looks"))

    def build(viewer):
        return [p_unit_id(u["slot"], ui), p_unit_join(u["slot"], ui, unit_us(S, u)),
                p_cur_state(u["slot"], unit_us(S, u), unit_us(S, u)),
                p_unit_state(unit_wire_id(u, viewer), 1, 1, 0)]
    return deliver(S, build, ctx)
