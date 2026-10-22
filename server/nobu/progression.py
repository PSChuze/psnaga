"""Character progression: level, EXP, proficiency, fame, merit, rank, and the pushes that show them.

KNOWN:
- S>C cmd 0x2F (parser 0x284fd0: u8 sub, u16 seq -> 0x1c6c00, then one archive per sub; main handler
  0x286cb0 = a switch on sub that posts a UI event with the struct):
    2F 0D  0x1f21e0: u8 level, u8 exp%, u16 w, u32 exp, u32 next  -> UI 0x4b -> 0x209620(P):
           P+0x2f = level, P+0x102c8 = exp%, P+0x10c68 = w, P+0x102cc = exp, P+0x102d0 = next;
           then 0x1fc400(UI+0x336d4, 0xb7, P) = window refresh.
    2F 0C  0x1ebde0: u8 faction, u8 rank, u16 fame, u16  -> UI 3 -> 0x207ef0(P):
           P+0x30 = faction, P+0x102c6 = rank, P+0x102c2 = fame (the last u16 is not stored).
    2F 0E  0x1f20d0: u8 rank, u8 b, u16 merit  -> UI 0x4a -> 0x2095e0(P):
           P+0x102c6 = rank, P+0x102c7 = b, P+0x102d4 = merit.
  (P = UI object 0x4b8550 + 0x2c = 0x4b857c, the local player.)
- The Status window (World.bin ctor 0x66e240, label group UIMSG 2.14) reads those fields every frame
  through callbacks: Exp row = two numbers UI+0x102f8 / UI+0x102fc (= P+0x102cc exp / P+0x102d0 next,
  field 0x38f5b0) plus a gauge (0x38fa10, max 100) fed by 0x20ba40(P) = P+0x102c8, or 100 once the level
  (P+0x2f) reaches the cap: 60 if 0x1880d0(net, 1) else 50. Fame row = P+0x102c2, Merit row = P+0x102d4,
  PvP-rating row (2.14.21) = P+0x10c68. The 9 stat rows = P+0x10294..0x102a4 (set only by the load path).
- Level also comes from CHAR_LOCATION / CHAR_INFO +0x3e (-> P+0x2f) and the character list +0x22.
- No level-up popup exists for the field: UIMSG 0.648.46 "LV went up" is a canned chat phrase (group 0x288,
  used by the phrase menu 0x2766f0 / 0x27bf08); UIMSG 3.27 "Level Up!" and 3.20 have no reference in the
  Battle.bin results window (block-3 getter 0x62a0e0 is called for every other row label of 3.18..3.28).
  The level-up notice is therefore a server system line (0D 06) here.
- No bonus-point allocation after creation: UIMSG 1.54 / 1.56 are creation-screen labels (block 1).

- Rank row of the Status window (ctor 0x66e8d8, refresh 0x66f500) = 0x209f00(P) = 0x273d80(rank
  P+0x102c6, faction P+0x30): rank < 13 -> WDMMSG 0.150[rank] (faction != 0) or 0.151[rank] (faction 0:
  the ronin names), else 0.153.0 "Unknown". So the rank byte of 2F 0C / 2F 0E is the WDMMSG 0.150 index:
  0 Mononofu, 1 Ashigaru, 2 Ashigaru Capt, 3 Ashigaru Gen., 4 Yoriki, 5 Samurai Gen., 6 Metsuke, 7
  Magistrate, 8 Churo, 9 Karo, 10 Elder. The Merit row reads P+0x102d4 as a u16 (0x66dc20). The 2F
  0E byte b (P+0x102c7) has no reader found.

HYPOTHESIS: the EXP table (the client has none: it only shows what 2F 0D sends), the meaning of "next"
(sent as the total EXP where the next level starts), the u16 w (PvP rating, from its row). Rank ladder
(FAQ04, WP rankSystem): Ashigaru at Lv 5, Ashigaru Capt at 350 merit, Ashigaru Gen. at 900, Yoriki at
1800 + exam, Samurai Gen. at 3500 + exam. The exams are quests from the Hall Master (lifecycle.py).

Knobs: NOBU_LEVEL_CAP (default 60), NOBU_EXP_BASE (30) / NOBU_EXP_POW (1.8) for the table,
NOBU_PROG_PUSH=0 (no 2F pushes), NOBU_PROG_FAME_PUSH=0 (no 2F 0C), NOBU_PROG_MERIT_PUSH=0 (no 2F 0E),
NOBU_PROG_CHAT=0 (no "exp" / "level" chat commands).
"""
import os
import unicodedata

from . import items, social, towns, vce

