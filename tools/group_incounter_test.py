"""The client's "2/3 in chat" counter -- are BOTH of its inputs right?

The account holder, comparing our group chat with retail on 2026-08-19: ours does
not show the `N/M in chat` line the Viewer draws above a group's chat box.
Nothing on the wire carries that string -- it is CLIENT-DERIVED
(established by decoding the retail group-chat capture):

    M  the group's total member count, from the 07:12 KGetGroupList count block
    N  how many of them are in the channel, from the 353/352 roster

so it can only render when both are true at once, and neither half can be
verified by looking at the other. What is needed is
therefore an AUDIT, and this is it: one three-member group, two of them joined,
every number checked against the three-and-two we set up.

Both halves are served by DIFFERENT containers over DIFFERENT bands -- 7:12 is a
lobby opcode on `login` and the roster is IRC on `authsess` -- which is exactly
the arrangement in which the two can drift apart without anything looking wrong
on either side. That is what this file exists to catch.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="group-counter-")
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


class Sess:
    """The fields the roster builders read off a ChatSession."""

    def __init__(self, nick, member_id, ip=b"192.0.2.1"):
        self.nick = nick
        self.peer_ip = ip
        self.member = {"id": member_id}
        self.away = False
        self.alive = True


def main():
    # --- three members, one group ------------------------------------------ #
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "GRPCOUNT", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Fox", "Cyn", "Yatih"):
        mid = accounts.add_member(conn, "GRPCOUNT", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, accounts.primary_handle_row(conn, mid)["id"])
    owner_hid = ids["Fox"][1]
    gid = accounts.add_friend(conn, owner_hid, "CRAZY PEOPLE",
                              kind=accounts.KIND_GROUP, guid=0)
    # The owner is stored as MASTER, which is what `7:1 KCreateGroup` does --
    # `_group_create` passes GROUP_CLASS_MASTER. It matters here and not only in
    # the 7:12 record: `_group_op_nicks` reads the STORED class to decide who
    # carries the '@' in the roster, and retail's own 353 for this channel puts
    # it on the class-5 member (`:UL0C0F1HJ @UD5PUQZGA`).
    for name in ("Fox", "Cyn", "Yatih"):
        accounts.add_group_member(
            conn, gid, name, member_handle=ids[name][1],
            cls=(accounts.GROUP_CLASS_MASTER if name == "Fox" else 3))
    conn.close()

    print("M -- the 07:12 member total ->")
    for who in ("Fox", "Cyn", "Yatih"):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid
        count = R._list_count(0x07, 0x0C)
        payload = R._list_payload(0x07, 0x0C, R._list_paylen(0x07, 0x0C, count))
        check(count == 1, f"{who}: sees the group at all", f"{count} group(s)")
        # Count block: byte 0 is the group count, bytes 1..4 the per-group
        # MEMBER counts -- this is M, and it is the number the client divides
        # the in-room roster against.
        check(payload[0] == 1 and payload[1] == 3,
              f"{who}: M = 3 (the group's TRUE total, not just who is online)",
              f"groups={payload[0]} members={payload[1]}")

    # An OWNER additionally sees people they have invited but who have not
    # accepted -- that is their own "Inviting into group" state and it is
    # deliberately private to them, so M legitimately differs by role. Pinned
    # here so a future change to that rule is a decision rather than a surprise.
    conn = accounts.connect(DB)
    mid4 = accounts.add_member(conn, "GRPCOUNT", "pending1", "pw")
    accounts.set_handle(conn, mid4, "Wiccaan")
    accounts.add_group_member(conn, gid, "Wiccaan",
                              member_handle=accounts.primary_handle_row(
                                  conn, mid4)["id"], pending=1)
    conn.close()
    seen = {}
    for who in ("Fox", "Cyn"):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid
        count = R._list_count(0x07, 0x0C)
        seen[who] = R._list_payload(0x07, 0x0C,
                                    R._list_paylen(0x07, 0x0C, count))[1]
    check(seen == {"Fox": 4, "Cyn": 3},
          "a pending invitee counts for the OWNER only", repr(seen))

    # --- N: the in-room roster --------------------------------------------- #
    print("\nN -- the in-room roster ->")
    chan = ("#XXL%016X" % gid).encode()
    srv = b"pol-1049-51244.pol.com"
    cas = Sess(b"UL0C0F1HJ", ids["Fox"][0], ip=b"108.55.250.177")
    cyn = Sess(b"UH5GRSV86", ids["Cyn"][0], ip=b"192.0.2.2")
    R.ROOMS.join(chan, cas)
    R.ROOMS.join(chan, cyn)

    names = R._names_line(chan, cas.nick, srv, sess=cas)
    print("      " + names.decode("latin1"))
    roster = names.split(b":", 2)[2].split()
    check(len(roster) == 2, "353 lists exactly the two who joined",
          f"{len(roster)}: {b' '.join(roster).decode()}")
    check({n.lstrip(b"@") for n in roster} == {cas.nick, cyn.nick},
          "and lists the right two")
    check([n for n in roster if n.startswith(b"@")] == [b"@" + cas.nick],
          "the '@' is on the group MASTER, as retail's 353 has it",
          b" ".join(roster).decode())

    lines = R._auth_session_reply(b"WHO " + chan, cas.nick, srv,
                                  peer_ip=cas.peer_ip, sess=cas)
    who_rows = [l.split()[7] for l in (lines or []) if b" 352 " in l]
    check(len(who_rows) == 2, "352 agrees with 353 -- two rows, not three",
          f"{len(who_rows)}: {b' '.join(who_rows).decode()}")
    check(set(who_rows) == {cas.nick, cyn.nick}, "and names the same two")
    flags = {l.split()[7]: l.split()[8] for l in lines if b" 352 " in l}
    check(flags.get(cas.nick, b"").endswith(b"@")
          and not flags.get(cyn.nick, b"@").endswith(b"@"),
          "352 puts the operator flag on the same person 353 does",
          repr(flags))

    # The third member is a member of the group but NOT in the channel, which is
    # the entire point of a counter that reads 2/3. A roster that listed them
    # would make it 3/3 and a member total that dropped them would make it 2/2 --
    # both wrong in a way only this pairing exposes.
    print("\nthe pairing ->")
    R._session_member_id = lambda: ids["Cyn"][0]
    R._session_handle_id = lambda db=None: ids["Cyn"][1]
    count = R._list_count(0x07, 0x0C)
    m = R._list_payload(0x07, 0x0C, R._list_paylen(0x07, 0x0C, count))[1]
    check((len(who_rows), m) == (2, 3),
          "N/M reads 2/3 for a three-member group with two in chat",
          f"{len(who_rows)}/{m}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all N/M checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
