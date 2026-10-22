"""World server (TCP, default 9100): VCE key exchange, Blowfish records, game frames.

The client connects here after World Select (host:port from the 0x1e world entry). The
connection is encrypted (see nobu/vcecrypto.py). After the handshake the plaintext is the
usual VCE stream (u16be length + payload, payload[0] = command). The client's first frame is
CODE_LOGIN (cmd 1, builder 0x196e20); we answer with a login success (cmd 1, parsed by
0x191068) and log every later frame decoded where known.

Env:
  NOBU_WORLD_PORT   TCP port (default 9100)
  NOBU_BIND         bind address (default 0.0.0.0)
  NOBU_LOG_DIR      log root (default ./logs): sessions/<sid>.log + events.jsonl
  NOBU_IDLE_SECS    close a silent session after this long (default 600)
  NOBU_LOGIN_RES    result code sent in the CODE_LOGIN reply (default 0 = success)
  NOBU_CHAR_DB      character store (default $NOBU_DATA/chars.json, else ./data/chars.json)
  NOBU_ZONE_ENTER   1 (default) = push 0B 02 after the 04 reply
  NOBU_AREA         area byte of 0B 02 / 0B 01 / 0B 0a (default: province id of the field, else 1)
  NOBU_MOVE_SPEED   CHAR_INFO PARAMS[8] = move speed (default 100 -> run speed 10.0; walk cap 3.5)
  NOBU_PARAMS       all 15 CHAR_INFO PARAMS as 30 hex digits (overrides NOBU_MOVE_SPEED)
  NOBU_INFO_W54/W58 CHAR_INFO u16 +0x54 / +0x58 = appearance bytes (default 0; nonzero hid the model)
  NOBU_ZONE         force the field map id (default: saved field, else faction home, Oda -> 10)
  NOBU_SPAWN        1 (default) = on `23 5a` push CHAR_LOCATION + SCMD_CHAR_LOAD + CHAR_LOCATION
  NOBU_SPAWN_X/Y/Z  override the spawn point (default chars.FIELD_SPAWN[zone])
  NOBU_WORLD_PUSH   1 = also push WORLD_DATE / WORLD_TIME / FIELD_INFO on `23 5a`
  NOBU_FIELD_NAME   FIELD_INFO name (default Owari)
  NOBU_DEBUG_NPC    1 = also spawn the debug NPC "Guide" next to the player (nobu/npc.py)
  NOBU_SHUTDOWN_SECS  SIGTERM: wait this long for sessions to close before cancelling them (default 5)
  NOBU_BACKUP_*, NOBU_SAVE_DELAY  persistence knobs, see nobu/persist.py
  NOBU_LIVE_FILE / NOBU_LIVE_EVERY  the boards' snapshot, see nobu/live.py
  NOBU_ADMIN_*      an OpenLobby admin panel's port (off unless NOBU_ADMIN_PORT), see nobu/adminport.py
"""
import asyncio
import datetime as dt
import json
import os
import sys

from . import battle, chars, enemies, items, misc, npc, party, players, quests, shops, social, upkeep, vce, vcecrypto
from . import progression
from . import zones                                  # gates / zone travel (nobu/zones.py)
from . import guild                                  # Meeting Hall jobs, merit, rank (nobu/guild.py)
from . import crafting                               # production / crafting (nobu/crafting.py)
from . import castle                                 # castle NPCs: lord, generals, castellan (nobu/castle.py)
from . import repair                                 # the repairer: mends worn gear for a fee (nobu/repair.py)
from . import doctor                                 # the doctor: restores Life / Ki for a fee (nobu/doctor.py)
from . import townquests                             # quest givers and their errands (nobu/townquests.py)
from . import fieldnpcs                              # ferrymen, checkpoint guards, villagers (nobu/fieldnpcs.py)
from . import elder                                  # the old sage: tips, record repair (nobu/elder.py)
from . import live                                   # the boards' snapshot (nobu/live.py)
from . import adminport                              # an OpenLobby admin panel (nobu/adminport.py)
from . import bank                                   # money changer / storage (nobu/bank.py)
from . import skills                                 # skills, scrolls, training (nobu/skills.py)
from . import war, war_week                          # diplomacy / province / war state, week (war.py)
from . import lifecycle                              # revive, ghosts, stat growth, exams (nobu/lifecycle.py)
from . import hiryu, house                           # specialisation / potential (hiryu.py), houses cmd 0x1b (house.py)

