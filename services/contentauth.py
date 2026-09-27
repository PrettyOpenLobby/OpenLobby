"""The per-login CONTENT AUTH value: which POL member launched this title.

A game service that cannot tell who a connection belongs to falls back to the
peer ADDRESS, i.e. the freshest POL sign-in from that IP. Two devices behind
one home router share that address, so one player's device can be served the
other player's character, and a result earned on one can be paid onto the
other's record.

The design this follows was read from Project Crystal Server
(github.com/Project-Crystal-Server/Project-Crystal-Server, AGPL-3.0,
POLProfile/RequestHandler.cs UpdateStatus + FMOServer/Database.cs) and
reimplemented here:

  * When the client enters a title it sends the lobby status update 4:5
    (Crystal's request 0x405). The server answers with a fresh 16-byte RANDOM
    VALUE in the first 16 bytes of the reply and records it against that POL
    login.
  * polcore keeps it for the rest of the POL session and hands it to the title.
    Front Mission Online's TCP cipher key is exactly those 16 bytes + the
    4-byte tail of our own 0x0322 reply (Crystal: `polRandomValueBinary +
    "IJKL"`). With a 4:5 reply that echoes the request's zero bytes, the key
    begins with 16 zero bytes.

So: mint a real value per login (responders.py, lobby side), publish it here,
and let the title recover the member from the KEY THE CLIENT ACTUALLY USES by
trial-decrypting its first encrypted packet under each candidate. A wrong
candidate fails the client's own checksum, so the match does not depend on the
address. Relaunching a title inside one POL session sends no new 4:5, which is
why a value lives for the whole POL session.

SCOPE. Only the zones in POL_CONTENT_AUTH_ZONES (default "4" = FMO) get a
random value. Any other title keeps the exact bytes it has always had, because
it is not known whether the other titles derive a key from these 16 bytes.

The file crosses containers (the lobby writes, the title's container reads the
shared /data), so it is one writer, an atomic replace, and an mtime-cached
read.
"""

import json
import os
import threading
import time

FILE = os.environ.get(
    "POL_CONTENT_AUTH_FILE",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "content-auth.json"))

#: Content ids (the 4:5 zone) that receive a minted value. Empty = feature off.
ZONES = frozenset(int(z) for z in
                  os.environ.get("POL_CONTENT_AUTH_ZONES", "4").split(",")
                  if z.strip())

#: How long a value stays a candidate. A POL session can run all evening and
#: polcore reuses the value on every relaunch inside it.
TTL = int(os.environ.get("POL_CONTENT_AUTH_TTL", str(24 * 3600)))

#: Hard cap on rows kept, newest first, so a busy night cannot grow the file
#: (and the trial list) without bound.
MAX_ROWS = int(os.environ.get("POL_CONTENT_AUTH_MAX", "256"))

LEN = 16

#: WHO GETS A MINTED VALUE -- the rollout gate, read on every 4:5 so it changes
#: with NO restart. One line: `*` = every member, or member ids `3,11`. Missing
#: or empty = nobody, i.e. the 4:5 reply is byte-for-byte what it always was.
#: Why a gate at all: only the PC client has been measured. A client whose
#: polcore builds its key from this value some other way (the PS2 is unmeasured)
#: would match no candidate and lose the title, so it is switched on per member.
#: POL_CONTENT_AUTH_MEMBERS, when set, overrides the file.
ENABLE_FILE = os.environ.get(
    "POL_CONTENT_AUTH_ENABLE_FILE",
    os.path.join(os.path.dirname(FILE) or ".", "content-auth-members.txt"))

_LOCK = threading.Lock()
_CACHE = {"mtime": -1.0, "rows": []}
_ENABLE_CACHE = {"mtime": None, "spec": ""}


def _parse_members(spec):
    """'*' -> '*'; '3, 11' -> {3, 11}; '' -> empty set. Junk ids are ignored."""
    spec = (spec or "").strip()
    if spec == "*":
        return "*"
    return {int(x) for x in spec.replace(" ", "").split(",") if x.isdigit()}


def enabled_for(member_id):
    """Is minting switched on for this member right now?"""
    if member_id is None:
        return False
    spec = os.environ.get("POL_CONTENT_AUTH_MEMBERS")
    if spec is None:
        try:
            mtime = os.stat(ENABLE_FILE).st_mtime
        except OSError:
            return False
        if mtime != _ENABLE_CACHE["mtime"]:
            try:
                with open(ENABLE_FILE, "r", encoding="utf-8") as f:
                    _ENABLE_CACHE["spec"] = f.read()
                _ENABLE_CACHE["mtime"] = mtime
            except OSError:
                return False
        spec = _ENABLE_CACHE["spec"]
    who = _parse_members(spec)
    return who == "*" or int(member_id) in who


def _load():
    try:
        mtime = os.stat(FILE).st_mtime
    except OSError:
        return []
    if mtime != _CACHE["mtime"]:
        try:
            with open(FILE, "r", encoding="utf-8") as f:
                rows = json.load(f) or []
            _CACHE["rows"] = rows if isinstance(rows, list) else []
            _CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _CACHE["rows"]              # torn write: last good view stands
    return _CACHE["rows"]


