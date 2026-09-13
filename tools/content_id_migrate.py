"""Re-mint every legacy 10-digit Content ID as an allocated serial, and move
everything that refers to one along with it.

    python tools/content_id_migrate.py --db /data/accounts.db            # DRY RUN
    python tools/content_id_migrate.py --db /data/accounts.db --apply

DRY RUN IS THE DEFAULT. `--apply` is the only thing that writes.

WHAT THIS IS FOR. Until 2026-08-23 a Content ID was COMPUTED -- `1000000000 +
member.id * 100 + content_code`, ten digits with the game in the last two -- and
a real SE value then showed all three premises were wrong (see
`accounts.allocate_content_id`, and the GITIGNORED `Ignored Files/content-ids.md`
for the measurement). The mint became an allocator the same day, but only for NEW
rows: existing ones were deliberately left alone because re-minting them is a
DATA migration with a filesystem side, not a code change. This is that migration,
run deliberately, by an admin, once.

WARNING: **A CONTENT ID IS NOT ONLY A DATABASE VALUE.** That is the whole reason this
tool exists instead of one UPDATE statement. Four things name one, and a
migration that moves the first and forgets the rest is worse than no migration --
it silently unlinks a player from their own data:

  1. `handle_content.content_id` -- the store, and the only authoritative copy.
  2. **The FFXI client's local per-character directory**, `FINAL FANTASY XI/
     USER/<content-id-in-hex>/`, which holds macros, config, chat filters and
     the equipment set files. It is on the PLAYER'S MACHINE, not here, so this
     tool cannot touch it -- it writes a MAPPING FILE and `work/pc/
     ffxi_userdir_rename.py` applies it there. Skip that step and the player
     keeps their character and loses every macro they ever wrote.
  3. `ffxi_idmap.json` -- the LSB bridge's charid <-> Content ID pairing. Stale
     here means `charid_for()` misses and the world lookup that gates the FFXI
     world socket (POL-0001) has nothing to match.
  4. `resources/tmrank/pool.json` -- the character pool each Tetra Master client
     told us about. The ranking row's identity IS the TM Content ID, so a stale
     one tells the player "Did not rank" with their own name on the screen. Its
     `cname` is rewritten too, but ONLY where it is the id's own digits (which
     is what the client sent before `POL_CHAR_NAME_HANDLE`); a real character
     name is left alone.

The generated ranking blobs (`resources/tmrank/U_g_TM0_RANKLIST*.bin`) embed the
id in each row. They are NOT patched -- they are regenerated, which is a job that
already exists: `python tools/tmrank.py --publish`. This tool reminds you.

WHAT IT WILL NOT DO
  * It will not touch `content.content_no`. That is a hand-entered real SE
    Content ID; migrating it would destroy the one true value we hold.
  * It will not run against a database it does not fully understand: a row whose
    `content_id` is neither legacy-shaped nor already-allocated aborts the run
    before anything is written.
  * It will not renumber a row twice. Re-running after a completed migration is
    a no-op that says so.

SESSIONS. The client caches the 64-slot content table from the lobby's `1:3`
reply at LOGIN. A player connected while this runs keeps the old ids in memory
until they relog, and their Run button will refuse ("You have no Content ID for
<game>") in the meantime. Run it with nobody connected, or tell them to relog.
"""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

import accounts as A

LEGACY_LO, LEGACY_HI = 1_000_000_000, 1_999_999_999


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def classify(conn):
    """Every `handle_content` row, split into legacy / already-done / unknown.

    "Unknown" is the important bucket and the reason this is a function rather
    than a WHERE clause: a value that is neither shape is something nobody
    anticipated -- a hand-entered real SE id, a blank, a string -- and guessing
    what to do with it is exactly how a migration eats data.
    """
    legacy, done, unknown = [], [], []
    for r in conn.execute(
            "SELECT hc.rowid AS rid, hc.handle_id, hc.content_code, hc.content_id,"
            "       hc.status, h.member_id, h.handle_name"
            "  FROM handle_content hc JOIN handle h ON h.id = hc.handle_id"
            " ORDER BY h.member_id, hc.handle_id, hc.content_code"):
        row = dict(r)
        n = A.content_id_int(row["content_id"])
        if n is None:
            unknown.append(row)
        elif LEGACY_LO <= n <= LEGACY_HI:
            legacy.append(row)
        elif A.CONTENT_ID_FLOOR <= n <= A.CONTENT_ID_CEILING:
            done.append(row)
        else:
            unknown.append(row)
    return legacy, done, unknown


