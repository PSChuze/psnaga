"""Houses: the cmd 0x1b House screen, the barn, furniture layout, access lists.

KNOWN = how the client reads it; HYPOTHESIS = a reading the client does not settle;
DESIGN = ours.

KNOWN (client)
- Only three House requests are posted by this build (0x1d9030 posters in the house model 0x3c0e90..0x3c0f44):
    1b 01 <seq>                                    kind 0x66: get the house (Arrange menu 0x6909ec, 0x690a78)
    1b 03 <seq> bytes(layout)                      kind 0x68: save the furniture layout (H+0x1a4)
    1b 04 <seq> u16 idx, u16 count, u8 dir         kind 0x69: barn transfer (0x6a12b0 dir 1, 0x6a1670 dir 0)
  Builders for 1b 02 / 05..09 / 0e / 0f exist (0x1c99c0.., kinds 0x67, 0x6a..0x70) but nothing posts them here.
- Replies (0x19aa90, sub -> waiter message 0x65 + sub): 1 = i32 res, u32, house state; 2 / 3 = i32 res (the
  callback 0x3c10a0 does nothing); 4 = i32 res, u32 item id, u16 count, u8 flag; 5..9 as in shops.MARKET_EMPTY.
- House state (0x1aa500; object 0xf00 bytes, class 0x450230): layout (0x1aa240), info (0x1a8780), barn list
  (0x1a99d0, max 1000 read, 100 fit):
    layout: u16 a (<= 10), u16 b (<= 510), u16 w, a x {u16, u8, u8, u8, u8, u16}, b x {u16, u8, u8, u8, u8}
    info:   u8 kind, str name (max 0x3d), str owner (max 0x21), u16, u8 access, u32
    barn:   u16 n, u16, n x {u16 item id, u16 count (< 999)}
  The 1b 01 callback (0x3c10c0 -> 0x3c0f60) copies layout and info into the house model H = UI+0x1b29c; the
  barn rows H+0xc are changed by the 1b 04 reply (0x3c0fb0: flag 1 adds {id, count}, flag 0 removes).
- Info access byte (+1): 1 = Public, 2 = Limited, other = Closed (0x69faa4, UIMSG
  2.438..2.440). Name +2 and owner +0x3f are the House Info window's house rows (0x69fa34,
  0x69fa68).
- S>C `18 01 <seq> i32 res, u32, house state` (parser 0x19a740) -> UI 0x129 (0x1d3e00) creates H (0xe58 bytes)
  when absent and copies layout / info: the push that makes the client know its house. A zone change
  (UI 0x142) deletes H again (0x1d3b64).
- Item types (STRDAT 3): 89 Furniture, 90 House, 91 Wallpaper; UIMSG 2.456 "Only furniture can go in the
  barn". Furniture item names are STRDAT 0 4540..4811.
HYPOTHESIS
- The layout's u16 in both entry kinds is a furniture item id and the four bytes a grid position / turn; the
  8-byte entries (<= 10) are big pieces or buildings; w is the wallpaper. Only the client interprets them, so
  the server stores the layout exactly as sent and gives it back.
- dir 1 = put backpack item idx into the barn, dir 0 = take barn row idx out.
DESIGN
- One house per character in chars.json c["house"]; created on first use; a few starter pieces go into
  the backpack (the client's barn rows only fill through 1b 04 deposits).
- 1b 03 stores the layout (NOBU_HOUSE_STRICT=1 also refuses layouts that place more of an id than the barn has).
- Access lists and the house name / access mode are kept by the server and changed by chat commands,
  because the client's own requests for them are not posted by this build.
"""
import os
import unicodedata

from . import items, misc, social, vce

CMD = 0x1B
CMD_ENTER = 0x18
T_FURNITURE = 89
T_WALLPAPER = 91
BARN_MAX = 100              # 0x68f780: row index < 100; the object holds 100 x 4 bytes
BARN_COUNT_MAX = 998        # 0x1a8c68: count < 999
BIG_MAX = 10                # layout +0xbfa .. +0xc4a, 8 bytes each
SMALL_MAX = 510             # layout +6 .. +0xbfa, 6 bytes each
NAME_MAX = 0x3d
OWNER_MAX = 0x21
ACCESS = {1: "Public", 2: "Limited", 3: "Closed"}
SUB_NAMES = {1: "GET", 2: "CHECK_LAYOUT", 3: "SAVE_LAYOUT", 4: "BARN", 5: "sub 5 (str,u32,u32)",
             6: "sub 6 (str,u32,u32)", 7: "sub 7 (str,u32 x4)", 8: "sub 8 (str,u32 x4)", 9: "ACCESS_LISTS",
             0xE: "sub 0x0e (str)", 0xF: "sub 0x0f (u32)"}
