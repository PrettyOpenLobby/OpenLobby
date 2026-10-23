"""Fantasy Earth: The Ring of Dominion. Tools are the world builder on the
world server's dev tool port; the actions POST to its /admin routes and the
calendar reads its calendar.json (hippaulring services/world/devtool.py)."""
from adminmodules import Action, Module, Service, Tool, sources


def _parse(doc, st):
    wars = doc.get("wars") if isinstance(doc.get("wars"), list) else []
    st["rooms_label"] = "wars"
    st["rooms"] = [{"name": str(w.get("field") or w.get("name") or "War"),
                    "detail": " vs ".join(str(x) for x in (w.get("attacker"), w.get("defender")) if x),
                    "state": str(w.get("state") or "")} for w in wars if isinstance(w, dict)]
    if isinstance(doc.get("in_world"), int) and st["online"] is None:
        st["online"] = doc["in_world"]
    nations = [n for n in (doc.get("nations") or []) if isinstance(n, dict)]
    for n in nations:
        st["facts"].append({"label": "%s fields" % n.get("name"), "value": n.get("fields")})


def _sysmsg(m, params, actor):
    import admingames
    text = " ".join(str(params.get("text") or "").split())
    if not text:
        return False, "Nothing to send."
    if len(text) > 200:
        return False, "A system message can be at most 200 characters."
    return admingames.call("fe", "admin/sysmsg", {"text": text}, actor)


def _number(v, hi):
    """1..hi from a field, or None. Digits only, so nothing else is sent."""
    v = str(v or "").strip()
    if not (0 < len(v) <= 3 and v.isdigit() and v.isascii()):
        return None
    n = int(v)
    return n if 1 <= n <= hi else None


def _war(m, params, actor):
    import admingames
    area = _number(params.get("area"), 95)
    if area is None:
        return False, "The field must be a number from 1 to 95."
    nation = _number(params.get("nation"), 5)
    if nation is None:
        return False, "The nation must be a number from 1 to 5."
    return admingames.call("fe", "admin/war", {"area": area, "nation": nation}, actor)


def _players(m, q, limit):
    """The player picker: the world's own search over its stored characters, online first
    (hippaulring world/devtool.py, admin/players.json)."""
    import urllib.parse
    import admingames
    status, doc = admingames.get_json("fe", "admin/players.json",
                                      urllib.parse.urlencode({"q": q, "limit": int(limit)}), "panel")
    if status != 200:
        raise OSError(doc.get("error") or doc.get("msg") or "HTTP %d" % status)
    return [p for p in doc.get("players") or [] if isinstance(p, dict)]


def _rings(m, params, actor):
    import admingames
    name = " ".join(str(params.get("name") or "").split())
    if not name:
        return False, "Pick a character."
    amount = _number(params.get("amount"), 999)
    if amount is None:
        return False, "Give between 1 and 999 Rings."
    return admingames.call("fe", "admin/rings", {"name": name, "amount": amount}, actor)


def _testmode(m, params, actor):
    import admingames
    on = str(params.get("on") or "").strip().lower()
    if on not in ("on", "off"):
        return False, "Write on or off."
    return admingames.call("fe", "admin/testmode", {"on": on}, actor)


def _popscale(m, params, actor):
    import admingames
    body = {}
    on = str(params.get("on") or "").strip().lower()
    if on:
        if on not in ("on", "off"):
            return False, "Write on or off, or leave it empty."
        body["on"] = on
    full = str(params.get("full") or "").strip()
    if full:
        n = _number(full, 50)
        if n is None:
            return False, "Full strength is 1 to 50 players a side."
        body["full"] = str(n)
    extra = str(params.get("extra") or "").strip()
    if extra:
        body["extra"] = extra
    return admingames.call("fe", "admin/popscale", body, actor)


def _warbots(m, params, actor):
    import admingames
    v = str(params.get("count") or "").strip()
    if not (0 < len(v) <= 3 and v.isdigit() and v.isascii()) or int(v) > 200:
        return False, "Soldiers a side is a number from 0 to 200."
    return admingames.call("fe", "admin/warbots", {"count": int(v)}, actor)


MODULE = Module(
    "fe", "Fantasy Earth", platform="PlayOnline: PC", content_ids=[11],
    markers=["felobby", "feworld"],
    tools=[Tool("world", "World builder", "")],
    actions=[Action("sysmsg", "System message", _sysmsg, kind="announce",
                    fields=[{"id": "text", "label": "Message", "type": "text"}],
                    about="The GM banner window, shown to every player in the world."),
             Action("war", "Start a war", _war, danger=True,
                    fields=[{"id": "area", "label": "Field number (1-95)", "type": "text"},
                            {"id": "nation", "label": "Attacking nation (1-5)", "type": "text"}],
                    about="Puts the field into War Prep with that nation attacking, under the "
                          "same rules as a keep vote: the field must border the nation's land "
                          "and neither side may already be at war. Players in the field are "
                          "shown War Prep straight away."),
             Action("rings", "Give Rings", _rings,
                    fields=[{"id": "name", "label": "Character", "type": "player"},
                            {"id": "amount", "label": "Rings (1-999)", "type": "text"}],
                    about="Adds Rings to the character's balance, the way a war's reward does. "
                          "Rings otherwise come only from wars; this is for testing the Ring "
                          "Shop and the Dragon feast's Ruby. A character in the world sees the "
                          "new balance at their next purchase or field change."),
             Action("testmode", "Summon test mode", _testmode, danger=True,
                    fields=[{"id": "on", "label": "on or off", "type": "text"}],
                    about="For trying summons on a quiet server: one vote declares a war, "
                          "summons cost nothing, and a player holding the Dragon item rises as "
                          "the Dragon on every death. It changes the running world only; "
                          "turn it off after the test (a restart of the world also does)."),
             Action("popscale", "War scaling", _popscale,
                    fields=[{"id": "on", "label": "on or off (empty = keep)", "type": "text"},
                            {"id": "full", "label": "Retail values from (players a side)",
                             "type": "text"},
                            {"id": "extra", "label": "Pretend extra players (N, or attack:defence; "
                                                     "0 = none)", "type": "text"}],
                    about="A small war scales toward the retail values by each side's players "
                          "online: summon prices, keep and building HP, the war's length, and "
                          "crystals per draw and per kill. At the full-strength number and "
                          "above, everything is retail. Pretend players let you see a big "
                          "war's values with two people. Send it empty to see what each war "
                          "under way scales to now. Changes last until the world restarts."),
             Action("warbots", "War bots", _warbots, danger=True,
                    fields=[{"id": "count", "label": "Soldiers a side (0 = off)", "type": "text"}],
                    about="A test mode for a big war with few people: every war fills with "
                          "soldier bots, this many a side (at most the war cap less 5, so real "
                          "players can still join). They start at their keep, march on the "
                          "enemy, fight players, each other and the enemy keep, and count in "
                          "the war's sign-ups. They earn nothing and are never saved. A war "
                          "under way gets them at once; 0 takes them all out. Changes last "
                          "until the world restarts.")],
    players=_players,
    services=[Service(["{core}-felobby-1", "{core}-feworld-1", "{core}-fellb-1"], {54848: "lobby"},
                      warning="Everyone in Fantasy Earth is disconnected.")],
    live=sources.board(_parse, markers=["felobby", "feworld"]),
    # the wars under way and their phases ahead, off the tool port
    # (hippaulring world/devtool.py, calendar.json)
    calendar=sources.tool_calendar(),
)
