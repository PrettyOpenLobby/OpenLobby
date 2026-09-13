#!/usr/bin/env python3
"""Built-in portal pages, synthesized when no file exists under www/.

The release ships no portal pages: they are Square Enix's. A server run without
a portal capture therefore answered every PML request with an empty document,
so the Viewer logged in and showed a blank main menu with no way to reach a
title's Play button. This module fills that gap with a minimal menu of our own:

    pml/main/index.pml               the main menu: one row per offered title,
                                     Friend List, optional GM Call, Log Out
    pml/game/<code>/index.pml        a title page: Play, Content ID, Back
    pml/game/<code>/contentid.pml    the Content ID sub-page
    pml/main/in01.pml                the menu DATA fragment a stock main page
                                     includes (see `main_in01`)
    pml/main/ad01.pml                an empty banner list
    pcd/ntool/<lang>/latestnews.pml  an empty news ticker (newsgen's shape)
    pcd/mainmenu/<lang>/data.pml     a comment-only override file
    <anything else>/index.pml and
    pml/game/<code>/*.pml            a "page not available" page with a Back
    any other .pml                   an empty fragment (a comment)

It is a FALLBACK: the http door and the lobby band both look for a real file
first and call `fallback_page` only on a miss, so a page you drop under www/
overrides the built-in one file by file. POL_PML_FALLBACK=0 turns it off.

Titles: `<code>` is the content id. Ids 1..3 live where the stock portal keeps
them (`ff11`, `tetra`, `jan`); every other id is its four-digit form (`0004`,
`0011`), which is also how the Viewer names them ("Contents0011"). Both
spellings are accepted on the way in, so a link from a real menu page reaches
the built-in title page when the title page itself is missing.

Which titles are listed comes from POL_LOBBY_CONTENT_IDS, the same list the
login reply offers when an account grants nothing more specific. A portal page
has no session, so per-account grants are not visible here; they are enforced
where they matter, at launch, by the lobby. Content id 14 (the Friend List) is
not a game and opens the Viewer's own navigator instead of a page.

PML facts this relies on (from the authoring notes and from pages proven to
render in the real client):

  * A page is `<pml><head>..</head><body>..</body></pml>`; `<title>`, a
    `<meta http-equiv="Content-Type">`, `<config>`, `<systembg>` and `<style>`
    definitions sit in the head.
  * Every element is absolutely positioned on a 640x480 stage. `<sheet pos
    size>` is the container; `<text pos size style valign>` draws a label; a
    `<text size bgcolor>` with no content is a coloured plate.
  * Only `<img>` (and `<timer>`) carry an `href`. A clickable row is a
    label `<text>` (with `bgcolor` as its plate and `margin` for the inset,
    both used by the stock main menu) and a transparent client-local image,
    `file:/img_s/general/im01s.png`, laid over it as the hitbox, which is how
    the title pages we author build their menu rows. `alt="^03..."` is the
    help line shown on hover.
  * Schemes: `gameto:<id>` launches a title, `toviewer:` returns to the Viewer
    main menu from a title page (a bare path does NOT on the PC client),
    `navigatorto:` opens the Friend List, `gmcallto:` the GM Call dialog,
    `tologout:` logs out. A plain `/pml/...` path opens another page.
  * `onkeyup`/`onkeydown="sd:focus@<name>"` chain controller focus between
    rows; `onkeycancel` is the controller Back; a `<timer enable="1" delay=N
    href="sd:focus@<name>">` gives the first row focus after the page settles.
  * Comments: `<!-- -->` and the short `<! >` form. An UNBALANCED QUOTE inside
    either kills the parse from that line on, silently; the short form ends at
    the first `>`. The generated pages carry no comments at all.
  * Entities: only a handful are known to the client and an unknown one
    errors the whole page, so the generated text carries NO `&` at all (the
    escaper drops it), and nothing outside ASCII.
  * System variables differ between the PC and PS2 dialects (`$_USER_LANG`
    exists only on the PC; an unresolved name renders as a literal error
    string, and `<if>` gates display, not evaluation). Nothing generated here
    reads a system variable.
  * Bytes: UTF-8 without BOM, CRLF line ends, served as
    `text/x-playonline-pml` with no charset, which is how the pages we author
    are stored and served. ASCII-only output makes the client's charset sniff
    moot.
"""
import os
import re