# DESIGN: furniture a new house owner gets in the backpack (STRDAT 0 names): Hearth, Andon Lamp, Small Tatami,
# Partition, Small Desk. Period rule: furniture goes into the barn before it can be placed, and the client's barn
# rows only fill through 1b 04 deposits, so the pieces start in the backpack.
STARTER_BARN = [(4685, 1), (4688, 2), (4682, 2), (4662, 1), (4590, 1)]
# empty bodies of the subs this server does not act on (the layouts of shops.MARKET_EMPTY, res 0)
EMPTY = {
    2: vce.put_u32(0),
    5: vce.put_u32(0) + vce.put_u32(0) * 3 + b"\x00",
    6: vce.put_u32(0) + vce.put_u32(0),
    7: vce.put_u32(0) * 6,
    8: vce.put_u32(0) * 6,
}


def enabled():
    return os.environ.get("NOBU_HOUSE", "1") == "1"


def _save(store):
    items.save(store)


def _name(c):
    return f"{c.get('family', '')}{c.get('given', '')}".strip()


HOUSE_SUFFIX = "'s House"


def default_name(c):
    """The name a new house gets: "<family> <given>'s House", the owner cut first so the suffix survives the clip."""
    owner = f"{c.get('family', '')} {c.get('given', '')}".strip()
    room = NAME_MAX - 1 - len(HOUSE_SUFFIX)
    return misc.clip_sjis(owner, room) + HOUSE_SUFFIX


# ---------------------------------------------------------------------------- state
def state(c, store=None):
    """c["house"], created on first use (DESIGN)."""
    h = c.get("house")
    if not isinstance(h, dict):
        h = dict(name=default_name(c), access=1, kind=1, grade=1,
                 layout=dict(wall=0, big=[], small=[]), barn=[], allowed=[], banned=[])
        c["house"] = h
        if os.environ.get("NOBU_HOUSE_STARTER", "1") == "1":
            inv = items.inventory(c, store)
            for iid, n in STARTER_BARN:
                give_item(inv, iid, n)
        _save(store)
    h.setdefault("layout", dict(wall=0, big=[], small=[]))
    h.setdefault("barn", [])
    h.setdefault("allowed", [])
    h.setdefault("banned", [])
    return h


def barn_count(h, iid):
    return sum(int(n) for i, n in h["barn"] if int(i) == iid)


def placed_counts(layout):
    out = {}
    for e in list(layout.get("big", [])) + list(layout.get("small", [])):
        out[int(e[0])] = out.get(int(e[0]), 0) + 1
    return out


# ---------------------------------------------------------------------------- wire
def w_layout(layout):
    big = list(layout.get("big", []))[:BIG_MAX]
    small = list(layout.get("small", []))[:SMALL_MAX]
    out = vce.put_u16(len(big)) + vce.put_u16(len(small)) + vce.put_u16(int(layout.get("wall", 0)))
    for e in big:
        e = (list(e) + [0] * 6)[:6]
        out += vce.put_u16(e[0]) + bytes(v & 0xFF for v in e[1:5]) + vce.put_u16(e[5])
    for e in small:
        e = (list(e) + [0] * 5)[:5]
        out += vce.put_u16(e[0]) + bytes(v & 0xFF for v in e[1:5])
    return out


def r_layout(r):
    """0x1aa240 read order: u16 a, u16 b, u16 w, a x 8-byte, b x 6-byte (bounds of the client object)."""
    a, b, wall = r.u16(), r.u16(), r.u16()
    if a > BIG_MAX or b > SMALL_MAX:
        raise vce.WireError(f"layout counts {a} / {b} exceed the client object")
    big = [[r.u16(), r.u8(), r.u8(), r.u8(), r.u8(), r.u16()] for _ in range(a)]
    small = [[r.u16(), r.u8(), r.u8(), r.u8(), r.u8()] for _ in range(b)]
    return dict(wall=wall, big=big, small=small)


