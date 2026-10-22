"""Upkeep (clock, hunger, durability, weight, Life / Ki): python -m unittest nobu.test_upkeep -v

The client readers below are models of how the client reads each reply, not from upkeep.py:
0x1eb140 -> 0x1a5f90 (WORLD_DATE), 0x284fd0 cmd 0x2F header, 0x1eb1d0 (2F 07 stats block, copied by
0x209280 into P), 0x285688.. (2F 08: u16, u16, 5 x u8 -> 0x209530), 0x285548 (2F 05 PARAMS)."""
import asyncio
import os
import tempfile
import unittest

from nobu import chars, items, social, upkeep, vce, world
from nobu.clientmodel import Arc, ParseError
from nobu.test_items import parse_info

T0 = 1735689600.0 + 150.15 * 24 * 40          # day 40 00:00 of the default clock
HOUR = 150.15


# ---------------------------------------------------------------------------- client models
def parse_date(p):
    """0C 05: 0x1eb140 reads u8 weather (+6), then 0x1a5f90 u16 (+0), u8 (+2), u8 (+3), u8 (+4).
    0x1d76d0 uses +0 as minute (high byte) and second (low byte), +4 as the hour."""
    a = Arc(p, 2)
    w, ms, mo, dy, hr = a.u8(), a.u16(), a.u8(), a.u8(), a.u8()
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(weather=w, minute=ms >> 8, second=ms & 0xFF, month=mo, day=dy, hour=hr)


def parse_2f(p):
    a = Arc(p, 1)
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 0x0A:
        d["level"] = a.u8()
    elif sub == 0x0B:
        d["cause"], d["dead"] = a.u8(), a.u8()
    elif sub == 0x08:                                  # -> P+0x102b8 / +0x102ba / +0x102bc..
        d["P"] = {0x102B8: a.u16(), 0x102BA: a.u16()}
        d["b"] = [a.u8() for _ in range(5)]
    elif sub == 0x07:                                  # 0x1eb1d0 struct -> 0x209280 P offsets
        st = {o: a.u16() for o in range(0, 0x12, 2)}
        st[0x14], st[0x18], st[0x1C], st[0x1E] = a.u32(), a.u32(), a.u16(), a.u8()
        for o in range(0x20, 0x30, 2):
            st[o] = a.u16()
        d["P"] = {0x102A6: st[0x20], 0x102B8: st[0x22], 0x102A8: st[0x24], 0x102BA: st[0x26],
                  0x102AA: st[0x28], 0x102AC: st[0x2A], 0x546C: st[0x2C], 0x546E: st[0x2E]}
        d["stats"] = [st[o] for o in range(0, 0x12, 2)]
    elif sub == 0x05:
        d["cid"], d["one"] = a.u32(), a.u8()
        d["params"] = a.bytes(0xF)
    else:
        raise ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    return d


def subs(frames, cmd=0x2F):
    return [f[1] for f in frames if f[0] == cmd]


# ---------------------------------------------------------------------------- fixture
class Base(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("NOBU_")}
        self.tmp = tempfile.TemporaryDirectory()
        self.store = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        looks = [5, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]   # Smith, Oda, Str 6 Sta 6
        self.c = self.store.create(0x04000471, 0, "織田", "太郎", looks)
        self.ctx = {"uid": 0x04000471, "cid": self.c["cid"], "char": self.c}
        items.inventory(self.c, self.store)

    def tearDown(self):
        self.tmp.cleanup()
        for k in list(os.environ):
            if k.startswith("NOBU_"):
                os.environ.pop(k)
        os.environ.update(self.env)

    def run_ticks(self, t_from, t_to, step=5.0):
        out, t = [], t_from
        while t <= t_to:
            out += upkeep.tick(self.ctx, self.store, now=t)
            t += step
        return out


