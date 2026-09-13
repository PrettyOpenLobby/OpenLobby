"""Does the Content ID mint still produce SE-SHAPED, unique, never-reused ids?

Offline: an in-memory `accounts.db`, no client, no server, no network.

WHY THIS EXISTS. Until 2026-08-23 a Content ID was COMPUTED --
`1000000000 + member.id * 100 + content_code`, ten digits with the game in the
last two. Then a real SE Content ID was read off the live SE-connected Viewer
two independent ways (`work/pc/polcontent.py` off the 64-slot content table at
`polcore+0x403080`, and the pol-shim `[pay]` capture of the real `1:3` reply at
record offset 0x10 LE; value and provenance in the GITIGNORED
`Ignored Files/content-ids.md`, which is why nothing here quotes it). It is a
plain ~8-digit integer in the tens of millions, with the game carried in a
SEPARATE u16 content-code field. Every premise of the old mint was wrong at once,
so the mint became an ALLOCATOR: `accounts.allocate_content_id`, a persistent
server-wide serial.

THE FOUR THINGS THAT CANNOT BE ALLOWED TO SILENTLY REGRESS, and why each one is
a live-visible failure rather than a style point:

  1. **SHAPE.** A 10-digit id is not what SE issues. Nothing we hold VALIDATES
     the format, so a wrong shape is invisible on our own server and shows up
     only against something that cares -- which is exactly how the old mint
     survived for months.
  2. **UNIQUENESS, server-wide.** A Content ID belongs to exactly ONE handle
     (POL-7169/7187/5326). The 2026-08-15 bug keyed the mint on `member_no` --
     the member's SLOT within a POL ID, `0` for the first member of every
     account -- so eight separate accounts minted `1000000001` and all eight
     resolved to the same FFXI character.
  3. **NO RE-MINTING.** The FFXI client names a character's local files by the
     Content ID in hex (`FINAL FANTASY XI/USER/<hexid>/`). Re-issuing an id that
     has already been served orphans that user's data. Legacy 10-digit rows
     therefore keep their values forever; migrating them is an admin task.
  4. **THE GAME IS NOT IN THE ID.** `content_code` is the u16 field the launch
     gate (`app.dll+0x199093`) actually compares. Putting the game in the digits
     was an artifact of our own invented numbers and is retracted for SE.

Run after any change to `allocate_content_id`, to the `handle_content` schema,
or to a provisioning path (`register_account`, `ensure_member`,
`link_member_content_to_primary`, `grant_content`). Exits non-zero on a failure.

IT CARRIES NEGATIVE CONTROLS, because a green suite that cannot go red is worth
nothing (this tree has been burned by exactly that -- see the run_all docstring:
a past verifier reported "0 Japanese left" on a file that was full of
it). Re-checked 2026-08-23 after the migration landed, by monkeypatching a
throwaway copy of this file (`HERE` repointed at the real tools directory so only
the patched behaviour differs):

  * the RETIRED computed mint (`1000000000 + n * 100 + code`) -> **10 failures**
    across sections 1, 2, 3 and 6 -- shape, digit count, the 1e9 base, all three
    game-in-the-digits tests, the re-mint, and the retirement checks;
  * a CONSTANT id (the 2026-08-15 collision, every account the same value) ->
    **13 failures** across sections 1, 2, 3, 4, 5 and 7, including the
    server-wide duplicate scan;
  * a migration that moves the DB row and **forgets the JSON stores**
    (`rewrite_json` stubbed to 0) -> **3 failures**, all in section 8 -- which is
    the precise failure `content_id_migrate.py` exists to prevent, so section 8
    is doing its job rather than restating section 1.

If you change what the mint or the migration does, re-run that trio. A section
that stays green under all three is testing nothing.
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)

import accounts as A

FAILED = []


def check(cond, what):
    print(f"  {'OK  ' if cond else 'FAIL'} {what}")
    if not cond:
        FAILED.append(what)
    return cond


def fresh_db():
    """An empty accounts DB, schema applied, exactly as `connect()` leaves one."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(A.SCHEMA)
    A._migrate(c)
    return c


