"""Per-connection lobby session state, its shared mirror, and the session record."""
import datetime
import hashlib
import json
import os
import secrets
import struct
import time
import threading
from polcore import kv
from srvcore import log
from .deps import accounts
from . import friendlist, handlelists, profilerecord



# --------------------------------------------------------------------------- #
# per-client session state
# --------------------------------------------------------------------------- #
#
# What this holds: the session IV recovered from the auth NICK line, and the
# member that NICK resolved to. Both are per Viewer LAUNCH -- both auth
# connections of one launch recover the same IV, and that launch's lobby
# messages decrypt with it.
#
# These used to be `_SESSION_IV = [None]` and `_SESSION_MEMBER = [None]`: ONE
# SLOT EACH, shared by every connection. The server is threaded per connection,
# so a second client authenticating overwrote the first client's values, and the
# lobby then decrypted with the wrong IV and served the wrong account's handle,
# profile and friend list. The server was structurally single-client.
#
# (The single slot was itself a fix for an earlier bug: before `_SESSION_MEMBER`
# existed every record builder did `SELECT ... FROM member ORDER BY id LIMIT 1`
# and always served member 1. That is why the state exists at all.)
#
# JOIN KEY -- NOT the peer address. It was, until 2026-08-13, and that was the
# single worst bug in this file: `_SESSIONS` was keyed on the client's IP, and
# under the dev compose every client arrives through the Docker bridge, so every
# client IS one address. Measured on our own log: 5,328 auth connections, one
# source address (172.18.0.1). Two players did not merely risk collision -- they
# were guaranteed to share one slot, and the second was served the first's
# handle, profile, friend list and mail badge with nothing logged.
#
# What replaces it, and why these two things:
#
#  * THE AUTH BAND identifies itself. The client sends `USER x 8 * :<48 chars>`
#    on every hop of a launch, and that token is per LAUNCH: measured across the
#    log, one token appears on each hop of a login (seconds apart) and never
#    again. It is client-generated, high entropy, and already on the wire before
#    we answer -- so it is the session id, and no device fingerprinting is
#    needed. `_sid_for_user_token` derives a short stable id from it.
#
#  * THE LOBBY BAND proves itself cryptographically. A lobby frame decrypts to a
#    self-validating header (type 0x02, and 40 + payload_len == frame length)
#    ONLY under the OFB IV of the auth session that owns it -- see
#    `_lobby_header_ok`, which already made IV selection a measurement rather
#    than a guess. So the lobby connection is joined to its session by trying
#    candidate IVs and keeping the one that validates. An address is not
#    involved anywhere in that decision.
#
# Peer address survives only as a HINT: it orders the candidate list so the
# common case costs one decrypt, and it is what `key_candidates` (which runs
# before any USER line has arrived) still looks tokens up by. It never decides
# who a session belongs to.
_SESSIONS_LOCK = threading.Lock()
_SESSIONS = {}                  # sid -> {"iv":..., "member_id":..., "peer_ip":...}
#: HOW LONG A SESSION STAYS IDENTIFIABLE. This was 3600 with the note "a launch
#: is long-lived but not forever" -- the SAME wrong premise `_STAMP_TTL` was
#: raised off (see its note above): a Viewer stays open for an entire evening,
#: and NOTHING refreshes `at` while it sits on a menu. `_session_put` is called
#: on auth events, not on the keepalive, so a client that has been up an hour
#: doing nothing but PONG ages out of `_SESSIONS` and out of the shared store --
#: while its socket is still open and its IV is still the one it encrypts with.
#: The lobby then finds no candidate IV that validates the frame, logs "no
#: session claims this frame", and answers 36 bytes of nothing. The read never
#: completes and the client blames the network.
#:
#: Measured 2026-08-17 on Tetra Master's zone list: three failures (01:13, 01:22,
#: 01:46) on a session 1.5-2.5h old, each one `no session claims this frame` ->
#: `TRM-8196-37130` "error occurred while connecting to the server"; one success
#: (02:12) on a session minutes old, which then walked ZL -> RL000 -> PTL. Same
#: symptom on a second machine, because nothing about it is machine-specific.
#:
#: 12 hours matches `_STAMP_TTL` and covers a play session. The keepalive now
#: touches the session too (see the PING tick in the authserv loop), so an
#: ACTIVE connection never ages out at all and this bound only governs sessions
#: whose client is genuinely gone.
_SESSION_TTL = 12 * 3600

