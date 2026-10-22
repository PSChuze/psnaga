"""The Meeting Hall job loop: a hall master NPC in every clan's home town hands out repeatable jobs (kill N of an
enemy kind of that province, bring N of a drop item); turning one in pays money, EXP and merit, and merit raises
the rank (progression.update_rank)., per town.

Wire (all KNOWN layouts, reused here):
  jobs are quest-window entries (Profile -> Quest tab): 34 02 ENTRY adds one (and answers `2a 03 <seq> <id>`
  when sent with that seq), 34 04 ENTRY updates it in place (kill progress), 34 03 id removes it. The job text
  carries the objective and the progress; the client has no objectives list for. Money: 30 06 total
  (shops.notify_money). Items handed in: 30 04 {index, count} (shops.notify_counts, HYPOTHESIS layout,
  NOBU_SHOP_ITEM_PUSH=0 turns it off). EXP / merit / rank: progression.flush (2F 0D / 0C / 0E + level-up and
  promotion lines as 0D 06).
  Talk: 28 01 on the NPC, answered with 0D 09 (NPC line) + 0D 05 (popup) like the other NPCs.

DESIGN (no server data survives): one job at a time; talking to the master offers the next job for the
character's level (rotation), accepting is implicit (as for the tutorial quest); kill progress counts won
battles against the job's enemy kinds after the job was taken (fights are 1v1: one win = one kill); bring jobs
check the backpack when the character reports back and take the items then. Rewards are ours (data file).

Kill hook: battle.finish calls on_kill(ctx, target cid, store) after enemies.on_defeated (one line). Without
the patch the kills are still counted, later: enemies.on_defeated appends ("defeated", cid, kind, secs) to
ctx["enemies_log"], and this module reads new entries on 23 5a (sent again after every battle), on 28 01, 2a
03 and its chat commands.

Data: $NOBU_DATA/guild.json (NOBU_GUILD_FILE), created from GUILD_SEED on first use and replaced by a newer
seed_version (old file kept as guild.json.v<N>.bak) unless it has "custom": true.
Character state (chars.json): c["guild"] = {"job": qid | None, "kills": n, "taken": time, "last": qid,
"done": {qid: times}}, c["merit"], c["rank"] (progression.py).

Env:
  NOBU_GUILD=0              module off (no NPC, answer() returns None)
  NOBU_GUILD_FILE           data file (default $NOBU_DATA/guild.json)
  NOBU_GUILD_CID            the master's cid (default: the data's, 0xfea00004)
  NOBU_GUILD_ANY_ZONE=1     outside the towns, spawn the home town's master (default: each master only in its town)
  NOBU_GUILD_JOB_HALLS=0    the master at the town centre for every job (default: at the player's job hall where it
                            is located, in the castle for samurai / onmyoji / ninja)
  NOBU_GUILD_CHAT=0         no chat commands ("jobs", "job <n>", "job cancel", "merit", "merit <n>")
  NOBU_GUILD_DEBUG=0        no "merit <n>" debug award (default on, like progression's "exp <n>")
"""
import copy
import datetime as dt
import json
import os
import shutil
import unicodedata

from . import persist
from . import chars, items, misc, npc, progression, quests, shops, social, towns, vce

ROLE = "guild"
QID_MIN, QID_MAX = 2001, 2999            # quest-window ids of guild jobs (tutorial quests use 1001..)

