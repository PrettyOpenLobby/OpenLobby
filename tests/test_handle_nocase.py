#!/usr/bin/env python3
"""A friend add that names a handle in the wrong case (POL_HANDLE_NOCASE).

    python tests/test_handle_nocase.py

Seen live 2026-09-27: Amara asked "kelpie" (the handle is `kElpie`). The exact
name lookup found nobody, so no mirror row was made and the request went to an
address that resolves to no handle. With the knob on (default) an unambiguous
case-insensitive match resolves; an exact match still wins; an ambiguous one
resolves to nobody, as before; POL_HANDLE_NOCASE=0 is exact-only.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="nocasetest-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
os.environ.pop("POL_HANDLE_NOCASE", None)

import accounts  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def row(db, hid, name):
    r = db.execute("SELECT status, peer_handle FROM friend WHERE handle_id = %s"
                   " AND peer_name = %s", (hid, name)).fetchone()
    return (r["status"], r["peer_handle"]) if r else None


db = accounts.connect()


def hid(name):
    return int(db.execute("SELECT id FROM handle WHERE handle_name = %s",
                          (name,)).fetchone()["id"])


for nm in ("Amara", "kElpie", "Twin"):
    accounts.register_account(db, nm, "Passw0rdTest", contents=(1,))
# An ambiguous pair: two handles that differ only in case. Inserted directly --
# whether sign-up allows it is not this test's business.
mid = db.execute("SELECT member_id FROM handle WHERE handle_name = 'Twin'"
                 ).fetchone()["member_id"]
db.execute("INSERT INTO handle (member_id, handle_name, is_primary, created_at)"
           " VALUES (%s, 'TWIN', 0, '2026-09-27')", (mid,))
db.commit()
A, E = hid("Amara"), hid("kElpie")

print("handle_by_name")
chk("exact", accounts.handle_by_name(db, "kElpie")["id"], E)
chk("wrong case resolves", accounts.handle_by_name(db, "kelpie")["id"], E)
chk("exact wins over a case twin", accounts.handle_by_name(db, "TWIN")["handle_name"],
    "TWIN")
chk("ambiguous wrong case resolves to nobody", accounts.handle_by_name(db, "twin"),
    None)
chk("unknown name", accounts.handle_by_name(db, "nobody"), None)
os.environ["POL_HANDLE_NOCASE"] = "0"
chk("knob off: wrong case resolves to nobody (the twin)",
    accounts.handle_by_name(db, "kelpie"), None)
chk("knob off: exact still works", accounts.handle_by_name(db, "kElpie")["id"], E)

print("the live case, knob off: Amara asks 'kelpie' -- no mirror (today)")
chk("request_friend", accounts.request_friend(db, A, "kelpie"), "requested")
chk("asker row", row(db, A, "kelpie"), ("pending", None))
chk("no mirror on kElpie", row(db, E, "Amara"), None)

print("knob on: the asker's client re-sends the name -> the mirror is made")
os.environ.pop("POL_HANDLE_NOCASE")
chk("request_friend again", accounts.request_friend(db, A, "kelpie"), "requested")
chk("mirror on kElpie", row(db, E, "Amara"), ("invited", A))
chk("the old row learned whose it is", row(db, A, "kelpie"), ("pending", E))
chk("no second asker row under the real name", row(db, A, "kElpie"), None)

print("kElpie accepts -> both sides active, the asker's row keeps its spelling")
chk("request_friend", accounts.request_friend(db, E, "Amara"), "accepted")
chk("kElpie's row", row(db, E, "Amara"), ("active", A))
chk("Amara's row", row(db, A, "kelpie"), ("active", E))

print("a fresh wrong-case add mirrors at once")
accounts.register_account(db, "Birdie", "Passw0rdTest", contents=(1,))
F = hid("Birdie")
chk("request_friend", accounts.request_friend(db, F, "KELPIE"), "requested")
chk("asker row", row(db, F, "KELPIE"), ("pending", E))
chk("mirror", row(db, E, "Birdie"), ("invited", F))
chk("an ambiguous name mirrors nothing",
    accounts.request_friend(db, F, "twin"), "requested")
chk("no mirror on either twin",
    (row(db, hid("Twin"), "Birdie"), row(db, hid("TWIN"), "Birdie")), (None, None))

print("reconcile heals a pending row whose peer's row spells us exactly")
db.execute("UPDATE friend SET status = 'pending' WHERE handle_id = %s AND peer_name"
           " = 'kelpie'", (A,))
db.commit()
chk("reconcile_pending", accounts.reconcile_pending(db, A), ["kelpie"])

db.close()
print("FAIL: %d check(s)" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
