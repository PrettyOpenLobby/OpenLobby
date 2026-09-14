#!/usr/bin/env python3
"""PlayOnline UCS CGI -- serves the in-client account registration page.

This is the server half of the sign-up flow. The Viewer's first-run wizard has a
native intro screen (`Startup_SignUpPlayOnline_Win`, which renders offline) and
then hands off to a HOSTED page for the actual form. app.dll 0x049ff3a1 builds
that URL entirely from env.dat keys:

    "https:" + POL_UCS_URL + POL_UCS_CGI
      + "?kinou_id=20&ret_url=tologin%3A&"
      + "area_kbn=" POL_UCS_AREA_KBN "&login_pf=" POL_UCS_LOGIN_PF
      + "&property=" POL_UCS_PROPERTY
      + "&isp_kbn=" (01 if the connection profile is "OCN" else 00)

With the shipped env.dat (POL_UCS_URL=//ucs.pol.com/pml-cgi-bin/, the other keys
absent) that is:

    https://ucs.pol.com/pml-cgi-bin/?kinou_id=20&ret_url=tologin%3A&area_kbn=&login_pf=&property=&isp_kbn=00

`kinou_id` (機能ID, "function id") selects the operation; 20 is registration. The
2002 JP PS2 build aimed the same builder at
`https://userctl.pol.com/pml-cgi-bin/UMENZ001.cgi`, so POL_UCS_CGI is
`UMENZ001.cgi` -- we accept any path under /pml-cgi-bin/ and dispatch on kinou_id.

MEASURED (2026-08-10, netstat against a live wizard): the client dials
**ucs.pol.com:51305**, a BAND port, not :443 -- the same way the portal is served.
So this listens on 51305 by default.

## The SSLv3 problem -- SOLVED, but not in this file

The client's ClientHello is genuine SSL 3.0 (record `16 03 00`, client_version
`03 00`). Modern OpenSSL builds REMOVE SSLv3 entirely, so the in-process listener
here CANNOT complete the handshake -- it answers `[SSL: VERSION_TOO_LOW]`, which
the client reports as POL-1322. Configuration cannot fix it; the protocol is
absent from the library.

Half the fix is the `ssl3ucs` terminator: stunnel linked against an OpenSSL
1.1.1w compiled with `enable-ssl3`. Set POL_UCS_SSL3_HOST/_PORT and `serve`
relays the TLS half of the port to it instead of wrapping the socket itself;
stunnel decrypts and speaks plain HTTP to the `--plain` twin. See
services/stunnel-ucs.conf for the topology and for why the first-byte sniff has
to stay HERE rather than letting stunnel own 51305.

WARNING: **HTTPS SIGN-UP DOES NOT WORK, and this docstring used to claim it did.** The
retracted line read "Verified end to end -- SSLv3 ciphersuite: DES-CBC3-SHA,
which is cipher 0x000a off the client's own list", along with a cipher list
beginning 000a. Both are wrong, and the real cipher list -- captured off the
live client BY THIS FILE, which dumps the ClientHello before relaying --
supersedes them:

    0005 RC4_128_SHA  0004 RC4_128_MD5  0009 DES_CBC_SHA
    0003 EXP_RC4_40_MD5                 0008 EXP_DES40_CBC_SHA

**There is no 000a.** So that "verification" was openssl talking to openssl and
never exercised the client's path -- a known trap that has now cost false
confidence twice. What actually happens is POL-1328: the
client reads our Certificate and closes mid-handshake with no alert. The cert
chase is UNRESOLVED after six live cycles, and the
error's two neighbours in SE's table are both certificate-authentication
failures.

The live route is therefore PLAIN HTTP -- a link in a page we serve is followed
as written, which is what the shim's `signup_http` lever exists for. The cost is
that the RDT block (see `rdt_member_block`) is refused on a plain-http source
URL; the way to buy it back is the install's `rdthosts.bin`, NOT this.

The in-process TLS path is kept for the case where a peer can negotiate
something modern, and still logs a refusal by name.

## The form markup is TRANSCRIBED, not inferred

It used to be a guess. It no longer is: SE's own account pages were recovered
from the PS2 HTTP cache (`E:/ps2hdd/urlcache-1815`, `urlcache-us` -- decoded by
work/ps2/poldcf.py), including a live `<form>`/`<formaction>` pair and the
shared includes for this exact flow. See the `forms` section below for the
recipe and the `_ART_STYLES` note for the stylesheet. Two corrections fell out
of that:

  * `<textbox>` is NOT an input. It is a read-only scrolling viewer bound to a
    `<data>` record. Everything typed into the old `<textbox>` fields was
    unreachable by construction -- real entry is `<input type="text">`.
  * URLs in href take a BARE `&`. The escaped `&amp;` this file used to emit
    went out on the wire literally and broke every step transition. See `_u`.

The `type` enum is app.dll's own, read out of the memory image at
0x3dac14-0x3dacf4: checkbox / radio / select / image / password / hidden /
input / textarea, with form `method` one of post / get / polmail.

## The chrome is SE's own account-area art

The registration flow had its own art set, `ucs/img_s/`, which SE SERVED rather
than shipped -- so the client has no local copy and it has to come from us. The
recovered set is in www/ucs.pol.com/ucs/img_s and is served from this process
(see `UCS_ART` and `Handler._serve_static`): bg01s.png is the backdrop,
bt01s/bt02s.ang the footer button plates, lgpl01s.png the PlayOnline mark.

References to art the client DOES ship use `file:/...` -- one slash, no `pml`
segment, resolving against `viewer/data/pmlus`. The `file://pml/...` this file
used to emit resolved `pml` as an authority component and loaded nothing.

    POL_UCS_SKIN=ucs        default -- SE's real registration art, served here
    POL_UCS_SKIN=local      the three-piece kit the client ships; needs no fetch
    POL_UCS_SKIN=ucs-jp     the JP kit; only present on a JP install
    POL_UCS_SKIN=flat       the pre-art chrome, for comparison

Usage:
    python ucscgi.py                 # TLS on the configured port (51305)
    python ucscgi.py --plain         # plain HTTP (behind a TLS terminator)
    python ucscgi.py --port 8443
"""
import argparse
import datetime
import html
import os
import socket
import secrets
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:                                        # pragma: no cover
    yaml = None

try:
    import accounts
    from accounts import CONTENT_NAMES
except ImportError:                                        # pragma: no cover
    accounts = None
    CONTENT_NAMES = {1: 'FinalFantasyXI', 2: 'TetraMaster'}

CONFIG_PATH = os.environ.get("POL_CONFIG", "/config/server.yaml")
LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")

#: Document root for the account-area art we serve (see UCS_ART). Only the
#: `ucs/img_s/` subtree is reachable -- this is not a general web server and the
#: port is exposed to the network.
WWW_DIR = os.environ.get("POL_UCS_WWW", "/www/ucs.pol.com")

#: Extension -> content type for that subtree. `.ang` is SE's own animated
#: sprite container ('@ANG'); the Viewer dispatches on the PML `src`, not on
#: this header, but the portal serves it as an image type so we match.
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ang": "image/x-playonline-ang",
}

#: PML's own content type, straight off the captured page's <meta http-equiv>.
PML_MIME = "text/x-playonline-pml;charset=UTF-8"

#: RDT's content type. The `text/rdt` token is MEASURED, not chosen: app.dll
#: carries a content-type table at 0x4c36a74 whose records pair an extension list
#: with a MIME list, and the record at 0x4c36b18/0x4c36b1c pairs `text/rdt` with
#: the extension `rdt` -- the same shape as PML's own `text/x-playonline-pml` /
#: `pml`. See `rdt_member_block` for what the client does with a document served
#: as this.
#:
#: `;charset=UTF-8` is REQUIRED, and its absence was POL-1281 ("RDT data has a
#: format error"). The client's RDT parser reads the body as 16-bit code units
#: (app.dll 0x4a67046 halves the byte count; 0x4a689fe reads `word ptr`), so the
#: HTTP layer must first decode the body to wide chars using the charset. That
#: charset is taken from the `charset=` parameter (app.dll 0x4a62f5a) -- there is
#: NO per-type default in the content-type table (its trailing column is a flag,
#: 1 or 2, not a charset id, which start at 2) -- so a bare `text/rdt` leaves the
#: connection's init charset in force and our all-ASCII block is misdecoded into
#: garbage field names, which the parser reports as a format error. PML never hit
#: this because it always carried `charset=UTF-8`. The token before `;` is what
#: the handler dispatch matches (proven by PML working with the same suffix), so
#: adding the parameter keeps the RDT handler and fixes the decode.
RDT_MIME = "text/rdt;charset=UTF-8"


class Document(str):
    """A response body plus the content type it has to be served as.

    Every page in this file is PML, with exactly one exception -- the RDT block
    that provisions the member. Rather than give that one reply its own sending
    path (and have to keep two of them in step over the no-cache headers, the
    logging and the error pages), a page may carry its own `mime` and `_send`
    honours it. A plain `str` still means PML, so nothing else changes.
    """

    mime = PML_MIME

    def __new__(cls, text, mime=PML_MIME):
        doc = super().__new__(cls, text)
        doc.mime = mime
        return doc

#: kinou_id values we implement. 20 = registration (the one the wizard uses);
#: 25 and 31 are the account operations SE's own game pages already link to --
#: see the block above `page_login` for where those numbers come from.
KINOU_REGISTER = 20
KINOU_CONTENTS = 25       # 有料コンテンツＩＤの確認 -- list entitlements
KINOU_REGCODE = 31        # 拡張コンテンツの登録 -- redeem a registration code
KINOU_PWCHANGE = 17       # POL パスワードの変更 -- change the login password
KINOU_CANCEL = 14         # コンテンツＩＤの解約 -- cancel a content licence
KINOU_REACTIVATE = 15     # コンテンツＩＤの再開 -- restore a cancelled one
KINOU_MAILADDR = 5        # メールアドレスの変更 -- pick a friendlier mail name
KINOU_MAILPW = 6          # メールパスワードの変更 -- set the POP3/SMTP password
KINOU_MEMBERINFO = 1      # 会員情報の確認 -- review the account
KINOU_MEMBERCHG = 4       # 会員情報の変更 -- what of it can be changed
#: OURS, not SE's: link a Discord account (polbridge.py, 2026-09-13). No SE page
#: links it; it is reached from Member Information's "Discord" button. 90 is far
#: from every number SE's menus use (1-31), so a real SE link can never land here.
KINOU_DISCORD = 90

#: The operations that need an authenticated member, i.e. everything routed
#: through `_account_step`. Kept as one tuple because GET and POST both dispatch
#: on it and they drifted apart once already.
KINOU_ACCOUNT = (KINOU_CONTENTS, KINOU_REGCODE, KINOU_PWCHANGE,
                 KINOU_CANCEL, KINOU_REACTIVATE,
                 KINOU_MAILADDR, KINOU_MAILPW,
                 KINOU_MEMBERINFO, KINOU_MEMBERCHG, KINOU_DISCORD)

#: Screen titles, SE's own wording for these two operations (the labels the
#: Membership menu gave them, which KINOU_UNAVAILABLE used to carry).
TITLE_CANCEL = "Content ID Cancellation"
TITLE_REACTIVATE = "Content ID Reactivation"
TITLE_MAILADDR = "Mail Account Address"
TITLE_MAILPW = "Mail Account Password"
TITLE_MEMBERINFO = "Member Information"
TITLE_MEMBERCHG = "Change Member Information"
TITLE_DISCORD = "Link Discord"

try:
    # The Discord link store (discordlink.py). Optional like `accounts`: absent,
    # kinou 90 says so instead of failing.
    import discordlink
except ImportError:                                        # pragma: no cover
    discordlink = None


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def log(msg):
    line = f"{_stamp()} [ucs] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "ucs.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) if yaml else {}
    except OSError:
        return {}


# --------------------------------------------------------------------------- #
# PML
# --------------------------------------------------------------------------- #
# Conventions copied from the captured page: absolutely-positioned <sheet>
# containers, <style> declarations referenced by name, <text pos size style>,
# and action verbs in href (`tologin:` returns the client to the login screen,
# which is exactly what the wizard passes as ret_url).
#: A 1x1 PNG the client already ships. Used as the clickable surface for links.
#:
#: NOTE THE SINGLE SLASH. `file:/...` is SE's own form, measured off the defines
#: in the recovered pages -- `file:/ucs/img_s/`, `file:/img_s/general/`,
#: `file:/game/ff11/` -- and it resolves against the Viewer's data root, which
#: for a Western install IS `viewer/data/pmlus`. The `file://pml/...` this file
#: used to emit is wrong twice over: the `//` makes `pml` an authority component
#: rather than a path segment, and there is no `pml` directory under the data
#: root to begin with. Every image on every page silently failed to load, which
#: is why the rendered pages were text floating on a bare backdrop.
_HITBOX_SRC = "file:/img_s/general/im01s.png"


def link(name, pos, size, href, label, style="body"):
    """A clickable link.

    MEASURED, not guessed: in SE's captured page `href` appears ONLY on <img>
    and <timer> -- never on <text>. A <text href=...> is inert, which is exactly
    how the first version of this page ended up with no way back out of it. So a
    link is a <text> label with a transparent <img> hitbox over it.
    """
    x, y = pos
    w, h = size
    return (f'\t<text pos="{x},{y}" size="{w},{h}" style="{style}" '
            f'valign="middle">{html.escape(label)}</text>\n'
            f'\t<img name="{name}" pos="{x},{y}" size="{w},{h}" '
            f'src="{_HITBOX_SRC}" href="{href}" clicksound="1">\n')


# --------------------------------------------------------------------------- #
# the layout grid
# --------------------------------------------------------------------------- #
# Every widget on these pages is absolutely positioned, so "it looks wrong" has
# always meant somebody eyeballed a y-coordinate. These constants are the frame
# all of it hangs off, and `tools/pmlfit.py --ucs` checks the result against the
# Viewer's OWN advance-width table (viewer/data/common/ppfont.bin) -- so a line
# that would wrap, or a field that would fall out of the panel, is caught here
# instead of on a client round-trip.
#
# What went wrong before, and what the grid fixes:
#
#   * copy written to fill a 600px box at size 15 needs ~600px, and the client
#     wraps it into a 20px-tall <text> where the second line is lost. Steps 3, 5
#     and 6 each had one. Lines are now budgeted at <=85% of their box.
#   * a screen laid out downward from the top eventually runs past the panel:
#     step 4's "* Required" and step 6's footnote both hung 8px below it, and
#     the Content ID list ran 26px past. Bottom copy is now STACKED UP from the
#     panel floor by `_bottom`, so it cannot.
#   * `scroll=True` put a scrollbar on screens that had nothing to scroll, which
#     both narrows the usable width and adds a controller focus target. Only a
#     screen with a <textbox> needs it, and the textbox does its own scrolling.
#
# The numbers are the art chrome's (`_wizard_art`); the `flat` comparison skin
# has a taller panel, so a page laid out to this grid fits there too.

PANEL_W = 640          # the content panel, in its own coordinates
PANEL_H = 238
PANEL_Y = 144          # ...and where the panel sits on the 640x480 stage
ROW_Y = PANEL_Y + PANEL_H     # 382 -- the button band starts flush under it
BTN_Y = ROW_Y + 10            # 392 -- plates in the top of the band, not centred
FOOT_Y = 440                  # the status strip

X = 24                        # left margin inside the panel
CW = PANEL_W - 2 * X          # 592 -- the widest any line may be
LABEL_W = 210                 # the label column
VAL_X = X + LABEL_W + 8       # 242 -- values and entry fields start here
VAL_W = PANEL_W - X - VAL_X   # 374 -- ...and run to the right margin
FIELD_W = 190                 # an <input> is narrower than its column...
HINT_X = VAL_X + FIELD_W + 14  # 446 -- ...leaving room for a hint beside it
HINT_W = PANEL_W - X - HINT_X  # 170

TOP = 16               # first line of copy
LINE = 24              # consecutive lines of prose
ROW = 34               # a label+field row (fields are 24 tall)
FLOOR = PANEL_H - 14   # 224 -- nothing may be drawn below this

#: A line of copy wider than this fraction of its box is treated as a mistake:
#: it either wraps into a box too short to show the second line, or is one
#: retranslation away from doing so. `tools/pmlfit.py` enforces the same number.
FILL_BUDGET = 0.85


def _line(y, text, style="body", x=X, w=CW, h=22, indent=1):
    """One positioned line of copy.

    Keep it to ONE line: the box is a line tall, and a string that overflows it
    is silently wrapped and clipped by the client. Anything longer than a line
    belongs in `_doc`/`_textbox`, which is what SE does with its long-form copy.
    """
    t = "\t" * (indent + 1)
    return (f'{t}<text pos="{x},{y}" size="{w},{h}" style="{style}" '
            f'valign="middle">{html.escape(text)}</text>\n')


def _row(y, label, value, style="body", indent=1, h=22, w=None):
    """A label in the left column and a value in the right one."""
    return (_line(y, label, "label", X, LABEL_W, h, indent)
            + _line(y, value, style, VAL_X, w or VAL_W, h, indent))


def _field(y, label, name, hint=None, required=True, **kw):
    """A label, an entry field beside it, and an optional hint after that."""
    out = (_line(y, label + (" *" if required else ""), "label",
                 X, LABEL_W, 24)
           + _input(name, (VAL_X, y), (FIELD_W, 24), **kw))
    if hint:
        out += _line(y, hint, "hint", HINT_X, HINT_W, 24)
    return out


def _bottom(*lines, **kw):
    """Footnotes and errors, stacked UP from the floor of the panel.

    Laying these out downward is what pushed "* Required" and step 6's footnote
    out of the panel: the copy above them grew and nothing noticed. Anchored to
    the floor they cannot leave, and a screen with an error simply grows upward
    into the space the fields are not using.

    Each entry is (style, text); None entries are skipped so a caller can pass
    an optional error inline.
    """
    indent = kw.get("indent", 1)
    rows = []
    for entry in lines:
        if not entry or not entry[1]:
            continue
        for part in str(entry[1]).split("\n"):
            if part.strip():
                rows.append((entry[0], part.strip()))
    out = []
    y = FLOOR - 20 - (len(rows) - 1) * 22
    for style, text in rows:
        out.append(_line(y, text, style, X, CW, 20, indent))
        y += 22
    return "".join(out)


# --------------------------------------------------------------------------- #
# chrome skins
# --------------------------------------------------------------------------- #
# `file://pml/...` resolves to the client's own viewer/data/pmlus root -- the
# same prefix SE uses for img_s/general/im01s.png -- so any art the Viewer
# already ships is addressable without this server sending a byte of it.
#
# MEASURED off that shipped art (worker-out/pmlart.py decodes every PML corpus
# into shortcut-icons/pml-art/): the account area has its own three-piece kit
# under `ucs/img_s/`, and it is the only art set in the client scoped to the
# member area rather than to a game or to the system chrome.
#
#   bgi01.png  640x480  the page backdrop -- dark olive, vignetted, scanlined.
#                       Luma runs 30 at the corners to 156 mid-panel, so copy on
#                       it has to be LIGHT. The flat chrome below covered it
#                       with a cream panel and near-black text, which is why the
#                       reconstruction never looked like the real screens.
#   bar01.png  578x56   translucent olive title band, feathered ends, drop
#                       shadow beneath. 578 wide centres at x=31.
#   logo01.png 192x112  the PlayOnline badge, mostly alpha; halves to 96x56.
#
# The JP client ships a second kit (bgi02/bar02/logo02) in blue. Western
# installs have kit 01 only, so `ucs-jp` is selectable only against a JP Viewer.
#
# Band fills carry 8-digit RGBA so the backdrop reads through them. If a build
# ignores the alpha byte the bands come out solid dark instead -- still
# light-on-dark, still legible. That is why the fallback colour is dark and not
# the old cream: the skin degrades into a different look, never an unreadable
# one.

