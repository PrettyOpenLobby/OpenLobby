"""ACCEPT-TIME PRESENCE: a friendship formed mid-session must not stay grey.

The online icon is painted ONLY by a presence push (`push_presence_burst`'s
docstring has the RE), and only two things send one: `_broadcast_presence` on a
TRANSITION, and the initial burst on a 2:3 FETCH. An accept is neither -- so
until `_friend_addrow_presence` both new rows sat grey.

MEASURED LIVE 2026-08-25 on prod, which is the case pinned here: `Fox` added
`clem` at 02:19:52Z, `clem`'s client accepted at 02:20:16Z and both rows went
active. `Fox`'s last 2:3 was 02:19:21Z -- BEFORE the row existed -- so the burst
could not carry it, and the next line naming `clem` in the entire log is a zone
change three minutes later. Reported the same night: "after someone accepted a
friend request, they did not update in my friends list as being online".

  1. BOTH DIRECTIONS on the accept: the accepter's own list AND the asker's,
     because the asker's client sends nothing at all (`POL_FRIEND_ACCEPT_BOTH`
     flips their row server-side).
  2. THE SLOT IS THE SERVED ONE. A presence record whose slot disagrees with
     the client's row is dropped SILENTLY -- indistinguishable from the bug --
     so the slot map we published wins over any re-derivation.
  3. THE GUID IS THE ROW GUID (`_push_identity_guid`), same coupling that
     `_broadcast_presence` needs and that broke presence on 2026-08-22.
  4. ONLY THE TRANSITION. A 2:6 that re-names an existing friend (a rename and
     a delete are both whole-list writes) asserts nothing -- otherwise every
     list write puts a push on the wire for every row.
  5. A PENDING row asserts nothing: it is not a friendship yet.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="accept-presence-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_PRESENCE_PUSH"] = "1"          # the master gate the burst rides
os.environ["POL_FRIEND_PRESENCE_BURST"] = "1"

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    line = "  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                            "  --  " + detail if detail else "")
    enc = sys.stdout.encoding or "ascii"
    print(line.encode(enc, "backslashreplace").decode(enc))
    if not ok:
        FAILS.append(label)


#: The spool is authserv's business; this suite pins WHAT the lobby decided to
#: queue, so intercept the emit rather than round-tripping a file.
EMITS = []
R._push_emit = lambda rec, db=None: (EMITS.append(rec), 1)[1]


def handle_row(db, member_id):
    return db.execute(
        "SELECT id, handle_name FROM handle WHERE member_id = ?"
        " ORDER BY is_primary DESC, id ASC LIMIT 1", (int(member_id),)).fetchone()


def friend_row_id(db, handle_id, peer_name):
    return int(db.execute(
        "SELECT id FROM friend WHERE handle_id = ? AND peer_name = ?",
        (int(handle_id), peer_name)).fetchone()["id"])


def publish(handle_id, slots):
    """Stand in for a 2:3 having been served with this numbering."""
    with R._FRIEND_SLOTS_LOCK:
        R._FRIEND_SLOTS[int(handle_id)] = {"at": time.time(), "slots": dict(slots)}


def emit_for(member_id):
    return [e for e in EMITS if e.get("kind") == "presencerows"
            and int(e.get("member", 0)) == int(member_id)]


db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
m_ask = int(accounts.ensure_member(db, "ASKERCAS")["id"])          # "Fox"
m_acc = int(accounts.ensure_member(db, "ACCEPTCLEM")["id"])        # "clem"
h_ask, h_acc = handle_row(db, m_ask), handle_row(db, m_acc)
accounts.open_session(db, m_ask, nick="ASKER")
accounts.open_session(db, m_acc, nick="ACCEPTER")

# The asker adds them: their row is PENDING, the mirror leaves an INVITED row on
# the accepter -- exactly the 02:19:52Z state.
out = accounts.request_friend(db, int(h_ask["id"]), h_acc["handle_name"])
check(out == "requested", "the add is a request, and it mirrored",
      f"request_friend -> {out!r}")

# The numbering each side was last served. The asker's list already had five
# friends when they added this one, so their client filed it at slot 5 and our
# 2:6 reply confirmed that -- the live case.
publish(int(h_ask["id"]),
        {5: (h_acc["handle_name"], friend_row_id(db, int(h_ask["id"]),
                                                 h_acc["handle_name"]))})
publish(int(h_acc["id"]),
        {0: (h_ask["handle_name"], friend_row_id(db, int(h_acc["id"]),
                                                 h_ask["handle_name"]))})

# --------------------------------------------------------------------------- #
print("5. a PENDING row asserts nothing -- it is not a friendship yet")
EMITS.clear()
R._friend_addrow_presence(db, m_acc, h_acc, [{"name": h_ask["handle_name"]}],
                          {h_ask["handle_name"]: accounts.STATUS_INVITED})
check(not EMITS, "nothing queued while the row is still invited",
      f"emits={EMITS!r}")

# --------------------------------------------------------------------------- #
print("1. the accept -> BOTH directions get a presence assertion")
before = {r["peer_name"]: r["status"]
          for r in accounts.list_friends(db, int(h_acc["id"]), status=None)}
out = accounts.request_friend(db, int(h_acc["id"]), h_ask["handle_name"])
check(out == "accepted", "naming the asker back is an acceptance",
      f"request_friend -> {out!r}")
EMITS.clear()
del R._FRIEND_PUT_ASSIGNED[:]
R._friend_addrow_presence(db, m_acc, h_acc, [{"name": h_ask["handle_name"]}],
                          before)
mine, theirs = emit_for(m_acc), emit_for(m_ask)
check(len(mine) == 1, "the ACCEPTER's own list is asserted", f"{mine!r}")
check(len(theirs) == 1,
      "the ASKER is asserted too -- their client sends nothing at all",
      f"{theirs!r}")

# --------------------------------------------------------------------------- #
print("2. the slot is the SERVED one, not a re-derivation")
if theirs:
    rows = theirs[0]["rows"]
    check(len(rows) == 1 and int(rows[0][0]) == 5,
          "the asker is told slot 5 -- the numbering their 2:3 handed out",
          f"rows={rows!r}")
if mine:
    rows = mine[0]["rows"]
    check(len(rows) == 1 and int(rows[0][0]) == 0,
          "the accepter is told slot 0 -- theirs", f"rows={rows!r}")

# --------------------------------------------------------------------------- #
print("3. the guid is the ROW guid (`_push_identity_guid`), or it is dropped")
if theirs and mine:
    check(int(theirs[0]["rows"][0][1]) == R._push_identity_guid(db, int(h_acc["id"]))
          and int(theirs[0]["rows"][0][2]) == int(h_acc["id"]),
          "the asker's record names the ACCEPTER, by the asker's row guid",
          f"row={theirs[0]['rows'][0]!r}")
    check(int(mine[0]["rows"][0][1]) == R._push_identity_guid(db, int(h_ask["id"]))
          and int(mine[0]["rows"][0][2]) == int(h_ask["id"]),
          "the accepter's record names the ASKER, by the accepter's row guid",
          f"row={mine[0]['rows'][0]!r}")

# --------------------------------------------------------------------------- #
print("4. ONLY the transition -- a later whole-list write asserts nothing")
EMITS.clear()
R._friend_addrow_presence(db, m_acc, h_acc, [{"name": h_ask["handle_name"]}],
                          {h_ask["handle_name"]: accounts.STATUS_ACTIVE})
check(not EMITS, "a rename/delete write over a settled friendship is silent",
      f"emits={EMITS!r}")

# --------------------------------------------------------------------------- #
print("6. an unserved peer is skipped, not guessed at")
publish(int(h_ask["id"]), {})          # we never served the asker this row
EMITS.clear()
R._friend_addrow_presence(db, m_acc, h_acc, [{"name": h_ask["handle_name"]}],
                          {h_ask["handle_name"]: accounts.STATUS_INVITED})
check(not emit_for(m_ask),
      "no slot known for us on their list -> nothing invented; heals at relog",
      f"emits={EMITS!r}")

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
