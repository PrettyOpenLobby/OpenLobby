#!/usr/bin/env python3
"""polcore.blobs, the saved-resource store, against a real PostgreSQL; and the
account deletion that has to take a member's saves with it.

    python tests/test_polcore_blobs.py

Uses tools/pgtest.py for a throwaway database (Docker, or
POL_TEST_DATABASE_URL). Without one it fails: the store is the database.
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import pgtest  # noqa: E402

TMP = tempfile.mkdtemp(prefix="blobtest-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
pgtest.use_fresh_database()

import accounts  # noqa: E402
from polcore import blobs, db  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db.migrate(log=lambda *_: None)

print("names: a row is exactly one old resources/ file")
chk("member file", blobs.split_name("7.U_g_TM0DataFile.bin"), ("7", "U_g_TM0DataFile.bin"))
chk("mail file", blobs.split_name("m.abc~d.bin.read"), ("mail", "abc~d.bin.read"))
chk("subject-keyed list", blobs.split_name("s1f.b_g_PTL.bin"), ("s1f", "b_g_PTL.bin"))
chk("a name with no dot is not a resource", blobs.split_name("README"), None)
for name in ("7.U_g_x.bin", "m.tok.bin.stale", "shared.u_account.bin",
             "s2a.b_g_ZL.bin", "jan.7.stats.json"):
    chk("round trip %s" % name, blobs.file_name(*blobs.split_name(name)), name)

print("put / get / stat")
chk("missing object", blobs.get("7", "nope.bin"), None)
chk("missing stat", blobs.stat("7", "nope.bin"), None)
blobs.put("7", "save.bin", b"\x00\x01first")
chk("get returns the bytes", blobs.get("7", "save.bin"), b"\x00\x01first")
st = blobs.stat("7", "save.bin")
chk("stat has the size", st.size, 7)
chk("stat has a recent updated_at", abs(st.updated_at - time.time()) < 60, True)
time.sleep(0.05)
blobs.put("7", "save.bin", b"second")
chk("put replaces", blobs.get("7", "save.bin"), b"second")
chk("and moves updated_at on", blobs.stat("7", "save.bin").updated_at > st.updated_at, True)
chk("a bytearray is stored as bytes", (blobs.put("shared", "x.bin", bytearray(b"ab")),
                                       blobs.get("shared", "x.bin"))[1], b"ab")
chk("an empty object is still an object", (blobs.put("7", "empty.bin", b""),
                                           blobs.stat("7", "empty.bin").size)[1], 0)
chk("exists", (blobs.exists("7", "save.bin"), blobs.exists("7", "nope.bin")),
    (True, False))

print("member link: only when the member exists")
conn = accounts.connect()
accounts.create_polid(conn, "UBLOBTEST", "x")
mid = int(accounts.add_member(conn, "UBLOBTEST", "blobber", "x"))
conn.commit()
conn.close()
blobs.put(str(mid), "U_g_save.bin", b"real member")
row = db.query_one("SELECT member_id FROM blob WHERE scope = %s AND path = %s",
                   (str(mid), "U_g_save.bin"))
chk("a real member's row is linked to them", row["member_id"], mid)
row = db.query_one("SELECT member_id FROM blob WHERE scope = '7' AND path = 'save.bin'")
chk("an unknown member id is stored, unlinked", row["member_id"], None)
row = db.query_one("SELECT member_id FROM blob WHERE scope = 'shared' AND path = 'x.bin'")
chk("a non-member scope is unlinked", row["member_id"], None)

print("rename (os.replace's rules)")
blobs.put("mail", "t.bin", b"message")
before = blobs.stat("mail", "t.bin").updated_at
time.sleep(0.05)
chk("rename of a present object", blobs.rename("mail", "t.bin", "mail", "t.bin.read"), True)
chk("the old name is gone", blobs.get("mail", "t.bin"), None)
chk("the bytes are under the new one", blobs.get("mail", "t.bin.read"), b"message")
chk("the write time is kept", blobs.stat("mail", "t.bin.read").updated_at, before)
chk("rename of a missing object", blobs.rename("mail", "t.bin", "mail", "x"), False)
blobs.put("mail", "u.bin", b"new")
blobs.rename("mail", "t.bin.read", "mail", "u.bin")
chk("rename replaces the target", blobs.get("mail", "u.bin"), b"message")
blobs.rename("mail", "u.bin", str(mid), "moved.bin")
row = db.query_one("SELECT member_id FROM blob WHERE scope = %s AND path = 'moved.bin'",
                   (str(mid),))
chk("moving into a member scope links it", row["member_id"], mid)

print("listing and deleting")
for p in ("a.bin", "a.bin.read", "b.bin", "O_m_x.bin", "c_d.bin", "cxd.bin"):
    blobs.put("9", p, b"z")
chk("by scope", [i.path for i in blobs.listing(scope="9")],
    ["O_m_x.bin", "a.bin", "a.bin.read", "b.bin", "c_d.bin", "cxd.bin"])
chk("by suffix", [i.path for i in blobs.listing(scope="9", suffix=".bin")],
    ["O_m_x.bin", "a.bin", "b.bin", "c_d.bin", "cxd.bin"])
chk("by prefix, across scopes", [(i.scope, i.path) for i in blobs.listing(prefix="O_m_")],
    [("9", "O_m_x.bin")])
chk("`_` in a prefix is literal, not a wildcard",
    [i.path for i in blobs.listing(scope="9", prefix="c_")], ["c_d.bin"])
chk("by exact path, any scope", [i.scope for i in blobs.listing(path="a.bin")], ["9"])
chk("listing reports sizes", {i.size for i in blobs.listing(scope="9")}, {1})
chk("delete", (blobs.delete("9", "b.bin"), blobs.delete("9", "b.bin")), (True, False))
chk("delete_prefix", sorted(i.path for i in blobs.delete_prefix("9", "a.")),
    ["a.bin", "a.bin.read"])
chk("the rest of the scope stays", [i.path for i in blobs.listing(scope="9")],
    ["O_m_x.bin", "c_d.bin", "cxd.bin"])

print("concurrent writers: every reader sees a whole object")
blobs.put("shared", "race.bin", b"A" * 50000)
seen, stop = set(), threading.Event()


def reader():
    while not stop.is_set():
        seen.add(frozenset(blobs.get("shared", "race.bin")))


def writer(ch):
    for _ in range(20):
        blobs.put("shared", "race.bin", ch * 50000)


ts = [threading.Thread(target=reader) for _ in range(2)]
ws = [threading.Thread(target=writer, args=(c,)) for c in (b"B", b"C")]
for t in ts + ws:
    t.start()
for t in ws:
    t.join()
stop.set()
for t in ts:
    t.join()
chk("no reader ever saw two writers' bytes mixed", all(len(s) == 1 for s in seen), True)

print("deleting an account deletes its saves, and nothing on disk")
blobs.put(str(mid), "tm_collection.json", b"{}")
blobs.put("s%x" % mid, "b_g_PTL.bin", b"lobby list")   # a subject scope, not theirs
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
canary = os.path.join(os.environ["POL_RESOURCE_DIR"], "%d.canary.bin" % mid)
with open(canary, "wb") as f:
    f.write(b"a file the deletion must not touch")
conn = accounts.connect()
fp = accounts.delete_polid(conn, "UBLOBTEST")
conn.close()
chk("the footprint names the member's saves by their old file names",
    fp["files"], sorted(["%d.U_g_save.bin" % mid, "%d.moved.bin" % mid,
                         "%d.tm_collection.json" % mid]))
chk("their rows are gone", blobs.listing(scope=str(mid)), [])
chk("a lobby list keyed by a subject that happens to match is kept",
    blobs.get("s%x" % mid, "b_g_PTL.bin"), b"lobby list")
chk("other members' saves are kept", blobs.get("7", "save.bin"), b"second")
chk("the filesystem is not touched", os.path.exists(canary), True)
chk("purge_member_files on its own (the CLI path) removes by scope",
    (blobs.put("7", "late.bin", b"x"), accounts.purge_member_files([7]))[1],
    ["7.empty.bin", "7.late.bin", "7.save.bin"])
chk("an empty member list removes nothing", accounts.purge_member_files([]), [])

print("\n%s" % ("all passed" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