try:
    import contentlist
except ImportError:                     # pragma: no cover - always shipped
    contentlist = None
try:
    import newsgen
except ImportError:                     # pragma: no cover - always shipped
    newsgen = None
try:
    from srvcore import RELEASE_DEFAULTS   # the release's own env defaults
except ImportError:                     # pragma: no cover - always shipped
    RELEASE_DEFAULTS = {}

#: Where the stock portal keeps the first three titles' pages. Everything else
#: is `%04d`, the Viewer's own "Contents%04d" naming.
GAME_DIRS = {1: "ff11", 2: "tetra", 3: "jan"}
_DIR_TO_ID = {v: k for k, v in GAME_DIRS.items()}

#: Content ids that are services, not launchable titles.
FRIEND_LIST_ID = 14

#: Fragment file names: a request for one of these gets a fragment (data or an
#: empty comment), never a whole page, because the requester is an `<include>`
#: inside another page and a whole page inlined there would break it.
_FRAGMENT_NAMES = {"in01.pml", "ad01.pml", "style.pml", "data.pml",
                   "latestnews.pml", "cont1.pml"}

HITBOX = "file:/img_s/general/im01s.png"
STAGE_W, STAGE_H = 640, 480


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def offered_ids(content_ids=None):
    """The titles to list: an explicit iterable, else POL_LOBBY_CONTENT_IDS."""
    if content_ids is None:
        raw = os.environ.get("POL_LOBBY_CONTENT_IDS",
                             RELEASE_DEFAULTS.get("POL_LOBBY_CONTENT_IDS", "1,2"))
        content_ids = [x for x in raw.replace(";", ",").split(",")]
    out = []
    for x in content_ids:
        try:
            v = int(str(x).strip())
        except ValueError:
            continue
        if 1 <= v <= 1023 and v not in out:
            out.append(v)
    return out


def gm_call_enabled(gm_call=None):
    """POL_GM_CALL=1 lists a GM Call row. Off by default: the GM service is
    optional in the release and a button that fails is worse than none."""
    if gm_call is None:
        gm_call = os.environ.get("POL_GM_CALL", "0")
    return str(gm_call).strip().lower() in ("1", "true", "yes", "on")


def game_dir(cid):
    return GAME_DIRS.get(cid, "%04d" % cid)


def dir_to_id(name):
    """The content id a `pml/game/<name>/` directory stands for, or None."""
    if name in _DIR_TO_ID:
        return _DIR_TO_ID[name]
    if re.fullmatch(r"\d{4}", name):
        v = int(name)
        return v if 1 <= v <= 1023 else None
    return None


def title_of(cid):
    if contentlist is not None:
        return esc(contentlist.content_title(cid))
    return "Content %d" % cid


def esc(s):
    """Text safe for a generated page: ASCII only, no markup characters, no
    `&` (see the entity note in the module docstring), no quotes (an attribute
    is double-quoted and a comment would be broken by an odd quote)."""
    s = str(s)
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = re.sub(r'[<>&"\']', "", s)
    return " ".join(s.split())


# --------------------------------------------------------------------------- #
# page pieces
# --------------------------------------------------------------------------- #
_STYLES = (
    '\t<style name="fbHd" face="6" size="22" proportional="1" spacing="1" '
    'color="#ffffffff,#000000ff">\r\n'
    '\t<style name="fbTt" face="6" size="18" proportional="1" spacing="1" '
    'color="#ffffffff,#000000ff">\r\n'
    '\t<style name="fbSb" face="6" size="13" proportional="1" '
    'color="#b8c0ccff,#000000ff">\r\n'
    '\t<style name="fbMn" face="6" size="15" proportional="1" '
    'color="#f0f0f0ff,#000000ff">\r\n'
    '\t<style name="fbTx" face="6" size="15" proportional="1" vspacing="4" '
    'color="#e6e6e6ff,#000000ff">\r\n'
)


def _head(title):
    return ('<pml>\r\n<head>\r\n'
            '\t<meta http-equiv="Content-Type" '
            'content="text/x-playonline-pml;charset=UTF-8">\r\n'
            '\t<title>%s</title>\r\n'
            '\t<config top="" addbookmark="0" screensaverlock="1" '
            'loadinterval="0">\r\n'
            '\t<systembg src="">\r\n'
            '%s</head>\r\n' % (esc(title), _STYLES))


