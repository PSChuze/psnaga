"""CODE_PONG and CODE_CHAR (world connection) codecs plus a per-account character store.

Client builders (class vtable 0x4501a0, used by the world object 0x4b3a80):
  0x190630  03 u32 time u32 uid                         ping, every ~5 s (net 0x18e960)
  0x1903f0  05 03 u32 uid                               character list request (sent after login)
  0x190140  05 01 u32 uid u16 slot + archive(create)    character create (net 0x18e6b0 -> 0x197910)
  0x190250  05 02 u32 u32                               character load (select)
  0x190320  05 04 u32 u32                               (delete?)
Server replies (parse chain 0x19e7b0 -> 0x18f5e0 -> 0x190f40; CODE_CHAR subcodes in 0x19c330):
  03 u32 time                                            0x1911c8 -> 0x188140 (ping ring 0x3bda90)
  05 03 u32 res, u32 count(<4), archive(list)            0x19c6e0
  05 01 u32 res, u32 a, u32 b, archive(char)             0x19c478
  05 02 u32 res, u32 a, archive(char)                    0x19c5d0

"archive" = the same VCE field codecs through the archive class (vtable 0x4513c0): u8/u16/u32 BE,
bytes = VCE length header + bytes, strings = bytes in UTF-8 (write hook 0x1a22b0 / read hook
0x1a22d0 convert SJIS <-> UTF-8), u32 array = length header (count) + u32s (0x128d60).
"""
import json
import os
import time

from . import persist, towns, vce

# ---------------------------------------------------------------------------- archive writer


class W:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v):
        self.b += vce.put_u8(v)
        return self

    def u16(self, v):
        self.b += vce.put_u16(v)
        return self

    def u32(self, v):
        self.b += vce.put_u32(v)
        return self

    def str(self, s):                       # vt+0x14 / 0x20c910 (UTF-8 on the wire)
        self.b += vce.put_bytes(s.encode("utf-8"))
        return self

    def raw(self, data, n):                 # vt+0x24 fixed-length bytes field
        data = bytes(data)[:n]
        self.b += vce.put_bytes(data + bytes(n - len(data)))
        return self

    def u32s(self, vals):                   # 0x128d60
        self.b += vce.put_len(len(vals))
        for v in vals:
            self.b += vce.put_u32(v)
        return self

    def bytes(self):
        return bytes(self.b)


# ---------------------------------------------------------------------------- structures

CREATE_U8_OFFSETS = [0x22, 0x23, 0x24, 0x26, 0x25] + list(range(0x27, 0x36))   # 20 bytes


def read_create(r):
    """Create struct (write 0x1ac040 / read 0x1ac338): str +0x00, str +0x11, 20 x u8."""
    family = r.bytes(0x100).decode("utf-8", "replace")
    given = r.bytes(0x100).decode("utf-8", "replace")
    looks = [r.u8() for _ in range(20)]
    return dict(family=family, given=given, looks=looks)


def write_create(c):
    w = W().str(c["family"]).str(c["given"])
    for v in c["looks"]:
        w.u8(v)
    return w.bytes()


def write_char(c, pos=None):
    """Character struct (0x1acce0): u8 +0, u32 +4, u32 +8, u32 +0xc, str +0x14, str +0x25, u8
    +0x36 +0x37 +0x38 +0x3a +0x39, u32 +0x3c, u8 +0x40, u8 +0x41, 9 x u16 +0x42..+0x52. KNOWN:
    the first four fields are the POSITION block (u8 area, u32 x, u32 y, u32 z):
    SCMD_CHAR_LOAD's main handler 0x188980 copies them with 0x18ff40 into net+0x324
    ("m_ptClient"), and UI event 0 (0x2093c0) copies x/y/z into the local-player object P
    (0x4b857c) +0x1c/+0x20/+0x24, which 0x60bd80 spawns the player at (y is replaced by the
    ground height under x/z when P+0x34 == 0). We used to send slot/cid/uid here, which put P at
    (2, 67110001, 0). The five u8 mirror the first five create bytes (month, day, sex, job,
    faction: +0x36 +0x37 +0x38 +0x3a +0x39; 0x2093c0 takes +0x38 sex -> P+0x2e, +0x39 faction ->
    P+0x30, +0x3a job -> P+0x2d)."""
    area, x, y, z = (list(pos) + [0, 0, 0, 0])[:4] if pos else (0, 0, 0, 0)
    w = W().u8(area).u32(x).u32(y).u32(z)
    w.str(c["family"]).str(c["given"])
    for v in (list(c["looks"]) + [0] * 5)[:5]:              # month, day, sex, job, faction
        w.u8(v)
    lk = list(c["looks"]) + [0] * 20
    w.u32(0).u8(lk[5]).u8(lk[6])
    for v in lk[11:20]:                                      # 5 stats + 4 elements (HYPOTHESIS)
        w.u16(v)
    return w.bytes()


def derived_stats(stats):
    """Life, Ki, Attack, Defense for display - INVENTED placeholders (the real formulas are
    server-side and unknown): Life 40+5*Sta, Ki 20+5*Int, Attack 2*Str, Defense 2*Sta."""
    st, sta, dex, intel, cha = (list(stats) + [0] * 5)[:5]
    return [40 + 5 * sta, 20 + 5 * intel, 2 * st, 2 * sta]


def write_list_entry(c):
    """Character-list struct (read 0x1ac630; copied to the UI slot struct by 0x1ec520).
    Wire: str +0x00, str +0x11, u8 +0x22, u8 +0x23, u32 +0x24, u8 +0x28 +0x29 +0x2a +0x2c
    +0x2b, u32 +0x30, u8 +0x34, 13 x u16 +0x36..+0x4e.
    Field meanings from the character-select screen:
      +0x22 level, +0x23 rank (20 showed "Unknown"), +0x28 birth month, +0x29 birth day,
      +0x2a sex, +0x2c job, +0x2b faction (same sex/job/faction order and swap as the create
      struct), 13 u16 = Life, Ki, Attack, Defense, Str, Sta, Dex, Int, Cha, Earth, Water,
      Fire, Wind. u32 +0x24, u32 +0x30 and u8 +0x34 are unknown (area? exp?) - sent 0.
    The sex / job / faction slots are proven only as a group (job 6 == faction 6 in the test)."""
    lk = list(c["looks"]) + [0] * 20
    month, day, sex, job, faction = lk[0], lk[1], lk[2], lk[3], lk[4]
    stats, elements = lk[11:16], lk[16:20]
    w = W().str(c["family"]).str(c["given"])
    w.u8(c.get("level", 1)).u8(c.get("rank", 0)).u32(0)
    w.u8(month).u8(day).u8(sex).u8(job).u8(faction)         # +0x28 +0x29 +0x2a +0x2c +0x2b
    w.u32(0).u8(0)
    for v in derived_stats(stats) + stats + elements:        # 13 x u16
        w.u16(v)
    return w.bytes()


