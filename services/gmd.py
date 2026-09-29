"""gmd -- the GM Call responder, in Python, in the stack.

A port of an earlier `gmserver.cpp`, which had to be a Windows EXE because it
mapped `polcore.dll` and called SE's own cipher out of it. That is no longer
necessary: `gmcrypt.py` is the cipher (CAST-128, see its docstring), so the GM
band can run anywhere the rest of the stack does -- including a Steam Deck or
any headless host, neither of which could ever serve a GM call before.

Everything here is measured off the live client.

    MESSAGE     +0x00 'MAG?' | +0x05 0x0a | +0x06 type | +0x08 length
                +0x0A echo16 | +0x0C echo32 | +0x14 checksum | +0x18 body
    CHECKSUM    +0x14 = 0xFFFFFFFF, then sum ceil(len/4) LE dwords into +0x14
    WIRE LENGTH (len & ~7) + 8 -- the cipher runs over the PADDED length
    REPLY       request type + 0x100, for every pair in the cft_12NN table

THREE CIPHER CONTEXTS PER CALL, and they are not interchangeable:

    sctx  key blob one   the client's stage-1 0x101 arrives on this
    gctx  key blob two   stage 2 arrives on this (the client's global context)
    zctx  ZERO key       everything WE send goes out on this, because the client
                         re-keys its per-slot context to zeros after stage 1

WARNING: A context is JUST A KEY: the CFB feedback is re-seeded from the IV on every
datagram, measured 2026-08-17 (see `gmcrypt.py`). Trials are therefore free and
need no clone, and a retransmission decrypts to the same plaintext every time.
The earlier "contexts are stateful, commit only on MAG?" model was assumed rather
than measured, and it is what broke stage 2: carrying the feedback from the
stage-1 reply into the stage-2 reply corrupted that reply's FIRST 8 BYTES, so the
client saw no `MAG?`, refused it, and retransmitted to the give-up count.

Session matching is by trial because the client CHANGES SOURCE PORT between stage
1 and stage 2, so the peer address is a hint for ordering candidates, never the key.

Configuration is environment only, so compose owns it:

    POL_GMD_PORT         51112
    POL_GMD_CHAT_IP      redirect target; default = our own address toward the
                         client, discovered per-call. HOST order on the wire --
                         network order sends the client to a reversed address.
    POL_GMD_CHAT_PORT    51112
    POL_GMD_STATUS_FLAGS 0x60   bit 0x20 -> Start (after a re-entry), 0x40 -> Join.
                         The answer when NOBODY has claimed the desk; the admin
                         panel's on-duty toggle overrides it live, per call, via
                         <ticket dir>/gm-control.json. See `read_control`.
    POL_GMD_FLAGS_ON_DUTY / _OFF_DUTY   0x60 / 0x20, what those claims mean
    POL_GMD_QUEUE        (unset) body +0x02, the "Users in queue: %d" the screen
                         shows. UNSET = derived from the live session table;
                         set it to a number to pin it. See `queue_depth`.
    POL_GMD_CHAT_ROOM    the IRC channel handed to the client; must start '#'
    POL_GMD_CHAT_KEY     that channel's key
    POL_GMD_TICKET_DIR   /data/gm-calls
"""
import os
import re
import socket
import struct
import sys
import time

try:
    # Per-client address (LAN / tailnet / internet-via-edge). See srvcore's
    # "The address a client is told to dial next" and deploy/edge/README.md.
    from srvcore import advertise_for
except ImportError:                     # standalone use outside services/
    def advertise_for(default, peer_ip=None, dialed_ip=None):
        return default

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: WARNING: THE CIPHER IS A DEPENDENCY OF THE SERVER, NOT OF THE RULES -- and importing
#: it at module scope took the admin dashboard down.
#:
#: `gmcrypt` raises ImportError on import when pycryptodome is absent (a good
#: refusal: see its docstring, a hand-written CAST-128 schedule was wrong once
#: already). But `admin.py` imports THIS module for `read_control` /
#: `effective_flags` / `on_duty` -- the precedence rules, which contain no
#: crypto -- and its image is built from `Dockerfile.ucs`, which does not install
#: pycryptodome. So the panel crash-looped on an import it never needed:
#:
#:     admin-1 | ImportError: gmcrypt needs pycryptodome for CAST-128
#:     admin-1 | File "/app/admin.py", line 63, in <module>  import gmd
#:
#: Every `gmcrypt` use in this file is inside `Session`/`Server`, i.e. the serve
#: path. So bind it lazily and keep the ORIGINAL error: a container that really
#: does try to serve GM calls without the cipher still gets gmcrypt's own
#: message, at the moment it matters, instead of an AttributeError on None.
try:
    import gmcrypt  # noqa: E402
    _GMCRYPT_ERROR = None
except ImportError as _exc:                                        # noqa: E402
    gmcrypt = None
    _GMCRYPT_ERROR = _exc


def _crypto():
    """`gmcrypt`, or re-raise exactly why it is not here.

    WARNING: CALL THIS IN THE SERVE PATH ONLY. Anything reachable from `admin.py` must
    stay importable without pycryptodome, which is the whole point of the banner
    above.
    """
    if gmcrypt is None:
        raise _GMCRYPT_ERROR
    return gmcrypt

PORT = int(os.environ.get("POL_GMD_PORT", "51112"))
CHAT_IP = os.environ.get("POL_GMD_CHAT_IP", "")
CHAT_PORT = int(os.environ.get("POL_GMD_CHAT_PORT", "51112"))
#: The fallback flags: what a caller is told when NOTHING says otherwise. This
#: used to be the only answer, and the note here explained why it had to be a
#: constant -- "that is a claim about a HUMAN, and nothing in this server observes
#: one ... what it would take is a real presence signal on the GM side, one line
#: written when an operator attaches, and then this becomes 0x60 if fresh else
#: 0x20." THAT SIGNAL NOW EXISTS: `control()` below, which the admin panel's
#: on-duty toggle writes. So this constant is the answer only while no operator
#: has said anything, and its value is unchanged, which is what keeps a stack
#: with no control file behaving exactly as it did.
STATUS_FLAGS = int(os.environ.get("POL_GMD_STATUS_FLAGS", "0x60"), 0)

#: What ON DUTY and OFF DUTY mean, once somebody is claiming one of them.
#: 0x20 offers Start and 0x40 offers Join (both VERIFIED live 2026-08-16), so
#: 0x60 = "a GM is here, come in".
#:
#: WARNING: THE OFF-DUTY VALUE IS A CHOICE, NOT A MEASUREMENT. 0x20 is what the note
#: this replaced proposed: leave Start on the screen so a caller can still
#: reserve a slot, but do not advertise a room to join that has nobody in it.
#: No capture of SE's own off-hours screen exists to check it against, and the
#: alternative readings (0x00 -- offer nothing) are equally defensible. Both are
#: env-tunable precisely because this is the field to A/B if the screen ever
#: looks wrong.
FLAGS_ON_DUTY = int(os.environ.get("POL_GMD_FLAGS_ON_DUTY", "0x60"), 0)
FLAGS_OFF_DUTY = int(os.environ.get("POL_GMD_FLAGS_OFF_DUTY", "0x20"), 0)