def digits(cid):
    return len(str(cid).strip())


# --------------------------------------------------------------------------- #
print(__doc__.splitlines()[0])
print()
print("1. THE SHAPE, AND THE SERIAL -- allocate a run of them from an empty DB")
# --------------------------------------------------------------------------- #
db = fresh_db()
N = 500
run = [A.allocate_content_id(db) for _ in range(N)]
nums = [int(x) for x in run]

check(len(set(run)) == N, f"{N} allocations produced {len(set(run))} distinct ids")
check(all(nums[i] < nums[i + 1] for i in range(N - 1)),
      "strictly increasing (a serial, not a hash)")
check(all(A.CONTENT_ID_FLOOR <= n <= A.CONTENT_ID_CEILING for n in nums),
      f"every id inside [{A.CONTENT_ID_FLOOR}, {A.CONTENT_ID_CEILING}]")
check(all(digits(x) == 8 for x in run),
      "every id is 8 digits, the shape the real SE value has")
# The old mint's fingerprint, named rather than described: if any of these ever
# reappears the shape has quietly reverted.
check(not any(1_000_000_000 <= n <= 1_999_999_999 for n in nums),
      "no id carries the retracted 1000000000 base")
check(all(str(x) == str(x).lstrip("0") for x in run),
      "no zero padding -- the value is a number, not a fixed-width field")
print(f"     (first {run[0]}, last {run[-1]})")

# --------------------------------------------------------------------------- #
print()
print("2. THE GAME IS A SEPARATE FIELD -- it must NOT be recoverable from the id")
# --------------------------------------------------------------------------- #
db = fresh_db()
A.create_polid(db, "GAMETEST", "pw-account")
mid = A.add_member(db, "GAMETEST", "gametest", "pw-member")
A.set_handle(db, mid, "OneHandle")
# One account, five different titles: FFXI(1), Tetra Master(2), Janhourou(3),
# FMO(4), Fantasy Earth(11).
CODES = [1, 2, 3, 4, 11]
for code in CODES:
    A.grant_content(db, mid, code)
A.link_member_content_to_primary(db, mid)
def primary_links(rows):
    """Slot 0 of each game -- "the" Content ID for that title.

    `handle_content` holds SEVERAL rows per (handle, game) since 2026-09-03, so
    that FFXI can issue one Content ID per CHARACTER. Slot 0 is the title's
    identity and is what every assertion below means; without this filter a
    `{content_code: content_id}` comprehension silently keeps whichever extra
    FFXI slot happened to sort last, and the checks would pass or fail on the
    wrong row.
    """
    return [r for r in rows if int(r.get("slot", 0)) == 0]


links = A.handle_content_list(db, A.primary_handle_row(db, mid)["id"])
got = {int(r["content_code"]): r["content_id"] for r in primary_links(links)}

check(sorted(got) == CODES, f"all five titles linked ({sorted(got)})")
check(len(set(got.values())) == len(CODES),
      "five titles, five distinct Content IDs")
# THE ACTUAL ASSERTION: the code lives in `content_code`, and the id says
# nothing about it. Both the old mint's encodings are checked -- the last two
# digits WERE the game, and that is precisely what must no longer hold.
check(all(int(str(cid)[-2:]) != code for code, cid in got.items()),
      "the game code is NOT the last two digits of the id")
check(all(int(str(cid)[-1:]) != code for code, cid in got.items() if code < 10),
      "...nor the last one")
check(all(A.content_id_int(cid) % 100 != code for code, cid in got.items()),
      "...nor the id modulo 100")
for code, cid in sorted(got.items()):
    print(f"     content_code={code:<3} content_id={cid}")

