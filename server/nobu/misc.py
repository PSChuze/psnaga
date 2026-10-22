"""News, support reports (bug report / request / GM call) and inventory actions for the world server.

Addresses. world.answer() calls answer() first; None = not ours.

News (Function menu -> "News", UIMSG 2.17.1): the menu handler 0x6657a0 case 1 posts net command 0xb5
(0x1d6320) -> 0x18f508 -> builder 0x1ccee0: `2a 05 <u16 seq> <u32 Version = gp-0x7e34 = 20003>`. Every
cmd 0x2a reply goes to 0x18f6e8 (table 0x420cb0[8]) whatever its sub: u8 sub (ignored), u16 seq, then
the 4-group table 0x2b2bf0 fed to 0x2744c0 (dynamic name tables). No UI event follows, so no window can
come from the 0x2a reply itself. We answer it with the empty tables and show the news through
NOTIFY_CHAT: sub 6 system lines (chat window) and, for the newest entry, sub 5 CHAT_MESSAGE (UI 0x71 ->
0x2592b0 box 0x2c -> 0x258bf0 type 3, a message box; HYPOTHESIS for its look).

Support (UIMSG 2.37.x): window handler ~0x631500 calls the GM-support object (UI + 0x1a484):
0x294940 (kind 2), 0x294a10 (kind 3 = bug report, answer text UIMSG 2.248), 0x294ae0 (kind 4),
0x294bb0 (kind 5 = request, UIMSG 2.249). Each registers a waiter for message 0x61 with flag 1 (no seq
check, 0x1d9030 -> 0x1f9290) and posts net 0x41 -> 0x198310:
  C>S `09 01 u32 kind, str text (SJIS -> UTF-8, max 0x3ff), str extra (max 0x4fff)`.
  extra = 0x188410: "LD:" + "%u,%u:" per entry of net+0x5038 (count net+0x50b8) + "%s\n" (HYPOTHESIS:
  link / latency samples; empty in the live frame "LD:\n").
Reply parse 0x19bf80 (table 0x421040): sub 1 = u32 res, u32 ticket, u32 c, bytes text (max 0x3fe);
main 0x1892f0 table 0x4206d0 -> UI 0x139 -> 0x1fc400(msg 0x61) -> 0x295540 -> 0x295420:
res low 16 bits < 0 -> nothing happens (the window keeps waiting); obj+0x14 = res, +0x10 = ticket,
+0xc = c; non-empty text -> chat line; res high 16 == 1 and GM-call kind (0x295f20) -> state 2
(awaiting GM) else state 3 -> 0x295c40 (send the rest of the attachment with 09 03, else) -> state 5.
Subs 2..6 carry u32 res (sub 5: u32, u32, u32) -> UI 0x13b..0x13f.
Other requests: `09 02 u32 ticket, u32, str` (GM chat line, 0x1984e0), `09 03 u32, str` (attachment
chunk, 0x198770; acked by reply sub 3), `09 04 u32` (0x1986d0), `09 06 u32` (0x198630).

Inventory cmd 0x24 (reply parser 0x287270, table 0x4285e0; request builders 0x1c7380..0x1c79f0):
  req 2 u8 | 3 u8,u8,u32 | 4 u8,u8 | 5 u8 | 6 u8,u8,u32 | 7 - | 8 u32
  rep 2 item struct 0x1ece80 | 3 - | 4 u8,u8 | 5 u8 | 6 u8,u8[,u16] | 7 bytes(max 0xc) | 8 struct 0x1ee160
"""
import datetime as dt
import json
import os

from . import persist
from . import vce

VERSION = 20003


def data_dir():
    return os.environ.get("NOBU_DATA") or os.path.join(os.getcwd(), "data")


def assets_dir():
    """The game data extract/ built from the disc: NOBU_ASSETS, else assets/ beside server/."""
    return os.environ.get("NOBU_ASSETS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                                         "assets")


# ---------------------------------------------------------------------------- news
NEWS_SEED = [{
    "date": "",                          # the day the file is made
    "title": "Welcome to the Nobunaga no Yabou Online revival",
    "body": "The world server is back up. Things still break: use Support -> Report a bug to tell us.",
}]


def news_path():
    return os.environ.get("NOBU_NEWS") or os.path.join(data_dir(), "news.json")


def load_news(path=None):
    """news.json = [{"date", "title", "body"}, ...] (newest first after sorting by date). Created with
    one welcome entry when missing."""
    path = path or news_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            seed = [dict(n, date=n["date"] or dt.date.today().isoformat()) for n in NEWS_SEED]
            persist.atomic_write_json(path, seed, ensure_ascii=False, indent=2)
        except OSError:
            return list(NEWS_SEED)
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh)
    except (OSError, ValueError) as e:
        persist.warn_once((path, repr(e)), f"{path} does not parse ({e!r}); showing no news")
        return []
    if isinstance(items, dict):
        items = items.get("news", [])
    items = [i for i in items if isinstance(i, dict)]
    return sorted(items, key=lambda i: str(i.get("date", "")), reverse=True)


