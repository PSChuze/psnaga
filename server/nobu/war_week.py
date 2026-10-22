"""The weekly war schedule (phase 2): / build-up, declaration, rounds, results. DESIGN: the client
has no clock of its own for this; everything it shows comes from war.py's messages (2F 04, the
board pages of cmd 0x11).

Week (war clock, Japan time UTC+9; ISO weekday 1 = Monday):
  build     Monday 00:00 .. the declaration      edict: last war, a province to build up
  declared  declaration .. the first round        war list entry flags 0x80, 2F 04 type 1 to everyone
  round     inside a round                        war list flags 0, HUD "Territory Battle #n (h:m)"
  break     between rounds                        HUD "Preparing (starts in h:m)"
  results   after the results time                losses, province transfer, 2F 04 type 1, list cleared
A new week (Monday 00:00) resets rounds and deeds, adds +100 funds per clan and +10 per province (caps
996 / 100). Round result: a side wins with >= NOBU_WAR_WIN_MIN (1000) and >= 2x the other, else a draw
(scaled). Scores come from war_week.add_score (phase 4 battles; chat "war score" with
NOBU_WAR_DEBUG=1). Losses after the war: the 2004 FAQ formulas (maxima 100; funds scaled to 996).

Env:
  NOBU_WAR_SCHEDULE=0     no scheduler: nothing is written, the war list stays as war.py seeds it
  NOBU_WAR_TICK           seconds between ticks (default 30, real time); the task is started by world.main
  NOBU_WAR_CLOCK_SCALE    war-clock speed (default 1; 60 = one war hour per real minute)
  NOBU_WAR_CLOCK_START    war-clock time at server start, "YYYY-MM-DDTHH:MM" Japan time (default: now)
  NOBU_WAR_DECLARE        "weekday-HH:MM" (default "4-21:00", Thursday)
  NOBU_WAR_ROUNDS         "weekday-HH:MM+minutes,..." (default "6-20:00+150,7-14:00+150,7-20:00+150")
  NOBU_WAR_RESULTS        "weekday-HH:MM" (default "7-23:00", Sunday)
  NOBU_WAR_WIN_MIN        round-win score floor (default 1000)
  NOBU_WAR_HUD_PUSH=0     do not refresh an open notice board when the war changes
"""
import asyncio
import datetime as dt
import os
import sys
import time

from . import social, war

JST = dt.timezone(dt.timedelta(hours=9))
WEEK = 7 * 86400
FLAG_DECLARED = 0x80            # war-list flags bit 7 (HYPOTHESIS: the other arrow colour = declared)

_T0 = time.time()
_TASK = None


# ---------------------------------------------------------------------------- clock
def scale():
    try:
        return max(0.0, float(os.environ.get("NOBU_WAR_CLOCK_SCALE", "1")))
    except ValueError:
        return 1.0


def clock_start():
    s = os.environ.get("NOBU_WAR_CLOCK_START")
    if not s:
        return _T0
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=JST).timestamp()
    except ValueError:
        return _T0


def war_now(real=None):
    """War-clock epoch seconds: NOBU_WAR_CLOCK_START + (real - server start) * scale."""
    real = time.time() if real is None else real
    return clock_start() + (real - _T0) * scale()


def schedule_enabled():
    return war.enabled() and os.environ.get("NOBU_WAR_SCHEDULE", "1") != "0"


# ---------------------------------------------------------------------------- schedule
def _slot(text, default):
    """'weekday-HH:MM' -> seconds from Monday 00:00."""
    try:
        day, hm = (text or default).split("-")
        h, m = hm.split(":")
        return (int(day) - 1) * 86400 + int(h) * 3600 + int(m) * 60
    except ValueError:
        return _slot(default, default)


def schedule():
    """(declare, [(start, end)], results) in seconds from Monday 00:00 (Japan time)."""
    declare = _slot(os.environ.get("NOBU_WAR_DECLARE"), "4-21:00")
    rounds = []
    for part in (os.environ.get("NOBU_WAR_ROUNDS") or "6-20:00+150,7-14:00+150,7-20:00+150").split(","):
        try:
            at, mins = part.strip().split("+")
            s = _slot(at, "6-20:00")
            rounds.append((s, s + int(mins) * 60))
        except ValueError:
            continue
    rounds.sort()
    results = _slot(os.environ.get("NOBU_WAR_RESULTS"), "7-23:00")
    if rounds:
        results = max(results, rounds[-1][1])
        declare = min(declare, rounds[0][0])
    return declare, rounds, results


