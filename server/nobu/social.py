"""Chat, player search and friends for the world server (additive to world.answer()).

Addresses.

C>S chat line `23 08 <u16 seq> <str line>` (builder 0x1cc420, net command 0x4e posted by 0x206760).
  The chat UI formats the line with "/%1$s/%2$s" (0x432090, 0x3487a0): "/<channel>/<text>".
  Channel codes by chat mode (switch 0x346510, table 0x431f60): 0/1 "s" say, 2 "o" shout,
  3 "n" party (only sent when in a party), 4 "bt_s" troop, 13 "mm"/"mmt" (emote, HYPOTHESIS).
  The 0x23 reply parser 0x283e30 has no sub 8 (table 0x4283a0 -> "unknown sub command"): no reply.
S>C NOTIFY_CHAT cmd 0x0D (parser 0x19b710, sub table 0x420ff0; main handler 0x189f98, table 0x420660):
  sub 1 SAY / sub 4 PARTY (0x1a62a0): u8 kind, u32 cid, str msg (max 0x191 SJIS bytes).
    kind != 1 -> UI 0xa9; kind 1 and cid == net+0x48 (our cid) -> UI 0xe = chat line with the
    local player's own names (0x1cf6b0 -> chat window 0x2a6240); other cid -> UI 0xca (0x1d2400,
    looks the character up by cid 0x1d73e0 for its names). Sub 4 -> UI 0xcb.
  sub 6 CONDITION (0x19b8f8): bytes msg (max 0x320, UTF-8 -> SJIS) -> UI 0x72 = chat line type 7
    with empty names: a system line.
"""
import re

from . import vce

# ---------------------------------------------------------------------------- session registry
# sid -> ctx. world.Session registers its ctx (with ctx["push"] = callable(payload) that seals and
# writes one S>C frame) so replies can later reach other sessions (zone chat, find-all, tells).
SESSIONS = {}


def register(sid, ctx):
    SESSIONS[sid] = ctx


def unregister(sid):
    SESSIONS.pop(sid, None)


def online(zone=None):
    """ctx of every live session that has loaded a character (optionally in one zone)."""
    return [c for c in SESSIONS.values()
            if c.get("char") and (zone is None or c.get("zone") == zone)]


def broadcast(payload, zone=None, exclude=None):
    """Push one S>C payload to every live session with a character (in `zone` if given),
    except the ctx `exclude`. Returns how many sessions got it. Not wired for chat yet."""
    n = 0
    for c in online(zone):
        if c is exclude or not c.get("push"):
            continue
        c["push"](payload)
        n += 1
    return n


# ---------------------------------------------------------------------------- chat
CHANNELS = {"s": "say", "o": "shout", "n": "party", "bt_s": "troop", "mm": "emote",
            "mmt": "emote"}
LINE_RE = re.compile(r"^/([^/]*)/(.*)$", re.S)


def parse_line(line):
    """'/s/hello' -> ('s', 'hello'); a line without the /x/ prefix -> ('', line)."""
    m = LINE_RE.match(line)
    return (m.group(1), m.group(2)) if m else ("", line)


def decode_chat(payload):
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    if sub != 8:
        raise vce.WireError("not a chat line")
    line = r.bytes(0x120).decode("utf-8", "replace")
    ch, text = parse_line(line)
    return dict(seq=seq, line=line, channel=ch, text=text)


def notify_chat(cid, text, sub=1, kind=1):
    """0D <sub> u8 kind, u32 cid, str msg. sub 1 SAY (sub 4 PARTY). kind 1 = a player."""
    return (vce.put_u8(0x0D) + vce.put_u8(sub) + vce.put_u8(kind) + vce.put_u32(cid)
            + vce.put_bytes(text.encode("utf-8")))


def notify_system(text):
    """0D 06 bytes msg: NOTIFY_CODE_CHAT_CONDITION, a system line in the chat window."""
    return vce.put_u8(0x0D) + vce.put_u8(6) + vce.put_bytes(text.encode("utf-8"))


def char_name(c):
    return f"{c.get('family', '')} {c.get('given', '')}".strip()


def chat_replies(d, ctx):
    """Replies for one decoded chat line. say / shout are echoed to the sender as SAY (the
    client does not print its own line). Other channels are only logged for now."""
    cid = ctx.get("cid") or (ctx.get("char") or {}).get("cid", 0)
    if d["channel"] in ("s", "o", ""):
        rep = notify_chat(cid, d["text"])
        from . import players; players.say_to_zone(ctx, d["text"])   # speaker's wire cid per viewer
        return [rep]
    if d["channel"] == "n":
        from . import party; return party.party_chat(ctx, d["text"])
    return []


