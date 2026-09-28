"""Refusing a lobby request: answering it with an error type instead of success.

The client's reader turns a non-zero reply type into POL-(5200 + type). Until
2026-09-28 only the 3:0 no-data path ever answered with one, so every other
refusal (a fifth group, a disband by someone who is not the owner, a rank
change the caller may not make, a profile that does not exist) was logged and
then answered as SUCCESS. The client then believed the action had happened,
and its screen disagreed with the server from that point on.

The codes are the ones Project Crystal Server answers with against the real
Viewer (POLProfile/RequestHandler.cs), each named for the refusal it reports.
"""
import os
import threading

from srvcore import log

LOBBY_ERR_GENERIC = 0xFF            # not permitted, or malformed
LOBBY_ERR_GROUP_MAX = 0x73          # 5315: you can only join up to 4 groups
LOBBY_ERR_GROUP_NAME = 0x74         # 5316: that group name is already in use
LOBBY_ERR_GROUP_ALREADY = 0x75      # 5317: already invited, or awaiting confirmation
LOBBY_ERR_GROUP_FULL = 0x78         # 5320: a group can only have up to 64 members
LOBBY_ERR_NO_PROFILE = 0x7C         # 5324: no profile found

_REFUSAL = threading.local()


def _lobby_refuse(code, why, req_pt):
    """Answer THIS request with error type `code` instead of success.

    Either side of the reply can refuse: the capture step that runs just
    before the reply (`lobbycapture._lobby_capture`, e.g. a 3:1 group invite)
    or the handler that builds it (`lobbyreply._lobby_payload`). The two see
    DIFFERENT decryptions of the same request, so the refusal is keyed by the
    request's bytes, never by object identity, and `_lobby_take_refusal` only
    hands it to the reply for that same request. Both run on the connection's
    own thread, which is why the slot is thread-local.

    POL_LOBBY_REFUSALS=0 logs the refusal and answers success, as before.
    """
    code = int(code) & 0xFF
    if os.environ.get("POL_LOBBY_REFUSALS", "1") != "1":
        log("lobby", f"  would refuse with type {code:#04x} (POL-{5200 + code}) "
                     f"but POL_LOBBY_REFUSALS=0: {why}")
        return
    _REFUSAL.code = code
    _REFUSAL.req = bytes(req_pt) if req_pt is not None else None
    log("lobby", f"  REFUSED with type {code:#04x} (POL-{5200 + code}): {why}")


def _lobby_take_refusal(req_pt):
    """The refusal recorded for `req_pt`, or None. Always clears the slot, so
    a refusal whose reply was never built cannot leak onto the next request."""
    code = getattr(_REFUSAL, "code", None)
    req = getattr(_REFUSAL, "req", None)
    _REFUSAL.code = None
    _REFUSAL.req = None
    if code is None or req is None or req_pt is None:
        return None
    if bytes(req_pt) != req:
        log("lobby", f"  a refusal ({code:#04x}) was recorded for a different "
                     f"request than this one; dropped, not applied")
        return None
    return code


def _lobby_refusal_header(code):
    """The whole reply for a refused request: the 24-byte header with the error
    type and no payload, the same wire form as the 3:0 no-data answer."""
    e = bytearray(24)
    e[0] = 0x83
    e[1] = int(code) & 0xFF
    return bytes(e)
