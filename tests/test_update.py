"""setup.py --update without Docker or GitHub: a fake docker answers `compose ps` and records the
builds and restarts, a fake release download is a zip made here. Also the updater block setup
sends when it pairs with a panel."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import setup                                    # noqa: E402
from hostsetup import openlobby                 # noqa: E402

OLD = "1" * 40
NEW = "2" * 40


class FakeDocker:
    """setup.run and proglib.run_logged for docker commands: answers `compose ps` from the running
    services, fails the commands it is told to, and records everything with its folder."""

    def __init__(self, running=(), fail=()):
        self.running = list(running)
        self.fail = set(fail)               # "build", "extract", "up", or "exec <service>"
        self.calls = []                     # (argv, cwd)

    def run(self, cmd, check=True, capture=False, **kw):
        self.calls.append((list(cmd), kw.get("cwd") or setup.ROOT))
        out = "\n".join(self.running) if cmd[:3] == ["docker", "compose", "ps"] else ""
        if cmd[2:3] == ["exec"] and "exec " + cmd[cmd.index("-c") + 2] in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "refused")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def logged(self, cmd, log, on_line=None, progress=None, cwd=None, env=None):
        self.calls.append((list(cmd), cwd))
        self.envs = getattr(self, "envs", []) + [env or {}]
        word = "build" if "build" in cmd else "extract" if "extract" in cmd else "up" if "up" in cmd else ""
        if word in self.fail:
            return 1, ["%s failed" % word]
        if word == "up":
            self.running = sorted(set(self.running) | set(cmd[cmd.index("--no-deps") + 1:]))
        return 0, ["ok"]

    def docker(self, args, timeout=600, cwd=None):
        self.calls.append((["docker"] + list(args), cwd))
        return 0, ""

    def commands(self, word):
        return [(c, cwd) for c, cwd in self.calls if word in c]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def release_zip(path, extract_text="old"):
    """A GitHub source zip of the new release: one top folder, as GitHub makes them."""
    with zipfile.ZipFile(path, "w") as z:
        top = "psnaga-%s/" % NEW
        z.writestr(top + "compose.yml", "name: nobu\n# new\n")
        z.writestr(top + "setup.py", "# new setup\n")
        z.writestr(top + "server/nobu/world.py", "# new world\n")
        z.writestr(top + "extract/__main__.py", extract_text + "\n")
        z.writestr(top + "overlay/text/names.tsv", "1\tName\n")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        disc = os.path.join(self.root, "game.iso")
        write(disc, "iso")
        old = os.path.join(self.root, "old.zip")
        with zipfile.ZipFile(old, "w") as z:
            z.writestr("psnaga/compose.yml", "name: nobu\n# old\n")
            z.writestr("psnaga/server/nobu/world.py", "# old world\n")
            z.writestr("psnaga/server/nobu/gone.py", "# removed by the release\n")
            z.writestr("psnaga/extract/__main__.py", "old\n")
            z.writestr("psnaga/overlay/text/names.tsv", "1\tName\n")
            z.writestr("psnaga/.setup-revision", json.dumps({"commit": OLD, "built_at": "2026-10-01T00:00:00Z"}))
        setup.unpack_server(old, self.root, "v1.0.0")
        os.remove(old)
        write(os.path.join(self.root, ".env"), "NOBU_PUBLIC_IP=192.0.2.10\nNOBU_ADMIN_TOKEN=t\n")
        write(os.path.join(self.root, "state", "data", "chars.json"), "{\"keep\": true}")
        write(os.path.join(self.root, "state", "assets", "manifest.json"), "{}")
        write(os.path.join(self.root, "state", "setup.json"), json.dumps({"disc": {"path": disc, "member": ""}}))
        self.saved_root = setup.ROOT
        setup.use_root(self.root)
        self.addCleanup(setup.use_root, self.saved_root)

    def update(self, dock, extract_text="old", release=("v1.1.0", NEW, "https://example.invalid/new.zip"),
               yes=True):
        out = io.StringIO()

        def no_local_connection(*a, **kw):
            raise AssertionError("the update's check connected from setup's own machine: %r" % (a,))

        def no_input(prompt=""):
            raise RuntimeError("lost sys.stdin")     # what input() does with stdin closed

        with mock.patch.object(setup, "run", dock.run), \
                mock.patch.object(setup.proglib, "run_logged", dock.logged), \
                mock.patch.object(setup, "docker_run", dock.docker), \
                mock.patch.object(setup, "wait_for_docker", lambda wait=True: True), \
                mock.patch.object(setup, "latest_release", lambda: release), \
                mock.patch.object(setup, "download", lambda url, path: release_zip(path, extract_text)), \
                mock.patch.object(setup.socket, "create_connection", no_local_connection), \
                mock.patch.object(setup.time, "sleep", lambda s: None), \
                mock.patch("builtins.input", no_input), \
                mock.patch("sys.stdout", out):
            code = setup.update_now(yes)
        lines = out.getvalue().strip().splitlines()
        return code, lines

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def test_already_current(self):
        dock = FakeDocker(running=["world"])
        code, lines = self.update(dock, release=("v1.0.0", OLD, "u"))
        self.assertEqual((code, lines[-1]), (0, "Already up to date (v1.0.0)."))
        self.assertEqual(dock.commands("build") + dock.commands("up"), [])

    def test_update_swaps_rebuilds_and_restarts_only_what_ran(self):
        dock = FakeDocker(running=["connsv", "world"])
        code, lines = self.update(dock)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "Updated to v1.1.0; restarted connsv, world, and the server answers.")
        builds = dock.commands("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][1], os.path.join(self.root, ".update", "server"))   # beside the old files
        self.assertEqual(dock.envs[0]["NOBU_REVISION"], NEW)
        self.assertEqual(dock.commands("up"), [(["docker", "compose", "up", "-d", "--no-build", "--no-deps",
                                                 "connsv", "world"], self.root)])
        self.assertEqual(dock.commands("extract"), [])                    # extract/ and overlay/ unchanged
        self.assertEqual(self.read("server/nobu/world.py"), "# new world\n")
        self.assertEqual(self.read("setup.py"), "# new setup\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "server", "nobu", "gone.py")))
        self.assertIn("NOBU_ADMIN_TOKEN=t", self.read(".env"))
        self.assertEqual(self.read("state/data/chars.json"), "{\"keep\": true}")
        self.assertEqual(json.loads(self.read(".setup-revision"))["commit"], NEW)
        self.assertEqual(self.read(".setup-version").strip(), "v1.1.0")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".update")))
        # run again: nothing to do
        code, lines = self.update(dock)
        self.assertEqual((code, lines[-1]), (0, "Already up to date (v1.1.0)."))

    def test_the_check_runs_inside_the_servers_containers(self):
        dock = FakeDocker(running=["connsv", "auth", "ssl3", "world"])
        code, lines = self.update(dock)
        self.assertEqual(code, 0, lines)
        execs = [c for c, _ in dock.commands("exec")]
        self.assertTrue(all(c[:5] == ["docker", "compose", "exec", "-T", "world"] for c in execs))
        self.assertEqual([c[c.index("-c") + 2:] for c in execs],
                         [["connsv", "9070", setup.UPDATE_CHECK.hex()], ["auth", "18080"], ["ssl3", "9443"],
                          ["world", "9110"]])
        self.assertNotIn("127.0.0.1", setup.PORT_CHECK)

    def test_a_service_that_does_not_answer_fails_the_update(self):
        dock = FakeDocker(running=["connsv", "world"], fail={"exec connsv"})
        clock = iter(range(0, 100000, 30))
        with mock.patch.object(setup.time, "monotonic", lambda: next(clock)):
            code, lines = self.update(dock)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "Updated to v1.1.0 and restarted, but connsv does not answer: see the "
                                    "server's logs.")

    def test_a_stopped_server_stays_stopped(self):
        dock = FakeDocker(running=[])
        code, lines = self.update(dock)
        self.assertEqual((code, lines[-1]), (0, "Updated to v1.1.0. The server was stopped and stays stopped."))
        self.assertEqual(dock.commands("up"), [])

    def test_a_changed_builder_builds_the_game_data_again(self):
        dock = FakeDocker(running=["world"])
        code, lines = self.update(dock, extract_text="new")
        self.assertEqual(code, 0, lines)
        extract = dock.commands("extract")
        self.assertEqual(len(extract), 1)
        self.assertEqual(extract[0][1], self.root)                        # with the new files in place
        self.assertLess(dock.calls.index((extract[0][0], extract[0][1])),
                        dock.calls.index(dock.commands("up")[0]))
        self.assertNotIn(setup.DATA_PENDING, setup.read_choices())

    def test_a_failed_game_data_build_is_done_by_the_next_run(self):
        dock = FakeDocker(running=["world"], fail={"extract"})
        code, lines = self.update(dock, extract_text="new")
        self.assertEqual(code, 1)
        self.assertIn("building the game data failed", lines[-1])
        self.assertEqual(dock.commands("up"), [])                         # the old containers keep running
        self.assertTrue(setup.read_choices()[setup.DATA_PENDING])
        dock.fail = set()
        code, lines = self.update(dock, extract_text="new")
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(dock.commands("extract")), 2)
        self.assertNotIn(setup.DATA_PENDING, setup.read_choices())

    def test_a_failed_build_keeps_the_old_version(self):
        dock = FakeDocker(running=["connsv", "world"], fail={"build"})
        code, lines = self.update(dock)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "Building v1.1.0 failed, so the server's files were not changed and the old "
                                    "version keeps running.")
        self.assertEqual(self.read("server/nobu/world.py"), "# old world\n")
        self.assertEqual(json.loads(self.read(".setup-revision"))["commit"], OLD)
        self.assertEqual(dock.commands("up"), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".update")))

    def test_failures_end_with_one_sentence(self):
        dock = FakeDocker(running=["world"])
        code, lines = self.update(dock, release=None)
        self.assertEqual((code, lines[-1]), (1, "Could not reach GitHub to find the newest release, so nothing "
                                                "was changed."))
        dock = FakeDocker(running=["world"], fail={"up"})
        code, lines = self.update(dock)
        self.assertEqual(code, 1)
        self.assertTrue(lines[-1].startswith("Updated to v1.1.0, but the server did not start again"))

    def test_without_yes_it_asks_and_closed_input_is_a_no(self):
        dock = FakeDocker(running=["world"])
        code, lines = self.update(dock, yes=False)
        self.assertEqual(code, 1)                   # input() raised: nothing was changed
        self.assertEqual(self.read("server/nobu/world.py"), "# old world\n")

    def test_runs_from_the_folder_with_stdin_closed(self):
        empty = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, os.path.join(HERE, "setup.py"), "--update", "--yes", "--folder", empty],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "There is no server in %s to update." % empty)


class Progress(unittest.TestCase):
    def test_a_long_step_says_it_is_still_going(self):
        now = [0.0]
        out = io.StringIO()
        p = setup.proglib.Progress(out=out, clock=lambda: now[0], heartbeat=120)
        p.begin("Getting the server program ready", "about 5 minutes")
        for t in range(0, 400, 10):
            now[0] = t
            p.tick()
        p.end()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 5)             # begin, 120 s, 240 s, 360 s, done
        self.assertIn("2 minutes so far", lines[1])


class Pairing(unittest.TestCase):
    def opener(self, answer, sent):
        class Answer(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def f(req, timeout=0):
            sent.append((req.full_url, json.loads(req.data)))
            return Answer(json.dumps(answer).encode())
        return f

    def test_pairing_sends_the_updater(self):
        sent = []
        block = openlobby.updater("/srv/Nobunaga Server", setup.UPDATE_COMMAND)
        ok, got, updater = openlobby.pair("http://panel", "abcd", "nobu", setup.TITLE, setup.ADMIN_URL, "tok",
                                          self.opener({"ok": True, "pairing": "p1", "updater": "requested"}, sent),
                                          updater=block)
        self.assertEqual((ok, got, updater), (True, "p1", "requested"))
        self.assertEqual(sent[0][1]["updater"], {"folder": "/srv/Nobunaga Server",
                                                 "command": ["python", "setup.py", "--update", "--yes"],
                                                 "timeout_minutes": 60})
        ok, why, updater = openlobby.pair_update("http://panel", "nobu", "p1", "tok", updater=block,
                                                 opener=self.opener({"ok": True}, sent))
        self.assertEqual((ok, updater), (True, ""))
        self.assertEqual(sent[1][0], "http://panel/api/pair/update")
        self.assertEqual(sent[1][1]["updater"]["folder"], "/srv/Nobunaga Server")

    def test_the_command_fits_the_contract(self):
        block = setup.updater_block()
        self.assertTrue(os.path.isabs(block["folder"]))
        self.assertIn(block["command"][0], ("python", "python3"))
        self.assertLessEqual(len(block["command"]), 16)
        self.assertTrue(all(len(a) < 200 for a in block["command"]))
        self.assertEqual(block["timeout_minutes"], 60)

    def test_pair_with_panel_tells_the_host_about_approval(self):
        d = tempfile.mkdtemp()
        saved = setup.ROOT
        setup.use_root(d)
        self.addCleanup(setup.use_root, saved)
        write(os.path.join(d, ".env"), "NOBU_ADMIN_TOKEN=t\n")
        out, sent = io.StringIO(), []

        def pair(panel, code, key, title, url, token, updater=None):
            sent.append(updater)
            return True, "p1", "requested"
        with mock.patch.object(setup.openlobby, "pair", pair), mock.patch("builtins.input", lambda p="": "abcd"), \
                mock.patch("sys.stdout", out):
            self.assertTrue(setup.pair_with_panel(setup.read_env(), "http://panel"))
        self.assertEqual(sent[0]["folder"], d)
        self.assertIn("Settings > Modules", out.getvalue())
        self.assertEqual(setup.read_choices()["updater_folder"], d)

    def test_a_moved_folder_is_sent_again(self):
        d = tempfile.mkdtemp()
        saved = setup.ROOT
        setup.use_root(d)
        self.addCleanup(setup.use_root, saved)
        sent = []

        def pair_update(panel, key, pairing, token, url=None, opener=None, updater=None):
            sent.append(updater["folder"])
            return True, "", ""
        env = {"OPENLOBBY_PAIRING": "p1", "NOBU_ADMIN_TOKEN": "t"}
        with mock.patch.object(setup.openlobby, "pair_update", pair_update), \
                mock.patch.object(setup.openlobby, "find_panel", lambda run: "http://panel"), \
                mock.patch("sys.stdout", io.StringIO()):
            setup.tell_panel_the_folder(env, {"updater_folder": "/old/place"})
            setup.tell_panel_the_folder(env, setup.read_choices())
        self.assertEqual(sent, [d])


if __name__ == "__main__":
    unittest.main()