# ---------------------------------------------------------------------------- find (23 12)
def decode_find(payload):
    """`23 12 <u16 seq> <str query>` (builder 0x1ccb50); "" = find all."""
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    q = r.bytes(0x100).decode("utf-8", "replace") if len(r.rest()) else ""
    return dict(seq=seq, query=q)


def find_replies(d, ctx):
    """The 0x23 reply parser has no sub 0x12, so the result comes some other way (not found in
    the client yet). HYPOTHESIS: answer with system lines in the chat window listing the
    online characters whose name contains the query."""
    q = d["query"].replace(" ", "").replace("　", "")
    names = [char_name(c["char"]) for c in online()
             if q in (c["char"].get("family", "") + c["char"].get("given", ""))]
    if not any(c is ctx for c in SESSIONS.values()) and ctx.get("char"):
        me = char_name(ctx["char"])
        if q in me.replace(" ", "") and me not in names:
            names.append(me)
    out = [notify_system(f"Online: {len(names)}")]
    out += [notify_system(n) for n in names[:20]]
    return out


# ---------------------------------------------------------------------------- friends (CODE_CHAR)
def find_char_by_name(store, name, exclude_cid=None):
    """Exact family+given (spaces ignored), then family or given alone. exclude_cid (the
    requester) is never returned: you cannot befriend or block yourself."""
    key = name.replace(" ", "").replace("　", "")
    every = [c for cs in store.data["chars"].values() for c in cs if c["cid"] != exclude_cid]
    for test in (lambda c: c["family"] + c["given"] == key,
                 lambda c: key in (c["family"], c["given"])):
        hits = [c for c in every if test(c)]
        if hits:
            return hits[0]
    return None


def name_lookup_reply(c):
    """05 10 reply (parser 0x19d848, UI event 0x117): u32, u32, u32, bytes (max 0x20),
    bytes (max 0x20), then optional u8/u8. HYPOTHESIS: u32 res, u32 uid, u32 cid, family, given
    (the FRIEND_ADD request 0x197ab0 sends u32, u32, str, str = what this returns)."""
    if c is None:
        return (vce.put_u8(5) + vce.put_u8(0x10) + vce.put_u32(0xFFFFFFFF) + vce.put_u32(0)
                + vce.put_u32(0) + vce.put_bytes(b"") + vce.put_bytes(b""))
    return (vce.put_u8(5) + vce.put_u8(0x10) + vce.put_u32(0) + vce.put_u32(c["uid"])
            + vce.put_u32(c["cid"]) + vce.put_bytes(c["family"].encode("utf-8"))
            + vce.put_bytes(c["given"].encode("utf-8")))


def friend_get_reply(friends):
    """05 08 (reader 0x19ca10): u32 (unused), u32 count (<= 50), count x {u32 uid, u32 cid,
    bytes family (max 0x20), bytes given (max 0x20)}; the optional tail is omitted."""
    out = vce.put_u8(5) + vce.put_u8(8) + vce.put_u32(0) + vce.put_u32(len(friends[:50]))
    for f in friends[:50]:
        out += (vce.put_u32(f["uid"]) + vce.put_u32(f["cid"])
                + vce.put_bytes(f["family"].encode("utf-8")) + vce.put_bytes(f["given"].encode("utf-8")))
    return out


def ignore_lookup_reply(c):
    """05 0f reply (parser 0x19d628, shared with sub 0x11; UI event 0x11d): u32 +0, u32 +4,
    u32 +8, u32 +0x34, bytes family (max 0x20 -> +0x12), bytes given (-> +0x23); no tail.
    HYPOTHESIS: u32 res (-1 = not found), u32 uid, u32 cid, u32 0 (+0x34 unknown), names;
    the IGNORE_ADD request (0x1980a0) sends u32, u32, str, str back."""
    if c is None:
        return (vce.put_u8(5) + vce.put_u8(0x0F) + vce.put_u32(0xFFFFFFFF) + vce.put_u32(0)
                + vce.put_u32(0) + vce.put_u32(0) + vce.put_bytes(b"") + vce.put_bytes(b""))
    return (vce.put_u8(5) + vce.put_u8(0x0F) + vce.put_u32(0) + vce.put_u32(c["uid"])
            + vce.put_u32(c["cid"]) + vce.put_u32(0) + vce.put_bytes(c["family"].encode("utf-8"))
            + vce.put_bytes(c["given"].encode("utf-8")))


def ignore_get_reply(ignores):
    """05 0c (reader 0x19d318): u32 (unused), u32 count (<= 50), count x {u32, u32, u32,
    bytes (max 0x20), bytes (max 0x20)}. HYPOTHESIS entry = uid, cid, 0, family, given."""
    out = vce.put_u8(5) + vce.put_u8(0x0C) + vce.put_u32(0) + vce.put_u32(len(ignores[:50]))
    for f in ignores[:50]:
        out += (vce.put_u32(f["uid"]) + vce.put_u32(f["cid"]) + vce.put_u32(0)
                + vce.put_bytes(f["family"].encode("utf-8")) + vce.put_bytes(f["given"].encode("utf-8")))
    return out


