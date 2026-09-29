"""The auth band's IRC verbs (JOIN, PART, NOTICE, PRIVMSG, ...) and the XXL gate."""
import os
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
from .deps import accounts
from . import chatsession, framing, gamenotice, lobbyrooms, lobbysession, memberstatus, presence, pushrecord, roomregistry



#: A ROOM'S NAME LIVES IN ITS IRC TOPIC, encoded: SE's `332` for Novice_Hall read
#: `zT95ToiTK7IT` + `Novice_Hall`, and for the created FOXROOM `zt9rTojTK7IT` +
#: `FOXROOM`. The leading twelve symbols are POL-base64 (nine bytes of room
#: settings, 'T' being that alphabet's zero) and they differ per room, so what
#: they MEAN is not established -- which is why this is a knob and not a constant
#: buried in a format string.
#:
#: The default is SE's own listed-room blob. Our client emits `zT7TTTTTTTTT` for a
#: room it creates with default settings, so an all-zero body is clearly legal;
#: SE's is used here because these three rooms are SE's rooms.
_ROOM_TOPIC_PREFIX = "zT95ToiTK7IT"


def _room_topic(chan):
    """The encoded topic for a channel: the stored one, else the listed room's.

    The persistent rooms are configuration, not something anybody created over
    IRC, so nothing ever called `TOPIC` to give them one. Without this a joiner
    gets no `332` and has no name for the room it just walked into.
    """
    stored = roomregistry.ROOMS.topic(chan)
    if stored:
        return stored
    for room in lobbyrooms._room_list():
        if lobbyrooms._room_chan(room) == chan:
            prefix = os.environ.get("POL_ROOM_TOPIC_PREFIX", _ROOM_TOPIC_PREFIX)
            return (prefix + room["handle_name"]).encode("cp932", "replace")
    return None


#: Mode letters that consume an argument, RFC 1459.
_MODE_ARG_FLAGS = b"klbovI"


def _parse_mode_change(arg):
    """`MODE <chan> +nlk 10 waaa` -> {"n": True, "l": 10, "k": b"waaa"}.

    Bans come back as "b_add"/"b_del" lists. Anything unrecognised is ignored
    rather than guessed at -- an unknown letter must not silently clear a real one.
    """
    # MULTI-SPEC LINES ARE REAL: the master handoff arrives as
    # `MODE <chan> +o <new> -o <old>` (measured live 2026-08-19T00:48), two
    # flag tokens with their nick arguments interleaved. The old walk read
    # `parts[1:2]` -- the FIRST spec only -- so the whole handoff parsed to
    # nothing and the room's '@' never moved.
    parts = arg.split()
    out = {"b_add": [], "b_del": [], "o_add": [], "o_del": []}
    toks, i = parts[1:], 0
    while i < len(toks):
        spec = toks[i]
        i += 1
        if spec[:1] not in (b"+", b"-"):
            continue                      # a stray argument with no spec
        adding = True
        for ch in spec.decode("latin1", "replace"):
            if ch in "+-":
                adding = ch == "+"
                continue
            a = None
            if ch.encode() in _MODE_ARG_FLAGS and i < len(toks) \
                    and toks[i][:1] not in (b"+", b"-"):
                a = toks[i]
                i += 1
            if ch == "n":
                out["n"] = adding
            elif ch == "l":
                try:
                    out["l"] = int(a) if adding and a is not None else None
                except ValueError:
                    pass
            elif ch == "k":
                out["k"] = a if adding else None
            elif ch == "b" and a is not None:
                out["b_add" if adding else "b_del"].append(a)
            elif ch == "o" and a is not None:
                out["o_add" if adding else "o_del"].append(a)
    return out


def _mode_string(m):
    """{"n","l","k"} -> SE's `+nlk 10 waaa`, or b"" when nothing is set."""
    letters, args = b"", []
    if m.get("n"):
        letters += b"n"
    if m.get("l") is not None:
        letters += b"l"
        args.append(str(m["l"]).encode())
    if m.get("k"):
        letters += b"k"
        args.append(m["k"])
    if not letters:
        return b""
    return b"+" + letters + (b" " + b" ".join(args) if args else b"")


def _chan_arg(arg):
    """The channel token from a command's argument, colon and NULs stripped.

    `PART :#chan` is the form the client actually sends (SE's capture and ours),
    so the leading ':' has to come off before the `#` test that every channel
    handler does. Reading the raw token instead silently drops the command.
    """
    tok = arg.split(None, 1)[0] if arg.split() else b""
    return tok.strip().rstrip(b"\x00").lstrip(b":")


#: *** A PERSISTENT ROOM IS NEVER EMPTY: SE KEEPS A PSEUDO-USER IN IT. ***
#: Decoded 2026-08-16 from the 2026-08-15 capture. Entering Novice_Hall, SE sent
#:
#:   353 UL0C0F1HJ = #01CPZYOTYU000003 :UL0C0F1HJ @PXANNNNXK
#:   352 ... 108.55.250.177 pol-1049-51244.pol.com UL0C0F1HJ H  :0 POL-INFO
#:   352 ... 202.67.54.139  202.67.54.139          PXANNNNXK H@ :2 *Not On This Net*
#:
#: -- the human joins PLAIN and a resident bot holds the operator flag, with the
#: server's own ADDRESS (not its name) in both the host and server slots, two hops
#: out, and a realname that says it is not a real user.
#:
#: WHY IT MATTERS: we gave the joiner the '@' in an otherwise empty channel, which
#: is precisely the shape of a CREATED room whose owner has left -- and the client
#: says so, in as many words: **"This room is already closed. Create a new room?"**
#: It was reading a listed room as a dead player room. The bot is what tells it the
#: room is a fixture that cannot close.
#:
#: The bot is a LISTED-ROOM fixture only. SE's created rooms carry no such user --
#: `#01CU` NAMES is `:@<creator>` on create and `:<joiner> @<owner>` for a visitor
#: -- so the create path this was validated against stays byte-for-byte unchanged.
_ROOM_BOT_NICK = b"PXANNNNXK"
_ROOM_BOT_REAL = b"*Not On This Net*"
_ROOM_BOT_HOPS = b"2"


def _room_bot_nick():
    return os.environ.get("POL_ROOM_BOT", "").encode() or _ROOM_BOT_NICK


def _is_listed_chan(chan):
    """True for a PERSISTENT room's channel.

    Deliberately not `_room_list()`: that now includes player-created rooms, and
    the resident bot is a fixture-only thing. SE's created rooms have no such user
    -- their operator is a real person, the creator.
    """
    return any(lobbyrooms._room_chan(r) == chan for r in lobbyrooms._persistent_rooms())


#: KEY: A USER PREFIX CARRIES **NO HOST**. Measured 2026-08-15 across every decoded
#: auth-band stream in the capture: **1055 of 1055** user-prefixed lines SE sent
#: have an empty host, with no exceptions and none by verb --
#:
#:     JOIN 91  KICK 21  MODE 60  NOTICE 589  PART 66  PRIVMSG 220  TOPIC 8
#:
#:     :UD5PUQZGA!~x@ JOIN :#XXL000000000002CF6D
#:     :UD5PUQZGA!~x@ PART #XXL000000000002CF6D :UD5PUQZGA
#:
#: SERVER prefixes are the other way round and keep their name
#: (`:pol-1049-51244.pol.com 353 ...`), so this is specifically the `nick!user@host`
#: form. The push channel already knew it -- `_push_deliver_watchers` builds
#: `!~x@` with nothing after it "exactly as captured" -- but every ROOM path
#: appended the server name, so each peer-visible line went out as
#: `:UD5PUQZGA!~x@pol-1000-51241.pol.com JOIN :#XXL...` where SE sends
#: `:UD5PUQZGA!~x@ JOIN :#XXL...`.
#:
#: That is the whole difference between a client that can attribute a JOIN, a
#: PART or a group NOTICE to a person and one that cannot -- which is what
#: "nobody shows as being in the room" looks like from the sidebar (reported
#: 2026-08-16). Not proven to BE that bug; it is the one peer-visible divergence
#: the capture actually shows, so it is the one to remove first.
#:
#: POL_IRC_PREFIX_HOST=1 restores the old form for a straight A/B.
def _irc_host(srv):
    """The host part of a USER prefix -- empty, as SE sends it."""
    if os.environ.get("POL_IRC_PREFIX_HOST", "0") == "1":
        return srv if isinstance(srv, bytes) else str(srv).encode()
    return b""


