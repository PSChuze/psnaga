"""Meeting Hall jobs, merit and rank (nobu/guild.py): python -m unittest nobu.test_guild -v

Everything the client sends goes through world.answer (guild.answer runs before quests.answer; guild.world_ready
after quests.world_ready on 23 5a). The battle test plays a real won battle against Nagoya's test Rat."""
import csv
import json
import os
import re
import tempfile
import unittest

from nobu import battle, chars, clientmodel as cm, enemies, guild, items, progression, quests, social, towns, vce, world
from nobu.test_quests import ClientList, parse_cmd34

UID = 0x4000471
LOOKS = [4, 20, 0, 6, 6, 3, 5, 5, 100, 50, 1, 6, 6, 8, 3, 3, 3, 1, 2, 2]      # male Smith, Oda
KNOBS = ("NOBU_DATA", "NOBU_GUILD", "NOBU_GUILD_FILE", "NOBU_GUILD_CID", "NOBU_GUILD_ANY_ZONE", "NOBU_GUILD_CHAT",
         "NOBU_GUILD_DEBUG", "NOBU_DEBUG_NPC", "NOBU_DEBUG_ENEMY", "NOBU_SHOPS", "NOBU_QUESTS", "NOBU_ZONE",
         "NOBU_FORCE_HOME", "NOBU_START_TOWN", "NOBU_SPAWN", "NOBU_BATTLE_FIGHT", "NOBU_ENEMIES", "NOBU_ENEMY_DROPS",
         "NOBU_BATTLE_ONE_HIT", "NOBU_PROG_MERIT_PUSH", "NOBU_PROG_FAME_PUSH", "NOBU_PROG_PUSH", "NOBU_SHOP_ITEM_PUSH",
         "NOBU_PLAYERS", "NOBU_TALK_RANGE", "NOBU_TOWN_SPOTS")
MASTER = 0xFEA00004


def say(text, seq=0x31):
    return bytes([0x23, 0x08]) + vce.put_u16(seq) + vce.put_bytes(text.encode("utf-8"))


def detail(qid, seq=0x77):
    return bytes([0x2A, 0x03]) + vce.put_u16(seq) + vce.put_u32(qid)


def model_2f(p):
    """0x284fd0 + the 0C / 0E archives -> the P fields they write (0x207ef0 / 0x2095e0)."""
    r = vce.Reader(p, 1)
    sub, _ = r.u8(), r.u16()
    if sub == 0x0C:
        fac, rank, fame, _x = r.u8(), r.u8(), r.u16(), r.u16()
        return {0x30: fac, 0x102c6: rank, 0x102c2: fame}
    if sub == 0x0E:
        rank, b, merit = r.u8(), r.u8(), r.u16()
        return {0x102c6: rank, 0x102c7: b, 0x102d4: merit}
    if sub == 0x0D:
        return {0x2f: r.u8()}
    if sub == 0x07:                                   # 0x209280: stats / Life / Ki (lifecycle.py growth)
        return {}
    raise AssertionError(sub)


def texts(frames, sub=6):
    """0D 06 system lines (0D 09 NPC lines with sub=9)."""
    out = []
    for f in frames:
        if f[:2] == bytes([0x0D, sub]):
            r = vce.Reader(f, 2)
            if sub == 9:
                r.u32(), r.u32()
            out.append(r.bytes().decode("utf-8"))
    return out


