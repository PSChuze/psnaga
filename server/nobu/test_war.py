"""War / realm state (23 04, 23 05, 2F 04): python -m unittest nobu.test_war -v

The parsers below are models of how the client reads each reply:
0x1f19b0 (DIPLOMATIC STATE), 0x1f1ca0 (FRIENDSHIP STATE), 0x1f15e8 (2F 04 type 1 / 3), and the
UI-side lookups 0x1f1860 (relation), 0x1f1b90 (faction flag), 0x625178 (funds coins)."""
import os
import unittest

from nobu import chars, clientmodel as cm, war, world, zones


# ---------------------------------------------------------------------------- client model
def arc_u16s(a, maxn):                                # 0x129140: count header, BE u16s, max check
    n = a._wrap(a.r.length)
    if n > maxn:
        raise cm.ParseError(f"u16 array of {n} > {maxn}")
    return [a.u16() for _ in range(n)]


def parse_diplomatic(a):
    """0x1f19b0 -> struct of 0x127 bytes (dict with the raw struct)."""
    st = bytearray(0x127)
    b = a.bytes(0x16)
    st[0:len(b)] = b
    for k in range(1, 15):                            # row k: max k bytes at +0x16 + 15k
        row = a.bytes(k)
        off = 0x16 + 0xF * k
        st[off:off + len(row)] = row
    f = a.bytes(0xF)
    st[0x118:0x118 + len(f)] = f
    return st


def relation(st, x, y):                               # 0x1f1860
    if not (0 <= x < 15 and 0 <= y < 15) or x == y:
        return 0
    return st[0x16 + 15 * max(x, y) + min(x, y)]


def flag(st, f):                                      # 0x1f1b90
    return 0 <= f < 15 and st[0x118 + f] > 0


def parse_friendship(a):
    """0x1f1ca0: bytes 15, u16s 15, bytes 22, bytes 22, u16s 18."""
    return dict(rel=a.bytes(0xF), funds=arc_u16s(a, 0xF), p2e=a.bytes(0x16), power=a.bytes(0x16),
                wanted=arc_u16s(a, 0x12))


def parse_cmd23(payload):
    a = cm.Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    if sub == 4:
        d = dict(state=parse_diplomatic(a), ui=0xE4, msg=0xD)
    elif sub == 5:
        d = dict(fs=parse_friendship(a), ui=0xE5, msg=0x54)
    else:
        raise cm.ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    d.update(sub=sub, seq=seq)
    return d


def parse_2f04(payload):
    """0x284fd0 header, case 0x285400: u8 type; 1 / 3 -> 0x1f1440 read half 0x1f15e8."""
    a = cm.Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    assert sub == 4
    kind = a.u8()
    d = dict(seq=seq, kind=kind)
    if kind in (1, 3):
        d["text"] = a.str(0x3D)
        d["b4c"], d["b4d"], n = a.u8(), a.u8(), a.u8()
        if n > 8:                                     # no bound in the client: would overrun +0x6f
            raise cm.ParseError(f"{n} war entries overrun the 8 slots")
        d["wars"] = [(a.u8(), a.u8(), a.u8(), a.u8()) for _ in range(n)]
        d["old"] = parse_diplomatic(a)
        d["new"] = parse_diplomatic(a)
    else:
        raise cm.ParseError(f"type {kind} not modelled")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return d


def coins(funds):                                     # 0x625178: six coins of 0xa6
    return [max(0.0, min(1.0, (funds - i * 0xA6) / 0xA6)) for i in range(6)]


class Env:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