#: Which SESSION the current thread is serving. Set by each connection handler
#: via session_bind(); read by the lookups below so their call sites keep working
#: without threading a session argument through every record builder.
_session_current = threading.local()


def _sid_for_user_token(token, peer_ip=""):
    """A short, stable session id for one launch, from its USER token.

    The token itself is 48 bytes of client-chosen text that ends up in log lines
    and in state shared with another container, so it is hashed rather than
    used raw: same launch -> same id, and nothing about the client's token has to
    round-trip through a key-safe encoding.
    """
    if isinstance(token, str):
        token = token.encode("latin-1", "replace")
    return "u" + hashlib.sha1(token).hexdigest()[:16]


def _sid_for_connection(peer_ip, port=0):
    """The fallback session id: one per CONNECTION, not one per address.

    Used when a hop has no USER token to key on (a redirect hop that dies early,
    a lobby connection that never validates). Deliberately unique per call --
    two clients that share an address must never share a fallback slot, which is
    exactly the bug this whole section replaced. `peer_ip` is kept in the id only
    to make the logs readable.
    """
    return f"c{peer_ip}:{port}:{secrets.token_hex(4)}"


def session_bind(sid):
    """Declare which session this thread serves. Call once per connection."""
    _session_current.sid = sid


def _session_sid():
    """The session id this thread is bound to, or None."""
    return getattr(_session_current, "sid", None)


#: ...and in the live-state store, for the SAME reason _STAMPS is (see
#: authtoken), but for a different consumer.
#:
#: `authserv` is the only thing that FILLS this table -- it is where a client's
#: IV and key are recovered. The LOBBY only reads it (_lobby_iv_candidates), and
#: the lobby is what needs the IV to decrypt a message and to answer under the
#: same key. In one process that is free. Run authserv in its own container (so
#: restarting `login` for a lobby edit stops killing live sessions) and the
#: lobby's copy of this dict is empty: it falls through to the XOR-derived
#: fallback, the client gets an unreadable reply, and POL-0008 fires the instant
#: it enters the lobby. That is not hypothetical -- it is exactly what happened
#: on 2026-08-13, and it is why the split was reverted.
#:
#: One key per session, `authsess:s:<sid>` = the slot as JSON, expiring
#: _SESSION_TTL after the slot's own `at`, and a counter `authsess:ver` bumped
#: on every write so a reader re-reads only when something moved (what the
#: file's mtime used to tell it). MERGE rather than replace, as before. The
#: contents are session crypto for a live connection, not durable secrets.
_SESSION_KEY = "authsess:s:"
_SESSION_VER_KEY = "authsess:ver"
#: The `authsess:ver` this process last folded in.
_SESSIONS_VER = [None]
#: sid -> the JSON last written or read for it, so a save sends only slots
#: that changed.
_SESSIONS_WRITTEN = {}


def _sess_enc(v):
    """bytes -> a JSON-safe tagged form; everything else passes through."""
    if isinstance(v, (bytes, bytearray)):
        return {"__b": bytes(v).hex()}
    if isinstance(v, (list, tuple)):
        return [_sess_enc(x) for x in v]
    return v


def _sess_dec(v):
    if isinstance(v, dict) and "__b" in v:
        return bytes.fromhex(v["__b"])
    if isinstance(v, list):
        return [_sess_dec(x) for x in v]
    return v