def _heading(main, sub, style="fbHd"):
    """The dark stage fill, then a heading and a sub-line. `fbTt` (18px) is
    for a title page, whose heading is a title name that can run long; the
    600px box holds the longest shipped name at that size with room left."""
    return ('<sheet name="shBg" pos="0,0" size="%d,%d" border="0" alpha="1" '
            'delay="0" appeartime="200" wait="2">\r\n'
            '\t<text size="%d,%d" bgcolor="#101418ff"></text>\r\n'
            '</sheet>\r\n'
            '<sheet name="shHd" pos="20,34" size="600,60" border="0" alpha="1" '
            'delay="100" appeartime="200" wait="2">\r\n'
            '\t<text pos="0,0" size="600,30" style="%s" valign="middle">'
            '%s</text>\r\n'
            '\t<text pos="0,34" size="600,18" style="fbSb" valign="middle">'
            '%s</text>\r\n'
            '</sheet>\r\n'
            % (STAGE_W, STAGE_H, STAGE_W, STAGE_H, style, esc(main), esc(sub)))


def _rows(rows, cancel, y0=120, step=30):
    """One sheet per row: a label on its own coloured plate (`bgcolor` and
    `margin` on a `<text>`, as the stock main menu draws its rows), then the
    transparent hitbox over it. `rows` is a list of (label, href, help);
    controller focus chains top to bottom and wraps."""
    out = []
    n = len(rows)
    for i, (label, href, help_) in enumerate(rows):
        prev_, next_ = "bt%d" % ((i - 1) % n), "bt%d" % ((i + 1) % n)
        out.append(
            '<sheet name="sh%d" pos="60,%d" size="520,28" border="0" alpha="1" '
            'delay="%d" appeartime="200" wait="2">\r\n'
            '\t<text pos="0,0" size="520,28" style="fbMn" valign="middle" '
            'margin="12" bgcolor="#2a3340cc">%s</text>\r\n'
            '\t<img name="bt%d" pos="0,0" size="520,28" src="%s" href="%s" '
            'mousesound="1" clicksound="3" onkeyup="sd:focus@%s" '
            'onkeydown="sd:focus@%s" onkeycancel="%s" alt="^03%s">\r\n'
            '</sheet>\r\n'
            % (i, y0 + i * step, 200 + i * 60, esc(label), i, HITBOX, href,
               prev_, next_, cancel, esc(help_)))
    return "".join(out)


def _tail(first="bt0"):
    return ('<timer enable="1" delay="900" href="sd:focus@%s">\r\n'
            '</body>\r\n</pml>\r\n' % first)


# --------------------------------------------------------------------------- #
# whole pages
# --------------------------------------------------------------------------- #
def main_index(content_ids=None, gm_call=None):
    """pml/main/index.pml: the built-in main menu."""
    ids = offered_ids(content_ids)
    rows = []
    for cid in ids:
        if cid == FRIEND_LIST_ID:
            rows.append((title_of(cid), "navigatorto:",
                         "Open the Friend List."))
        else:
            rows.append((title_of(cid), "/pml/game/%s/index.pml" % game_dir(cid),
                         "Open the %s page." % title_of(cid)))
    if FRIEND_LIST_ID not in ids:
        rows.append(("Friend List", "navigatorto:", "Open the Friend List."))
    if gm_call_enabled(gm_call):
        rows.append(("GM Call", "gmcallto:",
                     "Apply for, cancel, or begin a chat with a game master."))
    rows.append(("Log Out", "tologout:",
                 "Exit PlayOnline and return to the login screen."))
    return (_head("Main Menu")
            + '<body altbgcolor="#00000000">\r\n'
            + _heading("PlayOnline",
                       "Built-in menu. Choose a title to open its page.")
            + _rows(rows, "tologout:")
            + _tail())


def game_index(cid):
    """pml/game/<code>/index.pml: Play, Content ID, Back."""
    t = title_of(cid)
    d = game_dir(cid)
    rows = [("Play", "gameto:%d" % cid, "Play %s." % t),
            ("Content ID", "/pml/game/%s/contentid.pml" % d,
             "Content ID information for this title."),
            ("Back", "toviewer:", "Return to the PlayOnline main menu.")]
    return (_head("%s: Top Page" % t)
            + '<body altbgcolor="#00000000">\r\n'
            + _heading(t, "Content %04d. Built-in page." % cid, "fbTt")
            + _rows(rows, "toviewer:")
            + _tail())


