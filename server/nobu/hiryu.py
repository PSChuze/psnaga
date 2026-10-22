"""Hiryu systems: specialisation now; styles, potential, court ranks, fiefs and trials are designed. Houses live
in house.py.

Specialisation, what the client proves:
- 21 scroll lines = STRDAT 8 rows 1..21, three per job in job order (Bushido Martial Tactics / Sohei
  Buddhism Esoteric / ...). A book's line is B+0x1a6 (the trailing u8 of the 26 04 reply, 0x202e90); the
  scroll Details shows it as "<line>family" (UIMSG 0.371, 0x66c024) and so does the item window
  (0x36c808, item +0x2a2).
- The title shown for the local player is P+0xa1 = CHAR_INFO record +0x6a (0x208140 / vt 0x74): P vt 0x24
  (0x1db320) -> 0x274010(title, female) -> WDMMSG 0.147[title - 1] (0.148 for the female forms). Titles 1..7 are
  the jobs, 8..28 the 21 specialised titles (Swordsman ...), 29..49 a second tier (Duelist ...), 50..56 one top
  title per job (Kensei ...). The job byte P+0x2d itself stays the base job (the HUD 0x342220 only names 1..10).
- Buying a scroll: shop goods +6 -> item +0xaa carries the refusal bits the client turns into messages
  (0x2003d0 -> table 0x432b70): 0x10 "your job cannot learn this" (0.225), 0x20 prerequisite not mastered
  (0.226), 0x80 "not your specialisation line" (0.228, tested before 0x40), 0x40 already learned (0.227),
  0x08 level too low (0.229); 0x02 / 0x04 are warnings (cannot equip / fame).
Period rules (official patch notes 2004-06-30): every new combat scroll is a specialisation scroll; the first
one learned fixes the line; the NPC can erase the line (all line scrolls are lost). The 2004 line scrolls are
the books of STRDAT 2 232..244 / 286..315 (items 4140..4182). DESIGN: the unlock level (NOBU_SPEC_LEVEL, 40 =
the wiki's tier- level), the title pairing and tier levels, the debug chat commands `spec` / `spec <line>` /
`spec reset` / `spec scroll <line> [tier]`.
"""
import os
import unicodedata

from . import items, social, vce

# (line, en, job, [(scroll id, item id), ...] tier) - STRDAT 8 / 2 / 0; the Japanese name is STRDAT 8 row <line>
LINES = [
    (1, "Bushido", 1, [(232, 4140), (233, 4141)]),
    (2, "Martial Arts", 1, [(234, 4142), (235, 4143)]),
    (3, "Tactics", 1, [(236, 4144), (237, 4145)]),
    (4, "Warrior Monk", 2, [(238, 4146), (239, 4147)]),
    (5, "Buddhism", 2, [(240, 4148), (241, 4149)]),
    (6, "Esoteric", 2, [(242, 4150), (243, 4151)]),
    (7, "Shinto", 3, [(244, 4152), (286, 4153)]),
    (8, "Old Shinto", 3, [(287, 4154), (288, 4155)]),
    (9, "Gagaku", 3, [(289, 4156), (290, 4157)]),
    (10, "Onmyodo", 4, [(291, 4158), (292, 4159), (293, 4160)]),
    (11, "Sendo", 4, [(294, 4161), (295, 4162)]),
    (12, "Summoning", 4, [(296, 4163), (297, 4164)]),
    (13, "Ninpo", 5, [(298, 4165), (299, 4166)]),
    (14, "Assassination", 5, [(300, 4167), (301, 4168)]),
    (15, "Ninjutsu", 5, [(302, 4169), (303, 4170)]),
    (16, "Swordsmith", 6, [(304, 4171), (305, 4172)]),
    (17, "Armoursmith", 6, [(306, 4173), (307, 4174)]),
    (18, "Gunsmith", 6, [(308, 4175), (309, 4176)]),
    (19, "Medicine", 7, [(310, 4177), (311, 4178)]),
    (20, "Mysticism", 7, [(312, 4179), (313, 4180)]),
    (21, "Shugendo", 7, [(314, 4181), (315, 4182)]),
]
LINE = {ln[0]: ln for ln in LINES}
SCROLL_LINE = {s: ln[0] for ln in LINES for s, _ in ln[3]}
ITEM_SCROLL = {i: s for ln in LINES for s, i in ln[3]}
SCROLL_ITEM = {s: i for i, s in ITEM_SCROLL.items()}
# DESIGN (no period source pairs them): which of the job's three titles (WDMMSG 0.147 order) each line gives,
# matched by meaning where one is obvious (Martial -> Swordsman, Tactics -> Tactician, Gagaku -> Musician,
# Summoning -> Conjurer ...).
TITLE_SLOT = {1: 2, 2: 0, 3: 1, 4: 1, 5: 0, 6: 2, 7: 0, 8: 2, 9: 1, 10: 0, 11: 2, 12: 1,
              13: 2, 14: 0, 15: 1, 16: 0, 17: 1, 18: 2, 19: 0, 20: 1, 21: 2}