def _session_is_empty(slot):
    """True for a slot that carries no session material worth sharing.

    A connection binds a provisional session before the client has said anything,
    so most slots in a lobby/world process are placeholders. Writing those to the
    shared store is pure noise -- and, before the merge below existed, actively
    harmful.
    """
    return not (slot.get("iv") or slot.get("ivs") or slot.get("key")
                or slot.get("member_id") or slot.get("handle_id"))


def _slot_json(slot):
    return json.dumps({k: _sess_enc(v) for k, v in slot.items()},
                      sort_keys=True, separators=(",", ":"))


def _sessions_load_remote():
    """{sid: (decoded slot, its JSON)} for every session in the store."""
    out = {}
    for name in kv.keys(_SESSION_KEY + "*"):
        raw = kv.get(name)
        if raw is None:
            continue                          # expired between the scan and now
        try:
            slot = {k: _sess_dec(v) for k, v in json.loads(raw).items()}
        except (ValueError, AttributeError):
            continue
        out[name[len(_SESSION_KEY):]] = (slot, raw)
    return out


def _sessions_stored():
    """{sid: slot as stored (JSON-decoded, bytes still tagged)} -- what the other
    process sees. For tests and operators."""
    return {sid: json.loads(raw)
            for sid, (_slot, raw) in _sessions_load_remote().items()}


def _sessions_save_locked():
    """Publish `_SESSIONS`, MERGED with what the other process has published.

    Caller holds _SESSIONS_LOCK.

    THE MERGE IS THE POINT. This is how the `authsess` container hands a
    recovered IV and member to the `login` container (they are separate
    processes; see _SESSION_KEY). If the last writer's view of the world were
    the only one that survived -- and on 2026-08-13 that is exactly what
    happened with the old shared file: the lobby handler started binding a
    session per connection, so `login` wrote its own placeholder-only table over
    the top of `authsess`'s real sessions, the lobby then had no IV for anyone,
    every reply fell back to the XOR path, and the PS2 Viewer sat waiting until
    it reported POL-0010 ("disconnected from the server").

    So: sessions are stored one key each, a session we do not have is adopted
    rather than overwritten, and empty placeholder slots are not written at all.
    """
    try:
        now = time.time()
        ver = kv.get(_SESSION_VER_KEY)
        if ver is None and _SESSIONS_VER[0] is not None:
            _SESSIONS_WRITTEN.clear()         # the store was emptied: resend all
        if ver != _SESSIONS_VER[0]:
            # ADOPT IN BOTH DIRECTIONS. Take the other process's sessions into
            # memory too, so one we had not seen is not lost from our view.
            for sid, (slot, raw) in _sessions_load_remote().items():
                if sid not in _SESSIONS:
                    _SESSIONS[sid] = slot
                    _SESSIONS_WRITTEN[sid] = raw
        for sid in [k for k in _SESSIONS_WRITTEN if k not in _SESSIONS]:
            del _SESSIONS_WRITTEN[sid]
        wrote = 0
        for sid, slot in _SESSIONS.items():
            if _session_is_empty(slot):
                continue
            try:
                enc = _slot_json(slot)
                left = _SESSION_TTL - (now - float(slot.get("at") or 0))
            except Exception:
                continue        # never let one odd field lose the whole table
            if left <= 0 or _SESSIONS_WRITTEN.get(sid) == enc:
                continue
            kv.set(_SESSION_KEY + sid, enc, ttl=left)
            _SESSIONS_WRITTEN[sid] = enc
            wrote += 1
        if wrote:
            new = kv.incr(_SESSION_VER_KEY)
            # Our own write is the newest state; do not read it back in -- unless
            # somebody else wrote between our read of the counter and now.
            if ver == _SESSIONS_VER[0] and new == int(ver or 0) + 1:
                _SESSIONS_VER[0] = str(new)
    except Exception as exc:
        log("authserv", f"could not publish session state: {exc!r}")


