"""Does an AWAY member show `G@` in the in-room 352, the way retail does?

Two findings from the two-sided retail capture of 2026-08-19,
pinned together because one feeds the other:

  * **4:5 KChangeMyStatus carries a presence code** at body +0x16 -- 01 Online,
    02 Away, 05 Invisible -- and we had never read it. The bytes below are SE's
    own, straight off the capture.
  * **SE's 352 WHO reflects that code as the RFC 1459 H(ere)/G(one) letter.** Fox
    was AFK and their row read `G@`; ours hardcoded `H@`, so an away member
    showed as present in every room they were in.

The 352 is built by `_auth_session_reply`, which needs live ChatSessions and a
RoomRegistry, so this drives the real builder against stub sessions rather than
re-implementing the line. What it checks:

  1. the status byte decodes at the offset the capture puts it at, and the
     zone reading beside it is NOT disturbed (they share the same 40 bytes);
  2. `H` for an online/unknown member, `G` for away and for invisible;
  3. the letter reaches the actual 352 line, with the operator `@` still glued
     to it (`G@`, not `G @` and not `@G`);
  4. the flag is PER ROW -- one away member and one present member in the same
     room produce different letters, which is the whole thing a constant could
     never say;
  5. the IRC `AWAY` fallback works for a client that never sent a 4:5;
  6. logout clears the latch, so yesterday's "Away" does not answer for today.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="status-who-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_MEMBER_STATUS_FILE"] = os.path.join(TMP, "member-status.json")
os.environ["POL_TITLE_ZONE_FILE"] = os.path.join(TMP, "title-zone.json")

import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def status_body(code, zone=1000):
    """A 40-byte 4:5 body in SE's shape.

    From the live capture: the body is
    mostly zero, the zone is the u16 at +0x14 (`E8 03` = 1000, the Viewer) and
    the status code is the single byte right behind it, followed by `00 00 01`.
    """
    body = bytearray(40)
    struct.pack_into("<H", body, 0x14, zone)
    body[0x16] = code
    body[0x19] = 1
    return bytes(body)


class Sess:
    """The two fields `_who_here_flag` reads off a ChatSession, and no more.

    A real ChatSession owns a socket and a cipher; the WHO builder only ever
    asks it for its nick, its peer address, its member row and its away flag.
    """

    def __init__(self, nick, member_id=None, away=False, ip=b"192.0.2.1"):
        self.nick = nick
        self.peer_ip = ip
        self.member = {"id": member_id} if member_id is not None else None
        self.away = away
        self.alive = True


def main():
    print("the 4:5 body decodes ->")
    for name, code in (("online", R._STATUS_ONLINE), ("away", R._STATUS_AWAY),
                       ("invisible", R._STATUS_INVISIBLE)):
        body = status_body(code)
        check(R._status_frame_code(body) == code,
              f"status byte reads back for {name}", f"{code:#04x}")
    # The zone shares these 40 bytes; reading the status must not move it.
    zone, in_title = R._status_frame_zone(status_body(R._STATUS_AWAY, zone=2))
    check((zone, in_title) == (2, True),
          "the zone reading beside it is undisturbed", f"zone={zone}")
    check(R._status_frame_code(b"\x00" * 8) is None,
          "a body too short to hold the code answers None")

    print("\naway-ness, by code ->")
    check(not R._status_is_away(R._STATUS_ONLINE), "01 Online is HERE")
    check(R._status_is_away(R._STATUS_AWAY), "02 Away is GONE")
    check(R._status_is_away(R._STATUS_INVISIBLE), "05 Invisible is GONE")
    check(not R._status_is_away(0),
          "no record at all is HERE, not GONE (we were never told)")

    print("\nthe store crosses the container boundary ->")
    check(R._publish_member_status(7, R._STATUS_AWAY), "a status publishes")
    check(R._member_status(7) == R._STATUS_AWAY, "and reads back",
          f"{R._member_status(7):#04x}")
    check(not R._publish_member_status(7, R._STATUS_AWAY),
          "an unchanged status does NOT rewrite the file "
          "(4:5 arrives twice per toggle)")
    check(R._publish_member_status(7, R._STATUS_ONLINE), "a CHANGE does write")
    check(R._member_status(99) == 0, "a member we hold nothing for reads 0")

    print("\nthe letter ->")
    R._publish_member_status(7, R._STATUS_AWAY)
    check(R._who_here_flag(Sess(b"AWAYGUY", 7)) == b"G", "an away member is G")
    check(R._who_here_flag(Sess(b"HEREGUY", 8)) == b"H",
          "a member with no record is H")
    R._publish_member_status(8, R._STATUS_INVISIBLE)
    check(R._who_here_flag(Sess(b"HIDDEN", 8)) == b"G", "an invisible member is G")
    check(R._who_here_flag(Sess(b"IRCAWAY", None, away=True)) == b"G",
          "the IRC AWAY fallback answers G with no 4:5 at all")

    print("\nthe 352 line the client actually gets ->")
    # A created chat ROOM, not a `#XXL` group channel: in a group the '@' comes
    # from the group MASTER (`_group_op_nicks`), which needs a whole DB fixture
    # and is not what this file is about. A room's '@' is its creator, so the
    # first joiner below carries it -- which is exactly what makes `G@` (rather
    # than a bare `G`) testable here.
    chan = b"#01CUJZNNNNO0BKPCOEH1NWOAE2ENNNNNNNNNNNNN3N1N"
    srv = b"pol-1049-51244.pol.com"
    R._publish_member_status(7, R._STATUS_AWAY)
    R._publish_member_status(8, R._STATUS_ONLINE)
    away = Sess(b"UL0C0F1HJ", 7, ip=b"108.55.250.177")
    here = Sess(b"UD5PUQZGA", 8, ip=b"192.0.2.2")
    R.ROOMS.join(chan, away)
    R.ROOMS.join(chan, here)
    lines = R._auth_session_reply(b"WHO " + chan, away.nick, srv,
                                  peer_ip=away.peer_ip, sess=away)
    rows = [l for l in (lines or []) if b" 352 " in l]
    print("      " + "\n      ".join(l.decode("latin1") for l in rows))
    # RFC 1459 352: `:<srv> 352 <target> <chan> <user> <host> <server> <nick>
    # <H|G>[*][@|+] :<hops> <real>` -- so split()[7] is the nick and [8] the
    # flags. (Getting this wrong reads the HOST as the nick and every lookup
    # misses, which looks exactly like the server serving nothing.)
    got = {}
    for l in rows:
        f = l.split()
        got[f[7]] = f[8]
    check(got.get(b"UL0C0F1HJ", b"") == b"G@",
          "the AWAY member (and room owner) is served G@ -- SE's own line",
          got.get(b"UL0C0F1HJ", b"-").decode())
    check(got.get(b"UD5PUQZGA", b"") == b"H",
          "the present member is still H", got.get(b"UD5PUQZGA", b"-").decode())
    check(len(set(got.values())) == 2,
          "the flag is PER ROW, not one constant for the room", repr(got))

    print("\nlogout clears the latch ->")
    R._publish_member_status(7, None)
    check(R._member_status(7) == 0, "a cleared member reads 0 again")
    check(R._who_here_flag(Sess(b"UL0C0F1HJ", 7)) == b"H",
          "and their WHO letter goes back to H")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("all status/WHO checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
