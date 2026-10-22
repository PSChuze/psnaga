"""Quests ("requests") for the world server: the quest list, quest detail, and the tutorial
quest "Visit the castle overseer" given by the debug Guide NPC.

Addresses.

Server cmd 0x34 = quest list messages (KNOWN)
  DoResponse -> 0x18f5e0 table 0x420cb0[0x34-0x22] = 0x18f888 -> parser 0x28d3d0: u8 sub, u16 seq
  (-> 0x1c6c00, gp-0x7640 = the reply seq the request waiters compare), then per sub (table 0x428ec0):
    sub 1  0x1f27f0: u8 count, count x ENTRY            main -> UI 0x50 (0x1d6870: replace the list)
    sub 2  0x1f26c0: ENTRY                              main -> UI 0x51 (0x1d68b0: update by id or add
                                                        to the first free slot, then window msg 0x22)
    sub 3  u32 id                                       main -> UI 0x52 (0x1d6a30: remove by id, msg 0x22)
    sub 4  ENTRY                                        main -> UI 0x53 (0x1d6be0: update only if present)
    sub 5  [u8 kind; kind 1: u32 type, type 1: 4 x u32 [+ u8], type 2: u8]   -> UI 0x54 (not decoded)
    sub 6  u16, u32 cid?, u32 x3, u16, u16, u16          -> UI 0x55 (not decoded)
  ENTRY (0x1f26c0 read half) = u32 id, str title (max 0x41 SJIS incl. NUL), str text (max 0xc1).
  Main handler 0x28d780 (from 0x18b0c4, cmd 0x34 in 0x188530) posts UI event 0x4f + sub.
  The list lives in the UI object 0x4b8550: count +0x1b46c, 16 entries of 0x108 bytes at +0x1b470
  (u32 id, char title[0x41], char text[0xc1]); a slot is free when its id is 0 (id must be != 0).

The quest window (KNOWN): Profile window (UIMSG 2.3.0) tab, window code 0x66f810: heading 2.151, one
line per entry (entry +4 = title), 2.152 "No requests accepted." when the list is empty. Selecting a
line the first time (state window+0xe0+4*i == 0) calls 0x1d6810: request kind 0x22 -> net 0xb2 ->
builder 0x1cce00 = C>S `2a 03 <u16 seq> <u32 id>`, and waits (0x1d5650) for window message 0x22 with
that seq. Only UI 0x51 / 0x52 (cmd 0x34 subs 2 / 3) post message 0x22, so the answer is `34 02 <same
seq> ENTRY` (or `34 03 <seq> <id>`). Then the lower box shows entry +0x45 = the text (0x66fe40).
misc.py answers every other 0x2a sub with empty name tables; 2a 03 must reach this module first
(sending the 0x2a name reply would reset the name cache 0x512af0 and never complete the wait).

Not found in the client: an NPC accept / decline dialogue (no such UIMSG strings exist; 0.1.0 /
0.1.1 are generic), a reward message for quests, an objectives list for (the objectives window
0x6a9180 with 2.485.. "Defeat / Obtain / Talk to %1$s" is the trial (2.474) window). So accepting
is implicit (talking to the giver adds the quest with 34 02), the objective is written in the text,
completion removes it with 34 03, and the reward is announced as a chat line only.

Env:
  NOBU_QUESTS=0            disable this module (answer() returns None for everything)
  NOBU_QUEST_OVERSEER=0    do not spawn the Overseer NPC; the tutorial then completes on the next
                           talk to the Guide (talk-to objective on the giver itself)
  NOBU_QUEST_OVERSEER_CID  default 0xfea00002 (NPC range, see npc.is_npc_cid)
  NOBU_QUEST_OVERSEER_DX / _DZ  offset from the player's spawn point (default -300 / 300)
"""
import datetime as dt
import os

from . import chars, misc, npc, towns, vce

CMD = 0x34
SUB_LIST, SUB_ADD, SUB_REMOVE, SUB_UPDATE, SUB_5, SUB_6 = 1, 2, 3, 4, 5, 6
SUB_NAMES = {1: "list", 2: "add/update", 3: "remove", 4: "update-if-present", 5: "sub 5 (UI 0x54)",
             6: "sub 6 (UI 0x55)"}
MAX_ENTRIES = 16                 # UI list slots (0x1d68b0 / 0x1d6a30 loop 0x10); parser has no check
TITLE_MAX = 0x40                 # SJIS bytes (field 0x41 incl. NUL)
TEXT_MAX = 0xC0                  # SJIS bytes (field 0xc1 incl. NUL)

TUTORIAL = 1001