def _sessions_refresh():
    """Fold in session state recorded by ANOTHER process (see _SESSION_KEY).

    Cheap: one read of `authsess:ver` when nothing has moved. Merge rules, per
    SESSION --
      * unknown session         -> adopt it
      * their slot is newer     -> adopt their fields
      * either way              -> UNION the IV list, newest first
    The union matters even when ours is newer: the two processes can each have
    seen a different IV for one launch (its two auth hops), and the lobby
    identifies the right one by self-validating header, so an extra candidate is
    free while a missing one is fatal.
    """
    try:
        ver = kv.get(_SESSION_VER_KEY)
        if ver is None or ver == _SESSIONS_VER[0]:
            return
        remote = _sessions_load_remote()
    except Exception as exc:
        log("authserv", f"could not read session state: {exc!r}")
        return
    now = time.time()
    with _SESSIONS_LOCK:
        _SESSIONS_VER[0] = ver
        for sid, (slot, _raw) in remote.items():
            if now - float(slot.get("at") or 0) > _SESSION_TTL:
                continue
            mine = _SESSIONS.get(sid)
            if mine is None:
                _SESSIONS[sid] = slot
                continue
            theirs_ivs = list(slot.get("ivs") or [])
            theirs_claims = dict(slot.get("iv_claims") or {})
            mine_claims = dict(mine.get("iv_claims") or {})
            if float(slot.get("at") or 0) > float(mine.get("at") or 0):
                mine.update(slot)
            merged = list(mine.get("ivs") or [])
            for iv in theirs_ivs:
                if iv not in merged:
                    merged.append(iv)
            mine["ivs"] = merged[:8]
            # Claims union the same way the IVs do, NEWEST time per IV --
            # `mine.update(slot)` above would otherwise let the last writer's
            # claims dict erase the other process's newer claim, and the claim
            # time is the identity tiebreak (_lobby_arbitrate), so losing one
            # re-opens the wrong-account bind this exists to close.
            for k, t in theirs_claims.items():
                try:
                    if float(t or 0) > float(mine_claims.get(k) or 0):
                        mine_claims[k] = float(t)
                except (TypeError, ValueError):
                    continue
            if mine_claims:
                mine["iv_claims"] = mine_claims
            _SESSIONS[sid] = mine


def _session_put(sid, **fields):
    """Record auth state for one SESSION, and expire anything stale.

    `sid` is a session id (see _sid_for_user_token), not an address. Pass
    peer_ip=... as a field to record where the session is dialling from; that is
    a hint for candidate ordering and for the log, and nothing looks a session up
    by it.
    """
    now = time.time()
    with _SESSIONS_LOCK:
        for k in [k for k, v in _SESSIONS.items() if now - v["at"] > _SESSION_TTL]:
            del _SESSIONS[k]
        slot = _SESSIONS.setdefault(sid,
                                    {"iv": None, "member_id": None, "at": now,
                                     "ivs": [], "peer_ip": None})
        slot.update(fields)
        slot["at"] = now
        # KEEP EVERY IV THIS PEER HAS USED, newest first.
        #
        # The note above says two clients behind one NAT collide. What it did not
        # anticipate: **a launched GAME is a second client from the same address.**
        # A launched PS2 title opens its own auth hop, recovers its own IV, and
        # `slot["iv"]` overwrote the Viewer's -- after which the Viewer's own lobby
        # traffic decrypted with the game's IV and every header came out garbage.
        # Measured 2026-08-12: pre-launch lobby messages decode (type=0x02, length
        # self-consistent), post-launch ones do not.
        #
        # A list costs nothing and the lobby header is SELF-VALIDATING
        # (40 + payload_len == frame length), so the right IV can be *identified*
        # rather than guessed -- see _lobby_iv_candidates / the decrypt site.
        #
        # It stays a LIST now that sessions are per launch, because one launch
        # still re-keys: each auth hop recovers its own IV, and the lobby may
        # arrive holding either.
        iv = fields.get("iv")
        if iv:
            ivs = slot.setdefault("ivs", [])
            if iv in ivs:
                ivs.remove(iv)
            ivs.insert(0, iv)
            del ivs[8:]
            # ...AND WHEN this session last presented that IV -- the per-IV
            # CLAIM time. Two accounts on one machine end up holding the SAME
            # IV (the client caches its session cipher on disk and replays it
            # across account switches; the per-IP stamp pool replays the same
            # tokens to every account on the address), so "this IV is in the
            # slot" cannot decide identity between them. "Who authenticated
            # with this cipher most recently" can, and this is where that fact
            # is recorded -- `_lobby_arbitrate` reads it. Keys are hex so the
            # dict survives the JSON round-trip to the other container as-is.
            if isinstance(iv, (bytes, bytearray)):
                claims = slot.setdefault("iv_claims", {})
                claims[bytes(iv).hex()] = now
                keep = {bytes(i).hex() for i in ivs
                        if isinstance(i, (bytes, bytearray))}
                for k in [k for k in claims if k not in keep]:
                    del claims[k]
        # Publish for the other container (lobby/world run separately when the
        # authsess split is enabled). In the single-process case the store is
        # in memory and this costs a dict write per changed session.
        if os.environ.get("POL_SESSION_SHARE", "1") == "1":
            _sessions_save_locked()