def decode_friend_add(payload):
    """05 09 (builder 0x197ab0): u32, u32, str, str (HYPOTHESIS uid, cid, family, given)."""
    r = vce.Reader(payload, 2)
    return dict(uid=r.u32(), cid=r.u32(), family=r.bytes(0x100).decode("utf-8", "replace"),
                given=r.bytes(0x100).decode("utf-8", "replace"))


def res_reply(sub, res):
    """05 09 FRIEND_ADD / 05 0a FRIEND_DELETE replies (0x19cf08 / 0x19cf70): u32 only."""
    return vce.put_u8(5) + vce.put_u8(sub) + vce.put_u32(res)


# ---------------------------------------------------------------------------- profile (23 13/14/16, 23 0e)
# Set requests 0x1c6e30 (sub 0x13) / 0x1c7040 (sub 0x14): `23 <sub> <u16 seq> <str text>` (SJIS -> UTF-8,
# max 0x9f). Get request 0x1c6f60 (sub 0x16): `23 16 <u16 seq> <u32 id>`. No sub 0x17 request exists.
# Replies (0x283e30 table 0x4283a0): subs 0x13 / 0x16 -> 0x284510, subs 0x14 / 0x17 -> 0x284620:
# `u32 (read, unused), bytes text (max 0xa0 -> SJIS 0x51)`; callback (0x23, 0x16 resp. 0x17, text) -> UI dest 0x23.
# The profile's "Bio" is sub 0x14; sub 0x13 is a second text (HYPOTHESIS: comment).
PROFILE_KEYS = {0x13: "comment", 0x14: "bio"}


def profile_reply(sub, seq, text, x=0):
    return (vce.put_u8(0x23) + vce.put_u8(sub) + vce.put_u16(seq) + vce.put_u32(x)
            + vce.put_bytes(text.encode("utf-8")[:0xa0]))


def find_char_by_id(store, ident):
    every = [c for cs in store.data["chars"].values() for c in cs]
    for c in every:
        if c["cid"] == ident:
            return c
    for c in every:
        if c.get("uid") == ident:
            return c
    return None


def profile_answer(payload, ctx, store):
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    c = ctx.get("char")
    if sub in PROFILE_KEYS:
        text = r.bytes(0x100).decode("utf-8", "replace") if len(r.rest()) else ""
        if c is not None:
            c.setdefault("profile", {})[PROFILE_KEYS[sub]] = text
            if store is not None:
                store.save()
        return [profile_reply(sub, seq, text)]
    if sub == 0x16:
        ident = r.u32() if len(r.rest()) >= 4 else 0
        who = (find_char_by_id(store, ident) if store is not None else None) or c or {}
        prof = who.get("profile", {})
        return [profile_reply(0x16, seq, prof.get("comment", "")),
                profile_reply(0x17, seq, prof.get("bio", ""))]
    if sub == 0x0E:
        # 23 0e <seq> u8 kind, u32 value (builder 0x1cd0d0; live: kind 5 = player status, 4 =
        # "Buying"). The 0x23 parser has no sub 0x0e: no reply. Stored per character.
        kind, value = r.u8(), r.u32()
        if c is not None:
            c.setdefault("status", {})[str(kind)] = value
            if store is not None:
                store.save()
        return []
    return None


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """Replies for the social frames, or None when the frame is not ours (world.answer goes on)."""
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x23 and sub == 8 and len(payload) >= 4:
        d = decode_chat(payload)
        ctx.setdefault("chat_log", []).append(d)
        return chat_replies(d, ctx)
    if cmd == 0x23 and sub == 0x12 and len(payload) >= 4:
        return find_replies(decode_find(payload), ctx)
    if cmd == 0x23 and sub == 5 and len(payload) >= 4:
        # SCMD_GET_FRIENDSHIP_STATE (parser 0x284088 -> 0x1f1bb0): u8 bytes (max 15), u16 array
        # (max 15), u8 bytes (max 22), u8 bytes (max 22), u16 array (max 18); all empty.
        return [payload[:4] + b"\x00" * 5]
    if cmd == 0x23 and sub in (0x0E, 0x13, 0x14, 0x16) and len(payload) >= 4:
        return profile_answer(payload, ctx, store() if callable(store) else store)
    if cmd == 0x25 and sub == 2 and len(payload) >= 4:
        # CODE_CHONOBU_PC sub 2 (builder 0x1c9320, header only; sub 1 = SCMD_TARGET_SET with a
        # u32): the reply parser 0x28b190 sub 2 reads nothing and calls back -> UI event 0x92.
        return [payload[:4]]
    if cmd == 5 and store is not None and sub in (8, 9, 0xA, 0xC, 0xD, 0xE, 0xF, 0x10):
        store = store() if callable(store) else store
        c = ctx.get("char")
        me = c["cid"] if c else ctx.get("cid")
        if sub in (0xF, 0x10) and len(payload) >= 3:
            name = vce.Reader(payload, 2).bytes(0x200).decode("utf-8", "replace")
            hit = find_char_by_name(store, name, exclude_cid=me)
            return [ignore_lookup_reply(hit) if sub == 0xF else name_lookup_reply(hit)]
        if sub == 0xC and c and c.get("ignores"):
            return [ignore_get_reply(c["ignores"])]
        if sub == 0xD and c:
            f = decode_friend_add(payload)          # same layout as FRIEND_ADD (0x1980a0)
            fl = c.setdefault("ignores", [])
            if f["cid"] == c["cid"]:
                return [res_reply(0xD, -1)]
            if not any(x["cid"] == f["cid"] for x in fl):
                fl.append(f)
                store.save()
            return [res_reply(0xD, 0)]
        if sub in (0xA, 0xE) and c and len(payload) >= 6:
            # FRIEND_DELETE (0x197c70) / IGNORE_DELETE (0x198270): u32 (uid or cid, unknown
            # which: both match); replies 0x19cf70 / 0x19dd70 = u32 res.
            key = vce.Reader(payload, 2).u32()
            name = "friends" if sub == 0xA else "ignores"
            fl = c.get(name, [])
            keep = [x for x in fl if key not in (x["cid"], x["uid"])]
            if len(keep) != len(fl):
                c[name] = keep
                store.save()
            return [res_reply(sub, 0)]
        if sub == 8 and c and c.get("friends"):
            return [friend_get_reply(c["friends"])]
        if sub == 9 and c:
            f = decode_friend_add(payload)
            fl = c.setdefault("friends", [])
            if not any(x["cid"] == f["cid"] for x in fl) and f["cid"] != c["cid"]:
                fl.append(f)
                store.save()
            return [res_reply(9, 0)]
    return None