def appearance(c):
    """Record bytes (0x1a2500 / 0x1a30b0 layout) for the looks picked at creation. Chain as the
    client reads it: vt 0x74 (0x1db3b0) copies the record to P, vt 0x20 (0x1db0d0) builds
    the appearance struct A, the human body 0x2547a0 reads it:
      +0x68 -> P+0x63 -> A[0xd] face  (head mesh = face + sex*9 + 0x25, 0x253550)   looks[5]
      +0x67 -> P+0x64 -> A[0xc] hair  (0x253fd0, with sex)                          looks[6]
      +0x56 -> P+0x62 -> A[0xa] body slider 1 (0..100, 50.0 neutral; 0x255bf0)       looks[8]
      +0x69 -> P+0xa0 -> A[0xb] body slider 2 (same scale)                           looks[9]
      +0x6a -> P+0xa1 (with sex -> 0x274010; voice? HYPOTHESIS)                       looks[10]
    Same bytes as the creation preview (model +0x89 face, +0x88 hair, +0x86 / +0x87 sliders,
    0x67d580). looks[7] repeats the hair (0x679430 is called twice at 0x65f3c0). All 0 put the
    body at both slider extremes and face / hair 0, whatever was picked."""
    lk = list(c.get("looks", [])) + [0] * 20
    from . import hiryu                                   # +0x6a = the specialised title
    return dict(b56=lk[8], b67=lk[6], b68=lk[5], b69=lk[9], b6a=hiryu.title_byte(c, lk[10]))


def write_summary(c):
    """Character-select summary struct, exactly as read by 0x1a30b0 (read half 0x1a3628..):
    u32 A +0, u32 +4, [0x18ff70: u8, u32, u32, u32, u16] +8, str +0x1c, str +0x2d,
    u8 +0x3e +0x3f +0x40 +0x41 +0x43 +0x44, bytes(max 0xf) +0x45, u16 +0x54, u8 +0x56,
    u16 +0x58, u8 +0x5a, u8 +0x5b, u16 +0x5c, u8 +0x5e, u8 +0x5f, u8 +0x42, u8 +0x60..+0x66,
    u8 +0x6d, then CONDITIONAL on A (and +0x54):
      if A != 0 or u16 +0x54 == 0: u8 +0x67, u8 +0x68          (0x1a39e8)
      if A != 0: u8 +0x69, u8 +0x6a, u32 array (max 3) +0x7c    (0x1a3a40)
      if A == 0: u8 +0x6b, u8 +0x6c, u8 +0x6e, u8 +0x6f          (0x1a3ab8)
    then u8 +0x70, bytes(max 0xf) +0x71. The writer (0x1a30b0 write half) mirrors this.
    A = cid here (nonzero), so the +0x6b..+0x6f block is absent."""
    a = c["cid"]
    u54 = 0
    ap = appearance(c)
    w = W().u32(a).u32(c["uid"])
    w.u8(c["slot"]).u32(0).u32(0).u32(0).u16(0)           # 0x18ff70 at +8
    w.str(c["family"]).str(c["given"])                    # +0x1c, +0x2d
    for _ in range(6):                                    # +0x3e +0x3f +0x40 +0x41 +0x43 +0x44
        w.u8(0)
    w.raw(b"", 0xF)                                       # +0x45
    w.u16(u54).u8(ap["b56"]).u16(0).u8(0).u8(0).u16(0).u8(0).u8(0)  # +0x54..+0x5f
    w.u8(0)                                               # +0x42
    for _ in range(7):                                    # +0x60..+0x66
        w.u8(0)
    w.u8(0)                                               # +0x6d
    if a != 0 or u54 == 0:
        w.u8(ap["b67"]).u8(ap["b68"])                     # +0x67 hair, +0x68 face
    if a != 0:
        w.u8(ap["b69"]).u8(ap["b6a"])                     # +0x69 slider 2, +0x6a
        w.u32s([0, 0, 0])                                 # +0x7c
    else:
        w.u8(0).u8(0).u8(0).u8(0)                         # +0x6b +0x6c +0x6e +0x6f
    w.u8(0)                                               # +0x70
    w.raw(b"", 0xA)                                       # +0x71 (max 0xf)
    return w.bytes()


# ---------------------------------------------------------------------------- messages

def pong(payload):
    r = vce.Reader(payload, 1)
    t = r.u32()
    return vce.put_u8(3) + vce.put_u32(t)


# Province -> owning faction at world start (historical, c. 1560). Index = STRDAT table 4 id.
PROVINCE_OWNERS = [
    0,   # 0  none
    1,   # 1  Echigo      Uesugi
    1,   # 2  Kozuke      Uesugi
    3,   # 3  Musashi     Hojo
    2,   # 4  Kai         Takeda
    2,   # 5  Shinano     Takeda
    3,   # 6  Sagami      Hojo
    4,   # 7  Suruga      Imagawa
    4,   # 8  Totomi      Imagawa
    5,   # 9  Mikawa      Tokugawa
    6,   # 10 Owari       Oda
    7,   # 11 Mino        Saito
    8,   # 12 Etchu       Honganji
    8,   # 13 Kaga        Honganji
    9,   # 14 Echizen     Asakura
    10,  # 15 Omi         Azai
    6,   # 16 Ise         Oda
    11,  # 17 Iga         Iga
    12,  # 18 Yamashiro   Ashikaga
    13,  # 19 Settsu-Izumi Miyoshi
    13,  # 20 Yamato      Miyoshi
    14,  # 21 Kii         Saika
]
ACCOUNT_BYTES = bytes.fromhex(os.environ.get("NOBU_ACCOUNT_BYTES", bytes(PROVINCE_OWNERS).hex()))[:0x16]


