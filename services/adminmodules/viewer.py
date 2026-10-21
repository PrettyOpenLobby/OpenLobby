"""PlayOnline Viewer: the platform itself. Its page is the panel's own PML
preview; its services are the patch server and the portal."""
from adminmodules import Module, Service, Tool

MODULE = Module(
    "viewer", "PlayOnline Viewer", short="POL Viewer", platform="PlayOnline platform",
    kind="platform", present=True,
    about="Portal pages, the sign-up wizard and patching.",
    tools=[Tool("pml", "PML Preview", "pml", panel=True, perm="pml",
                about="Draws portal and title pages the way the Viewer does.")],
    services=[Service(["{core}-patch-1", "{core}-http-1"], {54000: "patch", 443: "web"},
                      name="Patch server and web pages",
                      warning="Anyone downloading an update has to retry it.")],
)