JOBS_EN = {1: "Samurai", 2: "Monk", 3: "Priest", 4: "Onmyoji", 5: "Ninja", 6: "Smith", 7: "Physician"}  # UIMSG 0.444
T_SCROLL = 82                     # STRDAT 3 row 82 Skill Scroll
# shop goods +6 / item +0xaa refusal bits (0x2003d0)
F_JOB, F_PREREQ, F_LEARNED, F_LEVEL, F_LINE = 0x10, 0x20, 0x40, 0x08, 0x80
REFUSAL_TEXT = {F_JOB: "Your job cannot learn this.", F_PREREQ: "Required scroll not mastered. Cannot learn.",
                F_LINE: "This scroll is not in your specialization line. Cannot learn.",
                F_LEARNED: "Skill scroll already learned.", F_LEVEL: "Required level not reached. Cannot learn."}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def enabled():
    return os.environ.get("NOBU_HIRYU", "1") == "1"


def spec_level():
    return _env_int("NOBU_SPEC_LEVEL", 40)


def job_of(c):
    return (list((c or {}).get("looks", [])) + [0] * 5)[3]


def level_of(c):
    return int((c or {}).get("level", 1) or 1)


def lines_of_job(job):
    return [ln for ln in LINES if ln[2] == job]


def scroll_line(scroll):
    """STRDAT 8 row of a scroll (0 = no line); the 26 04 reply's trailing byte (B+0x1a6)."""
    return SCROLL_LINE.get(int(scroll or 0), 0)


def scroll_label(scroll):
    from . import skills
    iid = SCROLL_ITEM.get(int(scroll))
    return items.item_name(iid) if iid else skills.scroll_name(scroll)


def line_label(line):
    ln = LINE.get(line)
    return ln[1] if ln else "none"


# ---------------------------------------------------------------------------- titles (CHAR_INFO +0x6a -> P+0xa1)
def title(c):
    """The title byte for a specialised character (1-based WDMMSG 0.147 row), else None. Tier 2 from
    NOBU_SPEC_TIER2_LEVEL (50), the job's top title at NOBU_SPEC_TOP_LEVEL (60); both DESIGN."""
    line = int((c or {}).get("spec") or 0)
    if line not in LINE or os.environ.get("NOBU_SPEC_TITLE", "1") != "1":
        return None
    job = LINE[line][2]
    k = TITLE_SLOT[line]
    lv = level_of(c)
    if lv >= _env_int("NOBU_SPEC_TOP_LEVEL", 60):
        return 50 + job - 1
    if lv >= _env_int("NOBU_SPEC_TIER2_LEVEL", 50):
        return 29 + (job - 1) * 3 + k
    return 8 + (job - 1) * 3 + k


def title_name(t, jp=False):
    """WDMMSG 0.147 row t - 1: English (wdmmsg.tsv, as the patched client shows it) unless jp, then the disc's
    Japanese."""
    if t is None:
        return ""
    if not 1 <= t <= 56:
        return "" if jp else "Unknown"
    if not jp:
        return items.tsv_name("wdmmsg.tsv", f"0.147.{t - 1}", "") or title_name(t, jp=True)
    return items.tsv_jp("wdmmsg.tsv", f"0.147.{t - 1}")


def title_byte(c, default):
    """chars.appearance hook: record +0x6a = the title when specialised, else what it was."""
    t = title(c) if enabled() else None
    return default if t is None else t


# ---------------------------------------------------------------------------- scroll gating
def _books(c):
    from . import skills
    return skills.state(c)["books"]


def learned_scrolls(c):
    return {int(b.get("scroll", 0)) for b in _books(c)}