def clip_sjis(text, limit):
    """Cut text so its Shift-JIS size stays under limit (the client converts UTF-8 -> SJIS into
    fixed buffers)."""
    out = ""
    for ch in text:
        try:
            n = len((out + ch).encode("cp932"))
        except UnicodeEncodeError:
            ch = "?"
            n = len((out + ch).encode("cp932"))
        if n > limit:
            break
        out += ch
    return out


def notify_system(text):
    """0D 06 bytes: NOTIFY_CODE_CHAT_CONDITION, one system line in the chat window."""
    return vce.put_u8(0x0D) + vce.put_u8(6) + vce.put_bytes(clip_sjis(text, 0x190).encode("utf-8"))


def notify_message(text):
    """0D 05 bytes: NOTIFY_CODE_CHAT_MESSAGE -> UI 0x71 -> message box 0x2c (0x258bf0 type 3)."""
    return vce.put_u8(0x0D) + vce.put_u8(5) + vce.put_bytes(clip_sjis(text, 0x190).encode("utf-8"))


def names_reply(payload):
    """cmd 0x2a reply for any sub: echo sub + seq, four empty groups (u8 n = 0, u32 array cnt 0)."""
    return bytes(payload[:4]) + b"\x00\x00" * 4


def news_replies(payload):
    out = [names_reply(payload)]
    items = load_news()
    if not items:
        out.append(notify_system("[News] No news."))
        return out
    out.append(notify_system(f"[News] {len(items)} item(s)"))
    for i in items[:int(os.environ.get("NOBU_NEWS_MAX", "5"))]:
        out.append(notify_system(f"[{i.get('date', '')}] {i.get('title', '')}"))
        if i.get("body"):
            out.append(notify_system(str(i["body"])))
    if os.environ.get("NOBU_NEWS_POPUP", "1") == "1":
        top = items[0]
        out.append(notify_message(f"{top.get('title', '')}\n{top.get('body', '')}".strip()))
    return out


# ---------------------------------------------------------------------------- support / reports
KINDS = {2: "stuck / GM call (kind 2)", 3: "bug report", 4: "harassment / GM (kind 4)",
         5: "request", 6: "other"}


def reports_path():
    return os.environ.get("NOBU_REPORTS") or os.path.join(data_dir(), "reports.jsonl")


def decode_report(payload):
    r = vce.Reader(payload, 2)
    kind = r.u32()
    text = r.bytes(0x400).decode("utf-8", "replace") if len(r.rest()) else ""
    extra = r.bytes(0x5000).decode("utf-8", "replace") if len(r.rest()) else ""
    return dict(kind=kind, kind_name=KINDS.get(kind, f"kind {kind}"), text=text, extra=extra)


def shout(line):
    """Print a prominent log line even on a console that cannot show Japanese."""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def store_report(rec, path=None):
    path = path or reports_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")     # one append per record (JSON lines)
        fh.flush()
        os.fsync(fh.fileno())                   # a player's report survives a crash right after


def read_reports(path=None):
    """[(line number, record), ...] of reports.jsonl, 1-based. The file is append-only, so a line
    number names a record for good; a line that does not parse is skipped (its number stays used)."""
    path = path or reports_path()
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return out
    with fh:
        for n, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append((n, rec))
    return out


# Staff answers to reports (the OpenLobby panel's help desk, nobu/adminport.py) live beside
# reports.jsonl, never in it: {"closed": {id: {"at", "by"}}, "answers": {id: [{"text", "at", "by",
# "delivered"}]}, "pending": [{"id", "uid", "text", "at"}]}. An answer to a player who is not online
# waits in "pending" until a character of that account enters the world (pending_answers).
def support_state_path():
    return os.environ.get("NOBU_REPORT_STATE") or os.path.join(data_dir(), "reports-state.json")


def load_support_state(path=None):
    """The state document; empty when the file does not exist. A file that does not parse raises
    (ValueError): writing over it would lose every answer and closure in it."""
    path = path or support_state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except FileNotFoundError:
        st = {}
    if not isinstance(st, dict):
        raise ValueError(f"{path} is not a JSON object")
    for key, empty in (("closed", dict), ("answers", dict), ("pending", list)):
        if not isinstance(st.get(key), empty):
            st[key] = empty()
    return st


def save_support_state(st, path=None):
    persist.atomic_write_json(path or support_state_path(), st, ensure_ascii=False, indent=1)


ANSWER_PREFIX = "[Support] "
ANSWER_MAX = 0x190 - len(ANSWER_PREFIX)     # SJIS bytes: the system line holds 0x190


