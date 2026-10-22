"""Zone travel through world.answer: python -m unittest nobu.test_zones -v

Client model used here: the 0B 02 WARPED handler 0x189760 reloads the world only when the area byte is
nonzero and differs from net+0x57; the reload ends with the world UI's 23 5a, which world.py answers with
the spawn pushes (CHAR_LOCATION area / position = where the client puts the player)."""
import json
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, social, vce, world, zones

UID = 0x4000471
LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
KNOBS = ("NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN", "NOBU_WORLD_PUSH",
         "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_PLAYERS", "NOBU_SHOPS", "NOBU_SHOPS_FILE", "NOBU_ZONES",
         "NOBU_ZONES_FILE", "NOBU_ZONE_RANGE", "NOBU_ZONE_ALL_GATES", "NOBU_ZONE_PROVINCES", "NOBU_ZONE_CMDS",
         "NOBU_ZONE_DEBUG", "NOBU_ZONE_PENDING_SECS", "NOBU_ENEMIES", "NOBU_BATTLE_FIGHT", "NOBU_QUESTS")


class ClientNet:
    """net+0x57 (area) / net+0x50 (map) as the WARPED handler 0x189760 keeps them."""

    def __init__(self):
        self.area, self.map, self.reloads = 0, 0, 0

    def feed(self, payload):
        if payload[:2] != b"\x0b\x02":
            return False
        r = vce.Reader(payload, 2)
        area, zone = r.u8(), r.u16()
        if area == 0 or area == self.area:
            return False                          # 0x1897a4 / 0x1897ac: no reload
        self.area, self.map, self.reloads = area, zone, self.reloads + 1
        return True


def warped(frames):
    return [f for f in frames if f[:2] == b"\x0b\x02"]


def self_location(frames):
    """(area, (x, y, z)) of the first CHAR_LOCATION for our uid in a spawn burst."""
    for f in frames:
        if f[:2] == b"\x0b\x01":
            for rec in cm.parse_notify_char(f)["records"]:
                if rec["uid"] == UID:
                    return rec["area"], tuple(rec["pos"][:3]) if len(rec["pos"]) > 3 else tuple(rec["pos"])
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        os.environ["NOBU_SHOPS_FILE"] = os.path.join(self.tmp.name, "shops.json")
        os.environ["NOBU_ENEMIES"] = "0"
        self.path = os.path.join(self.tmp.name, "chars.json")
        world.STORE = chars.CharStore(self.path)
        social.SESSIONS.clear()
        self.net = ClientNet()
        self.ctx = self.login()
        self.seq = 0x100

    def tearDown(self):
        world.STORE = None
        social.SESSIONS.clear()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def login(self, uid=UID, family="織田", given="信長", sid="z"):
        ctx = {"uid": uid, "push": lambda f: None}
        body = chars.write_create(dict(family=family, given=given, looks=LOOKS))
        world.answer(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body, ctx)
        cid = world.store().list(uid)[0]["cid"]
        world.answer(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(cid), ctx)
        social.register(sid, ctx)
        out = world.answer(b"\x04", ctx)
        if uid == UID:
            self.assertTrue(self.net.feed(out[1]))
        return ctx

    def ready(self, ctx=None):
        return world.answer(bytes.fromhex("235a0013"), ctx or self.ctx)

    def move(self, x, z, y=3200, ctx=None, area=None):
        ctx = ctx or self.ctx
        area = chars.area_for(ctx["zone"]) if area is None else area
        return world.answer(chars.encode_move(x, y, z, 0, 0, area=area), ctx)

    def circle(self, ctx=None):
        """○ at a gate: 28 02 <seq> (header only, builder 0x1c9190)."""
        self.seq += 1
        return world.answer(bytes.fromhex("2802") + vce.put_u16(self.seq), ctx or self.ctx)

    def travel(self, x, z, y=3200):
        """Walk to (x, z), press ○, let the client reload; returns (warp frames, spawn frames)."""
        self.move(x, z, y)
        out = self.circle()
        w = warped(out)
        self.assertEqual(len(w), 1, [f.hex() for f in out])
        self.assertTrue(self.net.feed(w[0]))
        return out, self.ready()


class AreaIndex(unittest.TestCase):
    def test_place_index(self):
        for p in range(1, 22):
            self.assertEqual(zones.area_of(p), p)                   # provinces: area == map id
        self.assertEqual((zones.area_of(110), zones.place_name(110)), (31, "Nagoya"))
        self.assertEqual((zones.area_of(410), zones.place_name(410)), (56, "Nagoya Castle"))
        self.assertEqual(zones.area_of(101), 22)                    # Kasugayama, first town
        self.assertEqual(zones.area_of(401), 47)                    # Kasugayama Castle
        self.assertEqual(zones.map_index(420), 420 - 0x163 - 1)     # the gap at 419
        self.assertEqual(zones.area_of(0), 1)
        self.assertEqual(chars.area_for(110), 31)

    def test_nagoya_and_owari_differ(self):
        self.assertNotEqual(chars.area_for(110), chars.area_for(10))
        self.assertNotEqual(chars.area_for(110), chars.area_for(410))


