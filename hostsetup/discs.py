"""Find the host's Nobunaga no Yabou Online - Hiryuu no Shou disc image: in the usual folders,
in PCSX2's game folders, or where they point us.

A disc is told apart by its SYSTEM.CNF (it boots SLPM_657.83), so file names do not matter.
Zips are looked into but not unpacked here (setup.py unpacks the one it uses).
"""
import os
import sys
import zipfile

from extract import disc as disclib

IMAGE_EXT = (".iso",)
ZIP_EXT = (".zip",)
MIN_IMAGE = 500 << 20       # smaller files are not this DVD
SKIP_DIRS = {"node_modules", "appdata", "library", "$recycle.bin", "windows", "program files",
             "program files (x86)", "system volume information", ".git", ".cache", "__pycache__"}


class Disc:
    """One disc found: path (the image, or the zip holding it) and member (the file inside the
    zip, or None)."""

    def __init__(self, path, member=None):
        self.path, self.member = path, member

    def __repr__(self):
        return "Disc(%r%s)" % (self.path, ", %r" % self.member if self.member else "")

    def __eq__(self, other):
        return (self.path, self.member) == (other.path, other.member)

    def __hash__(self):
        return hash((self.path, self.member))

    def shown(self):
        return "%s (inside %s)" % (self.member, self.path) if self.member else self.path


def is_hiryuu(path):
    """True when the .iso at path is the Hiryuu no Shou disc."""
    try:
        d = disclib.Disc(path)
        try:
            d.check()
            return True
        finally:
            d.close()
    except (disclib.DiscError, OSError, ValueError, KeyError):
        return False


def _zip_has_hiryuu(path):
    """Names of the .iso members of a zip that look like this disc (by size: a zip member cannot
    be read quickly enough to open its SYSTEM.CNF)."""
    try:
        with zipfile.ZipFile(path) as z:
            return [i.filename for i in z.infolist()
                    if i.filename.lower().endswith(IMAGE_EXT) and i.file_size >= MIN_IMAGE
                    and any(w in i.filename.lower() for w in ("nobunaga", "hiryu", "slpm-65197", "slpm_657"))]
    except (OSError, zipfile.BadZipFile):
        return []


def identify(path):
    """The discs in one file or folder: [Disc] (empty when it holds none)."""
    low = path.lower()
    if os.path.isdir(path):
        if os.path.exists(os.path.join(path, "SYSTEM.CNF")) and is_hiryuu(path):
            return [Disc(path)]
        return []
    if low.endswith(IMAGE_EXT):
        return [Disc(path)] if is_hiryuu(path) else []
    if low.endswith(ZIP_EXT):
        return [Disc(path, m) for m in _zip_has_hiryuu(path)]
    return []


def scan(folder, depth=3, limit=20000):
    """Every disc under folder, looking `depth` folders down and at most `limit` entries."""
    found, seen = [], 0
    stack = [(folder, 0)]
    while stack and seen < limit:
        d, level = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in sorted(entries, key=lambda e: e.name.lower()):
            seen += 1
            try:
                if e.is_dir(follow_symlinks=False):
                    if level < depth and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                        stack.append((e.path, level + 1))
                elif e.is_file() and e.name.lower().endswith(IMAGE_EXT + ZIP_EXT) \
                        and e.stat().st_size >= MIN_IMAGE // 2:
                    found += identify(e.path)
            except OSError:
                continue
    return found


def pcsx2_game_folders(home=None, platform=None, env=None):
    """The game folders PCSX2 lists in its PCSX2.ini (RecursivePaths / Paths of [GameList])."""
    home = home or os.path.expanduser("~")
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform.startswith("win"):
        inis = [os.path.join(home, "Documents", "PCSX2", "inis", "PCSX2.ini")]
    elif platform == "darwin":
        inis = [os.path.join(home, "Library", "Application Support", "PCSX2", "inis", "PCSX2.ini")]
    else:
        config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        inis = [os.path.join(config, "PCSX2", "inis", "PCSX2.ini")]
    out = []
    for ini in inis:
        try:
            with open(ini, encoding="utf-8", errors="replace") as f:
                section = ""
                for line in f:
                    line = line.strip()
                    if line.startswith("["):
                        section = line
                    elif section == "[GameList]" and "=" in line:
                        key, value = (x.strip() for x in line.split("=", 1))
                        if key in ("RecursivePaths", "Paths") and value:
                            out.append(value)
        except OSError:
            pass
    return out


def likely_folders(extra=(), home=None, platform=None, env=None):
    """Where discs usually are: the given folders first, then Downloads, Documents, Desktop and
    PCSX2's game folders. Only folders that exist."""
    home = home or os.path.expanduser("~")
    folders = list(extra) + [os.path.join(home, n) for n in ("Downloads", "Documents", "Desktop", "Games")]
    folders += pcsx2_game_folders(home, platform, env)
    out, seen = [], set()
    for d in folders:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def search(folders, depth=3):
    """Scan the folders in order; each disc file is listed once."""
    found, seen = [], set()
    for d in folders:
        for disc in scan(d, depth):
            key = (os.path.normcase(os.path.abspath(disc.path)), disc.member)
            if key not in seen:
                seen.add(key)
                found.append(disc)
    return found


def pick(found):
    """The disc to use: a plain image before a zip (no unpacking needed), else the first found."""
    if not found:
        return None
    return sorted(found, key=lambda d: d.member is not None)[0]