def refusal(c, scroll):
    """Refusal bits (shop goods +6 / item +0xaa) for learning scroll; 0 = may learn. Line scrolls only: the job
    must match, the character must be at the unlock level, the line must be free or its own, tier n needs the
    line's tier n-1 (not mastered: DESIGN, the period rule for tiers is not known)."""
    scroll = int(scroll)
    bits = 0
    if scroll in learned_scrolls(c):
        bits |= F_LEARNED
    line = scroll_line(scroll)
    if not line:
        return bits
    ln = LINE[line]
    if job_of(c) != ln[2]:
        bits |= F_JOB
    if level_of(c) < spec_level():
        bits |= F_LEVEL
    have = int(c.get("spec") or 0)
    if have and have != line:
        bits |= F_LINE
    tiers = [s for s, _ in ln[3]]
    k = tiers.index(scroll)
    if k and tiers[k - 1] not in learned_scrolls(c):
        bits |= F_PREREQ
    return bits


def refusal_text(bits):
    for b in (F_JOB, F_PREREQ, F_LINE, F_LEARNED, F_LEVEL):      # the client's order (0x2003d0)
        if bits & b:
            return REFUSAL_TEXT[b]
    return ""


def goods_flags(c, g):
    """Extra goods +6 bits for a shop row (shops.goods_entry): the refusal bits of a scroll item for this
    character, 0 for everything else."""
    if not c or not enabled():
        return 0
    scroll = ITEM_SCROLL.get(int(g.get("id", 0)))
    return refusal(c, scroll) if scroll else 0


def learn(ctx, scroll, store=None):
    """Add scroll as a new book (proficiency 0); a line scroll fixes the line when none is set. Returns
    (ok, lines, pushes)."""
    from . import skills
    c = ctx["char"]
    bits = refusal(c, scroll)
    if bits:
        return False, [refusal_text(bits)], []
    st = skills.state(c, store)
    if len(st["books"]) >= skills.MAX_BOOKS:
        return False, ["No room for another scroll."], []
    b = dict(scroll=int(scroll), prof=0)
    st["books"].append(b)
    out, lines = [skills.notify_book(len(st["books"]) - 1, b)], [f"Learned the scroll 《{scroll_label(scroll)}》."]
    line = scroll_line(scroll)
    if line and not c.get("spec"):
        out += choose(ctx, line, store, quiet=True)
        lines.append(f"Specialisation fixed: {line_label(line)}, title {title_name(title(c))}.")
    items.save(store)
    return True, lines, out


def use_scroll(payload, ctx, store=None):
    """24 03 on a line-scroll item: learn it (item used up) or refuse with the client's text. None = not ours."""
    c = (ctx or {}).get("char")
    if not c or not enabled() or len(payload) < 5 or payload[:2] != b"\x24\x03":
        return None
    inv = items.inventory(c, store)
    it = items.by_index(inv, payload[4])
    scroll = ITEM_SCROLL.get(int(it["id"])) if it else None
    if scroll is None:
        return None
    ok, lines, out = learn(ctx, scroll, store)
    if ok:
        from . import shops
        it["count"] = int(it.get("count", 1)) - 1
        if it["count"] <= 0:
            inv["items"].remove(it)
        items.save(store)
        out.append(shops.notify_counts([(it["idx"], max(0, it["count"]))]))
    return [bytes(payload[:4])] + out + [social.notify_system(t) for t in lines]


# ---------------------------------------------------------------------------- choosing / clearing
def self_info(ctx):
    """A fresh CHAR_INFO for the local player so P+0xa1 (the title) is copied again."""
    from . import chars
    c = ctx["char"]
    return chars.notify_char_info([chars.char_info_record(c, ctx.get("uid", c.get("uid", 0)), chars.char_position(c))])


def choose(ctx, line, store=None, quiet=False):
    c = ctx["char"]
    c["spec"] = int(line)
    items.save(store)
    out = [self_info(ctx)] if os.environ.get("NOBU_SPEC_INFO_PUSH", "1") == "1" else []
    if not quiet:
        out.append(social.notify_system(f"Specialisation: {line_label(line)}. Title: {title_name(title(c))}."))
    return out


def clear(ctx, store=None):
    """'s erase (period rule): the line and every line scroll go."""
    from . import skills
    c = ctx["char"]
    st = skills.state(c, store)
    keep = [b for b in st["books"] if not scroll_line(b.get("scroll"))]
    gone = len(st["books"]) - len(keep)
    st["books"] = keep
    if st.get("training") is not None and st["training"] >= len(keep):
        st["training"] = None
    c.pop("spec", None)
    items.save(store)
    out = [self_info(ctx)] if os.environ.get("NOBU_SPEC_INFO_PUSH", "1") == "1" else []
    return out + [social.notify_system(f"Specialisation cleared; {gone} line scroll(s) forgotten "
                                       "(re-enter the world to refresh the Skills window).")]


