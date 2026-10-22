"""The doctor: ○ on him restores Life and Ki to full, for a fee.

Who / where: the 2003 tutorial's doctor by the apothecary; one stands by the apothecary of every clan's home town
(town_spots.json role "doctor"), else near the town centre. Life and Ki outside battle are upkeep.py's (regenerating
while idle); the doctor saves the wait.

DESIGN (no 2004 price survives): 1 mon for every 2 points restored, at least 1 mon. A ghost is sent to the shrine.

Env:
  NOBU_DOCTOR=0          no doctor
  NOBU_DOCTOR_RATE       mon per point restored (default 0.5)
"""
import os

from . import chars, items, npc, shops, towns, upkeep

ROLE = "doctor"
CID_ROLE = 0x14                    # per-town cid role byte (shops.town_cid scheme)


def enabled():
    return os.environ.get("NOBU_DOCTOR", "1") != "0"


def missing(c):
    s = upkeep.state(c)
    mx_life, mx_ki = upkeep.maxima(c)[:2]
    return max(0, mx_life - int(s.get("life", mx_life))), max(0, mx_ki - int(s.get("ki", mx_ki)))


def fee(c):
    life, ki = missing(c)
    if not life and not ki:
        return 0
    return max(1, int(round((life + ki) * float(os.environ.get("NOBU_DOCTOR_RATE", "0.5")))))


def treat(c, store=None):
    """(ok, line, cost)."""
    if c.get("ghost"):
        return False, "I can do nothing for a spirit. Go to the shrine.", 0
    cost = fee(c)
    if not cost:
        return False, "You are in good health.", 0
    inv = items.inventory(c, store)
    kan, mon = items.money_split(cost)
    price = f"{kan} kan {mon} mon" if kan else f"{mon} mon"
    if int(inv.get("money", 0)) < cost:
        return False, f"Treating you costs {price}. Come back with the money.", 0
    inv["money"] = int(inv.get("money", 0)) - cost
    s = upkeep.state(c)
    s["life"], s["ki"] = upkeep.maxima(c)[:2]
    if store is not None:
        store.save()
    return True, f"Rest easy. That will be {price}.", cost


def doctor_npc(player, zone):
    return dict(cid=shops.town_cid(zone, CID_ROLE), uid=0, b6b=npc.TALK_ONLY, family="Doctor", given="",
                looks=npc.npc_looks(player, "guide", job=7), level=player.get("level", 1), role=ROLE,
                text="Wounded? I can help.")


def spawn_pushes(ctx):
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = chars.char_position(player)[0]
    if not towns.is_town(zone):
        return []
    pos = npc.town_spot(player, 450, 300, 270, role="doctor")
    n = doctor_npc(player, zone)
    n["pos"] = list(pos)
    ctx.setdefault("npcs", {})[n["cid"]] = n
    return npc.spawn_pushes(n, pos)


def answer(payload, ctx, store=None):
    """28 01 on the doctor: the treatment (or its price), the new Life / Ki and the money total."""
    if len(payload) < 2 or payload[0] != 0x28 or payload[1] != 1:
        return None
    n = (ctx.get("npcs") or {}).get(ctx.get("target"))
    if not n or n.get("role") != ROLE or not ctx.get("char"):
        return None
    c = ctx["char"]
    ok, line, cost = treat(c, store)
    out = npc.talk_replies(dict(n, text=line))
    if ok:
        out.append(upkeep.status_push(c))
        out.append(shops.notify_money(items.inventory(c, store).get("money", 0)))
    return out
