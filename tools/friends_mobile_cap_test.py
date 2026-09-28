#!/usr/bin/env python3
"""2:3 KGetFriendList: the mobile marker lifts the 12-row cap; the PC is untouched.

The PC Viewer sends 2:3 with an EMPTY payload and can only hold 12 rows per
read, so the reply is capped at 12. The mobile Tetra Master app marks its
request with the 4 ASCII bytes `MOB1` at payload +0 (decrypted message offset
0x28, right after the 40-byte request header) and gets up to
POL_FRIENDS_MOBILE_CAP (default 64) rows, in the same order with the same slot
numbers, so slot i agrees between a PC and a phone for i < 12.

Pinned here, against a member with 20 friends (2 of them pending):
  * no marker  -> 12 rows, and the WHOLE reply payload is byte-identical to
                  what the last single-file responders.py (commit 7a1a57a,
                  before the split into services/core/) builds for the same
                  DB (loaded from `git show 7a1a57a:...`, or from
                  $POL_BASELINE_RESPONDERS if set);
  * marker     -> 20 rows, count block 20, length 8 + 20*168, rows 0..11
                  byte-identical to the no-marker rows, slot map covers 0..19;
  * POL_FRIENDS_MOBILE=0 -> a marked request is served 12 rows, byte-identical
                  to the unmarked reply;
  * POL_FRIENDS_MOBILE_CAP=16 -> 16 rows;
  * NO PUSH FOR A SLOT >= 12, ever: a PC Viewer on the same member files a
    pushed row at table + slot*0xB0 in a 12-row table. A marked 2:3 queues
    exactly the PC's row spool + presence burst (slots 0..11, identical to
    the baseline's); `_friend_slot`, `_watcher_row_slot`, `push_friend_icons`,
    `push_presence_burst`, `field_push_lines`, both deliverers and
    `_broadcast_presence` (login/logout/status) all refuse slot 12+ and still
    serve slot 3.

Drives the real path: `_lobby_paylen` (the declared length) then
`_lobby_payload` with the decrypted request message, exactly as
`_build_lobby_reply_pt` does.
"""
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVICES = os.path.join(REPO, "services")
sys.path.insert(0, SERVICES)