def w_info(c, h):
    name = misc.clip_sjis(str(h.get("name", "")), NAME_MAX - 1).encode("utf-8")
    owner = misc.clip_sjis(_name(c), OWNER_MAX - 1).encode("utf-8")
    return (vce.put_u8(int(h.get("kind", 1))) + vce.put_bytes(name) + vce.put_bytes(owner)
            + vce.put_u16(int(h.get("grade", 1))) + vce.put_u8(int(h.get("access", 1)))
            + vce.put_u32(int(h.get("info_u32", 0))))


def w_barn(h):
    rows = [(int(i), int(n)) for i, n in h["barn"] if int(n) > 0][:BARN_MAX]
    return (vce.put_u16(len(rows)) + vce.put_u16(0)
            + b"".join(vce.put_u16(i) + vce.put_u16(min(n, BARN_COUNT_MAX)) for i, n in rows))


def w_state(c, h):
    """0x1aa500 order: layout, info, barn list."""
    return w_layout(h["layout"]) + w_info(c, h) + w_barn(h)


def _head(cmd, sub, seq):
    return vce.put_u8(cmd) + vce.put_u8(sub) + vce.put_u16(seq)


def get_reply(seq, c, h):
    return _head(CMD, 1, seq) + vce.put_u32(0) + vce.put_u32(int(c.get("cid", 0))) + w_state(c, h)


def enter_push(c, h, seq=0):
    """18 01: the house data push that creates the client's house model (UI 0x129)."""
    return _head(CMD_ENTER, 1, seq) + vce.put_u32(0) + vce.put_u32(int(c.get("cid", 0))) + w_state(c, h)


def barn_reply(seq, res, iid=0, count=0, flag=0):
    return _head(CMD, 4, seq) + vce.put_u32(res & 0xFFFFFFFF) + vce.put_u32(iid) + vce.put_u16(count) + vce.put_u8(flag)


def w_entry(e):
    """0x1a9f60: u32, u32, u32, str name (max 0x21)."""
    name = misc.clip_sjis(str(e.get("name", "")), OWNER_MAX - 1).encode("utf-8")
    return vce.put_u32(int(e.get("cid", 0))) + vce.put_u32(int(e.get("uid", 0))) + vce.put_u32(0) + vce.put_bytes(name)


def lists_reply(seq, h):
    """1b 09: i32 res, 0x1aa0c0 = u8 a, u8 b, (a + b) x entry. HYPOTHESIS: a = allowed, b = banned."""
    al, bn = h["allowed"][:50], h["banned"][:50]
    return (_head(CMD, 9, seq) + vce.put_u32(0) + vce.put_u8(len(al)) + vce.put_u8(len(bn))
            + b"".join(w_entry(e) for e in al + bn))


# ---------------------------------------------------------------------------- requests
def view(ctx):
    """The client's barn rows (H+0xc) as this session built them: the 1b 01 / 18 01 copy (0x3c0f60) leaves the
    list alone, so it starts empty and only 1b 04 replies change it (0x3c0fb0; add merges by id then fills the
    first free slot, 0x1a9630; remove by row, 0x1a9740; both compact). NOBU_HOUSE_VIEW=full starts it as the
    whole stored barn instead."""
    v = ctx.get("house_view")
    if v is None:
        v = []
        if os.environ.get("NOBU_HOUSE_VIEW") == "full" and ctx.get("char"):
            v = [[int(i), int(n)] for i, n in state(ctx["char"]).get("barn", []) if int(n) > 0]
        ctx["house_view"] = v
    return v


def rows_add(rows, iid, n):
    """0x1a9630: top up rows of the same id (count < 999), then new rows; False when 100 rows are full."""
    left = n
    for row in rows:
        if left and int(row[0]) == iid and int(row[1]) < BARN_COUNT_MAX:
            take = min(left, BARN_COUNT_MAX - int(row[1]))
            row[1] = int(row[1]) + take
            left -= take
    while left:
        if len(rows) >= BARN_MAX:
            return False
        take = min(left, BARN_COUNT_MAX)
        rows.append([iid, take])
        left -= take
    return True