def _session_get_for(peer_ip, field):
    """The newest value of `field` across the sessions dialling from `peer_ip`.

    key_candidates() wants the keys issued to an address, and it runs BEFORE the
    USER line that names the session, so an address is all it has. That is a
    legitimate use: a wrong key here costs one failed crib match, whereas a wrong
    member_id would serve somebody else's account. Nothing else may resolve
    identity this way.
    """
    _sessions_refresh()
    with _SESSIONS_LOCK:
        best, best_at = None, -1.0
        for slot in _SESSIONS.values():
            if slot.get("peer_ip") != peer_ip:
                continue
            v = slot.get(field)
            if v is not None and float(slot.get("at") or 0) > best_at:
                best, best_at = v, float(slot.get("at") or 0)
        return best


def _session_get(field):
    """This thread's session's value.

    The fallback is deliberately narrow: if this thread never bound a session, it
    borrows the most recent one ONLY when that is the single live session. With
    two clients up there is no defensible guess -- the old code took the most
    recent regardless, which is precisely how player two was served player one's
    account -- so it returns None and the caller's own fallback (a primary
    handle, an empty list) applies instead.
    """
    _sessions_refresh()
    with _SESSIONS_LOCK:
        sid = _session_sid()
        slot = _SESSIONS.get(sid) if sid else None
        if slot is None and sid is None and len(_SESSIONS) == 1:
            slot = next(iter(_SESSIONS.values()))
        return slot.get(field) if slot else None


def _session_handle_id(db=None):
    """The handle this client is logged in AS, or its member's primary handle.

    The client announces it on 4:7 (see `_capture_active_handle`); this is where
    that lands. It matters for every request that says "me" without naming a
    handle -- above all the 05:01 profile write, which carries no z_hid at all,
    so without this a two-handle account writes both profiles to whichever row
    the query happened to return first.

    Falling back to the primary handle keeps the pre-4:7 window working (the
    client fetches its profile before it ever sends one) and is right for the
    single-handle accounts that are the common case.
    """
    hid = _session_get("handle_id")
    if hid:
        return int(hid)
    if accounts is None:
        return None
    mid = _session_member_id()
    if not mid:
        return None
    own = db is None
    try:
        if own:
            db = accounts.connect()
        row = accounts.primary_handle_row(db, mid)
        return int(row["id"]) if row else None
    except Exception:
        return None
    finally:
        if own and db is not None:
            db.close()


def _session_handle_name():
    """The NAME of the handle this client is logged in as, or None.

    Thin wrapper over `_session_handle_id` for the callers that compare
    against name-keyed maps (`_client_guid_map` keys on handle_name).
    """
    if accounts is None:
        return None
    try:
        db = accounts.connect()
        try:
            hid = _session_handle_id(db)
            if not hid:
                return None
            row = db.execute("SELECT handle_name FROM handle WHERE id = %s",
                             (int(hid),)).fetchone()
            return row["handle_name"] if row else None
        finally:
            db.close()
    except Exception:
        return None