# --------------------------------------------------------------------------- #
print()
print("3. NEVER RE-MINT -- an id already served keeps its value forever")
# --------------------------------------------------------------------------- #
# `FINAL FANTASY XI/USER/<hexid>/` is named after the id, so re-minting one that
# has been served orphans a real user's macros and config. The ONE sanctioned
# exception is the admin-run migration (section 8), which moves the directory
# too; nothing on a normal code path may do this.
db = fresh_db()
A.create_polid(db, "OLDPOLID", "pw-account")
old_mid = A.add_member(db, "OLDPOLID", "oldmember", "pw-member")
A.set_handle(db, old_mid, "Veteran")
old_hid = A.primary_handle_row(db, old_mid)["id"]
A.grant_content(db, old_mid, 1)
A.grant_content(db, old_mid, 2)
A.link_member_content_to_primary(db, old_mid)
SERVED = {int(r["content_code"]): r["content_id"]
          for r in primary_links(A.handle_content_list(db, old_hid))}
check(len(SERVED) == 2, f"two titles linked to start with ({SERVED})")

# Everything a running server does to that account afterwards.
A.link_member_content_to_primary(db, old_mid)          # a second code redeemed
A.grant_content(db, old_mid, 1)                        # re-register FFXI
A.revoke_content(db, old_mid, 2)                       # cancel Tetra Master
A.grant_content(db, old_mid, 2)                        # ...and reactivate it
still = {int(r["content_code"]): r["content_id"]
         for r in primary_links(A.handle_content_list(db, old_hid, active_only=False))}
check(all(still.get(code) == cid for code, cid in SERVED.items()),
      f"served ids survived a redeem/revoke/regrant cycle ({still})")

# ...and a NEW title on that account gets a NEW id, not one of the existing ones.
A.grant_content(db, old_mid, 4)
A.link_member_content_to_primary(db, old_mid)
fresh = {int(r["content_code"]): r["content_id"]
         for r in primary_links(A.handle_content_list(db, old_hid))}.get(4)
check(fresh is not None and digits(fresh) == 8,
      f"a new title mints an 8-digit id (got {fresh!r})")
check(fresh not in SERVED.values(),
      "a new title did not recycle an id already on this account")

# --------------------------------------------------------------------------- #
print()
print("4. THE COUNTER IS PERSISTENT -- it is a row, not a process variable")
# --------------------------------------------------------------------------- #
import tempfile

tmpdir = tempfile.mkdtemp(prefix="polcid")
path = os.path.join(tmpdir, "accounts.db")
try:
    c1 = sqlite3.connect(path)
    c1.row_factory = sqlite3.Row
    c1.executescript(A.SCHEMA)
    A._migrate(c1)
    first = [A.allocate_content_id(c1) for _ in range(3)]
    c1.commit()
    c1.close()

    c2 = sqlite3.connect(path)                     # a RESTARTED server
    c2.row_factory = sqlite3.Row
    c2.executescript(A.SCHEMA)
    A._migrate(c2)
    second = [A.allocate_content_id(c2) for _ in range(3)]
    c2.commit()
    c2.close()
    check(not set(first) & set(second),
          f"a restart did not re-issue {first} -> {second}")
    check(int(second[0]) > int(first[-1]),
          "the serial carried on from where it stopped")
finally:
    for name in os.listdir(tmpdir):
        try:
            os.unlink(os.path.join(tmpdir, name))
        except OSError:
            pass
    os.rmdir(tmpdir)

# --------------------------------------------------------------------------- #
print()
print("5. AN ALREADY-ISSUED NUMBER IS NEVER HANDED OUT AGAIN")
# --------------------------------------------------------------------------- #
# The one case where the counter and the rows can disagree: an admin hand-
# links a real SE id (or restores a backup taken after the counter moved). The
# allocator must step over it rather than duplicate it.
db = fresh_db()
A.create_polid(db, "CLASHPID", "pw-account")
cm = A.add_member(db, "CLASHPID", "clash", "pw-member")
A.set_handle(db, cm, "Squatter")
chid = A.primary_handle_row(db, cm)["id"]
squat = str(A.CONTENT_ID_FLOOR + 1)                # the counter's SECOND value
A.grant_content(db, cm, 1)
A.link_content_to_handle(db, chid, 1, squat)
minted = [A.allocate_content_id(db) for _ in range(3)]
check(squat not in minted,
      f"the hand-linked {squat} was skipped, not re-issued ({minted})")
