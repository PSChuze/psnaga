"""Python models of how the client reads each CODE_CHAR reply. Used by the tests to prove a reply parses to the end exactly like
the client would. Raise ParseError where the client would return an error (VCE codes: -2 over
max, -0x2e short read, -0x1a null header)."""
from . import vce


class ParseError(Exception):
    pass


class Arc:
    """Archive reader (vtable 0x4513c0) over VCE field codecs."""

    def __init__(self, data, pos):
        self.r = vce.Reader(data, pos)

    def _wrap(self, f, *a):
        try:
            return f(*a)
        except vce.WireError as e:
            raise ParseError(f"at {self.r.off}: {e}")

    def u8(self):
        return self._wrap(self.r.u8)

    def u16(self):
        return self._wrap(self.r.u16)

    def u32(self):
        return self._wrap(self.r.u32)

    def bytes(self, maxlen):                          # vt+0x20 -> 0x129010, max from caller
        return self._wrap(self.r.bytes, maxlen)

    def str(self, maxlen):                            # vt+0x18 -> 0x20ca20 (UTF-8 -> SJIS)
        b = self.bytes(0x10000)
        s = b.decode("utf-8")
        if len(s.encode("cp932")) >= maxlen:
            raise ParseError(f"string {s!r} does not fit {maxlen}")
        return s

    def u32s(self, maxn):                             # vt+0x40 -> 0x1292b0
        n = self._wrap(self.r.length)
        if n > maxn:
            raise ParseError(f"u32 array of {n} > {maxn}")
        return [self.u32() for _ in range(n)]

    def done(self):
        return self.r.done()


def list_entry(a):                                    # 0x1ac630 read half (0x1ac988..)
    d = dict(family=a.str(0x11), given=a.str(0x11))
    d["b22"], d["b23"], d["u24"] = a.u8(), a.u8(), a.u32()
    d["b28"], d["b29"], d["b2a"], d["b2c"], d["b2b"] = (a.u8() for _ in range(5))
    d["u30"], d["b34"] = a.u32(), a.u8()
    d["w36"] = [a.u16() for _ in range(13)]
    return d


def read_position(a):                                 # 0x18ff70 read half
    return [a.u8(), a.u32(), a.u32(), a.u32(), a.u16()]


def summary(a):                                       # 0x1a30b0 read half (0x1a3628..0x1a3b90)
    d = dict(A=a.u32(), u4=a.u32(), s8=read_position(a), family=a.str(0x11), given=a.str(0x11))
    d["b3e"] = [a.u8() for _ in range(6)]
    d["x45"] = a.bytes(0xF)
    d["w54"], d["b56"], d["w58"], d["b5a"], d["b5b"], d["w5c"], d["b5e"], d["b5f"] = (
        a.u16(), a.u8(), a.u16(), a.u8(), a.u8(), a.u16(), a.u8(), a.u8())
    d["b42"] = a.u8()
    d["b60"] = [a.u8() for _ in range(7)]
    d["b6d"] = a.u8()
    if d["A"] != 0 or d["w54"] == 0:
        d["b67"] = [a.u8(), a.u8()]
    if d["A"] != 0:
        d["b69"] = [a.u8(), a.u8()]
        d["a7c"] = a.u32s(3)
    else:
        d["b6b"] = [a.u8() for _ in range(4)]
    d["b70"] = a.u8()
    d["x71"] = a.bytes(0xF)
    return d


def char_struct(a):                                   # 0x1acce0 read half (0x1acff8..)
    d = dict(b0=a.u8(), u4=a.u32(), u8=a.u32(), uc=a.u32(), family=a.str(0x11), given=a.str(0x11))
    d["b36"] = [a.u8() for _ in range(5)]
    d["u3c"], d["b40"], d["b41"] = a.u32(), a.u8(), a.u8()
    d["w42"] = [a.u16() for _ in range(9)]
    return d


def parse_list(payload):
    """0x19c6e0 (CODE_CHAR sub 3)."""
    r = vce.Reader(payload, 2)
    res = r.i32()
    if res < 0:
        return dict(res=res)
    count = r.u32()
    if count >= 4:
        raise ParseError("count >= 4")
    a = Arc(payload, r.off)
    owners = a.bytes(0x16)                            # 0x1f1de0
    slots = []
    for _ in range(count):
        cid = a.u32()                                 # 0x1ad6f0
        if cid:
            slots.append(dict(cid=cid, entry=list_entry(a), summary=summary(a)))
        else:
            slots.append(None)
    extra = a.u32s(count) if not a.done() else None   # 0x1292b0 into +0x1d24
    if not a.done():
        raise ParseError(f"{len(a.r.rest())} bytes left")
    return dict(res=res, count=count, owners=owners, slots=slots, extra=extra)


