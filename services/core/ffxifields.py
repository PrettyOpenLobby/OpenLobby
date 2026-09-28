"""FFXI world and character fields (to move into the FFXI bridge title)."""
import json
import os
from srvcore import log
from . import handlelists



def _ffxi_world_fields():
    """`{Content ID: world identity dword}` from the bridge's id map.

    Re-read whenever the file's mtime moves -- the bridge rewrites it the moment
    a character is created, and the client re-fetches `1:3` about three seconds
    later, so a value cached for the life of the process would be one login
    stale exactly when it matters most.

    A charid with no recorded `world_field` gets one DERIVED here from the same
    packing. Without that, a map written before this field existed (or by a
    bridge that has not seen a char list yet) would cost a whole extra
    launch-relogin-launch cycle before FFXI could connect, because the table is
    filled at POL login and the field is only learned once the game is already
    running. The recorded value always wins -- it is what the client was
    actually told.
    """
    try:
        mtime = os.path.getmtime(handlelists._FFXI_IDMAP)
    except OSError:
        # WARNING: ABSENT IS NOT SILENT ANY MORE (2026-08-23). This returned {} without
        # a word, on the reasoning at _FFXI_IDMAP above -- a stack with no LSB
        # overlay has no map and wants the old behaviour. True, and it made a
        # MISCONFIGURED stack indistinguishable from an unconfigured one: prod's
        # bridge was moved to write /data/ffxi_idmap.json while this kept its
        # /lsb default, and the lobby read a path that did not exist for days
        # with nothing anywhere saying so. The cost is POL-0001 -- no world
        # field means FFXI's world lookup cannot match and the world socket is
        # never opened.
        #
        # Said ONCE per path, not per call: this runs on every 1:3.
        if handlelists._FFXI_IDMAP not in handlelists._ffxi_missing_warned:
            handlelists._ffxi_missing_warned.add(handlelists._FFXI_IDMAP)
            log("lobby", f"WARNING: FFXI id map {handlelists._FFXI_IDMAP} DOES NOT EXIST -- every "
                         f"FFXI character will be served world field 0, and "
                         f"FFXI will refuse the world connect (POL-0001). If the "
                         f"bridge is running, POL_FFXI_IDMAP disagrees with its "
                         f"FFXI_IDMAP_FILE; see tools/ffxi_idmap_check.py. If "
                         f"there is no LSB overlay on this stack, this is "
                         f"expected and can be ignored.")
        return {}
    # A path that came back is a path that works: clear the latch so a map that
    # appears later (the bridge's first write) can complain again if it vanishes.
    handlelists._ffxi_missing_warned.discard(handlelists._FFXI_IDMAP)
    if handlelists._ffxi_world_cache["mtime"] == mtime:
        return handlelists._ffxi_world_cache["map"]
    out = {}
    prof = {}
    try:
        with open(handlelists._FFXI_IDMAP, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for charid, ent in raw.items():
            if isinstance(ent, dict):
                if not ent.get("content_id"):
                    continue
                cid = int(ent["content_id"])
                field = int(ent.get("world_field") or 0) & 0xFFFFFFFF
                # THE CONTENT PROFILE'S TAIL, off the same 0x20 record the world
                # field comes from (`ffxi_bridge.note_char_fields`). Read in the
                # same pass because it is the same file and the same mtime poll
                # -- a second reader would double the open on every 1:3.
                if isinstance(ent.get("profile"), dict):
                    prof[cid] = dict(ent["profile"])
            else:
                # Legacy flat entry {charid: content_id} -- the bridge's
                # load_idmap still accepts these (a restored .bak-* is how
                # they come back), so skipping them here served world field 0
                # (= POL-0001) for a map the bridge itself considered valid.
                # Derive the field exactly as the no-field dict branch does.
                cid = int(ent)
                field = 0
            if not field:
                cid24 = int(charid) & 0xFFFFFF
                field = (((((handlelists._FFXI_WORLD_ID & 0xFFFF) << 8) | (cid24 & 0xFFFF0000))
                          << 8) | (cid24 & 0xFFFF)) & 0xFFFFFFFF
            out[cid] = field
    except Exception as exc:
        # Do NOT cache the failure. The old arm stored {} under this mtime, so
        # a single torn read (the bridge's writer used to truncate in place)
        # kept serving world field 0 -- POL-0001 -- until the NEXT write moved
        # the mtime. Keep the last good map and retry on the next 1:3.
        log("lobby", f"FFXI id map {handlelists._FFXI_IDMAP} unreadable ({exc!r}); "
                     f"keeping the previous map "
                     f"({len(handlelists._ffxi_world_cache['map'])} entries) and retrying "
                     f"on the next fetch")
        return handlelists._ffxi_world_cache["map"]
    handlelists._ffxi_world_cache["mtime"] = mtime
    handlelists._ffxi_world_cache["map"] = out
    handlelists._ffxi_world_cache["prof"] = prof
    return out


def _ffxi_char_fields(cid):
    """The profile tail the bridge recorded for one FFXI Content ID, or {}.

    Goes through `_ffxi_world_fields` so the two share one mtime poll and can
    never be a write apart. Empty for a character whose char list the bridge has
    not seen -- the profile then leaves those fields unset, which is correct: we
    do not know them, and a zero Job Level would read as a fact.
    """
    try:
        _ffxi_world_fields()
    except Exception:
        return {}
    return handlelists._ffxi_world_cache.get("prof", {}).get(int(cid)) or {}