BIND = os.environ.get("NOBU_BIND", "0.0.0.0")
PORT = int(os.environ.get("NOBU_WORLD_PORT", "9100"))
LOG_DIR = os.environ.get("NOBU_LOG_DIR", os.path.join(os.getcwd(), "logs"))
IDLE = float(os.environ.get("NOBU_IDLE_SECS", "600"))
LOGIN_RES = int(os.environ.get("NOBU_LOGIN_RES", "0"))
from . import trade                                   # player trade / wares (nobu/trade.py)
from . import persist                                 # atomic saves, backups (nobu/persist.py)

# Client -> server command numbers seen in the client (builders in 0x196e20.. for vtable 0x4502a0)
# and server -> client numbers handled by 0x19e7b0 -> 0x18f5e0 -> 0x190f40 (main handler 0x188530
# logs "Recv: CODE_LOGIN" for 1 and "CODE_CHAR: SCMD_CHAR_LOAD" for 5/2).
CODE_NAMES = {1: "CODE_LOGIN", 3: "CODE_PONG", 4: "CMD_04_ENTER", 5: "CODE_CHAR", 0xB: "NOTIFY_CHAR",
              0xC: "NOTIFY", 0x23: "CMD_23"}
CHAR_SUB_NAMES = {1: "SCMD_CHAR_CREATE", 2: "SCMD_CHAR_LOAD", 3: "SCMD_CHAR_LIST", 4: "SCMD_CHAR_DELETE",
                  5: "SCMD_CHAR_MOVE"}
NOTIFY_CHAR_SUBS = {1: "CHAR_LOCATION", 2: "ZONE_ENTER", 8: "CHAR_LOCATION_UPDATE",
                    9: "CHAR_LOCATION_UPDATE_F", 0xA: "CHAR_INFO"}
STORE = None

B64 = "@ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"   # 0x3f16c0


# ---------------------------------------------------------------------------- string obfuscation

def obf_encode(text, seed):
    """Client 0x2986c0: seed byte, then (text + NUL) XOR a key starting at seed, +0x1f per
    byte, then the LSB-first 6-bit encoding 0x2981a0 with the alphabet at 0x3f16c0."""
    raw = text.encode("latin-1") + b"\0"
    buf, k = bytearray([seed & 0xFF]), seed & 0xFF
    for c in raw:
        buf.append(c ^ k)
        k = (k + 0x1F) & 0xFF
    out = []
    for o in range(0, len(buf), 3):
        g = buf[o:o + 3]
        b0 = g[0]
        out.append(B64[b0 & 0x3F])
        if len(g) == 1:
            out.append(B64[b0 >> 6])
            break
        b1 = g[1]
        out.append(B64[((b0 >> 6) | (b1 << 2)) & 0x3F])
        if len(g) == 2:
            out.append(B64[(b1 >> 4) & 0x3F])
            break
        b2 = g[2]
        out.append(B64[((b1 >> 4) | (b2 << 4)) & 0x3F])
        out.append(B64[(b2 >> 2) & 0x3F])
    return "".join(out)


def obf_decode(s):
    v = [B64.index(c) for c in s]
    buf = bytearray()
    for o in range(0, len(v), 4):
        g = v[o:o + 4]
        if len(g) >= 2:
            buf.append((g[0] | (g[1] << 6)) & 0xFF)
        if len(g) >= 3:
            buf.append(((g[1] >> 2) | (g[2] << 4)) & 0xFF)
        if len(g) == 4:
            buf.append(((g[2] >> 4) | (g[3] << 2)) & 0xFF)
    if not buf:
        return ""
    k, out = buf[0], bytearray()
    for c in buf[1:]:
        out.append(c ^ k)
        k = (k + 0x1F) & 0xFF
    return bytes(out).split(b"\0", 1)[0].decode("latin-1")


# ---------------------------------------------------------------------------- messages

def encode_login_request(cid, password, uid, version=20003, title=1000201, seed=0x5A):
    """CODE_LOGIN as the client builds it (0x196e20, called from 0x193950 / net command 0x10)."""
    a = obf_encode(cid, seed).encode("ascii")
    b = obf_encode(password, seed).encode("ascii")
    return (vce.put_u8(1) + vce.put_bytes(a) + vce.put_bytes(b) + vce.put_u32(uid)
            + vce.put_u32(version) + vce.put_u32(0) + vce.put_u8(1) + vce.put_u32(title))


