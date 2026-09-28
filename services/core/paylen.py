"""Lobby reply lengths per opcode and the constant-length fetch path tables."""
import os
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log, save_capture
from . import (contentprofiles, fetchpath, friendput, handlelists, lobbymail, lobbyops, lobbyrooms,
               lobbysearch, lobbysession, resourcestore, titlezone)


# THE REPLY READER, disassembled (polcore sub_037df690, the state-4 handler).
# It accumulates 0x18 bytes, OFB-decrypts them (0x3823ef0), and then:
#   reply[1] (TYPE) must be 0. Otherwise it computes 0xFFFFEBB0 - type, i.e.
#     -(5200 + type)  =>  every POL-52xx we have seen is this byte:
#     POL-5207 = type 7, POL-5212 = type 12. Our error codes are now readable.
#   reply[8:12], little-endian u32, is the WORLD HOST: if non-zero it stores
#     [0x3bc4ab8]=1, [0x3bc4aba]=0xC814 (=51220, HARDCODED -- so the world port
#     never came from the gate record, contrary to my earlier reading) and
#     [0x3bc4abc]=the address.
#   reply[4:8] is stashed at [esi+0x14] as the payload length, and a generic
#     reader (sub_037df800) then pulls exactly that many bytes.
# Nothing in that function inspects reply[2], so we send the value anchored by the
# 18 probe samples (0) rather than the one guess I had briefly put here.
#
# request opcode [1][2] -> reply payload length. The probe is anchored by 18 SE
# samples; the u/account fetch has exactly one SE sample (12). Its caller allocated
# a 664-byte result buffer (sub_037e0120 passes 0x298 to the request dispatcher,
# which stores it at slot+0xc0 with the buffer at +0xc4), so 12 may well be an
# "empty/short" answer rather than the general case -- override per opcode with
# POL_LOBBY_PAYLEN="4:7=8,3:0=664" while we find out.
_LOBBY_PAYLEN = {
    (0x04, 0x07): 8,      # 18 SE samples; client closes immediately after. Long
                          # labelled a "probe", but its REQUEST is the one place
                          # the client names the handle it is logged in as:
                          # payload +0x20 is a flag byte then a NUL-terminated
                          # handle name. `_capture_active_handle` reads it, and
                          # that is what scopes 05:01 profile writes to the right
                          # handle -- the write itself carries no z_hid.
    (0x04, 0x06): 128,    # **KGetMyStatus** -- MY online status, NOT "friends
                          # data" (the old label, which is what put friend entries
                          # here and broke the lobby clock). State 5 of the
                          # 0x37dd7b0 machine reads a HARDCODED 0x80. CONFIRMED LIVE.
    (0x03, 0x00): 668,    # KGetDetailData. sub_037e0120 allocates 0x298 = 664 for
                          # the DATA; the declared payload carries the 4-byte
                          # checksum on top, so 668. MEASURED 2026-08-15 -- SE's
                          # `u/account` fetch declares 0x29C. This is only the
                          # fallback for a path with no entry in _FETCH_PATHLEN;
                          # u/account itself is keyed there.
    (0x04, 0x05): 32,     # KChangeMyStatus / KPutBusy -- both K classes reach this
                          # one builder. State 5 of the 0x37ddef0 machine reads
                          # 0x20. Like (4,6) it special-cases the -5314 "no data".
    # ---- PS2-ONLY, resolved STATICALLY off the decrypted PS2 core 2026-09-18
    # (work/ps2/polpex/polpex.c, base 0x00101000). The PC's 28 reqEncode sites
    # never emit 4:0 or 4:1 (memory pol-lobby-handoff), which is why the PC
    # captures could not size them and dev carried a 128/128 SWEEP that never
    # shipped. Prod therefore served the 8-byte default, and a console with no
    # stored handle (fresh drive) sends 4:1 as its FIRST lobby request, got
    # 32 B on the wire, and ended the login with POL-0260 (real PS2, 2026-09-18
    # 22:51Z). The same account on a drive that already held its handle skips
    # 4:1 and goes 4:7 -> 4:6 -> 3:0 (127.0.0.1, 2026-09-07) -- so it only
    # ever bit a first login, which the PCSX2 rigs had all done long ago.
    (0x04, 0x01): 24,     # polpex_0014ae20: state 3 = header reader
                          # polpex_00143ac8, state 4 = polpex_00143cb0(ctx, 0x18,
                          # verify=1, buf) -- reads EXACTLY 24 payload bytes,
                          # checksum-verified (so the signer must stay on, as for
                          # 7:1). State 5 parses: [0] handle slot (< 0x40), [1]
                          # present flag, [2] position (< 8), [3] == 1 flag, [4:6]
                          # u16, [6] count (0 -> "no current handle": slot -1,
                          # state 4), [7] byte, [8:12] and [12:16] u32. All-zero
                          # = no current handle, which is the truthful answer
                          # for a fresh console; the 2026-08-12 sweep value 128
                          # worked only because the console reads 24 and the
                          # per-request socket swallowed the surplus.
    (0x04, 0x00): 32,     # polpex_0014b508 machine: state 3 WRITES 0x18 bytes
                          # (polpex_00143e98), state 4 header, state 5 =
                          # polpex_00143cb0(ctx, 0x20, verify=1, buf) -- reads
                          # exactly 32 payload bytes. Static only; not yet seen
                          # from the real console.
    (0x04, 0x04): 128,    # SAME BUILDER AS 4:1 -- that is why it was never sized.
                          # Read out of the US 1.15.05 `pol.pex` (decrypted off
                          # the drive, module base 0x00101000). The machine at
                          # 0x00136a98 picks the opcode from a flag:
                          #     0x00136b4c  lb    v1, 192(s1)
                          #     0x00136b54  addiu a2, zero, 4      <- opcode 4
                          #     0x00136b5c  movz  a2, v0, v1       <- ...or 1
                          #     0x00136b60  addiu a1, zero, 4      <- class 4
                          #     0x00136b64  jal   0x0012f1e8       <- the encoder
                          # so a console sends 4:1 or 4:4 from one state. Nobody
                          # had seen the flag SET, so 4:4 fell to the 8-byte
                          # default and the console hung ~98.7 s then reported
                          # POL-0260 (PCSX2, US 1.15.05 Viewer, 2026-09-19; the
                          # server logged `unknown opcode 4:4` both times).
                          # That function contains exactly ONE payload read:
                          #     0x00136bd4  addiu a1, zero, 128
                          #     0x00136be4  jal   0x0012f530   (a2 = 1, verified)
                          # 128 is also the class-4 status size -- 4:6
                          # KGetMyStatus reads a hardcoded 0x80.
                          # WARNING: UNRESOLVED: 4:1 above says 24, derived from the JP
                          # core where that machine had a separate header reader
                          # plus a 24-byte payload read. This US build's shared
                          # machine has only the 128. Either the builds differ or
                          # the JP function is a different analogue. If 4:4 at 128
                          # does not clear the stall, that tension is the thread
                          # to pull -- do NOT sweep sizes blindly, and do NOT use
                          # POL_LOBBY_PAYLEN (see the 2026-08-26 note in
                          # docker-compose.prod.yml: the env is consulted BEFORE
                          # this table, and a stale override silently won for
                          # weeks).
    (0x00, 0x08): 520,    # handle registration (payload 0x648 out). State 6 of the
                          # 0x37dd1f4 machine reads 0x208.
    # The four "keep-alive" opcodes are NOT keep-alives -- they are LIST fetches,
    # all with the same shape: an 8-byte count block (read with verify=0, so it is
    # only accumulated into the running checksum, never compared), then
    # `count` fixed-size records, then nothing -- no trailer, unlike (3,3).
    # So at count 0 the payload really is 8 bytes and our default was already
    # right; none of these four can be the session-death cause. Resolved
    # statically from polcore, 2026-08-11.
    # Listed here so nobody sweeps sizes for them again.
    (0x00, 0x09): 8,      # 8 + count*136. count = BYTE 0 of the block. Machine
                          # @0x37de408; consumer fills a 64-entry table indexed by
                          # record+0x00 (<0x40) with a 15B code at +0x10 and a 50B
                          # name at +0x18 -- the content/service list, most likely.
    (0x01, 0x03): 8,      # KGetChrList / KPutChrList (character list, not groups).
                          # 8 + count*104. count = BYTE 0. Machine @0x37e27a4.
    (0x01, 0x0A): 0,      # PS2 ONLY: `sqprofdb: Update chlist`, the console's
                          # character-list WRITE-BACK (KPutChrList's twin; the PC
                          # rides 1:3 for it). HEADER-ONLY: the sender's state 5 is
                          # the 24-byte header reader polpex_00143ac8 and reads no
                          # payload -- the 8-byte default it fell to for a week was
                          # tolerated, not asked for. Static 2026-09-11 off the
                          # decrypted PS2 core; body -> _chr_put. Memory
                          # ps2-lobby-1-10-is-sqprofdb-update-chlist.
    (0x02, 0x03): 8,      # **KGetFriendList**. 8 + count*168. count = U32.
                          # Machine @0x37e3bbc. Its WRITE half is 2:6.
    (0x07, 0x0C): 8,      # **KGetGroupList** (was "unidentified").
                          # 8 + count*136. count = BYTE 0 and it is VALIDATED:
                          # byte 0 must be <= 4 and bytes 1..4 each <= 0x40, or the
                          # client fails with -5133. Machine @0x37e869c.
    (0x05, 0x03): 16,     # STATUS / zone search. Machine @0x37e12bc. State 4 does
                          # `cmp dword [esi+0x14],0x10 / je` on the payload length
                          # WE declare and jumps to `mov edi,0FFFFEAB0h` = -5456
                          # otherwise -- the sole -5456 site in polcore (0x37e11e5).
                          # So this one is an exact-16 check, not a free length: the
                          # state-5 reader takes its length from the same field,
                          # which is what misled me into calling it self-describing.
    (0x07, 0x01): 16,     # CREATE GROUP / chat room. Machine @0x37e8ec0; the one
                          # post-header read is (ctx, len=0x10, verify=1, buf), so
                          # 16 bytes AND -- unlike the list opcodes -- it IS
                          # checksum-verified, so the signer must stay on. Answered
                          # with the default 8 the client blocks forever: the live
                          # capture at 04:54:53 is "TIMED OUT -- client is still
                          # WAITING on us", which is what a short read looks like.
    # ---- THE WRITE OPCODES ANSWER HEADER-ONLY. MEASURED 2026-08-15, 32 samples.
    # Every opcode below was decoded out of the two-account SE session (the 51220
    # frame cache of polshim-se.429364.log) and SE declared payload length **0**
    # in every single sample -- and, unlike the list opcodes, the client read
    # nothing either (`short` = 0), so 0 is both what SE declares and what the
    # reader wants.
    #
    #   3:1 x12   3:2 x12   4:3 x1   5:1 x2   7:3 x4   7:0b x1
    #
    # THIS MATTERS MORE THAN THE FOUR LISTS' 8-byte over-declare, which is
    # deliberate and documented ("the list opcodes over-declare by 8"):
    # there SE declares the surplus and never sends it. Here we BUILD the payload
    # (`bytearray(24 + n)`) and put it on the wire, onto a socket the client
    # reuses -- the SE capture shows several request/reply pairs per socket, each
    # opened by its own plaintext hello -- so every byte the reader skips is left
    # in front of the NEXT reply's header.
    #
    # Each one is revertible without a rebuild: POL_LOBBY_PAYLEN="4:3=520,3:1=8"
    # is consulted before this table.
    (0x03, 0x01): 0,      # MESSAGE SEND. Seen live 2026-08-11 the moment the user
                          # messaged a friend (471B request = 40 header + 431).
                          # Its machine (table 0x37e6c30, 12 states, sender
                          # 0x37e68e7) reads the 24-byte reply HEADER at 0x37e6b49
                          # and NOTHING else -- a full sweep of the machine body
                          # finds no payload read at all.
                          #
                          # That static reading was overridden here on the
                          # strength of a 2026-08-11 capture said to show an
                          # 8-byte payload. The 2026-08-15 session shows **0** in
                          # all twelve sends, which is what the disassembly said
                          # in the first place, so the override is retracted and
                          # the two now agree. The `3:x` reply table has said
                          # "reply 0" since that capture; this line was the last
                          # place still claiming 8.
    (0x03, 0x02): 0,      # OBJECT WRITE-BACK (mark-read / delete on `O/m/`
                          # paths). Was missing from this table
                          # entirely and fell through to the default 8.
    (0x03, 0x04): 0,      # MULTI-TARGET WRITE: one message to up to 20
                          # recipients. Never seen on our wire; Project Crystal
                          # Server answers it header-only against the Viewer,
                          # like the other writes. See resourcestore.
                          # _capture_multi_write.
    (0x07, 0x02): 0,      # KDeleteGroup (request len 0x10). ZERO wire
                          # samples -- no capture has ever carried one. Header-only
                          # like the other group WRITES (7:3, 7:11); a body here
                          # would sit in front of the next reply's header if the
                          # client reads none (the 5:1 lesson). See _group_delete.
    (0x07, 0x03): 0,      # KChgGrpMemClass. Also absent, also defaulted to 8.
    (0x07, 0x0B): 0,      # KChgMyGrpStatus / KChgMyAllGrpOnlineStatus. Ditto.
    (0x02, 0x06): 184,    # **KPutFriendList** -- the friend-list WRITE, not the
                          # "member search by mail / by handle" this line used to
                          # claim. Named from app.dll's RTTI + the polcore common
                          # function table; see the 02:06 note further down.
                          #
                          # **184, MEASURED 2026-08-16, and 168 was the guess that
                          # hung the friend list.** SE answered all FOUR of the
                          # 2:6 writes in polshim-se.429364.log with exactly 184
                          # bytes (`lobbydec.py`; requests 480 B, same as ours).
                          # Declaring 168 leaves the reader blocked waiting for
                          # the other 16, which is precisely the "Updating friend
                          # list" that never finishes and re-sends the same write
                          # every ~36 s. The old comment claimed 168 "is what SE
                          # sends"; nothing had ever measured it.
    (0x04, 0x03): 0,      # KPutMyCommentForFriend. Read statically as a 520-byte
                          # payload at verify=1; SE's live answer is header-only.
                          # This was the largest of the six -- 520 zero bytes
                          # pushed after every comment write.
    (0x07, 0x01): 16,     # GROUP create (new 2026-08-11, appeared with the group
                          # the user made). Resolved statically by lobbyops.py:
                          # out=32, one 16-byte payload read (machine 0x37e8ec0,
                          # sender 0x37e8d8b).
    (0x05, 0x01): 0,      # profile write. Found live (client says "profile
                          # updated"); machine @0x37e1e18, sender 0x37e1d5c. The
                          # 24 was never measured; SE answers both of the
                          # session's profile writes header-only.
    (0x05, 0x04): 604,    # PROFILE/PORTRAIT read-back. NOT from a guess: the SE
                          # capture table `_LOBBY_SHAPES` above has 76 -> reply
                          # TOTAL 628, and every reply is a 24-byte header plus
                          # payload, so SE's payload here is 628-24 = 604. This
                          # opcode was MISSING from this table entirely, so it
                          # fell through to _LOBBY_PAYLEN_DEFAULT and we answered
                          # 8 bytes where SE answers 604 -- a 596-byte shortfall,
                          # which is what a client stuck on a short read looks
                          # like. The other three rows of _LOBBY_SHAPES all agree
                          # with their entries here (104->8, 456->12, 132->16),
                          # which is what makes the fourth row trustworthy.
                          # The record's LAYOUT is still unknown -- this only
                          # fixes the length, so the client gets a correctly
                          # sized (zero) record instead of a truncated one.
    (0x03, 0x03): 12,     # mail list ("O/m/"). The 11-state machine @0x37e7b34
                          # reads an 8-byte count block (state 6), then `count`
                          # 264-byte records, then -- even when count is 0, via the
                          # short-circuit at 0x37e7a36 -> state 9 -- a **4-byte
                          # trailer**. 8+4 = 12, which is exactly the payload of
                          # SE's captured 456->36 reply: that sample was the MAIL
                          # request, not u/account as I first assumed.
}
_LOBBY_PAYLEN_DEFAULT = 8

