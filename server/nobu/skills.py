"""Skills and scroll training: learned skills, slots, the training scroll, proficiency.

KNOWN:
- The client keeps one skill object SK = UI+0x5ac4 (P+0x5a98): 100 scroll ("book") slots of 0x1a8 at SK+0x168,
  each with up to FOUR skills (0x64 each at B+0x14), the 10 current slots SK+0xc (pointers), a 5 x 10 swap grid
  of u16 (book*4 + skill index, 0xffff empty) at SK+0x100, the training scroll slot u16 SK+0xfe and the usable
  slot count u8 SK+0x34. Names come from STRDAT: skill id -> table 1 (0x273ed0), scroll id -> table 2 (0x273f30).
- World entry (0x18b880) asks `26 02` (book list, msg 9) then `26 01` (standby list, msg 8); both replies are
  stored by the waiter callbacks 0x2034f0 / 0x203490 (0x203940 / 0x203840).
    26 02 reply: u16 training slot, u16 array (len + n, n <= 100) = scroll id per slot (SK+0x36), u8 M,
                 M x {u16 slot, u8 (B+0x11), u8 (B+0x10), u16 proficiency (B+0x1a4), u16 scroll id (B+0xe),
                 u32 array (len + n, n <= 4) skill ids}                                    (0x1ef570, 0x202e40)
    26 01 reply: u8 N (<= 11), N x E (current slots), then 50 x E (the 5 x 10 swap grid, mandatory);
                 E = u8 slot, u8 book slot, u8 skill index, R; R = u32 skill id, u16 flags, u8, u8   (0x1efbb0)
- Learned skills are computed by the client: learned = proficiency / 2500 per scroll, the scroll is mastered
  at 2500 x its skill count (0x66a8d8 / 0x66bd94 / 0x36ced4: divide by 2500, compare with 0x9c4 * n).
- `26 03 <u32 skill id>` -> i8 res, u32 id, str (<= 0x10 SJIS kept, S+0x15), str (<= 0x34 kept, S+0x26, shown as
  two 26-byte lines), u16 (S+0x5c, shown as a number unless flags & 4) (0x1eef90, 0x202b60, panel 0x383750).
  `26 04 <u32 book slot>` -> i8 res, u16, 4 x R, [u8 -> B+0x1a6] (0x1ef250, 0x202e90).
  `26 08 <u32 book slot>` = set the training scroll -> u16 new training slot (waiter msg 0x41, 0x2031d0 ->
  0x203260 -> SK+0xfe); the Scrolls window refuses a mastered scroll itself (0x66aee0).
  `26 05 <u8 slot> <u32 book*4 + index + 1>` equip -> u8 slot, u32 same (0x207000 -> 0x203a60; 0 = refused).
  `26 0a <u8 set> <u8 slot> <u16 code>` -> i32 res, u8, u8, u16: code != 0 stores grid[set][slot] = code, code 0
  copies the current 10 slots into grid[set] (0x203270).
- S>C cmd 0x32 (parser 0x28a740, u8 sub, u16 seq; main 0x28b080): sub 1 `u16 book slot, u32 add` -> UI 0x49 ->
  0x203c40: B+0x1a4 += add (live proficiency); sub 2 book entry -> UI 0x2c (add / replace a scroll); sub 3 / 7
  skill info; sub 4 `u16` -> UI 0x41 -> SK+0xfe (training slot); sub 5 `u16 n, n x R` -> UI 0x2e (update skills);
  sub 9 `u8 n, n x u8 (set << 4 | slot)` -> UI 0x46 -> 0x203b90 (clear slots).
- S>C `2F 07` (0x1eb1d0 -> UI 1 -> 0x209280): 9 x u16 (P+0x10294.., the Status rows), u32, u32, u16, u8 skill slot
  count (-> SK+0x34, the only writer), 8 x u16 (P+0x102a6 / +0x102b8 / +0x102a8 / +0x102ba / +0x102aa / +0x102ac
  / +0x546c / +0x546e). Without it the client has 0 usable slots.
- Battle: a 22 33 item with key (category) 5 is a child of the Skills node 0xc (node kind 2, param 5: 0x2170c0
  lists every item whose +4 == 5). Its value comes back as the 22 01 action. Cost = u16 extra cost (+0x1a) plus
  a per-class surcharge (menu+0x50.., 0x21b160); greyed when cost > the unit's Ki (0x218020). Target type row
  5 = self (flags & 1), row 1 = one living enemy.

DESIGN (not from the client): the skill table below (real STRDAT names, invented numbers), one starter scroll
per job, slots = 3 + Int // 25 (max 10), proficiency gained = battle Prof. x NOBU_SKILL_TRAIN_RATE, the
starting proficiency (first technique known), the flags value, the battle effects and scene codes.

Knobs: NOBU_SKILLS=0 (module off), NOBU_SKILL_TRAIN_RATE (25), NOBU_SKILL_START_PROF (2500), NOBU_SKILL_BASE_SLOTS
(3), NOBU_SKILL_STATUS=0 (no 2F 07 after 26 01), NOBU_SKILL_NAMES=jp (Japanese names in the battle menu),
NOBU_SKILL_SCENE (scene code for every skill), NOBU_SKILL_CHAT=0 (no "skill" chat commands),
NOBU_SKILL_FLAGS (R flags, default 0x10), NOBU_SKILL_BATTLE=0 (no Skills entries in battle).
"""
import os
import unicodedata

from . import items, social, vce

