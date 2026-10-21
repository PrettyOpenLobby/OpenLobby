"""Fantasy Earth: The Ring of Dominion. Tools are the world builder on the
world server's dev tool port."""
from adminmodules import Module, Service, Tool, sources


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


MODULE = Module(
    "fe", "Fantasy Earth", platform="PlayOnline: PC", content_ids=[11],
    markers=["felobby", "feworld"],
    tools=[Tool("world", "World builder", "")],
    services=[Service(["{core}-felobby-1", "{core}-feworld-1", "{core}-fellb-1"], {54848: "lobby"},
                      warning="Everyone in Fantasy Earth is disconnected.")],
    live=sources.board(_parse, markers=["felobby", "feworld"]),
)