def answer_frames(text):
    """A staff answer as the player sees it: a system line in the chat window and a message box."""
    return [notify_system(ANSWER_PREFIX + text), notify_message(text)]


def pending_answers(ctx):
    """Frames for the answers waiting for this session's account (uid), taken out of the pending
    list. Called when a character enters the world; never raises (a bad state file is logged)."""
    uid = ctx.get("uid") or (ctx.get("char") or {}).get("uid")
    if not uid:
        return []
    try:
        st = load_support_state()
        mine = [p for p in st["pending"] if p.get("uid") == uid]
        if not mine:
            return []
        st["pending"] = [p for p in st["pending"] if p.get("uid") != uid]
        for p in mine:
            for a in st["answers"].get(p.get("id"), []):
                if not a.get("delivered") and a.get("at") == p.get("at") and a.get("text") == p.get("text"):
                    a["delivered"] = True
                    break
        save_support_state(st)
    except (OSError, ValueError) as e:
        persist.warn_once(("support-state", repr(e)), f"support answers not delivered: {e!r}")
        return []
    out = []
    for p in mine:
        out += answer_frames(str(p.get("text", "")))
    return out


def report_reply(res=0, ticket=0, c=0, text=""):
    """09 01: u32 res (low 16 >= 0 = accepted; high 16 == 1 = GM call queued), u32 ticket, u32 c,
    bytes text (shown as a chat line when non-empty)."""
    return (vce.put_u8(9) + vce.put_u8(1) + vce.put_u32(res) + vce.put_u32(ticket) + vce.put_u32(c)
            + vce.put_bytes(clip_sjis(text, 0x1ff).encode("utf-8")))


def support_answer(payload, ctx):
    sub = payload[1]
    c = ctx.get("char") or {}
    who = dict(uid=ctx.get("uid", c.get("uid", 0)), cid=ctx.get("cid", c.get("cid", 0)),
               name=f"{c.get('family', '')} {c.get('given', '')}".strip())
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if sub == 1:
        d = decode_report(payload)
        rec = dict(t=now, **who, **d)
        store_report(rec)
        ctx.setdefault("reports", []).append(rec)
        shout(f"*** SUPPORT REPORT *** {d['kind_name']} from {who['name']!r} uid={who['uid']:#x} "
              f"cid={who['cid']}: {d['text']!r} extra={d['extra']!r}")
        text = os.environ.get("NOBU_REPORT_ACK", "Report received. Thank you.")
        return [report_reply(0, 0, 0, text)]
    if sub in (2, 3) and len(payload) >= 6:
        # 2: GM chat line (u32, u32, str); 3: attachment chunk (u32, str). Log + store, ack u32 0.
        r = vce.Reader(payload, 2)
        ids = [r.u32() for _ in range(2 if sub == 2 else 1)]
        text = r.bytes(0x5000).decode("utf-8", "replace") if len(r.rest()) else ""
        store_report(dict(t=now, **who, sub=sub, ids=ids, text=text))
        shout(f"*** SUPPORT sub {sub} *** {who['name']!r}: {text!r}")
        return [vce.put_u8(9) + vce.put_u8(sub) + vce.put_u32(0)]
    if sub in (4, 6) and len(payload) >= 6:
        # 4 / 6 (u32): cancel / close GM support (HYPOTHESIS); reply u32 0 -> state 0xa / 8.
        return [vce.put_u8(9) + vce.put_u8(sub) + vce.put_u32(0)]
    return []


# ---------------------------------------------------------------------------- inventory (cmd 0x24)
def inventory_reply(payload):
    """Minimal replies for an empty backpack; None for subs not handled here."""
    sub, head = payload[1], bytes(payload[:4])
    r = vce.Reader(payload, 4)
    rest = r.rest()
    if sub == 3:                                     # 3: u8, u8, u32 -> reply header only
        return head
    if sub == 4:                                     # EQUIP_ITEM u8, u8 -> echo u8, u8 (HYPOTHESIS)
        return head + (rest[:2] + b"\x00\x00")[:2]
    if sub == 5:                                     # UNEQUIP_ITEM u8 -> echo u8
        return head + (rest[:1] + b"\x00")[:1]
    if sub == 6:                                     # DROP_ITEM u8, u8, u32 -> u8, u8
        return head + (rest[:2] + b"\x00\x00")[:2]
    if sub == 7:                                     # SORT_ITEM -> bytes (max 0xc), empty
        return head + b"\x00"
    return None


