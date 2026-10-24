#!/usr/bin/env python3
"""Publish a NEW version into an existing patch bundle.

`polsynth.py` gives a title its base tree: one version, matching the copy on
disk. This layers the next one on top (a newer build, a translation, a fix),
so a bundle grows a real version history instead of being rebuilt and losing
the client's upgrade path.

Why deliver changed files through the patch server rather than editing the
install: the client verifies its installed files against the manifest it was
served, so a file swapped in by hand matches no row and gets reverted or
refused. Published through here, the new `patch.cfg` arrives with the files and
the checksums agree by construction.

Two ways to say what changed:

  --overlay DIR   the files that changed, laid out as they sit in the install.
                  Everything in DIR is published; nothing else is touched.
  --from TREE     a whole newer copy of the title. Every file whose bytes differ
                  from the bundle's current newest version is published, and
                  files absent from the bundle are added as new blocks (a title
                  that grew files between builds is normal).

Both leave unchanged files alone, so a client already at the previous version
downloads only the difference, which is what the version rows are for.

What one version writes (the formats are in docs/patching.md):

  blobs/<ver>/Direct/<path>.slc   `[u8 method][payload]`, method 1 stored or 3
                                  zlib, whichever is smaller
  patch.cfg                       a row appended to that file's block
  patchlist.raw                   what the client is actually served for cmd 1:
                                  the same bytes in an .slc method-2 container
  meta.json                       latest_version, which is what cmd 8 reports
  manifest.json                   derived index, kept in step with patch.cfg

The first publish copies every file it touches to `<name>.prepublish`, and
later publishes leave those copies alone. `--revert` puts them back, which
removes EVERY version published since that first publish, not only the most
recent one. There is no single-step undo. Run `--revert --dry-run` first to see
what it would remove.

    python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --status
    python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --version 20040714_0 \\
                                     --from "D:/Games/newer/TetraMaster" --dry-run
    python tools/patch/polpublish.py --bundle mirrors/W2U-0002 --revert --dry-run
"""
import argparse
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))                    # patchver
sys.path.insert(0, os.path.join(_HERE, "..", "..", "services"))  # slc, polbundle
import polmanifest                                               # noqa: E402
import slc                                                       # noqa: E402
from polbundle import manifest, signed_bytesum                   # noqa: E402
from polsynth import (md5_4, patchlist_encode, read_list,        # noqa: E402
                      slc_encode, walk_files)

BACKED_UP = ("patch.cfg", "patchlist.raw", "meta.json", "manifest.json")
VER_RE = re.compile(r"^(\d{8}_\w+) ", re.M)


def version_key(v):
    """Sort key for patch versions: the date, then the suffix, shorter first, so a
    same-day _9 comes before _10 (SE's suffixes are one character; ours run to two)."""
    return (v[:8], len(v) - 9, v[9:])


def backup(bundle, restore=False):
    for name in BACKED_UP:
        live = os.path.join(bundle, name)
        keep = live + ".prepublish"
        if restore:
            if os.path.exists(keep):
                shutil.copy2(keep, live)
        elif not os.path.exists(keep) and os.path.exists(live):
            shutil.copy2(live, keep)


def versions_in(cfg):
    return sorted(set(VER_RE.findall(cfg)), key=version_key)


def added_versions(bundle):
    """Versions in patch.cfg that the .prepublish copy does not have."""
    cfg_path = os.path.join(bundle, "patch.cfg")
    pre = cfg_path + ".prepublish"
    if not os.path.exists(pre):
        return []
    now = set(versions_in(open(cfg_path, encoding="latin-1").read()))
    before = set(versions_in(open(pre, encoding="latin-1").read()))
    return sorted(now - before)


def revert(bundle, dry_run=False):
    """Restore the .prepublish copies and delete every published version's blobs.

    With dry_run nothing is written, moved or deleted: it only reports what a
    real revert would do. Returns the versions that are (or would be) removed.
    """
    cfg_path = os.path.join(bundle, "patch.cfg")
    if not os.path.exists(cfg_path + ".prepublish"):
        print("nothing to revert - no .prepublish backup")
        return []
    added = added_versions(bundle)
    if dry_run:
        print(f"[*] --dry-run: --revert would remove ALL {len(added)} version(s) "
              f"published since the first publish: {', '.join(added) or 'none'}")
        for ver in added:
            if os.path.isdir(os.path.join(bundle, "blobs", ver)):
                print(f"      would remove blobs/{ver}")
        restored = [n for n in BACKED_UP
                    if os.path.exists(os.path.join(bundle, n) + ".prepublish")]
        print(f"      would restore {', '.join(restored)} from .prepublish")
        print("[*] nothing changed")
        return added
    backup(bundle, restore=True)
    for ver in added:
        d = os.path.join(bundle, "blobs", ver)
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"removed blobs/{ver}")
    for name in BACKED_UP:
        keep = os.path.join(bundle, name) + ".prepublish"
        if os.path.exists(keep):
            os.remove(keep)
    print(f"reverted {bundle} to its pre-publish state "
          f"({len(added)} version(s) removed)")
    return added


