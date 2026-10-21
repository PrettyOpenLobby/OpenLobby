"""A group from creation to disband, through the real lobby handlers.

Every step goes in the way the client sends it and every check reads what the
server stored or served, so the steps are pinned together rather than one at a
time (group_refusal_test and group_gate_test pin each rule alone):

    7:1   Olive creates "TEST GROUP"; the reply carries the id 7:12 serves
    3:1   Olive invites Nico (kind 0x870A, group id in the object's trailer):
          Nico is stored PENDING, not yet a member
    3:1   Nico accepts (kind 0x878A, written by Nico): Nico is a member,
          7:12 lists the group for Nico, and the roster is pushed to both
    IRC   both JOIN #XXL<gid>; a chat line and a NOTICE from Olive reach Nico
          (and not Olive), and Nico's reply reaches Olive
    7:11  Nico's per-group comment and status come back in Nico's 7:12 record
    7:3   Nico leaves (class 1 on their own row): the row goes, no notice
    3:1   Pell is invited and accepts; 7:2 by Olive disbands the group: the
          row is gone and Pell is told (0x898A)

    python tools/group_lifecycle_test.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="group-lifecycle-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
os.environ["POL_GROUP_CTL"] = os.path.join(TMP, "no-such.ctl")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups")
os.environ.setdefault("POL_GROUP_MEMBERS", "1")
for k in ("POL_LOBBY_REFUSALS", "POL_GROUP_CREATE_LIMITS", "POL_GROUP_NOTICES",
          "POL_GROUP_REMOVE_PUSH", "POL_GROUP_CLASS_PUSH", "POL_GROUP_GATE",
          "POL_GROUP_INVITE_CHECKS", "POL_GROUPS"):
    os.environ.pop(k, None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []
WORLD = "127.0.0.1"
SRV = b"pol-1000-51241.pol.com"


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


PUSHES = []          # (member id, roster entries)


def _rec_push(db, member, entries):
    PUSHES.append((int(member), [[int(g), [list(m) for m in ms]] for g, ms in entries]))
    return 0


R.push_group_rosters = _rec_push
GSTATUS = []         # (member, handle, gid) per 7:11 status push
R.push_group_status = lambda db, m, h, g: GSTATUS.append((int(m), int(h), int(g))) or 0


def lobby_request(op1, op2, body=b""):
    hdr = bytearray(0x28)
    hdr[0], hdr[1], hdr[2] = 0x02, op1, op2
    struct.pack_into("<I", hdr, 4, len(body))
    return bytes(hdr) + body


def reply(pt):
    return R._build_lobby_reply_pt(pt, WORLD)


def message_path(sender_guid, recipient_guid, kind, sender=b"Sender"):
    """An `O/m/` path whose header is a message record of `kind`."""
    rec = bytearray(0x48)
    struct.pack_into("<Q", rec, 0x00, sender_guid ^ R._PUSH_GUID_MASK)
    struct.pack_into("<Q", rec, 0x08, recipient_guid ^ R._PUSH_GUID_MASK)
    rec[0x10:0x10 + len(sender)] = sender
    rec[0x20:0x25] = b"Group"
    struct.pack_into("<I", rec, 0x34, 1790000000)
    struct.pack_into("<I", rec, 0x38, 0x40)
    struct.pack_into("<H", rec, 0x3E, kind)
    struct.pack_into("<I", rec, 0x40, 0x100203E8)
    return R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec))


def write_31(path, content):
    """A 03:01 frame in SE's layout: path at +0x38, object block at the tail."""
    pt = bytearray(bytes([2, 3, 1, 0])) + bytes(R._MAIL_OBJ_OFF - 4)
    pt[R._FETCH_PATH_OFF:R._FETCH_PATH_OFF + len(path) + 1] = path.encode() + bytes(1)
    pt += struct.pack("<I", len(content)) + content + bytes.fromhex("deadbeef")
    struct.pack_into("<I", pt, 4, len(pt) - 0x28)
    return bytes(pt)


class Sess:
    def __init__(self, nick, member_id):
        self.nick = nick
        self.peer_ip = b"127.0.0.9"
        self.member = {"id": member_id}
        self.away = False
        self.alive = True
        self.sent = []

    def send(self, lines):
        self.sent.extend(lines)
        return True


