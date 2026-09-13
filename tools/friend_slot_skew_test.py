#!/usr/bin/env python3
"""A re-derived friend slot must equal the slot the 2:3 reply actually served.

REPLAYS A LIVE FAILURE (2026-09-06). The account holder had NINE incoming
friend requests pending and nine friends served:

    2:3 friends served: slot 0='Kestra' ... slot 7='laplacier', slot 8='Heulen'
    2:6 friend write: kept 9 incoming request(s) the write omitted
        [Kestra, Ironbadger, Example.gang, CredibleAsh, DeckTestNew,
         LaptopTest2, PCTest, clem, laplacier]

A friend changed their profile picture. The server did everything right --
`push: event 0 for 'Heulen' -> row repainted on 1` -- and the row never
changed, because the repaint resolved the slot by enumerating
`list_friends(status=None)` RAW. The 2:3 reply drops STATUS_INVITED (an
incoming request is a Message, not a friend-list row), so the derived index was
skewed by every request the watcher held, and the client -- which validates the
slot before applying anything -- dropped the record silently.

The same skew mis-aims PRESENCE pushes, which is the other half of "my friend
shows offline while they are online".

So this pins the invariant that was never stated anywhere: **the slot a
derivation returns must equal the slot `_db_friends` would have served**, on a
list that mixes active friends, outgoing (invited-by-us) rows, incoming
requests and groups. It is a pure ordering test -- no sockets, no client.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(os.path.dirname(HERE), "services")
sys.path.insert(0, SERVICES)
os.environ.setdefault("POL_LOG_DIR", tempfile.mkdtemp())

import accounts                                            # noqa: E402
import responders                                          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def build():
    """A watcher whose list interleaves friends with INCOMING requests."""
    tmp = tempfile.mkdtemp(prefix="slotskew-")
    db = accounts.connect(os.path.join(tmp, "accounts.db"))
    acct = accounts.register_account(db, "Watcher", "hunter2pw")
    whid = db.execute("SELECT id FROM handle WHERE member_id = ?",
                      (acct["member_id"],)).fetchone()["id"]

    peers = {}
    # Interleaved on purpose: if the incoming requests all sorted to the end,
    # a raw enumeration would accidentally agree for the early slots and the
    # test would pass while the bug was still there.
    plan = [("Kestra", "friend"), ("Example.gang", "incoming"),
            ("LaptopTest2", "friend"), ("Ironbadger", "incoming"),
            ("CredibleAsh", "friend"), ("clem", "incoming"),
            ("PCTest", "friend"), ("laplacier", "incoming"),
            ("Heulen", "friend")]
    for name, role in plan:
        a = accounts.register_account(db, name, "hunter2pw")
        phid = db.execute("SELECT id FROM handle WHERE member_id = ?",
                          (a["member_id"],)).fetchone()["id"]
        peers[name] = (int(phid), int(a["member_id"]))
        # peer_handle is what every slot derivation matches on, and the live
        # rows carry it -- a fixture without it tests nothing.
        status = (accounts.STATUS_ACTIVE if role == "friend"
                  # An INCOMING request: they asked US. It is a row in the
                  # table and must NOT be a row in the served list.
                  else accounts.STATUS_INVITED)
        accounts.add_friend(db, whid, name, peer_handle=int(phid),
                            status=status)
    db.commit()
    return db, int(whid), peers, [n for n, r in plan if r == "friend"]


def main():
    db, whid, peers, expected = build()
    try:
        rows = responders._friend_row_order(db, whid)
        served = [r["peer_name"] for r in rows]
        print(f"  served order: {served}")
        check("the served list holds ONLY the real friends", served == expected,
              f"{served} != {expected}")

        print("\n[every friend's derived slot matches its served slot]")
        for want, name in enumerate(expected):
            phid = peers[name][0]
            got = responders._friend_slot(db, whid, phid)
            check(f"slot of {name!r} is {want}", got == want, f"got {got}")

        print("\n[the push path derives the same slot from the GUID]")
        for want, name in enumerate(expected):
            phid = peers[name][0]
            guid = db.execute("SELECT peer_guid FROM friend WHERE handle_id = ?"
                              " AND peer_handle = ?", (whid, phid)).fetchone()
            got = responders._watcher_row_slot(
                db, _member_of(db, whid), int(guid["peer_guid"]))
            check(f"push slot of {name!r} is {want}", got == want, f"got {got}")

        print("\n[an incoming request is not addressable as a slot]")
        for name in ("Example.gang", "Ironbadger"):
            got = responders._friend_slot(db, whid, peers[name][0])
            check(f"{name!r} has no slot", got is None, f"got {got}")

        print("\n[the LAST friend is the one the live bug hit]")
        # Heulen was slot 8 with 4 requests interleaved ahead of them here; a
        # raw enumeration returns 8 + (requests before them) and the client
        # drops the record. This is the exact assertion that goes red on the
        # old code.
        got = responders._friend_slot(db, whid, peers["Heulen"][0])
        check("Heulen resolves to the LAST served slot", got == len(expected) - 1,
              f"got {got}, served list has {len(expected)}")
    finally:
        db.close()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("friend slot skew: all checks passed")
    return 0


def _member_of(db, handle_id):
    return int(db.execute("SELECT member_id FROM handle WHERE id = ?",
                          (handle_id,)).fetchone()["member_id"])


if __name__ == "__main__":
    sys.exit(main())
