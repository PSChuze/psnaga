"""Town quests: Nagoya's quest givers and the people their errands lead to.

Who / what (the wiki's Nagoya NPC list and quest table, 2004 site notes name 's Heal Powder request, 's
request): each quest is a short chain of steps, every step finished by talking to one NPC:
  talk   just talk to them (they may hand over an item, or ask for money first)
  bring  have the items in the backpack; they are taken
  kill   defeat `count` enemies of a kind (won battles logged by enemies.on_defeated), then report
Accepting is implicit, as for the tutorial quest and the guild jobs (quests.py: the client has no accept dialogue):
talking to a giver whose quest is open adds it to the Quest tab (34 02); finishing removes it (34 03) and the reward
is announced as a chat line.

Where: town_spots.json roles q_<key> (QUEST_ANCHORS): Nagoya, Nagoya Castle, Okazaki, Sunpu and the Owari
field (map 10).

Items and rewards: the quest items are the 2004 client's own (STRDAT table 0: Hayashi Deed 650, Tawaraya
Cargo 651, 654, Misshapen Comb 775, Wolf Dung 783); Fire Lance 784, Red Plum Comb 729 and Geni Black
Kesa 779 are the wiki's rewards. Where the wiki's reward has no known item type (Awayuki, Surprise
Daruma) or names none, the reward is DESIGN: money, fame, EXP.

Ids 3001.. in the Quest tab (tutorial 1001.., guild jobs 2001..2999). Cids: shops.town_cid(zone, 0x60 + i).

Character state (chars.json): c["tq"][str(id)] = {"step": i, "kills": n, "taken": time}; c["tq_done"][str(id)] =
time of the last completion (a quest with `every` days opens again after that long, else never).

Env:
  NOBU_TOWN_QUESTS=0   none
"""
import datetime as dt
import os

from . import chars, crafting, guild, items, misc, npc, progression, quests, shops, towns

ROLE = "tq"
ROLE_BASE = 0x60
QID_MIN, QID_MAX = 3001, 3999
ODA = 6

# key -> (zone, family, given, style, idle line). Spot role = "q_" + key.
NPCS = {
    "onui": (110, "Onui", "", "guide", "My husband went out to the fields and has not come back."),
    "sakuzo": (110, "Sakuzo", "", "guildmaster", "Now what was I supposed to be making?"),
    "nihei": (110, "Tawaraya", "Nihei", "merchant", "Trade with Suruga is good this season."),
    "seikichi": (110, "Seikichi", "", "merchant", "The roads are not safe for a merchant."),
    "ohana": (110, "Ohana", "", "guide", "Sakuzo forgets everything. Everything."),
    "sosuke": (110, "Sosuke", "", "overseer", "Orin keeps losing her combs."),
    "rokusuke": (110, "Rokusuke", "", "bandit", "I fought for the Oda before you were born."),
    "gisuke": (110, "Gisuke", "", "overseer", "I am the headman of this quarter."),
    "sansuke": (110, "Sansuke", "", "guard", "They say a ghost walks by night outside Kiyosu."),
    "geni": (410, "Maeda", "Geni", "guildmaster", "Lord Nobunaga's affairs keep me busy."),
    "hayashi": (104, "Hayashi", "Ryobei", "overseer", "Okazaki's trade is honest trade."),
    "saito": (107, "Saito", "Sakuji", "merchant", "Sunpu has everything a merchant could want."),
    "matahachi": (10, "Matahachi", "", "guard", "The harvest will not bring itself in."),
    "fujimura": (10, "Fujimura", "", "bandit", "What do you want out here?"),
}