WAR_KNOBS = ("NOBU_WAR", "NOBU_WAR_PAIR", "NOBU_WAR_PUSH", "NOBU_WAR_DB", "NOBU_DATA", "NOBU_WAR_INLINE",
             "NOBU_WAR_SCHEDULE", "NOBU_WAR_CLOCK_SCALE", "NOBU_WAR_CLOCK_START", "NOBU_WAR_DECLARE",
             "NOBU_WAR_ROUNDS", "NOBU_WAR_RESULTS", "NOBU_WAR_WIN_MIN", "NOBU_WAR_HUD_PUSH", "NOBU_WAR_BOARD",
             "NOBU_WAR_BOARD_ANY_ZONE", "NOBU_WAR_BOARD_MERIT", "NOBU_WAR_CHAT", "NOBU_WAR_DEBUG",
             "NOBU_WAR_SKIRMISH_PUSH", "NOBU_WAR_SPAWN_DELAY", "NOBU_WAR_TICK", "NOBU_ZONE", "NOBU_START_TOWN")

ODA = dict(cid=5, uid=7, slot=0, family="織田", given="太郎", looks=[1, 1, 0, 6, 6])


class WarState(unittest.TestCase):
    def setUp(self):
        self.env = Env(**{k: None for k in WAR_KNOBS})
        self.env.__enter__()
        os.environ["NOBU_WAR_DB"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-dir",
                                                 "war.json")            # never a real data file
        war.reset_state()

    def tearDown(self):
        war.reset_state()
        self.env.__exit__()

    def test_diplomatic_reply_parses_like_the_client(self):
        st = war.WarState()
        st.set_relation(1, 2, war.ALLIED)
        st.set_relation(14, 3, war.RIVAL)
        d = parse_cmd23(war.diplomatic_reply(0x21, st))
        self.assertEqual((d["seq"], d["ui"], d["msg"]), (0x21, 0xE4, 0xD))
        s = d["state"]
        self.assertEqual(list(s[:22]), chars.PROVINCE_OWNERS)       # same bytes as 05 03
        self.assertEqual(relation(s, 1, 2), war.ALLIED)
        self.assertEqual(relation(s, 2, 1), war.ALLIED)
        self.assertEqual(relation(s, 3, 14), war.RIVAL)
        self.assertEqual(relation(s, 6, 5), war.HOSTILE)          # the default war pair
        self.assertEqual(relation(s, 6, 9), war.NEUTRAL)
        self.assertEqual(relation(s, 5, 5), 0)
        for x in range(15):
            for y in range(15):
                self.assertEqual(relation(s, x, y), st.relation(x, y))

    def test_fallen_flag_follows_province_ownership(self):
        owners = list(chars.PROVINCE_OWNERS)
        owners[11] = 6                                           # Oda takes Mino: Saito has nothing
        st = war.WarState(dict(owners=owners, war_pair=[]))
        s = parse_cmd23(war.diplomatic_reply(1, st))["state"]
        self.assertTrue(flag(s, 7))
        self.assertFalse(flag(s, 6))
        self.assertFalse(flag(s, 0))

    def test_friendship_reply_funds_power_and_limits(self):
        st = war.WarState(dict(funds=[0, 996, 83] + [0] * 12, power=[0] * 10 + [100, 37] + [0] * 10))
        rep = war.friendship_reply(0x30, st, viewer=6, wanted=[0] * 7 + [5])
        d = parse_cmd23(rep)
        fs = d["fs"]
        self.assertEqual((d["ui"], d["msg"]), (0xE5, 0x54))
        self.assertEqual(len(fs["funds"]), 15)
        self.assertEqual(coins(fs["funds"][1]), [1.0] * 6)          # Uesugi: full (6 coins)
        self.assertEqual(coins(fs["funds"][2])[0], 0.5)            # Takeda: half a coin
        self.assertEqual((fs["power"][10], fs["power"][11]), (100, 37))
        self.assertEqual(len(fs["wanted"]), 18)
        self.assertEqual(fs["wanted"][7], 5)
        self.assertEqual(fs["rel"][5], war.HOSTILE)                 # viewer Oda vs Tokugawa
        self.assertEqual(fs["rel"][1], war.NEUTRAL)

    def test_power_is_clamped_to_the_bar(self):
        st = war.WarState(dict(power=[250] * 22))
        fs = parse_cmd23(war.friendship_reply(1, st))["fs"]
        self.assertEqual(set(fs["power"]), {100})

    def test_world_answers_the_realm_window(self):
        ctx = {"char": dict(ODA)}
        (rep,) = world.answer(bytes.fromhex("23050042"), ctx)
        d = parse_cmd23(rep)
        self.assertEqual(d["seq"], 0x42)
        self.assertEqual(d["fs"]["rel"][5], war.HOSTILE)
        (rep,) = world.answer(bytes.fromhex("23040043"), ctx)
        self.assertEqual(parse_cmd23(rep)["seq"], 0x43)
        self.assertIn("SCMD_GET_FRIENDSHIP_STATE request", world.describe(bytes.fromhex("23050042")))
        self.assertIn("funds=", world.describe(world.answer(bytes.fromhex("23050042"), ctx)[0], "S>C"))

    def test_disabled_falls_back(self):
        with Env(NOBU_WAR="0"):
            self.assertEqual(world.answer(bytes.fromhex("23050001"), {}),
                             [bytes.fromhex("23050001") + b"\x00" * 5])
            self.assertIsNone(war.answer(bytes.fromhex("23040001"), {}))

    def test_power_table_still_answered_by_chars(self):
        self.assertEqual(world.answer(bytes.fromhex("23030007"), {}),
                         [bytes.fromhex("23030007") + b"\x00" * 19])

    def test_notify_2f04_type3(self):
        st = war.WarState()
        st.declare(1, 2)                                            # Uesugi -> Takeda
        d = parse_2f04(war.notify_diplomacy(st, kind=3, text="外交"))
        self.assertEqual(d["kind"], 3)
        self.assertEqual(d["wars"], [(6, 5, 88, 0), (1, 2, 74, 0)])
        self.assertEqual(relation(d["new"], 1, 2), war.HOSTILE)
        self.assertEqual(bytes(d["old"]), bytes(d["new"]))

    def test_war_list_is_capped_at_eight(self):
        st = war.WarState(dict(war_pair=[]))
        for a in range(1, 11):
            st.declare(a, a + 1)
        self.assertEqual(len(st.wars), 8)
        parse_2f04(war.notify_diplomacy(st))

    def test_world_ready_is_deferred_without_a_loop(self):
        # no running loop / push (unit tests): nothing inline; NOBU_WAR_INLINE=1 returns the frames
        self.assertEqual(war.world_ready({"char": dict(ODA)}), [])
        with Env(NOBU_WAR_INLINE="1", NOBU_WAR_SCHEDULE="0"):
            frames = war.world_ready({"char": dict(ODA)})
            p = [f for f in frames if f[:2] == b"/"][0]
            self.assertEqual(parse_2f04(p)["wars"], [(6, 5, 88, 0)])
            self.assertIn("WAR NOTIFY diplomacy type=3", world.describe(p, "S>C"))