def _xxl_gid(chan):
    """The group id a `#XXL<16 hex>` channel names, or None if it is not one."""
    if not chan or not chan.startswith(b"#XXL"):
        return None
    try:
        return int(chan[4:].strip(), 16)
    except ValueError:
        return None


#: *** A GROUP'S CHANNEL IS FOR ITS MEMBERS. *** Without this gate nothing
#: checks: `JOIN #XXL<gid>` lets any signed-in client in (group ids are small
#: friend-row ids, so guessable), its NOTICE/PRIVMSG reaches every member,
#: WHO lists their nicks and addresses, a MODE +b/+k locks real members out,
#: and a KICK removes anyone -- none of which needs being in the room. Every
#: command naming a `#XXL` channel passes here first:
#:
#:   JOIN            only an accepted member or the owner (else 473, the +i
#:                   refusal, shaped like the 474/475 below)
#:   WHO / MODE?     a non-member gets the end of the list and nothing else
#:   NOTICE/PRIVMSG  only from a session that is IN the room (JOIN is gated)
#:   TOPIC set       likewise
#:   MODE change     master or sub-master, and only +o/-o (roles are 7:3's;
#:                   +b/+k/+l have no use in a group channel)
#:   KICK            master or sub-master
#:
#: The channel must be spelled as the client builds it (`#XXL%08X%08X`,
#: upper case): ROOMS keys on the exact bytes, so a lower-case spelling would
#: be a second, ungated copy of the room. POL_GROUP_GATE=0 turns it all off.
def _xxl_gate(verb, arg, nick, srv, sess):
    """None = carry on; a list = the whole reply (empty = say nothing)."""
    if verb not in (b"JOIN", b"WHO", b"MODE", b"NOTICE", b"PRIVMSG", b"TOPIC", b"KICK"):
        return None
    chan = _chan_arg(arg)
    gid = _xxl_gid(chan)
    if gid is None or os.environ.get("POL_GROUP_GATE", "1") == "0":
        return None
    who = nick.decode("latin1", "replace")
    refuse = [b":" + srv + b" 473 " + nick + b" " + chan + b" :Cannot join channel (+i)"]
    if chan != b"#XXL%016X" % gid:
        log("authserv", f"  group gate: {who} {verb.decode()} {chan.decode('latin1')} "
                        f"-- not the canonical spelling; refused")
        return refuse if verb == b"JOIN" else []
    in_room = sess is not None and sess in roomregistry.ROOMS.members(chan)
    if verb in (b"NOTICE", b"PRIVMSG"):
        # In the room, or a member whose session outlived an authsess restart
        # (it keeps talking before it re-JOINs, as it always could).
        if in_room or _xxl_class(sess, gid) is not None:
            return None
        log("authserv", f"  group gate: {who} {verb.decode()} to {chan.decode('latin1')} "
                        f"without being in it; dropped")
        return []
    cls = _xxl_class(sess, gid)
    words = arg.split()
    if verb == b"JOIN":
        if cls is not None:
            return None
        log("authserv", f"  group gate: {who} refused {chan.decode('latin1')} -- "
                        f"not a member of group {gid}")
        return refuse
    if verb == b"WHO" or (verb == b"MODE" and len(words) == 1):
        if cls is not None:
            return None
        return ([b":" + srv + b" 315 " + nick + b" " + chan + b" :End of WHO list."]
                if verb == b"WHO" else [])
    if verb == b"TOPIC":
        _, sep, _ = arg.partition(b" :")
        return None if (not sep and cls is not None) or (sep and in_room) else []
    if verb == b"MODE":
        flags = words[1] if len(words) > 1 else b""
        if cls is not None and cls >= accounts.GROUP_CLASS_SUBMASTER and \
                flags.strip(b"+-").strip(b"o") == b"":
            return None
    elif verb == b"KICK":
        if cls is not None and cls >= accounts.GROUP_CLASS_SUBMASTER:
            return None
    log("authserv", f"  group gate: {who} {verb.decode()} "
                    f"{arg[:60].decode('latin1', 'replace')} refused (class {cls})")
    return []


def _xxl_class(sess, gid):
    """The session member's class in group `gid`, or None (not in it, or unknown).

    Fails CLOSED: no member behind the session, or a database error, is None."""
    mid = chatsession._sess_member_id(sess) if sess is not None else None
    if mid is None:
        return None
    try:
        db = accounts.connect()
        try:
            return accounts.group_class_of(db, gid, member_id=mid)
        finally:
            db.close()
    except Exception as exc:
        log("authserv", f"  group gate: class lookup for member {mid} in {gid} "
                        f"failed ({exc!r}); treated as not a member")
        return None


def _who_here_flag(sess=None, member_id=None):
    """`H` or `G` for one row of the 352 WHO -- the member's LIVE presence.

    **RETAIL SERVES `G@`, NOT `H@`, WHEN THE MEMBER IS AWAY.** Measured
    2026-08-19 off a two-member retail group-chat capture (memory
    `group-chat-retail-decode`): Fox was AFK and SE's own 352 for them read
    `... UL0C0F1HJ G@ :0 POL-INFO`. RFC 1459 calls the field H(ere)/G(one) and
    SE uses it exactly that way; we hardcoded `H`, so an away member showed as
    present in every room they sat in.

    TWO SOURCES, in order, because they are two different statements:

      1. the member's own **4:5 KChangeMyStatus** code (`_member_status`) -- the
         explicit choice the user made in the Viewer's status menu, and the one
         SE reflects here. It crosses from the lobby container through
         `_publish_member_status`.
      2. the session's IRC **AWAY** state, for a client that announced its
         availability that way and never sent a 4:5. `presence-is-afk`: in this
         protocol presence IS the IRC away-ness, so the two are the same fact
         arriving by two doors.

    Neither one saying anything means `H`. That is deliberate: an absent record
    means "the client never told us", and answering `G` for someone plainly
    sitting in the room would be a worse lie than the `H` this replaced.
    """
    if member_id is None and sess is not None:
        member_id = chatsession._sess_member_id(sess)
    if member_id is not None and memberstatus._status_is_away(memberstatus._member_status(member_id)):
        return b"G"
    if sess is not None and getattr(sess, "away", False):
        return b"G"
    return b"H"


def _group_op_nicks(chan, members):
    """The nicks in `members` that should carry '@' in a GROUP channel.

    **In a group channel the operator is the group's MASTER, not whoever joined
    first.** SE's own 353 for `#XXL000000000002CF6D` reads

        :UL0C0F1HJ @UD5PUQZGA

    with the '@' on the member the 7:12 reply gives class 5 -- and SE moves it
    when the class moves (see `_group_mode_push`). `ROOMS.owner()` is join order,
    which is right for a chat ROOM the joiner created and wrong here: the master
    may not even be the first one in.

    Returns None for a channel that is not a group, so the caller keeps its
    existing owner-based behaviour untouched.
    """
    gid = _xxl_gid(chan)
    if gid is None or accounts is None or not members:
        return None
    try:
        db = accounts.connect()
        try:
            masters = {nm for _g, nm, cls in accounts.list_group_members(db, gid)
                       if int(cls) >= accounts.GROUP_CLASS_MASTER}
            if not masters:
                return set()
            out = set()
            for m in members:
                mid = chatsession._sess_member_id(m)
                if mid is None:
                    continue
                hn = accounts.primary_handle(db, mid)
                if hn and hn in masters:
                    out.add(m.nick)
            return out
        finally:
            db.close()
    except Exception as exc:
        log("authserv", f"  group ops for {chan!r}: lookup failed ({exc!r}); "
                        f"falling back to join order")
        return None