def rewrite_json(path, mapping, apply_, log):
    """Apply the mapping to one JSON store. Returns the number of values moved.

    Deliberately SHAPE-AWARE rather than a blind text substitution: a Content ID
    is a 10-digit number and a text pass would happily rewrite a timestamp, a
    guid or a card count that happened to share those digits. Each store's known
    fields are named here, and an unrecognised store is reported, not guessed at.
    """
    if not os.path.exists(path):
        log(f"    {path}: absent, nothing to do")
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"    {path}: UNREADABLE ({exc!r}) -- left alone")
        return 0
    moved = 0
    orphans = []            # legacy-shaped values in the store that the DB has no row for
    base = os.path.basename(path)

    if base == "ffxi_idmap.json":
        # {"<charid>": {"content_id": N, "name": ..., "world_field": N}}
        # `world_field` is packed from (charid, worldid, table) and carries no
        # Content ID, so it is NOT touched -- see chargate_check.
        for charid, ent in (data or {}).items():
            if not isinstance(ent, dict):
                continue
            old = A.content_id_int(ent.get("content_id"))
            if old in mapping:
                ent["content_id"] = int(mapping[old])
                moved += 1
                log(f"    ffxi_idmap charid {charid}: {old} -> {mapping[old]}")
            elif old is not None and LEGACY_LO <= old <= LEGACY_HI:
                orphans.append(f"charid {charid} -> {old}")
    elif base == "pool.json":
        # {"<member id>": {"cid": N, "cname": "...", ...}}
        for mid, ent in (data or {}).items():
            if not isinstance(ent, dict):
                continue
            old = A.content_id_int(ent.get("cid"))
            if old not in mapping:
                if old is not None and LEGACY_LO <= old <= LEGACY_HI:
                    orphans.append(f"member {mid} -> {old}")
                continue
            new = int(mapping[old])
            ent["cid"] = new
            moved += 1
            # ONLY when the name IS the digits. Before POL_CHAR_NAME_HANDLE the
            # client's own banner was the Content ID as a string, and leaving it
            # stale would put a retired number on a live screen. A real
            # character name is not ours to rewrite.
            if str(ent.get("cname", "")).strip() == str(old):
                ent["cname"] = str(new)
                log(f"    pool member {mid}: cid AND cname {old} -> {new}")
            else:
                log(f"    pool member {mid}: cid {old} -> {new} "
                    f"(cname {ent.get('cname')!r} left as it is)")
    else:
        log(f"    {path}: NOT A KNOWN STORE -- skipped, check it by hand")
        return 0

    # AN ORPHAN IS NOT A NO-OP, IT IS A FINDING. A legacy-shaped id sitting in a
    # store with no `handle_content` row behind it is stale data that this
    # migration is about to make PERMANENTLY unresolvable -- the row it used to
    # name is gone, and after this run nothing in the DB has that shape again.
    # Reported loudly rather than skipped quietly, because "the tool said
    # nothing" is how it would be mistaken for "there was nothing there".
    if orphans:
        log(f"    !!  {path}: {len(orphans)} legacy id(s) with NO handle_content row")
        for o in orphans:
            log(f"    !!    {o}  -- left as it is; it names nothing already")

    if moved and apply_:
        shutil.copy2(path, f"{path}.bak-cidmigrate-{stamp()}")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    return moved


