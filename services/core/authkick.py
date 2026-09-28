"""Administrator kick: disconnect a PlayOnline ID's live auth channels on request.

The admin panel runs in its own container and holds no sockets; `authsess`
owns every live channel. A kick request therefore crosses through the
live-state store (polcore.kv), like the push queue: the panel pushes
`{"polid", "reply", "expires"}` onto `authkick:queue` and waits on the reply
key, and the watcher thread here, started with `authserv`, pops the request,
sends each live channel the retail IRC KILL, closes its socket, retires its
session rows and answers `{"ok", "kicked"}` (or `{"ok": false, "error"}`).

A request that nobody answers in time is withdrawn by the panel, and one the
watcher finds past its `expires` is dropped unanswered, so an authsess that
starts later never replays an old kick.

A login that has opened its session row but not yet registered its channel is
exempt from a kick in progress: it is remembered in `_PENDING_SESSION_TOKENS`
and retired when it finishes (`_finish_pending_session`), so it cannot end up
online with its launch half torn down.
"""
import json
import socket
import threading
import time
from srvcore import log
from polcore import kv
from .deps import accounts
from . import lobbysession, logingate, presence


#: The request queue and the reply key prefix (docs/database.md). admin.py
#: (`_request_kick`) writes the same names; it does not import the core.
KICK_QUEUE = "authkick:queue"
KICK_REPLY = "authkick:reply:"
#: How long a reply is kept for a panel that has stopped waiting.
_KICK_REPLY_TTL = 60

_KICK_LOCK = threading.Lock()
# A login opens its DB row before it has a registered channel. Keep that brief
# interval visible to kick selection without putting a generation in the DB.
_SESSION_OPEN_LOCK = threading.Lock()
_PENDING_SESSION_TOKENS = {}          # DB token -> launch/session id
_KICKED_PENDING_SESSION_TOKENS = set()


def _register_account_channel(member_id, sess, token=None, resume=False):
    """Register a login's channel in PRESENCE, unless a kick retired it first.

    Returns False only for a resumed channel whose launch a kick has closed."""
    with _SESSION_OPEN_LOCK:
        if resume:
            # A kick may have retired the launch after the resume door read it.
            with lobbysession._SESSIONS_LOCK:
                open_channel = (lobbysession._SESSIONS.get(sess.sid) or {}).get(
                    "channel_open")
            if not open_channel:
                return False
            lobbysession._session_put(sess.sid, channel_open=True, viewer_open=True,
                                      peer_ip=sess.peer_ip.decode("ascii"))
        presence.PRESENCE.register(int(member_id), sess)
        if token is not None:
            _PENDING_SESSION_TOKENS.pop(token, None)
            _KICKED_PENDING_SESSION_TOKENS.discard(token)
    return True


def _retire_kicked_launch(member_id, sid):
    """Clear a kicked launch's routing/resume flags under _SESSION_OPEN_LOCK.

    A member can have several launches. Preserve a channel or pending login
    that reused this launch id; account-wide online state is a separate check.
    """
    if not sid or sid in _PENDING_SESSION_TOKENS.values():
        return
    if any(sess.alive and not sess.admin_kicked and not sess.killed_dup
           and sess.sid == sid for sess in presence.PRESENCE.sessions_for(member_id)):
        return
    try:
        lobbysession._session_put(sid, viewer_open=False, channel_open=False)
    except Exception as exc:
        log("authserv", f"could not retire kicked launch {sid}: {exc!r}")


def _finish_pending_session(token, member=None, peer=""):
    """Retire an unfinished hop if a kick spared it while it was opening."""
    with _SESSION_OPEN_LOCK:
        sid = _PENDING_SESSION_TOKENS.pop(token, None)
        was_kicked = token in _KICKED_PENDING_SESSION_TOKENS
        _KICKED_PENDING_SESSION_TOKENS.discard(token)
        if was_kicked and member is not None:
            _retire_kicked_launch(int(member["id"]), sid)
    if was_kicked and member is not None:
        presence._logout_wipe(int(member["id"]), member["login_name"], peer, None,
                              why=" -- unfinished login during administrator kick",
                              kick_tokens=(token,))


