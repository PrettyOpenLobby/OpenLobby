"""The lobby must bind a connection to the RIGHT account -- or say so when it can't.

The bug this pins (session binding is per-IP; measured
live 2026-08-23): a lobby frame carries no account id, the bind is "whichever
session's IV validates the header", and two accounts on one machine end up
HOLDING THE SAME IV (the client caches its session cipher across account
switches; the per-IP stamp pool replays the same tokens to every account on the
address). Candidate order then picked the member, and one client's binds
flapped 10x member 15 / 12x member 3 within minutes -- char lists, profiles and
friend lists served from the wrong account, silently.

What this suite asserts, each against the OLD behaviour first:

  1. SHARED IV, candidate order wrong: with POL_LOBBY_BIND_ARBITRATE=0 the
     stale-but-fresher session of the WRONG member wins (the reproduced bug);
     with it on (default), the newest per-IV CLAIM -- who actually
     authenticated with this cipher last -- wins.
  2. IV held ONLY by the wrong member's session (the cached-cipher shape): the
     bind necessarily goes wrong, and the 4:7 active-handle announcement -- the
     one lobby request that names its own subject -- PROVES it. Old behaviour
     (POL_LOBBY_BIND_CORROBORATE=0) logs "not one of ours" and keeps serving
     the wrong account; new behaviour warns, rebinds the thread to the owner,
     teaches the owner's session the cipher, and the NEXT connection binds
     right from frame one.
  3. The free warning: a member flip on one address/socket logs a WARNING: line.
     One account relogging in (new sid, same member) must NOT warn -- the
     single-account machine stays silent.
  4. The per-IV claims survive the JSON round-trip through auth-sessions.json
     (the cross-container path), so the arbitration verdict is the same after
     a reload.

The stamp/IV replay pool itself is deliberately untouched (it is load-bearing;
see the banner in `key_candidates`) -- nothing here deletes a candidate.
"""
import os
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="lobby-bind-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    line = "  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                            "  --  " + detail if detail else "")
    # The lines under test carry WARNING:; a cp1252 console must not crash the suite.
    enc = sys.stdout.encoding or "ascii"
    print(line.encode(enc, "backslashreplace").decode(enc))
    if not ok:
        FAILS.append(label)


# Capture every log line so the warnings can be asserted on.
LOGS = []
_orig_log = R.log


def _tee_log(channel, msg):
    LOGS.append("[%s] %s" % (channel, msg))
    _orig_log(channel, msg)


R.log = _tee_log

# Two real members, as the live incident had: the handles are what 4:7 names.
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
M_STALE = int(accounts.ensure_member(db, "PS2Tester")["id"])   # the wrong one
M_LIVE = int(accounts.ensure_member(db, "Fox")["id"])          # the right one
db.close()

IP = "203.0.113.5"
IV_SHARED = bytes.fromhex("552bd8ab00112233")   # held by BOTH members' sessions
IV_STALE_ONLY = bytes.fromhex("aa55aa55aa55aa55")  # held only by the wrong one


def frame_under(iv):
    """A minimal valid lobby frame: header [0]=0x02, [4:8]=LE payload len 0,
    total 40 bytes -- exactly what `_lobby_header_ok` self-validates."""
    pt = bytes([0x02, 0, 0, 0]) + struct.pack("<I", 0)
    return R._lobby_crypt(pt, iv) + b"\x00" * 32


def fresh_thread():
    """Simulate a brand-new connection thread: nothing bound yet."""
    R._session_current = threading.local()


def reset_state():
    with R._SESSIONS_LOCK:
        R._SESSIONS.clear()
    with R._BIND_NOTE_LOCK:
        R._BIND_LAST.clear()
        R._BIND_PROVEN.clear()
    fresh_thread()
    LOGS.clear()


def seed(sid, member, iv, at=None, claim=None):
    """A session slot the way auth would have left it."""
    R._session_put(sid, iv=iv, member_id=member, peer_ip=IP)
    with R._SESSIONS_LOCK:
        slot = R._SESSIONS[sid]
        if at is not None:
            slot["at"] = at
        if claim is not None:
            slot["iv_claims"][iv.hex()] = claim


def bound_member():
    sid = R._session_sid()
    with R._SESSIONS_LOCK:
        return (R._SESSIONS.get(sid) or {}).get("member_id"), sid


now = time.time()

# --------------------------------------------------------------------------- #
print("1. shared IV, two members: candidate order vs newest per-IV claim")
# The measured shape: BOTH sessions hold the shared cipher. The stale (wrong)
# session looks FRESHER by `at` (its slot kept being touched), but the live
# member authenticated with the cipher more recently (newer claim).
reset_state()
seed("u_stale", M_STALE, IV_SHARED, at=now, claim=now - 600)
seed("u_live", M_LIVE, IV_SHARED, at=now - 30, claim=now - 5)

os.environ["POL_LOBBY_BIND_ARBITRATE"] = "0"
fresh_thread()
iv, sid = R._lobby_bind(frame_under(IV_SHARED), IP, "test:1")
who, _ = bound_member()
check(iv == IV_SHARED and who == M_STALE,
      "OLD (ARBITRATE=0) reproduces the mis-bind: candidate order binds the "
      "WRONG member", f"bound member={who}, wanted the bug's {M_STALE}")