class TownToField(Base):
    def test_login_uses_the_town_area(self):
        self.assertEqual((self.net.area, self.net.map), (31, 110))
        area, pos = self_location(self.ready())
        self.assertEqual(area, 31)
        self.assertEqual(pos, (103713, 3200, 112594))

    def test_gate_to_owari_and_back(self):
        self.ready()
        out, spawn = self.travel(103000, 113100)                    # at the gate 2 doors (z 113000)
        self.assertEqual(out[0][:2], b"\x0d\x06")                     # "Travelling to Owari..."
        self.assertEqual((self.net.area, self.net.map), (10, 10))
        self.assertEqual(self.ctx["zone"], 10)
        self.assertEqual(self_location(spawn), (10, (110137, 3952, 86527)))
        saved = chars.CharStore(self.path).find(UID, self.ctx["cid"])["pos"]
        self.assertEqual(saved, [10, 110137, 3952, 86527, 0])         # on disk at once
        self.assertEqual(self.ctx["warps"][-1]["why"], "nagoya-gate2")
        # back through the field's Nagoya gate
        out, spawn = self.travel(109700, 86400, y=3952)
        self.assertEqual((self.net.area, self.net.map), (31, 110))
        self.assertEqual(self_location(spawn), (31, (103713, 3200, 112594)))
        self.assertEqual(self.net.reloads, 3)                          # login + 2 warps

    def test_stale_moves_do_not_overwrite_the_arrival(self):
        self.ready()
        self.move(103000, 113100)
        self.circle()
        self.assertEqual(self.move(103000, 113200, area=31), [])      # old field, still loading
        c = chars.CharStore(self.path).find(UID, self.ctx["cid"])
        self.assertEqual(c["pos"][:4], [10, 110137, 3952, 86527])
        self.move(110137, 86600, y=3952, area=10)                      # the new field reports
        self.assertNotIn("zone_pending", self.ctx)
        self.assertEqual(self.ctx["char"]["pos"][:4], [10, 110137, 3952, 86600])

    def test_town_npcs_follow_the_zone(self):
        first = self.ready()
        merchant = [r for f in first if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]
                    if r["cid"] == 0xFEA00003]
        self.assertEqual(merchant[0]["area"], 31)                      # same area as the player
        _, spawn = self.travel(103000, 113100)
        cids = [r["cid"] for f in spawn if f[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(f)["records"]]
        self.assertNotIn(0xFEA00003, cids)                             # the Merchant stays in Nagoya
        self.assertNotIn(0xFEA00003, self.ctx.get("npcs", {}))

    def test_other_gates_and_nothing_here(self):
        self.ready()
        self.move(95000, 95000)
        self.assertEqual(self.circle(), [])                            # no exit near the town centre
        os.environ["NOBU_ZONE_DEBUG"] = "1"
        (line,) = self.circle()
        self.assertEqual(line[:2], b"\x0d\x06")
        self.assertIn(b"map 110", line)
        os.environ["NOBU_ZONE_ALL_GATES"] = "0"
        self.move(106000, 76900)
        self.assertEqual(warped(self.circle()), [])                   # gate 1 only with ALL_GATES
        os.environ["NOBU_ZONE_ALL_GATES"] = "1"
        _, spawn = self.travel(106000, 76900)
        self.assertEqual(self_location(spawn), (56, (25325, 3200, 19114)))     # gate 1 = the castle gate

    def test_castle_round_trip(self):
        self.ready()
        _, spawn = self.travel(111650, 106600)
        self.assertEqual((self.net.area, self.net.map), (56, 410))
        self.assertEqual(self_location(spawn), (56, (25325, 3200, 19114)))
        _, spawn = self.travel(26000, 19600)
        self.assertEqual((self.net.area, self.net.map), (31, 110))
        self.assertEqual(self_location(spawn), (31, (111385, 3200, 106579)))


class Provinces(Base):
    def test_owari_to_mikawa_and_back(self):
        self.ready()
        self.travel(103000, 113100)                                    # into the Owari field
        _, spawn = self.travel(192426, 147904, y=5028)                 # east checkpoint
        self.assertEqual((self.net.area, self.net.map), (9, 9))
        self.assertEqual(self_location(spawn), (9, (53172, 4278, 73728)))
        _, spawn = self.travel(52748, 73230, y=4281)                   # Mikawa's west checkpoint
        self.assertEqual(self_location(spawn), (10, (191897, 5026, 148050)))

    def test_every_province_exit_has_a_way_back(self):
        ex = [e for e in zones.EXITS if e["kind"] in ("checkpoint", "gate")]
        for e in ex:
            back = [b for b in ex if b["map"] == e["to"] and b["to"] == e["map"]]
            self.assertTrue(back, e["id"])
            # the arrival point is within reach of an exit back (the gate the player arrives at)
            ax, _, az, _ = e["arrive"]
            self.assertTrue(any(((b["at"][0] - ax) ** 2 + (b["at"][1] - az) ** 2) ** 0.5 <= b["r"] + 500
                                for b in back), e["id"])

    @unittest.skipUnless(os.path.exists(zones.PROVINCE_FILE), "needs the game data (python -m extract)")
    def test_every_generated_exit_has_a_way_back(self):
        ex = [e for e in zones.exits() if e["kind"] in ("checkpoint", "border")]
        self.assertGreater(len(ex), 100)
        for e in ex:
            back = [b for b in ex if b["map"] == e["to"] and b["to"] == e["map"] and b["kind"] == "checkpoint"]
            self.assertTrue(back, e["id"])
            ax, _, az, _ = e["arrive"]
            self.assertTrue(any(((b["at"][0] - ax) ** 2 + (b["at"][1] - az) ** 2) ** 0.5 <= b["r"] + 500
                                for b in back), e["id"])

    @unittest.skipUnless(os.path.exists(zones.PROVINCE_FILE), "needs the game data (python -m extract)")
    def test_suruga_west_checkpoint_leads_to_totomi(self):
        # 28 02 x8 at the Suruga west checkpoint got no answer (only Owari's existed)
        e = zones.find_exit(7, 47106, 243745)
        self.assertEqual((e["id"], e["to"]), ("suruga-cp-w", 8))
        back = zones.find_exit(8, e["arrive"][0], e["arrive"][2])
        self.assertEqual((back["id"], back["to"]), ("totomi-cp-e", 7))

    def test_provinces_knob(self):
        os.environ["NOBU_ZONE_PROVINCES"] = "0"
        self.ready()
        self.travel(103000, 113100)
        self.move(192426, 147904, y=5028)
        self.assertEqual(warped(self.circle()), [])


class Commands(Base):
    def say(self, text):
        self.seq += 1
        return world.answer(bytes.fromhex("2308") + vce.put_u16(self.seq)
                            + vce.put_bytes(("/s/" + text).encode("utf-8")), self.ctx)

    def test_where_and_warp(self):
        self.ready()
        (line,) = self.say("where")
        self.assertIn("Nagoya (map 110, area 31)".encode(), line)
        self.assertIn(b"nearest exit nagoya-gate2", line)
        out = self.say("warp 10")
        self.assertTrue(self.net.feed(warped(out)[0]))
        self.assertEqual(self_location(self.ready()), (10, chars.FIELD_SPAWN[10]))
        out = self.say("warp 10 101000 96000")                        # same map: 0B 08 kind 1
        self.assertEqual(out[0][:2], b"\x0b\x08")
        upd = cm.parse_notify_char(out[0])["records"][0]
        self.assertEqual((upd["kind"], upd["cid"]), (1, self.ctx["cid"]))
        self.assertIn(b"usage", self.say("warp x")[0])
        self.assertIn(b"not a valid map", self.say("warp 9999")[0])

    def test_plain_chat_still_echoes(self):
        self.ready()
        (echo,) = self.say("hello")
        self.assertEqual(echo[:2], b"\x0d\x01")

    def test_same_area_is_refused(self):
        os.environ["NOBU_AREA"] = "5"
        self.ready()
        self.move(103000, 113100, area=5)
        (line,) = self.circle()
        self.assertIn(b"Cannot travel", line)
        self.assertEqual(self.ctx["zone"], 110)


class TwoPlayers(Base):
    def test_the_other_player_sees_the_warp(self):
        got = []
        other = self.login(uid=0x4000472, family="明智", given="光秀", sid="o")
        other["push"] = got.append
        self.ready()
        self.ready(other)
        got.clear()
        self.travel(103000, 113100)
        dels = [f for f in got if f[:2] == b"\x0b\x04"]
        self.assertEqual(len(dels), 1)                                 # hidden from Nagoya
        got.clear()
        self.move(109700, 86400, y=3952, ctx=other)
        self.assertEqual(got, [])                                      # nothing about us in the town


class Data(unittest.TestCase):
    def test_describe(self):
        self.assertIn("use object", world.describe(bytes.fromhex("28020008")))
        self.assertIn("WARPED", world.describe(chars.zone_enter(10, 10), "S>C"))
        self.assertIn("Owari", world.describe(chars.zone_enter(10, 10), "S>C"))

    def test_zones_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "zones.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump([dict(id="test", map=110, at=[95000, 95000], r=300, to=10,
                                arrive=[100460, 3455, 95716, 0], kind="gate", proven=False)], fh)
            os.environ["NOBU_ZONES_FILE"] = p
            try:
                self.assertEqual(zones.find_exit(110, 95100, 95000)["id"], "test")
            finally:
                os.environ.pop("NOBU_ZONES_FILE")
            self.assertIsNone(zones.find_exit(110, 95100, 95000))


if __name__ == "__main__":
    unittest.main()
