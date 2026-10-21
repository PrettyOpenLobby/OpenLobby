"""Dirge of Cerberus: Final Fantasy VII (PS2). One UDP world server."""
from adminmodules import Module, Service, sources

MODULE = Module(
    "doc", "Dirge of Cerberus", short="Dirge", platform="PlayOnline: PS2", content_ids=[10],
    markers=["doc"],
    services=[Service(["{core}-doc-1"], warning="Everyone in Dirge of Cerberus is disconnected.")],
    live=sources.board(markers=["doc"]),
)