#: `Users in queue: %d`, and it is LIVE as of 2026-08-18. It used to be a flat 0
#: from POL_GMD_QUEUE while the server was already tracking every caller it had
#: -- so a second player calling while a first waited was told the queue was
#: empty. Set POL_GMD_QUEUE to a number to pin it again for an A/B.
QUEUE_OVERRIDE = os.environ.get("POL_GMD_QUEUE", "").strip()
CHAT_ROOM = os.environ.get("POL_GMD_CHAT_ROOM", "").encode()
CHAT_KEY = os.environ.get("POL_GMD_CHAT_KEY", "").encode()
#: ONE ROOM PER REQUEST. CHAT_ROOM used to be handed to every caller, so every
#: request shared one transcript and one spool -- a GM's reply went to whichever
#: session in the room drained the spool first. Once a caller has filed a ticket
#: (their `req_no` exists) the 0x801 names `<prefix><req_no:03d>` instead, e.g.
#: `#gmcall004`; before that they still get CHAT_ROOM. The request counter
#: survives restarts (gm-state.txt), so a room name is never reused.
#: WARNING: The prefix must keep starting with gmchat.PREFIX ("#gm") or authserv will
#: not treat the room as GM chat. Empty = the old single shared room.
ROOM_PER_REQUEST = os.environ.get("POL_GMD_ROOM_PER_REQUEST", "#gmcall").encode()
TICKET_DIR = os.environ.get("POL_GMD_TICKET_DIR", "/data/gm-calls")
#: The 0x102 body is 0x1E0 bytes (its own +0x00 says so); the datagram after it
#: is padding and the checksum, which are not part of the ticket.
TICKET_BODY_LEN = 0x1E0
#: The operator's live desk, written by the admin panel (or tools/gmctl.py) and
#: read here on every 0x801. A FILE, for the same reason a GM chat line is a file
#: (see gmchat.py): gmd has no inbound API, adding a control port would be a new
#: lane to secure, and every container already shares /data. Absent = nothing is
#: overridden and this service behaves exactly as it did before it existed.
CONTROL_PATH = os.path.join(TICKET_DIR, "gm-control.json")
#: What gmd is actually serving right now, written FOR the panel so it can show
#: the effective values rather than re-deriving the precedence rules and drifting
#: out of step with them. Purely informational; nothing reads it back.
SERVING_PATH = os.path.join(TICKET_DIR, "gm-serving.json")
#: The desk's own status per ticket (open / answered / closed), written by the
#: admin panel. gmd only reads it.
TICKET_STATE_PATH = os.path.join(TICKET_DIR, "gm-tickets.json")

#: THE SERVER HOLDS THE TICKET (app.dll, read 2026-09-28). 0x801 flags bit
#: 0x02 means "there is an open request for you": with it CLEAR, a client that
#: still has a call (mode != 0) resets itself on the next 0x801 -- deletes
#: gmtool/reserve, clears Request No. and deletes GmCall.bin, which is what
#: every title reads for its GM Call notice (0x4ab0425 -> 0x4aaf23d(0) ->
#: 0x4ab1e80). With it SET and no local call (mode 0) the client REBUILDS the
#: request from our stage-2 0x201 body (polcore state 7 copies it to
#: 0x4dbd398): Request No. at body +0x04, flags at +0x34 (0x20 -> mode 2),
#: subject at +0x58, text at +0x98. With it set, Submit never sends a new
#: 0x102 and Confirm/Cancel sends 0x1101 instead of cancelling locally.
#:
#: The caller is recognised by the 0x101's session dwords, which are the
#: logged-in handle's 64-bit id: ticket #4's dwords 0x25_3953FBA4 are
#: handle Bluebell's stored client_guid (checked on prod 2026-09-28).
#: POL_GMD_HOLD_TICKETS=0 restores the old behaviour (bit never set).
HOLD_TICKETS = os.environ.get("POL_GMD_HOLD_TICKETS", "1") != "0"
#: START IS PER CALLER. Flags 0x20 tells a client "YOUR reserved GM chat is
#: ready": it saves mode 2 to gmtool/reserve, shows "GM chat is ready.", lights
#: the home-page GM button and, in Dirge of Cerberus, prints "A GM has requested
#: to speak with you". Served desk-wide (the old on/off-duty 0x60/0x20) it told
#: every caller that. Now it is set only for a caller whose held ticket the desk
#: has KNOCKED (gm-tickets.json `knocked_at`), and 0x40 (Join, an invitation into
#: someone else's chat) is not offered at all yet. POL_GMD_KNOCK_ONLY=0 restores
#: the desk-wide flags.
KNOCK_ONLY = os.environ.get("POL_GMD_KNOCK_ONLY", "1") != "0"
FLAG_START, FLAG_JOIN = 0x20, 0x40
TICKET_RE = re.compile(r"gm-\d{8}T\d{6}-(\d+)\.json")
#: Stage-2 0x201 fields the client copies into its request record, as MESSAGE
#: offsets (body + 0x18).
S2_REQ, S2_CONTENT, S2_ISSUE, S2_FLAGS = 0x1C, 0x48, 0x4A, 0x4C
S2_SUBJ, S2_SUBJ_LEN, S2_TEXT, S2_TEXT_LEN = 0x70, 0x40, 0xB0, 0x150
BIT_HELD = 0x02


#: THE RSA SESSION KEY REACHES GM CALL. When authserv sends a login the
#: RSA-wrapped session key (POL_AUTH_RSA), polcore keeps it (0x0386a858) and
#: cft_1217 re-keys the GM per-slot context with it right after stage 1:
#: FUN_037ce080(ctx, cft_1165(0), 8). Under K=0 that key is zero, which is
#: the "zero re-key" every reply here is built on. Under RSA it is not, so the
#: client cannot read a zero-keyed 0x201; it sends back its receive buffer
#: (our reply decrypted under its key) until we answer under the right one.
#: authserv records each key it issues under the client address
#: (remember_session_key) as the fast path; Gmd.find_session_key has the rest.
SESSION_KEYS_TTL = 12 * 3600
SESSION_KEYS_KEEP = 8


def _kv():
    try:
        from polcore import kv
        return kv
    except Exception:
        return None


def remember_session_key(peer_ip, key):
    """authserv: record an RSA session key it just sent to `peer_ip`. Never raises."""
    kv = _kv()
    if kv is None or not peer_ip or not key:
        return
    try:
        k = "gm:sesskeys:" + peer_ip
        keys = [h for h in (kv.get_json(k) or []) if h != bytes(key).hex()]
        keys = (keys + [bytes(key).hex()])[-SESSION_KEYS_KEEP:]
        kv.set_json(k, keys, ttl=SESSION_KEYS_TTL)
    except Exception:
        pass


