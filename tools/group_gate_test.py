"""A GROUP IS ITS MEMBERS' -- the #XXL channel and the group requests gate on it.

Without these gates nothing checks membership anywhere in the group system:
any signed-in client can JOIN `#XXL<gid>` (group ids are small, guessable
friend-row ids) and read the chat, talk into it without joining, list the
members' nicks and addresses with WHO, lock members out with MODE +b/+k, KICK
anyone, promote itself to master or remove others with 7:3, and make itself a
member with an accept-shaped message naming the group. This pins every rule of
`_xxl_gate`, the 7:3 authorisation and the invite/accept checks, allowed and
refused, so neither half can drift:

    owner Olive (5), sub-master Sable (4), member Mika (3), pending Pell,
    outsider Mallory.

    python tools/group_gate_test.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="group-gate-")
DB = os.path.join(TMP, "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_GROUP_CTL"] = os.path.join(TMP, "no-such.ctl")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups")
os.environ.setdefault("POL_GROUP_MEMBERS", "1")

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


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


SRV = b"pol-1000-51241.pol.com"


def say(sess, line):
    return R._auth_session_reply(line, sess.nick, SRV, peer_ip=sess.peer_ip, sess=sess) or []


def main():
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "GRPGATE", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Olive", "Sable", "Mika", "Pell", "Mallory"):
        mid = accounts.add_member(conn, "GRPGATE", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, accounts.primary_handle_row(conn, mid)["id"])
    gid = accounts.add_friend(conn, ids["Olive"][1], "TEST GROUP", kind=accounts.KIND_GROUP, guid=0)
    accounts.add_group_member(conn, gid, "Olive", member_handle=ids["Olive"][1], cls=accounts.GROUP_CLASS_MASTER)
    accounts.add_group_member(conn, gid, "Sable", member_handle=ids["Sable"][1], cls=accounts.GROUP_CLASS_SUBMASTER)
    accounts.add_group_member(conn, gid, "Mika", member_handle=ids["Mika"][1], cls=3)
    accounts.add_group_member(conn, gid, "Pell", member_handle=ids["Pell"][1], cls=3, pending=1)
    conn.close()
    chan = b"#XXL%016X" % gid
    s = {n: Sess(("U" + n.upper()).encode()[:9].ljust(9, b"X"), ids[n][0]) for n in ids}

    print("accounts.group_class_of ->")
    conn = accounts.connect(DB)
    got = {n: accounts.group_class_of(conn, gid, member_id=ids[n][0]) for n in ids}
    conn.close()
    check(got == {"Olive": 5, "Sable": 4, "Mika": 3, "Pell": None, "Mallory": None},
          "owner 5, sub-master 4, member 3, pending and outsider None", repr(got))

    print("\nJOIN ->")
    for n in ("Olive", "Sable", "Mika"):
        r = say(s[n], b"JOIN " + chan)
        check(any(b" 353 " in l for l in r), f"{n} (a member) joins")
    for n in ("Pell", "Mallory"):
        r = say(s[n], b"JOIN " + chan)
        check(r and b" 473 " in r[0], f"{n} ({'pending' if n == 'Pell' else 'outsider'}) is refused 473", repr(r[:1]))
    r = say(s["Mallory"], b"JOIN " + chan.lower().replace(b"#xxl", b"#XXL"))
    check(r and b" 473 " in r[0] and s["Mallory"] not in R.ROOMS.members(chan.lower().replace(b"#xxl", b"#XXL")),
          "a lower-case spelling of the channel is refused, not a second ungated room")
    check(s["Mallory"] not in R.ROOMS.members(chan), "the outsider is not in the room")

    print("\ntalking, listing, moderating ->")
    for m in s.values():
        m.sent.clear()
    say(s["Mallory"], b"NOTICE " + chan + b" :TATTTTTTztSKTTTT")
    say(s["Mallory"], b"PRIVMSG " + chan + b" :0 0 00Mallory\t01hi")
    check(not s["Olive"].sent and not s["Mika"].sent, "an outsider's NOTICE/PRIVMSG reaches nobody", repr(s["Olive"].sent))
    say(s["Mika"], b"NOTICE " + chan + b" :TATTTTTTztSKTTTT")
    check(any(b"NOTICE " + chan in l for l in s["Olive"].sent), "a member's NOTICE is relayed")
    r = say(s["Mallory"], b"WHO " + chan)
    check(len(r) == 1 and b" 315 " in r[0], "an outsider's WHO gets only the end of the list", repr(r))
    r = say(s["Mika"], b"WHO " + chan)
    check(sum(b" 352 " in l for l in r) == 3, "a member's WHO lists the three in the room", repr(len(r)))
    r = say(s["Mika"], b"MODE " + chan)
    check(r and b" 324 " in r[0], "a member may ask the channel modes")
    say(s["Mika"], b"MODE " + chan + b" +b " + s["Olive"].nick)
    check(s["Olive"].nick not in R.ROOMS.modes(chan).get("b", ()), "a plain member cannot +b (ban) the owner")
    say(s["Mallory"], b"MODE " + chan + b" +k secret")
    check(not R.ROOMS.modes(chan).get("k"), "an outsider cannot +k the channel")
    say(s["Olive"], b"MODE " + chan + b" +k secret")
    check(not R.ROOMS.modes(chan).get("k"), "even the master cannot +k a group channel (roles only)")
    r = say(s["Olive"], b"MODE " + chan + b" +o " + s["Sable"].nick)
    check(r and b" MODE " in r[0], "the master may +o", repr(r))
    say(s["Mika"], b"KICK " + chan + b" " + s["Sable"].nick)
    check(s["Sable"] in R.ROOMS.members(chan), "a plain member cannot KICK")
    say(s["Mallory"], b"TOPIC " + chan + b" :owned")
    check(R._room_topic(chan) != b"owned", "an outsider cannot set the topic")
    say(s["Sable"], b"KICK " + chan + b" " + s["Mika"].nick)
    check(s["Mika"] not in R.ROOMS.members(chan), "the sub-master may KICK")

    print("\n7:3 role changes ->")

    def class_change(who, target_name, cls):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid
        body = bytearray(R._GROUP_NAME_OFF + 0x28)
        o = R._GROUP_NAME_OFF
        struct.pack_into("<QII", body, o, gid, 1, 0)
        struct.pack_into("<QI", body, o + R._GROUP_CLASS_ENTRY_OFF,
                         accounts.handle_guid(ids[target_name][1]), cls << 8)
        R._group_class_change(bytes(body))
        c = accounts.connect(DB)
        try:
            return accounts.group_class_of(c, gid, member_id=ids[target_name][0])
        finally:
            c.close()

    check(class_change("Mika", "Mika", 5) == 3, "a member cannot promote themself")
    check(class_change("Mika", "Sable", 1) == 4, "a member cannot remove the sub-master")
    check(class_change("Mallory", "Mika", 1) == 3, "an outsider cannot remove a member")
    check(class_change("Sable", "Mika", 4) == 3, "a sub-master cannot raise a member to sub-master")
    check(class_change("Sable", "Olive", 1) == 5, "a sub-master cannot remove the owner")
    check(class_change("Olive", "Mika", 4) == 4, "the master can promote")
    check(class_change("Olive", "Mika", 3) == 3, "...and demote")
    check(class_change("Mika", "Mika", 1) is None, "a member can leave")

    print("\ninvites and accepts (3:1 messages) ->")
    real_recipient = R._mail_recipient_row

    def as_sender(who):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid

    def invite(sender, invitee):
        as_sender(sender)
        R._mail_recipient_row = lambda db, g, h=ids[invitee][1]: db.execute(
            "SELECT * FROM handle WHERE id = ?", (h,)).fetchone()
        R._group_join_from_message(gid, "TEST GROUP", "invite", {"recipient_guid": 1})
        c = accounts.connect(DB)
        try:
            return c.execute("SELECT pending FROM group_member WHERE group_id=? AND member_name=?",
                             (gid, invitee)).fetchone()
        finally:
            c.close()

    try:
        check(invite("Mallory", "Mallory") is None, "an outsider cannot invite (themself or anyone)")
        c = accounts.connect(DB)
        accounts.add_friend(c, ids["Sable"][1], "Mallory")      # irrelevant: friendship is not the gate
        c.close()
        as_sender("Mallory")
        R._group_join_from_message(gid, "TEST GROUP", "accept", None)
        c = accounts.connect(DB)
        check(accounts.group_class_of(c, gid, member_id=ids["Mallory"][0]) is None,
              "an ACCEPT with no invitation joins nobody")
        c.close()
        row = invite("Olive", "Mallory")
        check(row is not None and row["pending"] == 1, "the master's invite stores a pending member")
        as_sender("Mallory")
        R._group_join_from_message(gid, "TEST GROUP", "accept", None)
        c = accounts.connect(DB)
        check(accounts.group_class_of(c, gid, member_id=ids["Mallory"][0]) == 3,
              "...and their accept makes them a member")
        c.close()
        r = say(s["Mallory"], b"JOIN " + chan)
        check(any(b" 353 " in l for l in r), "...who may now join the channel")
    finally:
        R._mail_recipient_row = real_recipient

    print("\nthe kill switch ->")
    os.environ["POL_GROUP_GATE"] = "0"
    try:
        r = say(s["Pell"], b"JOIN " + chan)
        check(any(b" 353 " in l for l in r), "POL_GROUP_GATE=0 lets the pending invitee in again")
    finally:
        del os.environ["POL_GROUP_GATE"]

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all group gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