CMD, NOTIFY = 0x26, 0x32
PROF_PER_SKILL = 2500          # KNOWN: 0x9c4 per skill in the scroll list (0x66a8d8)
MAX_BOOKS, MAX_PER_BOOK, MAX_SLOTS, SETS = 100, 4, 10, 5
CATEGORY = 5                   # KNOWN: 22 33 key of the Skills node (node 0xc = kind 2, param 5)
NO_TRAINING = 0xFFFF
SELF, ENEMY = 5, 1             # 22 33 target types (table 0x3e7d80 rows): 5 = self, 1 = one living enemy
SCENE_SELF = 0x157E            # the intro camera code (plays with the actor only; used by battle.py's record 1)

SUB_NAMES = {1: "STANDBYLIST", 2: "BOOKLIST", 3: "SKILLDATA_GET_INFO", 4: "SKILLBOOK_GET_INFO", 5: "EQUIP",
             6: "UNEQUIP", 7: "COMMAND", 8: "SET_TRAINING", 9: "sub 9", 0xA: "SWAP_SETUP", 0xB: "sub 0xb",
             0xC: "CONFIG"}
NOTIFY_NAMES = {1: "PROF_ADD", 2: "BOOK", 3: "SKILL_INFO", 4: "TRAINING", 5: "SKILL_STATE", 6: "sub 6",
                7: "SKILL_INFO2", 8: "sub 8", 9: "SLOTS_CLEAR"}


def S(sid, en, kind, ki, power=0.0, stat="atk", ttype=ENEMY, scene=3, dur=7, buff=None, text="", kill_scene=None):
    """One skill (DESIGN): kind damage | arts (Int based damage) | heal | buff | debuff | drain.
    power = damage multiplier (damage / arts / drain) or heal fraction of max Life (heal); buff = {stat: pct}."""
    return dict(id=sid, en=en, kind=kind, ki=ki, power=power, stat=stat, ttype=ttype, scene=scene,
                dur=dur, buff=buff or {}, text=text, kill_scene=kill_scene)


# ---------------------------------------------------------------------------- the skill table (DESIGN) Ids =
# STRDAT table 1 index (the client names them itself), scroll ids = STRDAT table 2 index. English names are ours.
# Scenes (BCBSCENE codes; battle.py puts them in the record's BI u16@0; dur >= scene end frames / 60 because the
# director ends the scene at start + dur). KNOWN layout ("Scene codes"): 0x239..0x4a2 weapon combos, 0x5c1..0x824
# weapon-art blocks (dmg, kill, 4 dodge), 4001..4931 (0xfa1..0x1343) jutsu (4898 / 4899). No table links a skill
# id to a code, so each mapping below is a HYPOTHESIS by name / kind; kill_scene = the matching kill code when the
# skill is fatal.
SKILLS = {s["id"]: s for s in (
    # Samurai: Samurai Prec I
    S(121, "Twin Strike", "damage", 6, 1.4, text="Two quick cuts at one foe.", scene=0x255, dur=5, kill_scene=0x258),
    S(13, "Provoke", "buff", 4, ttype=SELF, buff={"dfn": 30},
      text="Draw the foe and raise your guard (Def +30%).", scene=0x1322, dur=5),
    S(43, "War Cry", "debuff", 5, 0.8, buff={"atk": -20}, text="A shout that shakes the foe (Atk -20%).", scene=0x106B, dur=4),
    S(130, "Armour Piercer", "damage", 10, 1.2, stat="pierce", text="A thrust that ignores armour.", scene=0x5C1, dur=5, kill_scene=0x5C2),
    # Monk: Devotions I
    S(168, "Recovery I", "heal", 8, 0.30, ttype=SELF,
      text="Restore 30% of your Life plus 2 x Int.", scene=0xFAF, dur=5),
    S(162, "Rite of Strength I", "buff", 6, ttype=SELF, buff={"atk": 25},
      text="Prayer that hardens the arm (Atk +25%).", scene=0xFB5, dur=8),
    S(159, "Rite of Warding I", "buff", 6, ttype=SELF, buff={"dfn": 30},
      text="Prayer that shields the body (Def +30%).", scene=0xFB5, dur=8),
    S(156, "Nenbutsu", "arts", 7, 1.2, text="A chanted invocation strikes one foe.", scene=0xFFB, dur=5),
    # Priest: Priest Prec I
    S(171, "Flame I", "arts", 7, 1.3, text="Sacred fire burns one foe.", scene=0xFFC, dur=5),
    S(282, "Grant Life I", "heal", 8, 0.35, ttype=SELF,
      text="Restore 35% of your Life plus 2 x Int.", scene=0xFAF, dur=5),
    S(260, "Divine Grace I", "buff", 6, ttype=SELF, buff={"dfn": 35},
      text="The gods' protection (Def +35%).", scene=0xFB5, dur=8),
    S(263, "Gale I", "arts", 12, 1.7, text="A cutting wind strikes one foe.", scene=0x1000, dur=5),
    # Onmyoji: Onmyoji Prec I
    S(276, "Wind Blade I", "arts", 5, 1.1, text="A blade of wind strikes one foe.", scene=0xFFF, dur=5),
    S(191, "Frost I", "arts", 8, 1.4, text="Freezing cold strikes one foe.", scene=0xFFD, dur=5),
    S(335, "Barrier I", "buff", 8, ttype=SELF, buff={"dfn": 40},
      text="A warding circle (Def +40%).", scene=0x1322, dur=5),
    S(273, "Hex I", "debuff", 6, 0.6, stat="int", buff={"atk": -25},
      text="A curse that saps the foe (Atk -25%).", scene=0x106B, dur=4),
    # Ninja: Ninja Prec I
    S(132, "Vital Aim", "damage", 6, 1.3, stat="crit", text="Aim for a vital spot (often critical).", scene=0x5CD, dur=5, kill_scene=0x5CE),
    S(22, "Evasion", "buff", 5, ttype=SELF, buff={"dex": 40},
      text="Light footwork (Dex +40%).", scene=0x1322, dur=5),
    S(380, "Flurry", "damage", 9, 1.6, text="A rain of blows on one foe.", scene=0x447, dur=7, kill_scene=0x44A),
    S(334, "Concealment", "buff", 8, ttype=SELF, buff={"dex": 60},
      text="Fade from sight (Dex +60%).", scene=0x1322, dur=5),
    # Smith: Smith Prec I
    S(146, "Adamant Blow I", "damage", 7, 1.4, text="A smith's hammer blow.", scene=0x5D3, dur=5, kill_scene=0x5D4),
    S(202, "Weapon Mastery", "buff", 5, ttype=SELF, buff={"atk": 20},
      text="Use every edge of the weapon (Atk +20%).", scene=0x1322, dur=5),
    S(221, "Nio Stance", "buff", 7, ttype=SELF, buff={"dfn": 40},
      text="Stand like a temple guardian (Def +40%).", scene=0xFB5, dur=8),
    # Physician: Phys Prec I
    S(210, "Revitalise I", "heal", 7, 0.35, ttype=SELF,
      text="Restore 35% of your Life plus 2 x Int.", scene=0xFAF, dur=5),
    S(288, "Drain Life I", "drain", 8, 1.0, stat="int",
      text="Steal Life from one foe (heals half).", scene=0x1010, dur=5),
    S(247, "Purify I", "heal", 5, 0.15, ttype=SELF,
      text="Clear your weakened states, restore 15% Life.", scene=0x1323, dur=5),
    # the second scrolls of the reviver jobs (lifecycle.py): Revive 1 first. Revive is a field skill (R flags
    # 0x20: the client refuses a field use without it, 0x667cf0) and stays out of battle.
    S(522, "Revive I", "revive", 12, 0.25, ttype=SELF, scene=0x1323, dur=5,
      text="Call a fallen ally (a ghost) back with 25% Life."),
    S(169, "Recovery II", "heal", 12, 0.45, ttype=SELF, scene=0xFAF, dur=5,
      text="Restore 45% of your Life plus 2 x Int."),
    S(163, "Rite of Strength II", "buff", 9, ttype=SELF, scene=0xFB5, dur=8, buff={"atk": 40},
      text="Prayer that hardens the arm (Atk +40%)."),
    S(160, "Rite of Warding II", "buff", 9, ttype=SELF, scene=0xFB5, dur=8, buff={"dfn": 45},
      text="Prayer that shields the body (Def +45%)."),
    S(283, "Grant Life II", "heal", 12, 0.5, ttype=SELF, scene=0xFAF, dur=5,
      text="Restore 50% of your Life plus 2 x Int."),
    S(172, "Flame II", "arts", 11, 1.8, scene=0xFFC, dur=5, text="Sacred fire engulfs one foe."),
    S(261, "Divine Grace II", "buff", 9, ttype=SELF, scene=0xFB5, dur=8, buff={"dfn": 50},
      text="The gods' protection (Def +50%)."),
    S(211, "Revitalise II", "heal", 11, 0.5, ttype=SELF, scene=0xFAF, dur=5,
      text="Restore 50% of your Life plus 2 x Int."),
    S(289, "Drain Life II", "drain", 12, 1.4, stat="int", scene=0x1010, dur=5,
      text="Steal Life from one foe (heals half)."),
    S(248, "Purify II", "heal", 8, 0.25, ttype=SELF, scene=0x1323, dur=5,
      text="Clear your weakened states, restore 25% Life."),
)}
FIELD_FLAGS = 0x20             # KNOWN: 0x667cf0 refuses a field use without R flag 0x20 (UIMSG 2.297)