class Battlefields(unittest.TestCase):
    def test_border_field_table(self):
        self.assertEqual(len(war.BORDER_FIELDS), 31)
        self.assertEqual(war.border_field(10, 11), 89)             # Owari / Mino: Mt. Komaki
        self.assertEqual(war.border_field(9, 10), 88)              # Mikawa / Owari: Okehazama
        self.assertEqual(war.border_field(5, 1), 74)               # Kawanakajima
        self.assertIsNone(war.border_field(1, 21))
        self.assertEqual(sorted(a for _, _, a, _ in war.BORDER_FIELDS), list(range(74, 105)))

    def test_map_ids(self):
        # 0x21bad0 and the known pairs (map 110 = Nagoya place 31, 410 = Nagoya castle place 56)
        self.assertEqual(war.map_to_place(110), 31)
        self.assertEqual(war.map_to_place(410), 56)
        self.assertEqual(war.map_to_place(420), 56 + 9)           # 419 has no map: 420 is 9 names on
        self.assertEqual(war.place_to_map(88), 212)                # Okehazama
        self.assertEqual(war.place_to_map(89), 301)                # Mt. Komaki
        self.assertEqual(war.place_to_map(74), 429)                # Kawanakajima
        fields = [war.place_to_map(a) for _, _, a, _ in war.BORDER_FIELDS]
        self.assertNotIn(None, fields)
        self.assertEqual(sorted(fields), list(range(201, 213)) + list(range(301, 317)) + [429, 430, 431])

    def test_battlefield_places_are_valid(self):
        for _, _, a, _ in war.BORDER_FIELDS:                     # index < 0x83 (0x21bad0)
            self.assertEqual(zones.area_of(war.place_to_map(a)), a)

    def test_castle_fields_are_clan_homes(self):
        homes = {p for _, p in war.CASTLE_FIELDS}
        self.assertEqual(len(homes), 14)
        for p in homes:
            self.assertNotEqual(chars.PROVINCE_OWNERS[p], 0)


