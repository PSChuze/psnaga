"""Self-test for nobu.auth: python -m unittest nobu.test_auth -v"""
import datetime as dt
import os
import tempfile
import unittest
import urllib.parse

from nobu import auth

# the client's login request, as it arrives behind the TLS terminator
CAPTURED_TARGET = "/auth?a=Of+Caaaaaa+Paaaaaa+T000f4309%252C001e8549+R1567ef"

# WDMMSG group for a client error code (table 0x424e90, read by 0x21e430)
ERR_TABLE = {0: 0x47, -1: 0x48, -2: 0x4a, -3: 0x4c, -4: 0x4e, -5: 0x4f, -6: 0x50, -11: 0x51,
             -12: 0x52, -13: 0x53, -21: 0x54, -22: 0x55, -31: 0x56, -32: 0x57, -33: 0x58,
             -34: 0x59, -35: 0x5a, -41: 0x5b}


def split(s, delims):
    """Model of the client tokenizer 0x21ddd0: a run of k delimiters yields k-1 empty tokens."""
    out, cur, i = [], None, 0
    while i < len(s):
        c = s[i]
        if c not in delims:
            if cur is None:
                cur = i
        else:
            if cur is not None:
                out.append(s[cur:i])
                cur = None
            if i + 1 < len(s) and s[i + 1] in delims:
                out.append("")
        i += 1
    if cur is not None:
        out.append(s[cur:])
    return out


def hexval(s):
    """0x21dd50: hex with optional '-', -1 on a bad digit."""
    neg = s.startswith("-")
    s = s[1:] if neg else s
    try:
        v = int(s, 16) if s else 0
    except ValueError:
        return -1
    return -v if neg else v


def client_parse(body):
    """Model of 0x28f800 -> 0x28fb80 -> 0x290cd0. Returns the client error code (0 = success)."""
    text = body.decode("ascii")
    text = text[:len(text) - 1]                       # 0x28f840: splits len-1 bytes
    part0 = text.split("\n\n", 1)[0]                   # 0x21df00 with 0x28f7b0 ("\n\n")
    lines = split(part0, "\n")                         # 0x28fac0
    if not lines:
        return -1
    f = split(lines[0], ", \n")                        # 0x28fae0
    if int(f[0]):                                      # 0x28fc14 atoi
        return -int(f[0])
    now = f[2]
    assert len(now) >= 14                              # 0x28ffc0 reads 14 digits
    if len(lines) < 2:                                 # 0x28fc44
        return -1
    playable = 0
    for line in lines[1:]:
        g = split(line, ", \n") + [None] * 16
        uid, code = hexval(g[0]), hexval(g[1])
        expiry = g[7]
        course = hexval(g[5]) if g[5] else 0
        flags = hexval(g[15]) if g[15] else 0
        assert uid != -1 and code != -1
        # 0x290210 status: 0 = valid (expiry after server time)
        if expiry == "29991231":
            status = 1
        elif course == 0:
            status = 2 if expiry < now[:8] else 0
        else:
            status = {1: 3, 2: 4}.get(course, 4)
        assert status in (0, 1)
        if code == 0xF4309:                            # 0x290bb0
            playable += 1
        assert auth.handle_name(uid) is not None
    return 0 if playable > 0 else -6                   # 0x28fd90


class Request(unittest.TestCase):
    def test_parse_capture(self):
        q = urllib.parse.urlsplit(CAPTURED_TARGET).query
        a = urllib.parse.parse_qs(q)["a"][0]
        req = auth.parse_a(a)
        self.assertEqual(req["id"], "aaaaaa")
        self.assertEqual(req["password"], "aaaaaa")
        self.assertEqual(req["titles"], [1000201, 2000201])
        self.assertEqual(req["r"], "1567ef")

    def test_password_with_symbols(self):
        # client: 0x21e050 per field, then the whole string again
        inner = "Of C%s P%s T000f4309%%2C001e8549 R1567ef" % ("abc", "p a+ss%")
        inner = inner.replace("p a+ss%", urllib.parse.quote_plus("p a+ss%"))
        target = "/auth?a=" + urllib.parse.quote_plus(inner)
        a = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)["a"][0]
        self.assertEqual(auth.parse_a(a)["password"], "p a+ss%")

    def test_full_width_is_normalised(self):
        # cp932 full-width "ｃａｓ" / "ＰＷ１" plus an ideographic space, as the pad keyboard types
        cid = urllib.parse.quote_from_bytes("ｃａｓ".encode("cp932"))
        pw = urllib.parse.quote_from_bytes("ＰＷ１　".encode("cp932"))
        req = auth.parse_a("Of C%s P%s T000f4309%%2C001e8549 R1567ef" % (cid, pw))
        self.assertEqual(req["id"], "cas")
        self.assertEqual(req["password"], "PW1")

    def test_id_lowercased(self):
        self.assertEqual(auth.parse_a("Of CCas Px R1567ef")["id"], "cas")

    def test_redact(self):
        self.assertNotIn("Paaaaaa", auth.redact("GET " + CAPTURED_TARGET))
        self.assertIn("+P***", auth.redact("GET " + CAPTURED_TARGET))