def char_list_reply(chars, slots=3):
    """05 03: res 0, count = slots (3, < 4 at 0x19c778), account bytes(<=0x16), then per slot
    u32 cid (0 = empty; 0x1ad6f0) and, if nonzero, create struct + summary struct."""
    by_slot = {c["slot"]: c for c in chars}
    body = W()
    # 0x1f1de0: bytes, max 0x16 = PROVINCE OWNERS: byte i = the faction (STRDAT table 5) that
    # holds province i (STRDAT table 4, 0 = none). Empty -> every faction "fallen" (UIMSG 1.112);
    # 22 x 01 -> only Uesugi standing and owning every province. A faction with no province counts
    # as fallen, so every clan must own at least one.
    body.b += vce.put_bytes(ACCOUNT_BYTES)
    for s in range(slots):
        c = by_slot.get(s)
        if c is None:
            body.u32(0)
        else:
            body.u32(c["cid"])
            body.b += write_list_entry(c)
            body.b += write_summary(c)
    return bytes(vce.put_u8(5) + vce.put_u8(3) + vce.put_u32(0) + vce.put_u32(slots)) + body.bytes()


def create_reply(res, c=None):
    out = vce.put_u8(5) + vce.put_u8(1) + vce.put_u32(res)
    if res >= 0 and c:
        zone, x, y, z, _ = char_position(c)
        out += vce.put_u32(c["cid"]) + vce.put_u32(c["slot"] + 1) + write_char(c, (area_for(zone), x, y, z))
    return out


def delete_reply(res):
    """05 04 u32 res (first-stage parser 0x1912e0; main 0x188e48 SCMD_CHAR_DELETE)."""
    return vce.put_u8(5) + vce.put_u8(4) + vce.put_u32(res)


def load_reply(res, c=None, pos=None):
    """05 02 (0x19c5d0): u32 res, u32 cid, char struct. pos = (area, x, y, z) for the char
    struct's position block; default = the character's saved/spawn position."""
    out = vce.put_u8(5) + vce.put_u8(2) + vce.put_u32(res)
    if res >= 0 and c:
        if pos is None:
            zone, x, y, z, _ = char_position(c)
            pos = (area_for(zone), x, y, z)
        out += vce.put_u32(c["cid"]) + write_char(c, pos)
    return out


def decode_create_request(payload):
    r = vce.Reader(payload, 2)
    uid, slot = r.u32(), r.u16()
    c = read_create(r)
    c.update(uid=uid, slot=slot)
    if not r.done():
        c["rest"] = r.rest().hex()
    return c


# ---------------------------------------------------------------------------- store

class CharStore:
    """{"next_cid": n, "chars": {uid: [char, ...]}} in $NOBU_DATA/chars.json.

    Persistence (nobu/persist.py): save() writes at once, atomically (use it for anything that must
    survive a crash: trades, purchases, creation, rewards); save_soon() marks the store dirty and
    coalesces into one write within NOBU_SAVE_DELAY s (moves, periodic ticks); flush() writes a
    pending deferred save now (shutdown). A corrupt file on load is recovered from the newest good
    backup, or PersistError is raised; never an empty start over existing data."""

    def __init__(self, path):
        self.path = path
        self.data = {"next_cid": 1, "chars": {}}
        self.dirty = False
        self._timer = None
        self._last_save = 0.0
        self._last_backup = time.monotonic()        # load_json just took the boot backup
        if path:
            self.data = persist.load_json(path, default=lambda: {"next_cid": 1, "chars": {}},
                                          validate=valid_data)

    def save(self):
        """Write now (atomic, durable). Also takes a rotating backup every NOBU_BACKUP_SECS."""
        self._cancel_timer()
        self.dirty = False
        if not self.path:
            return
        self._last_save = time.monotonic()
        text = persist.atomic_write_json(self.path, self.data, indent=1, ensure_ascii=False)
        if time.monotonic() - self._last_backup >= persist.backup_secs():
            self._last_backup = time.monotonic()
            try:
                persist.backup_text(self.path, text, tag="auto")
            except OSError as e:
                persist.loud(f"backup of {self.path} failed: {e!r}")

    def save_soon(self, delay=None):
        """Deferred save: one write within `delay` s (default NOBU_SAVE_DELAY), however many calls.
        Inside the server's asyncio loop a timer does it; without a loop (tests, tools) it saves
        when the last write is older than `delay`."""
        delay = persist.save_delay() if delay is None else delay
        self.dirty = True
        if delay <= 0:
            self.save()
            return
        if self._timer is not None:
            return
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if time.monotonic() - self._last_save >= delay:
                self.save()
            return
        self._timer = loop.call_later(delay, self._deferred)

    def _deferred(self):
        self._timer = None
        if not self.dirty:
            return
        try:
            self.save()
        except Exception as e:                      # disk full etc.: keep the data dirty, retry later
            self.dirty = True
            persist.loud(f"deferred save of {self.path} failed: {e!r}; retrying")
            self.save_soon()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def flush(self):
        """Write a pending deferred save now; True when something was written."""
        if self.dirty:
            self.save()
            return True
        self._cancel_timer()
        return False

    def list(self, uid):
        return self.data["chars"].get(str(uid), [])

    def create(self, uid, slot, family, given, looks):
        chars = self.data["chars"].setdefault(str(uid), [])
        if any(c["slot"] == slot for c in chars) or not 0 <= slot < 3:
            free = [s for s in range(3) if all(c["slot"] != s for c in chars)]
            if not free:
                return None
            slot = free[0]
        cid = self.data["next_cid"]
        self.data["next_cid"] += 1
        c = dict(cid=cid, uid=uid, slot=slot, family=family, given=given, looks=list(looks),
                 created=time.strftime("%Y-%m-%dT%H:%M:%S"))
        chars.append(c)
        self.save()
        return c

    def delete(self, uid, cid):
        """Remove character cid from account uid only; returns the removed char or None."""
        chars = self.data["chars"].get(str(uid), [])
        for i, c in enumerate(chars):
            if c["cid"] == cid:
                del chars[i]
                self.save()
                return c
        return None

    def set_pos(self, uid, cid, pos, min_interval=2.0):
        """Remember the last reported position [zone, x, y, z, dir]; written to disk within
        min_interval seconds, coalesced (moves arrive often); min_interval 0 = now."""
        c = self.find(uid, cid)
        if not c:
            return None
        c["pos"] = [int(v) for v in pos]
        if min_interval <= 0:
            self.save()
        else:
            self.save_soon(min_interval)
        return c

    def find(self, uid, cid):
        for c in self.list(uid):
            if c["cid"] == cid:
                return c
        for cs in self.data["chars"].values():
            for c in cs:
                if c["cid"] == cid:
                    return c
        return None


