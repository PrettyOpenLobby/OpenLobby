"""Tetra Master. The matches run inside authsess (the hippaulmaster title
plugin), so restarting its services here never touches a game in progress:
these are the rankings job and the board."""
from adminmodules import Module, Service, sources


def _parse(doc, st):
    week = doc.get("week") if isinstance(doc.get("week"), dict) else {}
    if week.get("next"):
        st["facts"].append({"label": "rankings update", "value": week["next"], "kind": "time"})


MODULE = Module(
    "tm", "Tetra Master", platform="PlayOnline: PC and PS2", content_ids=[2],
    markers=["tm"], self_described=True,   # its board's tmadmin.py speaks the admin protocol
    services=[Service(["{core}-tmrank-1", "{core}-board-master-1|{core}-tmboard-1"], name="Rankings and board",
                      warning="Rankings and the board pause for a few seconds. Matches go on.")],
    live=sources.board(_parse, counted=[("tm", "matches")]),
)
