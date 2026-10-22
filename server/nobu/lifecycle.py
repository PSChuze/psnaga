"""Character lifecycle: revive (the reviver's field skill and a paid shrine), ghosts as other players see them,
stat growth on level-up, promotion exams (Yoriki / Samurai Gen.).

KNOWN (VA == offset):
- Field skill use = `26 07 <u16 seq> <u8 slot>` (SCMD_SKILL_COMMAND). Poster 0x207110(P, skill entry): 0x204090
  finds the current slot (SK+0xc[i]) holding the skill (-1 = not equipped: nothing is sent), then request kind 0x3e
  (net command 0x72, dispatch 0x18ef70 -> 0x195b30 -> builder 0x1caf80: `26 07` + seq + u8). The caller 0x6676c0
  first runs the checks of 0x667cf0 and commits the target with 0x611490 (a `25 01 <cid>` only when the target
  changed). 0x667cf0 refuses: the player's PARAMS[0] == 2 (a ghost, UIMSG 2.292), PARAMS[0xe] (2.293), two
  states (2.294 / 2.295), skill flags & 4 (auto, 2.296), **flags & 0x20 == 0 (combat-only, 2.297)**, less than
  0xb4 ticks since the skill's last use (2.298), no skill (2.299). The reply is the bare `26 07 <seq>`.
- Other characters' entries (class vtable 0x4510c0) keep PARAMS[0] only as a flag: CHAR_INFO copy vt+0x78
  (0x273570) sets entry+0x98 = (record +0x45 == 0); the entry getter vt+0x28 (0x273140) returns
  (entry+0x98 == 0) for index 0, i.e. 1 for any nonzero PARAMS[0], and +0x99 / +0x9a / +0x9b for 1 / 8 / 9.
  The actor update 0x5f6f08 calls 0x1da540(P, entry): entry "ghost" (getter(0) == 1) and the **viewer's own
  PARAMS[1] == 0** -> alpha 0 (the ghost is not drawn) and actor flag 0x08000000; otherwise 0x5f6fa0 draws it
  with alpha 0.3 (0x3e99999a). P's PARAMS[1] (only reader: 0x1da540) is therefore "can see ghosts".
- The 2F 07 stats block (0x209280) writes the nine stats P+0x10294..+0x102a4, and the Status window reads them
  through per-frame callbacks (0x66dcd0: u16 P+0x10294 with the 2-bit mark from P+0x102b0, 0x66dd20: +0x10296,
  ...), so a 2F 07 updates the shown stats at once.

DESIGN (ours; no server data survives):
- Revive: skill Revive 1 (STRDAT 1.522) on the second scroll of the Monk / Priest / Physician line (Devotions
  II, Priest Prec II, Phys Prec II, unlocked when the first scroll is mastered, skills.py). Its R flags carry
  0x20 so the client lets it be used in the field. 26 07 on it revives the targeted ghost (the 25 01 target
  when it is a ghost, else the nearest ghost in range, party members first): Life NOBU_LIFE_REVIVE_PCT % of
  max, Ki cost from the skill table, no Pass On penalty.
- Shrine (cid 0xfea00013, Nagoya): a ghost pressing ○ next to it is revived for a fee (NOBU_LIFE_SHRINE_FEE mon
  per level, at least one level's worth) instead of Passing On; a living player gets a line.
- Spirit sight: PARAMS[1] = 1 for ghosts and (NOBU_LIFE_SPIRIT_SIGHT=revivers) Monks, Priests and Physicians, so
  they see ghosts at 30 % alpha; "all" / "ghosts" / "none" change who does.
- Stat growth: stat_i(L) = creation stat_i + floor((L - 1) x (NOBU_LIFE_GROWTH_JOB x job base_i +
  NOBU_LIFE_GROWTH_BONUS x creation points put into stat_i)) (FAQ04: growth depends on job and creation bonus
  points). Applied and pushed (2F 07 + a line) by progression.flush.
- Exams: at 1800 / 3500 merit the Hall Master gives an exam quest (quest window ids 3101 / 3102); finishing
  it and reporting back promotes. Modelled on the wiki's exams (a bandit fight, a proof item).

Knobs: NOBU_LIFECYCLE=0 (module off), NOBU_LIFE_REVIVE_PCT (25), NOBU_LIFE_REVIVE_RANGE (1000),
NOBU_LIFE_SHRINE=0, NOBU_LIFE_SHRINE_CID, NOBU_LIFE_SHRINE_FEE (100), NOBU_LIFE_SHRINE_PCT (50),
NOBU_LIFE_SPIRIT_SIGHT (revivers | all | ghosts | none), NOBU_LIFE_GROWTH=0, NOBU_LIFE_GROWTH_JOB (0.1),
NOBU_LIFE_GROWTH_BONUS (0.05), NOBU_LIFE_EXAMS=0, NOBU_LIFE_CHAT=0.
"""
import datetime as dt
import math
import os
import unicodedata

