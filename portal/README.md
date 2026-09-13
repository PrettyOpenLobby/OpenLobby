# Portal edits and authored pages

The PlayOnline Viewer's portal is a tree of PML pages that Square Enix served
from `wh000.pol.com`. This repository does not include those pages. What it
includes is the part that is ours:

- `patches/` - our edits to pages SE served, as zero-context unified diffs
  (`diff -U0` format). Each diff carries only the lines we changed; the
  surrounding page is never in this repository. `manifest.json` lists every
  patch with the SHA-256 of the SE original it applies to, the SHA-256 of the
  result, and a one-line description of what the edit does.
- `authored/` - pages we wrote from scratch, for content the Western portal
  never had a page for (Dirge of Cerberus, Fantasy Earth, JongHoLow, and two
  informational pages). They are stored with a `.txt` suffix and renamed on
  install. `authored/IMAGES-NEEDED.txt` lists the few menu images those pages
  expect, which you supply yourself.

## What the patches change

Run `python tools/portal_patch.py --selftest` to list them, or read
`patches/manifest.json`. In short: the Games submenu lists every title this
server can grant; the main menu shows the GM Call button and no longer renders
an array error on platforms without banner rows; the banner data files are
opened to the PS2; the FFXI and Front Mission Online top pages get the
background, music and buttons SE had left commented out; the Tetra Master top
page is translated to English; one support-page constant goes from https to
http; and three panel-era copies of the main-menu pages give a PS2 client a
language variable it otherwise lacks.

## Getting a base tree

You need the portal pages themselves before anything here is useful.

1. Your own capture: if you archived the portal while the Viewer could still
   reach it, use that. Its layout is `<host>/<path>`, so the main menu is at
   `www/wh000.pol.com/pml/main/index.pml`.
2. `tools/portal_crawl.py`: fetches pages over HTTP from a portal host that
   still serves them, authenticating with a live Viewer login. Read its
   docstring; it needs a working account on the server it is pointed at and it
   is deliberately slow and polite.

Put the tree at `www/` (or wherever `POL_WWW` in `.env` points).

## Applying

```
python tools/portal_patch.py --www ./www --check   # dry run: report only
python tools/portal_patch.py --www ./www           # apply
```

For every patch the tool hashes the page in your tree first:

- hash equals the SE original: the diff is applied in pure Python, the result
  is hash-checked, and the untouched page is kept as `<file>.orig`;
- hash equals the patched result: reported as already applied;
- anything else: reported as an unexpected version with both hashes, and
  skipped. Your page is a different revision of SE's file than the one the
  patch was made from; nothing is written.

Then the authored pages are copied in. A page that already exists in your
tree is left alone unless you pass `--force`, in which case the existing file
is kept as `<file>.orig`.

The tool prints a table of what it did and exits non-zero if anything was
skipped.

## Verifying the tool

```
python tools/portal_patch.py --selftest
```

runs the diff engine through a set of synthetic round trips, parses every
shipped diff, and, when the maintainer's development tree is present, applies
each diff to the original and checks the result byte for byte. Users do not
need that tree; the first two stages run anywhere.