def _session_member_id():
    """The logged-in member id, or the lowest id as a pre-login fallback.

    WARNING: THE FALLBACK IS WHY A REFUSED LOGIN COULD STILL READ AN ACCOUNT. Measured
    2026-08-17 with a test client: a NICK with the wrong credential was
    refused (`wrong password (token mismatch)`, SE status 0xCA) -- and the lobby
    then served that connection member 1's HANDLE LIST, FRIEND LIST and
    COMMENTS. The refusal still registers the connection's IV (it has to: the
    client needs it to decrypt the reject record), and the lobby identifies a
    session by *which IV decrypts its header*, so a refused session is
    indistinguishable from an accepted one at that point. Everything downstream
    then found no member_id and fell through to "the lowest id".

    So a session that was EXPLICITLY REFUSED is no longer eligible for the
    fallback. This is deliberately the narrowest possible fix: it changes nothing
    for a session that never attempted auth (the pre-login case the fallback
    exists for -- the portal reads this before any NICK), and nothing for one
    that authenticated. Only a session we ourselves said "no" to is affected.
    Escape hatch: POL_LOBBY_REQUIRE_AUTH=0 restores the old behaviour.
    """
    mid = _session_get("member_id")
    if mid:
        return mid
    if _session_get("auth_refused") \
            and os.environ.get("POL_LOBBY_REQUIRE_AUTH", "1") != "0":
        log("lobby", "  member lookup REFUSED: this session's login was rejected, "
                     "so it does not get the pre-login fallback account "
                     "(POL_LOBBY_REQUIRE_AUTH=0 to restore the old behaviour)")
        return None
    if accounts is None:
        return None
    try:
        db = accounts.connect()
        try:
            row = db.execute("SELECT id FROM member ORDER BY id LIMIT 1").fetchone()
            return int(row["id"]) if row else None
        finally:
            db.close()
    except Exception:
        return None


def _epoch(iso):
    """An ISO `%Y-%m-%dT%H:%M:%SZ` stamp as a UTC unix time_t, or 0."""
    if not iso:
        return 0
    try:
        dt = datetime.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())


#: The 04:06 reply is a 128-byte SESSION RECORD, not a list. Read straight out of
#: polcore's state machine (table 0x37dd7b0, state 5 reads the hardcoded 0x80,
#: state 6 at 0x37dd64d consumes it) -- every field it touches, in order:
#:
#:     +0x00  u8   index into the 64-entry table at polcore 0x386ab28 (0x68 stride,
#:                 filled from the 1:3 list). Stored at [0x383541c] if < 0x40.
#:     +0x01  u8   0 -> [0x3835420] = -1; non-zero -> compute it from +0x02
#:     +0x02  u8   < 8, indexes a status table at 0x3bc5820 -> [0x3835420]
#:     +0x03  u8   == 1 -> [0x386c54c] = 1
#:     +0x04  u16  -> [0x3835428]
#:     +0x06  u8   0 -> the whole record is treated as ABSENT ([0x383541c] = -1,
#:                 [0x3835424] = 4); otherwise [0x3835424] = this - 1
#:     +0x07  u8   -> [0x386aac8]
#:     +0x08  u32  -> [0x386ab24] = the PML system variable **$_LAST_LOGIN**
#:     +0x0C  u32  -> [0x386ab20] = the PML system variable **$_LAST_LOGOUT**
#:     +0x76  u8   bit 0 -> [0x386c528]
#:
#: The two timestamps are plain UTC unix time_t (the client renders them in local
#: time; that is why a zero showed as 12/31/1969 07:00 PM at UTC-5). app.dll's PML
#: resolver reaches them through polcore sub_037dc840 -- `f(time_t *login,
#: time_t *logout)` with a NULL for whichever it does not want -- called through
#: the polcore function table at [app.dll 0x4e17c84] + 0x14ac. The chain is
#: app.dll 0x4a4f763 ($_LAST_LOGIN) / 0x4a4f79f ($_LAST_LOGOUT).
#:
#: This REPLACES serving 32-byte friend entries here. They wrote a handle id over
#: +0x08 and the type flags over +0x0C, which is precisely the 12/31/1969 clock:
#: handle id 1 renders as epoch+1s. 4:6 was never the Friend List source anyway --
#: that screen draws from 2:3 (168-byte records).
SESSION_RECORD_LEN = 0x80


