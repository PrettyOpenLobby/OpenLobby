"""One game's patches, for its page in the panel: every bundle the patch service hands out for
the game's PlayOnline products, each version labelled retail, ours, retail files under our
number or unknown, and the retail versions known to exist that are not here
(polpatch_lib.provenance; docs/patching.md, "Where each version comes from").

    GET /api/games/patches?key=<game>   anyone who can see the game (game.<key>)

The bundles come from the first of:

  1. the deployer (GET /api/patches/provenance), when one is set up;
  2. a module named "patches" whose tool port answers versions.json in the same shape: a
     server without a deployer runs one beside its patch service (prod's patchinfo).

A game's products are its content ids as four digits (Final Fantasy XI: 0001 and the test
server's 0015); the Viewer's are the Viewer itself and the friend list. A game with none, or a
server with neither source, has nothing to show: {"bundles": [], "source": null}.
"""
import admindeploy
import admingames

#: products that are not a game's content id: the Viewer and its friend list
EXTRA = {"viewer": ("1000", "0014")}


def products(module):
    """The patch products of a module: its content ids as four digits, and EXTRA's."""
    out = ["%04d" % int(c) for c in getattr(module, "content_ids", []) or []
           if isinstance(c, int) and 0 <= c < 10000]
    return out + [p for p in EXTRA.get(module.key, ()) if p not in out]


def _all(actor):
    """(status, {"bundles", "sources", "at"}, source) from the first source that answers."""
    if admindeploy.configured() is not None:
        st, out = admindeploy.patch_provenance()
        if st == 200 and isinstance(out, dict) and isinstance(out.get("bundles"), list):
            return 200, out, "deployer"
    if "patches" in admingames.configured():
        st, out = admingames.get_json("patches", "versions.json", "", actor)
        if st == 200 and isinstance(out.get("bundles"), list):
            return 200, out, "patchinfo"
        return 502, {"error": out.get("error") or "The patch versions service did not answer (%s)." % st}, None
    return 200, {"bundles": []}, None


def for_game(module, actor):
    """(status, body) for GET /api/games/patches."""
    want = products(module)
    if not want:
        return 200, {"bundles": [], "source": None, "products": []}
    st, out, source = _all(actor)
    if st != 200:
        return st, out
    rows = [b for b in out["bundles"] if isinstance(b, dict) and b.get("product") in want]
    rows.sort(key=lambda b: (want.index(b["product"]), str(b.get("region") or "")))
    return 200, {"bundles": rows, "sources": out.get("sources") or {}, "at": out.get("at"),
                 "source": source, "products": want}
