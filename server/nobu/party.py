"""Parties: CODE_PARTY requests (cmd 7) and NOTIFY_PARTY pushes (cmd 0x0E). Static reading
of.

C>S CODE_PARTY, builders on the world object (vtable 0x4501a0), no sequence number (KNOWN):
  0x190930  07 01 u32 cid      SCMD_PARTY_INVITE   net 0x27 (0x18ea00 -> 0x193e20)
  0x1909d0  07 02              SCMD_PARTY_DISBAND  net 0x29
  0x190a30  07 03 u32 cid      SCMD_PARTY_FOLLOW   net 0x2a
  0x190ad0  07 04 u32 cid      SCMD_PARTY_CANCEL   net 0x2b
  0x190b70  07 05              SCMD_PARTY_INFO     net 0x2d
  0x190bd0  07 06              SCMD_PARTY_STATUS   net 0x2e
  0x1c86d0  07 07 str name     (invite by name, SJIS -> UTF-8 max 0x3f) net 0x28; no reply parser
  0x1c88c0.. 07 08 u32 / 07 09 u32 / 07 0a u32 / 07 0b u8 / 07 0c / 07 0d u16 seq, u8 (second
             group system, see below; not answered)
S>C CODE_PARTY replies (DoResponse 0x19ea88 subs 0xb / 0xd, base parser 0x191528 table 0x420d40):
  subs 1..6, 8, 9, 0xa: u32 a, u32 b. Main handler 0x1891b0 (table 0x4206f0): sub 1 -> UI 0x102
  (0x1d1a98: struct {res, cid}: names of cid, message box by res: 1 -> 0xc, -0x138 -> 5, -0x131 -> 8,
  -0x13b -> 0xd, -0x13c -> 0xe, anything else (0 too) -> 0x11); subs 2..6 only log
  "Recv: CODE_PARTY: %s: res=%d" (res = a). HYPOTHESIS: a = res, b = the cid concerned.
S>C NOTIFY_PARTY cmd 0x0E (DoResponse 0x19fad8 subs 5, 7..0xd; base parser 0x192548 subs 1..4, 6;
  main 0x18a1f8 table 0x420620, log "Recv: NOTIFY_PARTY: ..."):
  01 INVITE  u32 leader        -> UI 0xf9 (0x1d1680): names of leader, window 0x78 (invitation),
                                  message box 3; unknown cid -> name request first (0x1cd5f0)
  02 DISBAND u32 cid           -> UI 0xfd (0x1d18f8): message box 7, window 0x79
  03 STATUS  u32 pid           -> UI 0xfa (0x1d1718): window 4 {pid}; pid 0 -> window 0x7b
  04 CANCEL  u32 cid           -> UI 0xfe (0x1d1940): window 0xf {0, cid} (member out), box 5
  05 INFO    u32 array (max 7) -> UI 0xfb (0x1d1780): window 5 {7 cids, zero padded}
  06 JOIN    u32 cid           -> UI 0xfc (0x1d17c0): window 0xf {1, cid} (member in), 0x7a, box 6
  07 UPDATE  u32 cid, u8 hp%, u8 mp%, u8, u8, u8 (archive 0x1f1e70) -> UI 0x100: window 0x7d
  08 archive 0x1f2040 -> UI 0x101 (window 0x7e); 09 u32 -> 0x109; 0a u32 + u32 array (max 7) ->
  0x10a; 0b u32 -> 0x10c; 0c u32 -> 0x10e; 0d archive 0x1a7a80 -> 0x110 (second group system).

Second group system (KNOWN which object, HYPOTHESIS what it is): UI+0x120d4 holds a member list
  (lookup 0x1debe0 by cid) that NOTIFY_CHAT 0D 04 (party chat, UI 0xcb) uses to name the speaker,
  that the action hint uses to forbid attacking a member, and whose +0xc (UI+0x120e0) gates the
  "/n/" chat channel. It is filled by NOTIFY_PARTY 0a `u32 leader, u32[] members` (0x1de290 ->
  0x1de070) and 0b `u32 cid` (0x1dd7f0: +0xc = +0x10 = cid if it is a member; message box 9).
  We send 0a + 0b alongside the named messages so party chat has names and "/n/" opens.

Flow implemented (HYPOTHESIS for which request the invitee's "accept" sends; all are accepted):
  A: 07 01 <B>  -> A: 07 01 <0> <B>;  B: 0E 01 <A>                         (invitation)
  B: 07 03 <A>  (or 07 01 <A> back) -> B joins A's party (created on the first accept):
     B: 07 03 <0> <A>; every member: 0E 06 <B>, 0E 05 <members>, 0E 03 <pid>, 0E 07 per member,
     0E 0A <leader> <members>, 0E 0B <leader>
  B: 07 04 <A>  -> decline: B: 07 04 <0> <A>; A: system line "<B> declined"
  leader: 07 04 <member> -> kick; member: 07 04 <self> or 07 02 -> leave; leader: 07 02 -> disband
     leaver: 0E 02 <self>, 0E 03 0, 0E 0A 0 [];  rest: 0E 04 <leaver>, 0E 05, 0E 0A (or disband
     when one member is left)
  07 05 -> 07 05 <0> <pid> + 0E 05; 07 06 -> 07 06 <0> <pid> + 0E 03 <pid>
Cids on the wire go through players.wire_cid per recipient (own cid plain, others 0xfe......).
"""
import os