check(len(set(minted)) == 3 and all(int(a) < int(b) for a, b
                                    in zip(minted, minted[1:])),
      "and the run stayed unique and increasing across the skip")

# The other store the same number can hide in: `content.content_no`, the
# admin's "this member's real SE id" override, which never went through the
# counter at all.
db = fresh_db()
A.create_polid(db, "OVERRIDE", "pw-account")
om = A.add_member(db, "OVERRIDE", "override", "pw-member")
A.set_handle(db, om, "Operator")
reserved = str(A.CONTENT_ID_FLOOR + 2)
A.grant_content(db, om, 1, reserved)
run2 = [A.allocate_content_id(db) for _ in range(4)]
check(reserved not in run2,
      f"a content_no override ({reserved}) is not re-issued either ({run2})")
A.link_member_content_to_primary(db, om)
kept = primary_links(A.handle_content_list(db, A.primary_handle_row(db, om)["id"]))
check([r["content_id"] for r in kept] == [reserved],
      f"...and the override is what gets linked, not a serial ({kept and dict(kept[0])})")

# --------------------------------------------------------------------------- #
print()
print("6. THE ID RESOLVES BACK TO ITS HANDLE -- and the RETIRED shape does not")
# --------------------------------------------------------------------------- #
# The lobby's chat-room "View profile" names a member by the Content ID they are
# logged in under and sends it as the z_hid. Measured live on prod
# 2026-08-19T00:15: before it resolved, the reply was built for the wrong
# subject and the client CRASHED. The lookup compares numerically rather than
# formatting a fixed width, which is what let the shape change at all.
db = fresh_db()
A.create_polid(db, "RESOLVER", "pw-account")
rm = A.add_member(db, "RESOLVER", "resolver", "pw-member")
A.set_handle(db, rm, "Subject")
rhid = A.primary_handle_row(db, rm)["id"]
A.grant_content(db, rm, 1)
A.grant_content(db, rm, 2)
A.link_member_content_to_primary(db, rm)
cids = [r["content_id"] for r in A.handle_content_list(db, rhid)]
for cid in cids:
    row = A.handle_by_content_id(db, cid)
    check(row is not None and row["id"] == rhid, f"{cid} resolves to its handle")
    check(A.looks_like_content_id(cid), f"{cid} is recognised as a Content ID")
check(A.handle_by_content_id(db, "99999999") is None,
      "an id nobody holds resolves to nothing (not to the viewer's own handle)")
check(not A.looks_like_content_id(0) and not A.looks_like_content_id("")
      and not A.looks_like_content_id(None) and not A.looks_like_content_id("abc"),
      "0 / empty / None / non-numeric are not Content IDs")

# THE RETIREMENT ITSELF (2026-08-23). A 10-digit computed id must no longer be
# accepted as a Content ID -- but it must still be RECOGNISABLE as a retired
# one, because a client caches the content table from 1:3 at LOGIN and a player
# who was connected across the migration keeps sending the old value until they
# relog. "Not a Content ID" and "a Content ID we stopped issuing" are different
# answers and the lobby log needs the second one.
for retired in ("1000000101", 1000000102, 1999999999, "1000000000"):
    check(not A.looks_like_content_id(retired),
          f"the retired 10-digit {retired} is NOT accepted as a Content ID")
    check(A.looks_like_retired_content_id(retired),
          f"...but {retired} IS recognised as a retired one")
check(not any(A.looks_like_retired_content_id(c) for c in cids),
      "a live allocated id must never read as retired")
check(not A.looks_like_retired_content_id("99999999"),
      "an 8-digit id at the ceiling is not retired either")
# The friend-row subject space, which the lobby tests BEFORE this one: those
# z_hids are `(state << 19) | handle_id` for states 4 and 9, i.e. at most
# ~5.2 million. No Content ID may fall in there, or the two resolvers fight.
check(A.CONTENT_ID_MIN > (10 << 19),
      "the Content ID window starts above the friend-row subject space")