def _gm_roster_nick(chan):
    """The GM's nick, when `chan` is a GM Call chat room. None otherwise.

    A GM has no session (its lines come off the spool), so it never appears in
    ROOMS and therefore never in NAMES or WHO. It still has to be in the roster
    the client sees, or the client cannot resolve it as a speaker.
    """
    try:
        import gmchat
    except ImportError:
        return None
    return gmchat.GM_NICK if gmchat.is_gm_room(chan) else None


def _names_line(chan, nick, srv, sess=None):
    """SE's 353, captured verbatim: `353 <nick> = <chan> :@<owner> <other> `.

    The '@' operator prefix and the TRAILING SPACE are both from the real capture
    (2026-08-11) -- keep them. With one member this is byte-identical to what we
    sent before the registry existed, which is the shape the working chat-room
    create was validated against.
    """
    if _is_listed_chan(chan):
        # Everyone present is plain; the resident bot carries the '@', last, as SE
        # orders it. Without a registry this is just the joiner plus the bot.
        members = [m.nick for m in roomregistry.ROOMS.members(chan)] if sess is not None else []
        if nick not in members:
            members.append(nick)
        names = b" ".join(members) + b" @" + _room_bot_nick()
        return b":" + srv + b" 353 " + nick + b" = " + chan + b" :" + names + b" "
    members = roomregistry.ROOMS.members(chan) if sess is not None else []
    # *** THE GM IS A ROOM MEMBER TOO. *** It has no session -- its lines are
    # spooled from a file -- so it is not in ROOMS and was absent from every 353.
    # The client resolves a PRIVMSG sender against the channel roster before it
    # will build a member row for it, so a GM missing from NAMES can talk into a
    # room and never be heard. Added here rather than as a fake session, which
    # would have to survive ROOMS.broadcast() calling .send() on it.
    gm = _gm_roster_nick(chan)
    # GHOSTS ARE STILL IN THE ROOM. A member whose socket died with a restart
    # (their client kept its session through the relay) stays in NAMES until
    # they are adopted back or expire -- without this, a joiner right after a
    # deploy sees an empty room, gets made owner of it, and the survivor
    # becomes invisible. See RoomRegistry._ghosts.
    ghosts = roomregistry.ROOMS.ghost_nicks(chan) if sess is not None else []
    present = [m.nick for m in members]
    ghosts = [g for g in ghosts if g not in present and g != nick]
    if not members and not ghosts:
        extra = (b" " + gm) if gm else b""
        return (b":" + srv + b" 353 " + nick + b" = " + chan +
                b" :@" + nick + extra + b" ")
    ops = _group_op_nicks(chan, members)
    if ops is None:
        # the explicit op set once a master handoff has happened, else the owner
        ops = roomregistry.ROOMS.op_nicks(chan)
    names = b" ".join((b"@" if m.nick in ops else b"") + m.nick for m in members)
    for g in ghosts:
        names += (b" " if names else b"") + (b"@" if g in ops else b"") + g
    if nick not in present and nick not in ghosts:
        # the JOINER is in their own NAMES on every SE capture; a restored room
        # whose members are all ghosts must not drop them
        names = ((b"@" if nick in ops else b"") + nick +
                 (b" " + names if names else b""))
    if gm and gm not in present:
        names += b" " + gm
    return b":" + srv + b" 353 " + nick + b" = " + chan + b" :" + names + b" "


def _auth_session_reply(cmd_txt, nick, srv, peer_ip=b"0.0.0.0", sess=None):
    """Answer an in-session command on the auth band. None = say nothing.

    `sess` is this connection's ChatSession when the caller has one (the welcome
    hop). Without it every room behaves exactly as it did before the registry
    existed -- a room of one -- so the redirect hops and the unit tests are
    unaffected.

    Deliberately small. Only commands whose reply we can state from the IRC
    numerics POL inherited go in here; anything else returns None and is merely
    logged, which is byte-identical to the pre-2026-08-11 silent behaviour. That
    keeps this from becoming a place where guessed replies get invented -- a
    wrong-shaped answer is how POL-5135 and POL-0008 got raised elsewhere.
    """
    # A previous process's rooms come back before the first command is judged
    # -- one flag test after the first call, so the hot path does not pay.
    lobbyrooms._restore_rooms_once()
    # Callers hold the server name as a str (`prefix`) and the nick as bytes, so
    # normalise both rather than making every call site remember which is which --
    # that mismatch raised TypeError inside the observe loop on the first live
    # AWAY, which aborted the handler and dropped the socket.
    if isinstance(srv, str):
        srv = srv.encode()
    if isinstance(nick, str):
        nick = nick.encode()
    if isinstance(peer_ip, str):
        peer_ip = peer_ip.encode()
    parts = cmd_txt.split(None, 1)
    if not parts:
        return None
    verb = parts[0].upper()
    arg = parts[1] if len(parts) > 1 else b""
    gated = _xxl_gate(verb, arg, nick, srv, sess)
    if gated is not None:
        return gated or None
    handler = AUTH_VERBS.get(verb)
    if handler is None:
        return None
    return handler(arg, nick, srv, peer_ip, sess)


def _verb_notice(arg, nick, srv, peer_ip, sess):
    # This is how a launched GAME talks to us -- see _game_notice_reply.
    #
    # WARNING: RETURN ONLY IF IT ACTUALLY HANDLED IT. This used to `return` the call
    # unconditionally, and `_game_notice_reply` returns None for anything that
    # is not a game envelope -- so EVERY ordinary NOTICE ended here and the
    # channel relay 250 lines below was unreachable code. That relay is group
    # chat: the client publishes both its chat lines and its member/presence
    # records as `NOTICE #XXL<id> :<payload>`, and dropping them is why group
    # messages never appeared for anyone and the member sidebar stayed empty.
    # Seen live 2026-08-16: `NOTICE #XXL0000000000000002 :TATTTTTTztSKTTTT`
    # arrived and produced no reply, no relay and no log line at all.
    reply = gamenotice._game_notice_reply(arg, nick, srv, sess=sess)
    if reply is not None:
        return reply
    # otherwise fall through to the channel relay

    if not (arg.startswith(b"#")):
        return None
    # GROUP CHAT'S OWN CHANNEL. The client publishes its member/presence
    # records here, not as PRIVMSG -- observed on our own auth band as
    #     NOTICE #XXL0000000000000002 :G<base-64 payload>
    # and these used to be eaten by the game-envelope path above. Relay them
    # to the OTHER members exactly as chat is relayed; SE echoes nothing back
    # to the sender (checked in the 2026-08-15 capture: not one PRIVMSG or
    # NOTICE of the account's own came back), so neither do we.
    #
    # TARGET FORM IS A KNOB. Peers' notices arrive from SE addressed to the
    # RECIPIENT's nick (`:<peer>!~x@ NOTICE <mynick> :<payload>`), which a
    # single broadcast line cannot express, so `chan` (the sender's own form)
    # is the default and `nick` re-addresses per member.
    target, _, body = arg.partition(b" :")
    target = target.strip()
    if sess is None or not body:
        return None
    # *** THE FORM IS PER CHANNEL CLASS, and only ONE class is measured. ***
    #
    # `#XXL` group chat: the 2026-08-19 retail decode gives the in-room chat
    # line as `:<nick>!~x@ NOTICE #XXL<16hex> :<body>` and calls our envelope
    # a byte-for-byte PASS. That is `chan`, it is proven, and it does not
    # move.
    #
    # A TETRA MASTER ROOM IS NOT THAT CHANNEL, and `chan` is measured NOT to
    # work there. Live, 2026-08-20, two members in #<title>R001:
    #
    #     00:30:11  NOTICE #<title>R001 :G<tag>G42000800@Chat=/La=1/Dt=#CHAT#\tHI...
    #     00:30:11  room #<title>R001: relayed NOTICE from UF8TOQDTX to 1 member(s)
    #
    # -- delivered, and the receiving client drew nothing (verified in game on
    # both screens). Every OTHER game envelope that client accepts is
    # addressed to its own nick, and the comment above records SE's peer
    # notices arriving that way too, so `nick` is the experiment: same body,
    # same prefix, re-addressed per member.
    #
    # WARNING: THIS IS A PROBE, NOT A MEASUREMENT. `POL_ROOM_NOTICE_TARGET_GAME=chan`
    # puts it back. If `nick` does not draw either, the target form is NOT the
    # gate and the next suspect is the receiving client's channel roster --
    # the Members sidebar is empty on both screens, the client never sends a
    # WHO for the room (zero 352/315 for #<title>R001 in the whole log), so its
    # only roster inputs are the join 353 and our JOIN broadcasts.
    if target.startswith(b"#XXL"):
        form = os.environ.get("POL_ROOM_NOTICE_TARGET", "chan")
    else:
        form = os.environ.get("POL_ROOM_NOTICE_TARGET_GAME", "nick")
    prefix = b":" + nick + b"!~x@" + _irc_host(srv) + b" NOTICE "
    sent = 0
    # A TITLE MAY REWRITE ITS CHAT LINE BEFORE THE RELAY, and then it goes
    # to the SENDER as well (Tetra Master's client does not draw its own
    # line): re-broadcast in the msgid-4 form, or with the sender's name
    # filled in, because that client discards a line whose name part is
    # empty. See `titles.Title.room_notice`.
    _rw = titles.room_notice(body, sess, nick)
    if _rw is not None:
        _how, _rbody = _rw
        for m in roomregistry.ROOMS.members(target):
            if not m.alive:
                continue
            who = m.nick if form == "nick" else target
            if m.send([prefix + who + b" :" + _rbody]):
                sent += 1
        log("authserv",
            f"  room {target.decode('latin1')}: title chat from "
            f"{nick.decode('latin1')} relayed {_how.upper()} to {sent} "
            f"member(s) [{form}, sender included]")
        return None
    if form == "nick":
        for m in roomregistry.ROOMS.members(target):
            if m is sess or not m.alive:
                continue
            if m.send([prefix + m.nick + b" :" + body]):
                sent += 1
    else:
        sent = roomregistry.ROOMS.broadcast(target, [prefix + target + b" :" + body],
                               exclude=sess)
    log("authserv", f"  room {target.decode('latin1')}: relayed NOTICE from "
                    f"{nick.decode('latin1')} to {sent} member(s) [{form}]")
    return None