# Quest steps: kind, who finishes it (npc), what it needs / does, the objective (for the Quest tab) and what the
# NPC says when it is done.
QUESTS = {
    3001: dict(key="gunpowder", title="The Gunpowder Maker", giver="rokusuke", min_level=3,
               offer="Gunpowder needs wolf dung. Bring me 30 and I will give you something worth it.",
               steps=[dict(kind="bring", npc="rokusuke", items=[(783, 30)], goal="Bring Rokusuke 30 wolf dung.",
                           done="That will do. Here, take this fire lance.")],
               reward=dict(item=(784, 1), fame=1)),
    3002: dict(key="orin_comb", title="Orin's Comb", giver="sosuke", every=2,
               offer="Orin lost her comb. A gulper snake swallowed it, they say. Get it back for me.",
               steps=[dict(kind="bring", npc="sosuke", items=[(775, 1)], goal="Bring Sosuke the misshapen comb.",
                           done="That's it. Orin will be glad. Take this one for your trouble.")],
               reward=dict(item=(729, 1), fame=2)),
    3003: dict(key="bond", title="The Bond", giver="gisuke", min_fame=300,
               offer="Hayashi Ryobei in Okazaki holds a bond for me. Collect it.",
               steps=[dict(kind="talk", npc="hayashi", give=(650, 1), goal="Collect the bond from Hayashi in Okazaki.",
                           done="Gisuke's bond? Here it is."),
                      dict(kind="bring", npc="gisuke", items=[(650, 1)], goal="Bring Hayashi's bond to Gisuke.",
                           done="You have it. My thanks.")],
               reward=dict(money=1500, fame=5, exp=60)),
    3004: dict(key="sunpu_deal", title="Business in Sunpu", giver="nihei", min_level=5,
               offer="Go to Saito Sakuji in Sunpu and buy my goods. 4 kan. I will pay you back.",
               steps=[dict(kind="talk", npc="saito", pay=4000, give=(651, 1),
                           goal="Pay Saito in Sunpu 4 kan for Tawaraya's goods.", done="Tawaraya's order. 4 kan, thank you."),
                      dict(kind="bring", npc="nihei", items=[(651, 1)], goal="Bring the goods to Tawaraya Nihei.",
                           done="All here. Your 4 kan, and something extra.")],
               reward=dict(money=4600, fame=3, exp=40)),
    3005: dict(key="sakuzo_errand", title="Sakuzo Forgets", giver="sakuzo",
               offer="Ohana told me what to make, and I forgot. Ask her, will you?",
               steps=[dict(kind="talk", npc="ohana", goal="Ask Ohana what Sakuzo was making.",
                           done="Healing powder. He needs 4 pheasant's eye, 10 sansho berries and 5 St John's wort."),
                      dict(kind="bring", npc="sakuzo", items=[(413, 4), (1283, 10), (386, 5)],
                           goal="Bring Sakuzo 4 pheasant's eye, 10 sansho berries, 5 St John's wort.",
                           done="Healing powder, that was it. Take some.")],
               reward=dict(item=(201, 3), exp=50)),
    3006: dict(key="ghost_rumour", title="The Ghost Rumour", giver="sansuke", min_level=10, faction=ODA,
               offer="The ghost at Kiyosu is no ghost. Lord Maeda Geni at the castle wants it looked into.",
               steps=[dict(kind="talk", npc="geni", goal="Report to Maeda Geni in Nagoya Castle.",
                           done="Raiders in white. Drive them off."),
                      dict(kind="kill", npc="geni", enemy="kiyosu_bandit", count=3,
                           goal="Defeat 3 Kiyosu Raiders, then report to Maeda Geni.",
                           done="Well done. Wear this.")],
               reward=dict(item=(779, 1), fame=5, exp=150)),
    3007: dict(key="couple", title="The Devoted Couple", giver="onui", min_level=5,
               offer="My husband Matahachi is in the fields north of town. Tell him to come home.",
               steps=[dict(kind="talk", npc="matahachi", goal="Find Matahachi in the Owari fields.",
                           done="Onui sent you? Tell her I'll be home by dark."),
                      dict(kind="talk", npc="onui", goal="Tell Onui what Matahachi said.",
                           done="By dark. He always says that. Thank you.")],
               reward=dict(money=300, fame=2, exp=30)),
    3008: dict(key="stolen_goods", title="The Stolen Goods", giver="seikichi", min_fame=200,
               offer="My stock was taken on the road. A man called Fujimura in the south-east has it.",
               steps=[dict(kind="talk", npc="fujimura", give=(654, 1), goal="Get Seikichi's goods from Fujimura.",
                           done="Seikichi's? Take it. I want no trouble."),
                      dict(kind="bring", npc="seikichi", items=[(654, 1)], goal="Bring the goods to Seikichi.",
                           done="My stock! Thank you.")],
               reward=dict(money=800, fame=4, exp=40)),
}


