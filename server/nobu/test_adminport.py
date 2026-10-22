"""The OpenLobby admin port (nobu/adminport.py): python -m unittest nobu.test_adminport -v

Runs the real world server on 127.0.0.1 (its event loop in a thread, temp data and logs), signs two
modelled clients in (key exchange, CODE_LOGIN, character load, enter world: the client_run steps of
test_world), and drives the admin port over HTTP the way the panel does: the token, the description,
the status, and each action, which must reach the clients as the frames the world already sends.
"""
import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from nobu import adminport, auth, chars, misc, persist, social, vce, vcecrypto, war_week, world
from nobu.test_war import Env, WarTestBase, t_jst

TOKEN = "test-token-0123"
UID_A, UID_B = 0x4000471, 0x4000472
LOOKS = [1, 1, 0, 6, 6]                     # looks[4] = faction: 6 Oda, 5 Tokugawa


class Client:
    """A blocking model of the client, enough to get a character into the world."""

    def __init__(self, port, uid, cid):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.sendall(vcecrypto.client_hello())
        buf = b""
        while True:
            buf += self.sock.recv(4096)
            got = vcecrypto.parse_server_hello(buf)
            if got:
                break
        status, g, p, Y, _used = got
        xmsg, key = vcecrypto.ClientHandshake().answer(g, p, Y)
        self.sock.sendall(xmsg)
        self.bf = vcecrypto.Blowfish(key)
        self.rr, self.fr, self.frames, self.eof = vcecrypto.RecordReader(self.bf), vce.FrameReader(), [], False
        self.send(world.encode_login_request("dave", "secret", uid))
        self.wait(lambda f: f[0] == 1)
        self.send(vce.put_u8(5) + vce.put_u8(2) + vce.put_u32(uid) + vce.put_u32(cid))   # load
        self.wait(lambda f: f[:2] == b"\x05\x02")
        self.send(vce.put_u8(4))                                                          # enter
        self.wait(lambda f: f[:2] == b"\x0b\x02")

    def send(self, payload):
        self.sock.sendall(vcecrypto.seal(self.bf, vce.frame(payload)))

    def wait(self, pred, timeout=5):
        """Read until a frame matches pred (-> it) or the server closes (-> None)."""
        end = time.time() + timeout
        while True:
            for i, f in enumerate(self.frames):
                if pred(f):
                    del self.frames[:i + 1]
                    return f
            if self.eof or time.time() > end:
                return None
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                self.eof = True
                continue
            self.frames += self.fr.feed(self.rr.feed(data))


def report(kind, text):
    """09 01: a Support report as the client sends it (misc.decode_report)."""
    return vce.put_u8(9) + vce.put_u8(1) + vce.put_u32(kind) + vce.put_bytes(text.encode()) + vce.put_bytes(b"LD:\n")