from . import chars, items, misc, npc, players, progression, quests, shops, skills, social, towns, vce

REVIVE_SKILLS = {522}                   # STRDAT 1.522 Revive 1 (1.523 / 1.524 are)
REVIVER_JOBS = {2, 3, 7}                # Monk, Priest, Physician
SHRINE_ROLE = "shrine"
SHRINE_ZONE = 110                       # Nagoya
STAT_NAMES = ("Str", "Sta", "Dex", "Int", "Cha", "Earth", "Water", "Fire", "Wind")

THIEVES = ["petty_thief", "wander_suppa", "robber", "highwayman", "sneak_thief"]
EXAMS = {   # rank -> exam quest (DESIGN, after the wiki's Yoriki / Samurai Gen. exams: a bandit fight, a proof item)
    4: dict(id=3101, rank=4, kind="kill", count=3, enemies=THIEVES, target="bandits",
            title="Exam: the bandit chief",
            text="Bandits on the Owari roads carry the chief's orders. Defeat three of them and report back.",
            offer="Your merit is enough for Yoriki. First prove yourself: defeat three bandits on the Owari "
                  "roads and report to me."),
    5: dict(id=3102, rank=5, kind="bring", count=5, item=604, target="wolf fangs",
            title="Exam: proof of valour",
            text="Bring five wolf fangs from the man-eaters of Owari as proof of your valour.",
            offer="A Samurai General leads men. Bring me five wolf fangs from the Owari man-eaters as proof."),
}
EXAM_IDS = {e["id"]: r for r, e in EXAMS.items()}


def _on(name, default="1"):
    return os.environ.get(name, default) == "1"


def _i(name, default):
    try:
        return int(os.environ.get(name, str(default)), 0)
    except ValueError:
        return default