def _verb_away(arg, nick, srv, peer_ip, sess):
    # RFC 1459: no argument clears away (305), an argument sets it (306).
    # Captured live as the command the chat-room creation blocks on.
    is_away = bool(arg.strip().lstrip(b":"))
    # REMEMBER IT ON THE SESSION. The broadcast below tells this member's
    # FRIENDS; the flag tells the room -- `_who_here_flag` reads it for the
    # 352's H/G letter, which retail derives from live presence and we used
    # to hardcode as H (retail capture decode). Same fact, two
    # audiences, and only the friend half was being served.
    if sess is not None:
        sess.away = is_away
    # RELAY the away-state to this member's online friends. AWAY is the client
    # announcing its OWN availability, so it is the one presence change we see
    # explicitly on the wire (login/logout are inferred from the socket). The
    # broadcast is a no-op unless POL_PRESENCE_PUSH is on, so this is inert
    # today; the reply to the sender below is unchanged and still fires.
    if sess is not None and getattr(sess, "member", None) is not None:
        try:
            mrow = sess.member
            pushrecord._broadcast_presence(int(mrow["id"]), "away" if is_away else "back",
                                subject_name=mrow["login_name"]
                                if "login_name" in mrow.keys() else None)
        except Exception as exc:
            log("authserv", f"presence: AWAY relay failed ({exc!r})")
    if is_away:
        return [b":" + srv + b" 306 " + nick +
                b" :You have been marked as being away"]
    return [b":" + srv + b" 305 " + nick +
            b" :You are no longer marked as being away"]