#: Opcodes already named in the "not in _LOBBY_PAYLEN" log below, so a client
#: that asks 135 times logs once. Process-local and deliberately unbounded: the
#: opcode space is two bytes and the table covers nearly all of it, so this set
#: cannot grow past a handful without that itself being the finding.
_LOBBY_PAYLEN_UNKNOWN_SEEN = set()


#: THE 03:00 FETCH IS KEYED BY A PATH STRING, not by opcode -- found 2026-08-12
#: when a search that actually MATCHED made the client follow up with
#:
#:   0x30  54 dc 2c e9 62 01 00 00        session id (request payload +0x08)
#:   0x38  "u/s/select0"                  request payload +0x10, the KEY
#:
#: against the `u/account` we had always answered. `u/s/select%d` is a polcore
#: format string (0x383544c) filled with the result-set slot from the 05:03 reply,
#: so the two opcodes are one flow: 5:3 announces the hit count, 3:0 collects the
#: rows. Answering the account record to both is what produced POL-0008 on any
#: search that found somebody.
#:
#: The requester (0x37e1554) also writes the WINDOW it wants, in bytes:
#:   payload +0x190  record_size * first_index      (where to start)
#:   payload +0x194  record_size * (end - first)    (how much to send)
#: and the reader at 0x37e164a checks `declared_len - 4 <= record_size * count`,
#: failing with -5123 otherwise, then derives the RECORD COUNT by dividing the
#: bytes it got by the record size. So the reply length is dictated by the
#: request, the record size is the client's own constant, and serving a fixed 664
#: is guaranteed to overflow it.
_FETCH_PATH_OFF = 0x38                  # payload +0x10
_FETCH_WINDOW_OFF = 0x28 + 0x190        # payload +0x190: first record, in bytes
_FETCH_WINDOW_LEN = 0x28 + 0x194        # payload +0x194: bytes wanted
_SELECT_PATH = "u/s/select"

