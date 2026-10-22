"""Other players + parties with two fake sessions through world.answer: python -m unittest nobu.test_party -v"""
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, party, players, social, vce, world

LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]     # male Smith, Oda
KNOBS = ("NOBU_DEBUG_NPC", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN", "NOBU_WORLD_PUSH",
         "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_PLAYERS", "NOBU_SHOPS", "NOBU_GUILD", "NOBU_CRAFTING", "NOBU_BANK", "NOBU_REPAIR", "NOBU_DOCTOR", "NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER", "NOBU_LIFE_SHRINE", "NOBU_TOWN_GUARDS", "NOBU_TOWN_SPOTS")


def entry_event(rec, sub):
    """UI event 0x1893f0 posts for a same-area uid-0 record, read from the conditional move at 0x189608
    (sub 1) / 0x189c48 (sub 0xa): cid top byte 0xff -> 0xc0 / 0xbd, 0xfe -> 0xd5 / 0xd4 (entry +
    actor), anything else -> 0xa6 / 0xa3 (UI default, nothing)."""
    top = rec["cid"] >> 24
    ff, fe, other = {1: (0xC0, 0xD5, 0xA6), 0xA: (0xBD, 0xD4, 0xA3)}[sub]
    return ff if top == 0xFF else fe if top == 0xFE else other


def run(payload, ctx):
    return world.answer(payload, ctx)       # players / party are wired into world.answer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        os.environ["NOBU_SHOPS"] = "0"          # no Merchant: these tests count the spawn pushes
        os.environ["NOBU_GUILD"] = "0"          # no Hall Master either
        os.environ["NOBU_LIFE_SHRINE"] = "0"    # nor the shrine (lifecycle.py)
        os.environ["NOBU_CRAFTING"] = "0"       # no workshop keeper either
        os.environ["NOBU_BANK"] = "0"           # no money changer / storekeeper either
        os.environ["NOBU_REPAIR"] = "0"         # nor the repairer (nobu/repair.py)
        os.environ["NOBU_DOCTOR"] = "0"         # nor the doctor (nobu/doctor.py)
        os.environ["NOBU_TOWN_QUESTS"] = "0"   # nor the quest givers (nobu/townquests.py)
        os.environ["NOBU_FIELD_NPCS"] = "0"    # nor field people (nobu/fieldnpcs.py)
        os.environ["NOBU_ELDER"] = "0"         # nor the old sage (nobu/elder.py)
        os.environ["NOBU_TOWN_GUARDS"] = "0"    # nor the gate guards (npc.guard_spawn)
        os.environ["NOBU_TOWN_SPOTS"] = "0"     # the Guide on its offset beside the spawn point
        social.SESSIONS.clear()
        party.reset()
        self.a = self.session("a", 0x4000471, "織田", "信長")
        self.b = self.session("b", 0x4000472, "明智", "光秀")

    def tearDown(self):
        social.SESSIONS.clear()
        party.reset()
        world.STORE = None
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def session(self, sid, uid, family, given):
        got = []
        ctx = {"uid": uid, "push": got.append, "got": got}
        body = chars.write_create(dict(family=family, given=given, looks=LOOKS))
        run(bytes([5, 1]) + vce.put_u32(uid) + vce.put_u16(1) + body, ctx)
        cid = world.store().list(uid)[0]["cid"]
        run(bytes([5, 2]) + vce.put_u32(uid) + vce.put_u32(cid), ctx)
        social.register(sid, ctx)
        return ctx

    def enter(self, ctx):
        run(b"\x04", ctx)
        return run(bytes.fromhex("235a0013"), ctx)

    @staticmethod
    def cid(ctx):
        return ctx["char"]["cid"]