def describe(payload, direction="C>S"):
    """Log text for the frames above, or None."""
    if len(payload) < 4:
        return None
    try:
        if direction == "C>S" and payload[0] == 0x23 and payload[1] == 8:
            d = decode_chat(payload)
            return (f"CHAT {CHANNELS.get(d['channel'], d['channel'] or 'plain')} seq={d['seq']} "
                    f"{d['line']!r} -> {d['text']!r}")
        if direction == "C>S" and payload[0] == 0x23 and payload[1] == 0x12:
            d = decode_find(payload)
            return f"FIND seq={d['seq']} query={d['query']!r}"
        if direction == "C>S" and payload[0] == 0x23 and payload[1] in PROFILE_KEYS:
            r = vce.Reader(payload, 2)
            seq = r.u16()
            return (f"PROFILE set {PROFILE_KEYS[payload[1]]} seq={seq} "
                    f"{r.bytes().decode('utf-8', 'replace')!r}")
        if direction == "C>S" and payload[0] == 0x23 and payload[1] == 0x16:
            r = vce.Reader(payload, 2)
            return f"PROFILE get seq={r.u16()} id={r.u32():#x}"
        if direction == "C>S" and payload[0] == 0x23 and payload[1] == 0x0E:
            r = vce.Reader(payload, 2)
            return f"STATUS set seq={r.u16()} kind={r.u8()} value={r.u32()}"
        if direction == "C>S" and payload[0] == 0x25 and payload[1] == 2:
            return f"CODE_CHONOBU_PC sub 2 (target clear?) seq={vce.Reader(payload, 2).u16()}"
        if direction == "C>S" and payload[0] == 5 and payload[1] in (0xF, 0x10):
            what = "ignore" if payload[1] == 0xF else "friend"
            return (f"CODE_CHAR sub {payload[1]} ({what} name lookup) "
                    f"{vce.Reader(payload, 2).bytes().decode('utf-8', 'replace')!r}")
        if direction == "S>C" and payload[0] == 0x0D:
            r = vce.Reader(payload, 2)
            if payload[1] in (1, 4):
                return (f"NOTIFY_CHAT {'SAY' if payload[1] == 1 else 'PARTY'} kind={r.u8()} "
                        f"cid={r.u32()} {r.bytes().decode('utf-8', 'replace')!r}")
            if payload[1] == 6:
                return f"NOTIFY_CHAT CONDITION {r.bytes().decode('utf-8', 'replace')!r}"
    except vce.WireError:
        return None
    return None
