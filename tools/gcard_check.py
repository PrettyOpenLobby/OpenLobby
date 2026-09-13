"""Does the portal actually hold every greeting card its own list pages offer?

Sending a card is client-driven: the
message carries a card PATH REFERENCE and the recipient's Viewer fetches the
image from our portal. The archive half is done, so the question this answers is the narrow
one -- **can the fetch succeed for every card SE's own index names?** A card
listed but not mirrored is a broken image on somebody's message, and it is
invisible until a real client picks that exact card.

The index is SE's, not ours. Each `list/*/*.pml` holds a `$arDlList` array whose
rows are documented in the file's own header comment:

    [2] the card's name
    [3] the PREVIEW image path      gcard/gc_s/cards/ff11/.../ff11_08summ001s.png
    [4] the DOWNLOAD image path     ff11/.../ff11_08summ001.png

-- so [3] is relative to the magazine root and [4] is relative to
`gcard/gc_s/cards/`. Both are checked, because the card picker draws the preview
and the message draws the full-size one, and either being absent is a hole.

WARNING: **[4] IS ALSO THE STRONGEST LEAD ON THE MESSAGE'S REFERENCE FORMAT**, which
remains blocked on RE'ing the client (SE's own card download is
broken against live SE, so it cannot be sniffed). It is the one path SE stores
in a form that is neither a URL nor tied to the list page's own location, which
is exactly what a reference embedded in a message would have to be. Not a
measurement -- the client still has to be traced -- but it is where to look.

    python tools/gcard_check.py            # exit non-zero if a card is missing
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GCARD = os.path.join(ROOT, "www", "wh000.pol.com", "pml", "magazine", "gcard")
CARDS = os.path.join(GCARD, "gc_s", "cards")

#: One `<array>` row of `$arDlList`. The fields are comma-separated quoted
#: strings; only the three that name something are pulled out. Rows whose [0] is
#: "99" are sub-category HEADINGS and name no card -- they are skipped by the
#: preview field being empty rather than by the flag, so a heading that DOES
#: carry art is still checked.
ROW = re.compile(rb'<array>((?:\s*"[^"]*"\s*,?)+)\s*</array>')
FIELD = re.compile(rb'"([^"]*)"')


def rows():
    """Every (source, name, preview, download) the list pages declare."""
    out = []
    for dirpath, _dirs, files in os.walk(os.path.join(GCARD, "list")):
        for fn in sorted(files):
            if not fn.endswith(".pml"):
                continue
            full = os.path.join(dirpath, fn)
            with open(full, "rb") as fh:
                blob = fh.read()
            for m in ROW.finditer(blob):
                f = [x.decode("cp932", "replace") for x in FIELD.findall(m.group(1))]
                if len(f) < 5:
                    continue
                name, preview, download = f[2], f[3], f[4]
                if not preview and not download:
                    continue
                out.append((os.path.relpath(full, GCARD), name, preview, download))
    return out


def main():
    if not os.path.isdir(GCARD):
        print(f"no greeting-card tree at {GCARD} -- nothing to check")
        return 0
    declared = rows()
    if not declared:
        # An index that parses to nothing is a silent pass, and a silent pass on
        # a coverage check is worse than a failure: it reports "all present" for
        # a tree it never looked at.
        print(f"FAILED: parsed no card rows out of {GCARD}/list -- the $arDlList "
              "shape changed, or the mirror is empty")
        return 1
    missing, checked = [], 0
    for src, name, preview, download in declared:
        for label, rel, base in (("preview", preview, os.path.dirname(GCARD)),
                                 ("download", download, CARDS)):
            if not rel:
                continue
            checked += 1
            if not os.path.isfile(os.path.join(base, *rel.split("/"))):
                missing.append((src, name, label, rel))
    print(f"{len(declared)} card row(s) across "
          f"{len({d[0] for d in declared})} list page(s); {checked} image path(s) "
          f"checked against {os.path.relpath(GCARD, ROOT)}")
    if missing:
        print(f"\n{len(missing)} MISSING -- a client that picks these gets a "
              "broken image:")
        for src, name, label, rel in missing[:30]:
            print(f"  {src}: {name!r} {label} -> {rel}")
        if len(missing) > 30:
            print(f"  ... and {len(missing) - 30} more")
        return 1
    print("every card the index offers is mirrored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
