"""Old sage (the old man who knows everything): one by the notice board of every home town.

Who (2004 FAQ; the official site's notices): the helper who answers questions and repairs a character's
broken records (later patches moved quest-data repair to him). Here ○ on him repairs: requests and guild
jobs whose ids no longer exist are dropped, and the Quest tab is sent again in full (34 01). Otherwise he gives one
tip per talk, in turn.

Where: town_spots.json role "elder", else near the town centre. Cid: shops.town_cid(zone, 0x15).

Env:
  NOBU_ELDER=0   none
"""
import os

from . import chars, guild, misc, npc, quests, shops, towns, townquests

ROLE = "elder"
CID_ROLE = 0x15
TIPS = (
    "Your requests are in the Profile window, on the Quest tab.",
    "The Hall Master hands out work. Say \"jobs\" to see it.",
    "Wounded? The doctor by the medicine hall will see to you.",
    "Worn gear? The repairer stands by the smithy.",
    "Boats sail from Owari's port to Suruga and Settsu. 500 mon.",
    "The money changer keeps your coin and your goods safe.",
)


def enabled():
    return os.environ.get("NOBU_ELDER", "1") != "0"


def elder_npc(player, zone):
    return dict(cid=shops.town_cid(zone, CID_ROLE), uid=0, b6b=npc.TALK_ONLY, family="Old Sage", given="",
                looks=npc.npc_looks(player, "overseer"), level=player.get("level", 1), role=ROLE,
                text="Ask me anything. I may even know.")


def spawn_pushes(ctx):
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = chars.char_position(player)[0]
    if not towns.is_town(zone):
        return []
    pos = npc.town_spot(player, -600, 600, 90, role=ROLE)
    n = elder_npc(player, zone)
    n["pos"] = list(pos)
    ctx.setdefault("npcs", {})[n["cid"]] = n
    return npc.spawn_pushes(n, pos)


def repair(c):
    """Drop request / job records whose ids no longer exist. Returns how many."""
    fixed = 0
    for key in list((c.get("tq") or {})):
        if not key.isdigit() or int(key) not in townquests.QUESTS:
            c["tq"].pop(key)
            fixed += 1
    for key, rec in list((c.get("quests") or {}).items()):
        if not key.isdigit() or int(key) not in quests.QUESTS:
            c["quests"].pop(key)
            fixed += 1
    g = c.get("guild") or {}
    if g.get("job") and guild.active(c) is None:
        g.update(job=None, kills=0)
        fixed += 1
    return fixed


def quest_tab(ctx, store=None):
    """34 01 with every live request: the tutorial, the guild job, the town quests."""
    c = ctx["char"]
    ents = quests.active_entries(ctx)
    job = guild.active(c)
    if job is not None:
        ents.append(guild.job_entry(c, job, store))
    ents += [townquests.entry(c, q, store) for q in sorted(townquests.active(c))]
    return quests.quest_list(ents)


def talk(ctx, n, store=None):
    c = ctx["char"]
    fixed = repair(c)
    if fixed:
        st = store() if callable(store) else store
        if st is not None and hasattr(st, "save"):
            st.save()
        return (quests.say(n, "Your records were in a tangle. I've put them straight.")
                + [quest_tab(ctx, store), misc.notify_system(f"Repaired {fixed} request record(s).")])
    i = int(c.get("elder_tip", 0))
    c["elder_tip"] = (i + 1) % len(TIPS)
    return quests.say(n, TIPS[i % len(TIPS)]) + [quest_tab(ctx, store)]


def answer(payload, ctx, store=None):
    if not enabled() or payload[:2] != b"\x28\x01" or not (ctx or {}).get("char"):
        return None
    n = (ctx.get("npcs") or {}).get(ctx.get("target"))
    if n is None or n.get("role") != ROLE:
        return None
    ctx.setdefault("actions", []).append(ctx.get("target"))
    return talk(ctx, n, store)