def mint():
    """A fresh value. Never all-zero: zero is the legacy key and must stay
    distinguishable from a minted one."""
    while True:
        v = os.urandom(LEN)
        if any(v):
            return v


def publish(member_id, peer_ip, zone, value, now=None):
    """Record that `value` was handed to `member_id` for title `zone`.
    Returns True when written. Lobby side only."""
    if member_id is None or len(value) != LEN:
        return False
    now = time.time() if now is None else now
    row = {"v": value.hex(), "member_id": int(member_id),
           "peer_ip": peer_ip or "", "zone": int(zone), "at": now}
    with _LOCK:
        rows = [r for r in _load()
                if now - float(r.get("at") or 0) < TTL and r.get("v") != row["v"]]
        rows.insert(0, row)
        del rows[MAX_ROWS:]
        os.makedirs(os.path.dirname(FILE) or ".", exist_ok=True)
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, FILE)                  # atomic
        _CACHE["mtime"] = -1.0                 # our own next read re-stats
    return True


def candidates(peer_ip=None, zone=None, now=None):
    """[(value bytes, member_id, row)] to trial, best first: rows from this
    address newest first, then every other live row newest first. The address
    only ORDERS the trial -- it never decides; the key check does."""
    now = time.time() if now is None else now
    live = [r for r in _load()
            if now - float(r.get("at") or 0) < TTL
            and (zone is None or int(r.get("zone", -1)) == zone)]
    live.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
    same = [r for r in live if peer_ip and r.get("peer_ip") == peer_ip]
    rest = [r for r in live if r not in same]
    out = []
    for r in same + rest:
        try:
            v = bytes.fromhex(r["v"])
        except (KeyError, ValueError):
            continue
        if len(v) == LEN:
            out.append((v, int(r["member_id"]), r))
    return out


def selftest():
    """Round-trip through a temp file, with twins that must FAIL."""
    import tempfile
    global FILE
    ok = True
    saved = FILE
    with tempfile.TemporaryDirectory() as d:
        FILE = os.path.join(d, "ca.json")
        _CACHE.update(mtime=-1.0, rows=[])
        t0 = 1_000_000.0
        a, b, old = mint(), mint(), mint()
        publish(3, "1.2.3.4", 4, a, now=t0)
        publish(11, "1.2.3.4", 4, b, now=t0 + 5)
        publish(7, "9.9.9.9", 4, old, now=t0 - TTL - 1)
        c = candidates("1.2.3.4", zone=4, now=t0 + 10)
        got = [(m, v) for v, m, _ in c]
        want = [(11, b), (3, a)]
        print(f"  newest-first, same address, expired dropped: "
              f"{'OK' if got == want else 'FAIL ' + repr(got)}")
        ok &= got == want
        # Twin: the address orders but must not exclude.
        c2 = [m for _, m, _ in candidates("5.5.5.5", zone=4, now=t0 + 10)]
        print(f"  other address still sees every live row: "
              f"{'OK' if sorted(c2) == [3, 11] else 'FAIL ' + repr(c2)}")
        ok &= sorted(c2) == [3, 11]
        # Twin: wrong zone must find nothing.
        c3 = candidates("1.2.3.4", zone=2, now=t0 + 10)
        print(f"  another title's zone gets no candidates: "
              f"{'OK' if not c3 else 'FAIL'}")
        ok &= not c3
        # Twin: no member -> nothing written.
        wrote = publish(None, "1.2.3.4", 4, mint(), now=t0)
        print(f"  unknown member is not published: {'OK' if not wrote else 'FAIL'}")
        ok &= not wrote
        print(f"  mint is never zero: {'OK' if all(any(mint()) for _ in range(64)) else 'FAIL'}")
        # The rollout gate, through the FILE (env unset), and its twins.
        global ENABLE_FILE
        saved_ef, saved_env = ENABLE_FILE, os.environ.pop("POL_CONTENT_AUTH_MEMBERS", None)
        ENABLE_FILE = os.path.join(d, "members.txt")
        _ENABLE_CACHE.update(mtime=None, spec="")
        g0 = enabled_for(3)
        with open(ENABLE_FILE, "w") as f:
            f.write("3, 11\n")
        g1 = enabled_for(3) and enabled_for(11) and not enabled_for(12)
        os.utime(ENABLE_FILE, (t0, t0 + 1))     # force a new mtime
        with open(ENABLE_FILE, "w") as f:
            f.write("*")
        os.utime(ENABLE_FILE, (t0, t0 + 2))
        g2 = enabled_for(12)
        print(f"  gate: no file = nobody: {'OK' if not g0 else 'FAIL'}; "
              f"'3, 11' = exactly those: {'OK' if g1 else 'FAIL'}; "
              f"'*' = everyone, re-read without restart: {'OK' if g2 else 'FAIL'}")
        ok &= (not g0) and g1 and g2
        ENABLE_FILE = saved_ef
        if saved_env is not None:
            os.environ["POL_CONTENT_AUTH_MEMBERS"] = saved_env
        _ENABLE_CACHE.update(mtime=None, spec="")
    FILE = saved
    _CACHE.update(mtime=-1.0, rows=[])
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