def wdmmsg_group(g, col=1):
    """{index: English (col 1) or the disc's Japanese (col 0)} of WDMMSG group 0.g."""
    return {int(k.split(".")[2]): v[col] for k, v in items.tsv_names("wdmmsg.tsv").items()
            if k.startswith(f"0.{g}.")}


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k in KNOBS}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOBU_DATA"] = self.tmp.name
        os.environ["NOBU_TOWN_SPOTS"] = "0"           # talk from the spawn point: NPCs on their offsets
        os.environ["NOBU_SHOPS"] = "0"
        self.sessions = dict(social.SESSIONS)
        social.SESSIONS.clear()
        guild.reset()
        enemies.reset()
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        self.ctx = self.login(create=True)
        self.ui = ClientList()
        self.spawned = self.feed(self.answer(bytes.fromhex("235a0013")))

    def tearDown(self):
        world.STORE = None
        social.SESSIONS.clear()
        social.SESSIONS.update(self.sessions)
        guild.reset()
        enemies.reset()
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def login(self, create=False):
        ctx = {"uid": UID}
        if create:
            body = chars.write_create(dict(family="織田", given="信長", looks=LOOKS))
            world.answer(bytes([5, 1]) + vce.put_u32(UID) + vce.put_u16(1) + body, ctx)
        world.answer(bytes([5, 2]) + vce.put_u32(UID) + vce.put_u32(1), ctx)
        world.answer(b"\x04", ctx)
        return ctx

    @property
    def c(self):
        return self.ctx["char"]

    def saved_char(self):
        return chars.CharStore(world.STORE.path).find(UID, self.c["cid"])

    def answer(self, payload):
        return world.answer(payload, self.ctx)

    def feed(self, frames):
        for f in frames:
            if f[0] == 0x34:
                self.ui.apply(f)
        return frames

    def talk(self, cid=MASTER, seq=0x40):
        self.answer(bytes.fromhex("25010031") + vce.put_u32(cid))
        return self.feed(self.answer(bytes.fromhex("2801") + vce.put_u16(seq)))

    def chat(self, text):
        return self.feed(self.answer(say(text)))

    def give(self, item_id, *counts):
        inv = items.inventory(self.c, world.store)
        for n in counts:
            used = {i["idx"] for i in inv["items"]}
            idx = next(i for i in range(items.CAPACITY) if i not in used)
            inv["items"].append(dict(id=item_id, type=54, cat=0, count=n, flags=items.FLAG_STACK, dur=0,
                                     dur_max=0, idx=idx))
        items.save(world.store)

    def money(self):
        return int(items.inventory(self.c)["money"])