# ---------------------------------------------------------------------------- clock
class Clock(Base):
    def test_world_date_matches_the_reader(self):
        gt = dict(days=3, month=1, day=4, hour=13, minute=25, second=7, weather=3)
        p = upkeep.world_date(gt)
        self.assertEqual(p.hex(), "0c05" "03" "1907" "01" "04" "0d")
        self.assertEqual(parse_date(p), dict(weather=3, minute=25, second=7, month=1, day=4, hour=13))

    def test_one_game_hour_is_9000_vblanks(self):
        a, b = upkeep.game_time(T0), upkeep.game_time(T0 + HOUR)
        self.assertEqual((a["hour"], a["minute"], b["hour"], b["minute"]), (0, 0, 1, 0))
        m = upkeep.game_time(T0 + HOUR / 60 * 30 + 0.01)             # 30 in-game minutes = 4500 VBlanks
        self.assertEqual((m["hour"], m["minute"]), (0, 30))
        d = upkeep.game_time(T0 + HOUR * 24 * 30)
        self.assertEqual((d["month"], d["day"]), (upkeep.game_time(T0)["month"] % 12 + 1, 11))
        for h in range(0, 24 * 60, 7):
            self.assertIn(upkeep.game_time(T0 + h * HOUR)["weather"], (1, 2, 3, 4, 5))

    def test_tick_pushes_a_date_every_game_hour(self):
        os.environ["NOBU_HUNGER"] = "0"
        upkeep.session(self.ctx)["t"] = T0
        first = upkeep.tick(self.ctx, self.store, now=T0 + 1)
        self.assertEqual(subs(first, 0x0C), [5])
        self.assertEqual(subs(upkeep.tick(self.ctx, self.store, now=T0 + 60), 0x0C), [])
        again = upkeep.tick(self.ctx, self.store, now=T0 + HOUR + 1)
        self.assertEqual(parse_date(again[0])["hour"], 1)
        os.environ["NOBU_CLOCK"] = "0"
        self.assertEqual(subs(upkeep.tick(self.ctx, self.store, now=T0 + 3 * HOUR), 0x0C), [])

    def test_fixed_weather_and_world_time_knob(self):
        os.environ["NOBU_WEATHER"] = "5"
        os.environ["NOBU_CLOCK_WORLD_TIME"] = "1"
        f = upkeep.clock_pushes(upkeep.game_time(T0))
        self.assertEqual([x[:2].hex() for x in f], ["0c05", "0c04"])
        self.assertEqual(parse_date(f[0])["weather"], 5)


# ---------------------------------------------------------------------------- spawn / world hooks
class Spawn(Base):
    def test_spawn_frames(self):
        f = upkeep.spawn_frames(self.ctx)
        self.assertEqual([x[:2].hex() for x in f], ["0c05", "0c05", "2f07"])     # sated, not overweight
        st = parse_2f(f[2])
        from nobu import battle
        pc = battle.player_combat(self.c)
        self.assertEqual((st["P"][0x102A6], st["P"][0x102B8]), (pc["life"], pc["life"]))
        self.assertEqual((st["P"][0x102A8], st["P"][0x102BA]), (pc["ki"], pc["ki"]))
        self.assertEqual(st["stats"], self.c["looks"][11:20])
        self.assertEqual(st["P"][0x546E], upkeep.carried_weight(self.c))
        self.assertEqual(st["P"][0x546C], upkeep.weight_limit(self.c))
        self.c["upkeep"]["satiety"] = 10
        self.assertIn(bytes.fromhex("2f0a000002"), upkeep.spawn_frames(self.ctx))

    def test_world_spawn_is_unchanged_by_default_and_inline_by_knob(self):
        world.STORE = self.store
        os.environ.update(NOBU_GUILD="0", NOBU_SHOPS="0", NOBU_ENEMIES="0")
        base = world.answer(bytes.fromhex("235a0001"), self.ctx)
        self.assertNotIn(0x2F, [p[0] for p in base if p[1] in (7, 0xA)])
        self.assertEqual(self.ctx["upkeep"]["ticker"], "no loop")
        self.assertTrue(self.ctx["upkeep"]["spawn_pending"])
        os.environ["NOBU_UPKEEP_SPAWN_INLINE"] = "1"
        inline = world.answer(bytes.fromhex("235a0002"), dict(self.ctx, upkeep={}))
        self.assertEqual(len(inline), len(base) + 3)
        self.assertEqual([p[:2].hex() for p in inline[-3:]], ["0c05", "0c05", "2f07"])

    def test_disabled(self):
        os.environ["NOBU_UPKEEP"] = "0"
        self.assertEqual(upkeep.world_ready(self.ctx, self.store), [])
        self.assertEqual(upkeep.spawn_frames(self.ctx), [])
        self.assertEqual(upkeep.tick(self.ctx, self.store, now=T0), [])
        self.assertEqual(upkeep.on_battle_end(self.ctx, self.store), [])
        items.answer(bytes.fromhex("24010001"), self.ctx, self.store)
        self.assertEqual(len(items.answer(bytes.fromhex("24030002" + "0400" + "00000117"), self.ctx,
                                          self.store)), 1)