def parse_create_or_load(payload):
    """0x19c478 (sub 1: res, u32, u32, char) / 0x19c5d0 (sub 2: res, u32, char)."""
    r = vce.Reader(payload, 1)
    sub, res = r.u8(), r.i32()
    if res < 0:
        return dict(sub=sub, res=res)
    a1 = r.u32()
    a2 = r.u32() if sub == 1 else None
    a = Arc(payload, r.off)
    c = char_struct(a)
    if not a.done():
        raise ParseError(f"{len(a.r.rest())} bytes left")
    return dict(sub=sub, res=res, a1=a1, a2=a2, char=c)


# ---------------------------------------------------------------------------- world messages
# Models of the WORLD object's parser 0x19e7b0.

def location_record(a):                               # 0x1a3bb0 read half (0x1a3d28..)
    d = dict(uid=a.u32(), cid=a.u32())
    s = read_position(a)                                  # u8 area, u32 x, y, z, u16 dir
    d["area"], d["pos"], d["dir"] = s[0], tuple(s[1:4]), s[4]
    d["family"], d["given"] = a.str(0x11), a.str(0x11)
    d["level"], d["power"], d["job"], d["sex"], d["b42"] = (a.u8() for _ in range(5))
    return d


def location_update(a):                               # 0x1a6f10 read half (0x1a7018..)
    d = dict(kind=a.u8(), cid=a.u32(), pos=(a.u32(), a.u32(), a.u32()))
    d["dir"], d["b16"] = a.u16(), a.u8()
    return d


def char_info(a):                                     # 0x1a2500 read half (0x1a2ad0..0x1a30a4)
    d = dict(uid=a.u32(), cid=a.u32())
    s = read_position(a)
    d["area"], d["pos"], d["dir"] = s[0], tuple(s[1:4]), s[4]
    d["family"], d["given"] = a.str(0x11), a.str(0x11)
    d["level"], d["power"], d["job"], d["sex"], d["b43"], d["b44"] = (a.u8() for _ in range(6))
    d["params"] = a.bytes(0xF)                        # vt+0x28, max 0xf -> P+0x10fa4
    d["w54"], d["b56"], d["w58"], d["b5a"], d["b5b"], d["w5c"], d["b5e"], d["b5f"] = (
        a.u16(), a.u8(), a.u16(), a.u8(), a.u8(), a.u16(), a.u8(), a.u8())
    d["b42"] = a.u8()
    d["b60"] = [a.u8() for _ in range(7)]
    d["b6d"], d["b6f"], d["b67"], d["b68"] = a.u8(), a.u8(), a.u8(), a.u8()
    if d["uid"] != 0:
        d["b69"], d["b6a"], d["a88"] = a.u8(), a.u8(), a.u32s(3)
    else:
        d["b6b"], d["b6c"], d["b6e"] = a.u8(), a.u8(), a.u8()
    d["b70"] = a.u8()
    if d["b70"] != 2:
        d["u74"], d["u78"] = a.u32(), a.u32()
    d["b7c"] = a.u8()
    d["x7d"] = a.bytes(0xF)
    return d


def parse_notify_char(payload):
    """0x19ed18 (cmd 0x0B): u8 sub; sub 1 (0x19eeb0) / 8 (0x19ef88) / 0xa (0x19edd8): u8 count
    (signed; <= 0 -> nothing), count records; sub 2 zone enter (decoded elsewhere)."""
    r = vce.Reader(payload, 1)
    sub = r.u8()
    codec = {1: location_record, 8: location_update, 0xA: char_info}.get(sub)
    if codec is None:
        return dict(sub=sub)
    n = r.i8()
    a = Arc(payload, r.off)
    recs = [codec(a) for _ in range(max(n, 0))]
    if not a.done():
        raise ParseError(f"{len(a.r.rest())} bytes left")
    return dict(sub=sub, records=recs)


def parse_notify_field(payload):
    """0x19f658 (cmd 0x0C): u8 sub < 8; subs 1 (0x1a59a0), 4 (0x1a5ec0), 5 (0x1eb140)."""
    r = vce.Reader(payload, 1)
    sub = r.u8()
    a = Arc(payload, r.off)
    if sub == 1:
        d = dict(b100=a.u8(), pos=(a.u32(), a.u32(), a.u32()), name=a.str(0x21))
    elif sub == 4:
        d = dict(date=a.u32(), time=a.u32())
    elif sub == 5:
        d = dict(weather=a.u8(), year=a.u16(), month=a.u8(), day=a.u8(), hour=a.u8())
    else:
        raise ParseError(f"sub {sub} not modelled")
    if not a.done():
        raise ParseError(f"{len(a.r.rest())} bytes left")
    d["sub"] = sub
    return d