# Item ids = STRDAT table 0 (strdat_00_items.tsv), enemy kinds = enemies_seed.json keys (the wiki's
# Owari table, drops with their STRDAT ids). Rewards are DESIGN: money in mon (a Lv 1-4 enemy pays 0-60 mon,
# food costs 4-6 mon, Heal Powder 800), EXP about half the kills' own EXP again, merit so that Ashigaru Capt
# (350) takes about 15 jobs and Ashigaru Gen. (900) about 30.
GUILD_SEED = {
    "seed_version": 3,                   # 2: per-town masters and jobs (towns.py), Nagoya jobs carry "zone": 110;
                                         # 3: English-only title / target / text
    "master": {
        "cid": "0xfea00004", "family": "Hall Master", "given": "", "zone": 110,
        "dx": -300, "dz": -300, "dir": 45, "style": "guildmaster",
        "greeting": "This is the Meeting Hall. We have work for anyone willing to serve the house.",
        "src": "Meeting Hall head of the 2003-04 tutorial chain; UIMSG 4.9.18",
    },
    "jobs": [
        {"id": 2001, "zone": 110, "key": "rat_cull", "title": "Cull the rats", "kind": "kill", "enemies": ["rat"],
         "target": "Rats", "count": 5, "min_level": 1,
         "text": "Rats are eating the rice stores. Defeat 5 Rats (Lv 1) in the Owari field outside Nagoya.",
         "reward": {"money": 150, "exp": 40, "merit": 15}},
        {"id": 2002, "zone": 110, "key": "rat_tails", "title": "Bring rat tails", "kind": "bring", "item": 601,
         "target": "Rat Tails", "count": 5, "min_level": 1,
         "text": "Proof of the rat hunt is wanted. Bring 5 Rat Tails, dropped by Rats.",
         "reward": {"money": 200, "exp": 30, "merit": 20}},
        {"id": 2003, "zone": 110, "key": "snake_cull", "title": "Clear the snakes", "kind": "kill",
         "enemies": ["snake"], "target": "Snakes", "count": 5, "min_level": 2,
         "text": "Snakes frighten the farmers on the paths. Defeat 5 Snakes (Lv 2) in the Owari field.",
         "reward": {"money": 200, "exp": 60, "merit": 20}},
        {"id": 2004, "zone": 110, "key": "snake_skins", "title": "Bring snake skins", "kind": "bring", "item": 603,
         "target": "Snake Skins", "count": 5, "min_level": 2,
         "text": "The leather workers need skins. Bring 5 Snake Skins, dropped by Snakes.",
         "reward": {"money": 250, "exp": 50, "merit": 25}},
        {"id": 2005, "zone": 110, "key": "bat_cull", "title": "Drive off the rat bats", "kind": "kill",
         "enemies": ["nezu_bat", "big_bat"], "target": "Rat Bats", "count": 5,
         "min_level": 3,
         "text": "Rat Bats swarm the fields at dusk. Defeat 5 Rat Bats (Lv 3) in the Owari field.",
         "reward": {"money": 250, "exp": 90, "merit": 25}},
        {"id": 2006, "zone": 110, "key": "bat_wings", "title": "Bring bat wings", "kind": "bring", "item": 611,
         "target": "Bat Wings", "count": 5, "min_level": 3,
         "text": "A physician wants bat wings for medicine. Bring 5 Bat Wings.",
         "reward": {"money": 300, "exp": 80, "merit": 30}},
        {"id": 2007, "zone": 110, "key": "thief_hunt", "title": "Catch the thieves", "kind": "kill",
         "enemies": ["petty_thief", "wander_suppa", "robber", "highwayman", "sneak_thief"],
         "target": "Thieves", "count": 3, "min_level": 4,
         "text": "Thieves rob travellers on the Owari roads. Defeat 3 thieves (Petty Thief Lv 4 or worse).",
         "reward": {"money": 400, "exp": 120, "merit": 40}},
        {"id": 2008, "zone": 110, "key": "thief_topknots", "title": "Bring thieves' topknots", "kind": "bring",
         "item": 732, "target": "Thief's Topknots", "count": 3, "min_level": 7,
         "text": "Bring 3 Thief's Topknots as proof: Rogue Suppa, Robbers and Highwaymen carry them.",
         "reward": {"money": 500, "exp": 150, "merit": 50}},
        {"id": 2009, "zone": 110, "key": "wolf_hunt", "title": "Hunt the man-eaters", "kind": "kill",
         "enemies": ["maneater_wolf", "lone_wolf"], "target": "Wolves", "count": 3,
         "min_level": 12,
         "text": "Man-eater Wolves attack villages in Owari. Defeat 3 wolves (Lv 13 or more).",
         "reward": {"money": 800, "exp": 400, "merit": 80}},
        {"id": 2010, "zone": 110, "key": "wolf_fangs", "title": "Bring wolf fangs", "kind": "bring", "item": 604,
         "target": "Wolf Fangs", "count": 5, "min_level": 12,
         "text": "The armourers want fangs for charms. Bring 5 Wolf Fangs, dropped by wolves.",
         "reward": {"money": 900, "exp": 350, "merit": 90}},
    ],
}


# ---------------------------------------------------------------------------- the other home towns
def _plural(en):
    if en.endswith("s"):
        return en
    if en.endswith("ief"):
        return en[:-1] + "ves"                           # Thief -> Thieves
    if en.endswith("y") and en[-2:-1] not in "aeiou":
        return en[:-1] + "ies"                           # Spy -> Spies
    return en + "s"