# ---------------------------------------------------------------------------- hunger
class Hunger(Base):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_CLOCK"] = "0"
        os.environ["NOBU_REGEN"] = "0"
        upkeep.spawn_frames(self.ctx)
        upkeep.session(self.ctx)["t"] = T0

    def test_levels_follow_three_meals_a_day(self):
        os.environ["NOBU_HUNGER_NAG_SECS"] = "0"
        seen = []
        for h in (1, 3.9, 4.1, 5.9, 6.1, 7.9, 8.1, 9):
            for f in upkeep.tick(self.ctx, self.store, now=T0 + h * HOUR):
                if f[:2] == b"\x2f\x0a":
                    seen.append((h, parse_2f(f)["level"]))
        self.assertEqual(seen, [(4.1, 1), (6.1, 2), (8.1, 3)])   # full -> empty in 8 in-game hours
        self.assertEqual(upkeep.hunger_level(self.c["upkeep"]["satiety"]), 3)

    def test_nag_repeats_the_warning(self):
        os.environ["NOBU_HUNGER_NAG_SECS"] = "60"
        self.c["upkeep"]["satiety"] = 40
        f = upkeep.tick(self.ctx, self.store, now=T0 + 1)
        self.assertEqual(subs(f), [0x0A])
        self.assertEqual(subs(upkeep.tick(self.ctx, self.store, now=T0 + 30)), [])
        self.assertEqual(subs(upkeep.tick(self.ctx, self.store, now=T0 + 62)), [0x0A])

    def test_eating_food_through_24_03(self):
        self.c["upkeep"]["satiety"] = 20
        upkeep.session(self.ctx)["level"] = 2
        bp = items.answer(bytes.fromhex("24010001"), self.ctx, self.store)
        dango = next(it for it in self.c["inventory"]["items"] if it["id"] == 279)
        req = bytes.fromhex("24030002") + bytes([dango["idx"], 0]) + vce.put_u32(279)
        reps = items.answer(req, self.ctx, self.store)
        self.assertEqual(reps[0].hex(), "24030002")
        self.assertEqual(parse_2f(reps[1])["level"], 0)          # 20 + 35 = 55 > 50: sated
        self.assertEqual(reps[2][:2].hex(), "0d06")
        self.assertEqual(dango["count"], 2)
        self.assertEqual(self.c["upkeep"]["satiety"], 55)
        self.assertTrue(bp)

    def test_starvation_drains_to_one_by_default(self):
        os.environ["NOBU_REGEN"] = "1"
        self.c["upkeep"]["satiety"] = 0
        life0 = self.c["upkeep"]["life"]
        f = self.run_ticks(T0 + 5, T0 + 600)
        self.assertIn(8, subs(f))
        self.assertEqual(self.c["upkeep"]["life"], 1)
        self.assertLess(1, life0)
        self.assertFalse(self.c.get("ghost"))

    def test_starvation_death_mode(self):
        os.environ["NOBU_STARVE"] = "death"
        os.environ["NOBU_STARVE_GRACE_SECS"] = "10"
        self.c["upkeep"]["satiety"] = 0
        f = self.run_ticks(T0 + 5, T0 + 30)
        dead = [parse_2f(x) for x in f if x[:2] == b"\x2f\x0b"]
        self.assertEqual([(d["cause"], d["dead"]) for d in dead], [(1, 1)])   # WDMMSG 0.21 + 0.22 hunger
        p = next(parse_2f(x) for x in f if x[:2] == b"\x2f\x05")
        self.assertEqual((p["cid"], p["params"][0]), (self.c["cid"], 2))
        self.assertTrue(self.c["ghost"])
        world.STORE = self.store
        reps = upkeep.answer(bytes([0x23, 0x08]) + vce.put_u16(1) + vce.put_bytes(b"/s/upkeep revive"),
                             self.ctx, self.store)
        self.assertIn(bytes.fromhex("2f0b00000000"), reps)
        self.assertFalse(self.c.get("ghost"))