# ---------------------------------------------------------------------------- data
class Data(Base):
    def test_seed_written_on_first_use_and_consistent_with_the_enemy_data(self):
        path = os.path.join(self.tmp.name, "guild.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["seed_version"], guild.GUILD_SEED["seed_version"])
        table = guild.jobs()
        self.assertGreaterEqual(len(table), 6)
        edata = enemies.seed()["enemies"]
        drops = {d["id"] for e in edata.values() for d in e["drops"]}
        names = items.names()
        kinds = {j["kind"] for j in table.values()}
        self.assertEqual(kinds, {"kill", "bring"})
        for qid, j in table.items():
            self.assertTrue(guild.QID_MIN <= qid <= guild.QID_MAX)
            self.assertNotIn(qid, quests.QUESTS)
            for k in ("money", "exp", "merit"):
                self.assertGreater(j["reward"][k], 0, qid)
            if j["kind"] == "kill":
                field = towns.TOWNS[j["zone"]]["province"]            # Nagoya's jobs: 10 Owari (towns.py)
                for e in j["enemies"]:
                    self.assertIn(e, edata, qid)                      # real enemy kinds of the province's table
                    self.assertTrue(any(s["enemy"] == e and s["zone"] == field for s in enemies.seed()["spawns"]),
                                    (qid, e))                         # they spawn on the town's own field
            else:
                self.assertIn(j["item"], drops, qid)                  # a real drop (STRDAT id)
                if "target_jp" in j:                                  # generated jobs: the disc's name
                    self.assertEqual(names[j["item"]][0], j["target_jp"], qid)
                self.assertEqual(guild._plural_item(names[j["item"]][1]), j["target"], qid)  # English STRDAT name
        used = {e for j in table.values() if j["kind"] == "kill" for e in j["enemies"]}
        for want in ("rat", "snake", "nezu_bat", "maneater_wolf", "petty_thief"):
            self.assertIn(want, used)

    def test_player_text_is_english(self):
        jp = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
        for qid, j in guild.jobs().items():
            for k in ("title", "target", "text"):
                self.assertIsNone(jp.search(j.get(k, "")), (qid, k, j.get(k)))
            self.assertIsNone(jp.search(guild.job_text(self.c, j)), qid)
        for m in guild.masters():
            self.assertIsNone(jp.search(m.get("greeting", "") + m.get("family", "")), m)

    def test_old_seed_file_is_replaced(self):
        path = os.path.join(self.tmp.name, "guild.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        d["seed_version"] = 2
        d["jobs"][1]["title"] = "Bring tails"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        guild.reset()
        self.assertEqual(guild.load_data()["seed_version"], guild.GUILD_SEED["seed_version"])
        self.assertEqual(guild.jobs()[2002]["title"], "Bring rat tails")
        self.assertTrue(os.path.exists(path + ".v2.bak"))

    def test_entries_fit_the_client_fields(self):
        for j in guild.jobs().values():
            e = guild.job_entry(self.c, j)
            a = cm.Arc(bytes([0x34, 2, 0, 0]) + e, 4)
            self.assertEqual(a.u32(), j["id"])
            self.assertTrue(a.str(0x41))
            self.assertTrue(a.str(0xc1).startswith("[0/"))       # progress survives the clip

    def test_custom_file_is_used_and_bad_file_falls_back(self):
        path = os.path.join(self.tmp.name, "guild.json")
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        d["custom"] = True
        d["jobs"] = d["jobs"][:1]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        guild.reset()
        self.assertEqual(list(guild.jobs()), [2001])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        guild.reset()
        self.assertGreaterEqual(len(guild.jobs()), 6)


# ---------------------------------------------------------------------------- NPC and job window
class Master(Base):
    def test_spawned_in_nagoya_with_a_free_cid_and_own_look(self):
        recs = [r for p in self.spawned if p[:2] == b"\x0b\x0a" for r in cm.parse_notify_char(p)["records"]]
        m = next(r for r in recs if r["cid"] == MASTER)
        self.assertEqual(m["uid"], 0)
        self.assertNotIn(MASTER, (0xFEA00001, 0xFEA00002, 0xFEA00003))
        n = self.ctx["npcs"][MASTER]
        self.assertEqual((n["role"], n["family"]), ("guild", "Hall Master"))
        others = [x for c, x in self.ctx["npcs"].items() if c != MASTER]
        self.assertTrue(all(x["looks"][5:9] != n["looks"][5:9] for x in others))
        self.assertIn(n["looks"][3], range(2, 256))                    # job byte makes a person (npc_job)

    def test_not_spawned_elsewhere_or_when_off(self):
        self.assertEqual(guild.world_ready(dict(self.ctx, zone=10, npcs={})), [])
        os.environ["NOBU_GUILD"] = "0"
        self.assertNotIn(MASTER, {r["cid"] for p in self.answer(bytes.fromhex("235a0014")) if p[:2] == b"\x0b\x0a"
                                  for r in cm.parse_notify_char(p)["records"]})
        self.assertIsNone(guild.answer(say("jobs"), self.ctx))

    def test_talk_offers_the_first_job_through_the_quest_window(self):
        out = self.talk()
        self.assertEqual([f[:2].hex() for f in out[:2]], ["0d06", "0d05"])
        self.assertTrue(cm.parse_notify_chat(out[0])["msg"].startswith("Hall Master: "))   # named system line
        self.assertEqual(self.ui.titles(), ["[Guild] Cull the rats"])
        self.assertIn("New job: Cull the rats. Reward: 150 mon, 40 EXP, 15 merit.", texts(out))
        self.assertEqual(self.saved_char()["guild"]["job"], 2001)
        again = self.talk()                                             # not done: reminder, no new entry
        self.assertNotIn(0x34, [f[0] for f in again])
        self.assertIn("0/5", texts(again)[0])

    def test_detail_request_is_answered_with_the_same_seq(self):
        self.talk()
        out = self.feed(self.answer(detail(2001, seq=0x1234)))
        d = parse_cmd34(out[0])
        self.assertEqual((d["sub"], d["seq"], d["entry"]["id"]), (2, 0x1234, 2001))
        self.assertIn((0x22, 0x1234), self.ui.msgs)                     # completes the 0x1d6810 wait
        self.assertTrue(d["entry"]["text"].startswith("[0/5 defeated]"))
        out = self.answer(detail(2002, seq=0x55))                       # not the active job
        self.assertEqual(parse_cmd34(out[0])["sub"], 3)
        self.assertIn("GUILD job detail request seq=4660 id=2001", world.describe(detail(2001, 0x1234)))
        # tutorial ids still go to quests.py
        self.assertEqual(parse_cmd34(self.answer(detail(1001, seq=9))[0])["sub"], 3)

    def test_active_job_comes_back_after_a_relog(self):
        self.talk()
        guild.on_kill(self.ctx, "rat", world.store)
        self.ctx = self.login()
        self.ui = ClientList()
        self.feed(self.answer(bytes.fromhex("235a0015")))
        self.assertEqual(self.ui.titles(), ["[Guild] Cull the rats"])
        self.assertTrue(self.ui.slots[0]["text"].startswith("[1/5 defeated]"))


# ---------------------------------------------------------------------------- kill jobs
class KillJob(Base):
    def test_kills_count_then_turn_in_pays(self):
        self.talk()
        before, merit0 = self.money(), int(self.c.get("merit", 0))
        self.assertEqual(guild.on_kill(self.ctx, "snake", world.store), [])         # wrong kind
        for k in range(1, 6):
            out = self.feed(guild.on_kill(self.ctx, "rat", world.store))
            self.assertEqual(parse_cmd34(out[0])["sub"], 4)                          # 34 04 in place
            self.assertTrue(self.ui.slots[0]["text"].startswith(f"[{k}/5 defeated]"))
            self.assertIn(f"Cull the rats: {k}/5 Rats defeated.", texts(out)[0])
        self.assertIn("Report to the Hall Master", texts(out)[0])
        self.assertEqual(guild.on_kill(self.ctx, "rat", world.store), [])           # capped
        out = self.talk()
        self.assertEqual(self.ui.titles(), [])                                       # 34 03 removed it
        self.assertIn("Job complete: Cull the rats. Reward: 150 mon, 40 EXP, 15 merit.", texts(out))
        money = next(f for f in out if f[:2] == b"\x30\x06")
        self.assertEqual(vce.Reader(money, 4).u32(), before + 150)
        m = {}
        for f in out:
            if f[:1] == b"\x2f":
                m.update(model_2f(f))
        self.assertEqual((m[0x102d4], m[0x102c6]), (merit0 + 15, 0))                  # merit row; rank Mononofu
        s = self.saved_char()
        self.assertEqual((s["merit"], s["exp"], s["guild"]["job"], s["guild"]["done"]["2001"]), (15, 40, None, 1))
        self.assertEqual(self.money(), before + 150)
        nxt = self.talk()                                                            # rotation: next job
        self.assertEqual(self.ui.titles(), ["[Guild] Bring rat tails"])
        self.assertIn(0x34, [f[0] for f in nxt])

    def test_a_won_battle_counts_once_with_or_without_the_battle_patch(self):
        os.environ["NOBU_BATTLE_FIGHT"] = "1"
        os.environ["NOBU_ENEMY_DROPS"] = "0"
        self.feed(self.answer(bytes.fromhex("235a0016")))                              # enemies now spawned
        self.talk()
        rat = next(c for c, e in self.ctx["enemies"].items() if e.get("enemy") == "rat")
        _, x, y, z, _ = self.ctx["enemies"][rat]["pos"]
        self.answer(chars.encode_move(x + 120, y, z, 90, 0, area=chars.area_for(self.ctx["zone"])))  # walk up
        start = self.answer(bytes.fromhex("28010041"))                                 # ○: battle starts
        self.assertEqual(self.ctx["target"], rat)
        self.assertIn(0x0F, [f[0] for f in start])
        cmd = bytes([0x22, 0x01]) + vce.put_u32(rat) + bytes(12) + vce.put_u16(1) + bytes(2)
        os.environ["NOBU_BATTLE_ONE_HIT"] = "1"
        battle.fight_answer(cmd, self.ctx, world.store)
        fin = self.feed(battle.finish(self.ctx))
        self.assertIn(("defeated", rat, "rat"), [e[:3] for e in self.ctx["enemies_log"]])
        with open(battle.__file__, encoding="utf-8") as fh:
            patched = "guild.on_kill" in fh.read()
        if patched:
            self.assertIn("Cull the rats: 1/5 Rats defeated.", texts(fin))
        back = self.feed(self.answer(bytes.fromhex("235a0017")))                       # back in the field
        if not patched:                                                                # counted from enemies_log
            self.assertIn("Cull the rats: 1/5 Rats defeated.", texts(back))
        self.assertEqual(self.c["guild"]["kills"], 1)
        self.assertTrue(self.ui.slots[0]["text"].startswith("[1/5 defeated]"))
        self.assertEqual(guild.sync_log(self.ctx), [])                                 # never twice

    def test_on_kill_takes_a_field_enemy_cid(self):
        os.environ["NOBU_BATTLE_FIGHT"] = "1"
        self.answer(bytes.fromhex("235a0016"))
        self.talk()
        rat = next(c for c, e in self.ctx["enemies"].items() if e.get("enemy") == "rat")
        self.assertTrue(guild.on_kill(self.ctx, rat, world.store))
        self.assertEqual(guild.on_kill(self.ctx, 0xFEB00001, world.store), [])        # debug Bandit: no kind
        self.assertEqual(self.c["guild"]["kills"], 1)


# ---------------------------------------------------------------------------- bring jobs
class BringJob(Base):
    def test_items_are_checked_and_taken(self):
        out = self.chat("job 2")
        self.assertEqual(self.ui.titles(), ["[Guild] Bring rat tails"])
        self.assertIn("New job: Bring rat tails. Reward: 200 mon, 30 EXP, 20 merit.", texts(out))
        self.give(601, 3)
        out = self.talk()                                                              # 3 of 5: reminder
        self.assertIn("3/5", texts(out)[0])
        self.assertEqual(self.ui.titles(), ["[Guild] Bring rat tails"])
        self.give(601, 4)                                                              # 7 in two stacks
        idx = sorted(i["idx"] for i in items.inventory(self.c)["items"] if i["id"] == 601)
        out = self.talk()
        self.assertEqual(self.ui.titles(), [])
        counts = [f for f in out if f[:2] == b"\x30\x04"]
        self.assertEqual(len(counts), 1)
        r = vce.Reader(counts[0], 4)
        pairs = {r.u8(): r.u8() for _ in range(r.u8())}
        self.assertEqual(pairs, {idx[0]: 0, idx[1]: 2})                                # smaller stack first
        left = [i for i in items.inventory(self.c)["items"] if i["id"] == 601]
        self.assertEqual([i["count"] for i in left], [2])
        s = self.saved_char()
        self.assertEqual((s["merit"], sum(i["count"] for i in s["inventory"]["items"] if i["id"] == 601)), (20, 2))

    def test_worn_items_do_not_count(self):
        self.chat("job 2")
        self.give(601, 5)
        items.inventory(self.c)["items"][-1]["pos"] = 0
        self.assertEqual(guild.item_count(self.c, 601), 0)


# ---------------------------------------------------------------------------- chat
class Chat(Base):
    def test_jobs_list_cancel_and_level_gate(self):
        out = self.chat("jobs")
        lines = texts(out)
        self.assertTrue(lines[0].startswith("1. Cull the rats: kill 5"))
        self.assertIn("(from Lv 7)", lines[7])
        self.assertEqual(texts(self.chat("job 8")), ["That job needs level 7."])
        self.chat("job 1")
        self.assertIn("already have a job", texts(self.chat("job 3"))[0])
        out = self.chat("job cancel")
        self.assertEqual(self.ui.titles(), [])
        self.assertEqual(texts(out), ["Job given up: Cull the rats."])
        self.assertIsNone(self.saved_char()["guild"]["job"])
        self.assertEqual(texts(self.chat("job 2002"))[0].split(".")[0], "New job: Bring rat tails")
        self.assertIsNone(guild.answer(say("hello"), self.ctx))

    def test_chat_off(self):
        os.environ["NOBU_GUILD_CHAT"] = "0"
        self.assertIsNone(guild.answer(say("jobs"), self.ctx))


# ---------------------------------------------------------------------------- merit and rank
class Rank(Base):
    def status(self, frames):
        m = {}
        for f in frames:
            if f[:1] == b"\x2f":
                m.update(model_2f(f))
        return m

    def test_rank_names_are_wdmmsg_0_150_and_0_151(self):
        g150, g151 = wdmmsg_group(150, 0), wdmmsg_group(151, 0)
        self.assertEqual(progression.rank_name(2, faction=6, jp=True), g150.get(2, ""))
        self.assertEqual(progression.rank_name(2, faction=0, jp=True), g151.get(2, ""))
        self.assertEqual(progression.rank_name(13, jp=True), "")
        self.assertEqual(len(progression.RANK_NAMES), 11)
        self.assertEqual(len(progression.RONIN_NAMES), 11)
        self.assertEqual(progression.rank_name(2, faction=6), "Ashigaru Captain")      # server text: English
        self.assertEqual(progression.rank_name(2, faction=0), "Rogue")
        self.assertEqual(progression.rank_name(13), "Unknown")
        self.assertEqual(progression.rank_label(2, 6), "Ashigaru Captain")

    def test_ladder(self):
        c = dict(level=4, merit=1000)
        self.assertEqual(progression.rank_for(c), 0)                    # Ashigaru needs Lv 5 first
        c["level"] = 5
        self.assertEqual(progression.update_rank(c), [1, 2, 3])
        c["merit"] = 1799
        self.assertEqual(progression.update_rank(c), [])
        c["merit"] = 1800
        self.assertEqual(progression.update_rank(c), [-4])              # exam note, once
        self.assertEqual((progression.update_rank(c), c["rank"]), ([], 3))
        c["merit"] = 60000
        self.assertEqual(progression.rank_for(c), 3)                    # no exams: stops at Ashigaru Gen.
        c["rank"] = 5                                                   # an exam rank is kept
        self.assertEqual(progression.rank_for(dict(c, merit=0)), 5)
        self.assertEqual(progression.add_merit(c, 10 ** 6), 0xFFFF)

    def test_level_5_makes_ashigaru_and_merit_promotes(self):
        out = self.answer(say("exp 800"))                               # debug EXP (progression.py): Lv 5
        self.assertEqual(self.c["level"], 5)
        self.assertIn("Promotion! Your rank is now Ashigaru.", texts(out))
        self.assertEqual(self.status(out)[0x102c6], 1)
        out = self.chat("merit 349")
        self.assertEqual(self.status(out)[0x102d4], 349)
        self.assertFalse([t for t in texts(out) if t.startswith("Promotion")])
        out = self.chat("merit 1")
        st = self.status(out)
        self.assertEqual((st[0x102c6], st[0x102d4], st[0x30]), (2, 350, 6))    # Oda: 0.150[2] Ashigaru Capt
        self.assertIn("Promotion! Your rank is now Ashigaru Captain.", texts(out))
        self.assertIn("Next: Ashigaru General at 900 merit.", texts(out)[-1])
        out = self.chat("merit 1450")
        self.assertEqual(self.status(out)[0x102c6], 3)
        self.assertTrue(any("Yoriki promotion exam" in t for t in texts(out)))
        s = self.saved_char()
        self.assertEqual((s["rank"], s["merit"]), (3, 1800))
        # relog: the Status window gets rank and merit again
        self.ctx = self.login()
        st = self.status(self.answer(bytes.fromhex("235a0018")))
        self.assertEqual((st[0x102c6], st[0x102d4]), (3, 1800))

    def test_job_merit_reaches_captain(self):
        self.answer(say("exp 800"))
        self.c["merit"] = 340
        self.talk()
        for _ in range(5):
            guild.on_kill(self.ctx, "rat", world.store)
        out = self.talk()
        self.assertIn("Promotion! Your rank is now Ashigaru Captain.", texts(out))
        self.assertEqual(self.status(out)[0x102c6], 2)

    def test_merit_push_can_be_turned_off(self):
        os.environ["NOBU_PROG_MERIT_PUSH"] = "0"
        self.assertNotIn(b"\x2f\x0e", [f[:2] for f in self.chat("merit")])


if __name__ == "__main__":
    unittest.main()