def game_contentid(cid):
    """pml/game/<code>/contentid.pml: what a Content ID is, for this title."""
    t = title_of(cid)
    d = game_dir(cid)
    back = "/pml/game/%s/index.pml" % d
    body = ("Every title on PlayOnline is addressed by a numeric content code. "
            "%s is content %04d. A Content ID for it is issued by this server "
            "when an account is created or when the administrator grants the "
            "title, and the Viewer selects it on the Play screen." % (t, cid))
    return (_head("%s: Content ID" % t)
            + '<body altbgcolor="#00000000">\r\n'
            + _heading("Content ID", t)
            + '<sheet name="shTx" pos="60,120" size="520,150" border="0" '
              'alpha="1" delay="200" appeartime="200" wait="2">\r\n'
              '\t<text size="520,150" style="fbTx">%s</text>\r\n'
              '</sheet>\r\n' % esc(body)
            + _rows([("Back", back, "Return to the %s page." % t)], back, y0=300)
            + _tail())


def unavailable(rel):
    """Any other whole page: say so, and offer a way back."""
    m = re.match(r"pml/game/([^/]+)/", rel)
    cid = dir_to_id(m.group(1)) if m else None
    if cid is not None:
        back, where = "/pml/game/%s/index.pml" % game_dir(cid), title_of(cid)
    else:
        back, where = "toviewer:", "the PlayOnline main menu"
    return (_head("Page not available")
            + '<body altbgcolor="#00000000">\r\n'
            + _heading("Page not available",
                       "This server has no page at " + esc("/" + rel))
            + _rows([("Back", back, "Return to %s." % where)], back)
            + _tail())


# --------------------------------------------------------------------------- #
# fragments
# --------------------------------------------------------------------------- #
def _arr(fields):
    return '\t\t<array>' + ",".join('"%s"' % f for f in fields) + '</array>\r\n'


def main_in01(content_ids=None, gm_call=None):
    """pml/main/in01.pml: the DATA a stock main page reads from its include.

    A portal capture may hold the main page and lack this include (it is a
    separate file). The stock page builds its whole menu from these names, so
    this fragment defines every one of them with our own entries. Field order
    per row: name, help line, URL, submenu index (level 1); name, URL, help
    line, content id, owned flag (level 2). That page opens a level-2 URL
    with `eval:`, i.e. it evaluates the field as an expression, so a page link
    is written as a quoted string literal inside the field. Nothing here reads
    a system variable."""
    ids = [c for c in offered_ids(content_ids) if c != FRIEND_LIST_ID]
    gm = gm_call_enabled(gm_call)
    games = [(title_of(c), "'/pml/game/%s/index.pml'" % game_dir(c),
              "^03Open the %s page." % title_of(c), str(c), "1") for c in ids]
    navi = [("Friend List", "navigatorto:",
             "^03Communicate with friends through the Friend List.", "0", "0")]
    level1 = [("Games", "^03Select a title to play.", "", "0"),
              ("Navigator", "^03Friend List and communication tools.", "", "1")]
    if gm:
        level1.append(("GM Call", "^03Call a game master.", "gmcallto:", ""))
    level1.append(("Log Out", "^03Exit PlayOnline and return to the login "
                   "screen.", "tologout:,sound:13,sound:37", ""))
    lengths = [str(len(games)), str(len(navi))] + ["0"] * (len(level1) - 2)
    longest = max(len(games), len(navi))
    out = ['<array name="$arShortcutBt">\r\n',
           _arr(["masc00i.png", "/pml/main/index.pml", "^03PlayOnline"]),
           '</array>\r\n',
           '<define name="$mnNum1" value="0">\r\n',
           '<array name="$arMenu1">\r\n']
    out += [_arr(r) for r in level1]
    out += [_arr(["#"]), '</array>\r\n',
            '<define name="$mnMax1" value="%d">\r\n' % (len(level1) - 1),
            '<array name="$arMenu2Length">' + ",".join('"%s"' % x for x in lengths)
            + '</array>\r\n',
            '<define name="$txLPG" value="">\r\n',
            '<array name="$arLPG">"","","","",""</array>\r\n',
            '<array name="$arMenu2">\r\n\t<array>\r\n']
    out += [_arr(r) for r in games]
    out += [_arr(["#"]), '\t</array>\r\n\t<array>\r\n']
    out += [_arr(r) for r in navi]
    out += [_arr(["#"]), '\t</array>\r\n</array>\r\n',
            '<define name="$mnNum2Max" value="%d">\r\n' % longest,
            '<array name="$arMenu2Rec">\r\n']
    out += [_arr(["", "", "", "", ""]) for _ in range(longest + 1)]
    out += ['</array>\r\n']
    texts = [("txLogin", "Last login at "), ("txNewsBtTx1", "Latest Info"),
             ("txNewsBtTx2", "Close"), ("txNews", "Latest Info"),
             ("txNoNews", "No information"), ("txInfo", "Information"),
             ("txInfoAh11", "^03View the latest articles."),
             ("txInfoAh12", "^03Return to the default display."),
             ("txInfoAh2", "^03Check all information currently available."),
             ("txInfoAh3", "^03Read the full article."), ("txSc", "Shortcut"),
             ("txGm", "GM Call"),
             ("txGmBtAh", "^03Apply for, cancel, or begin a GM chat."),
             ("txMenu2ClsBt", "Back"),
             ("txMenu2ClsBtAh", "Return to the previous screen.")]
    out += ['<define name="$%s" value="%s">\r\n' % (k, v) for k, v in texts]
    return "".join(out)


