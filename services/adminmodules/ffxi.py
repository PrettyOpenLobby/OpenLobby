"""Final Fantasy XI: the LandSandBoat world behind the HippaulBridge.

Tools are the bridge's tool port (FFXI_FEDTOOL_PORT). Live status is the
who's-online JSON (POL_ADMIN_GAME_FFXI_LIVE: a list of {name, zone})."""
from adminmodules import Action, Module, Service, Tool, sources


def _parse(doc, st):
    zones = {p["where"] for p in st["players"] if p["where"]}
    if st["players"]:
        st["facts"].append({"label": "zones with players", "value": len(zones)})
    st["rooms_label"] = "zones"


def _announce(m, params, actor):
    import admingames
    text = " ".join(str(params.get("text") or "").split())
    if not text:
        return False, "Nothing to announce."
    return admingames.call("ffxi", "notices/announce",
                           {"text": text[:440], "urgent": bool(params.get("urgent", True))}, actor)


MODULE = Module(
    "ffxi", "Final Fantasy XI", short="FFXI", platform="PlayOnline: PC and PS2",
    content_ids=[1, 15],
    tools=[Tool("federation", "Federation", ""),
           Tool("notices", "Notices", "notices"),
           Tool("bonanza", "Bonanza", "bonanza"),
           Tool("chars", "Characters", "chars"),
           Tool("ah", "Auction house", "ah"),
           Tool("events", "Seasonal events", "events")],
    actions=[Action("announce", "Announce in game", _announce, kind="announce",
                    fields=[{"id": "text", "label": "Message", "type": "text"}],
                    about="A line in every player's chat log.")],
    services=[Service(["{core}-connect-1", "{core}-search-1", "{core}-world-1",
                       "{core}-map-1", "{core}-bridge-1"], {54001: "character select"},
                      warning="Everyone in FFXI is disconnected. The world takes a minute "
                              "or two to come back.")],
    live=sources.board(_parse),
)
