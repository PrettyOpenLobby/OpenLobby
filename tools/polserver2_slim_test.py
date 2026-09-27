#!/usr/bin/env python3
"""--slim-list / POLP_SLIM_LIST: the disc's own files leave the cmd-1 list.

    python tools/polserver2_slim_test.py

A PS2 can give up on a patch list a few seconds after asking for it
(POL-0006), and for some PS2 titles almost all of the list is the retail disc
catalogue.  What must hold:

  * a file with ONLY disc rows is left out;
  * a file with ANY later row is kept WHOLE, its disc row included;
  * the default base is the oldest ROW, not meta.json's oldest_version --
    the synthesised PS2 bundles set that to 00000000_0 on purpose, and the
    first cut used it, which made the whole slim a silent no-op;
  * the served list still decompresses and still ends in `end`.

The last case is the twin: a list that LOST a patched file must fail the same
checks the good one passes.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import polserver2                                                # noqa: E402
from slc import slc_compress, slc_decompress                     # noqa: E402

DISC, RETAIL, OURS = "20050324_0", "20060124_3", "20260923_0"

LIST = "\n".join([
    "file A0/disc_only.BIN {",
    f"{DISC} 8320 6980 -444687240 {DISC}/Direct/A0/disc_only.BIN.slc 657",
    "}",
    "",
    "file A0/patched.BIN {",
    f"{DISC} 522 15832 -2088161968 {DISC}/Direct/A0/patched.BIN.slc 159",
    f"{OURS} 530 15900 -2088161000 {OURS}/Direct/A0/patched.BIN.slc 161",
    "}",
    "",
    "file A0/retail_patch.BIN {",
    f"{DISC} 100 1 1 {DISC}/Direct/A0/retail_patch.BIN.slc 10",
    f"{RETAIL} 101 2 2 {RETAIL}/Direct/A0/retail_patch.BIN.slc 11",
    "}",
    "",
    "file new_file.dat {",
    f"{OURS} 43 3244 -1628133576 {OURS}/Direct/new_file.dat.slc 44",
    "}",
    "",
    "end",
    "",
]).encode("latin-1")

bad = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        bad.append(name)


def names(plain):
    return [ln[5:-2] for ln in plain.decode("latin-1").split("\n")
            if ln.startswith("file ")]


def block(plain, path):
    lines = plain.decode("latin-1").split("\n")
    i = lines.index(f"file {path} {{")
    return lines[i:lines.index("}", i) + 1]


def holds(orig, slim, base):
    """Every kept block identical, every dropped block base-only, no newer row
    lost, trailer intact -- the checks a real list is held to."""
    kept = names(slim)
    if not set(kept) <= set(names(orig)):
        return False
    for p in names(orig):
        rows = block(orig, p)[1:-1]
        if p in kept:
            if block(slim, p) != block(orig, p):
                return False
        elif max(r.split(" ")[0] for r in rows) > base:
            return False
    return slim.rstrip().endswith(b"end")


# --- slim_patchlist on its own -------------------------------------------------
slim, dropped, kept = polserver2.slim_patchlist(LIST, DISC)
check("disc-only file left out", names(slim) == ["A0/patched.BIN",
      "A0/retail_patch.BIN", "new_file.dat"])
check("counts 1 dropped / 3 kept", (dropped, kept) == (1, 3))
check("patched file kept WHOLE, disc row included",
      block(slim, "A0/patched.BIN") == block(LIST, "A0/patched.BIN"))
check("trailer `end` survives", slim.rstrip().endswith(b"end"))
check("good slim passes the checks", holds(LIST, slim, DISC))

# An explicit later base hides what that version touched -- by request only.
slim_r, _, _ = polserver2.slim_patchlist(LIST, RETAIL)
check("explicit base RETAIL also leaves out retail_patch",
      "A0/retail_patch.BIN" not in names(slim_r) and "A0/patched.BIN" in names(slim_r))

# TWIN: a list that lost a patched file must FAIL the same checks.
lost = LIST.replace(b"file A0/patched.BIN {", b"file A0/other.BIN {", 1)
check("control: a list missing a patched file FAILS the checks",
      not holds(LIST, polserver2.slim_patchlist(lost, DISC)[0], DISC))

# --- apply_slim_lists end to end, on a synthesised PS2 bundle ------------------
with tempfile.TemporaryDirectory() as root:
    b = os.path.join(root, "P2U-0004")
    os.makedirs(os.path.join(b, "blobs"))
    with open(os.path.join(b, "patchlist.raw"), "wb") as f:
        f.write(slc_compress(LIST))
    with open(os.path.join(b, "meta.json"), "w") as f:
        # a synthesised PS2 bundle's meta: oldest_version is NOT the disc
        json.dump({"region": "P2U", "product": "0004", "port": 53004,
                   "latest_version": OURS, "oldest_version": "00000000_0"}, f)
    bundles = polserver2.load_bundles(root)
    polserver2.apply_slim_lists(bundles, ["P2U/0004"])
    served = slc_decompress(bundles[("P2U", "0004")].patchlist)
    check("default base = oldest ROW, not meta's 00000000_0",
          "A0/disc_only.BIN" not in names(served))
    check("served list equals slim_patchlist's", served == slim)
    check("advertised latest unchanged", bundles[("P2U", "0004")].latest == OURS)

    try:
        polserver2.apply_slim_lists(bundles, ["P2U/9999"])
        check("unknown bundle refused", False)
    except SystemExit:
        check("unknown bundle refused", True)

print("FAILURES:", bad)
sys.exit(1 if bad else 0)