def main():
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "GRPLIFE", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Olive", "Nico", "Pell"):
        mid = accounts.add_member(conn, "GRPLIFE", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, int(accounts.primary_handle_row(conn, mid)["id"]))
    conn.close()
    G = {n: accounts.handle_guid(ids[n][1]) for n in ids}

    def as_session(who):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid

    def member_row(gid, name):
        c = accounts.connect(DB)
        try:
            return c.execute("SELECT class, pending FROM group_member WHERE"
                             " group_id = %s AND member_name = %s",
                             (gid, name)).fetchone()
        finally:
            c.close()

    def groups_served(who):
        as_session(who)
        n = R._list_count(0x07, 0x0C)
        return n, R._list_payload(0x07, 0x0C, R._list_paylen(0x07, 0x0C, n))

    def invite(gid, inviter, invitee):
        as_session(inviter)
        path = message_path(G[inviter], G[invitee], R.MAIL_KIND_GROUP_INVITE,
                            inviter.encode())
        data = (b"Would you like to join a friend group?\x07Join \"TEST GROUP\"?\x00"
                + R._group_notice_tail(gid, "TEST GROUP"))
        R._capture_resource_write(write_31(path, data), op="3:1")

    def accept(gid, who, owner):
        as_session(who)
        path = message_path(G[who], G[owner], R.MAIL_KIND_GROUP_REGISTERED,
                            who.encode())
        # A subject, an EMPTY body, then the trailer naming the group.
        data = (b"Group registration accepted\x07\x00"
                + R._group_notice_tail(gid, "TEST GROUP"))
        R._capture_resource_write(write_31(path, data), op="3:1")

    print("7:1 create ->")
    as_session("Olive")
    body = "TEST GROUP".encode("cp932").ljust(R._GROUP_NAME_MAX, b"\x00") + bytes(4)
    r = reply(lobby_request(0x07, 0x01, body))
    c = accounts.connect(DB)
    gid = accounts.group_id(c, ids["Olive"][1], "TEST GROUP")
    c.close()
    check(r[1] == 0 and gid is not None, "the group is created and stored", repr(gid))
    n, payload = groups_served("Olive")
    check(n >= 1 and b"TEST GROUP" in payload, "7:12 lists it for its owner")
    check(struct.pack("<Q", gid) in payload,
          "...under the id the create stored (the #XXL channel key)")

    print("\n3:1 invite ->")
    invite(gid, "Olive", "Nico")
    row = member_row(gid, "Nico")
    check(row is not None and int(row["pending"]) == 1,
          "Nico is stored PENDING", repr(dict(row) if row else None))
    n, payload = groups_served("Nico")
    check(b"TEST GROUP" not in payload,
          "a pending invitee is not yet served the group in 7:12")

    print("\n3:1 accept ->")
    PUSHES.clear()
    accept(gid, "Nico", "Olive")
    row = member_row(gid, "Nico")
    check(row is not None and int(row["pending"]) == 0
          and int(row["class"]) == accounts.GROUP_CLASS_MEMBER,
          "Nico is a member (class 3, not pending)", repr(dict(row) if row else None))
    n, payload = groups_served("Nico")
    check(b"TEST GROUP" in payload, "7:12 now lists the group for Nico")
    told = {m for m, _e in PUSHES}
    check({ids["Olive"][0], ids["Nico"][0]} <= told,
          "the roster is pushed to the owner and the new member", repr(told))
    names = {m[1] for _m, e in PUSHES for _g, ms in e for m in ms}
    check({"Olive", "Nico"} <= names, "...naming both of them", repr(names))

    print("\nIRC group chat between two members ->")
    chan = b"#XXL%016X" % gid
    s = {n: Sess(("U" + n.upper()).encode()[:9].ljust(9, b"X"), ids[n][0])
         for n in ("Olive", "Nico")}

    def say(who, line):
        return R._auth_session_reply(line, s[who].nick, SRV,
                                     peer_ip=s[who].peer_ip, sess=s[who]) or []

    for who in ("Olive", "Nico"):
        out = say(who, b"JOIN " + chan)
        check(any(b" 353 " in ln for ln in out), f"{who} joins {chan.decode()}")
    for m in s.values():
        m.sent.clear()
    say("Olive", b"PRIVMSG " + chan + b" :0 0 40Olive\t01hello group")
    check(any(b"PRIVMSG " + chan in ln and b"hello group" in ln
              for ln in s["Nico"].sent),
          "Olive's chat line reaches Nico", repr(s["Nico"].sent[-1:]))
    check(not any(b"hello group" in ln for ln in s["Olive"].sent),
          "...and is not relayed back to Olive's own session")
    say("Olive", b"NOTICE " + chan + b" :TATTTTTTztSKTTTT")
    check(any(b"NOTICE " + chan in ln for ln in s["Nico"].sent),
          "Olive's NOTICE reaches Nico")
    s["Olive"].sent.clear()
    say("Nico", b"PRIVMSG " + chan + b" :0 0 40Nico\t01hi back")
    check(any(b"hi back" in ln for ln in s["Olive"].sent),
          "Nico's reply reaches Olive")
    say("Nico", b"PART " + chan)
    say("Olive", b"PART " + chan)

    print("\n7:11 my status in the group ->")
    as_session("Nico")
    body = bytearray(0x78)
    struct.pack_into("<Q", body, 0, gid)
    wide = "back soon".encode("utf-16-le")
    body[0x08:0x08 + len(wide)] = wide
    body[0x6F] = 2
    r = reply(lobby_request(0x07, 0x0B, bytes(body) + bytes(4)))
    check(r[1] == 0, "7:11 is answered with success")
    n, payload = groups_served("Nico")
    check(wide in payload, "Nico's per-group comment is served back in 7:12")
    check(GSTATUS == [(ids["Nico"][0], ids["Nico"][1], gid)],
          "the other members are told: one grpstatus push for that group",
          repr(GSTATUS))
    c = accounts.connect(DB)
    try:
        st = [R._group_status_state(c, gid, ids["Nico"][1], s)
              for s in ("online", "offline")]
    finally:
        c.close()
    check(st == ["away", "offline"],
          "status 2 in this group reads away there, and never brightens offline",
          repr(st))
    body[0x6F] = 5
    reply(lobby_request(0x07, 0x0B, bytes(body) + bytes(4)))
    c = accounts.connect(DB)
    try:
        st = R._group_status_state(c, gid, ids["Nico"][1], "online")
        other = R._group_status_state(c, gid, ids["Olive"][1], "online")
    finally:
        c.close()
    check(st == "offline", "status 5 (Hidden in this group) reads offline", st)
    check(other == "online", "another member's state is untouched", other)
    body[0x6F] = 1
    reply(lobby_request(0x07, 0x0B, bytes(body) + bytes(4)))

    print("\n7:3 Nico leaves ->")
    as_session("Nico")
    body = bytearray(0x28)
    struct.pack_into("<QII", body, 0, gid, 1, 0)
    struct.pack_into("<QI", body, R._GROUP_CLASS_ENTRY_OFF, G["Nico"], 1 << 8)
    r = reply(lobby_request(0x07, 0x03, bytes(body)))
    check(r[1] == 0, "the leave is answered with success")
    check(member_row(gid, "Nico") is None, "Nico's member row is gone")
    n, payload = groups_served("Nico")
    check(b"TEST GROUP" not in payload, "7:12 no longer lists it for Nico")

    print("\n7:2 disband ->")
    invite(gid, "Olive", "Pell")
    accept(gid, "Pell", "Olive")
    check(member_row(gid, "Pell") is not None
          and int(member_row(gid, "Pell")["pending"]) == 0, "Pell joined")
    as_session("Olive")
    r = reply(lobby_request(0x07, 0x02, struct.pack("<Q", gid) + bytes(8)))
    c = accounts.connect(DB)
    gone = accounts.group_id(c, ids["Olive"][1], "TEST GROUP")
    c.close()
    check(r[1] == 0 and gone is None, "the owner disbands it")
    pell_mail = [m for _w, _p, m in R._mailbox(ids["Pell"][0])
                 if m["kind"] == R.MAIL_KIND_GROUP_DISBANDED]
    check(len(pell_mail) == 1, "Pell is mailed a 'disbanded' notice (0x898A)",
          repr(len(pell_mail)))
    n, payload = groups_served("Pell")
    check(b"TEST GROUP" not in payload, "7:12 no longer lists it for Pell")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all group lifecycle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