class OtherPlayers(Base):
    def test_wire_cid(self):
        self.assertEqual(players.wire_cid(2, 2), 2)
        self.assertEqual(players.wire_cid(2, 3), 0xFE000002)
        self.assertEqual(players.wire_cid(2), 0xFE000002)
        self.assertEqual(players.real_cid(0xFE000002), 2)
        self.assertEqual(players.real_cid(0x00A00001), 0x00A00001)
        self.assertEqual(players.real_cid(0xFEA00001), 0xFEA00001)      # NPC range: not a player
        self.assertFalse(players.is_player_wire(0xFEA00001))
        with self.assertRaises(ValueError):
            players.wire_cid(0xA00000, 1)

    def test_second_player_sees_the_first_and_is_shown_to_it(self):
        first = self.enter(self.a)
        self.assertEqual([p[:2].hex() for p in first], ["0b01", "0b0a", "0502", "0b01"])  # alone
        self.assertEqual(self.a["got"], [])
        second = self.enter(self.b)
        self.assertEqual([p[:2].hex() for p in second], ["0b01", "0b0a", "0502", "0b01", "0b0a"])
        (rec,) = cm.parse_notify_char(second[4])["records"]            # 0x1a2500 to the last byte
        self.assertEqual((rec["uid"], rec["cid"]), (0, 0xFE000000 | self.cid(self.a)))
        self.assertEqual((rec["family"], rec["given"]), ("織田", "信長"))
        self.assertEqual(entry_event(rec, 0xA), 0xD4)                  # 0x1d2df0: entry + actor
        self.assertNotIn(rec["job"], (0, 1))                           # 0x21c6e0 -> 0x21bd70
        self.assertEqual(rec["area"], chars.area_for(self.a["zone"]))  # same-area test 0x189ac8
        self.assertEqual(rec["params"], chars.char_params())           # PARAMS[8] = its speed
        (push,) = self.a["got"]
        (rec,) = cm.parse_notify_char(push)["records"]
        self.assertEqual((rec["uid"], rec["cid"]), (0, 0xFE000000 | self.cid(self.b)))
        self.assertEqual(rec["family"], "明智")

    def test_own_record_is_never_tagged(self):
        self.enter(self.a)
        own = cm.parse_notify_char(self.enter(self.b)[1])["records"][0]
        self.assertEqual((own["uid"], own["cid"]), (0x4000472, self.cid(self.b)))

    def test_movement_is_echoed_as_kind_6_update(self):
        self.enter(self.a)
        self.enter(self.b)
        self.a["got"].clear()
        mv = chars.encode_move(100500, 3456, 95800, 90, 0, area=10)
        self.assertEqual(run(mv, self.b), [])
        (upd,) = self.a["got"]
        (rec,) = cm.parse_notify_char(upd)["records"]
        self.assertEqual(rec["kind"], 6)                               # 0x189640 -> UI 0xd6
        self.assertEqual(rec["cid"], 0xFE000000 | self.cid(self.b))
        self.assertEqual((rec["pos"], rec["dir"]), ((100500, 3456, 95800), 90))
        self.assertIn("CHAR_LOCATION_UPDATE", players.describe(upd))
        self.assertEqual(self.b["got"], [])                           # no echo to the mover
        self.assertEqual(self.b["pos"]["x"], 100500)                  # world still records it

    def test_late_joiner_sees_the_last_position(self):
        self.enter(self.a)
        run(chars.encode_move(111, 3456, 222, 7, 0, area=10), self.a)
        rec = cm.parse_notify_char(self.enter(self.b)[4])["records"][0]
        self.assertEqual((rec["pos"], rec["dir"]), ((111, 3456, 222), 7))

    def test_other_zone_is_invisible(self):
        self.enter(self.a)
        run(b"\x04", self.b)
        self.b["zone"] = 1                                            # a different field
        out = run(bytes.fromhex("235a0013"), self.b)
        self.assertEqual([p[:2].hex() for p in out], ["0b01", "0b0a", "0502", "0b01"])
        self.assertEqual(self.a["got"], [])

    def test_disconnect_removes_the_character(self):
        self.enter(self.a)
        self.enter(self.b)
        self.a["got"].clear()
        players.on_leave(self.b)
        social.unregister("b")
        wb = 0xFE000000 | self.cid(self.b)
        self.assertEqual(self.a["got"], [bytes.fromhex("0b0400000000") + vce.put_u32(wb) + b"\x00"])
        self.assertIn("CHAR_DELETE", players.describe(self.a["got"][0]))

    def test_zone_enter_again_hides_until_the_next_spawn(self):
        self.enter(self.a)
        self.enter(self.b)
        self.a["got"].clear()
        run(b"\x04", self.b)
        self.assertEqual(self.a["got"][0][:2].hex(), "0b04")
        self.assertFalse(players.ready(self.b))
        run(chars.encode_move(1, 2, 3), self.b)                      # not in the world: silent
        self.assertEqual(len(self.a["got"]), 1)

    def test_target_set_on_a_player(self):
        self.enter(self.a)
        self.enter(self.b)
        wa = 0xFE000000 | self.cid(self.a)
        (rep,) = run(bytes.fromhex("25010031") + vce.put_u32(wa), self.b)
        d = cm.parse_cmd25(rep)
        self.assertEqual((d["res"], d["target"]["cid"], d["target"]["family"]), (0, wa, "織田"))
        (rep,) = run(bytes.fromhex("25010032") + vce.put_u32(0xFE00FFFF), self.b)
        self.assertEqual(cm.parse_cmd25(rep)["res"], -1)

    def test_say_reaches_the_zone_with_the_wire_cid(self):
        self.enter(self.a)
        self.enter(self.b)
        self.b["got"].clear()
        self.assertEqual(players.say_to_zone(self.a, "こんにちは"), 1)
        d = cm.parse_notify_chat(self.b["got"][0])
        self.assertEqual((d["sub"], d["kind"], d["cid"], d["msg"]),
                         (1, 1, 0xFE000000 | self.cid(self.a), "こんにちは"))

    def test_disabled(self):
        os.environ["NOBU_PLAYERS"] = "0"
        self.enter(self.a)
        self.assertEqual(len(self.enter(self.b)), 4)
        self.assertEqual(self.a["got"], [])