def rows_take(rows, iid, n):
    """Remove n of iid (last rows first); returns how many were removed. Empty rows are dropped (compacted)."""
    got = 0
    for row in reversed(rows):
        if got < n and int(row[0]) == iid:
            take = min(n - got, int(row[1]))
            row[1] = int(row[1]) - take
            got += take
    rows[:] = [r for r in rows if int(r[1]) > 0]
    return got


def barn_transfer(ctx, store, idx, count, direction):
    """1b 04. Returns (res, item id, count, flag, pushes, text)."""
    from . import shops
    c = ctx["char"]
    h = state(c, store)
    inv = items.inventory(c, store)
    rows = view(ctx)
    count = max(1, int(count))
    if direction == 1:                                           # backpack -> barn
        it = items.by_index(inv, idx)
        if it is None or it.get("pos") is not None:
            return -1, 0, 0, 1, [], "That item cannot go in the barn."
        if int(it.get("type", 0)) not in (T_FURNITURE, T_WALLPAPER) and os.environ.get("NOBU_HOUSE_ANY_ITEM") != "1":
            return -1, 0, 0, 1, [], "Only furniture can go in the barn."
        have = int(it.get("count", 1))
        count = min(count, have)
        iid = int(it["id"])
        if not rows_add([list(r) for r in h["barn"]], iid, count) or not rows_add([list(r) for r in rows], iid, count):
            return -1, 0, 0, 1, [], "The barn is full."
        rows_add(h["barn"], iid, count)
        rows_add(rows, iid, count)
        it["count"] = have - count
        if it["count"] <= 0:
            inv["items"].remove(it)
        _save(store)
        return (0, iid, count, 1, [shops.notify_counts([(it["idx"], max(0, it["count"]))])],
                f"Put {items.item_name(iid)} x{count} in the barn.")
    if not 0 <= idx < len(rows):                                 # barn -> backpack
        return -1, 0, 0, 0, [], "Nothing there in the barn."
    iid = int(rows[idx][0])
    count = min(count, int(rows[idx][1]), barn_count(h, iid))
    if count <= 0:
        return -1, 0, 0, 0, [], "Nothing there in the barn."
    got = give_item(inv, iid, count)
    if got is None:
        return -1, 0, 0, 0, [], "Cannot carry any more items."
    rows_take(h["barn"], iid, count)
    rows[idx][1] = int(rows[idx][1]) - count
    rows[:] = [r for r in rows if int(r[1]) > 0]
    _save(store)
    return 0, iid, count, 0, [shops.notify_items([got])], f"Took {items.item_name(iid)} x{count} out of the barn."


def give_item(inv, iid, count):
    """Put count of furniture iid in the backpack (top up a stack, else a free slot). The changed item or None."""
    same = next((it for it in inv["items"] if it["id"] == iid and it.get("flags", 0) & items.FLAG_STACK
                 and int(it.get("count", 1)) + count <= items.MAX_STACK), None)
    if same is not None:
        same["count"] = int(same.get("count", 1)) + count
        return same
    used = {it.get("idx") for it in inv["items"]}
    slot = next((i for i in range(items.CAPACITY) if i not in used), None)
    if slot is None or count > items.MAX_STACK:
        return None
    it = dict(id=iid, type=T_FURNITURE, cat=0, count=count, flags=items.FLAG_STACK, dur=0, dur_max=0, idx=slot)
    inv["items"].append(it)
    return it


def save_layout(ctx, store, layout, commit=True):
    """1b 02 / 03 body. Pieces newly placed leave the barn (and the client's rows), stowed pieces come back
    (NOBU_HOUSE_TRACK=0: store the layout only). NOBU_HOUSE_STRICT=1 refuses a layout that places pieces the
    barn does not hold. Returns (res, text)."""
    h = state(ctx["char"], store)
    track = os.environ.get("NOBU_HOUSE_TRACK", "1") == "1"
    old, new = placed_counts(h["layout"]), placed_counts(layout)
    if os.environ.get("NOBU_HOUSE_STRICT") == "1":
        for iid, n in new.items():
            if n - old.get(iid, 0) > barn_count(h, iid):
                return -1, f"{items.item_name(iid)}: not enough in the barn; layout not saved."
    if not commit:
        return 0, None
    if track:
        rows = view(ctx)
        for iid in set(old) | set(new):
            d = new.get(iid, 0) - old.get(iid, 0)
            if d > 0:
                rows_take(h["barn"], iid, d)
                rows_take(rows, iid, d)
            elif d < 0:
                rows_add(h["barn"], iid, -d)
                rows_add(rows, iid, -d)
    h["layout"] = layout
    _save(store)
    return 0, None


