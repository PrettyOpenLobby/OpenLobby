#!/usr/bin/env python3
"""pmlfit -- a layout linter for the PML pages we author.

The registration wizard is hand-positioned PML: every `<text>` and `<input>`
carries an absolute `pos`/`size` inside a fixed 640x480 stage. Nothing checks
those numbers, so the only way a too-long sentence or a field pushed off the
bottom of the content panel ever got noticed was somebody launching the Viewer
and looking at it. This tool does the looking.

## Why it can be accurate rather than a guess

The Viewer ships its OWN advance-width table: `viewer/data/common/ppfont.bin`.
Decoded here (nothing else in the repo reads it):

    offset 0    uint8  n, the number of faces
    offset 1    n bytes, each face's bytes-per-glyph in the bitmap that follows
    offset 0x10 n tables of 224 bytes: the ADVANCE WIDTH, in pixels, of every
                character from 0x20 to 0xFF, for that face
    then        the glyph bitmaps themselves

So `<style face="6">` selects table 6 and the width of a string is the sum of
its characters' entries. The Western install carries seven faces; PML's face 7
aliases the last table.

The one number that is not in the file is the size those widths are quoted AT,
since `<style size=...>` scales them. It is bracketed, not exact, and the
bracket is narrow enough not to matter:

  * LOWER bound ~15.2. SE's own `Last Updated '03.02.10` sits in a 160px box at
    size 12 (`work/pmlus-decoded`) and must fit; below ~15.2 the model says it
    would not.
  * UPPER bound ~15.87, from a LIVE render. Our step 6 carried
    `* You will be asked for your PlayOnline password each time you log in.` in
    a 600px box at size 15, and the client visibly broke it -- so its real width
    is over 600, which it only is if the em is under 15.87.

`EM = 15.5` sits in the middle of that and is deliberately on the pessimistic
side: erring low predicts text WIDER than it is, so the tool over-warns rather
than passing a line that will break. Override with --em.

## What it reports

    CLIP      the string does not fit its box on one line and the box is too
              short for the wrap. With valign="middle" -- which every one of our
              <text>s uses -- the two wrapped lines are then CENTRED in a
              one-line box, so the reader gets the bottom half of the first line
              and the top half of the second: it reads as text "cut off at both
              ends", not as a truncated tail. This is the one that actually
              loses content, and it is what step 3, 5 and 6 were each doing.
    WRAP      needs a second line, and has room for it (probably still unintended)
    TIGHT     over FILL_BUDGET of the box on one line -- one word, or one longer
              translation, from becoming a CLIP
    OUTSIDE   the element sticks out of the scrollarea/sheet that contains it
    OVERLAP   two pieces of content are drawn on top of each other

Usage:
    python tools/pmlfit.py page.pml [more.pml ...]
    python tools/pmlfit.py --ucs             # lint every ucscgi page
    python tools/pmlfit.py --ucs --verbose   # ...and list what fits, too
    python tools/pmlfit.py --measure "some copy" --width 592 --size 15

Exit status is 1 if anything CLIP/OUTSIDE/OVERLAP was found, so it can gate a
commit; WRAP and TIGHT are advisory.
"""
import argparse
import base64
import math
import os
import re
import sys
import zlib