# Quest table. Texts are ours (no dialogue survives); the chain follows the 2003-04 tutorial
# (guide -> castle overseer Metsuke -> ...).
QUESTS = {
    TUTORIAL: dict(
        title="Visit the castle overseer",
        text=("The guide asks you to report to the castle overseer (Metsuke). "
              "Objective: talk to the Overseer at the notice board."),
        text_self=("The guide asks you to come back once you have looked around. "
                   "Objective: talk to the Guide again."),
        giver="guide", target="overseer",
        offer=("Welcome to {town}. Before anything else, report to the castle overseer, "
               "the Metsuke. He keeps the register of new retainers. I have noted it in "
               "your requests (Profile, Quest tab)."),
        remind="The Overseer is waiting for you at the notice board.",
        finish=("So the guide sent you. Good. Your name is now in the register of the {house} house. "
                "Go and make yourself useful."),
        finish_self="You came back. Good, that is all I wanted: you know how to find me.",
        after="You have met the Overseer. More tasks will come soon.",
        reward="Reward: the Overseer's recognition (items and money are not implemented yet).",
    ),
}


def enabled():
    return os.environ.get("NOBU_QUESTS", "1") != "0"


# ---------------------------------------------------------------------------- wire
def clip(text, limit):
    return misc.clip_sjis(text, limit)


def entry(qid, title, text):
    """ENTRY = u32 id, str title, str text (0x1f26c0)."""
    if not qid:
        raise ValueError("quest id 0 marks a free slot in the client list")
    return (chars.W().u32(qid).str(clip(title, TITLE_MAX)).str(clip(text, TEXT_MAX)).bytes())


def header(sub, seq=0):
    return vce.put_u8(CMD) + vce.put_u8(sub) + vce.put_u16(seq)


def quest_list(entries, seq=0):
    """34 01 <seq> u8 count, count x ENTRY (replaces the whole list; at most 16)."""
    entries = list(entries)[:MAX_ENTRIES]
    return header(SUB_LIST, seq) + vce.put_u8(len(entries)) + b"".join(entries)


def quest_add(ent, seq=0):
    """34 02 <seq> ENTRY: add, or update the entry with that id; completes a `2a 03` wait."""
    return header(SUB_ADD, seq) + ent


def quest_remove(qid, seq=0):
    """34 03 <seq> u32 id: remove the entry."""
    return header(SUB_REMOVE, seq) + vce.put_u32(qid)


def quest_update(ent, seq=0):
    """34 04 <seq> ENTRY: update in place only if the id is listed (no window message)."""
    return header(SUB_UPDATE, seq) + ent


def decode_detail_request(payload):
    """C>S `2a 03 <u16 seq> <u32 id>` (builder 0x1cce00, net 0xb2, request kind 0x22)."""
    r = vce.Reader(payload, 2)
    seq = r.u16()
    return dict(seq=seq, id=r.u32() if len(r.rest()) >= 4 else 0)


# ---------------------------------------------------------------------------- state
def now():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def qstate(c, qid):
    """Per-character state in chars.json: c["quests"][str(id)] = {"state": "active" | "done", ...}."""
    return (c or {}).get("quests", {}).get(str(qid), {}).get("state")


def set_state(ctx, store, qid, state, **extra):
    c = ctx.get("char")
    if c is None:
        return
    rec = c.setdefault("quests", {}).setdefault(str(qid), {})
    rec["state"] = state
    rec[state] = now()
    rec.update(extra)
    st = store() if callable(store) else store
    if st is not None:
        st.save()


def quest_text(ctx, qid):
    q = QUESTS[qid]
    return q["text_self"] if target_role(ctx, qid) == q["giver"] else q["text"]


def active_entries(ctx):
    c = ctx.get("char") or {}
    out = []
    for key, rec in sorted(c.get("quests", {}).items()):
        qid = int(key)
        if rec.get("state") == "active" and qid in QUESTS:
            out.append(entry(qid, QUESTS[qid]["title"], quest_text(ctx, qid)))
    return out


# ---------------------------------------------------------------------------- NPC roles
def overseer_cid():
    cid = int(os.environ.get("NOBU_QUEST_OVERSEER_CID", "0xfea00002"), 0) & 0xFFFFFFFF
    return npc.check_npc_cid(cid, "NOBU_QUEST_OVERSEER_CID")


def role_of(ctx, cid):
    n = ctx.get("npcs", {}).get(cid)
    return n.get("role", "guide" if cid == npc.npc_cid() else None) if n else None


def target_role(ctx, qid):
    """The overseer when it was spawned, else the giver itself (fallback objective)."""
    q = QUESTS[qid]
    have = any(n.get("role") == q["target"] for n in ctx.get("npcs", {}).values())
    return q["target"] if have else q["giver"]


def overseer_npc(player):
    lk = npc.npc_looks(player, "overseer")
    return dict(cid=overseer_cid(), uid=0, b6b=npc.TALK_ONLY, family="Overseer", given="", looks=lk,
                level=player.get("level", 1), role="overseer",
                text="I keep the register of the castle. Do you have business with me?")


