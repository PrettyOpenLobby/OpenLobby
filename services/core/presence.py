"""Presence registry: which members are online, logout grace, presence status payloads."""
import os
import socket
import struct
import threading
from srvcore import log
from .deps import accounts
from . import authcap, authnode, lobbysession, logingate, memberstatus, pushrecord



def _member_still_present(remaining, member_id):
    """Does `member_id` still hold a session among `remaining`?

    WARNING: THE GUARD ON THE RETIRE, AND THE REASON IT EXISTS. A launch holds SEVERAL
    connections at once -- redirect hops share one session id, which is why the
    deliberate-close bit in the channel handler must compare fingerprints before
    clearing its flag. Retiring the room record on ANY close therefore retires a
    member who is still standing in the room on another socket. Measured
    2026-08-20, the day the retire shipped: member 9 was DROPPED three times in
    one sitting while actively playing, and the chat roster we then served to the
    other player was missing HIMSELF, because one of his own hops had closed
    seconds earlier.

    Kept as a function purely so it can be asserted; `ROOMS.drop` hands the
    caller exactly this list.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    for sess in remaining or ():
        other = getattr(sess, "member_id", None)
        try:
            if other is not None and int(other) == mid:
                return True
        except (TypeError, ValueError):
            continue
    return False


class PresenceRegistry:
    """`member_id` -> the live session-channel ChatSessions for that member.

    This is what lets a login/logout/away change REACH the friends who are online
    right now. The friend-list screen shows presence, but the 2:3 list only
    carries the state AT FETCH TIME (login); to update it live the server has to
    push a line down each watching friend's own session channel, and to do that it
    needs a handle on those sockets keyed by who is behind them. That handle is
    this registry -- the presence analogue of ROOMS.

    A member can briefly hold more than one live session (a relaunch races the old
    socket's cleanup), so the value is a list, and `unregister` removes the exact
    ChatSession rather than the member.

    In-process and not persisted, for the same reason as RoomRegistry: "who is
    connected right now" cannot outlive the sockets. The DURABLE side of presence
    -- "has a live session row" -- already lives in `accounts.session`; this only
    tracks the push targets.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._by_member = {}             # member_id -> [ChatSession, ...]

    def register(self, member_id, sess):
        with self._lock:
            lst = self._by_member.setdefault(int(member_id), [])
            if sess not in lst:
                lst.append(sess)

    def unregister(self, member_id, sess):
        with self._lock:
            lst = self._by_member.get(int(member_id))
            if not lst:
                return
            if sess in lst:
                lst.remove(sess)
            if not lst:
                self._by_member.pop(int(member_id), None)

    def sessions_for(self, member_id):
        with self._lock:
            return list(self._by_member.get(int(member_id), []))

    def is_online(self, member_id):
        with self._lock:
            return bool(self._by_member.get(int(member_id)))

    def sessions_by_nick(self, nick):
        """Live sessions whose nick matches, for delivering a WHISPER.

        A whisper is a PRIVMSG addressed to a nick rather than a channel, so the
        only routing key on the wire is the nick -- there is no channel to look
        the target up in. A member can hold more than one live session, so this
        returns a list like `sessions_for` rather than a single session.
        """
        if isinstance(nick, str):
            nick = nick.encode()
        with self._lock:
            return [s for lst in self._by_member.values() for s in lst
                    if s.nick == nick and s.alive]


#: Process-wide, same rationale as ROOMS.
PRESENCE = PresenceRegistry()


def _member_channel_alive(member_id):
    """Any LIVE registered channel for this member (the crash-path entries the
    `finally` has not reaped yet do not count -- `alive` is authoritative)."""
    try:
        return any(getattr(s, "alive", True)
                   for s in PRESENCE.sessions_for(int(member_id)))
    except Exception:
        return False


def _logout_wipe(member_id, login_name, peer, sid, why=""):
    """The logout bookkeeping, in one place: stamp the logout, drop the session
    rows (presence = the session table), push offline at the watchers, clear
    the 4:5 status latch, and mark the Viewer closed. Runs immediately when
    POL_PRESENCE_LOGOUT_GRACE=0, or from the grace timer when the member
    really stayed gone. Opens its own DB handle -- the caller's connection is
    long closed by the time a timer fires."""
    try:
        gone = 0
        if accounts is not None:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                accounts.record_logout(db, int(member_id))
                gone = accounts.close_sessions(db, int(member_id))
            finally:
                db.close()
        log("accounts", f"{peer} logout stamped for {login_name} "
                        f"({gone} session(s) closed){why}")
        pushrecord._broadcast_presence(int(member_id), "offline")
        # DROP THE 4:5 STATUS TOO -- it is a latch; logout is the one moment
        # we can be sure it is stale. See `_publish_member_status`.
        memberstatus._publish_member_status(int(member_id), None)
        if sid:
            lobbysession._session_put(sid, viewer_open=False)
    except Exception as exc:
        log("accounts", f"logout not stamped ({exc!r})")


_LOGOUT_GRACE_LOCK = threading.Lock()
_LOGOUT_GRACE_PENDING = set()          # member ids with a grace timer in flight


def _logout_or_grace(member_id, login_name, peer, sid):
    """Mark a member offline -- but not in the CHANNEL-CHURN DIP.

    THE FLAP, measured live 2026-08-23 22:52Z:
    the client cycles its session channel every few minutes, and
    each cycle has a dip of seconds in which ZERO channels are live. The old
    code ran the full logout at every last-channel close, so an ACTIVELY
    PLAYING member flipped offline->online every 10-30s ("Lex -> offline"
    at 22:52:19, back at 22:52:38, while the user never left TM) -- and since
    the friend list is a login-time snapshot, any 2:3 fetched during a dip
    showed that friend offline until the NEXT relaunch. With several members
    churning, "everyone appears offline" is just the fetch-timing lottery.

    So the offline flip now WAITS OUT the dip: schedule the wipe
    POL_PRESENCE_LOGOUT_GRACE seconds ahead (default 60; 0 restores the
    immediate wipe) and let the timer re-check -- if the member has a live
    channel again by then it was churn, log it as SUPPRESSED and do nothing;
    if they are still gone it was a real exit and the wipe runs, one grace
    late. A real logout and a churn dip are indistinguishable at close time
    (both are an EOF on the last channel; the genuine 22:36:25Z exit produced
    the same "login ended with no outcome" trace as the flaps), so time is
    the only discriminator -- this is why the delay cannot be zero and also
    must not be long. Session rows stay live during the grace, which is the
    point: a 2:3 fetched in the dip now serves ONLINE.
    """
    try:
        grace = int(os.environ.get("POL_PRESENCE_LOGOUT_GRACE", "60"))
    except ValueError:
        grace = 60
    if grace <= 0:
        _logout_wipe(member_id, login_name, peer, sid)
        return
    with _LOGOUT_GRACE_LOCK:
        if int(member_id) in _LOGOUT_GRACE_PENDING:
            log("accounts", f"{peer} last channel closed for {login_name} -- "
                            f"an offline grace check is already pending; not "
                            f"scheduling another")
            return
        _LOGOUT_GRACE_PENDING.add(int(member_id))
    log("accounts", f"{peer} last channel closed for {login_name} (member "
                    f"{member_id}) -- holding presence {grace}s before marking "
                    f"offline (POL_PRESENCE_LOGOUT_GRACE=0 restores the "
                    f"immediate wipe)")

    def _fire():
        try:
            with _LOGOUT_GRACE_LOCK:
                _LOGOUT_GRACE_PENDING.discard(int(member_id))
            if _member_channel_alive(member_id):
                log("accounts", f"presence grace: {login_name} (member "
                                f"{member_id}) re-dialled within {grace}s -- "
                                f"offline wipe SUPPRESSED (channel churn, not "
                                f"a logout)")
                return
            _logout_wipe(member_id, login_name, peer, sid,
                         why=f" -- still gone after the {grace}s grace")
        except Exception as exc:
            log("accounts", f"presence grace check failed ({exc!r})")

    t = threading.Timer(grace, _fire)
    t.daemon = True
    t.start()


def _kill_duplicate_logins(member_id, new_sess, peer=""):
    """KILL this member's older auth channels that a new login replaces.

    What SE does, per Project Crystal Server: a second login of one POL ID
    gets the old socket a KILL and an ERROR, both encrypted under the old
    connection's own cipher, and the old socket is closed without an offline
    notification (the member is still online, on the new one).

        :<srv> KILL <nick>: <srvip>!<srv>[unknown@<srvip>]!Kicked by same NICK
        ERROR :Closing Link: <nick>[~x@<client ip>] <srvip>(Killed(Kicked by same NICK))

    WARNING: WHY THIS IS NOT CRYSTAL'S "ANY OTHER SOCKET OF THE POL ID". Here the
    games dial this same auth port: a Tetra Master launch opens two or three
    more :51241 channels for the SAME member, from the same machine, under
    its own USER token and the same client signature as the PC Viewer. A
    member-wide kill would throw the Viewer off every time a game connects.
    And one account may run a PC and a PS2 at once. So the
    default scope (POL_AUTH_KILL_DUP_SCOPE=ip) only kills a channel that is
    ALL of: a different launch (session id), the same client build (NICK
    signature), and a DIFFERENT client address -- i.e. the same kind of
    client signing in from somewhere else. Behind the Docker bridge without
    the relay preamble every client shares one address, so it kills nothing.
    `member` is Crystal's rule (any other launch of the member); use it only
    where no game shares the auth port.

    Returns how many channels were killed. Never raises.
    """
    scope = os.environ.get("POL_AUTH_KILL_DUP_SCOPE") or "ip"
    killed = 0
    try:
        for old in PRESENCE.sessions_for(int(member_id)):
            if (old is new_sess or not getattr(old, "alive", False)
                    or getattr(old, "killed_dup", True)):
                continue
            old_sid = getattr(old, "sid", None)
            if old_sid is None or old_sid == new_sess.sid:
                continue                      # the same launch (or unknown)
            if scope != "member" and (
                    getattr(old, "client_sig", None) != new_sess.client_sig
                    or old.peer_ip == new_sess.peer_ip):
                continue
            srv = old.srv.decode("latin-1")
            nick = old.nick.decode("latin-1")
            ip = old.peer_ip.decode("latin-1")
            srvip = os.environ.get("POL_AUTH_KILL_SRVIP") or authcap._self_ip()
            lines = [
                authnode.NoPad(f":{srv} KILL {nick}: {srvip}!{srv}[unknown@{srvip}]"
                      "!Kicked by same NICK".encode()),
                authnode.NoPad(f"ERROR :Closing Link: {nick}[~x@{ip}] {srvip}"
                      "(Killed(Kicked by same NICK))".encode()),
            ]
            old.killed_dup = True
            try:
                old.send(lines)
            except Exception:
                pass
            old.alive = False
            try:
                # Wake the owning thread; it closes the socket in its own
                # `finally`, as every other ending does.
                old.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            killed += 1
            log("authserv", f"{peer} KILLED an older login of member "
                            f"{member_id} ({nick} from {ip}, session "
                            f"{old.sid}) -- replaced by this one "
                            f"(POL_AUTH_KILL_DUP, scope={scope})")
    except Exception as exc:
        log("authserv", f"{peer} duplicate-login check failed ({exc!r}) -- "
                        "nothing killed")
    return killed


def _member_has_other_channel(member_id, this_sess):
    """Is another LIVE session channel still open for this member?

    WARNING: **A CLOSING CHANNEL IS NOT A LOGOUT WHEN THE MEMBER HAS ANOTHER.**
    Reported 2026-08-17 -- "I can't see my friends as being online even though
    they are". The friend list was right; it was being told the truth about a
    lie. `accounts.close_sessions()` is `DELETE FROM session WHERE member_id`,
    i.e. it drops EVERY row the member has, and the logout site called it
    whenever one channel ended. So with two channels open, either one closing
    wiped the member's whole presence and pushed "offline" at every friend:

        00:15:24.173  58060 client replied 48B total       one channel ends
        00:15:24.391  presence: Lex -> offline             ...and takes both
        00:15:24.402  closing hop 51241                    the ordinary hop dance

    while connection 33282 was at keepalive **PING #65**, answering every one.
    175 of these across one day's log, back to 09:00 -- presence was correct
    only between the flaps, which reads as "totally broken" rather than
    "sometimes wrong". `_peer_online` reads that same table, so the 2:3 row and
    the push agreed with each other; both were downstream of the wipe.

    The registry already knows: at the logout site this channel is still
    registered (the unregister is in `finally`), so "another one" means any
    OTHER live entry. A registered-but-dead session must not block the logout --
    that is the crash path, and it is what `alive` is for.

    POL_PRESENCE_LAST_CHANNEL=0 restores the old unconditional behaviour.
    """
    if os.environ.get("POL_PRESENCE_LAST_CHANNEL", "1") != "1":
        return False
    try:
        return any(s is not this_sess and getattr(s, "alive", True)
                   for s in PRESENCE.sessions_for(int(member_id)))
    except Exception as exc:
        # Never let a presence lookup be the reason a logout does not happen:
        # a stuck "online" is worse than a duplicated "offline".
        log("authserv", f"presence: other-channel check failed ({exc!r})")
        return False

#: Presence-state -> IRC away-ness, for the formats that model presence as AWAY.
_PRESENCE_STATES = ("online", "offline", "away", "back")

#: The wire format the client actually applies (RE'd
#: 2026-08-13). Presence updates ride an IRC message to a `#XXL<zoneid>`
#: channel carrying a substituted-base64 payload; the client dispatches on the
#: 4-char `#XXL` prefix, base64-decodes, and pokes ONE friend slot (index at
#: decoded +0x1c) via polcore 0x37db854. Kept as constants so the eventual `xxl`
#: builder is exact, not re-derived.
_PRESENCE_B64_ALPHABET = (
    "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX")
#: decoded payload header = SUBCOMMAND(low byte) | (TYPE<<8); presence class = type
#: 0x1f. The exact SUBCOMMAND byte is the one field still unread.
_PRESENCE_TYPE = 0x1f
#: action byte -> the two status bits at slot+0x08 (bit11=0x800, bit12=0x1000),
#: read straight off 0x037decb0. Meaning inferred (online=connected, away=+flag):
_PRESENCE_ACTION = {"online": 2, "back": 2, "away": 1, "offline": 0}

#: Live-tunable presence knobs WITHOUT a container recreate. A `key=value` control
#: file (one per line, '#' comments) re-read per push, same idea as search_calib's
#: control file -- so the whole live bring-up (off -> log -> send, and tuning the
#: zone/prefix/guid2 guesses) is driven by writing this file while the client stays
#: connected, instead of recreating authsess (which drops the session) for each
#: change. Values here OVERRIDE the POL_PRESENCE_* env vars; env is the fallback.
_PRESENCE_CTL_FILE = os.environ.get(
    "POL_PRESENCE_CTL", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                     "presence.ctl"))
