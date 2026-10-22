"""Data admin CLI for $NOBU_DATA (backups, restore, validation). Stdlib only.

  python -m nobu.admin backup [chars|auth|all]     back the live file(s) up now (tag "manual")
  python -m nobu.admin list [chars|auth|all]       list backups, newest first, with a parse check
  python -m nobu.admin restore <backup> [--force]  put a backup back (name from `list`, or a path)
  python -m nobu.admin validate                    check every data file; exit 1 on a problem.
                                                   Strictly read-only: it never seeds, recovers or
                                                   rewrites anything (a fresh /data stays empty)

In the docker deploy run it inside the world container (same /data mount and env):
  docker compose -p nobu -f server/compose/nobu.yml exec world python -m nobu.admin list

restore:
  - The backup must parse and have the right shape. The current file is backed up first
    (tag "pre-restore"), then replaced atomically.
  - chars.json is owned by the running world server, which would overwrite a restore on its next
    save: restore refuses while the world server holds chars.json.lock. Stop the world service,
    restore with `docker compose run --rm world python -m nobu.admin restore ...`, start it again.
    --force skips that check (only when you know the lock is stale).
  - auth.json: the running auth server re-reads the file when it changes, no stop needed.

Paths follow the servers: NOBU_CHAR_DB / NOBU_AUTH_DB, else $NOBU_DATA/{chars,auth}.json;
backups in NOBU_BACKUP_DIR (default $NOBU_DATA/backups), NOBU_BACKUP_KEEP per file (default 48).
"""
import json
import os
import sys

from . import auth, chars, persist


def targets():
    return {"chars": chars.default_path(), "auth": auth.default_db()}


def _which(arg):
    t = targets()
    if arg in (None, "all"):
        return list(t.items())
    if arg not in t:
        raise SystemExit(f"unknown file {arg!r}: use chars, auth or all")
    return [(arg, t[arg])]


# ---------------------------------------------------------------------------- validation
def check_chars(data):
    """(errors, warnings) for a chars.json document."""
    errs, warns = [], []
    if not chars.valid_data(data):
        return ['not {"next_cid": int, "chars": {uid: [char, ...]}}'], warns
    seen = {}
    for uid, cs in data["chars"].items():
        slots = set()
        for c in cs:
            cid = c.get("cid")
            if not isinstance(cid, int):
                errs.append(f"account {uid}: a character without an integer cid")
                continue
            if cid in seen:
                errs.append(f"cid {cid} appears twice (accounts {seen[cid]} and {uid})")
            seen[cid] = uid
            if str(c.get("uid")) != str(uid):
                warns.append(f"cid {cid}: uid {c.get('uid')} differs from its account key {uid}")
            if c.get("slot") in slots:
                warns.append(f"account {uid}: two characters in slot {c.get('slot')}")
            slots.add(c.get("slot"))
    if seen and data["next_cid"] <= max(seen):
        errs.append(f"next_cid {data['next_cid']} <= highest cid {max(seen)} (a new character would reuse a cid)")
    return errs, warns


def check_auth(data):
    errs, warns = [], []
    if not auth.valid_accounts(data):
        return ['not {"accounts": {id: {"uid", "password"}}}'], warns
    seen = {}
    for cid, a in data["accounts"].items():
        if a["uid"] in seen:
            errs.append(f"uid {a['uid']:#x} used by {seen[a['uid']]} and {cid}")
        seen[a["uid"]] = cid
        if not str(a.get("password", "")).startswith("pbkdf2_sha256$"):
            warns.append(f"{cid}: password is not a pbkdf2 hash")
    return errs, warns


def _seeded():
    """(label, path, shape check) for the seeded / read-only files. Only the modules' path functions
    and pure shape checks are used, never their loaders (load_data / load_shops / load_news seed a
    missing file on first use)."""
    from . import crafting, enemies, guild, misc, shops
    war = os.environ.get("NOBU_WAR_DB") or os.path.join(misc.data_dir(), "war.json")
    return [
        ("enemies", enemies.data_path(), enemies._valid),
        ("guild", guild.data_path(), guild._valid),
        ("crafting", crafting.craft_path(), lambda d: isinstance(d, dict) and isinstance(d.get("recipes"), list)),
        ("shops", shops.shops_path(), lambda d: isinstance(d, dict) and isinstance(d.get("shops"), dict)),
        ("news", misc.news_path(), lambda d: isinstance(d, (list, dict))),
        ("war", war, lambda d: isinstance(d, dict)),
    ]


