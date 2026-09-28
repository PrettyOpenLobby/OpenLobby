"""RoomRegistry: the IRC-style chat rooms and who stands in them."""
import os
import time
import threading
from srvcore import log
from . import gamenotice, lobbyrooms, presence



def _room_of_member(member_id):
    """The channel this member is sitting in, or None.

    Asked from EITHER container: `_live_rooms()` answers from memory in the
    process that owns the registry and from the published file everywhere else,
    and the `who` roster it publishes carries `member_id` per row precisely so
    an identity can cross that boundary.
    """
    if not member_id:
        return None
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return None
    for chan, entry in (lobbyrooms._live_rooms() or {}).items():
        for row in (entry or {}).get("who") or []:
            if row.get("member_id") == mid:
                return chan
    return None


class RoomRegistry:
    """Who is in which channel. The thing whose absence made joining impossible.

    Rooms are IRC channels in two namespaces (captured 2026-08-11):
    `#01CU<40-symbol redirect token>` is a created chat ROOM, `#XXL<16 hex>` is a
    ZONE. Both are just channels here; only their advertised limit differs.

    Membership is deliberately IN-PROCESS and not persisted: a channel's member
    list is exactly "who is connected right now", so it cannot outlive the
    process any more than the sockets can. Group membership -- which *is*
    durable -- is a different thing and lives in `accounts`.

    *** BUT ANOTHER PROCESS HAS TO SEE IT. *** Rooms are joined over IRC, which is
    the `authsess` container, and the ROOM BROWSER is served over the lobby band,
    which is `login`. Everything the browser wants to say about a room -- does it
    exist, how many people are in it, how many are in the zone -- is known only
    here. Read from `login` this registry is permanently empty, and the symptom is
    not an error: created rooms silently never appear and every count reads zero.
    So each mutation publishes a snapshot (`_publish_rooms`), and a process that
    never mutates is a pure reader and can never clobber it.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._rooms = {}                 # chan -> {"members": [ChatSession], "owner": bytes, "topic": bytes}
        # Topics OUTLIVE the room dict entry, which is reaped when the last member
        # leaves. A listed room stands empty most of the time and must still know
        # its own name when the next person walks in.
        self._topics = {}
        # Channel modes, same lifetime as topics. `+k` is a room's PASSWORD and
        # `+l` its capacity, and both have to survive between the create and the
        # next person trying the door.
        self._modes = {}
        # GHOST MEMBERS: nicks restored from rooms.json after a restart, whose
        # sockets died with the old process while the CLIENTS kept their
        # sessions (authrelay holds them open and is deliberately never
        # bounced). Measured on prod 2026-08-19T00:22: a deploy recreated
        # authsess between a room's creation (00:17:53, +n +l 10 stored,
        # creator inside) and a friend's join (00:24:28) -- the joiner was made
        # @owner of an empty ghost room and served the fresh-room `+l 1`, so
        # their client backed straight out. A ghost keeps the seat warm: it
        # counts in NAMES/WHO/rosters, is adopted back to a live session the
        # moment one with its nick reappears, and expires if none does.
        # chan -> {nick(bytes): expiry_unix}
        self._ghosts = {}

    def _ghost_purge_locked(self, chan):
        g = self._ghosts.get(chan)
        if not g:
            return
        now = time.time()
        for nk in [n for n, exp in g.items() if exp <= now]:
            g.pop(nk, None)
            log("authserv", f"  room {chan.decode('latin1')}: ghost "
                            f"{nk.decode('latin1')} expired unclaimed")
        if not g:
            self._ghosts.pop(chan, None)
            # a room alive only through its ghosts is reaped with the last one
            room = self._rooms.get(chan)
            if room is not None and not room["members"]:
                self._rooms.pop(chan, None)
                if chan.startswith(b"#01CU"):
                    m = self._modes.get(chan)
                    if m:
                        m["k"] = None       # a dead room's password must not
                        m["o"] = None       # 475 whoever re-creates the token

    def ghost_nicks(self, chan):
        """Unexpired ghost nicks for `chan` (bytes), oldest promise first."""
        with self._lock:
            self._ghost_purge_locked(chan)
            return list(self._ghosts.get(chan, ()))

    def adopt_ghosts(self, chan, only=None):
        """Re-attach ghosts whose OWN client is still on a room screen.

        `only` is a nick: adopt THAT ghost and nobody else. Every caller passes
        one now -- see the banner below on why a channel-wide sweep was wrong.

        The lookup is `PRESENCE.sessions_by_nick` -- the same routing a whisper
        uses -- so adoption costs nothing when there are no ghosts, and a
        client that survived a restart via the relay is picked back up the
        first time the room is touched (a join, a broadcast, a publish).

        WARNING: A LIVE SESSION IS NOT EVIDENCE THAT ITS CLIENT IS IN THIS ROOM, and
        this used to treat it as though it were. Measured 2026-08-20T20:08:29:
        `UF8TOQDTX` had been carried as a ghost since before the restarts and had
        **no JOIN for `#<title>R001` anywhere in the log**; the moment a DIFFERENT
        player joined, this promoted him to a full member off nothing but "his
        socket is open". He was on the auction screen. The room then counted 2
        and the zone screen said 2, with one real player in it -- reported as
        "it thinks Lex is in Mermaids' Dreamworld, he is not".

        Adoption is triggered by a touch on the CHANNEL -- a join, a broadcast --
        which is evidence about whoever did the touching and says nothing
        whatever about the ghost. So the ghost has to speak for itself, and
        `ChatSession.in_room_recently` is exactly that signal: the class-L
        `<DR>` poll runs every 1-3 s while a room screen is up and stops when the
        client navigates away. Lex's session was connected and silent on that
        band; member 6's was polling it.

        WARNING: THIS DOES NOT EVICT ANYONE, which is the caution
        `in_room_recently`'s own docstring asks for. A ghost that fails the test
        stays a ghost -- still in `who`, still keeping the room from being
        reaped, still adoptable the instant it polls. We are declining to hand
        out a seat on no evidence, not taking one away. A survivor really sitting
        on the room screen is adopted within one poll, i.e. 1-3 s.

        A session that predates the signal (`last_room_heard == 0`) is adopted as
        before: never having been measured is not the same as having navigated
        away, and the old behaviour is the safe side of that one.
        """
        adopted = []
        with self._lock:
            self._ghost_purge_locked(chan)
            g = self._ghosts.get(chan)
            if not g:
                return adopted
            for nk in list(g):
                if only is not None and nk != only:
                    # WARNING: ONE PLAYER LEAVING USED TO RE-MATERIALISE ANOTHER.
                    # Measured 2026-08-20T21:00:22, and it is the hole in the
                    # `in_room_recently` gate two commits earlier:
                    #
                    #     PART :#<title>R003
                    #     room #<title>R003: ghost UA4XX8PKP re-attached to a live
                    #                    session
                    #
                    # `part()` correctly drops the PARTING nick's ghost -- but
                    # the PART is then BROADCAST, and the broadcast swept the
                    # channel and adopted somebody else's. `in_room_recently`
                    # did not stop it because that session really was on a room
                    # screen: A DIFFERENT ROOM'S. The signal says "this client
                    # is in a room", never "in THIS room", and there is no
                    # per-channel version of it to ask for.
                    #
                    # So the only sound trigger is the ghost's own act on this
                    # channel. Reported as "it says all three users are in that
                    # room" with two of them gone.
                    continue
                live = [s for s in presence.PRESENCE.sessions_by_nick(nk)
                        if getattr(s, "alive", False) and hasattr(s, "send")]
                if not live:
                    continue
                if os.environ.get("POL_ROOM_ADOPT_NEEDS_POLL", "1") == "1":
                    live = [s for s in live if gamenotice._session_in_room(s)]
                    if not live:
                        log("authserv",
                            f"  room {chan.decode('latin1')}: ghost "
                            f"{nk.decode('latin1')} has a live session but it "
                            f"is NOT polling the room band -- left as a ghost "
                            f"(its client navigated away; it re-attaches the "
                            f"moment it polls again)")
                        continue
                g.pop(nk, None)
                room = self._rooms.setdefault(
                    chan, {"members": [], "owner": nk, "topic": None})
                if live[0] not in room["members"]:
                    room["members"].append(live[0])
                adopted.append(live[0])
                log("authserv", f"  room {chan.decode('latin1')}: ghost "
                                f"{nk.decode('latin1')} re-attached to a live "
                                f"session")
            if not g:
                self._ghosts.pop(chan, None)
        if adopted:
            self._publish()
        return adopted

    def restore_state(self, reg, ghost_ttl):
        """Load owner/topic/modes and ghost rosters saved by a previous process.

        Only ever ADDS -- a chan this process already knows is left alone, so a
        late restore cannot clobber live state. Members cannot be restored as
        sessions (the sockets died with the old process); they come back as
        ghosts and re-attach through `adopt_ghosts`.
        """
        n = 0
        now = time.time()
        exp = now + ghost_ttl
        # WARNING: A GHOST WHOSE CLOCK RESTARTS WITH THE PROCESS NEVER DIES. Measured
        # 2026-08-20: `registration()` writes live members and ghosts into ONE
        # undifferentiated `who` list, and this used to give every restored nick
        # a fresh `now + ttl`. So each restart re-ghosted the previous restart's
        # ghosts with a brand-new 900 s promise, and published them again as
        # `who` for the next restart to read -- ghost laundering. Deploys ran at
        # 19:39, 19:44, 19:48, 19:50 and 19:53 that afternoon, none more than
        # 900 s apart, and one player who had long since quit was carried through
        # all five and shown on the zone screen as present.
        #
        # `deadlines` is the saved absolute expiry per nick, so a promise made
        # once is kept once. A nick with no saved deadline is a LIVE member from
        # the previous process and gets the full grace -- that is the case this
        # feature exists for.
        saved = {}
        for key, r in (reg or {}).items():
            for nk, when in (r.get("ghosts") or {}).items():
                try:
                    saved[(key, nk)] = float(when)
                except (TypeError, ValueError):
                    continue
        with self._lock:
            for key, r in reg.items():
                chan = key.encode("latin1")
                if chan in self._rooms or chan in self._modes:
                    continue
                m = r.get("modes") or {}
                if m:
                    self._modes[chan] = {
                        "n": bool(m.get("n")),
                        "l": int(m["l"]) if m.get("l") else None,
                        "k": m["k"].encode("latin1") if m.get("k") else None,
                        "b": {b.encode("latin1") for b in m.get("b", ())},
                        "o": ({x.encode("latin1") for x in m["o"]}
                              if m.get("o") is not None else None)}
                if r.get("topic"):
                    self._topics[chan] = r["topic"].encode("latin1")
                ghosts = [w for w in r.get("who", ())
                           if saved.get((key, w), exp) > now]
                dead = len(r.get("who", ())) - len(ghosts)
                if dead:
                    log("authserv", f"room restore: {key}: {dead} ghost(s) "
                                    f"expired while the server was down -- not "
                                    f"restored")
                deadlines = {w: saved.get((key, w), exp) for w in ghosts}
                ghosts = [w.encode("latin1") for w in ghosts]
                if ghosts:
                    self._rooms[chan] = {
                        "members": [],
                        "owner": (r["owner"].encode("latin1")
                                  if r.get("owner") else ghosts[0]),
                        "topic": self._topics.get(chan)}
                    self._ghosts[chan] = {
                        nk: deadlines[nk.decode("latin1")] for nk in ghosts}
                n += 1
        return n

    def registration(self):
        """{chan_str: {"owner","topic","modes","who"}} -- everything a future
        process needs to restore a room's structure. Members travel as NICKS
        (the `who` list, live + ghost) because sessions cannot cross a restart."""
        out = {}
        with self._lock:
            chans = set(self._rooms) | set(self._modes) | set(self._topics)
            for chan in chans:
                room = self._rooms.get(chan) or {}
                m = self._modes.get(chan) or {}
                who = [s.nick.decode("latin1", "replace")
                       for s in room.get("members", ())]
                # Ghosts travel WITH their deadline (see `restore_state`), so a
                # restart carries the promise rather than minting a new one. The
                # flat `who` list is kept beside it: every existing reader takes
                # that and must not have to learn a new shape.
                deadlines = {nk.decode("latin1", "replace"): when
                             for nk, when in self._ghosts.get(chan, {}).items()}
                who += list(deadlines)
                if not who and not m and chan not in self._topics:
                    continue
                out[chan.decode("latin1", "replace")] = {
                    "owner": (room.get("owner") or b"").decode("latin1",
                                                               "replace") or None,
                    "topic": (self._topics.get(chan) or b"").decode(
                        "latin1", "replace") or None,
                    "modes": {"n": bool(m.get("n")), "l": m.get("l"),
                              "k": (m.get("k") or b"").decode("latin1") or None,
                              "b": sorted(x.decode("latin1")
                                          for x in m.get("b", ())),
                              "o": (sorted(x.decode("latin1")
                                           for x in m["o"])
                                    if m.get("o") is not None else None)}
                             if m else {},
                    "who": who,
                    "ghosts": deadlines}
        return out

    def _publish(self):
        """Hand this registry's state to the other container. Never raises."""
        lobbyrooms._ROOMS_OWNER[0] = True
        try:
            lobbyrooms._publish_rooms()
        except Exception as exc:                  # a snapshot must not break a JOIN
            log("authserv", f"could not publish room state: {exc!r}")

    def join(self, chan, sess):
        """Add `sess`. Returns (others_before_join, is_owner)."""
        # THE JOINER'S OWN GHOST, and nobody else's: a re-JOIN is evidence about
        # exactly one player. The arm just below handles them coming back.
        self.adopt_ghosts(chan, only=sess.nick)
        with self._lock:
            self._ghost_purge_locked(chan)
            g = self._ghosts.get(chan)
            if g:
                # the joiner may BE a ghost coming back through an explicit
                # re-JOIN -- their seat is theirs again, not a second row
                g.pop(sess.nick, None)
                if not g:
                    self._ghosts.pop(chan, None)
            room = self._rooms.setdefault(
                chan, {"members": [], "owner": None, "topic": None})
            if room["owner"] is None:
                room["owner"] = sess.nick
            # WARNING: A RECONNECT IS THE SAME PERSON, NOT A SECOND ONE. The test
            # below is object identity, so a client that drops and rejoins under
            # the SAME NICK used to be appended beside its own stale session.
            # Measured 2026-08-20 after a restart cycle: `rooms-live.json` held
            # `UA4XX8PKP` twice, `members: 3` for two people, and Tetra Master's
            # member pane drew "LaptopTest2" twice. The ghost path already gets
            # this right (`g.pop(sess.nick)` above) -- it only covers sessions
            # that had already been moved to ghosts, and a fast rejoin beats that.
            stale = [m for m in room["members"]
                     if m is not sess and getattr(m, "nick", None) == sess.nick]
            for m in stale:
                room["members"].remove(m)
            if stale:
                log("authserv", f"  room {chan.decode('latin1', 'replace') if isinstance(chan, bytes) else chan}: "
                                f"{sess.nick!r} rejoined -- dropped "
                                f"{len(stale)} stale session(s) under that nick")
            others = [m for m in room["members"] if m is not sess]
            if sess not in room["members"]:
                room["members"].append(sess)
            out = others, room["owner"] == sess.nick
        self._publish()
        return out

    def set_modes(self, chan, changes):
        """Apply parsed IRC mode changes. `changes` is {"n":bool,"l":int,"k":bytes}
        plus "b_add"/"b_del" nicks; absent keys are left alone."""
        with self._lock:
            m = self._modes.setdefault(
                chan, {"n": False, "l": None, "k": None, "b": set(), "o": None})
            for f in ("n", "l", "k"):
                if f in changes:
                    m[f] = changes[f]
            for nick in changes.get("b_add", ()):
                m["b"].add(nick)
            for nick in changes.get("b_del", ()):
                m["b"].discard(nick)
            if changes.get("o_add") or changes.get("o_del"):
                # ops start as "the owner" implicitly; the first explicit change
                # materialises that so `+o <new> -o <old>` really MOVES the '@'
                # (the master handoff, measured live 2026-08-19T00:48)
                if m.get("o") is None:
                    own = (self._rooms.get(chan) or {}).get("owner")
                    m["o"] = {own} if own else set()
                for nick in changes.get("o_add", ()):
                    m["o"].add(nick)
                for nick in changes.get("o_del", ()):
                    m["o"].discard(nick)
        self._publish()

    def modes(self, chan):
        with self._lock:
            m = self._modes.get(chan)
            return dict(m, b=set(m["b"])) if m else {
                "n": False, "l": None, "k": None, "b": set(), "o": None}

    def op_nicks(self, chan):
        """Who carries the '@' in this channel: the explicit op set once one
        exists (a master handoff has happened), the owner until then."""
        with self._lock:
            m = self._modes.get(chan) or {}
            if m.get("o") is not None:
                return set(m["o"])
            room = self._rooms.get(chan)
            return ({room["owner"]} if room and room.get("owner") else set())

    def set_topic(self, chan, topic):
        """Store a room's encoded name. Survives the room going empty and coming
        back, so a listed room keeps its name across an idle period."""
        with self._lock:
            room = self._rooms.setdefault(
                chan, {"members": [], "owner": None, "topic": None})
            room["topic"] = topic
            self._topics[chan] = topic
        self._publish()

    def topic(self, chan):
        with self._lock:
            room = self._rooms.get(chan)
            if room and room.get("topic") is not None:
                return room["topic"]
            return self._topics.get(chan)

    def channels_of(self, sess):
        """Every channel this session is currently a member of.

        Needed by the channel-less `PART :` the PS2 sends when it leaves
        VS. COM: if the session really is in a room we part that one
        properly rather than acknowledging into the void.
        """
        with self._lock:
            return [c for c, r in self._rooms.items()
                    if sess in (r.get('members') or [])]

    def part(self, chan, sess):
        with self._lock:
            # a ghost PARTing is a real leave -- the client walked out of a
            # room it stayed in across our restart (measured 2026-08-19: the
            # creator's PART arrived for a room the wiped registry had no
            # record of)
            g = self._ghosts.get(chan)
            if g:
                g.pop(sess.nick, None)
                if not g:
                    self._ghosts.pop(chan, None)
            room = self._rooms.get(chan)
            if not room:
                return []
            if sess in room["members"]:
                room["members"].remove(sess)
            others = list(room["members"])
            if not others and not self._ghosts.get(chan):
                self._rooms.pop(chan, None)
                # a CREATED room dies with its last occupant, modes and all --
                # without this its old password outlives it and 475s the next
                # person who re-creates the same channel token. Listed rooms
                # (#01CP) and zones (#XXL) keep theirs: they are fixtures.
                if chan.startswith(b"#01CU"):
                    m = self._modes.get(chan)
                    if m:
                        m["k"] = None       # a dead room's password must not
                        m["o"] = None       # 475 whoever re-creates the token
            else:
                # *** THE MASTER MOVES WHEN THE MASTER LEAVES. *** Measured live
                # 2026-08-19: Lex (owner) parted RedbEacon, Amicable stayed, and
                # the registry kept owner=Lex with no explicit op set -- so
                # `op_nicks` named a nick that had walked out and the room had no
                # master at all. Promote the oldest remaining occupant (live
                # member first, else a ghost holding a seat), exactly as SE's '@'
                # hands off. Only when the leaver actually held the room.
                self._reassign_owner_locked(chan, room, sess.nick)
        self._publish()
        return others

    def _reassign_owner_locked(self, chan, room, leaver_nick):
        """If `leaver_nick` held the room, move ownership to the next occupant.

        Returns the new owner's nick when a handoff happened, else None. Caller
        holds `self._lock`.
        """
        m = self._modes.get(chan)
        if m and m.get("o") is not None:
            held = leaver_nick in m["o"]
        else:
            held = room.get("owner") == leaver_nick
        if not held:
            return None
        # WARNING: AND THE HEIR MUST ACTUALLY BE IN THE ROOM. Measured
        # 2026-08-20T18:04:37Z: this handed `#<title>R001` to a client that had
        # navigated to the auction screen and hung there, because the only test
        # was "is it in the members list" -- and a client that never sent a PART
        # is. It is not enough to ask whether it ANSWERS, either: that client
        # PONGs on schedule and sends auction traffic (the first version of this
        # check tested exactly that and would have picked it anyway). Ask whether
        # it is polling the ROOM band. Fall back to a member that is not, only
        # when there is nobody better -- a room with a bad master still beats a
        # room with none.
        heir = None
        silent = None
        for cand in room["members"]:
            if cand.nick == leaver_nick:
                continue
            if getattr(cand, "in_room_recently", None) and cand.in_room_recently():
                heir = cand.nick
                break
            if silent is None:
                silent = cand.nick
        if heir is None and silent is not None:
            heir = silent
            log("authserv", f"  room {chan.decode('latin1')}: no remaining "
                            f"member is polling the room band -- '@' goes to "
                            f"{silent.decode('latin1', 'replace')} anyway "
                            f"(a room with a bad master beats one with none)")
        if heir is None:                       # nobody live -- a ghost keeps it warm
            g = self._ghosts.get(chan) or {}
            heir = next(iter(g), None)
        if heir is None or heir == leaver_nick:
            return None
        room["owner"] = heir
        if m and m.get("o") is not None:
            m["o"].discard(leaver_nick)
            m["o"].add(heir)
        log("authserv", f"  room {chan.decode('latin1')}: master left; "
                        f"'@' handed to {heir.decode('latin1', 'replace')}")
        return heir

    def members(self, chan):
        with self._lock:
            room = self._rooms.get(chan)
            return list(room["members"]) if room else []

    def owner(self, chan):
        with self._lock:
            room = self._rooms.get(chan)
            return room["owner"] if room else None

    def rooms_of(self, sess):
        with self._lock:
            return [c for c, r in self._rooms.items() if sess in r["members"]]

    def drop(self, sess):
        """Remove `sess` everywhere. Returns [(chan, remaining_members)]."""
        out = []
        with self._lock:
            for chan in list(self._rooms):
                room = self._rooms[chan]
                if sess in room["members"]:
                    room["members"].remove(sess)
                    out.append((chan, list(room["members"])))
                    if not room["members"] and not self._ghosts.get(chan):
                        self._rooms.pop(chan, None)
                        if chan.startswith(b"#01CU"):
                            m = self._modes.get(chan)
                            if m:
                                m["k"] = None
                                m["o"] = None
                    else:
                        # same master-handoff as part(): a disconnect must not
                        # strand the room ownerless either.
                        self._reassign_owner_locked(chan, room, sess.nick)
        self._publish()
        return out

    def broadcast(self, chan, lines, exclude=None):
        """Push `lines` to every member except `exclude`. Returns the count sent."""
        # WARNING: NO ADOPTION HERE ANY MORE. This used to sweep the channel so "a
        # restart survivor hears the room again the moment anyone speaks" -- and
        # a broadcast is caused by somebody ELSE, so it re-materialised players
        # who had gone. Worse, every PART broadcasts, so leaving a room put other
        # people back INTO it (measured 2026-08-20T21:00:22; see `adopt_ghosts`).
        #
        # A survivor is adopted by their own JOIN instead. One that never
        # re-joins now expires on its lease rather than being propped up by
        # strangers' traffic, which is the right way round for a roster: erring
        # toward "nobody is there" matches what a player reads off the
        # screen, and a wrong PRESENT is far more visible than a wrong ABSENT.
        n = 0
        for m in self.members(chan):
            if m is exclude or not m.alive:
                continue
            if m.send(lines):
                n += 1
        return n

    def snapshot(self):
        with self._lock:
            return {c.decode("latin1", "replace"):
                    [m.nick.decode("latin1", "replace") for m in r["members"]]
                    for c, r in self._rooms.items()}


#: Process-wide room membership. One `login` container serves every client, so a
#: plain in-process registry is genuinely shared state -- no IPC needed.
ROOMS = RoomRegistry()