#: Widget captions are drawn on the client's OWN skin="1" pill and inset box,
#: which is light in every skin, so this style stays dark regardless. Splitting
#: it out of `body` is load-bearing -- `body` goes light on the art skins, and
#: without the split every button caption and field entry would vanish.
#:
#: `In15` is SE's own name for the <input> style (see the ucsgate pages); we keep
#: `field` as the alias the step builders already reference and declare both.
_WIDGET_STYLE = ('\t<style name="field" face="6" size="15" proportional="1" '
                 'color="#000000ff">\n'
                 '\t<style name="In15"  face="6" size="15" proportional="1" '
                 'color="#000000ff">\n')

#: Light-on-dark, for the art skins.
#:
#: TRANSCRIBED from SE's own shared stylesheet for this exact flow --
#: `usercte.pol.com/union.pml`, the Western registration host's include, out of
#: the PS2 HTTP cache (E:/ps2hdd/urlcache-us). Three things there that the
#: earlier hand-rolled styles all got wrong:
#:
#:   * `face="6"` is the proportional UI face the account pages use throughout
#:     (`face="7"` is its companion). `face="0"` is a different, blockier face.
#:   * `color` takes a PAIR -- fill then outline, e.g. `#ffffffff,#332211ff`.
#:     A single colour renders flat and thin against the vignetted backdrop;
#:     the outline is what makes light copy hold up over the art.
#:   * `spacing` / `vspacing` are set explicitly; SE runs vspacing="4" on every
#:     body style so wrapped paragraphs breathe.
#:
#: SE's own names are kept as aliases (Tw20, Bw15, ...) so markup lifted straight
#: out of a recovered page renders here without rewriting its style references.
_ART_STYLES = """\t<style name="hdr"   face="6" size="16" proportional="1" spacing="1" color="#ffffffff,#332211ff">
\t<style name="step"  face="6" size="16" proportional="1" spacing="1" color="#ccdd99ff,#332211ff">
\t<style name="title" face="6" size="20" proportional="1" spacing="1" color="#ffffffff,#332211ff">
\t<style name="body"  face="6" size="15" proportional="1" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="label" face="6" size="15" proportional="1" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="hint"  face="6" size="14" proportional="1" vspacing="4" color="#d8c48aff,#000000ff">
\t<style name="note"  face="6" size="14" proportional="1" vspacing="4" color="#ffab86ff,#000000ff">
\t<style name="ok"    face="6" size="15" proportional="1" vspacing="4" color="#9ede9eff,#000000ff">
\t<style name="foot"  face="6" size="14" proportional="1" color="#ffffffff,#000000ff">
\t<style name="btn"   face="6" size="15" proportional="1" color="#ffffffff,#000000ff" onmousecolor="#000000ff,#ffffffff" selectedcolor="#ffffffff,#000000ff" disablecolor="#00000033">
\t<style name="Sc16"  face="6" size="16" proportional="1" spacing="1" color="#ccdd99ff,#332211ff">
\t<style name="Tw20"  face="6" size="20" proportional="1" spacing="1" color="#ffffffff,#332211ff">
\t<style name="Hw18"  face="6" size="18" proportional="1" spacing="2" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="Bw15"  face="6" size="15" proportional="1" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="Bb15"  face="6" size="15" proportional="1" vspacing="4" color="#000000ff">
\t<style name="btn15" face="6" size="15" proportional="1" color="#ffffffff,#000000ff" onmousecolor="#000000ff,#ffffffff" selectedcolor="#ffffffff,#000000ff" disablecolor="#00000033">
"""

#: The previous chrome: palette read off SE's own registration screenshots
#: (steps 3/7, 5/7, 7/7). Dark text on a cream panel. Kept selectable so the two
#: can be compared against a live render rather than argued about.
_FLAT_STYLES = """\t<style name="hdr"   face="0" size="13" proportional="1" bold="1" color="#ffffffff">
\t<style name="step"  face="0" size="13" proportional="1" bold="1" color="#f0c860ff">
\t<style name="title" face="0" size="17" proportional="1" bold="1" color="#ffffffff">
\t<style name="body"  face="0" size="13" proportional="1" color="#1a1712ff">
\t<style name="label" face="0" size="13" proportional="1" bold="1" color="#1a1712ff">
\t<style name="hint"  face="0" size="12" proportional="1" color="#a83a20ff">
\t<style name="note"  face="0" size="12" proportional="1" color="#a02820ff">
\t<style name="ok"    face="0" size="13" proportional="1" bold="1" color="#1a7a3aff">
\t<style name="foot"  face="0" size="12" proportional="1" color="#ffffffff">
\t<style name="btn"   face="0" size="13" proportional="1" bold="1" color="#ffffffff" onmousecolor="#bfe4ffff" selectedcolor="#ffe8a8ff">
"""

#: Where we serve SE's own account-area art from. Root-relative on purpose: it
#: resolves against whatever host and port the page itself came from, so the
#: same markup works on 8080, on 51305 behind the terminator, and under either
#: hostname without threading a base URL through every page.
UCS_ART = "/ucs/img_s/"

SKINS = {
    # SE's REAL registration kit, recovered from the PS2 HTTP cache
    # (E:/ps2hdd/urlcache-1815/ucs.pol.com/ucs/img_s) and now served by us out
    # of www/ucs.pol.com/ucs/img_s. This is the art the live flow actually used
    # -- SE served it rather than shipping it, which is why the client has no
    # local copy and why the earlier `file:` references to it could never have
    # resolved even with the path spelled correctly.
    "ucs": {
        "bg":     UCS_ART + "bg01s.png",     # 640x480, the registration backdrop
        "bar":    None,                      # this kit has no title band...
        "logo":   UCS_ART + "lgpl01s.png",   # ...just the 146x50 PlayOnline mark
        "styles": _ART_STYLES,
        "head":   "#00000000",
        "scrim":  "#14120cb4",   # content panel, backdrop reads through
        "row":    "#0f0d09c8",   # button row, a shade heavier than the content
        "foot":   "#1a1712d2",   # status line
    },
    # The three-piece kit the client ships locally (bgi01/bar01/logo01 under
    # viewer/data/pmlus/ucs/img_s). Needs nothing from the network, so it is the
    # fallback if serving art over the band port ever turns out to be a problem.
    "local": {
        "bg":     "file:/ucs/img_s/bgi01.png",
        "bar":    "file:/ucs/img_s/bar01.png",
        "logo":   "file:/ucs/img_s/logo01.png",
        "styles": _ART_STYLES,
        "head":   "#00000000",
        "scrim":  "#14120cb4",
        "row":    "#0f0d09c8",
        "foot":   "#1a1712d2",
    },
    # JP kit -- blue. Only present on a JP install; falls through to a missing
    # image (not a crash) anywhere else.
    "ucs-jp": {
        "bg":     "file:/ucs/img_s/bgi02.png",
        "bar":    "file:/ucs/img_s/bar02.png",
        "logo":   "file:/ucs/img_s/logo02.png",
        "styles": _ART_STYLES,
        "head":   "#00000000",
        "scrim":  "#0c1219b4",
        "row":    "#080d13c8",
        "foot":   "#0c1219d2",
    },
    # No art: solid bands over the generic system backdrop.
    "flat": {
        "bg":     "file:/systembg/bg01i.png",
        "bar":    None,
        "logo":   "file:/logo/lg01sw.png",
        "styles": _FLAT_STYLES,
        "head":   "#2e2a24ff",   # header bar, dark warm charcoal
        "title":  "#463f36ff",   # the band carrying the screen title
        "scrim":  "#ece7ddff",   # cream content panel (NOT white)
        "row":    "#2e2a24ff",
        "foot":   "#1a5cbeff",   # blue status footer
        "flat":   True,          # selects the banded layout, not the art one
    },
}



def art_served():
    """Is SE's registration art kit (the served skin's buttons, backdrop and
    icons) under WWW_DIR? A stack with no portal content has none of it, and
    then the wizard must draw with what the client ships: the `local` skin
    for the chrome and text plates for the buttons (see `_buttons`)."""
    return os.path.isfile(os.path.join(WWW_DIR, "ucs", "img_s", "bt01s.ang"))


#: The default skin follows the art on disk; POL_UCS_SKIN still overrides.
SKIN = SKINS.get(os.environ.get("POL_UCS_SKIN")
                 or ("ucs" if art_served() else "local"), SKINS["ucs"])
#: A transparent hit target the client ships (the same one the core's built-in
#: menu uses), for buttons drawn without the art kit.
HITBOX = "file:/img_s/general/im01s.png"


def _page(title, inner, extra_head="", background=None, onclose=None):
    bg = background or SKIN["bg"]
    bg = f' background="{bg}"' if bg else ""
    # SE submits a form on BODY CLOSE, not from the button href: every real
    # ucsgate page carries `<body onclose="sd:submit@fm">` (see ex8filter1.pml /
    # ex9filter1.pml, and the recipe transcribed at the top of this file). Without
    # it the typed field values never POST, so a form screen loops on itself. Only
    # form-bearing pages pass onclose; a page with no <form name="fm"> must not.
    oc = f' onclose="{onclose}"' if onclose else ""
    return (
        "<pml>\n<head>\n"
        f'\t<meta http-equiv="Content-Type" content="{PML_MIME}">\n'
        f"\t<title>{html.escape(title)}</title>\n"
        f"{SKIN['styles']}{_WIDGET_STYLE}{extra_head}"
        f"</head>\n<body{bg}{oc}>\n"
        f"{inner}"
        "</body>\n</pml>\n"
    )


def _band(name, y, h, color, enable="1"):
    # enable="0" makes the band non-selectable. A <scrollarea> is a focus target
    # for the controller, so a decorative band (esp. the footer at y=440, whose
    # top-left corner IS the screen's bottom-left) otherwise shows up as an
    # "invisible button" the cursor keeps landing on.
    return (f'<scrollarea name="{name}" pos="0,{y}" size="640,{h}" '
            f'hbar="never" vbar="never" skin="0" bgcolor="{color}" '
            f'skincolor="#00000000" selectedskincolor="#00000000" '
            f'enable="{enable}">\n')


#: SE's own footer-button plates for THIS flow, from `ucs/img_s/` -- animated
#: .ang sprites, served by us. Sizes are SE's, read off the size branch in
#: `p1_ftButton.pml`: [S] 102x30, [M] 170x30, [L] 250x30 (bt03s.ang was never
#: cached, so only S and M are available; nothing here needs 250px).
#:
#: The previous art here was `game/img_s/bt03s.ang` -- the FFXI in-game button
#: plate. Wrong set: the registration flow has its own.
BUTTON_ART_S = UCS_ART + "bt01s.ang"
BUTTON_ART_M = UCS_ART + "bt02s.ang"


#: A caption wider than this gets the [M] plate instead of [S].
_BTN_M_THRESHOLD = 12