MASS_NOUNS = ("Meat", "Silk", "Leather", "Water", "Paper", "Iron Sand", "Charcoal", "Rice")


def _plural_item(en):
    """English plural of an item name for "Bring 5 ..." (Rat Tail -> Rat Tails; mass nouns stay)."""
    if en.endswith(MASS_NOUNS):
        return en
    if en.endswith("Tooth"):
        return en[:-5] + "Teeth"
    return _plural(en)


def _town_jobs(t, m, enemy_data):
    """DESIGN: the Hall Master jobs of home town `m` (not Nagoya, whose jobs are written out above), built from its
    province's starter enemies in enemies_seed.json (the wiki's province tables): for the five lowest-level kinds a
    kill job, and a bring job for the first material / food drop not asked for yet; ids 2000 + 20 x clan + 1..,
    rewards on Nagoya's scale (Lv 1: kill 150 mon / 40 EXP / 15 merit, bring 200 / 30 / 20)."""
    prov, field = t["province"], towns.PROVINCE_EN.get(t["province"], "home")
    kinds = [(eid, e) for eid, e in enemy_data["enemies"].items() if e.get("province") == prov]
    kinds = sorted(kinds, key=lambda k: int(k[1]["level"][0]))[:5]
    out, used = [], set()
    qid = 2000 + 20 * int(t["faction"])
    for eid, e in kinds:
        lv = int(e["level"][0])
        n = 5 if lv <= 3 else 3
        qid += 1
        out.append({"id": qid, "zone": m, "key": f"{eid}_cull", "title": f"Drive off the {_plural(e['name'])}",
                    "kind": "kill", "enemies": [eid], "target": _plural(e["name"]), "count": n,
                    "min_level": max(1, lv),
                    "text": f"{_plural(e['name'])} trouble the roads of {field}. Defeat {n} {_plural(e['name'])} "
                            f"(Lv {lv}) in the {field} field outside {t['name']}.",
                    "reward": {"money": 100 + 50 * lv, "exp": 20 + 20 * lv, "merit": 10 + 5 * lv},
                    "src": "DESIGN; enemy: enemies_seed.json " + eid})
        drop = next((d for d in e.get("drops", []) if d.get("stack") and d.get("type") in (54, 69)
                     and d["id"] not in used), None)
        if drop is None:
            continue
        used.add(drop["id"])
        qid += 1
        what = _plural_item(items.item_name(drop["id"]))
        out.append({"id": qid, "zone": m, "key": f"{eid}_bring", "title": f"Bring {what}", "kind": "bring",
                    "item": drop["id"], "target": what, "target_jp": items.names().get(drop["id"], ("", ""))[0],
                    "count": n,
                    "min_level": max(1, lv),
                    "text": f"The {t['house']} house needs supplies. Bring {n} {what}, dropped by "
                            f"{_plural(e['name'])} in the {field} field.",
                    "reward": {"money": 150 + 50 * lv, "exp": 10 + 20 * lv, "merit": 15 + 5 * lv},
                    "src": "DESIGN; drop: enemies_seed.json " + eid})
    return out


def _town_masters():
    """A Hall Master per home town: Nagoya's (GUILD_SEED["master"]) and one per other town with Nagoya's offsets
    (turned with the town's gate by npc.town_spot) and a per-town cid (shops.town_cid role 4)."""
    base = GUILD_SEED["master"]
    out = [dict(base)]
    for m, t in sorted(towns.TOWNS.items()):
        if m != towns.NAGOYA:
            out.append(dict(base, cid=f"{shops.town_cid(m, 4):#x}", zone=m,
                            greeting=f"This is the Meeting Hall of {t['name']}. We have work for anyone willing to "
                                     f"serve the {t['house']} house."))
    return out


def _extend_seed():
    from . import enemies
    try:
        ed = enemies.seed()
    except (OSError, ValueError):
        ed = {"enemies": {}}
    GUILD_SEED["masters"] = _town_masters()
    for m, t in sorted(towns.TOWNS.items()):
        if m != towns.NAGOYA:
            GUILD_SEED["jobs"] += _town_jobs(t, m, ed)


_extend_seed()
_DATA = {"path": None, "mtime": None, "data": None}


# ---------------------------------------------------------------------------- knobs / data
def enabled():
    return os.environ.get("NOBU_GUILD", "1") != "0"