class Parties(Base):
    def setUp(self):
        super().setUp()
        self.enter(self.a)
        self.enter(self.b)
        self.wa = 0xFE000000 | self.cid(self.a)
        self.wb = 0xFE000000 | self.cid(self.b)
        self.a["got"].clear()
        self.b["got"].clear()

    def invite(self):
        return run(bytes.fromhex("0701") + vce.put_u32(self.wb), self.a)

    def test_invite(self):
        self.assertEqual(self.invite(), [bytes.fromhex("070100000000") + vce.put_u32(self.wb)])
        self.assertEqual(self.b["got"], [bytes.fromhex("0e01") + vce.put_u32(self.wa)])
        self.assertIn("INVITE", party.describe(self.b["got"][0], "S>C"))
        self.assertIn("INVITE cid=", party.describe(bytes.fromhex("0701") + vce.put_u32(self.wb)))

    def test_invite_accept_join(self):
        self.invite()
        self.b["got"].clear()
        out = run(bytes.fromhex("0703") + vce.put_u32(self.wa), self.b)
        self.assertEqual(out[0], bytes.fromhex("070300000000") + vce.put_u32(self.wa))
        subs = [p[:2].hex() for p in out[1:]]
        self.assertEqual(subs, ["0e06", "0e05", "0e03", "0e07", "0e07", "0e0a", "0e0b"])
        info = vce.Reader(out[2], 2)
        self.assertEqual([info.u32() for _ in range(info.length())], [self.wa, self.cid(self.b)])
        self.assertEqual(out[3], bytes.fromhex("0e0300000001"))       # pid 1
        leader = vce.Reader(out[7], 2).u32()
        self.assertEqual(leader, self.wa)
        a_subs = [p[:2].hex() for p in self.a["got"]]
        self.assertEqual(a_subs[0], "0e06")
        self.assertEqual(vce.Reader(self.a["got"][0], 2).u32(), self.wb)
        info = vce.Reader(self.a["got"][1], 2)
        self.assertEqual([info.u32() for _ in range(info.length())], [self.cid(self.a), self.wb])
        self.assertEqual(party.PARTIES[1], {"leader": self.cid(self.a),
                                            "members": [self.cid(self.a), self.cid(self.b)]})

    def test_invite_back_counts_as_accept(self):
        self.invite()
        out = run(bytes.fromhex("0701") + vce.put_u32(self.wa), self.b)
        self.assertEqual(out[0], bytes.fromhex("070100000000") + vce.put_u32(self.wa))
        self.assertEqual(party.party_of(self.cid(self.b)), 1)

    def test_accept_without_invitation_fails(self):
        out = run(bytes.fromhex("0703") + vce.put_u32(self.wa), self.b)
        self.assertEqual(out, [bytes.fromhex("0703ffffffff") + vce.put_u32(self.wa)])

    def test_decline(self):
        self.invite()
        self.a["got"].clear()
        out = run(bytes.fromhex("0704") + vce.put_u32(self.wa), self.b)
        self.assertEqual(out, [bytes.fromhex("070400000000") + vce.put_u32(self.wa)])
        self.assertIn("declined", cm.parse_notify_chat(self.a["got"][0])["msg"])
        self.assertEqual(party.PARTIES, {})

    def join(self):
        self.invite()
        run(bytes.fromhex("0703") + vce.put_u32(self.wa), self.b)
        self.a["got"].clear()
        self.b["got"].clear()

    def test_member_leaves_two_person_party_disbands(self):
        self.join()
        out = run(bytes.fromhex("0702"), self.b)
        self.assertEqual(out, [bytes.fromhex("070200000000") + vce.put_u32(self.cid(self.b))])
        self.assertEqual([p[:2].hex() for p in self.b["got"]], ["0e02", "0e03", "0e0a"])
        self.assertEqual(self.b["got"][1], bytes.fromhex("0e0300000000"))
        self.assertEqual([p[:2].hex() for p in self.a["got"]], ["0e02", "0e03", "0e0a"])
        self.assertEqual(party.PARTIES, {})

    def test_leader_kicks(self):
        self.join()
        out = run(bytes.fromhex("0704") + vce.put_u32(self.wb), self.a)
        self.assertEqual(out, [bytes.fromhex("070400000000") + vce.put_u32(self.wb)])
        self.assertEqual(self.b["got"][0], bytes.fromhex("0e02") + vce.put_u32(self.cid(self.b)))
        self.assertEqual(party.PARTIES, {})

    def test_leader_disconnect_disbands(self):
        self.join()
        players.on_leave(self.a)
        self.assertIn(bytes.fromhex("0e02") + vce.put_u32(self.wa), self.b["got"])
        self.assertIn(bytes.fromhex("0e0300000000"), self.b["got"])
        self.assertEqual(party.PARTIES, {})

    def test_party_chat(self):
        self.join()
        out = party.party_chat(self.b, "行くぞ")
        self.assertEqual(cm.parse_notify_chat(out[0])["cid"], self.cid(self.b))
        d = cm.parse_notify_chat(self.a["got"][0])
        self.assertEqual((d["sub"], d["cid"], d["msg"]), (4, self.wb, "行くぞ"))

    def test_party_chat_outside_a_party(self):
        self.assertEqual(party.party_chat(self.a, "x"), [])

    def test_info_and_status(self):
        self.assertEqual(run(bytes.fromhex("0706"), self.a),
                         [bytes.fromhex("070600000000") + vce.put_u32(0), bytes.fromhex("0e0300000000")])
        self.join()
        out = run(bytes.fromhex("0705"), self.a)
        self.assertEqual(out[0], bytes.fromhex("070500000000") + vce.put_u32(1))
        self.assertIn("NOTIFY_PARTY INFO", party.describe(out[1], "S>C"))

    def test_invite_by_name(self):
        out = run(bytes.fromhex("0707") + vce.put_bytes("明智光秀".encode()), self.a)
        self.assertEqual(out[0], bytes.fromhex("070100000000") + vce.put_u32(self.wb))
        out = run(bytes.fromhex("0707") + vce.put_bytes("誰か".encode()), self.a)
        self.assertIn("not online", cm.parse_notify_chat(out[0])["msg"])

    def test_update_layout(self):
        p = party.notify_update(self.wb, 80, 50)
        self.assertEqual(p, bytes.fromhex("0e07") + vce.put_u32(self.wb) + bytes([80, 50, 0, 0, 0]))
        self.assertEqual(party.describe(p, "S>C"), f"NOTIFY_PARTY UPDATE cid={self.wb:#x} hp=80% mp=50%")

    def test_other_commands_untouched(self):
        self.assertIsNone(party.answer(bytes.fromhex("0503") + vce.put_u32(1), self.a))
        self.assertEqual(party.answer(bytes.fromhex("070c"), self.a), [])


