"""Game envelopes (NOTICE G<tag>G...) on the auth band: title dispatch and the POLpro classes."""
import os
import time
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
from .deps import polpro
from . import authnode, ircband, pfc



def _game_notice_reply(arg, nick, srv, sess=None):
    """Answer a content module's world traffic on the auth band, or None.

    `sess` is this connection's ChatSession when the caller has one: it is
    handed to the title with the envelope, and a title pins it as the socket
    its unprompted records go down (the SAME one the game talks on).

    CAPTURED LIVE 2026-08-12, and it corrects the endpoint this whole workstream
    was built on. The PS2 title does NOT open its own socket to its
    game host's 51272 port -- that port is only an immediate inside the
    module, and the module never connects. What actually happens when a game
    launches on the PS2:

        DNS  gi003.pol.com          -> us
        TCP  gi003.pol.com:51241    -> an ORDINARY AUTH SESSION (SESSION token,
                                       USER, NICK, welcome -- all of it already
                                       working)
        then NOTICE <peer> :G<tag>G<line>

    So the world rides the auth band, through the Viewer core's IRC client,
    exactly as predicted for the carrier (the transport is IRC and it lives in
    the Viewer core, not the game) -- the prediction just got the
    port wrong. The envelope is 'G' + a three-character SERVICE TAG + 'G':

        <tag> a title's own format (a 44-character binary record line, or
              `<8 hex code>@Cmd=` text lines) -- handed to the title registered
              for that tag through `titles.notice`; the shared POLpro classes
              stay here

    Returning None here is byte-identical to the pre-2026-08-12 behaviour, which
    is what produced the black screen: the game sends its first request and
    blocks, and ~19 s later the hop closes and the Viewer falls back to the
    portal.
    """
    if os.environ.get("POL_GAME_NOTICE", "1") != "1" or not titles.loaded():
        return None
    target, _, text = arg.partition(b" :")
    target = target.strip()
    # THE CLASS CHARACTER IS NOT ALWAYS 'G'. This used to require text[4:5]=='G'
    # and so ignored two whole sub-protocols. Measured 2026-08-13:
    #   G  the 'B' binary record   (the PS2 title's wire codec)
    #   P  profile   <PG>...       (plaintext, polpro)
    #   R  ranking   <RR>...       (plaintext, polpro)
    # A CHANNEL TARGET IS CHAT, NOT A GAME ENVELOPE. Measured 2026-08-15 against
    # SE's live service and then against our own log: group chat sends its member
    # /presence records as `NOTICE #XXL<id> :G...`, and this function was
    # classifying them as game envelopes (tag=b'TTT', which is base-64 zeros --
    # not a service id) and swallowing them. The game carrier is addressed to a
    # peer NICK ("NOTICE <peer-nick> :G<tag>G<payload>"); nothing that
    # starts with '#' is one. Symptoms this caused: the sender's own name missing
    # from the member sidebar, and chat that never reached the room.
    if target.startswith(b"#"):
        return None
    if not text or text[:1] != b"G" or len(text) < 6:
        return None                     # not a game envelope -- ordinary NOTICE
    tag, cls, payload = text[1:4], text[4:5], text[4:]
    if not cls.isalpha():
        return None
    # WARNING: LOG THE PAYLOAD, NOT JUST ITS LENGTH. Between this line and the
    # title's handler there are many branches, several of which return without
    # logging -- so a message swallowed on the way looked IDENTICAL to one that
    # was never sent. That is what the post-game "Change Settings" screen hit on
    # 2026-09-07T02:20Z: a 27-byte envelope arriving every 15 s, no handler
    # line, no "captured, silent", nothing to say which of the branches ate it
    # or what the client was asking for.
    log("authserv", f"game envelope tag={tag!r} class={cls!r} target={target!r} "
                    f"payload={len(payload)}B {bytes(payload[:64])!r}")
    # THE TITLE'S OWN FORMAT FIRST. Classes P/R/A/L are NOT a title's text
    # format -- they are the shared POLpro plaintext channel (profile, ranking,
    # auction, lobby), the same one the PS2 title uses -- so a title
    # hands those back with PASS and the polpro block below answers them. The
    # class list has to be kept in step in THREE places: the title's PASS
    # guard, this guard and the tuple below it. Measured 2026-08-15/16/17, one
    # class at a time: a class swallowed here shows on the wire as the
    # giveaway `<- no-header cmd=<SI>...` (Cards Bid On hanging on a loading
    # screen), `cmd=<DR>` (the room-entry spec keys never firing).
    _tr = titles.notice(cls, tag, payload, text, target, nick, srv, sess)
    if _tr is not titles.PASS:
        return _tr
    if not (titles.for_tag(tag) is not None
            and cls in (b"P", b"R", b"A", b"L")):
        # Anything else: log it and stay silent. Inventing a reply for an
        # undecoded format is how POL-5135 got raised elsewhere.
        log("authserv", f"  no decoder for service {tag!r} yet -- captured, "
                        f"not answered: {payload[:120]!r}")
        return None
    if cls in (b"P", b"R", b"A", b"L") and polpro is not None:
        # THE PLAINTEXT CHANNEL -- profile (P), ranking (R), auction (A) and the
        # LOBBY/table class (L). The grammar is
        # measured (polpro.py); the REPLY TAG SET IS NOT, so the answer comes from
        # a template file that is bind-mounted and re-read per request. Iterating
        # on an inferred format therefore costs an edit, with no rebuild and no
        # restart -- which matters because this traffic lands on the auth band, and
        # restarting that container is what kicks the player out.
        if os.environ.get("POL_POLPRO", "1") != "1":
            log("authserv", f"  polpro {cls!r} disabled -- {polpro.describe(payload)}")
            return None
        try:
            log("authserv", f"  polpro {cls.decode()} <- {polpro.describe(payload)}")
            # PASS THE SERVICE TAG. This channel carries both games and a request
            # shape does not tell them apart -- two titles send byte-identical
            # `<RR>`+`<PI>` rankings requests that need different replies. See
            # `polpro._spec_keys`; an untagged spec key still matches everything.
            # Class L is DECODE-ONLY until its reply vocabulary is measured --
            # see the `allow_default` banner in polpro.reply_for. An explicit
            # spec key still answers it; only the `*` wildcard is withheld.
            # THE TITLE ANSWERS ITS OWN VALUE-SHAPED REQUESTS FIRST. A template
            # file is keyed on a request's SHAPE, and three kinds of request
            # need an answer that depends on a VALUE: the class-L `<DR>` delta
            # stream (what the client already holds), the ranking `<RR>` (which
            # of several lists), the auction `<SN>` (this member's count). The
            # title registered for the tag answers those; declining falls
            # through to `reply_for` and the template exactly as before.
            reply, handled = titles.polpro_reply(cls, tag, payload)
            # AND THE PER-CONTENT-ID GAME CHARACTER, in the same shape as the
            # same shape: `<PG>` names ONE character by Content ID and a spec key
            # is a tag, so the template can only say one profile to everybody.
            # NOT gated on the service tag -- every title runs the same
            # parser over the same 71-value `<PO>` group (polpro.PROFILE_PO), so
            # one answer is correct for both. Declining falls through to the
            # polpro.json entry untouched.
            if not handled and cls == b"P":
                reply, handled = pfc._pfc_profile_reply(payload, tag)
            if not handled:
                reply = polpro.reply_for(payload, tag=tag,
                                         allow_default=(cls != b"L"),
                                         serial=titles.roster_sequence())
        except Exception as e:
            log("authserv", f"  polpro raised on {payload[:100]!r}: {e} -- silent")
            return None
        # THE ROSTER. `<DE>` (room entry) and `<PD>` (an option change) both
        # carry this member's own 88-byte record, and `cp__002fb290` applies
        # them through the SAME arm (0x20 and 0x13). SE's server keeps them and
        # serves the room's list back as `b/g/PTL`; we used to drop them on the
        # floor and serve a hand-authored file, which is why two players in one
        # room could not see each other.
        #
        # Recorded BEFORE the template lookup on purpose: whether we have a
        # reply to send is a separate question from whether the client just told
        # us something true, and `<PD>` has no reply (it is a delta command).
        # The title keeps the record, the character pool (what makes the
        # rankings say "Your Rank" instead of "Did not rank") and the room's
        # own peer guid, which this is the only band that carries.
        titles.polpro_noted(cls, tag, payload, target)
        # AND THE PROFILE WRITE, for both games. `<GR>` is the client handing us
        # its game character; it is the only source of that data and it is now
        # able to complete, so keep it. Not tag-gated -- every title writes
        # the same command on the same band.
        if cls == b"P":
            pfc._content_profile_note(payload, tag)
        # RECORDS THE PLAYER IS WAITING FOR ride this reply: a seat that is not
        # talking only ever polls, so the title hands over what it has queued
        # for this member, framed for this connection.
        _pushes = titles.polpro_pushes(tag, cls, payload)
        if not reply:
            if _pushes:
                # The client is CURRENT on deltas (the correct silence) but has
                # game records waiting. Silence is the right answer to the
                # poll, not to the player.
                return _pushes
            # WARNING: TWO SILENCES, TWO MEANINGS -- and printing the same line for
            # both cost 20 minutes of misdiagnosis on 2026-08-20T22:40: the
            # delta path's "CURRENT -- silent" arm returns handled=True with no
            # reply, which is the CORRECT answer, and this line then reported it
            # as a missing template on every poll. The real fault that night
            # (a torn-import process) had to be found against that noise.
            if handled:
                return None             # a handler CHOSE silence: say nothing
            log("authserv", "  polpro: no template for this command -- silent. "
                            f"Add it to {polpro.SPEC_FILE}")
            return None
        # WE REPLY TOO FAST, AND THE CLIENT LOSES THE ANSWER. Measured
        # 2026-08-15 against Tetra Master, and it is a genuine race in the
        # client, not a guess:
        #
        #   TMaster.pex 0x00417238  blez s0 -> skip     ; if the SEND failed...
        #                0x00417240  jal lock
        #                0x00417248  v0 = 1
        #                0x00417254  sw v0, 0xb0(s3)    ; ...mark pending AFTER
        #
        # The operation is marked pending only AFTER the request is transmitted,
        # and the result handler (pfcCharaPoolResult 0x004175b8) opens with
        # `if state != 1 -> error -8703` and silently drops the message. We answer
        # in ~2 ms, so our reply lands while the state is still 0: it is delivered
        # and consumed -- both confirmed, our `<CI>` sits in the client's own
        # message ring at 0x0045a630 next to the two replies that DID work -- and
        # then thrown away. The state is set to 1 immediately afterwards and stays
        # there until the game times out.
        #
        # A short pause is all it needs. The console is in no hurry: ordinary
        # conversations have been measured taking 30+ s
        # and the game's own timeout here is far longer than this delay.
        #
        # VERIFIED: CONFIRMED FOR `<PG>` TOO (2026-08-23, static, both builds). This was
        # written as "worth trying for the PS2 title"; it is now read rather than
        # hoped. `sqMgPfcGetCharacterProfile` (TM.dll sqmg_1a64d0) sends at
        # `sqmg_19e920` and only THEN takes the pfc lock and sets its state to 1,
        # and the result arm `sqmg_1a66d0` opens with `if (state == 1)`. Same
        # race, same cure -- so the pause is load-bearing for the profile popup
        # as well as the character pool.
        # POL_POLPRO_DELAY_MS=0 disables it and restores the old instant reply.
        delay_ms = int(os.environ.get("POL_POLPRO_DELAY_MS", "1500") or 0)
        # WARNING: NOT ON `<DR>`. The delay above exists for a client that marks an
        # operation pending AFTER it sends; `cp__002fb218` does no such thing --
        # it fires `<DR>` on a 2 s timer and forgets it. Sleeping 1.5 s inside a
        # 2 s poll would put us permanently one beat behind and, now that `<DR>`
        # carries the deltas, would hold the room list back by that much on every
        # change. Every other command keeps the measured pause.
        if payload[1:5] == b"<DR>":
            delay_ms = 0
        if delay_ms > 0:
            log("authserv", f"  polpro: pausing {delay_ms}ms before replying "
                            f"(the client marks the op pending AFTER it sends)")
            time.sleep(delay_ms / 1000.0)
        log("authserv", f"  polpro {cls.decode()} -> {polpro.describe(reply)}")
        body = b"G" + tag + cls + reply
        lines = [authnode.NoPad(_game_notice_line(body, target, nick, srv))] + _pushes
        try:
            chase = polpro.chaser_for(payload, tag=tag)
        except Exception as e:
            log("authserv", f"  polpro chaser raised: {e} -- first line only")
            chase = None
        if chase:
            log("authserv", f"  polpro {cls.decode()} -> (chaser) "
                            f"{polpro.describe(chase)}")
            lines.append(authnode.NoPad(_game_notice_line(b"G" + tag + cls + chase,
                                                 target, nick, srv)))
        return lines
    # A class the title did not take and the POLpro block did not answer:
    # nothing to say (the title's own classes returned above).
    return None