def _parse(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate(out=print):
    """Check every data file; returns the number of errors. Read-only: files are only opened for
    reading and directories only listed; missing files are reported, not created (exit 0 on a fresh
    install)."""
    n_err = 0
    checks = {"chars": check_chars, "auth": check_auth}
    for label, path in targets().items():
        if not os.path.exists(path):
            good = [b for b in persist.list_backups(path)]
            out(f"{label:9} MISSING  {path}" + (f" ({len(good)} backup(s) exist: the server recovers the newest)"
                                               if good else " (a fresh install starts empty)"))
            continue
        try:
            data = _parse(path)
        except (OSError, ValueError) as e:
            out(f"{label:9} ERROR    {path}: does not parse: {e}")
            n_err += 1
            continue
        errs, warns = checks[label](data)
        count = (sum(len(v) for v in data["chars"].values()) if label == "chars" and not errs else
                 len(data["accounts"]) if label == "auth" and not errs else 0)
        out(f"{label:9} {'ERROR' if errs else 'ok':8} {path} ({count} {'characters' if label == 'chars' else 'accounts'}, "
            f"{len(persist.list_backups(path))} backups)")
        for e in errs:
            out(f"          error: {e}")
        for w in warns:
            out(f"          warning: {w}")
        n_err += len(errs)
    for label, path, shape in _seeded():
        if not os.path.exists(path):
            out(f"{label:9} MISSING  {path} (seeded on first use)" if label != "war" else
                f"{label:9} MISSING  {path} (optional: built-in defaults, never written)")
            continue
        try:
            data = _parse(path)
        except (OSError, ValueError) as e:
            out(f"{label:9} ERROR    {path}: does not parse: {e} (the server falls back to its built-in data)")
            n_err += 1
            continue
        if not shape(data):
            out(f"{label:9} ERROR    {path}: unexpected structure (the server falls back to its built-in data)")
            n_err += 1
        else:
            out(f"{label:9} ok       {path}")
    for label, path in targets().items():
        d = os.path.dirname(os.path.abspath(path))
        try:
            left = sorted(n for n in os.listdir(d) if n.startswith(os.path.basename(path) + ".corrupt"))
        except OSError:
            left = []
        for n in left:
            out(f"{label:9} note     {os.path.join(d, n)} was set aside by a recovery; inspect, then delete")
    return n_err


# ---------------------------------------------------------------------------- commands
def cmd_backup(arg):
    for label, path in _which(arg):
        if not os.path.exists(path):
            print(f"{label}: {path} does not exist, nothing to back up")
            continue
        try:
            dst = persist.backup_file(path, tag="manual")
        except ValueError as e:
            print(f"{label}: {path} does not parse ({e}); not backed up")
            return 1
        print(f"{label}: {dst}" if dst else f"{label}: backups are off (NOBU_BACKUP_KEEP=0)")
    return 0


def cmd_list(arg):
    for label, path in _which(arg):
        bs = persist.list_backups(path)
        print(f"{label}: {path} -> {persist.backup_dir(path)} ({len(bs)} backups, keep {persist.backup_keep()})")
        for bpath, stamp, tag in bs:
            size, state = 0, "ok"
            try:
                size = os.path.getsize(bpath)
                _parse(bpath)
            except (OSError, ValueError):
                state = "UNREADABLE"
            print(f"  {os.path.basename(bpath):52} {tag:12} {size:>10} bytes  {state}")
    return 0


def _resolve_backup(name):
    """(backup path, label, target path) for a backup name from `list` or a path."""
    for label, path in targets().items():
        base = os.path.basename(path)
        cand = name if os.path.sep in name or "/" in name else os.path.join(persist.backup_dir(path), name)
        if os.path.basename(cand).startswith(base + ".") and os.path.exists(cand):
            return cand, label, path
    raise SystemExit(f"no backup {name!r} (see `python -m nobu.admin list`)")


def cmd_restore(name, force=False):
    bpath, label, target = _resolve_backup(name)
    try:
        data = _parse(bpath)
    except (OSError, ValueError) as e:
        print(f"{bpath} does not parse ({e}); not restored")
        return 1
    errs, _ = (check_chars if label == "chars" else check_auth)(data)
    if errs and not force:
        print(f"{bpath} fails validation: {errs[0]}; not restored (--force to restore anyway)")
        return 1
    if label == "chars" and persist.is_locked(target) and not force:
        print(f"the world server is running on {target} (chars.json.lock is held): it would overwrite the "
              "restore. Stop the world service first (or --force if the lock is stale).")
        return 1
    pre = persist.restore(bpath, target)
    print(f"{label}: restored {target} from {bpath}" + (f"; previous file kept as {pre}" if pre else ""))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    force = "--force" in rest
    rest = [a for a in rest if a != "--force"]
    if cmd == "backup" and len(rest) <= 1:
        return cmd_backup(rest[0] if rest else None)
    if cmd == "list" and len(rest) <= 1:
        return cmd_list(rest[0] if rest else None)
    if cmd == "restore" and len(rest) == 1:
        return cmd_restore(rest[0], force=force)
    if cmd == "validate" and not rest:
        n = validate()
        print(f"{n} error(s)")
        return 1 if n else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
