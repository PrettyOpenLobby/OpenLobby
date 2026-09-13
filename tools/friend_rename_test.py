"""Renaming a friend must RENAME them -- it used to delete them.

Every frame below is SE's own, lifted byte-for-byte out of the retail capture of
2026-08-19 (`grouplife.txt`, the six `2:6 KPutFriendList` writes at capture lines
25676-263945, decoded with the auth-band keystream-reuse method). The account
holder renamed `Cyn` to "Cool friend :3", renamed them back, ignored them,
changed the ignore's scope, un-ignored them, and deleted a different friend --
so this file replays a real editing session through our real parser.

THE BUG IT PINS. A rename is not a new
opcode: the client writes the caption into the same 16-byte field the NAME
occupies. `check_handle_policy` rejects "Cool friend :3" for its spaces, so
`_friend_put_name` answered None, so `_friend_put_deletes` claimed the record --
and a delete's target is the record's +0x04 slot, which on a rename is the
renamed friend's own slot. **Renaming a friend deleted them.**

AND THE ONE IT MUST NOT BREAK. The discriminator between a rename and a delete
has to be exact, because both arrive as "a record with no acceptable handle name
in it". SE's deletes carry stale heap in that field -- `66 76 47 5b f9 d5 cb 95
bb 88 0e e2 69 53 33 a3`, sixteen bytes with no terminator -- and a real string
is always NUL-terminated. That, plus "the guid resolves to somebody we hold", is
what separates them, and both deletes below are here to prove it still does.

A related invariant rides along: the ignore state is two flag bytes inside the SAME write (low
byte in front of the record, action byte at record +0x03) and the server's job is
to round-trip them, not to decode them. The four measured transitions are
replayed and read back.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="friend-rename-")
DB = os.path.join(TMP, "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_SEARCH_CALIB"] = os.path.join(TMP, "no-such.txt")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "2:3=friends")

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


# --------------------------------------------------------------------------- #
# SE's frames.
#
# Only the bytes the parser reads are transcribed: the grid runs from 0x15C and
# the fields that matter all sit in 0x150..0x180 (plus a second record at 0x200
# in the one two-record write). Everything else in a real frame is the client's
# 0x00..0xC7 preamble ramp and the empty-slot markers, neither of which any
# reader touches -- but the LENGTH is transcribed exactly, because the record
# count is derived from it and nothing else.
def frame(total, **blobs):
    """A 2:6 frame `total` bytes long with SE's bytes spliced in at `0xNNN=hex`."""
    buf = bytearray(total)
    for at, hx in blobs.items():
        off = int(at[1:], 16)
        raw = bytes.fromhex(hx)
        buf[off:off + len(raw)] = raw
    return bytes(buf)


#: RENAME. `Cyn` -> "Cool friend :3". state low 0x21 (normal), flag 0x00.
RENAME = frame(0x208, x150=(
    "606162630100965821a013ea0400000000000a00"
    "0000000091bc041e2c008c00436f6f6c20667269656e64203a3300a3"))
#: RENAME BACK. The same slot, the handle's own name written over the caption.
RENAME_BACK = frame(0x208, x150=(
    "60616263010039c921a013ea0400000000000a00"
    "0000000091bc041e2c008c0043796e00f9d5cb95bb880ee2695333a3"))
#: IGNORE ADD -- a TWO-record write. Record 0 is a delete (heap in the name
#: field); record 1 is Cyn with state low 0x31 and flag 0x34.
IGNORE_ADD = frame(0x2B0, x150=(
    "6061626302009658e2c7e11908616e4100d70a00"
    "00000000b3531a0eaeed2c436676475bf9d5cb95bb880ee2695333a3"),
    x200=("3100000000000034000044000000000091bc041e2c008c00"
          "43796e00d595cf2d6c3f445462dc4c59"))
#: SCOPE CHANGE -- ignore matched on the PlayOnline id instead. low 0x51.
IGNORE_SCOPE = frame(0x208, x150=(
    "606162630100000051a013ea0400000000000a00"
    "0000000091bc041e2c008c0043796e00f9d5cb95bb880ee2695333a3"))
#: UN-IGNORE. low back to 0x21, flag 0x40.
IGNORE_DEL = frame(0x208, x150=(
    "606162630100000021a013ea0400004000000a00"
    "0000000091bc041e2c008c0043796e00f9d5cb95bb880ee2695333a3"))