def _game_notice_line(body, target, nick, srv):
    """Wrap a game payload in the NOTICE the client reads it out of.

    Back the way it came: from the peer the game addressed (that nick is what its
    own sqMg member table has bound to the id it is talking to), to us. INFERENCE
    -- the prefix is the part with no direct evidence, so POL_GAME_NOTICE_PREFIX
    switches it in one restart if the client ignores us:
        peer (default) -- ":<target>!~x@ NOTICE <nick> :..."  (empty host,
        srv            -- ":<srv> NOTICE <nick> :..."
        bare           -- "NOTICE <nick> :..."   (no prefix at all)
    """
    mode = os.environ.get("POL_GAME_NOTICE_PREFIX", "peer")
    if mode == "srv":
        return b":" + srv + b" NOTICE " + nick + b" :" + body
    if mode == "bare":
        return b"NOTICE " + nick + b" :" + body
    return b":" + target + b"!~x@" + ircband._irc_host(srv) + b" NOTICE " + nick + b" :" + body


def _session_in_room(sess):
    """Is this session's client actually sitting on a room screen right now?

    POSITIVE EVIDENCE ONLY, and the first cut of this got it wrong in a way worth
    keeping written down: it gave a session that had never been seen on the room
    band the benefit of the doubt, reasoning that "never measured" is not "walked
    away". True in the abstract, and useless here -- immediately after a restart
    NOBODY has been measured yet, which is exactly when ghosts are adopted. The
    exception would have swallowed the rule and the reported case with it.

    So a client that has the signal must show it. A survivor really sitting on
    the room screen polls every 1-3 s and is adopted on the next one; the cost of
    being strict is that window, during which they remain a ghost -- still listed
    in `who`, still keeping the room alive. The cost of being lenient is a player
    on another screen counted as present indefinitely, which is what was
    reported.

    An object that is not a `ChatSession` at all (a relay stub, a test double)
    keeps the benefit of the doubt, because the signal does not exist for it and
    absence of a field is not absence of a player.
    """
    fn = getattr(sess, "in_room_recently", None)
    if fn is None:
        return True
    try:
        return bool(fn())
    except Exception:
        return True