def _verb_join(arg, nick, srv, peer_ip, sess):
    # Chat rooms ARE IRC channels. Captured live 2026-08-11, straight after the
    # AWAY it was blocking on:
    #     JOIN #01CUJZNNNNO0BKPCOEH1NWOAE2ENNNNNNNNNNNNN3N1N
    # The name is not opaque: strip the leading "#01CU" and the remaining 40
    # symbols are a standard base-32 REDIRECT RECORD -- _CONST_48 matches at
    # [4:8] and it decodes to 127.0.0.1:51242, i.e. OUR address on the
    # next-hop auth port this node advertises. So the client derives the room's
    # host from our own redirect chain and names the channel after it.
    #
    # The reply is the RFC 1459 join sequence: echo, topic, names, end-of-names.
    # That much is standard and unambiguous; what is NOT yet known is whether
    # POL expects anything extra, so this stays minimal and the log will show
    # whatever the client asks for next.
    chan = arg.split(None, 1)[0].strip().rstrip(b"\x00") if arg.split() else b""
    if not chan:
        return None
    # A GM CALL ROOM IS CONFIDENTIAL: only the player who filed that request, or
    # a player the desk invited, gets in (gmchat.room_allowed). 473 is IRC's
    # invite-only refusal.
    if _gm_roster_nick(chan):
        import gmchat
        member = getattr(sess, "member", None)
        if not gmchat.room_allowed(chan, member):
            log("authserv", f"  GM room {chan.decode('latin1')}: {nick!r} "
                            f"(member {gmchat.member_key(member)}) REFUSED -- not the requester "
                            f"and not invited")
            return [b":" + srv + b" 473 " + nick + b" " + chan +
                    b" :Cannot join channel (+i)"]
    # *** THE PASSWORD IS CHECKED HERE, AND IT IS THE ONLY PLACE IT CAN BE. ***
    # `JOIN <chan> <key>` -- the key is the second token, in the clear. SE's own
    # rejection, captured 2026-08-15 when the account holder typed the wrong one:
    #
    #   C->S JOIN #01CUPQNZ... PASS
    #   S->C 475 <nick> #01CUPQNZ... :Cannot join channel (+k)
    #   C->S JOIN #01CUPQNZ... waaa          <- the real key
    #   S->C <the join sequence>
    #
    # The creator's own JOIN carries the password BEFORE they set `+k`, so a
    # room with no key stored lets everybody in -- which is exactly right, and
    # is why the create path is unaffected.
    # A BAN OUTLASTS THE KICK. `MODE <chan> +b <handle-nick>` is how the client
    # bans, and without this the person walks straight back in -- the kick is
    # only the eviction. 474 is RFC 1459's counterpart to the 475 below;
    # unlike the 475 it was never captured, so it is the one guessed shape
    # here, and a wrong numeric names itself in the client's error table.
    if nick in roomregistry.ROOMS.modes(chan).get("b", ()):
        log("authserv", f"  room {chan.decode('latin1')}: refused "
                        f"{nick.decode('latin1')} -- banned (+b)")
        return [b":" + srv + b" 474 " + nick + b" " + chan +
                b" :Cannot join channel (+b)"]
    want_key = roomregistry.ROOMS.modes(chan).get("k")
    if want_key:
        given = arg.split()[1] if len(arg.split()) > 1 else b""
        if given != want_key:
            log("authserv", f"  room {chan.decode('latin1')}: refused "
                            f"{nick.decode('latin1')} -- "
                            + ("wrong password" if given else "no password given"))
            return [b":" + srv + b" 475 " + nick + b" " + chan +
                    b" :Cannot join channel (+k)"]
    # EXACT SE SHAPES, captured 2026-08-11 from a genuine Square Enix session
    # (MITM capture mode + the ircLineIn probe, which reads lines AFTER polcore
    # decrypts them). Replaces the RFC-1459 guesses that were here:
    #
    #   353 <nick> = <chan> :@<nick>        <- '@' operator prefix, trailing space
    #   366 <nick> <chan> :End of NAMES list.   <- a PERIOD, and no '/' in "NAMES"
    #
    # SE sends NO 331/332/333 for a new room. That is a real negative, not an
    # omission in the capture: numerics are exactly what this probe records, so
    # a topic line would have shown up.
    #
    # VERIFIED: THE JOIN ECHO IS SE'S TOO -- settled 2026-08-25, and this used to
    # read "its absence proves nothing" because the `[irc]` narrator only
    # decodes numerics. It does not have to: in SE's own group-chat join
    # (polshim-se.429364.log:86586) the 220-byte frame carrying the 353 and
    # the 366 holds THREE lines, and the first is 45 bytes -- exactly the
    # 41-char `:UL0C0F1HJ!~x@ JOIN :#XXL000000000002CF6D` plus `frame_line`'s
    # 4-char checksum. Lines 2 and 3 share the 25-byte ciphertext prefix that
    # is the `:pol-NNNN-NNNNN.pol.com 3` tell, line 1 does not. So the joiner
    # gets their own JOIN back, and this is not a place to look when a member
    # appears twice (that was the roster push's identity -- see
    # `_push_deliver_grouprows`).
    # MULTI-MEMBER (2026-08-12). Register, tell the people already in the room
    # that someone arrived, and answer the joiner with the REAL member list.
    # With no session, or a room of one, the three lines below are byte-for-byte
    # what we sent before the registry existed.
    if sess is not None:
        others, is_owner = roomregistry.ROOMS.join(chan, sess)
        # *** THE CREATE-JOIN IS WHERE THE PASSWORD ARRIVES, so STORE IT.
        # *** SE's flow ("JOIN <chan> <pass>" then "MODE +nlk 10 <pass>")
        # was read as "the +k will follow", and on this client build it
        # never does -- the live creates send `+n` and `+l 10` as separate
        # lines and no `+k` at all (measured 2026-08-19, three rooms), so a
        # room created WITH a password challenged nobody. The key token on
        # the join that CREATES a #01CU room is the only statement of the
        # password we get; a joiner's key token on an existing room is
        # their ANSWER to the prompt and is checked above, never stored.
        given = arg.split()[1] if len(arg.split()) > 1 else b""
        if is_owner and not others and given and chan.startswith(b"#01CU") \
                and not roomregistry.ROOMS.modes(chan).get("k") \
                and not roomregistry.ROOMS.ghost_nicks(chan):
            roomregistry.ROOMS.set_modes(chan, {"k": given})
            log("authserv", f"  room {chan.decode('latin1')}: created with "
                            f"a password (stored from the create-JOIN)")
        if others:
            # The arrival echo is the same prefixed JOIN the joiner gets. IRC
            # servers send exactly this to the rest of the channel, and the
            # client has always accepted our prefixed JOIN for itself.
            roomregistry.ROOMS.broadcast(
                chan, [b":" + nick + b"!~x@" + _irc_host(srv) + b" JOIN :" + chan],
                exclude=sess)
    out = [b":" + nick + b"!~x@" + _irc_host(srv) + b" JOIN :" + chan]
    # 332 GOES HERE, BETWEEN THE ECHO AND THE NAMES, and only when the room
    # ALREADY HAS A NAME. Both halves are SE's, decoded 2026-08-16 from the
    # 2026-08-15 capture:
    #
    #   creating a room     JOIN echo, 353, 366                 -- no 332
    #   entering FOXROOM    JOIN echo, 332 :zt9rTojTK7ITCASROOM, 353, 366
    #
    # So the "SE sends NO 331/332/333" note above was right about a room being
    # CREATED and wrong as a general rule. A joiner has no other source for the
    # room's name -- it lives in the topic, which is why the room search's
    # predicate is `z_topic` -- and our client walked straight back out of
    # Novice_Hall (JOIN, WHO, MODE, PART, 0.3s) with this missing.
    topic = _room_topic(chan)
    if topic:
        out.append(b":" + srv + b" 332 " + nick + b" " + chan + b" :" + topic)
    out.append(_names_line(chan, nick, srv, sess))
    out.append(b":" + srv + b" 366 " + nick + b" " + chan +
               b" :End of NAMES list.")
    # THE GM ARRIVES TOO, in a GM Call room. The Viewer's GM chat member table
    # is rebuilt from polcore's channel member list (app.dll 0x4ab3754 via
    # cft_1256), and polcore adds a member on a JOIN (0x037d7910) or a 352.
    # The GM has no session, so no JOIN of its own ever reached the client and
    # its row never stuck. Announce it, then send its `HA...:G` roster record
    # at once so the row gets the GM role and name without waiting for the
    # client's `HR`. POL_GMCHAT_ANNOUNCE=0 turns this off.
    gm = _gm_roster_nick(chan)
    if gm and os.environ.get("POL_GMCHAT_ANNOUNCE", "1") != "0":
        try:
            import gmchat
            host = _irc_host(srv)
            out.append(b":" + gm + b"!~x@" + host + b" JOIN :" + chan)
            out.append(gmchat.privmsg(chan, gmchat.encode_roster(chan), host))
        except Exception as exc:
            log("authserv", f"  GM announce for {chan!r} skipped ({exc!r})")
    return out


def _verb_part(arg, nick, srv, peer_ip, sess):
    # *** THE CLIENT SENDS `PART :#chan`, WITH THE COLON. *** Confirmed in
    # SE's own capture (`C->S PART :#01CU...`) and in our authserv log, where
    # every single PART in the capture run logged "no handler for b'PART'" -- the
    # channel token kept its leading ':', failed the `#` test, and the leave
    # fell out here. Nobody was ever removed from a room until their socket
    # died, so a member who walked out stayed in NAMES and WHO for everyone
    # else, and re-entering the room found themselves already in it.
    #
    # SE answers `:<nick>!~x@ PART <chan> :<nick>` -- the trailing part-message
    # is the leaver's own nick, which the guessed form here did not carry.
    chan = _chan_arg(arg)
    # \U0001f534 THE PS2 LEAVES VS. COM WITH A CHANNEL-LESS `PART :`, AND AN
    # UNANSWERED ONE HANGS IT.
    #
    # MEASURED 2026-09-09. `CComPart` state 2 calls `sqMgPartChannel`
    # (`0x003ab540` -> `0x00408b78`) on channel slot 0, and state 3 polls
    # `sqMgCommandReqCheck` (`0x003ab660` -> `0x00408d48`) until it returns
    # **> 0**. The console's channel table at `0x005C6C40` is ALL ZEROS in the
    # hang savestate -- VS. COM never joins an IRC channel -- so the name it
    # parts is empty and the wire line is literally `PART :`. Our handler
    # required a leading `#`, so every one of those logged
    # "no handler for b'PART'" and the client sat in state 3 until it showed
    # **`0-37160` "Timed out while disconnecting from the server."**
    #
    # \u26a0 It must be a SUCCESS echo, not an error numeric: state 3 does
    # `blez` on the result, so 442/ERR_NOTONCHANNEL would still fail (as 158).
    #
    # If the session really is in a channel we part that one properly; a
    # channel-less PART from a session in no channel is acknowledged by
    # the title that knows what its client expects (the exact-mirror echo
    # the console accepts, pinned live 2026-09-09; see the Tetra Master
    # title's `part_echo`) and nothing is mutated.
    if not chan and sess is not None:
        _acks = titles.part_echo(nick, srv, sess)
        if _acks:
            return _acks
        _mine = roomregistry.ROOMS.channels_of(sess)
        if _mine:
            chan = _mine[0]
    if not chan.startswith(b"#") or sess is None:
        return None
    line = b":" + nick + b"!~x@" + _irc_host(srv) + b" PART " + chan + b" :" + nick
    before = roomregistry.ROOMS.owner(chan)
    roomregistry.ROOMS.part(chan, sess)
    # VERIFIED: AN EXPLICIT PART IS AN AFFIRMATIVE DEPARTURE, and it is the signal
    # a title's room roster has never had. Its presence sync is add-only ON
    # PURPOSE -- absence from the registry is not a departure, because a
    # blipping auth session drops rows and keying presence to the socket is
    # what made people blink out of each other's lists. But this is not
    # absence: the client SAID it left.
    #
    # Without it a member record outlives the room. Reported 2026-08-20:
    # player 2 had joined only the ZONE, had not picked a room, and still
    # appeared in Freewheeler Room 1's member list -- from a record left over
    # by an earlier visit that nothing ever retired. The client never sends a
    # `stat = 0` record and never sends `@GameExit=`, so PART is the only
    # affirmative departure either side of this protocol produces.
    titles.room_parted(chan)
    after = roomregistry.ROOMS.owner(chan)
    lines = [line]
    # THE MASTER MOVED -> tell the room, so the survivor's client redraws the
    # '@' without a re-enter. Server-sourced MODE, same shape SE uses for an
    # op grant. op_nicks/NAMES already agree via the registry; this is only
    # the live push.
    if after and after != before and after != nick:
        lines.append(b":" + _irc_host(srv) + b" MODE " + chan
                     + b" +o " + after)
    roomregistry.ROOMS.broadcast(chan, lines)
    return lines