def enabled():
    return os.environ.get("NOBU_TOWN_QUESTS", "1") != "0"


def now():
    return dt.datetime.now()


def _save(store):
    st = store() if callable(store) else store
    if st is not None and hasattr(st, "save"):
        st.save()


# ---------------------------------------------------------------------------- state
def active(c):
    return {int(k): v for k, v in (c or {}).get("tq", {}).items() if int(k) in QUESTS}


def open_for(c, qid):
    """Quest qid can be taken now: not active, never done (or `every` days since), level / fame / clan allow it."""
    q = QUESTS[qid]
    if str(qid) in (c.get("tq") or {}):
        return False
    last = (c.get("tq_done") or {}).get(str(qid))
    if last:
        if not q.get("every"):
            return False
        try:
            if now() - dt.datetime.fromisoformat(last) < dt.timedelta(days=int(q["every"])):
                return False
        except ValueError:
            pass
    if int(c.get("level", 1) or 1) < int(q.get("min_level", 1)):
        return False
    if int(c.get("fame", 0)) < int(q.get("min_fame", 0)):
        return False
    return not q.get("faction") or progression.faction_of(c) == q["faction"]


def step_of(c, qid):
    rec = (c.get("tq") or {}).get(str(qid))
    return QUESTS[qid]["steps"][int(rec["step"])] if rec else None


def have(c, item_id, store=None):
    return guild.item_count(c, item_id, store)


def progress_text(c, qid, store=None):
    s = step_of(c, qid)
    rec = c["tq"][str(qid)]
    if s["kind"] == "kill":
        return f"[{min(int(rec.get('kills', 0)), s['count'])}/{s['count']} defeated] "
    if s["kind"] == "bring":
        return "[" + ", ".join(f"{min(have(c, i, store), n)}/{n}" for i, n in s["items"]) + "] "
    return ""


def entry(c, qid, store=None):
    q = QUESTS[qid]
    return quests.entry(qid, q["title"], progress_text(c, qid, store) + step_of(c, qid)["goal"])


def step_ready(c, qid, store=None):
    s = step_of(c, qid)
    if s["kind"] == "kill":
        return int(c["tq"][str(qid)].get("kills", 0)) >= int(s["count"])
    if s["kind"] == "bring":
        return all(have(c, i, store) >= n for i, n in s["items"])
    return True


# ---------------------------------------------------------------------------- NPCs
def spot(key):
    zone = NPCS[key][0]
    if os.environ.get("NOBU_TOWN_SPOTS", "1") == "0":
        return None
    s = towns._load_spots().get(str(zone), {}).get("q_" + key)
    if not s:
        return None
    x, y, z = s["pos"]
    return (zone, int(x), int(y), int(z), int(s.get("dir", 0)) % 360)


def npc_cid(key):
    zone = NPCS[key][0]
    i = [k for k in NPCS if NPCS[k][0] == zone].index(key)
    return shops.town_cid(zone, ROLE_BASE + i)


def quest_npc(player, key):
    zone, family, given, style, line = NPCS[key]
    return dict(cid=npc_cid(key), uid=0, b6b=npc.TALK_ONLY, family=family, given=given,
                looks=npc.npc_looks(player, style), level=player.get("level", 1), role=ROLE, key=key, text=line)


def spawn_pushes(ctx):
    player = ctx.get("char")
    if not player or not enabled():
        return []
    zone = int(chars.char_position(player)[0])
    out = []
    for key in NPCS:
        if NPCS[key][0] != zone:
            continue
        pos = spot(key)
        if pos is None:
            continue
        n = quest_npc(player, key)
        n["pos"] = list(pos)
        ctx.setdefault("npcs", {})[n["cid"]] = n
        out += npc.spawn_pushes(n, pos)
    return out