# ---------------------------------------------------------------------------- skills (cmd 0x26)
# Reply parser 0x289c80 (u8 sub, u16 seq, table 0x428940, sub < 0xd); main handler 0x28aef0 (table
# 0x428b00) logs "CODE_CHONOBU_PC: SCMD_..." (0x4289b0..) and posts UI events. Request builders
# 0x1cab00..0x1cb4f0. Subs 1 / 2 (standby / book list) stay in chars.WORLDLOAD_EMPTY.
SKILL_SUBS = {3: "SCMD_SKILLDATA_GET_INFO", 4: "SCMD_SKILLBOOK_GET_INFO", 5: "SCMD_SKILL_EQUIP",
              6: "SCMD_SKILL_UNEQUIP", 7: "SCMD_SKILL_COMMAND", 8: "skill sub 8", 9: "skill sub 9",
              0xA: "skill swap setup", 0xB: "skill sub 0xb", 0xC: "skill config (6 x u8)"}


def _save(ctx, store, key, value):
    c = ctx.get("char")
    if c is None:
        return
    c.setdefault("skills", {})[key] = value
    st = store() if callable(store) else store
    if st is not None:
        st.save()


def skill_reply(payload, ctx, store=None):
    """Minimal success replies (layouts KNOWN from the 0x289c80 cases, meanings HYPOTHESIS)."""
    sub, head = payload[1], bytes(payload[:4])
    r = vce.Reader(payload, 4)
    rest = bytes(r.rest())
    if sub in (3, 4):
        # req u32 id -> reply i8 res [+ info struct 0x1eef90 / 0x1ef250 only if res >= 0].
        # We know no skills: res -1 (the UI still gets its event 0x3a / 0x3b).
        return head + b"\xff"
    if sub == 5:                        # EQUIP req u8 slot, u32 skill -> reply u8, u32 (echo)
        rest = (rest + b"\x00" * 5)[:5]
        bar = (ctx.get("char") or {}).get("skills", {}).get("bar", {})
        bar = dict(bar, **{str(rest[0]): int.from_bytes(rest[1:5], "big")})
        _save(ctx, store, "bar", bar)
        return head + rest
    if sub == 6:                        # UNEQUIP req u8 slot -> reply u8 (echo)
        rest = (rest + b"\x00")[:1]
        bar = dict((ctx.get("char") or {}).get("skills", {}).get("bar", {}))
        bar.pop(str(rest[0]), None)
        _save(ctx, store, "bar", bar)
        return head + rest
    if sub == 7:                        # COMMAND req u8 -> reply header only
        return head
    if sub == 8:                        # req u32 -> reply u16
        return head + b"\x00\x00"
    if sub == 9:                        # req u8 -> reply u32 res, u8 (echo)
        return head + vce.put_u32(0) + (rest + b"\x00")[:1]
    if sub == 0xA:                      # swap setup req u8, u8, u16 -> reply u32 res, u8, u8, u16
        rest = (rest + b"\x00" * 4)[:4]
        _save(ctx, store, "swap", list(rest[:2]) + [int.from_bytes(rest[2:4], "big")])
        return head + vce.put_u32(0) + rest
    if sub == 0xB:                      # req u8 -> reply u8 (echo)
        return head + (rest + b"\x00")[:1]
    if sub == 0xC:                      # config: req 6 x u8 (0x1f5580) -> reply the same 6 x u8
        rest = (rest + b"\x00" * 6)[:6]
        _save(ctx, store, "config", list(rest))
        return head + rest
    return None


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x2A and len(payload) >= 4:
        if sub == 5:
            return news_replies(payload)
        return [names_reply(payload)]
    if cmd == 9:
        return support_answer(payload, ctx)
    if cmd == 0x26 and len(payload) >= 4 and 3 <= sub <= 0xC:
        return [skill_reply(payload, ctx, store)]
    if cmd == 0x24 and len(payload) >= 4:
        rep = inventory_reply(payload)
        return None if rep is None else [rep]
    return None


def describe(payload, direction="C>S"):
    if len(payload) < 2:
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if cmd == 0x2A and sub == 5 and len(payload) >= 4 and direction == "C>S":
            r = vce.Reader(payload, 2)
            seq = r.u16()
            ver = r.u32() if len(r.rest()) >= 4 else None
            return f"NEWS request seq={seq} version={ver}"
        if cmd == 9 and direction == "C>S" and sub == 1:
            d = decode_report(payload)
            return f"SUPPORT report {d['kind_name']} text={d['text']!r} extra={d['extra']!r}"
        if cmd == 9:
            return f"SUPPORT sub {sub} {payload[2:].hex()}"
        if cmd == 0x26 and sub in SKILL_SUBS and len(payload) >= 4:
            r = vce.Reader(payload, 2)
            return f"SKILL {SKILL_SUBS[sub]} seq={r.u16()} {r.rest().hex()}"
        if cmd == 0x0D and sub in (5, 6) and direction == "S>C":
            r = vce.Reader(payload, 2)
            return f"NOTIFY_CHAT sub {sub} {r.bytes().decode('utf-8', 'replace')!r}"
    except vce.WireError:
        return None
    return None
