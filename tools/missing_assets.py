#!/usr/bin/env python3
"""What has the client asked for that we do not have?

    python tools/missing_assets.py [--log logs/lobby.log] [--www www]

WHY THIS EXISTS
---------------
The news detail page rendered as a bare background because
`pml/info/ne_s/sh01s.png`, the panel its text sits on, was not in the tree.
Nothing surfaced it: **the server answers a missing asset with an EMPTY PML
BODY, not a 404**, so the client gets a valid-looking reply and simply draws
nothing. No error in the client, no error in the log unless you go looking.

Why it was absent when we hold the file in a mirror: every `src` on that page is
COMPUTED --

    src="$C_PATH1+'ne_s/sh01s.png'"

so a crawler matching `src="<path>"` extracts `$C_PATH1+'ne_s/...'`, cannot
resolve it, and never asks. Same blind spot that cost the main-menu shortcut
icons; see the computed-urls-are-invisible-to-the-crawl note.

WHY THIS READS THE LOG AND NOT THE PAGES
----------------------------------------
A static sweep was tried first and abandoned: `$C_PATH1` means different things
in `pml/info/`, `pml/game/`, `pml/game/ff11/` and so on, so resolving it without
following each page's own include chain guesses, and guessing produced ~12,000
"missing" files that mostly do not exist as references at all. The log has no
such problem -- it is what the client actually requested, already resolved by
the client itself. Zero false positives, at the cost of only covering pages
someone has actually visited.

So: browse the client, then run this.
"""
import argparse
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)

EMPTY = re.compile(r"served EMPTY PML for (\S+)")
REQ = re.compile(r"GET (\S+) \(Host: ([^;)]+)")
# art the CLIENT owns; a request for one of these is not our gap
CLIENT_SIDE = ("file:", "gameto:")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=os.path.join(SRV, "logs", "lobby.log"))
    ap.add_argument("--www", default=os.path.join(SRV, "www"))
    ap.add_argument("--mirror", default=os.path.join(
        SRV, os.pardir, "_external-archives", "cortex-p-gen-nz-2015", "mirror"),
        help="checked for a recoverable copy of anything missing")
    args = ap.parse_args()

    if not os.path.isfile(args.log):
        sys.exit("no log at %s -- pass --log, or copy one off prod" % args.log)

    missing = collections.Counter()
    host_of = {}
    last_host = "wh000.pol.com"
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = REQ.search(line)
            if m:
                last_host = m.group(2).strip()
            m = EMPTY.search(line)
            if m:
                path = m.group(1)
                if path.startswith(CLIENT_SIDE):
                    continue
                missing[path] += 1
                host_of.setdefault(path, last_host)

    if not missing:
        print("nothing requested-but-absent in %s" % os.path.relpath(args.log, SRV))
        return 0

    print("%d asset(s) the client asked for and we did not have:\n" % len(missing))
    recoverable = []
    for path, n in missing.most_common():
        host = host_of.get(path, "wh000.pol.com")
        rel = os.path.join(host, path.lstrip("/"))
        found = ""
        for band in ("51300", "51304"):
            cand = os.path.join(args.mirror, "%s-%s" % (host, band),
                                path.lstrip("/"))
            if os.path.isfile(cand):
                found = "  -> in mirror %s (%d B)" % (band, os.path.getsize(cand))
                recoverable.append((rel, cand))
                break
        print("  %-48s x%-4d %s%s"
              % (path, n, "MISSING", found or "  (not in the cortex mirror)"))

    print()
    print("%d of %d recoverable from SE's mirror." % (len(recoverable), len(missing)))
    if recoverable:
        print("Copy them in, then re-check -- and remember the client caches, so")
        print("clear usr/all/url/cache before deciding whether it worked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
