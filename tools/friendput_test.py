#!/usr/bin/env python3
"""The 02:06 friend-list WRITE: grid, deletion, and the reply SE actually sends.

Two live failures on 2026-08-16, both here:

  * "Updating friend list" never finished. The reply was declared at 168 bytes,
    a guess whose comment claimed SE sent it. SE sends **184** -- all four 2:6
    writes in polshim-se.429364.log, on 480-byte requests the same size as ours.
    Sixteen bytes short leaves the reader blocked mid-payload.
  * Deleting a friend hung too, because the parser demanded a parseable NAME in
    every record before it would accept the grid. A real deletion carries stale
    heap in the name field -- SE's own capture has one, and the account holder's
    is byte-for-byte the same shape -- so the write read as "did not parse" and
    was refused forever.

    python tools/friendput_test.py        # exit 0 on success
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

os.environ.setdefault("POL_LOG_DIR", tempfile.mkdtemp(prefix="fptest-log-"))
import responders as R                                          # noqa: E402

fails = []


def check(what, got, want):
    if got == want:
        print("  ok    %s" % what)
        return
    fails.append(what)
    print("  FAIL  %s\n          got  %r\n          want %r" % (what, got, want))


def make_put(record):
    """A 02:06 frame: 0x28 header, 0x134 preamble, one 168-byte record, checksum.

    The preamble's ascending 00,01,02... is what the client really sends -- the
    empty friend slots' index markers.
    """
    pt = bytearray(b"\x02\x02\x06\x00")
    pt += struct.pack("<I", 0x134 + len(record) + 4)
    pt += b"\x00" * (0x28 - 8)
    pt += bytes(i & 0xFF for i in range(0x134))          # the preamble
    pt += record
    pt += b"\xde\xad\xbe\xef"                            # request checksum
    return bytes(pt)


ADD = bytearray(168)
ADD[0x04:0x08] = b"\x00\x00\x0a\x00"
ADD[0x14:0x18] = b"Fox\x00"
ADD[0x18:0x24] = bytes.fromhex("26ac32e609dddcd4947886dd")     # the client's id
# The DELETE, measured: same record, same id at +0x18, stale heap where the name
# was, junk in the head bytes the add left zero.
DEL = bytearray(ADD)
DEL[0x00:0x08] = bytes.fromhex("6d58750900660a00")
DEL[0x0C:0x18] = bytes.fromhex("66f79fe7547f164199955d1b")

print("02:06 friend write")

start, raw = R._friend_put_records(make_put(ADD))
check("the grid is at the derived 0x15C", start, 0x15C)
check("one record in a 520-byte frame", len(raw), 1)
check("an add parses to its name",
      [r["name"] for r in R._parse_friend_put(make_put(ADD))], ["Fox"])

start, raw = R._friend_put_records(make_put(DEL))
check("a DELETE still finds its grid -- the name is stale heap, not a name",
      (start, len(raw)), (0x15C, 1))
check("and resolves to an empty list, which is what a deletion IS",
      R._parse_friend_put(make_put(DEL)), [])

# A length that does NOT fit `0x15C + N*168 + 4` is a misread, not a deletion,
# and must still find nothing rather than wipe somebody's friend list.
bad = make_put(ADD)[:-7]
check("a frame that does not fit the builder's formula finds no grid",
      R._friend_put_records(bad)[1], [])

print("\nthe reply SE sends")
os.environ["POL_FRIEND_PUT_REPLY"] = "se"
req = make_put(ADD)
R._LAST_FRIEND_PUT[:] = [bytes(ADD)]
rep = R._friend_put_reply(184, req)
check("the reply is 184 bytes, not 168", len(rep), 184)
check("count of entries applied at +0x00", struct.unpack_from("<I", rep, 0)[0], 1)
check("the request's 0x158.. is echoed into +0x08",
      rep[0x08:0x11], req[0x158:0x161])
check("record +0x05/+0x06 are normalised away", (rep[0x11], rep[0x12]), (0, 0))
check("the name field's last byte is cleared", rep[0x2F], 0)
marks = list(range(0x41, 0xB0, 0x10))          # the seven SE clears, +0x41..+0xA1
check("the empty slots' index markers are cleared",
      [rep[at] for at in marks], [0] * len(marks))
check("a row id is assigned when the client sent none",
      rep[0x09:0x0C] != b"\x00\x00\x00", True)
check("and it is STABLE across processes (hashlib, not hash())",
      R._friend_put_reply(184, req)[0x09:0x0D], rep[0x09:0x0D])

# --------------------------------------------------------------------------- #
# TWO RECORDS IN ONE WRITE -- SE's own shape, and one we answered wrongly.
#
# `KPutFriendList` was believed to be one record per change ("EVERY 2:6 ever
# captured is payload_len=480"), and the reply builder was written to that: count
# 1, one unit, every offset an absolute number. The 2026-08-19 retail capture has
# a **two-record write** (the ignore-add at capture line 259509, body 0x288) and
# SE answers it with a **352-byte, two-unit reply** (line 259803, paylen 0x160).
# Ours declared `count=1` and echoed one record, which is the same
# promise-more-than-you-carry failure the 7:12 count block is guarded against.
#
# The unit is 168 -- the request grid's own stride -- because the copy is 1:1
# from request 0x158: `reply[+0x08 + k] == request[0x158 + k]`. So the payload is
# `N*168 + 16`, of which SE's two shapes are the N=1 (184) and N=2 (352) cases.
print("\ntwo records in one write (SE's ignore-add shape)")

REC2_HEAD = bytes.fromhex(          # request 0x200: state dword + record 1 head
    "3100000000000034000044000000000091bc041e2c008c0043796e00")


def make_put2(rec_a, rec_b_head):
    """A 688-byte two-record 2:6, framed the way the builder computes it."""
    pt = bytearray(0x2B0)
    pt[0x15C:0x15C + len(rec_a)] = rec_a
    pt[0x200:0x200 + len(rec_b_head)] = rec_b_head
    return bytes(pt)


req2 = make_put2(ADD, REC2_HEAD)
check("a 688-byte frame parses as TWO records",
      len(R._friend_put_records(req2)[1]), 2)
check("the reply length follows N*168 + 16", R._friend_put_reply_len(req2), 352)
check("...and the 1-record case is the table's own 184",
      R._friend_put_reply_len(make_put(ADD)), 184)

rep2 = R._friend_put_reply(352, req2)
check("the two-record reply is 352 bytes", len(rep2), 352)
check("count of entries applied is 2", struct.unpack_from("<I", rep2, 0)[0], 2)
# Unit 1 sits one 168-byte stride past unit 0, which is where SE puts it.
BASE1 = 0x08 + 168
check("unit 1 begins at +0xB0", BASE1, 0xB0)
# Byte 0 of the state dword is the IGNORE SCOPE and must round-trip untouched;
# bytes 1..3 are the row id, which is the SERVER's to assign -- SE turns the
# client's `31 00 00 00` into `31 a0 13 ea` in exactly this field.
check("unit 1 round-trips the ignore-scope byte", rep2[BASE1], req2[0x200])
check("...while bytes 1..3 hold the row id we assigned",
      rep2[BASE1 + 1:BASE1 + 4] != b"\x00\x00\x00", True)
check("unit 1 carries the SECOND record's guid, not the first's",
      rep2[BASE1 + 0x10:BASE1 + 0x18], req2[0x210:0x218])
check("unit 1 carries the second record's name",
      bytes(rep2[BASE1 + 0x18:BASE1 + 0x28]).split(b"\x00")[0], b"Cyn")
# The normalisation is per unit, not once for the whole reply. SE's own second
# unit has the request's 0x44 at +0x0A cleared to 0.
check("record +0x05/+0x06 are normalised in unit 1 too",
      (rep2[BASE1 + 0x09], rep2[BASE1 + 0x0A]), (0, 0))
check("...and the request really did carry a 0x44 there to clear",
      req2[0x20A], 0x44)
check("the name field's last byte is cleared in unit 1", rep2[BASE1 + 0x27], 0)
check("a row id is assigned in unit 1 as well",
      rep2[BASE1 + 0x01:BASE1 + 0x04] != b"\x00\x00\x00", True)
check("and unit 0 is untouched by any of it -- it still holds record 0's name",
      bytes(rep2[0x20:0x30]).split(b"\x00")[0], b"Fox")

# THE LENGTH AND THE COUNT MUST AGREE. Handed a reply too short for the records
# the write carried, the builder must serve the count it can actually back --
# declaring two and carrying one is what walks the reader past the last record.
rep_short = R._friend_put_reply(184, req2)
check("a reply with room for one record declares one",
      struct.unpack_from("<I", rep_short, 0)[0], 1)

# POL_FRIEND_PUT_MULTI=0 pins the old single-record behaviour.
os.environ["POL_FRIEND_PUT_MULTI"] = "0"
check("POL_FRIEND_PUT_MULTI=0 restores the one-record reply",
      struct.unpack_from("<I", R._friend_put_reply(352, req2), 0)[0], 1)
del os.environ["POL_FRIEND_PUT_MULTI"]

print()

# A deletion has no name to build a reply around -- echoing is what makes it
# answerable at all.
R._LAST_FRIEND_PUT[:] = [bytes(DEL)]
rep = R._friend_put_reply(184, make_put(DEL))
check("a deletion gets the same 184-byte answer", len(rep), 184)

# A log line must never take down the request that wrote it: the name field of a
# deletion decodes to characters the Windows console cannot print.
R._log("lobby", "junk name: %r" % bytes(DEL[0x14:0x24])) if hasattr(R, "_log") \
    else R.log("lobby", "junk name: " + bytes(DEL[0x14:0x24]).decode("cp932", "replace"))
check("logging a junk name does not raise", True, True)

print("\na deletion names its row by the client's own id")

check("a named record carries the client ref",
      R._parse_friend_put(make_put(ADD))[0]["client_ref"],
      bytes(ADD[0x18:0x24]))
check("a nameless record IS a delete", len(R._friend_put_deletes(make_put(DEL))), 1)
check("a named record is not a delete", R._friend_put_deletes(make_put(ADD)), [])

# THE ID SITS BEHIND THE NAME, so its offset moves with the name's length. Nailing
# it to +0x18 was right for "Fox" and wrong for every other name -- the live store
# recorded "LaptopTest2"'s id as the tail of its own name plus four real bytes.
LONG = bytearray(168)
LONG[0x04:0x08] = b"\x00\x00\x0a\x00"
LONG[0x14:0x20] = b"LaptopTest2\x00"
LONG[0x20:0x2C] = bytes.fromhex("d37cd362bc405f0b35cd138e")
check("a long name does not shift the id out of reach",
      R._friend_put_ref(LONG, "LaptopTest2"),
      bytes.fromhex("d37cd362bc405f0b35cd138e"))
check("and a short one still reads at +0x18",
      R._friend_put_ref(ADD, "Fox"), bytes(ADD[0x18:0x24]))

import accounts as A                                            # noqa: E402

db = A.connect(os.path.join(tempfile.mkdtemp(prefix="fpdb-"), "accounts.db"))
me = A.ensure_member(db, "TESTER1")
you = A.ensure_member(db, "TESTER2")
mine = A.primary_handle_row(db, me["id"])
theirs = A.primary_handle_row(db, you["id"])
entry = dict(name=theirs["handle_name"], guid=0, kind=A.KIND_FRIEND,
             client_ref=bytes(ADD[0x18:0x24]), wire_ref=bytes.fromhex("a21af5eb"))
A.replace_friends(db, int(mine["id"]), [entry])
check("the write stored the row",
      [r["peer_name"] for r in A.list_friends(db, int(mine["id"]), status=None)],
      [theirs["handle_name"]])
check("and remembered the client's id for it",
      A.list_friends(db, int(mine["id"]), status=None)[0]["client_ref"],
      bytes(ADD[0x18:0x24]))
check("the delete lands on the right row, wherever the id sits in it",
      A.remove_friend_in_record(db, int(mine["id"]), bytes(DEL)),
      theirs["handle_name"])
check("and the row is GONE, which is what 'not persistent' meant",
      A.list_friends(db, int(mine["id"]), status=None), [])

# THE SECOND ID. A row learned by either route is deletable, which is what keeps
# a delete working when the 12-byte one never got recorded.
A.replace_friends(db, int(mine["id"]),
                  [dict(name=theirs["handle_name"], guid=0, kind=A.KIND_FRIEND,
                        wire_ref=bytes.fromhex("a21af5eb"))])
check("a row known only by its wire ref still deletes",
      A.remove_friend_in_record(db, int(mine["id"]), bytes(168),
                                wire_ref=bytes.fromhex("a21af5eb")),
      theirs["handle_name"])
check("an unknown ref deletes nothing",
      A.remove_friend_by_ref(db, int(mine["id"]), b"\x01" * 12), None)

print("\nthe head word: the friend's id, and the profile subject built from it")

# `0x200000 | (0x1EC96021 >> 13) == 0x20F64B` -- the constant z_hid every 05:04
# in every log has ever asked for. SE's head word, replayed into every row by us,
# IS where the client reads the profile subject. Hence one subject for all
# friends, hence the bleed.
check("the arithmetic that identified the field",
      0x200000 | (0x1EC96021 >> 13), 0x20F64B)

REC = R._friend_list_record(168, 0x80000000016, "LaptopTest2", hid=22, index=3)
head = struct.unpack_from("<I", REC, 0)[0]
check("SE's low 13 bits are kept exactly", head & 0x1FFF, 0x1EC96021 & 0x1FFF)
check("the friend's handle id rides bits 13..31", head >> 13, 22)
check("the subject the client will now send resolves to that friend",
      (0x200000 | (head >> 13)) & ~R._FRIEND_HID_TAG, 22)

# The collapse this must never cause again: +0x04 is the status word and +0x08 is
# the destination SLOT. Writing a tag over those made eleven friends render as one.
check("the status word at +0x04 is untouched", REC[4:8].hex(), "04000006")
check("the slot at +0x08 still carries the record index", REC[8], 3)
check("so two rows land in different slots",
      R._friend_list_record(168, 0x8, "A", hid=1, index=0)[8], 0)

check("a delete echoing the head word names its friend",
      R._friend_row_handles(REC), [22])
check("a frame with no head word of ours names nobody",
      R._friend_row_handles(bytes(168)), [])

A.replace_friends(db, int(mine["id"]),
                  [dict(name=theirs["handle_name"], guid=0, kind=A.KIND_FRIEND)])
check("a row known ONLY to us -- no client id at all -- still deletes",
      A.remove_friend_by_peer_handle(db, int(mine["id"]), int(theirs["id"])),
      theirs["handle_name"])
check("and an echo from the wrong handle deletes nothing",
      A.remove_friend_by_peer_handle(db, 999, int(theirs["id"])), None)

print("\na delete names its row by the 2:3 SLOT at +0x04")

# THE VECTORS ARE REAL. SE's four 2:6 writes, decoded out of
# polshim-se.429364.log with pol-shim/tools/lobbydec.py, and seven of the account
# holder's own -- only the first eight bytes of each record are needed, because
# that is where the slot is. `+0x06 == 0x0a` in every one of them.
def rec8(head8):
    r = bytearray(168)
    r[0:8] = bytes.fromhex(head8)
    return bytes(r)


# (what it was, record head, the slot it names)
SE_WRITES = [
    ("SE line 73345, add 'Cyn' at 2:3 slot 0",    "00000064 00000a00", 0),
    ("SE line 141215, add 'Yatih' at slot 1",     "00000042 01010a00", 1),
    ("SE line 143962, the DELETE (record heap)",  "38c20e02 008b0a00", 0),
    ("SE line 146089, re-add 'Cyn' at slot 0",    "04000042 00000a00", 0),
]
for what, head, slot in SE_WRITES:
    check(what, R._friend_put_slot(rec8(head.replace(" ", ""))), slot)

# Ours, from lobby.log 2026-08-16. Every byte from +0x0C on was identical stale
# heap across all seven; +0x04 was the only one that moved.
OURS = [("5c2060ef 054b0a00", 5), ("5c2060ef 004b0a00", 0),
        ("5c2060ef 024b0a00", 2), ("5c2060ef 044b0a00", 4),
        ("5c2060ef 034b0a00", 3)]
check("our client's seven deletes name slots 5,0,2,4,3",
      [R._friend_put_slot(rec8(h.replace(" ", ""))) for h, _ in OURS],
      [s for _, s in OURS])

# The guards. A record that fails either check must name NOBODY rather than
# index the friend list with heap.
check("a record without the 0x0a live marker names nobody",
      R._friend_put_slot(rec8("5c2060ef 054bff00")), None)
check("and a slot past the client's own 0x40 cap is heap, not a slot",
      R._friend_put_slot(rec8("5c2060ef 7f4b0a00")), None)
check("the delete fixture above still reads as slot 0",
      R._friend_put_slot(bytes(DEL)), 0)

# The resolver indexes the map of what we SERVED, so the slot cannot mean one
# thing on the way out and another on the way back.
SERVED = [(0, "Tester2", 41), (1, "DeckTester", 43), (2, "S02", 44)]
R._FRIEND_SLOTS.clear()
R._friend_list_handle_id = lambda: 7            # no session in this harness
R._friend_slots_publish(SERVED)
MAP = R._friend_slots_map(7)
check("slot 0 resolves to the first row we served",
      R._friend_slot_row(MAP, 0), ("Tester2", 41))
check("slot 2 resolves to the third", R._friend_slot_row(MAP, 2), ("S02", 44))
check("a slot past the end resolves to nobody",
      R._friend_slot_row(MAP, 3), None)
check("and so does a negative one", R._friend_slot_row(MAP, -1), None)
check("a handle we never served has no map at all",
      R._friend_slots_map(8), None)

# THE SHIFT BUG (2026-08-16). The client indexes the list AS SERVED and never
# renumbers, so the slots of everyone below a deleted row must NOT move. Resolving
# against the live list instead shifted them by one per delete and took out the
# wrong person -- silently, because the client hides the row you clicked either
# way and only a relog shows it.
R._friend_slots_update(7, 1, None)              # DeckTester deleted
check("a delete does not renumber the rows below it",
      R._friend_slot_row(R._friend_slots_map(7), 2), ("S02", 44))
check("and the deleted slot itself now names nobody",
      R._friend_slot_row(R._friend_slots_map(7), 1), None)
# The live-list resolver is what the old code did. Kept only as the no-2:3
# fallback; this is the arithmetic that proves it cannot be the primary.
LIVE = [(0, "Tester2", A.KIND_FRIEND, 8, "pending", 41),
        (0, "S02", A.KIND_FRIEND, 0, "active", 44)]
check("re-querying after that delete would have made slot 2 out of range",
      {i: (r[1], int(r[5])) for i, r in enumerate(LIVE)}.get(2), None)

# An ADD carries a slot too, and the client files the new row there without
# re-reading the list -- so the map has to learn it or a same-session delete of
# that friend is refused.
R._friend_slots_update(7, 3, ("S09", 61))
check("a row added this session is deletable by its slot",
      R._friend_slot_row(R._friend_slots_map(7), 3), ("S09", 61))
# A fresh 2:3 renumbers the client's whole table, so it replaces the map outright.
R._friend_slots_publish([(0, "Tester2", 41), (1, "S02", 44), (2, "S09", 61)])
check("a new 2:3 replaces the numbering rather than merging into it",
      [R._friend_slot_row(R._friend_slots_map(7), s) for s in (0, 1, 2, 3)],
      [("Tester2", 41), ("S02", 44), ("S09", 61), None])

# End to end on the DB: a row the client has only ever READ -- no client_ref, no
# wire_ref, no peer_handle -- is exactly the row the two id searches could never
# reach, and it is the common case.
A.replace_friends(db, int(mine["id"]),
                  [dict(name="S02", guid=0, kind=A.KIND_FRIEND),
                   dict(name="S03", guid=0, kind=A.KIND_FRIEND)])
rows = A.list_friends(db, int(mine["id"]), status=None)
check("two rows stored with no client id of any kind",
      [r["peer_name"] for r in rows], ["S02", "S03"])
check("deleting slot 1 by row id names the second",
      A.remove_friend_by_row(db, int(mine["id"]), int(rows[1]["id"])), "S03")
check("and it is gone while the first survives",
      [r["peer_name"] for r in A.list_friends(db, int(mine["id"]), status=None)],
      ["S02"])
check("a row id from somebody else's list deletes nothing",
      A.remove_friend_by_row(db, 999, int(rows[0]["id"])), None)

print("\nthe accept moves both rows, as SE's own service does")

db2 = A.connect(os.path.join(tempfile.mkdtemp(prefix="fpacc-"), "accounts.db"))
asker = A.primary_handle_row(db2, A.ensure_member(db2, "ASKER1")["id"])
askee = A.primary_handle_row(db2, A.ensure_member(db2, "ASKEE1")["id"])
A.request_friend(db2, int(asker["id"]), askee["handle_name"])
check("the ask stores pending here and invited there",
      [A.list_friends(db2, int(asker["id"]), status=None)[0]["status"],
       A.list_friends(db2, int(askee["id"]), status=None)[0]["status"]],
      [A.STATUS_PENDING, A.STATUS_INVITED])

check("accepting activates BOTH sides",
      A.request_friend(db2, int(askee["id"]), asker["handle_name"]), "accepted")
check("so the friendship is mutual without waiting on the asker's client",
      [A.list_friends(db2, int(asker["id"]), status=None)[0]["status"],
       A.list_friends(db2, int(askee["id"]), status=None)[0]["status"]],
      [A.STATUS_ACTIVE, A.STATUS_ACTIVE])

check("and the asker naming them again is inert, not a second request",
      A.request_friend(db2, int(asker["id"]), askee["handle_name"]), "exists")

# THE OLD TWO-STEP IS STILL REACHABLE, and this is the only place that takes
# that branch -- without it `POL_FRIEND_ACCEPT_BOTH=0` would rot untested and be
# worthless the day somebody needs to fall back to it.
os.environ["POL_FRIEND_ACCEPT_BOTH"] = "0"
try:
    A.remove_friend(db2, int(asker["id"]), askee["handle_name"])
    A.remove_friend(db2, int(askee["id"]), asker["handle_name"])  # both sides
    A.request_friend(db2, int(asker["id"]), askee["handle_name"])
    check("ACCEPT_BOTH=0: accepting activates the ACCEPTOR only",
          A.request_friend(db2, int(askee["id"]), asker["handle_name"]), "accepted")
    check("ACCEPT_BOTH=0: the asker stays pending, so their notification still "
          "makes sense",
          [A.list_friends(db2, int(asker["id"]), status=None)[0]["status"],
           A.list_friends(db2, int(askee["id"]), status=None)[0]["status"]],
          [A.STATUS_PENDING, A.STATUS_ACTIVE])
    check("ACCEPT_BOTH=0: the asker naming them again is the confirmation",
          A.request_friend(db2, int(asker["id"]), askee["handle_name"]), "accepted")
    check("ACCEPT_BOTH=0: which finally makes it mutual",
          [A.list_friends(db2, int(asker["id"]), status=None)[0]["status"],
           A.list_friends(db2, int(askee["id"]), status=None)[0]["status"]],
          [A.STATUS_ACTIVE, A.STATUS_ACTIVE])
finally:
    os.environ.pop("POL_FRIEND_ACCEPT_BOTH", None)

print("\ndeleting a friend is symmetric, and one-sided leftovers still heal")

# Deletion used to be one-sided -- I drop them, they keep me -- and every
# stale-row failure descended from it: a re-friend found the peer still holding
# a row, mirrored nothing, and the asker waited on a request nobody was ever
# sent. Reported live 2026-08-16 (two requests, neither received). Deletion is
# symmetric now; the repair paths below stay because old rows and the knob both
# exist, and because a peer can always drop you between the ask and the resend.
db3 = A.connect(os.path.join(tempfile.mkdtemp(prefix="fpre-"), "accounts.db"))
AH = int(A.primary_handle_row(db3, A.ensure_member(db3, "AAAAA1")["id"])["id"])
BR = A.primary_handle_row(db3, A.ensure_member(db3, "BBBBB1")["id"])
BH, BN = int(BR["id"]), BR["handle_name"]
AN = A.primary_handle_row(db3, A.ensure_member(db3, "AAAAA1")["id"])["handle_name"]


def wipe():
    """Clear BOTH sides, whatever the symmetric-delete default is. Delete is
    one-sided by default now, so a plain remove_friend leaves the peer's row --
    tests that just want a clean slate (not to exercise delete semantics) use
    this so they do not depend on the default."""
    A.remove_friend(db3, AH, BN)
    A.remove_friend(db3, BH, AN)


def befriend():
    """A and B, mutual. The accept is two steps -- see the block above.
    Starts from a clean slate so a leftover one-sided row cannot skew it."""
    wipe()
    A.request_friend(db3, AH, BN)                   # A asks
    A.request_friend(db3, BH, AN)                   # B accepts
    A.request_friend(db3, AH, BN)                   # A's client confirms


def rows(h):
    return [(r["peer_name"], r["status"])
            for r in A.list_friends(db3, h, status=None)]


befriend()
check("they start out mutual friends",
      [rows(AH), rows(BH)], [[(BN, A.STATUS_ACTIVE)], [(AN, A.STATUS_ACTIVE)]])

# DEFAULT IS ONE-SIDED now (matches SE, wire-confirmed 2026-08-19): deleting
# removes only MY row; the peer keeps theirs (offline, presence stops).
A.remove_friend(db3, AH, BN)
check("deleting a friend is one-sided by default -- only my row goes",
      [rows(AH), rows(BH)], [[], [(AN, A.STATUS_ACTIVE)]])

# The knob restores the symmetric divergence (delete removes both sides at once).
os.environ["POL_FRIEND_DELETE_SYMMETRIC"] = "1"
befriend()
A.remove_friend(db3, AH, BN)
check("POL_FRIEND_DELETE_SYMMETRIC=1 removes BOTH sides",
      [rows(AH), rows(BH)], [[], []])
del os.environ["POL_FRIEND_DELETE_SYMMETRIC"]

# A ONE-SIDED leftover -- B holds A, A holds nobody -- is now simply the default
# delete. Re-friending must not park on a request the peer has no reason to
# answer: they already have you.
befriend()
A.remove_friend(db3, AH, BN)                        # default one-sided: B keeps A
check("re-friending someone who still holds you agrees instead of asking",
      A.request_friend(db3, AH, BN), "accepted")
check("so both sides read active", [rows(AH), rows(BH)],
      [[(BN, A.STATUS_ACTIVE)], [(AN, A.STATUS_ACTIVE)]])

# THE LOST INVITATION: A asked, and B's copy went missing afterwards.
# `mirror=False` builds that deliberately.
wipe()                                              # clean slate, both sides
A.request_friend(db3, AH, BN)                       # A asks: A pending, B invited
A.remove_friend(db3, BH, AN, mirror=False)          # B's copy vanishes alone
check("A is left waiting on a request B no longer has",
      [rows(AH), rows(BH)], [[(BN, A.STATUS_PENDING)], []])
check("re-sending it rebuilds the invitation on B's side",
      A.request_friend(db3, AH, BN), "requested")
check("and B can see it now", rows(BH), [(AN, A.STATUS_INVITED)])

# ...but only while the asker is WAITING. An ACTIVE row plus a missing peer row
# must not be re-invited off the back of an unrelated write -- a rename is also
# a 2:6 naming them.
A.request_friend(db3, BH, AN)                       # B accepts
A.request_friend(db3, AH, BN)                       # A confirms
A.remove_friend(db3, BH, AN, mirror=False)          # B's row goes, A's stays
check("A holds B active while B has dropped A",
      [rows(AH), rows(BH)], [[(BN, A.STATUS_ACTIVE)], []])
check("a later write from A does NOT re-invite them",
      A.request_friend(db3, AH, BN), "exists")
check("so B stays deleted", rows(BH), [])

# The whole-list entry point has to reach all of that -- a repeat used to be
# skipped before `request_friend` was ever called.
wipe()
A.request_friend(db3, AH, BN)
A.remove_friend(db3, BH, AN, mirror=False)
add, acc, upd, rem, kept, kgrp = A.replace_friends(
    db3, AH, [dict(name=BN, guid=0, kind=A.KIND_FRIEND)])
check("replace_friends repairs a repeat instead of counting it as nothing",
      (add, acc), (1, 0))
check("and the invitation is back on B's side",
      rows(BH), [(AN, A.STATUS_INVITED)])

# RECONCILE IS THE BACKSTOP FOR A ROW THE ACCEPT DID NOT MOVE, and since
# 2026-08-16 the accept moves both, so the state it repairs can no longer be
# reached by accepting. It is NOT dead code -- it still covers rows written
# before that change and any acceptance that landed while the asker was offline
# -- so this drives it through `POL_FRIEND_ACCEPT_BOTH=0`, which is the one
# switch that still produces its input.
#
# THE ACCEPT'S SECOND STEP MUST NOT DEPEND ON A NOTIFICATION ARRIVING.
# Measured 2026-08-16: DeckTester accepted LaptopTest2's request, the server does
# not mint the acceptance mail by default, no client posted one either, and the
# asker sat on "Awaiting confirmation" with nothing left that could change it.
os.environ["POL_FRIEND_ACCEPT_BOTH"] = "0"
try:
    wipe()
    A.request_friend(db3, AH, BN)                       # A asks
    A.request_friend(db3, BH, AN)                       # B accepts; A stays pending
    check("the asker is still pending, as the two-step accept intends",
          [rows(AH), rows(BH)],
          [[(BN, A.STATUS_PENDING)], [(AN, A.STATUS_ACTIVE)]])
    check("reconcile closes the request they already accepted",
          A.reconcile_pending(db3, AH), [BN])
    check("so the asker reads active without any message being delivered",
          rows(AH), [(BN, A.STATUS_ACTIVE)])
    check("and it is inert once there is nothing pending",
          A.reconcile_pending(db3, AH), [])

    # It must NOT fire while their acceptance notification is still UNREAD: the
    # client is about to close its own row, and promoting it first is what makes
    # the client offer to RESEND -- "not waiting for friend registration". The
    # caller passes those peers as `skip`; whether a message is outstanding is
    # per-row state, not configuration.
    wipe()
    A.request_friend(db3, AH, BN)
    A.request_friend(db3, BH, AN)
    check("an unread acceptance holds the row back",
          A.reconcile_pending(db3, AH, skip={BN}), [])
    check("so it stays pending while the client still has the news to read",
          rows(AH), [(BN, A.STATUS_PENDING)])
    check("and it closes once that message is no longer outstanding",
          A.reconcile_pending(db3, AH), [BN])
    wipe()
    A.request_friend(db3, AH, BN)
    check("a pending row with no accepter stays pending either way",
          (A.reconcile_pending(db3, AH), rows(AH)),
          ([], [(BN, A.STATUS_PENDING)]))
finally:
    os.environ.pop("POL_FRIEND_ACCEPT_BOTH", None)

# ...and under the DEFAULT there is nothing left for it to reconcile, because
# the accept already moved the asker. That is the assertion that would catch the
# accept quietly reverting to one-sided.
wipe()
A.request_friend(db3, AH, BN)
A.request_friend(db3, BH, AN)
check("the default accept leaves reconcile with nothing to do",
      (rows(AH), A.reconcile_pending(db3, AH)),
      ([(BN, A.STATUS_ACTIVE)], []))
wipe()
A.request_friend(db3, AH, BN)
check("a pending row with no accepter stays pending either way",
      (A.reconcile_pending(db3, AH), rows(AH)),
      ([], [(BN, A.STATUS_PENDING)]))

# DECLINING IS THE ONE REMOVAL THAT STAYS ONE-SIDED. Refusing a request must not
# un-ask it: SE has a distinct "declined" message type and the asker is meant to
# be told, so their outgoing row has to survive.
wipe()                                              # clean slate, both sides
A.request_friend(db3, AH, BN)                       # A asks: A pending, B invited
check("declining drops only my row", A.decline_friend(db3, BH, AN), True)
check("the asker's row survives the decline",
      [rows(AH), rows(BH)], [[(BN, A.STATUS_PENDING)], []])

# THE MAILBOX LOOKUP THAT FEEDS `skip` MUST SURVIVE A NON-EMPTY MAILBOX.
# `_mailbox` yields (when, path, meta); its docstring said (when, path), and a
# caller that believed it raised -- inside `_db_friends`' broad except, which
# turned the whole friend list into "DB unavailable" and served ZERO friends to
# anyone who had mail. An EMPTY mailbox never trips it, which is exactly why it
# survived the suite, so this drives it with real messages stored.
mdir = tempfile.mkdtemp(prefix="fpskip-")
R.RESOURCE_DIR = mdir
os.environ["POL_ACCOUNTS_DB"] = os.path.join(mdir, "accounts.db")
mdb = A.connect(os.environ["POL_ACCOUNTS_DB"])
mrow = A.primary_handle_row(mdb, A.ensure_member(mdb, "MAILBX1")["id"])
MHID, MMID = int(mrow["id"]), int(mrow["member_id"])
mdb.close()
R._mail_mint("Someone", A.handle_guid(99), A.handle_guid(MHID),
             "Friend registration accepted", "Someone accepted.",
             kind=R.MAIL_KIND_FRIEND_ACCEPTED)
R._mail_mint("Chatty", A.handle_guid(99), A.handle_guid(MHID),
             "Hello", "an ordinary message", kind=R.MAIL_KIND_MESSAGE)
box = R._mailbox(MMID)
check("the mailbox has both messages", len(box), 2)
check("and its rows are 3-tuples (when, path, meta), not 2",
      sorted({len(r) for r in box}), [3])
check("only the acceptance counts as an unread notice",
      R._unread_notice_senders(MMID, R.MAIL_KIND_FRIEND_ACCEPTED), {"Someone"})
check("a kind nobody sent comes back empty",
      R._unread_notice_senders(MMID, R.MAIL_KIND_GROUP_INVITE), set())

print("\na dead acceptance retires itself at listing time (the 18161 guard)")
# The 2026-08-18T13:31 live report: an acceptance adopted from an older build
# was read by a member holding NO friend row for its sender, so the client's
# (guid, slot) lookup missed both arrays and raised string 18161 -- the "resend
# the request?" prompt -- re-asking a settled friendship. Listing is the one
# gate every copy passes (client 3:1, our mint, the adopt path), so the filter
# lives in `_mailbox` and the proof drives that, not the predicate directly.
mdb = A.connect(os.environ["POL_ACCOUNTS_DB"])
srow = A.primary_handle_row(mdb, A.ensure_member(mdb, "STALEACC")["id"])
SHID, SNAME = int(srow["id"]), srow["handle_name"]


def _senders():
    return {m["sender"] for _, _, m in R._mailbox(MMID)}


R._mail_mint(SNAME, A.handle_guid(SHID), A.handle_guid(MHID),
             "Friend registration accepted", "",
             kind=R.MAIL_KIND_FRIEND_ACCEPTED)
os.environ["POL_MAIL_STALE_ACCEPT"] = "0"
check("POL_MAIL_STALE_ACCEPT=0 leaves even a dead acceptance listed",
      SNAME in _senders(), True)
os.environ["POL_MAIL_STALE_ACCEPT"] = "1"
check("a resolvable sender the reader holds NO row for is retired",
      SNAME in _senders(), False)
check("the bytes survive under .stale, distinct from a 3:2's .read",
      any(n.endswith(".stale") for n in os.listdir(mdir)), True)
check("an unresolvable sender is never guessed at -- 'Someone' stays",
      "Someone" in _senders(), True)
# The real flow must be untouched: a reader PENDING for the sender is exactly
# who the acceptance exists for...
A.request_friend(mdb, MHID, SNAME)
R._mail_mint(SNAME, A.handle_guid(SHID), A.handle_guid(MHID),
             "Friend registration accepted", "",
             kind=R.MAIL_KIND_FRIEND_ACCEPTED)
check("with the reader pending on the sender, the acceptance stays",
      SNAME in _senders(), True)
# ...and so is a reader already ACTIVE: POL_FRIEND_ACCEPT_BOTH flips the
# asker's row at accept time, BEFORE their client has read the mail, so active
# + unread is the normal path since 2026-08-16, not a stale one.
A.set_friend_status(mdb, MHID, SNAME, A.STATUS_ACTIVE)
check("with the reader already active, it still stays", SNAME in _senders(), True)
mdb.close()
del os.environ["POL_MAIL_STALE_ACCEPT"]
del os.environ["POL_ACCOUNTS_DB"]

print("\nthe 2:6 reply carries the peer's real guid (the search-add fix)")
# A search-based add sends guid 0 in the client's own 2:6, so the friend enters
# the match array anonymous and the acceptance misses (string 18161). The reply
# is where we correct it: polcore's 2:6-reply parser stores record+0x10 into the
# friend table. Build a minimal request whose echoed record names 'CredibleAsh',
# arm the name->guid map, and check the reply carries the guid at payload +0x18
# -- the offset SE's own reply uses (frame 0x30 = payload +0x18).
GUID = A.handle_guid(5)
req = bytearray(0x208)
nm = b"CredibleAsh\x00"
req[0x170:0x170 + len(nm)] = nm             # echoed record +0x18 = reply +0x20
req[0x158] = 0x21                            # a plausible head low byte
R._FRIEND_PUT_GUIDS.clear()
R._FRIEND_PUT_GUIDS["CredibleAsh"] = GUID
del R._FRIEND_PUT_ASSIGNED[:]
os.environ["POL_FRIEND_PUT_GUID"] = "1"
rep = R._friend_put_reply(0xB8, bytes(req))
got = struct.unpack_from("<Q", rep, 0x18)[0]
check("the reply's +0x18 guid is the peer's real served guid", got, GUID)
check("...and the record name is still 'CredibleAsh'",
      bytes(rep[0x20:0x2B]), b"CredibleAsh")
os.environ["POL_FRIEND_PUT_GUID"] = "0"
rep0 = R._friend_put_reply(0xB8, bytes(req))
check("POL_FRIEND_PUT_GUID=0 restores the echoed (guid-0) record",
      struct.unpack_from("<Q", rep0, 0x18)[0], 0)
del os.environ["POL_FRIEND_PUT_GUID"]
R._FRIEND_PUT_GUIDS.clear()

print("\nan incoming request is a MESSAGE, not a friend-list row")

R.RESOURCE_DIR = tempfile.mkdtemp(prefix="fpmail-")
path = R._mail_mint("Fox", A.handle_guid(3), A.handle_guid(0x10),
                    "Let's be friend", "Fox would like to add you as a friend.",
                    kind=R.MAIL_KIND_FRIEND_REQUEST)
meta = R._mail_meta(path)
check("the minted path decodes back to its recipient",
      meta["recipient_guid"], A.handle_guid(0x10))
check("the sender's handle name survives", meta["sender"], "Fox")
check("the kind marks it a friend request",
      (meta["kind"], meta["notify"]), (R.MAIL_KIND_FRIEND_REQUEST, 0x10))
check("the record declares the object's real length",
      meta["size"], len(R._resource_blob(path, meta["size"])))
check("and the object reads back as <subject>\\x07<body>\\x00",
      R._resource_blob(path, meta["size"]),
      b"Let's be friend\x07Fox would like to add you as a friend.\x00")
check("the token is 96 chars, as SE's are", len(path) - 4, 96)

print("%s (%d failure%s)" % ("friend write intact" if not fails else "FAILED",
                             len(fails), "" if len(fails) == 1 else "s"))
sys.exit(1 if fails else 0)
