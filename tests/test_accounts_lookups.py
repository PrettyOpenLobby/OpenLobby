#!/usr/bin/env python3
"""The accounts lookups the title repositories call instead of reading the
account tables themselves: friend_row_by_id, member_created_list,
member_content_id_map, handle_client_guid, and open_session's created_at.

    python tests/test_accounts_lookups.py

Uses tools/pgtest.py for a throwaway database (Docker, or
POL_TEST_DATABASE_URL). Without one it fails.
"""
import datetime
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import pgtest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="lookuptest-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
pgtest.use_fresh_database()

import accounts  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


conn = accounts.connect()
accounts.create_polid(conn, "LKUP2345", "password1")
m1 = int(accounts.add_member(conn, "LKUP2345", "alpha", "password1"))
m2 = int(accounts.add_member(conn, "LKUP2345", "bravo", "password1"))
m3 = int(accounts.add_member(conn, "LKUP2345", "charlie", "password1"))
conn.commit()
h1a = accounts.set_handle(conn, m1, "Alpha")               # primary
h1b = accounts.set_handle(conn, m1, "AlphaAlt", primary=False)
h2a = accounts.set_handle(conn, m2, "Bravo")
h2b = accounts.set_handle(conn, m2, "BravoNew", primary=True)   # promoted later
h3 = accounts.set_handle(conn, m3, "Charlie")


def link(hid, code, slot, cid, status="active"):
    conn.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                 " content_id, status, linked_at) VALUES (%s,%s,%s,%s,%s,%s)",
                 (hid, code, slot, cid, status, "2026-01-01T00:00:00Z"))


# member 1: the primary handle wins over the older-id alternate's lower slot
link(h1b, 2, 0, "40000010")
link(h1a, 2, 1, "40000012")
link(h1a, 2, 0, "40000011")
# member 2: the promoted handle (newer id) is primary, and its only link is
# inactive; the old handle holds an active one
link(h2a, 2, 0, "40000020")
link(h2b, 2, 0, "40000021", status="suspended")
# member 3: only an inactive link, and a different game
link(h3, 2, 0, "40000030", status="closed")
link(h3, 1, 0, "40000031")
conn.commit()

print("member_content_id_map: primary handle first, any status by default")
got = accounts.member_content_id_map(conn, 2)
chk("every member that holds the game", sorted(got), sorted([m1, m2, m3]))
chk("member 1: primary handle, slot 0", got[m1], "40000011")
chk("member 2: the primary handle's link, though inactive", got[m2], "40000021")
chk("member 3: an inactive link counts", got[m3], "40000030")
chk("agrees with member_content_id(active_only=False)",
    all(got[m] == accounts.member_content_id(conn, m, 2, active_only=False)
        for m in got), True)
act = accounts.member_content_id_map(conn, 2, any_status=False)
chk("active only: member 2 falls back to the active link", act.get(m2), "40000020")
chk("active only: member 3 is left out", m3 in act, False)
chk("agrees with member_content_id(active_only=True)",
    all(act[m] == accounts.member_content_id(conn, m, 2) for m in act), True)
chk("another game", accounts.member_content_id_map(conn, 1), {m3: "40000031"})
chk("a game nobody holds", accounts.member_content_id_map(conn, 99), {})

print("member_created_list")
lst = accounts.member_created_list(conn)
chk("one row per member, by id", [m for m, _ in lst], sorted([m1, m2, m3]))
chk("created_at is the stored text",
    all(isinstance(c, str) and c.endswith("Z") for _, c in lst), True)
conn.execute("UPDATE member SET created_at = %s WHERE id = %s",
             ("2020-05-06T07:08:09Z", m2))
conn.commit()
chk("reads what is stored", dict(accounts.member_created_list(conn))[m2],
    "2020-05-06T07:08:09Z")

print("friend_row_by_id")
fid = accounts.add_friend(conn, h1a, "Bravo", peer_handle=h2b)
gid = accounts.add_friend(conn, h2b, "Squad", kind=accounts.KIND_GROUP)
chk("a friend row", accounts.friend_row_by_id(conn, fid),
    ("Bravo", accounts.KIND_FRIEND))
chk("a group row, whoever holds it", accounts.friend_row_by_id(conn, gid),
    ("Squad", accounts.KIND_GROUP))
chk("no such row", accounts.friend_row_by_id(conn, 987654321), None)

print("handle_client_guid")
chk("not seen yet", accounts.handle_client_guid(conn, h1a), 0)
accounts.learn_client_guid(conn, h1a, 0x8C002C1E04BC91)
chk("learned", accounts.handle_client_guid(conn, h1a), 0x8C002C1E04BC91)
chk("no such handle", accounts.handle_client_guid(conn, 987654321), None)

print("open_session(created_at=)")
t_now = accounts.open_session(conn, m1, nick="Alpha", peer_ip="127.0.0.1")
row = accounts.get_session(conn, t_now)
made = datetime.datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
chk("default is now",
    abs((datetime.datetime.utcnow() - made).total_seconds()) < 120, True)
old = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
t_old = accounts.open_session(conn, m1, created_at=old, ttl_seconds=600)
row = accounts.get_session(conn, t_old)
chk("an aware datetime", (row["created_at"], row["expires_at"]),
    ("2026-09-01T12:00:00Z", "2026-09-01T12:10:00Z"))
t_naive = accounts.open_session(conn, m1, created_at=datetime.datetime(2026, 9, 2, 1, 2, 3))
chk("a naive datetime is UTC", accounts.get_session(conn, t_naive)["created_at"],
    "2026-09-02T01:02:03Z")
t_text = accounts.open_session(conn, m1, created_at="2026-09-03T04:05:06Z")
chk("ISO text", accounts.get_session(conn, t_text)["created_at"],
    "2026-09-03T04:05:06Z")
t_off = accounts.open_session(conn, m1, created_at="2026-09-03T06:05:06+02:00")
chk("an offset is converted to UTC", accounts.get_session(conn, t_off)["created_at"],
    "2026-09-03T04:05:06Z")
# the reason the parameter exists: a backdated session is not fresh evidence
stale = accounts.open_session(
    conn, m3, peer_ip="127.0.0.9",
    created_at=datetime.datetime.now(datetime.timezone.utc)
    - datetime.timedelta(hours=2), ttl_seconds=86400)
chk("an old session is outside a one-hour window",
    accounts.session_by_ip(conn, "127.0.0.9", 3600), None)
chk("and inside a three-hour one",
    accounts.session_by_ip(conn, "127.0.0.9", 3 * 3600)[0], m3)
try:
    accounts.open_session(conn, m1, created_at=12345)
    chk("a number is refused", "accepted", "TypeError")
except TypeError:
    chk("a number is refused", "TypeError", "TypeError")
conn.close()

print()
print("PASS" if not bad else "FAIL: %d check(s)" % bad)
sys.exit(1 if bad else 0)