def level_note(c, old, new):
    """A line for progression when a level-up crosses the unlock level (no line chosen yet)."""
    if enabled() and not c.get("spec") and old < spec_level() <= new:
        return (f"Level {spec_level()}: you can specialise now. Say `spec` to see the "
                f"{JOBS_EN.get(job_of(c), '')} lines.")
    return None


# ---------------------------------------------------------------------------- chat
def _parse(text):
    t = unicodedata.normalize("NFKC", text or "").strip().lstrip("/!").strip()
    toks = t.split()
    if not toks or toks[0].lower() not in ("spec", "potential", "pot"):
        return None
    return toks


def _line_arg(c, tok):
    """`2`, `Martial` or `martial` -> a line of the character's job (1..3 = its own lines in order)."""
    own = lines_of_job(job_of(c))
    if tok.isdigit():
        n = int(tok)
        if 1 <= n <= 3 and own:
            return own[n - 1][0]
        return n if n in LINE else None
    low = tok.lower()
    for ln in LINES:
        if tok == items.tsv_jp("strdat_08_schools.tsv", f"8.{ln[0]}") or low == ln[1].lower() \
                or low == ln[1].lower().split()[0]:
            return ln[0]
    return None


def chat(payload, ctx, store=None):
    c = (ctx or {}).get("char")
    if not c or os.environ.get("NOBU_SPEC_CHAT", "1") != "1":
        return None
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    toks = _parse(social.parse_line(line)[1])
    if toks is None:
        return None
    if toks[0].lower() in ("potential", "pot"):
        return pot_chat(ctx, toks[1:], store) if os.environ.get("NOBU_POTENTIAL", "1") == "1" else None
    args = toks[1:]
    job = job_of(c)
    if args and args[0].lower() == "reset":
        return clear(ctx, store)
    if args and args[0].lower() == "scroll" and os.environ.get("NOBU_SPEC_DEBUG", "1") == "1":
        # debug command: the line scroll item in the backpack, to learn it by using it (the gated path)
        ln = LINE.get(_line_arg(c, args[1]) if len(args) > 1 else int(c.get("spec") or 0))
        tier = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
        if not ln or not 1 <= tier <= len(ln[3]):
            return [social.notify_system("spec scroll <line> [tier]")]
        from . import house, shops
        inv = items.inventory(c, store)
        it = house.give_item(inv, ln[3][tier - 1][1], 1)
        if it is None:
            return [social.notify_system("Cannot carry any more items.")]
        it["type"] = T_SCROLL
        it["flags"] = 0
        items.save(store)
        return [shops.notify_items([it]), social.notify_system(f"Got {items.item_name(it['id'])}; use it to learn it.")]
    if args:
        want = _line_arg(c, args[0])
        if want not in LINE or LINE[want][2] != job:
            return [social.notify_system("That is not one of your job's lines.")]
        if level_of(c) < spec_level():
            return [social.notify_system(f"Specialisation opens at level {spec_level()}.")]
        have = int(c.get("spec") or 0)
        if have and have != want:
            return [social.notify_system(f"You follow the {line_label(have)} line. Say `spec reset` first "
                                         "(all line scrolls are forgotten).")]
        return choose(ctx, want, store)
    have = int(c.get("spec") or 0)
    opts = ", ".join(f"{i + 1} {line_label(ln[0])}" for i, ln in enumerate(lines_of_job(job)))
    head = (f"Specialisation: {line_label(have)}, title {title_name(title(c))}." if have else
            f"No specialisation yet (opens at level {spec_level()}; you are {level_of(c)}).")
    return [social.notify_system(head), social.notify_system(f"Lines: {opts}. Say `spec <n>` to choose.")]


def answer(payload, ctx, store=None):
    if not enabled() or len(payload) < 4:
        return None
    if payload[:2] == b"\x23\x08":
        return chat(payload, ctx, store)
    if payload[:2] == b"\x24\x03":
        return use_scroll(payload, ctx, store)
    if payload[0] == 0x23 and payload[1] in (0x1A, 0x1B):
        try:
            return pot_answer(payload, ctx, store)
        except vce.WireError:
            return None
    return None


def world_ready(ctx):
    return pot_world_ready(ctx)