SCROLLS = {  # scroll id -> (en, skill ids in learning order; the client keeps at most 4)
    1: ("Way of the Samurai I", [121, 13, 43, 130]),
    48: ("Devotions I", [168, 162, 159, 156]),
    77: ("Way of the Shrine I", [171, 282, 260, 263]),
    109: ("Way of the Onmyoji I", [276, 191, 335, 273]),
    146: ("Way of the Shinobi I", [132, 22, 380, 334]),
    173: ("Way of the Smith I", [146, 202, 221]),
    199: ("Way of the Healer I", [210, 288, 247]),
    49: ("Devotions II", [522, 169, 163, 160]),
    78: ("Way of the Shrine II", [522, 283, 172, 261]),
    200: ("Way of the Healer II", [522, 211, 289, 248]),
}
NEXT_SCROLL = {48: 49, 77: 78, 199: 200}   # DESIGN: mastering the first scroll hands out the second
# jobs (UIMSG 0.444): 1 Samurai, 2 Monk, 3 Priest, 4 Onmyoji, 5 Ninja, 6 Smith, 7 Physician
STARTER = {1: 1, 2: 48, 3: 77, 4: 109, 5: 146, 6: 173, 7: 199}
DEFAULT_STARTER = 1


def _env_int(name, default):
    v = os.environ.get(name)
    try:
        return default if v in (None, "") else int(v, 0)
    except ValueError:
        return default


def enabled():
    return os.environ.get("NOBU_SKILLS", "1") == "1"


def _clip(text, n):
    from . import misc
    return misc.clip_sjis(text or "", n)


def _save(store):
    st = store() if callable(store) else store
    if st is not None and hasattr(st, "save"):
        st.save()


# ---------------------------------------------------------------------------- per-character state
def _looks(c):
    return (list((c or {}).get("looks", [])) + [0] * 20)[:20]


def job_of(c):
    return _looks(c)[3]