def _buttons(buttons, y):
    """The Next / Reset / Back / Exit row, built the way SE builds a button.

    TRANSCRIBED from `ucs.pol.com/ucs/csp_parts/p1_ftButton.pml`, the shared
    footer-button include for this exact flow. One `<img>` per button, carrying
    BOTH the plate and the caption:

        <sheet name="shS" pos="X,Y" size="W,H" border="0" alpha="1"
               appeartime="300" delay="200" wait="2">
            <img name="NAME" pos="0,0" size="W,H" src="$imgPath+bt01s.ang"
                 style="Bt14" value="CAPTION" href="..." alt="^03help">
        </sheet>

    The earlier version here stacked a transparent 1x1 hitbox on top of a
    separate plate image. That is a real SE idiom -- it is how the PORTAL builds
    its big menu tiles -- but it is not how this flow builds its footer, and the
    two-image version can only ever approximate the plate/caption alignment that
    the single-image form gets by construction.

    `href` is passed through untouched, so a caller can hand this an `sd:` verb
    (`sd:submit@fm`) just as readily as a URL -- which is exactly how the form
    steps submit.
    """
    # SE centres the row: [S] 102x30 plates, 11px apart, starting at x=95 for the
    # four-button case. Compute the same centring for whatever count we have so
    # a 2- or 3-button screen is not left hanging at the left margin.
    plates = [(BUTTON_ART_M, 170) if len(c) > _BTN_M_THRESHOLD
              else (BUTTON_ART_S, 102) for c, _ in buttons]
    gap = 11
    total = sum(w for _, w in plates) + gap * (len(plates) - 1)
    x = max(12, (640 - total) // 2)

    out = []
    for i, ((caption, href), (art, w)) in enumerate(zip(buttons, plates)):
        out.append(f'\t<sheet name="shbt{i}" pos="{x},{y}" size="{w},30" '
                   f'border="0" alpha="1" appeartime="300" delay="200" '
                   f'wait="2">\n')
        # Put initial focus on the first button so the controller cursor starts
        # on a real, visible target instead of landing on the empty content
        # region (the "invisible button" the gamepad kept selecting first).
        focus = ' focus="1"' if i == 0 else ''
        if art_served():
            out.append(f'\t\t<img name="bt{i}" pos="0,0" size="{w},30" '
                       f'src="{art}" style="btn" value="{html.escape(caption)}" '
                       f'href="{href}"{focus} clicksound="1">\n')
        else:
            # NO ART KIT: a coloured text plate with a transparent hit target
            # over it, the idiom the core's built-in menu draws with.
            out.append(f'\t\t<text pos="0,0" size="{w},30" style="btn" '
                       f'align="center" valign="middle" bgcolor="#2a3340e0">'
                       f'{html.escape(caption)}</text>\n')
            out.append(f'\t\t<img name="bt{i}" pos="0,0" size="{w},30" '
                       f'src="{HITBOX}" href="{href}"{focus} clicksound="1">\n')
        out.append('\t</sheet>\n')
        x += w + gap
    return "".join(out)


def _wizard_art(badge, title, inner, buttons, footer, scroll):
    """Registration chrome built from the account area's own art kit.

    Layout, top to bottom:
      * title band   bar01.png at its native 578x56, centred at x=31, carrying
                     the screen name and the step counter, with the PlayOnline
                     badge on its feathered left end (logo01 at half scale)
      * content      the step's fields on a translucent scrim, so the backdrop
                     still reads behind them instead of being papered over
      * button row   Next / Reset / Back / Exit
      * footer       product name + one line of status text

    Geometry deliberately tracks the flat chrome's (content 70..392 against its
    64..396) so the absolute field positions inside every step still land.
    """
    # HEADER ZONE (0..118). Coordinates from ucs/csp_parts/p1_header.pml:
    #   * the PlayOnline mark sits TOP-RIGHT (its sheet is at 440,0), NOT top-left
    #   * the STEP counter is right-aligned in the strip to the LEFT of the mark
    #   * the page title is a band BELOW that strip (p1_header puts it at y~85)
    # The body content then lives in the panel BELOW this zone -- the whole point
    # of the fix: content must not start at the top of the window and butt into
    # the header, which is what the old top-left/y=12 layout did.
    out = [_band("head", 0, 140, SKIN["head"], enable="0")]
    out.append(f'\t<img name="logo" pos="470,36" size="146,50" '
               f'src="{SKIN["logo"]}">\n')
    # STEP counter on the LEFT of the top strip (per the marked-up reference).
    out.append(f'\t<text pos="24,48" size="220,24" style="step" '
               f'align="left" valign="middle">{html.escape(badge)}</text>\n')
    # Full-width title band (so a long name wraps within the header, not up
    # against the mark), sitting lower with clear padding above the body.
    out.append(f'\t<text pos="24,90" size="586,34" style="title" '
               f'valign="middle">{html.escape(title)}</text>\n</scrollarea>\n')

    # BODY -- the content panel, well BELOW the header (y=144, ~20px of padding
    # under the title band). Its size is PANEL_W x PANEL_H, the numbers the whole
    # layout grid below is derived from; nothing here may be edited without
    # re-running `tools/pmlfit.py --ucs`.
    vbar = "auto" if scroll else "never"
    out.append(f'<scrollarea name="paper" pos="0,{PANEL_Y}" '
               f'size="{PANEL_W},{PANEL_H}" '
               f'hbar="never" vbar="{vbar}" skin="0" bgcolor="{SKIN["scrim"]}" '
               f'skincolor="#00000000" selectedskincolor="#00000000">\n')
    out.append(inner)
    out.append('</scrollarea>\n')

    # Row band is a non-selectable background; the buttons are drawn OVER it at
    # top level so they stay focus targets while the band itself does not. The
    # band runs down to the footer so there is no gap in the chrome, but the
    # plates sit in its TOP third -- the row read as too low when it was centred.
    out.append(_band("row", ROW_Y, FOOT_Y - ROW_Y, SKIN["row"], enable="0"))
    out.append('</scrollarea>\n')
    out.append(_buttons(buttons, BTN_Y))

    # The status strip carries a fixed product label and a per-screen line. Both
    # boxes are sized to the copy: the label needs 172px at size 14 and the
    # longest status ~360px, and the previous 192/420 split ran the pair to
    # x=632 -- 8px from the edge, with the status itself over its box on two
    # screens ("Confirm your information and proceed to the next page." wanted
    # 433px of 420 and lost its tail).
    out.append(_band("foot", FOOT_Y, 30, SKIN["foot"], enable="0"))
    out.append('\t<text pos="14,4" size="216,22" style="foot" '
               'valign="middle">PlayOnline registration</text>\n')
    out.append(f'\t<text pos="236,4" size="390,22" style="foot" '
               f'valign="middle">{html.escape(footer)}</text>\n</scrollarea>\n')
    return "".join(out)


def _wizard_flat(badge, title, inner, buttons, footer, scroll):
    """The pre-art chrome: solid bands, cream panel, blue footer.

    Kept selectable (`POL_UCS_SKIN=flat`) as the comparison baseline -- it is
    the layout the wizard was first proven to render with.
    """
    out = [_band("bar1", 0, 26, SKIN["head"])]
    out.append('\t<text pos="14,2" size="330,22" style="hdr" valign="middle">'
               'PlayOnline registration</text>\n')
    out.append(f'\t<text pos="360,2" size="120,22" style="step" '
               f'valign="middle">{html.escape(badge)}</text>\n')
    out.append(f'\t<img name="logo" pos="500,1" size="130,24" '
               f'src="{SKIN["logo"]}">\n</scrollarea>\n')

    out.append(_band("bar2", 26, 38, SKIN["title"]))
    out.append(f'\t<text pos="18,6" size="600,26" style="title" '
               f'valign="middle">{html.escape(title)}</text>\n</scrollarea>\n')

    vbar = "auto" if scroll else "never"
    out.append(f'<scrollarea name="paper" pos="0,64" size="640,332" '
               f'hbar="never" vbar="{vbar}" skin="0" bgcolor="{SKIN["scrim"]}" '
               f'skincolor="#00000000" selectedskincolor="#00000000">\n')
    out.append(inner)
    out.append('</scrollarea>\n')

    out.append(_band("bar3", 396, 44, SKIN["row"]))
    out.append(_buttons(buttons, 8))
    out.append('</scrollarea>\n')

    out.append(_band("bar4", 440, 30, SKIN["foot"]))
    out.append(f'\t<text pos="14,4" size="600,22" style="foot" '
               f'valign="middle">{html.escape(footer)}</text>\n</scrollarea>\n')
    return "".join(out)


def _chrome(badge, title, inner, buttons, footer, scroll=True):
    """Registration chrome. The skin decides which layout draws.

    `badge` is the small right-aligned marker on the title bar ("STEP 3/7", or
    "" for pages outside the numbered flow). `buttons` is a list of
    (caption, href) pairs. `scroll` puts a vertical scrollbar on the content
    panel, which the real step 5/7 and 7/7 both have.
    """
    draw = _wizard_flat if SKIN.get("flat") else _wizard_art
    return draw(badge, title, inner, buttons, footer, scroll)


def _wizard(step, total, title, inner, buttons, footer, scroll=True):
    """A numbered step of the 7-screen flow."""
    return _chrome(f"STEP {step}/{total}", title, inner, buttons, footer, scroll)


def _codefields(y, st, name="rc", groups=5, width=88, gap=14):
    """The five dash-separated registration-code boxes from step 3/7."""
    parts = (st.get("code") or "").split("-")
    out = []
    x = 40
    for i in range(groups):
        val = parts[i] if i < len(parts) else ""
        out.append(_input(f"{name}{i}", (x, y), (width, 24), value=val,
                          maxlength=8))
        x += width
        if i < groups - 1:
            out.append(f'\t\t<text pos="{x},{y}" size="{gap},24" style="body" '
                       f'valign="middle">-</text>\n')
            x += gap
    return "".join(out)


#: In-flight registrations, keyed by a token carried in the URL. Deliberately
#: in-memory: a half-finished sign-up is not worth persisting, and dropping them
#: on restart is the correct behaviour.
#:
#: Values are (created_at, state). Both this and `_UCS_SESSION` used to be plain
#: dicts that only ever grew, and every request without a `t` minted another
#: entry -- a leak any crawler turns into a fast one. They now expire, and a
#: token this process did not issue is not adopted (see `_store_get`).
_SIGNUP = {}

#: How long a half-finished sign-up, or an authenticated account-page session,
#: stays valid. SE's own flow is a few minutes of typing.
_STORE_TTL = int(os.environ.get("POL_UCS_TTL", "1800"))


def _signup_open():
    """PERMISSIVE SIGN-UP: `POL_SIGNUP_ANY_CODE=1` accepts whatever the player
    types in the registration-code box and grants the new account every title
    this server offers (`POL_LOBBY_CONTENT_IDS`). For a private server whose
    players are its friends; the default keeps SE's shape, where a code minted
    on the admin panel decides who may join and what they get."""
    return os.environ.get("POL_SIGNUP_ANY_CODE", "0") == "1"


def _all_content_codes():
    """The titles this server offers, as the core lists them (its release
    default when the environment does not say)."""
    ids = os.environ.get("POL_LOBBY_CONTENT_IDS")
    if not ids:
        try:
            import srvcore
            ids = srvcore.RELEASE_DEFAULTS.get("POL_LOBBY_CONTENT_IDS", "1")
        except ImportError:
            ids = "1"
    out = []
    for c in ids.split(","):
        c = c.strip()
        if c.isdigit() and int(c) not in out:
            out.append(int(c))
    return tuple(out) or (1,)


def _store_sweep(store):
    now = time.time()
    for k in [k for k, (born, _) in store.items() if now - born > _STORE_TTL]:
        del store[k]


def _store_new(store, value=None):
    """Mint a token WE choose, and return it."""
    _store_sweep(store)
    tok = secrets.token_hex(16)
    store[tok] = (time.time(), {} if value is None else value)
    return tok


def _store_get(store, tok):
    """The state behind a token, or None.

    A token that is not ours is not adopted. It used to be: `st =
    store.setdefault(tok, {})` took whatever the URL said, so a guessed token
    landed in someone else's flow -- and for the authenticated store that is
    someone else's logged-in session.
    """
    _store_sweep(store)
    row = store.get(tok or "")
    return row[1] if row else None


def _signup_new():
    return _store_new(_SIGNUP)


def _u(step, tok, **extra):
    """Build a URL back into this CGI for a given step.

    The separator is a BARE `&`, not `&amp;`. MEASURED, and it was the bug that
    stalled the whole wizard: the Viewer does not entity-decode inside an href,
    so an escaped separator went out on the wire literally --

        GET /pml-cgi-bin/?kinou_id=20&amp;step=2&amp;t=54cdf65b2a243f4d

    -- which parses as the parameters `kinou_id`, `amp;step` and `amp;t`. With
    no `step` at all every Next re-served step 1 under a fresh token, so the
    flow looked like it was refusing to advance. SE's own pages write bare `&`
    in URLs (see the OKTO hidden values in the ucsgate pages); PML's `&` entity
    syntax is `&name=value;` and a `&` that does not terminate is passed
    through, so this is safe.
    """
    q = {"kinou_id": KINOU_REGISTER, "step": step, "t": tok}
    q.update(extra)
    return "?" + "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                          for k, v in q.items())


def _upath(step, tok, **extra):
    """`_u`, but rooted -- for a <formaction action=>, which is not a link.

    SE writes an absolute URL there (`action="https://secure.square-enix.com/
    ucsgate/ex8filter2.pml"`). Root-relative gets the same unambiguous resolution
    without pinning the host or the port, which matters because this service is
    reached as ucs.pol.com:51305 through the terminator and as :8080 direct.
    """
    return "/pml-cgi-bin/" + _u(step, tok, **extra)


def _urdt(tok, **extra):
    """The step-7 link: the same query, on a path that ENDS IN `.rdt`.

    Belt and braces on how the client picks a handler for the reply. app.dll's
    content-type table keys the RDT record on BOTH the MIME `text/rdt` and the
    extension `rdt`, exactly as it keys PML on text/x-playonline-pml and `pml`.
    We send the MIME; the extension costs one path segment and covers the case
    where it is the URL, not the header, that is consulted. Every path under
    /pml-cgi-bin/ dispatches on `kinou_id` (see the module docstring), so the
    name here is free -- nothing routes on it.

    Rooted, not relative, for the reason `_upath` gives.
    """
    return "/pml-cgi-bin/regist.rdt" + _u(7, tok, **extra)


# --------------------------------------------------------------------------- #
# forms
# --------------------------------------------------------------------------- #
# TRANSCRIBED, no longer inferred. The recipe is SE's, from
# account.square-enix.com/ucsgate/ex8filter1.pml and its sibling body page:
#
#     <head>
#       <formaction name="fa" action="URL" method="post" encode="UTF8">
#     </head>
#     <body onclose="sd:submit@fm">
#       <form name="fm" target="fa" onerror="dialog:$ER,...">
#         <input name="MAILADDR" type="text" style="In15" pos size skin
#                skincolor maxlength="128" check="daAsm1" lock onerror alt>
#         <input name="OKTO" type="hidden" value="...">
#         <input type="image" src="...bt01s.ang" size style value="Send">
#       </form>
#     </body>
#
# and the enum backing `type` is app.dll's own, read out of the memory image at
# 0x3dac14-0x3dacf4: checkbox / radio / select / image / password / hidden /
# input / textarea, with `method` one of post / get / polmail. So `type=
# "password"` is real and masked entry needs no workaround.
#
# WHAT THIS REPLACES, and why nothing typed ever reached the server: the fields
# used to be `<textbox>`. That tag is NOT an input -- SE's own
# ucs/csp_parts/p1_textbox.pml shows it is a scrolling READ-ONLY viewer bound to
# a <data> record (`ref="docs" sub="text" index="0"`), which is also how the
# Western registration screens deliver their long-form copy. A <textbox> has
# nothing to submit and no submission mechanism was attached to it either way.

def _formaction(action, name="fa"):
    """The submission target. Belongs in <head>, as SE puts it."""
    return (f'\t<formaction name="{name}" action="{action}" method="post" '
            f'encode="UTF8">\n')


def _form(inner, step, tok, name="fm", target="fa", kinou=KINOU_REGISTER):
    """Wrap field markup in the form the submit verb names.

    The wizard state rides as hidden fields as WELL as in the <formaction> URL's
    query. Belt and braces on purpose: `do_POST` merges the body over the query,
    so if the client preserves the action's query string the two agree, and if
    it posts to a bare path the hidden copies are the only surviving state. SE
    leans on hidden fields the same way (OKTO / OKTOPASS / NGTO in the ucsgate
    pages carry the whole continuation).

    `kinou` MUST match the page's operation: the hidden kinou_id overrides the
    URL's on POST (body wins in do_POST), so a hardcoded 20 here would misroute
    every account-servlet POST (login / code / password change) back into the
    registration handler. Registration keeps the default.
    """
    state = (_hidden("kinou_id", kinou)
             + _hidden("step", step) + _hidden("t", tok))
    return (f'\t<form name="{name}" target="{target}">\n'
            f'{state}{inner}\t</form>\n')


def _hidden(name, value):
    return (f'\t\t<input name="{name}" type="hidden" '
            f'value="{html.escape(str(value))}">\n')


def _input(name, pos, size, value="", maxlength=None, type_="text",
           style="In15", skin="3", skincolor="#eeeeeeaa", check="daAsm1"):
    """One entry field, built the way SE builds an EDITABLE text input.

    `skin="3"` is load-bearing, and it must be THREE, not five. The value is read
    straight off the real ucsgate field -- the exact page this flow reproduces --
    in `urlcache-1815/_orphan/6278.pml`:

        <define name="$SkN1" value="3">
        <define name="$SkC1" value="#eeeeeeaa">
        <input name="MAILADDR" type="text" style="In15" ... skin="$SkN1"
               skincolor="$SkC1" ... check="daAsm1" lock ...>

    We previously hardcoded `skin="5"`, copied from `wh000/.../collect/copm01.pml`
    -- but that is a *magazine* page, a different skin set. Skins are per-context
    resources: skin 5 is not defined in the account/ucsgate context, so the field
    rendered as a focusable box that never entered edit mode, which is exactly why
    the software keyboard would not summon (the field focused, the cursor showed,
    and pressing confirm did nothing).

    `check="daAsm1"` and `style="In15"` are likewise ucsgate's own values,
    verified against 6278.pml -- NOT invented. `daAsm1` is the permissive mode SE
    uses on both the email and the auth-code field, so it allows digits (the
    serial). `lock` matches SE. `length`/`maxlength` cap the entry.
    """
    x, y = pos
    w, h = size
    ml = (f' length="{maxlength}" maxlength="{maxlength}"'
          if maxlength else "")
    ck = f' check="{check}"' if check else ""
    return (f'\t\t<input name="{name}" type="{type_}" style="{style}" '
            f'pos="{x},{y}" size="{w},{h}" skin="{skin}" '
            f'skincolor="{skincolor}"{ml}{ck} lock'
            f' value="{html.escape(str(value))}">\n')


#: Opaque parchment fill for entry fields -- light enough for In15's dark text.
_FIELD_BG_COLOR = os.environ.get("POL_UCS_FIELD_BG", "#efe9dcff")


#: What a Next button does on a form screen: hand the form to the client's own
#: submit machinery (app.dll's ePmlFormSubmit) rather than navigate. The POST
#: lands on the <formaction> URL, which already carries kinou_id/step/t.
SUBMIT = "sd:submit@fm"


# --------------------------------------------------------------------------- #
# long-form copy
# --------------------------------------------------------------------------- #
# Any screen with more prose than fits gets it as a <data> record displayed
# through a <textbox> -- which is what that tag is actually FOR. SE's Western
# registration delivers every one of its long screens this way; the age and
# privacy screens arrive from usercte.pol.com as bare documents:
#
#     <data name="dt_age" sub="doc">
#     <record>
#     "
#     &pre=1;&pos=10;&style=Bw17;PlayOnline&trade; Service Privacy Policy&style;
#
#     Square Enix, Inc. (&quot;SEI&quot;) is committed to ...
#     "
#     </record>
#     </data>
#
# and the page binds them with `<textbox ref="dt_age" sub="doc" index="0">`.
# Inline markup inside a record: `&pre=1;` preformatted, `&pos=N;` indent,
# `&style=NAME; ... &style;` a span, plus the usual `&quot;` / `&trade;`.
#
# Positioned <text> lines -- what step 2 used to be built from -- cannot scroll
# and silently clip whatever runs past the panel.

def _doc(name, text, sub="doc"):
    """A <data> record holding one screen's prose."""
    return (f'<data name="{name}" sub="{sub}">\n<record>\n"\n'
            f'{text}\n"\n</record>\n</data>\n')


def _textbox(name, pos, size, ref, sub="doc", index=0, style="Bw15"):
    """The read-only scrolling viewer bound to a <data> record."""
    x, y = pos
    w, h = size
    return (f'\t<textbox name="{name}" pos="{x},{y}" size="{w},{h}" '
            f'style="{style}" margin="4,2,2,2" ref="{ref}" sub="{sub}" '
            f'index="{index}" skin="0" vbar="auto">\n')


#: Step 2's copy. Ours, not SE's: their page is Square Enix Inc's actual privacy
#: policy (recovered in full, AGE_01_00016_0.pml) and reproducing it here would
#: assert that SEI handles data this server holds, which is false. The MARKUP is
#: theirs; the statement is accurate for what this actually is.
_PRIVACY_DOC = """&pre=1;&pos=10;&style=Hw18;I.&pos=37;What this server is&style;

This is a private, non-commercial PlayOnline revival server. It is
not operated by, affiliated with, or endorsed by Square Enix.

&pos=10;&style=Hw18;II.&pos=37;What is stored&style;

Any details you enter during registration are stored only on this
server, in its own account database. Nothing is transmitted to
Square Enix or to any other third party.

No personal information is required. The contact fields on the
following screens may be left blank, and an account created with
them empty works exactly the same as one with them filled in.

Your password is stored salted and hashed, never in plain text.

&pos=10;&style=Hw18;III.&pos=37;Declining&style;

If you decline, registration cannot continue and no record of this
attempt is kept."""


def page_step1(tok):
    inner = (
        '\t<text pos="24,24" size="580,22" style="body" valign="middle">'
        'Please confirm your age before continuing.</text>\n'
        '\t<text pos="24,66" size="580,22" style="label" valign="middle">'
        'Are you 13 years of age or older?</text>\n'
        '\t<text pos="24,104" size="580,20" style="note" valign="middle">'
        'Users under 13 cannot register for PlayOnline.</text>\n')
    return _page("PlayOnline registration", _wizard(
        1, 6, "Age Confirmation", inner,
        [("Yes", _u(2, tok)), ("No", "tologin:"), ("Exit", "tologin:")],
        "Confirm your age to continue.", scroll=False))


def page_step2(tok):
    # The record itself, then the viewer bound to it. The panel is 640x322, so
    # the box takes the full width less a margin and scrolls internally rather
    # than letting the chrome's own scrollbar move the whole screen.
    inner = (_doc("dt_policy", _PRIVACY_DOC)
             + _textbox("policy", (16, 6), (600, 224), "dt_policy"))
    return _page("PlayOnline registration", _wizard(
        2, 6, "Privacy Policy", inner,
        [("Accept", _u(3, tok)), ("Decline", "tologin:"), ("Exit", "tologin:")],
        "Accept the terms to continue.", scroll=False))


def page_step3(tok, error=None):
    """Registration code. Positions are the ones that already render well; the
    copy is shortened to fit the box it is in -- the first line needed 608px of
    a 600px box, so it wrapped and lost the word "package" off the bottom."""
    st = _store_get(_SIGNUP, tok) or {}
    fields = (
        _line(20, "Enter the registration code from your software package.")
        # NOT "the code is case-sensitive" any more -- SE's wording, and it
        # was false of our codes even when we compared them exactly (see
        # accounts.normalise_regcode). A player read it, was refused for
        # typing lower case, and had to retype: prod 2026-08-29 15:28.
        + _line(42, "Upper or lower case is fine.")
        + _codefields(110, st)
        + _bottom(("note", "* You only need to enter this code once."),
                  ("note", error)))
    return _page("PlayOnline registration", _wizard(
        3, 6, "Entering Registration Code", _form(fields, 4, tok),
        [("Submit", SUBMIT), ("Exit", "tologin:")],
        "Enter your registration code.", scroll=False),
        extra_head=_formaction(_upath(4, tok)))


def page_step4(tok, error=None):
    """Enter Information -- password (x2) and handle. Nothing else.

    SE's original also collected name / postal / address / phone / country here.
    Those wrote to a `profile` table nothing on this server ever reads (verified:
    `get_profile` has zero callers) -- dead writes, and a PII liability with no
    payoff. A registration completes identically without them (verified: a full
    run wrote zero profile rows). The one "country" the client actually uses is
    the in-game Handle Profile field, set in-game via lobby 05:01, not here.

    Three fields also means the panel no longer overflows: the eleven-row version
    ran off the bottom and the button bar floated up into the address rows.

    Laid out on the grid: one intro line, four rows on the 34px rhythm, and the
    footnote anchored to the panel floor. Previously the footnote was placed by
    hand at y=230 in a 242px panel and hung 8px BELOW it, and two of the labels
    filled 92% of their column -- "PlayOnline password *" needed 185px of 200.
    """
    st = _store_get(_SIGNUP, tok) or {}
    y = 52
    out = (_line(TOP, "Choose a PlayOnline password and a handle name.")
           + _row(y, "PlayOnline ID", "(issued on completion)", "ok")
           + _field(y + ROW, "Password", "pw1", "(8-15 characters)",
                    maxlength=15, type_="password")
           + _field(y + 2 * ROW, "Confirm password", "pw2",
                    maxlength=15, type_="password")
           + _field(y + 3 * ROW, "Handle name", "handle", "(up to 15)",
                    value=st.get("handle", ""), maxlength=15)
           + _bottom(("note", "* Required"), ("note", error)))
    return _page("PlayOnline registration", _wizard(
        4, 6, "Enter Information", _form(out, 5, tok),
        [("Next", SUBMIT), ("Reset", _u(4, tok, reset="1")),
         ("Back", _u(3, tok)), ("Exit", "tologin:")],
        "Choose a password and a handle name.", scroll=False),
        extra_head=_formaction(_upath(5, tok)))


def page_step5(tok):
    """Confirmation. Shows what will be created; Next commits.

    Same three rows as before, on the grid, plus the code that was accepted --
    it is the one thing the player typed that they cannot otherwise check before
    committing. `scroll=False`: there is nothing here to scroll, and the panel's
    scrollbar was both a wasted 16px and a stray controller focus target.
    """
    st = _store_get(_SIGNUP, tok) or {}
    y = 56
    inner = (_line(TOP, "Please confirm the information you have entered.", indent=0)
             + _row(y, "Handle name", st.get("handle", "(not set)"), indent=0)
             + _row(y + ROW, "Password", "(hidden)", indent=0)
             + _row(y + 2 * ROW, "Registration code",
                    st.get("code") or "(none)", indent=0)
             + _bottom(("note", "Next creates your account and issues your "
                                "PlayOnline ID."), indent=0))
    return _page("PlayOnline registration", _wizard(
        5, 6, "Confirmation", inner,
        [("Next", _u(6, tok)), ("Back", _u(4, tok)), ("Exit", "tologin:")],
        "Check your details, then choose Next.", scroll=False))


# --------------------------------------------------------------------------- #
# RDT -- the block that makes the client create the member for the player
# --------------------------------------------------------------------------- #
# The player was never meant to transcribe the PlayOnline ID. The Viewer creates
# the login-screen member entry ITSELF when you join, and says so in its own
# words -- StringTable id 22131, built as a login-screen dialog at app.dll
# 0x49f8413:
#
#   "Four members have been registered. Additional members cannot be
#    AUTOMATICALLY CREATED BY JOINING PLAYONLINE. Would you like to register a
#    member manually?"
#
# What feeds that, measured in the rebased app.dll disassembly (VAs below are
# rebased, base 0x04850000):
#
#   0x4a65dc2  requires SEVEN fields to be present, reads them, and calls --
#   0x49fe1b4  which walks the existing members comparing POL-ID (skip if the
#              account is already there), returns -100 if the member count has
#              reached the maximum, and otherwise WRITES THE MEMBER. Its own
#              fallback for an empty POLCOM-HOST is the literal "ci000.pol.com"
#              at 0x49fe28f, which is where RDT_POLCOM_HOST below comes from.
#              POL-ID lands at member+8, POL-PASSWORD at member+0x10 (and sets
#              member+0x24 = 1, the "password saved" flag), POLCOM-HOST at
#              member+0x167.
#
# The fields are numbered, and the numbers resolve against the descriptor table
# at 0x4c371d0 -- 56 rows of {name, id, ..., namelen<<16|maxlen, minlen<<16,
# validator}. The seven the auto-create requires:
#
#   0x0e POLCOM-HOST       0x0f POL-ID            0x10 POL-PASSWORD
#   0x11 ACCOUNT-NUMBER    0x12 MASTER-POLID      0x25 POL-MAIL-ADDRESS
#   0x26 POL-MAIL-PASSWORD
#
# ## The wire format
#
# One `FIELD-NAME: value` per line, tokenised at 0x4a66c60: leading whitespace
# skipped, `#` starts a comment (0x4a66c79), a blank line is accepted, a line
# with no `:` is "Syntax error(':' not found)" and one with nothing before the
# `:` is "Syntax error(no field name)". Values are length- and syntax-checked
# per the descriptor row. Decoded off the table (the names are UTF-16LE, which
# is why an ASCII grep for them finds nothing): POL-ID min 8 / max 32,
# MASTER-POLID min 8 / max 8, POL-PASSWORD max 16, NEXT-URL max 128,
# ACCOUNT-NUMBER exactly 1 numeric char, POLCOM-HOST min 4.
#
# The document-level validator is 0x4a655f0. It requires PROTOCOL-VERSION and
# NEXT-URL always; requires POL-ID if any of POLCOM-HOST / POL-PASSWORD /
# ACCOUNT-NUMBER / MASTER-POLID is present; and requires PPP-LOGIN and
# PPP-PASSWORD **only if CONNECT-TYPE is present and is 0 or 2** (0x4a656a1),
# which is why this block does not mention CONNECT-TYPE: those fields belong to
# the ISP-provisioning half of RDT and we have no dial-up to provision.
# PROTOCOL-VERSION is checked for PRESENCE only -- no reader of field 0 exists
# anywhere in app.dll -- so "1" is a value, not a guess at a protocol.
#
# MISC-INFO-BEGIN:/MISC-INFO-END: wrap a free-form blob (captured verbatim into
# field 0x14, capped at 0x1800, 0x4a66326). Nothing requires it and we have
# nothing to put in it, so it is omitted; the "MISC-INFO-END: not define" error
# only fires when a BEGIN was opened.
#
# ## Where it may be served from
#
# The gate is at 0x4a656f8. A source URL beginning `https:` whose host ENDS IN
# `.pol.com` is accepted outright (0x4a6577d); anything else falls back to the
# allow-list in the install's `usr/all/url/rdthosts.bin`, which decrypts (32-bit
# stream, key 0xC8D1B221, `k += (~k >> 21) | (k << 11)` per dword, app.dll
# 0x4a64e81) to exactly:
#
#     https://userctl.pol.com/pml-cgi-bin/
#     https://signup.ocn.ne.jp/
#
# WARNING: THE ABOVE IS THE PREREQUISITE, AND IT IS NOT MET. This comment used to
# end "so this servlet qualifies twice over, and NO client file has to be
# touched -- the registration wizard is https ... so the hop is inside the one
# flow that satisfies it." That is FALSE for the configuration we actually run,
# and it is why the block was refused on its first live outing (2026-08-20).
#
# MEASURED, prod logs/ucs.log 2026-08-20T15:02:04Z: the wizard reaches step 6
# over PLAIN HTTP. The client's own header on the RDT fetch reads
#     referer: http://ucs.pol.com/pml-cgi-bin/?kinou_id=20&step=5&t=...
# because the confirmed-live sign-up route is the shim's `signup_http=1`, which
# rewrites the client's own "https:%s%s" builder to "http:". https sign-up does
# NOT work -- it dies at POL-1328 on the certificate, unresolved after six live
# cycles (see the retraction in this module's docstring). So tier 1 of the gate
# is closed to us: the scheme is tested FIRST, and ours is not https.
#
# Tier 2 is therefore the whole mechanism, and the stock rdthosts.bin does not
# admit us -- neither of its two entries is our host. The gate returns false,
# its caller at 0x4a670e8 never builds the parser at all, and the document --
# which is well-formed against every rule above -- is discarded whole, NEXT-URL
# included.
#
# So this block cannot work until `pol-server/pol-client/rdthosts.py --add` has
# put the live wizard prefix into the install's allow-list. Do NOT "fix" it by
# switching sign-up to https; that is the cert chase, and it is a known trap.
#
# ## Status 2026-09-06, measured, not assumed
#
# The installers now DO the allow-list edit (install.sh and
# Install-PolHookProxy.ps1 both call rdthosts.py at install time), so a fresh
# install is admitted. Before that it was a hand edit on exactly one machine,
# which is why every player's client refused the block.
#
# Two things this file got wrong on top of the gate, both fixed the same day:
#
#   * NEXT-URL was FORCED to https (`_abs_url`), which pointed the hop at :443
#     -- stub.py's self-signed listener, i.e. the POL-1328 wall. The gate does
#     not look at NEXT-URL at all, so this bought nothing and broke the hop
#     even for a client that accepted the block.
#   * The ID screen sat BEHIND the hop, so any failure cost the player their
#     PlayOnline ID as well as the auto-add. Inverted -- see `page_step6`.
#
# WARNING: STILL UNPROVEN LIVE. The whole mechanism has never completed once against
# a real client: `grep -c 'step=7' logs/ucs.log` was 0 across every sign-up
# ever made (three live sign-ups, 08-22, 08-25 and 08-29; account ids in the
# examples here are synthetic: MNOP3456, QRST7890, UVWX2468). The proof to look
# for now is a `step 8 REACHED` line. Until one exists, do not describe the
# auto-add as working.

#: The client's own default when POLCOM-HOST is empty (app.dll 0x49fe28f), and
#: the login directory in our topology either way.
RDT_POLCOM_HOST = "ci000.pol.com"

#: The one character the format cannot carry. `#` starts a comment and the
#: tokeniser TRUNCATES the line at it (app.dll 0x4a66c79: find '#', at index 0
#: the line is treated as blank, past 0 the length is cut back to it) -- before
#: the value is ever validated, so nothing downstream notices.
#:
#: This bites the password and only the password. Our policy allows every
#: printable ASCII (accounts.check_password_policy, 0x21-0x7E) and so does the
#: client's own field: POL-PASSWORD carries validator class 6 and NO regex at
#: all, so nothing there rejects `#`. (The earlier note here read "checked
#: against the regex `\g+` at 0x4c37b84" -- misread. That block pairs each
#: message with the pattern that FOLLOWS it, so 0x4c37b84 `\g+` belongs to
#: Syntax error(URL) and POL-ID's pattern is `\w+` at 0x4c37bb8.) So the
#: format is narrower than either end of it, and a `#` password would be
#: silently CUT SHORT on its way into the player's saved member -- they would
#: be left with a client that cannot log in and no sign of why. There is no
#: escape or quoting mechanism anywhere in the tokeniser to reach for.
#:
#: So we do not send a block we cannot send correctly; `_finish` falls back to
#: the completion screen, which is exactly where this flow stood before.
RDT_UNREPRESENTABLE = "#"


def rdt_representable(*values):
    """False if any value would not survive the RDT tokeniser intact."""
    return not any(RDT_UNREPRESENTABLE in v for v in values)


def rdt_member_block(polid, password, mail, mail_password, next_url,
                     polcom_host=RDT_POLCOM_HOST, account_number=0):
    """The RDT document that makes the Viewer create this account's member.

    Every field here is required by 0x4a65dc2 (the seven) or 0x4a655f0 (the two
    document-level ones); nothing is decorative, and adding a field that trips a
    conditional requirement -- CONNECT-TYPE is the one to watch -- would make the
    client demand PPP credentials we do not have. See the block comment above.

    `\\r\\n` line endings because that is what SE's own client-side RDT file,
    rdthosts.bin, uses.
    """
    rows = [
        ("PROTOCOL-VERSION", "1"),
        ("POLCOM-HOST", polcom_host),
        ("POL-ID", polid),
        ("POL-PASSWORD", password),
        ("ACCOUNT-NUMBER", str(account_number)),
        ("MASTER-POLID", polid),
        ("POL-MAIL-ADDRESS", mail),
        ("POL-MAIL-PASSWORD", mail_password),
        ("NEXT-URL", next_url),
    ]
    return Document("".join(f"{k}: {v}\r\n" for k, v in rows), RDT_MIME)


def page_step6(tok, polid, handle, contents, can_rdt=True):
    """Registration complete -- the issued PlayOnline ID.

    Three rows and three footnotes is the fullest screen in the flow, so it is
    also the one the grid buys the most on: the footnotes are stacked up from
    the panel floor rather than down from the rows, which is what put the last
    of them at y=230 in a 238px panel -- drawn outside it, i.e. not drawn.

    WARNING: THIS SCREEN NOW COMES BEFORE THE RDT HOP, AND THAT IS THE WHOLE POINT.
    It used to be served at step 7, i.e. only after the client had accepted the
    RDT block and followed its NEXT-URL. That made the ID -- the account's only
    name -- reachable exclusively through a hop we cannot observe, so ANY RDT
    failure cost the player both the auto-add and the ID. It is not theoretical:
    measured 2026-09-06, this flow had been served live three times (synthetic
    example ids: MNOP3456, QRST7890, UVWX2468) and step 7 has NEVER been fetched
    once. All three accounts exist and none of their owners were ever told the ID.

    So the order is inverted. Step 6 creates the account and renders this, with
    the auto-add offered as its first button (`_urdt` -> step 7). Whatever the
    client does with the block, the player has already seen the ID, and the
    worst case is a member they add by hand rather than an account they cannot
    name. `can_rdt` False drops that button entirely -- see RDT_UNREPRESENTABLE,
    where the password cannot survive the tokeniser and offering the auto-add
    would provision a member with a truncated password.

    SE's own equivalent screen (StringTable 12017, "PlayOnline member
    registration complete! Select 'Log in to PlayOnline now!' to log in right
    away.") shows no ID and gives no Add Member instructions at all, which is
    itself evidence that on the real service the member was already there. We
    are not in that position until the hop is proved live, so we say both.

    The Content IDs row gets a two-line box because that is the one value here
    whose length we do not control: a code granting four titles prints ~550px
    of names into a 374px column.
    """
    names = ", ".join(_pretty(c) for c in contents) or "none"
    y = 46
    notes = [
        # No quotes around Add Member: html.escape turns them into `&quot;` and
        # it is unverified whether the client decodes that entity inside a
        # <text> body (it demonstrably does inside a <data> record). Not worth a
        # live test to keep two quotes.
        ("note", "* Write this ID down -- you need it to log in."),
        ("note", "* Add to Login Screen saves it, or use Add Member."),
        ("note", "* Your PlayOnline password is required to log in."),
    ]
    if not can_rdt:
        notes[1] = ("note", "* Choose Add Member and enter the ID above.")
    inner = (_line(TOP, "Registration complete.", indent=0)
             + _row(y, "PlayOnline ID", polid, "ok", indent=0)
             + _row(y + 32, "Handle name", handle, indent=0)
             + _row(y + 64, "Content IDs", names, indent=0, h=44)
             + _bottom(*notes, indent=0))
    buttons = [("Log in to PlayOnline", "tologin:"), ("Exit", "tologin:")]
    if can_rdt:
        buttons.insert(0, ("Add to Login Screen", _urdt(tok)))
    return _page("PlayOnline registration", _wizard(
        6, 6, "Registration Complete", inner, buttons,
        "Registration complete.", scroll=False))


def page_step8(polid):
    """After the RDT hop: the member is on the login screen.

    Deliberately thin. The client only lands here if it accepted the block, so
    this is the one screen in the flow that can state the auto-add as fact
    rather than as a hope -- and reaching it at all is the live proof the hop
    works, which is exactly what `ucs.log` has never been able to show.
    """
    inner = (_line(TOP, "Your member has been added to the login screen.",
                   indent=0)
             + _row(56, "PlayOnline ID", polid, "ok", indent=0)
             + _bottom(("note", "* Choose Log in to PlayOnline to start."),
                       ("note", "* Your PlayOnline password is required."),
                       indent=0))
    return _page("PlayOnline registration", _wizard(
        6, 6, "Member Added", inner,
        [("Log in to PlayOnline", "tologin:"), ("Exit", "tologin:")],
        "Member added.", scroll=False))


# --------------------------------------------------------------------------- #
# kinou_id 25 / 31 -- the account servlet the game pages already link to
# --------------------------------------------------------------------------- #
# SE's own UI has these buttons and our mirror still serves them. On the FFXI ID
# page (pml/game/ff11/id/idpm01.pml, cipm01.pml) and Tetra Master's
# (pml/game/tetra/id/mepm001.pml), each is an href that builds
#
#   https://userctl.pol.com/pml-cgi-bin/UMENZ001.cgi
#     ?kinou_id=NN&ret_url=<page to come back to>&area_kbn=..&login_pf=..
#     &service_code=0001&property=..
#
# and the member-services menu (pml2/cs/change/chpm01.pml) links twelve more.
# Their labels and help text, straight off the pages:
#
#   12  購　入   "コンテンツIDの新規購入・追加購入を行います"    buy a Content ID
#   14  解　約   "コンテンツIDの解約を行います"                  cancel
#   15  復　活   "コンテンツIDを復活させます"                    reactivate
#   25  確　認   "有料コンテンツＩＤの確認"                      list entitlements
#   28  期間延長 WebMoney contract extension
#   31  登　録   "ファイナルファンタジーXIの拡張コンテンツの登録を行います"
#                                                              redeem a code
#
# 25 and 31 are implemented here; both run purely off `accounts.py` and need no
# billing of any kind. 12/14/15/28 are money operations on the real service and
# are deliberately left unimplemented -- see `page_kinou_unavailable`.
#
# ## How the servlet knows who is asking
#
# It ASKS. SE's error table (ucs/csp_parts/er/err5000.pml) opens with 5000
# "Invalid or incomplete information. Please check your PlayOnline ID and
# password, and try again.", with a `$lm` variant that prompts for the password
# alone when the ID is already known, and 5020/5521 repeat the pattern for
# reactivation and password change. So the flow's first screen is a credential
# form, and that is what we do.
#
# We do NOT use the x-MD5-pol Digest identity for this. The client does send a
# `userName` in that header ([[pol-portal-digest]]), and it is tempting, but no
# capture in logs/ contains an Authorization: Digest line, so what that field
# actually holds for a logged-in session is unverified. Asking is both faithful
# and provable.

#: Field names whose values must never reach the log. The POST body was logged
#: verbatim -- correct while the only form was the sign-up wizard being reverse
#: engineered, and NOT correct now that kinou_id 25/31 ask for an EXISTING
#: account's password: that would write live credentials to logs/ucs.log in the
#: clear, from a service listening on a published port.
_SECRET_FIELDS = ("pw", "pw1", "pw2", "password", "passwd", "polpass")


def _redact(body):
    """Replace secret field values in a urlencoded body with <redacted>."""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:                                      # pragma: no cover
        return body[:2048]
    out = []
    for pair in text.split("&"):
        k, sep, _ = pair.partition("=")
        out.append(f"{k}=<redacted>" if sep and k.lower() in _SECRET_FIELDS
                   else pair)
    return "&".join(out)[:2048]


#: Authenticated servlet sessions, token -> {"member_id", "polid", "login"}.
#: In memory and forgotten on restart, exactly like `_SIGNUP`: these are short
#: interactive flows, not something worth persisting. Same (created_at, state)
#: shape and the same rules -- expiring, and never adopting a token we did not
#: issue, which for THIS store would mean handing over a logged-in session.
_UCS_SESSION = {}

#: SE's own wording for a failed credential check (err5000, code 5000).
ERR_5000 = ("Invalid or incomplete information.\n"
            "Please check your PlayOnline ID and password, and try again.")
#: ...and for a bad code (err5000, code 5103).
ERR_5103 = "Invalid registration code.\nPlease check your registration code."
#: ...and for one already used (err5000, code 5534, reworded for a single code).
ERR_5534 = ("This action cannot be performed because the selected content "
            "has already been registered.")


def _ret_ok(ret_url):
    """Sanitise the caller-supplied return URL.

    `ret_url` arrives in the query string and we put it in an href, so it is
    attacker-controlled markup input on a listener bound to a published port.
    The client's own verbs are fine; an http(s) URL is allowed only back into
    the hostnames we serve. Anything else falls back to the main menu, which is
    where every one of these flows sensibly ends anyway.
    """
    if not ret_url:
        return "toviewer:"
    low = ret_url.strip()
    if low in ("tologin:", "toviewer:", "tomainmenu:"):
        return low
    parts = urllib.parse.urlsplit(low)
    if parts.scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        if host.endswith(".pol.com") or host.endswith(".playonline.com"):
            return low
    log(f"ret_url refused: {ret_url!r}")
    return "toviewer:"


def _pretty(code):
    """Display name for a content code.

    Delegates to `contentlist.content_title`, which is the one place these are
    spelled for a reader. This function used to carry its own copy of the table
    and fall back to `CONTENT_NAMES` -- the WIRE identifiers -- so any title the
    local copy had missed reached the player as `FrontMissionOnline`.
    """
    try:
        import contentlist
        return contentlist.content_title(code)
    except ImportError:                                    # pragma: no cover
        return CONTENT_NAMES.get(code, f"Content {code}")


#: Use the caller's identity (when there is one) to spare them the ID field.
#: On by default; `POL_UCS_PREFILL_ID=0` disables it and always asks for both.
#: See `accounts.sole_online_member` for what "identity" can mean here at all.
PREFILL_ID = os.environ.get("POL_UCS_PREFILL_ID", "1") != "0"


def page_login(kinou, tok, ret_url, title, error=None, polid="", known=False):
    """The credential screen both operations open with, in SE's two shapes.

    `known=True` renders the **password-only** form: the ID is stated rather than
    asked for, and rides the POST as a hidden field. This is SE's own second
    variant -- `err5000.pml` carries a `$lm` form that prompts for the password
    alone when the ID is already established -- so it is a shape the client was
    always given, not one invented here. It is used only when
    `accounts.sole_online_member` can say who is asking.

    `known=False` is the full ID + password form, unchanged. It is what a caller
    gets when identity is unavailable or ambiguous, and it is where a failed
    password lands (see `_account_step`), so a wrong guess is self-healing rather
    than a dead end: the player simply gets the ordinary form back.

    `polid` seeds the field in that full form -- on a retry it is whatever they
    typed, so a mistyped password does not also cost them the ID.

    Either way this stays inside the form SE already drew: same POST, same
    `kinou_id`/`step`/`t`, no new page and no new route
    ([[no-page-redirect-workarounds]]). The password is always checked.
    """
    if known and polid:
        fields = (_line(TOP, f"Signed in as {polid}.")
                  + _line(TOP + ROW, "Please enter your PlayOnline password.")
                  + _hidden("polid", polid)
                  + _field(56 + ROW, "PlayOnline password", "pw",
                           required=False, maxlength=15, type_="password")
                  + _bottom(("note", error)))
        footer = "Enter your PlayOnline password."
    else:
        fields = (_line(TOP, "Please enter your PlayOnline ID and password.")
                  + _field(56, "PlayOnline ID", "polid", required=False,
                           maxlength=32, value=polid)
                  + _field(56 + ROW, "PlayOnline password", "pw",
                           required=False, maxlength=15, type_="password")
                  + _bottom(("note", error)))
        footer = ("Enter your PlayOnline ID and password." if not polid else
                  "Check the PlayOnline ID and enter your password.")
    return _page("PlayOnline", _chrome(
        "", title, _form(fields, 2, tok, kinou=kinou),
        [("Next", SUBMIT), ("Exit", _ret_ok(ret_url))],
        footer, scroll=False),
        extra_head=_formaction(_probe(
            f"/pml-cgi-bin/?kinou_id={kinou}&step=2&t={tok}")))


#: Ask the CLIENT who it is, by putting a PML system variable in the form's
#: action URL. `POL_UCS_IDENT_PROBE=0` removes it.
#:
#: This is the only mechanism that can work here: there is no server-side path
#: from an HTTP request to a lobby session (see `accounts.sole_online_member`),
#: and "the only member online" is useless on a box where a second test account
#: is permanently logged in -- measured 2026-08-16, two fresh sessions while the
#: player was looking at the screen.
#:
#: UNPROVEN, which is why it starts as a probe that only LOGS. `$_HANDLEID` is a
#: real name from the engine's own table (FACTS, read out of the PS2 `pml.pex`)
#: but nothing on this server has ever exercised it.
#:
#: It goes LAST in the query string deliberately. An unknown variable renders as
#: `(Variable Error)`, whose SPACE truncates the request line
#: ([[ps2-portal-user-lang]]) -- last means a truncation costs only this probe,
#: and `kinou_id`/`step`/`t` still arrive. Putting it anywhere else would break
#: the submit for everyone the moment the name turned out to be wrong.
IDENT_PROBE = os.environ.get("POL_UCS_IDENT_PROBE", "1") != "0"
IDENT_VAR = os.environ.get("POL_UCS_IDENT_VAR", "$_HANDLEID")


def _probe(url):
    """Append the identity probe to a form action URL, if it is enabled."""
    return f"{url}&hid={IDENT_VAR}" if IDENT_PROBE else url


def _log_ident_probe(db, q):
    """Record what the client substituted for IDENT_VAR. Logs, nothing else.

    Until a real click proves what the variable holds, acting on it would be
    building on a guess. Three outcomes to tell apart:

      a handle id  -> the mechanism works, wire it up
      "(Variable"  -> wrong name; the space truncated the request line
      absent       -> the client dropped the query

    The FORM ACTION carrier was tried first and came back absent every time
    (measured 2026-08-16: the POST reached step 2, so kinou_id/step/t arrived --
    but only because they also ride as hidden fields). A NAVIGATION href works,
    which SE's own Membership page proves: its buttons build
    `...&area_kbn='+$_POL_UCS_AREA_KBN+'&login_pf='+$_POL_UCS_LOGIN_PF` and both
    arrive substituted (`area_kbn=01&login_pf=02`). So the probe now rides one of
    those hrefs instead.
    """
    if not IDENT_PROBE or "hid" not in q:
        return
    raw = q.get("hid") or ""
    verdict = ("EMPTY" if not raw
               else "VARIABLE-ERROR (wrong name)"
               if raw.startswith("(") or "Error" in raw
               else "RESOLVED")
    log(f"ident probe: {IDENT_VAR} -> {raw!r}  [{verdict}]")
    if verdict != "RESOLVED":
        return
    # `$_HANDLEID` is a HANDLE id, not a POL ID, and carries the `NN-NNNNNNN`
    # shape -- the very form once misread as a PlayOnline ID and shipped as one
    # (accounts.py mint_polid: `00-4946053` is z_hid 0x4B7885). So take the
    # digits after the dash; a bare integer is accepted too.
    try:
        hid = int(raw.split("-")[-1])
        row = (accounts.handle_by_guid(db, accounts.handle_guid(hid))
               or accounts.handle_by_client_guid(db, hid)
               or db.execute("SELECT * FROM handle WHERE id = ?",
                             (hid,)).fetchone())
        if row is None:
            log(f"ident probe: handle id {hid} names nobody here")
            return
        m = db.execute("SELECT polid FROM member WHERE id = ?",
                       (row["member_id"],)).fetchone()
        log(f"ident probe: handle id {hid} is {row['handle_name']!r} -> "
            f"member {row['member_id']} ({m['polid'] if m else '?'})")
    except Exception as exc:                   # diagnostic: never break the page
        log(f"ident probe: cannot read {raw!r} as a handle id ({exc!r})")


def _whoami(db):
    """(polid, known) for the caller -- ("", False) when nobody can be named.

    Fails soft and loudly-in-the-log on ANY error: identity is a convenience, and
    never worth a 500 on the one screen a player has to get through. Also keeps
    working against an `accounts` module that predates `sole_online_member`.
    """
    if not PREFILL_ID:
        return "", False
    try:
        row = accounts.sole_online_member(db)
    except Exception as exc:                               # pragma: no cover
        log(f"caller identity unavailable ({exc!r})")
        return "", False
    if row is None:
        return "", False
    return row["polid"], True


def page_contents(tok, ret_url, sess, codes):
    """kinou_id=25 -- 有料コンテンツＩＤの確認, "check paid Content IDs".

    The list is the whole screen, so the "register a code" advice moved to the
    status strip: it used to be positioned under the last row, which meant a
    six-title account pushed it 26px out of the panel. Six rows is every content
    id this server knows; more than that and the tail is summarised rather than
    drawn off the bottom.
    """
    rows, y = [], 48
    shown = codes[:6]
    for c in shown:
        rows.append(_line(y, _pretty(c), "ok", X + 22, VAL_W, 22, indent=0)
                    + _line(y, "code %d" % c, "hint", HINT_X, HINT_W, 22,
                            indent=0))
        y += 28
    if not codes:
        rows.append(_line(64, "No Content IDs are registered to this account.",
                          "note", X + 22, CW - 22, 22, indent=0))
    inner = (_line(TOP, "Content IDs registered to %s:" % sess["polid"],
                   indent=0)
             + "".join(rows)
             + _bottom(("hint", "...and %d more." % (len(codes) - len(shown))
                        if len(codes) > len(shown) else None), indent=0))
    return _page("PlayOnline", _chrome(
        "", "Content ID Confirmation", inner,
        [("Register a code", f"?kinou_id=31&t={tok}"
                             f"&ret_url={urllib.parse.quote(ret_url, safe='')}"),
         ("Exit", _ret_ok(ret_url))],
        "Register a code to add content.", scroll=False))


def page_content_pick(kinou, tok, ret_url, sess, codes, error=None):
    """kinou 14 / 15 -- choose which Content ID to cancel or restore.

    One button per title, because PML has no list control we can bind a
    selection to: SE's own widgets act via `href`, so the choice IS the link.
    Six rows is every content id this server knows (page_contents caps at the
    same six for the same reason -- a seventh row is drawn off the panel).
    """
    cancelling = kinou == KINOU_CANCEL
    rows, y = [], 48
    for c in codes[:6]:
        rows.append(_line(y, _pretty(c), "ok", X + 22, VAL_W, 22, indent=0)
                    + _line(y, "code %d" % c, "hint", HINT_X, HINT_W, 22,
                            indent=0))
        y += 28
    if not codes:
        rows.append(_line(64,
                          "There is nothing to cancel on this account."
                          if cancelling else
                          "No cancelled Content IDs to restore.",
                          "note", X + 22, CW - 22, 22, indent=0))
    head = ("Choose a Content ID to cancel:" if cancelling
            else "Choose a Content ID to restore:")
    inner = (_line(TOP, head, indent=0) + "".join(rows)
             + _bottom(("note", error) if error else None, indent=0))
    ret_q = urllib.parse.quote(ret_url, safe="")
    buttons = [(_pretty(c), f"?kinou_id={kinou}&t={tok}&step=3&code={c}"
                            f"&ret_url={ret_q}") for c in codes[:6]]
    buttons.append(("Exit", _ret_ok(ret_url)))
    status = ("The title stays on your account until you confirm."
              if cancelling else
              "Restoring puts the title back on the handle it was on.")
    return _page("PlayOnline", _chrome("", TITLE_CANCEL if cancelling
                                       else TITLE_REACTIVATE,
                                       inner, buttons, status, scroll=False))


def page_content_done(kinou, ret_url, code):
    """Confirmation after a cancel or a restore."""
    cancelling = kinou == KINOU_CANCEL
    what = _pretty(code)
    body = ((f"{what} has been cancelled.",
             "It no longer appears on your handle.",
             "You can restore it from this menu at any time.")
            if cancelling else
            (f"{what} has been restored.",
             "It is back on the handle it was registered to.",
             None))
    y, rows = TOP, []
    for text in body:
        if text:
            rows.append(_line(y, text, "note", X + 22, CW - 22, 22, indent=0))
            y += 24
    inner = "".join(rows) + _bottom(None, indent=0)
    return _page("PlayOnline", _chrome(
        "", TITLE_CANCEL if cancelling else TITLE_REACTIVATE, inner,
        [("Exit", _ret_ok(ret_url))],
        "Done.", scroll=False))


def page_code(tok, ret_url, sess, error=None):
    """kinou_id=31 step 2 -- the registration-code entry screen.

    Same shape as step 3 of the wizard, on purpose: it is the same task, and the
    code boxes sit at the same y so the two screens do not jump.
    """
    fields = (_line(TOP, "Registering to %s." % sess["polid"])
              + _line(TOP + LINE, "Enter your PlayOnline registration code.")
              + _line(TOP + 2 * LINE,
                      "Upper or lower case is fine.")
              + _codefields(110, {})
              + _bottom(("note", error)))
    return _page("PlayOnline", _chrome(
        "", "Registration Code", _form(fields, 3, tok, kinou=KINOU_REGCODE),
        [("Register", SUBMIT), ("Exit", _ret_ok(ret_url))],
        "Enter your registration code.", scroll=False),
        extra_head=_formaction(f"/pml-cgi-bin/?kinou_id=31&step=3&t={tok}"))


def _short_date(iso):
    """`2026-08-17T01:31:20Z` -> `2026-08-17 01:31`, for a one-line row.

    Anything unparseable is shown as-is rather than hidden: a stored value we do
    not recognise is worth seeing on the screen that exists to show stored
    values. A missing one is genuinely "never".
    """
    if not iso:
        return "(never)"
    text = str(iso).replace("T", " ").rstrip("Z").strip()
    return text[:16] if len(text) >= 16 else text


def page_memberinfo(tok, ret_url, sess, info):
    """kinou_id=1 -- review the account.

    NOT SE's screen, deliberately. SE reviewed the contact details it collected
    at sign-up -- real name, postcode, address, phone -- and this server does not
    collect them: page_step4 dropped those fields as "dead writes, and a PII
    liability with no payoff", `profile` has zero rows and `get_profile` zero
    callers. Rebuilding SE's form would resurrect a collection someone removed on
    purpose, into a table nothing reads.

    So this reviews what the server actually holds. Same shape, honest content.
    `info` is assembled by the caller, which is the only thing that touches the
    database.
    """
    rows, y = [], 48
    for label, value in info["rows"]:
        rows.append(_row(y, label, value, "ok", indent=0))
        y += 26
    inner = (_line(TOP, "Account details for %s:" % sess["polid"], indent=0)
             + "".join(rows)
             + _bottom(("hint", info.get("note")), indent=0))
    ret_q = urllib.parse.quote(ret_url, safe="")
    return _page("PlayOnline", _chrome(
        "", TITLE_MEMBERINFO, inner,
        [("Change...", f"?kinou_id={KINOU_MEMBERCHG}&t={tok}&ret_url={ret_q}"),
         ("Discord", f"?kinou_id={KINOU_DISCORD}&t={tok}&ret_url={ret_q}"),
         ("Exit", _ret_ok(ret_url))],
        # Shortened when the Discord button made the button row wider: the old
        # "The contact details SE collected are not kept here." clipped (pmlfit).
        # No apostrophe: html.escape turns it into &#x27;, which the client has
        # never been shown to decode.
        "Contact details are not kept here.", scroll=False))


def page_memberchange(tok, ret_url, sess):
    """kinou_id=4 -- what of the account can actually be changed.

    A menu rather than a form, because everything changeable here already has a
    screen of its own. Each button carries the SAME token: `_account_step`
    promotes an authenticated step 1 to step 2, so they open on the operation
    instead of asking for the password a second time (that is how page_contents
    hands off to kinou 31).
    """
    ret_q = urllib.parse.quote(ret_url, safe="")
    items = [("PlayOnline password", "Change the login password.",
              KINOU_PWCHANGE),
             ("Mail address", "Choose your mail name.", KINOU_MAILADDR),
             ("Mail password", "For POP3 and SMTP.", KINOU_MAILPW),
             ("Content IDs", "Review, cancel or restore titles.",
              KINOU_CONTENTS)]
    rows, y = [], 48
    for label, hint, _k in items:
        rows.append(_row(y, label, hint, "hint", indent=0))
        y += 26
    inner = (_line(TOP, "What would you like to change?", indent=0)
             + "".join(rows)
             + _bottom(("hint", "Your handle is changed in the Viewer, not "
                                "here."), indent=0))
    buttons = [(label, f"?kinou_id={k}&t={tok}&ret_url={ret_q}")
               for label, _h, k in items]
    buttons.append(("Exit", _ret_ok(ret_url)))
    return _page("PlayOnline", _chrome(
        "", TITLE_MEMBERCHG, inner, buttons,
        "Choose what to change.", scroll=False))


def page_discord(tok, ret_url, sess, link, error=None):
    """kinou_id=90 -- link this account to Discord (ours; polbridge.py).

    The code comes from `/playonline link` in Discord. Entering it HERE, behind
    the member's own password, is the whole proof of ownership -- the bot is
    never given a password (a firm design rule, 2026-09-13).
    """
    who = (link["discord_name"] or "a Discord account") if link else None
    fields = (_line(TOP, "Linked to Discord as %s." % who if who
                    else "Not linked to Discord.")
              + _field(56, "Link code", "code", "(from Discord)",
                       required=False, maxlength=9)
              + _bottom(("hint", "In Discord, type /playonline link for a code."),
                        ("note", error)))
    ret_q = urllib.parse.quote(ret_url, safe="")
    buttons = [("Link", SUBMIT)]
    if link:
        buttons.append(("Unlink",
                        f"?kinou_id={KINOU_DISCORD}&step=4&t={tok}&ret_url={ret_q}"))
    buttons.append(("Exit", _ret_ok(ret_url)))
    return _page("PlayOnline", _chrome(
        "", TITLE_DISCORD, _form(fields, 3, tok, kinou=KINOU_DISCORD), buttons,
        "Get PlayOnline messages in Discord.", scroll=False),
        extra_head=_formaction(
            f"/pml-cgi-bin/?kinou_id={KINOU_DISCORD}&step=3&t={tok}"))


def page_discord_done(ret_url, text, hint):
    inner = (_line(TOP, text, indent=0) + _bottom(("hint", hint), indent=0))
    return _page("PlayOnline", _chrome(
        "", TITLE_DISCORD, inner, [("Exit", _ret_ok(ret_url))],
        "Discord link updated.", scroll=False))


def page_mailaddr(tok, ret_url, sess, current, error=None):
    """kinou_id=5 -- pick a friendlier mail name.

    SE issues `x` + 12 digits at sign-up and lets you replace the local part
    later; the domain is never yours to choose. The hint quotes SE's own rule
    from the mail screen, which is what check_mail_local enforces.
    """
    # getattr, not attribute access: `accounts` is an optional import and is
    # None when the DB module is unavailable. _account_step refuses before it
    # can reach here, but a page that crashes on import state is a 500 waiting
    # to happen. The fallbacks are SE's own numbers from the mail screen.
    lo = getattr(accounts, "MAIL_LOCAL_MIN", 4)
    hi = getattr(accounts, "MAIL_LOCAL_MAX", 15)
    fields = (_line(TOP, "Your mail address is %s." % current)
              + _field(56, "Mail name", "local",
                       "(%d-%d characters, lowercase)" % (lo, hi),
                       required=False, maxlength=hi)
              # A line, not a field: the domain is fixed, and a disabled-looking
              # input is still an input the client will happily let you type in.
              + _line(56 + ROW, "The domain is always @pol.com.", "hint")
              + _bottom(("note", error)))
    return _page("PlayOnline", _chrome(
        "", TITLE_MAILADDR,
        _form(fields, 3, tok, kinou=KINOU_MAILADDR),
        [("Change", SUBMIT), ("Exit", _ret_ok(ret_url))],
        "Choose a mail name.", scroll=False),
        extra_head=_formaction(
            f"/pml-cgi-bin/?kinou_id={KINOU_MAILADDR}&step=3&t={tok}"))


def page_mailaddr_done(ret_url, addr):
    inner = (_line(TOP, "Your mail address is now %s." % addr, indent=0)
             + _bottom(("hint", "Mail sent to the old address is not "
                                "forwarded."), indent=0))
    return _page("PlayOnline", _chrome(
        "", TITLE_MAILADDR, inner, [("Exit", _ret_ok(ret_url))],
        "Mail address changed.", scroll=False))


def page_mailpw(tok, ret_url, sess, error=None):
    """kinou_id=6 -- set the mail password.

    This is NOT the PlayOnline password (kinou 17): it is the one the mail
    client sends to POP3/SMTP on 51260/51261. Stored hashed AND in plaintext,
    because APOP verifies MD5(banner + password) and a salted hash cannot
    produce that -- see accounts.set_mail_password.
    """
    fields = (_line(TOP, "Mail password for %s." % sess["polid"])
              + _field(56, "New password", "mp1", "(8-15 characters)",
                       required=False, maxlength=15, type_="password")
              + _field(56 + ROW, "Confirm password", "mp2", required=False,
                       maxlength=15, type_="password")
              + _bottom(("note", error)))
    return _page("PlayOnline", _chrome(
        "", TITLE_MAILPW,
        _form(fields, 3, tok, kinou=KINOU_MAILPW),
        [("Change", SUBMIT), ("Exit", _ret_ok(ret_url))],
        "This is your mail password, not your PlayOnline password.",
        scroll=False),
        extra_head=_formaction(
            f"/pml-cgi-bin/?kinou_id={KINOU_MAILPW}&step=3&t={tok}"))


def page_mailpw_done(ret_url):
    inner = (_line(TOP, "Your mail password has been changed.", indent=0)
             + _bottom(("hint", "Use it in your mail client, not at the "
                                "PlayOnline login."), indent=0))
    return _page("PlayOnline", _chrome(
        "", TITLE_MAILPW, inner, [("Exit", _ret_ok(ret_url))],
        "Mail password changed.", scroll=False))


def page_pwchange(tok, ret_url, sess, error=None):
    """kinou_id=17 -- set a new PlayOnline password (shown after auth)."""
    fields = (_line(TOP, "Changing the password for %s." % sess["polid"])
              + _field(56, "New password", "pw1", "(8-15 characters)",
                       required=False, maxlength=15, type_="password")
              + _field(56 + ROW, "Confirm password", "pw2", required=False,
                       maxlength=15, type_="password")
              + _bottom(("note", error)))
    return _page("PlayOnline", _chrome(
        "", "Change PlayOnline Password",
        _form(fields, 3, tok, kinou=KINOU_PWCHANGE),
        [("Change", SUBMIT), ("Exit", _ret_ok(ret_url))],
        "Enter a new password.", scroll=False),
        extra_head=_formaction(f"/pml-cgi-bin/?kinou_id=17&step=3&t={tok}"))


def page_pwchange_done(ret_url):
    inner = (_line(TOP, "Your PlayOnline password has been changed.", indent=0)
             + _bottom(("hint", "Use the new password the next time you "
                                "log in."), indent=0))
    return _page("PlayOnline", _chrome(
        "", "Password Changed", inner,
        [("Exit", _ret_ok(ret_url))],
        "Password changed.", scroll=False))


def page_code_done(ret_url, granted):
    names = ", ".join(_pretty(c) for c in granted) or "none"
    inner = (_line(TOP, "Registration complete.", indent=0)
             + _row(56, "Registered", names, "ok", indent=0, h=44)
             + _bottom(("hint", "The content is available the next time you "
                                "log in."), indent=0))
    return _page("PlayOnline", _chrome(
        "", "Registration Complete", inner,
        [("Exit", _ret_ok(ret_url))],
        "Registration complete.", scroll=False))


#: Why an operation is missing, as two lines of copy plus a closing hint. The
#: billing wording used to be hardcoded, which was right while the only reachable
#: buttons were the money ones -- it is wrong now that SE's Membership menu is
#: reachable and offers member-details and mail-account operations too. Telling
#: someone their address book is "part of PlayOnline's billing service" is worse
#: than saying nothing.
UNAVAIL_BILLING = ("It was part of PlayOnline's billing service.",
                   "Nothing on this server is charged for.",
                   "Content is granted by registering a registration code.")
UNAVAIL_PROFILE = ("This server does not keep the contact details the real "
                   "service collected.",
                   "There is nothing here to review or change.",
                   "Your handle and password are the account.")
UNAVAIL_MAIL = ("Mail account settings cannot be changed from here.",
                "The address is issued with the account and is fixed.",
                None)


def page_kinou_unavailable(kinou, ret_url, label, reason=UNAVAIL_BILLING):
    """An operation SE's menu offers that this server does not implement.

    Answered honestly rather than 404'd, and with a way back out -- a page with
    nothing clickable strands the player with no route to the menu.
    """
    why1, why2, hint = reason
    inner = (_line(TOP, label, indent=0)
             + _line(TOP + 2 * LINE,
                     "This option is not available on this server.", indent=0)
             + _line(TOP + 3 * LINE, why1, indent=0)
             + _line(TOP + 4 * LINE, why2, indent=0)
             + _bottom(("hint", hint), indent=0))
    return _page("PlayOnline", _chrome(
        "", "Not Available", inner,
        [("Exit", _ret_ok(ret_url))],
        f"kinou_id {kinou} is not implemented here.", scroll=False))


#: Every operation SE's Membership menu (pml2/cs/change/chpm01.pml) links that we
#: do not implement, by the label and comment SE's own page gives it. 17 (change
#: password) and 25 (review Content ID) are absent because they WORK; 31 is
#: reached from 25's screen. 12 and 28 are not on this menu but arrive from the
#: game pages, so they stay.
KINOU_UNAVAILABLE = {
    3:  ("Fees", UNAVAIL_BILLING),
    30: ("Payment Method", UNAVAIL_BILLING),
    16: ("Register/Change Payment Method", UNAVAIL_BILLING),
    12: ("Content ID purchase", UNAVAIL_BILLING),
    28: ("Contract extension", UNAVAIL_BILLING),
    # 14 (cancellation) and 15 (reactivation) used to sit here. They are the two
    # entries on this menu that need no billing to mean something: a licence is
    # granted by a registration code, so cancelling and restoring it is entirely
    # local. They are implemented -- see KINOU_CANCEL / KINOU_REACTIVATE.
}


def page_probe5(ret_url, action=None):
    """Probe v5 -- SE's OWN widget recipe, copied from a real archived page.

    The breakthrough: `www/wh000.pol.com/pml/info/index.pml` (archived by the
    portal crawl) contains a genuine SE <button>:

        <button name="bt_ic" pos="130,202" size="80,22" skin="1" style="C18"
                value="$tx31" href="sd:focus@im_icb,sd:show=0@sh_gd,sd:@sh_ic"
                onkeyleft="null:" ... alt="$tx32">

    Three things I had wrong, all visible in that one line:
      * the caption is `value=`  -- not label=, not content=, not element text
      * it needs `skin=`         -- without a skin it draws an empty pill
      * it needs `style=`        -- that is where the caption's font comes from
      * SE buttons act via `href=`, not by submitting an enclosing <form>

    No archived page uses <textbox>, so its skin id is still unknown -- hence the
    three skin variants below. Container is the proven v3/v0 <sheet>.
    """
    rows = []
    y = 118

    def add(letter, desc, markup):
        nonlocal y
        rows.append(
            f'\t<text pos="40,{y}" size="18,20" style="body" valign="middle">'
            f"{letter}</text>\n"
            f'\t<text pos="62,{y}" size="210,20" style="body" '
            f'valign="middle">{html.escape(desc)}</text>\n'
            f"\t{markup}\n")
        y += 34

    add("A", "textbox skin=1 +style +value",
        '<textbox name="ta" pos="290,%d" size="200,22" skin="1" style="field" '
        'value="" maxlength="16">' % y)
    add("B", "textbox skin=0",
        '<textbox name="tb" pos="290,%d" size="200,22" skin="0" style="body" '
        'value="">' % y)
    add("C", "textbox skin=2",
        '<textbox name="tc" pos="290,%d" size="200,22" skin="2" style="body" '
        'value="">' % y)
    add("D", "button, SE recipe exactly",
        '<button name="td" pos="290,%d" size="130,26" skin="1" style="field" '
        'value="Register" href="%s">'
        % (y, html.escape(action or ret_url)))

    return _page(
        "PML widget probe v5",
        '<sheet name="shReg" pos="0,0" size="640,400" border="0" type="0" '
        'alpha="0" zindex="0">\n'
        '\t<text pos="40,40" size="560,24" style="hdr" valign="middle">'
        "Widget probe v5 -- SE's own recipe</text>\n"
        '\t<text pos="40,68" size="560,20" style="body" valign="middle">'
        "skin= + style= + value=, copied from a real SE button.</text>\n"
        + "".join(rows)
        + link("lnkBack", (40, y + 20), (260, 20), html.escape(ret_url),
               "Return to the login screen", style="note")
        + "</sheet>\n")


def page_probe4(ret_url):
    """Probe v4 -- candidates taken from app.dll's ATTRIBUTE table, not invented.

    v0-v3 guessed attribute names and got blank pills and invisible textboxes.
    The parser's attribute vocabulary is at RVA 0x3ddf00-0x3de900 (reverse
    alphabetical, same shape as the tag table at 0x3df200). It contains
    `content`, `default`, `rows`, `cols`, `length`, `readonly`, `checked`,
    `border`, `borderskin`, `bordertype`, `clickable`, `onenter`, `onchange`,
    `itemheight` ... and NO `label`, `caption`, `text` or `title`. So the caption
    is almost certainly `content=`, which is also why `<button>Register</button>`
    drew a blank pill: the parser never reads element text for widgets.

    Container is unchanged from v3 (the baseline that renders).
    """
    rows = []
    y = 120

    def add(letter, desc, markup):
        nonlocal y
        rows.append(
            f'\t\t<text pos="40,{y}" size="18,20" style="body" valign="middle">'
            f"{letter}</text>\n"
            f'\t\t<text pos="62,{y}" size="230,20" style="body" '
            f'valign="middle">{html.escape(desc)}</text>\n'
            f"\t\t{markup}\n")
        y += 34

    add("A", 'button content=',
        '<button name="sa" pos="300,%d" size="130,26" content="Register">' % y)
    add("B", 'textbox content=',
        '<textbox name="sb" pos="300,%d" size="200,22" content="">' % y)
    add("C", 'textbox default=',
        '<textbox name="sc" pos="300,%d" size="200,22" default="type here">' % y)
    add("D", 'textbox rows/cols',
        '<textbox name="sd" pos="300,%d" size="200,22" rows="1" cols="20">' % y)
    add("E", 'textbox +border/skin',
        '<textbox name="se" pos="300,%d" size="200,22" border="1" '
        'bordertype="0" skin="0">' % y)

    return _page(
        "PML widget probe v4",
        '<sheet name="shReg" pos="0,0" size="640,400" border="0" type="0" '
        'alpha="0" zindex="0">\n'
        '\t<text pos="40,40" size="560,24" style="hdr" valign="middle">'
        "Widget probe v4 -- attributes from the parser</text>\n"
        '\t<text pos="40,70" size="560,20" style="body" valign="middle">'
        "A = button content=. B-E = textbox variants.</text>\n"
        '\t<form name="probe" action="' + html.escape(ret_url) + '" '
        'method="post">\n'
        + "".join(rows)
        + "\t</form>\n"
        + link("lnkBack", (40, y + 20), (260, 20), html.escape(ret_url),
               "Return to the login screen", style="note")
        + "</sheet>\n")


def page_probe3(ret_url):
    """Probe v3 -- the known-good page, with ONLY the widget spelling varied.

    History, because it is the whole lesson here:
      v0 (the real form)  <sheet alpha=0> + <form>  -> text drew, button drew as
                          a blank pill, textbox drew nothing.  GOOD BASELINE.
      v1  moved everything into <scrollarea> AND dropped <form>  -> text drew,
                          NOTHING else did. Two changes at once: uninformative.
      v2  <scrollarea> as a sibling background + <sheet> on top  -> WHITE SCREEN,
                          not even the header. The full-size scrollarea paints
                          over the sheet regardless of zindex.

    So v3 is byte-for-byte the v0 container -- one <sheet alpha="0" zindex="0">
    holding a <form> -- and the ONLY difference from v0 is which widget spellings
    sit in the rows. No background panel: contrast is a separate problem and
    mixing it in is what cost the last two runs.
    """
    rows = []
    y = 120

    def add(letter, desc, markup):
        nonlocal y
        rows.append(
            f'\t\t<text pos="40,{y}" size="18,20" style="body" valign="middle">'
            f"{letter}</text>\n"
            f'\t\t<text pos="62,{y}" size="220,20" style="body" '
            f'valign="middle">{html.escape(desc)}</text>\n'
            f"\t\t{markup}\n")
        y += 34

    add("A", "textbox bare", '<textbox name="ra" pos="300,%d" size="200,22">' % y)
    add("B", "textbox +style",
        '<textbox name="rb" pos="300,%d" size="200,22" style="body">' % y)
    add("C", "button CONTENT",
        '<button name="rc" pos="300,%d" size="130,26">Register</button>' % y)
    add("D", "button label= [control]",
        '<button name="rd" pos="300,%d" size="130,26" label="Register">' % y)

    return _page(
        "PML widget probe v3",
        '<sheet name="shReg" pos="0,0" size="640,400" border="0" type="0" '
        'alpha="0" zindex="0">\n'
        '\t<text pos="40,40" size="560,24" style="hdr" valign="middle">'
        "Widget probe v3</text>\n"
        '\t<text pos="40,70" size="560,20" style="body" valign="middle">'
        "D is the control -- it drew a blank pill in the first render.</text>\n"
        '\t<form name="probe" action="' + html.escape(ret_url) + '" '
        'method="post">\n'
        + "".join(rows)
        + "\t</form>\n"
        + link("lnkBack", (40, y + 20), (260, 20), html.escape(ret_url),
               "Return to the login screen", style="note")
        + "</sheet>\n")


def page_probe2(ret_url):
    """Probe v2 -- one variable at a time.

    v1 was a botched experiment: it moved the widgets from a <sheet> into a
    <scrollarea> AND dropped the <form>, then nothing drew at all -- including
    the <button label=...> control that HAD drawn (as a blank pill) in the very
    first render inside <sheet><form>. Two changes, one result, no information.

    So: the <scrollarea> stays as a pure BACKGROUND (it gave us readable
    contrast, which was the one thing v1 did prove), the widgets go back inside
    a <sheet>, and the only things varied are the widget spelling and whether a
    <form> wraps them.
    """
    rows = []
    y = 96

    def add(letter, desc, markup):
        nonlocal y
        rows.append(
            f'\t<text pos="24,{y}" size="18,20" style="body" valign="middle">'
            f"{letter}</text>\n"
            f'\t<text pos="46,{y}" size="250,20" style="note" valign="middle">'
            f"{html.escape(desc)}</text>\n"
            f"\t{markup}\n")
        y += 36

    inside = []
    inside.append(('A', 'textbox bare (in form)',
                   '<textbox name="qa" pos="300,%d" size="200,22">'))
    inside.append(('B', 'textbox +style (in form)',
                   '<textbox name="qb" pos="300,%d" size="200,22" style="body">'))
    inside.append(('C', 'button, caption CONTENT (in form)',
                   '<button name="qc" pos="300,%d" size="130,26">Register</button>'))
    inside.append(('D', 'button, label= [control: drew blank before]',
                   '<button name="qd" pos="300,%d" size="130,26" label="Register">'))

    body = ['\t<form name="probe" action="' + html.escape(ret_url) + '" '
            'method="post">\n']
    for letter, desc, tmpl in inside:
        add(letter, desc, tmpl % y)
    body += rows
    body.append("\t</form>\n")

    rows = []
    add('E', 'button, CONTENT, OUTSIDE any form',
        '<button name="qe" pos="300,%d" size="130,26">Outside</button>' % y)
    body += rows

    return _page(
        "PML widget probe v2",
        # Background panel only -- proven to give readable contrast in v1.
        '<scrollarea name="bg" pos="0,0" size="640,470" hbar="never" '
        'vbar="never" skin="0" bgcolor="#efefefff" skincolor="#00000000" '
        'selectedskincolor="#00000000">\n</scrollarea>\n'
        # Widgets live in a <sheet>, which is where the button DID draw.
        '<sheet name="shProbe" pos="0,0" size="640,470" border="0" type="0" '
        'alpha="0" zindex="5">\n'
        '\t<text pos="24,44" size="580,24" style="hdr" valign="middle">'
        "PML widget probe v2 -- sheet + form</text>\n"
        + "".join(body)
        + link("lnkBack", (24, y + 20), (260, 20), html.escape(ret_url),
               "Return to the login screen", style="note")
        + "</sheet>\n")


def page_probe(ret_url):
    """A markup probe: several spellings of the widgets we can't yet render.

    The first live render proved <text>, <style> and <sheet> work, but <textbox>
    drew NOTHING and <button label="..."> drew an uncaptioned pill. SE's captured
    page has no `label`/`caption`/`text=` attribute anywhere, so captions are
    almost certainly element CONTENT (as they are for <text>). Rather than guess
    one spelling per launch, render them all side by side and let one screenshot
    pick the winner. Row letters are drawn next to each candidate.
    """
    rows = []
    y = 90

    def add(letter, desc, markup):
        nonlocal y
        rows.append(
            f'\t<text pos="24,{y}" size="18,20" style="body" valign="middle">'
            f"{letter}</text>\n"
            f'\t<text pos="46,{y}" size="250,20" style="note" valign="middle">'
            f"{html.escape(desc)}</text>\n"
            f"\t{markup}\n")
        y += 34

    add("A", "textbox bare", '<textbox name="pa" pos="300,%d" size="200,22">' % y)
    add("B", "textbox +style", '<textbox name="pb" pos="300,%d" size="200,22" '
                               'style="body">' % y)
    add("C", "textbox +skin", '<textbox name="pc" pos="300,%d" size="200,22" '
                              'skin="0" bgcolor="#ffffffff">' % y)
    add("D", "textbox w/ close tag",
        '<textbox name="pd" pos="300,%d" size="200,22"></textbox>' % y)
    add("E", "listbox (does any widget draw?)",
        '<listbox name="pe" pos="300,%d" size="200,44" skin="0">' % y)
    add("F", "button, caption as CONTENT",
        '<button name="pf" pos="300,%d" size="120,26">Register</button>' % y)
    add("G", "button, caption + style",
        '<button name="pg" pos="300,%d" size="120,26" style="body">Register'
        '</button>' % y)
    add("H", "button, label= (known blank)",
        '<button name="ph" pos="300,%d" size="120,26" label="Register">' % y)

    return _page(
        "PML widget probe",
        # An opaque backing panel. <scrollarea> is the one tag we have seen take
        # a bgcolor, and the first render was unreadable against the animated
        # PlayOnline background.
        '<scrollarea name="bg" pos="0,0" size="640,470" hbar="never" '
        'vbar="never" skin="0" bgcolor="#efefefff" '
        'skincolor="#00000000" selectedskincolor="#00000000">\n'
        '\t<text pos="24,40" size="580,24" style="hdr" valign="middle">'
        "PML widget probe -- which of these draw?</text>\n"
        + "".join(rows)
        + link("lnkBack", (24, y + 16), (260, 20), html.escape(ret_url),
               "Return to the login screen", style="note")
        + "</scrollarea>\n")


def page_launcher(action, ret_url="tologin:"):
    """The pre-login way IN to the wizard, emitted as a static file.

    Why this page exists at all: the client cannot open the real sign-up URL.
    app.dll asks for it over TLS on ucs.pol.com:51305 with a genuine SSLv3
    ClientHello and modern OpenSSL cannot answer one -- measured, the client
    reports POL-1322 / VERSION_TOO_LOW. But it DOES fetch
    POL_SERVERINFO_PAGE_URL over plain HTTP, so that page is used as the door:
    it is written to www/info.playonline.com/snews/*/index.pml and its button
    links into the plain-HTTP twin of this service.

    It carries NO input fields. The previous version had polid/password/handle
    textboxes on it, which was doubly wrong: the button is `href=` navigation so
    nothing typed was ever sent, and the wizard ISSUES the PlayOnline ID at step
    7 rather than letting you pick one.

    Written by tools/gen_snews.py, which calls this so the page cannot drift
    from the wizard's chrome.
    """
    inner = (
        '\t<text pos="24,30" size="580,24" style="label" valign="middle">'
        'Welcome to PlayOnline.</text>\n'
        '\t<text pos="24,72" size="580,20" style="body" valign="middle">'
        'Registering creates a PlayOnline ID on this server. The ID is issued '
        'to you at</text>\n'
        '\t<text pos="24,94" size="580,20" style="body" valign="middle">'
        'the end of the process -- you choose a password and a handle name.</text>\n'
        '\t<text pos="24,136" size="580,20" style="body" valign="middle">'
        'Have your registration code ready if you have one.</text>\n'
        '\t<text pos="24,186" size="580,18" style="note" valign="middle">'
        'This server is a private PlayOnline revival. Accounts are local to '
        'it.</text>\n'
        # A bare text+hitbox escape, with no button plate over it. Same
        # `<img href>` mechanism as the buttons, so it is not a second
        # experiment -- it is here so that a page which somehow still draws
        # nothing clickable cannot strand you with no way back to the login
        # screen, which is exactly how the last two live tests ended.
        + link("lnkBack", (24, 236), (300, 20), ret_url,
               "Return to the login screen", style="note"))
    return _page("PlayOnline Registration", _chrome(
        "", "PlayOnline Registration", inner,
        [("Begin registration", action), ("Exit", ret_url)],
        "Set up login information.", scroll=False))


def page_message(title, message):
    return _page(
        title,
        '<sheet name="shMsg" pos="0,0" size="640,400" border="0" type="0" '
        'alpha="0" zindex="0">\n'
        f'\t<text pos="40,40" size="560,24" style="hdr" valign="middle">'
        f"{html.escape(title)}</text>\n"
        f'\t<text pos="40,90" size="560,20" style="body" valign="middle">'
        f"{html.escape(message)}</text>\n"
        "</sheet>\n")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "Apache"            # SE's box says Apache; don't stand out
    sys_version = ""                     # ...and don't append "Python/3.x" to it
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # silence the default stderr logger
        # TEMP CAPTURE (remove): surface pre-dispatch HTTP parse failures that
        # are normally swallowed, so we can see what a non-conforming client sent.
        try:
            log("[rawcap] parse: " + (fmt % args))
        except Exception:
            log("[rawcap] parse: " + repr((fmt, args)))

    def handle_one_request(self):
        # TEMP CAPTURE (remove): dump the first raw bytes the client sends BEFORE
        # HTTP parsing, without consuming them (peek), to identify the dialect.
        try:
            import binascii
            raw = self.rfile.peek(256)
            if raw:
                log(f"[rawcap] {self.client_address[0]} first {len(raw)}B "
                    f"hex={binascii.hexlify(raw[:96]).decode()} ascii={raw[:96]!r}")
            else:
                log(f"[rawcap] {self.client_address[0]} connected but sent no bytes")
        except Exception as e:
            log(f"[rawcap] peek failed: {e}")
        return super().handle_one_request()

    def _log_request(self, body=b""):
        hdrs = "; ".join(f"{k}: {v}" for k, v in self.headers.items())
        log(f"{self.client_address[0]} {self.command} {self.path}")
        log(f"    headers: {hdrs}")
        if body:
            log(f"    body[{len(body)}]: {_redact(body)!r}")

    def _send(self, text, status=200):
        payload = text.encode("utf-8")
        self.send_response(status)
        # PML unless the page says otherwise -- see `Document`. The one reply
        # that says otherwise is the RDT block.
        self.send_header("Content-Type", getattr(text, "mime", PML_MIME))
        self.send_header("Content-Length", str(len(payload)))
        # NEVER cache a wizard page. It carries a per-session token and changes
        # as we iterate; without this the Viewer caches it (usr/all/url/cache)
        # and shows a stale step forever -- which masked every fix during the
        # first live test (it kept rendering the old 7-step pages).
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(payload)

    def _query(self):
        parts = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        return parts.path, {k: v[0] for k, v in q.items()}

    def _wizard_step(self, q):
        """Walk the 6-step registration flow (1 age, 2 privacy, 3 code,
        4 enter info, 5 confirm, 6 complete).

        State lives in `_SIGNUP[token]`; the token rides in the URL because the
        client follows plain hrefs between steps. Field values are read out of
        `q`, the URL query merged with any POST body (see `do_POST`).

        PROVEN (offline harness, curl): the server flow is correct end to end --
        when pw1/pw2/handle arrive, the flow advances 4->5->6 and issues a real
        PlayOnline ID. The remaining risk is entirely CLIENT-SIDE: whether the
        real 2003 Viewer's `<form>`/`onclose="sd:submit@fm"` machinery actually
        gathers the step-4 <input> values and POSTs them. If it does not, step 5
        bounces back to step 4 on the empty password -- exactly the historical
        stall. The form now matches SE's ex8filter1 recipe (no overlapping
        backstop scrollarea, which was our own addition and the prime suspect).
        Every request is logged verbatim, so the first live press shows the truth.
        """
        try:
            step = int(q.get("step") or 1)
        except ValueError:
            step = 1                                # never dead-end on garbage
        # A token we did not issue starts a NEW sign-up rather than being
        # adopted: the alternative is that any `t=` value in a URL becomes live
        # state. The cost of being strict is that a client holding a token from
        # before a restart begins again at step 1, which is the right outcome --
        # its half-filled state is gone either way.
        tok = q.get("t") or ""
        st = _store_get(_SIGNUP, tok)
        if st is None:
            tok = _signup_new()
            st = _store_get(_SIGNUP, tok)
        if q.get("reset"):
            # SE's "Reset" button clears the current screen's entries.
            for k in list(st):
                if k not in ("code",):
                    st.pop(k, None)
        # Only the three fields we actually store. The old code also collected
        # accounts.PROFILE_COLUMNS (name/postal/address/phone/country) into a
        # table nothing reads -- dropped, see page_step4.
        for k in ("handle", "pw1", "pw2"):
            if q.get(k):
                st[k] = q[k]
        code = "-".join(q[f"rc{i}"] for i in range(5) if q.get(f"rc{i}"))
        if code:
            st["code"] = code

        # Six steps the player SEES: 1 age, 2 privacy, 3 reg code, 4 enter info
        # (pw + handle), 5 confirm, 6 the completion screen with the issued ID.
        # Country (an old step 4) is gone entirely.
        #
        # Eight internally. 6 commits and SHOWS THE ID -- always, whatever
        # happens next. 7 is the RDT block that provisions the member, reached
        # from that screen's first button; its reply is not a page at all. 8 is
        # where the block's NEXT-URL lands, i.e. the only proof this side ever
        # gets that the client accepted it.
        #
        # WARNING: The ID SCREEN COMES FIRST ON PURPOSE. Until 2026-09-06 the order was
        # 6=RDT, 7=ID screen, which put the account's only name behind a hop we
        # cannot observe -- and prod's ucs.log shows that hop has never once
        # completed, so three real players were left with an account they could
        # not name. Do not reorder these back. See `page_step6`.
        #
        # The wizard chrome still says 6 of 6 on the last two screens -- the hop
        # is machinery, not a step anyone takes.
        if step == 1:
            return page_step1(tok)
        if step == 2:
            return page_step2(tok)
        if step == 3:
            return page_step3(tok)
        if step == 4:
            # Validate the code on the way out of step 3, the way SE's Submit
            # does. A BLANK code must be rejected too -- previously the check
            # only ran `if st.get("code")`, so submitting the page with empty
            # boxes skipped validation and advanced, which read as "it let me
            # through without a code".
            if accounts is not None:
                code = st.get("code")
                if not code:
                    return page_step3(tok, error="Please enter your "
                                                 "registration code.")
                db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                     accounts.DEFAULT_DB))
                try:
                    if _signup_open():
                        pass            # any code opens the door; see _signup_open
                    elif accounts.check_regcode(db, code) is None:
                        return page_step3(tok, error="That registration code is "
                                                     "not valid or has already "
                                                     "been used.")
                finally:
                    db.close()
            return page_step4(tok)
        if step == 5:
            # Validate password + handle on the way out of the info page.
            if accounts is not None:
                pw = st.get("pw1", "")
                bad = accounts.check_password_policy(pw)
                if bad:
                    return page_step4(tok, error=bad)
                if pw != st.get("pw2", ""):
                    return page_step4(tok, error="The two passwords do not match.")
                bad = accounts.check_handle_policy(st.get("handle", ""))
                if bad:
                    return page_step4(tok, error=bad)
            st["confirmed"] = True
            return page_step5(tok)
        if step == 6:
            # GATE. Step 6 is the only request that WRITES, and it used to commit
            # on nothing but the URL: `?kinou_id=20&step=6&t=anything` would mint
            # a live account, unauthenticated, from a port we expose. The client
            # can only reach here via the step-5 confirm, so requiring that flag
            # costs the real flow nothing and shuts the bare URL out.
            if not st.get("confirmed"):
                log(f"step 6 refused: signup {tok!r} never cleared confirmation")
                return page_step4(tok, error="Please complete the previous "
                                             "steps before continuing.")
            return self._finish(tok, st)
        if step == 7:
            # The RDT hop, reached from the completion screen's first button.
            # The account already exists by now -- this only builds the block
            # for it, so a refused or retried fetch costs nothing and the
            # player keeps the ID they were shown at step 6.
            done = st.get("account")
            if not done:
                log(f"step 7 without an issued account for signup {tok!r}")
                return page_step1(tok)
            if not done.get("pw") or not rdt_representable(
                    done["pw"], done["mail"], done["polid"]):
                # Credentials already scrubbed (step 8 has run), or a value the
                # tokeniser would truncate. Either way the block cannot be
                # built honestly, so show the ID again rather than send one.
                log(f"{done['polid']}: RDT hop declined "
                    f"(unrepresentable or already scrubbed)")
                return page_step6(tok, done["polid"], done["handle"],
                                  done["contents"], can_rdt=False)
            log(f"step 7: serving the RDT block for {done['polid']}")
            return rdt_member_block(
                done["polid"], done["pw"], done["mail"], done["mail_pw"],
                self._abs_url(_upath(8, tok)))
        if step == 8:
            # Where the RDT block's NEXT-URL lands -- and the ONLY evidence
            # this side ever gets that the client accepted the block. Log it
            # loudly: `grep 'step 8' ucs.log` is the live proof the auto-add
            # works, and its absence is how we know it never has.
            done = st.get("account")
            if not done:
                log(f"step 8 without an issued account for signup {tok!r}")
                return page_step1(tok)
            log(f"step 8 REACHED for {done['polid']} -- the client applied the "
                f"RDT block and created the member itself")
            # The credentials were kept only to rebuild the block for a retried
            # fetch. Past the hop they are dead weight in a process-resident
            # dict, so drop them; the page below needs only the ID.
            for k in ("pw", "mail_pw"):
                done.pop(k, None)
            st.pop("pw1", None)
            st.pop("pw2", None)
            return page_step8(done["polid"])
        return page_step1(tok)

    def _account_step(self, kinou, q):
        """kinou_id 25 / 31: credential screen, then the operation.

        `step` is carried the same way the wizard carries it, and `t` names the
        authenticated session. Every path that touches the database goes through
        `_UCS_SESSION`, so an unauthenticated request can reach the login form
        and nothing else.
        """
        if accounts is None:
            return page_message("PlayOnline", "Account database unavailable.")
        ret_url = q.get("ret_url") or "toviewer:"
        try:
            step = int(q.get("step") or 1)
        except ValueError:
            step = 1
        tok = q.get("t") or ""
        sess = _store_get(_UCS_SESSION, tok)
        title = {KINOU_CONTENTS: "Content ID Confirmation",
                 KINOU_PWCHANGE: "Change PlayOnline Password",
                 KINOU_CANCEL: TITLE_CANCEL,
                 KINOU_REACTIVATE: TITLE_REACTIVATE,
                 KINOU_MAILADDR: TITLE_MAILADDR,
                 KINOU_MAILPW: TITLE_MAILPW,
                 KINOU_MEMBERINFO: TITLE_MEMBERINFO,
                 KINOU_MEMBERCHG: TITLE_MEMBERCHG,
                 KINOU_DISCORD: TITLE_DISCORD,
                 }.get(kinou, "Registration Code")

        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                             accounts.DEFAULT_DB))
        try:
            _log_ident_probe(db, q)

            # Who is asking, when that is answerable at all. `known` decides
            # between SE's two forms: password-only, or ID + password.
            prefill, known = _whoami(db)

            # ALREADY AUTHENTICATED? Then step 1 is not a login, it is the first
            # screen of the operation. `page_contents`' "Register a code" button
            # hands kinou 31 the SAME token without a `step`, so this defaulted
            # to 1 and re-asked for the password one screen after checking it --
            # throwing away a session it was still holding. Normalising here
            # rather than fixing that one href covers every caller, and `sess` is
            # truthy only once credentials have actually been verified (an
            # unauthenticated token's state is `{}`).
            if sess and step < 2:
                step = 2

            if (step == 1 and not sess) or (not tok and sess is None):
                # The login form carries a token WE mint. A client-chosen one was
                # accepted here, which meant a guessed `t` could be the key an
                # authenticated session later lands under.
                return page_login(kinou, _store_new(_UCS_SESSION, {}), ret_url,
                                  title, polid=prefill, known=known)

            if step == 2 and not sess:
                if _store_get(_UCS_SESSION, tok) is None:
                    # Expired, or never issued by this process: start over rather
                    # than authenticate into a token we do not know.
                    return page_login(kinou, _store_new(_UCS_SESSION, {}),
                                      ret_url, title, polid=prefill,
                                      known=known)
                # Credential check. `verify_member` fails closed on a suspended
                # member OR a suspended parent PlayOnline ID.
                polid = (q.get("polid") or "").strip()
                row = accounts.verify_member(db, polid, q.get("pw") or "")
                if row is None:
                    log(f"kinou={kinou} auth FAILED for {polid!r} "
                        f"from {self.client_address[0]}")
                    # Always fall back to the FULL form. If the password-only
                    # screen named the wrong account -- the one way identity here
                    # can be wrong -- a second password-only screen would just
                    # fail again forever. Dropping to ID + password makes that
                    # self-healing. The typed ID is echoed rather than our guess:
                    # on a retry it is the thing being corrected.
                    return page_login(kinou, tok, ret_url, title,
                                      error=ERR_5000, polid=polid or prefill,
                                      known=False)
                _UCS_SESSION[tok] = (time.time(),
                                     {"member_id": row["id"],
                                      "polid": row["polid"],
                                      "login": row["login_name"]})
                log(f"kinou={kinou} auth ok for {row['polid']}")

            sess = _store_get(_UCS_SESSION, tok) or None
            if sess is None:
                return page_login(kinou, tok, ret_url, title, polid=prefill,
                                  known=known)

            if kinou == KINOU_CONTENTS:
                return page_contents(tok, ret_url, sess,
                                     accounts.content_ids(db,
                                                          sess["member_id"]))

            if kinou == KINOU_MEMBERINFO:
                mid = sess["member_id"]
                m = db.execute(
                    "SELECT mail_address, last_login_at, prev_login_at, status,"
                    " created_at FROM member WHERE id = ?", (mid,)).fetchone()
                handles = db.execute(
                    "SELECT handle_name, is_primary FROM handle"
                    " WHERE member_id = ? ORDER BY is_primary DESC, id",
                    (mid,)).fetchall()
                # WHICH HANDLE IS THE CLIENT ASKING ABOUT? kinou 1 is the one
                # menu entry that carries `hid=$_HANDLEID`. _log_ident_probe
                # (called above) records whether it resolved; use it only when it
                # names a handle of THIS member, and fall back to the primary
                # otherwise, so a variable that never substitutes costs nothing.
                named = None
                raw = (q.get("hid") or "").strip()
                if raw and not raw.startswith("(") and "Error" not in raw:
                    try:
                        hid = int(raw.split("-")[-1])
                    except ValueError:
                        hid = None
                    if hid is not None:
                        row = (accounts.handle_by_guid(
                                   db, accounts.handle_guid(hid))
                               or accounts.handle_by_client_guid(db, hid)
                               or db.execute("SELECT * FROM handle WHERE id = ?",
                                             (hid,)).fetchone())
                        if row is not None and row["member_id"] == mid:
                            named = row["handle_name"]
                active = accounts.content_ids(db, mid)
                cancelled = accounts.content_ids(db, mid, "inactive")
                names = ", ".join(
                    h["handle_name"] + (" *" if h["is_primary"] else "")
                    for h in handles) or "(none)"
                rows = [("PlayOnline ID", sess["polid"]),
                        ("Handle" + ("s" if len(handles) > 1 else ""), names),
                        ("Mail address", m["mail_address"] or "(not set)"),
                        ("Content IDs", "%d registered%s"
                         % (len(active),
                            ", %d cancelled" % len(cancelled) if cancelled
                            else "")),
                        ("Status", (m["status"] or "active").title()),
                        ("Member since", _short_date(m["created_at"])),
                        ("Last login", _short_date(m["last_login_at"]))]
                note = ("Viewing as %s." % named if named else
                        "* marks the handle your badge uses.")
                return page_memberinfo(tok, ret_url, sess,
                                       {"rows": rows, "note": note})

            if kinou == KINOU_MEMBERCHG:
                return page_memberchange(tok, ret_url, sess)

            if kinou == KINOU_DISCORD:
                # Step 2 shows the link and the code field, step 3 redeems a
                # code, step 4 (the Unlink button) removes the link.
                if discordlink is None:
                    return page_message("PlayOnline", "Discord linking is not "
                                        "available on this server.")
                mid = sess["member_id"]
                ldb = discordlink.connect()
                try:
                    link = discordlink.link_by_member(ldb, mid)
                    if step == 4:
                        discordlink.unlink_member(ldb, mid)
                        log(f"kinou=90 Discord unlinked for {sess['polid']}")
                        _UCS_SESSION.pop(tok, None)
                        return page_discord_done(
                            ret_url, "Discord has been unlinked.",
                            "You will not get PlayOnline DMs any more.")
                    if step < 3:
                        return page_discord(tok, ret_url, sess, link)
                    got = discordlink.redeem(ldb, q.get("code") or "", mid)
                    if got is None:
                        log(f"kinou=90 code refused for {sess['polid']}")
                        return page_discord(
                            tok, ret_url, sess, link,
                            error="That code is not valid, or it has expired.")
                    log(f"kinou=90 {sess['polid']} linked to Discord "
                        f"{got['discord_id']} ({got['discord_name']!r})")
                    _UCS_SESSION.pop(tok, None)
                    return page_discord_done(
                        ret_url, "Linked to Discord as %s."
                        % (got["discord_name"] or "your account"),
                        "New PlayOnline messages will arrive as Discord DMs.")
                finally:
                    ldb.close()

            if kinou == KINOU_MAILADDR:
                row = db.execute("SELECT mail_address FROM member WHERE id = ?",
                                 (sess["member_id"],)).fetchone()
                current = (row["mail_address"] if row else None) or "(not set)"
                if step < 3:
                    return page_mailaddr(tok, ret_url, sess, current)
                local = (q.get("local") or "").strip()
                bad = accounts.check_mail_local(local)
                if bad:
                    return page_mailaddr(tok, ret_url, sess, current, error=bad)
                addr = f"{local}@pol.com"
                # Two mailboxes with one address would make member_by_mail --
                # which is how POP3 resolves the login -- ambiguous.
                if addr != current and accounts.member_by_mail(db, addr):
                    return page_mailaddr(tok, ret_url, sess, current,
                                         error="That mail name is already taken.")
                accounts.assign_mail_address(db, sess["member_id"], local)
                log(f"kinou=5 mail address for {sess['polid']}: "
                    f"{current} -> {addr}")
                _UCS_SESSION.pop(tok, None)
                return page_mailaddr_done(ret_url, addr)

            if kinou == KINOU_MAILPW:
                if step < 3:
                    return page_mailpw(tok, ret_url, sess)
                mp1 = q.get("mp1") or ""
                bad = accounts.check_password_policy(mp1)
                if bad:
                    return page_mailpw(tok, ret_url, sess, error=bad)
                if mp1 != (q.get("mp2") or ""):
                    return page_mailpw(tok, ret_url, sess,
                                       error="The two passwords do not match.")
                accounts.set_mail_password(db, sess["member_id"], mp1)
                log(f"kinou=6 mail password set for {sess['polid']}")
                _UCS_SESSION.pop(tok, None)
                return page_mailpw_done(ret_url)

            if kinou in (KINOU_CANCEL, KINOU_REACTIVATE):
                # Step 2 lists the candidates; step 3 acts on the chosen one.
                # Cancel offers what is active, restore offers what is not --
                # the two screens are the same shape over opposite sets.
                want = "active" if kinou == KINOU_CANCEL else "inactive"
                codes = accounts.content_ids(db, sess["member_id"], want)
                if step < 3:
                    return page_content_pick(kinou, tok, ret_url, sess, codes)
                try:
                    code = int(q.get("code") or "")
                except ValueError:
                    code = -1
                if code not in codes:
                    # Not a title in the set this screen offers: a stale link
                    # (already cancelled in another window), or a hand-typed
                    # code. Re-offer rather than act on it.
                    log(f"kinou={kinou} refused code {code!r} for "
                        f"{sess['polid']} (not {want})")
                    return page_content_pick(
                        kinou, tok, ret_url, sess, codes,
                        error="That Content ID is no longer available here.")
                if kinou == KINOU_CANCEL:
                    accounts.revoke_content(db, sess["member_id"], code)
                else:
                    accounts.grant_content(db, sess["member_id"], code)
                    # A licence cancelled before it was ever placed has no link
                    # to revive, so give it one.
                    accounts.link_member_content_to_primary(db,
                                                            sess["member_id"])
                log(f"kinou={kinou} {'cancelled' if kinou == KINOU_CANCEL else 'restored'}"
                    f" content {code} for {sess['polid']}")
                _UCS_SESSION.pop(tok, None)
                return page_content_done(kinou, ret_url, code)

            if kinou == KINOU_PWCHANGE:
                # Authenticated above (current password verified). Step 2 shows
                # the new-password form; step 3 commits it.
                if step == 2:
                    return page_pwchange(tok, ret_url, sess)
                pw1 = q.get("pw1") or ""
                bad = accounts.check_password_policy(pw1)
                if bad:
                    return page_pwchange(tok, ret_url, sess, error=bad)
                if pw1 != (q.get("pw2") or ""):
                    return page_pwchange(tok, ret_url, sess,
                                         error="The two passwords do not match.")
                accounts.set_member_password(db, sess["login"], pw1)
                log(f"kinou=17 password changed for {sess['polid']}")
                _UCS_SESSION.pop(tok, None)
                return page_pwchange_done(ret_url)

            # kinou 31 -- redeem a registration code.
            if step == 2:
                return page_code(tok, ret_url, sess)
            code = "-".join(q[f"rc{i}"] for i in range(5) if q.get(f"rc{i}"))
            if not code:
                return page_code(tok, ret_url, sess, error=ERR_5103)
            if accounts.check_regcode(db, code) is None:
                # Unknown, or already redeemed. SE distinguishes these (5103 vs
                # 5534) and so do we, but only for a code that EXISTS -- saying
                # "already used" about a code we have never issued would confirm
                # its existence to someone guessing.
                row = db.execute("SELECT redeemed_by FROM regcode"
                                 " WHERE code=? COLLATE NOCASE",
                                 (accounts.normalise_regcode(code),)).fetchone()
                err = ERR_5534 if row is not None else ERR_5103
                log(f"kinou=31 code refused for {sess['polid']} "
                    f"({'used' if row is not None else 'unknown'})")
                return page_code(tok, ret_url, sess, error=err)
            granted = accounts.redeem_regcode(db, code, sess["polid"]) or []
            for c in granted:
                accounts.grant_content(db, sess["member_id"], c)
            accounts.link_member_content_to_primary(db, sess["member_id"])
            log(f"kinou=31 redeemed for {sess['polid']}: granted {granted}")
            _UCS_SESSION.pop(tok, None)
            return page_code_done(ret_url, granted)
        finally:
            db.close()

    def _abs_url(self, path_and_query):
        """An absolute URL back into this servlet, from the Host asked for.

        WARNING: THIS USED TO FORCE `https:` AND THAT WAS THE BUG. The retracted
        reasoning was "for the RDT hop specifically -- the scheme is half of the
        source-URL gate at app.dll 0x4a656f8". The gate is real, but it is
        evaluated on the RDT document's OWN source URL -- the `.rdt` fetch the
        client just made -- and NOT on NEXT-URL, which is nothing but a
        navigation target the client visits afterwards. So forcing https bought
        the gate exactly nothing, and pointed the hop at port 443, which on the
        live topology is `stub.py`'s self-signed TLS listener: the POL-1328 cert
        wall that six live cycles failed to get through.
        Measured 2026-09-06: ucs.log has ZERO step-7 fetches across
        every sign-up ever made, and http.log shows the client never opened a
        connection to :443 either -- it could not follow this URL.

        Behind the SSLv3 terminator this process only ever sees plain HTTP, so
        our own socket cannot answer the question. The Referer can: the client
        sends the URL it came from, and that carries the scheme it is really
        speaking. Fall back to POL_UCS_SCHEME, then to http -- the live route
        (the shim's `signup_http` lever, MODIFICATIONS §A3).
        """
        host = (self.headers.get("Host") or "ucs.pol.com").split(",")[0].strip()
        scheme = urllib.parse.urlparse(
            self.headers.get("Referer") or "").scheme
        if scheme not in ("http", "https"):
            scheme = os.environ.get("POL_UCS_SCHEME", "http")
        return f"{scheme}://{host}{path_and_query}"

    def _finish(self, tok, st):
        """Create the account, then show the player their PlayOnline ID.

        This used to answer with the RDT block directly and leave the ID to
        step 7, on the far side of a hop this side cannot observe. It no longer
        does -- see `page_step6` for why, and for the three real players that
        arrangement stranded. The block is now one button away, at step 7.

        IDEMPOTENT. If the token has already been through here the account is
        NOT created a second time; the same screen is rebuilt, which is what a
        retried fetch of the same URL should get.
        """
        if accounts is None:
            return page_message("PlayOnline", "Account database unavailable.")
        done = st.get("account")
        if done:
            log(f"step 6 re-served for {done['polid']} (account already issued)")
            return page_step6(tok, done["polid"], done["handle"],
                              done["contents"],
                              can_rdt=bool(done.get("pw")) and rdt_representable(
                                  done["pw"], done["mail"], done["polid"]))
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                             accounts.DEFAULT_DB))
        try:
            handle = st.get("handle") or f"Player{secrets.randbelow(9999):04d}"
            pw = st.get("pw1") or secrets.token_hex(8)
            try:
                # ONE transaction for the whole account -- see
                # accounts.register_account. A failure here leaves nothing
                # behind, so the user can fix the problem and try again. No
                # `profile=` any more: we do not collect the contact fields.
                if _signup_open():
                    acct = accounts.register_account(
                        db, handle, pw, contents=_all_content_codes())
                else:
                    acct = accounts.register_account(db, handle, pw,
                                                     code=st.get("code"))
            except accounts.RegistrationError as exc:
                log(f"registration refused: {exc}")
                return page_step4(tok, error=str(exc))
            # The RDT block has to carry a POL-MAIL-PASSWORD -- the client's
            # auto-create refuses without one -- and what it carries is what
            # the player's saved member will present to our POP3/SMTP. So set
            # it, rather than emit a value the mail service would reject: a new
            # account had no mail password at all until kinou 6 was used, and
            # writing a fiction into the client's own store is the failure mode
            # this whole change exists to stop.
            accounts.set_mail_password(db, acct["member_id"], pw)
            log(f"registered {acct['polid']} handle={acct['handle']!r} "
                f"contents={acct['contents']} mail={acct['mail']}")
            st["account"] = {"polid": acct["polid"], "handle": acct["handle"],
                             "contents": acct["contents"], "mail": acct["mail"],
                             "pw": pw, "mail_pw": pw}
            can_rdt = rdt_representable(pw, acct["mail"], acct["polid"])
            if not can_rdt:
                # See RDT_UNREPRESENTABLE: a value here would be truncated by
                # the tokeniser, so the auto-add button is not offered at all
                # rather than provision a member with a cut-short password. The
                # ID screen is the same screen either way. Logged WITHOUT the
                # password, obviously.
                log(f"{acct['polid']}: RDT not offered, a value contains "
                    f"{RDT_UNREPRESENTABLE!r} -- Add Member is the path")
            return page_step6(tok, acct["polid"], acct["handle"],
                              acct["contents"], can_rdt=can_rdt)
        finally:
            db.close()

    def _serve_static(self, path):
        """Serve SE's account-area art out of WWW_DIR.

        Returns True if the request was handled. Scope is deliberately one
        subtree: `..` is rejected outright rather than normalised, and anything
        outside `ucs/img_s/` never reaches the filesystem.
        """
        if not path.startswith("/ucs/img_s/") or ".." in path:
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext not in _MIME:
            return False
        full = os.path.join(WWW_DIR, path.lstrip("/").replace("/", os.sep))
        if not os.path.isfile(full):
            log(f"static MISS {path} (looked in {full})")
            self.send_error(404)
            return True
        with open(full, "rb") as f:
            blob = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME[ext])
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)
        return True

    def do_GET(self):
        try:
            self._log_request()
            path, q = self._query()
            if self._serve_static(path):
                return
            ret_url = q.get("ret_url") or "tologin:"
            kinou = q.get("kinou_id")
            if kinou is None:
                self._send(page_message("PlayOnline",
                                        "No function requested."), 404)
                return
            kinou = int(kinou)
            if kinou in KINOU_ACCOUNT:
                self._send(self._account_step(kinou, q))
                return
            # The probe rides a button whose destination is already a dead end,
            # so a variable that fails to resolve costs nothing that worked.
            # _account_step does its own logging; everything else comes here.
            if IDENT_PROBE and "hid" in q and accounts is not None:
                db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                     accounts.DEFAULT_DB))
                try:
                    _log_ident_probe(db, q)
                finally:
                    db.close()
            if kinou in KINOU_UNAVAILABLE:
                label, reason = KINOU_UNAVAILABLE[kinou]
                self._send(page_kinou_unavailable(kinou, ret_url, label, reason))
                return
            if kinou != KINOU_REGISTER:
                # Everything else the client might ask this CGI for. Logged
                # above, so an unexpected kinou_id shows up as a real data point.
                self._send(page_message(
                    "PlayOnline",
                    f"Function {kinou} is not available on this server."), 404)
                return
            # The wizard's ENTRY request carries no `step` -- app.dll asks for
            # `?kinou_id=20&ret_url=tologin:&area_kbn=&login_pf=&property=&
            # isp_kbn=00` and nothing else. That used to fall through to a
            # `page_register_form` that does not exist, so the client's very
            # first request raised NameError and got the 500 page: the wizard
            # could never open. `_wizard_step` defaults a missing step to 1 and
            # mints the token, so the entry request is just step 1.
            self._send(self._wizard_step(q))
        except Exception:
            log("GET failed:\n" + traceback.format_exc())
            self._send(page_message("PlayOnline", "Server error."), 500)

    def do_POST(self):
        """A posted step: body fields merged over the query, then the wizard.

        We do not know yet whether PML posts at all -- if it does, this is the
        path that finally carries the typed values, and the flow completes for
        free. If it does not, the body is logged verbatim above either way and
        we learn the real shape from the log. Merging body OVER query is the
        right precedence: the URL supplies kinou_id/step/t, the body supplies
        what the user entered.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            self._log_request(body)
            _, q = self._query()
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(
                body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            merged = dict(q)
            merged.update(fields)
            try:
                kinou = int(merged.get("kinou_id") or KINOU_REGISTER)
            except ValueError:
                kinou = KINOU_REGISTER
            if kinou in KINOU_ACCOUNT:
                self._send(self._account_step(kinou, merged))
                return
            self._send(self._wizard_step(merged))
        except Exception:
            log("POST failed:\n" + traceback.format_exc())
            self._send(page_message("PlayOnline", "Server error."), 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # ssl.SSLError is an OSError, which the default implementation would
        # print and move on from. We want handshake refusals called out by name,
        # because with an SSLv3 client that is the single most likely failure.
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            log(f"{client_address[0]} TLS handshake FAILED: {exc}")
        else:
            log(f"{client_address[0]} error: {exc!r}")


def make_cert():
    """Reuse the stub's self-signed cert so both listeners present the same one."""
    cert = os.path.join(LOG_DIR, "stub-cert.pem")
    key = os.path.join(LOG_DIR, "stub-key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    os.makedirs(LOG_DIR, exist_ok=True)
    san = "DNS:*.pol.com,DNS:pol.com,DNS:*.playonline.com,DNS:*.sqex.net"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "3650",
         "-subj", "/CN=*.pol.com", "-addext", f"subjectAltName={san}"],
        check=True)
    log(f"generated self-signed cert {cert}")
    return cert, key


