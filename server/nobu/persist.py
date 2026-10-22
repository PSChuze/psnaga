"""Crash-safe JSON persistence for $NOBU_DATA: atomic writes, rotating backups, corruption recovery.

Every server-owned JSON file (chars.json, auth.json and the seeded enemies / guild / crafting /
shops / news files) is written through atomic_write_json: the text is serialized first, written
to a temp file in the same directory, fsynced, then os.replace()d over the target (and the
directory fsynced on POSIX). A crash or kill -9 at any point leaves either the old file or the
new one, never a torn mix.

The server is one asyncio process with no threads or executors, so writes of one store cannot
interleave inside a process. Across processes: the world server holds an advisory lock on
chars.json.lock (hold_lock) that `python -m nobu.admin restore` checks before touching the file.

Backups: $NOBU_BACKUP_DIR (default <data dir>/backups) keeps timestamped copies named
<file>.<UTC stamp>.<tag> (tag = boot / auto / manual / pre-restore / recovered). A backup is
taken when a store loads, then at most every NOBU_BACKUP_SECS on save; identical content is not
backed up twice in a row. The newest NOBU_BACKUP_KEEP per file are kept (0 = no backups).

Loading (load_json): a file that fails to parse is moved aside as <file>.corrupt-<stamp>, the
newest backup that parses is used instead (and written back), all with loud log lines. When no
backup parses, PersistError is raised: the server never silently starts from empty data.

Env:
  NOBU_BACKUP_DIR    backup directory (default: backups/ next to the data file)
  NOBU_BACKUP_KEEP   backups kept per file (default 48; 0 disables backups)
  NOBU_BACKUP_SECS   minimum seconds between automatic backups on save (default 3600)
  NOBU_SAVE_DELAY    seconds a deferred (save_soon) character save may wait (default 5)
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time


class PersistError(RuntimeError):
    """A data file is unreadable and no backup could replace it."""


# ---------------------------------------------------------------------------- knobs
def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def backup_keep():
    return int(_num("NOBU_BACKUP_KEEP", "48"))


def backup_secs():
    return _num("NOBU_BACKUP_SECS", "3600")


def save_delay():
    return max(0.0, _num("NOBU_SAVE_DELAY", "5"))


def backup_dir(path):
    return os.environ.get("NOBU_BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(path)), "backups")


# ---------------------------------------------------------------------------- logging
def loud(msg, **kw):
    """A log line that stands out on stderr (docker logs) and in events.jsonl when a log dir is set."""
    line = f"!!! persist: {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr, flush=True)
    log_dir = os.environ.get("NOBU_LOG_DIR")
    if log_dir:
        try:
            rec = dict(t=dt.datetime.now(dt.timezone.utc).isoformat(), ev="persist", msg=msg, **kw)
            with open(os.path.join(log_dir, "events.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


_WARNED = set()


def warn_once(key, msg):
    """loud() once per key (e.g. (path, mtime) of a corrupt seeded file that is read on every request)."""
    if key not in _WARNED:
        _WARNED.add(key)
        loud(msg)


# ---------------------------------------------------------------------------- atomic write
def _fsync_dir(d):
    if os.name != "posix":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace(src, dst):
    for i in range(20):                      # Windows: a reader holding dst open makes replace fail briefly
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or i == 19:
                raise
            time.sleep(0.05)


def atomic_write_text(path, text):
    """Write text to path atomically (temp file in the same dir, fsync, os.replace, dir fsync).
    Keeps the permission bits of an existing file."""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def dumps(obj, **kw):
    kw.setdefault("ensure_ascii", False)
    kw.setdefault("indent", 1)
    return json.dumps(obj, **kw)


def atomic_write_json(path, obj, **kw):
    """Serialize first (a serialization error never touches the file), then atomic_write_text."""
    text = dumps(obj, **kw)
    atomic_write_text(path, text)
    return text


# ---------------------------------------------------------------------------- backups
def _stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def list_backups(path):
    """[(full path, stamp, tag), ...] for path's backups, newest first."""
    d, base = backup_dir(path), os.path.basename(path)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.startswith(base + ".") or n.endswith(".tmp"):
            continue
        rest = n[len(base) + 1:].split(".")
        if len(rest) != 2 or not rest[0].endswith("Z"):
            continue
        out.append((os.path.join(d, n), rest[0], rest[1]))
    out.sort(key=lambda b: b[1], reverse=True)
    return out


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def backup_text(path, text, tag="auto", keep=None, dedupe=True):
    """Store text as a new backup of path and prune to the newest `keep`. Returns the backup path
    (None when backups are off or the newest backup already has this content)."""
    keep = backup_keep() if keep is None else keep
    if keep <= 0:
        return None
    have = list_backups(path)
    if dedupe and have:
        try:
            if _read(have[0][0]) == text:
                return None
        except OSError:
            pass
    dst = os.path.join(backup_dir(path), f"{os.path.basename(path)}.{_stamp()}.{tag}")
    atomic_write_text(dst, text)
    for old, _, _ in list_backups(path)[keep:]:
        try:
            os.unlink(old)
        except OSError:
            pass
    return dst


