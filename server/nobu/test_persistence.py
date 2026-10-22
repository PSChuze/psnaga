"""Crash-safe persistence: atomic writes, backups, corruption recovery, deferred saves, shutdown, admin CLI."""
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nobu import admin, auth, chars, persist, shops, world

ENV_KEYS = ("NOBU_BACKUP_DIR", "NOBU_BACKUP_KEEP", "NOBU_BACKUP_SECS", "NOBU_SAVE_DELAY", "NOBU_DATA",
            "NOBU_CHAR_DB", "NOBU_AUTH_DB", "NOBU_LOG_DIR")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["NOBU_DATA"] = self.d
        self.path = os.path.join(self.d, "chars.json")
        self.quiet = contextlib.redirect_stderr(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def leftovers(self):
        return [n for n in os.listdir(self.d) if n.endswith(".tmp")]

    def store_with_char(self):
        st = chars.CharStore(self.path)
        c = st.create(1, 0, "織田", "信長", [0] * 20)
        return st, c


class AtomicWrite(Base):
    def test_write_and_permissions_kept(self):
        persist.atomic_write_json(self.path, {"a": "あ"})
        self.assertEqual(self.read(self.path), {"a": "あ"})
        if os.name == "posix":
            os.chmod(self.path, 0o640)
            persist.atomic_write_json(self.path, {"a": 2})
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.assertEqual(self.leftovers(), [])

    def test_serialization_error_leaves_old_file(self):
        persist.atomic_write_json(self.path, {"a": 1})
        with self.assertRaises(TypeError):
            persist.atomic_write_json(self.path, {"a": object()})
        self.assertEqual(self.read(self.path), {"a": 1})
        self.assertEqual(self.leftovers(), [])

    def test_crash_before_replace_leaves_old_file(self):
        st, c = self.store_with_char()
        c["inventory"] = {"money": 5}
        with mock.patch.object(persist.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                st.save()
        self.assertNotIn("inventory", chars.CharStore(self.path).find(1, c["cid"]))
        self.assertEqual(self.leftovers(), [])

    def test_seeded_file_written_atomically(self):
        p = os.path.join(self.d, "shops.json")
        data = shops.load_shops(p)
        self.assertEqual(self.read(p), data)
        self.write(p, "{broken")
        self.assertEqual(shops.load_shops(p), shops.SHOPS_SEED)        # seed in memory, file untouched
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{broken")


class Recovery(Base):
    def test_corrupt_file_recovers_from_newest_good_backup(self):
        st, c = self.store_with_char()
        persist.backup_file(self.path, tag="manual")
        c["given"] = "newer"
        st.save()
        persist.backup_file(self.path, tag="manual")
        # newest backup is itself torn: it must be skipped for the one before
        bs = persist.list_backups(self.path)
        self.write(os.path.join(persist.backup_dir(self.path), os.path.basename(self.path) +
                                ".99991231T000000000000Z.auto"), '{"next_cid": 2, "cha')
        self.write(self.path, '{"next_cid": 2, "chars": {"1": [{"cid"')      # torn write
        got = chars.CharStore(self.path)
        self.assertEqual(got.find(1, c["cid"])["given"], "newer")
        self.assertEqual(self.read(self.path)["next_cid"], 2)                 # written back
        self.assertTrue(any(n.startswith("chars.json.corrupt-") for n in os.listdir(self.d)))
        self.assertGreaterEqual(len(bs), 2)

    def test_corrupt_without_backup_refuses_to_start_empty(self):
        self.write(self.path, "not json")
        with self.assertRaises(persist.PersistError):
            chars.CharStore(self.path)

    def test_wrong_shape_counts_as_corrupt(self):
        self.write(self.path, "[]")
        with self.assertRaises(persist.PersistError):
            chars.CharStore(self.path)

    def test_missing_file_with_backups_is_recovered(self):
        st, c = self.store_with_char()
        persist.backup_file(self.path)
        os.unlink(self.path)
        self.assertIsNotNone(chars.CharStore(self.path).find(1, c["cid"]))

    def test_fresh_install_starts_empty(self):
        st = chars.CharStore(self.path)
        self.assertEqual(st.data, {"next_cid": 1, "chars": {}})

    def test_auth_recovery(self):
        p = os.path.join(self.d, "auth.json")
        s = auth.AccountStore(p)
        s.add("alice", "pw")
        self.write(p, "")
        self.assertIn("alice", auth.AccountStore(p).data["accounts"])     # from the save's backup


class Backups(Base):
    def test_rotation_and_dedupe(self):
        os.environ["NOBU_BACKUP_KEEP"] = "3"
        persist.atomic_write_json(self.path, {"next_cid": 1, "chars": {}})
        for i in range(6):
            self.assertIsNotNone(persist.backup_text(self.path, json.dumps({"n": i}), tag="auto"))
        self.assertIsNone(persist.backup_text(self.path, json.dumps({"n": 5}), tag="auto"))   # same content
        bs = persist.list_backups(self.path)
        self.assertEqual([self.read(b[0])["n"] for b in bs], [5, 4, 3])

    def test_boot_backup_and_throttled_auto_backup(self):
        st, c = self.store_with_char()
        self.assertEqual(len(persist.list_backups(self.path)), 0)       # fresh file: nothing to back up yet
        chars.CharStore(self.path)                                        # boot backup of the good file
        self.assertEqual([b[2] for b in persist.list_backups(self.path)], ["boot"])
        os.environ["NOBU_BACKUP_SECS"] = "0"
        c["given"] = "x"
        st.save()
        self.assertEqual(persist.list_backups(self.path)[0][2], "auto")

    def test_backups_off(self):
        os.environ["NOBU_BACKUP_KEEP"] = "0"
        self.store_with_char()
        chars.CharStore(self.path)
        self.assertEqual(persist.list_backups(self.path), [])


class DeferredSaves(Base):
    def test_moves_coalesce_into_one_write_in_the_loop(self):
        st, c = self.store_with_char()
        writes = []
        real = persist.atomic_write_json

        def counting(*a, **kw):
            writes.append(1)
            return real(*a, **kw)

        async def go():
            with mock.patch.object(persist, "atomic_write_json", counting):
                for x in range(50):
                    st.set_pos(1, c["cid"], [10, x, 0, 0, 0], min_interval=0.05)
                self.assertEqual(writes, [])
                self.assertTrue(st.dirty)
                await asyncio.sleep(0.2)
        asyncio.run(go())
        self.assertEqual(len(writes), 1)
        self.assertFalse(st.dirty)
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["pos"], [10, 49, 0, 0, 0])

    def test_flush_writes_pending_and_immediate_save_cancels_timer(self):
        st, c = self.store_with_char()

        async def go():
            st.set_pos(1, c["cid"], [10, 7, 0, 0, 0], min_interval=60)
            self.assertIsNotNone(st._timer)
            self.assertTrue(st.flush())
            self.assertIsNone(st._timer)
            self.assertFalse(st.flush())
            st.save_soon(60)
            st.save()                               # a trade / purchase: immediate, timer dropped
            self.assertIsNone(st._timer)
        asyncio.run(go())
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["pos"], [10, 7, 0, 0, 0])

    def test_without_loop_throttles(self):
        st, c = self.store_with_char()
        st.set_pos(1, c["cid"], [10, 1, 0, 0, 0], min_interval=3600)      # saved just now by create
        self.assertTrue(st.dirty)
        self.assertNotIn("pos", self.read(self.path)["chars"]["1"][0])
        st.flush()
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["pos"], [10, 1, 0, 0, 0])
        st.set_pos(1, c["cid"], [10, 2, 0, 0, 0], min_interval=0)          # zones.save_pos: at once
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["pos"], [10, 2, 0, 0, 0])

    def test_failed_deferred_save_stays_dirty(self):
        st, c = self.store_with_char()

        async def go():
            with mock.patch.object(persist, "atomic_write_json", side_effect=OSError("full")):
                st.save_soon(0.01)
                await asyncio.sleep(0.05)
                self.assertTrue(st.dirty)
            st.flush()
        asyncio.run(go())
        self.assertFalse(st.dirty)