# ============================================================================ potential KNOWN (client): 23 1a <seq>
# (header only, world entry 0x18b920 -> 0x206f20, kind 0x37 / net 0x6c) is answered with `u8 n` (raw), n x {u8 id, u8
# cur, u8 max, u16 cost} (0x1eff90), then, only if bytes remain, n x u8 category (0x1f00e0): UI 0x34 -> 0x209720 stores
# id < 200 at P+0x10308 + id*12. `23 1b <seq> u8 id` (kind 0x38, net 0x6d; the Profile -> Potential raise dialog
# 0x671f50 checks cur < max and points >= cost itself, UIMSG 2.359) is answered with `u16 points, u8 id, u8 cur, u8 max,
# u16 cost [, u8 cat]` (0x284e40): UI 0x35 -> 0x2097d0 sets the category's points (P+0x102d8 + cat*16 + 2) and the
# entry. 2F 1E (0x286258) carries up to 3 category records REC = u16 level, u16 points, u8, u32 mastery, u32 mastery
# needed (0x1f2370): REC0 [u8 cat0, u8 k, k x (REC, u8 cat)] -> UI 0x4e -> 0x209670 (the page header: 2.356 points,
# 2.357 Lv = level + 1, 2.358 mastery gauge). The pane is closed below level 30 (0x66d278, UIMSG 2.378). Names /
# descriptions are STRDAT 6 / 7 (0x2740f0 / 0x274090). Period (B 2004-06-30): from Lv 30 battles give; enough raises the
# potential level, which gives points to spend. W (UNDATED): +10 points per potential level; the per-ability raise
# counts and costs below. DESIGN: mastery = the battle Prof. reward at level >= 30 (progression.award hook),
# NOBU_POT_MASTERY per level; the effects are stored only (no stat changes yet); Counter Will (no wiki row) costs like
# Sharp Eyes; totals the wiki gives without a per-raise list are split by us.
POTENTIALS = {  # id (STRDAT 6) -> cost of each raise (len = max raises)
    1: [3, 3, 3, 4, 4, 4, 5, 5, 5, 6],                  # Leg Strength move +1% (total 42)
    2: [5, 5, 6, 6, 7, 7, 8, 8, 9, 9], 3: [5, 5, 6, 6, 7, 7, 8, 8, 9, 9],   # Sharp Eyes Reflexes (5..9, 70)
    4: [5, 5, 6, 6, 7, 7, 8, 8, 9, 9],                  # Counter Will (no wiki row: as Sharp Eyes)
    5: list(range(4, 24)), 6: list(range(4, 24)),       # Health Spirit +10 Life / Ki (270)
    7: [4, 4, 5, 5, 6], 8: [6, 6, 7, 7, 8],             # Recovery Grit field regen (24 / 34)
    9: [10, 12, 14],                                    # Muscle carry weight (36)
    10: [50, 80],                                       # Versatility +1 skill slot (130)
    **{i: list(range(6, 26)) for i in range(11, 20)},   # Grip Sturdy Agile Calm Virtue + the four elements (310)
    20: [12, 14, 16],                                   # Bargaining (42)
}
POT_LEVEL = 30
POT_POINTS_PER_LEVEL = 10


def pot_state(c):
    p = c.setdefault("potential", {})
    for k in ("level", "points", "mastery"):
        p.setdefault(k, 0)
    p.setdefault("raised", {})
    return p


def pot_need():
    return max(1, _env_int("NOBU_POT_MASTERY", 300))


def pot_entry(c, pid):
    costs = POTENTIALS[pid]
    cur = int(pot_state(c)["raised"].get(str(pid), 0))
    return pid, cur, len(costs), (costs[cur] if cur < len(costs) else 0)


def pot_add_mastery(c, n):
    """Mastery at level >= 30 (DESIGN: the battle Prof. reward); returns the potential levels gained."""
    if not enabled() or level_of(c) < POT_LEVEL or int(n) <= 0:
        return 0
    p = pot_state(c)
    p["mastery"] = int(p["mastery"]) + int(n)
    gained = 0
    while p["mastery"] >= pot_need() and p["level"] < 0xFFFF:
        p["mastery"] -= pot_need()
        p["level"] += 1
        p["points"] = min(0xFFFF, p["points"] + POT_POINTS_PER_LEVEL)
        gained += 1
    return gained


