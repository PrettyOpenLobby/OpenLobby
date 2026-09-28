"""The push channel: how live updates reach a client on its auth band."""
import struct
import time
from srvcore import log
from . import logingate

#: guid field = raw 2:3 record guid XOR this constant (the runtime key cancels
#: because the 2:3 slot-store XORs with the same key -- proven).
_PRESENCE_GUID_XOR = (0x67891133, 0x1c273e45)          # (lo, hi)


# --------------------------------------------------------------------------- #
# THE PUSH CHANNEL -- how SE actually delivers live updates (captured 2026-08-15)
# --------------------------------------------------------------------------- #
# Measured against SE's live service with two real accounts. The friend changed
# their profile PICTURE and it appeared in the other account's friend list with
# no refresh, no profile view and no relog. The carrier is NOT the `#XXL` channel
# message the `xxl` format above aims at -- that thesis is
# dead. It is a NICK-TARGETED NOTICE from a per-record
# pseudo-nick:
#
#     :PMY4QWC1V!~x@ NOTICE <mynick> :<POL-base64 record>
#
# 38 of them arrived in one sitting. Note the empty host after `@` -- that is
# SE's, not a transcription slip, so we reproduce it.
#
# THE RECORD IS THE SAME 72-BYTE STRUCT AS THE LOGIN GATE. `build_gate_record`
# above builds one for the 001 payload, and the client parses both with the same
# routine (`FUN_037db6f0`: base64-decode 0x60 chars -> 72 bytes, XOR-unmask
# [0:8]+[8:16], everything from 0x10 plaintext). Two independent checks agree:
#
#   * the gate's own discriminator. A gate needs [0x10]==1, [0x18]==0xff,
#     [0x1c]==0xff and [0x3e]&0xf80==0xf80. The captured PUSH record has
#     0x3e == 0x8000, so &0xf80 == 0 -- it CANNOT be mistaken for a gate by the
#     client, which is exactly what a shared parser requires.
#   * [0x42]&1. The gate sets it; the captured push has 0x42 == 0x03, which also
#     has bit 0 set. Same field, same rule, different record.
#
# So the `#XXL` RE trail was not wasted work -- it byte-mapped the right STRUCT
# through the right handler and posted it to the wrong CARRIER.
#
# Layout, read straight off the friend-request record (every field legible):
#
#     +0x00  guid, XOR-masked        +0x30  event code (u32)  [SEE BELOW: it is
#     +0x08  guid, XOR-masked        +0x34  unix timestamp     a THREAD INDEX]
#     +0x10  display name, 16 bytes  +0x38  08 00 00 00
#     +0x20  event text,   16 bytes  +0x3c  01 00 00 80
#                                    +0x40  the 0x03E8 member tag again
#
# *** THE "+0x30 EVENT CODE" READING IS RETRACTED (2026-08-22, measured). ***
# Re-decoding EVERY short push in polshim-se.429364.log settles it: the eleven
# short records SE sent one recipient carry +0x30 = 0,1,2,3,4,5,7,9,10,11,12 in
# arrival order, each paired with a mail subject in the text field -- and TWO of
# them are friend requests, riding 7 ("o/", Tobin's) and 11 ("Let's be friend",
# Yui's). +0x30 is the recipient's MAIL THREAD INDEX -- the same +0x30 the
# stored `O/m/` record carries (pol-lobby-messages' own field map) -- and the
# 0/7/13 "event map" was a coincidence of which position in the mailbox each
# observed message happened to land.
#
# What that means for the SHORT form: SE only ever sends it as A STORED
# MESSAGE'S OWN RECORD (the mail-arrival announcement, +0x42 state 03). There
# is no separate "friend request event" and no "profile change event" -- a
# friend request notifies as its MAIL, and a profile change rides ONLY the
# field-list class below (every 0xcf80 record in the capture; the "text = new
# value" reading was the field-list COMMENT chunk, misattributed). The client
# accordingly treats ANY short record as a mail announcement: it canonicalises
# the state byte and 3:0s the record as an `O/m/` token. A synthesized short
# record therefore names a message that does not exist, the store answers the
# miss with zeros, and the client lists a BLANK message that CRASHES it on open
# -- measured live 2026-08-22 (the retired 2:6-time "event 7" push).
#
# The constants below are kept for the re-arm knobs' sake, but they are
# labels for a misreading: do not build new pushes on them. The only correct
# short-form push is `_mail_announce`'s -- a real stored record, state-flipped.
_PUSH_EV_PROFILE = 0          # RETRACTED reading -- see the block above
_PUSH_EV_FRIEND_REQUEST = 7   # RETRACTED reading -- was Tobin's thread index
_PUSH_EV_MESSAGE = 13         # RETRACTED reading -- never used

#: The guid mask. Same constant pair the 2:3 slot-store XORs with, which is why
#: the runtime key cancels (proven during the `#XXL` work -- the one result from
#: that effort that survives its refutation).
_PUSH_GUID_MASK = (_PRESENCE_GUID_XOR[0] | (_PRESENCE_GUID_XOR[1] << 32))

#: The 72-byte form's own text field. The LONG form below carries text (and the
#: face icon, and the comment) in a separate chunk instead, so this cap applies
#: only to the short record.
_PUSH_TEXT_MAX = 15           # 16-byte field, NUL-terminated


def build_push_record(subject_guid, peer_guid, name, text, event, when=None):
    """One 72-byte push record -> POL-base64, ready for a NOTICE body."""
    r = bytearray(72)
    struct.pack_into("<Q", r, 0x00, (int(subject_guid) ^ _PUSH_GUID_MASK)
                     & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<Q", r, 0x08, (int(peer_guid) ^ _PUSH_GUID_MASK)
                     & 0xFFFFFFFFFFFFFFFF)
    if isinstance(name, str):
        name = name.encode("cp932", "replace")
    if isinstance(text, str):
        text = text.encode("cp932", "replace")
    if len(text) > _PUSH_TEXT_MAX:
        log("authserv", f"push: text truncated {len(text)} -> {_PUSH_TEXT_MAX} "
                        f"bytes (the long record form is not pinned)")
    r[0x10:0x10 + 16] = name[:_PUSH_TEXT_MAX].ljust(16, b"\x00")
    r[0x20:0x20 + 16] = text[:_PUSH_TEXT_MAX].ljust(16, b"\x00")
    struct.pack_into("<I", r, 0x30, int(event) & 0xFFFFFFFF)
    struct.pack_into("<I", r, 0x34, int(when if when is not None else time.time())
                     & 0xFFFFFFFF)
    struct.pack_into("<I", r, 0x38, 0x00000008)
    # 0x3c: `01 00 00 80`. The 0x8000 half is load-bearing -- it is what keeps
    # [0x3e]&0xf80 clear so the client's shared parser does not read this as a
    # login gate. Do not "tidy" it to zero.
    struct.pack_into("<I", r, 0x3c, 0x80000001)
    struct.pack_into("<I", r, 0x40, 0x000303E8)     # e8 03 03 00
    return logingate._b64encode(bytes(r))
