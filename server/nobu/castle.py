"""Castle NPCs: the lord and his generals at the keep, the castellan, the onmyoji elder and the gate guards.

Who (2003-2005 official site and FAQ): every clan castle holds its daimyo with his generals (the wiki's
pre-2008 formation for Oda: Oda...), a, the sorcery elder at the onmyoji hall (restores) and at the gate.
Here they are talk-only: the exam report stays with the Hall Master (lifecycle.py) and is not modelled.

Where: town_spots.json, castle entries (castle_spots / other_castles). Every clan's castle is its home town's map
+ 300; Nagoya Castle's gate is in zones.EXITS, the others in castle_gates.json (towns.exits). Nagoya Castle has
the wiki's generals; the other castles have their daimyo with the tutor who greets new players in the client's
intro cutscene (UIMSG 4.18: Uesugi, Takeda...), a castellan and two guards.

Cids: shops.town_cid(castle map, 0x50 + i).

Env:
  NOBU_CASTLE_NPCS=0   none
"""
import os

from . import chars, npc, shops, towns

ROLE_BASE = 0x50
# castle map -> [(spot role, family, given, style, line)]. Names in the English the client shows (romanised);
# lines kept short (no-AI-sounding-copy rule).
CASTLES = {
    410: [
        ("lord", "Oda", "Nobunaga", "overseer", "Serve the house well, and Owari will remember it."),
        ("general_0", "Hashiba", "Hideyoshi", "guide", "Lord Nobunaga has work for willing hands."),
        ("general_1", "Akechi", "Mitsuhide", "guildmaster", "Mind your manners in the lord's hall."),
        ("general_2", "Shibata", "Katsuie", "bandit", "Train hard. The Imagawa will not wait."),
        ("general_3", "Niwa", "Nagahide", "overseer", "The castle stores are in my keeping."),
        ("general_4", "Takigawa", "Kazumasu", "guard", "Keep your eyes open on the roads."),
        ("castellan", "Castellan", "", "overseer", "I hold this castle in the lord's absence."),
        ("onmyoji_elder", "Onmyoji Elder", "", "guide", "Spent your spirit power? Rest, and it returns."),
        ("guard_212_0", "Castle Guard", "", "guard", "State your business in the castle."),
        ("guard_212_1", "Castle Guard", "", "guard", "State your business in the castle."),
    ],
}


# clan (STRDAT table 5, towns.TOWNS faction) -> (daimyo, tutor) as (family, given), from the intro cutscene speakers
# (UIMSG 4.18.0..27), romanised.
LORDS = {
    1: (("Uesugi", "Kenshin"), ("Usami", "Sadamitsu")), 2: (("Takeda", "Shingen"), ("Sanada", "Yukitaka")),
    3: (("Hojo", "Ujiyasu"), ("Hojo", "Genan")), 4: (("Imagawa", "Yoshimoto"), ("Taigen", "Sessai")),
    5: (("Tokugawa", "Ieyasu"), ("Honda", "Masanobu")), 6: (("Oda", "Nobunaga"), ("Hashiba", "Hideyoshi")),
    7: (("Saito", "Dosan"), ("Akechi", "Mitsuyasu")), 8: (("Honganji", "Kennyo"), ("Honganji", "Kyonyo")),
    9: (("Asakura", "Yoshikage"), ("Asakura", "Soteki")), 10: (("Azai", "Nagamasa"), ("Azai", "Hisamasa")),
    11: (("Momochi", "Sandayu"), ("Fujibayashi", "Masayasu")), 12: (("Ashikaga", "Yoshiteru"), ("Ashikaga", "Yoshiaki")),
    13: (("Miyoshi", "Nagayoshi"), ("Matsunaga", "Hisahide")), 14: (("Saika", "Magoichi"), ("Saika", "Magoroku")),
}


def roster(zone):
    """[(spot role, family, given, style, line)] of castle `zone`: Nagoya Castle's written list, else the clan's
    daimyo and tutor, the castellan and the guards of the castle's spots."""
    if zone in CASTLES:
        return CASTLES[zone]
    t = towns.TOWNS.get(zone - 300)
    if not t:
        return []
    (lf, lg), (tf, tg) = LORDS.get(t["faction"], (("Lord", ""), ("Retainer", "")))
    out = [("lord", lf, lg, "overseer", f"Serve the {t['house']} house well."),
           ("retainer", tf, tg, "guide", "The lord's orders come through me."),
           ("castellan", "Castellan", "", "overseer", "I hold this castle in the lord's absence."),
           ("onmyoji_elder", "Onmyoji Elder", "", "guide", "Spent your spirit power? Rest, and it returns.")]
    guards = sorted(r for r in towns._load_spots().get(str(zone), {}) if r.startswith("guard_"))
    out += [(r, "Castle Guard", "", "guard", "State your business in the castle.") for r in guards]
    return out


def enabled():
    return os.environ.get("NOBU_CASTLE_NPCS", "1") != "0"


def castle_spot(zone, role):
    """(zone, x, y, z, dir) of `role` in castle `zone` from town_spots.json, or None (NOBU_TOWN_SPOTS=0 too)."""
    if os.environ.get("NOBU_TOWN_SPOTS", "1") == "0":
        return None
    s = towns._load_spots().get(str(int(zone)), {}).get(role)
    if not s:
        return None
    x, y, z = s["pos"]
    return (int(zone), int(x), int(y), int(z), int(s.get("dir", 0)) % 360)


def spawn_pushes(ctx):
    """CHAR_INFO pushes for the NPCs of the castle the player is in (appended to world_ready_pushes)."""
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = int(chars.char_position(player)[0])
    out = []
    for i, (role, family, given, style, line) in enumerate(roster(zone)):
        pos = castle_spot(zone, role)
        if pos is None:
            continue
        n = dict(cid=shops.town_cid(zone, ROLE_BASE + i), uid=0, b6b=npc.TALK_ONLY, family=family, given=given,
                 looks=npc.npc_looks(player, style), level=player.get("level", 1), role="castle", text=line)
        n["pos"] = list(pos)
        ctx.setdefault("npcs", {})[n["cid"]] = n
        out += npc.spawn_pushes(n, pos)
    return out
