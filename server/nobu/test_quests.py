"""Quests (cmd 0x34, 2a 03, tutorial): python -m unittest nobu.test_quests -v

Everything goes through world.answer (quests.answer runs before npc.answer; quests.world_ready
after npc.debug_spawn on 23 5a)."""
import os
import tempfile
import unittest

from nobu import chars, clientmodel as cm, quests, vce, world


# ---------------------------------------------------------------------------- client model
def parse_entry(a):                                   # 0x1f26c0 read half (0x1f2758..)
    return dict(id=a.u32(), title=a.str(0x41), text=a.str(0xc1))


def parse_cmd34(payload):
    """0x28d3d0: u8 sub, u16 seq, then table 0x428ec0. Raises cm.ParseError like the client."""
    a = cm.Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 1:                                      # 0x1f27f0: u8 count, entries (+4, 0x108 each)
        n = a.u8()
        d["entries"] = [parse_entry(a) for _ in range(n)]
    elif sub in (2, 4):
        d["entry"] = parse_entry(a)
    elif sub == 3:
        d["id"] = a.u32()
    else:
        raise cm.ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise cm.ParseError("trailing bytes")
    d["ui_event"] = 0x4F + sub                        # 0x28d780, table 0x428ee0
    return d


class ClientList:
    """The UI list (0x4b8550 + 0x1b46c): 16 slots, id 0 = free; 0x1d6870 / 0x1d68b0 / 0x1d6a30."""

    def __init__(self):
        self.slots = [None] * 16
        self.msgs = []                                # window messages (0x1fc400) with the seq

    def apply(self, payload):
        d = parse_cmd34(payload)
        if d["sub"] == 1:
            self.slots = (d["entries"] + [None] * 16)[:16]
        elif d["sub"] in (2, 4):
            e = d["entry"]
            hit = [i for i, s in enumerate(self.slots) if s and s["id"] == e["id"]]
            if hit:
                self.slots[hit[0]] = e
            elif d["sub"] == 2:
                self.slots[self.slots.index(None)] = e
            if d["sub"] == 2:
                self.msgs.append((0x22, d["seq"]))
        elif d["sub"] == 3:
            self.slots = [s for s in self.slots if not (s and s["id"] == d["id"])]
            self.slots += [None] * (16 - len(self.slots))
            self.msgs.append((0x22, d["seq"]))
        return d

    def titles(self):
        return [s["title"] for s in self.slots if s]


# ---------------------------------------------------------------------------- fixtures
class Base(unittest.TestCase):
    UID = 0x4000471
    LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]   # male Smith, Oda
    KNOBS = ("NOBU_DEBUG_NPC", "NOBU_NPC_CID", "NOBU_NPC_DX", "NOBU_NPC_DZ", "NOBU_NPC_TEXT",
             "NOBU_NPC_POPUP", "NOBU_NPC_TALK_ON_TARGET", "NOBU_ZONE", "NOBU_AREA", "NOBU_SPAWN",
             "NOBU_WORLD_PUSH", "NOBU_START_TOWN", "NOBU_FORCE_HOME", "NOBU_QUESTS",
             "NOBU_QUEST_OVERSEER", "NOBU_QUEST_OVERSEER_CID", "NOBU_SHOPS", "NOBU_GUILD", "NOBU_CRAFTING", "NOBU_BANK", "NOBU_REPAIR", "NOBU_DOCTOR", "NOBU_TOWN_QUESTS", "NOBU_FIELD_NPCS", "NOBU_ELDER", "NOBU_LIFE_SHRINE", "NOBU_TOWN_SPOTS")
    DEBUG_NPC = "1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "chars.json")
        world.STORE = chars.CharStore(self.path)
        self.env = {k: os.environ.pop(k) for k in list(os.environ) if k in self.KNOBS}
        os.environ["NOBU_DEBUG_NPC"] = self.DEBUG_NPC
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
        os.environ["NOBU_TOWN_SPOTS"] = "0"     # talk from the spawn point: NPCs on their offsets
        self.ctx = {"uid": self.UID}
        body = chars.write_create(dict(family="織田", given="信長", looks=self.LOOKS))
        world.answer(bytes([5, 1]) + vce.put_u32(self.UID) + vce.put_u16(1) + body, self.ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(self.UID) + vce.put_u32(1), self.ctx)
        world.answer(b"\x04", self.ctx)
        self.ui = ClientList()

    def tearDown(self):
        world.STORE = None
        for k in self.KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.env)
        self.tmp.cleanup()

    def answer(self, payload):
        return world.answer(payload, self.ctx)

    def spawn(self):
        return world.answer(bytes.fromhex("235a0013"), self.ctx)

    def feed(self, frames):
        for f in frames:
            if f[0] == 0x34:
                self.ui.apply(f)
        return frames

    def target(self, cid):
        return self.answer(bytes.fromhex("25010031") + vce.put_u32(cid))

    def talk(self, seq=0x40):
        return self.feed(self.answer(bytes.fromhex("2801") + vce.put_u16(seq)))