def pot_list_reply(seq, c):
    ids = sorted(POTENTIALS)
    body = vce.put_u8(len(ids))
    for pid in ids:
        pid, cur, mx, cost = pot_entry(c, pid)
        body += bytes([pid, cur, mx]) + vce.put_u16(cost)
    body += bytes(len(ids))                                      # category 0 (base) for every entry
    return vce.put_u8(0x23) + vce.put_u8(0x1A) + vce.put_u16(seq) + body


def pot_raise_reply(seq, c, pid):
    pid, cur, mx, cost = pot_entry(c, pid)
    return (vce.put_u8(0x23) + vce.put_u8(0x1B) + vce.put_u16(seq) + vce.put_u16(pot_state(c)["points"])
            + bytes([pid, cur, mx]) + vce.put_u16(cost) + vce.put_u8(0))


def pot_push(c, seq=0):
    """2F 1E: base (0) with the character's numbers, combat (1) and craft (2) empty."""
    p = pot_state(c)

    def rec(lv, pts, mastery):
        return vce.put_u16(lv) + vce.put_u16(pts) + vce.put_u8(0) + vce.put_u32(mastery) + vce.put_u32(pot_need())
    return (vce.put_u8(0x2F) + vce.put_u8(0x1E) + vce.put_u16(seq) + rec(p["level"], p["points"], p["mastery"])
            + vce.put_u8(0) + vce.put_u8(2) + rec(0, 0, 0) + vce.put_u8(1) + rec(0, 0, 0) + vce.put_u8(2))


def pot_raise(ctx, pid, store=None):
    """(ok, text) for one raise of potential pid."""
    c = ctx["char"]
    if level_of(c) < POT_LEVEL:
        return False, "Potential cannot be used until Level 30."
    if pid not in POTENTIALS:
        return False, "No such potential."
    p = pot_state(c)
    pid, cur, mx, cost = pot_entry(c, pid)
    if cur >= mx:
        return False, "That potential is at its maximum."
    if p["points"] < cost:
        return False, f"Not enough potential points ({p['points']} / {cost})."
    p["points"] -= cost
    p["raised"][str(pid)] = cur + 1
    items.save(store)
    return True, f"{pot_name(pid)} raised to {cur + 1}/{mx} ({p['points']} points left)."


def pot_name(pid, jp=False):
    """STRDAT 6 row pid: English (strdat_06_traits.tsv) unless jp, then the disc's Japanese."""
    if not jp and pid in POTENTIALS:
        return items.tsv_name("strdat_06_traits.tsv", f"6.{pid}", "") or pot_name(pid, jp=True)
    return items.tsv_jp("strdat_06_traits.tsv", f"6.{pid}") if 1 <= pid <= 20 else f"potential {pid}"


def pot_answer(payload, ctx, store=None):
    c = (ctx or {}).get("char")
    if not c or os.environ.get("NOBU_POTENTIAL", "1") != "1":
        return None
    r = vce.Reader(payload, 2)
    seq = r.u16()
    if payload[1] == 0x1A:
        return [pot_list_reply(seq, c)]
    pid = r.u8() if len(r.rest()) else 0
    ok, text = pot_raise(ctx, pid, store)
    return [pot_raise_reply(seq, c, pid if pid in POTENTIALS else 1), pot_push(c), social.notify_system(text)]


def pot_chat(ctx, args, store=None):
    c = ctx["char"]
    p = pot_state(c)
    out = []
    if len(args) >= 2 and args[0].lower() == "mastery" and args[1].isdigit() and os.environ.get("NOBU_SPEC_DEBUG", "1") == "1":
        gained = pot_add_mastery(c, int(args[1]))
        items.save(store)
        if gained:
            out.append(social.notify_system(f"Potential level up! (+{gained * POT_POINTS_PER_LEVEL} points)"))
    elif len(args) >= 2 and args[0].lower() == "raise" and args[1].isdigit():
        out.append(social.notify_system(pot_raise(ctx, int(args[1]), store)[1]))
    raised = ", ".join(f"{pot_name(int(k))} {v}" for k, v in sorted(p["raised"].items(), key=lambda kv: int(kv[0])))
    out.append(social.notify_system(f"Potential Lv {p['level']}, {p['points']} points, mastery {p['mastery']}/"
                                    f"{pot_need()}; raised: {raised or 'none'}."))
    return out + [pot_push(c)]


def pot_world_ready(ctx):
    c = (ctx or {}).get("char")
    if not c or not enabled() or os.environ.get("NOBU_POTENTIAL", "1") != "1" or level_of(c) < POT_LEVEL:
        return []
    return [pot_push(c)]