TMP = tempfile.mkdtemp(prefix="friends-mobile-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_SEARCH_CALIB"] = os.path.join(TMP, "no-such.txt")
os.environ["POL_LOBBY_LIST_MODE"] = "2:3=friends"
# The row/icon spool and the presence burst are ON so the test can see what
# they queue; `_push_emit` is replaced by a recorder below, so nothing is
# spooled or sent.
os.environ["POL_PRESENCE_PUSH"] = "1"
os.environ["POL_FRIEND_ROW_PUSH"] = "1"
os.environ["POL_FRIEND_PRESENCE_BURST"] = "1"
os.environ.pop("POL_FRIENDS_MOBILE", None)
os.environ.pop("POL_FRIENDS_MOBILE_CAP", None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []
REC = 0xA8

# The last commit where services/responders.py was the whole lobby in one
# file. After it, responders.py is a facade over services/core/ and cannot be
# loaded on its own. The PC reply must still be byte-identical to what this
# build served, which the mobile list already shipped in.
BASELINE_COMMIT = "7a1a57a"


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def load_baseline():
    path = os.environ.get("POL_BASELINE_RESPONDERS")
    if not path:
        src = subprocess.run(
            ["git", "-C", REPO, "show",
             BASELINE_COMMIT + ":services/responders.py"],
            capture_output=True, check=True).stdout
        path = os.path.join(TMP, "responders_baseline.py")
        with open(path, "wb") as f:
            f.write(src)
    spec = importlib.util.spec_from_file_location("responders_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The baseline predates PostgreSQL: its OWN queries are written with
    # SQLite's ? placeholders. It reaches the database only through
    # `accounts`, so it gets an `accounts` whose connections accept those, and
    # the same database as the module under test. What it builds from the rows
    # is untouched, which is the thing being compared.
    mod.accounts = _QmarkAccounts()
    return mod


class _QmarkConn:
    """An account connection that also takes a `?`-placeholder statement."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=None):
        if params is not None and "?" in sql and "%s" not in sql:
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self._c.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *exc):
        return self._c.__exit__(*exc)


class _QmarkAccounts:
    """`accounts`, with connect() handing out `_QmarkConn`."""

    def connect(self, path=None):
        return _QmarkConn(accounts.connect())

    def __getattr__(self, name):
        return getattr(accounts, name)


def request(payload=b""):
    """A decrypted 2:3 request MESSAGE: 40-byte header + payload."""
    hdr = bytearray(0x28)
    hdr[0], hdr[1], hdr[2] = 0x02, 0x02, 0x03
    struct.pack_into("<I", hdr, 4, len(payload))
    return bytes(hdr) + payload


def serve(mod, req_pt):
    n = mod._lobby_paylen(0x02, 0x03, req_pt)
    pay = mod._lobby_payload(0x02, 0x03, n, req_pt)
    return n, bytes(pay)


def rows(pay):
    count = struct.unpack_from("<I", pay, 0)[0]
    return count, [pay[8 + i * REC:8 + (i + 1) * REC] for i in range(count)]


EMITTED = {}


def queued(mod):
    """{kind: [rows]} queued through `_push_emit` since the last call."""
    out = {}
    for rec in EMITTED[mod]:
        out.setdefault(rec["kind"], []).extend(rec["rows"])
    EMITTED[mod].clear()
    return out


def slots_of(q):
    return str({k: [r[0] for r in v] for k, v in q.items()})


class FakeSession:
    """A live push session that records what it was sent."""
    alive = True
    nick = b"WATCHER"
    srv = b"srv"

    def __init__(self):
        self.lines = []

    def send(self, lines):
        self.lines += list(lines)
        return True


def push_checks(mid, hid, marker):
    """Every other push path, after a MARKED 2:3 published slots 0..19."""
    print("pushes after a marked 2:3 (slot map 0..19) ->")
    serve(R, request(marker))
    queued(R)
    check(sorted(R._friend_slots_map(hid) or {}) == list(range(20)),
          "slot map is 0..19")
    db = accounts.connect(DB)
    try:
        peer = {r["peer_name"]: r for r in
                accounts.list_friends(db, hid, status=None)}
        p3, p15 = peer["Pal03"], peer["Pal16"]
        m15 = int(db.execute("SELECT member_id FROM handle WHERE id = %s",
                             (int(p15["peer_handle"]),)).fetchone()[0])
        m3 = int(db.execute("SELECT member_id FROM handle WHERE id = %s",
                            (int(p3["peer_handle"]),)).fetchone()[0])
        s15 = R._friend_slot_raw(db, hid, int(p15["peer_handle"]))
        check(s15 == 16, "the served map does hold Pal16 at slot 16", str(s15))
        check(R._friend_slot(db, hid, int(p15["peer_handle"])) is None,
              "_friend_slot: slot 16 -> None (no push may address it)")
        check(R._friend_slot(db, hid, int(p3["peer_handle"])) == 3,
              "_friend_slot: slot 3 -> 3, as before")
        check(R._watcher_row_slot(db, mid, int(p15["peer_guid"])) is None,
              "_watcher_row_slot (profile repaint): slot 16 -> None")
        check(R._watcher_row_slot(db, mid, int(p3["peer_guid"])) == 3,
              "_watcher_row_slot: slot 3 -> 3")
        g3, g15 = int(p3["peer_guid"]), int(p15["peer_guid"])
    finally:
        db.close()

    # Enqueue side (the accept pushes call these two as well).
    R.push_friend_icons(None, mid, [(15, g15, 7, None), (19, g15, 7, "x")])
    R.push_presence_burst(None, mid, [(12, g15, 1), (63, g15, 1)])
    check(queued(R) == {}, "push_friend_icons / push_presence_burst: slots "
                           "12+ queue NOTHING")
    R.push_friend_icons(None, mid, [(3, g3, 7, None), (15, g15, 7, None)])
    R.push_presence_burst(None, mid, [(3, g3, 1), (15, g15, 1)])
    q = queued(R)
    check([r[0] for r in q.get("rows", [])] == [3]
          and [r[0] for r in q.get("presencerows", [])] == [3],
          "mixed rows: only slot 3 is queued", slots_of(q))

    # The chokepoint every friend push record is built through.
    try:
        R.field_push_lines(b"N", g15, 12, state=1)
        check(False, "field_push_lines refuses slot 12")
    except ValueError:
        check(True, "field_push_lines refuses slot 12")
    check(len(R.field_push_lines(b"N", g3, 11, state=1)) == 1,
          "field_push_lines still builds slot 11")

    # Delivery side, for spool lines written by anything.
    ts = FakeSession()
    R.PRESENCE.sessions_for = lambda m: [ts] if int(m) == mid else []
    R._push_deliver_rows({"kind": "rows", "member": mid, "after": 0,
                          "rows": [[15, g15, 7, None], [40, g15, 7, None]]})
    R._push_deliver_presencerows({"kind": "presencerows", "member": mid,
                                  "after": 0, "rows": [[15, g15, m15]]})
    check(ts.lines == [], "deliverers send NOTHING for a spooled slot 12+",
          f"{len(ts.lines)} line(s)")
    R._push_deliver_rows({"kind": "rows", "member": mid, "after": 0,
                          "rows": [[3, g3, 7, None], [15, g15, 7, None]]})
    check(len(ts.lines) == 1, "_push_deliver_rows: slot 3 delivered, 15 not",
          f"{len(ts.lines)} line(s)")
    ts.lines.clear()
    R._push_deliver_presencerows({"kind": "presencerows", "member": mid,
                                  "after": 0,
                                  "rows": [[3, g3, m3], [15, g15, m15]]})
    check(len(ts.lines) == 1,
          "_push_deliver_presencerows: slot 3 delivered, 15 not",
          f"{len(ts.lines)} line(s)")
    ts.lines.clear()

    # Login / logout / status (4:5 watcher) all go through _broadcast_presence.
    for state in ("online", "offline", "away", "back"):
        R._broadcast_presence(m15, state)
    check(ts.lines == [], "_broadcast_presence for the slot-16 friend "
                          "(online/offline/away/back) sends NOTHING",
          f"{len(ts.lines)} line(s)")
    R._broadcast_presence(m3, "online")
    check(len(ts.lines) >= 1, "_broadcast_presence for the slot-3 friend "
                              "still pushes", f"{len(ts.lines)} line(s)")
    ts.lines.clear()


def main():
    conn = accounts.connect(DB)
    me = accounts.register_account(conn, "Mobileme", "hunter2pw")
    mid = int(me["member_id"])
    hid = int(conn.execute("SELECT id FROM handle WHERE member_id = %s",
                           (mid,)).fetchone()["id"])
    for i in range(20):
        a = accounts.register_account(conn, f"Pal{i:02d}", "hunter2pw")
        ph = conn.execute("SELECT id FROM handle WHERE member_id = %s",
                          (a["member_id"],)).fetchone()["id"]
        # Two OUTGOING pending rows, one inside the PC's 12 and one past it,
        # so pending rows ride both halves of the list.
        st = accounts.STATUS_PENDING if i in (5, 15) else accounts.STATUS_ACTIVE
        accounts.add_friend(conn, hid, f"Pal{i:02d}", peer_handle=int(ph),
                            status=st)
        # A face on every friend, so every served row has a row-spool entry.
        accounts.set_handle_profile(conn, int(ph), {R._PROFILE_FICON: 100 + i})
    conn.commit()
    conn.close()

    base = load_baseline()
    for mod in (R, base):
        mod._session_member_id = lambda: mid
        mod._session_handle_id = lambda db=None: hid
        mod._session_get = (lambda k, _o=mod._session_get:
                            mid if k == "member_id" else _o(k))
    for mod in (R, base):
        EMITTED[mod] = []
        mod._push_emit = (lambda rec, db=None, _l=EMITTED[mod]:
                          _l.append(json.loads(json.dumps(rec))) or 0)

    marker = b"MOB1"
    signed_marker = bytearray(8)                  # MOB1 + cksum32 trailer
    signed_marker[0:4] = marker
    struct.pack_into("<I", signed_marker, 4, R._lobby_cksum(bytes(marker)))

    print("PC (no marker) ->")
    n_old, p_old = serve(base, request())
    q_old = queued(base)
    n_pc, p_pc = serve(R, request())
    q_pc = queued(R)
    check(q_pc == q_old and set(q_pc) == {"rows", "presencerows"}
          and [r[0] for r in q_pc["rows"]] == list(range(12))
          and [r[0] for r in q_pc["presencerows"]] == list(range(12)),
          "PC 2:3 queues row spool + presence burst for slots 0..11, "
          "IDENTICAL to the baseline", slots_of(q_pc))
    c_pc, r_pc = rows(p_pc)
    check(c_pc == 12 and len(r_pc) == 12, "12 rows", f"count={c_pc}")
    check(n_pc == 8 + 12 * REC, "declared length 8 + 12*168", str(n_pc))
    check(n_pc == n_old, "length identical to the baseline", f"{n_pc} vs {n_old}")
    check(p_pc == p_old and len(p_pc) == n_pc,
          "payload BYTE-IDENTICAL to the baseline's",
          f"{len(p_pc)}B vs {len(p_old)}B")
    check(sorted((R._friend_slots_map(hid) or {})) == list(range(12)),
          "slot map = 0..11")
    # A payload that is not the marker is the PC path too.
    n_x, p_x = serve(R, request(b"MOB2"))
    check(queued(R) == q_pc, "...and queues exactly the PC's pushes")
    check((n_x, p_x) == (n_pc, p_pc), "a non-marker payload is served as the PC")

    print("mobile (MOB1 at payload +0) ->")
    for label, pl in (("bare MOB1", marker), ("MOB1 + cksum", bytes(signed_marker))):
        n_m, p_m = serve(R, request(pl))
        c_m, r_m = rows(p_m)
        check(c_m == 20 and len(r_m) == 20, f"[{label}] 20 rows", f"count={c_m}")
        check(n_m == 8 + 20 * REC and len(p_m) == n_m,
              f"[{label}] declared length 8 + 20*168 = payload length",
              f"n={n_m} len={len(p_m)}")
        check(r_m[:12] == r_pc, f"[{label}] rows 0..11 byte-identical to the PC's")
        check(p_m[4:8] == p_pc[4:8], f"[{label}] count block pad identical")
        # Each row names its own slot at +0x08 (the numbering 2:6 indexes).
        check([r[8] for r in r_m] == list(range(20)),
              f"[{label}] row i carries slot i",
              str([r[8] for r in r_m]))
        check(len(r_m) == 20
              and all(r_m[i] != r_m[j] for i in range(20) for j in range(i)),
              f"[{label}] 20 distinct rows")
        smap = R._friend_slots_map(hid) or {}
        check(sorted(smap) == list(range(20)), f"[{label}] slot map = 0..19",
              str(sorted(smap)))
        q_m = queued(R)
        check(q_m == q_pc,
              f"[{label}] pushes queued = the PC's exactly (slots 0..11); "
              "NOTHING for slots 12..19", slots_of(q_m))

    print("POL_FRIENDS_MOBILE=0 ->")
    os.environ["POL_FRIENDS_MOBILE"] = "0"
    try:
        n_off, p_off = serve(R, request(marker))
    finally:
        os.environ.pop("POL_FRIENDS_MOBILE", None)
    check(rows(p_off)[0] == 12 and (n_off, p_off) == (n_pc, p_pc),
          "marker ignored: 12 rows, identical to the PC reply")

    print("POL_FRIENDS_MOBILE_CAP=16 ->")
    os.environ["POL_FRIENDS_MOBILE_CAP"] = "16"
    try:
        n_16, p_16 = serve(R, request(marker))
    finally:
        os.environ.pop("POL_FRIENDS_MOBILE_CAP", None)
    check(rows(p_16)[0] == 16 and n_16 == 8 + 16 * REC,
          "16 rows, length 8 + 16*168", f"n={n_16}")

    push_checks(mid, hid, marker)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
