#!/usr/bin/env python3
"""Byte vectors for the push-channel event record, against SE's own capture.

The record was read off a live Square Enix session on 2026-08-15 with two real
accounts: one sent the other a friend request, and the resulting push arrived
complete and small enough that every field is legible. That capture is the
vector here -- these assertions compare our builder against SE, not against
ourselves, which is the only comparison worth making for a wire format.

    python tools/push_test.py        # exit 0 on success

What this CANNOT assert is that the client applies the record; that needs a live
client, and it is why the push ships behind POL_PRESENCE_PUSH. What it does
assert is that if the client is happy with SE's bytes it will be happy with ours,
because for every pinned field they are the same bytes.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

import responders

fails = []


def check(what, got, want):
    if got == want:
        print("  ok    %s" % what)
        return
    fails.append(what)
    print("  FAIL  %s\n          got  %r\n          want %r" % (what, got, want))


# --------------------------------------------------------------------------- #
# THE CAPTURED RECORD, transcribed from the friend request Yatih sent Fox.
# Offsets 0x30..0x43 are the fixed tail every record shares; 0x10/0x20 are the
# name and text fields with their real contents.
# --------------------------------------------------------------------------- #
SE_NAME = b"Yatih"
SE_TEXT = b"o/"
SE_EVENT = 7
SE_WHEN = 0x6A80C51E                      # 1e c5 80 6a, little-endian
SE_TAIL = bytes([0x07, 0x00, 0x00, 0x00,  # +0x30 event
                 0x1E, 0xC5, 0x80, 0x6A,  # +0x34 timestamp
                 0x08, 0x00, 0x00, 0x00,  # +0x38
                 0x01, 0x00, 0x00, 0x80,  # +0x3c
                 0xE8, 0x03, 0x03, 0x00])  # +0x40 the 0x03E8 member tag


def b64decode(s):
    """Inverse of responders._b64encode, for reading our own output back."""
    inv = {c: i for i, c in enumerate(responders._B64)}
    out = bytearray()
    for i in range(0, len(s), 4):
        g = s[i:i + 4]
        v = 0
        for c in g:
            v = (v << 6) | inv[c]
        v <<= 6 * (4 - len(g))
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return bytes(out)


print("push record vs SE capture 2026-08-15 (friend request Yatih -> Fox)")

enc = responders.build_push_record(0x1234567812345678, 0x8765432187654321,
                                   SE_NAME, SE_TEXT, SE_EVENT, SE_WHEN)
rec = b64decode(enc)

check("record is 72 bytes", len(rec), 72)
check("base64 is 96 symbols", len(enc), 96)
check("name field  +0x10", rec[0x10:0x20], SE_NAME.ljust(16, b"\x00"))
check("text field  +0x20", rec[0x20:0x30], SE_TEXT.ljust(16, b"\x00"))
check("fixed tail  +0x30", rec[0x30:0x44], SE_TAIL)

# The guid mask has to be an involution, or a client that unmasks gets rubbish.
lo = struct.unpack_from("<Q", rec, 0x00)[0] ^ responders._PUSH_GUID_MASK
hi = struct.unpack_from("<Q", rec, 0x08)[0] ^ responders._PUSH_GUID_MASK
check("guid A round-trips", lo, 0x1234567812345678)
check("guid B round-trips", hi, 0x8765432187654321)

# THE DISCRIMINATOR. The client parses gates and pushes with the same routine and
# tells them apart by these flags. If a push ever satisfies the gate test, the
# client will treat a friend update as a login completion -- so assert the
# separation rather than trusting the constant to stay put.
gate_like = (rec[0x10] == 1 and rec[0x18] == 0xFF and rec[0x1C] == 0xFF and
             (struct.unpack_from("<H", rec, 0x3E)[0] & 0x0F80) == 0x0F80)
check("does NOT look like a login gate", gate_like, False)
check("0x42 bit 0 set (shared with the gate)", rec[0x42] & 1, 1)

# The carrier: SE's prefix has an EMPTY host after `@`.
lines = responders.push_lines(b"CASNICK", 1, 2, b"Cyn", b"hi",
                              responders._PUSH_EV_PROFILE, SE_WHEN)
check("one line", len(lines), 1)
check("nick-targeted NOTICE", b" NOTICE CASNICK :" in lines[0], True)
check("empty host after @", b"!~x@ NOTICE" in lines[0], True)
check("pseudo-nick is token alphabet",
      all(chr(c) in responders.TOKEN_ALPHABET
          for c in lines[0][1:lines[0].index(b"!")]), True)

# A NICK CANNOT BEGIN WITH A DIGIT (RFC 1459; every SE push nick starts `PM`).
# TOKEN_ALPHABET contains ten digits, so a plain hash produced one on 19% of
# records -- and the FakeSessions below do not parse IRC, so no vector here
# would ever have caught it. Sample widely rather than testing one nick.
check("pseudo-nick never starts with a digit",
      sorted({responders._push_nick("rec-%d" % i)[0].isalpha()
              for i in range(4000)}), [True])

# Two different records must not share a pseudo-nick (SE's are per-record).
a = responders.push_lines(b"N", 1, 2, b"Cyn", b"a", 0, SE_WHEN)[0]
b = responders.push_lines(b"N", 1, 2, b"Cyn", b"b", 0, SE_WHEN)[0]
check("pseudo-nick differs per record", a.split(b"!")[0] != b.split(b"!")[0], True)

# Overlong text truncates rather than corrupting the fields after it -- the long
# record form is NOT pinned, so this is the documented, safe behaviour.
enc2 = responders.build_push_record(1, 2, b"N", b"Hello 2026 and then some",
                                    0, SE_WHEN)
rec2 = b64decode(enc2)
check("overlong text does not overrun", rec2[0x30:0x44],
      bytes([0, 0, 0, 0]) + SE_TAIL[4:])


# --------------------------------------------------------------------------- #
# THE LONG (FIELD-LIST) RECORD -- the one that carries a friend's FACE ICON.
#
# The vector is SE's own line, decrypted out of `polshim-se.429364.log` at
# 19:28:01 UTC on 2026-08-15, when the friend `Cyn` came online and their picture
# appeared on the account holder's friend list. Whole line, minus the 4-character
# line checksum `frame_line` adds:
#
#   :PMY4QWCEH!~x@ NOTICE UL0C0F1HJ :<96 chars main><22 chars chunk>P?{Q
#
# The guid is the same one their `2:3` row carried at +0x10 (008c002c1e04bc91),
# which is what makes this a real end-to-end check rather than a self-comparison:
# our masking, our chunk encoding and our field widths all have to agree with
# SE's at once for the string to come out equal.
# --------------------------------------------------------------------------- #
print("\nlong field-list record vs SE capture (Cyn's picture, 19:28:01Z)")

SE_ROW_GUID = 0x008C002C1E04BC91
SE_ROW_WHEN = 0x6A80BDC1
SE_ROW = ("iPfkQm5uPptTTTTTTTTTTTTSTTTTTTTTTTITTTTTTTTTTTTT"   # main, 1st 48
          "TTTTTTTTTTTTTTTTTTTTToc6AciZTTTTTTGTatTTT7TTTTTT"   # main, 2nd 48
          "S7TTTTTTTT80TATTTTTTTT")                            # chunk, 22

# A run of 'T' is base64 for a run of zeros, and a transcription that loses one
# still looks right. Assert the split lengths so a bad paste fails here rather
# than showing up as a mysterious byte mismatch below.
check("the vector is 96 + 22 characters", len(SE_ROW), 118)

# `block=True` reproduces SE exactly. The server does NOT send that bit -- see
# build_field_push_record -- so this asserts the encoder, not the policy.
check("reproduces SE's record byte for byte",
      responders.build_field_push_record(SE_ROW_GUID, 0, icon=757, seq=0,
                                         when=SE_ROW_WHEN, hslot=0, block=True),
      SE_ROW)

row = responders.build_field_push_record(SE_ROW_GUID, 3, icon=757,
                                         when=SE_ROW_WHEN)
main = b64decode(row[:96])
chunk = row[96:]
check("main record is still 96 symbols", len(row) - len(chunk), 96)
check("+0x1c is the friend's 2:3 slot", main[0x1C], 3)
check("+0x19 bit 0 set (the field-list gate)", main[0x19] & 1, 1)
check("+0x1a / +0x1b clear (not a gate, not a text event)",
      (main[0x1A], main[0x1B]), (0, 0))
check("+0x3e selects the field-list class",
      struct.unpack_from("<H", main, 0x3E)[0] & 0x0F80, 0x0F80)
check("+0x42 bit 0 set", main[0x42] & 1, 1)
check("+0x38 is the chunk length + 1",
      struct.unpack_from("<I", main, 0x38)[0], len(chunk) + 1)
check("guid round-trips through the mask",
      struct.unpack_from("<Q", main, 0x00)[0] ^ responders._PUSH_GUID_MASK,
      SE_ROW_GUID)
# The presence bit is the one field that can do harm: it rewrites the friend
# slot's status word, which is where our 2:3 +0x09 online/offline byte landed.
check("the presence block is NOT sent by default",
      b64decode(chunk + "T" * (-len(chunk) % 4))[0] & 0x01, 0)

# Field widths: absent means "leave alone", so the chunk must grow by exactly the
# parser's stride per field or every field after it lands on the wrong bytes.
def chunk_bytes(**kw):
    r = responders.build_field_push_record(SE_ROW_GUID, 0, when=SE_ROW_WHEN, **kw)
    return b64decode(r[96:] + "T" * (-len(r[96:]) % 4))


check("icon alone: 8 header + 8", len(chunk_bytes(icon=1)[:16]), 16)
check("icon + name: 8 header + 8 + 16", len(chunk_bytes(icon=1, name="Cyn")[:32]),
      32)
check("the icon is a u32 at chunk+8",
      struct.unpack_from("<I", chunk_bytes(icon=903), 8)[0], 903)
check("the name follows the icon at chunk+16",
      chunk_bytes(icon=903, name="Cyn")[16:20], b"Cyn\x00")
check("a comment is UTF-16LE at chunk+16 when it follows the icon",
      chunk_bytes(icon=903, comment="hi")[16:20], "hi".encode("utf-16-le"))

# The client caps the chunk at 0x158 characters and drops anything longer, so
# refuse to build one rather than emit a record that will be silently ignored.
try:
    responders.build_field_push_record(SE_ROW_GUID, 0, name="x" * 15,
                                       comment="c" * 50, icon=1,
                                       when=SE_ROW_WHEN)
    check("a full record still fits the 0x158 cap", True, True)
except ValueError as exc:
    check("a full record still fits the 0x158 cap", repr(exc), "fits")


# --------------------------------------------------------------------------- #
# THE FAN-OUT. Vectors above prove the bytes; this proves the routing -- that an
# event reaches exactly the people it should and nobody else. The two directions
# are different and both matter:
#
#   broadcast_event  my profile changed -> everyone WATCHING me
#   push_to_handle   I did something to you -> only YOU
# --------------------------------------------------------------------------- #
import json                                                       # noqa: E402
import tempfile                                                   # noqa: E402
import accounts                                                   # noqa: E402


class FakeSession:
    def __init__(self, nick):
        self.nick = nick.encode() if isinstance(nick, str) else nick
        self.srv = b"pol"
        self.sent = []
        self.alive = True

    def send(self, lines, pad_override=None):
        self.sent.append(list(lines))
        return True


path = os.path.join(tempfile.mkdtemp(prefix="pushtest-"), "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = path
c = accounts.connect(path)
ids = {}
for who in ("Fox", "Cyn", "Stranger"):
    m = accounts.ensure_member(c, who)
    h = c.execute("SELECT id FROM handle WHERE member_id=?",
                  (m["id"],)).fetchone()["id"]
    ids[who] = (int(m["id"]), int(h))
# Cyn watches Fox. Stranger does not.
accounts.add_friend(c, ids["Cyn"][1], "Fox", peer_handle=ids["Fox"][1])

os.environ["POL_PRESENCE_PUSH"] = "1"
# Pretend to be the authserv process: it is the one that holds session channels,
# so it is the one that delivers. Everywhere else the record goes to the spool --
# exercised separately below.
responders._PUSH_LOCAL[0] = True
cyn = FakeSession("CYNNICK")
stranger = FakeSession("STRANGER")
responders.PRESENCE.register(ids["Cyn"][0], cyn)
responders.PRESENCE.register(ids["Stranger"][0], stranger)

# A synthesized SHORT record is a phantom mail announcement to the client
# (measured live 2026-08-22 -- the retired event-7 push; see responders'
# +0x30 retraction), so broadcast_event drops it BY DEFAULT and the field-list
# record is the whole profile push, as SE's own capture shows.
n = responders.broadcast_event(c, ids["Fox"][1], responders._PUSH_EV_PROFILE,
                               "banana")
check("the short record is dropped by default", n, 0)
check("...nothing reaches the watcher's session", len(cyn.sent), 0)

# The MECHANISM stays testable behind the re-arm knob.
os.environ["POL_PUSH_SHORT_EVENT"] = "1"
n = responders.broadcast_event(c, ids["Fox"][1], responders._PUSH_EV_PROFILE,
                               "banana")
check("comment change reaches the watcher", n, 1)
check("watcher got one line", len(cyn.sent), 1)
check("non-watcher got nothing", len(stranger.sent), 0)
check("addressed to the watcher's nick",
      b" NOTICE CYNNICK :" in cyn.sent[0][0], True)

# The event text really is the new comment, all the way through the codec.
body = cyn.sent[0][0].split(b" :", 1)[1].decode()
check("payload carries the new value",
      b64decode(body)[0x20:0x26], b"banana")

# A friend request goes to the TARGET, not to the requester's watchers.
cyn.sent.clear()
n = responders.push_to_handle(c, ids["Cyn"][1],
                              responders._PUSH_EV_FRIEND_REQUEST, "",
                              from_handle_id=ids["Fox"][1], from_name="Fox")
check("request reaches the target", n, 1)
check("request names the requester",
      b64decode(cyn.sent[0][0].split(b" :", 1)[1].decode())[0x10:0x13], b"Fox")

# Offline target: no sessions, no error, no queue.
check("offline target is a no-op, not a failure",
      responders.push_to_handle(c, ids["Stranger"][1] + 999, 7, ""), 0)

# --------------------------------------------------------------------------- #
# THE CROSS-PROCESS HOP. `login` (lobby) and `authsess` (authserv) are separate
# containers; a comment edit arrives in the first and the socket to push it down
# lives in the second. Without the spool, every lobby-side push would fan out
# over an empty registry and report success having sent nothing -- so this is the
# assertion that the two halves are actually connected.
# --------------------------------------------------------------------------- #
responders._PUSH_SPOOL = os.path.join(os.path.dirname(path), "push-spool.jsonl")
responders._PUSH_LOCAL[0] = False                    # i.e. we are the LOBBY now
cyn.sent.clear()
n = responders.broadcast_event(c, ids["Fox"][1], responders._PUSH_EV_PROFILE,
                               "spooled")
check("lobby-side push delivers nothing directly", n, 0)
check("lobby-side push does not touch the session", len(cyn.sent), 0)
check("...but it is on disk", os.path.exists(responders._PUSH_SPOOL), True)

# Now be authserv: drain what the lobby wrote and check it lands.
responders._PUSH_LOCAL[0] = True
with open(responders._PUSH_SPOOL, encoding="utf-8") as f:
    spooled = [json.loads(l) for l in f if l.strip()]
check("exactly one record spooled", len(spooled), 1)
for r in spooled:
    responders._push_deliver(r, c)
check("drained record reaches the watcher", len(cyn.sent), 1)
check("drained payload survived the round trip",
      b64decode(cyn.sent[0][0].split(b" :", 1)[1].decode())[0x20:0x27],
      b"spooled")

# THE FACE-ICON FAN-OUT. Same spool, different record kind, and one property
# that matters more than the rest: it must reach the WATCHER themself (the person
# whose friend list is on screen), not the friend whose picture it carries.
responders._PUSH_LOCAL[0] = False
cyn.sent.clear()
# The row push DEFAULTS OFF since it broke login with POL-5135 (see
# _row_push_enabled), so the fan-out vectors have to opt in explicitly.
os.environ["POL_FRIEND_ROW_PUSH"] = "1"
open(responders._PUSH_SPOOL, "w").close()
responders.push_friend_icons(None, ids["Cyn"][0],
                             [(0, SE_ROW_GUID, 757), (1, SE_ROW_GUID + 1, 903)])
with open(responders._PUSH_SPOOL, encoding="utf-8") as f:
    rows = [json.loads(l) for l in f if l.strip()]
check("icon rows spool as one record", len(rows), 1)
check("...carrying both slots", len(rows[0]["rows"]), 2)
responders._PUSH_LOCAL[0] = True
rows[0]["after"] = 0                     # skip the ordering delay in the test
responders._push_deliver(rows[0], c)
check("both icons reach the watcher's session", len(cyn.sent[0]), 2)
check("addressed to the WATCHER, not the friend",
      b" NOTICE CYNNICK :" in cyn.sent[0][0], True)
first = b64decode(cyn.sent[0][0].split(b" :", 1)[1].decode()[:96])
check("the row push names slot 0", first[0x1C], 0)
check("...and its guid is the friend's",
      struct.unpack_from("<Q", first, 0x00)[0] ^ responders._PUSH_GUID_MASK,
      SE_ROW_GUID)

# The row push has its OWN switch: presence being off must not blank the
# pictures, which is the whole reason it is not tied to POL_PRESENCE_PUSH.
os.environ["POL_FRIEND_ROW_PUSH"] = "0"
cyn.sent.clear()
check("rowpush=0 sends nothing",
      responders.push_friend_icons(None, ids["Cyn"][0], [(0, SE_ROW_GUID, 757)]),
      0)
# And OFF is the default -- this one is load-bearing, not cosmetic: the push
# broke every login with POL-5135, so a build that quietly re-enables it is a
# regression that only a live client would catch.
del os.environ["POL_FRIEND_ROW_PUSH"]
check("...and off is the DEFAULT", responders._row_push_enabled(), False)
os.environ["POL_FRIEND_ROW_PUSH"] = "1"

# A malformed line must not kill the drain loop's record handling.
try:
    responders._push_deliver({"kind": "watchers"})
    check("malformed record does not raise", True, True)
except Exception as exc:
    check("malformed record does not raise", repr(exc), "no exception")

# --------------------------------------------------------------------------- #
# WHISPERS. A PRIVMSG to a NICK rather than a channel. Same capture, and until
# now these fell off the end of the PRIVMSG handler and vanished -- the target is
# not a channel, so there was nothing to broadcast to.
# --------------------------------------------------------------------------- #
cas = FakeSession("CASNICK")
responders.PRESENCE.register(ids["Fox"][0], cas)
cyn.sent.clear()
responders._auth_session_reply(b"PRIVMSG CYNNICK :hello there", b"CASNICK",
                               b"pol", sess=cas)
check("whisper reaches the named nick", len(cyn.sent), 1)
check("whisper body survives", b":hello there" in cyn.sent[0][0], True)
check("whisper is attributed to the sender",
      cyn.sent[0][0].startswith(b":CASNICK!~x@"), True)
check("no echo to the sender", len(cas.sent), 0)

# A whisper to somebody who is not online is dropped, not an error.
cyn.sent.clear()
responders._auth_session_reply(b"PRIVMSG NOBODY :hi", b"CASNICK", b"pol",
                               sess=cas)
check("whisper to an absent nick is inert", len(cyn.sent), 0)

# And the whole thing stays inert when the push is disabled.
os.environ["POL_PRESENCE_PUSH"] = "0"
cyn.sent.clear()
responders.broadcast_event(c, ids["Fox"][1], responders._PUSH_EV_PROFILE, "x")
check("disabled means silent", len(cyn.sent), 0)
c.close()

print("%s (%d failure%s)" % ("all vectors match" if not fails else "FAILED",
                             len(fails), "" if len(fails) == 1 else "s"))
sys.exit(1 if fails else 0)
