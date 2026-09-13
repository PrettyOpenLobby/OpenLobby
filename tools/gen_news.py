#!/usr/bin/env python3
"""Publish server announcements to every PML file the Viewer reads.

The rendering lives in `services/newsgen.py`, NOT here -- the admin dashboard's
News tab publishes the same files from inside a container that mounts
`services/` and never sees `tools/`. This script is the command-line front end
to that module, so the two can never drift.

  wh000.pol.com/pcd/ntool/<loc>/latestnews.pml   the login-screen ticker
  wh000.pol.com/pcd/ntool/<loc>/news<N>.pml      SE's real Information section
  wh000.pol.com/pcd/ntool/<loc>/<serial>.pml     one per announcement: the body
  wh000.pol.com/pml/info/news0.pml               fr-FR / de-DE fallback
  info.playonline.com/snews/<loc>/index.pml      our own substitute page

Read `services/newsgen.py`'s docstring for where each format comes from -- all
of it is transcribed from SE's own pages and captured data, none invented.

Usage:
    python tools/gen_news.py                  # publish
    python tools/gen_news.py --check          # report, write nothing
    python tools/gen_news.py --source F       # read a specific store
    python tools/gen_news.py --www DIR        # target a different tree
    python tools/gen_news.py --no-prune       # keep orphaned <serial>.pml
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "services"))

import newsgen  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=None,
                    help="announcements file (default: the live store if it "
                         "exists, else content/announcements.yaml)")
    ap.add_argument("--www", default=None, help="serving tree to write into")
    ap.add_argument("--check", action="store_true",
                    help="report what would happen, write nothing")
    ap.add_argument("--no-prune", action="store_true",
                    help="leave <serial>.pml files whose announcement is gone")
    a = ap.parse_args()

    source = a.source or newsgen.store_path()
    www = a.www or newsgen.WWW_DIR
    try:
        items = newsgen.load(source)
    except newsgen.NewsError as exc:
        sys.exit(f"{source}: {exc}")

    print(f"{len(items)} announcement(s) from {source}")
    for it in items:
        flag = " [status]" if it.get("status") else ""
        print(f"  {it['serial']}  [{it['kind']:11s}] {it['date']}  "
              f"{it['title']}{flag}")

    res = newsgen.publish(items, www, prune=not a.no_prune, dry_run=a.check)
    verb = "would write" if a.check else "wrote"
    print(f"\n{verb} {len(res['written'])} of {res['total']} file(s) under {www}")
    for rel in res["written"]:
        print(f"  {rel}")
    if res["backed_up"]:
        print(f"\nkept a .se-orig copy of {len(res['backed_up'])} replaced "
              f"file(s) (first replacement only):")
        for rel in res["backed_up"]:
            print(f"  {rel}")
    if res["pruned"]:
        print(f"\n{'would remove' if a.check else 'removed'} "
              f"{len(res['pruned'])} orphaned detail file(s):")
        for rel in res["pruned"]:
            print(f"  {rel}")
    if res["skipped"]:
        # Should be unreachable: outputs() only ever produces allowlisted paths.
        print(f"\nREFUSED (outside the write allowlist): {res['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
