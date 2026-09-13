#!/usr/bin/env python3
"""List the concrete URLs a PML page will actually request.

`portal_crawl.py` discovers links by pattern-matching quoted strings, and drops
anything still containing a `$` after a naive de-quote. Real SE pages build
almost every asset URL by concatenation:

    <define name="$C_PATH1" value="/pml/game/ff11/">
    ...
    <img src="$C_PATH1+'img/tp01.png'">

so the crawler saw `$C_PATH1img/tp01.png`, threw it away, and mirrored the page
without its artwork -- which is why `www/wh000.pol.com/pml/game/ff11/` holds
index.pml and nothing else. This resolves the page's own `<define>` table first,
then expands the concatenations, so the asset list comes out concrete.

Two uses:

  * audit what is missing from the local mirror, offline --
        python tools/pml_assets.py --www www --missing
  * produce a seeds list for the next live crawl (a real SE session is needed;
    `file:` URLs are excluded because they are served from the client's own
    install, not over HTTP) --
        python tools/pml_assets.py --www www --missing --seeds > seeds.txt
        python tools/portal_crawl.py --user U --secret S --seeds-file seeds.txt
"""
import argparse
import os
import re
import sys

# Client-provided specials. $SC_ID is the shortcut/content id the menu passes in
# the query string; 1 (FFXI) is what a West session sends.
BUILTINS = {"$_PLATFORM": "WIN", "$_USER_LANG": "en-US", "$_PRODUCTID": "1",
            "$SC_ID": "1", "$PF": "WIN"}

DEFINE_RE = re.compile(r'<define\s+name\s*=\s*"(\$\w+)"\s+value\s*=\s*"([^"]*)"',
                       re.I)
# src=, background=, href= are the three attributes that carry a fetchable URL.
ATTR_RE = re.compile(r'\b(?:src|background|href)\s*=\s*"([^"]+)"', re.I)
ASSET_EXT = (".pml", ".png", ".ang", ".jpg", ".jpeg", ".gif")


def defines(text):
    """The page's own $VAR table, with earlier definitions substituted into
    later ones (SE chains them: $C_PATH2 is built from $C_PATH1)."""
    table = dict(BUILTINS)
    for name, value in DEFINE_RE.findall(text):
        lit = expand(value, table)
        if lit is not None:      # a computed/conditional define is not a constant
            table[name] = lit
    return table


def expand(expr, table):
    """Collapse a PML string expression to a literal, or return None.

    `"'/pml/game/'+$dir+'/index.pml'"` -> `/pml/game/<dir>/index.pml`. Anything
    with an unresolved variable, an array index or arithmetic is not a constant
    URL and comes back None.
    """
    out = []
    for part in expr.split("+"):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] == "'":
            out.append(part[1:-1])
        elif part in table:
            out.append(table[part])
        elif re.fullmatch(r"[\w./%:?&=~-]+", part) and "$" not in part:
            out.append(part)          # a bare literal (unquoted attribute value)
        else:
            return None
    return "".join(out)


def normalise(url, base_dir):
    """Absolute server path, or None if this is not an HTTP fetch."""
    url = re.sub(r"^https?://[^/]+", "", url)
    m = re.match(r"^([a-z][a-z0-9]*):(.*)$", url, re.I)
    if m:
        # `file:` is the client's own install; every other scheme (sd:, eval:,
        # null:, gameto:, sound:, reload:, tologout:) is an action, not a fetch.
        return None
    if not url or url.startswith("#"):
        return None
    if not url.startswith("/"):
        url = base_dir.rstrip("/") + "/" + url
    parts = []
    for seg in url.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/" + "/".join(parts)


def read_pml(path):
    """Decode a mirrored PML page.

    32 of SE's pages are **UTF-16LE with no BOM** -- the whole `pml2/help/`
    manual and staff trees, the UCS sign-up components and the usercte age
    pages. Six of them still declare `charset=UTF-8` in their own
    `<meta http-equiv>`; SE's bytes are UTF-16 anyway and the client sniffs
    rather than believing the meta. Decoding them as UTF-8 "succeeds" -- the
    interleaved NULs are valid UTF-8 -- so nothing raises and every `src="..."`
    silently stops matching, which used to hide those pages' artwork from
    `--missing`.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or \
            (len(data) >= 2 and data[0] != 0 and data[1] == 0):
        return data.decode("utf-16le" if data[:2] != b"\xfe\xff" else "utf-16be",
                           "replace").lstrip("﻿")
    return data.decode("utf-8", "replace")


def page_assets(path, url_path):
    """URLs referenced by one PML file, as absolute server paths."""
    text = read_pml(path)
    table = defines(text)
    base_dir = url_path.rsplit("/", 1)[0]
    out = set()
    for raw in ATTR_RE.findall(text):
        lit = expand(raw, table)
        if lit is None:
            continue
        url = normalise(lit, base_dir)
        if url and url.lower().split("?")[0].endswith(ASSET_EXT):
            out.add(url)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--www", default=os.path.join(os.path.dirname(__file__), "..", "www"))
    ap.add_argument("--host", default="wh000.pol.com")
    ap.add_argument("--missing", action="store_true",
                    help="only URLs with no file in the mirror")
    ap.add_argument("--seeds", action="store_true",
                    help="bare URL list (feed to portal_crawl --seeds-file)")
    a = ap.parse_args()

    root = os.path.normpath(os.path.join(a.www, a.host))
    if not os.path.isdir(root):
        print(f"no mirror at {root}", file=sys.stderr)
        return 2

    refs = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(".pml"):
                continue
            p = os.path.join(dirpath, fn)
            url_path = "/" + os.path.relpath(p, root).replace(os.sep, "/")
            for u in page_assets(p, url_path):
                refs.setdefault(u, set()).add(url_path)

    rows = []
    for url in sorted(refs):
        local = os.path.join(root, url.split("?")[0].lstrip("/").replace("/", os.sep))
        have = os.path.isfile(local)
        if a.missing and have:
            continue
        rows.append((url, have, sorted(refs[url])))

    if a.seeds:
        for url, _have, _by in rows:
            print(url)
        return 0

    for url, have, by in rows:
        print(f"{'HAVE' if have else 'MISS'}  {url}")
        print(f"        referenced by {', '.join(by)}")
    miss = sum(1 for _u, h, _b in rows if not h)
    print(f"\n{len(rows)} referenced URLs listed, {miss} missing from the mirror")
    return 0


if __name__ == "__main__":
    sys.exit(main())