def session_keys(peer_ip):
    """The session keys issued to `peer_ip`, newest first. Never raises."""
    kv = _kv()
    if kv is None or not peer_ip:
        return []
    try:
        return [bytes.fromhex(h) for h in reversed(kv.get_json("gm:sesskeys:" + peer_ip) or [])]
    except Exception:
        return []


def session_key_blobs(key):
    """The GM key blob for one session key: the 8 bytes as authserv feeds
    Blowfish, then zeros (FUN_037ce080 with length 8). Only the low 14 bits of
    the first little-endian dword survive the derivation. The byte-reversed
    order is kept as a second guess; the brute force covers both anyway."""
    return [bytes(key) + b"\0" * 8, bytes(key)[::-1] + b"\0" * 8]


def session_guid(ses):
    """The caller's 64-bit handle id, from the 0x101 session dwords."""
    return ((ses.dwB & 0xFFFFFFFF) << 32) | (ses.dwA & 0xFFFFFFFF)


def _ticket_state(path=None):
    import json
    try:
        with open(path or TICKET_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def open_ticket_for(guid, req_no=0, ticket_dir=None):
    """(name, record) of the newest OPEN ticket this caller holds, or None.

    A ticket is open until the desk closes it or the player cancels it. Matched
    on the caller's handle id; `req_no` also matches a ticket filed in this
    same session before the id was recorded on tickets.
    """
    import json
    d = ticket_dir or TICKET_DIR
    try:
        names = sorted((n for n in os.listdir(d) if TICKET_RE.fullmatch(n)),
                       reverse=True)
    except OSError:
        return None
    state = _ticket_state(os.path.join(d, "gm-tickets.json"))
    for name in names:
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        # By the caller's handle id ONLY. A request number is per session and
        # must never be enough to hand someone a ticket.
        if not guid or rec.get("guid") != guid:
            continue
        tid = name[:-len(".json")]
        if rec.get("cancelled_at") or (state.get(tid) or {}).get("status") == "closed":
            return None                # their newest ticket is finished
        rec = dict(rec, _knocked=(state.get(tid) or {}).get("knocked_at"))
        return name, rec
    return None


def waiting_requests(ticket_dir=None):
    """{handle id: request number} of every caller still waiting for the desk:
    their newest ticket is open (not closed, not cancelled) and not yet
    knocked. One entry per caller, however many sessions they have open."""
    import json
    d = ticket_dir or TICKET_DIR
    try:
        names = sorted((n for n in os.listdir(d) if TICKET_RE.fullmatch(n)),
                       reverse=True)
    except OSError:
        return {}
    state = _ticket_state(os.path.join(d, "gm-tickets.json"))
    seen, out = set(), {}
    for name in names:
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        guid = rec.get("guid") if isinstance(rec, dict) else None
        if not guid or guid in seen:
            continue
        seen.add(guid)                     # only their newest ticket counts
        st = state.get(name[:-len(".json")]) or {}
        if rec.get("cancelled_at") or st.get("status") == "closed" or st.get("knocked_at"):
            continue
        out[guid] = int(rec.get("request_no") or 0)
    return out


def invite_for(guid, ticket_dir=None):
    """The room of an open request whose desk state invites this caller's
    handle id (gm-tickets.json `invited`: [{"guid": ...}]), or None."""
    import json
    d = ticket_dir or TICKET_DIR
    if not guid:
        return None
    state = _ticket_state(os.path.join(d, "gm-tickets.json"))
    for tid, st in state.items():
        if not isinstance(st, dict) or st.get("status") == "closed":
            continue
        if not any(isinstance(i, dict) and int(i.get("guid") or 0) == guid
                   for i in st.get("invited") or []):
            continue
        try:
            with open(os.path.join(d, tid + ".json"), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("cancelled_at") or not rec.get("room"):
            continue
        return str(rec["room"]).encode("latin1", "replace")
    return None


def fill_held_ticket(m, rec, flags):
    """Write a held ticket into a stage-2 0x201, where the client rebuilds its
    request from. The content id / issue pair at body +0x30/+0x32 mirrors the
    0x102's own +0x04/+0x06 and is inferred, not proven."""
    def put(off, n, text):
        raw = str(text or "").encode("cp932", "replace")[:n - 1]
        m[off:off + n] = raw + b"\0" * (n - len(raw))
    struct.pack_into("<I", m, S2_REQ, int(rec.get("request_no") or 0))
    struct.pack_into("<HH", m, S2_CONTENT, int(rec.get("content_id") or 0) & 0xFFFF,
                     int(rec.get("issue") or 0) & 0xFFFF)
    struct.pack_into("<I", m, S2_FLAGS, (flags & 0x60) | BIT_HELD)
    put(S2_SUBJ, S2_SUBJ_LEN, rec.get("subject"))
    put(S2_TEXT, S2_TEXT_LEN, rec.get("body"))

#: cft_1217_udp's key blob one, permuted a8,ac,a0,a4 on the way into the buffer.
KEY16 = struct.pack("<4I", 0xBC5224A2, 0x2B61B926, 0xA0A0D48A, 0xB0FEB355)
#: key blob two, keyed into the GLOBAL context by cft_1216. No permutation here.
KEY16_B = struct.pack("<4I", 0xC7D8935A, 0x00000000, 0xBC5224A2, 0x2B61B926)
KEY_ZERO = b"\0" * 16

IDLE_S = 300
MAX_SESS = 16
#: The client retransmits at 2s, 4s, 8s, 16s and then gives up, so anything
#: repeated later than this is a new request that merely looks the same.
RETRY_WINDOW_S = 20
#: Types whose high byte puts them in the echo-EXEMPT family (`0x5xx`, `0x6xx`,
#: `0x17xx`). These take no part in sequence numbering, so consecutive ones are
#: byte-identical BY DESIGN and must never be read as a retransmission -- the
#: `0x501` keepalive of a perfectly happy client is one every 40 seconds.
KEEPALIVE = (0x5, 0x6, 0x17)

#: The 0x801's two room blocks live PAST the 0x48 fixed length, at message +0x48
#: and +0x8a: 0x32 of name then 0x10 of key. See gmserver.cpp for the derivation.
R_ROOM_A, R_KEY_A, R_ROOM_B, R_KEY_B = 0x48, 0x7A, 0x8A, 0xBC
R_NAME_LEN, R_KEY_LEN, LEN_ROOMS = 0x32, 0x10, 0xCC


def log(msg):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"{ts}Z [gmd] {msg}", flush=True)


# --------------------------------------------------------------------- control
#: The one place the precedence rules live. `admin.py` imports THIS rather than
#: reimplementing them, so the panel cannot tell an operator something different
#: from what the client is being told -- the two-values-for-one-setting failure
#: this project keeps paying for.
#: `duty` is TRI-STATE and that is the whole point:
#:   None   nobody has ever touched this -- fall back to POL_GMD_STATUS_FLAGS
#:   True   an operator is at the desk, until `on_duty_until` passes
#:   False  an operator has explicitly signed off
#: Collapsing the last two into one value is a bug this had: signing off went
#: back to the env default, which is 0x60 -- so "off duty" still told callers to
#: come in, and the button appeared to do nothing.
DEFAULT_CONTROL = {"flags": None, "queue": None, "duty": None,
                   "on_duty_until": None, "by": None, "at": None}


def read_control(path=None):
    """The operator's desk, or the all-None default. NEVER raises.

    A missing or torn file must not take the GM band down with it: this runs on
    the reply path of every 0x801, and an unreadable control file has to mean
    "nobody has said anything", which is the state the server ran in for its
    whole life before this existed.
    """
    import json
    try:
        with open(path or CONTROL_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_CONTROL)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CONTROL)
    out = dict(DEFAULT_CONTROL)
    for k in ("flags", "queue"):
        try:
            out[k] = None if raw.get(k) is None else int(raw[k])
        except (TypeError, ValueError):
            out[k] = None
    try:
        out["on_duty_until"] = (None if raw.get("on_duty_until") is None
                                else float(raw["on_duty_until"]))
    except (TypeError, ValueError):
        out["on_duty_until"] = None
    out["duty"] = None if raw.get("duty") is None else bool(raw["duty"])
    out["by"], out["at"] = raw.get("by"), raw.get("at")
    # Per-GM claims (gmduty.py). Kept as read; gmduty filters the expired ones.
    out["gms"] = raw.get("gms") if isinstance(raw.get("gms"), dict) else {}
    return out


