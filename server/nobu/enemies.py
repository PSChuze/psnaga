"""Field enemies: data, per-zone spawns, visibility, respawn, wandering, and the interface battle.py uses.

Data: $NOBU_DATA/enemies.json, created from enemies_seed.json (from the wiki's Owari enemy
table, STRDAT and CMAP010; every value carries its source).

How an enemy is drawn (KNOWN chain): a uid-0 CHAR_INFO record (0B 0a) with a 0xfe cid reaches UI 0xd4 (entry +
actor); faction 0 and record +0x6b = 0 make it attackable (0x2733a0), so ○ on it shows Attack and sends 25 01 /
28 01 like the debug Bandit (battle.py). Human enemies are drawn as a person with fixed face / hair / build
(npc.npc_looks styles). Animals carry a creature model id (CREATURE_MODELS below): CHAR_INFO u16 +0x54 = the MOB
model id (1-based row of the CHAROFFS.PAK table = MOBLOW.PAK record), u8 +0x56 = its scale (50 = 1.0); the
battle unit identity carries the same id. Removal: 0B 04 uid 0 + cid (UI 0xdf). Movement: 0B 08 kind 6 (UI 0xd6,
players.notify_update).

Cids: ENEMY_BASE (0xfec00000) + 1 + the spawn's index in the data, so they never meet NPCs (0xfea.....),
the debug Bandit (0xfeb00001) or players (0xfe000000 | cid, cid < 0xa00000).

State: WORLD[zone][cid] = one spawn (a field symbol = the leader of a group of `count`), shared by every
session: alive / dead_until (respawn), engaged (the session fighting it), level rolled per life. Each session
sees the alive, free spawns within NOBU_ENEMY_VIEW of its last position (at most NOBU_ENEMY_MAX); they sit in
ctx["enemies"] (with the shared "pos" list) where battle.py and npc.resolve_target look them up.

Interface for battle.py (which calls these):
  is_enemy_cid(cid)                     True for cids of this module
  get(cid)                              {cid, id, name, name_jp, level, life, ki, count, rewards{exp, prof, fame,
                                         money}, drops, side, aggressive} or None
  engage(ctx, cid)                      claim it for this session's battle (False: someone else is fighting it);
                                        hides it from the other sessions
  release(ctx, cid)                     battle lost / fled: free it again (shown again to the others)
  on_defeated(ctx, cid, store=None)     battle won: dead, respawn timer, drops; returns the frames for this
                                        session (0B 04 + system lines)
World hooks (world.py): spawn_pushes(ctx) after 23 5a, on_move(ctx, m) on 05 05.

Env:
  NOBU_ENEMIES          auto (default: on when NOBU_BATTLE_FIGHT=1) | 1 | 0
  NOBU_ENEMIES_FILE     data file (default $NOBU_DATA/enemies.json, seeded from enemies_seed.json)
  NOBU_ENEMY_TOWN       1 (default) = also the test spawns in Nagoya (map 110, next to the town spawn point)
  NOBU_ENEMY_NAMES      en (default) | jp: which name the field and the battle show
  NOBU_ENEMY_VIEW       view radius in world units (default 25000); NOBU_ENEMY_MAX visible cap (default 24)
  NOBU_ENEMY_RESPAWN    respawn seconds for every enemy (default: the data's per-enemy value)
  NOBU_ENEMY_DROPS      1 (default) = roll drops on a win and put them in the backpack
  NOBU_ENEMY_WANDER     1 = wander (0B 08 kind 6, dir in degrees) every NOBU_ENEMY_WANDER_SECS (2): walk to goals
                        within NOBU_ENEMY_WANDER_RADIUS (600) of home, NOBU_ENEMY_WANDER_STEP (150) units a tick,
                        NOBU_ENEMY_WANDER_MOVE (0.5) chance an idle one sets off, NOBU_ENEMY_WANDER_STATE (0) the
                        u8 after dir, frozen within NOBU_ENEMY_WANDER_FREEZE (800) of a fighting player
  NOBU_ENEMY_AGGRO      1 = aggressive (red) enemies start a battle (needs NOBU_BATTLE_FIGHT=1) when the player is
                        within NOBU_ENEMY_AGGRO_RANGE (500) x arc (front 1, sides 1/2, back 1/3) x level factor
                        (1; out-levelled by d: 0.5 * (1 - d / NOBU_ENEMY_AGGRO_LEVEL_GAP (10)); 0 from the gap on).
                        Never on ghosts, while fighting, NOBU_ENEMY_AGGRO_CALM_SECS (10) after a battle,
                        NOBU_ENEMY_AGGRO_GRACE_SECS (5) after a map load, or outside province fields
                        (NOBU_ENEMY_AGGRO_TOWN=1 allows towns; NOBU_ENEMY_AGGRO_ALL=1 makes yellow enemies attack)
  NOBU_ENEMY_NIGHT      auto (default: when upkeep's clock runs) | 1 | 0: night_only enemies exist only at in-game
                        night (outside 05:00..18:04, the client's own day test 0x67bcb0)
  NOBU_ENEMY_B6C        "red,yellow" CHAR_INFO +0x6c values (default 0,0; name colour experiment, name_byte())
  NOBU_ENEMY_MODELS     1 (default) = animals use their creature model; 0 = every enemy is drawn as a person
  NOBU_ENEMY_MODEL_SCALE  force the +0x56 scale byte of every creature (default: the data, 50 = authored size)
  NOBU_ENEMY_MODEL_KINDS  at most this many distinct creature models in one client's view (default 3 = the
                        field's small model cache slots 0..2; 0 = no cap)
"""
import copy
import json
import math
import os
import random
import shutil
import time