def data_path():
    return os.environ.get("NOBU_GUILD_FILE") or os.path.join(misc.data_dir(), "guild.json")


def _valid(d):
    return isinstance(d, dict) and isinstance(d.get("jobs"), list) and isinstance(d.get("master"), dict)


def load_data(path=None):
    """guild.json: created from GUILD_SEED when missing, replaced by a newer seed unless "custom": true.
    Cached until the file changes; any read error falls back to the seed."""
    path = path or data_path()
    seed = copy.deepcopy(GUILD_SEED)
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            persist.atomic_write_json(path, seed, ensure_ascii=False, indent=1)
        except OSError:
            return seed
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return seed
    if _DATA["path"] == path and _DATA["mtime"] == mtime and _DATA["data"] is not None:
        return _DATA["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        persist.warn_once((path, repr(e)), f"{path} does not parse ({e!r}); using the built-in seed")
        data = None
    if not _valid(data):
        return seed
    if not data.get("custom") and int(data.get("seed_version", 0)) < int(seed["seed_version"]):
        try:
            shutil.copyfile(path, f"{path}.v{data.get('seed_version', 0)}.bak")
            persist.atomic_write_json(path, seed, ensure_ascii=False, indent=1)
            mtime = os.path.getmtime(path)
        except OSError:
            pass
        data = seed
    _DATA.update(path=path, mtime=mtime, data=data)
    return data


def reset():
    _DATA.update(path=None, mtime=None, data=None)


def jobs(data=None):
    """qid -> job, in id order (only well-formed jobs with an id in the guild range)."""
    out = {}
    for j in (data or load_data())["jobs"]:
        try:
            qid = int(j["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if QID_MIN <= qid <= QID_MAX and j.get("kind") in ("kill", "bring") and int(j.get("count", 0)) > 0:
            out[qid] = j
    return dict(sorted(out.items()))


def is_job(qid, data=None):
    return qid in jobs(data)


def master_cid(data=None, m=None):
    """The cid of master m (default: data["master"], Nagoya's). NOBU_GUILD_CID overrides Nagoya's only."""
    data = data or load_data()
    base = data["master"]
    m = m or base
    env = os.environ.get("NOBU_GUILD_CID") if m.get("zone") == base.get("zone") else None
    raw = env or m.get("cid", "0xfea00004")
    cid = int(raw, 0) & 0xFFFFFFFF if isinstance(raw, str) else int(raw) & 0xFFFFFFFF
    return npc.check_npc_cid(cid, "NOBU_GUILD_CID")


def masters(data=None):
    """Every hall master: data["masters"] (one per home town, seed 2), else the single data["master"]."""
    data = data or load_data()
    ms = data.get("masters")
    return [m for m in ms if isinstance(m, dict)] if isinstance(ms, list) and ms else [data["master"]]


def town_jobs(zone, data=None):
    """qid -> job offered in town `zone`: the jobs whose "zone" is that town, or unset (a custom file's jobs)."""
    return {q: j for q, j in jobs(data).items() if j.get("zone") in (None, zone)}


def job_town(c, ctx=None, data=None):
    """The town whose jobs a character sees: the current zone when it has a hall master, else the home town."""
    zone = (ctx or {}).get("zone", chars.char_position(c)[0])
    if any(m.get("zone") == zone for m in masters(data)):
        return zone
    return chars.home_town(c) or towns.NAGOYA


# ---------------------------------------------------------------------------- character state
def now():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def gstate(c):
    g = c.setdefault("guild", {})
    g.setdefault("job", None)
    g.setdefault("kills", 0)
    g.setdefault("done", {})
    return g


def active(c, data=None):
    """The character's current job dict, or None."""
    if not c:
        return None
    qid = (c.get("guild") or {}).get("job")
    return jobs(data).get(qid) if qid else None


def _save(store):
    st = store() if callable(store) else store
    if st is not None and hasattr(st, "save"):
        st.save()


def item_count(c, item_id, store=None):
    """How many of item_id the backpack holds (worn items do not count)."""
    inv = items.inventory(c, store)
    return sum(int(it.get("count", 1)) for it in inv["items"]
               if it["id"] == item_id and it.get("pos") is None)


def progress(c, job, store=None):
    """(have, need) for job: kills since it was taken, or the matching items in the backpack."""
    need = int(job["count"])
    if job["kind"] == "kill":
        have = int((c.get("guild") or {}).get("kills", 0))
    else:
        have = item_count(c, int(job["item"]), store)
    return min(have, need), need


def complete(c, job, store=None):
    have, need = progress(c, job, store)
    return have >= need


def eligible(c, data=None, zone=None):
    """Jobs the character's level allows (of town `zone` only, when given)."""
    lv = int((c or {}).get("level", 1) or 1)
    table = jobs(data) if zone is None else town_jobs(zone, data)
    return [j for q, j in table.items() if int(j.get("min_level", 1)) <= lv
            and lv <= int(j.get("max_level", 255))]


def next_offer(c, data=None, zone=None):
    """The next eligible job after the last one taken (rotation through the list; town `zone`'s jobs when given),
    or None."""
    el = eligible(c, data, zone)
    if not el:
        return None
    last = int((c.get("guild") or {}).get("last") or 0)
    after = [j for j in el if int(j["id"]) > last]
    return (after or el)[0]


# ---------------------------------------------------------------------------- texts / entries
def reward_text(job):
    rw = job.get("reward") or {}
    parts = []
    if rw.get("money"):
        parts.append(f"{int(rw['money'])} mon")
    if rw.get("exp"):
        parts.append(f"{int(rw['exp'])} EXP")
    if rw.get("fame"):
        parts.append(f"{int(rw['fame'])} fame")
    if rw.get("merit"):
        parts.append(f"{int(rw['merit'])} merit")
    return ", ".join(parts) or "nothing"


def job_text(c, job, store=None):
    """Entry text (max 0xc0 SJIS bytes): progress first so the clip never cuts it, then the job's text and
    the reward."""
    have, need = progress(c, job, store)
    verb = "defeated" if job["kind"] == "kill" else "in your pack"
    done = " Done: report to the Hall Master." if have >= need else ""
    return f"[{have}/{need} {verb}]{done} {job.get('text', '')} Reward: {reward_text(job)}."


def job_title(job):
    return "[Guild] " + job.get("title", "Job")


def job_entry(c, job, store=None):
    return quests.entry(int(job["id"]), job_title(job), job_text(c, job, store))


# ---------------------------------------------------------------------------- NPC
def master_npc(player, data=None, m=None):
    data = data or load_data()
    m = m or data["master"]
    lk = npc.npc_looks(player, m.get("style", "guildmaster"))
    return dict(cid=master_cid(data, m), uid=0, b6b=npc.TALK_ONLY, family=m.get("family", "Hall Master"),
                given=m.get("given", ""), looks=lk, level=player.get("level", 1), role=ROLE,
                text=m.get("greeting", ""), town=m.get("zone"))


# The Meeting Hall is per job (2004 FAQ): samurai, monk, priest, onmyoji onmyoji hall, ninja Ninjahouse, smith
# smithy, physician apothecary; the samurai, onmyoji and ninja halls are in the castle (town map + 300). Where a
# hall is located (town_spots.json role hall_<job>,: Nagoya and Nagoya Castle) the Hall Master stands there for
# that job; elsewhere at the town centre (role guildmaster).
CASTLE_JOBS = {1, 4, 5}


def job_halls_on():
    return os.environ.get("NOBU_GUILD_JOB_HALLS", "1") != "0"


def job_of(player):
    lk = player.get("looks") or []
    return int(lk[3]) if len(lk) > 3 else 0


def castle_hall(player, town):
    """(zone, x, y, z, dir) of the player's job hall in `town`'s castle, or None."""
    if not job_halls_on() or job_of(player) not in CASTLE_JOBS:
        return None
    from . import castle
    return castle.castle_spot(int(town) + 300, "hall_%d" % job_of(player))


def master_position(player, data=None, m=None):
    """The player's job hall in town when it is located (hall_<job>), else the town centre spot (guildmaster), else
    Nagoya's offsets from the town's spawn point, turned with its gate (npc.town_spot)."""
    m = m or (data or load_data())["master"]
    zone = chars.char_position(player)[0]
    if job_halls_on():
        hall = towns.spot(zone, "hall_%d" % job_of(player))
        if hall:
            return hall
    return npc.town_spot(player, int(m.get("dx", -300)), int(m.get("dz", -300)), int(m.get("dir", 0)) % 360,
                         role=m.get("spot", "guildmaster"))


def world_ready(ctx, store=None):
    """23 5a hook (after quests.world_ready): the hall master in its zone, the active job's entry (34 02; the
    client list starts empty at every login and quests.world_ready only lists the tutorial quests), and the
    progress of kills logged by enemies.on_defeated since the last check (a battle just ended)."""
    c = ctx.get("char")
    if not enabled() or not c:
        return []
    data = load_data()
    out = []
    zone = ctx.get("zone", chars.char_position(c)[0])
    rows = [(i, m, None) for i, m in enumerate(masters(data))]
    here = shops.local_keepers(rows, zone, c, os.environ.get("NOBU_GUILD_ANY_ZONE") == "1")
    if not here and towns.is_town(int(zone) - 300 if isinstance(zone, int) else -1):
        # in a castle: the town's master stands at the player's castle job hall (samurai / onmyoji / ninja)
        town = int(zone) - 300
        if castle_hall(c, town):
            here = [r for r in rows if r[1].get("zone") == town]
    for _, m, _ in here:
        if towns.is_town(zone) and castle_hall(c, zone):
            continue                                # this job's hall is in the castle: the master waits there
        n = master_npc(c, data, m)
        pos = castle_hall(c, int(zone) - 300) if not towns.is_town(zone) else master_position(c, data, m)
        if pos is None:
            continue
        n["pos"] = list(pos)
        ctx.setdefault("npcs", {})[n["cid"]] = n
        out += npc.spawn_pushes(n, pos)
    lines = sync_log(ctx, store)
    job = active(c, data)
    if job is not None:
        out.append(quests.quest_add(job_entry(c, job, store)))
    return out + lines


# ---------------------------------------------------------------------------- kills
def _kind(enemy):
    """Enemy kind from a kind string or a field-enemy cid (enemies.spawn)."""
    if isinstance(enemy, str):
        return enemy
    if isinstance(enemy, int):
        from . import enemies
        s = enemies.spawn(enemy) if enemies.is_enemy_cid(enemy) else None
        return s["enemy"] if s else None
    return None


def count_kill(ctx, kind, store=None, update=True):
    """One defeated enemy of `kind`: +1 on the active kill job when it matches. Returns frames (34 04 update
    + a progress line) or []."""
    c = ctx.get("char")
    job = active(c)
    if not kind or job is None or job["kind"] != "kill" or kind not in job.get("enemies", []):
        return []
    g = gstate(c)
    need = int(job["count"])
    if g["kills"] >= need:
        return []
    g["kills"] += 1
    _save(store)
    out = [quests.quest_update(job_entry(c, job, store))] if update else []
    line = f"{job['title']}: {g['kills']}/{need} {job.get('target', kind)} defeated."
    if g["kills"] >= need:
        line += f" Report to the Hall Master in {towns.name(job.get('zone'), 'town')}."
    return out + [misc.notify_system(line)]


def sync_log(ctx, store=None, skip_cid=None):
    """Count the "defeated" entries enemies.on_defeated appended to ctx["enemies_log"] since the last call.
    Returns (frames). skip_cid: a cid on_kill counts itself (so it is never counted twice)."""
    log = ctx.get("enemies_log") or []
    seen = int(ctx.get("guild_log_seen", 0))
    ctx["guild_log_seen"] = len(log)
    out = []
    for ent in log[seen:]:
        if len(ent) >= 3 and ent[0] == "defeated" and ent[1] != skip_cid:
            out += count_kill(ctx, ent[2], store)
    return out


def on_kill(ctx, enemy, store=None):
    """Battle hook (battle.finish after a won battle): enemy = the target cid (a field enemy) or an enemy kind.
    Returns frames for this session (34 04 job update + a progress line)."""
    if not enabled() or not (ctx or {}).get("char"):
        return []
    cid = enemy if isinstance(enemy, int) else None
    log = ctx.get("enemies_log") or []
    logged = any(len(e) >= 2 and e[0] == "defeated" and e[1] == cid for e in log[int(ctx.get("guild_log_seen", 0)):])
    out = sync_log(ctx, store, skip_cid=cid if logged else None)
    return out + count_kill(ctx, _kind(enemy), store)


# ---------------------------------------------------------------------------- take / turn in / cancel
def take(ctx, job, store=None, n=None):
    """Make job the active one. Returns frames: NPC line (when talking to n), 34 02, system line."""
    c = ctx["char"]
    g = gstate(c)
    g.update(job=int(job["id"]), kills=0, taken=now(), last=int(job["id"]))
    _save(store)
    out = []
    if n is not None:
        out += quests.say(n, f"{job.get('text', '')} Come back to me when it is done.")
    out.append(quests.quest_add(job_entry(c, job, store)))
    out.append(misc.notify_system(f"New job: {job['title']}. Reward: {reward_text(job)}."))
    if job["kind"] == "bring":
        have, need = progress(c, job, store)
        if have >= need:
            out.append(misc.notify_system("You already carry enough: report to the Hall Master."))
    return out


def take_items(c, item_id, count, store=None):
    """Remove count of item_id from the backpack (fullest stacks last). Returns the changed item dicts (count
    0 = gone) or None when there are not enough."""
    inv = items.inventory(c, store)
    stacks = [it for it in inv["items"] if it["id"] == item_id and it.get("pos") is None]
    if sum(int(it.get("count", 1)) for it in stacks) < count:
        return None
    left, changed = count, []
    for it in sorted(stacks, key=lambda i: int(i.get("count", 1))):
        if not left:
            break
        k = min(left, int(it.get("count", 1)))
        it["count"] = int(it.get("count", 1)) - k
        left -= k
        changed.append(it)
        if it["count"] <= 0:
            inv["items"].remove(it)
    items.save(store)
    return changed


def turn_in(ctx, job, n, store=None):
    """Hand in a complete job: items taken (bring), money, EXP / merit (progression.award_ctx), entry removed.
    Returns frames."""
    c = ctx["char"]
    out = []
    pushes = []
    if job["kind"] == "bring":
        changed = take_items(c, int(job["item"]), int(job["count"]), store)
        if changed is None:
            return quests.say(n, "You do not have everything yet.")
        pushes += shops.item_pushes(changed, sold=True)
    rw = job.get("reward") or {}
    inv = items.inventory(c, store)
    if rw.get("money"):
        inv["money"] = int(inv.get("money", 0)) + int(rw["money"])
        pushes.insert(0, shops.notify_money(inv["money"]))
    g = gstate(c)
    key = str(job["id"])
    g["done"][key] = int(g["done"].get(key, 0)) + 1
    g.update(job=None, kills=0)
    progression.award_ctx(ctx, exp=int(rw.get("exp", 0)), fame=int(rw.get("fame", 0)),
                          merit=int(rw.get("merit", 0)), store=store)
    out += quests.say(n, "Well done. Here is your pay; the house will remember your service.")
    out.append(quests.quest_remove(int(job["id"])))
    out.append(misc.notify_system(f"Job complete: {job['title']}. Reward: {reward_text(job)}."))
    out += pushes
    out += progression.flush(ctx)                     # 2F 0D / 0C / 0E + level-up / promotion lines
    ctx.setdefault("guild_log", []).append(("turn_in", int(job["id"])))
    return out


def cancel(ctx, store=None):
    c = ctx["char"]
    job = active(c)
    if job is None:
        return [misc.notify_system("You have no guild job.")]
    gstate(c).update(job=None, kills=0)
    _save(store)
    return [quests.quest_remove(int(job["id"])), misc.notify_system(f"Job given up: {job['title']}.")]


def talk(ctx, n, store=None):
    """○ on the hall master: turn in a finished job, remind of an unfinished one, else offer the next job."""
    c = ctx["char"]
    out = sync_log(ctx, store)
    from . import lifecycle
    exam = lifecycle.exam_talk(ctx, n, store)          # promotion exam (Yoriki / Samurai Gen.) first (lifecycle.py)
    if exam is not None:
        return out + exam
    job = active(c)
    if job is not None:
        if complete(c, job, store):
            return out + turn_in(ctx, job, n, store)
        have, need = progress(c, job, store)
        return out + quests.say(n, f"{job['title']}: {have}/{need} so far. {job.get('text', '')}") + [
            misc.notify_system("Say \"jobs\" for the job list, \"job cancel\" to give this job up.")]
    offer = next_offer(c, zone=n.get("town"))               # this master's town's jobs (any, for a townless one)
    if offer is None:
        return out + quests.say(n, "There is no work for you at the moment.")
    return out + take(ctx, offer, store, n)


# ---------------------------------------------------------------------------- chat
def _chat_text(payload):
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    _, text = social.parse_line(line)
    return unicodedata.normalize("NFKC", text or "").strip().lower().lstrip("/!").strip()


def job_list(c, zone=None):
    """The job list of town `zone` (job_town: the current town, else the home town), numbered for "job <n>"."""
    lines = []
    job = active(c)
    if job is not None:
        have, need = progress(c, job)
        lines.append(f"Current job: {job['title']} ({have}/{need}).")
    for i, j in enumerate((jobs() if zone is None else town_jobs(zone)).values(), 1):
        lv = int(j.get("min_level", 1))
        mark = "" if lv <= int(c.get("level", 1) or 1) else f" (from Lv {lv})"
        lines.append(f"{i}. {j['title']}: {j['kind']} {j['count']} {j.get('target', '')}{mark}."
                     f" {reward_text(j)}.")
    lines.append("Say \"job <number>\" to take one, \"job cancel\" to give yours up.")
    return [misc.notify_system(t) for t in lines]


def chat(ctx, text, store=None):
    toks = (text or "").split()
    if not toks or toks[0] not in ("jobs", "job", "merit", "rank"):
        return None
    c = ctx["char"]
    if toks[0] in ("merit", "rank"):
        if (toks[0] == "merit" and len(toks) > 1 and os.environ.get("NOBU_GUILD_DEBUG", "1") == "1"):
            try:
                progression.award_ctx(ctx, merit=int(toks[1]), store=store)
            except ValueError:
                return None
        nxt = progression.next_rank(c)
        more = (f" Next: {progression.rank_label(nxt[0], progression.faction_of(c))} at {nxt[1]} merit"
                + (" and an exam." if nxt[2] else ".") if nxt else "")
        line = (f"Rank {progression.rank_label(int(c.get('rank', 0)), progression.faction_of(c))},"
                f" merit {int(c.get('merit', 0))}.{more}")
        return progression.flush(ctx) + [misc.notify_system(line)]
    out = sync_log(ctx, store)
    here = job_town(c, ctx)
    if toks[0] == "jobs" or len(toks) == 1:
        return out + job_list(c, here)
    arg = toks[1]
    if arg in ("cancel", "quit", "drop"):
        return out + cancel(ctx, store)
    try:
        k = int(arg)
    except ValueError:
        return out + [misc.notify_system("Say \"job <number>\" (see \"jobs\").")]
    table, local_ = jobs(), list(town_jobs(here).values())      # "job 2002" by id anywhere, "job 3" = list row
    job = table.get(k) or (local_[k - 1] if 1 <= k <= len(local_) else None)
    if job is None:
        return out + [misc.notify_system("There is no such job.")]
    if active(c) is not None:
        return out + [misc.notify_system(f"You already have a job: {active(c)['title']}. Say \"job cancel\" first.")]
    if int(job.get("min_level", 1)) > int(c.get("level", 1) or 1):
        return out + [misc.notify_system(f"That job needs level {job['min_level']}.")]
    return out + take(ctx, job, store)


# ---------------------------------------------------------------------------- dispatch
def answer(payload, ctx, store=None):
    """2a 03 for a guild job id, 28 01 on the hall master, chat commands; None for everything else."""
    if not enabled() or len(payload) < 4 or not (ctx or {}).get("char"):
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x2A and sub == 3:
        d = quests.decode_detail_request(payload)
        if not QID_MIN <= d["id"] <= QID_MAX:
            return None                                 # a tutorial quest: quests.py answers
        lines = sync_log(ctx, store)
        job = active(ctx["char"])
        if job is not None and int(job["id"]) == d["id"]:
            return [quests.quest_add(job_entry(ctx["char"], job, store), d["seq"])] + lines
        return [quests.quest_remove(d["id"], d["seq"])] + lines
    if cmd == 0x28 and sub == 1:
        n = (ctx.get("npcs") or {}).get(ctx.get("target"))
        if n is None or n.get("role") != ROLE:
            return None
        ctx.setdefault("actions", []).append(ctx.get("target"))
        return talk(ctx, n, store)
    if cmd == 0x23 and sub == 8 and os.environ.get("NOBU_GUILD_CHAT", "1") == "1":
        text = _chat_text(payload)
        return chat(ctx, text, store) if text is not None else None
    return None


def describe(payload, direction="C>S"):
    if direction != "C>S" or len(payload) < 8 or payload[:2] != b"\x2a\x03":
        return None
    try:
        d = quests.decode_detail_request(payload)
    except vce.WireError:
        return None
    if QID_MIN <= d["id"] <= QID_MAX:
        return f"GUILD job detail request seq={d['seq']} id={d['id']}"
    return None