# ---------------------------------------------------------------------------- tests
class Wire(unittest.TestCase):
    def test_layouts_parse_to_the_end(self):
        e = quests.entry(1001, "Visit the castle overseer", "Objective: talk to the Overseer.")
        lst = quests.quest_list([e, quests.entry(7, "二", "説明")])
        d = parse_cmd34(lst)
        self.assertEqual((d["sub"], d["seq"], d["ui_event"]), (1, 0, 0x50))
        self.assertEqual([x["id"] for x in d["entries"]], [1001, 7])
        self.assertEqual(parse_cmd34(quests.quest_add(e, 0x2a))["entry"]["title"],
                         "Visit the castle overseer")
        self.assertEqual(parse_cmd34(quests.quest_remove(1001))["id"], 1001)
        self.assertEqual(parse_cmd34(quests.quest_update(e))["ui_event"], 0x53)
        self.assertEqual(quests.quest_remove(1001, 5).hex(), "3403" + "0005" + "000003e9")
        self.assertEqual(parse_cmd34(quests.quest_list([])), dict(sub=1, seq=0, entries=[], ui_event=0x50))

    def test_long_texts_are_clipped_to_the_client_fields(self):
        e = quests.entry(1, "あ" * 100, "い" * 200)
        d = parse_cmd34(quests.quest_add(e))["entry"]          # would raise if it did not fit
        self.assertEqual(len(d["title"].encode("cp932")), 0x40)
        self.assertEqual(len(d["text"].encode("cp932")), 0xC0)
        with self.assertRaises(ValueError):
            quests.entry(0, "x", "y")
        self.assertEqual(len(parse_cmd34(quests.quest_list([e] * 20))["entries"]), 16)

    def test_describe(self):
        self.assertEqual(quests.describe(bytes.fromhex("2a030033000003e9")),
                         "QUEST detail request seq=51 id=1001")
        e = quests.entry(1001, "T", "x")
        self.assertIn("QUEST add/update seq=0 id=1001 'T'", quests.describe(quests.quest_add(e), "S>C"))
        self.assertIn("n=1 1001:'T'", quests.describe(quests.quest_list([e]), "S>C"))
        self.assertIsNone(quests.describe(bytes.fromhex("2a050026"), "C>S"))