#: The seven advance-width tables out of the Western install's ppfont.bin
#: (`viewer/data/common/ppfont.bin`, 80432 bytes, faces 28/64/54/40/44/55/60
#: bytes per glyph). 7 x 224 bytes, zlib'd -- embedded so the tool runs without
#: a client install on the box. Regenerate with:
#:
#:   d = open("ppfont.bin","rb").read(); n = d[0]
#:   base64.b64encode(zlib.compress(d[0x10:0x10+n*0xE0], 9))
_PPFONT_B64 = (
    "eNqFlNuSGyEMRJ8AqXWByf9/bFrAeDdJpRaX8cAYoUO3kK6my5uqW0dT+7TW2OkMS1dN6wqZPjEB"
    "hS9zExfVwQe3AetD+wQHQ8QsYZAmjLwaNBgoMGLZWo1bKRZCOYZL55JojK3m4GoTEabC3iICeRsX"
    "6WALv61SrK13O7P1/ibv748IkZ6QIXBU6oLbVCMDD2N4BWh8n56kZYtfEUUHO38fQOva5nnmaPMN"
    "BD/8xy9Ge2w8nEQlqj41lWOu78zCOziGa0JMVQy6g5vdXHL3nS3s0MVOH3+/FxK86Ws9fucTk3/4"
    "VviHz6aXmkX8sD98zMHYkw/ks3r+gY/fH/jclHlVsOOn73zYfPAvCrbYPV+POphNRgPUI3vmnDSV"
    "ek29ERlaPR0rg5ieUyzjyYyZFpkP53Bo6Cpo2dLHKsOg8ptcTz57HB1YWnx9FZ+UOvaQh2Puydfl"
    "z+ILTRcved/T31uwreMStmNMO5R28U7qUnwHT6/ygn1OK44W7IIR6Ht+uaqcmOUFhmsEWcfnRz8v"
    "IaDnLIwVbO2xTczRrLOnck+QgdVWfPInH65+3CZ61ad6dfWXOlwtNr98efRp5c/3/PfPFTDlvh8X"
    "+Fq3+CR0Xv0oro+ySKlIvtj60Zy8AjovgmcWuHnmisSuECmL0uPeu/WH6yjfuV/k6ieOZ+s31hGh"
    "tn8kpdON1I+FYT/pF/r689XPjn74n354+foITC9yVgE3+9RfxfJ7v+DUX/5Rf4OXZ7mPwYbd+8U+"
    "9wvnKQ3rr/zJnejPXX91fwrIV/7kfPnTu5KXWSa6jUE122BC+Ha/fPQ7xB+A0w5f+9LvLUz9DYSU"
    "Pu4="
)

#: The size the tables are quoted at -- see the module docstring.
EM = 15.5

#: The share of a box a single line may fill. Over this it is one edit away
#: from wrapping, and a wrapped line in a one-line box loses BOTH halves.
FILL_BUDGET = 0.85

#: PML `face` -> table index. The install has seven tables (0..6); face 7 is the
#: companion of face 6 in SE's stylesheets and falls on the last table.
_FACE_ALIAS = {7: 6}

#: A line box is this many times the style size tall. Measured off SE's own
#: pages, where a `size="15"` paragraph in a <textbox> steps 18px per line.
LINE_FACTOR = 1.2