def verify(conn, mapping, log):
    """Re-read the DB and prove the four properties that matter. Returns bad count."""
    bad = 0
    legacy, done, unknown = classify(conn)
    if legacy:
        log(f"  FAIL {len(legacy)} row(s) still carry a legacy id"); bad += 1
    else:
        log("  OK   no legacy-shaped id remains in handle_content")
    if unknown:
        log(f"  FAIL {len(unknown)} row(s) are neither shape"); bad += 1
    else:
        log("  OK   every row is in the allocated window")
    dupes = [(r["content_id"], r["n"]) for r in conn.execute(
        "SELECT content_id, COUNT(*) n FROM handle_content WHERE content_id IS NOT NULL"
        " GROUP BY CAST(content_id AS INTEGER) HAVING n > 1")]
    if dupes:
        log(f"  FAIL a Content ID is on two handles: {dupes}"); bad += 1
    else:
        log("  OK   every Content ID is still unique across all handles")
    # A bijection: the migration must not have collapsed two rows onto one id,
    # and must not have invented or lost one.
    if len(set(mapping.values())) != len(mapping):
        log("  FAIL the old -> new mapping is not one-to-one"); bad += 1
    else:
        log(f"  OK   the mapping is one-to-one ({len(mapping)} ids)")
    if set(mapping) & set(mapping.values()):
        log("  FAIL an old id was re-used as a new one"); bad += 1
    else:
        log("  OK   no old id was recycled as a new one")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="accounts.db (default: accounts.DEFAULT_DB)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--data-dir", default=None,
                    help="where the JSON stores live (default: the DB's directory)")
    ap.add_argument("--map-out", default=None,
                    help="where to write the old->new mapping (default: beside the DB)")
    ap.add_argument("--store", action="append", default=[], metavar="PATH",
                    help="an EXTRA JSON store to rewrite, repeatable. The two "
                         "standard ones are found relative to --data-dir; this is "
                         "for a deployment that puts one somewhere else -- dev "
                         "keeps ffxi_idmap.json in lsb/ while prod's is in data/, "
                         "and a store this tool does not visit is a stale id that "
                         "nothing will ever correct")
    args = ap.parse_args()

    db_path = args.db or A.DEFAULT_DB
    data_dir = args.data_dir or os.path.dirname(os.path.abspath(db_path))
    out = []

    def log(msg):
        print(msg)
        out.append(msg)

    log(f"Content ID migration -- {'APPLY' if args.apply else 'DRY RUN'}")
    log(f"  db       {db_path}")
    log(f"  data dir {data_dir}")
    log(f"  window   {A.CONTENT_ID_FLOOR}..{A.CONTENT_ID_CEILING}")
    log("")

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(A.SCHEMA)          # the counter table, on an old DB
    A._migrate(conn)
    conn.commit()

    legacy, done, unknown = classify(conn)
    log(f"handle_content: {len(legacy)} legacy, {len(done)} already allocated, "
        f"{len(unknown)} unrecognised")

    if unknown:
        log("")
        log("REFUSING TO RUN -- these rows are neither shape, and guessing is how")
        log("a migration eats data. Resolve them by hand first:")
        for r in unknown:
            log(f"  handle {r['handle_id']} ({r['handle_name']!r}) code "
                f"{r['content_code']}: {r['content_id']!r}")
        return 2
    if not legacy:
        log("")
        log("Nothing to migrate: every row already carries an allocated id.")
        return 0

    # --- the mapping ------------------------------------------------------ #
    # Allocated through the SAME function the live mint uses, so the ids this
    # produces are indistinguishable from an ordinary new player's, and the
    # counter is left correct for the next one. Ordered by member so an
    # account's ids come out contiguous, which is what a fresh account looks
    # like anyway.
    mapping = {}
    plan = []
    for r in legacy:
        old = A.content_id_int(r["content_id"])
        new = A.allocate_content_id(conn)
        mapping[old] = int(new)
        plan.append((r, old, int(new)))

    log("")
    log("  member  handle                     game  old id        new id")
    for r, old, new in plan:
        log(f"  {r['member_id']:<7} {str(r['handle_name'])[:24]:<26} "
            f"{r['content_code']:<5} {old:<13} {new}   "
            f"USER/{old:08x} -> USER/{new:08x}")

    if not args.apply:
        conn.rollback()
        log("")
        log("DRY RUN -- nothing was written. The ids above are what --apply would")
        log("issue; the counter was rolled back, so re-running gives the same plan.")
        log("")
        log("Also inspected (not written):")
        for name, p in _stores(data_dir, args.store):
            rewrite_json(p, mapping, False, log)
        log("")
        log("Re-run with --apply to perform it.")
        return 0

    # --- apply ------------------------------------------------------------ #
    backup = f"{db_path}.bak-cidmigrate-{stamp()}"
    shutil.copy2(db_path, backup)
    log("")
    log(f"backup: {backup}")

    for r, old, new in plan:
        conn.execute("UPDATE handle_content SET content_id = ? WHERE rowid = ?",
                     (str(new), r["rid"]))
    conn.commit()
    log(f"handle_content: {len(plan)} row(s) re-minted")

    log("")
    log("JSON stores:")
    for name, p in _stores(data_dir, args.store):
        n = rewrite_json(p, mapping, True, log)
        if n:
            log(f"    {p}: {n} value(s) moved")

    map_path = args.map_out or os.path.join(
        os.path.dirname(os.path.abspath(db_path)),
        f"content-id-migration-{stamp()}.json")
    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump({
            "generated": stamp(),
            "db": os.path.abspath(db_path),
            "note": "old -> new Content IDs. Feed to work/pc/ffxi_userdir_rename.py "
                    "on every machine with a FINAL FANTASY XI install.",
            "map": [{"member_id": r["member_id"], "handle": r["handle_name"],
                     "content_code": r["content_code"], "old": old, "new": new,
                     "old_hex": f"{old:08x}", "new_hex": f"{new:08x}"}
                    for r, old, new in plan],
        }, fh, indent=1)
    log(f"mapping: {map_path}")

    log("")
    log("VERIFY")
    bad = verify(conn, mapping, log)
    conn.close()

    log("")
    if bad:
        log(f"RESULT: {bad} CHECK(S) FAILED -- the backup above is the way back")
        return 1
    log("RESULT: migrated. TWO THINGS ARE STILL OUTSTANDING and this tool cannot")
    log("do either of them:")
    log(f"  1. On EVERY machine with a FINAL FANTASY XI install, rename the local")
    log(f"     per-character directories:")
    log(f"         python work/pc/ffxi_userdir_rename.py --map {os.path.basename(map_path)}")
    log(f"     Until that runs, those players keep their characters and lose their")
    log(f"     macros and config to a freshly created empty directory.")
    log(f"  2. Regenerate the Tetra Master ranking blobs, which embed the id per")
    log(f"     row:  python tools/tmrank.py --publish")
    log("  ...and anyone connected right now must relog: the client cached the old")
    log("  ids from 1:3 at login.")
    return 0


def _stores(data_dir, extra=()):
    """The JSON stores that carry a Content ID, as (label, path).

    `extra` is `--store`, and it exists because the deployments disagree about
    where these live: prod's `ffxi_idmap.json` is in `data/` (the bridge's
    `FFXI_IDMAP_FILE`), the dev tree's is in `lsb/`. A store this function does
    not name is a Content ID nothing will ever correct, so an unusual layout
    gets named on the command line rather than guessed at.
    """
    out = [
        ("ffxi_idmap", os.path.join(data_dir, "ffxi_idmap.json")),
        ("tm pool", os.path.join(data_dir, "resources", "tmrank", "pool.json")),
    ]
    seen = {os.path.abspath(p) for _, p in out}
    for p in extra or ():
        if os.path.abspath(p) not in seen:
            out.append(("extra", p))
            seen.add(os.path.abspath(p))
    return out


if __name__ == "__main__":
    sys.exit(main())