def tls_context():
    """As permissive as the local OpenSSL allows -- see the SSLv3 note up top."""
    cert, key = make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    for attr in ("minimum_version",):
        try:
            setattr(ctx, attr, ssl.TLSVersion.SSLv3)
        except (ValueError, AttributeError):
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except (ValueError, AttributeError):
                pass
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError:
        log("could not lower cipher SECLEVEL; legacy clients may fail")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    log(f"TLS context: min={getattr(ctx, 'minimum_version', '?')} "
        f"(OpenSSL {ssl.OPENSSL_VERSION})")
    return ctx


class _MiniServer:
    """Just enough of a server object for BaseHTTPRequestHandler."""

    def __init__(self, port):
        self.server_name = "ucs.pol.com"
        self.server_port = port


def hexdump(data, limit=512):
    out = []
    for i in range(0, min(len(data), limit), 16):
        c = data[i:i + 16]
        out.append(f"    {i:04x}  {' '.join(f'{b:02x}' for b in c):<47}  "
                   + "".join(chr(b) if 32 <= b < 127 else "." for b in c))
    return "\n".join(out)


def save_capture(name, data):
    d = os.path.join(LOG_DIR, "captures")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{name}-{_stamp().replace(':', '')}.bin")
    with open(p, "wb") as f:
        f.write(data)
    return p


