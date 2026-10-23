"""setup.py and hostsetup/ without Docker: the settings the answers turn into, the ports players
need, the check against a real connect server, finding the disc, and the docker commands that find
and start the shared DNS and pair with a panel (a fake docker records them)."""
import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))

import setup                                    # noqa: E402
from hostsetup import discs, openlobby          # noqa: E402

CNF = b"BOOT2 = cdrom0:\\SLPM_657.83;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n"


class FakeDocker:
    """Answers docker commands from a table and records them."""

    def __init__(self, running=None, labels=None, fail=(), answers=None):
        self.running = running or {}         # label -> [container names]
        self.labels = labels or {}           # (container, key) -> value
        self.fail = set(fail)                # first words of commands that fail
        self.answers = answers or {}         # first words -> (code, output)
        self.calls = []

    def __call__(self, args, timeout=600, cwd=None):
        self.calls.append(list(args))
        for k, v in self.answers.items():
            if " ".join(args).startswith(k):
                return v
        if args[0] in self.fail or " ".join(args[:2]) in self.fail:
            return 1, "error"
        if args[:2] == ["ps", "--filter"]:
            return 0, "\n".join(self.running.get(args[2][len("label="):], []))
        if args[0] == "inspect":
            key = args[2].split('"')[1]
            return 0, self.labels.get((args[3], key), "<no value>")
        if args[:2] == ["compose", "-f"] and "up" in args:
            self.running["org.openlobby.role=dns"] = ["openlobby-dns-dns-1"]
        return 0, ""


class Settings(unittest.TestCase):
    def test_first_settings(self):
        c = setup.first_settings("192.0.2.10", "Test, Server@home", setup.read_env(setup.ENV_EXAMPLE))
        self.assertEqual(c["NOBU_PUBLIC_IP"], "192.0.2.10")
        self.assertEqual(c["NOBU_SERVER_NAME"], "Test Server home")      # no list separators
        self.assertRegex(c["NOBU_ADMIN_TOKEN"], r"^[0-9a-f]{48}$")
        self.assertNotIn("NOBU_ADMIN_TOKEN", setup.first_settings("192.0.2.10", "x", {"NOBU_ADMIN_TOKEN": "t"}))
        self.assertEqual(setup.world_name(""), "Nobunaga")
        self.assertEqual(len(setup.world_name("x" * 80)), 32)

    def test_ports(self):
        self.assertEqual(setup.ports_to_open({}), [9070, 9443, 9110])
        self.assertEqual(setup.ports_to_open({"NOBU_LOGIN_PORT": "10443", "NOBU_WORLD_PORT": "10110"}),
                         [9070, 10443, 10110])

    def test_who_plays_and_addresses(self):
        self.assertEqual(setup.who_plays({"NOBU_PUBLIC_IP": "10.0.0.5"}, {}), "home")
        self.assertEqual(setup.who_plays({"NOBU_PUBLIC_IP": "203.0.113.7"}, {}), "internet")
        self.assertEqual(setup.who_plays({"NOBU_PUBLIC_IP": "10.0.0.5"}, {"who": "internet"}), "internet")
        self.assertEqual(setup.to_ip("203.0.113.7"), "203.0.113.7")
        self.assertEqual(setup.to_ip("10.1"), "")

    def test_env_file(self):
        d = tempfile.mkdtemp()
        env = os.path.join(d, ".env")
        setup.write_env({"NOBU_PUBLIC_IP": "192.0.2.10", "NOBU_BATTLE_FIGHT": "1", "NEW_ONE": "1"},
                        path=env, template=setup.ENV_EXAMPLE)
        got = setup.read_env(env)
        self.assertEqual((got["NOBU_PUBLIC_IP"], got["NOBU_BATTLE_FIGHT"], got["NEW_ONE"], got["NOBU_AUTH_OPEN"]),
                         ("192.0.2.10", "1", "1", "claim"))
        with open(env, encoding="utf-8") as f:
            self.assertIn("# Required: the address players' consoles reach", f.read())

    def test_news_file(self):
        d = tempfile.mkdtemp()
        setup.write_news({"NOBU_STATE": d}, "Test Server", "Hello.")
        with open(os.path.join(d, "data", "news.json"), encoding="utf-8") as f:
            news = json.load(f)
        self.assertEqual((news[0]["title"], news[0]["body"]), ("Welcome to Test Server", "Hello."))
        self.assertRegex(news[0]["date"], r"^\d{4}-\d\d-\d\d$")

    def test_unpack_keeps_the_hosts_files(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("NOBU_PUBLIC_IP=192.0.2.10\n")
        archive = os.path.join(d, "src.zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("psnaga/compose.yml", "name: nobu\n")
            z.writestr("psnaga/.env", "NOBU_PUBLIC_IP=\n")
            z.writestr("psnaga/state/data/chars.json", "{}")
            z.writestr("psnaga/server/nobu/world.py", "")
        written = setup.unpack_server(archive, d, "v1.0.0")
        self.assertEqual(sorted(written), ["compose.yml", "server/nobu/world.py"])
        self.assertEqual(setup.read_env(os.path.join(d, ".env"))["NOBU_PUBLIC_IP"], "192.0.2.10")

    def test_disc_mount(self):
        d = tempfile.mkdtemp()
        iso = os.path.join(d, "Nobunaga (Japan).iso")
        vols, inside = setup.disc_mount(iso)
        self.assertEqual((vols[0], vols[1].endswith(":/picked/1:ro"), inside),
                         ("-v", True, "/picked/1/Nobunaga (Japan).iso"))
        self.assertEqual(setup.disc_mount(d)[1], "/picked/1")


class Checks(unittest.TestCase):
    """connsv_answers against the real connect server on a local port."""

    def test_connect_server_check(self):
        logs = tempfile.mkdtemp()
        os.makedirs(os.path.join(logs, "sessions"))
        from nobu import connsv
        connsv.LOG_DIR = logs
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        ports = {}

        async def serve():
            srv = await asyncio.start_server(lambda r, w: connsv.session(r, w, 9070), "127.0.0.1", 0)
            ports["c"] = srv.sockets[0].getsockname()[1]
            ready.set()
        threading.Thread(target=lambda: (loop.run_until_complete(serve()), loop.run_forever()), daemon=True).start()
        ready.wait(5)
        try:
            self.assertTrue(setup.connsv_answers(ports["c"]))
            self.assertTrue(setup.port_open(ports["c"]))
        finally:
            loop.call_soon_threadsafe(loop.stop)
        self.assertFalse(setup.connsv_answers(1, timeout=1))


class Discs(unittest.TestCase):
    def folder_disc(self, boot=CNF):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "SYSTEM.CNF"), "wb") as f:
            f.write(boot)
        return d

    def test_a_disc_folder_is_recognised(self):
        d = self.folder_disc()
        self.assertEqual(discs.identify(d), [discs.Disc(d)])
        self.assertEqual(discs.identify(self.folder_disc(b"BOOT2 = cdrom0:\\SLPM_651.97;1\r\n")), [])
        self.assertEqual(discs.pick([discs.Disc("a.zip", "x.iso"), discs.Disc("b.iso")]), discs.Disc("b.iso"))
        self.assertIsNone(discs.pick([]))

    def test_pcsx2_folders(self):
        home = tempfile.mkdtemp()
        ini = os.path.join(home, "Documents", "PCSX2", "inis")
        os.makedirs(ini)
        with open(os.path.join(ini, "PCSX2.ini"), "w") as f:
            f.write("[UI]\nX = 1\n[GameList]\nRecursivePaths = %s\n" % home)
        self.assertEqual(discs.pcsx2_game_folders(home, "win32", {}), [home])