def overseer_position(player):
    """At the town's notice board (town_spots.json role "overseer"), else Nagoya offsets from the spawn point,
    turned with the town's gate in the other home towns (npc.town_spot)."""
    dx = int(os.environ.get("NOBU_QUEST_OVERSEER_DX", "-300"))
    dz = int(os.environ.get("NOBU_QUEST_OVERSEER_DZ", "300"))
    return npc.town_spot(player, dx, dz, 90, role="overseer")


def local(n, text):
    """Fill {town} / {house} with the town NPC n stands in (towns.py; Nagoya / Oda outside the home towns)."""
    t = towns.town((n.get("pos") or [0])[0]) or towns.TOWNS[towns.NAGOYA]
    return text.format(town=t["name"], house=t["house"])


def world_ready(ctx, store=None):
    """Pushes after the spawn pushes (23 5a, after npc.debug_spawn): the Overseer NPC when the
    debug Guide is on, then the character's quest list (34 01) when it has active quests."""
    if not enabled() or not ctx.get("char"):
        return []
    out = []
    player = ctx["char"]
    guide = ctx.get("npcs", {}).get(npc.npc_cid()) if npc.debug_enabled() else None
    if guide is not None:
        guide.setdefault("role", "guide")
        if os.environ.get("NOBU_QUEST_OVERSEER", "1") == "1":
            o = overseer_npc(player)
            pos = overseer_position(player)
            o["pos"] = list(pos)
            ctx["npcs"][o["cid"]] = o
            out += npc.spawn_pushes(o, pos)
    active = active_entries(ctx)
    if active:                                          # the client list starts empty per login
        out.append(quest_list(active))
    return out


# ---------------------------------------------------------------------------- talk
def say(n, text, popup=True):
    out = [npc.notify_npc_say(n["cid"], text, name=npc.npc_name(n))]
    if popup:
        out.append(misc.notify_message(text))
    return out


def on_talk(ctx, n, store=None):
    """Frames for ○ on NPC n (28 01), or None to leave the default NPC line to npc.py."""
    role = n.get("role") or role_of(ctx, n["cid"])
    if role not in ("guide", "overseer"):
        return None
    q = QUESTS[TUTORIAL]
    st = qstate(ctx.get("char"), TUTORIAL)
    tgt = target_role(ctx, TUTORIAL)
    if role == "guide" and st is None:
        set_state(ctx, store, TUTORIAL, "active")
        return (say(n, local(n, q["offer"]))
                + [quest_add(entry(TUTORIAL, q["title"], quest_text(ctx, TUTORIAL))),
                   misc.notify_system(f"New request: {q['title']}")])
    if st == "active" and role == tgt:
        set_state(ctx, store, TUTORIAL, "done")
        text = q["finish_self"] if role == "guide" else q["finish"]
        return (say(n, local(n, text))
                + [quest_remove(TUTORIAL),
                   misc.notify_system(f"Request complete: {q['title']}"),
                   misc.notify_system(q["reward"])])
    if role == "guide" and st == "active":
        return say(n, q["remind"])
    if role == "guide" and st == "done":
        return say(n, q["after"])
    return None                                         # overseer before / after: default line


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """2a 03 quest detail and 28 01 on a quest NPC; None for everything else."""
    if not enabled() or len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x2A and sub == 3:
        d = decode_detail_request(payload)
        if qstate(ctx.get("char"), d["id"]) == "active" and d["id"] in QUESTS:
            q = QUESTS[d["id"]]
            return [quest_add(entry(d["id"], q["title"], quest_text(ctx, d["id"])), d["seq"])]
        # unknown / finished id: remove it (0x1d6a30 ignores id 0) - still posts msg 0x22
        return [quest_remove(d["id"], d["seq"])]
    if cmd == 0x28 and sub == 1:
        n = ctx.get("npcs", {}).get(ctx.get("target"))
        if n is None:
            return None
        out = on_talk(ctx, n, store)
        if out is not None:
            ctx.setdefault("actions", []).append(ctx.get("target"))
        return out
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if direction == "C>S" and cmd == 0x2A and sub == 3:
            d = decode_detail_request(payload)
            return f"QUEST detail request seq={d['seq']} id={d['id']}"
        if direction == "S>C" and cmd == CMD:
            r = vce.Reader(payload, 2)
            seq = r.u16()
            label = f"QUEST {SUB_NAMES.get(sub, f'sub {sub}')} seq={seq}"
            if sub == SUB_LIST:
                n = r.u8()
                items = []
                for _ in range(n):
                    qid, title = r.u32(), r.bytes().decode("utf-8", "replace")
                    r.bytes()
                    items.append(f"{qid}:{title!r}")
                return f"{label} n={n} " + ", ".join(items)
            if sub in (SUB_ADD, SUB_UPDATE):
                qid, title = r.u32(), r.bytes().decode("utf-8", "replace")
                return f"{label} id={qid} {title!r}"
            if sub == SUB_REMOVE:
                return f"{label} id={r.u32()}"
            return f"{label} {r.rest().hex()}"
    except vce.WireError:
        return None
    return None