def main_ad01():
    """pml/main/ad01.pml: no banners. The stock page stops at the `#` row."""
    return '<array name="$adBanner">\r\n\t<array>"#"</array>\r\n</array>\r\n'


def latestnews():
    """pcd/ntool/<lang>/latestnews.pml: an empty ticker in newsgen's shape."""
    if newsgen is not None:
        return newsgen.feed([]).replace("\r\n", "\n").replace("\n", "\r\n")
    return ('<ARRAY NAME="$LATESTNEWS">\r\n</ARRAY>\r\n'
            '<define name="$LATESTNEWSMAX" value="0">\r\n')


EMPTY_FRAGMENT = "<!-- -->\r\n"


# --------------------------------------------------------------------------- #
# the entry point
# --------------------------------------------------------------------------- #
def _bytes(text):
    return text.encode("ascii", "replace")


def fallback_page(host, path, content_ids=None, gm_call=None):
    """The built-in page for `path` (any host), as bytes, or None when the
    path is not a PML page at all. `host` is accepted for symmetry with the
    servers that call this and for logging; every portal host gets the same
    built-in tree. `content_ids` / `gm_call` override the environment."""
    rel = (path or "").split("?", 1)[0].split("#", 1)[0].lstrip("/")
    rel = rel.replace("\\", "/")
    if not rel.lower().endswith(".pml"):
        return None
    parts = rel.split("/")
    base = parts[-1].lower()

    if rel.lower() == "pml/main/index.pml":
        return _bytes(main_index(content_ids, gm_call))
    if rel.lower() == "pml/main/in01.pml":
        return _bytes(main_in01(content_ids, gm_call))
    if rel.lower() == "pml/main/ad01.pml":
        return _bytes(main_ad01())
    if base == "latestnews.pml":
        return _bytes(latestnews())

    m = re.fullmatch(r"pml/game/([^/]+)/([^/]+\.pml)", rel, re.IGNORECASE)
    if m and base not in _FRAGMENT_NAMES:
        cid = dir_to_id(m.group(1))
        if cid is not None:
            if base == "index.pml":
                return _bytes(game_index(cid))
            if base == "contentid.pml":
                return _bytes(game_contentid(cid))
        return _bytes(unavailable(rel))

    if base == "index.pml" and base not in _FRAGMENT_NAMES:
        return _bytes(unavailable(rel))
    return _bytes(EMPTY_FRAGMENT)


if __name__ == "__main__":                       # print one page, for a look
    import sys
    sys.stdout.write(fallback_page("wh000.pol.com",
                                   sys.argv[1] if len(sys.argv) > 1
                                   else "/pml/main/index.pml").decode("ascii"))
