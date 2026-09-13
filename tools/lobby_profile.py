"""WHERE DO THE MILLISECONDS GO? -- an offline profile of one lobby round-trip.

The account holder, watching retail SE beside our server on 2026-08-19:
"our server runs so much slower than [SE] on almost everything" -- friend-accept,
group-list and status-change round-trips were near-instant on SE and visibly
lagged on ours. The working rule for that effort was to MEASURE one
round-trip before changing anything, and this is that measurement.

What it profiles, offline against a temporary DB -- no client, no container:

    2:3   KGetFriendList      the friend list serve
    7:12  KGetGroupList       the group list serve (headers + member records)
    2:6   KPutFriendList      the reply build for a friend write

For each it reports the wall time and, separately, the time spent inside
`accounts.connect()` and the NUMBER of connections opened -- the lead hypothesis
going in, since `connect()` opens a fresh `sqlite3.connect` per call and
the responders open one per lookup rather than one per request.

    python tools/lobby_profile.py            # default fixture
    python tools/lobby_profile.py -n 50      # more iterations
    python tools/lobby_profile.py --friends 30 --members 20

It is a MEASUREMENT, not a test: it always exits 0 unless it cannot build the
fixture. Compare two runs (before/after a change) rather than reading one in
isolation -- the absolute numbers are this box's, not the server's.

WARNING: **AND "THIS BOX" MATTERS MORE THAN IT SOUNDS.** On the Windows dev box a bare
`sqlite3.connect`+`close` costs ~0.49 ms; on prod's Ubuntu VM it is ~0.017 ms.
Run this ON THE MACHINE you mean to reason about -- a dev run overstates the
connection tax by more than an order of magnitude, and reading it as a prod
number is what nearly sent the 2026-08-19 performance work after the wrong
cause. (It was the lobby reader's idle window, not the database.)
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

DB = os.path.join(tempfile.mkdtemp(prefix="lobby-profile-"), "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups,2:3=friends")
os.environ["POL_GROUP_CTL"] = os.path.join(os.path.dirname(DB), "no-such.ctl")
os.environ["POL_SEARCH_CALIB"] = os.path.join(os.path.dirname(DB), "no-such.txt")
os.environ["POL_LOG_DIR"] = os.path.dirname(DB)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402


class Counter:
    """Wrap `accounts.connect` so every open is counted and timed.

    The wrapper is installed on the `accounts` module itself, which is what
    `responders` calls through -- so it sees every open the request path makes,
    including the ones buried in helpers this file never names.
    """

    def __init__(self):
        self.opens = 0
        self.open_s = 0.0
        self.close_s = 0.0
        self._real = accounts.connect

    def __enter__(self):
        counter = self

        class Timed:
            """A pass-through proxy. `sqlite3.Connection.close` is read-only, so
            the only way to time the close is to own the attribute."""

            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, "_conn"), name)

            def __setattr__(self, name, value):
                setattr(object.__getattribute__(self, "_conn"), name, value)

            def close(self):
                t = time.perf_counter()
                object.__getattribute__(self, "_conn").close()
                counter.close_s += time.perf_counter() - t

        def wrapped(path=None):
            t0 = time.perf_counter()
            conn = self._real(path)
            self.open_s += time.perf_counter() - t0
            self.opens += 1
            return Timed(conn)
        accounts.connect = wrapped
        R.accounts.connect = wrapped
        return self

    def __exit__(self, *exc):
        accounts.connect = self._real
        R.accounts.connect = self._real
        return False


def fixture(n_friends, n_group_members):
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "PROFPOLID", "pw-polid", area_kbn="00",
                          login_pf="01")
    mid = accounts.add_member(conn, "PROFPOLID", "profmember", "pw-member")
    accounts.set_handle(conn, mid, "Prof")
    hid = accounts.primary_handle_row(conn, mid)["id"]
    for i in range(n_friends):
        accounts.add_friend(conn, hid, f"Friend{i:02d}", kind=accounts.KIND_FRIEND,
                            guid=0x80000000000 + i)
    gid = accounts.add_friend(conn, hid, "ProfGroup", kind=accounts.KIND_GROUP,
                              guid=0)
    accounts.add_group_member(conn, gid, "Prof", member_handle=hid)
    for i in range(n_group_members):
        accounts.add_group_member(conn, gid, f"Member{i:02d}")
    conn.close()
    R._session_member_id = lambda: mid
    R._session_handle_id = lambda db=None: hid
    return mid, hid


def bench(label, fn, n):
    """Run `fn` n times; report ms/op and the connection tax inside it."""
    fn()                                            # warm caches, ignore
    with Counter() as c:
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        total = time.perf_counter() - t0
    per = total / n * 1000.0
    conns = c.opens / n
    conn_ms = (c.open_s + c.close_s) / n * 1000.0
    share = (conn_ms / per * 100.0) if per else 0.0
    print(f"  {label:<28} {per:8.2f} ms/op   {conns:5.1f} conn/op   "
          f"{conn_ms:7.2f} ms in connect/close ({share:4.1f}%)")
    return per, conns, conn_ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=25, help="iterations per op")
    ap.add_argument("--friends", type=int, default=12)
    ap.add_argument("--members", type=int, default=8)
    args = ap.parse_args()

    fixture(args.friends, args.members)
    print(f"fixture: {args.friends} friends, {args.members} group members, "
          f"{args.n} iterations\ndb: {DB}\n")

    def serve(op1, op2):
        def go():
            count = R._list_count(op1, op2)
            n = R._list_paylen(op1, op2, count)
            R._list_payload(op1, op2, n)
        return go

    print("SERVE PATHS (what the client waits on)")
    bench("2:3  KGetFriendList", serve(0x02, 0x03), args.n)
    bench("7:12 KGetGroupList", serve(0x07, 0x0C), args.n)
    bench("  ..._groups_for_list only", lambda: R._groups_for_list(4), args.n)
    bench("  ..._group_members only",
          lambda: R._group_members(R._groups_for_list(4)), args.n)
    bench("  ..._db_friends only",
          lambda: R._db_friends(kinds=(accounts.KIND_FRIEND,)), args.n)

    print("\nBASELINE (the tax by itself)")

    def open_close():
        accounts.connect(DB).close()
    bench("accounts.connect + close", open_close, args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
