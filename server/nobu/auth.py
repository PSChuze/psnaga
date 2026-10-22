"""GAMECITY login backend (plain HTTP; stunnel in front terminates the client's TLS).

The client (after our 0x22 reply names the host) sends
    GET /auth?a=Of+C<id>+P<password>+T<hex>%252C<hex>+R1567ef HTTP/1.0
and expects HTTP 200 with a text body (decode, "Login response"):

    <result>,<unused>,<YYYYMMDDhhmmss>\n                 line 0 (0x28fb80)
    <uid>,<code>,0,0,0,<course>,0,<YYYYMMDD>,0,0,0,0,0,0,<name>,<flags>\n   one line per account
    [\n<notice text>]                                     optional notice after a blank line

result 0 = success; any other number N makes the client show the WDMMSG message for error -N
(table 0x424e90). uid, code, course and flags are HEX (0x21dd50).

Which failure says what (the client itself shows 0.84 for every LOCAL rejection, so the server
uses other codes to make its own verdicts tell apart):
  21 -> 0.84 "authentication failed"   client-local: ID not 1-8 of a-z, or empty password
                                        (0x28f6c0 / 0x21e050, the request never leaves the PS2);
                                        server: request without a usable ID/password
  5  -> 0.79 "invalid characters"      server: the ID is not 1-8 of a-z after normalising
  1  -> 0.72 "account not found"       server, closed mode: no such ID
  34 -> 0.89 "already registered"      server: the ID exists and the password does not match
Before checking, the ID and password are decoded from the client's bytes as cp932 and NFKC
normalised (full-width ａｂｃ１ -> abc1, ideographic space -> space), then trimmed; the ID is
lowercased. A stored password that only matches the old decoding (UTF-8, lossy for cp932) still
works and is re-hashed in the normalised form.
An account line is usable when code == 0xf4309 (1000201, the client's Code) (0x290bb0).

Env:
  NOBU_AUTH_BIND   bind address (default 127.0.0.1)
  NOBU_AUTH_PORT   port (default 18080)
  NOBU_AUTH_DB     account file (default $NOBU_DATA/auth.json, else ./data/auth.json)
  NOBU_AUTH_OPEN   1 = accept any valid-format ID with any password (creates the account; DEV
                   ONLY: passwords are never checked). claim = an unknown ID is created with the
                   password it first logs in with, after that the password must match (public).
  NOBU_LOG_DIR     log root (default ./logs); requests go to auth.log and events.jsonl

Accounts:
  python -m nobu.auth add <id> <password>
  python -m nobu.auth list
"""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import unicodedata
import urllib.parse

from . import gamelink, persist

CLIENT_CODE = 0xF4309          # 1000201, gp-0x7e30; the account line must carry it (0x290bb0)
DEFAULT_FLAGS = 0x40000000     # 0x290700 returns this when no account is selected
DEFAULT_EXPIRY = "20991231"    # after the server time -> status 0 "valid" (0x290210)

ERR_AUTH_FAILED = 21           # -21 -> WDMMSG 0.84 (0x424e90); also every client-local rejection
ERR_BAD_ID = 5                 # -5  -> WDMMSG 0.79 "citizen ID contains invalid characters"
ERR_NO_ACCOUNT = 6             # -6  -> WDMMSG 0.80 "no playable account"
ERR_NO_SUCH_ID = 1             # -1  -> WDMMSG 0.72 "account not found" (closed mode only)
ERR_WRONG_PASSWORD = 34        # -34 -> WDMMSG 0.89 "already registered" (ID taken, password differs)

ID_RE = re.compile(r"^[a-z]{1,8}$")   # client check 0x28f6c0 + 9-byte field at login obj +0x148

HANDLE_LETTERS = "BCDFGHKLMNPRTVWJQSXYZ"   # 0x42b510, used by 0x2ac260
JST = dt.timezone(dt.timedelta(hours=9))

BIND = os.environ.get("NOBU_AUTH_BIND", "127.0.0.1")
PORT = int(os.environ.get("NOBU_AUTH_PORT", "18080"))
OPEN = {"1": True, "claim": "claim"}.get(os.environ.get("NOBU_AUTH_OPEN", ""), False)
LOG_DIR = os.environ.get("NOBU_LOG_DIR", os.path.join(os.getcwd(), "logs"))