def _verb_kick(arg, nick, srv, peer_ip, sess):
    # SE's own line, captured 2026-08-15:
    #   :UL0C0F1HJ!~x@ KICK #01CU... UD5PUQZGA :UL0C0F1HJ
    # -- kicker's prefix, and the comment is the kicker's nick. Everyone in the
    # room sees it, the target included: that is how their client learns it has
    # been thrown out, and it is the only notice it gets.
    parts = arg.split()
    if len(parts) < 2 or sess is None:
        return None
    chan, victim = _chan_arg(arg), parts[1].strip().rstrip(b"\x00")
    if not chan.startswith(b"#"):
        return None
    line = (b":" + nick + b"!~x@" + _irc_host(srv) + b" KICK " + chan + b" " + victim +
            b" :" + nick)
    gone = [m for m in roomregistry.ROOMS.members(chan) if m.nick == victim]
    roomregistry.ROOMS.broadcast(chan, [line])          # the victim is still in, so is told
    for m in gone:
        roomregistry.ROOMS.part(chan, m)
    log("authserv", f"  room {chan.decode('latin1')}: "
                    f"{nick.decode('latin1')} kicked "
                    f"{victim.decode('latin1')} ({len(gone)} session(s))")
    return [line]


def _verb_who(arg, nick, srv, peer_ip, sess):
    # Sent immediately after our 366, against the same channel. RFC 1459 352:
    #   <channel> <user> <host> <server> <nick> <H|G>[*][@|+] :<hops> <real>
    # We are the only member (the 353 above said so), so one 352 then 315.
    chan = arg.split(None, 1)[0].strip().rstrip(b"\x00") if arg.split() else b""
    if not chan:
        return None
    # SE's exact 352, captured live:
    #   352 <nick> <chan> ~x <CLIENT-IP> <servername> <nick> H@ :0 POL-INFO
    # Two details our guess had wrong: the host field is the CLIENT's address
    # (not the server's), and the realname is the literal "POL-INFO".
    #
    # ONE 352 PER MEMBER (2026-08-12) -- each carrying that member's own nick
    # and IP, which is why ChatSession keeps peer_ip. The '@' in "H@" is the
    # operator flag, so it belongs to the room OWNER; with a single member (who
    # is always the owner) this is byte-identical to the captured line.
    members = roomregistry.ROOMS.members(chan) if sess is not None else []
    listed = _is_listed_chan(chan)
    # THE H/G LETTER IS PER ROW AND IT IS LIVE -- see `_who_here_flag`. Each
    # tuple below therefore carries its own flag rather than sharing one
    # constant: the whole point is that two people in one room can be in two
    # different states, which is what a hardcoded `H` could never say.
    if not members:
        # In a LISTED room nobody is the operator -- the resident bot is (see
        # `_names_line`), and the 353 the joiner just got says so. Handing the
        # human an '@' here would contradict it.
        rows = [(nick, peer_ip, not listed, _who_here_flag(sess))]
    else:
        # Same operator rule as 353 -- a group channel's '@' is its MASTER,
        # any other channel's is the creator. The two lines must agree: SE's
        # capture has the '@' of 353 and the `H@` of 352 on the same person,
        # and a client told two different things about who runs the room is
        # worse off than one told nothing.
        ops = _group_op_nicks(chan, members)
        if ops is None:
            # the explicit op set once a master handoff has happened (same
            # source as 353 -- the two lines must agree), else the owner
            ops = roomregistry.ROOMS.op_nicks(chan)
        rows = [(m.nick, m.peer_ip, (not listed) and m.nick in ops,
                 _who_here_flag(m))
                for m in members]
        # ghosts agree with 353 here too -- a restart survivor missing from
        # WHO while present in NAMES is two answers to one question
        present = {m.nick for m in members}
        # A GHOST'S SESSION IS GONE, so there is no `away` flag to read and
        # no member id to look one up by -- it renders `H`, the same answer
        # this whole builder used to give everyone.
        rows += [(g, peer_ip, (not listed) and g in ops, b"H")
                 for g in roomregistry.ROOMS.ghost_nicks(chan) if g not in present]
    # The GM has no session, so it is not in `members` -- but 352 and 353 have
    # to agree, and the client resolves a speaker against this roster.
    _gm = _gm_roster_nick(chan)
    if _gm and _gm not in [r[0] for r in rows]:
        rows.append((_gm, peer_ip, False, b"H"))
    out = [
        b":" + srv + b" 352 " + nick + b" " + chan + b" ~x " + mip +
        b" " + srv + b" " + mnick + b" " + here + (b"@" if op else b"") +
        b" :0 POL-INFO"
        for mnick, mip, op, here in rows
    ]
    if listed:
        # SE's own row for it, address in BOTH the host and server slots, two
        # hops out: `~x 202.67.54.139 202.67.54.139 PXANNNNXK H@ :2 *Not On
        # This Net*`. Ours is our own advertised address.
        ip = framing._lobby_world_ip().encode()
        out.append(b":" + srv + b" 352 " + nick + b" " + chan + b" ~x " + ip +
                   b" " + ip + b" " + _room_bot_nick() + b" H@ :" +
                   _ROOM_BOT_HOPS + b" " + _ROOM_BOT_REAL)
    return out + [
        b":" + srv + b" 315 " + nick + b" " + chan + b" :End of WHO list.",
    ]


def _verb_mode(arg, nick, srv, peer_ip, sess):
    # `MODE #chan` with no mode string is a QUERY -> 324 RPL_CHANNELMODEIS.
    # Only answered for channels: a user-mode query has a different reply and
    # has not been observed, so it stays silent rather than guessed.
    target = arg.split(None, 1)[0].strip().rstrip(b"\x00") if arg.split() else b""
    if not target.startswith(b"#"):
        return None
    if len(arg.split()) > 1:            # an actual mode CHANGE, not a query
        # *** STORE IT. *** Echoing a mode change without recording it is why a
        # room with a password let everyone in: `MODE <chan> +k PASS` is the
        # ONLY place the password is ever stated, and we replied "yes, +k PASS"
        # and remembered nothing. `+l` matters just as much -- it is where the
        # capacity in the browser's `N/M` comes from.
        roomregistry.ROOMS.set_modes(target, _parse_mode_change(arg))
        echo = b":" + nick + b"!~x@" + _irc_host(srv) + b" MODE " + arg
        # *** THE ROOM HEARS THE CHANGE TOO. *** Echoing only to the sender
        # is why a master handoff (`+o <new> -o <old>`, live 2026-08-19)
        # "didn't reflect on their client" -- the new master was never told.
        # IRC servers broadcast a channel MODE to the channel; so do we.
        if sess is not None:
            roomregistry.ROOMS.broadcast(target, [echo], exclude=sess)
        return [echo]
    # SE answers with a channel USER LIMIT, not the `+nt` we guessed -- and the
    # limit depends on WHICH KIND of channel it is. Captured 2026-08-11 from a
    # real session, there are two namespaces:
    #
    #   #01CU<40-symbol redirect token>   a created chat ROOM  -> +l 1
    #                                     (the client then sets +l 10 itself)
    #   #XXL<16 hex digits>               a ZONE / public area -> +l 64
    #
    # Zones are the same IRC mechanism: changing zone is a JOIN to a different
    # #XXL channel, so nothing new is needed beyond answering with the right
    # capacity. The ids run sequentially (0002CEF5, 0002CF31).
    #   #01CP<room id>                    a LISTED public room -> +l 21
    #                                     (measured on SE 2026-08-15: the
    #                                     account holder entered Novice_Hall
    #                                     as #01CPZYOTYU000003 and SE answered
    #                                     `324 ... +l 21`. The id is the one
    #                                     the 3:0 room record carries, so a
    #                                     listed room's channel name is
    #                                     "#01CP" + that id -- a THIRD
    #                                     namespace, not a spelling of #01CU.)
    if target.startswith(b"#XXL"):
        mode_str = os.environ.get("POL_ZONE_MODE", "+l 64").encode()
    elif _is_listed_chan(target):
        mode_str = os.environ.get("POL_LISTED_ROOM_MODE", "+l 21").encode()
    else:
        mode_str = os.environ.get("POL_ROOM_MODE", "+l 1").encode()
    # WHAT THE ROOM ACTUALLY SET WINS. SE's answer for the created, keyed
    # FOXROOM was `+nlk 10 waaa` -- flag letters in n,l,k order and their
    # arguments in the same order -- against `+l 1` for a room that had set
    # nothing yet. The defaults above are the "nothing set" case.
    built = _mode_string(roomregistry.ROOMS.modes(target))
    return [b":" + srv + b" 324 " + nick + b" " + target + b" " +
            (built or mode_str)]