#: Live-tunable comment read-back, so the offset can be swept while the client
#: stays connected (same idiom as presence.ctl / group.ctl). Keys: `off` (the
#: byte offset in the 04:06 reply, default 0x10; anything below 0x10 is refused
#: because that is the active handle + login clock). Clear the file when done.
_COMMENT_CTL_FILE = os.environ.get(
    "POL_COMMENT_CTL", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                    "comment.ctl"))
_COMMENT_CTL = {"mtime": None, "vals": {}}


def _comment_cfg_int(key, default):
    try:
        st = os.stat(_COMMENT_CTL_FILE)
        if st.st_mtime != _COMMENT_CTL["mtime"]:
            vals = {}
            with open(_COMMENT_CTL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            _COMMENT_CTL["vals"], _COMMENT_CTL["mtime"] = vals, st.st_mtime
    except OSError:
        _COMMENT_CTL["vals"], _COMMENT_CTL["mtime"] = {}, None
    try:
        return int(str(_COMMENT_CTL["vals"].get(key, default)), 0)
    except ValueError:
        return default


def _own_comment():
    """The session handle's stored 04:03 comment, or None."""
    if accounts is None:
        return None
    try:
        db = accounts.connect()
        try:
            hid = _session_handle_id(db)
            if not hid:
                return None
            val = accounts.get_handle_profile(db, int(hid)).get(profilerecord._COMMENT_FIELD)
            return str(val) if val else None
        finally:
            db.close()
    except Exception:
        return None


def _session_record(n):
    """The 04:06 session record: the account's login/logout clock."""
    out = bytearray(n)
    prev_login = last_logout = None
    if accounts is not None:
        try:
            db = accounts.connect()
            try:
                mid = _session_member_id()
                if mid:
                    prev_login, last_logout = accounts.login_times(db, mid)
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"session record: DB unavailable ({exc})")
    login_t, logout_t = _epoch(prev_login), _epoch(last_logout)
    if n >= 0x10:
        struct.pack_into("<II", out, 0x08, login_t & 0xFFFFFFFF,
                         logout_t & 0xFFFFFFFF)

    # THE ACTIVE HANDLE LIVES IN THE FIRST 8 BYTES. Read out of FriendList.dll's
    # own 4:6 handler at 0x100ffdbd (memory image; the DLL is packed on disk, so
    # this came from a live minidump -- see work/friendlist/flcode.py):
    #
    #     mov  edi, [esi+0x328]     ; the 4:6 reply payload
    #     mov  al, byte [edi + 6]
    #     cmp  al, bl               ; bl = 0
    #     jne  parse_handle
    #     mov  [0x101df15c], edx    ; edx = -1  ->  ACTIVE HANDLE = -1
    #   parse_handle:
    #     mov  al, byte [edi]       ; slot
    #     cmp  al, 0x40
    #     jae  skip                 ; slot must be < 0x40, same cap as 0:9
    #     mov  [0x101df15c], eax    ; ACTIVE HANDLE = byte 0
    #     mov  al, byte [edi + 6]
    #     dec  eax
    #     mov  [0x101df164], eax    ; (count - 1)
    #     cmp  byte [edi + 1], bl   ; byte 1 must be non-zero to go on
    #
    # A negative 0x101df15c is what the standalone Friend List reports as
    # "You have no active handle." (string 6961): its session state machine at
    # 0x1008d600 reads it back through the function table at +0x2F8 and jumps to
    # state 4. The Viewer never cared because it DECLARES its handle with 4:7 --
    # this app never sends 4:7 at all and expects to be told.
    #
    # These three bytes sit BELOW the clock at +0x08, so this costs the lobby
    # date nothing -- unlike POL_FRIENDS=1, which overwrites it (and which does
    # not set these offsets either, so it never fixed this).
    handles = handlelists._db_handles()
    slot = 0
    if handles:
        hid = _session_handle_id()
        for i, (h, _name, _prof) in enumerate(handles):
            if hid and h == hid:
                slot = i
                break
    if n > 6 and handles:
        out[0x00] = slot & 0x3F              # < 0x40, as the client checks
        out[0x01] = 1                        # non-zero or the client skips on
        out[0x06] = min(len(handles), 0xFF)  # handle count; client uses count-1
    # THE COMMENT READ-BACK -- a candidate, deliberately sweepable.
    #
    # 04:03 (KPutMyCommentForFriend) WRITES the comment and we store it, but
    # nothing ever served it back: it went into handle profile field 100, and
    # `_profile_record` only emits ids that appear in `_PROFILE_SCHEMA` (0..31),
    # so `fields.get(100)` was never consulted. Stored, then silently dropped --
    # which is why the client kept drawing an unfilled buffer.
    #
    # WHY HERE: the 04:03 write payload is 112 bytes with the comment at its very
    # top (captured live: 'rrrrrr' as UTF-16LE at payload+0), and this 128-byte
    # 04:06 reply uses only its first 16 bytes, leaving EXACTLY 112 free. Same
    # width, same 102-byte comment + 10 spare. That is a candidate, not a
    # measurement -- so the offset is a knob and the default is the one the
    # symmetry points at.
    #
    # Never touches 0x00..0x0F: that is the active handle + the login clock, and
    # getting those wrong is what made the lobby read 12/31/1969 once already.
    off = _comment_cfg_int("off", 0x10)
    text = _own_comment()
    if text and off >= 0x10 and n >= off + 2:
        raw = text.encode("utf-16-le", "replace")[:profilerecord._COMMENT_MAX - 2]
        raw = raw[:max(0, n - off - 2)] + b"\x00\x00"
        out[off:off + len(raw)] = raw
        log("lobby", f"  session 4:6: comment {text!r} at +{off:#04x} "
                     f"({len(raw)}B UTF-16LE)")
    log("lobby", f"  session 4:6: $_LAST_LOGIN={prev_login or '(none)'} "
                 f"({login_t}), $_LAST_LOGOUT={last_logout or '(none)'} "
                 f"({logout_t}), active handle slot={slot} "
                 f"count={len(handles)}")
    # THE TM MEMBER-SIDEBAR SELF-ID IS SET FROM THIS RECORD (2026-08-21).
    # polcore's 4:6 consumer (state 6, 0x37dd64d -- see the layout note above
    # SESSION_RECORD_LEN) stores byte 0 at [0x383541c] and derives [0x3835420]
    # from bytes 1/2 via the status table at 0x3bc5820 -- and that PAIR is what
    # TM.dll's member-menu self-guard (0x925F0) compares the selected member's
    # id against. Log the identity half verbatim so a live run can be matched
    # against readself.py's dump of those globals, byte for byte.
    log("lobby", f"  session 4:6 identity bytes[0..7]={bytes(out[:8]).hex()} "
                 f"-> polcore will set [0x383541c]=byte0={out[0]}, "
                 f"[0x3835420]=statustable[byte2={out[2]}] (byte1={out[1]} "
                 f"non-zero keeps it live)")
    return bytes(out)


def payload_my_status(n, req_pt):
    """4:6 KGetMyStatus (lobby opcode table): the session record.

    POL_FRIENDS=1 restores the pre-2026-08-12 behaviour (32-byte friend
    entries here) for A/B. It is WRONG -- see _session_record -- and it is
    what made the lobby clock read 12/31/1969."""
    if os.environ.get("POL_FRIENDS", "0") == "1":
        return friendlist._friend_payload(n)
    return _session_record(n)
