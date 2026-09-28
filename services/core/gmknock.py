"""GM knock: tell a player, live, that their GM chat is ready.

Retail's GM did not wait for the player's GM Call screen to poll. It sent a
KNOCK: an ordinary PlayOnline message of type 0x0D (kind word 0x8680, the
record's +0x3E bitfield) whose text is followed by a binary tail app.dll reads
at 0x48a6816 -> 0x4ab2656:

    tail +0x04  room name, 0x33 bytes      (the same field 0x801 carries)
    tail +0x37  room key, 0x18 bytes
    tail +0x4F  action: 0 = knock, 2 = close the call
    tail +0x50  u32 request number -- equal to the player's own Request No.
                enables Start (and saves the reservation), otherwise Join

The message rides the existing mail push (`lobbymail._mail_mint` stores it and
announces it; authserv NOTICEs the recipient's sessions), so the Viewer reads
it without being asked and shows "Knock!".

The admin panel runs in its own container, so a knock crosses through the
live-state store like a kick (core/authkick.py): the panel pushes
`{"reply", "expires", "handle", "guid", "room", "key", "request_no"}` onto
`gmknock:queue` and waits on the reply key; the watcher here, started with
`authserv`, mints and answers `{"ok", "path"}` or `{"ok": false, "error"}`.
"""
import json
import os
import struct
import time
from srvcore import log
from polcore import kv
from .deps import accounts
from . import lobbymail

KNOCK_QUEUE = "gmknock:queue"
KNOCK_REPLY = "gmknock:reply:"
_KNOCK_REPLY_TTL = 60

MAIL_KIND_GM_KNOCK = 0x8680          # valid | type 0x0D << 7
ACTION_KNOCK, ACTION_CLOSE = 0, 2
_TAIL_LEN = 0x54
_ROOM_OFF, _ROOM_LEN = 0x04, 0x33
_KEY_OFF, _KEY_LEN = 0x37, 0x18
_ACTION_OFF, _REQNO_OFF = 0x4F, 0x50

#: What the knock says in the player's message list. SE's own GM Call strings.
SENDER = os.environ.get("POL_GM_KNOCK_SENDER", "GM")
SUBJECT = os.environ.get("POL_GM_KNOCK_SUBJECT", "GM Call")
TEXT = os.environ.get("POL_GM_KNOCK_TEXT", "GM chat is ready. Please start GM chat.")


def knock_tail(room, key, request_no, action=ACTION_KNOCK):
    """The 0x54-byte block app.dll reads after the knock's text."""
    t = bytearray(_TAIL_LEN)
    r = str(room or "").encode("cp932", "replace")[:_ROOM_LEN - 1]
    k = str(key or "").encode("cp932", "replace")[:_KEY_LEN - 1]
    t[_ROOM_OFF:_ROOM_OFF + len(r)] = r
    t[_KEY_OFF:_KEY_OFF + len(k)] = k
    t[_ACTION_OFF] = action & 0xFF
    struct.pack_into("<I", t, _REQNO_OFF, int(request_no or 0) & 0xFFFFFFFF)
    return bytes(t)


def _recipient_handle(db, name, guid):
    """The handle row a ticket names: by handle name, else by the handle id
    gmd recorded from the call (in-game calls leave the name empty)."""
    h = accounts.handle_by_name(db, name) if name else None
    if h is None and guid:
        h = accounts.handle_by_client_guid(db, int(guid))
    return h


def send_knock(name, guid, room, key, request_no, action=ACTION_KNOCK):
    """Mint and announce one knock. Returns the message path; raises on error."""
    db = accounts.connect()
    try:
        h = _recipient_handle(db, name, guid)
    finally:
        db.close()
    if h is None:
        raise ValueError("the player on this request has no handle we know")
    path = lobbymail._mail_mint(
        SENDER, 0, accounts.handle_guid(int(h["id"])), SUBJECT, TEXT,
        kind=MAIL_KIND_GM_KNOCK, tail=knock_tail(room, key, request_no, action))
    if not path:
        raise ValueError("the knock message could not be stored")
    log("authserv", f"GM knock ({'close' if action == ACTION_CLOSE else 'ready'}) "
                    f"for request #{request_no} sent to {h['handle_name']!r} "
                    f"(room {room!r})")
    return path


def _knock_request(raw):
    try:
        req = json.loads(raw)
        reply = req["reply"]
        if not isinstance(reply, str) or not reply.startswith(KNOCK_REPLY):
            return None
        if float(req.get("expires") or 0) < time.time():
            log("authserv", "GM knock request expired before it was read; dropped")
            return None
    except (ValueError, KeyError, TypeError):
        log("authserv", f"GM knock request unreadable: {raw[:120]!r}")
        return None
    try:
        path = send_knock(req.get("handle"), req.get("guid"), req.get("room"),
                          req.get("key"), req.get("request_no"),
                          int(req.get("action") or ACTION_KNOCK))
        return reply, {"ok": True, "path": path}
    except ValueError as exc:
        return reply, {"ok": False, "error": str(exc)}
    except Exception as exc:
        log("authserv", f"GM knock failed: {exc!r}")
        return reply, {"ok": False, "error": "the knock could not be sent"}


def _knock_watcher():
    """Daemon (authserv only): send the admin panel's knocks."""
    log("authserv", f"GM knocks read from {KNOCK_QUEUE}")
    while True:
        try:
            raw = kv.pop(KNOCK_QUEUE, timeout=1)
            if raw is None:
                continue
            done = _knock_request(raw)
            if done is not None:
                kv.push(done[0], json.dumps(done[1]))
                kv.expire(done[0], _KNOCK_REPLY_TTL)
        except Exception as exc:
            log("authserv", f"GM knock watcher error: {exc!r}")
            time.sleep(1.0)