def intellect(c):
    return _looks(c)[14]                     # Str, Sta, Dex, Int, Cha = looks[11:16] (chars.py)


def slot_count(c):
    """DESIGN: 3 slots plus one per 25 Int (FAQ04: slots grow with Intellect), at most the client's 10."""
    return max(0, min(MAX_SLOTS, _env_int("NOBU_SKILL_BASE_SLOTS", 3) + intellect(c) // 25))


def state(c, store=None):
    """char["skills"] (shared with misc.py's bar / swap / config), created with the job's starter scroll."""
    st = c.setdefault("skills", {})
    if not isinstance(st.get("books"), list):
        scroll = STARTER.get(job_of(c), DEFAULT_STARTER)
        prof = max(0, min(PROF_PER_SKILL * len(SCROLLS[scroll][1]), _env_int("NOBU_SKILL_START_PROF", 2500)))
        st["books"] = [dict(scroll=scroll, prof=prof)]
        st["training"] = 0 if prof < PROF_PER_SKILL * len(SCROLLS[scroll][1]) else None
        st.setdefault("bar", {})
        _autoequip(c, st)
        _save(store)
    st.setdefault("bar", {})
    return st


def book_skills(book):
    return list(SCROLLS.get(book.get("scroll"), ("", []))[1])[:MAX_PER_BOOK]


def learned_count(book):
    return min(len(book_skills(book)), int(book.get("prof", 0)) // PROF_PER_SKILL)


def mastered(book):
    n = len(book_skills(book))
    return n > 0 and int(book.get("prof", 0)) >= PROF_PER_SKILL * n


def learned(c):
    """Learned skill ids in scroll order."""
    out = []
    for b in state(c)["books"]:
        out += book_skills(b)[:learned_count(b)]
    return out


def locate(c, sid):
    """(book slot, skill index) of a skill id, or None."""
    for i, b in enumerate(state(c)["books"]):
        if sid in book_skills(b):
            return i, book_skills(b).index(sid)
    return None


def packed(book, idx):
    return book * 4 + idx + 1               # 26 05 value (0x207000: book*4 + index + 1)


def unpack(v):
    v = int(v) - 1
    return (v // 4, v % 4) if v >= 0 else None


def bar(c):
    """{slot: skill id} of the valid, learned entries of the equip bar."""
    st, out = state(c), {}
    for k, v in st.get("bar", {}).items():
        try:
            slot, bi = int(k), unpack(v)
        except (TypeError, ValueError):
            continue
        if bi is None or not 0 <= slot < slot_count(c) or not 0 <= bi[0] < len(st["books"]):
            continue
        b = st["books"][bi[0]]
        if bi[1] < learned_count(b):
            out[slot] = book_skills(b)[bi[1]]
    return out


def _autoequip(c, st):
    """Put learned skills that are not on the bar into free slots (DESIGN; the client learns the bar from 26 01)."""
    used = {int(k) for k in st.get("bar", {})}
    on = set()
    for v in st.get("bar", {}).values():
        bi = unpack(v)
        if bi:
            on.add(bi)
    for i, b in enumerate(st["books"]):
        for j in range(learned_count(b)):
            if (i, j) in on:
                continue
            free = next((s for s in range(slot_count(c)) if s not in used), None)
            if free is None:
                return
            st["bar"][str(free)] = packed(i, j)
            used.add(free)


# ---------------------------------------------------------------------------- training (battle Prof.)
def train(ctx, prof, store=None, rate=None):
    """Battle Prof. -> the training scroll (x NOBU_SKILL_TRAIN_RATE, capped at mastery). Queues the frames for
    flush(): 32 01 (live proficiency), 32 04 when the scroll is mastered (no training scroll), system lines
    "Learned skill ..." / "... mastered!" (UIMSG 3.35 / 3.34 wording). Returns dict(add, learned, mastered)."""
    c = (ctx or {}).get("char")
    if not c or not enabled() or prof <= 0:
        return None
    st = state(c, store)
    slot = st.get("training")
    if slot is None or not 0 <= slot < len(st["books"]):
        return None
    b = st["books"][slot]
    cap = PROF_PER_SKILL * len(book_skills(b))
    before = learned_count(b)
    add = max(0, min(cap - int(b["prof"]), int(prof) * (_env_int("NOBU_SKILL_TRAIN_RATE", 25) if rate is None else rate)))
    if add <= 0:
        return None
    b["prof"] = int(b["prof"]) + add
    new = book_skills(b)[before:learned_count(b)]
    res = dict(slot=slot, add=add, prof=b["prof"], learned=new, mastered=mastered(b))
    q = ctx.setdefault("skill_pending", [])
    q.append(notify_prof(slot, add))
    for sid in new:
        q.append(social.notify_system(f"Learned skill 《{name(sid)}》."))
    if res["mastered"]:
        st["training"] = None
        q.append(notify_training(NO_TRAINING))
        q.append(social.notify_system(f"Scroll 《{scroll_name(b['scroll'])}》 mastered!"))
        q += unlock_next(c, b)
    if new:
        _autoequip(c, st)
    _save(store)
    ctx.setdefault("skill_log", []).append(res)
    return res


def flush(ctx):
    """The queued 32 01 / 32 04 pushes and lines (call when the player is back in the field)."""
    return list((ctx or {}).pop("skill_pending", []))


# ---------------------------------------------------------------------------- names / info
def name(sid, lang=None):
    s = SKILLS.get(sid)
    if not s:
        return f"skill {sid}"
    lang = lang or os.environ.get("NOBU_SKILL_NAMES", "en")
    return items.tsv_jp("strdat_01_skills.tsv", f"1.{sid}", s["en"]) if lang == "jp" else s["en"]


def scroll_name(scroll):
    en = SCROLLS.get(scroll, (f"scroll {scroll}", []))[0]
    if os.environ.get("NOBU_SKILL_NAMES", "en") == "jp":
        return items.tsv_jp("strdat_02_skillbooks.tsv", f"2.{scroll}", en)
    return en


def label(s):
    """Short tag for the details panel (S+0x15 keeps 0x10 SJIS bytes)."""
    return {"damage": "Strike", "arts": "Art", "heal": "Healing", "buff": "Self boon", "debuff": "Curse",
            "drain": "Drain"}.get(s["kind"], "")


def info(c, action):
    """(name, text) for a battle 22 42 on a skill of this character, else None."""
    s = SKILLS.get(action)
    if not s or not c or action not in learned(c):
        return None
    return name(action), f"{s['text']} Ki {s['ki']}."


def flags(sid=None):
    """R flags: NOBU_SKILL_FLAGS (0x10, combat) for battle skills, FIELD_FLAGS for field-only ones (revive)."""
    if SKILLS.get(sid, {}).get("kind") == "revive":
        return FIELD_FLAGS
    return _env_int("NOBU_SKILL_FLAGS", 0x10) & 0xFFFF


def unlock_next(c, book):
    """A mastered scroll with a NEXT_SCROLL entry adds that scroll (once) and makes it the training scroll:
    32 02 book entry + 32 04 training slot + a line. Returns the frames."""
    nxt = NEXT_SCROLL.get(book.get("scroll"))
    st = state(c)
    if nxt is None or any(b["scroll"] == nxt for b in st["books"]) or len(st["books"]) >= MAX_BOOKS:
        return []
    st["books"].append(dict(scroll=nxt, prof=0))
    slot = len(st["books"]) - 1
    st["training"] = slot
    return [notify_book(slot, st["books"][slot]), notify_training(slot),
            social.notify_system(f"New scroll 《{scroll_name(nxt)}》: set for training.")]


# ---------------------------------------------------------------------------- wire (cmd 0x26 / 0x32 / 2F 07)
def _hdr(cmd, sub, seq):
    return vce.put_u8(cmd) + vce.put_u8(sub) + vce.put_u16(seq)


def w_r(sid, fl=None, b6=0, b7=0):
    """R (0x1ef100): u32 skill id, u16 flags (S+0x5e), u8 (S+0x14), u8 (S+0x60)."""
    return vce.put_u32(sid) + vce.put_u16(flags(sid) if fl is None else fl) + bytes([b6 & 0xFF, b7 & 0xFF])


def w_e(slot=0, book=0, idx=0, sid=0):
    """E (0x1efbb0): u8 slot, u8 book slot, u8 skill index, R. sid 0 = empty (skipped by 0x203840)."""
    return bytes([slot, book, idx]) + (w_r(sid) if sid else bytes(8))


def w_book(slot, b):
    """Book entry (0x1ef570): u16 slot, u8, u8, u16 proficiency, u16 scroll id, u32 array (<= 4) skill ids."""
    ids = book_skills(b)
    return (vce.put_u16(slot) + b"\0\0" + vce.put_u16(min(0xFFFF, int(b["prof"]))) + vce.put_u16(b["scroll"])
            + vce.put_len(len(ids)) + b"".join(vce.put_u32(i) for i in ids))


def booklist_reply(seq, c):
    st = state(c)
    books = st["books"][:MAX_BOOKS]
    tr = st.get("training")
    out = _hdr(CMD, 2, seq) + vce.put_u16(NO_TRAINING if tr is None else tr)
    out += vce.put_len(len(books)) + b"".join(vce.put_u16(b["scroll"]) for b in books)
    out += vce.put_u8(len(books)) + b"".join(w_book(i, b) for i, b in enumerate(books))
    return out


def standby_reply(seq, c):
    st = state(c)
    cur = []
    for slot, sid in sorted(bar(c).items())[:11]:
        bi = locate(c, sid)
        cur.append(w_e(slot, bi[0], bi[1], sid))
    grid = b""
    g = st.get("grid") or []
    for s in range(SETS):
        row = (list(g[s]) if s < len(g) else []) + [0xFFFF] * MAX_SLOTS      # grid code = book*4 + index
        for j in range(MAX_SLOTS):
            bi = unpack(row[j] + 1) if isinstance(row[j], int) and 0 <= row[j] < 0xFFFF else None
            sid = 0
            if bi and 0 <= bi[0] < len(st["books"]) and bi[1] < learned_count(st["books"][bi[0]]):
                sid = book_skills(st["books"][bi[0]])[bi[1]]
            grid += w_e(j, bi[0], bi[1], sid) if sid else w_e()
    return _hdr(CMD, 1, seq) + vce.put_u8(len(cur)) + b"".join(cur) + grid


def skill_info_reply(seq, sid, c=None):
    s = SKILLS.get(sid)
    if not s:
        return _hdr(CMD, 3, seq) + vce.put_i8(-1)
    return (_hdr(CMD, 3, seq) + vce.put_i8(0) + vce.put_u32(sid)
            + vce.put_bytes(_clip(label(s), 0x10).encode("utf-8"))
            + vce.put_bytes(_clip(s["text"], 0x34).encode("utf-8")) + vce.put_u16(s["ki"]))


def book_info_reply(seq, slot, c):
    st = state(c)
    if not 0 <= slot < len(st["books"]):
        return _hdr(CMD, 4, seq) + vce.put_i8(-1)
    ids = (book_skills(st["books"][slot]) + [0] * 4)[:4]
    from . import hiryu                                   # trailing u8 -> B+0x1a6 = the scroll's line
    line = hiryu.scroll_line(st["books"][slot].get("scroll"))
    return (_hdr(CMD, 4, seq) + vce.put_i8(0) + vce.put_u16(slot)
            + b"".join(w_r(i) if i else bytes(8) for i in ids) + vce.put_u8(line))


def status_push(c, seq=0):
    """2F 07 (0x1eb1d0 -> 0x209280): the only writer of the usable skill slot count (u8 +0x1e -> SK+0x34).
    With upkeep.py on (its field Life / Ki / weight state) its stats_push is used, which carries the same slot
    count; this module's own block is the fallback.
    Other fields (HYPOTHESIS for the order): the 9 u16 = Str, Sta, Dex, Int, Cha, Earth, Water, Fire, Wind
    (the list entry's order), u32 / u32 / u16 = 0, then max Life, Life, max Ki, Ki, Attack, Defence (equal pairs,
    so the pair order does not matter), 0, 0. Life / Ki / Attack / Defence = battle.player_combat."""
    from . import battle, upkeep
    if upkeep.enabled():
        return upkeep.stats_push(c)
    lk = _looks(c)
    pc = battle.player_combat(c)
    body = b"".join(vce.put_u16(v) for v in lk[11:20])
    body += vce.put_u32(0) + vce.put_u32(0) + vce.put_u16(0) + vce.put_u8(slot_count(c))
    body += b"".join(vce.put_u16(v) for v in (pc["life"], pc["life"], pc["ki"], pc["ki"], pc["atk"], pc["dfn"], 0, 0))
    return _hdr(0x2F, 7, seq) + body


def notify_prof(slot, add, seq=0):
    """32 01: u16 book slot, u32 add -> B+0x1a4 += add (0x203c40)."""
    return _hdr(NOTIFY, 1, seq) + vce.put_u16(slot) + vce.put_u32(add)


def notify_training(slot, seq=0):
    """32 04: u16 -> SK+0xfe (training scroll slot; 0xffff = none)."""
    return _hdr(NOTIFY, 4, seq) + vce.put_u16(slot)


def notify_book(slot, b, seq=0):
    """32 02: a book entry (0x1ef480 -> UI 0x2c -> 0x203a00: SK+0x36[slot] = scroll, slot filled)."""
    return _hdr(NOTIFY, 2, seq) + w_book(slot, b)


# ---------------------------------------------------------------------------- client models (replies / pushes)
def _r_r(r):
    return dict(id=r.u32(), flags=r.u16(), b6=r.u8(), b7=r.u8())


def _r_arr(r, width, maxn):
    n = r.length()
    if n > maxn:
        raise vce.WireError(f"array of {n} > {maxn}")
    return [(r.u16() if width == 2 else r.u32()) for _ in range(n)]


def _r_str(r, keep):
    s = r.bytes(0x200).decode("utf-8")
    return s, len(s.encode("cp932")) <= keep


def parse(payload):
    """Client read model of the S>C frames this module sends (cmd 0x26 subs 1..4, 8; 0x32 subs 1, 2, 4; 2F 07).
    'left' = unread bytes (0 expected)."""
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    d = dict(cmd=payload[0], sub=sub, seq=seq)
    cmd = payload[0]
    if cmd == CMD and sub == 1:
        n = r.u8()
        if n > 11:
            raise vce.WireError("N > 11")
        rd = lambda: dict(slot=r.u8(), book=r.u8(), idx=r.u8(), r=_r_r(r))
        d["cur"] = [rd() for _ in range(n)]
        d["grid"] = [[rd() for _ in range(MAX_SLOTS)] for _ in range(SETS)]
    elif cmd == CMD and sub == 2:
        d["training"] = r.u16()
        d["scrolls"] = _r_arr(r, 2, MAX_BOOKS)
        m = r.i8()
        d["books"] = []
        for _ in range(max(0, m)):
            e = dict(slot=r.u16(), b2=r.u8(), b3=r.u8(), prof=r.u16(), scroll=r.u16())
            e["skills"] = _r_arr(r, 4, MAX_PER_BOOK)
            d["books"].append(e)
    elif cmd == CMD and sub == 3:
        d["res"] = r.i8()
        if d["res"] >= 0:
            d["id"] = r.u32()
            d["label"], d["label_fits"] = _r_str(r, 0x10)
            d["text"], d["text_fits"] = _r_str(r, 0x34)
            d["ki"] = r.u16()
    elif cmd == CMD and sub == 4:
        d["res"] = r.i8()
        if d["res"] >= 0:
            d["u16"] = r.u16()
            d["skills"] = [_r_r(r) for _ in range(4)]
            if not r.done():
                d["b24"] = r.u8()
    elif cmd == CMD and sub == 8:
        d["training"] = r.u16()
    elif cmd == CMD and sub == 5:
        d["slot"], d["packed"] = r.u8(), r.u32()
    elif cmd == NOTIFY and sub == 1:
        d["slot"], d["add"] = r.u16(), r.u32()
    elif cmd == NOTIFY and sub == 4:
        d["training"] = r.u16()
    elif cmd == NOTIFY and sub == 2:
        e = dict(slot=r.u16(), b2=r.u8(), b3=r.u8(), prof=r.u16(), scroll=r.u16())
        e["skills"] = _r_arr(r, 4, MAX_PER_BOOK)
        d["book"] = e
    elif cmd == 0x2F and sub == 7:
        d["stats"] = [r.u16() for _ in range(9)]
        d["u14"], d["u18"], d["u1c"], d["slots"] = r.u32(), r.u32(), r.u16(), r.u8()
        d["tail"] = [r.u16() for _ in range(8)]
    else:
        raise ValueError(f"{cmd:#x} {sub} not modelled")
    d["left"] = len(r.rest())
    return d


# ---------------------------------------------------------------------------- battle interface
def menu_entries(c):
    """The Skills children for the battle menu: learned skills (DESIGN: all learned ones, bar order first)."""
    if not c or not enabled() or os.environ.get("NOBU_SKILL_BATTLE", "1") != "1":
        return []
    ids = list(dict.fromkeys(list(bar(c).values()) + learned(c)))[:20]     # 30 items max incl. Attack / Flee
    out = []
    for sid in ids:
        s = SKILLS.get(sid)
        if s and s["kind"] != "revive":                  # field-only skills stay out of the battle menu
            out.append(dict(value=sid, name=name(sid), ttype=s["ttype"], ki=s["ki"], kind=s["kind"]))
    return out


def menu_items(c):
    """22 33 frames (category 5 = the Skills node): value = skill id (comes back as the 22 01 action), target
    type, Ki cost in the u16 extra cost (the client greys the entry when it exceeds the unit's Ki)."""
    from . import battle
    return [battle.p_menu_item(e["value"], CATEGORY, e["name"], ttype=e["ttype"], cost=e["ki"])
            for e in menu_entries(c)]


def is_skill(action):
    return action in SKILLS


def resolve(c, action, actor=None, target=None, rng=None):
    """The effect of a skill command, or None when the action is not one of this character's skills.
    Without actor / target: the static description. With battle units (dicts with hp, mhp, ki, c = combat
    stats as battle.player_combat / enemy_combat): the rolled outcome (DESIGN formulas):
      damage  atk x power x U(0.9, 1.1) - def / 2 (pierce: no defence; crit: 35 % x1.5)
      arts    (3 x Int + 2 x level) x power x U(0.9, 1.1) - def / 4, never misses
      drain   arts damage, heals half of it
      heal    max Life x power + 2 x Int
      buff    the actor's combat stat x (1 + pct / 100) for the rest of the battle
      debuff  arts-like damage x power and the target's stat x (1 + pct / 100)
    Returns dict(ok, id, name, kind, ki, self, scene, dur, dmg, heal, buffs {stat: new value}, debuffs, outcome,
    reason) - ok False (reason 'ki') when the actor lacks Ki; the caller then falls back to a plain attack."""
    s = SKILLS.get(action)
    if not s or not c or not enabled() or action not in learned(c):
        return None
    scene = _env_int("NOBU_SKILL_SCENE", s["scene"])
    eff = dict(ok=True, id=action, name=name(action), kind=s["kind"], ki=s["ki"], self=s["ttype"] == SELF,
               scene=scene, dur=s["dur"], dmg=0, heal=0, buffs={}, debuffs={}, outcome="skill", reason="")
    if actor is None:
        return eff
    if int(actor.get("ki", 0)) < s["ki"]:
        return dict(eff, ok=False, reason="ki")
    import random
    rng = rng or random.Random()
    a = actor.get("c") or {}
    lv, intel = int(a.get("level", 1)), intellect(c)
    d = (target or {}).get("c") or {}
    k = s["kind"]
    if k in ("damage",):
        base = a.get("atk", 1) * s["power"] * rng.uniform(0.9, 1.1)
        dmg = base - (0 if s["stat"] == "pierce" else d.get("dfn", 0) / 2)
        if s["stat"] == "crit" and rng.random() < 0.35:
            dmg, eff["outcome"] = dmg * 1.5, "crit"
        eff["dmg"] = max(1, round(dmg))
    elif k in ("arts", "drain", "debuff"):
        dmg = (3 * intel + 2 * lv) * s["power"] * rng.uniform(0.9, 1.1) - d.get("dfn", 0) / 4
        eff["dmg"] = max(1, round(dmg))
        if k == "drain":
            eff["heal"] = eff["dmg"] // 2
        if k == "debuff":
            eff["debuffs"] = {st: max(1, round(d.get(st, 1) * (1 + pct / 100))) for st, pct in s["buff"].items()}
    elif k == "heal":
        eff["heal"] = max(1, round(actor.get("mhp", 0) * s["power"] + 2 * intel))
    elif k == "buff":
        eff["buffs"] = {st: max(1, round(a.get(st, 1) * (1 + pct / 100))) for st, pct in s["buff"].items()}
    if target is not None and eff["dmg"]:
        eff["dmg"] = min(eff["dmg"], int(target.get("hp", eff["dmg"])))
    return eff


def apply(eff, actor, target):
    """Apply a resolved effect to the battle units (Ki, Life, stats). Returns the unit that was the target."""
    actor["ki"] = max(0, int(actor["ki"]) - eff["ki"])
    tgt = actor if eff["self"] else target
    if eff["dmg"] and target is not None:
        target["hp"] = max(0, int(target["hp"]) - eff["dmg"])
    if eff["heal"]:
        actor["hp"] = min(int(actor["mhp"]), int(actor["hp"]) + eff["heal"])
    actor.setdefault("c", {}).update(eff["buffs"])
    if target is not None and eff["debuffs"]:
        target.setdefault("c", {}).update(eff["debuffs"])
    return tgt


# ---------------------------------------------------------------------------- cmd 0x26 requests
def answer(payload, ctx, store=None):
    """cmd 0x26 with a loaded character (None otherwise: chars.WORLDLOAD_EMPTY / misc.py answer), and the
    "skill" chat commands. None = not ours."""
    if not enabled() or len(payload) < 4:
        return None
    c = (ctx or {}).get("char")
    if payload[:2] == b"\x23\x08":
        return chat(payload, ctx, store)
    if payload[0] != CMD or not c:
        return None
    sub = payload[1]
    seq = vce.Reader(payload, 2).u16()
    rest = bytes(payload[4:])
    st = state(c, store)
    if sub == 1:
        out = [standby_reply(seq, c)]
        if os.environ.get("NOBU_SKILL_STATUS", "1") == "1":
            out.append(status_push(c))
        return out
    if sub == 2:
        return [booklist_reply(seq, c)]
    if sub == 3 and len(rest) >= 4:
        return [skill_info_reply(seq, int.from_bytes(rest[:4], "big"), c)]
    if sub == 4 and len(rest) >= 4:
        return [book_info_reply(seq, int.from_bytes(rest[:4], "big"), c)]
    if sub == 8 and len(rest) >= 4:
        slot = int.from_bytes(rest[:4], "big")
        if 0 <= slot < len(st["books"]) and not mastered(st["books"][slot]):
            st["training"] = slot
            _save(store)
            note = [social.notify_system(f"Training scroll: 《{scroll_name(st['books'][slot]['scroll'])}》.")]
        else:
            note = []
        tr = st.get("training")
        return [_hdr(CMD, 8, seq) + vce.put_u16(NO_TRAINING if tr is None else tr)] + note
    if sub == 5 and len(rest) >= 5:
        slot, pk = rest[0], int.from_bytes(rest[1:5], "big")
        bi = unpack(pk)
        ok = (bi is not None and slot < slot_count(c) and 0 <= bi[0] < len(st["books"])
              and bi[1] < learned_count(st["books"][bi[0]]))
        if ok:
            st["bar"] = {k: v for k, v in st["bar"].items() if v != pk}
            st["bar"][str(slot)] = pk
            _save(store)
            return [_hdr(CMD, 5, seq) + bytes([slot]) + vce.put_u32(pk)]
        return [_hdr(CMD, 5, seq) + bytes([slot]) + vce.put_u32(0), social.notify_system("Cannot use that skill yet.")]
    if sub == 0xA and len(rest) >= 4:
        s_, slot, code = rest[0], rest[1], int.from_bytes(rest[2:4], "big")
        if s_ < SETS:
            g = st.setdefault("grid", [[0xFFFF] * MAX_SLOTS for _ in range(SETS)])
            if code:
                if slot < MAX_SLOTS:
                    g[s_][slot] = code
            else:
                row = [0xFFFF] * MAX_SLOTS
                for sl, sid in bar(c).items():
                    b, i = locate(c, sid)
                    row[sl] = b * 4 + i
                g[s_] = row
        from . import misc
        return [misc.skill_reply(payload, ctx, store)]
    if 3 <= sub <= 0xC:
        from . import misc
        rep = misc.skill_reply(payload, ctx, store)
        return None if rep is None else [rep]
    return None


# ---------------------------------------------------------------------------- chat (debug commands)
def _parse_chat(text):
    t = unicodedata.normalize("NFKC", text or "").strip().lower().lstrip("/!").strip()
    toks = t.split()
    if not toks or toks[0] not in ("skill", "skills"):
        return None
    if len(toks) >= 3 and toks[1] in ("prof", "train"):
        try:
            return "prof", int(toks[2])
        except ValueError:
            return None
    if len(toks) >= 2 and toks[1] == "reset":
        return "reset", 0
    return "show", 0


def chat(payload, ctx, store=None):
    """"skill" / "skills": list scrolls, proficiency and learned skills; "skill prof <n>": add n proficiency
    to the training scroll (debug command, pushes 32 01 at once); "skill reset": back to the starter scroll."""
    c = (ctx or {}).get("char")
    if not c or os.environ.get("NOBU_SKILL_CHAT", "1") != "1":
        return None
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    got = _parse_chat(social.parse_line(line)[1])
    if got is None:
        return None
    out = []
    if got[0] == "reset":
        c.pop("skills", None)
        state(c, store)
        out.append(social.notify_system("Skills reset (re-enter the world to refresh the window)."))
    elif got[0] == "prof":
        st = state(c, store)
        res = train(ctx, got[1], store, rate=1)
        if res is None:
            out.append(social.notify_system("No training scroll set." if st.get("training") is None
                                            else "Nothing to train."))
        out += flush(ctx)
    st = state(c, store)
    for i, b in enumerate(st["books"]):
        n = len(book_skills(b))
        tag = " (training)" if st.get("training") == i else ""
        out.append(social.notify_system(f"{scroll_name(b['scroll'])}{tag}: {b['prof']}/{PROF_PER_SKILL * n}, "
                                        f"learned {learned_count(b)}/{n}"))
    names = ", ".join(f"{name(s)} (Ki {SKILLS[s]['ki']})" for s in learned(c)) or "none"
    out.append(social.notify_system(f"Skills: {names}. Slots {len(bar(c))}/{slot_count(c)}."))
    return out


# ---------------------------------------------------------------------------- describe
def describe(payload, direction="C>S"):
    if len(payload) < 4 or payload[0] not in (CMD, NOTIFY, 0x2F):
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x2F and (direction != "S>C" or sub != 7):
        return None
    if cmd == NOTIFY and direction != "S>C":
        return None
    try:
        if direction == "S>C":
            d = parse(payload)
            body = " ".join(f"{k}={v}" for k, v in d.items() if k not in ("cmd", "sub", "seq", "grid", "left"))
            name_ = (SUB_NAMES if cmd == CMD else NOTIFY_NAMES).get(sub, f"sub {sub}") if cmd != 0x2F else "STATUS"
            return f"SKILL S>C {cmd:02x} {sub:02x} {name_} seq={d['seq']} {body}"
        r = vce.Reader(payload, 2)
        return f"SKILL {SUB_NAMES.get(sub, f'sub {sub}')} seq={r.u16()} {r.rest().hex()}"
    except (vce.WireError, ValueError):
        return None