# ---------------------------------------------------------------------------- phase 1 / 2 client models
def parse_trailer(a):                                 # 0x1eafc0 read half
    flags, rid = a.u32(), a.u16()
    extra = a.u32() if flags & 1 else None
    return dict(flags=flags, id=rid, extra=extra, tail=a.u8())


def parse_edict(a):                                   # 0x228890 read half (0x228b18)
    d = dict(own=a.u32(), last=(a.u8(), a.u8(), a.u8()), u14=a.u32(), b18=a.u8())
    d["current"] = (a.u8(), a.u8(), a.u32())
    d["next"] = (a.u8(), a.u8(), a.u32())
    d["watch"], d["foster"], d["build"] = a.u32(), a.u32(), a.u32()
    d["b34"] = [a.u8() for _ in range(4)]
    d["trailer"] = parse_trailer(a)
    return d


def parse_hud(a):                                     # 0x229600 read half (0x229860)
    d = dict(place=a.u8(), attacker=a.u32(), defender=a.u32(), score_a=a.u32(), score_d=a.u32())
    d["camps_a"], d["camps_d"] = a.bytes(9), a.bytes(9)
    d["hq_a"], d["hq_d"], d["rounds"], d["wins_a"], d["wins_d"] = (a.u8() for _ in range(5))
    d["flags"] = a.u32()
    d["next_start"], d["round_start"], d["now"] = a.u32(), a.u32(), a.u32()   # 0x1ad790: u32 -> u64
    d["draws"] = max(0, d["rounds"] - d["wins_a"] - d["wins_d"])              # 0x2295d0
    d["trailer"] = parse_trailer(a)
    return d


def parse_ranking(a):                                 # 0x227dc0 read half (0x227eb8)
    d = dict(category=a.u8(), highlight=a.u8(), base=a.u8())
    d["names"] = [a.str(0x21) for _ in range(20)]
    d["values"] = a.u32s(20)
    d["trailer"] = parse_trailer(a)
    return d


def parse_merit(a):                                   # 0x228140 read half (0x228350)
    d = dict(b0c=a.u8(), count=a.u8())
    names = [a.str(0x21) for _ in range(0x21)]       # loop bound 0x21 at 0x2283c4
    raw10 = names[10].encode("cp932")
    count = raw10[0] if raw10 else 0                  # string #10 lands on +0x157 = the count
    if count > 16:
        raise cm.ParseError("count past the 16 slots")
    d["names"] = names[:10]
    d["pairs"] = [(a.str(0x21)) for _ in range(count)] + [a.str(0x21) for _ in range(count)]
    d["first"] = a.str(0x21)
    d["b5b3"], d["b599"], d["b5a3"], d["b5a2"] = a.u8(), a.bytes(9), a.bytes(16), a.u8()
    d["trailer"] = parse_trailer(a)
    return d


PAGE_PARSERS = {0x0F: parse_edict, 0x13: parse_hud, 0x0D: parse_ranking, 0x0E: parse_merit}


