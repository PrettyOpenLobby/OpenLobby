#!/usr/bin/env python3
"""Switch the login ticker's badge strip, and say what it changes.

    python tools/news_badge_strip.py                 # what is installed, and the options
    python tools/news_badge_strip.py --use se-8frame # install one

WHY THIS EXISTS
---------------
`pml/main/index.pml` draws the ticker badge with
`sd:sequence=$LATESTNEWS[i][0]@icNews<i>`, so the content id IS the frame index
of `ma_i/maic06i.ang`. That has two consequences worth a tool:

* the set of services an admin can file news under is exactly the set of
  frames in this ONE file -- `newsgen.available_contents()` reads it and the
  admin panel offers only what it can draw;
* a strip the Viewer REJECTS does not lose one badge, it loses the whole image.
  On 2026-08-21 a rejected strip drew the broken-image box across the ticker for
  stories that only ever used frame 1.

Which strips the Viewer accepts is still an open question and only a real client
can answer it, so this makes the A/B cheap and hard to get wrong: it verifies
the file parses before installing it, and prints the exact services that will
appear or disappear.

WARNING: AFTER INSTALLING: commit and push (prod is git-synced; never edit on the box),
then CLEAR THE VIEWER'S URL CACHE or you will be looking at its cached copy of
the old file and conclude nothing changed. Back up `usr/all/url/cache`, delete
`*.dcf` and `dcfat*.bin`, elevated. That has bitten this project before.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SRV, "services"))
import newsgen  # noqa: E402

MA_I = os.path.join(SRV, "www", "wh000.pol.com", "pml", "main", "ma_i")
ANG = os.path.join(MA_I, "maic06i.ang")

VARIANTS = {
    "se-orig": ("maic06i.ang.se-orig",
                "SE's 6-frame strip. The file that served for months and the "
                "only one with a working history. Safe floor."),
    "se-8frame": ("maic06i.ang.se-8frame",
                  "SE's 8-frame strip, from band 51300 and the JP PS2 1.18.15 "
                  "cache (identical). Adds Dirge of Cerberus and Fantasy Earth. "
                  "PROVEN on a real client 2026-08-21 -- the current default, "
                  "and the one to go back to."),
    "reencoded": (None,
                  "BISECT: SE's EIGHT frames re-encoded through our pipeline, "
                  "nothing added. Same frames, same cells, same 0x2C -- only "
                  "the image blobs differ. Offers exactly what se-8frame does, "
                  "so if the ticker breaks on this, our encoder is the fault "
                  "and the frame count is innocent."),
    "extended": (None,
                 "Ten frames: SE's eight plus EverQuest II and FINAL FANTASY "
                 "XIV, built by tools/make_news_badges.py from SE's cicn badge "
                 "art. OURS, and the build the Viewer REJECTED on 2026-08-21."),
}


def frames_of(path):
    try:
        with open(path, "rb") as fh:
            return len(newsgen.ang_frames(fh.read()))
    except (OSError, newsgen.NewsError) as exc:
        return exc


def services_for(n):
    return sorted((v, k) for k, v in newsgen.CONTENTS.items() if v < n)


def show():
    cur = frames_of(ANG)
    print("installed: maic06i.ang -- %s frame(s)" % cur)
    print("offers   : %s" % ", ".join(
        "%d %s" % (i, newsgen.CONTENT_LABELS[k]) for i, k in services_for(cur))
        if isinstance(cur, int) else "  (unreadable)")
    print()
    for name, (fn, why) in VARIANTS.items():
        path = os.path.join(MA_I, fn) if fn else None
        n = frames_of(path) if path and os.path.exists(path) else "(built on demand)"
        mark = " <- installed" if isinstance(n, int) and isinstance(cur, int) \
            and path and open(path, "rb").read() == open(ANG, "rb").read() else ""
        print("  %-10s %s frame(s)%s" % (name, n, mark))
        print("             %s" % why)
        if isinstance(n, int):
            gained = [k for i, k in services_for(n)
                      if isinstance(cur, int) and i >= cur]
            if gained:
                print("             would ADD: %s"
                      % ", ".join(newsgen.CONTENT_LABELS[k] for k in gained))
    print()
    print("after --use: commit + push, then CLEAR THE VIEWER'S URL CACHE")


def use(name):
    fn, _ = VARIANTS[name]
    if name in ("extended", "reencoded"):
        import make_news_badges
        sys.argv = [sys.argv[0]] + (["--reencode-only"] if name == "reencoded" else [])
        if make_news_badges.main() != 0:
            sys.exit("build failed; nothing installed")
        print()
        show()
        return
    src = os.path.join(MA_I, fn)
    if not os.path.exists(src):
        sys.exit("%s is missing" % src)
    n = frames_of(src)
    if not isinstance(n, int):
        sys.exit("%s does not parse as @ANG1B: %s" % (fn, n))
    shutil.copy2(src, ANG)
    print("installed %s (%d frames)" % (fn, n))
    print()
    show()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--use", choices=sorted(VARIANTS),
                    help="install a strip and report what it changes")
    args = ap.parse_args()
    sys.path.insert(0, HERE)
    if args.use:
        use(args.use)
    else:
        show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
