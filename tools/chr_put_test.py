"""Lobby 1:10 -- the PS2 Viewer's character-list write-back (`sqprofdb: Update
chlist`) -- against a real temporary accounts DB. No client, no container.

WHAT THIS PINS

  1. `_char_record` serves a UNIQUE order byte at +0x01 (the slot index), which
     is what stops the console re-sending 1:10 on every list load; and
     POL_CHAR_ORDER=0 restores the old zero.
  2. 1:10 is tabled HEADER-ONLY (`_lobby_paylen` -> 0), not the 8-byte default.
  3. `_chr_put` MOVES a character to the handle the block names, through the
     same `link_content_to_handle` the sign-up flow uses, and the next 1:3
     serves it there.
  4. It REFUSES: a bad trailer (nothing applied), a short body, an unbind (kind
     2 -- logged, never deleted), a slot we never served, a handle slot we never
     served -- none of them raise and none of them touch the DB.
  5. The dispatch reaches it (`_lobby_payload(1, 0x0A, ...)`).

The body layout is the one read off `polpex_0014e8e0` on 2026-09-11 (memory
ps2-lobby-1-10-is-sqprofdb-update-chlist). WARNING: No real 1:10 frame has ever been
read; if a captured frame disagrees with this fixture, the fixture is the thing
to change.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="chr-put-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_LOBBY_LIST_MODE"] = "1:3=chars"
os.environ.pop("POL_CHAR_ORDER", None)
os.environ.pop("POL_CHR_PUT", None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


# --- fixture: one member, two handles, two titles on the primary ------------ #
conn = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
accounts.create_polid(conn, "CHRPOLID", "pw-polid", area_kbn="00", login_pf="01")
MID = accounts.add_member(conn, "CHRPOLID", "chrmember", "pw-member")
accounts.set_handle(conn, MID, "Primary")
accounts.set_handle(conn, MID, "Second", primary=False)
HID_A = accounts.primary_handle_row(conn, MID)["id"]
HID_B = conn.execute("SELECT id FROM handle WHERE handle_name = 'Second'").fetchone()["id"]
accounts.link_content_to_handle(conn, HID_A, 2, "1000000102")     # Tetra Master
accounts.link_content_to_handle(conn, HID_A, 3, "1000000103")     # Janhourou
conn.close()

R._session_member_id = lambda: MID
R._session_handle_id = lambda db=None: HID_A


def where(code):
    c = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    try:
        rows = c.execute("SELECT handle_id, slot, content_id FROM handle_content "
                         "WHERE content_code = ? ORDER BY slot", (code,)).fetchall()
        return [(int(r["handle_id"]), int(r["slot"]), r["content_id"]) for r in rows]
    finally:
        c.close()


chars = R._db_chars()
print("served characters:", chars)
check(len(chars) == 2 and chars[0][2] == 2 and chars[1][2] == 3,
      "fixture serves TM then Jan on handle slot 0")

# --- 1. the order byte ------------------------------------------------------ #
rec = R._char_record(bytes(0x68), 5, 0, 1, 2, "1000000102")
check(rec[0x00] == 5 and rec[0x01] == 5, "+0x01 is the slot index by default",
      f"+0x00={rec[0]} +0x01={rec[1]}")
os.environ["POL_CHAR_ORDER"] = "0"
rec0 = R._char_record(bytes(0x68), 5, 0, 1, 2, "1000000102")
check(rec0[0x01] == 0, "POL_CHAR_ORDER=0 restores the zero")
os.environ.pop("POL_CHAR_ORDER")

# --- 2. tabled header-only -------------------------------------------------- #
check(R._LOBBY_PAYLEN.get((1, 0x0A)) == 0, "(1,10) is in _LOBBY_PAYLEN as 0")
check(R._lobby_paylen(1, 0x0A) == 0, "_lobby_paylen(1,10) == 0")


# --- the body builder, exactly the console's shape -------------------------- #
def body(blocks, order=None, bad_trailer=False, short=False):
    b = bytearray(R._CHR_PUT_BODY)
    for i in range(R._CHR_PUT_SLOTS):
        b[i] = (order[i] if order and i < len(order) else i) & 0x3F
    for i, kind, hslot, hpos in blocks:
        off = R._CHR_PUT_BLOCKS_OFF + i * 8
        b[off] = kind
        b[off + 1] = hslot
        b[off + 2] = hpos
    ck = R._lobby_cksum(bytes(b[:R._CHR_PUT_CKSUM_OFF]))
    if bad_trailer:
        ck ^= 0x5A5A5A5A
    b[R._CHR_PUT_CKSUM_OFF:R._CHR_PUT_CKSUM_OFF + 4] = ck.to_bytes(4, "little")
    if short:
        b = b[:100]
    return bytes(R._CHR_PUT_PAYLOAD_OFF) + bytes(b)


# --- 4a. refusals first, so the fixture is untouched when we test the move --- #
before = (where(2), where(3))
r = R._chr_put(body([(1, 1, 1, 0)], bad_trailer=True))
check(r == b"" and (where(2), where(3)) == before, "bad trailer: nothing applied")
r = R._chr_put(body([(1, 1, 1, 0)], short=True))
check(r == b"" and (where(2), where(3)) == before, "short body: nothing applied")
r = R._chr_put(body([(1, 2, 1, 0)]))
check(r == b"" and (where(2), where(3)) == before, "kind 2 (unbind): logged, not applied")
r = R._chr_put(body([(7, 1, 1, 0)]))
check(r == b"" and (where(2), where(3)) == before, "a slot we never served: skipped")
r = R._chr_put(body([(1, 1, 9, 0)]))
check(r == b"" and (where(2), where(3)) == before, "a handle slot we never served: skipped")
r = R._chr_put(body([(1, 1, 0, 1)]))
check(r == b"" and (where(2), where(3)) == before, "same handle slot: no-op")
r = R._chr_put(None)
check(r == b"", "None request: b''")

# --- 3. the move ------------------------------------------------------------ #
r = R._chr_put(body([(1, 1, 1, 0)], order=[1, 0]))
jan = where(3)
check(r == b"" and len(jan) == 1 and jan[0][0] == HID_B and jan[0][2] == "1000000103",
      "kind 1 to handle slot 1 MOVES Janhourou to 'Second', id kept", str(jan))
check(where(2) == before[0], "Tetra Master stayed on 'Primary'")
after = R._db_chars()
check(any(c[0] == 1 and c[2] == 3 for c in after),
      "the next 1:3 serves Janhourou on handle slot 1", str(after))

# --- 5. the dispatch --------------------------------------------------------- #
r = R._lobby_payload(1, 0x0A, 0, body([(1, 0, 0, 0)]))
check(r == b"", "_lobby_payload(1,10) returns the header-only b''", repr(r)[:40])
tm = where(2)
check(len(tm) == 1 and tm[0][0] == HID_A, "dispatch path applied a no-op cleanly")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("chr_put_test: all checks passed")