def parse_event(payload):
    """0x1a04f8: 11 <u8 id> <u8 n> n x {u8 tag, object}; tag 6 = shop (type 7)."""
    a = cm.Arc(payload, 1)
    assert payload[0] == 0x11
    d = dict(id=a.u8(), n=a.u8(), pages=[])
    for _ in range(d["n"]):
        tag = a.u8()
        if tag not in PAGE_PARSERS:
            raise cm.ParseError(f"tag {tag:#x} not modelled")
        d["pages"].append((tag, PAGE_PARSERS[tag](a)))
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return d


def parse_2f30(payload):                              # 0x1f3af0 read half (0x1f3cb8)
    a = cm.Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    assert sub == 0x30
    b = [a.u8() for _ in range(9)]
    d = dict(clan=b[0], streak=b[3], best=b[4], wins=b[7], losses=b[8], u0c=a.u32(), earned=a.u32(),
             total=a.u32(), unspent=a.u32())
    if not a.done():
        raise cm.ParseError("trailing bytes")
    return d


import datetime as _dt
import json as _json
import tempfile as _tempfile

from nobu import misc, npc, social, war_week

JST = _dt.timezone(_dt.timedelta(hours=9))


def t_jst(s):
    return _dt.datetime.fromisoformat(s).replace(tzinfo=JST).timestamp()


NAGOYA = dict(ODA, pos=[110, 90000, 3200, 100000, 0], merit=40)


class WarTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = _tempfile.TemporaryDirectory()
        self.env = Env(**{k: None for k in WAR_KNOBS})
        self.env.__enter__()
        os.environ["NOBU_WAR_DB"] = os.path.join(self.tmp.name, "war.json")
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "0"                  # a frozen war clock
        os.environ["NOBU_WAR_CLOCK_START"] = "2025-01-11T20:30"     # Saturday, inside round 1
        war.reset_state()
        social.SESSIONS.clear()

    def tearDown(self):
        war.reset_state()
        social.SESSIONS.clear()
        self.env.__exit__()
        self.tmp.cleanup()


