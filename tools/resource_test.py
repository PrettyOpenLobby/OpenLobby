#!/usr/bin/env python3
"""Round-trip test for the 03:02 object write / 03:00 read pair.

The bug: `_resource_store` sat in the tree with ZERO callers. The client wrote an
object, we acknowledged and dropped it, and the read back was answered with 664
zero bytes -- which is what made a delivered Message render with no subject and
an empty body (measured live with two accounts, 2026-08-15).

This asserts the pair actually round-trips, using the LAYOUT measured from that
day's decrypted 448-byte write:

    +0x00  02 03 02 00 <u32 len>     header
    +0x38  "O/m/<...>\\0"             path (same offset 03:00 reads)
    +0x9C  content

    python tools/resource_test.py        # exit 0 on success
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

os.environ.setdefault("POL_LOG_DIR", tempfile.mkdtemp(prefix="restest-log-"))
# The storage checks below address objects by the path the client sent, so the
# sender-field rewrite is off for them and tested on its own at the bottom.
os.environ["POL_MAIL_NORMALISE"] = "0"
import responders as R                                          # noqa: E402

fails = []


def check(what, got, want):
    if got == want:
        print("  ok    %s" % what)
        return
    fails.append(what)
    print("  FAIL  %s\n          got  %r\n          want %r" % (what, got, want))


# The real path from the capture -- 99 chars, base64-ish, NUL-terminated.
SE_PATH = ("O/m/O9cH2defHpt1Isrw9zswWI9rsfMe2Zkd2Z3TTTTTTTSL2b2jym1r"
           "TTTTTTTTTTTTTTTTTnleAci3TTTTT7TTAOA8TATTTTTT")
CONTENT = bytes(range(1, 251)) + b"\xde\xad\xbe\xef"       # 254B, high-entropy-ish


def make_write(path, content, op=0x01):
    """A 03:01 frame in SE's measured layout: the path in its fixed 0x184 field,
    then `[u32 objlen][object][u32 checksum]` at the tail (`_MAIL_OBJ_OFF`)."""
    pt = bytearray(b"\x02\x03" + bytes([op]) + b"\x00")
    pt += b"\x00" * (R._MAIL_OBJ_OFF - 4)
    pt[R._FETCH_PATH_OFF:R._FETCH_PATH_OFF + len(path) + 1] = \
        path.encode("cp932") + b"\x00"
    pt += struct.pack("<I", len(content)) + content + b"\xde\xad\xbe\xef"
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)     # declared body length
    return bytes(pt)


def make_read(path, want):
    """A 03:00 frame: same fixed path field, and the length the reader expects."""
    pt = bytearray(b"\x02\x03\x00\x00")
    pt += b"\x00" * (R._MAIL_OBJ_OFF - 4)
    pt[R._FETCH_PATH_OFF:R._FETCH_PATH_OFF + len(path) + 1] = \
        path.encode("cp932") + b"\x00"
    pt += struct.pack("<I", want) + b"\x00" * 4 + b"\xde\xad\xbe\xef"
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)
    return bytes(pt)


R.RESOURCE_DIR = tempfile.mkdtemp(prefix="restest-")
print("03:01 write -> 03:00 read round trip")

pt = make_write(SE_PATH, CONTENT)
check("path parses back out of the frame", R._fetch_path(pt), SE_PATH)
check("the object block is found at SE's fixed offset", R._mail_object(pt), CONTENT)

R._capture_resource_write(pt)
# The read side already existed; this is the half that was never fed.
blob = R._resource_blob(SE_PATH, len(CONTENT) + 4)
check("content survives verbatim", blob[:len(CONTENT)], CONTENT)

# LENGTH. SE answers a 30-byte object with a 34-byte payload and never pads;
# padding it to this opcode's 664 default is what raised POL-5135, because the
# reader checks the trailer at the length it asked for.
check("a message is served at its own length + trailer",
      R._lobby_paylen(0x03, 0x00, make_read(SE_PATH, len(CONTENT))),
      len(CONTENT) + 4)
# The account record keeps its own measured length -- 668, from SE's live fetch
# on 2026-08-15 (0x29C: polcore's 0x298 allocation plus the 4-byte checksum on
# top). The point of the check is that the mail branch above did not capture it,
# not the constant itself, so it reads the table rather than restating it.
check("and the account record is untouched by that",
      R._lobby_paylen(0x03, 0x00, make_read("u/account", 0)),
      R._FETCH_PATHLEN["u/account"])

# An unrelated path must still read as "nothing stored" -- each message is its
# own object, and the capture showed the write and read paths genuinely differ.
other = R._resource_blob(SE_PATH.replace("O9cH", "ZZZZ"), 664)
check("a different path is still empty", other, b"\x00" * 664)

# SCOPE. A game save must NOT be captured by this: storing a wrong guess back to
# Janhourou would break it worse than storing nothing.
#
# NB the comparison is against the path's UNTOUCHED reading, not against zeros --
# `_resource_blob` serves a measured init header (the 0x02030100 magic) for known
# game paths, which is correct and long-standing. "Not stored" means the read is
# unchanged by the write, not that it is empty.
before = R._resource_blob("U/g/MJSUserData", 16)
R._capture_resource_write(make_write("U/g/MJSUserData", b"\x01\x02\x03\x04"))
check("game save read is unchanged by a write",
      R._resource_blob("U/g/MJSUserData", 16), before)
check("game save did not take our bytes",
      R._resource_blob("U/g/MJSUserData", 16).startswith(b"\x01\x02\x03\x04"), False)

# Malformed frames must not raise or store.
R._capture_resource_write(make_write("nopath", b"x"))
R._capture_resource_write(bytes(b"\x02\x03\x02\x00" + b"\x00" * 8))
check("malformed frames are inert", True, True)

# A 03:02 is SHORTER than one object block -- SE's hold nothing but heap junk in
# that region. It is the client saying it has TAKEN the message, and SE retires
# it: in SE's capture four messages are read-then-3:2'd and the later 3:3 lists
# one message, none of the four. Keeping them is what made an already-read
# message arrive unread again on the next login.
short = bytearray(make_write(SE_PATH, CONTENT, op=0x02))
del short[R._MAIL_OBJ_OFF:]                          # no object block at all

os.environ["POL_MAIL_RETIRE"] = "0"
R._capture_resource_write(bytes(short), op="3:2")
check("with retire off, a 3:2 leaves the message alone",
      R._resource_blob(SE_PATH, len(CONTENT)), CONTENT)

os.environ["POL_MAIL_RETIRE"] = "1"
R._capture_resource_write(bytes(short), op="3:2")
check("a 3:2 retires the message from the mailbox",
      os.path.exists(R._resource_file(SE_PATH)), False)
check("and keeps the bytes beside it, undo-able",
      open(R._resource_file(SE_PATH) + ".read", "rb").read(), CONTENT)
# Retiring twice is a no-op, not a crash -- the client re-sends on a re-login.
R._capture_resource_write(bytes(short), op="3:2")
check("retiring an already-retired message is inert", True, True)

# --- THE MAILBOX HALF: one message, one name, whoever is holding the socket ---
#
# What this pins is the 2026-08-16 failure: the message was filed under the
# SENDER's member id, the recipient's client read under its own, and all four
# 3:0 body fetches came back zero -- an inbox of empty rows, which reads as an
# empty inbox. See `_resource_file`.
print("\nmessage objects are filed by path, not by member")

MAIL = ("O/m/qNkv0tNXHpttIsrw9zswWI9rsfMe2Zkd2Z3TTTTTTTSjBcpjBcpj"
        "TTTTTTTTTTTTTTTTT3JkAmiHTTTTTTTTAOA8TATTTTTT")
BODY = b"the sender's real message" + bytes(range(200))

check("a message name is reversible", R._mail_path_of(R._mail_name(MAIL)), MAIL)
AT = MAIL.replace("SjBcpjBcpj", "SbEmk@TTTT")          # `@` IS in POL's base64
check("an `@` in the token survives the round trip",
      R._mail_path_of(R._mail_name(AT)), AT)
check("and it is not left in the filename", "@" in R._mail_name(AT), False)

# Written by member 7's session under the old scheme, read by member 1's client.
legacy = os.path.join(R.RESOURCE_DIR, "7.%s.bin" %
                      __import__("re").sub(r"[^A-Za-z0-9._-]", "_", MAIL))
with open(legacy, "wb") as f:
    f.write(BODY)
check("a message an older build filed under the sender still reads back",
      R._resource_blob(MAIL, 664)[:len(BODY)], BODY)
check("and it is adopted, so it is stored under exactly one name",
      (os.path.exists(legacy), os.path.exists(R._resource_file(MAIL))),
      (False, True))

# The recipient's own client answers a read with a write of its own. That echo
# must never land on the author's bytes.
_owner, _sess = R._mail_owner, R._session_get
R._mail_owner = lambda path: 1
R._session_get = lambda field: 1 if field == "member_id" else _sess(field)
try:
    R._capture_resource_write(make_write(MAIL, b"the reader's echo"))
    check("a reader's write-back does not overwrite the message",
          R._resource_blob(MAIL, 664)[:len(BODY)], BODY)
    check("but it is kept beside it",
          open(R._resource_file(MAIL) + ".readback", "rb").read(),
          b"the reader's echo")
    # The author writing again is a normal update and must still land.
    R._mail_owner = lambda path: 7
    R._capture_resource_write(make_write(MAIL, b"the author's second draft"))
    check("the author's own write still lands",
          R._resource_blob(MAIL, 32)[:25], b"the author's second draft")
finally:
    R._mail_owner, R._session_get = _owner, _sess

# --- BOTH IDENTITY FIELDS, which is what "Unknown User" in a header is --------
#
# A client-written record names two people in two different vocabularies: +0x00
# the SENDER, in the id that client knows itself by, and +0x08 the RECIPIENT, in
# OUR guid (that is what the address book gave the composer). The reader resolves
# each against what IT knows, so they have to be swapped in opposite directions:
# the sender into our guid, the recipient into its own.
print("\nboth identity fields are rewritten into the reader's vocabulary")

os.environ["POL_MAIL_NORMALISE"] = "1"
import accounts as A                                            # noqa: E402

# A DB of our own, so the check does not depend on whatever the live one holds.
os.environ["POL_ACCOUNTS_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="restest-db-"), "accounts.db")
_db = A.connect(os.environ["POL_ACCOUNTS_DB"])
A.create_polid(_db, "UTESTSEND", "x")
A.create_polid(_db, "UTESTRECV", "y")
_sender = A.add_member(_db, "UTESTSEND", "sender", "x")
_recip = A.add_member(_db, "UTESTRECV", "recip", "y")
A.set_handle(_db, _sender, "Sender")
A.set_handle(_db, _recip, "Recipient")
SENDER_H = _db.execute("SELECT id FROM handle WHERE handle_name='Sender'").fetchone()["id"]
RECIP_H = _db.execute("SELECT id FROM handle WHERE handle_name='Recipient'").fetchone()["id"]
_db.close()

_struct = __import__("struct")


def addressed(to_guid, from_guid):
    """An `O/m/` path addressed TO `to_guid` and claiming to be FROM `from_guid`."""
    rec = bytearray(R._b64decode(MAIL[len(R._MAIL_PATH_PREFIX):]))
    _struct.pack_into("<Q", rec, 0x00, from_guid ^ R._PUSH_GUID_MASK)
    _struct.pack_into("<Q", rec, 0x08, to_guid ^ R._PUSH_GUID_MASK)
    return R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec))


CLIENT_SELF = 0x162E92CDC54           # the shape a real client writes (Fox's)
SENT = addressed(A.handle_guid(RECIP_H), CLIENT_SELF)

check("with no sender to attribute it to, the sender field is left as sent",
      _struct.unpack_from("<Q", R._b64decode(R._mail_normalise(SENT, None)[4:]), 0)[0]
      ^ R._PUSH_GUID_MASK, CLIENT_SELF)
os.environ["POL_MAIL_NORMALISE"] = "0"
check("and the knob really does turn it off", R._mail_normalise(SENT, SENDER_H), SENT)
os.environ["POL_MAIL_NORMALISE"] = "1"

fixed = R._mail_normalise(SENT, SENDER_H)
check("the sender field becomes that handle's guid",
      _struct.unpack_from("<Q", R._b64decode(fixed[4:]), 0)[0] ^ R._PUSH_GUID_MASK,
      A.handle_guid(SENDER_H))
check("and what the sender calls itself is now on record",
      A.handle_by_client_guid(A.connect(os.environ["POL_ACCOUNTS_DB"]),
                              CLIENT_SELF)["id"], SENDER_H)
# THE "To:" HALF. Until the recipient has named itself we cannot do better than
# our own guid -- and saying so is the point, because that IS "Unknown User".
check("a recipient who has never named itself is addressed by our guid",
      R._mail_meta(fixed)["recipient_guid"], A.handle_guid(RECIP_H))

RECIP_SELF = 0x860FB3E2A2
_db = A.connect(os.environ["POL_ACCOUNTS_DB"])
A.learn_client_guid(_db, RECIP_H, RECIP_SELF)
_db.close()
fixed = R._mail_normalise(SENT, SENDER_H)
check("once it has, the recipient field carries the id IT knows itself by",
      R._mail_meta(fixed)["recipient_guid"], RECIP_SELF)
check("and the message is still deliverable under that id",
      R._mail_owner(fixed), _recip)
check("the subject, the size and the kind are untouched",
      [R._mail_meta(fixed)[k] for k in ("subject", "size", "kind")],
      [R._mail_meta(SENT)[k] for k in ("subject", "size", "kind")])
check("a record already carrying both right ids is not rewritten",
      R._mail_normalise(fixed, SENDER_H), fixed)

# The same id, volunteered the earlier way: every client sends it in front of the
# path on a `u/account` fetch, which is how a recipient can be addressed by name
# before it has ever sent a message of its own.
_db = A.connect(os.environ["POL_ACCOUNTS_DB"])
_db.execute("UPDATE handle SET client_guid = NULL WHERE id = ?", (RECIP_H,))
_db.commit(); _db.close()
_sess = R._session_get
R._session_get = lambda field: RECIP_H if field == "handle_id" else _sess(field)
try:
    R._capture_self_guid(make_read("u/account", 664)[:R._FETCH_SUBJECT_OFF]
                         + _struct.pack("<Q", RECIP_SELF)
                         + make_read("u/account", 664)[R._FETCH_SUBJECT_OFF + 8:])
    check("a `u/account` fetch teaches us what the caller calls itself",
          A.handle_by_client_guid(A.connect(os.environ["POL_ACCOUNTS_DB"]),
                                  RECIP_SELF)["id"], RECIP_H)
    # An `O/m/` fetch carries the MESSAGE's recipient there, not the caller's own
    # id -- learning from that would file somebody else's guid under this handle.
    _db = A.connect(os.environ["POL_ACCOUNTS_DB"])
    _db.execute("UPDATE handle SET client_guid = NULL WHERE id = ?", (RECIP_H,))
    _db.commit(); _db.close()
    R._capture_self_guid(make_read(MAIL, 664)[:R._FETCH_SUBJECT_OFF]
                         + _struct.pack("<Q", 0x123456789)
                         + make_read(MAIL, 664)[R._FETCH_SUBJECT_OFF + 8:])
    check("but an `O/m/` fetch teaches us nothing",
          A.handle_by_client_guid(A.connect(os.environ["POL_ACCOUNTS_DB"]),
                                  0x123456789), None)
finally:
    R._session_get = _sess
os.environ["POL_MAIL_NORMALISE"] = "0"

# --- THE MAIL PUSH, checked against SE's own ---------------------------------
#
# A message arrives live on SE because SE pushes it: all 11 message reads in
# polshim-se.429364.log are preceded by a nick-targeted NOTICE carrying that
# message's token. The pushed record is the `O/m/` record with ONE byte changed.
print("\nthe push that makes a message arrive live")

SE_MAIL = ("O/m/iPfkQm5uPppyZkAKtRuZWek1BATTTTTTTTTTTTTTTTScYbrrBb7AYbCwyZkd"
           "YbITTTTTTov6AciQTTTTTTGTUOA8TUTTTTTT")
SE_PUSH = ("iPfkQm5uPppyZkAKtRuZWek1BATTTTTTTTTTTTTTTTScYbrrBb7AYbCwyZkd"
           "YbITTTTTTov6AciQTTTTTTGTUOA8TpTTTTTTwIhW")
token = R._mail_push_token(SE_MAIL)
check("our push record is SE's, byte for byte", token, SE_PUSH[:96])
check("only +0x42 differs from the path record",
      [i for i in range(72)
       if R._b64decode(SE_MAIL[4:])[i] != R._b64decode(token)[i]], [0x42])
check("and it is the 0x02 -> 0x03 SE sends", R._b64decode(token)[0x42], 0x03)
check("a path too short to be a record pushes nothing",
      R._mail_push_token("O/m/TTTT"), None)

# --- A TITLE'S EVENT RESOURCES: shipped, but INERT until an event is declared
#
# Tetra Master's `b/g/TM0Event{DataList,MemberList}` come with its title module
# (services/titles.py) and are served through `titles.resource_template`. A
# populated ranking with no event window is a phantom event, so the template is
# gated on the title's event window -- the SAME POL_TM_EVENT_START/END its
# countdown reads. Off by default = None = an all-zero empty list. With no
# title loaded (POL_TITLES unset) the template is None and the checks skip.
import titles                                                    # noqa: E402
print("\nthe event ranking ships but stays inert until an event is declared")

for _k in ("POL_TM_EVENT_START", "POL_TM_EVENT_END"):
    os.environ.pop(_k, None)
check("with no window declared, the event data list is not served",
      titles.resource_template("b/g/TM0EventDataList"), None)
check("with no window declared, the event member list is not served",
      titles.resource_template("b/g/TM0EventMemberList"), None)
check("an explicit -1/-1 no-event window is still inert",
      (os.environ.update({"POL_TM_EVENT_START": "-1", "POL_TM_EVENT_END": "-1"})
       or titles.resource_template("b/g/TM0EventDataList")), None)

os.environ["POL_TM_EVENT_START"] = "1"            # any non -1 declares the event
# the board's rows come from the title's configuration (nobody by default):
os.environ["POL_TM_EVENT_MEMBERS"] = "1000000002:MEMBER-B:90"
if titles.resource_template("b/g/TM0EventDataList") is None:
    print("[SKIP] event-window resource checks: the Tetra Master title module "
          "is not loaded in this run (POL_TITLES)")
    for _k in ("POL_TM_EVENT_START", "POL_TM_EVENT_END"):
        os.environ.pop(_k, None)
else:
    _data = titles.resource_template("b/g/TM0EventDataList")
    _memb = titles.resource_template("b/g/TM0EventMemberList")
    check("a declared window serves the data list at its measured length",
          _data is not None and len(_data), 4872)
    check("and the member list at its measured length",
          _memb is not None and len(_memb), 10248)
    # The client reads the member count at +0x04; a populated list is the whole
    # point -- an all-zero body here opens no shop.
    check("the member list actually carries a ranking (count > 0)",
          _memb is not None and struct.unpack_from("<i", _memb, 0x04)[0] > 0, True)
    # And it survives the full serve path (stored miss -> template -> the four
    # live-count patchers, none of which touch an event path).
    check("the full serve path returns the populated list, not zeros",
          R._resource_blob("b/g/TM0EventMemberList", 10248)[0x04:0x08],
          _memb[0x04:0x08])
    for _k in ("POL_TM_EVENT_START", "POL_TM_EVENT_END", "POL_TM_EVENT_MEMBERS"):
        os.environ.pop(_k, None)

print("%s (%d failure%s)" % ("round trip intact" if not fails else "FAILED",
                             len(fails), "" if len(fails) == 1 else "s"))
sys.exit(1 if fails else 0)