# --------------------------------------------------------------------------- #
print()
print("7. TWO ACCOUNTS CANNOT COLLIDE -- the 2026-08-15 bug, from the other side")
# --------------------------------------------------------------------------- #
# Every account's first member has `member_no = 0`. The old mint keyed on that,
# so eight accounts minted the same id. The allocator has no per-account input
# at all, which is the structural reason this cannot come back -- so the test
# builds the exact situation and asserts the ids differ.
db = fresh_db()
seen = {}
for i in range(8):
    pid = f"ACCT{i:04d}"
    A.create_polid(db, pid, "pw-account")
    m = A.add_member(db, pid, f"member{i}", "pw-member")
    A.set_handle(db, m, f"Handle{i}")
    A.grant_content(db, m, 1)
    A.link_member_content_to_primary(db, m)
    no = db.execute("SELECT member_no FROM member WHERE id = ?", (m,)).fetchone()[0]
    cid = primary_links(A.handle_content_list(db, A.primary_handle_row(db, m)["id"]))[0]["content_id"]
    seen[pid] = (no, cid)
check(all(v[0] == 0 for v in seen.values()),
      "all eight are member_no 0 -- the input the old mint got wrong")
check(len({v[1] for v in seen.values()}) == 8,
      f"...and all eight Content IDs differ ({[v[1] for v in seen.values()]})")
dupes = [(r["content_id"], r["n"]) for r in db.execute(
    "SELECT content_id, COUNT(*) n FROM handle_content WHERE content_id IS NOT NULL"
    " GROUP BY CAST(content_id AS INTEGER) HAVING n > 1")]
check(not dupes, f"no Content ID appears on two handles anywhere in the DB ({dupes})")

# --------------------------------------------------------------------------- #
print()
print("8. THE MIGRATION -- it must move the id AND everything that names one")
# --------------------------------------------------------------------------- #
# `tools/content_id_migrate.py` is the one sanctioned re-mint, and the thing it
# has to get right is not the UPDATE -- it is the set of OTHER places a Content
# ID lives. A migration that moves the DB row and forgets `ffxi_idmap.json` or
# the TM pool leaves a player linked to nothing, silently. Run here end to end
# against a throwaway tree so that set is pinned by a test rather than by
# somebody having remembered.
import json
import subprocess
import tempfile

import content_id_migrate as M