from . import persist
from . import chars, misc, npc, players, social, vce

ENEMY_BASE = 0xFEC00000
ENEMY_END = 0xFED00000                     # exclusive
SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "enemies_seed.json")
NAME_MAX = 0x10                            # CHAR_INFO str family (max 0x11 with the NUL)
RESYNC_STEP = 500                          # world units moved before the visible set is recomputed

WORLD = {}                                 # zone -> {cid: spawn}
TICKERS = {}                               # zone -> wander ticker generation
_DATA = {"path": None, "mtime": None, "data": None}

# Creature models, as the client reads them: CHAR_INFO u16 +0x54 -> P+0x58 ->
# appearance struct A[0..1] (0x1db0d0); 0x253dd0 builds the human body (0x2547a0) when it is 0, else the MOB
# model (0x256410): row id of CHAROFFS.PAK part 2 (100-byte rows, 145 rows, gp-0x7578) and record id-1 of
# MOBLOW.PAK (file 0x26; offsets = CHAROFFS part 4). The field loads those records on demand (World.bin 0x60b2c0:
# small models in cache slots 0..2, big ones in 3..4). u8 +0x56 -> A[0xa] -> scale of the creature (0x255ed0:
# value * 2 / 100). Rows 1..65 (and 103, 130..138) are human-shaped NPC bodies; 66+ are creatures. Which row is
# which animal: skeleton, bounding box and texture of each MOBLOW record. Rows 68 / 75 / 106 are copies of the
# snake 66 with placeholder motions, so every snake uses 66.
SNAKE, RAT, WEASEL, BAT, SPIDER, WOLF, DOG, BUG = 66, 71, 110, 76, 67, 69, 74, 81
CREATURE_MODELS = {
    "rat": dict(model=RAT, src="MOBLOW 71: rodent texture (brown rat face), long tail, 43x57x249"),
    "snake": dict(model=SNAKE, src="MOBLOW 66: 25-bone chain 11x10x412, snake-scale texture"),
    "gulp_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "viper": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "hachimaki_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "white_oni_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "black_oni_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "red_oni_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "tsuto_snake": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "great_viper": dict(model=SNAKE, src="MOBLOW 66 (the only snake mesh)"),
    "nezu_bat": dict(model=BAT, src="MOBLOW 76: bat wings (span 219) and bat-face texture"),
    "big_bat": dict(model=BAT, src="MOBLOW 76 (the only bat)"),
    "poison_spider": dict(model=SPIDER, src="MOBLOW 67: flat 293x68x300, 63 bones (legs); spider HYPOTHESIS"),
    "wild_dog": dict(model=DOG, src="MOBLOW 74: canine (motions shared with 69 / 111), tan dog / fox face; "
                     "dog vs fox HYPOTHESIS"),
    "maneater_wolf": dict(model=WOLF, src="MOBLOW 69: canine, grey wolf texture"),
    "lone_wolf": dict(model=WOLF, src="MOBLOW 69: canine, grey wolf texture"),
    "okuri_weasel": dict(model=WEASEL, src="MOBLOW 110: rodent family (motions shared with the rat 71), long low "
                         "body 49x80x312; weasel HYPOTHESIS"),
    "firefly_bug": dict(model=BUG, src="MOBLOW 81: segmented insect-like body, glowing eye; HYPOTHESIS"),
}
MODEL_SCALE = 50                           # +0x56 / identity +0x30: 50 = the authored size (0x255ed0)


# ---------------------------------------------------------------------------- knobs
def _env_int(name, default):
    v = os.environ.get(name)
    return default if v in (None, "") else int(v, 0)


def _env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def enabled():
    v = os.environ.get("NOBU_ENEMIES", "auto").lower()
    if v == "auto":
        return os.environ.get("NOBU_BATTLE_FIGHT", "0") == "1"
    return v == "1"


def town_enabled():
    return os.environ.get("NOBU_ENEMY_TOWN", "1") == "1"


def is_enemy_cid(cid):
    return isinstance(cid, int) and ENEMY_BASE < cid < ENEMY_END


# ---------------------------------------------------------------------------- data
def data_path():
    return os.environ.get("NOBU_ENEMIES_FILE") or os.path.join(misc.data_dir(), "enemies.json")


