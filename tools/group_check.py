"""Does our 07:12 reply carry a membership the client will actually install?

Runs the CLIENT's own count-block validation and member-record walk over our own
payload, offline, against a real (temporary) accounts DB -- no client, no
container, no live server.

The three things this covers that `smoke_chain.py` cannot judge:

  1. **The count block agrees with the records.** Byte 0 is the group count and
     bytes 1..4 are the per-group MEMBER counts. The installer takes each count
     as its third argument and walks that many 32-byte records
     (`add edi,0x20` @0x37e898f) out of one flat array. If the block promises
     more records than the reply carries, the client walks off the end of one
     group's members into the next group's.
  2. **The client's two ceilings hold.** <= 4 groups (polcore 0x3bb06c0, stride
     0x3098, `cmp ecx,4`) and <= 0x40 members each (obj+0x30, stride 0xC0).
     Exceeding either draws POL-5133 and loses the WHOLE reply, not one row.
  3. **Every member is ACCEPTED.** polcore range-checks the 3-bit class at bit
     50 to 2..5 (0x37e87b4) and skips anything else; a group whose members are
     all skipped never gets its valid bit (0x37e89c6) and does not render. A
     class of 2 additionally marks the group UNUSABLE (0x37e7d10), so a group
     that installs can still be invisible -- this checks for both.

Run after any change to `_group_members`, `_group_member_record`,
`_group_record` or `accounts.list_group_members`. Exits non-zero if a group
would fail to render.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

DB = os.path.join(tempfile.mkdtemp(prefix="group-check-"), "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups")
os.environ.setdefault("POL_GROUP_MEMBERS", "1")
# The control file tunes live sweeps; a stale one must not silently decide the
# result of a test. Point it at a path that cannot exist.
os.environ["POL_GROUP_CTL"] = os.path.join(os.path.dirname(DB), "no-such.ctl")

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

MEMBER_REC = 0x20
CLASS_BIT, SLOT_BIT = 50, 53

# --- the fixture ----------------------------------------------------------- #
# Three groups exercising the three cases that actually differ: a group with
# real stored members, a group with NONE (the owner fallback -- what every group
# created before membership existed looks like), and a group deliberately
# overfilled past the client's 64 slots.
conn = accounts.connect(DB)
accounts.create_polid(conn, "GRPPOLID", "pw-polid", area_kbn="00", login_pf="01")
MID = accounts.add_member(conn, "GRPPOLID", "grpmember", "pw-member")
accounts.set_handle(conn, MID, "Owner")
HID = accounts.primary_handle_row(conn, MID)["id"]
# a second local handle, so at least one member resolves to a REAL handle guid
accounts.set_handle(conn, MID, "Friendly", primary=False)
PEER = conn.execute("SELECT id FROM handle WHERE handle_name = 'Friendly'"
                    ).fetchone()["id"]

g_full = accounts.add_friend(conn, HID, "FoxGoons", kind=accounts.KIND_GROUP, guid=0)
g_empty = accounts.add_friend(conn, HID, "FoxGroup", kind=accounts.KIND_GROUP, guid=0)
g_over = accounts.add_friend(conn, HID, "FoxFriends", kind=accounts.KIND_GROUP, guid=0)

accounts.add_group_member(conn, g_full, "Owner", member_handle=HID)
accounts.add_group_member(conn, g_full, "Friendly", member_handle=PEER)
accounts.add_group_member(conn, g_full, "Outsider")            # non-local
refused = 0
for i in range(accounts.GROUP_MEMBER_MAX + 6):
    if not accounts.add_group_member(conn, g_over, f"Filler{i}"):
        refused += 1
conn.close()

R._session_member_id = lambda: MID
R._session_handle_id = lambda db=None: HID

# --- build the reply exactly as the lobby does ----------------------------- #
count = R._list_count(7, 0x0C)
n = R._list_paylen(7, 0x0C, count)
payload = R._list_payload(7, 0x0C, n)

rec, _width, cap = R._LOBBY_LIST[(7, 0x0C)]
groups = [g[1] for g in R._groups_for_list(count)]
counts = list(payload[1:5])

print(f"groups served : {count} {groups}")
print(f"payload       : {len(payload)}B declared {n}B")
print(f"count block   : {payload[:8].hex(' ')}")
print(f"member counts : {counts}")
print(f"over-full group refused {refused} member(s) past the "
      f"{accounts.GROUP_MEMBER_MAX} slot ceiling")
print()

ok = True


def check(label, got, want):
    global ok
    good = got == want
    ok = good and ok
    print(f"  {'OK  ' if good else 'FAIL'} {label}: {got!r}" +
          ("" if good else f"  (want {want!r})"))


# --- 1. framing ------------------------------------------------------------ #
check("payload length matches the declared length", len(payload), n)
check("count block byte 0 is the group count", payload[0], count)
check("length = 8 + groups*136 + members*32",
      len(payload), 8 + count * rec + sum(counts) * MEMBER_REC)

# --- 2. the client's ceilings ---------------------------------------------- #
check("group count within the client's four slots", payload[0] <= 4, True)
check("every member count within the 0x40 slots",
      all(c <= 0x40 for c in counts), True)
check("group count within the per-read cap", count <= cap, True)

# --- 3. walk the members exactly as the installer does --------------------- #
off = 8 + count * rec
seen = {}
classes = set()
for g in range(count):
    mems = []
    for _ in range(counts[g]):
        r = payload[off:off + MEMBER_REC]
        # +0x00 IS THE RAW guid ON THE WIRE. The client reads it as
        # `cft_0355(stored) = stored ^ K` (K = the PER-SESSION guid key, polcore
        # `[0x0386A848]`) and compares against its own id, which it holds already
        # mangled as `client_guid ^ K` -- so the match needs `stored ^ K ==
        # client_guid ^ K`, i.e. **stored = the raw guid**. MEASURED LIVE
        # 2026-08-19 (`tools/readself.py`): own_id ^ K == client_guid exactly.
        # The earlier `^ _PUSH_GUID_MASK` here encoded the f46e2295 mask, which
        # was a coincidence of one SE capture, not the transform the client runs.
        guid = struct.unpack_from("<Q", r, 0x00)[0]
        packed = struct.unpack_from("<Q", r, 0x08)[0]
        cls = (packed >> CLASS_BIT) & 7
        slot = (packed >> SLOT_BIT) & 0x3F
        name = r[0x10:0x1F].rstrip(b"\0").decode("cp932", "replace")
        mems.append((name, cls, slot, guid))
        classes.add(cls)
        off += MEMBER_REC
    seen[groups[g]] = mems

check("the walk consumed the whole payload", off, len(payload))
for gname, mems in seen.items():
    print(f"    {gname}: {len(mems)} member(s) -> "
          + ", ".join(f"{n_}(class {c})" for n_, c, _s, _g in mems[:4])
          + (" ..." if len(mems) > 4 else ""))
print()

check("every member class is in polcore's accepted 2..5",
      all(2 <= c <= 5 for c in classes), True)
# THE HAZARD IS CLASS 2 ON *ME*, NOT CLASS 2 ANYWHERE. This used to assert
# `2 not in classes`, which was right while 3 was our conservative default and
# wrong from 2026-08-16, when `_group_role` started mapping a stored 3 down to 2
# (3 is not a role -- it appears nowhere in either SE capture). The blanket
# check then failed on every run and took the 15 checks below dark with it.
#
# The real gate is narrower: polcore copies a member's class into the GROUP's
# flags only for the member whose guid equals the CLIENT'S OWN id (0x37e8917),
# and a group is reported unusable when those flags carry 2 (0x37e7d10). So
# class 2 is correct -- and SE's own value -- for everybody else, and fatal only
# on the session's own row, which `_group_role` gives 5 (master) precisely
# because it is the owner.
own_guid = accounts.handle_guid(HID)
check("the member who IS the client does not carry class 2 (it would install "
      "the group, then mark it unusable)",
      [c_ for _n, c_, _s, g in
       [m for mems in seen.values() for m in mems] if g == own_guid and c_ == 2],
      [])
check("...and that member is the master, which is what gates the invite button",
      sorted({c_ for _n, c_, _s, g in
              [m for mems in seen.values() for m in mems] if g == own_guid}),
      [accounts.GROUP_CLASS_MASTER])
check("the group with stored members serves them",
      [m[0] for m in seen["FoxGoons"]], ["Owner", "Friendly", "Outsider"])
check("a group with NO stored members falls back to the owner, not empty",
      [m[0] for m in seen["FoxGroup"]], ["Owner"])
check("the over-full group is capped at the client's ceiling",
      len(seen["FoxFriends"]), accounts.GROUP_MEMBER_MAX)
check("the ceiling refused the surplus at write time", refused, 6)

# A local member's guid must be the handle's real one -- polcore compares a
# member's guid against the CLIENT'S OWN id to decide whose class propagates
# into the group flags, so a synthetic value silently changes that outcome.
check("a local member carries its handle's real guid",
      dict((m[0], m[3]) for m in seen["FoxGoons"])["Friendly"],
      accounts.handle_guid(PEER))
check("a non-local member carries the same stable id a friend would get",
      dict((m[0], m[3]) for m in seen["FoxGoons"])["Outsider"],
      accounts._peer_guid(None, "Outsider"))

print()
print("group_check: OK" if ok else "group_check: FAILED")
sys.exit(0 if ok else 1)