def _verb_privmsg(arg, nick, srv, peer_ip, sess):
    # After the join sequence the client sends the ROOM DESCRIPTOR as a channel
    # PRIVMSG (ends with the owner's handle, carries a 64-bit id and 8 repeated
    # `000z` groups) and then waits -- the creation progress bar sticks at about
    # half. A strict IRC server does NOT echo a PRIVMSG to its sender, but with
    # one member in the channel that leaves the client with no confirmation the
    # room exists, which is exactly the observed stall. So broadcast it back,
    # sender included, which is what a chat server does with channel traffic.
    #
    # EXPERIMENT, not established: POL_AUTH_ECHO_PRIVMSG=0 disables it. If the
    # bar still sticks, the client wants a different message, not this one.
    if os.environ.get("POL_AUTH_ECHO_PRIVMSG", "1") != "1":
        return None
    target = arg.split(None, 1)[0].strip() if arg.split() else b""
    if not target.startswith(b"#"):
        # A WHISPER: PRIVMSG to a NICK, not a channel. Captured 2026-08-15 --
        # the two accounts whispered and the line is an ordinary
        # nick-targeted PRIVMSG, plaintext like room chat (it is group chat,
        # not whisper, that uses the base-64 UTF-16 encoding). These used to
        # fall off the end of this handler and vanish.
        #
        # Relayed verbatim under the SENDER's prefix, which is what the
        # recipient's client needs to attribute it. No echo to the sender:
        # SE sent none back in the capture.
        _, _, body = arg.partition(b" :")
        if not body or sess is None:
            return None
        sent = 0
        for ts in presence.PRESENCE.sessions_by_nick(target):
            if ts is sess:
                continue
            if ts.send([b":" + nick + b"!~x@" + _irc_host(srv) + b" PRIVMSG " +
                        target + b" :" + body]):
                sent += 1
        log("authserv", f"  whisper {nick.decode('latin1')} -> "
                        f"{target.decode('latin1')}: {sent} session(s)"
                        + ("" if sent else " -- not online, dropped"))
        # *** EVERY WHISPER SENDER GETS ITS 300 BACK, OR IT HANGS. *** The
        # "SE sent none back in the capture" note this replaces was FALSE --
        # only PRIVMSG-shaped replies had been checked, not numerics.
        # Re-read 2026-08-19 after a live sender hung (the recipient got the
        # whisper; the sender's client wedged): BOTH whisper sends in
        # auth429364.pkl are answered immediately with
        #     :pol-1049-51244.pol.com 300 UL0C0F1HJ UD5PUQZGA
        # -- the same `300 <sender> <target>` a channel PRIVMSG gets. The GM
        # special case below is therefore just the general rule.
        return [b":" + srv + b" 300 " + nick + b" " + target]
    # POL_AUTH_ROOM_CONFIRM picks what we send back after the room descriptor.
    # This is the one remaining unknown in the create sequence, and instrumenting
    # the client further stopped paying: the chat state machine's dispatchers are
    # hot multi-threaded loops, and the shim's INT3/single-step re-arm loses the
    # byte on the first concurrent hit (a probe there fires once and goes silent,
    # observed twice), while the cold `chatPoll` site re-arms fine. So these are
    # informed guesses, testable in one run each:
    #
    #   self  -- echo with the CLIENT's own prefix (what we have been doing).
    #            Suspect precisely because IRC clients conventionally ignore
    #            self-originated PRIVMSG, so the confirmation may never register.
    #   svc   -- echo the same descriptor from a SERVICE prefix, plus the topic
    #            pair 332/333. A room's authoritative state coming from the
    #            server rather than from yourself is what a chat service does,
    #            and 332/333 is the standard post-JOIN topic report we currently
    #            answer with 331 "no topic".
    #   both  -- send the self echo AND the service form.
    #
    # A wrong shape here is not silent: the Viewer's error table has a real IRC
    # protocol vocabulary (4500/4534/4571/4589, plus 4501 "the room was already
    # gone mid-entry" and 4590 "channel full"), so a malformed reply should name
    # itself rather than hang. None of those has ever been seen.
    # *** SOLVED from a real SE session, 2026-08-11. ***
    # SE answers the room descriptor with ONE line, the non-RFC numeric 300:
    #
    #     300 <nick> <channel>
    #
    # and the client immediately sends three more messages instead of waiting.
    # The interleave that proves it is a reply to the DESCRIPTOR and not to the
    # preceding MODE:
    #
    #     OUT 56   MODE
    #     IN       352 / 315 / 324 +l 1
    #     OUT 178  PRIVMSG <descriptor>
    #     IN       300 <nick> <chan>
    #     OUT 75, 59, 62      <- the client proceeds
    #
    # Every confirmation guessed before this (nothing / self-echo / service-echo
    # + topic) was wrong, and none of them was close: SE echoes no PRIVMSG here
    # at all. `POL_AUTH_ROOM_CONFIRM` keeps the old shapes for A/B only.
    mode = os.environ.get("POL_AUTH_ROOM_CONFIRM", "se")
    self_echo = b":" + nick + b"!~x@" + _irc_host(srv) + b" PRIVMSG " + arg
    se_300 = b":" + srv + b" 300 " + nick + b" " + target
    # TWO different PRIVMSGs ride this channel, and only the first wants a 300:
    #
    #   room descriptor : ":2000000...<64-bit id>00011<handle>"  -- one per create
    #   chat text       : ":0 0 02<handle>\t01<message>"         -- every line typed
    #
    # Confirmed live 2026-08-11 by the first working room: the user typed a
    # message and it arrived in the second shape. Answering chat with 300 is
    # wrong (300 means "room ready"), and in a multi-user room the server must
    # instead relay the line to the other members.
    #
    # DEFAULT KEEPS THE BEHAVIOUR THAT JUST WORKED. The room was created with
    # 300 answering both, so `both300` stays the default until a two-client test
    # says otherwise -- there is no evidence yet on whether the sender's own
    # client renders its text locally (standard IRC) or expects the echo.
    #   POL_ROOM_CHAT=both300   300 to everything (as tested, default)
    #   POL_ROOM_CHAT=relay     300 only to the descriptor; chat is echoed back
    #                           to the channel, which is what a real relay does
    #   POL_ROOM_CHAT=quiet     300 only to the descriptor; chat gets no reply
    body = arg.split(b":", 1)[1] if b":" in arg else b""
    is_chat = body.startswith(b"0 0 ") or b"\t" in body
    chat_mode = os.environ.get("POL_ROOM_CHAT", "both300")

    # *** RELAY EVERY CHANNEL PRIVMSG, NOT ONLY THE ONES THAT LOOK LIKE CHAT. ***
    # `is_chat` used to gate this, and that gate is why a room with two people
    # in it showed one. The codes are not decoration -- they are the membership
    # protocol, and SE relays all of them. From its own capture, everything Fox
    # RECEIVED on #01CPZYOTYU000003 and #01CU...:
    #
    #   :<them> PRIVMSG <chan> :2…0001 1 Cyn    ARRIVED -- carries the DISPLAY NAME
    #   :<them> PRIVMSG <chan> :3…0001 1 Cyn    I AM ALREADY HERE, sent by the
    #                                           people already in the room when
    #                                           somebody new arrives
    #   :<them> PRIVMSG <chan> :0 0 40Cyn\t01…  chat
    #   :<them> PRIVMSG <chan> :900000Cyn\t010… away / back
    #   :<them> PRIVMSG <chan> :80000001        left
    #   :<them> PRIVMSG <chan> :a0000001Cas     role change
    #
    # `is_chat` matched only the middle two, so `2`, `3`, `8` and `a` were
    # swallowed. The sidebar is built from the code-2 announce -- NAMES carries
    # only opaque handle-nicks, the display name is in the announce -- so
    # dropping it means the other person never appears, and an away flag for
    # somebody with no row has nothing to attach to. Both symptoms, one gate.
    #
    # The descriptor a room's creator sends is code 2 as well, and relaying it
    # is safe by construction: at create time the creator is the only member, so
    # `exclude=sess` reaches nobody and the validated create path is unchanged.
    if sess is not None and target.startswith(b"#"):
        n = roomregistry.ROOMS.broadcast(target, [self_echo], exclude=sess)
        if n:
            code = body[:1].decode("latin1", "replace")
            log("authserv", f"  room {target.decode('latin1')}: relayed "
                            f"code-{code} from {nick.decode('latin1')} to "
                            f"{n} member(s)")

    if is_chat and mode == "se":
        if chat_mode == "relay":
            return [self_echo]
        if chat_mode == "quiet":
            return None
    if mode == "se":
        return [se_300]
    if mode == "self":
        return [self_echo]
    svc = b"pol!service@" + srv
    svc_lines = [b":" + svc + b" PRIVMSG " + arg,
                 b":" + srv + b" 332 " + nick + b" " + target + b" :" + target,
                 b":" + srv + b" 333 " + nick + b" " + target + b" " + nick + b" 0"]
    return ([self_echo] + svc_lines) if mode == "both" else svc_lines