from . import players, social, vce

MAX_MEMBERS = 7                   # NOTIFY_PARTY INFO / 0a arrays (0x1292b0 max 7)
RES_OK, RES_FAIL = 0, -1
FULL_HP = 100

PARTIES = {}                      # pid -> {"leader": cid, "members": [cid, ...]} (real cids)
INVITES = {}                      # invitee cid -> set of inviter cids
_NEXT = [1]

SUB_NAMES = {1: "INVITE", 2: "DISBAND", 3: "FOLLOW", 4: "CANCEL", 5: "INFO", 6: "STATUS",
             7: "INVITE_BY_NAME"}
NOTIFY_NAMES = {1: "INVITE", 2: "DISBAND", 3: "STATUS", 4: "CANCEL", 5: "INFO", 6: "JOIN",
                7: "UPDATE", 0xA: "MEMBERS", 0xB: "LEADER"}


def reset():
    PARTIES.clear()
    INVITES.clear()
    _NEXT[0] = 1


# ---------------------------------------------------------------------------- messages
def reply(sub, res, b=0):
    """07 <sub> u32 res, u32 b (base parser 0x191590 for subs 1..6)."""
    return vce.put_u8(7) + vce.put_u8(sub) + vce.put_u32(res & 0xFFFFFFFF) + vce.put_u32(b)


def notify_u32(sub, v):
    return vce.put_u8(0x0E) + vce.put_u8(sub) + vce.put_u32(v)


def notify_invite(leader_w):
    return notify_u32(1, leader_w)


def notify_disband(cid_w):
    return notify_u32(2, cid_w)


def notify_status(pid):
    return notify_u32(3, pid)


def notify_cancel(cid_w):
    return notify_u32(4, cid_w)


def notify_join(cid_w):
    return notify_u32(6, cid_w)


def u32_array(vals):
    out = vce.put_len(len(vals))
    for v in vals:
        out += vce.put_u32(v)
    return out


def notify_info(cids_w):
    """0E 05 u32 array (0x1292b0, max 7)."""
    return vce.put_u8(0x0E) + vce.put_u8(5) + u32_array(list(cids_w)[:MAX_MEMBERS])


def notify_update(cid_w, hp=FULL_HP, mp=FULL_HP):
    """0E 07 archive 0x1f1e70: u32 cid, u8 hp %, u8 mp %, u8, u8, u8."""
    return (vce.put_u8(0x0E) + vce.put_u8(7) + vce.put_u32(cid_w) + vce.put_u8(hp) + vce.put_u8(mp)
            + b"\x00\x00\x00")


def notify_members(leader_w, cids_w):
    """0E 0A u32, u32 array (max 7) -> 0x1de290: the member list of the UI+0x120d4 object."""
    return (vce.put_u8(0x0E) + vce.put_u8(0xA) + vce.put_u32(leader_w)
            + u32_array(list(cids_w)[:MAX_MEMBERS]))


def notify_leader(cid_w):
    """0E 0B u32 -> 0x1dd7f0: UI+0x120e0 = cid when it is a member ("/n/" channel open)."""
    return notify_u32(0xB, cid_w)


def system_line(text):
    return social.notify_system(text)


# ---------------------------------------------------------------------------- state helpers
def party_of(cid):
    for pid, p in PARTIES.items():
        if cid in p["members"]:
            return pid
    return 0


def name_of(cid):
    o = players.session_by_cid(cid)
    return social.char_name(o["char"]) if o else f"#{cid}"


def w(cid, viewer):
    return players.wire_cid(cid, viewer)


