"""A channel-churn dip must NOT flap a playing member offline -- a real logout must.

The flap this pins (measured live 2026-08-23
22:52Z): the client cycles its session channel every few minutes, each cycle
has a dip of seconds with ZERO live channels, and the old code ran the full
logout wipe (record_logout + close_sessions + offline push) at every
last-channel close. Actively playing members flipped offline every 10-30s;
because the friend list is a login-time snapshot, any 2:3 fetched during a dip
showed that friend offline until the next relaunch -- "everyone appears
offline" was the fetch-timing lottery.

Asserted here, against `responders._logout_or_grace` itself:

  1. CHURN: last channel closes, the member re-dials within the grace ->
     session rows SURVIVE (friends keep reading online), the wipe is
     SUPPRESSED, and the suppression is logged (the flap stays visible).
  2. REAL LOGOUT: last channel closes and nobody comes back -> the wipe runs
     after the grace: rows gone, offline pushed, logout stamped.
  3. REVERT: POL_PRESENCE_LOGOUT_GRACE=0 restores the immediate wipe
     (the pre-grace behaviour), synchronously.

The grace is timer-driven, so cases 1-2 really wait it out (1s in this suite).
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="presence-grace-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP

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


LOGS = []
_orig_log = R.log


def _tee(channel, msg):
    LOGS.append("[%s] %s" % (channel, msg))
    _orig_log(channel, msg)


R.log = _tee

PUSHED = []
_orig_push = R._broadcast_presence
R._broadcast_presence = lambda mid, state, *a, **k: PUSHED.append((int(mid),
                                                                   state))


class FakeChannel:
    alive = True


def seed_member():
    db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    row = accounts.ensure_member(db, "GraceTester")
    mid = int(row["id"])
    accounts.open_session(db, mid, nick="GraceTester")
    online = accounts.member_online(db, mid)
    db.close()
    return mid, online


def is_online(mid):
    db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    try:
        return accounts.member_online(db, mid)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
print("1. churn dip: the member re-dials within the grace -> wipe SUPPRESSED")
os.environ["POL_PRESENCE_LOGOUT_GRACE"] = "1"
mid, online = seed_member()
check(online, "fixture: member has a live session row (reads online)")

R._logout_or_grace(mid, "GraceTester", "test:1", None)
check(is_online(mid), "no wipe at close time -- rows survive the dip")
# The re-dial: a live channel registers before the timer fires.
chan = FakeChannel()
R.PRESENCE.register(mid, chan)
time.sleep(1.6)
check(is_online(mid),
      "rows STILL live after the grace -- friends keep reading online")
check(any("SUPPRESSED" in ln for ln in LOGS),
      "the suppressed wipe is logged (the flap stays visible)")
check(not any(s == "offline" for m, s in PUSHED if m == mid),
      "no offline push reached the watchers")

# --------------------------------------------------------------------------- #
print("2. real logout: nobody comes back -> the wipe runs after the grace")
LOGS.clear()
PUSHED.clear()
R.PRESENCE.unregister(mid, chan)
R._logout_or_grace(mid, "GraceTester", "test:2", None)
check(is_online(mid), "still online during the grace window")
time.sleep(1.6)
check(not is_online(mid), "offline after the grace -- session rows closed")
check((mid, "offline") in PUSHED, "offline pushed to the watchers")
check(any("logout stamped" in ln for ln in LOGS), "the logout was stamped")

# --------------------------------------------------------------------------- #
print("3. POL_PRESENCE_LOGOUT_GRACE=0 restores the immediate wipe")
LOGS.clear()
PUSHED.clear()
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
accounts.open_session(db, mid, nick="GraceTester")
db.close()
os.environ["POL_PRESENCE_LOGOUT_GRACE"] = "0"
R._logout_or_grace(mid, "GraceTester", "test:3", None)
check(not is_online(mid), "wipe ran synchronously with the knob at 0")
check((mid, "offline") in PUSHED, "offline pushed immediately")

print()
if FAILS:
    print("FAILED:", len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