CMD23_SUBS = 0x1E                                     # 0x283e30: only signed subs below 0x1e


def cmd23_has_parser(sub):
    """0x283e30 dispatches (signed) subs 0..0x1d through table 0x4283a0; anything else prints
    "unknown sub command (%d)" and returns without a callback. Subs 0, 6-9, 0xe, 0x12 map to the
    same default case, sub 2 to 0x284f90 (no print, no callback)."""
    return 0 <= (sub if sub < 0x80 else sub - 0x100) < CMD23_SUBS and sub not in (0, 2, 6, 7, 8, 9, 0xE, 0x12)


def parse_cmd2a(payload):
    """0x18f6e8 (any sub): u8 sub (ignored), u16 seq, then 0x2b2bf0: four groups of u8 n, u32 array
    (max n), n strings; group 0 also n strings (max 0x41). Group string max 0x21/0x21/0x19/0x21.
    Model of the empty case plus the counts; strings are read as length-prefixed bytes."""
    a = Arc(payload, 1)
    d = dict(sub=a.u8(), seq=a.u16(), groups=[])
    for k, smax in enumerate((0x21, 0x21, 0x19, 0x21)):
        n = a.u8()
        ids = a.u32s(n)
        names = [a.bytes(smax) for _ in range(n)]
        extra = [a.bytes(0x41) for _ in range(n)] if k == 0 else []
        d["groups"].append((ids, names, extra))
    if not a.done():
        raise ParseError("trailing bytes")
    return d


def parse_cmd9(payload):
    """0x19bf80 (table 0x421040): sub 1 = u32, u32, u32, bytes (max 0x3fe); sub 5 = u32 x3;
    subs 2, 3, 4, 6 = u32. Anything else: no callback."""
    a = Arc(payload, 1)
    sub = a.u8()
    if sub == 1:
        d = dict(sub=1, res=a.u32(), ticket=a.u32(), c=a.u32(),
                 text=a.bytes(0x3fe).decode("utf-8"))
    elif sub == 5:
        d = dict(sub=5, vals=[a.u32(), a.u32(), a.u32()])
    elif sub in (2, 3, 4, 6):
        d = dict(sub=sub, res=a.u32())
    else:
        raise ParseError(f"no cmd 9 sub {sub}")
    if not a.done():
        raise ParseError("trailing bytes")
    return d