# ---------------------------------------------------------------------------- kills
def sync_log(ctx, store=None):
    """Count the "defeated" entries of ctx["enemies_log"] (enemies.on_defeated) since the last call on the active
    kill steps. Returns frames (34 04 updates and progress lines)."""
    c = ctx.get("char")
    log = ctx.get("enemies_log") or []
    seen = int(ctx.get("tq_log_seen", 0))
    ctx["tq_log_seen"] = len(log)
    out = []
    for ent in log[seen:]:
        if len(ent) < 3 or ent[0] != "defeated":
            continue
        for qid in active(c):
            s = step_of(c, qid)
            rec = c["tq"][str(qid)]
            if s["kind"] != "kill" or s["enemy"] != ent[2] or int(rec.get("kills", 0)) >= int(s["count"]):
                continue
            rec["kills"] = int(rec.get("kills", 0)) + 1
            _save(store)
            out.append(quests.quest_update(entry(c, qid, store)))
            line = f"{QUESTS[qid]['title']}: {rec['kills']}/{s['count']} defeated."
            if rec["kills"] >= int(s["count"]):
                line += f" Report to {npc.npc_name(quest_npc(c, s['npc']))}."
            out.append(misc.notify_system(line))
    return out


# ---------------------------------------------------------------------------- talk
def _give(c, item_id, qty, store=None):
    """Put qty of item_id in the backpack (equipment with its wiki stats). Returns the changed items or None."""
    known = items.stats_data().get(int(item_id)) or {}
    if known:
        proto = dict(id=item_id, type=int(known.get("type", 0)), cat=int(known.get("cat", 0)), stack=False,
                     dur=items.new_durability(int(item_id)))
    elif item_id == 201:
        proto = dict(id=item_id, type=items.T_MEDICINE, cat=0, stack=True)
    else:
        proto = dict(id=item_id, type=54, cat=0, stack=False)
    changed = crafting.give(items.inventory(c, store), proto, qty)
    if changed is not None:
        items.save(store)
    return changed


def reward_text(rw):
    parts = []
    if rw.get("item"):
        i, n = rw["item"]
        parts.append(items.item_name(i) + (f" x{n}" if n > 1 else ""))
    if rw.get("money"):
        kan, mon = items.money_split(int(rw["money"]))
        parts.append(f"{kan} kan {mon} mon" if kan else f"{mon} mon")
    for k, label in (("exp", "EXP"), ("fame", "fame")):
        if rw.get(k):
            parts.append(f"{int(rw[k])} {label}")
    return ", ".join(parts) or "nothing"


def take(ctx, qid, n, store=None):
    c = ctx["char"]
    c.setdefault("tq", {})[str(qid)] = dict(step=0, kills=0, taken=now().isoformat(timespec="seconds"))
    _save(store)
    q = QUESTS[qid]
    return (quests.say(n, q["offer"]) + [quests.quest_add(entry(c, qid, store)),
                                          misc.notify_system(f"New request: {q['title']}")])


def finish(ctx, qid, n, store=None):
    """The last step is done: reward, entry removed. Returns frames (or a "backpack full" line, nothing changed)."""
    c = ctx["char"]
    q = QUESTS[qid]
    rw = q.get("reward") or {}
    out = []
    if rw.get("item"):
        changed = _give(c, *rw["item"], store=store)
        if changed is None:
            return None
        out += shops.item_pushes(changed)
    inv = items.inventory(c, store)
    if rw.get("money"):
        inv["money"] = int(inv.get("money", 0)) + int(rw["money"])
        out.insert(0, shops.notify_money(inv["money"]))
    c["tq"].pop(str(qid), None)
    c.setdefault("tq_done", {})[str(qid)] = now().isoformat(timespec="seconds")
    progression.award_ctx(ctx, exp=int(rw.get("exp", 0)), fame=int(rw.get("fame", 0)), store=store)
    return ([quests.quest_remove(qid), misc.notify_system(f"Request complete: {q['title']}. Reward: {reward_text(rw)}.")]
            + out + progression.flush(ctx))


