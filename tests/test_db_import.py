#!/usr/bin/env python3
"""tools/db_import.py against a /data tree written by the code that ran before
PostgreSQL.

    python tests/test_db_import.py

The fixture is made the way a real server made it: accounts.db, admin.db and
discord_links.db are created by the pre-PostgreSQL accounts.py, adminusers.py
and discordlink.py, taken from git (OLD_COMMIT, the commit before the move),
and run from a temporary directory. On top of that go the rows SQLite let
through and PostgreSQL will not (children of missing parents), the two backup
tables the old rebuilds kept, an unknown table, a value its column cannot
hold, resources/ files and live files that are left behind.

It is imported into a throwaway database (tools/pgtest.py) and checked: row
counts, ids and sequences, the group member order, the orphans in the report,
the saves through the resource store, a second run that changes nothing, a
dry run that writes nothing, --merge, the refusals, and a source tree that is
byte for byte what it was.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "services"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pgtest  # noqa: E402

#: The last commit whose account code wrote SQLite (the `core-split` branch).
OLD_COMMIT = "9e58ab97f86faa91d2aa19d4abd8fe5ad1bf70b2"
OLD_FILES = ("services/accounts.py", "services/adminusers.py",
             "services/discordlink.py")

BASE = tempfile.mkdtemp(prefix="dbimport-")
os.environ["POL_DATA_DIR"] = os.path.join(BASE, "run-data")
os.environ["POL_LOG_DIR"] = os.path.join(BASE, "run-logs")
os.environ["POL_RESOURCE_DIR"] = os.path.join(BASE, "run-data", "resources")
os.environ["POL_MAIL_NORMALISE"] = "0"
for d in ("run-data", "run-logs"):
    os.makedirs(os.path.join(BASE, d), exist_ok=True)
URL = pgtest.use_fresh_database()

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


# --------------------------------------------------------------------------- #
# the old code
# --------------------------------------------------------------------------- #
def old_code():
    out = os.path.join(BASE, "old")
    os.makedirs(out)
    commit = OLD_COMMIT
    probe = subprocess.run(["git", "cat-file", "-e", commit + "^{commit}"],
                           cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        print("FAIL: this suite needs the pre-PostgreSQL account code at %s "
              "(a shallow clone lacks it: git fetch --unshallow)" % commit)
        sys.exit(1)
    for f in OLD_FILES:
        src = subprocess.run(["git", "show", "%s:%s" % (commit, f)], cwd=ROOT,
                             capture_output=True, check=True).stdout
        with open(os.path.join(out, os.path.basename(f)), "wb") as fh:
            fh.write(src)
    return out


FIXTURE = r'''
import json, os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
data = sys.argv[2]
os.environ["POL_DB_POOL"] = "0"
os.environ["POL_DATA_DIR"] = data
import accounts as A, adminusers as AU, discordlink as DL

p = os.path.join(data, "accounts.db")
c = A.connect(p)
A.create_polid(c, "ABCD2345", "password1")
A.create_polid(c, "EFGH6789", "password2")
m1 = A.add_member(c, "ABCD2345", "alice", "password1")
m2 = A.add_member(c, "ABCD2345", "bob", "password1")
m3 = A.add_member(c, "EFGH6789", "carol", "password2")
m4 = A.add_member(c, "EFGH6789", "dave", "password2")
c.commit()
h1 = A.set_handle(c, m1, "Alice")
h2 = A.set_handle(c, m2, "Bob")
h3 = A.set_handle(c, m3, "Carol")
h3b = A.set_handle(c, m3, "CarolAlt", primary=False)
A.grant_content(c, m1, 2)
A.link_content_to_handle(c, h1, 2)
A.grant_content(c, m3, 1)
A.link_content_to_handle(c, h3, 1)
f12 = A.add_friend(c, h1, "Bob", peer_handle=h2)
f21 = A.add_friend(c, h2, "Alice", peer_handle=h1)
g = A.add_friend(c, h1, "Guild", kind=A.KIND_GROUP)
for name, hid in (("Zed", None), ("Alice", h1), ("Mo", None), ("Bob", h2)):
    A.add_group_member(c, g, name, member_handle=hid)
A.set_profile(c, "ABCD2345", kanji_family="山田", country="JP")
addr = A.assign_mail_address(c, m1)
A.deliver_mail(c, addr, b"Subject: hi\r\n\r\nbody", sender="bob@example.com",
               subject="hi")
A.open_session(c, m1, nick="Alice", peer_ip="127.0.0.1", iv=b"\x01" * 8)
A.set_login_alias(c, "alice-nick", m1)
A.record_login_failure(c, m2, peer_ip="127.0.0.1")
A.record_login_failure(c, m2, peer_ip="127.0.0.1")
A.list_stamp(c, m1, "friends", "fp")
A.set_client_token(c, m1, "sig-a", "tok")
A.prove_digest_client(c, "sig-b", m1)
c.commit()
c.close()

r = sqlite3.connect(p)
r.execute("PRAGMA foreign_keys = OFF")
# dave goes, so SQLite's AUTOINCREMENT counter is above the highest id
r.execute("DELETE FROM member WHERE id = ?", (m4,))
# handle_profile, including a value its INTEGER column cannot hold
r.execute("INSERT INTO handle_profile (handle_id, field_id, val_int, val_text,"
          " updated_at) VALUES (?, 1, 7, 'hello', '2026-01-02T03:04:05Z')", (h1,))
r.execute("INSERT INTO handle_profile (handle_id, field_id, val_int, val_text,"
          " updated_at) VALUES (?, 2, 'n/a', NULL, '2026-01-02T03:04:05Z')", (h1,))
r.execute("UPDATE friend SET client_ref = ?, wire_ref = ? WHERE id = ?",
          (b"\x00\x01\x02" * 4, b"\xa2\x1a\xf5\xeb", f12))
r.execute("INSERT INTO regcode (code, contents, created_at, redeemed_at,"
          " redeemed_by) VALUES ('AAAAA-BBBBB', '1,2', '2026-01-01T00:00:00Z',"
          " '2026-01-02T00:00:00Z', 'ABCD2345')")
r.execute("INSERT OR REPLACE INTO content_id_seq (id, next_id) VALUES (1, 30000050)")
r.execute("INSERT INTO regcode (code, contents, created_at) VALUES"
          " ('CCCCC-DDDDD', '1', '2026-01-01T00:00:00Z')")
# the orphans: rows whose parent is gone, which SQLite kept
r.execute("INSERT INTO member (id, polid, member_no, login_name, pw_hash, pw_salt,"
          " created_at) VALUES (900, 'NOPE2222', 0, 'nobody', 'x', 'y',"
          " '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO handle (id, member_id, handle_name, is_primary, created_at)"
          " VALUES (901, 900, 'Nobody', 1, '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO handle (id, member_id, handle_name, is_primary, created_at)"
          " VALUES (902, 999, 'Ghost', 1, '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO friend (id, handle_id, peer_name, kind, created_at)"
          " VALUES (903, 902, 'Alice', 2048, '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO friend (id, handle_id, peer_name, kind, created_at)"
          " VALUES (904, 902, 'GhostGuild', 1, '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO group_member (group_id, member_name, created_at)"
          " VALUES (904, 'Ghost', '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO group_member (group_id, member_name, created_at)"
          " VALUES (5555, 'Nowhere', '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO handle_content (handle_id, content_code, slot, content_id,"
          " status, linked_at) VALUES (902, 2, 0, '39999999', 'active',"
          " '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO content (member_id, content_code, status, registered_at)"
          " VALUES (999, 2, 'active', '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO mail (box, member_id, uidl, raw, received_at) VALUES"
          " ('ghost', 999, 'u1', X'00', '2026-01-01T00:00:00Z')")
r.execute("INSERT INTO session (token, member_id, created_at, expires_at) VALUES"
          " ('deadbeef', 999, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')")
r.execute("INSERT INTO login_fail (member_id, at) VALUES (999, 1.5)")
# references that are cleared instead (SET NULL / NO ACTION)
r.execute("INSERT INTO friend (id, handle_id, peer_handle, peer_name, kind,"
          " created_at) VALUES (905, ?, 888, 'Gone', 2048, '2026-01-01T00:00:00Z')",
          (h2,))
r.execute("INSERT INTO group_member (group_id, member_handle, member_name,"
          " created_at) VALUES (?, 777, 'Ghosty', '2026-01-01T00:00:00Z')", (g,))
r.execute("INSERT INTO regcode (code, contents, created_at, redeemed_at,"
          " redeemed_by) VALUES ('EEEEE-FFFFF', '1', '2026-01-01T00:00:00Z',"
          " '2026-01-03T00:00:00Z', 'ZZZZ9999')")
# tables the schema owns that older code created lazily
r.execute("CREATE TABLE IF NOT EXISTS handle_content_trimmed (handle_id INTEGER,"
          " content_code INTEGER, slot INTEGER, content_id TEXT, prev_status TEXT,"
          " trimmed_at TEXT)")
r.execute("INSERT INTO handle_content_trimmed VALUES (?, 1, 3, '31000003',"
          " 'active', '2026-02-02T00:00:00Z')", (h3,))
r.execute("INSERT INTO handle_content_trimmed VALUES (?, 1, 3, '31000003',"
          " 'active', '2026-02-02T00:00:00Z')", (h3,))
r.execute("CREATE TABLE IF NOT EXISTS content_character (content_id TEXT NOT NULL,"
          " content_code INTEGER NOT NULL DEFAULT 1, world_charid INTEGER,"
          " character_name TEXT NOT NULL, world_name TEXT, first_seen TEXT NOT NULL,"
          " last_seen TEXT NOT NULL, PRIMARY KEY (content_id, content_code))")
r.execute("INSERT INTO content_character VALUES ('31000001', 1, 4, 'Kharn',"
          " 'Bahamut', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')")
# the pre-rebuild copies, and a table nobody declared
r.execute("CREATE TABLE handle_profile_by_member (member_id INTEGER, field_id"
          " INTEGER, val_int INTEGER, val_text TEXT, updated_at TEXT)")
r.execute("INSERT INTO handle_profile_by_member VALUES (1, 1, 1, 'x', 'y')")
r.execute("CREATE TABLE handle_content_pre_slots (handle_id INTEGER, content_code"
          " INTEGER, content_id TEXT, status TEXT, linked_at TEXT)")
r.execute("INSERT INTO handle_content_pre_slots VALUES (1, 2, '1', 'active', 'x')")
r.execute("INSERT INTO handle_content_pre_slots VALUES (2, 2, '2', 'active', 'x')")
r.execute("CREATE TABLE scratch_notes (note TEXT)")
r.execute("INSERT INTO scratch_notes VALUES ('left by hand')")
r.commit()
seq = dict(r.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
r.close()

a = AU.connect(os.path.join(data, "admin.db"))
AU.create_mod(a, "Helper", "longpassword1", ["gm", "codes"], code_limit=5, by="owner")
AU.create_mod(a, "Second", "longpassword2", ["reports"])
AU.audit(a, "owner", "owner", "create-mod", target="Helper", detail={"x": 1})
AU.audit(a, "Helper", "mod", "make-code", target="AAAAA-BBBBB", ok=False,
         addr="127.0.0.1")
AU.code_origin_add(a, "AAAAA-BBBBB", "Helper", "mod", expires_at=1.9e9)
AU.set_setting(a, "gm_duty", "1")
AU.push_add(a, {"endpoint": "https://push.example.com/1",
                "keys": {"p256dh": "k", "auth": "a"}}, "Helper", "mod", 1,
            "https://admin.example.com", "ua")
AU.alerted_add(a, "ticket-1", "sent")
a.close()

d = DL.connect(os.path.join(data, "discord_links.db"))
code = DL.new_code(d, "1234567890", "someone")
DL.redeem(d, code, m1)
DL.new_code(d, "555", "other")
DL.mark_notified(d, ["m.tok.bin", "m.tok2.bin"])
DL.new_reply(d, m1, h1, str(2 ** 63 + 5), "Bob", "hi")
DL.note_sent(d, m1)
DL.note_sent(d, m1)
DL.set_meta(d, "last", "x")
d.close()

print(json.dumps({"m": [m1, m2, m3, m4], "h": [h1, h2, h3, h3b],
                  "g": g, "f": [f12, f21], "seq": seq}))
'''


def tree_state(root):
    """{relative path: (size, mtime_ns, sha256)} of every file under root."""
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns,
                                                 hashlib.sha256(fh.read()).hexdigest())
    return out


def run_import(src, *extra):
    js = os.path.join(BASE, "result-%d.json" % time.monotonic_ns())
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "db_import.py"),
                        src, "--database-url", URL, "--json", js] + list(extra),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    result = None
    if os.path.exists(js):
        with open(js, encoding="utf-8") as fh:
            result = json.load(fh)
    return p.returncode, p.stdout + p.stderr, result


def table_of(result, source, table):
    for s in result["sources"]:
        if s["name"] == source:
            for t in s["tables"]:
                if t["table"] == table:
                    return t
    return None


# --------------------------------------------------------------------------- #
print("fixture: a /data tree written by the pre-PostgreSQL code")
OLD = old_code()
SRC = os.path.join(BASE, "data")
LOGS = os.path.join(BASE, "logs")
os.makedirs(os.path.join(SRC, "resources", "tmrank"))
os.makedirs(LOGS)
env = dict(os.environ)
env.pop("POL_DATABASE_URL", None)
with open(os.path.join(BASE, "fixture.py"), "w", encoding="utf-8") as fh:
    fh.write(FIXTURE)
p = subprocess.run([sys.executable, os.path.join(BASE, "fixture.py"), OLD, SRC], env=env,
                   capture_output=True, text=True, encoding="utf-8")
if p.returncode != 0:
    print(p.stdout + p.stderr)
    print("FAIL: the old code could not build the fixture")
    sys.exit(1)
FX = json.loads(p.stdout.strip().splitlines()[-1])
m1, m2, m3, m4 = FX["m"]
h1, h2, h3, h3b = FX["h"]
G = FX["g"]
print("  members %s, handles %s, group %s, sqlite_sequence %s"
      % (FX["m"], FX["h"], G, FX["seq"]))

# importing responders reads no account data; it gives the resource names
import responders as R  # noqa: E402
from polcore import db  # noqa: E402

MAIL_PATH = ("O/m/O9cH2defHpt1Isrw9zswWI9rsfMe2Zkd2Z3TTTTTTTSL2b2jym1r"
             "TTTTTTTTTTTTTTTTTnleAci3TTTTT7TTAOA8TATTTTTT")
MAIL_BODY = bytes(range(1, 200)) + b"\xde\xad"
mail_name = R._resource_file(MAIL_PATH)
RES = {
    "%d.U_g_TM0DataFile.bin" % m1: (b"\x01\x02\x03save" * 10, 1_700_000_000),
    "shared.u_account.bin": (b"shared", 1_700_000_100),
    "s1f.b_g_PTL.bin": (b"lobby list", 1_700_000_200),
    mail_name: (MAIL_BODY, 1_700_000_300),
    mail_name + ".read": (b"", 1_700_000_400),
    "%d.jan_stats.json" % m3: (b'{"games": 3}', 1_700_000_500),
    "%d.U_g_x.bin" % m4: (b"a deleted member's save", 1_700_000_600),
    "auction-pending-%d.json" % m1: (b"[]", 1_700_000_700),
    "content-profiles.json": (b"{}", 1_700_000_800),
    "README": (b"not a resource", 1_700_000_900),
}
for name, (data, mtime) in RES.items():
    fn = os.path.join(SRC, "resources", name)
    with open(fn, "wb") as fh:
        fh.write(data)
    os.utime(fn, (mtime, mtime))
with open(os.path.join(SRC, "resources", "tmrank", "r1.bin"), "wb") as fh:
    fh.write(b"rank")
for live in ("auth-sessions.json", "rooms-live.json", "fmo-sessions-live.json",
             "fe.db", "fmowar.json", "ffxi_idmap.json"):
    with open(os.path.join(SRC, live), "w") as fh:
        fh.write("{}")
for live in ("push-spool.jsonl", "doc-stats.json"):
    with open(os.path.join(LOGS, live), "w") as fh:
        fh.write("{}")
# the old code ran in TRUNCATE mode: an empty -journal is what a stopped
# server leaves, and must not count as live
chk("the old code left an empty journal",
    os.path.getsize(os.path.join(SRC, "accounts.db-journal")) == 0
    if os.path.exists(os.path.join(SRC, "accounts.db-journal")) else True, True)

src_conn = sqlite3.connect(os.path.join(SRC, "accounts.db"))
SOURCE_COUNTS = {t: src_conn.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0]
                 for (t,) in src_conn.execute(
                     "SELECT name FROM sqlite_master WHERE type = 'table'"
                     " AND name NOT LIKE 'sqlite_%'").fetchall()}
src_conn.close()

# --------------------------------------------------------------------------- #
print("refused: a source written a moment ago looks live")
code, out, _res = run_import(SRC)
chk("exit 3", code, 3)
chk("says why", "written" in out and "REFUSED" in out, True)
chk("and that nothing was touched", "Nothing was read or written" in out, True)

# as if the stack had been stopped yesterday
old = time.time() - 86400
for d, _dirs, files in os.walk(SRC):
    for f in files:
        fp = os.path.join(d, f)
        if os.path.dirname(fp) != os.path.join(SRC, "resources"):
            os.utime(fp, (old, old))
BEFORE = tree_state(SRC)


def public_tables():
    return db.query("SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'public'")


print("dry run: plans against the schema, writes nothing")
code, out, res = run_import(SRC, "--dry-run", "--logs", LOGS)
chk("exit 0", code, 0)
chk("no table was created (not even the migrations)", public_tables(), [])
chk("no sequence either", db.query("SELECT sequencename FROM pg_sequences"), [])
chk("says it wrote nothing", "Dry run: nothing was written." in out, True)
chk("the plan counts the members to insert",
    table_of(res, "accounts", "member")["to_insert"], 3)
chk("the plan links the saves of the members it would add",
    [n for n in next(s for s in res["sources"] if s["name"] == "resources")
     ["extra"]["not_linked"]], ["%d.U_g_x.bin" % m4])

print("import")
code, out, res = run_import(SRC, "--logs", LOGS, "--report",
                            os.path.join(BASE, "report.txt"))
chk("exit 0", code, 0)
if code != 0:
    print(out)
chk("status", res["status"], "done")
chk("every source committed",
    [(s["name"], s["committed"]) for s in res["sources"]],
    [("accounts", True), ("admin", True), ("discord", True), ("resources", True)])
with open(os.path.join(BASE, "report.txt"), encoding="utf-8") as fh:
    report = fh.read()
chk("--report holds the printed report", report.strip() == out.strip()
    or report.strip() in out, True)

print("orphans: reported by table and skipped, references cleared")
want_orphans = {
    "member": {"polid -> polid.polid": 1},
    "handle": {"member_id -> member.id": 2},
    "friend": {"handle_id -> handle.id": 2},
    "group_member": {"group_id -> friend.id": 2},
    "handle_content": {"handle_id -> handle.id": 1},
    "content": {"member_id -> member.id": 1},
    "mail": {"member_id -> member.id": 1},
    "session": {"member_id -> member.id": 1},
}
got_orphans = {t["table"]: t["orphans"] for s in res["sources"] for t in s["tables"]
               if t["orphans"]}
chk("orphans by table", got_orphans, want_orphans)
got_cleared = {t["table"]: t["cleared"] for s in res["sources"] for t in s["tables"]
               if t["cleared"]}
chk("cleared references", got_cleared, {
    "friend": {"peer_handle -> handle.id": 1},
    "group_member": {"member_handle -> handle.id": 1},
    "regcode": {"redeemed_by -> polid.polid": 1}})
hp = table_of(res, "accounts", "handle_profile")
chk("a value the column cannot hold is skipped and reported",
    [u[1] for u in hp["unconvertible"]], ["'n/a' is not an integer"])
for name in ("Skipped, parent missing", "handle_profile_by_member",
             "handle_content_pre_slots", "scratch_notes", "Reference cleared",
             "value the column cannot hold"):
    chk("the report names %r" % name, name in out, True)
left = {x[0]: x[1] for s in res["sources"] if s["name"] == "accounts"
        for x in s["left_out"]}
chk("left out, with row counts", left, {"handle_profile_by_member": 1,
                                        "handle_content_pre_slots": 2,
                                        "scratch_notes": 1})

print("row counts")
for s in res["sources"]:
    for t in s["tables"]:
        if s["name"] == "resources":
            continue
        expect = (t["source_rows"] - sum(t["orphans"].values())
                  - len(t["unconvertible"]) - t["duplicates"])
        n = db.query_one('SELECT count(*) AS n FROM "%s"' % t["table"])["n"]
        chk("%s: %d in the source, %d in PostgreSQL" % (t["table"], t["source_rows"], n),
            n, expect)
        if s["name"] == "accounts":
            chk("%s: the source count is the table's" % t["table"],
                t["source_rows"], SOURCE_COUNTS[t["table"]])
chk("the keyless login_fail keeps its repeated rows",
    db.query_one("SELECT count(*) AS n FROM login_fail WHERE member_id = %s", (m2,))["n"], 2)
chk("so does handle_content_trimmed",
    db.query_one("SELECT count(*) AS n FROM handle_content_trimmed")["n"], 2)

print("ids and values kept")
chk("member ids", [r["id"] for r in db.query("SELECT id FROM member ORDER BY id")],
    [m1, m2, m3])
chk("handle ids", [r["id"] for r in db.query("SELECT id FROM handle ORDER BY id")],
    [h1, h2, h3, h3b])
fr = db.query_one("SELECT * FROM friend WHERE id = %s", (FX["f"][0],))
chk("a friend row by its old id", (fr["handle_id"], fr["peer_handle"], fr["peer_name"]),
    (h1, h2, "Bob"))
chk("bytes stay bytes", (bytes(fr["client_ref"]), bytes(fr["wire_ref"])),
    (b"\x00\x01\x02" * 4, b"\xa2\x1a\xf5\xeb"))
chk("the cleared peer_handle is NULL, the row kept",
    db.query_one("SELECT peer_handle, peer_name FROM friend WHERE id = 905"),
    {"peer_handle": None, "peer_name": "Gone"})
chk("timestamps stay the stored text",
    db.query_one("SELECT created_at FROM member WHERE id = %s", (m1,))["created_at"][-1:], "Z")
rc = db.query_one("SELECT redeemed_at, redeemed_by FROM regcode WHERE code = 'EEEEE-FFFFF'")
chk("a code whose redeemer is gone stays spent", rc,
    {"redeemed_at": "2026-01-03T00:00:00Z", "redeemed_by": None})
chk("Japanese text", db.query_one("SELECT kanji_family FROM profile")["kanji_family"],
    "山田")
chk("admin: the quoted \"by\" column",
    db.query_one('SELECT "by" FROM admin_code_origin')["by"], "Helper")
chk("discord: a u64 guid kept as text",
    db.query_one("SELECT peer_guid FROM discord_reply")["peer_guid"], str(2 ** 63 + 5))
chk("discord: the link", db.query_one("SELECT member_id FROM discord_link")["member_id"], m1)

print("group_member in rowid order")
order = [r["member_name"] for r in db.query(
    "SELECT member_name FROM group_member WHERE group_id = %s ORDER BY seq", (G,))]
chk("seq follows the order the members were added", order,
    ["Zed", "Alice", "Mo", "Bob", "Ghosty"])
import accounts  # noqa: E402
conn = accounts.connect()
chk("list_group_members serves that order",
    [name for _guid, name, _cls in accounts.list_group_members(conn, G)],
    ["Zed", "Alice", "Mo", "Bob", "Ghosty"])

print("sequences: a new row gets a fresh id")
floor = max(FX["seq"].get("member", 0), m3)
nm = int(accounts.add_member(conn, "EFGH6789", "erin", "password3"))
conn.commit()
chk("a new member is past SQLite's counter (not dave's old id %d)" % m4, nm, floor + 1)
nh = accounts.set_handle(conn, nm, "Erin")
chk("a new handle", nh, max(FX["seq"].get("handle", 0), h3b) + 1)
nf = accounts.add_friend(conn, nh, "Alice", peer_handle=h1)
chk("a new friend row", nf, max(FX["seq"].get("friend", 0), 905) + 1)
accounts.grant_content(conn, nm, 3)
cid = db.query_one("SELECT id FROM content WHERE member_id = %s", (nm,))["id"]
chk("a new content row (content_row_id_seq)", cid,
    max(FX["seq"].get("content", 0),
        db.query_one("SELECT max(id) AS m FROM content WHERE member_id <> %s", (nm,))["m"]) + 1)
accounts.add_group_member(conn, G, "Erin", member_handle=nh)
chk("a new group member goes last",
    [r["member_name"] for r in db.query(
        "SELECT member_name FROM group_member WHERE group_id = %s ORDER BY seq", (G,))][-1],
    "Erin")
chk("the Content ID counter came across",
    db.query_one("SELECT next_id FROM content_id_seq")["next_id"], 30000050)
new_mod = db.query_one("INSERT INTO admin_moderator (username, pw_hash, pw_salt,"
                       " created_at, pw_changed_at) VALUES ('Third', 'h', 's', 0, 0)"
                       " RETURNING id")["id"]
chk("a new moderator", new_mod, 3)
conn.close()

print("resources: blobs, readable through the resource store")
rs = next(s for s in res["sources"] if s["name"] == "resources")
chk("blob rows", db.query_one("SELECT count(*) AS n FROM blob")["n"], 8)
left = sorted(x[0] for x in rs["left_out"])
chk("left as files", left, ["README", "content-profiles.json", "tmrank/"])
chk("the mail object through the store",
    R._resource_blob(MAIL_PATH, len(MAIL_BODY) + 4)[:len(MAIL_BODY)] == MAIL_BODY, True)
store = sys.modules.get("core.resourcestore")
for name in ("%d.U_g_TM0DataFile.bin" % m1, "s1f.b_g_PTL.bin", mail_name + ".read",
             "%d.jan_stats.json" % m3):
    chk("%s: bytes" % name[:40], store._res_read(name) == RES[name][0], True)
    chk("%s: updated_at is the mtime" % name[:40], int(store._res_mtime(name)),
        RES[name][1])
chk("a member's save is linked to the member",
    db.query_one("SELECT member_id FROM blob WHERE scope = %s", (str(m1),))["member_id"], m1)
chk("a deleted member's save is kept, not linked",
    db.query_one("SELECT member_id FROM blob WHERE scope = %s", (str(m4),))["member_id"], None)
chk("and reported", rs["extra"]["not_linked"], ["%d.U_g_x.bin" % m4])
chk("title files reported by scope", rs["extra"]["other_scopes"],
    {"auction-pending-%d" % m1: 1})

print("title follow-ups")
for needle in ("docdb.py import stats /logs/doc-stats.json",
               "festore.py --import /data/fe_characters.json",
               "GAP: ffxi_idmap.json and ffxi_accounts.json",
               "BEFORE the new bridge starts", "GAP: fe.db", "GAP: fmo.db",
               "data/ffxi_idmap.json [found]", "logs/doc-stats.json [found]",
               "data/fmowar.json [found]", "data/fe_characters.json [not there]"):
    chk("printed: %s" % needle, needle in " ".join(out.split()), True)
chk("live files named as left behind",
    all(n in out for n in ("auth-sessions.json", "rooms-live.json",
                           "*-sessions-live.json", "push-spool.jsonl")), True)


def fingerprint():
    out = {}
    for (t,) in [(r["table_name"],) for r in public_tables()]:
        rows = db.query('SELECT * FROM "%s"' % t)
        out[t] = hashlib.sha256(repr(sorted(repr(sorted(r.items())) for r in rows))
                                .encode()).hexdigest()
    out["__seq"] = repr(sorted((r["sequencename"], r["last_value"]) for r in db.query(
        "SELECT sequencename, last_value FROM pg_sequences")))
    return out


print("a second run changes nothing")
FP = fingerprint()
code, out, res2 = run_import(SRC)
chk("exit 0", code, 0)
chk("status", res2["status"], "nothing")
chk("says so", "Nothing to import" in out, True)
chk("every table and sequence as it was", fingerprint() == FP, True)
code, out, res3 = run_import(SRC, "--merge")
chk("with --merge too", (code, res3["status"], fingerprint() == FP), (0, "nothing", True))

print("the source tree was not touched")
chk("every file, size, mtime and byte", tree_state(SRC) == BEFORE, True)
chk("no journal or -wal was made", sorted(n for n in os.listdir(SRC)
                                          if n.endswith(("-wal", "-shm"))), [])

print("a non-empty target: refused without --merge, merged with it")
SRC2 = os.path.join(BASE, "data2")
shutil.copytree(SRC, SRC2)
c2 = sqlite3.connect(os.path.join(SRC2, "accounts.db"))
c2.execute("INSERT INTO member (id, polid, member_no, login_name, pw_hash, pw_salt,"
           " created_at) VALUES (950, 'ABCD2345', 5, 'frank', 'h', 's',"
           " '2026-03-01T00:00:00Z')")
c2.execute("UPDATE handle SET handle_name = 'AliceRenamed' WHERE id = ?", (h1,))
c2.execute("UPDATE content_id_seq SET next_id = 30000080")
c2.commit()
c2.close()
for d, _dirs, files in os.walk(SRC2):
    for f in files:
        fp = os.path.join(d, f)
        if os.path.dirname(fp) != os.path.join(SRC2, "resources"):
            os.utime(fp, (old, old))
FP = fingerprint()
code, out, res4 = run_import(SRC2)
chk("refused, exit 2", code, 2)
chk("names the table", "member" in out and "REFUSED" in out, True)
chk("nothing written", fingerprint() == FP, True)
code, out, res5 = run_import(SRC2, "--merge")
chk("--merge: exit 0", code, 0)
chk("--merge: only the new member", table_of(res5, "accounts", "member")["inserted"], 1)
chk("--merge: frank is in", db.query_one(
    "SELECT login_name FROM member WHERE id = 950")["login_name"], "frank")
chk("--merge: a key in both with other contents keeps the target's row",
    table_of(res5, "accounts", "handle")["existing_differs"], [[h1]])
chk("--merge: and says so", "Kept the target's row" in out, True)
chk("--merge: the Content ID counter takes the higher value",
    (table_of(res5, "accounts", "content_id_seq")["raised"],
     db.query_one("SELECT next_id FROM content_id_seq")["next_id"]),
    ([30000050, 30000080], 30000080))
chk("--merge: the handle is not renamed", db.query_one(
    "SELECT handle_name FROM handle WHERE id = %s", (h1,))["handle_name"], "Alice")

print("refused: a SQLite file with a hot journal")
with open(os.path.join(SRC2, "accounts.db-journal"), "wb") as fh:
    fh.write(b"\x00" * 512)
code, out, _r = run_import(SRC2, "--merge")
chk("exit 3", code, 3)
chk("names the journal", "accounts.db-journal is not empty" in out, True)
code, out, _r = run_import(os.path.join(BASE, "run-logs"))
chk("a directory that is not a data volume: exit 4", code, 4)

print()
print("PASS" if not bad else "FAIL: %d check(s)" % bad)
sys.exit(1 if bad else 0)