#: A REAL DELETE, of a different friend. The name field is stale heap.
DELETE = frame(0x208, x150=(
    "6061626301002222e2c7e11908616e4102d70a00"
    "00000000b3531a0eaeed2c436676475bf9d5cb95bb880ee2695333a3"))


def records(pt):
    return R._friend_put_records(pt)[1]


def main():
    print("the record fields, straight off SE's bytes ->")
    rec = records(RENAME)[0]
    check(R._friend_put_text(rec) == "Cool friend :3",
          "the rename's text field reads as a LABEL",
          repr(R._friend_put_text(rec)))
    check(R._friend_put_name(rec) is None,
          "...and NOT as a handle name -- which is why it used to be a delete")
    check(R._friend_put_guid(rec) == 0x008C002C1E04BC91,
          "the stable guid is at +0x0C",
          f"{R._friend_put_guid(rec):#018x}")
    check(R._friend_put_state_low(RENAME) == 0x21, "state low byte = 0x21 (normal)")
    check(R._friend_put_flag(rec) == 0x00, "action flag = 0x00")

    drec = records(DELETE)[0]
    check(R._friend_put_text(drec) is None,
          "a DELETE's name field is heap, not text -- no NUL terminator",
          repr(bytes(drec[0x14:0x24]).hex()))
    check(R._friend_put_name(drec) is None, "and it is not a handle name either")

    two = records(IGNORE_ADD)
    check(len(two) == 2, "the ignore-add write carries TWO records", str(len(two)))
    check(R._friend_put_state_low(IGNORE_ADD, 0) == 0xE2
          and R._friend_put_state_low(IGNORE_ADD, 1) == 0x31,
          "each record has its OWN state dword, 4 bytes in front of it",
          f"{R._friend_put_state_low(IGNORE_ADD, 0):#04x}, "
          f"{R._friend_put_state_low(IGNORE_ADD, 1):#04x}")
    check(R._friend_put_flag(two[1]) == 0x34, "record 1's action flag = 0x34 (add)")
    check(R._friend_put_guid(two[1]) == 0x008C002C1E04BC91,
          "record 1 is Cyn -- same guid as every other write for them")
    check(R._friend_put_slot(two[1]) is None,
          "record 1 fails the +0x06 live check (it reads 0x44), so ONLY the guid "
          "can place it")

    # --- the fixture -------------------------------------------------------- #
    # THE SLOTS ARE DELIBERATELY NOT SE'S. Their records name +0x04 = 0 for the
    # Cyn writes and the deletes, which in SE's own list was whoever sat there.
    # Ours puts **Wiccaan** in slot 0, Cyn in slot 1 and Yatih in slot 2 -- so a
    # resolver that fell back to the slot would rename and then ignore the WRONG
    # person, and only guid-first resolution gets Cyn. The two deletes still
    # land correctly because a delete has nothing BUT its slot.
    print("\nreplaying the session against a real store ->")
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "RENPOLID", "pw", area_kbn="00", login_pf="01")
    mid = accounts.add_member(conn, "RENPOLID", "renmember", "pw")
    accounts.set_handle(conn, mid, "Fox")
    hid = accounts.primary_handle_row(conn, mid)["id"]
    for nm, guid in (("Wiccaan", 0x00445566),
                     ("Cyn", 0x008C002C1E04BC91),
                     ("Yatih", 0x00112233)):
        accounts.add_friend(conn, hid, nm, kind=accounts.KIND_FRIEND, guid=guid)
    conn.close()
    R._session_member_id = lambda: mid
    R._session_handle_id = lambda db=None: hid

    def friends():
        db = accounts.connect(DB)
        try:
            return {r["peer_name"]: r for r in
                    accounts.list_friends(db, hid, status=None)}
        finally:
            db.close()

    def serve_23():
        """Serve a real 2:3 -- which is what publishes the slot map a later 2:6
        delete indexes. Returns the payload so the caller can read the wire."""
        n = R._list_count(0x02, 0x03)
        return R._list_payload(0x02, 0x03, R._list_paylen(0x02, 0x03, n))

    serve_23()
    slots = R._friend_slots_map(hid) or {}
    check({v[0] for v in slots.values()} == {"Wiccaan", "Cyn", "Yatih"}
          and slots.get(0, ("?",))[0] == "Wiccaan",
          "the fixture serves Wiccaan in slot 0, NOT Cyn",
          ", ".join(f"{k}={v[0]}" for k, v in sorted(slots.items())))

    R._capture_friend_put(RENAME)
    have = friends()
    check("Cyn" in have, "*** the renamed friend still EXISTS *** "
                         "(this is the whole bug)",
          ", ".join(sorted(have)))
    check((have.get("Cyn") or {})["label"] == "Cool friend :3"
          if "Cyn" in have else False,
          "and the caption is stored against their row",
          repr(have["Cyn"]["label"]) if "Cyn" in have else "row gone")
    check("Cool friend :3" not in have,
          "the caption did NOT become a friend of its own")

    check("Wiccaan" in have,
          "and the person in the slot the record NAMED is untouched -- "
          "the guid placed it, not the slot")

    print("\n  ...and the client sees it ->")
    rows = R._db_friends(kinds=(accounts.KIND_FRIEND,))
    shown = {r[1]: r[6] for r in rows}
    check(shown.get("Cyn") == "Cool friend :3",
          "the 2:3 row carries the label while element 1 stays the real name",
          repr(shown))
    check(b"Cool friend :3" in serve_23(),
          "the caption is in the bytes that go on the wire")

    R._capture_friend_put(RENAME_BACK)
    have = friends()
    check(not have["Cyn"]["label"],
          "writing the real name back CLEARS the rename",
          repr(have["Cyn"]["label"]))

    print("\nthe ignore state round-trips ->")
    # The ignore-ADD write's FIRST record is a delete naming slot 0 -- Wiccaan
    # in our fixture -- so it is expected to go, and its going is part of the
    # replay rather than a side effect to work around.
    for label, pt, low, flag in (
            ("ignore ADD", IGNORE_ADD, 0x31, 0x34),
            ("scope -> PlayOnline id", IGNORE_SCOPE, 0x51, 0x00),
            ("un-ignore", IGNORE_DEL, 0x21, 0x40)):
        R._capture_friend_put(pt)
        row = friends()["Cyn"]
        check((row["ignore_low"], row["ignore_flag"]) == (low, flag),
              f"{label}: stored {low:#04x}/{flag:#04x}",
              f"got {row['ignore_low']}/{row['ignore_flag']}")
    check("Wiccaan" not in friends(),
          "the ignore-add write's OTHER record still deleted slot 0")

    print("\n  ...and the reply hands them straight back ->")
    # SE's own 2:6 reply is an ECHO of the request from 0x158 on, so the two flag
    # bytes come back in it untouched. That is what the client reads its own
    # state out of, and it is the half of A4 that was already right.
    reply = R._friend_put_reply(0xB8, IGNORE_SCOPE)
    check(reply[0x08] == 0x51, "the reply echoes the state low byte",
          f"{reply[0x08]:#04x}")
    check(reply[0x0F] == 0x00, "and the action flag with it",
          f"{reply[0x0F]:#04x}")

    print("\nan ADD is still an add ->")
    # SE's own add of `Examplemember` -- a perfectly good handle name in a
    # record whose +0x04 names slot 2 and whose guid we do not hold. If the
    # resolver fell through to the slot it would find whoever sits there and the
    # caller would read the write as "rename THEM to Examplemember". It has to
    # read as an add instead, which is why the slot arm is closed to records
    # that carry a real name.
    ADD = frame(0x208, x150=(
        "6061626301002222210000000000004002020a00"
        "0000000054dc2ce96201bd014578616d706c656d656d6265720033a3"))
    was = {n: r["label"] for n, r in friends().items()}
    R._capture_friend_put(ADD)
    now = friends()
    check("Examplemember" in now, "the add created a friend",
          ", ".join(sorted(now)))
    check(all(now[n]["label"] == was[n] for n in was if n in now),
          "and renamed nobody -- no existing row picked up a caption",
          repr({n: now[n]["label"] for n in was if n in now}))

    print("\na DELETE is still a delete ->")
    # SE's delete record names slot 2, which our fixture served as Yatih. The
    # record's guid field is heap, so nothing but the slot can place it -- which
    # is exactly the path a rename must never take.
    before = set(friends())
    R._capture_friend_put(DELETE)
    after = set(friends())
    check("Cyn" in after,
          "the delete did not touch the friend it does not name")
    check(before - after == {"Yatih"},
          "the friend the slot names is gone", f"{sorted(before)} -> {sorted(after)}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all rename/ignore checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