def _kick_live_account(polid):
    """Send the retail IRC KILL to each live channel, then close its socket."""
    with _SESSION_OPEN_LOCK:
        sessions = [sess for sess in presence.PRESENCE.sessions_for_polid(polid)
                    if not sess.admin_kicked]
        if not sessions:
            return 0
        db = accounts.connect()
        try:
            # Old redirect hops have DB rows but no live socket. Retire those
            # too; only a login still between open_session and registration is
            # exempt from this kick.
            tokens = {}
            for sess in sessions:
                mid = int(sess.member["id"])
                if mid not in tokens:
                    found = [row["token"] for row in db.execute(
                        "SELECT token FROM session WHERE member_id = %s", (mid,))]
                    _KICKED_PENDING_SESSION_TOKENS.update(
                        token for token in found
                        if token in _PENDING_SESSION_TOKENS)
                    tokens[mid] = tuple(
                        token for token in found
                        if token not in _PENDING_SESSION_TOKENS)
        finally:
            db.close()
        for sess in sessions:
            sess.admin_kicked = True
        for sess in sessions:
            _retire_kicked_launch(int(sess.member["id"]), sess.sid)
    kicked = 0
    for sess in sessions:
        try:
            delivered = sess.send([logingate.pol_kill_line(sess.srv, sess.nick)])
        except Exception as exc:
            log("authserv", f"admin kick could not send KILL to {sess.nick!r}: {exc!r}")
            delivered = False
        try:
            sess.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        log("authserv", f"admin kicked {polid} channel {sess.nick!r}: "
                        f"IRC KILL {'sent' if delivered else 'send failed'}")
        kicked += 1
    # The kick request owns DB cleanup. A channel handler may already be in
    # teardown (or may fail there), so it cannot be relied on to reach a kick
    # branch after shutdown.
    owners = {}
    for sess in sessions:
        owners.setdefault(int(sess.member["id"]), sess)
    for member_id, sess in owners.items():
        presence._logout_wipe(member_id, sess.member["login_name"], "admin",
                              None, why=" -- administrator kick",
                              kick_tokens=tokens[member_id])
    return kicked


def _kick_request(raw):
    """Carry out one queued request. Returns (reply key, result), or None for a
    request that is malformed beyond answering or has already expired."""
    try:
        req = json.loads(raw)
        reply = req["reply"]
        if not isinstance(reply, str) or not reply.startswith(KICK_REPLY):
            return None
    except (ValueError, KeyError, TypeError):
        log("authserv", f"kick request unreadable: {raw[:120]!r}")
        return None
    try:
        if float(req.get("expires") or 0) < time.time():
            log("authserv", f"kick request for {req.get('polid')!r} expired "
                            f"before it was read; dropped")
            return None
    except (TypeError, ValueError):
        return None
    polid = req.get("polid")
    try:
        if not isinstance(polid, str) or not polid or len(polid) > 64:
            raise ValueError("invalid PlayOnline ID")
        with _KICK_LOCK:
            result = {"ok": True, "kicked": _kick_live_account(polid)}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        log("authserv", f"kick request failed: {exc!r}")
        result = {"ok": False, "error": "auth service could not kick account"}
    return reply, result


def _kick_answer(reply, result):
    kv.push(reply, json.dumps(result))
    kv.expire(reply, _KICK_REPLY_TTL)


def _kick_watcher():
    """Daemon (authserv only): answer the admin panel's kick requests."""
    log("authserv", f"kick requests read from {KICK_QUEUE}")
    while True:
        try:
            raw = kv.pop(KICK_QUEUE, timeout=1)
            if raw is None:
                continue
            done = _kick_request(raw)
            if done is not None:
                _kick_answer(*done)
        except Exception as exc:
            log("authserv", f"kick watcher error: {exc!r}")
            time.sleep(1.0)

