"""gmconsole desk <-> Discord sync: a call taken or closed on the admin panel
must show up in Discord, and a Discord knock must not take a call the desk
already has. Runs against a fake Discord API and in-memory alert/thread rows,
so it needs no Postgres and no network.

    python tools/gmconsole_sync_test.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="gmsync-")
os.environ["POL_GM_CALL_DIR"] = TMP

import discordlink  # noqa: E402
import gmconsole    # noqa: E402

gmconsole.GM_CALL_DIR = TMP
gmconsole._TICKET_STATE_PATH = os.path.join(TMP, "gm-tickets.json")

fails = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


# ---- fakes ---------------------------------------------------------------
class Store:
    def __init__(self):
        self.alerts, self.threads = {}, {}


S = Store()


class _Conn:
    def close(self):
        pass


def _install_fakes():
    L = discordlink
    L.connect = lambda: _Conn()
    L.gm_alerts_all = lambda c: [dict(a) for a in S.alerts.values()]
    L.gm_alert_get = lambda c, t: dict(S.alerts[t]) if t in S.alerts else None
    L.gm_alert_drop = lambda c, t: S.alerts.pop(t, None)
    L.gm_alert_claim = lambda c, t, who, now=None: S.alerts[t].update(claimed_by=who)
    L.gm_alert_unclaim = lambda c, t: S.alerts[t].update(claimed_by=None)
    L.gm_threads_open = lambda c: [dict(t) for t in S.threads.values()
                                   if t["closed_at"] is None]
    L.gm_thread_by_room = lambda c, r: next(
        (dict(t) for t in S.threads.values()
         if t["room"] == r and t["closed_at"] is None), None)

    def close(c, tid, now=None):
        S.threads[tid]["closed_at"] = 1.0
        return True
    L.gm_thread_close = close


calls = []


def fake_api(method, path, payload=None):
    calls.append((method, path, payload))
    return (204 if method == "DELETE" else 200), {}


def reset():
    S.alerts.clear()
    S.threads.clear()
    calls.clear()
    for n in os.listdir(TMP):
        os.remove(os.path.join(TMP, n))


def ticket(tid, room="#gmcall001", **extra):
    with open(os.path.join(TMP, tid + ".json"), "w", encoding="utf-8") as f:
        json.dump(dict({"room": room, "handle": "Bluebell", "guid": 7}, **extra), f)


def state(d):
    with open(gmconsole._TICKET_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f)


def alert(tid, room="#gmcall001", claimed_by=None):
    S.alerts[tid] = {"ticket_id": tid, "room": room, "guild_id": "g",
                     "channel_id": "C", "message_id": "M" + tid[-1],
                     "claimed_by": claimed_by}


def labels():
    return [p["components"][0]["components"][0]["label"]
            for m, _p, p in calls if m == "PATCH" and p and "components" in p]


TID = "gm-20260930T051000-1"


def main():
    _install_fakes()
    gmconsole.wire(fake_api)

    # 1) nothing happened on the desk: the sync leaves the alert alone
    reset(); ticket(TID); alert(TID); state({})
    gmconsole.sync_desk_state()
    check("an untouched call makes no Discord calls", calls == [], calls)

    # 2) desk knock -> alert reads "Claimed by ... (GM desk)", once
    reset(); ticket(TID); alert(TID)
    state({TID: {"status": "open", "knocked_at": 1.0, "knocked_by": "gm1"}})
    gmconsole.sync_desk_state()
    check("a desk knock relabels the alert", labels() == ["Claimed by gm1 (GM desk)"], labels())
    check("...and the claim is remembered", S.alerts[TID]["claimed_by"] == "desk:gm1",
          S.alerts[TID]["claimed_by"])
    calls.clear(); gmconsole.sync_desk_state()
    check("...so the next pass does nothing", calls == [], calls)

    # 3) a Discord knock's claim is not relabelled as a desk one
    reset(); ticket(TID); alert(TID, claimed_by="123456")
    state({TID: {"status": "open", "knocked_at": 1.0, "knocked_by": "discord:Gm1"}})
    gmconsole.sync_desk_state()
    check("a Discord-claimed alert is left alone", calls == [], calls)

    # 4) desk withdraws its knock -> Knock button comes back
    reset(); ticket(TID); alert(TID, claimed_by="desk:gm1"); state({TID: {"status": "open"}})
    gmconsole.sync_desk_state()
    check("a withdrawn desk knock restores the Knock button",
          labels() == ["Knock (open a private thread)"] and not S.alerts[TID]["claimed_by"],
          labels())

    # 5) desk close -> alert deleted, open thread told + archived + locked
    reset(); ticket(TID); alert(TID, claimed_by="123456")
    S.threads["T1"] = {"thread_id": "T1", "ticket_id": TID, "room": "#gmcall001",
                       "closed_at": None}
    state({TID: {"status": "closed", "by": "gm1"}})
    gmconsole.sync_desk_state()
    posts = [p["content"] for m, path, p in calls if m == "POST"]
    check("a desk close tells the thread who closed it",
          posts and "closed on the GM desk by gm1" in posts[0], posts)
    check("...archives and locks it",
          ("PATCH", "/channels/T1", {"archived": True, "locked": True}) in calls)
    check("...stops the relay", S.threads["T1"]["closed_at"] is not None)
    check("...and deletes the alert",
          ("DELETE", "/channels/C/messages/M1", None) in calls and TID not in S.alerts)

    # 6) player cancelled -> alert deleted
    reset(); ticket(TID, cancelled_at=5.0); alert(TID); state({})
    gmconsole.sync_desk_state()
    check("a cancelled call's alert is deleted", TID not in S.alerts, calls)

    # 7) a ticket gmd is mid-writing is NOT treated as gone
    reset(); alert(TID); state({})
    with open(os.path.join(TMP, TID + ".json"), "w") as f:
        f.write('{"room": "#gmc')
    gmconsole.sync_desk_state()
    check("a half-written ticket keeps its alert", TID in S.alerts and calls == [], calls)

    # 8) the per-pass cap spreads a backlog over ticks
    reset(); state({})
    for i in range(10):
        t = "gm-20260930T0510%02d-%d" % (i, i)
        alert(t)                        # no ticket file -> gone
    gmconsole.sync_desk_state()
    check("one pass stays under SYNC_MAX_CALLS",
          len(calls) == gmconsole.SYNC_MAX_CALLS, len(calls))

    # 9) Discord knock refuses a call the desk already took, or a closed one
    gmconsole._knocker_is_gm = lambda i: "gm1"
    reset(); ticket(TID); alert(TID)
    state({TID: {"status": "open", "knocked_at": 1.0, "knocked_by": "gm1"}})
    got = gmconsole.on_knock({}, TID)["data"]["content"]
    check("Discord knock refuses a desk-taken call",
          "already taken by gm1 (GM desk)" in got and calls == [], got)
    state({TID: {"status": "closed"}})
    got = gmconsole.on_knock({}, TID)["data"]["content"]
    check("Discord knock refuses a closed call", "already closed" in got, got)

    print()
    if fails:
        print("%d check(s) FAILED: %s" % (len(fails), fails))
        return 1
    print("gmconsole desk sync self-test OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