if __name__ == "__main__":
    unittest.main()


class Wiring(Base):
    """NPCs, the debug enemy and other players side by side through world.answer."""
    KNOBS2 = ("NOBU_DEBUG_ENEMY", "NOBU_QUESTS")

    def setUp(self):
        super().setUp()
        self.env2 = {k: os.environ.pop(k) for k in list(os.environ) if k in self.KNOBS2}
        os.environ["NOBU_DEBUG_NPC"] = "1"
        os.environ["NOBU_DEBUG_ENEMY"] = "1"
        self.enter(self.a)
        self.pushes = self.enter(self.b)

    def tearDown(self):
        for k in self.KNOBS2:
            os.environ.pop(k, None)
        os.environ.update(self.env2)
        super().tearDown()

    def records(self):
        return [r for p in self.pushes if p[:2] == b"\x0b\x0a"
                for r in cm.parse_notify_char(p)["records"] if r["uid"] == 0]

    def test_every_uid0_record_is_drawn_and_distinct(self):
        recs = {r["family"]: r for r in self.records()}
        self.assertEqual(set(recs), {"Guide", "Bandit", "Overseer", "織田"})
        self.assertEqual(len({r["cid"] for r in recs.values()}), 4)
        for r in recs.values():
            self.assertEqual(cm.npc_ui_event(r, 0xA), 0xD4, r["family"])
            self.assertTrue(cm.npc_actor_created(r), r["family"])
        from nobu import npc
        for name in ("Guide", "Bandit", "Overseer"):
            self.assertTrue(npc.is_npc_cid(recs[name]["cid"]), name)
        self.assertTrue(players.is_player_wire(recs["織田"]["cid"]))
        # talk-only NPCs and players read "○Look"; the faction-0 Bandit "○Attack"
        self.assertEqual({n: cm.action_hint(r) for n, r in recs.items()},
                         {"Guide": 0xD, "Overseer": 0xD, "織田": 0xD, "Bandit": 0xA})

    def test_target_and_circle_route_to_the_owner(self):
        from nobu import battle, npc
        guide, bandit = npc.npc_cid(), battle.DEFAULT_ENEMY_CID
        wa = 0xFE000000 | self.cid(self.a)
        for cid, family in ((guide, "Guide"), (bandit, "Bandit"), (wa, "織田")):
            (rep,) = run(bytes.fromhex("25010031") + vce.put_u32(cid), self.b)
            d = cm.parse_cmd25(rep)
            self.assertEqual((d["res"], d["target"]["cid"], d["target"]["family"]), (0, cid, family))
        run(bytes.fromhex("25010032") + vce.put_u32(guide), self.b)
        out = run(bytes.fromhex("28010033"), self.b)                 # ○ on the Guide (Look path)
        self.assertEqual(out[0][:2].hex(), "0d06")
        self.assertTrue(cm.parse_notify_chat(out[0])["msg"].startswith("Guide: "))
        run(bytes.fromhex("25010034") + vce.put_u32(bandit), self.b)
        out = run(bytes.fromhex("28010035"), self.b)                 # ○ on the Bandit: battle
        self.assertEqual(out[-1][0], 0x0F)
