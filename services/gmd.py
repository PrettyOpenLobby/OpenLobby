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
TICKET_DIR = os.environ.get("POL_GMD_TICKET_DIR", "/data/gm-calls")
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
    if not ctl.get("duty") or not ctl.get("on_duty_until"):
        return False
    return (now or time.time()) < ctl["on_duty_until"]


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
                 "last_request", "last_request_at", "last_reply")

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
        """Find the session this datagram belongs to, by TRIAL on clones.

        Returns (session, plaintext, opened_global, is_retransmit), or
        (None, None, False, False) when nothing opens it.
        """
        ordered = sorted(self.sessions,
                         key=lambda s: (s.peer != peer, -s.last_seen))
        for s in ordered:
            for name, ctx in (("per-slot ZERO", s.zctx), ("per-slot", s.sctx),
                              ("GLOBAL", s.gctx)):
                pt = ctx.decrypt(raw)
                if pt[:4] == b"MAG?":
                    again = (pt == s.last_request
                             and time.time() - s.last_request_at < RETRY_WINDOW_S
                             and (struct.unpack_from("<H", pt, 6)[0] >> 8) not in KEEPALIVE)
                    log(f"session #{s.idx}, opened with its {name} context"
                        + (" -- RETRANSMIT, our answer was not accepted" if again else ""))
                    return s, pt, name == "GLOBAL", again
        # Nothing matched. A fresh stage 1 is the only thing that legitimately
        # arrives with no session behind it, and it is always key blob one.
        pt = _crypto().GmContext(KEY16).decrypt(raw)
        if pt[:4] != b"MAG?":
            # Last chance: a mid-call client whose session we lost entirely (no
            # state file). Its traffic is ZERO-keyed. Adopt it only for the
            # echo-exempt keepalives, whose answers carry no session data -- a
            # 0x801 built with session dwords we do not have is how POL-0685
            # gets raised. Answering the keepalive keeps the call alive until
            # the client asks for something that needs the real state.
            pt = _crypto().GmContext(KEY_ZERO).decrypt(raw)
            if pt[:4] != b"MAG?" or (struct.unpack_from("<H", pt, 6)[0] >> 8) not in KEEPALIVE:
                return None, None, False, False
            s = Session(len(self.sessions), _crypto().GmContext(KEY16))
            self.sessions.append(s)
            log(f"ADOPTED session #{s.idx} from a zero-keyed keepalive -- we lost "
                f"its state, so only keepalives can be answered honestly")
            return s, pt, False, False
        if len(self.sessions) >= MAX_SESS:
            stale = min(self.sessions, key=lambda s: s.last_seen)
            if time.time() - stale.last_seen <= IDLE_S:
                log(f"** all {MAX_SESS} session slots busy and none idle -- dropped")
                return None, None, False, False
            self.sessions.remove(stale)
        s = Session(len(self.sessions), _crypto().GmContext(KEY16))
        self.sessions.append(s)
        log(f"NEW session #{s.idx} (a fresh 0x101 under key blob one)")
        return s, pt, False, False

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
        if rooms:
            m[R_ROOM_A:R_ROOM_A + len(CHAT_ROOM)] = CHAT_ROOM[:R_NAME_LEN - 1]
            m[R_ROOM_B:R_ROOM_B + len(CHAT_ROOM)] = CHAT_ROOM[:R_NAME_LEN - 1]
            if CHAT_KEY:
                m[R_KEY_A:R_KEY_A + len(CHAT_KEY)] = CHAT_KEY[:R_KEY_LEN - 1]
                m[R_KEY_B:R_KEY_B + len(CHAT_KEY)] = CHAT_KEY[:R_KEY_LEN - 1]
        return checksum(bytes(m[:wire_len(total)]), total), total

    def write_ticket(self, body, req_no, peer):
        """The 0x102 is the whole ticket -- who, which title, category, subject,
        description. Filed as JSON into the directory the dashboard reads."""
        import json
        def s(off, n):
            return body[off:off + n].split(b"\0")[0].decode("cp932", "replace")
        rec = {
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_no": req_no,
            "handle": s(0x40, 16),
            "content_id": struct.unpack_from("<H", body, 0x04)[0],
            "issue": struct.unpack_from("<H", body, 0x06)[0],
            "subject": s(0x50, 64),
            "body": s(0x90, 320),
            "peer": peer,
        }
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
            f"room={CHAT_ROOM.decode() or '(none)'}")
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
                    self.write_ticket(pt[0x18:], rn, f"{peer[0]}:{peer[1]}")
                elif rtype_out == 0x801:
                    # ONE read of the desk for this whole reply. Flags and queue
                    # that disagreed about which version of the file they came
                    # from would be a race nobody could reproduce.
                    self.control = read_control()
                    flags, why = effective_flags(self.control)
                    q = self.queue_depth(ses)
                    stamp = (flags, q, why)
                    if stamp != self.control_seen:
                        self.control_seen = stamp
                        log(f"    serving flags={flags:#x} ({why}), "
                            f"queue={q}"
                            + (f", set by {self.control['by']}"
                               if self.control.get("by") else ""))
                    self.publish_serving(flags, why, q)
                    def edit(m, q=q, flags=flags):
                        struct.pack_into("<H", m, 0x1A, q)         # body +0x02
                        struct.pack_into("<I", m, 0x24, flags)     # body +0x0C
                rep, n = self.generic(ses, rtype_out, echoA, echoC, edit)
                log(f"[>] {rtype:#06x} -> {rtype_out:#06x} (total {n:#x})")

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
        now = time.time()
        n = sum(1 for s in self.sessions
                if s is not me and s.req_no and now - s.last_seen <= IDLE_S)
        return min(n, 0xFFFF)

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
