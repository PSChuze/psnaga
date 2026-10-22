"""This server in an OpenLobby admin panel (the panel's admin protocol, nobu/olport.py).

Off unless NOBU_ADMIN_PORT is set. It runs in the world process, which holds the players' sessions
and the boards' snapshot. The panel is then given this address and the token:

  NOBU_ADMIN_PORT     the HTTP port, e.g. 19111
  NOBU_ADMIN_BIND     default 127.0.0.1; compose.yml binds 0.0.0.0 inside the container, which only the
                      openlobby network reaches (a token is then required)
  NOBU_ADMIN_TOKEN    the token the panel sends (POL_ADMIN_GAME_NOBU_TOKEN on the panel's side)
  NOBU_ADMIN_CONTAINERS  the containers its server check covers (comma list), default this
                      compose file's (project "nobu")

What the panel gets: who is online and where, the clans (as its rooms) and the war, all from the
boards' snapshot (nobu/live.py); the war weeks on its Calendar (war_calendar, from war_week's
schedule and clock, so NOBU_WAR_DECLARE / _ROUNDS / _RESULTS / _CLOCK_START / _CLOCK_SCALE show as
they run; nothing with NOBU_WAR_SCHEDULE=0 or a stopped clock); and two actions:
  announce  a system line (0D 06, misc.notify_system, the line every NPC and battle notice uses)
            in the chat window of everyone with a character loaded. The panel's maintenance
            warning uses it.
  kick      closes the player's connection, the same close a docker stop gives every session
            (world.shutdown): the leave hooks run and the character is saved. An optional reason
            is sent first as a system line. Names are character names (family and given name, any
            case, spaces ignored); an account's Kick sends its handles, tried as names.
Each runs on the world's event loop (the sessions are the loop's alone).

The in-game Support reports (misc.support_answer, $NOBU_DATA/reports.jsonl) are the panel's help
desk: one request per report, named r<line> (the file is append-only and never rewritten), with
any later GM chat line from the same character added to it. An answer is shown to the player as a
system line and a message box (misc.answer_frames); when no character of the account is online it
waits and is sent when one next enters the world (misc.pending_answers, from
world.world_ready_pushes). Answers, the waiting ones and closures are kept beside the reports in
reports-state.json (NOBU_REPORT_STATE).

Two tool pages, both without script (olpage.py, OpenLobby's look):
  news     edit the Function menu's News items (news.json, written atomically)
  backups  the backups of chars.json and auth.json (persist.list_backups), "Back up now" (a manual
           backup after any pending character save) and "Validate" (admin.validate, read-only).
           Restoring stays a shell job (python -m nobu.admin restore) with the world stopped.
Every change made from the panel is logged with the panel user's name (X-Devtool-Actor) to stderr
and events.jsonl.

The account window's player lookup (@port.player): the panel sends the citizen IDs the account's
members have linked on the website (who["links"]["nobu"]; the key is the
citizen ID, auth.link_verify). Each is looked up in auth.json (read only, never written here) for
its uid, and that uid's characters in the character store, with whether each is in the world now,
its level, rank, clan and place. Handles are never matched: the handle the client derives from a
uid (auth.handle_name) is not a panel handle.
"""
import asyncio
import datetime as dt
import json
import os
import sys
import time

from . import admin, auth, chars, live, misc, olpage, olport, persist, social, war_week, zones

PORT = int(os.environ.get("NOBU_ADMIN_PORT", "0") or 0)
BIND = os.environ.get("NOBU_ADMIN_BIND", "127.0.0.1")
TOKEN = os.environ.get("NOBU_ADMIN_TOKEN", "")
CONTAINERS = [c.strip() for c in os.environ.get(
    "NOBU_ADMIN_CONTAINERS", "nobu-world-1,nobu-connsv-1,nobu-auth-1,nobu-ssl3-1").split(",")
    if c.strip()]
WAIT = 10.0
CAL_WEEKS = 400                 # most war weeks one calendar answer lists (a fast clock has many)
DECLARE_LEN = 30 * 60           # the declaration's entry: half an hour of war clock


def _on_loop(loop, fn):
    """Run fn() on the world's event loop and wait for its answer."""
    async def run():
        return fn()
    return asyncio.run_coroutine_threadsafe(run(), loop).result(WAIT)