def seed():
    with open(SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _valid(d):
    return isinstance(d, dict) and isinstance(d.get("enemies"), dict) and isinstance(d.get("spawns"), list)


def load_data(path=None):
    """enemies.json: created from the seed when missing; replaced by a newer seed (old file kept as
    enemies.json.v<N>.bak) unless it has "custom": true. Cached until the file changes."""
    path = path or data_path()
    sd = None
    if not os.path.exists(path):
        sd = seed()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            persist.atomic_write_json(path, sd, ensure_ascii=False, indent=1)
        except OSError:
            return sd
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return sd or seed()
    if _DATA["path"] == path and _DATA["mtime"] == mtime and _DATA["data"] is not None:
        return _DATA["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        persist.warn_once((path, repr(e)), f"{path} does not parse ({e!r}); using the built-in seed")
        data = None
    if not _valid(data):
        return sd or seed()
    sd = sd or seed()
    if not data.get("custom") and int(data.get("seed_version", 0)) < int(sd.get("seed_version", 0)):
        try:
            shutil.copyfile(path, f"{path}.v{data.get('seed_version', 0)}.bak")
            persist.atomic_write_json(path, sd, ensure_ascii=False, indent=1)
            mtime = os.path.getmtime(path)
        except OSError:
            pass
        data = sd
    _DATA.update(path=path, mtime=mtime, data=data)
    return data


def reset():
    """Forget the world state and the data cache (tests, reload)."""
    WORLD.clear()
    TICKERS.clear()
    _DATA.update(path=None, mtime=None, data=None)


def definition(eid, data=None):
    return (data or load_data())["enemies"].get(eid)


def display_name(e):
    name = e.get("name_jp") if os.environ.get("NOBU_ENEMY_NAMES", "en") == "jp" else e.get("name")
    return misc.clip_sjis(name or e.get("name_jp") or "Enemy", NAME_MAX)


def models_enabled():
    return os.environ.get("NOBU_ENEMY_MODELS", "1") != "0"


def model_of(eid, e=None):
    """(model id, scale) of enemy `eid`, or (0, 0) for a person. The data's "model" key wins (an int id, or
    {"id", "scale"}; 0 = person), else CREATURE_MODELS. NOBU_ENEMY_MODELS=0 turns every enemy into a person."""
    if not models_enabled():
        return 0, 0
    e = e if e is not None else (definition(eid) or {})
    m = e.get("model", CREATURE_MODELS.get(eid, {}).get("model", 0))
    scale = MODEL_SCALE
    if isinstance(m, dict):
        scale = int(m.get("scale", MODEL_SCALE))
        m = m.get("id", 0)
    m = int(m or 0) & 0xFFFF
    if not m:
        return 0, 0
    if os.environ.get("NOBU_ENEMY_MODEL_SCALE"):
        scale = _env_int("NOBU_ENEMY_MODEL_SCALE", MODEL_SCALE)
    return m, scale & 0xFF


# ---------------------------------------------------------------------------- world state
def _roll(s, e):
    rng = random.Random((s["cid"] << 8) ^ s["gen"])
    lo, hi = (list(e.get("level", [1, 1])) + [1, 1])[:2]
    clo, chi = (list(e.get("count", [1, 1])) + [1, 1])[:2]
    s["level"] = rng.randint(int(lo), max(int(lo), int(hi)))
    s["count"] = rng.randint(int(clo), max(int(clo), int(chi)))


def populate(zone, data=None):
    """The zone's spawns (created on first use). Test spawns only with NOBU_ENEMY_TOWN=1."""
    if zone in WORLD:
        return WORLD[zone]
    data = data or load_data()
    out = {}
    for i, sp in enumerate(data["spawns"]):
        if int(sp.get("zone", -1)) != zone or sp.get("enemy") not in data["enemies"]:
            continue
        if sp.get("test") and not town_enabled():
            continue
        if sp.get("disabled"):
            continue
        x, y, z = [int(v) for v in sp["pos"]]
        cid = ENEMY_BASE + 1 + i
        s = dict(cid=cid, zone=zone, enemy=sp["enemy"], home=(x, y, z),
                 pos=[zone, x, y, z, int(sp.get("dir", 0)) % 360], alive=True, dead_until=None,
                 engaged=None, gen=0)
        _roll(s, data["enemies"][sp["enemy"]])
        out[cid] = s
    WORLD[zone] = out
    return out


def spawn(cid):
    for z in WORLD.values():
        if cid in z:
            return z[cid]
    return None


def _session_of(ident):
    for c in social.SESSIONS.values():
        if id(c) == ident:
            return c
    return None


def _engaged_by_other(s, ctx):
    if s["engaged"] is None or s["engaged"] == id(ctx):
        return False
    if not social.SESSIONS or _session_of(s["engaged"]) is not None:
        return True
    s["engaged"] = None                          # its session is gone
    return False


def revive_due(zone, now=None):
    """Dead spawns whose timer ran out come back (new level roll). Returns the revived cids."""
    now = time.monotonic() if now is None else now
    out = []
    for s in WORLD.get(zone, {}).values():
        if not s["alive"] and s["dead_until"] is not None and s["dead_until"] <= now:
            s.update(alive=True, dead_until=None, engaged=None, gen=s["gen"] + 1)
            s["pos"][1:4] = list(s["home"])
            e = definition(s["enemy"])
            if e:
                _roll(s, e)
            out.append(s["cid"])
    return out


# ---------------------------------------------------------------------------- night-only enemies
DAY_START, DAY_END = 300, 1085             # KNOWN (0x67bcb0): hour*60+minute in [300, 1085) is "day"


def night_rules():
    """NOBU_ENEMY_NIGHT: auto (default) = night-only enemies follow upkeep's world clock when that clock runs;
    1 = always follow it; 0 = night-only enemies appear at any time (the old behaviour)."""
    v = os.environ.get("NOBU_ENEMY_NIGHT", "auto").lower()
    if v == "auto":
        from . import upkeep
        return upkeep.clock_enabled()
    return v == "1"


def is_night(now=None):
    """In-game night by upkeep.game_time (the same clock the client shows): outside 05:00..18:04."""
    from . import upkeep
    gt = upkeep.game_time(now)
    return not DAY_START <= gt["hour"] * 60 + gt["minute"] < DAY_END


def active(s, now=None):
    """A spawn exists now: every spawn, except a night_only one in daytime while night_rules() hold. One that
    is being fought stays until its battle ends."""
    e = definition(s["enemy"]) or {}
    if not e.get("night_only") or s.get("engaged") is not None or not night_rules():
        return True
    return is_night(now)


def night_flip(zone):
    """Push the visible-set change to the zone when day turns to night or back (wander ticker / on_move)."""
    n = is_night()
    key = ("night", zone)
    if TICKERS.get(key) is None:
        TICKERS[key] = n
        return False
    if TICKERS[key] != n:
        TICKERS[key] = n
        if night_rules():
            _push_all(zone)
        return True
    return False


def get(cid):
    """Battle stats of a spawned enemy (None for any other cid): the data's base values at the lowest level
    plus per_level for every level above it."""
    s = spawn(cid)
    if s is None:
        return None
    e = definition(s["enemy"])
    if e is None:
        return None
    d = s["level"] - int((e.get("level") or [1])[0])
    per = e.get("per_level") or {}
    rw = e.get("rewards") or {}

    def lv(v, k):
        return max(0, int(round(float(v) + float(per.get(k, 0)) * d)))
    return dict(cid=cid, id=s["enemy"], name=display_name(e), name_jp=e.get("name_jp", ""), level=s["level"],
                life=max(1, lv(e.get("life", 100), "life")), ki=lv(e.get("ki", 50), "ki"), count=s["count"],
                rewards={k: lv(rw.get(k, 0), k) for k in ("exp", "prof", "fame", "money")},
                # combat (battle.py DESIGN): data keys attack / defence (+ per_level), else 4+2*lv / 1+lv
                attack=lv(e["attack"], "attack") if "attack" in e else 4 + 2 * s["level"],
                defence=lv(e["defence"], "defence") if "defence" in e else 1 + s["level"],
                drops=copy.deepcopy(e.get("drops") or []), side=display_name(e),
                aggressive=bool(e.get("aggressive")), race=e.get("race", 0),
                # creature model for the battle unit identity (0 = person)
                model=model_of(s["enemy"], e)[0], model_scale=model_of(s["enemy"], e)[1])


# ---------------------------------------------------------------------------- per-viewer records
def view(s, viewer):
    """The char dict for CHAR_INFO / target info / battle.py, as `viewer` (a character) sees it: the viewer's
    looks with the enemy's style (face / hair / build), job and sex, faction 0 (attackable)."""
    e = definition(s["enemy"]) or {}
    lk_def = e.get("looks") or {}
    job = lk_def.get("job")
    lk = npc.npc_looks(viewer or {}, lk_def.get("style", "bandit"), job=job)
    if lk_def.get("sex") is not None:
        lk[2] = int(lk_def["sex"])
    lk[4] = 0                                    # faction 0 -> attackable (0x2733a0)
    model, scale = model_of(s["enemy"], e)
    return dict(cid=s["cid"], uid=0, family=display_name(e), given="", looks=lk, level=s["level"],
                pos=s["pos"], enemy=s["enemy"], b6b=0, b6c=name_byte(e), model=model, model_scale=scale)


def name_byte(e):
    """CHAR_INFO +0x6c (-> entry +0x2b) for a red (aggressive) / yellow enemy: NOBU_ENEMY_B6C = "red,yellow"
    (default "0,0" = unchanged). KNOWN (0x2733a0): entry +0x2b is a relation 0..5 (0 attackable, 1 attackable
    when +0x6b == 0, 2..5 not), but faction 0 returns "attackable" before it is read, so any value keeps an
    enemy attackable. HYPOTHESIS to test live: the field name colour follows this byte (the colour code was not
    found statically)."""
    try:
        red, yellow = [int(v, 0) for v in os.environ.get("NOBU_ENEMY_B6C", "0,0").split(",")[:2]]
    except ValueError:
        red, yellow = 0, 0
    return (red if e.get("aggressive") else yellow) & 0xFF


def _record(s, viewer):
    return chars.char_info_record(view(s, viewer), 0, tuple(s["pos"]))


def _zone(ctx):
    z = ctx.get("enemies_zone")
    if z is None:
        z = ctx.get("zone", chars.char_position(ctx.get("char"))[0])
    return z


def _xz(ctx):
    p = ctx.get("enemies_xz")
    if p:
        return p
    _, x, _, z, _ = chars.char_position(ctx.get("char"))
    return x, z


def _dist(s, x, z):
    return math.hypot(s["pos"][1] - x, s["pos"][3] - z)


def _mine(ctx):
    return {c: v for c, v in (ctx.get("enemies") or {}).items() if is_enemy_cid(c)}


def visible(ctx, radius=None):
    """cids this session should see: alive, not fought by someone else, within radius, nearest first."""
    zone = _zone(ctx)
    x, z = _xz(ctx)
    radius = _env_int("NOBU_ENEMY_VIEW", 25000) if radius is None else radius
    cand = [s for s in WORLD.get(zone, {}).values()
            if s["alive"] and active(s) and not _engaged_by_other(s, ctx) and _dist(s, x, z) <= radius]
    cand.sort(key=lambda s: _dist(s, x, z))
    return [s["cid"] for s in cand[:_env_int("NOBU_ENEMY_MAX", 24)]]


def _cap_kinds(new, zone, kept):
    """Drop new enemies whose creature model would be a distinct model beyond NOBU_ENEMY_MODEL_KINDS (default 3)
    in this client's view. KNOWN (World.bin 0x60b2c0 / 0x258620): the field streams small creature models into 3
    cache slots and only ever loads from the first 3 wanted ids, so a 4th model stays unloaded (invisible)."""
    cap = _env_int("NOBU_ENEMY_MODEL_KINDS", 3)
    if cap <= 0:
        return new
    z = WORLD.get(zone, {})

    def kind(cid):
        s = z.get(cid)
        return model_of(s["enemy"])[0] if s else 0
    kinds = {k for k in map(kind, kept) if k}
    out = []
    for cid in new:
        k = kind(cid)
        if k and k not in kinds:
            if len(kinds) >= cap:
                continue
            kinds.add(k)
        out.append(cid)
    return out


def sync(ctx):
    """Frames that bring this session's client in line with visible(): CHAR_INFO for new ones (16 per frame),
    0B 04 for the ones gone (dead, engaged elsewhere, or beyond 1.25 x the view radius)."""
    zone = _zone(ctx)
    have = _mine(ctx)
    want = visible(ctx)
    keep_r = _env_int("NOBU_ENEMY_VIEW", 25000) * 1.25
    x, z = _xz(ctx)
    ens = ctx.setdefault("enemies", {})
    gone = []
    for cid in have:
        s = WORLD.get(zone, {}).get(cid)
        if s is None or not s["alive"] or _engaged_by_other(s, ctx) or _dist(s, x, z) > keep_r \
                or (not active(s) and s["engaged"] != id(ctx)):
            gone.append(cid)
    out = []
    for cid in gone:
        ens.pop(cid, None)
        out.append(players.notify_delete(cid))
    new = _cap_kinds([cid for cid in want if cid not in ens], zone, [c for c in ens if c not in gone])
    recs = []
    for cid in new:
        s = WORLD[zone][cid]
        ens[cid] = view(s, ctx.get("char"))
        recs.append(_record(s, ctx.get("char")))
    out += players.notify_infos(recs)
    ctx["enemies_sync_xz"] = (x, z)
    ctx["enemies_night"] = is_night()
    return out


def _viewers(zone):
    return [c for c in social.SESSIONS.values() if c.get("char") and c.get("enemies_zone") == zone]


def _in_battle(ctx):
    return (ctx.get("battle") or {}).get("phase") in ("setup", "turns", "end")


def _push_all(zone, exclude=None):
    """sync() for every other session in the zone (not in a battle); frames go through their push."""
    n = 0
    for c in _viewers(zone):
        if c is exclude or _in_battle(c) or not c.get("push"):
            continue
        for f in sync(c):
            c["push"](f)
            n += 1
    return n


# ---------------------------------------------------------------------------- world hooks
def spawn_pushes(ctx):
    """world_ready_pushes hook (23 5a): the client has (re)loaded a field, so its old enemy entries are gone.
    Returns the CHAR_INFO frames for the enemies around the player."""
    if not enabled() or not ctx.get("char"):
        return []
    zone = ctx.get("zone", chars.char_position(ctx["char"])[0])
    ens = ctx.setdefault("enemies", {})
    for cid in list(ens):
        if is_enemy_cid(cid):
            del ens[cid]
    ctx["enemies_zone"] = zone
    _, x, _, z, _ = chars.char_position(ctx["char"])
    ctx["enemies_xz"] = (x, z)
    ctx["enemies_spawn_t"] = time.monotonic()      # aggro grace after a (re)load
    populate(zone)
    revive_due(zone)
    if os.environ.get("NOBU_ENEMY_WANDER") == "1":
        start_wander(zone)
    return sync(ctx)


def _battle_patched():
    from . import battle
    return hasattr(battle, "field_enemies")


def on_move(ctx, m):
    """05 05 hook: resync the visible set every RESYNC_STEP units; aggro check. Returns frames."""
    if not enabled() or ctx.get("enemies_zone") is None or not ctx.get("char"):
        return []
    ctx["enemies_xz"] = (m["x"], m["z"])
    if m.get("dir") is not None:
        ctx["enemies_dir"] = m["dir"]
    if _in_battle(ctx):
        return []
    zone = ctx["enemies_zone"]
    revived = revive_due(zone)
    out = []
    last = ctx.get("enemies_sync_xz")
    b = ctx.get("battle") or {}
    if b.get("phase") == "done" and ctx.get("enemies_calm_bid") != b.get("bid"):
        # first move after a battle: the green blink, no NPC attacks (PLAYABILITY 1.2) for a while
        ctx["enemies_calm_bid"] = b.get("bid")
        ctx["enemies_calm_until"] = time.monotonic() + _env_float("NOBU_ENEMY_AGGRO_CALM_SECS", 10)
    night_flip(zone)
    if ctx.get("enemies_night") is not None and ctx["enemies_night"] != is_night():
        last = None                                  # day <-> night: night-only enemies come or go
    if b.get("phase") == "done" and b.get("won") and ctx.get("enemies_seen_bid") != b.get("bid") \
            and not _battle_patched():
        # battle.finish of a won battle sends 0B 04 for every ctx["enemies"] entry (battle.despawn, before the
        # hook patch): forget ours so this sync draws them again
        ctx["enemies_seen_bid"] = b.get("bid")
        for cid in _mine(ctx):
            ctx["enemies"].pop(cid, None)
        last = None
    if revived or last is None or math.hypot(m["x"] - last[0], m["z"] - last[1]) >= RESYNC_STEP:
        out += sync(ctx)
    if revived:
        _push_all(zone, exclude=ctx)
    return out + aggro(ctx)


# ---------------------------------------------------------------------------- battle interface
def engage(ctx, cid):
    """Claim spawn cid for this session's battle. False if it is dead or another live session fights it.
    The other sessions stop seeing it (0B 04 through their push)."""
    s = spawn(cid)
    if s is None or not s["alive"] or _engaged_by_other(s, ctx) or not active(s):
        return False
    s["engaged"] = id(ctx)
    _push_all(s["zone"], exclude=ctx)
    return True


def release(ctx, cid):
    """The battle ended without a win (lost / fled / exit): the spawn is free and visible to everyone again."""
    s = spawn(cid)
    if s is None or s["engaged"] not in (None, id(ctx)):
        return []
    s["engaged"] = None
    _push_all(s["zone"], exclude=ctx)
    return []


def on_defeated(ctx, cid, store=None):
    """The battle against spawn cid was won: it dies for everyone, comes back after its respawn time (pushed
    through a timer when a loop runs, else lazily on the next move / zone load), drops go to the backpack.
    Returns this session's frames: 0B 04 (remove the symbol) and one system line per drop."""
    s = spawn(cid)
    if s is None:
        return []
    e = definition(s["enemy"]) or {}
    stats = get(cid)
    secs = _env_int("NOBU_ENEMY_RESPAWN", int(e.get("respawn", 60)))
    s.update(alive=False, engaged=None, dead_until=time.monotonic() + secs)
    (ctx.get("enemies") or {}).pop(cid, None)
    out = [players.notify_delete(cid)]
    _push_all(s["zone"], exclude=ctx)
    _schedule(secs, lambda: _respawn_push(s["zone"]))
    if stats and os.environ.get("NOBU_ENEMY_DROPS", "1") == "1":
        out += give_drops(ctx, store, roll_drops(stats))
    ctx.setdefault("enemies_log", []).append(("defeated", cid, s["enemy"], secs))
    return out


def roll_drops(stats, rng=None):
    rng = rng or random
    return [d for d in stats.get("drops", []) if rng.random() < float(d.get("rate", 0))]


def give_drops(ctx, store, drops):
    """Add drops to the character's inventory (items.py format): top up a stack, else a free backpack slot.
    Returns system lines ("Obtained ..." / "... was lost: backpack full")."""
    c = ctx.get("char")
    if not c or not drops:
        return []
    from . import items
    inv = items.inventory(c, store)
    out = []
    for d in drops:
        name = items.item_name(d["id"])                 # by id: a drop's "name" is the wiki's Japanese (provenance)
        stack = bool(d.get("stack", True))
        it = next((i for i in inv["items"] if stack and i["id"] == d["id"] and i.get("flags", 0) & items.FLAG_STACK
                   and int(i.get("count", 1)) < items.MAX_STACK), None)
        if it is not None:
            it["count"] = int(it.get("count", 1)) + 1
        else:
            used = {i.get("idx") for i in inv["items"]}
            free = [i for i in range(items.CAPACITY) if i not in used]
            if not free:
                out.append(misc.notify_system(f"{name} was lost: backpack full."))
                continue
            known = items.stats_data().get(int(d["id"])) or {}       # equipment: wiki durability / category
            dur = int(d.get("dur", 0 if stack else items.new_durability(int(d["id"]))))
            cat = int(d.get("cat", 0 if stack else known.get("cat", 0)))
            inv["items"].append(dict(id=d["id"], type=int(d.get("type", 54)), cat=cat, count=1,
                                     flags=items.FLAG_STACK if stack else 0, dur=dur, dur_max=dur, idx=free[0]))
        out.append(misc.notify_system(f"Obtained {name}."))
    items.save(store)
    return out


# ---------------------------------------------------------------------------- timers
def _schedule(delay, fn):
    """fn() after delay s on the running loop; False without one (tests: the lazy path revives)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.call_later(delay, fn)
    return True


def _respawn_push(zone):
    if revive_due(zone):
        _push_all(zone)


def start_wander(zone):
    """One ticker per zone (NOBU_ENEMY_WANDER=1): every NOBU_ENEMY_WANDER_SECS wander_step(); also notices
    day <-> night for night-only enemies. Stops when nobody is left in the zone."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if TICKERS.get(zone):
        return True
    TICKERS[zone] = gen = TICKERS.get(("gen", zone), 0) + 1
    TICKERS[("gen", zone)] = gen
    secs = max(0.5, _env_float("NOBU_ENEMY_WANDER_SECS", 2))

    def tick():
        if TICKERS.get(zone) != gen:
            return
        if not _viewers(zone):
            TICKERS[zone] = None
            return
        night_flip(zone)
        wander_step(zone)
        loop.call_later(secs, tick)
    loop.call_later(secs, tick)
    return True


def heading(dx, dz):
    """Facing in the client's unit: whole degrees 0..359 of atan2(dx, dz). KNOWN unit (the local player's 05 05
    dir is (int) atan2(m[+0x20], m[+0x28]) * 180 / pi wrapped to [0, 360), 0x5fc7b0; entries keep it as a u16,
    0x1d9a10); that m[+0x20] / m[+0x28] are the x / z of the forward axis is a HYPOTHESIS (0 = facing +z)."""
    return int(round(math.degrees(math.atan2(dx, dz)))) % 360


def wander_step(zone, rng=None):
    """One wander tick. A free enemy either stands (pause), or walks towards a goal inside
    NOBU_ENEMY_WANDER_RADIUS (600) of its home by at most NOBU_ENEMY_WANDER_STEP (150) units per tick, facing
    where it goes; at the goal it pauses and later picks a new one (NOBU_ENEMY_WANDER_MOVE, 0.5 = the chance per
    tick that an idle enemy sets off). Engaged, dead or inactive (night-only by day) enemies do not move, nor
    does one within NOBU_ENEMY_WANDER_FREEZE (800) of a player who is fighting. Viewers get 0B 08 kind 6 with the
    state byte NOBU_ENEMY_WANDER_STATE (0, as players.notify_update sends for other players). Returns the cids
    that moved."""
    rng = rng or random
    radius = _env_int("NOBU_ENEMY_WANDER_RADIUS", 600)
    step = max(1, _env_int("NOBU_ENEMY_WANDER_STEP", 150))
    go = _env_float("NOBU_ENEMY_WANDER_MOVE", 0.5)
    freeze = _env_int("NOBU_ENEMY_WANDER_FREEZE", 800)
    fighting = [_xz(c) for c in _viewers(zone) if _in_battle(c)]
    moved = []
    for s in WORLD.get(zone, {}).values():
        if not s["alive"] or s["engaged"] is not None or not active(s):
            continue
        if any(_dist(s, x, z) <= freeze for x, z in fighting):
            continue
        goal = s.get("goal")
        if goal is None:
            if rng.random() >= go:
                continue
            hx, hy, hz = s["home"]
            a, r = rng.uniform(0, 2 * math.pi), rng.uniform(0, radius)
            goal = s["goal"] = (int(hx + r * math.sin(a)), int(hz + r * math.cos(a)))
        dx, dz = goal[0] - s["pos"][1], goal[1] - s["pos"][3]
        dist = math.hypot(dx, dz)
        if dist <= step:
            nx, nz = goal
            s["goal"] = None                         # arrived: idle until the next roll
        else:
            nx, nz = int(s["pos"][1] + dx * step / dist), int(s["pos"][3] + dz * step / dist)
        if (nx, nz) == (s["pos"][1], s["pos"][3]):
            continue
        s["pos"][4] = heading(nx - s["pos"][1], nz - s["pos"][3])
        s["pos"][1], s["pos"][3] = nx, nz
        moved.append(s["cid"])
    if moved:
        state = _env_int("NOBU_ENEMY_WANDER_STATE", 0) & 0xFF
        for c in _viewers(zone):
            if _in_battle(c) or not c.get("push"):
                continue
            for cid in moved:
                if cid in (c.get("enemies") or {}):
                    c["push"](players.notify_update(cid, tuple(WORLD[zone][cid]["pos"]), state=state))
    return moved


# ---------------------------------------------------------------------------- aggro (NOBU_ENEMY_AGGRO=1)
def is_field(zone):
    """A province field (area 1..21, zones.area_of): towns (22..46), castles (47..) and anything else are not."""
    from . import zones
    return 1 <= zones.area_of(zone) <= 21 and zones.map_index(zone) >= 0


def arc_factor(s, x, z):
    """Detection by where the player stands relative to the enemy's facing (PLAYABILITY 1.2 / 5.1; DESIGN
    angles): front (within 60 degrees of the facing) 1, sides (60..120) 1/2, back 1/3."""
    dx, dz = x - s["pos"][1], z - s["pos"][3]
    if dx == 0 and dz == 0:
        return 1.0
    diff = abs((heading(dx, dz) - int(s["pos"][4])) % 360)
    diff = min(diff, 360 - diff)
    return 1.0 if diff <= 60 else 0.5 if diff <= 120 else 1.0 / 3


def level_factor(player_level, enemy_level):
    """Out-levelling the enemy weakens its reaction (DESIGN): same level or below 1; d levels above it
    0.5 * (1 - d / NOBU_ENEMY_AGGRO_LEVEL_GAP) (gap 10), and nothing from the gap on."""
    d = int(player_level) - int(enemy_level)
    if d <= 0:
        return 1.0
    gap = max(1, _env_int("NOBU_ENEMY_AGGRO_LEVEL_GAP", 10))
    return 0.0 if d >= gap else 0.5 * (1 - d / gap)


def aggro_range(s, x, z, player_level):
    """How close (world units, 100 = 1 m) the player must come for spawn s to attack."""
    return _env_int("NOBU_ENEMY_AGGRO_RANGE", 500) * arc_factor(s, x, z) * level_factor(player_level, s["level"])


def aggro_blocked(ctx):
    """None if an attack may start for this session, else the reason (for tests and the log)."""
    if os.environ.get("NOBU_ENEMY_AGGRO") != "1" or os.environ.get("NOBU_BATTLE_FIGHT") != "1":
        return "off"
    c = ctx.get("char") or {}
    if not c:
        return "no character"
    if c.get("ghost"):
        return "ghost"
    from . import battle
    if _in_battle(ctx) or battle.in_battle(ctx):
        return "fighting"
    now = time.monotonic()
    if now < ctx.get("enemies_calm_until", 0):
        return "calm"
    if now < ctx.get("enemies_spawn_t", -1e9) + _env_float("NOBU_ENEMY_AGGRO_GRACE_SECS", 5):
        return "grace"
    if not is_field(_zone(ctx)) and os.environ.get("NOBU_ENEMY_AGGRO_TOWN") != "1":
        return "town"
    return None


def aggro(ctx):
    """05 05 hook: the nearest aggressive (red) enemy in view whose aggro_range() the player has entered starts
    the battle as if the player had pressed ○ on it (the 28 01 path of battle.py: engage, battle_target,
    start_frames + fight_setup). Never on a ghost, while this session is fighting (or NOBU_ENEMY_AGGRO_CALM_SECS
    after its last battle), within NOBU_ENEMY_AGGRO_GRACE_SECS of a map load, or outside a province field
    (NOBU_ENEMY_AGGRO_TOWN=1 allows towns). NOBU_ENEMY_AGGRO_ALL=1 makes yellow enemies aggressive too (tests)."""
    if aggro_blocked(ctx):
        return []
    x, z = _xz(ctx)
    lv = int((ctx.get("char") or {}).get("level", 1) or 1)
    every = os.environ.get("NOBU_ENEMY_AGGRO_ALL") == "1"
    cand = []
    for cid in _mine(ctx):
        s = spawn(cid)
        e = definition(s["enemy"]) if s else None
        if not s or not e or not (e.get("aggressive") or every) or not s["alive"] or not active(s):
            continue
        dist = _dist(s, x, z)
        if dist <= aggro_range(s, x, z, lv):
            cand.append((dist, cid, e))
    for _, cid, e in sorted(cand, key=lambda t: t[0]):
        if not engage(ctx, cid):
            continue
        from . import battle
        s = spawn(cid)
        s["goal"] = None
        s["pos"][4] = heading(x - s["pos"][1], z - s["pos"][3])      # it turns to the player
        ctx["target"] = ctx["battle_target"] = cid
        ctx["enemies_calm_until"] = time.monotonic() + _env_float("NOBU_ENEMY_AGGRO_CALM_SECS", 10)
        ctx.setdefault("enemies_log", []).append(("aggro", cid, s["enemy"]))
        return ([misc.notify_system(f"{display_name(e)} attacks!")] + battle.start_frames(ctx)
                + (battle.fight_setup(ctx) if battle.fight_enabled() else []))
    return []


# ---------------------------------------------------------------------------- describe
def describe_spawn(cid):
    s = spawn(cid)
    if s is None:
        return None
    return f"enemy {s['enemy']} Lv{s['level']} x{s['count']} at {s['pos'][1:4]} alive={s['alive']}"