def send(cid, payload):
    """Push to the live session of character cid; False if it is offline."""
    return players.push(players.session_by_cid(cid), payload)


def member_frames(pid, viewer):
    """Everything a member needs to (re)draw the party: INFO, STATUS, UPDATE per member, and the
    second-system member list + leader (party chat names, "/n/")."""
    p = PARTIES[pid]
    ms = [w(m, viewer) for m in p["members"]]
    out = [notify_info(ms), notify_status(pid)]
    out += [notify_update(x) for x in ms]
    out += [notify_members(w(p["leader"], viewer), ms), notify_leader(w(p["leader"], viewer))]
    return out


def left_frames(me):
    """What the character that is no longer in a party gets."""
    return [notify_disband(me), notify_status(0), notify_members(0, [])]


def broadcast_party(pid, build, exclude=None):
    """Push build(viewer_cid) (a list of payloads) to every online member except `exclude`."""
    for m in list(PARTIES[pid]["members"]):
        if m == exclude:
            continue
        for f in build(m):
            send(m, f)


# ---------------------------------------------------------------------------- operations
def invite(ctx, target):
    """The sender (ctx) invites real cid target. Returns the sender's replies."""
    me = players.my_cid(ctx)
    o = players.session_by_cid(target)
    if target == me or o is None:
        return [reply(1, RES_FAIL, w(target, me))]
    if me in INVITES and target in INVITES[me]:        # inviting back = accepting (HYPOTHESIS)
        return accept(ctx, target, sub=1)
    mine = party_of(me)
    if party_of(target) or (mine and (PARTIES[mine]["leader"] != me
                                      or len(PARTIES[mine]["members"]) >= MAX_MEMBERS)):
        return [reply(1, RES_FAIL, w(target, me))]
    INVITES.setdefault(target, set()).add(me)
    players.push(o, notify_invite(w(me, target)))
    return [reply(1, RES_OK, w(target, me))]


def accept(ctx, leader, sub=3):
    """ctx joins the party of the character that invited it."""
    me = players.my_cid(ctx)
    if leader not in INVITES.get(me, ()) or party_of(me):
        return [reply(sub, RES_FAIL, w(leader, me))]
    INVITES[me].discard(leader)
    pid = party_of(leader)
    if not pid:
        pid = _NEXT[0]
        _NEXT[0] += 1
        PARTIES[pid] = {"leader": leader, "members": [leader]}
    p = PARTIES[pid]
    if len(p["members"]) >= MAX_MEMBERS:
        return [reply(sub, RES_FAIL, w(leader, me))]
    p["members"].append(me)
    broadcast_party(pid, lambda v: [notify_join(w(me, v))] + member_frames(pid, v), exclude=me)
    return [reply(sub, RES_OK, w(leader, me)), notify_join(me)] + member_frames(pid, me)


def decline(ctx, leader):
    me = players.my_cid(ctx)
    INVITES.get(me, set()).discard(leader)
    send(leader, system_line(f"{name_of(me)} declined the party invitation."))
    return [reply(4, RES_OK, w(leader, me))]


def remove(cid, pid):
    """Take cid out of party pid; pushes to everyone concerned. Disbands at one member left."""
    p = PARTIES.get(pid)
    if not p or cid not in p["members"]:
        return
    if cid == p["leader"]:
        disband(pid)
        return
    p["members"].remove(cid)
    for f in left_frames(cid):
        send(cid, f)
    if len(p["members"]) <= 1:
        disband(pid)
        return
    broadcast_party(pid, lambda v: [notify_cancel(w(cid, v))] + member_frames(pid, v))


def disband(pid):
    p = PARTIES.pop(pid, None)
    if not p:
        return
    for m in p["members"]:
        send(m, notify_disband(w(p["leader"], m)))
        for f in left_frames(m)[1:]:
            send(m, f)


def leave(ctx, sub=2):
    me = players.my_cid(ctx)
    pid = party_of(me)
    if not pid:
        return [reply(sub, RES_FAIL, me)]
    if PARTIES[pid]["leader"] == me and sub == 2:
        disband(pid)
    else:
        remove(me, pid)
    return [reply(sub, RES_OK, me)]


def cancel(ctx, target):
    """07 04 <cid>: decline an invitation from cid, kick cid (leader), or leave (own cid / 0)."""
    me = players.my_cid(ctx)
    if target in INVITES.get(me, ()):
        return decline(ctx, target)
    if target in (0, me):
        return leave(ctx, sub=4)
    pid = party_of(me)
    if pid and PARTIES[pid]["leader"] == me and target in PARTIES[pid]["members"]:
        remove(target, pid)
        return [reply(4, RES_OK, w(target, me))]
    if me in INVITES.get(target, ()):                   # withdrawing an invitation
        INVITES[target].discard(me)
        return [reply(4, RES_OK, w(target, me))]
    return [reply(4, RES_FAIL, w(target, me))]