class OpenLobby(unittest.TestCase):
    def test_reuses_a_running_dns(self):
        dock = FakeDocker(running={"org.openlobby.role=dns": ["pol-dns-1"]})
        self.assertEqual(openlobby.find_dns(dock), "pol-dns-1")
        ok, _ = openlobby.dns_names("pol-dns-1", "nobu", setup.DNS_NAMES, dock)
        self.assertTrue(ok)
        self.assertEqual(dock.calls[-1], ["exec", "pol-dns-1", "python", "stub.py", "dns-names", "add", "nobu",
                                          "nobol.koei.co.jp", "testconsv1.ax.koei.co.jp"])
        self.assertTrue(openlobby.dns_remove("pol-dns-1", "nobu", dock)[0])
        self.assertEqual(dock.calls[-1], ["exec", "pol-dns-1", "python", "stub.py", "dns-names", "remove", "nobu"])
        self.assertFalse(any(c[0] == "compose" for c in dock.calls))     # never a second one

    def test_starts_the_dns_when_there_is_none(self):
        dock = FakeDocker()
        d = tempfile.mkdtemp()
        self.assertEqual(openlobby.start_dns(d, "192.0.2.10", dock), "openlobby-dns-dns-1")
        with open(os.path.join(d, "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ("org.openlobby.role: dns", 'POL_ADVERTISE: "192.0.2.10"', '"0.0.0.0:53:53/udp"',
                     'command: ["dns"]', "dns-data:/data", openlobby.DNS_IMAGE):
            self.assertIn(want, text)
        self.assertNotIn("server.yaml", text)

    def test_an_old_dns_image_is_said_plainly(self):
        usage = "usage: stub.py [-h] {run,dns,http} ..."
        dock = FakeDocker(running={"org.openlobby.role=dns": ["pol-dns-1"]},
                          answers={"exec pol-dns-1": (1, usage)})
        ok, why = openlobby.dns_names("pol-dns-1", "nobu", setup.DNS_NAMES, dock)
        self.assertFalse(ok)
        self.assertIn("too old", why)
        self.assertIn("docker pull", why)
        dock = FakeDocker(answers={"exec pol-dns-1": (2, "dns-names: bad name 'x y'")})
        self.assertEqual(openlobby.dns_names("pol-dns-1", "nobu", ["x y"], dock), (False, "dns-names: bad name 'x y'"))

    def test_ensure_dns(self):
        dock = FakeDocker(running={"org.openlobby.role=dns": ["pol-dns-1"]}, fail={"exec"})
        log, out = io.StringIO(), io.StringIO()
        with mock.patch.object(setup, "docker_run", dock), mock.patch("sys.stdout", out):
            self.assertFalse(setup.ensure_dns({"NOBU_PUBLIC_IP": "192.0.2.10"},
                                              setup.proglib.Progress(out=io.StringIO()), log))
            self.assertIn("nobol.koei.co.jp", out.getvalue())             # never fails silently
            dock.fail = set()
            self.assertTrue(setup.ensure_dns({"NOBU_PUBLIC_IP": "192.0.2.10"},
                                             setup.proglib.Progress(out=io.StringIO()), log))

    def test_network_is_made_only_when_missing(self):
        dock = FakeDocker(fail={"network inspect"})
        self.assertTrue(openlobby.ensure_network(dock))
        self.assertEqual(dock.calls, [["network", "inspect", "openlobby"], ["network", "create", "openlobby"]])
        dock = FakeDocker()
        self.assertTrue(openlobby.ensure_network(dock))
        self.assertEqual(dock.calls, [["network", "inspect", "openlobby"]])

    def test_panel_and_pairing(self):
        dock = FakeDocker(running={"org.openlobby.role=panel": ["openlobby-web-1"]},
                          labels={("openlobby-web-1", "org.openlobby.panel.url"): "http://127.0.0.1:8090/"})
        self.assertEqual(openlobby.find_panel(dock), "http://127.0.0.1:8090")
        sent = []

        class Answer(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=0):
            sent.append((req.full_url, json.loads(req.data)))
            return Answer(json.dumps({"ok": True, "title": "x", "pairing": "secret-1"}).encode())
        ok, got, updater = openlobby.pair("http://127.0.0.1:8090", " abcd-2345 ", "nobu", setup.TITLE,
                                          setup.ADMIN_URL, "tok", opener)
        self.assertEqual((ok, got, updater), (True, "secret-1", ""))
        self.assertEqual(sent[0], ("http://127.0.0.1:8090/api/pair",
                                   {"code": "abcd-2345", "key": "nobu", "title": "Nobunaga's Ambition Online",
                                    "url": "http://nobu:19111", "token": "tok"}))
        ok, _, _ = openlobby.pair_update("http://127.0.0.1:8090", "nobu", "secret-1", "tok2", opener=opener)
        self.assertTrue(ok)
        self.assertEqual(sent[1], ("http://127.0.0.1:8090/api/pair/update",
                                   {"key": "nobu", "pairing": "secret-1", "token": "tok2"}))

        def refused(code, body):
            def f(req, timeout=0):
                raise urllib.error.HTTPError(req.full_url, code, "no", {}, io.BytesIO(body))
            return f
        self.assertEqual(openlobby.pair("http://x", "c", "nobu", "t", "u", "tok", refused(403, b'{"error": "x"}')),
                         (False, "The code is wrong, used or older than 10 minutes.", ""))
        ok, why, _ = openlobby.pair("http://x", "c", "nobu", "t", "u", "tok", refused(400, b'{"error": "already set"}'))
        self.assertFalse(ok)
        self.assertIn("POL_ADMIN_GAME_NOBU_URL", why)


class FirstRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        d = tempfile.mkdtemp()
        disc = tempfile.mkdtemp()
        with open(os.path.join(disc, "SYSTEM.CNF"), "wb") as f:
            f.write(CNF)
        answers = iter(["2", "203.0.113.7", disc, "Test Server"])
        out = io.StringIO()
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch.object(setup, "public_address", lambda: "203.0.113.7"), \
                mock.patch.object(setup.disclib, "likely_folders", lambda places: []), \
                mock.patch("sys.stdout", out):
            setup.use_root(d)
            try:
                setup.ENV_EXAMPLE = os.path.join(HERE, ".env.example")
                setup.first_run(dry_run=True)
            finally:
                setup.use_root(HERE)
        text = out.getvalue()
        self.assertIn("NOBU_PUBLIC_IP=203.0.113.7", text)
        self.assertIn("NOBU_SERVER_NAME=Test Server", text)
        self.assertIn("Found: %s" % disc, text)
        self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
