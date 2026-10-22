"""Upkeep: world clock, hunger, gear durability, carried weight and out-of-battle Life / Ki.

Addresses.

What the client does with each push (KNOWN unless marked):
  0C 05 WORLD_DATE  u8 weather, u16 (minute << 8 | second), u8 month, u8 day, u8 hour
      (parser 0x1eb140 -> 0x1a5f90, UI event 0xe7 -> 0x1d76d0). The client runs its own minute hand:
      minute = VBlanks since the push / 150 (0x1d7540, capped at 59), so ONE IN-GAME HOUR = 9000
      VBlanks = 150.15 s at 59.94 Hz and the server must push a new date every in-game hour. The very
      first push after world load only starts the hand at minute 0 (UI+0x33734 == 0 path); later
      pushes set it from the u16 and fast-forward the displayed time (UI+0x33738 catch-up, -10 per
      update). Weather 1..5 = UIMSG 0.42 Clear / Cloudy / Rain / Thunder / Snow. "/time" prints UIMSG
      0.126 from these; hour*60+min in [300, 1085) is "day" (0x67bcb0).
  0C 04 WORLD_TIME  u32, u32 -> UI+0x1a474 / +0x1a478; nothing reads them in this build (inert).
  2F 0A hunger      `2f 0a <u16 seq> <u8 level>` (parser 0x284fd0, main 0x286cb0 -> UI 0xe9 ->
      0x1d3230): level 0..3 kept at UI+0x10db8; level 1 / 2 / >= 3 print WDMMSG 0.18.0 / .1 / .2
      ("I'm hungry..." / "So hungry, I could collapse..." / "I'll starve at this rate...") as a
      chat line (0x2592b0 kind 0x14); level 0 prints nothing.
  2F 0B death       `2f 0b <u16 seq> <u8 cause> <u8 dead>` (UI 0xeb -> 0x1d32b0): dead == 1 sets
      P+0x10d94 = 1 (ghost) and prints WDMMSG 0.21 "Died of %1$s", cause 1 = 0.22 hunger,
      2 = 0.24 battle, 3 = 0.23 accident, else 0.25; dead == 0 clears it (kind 0x18 line).
  2F 07 stats       u16 x9 (+0..+0x10 -> P+0x10294.. Str Sta Dex Int Cha Earth Water Fire Wind),
      u32 +0x14 (P+0x102b0 state bits), u32 +0x18 (P+0x102b4), u16 +0x1c, u8 +0x1e, u16 max Life
      (+0x20 -> P+0x102a6), u16 Life (+0x22 -> P+0x102b8), u16 max Ki (+0x24 -> P+0x102a8), u16 Ki
      (+0x26 -> P+0x102ba), u16 +0x28 / +0x2a (P+0x102aa / +0x102ac, shown next to the gauges),
      u16 weight limit (+0x2c -> P+0x546c), u16 carried weight (+0x2e -> P+0x546e)
      (reader 0x1eb1d0, UI 1 -> 0x209280). The field HUD gauges (0x3869a0) and the status window
      (0x64dff0) read exactly these P fields; the load path (0x2093c0) never sets max Life / Ki.
  2F 08 status      u16 Life, u16 Ki, u8 x5 (UI 2 -> 0x209530 -> P+0x102b8 / +0x102ba / +0x102bc..).
  2F 05 PARAMS      `2f 05 <u16 seq> <u32 cid> 01 <bytes PARAMS(15)>` replaces all PARAMS (battle.p_params,
      "Death"); used here for the weight speed so no CHAR_INFO warp is needed.
  Weight            the Holdings panel (vtable 0x454230, update 0x387210) shows P+0x546e / 10 of
      P+0x546c / 10 as a gauge. The "stuck" dialog (0x632060) says "cannot move because of weight"
      (UIMSG 2.253) when PARAMS[8] (move speed, CHAR_INFO) is 0 and "move slowly" (2.254) when it is
      < 11: the slow / root effect IS the move-speed PARAM the server sends.
  Durability        item info struct +0x2f / +0x30 / +0x31 -> item obj +0x5b / +0x5c / +0x5d
      (0x1ffe30); the item window draws "Dur." as +0x5d / +0x5c and "Tough" as
      100 * (+0x5c - 1) / (+0x5b - 1) % (0x36aad4..). Weight = info +0x3a (u16, or u8 +0x32 when
      +0x3a is 0) -> obj+0x5e, drawn on the Weight row. (Record +0x14 / +0x12 are the job mask and
      the required level, NOT durability: see items.py.)

Everything else here is design: rates, food values, weights, thresholds, the starvation effect.
Knobs (all read at call time):
  NOBU_UPKEEP=0                 disable the whole module (no spawn pushes, no ticker)
  NOBU_UPKEEP_TICK              ticker period in seconds (default 5)
  NOBU_UPKEEP_SPAWN_DELAY       seconds after 23 5a before the spawn frames are pushed (default 1);
                                NOBU_UPKEEP_SPAWN_INLINE=1 appends them to the spawn pushes instead
  NOBU_CLOCK=0                  no WORLD_DATE pushes; NOBU_CLOCK_SECS_PER_HOUR (default 150.15),
                                NOBU_CLOCK_EPOCH (unix s, default 1735689600), NOBU_CLOCK_OFFSET_HOURS,
                                NOBU_WEATHER (1..5 fixed), NOBU_CLOCK_WORLD_TIME=1 (also 0C 04)
  NOBU_HUNGER=0                 no hunger; NOBU_HUNGER_MEAL_HOURS (in-game hours from full to empty,
                                default 8 = three meals a day), NOBU_HUNGER_NAG_SECS (default 180),
                                NOBU_FOOD_VALUE (satiety per food item, overrides the table)
  NOBU_STARVE                   drain (default: Life drains to 1, no regen) | death | off;
                                NOBU_STARVE_DRAIN_PCT (default 2 % of max Life per tick),
                                NOBU_STARVE_GRACE_SECS (death mode, default 120)
  NOBU_REGEN=0                  no Life / Ki regen; NOBU_REGEN_PCT (default 5 % of max per
                                NOBU_REGEN_SECS, default 3), NOBU_REGEN_BLINK_SECS (after a battle,
                                default 5), NOBU_MEDICINE_HEAL (Life per medicine, default 30)
  NOBU_UPKEEP_STATS=0           no 2F 07 stats push at spawn / on weight change
  NOBU_UPKEEP_BATTLE_COST       % of max Life and Ki spent per battle when the battle state has no unit
                                for the player (default 20; 0 = none)
  NOBU_UPKEEP_AFTER_BATTLE_SECS the after-battle frames wait this long for the field (default 4);
                                NOBU_UPKEEP_BATTLE_INLINE=1 returns them from on_battle_end instead
  NOBU_DURABILITY=0             no durability loss; NOBU_DUR_LOSS (default 1 per battle),
                                NOBU_DUR_WARN (default 10), NOBU_DUR_BREAK_UNEQUIP=1
  NOBU_WEIGHT=0                 no weight effects; NOBU_WEIGHT_LIMIT_BASE (tenths, default 200),
                                NOBU_WEIGHT_PER_STR (tenths, default 10), NOBU_WEIGHT_SLOW_SPEED
                                (default 10), NOBU_WEIGHT_ROOT_FACTOR (default 3),
                                NOBU_ITEM_WEIGHT_DIV (item window unit, default 10),
                                NOBU_WEIGHT_CHAR_INFO=1 (speed via our own 0B 0a instead of 2F 05)
Chat (say): "upkeep" status lines; "upkeep hunger N" / "life N" / "ki N" / "revive" / "wear" / "time".
"""
import os
import time
import unicodedata