def newest_rows(bundle):
    """path -> the row carrying that file's newest version."""
    out = {}
    for r in manifest(bundle):
        cur = out.get(r["path"])
        if cur is None or version_key(r["version"]) > version_key(cur["version"]):
            out[r["path"]] = r
    return out


def blob_bytes(bundle, row):
    with open(os.path.join(bundle, "blobs", *row["slc"].split("/")), "rb") as fh:
        return slc.slc_decompress(fh.read())


def collect(args, bundle):
    """-> [(relpath, new bytes)] for the files this version changes."""
    if args.overlay:
        files = []
        for dirpath, _, names in os.walk(args.overlay):
            for nm in sorted(names):
                full = os.path.join(dirpath, nm)
                rel = os.path.relpath(full, args.overlay).replace(os.sep, "/")
                files.append((rel, open(full, "rb").read()))
        return files, []

    src = args.source
    rows = read_list(src)
    listing = rows if rows is not None else walk_files(src, set())
    current = newest_rows(bundle)
    changed, unchanged = [], 0
    for rel, _size, _hash in listing:
        full = os.path.join(src, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        raw = open(full, "rb").read()
        row = current.get(rel)
        # Compare against the bytes the bundle would actually serve, not against
        # a checksum column: this is the one place a corrupt blob in the bundle
        # would otherwise be silently carried forward as "unchanged".
        if row is not None and len(raw) == row["size"] and blob_bytes(bundle, row) == raw:
            unchanged += 1
            continue
        changed.append((rel, raw))
    gone = sorted(set(current) - {rel for rel, _, _ in listing})
    print(f"[*] {len(listing)} file(s) in the source: {len(changed)} changed or "
          f"new, {unchanged} unchanged")
    return changed, gone


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--version", help="the new version, e.g. 20040714_0")
    ap.add_argument("--overlay", help="directory of just the changed files")
    ap.add_argument("--from", dest="source", help="a whole newer copy of the title")
    ap.add_argument("--revert", action="store_true",
                    help="remove EVERY version published with this tool and "
                         "restore the bundle to how it was before the first "
                         "publish. It does not undo just the last version. "
                         "Combine with --dry-run to see what it would remove.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be published (or, with --revert, "
                         "removed) and write nothing")
    ap.add_argument("--backfill", action="store_true",
                    help="the version is OLDER than the bundle's latest: an "
                         "older build found after newer ones were published. "
                         "Adds its rows but leaves latest_version alone.")
    ap.add_argument("--note", help="provenance line for meta.json's published[] "
                                   "entry; the default says 'local build'")
    a = ap.parse_args(argv)

    bundle = os.path.abspath(a.bundle)
    cfg_path = os.path.join(bundle, "patch.cfg")
    meta_path = os.path.join(bundle, "meta.json")
    meta = json.load(open(meta_path))

    if a.status:
        print(f"bundle          {bundle}")
        print(f"service         {meta.get('region')}/{meta.get('product')} "
              f"port {meta.get('port')}")
        print(f"latest_version  {meta.get('latest_version')}")
        print(f"synthesised     {bool(meta.get('synthesised'))}")
        added = added_versions(bundle)
        print(f"published here  {', '.join(added) if added else 'none'}")
        return 0

    if a.revert:
        revert(bundle, dry_run=a.dry_run)
        return 0

    if not a.version:
        ap.error("--version is required to publish")
    if bool(a.overlay) == bool(a.source):
        ap.error("give exactly one of --overlay or --from")
    ver = a.version
    if not re.match(r"^[1-9]\d{7}_\w+$", ver):
        ap.error(f"{ver!r} is not a version shape the client's status rule "
                 f"accepts: [1-9] + 7 digits + '_' + a suffix")

    cfg = open(cfg_path, encoding="latin-1").read()
    if re.search(r"^%s " % re.escape(ver), cfg, re.M):
        raise SystemExit(f"{ver} is already in patch.cfg - --revert first")
    if version_key(ver) <= version_key(meta.get("latest_version", "")) and not a.backfill:
        raise SystemExit(
            f"{ver} is not newer than the bundle's latest ({meta['latest_version']}). "
            f"cmd 8 hands the client `latest` and it compares as a STRING, so "
            f"publishing backwards would tell every client it is up to date. "
            f"If this is an older build being added after newer ones, pass "
            f"--backfill: it keeps latest_version where it is.")
    if a.backfill and version_key(ver) > version_key(meta.get("latest_version", "")):
        raise SystemExit(
            f"--backfill is for a version OLDER than latest, but {ver} is newer "
            f"than {meta.get('latest_version')!r}. Publish it normally.")

    files, gone = collect(a, bundle)
    if not files:
        print("[*] nothing to publish - the source matches the bundle already")
        return 0
    if gone:
        # Deleting a file has no representation in patch.cfg: a row only ever
        # adds a version. Say so rather than quietly publishing a partial state.
        print(f"[!] {len(gone)} file(s) are in the bundle but not in the source. "
              f"patch.cfg has no way to express a deletion, so they stay:")
        for rel in gone[:10]:
            print(f"      {rel}")

    if a.dry_run:
        print(f"[*] --dry-run: would publish {len(files)} file(s) as {ver}")
        for rel, raw in files[:20]:
            print(f"      {rel}  {len(raw)} B")
        if len(files) > 20:
            print(f"      ... and {len(files)-20} more")
        return 0

    backup(bundle)
    rows = manifest(bundle)
    added = []
    new_blocks = []
    for rel, raw in files:
        blob = slc_encode(raw)
        blob_rel = "%s/Direct/%s.slc" % (ver, rel)
        blob_path = os.path.join(bundle, "blobs", *blob_rel.split("/"))
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        with open(blob_path, "wb") as fh:
            fh.write(blob)
        if slc.slc_decompress(blob) != raw:
            raise SystemExit(f"{rel}: blob does not round-trip")

        row = "%s %d %d %d %s %d" % (ver, len(raw), signed_bytesum(raw),
                                     md5_4(raw), blob_rel, len(blob))
        block = re.search(r"file %s \{\n(.*?)\n\}" % re.escape(rel), cfg, re.S)
        if block:
            cfg = cfg[:block.end(1)] + "\n" + row + cfg[block.end(1):]
        else:
            # A file the title did not have before. Appending a fresh block is
            # correct (patch.cfg is just a list of blocks), but flag it, since
            # in an --overlay run it usually means a mistyped path.
            new_blocks.append(rel)
            cfg = cfg.replace("\nend\n", "\nfile %s {\n%s\n}\n\nend\n" % (rel, row), 1)
        rows.append({"path": rel, "version": ver, "size": len(raw),
                     "bytesum": signed_bytesum(raw), "md5_4": md5_4(raw, signed=False),
                     "slc": blob_rel, "slc_size": len(blob),
                     "olc": None, "olc_size": 0})
        added.append((rel, len(raw), len(blob), blob[0]))

    with open(cfg_path, "w", encoding="latin-1", newline="") as fh:
        fh.write(cfg)
    payload = cfg.encode("latin-1")
    listraw = patchlist_encode(payload)
    if slc.slc_decompress(listraw) != payload:
        raise SystemExit("patch list does not round-trip")
    with open(os.path.join(bundle, "patchlist.raw"), "wb") as fh:
        fh.write(listraw)
    with open(os.path.join(bundle, "manifest.json"), "w") as fh:
        json.dump(rows, fh)

    if not a.backfill:
        meta["latest_version"] = ver
    meta["version_count"] = meta.get("version_count", 0) + 1
    meta["blob_count"] = meta.get("blob_count", 0) + len(added)
    meta["row_count"] = len(rows)
    meta["published"] = meta.get("published", []) + [{
        "version": ver,
        "files": [r for r, _, _, _ in added],
        "new_files": new_blocks,
        "source": os.path.abspath(a.overlay or a.source),
        "note": a.note or "local build",
        **({"backfill": True} if a.backfill else {}),
    }]
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)

    print(f"published {ver} to {os.path.basename(bundle)}: {len(added)} file(s)")
    for rel, size, bsize, method in added[:20]:
        print(f"   %-42s %8d B -> %8d B .slc (method %d)" % (rel, size, bsize, method))
    if len(added) > 20:
        print(f"   ... and {len(added)-20} more")
    if new_blocks:
        print(f"   [!] {len(new_blocks)} file(s) had no block in patch.cfg and were "
              f"ADDED as new: {', '.join(new_blocks[:5])}"
              + (" ..." if len(new_blocks) > 5 else ""))
    if a.backfill:
        # A backfilled row only changes what a client is SERVED where it is the
        # newest row for that path. Saying so here stops "published N files"
        # being read as "N files changed on the wire".
        newest = polmanifest.newest_blobs(bundle)
        wins = sum(1 for rel, _, _, _ in added if newest.get(rel, ("",))[0] == ver)
        print(f"latest_version left at {meta['latest_version']} (--backfill)")
        print(f"   {wins} of {len(added)} backfilled row(s) are the newest for "
              f"their path and will actually be served; the rest are outranked "
              f"by a later version already in the bundle")
    else:
        print(f"latest_version is now {ver}")
    print("restart polserver2 - a bundle's latest_version is read once at startup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