def write_control(ctl, path=None):
    """Replace the desk file atomically -- gmd reads it from another process."""
    import json
    p = path or CONTROL_PATH
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctl, f, indent=1)
    os.replace(tmp, p)
    return ctl


def on_duty(ctl, now=None):
    """True while an operator's claim to be at the desk is still fresh.

    It EXPIRES on purpose. An operator who closes the panel, loses power or
    simply forgets is the normal case, and a sticky "a GM is available" is worse
    than none: it invites a caller into a room nobody is in. The panel re-arms it
    while it is open, so the expiry is only ever reached by going away.
    """
    now = now or time.time()
    for c in (ctl.get("gms") or {}).values():
        try:
            if float(c.get("until") or 0) > now:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    if not ctl.get("duty") or not ctl.get("on_duty_until"):
        return False
    return now < ctl["on_duty_until"]


def effective_flags(ctl, now=None):
    """The 0x801 status flags, and WHY -- returned together so the panel and the
    log can both say which rule won rather than just quoting a number."""
    if ctl.get("flags") is not None:
        return ctl["flags"] & 0xFFFFFFFF, "pinned by the operator"
    if ctl.get("duty") is not None:
        if on_duty(ctl, now):
            return FLAGS_ON_DUTY, "a GM is on duty"
        if ctl["duty"]:
            return FLAGS_OFF_DUTY, "off duty (the on-duty claim expired)"
        return FLAGS_OFF_DUTY, "off duty (signed off)"
    return STATUS_FLAGS, "POL_GMD_STATUS_FLAGS (nobody has said otherwise)"


def room_for(ses):
    """The chat room this caller is told to join. See ROOM_PER_REQUEST."""
    if ROOM_PER_REQUEST and CHAT_ROOM and ses is not None and ses.req_no:
        return (ROOM_PER_REQUEST + b"%03d" % ses.req_no)[:R_NAME_LEN - 1]
    return CHAT_ROOM


def checksum(buf, length):
    """FUN_037cdcd0. `length` is the message's own length field, not the wire one."""
    b = bytearray(buf)
    b[0x14:0x18] = b"\xff\xff\xff\xff"
    words = (length + 3) // 4
    if len(b) < words * 4:
        b += b"\0" * (words * 4 - len(b))
    s = sum(struct.unpack_from("<%dI" % words, bytes(b), 0)) & 0xFFFFFFFF
    out = bytearray(buf)
    struct.pack_into("<I", out, 0x14, s)
    return bytes(out)


def wire_len(n):
    return (n & ~7) + 8            # FUN_037cdca0


def fixed_len(t):
    """FUN_037cdf60. Anything past this in the declared length is a text tail."""
    return {0x101: 0x48, 0x801: 0x48, 0x102: 0x1F8, 0x201: 0x200, 0x901: 0x230,
            0xA01: 0x210, 0xB01: 0x210, 0xC01: 0x210, 0xD01: 0x210, 0xF01: 0xC0,
            0x202: 0x40, 0x701: 0x40, 0x1001: 0x40, 0x1301: 0x40, 0x1601: 0x40,
            0xFF01: 0x40}.get(t, 0x18)


class Session:
    __slots__ = ("sctx", "gctx", "zctx", "sctx_ready", "dwA", "dwB",
                 "echoA", "echoC", "peer", "last_seen", "req_no", "idx",
                 "last_request", "last_request_at", "last_reply", "held",
                 "invite")

    def __init__(self, idx, sctx):
        self.idx = idx
        self.sctx = sctx
        self.gctx = _crypto().GmContext(KEY16_B)
        self.zctx = _crypto().GmContext(KEY_ZERO)
        self.sctx_ready = False
        self.dwA = self.dwB = self.echoA = self.echoC = 0
        self.peer = None
        self.last_seen = time.time()
        self.req_no = 0
        #: The open ticket this caller holds, as (name, record), or None.
        #: Looked up again on every 0x801, so a close on the desk lands on the
        #: next poll.
        self.held = None
        #: The room of an open request this caller was INVITED into (Join),
        #: or None. Looked up again on every 0x801, like `held`.
        self.invite = None
        #: The last request we read, when it arrived, and the exact ciphertext we
        #: answered it with. The client repeats a datagram verbatim when our answer
        #: does not satisfy it (2s, 4s, 8s, 16s, then it gives up), so an identical
        #: plaintext arriving INSIDE that window is a retransmission and the honest
        #: response is the same bytes again.
        #:
        #: WARNING: Identical bytes alone are NOT enough. The `0x501` keepalive is a bare
        #: header with a zero echo, so every one of them is byte-identical to the
        #: last -- a healthy client parked in the queue sends one every 40s for
        #: ever. Judging on content alone labelled that steady state "RETRANSMIT,
        #: our answer was not accepted", which is precisely backwards.
        self.last_request = None
        self.last_request_at = 0.0
        self.last_reply = None