#: SSL 3.0 / TLS cipher suites the Viewer is documented to offer, for readable
#: logging. See Dockerfile.ssl3 -- these are the six it was measured sending.
_SUITES = {
    0x0003: "EXP_RC4_40_MD5", 0x0004: "RC4_128_MD5", 0x0005: "RC4_128_SHA",
    0x0008: "EXP_DES40_CBC_SHA", 0x0009: "DES_CBC_SHA", 0x000a: "3DES_EDE_CBC_SHA",
    0x002f: "AES128_SHA", 0x0035: "AES256_SHA",
}


def describe_hello(b):
    """Parse enough of a ClientHello to say what was actually offered."""
    try:
        if len(b) < 11 or b[0] != 0x16:
            return f"not a handshake record (first byte 0x{b[0]:02x})"
        rec_ver = f"{b[1]}.{b[2]}"
        rec_len = int.from_bytes(b[3:5], "big")
        if b[5] != 0x01:
            return f"record ok but handshake type {b[5]} is not ClientHello"
        hs_len = int.from_bytes(b[6:9], "big")
        cli_ver = f"{b[9]}.{b[10]}"
        i = 11 + 32                       # random
        sid_len = b[i]; i += 1 + sid_len
        cs_len = int.from_bytes(b[i:i + 2], "big"); i += 2
        suites = [int.from_bytes(b[i + j:i + j + 2], "big")
                  for j in range(0, cs_len, 2)]
        i += cs_len
        comp_len = b[i] if i < len(b) else 0
        names = ", ".join(_SUITES.get(s, f"0x{s:04x}") for s in suites)
        have = len(b)
        return (f"ClientHello: record_ver={rec_ver} client_ver={cli_ver} "
                f"record_len={rec_len} handshake_len={hs_len} "
                f"session_id={sid_len}B ciphers={len(suites)} [{names}] "
                f"compression_methods={comp_len} "
                f"| peeked {have}B of {rec_len + 5}B record"
                + ("  <-- TRUNCATED, the rest had not arrived yet"
                   if have < rec_len + 5 else ""))
    except Exception as exc:                                # pragma: no cover
        return f"could not parse hello: {exc!r}"


