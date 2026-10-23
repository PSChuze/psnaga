"""The update notice (nobu/updates.py) against a fake GitHub API: python -m unittest nobu.test_updates -v"""
import threading
import unittest

from nobu import updates

OLD = "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MID = "2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NEW = "3333333ccccccccccccccccccccccccccccccccc"
MERGE = "4444444ddddddddddddddddddddddddddddddddd"


def commit(sha, date, message, parents=1):
    return {"sha": sha, "parents": [{"sha": "x"}] * parents,
            "commit": {"message": message, "committer": {"date": date}}}


class FakeGitHub:
    """Answers the three API calls from a table and records the paths asked for."""

    def __init__(self, release="v1.1.0", head=NEW, offline=False, status=None):
        self.release, self.head, self.offline, self.status = release, head, offline, status
        self.paths = []

    def __call__(self, url):
        path = url[len(updates.API):]
        self.paths.append(path)
        if self.offline:
            raise OSError("network is unreachable")
        if self.status:
            return self.status, {"message": "no"}
        if path == "/repos/PSChuze/psnaga/releases/latest":
            return (200, {"tag_name": self.release}) if self.release else (404, {"message": "Not Found"})
        if path == "/repos/PSChuze/psnaga/commits/" + str(self.release):
            return 200, commit(self.head, "2026-10-20T10:00:00+02:00", "Release")
        if path.startswith("/repos/PSChuze/psnaga/compare/%s..." % OLD[:7]):
            return 200, {"ahead_by": 3, "commits": [
                commit(MID, "2026-10-18T09:00:00Z", "world: fix the ferry\n\nlonger text"),
                commit(MERGE, "2026-10-19T09:00:00Z", "Merge branch x", parents=2),
                commit(NEW, "2026-10-20T08:00:00Z", "extract: read the maps")]}
        return 404, {"message": "Not Found"}


def check(revision, gh, repo="PSChuze/psnaga", built_at="2026-10-01T00:00:00Z"):
    return updates.UpdateCheck(repo, revision, built_at, fetch=gh).get(wait=5)


def assert_unknown(t, got, running_commit=OLD):
    """Every unknown answer keeps the panel's shape: objects, never null, behind 0, no changes."""
    t.assertEqual(got["update"], "unknown")
    t.assertEqual(got["latest"], {"commit": None, "date": None})
    t.assertEqual((got["behind"], got["changes"]), (0, []))
    t.assertEqual(got["running"], {"commit": running_commit or None, "built_at": "2026-10-01T00:00:00Z"})
    t.assertTrue(got["why"])
    t.assertEqual(got["how"], updates.HOW)
    return got


class UpdateCheckTest(unittest.TestCase):
    def test_available_lists_the_changes_newest_first_without_merges(self):
        got = check(OLD[:7], FakeGitHub())
        self.assertEqual(got["update"], "available")
        self.assertEqual(got["behind"], 3)
        self.assertIsNone(got["why"])
        self.assertEqual(got["latest"], {"commit": NEW, "date": "2026-10-20T08:00:00Z"})
        self.assertEqual(got["running"], {"commit": OLD[:7], "built_at": "2026-10-01T00:00:00Z"})
        self.assertEqual([c["subject"] for c in got["changes"]], ["extract: read the maps", "world: fix the ferry"])
        self.assertEqual(got["changes"][1], {"commit": MID, "date": "2026-10-18T09:00:00Z",
                                             "subject": "world: fix the ferry"})
        self.assertEqual(got["how"], "Run Nobunaga Server Setup and choose Update.")

    def test_current_needs_no_compare(self):
        gh = FakeGitHub()
        got = check(NEW, gh)
        self.assertEqual((got["update"], got["behind"], got["changes"], got["why"]), ("current", 0, [], None))
        self.assertFalse(any("/compare/" in p for p in gh.paths))

    def test_offline_is_unknown_with_a_reason(self):
        got = assert_unknown(self, check(OLD, FakeGitHub(offline=True)))
        self.assertIn("GitHub did not answer", got["why"])
        self.assertIn("too many requests", assert_unknown(self, check(OLD, FakeGitHub(status=403)))["why"])

    def test_unknown_cases(self):
        self.assertIn("no release", assert_unknown(self, check(OLD, FakeGitHub(release=None)))["why"])
        got = assert_unknown(self, check("", FakeGitHub()), running_commit="")
        self.assertIn("does not know its commit", got["why"])
        self.assertIn("not in", assert_unknown(self, check("abcdef0", FakeGitHub()), "abcdef0")["why"])
        self.assertIn("is off", assert_unknown(self, check(OLD, FakeGitHub(), repo=""))["why"])
        self.assertIn("owner/name", assert_unknown(self, check(OLD, FakeGitHub(), repo="nope"))["why"])
        gate = threading.Event()
        c = updates.UpdateCheck("PSChuze/psnaga", OLD, "2026-10-01T00:00:00Z",
                                fetch=lambda url: (gate.wait(5), FakeGitHub()(url))[1])
        self.assertIn("still asking", assert_unknown(self, c.get(wait=0))["why"])
        gate.set()
        c.thread and c.thread.join(5)

    def test_the_answer_is_cached(self):
        now = [0.0]
        gh = FakeGitHub()
        c = updates.UpdateCheck("PSChuze/psnaga", NEW, fetch=gh, clock=lambda: now[0])
        c.get(wait=5)
        asked = len(gh.paths)
        now[0] = updates.TTL - 1
        c.get(wait=5)
        self.assertEqual(len(gh.paths), asked)
        now[0] = updates.TTL + 1
        c.get(wait=5)
        c.thread and c.thread.join(5)
        self.assertGreater(len(gh.paths), asked)

    def test_settings(self):
        self.assertEqual(updates.repo_setting({}), "PSChuze/psnaga")
        self.assertEqual(updates.repo_setting({"NOBU_UPDATE_REPO": ""}), "")
        self.assertEqual(updates.repo_setting({"NOBU_UPDATE_REPO": "off"}), "")
        c = updates.from_env({"NOBU_UPDATE_REPO": "me/fork", "NOBU_REVISION": NEW, "NOBU_BUILT_AT": "x"})
        self.assertEqual((c.repo, c.revision, c.built_at), ("me/fork", NEW, "x"))


if __name__ == "__main__":
    unittest.main()