#: Hits announced by the last 05:03. The client asks 03:00 for exactly this many
#: records, so it doubles as the divisor that reveals the record size.
_LAST_SEARCH_COUNT = [0]


#: 03:00 REPLY LENGTH IS PER PATH, and it comes from the CLIENT'S CODE.
#:
#: RETRACTED, 2026-08-12: I first read payload +0x0C as "the size the request asks
#: for", because a live game-save fetch had 0xC8 = 200 there. It is not a
#: length -- the very next capture showed `u/account` carrying the same 0xC8, and
#: serving 200 bytes to u/account broke the account record that had been working.
#: One sample that fit a theory, and the theory was wrong.
#:
#: The lengths are not on the wire at all; each caller passes its own constant to
#: the resource reader, so they are readable statically and exactly:
#:
#:   u/account         664   polcore sub_037e0120 allocates 0x298
#:   a title's save    976   the game module's read call:
#:                             a0 = the path string
#:                             a1 = the destination buffer
#:                             a2 = 976        <- the length
#:
#: So add a path here when a new resource appears, with the site it came from.
#: A path we have never seen falls back to the request's declared frame length,
#: which is the least-wrong default and is logged loudly.
#: NOTE THESE ARE DECLARED PAYLOAD LENGTHS, AND THE LAST 4 BYTES OF A PAYLOAD ARE
#: THE CHECKSUM (see _build_lobby_reply_pt). So a resource whose reader wants D
#: bytes of DATA needs D + 4 declared, exactly as the mail reply does (8-byte count
#: block + 4-byte trailer = 12 declared, matching SE's own capture).
#:
#: Getting that wrong is NOT a checksum failure, it is a HANG: declaring 976 for a
#: 976-byte reader delivers 972 bytes of data, the client waits for the remaining 4
#: forever, and `handle_lobby` logs "TIMED OUT -- client is still WAITING on us".
#: That is precisely what kept one title's save data blank -- the read never
#: completed, so the magic we had just fixed never reached the buffer at all.

