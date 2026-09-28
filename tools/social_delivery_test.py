"""Who receives what: group messages, multi-target sends, group presence.

    python tools/social_delivery_test.py

Pins, after Project Crystal Server's handling of the same requests:

  * a message sent TO A GROUP (type 0x11, the group id in the write's target
    field) is stored once per other member, each copy addressed to them; a
    sender who is not in the group is refused with 0xFF and nothing is stored;
  * 3:4 stores one copy per target and refuses more than 20 targets;
  * a presence change reaches group members who are not friends, with the
    group-scoped record only; pending invitees and friends (who already get
    the pair) are not in that set; `only_handle` limits a broadcast to one of
    the subject's handles;
  * a handle switch is an offline for the old handle and an online for the
    new one.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="social-delivery-")
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_GROUP_CTL"] = os.path.join(TMP, "no-such.ctl")
os.environ["POL_PRESENCE_PUSH"] = "1"
for k in ("POL_GROUP_MESSAGES", "POL_MULTI_WRITE", "POL_PRESENCE_GROUPS",
          "POL_LOBBY_REFUSALS"):
    os.environ.pop(k, None)

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []
WORLD = "127.0.0.1"


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


STORED = []          # (path, data)
ANNOUNCED = []       # path


R._resource_store = lambda path, data, *a, **kw: STORED.append((path, bytes(data)))
R._mail_announce = lambda path, *a, **kw: ANNOUNCED.append(path)


def lobby_request(op1, op2, body=b""):
    hdr = bytearray(0x28)
    hdr[0], hdr[1], hdr[2] = 0x02, op1, op2
    struct.pack_into("<I", hdr, 4, len(body))
    return bytes(hdr) + body


def message_path(sender_guid, recipient_guid, kind):
    """An `O/m/` path whose header is a message record of `kind`."""
    rec = bytearray(0x48)
    struct.pack_into("<Q", rec, 0x00, sender_guid ^ R._PUSH_GUID_MASK)
    struct.pack_into("<Q", rec, 0x08, recipient_guid ^ R._PUSH_GUID_MASK)
    rec[0x10:0x16] = b"Sender"
    rec[0x20:0x25] = b"Hello"
    struct.pack_into("<I", rec, 0x34, 1790000000)
    struct.pack_into("<I", rec, 0x38, 12)
    struct.pack_into("<H", rec, 0x3E, kind)
    struct.pack_into("<I", rec, 0x40, 0x000203E8)
    return R._MAIL_PATH_PREFIX + R._b64encode(bytes(rec))


class Sess:
    alive = True
    srv = b"srv"

    def __init__(self, nick):
        self.nick = nick
        self.lines = []

    def send(self, lines):
        self.lines += list(lines)
        return True


def main():
    conn = accounts.connect(DB)
    accounts.create_polid(conn, "SOCDEL", "pw", area_kbn="00", login_pf="01")
    ids = {}
    for name in ("Olive", "Mika", "Nico", "Pell", "Mallory", "Frank"):
        mid = accounts.add_member(conn, "SOCDEL", name.lower(), "pw")
        accounts.set_handle(conn, mid, name)
        ids[name] = (mid, accounts.primary_handle_row(conn, mid)["id"])
    gid = accounts.add_friend(conn, ids["Olive"][1], "CREW", kind=accounts.KIND_GROUP, guid=0)
    accounts.add_group_member(conn, gid, "Olive", member_handle=ids["Olive"][1], cls=5)
    accounts.add_group_member(conn, gid, "Mika", member_handle=ids["Mika"][1], cls=3)
    accounts.add_group_member(conn, gid, "Nico", member_handle=ids["Nico"][1], cls=3)
    accounts.add_group_member(conn, gid, "Pell", member_handle=ids["Pell"][1], cls=3, pending=1)
    # Frank is Mika's friend and not in the group.
    accounts.add_friend(conn, ids["Frank"][1], "Mika", peer_handle=ids["Mika"][1])
    accounts.add_friend(conn, ids["Mika"][1], "Frank", peer_handle=ids["Frank"][1])
    conn.close()
    guid = {n: accounts.handle_guid(ids[n][1]) for n in ids}

    def as_session(who):
        mid, hid = ids[who]
        R._session_member_id = lambda mid=mid: mid
        R._session_handle_id = lambda db=None, hid=hid: hid

    print("a message to a group (type 0x11) ->")
    kind = 0x8000 | (0x11 << 7)
    check(R._mail_kind_type(kind) == 0x11, "the kind word carries type 0x11")
    path = message_path(guid["Mika"], 0, kind)
    data = b"Hello\x07all of you\x00"
    body = bytearray(0x20)
    struct.pack_into("<Q", body, 0x08, gid)
    pt = lobby_request(0x03, 0x01, bytes(body))
    check(R._fetch_subject(pt) == gid, "the group id rides in the target field")
    as_session("Mika")
    STORED.clear()
    ANNOUNCED.clear()
    check(R._group_message_fanout(path, data, pt) is True, "the fan-out handles it")
    got = sorted(R._mail_meta(p)["recipient_guid"] for p, _d in STORED)
    check(got == sorted([guid["Olive"], guid["Nico"]]),
          "one copy each for the owner and the other accepted member, none for "
          "the sender or the pending invitee", repr(got))
    check(all(d == data for _p, d in STORED) and sorted(ANNOUNCED) == sorted(p for p, _d in STORED),
          "each copy carries the object and is announced")
    check(R._lobby_take_refusal(pt) is None, "nothing is refused")
    as_session("Mallory")
    STORED.clear()
    check(R._group_message_fanout(path, data, pt) is True and not STORED,
          "an outsider's group message is not delivered")
    check(R._lobby_take_refusal(pt) == 0xFF, "...and is refused with 0xFF")
    other = message_path(guid["Mika"], guid["Nico"], 0x8000)
    check(R._group_message_fanout(other, data, pt) is False,
          "an ordinary message is left to the normal path")

    print("\n3:4 multi-target write ->")

    def multi(targets, path=None, data=b"Hi\x07there\x00"):
        body = bytearray(0x398)
        body[0] = len(targets)
        for i, t in enumerate(targets[:20]):
            struct.pack_into("<Q", body, 0x170 + 8 * i, t)
        p = (path or message_path(guid["Olive"], 0, 0x8000)).encode()
        body[0x210:0x210 + len(p)] = p
        struct.pack_into("<I", body, 0x394, len(data))
        return lobby_request(0x03, 0x04, bytes(body) + data)

    as_session("Olive")
    STORED.clear()
    pt = multi([guid["Mika"], guid["Nico"], 0x123456789])
    check(R.capture_multi_write(pt) is True, "3:4 is captured")
    got = sorted(R._mail_meta(p)["recipient_guid"] for p, _d in STORED)
    check(got == sorted([guid["Mika"], guid["Nico"]]),
          "one copy per target we know; an unknown id gets none", repr(got))
    STORED.clear()
    pt = multi([guid["Mika"]] * 21)
    R.capture_multi_write(pt)
    check(not STORED and R._lobby_take_refusal(pt) == 0xFF,
          "21 targets: refused with 0xFF, nothing stored")
    check(R._lobby_paylen(0x03, 0x04, multi([guid["Mika"]])) == 0,
          "the 3:4 reply is header-only")

    print("\npresence for group members who are not friends ->")
    c = accounts.connect(DB)
    got = sorted((g, m) for g, _sh, m, _wh in
                 R._group_only_watchers(c, ids["Mika"][0]))
    got_ex = sorted(m for _g, _sh, m, _wh in
                    R._group_only_watchers(c, ids["Mika"][0], exclude_members={ids["Olive"][0]}))
    c.close()
    check(got == sorted([(gid, ids["Olive"][0]), (gid, ids["Nico"][0])]),
          "Mika's group-only watchers are Olive and Nico (not Pell, pending; "
          "not Frank, not in the group)", repr(got))
    check(got_ex == [ids["Nico"][0]], "a friend watcher passed in is excluded")
    sess = {n: Sess(("U" + n.upper()).encode()[:9].ljust(9, b"X")) for n in ids}
    real = R.PRESENCE.sessions_for
    R.PRESENCE.sessions_for = lambda m: [s for n, s in sess.items() if ids[n][0] == int(m)]
    try:
        R._broadcast_presence(ids["Mika"][0], "online")
        check(sess["Frank"].lines, "Mika's friend Frank hears it (the friend path)")
        for n in ("Olive", "Nico"):
            recs = [R._b64decode(l.split(b" :", 1)[1].decode()[96:]) for l in sess[n].lines]
            check(len(recs) == 1 and recs[0][0] & R._PUSH_F_OBJECT
                  and struct.unpack_from("<I", recs[0], 8)[0] == gid,
                  f"group member {n} gets ONE group-scoped record naming the group",
                  repr([r[:12].hex() for r in recs]))
        check(not sess["Pell"].lines and not sess["Mallory"].lines,
              "the pending invitee and the outsider hear nothing")
        for s in sess.values():
            s.lines.clear()
        os.environ["POL_PRESENCE_GROUPS"] = "0"
        try:
            R._broadcast_presence(ids["Mika"][0], "online")
            check(not sess["Olive"].lines and not sess["Nico"].lines,
                  "POL_PRESENCE_GROUPS=0: group-only members hear nothing again")
        finally:
            del os.environ["POL_PRESENCE_GROUPS"]
        for s in sess.values():
            s.lines.clear()
        R._broadcast_presence(ids["Mika"][0], "online", only_handle=999999)
        check(not any(s.lines for s in sess.values()),
              "only_handle for a handle nobody watches reaches nobody")
    finally:
        R.PRESENCE.sessions_for = real

    print("\nhandle switch ->")
    calls = []
    real_bp = R._broadcast_presence
    R._broadcast_presence = lambda m, st, **kw: calls.append((m, st, kw.get("only_handle"))) or 1
    try:
        n = R._push_deliver_handleswitch({"kind": "handleswitch", "member": 7,
                                          "old": 11, "new": 12})
        check(calls == [(7, "offline", 11), (7, "online", 12)] and n == 2,
              "offline for the old handle, then online for the new", repr(calls))
        calls.clear()
        R._push_deliver_handleswitch({"kind": "handleswitch", "member": 7,
                                      "old": 12, "new": 12})
        check(not calls, "the same handle again is not a switch")
    finally:
        R._broadcast_presence = real_bp

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all social delivery checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