os.environ["POL_LOBBY_BIND_ARBITRATE"] = "1"
fresh_thread()
iv, sid = R._lobby_bind(frame_under(IV_SHARED), IP, "test:2")
who, sid = bound_member()
check(who == M_LIVE and sid == "u_live",
      "NEW: newest per-IV claim wins -- the member who last authenticated "
      "with this cipher", f"bound member={who} sid={sid}")
check(any("AMBIGUOUS BIND" in ln for ln in LOGS),
      "the ambiguity is LOGGED, not silent")

# The flip warning fired too (test:1 bound M_STALE, test:2 bound M_LIVE).
check(any("MEMBER FLIP" in ln for ln in LOGS),
      "member flip on one address logs the WARNING: warning")

# Same-thread continuity: the bound session keeps winning on later frames.
iv2, sid2 = R._lobby_bind(frame_under(IV_SHARED), IP, "test:2")
check(sid2 == "u_live" and R._session_sid() == "u_live",
      "continuity: the bound thread stays with its session")

# --------------------------------------------------------------------------- #
print("2. per-IV claims survive the auth-sessions.json round-trip")
# Simulate the OTHER container: drop memory, reload from the shared file.
with R._SESSIONS_LOCK:
    R._sessions_save_locked()
    R._SESSIONS.clear()
R._SESSIONS_MTIME[0] = 0.0
fresh_thread()
iv, sid = R._lobby_bind(frame_under(IV_SHARED), IP, "test:3")
who, sid = bound_member()
check(who == M_LIVE,
      "after reload, arbitration still picks the right member",
      f"bound member={who} sid={sid}")

# --------------------------------------------------------------------------- #
print("3. cached-cipher shape: IV held ONLY by the wrong session; 4:7 proves it")
reset_state()
seed("u_stale", M_STALE, IV_STALE_ONLY, at=now, claim=now - 600)
seed("u_live", M_LIVE, IV_SHARED, at=now - 30, claim=now - 5)


def announce_47(handle_name):
    """A 4:7 request naming the handle the client is logged in as."""
    pt = bytearray(R._ACTIVE_HANDLE_OFF + 1 + 16)
    pt[R._ACTIVE_HANDLE_OFF + 1:
       R._ACTIVE_HANDLE_OFF + 1 + len(handle_name)] = handle_name.encode()
    R._capture_active_handle(bytes(pt))


# OLD behaviour: the proof is thrown away, the wrong bind stands.
os.environ["POL_LOBBY_BIND_CORROBORATE"] = "0"
fresh_thread()
R._lobby_bind(frame_under(IV_STALE_ONLY), IP, "test:4")
who, _ = bound_member()
check(who == M_STALE, "the cached-cipher frame necessarily binds wrong "
                      "(only the stale session holds the IV)",
      f"bound member={who}")
announce_47("Fox")
who, _ = bound_member()
check(who == M_STALE and any("not one of ours" in ln for ln in LOGS),
      "OLD (CORROBORATE=0): 4:7 proof discarded as 'not one of ours', "
      "wrong bind kept")

# NEW behaviour: warn, rebind, teach the cipher; next connection binds right.
os.environ["POL_LOBBY_BIND_CORROBORATE"] = "1"
LOGS.clear()
fresh_thread()
R._lobby_bind(frame_under(IV_STALE_ONLY), IP, "test:5")
announce_47("Fox")
who, sid = bound_member()
check(who == M_LIVE and sid == "u_live",
      "NEW: 4:7 mismatch rebinds the thread to the handle's owner",
      f"bound member={who} sid={sid}")
check(any("MIS-BOUND SESSION PROVEN" in ln for ln in LOGS),
      "the proof is logged as a WARNING: warning")
with R._SESSIONS_LOCK:
    taught = IV_STALE_ONLY in (R._SESSIONS["u_live"].get("ivs") or [])
check(taught, "the owner's session was taught the connection's cipher")

fresh_thread()
R._lobby_bind(frame_under(IV_STALE_ONLY), IP, "test:6")
who, sid = bound_member()
check(who == M_LIVE,
      "the NEXT connection under the same cipher binds right from frame one",
      f"bound member={who} sid={sid}")

# --------------------------------------------------------------------------- #
print("4. no false positives: one account, relogin cycle, two ciphers")
reset_state()
IV_A, IV_B = bytes(8), bytes.fromhex("0102030405060708")
seed("u_launch1", M_LIVE, IV_A, at=now - 300)
seed("u_launch2", M_LIVE, IV_B, at=now)      # relaunched: new sid, same member
fresh_thread()
R._lobby_bind(frame_under(IV_B), IP, "test:7")
fresh_thread()
R._lobby_bind(frame_under(IV_A), IP, "test:8")   # client re-keyed mid-evening
noisy = [ln for ln in LOGS if "MEMBER FLIP" in ln or "AMBIGUOUS" in ln
         or "MIS-BOUND" in ln]
check(not noisy, "a single-account machine triggers NO binding warnings",
      "; ".join(noisy[:2]))

print()
if FAILS:
    print("FAILED:", len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