def decode_login_request(payload):
    r = vce.Reader(payload)
    if r.u8() != 1:
        raise vce.WireError("not CODE_LOGIN")
    cid = obf_decode(r.bytes(0x210).decode("ascii"))
    pw = obf_decode(r.bytes(0x210).decode("ascii"))
    d = dict(id=cid, password=pw, uid=r.u32(), version=r.u32(), zero=r.u32(), one=r.u8(),
             title=r.u32())
    if not r.done():
        d["rest"] = r.rest().hex()
    return d


# Login reply +8 -> net+0x52 = usable character slots: the select screen (0x66d630) clamps it to
# 1..3 (0 -> 1); slots at or above it show "------" (UIMSG 1.120) instead of Create (1.37), and
# the same test (0x66da30) gates the filled slots. 0 left only slot 1 usable.
CHAR_SLOTS = int(os.environ.get("NOBU_CHAR_SLOTS", "3"))


def encode_login_reply(res, uid=0, c=0, b8=0):
    """0x191068: u32 res; if res >= 0: u32 uid (+4), u32 (+0xc), u32 (+8).
    Main handler 0x1886c0: res >= 0 -> net+0x44 = uid, net+0x52 = (u8)+8, net+0x4c = +0xc,
    notification 6 (next scene); res < 0 -> error (-0x227 special at 0x188798)."""
    out = vce.put_u8(1) + vce.put_u32(res)
    if res >= 0 and res < 0x80000000:
        out += vce.put_u32(uid) + vce.put_u32(c) + vce.put_u32(b8)
    return out