def answer(payload, ctx, store=None):
    """cmd 0x1b requests (a character must be loaded) and the `house` chat commands; None when not ours."""
    if not enabled() or len(payload) < 4:
        return None
    if payload[:2] == b"\x23\x08":
        return chat(payload, ctx, store)
    if payload[0] != CMD or not (ctx or {}).get("char"):
        return None
    sub = payload[1]
    c = ctx["char"]
    try:
        r = vce.Reader(payload, 2)
        seq = r.u16()
        if sub == 1:
            return [get_reply(seq, c, state(c, store))]
        if sub in (2, 3):
            body = r.bytes(0x1060)
            try:
                layout = r_layout(vce.Reader(body))
            except vce.WireError:
                layout = r_layout(vce.Reader(payload, 4))     # tolerate a body without the length header
            res, text = save_layout(ctx, store, layout, commit=(sub == 3))
            ctx.setdefault("house_log", []).append(("layout", sub, res, len(layout["big"]), len(layout["small"])))
            out = [_head(CMD, sub, seq) + vce.put_u32(res & 0xFFFFFFFF)]
            return out + ([social.notify_system(text)] if text else [])
        if sub == 4:
            idx, count, direction = r.u16(), r.u16(), r.u8()
            res, iid, n, flag, pushes, text = barn_transfer(ctx, store, idx, count, direction)
            ctx.setdefault("house_log", []).append(("barn", direction, idx, count, res))
            return [barn_reply(seq, res, iid, n, flag)] + pushes + ([social.notify_system(text)] if text else [])
        if sub == 9:
            return [lists_reply(seq, state(c, store))]
        if sub == 0xE:                                             # HYPOTHESIS: house name; no reply parser
            h = state(c, store)
            h["name"] = misc.clip_sjis(r.bytes(0x100).decode("utf-8", "replace"), NAME_MAX - 1)
            _save(store)
            return []
        if sub == 0xF:                                             # HYPOTHESIS: access mode; no reply parser
            v = r.u32()
            if v in ACCESS:
                state(c, store)["access"] = v
                _save(store)
            return []
        if sub in EMPTY:
            return [_head(CMD, sub, seq) + EMPTY[sub]]
    except vce.WireError:
        return None
    return None


# ---------------------------------------------------------------------------- chat
def _parse_chat(text):
    t = unicodedata.normalize("NFKC", text or "").strip().lstrip("/!").strip()
    toks = t.split()
    if not toks or toks[0].lower() != "house":
        return None
    return [toks[0].lower()] + toks[1:]


