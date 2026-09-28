#!/usr/bin/env python3
"""2:3 KGetFriendList: every client gets the whole list, up to the 0x40 row cap.

Until 2026-09-28 the PC reply was capped at 12 rows and only a mobile-marked
request (the Tetra Master app, MOB1 at payload +0) got more. The 12 came from
polcore's 2:3 reader: `min(remaining*168, 0x7e0)` bytes per socket read, which
is a 12-record READ CHUNK -- the reader files each chunk at slot byte +0x08 and
re-enters the read until the U32 count is spent. Read as a list cap, it dropped
every friend past the 12th for any account holding more (a member with 19
friends never received 7 of them). The cap is now `_LOBBY_LIST[(2,3)][2]` = 0x40 for everyone, the
ceiling the push records' slot byte (+0x1c) and the slot maps allow.

Asserts, on an account with 20 friends (two of them outgoing-pending):

  * no marker  -> 20 rows, slot i at +0x08 of row i, slot map 0..19, and the
    row spool + presence burst queued for ALL 20 slots;
  * MOB1 (bare, or with its cksum32 trailer) -> byte-identical to the PC reply
    and the same pushes: the marker changes nothing any more, and still parses;
  * POL_FRIENDS_MOBILE=0 / POL_FRIENDS_MOBILE_CAP=16 -> the list cap wins
    (never below it), so still 20 rows;
  * pushes: every friend push path (`push_friend_icons`, `push_presence_burst`,
    `field_push_lines`, the row/presence deliverers, `_broadcast_presence`)
    addresses slots below 0x40 and refuses 0x40+, both at enqueue and delivery.

Drives the real path: `_lobby_paylen` (the declared length) then
`_lobby_payload` with the decrypted request message, exactly as
`_build_lobby_reply_pt` does.

Run:  python tools/friends_mobile_cap_test.py
"""
import json
import os
import struct
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
CAP = R._LOBBY_LIST[(0x02, 0x03)][2]


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


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