class Response(unittest.TestCase):
    NOW = dt.datetime(2025, 1, 2, 12, 0, 0, tzinfo=auth.JST)

    def test_success_is_accepted(self):
        uid = auth.uid_for(1)
        body = auth.success_body([dict(uid=uid, name=auth.handle_name(uid))], now=self.NOW)
        self.assertEqual(client_parse(body), 0)
        self.assertTrue(body.startswith(b"0,0,20250102120000\n"))

    def test_error_maps_to_084(self):
        self.assertEqual(ERR_TABLE[client_parse(auth.error_body(auth.ERR_AUTH_FAILED))], 0x54)
        self.assertEqual(ERR_TABLE[client_parse(auth.error_body(auth.ERR_BAD_ID))], 0x4f)

    def test_server_verdicts_are_not_084(self):
        # 0.84 is what the client shows for its own local rejections; the server's are distinct
        self.assertEqual(ERR_TABLE[client_parse(auth.error_body(auth.ERR_WRONG_PASSWORD))], 0x59)
        self.assertEqual(ERR_TABLE[client_parse(auth.error_body(auth.ERR_NO_SUCH_ID))], 0x48)

    def test_handle(self):
        self.assertEqual(auth.handle_name(auth.uid_for(1)), "BBBC0001")
        self.assertIsNone(auth.handle_name(0))

    def test_http_framing(self):
        r = auth.http_response(b"x\n")
        self.assertTrue(r.startswith(b"HTTP/1.0 200 OK\r\n"))
        self.assertIn(b"Content-Length: 2\r\n", r)


class Store(unittest.TestCase):
    def test_accounts(self):
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            st.add("abc", "pw")
            self.assertEqual(st.check("abc", "pw")[0], 0)
            self.assertEqual(st.check("abc", "no")[0], auth.ERR_WRONG_PASSWORD)
            self.assertEqual(st.check("zzz", "pw")[0], auth.ERR_NO_SUCH_ID)
            self.assertEqual(st.check("ABC", "pw")[0], auth.ERR_BAD_ID)
            self.assertEqual(st.check("zzz", "pw", open_mode=True)[0], 0)
            self.assertIn("zzz", auth.AccountStore(os.path.join(d, "auth.json")).data["accounts"])

    def test_claim_mode(self):
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            self.assertEqual(st.check("newbie", "", open_mode="claim")[0], auth.ERR_AUTH_FAILED)
            self.assertEqual(st.check("newbie", "pw1", open_mode="claim")[0], 0)   # claimed
            self.assertEqual(st.check("newbie", "pw1", open_mode="claim")[0], 0)
            self.assertEqual(st.check("newbie", "other", open_mode="claim")[0], auth.ERR_WRONG_PASSWORD)
            self.assertTrue(st.data["accounts"]["newbie"]["password"].startswith("pbkdf2_sha256$"))

    def test_plaintext_legacy_entry(self):
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            st.data["accounts"]["old"] = {"password": "pw", "uid": auth.uid_for(1)}
            self.assertEqual(st.check("old", "pw")[0], 0)
            self.assertEqual(st.check("old", "no", open_mode="claim")[0], auth.ERR_WRONG_PASSWORD)

    def test_cp932_passwords_do_not_collide(self):
        # the old UTF-8 decoding turned every cp932 lead/trail byte into U+FFFD
        enc = lambda t: urllib.parse.quote_from_bytes(t.encode("cp932"))
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            a = auth.parse_a("Of Cabc P%s R1567ef" % enc("ねこ"))
            self.assertEqual(st.check(a["id"], a["password"], "claim", a["password_legacy"])[0], 0)
            b = auth.parse_a("Of Cabc P%s R1567ef" % enc("いぬ"))
            self.assertEqual(st.check(b["id"], b["password"], "claim", b["password_legacy"])[0],
                             auth.ERR_WRONG_PASSWORD)

    def test_full_and_half_width_password_match(self):
        enc = lambda t: urllib.parse.quote_from_bytes(t.encode("cp932"))
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            a = auth.parse_a("Of Cabc P%s R1567ef" % enc("ｐｗ１"))
            self.assertEqual(st.check(a["id"], a["password"], "claim", a["password_legacy"])[0], 0)
            b = auth.parse_a("Of Cabc Ppw1 R1567ef")
            self.assertEqual(st.check(b["id"], b["password"], "claim", b["password_legacy"])[0], 0)

    def test_legacy_hash_still_matches_and_upgrades(self):
        enc = lambda t: urllib.parse.quote_from_bytes(t.encode("cp932"))
        a = auth.parse_a("Of Cabc P%s R1567ef" % enc("ｐｗ"))
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            st.add("abc", a["password_legacy"])           # stored by the old server
            self.assertEqual(st.check(a["id"], a["password"], "claim", a["password_legacy"])[0], 0)
            self.assertEqual(st.check("abc", "pw", "claim")[0], 0)   # re-hashed as "pw"

    def test_answer_capture(self):
        with tempfile.TemporaryDirectory() as d:
            st = auth.AccountStore(os.path.join(d, "auth.json"))
            q = urllib.parse.urlsplit(CAPTURED_TARGET).query
            body, _ = auth.answer(q, st, open_mode=False)
            self.assertEqual(client_parse(body), -1)            # 0.72 account not found
            body, note = auth.answer(q, st, open_mode=True)
            self.assertEqual(client_parse(body), 0, note)