class AuthReload(Base):
    def test_running_server_sees_cli_add_and_ignores_garbage(self):
        p = os.path.join(self.d, "auth.json")
        server = auth.AccountStore(p)
        server.add("alice", "a")
        auth.AccountStore(p).add("bob", "b")                # `python -m nobu.auth add` in another process
        os.utime(p, ns=(1, 1))                              # coarse clocks: force a visible change
        self.assertEqual(server.check("bob", "b")[0], 0)
        server.add("carol", "c")
        self.assertEqual(set(self.read(p)["accounts"]), {"alice", "bob", "carol"})
        self.write(p, "{oops")
        self.assertFalse(server.reload_if_changed())
        self.assertIn("carol", server.data["accounts"])


class Shutdown(Base):
    def test_sigterm_path_closes_sessions_and_flushes(self):
        os.makedirs(os.path.join(self.d, "logs", "sessions"))
        st, c = self.store_with_char()
        old = world.STORE, world.LOG_DIR
        world.STORE, world.LOG_DIR = st, os.path.join(self.d, "logs")

        async def go():
            srv = await asyncio.start_server(world.session, "127.0.0.1", 0)
            port = srv.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(100):
                if world.SESSION_TASKS:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(world.SESSION_TASKS), 1)
            st.set_pos(1, c["cid"], [10, 3, 0, 0, 0], min_interval=60)
            with contextlib.redirect_stdout(io.StringIO()):
                await world.shutdown(srv, grace=2)
            self.assertEqual(world.SESSION_TASKS, {})
            w.close()
        try:
            asyncio.run(go())
        finally:
            world.STORE, world.LOG_DIR = old
        self.assertFalse(st.dirty)
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["pos"], [10, 3, 0, 0, 0])


