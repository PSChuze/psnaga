"""The repairer: ○ on him mends every worn piece of gear the character carries, for a fee.

Who / where: the 2003 tutorial sends new players to the repairer at the smith; one stands
beside the Craftsman in every clan's home town (town_spots.json role "repairer"). upkeep.py wears gear down in
battle and tells the player to "have it repaired".

DESIGN (no 2004 repair prices survive): the fee for one piece is half its lowest shop price times the share of
durability it lost, at least 1 mon (gear no shop sells: 1 mon per point). Talking repairs everything at once when
the money in the backpack covers it, else he names the price. Durability lives on the server (items.py); the client
sees the new values the next time it asks for the backpack, as after wear.

Env:
  NOBU_REPAIR=0        no repairer
  NOBU_REPAIR_RATE     share of the shop price a full repair costs (default 0.5)
"""
import os

from . import chars, items, npc, shops, towns

ROLE = "repair"
CID_ROLE = 0x13                    # per-town cid role byte (shops.town_cid scheme)


def enabled():
    return os.environ.get("NOBU_REPAIR", "1") != "0"


def worn(inv):
    """Gear (equipment with durability) that lost some."""
    return [it for it in inv.get("items", []) if int(it.get("dur_max", 0) or 0) > 0
            and int(it.get("dur", 0) or 0) < int(it["dur_max"])]


def fee(it, catalog):
    lost = int(it["dur_max"]) - int(it.get("dur", 0) or 0)
    price = catalog.get(it["id"])
    if not price:
        return max(1, lost)
    rate = float(os.environ.get("NOBU_REPAIR_RATE", "0.5"))
    return max(1, int(round(price * rate * lost / int(it["dur_max"]))))


def quote(c, store=None):
    inv = items.inventory(c, store)
    cat = shops.catalog(shops.load_shops())
    pieces = worn(inv)
    return pieces, sum(fee(it, cat) for it in pieces)


def repair_all(c, store=None):
    """(ok, line, cost). Mends every worn piece when the backpack money covers the whole bill."""
    inv = items.inventory(c, store)
    pieces, cost = quote(c, store)
    if not pieces:
        return False, "Nothing of yours needs mending.", 0
    kan, mon = items.money_split(cost)
    price = f"{kan} kan {mon} mon" if kan else f"{mon} mon"
    if int(inv.get("money", 0)) < cost:
        return False, f"Mending all of it costs {price}. Come back with the money.", 0
    inv["money"] = int(inv.get("money", 0)) - cost
    for it in pieces:
        it["dur"] = int(it["dur_max"])
        it.pop("broken", None)
    if store is not None:
        store.save()
    return True, f"Mended {len(pieces)} piece(s) for {price}.", cost


def repairer_npc(player, zone):
    cid = shops.town_cid(zone, CID_ROLE)
    return dict(cid=cid, uid=0, b6b=npc.TALK_ONLY, family="Repairer", given="",
                looks=npc.npc_looks(player, "craftsman"), level=player.get("level", 1), role=ROLE,
                text="Bring me anything worn. I'll have it like new.")


def spawn_pushes(ctx):
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = chars.char_position(player)[0]
    if not towns.is_town(zone):
        return []
    pos = towns.spot(zone, "repairer")
    if pos is None:
        pos = npc.town_spot(player, -450, 300, 90, role="repairer")    # beside the Craftsman's offset
    n = repairer_npc(player, zone)
    n["pos"] = list(pos)
    ctx.setdefault("npcs", {})[n["cid"]] = n
    return npc.spawn_pushes(n, pos)


def answer(payload, ctx, store=None):
    """28 01 on the repairer: his line, then the repair (or the quote) as a popup and the money total."""
    if len(payload) < 2 or payload[0] != 0x28 or payload[1] != 1:
        return None
    n = (ctx.get("npcs") or {}).get(ctx.get("target"))
    if not n or n.get("role") != ROLE or not ctx.get("char"):
        return None
    c = ctx["char"]
    ok, line, cost = repair_all(c, store)
    out = npc.talk_replies(dict(n, text=line))
    if ok:
        out.append(shops.notify_money(items.inventory(c, store).get("money", 0)))
    ctx.setdefault("repair_log", []).append((ok, cost))
    return out
