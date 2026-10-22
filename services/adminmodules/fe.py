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
                          "shown War Prep straight away.")],
    services=[Service(["{core}-felobby-1", "{core}-feworld-1", "{core}-fellb-1"], {54848: "lobby"},
                      warning="Everyone in Fantasy Earth is disconnected.")],
    live=sources.board(_parse, markers=["felobby", "feworld"]),
    # the wars under way and their phases ahead, off the tool port
    # (hippaulring world/devtool.py, calendar.json)
    calendar=sources.tool_calendar(),
)