class Gmd:
    def __init__(self):
        self.sessions = []
        self.state_path = os.path.join(TICKET_DIR, "gm-state.txt")
        self.sess_path = os.path.join(TICKET_DIR, "gm-sessions.json")
        #: The control file as of the datagram being answered. Re-read once per
        #: 0x801 rather than per field, so the flags and the queue count in one
        #: reply cannot come from two different versions of it.
        self.control = None
        self.control_seen = None

    # *** A RESTART USED TO KILL A LIVE CALL, AND NO LONGER NEEDS TO. *** The old
    # note here read "cipher contexts deliberately do NOT persist: a restart has
    # already broken lockstep with the client, so losing the call is honest."
    # That was true only under the stateful-cipher model. There is no lockstep:
    # the IV re-seeds per datagram, so ANY datagram can be opened at any time by
    # trying the three keys. The only thing a restart really loses is the SESSION
    # DWORDS, and those are four bytes each -- so they are written down.
    #
    # This is not hypothetical tidying. Restarting gmd under a live session on
    # 2026-08-17 cost the client its call: keepalives went unanswered, and it
    # raised POL-0685 (row 685, "an inconsistency arose in the exchange with the
    # GM server") and suspended itself to mode 3, which greys out the chat box.
    def save_sessions(self):
        import json
        try:
            os.makedirs(TICKET_DIR, exist_ok=True)
            with open(self.sess_path, "w") as f:
                json.dump([{"peer": s.peer, "dwA": s.dwA, "dwB": s.dwB,
                            "req_no": s.req_no, "ready": s.sctx_ready,
                            "room": room_for(s).decode("latin1") or None,
                            "at": s.last_seen} for s in self.sessions], f)
        except OSError as e:
            log(f"** could not persist the session table: {e}")

    def publish_serving(self, flags, why, queue):
        """Say what this service is CURRENTLY telling callers, for the panel.

        Written rather than recomputed on the panel side because the precedence
        rules live here; a second implementation would eventually disagree with
        this one and the operator would be reading a number the client never saw.
        """
        import json
        try:
            os.makedirs(TICKET_DIR, exist_ok=True)
            tmp = SERVING_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"flags": flags, "flags_why": why, "queue": queue,
                           "join": bool(flags & 0x40), "start": bool(flags & 0x20),
                           "room": CHAT_ROOM.decode("latin1") or None,
                           "chat_port": CHAT_PORT,
                           "at": time.time()}, f, indent=1)
            os.replace(tmp, SERVING_PATH)
        except OSError:
            pass                       # informational only; never fail a reply

    def load_sessions(self):
        import json
        try:
            with open(self.sess_path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        for r in rows:
            if now - r.get("at", 0) > IDLE_S:
                continue
            s = Session(len(self.sessions), _crypto().GmContext(KEY16))
            s.peer = tuple(r["peer"]) if r.get("peer") else None
            s.dwA, s.dwB, s.req_no = r["dwA"], r["dwB"], r["req_no"]
            s.sctx_ready, s.last_seen = r["ready"], r["at"]
            self.sessions.append(s)
        if self.sessions:
            log(f"resumed {len(self.sessions)} session(s) across a restart -- "
                f"a live call survives this now, which it did not before")

    # -- request numbers survive a restart; a caller told "request 4" must not
    # -- then see 1.
    def next_request(self):
        n = 1
        try:
            with open(self.state_path) as f:
                n = int(f.read().strip() or "1") or 1
        except (OSError, ValueError):
            pass
        try:
            os.makedirs(TICKET_DIR, exist_ok=True)
            with open(self.state_path, "w") as f:
                f.write(str(n + 1) + "\n")
        except OSError as e:
            log(f"** could not persist the request counter: {e}")
        return n

    def resolve(self, raw, peer):
        """Find the session this datagram belongs to.

        Returns (session, plaintext, opened_global, is_retransmit), or
        (None, None, False, False) when nothing opens it.

        CONFIDENTIALITY. Every GM key is a constant shared by all clients, so
        "whose context opens it" says nothing about WHO sent a datagram. This
        used to match on that alone, and on 2026-09-28 every caller landed on
        session #0 and inherited the previous caller's request number, room and
        held ticket. A session is now chosen by identity only:
          * a stage-1 0x101 (key blob one) belongs to a session from the SAME
            address and port with the SAME session dwords (the caller's handle
            id), and otherwise starts a NEW session;
          * a stage-2 0x101 (key blob two, from a new port) belongs to the
            session with the same dwords;
          * anything else only to a session from the same address and port.
        """
        def _type(pt):
            return struct.unpack_from("<H", pt, 6)[0]

        def _again(s, pt):
            return (pt == s.last_request
                    and time.time() - s.last_request_at < RETRY_WINDOW_S
                    and (_type(pt) >> 8) not in KEEPALIVE)

        mine = sorted((x for x in self.sessions if x.peer == peer),
                      key=lambda x: -x.last_seen)
        # 1. stage 1
        pt = _crypto().GmContext(KEY16).decrypt(raw)
        if pt[:4] == b"MAG?" and _type(pt) == 0x101:
            dw = struct.unpack_from("<II", pt, 0x20)
            s = next((x for x in mine if (x.dwA, x.dwB) == dw), None)
            if s is not None:
                again = _again(s, pt)
                log(f"session #{s.idx}: stage 1 again from the same caller"
                    + (" -- RETRANSMIT" if again else ""))
                return s, pt, False, again
            return self.new_session(pt, "a fresh 0x101 under key blob one")
        # 2. stage 2
        pt = _crypto().GmContext(KEY16_B).decrypt(raw)
        if pt[:4] == b"MAG?" and _type(pt) == 0x101:
            dw = struct.unpack_from("<II", pt, 0x20)
            cands = sorted((x for x in self.sessions
                            if x.sctx_ready and (x.dwA, x.dwB) == dw),
                           key=lambda x: -x.last_seen)
            if cands:
                s = cands[0]
                again = _again(s, pt)
                log(f"session #{s.idx}: stage 2 for handle {session_guid(s):#x}"
                    + (" -- RETRANSMIT" if again else ""))
                return s, pt, True, again
            log("** stage 2 with no stage 1 behind it (unknown session dwords) "
                "-- not answering")
            return None, None, False, False
        # 3. everything else: this caller's own session only
        for s in mine:
            pt = s.zctx.decrypt(raw)
            if pt[:4] == b"MAG?":
                again = _again(s, pt)
                log(f"session #{s.idx}, opened with its reply context"
                    + (" -- RETRANSMIT, our answer was not accepted" if again else ""))
                return s, pt, False, again
        # A client that logged in under POL_AUTH_RSA re-keyed its per-slot
        # context with its auth session key after stage 1 (see
        # remember_session_key).
        got = self.find_session_key(raw, peer, mine)
        if got:
            return got
        # Last chance: a mid-call client whose session we lost entirely. Adopt it
        # only for the echo-exempt keepalives, whose answers carry no session
        # data -- nothing about anyone's request is ever sent on this path.
        pt = _crypto().GmContext(KEY_ZERO).decrypt(raw)
        if pt[:4] != b"MAG?" or (_type(pt) >> 8) not in KEEPALIVE:
            return None, None, False, False
        s = Session(len(self.sessions), _crypto().GmContext(KEY16))
        self.sessions.append(s)
        log(f"ADOPTED session #{s.idx} from a zero-keyed keepalive -- we lost "
            f"its state, so only keepalives can be answered honestly")
        return s, pt, False, False

    def new_session(self, pt, why):
        """A fresh session: no request, no ticket, no invitation, no room."""
        if len(self.sessions) >= MAX_SESS:
            stale = min(self.sessions, key=lambda s: s.last_seen)
            if time.time() - stale.last_seen <= IDLE_S:
                log(f"** all {MAX_SESS} session slots busy and none idle -- dropped")
                return None, None, False, False
            self.sessions.remove(stale)
        s = Session(len(self.sessions), _crypto().GmContext(KEY16))
        self.sessions.append(s)
        log(f"NEW session #{s.idx} ({why})")
        return s, pt, False, False

    def find_session_key(self, raw, peer, ordered):
        """The reply key of a client that logged in under POL_AUTH_RSA.

        After stage 1 the client decrypts everything with FUN_037ce080(ctx,
        session key, 8), which reads only the low 14 bits of the key's first
        little-endian dword: 16384 possible contexts. Two things identify it:
          * a client that could not read our 0x201 sends back its receive
            buffer, which is our reply decrypted under ITS key
            (cft_1218_udp case -1 resends that buffer), so the right context
            turns our last reply into exactly these bytes -- and then the
            answer is our last request, re-answered under that key;
          * any later message, e.g. after a gmd restart mid-call, opens as
            MAG? under it.
        authserv's recorded keys are tried first, then all 16384.
        """
        if not peer:
            return None
        ses = next((x for x in ordered if x.peer == peer), None)
        echo = ses and ses.last_reply and ses.last_request
        seen, cands = set(), []
        for key in session_keys(peer[0]):
            for blob in session_key_blobs(key):
                cands.append(blob[:4])
        if ses is not None:
            # ~0.25 s at worst, so only for an address already in a call
            cands += [struct.pack("<I", k) for k in range(0x4000)]
        for k4 in cands:
            k0 = struct.unpack("<I", k4)[0] & 0x3FFF
            if k0 in seen:
                continue
            seen.add(k0)
            ctx = _crypto().GmContext(struct.pack("<I", k0) + b"\0" * 12)
            if echo and ctx.decrypt(ses.last_reply[:8]) == raw[:8] \
                    and ctx.decrypt(ses.last_reply[:len(raw)]) == raw:
                ses.zctx = ctx
                log(f"session #{ses.idx}: {peer[0]} echoed our reply back "
                    f"decrypted under its auth SESSION key (POL_AUTH_RSA); "
                    f"answering its last request again under that key")
                return ses, ses.last_request, False, False
            if ctx.decrypt(raw[:8])[:4] == b"MAG?":
                pt = ctx.decrypt(raw)
                if ses is None:
                    ses = Session(len(self.sessions), _crypto().GmContext(KEY16))
                    self.sessions.append(ses)
                ses.zctx = ctx
                log(f"session #{ses.idx}: opened with {peer[0]}'s auth "
                    f"SESSION key (POL_AUTH_RSA); replies now use it")
                return ses, pt, False, False
        return None

    # ------------------------------------------------------------------ replies
    def redirect(self, ses, echoA, echoC, our_ip):
        """Stage 1: the 0x201 that moves the client to our chat endpoint.

        WARNING: The endpoint IP is HOST order in both fields. Network order silently
        redirects the client to the reversed address, and the symptom is
        indistinguishable from a rejected reply.
        """
        m = bytearray(0x400)
        m[0:4] = b"MAG?"
        m[5] = 0x0A
        struct.pack_into("<HHHI", m, 6, 0x0201, 0x200, echoA, echoC)
        struct.pack_into("<H", m, 0x1A, CHAT_PORT)                  # body +0x02
        struct.pack_into("<I", m, 0x24, struct.unpack("!I", socket.inet_aton(our_ip))[0])
        return checksum(bytes(m[:wire_len(0x200)]), 0x200), 0x200

    def stage2(self, ses, echoA, echoC):
        """Stage 2 wants the SESSION DWORDS echoed at body +0x10/+0x14.

        State 7, not state 2: it no longer wants a redirect, and -0x2a3
        (POL-0675) has exactly one source, which is this check. Re-sending
        stage 1's redirect is a perfectly valid 0x201 with zeros in the two
        fields being tested, which is what produced that error.
        """
        m = bytearray(0x400)
        m[0:4] = b"MAG?"
        m[5] = 0x0A
        struct.pack_into("<HHHI", m, 6, 0x0201, 0x200, echoA, echoC)
        struct.pack_into("<II", m, 0x28, ses.dwA, ses.dwB)
        if ses.held:
            fill_held_ticket(m, ses.held[1],
                             self.caller_flags(ses, effective_flags(read_control())[0]))
        return checksum(bytes(m[:wire_len(0x200)]), 0x200), 0x200

    def generic(self, ses, rtype, echoA, echoC, body_edit=None):
        fixed = fixed_len(rtype)
        rooms = LEN_ROOMS - fixed if (rtype == 0x801 and CHAT_ROOM) else 0
        total = fixed + rooms
        m = bytearray(max(total, 0x40) + 16)
        m[0:4] = b"MAG?"
        m[5] = 0x0A
        struct.pack_into("<HHHI", m, 6, rtype, total, echoA, echoC)
        if total >= 0x30:
            struct.pack_into("<II", m, 0x28, ses.dwA, ses.dwB)
        if body_edit:
            body_edit(m)
        # A ROOM IS ONLY EVER NAMED TO SOMEONE WHO HAS ONE: their own held
        # request, or an invitation. Nobody is handed a shared room.
        if rooms and ses is not None and (ses.held or ses.invite):
            room = room_for(ses)[:R_NAME_LEN - 1]
            # Block A is the caller's own chat (Start), block B the one Join
            # enters: an invitation's room when there is one.
            join = (ses.invite if ses is not None and ses.invite else room)[:R_NAME_LEN - 1]
            m[R_ROOM_A:R_ROOM_A + len(room)] = room
            m[R_ROOM_B:R_ROOM_B + len(join)] = join
            if CHAT_KEY:
                m[R_KEY_A:R_KEY_A + len(CHAT_KEY)] = CHAT_KEY[:R_KEY_LEN - 1]
                m[R_KEY_B:R_KEY_B + len(CHAT_KEY)] = CHAT_KEY[:R_KEY_LEN - 1]
        return checksum(bytes(m[:wire_len(total)]), total), total

    # -- the ticket the caller holds ----------------------------------------- #
    def refresh_held(self, ses):
        """Look up the caller's open ticket again. A ticket that was held and
        is now closed or cancelled drops the request number with it, so the
        caller leaves the queue and its room."""
        if not HOLD_TICKETS or not (ses.dwA or ses.dwB):
            return
        inv = invite_for(session_guid(ses))
        if inv != ses.invite:
            log(f"    {'invited into ' + inv.decode('latin1') if inv else 'invitation withdrawn'}"
                f" for handle {session_guid(ses):#x}: Join {'offered' if inv else 'off'}")
        ses.invite = inv
        was = ses.held
        ses.held = open_ticket_for(session_guid(ses), ses.req_no)
        if ses.held:
            ses.req_no = int(ses.held[1].get("request_no") or ses.req_no)
            if not was or was[0] != ses.held[0]:
                log(f"    holding ticket {ses.held[0]} (request #{ses.req_no}) "
                    f"for handle {session_guid(ses):#x}: bit 0x02 set")
        else:
            if was:
                log(f"    ticket {was[0]} is finished: bit 0x02 cleared, the client "
                    f"resets its call on this 0x801")
            ses.req_no = 0

    @staticmethod
    def caller_flags(ses, flags):
        """The Start/Join bits for THIS caller (see KNOCK_ONLY)."""
        if not KNOCK_ONLY:
            return flags
        flags &= ~(FLAG_START | FLAG_JOIN)
        if ses is not None and ses.held and ses.held[1].get("_knocked"):
            flags |= FLAG_START
        if ses is not None and ses.invite:
            flags |= FLAG_JOIN
        return flags

    def cancel_held(self, ses):
        """The player cancelled from Confirm/Cancel (0x1101). Recorded on the
        ticket itself -- gmd owns those files; gm-tickets.json is the panel's."""
        import json
        if not ses.held:
            log("    0x1101 with no held ticket -- answered, nothing to cancel")
            return
        name, rec = ses.held
        # A DESK CLOSE ALSO ARRIVES AS A 0x1101: the close knock makes the
        # Viewer end its call, which it reports the same way as a player's
        # Cancel. A ticket the desk has already closed is not the player's
        # cancel, and must not be recorded as one.
        st = _ticket_state(os.path.join(TICKET_DIR, "gm-tickets.json"))
        if (st.get(name[:-len(".json")]) or {}).get("status") == "closed":
            log(f"    0x1101 after the desk closed {name} -- the call ended, not cancelled")
            ses.held, ses.req_no = None, 0
            return
        rec = dict(rec, cancelled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()))
        path = os.path.join(TICKET_DIR, name)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
            log(f"    ticket {name} CANCELLED by the player")
        except OSError as e:
            log(f"  ** could not mark {name} cancelled: {e}")
        ses.held, ses.req_no = None, 0

    def write_ticket(self, body, req_no, peer, guid=0):
        """The 0x102 is the whole ticket -- who, which title, category, subject,
        description. Filed as JSON into the directory the dashboard reads."""
        import json
        def s(off, n):
            return body[off:off + n].split(b"\0")[0].decode("cp932", "replace")
        rec = {
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_no": req_no,
            # The caller's handle id (the 0x101 session dwords). What a later
            # session is matched to this ticket by, and what the panel resolves
            # to a member when the handle field is empty (in-game calls).
            "guid": guid,
            "handle": s(0x40, 16),
            "content_id": struct.unpack_from("<H", body, 0x04)[0],
            "issue": struct.unpack_from("<H", body, 0x06)[0],
            "subject": s(0x50, 64),
            "body": s(0x90, 320),
            "peer": peer,
            # Recorded, not re-derived: the panel shows this room's transcript
            # for this request, and the naming rule may change later.
            "room": (ROOM_PER_REQUEST + b"%03d" % req_no).decode("latin1")
                    if (ROOM_PER_REQUEST and CHAT_ROOM)
                    else (CHAT_ROOM.decode("latin1") or None),
            # The whole body as it came. In-game GM Calls from Front Mission
            # Online arrive with +0x40 empty, issue 0 and subject "GM Call";
            # the raw 0x1E0 bytes are kept to show whether the title names its
            # caller some other way (a Content ID, a character name).
            "raw": bytes(body[:TICKET_BODY_LEN]).hex(),
        }
        # Nonzero bytes outside the decoded fields: the place to look first.
        extra = [o for o in range(0x10, 0x40) if o < len(body) and body[o]]
        if extra:
            log(f"  ticket body has bytes in the undecoded +0x10..+0x3F block at "
                f"{', '.join('+0x%02X' % o for o in extra[:24])}"
                f"{' ...' if len(extra) > 24 else ''}")
        try:
            os.makedirs(TICKET_DIR, exist_ok=True)
            name = time.strftime("gm-%Y%m%dT%H%M%S", time.gmtime()) + f"-{req_no}.json"
            with open(os.path.join(TICKET_DIR, name), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            log(f"  ticket #{req_no} from {rec['handle']!r} filed as {name}")
        except OSError as e:
            log(f"  ** could not file the ticket: {e}")
        return rec

    # --------------------------------------------------------------------- loop
    def serve(self):
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # NO SO_REUSEADDR. On Windows it lets a second socket bind a UDP port
        # another process already holds and datagrams go to only ONE of them --
        # a squatter ate every GM datagram for hours in 2026-08-15.
        sk.bind(("0.0.0.0", PORT))
        ctl = read_control()
        flags, why = effective_flags(ctl)
        log(f"listening on 0.0.0.0:{PORT}/udp  flags={flags:#x} ({why}) "
            f"queue={ctl['queue'] if ctl.get('queue') is not None else (QUEUE_OVERRIDE or 'live')} "
            f"room={CHAT_ROOM.decode() or '(none)'}"
            + (f", then {ROOM_PER_REQUEST.decode()}NNN per request"
               if ROOM_PER_REQUEST and CHAT_ROOM else ""))
        log(f"desk control file: {CONTROL_PATH}"
            + ("" if os.path.exists(CONTROL_PATH) else " (absent -- env defaults)"))
        self.load_sessions()
        while True:
            raw, peer = sk.recvfrom(4096)
            log(f"[<] {len(raw)}B from {peer[0]}:{peer[1]}")
            ses, pt, via_global, retransmit = self.resolve(raw, peer)
            if ses is None:
                log(f"** UNREADABLE ({len(raw)}B) -- no tracked context opened it; "
                    f"not answering, a reply on a desynchronised context cannot be "
                    f"read either")
                continue
            ses.peer, ses.last_seen = peer, time.time()
            self.save_sessions()
            ses.last_request, ses.last_request_at = pt, time.time()
            if retransmit:
                # Re-send the same bytes: the client is asking again, and the only
                # honest reading is that our answer was lost or refused. If it
                # keeps asking, `gmkeys.py` says which -- `msgbuf` (0x038639bc)
                # holds our reply as the client decrypted it, so `MAG?` there
                # means the cipher was right and the refusal is in the message.
                rtype, length = struct.unpack_from("<HH", pt, 6)
                log(f"    type={rtype:#05x} len={length:#x} (repeat)")
                if ses.last_reply:
                    sk.sendto(ses.last_reply, peer)
                    log(f"[>] re-sent the same {len(ses.last_reply)}B answer "
                        f"(last 8 = {ses.last_reply[-8:].hex()})")
                else:
                    log("    ** nothing cached to re-send")
                continue
            rtype, length = struct.unpack_from("<HH", pt, 6)
            echoA, echoC = struct.unpack_from("<HI", pt, 0x0A)
            log(f"    type={rtype:#05x} len={length:#x} echo +0x0A={echoA:#06x} "
                f"+0x0C={echoC:#010x}")

            if rtype == 0x101 and via_global and ses.sctx_ready:
                rep, n = self.stage2(ses, echoA, echoC)
                log(f"[>] STAGE-2 answer: 0x201 echoing {ses.dwA:08x} / "
                    f"{ses.dwB:08x} at body +0x10/+0x14")
            elif rtype == 0x101 and via_global:
                log("** stage 2, but this run never saw the stage-1 0x101, so the "
                    "session dwords are unknown. Answering would guarantee "
                    "POL-0675. Restart the GM call with gmd already up.")
                continue
            elif rtype == 0x101:
                ses.dwA, ses.dwB = struct.unpack_from("<II", pt, 0x20)
                ses.echoA, ses.echoC, ses.sctx_ready = echoA, echoC, True
                log(f"    session dwords: {ses.dwA:08x} / {ses.dwB:08x}")
                self.refresh_held(ses)
                our_ip = advertise_for(CHAT_IP or self.local_ip_for(peer[0]),
                                       peer[0])
                rep, n = self.redirect(ses, echoA, echoC, our_ip)
                log(f"[>] 0x201 redirect -> {our_ip}:{CHAT_PORT} (host order)")
            else:
                rtype_out = rtype + 0x100
                edit = None
                if rtype_out == 0x202:
                    if not ses.req_no:
                        ses.req_no = self.next_request()
                    rn = ses.req_no
                    edit = lambda m: struct.pack_into("<I", m, 0x1C, rn)
                    self.write_ticket(pt[0x18:], rn, f"{peer[0]}:{peer[1]}",
                                      guid=session_guid(ses))
                    self.refresh_held(ses)
                elif rtype_out == 0x1201:
                    # Confirm/Cancel with a held ticket: the player withdrew it.
                    # A bare 0x1201 is all cft_1251 checks for.
                    self.cancel_held(ses)
                elif rtype_out == 0x801:
                    # ONE read of the desk for this whole reply. Flags and queue
                    # that disagreed about which version of the file they came
                    # from would be a race nobody could reproduce.
                    self.control = read_control()
                    flags, why = effective_flags(self.control)
                    self.refresh_held(ses)
                    flags = (flags | BIT_HELD) if ses.held else (flags & ~BIT_HELD)
                    flags = self.caller_flags(ses, flags)
                    q = self.queue_depth(ses)
                    place = self.queue_place(ses)
                    stamp = (flags, q, place, why)
                    if stamp != self.control_seen:
                        self.control_seen = stamp
                        log(f"    serving flags={flags:#x} ({why}), "
                            f"queue={q}, place={place}"
                            + (f", set by {self.control['by']}"
                               if self.control.get("by") else ""))
                    self.publish_serving(flags, why, q)
                    def edit(m, q=q, place=place, flags=flags):
                        struct.pack_into("<H", m, 0x1A, q)         # body +0x02
                        struct.pack_into("<H", m, 0x1C, place)     # body +0x04
                        struct.pack_into("<I", m, 0x24, flags)     # body +0x0C
                rep, n = self.generic(ses, rtype_out, echoA, echoC, edit)
                log(f"[>] {rtype:#06x} -> {rtype_out:#06x} (total {n:#x})"
                    + (f" room {room_for(ses).decode('latin1')}"
                       if rtype_out == 0x801 and CHAT_ROOM else ""))

            w = wire_len(n)
            # The plaintext head is logged so a live `msgbuf` read (0x038639bc,
            # via gmkeys.py) can be compared to it field for field. That is the
            # comparison that found the IV bug: everything past the first 8 bytes
            # matched, which is a right key with a wrong starting block.
            ct = ses.zctx.encrypt(rep[:w])
            ses.last_reply = ct
            sk.sendto(ct, peer)
            log(f"    sent {len(ct)}B: plaintext {rep[:0x10].hex()}")

    def queue_depth(self, me):
        """`Users in queue` -- the callers actually waiting, not a literal 0.

        A caller is in the queue once their 0x202 filed a ticket (`req_no` is
        assigned there and nowhere else) and while their session is still live by
        the same IDLE_S the session table is pruned with. Both facts are already
        maintained for other reasons; nothing new is observed to produce this.

        WARNING: THE ONE JUDGEMENT CALL, and it is flagged rather than buried: SE's
        label is "Users in queue", which does not say whether the reader is
        counted in it. This EXCLUDES the caller being answered, so the number
        reads as "people ahead of you" -- the reading that makes 0 meaningful for
        the only person waiting. If a screenshot ever shows a lone caller being
        told 1, drop the `s is not me` test. `POL_GMD_QUEUE=<n>` pins it either
        way without a rebuild.
        """
        # Precedence: the operator's live pin, then the env pin, then the truth.
        # The operator wins over compose because they are the later, more
        # specific statement -- and unlike compose theirs can be taken back
        # without a restart, which is the whole point of the control file.
        ctl = self.control if self.control is not None else read_control()
        if ctl.get("queue") is not None:
            return ctl["queue"] & 0xFFFF
        if QUEUE_OVERRIDE:
            try:
                return int(QUEUE_OVERRIDE, 0) & 0xFFFF
            except ValueError:
                pass
        # Counted by REQUEST, one per caller (waiting_requests): counting live
        # sessions made one player with two open sessions two people, and the
        # screen said "0 of 2" to a caller alone in the queue (2026-09-29).
        mine = session_guid(me) if me is not None else 0
        n = sum(1 for g in waiting_requests() if g != mine)
        return min(n, 0xFFFF)

    def queue_place(self, me):
        """The caller's place in line, 1-based, for body +0x04; 0 once the desk
        has knocked (or with no open request).

        The screen reads "Currently <body +0x04> of <body +0x02 + 1>": with
        +0x04 left at zero it showed "0 of 2" while +0x02 was 1. Inferred from
        that one screen, not traced in the client."""
        waiting = waiting_requests()
        mine = waiting.get(session_guid(me)) if me is not None else None
        if not mine:
            return 0
        return min(1 + sum(1 for r in waiting.values() if 0 < r < mine), 0xFFFF)

    @staticmethod
    def local_ip_for(peer_ip):
        """Our own address on the route toward the client. A connected UDP socket
        sends nothing; it just makes the kernel pick the source address."""
        t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            t.connect((peer_ip, 9))
            return t.getsockname()[0]
        finally:
            t.close()


if __name__ == "__main__":
    Gmd().serve()
