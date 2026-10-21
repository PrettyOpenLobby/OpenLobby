"""Blocking (the client's IGNORE table) and the 200-row friend list.

polcore keeps ignored members in a table of their own (100 entries beside the
200 friends) and carries both in the same 2:6 / 2:3: a row whose bitfield head
has bit 0x10 set is an ignore row, head bits 5-6 its scope (1 the handle, 2
the whole PlayOnline ID) and its +0x08 (our record +0x04) the ignore index
(static RE of polcore 0x37e39bc, 0x37df090, 0x37e36a0, 2026-10-08). Pinned:

  * blocking a friend (the two-record write: friend slot freed + ignore row)
    removes the friend row, stores the ignore entry, files NO friend request;
  * blocking a non-friend from a message (one ignore row) files nothing either;
  * the 2:3 serves the ignore rows behind the friends, head bit 0x10 set,
    +0x08 the ignore index;
  * a blocked member's message, friend request and group invite are not
    stored for the blocker; anyone else's still are;
  * scope 2 blocks every handle of the PlayOnline ID;
  * deleting the ignore row (head bit 1 + bit 0x10) lifts the block and does
    NOT delete the friend at the same index;
  * a 2:3 serves 70 friends, slot 69 at +0x08 (the cap is the client's 200).

    python tools/friend_block_test.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="friend-block-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
os.environ["POL_SEARCH_CALIB"] = os.path.join(TMP, "no-such.txt")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "2:3=friends")
for k in ("POL_FRIEND_BLOCK", "POL_FRIEND_IGNORE_TABLE", "POL_FRIEND_IGNORE_SERVE"):
    os.environ.pop(k, None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []
REC = 168
START = 0x15C


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def frame26(rows):
    """A 2:6 with one record per (head_low, slot, live, name or None=heap)."""
    n = len(rows)
    buf = bytearray(START + n * REC + 4)
    struct.pack_into("<H", buf, START - 8, n)
    for i, (_head, slot, live, name) in enumerate(rows):
        at = START + i * REC
        buf[at + 0x04] = slot
        buf[at + 0x06] = live
        if name is None:
            buf[at + 0x14:at + 0x24] = bytes.fromhex("6676475bf9d5cb95bb880ee2695333a3")
        else:
            buf[at + 0x14:at + 0x14 + len(name) + 1] = name.encode() + b"\x00"
    for i, (head, _slot, _live, _name) in enumerate(rows):
        struct.pack_into("<I", buf, START + i * REC - 4, head)
    return bytes(buf)


def message_path(sender_guid, recipient_guid, kind, sender=b"Sender"):
    rec = bytearray(0x48)
    struct.pack_into("<Q", rec, 0x00, sender_guid ^ R._PUSH_GUID_MASK)
    struct.pack_into("<Q", rec, 0x08, recipient_guid ^ R._PUSH_GUID_MASK)
    rec[0x10:0x10 + len(sender)] = sender
    rec[0x20:0x25] = b"Hello"
    struct.pack_into("<I", rec, 0x34, 1790000000)
    struct.pack_into("<I", rec, 0x38, 12)
    struct.pack_into("<H", rec, 0x3E, kind)
    struct.pack_into("<I", rec, 0x40, 0x000203E8)
    return R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec))


def write_31(path, content):
    pt = bytearray(bytes([2, 3, 1, 0])) + bytes(R._MAIL_OBJ_OFF - 4)
    pt[R._FETCH_PATH_OFF:R._FETCH_PATH_OFF + len(path) + 1] = path.encode() + bytes(1)
    pt += struct.pack("<I", len(content)) + content + bytes.fromhex("deadbeef")
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)
    return bytes(pt)


def main():
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "BLOCKT", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Lex", "Arden", "Pell", "Quin"):
        mid = accounts.add_member(conn, "BLOCKT", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, int(accounts.primary_handle_row(conn, mid)["id"]))
    arden_alt = int(accounts.set_handle(conn, ids["Arden"][0], "ArdenTwo"))
    for a, b in (("Lex", "Arden"), ("Lex", "Quin")):
        accounts.request_friend(conn, ids[a][1], b)
        accounts.request_friend(conn, ids[b][1], a)
    conn.close()
    G = {n: accounts.handle_guid(ids[n][1]) for n in ids}

    def as_session(who):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid

    def friends(who):
        c = accounts.connect(DB)
        try:
            return {r["peer_name"]: r["status"] for r in
                    accounts.list_friends(c, ids[who][1], status=None)}
        finally:
            c.close()

    def ignores(who):
        c = accounts.connect(DB)
        try:
            return {r["slot"]: (r["peer_name"], r["scope"])
                    for r in accounts.ignore_list(c, ids[who][1])}
        finally:
            c.close()

    def serve_23():
        n = R._list_count(0x02, 0x03)
        return n, R._list_payload(0x02, 0x03, R._list_paylen(0x02, 0x03, n))

    def mail_kinds(who):
        return sorted(m["kind"] for _w, _p, m in R._mailbox(ids[who][0]))

    def send(sender, recipient, kind, body=b"Hi\x07there\x00", sender_hid=None):
        as_session(sender)
        if sender_hid is not None:
            R._session_handle_id = lambda db=None, h=sender_hid: h
        g = accounts.handle_guid(sender_hid) if sender_hid else G[sender]
        R._capture_resource_write(
            write_31(message_path(g, G[recipient], kind, sender.encode()), body),
            op="3:1")

    print("blocking a friend ->")
    as_session("Lex")
    n, _ = serve_23()
    slots = R._friend_slots_map(ids["Lex"][1]) or {}
    arden_slot = next(s for s, v in slots.items() if v[0] == "Arden")
    R._capture_friend_put(frame26([(0xE2, arden_slot, 0x0A, None),
                                   (0x31, 0, 0x44, "Arden")]))
    check("Arden" not in friends("Lex"), "Lex's friend row for Arden is gone",
          repr(friends("Lex")))
    check(ignores("Lex") == {0: ("Arden", 1)}, "Arden is ignore entry 0, scope handle",
          repr(ignores("Lex")))
    check("Quin" in friends("Lex"), "the other friend is untouched")
    check(friends("Arden").get("Lex") == accounts.STATUS_ACTIVE,
          "Arden's own row is untouched (one-sided, like a delete)")

    print("\nblocking a non-friend from a message ->")
    R._capture_friend_put(frame26([(0x31, 1, 0x44, "Pell")]))
    check("Pell" not in friends("Lex") and "Lex" not in friends("Pell"),
          "no friend row and NO friend request either way",
          repr((friends("Lex"), friends("Pell"))))
    check(ignores("Lex") == {0: ("Arden", 1), 1: ("Pell", 1)},
          "Pell is ignore entry 1", repr(ignores("Lex")))

    print("\nthe 2:3 serves the ignore table behind the friends ->")
    n, payload = serve_23()
    check(n == 3, "count = 1 friend + 2 ignore rows", repr(n))
    recs = [payload[8 + i * REC:8 + (i + 1) * REC] for i in range(n)]
    tail = recs[1:]
    check(all(r[0] & 0x11 == 0x11 and not r[0] & 0x02 for r in tail),
          "ignore rows: head bit 0 and bit 0x10 set",
          repr([hex(r[0]) for r in tail]))
    check(recs[0][0] & 0x10 == 0, "the friend row has bit 0x10 clear")
    check([r[0x08] for r in tail] == [0, 1], "+0x08 is the ignore index",
          repr([r[0x08] for r in tail]))
    check([r[0x18:0x18 + 6].split(b"\x00")[0] for r in tail] == [b"Arden", b"Pell"],
          "names at +0x18")
    check(((tail[0][0] >> 5) & 3) == 1, "scope 1 (handle) in head bits 5-6")
    slots = R._friend_slots_map(ids["Lex"][1]) or {}
    check(sorted(v[0] for v in slots.values()) == ["Quin"],
          "the friend slot map holds friends only", repr(slots))

    print("\nwhat a blocked member sends does not reach the blocker ->")
    send("Arden", "Lex", R.MAIL_KIND_MESSAGE)
    send("Arden", "Lex", R.MAIL_KIND_FRIEND_REQUEST)
    send("Arden", "Lex", R.MAIL_KIND_GROUP_INVITE,
         b"Join?\x07Join \"G\"?\x00" + R._group_notice_tail(77, "G"))
    check(mail_kinds("Lex") == [], "message, friend request, group invite: none stored",
          repr(mail_kinds("Lex")))
    send("Arden", "Lex", R.MAIL_KIND_MESSAGE, sender_hid=arden_alt)
    check(mail_kinds("Lex") == [R.MAIL_KIND_MESSAGE],
          "scope 1: Arden's OTHER handle still gets through", repr(mail_kinds("Lex")))
    send("Quin", "Lex", R.MAIL_KIND_MESSAGE)
    check(len(mail_kinds("Lex")) == 2, "a friend's message is delivered")
    send("Lex", "Arden", R.MAIL_KIND_MESSAGE)
    check(mail_kinds("Arden") == [R.MAIL_KIND_MESSAGE],
          "the blocker can still write to the blocked member (the client refuses that itself)")

    print("\nscope 2: the whole PlayOnline ID ->")
    as_session("Lex")
    R._capture_friend_put(frame26([(0x51, 0, 0x0A, "Arden")]))
    check(ignores("Lex")[0] == ("Arden", 2), "entry 0 is now scope 2", repr(ignores("Lex")))
    before = len(mail_kinds("Lex"))
    send("Arden", "Lex", R.MAIL_KIND_MESSAGE, sender_hid=arden_alt)
    check(len(mail_kinds("Lex")) == before, "Arden's other handle is blocked too")

    print("\nlifting the block ->")
    as_session("Lex")
    n, _ = serve_23()
    quin_slot = next(s for s, v in (R._friend_slots_map(ids["Lex"][1]) or {}).items()
                     if v[0] == "Quin")
    check(quin_slot == 0, "Quin is friend slot 0, the same number as ignore entry 0")
    R._capture_friend_put(frame26([(0x12, 0, 0x0A, None)]))
    check(0 not in ignores("Lex"), "ignore entry 0 (Arden) is removed",
          repr(ignores("Lex")))
    check("Quin" in friends("Lex"),
          "...and friend slot 0 (Quin) is NOT deleted by it")
    before = len(mail_kinds("Lex"))
    send("Arden", "Lex", R.MAIL_KIND_MESSAGE)
    check(len(mail_kinds("Lex")) == before + 1, "Arden's messages arrive again")

    print("\nPOL_FRIEND_BLOCK=0 ->")
    os.environ["POL_FRIEND_BLOCK"] = "0"
    try:
        before = len(mail_kinds("Lex"))
        send("Pell", "Lex", R.MAIL_KIND_MESSAGE)
        check(len(mail_kinds("Lex")) == before + 1, "nothing is filtered")
    finally:
        del os.environ["POL_FRIEND_BLOCK"]

    print("\n200-row friend table ->")
    c = accounts.connect(DB)
    for i in range(70):
        accounts.add_friend(c, ids["Quin"][1], f"Pal{i:03d}", kind=accounts.KIND_FRIEND,
                            guid=0x7000 + i)
    c.close()
    as_session("Quin")
    n, payload = serve_23()
    check(n == 71, "71 friends (Lex + 70) are all served, past the old 64", repr(n))
    last = payload[8 + 70 * REC:8 + 71 * REC]
    check(last[0x08] == 70, "the last record carries slot 70 at +0x08", repr(last[0x08]))
    check(R._friend_put_slot(bytes(4) + bytes([70, 0, 0x0A, 0])) == 70,
          "a 2:6 delete of slot 70 resolves (cap 200, was 0x40)")
    check(R._friend_push_slot_ok(70) and not R._friend_push_slot_ok(200),
          "pushes may address slot 70, not 200")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all block checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