def advance(ctx, qid, n, store=None):
    """Finish the current step with NPC n. Returns frames, or None when it is not ready (the caller reminds)."""
    c = ctx["char"]
    s = step_of(c, qid)
    inv = items.inventory(c, store)
    out = []
    last = int(c["tq"][str(qid)]["step"]) + 1 >= len(QUESTS[qid]["steps"])
    gets = s.get("give") or (last and (QUESTS[qid].get("reward") or {}).get("item"))
    if gets and s["kind"] != "bring" and len({it.get("idx") for it in inv["items"]}) >= items.CAPACITY:
        return [misc.notify_system("Your backpack is full.")]
    if s["kind"] == "bring":
        if not step_ready(c, qid, store):
            return None
        for i, k in s["items"]:
            out += shops.item_pushes(guild.take_items(c, i, k, store), sold=True)
    elif s["kind"] == "kill" and not step_ready(c, qid, store):
        return None
    if s.get("pay"):
        if int(inv.get("money", 0)) < int(s["pay"]):
            kan, mon = items.money_split(int(s["pay"]))
            return quests.say(n, f"That will be {kan} kan {mon} mon." if kan else f"That will be {mon} mon.")
    if s.get("give"):
        changed = _give(c, *s["give"], store=store)
        if changed is None:
            return [misc.notify_system("Your backpack is full.")]
        out += shops.item_pushes(changed)
    if s.get("pay"):
        inv["money"] = int(inv.get("money", 0)) - int(s["pay"])
        out.insert(0, shops.notify_money(inv["money"]))
    rec = c["tq"][str(qid)]
    if int(rec["step"]) + 1 >= len(QUESTS[qid]["steps"]):
        done = finish(ctx, qid, n, store)
        if done is None:
            return quests.say(n, s["done"]) + out + [misc.notify_system("Your backpack is full.")]
        return quests.say(n, s["done"]) + out + done
    rec.update(step=int(rec["step"]) + 1, kills=0)
    _save(store)
    return quests.say(n, s["done"]) + out + [quests.quest_add(entry(c, qid, store))]


def talk(ctx, n, store=None):
    c = ctx["char"]
    key = n.get("key")
    out = sync_log(ctx, store)
    for qid in sorted(active(c)):
        if step_of(c, qid)["npc"] == key:
            res = advance(ctx, qid, n, store)
            if res is not None:
                return out + res
            return out + quests.say(n, step_of(c, qid)["goal"] + " " + progress_text(c, qid, store).strip())
    for qid in sorted(active(c)):
        if QUESTS[qid]["giver"] == key:
            return out + quests.say(n, step_of(c, qid)["goal"])
    for qid, q in sorted(QUESTS.items()):
        if q["giver"] == key and open_for(c, qid):
            return out + take(ctx, qid, n, store)
    return out + quests.say(n, n.get("text", ""))


# ---------------------------------------------------------------------------- hooks
def world_ready(ctx, store=None):
    """23 5a: the quest NPCs of the zone, kills since the last check, and the active quests' entries (34 02; the
    client list starts empty at every login)."""
    c = ctx.get("char")
    if not enabled() or not c:
        return []
    out = spawn_pushes(ctx)
    lines = sync_log(ctx, store)
    for qid in sorted(active(c)):
        out.append(quests.quest_add(entry(c, qid, store)))
    return out + lines


def answer(payload, ctx, store=None):
    """2a 03 for a town quest id and 28 01 on a quest NPC; None for everything else."""
    if not enabled() or len(payload) < 4 or not (ctx or {}).get("char"):
        return None
    cmd, sub = payload[0], payload[1]
    if cmd == 0x2A and sub == 3:
        d = quests.decode_detail_request(payload)
        if not QID_MIN <= d["id"] <= QID_MAX:
            return None
        c = ctx["char"]
        if d["id"] in active(c):
            return [quests.quest_add(entry(c, d["id"], store), d["seq"])]
        return [quests.quest_remove(d["id"], d["seq"])]
    if cmd == 0x28 and sub == 1:
        n = (ctx.get("npcs") or {}).get(ctx.get("target"))
        if n is None or n.get("role") != ROLE:
            return None
        ctx.setdefault("actions", []).append(ctx.get("target"))
        return talk(ctx, n, store)
    return None