# ---------------------------------------------------------------------------- Life / Ki
class LifeKi(Base):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_CLOCK"] = "0"
        os.environ["NOBU_HUNGER"] = "0"
        upkeep.spawn_frames(self.ctx)
        upkeep.session(self.ctx)["t"] = T0

    def test_battle_end_carries_life_then_regen_after_the_blink(self):
        mx_life, mx_ki = upkeep.maxima(self.c)[:2]
        S = {"units": [{"ctx": self.ctx, "hp": 10, "mhp": 100, "ki": 25, "mki": 50}]}
        self.assertEqual(upkeep.on_battle_end(self.ctx, self.store, S), [])      # held for the field
        self.assertEqual(self.c["upkeep"]["life"], round(mx_life * 0.1))
        u = upkeep.session(self.ctx)
        t = u["pending_at"] + 0.1
        u["t"] = t - 5
        u["blink_until"] = t + 5
        first = upkeep.tick(self.ctx, self.store, now=t)
        st = parse_2f(next(x for x in first if x[:2] == b"\x2f\x08"))
        self.assertEqual(st["P"][0x102B8], round(mx_life * 0.1))
        self.assertEqual(subs(upkeep.tick(self.ctx, self.store, now=t + 4)), [])  # blink: no regen yet
        later = self.run_ticks(t + 6, t + 200)
        self.assertTrue(subs(later).count(8) >= 2)
        self.assertEqual((self.c["upkeep"]["life"], self.c["upkeep"]["ki"]), (mx_life, mx_ki))

    def test_no_regen_in_battle_or_as_a_ghost(self):
        self.c["upkeep"]["life"] = 5
        self.ctx["battle"] = {"phase": "turns"}
        self.assertEqual(self.run_ticks(T0 + 5, T0 + 60), [])
        self.ctx["battle"] = {"phase": "done"}
        self.c["ghost"] = True
        self.assertEqual(self.run_ticks(T0 + 65, T0 + 120), [])
        self.c.pop("ghost")
        self.assertIn(8, subs(self.run_ticks(T0 + 125, T0 + 130)))

    def test_debug_exit_without_a_fight_changes_nothing(self):
        life = self.c["upkeep"]["life"]
        self.assertEqual(upkeep.on_battle_end(self.ctx, self.store, {}), [])
        self.assertEqual(self.c["upkeep"]["life"], life)

    def test_medicine_heals(self):
        self.c["upkeep"]["life"] = 5
        items.answer(bytes.fromhex("24010001"), self.ctx, self.store)
        med = next(it for it in self.c["inventory"]["items"] if it["id"] == 201)
        reps = items.answer(bytes.fromhex("24030002") + bytes([med["idx"], 0]) + vce.put_u32(201), self.ctx,
                            self.store)
        self.assertEqual(parse_2f(reps[1])["P"][0x102B8], 35)