def chat(payload, ctx, store=None):
    """house | house name <text> | house open / limited / closed | house allow / ban / unallow / unban <name> |
    house barn | house take <item id> [n] | house give <item id> [n] (adds to the barn; NOBU_HOUSE_DEBUG=1, default on)."""
    c = (ctx or {}).get("char")
    if not c or os.environ.get("NOBU_HOUSE_CHAT", "1") != "1":
        return None
    try:
        r = vce.Reader(payload, 2)
        r.u16()
        line = r.bytes(0x200).decode("utf-8", "replace")
    except vce.WireError:
        return None
    toks = _parse_chat(social.parse_line(line)[1])
    if toks is None:
        return None
    h = state(c, store)
    out = []
    cmd = toks[1].lower() if len(toks) > 1 else ""
    arg = " ".join(toks[2:])
    if cmd == "name" and arg:
        h["name"] = misc.clip_sjis(arg, NAME_MAX - 1)
        out.append(social.notify_system(f"House name: {h['name']}."))
    elif cmd in ("open", "public", "limited", "closed"):
        h["access"] = {"open": 1, "public": 1, "limited": 2, "closed": 3}[cmd]
        out.append(social.notify_system(f"Access: {ACCESS[h['access']]}."))
    elif cmd in ("allow", "ban") and arg:
        key = "allowed" if cmd == "allow" else "banned"
        other = "banned" if cmd == "allow" else "allowed"
        h[other] = [e for e in h[other] if e.get("name") != arg]
        if all(e.get("name") != arg for e in h[key]) and len(h[key]) < 50:
            h[key].append(dict(name=arg, cid=0))
        out.append(social.notify_system(f"{'Allowed' if key == 'allowed' else 'Banned'}: "
                                        + (", ".join(e["name"] for e in h[key]) or "nobody") + "."))
    elif cmd in ("unallow", "unban") and arg:
        key = "allowed" if cmd == "unallow" else "banned"
        h[key] = [e for e in h[key] if e.get("name") != arg]
        out.append(social.notify_system(f"Removed {arg}."))
    elif cmd == "give" and os.environ.get("NOBU_HOUSE_DEBUG", "1") == "1" and toks[2:]:
        try:
            iid = int(toks[2])
            n = int(toks[3]) if len(toks) > 3 else 1
        except ValueError:
            iid, n = 0, 0
        if iid and n > 0 and rows_add(h["barn"], iid, n):
            out.append(social.notify_system(f"Barn: +{items.item_name(iid)} x{n}."))
    elif cmd == "take" and toks[2:]:
        try:
            iid = int(toks[2])
            n = int(toks[3]) if len(toks) > 3 else 1
        except ValueError:
            iid, n = 0, 0
        n = min(n, barn_count(h, iid))
        got = give_item(items.inventory(c, store), iid, n) if n > 0 else None
        if got is not None:
            rows_take(h["barn"], iid, n)
            rows_take(view(ctx), iid, n)
            from . import shops
            out += [shops.notify_items([got]), social.notify_system(f"Took {items.item_name(iid)} x{n} out of the barn.")]
    elif cmd == "barn":
        rows = ", ".join(f"{items.item_name(int(i))} x{n}" for i, n in h["barn"]) or "empty"
        out.append(social.notify_system(f"Barn: {rows}."))
    _save(store)
    placed = sum(placed_counts(h["layout"]).values())
    out.append(social.notify_system(
        f"House {h['name']}: access {ACCESS.get(h['access'], ACCESS[3])}, {placed} placed, "
        f"{sum(int(n) for _, n in h['barn'])} in the barn, {len(h['allowed'])} allowed, {len(h['banned'])} banned."))
    if os.environ.get("NOBU_HOUSE_ENTER_PUSH", "1") == "1":
        out.append(enter_push(c, h))                               # (re)create the client's house model
    return out


def world_ready(ctx, store=None):
    """NOBU_HOUSE_PUSH=1: send 18 01 after the spawn so the House menu has a house model at once."""
    c = (ctx or {}).get("char")
    if ctx is not None:
        ctx.pop("house_view", None)               # a world load deletes the client's house model (UI 0x142)
    if not c or not enabled() or os.environ.get("NOBU_HOUSE_PUSH") != "1":
        return []
    return [enter_push(c, state(c, store))]


# ---------------------------------------------------------------------------- describe
def describe(payload, direction="C>S"):
    if len(payload) < 4 or payload[0] not in (CMD, CMD_ENTER):
        return None
    sub = payload[1]
    try:
        r = vce.Reader(payload, 2)
        seq = r.u16()
        if payload[0] == CMD_ENTER:
            return f"HOUSE 18 {sub:02x} push (house model) seq={seq} +{len(r.rest())} bytes" if direction == "S>C" else None
        what = SUB_NAMES.get(sub, f"sub {sub}")
        if direction == "C>S":
            if sub == 4:
                return f"HOUSE request BARN idx={r.u16()} count={r.u16()} dir={r.u8()} seq={seq}"
            return f"HOUSE request {what} seq={seq} +{len(r.rest())} bytes"
        res = r.i32()
        if sub == 4:
            return f"HOUSE reply BARN res={res} item={r.u32()} count={r.u16()} flag={r.u8()}"
        return f"HOUSE reply {what} seq={seq} res={res} +{len(r.rest())} bytes"
    except vce.WireError as e:
        return f"HOUSE {sub:02x} (malformed: {e})"
