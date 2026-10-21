#!/usr/bin/env python3
"""Rebuild a title's `file.txt` so it agrees with what the patch bundle SERVES.

`file.txt` is the manifest the client's Check Files verifies: one
`digest:size:path` line per installed file. Publish a changed file without
publishing a matching manifest line and the client is one Check Files away
from reverting it, silently, with no error shown.

So the manifest is built from the bundle, not from a local install, because a
local install is not necessarily what clients have. For every path take the
NEWEST published blob, decompress it, and hash those exact bytes. Files about
to be published in this pass come from the overlay and win over the bundle.
Publish the resulting file.txt in the same pass as the files it describes.

    python tools/patch/polmanifest.py --bundle mirrors/W20-0011 \\
                                      --base "D:/Games/ExampleTitle/file.txt" \\
                                      --overlay stage/example --write

Without --write it reports what would change and touches nothing.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))                    # patchver
sys.path.insert(0, os.path.join(HERE, "..", "..", "services"))  # slc, polbundle

import slc                                                       # noqa: E402
from polbundle import manifest                                   # noqa: E402
from polsynth import pol_hash                                    # noqa: E402


def newest_blobs(bundle):
    """path -> (version, blob_relpath) for the newest row of every file block.

    On a tie (the same version twice for one path) the later row wins.
    """
    out = {}
    for r in manifest(bundle):
        prev = out.get(r["path"])
        if prev is None or r["version"] >= prev[0]:
            out[r["path"]] = (r["version"], r["slc"])
    return out


def blob_bytes(bundle, blob_rel):
    p = os.path.join(bundle, "blobs", *blob_rel.split("/"))
    with open(p, "rb") as fh:
        return slc.slc_decompress(fh.read())


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--base", required=True, help="the install's original file.txt")
    ap.add_argument("--overlay", required=True, help="files being published this pass")
    ap.add_argument("--write", action="store_true",
                    help="write the new file.txt into --overlay")
    a = ap.parse_args()

    overlay = {}
    for root, _, files in os.walk(a.overlay):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, a.overlay).replace("\\", "/")
            if rel != "file.txt":
                with open(full, "rb") as fh:
                    overlay[rel] = fh.read()

    served = newest_blobs(a.bundle)
    with open(a.base, "rb") as fh:
        rows = fh.read().split(b"\n")
    # A file.txt may use CRLF line ends. Index each path without the CR and
    # put it back on any line rewritten below, so the file keeps its own form.
    index = {}
    for i, row in enumerate(rows):
        parts = row.rstrip(b"\r").split(b":")
        if len(parts) == 3:
            index[parts[2].decode("utf-8", "replace")] = i

    changed, missing = 0, []
    for path in sorted(set(list(served) + list(overlay))):
        if path == "file.txt":
            continue
        if path in overlay:
            data, src = overlay[path], "overlay"
        else:
            ver, blob = served[path]
            data, src = blob_bytes(a.bundle, blob), ver
        line = ("%s:%d:%s" % (pol_hash(data), len(data), path)).encode()
        i = index.get(path)
        if i is None:
            missing.append(path)
            continue
        if rows[i].endswith(b"\r"):
            line += b"\r"
        if rows[i] != line:
            print("  %-46s %s" % (path, src))
            print("      was %s" % rows[i].rstrip(b"\r").decode("utf-8", "replace"))
            print("      now %s" % line.rstrip(b"\r").decode())
            rows[i] = line
            changed += 1

    if missing:
        print("NOT IN file.txt (%d): %s" % (len(missing), missing[:5]))
    print("%d manifest line(s) need rewriting" % changed)

    if a.write:
        out = os.path.join(a.overlay, "file.txt")
        with open(out, "wb") as fh:
            fh.write(b"\n".join(rows))
        print("wrote %s" % out)


if __name__ == "__main__":
    main()
