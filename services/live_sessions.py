#!/usr/bin/env python3
"""live_sessions.py -- one tiny obligation that keeps a deploy from eating a game.

THE PROBLEM THIS GENERALISES. Recreating a container drops every live session
it holds, and `pol-git-sync` runs every 3 minutes, unattended. Tetra Master
solved this for itself: `tetramaster._live_matches_write` touches
`data/tm-matches-live.json` on every match message, and both deploy scripts
DEFER a login/authsess restart while that marker is live and fresh. But the
gate was hard-wired to that one file and those two services, so a push while
someone was in an FE field or an FMO sortie or a mahjong hanchan bounced their
service anyway (memory: feworld-push-restarts-live-session, seen twice).

THE CONTRACT. Any session-carrying service calls `start_heartbeat(service,
count_fn)` once, at startup. A daemon then writes
`data/<service>-sessions-live.json` = {"count": count_fn(), "stamp": <epoch>}
every few seconds, atomically. The deploy scripts read `<service>-...` for
whatever they are about to recreate and defer while count>0 and the stamp is
fresh -- the identical rule TM already proved, now generic.

WHY A COUNT CALLBACK AND NOT A REGISTRY. Each service already knows its own
liveness cheaply -- most run one daemon thread per session with a service-name
prefix, so `thread_count("feworld-")` is the whole implementation and needs no
surgery into the per-session handlers (which is exactly where a freeze would be
introduced). `count_fn` lets each service report liveness however it naturally
can without this module dictating a data structure.

SAFE BY OMISSION. A service that never calls this writes no marker, and an
absent marker means "not protected" -- i.e. today's behaviour. Nothing here can
make a deploy worse; it can only start deferring one that would have cut a
session. So it is fine to adopt service by service.
"""

from __future__ import annotations

import json
import os
import threading
import time


def data_dir() -> str:
    return os.environ.get("POL_DATA_DIR", "/data")


def marker_path(service: str) -> str:
    return os.path.join(data_dir(), f"{service}-sessions-live.json")


def thread_count(prefix: str) -> int:
    """Live daemon threads whose name starts with `prefix`.

    The natural liveness metric for the services that spawn one named thread
    per session (feworld-<ip>-<port>, felobby-..., fmo-...). Cheap and needs no
    per-session bookkeeping.
    """
    return sum(1 for t in threading.enumerate()
               if t.is_alive() and t.name.startswith(prefix))


def write_marker(service: str, count: int) -> None:
    """Atomically publish {count, stamp} for `service`. Best-effort, never raises.

    Per-writer tmp name and os.replace, for the same reason tetramaster's own
    marker uses them: two writers sharing one ".tmp" can interleave and publish
    corrupt JSON, which the deploy scripts would read as "0 live" -- the unsafe
    direction.
    """
    path = marker_path(service)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            f.write(json.dumps({"count": int(count), "stamp": time.time()}))
        os.replace(tmp, path)
    except Exception:
        # A service must never fall over because the deploy gate could not be
        # written -- the gate is an optimisation, the session is the point.
        try:
            os.unlink(tmp)
        except Exception:
            pass


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