class AdminPortTest(WarTestBase):
    def setUp(self):
        super().setUp()                     # frozen war clock: round 1, Oda attacks Tokugawa
        self.files = Env(NOBU_LOG_DIR=self.tmp.name, NOBU_REPORTS=None, NOBU_REPORT_STATE=None,
                         NOBU_NEWS=None, NOBU_CHAR_DB=None, NOBU_AUTH_DB=None, NOBU_BACKUP_DIR=None,
                         NOBU_BACKUP_KEEP=None, NOBU_SPAWN="0", NOBU_WORLD_PUSH=None, NOBU_NEWS_POPUP=None)
        self.files.__enter__()
        os.environ["NOBU_DATA"] = self.tmp.name             # NOBU_DATA is put back by WarTestBase
        world.LOG_DIR = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "sessions"))
        world.STORE = chars.CharStore(os.path.join(self.tmp.name, "chars.json"))
        world.STORE.data = {"next_cid": 3, "chars": {
            str(UID_A): [dict(cid=1, uid=UID_A, slot=0, family="Alice", given="Carol", looks=LOOKS,
                              pos=[10, 100460, 3455, 95716, 0])],
            str(UID_B): [dict(cid=2, uid=UID_B, slot=0, family="Bob", given="Dave",
                              looks=LOOKS[:4] + [5], pos=[10, 100460, 3455, 95716, 0])]}}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.srv = asyncio.run_coroutine_threadsafe(
            asyncio.start_server(world.session, "127.0.0.1", 0), self.loop).result(5)
        gport = self.srv.sockets[0].getsockname()[1]
        self.a = Client(gport, UID_A, 1)
        self.b = Client(gport, UID_B, 2)
        self.saved = adminport.TOKEN, adminport.BIND, adminport.PORT
        adminport.TOKEN, adminport.BIND, adminport.PORT = TOKEN, "127.0.0.1", 0
        self.port = adminport.build(self.loop, world.store)
        self.url = "http://127.0.0.1:%d" % self.port.start()[1]

    def tearDown(self):
        self.port.stop()
        adminport.TOKEN, adminport.BIND, adminport.PORT = self.saved
        for c in (self.a, self.b):
            c.sock.close()
        # The rest runs whatever the shutdown does: on a busy CI runner it has taken over 10 s, and
        # a tearDown that stopped here left world.STORE, the files and the war clock to every
        # later test, which then failed in the world (house, progression, skills, bank).
        try:
            asyncio.run_coroutine_threadsafe(world.shutdown(self.srv, grace=2), self.loop).result(60)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            world.STORE = None
            self.files.__exit__()
            super().tearDown()

    def call(self, path, body=None, token=TOKEN):
        req = urllib.request.Request(self.url + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Devtool-Token": token, "X-Devtool-Actor": "admin"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}

    def page(self, path, form=None):
        """A tool page as the panel's frame loads it: GET, or a form posted back to it."""
        data = None if form is None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(self.url + path, data=data, method="GET" if form is None else "POST",
                                     headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "admin",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
            return r.read().decode()

    def events(self, ev="admin"):
        with open(os.path.join(self.tmp.name, "events.jsonl"), encoding="utf-8") as fh:
            return [r for r in map(json.loads, fh) if r.get("ev") == ev]

    def file_report(self, client, kind, text):
        client.send(report(kind, text))
        self.assertIsNotNone(client.wait(lambda f: f[:2] == b"\x09\x01"))

    def test_token_is_required(self):
        for path in ("/openlobby/module.json", "/openlobby/status.json"):
            self.assertEqual(self.call(path, token="nope")[0], 403)
        self.assertEqual(self.call("/openlobby/actions/announce", {"text": "x"}, token="")[0], 403)

    def test_description(self):
        st, desc = self.call("/openlobby/module.json")
        self.assertEqual(st, 200)
        self.assertEqual((desc["key"], desc["title"], desc["short"], desc["platform"]),
                         ("nobu", "Nobunaga's Ambition Online", "Nobunaga", "PS2 HDD"))
        self.assertEqual([(a["id"], a["kind"]) for a in desc["actions"]],
                         [("announce", "announce"), ("kick", "kick")])
        self.assertIn("nobu-world-1", desc["services"][0]["containers"])
        self.assertTrue(desc["calendar"])
        self.assertTrue(desc["help"])
        self.assertTrue(desc["player"])
        self.assertEqual([(t["id"], t["path"]) for t in desc["tools"]], [("news", "news"), ("backups", "backups")])

    def lookup(self, links):
        q = urllib.parse.urlencode({"polid": "TEST0001", "handles": "Alice,Bob", "members": "7",
                                    "links": json.dumps(links)})
        return self.call("/openlobby/player.json?" + q)

    def test_player_lookup_by_linked_citizen_id(self):
        store = auth.AccountStore(os.path.join(self.tmp.name, "auth.json"))
        store.add("alice", "s3cret22")
        store.data["accounts"]["alice"]["uid"] = UID_A
        store.add("quiet", "hunter33")
        store.data["accounts"]["quiet"]["uid"] = 0x4000479
        store.save()
        adminport._on_loop(self.loop, lambda: world.STORE.data["chars"].setdefault(str(UID_A), []).append(
            dict(cid=9, uid=UID_A, slot=1, family="Alice", given="Sel", looks=LOOKS,
                 created="2025-01-09T10:00:00", pos=[10, 1, 2, 3, 0])))
        st, doc = self.lookup({"nobu": [{"key": "alice", "name": "alice"}],
                               "othergame": [{"key": "alice", "name": "alice"}]})
        self.assertEqual(st, 200)
        self.assertEqual([(c["name"], c["online"]) for c in doc["characters"]],
                         [("AliceCarol", True), ("AliceSel", False)])
        facts = {f["label"]: f["value"] for f in doc["characters"][1]["facts"]}
        self.assertEqual((facts["Citizen ID"], facts["Level"], facts["Created"]), ("alice", "1", "2025-01-09"))
        self.assertIn("Last seen", facts)
        self.assertIn("Where", {f["label"] for f in doc["characters"][0]["facts"]})
        self.assertTrue(doc["characters"][0]["detail"].startswith("Level 1"))
        text = json.dumps(doc)
        for secret in ("s3cret22", store.data["accounts"]["alice"]["password"], "password", "token"):
            self.assertNotIn(secret, text)
        self.assertNotIn(str(UID_A), text)
        # a known citizen ID without characters is still a player the game knows
        st, doc = self.lookup({"nobu": [{"key": "QUIET", "name": "quiet"}]})
        self.assertEqual(doc, {"characters": [], "note": "Citizen ID quiet has no characters yet."})

    def test_player_lookup_unknown_or_unlinked(self):
        store = auth.AccountStore(os.path.join(self.tmp.name, "auth.json"))
        store.add("alice", "s3cret22")
        store.data["accounts"]["alice"]["uid"] = UID_A
        store.save()
        self.assertEqual(self.lookup({"nobu": [{"key": "nobody", "name": "nobody"}]}), (200, {}))
        self.assertEqual(self.lookup({}), (200, {}))
        # another system's key of the same spelling, and handles that name characters here, match nothing
        self.assertEqual(self.lookup({"othergame": [{"key": "alice", "name": "alice"}]}), (200, {}))
        self.assertEqual(self.call("/openlobby/player.json?polid=TEST0001&handles=AliceCarol"), (200, {}))

    def test_player_picker(self):
        adminport._on_loop(self.loop, lambda: world.STORE.data["chars"].setdefault(str(UID_A), []).append(
            dict(cid=9, uid=UID_A, slot=1, family="Alice", given="Sel", looks=LOOKS)))
        st, doc = self.call("/openlobby/players.json?q=alice&limit=5")
        self.assertEqual(st, 200)
        self.assertEqual([(p["id"], p["online"]) for p in doc["players"]], [("AliceCarol", True), ("AliceSel", False)])
        self.assertTrue(doc["players"][0]["detail"].startswith("Level 1"))
        st, doc = self.call("/openlobby/players.json?q=&limit=1")
        self.assertEqual(len(doc["players"]), 1)
        self.assertTrue(doc["players"][0]["online"])
        self.assertEqual(self.call("/openlobby/players.json?q=dave%20&limit=5")[1]["players"][0]["id"], "BobDave")
        self.assertEqual(self.call("/openlobby/players.json?q=x&limit=0")[0], 400)
        self.assertTrue(self.call("/openlobby/module.json")[1]["players"])

    def test_calendar_over_http(self):
        lo, hi = int(t_jst("2025-01-27T00:00")), int(t_jst("2025-02-03T00:00"))
        q = "/openlobby/calendar.json?from=%d&to=%d" % (lo, hi)
        self.assertEqual(self.call(q), (200, {"events": []}))           # the frozen test clock
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "1"
        os.environ.pop("NOBU_WAR_CLOCK_START")
        st, doc = self.call(q)
        self.assertEqual([e["id"] for e in doc["events"]], ["war-20250127", "war-20250127-declare"])
        self.assertEqual(self.call("/openlobby/calendar.json?from=x&to=1")[0], 400)

    def test_status_is_the_boards_snapshot(self):
        st, doc = self.call("/openlobby/status.json")
        self.assertEqual((st, doc["up"], doc["online"], doc["rooms_label"]), (200, True, 2, "clans"))
        self.assertEqual(sorted((p["name"], p["where"], p["clan"]) for p in doc["players"]),
                         [("AliceCarol", "Owari", "Oda"), ("BobDave", "Owari", "Tokugawa")])
        clans = {r["name"]: r for r in doc["rooms"]}
        self.assertEqual((clans["Oda"]["players"], clans["Oda"]["state"]), (1, "attacking"))
        self.assertEqual((clans["Tokugawa"]["players"], clans["Tokugawa"]["state"]), (1, "defending"))
        self.assertEqual(clans["Uesugi"]["players"], 0)
        self.assertEqual(doc["summary"], "War round 1 of 3, 0 to 0 (Oda attacks Tokugawa at Okehazama)")
        self.assertEqual(doc["facts"][:2], [{"label": "characters", "value": 2},
                                            {"label": "war phase", "value": "round"}])

    def test_announce_reaches_everyone_as_a_system_line(self):
        st, out = self.call("/openlobby/actions/announce", {"text": "  Maintenance at  10:00 UTC "})
        self.assertEqual(out, {"ok": True, "msg": "Sent to 2 players."})
        line = misc.notify_system("Maintenance at 10:00 UTC")
        self.assertEqual(line[:2], b"\x0d\x06")
        for c in (self.a, self.b):
            self.assertEqual(c.wait(lambda f: f[:2] == b"\x0d\x06"), line)
        self.assertEqual(self.call("/openlobby/actions/announce", {"text": " "})[1],
                         {"ok": False, "msg": "Nothing to announce."})

    def test_kick_closes_only_that_session(self):
        st, out = self.call("/openlobby/actions/kick", {"name": "alice carol", "reason": "Server restart"})
        self.assertEqual(out, {"ok": True, "msg": "Disconnected AliceCarol."})
        self.assertEqual(self.a.wait(lambda f: f[:2] == b"\x0d\x06"), misc.notify_system("Server restart"))
        self.assertIsNone(self.a.wait(lambda f: False, timeout=5))
        self.assertTrue(self.a.eof)
        for _ in range(50):                                    # the session's leave path runs
            if self.call("/openlobby/status.json")[1]["online"] == 1:
                break
            time.sleep(0.1)
        doc = self.call("/openlobby/status.json")[1]
        self.assertEqual([p["name"] for p in doc["players"]], ["BobDave"])
        self.b.send(vce.put_u8(3) + vce.put_u32(0xBDF4))       # Bob is still connected: a ping
        self.assertIsNotNone(self.b.wait(lambda f: f[0] == 3))
        self.assertEqual(self.call("/openlobby/actions/kick", {"name": "Alice Carol"})[1],
                         {"ok": False, "msg": "Not online."})

    def test_account_kick_sends_handles(self):
        st, out = self.call("/openlobby/actions/kick", {"polid": "TEST0002", "handles": "Nobody,BobDave"})
        self.assertEqual(out, {"ok": True, "msg": "Disconnected BobDave."})
        self.assertIsNone(self.b.wait(lambda f: False, timeout=5))
        self.assertTrue(self.b.eof)
        self.assertEqual(self.call("/openlobby/actions/kick", {"handles": ""})[1],
                         {"ok": False, "msg": "No character named."})


    # -- help desk ------------------------------------------------------------ #
    def test_help_desk_lists_the_reports(self):
        self.file_report(self.a, 3, "The ferry at Tsushima will not take me")
        self.file_report(self.b, 5, "Please add more shops")
        self.assertEqual(self.call("/openlobby/help.json"), (200, {"threads": [
            {"id": "r2", "subject": "Request: Please add more shops", "who": "Bob Dave", "at": ANY,
             "closed": False, "waiting": True, "count": 1, "category": "Request"},
            {"id": "r1", "subject": "Bug report: The ferry at Tsushima will not take me", "who": "Alice Carol",
             "at": ANY, "closed": False, "waiting": True, "count": 1, "category": "Bug report"}]}))
        st, t = self.call("/openlobby/help/thread.json?id=r1")
        self.assertEqual((st, t["who"], t["closed"]), (200, {"name": "Alice Carol", "online": True}, False))
        self.assertEqual([(m["from"], m["text"]) for m in t["messages"]],
                         [("player", "The ferry at Tsushima will not take me")])
        self.assertEqual(self.call("/openlobby/help/thread.json?id=r9")[0], 404)
        self.assertEqual(self.call("/openlobby/help/reply", {"id": "r9", "text": "hi"})[1],
                         {"ok": False, "msg": "No such report."})

    def test_an_answer_reaches_an_online_player_at_once(self):
        self.file_report(self.a, 3, "Stuck behind the castle gate")
        st, out = self.call("/openlobby/help/reply", {"id": "r1", "text": " Try  the east gate. "})
        self.assertEqual(out, {"ok": True, "msg": "Sent to Alice Carol."})
        self.assertEqual(self.a.wait(lambda f: f[:2] == b"\x0d\x06"), misc.notify_system("[Support] Try the east gate."))
        self.assertEqual(self.a.wait(lambda f: f[:2] == b"\x0d\x05"), misc.notify_message("Try the east gate."))
        t = self.call("/openlobby/help/thread.json?id=r1")[1]
        self.assertEqual([(m["from"], m["text"]) for m in t["messages"]],
                         [("player", "Stuck behind the castle gate"), ("staff", "Try the east gate.")])
        rows = self.call("/openlobby/help.json")[1]["threads"]
        self.assertEqual((rows[0]["waiting"], rows[0]["count"]), (False, 2))
        state = misc.load_support_state()
        self.assertEqual((state["pending"], state["answers"]["r1"][0]["delivered"],
                          state["answers"]["r1"][0]["by"]), ([], True, "admin"))
        self.assertEqual(self.call("/openlobby/help/reply", {"id": "r1", "text": "x" * 400})[1]["ok"], False)
        self.assertEqual(self.call("/openlobby/help/reply", {"id": "r1", "text": "  "})[1],
                         {"ok": False, "msg": "Nothing to send."})

    def test_an_answer_waits_for_an_offline_player_and_is_sent_at_login(self):
        self.file_report(self.b, 2, "A bandit follows me everywhere")
        with open(misc.reports_path(), encoding="utf-8") as fh:
            reports = fh.read()
        self.b.sock.close()
        for _ in range(50):
            if self.call("/openlobby/status.json")[1]["online"] == 1:
                break
            time.sleep(0.1)
        st, out = self.call("/openlobby/help/reply", {"id": "r1", "text": "We removed it.", "close": True})
        self.assertEqual(out, {"ok": True, "msg": "Bob Dave is not online; the answer is kept and shown at "
                                                  "their next login. The report is closed."})
        self.assertEqual(self.call("/openlobby/help/thread.json?id=r1")[1]["who"],
                         {"name": "Bob Dave", "online": False})
        self.assertEqual([p["uid"] for p in misc.load_support_state()["pending"]], [UID_B])
        self.assertEqual(self.call("/openlobby/help.json?open=1")[1], {"threads": []})
        # back in: the answer comes with the world-ready pushes (23 5a), once
        self.b = Client(self.srv.sockets[0].getsockname()[1], UID_B, 2)
        self.b.send(vce.put_u8(0x23) + vce.put_u8(0x5A) + vce.put_u16(7))
        self.assertEqual(self.b.wait(lambda f: f[:2] == b"\x0d\x06"), misc.notify_system("[Support] We removed it."))
        self.assertEqual(self.b.wait(lambda f: f[:2] == b"\x0d\x05"), misc.notify_message("We removed it."))
        state = misc.load_support_state()
        self.assertEqual((state["pending"], state["answers"]["r1"][0]["delivered"]), ([], True))
        self.b.send(vce.put_u8(0x23) + vce.put_u8(0x5A) + vce.put_u16(8))
        self.assertIsNone(self.b.wait(lambda f: f[:2] == b"\x0d\x06", timeout=1))
        with open(misc.reports_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), reports)                # reports.jsonl is never rewritten
        self.assertEqual([e["what"] for e in self.events()], ["answered report r1"])

    def test_close_and_a_gm_chat_line_joins_its_report(self):
        self.file_report(self.a, 2, "Help")
        self.a.send(vce.put_u8(9) + vce.put_u8(2) + vce.put_u32(0) + vce.put_u32(0) + vce.put_bytes(b"Still here"))
        self.assertIsNotNone(self.a.wait(lambda f: f[:2] == b"\x09\x02"))
        t = self.call("/openlobby/help/thread.json?id=r1")[1]
        self.assertEqual([m["text"] for m in t["messages"]], ["Help", "Still here"])
        self.assertEqual(self.call("/openlobby/help/close", {"id": "r1"})[1], {"ok": True, "msg": "Closed."})
        rows = self.call("/openlobby/help.json")[1]["threads"]
        self.assertEqual([(r["id"], r["closed"], r["waiting"]) for r in rows], [("r1", True, False)])
        self.assertEqual(misc.load_support_state()["closed"]["r1"]["by"], "admin")
        self.assertEqual(self.call("/openlobby/help/close", {"id": "r5"})[1]["ok"], False)

    def test_a_broken_state_file_is_not_written_over(self):
        self.file_report(self.a, 3, "x")
        with open(misc.support_state_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(self.call("/openlobby/help/reply", {"id": "r1", "text": "hi"})[0], 500)
        self.assertEqual(self.call("/openlobby/help.json")[0], 500)
        with open(misc.support_state_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")

    # -- tool pages ----------------------------------------------------------- #
    def test_news_editor(self):
        html = self.page("/news")
        self.assertIn(misc.NEWS_SEED[0]["title"], html)
        self.assertNotIn("<script", html)
        html = self.page("/news", {"op": "add", "date": "2030-01-21", "title": " Castle  war tonight ",
                                   "body": "Oda against Tokugawa.\r\nGood luck."})
        self.assertIn("Added.", html)
        self.assertEqual(misc.load_news()[0], {"date": "2030-01-21", "title": "Castle war tonight",
                                               "body": "Oda against Tokugawa.\nGood luck."})
        self.assertIn("The date must be YYYY-MM-DD.", self.page("/news", {"op": "add", "date": "soon", "title": "x"}))
        self.assertIn("look again", self.page("/news", {"op": "save", "i": "0", "was": "Other",
                                                        "date": "2030-01-22", "title": "y"}))
        self.page("/news", {"op": "save", "i": "0", "was": "Castle war tonight", "date": "2030-01-22",
                            "title": "Castle war tomorrow", "body": "<b>Oda</b>"})
        self.assertIn("&lt;b&gt;Oda&lt;/b&gt;", self.page("/news"))
        self.assertEqual(misc.load_news()[0]["title"], "Castle war tomorrow")
        self.page("/news", {"op": "delete", "i": "1", "was": misc.NEWS_SEED[0]["title"]})
        self.assertEqual([i["title"] for i in misc.load_news()], ["Castle war tomorrow"])
        self.assertEqual([e["what"] for e in self.events()],
                         ["added news 'Castle war tonight'", "edited news 'Castle war tomorrow'",
                          "deleted news %r" % misc.NEWS_SEED[0]["title"]])
        self.assertEqual({e["actor"] for e in self.events()}, {"admin"})
        # the players' News menu shows the edit
        self.a.send(vce.put_u8(0x2A) + vce.put_u8(5) + vce.put_u16(1) + vce.put_u32(misc.VERSION))
        self.assertIsNotNone(self.a.wait(lambda f: f == misc.notify_system("[2030-01-22] Castle war tomorrow")))

    def test_news_editor_leaves_a_broken_file_alone(self):
        with open(misc.news_path(), "w", encoding="utf-8") as fh:
            fh.write("[{oops")
        self.assertIn("does not parse", self.page("/news"))
        self.assertIn("does not parse", self.page("/news", {"op": "add", "date": "2030-01-21", "title": "x"}))
        with open(misc.news_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "[{oops")

    def test_backups_page(self):
        auth.AccountStore(auth.default_db()).add("alice", "secret1")
        html = self.page("/backups")
        self.assertIn("Back up now", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("chars.json.", html)
        html = self.page("/backups", {"op": "backup"})
        self.assertIn("Backed up: chars.json.", html)
        chars_path = os.path.join(self.tmp.name, "chars.json")
        self.assertTrue(os.path.exists(chars_path))         # the world's store was written first
        self.assertEqual([t for _p, _s, t in persist.list_backups(chars_path)], ["manual"])
        self.assertEqual([t for _p, _s, t in persist.list_backups(auth.default_db())][0], "manual")
        self.assertIn("<td>manual</td>", html)
        self.assertEqual(self.events()[0]["actor"], "admin")
        html = self.page("/backups", {"op": "validate"})
        self.assertIn("No problems found.", html)
        self.assertIn("2 characters", html)
        with open(chars_path, "w", encoding="utf-8") as fh:
            fh.write("{")
        html = self.page("/backups", {"op": "validate"})
        self.assertIn("1 error found.", html)
        with open(chars_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{")                  # validate never repairs


class _Any:
    def __eq__(self, other):
        return isinstance(other, int) and other > 0


ANY = _Any()


class CalendarTest(WarTestBase):
    def setUp(self):
        super().setUp()
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "1"            # the real clock: war time = real time
        os.environ.pop("NOBU_WAR_CLOCK_START")

    def test_a_month_of_war_weeks(self):
        lo, hi = int(t_jst("2025-01-09T00:00")), int(t_jst("2025-02-09T00:00"))
        got = adminport.war_calendar(lo, hi)
        weeks = [e for e in got if e["title"].startswith("Castle war")]
        self.assertEqual([e["title"] for e in weeks],
                         ["Castle war, week of Jan 6", "Castle war, week of Jan 13",
                          "Castle war, week of Jan 20", "Castle war, week of Jan 27",
                          "Castle war, week of Feb 3"])
        self.assertEqual(weeks[3], {
            "id": "war-20250127", "title": "Castle war, week of Jan 27", "kind": "war",
            "start": int(t_jst("2025-01-27T00:00")), "end": int(t_jst("2025-02-02T23:00")),
            "about": "Build-up from Mon 00:00, war declared Thu 21:00, 3 battle rounds, "
                     "results Sun 23:00 (Japan time).",
            "times": [[int(t_jst("2025-02-01T20:00")), int(t_jst("2025-02-01T22:30"))],
                      [int(t_jst("2025-02-02T14:00")), int(t_jst("2025-02-02T16:30"))],
                      [int(t_jst("2025-02-02T20:00")), int(t_jst("2025-02-02T22:30"))]]})
        decl = [e for e in got if e["title"] == "War declared"]
        self.assertEqual(len(decl), 5)
        self.assertEqual((decl[3]["id"], decl[3]["start"], decl[3]["end"]),
                         ("war-20250127-declare", int(t_jst("2025-01-30T21:00")),
                          int(t_jst("2025-01-30T21:30"))))
        for e in got:                                       # every entry touches the range
            self.assertTrue(e["end"] > lo and e["start"] < hi)

    def test_only_what_touches_the_range(self):
        got = adminport.war_calendar(int(t_jst("2025-02-02T23:00")), int(t_jst("2025-02-03T00:00")))
        self.assertEqual(got, [])                           # after the results, before Monday
        got = adminport.war_calendar(int(t_jst("2025-02-01T00:00")), int(t_jst("2025-02-01T01:00")))
        self.assertEqual([e["id"] for e in got], ["war-20250127"])

    def test_overrides_are_honoured(self):
        os.environ["NOBU_WAR_DECLARE"] = "3-12:00"
        os.environ["NOBU_WAR_ROUNDS"] = "5-21:00+60"
        os.environ["NOBU_WAR_RESULTS"] = "6-09:00"
        lo = int(t_jst("2025-01-27T00:00"))
        week = adminport.war_calendar(lo, lo + 86400)[0]
        self.assertEqual((week["end"], week["times"]),
                         (int(t_jst("2025-02-01T09:00")),
                          [[int(t_jst("2025-01-31T21:00")), int(t_jst("2025-01-31T22:00"))]]))
        self.assertIn("war declared Wed 12:00, 1 battle round, results Sat 09:00", week["about"])

    def test_a_scaled_clock_is_turned_back_into_real_time(self):
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "60"           # a war week in 2.8 real hours
        os.environ["NOBU_WAR_CLOCK_START"] = "2025-01-13T00:00"
        t0 = war_week._T0
        got = adminport.war_calendar(int(t0), int(t0) + 3 * 3600)
        weeks = [e for e in got if e["kind"] == "war" and e["title"].startswith("Castle")]
        self.assertEqual([e["id"] for e in weeks], ["war-20250113", "war-20250120"])
        first = weeks[0]
        self.assertEqual(first["start"], round(t0))
        self.assertEqual(first["times"][0][0], round(t0 + (5 * 86400 + 20 * 3600) / 60))
        self.assertTrue(first["about"].endswith("War clock x60."))

    def test_nothing_with_the_schedule_off_or_a_stopped_clock(self):
        lo, hi = int(t_jst("2025-01-09T00:00")), int(t_jst("2025-02-09T00:00"))
        os.environ["NOBU_WAR_SCHEDULE"] = "0"
        self.assertEqual(adminport.war_calendar(lo, hi), [])
        os.environ["NOBU_WAR_SCHEDULE"] = "1"
        os.environ["NOBU_WAR"] = "0"
        self.assertEqual(adminport.war_calendar(lo, hi), [])
        os.environ["NOBU_WAR"] = "1"
        os.environ["NOBU_WAR_CLOCK_SCALE"] = "0"
        self.assertEqual(adminport.war_calendar(lo, hi), [])


class StartTest(unittest.TestCase):
    def test_off_without_a_port_and_refuses_a_wide_bind_without_a_token(self):
        saved = adminport.TOKEN, adminport.BIND, adminport.PORT
        logged = []
        try:
            adminport.PORT = 0
            self.assertIsNone(adminport.start(None, log=logged.append))
            adminport.TOKEN, adminport.BIND, adminport.PORT = "", "172.17.0.1", 19111
            self.assertIsNone(adminport.start(None, log=logged.append))
        finally:
            adminport.TOKEN, adminport.BIND, adminport.PORT = saved
        self.assertEqual(logged, ["adminport: not started (a port bound to 172.17.0.1 needs a token)"])

    def test_war_summary(self):
        self.assertEqual(adminport.war_summary({"phase": "static"}), "")
        self.assertEqual(adminport.war_summary({"phase": "build", "next_in": 3 * 3600 + 5 * 60}),
                         "War: build, next change in 3h 05m")
        self.assertEqual(adminport.war_summary(
            {"phase": "round", "round": 1, "rounds": 3, "score": [1200, 300], "attacker": "Oda",
             "defender": "Takeda", "field": "Okehazama", "next_in": None}),
            "War round 2 of 3, 1200 to 300 (Oda attacks Takeda at Okehazama)")


if __name__ == "__main__":
    unittest.main()
