"""Broadcast: one PlayOnline message to every member who is online.

The mail half of the maintenance warning (services/maintnotice.py has the
forced-news half and says why there is no third way). A minted message rides
the existing push (`lobbymail._mail_mint` stores it and announces it, authsess
NOTICEs the recipient's sessions), so a player in a game still sees the
message prompt, which is how SE reached players with notices of its own.

Who gets it: every member with a live session row (presence IS the session
table, `accounts.member_online`), once per handle they own, because the
message is addressed to a handle and we cannot know which of a member's
handles the Viewer is on. A member sees only the copy for the handle they
are logged in as; the others wait in that handle's mailbox like any mail.

The admin panel runs in its own container, so the request crosses through the
live-state store exactly like a kick or a GM knock: the panel pushes
`{"reply", "expires", "subject", "text", "sender"}` onto `broadcast:queue` and
waits on the reply key; the watcher here, started with `authserv`, mints and
answers `{"ok", "members", "messages"}` or `{"ok": false, "error"}`.
"""
import json
import time
from srvcore import log
from polcore import kv
from .deps import accounts
from . import lobbymail

QUEUE = "broadcast:queue"
REPLY = "broadcast:reply:"
_REPLY_TTL = 60
#: The message list shows 15 characters of a subject (lobbymail caps it).
SUBJECT_MAX = 15
SENDER = "PlayOnline"


def online_handles(db):
    """[(member_id, handle_id, handle_name)] for every online member."""
    rows = db.execute(
        "SELECT h.member_id, h.id, h.handle_name FROM handle h "
        "WHERE h.member_id IN (SELECT DISTINCT member_id FROM session "
        "WHERE expires_at > %s) ORDER BY h.member_id, h.id",
        (accounts._now(),)).fetchall()
    return [(int(r["member_id"]), int(r["id"]), r["handle_name"]) for r in rows]


def send_broadcast(subject, text, sender=SENDER, mint=None):
    """Mint one message per online handle. Returns (members, messages)."""
    subject = str(subject or "").strip()[:SUBJECT_MAX]
    text = str(text or "").strip()
    if not subject or not text:
        raise ValueError("a broadcast needs a subject and a text")
    mint = mint or lobbymail._mail_mint
    db = accounts.connect()
    try:
        targets = online_handles(db)
    finally:
        db.close()
    members, sent = set(), 0
    for member_id, hid, name in targets:
        try:
            path = mint(sender, 0, accounts.handle_guid(hid), subject, text)
        except Exception as exc:                          # noqa: BLE001
            log("authserv", f"broadcast: {name!r} failed ({exc!r})")
            continue
        if path:
            sent += 1
            members.add(member_id)
    log("authserv", f"broadcast {subject!r}: {sent} message(s) to "
                    f"{len(members)} online member(s)")
    return len(members), sent


def _request(raw):
    try:
        req = json.loads(raw)
        reply = req["reply"]
        if not isinstance(reply, str) or not reply.startswith(REPLY):
            return None
        if float(req.get("expires") or 0) < time.time():
            log("authserv", "broadcast request expired before it was read; dropped")
            return None
    except (ValueError, KeyError, TypeError):
        log("authserv", f"broadcast request unreadable: {raw[:120]!r}")
        return None
    try:
        members, sent = send_broadcast(req.get("subject"), req.get("text"),
                                       req.get("sender") or SENDER)
        return reply, {"ok": True, "members": members, "messages": sent}
    except ValueError as exc:
        return reply, {"ok": False, "error": str(exc)}
    except Exception as exc:                              # noqa: BLE001
        log("authserv", f"broadcast failed: {exc!r}")
        return reply, {"ok": False, "error": "the broadcast could not be sent"}


def _watcher():
    """Daemon (authserv only): send the admin panel's broadcasts."""
    log("authserv", f"broadcasts read from {QUEUE}")
    while True:
        try:
            raw = kv.pop(QUEUE, timeout=1)
            if raw is None:
                continue
            done = _request(raw)
            if done is not None:
                kv.push(done[0], json.dumps(done[1]))
                kv.expire(done[0], _REPLY_TTL)
        except Exception as exc:                          # noqa: BLE001
            log("authserv", f"broadcast watcher error: {exc!r}")
            time.sleep(1.0)