def backup_file(path, tag="manual", keep=None, dedupe=False):
    """Back up the file as it is on disk; it must parse as JSON (a corrupt file is not a backup)."""
    text = _read(path)
    json.loads(text)
    return backup_text(path, text, tag=tag, keep=keep, dedupe=dedupe)


# ---------------------------------------------------------------------------- load with recovery
def load_json(path, default=None, validate=None, backup_tag="boot"):
    """Parse path. Missing file: newest good backup if there is one (loud), else default().
    Unparseable / invalid file: moved to <path>.corrupt-<stamp>, newest good backup used and written
    back (loud). Nothing usable: PersistError. A good load is backed up once (tag `backup_tag`)."""
    def ok(obj):
        return validate is None or validate(obj)

    problem = None
    if os.path.exists(path):
        try:
            text = _read(path)
            obj = json.loads(text)
            if ok(obj):
                if backup_tag:
                    try:
                        backup_text(path, text, tag=backup_tag)
                    except OSError as e:
                        loud(f"cannot back up {path}: {e!r}")
                return obj
            problem = "does not have the expected structure"
        except (OSError, ValueError) as e:            # JSONDecodeError and UnicodeDecodeError are ValueErrors
            problem = repr(e)
        aside = f"{path}.corrupt-{_stamp()}"
        loud(f"{path} is unreadable ({problem}); moving it to {aside}", path=path, error=problem)
        try:
            os.replace(path, aside)
        except OSError as e:
            loud(f"cannot move {path} aside: {e!r}")
    for bpath, stamp, tag in list_backups(path):
        try:
            text = _read(bpath)
            obj = json.loads(text)
        except (OSError, ValueError) as e:
            loud(f"backup {bpath} is unreadable too ({e!r}); trying an older one")
            continue
        if not ok(obj):
            loud(f"backup {bpath} does not have the expected structure; trying an older one")
            continue
        loud(f"RECOVERED {path} from backup {bpath} ({stamp}); changes after that backup are lost",
             path=path, backup=bpath)
        atomic_write_text(path, text)
        return obj
    if problem is not None:
        raise PersistError(f"{path} is unreadable ({problem}) and no usable backup exists in "
                           f"{backup_dir(path)}; refusing to start from empty data")
    return default() if callable(default) else default


# ---------------------------------------------------------------------------- process lock
class FileLock:
    """Advisory, non-blocking, exclusive lock on <path>.lock (fcntl.flock on POSIX, msvcrt on Windows).
    Works across containers that bind-mount the same data directory."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self):
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def is_locked(path):
    """True when another process holds path's FileLock (e.g. the world server owns chars.json)."""
    lk = FileLock(path)
    if lk.acquire():
        lk.release()
        return False
    return True


# ---------------------------------------------------------------------------- shutdown signals
def on_shutdown_signals(callback):
    """Call callback() on SIGTERM (docker stop) / SIGINT inside the running loop. Where the loop
    cannot take signal handlers (Windows), Ctrl-C still cancels asyncio.run's main task."""
    import asyncio
    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


def restore(backup_path, target):
    """Replace target with a backup (which must parse); the current target is backed up first."""
    text = _read(backup_path)
    json.loads(text)
    pre = None
    if os.path.exists(target):
        cur = _read(target)
        try:
            json.loads(cur)
            good = True
        except ValueError:
            good = False
        if good and backup_keep() > 0:
            pre = backup_text(target, cur, tag="pre-restore", dedupe=False)
        else:                                       # keep a corrupt file out of the rotation
            pre = f"{target}.{'pre-restore' if good else 'corrupt'}-{_stamp()}"
            shutil.copyfile(target, pre)
    atomic_write_text(target, text)
    return pre