def on_leave(ctx):
    """Session end: leave the party, forget invitations to and from this character."""
    me = players.my_cid(ctx)
    if me is None:
        return
    pid = party_of(me)
    if pid:
        remove(me, pid)
    INVITES.pop(me, None)
    for s in INVITES.values():
        s.discard(me)


def party_chat(ctx, text):
    """'/n/' line: NOTIFY_CHAT 0D 04 (u8 1, u32 speaker, str) to every member (the speaker gets
    its own plain cid). Outside a party: nothing (the client only sends "/n/" while UI+0x120e0
    is set and prints its own error line otherwise)."""
    me = players.my_cid(ctx)
    pid = party_of(me)
    if not pid:
        return []
    for m in PARTIES[pid]["members"]:
        if m != me:
            send(m, social.notify_chat(w(me, m), text, sub=4))
    return [social.notify_chat(me, text, sub=4)]


# ---------------------------------------------------------------------------- dispatch
def find_by_name(name):
    key = name.replace(" ", "").replace("　", "")
    for c in social.SESSIONS.values():
        ch = c.get("char")
        if ch and key and key in (ch.get("family", "") + ch.get("given", ""),
                                  ch.get("family", ""), ch.get("given", "")):
            return ch["cid"]
    return None


def answer(payload, ctx, store=None):
    """Replies for cmd 7 (CODE_PARTY), or None for any other frame."""
    if len(payload) < 2 or payload[0] != 7 or not (ctx or {}).get("char"):
        return None
    sub = payload[1]
    r = vce.Reader(payload, 2)
    arg = r.u32() if sub in (1, 3, 4, 8, 9, 0xA) and len(payload) >= 6 else None
    if sub == 1 and arg is not None:
        return invite(ctx, players.real_cid(arg))
    if sub == 2:
        return leave(ctx)
    if sub == 3 and arg is not None:
        return accept(ctx, players.real_cid(arg))
    if sub == 4 and arg is not None:
        return cancel(ctx, players.real_cid(arg))
    me = players.my_cid(ctx)
    if sub == 5:
        pid = party_of(me)
        ms = [w(m, me) for m in PARTIES[pid]["members"]] if pid else []
        return [reply(5, RES_OK, pid), notify_info(ms)]
    if sub == 6:
        pid = party_of(me)
        return [reply(6, RES_OK, pid), notify_status(pid)]
    if sub == 7 and len(payload) >= 3:
        name = r.bytes(0x100).decode("utf-8", "replace")
        target = find_by_name(name)
        if target is None or target == me:
            return [system_line(f"{name}: not online.")]
        return invite(ctx, target)
    return []                                            # 07 08..0d: second system, logged only


def describe(payload, direction="C>S"):
    if len(payload) < 2 or payload[0] not in (7, 0x0E):
        return None
    sub = payload[1]
    try:
        r = vce.Reader(payload, 2)
        if payload[0] == 7 and direction == "C>S":
            name = SUB_NAMES.get(sub, f"sub {sub}")
            if sub == 7:
                return f"CODE_PARTY {name} {r.bytes().decode('utf-8', 'replace')!r}"
            rest = f" cid={r.u32():#x}" if len(payload) >= 6 else ""
            return f"CODE_PARTY {name}{rest}"
        if payload[0] == 7:
            return (f"CODE_PARTY {SUB_NAMES.get(sub, f'sub {sub}')} reply res={r.i32()} "
                    f"b={r.u32():#x}")
        name = NOTIFY_NAMES.get(sub, f"sub {sub}")
        if sub in (5,):
            n = r.length()
            return f"NOTIFY_PARTY {name} " + ",".join(f"{r.u32():#x}" for _ in range(n))
        if sub == 0xA:
            lead = r.u32()
            n = r.length()
            return (f"NOTIFY_PARTY {name} leader={lead:#x} "
                    + ",".join(f"{r.u32():#x}" for _ in range(n)))
        if sub == 7:
            return f"NOTIFY_PARTY {name} cid={r.u32():#x} hp={r.u8()}% mp={r.u8()}%"
        return f"NOTIFY_PARTY {name} {r.u32():#x}"
    except vce.WireError:
        return None
