"""Which title a member is in: leases, publishing across processes, expiry."""
import json
import os
import time
from srvcore import log


#: WHICH TITLE A MEMBER IS IN (2026-08-16). The client says so itself, in the
#: 4:5 KChangeMyStatus state: u16 at +20 is the zone -- 1000 the Viewer, and the
#: CONTENT ID of the title otherwise (Tetra Master is 2) -- with +19 set to 1
#: while inside one. Measured four ways in both directions.
#: Until this, `_presence_zone` hardcoded _PRESENCE_ZONE_VIEWER, so a
#: watcher saw "in the Viewer" for a whole Tetra Master session.
#:
#: The 4:5 lands on the LOBBY (`login`) and the push happens in `authsess`, two
#: containers -- so this crosses the same way room state does: one writer, an
#: atomic replace, and an mtime-cached read on the other side.
_TITLE_ZONE_FILE = os.environ.get(
    "POL_TITLE_ZONE_FILE", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                        "title-zone.json"))
_TITLE_ZONE_CACHE = {"mtime": -1.0, "data": {}}
#: A client that dies without saying "I left the title" would otherwise read as
#: in-title forever. Long enough not to expire a real session.
_TITLE_ZONE_TTL = 12 * 3600

#: THE LEASE TTL, for zones the SERVER publishes rather than the client.
#:
#: 12 hours is right for a client-reported zone, because the client also reports
#: the EXIT (`4:5` with in_title=0) and the TTL is only a backstop for a console
#: that died. It is badly wrong for a zone we synthesised, because there is no
#: exit report to wait for -- see `_title_zone_lease`.
#:
#: 900s is measured, not picked. The PS2 title's inter-message gaps
#: over 4082 samples:
#: p50 2.0s, p90 3.3s, p95 5.1s, **p99 105s**, max 586s. A short lease (60-90s)
#: would expire mid-session on any of the ~1% of gaps past 105s and flap the
#: friend list; 900s clears a dead console in 15 minutes and sits comfortably
#: above the longest gap ever observed. POL_TITLE_LEASE_TTL retunes it without a
#: rebuild if a longer idle turns up.
_TITLE_ZONE_LEASE_TTL = int(os.environ.get("POL_TITLE_LEASE_TTL", "900"))

#: Refresh at a third of the lease, so two refreshes can be lost before a live
#: session expires. Without this the lease would rewrite the file on EVERY game
#: message -- one title alone logged 10,361 of them -- and two containers do
#: read-modify-write on it.
_TITLE_ZONE_REFRESH = max(5, _TITLE_ZONE_LEASE_TTL // 3)


def _live_title_zones():
    """Per-member title state, re-read only when the file's mtime moves."""
    try:
        mtime = os.stat(_TITLE_ZONE_FILE).st_mtime
    except OSError:
        return {}
    if mtime != _TITLE_ZONE_CACHE["mtime"]:
        try:
            with open(_TITLE_ZONE_FILE, "r", encoding="utf-8") as f:
                _TITLE_ZONE_CACHE["data"] = json.load(f) or {}
            _TITLE_ZONE_CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _TITLE_ZONE_CACHE["data"]   # torn write: last good view stands
    return _TITLE_ZONE_CACHE["data"]


def _publish_title_zone(member_id, zone, in_title):
    """Record (or clear) the title this member is in. Lobby side only."""
    if member_id is None:
        return
    try:
        state = dict(_live_title_zones())
        key = str(int(member_id))
        if in_title:
            state[key] = {"zone": int(zone), "at": time.time()}
        elif key not in state:
            return                            # nothing to clear, nothing to write
        else:
            del state[key]
        os.makedirs(os.path.dirname(_TITLE_ZONE_FILE), exist_ok=True)
        tmp = _TITLE_ZONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _TITLE_ZONE_FILE)     # atomic
        _TITLE_ZONE_CACHE["mtime"] = -1.0     # our own next read re-stats
    except (OSError, ValueError) as exc:
        log("lobby", f"  title zone: cannot publish ({exc})")


def _title_zone_lease(member_id, zone):
    """Publish a title zone the SERVER inferred, as a LEASE that expires.

    WHY THIS IS NOT `_publish_title_zone`. That one records what the CLIENT said
    on 4:5, and the client also says when it leaves -- so the entry is a latch and
    its 12-hour TTL only covers a console that died. **The PS2 title
    never sends 4:5 at all**, in either direction (measured 2026-08-17: zone 3
    appears zero times in four log generations, against 10,361 game messages),
    so a latch set here would never be cleared and the friend list would show
    "in the title" forever
    -- including after the player is back at the Viewer, which is worse than
    showing nothing.

    So this writes `lease`, and every reader treats the entry as gone once it
    lapses. Three things then clear it, in order of speed:

      * the Viewer reporting `4:5 zone=1000` on return -- instant, and it wins
        because `_publish_title_zone` deletes the key outright;
      * the member going offline -- `_presence_zone` already answers 0 for an
        offline subject, and the watcher skips them;
      * the lease lapsing -- the backstop, and for that title the ONLY one that
        covers "quit the game but stayed logged in", because no exit is reported.

    THE REFRESH IS FREE. The caller is the per-message game dispatch, so a live
    session renews the lease simply by being played; there is no timer to run and
    nothing to unwind if the process dies. The throttle below is what keeps that
    from rewriting the file thousands of times a session.
    """
    if member_id is None:
        return
    try:
        key = str(int(member_id))
    except (TypeError, ValueError):
        return
    row = (_live_title_zones() or {}).get(key) or {}
    # Same zone and refreshed recently -> nothing to do. A DIFFERENT zone always
    # writes: that is the player moving between titles and the friend list should
    # follow it immediately, not at the next refresh tick.
    if row.get("zone") == int(zone) \
            and time.time() - row.get("at", 0) < _TITLE_ZONE_REFRESH:
        return
    try:
        state = dict(_live_title_zones())
        state[key] = {"zone": int(zone), "at": time.time(),
                      "lease": _TITLE_ZONE_LEASE_TTL}
        os.makedirs(os.path.dirname(_TITLE_ZONE_FILE), exist_ok=True)
        tmp = _TITLE_ZONE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _TITLE_ZONE_FILE)
        _TITLE_ZONE_CACHE["mtime"] = -1.0
        if row.get("zone") != int(zone):
            log("authserv", f"  title zone: member {key} LEASED zone {int(zone)} "
                            f"for {_TITLE_ZONE_LEASE_TTL}s (renewed by traffic)")
    except (OSError, ValueError) as exc:
        log("authserv", f"  title zone: cannot lease ({exc})")


def _title_zone_expired(row):
    """True if this entry has lapsed. A leased entry uses its own (short) TTL;
    a client-reported one keeps the long backstop, because the client reports
    its own exit and the TTL is not what clears it."""
    if not isinstance(row, dict) or row.get("zone") is None:
        return True
    ttl = row.get("lease") or _TITLE_ZONE_TTL
    return time.time() - row.get("at", 0) > ttl


def _title_zone(member_id):
    """The content id of the title this member is in, or None. POL_TITLE_ZONE=0
    reverts to the pre-2026-08-16 behaviour (everyone online is "in the Viewer")."""
    if os.environ.get("POL_TITLE_ZONE", "1") != "1":
        return None
    try:
        row = _live_title_zones().get(str(int(member_id))) or {}
    except (TypeError, ValueError):
        return None
    if _title_zone_expired(row):
        return None
    return int(row["zone"])
