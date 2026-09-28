"""The push spool: cross-process delivery of pushes and their watchers."""
import itertools
import json
import os
import time
import threading
from srvcore import log
from .deps import accounts
from . import framing, friendgroups, friendroster, handlelists, memberstatus, presence, profilerecord, pushchannel, pushrecord, roomregistry, titlezone



# --------------------------------------------------------------------------- #
# THE PUSH SPOOL -- because the writer and the socket are in DIFFERENT PROCESSES
# --------------------------------------------------------------------------- #
# `login` runs directory,lobby,world,mail and `authsess` runs authserv, in two
# containers. The session channels a push has to be WRITTEN to are ChatSessions,
# which only ever exist in authserv -- but the events worth pushing (a comment
# edit, a friend request) arrive as LOBBY messages, in the other process.
#
# So a lobby-side `broadcast_event` that just looked in PRESENCE would find an
# empty registry every time and deliver to nobody, silently and with a cheerful
# return value of 0. This spool is the hop between them: the lobby APPENDS the
# intent, authserv DRAINS it and does the fan-out where the sockets are.
#
# /logs, not /data -- both containers mount ./logs read-write, and `login` does
# not mount ./data at all. Same directory the presence control files already use.
_PUSH_SPOOL = os.environ.get(
    "POL_PUSH_SPOOL", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                   "push-spool.jsonl"))
#: True only in the process that runs `authserv` -- i.e. the one holding the
#: sockets. Set in main(). Everywhere else, pushes go to the spool.
_PUSH_LOCAL = [False]
_PUSH_SPOOL_LOCK = threading.Lock()

#: WARNING: THE DRAIN OFFSET IS PERSISTED, AND IT DID NOT USED TO BE.
#:
#: `_push_spool_watcher` kept `off` in a local, so it started every process at
#: byte 0 and re-delivered the WHOLE spool on every restart. Found 2026-08-17 by
#: restarting authsess and watching 315 records replay out of a 353-record file
#: that nothing had ever trimmed.
#:
#: It looked harmless -- all 315 were dropped, because no session had registered
#: yet -- and that is exactly the trap. It inverts the point of `authrelay`,
#: which exists so a client KEEPS its socket across an authsess restart. In the
#: case the relay is built for, re-registration races the drain, and a client
#: that wins that race is handed the entire accumulated push history at once:
#: stale friend rows, stale events, all replayed as if they had just happened.
#: Dropping them was luck, not design.
_PUSH_SPOOL_OFFSET = _PUSH_SPOOL + ".offset"

#: Rotate the spool once it is fully consumed and past this, so it cannot grow
#: forever (it is append-only and nothing ever trimmed it). One generation is
#: kept, because a spool is the only record of what was pushed and when.
_PUSH_SPOOL_MAX = int(float(os.environ.get("POL_PUSH_SPOOL_MAX_MB", "4"))
                      * 1024 * 1024)
#: ...and only after it has been QUIET this long. The writer is the other
#: container and appends with a plain O_APPEND open, so a rename could in
#: principle land between its open and its write. Requiring the file to be both
#: fully drained AND idle makes that window one nobody will hit; without the
#: idle test it is merely small.
_PUSH_SPOOL_IDLE = 30.0


def _push_offset_load():
    """The byte offset this process should resume draining from."""
    try:
        with open(_PUSH_SPOOL_OFFSET, "r", encoding="utf-8") as f:
            return max(0, int(f.read().strip() or 0))
    except (OSError, ValueError):
        return 0


def _push_offset_save(off):
    try:
        tmp = _PUSH_SPOOL_OFFSET + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(off)))
        os.replace(tmp, _PUSH_SPOOL_OFFSET)   # atomic
    except OSError as exc:
        log("authserv", f"push[spool] cannot persist offset ({exc})")


def _push_spool_rotate(off):
    """Retire a fully-drained spool. Returns the new offset (0 if rotated)."""
    if _PUSH_SPOOL_MAX <= 0:
        return off
    try:
        st = os.stat(_PUSH_SPOOL)
    except OSError:
        return off
    if st.st_size < _PUSH_SPOOL_MAX or off < st.st_size:
        return off                            # too small, or not caught up
    if time.time() - st.st_mtime < _PUSH_SPOOL_IDLE:
        return off                            # still being written to
    try:
        os.replace(_PUSH_SPOOL, _PUSH_SPOOL + ".1")
        _push_offset_save(0)
        log("authserv", f"push[spool] rotated at {st.st_size} bytes, fully "
                        f"drained and idle {_PUSH_SPOOL_IDLE:.0f}s")
        return 0
    except OSError as exc:
        log("authserv", f"push[spool] cannot rotate ({exc})")
        return off


def _row_push_enabled():
    """Is the friend-ROW push (face icons) on?

    WARNING: **DEFAULT OFF SINCE 2026-08-16, BECAUSE IT BROKE LOGIN.** Turning it on
    made every login fail with **POL-5135** at the "retrieving messages" step.
    The A/B is clean: with `rowpush=1` three consecutive logins died there, and
    with `rowpush=0` the very next login completed and served its mailbox, with
    identical lobby opcodes, reply sizes and latencies either way. The only
    difference on the wire was the record this pushes.

    So the bytes matching SE's is NOT sufficient, and the confident reading in
    the commit that added this was wrong. Two things are still confounded and
    `rowpush=fire` exists to separate them:

      * the RECORD may be malformed in some way the capture does not show. One
        real defect has already turned up -- our pseudo-nick began with a digit
        on 19% of records, which is not a legal nick (see `_push_nick`) -- and it
        was live for exactly one of the two records we sent.
      * the MOMENT may be wrong. SE pushed theirs at a friend's login, to a
        client sitting idle at the menu. We push during the client's own login,
        while it has four concurrent lobby conversations open and is about to
        fetch its mailbox. An unsolicited auth-band NOTICE may simply not be
        safe there, in which case no amount of byte-fixing helps.

    Settings (`logs/presence.ctl`, re-read per push -- no restart):

        rowpush=0     off. THE DEFAULT.
        rowpush=fire  the login-time push stays off, but `rowpush-fire.ctl`
                      can send ONE record on demand -- the safe experiment,
                      because a client idle at the menu costs nothing if it
                      fails.
        rowpush=1     push at login. Only once `fire` has proven a record.
    """
    return presence._presence_cfg("rowpush", "POL_FRIEND_ROW_PUSH", "0") == "1"