class Board(WarTestBase):
    def board_ctx(self):
        ctx = {"char": dict(NAGOYA)}
        with Env(NOBU_WAR_INLINE="1"):
            frames = war.world_ready(ctx)
        return ctx, frames

    def test_spawn_frames_board_diplomacy_skirmish(self):
        ctx, frames = self.board_ctx()
        self.assertEqual([f[:2].hex() for f in frames], ["0b0a", "2f04", "2f30"])
        rec = cm.parse_notify_char(frames[0])["records"][0]
        self.assertEqual((rec["uid"], rec["cid"]), (0, 0xFEA00020))
        self.assertEqual(ctx["npcs"][0xFEA00020]["role"], "war_board")
        sk = parse_2f30(frames[2])
        self.assertEqual((sk["clan"], sk["wins"], sk["unspent"]), (6, 0, 0))
        self.assertIn("WAR 2F 30", world.describe(frames[2], "S>C"))

    def test_board_only_in_nagoya(self):
        ctx = {"char": dict(ODA, pos=[10, 1000, 0, 1000, 0])}
        with Env(NOBU_WAR_INLINE="1"):
            self.assertEqual([f[:2].hex() for f in war.world_ready(ctx)], ["2f04", "2f30"])
        with Env(NOBU_WAR_INLINE="1", NOBU_WAR_BOARD="0"):
            self.assertNotIn("0b0a", [f[:2].hex() for f in war.world_ready({"char": dict(NAGOYA)})])

    def test_talk_opens_the_board_through_world(self):
        ctx, _ = self.board_ctx()
        store = chars.CharStore(None)
        store.data["chars"] = {"7": [dict(NAGOYA)], "8": [dict(NAGOYA, cid=9, given="次郎", merit=99)]}
        out = war.answer(bytes.fromhex("28010005"), dict(ctx, target=0xFEA00020), store)
        self.assertEqual(out[0][:2].hex(), "0d06")
        ev = parse_event(out[1])
        tags = [t for t, _ in ev["pages"]]
        self.assertEqual(tags, [0x0F, 0x13, 0x0D])
        self.assertIn("WAR board event", world.describe(out[1], "S>C"))
        # through world.answer (war runs before trade / npc; the target resolves to the board)
        reps = world.answer(bytes.fromhex("28010006"), ctx)
        self.assertEqual(reps[0][:2].hex(), "0d06")
        ev = parse_event(reps[1])
        self.assertEqual(ev["id"], ctx["war_event"]["id"])
        edict, hud = ev["pages"][0][1], ev["pages"][1][1]
        self.assertEqual(edict["own"], 6)
        self.assertEqual(edict["current"], (1, 88, 5))                     # we (Oda) attack at Okehazama
        self.assertEqual((hud["attacker"], hud["defender"], hud["place"], hud["flags"]), (6, 5, 88, 0))
        # ranking page: the viewer's clan by deeds then merit
        rk = parse_event(war.answer(bytes.fromhex("28010007"), dict(ctx, target=0xFEA00020),
                                              store)[1])["pages"][2][1]
        self.assertEqual(rk["names"][:2], ["織田次郎", "織田太郎"])
        self.assertEqual(rk["category"], 1)
        self.assertEqual(len(rk["values"]), 2)

    def test_close_answer_is_consumed_only_for_our_event(self):
        ctx, _ = self.board_ctx()
        world.answer(bytes.fromhex("28010006"), ctx)
        eid = ctx["war_event"]["id"]
        from nobu import shops
        self.assertIsNone(war.answer(shops.encode_event_reply((eid + 1) & 0xFF, None), ctx))
        self.assertEqual(world.answer(shops.encode_event_reply(eid, None), ctx), [])
        self.assertIsNone(ctx["war_event"])

    def test_merit_page_is_opt_in_and_parses(self):
        ctx, _ = self.board_ctx()
        with Env(NOBU_WAR_BOARD_MERIT="1"):
            ev = parse_event(world.answer(bytes.fromhex("28010006"), ctx)[1])
        self.assertEqual(ev["pages"][-1][0], 0x0E)
        self.assertEqual(ev["pages"][-1][1]["pairs"], [])

    def test_no_hud_in_the_build_phase(self):
        os.environ["NOBU_WAR_CLOCK_START"] = "2025-01-07T12:00"      # Tuesday
        ctx, _ = self.board_ctx()
        ev = parse_event(world.answer(bytes.fromhex("28010006"), ctx)[1])
        self.assertEqual([t for t, _ in ev["pages"]], [0x0F, 0x0D])
        self.assertEqual(ev["pages"][0][1]["current"], (0, 0, 0))