CMD = 0x2F
SUB_LEVEL, SUB_FAME, SUB_MERIT = 0x0D, 0x0C, 0x0E


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------- the EXP table (HYPOTHESIS)
def level_cap():
    """60 with the Hiryu flag (0x20ba40 picks 60 / 50 from 0x1880d0(net, 1)); this disc is Hiryu."""
    return max(1, min(255, _env_int("NOBU_LEVEL_CAP", 60)))


def need(level):
    """EXP from the start of `level` to the start of level + 1. INVENTED: base * level ** pow
    (30, 104, 217, ... 46,201 at 59; 996,747 in total to 60). The battle reward is 10 per Bandit."""
    return max(1, int(round(_env_float("NOBU_EXP_BASE", 30) * level ** _env_float("NOBU_EXP_POW", 1.8))))


def threshold(level):
    """Total EXP at which `level` starts (level 1 = 0)."""
    return sum(need(k) for k in range(1, max(1, level)))


def level_for(exp):
    lv = 1
    while lv < level_cap() and exp >= threshold(lv + 1):
        lv += 1
    return lv


def exp_percent(exp, level):
    """Gauge 0..100 inside the current level (the client shows 100 by itself at the cap)."""
    if level >= level_cap():
        return 100
    lo, hi = threshold(level), threshold(level + 1)
    return max(0, min(99, (exp - lo) * 100 // max(1, hi - lo)))


def status(c):
    """Level / EXP numbers for the 2F 0D push."""
    exp = int(c.get("exp", 0))
    lv = int(c.get("level", 1)) or 1
    nxt = threshold(lv + 1) if lv < level_cap() else exp
    return dict(level=lv, pct=exp_percent(exp, lv), exp=exp, next=nxt, w=int(c.get("pvp", 0)) & 0xFFFF)


# ---------------------------------------------------------------------------- rank and merit rank byte
# = WDMMSG 0.150 index (KNOWN, 0x273d80); the English names follow wdmmsg.tsv.
RANK_NAMES = ["Mononofu", "Ashigaru", "Ashigaru Captain", "Ashigaru General", "Yoriki", "Samurai General",
              "Metsuke", "Magistrate", "Churo", "Karo", "Elder"]
RONIN_NAMES = ["Mononofu", "Ronin", "Rogue", "Landless", "Kokujin", "Local Lord", "Gentry", "Patriot", "Hero",
               "Dignitary", "Notable"]   # WDMMSG 0.151 (faction 0)
MERIT_CAP = 0xFFFF                   # u16 on the wire and in P+0x102d4; FAQ04 cap 65535
ASHIGARU_LEVEL = 5
# (rank, merit needed, exam needed), PLAYABILITY 1.4. Rank 1 needs only the level.
RANK_LADDER = [(1, 0, False), (2, 350, False), (3, 900, False), (4, 1800, True), (5, 3500, True)]


def faction_of(c):
    return (list((c or {}).get("looks", [])) + [0] * 5)[4]


def rank_name(rank, faction=1, jp=False):
    """The name the Status window shows for this rank byte (0x273d80: 0.150 or, for faction 0, 0.151); English
    (wdmmsg.tsv) unless jp, then the disc's Japanese."""
    table = RANK_NAMES if faction else RONIN_NAMES
    if not 0 <= rank < len(table):
        return "" if jp else "Unknown"
    if jp:
        return items.tsv_jp("wdmmsg.tsv", "0.%d.%d" % (150 if faction else 151, rank))
    return table[rank]


def rank_label(rank, faction=1):
    """The rank as server text names it (English only: the patched client shows English everywhere)."""
    return rank_name(rank, faction)


def rank_for(c):
    """Highest rank the level and merit reach without an exam; never below the stored rank (an exam rank or
    an admin edit stays)."""
    have = int(c.get("rank", 0))
    best = 0
    if int(c.get("level", 1) or 1) >= ASHIGARU_LEVEL:
        for rank, need, exam in RANK_LADDER:
            if exam or int(c.get("merit", 0)) < need:
                break
            best = rank
    return max(have, best)


def next_rank(c):
    """(rank, merit needed, exam needed) of the next step above the stored rank, or None at the top."""
    have = int(c.get("rank", 0))
    return next((step for step in RANK_LADDER if step[0] > have), None)


def update_rank(c):
    """Promote c as far as rank_for allows. Returns the ranks reached (ascending), plus -r once when the merit
    for exam rank r is reached (c["exam_ready"] = r remembers that the note was given)."""
    old = int(c.get("rank", 0))
    new = rank_for(c)
    out = list(range(old + 1, new + 1))
    c["rank"] = new
    nxt = next_rank(c)
    if (nxt and nxt[2] and int(c.get("merit", 0)) >= nxt[1] and c.get("exam_ready") != nxt[0]
            and int(c.get("level", 1) or 1) >= ASHIGARU_LEVEL):
        c["exam_ready"] = nxt[0]
        out.append(-nxt[0])
    return out


def add_merit(c, merit):
    c["merit"] = max(0, min(MERIT_CAP, int(c.get("merit", 0)) + int(merit)))
    return c["merit"]


def rank_lines(ranks, faction=1):
    out = []
    for r in ranks:
        if r > 0:
            out.append(f"Promotion! Your rank is now {rank_label(r, faction)}.")
        else:
            hall = towns.name(towns.home_town(faction) or towns.NAGOYA, "Nagoya")   # the clan's home town
            out.append(f"Your merit qualifies you for the {rank_label(-r, faction)} promotion exam: "
                       f"talk to the Hall Master in {hall}.")
    return out


# ---------------------------------------------------------------------------- award
def award(c, exp=0, prof=0, fame=0, merit=0):
    """Add rewards to the character dict (chars.json keys exp / prof / fame / merit / level / rank), level it
    up and promote it (update_rank). Returns dict(old, new, levels=[each level reached], exp, prof, fame,
    merit, ranks=[update_rank result], faction). Fame and merit are capped at 65535 (FAQ04)."""
    old = int(c.get("level", 1)) or 1
    c["exp"] = max(0, int(c.get("exp", 0)) + int(exp))
    c["prof"] = max(0, int(c.get("prof", 0)) + int(prof))
    c["fame"] = max(0, min(0xFFFF, int(c.get("fame", 0)) + int(fame)))
    if merit:
        add_merit(c, merit)
    new = max(old, level_for(c["exp"]))                  # never lose a level
    c["level"] = new
    ranks = update_rank(c)
    from . import hiryu                                   # Lv 30+: Prof. also feeds potential mastery (hiryu.py)
    pot = hiryu.pot_add_mastery(c, prof)
    return dict(old=old, new=new, levels=list(range(old + 1, new + 1)), exp=c["exp"], prof=c["prof"],
                fame=c["fame"], merit=int(c.get("merit", 0)), ranks=ranks, faction=faction_of(c), pot=pot)


def level_up_lines(res):
    return ([f"Level up! You are now level {lv}." for lv in res["levels"]]
            + rank_lines(res.get("ranks") or [], res.get("faction", 1)))


def queue(ctx, res):
    """Remember the level-up / promotion lines for the next flush (after the battle, back in the field)."""
    if ctx is not None and res and (res["levels"] or res.get("ranks")):
        ctx.setdefault("progress_pending", []).extend(level_up_lines(res))
        from . import hiryu                               # "you can specialise now" at the unlock level
        note = hiryu.level_note(ctx.get("char") or {}, res["old"], res["new"])
        if note:
            ctx["progress_pending"].append(note)
    if ctx is not None and res and res.get("pot"):
        ctx.setdefault("progress_pending", []).append(
            f"Potential level up! (+{res['pot'] * 10} potential points)")


def award_ctx(ctx, exp=0, prof=0, fame=0, store=None, merit=0):
    """award() on ctx["char"], queue the level-up / promotion lines and save the store. None without a
    character."""
    c = (ctx or {}).get("char")
    if not c:
        return None
    res = award(c, exp, prof, fame, merit)
    queue(ctx, res)
    _save(store)
    return res


def _save(store):
    st = store() if callable(store) else store
    if st is not None and hasattr(st, "save"):
        st.save()


# ---------------------------------------------------------------------------- pushes
def _hdr(sub, seq=0):
    return vce.put_u8(CMD) + vce.put_u8(sub) + vce.put_u16(seq)


def push_level(level, pct, exp, nxt, w=0, seq=0):
    """2F 0D: u8 level, u8 exp%, u16 w, u32 exp, u32 next (0x1f21e0 -> UI 0x4b -> 0x209620)."""
    return (_hdr(SUB_LEVEL, seq) + vce.put_u8(level & 0xFF) + vce.put_u8(pct & 0xFF) + vce.put_u16(w & 0xFFFF)
            + vce.put_u32(exp & 0xFFFFFFFF) + vce.put_u32(nxt & 0xFFFFFFFF))


def push_fame(faction, rank, fame, extra=0, seq=0):
    """2F 0C: u8 faction, u8 rank, u16 fame, u16 (0x1ebde0 -> UI 3 -> 0x207ef0; sets P+0x30 faction too)."""
    return (_hdr(SUB_FAME, seq) + vce.put_u8(faction & 0xFF) + vce.put_u8(rank & 0xFF)
            + vce.put_u16(fame & 0xFFFF) + vce.put_u16(extra & 0xFFFF))


def push_merit(rank, b, merit, seq=0):
    """2F 0E: u8 rank, u8 b, u16 merit (0x1f20d0 -> UI 0x4a -> 0x2095e0)."""
    return _hdr(SUB_MERIT, seq) + vce.put_u8(rank & 0xFF) + vce.put_u8(b & 0xFF) + vce.put_u16(merit & 0xFFFF)


def status_pushes(c):
    """The 2F frames that make the Status window show this character's numbers."""
    if not c or os.environ.get("NOBU_PROG_PUSH", "1") != "1":
        return []
    s = status(c)
    out = [push_level(s["level"], s["pct"], s["exp"], s["next"], s["w"])]
    if os.environ.get("NOBU_PROG_FAME_PUSH", "1") == "1":
        out.append(push_fame(faction_of(c), int(c.get("rank", 0)), int(c.get("fame", 0))))
    if os.environ.get("NOBU_PROG_MERIT_PUSH", "1") == "1":        # rank + merit rows (0x2095e0)
        out.append(push_merit(int(c.get("rank", 0)), 0, int(c.get("merit", 0))))
    return out


def flush(ctx):
    """Status pushes + the queued level-up / promotion lines (0D 06 system lines). Call when the player is in
    the field: world_ready (23 5a, also sent again after a battle) and right after the battle exit. Promotes
    first (update_rank), for a level reached through a path that did not call award()."""
    c = (ctx or {}).get("char")
    if not c:
        return []
    ranks = update_rank(c)
    if ranks:
        ctx.setdefault("progress_pending", []).extend(rank_lines(ranks, faction_of(c)))
    from . import lifecycle, hiryu
    out = status_pushes(c) + lifecycle.growth_frames(ctx)   # stat growth: 2F 07 + a line (lifecycle.py)
    out += hiryu.pot_world_ready(ctx)                    # 2F 1E potential header (level >= 30 only)
    out += [social.notify_system(t) for t in ctx.pop("progress_pending", [])]
    return out


def world_ready(ctx):
    """flush() for a character that has progress (any exp / prof / fame / merit / rank / level > 1) or queued
    lines. A new character gets nothing, so the spawn burst is unchanged for it (the client's fields stay 0 =
    level 1, rank 0 Mononofu, merit 0)."""
    c = (ctx or {}).get("char") or {}
    if (ctx or {}).get("progress_pending") or any(c.get(k) for k in ("exp", "prof", "fame", "merit", "rank")) \
            or int(c.get("level", 1) or 1) > 1:
        return flush(ctx)
    return []


# ---------------------------------------------------------------------------- chat commands
def _parse(text):
    t = unicodedata.normalize("NFKC", text or "").strip().lower().lstrip("/!").strip()
    toks = t.split()
    if not toks or toks[0] not in ("exp", "level", "lv"):
        return None
    if toks[0] == "exp" and len(toks) > 1:
        try:
            return "add", int(toks[1])
        except ValueError:
            return None
    return "show", 0


def answer(payload, ctx, store=None):
    """Field chat "exp <n>" (debug: award n EXP) and "level" / "exp" (show). None when not ours."""
    if os.environ.get("NOBU_PROG_CHAT", "1") != "1" or len(payload) < 4 or payload[:2] != b"\x23\x08":
        return None
    c = (ctx or {}).get("char")
    if not c:
        return None
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    _, text = social.parse_line(line)
    got = _parse(text)
    if got is None:
        return None
    if got[0] == "add":
        award_ctx(ctx, exp=got[1], store=store)
    out = flush(ctx)
    s = status(c)
    note = (f"Level {s['level']}  Exp {s['exp']} / {s['next']} ({s['pct']}%)  Prof. {c.get('prof', 0)}"
            f"  Fame {c.get('fame', 0)}  Rank {rank_name(int(c.get('rank', 0)), faction_of(c), jp=False)}"
            f"  Merit {c.get('merit', 0)}")
    return out + [social.notify_system(note)]


def describe(payload, direction="C>S"):
    if direction != "S>C" or len(payload) < 4 or payload[0] != CMD:
        return None
    sub = payload[1]
    try:
        r = vce.Reader(payload, 4)
        if sub == SUB_LEVEL:
            return (f"PROGRESS 2F 0D level={r.u8()} pct={r.u8()} w={r.u16()} exp={r.u32()} next={r.u32()}")
        if sub == SUB_FAME:
            return f"PROGRESS 2F 0C faction={r.u8()} rank={r.u8()} fame={r.u16()} x={r.u16()}"
        if sub == SUB_MERIT:
            return f"PROGRESS 2F 0E rank={r.u8()} b={r.u8()} merit={r.u16()}"
    except vce.WireError as e:
        return f"PROGRESS 2F {sub:02x} (malformed: {e})"
    return f"cmd 0x2f sub {sub:#04x} {payload[4:].hex()}"