# ---------------------------------------------------------------------------- durability
class Durability(Base):
    def test_wear_break_and_the_item_window(self):
        os.environ["NOBU_UPKEEP_BATTLE_INLINE"] = "1"
        os.environ["NOBU_DUR_LOSS"] = "45"
        weapon = next(it for it in self.c["inventory"]["items"] if it.get("pos") == 0)
        for it in self.c["inventory"]["items"]:         # starter kits carry the wiki durability (items.py)
            if it.get("dur_max"):
                it["dur"] = it["dur_max"] = 100
        f = upkeep.on_battle_end(self.ctx, self.store)
        self.assertEqual(weapon["dur"], 55)
        self.assertEqual([x[:2].hex() for x in f], ["2f08"])
        f = upkeep.on_battle_end(self.ctx, self.store)
        self.assertEqual(weapon["dur"], 10)
        self.assertTrue(any(b"badly worn" in x for x in f))
        f = upkeep.on_battle_end(self.ctx, self.store)
        self.assertEqual((weapon["dur"], weapon["broken"], weapon["pos"]), (0, True, 0))
        self.assertTrue(any(b"broke" in x for x in f))
        items.answer(bytes.fromhex("24010001"), self.ctx, self.store)
        info = parse_info(items.answer(bytes.fromhex("24020002") + bytes([weapon["idx"]]), self.ctx,
                                       self.store)[0])["info"]
        # 0x1ffe30: +0x2f / +0x30 / +0x31 -> obj+0x5b / +0x5c / +0x5d ("Dur." = +0x5d / +0x5c), +0x3a -> weight
        self.assertEqual((info[0x2F], info[0x30], info[0x31]), (100, 100, 0))
        self.assertEqual(info[0x3A], upkeep.info_weight(weapon))
        self.assertEqual((info[0x24], info[0x28]), (items.stats(weapon)["jobs"], 0))   # job mask / level

    def test_break_unequip_knob_and_off(self):
        os.environ["NOBU_DUR_LOSS"] = "200"
        os.environ["NOBU_DUR_BREAK_UNEQUIP"] = "1"
        upkeep.on_battle_end(self.ctx, self.store)
        self.assertEqual([it.get("pos") for it in self.c["inventory"]["items"] if it.get("dur_max")],
                         [None, None])
        os.environ["NOBU_DURABILITY"] = "0"
        for it in self.c["inventory"]["items"]:
            it["dur"] = 100
        upkeep.on_battle_end(self.ctx, self.store)
        self.assertEqual({it["dur"] for it in self.c["inventory"]["items"] if it.get("dur_max")}, {100})


# ---------------------------------------------------------------------------- weight
class Weight(Base):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_CLOCK"] = "0"
        os.environ["NOBU_HUNGER"] = "0"
        upkeep.spawn_frames(self.ctx)
        upkeep.session(self.ctx)["t"] = T0

    def test_slow_then_rooted_then_free(self):
        lim = upkeep.weight_limit(self.c)
        rock = dict(id=1, type=0, cat=0, count=1, flags=0, weight=lim, idx=40)
        self.c["inventory"]["items"].append(rock)
        f = upkeep.tick(self.ctx, self.store, now=T0 + 5)
        p = [parse_2f(x) for x in f if x[:2] == b"\x2f\x05"]
        self.assertEqual([x["params"][8] for x in p], [10])                     # < 11: "move slowly"
        g = parse_2f(next(x for x in f if x[:2] == b"\x2f\x07"))
        self.assertEqual((g["P"][0x546E], g["P"][0x546C]), (upkeep.carried_weight(self.c), lim))
        rock["weight"] = 3 * lim
        f = upkeep.tick(self.ctx, self.store, now=T0 + 10)
        self.assertEqual([parse_2f(x)["params"][8] for x in f if x[:2] == b"\x2f\x05"], [0])   # rooted
        self.c["inventory"]["items"].remove(rock)
        f = upkeep.tick(self.ctx, self.store, now=T0 + 15)
        self.assertEqual([parse_2f(x)["params"][8] for x in f if x[:2] == b"\x2f\x05"], [100])
        self.assertEqual(upkeep.tick(self.ctx, self.store, now=T0 + 20), [])

    def test_char_info_variant_and_ghost_bit(self):
        os.environ["NOBU_WEIGHT_CHAR_INFO"] = "1"
        self.ctx["pos"] = dict(x=1000, y=50, z=2000, dir=90)
        f = upkeep.speed_push(self.ctx, self.c, 1)
        from nobu import clientmodel
        rec = clientmodel.parse_notify_char(f)["records"][0]
        self.assertEqual((rec["params"][8], rec["pos"], rec["dir"]), (10, (1000, 50, 2000), 90))
        os.environ.pop("NOBU_WEIGHT_CHAR_INFO")
        self.c["ghost"] = True
        self.assertEqual(parse_2f(upkeep.speed_push(self.ctx, self.c, 2))["params"][:1], b"\x02")

    def test_weight_off(self):
        os.environ["NOBU_WEIGHT"] = "0"
        self.c["inventory"]["items"].append(dict(id=1, type=0, cat=0, count=1, weight=99999, idx=40))
        self.assertEqual(upkeep.speed_class(self.c), 0)


