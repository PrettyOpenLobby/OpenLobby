"""ChatSession: one client's auth-band connection and its cipher state."""
import os
import time
import threading
import sessioncrypt
from . import authnode



class ChatSession:
    """One live auth-band session channel -- a client we can PUSH lines to.

    Until this existed `_auth_session_reply` was a pure function: it could answer
    the caller and nothing else, so a JOIN could only ever report a room of one.
    Everything about rooms that needs a second person -- seeing them arrive, their
    name in NAMES/WHO, their chat -- needs a handle on the OTHER connection, which
    is what this is.

    Safe to send from another thread. The session cipher is OFB **re-keyed from
    this connection's IV for every line** (see the observe loop: `ofb_apply(P, S,
    iv, ...)` is called fresh per line, never chained), so lines are independent
    and the only thing that must be serialised is the socket write itself.

    EVERY WRITE TO THE SOCKET GOES THROUGH HERE -- including the owning
    connection's own. That was not true until 2026-08-13: this class locked its
    writes while the handler that owns the connection called `conn.sendall()`
    directly for the welcome burst, the keepalive PING and every reply in the
    observe loop. A lock only one writer takes is not a lock, so a broadcast
    landing between a partial send and its remainder interleaved two lines, and
    an OFB line whose bytes are split by another line's fails the trailing
    checksum the client verifies (frame_line) and is discarded. Dormant with one
    user; reachable exactly when the room registry starts doing its job.
    """

    def __init__(self, nick, srv, peer_ip, conn, P, S, iv, member=None):
        self.nick = nick if isinstance(nick, bytes) else str(nick).encode()
        self.srv = srv if isinstance(srv, bytes) else str(srv).encode()
        self.peer_ip = (peer_ip if isinstance(peer_ip, bytes)
                        else str(peer_ip).encode())
        self.conn, self.P, self.S, self.iv = conn, P, S, iv
        self.member = member
        self._lock = threading.Lock()
        self.alive = True
        #: Which launch and which client build this channel is (set by the
        #: auth hop), and whether a newer login KILLED it. See
        #: `_kill_duplicate_logins`: a killed channel's close is not a logout.
        self.sid = None
        self.client_sig = None
        self.killed_dup = False
        #: IRC away-ness, as this connection last declared it (`AWAY <text>` /
        #: bare `AWAY`). Read by `_who_here_flag` as the FALLBACK behind the 4:5
        #: status: `presence-is-afk` -- in this protocol presence is modelled as
        #: IRC AWAY, so a client that announces it here and never sends a 4:5
        #: still gets the right `G` in the room's 352.
        self.away = False
        #: *** LIVENESS IS NOT PARTICIPATION, AND NEITHER IS RESPONSIVENESS. ***
        #:
        #: `alive` says the socket is up. It says nothing about whether the
        #: client is taking part in this ROOM, and the two came apart in
        #: live testing on 2026-08-20: a Tetra Master client walked into the
        #: auction screen, hit the unanswered 0x21/0x23/0x25/0x2A/0x2C poll loop
        #: and never sent the PART it would have sent on
        #: leaving the room. It kept its seat in `#<title>R001`, and at 18:04:37
        #: `_reassign_owner_locked` handed it the room. The live report put it
        #: plainly: "Fox isn't in the room. He's in the auction."
        #:
        #: WARNING: AND "HAVE WE HEARD FROM IT" DOES NOT SEPARATE THEM. That was the
        #: first version of this and it was FALSIFIED by the very case it was
        #: written for: the hung client answers `PONG` on schedule and is busily
        #: sending class-A auction traffic. At the socket level it is a model
        #: citizen. Counting any inbound line would have handed it the room all
        #: the same.
        #:
        #: What separates them is the BAND. A client sitting on a room screen
        #: polls the room's roster on class **L** (`G<tag>L<DR>`) every one to
        #: three seconds. Measured over the same two clients, same minute:
        #:
        #:     port 36396 (in the room)     227 <DR> polls, last 3 s ago
        #:     port 49326 (in the auction)    0 <DR> polls, EVER
        #:     port 49312 (in the auction)    0 <DR> polls, EVER
        #:
        #: so this is when we last saw class-L traffic, and it is the only one of
        #: the three signals that answers the question actually being asked.
        self.last_heard = time.time()
        self.last_room_heard = 0.0

    def note_heard(self, room_band=False, when=None):
        """The client said something; `room_band` when it was class L."""
        now = time.time() if when is None else when
        self.last_heard = now
        if room_band:
            self.last_room_heard = now

    def in_room_recently(self, within=None):
        """Is this client actually sitting on a room screen?

        WARNING: Deliberately generous, and deliberately NOT used to evict anybody.
        The poll runs every 1-3 s, so the default window is many multiples of it
        and only ever separates a client that has NAVIGATED AWAY from one that
        is briefly busy. Whether `<DR>` pauses during an in-room sub-screen (the
        card shop, the table dialog) is NOT measured -- which is exactly why
        this only picks between candidates for a duty and never takes a seat
        away from anyone.
        """
        if within is None:
            within = float(os.environ.get("POL_ROOM_PRESENT_S", "60") or 60)
        return self.last_room_heard > 0 and (time.time() - self.last_room_heard) <= within

    def encode(self, lines, pad_override=None):
        """Lines -> the exact bytes that go on the wire (checksummed, encrypted).

        `pad_override` is for the game-notice path, which frames a NoPad line
        without frame_line's pad byte (see POL_GAME_NOTICE_PAD).
        """
        out = b""
        for l in lines:
            pad = b" "
            if isinstance(l, authnode.NoPad):
                pad = b"" if pad_override is None else pad_override
            out += sessioncrypt.ofb_apply(
                self.P, self.S, self.iv,
                sessioncrypt.frame_line(l, pad=pad)) + b"\r\n"
        return out

    def send(self, lines, pad_override=None):
        """Encrypt + write `lines`. Never raises: a dead peer must not take down
        whoever is broadcasting to it."""
        if not lines or not self.alive:
            return False
        try:
            return self.send_raw(self.encode(lines, pad_override))
        except OSError:
            self.alive = False
            return False

    def send_raw(self, blob):
        """Write already-encoded bytes under the SAME lock every other writer
        takes. This is the one door onto the socket -- the owning connection uses
        it too, which is the whole point (see the class note)."""
        if not blob:
            return True
        with self._lock:
            self.conn.sendall(blob)
        return True

    def __repr__(self):
        return f"<ChatSession {self.nick.decode('latin1')} {self.peer_ip.decode()}>"


def _sess_member_id(sess):
    """The member id behind a ChatSession, or None.

    `ChatSession.member` is the **`member` ROW**, not an id -- every other caller
    goes through `int(member["id"])`. Taking `int(sess.member)` raises
    `TypeError: int() argument must be ... not 'Row'`, which is exactly
    what `_group_op_nicks` did on every single call (seen in authserv.log against
    every JOIN and WHO on a group channel). It was caught and logged, so the only
    symptom was the operator flag silently falling back to join order -- the
    feature never ran once.
    """
    m = getattr(sess, "member", None)
    if m is None:
        return None
    try:
        return int(m["id"])
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return int(m)                    # already an id
    except (TypeError, ValueError):
        return None