def parse_cmd24(payload):
    """0x287270 subs 3..7 (table 0x4285e0): 3 nothing; 4 u8, u8; 5 u8; 6 u8, u8 [u16 if bytes
    remain]; 7 bytes (max 0xc)."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 4:
        d["vals"] = [a.u8(), a.u8()]
    elif sub == 5:
        d["vals"] = [a.u8()]
    elif sub == 6:
        d["vals"] = [a.u8(), a.u8()]
        if not a.done():
            d["vals"].append(a.u16())
    elif sub == 7:
        d["order"] = a.bytes(0xc)
    elif sub != 3:
        raise ParseError(f"cmd 0x24 sub {sub} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    return d


def parse_cmd26(payload):
    """0x289c80 subs 3..0xc (table 0x428940): 3 / 4 i8 res [+ struct if res >= 0, not modelled];
    5 u8, u32; 6 u8; 7 nothing; 8 u16; 9 u32, u8; 0xa u32, u8, u8, u16; 0xb u8; 0xc 6 x u8 (0x1f5580)."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    layouts = {5: "BI", 6: "B", 7: "", 8: "H", 9: "IB", 0xA: "IBBH", 0xB: "B", 0xC: "BBBBBB"}
    if sub in (3, 4):
        res = a.u8()
        if res < 0x80:
            raise ParseError("info struct not modelled")
        vals = [res - 0x100]
    elif sub in layouts:
        get = {"B": a.u8, "H": a.u16, "I": a.u32}
        vals = [get[c]() for c in layouts[sub]]
    else:
        raise ParseError(f"cmd 0x26 sub {sub} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    return dict(sub=sub, seq=seq, vals=vals)


# ---------------------------------------------------------------------------- NPCs
def target_info(a):                                   # 0x1f0dc0 read half (0x1f0f9c..0x1f1130)
    d = dict(cid=a.u32(), family=a.str(0x11), given=a.str(0x11))
    d["level"] = a.u8()                               # +0x26 -> target +0x2f
    d["b27"], d["b28"], d["b29"], d["b2a"], d["b2b"] = (a.u8() for _ in range(5))
    d["pos"] = (a.u32(), a.u32(), a.u32())            # +0x2c/+0x30/+0x34 -> target +0x1c..
    d["dir"] = a.u16()                                # +0x38
    return d


def parse_cmd25(payload):
    """0x28b190 (u8 sub, u16 seq); sub 1 SCMD_TARGET_SET (0x28b2a8): i32 res, then if res >= 0
    the struct 0x1f0dc0; sub 2 (0x28b370): nothing. Other subs not modelled."""
    a = Arc(payload, 1)
    sub, seq = a.u8(), a.u16()
    d = dict(sub=sub, seq=seq)
    if sub == 1:
        res = a.u32()
        d["res"] = res - (1 << 32) if res & 0x80000000 else res
        if d["res"] >= 0:
            d["target"] = target_info(a)
    elif sub != 2:
        raise ParseError(f"cmd 0x25 sub {sub} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    return d


def parse_notify_chat(payload):
    """0x19b710 (cmd 0x0D): sub 1 / 4 (0x1a62a0) u8 kind, u32 cid, str msg (max 0x191);
    sub 5 / 6 (0x19b8f8) bytes msg (max 0x320); sub 9 (0x19b988) u32 -> record +4 (speaker cid),
    u32 -> record +0, bytes msg (max 0x320, SJIS max 0x191). Returns the record fields."""
    a = Arc(payload, 1)
    sub = a.u8()
    if sub in (1, 4):
        d = dict(kind=a.u8(), cid=a.u32(), msg=a.str(0x191))
    elif sub in (5, 6):
        d = dict(msg=a.bytes(0x320).decode("utf-8"))
    elif sub == 9:
        d = dict(cid=a.u32(), extra=a.u32(), msg=a.bytes(0x320).decode("utf-8"))
        if len(d["msg"].encode("cp932")) >= 0x191:
            raise ParseError("sub 9 text does not fit 0x191 SJIS bytes")
    else:
        raise ParseError(f"NOTIFY_CHAT sub {sub} not modelled")
    if not a.done():
        raise ParseError("trailing bytes")
    d["sub"] = sub
    return d


def npc_ui_event(rec, notify_sub):
    """The UI event 0x1893f0 posts for a same-area record with uid 0: CHAR_LOCATION (sub 1)
    0xc0 / 0xd5 / 0xa6, CHAR_INFO (sub 0xa) 0xbd / 0xd4 / 0xa3 for cid top byte 0xff / 0xfe /
    other (the conditional move at 0x189608 / 0x189c48 keeps 0xd5 / 0xd4 only for 0xfe). Only 0xd5 / 0xd4 have
    handlers (0x1d2eb0 / 0x1d2df0); the others hit the default."""
    top = rec["cid"] >> 24
    table = {1: (0xC0, 0xD5, 0xA6), 0xA: (0xBD, 0xD4, 0xA3)}[notify_sub]
    return table[0] if top == 0xFF else table[1] if top == 0xFE else table[2]


def npc_actor_created(rec):
    """0x21c6e0: task 0x94 event 0x26 does nothing for job byte 0, takes the 0x21c2a0 object path
    for 1, and creates the character actor (0x21bd70 -> 0x97/5 event 1 -> 0x60c220) otherwise."""
    return rec["uid"] == 0 and npc_ui_event(rec, 0xA) == 0xD4 and rec["job"] not in (0, 1)


def entry_attackable(rec, zone=0, my_b42=0):
    """0x2733a0 (character entry vt 0x40) for a CHAR_INFO record, without its first test
    (entry vt 0x38(6) & 1, not decoded): record +0x6b (entry +0x2c) == 2 -> never; faction
    (+0x3f, entry +0x30) & 0x7f == 0 -> yes; zone 200..299 and a different +0x42 -> yes; else
    record +0x6c (entry +0x2b): 0 -> yes, 1 -> only if +0x6b == 0, 2..5 (and above) -> no."""
    b6b, b6c = rec.get("b6b", 0), rec.get("b6c", 0)
    if b6b == 2:
        return False
    if rec["power"] & 0x7F == 0:
        return True
    if 200 <= zone < 300 and rec.get("b42", 0) != my_b42:
        return True
    return {0: True, 1: b6b == 0}.get(b6c, False)


def action_hint(rec, zone=0):
    """Simplified 0x62d3d0 for a target in range: attackable -> 0xa "○Attack" (the real code has
    more conditions); else uid 0: 0xd "○Look" for a 0xfe cid, 9 "○Talk" otherwise. Any value but
    0x2c lets ○ run 0x611880 -> `28 01` (0x62c990 only compares with 0x2c)."""
    if entry_attackable(rec, zone):
        return 0xA
    if rec["uid"] == 0:
        return 0xD if rec["cid"] >> 24 == 0xFE else 9
    return 0x2C