def push_checks(mid, hid):
    """Every other push path, after a 2:3 published slots 0..19."""
    print("pushes after a 2:3 (slot map 0..19) ->")
    serve(R, request())
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
        check(s15 == 16, "the served map holds Pal16 at slot 16", str(s15))
        check(R._friend_slot(db, hid, int(p15["peer_handle"])) == 16,
              "_friend_slot: slot 16 -> 16 (a push may address it now)")
        check(R._friend_slot(db, hid, int(p3["peer_handle"])) == 3,
              "_friend_slot: slot 3 -> 3")
        check(R._watcher_row_slot(db, mid, int(p15["peer_guid"])) == 16,
              "_watcher_row_slot (profile repaint): slot 16 -> 16")
        check(R._watcher_row_slot(db, mid, int(p3["peer_guid"])) == 3,
              "_watcher_row_slot: slot 3 -> 3")
        g3, g15 = int(p3["peer_guid"]), int(p15["peer_guid"])
    finally:
        db.close()

    # Enqueue side (the accept pushes call these two as well).
    R.push_friend_icons(None, mid, [(CAP, g15, 7, None), (200, g15, 7, "x")])
    R.push_presence_burst(None, mid, [(CAP, g15, 1), (255, g15, 1)])
    check(queued(R) == {}, "push_friend_icons / push_presence_burst: slots "
                           f"{CAP:#x}+ queue NOTHING")
    R.push_friend_icons(None, mid, [(3, g3, 7, None), (15, g15, 7, None),
                                    (CAP, g15, 7, None)])
    R.push_presence_burst(None, mid, [(3, g3, 1), (15, g15, 1), (CAP, g15, 1)])
    q = queued(R)
    check([r[0] for r in q.get("rows", [])] == [3, 15]
          and [r[0] for r in q.get("presencerows", [])] == [3, 15],
          f"mixed rows: slots 3 and 15 queued, {CAP:#x} dropped", slots_of(q))

    # The chokepoint every friend push record is built through.
    try:
        R.field_push_lines(b"N", g15, CAP, state=1)
        check(False, f"field_push_lines refuses slot {CAP:#x}")
    except ValueError:
        check(True, f"field_push_lines refuses slot {CAP:#x}")
    check(len(R.field_push_lines(b"N", g15, CAP - 1, state=1)) == 1,
          f"field_push_lines builds slot {CAP - 1:#x}")
    check(len(R.field_push_lines(b"N", g15, 15, state=1)) == 1,
          "field_push_lines builds slot 15")

    # Delivery side, for spool lines written by anything.
    ts = FakeSession()
    R.PRESENCE.sessions_for = lambda m: [ts] if int(m) == mid else []
    R._push_deliver_rows({"kind": "rows", "member": mid, "after": 0,
                          "rows": [[CAP, g15, 7, None], [200, g15, 7, None]]})
    R._push_deliver_presencerows({"kind": "presencerows", "member": mid,
                                  "after": 0, "rows": [[CAP, g15, m15]]})
    check(ts.lines == [],
          f"deliverers send NOTHING for a spooled slot {CAP:#x}+",
          f"{len(ts.lines)} line(s)")
    R._push_deliver_rows({"kind": "rows", "member": mid, "after": 0,
                          "rows": [[3, g3, 7, None], [15, g15, 7, None],
                                   [CAP, g15, 7, None]]})
    check(len(ts.lines) == 2, "_push_deliver_rows: slots 3 and 15 delivered, "
                              f"{CAP:#x} not", f"{len(ts.lines)} line(s)")
    ts.lines.clear()
    R._push_deliver_presencerows({"kind": "presencerows", "member": mid,
                                  "after": 0,
                                  "rows": [[3, g3, m3], [15, g15, m15],
                                           [CAP, g15, m15]]})
    check(len(ts.lines) == 2,
          f"_push_deliver_presencerows: slots 3 and 15 delivered, {CAP:#x} not",
          f"{len(ts.lines)} line(s)")
    ts.lines.clear()

    # Login / logout / status (4:5 watcher) all go through _broadcast_presence.
    for state in ("online", "offline", "away", "back"):
        R._broadcast_presence(m15, state)
    check(len(ts.lines) >= 4, "_broadcast_presence for the slot-16 friend "
                              "(online/offline/away/back) pushes every time",
          f"{len(ts.lines)} line(s)")
    ts.lines.clear()
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
        # Two OUTGOING pending rows, one inside the old PC 12 and one past it,
        # so pending rows ride both halves of the list.
        st = accounts.STATUS_PENDING if i in (5, 15) else accounts.STATUS_ACTIVE
        accounts.add_friend(conn, hid, f"Pal{i:02d}", peer_handle=int(ph),
                            status=st)
        # A face on every friend, so every served row has a row-spool entry.
        accounts.set_handle_profile(conn, int(ph), {R._PROFILE_FICON: 100 + i})
    conn.commit()
    conn.close()

    R._session_member_id = lambda: mid
    R._session_handle_id = lambda db=None: hid
    R._session_get = (lambda k, _o=R._session_get:
                      mid if k == "member_id" else _o(k))
    EMITTED[R] = []
    R._push_emit = (lambda rec, db=None, _l=EMITTED[R]:
                    _l.append(json.loads(json.dumps(rec))) or 0)

    marker = b"MOB1"
    signed_marker = bytearray(8)                  # MOB1 + cksum32 trailer
    signed_marker[0:4] = marker
    struct.pack_into("<I", signed_marker, 4, R._lobby_cksum(bytes(marker)))

    check(CAP == 0x40,
          "the 2:3 record cap is 0x40 (not the 12-record read chunk)",
          f"cap={CAP}")

    print("PC (no marker) ->")
    n_pc, p_pc = serve(R, request())
    q_pc = queued(R)
    check(set(q_pc) == {"rows", "presencerows"}
          and [r[0] for r in q_pc["rows"]] == list(range(20))
          and [r[0] for r in q_pc["presencerows"]] == list(range(20)),
          "PC 2:3 queues row spool + presence burst for ALL slots 0..19",
          slots_of(q_pc))
    c_pc, r_pc = rows(p_pc)
    check(c_pc == 20 and len(r_pc) == 20, "20 rows", f"count={c_pc}")
    check(n_pc == 8 + 20 * REC and len(p_pc) == n_pc,
          "declared length 8 + 20*168 = payload length", f"n={n_pc}")
    check([r[8] for r in r_pc] == list(range(20)),
          "row i carries slot i at +0x08", str([r[8] for r in r_pc]))
    check(all(r_pc[i] != r_pc[j] for i in range(20) for j in range(i)),
          "20 distinct rows")
    check(sorted((R._friend_slots_map(hid) or {})) == list(range(20)),
          "slot map = 0..19")
    # A payload that is not the marker is the PC path too.
    n_x, p_x = serve(R, request(b"MOB2"))
    check(queued(R) == q_pc, "...and queues exactly the PC's pushes")
    check((n_x, p_x) == (n_pc, p_pc), "a non-marker payload is served as the PC")

    print("mobile (MOB1 at payload +0) ->")
    for label, pl in (("bare MOB1", marker),
                      ("MOB1 + cksum", bytes(signed_marker))):
        n_m, p_m = serve(R, request(pl))
        check((n_m, p_m) == (n_pc, p_pc),
              f"[{label}] byte-identical to the PC reply "
              "(the marker changes nothing)")
        check(queued(R) == q_pc, f"[{label}] pushes queued = the PC's exactly")

    print("POL_FRIENDS_MOBILE=0 / POL_FRIENDS_MOBILE_CAP=16 ->")
    os.environ["POL_FRIENDS_MOBILE"] = "0"
    try:
        n_off, p_off = serve(R, request(marker))
    finally:
        os.environ.pop("POL_FRIENDS_MOBILE", None)
    check((n_off, p_off) == (n_pc, p_pc),
          "marker off: identical to the PC reply")
    os.environ["POL_FRIENDS_MOBILE_CAP"] = "16"
    try:
        n_16, p_16 = serve(R, request(marker))
    finally:
        os.environ.pop("POL_FRIENDS_MOBILE_CAP", None)
    check(rows(p_16)[0] == 20 and (n_16, p_16) == (n_pc, p_pc),
          "a mobile cap below the list cap never lowers it: still 20 rows",
          f"n={n_16}")
    queued(R)

    push_checks(mid, hid)

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
