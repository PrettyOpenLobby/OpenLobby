"""Front Mission Online. Tools are the lobby NPC editor and the story gates
on the fmo service's dev tool port, which also serves the calendar."""
from adminmodules import Module, Service, Tool, sources


def _parse(doc, st):
    phase = doc.get("phase") if isinstance(doc.get("phase"), dict) else {}
    if phase.get("n"):
        st["summary"] = "War phase %s%s" % (phase["n"], ", ceasefire" if phase.get("ceasefire") else "")
    score = doc.get("score") if isinstance(doc.get("score"), dict) else {}
    for side in ("ocu", "usn"):
        if side in score:
            st["facts"].append({"label": "%s cities" % side.upper(), "value": score[side]})


MODULE = Module(
    "fmo", "Front Mission Online", short="FMO", platform="PlayOnline: PC and PS2",
    content_ids=[4], markers=["fmo"],
    tools=[Tool("npcs", "Lobby NPCs", ""), Tool("gates", "Story gates", "gates"),
           Tool("coliseum", "Coliseum", "coliseum"), Tool("war", "War", "war")],
    services=[Service(["{core}-fmo-1"], {61300: "lobby"},
                      warning="Everyone in Front Mission Online is disconnected.")],
    live=sources.board(_parse, markers=["fmo"]),
    # the war's phases and the Coliseum's seasons and arenas, off the tool port
    # (hippaulfront fmoserver/devtool.py, calendar.json)
    calendar=sources.tool_calendar(),
)