# ---------------------------------------------------------------------------- ticker / chat / log
class Ticker(Base):
    def test_ticker_pushes_and_stops_on_disconnect(self):
        os.environ.update(NOBU_UPKEEP_TICK="0.01", NOBU_UPKEEP_SPAWN_DELAY="0")
        pushed = []
        self.ctx["push"] = pushed.append
        social.register("sid-upkeep", self.ctx)

        async def run():
            upkeep.world_ready(self.ctx, self.store)
            await asyncio.sleep(0.05)
            n = len(pushed)
            social.unregister("sid-upkeep")
            upkeep.on_leave(self.ctx, self.store)
            await asyncio.sleep(0.05)
            return n
        try:
            n = asyncio.run(run())
        finally:
            social.unregister("sid-upkeep")
        self.assertGreaterEqual(n, 3)
        self.assertEqual([p[:2].hex() for p in pushed[:3]], ["0c05", "0c05", "2f07"])
        self.assertEqual(len(pushed), n)
        self.assertTrue(self.ctx["upkeep"]["stopped"])

    def test_ticker_stops_when_the_push_fails(self):
        os.environ.update(NOBU_UPKEEP_TICK="0.01", NOBU_UPKEEP_SPAWN_DELAY="0")

        def broken(_):
            raise ConnectionError("closed")
        self.ctx["push"] = broken

        async def run():
            upkeep.world_ready(self.ctx, self.store)
            await asyncio.sleep(0.05)
        asyncio.run(run())
        self.assertTrue(self.ctx["upkeep"]["ticker"].startswith("stopped"))

    def test_chat_status_and_describe(self):
        reps = upkeep.answer(bytes([0x23, 0x08]) + vce.put_u16(3) + vce.put_bytes("/s/upkeep".encode()),
                             self.ctx, self.store)
        lines = [r for r in reps if r[:2] == b"\x0d\x06"]
        self.assertEqual(len(lines), 4)
        self.assertIsNone(upkeep.answer(bytes([0x23, 0x08]) + vce.put_u16(3) + vce.put_bytes(b"/s/hello"),
                                        self.ctx, self.store))
        reps = upkeep.answer(bytes([0x23, 0x08]) + vce.put_u16(4) + vce.put_bytes(b"/s/upkeep hunger 10"),
                             self.ctx, self.store)
        self.assertIn(bytes.fromhex("2f0a000002"), reps)
        self.assertIn("HUNGER seq=0 level=2 (very hungry)", world.describe(bytes.fromhex("2f0a000002"), "S>C"))
        self.assertIn("WORLD_DATE", world.describe(upkeep.world_date_now(T0), "S>C"))
        self.assertIn("life=", world.describe(upkeep.stats_push(self.c), "S>C"))


if __name__ == "__main__":
    unittest.main()
