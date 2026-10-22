"""The province-to-province checkpoints of every province field, from the disc's CMAP files.

CMAP<id>.BIN: u32 0, 8 x u32 section offsets; section 0 is the header (u32 a, b, origin x, origin z, w, h,
max x, max z), section 1 "OLS0" and six {u32 count, u32 offset} tables:
  T1 20 B {u16 type, i16 link, f32 x, y, z, i16 angle, i16}   gate and door halves
  T2 20 B {u16, i16, f32 x, y, z, u16, i16}                   walkable points
  T4 40 B {u32 id, f32[9]}                                    triangles tagged with an area id (22x = map edge side)

Each province border has a group number N: the checkpoint is the T1 door pair with links N1 / N3, the border
marker the T1 row with link N0. The nearest 22x area gives the side (last digit 1 north, 2 south, 3 west,
4 east; x runs east, z south). Which neighbour a group leads to is not in the map files, so LINKS pairs the
groups by geography. An exit triggers at the door pair's midpoint (r 1500) and at the border marker (r 2500);
the arrival is the walkable point nearest the destination checkpoint.
"""
import math
import struct

from nobu.zones import PLACE_NAMES

SIDES = {1: "n", 2: "s", 3: "w", 4: "e"}
PROVINCES = range(1, 22)

# (map, group) <-> (map, group). 1 Echigo 2 Kozuke 3 Musashi 4 Kai 5 Shinano 6 Sagami 7 Suruga 8 Totomi
# 9 Mikawa 10 Owari 11 Mino 12 Etchu 13 Kaga 14 Echizen 15 Omi 16 Ise 17 Iga 18 Yamashiro 19 Settsu
# 20 Yamato 21 Kii.
LINKS = [
    ((1, 1), (2, 1)), ((1, 2), (5, 1)), ((1, 3), (12, 1)),
    ((2, 2), (3, 1)), ((2, 3), (5, 2)),
    ((3, 2), (4, 1)), ((3, 3), (6, 1)),
    ((4, 2), (5, 3)), ((4, 3), (7, 1)),
    ((5, 4), (8, 1)), ((5, 5), (11, 1)),
    ((6, 2), (7, 2)),
    ((7, 3), (8, 2)),
    ((8, 3), (9, 1)),
    ((9, 2), (10, 1)),
    ((10, 2), (11, 2)), ((10, 3), (16, 1)),
    ((11, 3), (14, 1)), ((11, 4), (15, 1)),
    ((12, 2), (13, 1)),
    ((13, 2), (14, 2)),
    ((14, 3), (15, 2)),
    ((15, 3), (16, 2)), ((15, 4), (18, 1)),
    ((16, 3), (17, 1)),
    ((17, 2), (20, 1)),
    ((18, 2), (19, 1)),
    ((19, 2), (20, 3)), ((19, 3), (21, 1)),
    ((20, 4), (21, 2)),
]


def cmap_name(m):
    return "data/worldmap/cmap%d/CMAP%03d.BIN" % (m // 100, m)


class Cmap:
    def __init__(self, data):
        self.d = data
        self.secs = struct.unpack_from("<8I", data, 4)
        o = self.secs[1]
        if data[o:o + 4] != b"OLS0":
            raise ValueError("CMAP section 1 is not OLS0")
        self.o = o
        self.tables = [struct.unpack_from("<II", data, o + 4 + 8 * i) for i in range(6)]

    def header(self):
        return struct.unpack_from("<8i", self.d, self.secs[0])

    def t1(self):
        n, off = self.tables[1]
        return [struct.unpack_from("<Hhfffhh", self.d, self.o + off + 20 * i) for i in range(n)]

    def t2(self):
        n, off = self.tables[2]
        return [struct.unpack_from("<fff", self.d, self.o + off + 20 * i + 4) for i in range(n)]

    def regions(self, lo=200, hi=400):
        """{id: [min x, max x, min z, max z]} of the T4 triangles with lo <= id < hi."""
        n, off = self.tables[4]
        out = {}
        for i in range(n):
            v = struct.unpack_from("<I9f", self.d, self.o + off + 40 * i)
            if not lo <= v[0] < hi:
                continue
            xs, zs = v[1::3], v[3::3]
            r = out.setdefault(v[0], [math.inf, -math.inf, math.inf, -math.inf])
            r[0], r[1] = min(r[0], *xs), max(r[1], *xs)
            r[2], r[3] = min(r[2], *zs), max(r[3], *zs)
        return out


def groups(cm):
    """{group: dict(cp=(x, y, z) midpoint, marker=(x, y, z) or None, side=letters)} of a province map."""
    regs = cm.regions()
    hx, hz = cm.header()[6:8]
    parts = {}
    for typ, link, x, y, z, ang, e in cm.t1():
        if link >= 10 and link % 10 in (0, 1, 3):
            parts.setdefault(link // 10, {})[link % 10] = (x, y, z)
    out = {}
    for g, d in sorted(parts.items()):
        if 1 not in d or 3 not in d:
            continue
        cp = tuple((a + b) / 2 for a, b in zip(d[1], d[3]))
        near = [r for r in regs if 220 <= r < 230]
        r = min(near, key=lambda r: math.hypot((regs[r][0] + regs[r][1]) / 2 - cp[0],
                                              (regs[r][2] + regs[r][3]) / 2 - cp[2]), default=None)
        side = SIDES.get(r % 10) if r else None
        if side is None:                          # no 22x area: name it by the compass point on the map
            side = ("n" if cp[2] < hz / 3 else "s" if cp[2] > 2 * hz / 3 else "") + \
                   ("w" if cp[0] < hx / 3 else "e" if cp[0] > 2 * hx / 3 else "")
        if any(o["side"] == side for o in out.values()):
            side += str(g)                        # a second gate on the same side (Shinano 3)
        out[g] = dict(cp=cp, marker=d.get(0), side=side)
    return out


def arrival(cm, cp):
    p = min(cm.t2(), key=lambda p: math.hypot(p[0] - cp[0], p[2] - cp[2]))
    return [round(p[0]), round(p[1]), round(p[2]), 0]


def province_gates(files):
    """The province_gates.json document from {cmap_name(m): bytes} of maps 1..21."""
    maps = {m: Cmap(files[cmap_name(m)]) for m in PROVINCES}
    gs = {m: groups(maps[m]) for m in PROVINCES}
    used = set()
    exits = []
    for a, b in LINKS:
        for (m, g), (to, tg) in ((a, b), (b, a)):
            if (m, g) in used:
                raise ValueError(f"map {m} group {g} is linked twice")
            used.add((m, g))
            src, dst = gs[m][g], gs[to][tg]
            key = PLACE_NAMES[m].lower()
            arrive = arrival(maps[to], dst["cp"])
            exits.append(dict(id=f"{key}-cp-{src['side']}", map=m, at=[round(src["cp"][0]), round(src["cp"][2])],
                              r=1500, to=to, arrive=arrive, kind="checkpoint", proven=False))
            if src["marker"]:
                mk = src["marker"]
                exits.append(dict(id=f"{key}-border-{src['side']}", map=m, at=[round(mk[0]), round(mk[2])],
                                  r=2500, to=to, arrive=arrive, kind="border", proven=False))
    ids = [e["id"] for e in exits]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"duplicate exit ids {sorted(dup)}")
    unpaired = [(m, g) for m in gs for g in gs[m] if (m, g) not in used]
    return {"_doc": "built by extract from the disc's CMAP files; groups paired by geography; unpaired groups: "
                    + ", ".join(f"{PLACE_NAMES[m]} {g}" for m, g in unpaired),
            "exits": exits}