#: A title's variable-length resources (ranking lists, auction lists: the
#: reader's length follows a count the OTHER band promised) get NO entry here;
#: the title declares each fetch's length through `titles.resource_length`, and
#: its constant-length paths are merged into this table at load.
_FETCH_PATHLEN = {
    # 668, AND THE CAPTURE THIS LINE ASKED FOR NOW EXISTS. The old value stood at
    # 664 with "do not correct it to 668 without a capture saying so" -- polcore
    # allocates 0x298 = 664, and the open question was whether the 4-byte checksum
    # sat inside that or on top of it. SE's live `u/account` fetch (2026-08-15,
    # the two-account session) declares **0x29C = 668**, so the checksum is on
    # top and the record body really is the full 664 the allocation implies.
    #
    # At 664 we were serving a 660-byte record: not a hang (the client got the
    # length it was told) but four bytes of the account record short, every time.
    "u/account": 668,
    "b/g/ZL": 2120 + 4,
    # b/g/PTL -- the table/participant list, and the THIRD member of that same
    # chain. Measured 2026-08-16 from the PS2 title's module instead: its
    # `cp__002fc608` calls mg__002f5c58(path, buf, 0xc050, ...) -- 49232
    # bytes -- and the path is sprintf'd at 0x002f61c8. The same two-module
    # agreement that gave us ZL and RL: those two were read off TMaster.pex
    # and the PS2 module asks for byte-identical lengths, so this one should
    # hold for Tetra Master too.
    #
    # WHY THIS ENTRY ALONE IS WORTH HAVING:
    # sqMgCpEnterRoom BLOCKS on this fetch (states 10/11) before it will join the
    # room channel, and unlike a game save the poll (`cp__002fc6b8`) has NO
    # format gate -- it is a bare sqMgReadFileCheck. So the all-zero fallback at
    # this length is a well-formed EMPTY list (header count `pn` at +0x44 and
    # `tn` at +0x48 both read 0), which should carry EnterRoom through to the
    # channel join. A title declares the content-bearing length through
    # `titles.Title.resource_length`; this is the reader's buffer, the ceiling.
    "b/g/PTL": 49232 + 4,
}