def _key(name):
    return "".join(str(name).split()).casefold()


def _name(ctx):
    return live._player(ctx["char"])["name"]


def _where(ctx):
    zone = ctx.get("zone")
    return zones.place_name(zone) if zone else ""


def war_summary(w):
    """The snapshot's war view as one line for the panel."""
    phase = w.get("phase") or "static"
    if phase == "static":
        return ""
    pair = "%s attacks %s" % (w["attacker"], w["defender"]) if w.get("attacker") else ""
    if w.get("field"):
        pair += " at %s" % w["field"]
    if phase == "round" and w.get("round") is not None:
        line = "War round %d of %d" % (w["round"] + 1, w["rounds"]) if w.get("rounds") else "War round"
        if w.get("score"):
            line += ", %d to %d" % tuple(w["score"])
    else:
        line = "War: %s" % phase
    if pair:
        line += " (%s)" % pair
    if w.get("next_in") is not None:
        m = int(w["next_in"]) // 60
        line += ", next change in %dh %02dm" % (m // 60, m % 60)
    return line


def _war_day(off):
    """Seconds from Monday 00:00 -> "Thu 21:00"."""
    d = dt.datetime(2025, 1, 6) + dt.timedelta(seconds=off)       # a Monday
    return d.strftime("%a %H:%M")


def war_calendar(lo, hi):
    """The war weeks touching real time [lo, hi) as calendar entries: one per week from the
    build-up (Monday 00:00) to the results, its battle rounds as "times", and the declaration as
    a short entry of its own. War-clock times (war_week.war_now) are turned back into real ones,
    so a scaled or shifted clock shows when things happen for the players."""
    rate = war_week.scale()
    if not war_week.schedule_enabled() or rate <= 0:
        return []
    w0, t0 = war_week.clock_start(), war_week._T0

    def real(w):
        return int(round(t0 + (w - w0) / rate))

    declare, rounds, results = war_week.schedule()
    about = "Build-up from Mon 00:00, war declared %s, %s, results %s (Japan time)." % (
        _war_day(declare), _plural(len(rounds), "battle round"), _war_day(results))
    if rate != 1:
        about += " War clock x%g." % rate
    out = []
    week = war_week.week_start(war_week.war_now(lo)) - war_week.WEEK
    end = war_week.war_now(hi)
    for _ in range(CAL_WEEKS):
        if week >= end:
            break
        start, stop = real(week), real(week + results)
        if stop > lo and start < hi:
            d = dt.datetime.fromtimestamp(week, war_week.JST)
            out.append({"id": "war-%s" % d.strftime("%Y%m%d"),
                        "title": "Castle war, week of %s %d" % (d.strftime("%b"), d.day),
                        "start": start, "end": stop, "kind": "war", "about": about,
                        "times": [[real(week + s), real(week + e)] for s, e in rounds]})
            ds, de = real(week + declare), real(week + declare + DECLARE_LEN)
            if de > lo and ds < hi:
                out.append({"id": "war-%s-declare" % d.strftime("%Y%m%d"), "title": "War declared",
                            "start": ds, "end": max(de, ds + 1), "kind": "war",
                            "about": "The attacker declares war; the battle rounds follow."})
        week += war_week.WEEK
    return out


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def announce_now(text):
    """Push one system line to everyone online. -> how many got it. Loop thread only."""
    return social.broadcast(misc.notify_system(text))


def kick_now(names, reason=""):
    """Close the session of every online character named in `names`. -> the names kicked.
    Loop thread only."""
    want = {_key(n) for n in names if _key(n)}
    out = []
    for ctx in social.online():
        if _key(_name(ctx)) not in want or not ctx.get("close"):
            continue
        if reason and ctx.get("push"):
            ctx["push"](misc.notify_system(reason))
        ctx["close"]()                  # the session's read sees EOF -> its normal leave path
        out.append(_name(ctx))
    return out


def audit(actor, what, **kw):
    """One line per panel change: stderr (docker logs) and events.jsonl in NOBU_LOG_DIR."""
    print("adminport: %s %s" % (actor or "?", what), file=sys.stderr, flush=True)
    log_dir = os.environ.get("NOBU_LOG_DIR")
    if log_dir:
        rec = dict(t=dt.datetime.now(dt.timezone.utc).isoformat(), ev="admin", actor=actor or "?",
                   what=what, **kw)
        try:
            with open(os.path.join(log_dir, "events.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------- player lookup
def _auth_accounts():
    """auth.json's accounts table, read as it is on disk (the auth server owns the file)."""
    try:
        with open(auth.default_db(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    return data["accounts"] if auth.valid_accounts(data) else {}


def _link_key(key):
    return auth._link_id({"account": key})


def player_lookup(who, store):
    """The characters of the citizen IDs in who["links"]["nobu"], or None when none of them is
    an account here. Loop thread only (the character store and the sessions are the loop's)."""
    keys = []
    for link in (who.get("links") or {}).get("nobu", []):
        k = _link_key(link.get("key", ""))
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return None
    accounts = _auth_accounts()
    st = store() if callable(store) else store
    here = {(c.get("uid"), c["char"].get("cid")): c for c in social.online()}
    out, known = [], []
    for k in keys:
        acct = accounts.get(k)
        if not isinstance(acct, dict) or "uid" not in acct:
            continue
        known.append(k)
        uid = acct["uid"]
        for c in sorted(st.list(uid), key=lambda c: c.get("slot", 0)):
            p = live._player(c)
            ctx = here.get((uid, c.get("cid")))
            pos = c.get("pos") or []
            if ctx is not None:
                place = ("Where", _where(ctx))
            elif pos:
                place = ("Last seen", zones.place_name(int(pos[0])))
            else:
                place = ("Last seen", "")
            facts = [("Citizen ID", k), ("Level", str(p["level"])), ("Rank", p["rank"]),
                     ("Clan", p["clan"]), place, ("Created", str(c.get("created") or "")[:10])]
            out.append({"name": p["name"], "detail": ", ".join(x for x in ("Level %d" % p["level"], p["rank"], p["clan"]) if x),
                        "online": ctx is not None,
                        "facts": [{"label": a, "value": b} for a, b in facts if b]})
    if not known:
        return None
    doc = {"characters": out}
    if not out:
        doc["note"] = "Citizen ID %s has no characters yet." % ", ".join(known)
    return doc


def player_search(q, limit, store):
    """The panel's player picker: characters whose name holds q (any case, spaces ignored), online ones
    first, then by name. "id" is the character name, what the kick action takes. Loop thread only."""
    want = _key(q)
    st = store() if callable(store) else store
    online = {_key(_name(c)) for c in social.online()}
    seen, hits = set(), []
    for chars_of in ((st.data.get("chars") or {}).values() if st is not None else ()):
        for c in chars_of:
            p = live._player(c)
            k = _key(p["name"])
            if k in seen or (want and want not in k):
                continue
            seen.add(k)
            hits.append({"id": p["name"], "label": p["name"],
                         "detail": ", ".join(x for x in ("Level %d" % p["level"], p["rank"], p["clan"]) if x),
                         "online": k in online})
    hits.sort(key=lambda h: (not h["online"], _key(h["label"])))
    return hits[:limit]


# ---------------------------------------------------------------------------- help desk
KIND_LABELS = {2: "Stuck / GM call", 3: "Bug report", 4: "Harassment", 5: "Request", 6: "Other"}


def _unix(iso):
    try:
        return int(dt.datetime.fromisoformat(str(iso)).timestamp())
    except ValueError:
        return 0


def report_threads():
    """{id: thread} from reports.jsonl and the answers beside it. A report (sub 1) is a thread,
    named r<line>; a later GM chat line (sub 2) from the same character joins its latest report.
    Loop thread only (the world appends to the file there)."""
    st = misc.load_support_state()
    threads, latest = {}, {}
    for n, rec in misc.read_reports():
        who = (rec.get("uid"), rec.get("cid"))
        at = _unix(rec.get("t"))
        if "kind" in rec:
            tid = "r%d" % n
            label = KIND_LABELS.get(rec.get("kind"), "Report (kind %s)" % rec.get("kind"))
            text = str(rec.get("text") or "")
            first = " ".join(text.split())[:60]
            threads[tid] = {"id": tid, "subject": label + (": " + first if first else ""),
                            "kind": label, "uid": rec.get("uid") or 0, "cid": rec.get("cid") or 0,
                            "name": rec.get("name") or "?", "at": at,
                            "messages": [{"from": "player", "subject": label, "text": text, "at": at}]}
            latest[who] = tid
        elif rec.get("sub") == 2 and who in latest and rec.get("text"):
            threads[latest[who]]["messages"].append({"from": "player", "text": str(rec["text"]), "at": at})
    for tid, answers in st["answers"].items():
        if tid in threads:
            threads[tid]["messages"] += [{"from": "staff", "text": str(a.get("text", "")),
                                          "at": int(a.get("at") or 0)} for a in answers]
    for t in threads.values():
        t["messages"].sort(key=lambda m: m["at"])
        t["closed"] = t["id"] in st["closed"]
    return threads


def _online_ctx(uid):
    return next((c for c in social.online() if uid and c.get("uid") == uid and c.get("push")), None)


def help_list(open_only):
    rows = []
    for t in report_threads().values():
        if open_only and t["closed"]:
            continue
        rows.append({"id": t["id"], "subject": t["subject"], "who": t["name"], "at": t["at"],
                     "closed": t["closed"], "waiting": not t["closed"] and t["messages"][-1]["from"] == "player",
                     "count": len(t["messages"]), "category": t["kind"]})
    rows.sort(key=lambda r: (-r["at"], -int(r["id"][1:])))             # newest first
    return rows


def help_thread(tid):
    t = report_threads().get(tid)
    if t is None:
        return None
    return {"id": tid, "subject": t["subject"], "closed": t["closed"],
            "who": {"name": t["name"], "online": _online_ctx(t["uid"]) is not None},
            "messages": t["messages"]}


def help_answer(tid, text, close, actor):
    """Send a staff answer to the player who filed `tid`: at once when a character of that account
    is online, else kept and sent when one next enters the world. -> (ok, message)."""
    t = report_threads().get(tid)
    if t is None:
        return False, "No such report."
    if not t["uid"]:
        return False, "This report came from a session without an account; it cannot be answered."
    st = misc.load_support_state()
    ctx = _online_ctx(t["uid"])
    at = int(time.time())
    st["answers"].setdefault(tid, []).append({"text": text, "at": at, "by": actor or "?",
                                              "delivered": ctx is not None})
    if ctx is None:
        st["pending"].append({"id": tid, "uid": t["uid"], "text": text, "at": at})
    if close:
        st["closed"][tid] = {"at": at, "by": actor or "?"}
    misc.save_support_state(st)                 # kept before it is sent
    if ctx is not None:
        for f in misc.answer_frames(text):
            ctx["push"](f)
        msg = "Sent to %s." % t["name"]
    else:
        msg = "%s is not online; the answer is kept and shown at their next login." % t["name"]
    return True, msg + (" The report is closed." if close else "")


def help_close(tid, actor):
    if tid not in report_threads():
        return False, "No such report."
    st = misc.load_support_state()
    if tid not in st["closed"]:
        st["closed"][tid] = {"at": int(time.time()), "by": actor or "?"}
        misc.save_support_state(st)
    return True, "Closed."


# ---------------------------------------------------------------------------- tool pages
EXTRA_CSS = olpage.Raw("<style>textarea{font:inherit;font-size:14px;color:var(--text);background:var(--panel2);"
                       "border:1px solid var(--line);border-radius:8px;padding:7px 10px;width:100%;"
                       "min-height:84px}form.stack{display:grid;gap:10px;margin:0}"
                       "form.stack label{width:100%}</style>")
NEWS_TITLE_MAX = 120
NEWS_BODY_MAX = 0x190          # one system line holds 0x190 SJIS bytes (misc.notify_system)


def _html(title, *parts, sub="", note=None):
    return 200, olpage.CTYPE, olpage.page(title, EXTRA_CSS, *parts, sub=sub, note=note).encode()


def news_raw():
    """(items as load_news orders them, None) or ([], why) when news.json does not parse: the
    editor then refuses to write over it."""
    path = misc.news_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except (OSError, ValueError) as e:
            return [], "%s does not parse (%s); fix or remove it first." % (os.path.basename(path), e)
    return misc.load_news(), None


def _sjis_len(text):
    return len(text.encode("cp932", "replace"))


def news_change(form, actor):
    """Apply one posted news form. -> (ok, message). Loop thread only."""
    items, bad = news_raw()
    if bad:
        return False, bad
    op = form.get("op", "")
    date, title, body = form.get("date", ""), " ".join(form.get("title", "").split()), form.get("body", "")
    body = "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").split("\n")).strip()
    if op in ("add", "save"):
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            return False, "The date must be YYYY-MM-DD."
        if not title:
            return False, "A title is needed."
        if _sjis_len(title) > NEWS_TITLE_MAX or _sjis_len(body) > NEWS_BODY_MAX:
            return False, "Too long: a title holds %d bytes, a body %d (Shift-JIS)." % (NEWS_TITLE_MAX, NEWS_BODY_MAX)
    if op == "add":
        items.append({"date": date, "title": title, "body": body})
        what = "added news %r" % title
    elif op in ("save", "delete"):
        try:
            i = int(form.get("i", ""))
        except ValueError:
            i = -1
        if not 0 <= i < len(items) or str(items[i].get("title", "")) != form.get("was", ""):
            return False, "The news changed since this page was loaded; look again."
        if op == "save":
            items[i] = dict(items[i], date=date, title=title, body=body)
            what = "edited news %r" % title
        else:
            what = "deleted news %r" % items.pop(i).get("title", "")
    else:
        return False, "Nothing to do."
    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    persist.atomic_write_json(misc.news_path(), items, ensure_ascii=False, indent=2)
    audit(actor, what, file="news.json")
    return True, {"add": "Added.", "save": "Saved.", "delete": "Deleted."}[op]


def news_page(items, bad, note):
    esc = olpage.esc

    def editor(i, item):
        hid = ('<input type=hidden name=op value=save><input type=hidden name=i value="%d">'
               '<input type=hidden name=was value="%s">' % (i, esc(item.get("title", ""))))
        return olpage.Raw(
            '<form method=post class=stack>%s<label>Date<input name=date value="%s"></label>'
            '<label>Title<input name=title value="%s"></label><label>Text<textarea name=body>%s'
            '</textarea></label><div><button type=submit class=act>Save</button></div></form>'
            % (hid, esc(item.get("date", "")), esc(item.get("title", "")), esc(item.get("body", ""))))

    parts = []
    for i, item in enumerate(items):
        parts.append(olpage.panel("%s  %s" % (item.get("date", ""), item.get("title", "")), editor(i, item),
                                  olpage.form([], "Delete", hidden={"op": "delete", "i": i,
                                                                    "was": item.get("title", "")},
                                              danger=True, confirm="Removes this item from the News list")))
    add = olpage.Raw(
        '<form method=post class=stack><input type=hidden name=op value=add>'
        '<label>Date<input name=date value="%s"></label><label>Title<input name=title value=""></label>'
        '<label>Text<textarea name=body></textarea></label><div><button type=submit class=act>Add</button>'
        '</div></form>' % esc(dt.date.today().isoformat()))
    shown = int(os.environ.get("NOBU_NEWS_MAX", "5"))
    if bad:
        body = [olpage.panel("", olpage.Raw("<p class=muted>%s</p>" % esc(bad)))]
    else:
        body = [olpage.panel("New item", add)] + (parts or [olpage.panel("", olpage.table([], [], empty="No news yet."))])
    return _html("News", *body,
                 sub="What Function menu, News shows: the newest %d items as system lines, the newest "
                     "also in a message box. Newest first by date." % shown, note=note)


BACKUP_FILES = ("chars", "auth")


def backup_targets(store):
    st = store() if callable(store) else store
    return {"chars": getattr(st, "path", None) or chars.default_path(), "auth": auth.default_db()}


def backup_rows(store):
    rows = []
    for label, path in backup_targets(store).items():
        for bpath, stamp, tag in persist.list_backups(path):
            size, state = 0, "ok"
            try:
                size = os.path.getsize(bpath)
                with open(bpath, encoding="utf-8") as fh:
                    json.load(fh)
            except (OSError, ValueError):
                state = "UNREADABLE"
            when = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").strftime("%Y-%m-%d %H:%M:%S UTC")
            rows.append([label, olpage.Raw('<span class=mono>%s</span>' % olpage.esc(os.path.basename(bpath))),
                         when, tag, "%d bytes" % size, state])
    return rows


def backup_now(store, actor):
    """Write a pending character save, then a "manual" backup of chars.json and auth.json.
    -> (ok, message). Loop thread only."""
    st = store() if callable(store) else store
    if st is not None and getattr(st, "path", None):
        if st.dirty or not os.path.exists(st.path):
            st.save()
    made, problems = [], []
    for label, path in backup_targets(store).items():
        if not os.path.exists(path):
            problems.append("%s: no file yet" % label)
            continue
        try:
            dst = persist.backup_file(path, tag="manual")
        except ValueError:
            problems.append("%s does not parse; not backed up" % label)
            continue
        if dst:
            made.append(os.path.basename(dst))
        else:
            problems.append("backups are off (NOBU_BACKUP_KEEP=0)")
    if made:
        audit(actor, "backed up " + ", ".join(made))
    text = ("Backed up: " + ", ".join(made) + "." if made else "Nothing backed up.") + \
        ("" if not problems else " " + "; ".join(problems) + ".")
    return bool(made) and not any("parse" in p for p in problems), text


def backups_page(store, form, actor):
    note, report = None, None
    if form.get("op") == "backup":
        note = backup_now(store, actor)
    elif form.get("op") == "validate":
        lines = []
        n = admin.validate(out=lines.append)
        note = (n == 0, "No problems found." if n == 0 else "%s found." % _plural(n, "error"))
        report = olpage.Raw('<pre class=mono style="white-space:pre-wrap;margin:0">%s</pre>'
                            % olpage.esc("\n".join(lines)))
    actions = olpage.Raw(
        '<div class="row" style="display:flex;gap:10px;flex-wrap:wrap">%s%s</div>'
        % (olpage.form([], "Back up now", hidden={"op": "backup"}, inline=True),
           olpage.form([], "Validate", hidden={"op": "validate"}, inline=True)))
    keep = persist.backup_keep()
    parts = [olpage.panel("", actions, olpage.Raw(
        '<p class=muted style="margin:10px 0 0">Back up now writes any pending character save first, '
        'then copies chars.json and auth.json. Validate reads every data file and changes nothing. '
        'Restoring is done from a shell (python -m nobu.admin restore), with the world stopped.</p>'))]
    if report is not None:
        parts.append(olpage.panel("Validation", report))
    parts.append(olpage.panel("Backups", olpage.table(["File", "Backup", "Taken", "Tag", "Size", "Parses"],
                                                      backup_rows(store), empty="No backups yet.")))
    return _html("Backups", *parts, sub="Newest first. %s kept per file (NOBU_BACKUP_KEEP); %s." % (
        keep, persist.backup_dir(backup_targets(store)["chars"])), note=note)


def build(loop, store=None):
    """The AdminPort for the world running on `loop`; `store` is world.store. Not started."""
    port = olport.AdminPort(
        "nobu", "Nobunaga's Ambition Online", short="Nobunaga", platform="PS2 HDD",
        about="Nobunaga no Yabou Online: the world server, its clans and the weekly war.",
        token=TOKEN, bind=BIND, port=PORT,
        services=[{"name": "Nobunaga's Ambition Online", "containers": CONTAINERS,
                   "ports": {os.environ.get("NOBU_WORLD_PORT", "9110"): "world", "9070": "connect",
                             "9443": "login"},
                   "warning": "Everyone in Nobunaga's Ambition Online is disconnected."}])

    def snap():
        doc = live.snapshot(store)
        doc["_where"] = {_name(c): _where(c) for c in social.online()}
        return doc

    @port.status
    def status():
        doc = _on_loop(loop, snap)
        where, w = doc.pop("_where"), doc["war"]
        pair = (w.get("attacker"), w.get("defender"))
        return {
            "up": True, "online": doc["online"],
            "players": [{"name": p["name"], "where": where.get(p["name"], ""), "clan": p["clan"],
                         "level": p["level"], "rank": p["rank"]} for p in doc["players"]],
            "rooms": [{"name": c["name"], "players": c["online"],
                       "state": "attacking" if c["name"] == pair[0] else
                                "defending" if c["name"] == pair[1] else
                                _plural(c["provinces"], "province"),
                       "members": c["members"], "provinces": c["provinces"], "funds": c["funds"]}
                      for c in doc["clans"]],
            "rooms_label": "clans",
            "summary": war_summary(w),
            "facts": [{"label": "characters", "value": doc["totals"]["characters"]},
                      {"label": "war phase", "value": w.get("phase") or "static"},
                      {"label": "clans holding provinces",
                       "value": sum(1 for c in doc["clans"] if c["provinces"])}],
        }

    port.calendar(war_calendar)

    @port.player
    def player(who):
        return _on_loop(loop, lambda: player_lookup(who, store))

    @port.players
    def players(q, limit):
        return _on_loop(loop, lambda: player_search(q, limit, store))

    @port.action("announce", "Announce", kind="announce",
                 fields=[{"id": "text", "label": "Message", "type": "text"}],
                 about="A system line in the chat window of everyone with a character loaded "
                       "(long lines are cut at 400 bytes).")
    def announce(params, actor):
        text = " ".join(str(params.get("text") or "").split())
        if not text:
            return False, "Nothing to announce."
        n = _on_loop(loop, lambda: announce_now(text))
        return True, "Sent to %s." % _plural(n, "player")

    @port.action("kick", "Disconnect a player", kind="kick", danger=True,
                 fields=[{"id": "name", "label": "Character", "type": "player"},
                         {"id": "reason", "label": "Reason (shown first)", "type": "text"}],
                 about="Closes the character's connection; the character is saved as on any "
                       "logout. Several names may be given, comma separated.")
    def kick(params, actor):
        names = [n.strip() for n in str(params.get("name") or params.get("handles") or "").split(",")
                 if n.strip()]
        if not names:
            return False, "No character named."
        reason = " ".join(str(params.get("reason") or "").split())
        gone = _on_loop(loop, lambda: kick_now(names, reason))
        if not gone:
            return False, "Not online."
        print("adminport: %s disconnected %s" % (actor or "?", ", ".join(gone)),
              file=sys.stderr, flush=True)
        return True, "Disconnected %s." % ", ".join(gone)

    def answer(tid, text, close, actor):
        text = " ".join(text.split())
        if not text:
            return False, "Nothing to send."
        if len(text.encode("cp932", "replace")) > misc.ANSWER_MAX:
            return False, "Too long: the game shows %d bytes at most (Shift-JIS)." % misc.ANSWER_MAX
        ok, msg = _on_loop(loop, lambda: help_answer(tid, text, close, actor))
        if ok:
            audit(actor, "answered report %s" % tid, report=tid, close=bool(close))
        return ok, msg

    def close(tid, actor):
        ok, msg = _on_loop(loop, lambda: help_close(tid, actor))
        if ok:
            audit(actor, "closed report %s" % tid, report=tid)
        return ok, msg

    port.help_desk(lambda open_only: _on_loop(loop, lambda: help_list(open_only)),
                   lambda tid: _on_loop(loop, lambda: help_thread(tid)), answer, close)

    @port.tool("news", "News", "news", about="The Function menu's News items (news.json).")
    def news(method, sub, query, body, actor):
        note = None
        if method == "POST":
            form = olpage.posted(body)
            note = _on_loop(loop, lambda: news_change(form, actor))
        items, bad = _on_loop(loop, news_raw)
        return news_page(items, bad, note)

    @port.tool("backups", "Backups", "backups",
               about="The rotating backups of chars.json and auth.json; back up now, validate.")
    def backups(method, sub, query, body, actor):
        form = olpage.posted(body) if method == "POST" else {}
        return _on_loop(loop, lambda: backups_page(store, form, actor))

    return port


def start(loop, store=None, log=print):
    """Start the admin port if NOBU_ADMIN_PORT is set. Never raises: a bad setting is logged and
    the world runs on without it."""
    if not PORT:
        return None
    try:
        port = build(loop, store)
        host, n = port.start()
    except (OSError, ValueError) as ex:
        log(f"adminport: not started ({ex})")
        return None
    log(f"adminport: OpenLobby admin protocol on {host}:{n}" + ("" if TOKEN else " (no token)"))
    return port