def week_start(t):
    d = dt.datetime.fromtimestamp(t, JST)
    monday = (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


def phase_at(t):
    """Where war-clock time t falls in its week. Times in the result are seconds from the week start."""
    ws = week_start(t)
    off = t - ws
    declare, rounds, results = schedule()
    p = dict(week=ws, now=int(off), phase="build", round=None, round_start=0, round_end=0,
             next_start=declare)
    if off < declare:
        return p
    p["phase"] = "declared"
    p["next_start"] = rounds[0][0] if rounds else results
    for i, (s, e) in enumerate(rounds):
        if off < s:
            if i:
                p["phase"] = "break"
            p["next_start"] = s
            return p
        if off < e:
            p.update(phase="round", round=i, round_start=s, round_end=e, next_start=e)
            return p
    if off < results:
        p.update(phase="break", next_start=results)
        return p
    p.update(phase="results", next_start=WEEK + declare)
    return p


def view(st=None, now=None):
    if not schedule_enabled():
        return dict(phase="static", now=0, next_start=0, round_start=0)
    return phase_at(war_now() if now is None else now)


# ---------------------------------------------------------------------------- scores
def round_result(score, win_min=None):
    win_min = int(os.environ.get("NOBU_WAR_WIN_MIN", "1000")) if win_min is None else win_min
    a, d = score
    if a >= win_min and a >= 2 * d:
        return "a"
    if d >= win_min and d >= 2 * a:
        return "d"
    return "draw"


def add_score(side, n, st=None, now=None):
    """Add to side 0 (attacker) / 1 (defender) of the running round. False outside a round."""
    st = st or war.state()
    p = view(st, now)
    if p["phase"] != "round" or st.week.get("id") != p.get("week"):
        return False
    sc = st.week.setdefault("score", [0, 0])
    sc[side] = max(0, sc[side] + int(n))
    return True


def add_deeds(cid, n, st=None):
    st = st or war.state()
    st.deeds[str(cid)] = st.deeds.get(str(cid), 0) + int(n)


def apply_results(st):
    """Losses on the field's two provinces and the clans' funds; transfer when the defender
    loses with <= 0. Returns the last_war record."""
    att, dfn = st.pair
    rounds = [r for r in st.week.get("rounds", []) if "result" in r]
    total = len(rounds)
    aw = sum(1 for r in rounds if r["result"] == "a")
    dw = sum(1 for r in rounds if r["result"] == "d")
    winner = att if aw > dw else dfn if dw > aw else 0
    rec = dict(area=st.area or 0, attacker=att, defender=dfn, winner=winner, wins=[aw, dw, total],
               transfer=0, week=st.week.get("id"))
    if total:
        pa, pd = st.field_provinces()
        fs = war.FUNDS_FULL / 100.0
        if pa:
            st.power[pa] = max(0, st.power[pa] - round(50 * dw / total / 2))
        if pd:
            st.power[pd] = max(0, st.power[pd] - round(50 * aw / total))
        st.funds[att] = max(0, st.funds[att] - round((50 + 50 * (total - aw) / total) * fs))
        st.funds[dfn] = max(0, st.funds[dfn] - round(100 * (total - dw) / total / 2 * fs))
        if winner == att and pd and st.owners[pd] == dfn and st.power[pd] <= 0:
            st.owners[pd] = att
            st.power[pd] = war.POWER_MAX // 2
            rec["transfer"] = pd
    st.last_war = rec
    st.history = (st.history + [rec])[-20:]
    return rec


def new_week(st, week_id):
    st.week = dict(id=week_id, declared=False, rounds=[], score=[0, 0], done=False)
    st.deeds = {}
    st.wars = []
    for f in range(1, war.FACTIONS):
        st.funds[f] = min(war.FUNDS_FULL, st.funds[f] + 100)
    for p in range(1, war.PROVINCES):
        st.power[p] = min(war.POWER_MAX, st.power[p] + 10)


def names(st):
    att, dfn = st.pair
    place = next((n for _, _, a, n in war.BORDER_FIELDS if a == st.area), f"place {st.area}")
    return war.FACTION_NAMES[att], war.FACTION_NAMES[dfn], place


def advance(st, now):
    """Bring the state up to war-clock time now. Returns (changed, notices)."""
    p = phase_at(now)
    changed, notices = False, []
    if st.week.get("id") != p["week"]:
        new_week(st, p["week"])
        changed = True
    if not st.pair:
        return changed, notices
    wk = st.week
    declare, rounds, results = schedule()
    off = p["now"]
    att, dfn = st.pair
    a, d, place = names(st)
    if off >= declare and not wk.get("declared"):
        st.set_relation(att, dfn, war.HOSTILE)
        st.wars = [(att, dfn, st.area or 0, FLAG_DECLARED)]
        wk["declared"] = True
        notices.append(f"{a} declares war on {d} at {place}.")
        changed = True
    for i, (s, e) in enumerate(rounds):
        done = {r["no"]: r for r in wk.setdefault("rounds", [])}
        if off >= s and i not in done:
            wk["rounds"].append(dict(no=i))
            wk["score"] = [0, 0]
            st.wars = [(att, dfn, st.area or 0, 0)]
            notices.append(f"The war at {place}: round {i + 1} begins.")
            changed = True
            done = {r["no"]: r for r in wk["rounds"]}
        if off >= e and i in done and "result" not in done[i]:
            r = done[i]
            r["score"] = list(wk.get("score", [0, 0]))
            r["result"] = round_result(r["score"])
            wk["score"] = [0, 0]
            who = {"a": a, "d": d}.get(r["result"])
            notices.append(f"Round {i + 1} at {place}: " + (f"{who} wins." if who else "a draw."))
            changed = True
    if off >= results and not wk.get("done"):
        rec = apply_results(st)
        st.wars = []
        wk["done"] = True
        aw, dw, total = rec["wins"]
        if not total:
            notices.append(f"The war at {place} was not fought.")
        elif rec["winner"]:
            notices.append(f"{war.FACTION_NAMES[rec['winner']]} wins the war at {place} ({aw}-{dw}).")
        else:
            notices.append(f"The war at {place} ends undecided ({aw}-{dw}).")
        if rec["transfer"]:
            notices.append(f"{war.PROVINCE_NAMES[rec['transfer']]} passes to {a}.")
        changed = True
    return changed, notices


# ---------------------------------------------------------------------------- pushes
def sessions():
    return [c for c in social.SESSIONS.values() if isinstance(c, dict) and c.get("char") and c.get("push")]


def _push(ctx, payload):
    try:
        ctx["push"](payload)
        return True
    except Exception:                               # a closed session: skip it
        return False


def broadcast(notices, st=None, targets=None, store=None, now=None):
    """2F 04 type 1 per notice (type 3 when there is none) to everyone online; a fresh board to
    anyone whose board is open (and who has no shop / craft / trade event waiting)."""
    st = st or war.state()
    targets = sessions() if targets is None else targets
    sent = 0
    for ctx in targets:
        frames = [war.notify_diplomacy(st, kind=1, text=war.misc.clip_sjis(t, war.TEXT_MAX - 1))
                  for t in notices] or [war.notify_diplomacy(st, kind=3)]
        frames += [war.misc.notify_system(t) for t in notices]
        if (ctx.get("war_event") and not war.other_event_open(ctx)
                and os.environ.get("NOBU_WAR_HUD_PUSH", "1") != "0"):
            frames.append(war.open_board(ctx, store, now))
        for f in frames:
            sent += _push(ctx, f)
    return sent


def tick(now=None, targets=None, store=None):
    """One scheduler step (also the chat "war tick"). Returns the notices."""
    if not schedule_enabled():
        return []
    st = war.state()
    now = war_now() if now is None else now
    changed, notices = advance(st, now)
    if changed:
        war.save(st)
        broadcast(notices, st, targets, store, now)
    return notices


def ensure_scheduler():
    """Start the tick task once, on the running loop (world.main after start_server)."""
    global _TASK
    if not schedule_enabled() or (_TASK is not None and not _TASK.done()):
        return _TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    _TASK = loop.create_task(_run())
    return _TASK


async def _run():
    period = max(1.0, float(os.environ.get("NOBU_WAR_TICK", "30")))
    while schedule_enabled():
        try:
            tick()
        except Exception as ex:                       # never let the scheduler take the server down
            print(f"war: tick failed: {ex!r}", file=sys.stderr, flush=True)
        await asyncio.sleep(period)


# ---------------------------------------------------------------------------- status
def fmt(week, off):
    return dt.datetime.fromtimestamp(week + off, JST).strftime("%a %m-%d %H:%M")


def status_lines(st=None, now=None):
    st = st or war.state()
    p = view(st, now)
    if not st.pair:
        return ["War: no war is scheduled."]
    a, d, place = names(st)
    rounds = [r for r in st.week.get("rounds", []) if "result" in r]
    aw = sum(1 for r in rounds if r["result"] == "a")
    dw = sum(1 for r in rounds if r["result"] == "d")
    lines = [f"War: {a} (attacker) vs {d} at {place}."]
    if p["phase"] == "static":
        lines.append("Schedule off (NOBU_WAR_SCHEDULE=0).")
        return lines
    lines.append(f"Phase: {p['phase']}, war clock {fmt(p['week'], p['now'])} (x{scale():g}).")
    if p["phase"] == "round":
        sc = st.week.get("score", [0, 0])
        lines.append(f"Round {p['round'] + 1}: score {sc[0]} - {sc[1]}, ends {fmt(p['week'], p['round_end'])}.")
    lines.append(f"Rounds: {a} {aw}, {d} {dw}, draws {len(rounds) - aw - dw}.")
    if p["phase"] != "round":
        lines.append(f"Next: {fmt(p['week'], p['next_start'])}.")
    if st.last_war and p["phase"] in ("build", "results"):
        lw = st.last_war
        who = war.FACTION_NAMES[lw["winner"]] if lw.get("winner") else "nobody"
        lines.append(f"Last war: won by {who} ({lw['wins'][0]}-{lw['wins'][1]}).")
    return lines