#: Paths whose name is FORMATTED per request, so they cannot be dict keys. The
#: room list is `b/g/RL%03d` (built at TMaster.pex 0x0040a138, the zone number as
#: its argument), fetched with a2 = 0xc848 at 0x004108b0 -- confirmed twice, the
#: same constant is re-stored as the recorded length at 0x004108f0.
#: Longest prefix wins, so a future `b/g/RLxxx` variant can be added beside it.
_FETCH_PATHLEN_PREFIX = {
    "b/g/RL": 51272 + 4,
}


#: WHERE A CLIENT NAMES THE PARTY A FETCH IS ABOUT: the 8 bytes immediately in
#: front of the path, i.e. payload +0x08. Measured 2026-08-16 over three accounts
#: in `lobby.log`, and it is a SUBJECT field, not a session id:
#:
#:     3:0 `u/account`   0x860fb3e2a2        <- what this client calls ITSELF
#:     3:0 `O/m/...`     0x080000000016      <- the message record's own +0x08
#:     3:1 `O/m/...`     0x080000000016      <- the RECIPIENT, from the address book
#:
#: so only the `u/account` flavour says anything about the sender, and that is
#: the only one `_capture_self_guid` reads. The value it carries there is the
#: same one that client puts in a message's sender field, which is what makes it
#: a second, earlier source for `accounts.client_guid` -- a client volunteers it
#: on its first account fetch, long before it ever sends a message.
_FETCH_SUBJECT_OFF = 0x30
_SELF_GUID_PATH = "u/account"


