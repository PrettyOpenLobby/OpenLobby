"""The INITIAL PRESENCE BURST: a fresh 2:3 list must not stay grey.

The online icon is painted ONLY by the push -- the 2:3 row carries no presence
the client reads (app.dll 0x0488efc5 tests slot +0x08 bits the record-copy
0x037deeb0 never writes; the SE capture flips no parsed field between online and
offline) -- and `_broadcast_presence` fires only on TRANSITIONS. So without a
burst, a freshly fetched list shows every friend grey until each one next
changes state ("fresh restart, everyone offline", account holder 2026-08-23).

Pinned here, against `push_presence_burst` / `_push_deliver_presencerows`:

  1. GATE: POL_FRIEND_PRESENCE_BURST=0 queues nothing.
  2. DELIVERY, online friend: one field push with +0x11=online(3), zone=Viewer
     (1000), and EXACTLY the (slot, guid) the serve handed over -- the guid is
     the row's own variable, so identity matches the row by construction.
  3. DELIVERY, away friend: +0x11=away(2) -- the burst honours the 4:5 latch.
  4. DELIVERY, offline friend (incl. one who logged out inside the spool
     delay): the OFFLINE assertion `+0x11=1, zone=0` -- SE bursts every friend,
     offline included (measured, auth429364.pkl), and the offline assertion is
     what greys a stale icon.
  5. REGISTRATION RACE: no live watcher session -> the record DEFERS, and the
     kind-dispatching retry delivers it once the session registers (this also
     pins the retry's per-kind dispatch, which used to drop non-"rows" kinds).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="presence-burst-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_PRESENCE_PUSH"] = "1"          # the master gate the burst rides

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


#: Capture every field push instead of encoding it -- the record BYTES are
#: `push_test.py`'s job; this suite pins WHAT the burst decided to send.
CALLS = []
_real_fpl = R.field_push_lines


def _capture_fpl(nick, guid, slot, **kw):
    CALLS.append({"nick": nick, "guid": int(guid), "slot": int(slot), **kw})
    return [b"NOTICE line"]


R.field_push_lines = _capture_fpl


class FakeSession:
    alive = True
    nick = b"WATCHERNICK"

    def __init__(self):
        self.sent = []

    def send(self, lines):
        self.sent.append(list(lines))
        return True


def _primary_handle(db, member_id):
    return int(db.execute(
        "SELECT id FROM handle WHERE member_id = ? "
        "ORDER BY is_primary DESC, id ASC LIMIT 1", (int(member_id),)
    ).fetchone()["id"])


db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
watcher = int(accounts.ensure_member(db, "BURSTWATCHER")["id"])
m_on = int(accounts.ensure_member(db, "FRIENDONLINE")["id"])
h_on = _primary_handle(db, m_on)
m_away = int(accounts.ensure_member(db, "FRIENDAWAY")["id"])
h_away = _primary_handle(db, m_away)
m_off = int(accounts.ensure_member(db, "FRIENDGONE")["id"])
h_off = _primary_handle(db, m_off)
accounts.open_session(db, m_on, nick="FRIENDONLINE")
accounts.open_session(db, m_away, nick="FRIENDAWAY")
# m_off: NO session row -- logged out between the serve and the delivery.
GUID_ON, GUID_AWAY, GUID_OFF = 0x860FB3E2A2, 0x12C47625846, 0xDEAD

# --------------------------------------------------------------------------- #
print("1. gate: POL_FRIEND_PRESENCE_BURST=0 queues nothing")
os.environ["POL_FRIEND_PRESENCE_BURST"] = "0"
n = R.push_presence_burst(db, watcher, [(0, GUID_ON, h_on)])
check(n == 0, "burst off -> push_presence_burst returns 0", f"n={n}")
os.environ["POL_FRIEND_PRESENCE_BURST"] = "1"

# --------------------------------------------------------------------------- #
print("2. delivery: online friend -> +0x11=online, zone=Viewer, served identity")
sess = FakeSession()
R.PRESENCE.register(watcher, sess)
CALLS.clear()
sent = R._push_deliver_presencerows(
    {"kind": "presencerows", "member": watcher,
     "rows": [[1, GUID_ON, h_on]], "after": 0}, db)
check(sent == 1, "delivered to the watcher's session", f"sent={sent}")
check(len(CALLS) == 1, "exactly one field push for one online friend",
      f"calls={len(CALLS)}")
if CALLS:
    c = CALLS[0]
    check(c["guid"] == GUID_ON and c["slot"] == 1,
          "push carries the SERVED (slot, guid) unchanged",
          f"slot={c['slot']} guid=0x{c['guid']:X}")
    check(c.get("state") == R._PRESENCE_FIELD_STATE["online"],
          "+0x11 = online (3)", f"state={c.get('state')}")
    check(c.get("zone") == R._PRESENCE_ZONE_VIEWER,
          "zone = the Viewer (1000) for a friend in no title/room",
          f"zone={c.get('zone')}")

# --------------------------------------------------------------------------- #
print("3. delivery: away friend -> +0x11=away (the 4:5 latch is honoured)")
R._publish_member_status(m_away, R._STATUS_AWAY)
CALLS.clear()
R._push_deliver_presencerows(
    {"kind": "presencerows", "member": watcher,
     "rows": [[2, GUID_AWAY, h_away]], "after": 0}, db)
check(len(CALLS) == 1 and CALLS[0].get("state") ==
      R._PRESENCE_FIELD_STATE["away"],
      "+0x11 = away (2) for a latched-away friend",
      f"calls={CALLS!r}")

# --------------------------------------------------------------------------- #
print("4. delivery: offline friend -> the OFFLINE assertion (+0x11=1, zone=0)")
CALLS.clear()
sent = R._push_deliver_presencerows(
    {"kind": "presencerows", "member": watcher,
     "rows": [[3, GUID_OFF, h_off]], "after": 0}, db)
check(sent == 1 and len(CALLS) == 1,
      "an offline friend is asserted, not skipped",
      f"sent={sent} calls={len(CALLS)}")
if CALLS:
    c = CALLS[0]
    check(c.get("state") == R._PRESENCE_FIELD_STATE["offline"]
          and c.get("zone") == 0,
          "offline assertion is +0x11=1, zone=0 (SE's 01 01 00 00 00 00)",
          f"state={c.get('state')} zone={c.get('zone')}")

# --------------------------------------------------------------------------- #
print("5. registration race: defer, then the KIND-dispatching retry delivers")
R.PRESENCE.unregister(watcher, sess)
CALLS.clear()
rec = {"kind": "presencerows", "member": watcher,
       "rows": [[1, GUID_ON, h_on]], "after": 0}
sent = R._push_deliver_presencerows(rec, db)
check(sent == 0 and rec in R._PUSH_DEFER,
      "no live session -> the record is DEFERRED, not dropped",
      f"sent={sent} deferred={rec in R._PUSH_DEFER}")
R.PRESENCE.register(watcher, sess)
R._push_defer_retry()
check(len(CALLS) == 1 and CALLS[0]["guid"] == GUID_ON,
      "the retry routed the burst back to its own deliverer and it landed",
      f"calls={CALLS!r}")

R.field_push_lines = _real_fpl
db.close()

print()
if FAILS:
    print("FAILED:", len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