def describe(payload, direction="C>S"):
    if not payload:
        return "empty"
    cmd = payload[0]
    name = CODE_NAMES.get(cmd, f"cmd {cmd:#04x}")
    znd = zones.describe(payload, direction)        # 28 02 use object, 0B 02 WARPED (nobu/zones.py)
    if znd:
        return znd
    pld = players.describe(payload, direction) or party.describe(payload, direction)
    if pld:                                         # 0B 04 / 0B 08, cmd 7 / 0x0E (players.py, party.py)
        return pld
    trd = trade.describe(payload, direction)         # cmd 0x27 / 0x33, 24 17, 25 07 / 08 (trade.py)
    if trd:
        return trd
    lcd = lifecycle.describe(payload, direction)     # 26 07 field skill (nobu/lifecycle.py)
    if lcd:
        return lcd
    btd = battle.describe(payload, direction)        # cmd 0x0F, battle chat triggers (nobu/battle.py)
    if btd:
        return btd
    gd = guild.describe(payload, direction)           # 2a 03 on a guild job (nobu/guild.py)
    if gd:
        return gd
    upd = upkeep.describe(payload, direction)        # 0C 05 date, 2F hunger / Life / stats (nobu/upkeep.py)
    if upd:
        return upd
    qd = quests.describe(payload, direction)          # quest list 34 / detail 2a 03 (nobu/quests.py)
    if qd:
        return qd
    bkd = bank.describe(payload, direction)          # bank event 11/07, 0a answers, 24 0f-13, 30 05/07
    if bkd:
        return bkd
    crd = crafting.describe(payload, direction)      # craft event 11/08, 0a answers, 30 0b/0f/10
    if crd:
        return crd
    hsd = house.describe(payload, direction)         # House 1b requests / replies, 18 01 push (nobu/house.py)
    if hsd:
        return hsd
    wrd = war.describe(payload, direction)           # 23 03-05 realm, 2F 04 / 2F 30, board events (war.py)
    if wrd:
        return wrd
    shd = shops.describe(payload, direction)         # event 11 / 0a, market 1b, 23 0a-0c (nobu/shops.py)
    if shd:
        return shd
    skd = skills.describe(payload, direction)        # 26 skill requests, 32 / 2F 07 pushes (nobu/skills.py)
    if skd:
        return skd
    prd = progression.describe(payload, direction)   # 2F level / exp / fame pushes (nobu/progression.py)
    if prd:
        return prd
    npd = npc.describe(payload, direction)           # target set / talk / NPC say (nobu/npc.py)
    if npd:
        return npd
    soc = social.describe(payload, direction)       # chat / find / friends (nobu/social.py)
    if soc:
        return soc
    itd = items.describe(payload, direction)         # backpack / items (nobu/items.py)
    if itd:
        return itd
    msc = misc.describe(payload, direction)         # news / support / inventory (nobu/misc.py)
    if msc:
        return msc
    try:
        if cmd == 1 and direction == "C>S":
            d = decode_login_request(payload)
            d["password"] = "***"
            return f"{name} {d}"
        if cmd == 1 and direction == "S>C":
            r = vce.Reader(payload, 1)
            return f"{name} reply res={r.i32()} rest={r.rest().hex()}"
        if cmd == 3:
            r = vce.Reader(payload, 1)
            vals = [r.u32() for _ in range(len(r.rest()) // 4)]
            return f"{name} " + " ".join(f"{v:#x}" for v in vals)
        if cmd == 0xB and len(payload) >= 2:
            sub = payload[1]
            label = f"{name} {NOTIFY_CHAR_SUBS.get(sub, f'sub {sub}')}"
            if sub == 1:
                from . import clientmodel as cm
                recs = cm.parse_notify_char(payload)["records"]
                return label + " " + "; ".join(
                    f"uid={d['uid']:#x} cid={d['cid']} pos={d['pos']} lv={d['level']} "
                    f"power={d['power']} job={d['job']}" for d in recs)
            if sub == 0xA:
                from . import clientmodel as cm
                recs = cm.parse_notify_char(payload)["records"]
                return label + " " + "; ".join(
                    f"uid={d['uid']:#x} cid={d['cid']} area={d['area']} pos={d['pos']} "
                    f"params={d['params'].hex()} life={d['w54']} ki={d['w58']}" for d in recs)
            if sub == 2 and len(payload) >= 5:
                r = vce.Reader(payload, 2)
                return f"{label} area={r.u8()} zone={r.u16()} {r.rest().hex()}"
            return f"{label} {payload[2:].hex()}"
        if cmd == 0x23 and len(payload) >= 4:
            r = vce.Reader(payload, 1)
            sub, seq = r.u8(), r.u16()
            if sub == 0x5A:
                return f"{name} sub 90 (WDMC_INIT_WORLD_ALL: world UI ready) seq={seq}"
            if direction == "C>S" and sub == 1 and len(r.rest()) >= 2:
                return f"{name} sub 1 (creation stat table) seq={seq} job={r.u8()} faction={r.u8()}"
            return f"{name} sub {sub} seq={seq} {r.rest().hex()}"
        if cmd == 5 and len(payload) > 1:
            sub = payload[1]
            label = f"{name} {CHAR_SUB_NAMES.get(sub, f'sub {sub}')}"
            if direction == "C>S" and sub == 1:
                return f"{label} {chars.decode_create_request(payload)}"
            if direction == "C>S" and sub == 5 and len(payload) >= 16:
                return f"{label} {chars.decode_move(payload)}"
            if direction == "C>S" and sub in (2, 3, 4):
                r = vce.Reader(payload, 2)
                return label + " " + " ".join(f"{r.u32():#x}" for _ in range(len(r.rest()) // 4))
            if direction == "S>C":
                r = vce.Reader(payload, 2)
                return f"{label} reply res={r.i32()} +{len(r.rest())} bytes"
            return f"{label} {payload[2:].hex()}"
    except vce.WireError as e:
        return f"{name} (malformed: {e}) {payload.hex()}"
    return f"{name} {payload[1:].hex()}"


def world_ready_pushes(ctx):
    """Pushes sent when the client reports the world UI is up (23 5a): spawn the local player
    (chars.spawn_pushes) and, with NOBU_WORLD_PUSH=1, the field name / date / time notifies."""
    out = []
    c = ctx.get("char")
    if c and os.environ.get("NOBU_SPAWN", "1") == "1":
        out += chars.spawn_pushes(c, ctx.get("uid", c.get("uid", 0)))
        out += npc.debug_spawn(ctx)                     # NOBU_DEBUG_NPC=1: the guide NPC
        out += battle.debug_spawn(ctx)                  # NOBU_DEBUG_ENEMY=1: the Bandit
        out += enemies.spawn_pushes(ctx)                # field enemies around the player (nobu/enemies.py)
        out += shops.spawn_pushes(ctx)                  # shopkeepers of the zone (nobu/shops.py)
        out += crafting.spawn_pushes(ctx)               # workshop keeper (nobu/crafting.py)
        out += bank.spawn_pushes(ctx)                   # money changer + storekeeper (nobu/bank.py)
        out += npc.guard_spawn(ctx)                     # gate guards of a home town (town_spots.json)
        out += castle.spawn_pushes(ctx)                 # lord, generals, castellan, guards of a castle
        out += repair.spawn_pushes(ctx)                 # the repairer beside the Craftsman (nobu/repair.py)
        out += doctor.spawn_pushes(ctx)                 # the doctor by the apothecary (nobu/doctor.py)
        out += elder.spawn_pushes(ctx)                  # the old sage by the notice board (nobu/elder.py)
        out += fieldnpcs.spawn_pushes(ctx)              # ferrymen, checkpoint guards, villagers (fieldnpcs.py)
        out += quests.world_ready(ctx, store)           # Overseer NPC + 34 01 quest list (last)
        out += guild.world_ready(ctx, store)            # hall master NPC + 34 02 active job (nobu/guild.py)
        out += townquests.world_ready(ctx, store)       # quest givers + 34 02 active quests (townquests.py)
        out += lifecycle.world_ready(ctx, store)        # Nagoya shrine + 34 02 active exam (nobu/lifecycle.py)
        reg = npc.registry_frames(ctx)                  # cmd 0x17: every NPC above into the name registry
        if reg:                                         # right after the last spawn record (0B 0A)
            last = max((i for i, f in enumerate(out) if f[:2] == bytes([0x0B, 0x0A])), default=len(out) - 1)
            out[last + 1:last + 1] = reg
        out += players.on_spawn(ctx)                    # other players in the zone (nobu/players.py)
        out += progression.world_ready(ctx)             # 2F level / exp / fame + level-up lines
        out += upkeep.world_ready(ctx, store)           # clock, stats, hunger, weight + ticker (upkeep.py)
        out += war.world_ready(ctx)                     # board NPC, 2F 04 diplomacy, 2F 30 (war.py)
        out += house.world_ready(ctx, store)            # NOBU_HOUSE_PUSH=1: 18 01 house model (nobu/house.py)
    if c:
        out += misc.pending_answers(ctx)                # staff answers to reports, kept while offline
    if os.environ.get("NOBU_WORLD_PUSH") == "1":            # layouts KNOWN, need not
        zone, x, y, z, _ = chars.char_position(c)
        out += [upkeep.world_date_now() if upkeep.clock_enabled() else
                chars.notify_world_date(1560, 5, 19, 12, 0),
                chars.notify_world_time(0, 0),
                chars.notify_field_info(os.environ.get("NOBU_FIELD_NAME", "Owari"), x, y, z)]
    return out


def store():
    global STORE
    if STORE is None:
        STORE = chars.default_store()
    return STORE


def answer(payload, ctx=None):
    """Replies for one client frame. ctx carries per-session state (uid)."""
    ctx = {} if ctx is None else ctx
    if not payload:
        return []
    cmd = payload[0]
    zna = zones.answer(payload, ctx, store)        # 28 02 gates -> 0B 02; stale 05 05 after a warp
    if zna is not None:
        return zna
    if payload[:2] == b"\x28\x01":
        wta = trade.wares_talk(ctx, store)         # ○ on a player with wares: its shop (trade.py)
        if wta is not None:
            return wta
        npc.resolve_target(ctx)                    # ○ sends no 25 01: nearest NPC / enemy in range
    pla = players.answer(payload, ctx, store)      # 25 01 on a player; sees 04 / 05 05 (nobu/players.py)
    if pla is not None:
        return pla
    pty = party.answer(payload, ctx, store)        # CODE_PARTY cmd 7 (nobu/party.py)
    if pty is not None:
        return pty
    wra = war.answer(payload, ctx, store)          # 23 04/05 realm, 28 01 notice board, its 0a, "war" chat
    if wra is not None:
        return wra
    tra = trade.answer(payload, ctx, store)        # 27 trade, 24 17 prices, 25 07/08 wares, 0a buys
    if tra is not None:
        return tra
    lfa = lifecycle.answer(payload, ctx, store)    # 26 07 revive, ○ at the shrine, exams (nobu/lifecycle.py)
    if lfa is not None:
        return lfa
    hra = hiryu.answer(payload, ctx, store)        # "spec" / "potential" chat, 24 03 line scroll, 23 1a/1b
    if hra is not None:
        return hra
    hsa = house.answer(payload, ctx, store)        # cmd 0x1b House screen, "house" chat (nobu/house.py)
    if hsa is not None:
        return hsa
    bt = battle.answer(payload, ctx, store)        # debug enemy 25 01 / 28 01, "battle" chat
    if bt is not None:
        return bt
    gla = guild.answer(payload, ctx, store)        # 2a 03 job detail, 28 01 hall master, "jobs" chat
    if gla is not None:
        return gla
    upa = upkeep.answer(payload, ctx, store)       # "upkeep ..." chat commands (nobu/upkeep.py)
    if upa is not None:
        return upa
    tqa = townquests.answer(payload, ctx, store)   # 2a 03 town quest detail, 28 01 on a quest giver
    if tqa is not None:
        return tqa
    qa = quests.answer(payload, ctx, store)        # 2a 03 quest detail, 28 01 on a quest NPC
    if qa is not None:
        return qa
    bka = bank.answer(payload, ctx, store)         # 28 01 on a bank NPC, 0a bank answers, 24 0f-12
    if bka is not None:
        return bka
    cra = crafting.answer(payload, ctx, store)     # 28 01 on the workshop, 0a craft answers (first)
    if cra is not None:
        return cra
    sha = shops.answer(payload, ctx, store)        # 28 01 on a shopkeeper, 0a trades, 1b, 23 0a-0c
    if sha is not None:
        return sha
    rpa = repair.answer(payload, ctx, store)       # 28 01 on the repairer (nobu/repair.py)
    if rpa is not None:
        return rpa
    dra = doctor.answer(payload, ctx, store)       # 28 01 on the doctor (nobu/doctor.py)
    if dra is not None:
        return dra
    ela = elder.answer(payload, ctx, store)        # 28 01 on the old sage (nobu/elder.py)
    if ela is not None:
        return ela
    fna = fieldnpcs.answer(payload, ctx, store)    # 28 01 on a ferryman / guard / villager (fieldnpcs.py)
    if fna is not None:
        return fna
    npa = npc.answer(payload, ctx, store)          # 25 01 target set, 28 01 talk (nobu/npc.py)
    if npa is not None:
        return npa
    ska = skills.answer(payload, ctx, store)       # cmd 0x26 skill window, chat "skill" (nobu/skills.py)
    if ska is not None:
        return ska
    pra = progression.answer(payload, ctx, store)  # chat "exp <n>" / "level" (nobu/progression.py)
    if pra is not None:
        return pra
    soc = social.answer(payload, ctx, store)      # chat / find / friends (nobu/social.py)
    if soc is not None:
        return soc
    ita = items.answer(payload, ctx, store)            # backpack / items (nobu/items.py)
    if ita is not None:
        return ita
    msc = misc.answer(payload, ctx, store)             # news / support / inventory (nobu/misc.py)
    if msc is not None:
        return msc
    if cmd == 1:
        req = decode_login_request(payload)
        ctx["uid"] = req["uid"]
        return [encode_login_reply(LOGIN_RES, req["uid"], b8=CHAR_SLOTS)]
    if cmd == 3 and len(payload) >= 5:
        return [chars.pong(payload)]
    if cmd == 4:
        out = [chars.enter_reply(0)]
        if os.environ.get("NOBU_ZONE_ENTER", "1") == "1":
            # map (zone) = the character's saved field, else its faction's home province
            # (Oda -> 10 Owari), else 1; NOBU_ZONE overrides. area = NOBU_AREA (default 1).
            zone = chars.char_position(ctx.get("char"))[0]
            ctx["zone"] = zone
            out.append(chars.zone_enter(chars.area_for(zone), zone))
        return out
    if cmd == 0x23 and len(payload) >= 4 and payload[1] == 0x5A:
        # 23 5a <seq>: builder 0x1cc240 (net command 0x8f), posted by the UI's
        # WDMC_INIT_WORLD_ALL (UI event 0x141, 0x1d3ac4) once World.bin has loaded the field.
        # The 0x23 reply parser 0x283e30 only knows subs < 0x1e ("unknown sub command"), so
        # there is no reply; it is our cue that the world tasks exist -> spawn the player.
        return world_ready_pushes(ctx)
    if cmd == 0x23 and len(payload) >= 4:
        rep = chars.cmd23_reply(payload)
        out = [rep] if rep else []
        if payload[1] == 0x0D and ctx is not None:
            # 23 0d arrives when the quit countdown reaches 0; 0B 05 res 0 ends the quit
            out.append(chars.notify_logout(ctx.get("uid", 0), ctx.get("cid", 0)))
        return out
    if cmd in (0x24, 0x26, 0x2A):
        rep = chars.worldload_reply(payload)
        return [rep] if rep else []
    if cmd == 5 and len(payload) >= 2:
        sub = payload[1]
        r = vce.Reader(payload, 2)
        if sub == 3:
            uid = r.u32() if len(r.rest()) >= 4 else ctx.get("uid", 0)
            return [chars.char_list_reply(store().list(uid))]
        if sub == 4:
            # 05 04 u32 uid u32 cid (builder 0x190320, net 0x14): character delete. Reply `05 04
            # u32 res` is decoded by the first-stage parser (0x190f40 cmd 5 -> 0x1912e0, raw
            # u32), not 0x19c330; main 0x188e48 logs "SCMD_CHAR_DELETE: res=%d" and lets the
            # select screen go on when res >= 0. Answering with the list hung it.
            uid = r.u32() if len(r.rest()) >= 4 else ctx.get("uid", 0)
            cid = r.u32() if len(r.rest()) >= 4 else 0
            uid = ctx.get("uid", uid)
            gone = store().delete(uid, cid)
            return [chars.delete_reply(0 if gone else -1)]
        if sub == 1:
            req = chars.decode_create_request(payload)
            # the create request's slot counts from 1 (the first slot sends 1); the list and the
            # store count from 0. Storing it as-is put the character one slot down, and the
            # client's follow-up load read the empty slot's cid 0 -> res -1.
            c = store().create(req["uid"], req["slot"] - 1, req["family"], req["given"], req["looks"])
            return [chars.create_reply(0 if c else -1, c)]
        if sub in (8, 0x0C):
            # SCMD_CHAR_FRIEND_GET (reader 0x19ca10) / SCMD_CHAR_IGNORE_GET (0x19d318): u32 (unused),
            # u32 count (<= 50), entries; empty lists.
            return [vce.put_u8(5) + vce.put_u8(sub) + vce.put_u32(0) + vce.put_u32(0)]
        if sub == 2:
            a = r.u32() if len(r.rest()) >= 4 else 0
            b = r.u32() if len(r.rest()) >= 4 else 0
            uid = ctx.get("uid", a)
            c = store().find(uid, b) or store().find(uid, a)
            if c:
                ctx["char"], ctx["cid"] = c, c["cid"]
            return [chars.load_reply(0 if c else -1, c)]
        if sub == 5 and len(payload) >= 16:
            # 05 05 x y z dir flag: the local player's position report (0x190490); no reply.
            m = chars.decode_move(payload)
            ctx["pos"] = m
            if ctx.get("char"):
                store().set_pos(ctx.get("uid", 0), ctx["cid"],
                                [ctx.get("zone", chars.char_position(ctx["char"])[0]),
                                 m["x"], m["y"], m["z"], m["dir"]])
            return enemies.on_move(ctx, m)             # enemies coming into / out of view
    return []


# ---------------------------------------------------------------------------- server

def now():
    return dt.datetime.now(dt.timezone.utc)


def event(**kw):
    kw["t"] = now().isoformat()
    with open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(kw) + "\n")
    print(json.dumps(kw), flush=True)


class Session:
    def __init__(self, reader, writer, log):
        self.reader, self.writer, self.log = reader, writer, log
        self.buf = bytearray()

    def record(self, direction, data, note=""):
        from .connsv import hexdump
        self.log.write(f"{now().isoformat()} {direction} {len(data)} bytes{note}\n{hexdump(data)}\n")
        self.log.flush()

    async def need(self, parse):
        """Read until parse(buf) returns non-None; consume what it used."""
        while True:
            got = parse(bytes(self.buf))
            if got is not None:
                return got
            data = await asyncio.wait_for(self.reader.read(65536), IDLE)
            if not data:
                raise ConnectionError("peer closed")
            self.record("C>S raw", data)
            self.buf += data

    async def run(self, sid):
        # 1. client hello: u32 major, u32 key length
        major, keylen = await self.need(
            lambda b: vcecrypto.parse_client_hello(b) if len(b) >= 8 else None)
        del self.buf[:8]
        self.log.write(f"  = client hello: vce major {major}, key length {keylen}\n")
        hs = vcecrypto.ServerHandshake()
        hello = hs.hello(keylen)
        self.writer.write(hello)
        await self.writer.drain()
        self.record("S>C raw", hello, f"  = server hello g={hs.g} p={len(hs.p)} hex digits Y={len(hs.Y)}")
        # 2. client X
        x_hex, used = await self.need(vcecrypto.parse_lp)
        del self.buf[:used]
        key = hs.finish(x_hex)
        self.log.write(f"  = client X ({len(x_hex)} hex digits); session key {key.hex()}\n")
        event(ev="world_keyex", sid=sid, key=key.hex())
        bf = vcecrypto.Blowfish(key)
        records = vcecrypto.RecordReader(bf)
        frames = vce.FrameReader()
        ctx = {}

        def push(rep):                              # S>C frame from another session (social)
            self.writer.write(vcecrypto.seal(bf, vce.frame(rep)))
            self.record("S>C plain", vce.frame(rep), f"  = push {describe(rep, 'S>C')}")
        ctx["push"] = push
        ctx["close"] = self.writer.close           # an admin's kick (nobu/adminport.py)
        social.register(sid, ctx)
        pending = bytes(self.buf)
        self.buf.clear()
        while True:
            if pending:
                data, pending = pending, b""
            else:
                data = await asyncio.wait_for(self.reader.read(65536), IDLE)
                if not data:
                    break
                self.record("C>S raw", data)
            plain = records.feed(data)
            if plain:
                self.record("C>S plain", plain)
            for payload in frames.feed(plain):
                what = describe(payload, "C>S")
                self.log.write(f"  = {what}\n")
                event(ev="world_frame", sid=sid, dir="C>S", cmd=payload[0] if payload else None,
                      what=what)
                try:
                    replies = answer(payload, ctx)
                except vce.WireError as e:
                    self.log.write(f"  ! cannot answer: {e}\n")
                    replies = []
                for rep in replies:
                    wire = vcecrypto.seal(bf, vce.frame(rep))
                    self.writer.write(wire)
                    await self.writer.drain()
                    self.record("S>C plain", vce.frame(rep), f"  = {describe(rep, 'S>C')}")
                    event(ev="world_frame", sid=sid, dir="S>C", cmd=rep[0], what=describe(rep, "S>C"))


SESSION_TASKS = {}                                   # task -> writer, for the graceful shutdown


async def session(reader, writer):
    task = asyncio.current_task()
    SESSION_TASKS[task] = writer
    try:
        await _session(reader, writer)
    finally:
        SESSION_TASKS.pop(task, None)


async def _session(reader, writer):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{PORT}_world"
    path = os.path.join(LOG_DIR, "sessions", sid + ".log")
    event(ev="open", sid=sid, peer=peer[0], port=PORT, svc="world")
    with open(path, "a", encoding="utf-8") as log:
        try:
            await Session(reader, writer, log).run(sid)
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except (ConnectionError, vce.WireError, vcecrypto.WireError, ValueError) as e:
            log.write(f"{now().isoformat()} {e!r}\n")
        finally:
            trade.on_leave(social.SESSIONS.get(sid), store)   # close an open trade (nobu/trade.py)
            players.on_leave(social.SESSIONS.get(sid))  # hide the character, leave the party
            upkeep.on_leave(social.SESSIONS.get(sid), store)   # stop the upkeep ticker, save
            social.unregister(sid)
        log.write(f"{now().isoformat()} closed\n")
    writer.close()
    event(ev="close", sid=sid)


async def shutdown(srv, grace=None):
    """SIGTERM / docker stop: stop accepting, close every session (their finally blocks run the
    trade / party / upkeep leave hooks and save), then flush the deferred character save."""
    grace = float(os.environ.get("NOBU_SHUTDOWN_SECS", "5")) if grace is None else grace
    if srv is not None:
        srv.close()
    tasks = list(SESSION_TASKS)
    print(f"world: shutting down, closing {len(tasks)} session(s)", file=sys.stderr, flush=True)
    for w in list(SESSION_TASKS.values()):
        try:
            w.close()                               # the session's read sees EOF -> normal close path
        except Exception:
            pass
    if tasks:
        _, late = await asyncio.wait(tasks, timeout=grace)
        for t in late:                              # stuck (e.g. in drain): cancel; finally still runs
            t.cancel()
        if late:
            await asyncio.wait(late, timeout=grace)
    if STORE is not None:
        STORE.flush()
    print("world: data flushed, bye", file=sys.stderr, flush=True)


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    st = store()                                    # load (or recover) chars.json before listening:
    lock = persist.FileLock(st.path)                # an unrecoverable file stops the boot here
    if not lock.acquire():
        persist.loud(f"{st.path} is locked by another process (a second world server on the same data?)")
    srv = await asyncio.start_server(session, BIND, PORT)
    war_week.ensure_scheduler()                     # weekly war schedule (NOBU_WAR_SCHEDULE=0: off)
    live_task = asyncio.create_task(live.run(store)) # the boards' snapshot (nobu/live.py)
    adminport.start(asyncio.get_running_loop(), store,   # an OpenLobby panel (nobu/adminport.py)
                    log=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"world: listening on {BIND}:{PORT}, logs in {LOG_DIR}, chars {st.path}", file=sys.stderr, flush=True)
    stop = asyncio.Event()
    persist.on_shutdown_signals(stop.set)
    try:
        await stop.wait()
    finally:
        live_task.cancel()
        await shutdown(srv)
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
