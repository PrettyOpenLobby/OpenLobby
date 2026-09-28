#!/usr/bin/env python3
"""0:8 KPutHandleList read as 64 records, behind POL_HANDLE_STORE_LAYOUT=crystal.

    python tests/test_handle_store_layout.py

Order bytes at 0x28, then 64 records of 0x18 from 0x68 (mode, position, open
level, name at +8). Pinned: the head of a handle registration as our own lobby
log showed it (session bytes zeroed) parses as record 0 = store "Lex"; with the knob OFF the scavenger
behaves exactly as before (including taking a stale name out of a skipped
record, which is the phantom-handle bug); with it ON only mode-1 records are
stored and a mode-2 delete acts only with POL_HANDLE_STORE_DELETE=1.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="handlestore-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "accounts.db")
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ.pop("POL_HANDLE_STORE_LAYOUT", None)
os.environ.pop("POL_HANDLE_STORE_DELETE", None)

import accounts  # noqa: E402
import responders as R  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


#: The first 0x80 bytes of a logged 0:8 (the only part ever dumped), with the
#: session's bytes at 0x18..0x28 and the heap after the name zeroed.
LEX_HEAD = bytes.fromhex(
    "02000800480600000000000000000000"
    "00000000000000000000000000000000"
    "00000000000000000100020304050607"
    "08090a0b0c0d0e0f1011121314151617"
    "18191a1b1c1d1e1f2021222324252627"
    "28292a2b2c2d2e2f3031323334353637"
    "38393a3b3c3d3e3f0100020000000000"
    "4c657800000000000000000000000000")


def frame(records):
    """A 0x670-byte decrypted 0:8 with `records` = {index: (mode, pos, lvl, name)}."""
    pt = bytearray(LEX_HEAD[:0x68]) + bytearray(0x670 - 0x68)
    for i, (mode, pos, lvl, name) in records.items():
        at = 0x68 + i * 0x18
        pt[at:at + 3] = bytes([mode, pos, lvl])
        nm = name.encode("ascii") if isinstance(name, str) else name
        pt[at + 8:at + 8 + len(nm)] = nm
    return bytes(pt)


print("the real head")
chk("frame length", len(LEX_HEAD), 0x80)
chk("order bytes are the (swapped) ramp", LEX_HEAD[0x28:0x2C].hex(), "01000203")
recs = R._handle_store_records(LEX_HEAD + bytes(0x670 - 0x80))
chk("record 0 = store, position 0, open level 2, 'Lex'",
    [(i, m, p, lv, R._handle_store_text(nm)) for i, m, p, lv, nm in recs],
    [(0, 1, 0, 2, "Lex")])

db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
me = accounts.ensure_member(db, "Lex")
mid = int(me["id"])
accounts.set_handle(db, mid, "OldOne")
db.commit()
db.close()
R._session_member_id = lambda: mid


def handles():
    c = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    try:
        return sorted(r["handle_name"] for r in c.execute(
            "SELECT handle_name FROM handle WHERE member_id = ?", (mid,)))
    finally:
        c.close()


before = handles()
# record 0 store Lex, record 1 SKIPPED with stale "quare" in its name field
# (the 0x88 slot the scavenger reads), record 2 store NewOne, record 3 delete.
PT = frame({0: (1, 0, 2, b"Lex\x00\x90\x3e\x12\x46"),
            1: (0, 0, 0, "quare"),
            2: (1, 2, 3, "NewOne"),
            3: (2, 1, 3, "OldOne")})

print("knob OFF: the scavenger, unchanged")
R._lobby_capture(PT)
after_off = handles()
chk("took the stale name out of the skipped record (the old bug, unchanged)",
    "quare" in after_off, True)
chk("record 2's name sits off the 0x10 grid, so it was missed",
    "NewOne" in after_off, False)
chk("the delete was not acted on", "OldOne" in after_off, True)

print("knob ON")
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
accounts.delete_handle(db, mid, "quare")          # clean slate, tombstoned
db.execute("DELETE FROM deleted_handle WHERE handle_name = 'quare'")
db.commit()
db.close()
os.environ["POL_HANDLE_STORE_LAYOUT"] = "crystal"
R._lobby_capture(PT)
got = handles()
chk("mode-1 records stored", "NewOne" in got, True)
chk("the skipped record's stale name is not", "quare" in got, False)
chk("the delete is logged only without POL_HANDLE_STORE_DELETE",
    "OldOne" in got, True)
os.environ["POL_HANDLE_STORE_DELETE"] = "1"
R._lobby_capture(PT)
chk("POL_HANDLE_STORE_DELETE=1 deletes it", "OldOne" in handles(), False)
db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
chk("and tombstones it", bool(accounts.is_handle_deleted(db, mid, "OldOne")), True)
db.close()
chk("a delete naming another member's handle does nothing",
    R._handle_store_records(frame({5: (2, 0, 0, "Nobody")}))[0][1], 2)
R._lobby_capture(frame({5: (2, 0, 0, "Nobody")}))
chk("...and the list is untouched", sorted(handles()),
    sorted(h for h in got if h != "OldOne"))

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