def default_db():
    if os.environ.get("NOBU_AUTH_DB"):
        return os.environ["NOBU_AUTH_DB"]
    base = os.environ.get("NOBU_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "auth.json")


# ---------------------------------------------------------------------------- request

def field_text(val):
    """(normalised, legacy) text of one %-encoded field.

    The client sends the bytes it holds, cp932 when the player typed Japanese-keyboard characters.
    normalised: cp932 (latin-1 if that fails) -> NFKC -> trimmed, so full-width ａｂｃ１２３ and a
    stray ideographic space compare equal to what a USB keyboard types. legacy: the old UTF-8
    decoding, kept so passwords stored before normalisation still match.
    """
    raw = urllib.parse.unquote_to_bytes(val.replace("+", " "))
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return unicodedata.normalize("NFKC", text).strip(), urllib.parse.unquote_plus(val)


def parse_a(a):
    """Decode the already query-decoded a= value: "Of C<id> P<pw> T<hex>%2C<hex> R1567ef".

    The client URL-encodes ID and password (0x21e050: alnum kept, space -> '+', else %XX),
    joins the fields with spaces, and URL-encodes the whole string again (0x28f180).
    id is lowercased after normalisation; password_legacy is the pre-normalisation decoding.
    """
    out = {"id": None, "password": None, "password_legacy": None, "titles": [], "r": None,
           "tag": None}
    for tok in a.split(" "):
        if not tok:
            continue
        key, val = tok[0], tok[1:]
        if key == "C":
            out["id"] = field_text(val)[0].lower()
        elif key == "P":
            out["password"], out["password_legacy"] = field_text(val)
        elif key == "T":
            out["titles"] = [int(x, 16) for x in urllib.parse.unquote_plus(val).split(",") if x]
        elif key == "R":
            out["r"] = val
        elif key == "O":
            out["tag"] = tok
    return out


def redact(text):
    """Hide the password in a raw or decoded request line."""
    text = re.sub(r"(\+P)[^+&\s]*", r"\1***", text)
    return re.sub(r"( P)\S*", r"\1***", text)


# ---------------------------------------------------------------------------- response

def handle_name(uid):
    """The 8-character handle the client derives from a uid (0x2ac260), or None."""
    top = uid >> 26
    if not 1 <= top <= 20:
        return None
    n = uid // 10000 - 6710
    if n < 0 or n >= 21 ** 4:
        return None
    letters = [HANDLE_LETTERS[(n // 21 ** k) % 21] for k in (3, 2, 1, 0)]
    return "".join(letters) + f"{uid % 10000:04d}"


def uid_for(n):
    """A uid whose client-side handle is well formed (top 6 bits 1..20, 0x2ac260)."""
    uid = (6710 + n) * 10000 + n % 10000
    assert handle_name(uid) is not None
    return uid


def account_line(uid, name, code=CLIENT_CODE, course=0, expiry=DEFAULT_EXPIRY, flags=DEFAULT_FLAGS):
    """One account entry, 16 fields (0x28fca8 loop; unused fields are sent as 0)."""
    name_enc = urllib.parse.quote(name, safe="")[:20]     # decoded by 0x21e1c0 into 21 bytes
    f = ["0"] * 16
    f[0] = f"{uid:x}"          # entry +0x00 (0x28fe40)
    f[1] = f"{code:x}"         # entry +0x14 (0x290a30), must be 0xf4309
    f[5] = f"{course:x}"       # entry +0x0b before 0x290210 (0/1/2)
    f[7] = expiry              # entry +0x04..+0x0a, YYYYMMDD 23:59:59 (0x28feb0)
    f[14] = name_enc           # entry +0x18 (0x290970)
    f[15] = f"{flags:x}"       # entry +0x10 (0x2903d0), later sent in 0x14
    return ",".join(f)


def success_body(accounts, now=None, notice=None):
    now = now or dt.datetime.now(JST)
    lines = [f"0,0,{now:%Y%m%d%H%M%S}"] + [account_line(**a) for a in accounts]
    body = "\n".join(lines) + "\n"
    if notice:
        body += "\n" + notice + "\n"
    return body.encode("ascii")


def error_body(code):
    return f"{code},0,0\n".encode("ascii")


def http_response(body, status="200 OK"):
    head = (f"HTTP/1.0 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n")
    return head.encode("ascii") + body


# ---------------------------------------------------------------------------- accounts

def valid_accounts(d):
    return isinstance(d, dict) and isinstance(d.get("accounts"), dict) and all(
        isinstance(a, dict) and "uid" in a for a in d["accounts"].values())


class AccountStore:
    """auth.json, written atomically with rotating backups (nobu/persist.py). The running server
    re-reads the file when it changed on disk (`python -m nobu.auth add`, `nobu.admin restore`)."""

    def __init__(self, path):
        self.path = path
        self.data = persist.load_json(path, default=lambda: {"accounts": {}}, validate=valid_accounts)
        self._stat = self._disk_stat()
        # The website's account API (gamelink.py) runs in a thread beside the login loop.
        self.lock = threading.RLock()

    def _disk_stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def reload_if_changed(self):
        """Pick up an edit made by another process; a file that does not parse is ignored (loud)."""
        now = self._disk_stat()
        if now is None or now == self._stat:
            return False
        self._stat = now
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            persist.loud(f"{self.path} changed on disk but does not parse ({e!r}); keeping the loaded accounts")
            return False
        if not valid_accounts(data):
            persist.loud(f"{self.path} changed on disk but has no accounts table; keeping the loaded accounts")
            return False
        self.data = data
        return True

    def save(self):
        text = persist.atomic_write_json(self.path, self.data, indent=1, sort_keys=True, ensure_ascii=True)
        self._stat = self._disk_stat()
        try:
            persist.backup_text(self.path, text, tag="auto")
        except OSError as e:
            persist.loud(f"backup of {self.path} failed: {e!r}")

    def add(self, cid, password):
        """Create or reset an account. The password is stored as a PBKDF2 hash."""
        if not ID_RE.match(cid):
            raise ValueError("citizen ID must be 1-8 lowercase letters (client check 0x28f6c0)")
        with self.lock:
            return self._add(cid, password)

    def _add(self, cid, password):
        accts = self.data["accounts"]
        uid = accts[cid]["uid"] if cid in accts else uid_for(len(accts) + 1)
        accts[cid] = {"password": hash_password(password), "uid": uid}
        self.save()
        return accts[cid]

    def check(self, cid, password, open_mode=False, legacy_password=None):
        """Return (error_code, account). error_code 0 = ok.

        legacy_password: the same password decoded the old way; accepted for an account stored
        before normalisation, which is then re-hashed with `password`.
        """
        with self.lock:
            return self._check(cid, password, open_mode, legacy_password)

    def _check(self, cid, password, open_mode, legacy_password):
        self.reload_if_changed()
        if not cid or not ID_RE.match(cid):
            return ERR_BAD_ID, None
        acct = self.data["accounts"].get(cid)
        if acct is None:
            if not open_mode:
                return ERR_NO_SUCH_ID, None
            if open_mode == "claim" and not password:
                return ERR_AUTH_FAILED, None
            acct = self.add(cid, password or "")
        elif open_mode is not True and not check_password(acct["password"], password or ""):
            if (legacy_password and legacy_password != password
                    and check_password(acct["password"], legacy_password)):
                acct["password"] = hash_password(password or "")
                self.save()
            else:
                return ERR_WRONG_PASSWORD, None
        return 0, acct


PBKDF2_ITER = 200_000


def hash_password(password):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${dk.hex()}"


def check_password(stored, password):
    """PBKDF2 entries, or a plain-text password from an older auth.json."""
    if stored.startswith("pbkdf2_sha256$"):
        _, it, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), want)
    return hmac.compare_digest(stored.encode("utf-8"), password.encode("utf-8"))


def answer(query, store, open_mode=False, now=None):
    """(body, note) for a request query string."""
    qs = urllib.parse.parse_qs(query, keep_blank_values=True)
    if "a" not in qs:
        return error_body(ERR_AUTH_FAILED), "no a= parameter"
    req = parse_a(qs["a"][0])
    err, acct = store.check(req["id"], req["password"], open_mode, req["password_legacy"])
    if err:
        return error_body(err), f"id={req['id']!r} -> error {err}"
    uid = acct["uid"]
    name = handle_name(uid) or req["id"]
    body = success_body([dict(uid=uid, name=name)], now=now)
    return body, f"id={req['id']!r} uid={uid:#x} handle={name} titles={[hex(t) for t in req['titles']]}"


# ---------------------------------------------------------------------------- website

def _link_id(req):
    return unicodedata.normalize("NFKC", str(req.get("account", ""))).strip().lower()


def link_verify(store, req):
    """The OpenLobby website links a citizen ID after checking its password here."""
    cid = _link_id(req)
    with store.lock:
        store.reload_if_changed()
        acct = store.data["accounts"].get(cid)
        if acct is None:
            return {"ok": False, "error": "no_account"}
        if not check_password(acct["password"], str(req.get("password", ""))):
            return {"ok": False, "error": "bad_password"}
    return {"ok": True, "account": cid, "key": cid}


def link_set_password(store, req):
    """A new password for an existing citizen ID: 4 to 32 printable ASCII, no spaces (the client's
    password buffer is 0x21 bytes)."""
    pw = str(req.get("password", ""))
    if not (4 <= len(pw) <= 32 and pw.isascii() and pw.isprintable() and " " not in pw):
        return {"ok": False, "error": "bad_new_password"}
    cid = _link_id(req)
    with store.lock:
        store.reload_if_changed()
        if cid not in store.data["accounts"]:
            return {"ok": False, "error": "no_account"}
        store.add(cid, pw)
    return {"ok": True, "account": cid, "key": cid}


# ---------------------------------------------------------------------------- server

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def log(**kw):
    kw["t"] = now_utc().isoformat()
    line = json.dumps(kw, ensure_ascii=False)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    with open(os.path.join(LOG_DIR, "auth.log"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


async def session(reader, writer, store):
    peer = writer.get_extra_info("peername") or ("?", 0)
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as e:
        log(ev="auth_bad_request", peer=peer[0], err=repr(e))
        writer.close()
        return
    text = head.decode("latin-1")
    first = text.split("\r\n", 1)[0]
    parts = first.split(" ")
    target = parts[1] if len(parts) > 1 else ""
    query = urllib.parse.urlsplit(target).query
    body, note = answer(query, store, OPEN)
    resp = http_response(body)
    writer.write(resp)
    await writer.drain()
    log(ev="auth", svc="auth", peer=peer[0], request=redact(text), note=note,
        response=body.decode("ascii", "replace"))
    writer.close()


async def serve():
    store = AccountStore(default_db())
    srv = await asyncio.start_server(lambda r, w: session(r, w, store), BIND, PORT)
    gamelink.serve({"/v1/verify": lambda req: link_verify(store, req),
                    "/v1/set-password": lambda req: link_set_password(store, req)},
                   int(os.environ.get("NOBU_LINK_PORT", "18081")),
                   log=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"auth: listening on {BIND}:{PORT}, db {store.path}, open={OPEN}, logs in {LOG_DIR}",
          file=sys.stderr, flush=True)
    stop = asyncio.Event()
    persist.on_shutdown_signals(stop.set)          # docker stop: finish, no half-written auth.json
    try:
        await stop.wait()
    finally:
        srv.close()
        print("auth: shutting down", file=sys.stderr, flush=True)


def main(argv):
    if len(argv) >= 1 and argv[0] == "add" and len(argv) == 3:
        store = AccountStore(default_db())
        acct = store.add(argv[1], argv[2])
        print(f"{argv[1]}: uid {acct['uid']:#x} handle {handle_name(acct['uid'])} -> {store.path}")
    elif argv and argv[0] == "list":
        store = AccountStore(default_db())
        for cid, a in sorted(store.data["accounts"].items()):
            print(f"{cid:8} uid {a['uid']:#x} handle {handle_name(a['uid'])}")
    elif not argv:
        asyncio.run(serve())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
