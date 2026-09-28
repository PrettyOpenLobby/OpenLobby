#!/usr/bin/env python3
"""live_sessions.py -- one tiny obligation that keeps a deploy from eating a game.

THE PROBLEM THIS GENERALISES. Recreating a container drops every live session
it holds, and `pol-git-sync` runs every 3 minutes, unattended. Tetra Master
solved this for itself: it published a live-match marker on every match
message, and both deploy scripts DEFER a login/authsess restart while that
marker is live and fresh. But the gate was hard-wired to one marker and two
services, so a push while someone was in an FE field or an FMO sortie or a
mahjong hanchan bounced their service anyway (memory:
feworld-push-restarts-live-session, seen twice).

THE CONTRACT. Any session-carrying service calls `start_heartbeat(service,
count_fn)` once, at startup. A daemon then publishes
{"count": count_fn(), "stamp": <epoch>} for that service every few seconds. The
deploy scripts read the marker of whatever they are about to recreate and defer
while count>0 and the stamp is fresh -- the identical rule TM already proved,
now generic.

WHERE IT LIVES. In the live-state store (polcore.kv), one key per service,
`live:<service>` = the record as JSON, expiring MARKER_TTL after the last
publish. It used to be `<POL_DATA_DIR>/<service>-sessions-live.json`; the
functions below keep the names they had, and add the readers the deploy gate,
the admin panel and the board bots use:

    marker_key(service)                    the kv key
    write_marker(service, count, extra=None)
    read_marker(service) -> {"count", "stamp", ...} or None
    read_count(service, stale=None) -> int or None (None = nobody said recently)
    live_markers() -> {service: record}
    start_heartbeat(service, count_fn, interval=10.0)
    thread_count(prefix)

and a command line for a deploy script, which cannot import Python:

    python live_sessions.py count <service> [--stale SECONDS]
        prints the count, or nothing when there is no fresh marker; exit 0
    python live_sessions.py list
        one line per marker: service, count, age in seconds

WHY A COUNT CALLBACK AND NOT A REGISTRY. Each service already knows its own
liveness cheaply -- most run one daemon thread per session with a service-name
prefix, so `thread_count("feworld-")` is the whole implementation and needs no
surgery into the per-session handlers (which is exactly where a freeze would be
introduced). `count_fn` lets each service report liveness however it naturally
can without this module dictating a data structure.

SAFE BY OMISSION. A service that never calls this publishes no marker, and an
absent marker means "not protected" -- i.e. today's behaviour. Nothing here can
make a deploy worse; it can only start deferring one that would have cut a
session. So it is fine to adopt service by service.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

try:
    from polcore import kv
except ImportError:                      # run from a directory above services/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from polcore import kv

#: Key prefix of every marker.
KEY_PREFIX = "live:"

#: How long a marker outlives its last publish. Far longer than any staleness
#: window a reader applies, so a service that died still shows as stale (with
#: its age) for a day instead of vanishing.
MARKER_TTL = float(os.environ.get("POL_LIVE_SESSIONS_TTL", "86400") or 86400)

#: A marker older than this reads as "nobody said recently" in read_count,
#: unless the caller passes its own window. The board bots' default.
STALE_DEFAULT = float(os.environ.get("POL_PRESENCE_STALE", "180") or 180)


def data_dir() -> str:
    return os.environ.get("POL_DATA_DIR", "/data")


def marker_key(service: str) -> str:
    return KEY_PREFIX + service


def thread_count(prefix: str) -> int:
    """Live daemon threads whose name starts with `prefix`.

    The natural liveness metric for the services that spawn one named thread
    per session (feworld-<ip>-<port>, felobby-..., fmo-...). Cheap and needs no
    per-session bookkeeping.
    """
    return sum(1 for t in threading.enumerate()
               if t.is_alive() and t.name.startswith(prefix))


def write_marker(service: str, count: int, extra: dict | None = None) -> None:
    """Publish {count, stamp} (plus `extra`) for `service`. Best-effort, never
    raises. One SET, so a reader never sees half a record."""
    try:
        rec = dict(extra or {})
        rec.update(count=int(count), stamp=time.time())
        kv.set(marker_key(service), json.dumps(rec), ttl=MARKER_TTL)
    except Exception:
        # A service must never fall over because the deploy gate could not be
        # written -- the gate is an optimisation, the session is the point.
        pass


def read_marker(service: str) -> dict | None:
    """The last record `service` published, or None."""
    try:
        raw = kv.get(marker_key(service))
        rec = json.loads(raw) if raw else None
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def read_count(service: str, stale: float | None = None) -> int | None:
    """How many live sessions `service` reported, or None if it has not said
    so within `stale` seconds (default STALE_DEFAULT; 0 or less = any age)."""
    stale = STALE_DEFAULT if stale is None else float(stale)
    rec = read_marker(service)
    if rec is None:
        return None
    try:
        if stale > 0 and time.time() - float(rec.get("stamp") or 0) > stale:
            return None
        return max(0, int(rec.get("count") or 0))
    except (TypeError, ValueError):
        return None


def live_markers() -> dict:
    """{service: record} for every marker in the store."""
    out = {}
    try:
        for name in kv.keys(KEY_PREFIX + "*"):
            service = name[len(KEY_PREFIX):]
            rec = read_marker(service)
            if rec is not None:
                out[service] = rec
    except Exception:
        pass
    return out


def start_heartbeat(service: str, count_fn, interval: float = 10.0):
    """Spawn the daemon that publishes `service`'s live-session count.

    `count_fn()` returns the current number of live sessions (0 is normal and
    correct -- it means "safe to recreate"). Returns the thread. Disabled with
    POL_LIVE_SESSIONS=0 (the marker then never appears, i.e. the service is
    unprotected, i.e. today's behaviour).
    """
    if os.environ.get("POL_LIVE_SESSIONS", "1") == "0":
        return None
    try:
        interval = float(os.environ.get("POL_LIVE_SESSIONS_INTERVAL", "")
                         or interval)
    except ValueError:
        pass

    def _loop():
        # Publish once immediately so a marker exists before the first tick --
        # a deploy landing in the first `interval` seconds still sees liveness.
        while True:
            try:
                write_marker(service, count_fn())
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name=f"live-sessions-{service}",
                         daemon=True)
    t.start()
    return t


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="read the live-session markers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("count", help="print one service's fresh count")
    c.add_argument("service")
    c.add_argument("--stale", type=float, default=None,
                   help="seconds a marker stays fresh (default %(default)s: "
                        "POL_PRESENCE_STALE or 180)")
    sub.add_parser("list", help="every marker, with its age")
    args = ap.parse_args(argv)
    if args.cmd == "count":
        n = read_count(args.service, args.stale)
        if n is not None:
            print(n)
        return 0
    now = time.time()
    for service, rec in sorted(live_markers().items()):
        print("%s %s %d" % (service, rec.get("count"),
                            round(now - float(rec.get("stamp") or 0))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
