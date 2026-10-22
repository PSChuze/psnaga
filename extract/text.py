"""The game's name tables, read from STRDAT.BIN and WDMMSG.BIN, joined with our English overlay.

STRDAT.BIN: u32 section count (10), then per section {u32 offset, u32 count, u32 width}; the
offset counts from the end of that header. Records are `width` bytes of cp932, NUL padded.
Section 0 (item names) is scrambled: every non-zero byte goes through a bit permutation picked
by its index in the record modulo 3.

WDMMSG.BIN: "CSTR" string table blocks, each {"CSTR", u32 groups, u32 strings, u32 pool size,
24 more header bytes, groups x {u16 first, u16 count}, strings x u32 pool offset, pool of NUL
terminated cp932}, padded to 0x200. A message id is block.group.index.

The tables are written as <out>/<table>.tsv with the columns id, max, jp, en, note: max is the
byte limit of the slot, jp the disc's text and en our English from overlay/text/<table>.tsv.
"""
import csv
import os
import struct

STRDAT_TABLES = ["strdat_00_items", "strdat_01_skills", "strdat_02_skillbooks", "strdat_03_itemtypes",
                 "strdat_04_provinces", "strdat_05_factions", "strdat_06_traits", "strdat_07_traitdesc",
                 "strdat_08_schools", "strdat_09_creatures"]
# What the server reads; the rest of the disc's text stays on the disc.
SERVER_TABLES = ("strdat_00_items", "strdat_01_skills", "strdat_02_skillbooks", "strdat_04_provinces",
                 "strdat_05_factions", "strdat_06_traits", "strdat_08_schools", "wdmmsg")

# Section 0's scramble, per byte index % 3: bit i of the plain byte is bit P[i] of the stored one.
_PERM = [(6, 4, 0, 2, 7, 3, 1, 5), (2, 1, 6, 5, 4, 0, 3, 7), (3, 5, 7, 0, 1, 6, 4, 2)]


def _table(p):
    t = bytearray(256)
    for v in range(256):
        o = 0
        for i in range(8):
            if v >> i & 1:
                o |= 1 << p[i]
        t[v] = o
    return bytes(t)


_DEC = [_table(p) for p in _PERM]


def esc(s):
    """Backslash escapes for the tsv: \\\\, \\n, \\t and \\xNN for other control characters."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7f:
            out.append("\\x%02x" % o)
        else:
            out.append(ch)
    return "".join(out)


def strdat(data):
    """{table name: [(id, max, text)]} of STRDAT.BIN."""
    n = struct.unpack_from("<I", data, 0)[0]
    base = 4 + 12 * n
    out = {}
    for i in range(n):
        off, count, width = struct.unpack_from("<III", data, 4 + 12 * i)
        rows = []
        for j in range(count):
            rec = data[base + off + j * width: base + off + (j + 1) * width]
            if i == 0:
                rec = bytes(_DEC[k % 3][b] for k, b in enumerate(rec))
            if not any(rec):
                continue
            text = rec.split(b"\0")[0]
            rows.append(("%d.%d" % (i, j), width if i == 0 else width - 1, text.decode("cp932")))
        out[STRDAT_TABLES[i] if i < len(STRDAT_TABLES) else "strdat_%02d" % i] = rows
    return out


def cstr(data):
    """[(id, max, text)] of a CSTR string table (WDMMSG.BIN)."""
    rows = []
    o = b = 0
    while o < len(data) and data[o:o + 4] == b"CSTR":
        _, ng, ns, ps = struct.unpack_from("<4sIII", data, o)
        groups = [struct.unpack_from("<HH", data, o + 0x30 + 4 * i) for i in range(ng)]
        offs = struct.unpack_from("<%dI" % ns, data, o + 0x30 + 4 * ng)
        pool_at = o + 0x30 + 4 * ng + 4 * ns
        pool = data[pool_at:pool_at + ps]
        for g, (first, count) in enumerate(groups):
            for k in range(count):
                x = offs[first + k]
                s = pool[x:pool.index(b"\0", x)]
                rows.append(("%d.%d.%d" % (b, g, k), len(s), s.decode("cp932")))
        o = (pool_at + ps + 0x1ff) & ~0x1ff
        b += 1
    return rows


def read_overlay(path):
    """{id: English} of an overlay table; {} when there is none."""
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row.get("id") and row.get("en"):
                    out[row["id"]] = row["en"]
    return out


def write_table(path, rows, english):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id\tmax\tjp\ten\tnote\n")
        for rid, limit, jp in rows:
            fh.write("%s\t%d\t%s\t%s\t\n" % (rid, limit, esc(jp), english.get(rid, "")))


def build(files, overlay_dir, out_dir):
    """Write the server's tables into out_dir; returns {table: rows written}."""
    tables = strdat(files["data/STRDAT.BIN"])
    tables["wdmmsg"] = cstr(files["data/ui/WDMMSG.BIN"])
    os.makedirs(out_dir, exist_ok=True)
    done = {}
    for name in SERVER_TABLES:
        english = read_overlay(os.path.join(overlay_dir, name + ".tsv"))
        write_table(os.path.join(out_dir, name + ".tsv"), tables[name], english)
        done[name] = len(tables[name])
    return done
