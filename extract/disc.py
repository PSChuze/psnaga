"""Read the files the server needs from the Hiryuu no Shou disc.

The disc's game data sits in one archive, INSTIMG/NOBUON.LWZ, which the PS2 installer
copies to the hard disk. A disc is accepted as an ISO image or as a folder with the disc's
files in it.

LWZ layout (big-endian unless noted):
  archive header  16 bytes: "LA" u16 header length (16) and three u32
  entry           "LF" u16 flags | path length (bit 15 = stored), u64 x2 file times, u32 data length,
                  path (cp932, relative to the build tree, e.g. ..\\patch\\recentver\\data\\STRDAT.BIN),
                  data: "BILZ" u32le size u32le packed size + zlib stream, or the file as it is
"""
import io
import os
import struct
import zipfile
import zlib

SECTOR = 2048
# Hiryuu no Shou (SLPM-65197, the expansion pressing): the launcher this disc boots.
BOOT2 = "SLPM_657.83"
LWZ_PATH = "INSTIMG/NOBUON.LWZ"


class DiscError(Exception):
    pass


class _Window(io.RawIOBase):
    """A read-only view of `size` bytes of `f` from `start`."""

    def __init__(self, f, start, size):
        self.f, self.start, self.size, self.pos = f, start, size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, pos, whence=0):
        self.pos = pos if whence == 0 else self.pos + pos if whence == 1 else self.size + pos
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, buf):
        n = max(0, min(len(buf), self.size - self.pos))
        self.f.seek(self.start + self.pos)
        data = self.f.read(n)
        buf[:len(data)] = data
        self.pos += len(data)
        return len(data)


def iso_files(f):
    """{path: (offset, size)} of every file on an ISO 9660 image, paths upper case with '/'."""
    f.seek(16 * SECTOR)
    pvd = f.read(SECTOR)
    if pvd[1:6] != b"CD001":
        raise DiscError("not an ISO 9660 disc image")
    root = pvd[156:190]
    out = {}
    stack = [(struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0], "")]
    while stack:
        lba, size, path = stack.pop()
        f.seek(lba * SECTOR)
        d = f.read(size)
        i = 0
        while i < len(d):
            n = d[i]
            if n == 0:
                i = (i // SECTOR + 1) * SECTOR
                continue
            r = d[i:i + n]
            i += n
            ext, sz = struct.unpack("<I", r[2:6])[0], struct.unpack("<I", r[10:14])[0]
            name = r[33:33 + r[32]]
            if name in (b"\0", b"\1"):
                continue
            full = (path + "/" + name.decode("latin1").split(";")[0]).lstrip("/").upper()
            if r[25] & 2:
                stack.append((ext, sz, full))
            else:
                out[full] = (ext * SECTOR, sz)
    return out


class Disc:
    """One Hiryuu no Shou disc: .files() opens a disc file by its path (e.g. "SYSTEM.CNF")."""

    def __init__(self, path):
        self.path = path
        self._f = None
        if os.path.isdir(path):
            self.kind = "folder"
            return
        if zipfile.is_zipfile(path):
            raise DiscError("%s is a zip: unpack the disc image from it first" % path)
        self._f = open(path, "rb")
        self.kind = "iso"
        self._index = iso_files(self._f)

    def open(self, name):
        """A file object for disc path `name` (any case, '/' separated)."""
        key = name.replace("\\", "/").upper()
        if self.kind == "folder":
            for base, dirs, files in os.walk(self.path):
                rel = os.path.relpath(base, self.path).replace("\\", "/")
                for fn in files:
                    full = (fn if rel == "." else rel + "/" + fn).upper()
                    if full == key:
                        return open(os.path.join(base, fn), "rb")
            raise DiscError("%s is not in %s" % (name, self.path))
        if key not in self._index:
            raise DiscError("%s is not on the disc" % name)
        off, size = self._index[key]
        return io.BufferedReader(_Window(self._f, off, size), 1 << 16)

    def check(self):
        """Raise DiscError unless this is the Hiryuu no Shou disc."""
        try:
            with self.open("SYSTEM.CNF") as f:
                cnf = f.read(512).decode("latin1")
        except DiscError:
            raise DiscError("%s is not a PS2 disc (no SYSTEM.CNF)" % self.path)
        if BOOT2 not in cnf.upper():
            raise DiscError("%s is not Nobunaga no Yabou Online - Hiryuu no Shou (SLPM-65197, boots %s); "
                            "SYSTEM.CNF says: %s" % (self.path, BOOT2, " ".join(cnf.split())))

    def close(self):
        if self._f:
            self._f.close()


def lwz_entries(f):
    """(path, data offset, data length) of every LWZ entry; paths with '/' and no leading '../'."""
    f.seek(0)
    head = f.read(16)
    if head[:2] != b"LA":
        raise DiscError("NOBUON.LWZ: not an LWZ archive")
    p = struct.unpack(">H", head[2:4])[0]
    while True:
        f.seek(p)
        h = f.read(24)
        if len(h) < 24 or h[:2] != b"LF":
            return
        plen = struct.unpack(">H", h[2:4])[0] & 0x7FFF
        dlen = struct.unpack(">I", h[20:24])[0]
        name = f.read(plen).decode("cp932", "backslashreplace").replace("\\", "/")
        parts = [x for x in name.split("/") if x not in ("..", ".", "")]
        yield "/".join(parts), p + 24 + plen, dlen
        p += 24 + plen + dlen


def lwz_read(f, off, dlen):
    f.seek(off)
    data = f.read(dlen)
    if data[:4] == b"BILZ":
        usize, csize = struct.unpack("<II", data[4:12])
        out = zlib.decompress(data[12:12 + csize])
        if len(out) != usize:
            raise DiscError("NOBUON.LWZ: an entry unpacks to the wrong size")
        return out
    return data


def game_files(disc, wanted):
    """{name: bytes} for each wanted path suffix (e.g. "data/STRDAT.BIN") found in NOBUON.LWZ."""
    f = disc.open(LWZ_PATH)
    got = {}
    want = {w.lower(): w for w in wanted}
    for name, off, dlen in lwz_entries(f):
        low = name.lower()
        for key, w in want.items():
            if low.endswith(key) and w not in got:
                got[w] = lwz_read(f, off, dlen)
    missing = [w for w in wanted if w not in got]
    if missing:
        raise DiscError("NOBUON.LWZ lacks %s" % ", ".join(missing))
    return got