def _splice(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _passthrough(conn, upstream_host, upstream_ip, port):
    """Relay a non-TLS connection to the real content host, unchanged.

    51305 carries TWO different things: the portal fetches PML from
    wh000.pol.com over PLAIN HTTP on 51300/51304/51305, while the sign-up wizard
    fetches from ucs.pol.com over TLS on 51305. Both hosts resolve to us once
    they are in dns.redirect_only, so owning the port outright would silently
    break portal browsing.

    Splitting on the first byte avoids that: a TLS ClientHello starts 0x16 0x03,
    an HTTP request starts with an ASCII method. TLS is ours; everything else is
    handed straight through to the real portal box, byte for byte.
    """
    try:
        up = socket.create_connection((upstream_ip, port), timeout=15)
    except OSError as exc:
        log(f"passthrough to {upstream_host}[{upstream_ip}]:{port} failed: {exc}")
        conn.close()
        return
    log(f"passthrough -> {upstream_host}[{upstream_ip}]:{port}")
    threading.Thread(target=_splice, args=(conn, up), daemon=True).start()
    threading.Thread(target=_splice, args=(up, conn), daemon=True).start()


def _resolve_passthrough(cfg, port):
    """(host, ip, port) for non-TLS traffic on our port.

    MODE-DEPENDENT, and getting it wrong leaks traffic to SE. In capture mode the
    portal is the REAL wh000, so non-TLS on 51305 must go there. In own-server
    mode the portal is served by our own `login` service, so it must go THERE
    instead -- otherwise carving 51305 quietly sends a slice of portal traffic to
    Square while every other band port is served locally.

    Set POL_UCS_PASSTHROUGH_HOST/_IP/_PORT per mode:
      capture     wh000.pol.com  (pinned 202.67.54.55)  port = ours (51305)
      own-server  login          (the container)        port = its band HTTP port
    """
    proxy = (cfg.get("proxy") or {}) if isinstance(cfg, dict) else {}
    pins = proxy.get("pins") or {}
    host = os.environ.get("POL_UCS_PASSTHROUGH_HOST", "wh000.pol.com")
    ip = os.environ.get("POL_UCS_PASSTHROUGH_IP") or pins.get(host)
    if not ip:
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            ip = None
    up_port = int(os.environ.get("POL_UCS_PASSTHROUGH_PORT") or port)
    return host, ip, up_port


def _route_nontls(conn, addr, portal_host, portal_ip, portal_port):
    """Route a NON-TLS connection on the band port by request path.

    The band port carries two plain-HTTP things at once: the portal fetching PML
    (which must go to `login`), and -- once the client's sign-up wizard is patched
    to use http:// instead of https:// -- the registration CGI request
    (`/pml-cgi-bin/...`, which must go to `ucs-plain`/ucscgi). A blind splice sent
    BOTH to the portal, so registration 404'd. Peek the request line (the client
    is request-first here, exactly as it is for portal PML) and split on the path.

    Fail-safe: if nothing is readable within the timeout, or the line is not a
    /pml-cgi-bin/ request, behave exactly as the old blind passthrough did and
    hand the connection to the portal untouched -- so portal browsing is
    unaffected and a client that says nothing does not hang here.
    """
    cgi_host = os.environ.get("POL_UCS_CGI_HOST", "ucs-plain")
    cgi_port = int(os.environ.get("POL_UCS_CGI_PORT", "8080"))
    path = b""
    try:
        conn.settimeout(float(os.environ.get("POL_UCS_ROUTE_PEEK_TIMEOUT", "6")))
        blob = conn.recv(2048, socket.MSG_PEEK)   # peek: bytes stay for the splice
        first = blob.split(b"\n", 1)[0]
        bits = first.split(b" ")
        if len(bits) >= 2:
            path = bits[1]
    except OSError:
        pass
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    if path.startswith(b"/pml-cgi-bin/"):
        try:
            ip = socket.gethostbyname(cgi_host)
        except OSError as exc:
            log(f"{addr[0]} CGI host {cgi_host} unresolvable ({exc}); -> portal")
            _passthrough(conn, portal_host, portal_ip, portal_port)
            return
        log(f"{addr[0]} non-TLS {path.decode('latin1', 'replace')[:90]} "
            f"-> CGI {cgi_host}[{ip}]:{cgi_port}")
        _passthrough(conn, cgi_host, ip, cgi_port)
    else:
        _passthrough(conn, portal_host, portal_ip, portal_port)


def serve(port, plain=False):
    """Plain mode is a straight HTTP server (for use behind a TLS terminator).
    TLS mode sniffs the first byte and only claims the TLS half of the port."""
    if plain:
        srv = Server(("0.0.0.0", port), Handler)
        log(f"listening on {port} (plain HTTP); "
            f"kinou_id={KINOU_REGISTER} -> registration")
        srv.serve_forever()
        return

    ctx = tls_context()
    cfg = load_config()
    pt_host, pt_ip, pt_port = _resolve_passthrough(cfg, port)
    mini = _MiniServer(port)

    # Optional SSLv3 terminator. See the module docstring: this process's
    # OpenSSL is built `no-ssl3`, so a genuine SSL 3.0 ClientHello can only be
    # answered by a library that still has the protocol compiled in. When
    # POL_UCS_SSL3_HOST is set we stop trying to wrap the socket ourselves and
    # hand the whole TLS connection to that terminator, which decrypts and
    # speaks plain HTTP to the `--plain` twin.
    #
    # The sniff stays HERE rather than letting stunnel own the port outright,
    # because stunnel cannot do it: 51305 carries plaintext portal traffic as
    # well, and that has to keep flowing (see `_passthrough`). So we remain the
    # front door and only the TLS half is relayed onward.
    ssl3_host = os.environ.get("POL_UCS_SSL3_HOST") or None
    ssl3_port = int(os.environ.get("POL_UCS_SSL3_PORT") or 8443)
    if ssl3_host:
        log(f"SSLv3 terminator: TLS on {port} -> {ssl3_host}:{ssl3_port}")

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("0.0.0.0", port))
    lsock.listen(64)
    log(f"listening on {port}; TLS -> registration (kinou_id={KINOU_REGISTER}), "
        f"non-TLS -> passthrough to {pt_host}"
        f"[{pt_ip or 'UNRESOLVED'}]:{pt_port}")

    def worker(conn, addr):
        try:
            head = conn.recv(3, socket.MSG_PEEK)
            if len(head) >= 2 and head[0] == 0x16 and head[1] == 0x03:
                if ssl3_host:
                    try:
                        ip = socket.gethostbyname(ssl3_host)
                    except OSError as exc:
                        log(f"SSLv3 terminator {ssl3_host} unresolvable: {exc}")
                        conn.close()
                        return
                    # DUMP THE CLIENTHELLO. stunnel reports this handshake as
                    # "SSL_accept: Success (0)" with 0 bytes moved in either
                    # direction -- i.e. it never even answered -- while an
                    # openssl s_client over the identical path negotiates fine.
                    # So the interesting difference is in the client's hello,
                    # and inferring it from an error code got us nowhere.
                    # MSG_PEEK, so the bytes stay in the buffer for the relay.
                    try:
                        blob = conn.recv(512, socket.MSG_PEEK)
                        save_capture(f"clienthello-{addr[0]}", blob)
                        log(f"{addr[0]} ClientHello {len(blob)}B:\n"
                            + hexdump(blob, 512))
                        log(f"{addr[0]} " + describe_hello(blob))
                    except OSError as exc:
                        log(f"{addr[0]} could not peek the hello: {exc}")
                    log(f"{addr[0]} TLS (hello {head.hex()}) -> terminator "
                        f"{ssl3_host}[{ip}]:{ssl3_port}")
                    _passthrough(conn, ssl3_host, ip, ssl3_port)
                    return
                try:
                    tls = ctx.wrap_socket(conn, server_side=True)
                except ssl.SSLError as exc:
                    # The single most likely failure: the client speaks genuine
                    # SSLv3 and this OpenSSL cannot. Name it loudly.
                    log(f"{addr[0]} TLS handshake FAILED "
                        f"(client hello {head.hex()}): {exc}")
                    conn.close()
                    return
                Handler(tls, addr, mini)
            elif pt_ip:
                _route_nontls(conn, addr, pt_host, pt_ip, pt_port)
            else:
                log(f"{addr[0]} non-TLS and no passthrough target; dropping")
                conn.close()
        except Exception:
            log("connection failed:\n" + traceback.format_exc())
            try:
                conn.close()
            except OSError:
                pass

    while True:
        conn, addr = lsock.accept()
        threading.Thread(target=worker, args=(conn, addr), daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PlayOnline UCS CGI")
    ap.add_argument("--port", type=int)
    ap.add_argument("--plain", action="store_true",
                    help="serve plain HTTP (use behind a TLS terminator)")
    args = ap.parse_args(argv)
    cfg = load_config()
    ucs = (cfg.get("ucs") or {}) if isinstance(cfg, dict) else {}
    port = args.port or int(os.environ.get("POL_UCS_PORT")
                            or ucs.get("port") or 51305)
    plain = args.plain or os.environ.get("POL_UCS_PLAIN", "0") == "1"
    serve(port, plain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
