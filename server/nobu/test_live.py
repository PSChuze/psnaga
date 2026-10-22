"""The boards' snapshot (nobu/live.py): python -m unittest nobu.test_live -v"""
import json
import os

from nobu import chars, live, social, war
from nobu.test_war import ODA, WarTestBase


def store_of(*cs):
    st = chars.CharStore(None)
    st.data = {"next_cid": 99, "chars": {"7": list(cs)}}
    return st


class LiveTest(WarTestBase):
    def test_empty_world(self):
        snap = live.snapshot()
        self.assertEqual((snap["v"], snap["game"], snap["online"], snap["players"]),
                         (1, "nobunaga", 0, []))
        self.assertEqual([c["name"] for c in snap["clans"]][:2], ["Uesugi", "Takeda"])
        self.assertEqual(len(snap["clans"]), war.FACTIONS - 1)
        self.assertEqual(len(snap["provinces"]), war.PROVINCES - 1)
        json.dumps(snap)

    def test_players_and_clan_counts(self):
        social.register("a", {"char": dict(ODA, level=12, rank=2)})
        social.register("b", {"char": None})                     # connected, no character loaded yet
        snap = live.snapshot()
        self.assertEqual(snap["online"], 1)
        self.assertEqual(snap["players"], [{"name": "織田太郎", "clan": "Oda", "level": 12,
                                            "rank": "Ashigaru Captain"}])
        oda = next(c for c in snap["clans"] if c["name"] == "Oda")
        self.assertEqual(oda["online"], 1)
        self.assertEqual(oda["provinces"], len(war.state().provinces_of(6)))

    def test_war_round(self):
        # the base fixture freezes the war clock on Saturday inside round 1
        st = war.state()
        st.week.setdefault("score", [0, 0])[:] = [1200, 300]
        w = live.snapshot()["war"]
        self.assertEqual((w["phase"], w["round"]), ("round", 0))
        self.assertEqual((w["attacker"], w["defender"]), ("Oda", "Tokugawa"))
        self.assertEqual(w["field"], "Okehazama")
        self.assertEqual(w["score"], [1200, 300])
        self.assertIsNone(w["next_in"])                          # frozen clock: no countdown

    def test_last_war(self):
        war.state().last_war = dict(area=88, attacker=6, defender=5, winner=6, wins=[2, 1, 3],
                                    transfer=9)
        last = live.snapshot()["war"]["last"]
        self.assertEqual(last, {"attacker": "Oda", "defender": "Tokugawa", "winner": "Oda",
                                "field": "Okehazama", "wins": [2, 1, 3], "transfer": "Mikawa"})

    def test_write(self):
        os.environ["NOBU_DATA"] = self.tmp.name
        dest = live.path()
        self.assertEqual(dest, os.path.join(self.tmp.name, "board", "live.json"))
        live.write(dest)                                         # creates board/
        with open(dest, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["game"], "nobunaga")

    def test_standings(self):
        a = dict(ODA, level=20, exp=5000, merit=900, fame=40, rank=3)
        b = dict(ODA, cid=6, given="次郎", level=20, exp=4000, merit=900, fame=0)
        c = dict(ODA, cid=8, given="三郎", level=3, looks=[1, 1, 0, 6, 2])     # Takeda, no merit
        war.state().deeds = {"6": 30}
        snap = live.snapshot(store_of(a, b, c))
        st = snap["standings"]
        self.assertEqual([(r["place"], r["name"]) for r in st["level"]],
                         [(1, "織田太郎"), (2, "織田次郎"), (3, "織田三郎")])
        self.assertEqual([r["place"] for r in st["merit"]], [1, 1])            # a tie shares a place
        self.assertEqual([r["name"] for r in st["fame"]], ["織田太郎"])         # 0 fame left out
        self.assertEqual([(r["name"], r["value"]) for r in st["deeds"]], [("織田次郎", 30)])
        self.assertEqual(st["level"][0]["rank"], "Ashigaru General")
        self.assertEqual(snap["totals"], {"characters": 3})
        members = {c["name"]: c["members"] for c in snap["clans"]}
        self.assertEqual((members["Oda"], members["Takeda"]), (2, 1))

    def test_no_secrets(self):
        a = dict(ODA, level=2, password="hunter2", uid=7)
        text = json.dumps(live.snapshot(store_of(a)), ensure_ascii=False)
        self.assertNotIn("hunter2", text)
        self.assertNotIn('"uid"', text)
        self.assertNotIn('"cid"', text)

    def test_history(self):
        war.state().history = [dict(area=88, attacker=6, defender=5, winner=0, wins=[1, 1, 3])]
        self.assertEqual(live.snapshot()["history"][0]["winner"], "")