class Schedule(WarTestBase):
    def test_phase_at(self):
        p = war_week.phase_at(t_jst("2025-01-07T12:00"))
        self.assertEqual(p["phase"], "build")
        self.assertEqual(p["week"], int(t_jst("2025-01-06T00:00")))
        self.assertEqual(war_week.phase_at(t_jst("2025-01-09T21:00"))["phase"], "declared")
        p = war_week.phase_at(t_jst("2025-01-11T21:00"))
        self.assertEqual((p["phase"], p["round"]), ("round", 0))
        self.assertEqual(p["now"] - p["round_start"], 3600)
        p = war_week.phase_at(t_jst("2025-01-12T12:00"))
        self.assertEqual((p["phase"], p["next_start"]), ("break", 6 * 86400 + 14 * 3600))
        self.assertEqual(war_week.phase_at(t_jst("2025-01-12T23:30"))["phase"], "results")
        self.assertEqual(war_week.phase_at(t_jst("2025-01-13T00:00"))["phase"], "build")

    def test_clock_scale_and_start(self):
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "60"
        t0 = war_week._T0
        self.assertAlmostEqual(war_week.war_now(t0 + 10), t_jst("2025-01-11T20:30") + 600)

    def test_a_whole_week(self):
        a, d = [], []
        targets = [dict(char=dict(ODA), push=a.append), dict(char=dict(ODA, cid=6), push=d.append,
                                                              shop_event={"id": 3}, war_event={"id": 4})]
        st = war.state()
        n = war_week.tick(t_jst("2025-01-07T12:00"), targets)          # new week, build
        self.assertEqual(st.wars, [])
        self.assertEqual(n, [])
        a.clear()
        n = war_week.tick(t_jst("2025-01-09T21:05"), targets)          # declaration
        self.assertEqual(n, ["Oda declares war on Tokugawa at Okehazama."])
        self.assertEqual(st.wars, [(6, 5, 88, 0x80)])
        p = parse_2f04(a[0])
        self.assertEqual((p["kind"], p["text"], p["wars"]), (1, n[0], [(6, 5, 88, 0x80)]))
        self.assertTrue(os.path.exists(os.environ["NOBU_WAR_DB"]))
        # the session with a shop event waiting gets no board, the other has none open
        self.assertFalse([f for f in a + d if f[:1] == b"\x11"])
        war_week.tick(t_jst("2025-01-11T20:10"), targets)                # round 1 starts
        self.assertEqual(st.wars, [(6, 5, 88, 0)])
        self.assertTrue(war_week.add_score(0, 1500, now=t_jst("2025-01-11T21:00")))
        self.assertTrue(war_week.add_score(1, 300, now=t_jst("2025-01-11T21:00")))
        war_week.tick(t_jst("2025-01-12T14:30"), targets)                # round 1 ends, round 2 on
        self.assertEqual(st.week["rounds"][0]["result"], "a")
        war_week.add_score(1, 900, now=t_jst("2025-01-12T15:00"))       # below the floor: a draw
        war_week.tick(t_jst("2025-01-12T20:30"), targets)
        war_week.add_score(0, 2000, now=t_jst("2025-01-12T21:00"))
        n = war_week.tick(t_jst("2025-01-12T23:10"), targets)
        self.assertEqual([r["result"] for r in st.week["rounds"]], ["a", "draw", "a"])
        self.assertIn("Oda wins the war at Okehazama (2-0).", n)
        self.assertEqual(st.wars, [])
        lw = st.last_war
        self.assertEqual((lw["winner"], lw["wins"]), (6, [2, 0, 3]))
        self.assertEqual(st.power[9], 100 - round(50 * 2 / 3))            # Mikawa, the defender's side
        self.assertEqual(st.power[10], 100)                                # Owari lost no round
        self.assertEqual(st.funds[6], max(0, 598 - round((50 + 50 * 1 / 3) * 9.96)))   # +100 weekly
        self.assertEqual(st.owners[9], 5)                                  # > 0: no transfer
        # persisted and reloadable
        with open(os.environ["NOBU_WAR_DB"], encoding="utf-8") as fh:
            data = _json.load(fh)
        again = war.WarState(data)
        self.assertEqual((again.last_war["winner"], again.wars, again.power[9]), (6, [], st.power[9]))
        # the edict of the next week shows the victory; the new week tops up funds and power
        war_week.tick(t_jst("2025-01-13T09:00"), targets)
        ed = parse_edict(cm.Arc(war.edict_for(6, st, war_week.view(st, t_jst("2025-01-13T09:00"))), 0))
        self.assertEqual(ed["last"], (1, 0, 88))
        ed5 = parse_edict(cm.Arc(war.edict_for(5, st, war_week.view(st, t_jst("2025-01-13T09:00"))), 0))
        self.assertEqual(ed5["last"][0], 2)
        self.assertEqual(st.power[9], min(100, 100 - round(50 * 2 / 3) + 10))
        self.assertEqual(st.funds[5], min(996, 598 - round(100 * 3 / 3 / 2 * 9.96) + 100))

    def test_transfer_when_power_runs_out(self):
        st = war.reset_state(dict(power=[0] * 9 + [10] + [100] * 12))
        st.week = dict(id=1, rounds=[dict(no=0, result="a"), dict(no=1, result="a")])
        rec = war_week.apply_results(st)
        self.assertEqual((rec["transfer"], st.owners[9], st.power[9]), (9, 6, 50))
        self.assertEqual(st.relation(6, 5), war.HOSTILE)

    def test_catch_up_after_downtime(self):
        n = war_week.tick(t_jst("2025-01-12T23:30"), [])
        self.assertEqual(n[0], "Oda declares war on Tokugawa at Okehazama.")
        self.assertIn("The war at Okehazama ends undecided (0-0).", n)
        self.assertEqual(len(war.state().week["rounds"]), 3)

    def test_open_board_is_refreshed(self):
        got = []
        ctx = dict(char=dict(NAGOYA), push=got.append, war_event={"id": 9})
        war_week.tick(t_jst("2025-01-09T21:05"), [ctx])
        boards = [f for f in got if f[:1] == b"\x11"]
        self.assertEqual(len(boards), 1)
        ev = parse_event(boards[0])
        self.assertEqual(ev["id"], ctx["war_event"]["id"])
        self.assertEqual(ev["pages"][0][1]["next"], (1, 88, 5))            # 2.114 next war line
        got.clear()
        with Env(NOBU_WAR_HUD_PUSH="0"):
            war_week.tick(t_jst("2025-01-11T20:10"), [ctx])
        self.assertFalse([f for f in got if f[:1] == b"\x11"])

    def test_schedule_off_writes_nothing(self):
        os.environ["NOBU_WAR_SCHEDULE"] = "0"
        self.assertEqual(war_week.tick(t_jst("2025-01-09T21:05"), []), [])
        self.assertFalse(os.path.exists(os.environ["NOBU_WAR_DB"]))
        self.assertEqual(war_week.view()["phase"], "static")
        self.assertIsNone(war.hud_for())

    def test_chat_status_and_debug_score(self):
        ctx = {"char": dict(ODA), "cid": 5}
        say = lambda t: bytes.fromhex("23080001") + social.vce.put_bytes(("/s/" + t).encode())
        out = world.answer(say("war"), ctx)
        text = b"".join(out).decode("utf-8", "replace")
        self.assertIn("Oda (attacker) vs Tokugawa at Okehazama", text)
        war_week.tick(t_jst("2025-01-11T20:30"), [])
        world.answer(say("war score a 250"), ctx)
        self.assertEqual(war.state().week["score"], [0, 0])               # debug off
        with Env(NOBU_WAR_DEBUG="1"):
            world.answer(say("war score a 250"), ctx)
        self.assertEqual(war.state().week["score"], [250, 0])
        with Env(NOBU_WAR_CHAT="0"):
            self.assertIsNone(war.answer(say("war"), ctx))

    def test_scheduler_and_deferred_spawn_on_a_loop(self):
        import asyncio
        os.environ["NOBU_WAR_SPAWN_DELAY"] = "0"
        os.environ["NOBU_WAR_TICK"] = "1"
        got = []
        ctx = {"char": dict(NAGOYA), "push": got.append}

        async def run():
            self.assertEqual(war.world_ready(ctx), [])
            war_week.ensure_scheduler()                                 # world.main does this
            await asyncio.sleep(0.05)
            task = war_week._TASK
            self.assertIsNotNone(task)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            asyncio.run(run())
        finally:
            war_week._TASK = None
        self.assertEqual([f[:2].hex() for f in got], ["0b0a", "2f04", "2f30"])
        self.assertTrue(os.path.exists(os.environ["NOBU_WAR_DB"]))         # the first tick saved


if __name__ == "__main__":
    unittest.main()
