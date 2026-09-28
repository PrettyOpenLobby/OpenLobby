"""The session channel loop and re-attaching a session after an authserv restart."""
import os
import socket
import time
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
import sessioncrypt
from .deps import accounts, gmchat
from . import authkick, authnode, authserv, chatsession, friendroster, gamenotice, ircband, lobbysearch, lobbysession, pacing, presence, roomregistry



# --------------------------------------------------------------------------- #
# The session channel, and re-attaching one after an authserv restart
# --------------------------------------------------------------------------- #
def _auth_channel_loop(conn, peer, addr, chat_sess, nick, prefix, P, S, iv,
                       keepalive, ping_every):
    """Serve an established auth connection as this client's session channel.

    Lifted out of handle_authserv verbatim (2026-08-15) so that
    handle_authresume can run the SAME loop on a re-made socket. One copy is the
    whole point: an in-session fix must never apply to a fresh login and not to
    a resumed one. Returns everything the client sent, for the caller's log.

    WARNING: THE BUILD MARKER IS SET HERE TOO. `_peer_build` is per-THREAD and the
    lobby handler sets it on the lobby connection, which is not this one: a
    title's game band rides the auth session, and a title that shapes its
    records by client build reads the marker from this thread.
    """
    pacing._peer_build.ip = addr[0] if addr else None
    ping_seq = 0
    extra = b""
    # The observe window is NOT passive. Live 2026-08-11: creating a chat room
    # sends lobby 4:5, then speaks on THIS socket -- one encrypted line, `AWAY`
    # -- and blocks on the numeric reply. We logged it and said nothing, so the
    # client sat on "Creating chat room..." forever. So: decrypt line by line,
    # answer what we recognise, and log the rest VERBATIM (which is byte-for-byte
    # the old behaviour for anything unrecognised -- an unknown command still
    # gets silence, exactly as before).
    #
    # Two framing details, both verified against the captured AWAY line:
    #   * OFB resets from the IV each line, so every line uses this connection's
    #     recovered `iv`, never the fixed K0_IV, which would decrypt this
    #     connection to garbage.
    #   * The client appends the 4-char checksum but does NOT transmit the pad
    #     byte it checksums over, so the command text is line[:-4].
    #     frame_line("AWAY", pad=NUL) reproduces the captured "USDV" exactly.
    interact = os.environ.get("POL_AUTH_INTERACT", "1") == "1"
    buf = b""
    # THE SESSION CHANNEL. `chat_sess` already exists (it owns the socket);
    # what is decided here is whether it may be PUT IN A ROOM. Only the
    # `welcome` hop is a real session -- the redirect hops close immediately --
    # so only it is handed to the reply builder as a joinable session. It is
    # removed from every room in `finally` no matter how this ends, which is
    # what stops a crashed client haunting a room's NAMES list forever.
    room_sess = chat_sess if (keepalive and interact) else None
    # THE GM CHAT RELAY. A GM's line arrives as a FILE (services/gmchat.py says
    # why) and is delivered from inside this loop, so it needs no port and no
    # thread. Two pieces of state: which GM room this session is in -- learned by
    # watching its own PRIVMSGs, since the client tells us on every heartbeat --
    # and when the last PING went out.
    #
    # WARNING: The socket timeout is what bounds delivery latency, and it used to be the
    # PING interval (60s), which is unusable for chat. It is now the SHORTER of
    # the two, with the PING still sent on its own schedule -- so pumping the
    # spool does not turn into a ping flood. Only the keepalive hop is affected;
    # the redirect hops keep `observe` and their idle-to-close behaviour.
    gm_room = None
    # WHICH GAME PEERS THIS SOCKET CARRIES. A member has more than one auth
    # connection (the room band and the table band are separate sockets), so
    # "this member has a push due" is not enough to decide it may leave HERE --
    # see the title's idle_pushes, and the black screen that proved it.
    game_peers = set()
    # WHICH BAND IS THIS, AND WHO HUNG UP. A title opens more than one :51241
    # and the roles are only distinguishable by what the client says on each
    # (Tetra Master, the case that was measured):
    #
    #   GAME band  -- carries the game handshake `@TeachDV=` (0xE1) then
    #                 `@Init=` (0x80). This is the one the game records as its
    #                 GameIrcID; its channel-2 watchdog pops "Failed to connect
    #                 to server." once this band has been opened and its socket
    #                 is no longer alive.
    #   ROOM band  -- carries the class-L roster poll `<DR>` every 1-3 s.
    #   VIEWER     -- the POL Viewer's session hop: PING/PONG and nothing else.
    #
    # The title classifies the line (`titles.band_role`); this is diagnostics.
    #
    # Recorded here, reported once at close with the connection's lifetime and
    # WHO ended it, because "the game band died" and "we closed the game band"
    # are different findings and the old log could not tell them apart -- it
    # printed the same `closing hop 51241` line for all three roles and every
    # cause. A live `[tmband]` WATCHDOG TRIPPED line is meant to be read against
    # this one. Diagnostic only: nothing here changes what is sent or held.
    band_role = "unclassified"
    band_open_at = time.time()
    close_why = "loop exited"
    gm_poll = float(os.environ.get("POL_GMCHAT_POLL", "2"))
    if keepalive and gmchat is not None and gm_poll > 0:
        conn.settimeout(min(ping_every, gm_poll))
    last_ping = time.time()
    _idle_push_gate_said = False    # one log per connection, not per timeout
    try:
        while True:
            if chat_sess is not None and getattr(chat_sess, "killed_dup", False):
                # _kill_duplicate_logins shut this socket down, which wakes a
                # blocked recv on Linux; this catches the next timeout anywhere.
                close_why = "we closed it: a newer login KILLED this one"
                break
            if gmchat is not None and gm_room:
                for spool_nick, rec in gmchat.drain(gm_room):
                    # An empty nick means "attribute it to the client itself" --
                    # the probe that separates a malformed record from a speaker
                    # that does not resolve in the member table.
                    who = nick if spool_nick == b"self" else (spool_nick or None)
                    line = gmchat.privmsg(gm_room, rec, ircband._irc_host(prefix[1:]),
                                          nick=who)
                    if chat_sess.send([line]):
                        log("authserv", f"  gm-chat {gm_room.decode('latin1')}: "
                                        f"delivered {rec[:60]!r} as "
                                        f"{(who or gmchat.GM_NICK).decode('latin1')}")
                        # Into the room's transcript, so the GM sees their
                        # own line land in the same list as the player's.
                        # DELIVERED IS NOT RENDERED, and the panel says so: a
                        # line can leave here intact and still draw nothing,
                        # which is a standing open problem.
                        gmchat.record(gm_room, "out", who or gmchat.GM_NICK, rec)
            try:
                c = conn.recv(4096)
            except socket.timeout:
                if not keepalive:
                    close_why = 'we closed it: redirect hop went idle'
                    break                      # redirect hop: idle -> close (advance)
                # *** TETRA MASTER'S PUSHES, WITHOUT WAITING TO BE ASKED. ***
                #
                # Measured 2026-08-20 in the first two-player match: in a match the
                # client's only unprompted traffic is `@Pong=`, every 15 s. Every
                # server push -- the other player's `@PutCard=`, the next
                # `@TurnData=` -- rode a REPLY, so each one cost a whole Pong
                # interval and a turn took ~30 s of pure delivery latency. The
                # live report was "it's happening but it's happening veeeery
                # slowly"; none of it was the game thinking.
                #
                # This loop already wakes every POL_GMCHAT_POLL seconds (2 by
                # default) and `chat_sess` exists precisely to be pushed to -- the
                # GM chat relay above is the same shape, and the socket timeout was
                # lowered from the 60 s ping interval for exactly this reason. So
                # WARNING: AND IT MUST BE **HERE**, IN THE TIMEOUT ARM -- not at the
                # top of the loop, where the first version put it. The loop head
                # also runs immediately after a line has been processed, so a
                # push queued while handling that line went out MICROSECONDS
                # later: measured 2026-08-20T17:21:31Z, the match wake-up left
                # 2 ms after the recipient's own `@GameEN=` and the client
                # answered `@GameNG=`. That is the push law -- a push must not ride
                # the reply that changes the scene -- violated by construction,
                # and it showed up live as "one computer said game start, black
                # screen; the other is still in the room screen".
                #
                # A `socket.timeout` is the one signal that means the client has
                # actually gone QUIET, which is the condition the law wants and
                # a counter can only approximate.
                # WARNING: ONLY IF THIS CONNECTION IS THE ONE THE CLIENT IS ACTUALLY
                # ON. A relaunched client leaves its old connection looping
                # here with a dead socket that BUFFERS writes without erroring
                # -- and whichever connection with this member_id times out
                # first DRAINS the push queue. Measured 2026-08-24T01:31Z: the
                # trade card lists were "pushed unprompted" to 127.0.0.1:
                # 37359 (the pre-relaunch zombie) while the live client's
                # [tmlog] narration showed ZERO arrivals -- delivered is not
                # landed, the push-path edition (the relay learned this same
                # lesson as 28c8d7fc). `last_heard` updates on every line THIS
                # socket receives; a live client speaks every ~2s (<DR>/@Pong),
                # a zombie's is frozen at the relaunch. Gate the drain on it.
                _fresh = (time.time() - getattr(chat_sess, "last_heard", 0)
                          < float(os.environ.get("POL_PUSH_FRESH", "25")))
                if not _fresh and not _idle_push_gate_said:
                    _idle_push_gate_said = True
                    log("authserv", f"{peer} idle title push drain SUPPRESSED -- "
                                    f"this connection has not heard the client "
                                    f"for >{os.environ.get('POL_PUSH_FRESH', '25')}s "
                                    f"(stale/zombie socket; the live connection "
                                    f"will drain instead)")
                # A title that sets `idle_push_when_quiet` is asked even on a
                # quiet connection: its client may wait for a record in
                # silence, so silence is not evidence of a zombie socket for
                # it (the title pins delivery to the live session itself).
                if (titles.loaded() and room_sess is not None
                        and (_fresh or titles.any_push_when_quiet())):
                    # WARNING: A POP IS NOT A DELIVERY. idle_pushes forgets what it
                    # hands over, and this band never resends -- so a failed
                    # send here used to drop match-critical bodies (@StartData/
                    # @TurnData) on the floor and park the player forever.
                    # Whatever is not confirmed sent goes BACK via
                    # requeue_pushes for the next tick (or the live connection,
                    # if this one is the zombie).
                    _tp_due, _tp_sent = [], 0
                    try:
                        _tp_mid = lobbysession._session_get("member_id")
                        _tp_due = (titles.idle_pushes(_tp_mid, game_peers,
                                                      quiet=not _fresh)
                                   if _tp_mid is not None else [])
                        for _title, _peer, _body in _tp_due:
                            _line = authnode.NoPad(gamenotice._game_notice_line(
                                b"G" + _title.tag + b"G" + _body,
                                _peer or nick, nick, prefix[1:]))
                            if not chat_sess.send([_line], pad_override=(
                                    b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                                    == "space" else b"")):
                                log("authserv", f"{peer} idle title push failed -- "
                                                f"peer gone; requeueing "
                                                f"{len(_tp_due) - _tp_sent} "
                                                f"undelivered")
                                break
                            _tp_sent += 1
                            log("authserv", f"{peer} pushed unprompted: "
                                            f"{_title.describe_line(_body)}")
                    except Exception as _e:
                        # A fault here must never take the session down -- the same
                        # rule the reply builder below already follows.
                        log("authserv", f"{peer} idle title push raised {_e!r} -- "
                                        f"undelivered entries requeued")
                    if _tp_sent < len(_tp_due):
                        for _title in titles.all():
                            _left = [(p, b) for t, p, b in _tp_due[_tp_sent:]
                                     if t is _title]
                            if not _left:
                                continue
                            try:
                                _title.requeue_pushes(
                                    _tp_mid, _left, why=f"send failed on {peer}")
                            except Exception as _e:
                                log("authserv", f"{peer} push requeue itself raised "
                                                f"{_e!r} -- {len(_left)}"
                                                f" push(es) LOST")
                if time.time() - last_ping < ping_every:
                    continue                   # a spool poll, not a ping tick
                last_ping = time.time()
                ping_seq += 1
                # Standard server-originated IRC PING (no source prefix): the
                # client answers PONG. Format is a best guess -- SE's encrypted
                # PING was never captured -- but the connection-hold above is the
                # actual fix; an ignored PING still leaves the socket open.
                ping = b"PING :POL" + str(ping_seq).encode()
                if not chat_sess.send([ping]):
                    log("authserv", f"{peer} keepalive send failed after "
                                    f"{ping_seq} ping(s) -- peer gone, closing")
                    close_why = 'TCP refused our PING: the peer was already gone'
                    break
                log("authserv", f"{peer} keepalive PING #{ping_seq} "
                                f"(idle {ping_every}s); holding session open")
                # ...and hold it open in the SESSION STORE too, not just on the
                # socket. `at` is otherwise only touched by auth events, so a
                # client sitting on a menu aged out of `_SESSIONS` after
                # _SESSION_TTL while its connection was still live -- and the
                # lobby, which identifies a connection by trying each session's
                # IV, then had no candidate that validated and answered an empty
                # frame ("no session claims this frame"). One republish a minute
                # per connection is cheap; see the _SESSION_TTL note.
                if lobbysession._session_sid():
                    lobbysession._session_put(lobbysession._session_sid())
                continue
            except OSError as exc:
                close_why = ('administrator kicked account' if chat_sess.admin_kicked
                             else f'socket read failed: {exc}')
                break
            if not c:
                close_why = ('administrator kicked account' if chat_sess.admin_kicked
                             else 'THE CLIENT hung up (clean EOF)')
                break
            extra += c
            if not interact:
                continue
            buf += c
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                if not line:
                    continue
                pt = sessioncrypt.ofb_apply(P, S, iv, line)
                cmd_txt = pt[:-4] if len(pt) > 4 else pt
                # WE HEARD FROM THEM. See ChatSession.last_heard: this is the
                # signal that separates a client that is idle from one that has
                # stopped taking part, and it is what keeps a hung client from
                # being handed the room.
                # WE HEARD FROM THEM, AND ON WHICH BAND. Class **L** is the
                # room roster poll (`G<tag>L<DR>`, every 1-3 s while a room screen
                # is up) and it is the only signal that says this client is in
                # the ROOM rather than merely connected -- see
                # ChatSession.in_room_recently.
                _t = cmd_txt.split(None, 2)
                chat_sess.note_heard(room_band=(
                    len(_t) >= 3 and _t[0].upper() == b"NOTICE"
                    and _t[2][:2] == b":G" and _t[2][5:6] == b"L"))
                log("authserv", f"{peer} in-session line: {cmd_txt!r} "
                                f"(chk={pt[-4:]!r})")
                # `NOTICE <peer> :G<tag>G...` is a game envelope; the target is
                # the peer nick this socket speaks for. Recorded so an
                # unprompted push can only leave on the band it belongs to.
                _gp = cmd_txt.split(None, 2)
                if len(_gp) >= 3 and _gp[0].upper() == b"NOTICE"                         and _gp[2][:2] == b":G" and not _gp[1].startswith(b"#"):
                    game_peers.add(_gp[1])
                # CLASSIFY THE BAND from the client's own words -- see the
                # band_role banner. The game handshake wins over the room poll
                # if a connection somehow carries both, because it is the
                # handshake that makes TM record this socket as its GameIrcID.
                #
                # The title reads the line (it knows its own handshake codes
                # and its roster poll; see the title's `band_role`) and answers
                # (priority, label). A higher-priority verdict replaces a lower
                # one; a lower one only fills in an unclassified band.
                _was_role = band_role
                _br = titles.band_role(cmd_txt)
                if _br and (band_role == "unclassified" or _br[0] >= 2):
                    band_role = _br[1]
                if band_role != _was_role:
                    log("authserv", f"{peer} band role: {band_role} "
                                    f"(learned {time.time() - band_open_at:.1f}s "
                                    f"after this hop opened)")
                # Learn this session's GM room from its own traffic. The client
                # PRIVMSGs its 'H' presence record to the room on a timer, so this
                # binds within one heartbeat and re-binds if it moves rooms. JOIN
                # is deliberately not used: the client was observed talking to
                # #gmchat001 without one ever reaching us.
                if gmchat is not None:
                    _p = cmd_txt.split(None, 2)
                    if len(_p) >= 2 and _p[0].upper() in (b"PRIVMSG", b"JOIN") \
                            and gmchat.is_gm_room(_p[1]):
                        if gm_room != _p[1]:
                            gm_room = _p[1]
                            log("authserv", f"{peer} is in GM chat room "
                                            f"{gm_room.decode('latin1')} -- "
                                            f"relaying spooled GM lines here")
                        # *** AND KEEP THE CLIENT'S OWN RECORD. *** This is
                        # the only place SE's record language is ever seen being
                        # SPOKEN rather than parsed out of a disassembly, and
                        # until now it reached nothing but the log line above,
                        # mixed in with every other session's traffic.
                        # `gmchat.T_HEAD` exists only because somebody read a
                        # client's own T record back out of a log by hand;
                        # recording it verbatim -- HEX, not just the decoded
                        # text -- makes the next such correction a lookup. It is
                        # also what puts the player's half of the conversation
                        # in front of the GM.
                        if _p[0].upper() == b"PRIVMSG" and len(_p) >= 3:
                            _rec = _p[2][1:] if _p[2][:1] == b":" else _p[2]
                            if _rec:
                                gmchat.record(gm_room, "in", nick, _rec)
                            # THE GM ANSWERS THE ROLL CALL. A newcomer's `HR`
                            # asks every member for an `HA`; the GM has no
                            # client to answer it, so we do. The `G` in it is
                            # what makes the client draw the GM with the phoenix
                            # (gmchat.encode_roster). Straight to this socket,
                            # not the spool: only the asker needs it.
                            if _rec and gmchat.is_roster_request(_rec):
                                _ha = gmchat.encode_roster(gm_room)
                                if chat_sess.send([gmchat.privmsg(
                                        gm_room, _ha,
                                        ircband._irc_host(prefix[1:]))]):
                                    gmchat.record(gm_room, "out",
                                                  gmchat.GM_NICK, _ha,
                                                  note="GM roster answer")
                # A fault in here must NOT tear down the connection: the old
                # behaviour was silence, and silence is the safe fallback.
                # Learned the hard way -- a str/bytes TypeError in the reply
                # builder killed the whole hop on the first live AWAY.
                try:
                    reply = ircband._auth_session_reply(cmd_txt, nick, prefix[1:],
                                                addr[0].encode(),
                                                sess=room_sess)
                except Exception as e:
                    log("authserv", f"{peer} reply builder failed on "
                                    f"{cmd_txt!r}: {e} -- staying silent")
                    continue
                if reply is None:
                    # *** LOG THE WHOLE LINE, NOT JUST THE VERB. *** A bare
                    # "no handler for b'NOTICE'" is unactionable: it names the
                    # verb we ignored and hides the payload that would say WHY
                    # the client keeps re-sending it. The VS. COM session spun
                    # on exactly that for 116 retries (2026-08-22) -- the log
                    # proved a loop existed and could not say what it wanted, so
                    # reading it cost a code change and a redeploy. Capped
                    # because an unhandled line is untrusted length.
                    _v = cmd_txt.split()
                    log("authserv",
                        (f"{peer} no handler for {_v[0]!r} -- line: "
                         f"{cmd_txt[:lobbysearch._NOHANDLER_LOG_MAX]!r}"
                         f"{'...' if len(cmd_txt) > lobbysearch._NOHANDLER_LOG_MAX else ''}")
                        if _v else f"{peer} empty line")
                    continue
                if not reply:
                    # HANDLED, NOTHING TO SEND -- distinct from `None`, which is
                    # "no handler" above. An arm returns `[]` when the protocol
                    # says consume-and-stay-quiet (PONG). Guarding here rather
                    # than inside send() keeps `send([])` from ever being a
                    # question: it is never called with an empty list.
                    continue
                # A NoPad line is game-world traffic, which the PS2 side
                # frames without frame_line()'s pad byte -- see NoPad. The
                # checksum verifies either way (the pad sits INSIDE the
                # covered span, so `n = len-6` still matches what we summed),
                # so this only decides whether a stray trailing byte lands in
                # the game's payload. Unpadded is the PS2 emitter's own shape;
                # POL_GAME_NOTICE_PAD=space falls back to the form every auth
                # line the PS2 client has accepted so far uses, if a payload
                # ever needs to be ruled out as the problem.
                game_pad = (b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                            == "space" else b"")
                chat_sess.send(reply, pad_override=game_pad)
                log("authserv", f"{peer} answered with {len(reply)} line(s): "
                                + "; ".join(repr(l) for l in reply))
    except socket.timeout:
        close_why = 'read timed out'
    # THE BAND VERDICT. One line per connection saying what it turned out to be,
    # how long it lasted and who ended it -- the three facts a pol-shim
    # "[tmband] WATCHDOG TRIPPED" line has to be read against. Never raises: a
    # diagnostic must not be the reason a session teardown fails.
    try:
        if os.environ.get("POL_BAND_DIAG", "1") == "1":
            log("authserv", f"{peer} band verdict: {band_role}, lived "
                            f"{time.time() - band_open_at:.1f}s, ended because "
                            f"{close_why}"
                            + ("  <-- A GAME BAND ENDING IS WHAT ARMS TM's "
                               "channel-2 watchdog ([0x52431F0]); expect "
                               "'Failed to connect to server.' on the next "
                               "scene that runs the 0xA8830 pump"
                               if band_role.startswith("GAME") else ""))
    except Exception:
        pass
    return extra


