"""The world's public snapshot, for a status board and the admin port (adminport.py).

A board can run in its own container with ONLY $NOBU_DATA/board/ mounted read-only: it never connects
to the world (a board bug can never touch a session) and never sees chars.json, war.json or auth.json
beside it. So everything the board draws is published here, and nothing else: who is online, where the
war clock stands (the week view needs this process's clock scale and schedule), the clans and
provinces, and the standings (character names with their public level / merit / fame / deeds).

Shape (version 1), written atomically to NOBU_LIVE_FILE (default $NOBU_DATA/board/live.json):
  {"v": 1, "game": "nobunaga", "updated": unix, "online": N,
   "players": [{"name", "clan", "level", "rank"}],
   "clans": [{"id", "name", "online", "members", "provinces", "funds"}],   # factions 1..14
   "provinces": [{"id", "name", "clan", "power"}],                        # provinces 1..21
   "war": {"phase", "round", "rounds", "next_in", "attacker", "defender", "field",
           "score": [a, d] | null, "results": ["a" | "d" | "draw", ...], "last": WAR | null},
   "history": [WAR, ...],                                                  # oldest first, <= 10
   "standings": {"level" | "merit" | "fame" | "deeds": [{"place", "name", "clan", "rank", "level",
                                                         "value"}]},       # top STANDINGS_ROWS
   "totals": {"characters"}}
  WAR = {"attacker", "defender", "winner", "field", "wins": [a, d, rounds], "transfer"}.
  phase is war_week's (build / declared / round / break / results, or static with no schedule);
  next_in is REAL seconds to the next change (the war clock may run faster), null when frozen.
  deeds are this week's war deeds (war.json), merit the tie-break.

Env:
  NOBU_LIVE_FILE   output path (default <data dir>/board/live.json)
  NOBU_LIVE_EVERY  seconds between writes (default 10; 0 = off)
"""
import asyncio
import os
import sys
import time

from . import misc, persist, progression, social, war, war_week

VERSION = 1
STANDINGS_ROWS = 25
STANDINGS = ("level", "merit", "fame", "deeds")


def path():
    return os.environ.get("NOBU_LIVE_FILE") or os.path.join(misc.data_dir(), "board", "live.json")


def _clan(f):
    return war.FACTION_NAMES[f] if 0 <= f < len(war.FACTION_NAMES) else "none"


def _field(area):
    if not area:
        return ""
    return next((n for _, _, a, n in war.BORDER_FIELDS if a == area), f"place {area}")


def _player(c):
    f = progression.faction_of(c)
    return {"name": f"{c.get('family', '')}{c.get('given', '')}" or "?", "clan": _clan(f),
            "level": int(c.get("level", 1) or 1),
            "rank": progression.rank_name(int(c.get("rank", 0)), f, jp=False)}


def _war_record(lw):
    t = lw.get("transfer") or 0
    return {"attacker": _clan(lw.get("attacker", 0)), "defender": _clan(lw.get("defender", 0)),
            "winner": _clan(lw.get("winner", 0)) if lw.get("winner") else "",
            "field": _field(lw.get("area", 0)), "wins": list(lw.get("wins") or []),
            "transfer": war.PROVINCE_NAMES[t] if 0 < t < war.PROVINCES else ""}


def war_view(st=None, now=None):
    st = st or war.state()
    v = war_week.view(st, now)
    out = {"phase": v["phase"], "round": v.get("round"),
           "rounds": len(war_week.schedule()[1]) if war_week.schedule_enabled() else 0,
           "next_in": None, "attacker": "", "defender": "", "field": "", "score": None,
           "results": [r["result"] for r in st.week.get("rounds", []) if "result" in r],
           "last": _war_record(st.last_war) if st.last_war else None}
    if v["phase"] != "static" and v.get("next_start") and war_week.scale() > 0:   # 0 = frozen clock
        out["next_in"] = max(0, int((v["next_start"] - v["now"]) / war_week.scale()))
    if st.pair:
        out.update(attacker=_clan(st.pair[0]), defender=_clan(st.pair[1]), field=_field(st.area))
    if v["phase"] == "round":
        out["score"] = list(st.week.get("score", [0, 0]))
    return out


def all_chars(store):
    if store is None:
        return []
    s = store() if callable(store) else store
    return [c for cs in (getattr(s, "data", None) or {}).get("chars", {}).values() for c in cs or []
            if isinstance(c, dict) and "cid" in c]


def standings(cs, deeds):
    """{category: top STANDINGS_ROWS}, ties sharing a place; a 0 value leaves a character out."""
    rows = [dict(_player(c), cid=c["cid"], exp=int(c.get("exp", 0)), merit=int(c.get("merit", 0)),
                 fame=int(c.get("fame", 0)), deeds=int(deeds.get(str(c["cid"]), 0))) for c in cs]
    keys = {"level": lambda r: (r["level"], r["exp"]), "merit": lambda r: (r["merit"],),
            "fame": lambda r: (r["fame"],), "deeds": lambda r: (r["deeds"], r["merit"])}
    out = {}
    for cat in STANDINGS:
        key = keys[cat]
        pool = sorted((r for r in rows if key(r)[0] > 0), key=lambda r: (key(r), -r["cid"]), reverse=True)
        top, place, prev = [], 0, None
        for n, r in enumerate(pool[:STANDINGS_ROWS], 1):
            if key(r) != prev:
                place, prev = n, key(r)
            top.append({"place": place, "name": r["name"], "clan": r["clan"], "rank": r["rank"],
                        "level": r["level"], "value": key(r)[0]})
        out[cat] = top
    return out


def snapshot(store=None, now=None):
    st = war.state()
    players = [_player(ctx["char"]) for ctx in social.online()]
    players.sort(key=lambda p: (-p["level"], p["name"]))
    cs = all_chars(store)
    online, members = {}, {}
    for p in players:
        online[p["clan"]] = online.get(p["clan"], 0) + 1
    for c in cs:
        k = _clan(progression.faction_of(c))
        members[k] = members.get(k, 0) + 1
    clans = [{"id": f, "name": _clan(f), "online": online.get(_clan(f), 0),
              "members": members.get(_clan(f), 0),
              "provinces": len(st.provinces_of(f)), "funds": int(st.funds[f])}
             for f in range(1, war.FACTIONS)]
    provinces = [{"id": p, "name": war.PROVINCE_NAMES[p], "clan": _clan(st.owners[p]),
                  "power": int(st.power[p])} for p in range(1, war.PROVINCES)]
    return {"v": VERSION, "game": "nobunaga", "updated": int(time.time()), "online": len(players),
            "players": players, "clans": clans, "provinces": provinces, "war": war_view(st, now),
            "history": [_war_record(h) for h in st.history[-10:]],
            "standings": standings(cs, st.deeds), "totals": {"characters": len(cs)}}


def write(dest=None, store=None):
    dest = dest or path()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    persist.atomic_write_json(dest, snapshot(store), ensure_ascii=False, indent=None, separators=(",", ":"))


async def run(store=None):
    """The writer task (world.main starts it with its character store). A failed write is logged once
    and retried."""
    every = float(os.environ.get("NOBU_LIVE_EVERY", "10"))
    if every <= 0:
        return
    dest = path()
    print(f"live: snapshot -> {dest} every {every:g}s", file=sys.stderr, flush=True)
    while True:
        try:
            write(dest, store)
        except Exception as e:                      # noqa: BLE001 never take the world down
            persist.warn_once(("live", dest), f"live: cannot write {dest}: {e!r}")
        await asyncio.sleep(every)