def _verb_topic(arg, nick, srv, peer_ip, sess):
    # TWO DIFFERENT MESSAGES SHARE THIS VERB, and answering the second one as
    # if it were the first is why entering an existing room did nothing.
    #
    #   TOPIC #<chan> :zt7TTTTTTTTTyry     a SET -- the creator naming the room
    #   TOPIC #<chan>                      a QUERY -- a joiner ASKING the name
    #
    # SE's capture, 2026-08-15, decoded 2026-08-16 (`authdec.py`), shows both:
    #
    #   C->S TOPIC <chan> :zt9rToiTKtITCYNROOM
    #   S->C :<nick>!~x@ TOPIC <chan> :zt9rToiTKtITCYNROOM      <- echo
    #   C->S TOPIC <chan>
    #   S->C :<srv> 332 <nick> <chan> :zt9rTojTK7ITCASROOM      <- 332, not an echo
    #
    # We answered a QUERY with the echo shape, i.e. broadcast that the joiner
    # had just set the topic to nothing. The value is A64 ('T' is A64[0] =
    # zero) -- the room's encoded name -- so it is stored and handed back
    # verbatim, never reinterpreted.
    chan = _chan_arg(arg)
    if not chan.startswith(b"#"):
        return None
    _, sep, topic = arg.partition(b" :")
    if not sep:
        topic = _room_topic(chan)
        if topic is None:
            # RFC 331 is the honest answer, but SE was never seen sending one
            # and a guessed numeric is how POL-5135 gets raised. Stay silent.
            return None
        return [b":" + srv + b" 332 " + nick + b" " + chan + b" :" + topic]
    roomregistry.ROOMS.set_topic(chan, topic)
    lobbyrooms._register_created_room(chan, topic, sess)
    return [b":" + nick + b"!~x@" + _irc_host(srv) + b" TOPIC " + chan + b" :" + topic]


def _verb_ping(arg, nick, srv, peer_ip, sess):
    # Not observed yet, but a PING with no PONG is the other way this socket
    # can hang, and the reply is unambiguous.
    return [b":" + srv + b" PONG " + srv + (b" :" + arg.lstrip(b":") if arg else b"")]


def _verb_pong(arg, nick, srv, peer_ip, sess):
    # CONSUMED ON PURPOSE, AND THAT IS THE WHOLE POINT OF THIS ARM.
    #
    # We send the keepalive PING; the client answers PONG; RFC 1459 wants
    # no reply to it. But with no arm here it fell to `return None`, and
    # the caller logs every None as "no handler" -- **11,161 times** across
    # 2026-08-11..08-25, 2,715 of them in the live log alone. That is not a
    # protocol gap, it is a LOG DEFECT, and it was an expensive one: the
    # "no handler" line is this band's only oracle for what we fail to
    # answer, and PONG was burying the real misses in noise thousands of
    # lines deep. An oracle nobody can read is not an oracle.
    #
    # Empty list, NOT None: `[]` means "handled, nothing to send" and skips
    # both the log and the send (see the `if not reply` guard at the call
    # site). Returning None here would restore the noise.
    return []


def _verb_quit(arg, nick, srv, peer_ip, sess):
    # *** LEAVING GM CHAT STALLED HERE. *** Observed live 2026-08-16: exiting
    # GM chat sends, in order, `PRIVMSG <room> :UE3<handle>` (the 'U' record,
    # subcode E = left), `PART :<room>`, then `QUIT` -- and QUIT logged "no
    # handler", so the client sat on "Exiting GM chat..." waiting for a server
    # that had stopped talking. The GM UDP band is NOT involved; gmd received
    # nothing during the whole sequence.
    #
    # RFC 1459: a server acknowledges QUIT with ERROR and closes. The client
    # is also still in whatever rooms it had, so tell those rooms before the
    # socket goes -- otherwise the leaver haunts NAMES exactly the way the
    # PART bug above used to let them.
    why = arg.lstrip(b":").strip() or nick
    line = b":" + nick + b"!~x@" + _irc_host(srv) + b" QUIT :" + why
    if sess is not None:
        for chan, others in roomregistry.ROOMS.drop(sess):
            if others:
                roomregistry.ROOMS.broadcast(chan, [line])
    # A SEAT DIES WITH ITS GAME-BAND SESSION. Only that session: a member
    # holds several connections (GM chat QUITs too), and the title knows
    # which one its game talks on.
    try:
        titles.session_quit(lobbysession._session_get("member_id"), lobbysession._session_sid())
    except Exception as _e:
        log("authserv", f"  title QUIT hook raised ({_e!r})")
    return [b"ERROR :Closing Link: " + nick]


#: The verbs a logged-in client sends on the auth band, and what answers
#: each. A handler returns a list of lines to send, [] for "handled, nothing
#: to send", or None for "not handled" (logged by the caller as a miss).
#: Kept in the order the wire was worked out in.
AUTH_VERBS = {
    b"NOTICE": _verb_notice,
    b"AWAY": _verb_away,
    b"JOIN": _verb_join,
    b"PART": _verb_part,
    b"KICK": _verb_kick,
    b"WHO": _verb_who,
    b"MODE": _verb_mode,
    b"PRIVMSG": _verb_privmsg,
    b"TOPIC": _verb_topic,
    b"PING": _verb_ping,
    b"PONG": _verb_pong,
    b"QUIT": _verb_quit,
}