from . import chars, items, social, vce

# ---------------------------------------------------------------------------- knobs


def _on(name, default="1"):
    return os.environ.get(name, default) == "1"


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def enabled():
    return _on("NOBU_UPKEEP")


def clock_enabled():
    return enabled() and _on("NOBU_CLOCK")


# ---------------------------------------------------------------------------- world clock
DAYS_PER_MONTH = 30
WEATHER_NAMES = {1: "Clear", 2: "Cloudy", 3: "Rain", 4: "Thunder", 5: "Snow"}


def game_time(now=None):
    """In-game calendar for wall time `now`: dict(days, month, day, hour, minute, second, weather)."""
    now = time.time() if now is None else now
    per_hour = max(1.0, _f("NOBU_CLOCK_SECS_PER_HOUR", "150.15"))
    hours = (now - _f("NOBU_CLOCK_EPOCH", "1735689600")) / per_hour + _f("NOBU_CLOCK_OFFSET_HOURS", "0")
    hours = max(0.0, hours)
    days = int(hours // 24)
    secs = int((hours - days * 24) * 3600)
    hour, minute, second = secs // 3600, secs // 60 % 60, secs % 60
    month = days // DAYS_PER_MONTH % 12 + 1
    return dict(days=days, month=month, day=days % DAYS_PER_MONTH + 1, hour=hour, minute=minute,
                second=second, weather=weather_for(days, hour, month))


def weather_for(days, hour, month):
    """Design: fixed by NOBU_WEATHER, else a deterministic roll per 6 in-game hours (snow only in
    months 12, 1, 2)."""
    fixed = os.environ.get("NOBU_WEATHER")
    if fixed:
        try:
            return max(1, min(5, int(fixed)))
        except ValueError:
            pass
    roll = (days * 4 + hour // 6) * 2654435761 % 100
    if roll < 50:
        return 1
    if roll < 75:
        return 2
    if roll < 92:
        return 5 if month in (12, 1, 2) else 3
    return 4


def world_date(gt):
    """0C 05 (parser 0x1eb140 -> 0x1a5f90): u8 weather, u16 minute << 8 | second, u8 month,
    u8 day, u8 hour. (chars.notify_world_date names the u16 'year' after the client's log format;
    the clock code 0x1d76d0 uses it as minute:second.)"""
    return (vce.put_u8(0x0C) + vce.put_u8(5) + vce.put_u8(gt["weather"])
            + vce.put_u16((gt["minute"] & 0xFF) << 8 | gt["second"] & 0xFF)
            + vce.put_u8(gt["month"]) + vce.put_u8(gt["day"]) + vce.put_u8(gt["hour"]))


def world_time(gt):
    """0C 04 u32, u32 (stored at UI+0x1a474 / +0x1a478, never read): days, second of the day."""
    return chars.notify_world_time(gt["days"], gt["hour"] * 3600 + gt["minute"] * 60 + gt["second"])


def world_date_now(now=None):
    return world_date(game_time(now))


def clock_pushes(gt):
    out = [world_date(gt)]
    if _on("NOBU_CLOCK_WORLD_TIME", "0"):
        out.append(world_time(gt))
    return out


# ---------------------------------------------------------------------------- cmd 0x2F builders
def notify_hunger(level, seq=0):
    return bytes([0x2F, 0x0A]) + vce.put_u16(seq) + vce.put_u8(max(0, min(3, level)))


def notify_death(cause=1, dead=1, seq=0):
    return bytes([0x2F, 0x0B]) + vce.put_u16(seq) + vce.put_u8(cause) + vce.put_u8(dead)


def notify_status(life, ki, extra=(0, 0, 0, 0, 0), seq=0):
    out = bytes([0x2F, 0x08]) + vce.put_u16(seq) + vce.put_u16(life & 0xFFFF) + vce.put_u16(ki & 0xFFFF)
    return out + bytes(list(extra)[:5] + [0] * (5 - len(list(extra)[:5])))


def notify_stats(stats9, max_life, life, max_ki, ki, attack=0, defense=0, weight_limit=0, weight=0,
                 flags=0, flags2=0, w1c=0, b1e=0, seq=0):
    out = bytes([0x2F, 0x07]) + vce.put_u16(seq)
    for v in (list(stats9) + [0] * 9)[:9]:
        out += vce.put_u16(int(v) & 0xFFFF)
    out += vce.put_u32(flags) + vce.put_u32(flags2) + vce.put_u16(w1c) + vce.put_u8(b1e)
    for v in (max_life, life, max_ki, ki, attack, defense, weight_limit, weight):
        out += vce.put_u16(max(0, min(0xFFFF, int(v))))
    return out


# ---------------------------------------------------------------------------- per-character state
SAT_MAX = 100.0
HUNGER_NAMES = {0: "sated", 1: "hungry", 2: "very hungry", 3: "starving"}
FOOD_VALUES = {279: 35, 282: 50, 300: 40, 538: 60, 578: 40, 2549: 10, 3447: 70, 3474: 55,
               4523: 80, 4554: 45}                     # INVENTED satiety per item (STRDAT 0 ids)
DEFAULT_FOOD = 40


def looks(c):
    return list((c or {}).get("looks", [])) + [0] * 20


def base_stats(c):
    """9 u16: Str, Sta, Dex, Int, Cha, Earth, Water, Fire, Wind (the create looks[11:20])."""
    return looks(c)[11:20]


def maxima(c):
    """(max Life, max Ki, attack, defense): the same numbers a battle starts with (battle.player_combat,
    INVENTED formulas: Life 40 + 5 Sta + 10 (Lv - 1), Ki 20 + 5 Int), so field and battle agree."""
    from . import battle                            # lazy: battle.py lazily imports upkeep (patch)
    pc = battle.player_combat(c)
    return pc["life"], pc["ki"], pc["atk"], pc["dfn"]


def state(c):
    """Persistent part in chars.json "upkeep": {"satiety": float, "life": int, "ki": int}. The ghost flag is
    battle.py's c["ghost"] (a ghost gets no regen and no starvation)."""
    s = c.setdefault("upkeep", {})
    mx_life, mx_ki = maxima(c)[:2]
    s.setdefault("satiety", SAT_MAX)
    s.setdefault("life", mx_life)
    s.setdefault("ki", mx_ki)
    s["life"] = max(0, min(mx_life, int(s["life"])))
    s["ki"] = max(0, min(mx_ki, int(s["ki"])))
    return s


def hunger_level(satiety):
    """Design: > 50 % sated, > 25 % hungry, > 0 very hungry, 0 starving."""
    if satiety > SAT_MAX * 0.5:
        return 0
    if satiety > SAT_MAX * 0.25:
        return 1
    if satiety > 0:
        return 2
    return 3


def session(ctx):
    return ctx.setdefault("upkeep", {})


def _save(store, soon=False):
    """soon=True: a deferred, coalesced write (CharStore.save_soon) for the periodic tick save."""
    st = store() if callable(store) else store
    if st is not None:
        try:
            if soon and hasattr(st, "save_soon"):
                st.save_soon()
            else:
                st.save()
        except OSError:
            pass


# ---------------------------------------------------------------------------- weight
# INVENTED weights in tenths of the display unit, by item type (STRDAT 3), else by category.
WEIGHT_BY_TYPE = {items.T_DAGGER: 5, items.T_BATON: 15, items.T_KATANA: 25, items.T_AXE: 20,
                  items.T_CLUB: 25, items.T_NOSHI: 15, items.T_MONK_ROBE: 20, items.T_VESTMENTS: 20,
                  items.T_SUIKAN: 20, items.T_NINJA_GARB: 15, items.T_HARAATE: 40,
                  items.T_MEDICINE: 1, items.T_FOOD: 2, 53: 1, 55: 5}
WEIGHT_BY_CAT = {1: 25, 2: 15, 3: 3, 4: 30, 5: 5, 6: 5, 7: 8, 8: 8, 9: 1, 0xA: 1, 0xB: 1}
WEIGHT_OTHER = 5


def item_weight(it):
    """Weight of ONE unit in tenths (an explicit it["weight"] wins)."""
    if "weight" in it:
        return max(0, int(it["weight"]))
    if it.get("type") in WEIGHT_BY_TYPE:
        return WEIGHT_BY_TYPE[it["type"]]
    return WEIGHT_BY_CAT.get(it.get("cat", 0), WEIGHT_OTHER)


def info_weight(it):
    """Info struct +0x3a (item window Weight row): the unit weight in display units (rounded up)."""
    div = max(1, int(_f("NOBU_ITEM_WEIGHT_DIV", "10")))
    w = item_weight(it)
    return 0 if w <= 0 else max(1, -(-w // div))


def carried_weight(c):
    inv = (c or {}).get("inventory") or {}
    return sum(item_weight(it) * max(0, int(it.get("count", 1))) for it in inv.get("items", []))


def weight_limit(c):
    strength = looks(c)[11]
    return int(_f("NOBU_WEIGHT_LIMIT_BASE", "200") + _f("NOBU_WEIGHT_PER_STR", "10") * strength)


def speed_class(c):
    """0 normal, 1 slow (over the limit), 2 rooted (over NOBU_WEIGHT_ROOT_FACTOR x the limit)."""
    if not _on("NOBU_WEIGHT"):
        return 0
    w, lim = carried_weight(c), max(1, weight_limit(c))
    if w > lim * _f("NOBU_WEIGHT_ROOT_FACTOR", "3"):
        return 2
    if w > lim:
        return 1
    return 0


def speed_params(cls, c=None):
    """CHAR_INFO PARAMS with [8] = move speed for the class (the client's own test: 0 = cannot move,
    < 11 = slow; 0x632060). With c: the ghost / spirit-sight bytes too (chars.char_params(c))."""
    p = bytearray(chars.char_params(c))
    if cls == 1:
        p[8] = int(_f("NOBU_WEIGHT_SLOW_SPEED", "10")) & 0xFF
    elif cls == 2:
        p[8] = 0
    return bytes(p)


def _pos(ctx, c):
    zone, x, y, z, d = chars.char_position(c)
    m = ctx.get("pos")
    if isinstance(m, dict) and "x" in m:
        x, y, z, d = m["x"], m.get("y", y), m["z"], m.get("dir", d)
    return (ctx.get("zone", zone), x, y, z, d)


def speed_push(ctx, c, cls):
    """The new move speed as `2F 05 <seq> <u32 cid> 01 <PARAMS>` (battle.p_params; UI 5 -> P vt+0x48
    replaces all 15 PARAMS, so a ghost keeps PARAMS[0] = 2). NOBU_WEIGHT_CHAR_INFO=1 sends our own
    CHAR_INFO (0B 0a) instead, at the last reported position (0x1d96b0 also moves P there)."""
    from . import battle
    params = bytearray(speed_params(cls, c))
    if c.get("ghost"):
        params[0] = 2
    if _on("NOBU_WEIGHT_CHAR_INFO", "0"):
        uid = ctx.get("uid", c.get("uid", 0))
        rec = chars.char_info_record(c, uid, _pos(ctx, c), params=bytes(params))
        return chars.notify_char_info([rec])
    return battle.p_params(battle._cid(ctx), bytes(params))


# ---------------------------------------------------------------------------- pushes
def stats_push(c):
    s = state(c)
    mx_life, mx_ki, atk, dfn = maxima(c)
    from . import skills                            # u8 +0x1e = usable skill slots (SK+0x34)
    return notify_stats(base_stats(c), mx_life, s["life"], mx_ki, s["ki"], atk, dfn,
                        weight_limit(c), carried_weight(c), b1e=skills.slot_count(c) if skills.enabled() else 0)


def status_push(c):
    s = state(c)
    return notify_status(s["life"], s["ki"])


def world_ready(ctx, store=None):
    """Hook for world.world_ready_pushes (23 5a). Starts the ticker, whose first step (after
    NOBU_UPKEEP_SPAWN_DELAY s, default 1) pushes spawn_frames(); returns [] so the spawn sequence
    (and its 34 01 quest list last) is unchanged. NOBU_UPKEEP_SPAWN_INLINE=1 returns the frames here
    instead."""
    c = ctx.get("char")
    if not enabled() or not c:
        return []
    if _on("NOBU_UPKEEP_SPAWN_INLINE", "0"):
        out = spawn_frames(ctx)
        start_ticker(ctx, store)
        return out
    session(ctx)["spawn_pending"] = True
    start_ticker(ctx, store, first=_f("NOBU_UPKEEP_SPAWN_DELAY", "1"))
    return []


def spawn_frames(ctx):
    """Date twice (the first only starts the client's minute hand), the stats block (max / current
    Life, Ki, weight gauge), the hunger level, a slow / rooted CHAR_INFO when overweight."""
    c = ctx.get("char")
    if not enabled() or not c:
        return []
    u = session(ctx)
    u.pop("spawn_pending", None)
    now = time.time()
    out = []
    if clock_enabled():
        gt = game_time(now)
        out += clock_pushes(gt) + [world_date(gt)]
        u["hour_key"] = (gt["days"], gt["hour"])
    s = state(c)
    if _on("NOBU_UPKEEP_STATS"):
        out.append(stats_push(c))
        u["gauge"] = (weight_limit(c), carried_weight(c))
    if _on("NOBU_HUNGER"):
        lvl = hunger_level(s["satiety"])
        u["level"], u["nag_t"] = lvl, now
        if lvl:
            out.append(notify_hunger(lvl))
    cls = speed_class(c)
    u["speed"] = cls
    if cls:
        out.append(speed_push(ctx, c, cls))
    u["t"] = now
    return out + u.pop("pending", [])


def in_battle(ctx):
    b = ctx.get("battle") or {}
    return bool(b.get("active")) or b.get("phase") in ("setup", "turns", "end")


def tick(ctx, store=None, now=None):
    """One upkeep step: returns the pushes. Pure apart from ctx / the character dict (tests call it
    with a fake `now`)."""
    c = ctx.get("char")
    if not enabled() or not c:
        return []
    now = time.time() if now is None else now
    u = session(ctx)
    dt = max(0.0, now - u.get("t", now))
    u["t"] = now
    s = state(c)
    mx_life, mx_ki = maxima(c)[:2]
    out = []
    # clock: a new date every in-game hour (the client's minute hand stops at 59)
    if clock_enabled():
        gt = game_time(now)
        key = (gt["days"], gt["hour"])
        if key != u.get("hour_key"):
            u["hour_key"] = key
            out += clock_pushes(gt)
    fighting = in_battle(ctx)
    # hunger
    lvl = 0
    if _on("NOBU_HUNGER"):
        per_hour = max(1.0, _f("NOBU_CLOCK_SECS_PER_HOUR", "150.15"))
        drop = SAT_MAX / max(0.01, _f("NOBU_HUNGER_MEAL_HOURS", "8")) * dt / per_hour
        s["satiety"] = max(0.0, s["satiety"] - drop)
        lvl = hunger_level(s["satiety"])
        nag = _f("NOBU_HUNGER_NAG_SECS", "180")
        if lvl != u.get("level") or (lvl and nag > 0 and now - u.get("nag_t", now) >= nag):
            if lvl or u.get("level"):
                out.append(notify_hunger(lvl))
            u["level"], u["nag_t"] = lvl, now
        if lvl < 3:
            u.pop("starve_t", None)
    life0, ki0 = s["life"], s["ki"]
    # starvation (design; non-lethal by default)
    mode = os.environ.get("NOBU_STARVE", "drain").lower()
    if lvl == 3 and not c.get("ghost"):
        if mode == "drain" and not fighting:
            u["starve_acc"] = u.get("starve_acc", 0.0) + mx_life * _f("NOBU_STARVE_DRAIN_PCT", "2") / 100 \
                * dt / max(0.01, _f("NOBU_UPKEEP_TICK", "5"))
            take = int(u["starve_acc"])
            if take:
                u["starve_acc"] -= take
                s["life"] = max(1, s["life"] - take)
        elif mode == "death":
            u.setdefault("starve_t", now)
            if now - u["starve_t"] >= _f("NOBU_STARVE_GRACE_SECS", "120"):
                from . import battle                # the same ghost state as a battle death
                c["ghost"], s["life"] = True, 0
                params = bytearray(speed_params(speed_class(c), c))
                params[0] = 2
                from . import players; players.refresh(ctx)             # others see the ghost (lifecycle.py)
                out += [notify_death(1, 1), battle.p_params(battle._cid(ctx), bytes(params)),
                        social.notify_system("You starved. Press ○ to Pass On, or say 'upkeep revive'.")]
                _save(store)
    # regen while idle out of battle
    regen_ok = (_on("NOBU_REGEN") and not fighting and not c.get("ghost") and lvl < 3
                and now >= u.get("blink_until", 0))
    if regen_ok and (s["life"] < mx_life or s["ki"] < mx_ki):
        rate = _f("NOBU_REGEN_PCT", "5") / 100 * dt / max(0.01, _f("NOBU_REGEN_SECS", "3"))
        u["life_acc"] = u.get("life_acc", 0.0) + mx_life * rate
        u["ki_acc"] = u.get("ki_acc", 0.0) + mx_ki * rate
        gl, gk = int(u["life_acc"]), int(u["ki_acc"])
        u["life_acc"] -= gl
        u["ki_acc"] -= gk
        s["life"] = min(mx_life, s["life"] + gl)
        s["ki"] = min(mx_ki, s["ki"] + gk)
    else:
        u["life_acc"] = u["ki_acc"] = 0.0
    if (s["life"], s["ki"]) != (life0, ki0):
        out.append(status_push(c))
    # weight: gauge and move speed
    if _on("NOBU_UPKEEP_STATS"):
        gauge = (weight_limit(c), carried_weight(c))
        if gauge != u.get("gauge"):
            u["gauge"] = gauge
            out.append(stats_push(c))
    cls = speed_class(c)
    if cls != u.get("speed", 0) and not fighting:
        prev, u["speed"] = u.get("speed", 0), cls
        out.append(speed_push(ctx, c, cls))
        if cls > prev:
            out.append(social.notify_system(
                "Too heavy: you cannot move. Drop something." if cls == 2 else
                "Your load is over the limit: you move slowly."))
        elif cls == 0:
            out.append(social.notify_system("Your load is back under the limit."))
    if u.get("pending") and now >= u.get("pending_at", 0):
        out += u.pop("pending")
    u["n"] = u.get("n", 0) + 1
    if u["n"] % 12 == 0:
        _save(store, soon=True)                     # periodic: coalesced with other sessions' ticks
    return out


# ---------------------------------------------------------------------------- events
def on_item_used(ctx, it, store=None):
    """Hook from items.py (24 03 consumed one `it`): food restores satiety, medicine heals Life.
    Returns pushes to append after the use reply."""
    c = ctx.get("char") if isinstance(ctx, dict) else None
    if not enabled() or not c or not it:
        return []
    s = state(c)
    u = session(ctx)
    out = []
    if it.get("type") == items.T_FOOD and _on("NOBU_HUNGER"):
        gain = _f("NOBU_FOOD_VALUE", str(FOOD_VALUES.get(it["id"], DEFAULT_FOOD)))
        before = hunger_level(s["satiety"])
        s["satiety"] = min(SAT_MAX, s["satiety"] + gain)
        lvl = hunger_level(s["satiety"])
        if lvl != before or lvl != u.get("level"):
            out.append(notify_hunger(lvl))
        u["level"], u["nag_t"] = lvl, time.time()
        out.append(social.notify_system(f"You ate {items.item_name(it['id'])}. ({HUNGER_NAMES[lvl]})"))
    elif it.get("type") == items.T_MEDICINE and _on("NOBU_REGEN"):
        mx_life = maxima(c)[0]
        if s["life"] < mx_life and not c.get("ghost"):
            s["life"] = min(mx_life, s["life"] + int(_f("NOBU_MEDICINE_HEAL", "30")))
            out.append(status_push(c))
    _save(store)
    return out


def on_battle_end(ctx, store=None, S=None):
    """Hook for battle.finish (one member leaves the battle, 0F 06): the member's Life / Ki as the battle
    left them (S = the shared battle state; its unit for ctx carries hp / mhp / ki / mki, scaled to the
    field maxima), else NOBU_UPKEEP_BATTLE_COST % of max; durability loss on equipped gear; the
    post-battle regen pause. Returns pushes (2F 08, system lines). S == {} (a debug 'battle exit' without
    a fight) changes nothing; S=None (called from elsewhere) counts as a fight without unit data."""
    c = ctx.get("char") if isinstance(ctx, dict) else None
    if not enabled() or not c or S == {}:
        return []
    u = session(ctx)
    s = state(c)
    out = []
    u["blink_until"] = time.time() + _f("NOBU_REGEN_BLINK_SECS", "5")
    mx_life, mx_ki = maxima(c)[:2]
    unit = next((x for x in (S or {}).get("units", []) if x.get("ctx") is ctx), None)
    if unit and unit.get("mhp"):
        s["life"] = max(0, min(mx_life, round(mx_life * max(0, unit.get("hp", 0)) / unit["mhp"])))
        if unit.get("mki"):
            s["ki"] = max(0, min(mx_ki, round(mx_ki * max(0, unit.get("ki", 0)) / unit["mki"])))
        out.append(status_push(c))
    else:
        cost = _f("NOBU_UPKEEP_BATTLE_COST", "20") / 100
        if cost > 0:
            s["life"] = max(1, s["life"] - int(mx_life * cost))
            s["ki"] = max(0, s["ki"] - int(mx_ki * cost))
            out.append(status_push(c))
    if enabled() and _on("NOBU_DURABILITY"):
        out += wear_gear(c, ctx)
    u["battles"] = u.get("battles", 0) + 1
    _save(store)
    if _on("NOBU_UPKEEP_BATTLE_INLINE", "0"):
        return out
    # default: hold the frames until the client is back on the field (the next 23 5a spawn frames or the
    # first tick NOBU_UPKEEP_AFTER_BATTLE_SECS after the exit), so battle.py's frame order is unchanged
    u.setdefault("pending", []).extend(out)
    u["pending_at"] = time.time() + _f("NOBU_UPKEEP_AFTER_BATTLE_SECS", "4")
    return []


def wear_gear(c, ctx=None):
    inv = items.inventory(c)
    loss = int(_f("NOBU_DUR_LOSS", "1"))
    warn = int(_f("NOBU_DUR_WARN", "10"))
    out = []
    for it in list(inv["items"]):
        if it.get("pos") is None or int(it.get("dur_max", 0)) <= 0:
            continue
        before = int(it.get("dur", 0))
        if before <= 0:
            continue
        it["dur"] = after = max(0, before - loss)
        name = items.item_name(it["id"])
        if after == 0:
            it["broken"] = True
            line = f"{name} broke! Have it repaired."
            if _on("NOBU_DUR_BREAK_UNEQUIP", "0"):
                it["pos"] = None
                line = f"{name} broke and was taken off. Have it repaired."
            out.append(social.notify_system(line))
        elif before > warn >= after:
            out.append(social.notify_system(f"{name} is badly worn ({after}/{it['dur_max']})."))
    if ctx is not None:
        ctx.setdefault("upkeep_log", []).append(("wear", [(i["id"], i.get("dur")) for i in inv["items"]
                                                          if i.get("pos") is not None]))
    return out


def on_leave(ctx, store=None):
    """world.session() on disconnect: stop the ticker and keep the state."""
    if not isinstance(ctx, dict):
        return
    u = session(ctx)
    u["stopped"] = True
    u["gen"] = u.get("gen", 0) + 1
    if ctx.get("char"):
        _save(store)


# ---------------------------------------------------------------------------- ticker
def start_ticker(ctx, store=None, first=None):
    """Call tick() every NOBU_UPKEEP_TICK s on the running asyncio loop and push its frames through
    ctx["push"]. Stops on disconnect (world.session unregisters the ctx / calls on_leave), a newer
    ticker, a failed push or NOBU_UPKEEP=0. Without a loop (unit tests) nothing is scheduled."""
    import asyncio
    u = session(ctx)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        u["ticker"] = "no loop"
        return
    push = ctx.get("push")
    if push is None:
        u["ticker"] = "no push"
        return
    period = max(0.01, _f("NOBU_UPKEEP_TICK", "5"))
    gen = u["gen"] = u.get("gen", 0) + 1
    u["stopped"] = False
    registered = any(v is ctx for v in social.SESSIONS.values())

    def step():
        if u.get("gen") != gen or u.get("stopped"):
            return
        if not enabled():
            u["ticker"] = "stopped: NOBU_UPKEEP=0"
            return
        if registered and not any(v is ctx for v in social.SESSIONS.values()):
            u["ticker"] = "stopped: session closed"
            return
        try:
            frames = spawn_frames(ctx) if u.get("spawn_pending") else tick(ctx, store)
            for f in frames:
                push(f)
        except Exception as ex:                     # writer closed: the session is gone
            u["ticker"] = f"stopped: {ex!r}"
            return
        loop.call_later(period, step)

    u["ticker"] = "running"
    loop.call_later(period if first is None else max(0.0, first), step)


# ---------------------------------------------------------------------------- debug chat
def status_lines(ctx):
    c = ctx["char"]
    s = state(c)
    mx_life, mx_ki = maxima(c)[:2]
    gt = game_time()
    worn = [f"{items.item_name(i['id'])} {i.get('dur', 0)}/{i.get('dur_max', 0)}"
            for i in items.inventory(c)["items"] if i.get("pos") is not None and i.get("dur_max")]
    return [f"Time {gt['month']}/{gt['day']} {gt['hour']:02d}:{gt['minute']:02d} "
            f"{WEATHER_NAMES.get(gt['weather'], '?')}",
            f"Life {s['life']}/{mx_life} Ki {s['ki']}/{mx_ki} satiety {s['satiety']:.0f} "
            f"({HUNGER_NAMES[hunger_level(s['satiety'])]})",
            f"Weight {carried_weight(c) / 10:.1f}/{weight_limit(c) / 10:.1f} "
            f"({['normal', 'slow', 'rooted'][speed_class(c)]})",
            "Gear " + (", ".join(worn) or "-")]


def answer(payload, ctx, store=None):
    """Chat 'upkeep' (status), 'upkeep hunger N' (satiety 0..100), 'upkeep life N', 'upkeep ki N',
    'upkeep revive', 'upkeep wear' (one battle's durability loss), 'upkeep time' (push the date).
    Returns None for anything else."""
    if len(payload) < 4 or payload[0] != 0x23 or payload[1] != 0x08 or not isinstance(ctx, dict):
        return None
    if not enabled() or not ctx.get("char"):
        return None
    try:
        d = social.decode_chat(payload)
    except vce.WireError:
        return None
    t = unicodedata.normalize("NFKC", d["text"] or "").strip().lower().lstrip("/!").split()
    if not t or t[0] != "upkeep":
        return None
    c = ctx["char"]
    s = state(c)
    out = social.chat_replies(d, ctx)
    arg = t[1] if len(t) > 1 else ""
    try:
        val = float(t[2]) if len(t) > 2 else None
    except ValueError:
        val = None
    if arg == "hunger" and val is not None:
        s["satiety"] = max(0.0, min(SAT_MAX, val))
        u = session(ctx)
        u["level"] = hunger_level(s["satiety"])
        out.append(notify_hunger(u["level"]))
    elif arg in ("life", "ki") and val is not None:
        mx = maxima(c)[0 if arg == "life" else 1]
        s[arg] = max(0, min(mx, int(val)))
        out.append(status_push(c))
    elif arg == "revive":
        from . import battle
        out += battle.revive(ctx, store)            # 2F 05 PARAMS[0] = 0 + 2F 0B "Revived!" (if a ghost)
        s["life"] = max(1, s["life"])
        s["satiety"] = max(s["satiety"], SAT_MAX * 0.5 + 1)
        session(ctx)["level"] = 0
        out += [status_push(c), notify_hunger(0)]
    elif arg == "wear":
        out += wear_gear(c, ctx)
    elif arg == "time":
        out += clock_pushes(game_time())
    out += [social.notify_system(line) for line in status_lines(ctx)]
    _save(store)
    return out


# ---------------------------------------------------------------------------- logging
SUBS_2F = {7: "STATS", 8: "STATUS", 9: "SUB9", 0xA: "HUNGER", 0xB: "DEATH"}


def describe(payload, direction="S>C"):
    if len(payload) < 2 or direction != "S>C":
        return None
    cmd, sub = payload[0], payload[1]
    try:
        if cmd == 0x0C and sub == 5 and len(payload) >= 8:
            r = vce.Reader(payload, 2)
            w, ms, mo, dy, hr = r.u8(), r.u16(), r.u8(), r.u8(), r.u8()
            return (f"NOTIFY WORLD_DATE {mo}/{dy} {hr:02d}:{ms >> 8:02d}:{ms & 0xFF:02d} "
                    f"weather={w} ({WEATHER_NAMES.get(w, '?')})")
        if cmd != 0x2F or sub not in SUBS_2F:
            return None
        r = vce.Reader(payload, 2)
        seq = r.u16()
        label = f"UPKEEP 2F {SUBS_2F[sub]} seq={seq}"
        if sub == 0xA:
            lvl = r.u8()
            return f"{label} level={lvl} ({HUNGER_NAMES.get(lvl, '?')})"
        if sub == 0xB:
            return f"{label} cause={r.u8()} dead={r.u8()}"
        if sub == 8:
            return f"{label} life={r.u16()} ki={r.u16()}"
        if sub == 7:
            st = [r.u16() for _ in range(9)]
            r.u32(), r.u32(), r.u16(), r.u8()
            v = [r.u16() for _ in range(8)]
            return (f"{label} stats={st} life={v[1]}/{v[0]} ki={v[3]}/{v[2]} atk={v[4]} def={v[5]} "
                    f"weight={v[7]}/{v[6]}")
        return f"{label} {r.rest().hex()}"
    except vce.WireError:
        return None