def _resume_fail(conn, peer, why):
    try:
        conn.sendall(b"RESUME FAIL " + why.encode() + b"\r\n")
    except OSError:
        pass
    log("authserv", f"{peer} resume DECLINED: {why}")


def handle_authresume(conn, addr, srv_name):
    """The resume door: re-attach an ORPHANED session channel to a new socket.

    Spoken by services/authrelay.py and by nothing else -- least of all the
    client, whose own socket is the thing being saved and which never learns
    that any of this happened.

        relay -> us   RESUME :<sha1 of the session's NICK line> <port>
        us -> relay   RESUME OK   |   RESUME FAIL <reason>

    Everything a channel needs -- key, IV, nick, member, the server name in the
    prefix -- was written to auth-sessions.json by the login that created it, so
    it survives the restart this exists to hide. After the OK this socket IS the
    session channel and runs the same _auth_channel_loop a fresh login does.

    WHAT THIS MUST NEVER DO is resurrect a hop that closed on purpose: a
    redirect hop's EOF is how the client advances to the next auth node, and
    answering OK there would strand the login. `channel_open` decides it, and it
    decides correctly by construction -- handle_authserv's `finally` clears the
    flag on every path it controls, so a flag still set means the process was
    killed holding the socket.

    A resume is NOT a login: no password is checked here and none is presented.
    That is safe only because the fingerprint is unforgeable without having seen
    the client's own encrypted NICK line, and because the door is reachable only
    from where the relay runs (POL_AUTH_RESUME_BIND, 127.0.0.1 in production).
    Do not widen either without replacing the proof of identity.
    """
    peer = f"{addr[0]}:{addr[1]}"
    chat_sess, acct_db, member, sid = None, None, None, None
    try:
        line, _rest = authserv._recv_line(conn, timeout=10)
        parts = line.split() if line else []
        if len(parts) < 2 or parts[0].upper() != b"RESUME":
            return _resume_fail(conn, peer, "protocol")
        fp = parts[1].lstrip(b":").decode("latin-1", "replace")
        port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        # Another container wrote the slot we are looking for (or our own
        # previous life did). Re-read before deciding it does not exist.
        lobbysession._sessions_refresh()
        with lobbysession._SESSIONS_LOCK:
            sid = next((k for k, v in lobbysession._SESSIONS.items()
                        if v.get("resume_fp") == fp), None)
            slot = dict(lobbysession._SESSIONS[sid]) if sid else None
        if slot is None:
            return _resume_fail(conn, peer, "unknown-session")
        if not slot.get("channel_open"):
            return _resume_fail(conn, peer, "closed")
        iv, nick = slot.get("iv"), slot.get("nick")
        srv = slot.get("srv") or f"pol-1000-{port or 51241}.pol.com"
        if not iv or not nick:
            return _resume_fail(conn, peer, "incomplete-session")
        key = slot.get("key") or b"\x00" * 8
        P, S = ((authnode._P0, authnode._S0) if key == b"\x00" * 8
                else sessioncrypt.bf_setkey(key))
        # The account. Looked up the same way the login looked it up, but
        # WITHOUT resolve_account: no provisioning, no credential check and no
        # new session row belong on this path -- the login already did all
        # three, and the row it opened is still open because nothing closed it.
        # The id is then checked against the one the session recorded: if those
        # ever disagree, something is wrong enough that serving an account is
        # the last thing to do.
        nick_s = nick.decode("ascii", "replace")
        if accounts is not None and os.environ.get("POL_ACCOUNTS", "1") == "1":
            acct_db = accounts.connect()
            member = (accounts.member_by_handle(acct_db, nick_s) or
                      accounts.get_member(acct_db, nick_s) or
                      accounts.member_by_alias(acct_db, nick_s))
        want = slot.get("member_id")
        if member is not None and want is not None and int(member["id"]) != int(want):
            return _resume_fail(conn, peer, "member-mismatch")
        lobbysession.session_bind(sid)
        ping_every = int(os.environ.get("POL_AUTH_PING", "60"))
        prefix = ":" + srv
        chat_sess = chatsession.ChatSession(nick, srv, addr[0], conn, P, S, iv,
                                member=member)
        chat_sess.sid, chat_sess.client_sig = sid, slot.get("client_sig")
        key_txt = "0" if key == b"\x00" * 8 else key.hex()
        log("authserv", f"{peer} RESUMED session {sid} as {nick_s} "
                        f"(fp={fp[:12]}, IV={iv.hex()}, K={key_txt})")
        # Re-arm what lived in the DEAD process's memory. The registry held a
        # ChatSession wrapping a socket that no longer exists; this one replaces
        # it. Room membership is NOT restored -- ROOMS lived in that process too,
        # and re-joining rooms on the client's behalf would invent traffic it
        # never asked for. Presence is different: the account still reads online
        # (the session row was never closed), so the registry has to agree.
        # The registration happens before RESUME OK, under the same lock a kick
        # takes, so a kick that retired this launch meanwhile refuses the resume.
        if member is not None:
            if not authkick._register_account_channel(member["id"], chat_sess,
                                                      resume=True):
                return _resume_fail(conn, peer, "closed")
        else:
            lobbysession._session_put(sid, channel_open=True, viewer_open=True,
                                      peer_ip=addr[0])
        conn.sendall(b"RESUME OK\r\n")
        if member is not None:
            # A resumed session lost polcore's recognition table with the old
            # process, so re-load the roster here too (gated POL_FRIEND_LOAD).
            friendroster._send_friend_roster(chat_sess, int(member["id"]))
        conn.settimeout(ping_every)
        extra = _auth_channel_loop(conn, peer, addr, chat_sess, nick, prefix,
                                   P, S, iv, True, ping_every)
        chat_sess.alive = False
        if extra:
            log("authserv", f"{peer} resumed client sent {len(extra)}B total")
        if member is not None and chat_sess.admin_kicked:
            # The kick request owns cleanup after it closes the socket.
            pass
        elif member is not None and chat_sess.killed_dup:
            log("accounts", f"{peer} resumed channel for "
                            f"{member['login_name']} was killed by a newer "
                            "login -- not a logout, no offline notification")
        elif acct_db is not None and member is not None \
                and presence._member_has_other_channel(int(member["id"]), chat_sess):
            log("accounts", f"{peer} resumed channel closed for "
                            f"{member['login_name']} but other live channel(s) "
                            "remain -- not a logout, presence held")
        elif acct_db is not None and member is not None:
            # Same logout bookkeeping the original hop would have done, because
            # this connection ended the session in its place -- and the same
            # churn-dip grace applies (see `_logout_or_grace`): a resumed
            # channel's EOF is no more proof of a logout than the original's.
            presence._logout_or_grace(int(member["id"]), member["login_name"], peer, sid)
        log("authserv", f"{peer} resumed channel closed")
    except Exception as e:
        log("authserv", f"{peer} resume error: {e!r}")
    finally:
        if sid is not None and not (chat_sess is not None and chat_sess.admin_kicked):
            for field in ("viewer_open", "channel_open"):
                try:
                    lobbysession._session_put(sid, **{field: False})
                except Exception:
                    pass
        if chat_sess is not None:
            chat_sess.alive = False
            if member is not None:
                presence.PRESENCE.unregister(int(member["id"]), chat_sess)
            for chan, remaining in roomregistry.ROOMS.drop(chat_sess):
                quit_line = (b":" + chat_sess.nick + b"!~x@" + ircband._irc_host(chat_sess.srv) +
                             b" QUIT :Connection closed")
                for m in remaining:
                    m.send([quit_line])
                # ...AND RETIRE THE TETRA MASTER ROOM RECORD, for the same reason
                # the registry is cleaned here: a session that vanishes without a
                # PART would otherwise sit there for ever.
                #
                # WARNING: ONLY IF THIS WAS THE MEMBER'S LAST SESSION IN THE ROOM.
                # A launch holds SEVERAL connections at once -- redirect hops
                # share one session id, which is exactly why the deliberate-close
                # bit above must check the fingerprint before clearing it -- so
                # retiring on ANY close retires a member who is still standing in
                # the room on another socket. Measured 2026-08-20, the first day
                # this shipped: member 9 was DROPPED three times in one sitting
                # while actively playing, and the roster we then served to the
                # other player was missing HIMSELF, because one of his own hops
                # had closed seconds earlier. `remaining` is precisely the set
                # that answers "is he still here", and it is already in hand.
                if member is not None and not presence._member_still_present(
                        remaining, member["id"]):
                    titles.session_closed(int(member["id"]))
        if acct_db is not None:
            try:
                acct_db.close()
            except Exception:
                pass
        conn.close()