mig = tempfile.mkdtemp(prefix="polcidmig")
try:
    os.makedirs(os.path.join(mig, "resources", "tmrank"))
    mig_db = os.path.join(mig, "accounts.db")
    c = sqlite3.connect(mig_db)
    c.row_factory = sqlite3.Row
    c.executescript(A.SCHEMA)
    A._migrate(c)
    A.create_polid(c, "MIGRATE1", "pw-account")
    mm = A.add_member(c, "MIGRATE1", "migrant", "pw-member")
    A.set_handle(c, mm, "Migrant")
    mhid = A.primary_handle_row(c, mm)["id"]
    # A pre-migration database: the retired computed shape, exactly as it was
    # written by `1000000000 + member.id * 100 + content_code`.
    OLD = {1: "1000000101", 2: "1000000102", 4: "1000000104"}
    for code, cid in OLD.items():
        A.grant_content(c, mm, code)
        A.link_content_to_handle(c, mhid, code, cid)
    c.commit()
    c.close()
    with open(os.path.join(mig, "ffxi_idmap.json"), "w") as fh:
        json.dump({"7": {"content_id": 1000000101, "name": "Migrant",
                         "world_field": 2097159}}, fh)
    with open(os.path.join(mig, "resources", "tmrank", "pool.json"), "w") as fh:
        json.dump({str(mm): {"cid": 1000000102, "cname": "1000000102",
                             "cinfo": "Card Level 7"}}, fh)

    quiet = []
    rc = subprocess.call([sys.executable, os.path.join(HERE, "content_id_migrate.py"),
                          "--db", mig_db, "--apply"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    check(rc == 0, f"the migration exited 0 (got {rc})")

    c = sqlite3.connect(mig_db)
    c.row_factory = sqlite3.Row
    after = {int(r["content_code"]): r["content_id"]
             for r in primary_links(A.handle_content_list(c, mhid))}
    check(len(after) == 3 and not any(A.looks_like_retired_content_id(v)
                                      for v in after.values()),
          f"every row left the retired shape ({after})")
    check(all(A.looks_like_content_id(v) for v in after.values()),
          f"...and landed in the allocated window ({after})")
    check(len(set(after.values())) == 3, "the three ids are still distinct")
    check(set(after) == set(OLD), "no row was lost or invented")
    # The identity has to still RESOLVE -- this is the crash path from section 6.
    for v in after.values():
        check(A.handle_by_content_id(c, v) is not None,
              f"the migrated {v} resolves to its handle")
    c.close()

    with open(os.path.join(mig, "ffxi_idmap.json")) as fh:
        idmap = json.load(fh)
    check(idmap["7"]["content_id"] == A.content_id_int(after[1]),
          f"ffxi_idmap followed the FFXI id ({idmap['7']['content_id']} vs {after[1]})")
    check(idmap["7"]["world_field"] == 2097159,
          "...and world_field, which is NOT a Content ID, was left alone")

    with open(os.path.join(mig, "resources", "tmrank", "pool.json")) as fh:
        pool = json.load(fh)
    check(pool[str(mm)]["cid"] == A.content_id_int(after[2]),
          f"the TM pool followed the Tetra Master id ({pool[str(mm)]['cid']})")
    check(pool[str(mm)]["cname"] == str(after[2]),
          "...and cname too, since it WAS the digits")
    check(pool[str(mm)]["cinfo"] == "Card Level 7",
          "...while everything else in the record was left untouched")

    # THE MAPPING FILE is the only route the FFXI USER/<hexid> directories have.
    maps = [f for f in os.listdir(mig) if f.startswith("content-id-migration-")]
    check(len(maps) == 1, f"exactly one mapping file was written ({maps})")
    with open(os.path.join(mig, maps[0])) as fh:
        doc = json.load(fh)
    check(len(doc["map"]) == 3, "the mapping covers all three ids")
    check(all(int(e["old_hex"], 16) == e["old"] and int(e["new_hex"], 16) == e["new"]
              for e in doc["map"]),
          "every hex form matches its decimal -- the USER/ directory name")
    check({e["old"] for e in doc["map"]} == {int(v) for v in OLD.values()},
          "the mapping's old ids are exactly the ones that were in the DB")

    # And it must be idempotent: a second run is a no-op, not a second re-mint.
    before = dict(after)
    rc2 = subprocess.call([sys.executable, os.path.join(HERE, "content_id_migrate.py"),
                           "--db", mig_db, "--apply"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    c = sqlite3.connect(mig_db)
    c.row_factory = sqlite3.Row
    again = {int(r["content_code"]): r["content_id"]
             for r in primary_links(A.handle_content_list(c, mhid))}
    c.close()
    check(rc2 == 0 and again == before,
          f"a second run changed nothing ({before} -> {again})")

    # A row it does not understand must ABORT the run, before anything is written.
    c = sqlite3.connect(mig_db)
    # slot 0 only: `content_id` is UNIQUE now, so setting every FFXI row to the
    # same sentinel is a constraint violation rather than a test. One
    # unrecognised row is all this check ever needed.
    c.execute("UPDATE handle_content SET content_id = 'HAND-ENTERED'"
              " WHERE handle_id = ? AND content_code = 1 AND slot = 0", (mhid,))
    c.commit()
    c.close()
    rc3 = subprocess.call([sys.executable, os.path.join(HERE, "content_id_migrate.py"),
                           "--db", mig_db, "--apply"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    check(rc3 == 2, f"an unrecognised row refuses the run (exit {rc3}, wanted 2)")
finally:
    shutil.rmtree(mig, ignore_errors=True)

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print(f"RESULT: {len(FAILED)} FAILURE(S)")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: the Content ID mint is SE-shaped, unique, serial and non-reusing")
sys.exit(0)