class Tutorial(Base):
    def test_spawn_adds_the_overseer_and_no_list_without_quests(self):
        pushes = self.feed(self.spawn())
        self.assertEqual([p[:2].hex() for p in pushes], ["0b01", "0b0a", "0502", "0b01", "0b0a", "0b0a", "1717"])
        (rec,) = cm.parse_notify_char(pushes[5])["records"]
        self.assertEqual((rec["uid"], rec["cid"], rec["family"]), (0, 0xFEA00002, "Overseer"))
        self.assertEqual(cm.npc_ui_event(rec, 0xA), 0xD4)
        self.assertEqual(self.ui.titles(), [])
        self.assertIn("QUEST detail", world.describe(bytes.fromhex("2a030033000003e9")))

    def test_full_chain_and_persistence(self):
        self.spawn()
        # talk to the guide: offer, quest added, system line
        self.target(0xFEA00001)
        reps = self.talk()
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0d05", "3402", "0d06"])
        self.assertEqual(self.ui.titles(), ["Visit the castle overseer"])
        self.assertEqual(self.ui.msgs, [(0x22, 0)])
        self.assertEqual(chars.CharStore(self.path).find(self.UID, 1)["quests"]["1001"]["state"], "active")
        # the quest window asks for the detail: 2a 03 <seq> <id> -> 34 02 with the same seq
        (rep,) = self.feed(self.answer(bytes.fromhex("2a030033000003e9")))
        d = parse_cmd34(rep)
        self.assertEqual((d["sub"], d["seq"], d["entry"]["id"]), (2, 0x33, 1001))
        self.assertIn("talk to the Overseer", d["entry"]["text"])
        self.assertEqual(self.ui.msgs[-1], (0x22, 0x33))              # completes the wait
        # guide again: reminder only
        reps = self.talk()
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0d05"])
        # target the overseer (npc.py answers 25 01 from ctx["npcs"]) and talk: complete
        (tgt,) = self.target(0xFEA00002)
        self.assertEqual(cm.parse_cmd25(tgt)["res"], 0)
        reps = self.talk()
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0d05", "3403", "0d06", "0d06"])
        self.assertTrue(cm.parse_notify_chat(reps[0])["msg"].startswith("Overseer: "))
        self.assertEqual(self.ui.titles(), [])
        saved = chars.CharStore(self.path).find(self.UID, 1)["quests"]["1001"]
        self.assertEqual(saved["state"], "done")
        # detail for a finished id: removed, wait completed
        (rep,) = self.feed(self.answer(bytes.fromhex("2a030034000003e9")))
        self.assertEqual(rep.hex(), "34030034000003e9")
        # overseer afterwards: npc.py's default line; guide afterwards: "after" line
        self.assertEqual([r[:2].hex() for r in self.talk()], ["0d06", "0d05"])
        self.target(0xFEA00001)
        self.assertIn(b"More tasks", self.talk()[0])
        # next login: no list push (quest done)
        self.assertNotIn(0x34, [p[0] for p in self.spawn()])

    def test_active_quest_is_pushed_on_the_next_spawn(self):
        self.spawn()
        self.target(0xFEA00001)
        self.talk()
        pushes = self.feed(self.spawn())
        d = parse_cmd34(pushes[-1])
        self.assertEqual([e["id"] for e in d["entries"]], [1001])

    def test_overseer_before_the_quest_uses_the_default_line(self):
        self.spawn()
        self.target(0xFEA00002)
        reps = self.talk()
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0d05"])
        self.assertIsNone(quests.qstate(self.ctx["char"], quests.TUTORIAL))

    def test_other_frames_are_not_ours(self):
        self.assertIsNone(quests.answer(bytes.fromhex("2a050026") + vce.put_u32(20003), self.ctx))
        self.assertIsNone(quests.answer(bytes.fromhex("25010031") + vce.put_u32(1), self.ctx))
        self.assertIsNone(quests.answer(bytes.fromhex("28010040"), self.ctx))   # no target
        os.environ["NOBU_QUESTS"] = "0"
        self.assertIsNone(quests.answer(bytes.fromhex("2a030033000003e9"), self.ctx))
        self.assertEqual(quests.world_ready(self.ctx), [])


class GuideOnly(Base):
    """NOBU_QUEST_OVERSEER=0: the objective becomes 'talk to the Guide again'."""

    def setUp(self):
        super().setUp()
        os.environ["NOBU_QUEST_OVERSEER"] = "0"

    def test_complete_on_the_giver(self):
        pushes = self.spawn()
        self.assertEqual([p[:2].hex() for p in pushes][-3:], ["0b01", "0b0a", "1717"])   # guide only + registry
        self.target(0xFEA00001)
        self.talk()
        self.assertIn("Guide again", self.ui.slots[0]["text"])
        reps = self.talk()
        self.assertEqual([r[:2].hex() for r in reps], ["0d06", "0d05", "3403", "0d06", "0d06"])
        self.assertEqual(quests.qstate(self.ctx["char"], quests.TUTORIAL), "done")


class NoDebugNpc(Base):
    DEBUG_NPC = "0"

    def test_spawn_unchanged(self):
        pushes = self.spawn()
        self.assertEqual([p[:2].hex() for p in pushes], ["0b01", "0b0a", "0502", "0b01"])


if __name__ == "__main__":
    unittest.main()
