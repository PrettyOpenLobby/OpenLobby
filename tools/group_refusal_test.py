"""Group refusals and notices: what the lobby says no to, and who it tells.

Until 2026-09-28 only the 3:0 no-data path answered with an error type, so a
fifth group, a disband by a non-owner or a forbidden rank change were logged
and then answered as SUCCESS, and a disband, a removal or a declined invite
reached the other members only at their next login. The behaviour pinned here
follows Project Crystal Server, which answers the Viewer the same way:

    7:1  a name already on the handle -> 0x74; a fifth group -> 0x73
    7:2  no such group, or not the owner -> 0xFF; otherwise every other member
         gets a class-1 push of their own row and a "disbanded" notice (0x13)
    7:3  a change the caller may not make -> 0xFF; a removal is pushed to the
         others and mailed to the removed member ("removed", 0x12); a rank
         change is pushed to the others
    3:1  an invite whose invitee is already in the group -> 0x75, whose group
         is full -> 0x78, whose invitee holds four groups -> 0x73; the
         invitation is not filed
    3:1  a decline (type 0x10) removes the pending invitee and tells the group
    push a class-1 roster record carries the group data only (no name, no icon)

Each refusal is checked through `_build_lobby_reply_pt`, i.e. the reply that
goes on the wire: a 24-byte header with the error type.

    python tools/group_refusal_test.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="group-refusal-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_GROUP_CTL"] = os.path.join(TMP, "no-such.ctl")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups")
os.environ.setdefault("POL_GROUP_MEMBERS", "1")
for k in ("POL_LOBBY_REFUSALS", "POL_GROUP_CREATE_LIMITS", "POL_GROUP_NOTICES",
          "POL_GROUP_REMOVE_PUSH", "POL_GROUP_CLASS_PUSH",
          "POL_GROUP_INVITE_CHECKS", "POL_GROUP_GATE"):
    os.environ.pop(k, None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []
WORLD = "127.0.0.1"


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


# What the handlers send, recorded instead of delivered.
PUSHES = []          # (watcher member id, [[gid, [[guid, name, class], ...]]])
MAILS = []           # (sender name, recipient guid, subject, kind, tail)


def _rec_push(db, member, entries):
    PUSHES.append((int(member), [[int(g), [list(m) for m in ms]] for g, ms in entries]))
    return 0


def _rec_mail(sender_name, sender_guid, recipient_guid, subject, body,
              kind=0x8000, thread=None, when=None, sender_slot=None, tail=b""):
    MAILS.append((sender_name, int(recipient_guid), subject, int(kind), bytes(tail)))
    return "O/m/recorded"


R.push_group_rosters = _rec_push
R._mail_mint = _rec_mail


def lobby_request(op1, op2, body=b""):
    """A decrypted lobby request: the 40-byte header, then the body."""
    hdr = bytearray(0x28)
    hdr[0], hdr[1], hdr[2] = 0x02, op1, op2
    struct.pack_into("<I", hdr, 4, len(body))
    return bytes(hdr) + body


def reply_type(pt):
    """The type byte of the reply `_build_lobby_reply_pt` puts on the wire,
    and whether it is the header-only refusal form."""
    r = R._build_lobby_reply_pt(pt, WORLD)
    return r[1], len(r) == 24


def main():
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "GRPREF", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Olive", "Sable", "Mika", "Pell", "Nico", "Quin"):
        mid = accounts.add_member(conn, "GRPREF", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, accounts.primary_handle_row(conn, mid)["id"])
    conn.close()

    def as_session(who):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid

    def create(who, name):
        as_session(who)
        body = name.encode("cp932").ljust(R._GROUP_NAME_MAX, b"\x00") + b"\x00" * 4
        return reply_type(lobby_request(0x07, 0x01, body))

    def group_of(owner, name):
        c = accounts.connect(DB)
        try:
            return accounts.group_id(c, ids[owner][1], name)
        finally:
            c.close()

    print("the refusal slot ->")
    pt = lobby_request(0x07, 0x02, b"\x00" * 12)
    R._lobby_refuse(R.LOBBY_ERR_GENERIC, "test", pt)
    check(R._lobby_take_refusal(lobby_request(0x07, 0x03, b"\x00" * 12)) is None,
          "a refusal recorded for one request is not applied to another")
    check(R._lobby_take_refusal(pt) is None, "...and it is cleared, not kept")
    R._lobby_refuse(R.LOBBY_ERR_GENERIC, "test", pt)
    check(R._lobby_take_refusal(pt) == 0xFF, "the same request's refusal is taken")

    print("\n7:1 create ->")
    t, hdr = create("Olive", "TEST GROUP")
    check(t == 0 and not hdr, "a first group is created (type 0, id in the payload)")
    gid = group_of("Olive", "TEST GROUP")
    check(gid is not None, "and stored", repr(gid))
    t, hdr = create("Olive", "TEST GROUP")
    check((t, hdr) == (0x74, True), "the same name again: 0x74 name in use", repr((t, hdr)))
    c = accounts.connect(DB)
    accounts.add_friend(c, ids["Sable"][1], "Mika")
    c.close()
    t, hdr = create("Sable", "Mika")
    check((t, hdr) == (0x74, True), "a name that is a FRIEND on this handle: 0x74", repr((t, hdr)))
    c = accounts.connect(DB)
    row = c.execute("SELECT kind FROM friend WHERE handle_id = %s AND peer_name = %s",
                    (ids["Sable"][1], "Mika")).fetchone()
    c.close()
    check(row is not None and int(row["kind"]) == accounts.KIND_FRIEND,
          "...and the friend row stays a friend (it used to become a group)")
    for n in ("G2", "G3", "G4"):
        t, _ = create("Olive", n)
        check(t == 0, f"group {n} is created")
    t, hdr = create("Olive", "G5")
    check((t, hdr) == (0x73, True), "a fifth group: 0x73 you can only join up to 4", repr((t, hdr)))
    check(group_of("Olive", "G5") is None, "...and nothing is stored")
    os.environ["POL_LOBBY_REFUSALS"] = "0"
    try:
        t, _ = create("Olive", "G5")
        check(t == 0, "POL_LOBBY_REFUSALS=0 answers success, as before")
    finally:
        del os.environ["POL_LOBBY_REFUSALS"]
    t, hdr = create("Sable", "SABLES")
    check(t == 0 and not hdr, "another member's first group is unaffected")

    c = accounts.connect(DB)
    accounts.add_group_member(c, gid, "Sable", member_handle=ids["Sable"][1], cls=4)
    accounts.add_group_member(c, gid, "Mika", member_handle=ids["Mika"][1], cls=3)
    accounts.add_group_member(c, gid, "Pell", member_handle=ids["Pell"][1], cls=3, pending=1)
    c.close()

    print("\n7:3 role changes ->")

    def class_change(who, target, cls):
        as_session(who)
        body = bytearray(0x28)
        struct.pack_into("<QII", body, 0, gid, 1, 0)
        struct.pack_into("<QI", body, R._GROUP_CLASS_ENTRY_OFF,
                         accounts.handle_guid(ids[target][1]), cls << 8)
        return reply_type(lobby_request(0x07, 0x03, bytes(body)))

    PUSHES.clear()
    MAILS.clear()
    t, hdr = class_change("Mika", "Mika", 5)
    check((t, hdr) == (0xFF, True), "a member promoting themself: 0xFF", repr((t, hdr)))
    check(not PUSHES and not MAILS, "...and nobody is told anything")
    t, _ = class_change("Olive", "Mika", 4)
    check(t == 0, "the master promotes Mika")
    told = {m for m, _e in PUSHES}
    check(told == {ids["Sable"][0], ids["Mika"][0], ids["Pell"][0]},
          "the rank change is pushed to everyone but the requester", repr(told))
    check(all(e == [[gid, [[accounts.handle_guid(ids["Mika"][1]), "Mika", 4]]]]
              for _m, e in PUSHES), "...as Mika's row at class 4", repr(PUSHES[:1]))
    PUSHES.clear()
    t, _ = class_change("Olive", "Mika", 1)
    check(t == 0, "the master removes Mika")
    check({m for m, _e in PUSHES} == {ids["Sable"][0], ids["Mika"][0], ids["Pell"][0]}
          and all(e[0][1][0][2] == 1 for _m, e in PUSHES),
          "the removal is pushed at class 1, Mika included", repr(PUSHES))
    check(len(MAILS) == 1 and MAILS[0][1] == accounts.handle_guid(ids["Mika"][1])
          and MAILS[0][3] == R.MAIL_KIND_GROUP_REMOVED,
          "Mika is mailed a 'removed' notice (0x890A)", repr(MAILS))
    check(MAILS and MAILS[0][2] == "TEST GROUP"
          and struct.unpack_from("<Q", MAILS[0][4], 0)[0] == gid
          and MAILS[0][4][8:18] == b"TEST GROUP",
          "...naming the group in its subject and its trailing block")
    PUSHES.clear()
    MAILS.clear()
    t, _ = class_change("Sable", "Sable", 1)
    check(t == 0, "a sub-master may leave")
    check(not MAILS, "leaving on your own sends no 'removed' notice")

    print("\n3:1 invites ->")
    real_meta, real_row = R._mail_meta, R._mail_recipient_row

    def invite_refused(inviter, invitee, group, gname):
        as_session(inviter)
        R._mail_meta = lambda path: {"kind": R.MAIL_KIND_GROUP_INVITE,
                                     "recipient_guid": 1, "subject": "x"}
        R._mail_recipient_row = lambda db, g, h=ids[invitee][1]: db.execute(
            "SELECT * FROM handle WHERE id = %s", (h,)).fetchone()
        data = (b"Would you like to join?\x07Join \"" + gname.encode() + b"\"\x00"
                + R._group_notice_tail(group, gname))
        req = lobby_request(0x03, 0x01, b"\x00" * 16)
        refused = R._group_invite_refusal("O/m/x", data, req)
        return refused, R._lobby_take_refusal(req)

    try:
        check(invite_refused("Olive", "Pell", gid, "TEST GROUP") == (True, 0x75),
              "inviting someone already invited: 0x75")
        check(invite_refused("Olive", "Nico", gid, "TEST GROUP") == (False, None),
              "inviting a newcomer goes through")
        c = accounts.connect(DB)
        for i in range(accounts.GROUP_MEMBER_MAX):
            accounts.add_group_member(c, gid, f"Filler{i:02d}", cls=3)
        c.close()
        check(invite_refused("Olive", "Nico", gid, "TEST GROUP") == (True, 0x78),
              "inviting into a group of 64: 0x78")
        c = accounts.connect(DB)
        c.execute("DELETE FROM group_member WHERE group_id = %s AND member_name LIKE %s",
                  (gid, "Filler%"))
        c.commit()
        for n in ("Q1", "Q2", "Q3", "Q4"):
            accounts.add_friend(c, ids["Quin"][1], n, kind=accounts.KIND_GROUP, guid=0)
        c.close()
        check(invite_refused("Olive", "Quin", gid, "TEST GROUP") == (True, 0x73),
              "inviting someone who holds four groups: 0x73")
        os.environ["POL_GROUP_INVITE_CHECKS"] = "0"
        try:
            check(invite_refused("Olive", "Quin", gid, "TEST GROUP") == (False, None),
                  "POL_GROUP_INVITE_CHECKS=0 files it as before")
        finally:
            del os.environ["POL_GROUP_INVITE_CHECKS"]

        print("\n3:1 decline ->")
        PUSHES.clear()
        as_session("Pell")
        R._mail_meta = lambda path: {"kind": R.MAIL_KIND_GROUP_DECLINED,
                                     "recipient_guid": 1, "subject": "x"}
        data = b"No thanks\x07\x00" + R._group_notice_tail(gid, "TEST GROUP")
        R._capture_group_invite("O/m/x", data)
        c = accounts.connect(DB)
        gone = c.execute("SELECT 1 FROM group_member WHERE group_id = %s AND member_name = %s",
                         (gid, "Pell")).fetchone()
        c.close()
        check(gone is None, "Pell's decline removes the pending invitation")
        check(ids["Olive"][0] in {m for m, _e in PUSHES}
              and all(e[0][1][0][1:] == ["Pell", 1] for _m, e in PUSHES),
              "...and the owner is pushed Pell at class 1", repr(PUSHES))
    finally:
        R._mail_meta, R._mail_recipient_row = real_meta, real_row

    print("\n7:11 my settings in the group ->")
    as_session("Olive")
    before = R._group_record(0x88, 0, "TEST GROUP", guid=gid,
                             settings=R._my_group_settings(gid))
    check(before[0x08:0x08 + 20] == "TEST GROUP".encode("utf-16-le"),
          "with nothing sent, +0x08 is what it always was")
    body = bytearray(0x78)
    struct.pack_into("<Q", body, 0, gid)
    wide = "back soon".encode("utf-16-le")
    body[0x08:0x08 + len(wide)] = wide
    body[0x6E] = 0
    body[0x6F] = 3
    t, _ = reply_type(lobby_request(0x07, 0x0B, bytes(body) + b"\x00" * 4))
    check(t == 0, "7:11 is answered with success")
    after = R._group_record(0x88, 0, "TEST GROUP", guid=gid,
                            settings=R._my_group_settings(gid))
    check(after[0x08:0x08 + len(wide)] == wide and after[0x08 + len(wide)] == 0,
          "the comment comes back at +0x08", repr(after[0x08:0x20]))
    check(after[0x6F] == 3, "the status comes back at +0x6F", repr(after[0x6F]))
    check(after[0x70:0x7A] == b"TEST GROUP", "the row label at +0x70 is unchanged")
    as_session("Nico")
    other = R._group_record(0x88, 0, "TEST GROUP", guid=gid,
                            settings=R._my_group_settings(gid))
    check(other == before, "another member's record is not touched")

    print("\n7:2 disband ->")

    def disband(who, group):
        as_session(who)
        body = struct.pack("<Q", group) + b"\x00" * 8
        return reply_type(lobby_request(0x07, 0x02, body))

    c = accounts.connect(DB)
    accounts.add_group_member(c, gid, "Nico", member_handle=ids["Nico"][1], cls=3)
    c.close()
    PUSHES.clear()
    MAILS.clear()
    t, hdr = disband("Nico", gid)
    check((t, hdr) == (0xFF, True), "a member who is not the owner: 0xFF", repr((t, hdr)))
    check(group_of("Olive", "TEST GROUP") == gid, "...and the group stays")
    t, hdr = disband("Olive", 999999)
    check((t, hdr) == (0xFF, True), "a group id we do not hold: 0xFF")
    t, _ = disband("Olive", gid)
    check(t == 0 and group_of("Olive", "TEST GROUP") is None, "the owner disbands it")
    check([(m, e[0][1][0][1], e[0][1][0][2]) for m, e in PUSHES] == [(ids["Nico"][0], "Nico", 1)],
          "each other member is pushed their OWN row at class 1", repr(PUSHES))
    check([(r, k) for _s, r, _sub, k, _t in MAILS]
          == [(accounts.handle_guid(ids["Nico"][1]), R.MAIL_KIND_GROUP_DISBANDED)],
          "...and mailed a 'disbanded' notice (0x898A)", repr(MAILS))

    print("\nthe class-1 push record ->")

    class Sess:
        alive = True
        nick = b"WATCHER"

        def __init__(self):
            self.lines = []

        def send(self, lines):
            self.lines += list(lines)
            return True

    ts = Sess()
    real_sessions = R.PRESENCE.sessions_for
    R.PRESENCE.sessions_for = lambda m: [ts]
    try:
        g = accounts.handle_guid(ids["Nico"][1])
        R._push_deliver_grouprows({"member": 1, "after": 0,
                                   "groups": [[7, [[g, "Nico", 1], [g, "Nico", 3]]]]})
    finally:
        R.PRESENCE.sessions_for = real_sessions
    flags = [R._b64decode(line.split(b" :", 1)[1].decode()[96:])[0] for line in ts.lines]
    check(len(flags) == 2 and flags[0] == R._PUSH_F_OBJECT,
          "a class-1 record carries the group data only", repr(flags))
    check(len(flags) == 2 and flags[1] & R._PUSH_F_NAME,
          "a class-3 record still carries the name", repr(flags))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all group refusal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