def _push_emit(rec, db=None):
    """Deliver `rec` if we hold the sockets, otherwise spool it for authserv.

    WARNING: **THE RETURN IS A DELIVERY COUNT, AND IT IS 0 WHENEVER WE SPOOLED.** A
    spooled record has not been delivered yet -- authserv drains it later -- so
    0 here means "handed off", NOT "failed". Only the local-delivery path
    (`_PUSH_LOCAL`) can return non-zero.

    Do not sum this into anything a human will read as a success count. That is
    exactly what the group-accept roster push used to do: it printed
    `0 spool line(s)` on every successful accept, with the `push spooled:`
    lines sitting directly above it, and it reads as a failure. A caller that
    wants to report progress should count what it knows -- how many pushes it
    queued -- rather than what this returns.
    """
    if rec.get("kind") == "rows":
        if not _row_push_enabled():
            return 0
    elif rec.get("kind") == "gmode":
        # ITS OWN SWITCH, on by default. A channel MODE is a chat-visible effect
        # that depends on nothing else being right -- unlike the friend-row push
        # the general gate is holding back -- and without it the 7:3 handler is
        # invisible until the member rejoins. `groupmode=0` in presence.ctl, or
        # POL_GROUP_MODE_PUSH=0, stops it.
        if presence._presence_cfg("groupmode", "POL_GROUP_MODE_PUSH", "1") != "1":
            return 0
    elif rec.get("kind") == "mail":
        # ITS OWN SWITCH, on by default. A mail push carries the message's own
        # record and asks the client to read an object that definitely exists,
        # so it does not depend on the friend-row record being right -- which is
        # the thing the general push gate is holding back. `mailpush=0` in
        # presence.ctl, or POL_MAIL_PUSH=0, stops it without touching that.
        if presence._presence_cfg("mailpush", "POL_MAIL_PUSH", "1") != "1":
            return 0
    elif presence._presence_cfg("push", "POL_PRESENCE_PUSH", "0") != "1":
        return 0
    if _PUSH_LOCAL[0]:
        return _push_deliver(rec, db)
    try:
        with _PUSH_SPOOL_LOCK:
            with open(_PUSH_SPOOL, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        log("lobby", f"  push spooled: {rec.get('kind')} "
                     + (f"{rec.get('subject')!r} from {rec.get('sender')!r}"
                        if rec.get("kind") == "mail" else
                        f"event {rec.get('event')} {rec.get('text')!r}"))
    except Exception as exc:
        log("lobby", f"  push spool failed ({exc!r})")
    return 0


def _push_deliver(rec, db=None):
    """Do the fan-out for one spooled record. Runs in authserv only.

    Validates rather than trusting: the spool is a FILE, so a truncated write, a
    half-flushed line or an older build's record shape can all turn up here, and
    a KeyError deep in the fan-out would be caught by the drain loop but only
    after this record was silently lost. Bad records are named in the log.
    """
    if accounts is None:
        return 0
    if rec.get("kind") == "rows":
        return _push_deliver_rows(rec)          # carries its own data, no DB
    if rec.get("kind") == "presencerows":
        return _push_deliver_presencerows(rec, db)  # state/zone at delivery
    if rec.get("kind") == "grouprows":
        return _push_deliver_grouprows(rec, db)  # resolves icons at delivery
    if rec.get("kind") == "gmode":
        return _push_deliver_gmode(rec)         # channel MODE, no DB
    if rec.get("kind") == "mail":
        return _push_deliver_mail(rec, db)      # carries its own record
    missing = [k for k in ("kind", "handle", "event") if k not in rec]
    if missing:
        log("authserv", f"push: ignoring malformed record, no {'/'.join(missing)}"
                        f" ({rec!r})")
        return 0
    own = db is None
    if own:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
        except Exception as exc:
            log("authserv", f"push: db open failed ({exc!r})")
            return 0
    try:
        if rec.get("kind") == "handle":
            return _push_deliver_handle(db, rec)
        return _push_deliver_watchers(db, rec)
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


def _push_deliver_gmode(rec):
    """Announce a group role change on its `#XXL` channel. Runs in authserv.

    SE's own line, captured 2026-08-15 the moment a class changed:

        :202.67.54.139 MODE #XXL000000000002CF6D +o UL0C0F1HJ

    WARNING: **The prefix is the server's raw IP**, not the `pol-1049-51244.pol.com`
    form every numeric uses and not a user prefix. That is SE's shape for an
    unsolicited channel MODE and it is what we send.

    The target is named by MEMBER rather than by nick, because the nick belongs
    to the live session and only this process can see it -- resolving it on the
    lobby side would mean duplicating the alias table and getting a different
    answer whenever the two disagreed.
    """
    gid = int(rec.get("gid", 0))
    member = int(rec.get("member", 0))
    if not gid or not member:
        log("authserv", f"push[gmode]: ignoring malformed record ({rec!r})")
        return 0
    chan = ("#XXL%016X" % gid).encode()
    sessions = [ts for ts in presence.PRESENCE.sessions_for(member) if ts.alive]
    if not sessions:
        # Not an error: a role can change while the member is offline, and the
        # 353 they get on their next JOIN already carries the '@'. Logged for the
        # same reason `_push_deliver_rows` logs it -- so "sent and ignored" and
        # "never sent" stay distinguishable.
        log("authserv", f"push[gmode]: member {member} has no live session; "
                        f"{chan.decode()} MODE not announced (their next JOIN "
                        f"carries it in the 353)")
        return 0
    if not roomregistry.ROOMS.members(chan):
        log("authserv", f"push[gmode]: nobody is in {chan.decode()}; "
                        f"MODE not announced")
        return 0
    target = sessions[0].nick
    flag = b"+o" if rec.get("op") else b"-o"
    line = (b":" + framing._lobby_world_ip().encode() + b" MODE " + chan + b" " +
            flag + b" " + target)
    sent = roomregistry.ROOMS.broadcast(chan, [line])
    log("authserv", f"push[gmode]: {chan.decode()} {flag.decode()} "
                    f"{target.decode('latin1')} -> {sent} member(s)")
    return sent


def _push_deliver_mail(rec, db=None):
    """Tell a logged-in recipient that a message has arrived.

    THE CARRIER IS THE ONE THAT ALREADY EXISTS, and the record is one we already
    have: SE's mail push is the message's own `O/m/` record. Measured 2026-08-16
    on 11 of 11 message reads in polshim-se.429364.log -- every one is preceded
    by a nick-targeted NOTICE carrying that message's token:

        :PMY4QWBZY!~x@ NOTICE UL0C0F1HJ :iPfkQm5uPppy...UOA8Tp...wIhW
        lobby 3:0                    O/m/iPfkQm5uPppy...UOA8TU...

    so the client learns the path from the push and reads the object without
    being asked. That is why mail appears live on SE and only at login here.

    `token` is built by `_mail_push_token` and travels in the spool record, so
    this end neither parses nor rebuilds it.
    """
    token = rec.get("token")
    handle_id = rec.get("handle")
    if not token or not handle_id:
        log("authserv", f"push: mail record with no token/handle ({rec!r})")
        return 0
    own = db is None
    if own:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
        except Exception as exc:
            log("authserv", f"push: db open failed ({exc!r})")
            return 0
    try:
        row = db.execute("SELECT member_id FROM handle WHERE id = ?",
                         (int(handle_id),)).fetchone()
        if row is None:
            return 0
        sent = 0
        for ts in presence.PRESENCE.sessions_for(int(row["member_id"])):
            if not ts.alive:
                continue
            nick = ts.nick.encode() if isinstance(ts.nick, str) else ts.nick
            if ts.send([b":" + pushrecord._push_nick(token).encode() + b"!~x@ NOTICE "
                        + nick + b" :" + token.encode()]):
                sent += 1
        if sent:
            log("authserv", f"push: MAIL {rec.get('subject')!r} from "
                            f"{rec.get('sender')!r} -> handle {handle_id}, "
                            f"{sent} session(s) -- they should read it without "
                            "being asked")
        return sent
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


def _push_spool_watcher():
    """Daemon: drain the spool as the lobby writes to it.

    Tracks a byte offset rather than deleting, so a half-written line is simply
    not read until its newline lands. A file that SHRANK was rotated or cleared
    under us, so the offset resets rather than seeking past the end.

    THE OFFSET IS PERSISTED, and it must be: keeping it in a local made every
    restart replay the whole spool. See `_PUSH_SPOOL_OFFSET` for what that costs
    in the case `authrelay` is built for.
    """
    off = _push_offset_load()
    try:
        # A persisted offset past the end means the spool was replaced while we
        # were down. Start over rather than seeking past EOF and going deaf.
        if off > os.path.getsize(_PUSH_SPOOL):
            log("authserv", f"push[spool] stored offset {off} is past the end -- "
                            f"the spool was replaced; restarting from 0")
            off = 0
    except OSError:
        off = 0
    if off:
        log("authserv", f"push[spool] resuming at byte {off} "
                        f"(not replaying what a previous run already delivered)")
    while True:
        try:
            size = os.path.getsize(_PUSH_SPOOL)
            if size < off:
                off = 0                       # rotated or truncated
                _push_offset_save(off)
            if size > off:
                # BINARY, and that is not a detail. This tracks a BYTE offset,
                # and a text-mode read translates newlines -- so on any platform
                # where the spool has CRLF, `len(line.encode())` undercounts by
                # one byte per line and the next seek lands mid-record. The
                # container writes LF and never noticed; the offset only became
                # durable (and the drift cumulative) when it started being
                # persisted, and the first test written against it hit exactly
                # this on Windows. Bytes in, decode per line, no translation.
                with open(_PUSH_SPOOL, "rb") as f:
                    f.seek(off)
                    chunk = f.read()
                # A trailing fragment with no newline is still being written:
                # leave it, and its bytes, for the next pass. cut == 0 means the
                # whole chunk is that fragment, so there is nothing to do yet.
                cut = chunk.rfind(b"\n") + 1
                if cut:
                    off += cut
                    for raw in chunk[:cut].split(b"\n"):
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            _push_deliver(json.loads(line.decode("utf-8")))
                        except Exception as exc:
                            log("authserv", f"push[spool] bad record ({exc!r})")
                    # AFTER delivering, never before: a crash mid-batch then
                    # replays that batch, which is the right way round. These are
                    # idempotent row/event pushes, so re-delivering a few beats
                    # losing them.
                    _push_offset_save(off)
            off = _push_spool_rotate(off)
        except OSError:
            off = 0                           # no file yet -- inert
        except Exception as exc:
            log("authserv", f"push[spool] error: {exc!r}")
        # OUTSIDE the try, deliberately. The deferred queue has nothing to do
        # with the spool file, and the spool is legitimately ABSENT for a while
        # after a rotation -- inside, `getsize` would raise, jump to the handler,
        # and skip the retry for exactly as long as that lasted. This tick is the
        # retry clock: 0.5s, so a session that registers just after its push was
        # issued still gets its icons promptly.
        try:
            _push_defer_retry()
        except Exception as exc:
            log("authserv", f"push[rows] defer sweep error: {exc!r}")
        time.sleep(0.5)


def broadcast_event(db, subject_handle_id, event, text, subject_name=None):
    """Push one event record to every online friend watching `subject_handle_id`.

    The generic form of `_broadcast_presence`, for the event codes that are NOT
    presence: 0 (a profile field changed -- text is the new value), 7 (a friend
    request -- text is the greeting) and 13 (a Message arrived -- text is the
    subject; the BODY deliberately does not travel, exactly as SE does it).

    Scoped to the SUBJECT HANDLE, not the member. Friend lists are per-handle
    (corrected 2026-08-15 from the account holder's own testing), so only the
    watchers who friended *this* handle should see the change -- a member's other
    handle is, to them, a different person.

    Takes the caller's `db` because every call site already has one open.
    Returns the number of watcher sessions written to -- which is 0 whenever this
    process does not hold the sockets, because the record went to the spool
    instead and authserv will deliver it a moment later.
    """
    return _push_emit({"kind": "watchers", "handle": int(subject_handle_id),
                       "event": int(event), "text": str(text),
                       "name": subject_name}, db)


def _shared_groups(db, watcher_handle, subject_handle):
    """Group ids both handles belong to, for the presence companion record.

    A group id here is the OWNER's `friend` row id -- the same stable identity
    `_groups_for_list` serves and the client turns into `#XXL<id>`, so the number
    in this record is the number the watcher already holds for that group.

    Membership means owning it or sitting in `group_member`; both count, because
    the owner is a member of their own group and SE's records do not distinguish.
    Returns [] on any failure: a missing companion costs a presence nicety, and
    raising here would cost the whole push.
    """
    def ids(hid):
        out = set()
        try:
            for r in db.execute(
                    "SELECT id FROM friend WHERE handle_id = ? AND kind = ?",
                    (int(hid), accounts.KIND_GROUP)):
                out.add(int(r["id"]))
            for r in db.execute(
                    "SELECT group_id FROM group_member WHERE member_handle = ?",
                    (int(hid),)):
                out.add(int(r["group_id"]))
        except Exception:
            return set()
        return out
    try:
        return sorted(ids(watcher_handle) & ids(subject_handle))
    except Exception as exc:
        log("authserv", f"presence: shared-group lookup failed ({exc!r})")
        return []


def _watcher_row_slot(db, watcher_member, subject_guid):
    """Which slot `subject_guid` occupies in `watcher_member`'s friend list.

    The client validates a row push against BOTH the slot and the guid, and the
    slot is per watcher -- the same friend is row 1 to one account and row 6 to
    another. Resolved the same way the 2:3 reply orders its records (friends only,
    in `list_friends` order), because that is the ordering the client is holding.

    None when they are not on that list, which is the right answer for a watcher
    who friended a different handle of ours: no row, no repaint.

    WARNING: ORDERING IS THE WHOLE JOB HERE. This used to enumerate
    `list_friends(status=None)` directly, which counts the watcher's INCOMING
    requests as rows even though the 2:3 reply does not serve them -- so on any
    account holding a request the number this returned was not the number the
    client held, and every repaint built from it was dropped by the client's own
    slot check. `_friend_row_order` is the shared filter now; see its note.

    And the published map wins over any derivation: the client keeps the slots
    it was GIVEN, so when this process served that 2:3 we use what we served.
    In the split-container layout the map lives in the other process, which is
    why a derivation has to exist at all.
    """
    try:
        hid = handlelists._member_primary_handle(db, int(watcher_member))
        for i, r in enumerate(friendroster._friend_row_order(db, hid)):
            if int(r["peer_guid"]) == int(subject_guid):
                slot = i
                if r["peer_handle"] is not None:
                    served = friendroster._friend_slot_raw(db, hid, int(r["peer_handle"]))
                    if served is not None:
                        slot = served
                # Past the PC cap: no row push may address it.
                return slot if friendroster._friend_push_slot_ok(slot) else None
    except Exception as exc:
        log("authserv", f"push: slot lookup failed for member "
                        f"{watcher_member} ({exc!r})")
    return None


def _push_deliver_watchers(db, rec):
    subject_handle_id = int(rec["handle"])
    event, text = int(rec["event"]), rec.get("text", "")
    try:
        row = db.execute("SELECT member_id, handle_name FROM handle WHERE id = ?",
                         (subject_handle_id,)).fetchone()
        if row is None:
            return 0
        watchers = accounts.friend_watchers(db, int(row["member_id"]))
    except Exception as exc:
        log("authserv", f"push: watcher lookup failed ({exc!r})")
        return 0
    guid = accounts.handle_guid(subject_handle_id)
    name = rec.get("name") or row["handle_name"] or ""

    # A PROFILE CHANGE HAS TO REPAINT THE ROW, AND THE SHORT RECORD DOES NOT.
    #
    # `push_lines` builds the SHORT form. That is the right carrier for "something
    # happened" -- it is what the client turns into a notification -- but the
    # picture and the comment live in the friend-row slot, and the only record that
    # writes them is the LONG field record (the one the login spool sends: block +
    # icon + comment, flags 0x05 / 0x25). So editing a comment or a portrait
    # pushed a notification and left the row exactly as it was. Send both: the
    # short line for the event, the long record for the state it describes.
    #
    # The long record needs the subject's slot AS THIS WATCHER SEES IT -- each
    # watcher has their own ordering, and the client checks the slot and the guid
    # before applying anything. That is why this resolves per watcher rather than
    # once for the event.
    icon = cmt = None
    if int(event) == pushchannel._PUSH_EV_PROFILE:
        try:
            prof = accounts.get_handle_profile(db, subject_handle_id)
            icon = int(prof.get(friendgroups._PROFILE_FICON) or 0) or None
            cmt = prof.get(profilerecord._COMMENT_FIELD) or None
        except Exception as exc:
            log("authserv", f"push: profile lookup failed ({exc!r})")

    # THE SHORT RECORD IS A MAIL ANNOUNCEMENT, so a synthesized one is a PHANTOM
    # MESSAGE -- the client 3:0s it as an `O/m/` token, the miss serves zeros,
    # and the recipient gets a blank mail that crashes on open (measured live
    # 2026-08-22 on the retired event-7 push; see the +0x30 retraction at
    # `_PUSH_EV_PROFILE`). SE sends NO short record for a profile change -- the
    # field-list record below is the whole push, and it is what actually paints
    # the row anyway. This send had never fired to a live session (zero
    # `push: event 0` lines in all of authserv.log), so nothing is lost.
    # POL_PUSH_SHORT_EVENT=1 re-arms it for measurement only.
    short_ok = os.environ.get("POL_PUSH_SHORT_EVENT", "0") == "1"
    sent = rows_sent = 0
    for watcher_member, _watcher_handle, subject_handle in watchers:
        if int(subject_handle) != int(subject_handle_id):
            continue                  # they friended a DIFFERENT handle of ours
        slot = (_watcher_row_slot(db, watcher_member, guid)
                if (icon or cmt) and _row_push_enabled() else None)
        for ts in presence.PRESENCE.sessions_for(watcher_member):
            if not ts.alive:
                continue
            if short_ok and ts.send(pushrecord.push_lines(ts.nick, guid, guid, name, text,
                                               event)):
                sent += 1
            if slot is None:
                continue
            try:
                if ts.send(pushrecord.field_push_lines(ts.nick, guid, slot, icon=icon,
                                            comment=cmt,
                                            seq=next(_ROW_PUSH_SEQ), block=True)):
                    rows_sent += 1
            except Exception as exc:
                log("authserv", f"push: row repaint for {name!r} slot {slot} "
                                f"failed ({exc!r})")
    if sent or rows_sent:
        log("authserv", f"push: event {event} for {name!r} ({text!r}) -> "
                        f"{sent} watcher session(s)"
                        + (f", row repainted on {rows_sent}" if rows_sent else ""))
    return sent


#: How long after the 2:3 reply the row pushes go out, in seconds.
#:
#: ORDER MATTERS AND ONLY IN ONE DIRECTION. The client applies a field-list
#: record into friend slot N; the 2:3 reply then COPIES its own record over that
#: slot. Arrive first and the copy can undo us; arrive after and the identity
#: check (`DAT_0387ca80` set -> compare guid + handle bits) is the thing that
#: proves we addressed the right friend. The spool drain already adds ~0.5 s of
#: its own; this makes the margin deliberate instead of accidental.
_ROW_PUSH_DELAY = 1.5

#: ROW PUSHES THAT ARRIVED BEFORE THEIR WATCHER REGISTERED. Held here and
#: retried by the spool watcher's own tick, rather than dropped on the first
#: miss -- see `_push_deliver_rows` for why that miss is normally a race.
#:
#: 30s against a 1.5s issue delay is deliberately generous: the cost of waiting
#: is a face picture arriving a few seconds late, and the cost of giving up too
#: early is that it never arrives at all for that session.
#:
#: NOT persisted, on purpose. A restart means the client reconnects and composes
#: a fresh friend list, which issues its own row push -- so a held record would
#: duplicate that at best and repaint a stale icon at worst.
_PUSH_DEFER = []
_PUSH_DEFER_LOCK = threading.Lock()
_PUSH_DEFER_WINDOW = float(os.environ.get("POL_PUSH_DEFER_S", "30"))
_PUSH_DEFER_MAX = 200


def _push_defer_retry():
    """Re-attempt every deferred row push. Called from the drain tick.

    Drains the list first and lets the per-kind deliverer decide again: it
    re-defers what is still early and drops what has run out of time, so the
    expiry rule lives in exactly one place.

    WARNING: DISPATCH BY KIND. This used to call `_push_deliver_rows` for EVERYTHING in
    the defer list, but `_push_deliver_grouprows` defers into the SAME list --
    and a grouprows record has `groups`, not `rows`, so the retry hit the
    `if not rows: return 0` guard and every deferred group-roster push was
    silently dropped (latent; found 2026-08-23 while adding the presence burst,
    which defers here too). Route each record back to its own deliverer.
    """
    if not _PUSH_DEFER:
        return
    with _PUSH_DEFER_LOCK:
        pending, _PUSH_DEFER[:] = _PUSH_DEFER[:], []
    for rec in pending:
        kind = rec.get("kind", "rows")
        try:
            if kind == "presencerows":
                _push_deliver_presencerows(rec)
            elif kind == "grouprows":
                _push_deliver_grouprows(rec)
            else:
                _push_deliver_rows(rec)
        except Exception as exc:
            log("authserv", f"push[{kind}] deferred retry failed ({exc!r})")

#: A monotonic sequence for the row push's +0x30.
#:
#: The client keeps the last (timestamp, seq) it applied to a slot and drops
#: anything not STRICTLY newer -- `uStack_24 < uStack_2e0 || (uStack_24 <=
#: uStack_2e0 && uStack_28 <= uStack_2e4)`. Timestamps are whole seconds, so two
#: pushes for the same friend inside one second tie on the clock and the second
#: is discarded unless the sequence breaks the tie.
_ROW_PUSH_SEQ = itertools.count(1)


def push_friend_icons(db, watcher_member_id, rows):
    """Paint the face pictures on ONE watcher's friend list.

    `rows` is `[(slot, guid, icon, comment), ...]` straight out of the 2:3 reply
    we just composed -- the same slot index and the same guid, because the client
    checks both before it applies anything. `comment` may be None; `icon` may be 0.

    WARNING: THE SPOOL LINE IS JSON, so every element must survive a round trip -- which
    is why the comment is coerced to `str`/None here rather than passed through.
    And this runs INSIDE the 2:3 handler: an exception here does not merely skip
    the push, it aborts the reply and drops the lobby connection, which the client
    reports as **POL-0008**. That is exactly what a 3-tuple unpack did when the
    comment was added (2026-08-16) -- the record was fine and the login died
    anyway. Keep this tolerant, and keep it cheap.

    THIS IS WHY THE FRIEND LIST DREW NO PICTURES. The `2:3` row has no icon field
    (settled by search against SE's own replies), the 0:9 handle table is not
    where the row reads one either (tried, changed nothing), and the account
    holder's OWN badge worked all along because it comes from a third place. SE
    delivers a friend's picture on the push channel, as a field-list record, and
    until now we could not build one.
    """
    out = []
    for row in rows:
        s, g, i = row[0], row[1], row[2]
        c = row[3] if len(row) > 3 else None
        if not friendroster._friend_push_slot_ok(s):
            continue                    # past the PC table -- see the helper
        out.append([int(s), int(g), int(i), str(c) if c else None])
    if not out:
        return 0
    return _push_emit({"kind": "rows", "member": int(watcher_member_id),
                       "rows": out,
                       "after": time.time() + _ROW_PUSH_DELAY}, db)


def push_presence_burst(db, watcher_member_id, rows):
    """Queue the INITIAL PRESENCE BURST for ONE watcher's fresh 2:3 list.

    `rows` is `[(slot, guid, friend_handle_id), ...]` for every friend the reply
    just served ONLINE -- the same slot and the SAME guid variable the row record
    was built from, so the push identity matches the row by construction (the
    identity-drift class `_push_identity_guid` exists for cannot occur here).

    WHY THIS EXISTS: the online icon is painted ONLY by the push -- the 2:3 row
    carries no presence the client reads (proven from app.dll 0x0488efc5 +
    polcore 0x037deeb0 and the SE capture, 2026-08-23) -- and `_broadcast_presence`
    fires only on TRANSITIONS. Without a burst, a freshly fetched list shows
    every friend grey until each one next changes state, which is exactly the
    account holder's "fresh restart, everyone offline" report. Retail clients
    showed online friends at sign-in, so SE must deliver current state after the
    list; this is that delivery.

    STATE AND ZONE ARE RESOLVED AT DELIVERY TIME, in authserv -- `_presence_zone`
    reads the live PRESENCE/ROOMS registries and `_member_status` the 4:5 latch,
    none of which exist in the lobby process (the empty-registry artefact
    noted 2026-08-23). The spool carries only identities.

    ALL friends ride the burst, OFFLINE INCLUDED -- measured off SE's own burst
    (auth429364.pkl, decoded 2026-08-23): after a (re)login 2:3, every friend
    gets a presence assertion on the auth band, `01 03 00 00 e8 03` (online,
    zone 0x3E8) or `01 01 00 00 00 00` (offline, zone 0) at main+0x10, keyed by
    the 2:3 slot at main+0x1c. The offline assertion is not decorative: it is
    what makes a stale online icon from an earlier session go grey.

    Gated by `POL_FRIEND_PRESENCE_BURST` (default ON; also live-tunable as
    `burst=` in logs/presence.ctl). Same JSON round-trip and POL-0008 rules as
    `push_friend_icons` -- this runs inside the 2:3 handler.

    Returns the number of rows QUEUED (the spool path cannot know delivery
    counts; those are authserv's `push[presence-burst]` log line). 0 = gated
    off or nothing to queue.
    """
    if presence._presence_cfg("burst", "POL_FRIEND_PRESENCE_BURST", "1") != "1":
        return 0
    # The master presence-push gate, checked here so the return value is honest
    # -- `_push_emit` returns 0 BOTH for "refused by the master gate" and for
    # "spooled fine" (the spool cannot know), which made the first live burst
    # log "NOT queued" while authserv was delivering it two seconds later.
    if presence._presence_cfg("push", "POL_PRESENCE_PUSH", "0") != "1":
        return 0
    out = [[int(s), int(g), int(h)] for s, g, h in rows
           if friendroster._friend_push_slot_ok(s)]          # past the PC table: never
    if not out:
        return 0
    _push_emit({"kind": "presencerows", "member": int(watcher_member_id),
                "rows": out,
                "after": time.time() + _ROW_PUSH_DELAY}, db)
    return len(out)


def _push_deliver_presencerows(rec, db=None):
    """Send one watcher's initial presence burst. Runs in authserv.

    Mirrors `_push_deliver_rows` (sessions, bounded defer) but sends the
    PRESENCE record shape: block-only field records with `+0x11` = the friend's
    CURRENT state and `+0x14` = their zone -- the exact shape (and builder) the
    transition push uses, so whatever the client accepts from
    `_broadcast_presence` it accepts from the burst.
    """
    member = int(rec.get("member", 0))
    # A spool line may come from anywhere (an older process, a hand edit): the
    # PC-cap rule is enforced here again, not only where rows are queued.
    rows = [r for r in (rec.get("rows") or []) if friendroster._friend_push_slot_ok(r[0])]
    if not member or not rows:
        return 0
    wait = min(max(0.0, float(rec.get("after", 0)) - time.time()),
               _ROW_PUSH_DELAY + 1.0)
    if wait:
        time.sleep(wait)
    sessions = [ts for ts in presence.PRESENCE.sessions_for(member) if ts.alive]
    if not sessions:
        # Same registration race as the icon rows; same bounded defer. The
        # retry dispatches by `kind`, so a deferred burst comes back HERE.
        deadline = rec.get("defer_until")
        if deadline is None:
            deadline = time.time() + _PUSH_DEFER_WINDOW
            rec["defer_until"] = deadline
        if time.time() < deadline:
            with _PUSH_DEFER_LOCK:
                if len(_PUSH_DEFER) < _PUSH_DEFER_MAX:
                    _PUSH_DEFER.append(rec)
                    return 0
                log("authserv", f"push[presence-burst]: defer queue full at "
                                f"{_PUSH_DEFER_MAX}; member {member} not held")
        log("authserv", f"push[presence-burst]: member {member} has NO live "
                        f"push session -- {len(rows)} row(s) dropped")
        return 0
    own = None
    if db is None:
        try:
            db = own = accounts.connect(
                os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        except Exception as exc:
            log("authserv", f"push[presence-burst]: db open failed ({exc!r})")
            return 0
    try:
        # Resolve each friend's CURRENT state once, not per session.
        resolved = []
        for row in rows:
            slot, guid, fhandle = int(row[0]), int(row[1]), int(row[2])
            try:
                h = db.execute("SELECT member_id FROM handle WHERE id = ?",
                               (fhandle,)).fetchone()
                fmember = int(h["member_id"]) if h else 0
            except Exception:
                fmember = 0
            if not fmember:
                continue
            # CURRENT state, not the serve-time snapshot: SE asserts EVERY
            # friend's state in the burst, offline as `01 01 00 00 00 00`
            # (state 1, zone 0) -- measured, auth429364.pkl. This also covers
            # a friend who logged out inside the 1.5 s spool delay: they get
            # the offline assertion instead of a stale online paint.
            online = False
            try:
                online = accounts.member_online(db, fmember)
            except Exception:
                pass                      # on doubt, assert offline
            if online:
                away = False
                try:
                    away = memberstatus._status_is_away(memberstatus._member_status(fmember))
                except Exception:
                    pass
                state = pushrecord._presence_field_state("away" if away else "online")
            else:
                state = pushrecord._presence_field_state("offline")
            if state is None:
                continue
            zone = pushrecord._presence_zone(fmember, state)
            resolved.append((slot, guid, state, zone))
        if not resolved:
            return 0
        when = int(time.time())
        sent = 0
        for ts in sessions:
            lines = []
            for slot, guid, state, zone in resolved:
                try:
                    lines += pushrecord.field_push_lines(
                        ts.nick, guid, slot, state=state, zone=zone,
                        seq=next(_ROW_PUSH_SEQ), when=when)
                except ValueError as exc:
                    log("authserv",
                        f"push[presence-burst]: slot {slot} skipped ({exc})")
            if lines and ts.send(lines):
                sent += 1
        log("authserv", "push[presence-burst]: "
                        f"{[(s, f'+0x11={st}', f'zone={z}') for s, _g, st, z in resolved]}"
                        f" -> member {member}, {sent}/{len(sessions)} session(s)")
        return sent
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass


def push_group_rosters(db, watcher_member_id, entries):
    """Paint the pictures and roles on ONE watcher's GROUP rosters.

    `entries` is `[[group_id, [[guid, name, class], ...]], ...]` -- the same
    tuples the 7:12 member records were just composed from, because the client
    checks the packed word against what the list carried. The 32-byte member
    record has NO icon field (its 8+8+15+NUL layout is full, measured against
    SE's CRAZY PEOPLE replies), so this push is the ONLY carrier of a group
    member's face picture -- SE delivers one per member, ev = group_id<<8,
    chunk mask 0x16 (OBJECT|ICON|NAME), decoded 2026-08-18 from the narrated
    capture where the role handoffs step the class bits 2->3->4 in sequence.

    Icons and each member's push-space subject id are resolved at DELIVERY time
    (authserv opens the db there anyway); this keeps the emit inside the 7:12
    handler cheap and exception-tolerant -- the POL-0008 lesson from the friend
    row push applies unchanged.
    """
    out = []
    for gid, mems in entries:
        if not gid or not mems:
            continue
        out.append([int(gid), [[int(m[0]), str(m[1]), int(m[2])]
                               for m in mems]])
    if not out:
        return 0
    return _push_emit({"kind": "grouprows", "member": int(watcher_member_id),
                       "groups": out,
                       "after": time.time() + _ROW_PUSH_DELAY}, db)


def _push_deliver_grouprows(rec, db=None):
    """Send one watcher's group-roster pushes. Runs in authserv."""
    member = int(rec.get("member", 0))
    groups = rec.get("groups") or []
    if not member or not groups:
        return 0
    wait = min(max(0.0, float(rec.get("after", 0)) - time.time()),
               _ROW_PUSH_DELAY + 1.0)
    if wait:
        time.sleep(wait)
    sessions = [ts for ts in presence.PRESENCE.sessions_for(member) if ts.alive]
    if not sessions:
        # Same race as the friend rows: the push is issued while the list is
        # composed and the push session registers on its own schedule. Reuse
        # the bounded defer rather than reinventing it.
        deadline = rec.get("defer_until")
        if deadline is None:
            deadline = time.time() + _PUSH_DEFER_WINDOW
            rec["defer_until"] = deadline
        if time.time() < deadline:
            with _PUSH_DEFER_LOCK:
                if len(_PUSH_DEFER) < _PUSH_DEFER_MAX:
                    _PUSH_DEFER.append(rec)
                    return 0
        log("authserv", f"push[grouprows]: member {member} has no live push "
                        f"session; {len(groups)} group(s) dropped")
        return 0
    own = db is None
    if own:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
        except Exception as exc:
            log("authserv", f"push[grouprows]: db open failed ({exc!r})")
            return 0
    try:
        byname = {r["handle_name"]: r for r in db.execute(
            "SELECT id, handle_name, client_guid FROM handle")}
        icons = handlelists._face_icons_by_handle()
    finally:
        if own:
            db.close()
    when = int(time.time())
    sent = 0
    for ts in sessions:
        lines = []
        for gid, mems in groups:
            for guid, name, cls in mems:
                h = byname.get(name)
                # The subject id must be the same value whose masked form the
                # 7:12 member record carries at +0x00 -- client-space when the
                # client has told us one, our guid otherwise (the same choice
                # `_group_member_record` makes). SE keeps these identical
                # (member +0x00 == friend row +0x10 == push subject ^ MASK).
                subject = (int(h["client_guid"]) if h and h["client_guid"]
                           else int(guid))
                # KEY: *** THE GROUP MEMBER TABLE IS KEYED IN THE 7:12 DIALECT,
                # NOT THE PUSH ONE - AND THAT IS WHY EVERY MEMBER APPEARED
                # TWICE. *** MEASURED LIVE 2026-08-25, single-variable A/B on
                # prod with the account holder watching the screen: six members
                # served, six pushed, list showed TWELVE; `rowpush=0` in
                # group.ctl (fetch at 04:29:46Z served 6, zero pushes followed)
                # and the same list showed SIX. So this push does not UPDATE the
                # member the 7:12 record installed, it APPENDS a second one.
                #
                # The install path is shared with the 7:12 record, so it finds-
                # or-appends on the member's identity -- and the two records
                # were stating that identity in different dialects. The 7:12
                # member record carries `+0x00` RAW (measured against the live
                # per-session key K: `cft_0355(member+0x00) == own_id` needs the
                # raw client_guid, and that
                # is the fix that made the owner recognise their own row).
                # `build_field_push_record` XORs `subject` by `_PUSH_GUID_MASK`
                # unconditionally -- correct for the FRIEND family, where the
                # masked form is measured to land (the 2026-08-23 online-icon
                # fix) -- so the wire carried `client_guid ^ MASK` here against
                # the record's `client_guid`. Pre-XOR cancels the builder's XOR
                # and puts the SAME 8 bytes in both records. The builder is left
                # alone: the friend family shares it and is measured correct.
                #
                # WARNING: WHAT THIS IS NOT. `ev = group<<8 | sub` (we send `sub`=0 for
                # every member) was the other suspect and the SAME measurement
                # refutes it: a `sub`-keyed lookup would have matched member 0 --
                # the viewer's own row, whose identity is right by construction --
                # and duplicated only the other five, giving ELEVEN. Twelve is a
                # clean doubling of all six, i.e. NO member ever matched, which
                # is an identity failure and not an index one. Fixing `sub`
                # anyway would have been a plausible-mechanism change with no
                # measurement behind it; it stays unfixed and unclaimed.
                #
                # POL_GROUP_ROWPUSH_RAW=0 restores the masked form for an A/B.
                if os.environ.get("POL_GROUP_ROWPUSH_RAW", "1") == "1":
                    subject ^= pushchannel._PUSH_GUID_MASK
                icon = icons.get(int(h["id"])) if h else None
                # WARNING:KEY: **THE SAME PRODUCER THE 7:12 RECORD USES, AND THAT IS THE
                # FIX for the long-standing roster-doubling bug.** The push's installer (polcore
                # `FUN_037e9650`) finds-or-appends on the LOW 44 BITS OF THIS
                # WORD and looks at nothing else -- not `+0x00`, which is what
                # the 2026-08-25 "dialect" fix aligned, and why that fix changed
                # nothing on screen. This line used to build the word itself
                # from a RAW `guid & 0xFFFFFFFF`, while `_group_member_record`
                # had begun writing the TAGGED z_hid form there three days
                # earlier (POL_GROUP_MEMBER_ZHID, 2026-08-22, so a group row's
                # "View Profile" would resolve). Two producers, one comparison:
                # the low 44 bits differed for every member, every push
                # appended, and the roster doubled. See `_group_member_packed`.
                packed = friendgroups._group_member_packed(guid, cls)
                try:
                    lines += pushrecord.field_push_lines(
                        ts.nick, subject, 0,
                        icon=icon if icon else None, name=name,
                        seq=((int(gid) << 8) & 0xFFFFFFFF), when=when,
                        group=gid, gpacked=packed)
                except ValueError as exc:
                    log("authserv", f"push[grouprows]: {name!r} skipped ({exc})")
        if lines and ts.send(lines):
            sent += 1
    log("authserv", f"push[grouprows]: {sum(len(m) for _g, m in groups)} "
                    f"member record(s) across {len(groups)} group(s) -> member "
                    f"{member}, {sent}/{len(sessions)} session(s)"
                    + ("" if sent else " -- SEND FAILED"))
    return sent


def _push_deliver_rows(rec):
    """Send one watcher's friend-row updates. Runs in authserv."""
    member = int(rec.get("member", 0))
    # Same PC-cap rule as the presence burst, re-checked at delivery.
    rows = [r for r in (rec.get("rows") or []) if friendroster._friend_push_slot_ok(r[0])]
    if not member or not rows:
        return 0
    # Honour the delay the writer asked for. Bounded, because a bad clock or a
    # stale spool line must not park the drain thread indefinitely.
    wait = min(max(0.0, float(rec.get("after", 0)) - time.time()),
               _ROW_PUSH_DELAY + 1.0)
    if wait:
        time.sleep(wait)
    sessions = [ts for ts in presence.PRESENCE.sessions_for(member) if ts.alive]
    if not sessions:
        # *** A MISS HERE IS USUALLY A RACE, NOT AN ABSENCE, SO WAIT BEFORE
        # GIVING UP. *** The comment this replaces already named the cause --
        # "a registration-timing question, not a record question" -- and then
        # dropped the record anyway. The row push is issued as the 2:3 friend
        # list is composed, `_ROW_PUSH_DELAY` is only 1.5s, and the watcher's
        # session registers on its own schedule; lose that race and the friend
        # keeps whatever face picture they had, because the icon does NOT travel
        # in the 2:3 record and the push is its only carrier.
        #
        # So retry for a bounded window instead. Deferral is IN MEMORY and
        # deliberately not persisted: a restart means the client reconnects and
        # composes a fresh list, which issues its own push, so a held record
        # would at best duplicate it and at worst repaint a stale icon.
        deadline = rec.get("defer_until")
        if deadline is None:
            deadline = time.time() + _PUSH_DEFER_WINDOW
            rec["defer_until"] = deadline
        if time.time() < deadline:
            with _PUSH_DEFER_LOCK:
                if len(_PUSH_DEFER) < _PUSH_DEFER_MAX:
                    _PUSH_DEFER.append(rec)
                    return 0
                # The cap is the backstop against a flood of pushes for members
                # who really are gone. Say when it bites -- silently discarding
                # past a threshold is how the original silence got here.
                log("authserv", f"push[rows]: defer queue full at "
                                f"{_PUSH_DEFER_MAX}; member {member} not held")
        # Out of time (or out of room): now it is a real miss, and it gets the
        # same diagnosis it always did. That line is load-bearing -- it is what
        # separates "we never sent it" from "we sent it and the client ignored
        # it", which need different fixes.
        try:
            with presence.PRESENCE._lock:
                known = sorted(m for m, v in presence.PRESENCE._by_member.items() if v)
        except Exception:
            known = "?"
        log("authserv", f"push[rows]: member {member} has NO live push session "
                        f"after {_PUSH_DEFER_WINDOW:.0f}s -- {len(rows)} row(s) "
                        f"dropped; registered: {known}")
        return 0
    when = int(time.time())
    sent = 0
    for ts in sessions:
        lines = []
        for row in rows:
            # 4-tuples now (the comment came later); tolerate the 3-tuple form so
            # a spool line written by an older process still drains.
            slot, guid, icon = row[0], row[1], row[2]
            rcmt = row[3] if len(row) > 3 else None
            try:
                # block=True IS REQUIRED, AND IT IS WHY THE PICTURE NEVER DREW.
                #
                # `build_field_push_record` omitted field bit 0x01 on the reading
                # that "the icon is applied independently of it". Measured: it is
                # not. Decoding the record we actually sent and diffing it against
                # SE's own 87-byte icon push left exactly one structural byte
                # apart -- +0x48, the field-flags word: ours 0x04 (ICON), SE 0x05
                # (BLOCK|ICON). SE never sends ICON without BLOCK; all five of its
                # icon records are 0x05. Re-fired with block=1 and the friend's
                # picture appeared, on every row, immediately (2026-08-16).
                #
                # The stated reason for leaving it out was that the +0x10 block
                # also writes the friend's STATUS word, so a wrong action byte
                # could flip somebody offline. That risk is now measured away
                # rather than argued away: with block=True our record differs from
                # SE's in the token, the timestamp and the icon value and NOTHING
                # else, so the action byte we send is the one SE sends.
                lines += pushrecord.field_push_lines(
                    ts.nick, guid, slot,
                    icon=icon if icon else None,
                    comment=rcmt,
                    seq=next(_ROW_PUSH_SEQ), when=when, block=True)
            except ValueError as exc:
                log("authserv", f"push[rows]: slot {slot} skipped ({exc})")
        if lines and ts.send(lines):
            sent += 1
    # Log the FAILURE too. `if sent:` alone meant a send that returned falsy --
    # a socket that went away between the lookup and the write -- looked exactly
    # like the push never being attempted.
    log("authserv", f"push[rows]: {len(rows)} row(s) -> member {member}, "
                    f"{sent}/{len(sessions)} session(s)"
                    + ("" if sent else " -- SEND FAILED"))
    return sent


def push_to_handle(db, target_handle_id, event, text, from_handle_id=None,
                   from_name=None):
    """Push one event record to whoever is logged in on `target_handle_id`.

    The other direction from `broadcast_event`: that one tells my WATCHERS that
    something about me changed, this one tells ONE named person something aimed
    at them -- a friend request (7) or an arriving Message (13).

    Returns the number of sessions written to; 0 if they are not online, which is
    normal and not an error. Nothing is queued: SE's push is a live notification,
    and the durable copy is the friend row or the Message itself, which the
    recipient picks up at next login the ordinary way.
    """
    return _push_emit({"kind": "handle", "handle": int(target_handle_id),
                       "event": int(event), "text": str(text),
                       "from_handle": int(from_handle_id or target_handle_id),
                       "name": from_name}, db)


def _push_deliver_handle(db, rec):
    target_handle_id = int(rec["handle"])
    event, text = int(rec["event"]), rec.get("text", "")
    try:
        row = db.execute("SELECT member_id FROM handle WHERE id = ?",
                         (target_handle_id,)).fetchone()
        if row is None:
            return 0
    except Exception as exc:
        log("authserv", f"push: target lookup failed ({exc!r})")
        return 0
    guid = accounts.handle_guid(int(rec.get("from_handle") or target_handle_id))
    sent = 0
    for ts in presence.PRESENCE.sessions_for(int(row["member_id"])):
        if not ts.alive:
            continue
        if ts.send(pushrecord.push_lines(ts.nick, guid, guid, rec.get("name") or "",
                              text, event)):
            sent += 1
    if sent:
        log("authserv", f"push: event {event} to handle {target_handle_id} "
                        f"({text!r}) -> {sent} session(s)")
    return sent


#: On-demand presence TRIGGER, for live bring-up only. During the field sweep it is
#: tedious to make a real friend log in/out for every candidate value, so this
#: watches a control file and fires `_broadcast_presence` directly -- the watcher's
#: friend-list slot updates without them touching a client. Write e.g.
#:   member=7 state=offline
#: (then again with state=online) to flip member 7 for its watchers. `subject` names
#: the friend whose slot moves; the watchers are whoever has them friended and is
#: online. A `seq=<n>` line overrides the sequence for testing the monotonic gate.
#: Removed once presence is confirmed -- it is a bring-up scaffold, gated on the
#: file simply not existing (so it is inert in normal operation).
_PRESENCE_FIRE_FILE = os.environ.get(
    "POL_PRESENCE_FIRE", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                      "presence-fire.ctl"))


def _presence_fire_watcher():
    """Daemon: poll the fire file; on change, fire the requested presence push."""
    last = None
    while True:
        try:
            st = os.stat(_PRESENCE_FIRE_FILE)
            if st.st_mtime != last:
                last = st.st_mtime
                kv = {}
                with open(_PRESENCE_FIRE_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            kv[k.strip()] = v.strip()
                if "member" in kv:
                    member = int(kv["member"], 0)
                    state = kv.get("state", "online")
                    seq = int(kv["seq"], 0) if "seq" in kv else None
                    log("authserv", f"presence[fire]: member={member} state={state}"
                                    + (f" seq={seq}" if seq is not None else ""))
                    pushrecord._broadcast_presence(member, state,
                                        subject_name=kv.get("name"), seq=seq)
        except OSError:
            last = None                       # file gone -> inert, keep polling
        except Exception as exc:
            log("authserv", f"presence[fire] error: {exc!r}")
        time.sleep(1.0)


def _title_zone_watcher():
    """Daemon: entering or leaving a TITLE is a presence change too.

    `_broadcast_presence` fires only on login / logout / away-back, so the zone
    the client reports on 4:5 -- written by the LOBBY container, a different
    process, see `_publish_title_zone` -- would just sit in the file until some
    unrelated event happened to push. Measured 2026-08-16: the zone reached the
    file 9 s AFTER the last push, so a watcher's friend list never saw it.
    Watching the file is what makes "X is in Tetra Master" arrive when it
    becomes true rather than at their next login.

    Offline members are skipped: the logout push already said zone 0, and
    re-asserting "online" for someone who is not would be a lie with a record
    attached.
    """
    seen = {}
    while True:
        try:
            # WARNING: THE mtime GATE IS GONE, DELIBERATELY. It used to skip this whole
            # block unless the file had changed, which is right for a latched
            # entry (it only ever changes by being written) and WRONG for a
            # lease: a lease lapses because the clock moved, not because anyone
            # touched the file, so under the old gate an expiring lease would
            # never have produced a "left the title" push at all. The read
            # underneath is still mtime-cached, so a tick that finds nothing new
            # costs one stat() and a dict comprehension.
            now = {k: int(v["zone"])
                   for k, v in (titlezone._live_title_zones() or {}).items()
                   if not titlezone._title_zone_expired(v)}
            for member in set(now) | set(seen):
                if now.get(member) == seen.get(member):
                    continue
                try:
                    mid = int(member)
                except (TypeError, ValueError):
                    continue
                if not presence.PRESENCE.is_online(mid):
                    continue
                log("authserv", f"presence[title]: member={mid} zone="
                                f"{now.get(member, 'left the title')} -- pushing")
                pushrecord._broadcast_presence(mid, "online")
            seen = now
        except Exception as exc:
            log("authserv", f"presence[title] error: {exc!r}")
        time.sleep(1.0)


def _member_status_watcher():
    """Daemon: a 4:5 STATUS change is a presence change, so push it.

    The exact shape of `_title_zone_watcher` beside it, and for the same reason:
    the 4:5 request lands on the LOBBY container and the push has to leave from
    `authsess`, so the lobby writes a file (`_publish_member_status`) and this
    side watches it. Without a watcher the new status would sit there until some
    unrelated event happened to push -- which for a user who just set themselves
    Away means their friends learn about it at their next login.

    Away/back rather than online/offline: the friend-list slot already models
    away-ness (`_PRESENCE_ACTION`, `_PRESENCE_FIELD_STATE`), and "invisible" is
    served as away too -- the one thing we must NOT do is push "offline" for it,
    because a member who is still connected and still answering keepalives is
    not offline, and `_peer_online` would immediately contradict us.

    WARNING: **THE PUSH ITSELF IS INFERRED, NOT CAPTURED.** SE's own presence push for a
    status change lands on the FRIEND's connection, and the 2026-08-19 capture is
    of the person CHANGING their status -- so we have their 4:5 and the ack, and
    nothing of what their friends were told. What goes out here is the existing
    `_broadcast_presence` record, which IS measured for login/logout/away
    (`pol-friend-presence`); only the decision to fire it on a 4:5 is new. If a
    friend's list turns out to redraw wrongly, this is the thing to disable
    (POL_STATUS_PUSH=0) -- not the store, which the WHO letter also reads.

    Offline members are skipped, exactly as the title watcher skips them: their
    logout push already said everything, and re-asserting anything for someone
    who is not connected is a lie with a record attached.
    """
    if os.environ.get("POL_STATUS_PUSH", "1") != "1":
        log("authserv", "presence[status]: watcher disabled (POL_STATUS_PUSH=0)")
        return
    seen = {}
    while True:
        try:
            now = {k: int(v.get("code") or 0)
                   for k, v in (memberstatus._live_member_status() or {}).items()}
            for member in set(now) | set(seen):
                if now.get(member) == seen.get(member):
                    continue
                try:
                    mid = int(member)
                except (TypeError, ValueError):
                    continue
                if not presence.PRESENCE.is_online(mid):
                    continue
                code = now.get(member, 0)
                state = "away" if memberstatus._status_is_away(code) else "back"
                log("authserv", f"presence[status]: member={mid} "
                                f"status={code:#04x} -> {state} -- pushing")
                pushrecord._broadcast_presence(mid, state)
            seen = now
        except Exception as exc:
            log("authserv", f"presence[status] error: {exc!r}")
        time.sleep(1.0)


#: ON-DEMAND ROW PUSH, for bringing the face icon up without costing a login.
#:
#: Pushing at login turned out to be the expensive place to be wrong: a bad
#: record there is POL-5135 and a full relogin, and it confounds "the record is
#: malformed" with "this moment is wrong" (see `_row_push_enabled`). This fires
#: ONE record at a client sitting idle at the menu, which is the situation SE's
#: own pushes actually arrive in -- so a failure here indicts the RECORD, and a
#: success moves the question to the timing.
#:
#: Write, to `logs/rowpush-fire.ctl`:
#:
#:     member=1 slot=1 icon=2550
#:
#: `guid` defaults to the friend the 2:3 reply put in that slot; pass it
#: explicitly to test a mismatch. `name=` and `comment=` add those fields.
#: Needs `rowpush=fire` (or `=1`) in presence.ctl. Inert when the file is absent.
_ROW_FIRE_FILE = os.environ.get(
    "POL_ROWPUSH_FIRE", os.path.join(os.environ.get("POL_LOG_DIR", "/logs"),
                                     "rowpush-fire.ctl"))


def _row_fire_watcher():
    """Daemon: poll the fire file; on change, send ONE row-push record."""
    last = None
    while True:
        try:
            st = os.stat(_ROW_FIRE_FILE)
            if st.st_mtime != last:
                last = st.st_mtime
                kv = {}
                with open(_ROW_FIRE_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            kv[k.strip()] = v.strip()
                if "member" in kv:
                    _row_fire(kv)
        except OSError:
            last = None                       # file gone -> inert, keep polling
        except Exception as exc:
            log("authserv", f"rowpush[fire] error: {exc!r}")
        time.sleep(1.0)


def _row_fire(kv):
    mode = presence._presence_cfg("rowpush", "POL_FRIEND_ROW_PUSH", "0")
    if mode not in ("1", "fire"):
        log("authserv", f"rowpush[fire]: ignored, rowpush={mode!r} "
                        f"(set rowpush=fire in presence.ctl)")
        return
    member = int(kv["member"], 0)
    slot = int(kv.get("slot", "0"), 0)
    guid = int(kv["guid"], 0) if "guid" in kv else None
    if guid is None and accounts is not None:
        # Whichever friend the 2:3 reply put in that slot -- the identity the
        # client will compare against. Guessing it is the whole point of the
        # check, so resolve it the same way the list did rather than by hand.
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                rows = [r for r in accounts.list_friends(
                    db, handlelists._member_primary_handle(db, member), status=None)
                    if int(r["kind"]) == accounts.KIND_FRIEND]
            finally:
                db.close()
            guid = int(rows[slot]["peer_guid"]) if slot < len(rows) else None
        except Exception as exc:
            log("authserv", f"rowpush[fire]: guid lookup failed ({exc!r})")
    if guid is None:
        log("authserv", "rowpush[fire]: no guid for that slot; pass guid=")
        return
    kw = {}
    if "icon" in kv:
        kw["icon"] = int(kv["icon"], 0)
    if "name" in kv:
        kw["name"] = kv["name"]
    if "comment" in kv:
        kw["comment"] = kv["comment"]
    if "hslot" in kv:
        kw["hslot"] = int(kv["hslot"], 0)
    if "block" in kv:
        kw["block"] = kv["block"] == "1"
    sessions = [ts for ts in presence.PRESENCE.sessions_for(member) if ts.alive]
    log("authserv", f"rowpush[fire]: member={member} slot={slot} "
                    f"guid={guid:#x} {kw} -> {len(sessions)} session(s)")
    if not friendroster._friend_push_slot_ok(slot):
        log("authserv", f"rowpush[fire]: slot {slot} is past the PC table -- "
                        "refused")
        return
    for ts in sessions:
        lines = pushrecord.field_push_lines(ts.nick, guid, slot,
                                 seq=next(_ROW_PUSH_SEQ), **kw)
        log("authserv", f"rowpush[fire]:   {lines[0].decode('latin1')}")
        ts.send(lines)
