"""extract/ on a made-up disc folder: the version check, reading NOBUON.LWZ, the name tables and the
overlay join. (The real disc's output is checked against the server's own expectations by the
server tests run with NOBU_ASSETS pointing at it.)"""
import csv
import os
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from extract import disc, text   # noqa: E402

CNF = b"BOOT2 = cdrom0:\\SLPM_657.83;1\r\nVER = 1.00\r\n"


def strdat(sections):
    """STRDAT.BIN from [(width, [bytes])]; section 0 scrambled as on the disc."""
    enc = [bytes(t.index(v) for v in range(256)) for t in text._DEC]
    head, body = b"", b""
    for i, (width, recs) in enumerate(sections):
        data = b""
        for r in recs:
            r = r.ljust(width, b"\0")
            if i == 0:
                r = bytes(enc[k % 3][b] for k, b in enumerate(r))
            data += r
        head += struct.pack("<III", len(body), len(recs), width)
        body += data
    return struct.pack("<I", len(sections)) + head + body


def cstr(groups):
    """One CSTR block from [[bytes, ...], ...] (one list per group)."""
    strs = [s for g in groups for s in g]
    pool, offs = b"", []
    for s in strs:
        offs.append(len(pool))
        pool += s + b"\0"
    hdr = struct.pack("<4sIII", b"CSTR", len(groups), len(strs), len(pool)) + b"\0" * 32
    firsts, n = [], 0
    for g in groups:
        firsts.append(struct.pack("<HH", n, len(g)))
        n += len(g)
    blk = hdr + b"".join(firsts) + struct.pack("<%dI" % len(strs), *offs) + pool
    return blk + b"\0" * (-len(blk) % 0x200)


def lwz(files):
    """NOBUON.LWZ from {path: bytes}, every entry packed."""
    out = b"LA" + struct.pack(">H", 16) + b"\0" * 12
    for path, data in files.items():
        name = path.replace("/", "\\").encode("cp932")
        packed = b"BILZ" + struct.pack("<II", len(data), 0) + zlib.compress(data)
        packed = packed[:8] + struct.pack("<I", len(packed) - 12) + packed[12:]
        out += b"LF" + struct.pack(">HQQI", len(name), 0, 0, len(packed)) + name + packed
    return out


def disc_folder(boot=CNF, files=None):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "SYSTEM.CNF"), "wb") as f:
        f.write(boot)
    if files is not None:
        os.makedirs(os.path.join(d, "INSTIMG"))
        with open(os.path.join(d, "INSTIMG", "NOBUON.LWZ"), "wb") as f:
            f.write(lwz(files))
    return d


class Disc(unittest.TestCase):
    def test_only_hiryuu_no_shou(self):
        disc.Disc(disc_folder()).check()
        with self.assertRaises(disc.DiscError):
            disc.Disc(disc_folder(b"BOOT2 = cdrom0:\\SLPM_651.97;1\r\n")).check()
        with self.assertRaises(disc.DiscError):
            disc.Disc(tempfile.mkdtemp()).check()

    def test_lwz_entries(self):
        files = {"../patch/recentver/data/STRDAT.BIN": b"abc", "../patch/recentver/data/ui/WDMMSG.BIN": b"x" * 999}
        d = disc.Disc(disc_folder(files=files))
        got = disc.game_files(d, ["data/STRDAT.BIN", "data/ui/WDMMSG.BIN"])
        self.assertEqual(got, {"data/STRDAT.BIN": b"abc", "data/ui/WDMMSG.BIN": b"x" * 999})
        with self.assertRaises(disc.DiscError):
            disc.game_files(d, ["data/NOPE.BIN"])


class Text(unittest.TestCase):
    def test_tables(self):
        sections = [(16, [b"None", b"Kogatana", b"\x82\xa0" * 8])] + [(9, [b"None", b"Echigo"])] * 9
        tables = text.strdat(strdat(sections))
        self.assertEqual(tables["strdat_00_items"][1], ("0.1", 16, "Kogatana"))
        self.assertEqual(tables["strdat_00_items"][2][2], "あ" * 8)       # all 16 bytes text, no NUL
        self.assertEqual(tables["strdat_04_provinces"][1], ("4.1", 8, "Echigo"))
        rows = text.cstr(cstr([[b"a", b"bb"], [b"line\nnext"]]))
        self.assertEqual(rows, [("0.0.0", 1, "a"), ("0.0.1", 2, "bb"), ("0.1.0", 9, "line\nnext")])

    def test_build_joins_the_overlay(self):
        sections = [(16, [b"None", b"Kogatana"])] + [(9, [b"None"])] * 9
        files = {"data/STRDAT.BIN": strdat(sections), "data/ui/WDMMSG.BIN": cstr([[b"one\ttwo"]])}
        ov, out = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(ov, "strdat_00_items.tsv"), "w", encoding="utf-8", newline="\n") as f:
            f.write("id\ten\n0.1\tSmall Knife\n")
        done = text.build(files, ov, out)
        self.assertEqual(done["strdat_00_items"], 2)
        with open(os.path.join(out, "strdat_00_items.tsv"), encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        self.assertEqual([(r["id"], r["max"], r["jp"], r["en"]) for r in rows],
                         [("0.0", "16", "None", ""), ("0.1", "16", "Kogatana", "Small Knife")])
        with open(os.path.join(out, "wdmmsg.tsv"), encoding="utf-8") as f:
            self.assertIn("0.0.0\t7\tone\\ttwo\t\t", f.read())


if __name__ == "__main__":
    unittest.main()