class Admin(Base):
    def run_admin(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = admin.main(list(argv))
        return rc, out.getvalue()

    def test_backup_list_restore_validate(self):
        st, c = self.store_with_char()
        auth.AccountStore(os.path.join(self.d, "auth.json")).add("alice", "pw")
        rc, out = self.run_admin("validate")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_admin("backup")
        self.assertEqual(rc, 0, out)
        name = os.path.basename(persist.list_backups(self.path)[0][0])
        self.assertTrue(name.endswith(".manual"))
        c["given"] = "changed"
        st.save()
        rc, out = self.run_admin("list", "chars")
        self.assertIn(name, out)
        rc, out = self.run_admin("restore", name)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(self.path)["chars"]["1"][0]["given"], "信長")
        self.assertTrue(any(t == "pre-restore" for _, _, t in persist.list_backups(self.path)))

    def test_restore_refused_while_world_holds_the_lock(self):
        self.store_with_char()
        persist.backup_file(self.path)
        name = os.path.basename(persist.list_backups(self.path)[0][0])
        lock = persist.FileLock(self.path)
        self.assertTrue(lock.acquire())
        try:
            rc, out = self.run_admin("restore", name)
            self.assertEqual(rc, 1)
            self.assertIn("Stop the world service", out)
        finally:
            lock.release()
        self.assertEqual(self.run_admin("restore", name)[0], 0)

    def test_validate_finds_problems(self):
        persist.atomic_write_json(self.path, {"next_cid": 1, "chars": {"1": [{"cid": 1, "uid": 1, "slot": 0},
                                                                              {"cid": 1, "uid": 1, "slot": 1}]}})
        self.write(os.path.join(self.d, "guild.json"), "{")
        rc, out = self.run_admin("validate")
        self.assertEqual(rc, 1)
        self.assertIn("appears twice", out)
        self.assertIn("next_cid 1", out)
        self.assertIn("guild", out)

    def tree(self):
        return sorted(os.path.relpath(os.path.join(r, n), self.d)
                      for r, ds, fs in os.walk(self.d) for n in ds + fs)

    def test_validate_on_empty_dir_writes_nothing(self):
        # a fresh process, as nobu-backup runs it (docker exec ... python -m nobu.admin validate)
        env = {k: v for k, v in os.environ.items() if not k.startswith("NOBU_")}
        env["NOBU_DATA"] = self.d
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(admin.__file__)))
        r = subprocess.run([sys.executable, "-m", "nobu.admin", "validate"], cwd=pkg_root, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.tree(), [])
        for label in ("chars", "auth", "enemies", "guild", "crafting", "shops", "news", "war"):
            self.assertRegex(r.stdout, rf"(?m)^{label} +MISSING ")
        self.assertIn("(seeded on first use)", r.stdout)
        # and in-process with every write primitive trapped
        def boom(*a, **kw):
            raise AssertionError(f"validate wrote: {a!r}")
        real_open = open

        def ro_open(f, mode="r", *a, **kw):
            if any(c in mode for c in "wax+"):
                boom(f, mode)
            return real_open(f, mode, *a, **kw)
        with mock.patch("builtins.open", ro_open), mock.patch.object(os, "replace", boom), \
                mock.patch.object(os, "makedirs", boom), mock.patch.object(os, "mkdir", boom), \
                mock.patch.object(os, "unlink", boom), mock.patch("tempfile.mkstemp", boom):
            self.assertEqual(admin.validate(out=lambda *a: None), 0)
        self.assertEqual(self.tree(), [])

    def test_validate_leaves_existing_files_alone(self):
        self.store_with_char()
        self.write(os.path.join(self.d, "guild.json"), "{")
        before = {n: os.stat(os.path.join(self.d, n)).st_mtime_ns for n in os.listdir(self.d)}
        tree = self.tree()
        self.assertEqual(self.run_admin("validate")[0], 1)
        self.assertEqual(self.tree(), tree)                    # no recovery, no .corrupt, no backups
        self.assertEqual({n: os.stat(os.path.join(self.d, n)).st_mtime_ns for n in os.listdir(self.d)}, before)

    def test_bad_backup_not_restored(self):
        persist.atomic_write_json(self.path, {"next_cid": 1, "chars": {}})
        bdir = persist.backup_dir(self.path)
        os.makedirs(bdir)
        self.write(os.path.join(bdir, "chars.json.20260101T000000000000Z.manual"), "{")
        rc, out = self.run_admin("restore", "chars.json.20260101T000000000000Z.manual")
        self.assertEqual(rc, 1)
        self.assertEqual(self.read(self.path), {"next_cid": 1, "chars": {}})


if __name__ == "__main__":
    unittest.main()