def _select_paylen(pt):
    """Reply length for a u/s/select fetch, or None if this is not one.

    THE CLIENT SLICES WHAT WE SEND INTO ROWS ITSELF: reader 0x37e164a divides the
    bytes it got by its own record size and renders that many rows -- which is why
    a full 60000-byte window renders HUNDREDS of blank rows next to a header that
    correctly says "1". The window the client asks for is `record * page`, not
    `record * hits`, so it is safe (it can never overflow the -5123 bound) but far
    too long.

    Four knobs, in order of precedence:
      the CALIB FILE     a byte count in `POL_SEARCH_CALIB` (default
                         `$POL_LOG_DIR/search-calib.txt`), re-read on EVERY fetch.
                         See the calibration note below -- this is the only knob
                         that can change mid-session, which is the whole point.
      POL_SEARCH_RECORD  bytes per row. Once known, we serve hits*record and the
                         list finally matches the count.
      POL_SEARCH_BYTES   a fixed byte count, pinned for the whole run.
      neither            the full window, i.e. today's blank-row flood.
    The floor is one whole record -- fewer bytes than that divides to zero rows
    and the reader answers -5123 -- so nothing here ever goes below `record`.

    CALIBRATING THE RECORD SIZE (2026-08-12). The client renders
    `served // record` rows, so one (served, rows) pair only brackets the answer
    to `(served/(rows+1), served/rows]` -- a window of about `record/rows` bytes.
    Pinning a ~664-byte record to the nearest 8 that way needs ~90 rows counted
    off a screen, which is not a measurement anyone can make reliably.

    So do not count rows. Walk the **1-row/2-row boundary** instead: `served //
    record` steps from 1 to 2 at exactly `served == 2 * record`, and telling one
    row from two takes no counting at all. Bisecting that boundary reaches the
    nearest 8 bytes in about eight searches, at any record size.

    That is why this knob is a FILE and not an env var: changing an env var means
    recreating the container, and **a rebuild drops the client's live connection**
    (POL-0008), which would end the very session being measured. The file is read
    per fetch, so `tools/search_calib.py` can drive the whole bisection while the
    user stays logged in. Empty or unparseable file = knob absent, no effect.
    """
    win = fetchpath._select_window(pt)
    if win is None:
        return None
    first, want = win
    rows, kind = lobbysearch._search_result_get()
    announced = len(rows)
    rec = int(os.environ.get("POL_SEARCH_RECORD", "0"), 0)
    # A ROOM result set is 160-byte records, not the 600-byte profile a member
    # search serves. Get this wrong and the reader divides the window by the wrong
    # size and answers POL-0008 -- which is exactly what "update the chat room
    # list" did the first time rooms were served. Both the kind and the rows come
    # from THIS session (see `_search_result_get`): they used to be process-wide,
    # so a second client's search could resize this one's reply.
    if kind == "room":
        rec = lobbyrooms._ROOM_RECORD
    elif kind == "zone":
        rec = lobbyrooms._ZONE_SUMMARY_RECORD
    fixed = int(os.environ.get("POL_SEARCH_BYTES", "0"), 0)
    calib = lobbysearch._search_calib_bytes()
    if calib:
        served = min(calib, want)
        log("lobby", f"  select CALIB: serving {served}B "
                     f"(from {lobbysearch._search_calib_path()}) -- if the list shows ONE row "
                     f"the record is > {served // 2}B, if TWO it is <= {served // 2}B")
    elif rec > 0:
        rows = max(1, min(announced or 1, want // rec))
        served = rows * rec
    elif fixed > 0:
        served = min(fixed, want)
    else:
        served = want
    log("lobby", f"  select fetch: window +{first} {want}B for {announced} "
                 f"announced hit(s); serving {served}B"
                 + (f" = {served // rec} row(s) of {rec}B" if rec > 0 else
                    " (record size unknown -- set POL_SEARCH_RECORD)"))
    return served + 4


#: THE 02:06 REPLY IS A GRID TOO, and its unit is the request's own.
#:
#: SE's reply copies the request from **0x158** onward at a 1:1 offset --
#: `reply[+0x08 + k] == request[0x158 + k]` -- which makes the reply's per-record
#: stride the same 168 the request grid uses, and the whole payload
#:
#:     8 (count block) + N*168 + 4 (the trailing state dword) + 4 (checksum)
#:     = N*168 + 16
#:
#: Checked against both shapes in the 2026-08-19 retail capture: N=1 gives 184
#: (0xB8, which is exactly what `_LOBBY_PAYLEN` has always held) and N=2 gives
#: 352 (0x160, SE's own two-record reply at capture line 259803). So the length
#: table's value was never wrong -- it was the N=1 case of this.
_FRIEND_PUT_REPLY_BASE = 0x08
_FRIEND_PUT_REPLY_FIXED = 16


def _friend_put_reply_len(req_pt):
    """The 02:06 reply payload length for this request, or None if unreadable."""
    if req_pt is None:
        return None
    _start, recs = friendput._friend_put_records(req_pt)
    if not recs:
        return None
    return len(recs) * friendput._FRIEND_PUT_REC + _FRIEND_PUT_REPLY_FIXED


#: Candidate 02:06 reply payload lengths, tried in this order by the auto-sweep.
#: 168 is the table's (unmeasured) value and goes first so the sweep starts from
#: today's behaviour; 176 = 8 + one 168-byte record, the list-shaped answer;
#: 8 = a bare count block; 480 matches the request's own payload; 16 = a small
#: status struct; 520 is what the table claimed for the sibling 4:3 (and 4:3 was
#: measured at 112, so 520 was wrong there -- included to rule it out here).
_FRIEND_PAYLEN_SWEEP = (168, 176, 8, 480, 16, 520)
_FRIEND_PAYLEN_AT = [0]


    # 02:06 REPLY LENGTH, live-tunable. **168 was never measured.** The length
    # table is anchored on 18 SE samples and SE's captures contain no friend
    # write at all, so this one entry is a guess that has been treated as fact.
    #
    # It matters more than the CONTENT: after a friend write the client sits on
    # "Updating friend list" and re-sends the same 2:6 every ~35-45 s. A HANG
    # rather than an error is what "waiting for more bytes" looks like -- if we
    # declare fewer bytes than the reader wants, it blocks. Content shapes
    # (`friend_reply`) were tried first and `ack` did not move it, which is what
    # pointed here.
    #
    # Sweep it with `friend_paylen=` in the control file; 0 means "no opinion".
def paylen_friend_put(req_pt):
    """2:6 KPutFriendList reply length (lobby opcode table).

    A MULTI-RECORD WRITE NEEDS A MULTI-RECORD REPLY, and the length is
    live-tunable; None lets the table's 184 stand.
    """
    # A MULTI-RECORD WRITE NEEDS A MULTI-RECORD REPLY. The table's 184 is
    # the N=1 case of `N*168 + 16` (see `_friend_put_reply_len`), and SE's
    # own two-record reply is 352 -- so answering 184 to an N=2 write, which
    # is what we did, declares one record and drops the other. Our client has
    # only ever been seen sending N=1, where this is byte-identical to the
    # table; SE's client sent N=2 for an ignore-add. POL_FRIEND_PUT_MULTI=0
    # pins the old fixed length.
    if req_pt is not None and os.environ.get("POL_FRIEND_PUT_MULTI", "1") == "1":
        want = _friend_put_reply_len(req_pt)
        if want and want != _LOBBY_PAYLEN.get((0x02, 0x06)):
            log("lobby", f"  2:6 reply length: {want} for "
                         f"{(want - _FRIEND_PUT_REPLY_FIXED)//friendput._FRIEND_PUT_REC}"
                         f" record(s) (the table's "
                         f"{_LOBBY_PAYLEN.get((0x02, 0x06))} is the 1-record "
                         f"case)")
            return want
    n = lobbysearch._search_calib()["friend_paylen"]
    if n == "sweep":
        # AUTO-SWEEP. Each successive 2:6 gets the next candidate length, so
        # the whole sweep needs no commands between attempts -- which matters
        # because a hung Viewer has to be restarted by hand, and making the
        # user drive five settings by hand costs five restarts.
        #
        # The client re-sends the same write every ~35-45 s while it is
        # unhappy, so a length that SATISFIES it shows up in the log as the
        # value after which the retries stop.
        n = _FRIEND_PAYLEN_SWEEP[_FRIEND_PAYLEN_AT[0] % len(_FRIEND_PAYLEN_SWEEP)]
        _FRIEND_PAYLEN_AT[0] += 1
        log("lobby", f"  2:6 reply length SWEEP step "
                     f"{_FRIEND_PAYLEN_AT[0]}/{len(_FRIEND_PAYLEN_SWEEP)}: "
                     f"n={n}  (if the client stops retrying after this one, "
                     f"{n} is the answer)")
        return n
    if n:
        log("lobby", f"  2:6 reply length OVERRIDE: {n} "
                     f"(table says {_LOBBY_PAYLEN.get((0x02, 0x06))})")
        return n
    return None


def paylen_fetch(req_pt):
    """3:0 KGetDetailData reply length (lobby opcode table): a search
    result's own length, a message at the length its reader declares, a
    title's variable-length resource, a per-path override, or the measured
    length in _FETCH_PATHLEN. None means no measured length: the generic
    rules answer with the opcode default and say so."""
    if req_pt is None:
        return None
    if fetchpath._fetch_path(req_pt).startswith(_SELECT_PATH):
        n = _select_paylen(req_pt)
        if n:
            return n
    # ANY OTHER 03:00 path: its own measured length -- see _FETCH_PATHLEN. The
    # length is a constant in whichever caller issues the fetch, NOT a field on the
    # wire (that reading was wrong and is retracted above).
    path = fetchpath._fetch_path(req_pt)
    if path.startswith(lobbysearch._MAIL_PATH_PREFIX):
        # A MESSAGE IS SERVED AT ITS OWN LENGTH, WHICH THE READER DECLARES.
        # SE answers a 30-byte object with a 34-byte payload -- object plus
        # trailer, never padded (`_MAIL_OBJ_OFF`). Padding it to the 664 this
        # opcode defaults to is what raised **POL-5135**: the reader checks
        # the trailer at the length IT asked for, finds our zero padding
        # there, and the bytes it never read desynchronise the socket, which
        # is the long stall before the error.
        want = lobbymail._mail_read_len(req_pt) or 0
        try:
            have = resourcestore._res_size(resourcestore._resource_read_file(path)) or 0
        except Exception:
            have = 0
        n = want or have
        if have and want and have != want:
            log("lobby", f"  3:0 {path[:32]!r}: reader wants {want}B, the store "
                         f"holds {have}B -- serving {n}B (+4 trailer). A "
                         "mismatch means the object was stored wrong.")
        if n:
            return n + 4
    # A TITLE'S VARIABLE-LENGTH RESOURCES declare their length here: the
    # lists whose reader asks for `count * record` where the count is what
    # the title promised on the auth band, so no constant can be right for
    # more than one length, and the one header whose length differs by
    # client build. See `titles.Title.resource_length`.
    # THE REQUESTER'S OWN TITLE IS ASKED FIRST: two titles serve the same
    # lobby-list paths at different lengths, and which one this member is
    # in is known from the title-zone lease.
    _tn = titles.resource_length(path, zone=titlezone._title_zone(lobbysession._session_get("member_id")))
    if _tn is not None:
        return _tn
    if path:
        # PER-PATH OVERRIDE, for bisecting a stalled fetch without touching
        # the paths that work. POL_LOBBY_PAYLEN is per-OPCODE, and 03:00 is
        # one opcode carrying many resources -- overriding it would break
        # `u/account` in the same breath, which is the path-mixing trap this
        # file has fallen into twice.
        #
        #   POL_RESOURCE_PAYLEN="U/g/<a save path>=668"
        #
        # WHY THIS EXISTS (2026-08-13): one title's save was the ONLY reply on
        # the whole channel that stalls deterministically -- 10/10 across every
        # log we have, at both lengths tried (1000 and 1004 total), while
        # replies of 1040, 1208, 1884, 2048 and 60028 bytes are all accepted.
        # So it is not size in general. Serving THIS path at a length already
        # proven on the same opcode (664 data, as `u/account` and
        # a title's save both use) separates "this length is unreadable" from
        # "this path is unreadable", which no other measurement can.
        for item in os.environ.get("POL_RESOURCE_PAYLEN", "").split(","):
            key, _, val = item.partition("=")
            if key.strip() == path:
                try:
                    n = int(val, 0)
                except ValueError:
                    break
                # WARNING: Do not re-add the old "transport probe; the game will
                # reject the short blob" wording here: it was inherited
                # from the 2026-08-13 save-path probe and is WRONG for
                # b/g/RL000, whose 1476B declare the client ACCEPTS
                # (confirmed live 2026-08-19T20:30Z).
                log("lobby", f"  3:0 {path!r}: PAYLEN OVERRIDE {n} "
                             f"(measured {_FETCH_PATHLEN.get(path)})")
                return n
        n = _FETCH_PATHLEN.get(path)
        if n:
            return n
        # Formatted paths (b/g/RL000, ...): longest matching prefix wins.
        hit = max((p for p in _FETCH_PATHLEN_PREFIX if path.startswith(p)),
                  key=len, default=None)
        if hit:
            return _FETCH_PATHLEN_PREFIX[hit]
        log("lobby", f"  3:0 path {path!r} has NO measured length -- falling "
                     f"back to the declared frame length. Read the caller's "
                     f"length argument and add it to _FETCH_PATHLEN.")
    return None


def paylen_override(op1, op2):
    """POL_LOBBY_PAYLEN="4:7=8,3:0=664": a per-opcode length pin, for finding
    a length by sweeping. None when the opcode is not named."""
    for item in os.environ.get("POL_LOBBY_PAYLEN", "").split(","):
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        a, _, b = key.strip().partition(":")
        try:
            if (int(a, 0), int(b, 0)) == (op1, op2):
                return int(val, 0)
        except ValueError:
            continue
    return None


def _lobby_paylen(op1, op2, req_pt=None):
    op = lobbyops.lookup(op1, op2)
    if op is not None and op.paylen is not None:
        n = op.paylen(req_pt)
        if n is not None:
            return n
    if handlelists._list_count(op1, op2, req_pt):
        # req_pt reaches the count so a mobile 2:3 declares the length of the
        # rows it will actually carry (a mismatch is POL-5135).
        return handlelists._list_paylen(op1, op2, handlelists._list_count(op1, op2, req_pt))
    forced = paylen_override(op1, op2)
    if forced is not None:
        return forced
    _known = _LOBBY_PAYLEN.get((op1, op2))
    if _known is not None:
        return _known
    # WARNING: AN UNNAMED OPCODE USED TO BE ANSWERED WITH ZEROS IN COMPLETE SILENCE.
    #
    # There was no "unknown lobby opcode" line anywhere: an opcode missing from
    # the table just took the 8-byte default and went out looking like a normal
    # reply. That is how `4:0` and `4:1` -- 176 wire samples between them, named
    # in no document in this repo -- stayed invisible until someone grepped
    # `opcode=` out of the decrypt log by hand in 2026-08-26's completeness
    # sweep. The whole band has an oracle for unanswered CHAT verbs ("no handler
    # for X") and had none for unanswered LOBBY opcodes.
    #
    # WARNING: The default is very likely WRONG for anything that lands here, and the
    # failure is silent on the client too: `4:1` fell to a 32-byte default and
    # the PS2 Viewer simply SAT THERE (-> POL-0010), which is why
    # docker-compose.yml carries an unfinished 128/520/664 sweep for it. Under-
    # declaring hangs the client; over-declaring leaves surplus bytes in front
    # of the next reply on a reused socket. Neither announces itself.
    # KEEP THE FRAME. The generic `lobby-<port>-req` captures rotate at 64 per
    # name on a busy channel, so the two `1:10` frames a PS2 Viewer sent on
    # 2026-09-04 (624 B, member 10, prod) were gone before anyone looked. An
    # opcode we cannot name is the one request whose bytes are worth keeping:
    # save the DECRYPTED frame under the opcode's own capture name (budgeted like
    # every other capture -- see srvcore.save_capture) so the next one survives.
    if req_pt is not None:
        ucap = save_capture(f"lobby-unknown-{op1:d}-{op2:d}", bytes(req_pt))
        if ucap:
            log("lobby", f"  unknown opcode {op1:d}:{op2:d}: {len(req_pt)}B "
                         f"decrypted frame saved {ucap}")
    if (op1, op2) not in _LOBBY_PAYLEN_UNKNOWN_SEEN:
        _LOBBY_PAYLEN_UNKNOWN_SEEN.add((op1, op2))
        log("lobby",
            f"WARNING: opcode {op1:d}:{op2:d} is NOT in _LOBBY_PAYLEN -- answering "
            f"the {_LOBBY_PAYLEN_DEFAULT}-byte default, which is a GUESS. If "
            f"the client stalls after this, the real length is larger; sweep "
            f"it with POL_LOBBY_PAYLEN=\"{op1:d}:{op2:d}=<n>\". Logged once "
            f"per opcode per process.")
    return _LOBBY_PAYLEN_DEFAULT