def _fl(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def enabled():
    return _on("NOBU_LIFECYCLE")


def _save(store):
    st = store() if callable(store) else store
    if st is not None and hasattr(st, "save"):
        st.save()


def _looks(c):
    return (list((c or {}).get("looks", [])) + [0] * 20)[:20]


def job_of(c):
    return _looks(c)[3]


# ---------------------------------------------------------------------------- PARAMS (ghost, spirit sight)
def sees_ghosts(c):
    """PARAMS[1] of the local player (0x1da540: 0 hides other ghosts). DESIGN: ghosts and the reviver jobs."""
    mode = os.environ.get("NOBU_LIFE_SPIRIT_SIGHT", "revivers").lower()
    if not enabled() or not c or mode == "none":
        return False
    if mode == "all" or c.get("ghost"):
        return True
    return mode == "revivers" and job_of(c) in REVIVER_JOBS


def overlay_params(c, params):
    """chars.char_params(c): PARAMS[0] = 2 for a ghost (P: the ghost state; other clients: the ghost flag),
    PARAMS[1] = 1 when the character sees ghosts."""
    p = bytearray(params)
    if not c or not enabled():
        return bytes(p)
    if c.get("ghost"):
        p[0] = 2
    if sees_ghosts(c):
        p[1] = 1
    return bytes(p)


def field_params(c):
    """The 15 PARAMS the character should have in the field now (move speed from the carried weight)."""
    from . import upkeep
    if upkeep.enabled():
        return upkeep.speed_params(upkeep.speed_class(c), c)
    return chars.char_params(c)


# ---------------------------------------------------------------------------- revive
def _cid(ctx):
    return ctx.get("cid") or (ctx.get("char") or {}).get("cid", 0)


def p_params(cid, params, seq=0):
    """2F 05 <seq> <u32 cid> 01 <PARAMS(15)> (0x284fd0 case 0x285548 -> UI 5 -> P vt+0x48; battle.p_params)."""
    return (vce.put_u8(0x2F) + vce.put_u8(5) + vce.put_u16(seq) + vce.put_u32(cid) + vce.put_u8(1)
            + chars.W().raw(bytes(params), 0xF).bytes())


def p_dead(dead, cause=0, seq=0):
    """2F 0B <seq> <u8 cause> <u8 dead> (0x285aa8 -> UI 0xeb): dead 0 clears P+0x10d94 ("Revived!")."""
    return vce.put_u8(0x2F) + vce.put_u8(0x0B) + vce.put_u16(seq) + vce.put_u8(cause) + vce.put_u8(1 if dead else 0)


def is_ghost(ctx):
    return bool(((ctx or {}).get("char") or {}).get("ghost"))


def revive(ctx, store=None, pct=None, how="revived"):
    """Bring the ghost of session ctx back where it stands. Returns the frames for ctx (2F 05 PARAMS[0] = 0,
    2F 0B "Revived!", 2F 08 Life / Ki); the other players of the zone get its new CHAR_INFO (players.refresh)."""
    from . import upkeep
    c = (ctx or {}).get("char")
    if not c or not c.get("ghost"):
        return []
    c.pop("ghost", None)
    out = [p_params(_cid(ctx), field_params(c)), p_dead(False, 0)]
    if upkeep.enabled():
        s = upkeep.state(c)
        mx = upkeep.maxima(c)[0]
        pct = _i("NOBU_LIFE_REVIVE_PCT", 25) if pct is None else pct
        s["life"] = max(1, s["life"], mx * max(1, min(100, pct)) // 100)
        out.append(upkeep.status_push(c))
    _save(store)
    ctx.setdefault("lifecycle_log", []).append(("revive", how))
    players.refresh(ctx)
    return out


def _dist(a, b):
    return math.hypot(a[1] - b[1], a[3] - b[3])


def _party_cids(ctx):
    """Real cids of the caster's party (party.PARTIES), empty without a party."""
    from . import party
    pid = party.party_of(_cid(ctx))
    return set(party.PARTIES.get(pid, {}).get("members", [])) if pid else set()


def revive_target(ctx):
    """The ghost session to revive: the 25 01 target when it is a ghost in the zone and in range, else the
    nearest ghost in range (party members first). None when there is none."""
    rng = _i("NOBU_LIFE_REVIVE_RANGE", 1000)
    me = players.position(ctx)
    ghosts = [o for o in players.peers(ctx) if is_ghost(o) and _dist(me, players.position(o)) <= rng]
    t = ctx.get("target")
    if t is not None and players.is_player_wire(t):
        o = players.session_by_cid(players.real_cid(t))
        if any(o is g for g in ghosts):
            return o
    if not ghosts:
        return None
    mates = _party_cids(ctx)
    return min(ghosts, key=lambda o: (o["char"]["cid"] not in mates, _dist(me, players.position(o))))


def skill_command(payload, ctx, store=None):
    """26 07 <seq> <u8 slot>: use the skill in that slot in the field. Returns the frames for the caster."""
    from . import upkeep
    seq = vce.Reader(payload, 2).u16()
    slot = payload[4] if len(payload) > 4 else 0xFF
    out = [bytes([0x26, 7]) + vce.put_u16(seq)]
    c = ctx["char"]
    sid = skills.bar(c).get(slot)
    s = skills.SKILLS.get(sid)
    if s is None or s["kind"] != "revive":
        return out + [social.notify_system("That skill cannot be used here.")]
    if c.get("ghost"):
        return out + [social.notify_system("A ghost cannot use skills.")]
    o = revive_target(ctx)
    if o is None:
        return out + [social.notify_system("There is no fallen ally nearby.")]
    if upkeep.enabled():
        st = upkeep.state(c)
        if st["ki"] < s["ki"]:
            return out + [social.notify_system(f"Not enough Ki ({st['ki']} / {s['ki']}).")]
        st["ki"] -= s["ki"]
        out.append(upkeep.status_push(c))
    name = f"{o['char'].get('family', '')} {o['char'].get('given', '')}".strip()
    frames = revive(o, store, pct=int(s["power"] * 100) if s.get("power") else None, how=f"skill {sid}")
    frames.append(social.notify_system(f"{c.get('family', '')} {c.get('given', '')}".strip()
                                       + f" revived you with 《{skills.name(sid)}》."))
    for f in frames:
        players.push(o, f)
    ctx.setdefault("lifecycle_log", []).append(("revived", o["char"]["cid"], sid))
    return out + [social.notify_system(f"You revived {name}.")]


# ---------------------------------------------------------------------------- the shrine (Nagoya)
def shrine_cid():
    cid = _i("NOBU_LIFE_SHRINE_CID", 0xFEA00013) & 0xFFFFFFFF
    return npc.check_npc_cid(cid, "NOBU_LIFE_SHRINE_CID")


def shrine_npc(player):
    lk = npc.npc_looks(player, "guide", job=3)          # a priest: job byte 3
    lk[5], lk[6], lk[7], lk[8] = 2, 4, 4, 45
    return dict(cid=shrine_cid(), uid=0, b6b=npc.TALK_ONLY, family="Shrine Keeper", given="", looks=lk,
                level=player.get("level", 1), role=SHRINE_ROLE,
                text="The spirits of the fallen find their way here. For an offering I can call one back.")


def shrine_position(player):
    """At the town's temple graveyard (town_spots.json role "shrine"), else spawn point + (600, 600). Either way out
    of the ghost's shrine range at the spawn point, so ○ there still Passes On. Without a spot the offsets are
    Nagoya's, from the town's spawn point, turned with its gate (npc.town_spot)."""
    return npc.town_spot(player, 600, 600, 225, role=SHRINE_ROLE)


def shrine_zone(zone):
    """A shrine stands in Nagoya (SHRINE_ZONE) and in every clan's home town (towns.py), where defeated players of
    that clan are carried."""
    return zone == SHRINE_ZONE or towns.is_town(zone)


def hall(c):
    """The town whose Hall Master a character reports to: its clan's home town (towns.py), else Nagoya."""
    return towns.name(chars.home_town(c) or towns.NAGOYA, "Nagoya")


def shrine_fee(c):
    return max(1, int((c or {}).get("level", 1) or 1)) * _i("NOBU_LIFE_SHRINE_FEE", 100)


def _near_shrine(ctx, rng=None):
    n = (ctx.get("npcs") or {}).get(shrine_cid())
    if not n:
        return None
    x, z = npc.player_xz(ctx)
    p = n["pos"]
    rng = _i("NOBU_TALK_RANGE", npc.TALK_RANGE) if rng is None else rng
    return n if math.hypot(p[1] - x, p[3] - z) <= rng else None


def shrine_revive(ctx, n, store=None):
    """○ as a ghost next to the shrine: pay the fee (money in the backpack) and come back with
    NOBU_LIFE_SHRINE_PCT % Life."""
    c = ctx["char"]
    inv = items.inventory(c, store)
    fee = shrine_fee(c)
    if int(inv.get("money", 0)) < fee:
        kan, mon = items.money_split(fee)
        return quests.say(n, f"The offering is {kan} kan {mon} mon; you do not carry enough.") + [
            social.notify_system("Walk away from the shrine and press ○ to Pass On.")]
    inv["money"] = int(inv["money"]) - fee
    items.save(store)
    kan, mon = items.money_split(fee)
    out = quests.say(n, "The gods have heard you. Return to the living.")
    out += revive(ctx, store, pct=_i("NOBU_LIFE_SHRINE_PCT", 50), how="shrine")
    out += [shops.notify_money(inv["money"]),
            social.notify_system(f"The shrine revived you for {kan} kan {mon} mon.")]
    return out


def shrine_talk(ctx, n, store=None):
    c = ctx["char"]
    kan, mon = items.money_split(shrine_fee(c))
    text = (f"Spirits of the fallen may come to me: for {kan} kan {mon} mon I call them back, and they keep "
            "their fame and purse.")
    if job_of(c) in REVIVER_JOBS:
        text += " Your calling can raise the fallen too, once you learn Revive."
    return quests.say(n, text)


# ---------------------------------------------------------------------------- stat growth on level-up
def growth_rates(c):
    """Per-level growth of the 9 stats: NOBU_LIFE_GROWTH_JOB x the job's creation base (chars.JOB_BASE) +
    NOBU_LIFE_GROWTH_BONUS x the creation points the player put into the stat."""
    job = job_of(c)
    jb = chars.JOB_BASE.get(job, chars.DEFAULT_JOB_BASE)
    fb = chars.FACTION_BASE.get(_looks(c)[4], chars.DEFAULT_FACTION_BASE)
    base = stats_base(c)
    kj, kb = _fl("NOBU_LIFE_GROWTH_JOB", 0.1), _fl("NOBU_LIFE_GROWTH_BONUS", 0.05)
    return [kj * jb[i] + kb * max(0, base[i] - jb[i] - fb[i]) for i in range(9)]


def stats_base(c):
    """The creation stats (looks[11:20] before any growth), remembered in c["stat_growth"]["base"]."""
    g = c.setdefault("stat_growth", {})
    if not isinstance(g.get("base"), list) or len(g["base"]) != 9:
        g["base"] = _looks(c)[11:20]
    return list(g["base"])


def stats_at(c, level):
    base, rates = stats_base(c), growth_rates(c)
    return [min(0xFFFF, base[i] + int(math.floor((max(1, level) - 1) * rates[i] + 1e-9))) for i in range(9)]


def grow(c):
    """Set looks[11:20] to the stats of the character's level. Returns [(index, old, new)] changed."""
    if not c or not enabled() or not _on("NOBU_LIFE_GROWTH") or not c.get("looks"):
        return []
    lk = list(c["looks"]) + [0] * max(0, 20 - len(c["looks"]))
    new = stats_at(c, int(c.get("level", 1) or 1))
    changed = [(i, lk[11 + i], new[i]) for i in range(9) if lk[11 + i] != new[i]]
    if changed:
        lk[11:20] = new
        c["looks"] = lk
        c["stat_growth"]["level"] = int(c.get("level", 1) or 1)
    return changed


def growth_frames(ctx):
    """progression.flush hook: grow, then a 2F 07 (the Status rows, Life / Ki maxima) and a line queued with
    the level-up lines."""
    c = (ctx or {}).get("char")
    changed = grow(c)
    if not changed:
        return []
    parts = [f"{STAT_NAMES[i]} {new} ({'+' if new >= old else ''}{new - old})" for i, old, new in changed]
    ctx.setdefault("progress_pending", []).append("Your stats grew: " + ", ".join(parts) + ".")
    return [skills.status_push(c)]


# ---------------------------------------------------------------------------- promotion exams
def exams_enabled():
    return enabled() and _on("NOBU_LIFE_EXAMS")


def exam_due(c):
    """The exam dict whose merit is reached and whose rank is the next step, else None."""
    if not c or not exams_enabled():
        return None
    nxt = progression.next_rank(c)
    if not nxt or not nxt[2] or nxt[0] not in EXAMS:
        return None
    if int(c.get("merit", 0)) < nxt[1] or int(c.get("level", 1) or 1) < progression.ASHIGARU_LEVEL:
        return None
    return EXAMS[nxt[0]]


def exam_active(c):
    e = (c or {}).get("exam") or {}
    return EXAMS.get(e.get("rank"))


def exam_progress(c, exam, store=None):
    need = int(exam["count"])
    if exam["kind"] == "kill":
        have = int((c.get("exam") or {}).get("kills", 0))
    else:
        from . import guild
        have = guild.item_count(c, int(exam["item"]), store)
    return min(have, need), need


def exam_entry(c, exam, store=None):
    have, need = exam_progress(c, exam, store)
    verb = "defeated" if exam["kind"] == "kill" else "in your pack"
    done = " Done: report to the Hall Master." if have >= need else ""
    return quests.entry(exam["id"], "[Exam] " + exam["title"], f"[{have}/{need} {verb}]{done} {exam['text']}")


def _count_kill(ctx, kind, store=None):
    c = ctx.get("char")
    exam = exam_active(c)
    if not kind or exam is None or exam["kind"] != "kill" or kind not in exam["enemies"]:
        return []
    e = c["exam"]
    if int(e.get("kills", 0)) >= int(exam["count"]):
        return []
    e["kills"] = int(e.get("kills", 0)) + 1
    _save(store)
    line = f"{exam['title']}: {e['kills']}/{exam['count']} {exam['target']} defeated."
    if e["kills"] >= int(exam["count"]):
        line += f" Report to the Hall Master in {hall(c)}."
    return [quests.quest_update(exam_entry(c, exam, store)), social.notify_system(line)]


def sync_log(ctx, store=None, skip_cid=None):
    """Kills from ctx["enemies_log"] (enemies.on_defeated) since the last look (own counter)."""
    log = ctx.get("enemies_log") or []
    seen = int(ctx.get("exam_log_seen", 0))
    ctx["exam_log_seen"] = len(log)
    out = []
    for ent in log[seen:]:
        if len(ent) >= 3 and ent[0] == "defeated" and ent[1] != skip_cid:
            out += _count_kill(ctx, ent[2], store)
    return out


def on_kill(ctx, enemy, store=None):
    """Battle hook (battle.finish after a win), like guild.on_kill."""
    if not exams_enabled() or not (ctx or {}).get("char"):
        return []
    from . import guild
    cid = enemy if isinstance(enemy, int) else None
    log = ctx.get("enemies_log") or []
    logged = any(len(e) >= 2 and e[0] == "defeated" and e[1] == cid for e in log[int(ctx.get("exam_log_seen", 0)):])
    return sync_log(ctx, store, skip_cid=cid if logged else None) + _count_kill(ctx, guild._kind(enemy), store)


def take_exam(ctx, exam, n=None, store=None):
    c = ctx["char"]
    c["exam"] = dict(rank=exam["rank"], kills=0, taken=dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    _save(store)
    out = quests.say(n, exam["offer"]) if n is not None else []
    out.append(quests.quest_add(exam_entry(c, exam, store)))
    out.append(social.notify_system(f"Promotion exam: {exam['title']}."))
    return out


def pass_exam(ctx, exam, n, store=None):
    from . import guild
    c = ctx["char"]
    out = []
    if exam["kind"] == "bring":
        changed = guild.take_items(c, int(exam["item"]), int(exam["count"]), store)
        if changed is None:
            return quests.say(n, "You do not have everything yet.")
        out += shops.item_pushes(changed, sold=True)
    c.pop("exam", None)
    old = int(c.get("rank", 0))
    c["rank"] = max(old, int(exam["rank"]))
    c.pop("exam_ready", None)
    _save(store)
    faction = progression.faction_of(c)
    ctx.setdefault("progress_pending", []).extend(progression.rank_lines([c["rank"]], faction))
    ctx.setdefault("lifecycle_log", []).append(("exam passed", exam["id"]))
    return (quests.say(n, f"You passed. From today you serve as {progression.rank_name(c['rank'], faction)}.")
            + [quests.quest_remove(exam["id"]), social.notify_system(f"Exam passed: {exam['title']}.")]
            + out + progression.flush(ctx))


def exam_talk(ctx, n, store=None):
    """guild.talk hook (○ on the Hall Master): the active exam (report / reminder), else a due exam when no guild
    job is active. None = the normal job talk."""
    c = (ctx or {}).get("char")
    if not c or not exams_enabled():
        return None
    out = sync_log(ctx, store)
    exam = exam_active(c)
    if exam is not None:
        have, need = exam_progress(c, exam, store)
        if have >= need:
            return out + pass_exam(ctx, exam, n, store)
        return out + quests.say(n, f"{exam['title']}: {have}/{need}. {exam['text']}")
    exam = exam_due(c)
    if exam is None or (c.get("guild") or {}).get("job"):
        return None
    return out + take_exam(ctx, exam, n, store)


# ---------------------------------------------------------------------------- hooks / dispatch
def world_ready(ctx, store=None):
    """23 5a (after guild.world_ready): the shrine (Nagoya and every home town), the active exam's quest entry,
    logged kills."""
    c = (ctx or {}).get("char")
    if not c or not enabled():
        return []
    out = []
    zone = ctx.get("zone", chars.char_position(c)[0])
    if _on("NOBU_LIFE_SHRINE") and shrine_zone(zone):
        n = shrine_npc(c)
        pos = shrine_position(c)
        n["pos"] = list(pos)
        ctx.setdefault("npcs", {})[n["cid"]] = n
        out += npc.spawn_pushes(n, pos)
    exam = exam_active(c)
    if exam is not None and exams_enabled():
        out.append(quests.quest_add(exam_entry(c, exam, store)))
        out += sync_log(ctx, store)
    return out


def _schedule_refresh(ctx):
    """First death: battle.ghost_frames sets c["ghost"] NOBU_BATTLE_DEATH_DELAY s after this 23 5a, after the
    spawn showed us to the zone; re-send our CHAR_INFO to the others once it is set."""
    from . import battle
    delay = _fl("NOBU_BATTLE_DEATH_DELAY", 1.0) + 0.5
    battle.schedule(ctx, delay, lambda: (players.refresh(ctx), [])[1])


def _chat(payload):
    try:
        d = social.decode_chat(payload)
    except vce.WireError:
        return None, None
    t = unicodedata.normalize("NFKC", d["text"] or "").strip().lower().lstrip("/!").split()
    return d, t


def chat(payload, ctx, store=None):
    d, t = _chat(payload)
    if not t or t[0] not in ("exam", "stats"):
        return None
    c = ctx["char"]
    out = social.chat_replies(d, ctx) + sync_log(ctx, store)
    if t[0] == "stats":
        grown = growth_frames(ctx)
        out += grown + [social.notify_system(t_) for t_ in ctx.pop("progress_pending", [])]
        lk = _looks(c)
        out.append(social.notify_system(" ".join(f"{STAT_NAMES[i]} {lk[11 + i]}" for i in range(9))
                                        + f" (Lv {c.get('level', 1)})."))
        return out
    exam = exam_active(c)
    if len(t) > 1 and t[1] in ("cancel", "quit") and exam is not None:
        c.pop("exam", None)
        _save(store)
        return out + [quests.quest_remove(exam["id"]), social.notify_system(f"Exam given up: {exam['title']}.")]
    if exam is not None:
        have, need = exam_progress(c, exam, store)
        return out + [social.notify_system(f"{exam['title']}: {have}/{need}. {exam['text']}")]
    due = exam_due(c)
    if due is not None:
        return out + [social.notify_system(f"The {progression.rank_label(due['rank'], progression.faction_of(c))}"
                                           f" exam is open: talk to the Hall Master in {hall(c)}.")]
    return out + [social.notify_system("No promotion exam is open for you.")]


def answer(payload, ctx, store=None):
    """Called before battle.answer: 26 07 (field skill), 28 01 at the shrine, 2a 03 on an exam entry, chat
    "exam" / "stats"; 23 5a only schedules the ghost refresh. None for everything else."""
    if not enabled() or len(payload) < 4 or not (ctx or {}).get("char"):
        return None
    cmd, sub = payload[0], payload[1]
    c = ctx["char"]
    if cmd == 0x23 and sub == 0x5A:
        if ctx.get("defeat_pending") and not c.get("ghost") and players.enabled():
            _schedule_refresh(ctx)
        return None
    if cmd == 0x26 and sub == 7 and skills.enabled():
        return skill_command(payload, ctx, store)
    if cmd == 0x28 and sub == 1 and _on("NOBU_LIFE_SHRINE"):
        if c.get("ghost"):                             # ○ is always Pass On for a ghost (hint 8, 0x62d7d0)
            n = _near_shrine(ctx, _i("NOBU_LIFE_SHRINE_RANGE", 300))
            return shrine_revive(ctx, n, store) if n is not None else None
        n = _near_shrine(ctx)
        if n is not None and ctx.get("target") == n["cid"]:
            return shrine_talk(ctx, n, store)
        return None
    if cmd == 0x2A and sub == 3 and exams_enabled():
        d = quests.decode_detail_request(payload)
        if d["id"] not in EXAM_IDS:
            return None
        lines = sync_log(ctx, store)
        exam = exam_active(c)
        if exam is not None and exam["id"] == d["id"]:
            return [quests.quest_add(exam_entry(c, exam, store), d["seq"])] + lines
        return [quests.quest_remove(d["id"], d["seq"])] + lines
    if cmd == 0x23 and sub == 0x08 and _on("NOBU_LIFE_CHAT"):
        return chat(payload, ctx, store)
    return None


def describe(payload, direction="C>S"):
    if direction == "C>S" and len(payload) >= 5 and payload[:2] == b"\x26\x07":
        return f"SKILL COMMAND (field use, lifecycle.py) seq={vce.Reader(payload, 2).u16()} slot={payload[4]}"
    return None