def valid_data(d):
    """Shape check used on load / restore / validate: {"next_cid": int, "chars": {uid: [dict]}}."""
    return (isinstance(d, dict) and isinstance(d.get("next_cid"), int) and isinstance(d.get("chars"), dict)
            and all(isinstance(v, list) and all(isinstance(c, dict) for c in v) for v in d["chars"].values()))


def default_path():
    if os.environ.get("NOBU_CHAR_DB"):
        return os.environ["NOBU_CHAR_DB"]
    base = os.environ.get("NOBU_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "chars.json")


def default_store():
    return CharStore(default_path())


# ---------------------------------------------------------------------------- creation stat table
# Client 0x1c6c20 (net command 0x50 at 0x18ed20, posted by the creation UI 0x65f270 whenever
# job and faction are both chosen and their table entries are not loaded):
#   23 01 u16 seq u8 job u8 faction
# Reply parsed at 0x283e30: u8 sub, u16 (stored to gp-0x7640 by 0x1c6c00), then sub 1 (0x283f48):
#   archive of 20 u8 (0x1eb760): job, faction, 9 job values, 9 faction values.
# Main handler (cmd 0x23 -> 0x2869a0) forwards it to the UI (dest 0x23, sub 1); the UI
# (0x661240) stores the 9 job bytes at state+job*16+0xbc and marks +0xb8 = 2, the 9 faction
# bytes at state+faction*16+0x13c and +0x138 = 2. The Stats row (0x661790) only opens when both
# entries are 2 (0x65efa0 bit 0x40), and OK needs the 10 bonus points spent (0x65ef60).
# The 9 values: [0..4] = the 5 stats (Strength, Stamina, Dexterity, Intellect, Charisma),
# [5..8] = the 4 elements (Earth, Water, Fire, Wind). KNOWN use: stat i base = job[i] +
# faction[i] (0x65f210 -> 0x65f150 / 0x65f180), element i = job[5+i] + faction[5+i] (0x65f240);
# the "+" key (0x66b840) adds a point only while points are left (0x65ef60 > 0) AND
# base + allocated < 10 (0x66b898; same test greys the arrow at 0x66bdec). So every
# job[i] + faction[i] must stay well below 10 or that stat cannot take points.
# The real per-job numbers are not in the client (they came from the server); these profiles
# are INVENTED, kept small so the 10 bonus points can go anywhere. Jobs = UIMSG 0.444.x.
JOB_BASE = {
    1: [5, 4, 3, 2, 2, 2, 1, 3, 2],   # Samurai
    2: [3, 4, 2, 4, 3, 2, 3, 1, 2],   # Monk
    3: [2, 3, 2, 4, 5, 2, 3, 2, 1],   # Priest
    4: [2, 2, 3, 5, 4, 1, 2, 3, 2],   # Onmyoji
    5: [3, 3, 5, 3, 2, 1, 2, 2, 3],   # Ninja
    6: [4, 5, 3, 2, 2, 3, 1, 2, 2],   # Smith
    7: [2, 3, 4, 4, 3, 2, 3, 1, 2],   # Physician
}
DEFAULT_JOB_BASE = [3, 3, 3, 3, 3, 2, 2, 2, 2]
FACTION_BASE = {}                         # faction -> 9 values; default all 0
DEFAULT_FACTION_BASE = [0] * 9
STAT_CAP = 10                             # base + allocated must stay < 10 to add (0x66b898)


# cmd 0x23 other subs sent right after the 04 reply (read in 0x283e30; after u8 sub, u16 seq):
#   sub 3    (0x283fa8 -> 0x1ebcb0 read half 0x1ebd48): 18 x string (max 0x21) + bytes (max 0x12)
#   sub 0x18 (0x284790 -> 0x1f40f0 read half 0x1f4160): u8 + string (max 0x101)
#   sub 0x1a (0x284c40): raw u8 count; count 0 -> callback directly; else archive of count
#            x 0x1eff90 records (+ optional 0x1f00e0 records)
#   sub 0xd  (0x284108): u32 (raw, 0x128f90) -> main 0x2869a0 -> UI event 0x48 (0x1d0808):
#            0x1d6770(0) and UI+0x10dc4 = 0, then scene event 0x1b. That undoes the wait mode the
#            quit window 0x62b5e0 ("do not move until it finishes", UIMSG 2.237) enters through
#            0x1d6630 (mode 0xf, UI+0x10dc4 = 1). Request = net 0x4f (builder 0x1c7220), sent with
#            23 11 and 05 06 when quitting in the world; unanswered, the quit waits forever.
CMD23_EMPTY = {
    3: b"\x00" * 18 + b"\x00",
    0x0D: b"\x00\x00\x00\x00",                # res 0
    0x18: b"\x00" + b"\x00",
    0x1A: b"\x00",
}


# Minimal "empty" replies for the world-load burst (header `<cmd> <u8 sub> <u16 seq>`, the reply echoes
# sub and seq). Layouts as the client reads them, each body parses to its last byte
# and reaches the success callback.
WORLDLOAD_EMPTY = {
    # cmd 0x24 (0x287270): 1 = SCMD_GET_BACKPACK: u32, u32, bytes(max 0xc), u8 count
    (0x24, 0x01): b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00" + b"\x00",
    # 0x0f (storage? same item records, no bytes field): u32, u32, u8 count
    (0x24, 0x0F): b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00",
    # 0x1a: bytes(max 0xc)
    (0x24, 0x1A): b"\x00",
    # cmd 0x26 (0x289c80): 1 = SCMD_SKILL_GET_STANDBYLIST: u8 N=0 then a MANDATORY 5x10 grid of 11-byte entries
    (0x26, 0x01): b"\x00" + b"\x00" * (50 * 11),
    # 2 = SCMD_SKILL_GET_BOOKLIST: u16, u16 array (cnt 0), u8 M=0
    (0x26, 0x02): b"\x00\x00" + b"\x00" + b"\x00",
    # cmd 0x2a (0x18f6e8): four groups of (u8 n=0, u32 array cnt 0); the parser ignores the sub byte
    # (echoed). Sub 100 = world-entry request (0x18b880). Sub 5 (news) and the other subs are answered
    # by nobu/misc.py before this table is reached.
    (0x2A, 0x64): b"\x00\x00" * 4,
}


def zone_enter(area, zone, x4=0, x5=0, extra=b""):
    """Server push cmd 0x0B sub 2 (parser 0x19ed18 -> 0x19f138 -> 0x1a7520, handler 0x189760): the
    zone/area enter that moves net state to 0x18, after which the client loads WORLD.EBN (zone != 0) or
    BATTLE.EBN (zone == 0). area must be nonzero and differ from the current net+0x57 (0 at login).
    Layout: u8 area, u16 zone, u8 x4, u8 x5, u8 n, n bytes; two optional trailing u8 omitted."""
    extra = bytes(extra)
    return (vce.put_u8(0x0B) + vce.put_u8(2) + vce.put_u8(area) + vce.put_u16(zone)
            + vce.put_u8(x4) + vce.put_u8(x5) + vce.put_u8(len(extra)) + extra)


def worldload_reply(payload):
    """Seq-echoing minimal reply for a world-load request, or None if not covered."""
    if len(payload) < 4:
        return None
    cmd, sub = payload[0], payload[1]
    body = WORLDLOAD_EMPTY.get((cmd, sub), WORLDLOAD_EMPTY.get((cmd, None)))
    if body is None:
        return None
    return bytes(payload[:4]) + body


def cmd23_reply(payload):
    """Replies for cmd 0x23 requests `23 <sub> <u16 seq> [...]`."""
    r = vce.Reader(payload, 1)
    sub, seq = r.u8(), r.u16()
    if sub == 1:
        return stat_table_reply(payload)
    body = CMD23_EMPTY.get(sub)
    if body is None:
        return None
    return vce.put_u8(0x23) + vce.put_u8(sub) + vce.put_u16(seq) + body


def stat_table_reply(payload):
    r = vce.Reader(payload, 1)
    sub = r.u8()
    seq = r.u16()
    job = r.u8() if len(r.rest()) else 0
    faction = r.u8() if len(r.rest()) else 0
    if sub != 1:
        return None
    jb = JOB_BASE.get(job, DEFAULT_JOB_BASE)
    fb = FACTION_BASE.get(faction, DEFAULT_FACTION_BASE)
    rec = bytes([job, faction] + jb + fb)
    return vce.put_u8(0x23) + vce.put_u8(1) + vce.put_u16(seq) + b"".join(vce.put_u8(b) for b in rec)


# ---------------------------------------------------------------------------- world entry
# C>S cmd 4 (builder 0x190600, empty payload) is sent by net command 0x17 (0x18e770 -> 0x193bc0),
# posted by the character-select scene (0x67a118) after the character is loaded; net+0x30 = 0x17.
# S>C cmd 4 (0x1913f8): u32 res. Main handler 0x189060: res >= 0 -> 0x18b880 (resets the field /
# party / chat state) and, only if net+0x20 == 0, posts {0xba, kind 4}; res < 0 -> error display.
def enter_reply(res=0):
    return vce.put_u8(4) + vce.put_u32(res)


# NOTIFY field/world (cmd 0x0C, u8 sub) as read by the WORLD object's parser 0x19e7b0, branch
# 0x19f658 (subs 0..7, table 0x4211d0), archive codecs; main handler 0x189e80 (table 0x4206b0).
# (The older layouts from 0x191b58 were wrong for this object.) KNOWN:
#   sub 1 FIELD_INFO  0x1a59a0: u8 +0x100, u32 x, u32 y, u32 z, str name (max 0x21)
#                     -> log "NOTIFY_CODE_FIELD_INFO: name=%s,pos=(%d,%d,%d)", UI event 0xd9
#   sub 2             u8 count (signed), count x 0x1a5b40 {u8, u32, u8, u32, u32, u32, u8, str}
#                     -> UI event 0xde per record
#   sub 3             0x1a5df0: u8, u32 -> UI event 0xdf (a1 = the u32)
#   sub 4 WORLD_TIME  0x1a5ec0: u32 date, u32 time -> UI event 0xe6
#   sub 5 WORLD_DATE  0x1eb140: u8 weather (+6), then 0x1a5f90: u16 year, u8 month, u8 day,
#                     u8 hour -> log "(%04d/%02d/%02d %02d, weather(%u))", UI event 0xe7
#   sub 6 / 7         0x1f2e70 / 0x1f2f00 -> UI events 0xe1 / 0xe2 (not decoded)
def notify_field_info(name, x, y, z, b0=0):
    return (vce.put_u8(0xC) + vce.put_u8(1) + vce.put_u8(b0) + vce.put_u32(x) + vce.put_u32(y)
            + vce.put_u32(z) + vce.put_bytes(name.encode("utf-8")))


def notify_world_time(date, t):
    return vce.put_u8(0xC) + vce.put_u8(4) + vce.put_u32(date) + vce.put_u32(t)


def notify_world_date(year, month, day, hour, weather=0):
    return (vce.put_u8(0xC) + vce.put_u8(5) + vce.put_u8(weather) + vce.put_u16(year)
            + vce.put_u8(month) + vce.put_u8(day) + vce.put_u8(hour))


# ---------------------------------------------------------------------------- the field
# Zone enter (0B 02, handler 0x189760): net+0x53 / net+0x57 = area ("id"), net+0x50 = zone
# ("map"): log "go next zone(id=%u,map=%u)" (0x18d3a8), then 0x94 event 0x2e -> 0x21bad0 maps
# the map id to a CMAP file: 1..21 -> CMAP0\CMAP001..021 (the 21 provinces, same numbering as
# STRDAT table 4: 10 = Owari), 101.. -> CMAP1 (towns), 201.. CMAP2, 301.. CMAP3, 401.. CMAP4,
# 500.. CMAP5, 600.. CMAP6. Positions are u32 world units = the float coordinates of the CMAP
# data (vitof, no scale); a province field is about 200000 x 300000 (CMAP section 0 header).
# FIELD_SPAWN: the first point of each province map's OLS0 table 3 (CMAPxxx.BIN, section 1 =
# "OLS0", 20-byte entries {u16 type 0, i16 -1, f32 x, y, z, ...}). HYPOTHESIS that these are
# start points; any x/z inside the field works because the client drops the player onto the
# ground under x/z (0x60bfa8 -> 0x16bee0).
FIELD_SPAWN = {
    1: (51240, 5070, 230799), 2: (108381, 5640, 132002), 3: (138213, 4985, 104702),
    4: (84295, 3564, 118909), 5: (138533, 4800, 71762), 6: (62567, 3550, 145904),
    7: (69719, 2600, 207470), 8: (93625, 2800, 207091), 9: (87571, 4900, 106290),
    10: (100460, 3455, 95716), 11: (107586, 5396, 86817), 12: (85122, 3300, 87445),
    13: (106484, 3900, 234887), 14: (93132, 4085, 96352), 15: (145936, 3550, 110099),
    16: (113724, 3150, 29445), 17: (48785, 4650, 69892), 18: (79845, 5800, 72155),
    19: (138662, 3300, 94570), 20: (79992, 5000, 50972), 21: (51043, 3270, 57748),
}
# Towns (CMAP1\CMAP1xx). Per the NPC data survey Owari's town is Nagoya = map 110 (castle 410,
# Nanban quarter 428). Spawn = OLS0 table 2 entry 0 of CMAP110, just inside the south gate
# (gate halves at z 115000, angle 180). HYPOTHESIS that T2 points are walkable standing spots.
FIELD_SPAWN[110] = (103713, 3200, 112594)
# Every clan's home town (nobu/towns.py): spawn = the arrival point of the town's first gate to its province field
# (for Nagoya the same point as above).
FIELD_SPAWN.update({m: towns.spawn(m) for m in towns.TOWNS})
TOWN_PROVINCE = {m: t["province"] for m, t in towns.TOWNS.items()}   # town map -> its province; area: area_for()
PROVINCE_TOWN = {v: k for k, v in TOWN_PROVINCE.items()}

DEFAULT_AREA = int(os.environ.get("NOBU_AREA", "1"))   # fallback only; see area_for()


def area_for(zone):
    """Area byte for a field (0B 02 / 0B 01 / 0B 0a, net+0x57, P+0x18). The status screen shows
    it as "Area <province>" (area 1 shows "Echigo"), so for the province fields 1..21
    it is the province id = the map id. NOBU_AREA overrides. Every map: the place index of the
    map (0x21bad0 index + 1, Nagoya 110 -> 31), because a zone change (0B 02) only reloads when
    the area differs (zones.area_of)."""
    if os.environ.get("NOBU_AREA"):
        return int(os.environ["NOBU_AREA"])
    from . import zones
    return zones.area_of(zone)


def faction_of(c):
    return (list((c or {}).get("looks", [])) + [0] * 5)[4] if c else 0


def home_town(c):
    """The clan's home town map (towns.FACTION_TOWN: Oda -> Nagoya 110, Uesugi -> Kasugayama 101, ...), or None."""
    return towns.home_town(faction_of(c))


def home_zone(c):
    """Starting map for a character: NOBU_ZONE if set, else its clan's home town (towns.py; NOBU_START_TOWN=0: that
    town's province field), else the first province its faction owns in PROVINCE_OWNERS, else 1."""
    if os.environ.get("NOBU_ZONE"):
        return int(os.environ["NOBU_ZONE"])
    faction = faction_of(c)
    town = towns.home_town(faction)
    if town is not None:
        if os.environ.get("NOBU_START_TOWN", "1") == "1":
            return town
        return towns.TOWNS[town]["province"]
    for prov, owner in enumerate(PROVINCE_OWNERS):
        if prov and owner == faction:
            if os.environ.get("NOBU_START_TOWN", "1") == "1" and towns.province_town(prov) is not None:
                return towns.province_town(prov)     # the province's town (NOBU_TOWNS=0: only Nagoya)
            return prov
    return 1


def char_position(c):
    """(zone, x, y, z, dir): the saved position (from the client's 05 05 moves) if any, else
    the spawn point of the character's home field. NOBU_SPAWN_X/Y/Z override the spawn point."""
    if c and c.get("pos") and os.environ.get("NOBU_FORCE_HOME") != "1":
        p = [int(v) for v in (list(c["pos"]) + [0] * 5)[:5]]
        if p[2] > 0xFFFF:       # saved before: the packed 05 05 word area<<16 | y
            p[2] &= 0xFFFF
        return tuple(p)
    zone = home_zone(c)
    x, y, z = FIELD_SPAWN.get(zone, FIELD_SPAWN[1])
    x = int(os.environ.get("NOBU_SPAWN_X", x))
    y = int(os.environ.get("NOBU_SPAWN_Y", y))
    z = int(os.environ.get("NOBU_SPAWN_Z", z))
    return zone, x, y, z, 0


# NOTIFY_CHAR (cmd 0x0B, u8 sub; parser 0x19e7b0 -> 0x19ed18; main handler 0x1893f0). KNOWN:
#   sub 1 CHAR_LOCATION (0x19eeb0): u8 count (signed), count x record 0x1a3bb0:
#       u32 uid +0, u32 cid +4, [0x18ff70: u8 area +8, u32 x +0xc, u32 y +0x10, u32 z +0x14,
#       u16 dir +0x18], str family +0x1c (max 0x11), str given +0x2d (max 0x11),
#       u8 level +0x3e, u8 power(faction) +0x3f, u8 job +0x40, u8 sex +0x41, u8 +0x42.
#     Handler: uid == net+0x44 (us) -> net+0x324.. = area/x/y/z/dir; then if area != 0 and
#     area != net+0x57 (current area) -> UI event 0xc5 (other area); else uid == us -> UI event 7
#     (0x1d98c0: P+0x1c.. = x/y/z, +0x28 dir, +0x18 area, +0x2f level, +0x30 faction,
#     +0x2d job, +0x2e sex, +0x2a = +0x42, names; broadcast 0x94 event 0x1d -> 0x21c000: the
#     player object (task type 8, id cid) event 0x1d (x, y, z) and task 9 event 9 (camera)),
#     other uid -> 0xc7; uid 0 -> NPC kinds 0xc0 / 0xa6 / 0xd5 by cid & 0xff000000.
#   sub 2 zone enter (see zone_enter).
#   sub 8 CHAR_LOCATION_UPDATE (0x19ef88): u8 count, count x 0x1a6f10: u8 kind +0, u32 cid +4,
#     u32 x +8, u32 y +0xc, u32 z +0x10, u16 dir +0x14, u8 +0x16. kind 1 and cid == net+0x48
#     (us) -> UI event 0xc (0x1d9a10: P xyz, broadcast 0x1d); kind 1 other -> 0xc8; 5 -> 0xc1;
#     6 -> 0xd6 / 0xa7.
#   sub 9 CHAR_LOCATION_UPDATE_F: same with u16 x/y/z (<< 3) - not built here.
def location_record(c, uid, pos, area=None, level=1, b42=0):
    zone, x, y, z, d = pos
    lk = list(c.get("looks", [])) + [0] * 20
    sex, job, faction = lk[2], lk[3], lk[4]
    w = W().u32(uid).u32(c["cid"])
    w.u8(area_for(zone) if area is None else area).u32(x).u32(y).u32(z).u16(d)
    w.str(c["family"]).str(c["given"])
    w.u8(c.get("level", level)).u8(faction).u8(job).u8(sex).u8(b42)
    return w.bytes()


def notify_char_location(records):
    """0B 01 u8 count + records (location_record)."""
    return vce.put_u8(0x0B) + vce.put_u8(1) + vce.put_u8(len(records)) + b"".join(records)


def notify_location_update(cid, x, y, z, d=0, kind=1, b16=0):
    """0B 08 u8 count=1 + {u8 kind, u32 cid, u32 x, u32 y, u32 z, u16 dir, u8}. For our own
    cid with kind 1 this moves the local player (UI event 0xc) - a server-side warp/correction."""
    return (vce.put_u8(0x0B) + vce.put_u8(8) + vce.put_u8(1) + vce.put_u8(kind) + vce.put_u32(cid)
            + vce.put_u32(x) + vce.put_u32(y) + vce.put_u32(z) + vce.put_u16(d) + vce.put_u8(b16))


def spawn_pushes(c, uid, pos=None):
    """What makes the local player appear (observed client behaviour):
    SCMD_CHAR_LOAD reply (05 02) -> main 0x188980 -> UI event 0 (0x1cf458: 0x2093c0 fills P
    from the load, then broadcast 0x94 event 0x17 with P) -> task 0x94 (0x21c340, case 0x17 at
    0x21c690) -> task 0x97/1 event 2 (world root 0x1805a0, 0x1806e0) -> task 0x97/5 event 0
    (0x60adc0) -> 0x60bd80 creates the player object (type 8, id = P+0x14 = cid,
    gp-0x7100 = cid) at P's x/z on the ground. UI event 0 has no other poster, and the tasks
    0x94 / 0x97 only exist once World.bin runs, so the load reply the client got at character
    select could not spawn anyone: the server has to send it again inside the world.
    Order: CHAR_LOCATION (P area/pos/level/faction + camera), the load reply (spawn), then
    CHAR_LOCATION again so the now existing player object gets event 0x1d at the position."""
    pos = pos or char_position(c)
    zone, x, y, z, d = pos
    loc = notify_char_location([location_record(c, uid, pos)])
    info = notify_char_info([char_info_record(c, uid, pos)])
    return [loc, info, load_reply(0, c, (area_for(zone), x, y, z)), loc]


# NOTIFY_CHAR sub 0xa CHAR_INFO: parser 0x19edd8: u8 count (signed),
# count x record 0x1a2500 (read half 0x1a2ad0):
#   u32 uid +0, u32 cid +4, [0x18ff70 area/x/y/z/dir +8], str family +0x1c, str given +0x2d
#   (max 0x11), u8 +0x3e level, +0x3f power(faction), +0x40 job, +0x41 sex, +0x43, +0x44,
#   bytes(max 0xf) +0x45 PARAMS, u16 +0x54, u8 +0x56, u16 +0x58, u8 +0x5a, u8 +0x5b, u16 +0x5c,
#   u8 +0x5e, u8 +0x5f, u8 +0x42, u8 +0x60..+0x66, u8 +0x6d, u8 +0x6f, u8 +0x67, u8 +0x68,
#   if uid != 0: u8 +0x69, u8 +0x6a, u32 array (max 3) +0x88; else u8 +0x6b, +0x6c, +0x6e;
#   u8 +0x70; if +0x70 != 2: u32 +0x74, u32 +0x78; u8 +0x7c; bytes(max 0xf) +0x7d.
# Main handler 0x189ac8: same area test as CHAR_LOCATION; our uid (net+0x44) AND our cid
# (net+0x48) -> UI event 0xb (0x1cf570) -> 0x1d96b0 fills P: position, level/faction/job/sex,
# +0x42 -> P+0x46, +0x43 -> P+0x31, +0x54 -> P+0x58 ...; P vt 0x74 (0x1db3b0: +0x54..+0x6a ->
# P+0x58..+0xa0), vt 0x70 (0x1d9fe0: +0x7c/+0x7d.. boarding/ship -> P+0x34), vt 0x78 (0x209d10:
# the 15 PARAMS -> P+0x10fa4), vt 0x7c; then broadcast 0x94 event 0x1d (move the player there).
# PARAMS are read with P->vt[0x28](i) (0x209c80). The local player's move speed (0x5f7de4) is 10
# * PARAMS[8] / 100 (0x20b880; 0 while the paralysis timer P+0x10f98 runs), capped at 3.5 in walk
# mode, then 0x15b040 sets actor+0x194 = speed * 2 (gp-0x7eec). PARAMS[8] == 0 -> max speed 0 ->
# the walk animation plays without displacement. Nothing else fills PARAMS (the load path
# 0x2093c0 does not copy the list summary +0x45). Other indexes: [0] == 1 -> 0x1da540 slows/stops
# the actor (x0.3 / 0), [6] / 100 (max 1) is used on slopes (0x5fcf24), [0xe] != 0 sets actor
# flags 0x60000 at spawn (0x60be94); rest unknown.
MOVE_SPEED = int(os.environ.get("NOBU_MOVE_SPEED", "100"))     # PARAMS[8]; 35 == the walk cap


def char_params(c=None):
    """The 15 PARAMS bytes (KNOWN: [8] move speed, [6] slope factor; others 0). With a character c:
    lifecycle.overlay_params ([0] = 2 for a ghost, [1] = 1 when it sees ghosts)."""
    p = [0] * 15
    p[6] = 100
    p[8] = int(os.environ.get("NOBU_MOVE_SPEED", MOVE_SPEED))
    if os.environ.get("NOBU_PARAMS"):                       # 30 hex digits, overrides all
        p = list(bytes.fromhex(os.environ["NOBU_PARAMS"]))[:15] + [0] * 15
    if c is not None:
        from . import lifecycle
        return lifecycle.overlay_params(c, bytes(p[:15]))
    return bytes(p[:15])


def char_info_record(c, uid, pos, w54=None, w58=None, params=None):
    # c["b6b"] / c["b6c"] (uid-0 records only): record +0x6b -> entry +0x2c, +0x6c -> entry +0x2b.
    # The attack test 0x2733a0 (entry vt 0x40): +0x2c == 2 -> never attackable, so the action hint
    # 0x62d3d0 falls through to Look / Talk; with 0 a nonzero faction is attackable when +0x6c == 0.
    """0x1a2500 record for NOTIFY_CHAR sub 0xa.
    u16 +0x54 / +0x58 are NOT Life / Ki: 0x1d96b0 / vt 0x74 copy them to P+0x58 / P+0x60, which P
    vt 0x20 (0x1db0d0) turns into the appearance struct bytes [0..1] / [8] the player model is
    built from (0x60bef0 -> 0x5ff2d0). 100 / 50 there make the player invisible (the player
    object gets no model children). They stay 0 like the load path
    left them (NOBU_INFO_W54 / NOBU_INFO_W58 to experiment). Face / hair / body sliders: see
    appearance(); the uid-0 branch has no +0x69 / +0x6a."""
    zone, x, y, z, d = pos
    lk = list(c.get("looks", [])) + [0] * 20
    sex, job, faction = lk[2], lk[3], lk[4]
    ap = appearance(c)
    # c["model"]: MOB model id for +0x54 (0 = person), c["model_scale"]: +0x56 (50 = authored size); enemies.py
    # CREATURE_MODELS /. 0x253dd0 builds the MOB model (0x256410) when A[0..1] != 0.
    model = int(c.get("model") or 0)
    if w54 is None and model:
        w54 = model
    b56 = int(c.get("model_scale", 50)) & 0xFF if model else ap["b56"]
    life = int(os.environ.get("NOBU_INFO_W54", "0")) if w54 is None else w54
    ki = int(os.environ.get("NOBU_INFO_W58", "0")) if w58 is None else w58
    w = W().u32(uid).u32(c["cid"])
    w.u8(area_for(zone)).u32(x).u32(y).u32(z).u16(d)
    w.str(c["family"]).str(c["given"])
    w.u8(c.get("level", 1)).u8(faction).u8(job).u8(sex).u8(0).u8(0)   # +0x3e..+0x41 +0x43 +0x44
    w.raw(char_params(c) if params is None else bytes(params), 0xF)    # +0x45 PARAMS (upkeep: speed)
    w.u16(life).u8(b56).u16(ki).u8(0).u8(0).u16(0).u8(0).u8(0)         # +0x54..+0x5f
    w.u8(0)                                                            # +0x42
    for _ in range(7):                                                 # +0x60..+0x66
        w.u8(0)
    w.u8(0).u8(0).u8(ap["b67"]).u8(ap["b68"])                          # +0x6d +0x6f +0x67 +0x68
    if uid != 0:
        # u32[3] +0x88: vt 0x7c (0x1db4c0) memcpys 12 bytes from +0x88 to P+0xa4 and sets
        # P+0x38 = +0x90, so an empty array leaves stack garbage there (P+0xac = 0x01ffec34 live)
        w.u8(ap["b69"]).u8(ap["b6a"]).u32s([0, 0, 0])                  # +0x69 +0x6a, u32[3] +0x88
    else:
        w.u8(c.get("b6b", 0)).u8(c.get("b6c", 0)).u8(0)                # +0x6b +0x6c +0x6e
    w.u8(0).u32(0).u32(0)                                              # +0x70 (!= 2) +0x74 +0x78
    w.u8(0).raw(b"", 0xF)                                              # +0x7c not aboard, +0x7d
    return w.bytes()


def notify_logout(uid, cid, res=0):
    """NOTIFY_CHAR sub 5 (parser 0x19f430: three raw u32; main 0x1893f0 -> case 0x1899c0): res
    (the 2nd u32) == 0 -> UI event 0x142 (0x1d3b28, UI reset before leaving), then UI 0x47
    (0x1d07e0) -> res == 0 completes waiter 0x1c, which the quit window 0x62b5e0 registered
    through 0x1d6630. Without it the countdown reaches 0 and nothing happens. The 1st / 3rd u32
    are not read on this path; uid / cid are a HYPOTHESIS."""
    return (vce.put_u8(0x0B) + vce.put_u8(5) + vce.put_u32(uid) + vce.put_u32(res)
            + vce.put_u32(cid))


def notify_char_info(records):
    """0B 0a u8 count + char_info_record()s."""
    return vce.put_u8(0x0B) + vce.put_u8(0x0A) + vce.put_u8(len(records)) + b"".join(records)


def decode_move(payload):
    """C>S 05 05 (builder 0x190490 via 0x1979f0 / 0x193b50, net commands 0x1b and 0x1e): the
    local player's position report: u32 x, u32 y, u32 z, u16 dir, u8 flag (the 0x18ff70
    block without its area byte). Net command 0x1e also stores it in net+0x328.. (m_ptClient).
    No reply parser exists (0x19c330 has no sub 5): the server only records it.
    The y word carries the area in its high 16 bits (e.g. 0x00010d80 = area 1, y 3456;
    0x000a0fd7 = area 10, y 4055), so y = word & 0xffff, area = word >> 16 (HYPOTHESIS that the high half is the area;
    it matched both live samples).
    flag: bit 0x80 = walk mode (player +0x2b0 & 0x800000, 0x5fcaa8)."""
    r = vce.Reader(payload, 2)
    x, yw, z, d = r.u32(), r.u32(), r.u32(), r.u16()
    return dict(x=x, y=yw & 0xFFFF, area=yw >> 16, z=z, dir=d,
                flag=r.u8() if not r.done() else None)


def encode_move(x, y, z, d=0, flag=0, area=0):
    return (vce.put_u8(5) + vce.put_u8(5) + vce.put_u32(x) + vce.put_u32((area << 16) | y) + vce.put_u32(z)
            + vce.put_u16(d) + vce.put_u8(flag))