_PRESENCE_CTL = {"mtime": None, "vals": {}}


def _presence_cfg(key, env_name, default):
    """Presence knob: control-file value if set, else the env var, else default."""
    try:
        st = os.stat(_PRESENCE_CTL_FILE)
        if st.st_mtime != _PRESENCE_CTL["mtime"]:
            vals = {}
            with open(_PRESENCE_CTL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            _PRESENCE_CTL["vals"] = vals
            _PRESENCE_CTL["mtime"] = st.st_mtime
    except OSError:
        _PRESENCE_CTL["vals"], _PRESENCE_CTL["mtime"] = {}, None
    if key in _PRESENCE_CTL["vals"]:
        return _PRESENCE_CTL["vals"][key]
    return os.environ.get(env_name, default)


#: The second `#XXL` chunk's decoded length. The parser reads it into a buffer at
#: esp+0x134 and the widest field set (0x08 + 0x10 + 0x68) needs 0x80; 0x90 is
#: what the earlier trace measured, and it stays under the 0x158 cap.
_PRESENCE_EXT_LEN = 0x90


def _presence_xxl_payload(served_guid, slot, action, seq, comment=None,
                          hslot=None):
    """The substituted-base64 payload for a `#XXL` presence update, byte-mapped
    from the client's own decoder/handler.

    Decoded struct (handler `0x037db6f0`; `_b64encode` is the client's exact codec,
    round-trip verified):
        +0x00 u64  guid1 = served_guid XOR 0x67891133(lo)/0x1c273e45(hi). The
                   handler decrypts with a runtime key that CANCELS against how the
                   2:3 list stored the same guid, so this is just served ^ const.
        +0x08 u64  guid2 -- second guid, purpose unconfirmed; set = guid1.
        +0x10 u8   action: 2=online, 1=away, 0=offline (flips slot+0x08 bits).
        +0x18 u8   0xff sentinel (handler gate).
        +0x1b u8   nonzero gate.
        +0x1c u8   friend SLOT index (< 0x40) -- the slot the watcher's 2:3 list
                   put this friend in; addresses the friend directly, no lookup.
        +0x30 u64  SEQUENCE -- update applies only if > the slot's stored seq
                   (`ja/jb/jae` at 0x37db89a), so a monotonic counter/timestamp.
        +0x38 u32  length of a 2nd base64 chunk; 0 = none (what we send).
        +0x3e u16  type word, must satisfy (w & 0xf80) == 0xf80 -> 0x0f80.
        +0x42 u8   bit0 set (handler gate).
    """
    p = bytearray(69)                                  # b64 -> 92 chars, < 0x60 cap
    lo = (int(served_guid) ^ 0x67891133) & 0xFFFFFFFF
    hi = ((int(served_guid) >> 32) ^ 0x1c273e45) & 0xFFFFFFFF
    struct.pack_into("<II", p, 0x00, lo, hi)           # guid1
    # --- LIVE-TUNABLE fields (the identity gates that decide whether the client
    # APPLIES the update; sweep via logs/presence.ctl, no restart). The client
    # accepts the line either way, so a wrong value just no-ops. ---
    # +0x18: the normal-path compare wants this to equal (slot+0x9c >> 13) & 0x3f,
    # a 6-bit field; 0xff is the SPECIAL-case sentinel, NOT this. Candidates:
    #   slot (the friend's index, default), 0, ff, or an explicit int.
    # RESOLVED 2026-08-13: this is the friend's HANDLE SLOT, not the record
    # index, and it is NOT key-scrambled. The compare is
    #   0x37dba7f  ecx = [slot+0x9c]; shr ecx,0xD; and ecx,0x3F; cmp ecx,[esi+0x18]
    # and +0x9c is filled by a PLAIN bitfield move from the 2:3 record's dword0
    # bits 7..12 (0x37df001) -- the handle slot the server now serves. The old
    # note that it was K-scrambled confused this with slot +0x00 (the guid,
    # which IS scrambled). The earlier sweep over {slot, 0, ff} could not have
    # worked: we served 0 in that record field then, so "slot" and "0" were only
    # the same wrong answer twice.
    id18 = _presence_cfg("id18", "POL_PRESENCE_ID18", "hslot")
    if id18 == "hslot":
        p[0x18] = int(slot if hslot is None else hslot) & 0x3f
    elif id18 == "slot":
        p[0x18] = int(slot) & 0x3f
    elif id18 in ("ff", "0xff"):
        p[0x18] = 0xff
    else:
        try:
            p[0x18] = int(id18, 0) & 0xff
        except ValueError:
            p[0x18] = int(slot) & 0x3f
    # +0x08 guid2: purpose unconfirmed. "guid1" (default) | "zero".
    if _presence_cfg("guid2", "POL_PRESENCE_GUID2", "guid1") != "zero":
        struct.pack_into("<II", p, 0x08, lo, hi)
    p[0x10] = int(action) & 0xff
    # THE GATE BYTES, re-read off the handler 2026-08-13 and CORRECTED. The old
    # values could never apply: the run-up to the field parser is
    #
    #   0x37db97e  al = [esi+0x1b]; test al,al; jne  bail   -- +0x1b MUST BE 0
    #   0x37db989  al = [esi+0x1a]; test al,al; jne  bail   -- +0x1a MUST BE 0
    #   0x37db994  test byte [esi+0x19],1;      je   bail   -- +0x19 bit0 SET
    #   0x37db99e  eax = [esi+0x38]; cmp 0x158; jge  bail   -- chunk len < 0x158
    #
    # and we were sending +0x1b = 1, which bails on the FIRST of those. That is
    # why the 2026-08-13 live test saw the client accept the line and change
    # nothing: it never reached the apply path, so the subscription state was
    # never even the question. `gates=old` restores the previous bytes for A/B.
    if _presence_cfg("gates", "POL_PRESENCE_GATES", "fixed") == "old":
        p[0x1b] = 1
    else:
        p[0x19] |= 1
        p[0x1a] = 0
        p[0x1b] = 0
    p[0x1c] = int(slot) & 0x3f
    struct.pack_into("<II", p, 0x30, int(seq) & 0xFFFFFFFF, (int(seq) >> 32) & 0xFFFFFFFF)
    struct.pack_into("<H", p, 0x3e, 0x0f80)            # type word
    p[0x42] = 1

    # THE SECOND CHUNK. Everything the updater is handed comes from here: the
    # parser base64-decodes it (0x37db9cf), takes a FLAGS byte from ext+0x00 and
    # walks a cursor that starts at ext+0x08, advancing by each present field --
    #
    #   bit 0x01  the +0x10 presence-action block from the MAIN payload
    #   bit 0x02  0x10 bytes at ext+0x08 (takes a DIFFERENT handler branch --
    #             0x37dba36 `test edi,edi; jne 0x37dbb41` -- so leave it CLEAR)
    #   bit 0x04  8 bytes      bit 0x08  0x10 bytes
    #   bit 0x10  0x10 bytes   NAME (15 chars + NUL)
    #   bit 0x20  0x68 bytes   COMMENT (50 UTF-16 chars + terminator)
    #   bit 0x40  a further field
    #
    # With only bits 0x01|0x20 set the cursor never advances before the comment,
    # so the comment sits at ext+0x08. Sending NO chunk is not the safe option:
    # the decode still runs and the flags byte is then read out of an
    # uninitialised stack buffer.
    ext = bytearray(_PRESENCE_EXT_LEN)
    flags = 0x01
    if comment:
        flags |= 0x20
        raw = str(comment).encode("utf-16-le", "replace")[:0x64]
        ext[0x08:0x08 + len(raw)] = raw
    ext[0x00] = flags
    chunk = logingate._b64encode(bytes(ext))
    # Whether +0x38 is the ENCODED or the DECODED length is NOT established --
    # it is passed straight to the decoder as its third argument. Tunable; the
    # only hard constraint read off the binary is < 0x158.
    declared = len(chunk) if _presence_cfg(
        "len38", "POL_PRESENCE_LEN38", "encoded") == "encoded" else len(ext)
    struct.pack_into("<I", p, 0x38, min(declared, 0x157))
    # The main block is padded so it encodes to EXACTLY 0x60 chars, which is
    # where the parser expects the second chunk to start. 72 bytes -> 96 chars.
    main = logingate._b64encode(bytes(p).ljust(0x48, b"\x00"))
    return main + chunk
