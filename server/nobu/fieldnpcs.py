"""People of the province fields: ferrymen, checkpoint guards and villagers.

Who / where (field_spots.json, the wiki's province pages):
  ferry_<to>    ferryman at the province's port. ○: pay the fare (500 mon, the wiki's ship row) and cross to the port of
                field <to>. Routes: Owari <-> Suruga, Owari <-> Settsu-Izumi, Echizen <-> Echigo. The 2003 official
                site's diaries mention Owari's pier; the original boarded a ship that sailed, here the crossing is a
                warp (DESIGN).
  cp_<id>_<n>   two guards at every checkpoint of province_gates.json; talk-only
  <kind>_<n>    village headmen, farmers (day only where the wiki says Chisel), travellers, Ikko followers;
                talk-only. The table's other rows (exam monsters, later quest NPCs) are not people here.
Excluded by era.

Cids: shops.town_cid(field, 0x80 + i).

Env:
  NOBU_FIELD_NPCS=0   none
  NOBU_FERRY_FARE     mon per crossing (default 500)
"""
import json
import os

from . import chars, items, npc, quests, shops, zones

ROLE = "field"
ROLE_BASE = 0x80
SPOTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_spots.json")
# kind -> (family, style, line). Lines kept short (no-AI-sounding-copy rule).
KINDS = {
    "headman": ("Village Headman", "overseer", "Bandits again. The village can't take much more."),
    "farmer": ("Farmer", "guide", "Good weather for the rice this year."),
    "farmwife": ("Farmer's Wife", "guide", "The men are all out in the paddies."),
    "traveller": ("Traveller", "merchant", "I'm bound for the capital. Long road yet."),
    "ikko": ("Ikko Follower", "guildmaster", "Namu Amida Butsu."),
    "cp": ("Checkpoint Guard", "guard", "The border of {province}. Keep your papers ready."),
    "ferry": ("Ferryman", "bandit", "Ferry to {dest}. {fare} mon."),
}
_spots = None


def enabled():
    return os.environ.get("NOBU_FIELD_NPCS", "1") != "0"


def fare():
    return int(os.environ.get("NOBU_FERRY_FARE", "500"))


def load():
    global _spots
    if _spots is None:
        try:
            with open(SPOTS_FILE, encoding="utf-8") as fh:
                _spots = json.load(fh).get("fields", {})
        except (OSError, ValueError):
            _spots = {}
    return _spots


def kind_of(role):
    return role.split("_", 1)[0]


def present(s, night=None):
    when = s.get("when", "any")
    if when == "any":
        return True
    if night is None:
        from . import enemies
        night = enemies.is_night()
    return (when == "night") == night


def field_npc(player, zone, i, role, s):
    kind = kind_of(role)
    family, style, line = KINDS[kind]
    n = dict(cid=shops.town_cid(zone, ROLE_BASE + i), uid=0, b6b=npc.TALK_ONLY, family=family, given="",
             looks=npc.npc_looks(player, style), level=player.get("level", 1), role=ROLE, spot=role)
    if kind == "ferry":
        to = int(role.split("_")[1])
        n["given"] = zones.place_name(to)[:16]                 # "Ferryman Suruga" (names are max 16 bytes)
        n.update(ferry_to=to)
        line = line.format(dest=zones.place_name(to), fare=fare())
    else:
        line = line.format(province=zones.place_name(zone))
    n["text"] = line
    return n


def spawn_pushes(ctx, night=None):
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = int(chars.char_position(player)[0])
    out = []
    for i, (role, s) in enumerate(sorted(load().get(str(zone), {}).items())):
        if kind_of(role) not in KINDS or not present(s, night):
            continue
        n = field_npc(player, zone, i, role, s)
        x, y, z = s["pos"]
        pos = (zone, int(x), int(y), int(z), int(s.get("dir", 0)) % 360)
        n["pos"] = list(pos)
        ctx.setdefault("npcs", {})[n["cid"]] = n
        out += npc.spawn_pushes(n, pos)
    return out


def ferry(ctx, n, store=None):
    """○ on a ferryman: the fare, then the crossing to the other port (zones.warp), or why not."""
    c = ctx["char"]
    to = n["ferry_to"]
    here = int(chars.char_position(c)[0])
    dest = load().get(str(to), {}).get("ferry_%d" % here)
    if not dest:
        return quests.say(n, "No boats are running there today.")
    if zones.in_battle(ctx):
        return None
    inv = items.inventory(c, store)
    cost = fare()
    if int(inv.get("money", 0)) < cost:
        return quests.say(n, f"The crossing is {cost} mon. Come back when you have it.")
    inv["money"] = int(inv.get("money", 0)) - cost
    items.save(store)
    x, y, z = dest.get("arrive") or dest["pos"]
    return (quests.say(n, f"{cost} mon. Climb aboard.", popup=False) + [shops.notify_money(inv["money"])]
            + zones.warp(ctx, store, to, x, y, z, int(dest.get("dir", 0)), why="ferry"))


def answer(payload, ctx, store=None):
    """28 01 on a field NPC: the ferry, else the NPC's line; None for everything else."""
    if not enabled() or payload[:2] != b"\x28\x01" or not (ctx or {}).get("char"):
        return None
    n = (ctx.get("npcs") or {}).get(ctx.get("target"))
    if n is None or n.get("role") != ROLE:
        return None
    ctx.setdefault("actions", []).append(ctx.get("target"))
    if n.get("ferry_to"):
        out = ferry(ctx, n, store)
        if out is not None:
            return out
    return quests.say(n, n.get("text", ""))