def _read_pml(path):
    """Decode a PML page, UTF-16LE included.

    32 of SE's mirrored pages are UTF-16LE with no BOM (the whole `pml2/help/`
    manual and staff trees, the UCS sign-up components, the usercte age pages);
    six of them still declare `charset=UTF-8` in their own `<meta http-equiv>`.
    Reading one as UTF-8 does not raise -- the interleaved NULs are valid UTF-8
    -- it just puts a NUL between every character, which doubles every measured
    width and silently makes the whole report nonsense.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or \
            (len(data) >= 2 and data[0] != 0 and data[1] == 0):
        enc = "utf-16be" if data[:2] == b"\xfe\xff" else "utf-16le"
        return data.decode(enc, "replace").lstrip("﻿")
    return data.decode("utf-8", "replace")


def load_tables(path=None):
    """The seven advance tables, as dicts char -> pixels at EM."""
    if path:
        d = open(path, "rb").read()
        n = d[0]
        raw = d[0x10:0x10 + n * 0xE0]
    else:
        raw = zlib.decompress(base64.b64decode(_PPFONT_B64))
        n = len(raw) // 0xE0
    return [{chr(0x20 + i): raw[k * 0xE0 + i] for i in range(0xE0)}
            for k in range(n)]


TABLES = load_tables()


def advance(text, face=6, size=15, spacing=0, em=EM):
    """Width in pixels of `text` drawn in that face at that size."""
    face = _FACE_ALIAS.get(int(face), int(face))
    tab = TABLES[face if 0 <= face < len(TABLES) else 6]
    # Anything outside the table (CJK, control) is a full-width cell; the pages
    # we lint are English, so this only ever guards against surprises.
    total = sum(tab.get(ch, 2 * em) + spacing for ch in text)
    return total * size / float(em)


# --------------------------------------------------------------------------- #
# a lenient PML reader
# --------------------------------------------------------------------------- #
# PML is XML-ish but not well formed: <input>/<img>/<style>/<meta>/<textbox> are
# unclosed, hrefs carry bare `&`, and `&name=value;` is its own entity syntax.
# Same reasoning as services/admin_web/pml.js, which renders these pages for the
# admin preview -- keep the two in step if either grows a tag.

_VOID = {"input", "img", "meta", "formaction", "define", "style", "br",
         "bgsound", "include", "timer", "textbox", "area", "addmenu",
         "addlink", "config", "plugin", "hidden", "systembg", "inlineimg",
         "multilink", "title"}

_TOK = re.compile(r"</?[\w:-]+[^>]*?>|[^<]+", re.S)
_ATTR = re.compile(r'([\w:-]+)\s*(?:=\s*"([^"]*)"|=\s*\'([^\']*)\'|=(\S+))?')


class Node:
    __slots__ = ("tag", "attrs", "children", "text", "line")

    def __init__(self, tag, attrs=None, line=0):
        self.tag, self.attrs, self.children, self.text, self.line = (
            tag, attrs or {}, [], "", line)


def parse(src):
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"<![^>]*>", "", src)
    root = Node("#root")
    stack = [root]
    for m in _TOK.finditer(src):
        tok = m.group(0)
        line = src.count("\n", 0, m.start()) + 1
        if tok[0] != "<":
            if tok.strip():
                stack[-1].text += tok
            continue
        if tok.startswith("</"):
            name = tok[2:].split(">")[0].strip().lower()
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == name:
                    del stack[i:]
                    break
            continue
        name = re.split(r"[\s/>]", tok[1:], 1)[0].lower()
        body = tok[1 + len(name):].rstrip(">").rstrip("/")
        attrs = {}
        for a in _ATTR.finditer(body):
            v = a.group(2) if a.group(2) is not None else (
                a.group(3) if a.group(3) is not None else (a.group(4) or ""))
            attrs[a.group(1).lower()] = v
        node = Node(name, attrs, line)
        stack[-1].children.append(node)
        if not (tok.endswith("/>") or name in _VOID):
            stack.append(node)
    return root


def _entities(s):
    s = re.sub(r"&(pre|pos|style|var)(=[^;]*)?;", "", s)
    return (s.replace("&quot;", '"').replace("&trade;", "(tm)')")
             .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&nbsp;", " "))


def _xy(v, default=(0, 0)):
    p = str(v or "").split(",")
    try:
        return float(p[0]), float(p[1])
    except (IndexError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #

class Finding:
    def __init__(self, kind, line, where, detail):
        self.kind, self.line, self.where, self.detail = kind, line, where, detail

    #: CLIP/OUTSIDE/OVERLAP lose or garble content; WRAP/TIGHT are advisory.
    HARD = {"CLIP", "OUTSIDE", "OVERLAP"}

    def __str__(self):
        return "  %-7s line %-4d %-28s %s" % (
            self.kind, self.line, self.where, self.detail)


def collect_styles(root):
    styles = {}

    def walk(n):
        if n.tag == "style" and "name" in n.attrs:
            styles[n.attrs["name"]] = n.attrs
        for c in n.children:
            walk(c)
    walk(root)
    return styles


def _style_of(styles, name):
    s = styles.get(name or "", {})

    def num(key, default):
        try:
            return float(s.get(key, default) or default)
        except ValueError:
            return default
    return {
        "face": int(num("face", 6)),
        "size": num("size", 13),
        "spacing": num("spacing", 0),
        "vspacing": num("vspacing", 0),
        "proportional": s.get("proportional", "1") != "0",
    }


def check(src, name="<page>", tight=FILL_BUDGET, em=EM):
    """Lint one page. Returns a list of Findings."""
    root = parse(src)
    styles = collect_styles(root)
    out = []
    #: (rect, label, line) of everything drawn, per container, for OVERLAP.
    boxes = []

    def walk(node, ox, oy, clip, clip_name):
        for n in node.children:
            a = n.attrs
            px, py = _xy(a.get("pos"))
            w, h = _xy(a.get("size"))
            ax, ay = ox + px, oy + py
            rect = (ax, ay, ax + w, ay + h)

            if n.tag in ("scrollarea", "sheet", "systembg"):
                if w and h and clip:
                    _outside(out, rect, clip, clip_name, n, a.get("name", n.tag))
                walk(n, ax, ay, rect if (w and h) else clip,
                     a.get("name", n.tag) if (w and h) else clip_name)
                continue

            if n.tag in ("text", "textbox", "input", "img"):
                if w and h and clip:
                    _outside(out, rect, clip, clip_name, n,
                             a.get("name") or _label(n))
                if n.tag == "text":
                    _measure(out, n, w, h, styles, em, tight)
                if n.tag in ("text", "input", "textbox"):
                    boxes.append((rect, _label(n), n.line, clip_name))
            walk(n, ax, ay, clip, clip_name)

    body = _find(root, "body") or root
    walk(body, 0, 0, (0, 0, 640, 480), "stage")
    _overlaps(out, boxes)
    return out


def _label(n):
    if n.tag == "text":
        t = _entities(n.text).strip()
        return '"%s"' % (t[:24] + ("..." if len(t) > 24 else ""))
    return "<%s %s>" % (n.tag, n.attrs.get("name", ""))


def _find(node, tag):
    for c in node.children:
        if c.tag == tag:
            return c
        got = _find(c, tag)
        if got:
            return got
    return None


def _outside(out, rect, clip, clip_name, n, label):
    x0, y0, x1, y1 = rect
    c0, d0, c1, d1 = clip
    over = []
    if x1 > c1 + 0.5:
        over.append("right by %d" % round(x1 - c1))
    if y1 > d1 + 0.5:
        over.append("bottom by %d" % round(y1 - d1))
    if x0 < c0 - 0.5:
        over.append("left by %d" % round(c0 - x0))
    if y0 < d0 - 0.5:
        over.append("top by %d" % round(d0 - y0))
    if over:
        out.append(Finding("OUTSIDE", n.line, label,
                           "leaves %s (%dx%d at %d,%d) %s" % (
                               clip_name, c1 - c0, d1 - d0, c0, d0,
                               " and ".join(over))))


def _measure(out, n, w, h, styles, em, tight):
    text = _entities(n.text).strip()
    if not text or not w:
        return
    st = _style_of(styles, n.attrs.get("style"))
    adv = advance(text, st["face"], st["size"], st["spacing"], em)
    lines = max(1, int(math.ceil(adv / w - 1e-6)))
    line_h = st["size"] * LINE_FACTOR + st["vspacing"]
    if lines > 1 and h and lines * line_h > h + 0.5:
        out.append(Finding(
            "CLIP", n.line, _label(n),
            "needs %d lines (%dpx of copy in a %dpx box) but is only %dpx tall"
            % (lines, round(adv), round(w), round(h))))
    elif lines > 1:
        out.append(Finding("WRAP", n.line, _label(n),
                           "wraps to %d lines (%dpx in %dpx)"
                           % (lines, round(adv), round(w))))
    elif adv > w * tight:
        out.append(Finding("TIGHT", n.line, _label(n),
                           "%d%% of the box (%dpx in %dpx)"
                           % (round(100 * adv / w), round(adv), round(w))))


def _overlaps(out, boxes):
    for i in range(len(boxes)):
        (a, la, lna, ca) = boxes[i]
        for j in range(i + 1, len(boxes)):
            (b, lb, lnb, cb) = boxes[j]
            if ca != cb:
                continue
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > 1 and oy > 1:
                out.append(Finding("OVERLAP", lnb, lb,
                                   "sits on %s (line %d) by %dx%d px"
                                   % (la, lna, round(ox), round(oy))))


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #

def _ucs_pages():
    """Every page ucscgi can serve, built with plausible state."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(here), "services"))
    os.environ.setdefault("POL_LOG_DIR", os.environ.get("TEMP", "."))
    import ucscgi as u

    tok = "0" * 32
    st = u._SIGNUP.setdefault(tok, (0, {}))[1]
    st.update({"handle": "Testhandle", "code": "PLAY-NINE-REVA-GAME-FFXI",
               "contents": [1, 2]})
    sess = {"polid": "US000012345678", "member_id": 1, "login": "test"}
    ret = "tologin:"
    long_err = ("That registration code has already been used.\n"
                "Check the code and try again.")
    return [
        ("step1", u.page_step1(tok)),
        ("step2", u.page_step2(tok)),
        ("step3", u.page_step3(tok)),
        ("step3+err", u.page_step3(tok, "That code was not recognised.")),
        ("step4", u.page_step4(tok)),
        ("step4+err", u.page_step4(tok, "Those passwords do not match.")),
        ("step5", u.page_step5(tok)),
        ("step6", u.page_step6(tok, "US000012345678", "Testhandle", [1, 2])),
        # The worst case for the Content IDs row: a code granting four titles
        # prints ~570px of names into a 374px column, so the row is built two
        # lines tall. Lint it, or that only shows up on somebody's live account.
        ("step6:4titles", u.page_step6(tok, "US000012345678", "Testhandle",
                                       [1, 2, 4, 11])),
        # can_rdt False drops the auto-add button and swaps one footnote, so it
        # is a different button row AND a different bottom stack -- lint both.
        ("step6:nordt", u.page_step6(tok, "US000012345678", "Testhandle",
                                     [1, 2], can_rdt=False)),
        ("step8", u.page_step8("US000012345678")),
        ("login", u.page_login(25, tok, ret, "Content ID Confirmation")),
        ("login+err", u.page_login(25, tok, ret, "Content ID Confirmation",
                                   u.ERR_5000)),
        ("contents", u.page_contents(tok, ret, sess, [1, 2, 3, 4, 11, 14])),
        ("contents:none", u.page_contents(tok, ret, sess, [])),
        ("code", u.page_code(tok, ret, sess)),
        ("code+err", u.page_code(tok, ret, sess, long_err)),
        ("pwchange", u.page_pwchange(tok, ret, sess)),
        ("pwchange+err", u.page_pwchange(tok, ret, sess, long_err)),
        ("pwchange_done", u.page_pwchange_done(ret)),
        ("code_done", u.page_code_done(ret, [1, 2])),
        ("code_done:4", u.page_code_done(ret, [1, 2, 4, 11])),
        ("unavailable", u.page_kinou_unavailable(12, ret, "Content ID purchase")),
        ("message", u.page_message("Session expired",
                                   "Your registration session has expired.")),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", help="PML files to lint")
    ap.add_argument("--ucs", action="store_true",
                    help="lint every page services/ucscgi.py can serve")
    ap.add_argument("--font", help="a ppfont.bin to measure with, instead of "
                                   "the embedded tables")
    ap.add_argument("--em", type=float, default=EM,
                    help="size the advance tables are quoted at (default 16)")
    ap.add_argument("--tight", type=float, default=FILL_BUDGET,
                    help="fill ratio earning a TIGHT warning (default %.2f, "
                         "the authoring budget)" % FILL_BUDGET)
    ap.add_argument("--measure", help="just measure this string and exit")
    ap.add_argument("--width", type=float, default=592.0)
    ap.add_argument("--size", type=float, default=15.0)
    ap.add_argument("--face", type=int, default=6)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also name the pages that are clean")
    args = ap.parse_args(argv)

    if args.font:
        global TABLES
        TABLES = load_tables(args.font)

    if args.measure is not None:
        adv = advance(args.measure, args.face, args.size, em=args.em)
        print("%.0fpx in a %.0fpx box = %d%% (%s)"
              % (adv, args.width, round(100 * adv / args.width),
                 "fits" if adv <= args.width else "WRAPS"))
        return 0

    pages = []
    if args.ucs:
        pages += _ucs_pages()
    for f in args.files:
        pages.append((os.path.basename(f), _read_pml(f)))
    if not pages:
        ap.error("give some files, or --ucs")

    hard = soft = 0
    for name, src in pages:
        found = check(src, name, tight=args.tight, em=args.em)
        h = [f for f in found if f.kind in Finding.HARD]
        hard += len(h)
        soft += len(found) - len(h)
        if found:
            print("%s:" % name)
            for f in sorted(found, key=lambda f: (f.kind not in Finding.HARD,
                                                  f.line)):
                print(f)
        elif args.verbose:
            print("%s: clean" % name)
    print("\n%d page(s): %d clip/outside/overlap, %d wrap/tight"
          % (len(pages), hard, soft))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
