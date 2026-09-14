#!/usr/bin/env python3
"""Prove the built-in portal pages parse, list the offered titles, and are
served on both doors when www/ has no file.

    python tools/pmlfallback_test.py

WHY THIS EXISTS. The release ships no portal pages, so a fresh install used to
answer every PML request with an empty document and the Viewer showed a blank
menu with no way to a Play button. `services/pmlfallback.py` synthesizes a
minimal menu instead. Three things can go wrong with that, and each is checked
here from a different side:

  * the page itself. There is no client in a test run, so `services/pmleval.py`
    (the same template evaluator the admin preview uses) is the parser oracle:
    every generated page must run through it with no evaluator failure and
    with NO variable read that the page does not define -- an unresolved name
    renders as a literal error string in the real client. The authoring rules
    that have each cost a day are asserted as bytes: no unbalanced quote in
    either comment form, balanced <if>/<array>/<sheet>, no `&` (unknown
    entities error the page), ASCII, CRLF, no BOM, and the include-size budget.

  * the content. The games list must carry every offered content id with a
    link to that title's page, the title page must carry `gameto:<id>` -- the
    Play control -- exactly once, and the GM Call row must appear only when
    POL_GM_CALL is on, without disturbing the games or the Play flow.

  * the seam. The lobby band (`responders._serve_http_on_lobby`) is the door
    the Viewer actually fetches pages through; the :80 door (`stub.StubHandler`)
    is the other. Both are driven for real, over a socketpair and a loopback
    listener, against an EMPTY www/ -- with the knob on, off, and with a real
    file present, which must win.
"""
import io
import os
import re
import shutil
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "services"))

TMP = tempfile.mkdtemp(prefix="pmlfallback-")
WWW = os.path.join(TMP, "www")
LOGS = os.path.join(TMP, "logs")
os.makedirs(WWW)
os.makedirs(LOGS)
os.environ["POL_WWW_DIR"] = WWW
os.environ["POL_LOG_DIR"] = LOGS
os.environ["POL_CONFIG"] = os.path.join(TMP, "none.yaml")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "unused.db")
os.environ["POL_ACCOUNTS_ENFORCE"] = "0"
os.environ.pop("POL_PML_FALLBACK", None)
os.environ.pop("POL_GM_CALL", None)

import pmleval                                   # noqa: E402
import pmlfallback                               # noqa: E402

ok = True
IDS = [1, 2, 4, 11, 14]                          # the release default list
HOST = "wh000.pol.com"


def check(label, got, want=True):
    global ok
    good = got == want
    ok = ok and good
    print(f"  {'ok  ' if good else 'FAIL'} {label}")
    if not good:
        print(f"       got  {got!r}\n       want {want!r}")


def page(path, **kw):
    return pmlfallback.fallback_page(HOST, path, **kw).decode("ascii")


# --------------------------------------------------------------------------- #
# the authoring rules, as byte checks (PML-AUTHORING section 1, 5, 10)
# --------------------------------------------------------------------------- #
COMMENT = re.compile(r"<!--(.*?)-->|<!(?!--)([^>]*)>", re.S)


def lint(label, raw):
    """The stop-ship checks a page we author must pass."""
    check(f"{label}: ASCII only", all(b < 0x80 for b in raw))
    check(f"{label}: no BOM", not raw.startswith(b"\xef\xbb\xbf"))
    text = raw.decode("ascii")
    check(f"{label}: CRLF line ends only",
          text.count("\n") == text.count("\r\n"))
    check(f"{label}: no entity or bare ampersand", "&" not in text)
    bad = [m.start() for m in COMMENT.finditer(text)
           if ((m.group(1) or m.group(2) or "").count("'") % 2
               or (m.group(1) or m.group(2) or "").count('"') % 2)]
    check(f"{label}: no unbalanced quote in a comment", bad, [])
    for tag in ("if", "array", "sheet", "text", "pml", "head", "body"):
        o = len(re.findall(rf"<{tag}[\s>]", text))
        c = len(re.findall(rf"</{tag}>", text))
        check(f"{label}: <{tag}> balanced ({o}/{c})", o == c)
    check(f"{label}: under the 19 KiB include budget", len(raw) < 19 * 1024)


def evaluate(label, text, sysvars=None):
    """Run the page through the evaluator; it must not fail and must read no
    variable it did not define. Returns (flattened text, variables)."""
    V = pmleval._Vars(pmleval.DEFAULT_SYSVARS)
    if sysvars:
        V.update(sysvars)
    ctx = {"resolve": None, "depth": 0, "base": None, "unresolved": set()}
    out = []
    try:
        root = pmleval._parse(text)
        pmleval._walk(root["children"], V, out, ctx)
        failed = ""
    except Exception as exc:                           # noqa: BLE001
        failed = repr(exc)
    check(f"{label}: evaluator ran clean", failed, "")
    check(f"{label}: no undefined variable read", sorted(V.missing), [])
    flat = "".join(out)
    check(f"{label}: no include left for the client to chase",
          "<include" in flat, False)
    return flat, V


# --------------------------------------------------------------------------- #
print("the main menu")
os.environ["POL_LOBBY_CONTENT_IDS"] = ",".join(str(i) for i in IDS)
raw = pmlfallback.fallback_page(HOST, "/pml/main/index.pml")
lint("main", raw)
main, _ = evaluate("main", raw.decode("ascii"))
for cid in IDS:
    if cid == pmlfallback.FRIEND_LIST_ID:
        continue
    href = f'href="/pml/game/{pmlfallback.game_dir(cid)}/index.pml"'
    check(f"main lists content {cid} with a link to its page", href in main)
    check(f"main names content {cid}",
          pmlfallback.title_of(cid) in main)
check("content 14 is the Friend List, not a page", 'href="navigatorto:"' in main)
check("exactly one Friend List row", main.count('href="navigatorto:"'), 1)
check("a Log Out row", 'href="tologout:"' in main)
check("no Play control on the main menu (it lives on the title page)",
      "gameto:" in main, False)
check("no GM Call row by default", "gmcallto:" in main, False)
check("the title tag", "<title>Main Menu</title>" in main)
check("ids 1..3 use the stock directories",
      [pmlfallback.game_dir(i) for i in (1, 2, 3, 4, 11)],
      ["ff11", "tetra", "jan", "0004", "0011"])
check("the environment list is the default source",
      pmlfallback.offered_ids(), IDS)
check("a bad id in the list is skipped, not fatal",
      pmlfallback.offered_ids(["1", "x", "0", "2000", "2", "2"]), [1, 2])
del os.environ["POL_LOBBY_CONTENT_IDS"]
check("with no environment the release default list applies",
      pmlfallback.offered_ids(), IDS)

print()
print("the GM Call knob: only the one row changes")
games_re = re.compile(r'href="(/pml/game/[^"]+|gameto:\d+|navigatorto:|tologout:)"')
off = page("/pml/main/index.pml", content_ids=IDS, gm_call="0")
on = page("/pml/main/index.pml", content_ids=IDS, gm_call="1")
check("POL_GM_CALL=0: no GM Call", "gmcallto:" in off, False)
check("POL_GM_CALL=1: one GM Call row", on.count('href="gmcallto:"'), 1)
check("POL_GM_CALL=1: the row is labelled", ">GM Call<" in on)
check("the games, Friend List and Log Out links are identical either way",
      games_re.findall(on), games_re.findall(off))
os.environ["POL_GM_CALL"] = "1"
check("the knob is read from the environment",
      "gmcallto:" in page("/pml/main/index.pml", content_ids=IDS))
os.environ["POL_GM_CALL"] = "0"
check("and 0 turns it off again",
      "gmcallto:" in page("/pml/main/index.pml", content_ids=IDS), False)
del os.environ["POL_GM_CALL"]
lint("main+gm", on.encode("ascii"))
evaluate("main+gm", on)
ok_gm = page("/pml/game/0011/index.pml", gm_call="1")
check("a title page is the same with the knob on",
      ok_gm, page("/pml/game/0011/index.pml", gm_call="0"))

print()
print("the title pages: Play targets the right content")
for cid in (1, 2, 3, 4, 10, 11, 15, 99):
    d = pmlfallback.game_dir(cid)
    raw = pmlfallback.fallback_page(HOST, f"/pml/game/{d}/index.pml")
    lint(f"game {cid}", raw)
    flat, _ = evaluate(f"game {cid}", raw.decode("ascii"))
    check(f"game {cid}: one Play control, gameto:{cid}",
          flat.count(f'href="gameto:{cid}"'), 1)
    check(f"game {cid}: no other launch target",
          len(re.findall(r'gameto:\d+', flat)), 1)
    check(f"game {cid}: offers Play and Back only (no Content ID button)",
          f'href="/pml/game/{d}/contentid.pml"' not in flat)
    check(f"game {cid}: Back returns with toviewer:",
          'href="toviewer:"' in flat)
    check(f"game {cid}: the title is named",
          pmlfallback.title_of(cid) in flat)
    cid_raw = pmlfallback.fallback_page(HOST, f"/pml/game/{d}/contentid.pml")
    lint(f"contentid {cid}", cid_raw)
    cflat, _ = evaluate(f"contentid {cid}", cid_raw.decode("ascii"))
    check(f"contentid {cid}: states the code", f"content {cid:04d}" in cflat)
    check(f"contentid {cid}: Back goes to the title page",
          f'href="/pml/game/{d}/index.pml"' in cflat)
check("a query string is ignored on the way in",
      page("/pml/game/0011/index.pml?SC=0&PF=WIN"),
      page("/pml/game/0011/index.pml"))
check("the stock directory spelling maps to the id",
      'href="gameto:1"' in page("/pml/game/ff11/index.pml"))
check("the four-digit spelling of the same id maps too",
      'href="gameto:1"' in page("/pml/game/0001/index.pml"))
check("an unknown title stays reachable and gets a generic name",
      "Content 99" in page("/pml/game/0099/index.pml"))

print()
print("the menu DATA fragment for a stock main page")
raw = pmlfallback.fallback_page(HOST, "/pml/main/in01.pml", content_ids=IDS,
                                gm_call="0")
lint("in01", raw)
_flat, V = evaluate("in01", raw.decode("ascii"))
m1 = V.get("arMenu1")
check("$arMenu1 is a row list ending in the # sentinel",
      bool(m1) and m1[-1] == ["#"])
check("$arMenu1 opens with Games and Navigator",
      [r[0] for r in m1[:2]], ["Games", "Navigator"])
check("$mnMax1 is the LAST index, as the stock loop computes it",
      V.get("mnMax1"), len(m1) - 2)
check("$arMenu2Length has one count per level-1 entry",
      len(V.get("arMenu2Length") or []), len(m1) - 1)
games = V.get("arMenu2")[0]
check("$arMenu2[0] carries every offered game by content id",
      [r[3] for r in games[:-1]],
      [str(i) for i in IDS if i != pmlfallback.FRIEND_LIST_ID])
check("each game row links its page as a quoted expression",
      all(r[1] == f"'/pml/game/{pmlfallback.game_dir(int(r[3]))}/index.pml'"
          for r in games[:-1]))
check("$arMenu2Length[0] is the game count",
      V["arMenu2Length"][0], str(len(games) - 1))
check("$mnNum2Max is the longest submenu",
      V.get("mnNum2Max"), len(games) - 1)
check("$arMenu2Rec has mnNum2Max+1 blank rows",
      len(V.get("arMenu2Rec") or []), V["mnNum2Max"] + 1)
check("$arShortcutBt has its row (an empty array is the known trap)",
      len(V.get("arShortcutBt") or []), 1)
for name in ("txLogin", "txNews", "txNoNews", "txInfo", "txSc", "txGm",
             "txMenu2ClsBt", "txMenu2ClsBtAh", "arLPG", "txLPG"):
    check(f"${name} is defined for the stock page", name in V)
check("no GM Call level-1 entry by default",
      any(r[0] == "GM Call" for r in m1), False)
raw_gm = pmlfallback.fallback_page(HOST, "/pml/main/in01.pml", content_ids=IDS,
                                   gm_call="1")
_f, Vg = evaluate("in01+gm", raw_gm.decode("ascii"))
check("POL_GM_CALL=1 adds one level-1 GM Call entry",
      [r for r in Vg["arMenu1"] if r[0] == "GM Call"],
      [["GM Call", "^03Call a game master.", "gmcallto:", ""]])
check("and leaves the games submenu unchanged", Vg["arMenu2"][0], games)
check("$mnMax1 tracks the extra entry", Vg["mnMax1"], V["mnMax1"] + 1)
check("$arMenu2Length grew by one zero",
      Vg["arMenu2Length"], V["arMenu2Length"] + ["0"])

print()
print("the other includes a stock main page fetches on load")
ad = page("/pml/main/ad01.pml")
_f, Va = evaluate("ad01", ad)
check("ad01: $adBanner ends at the # row", Va.get("adBanner"), [["#"]])
news = page("/pcd/ntool/en-US/latestnews.pml")
_f, Vn = evaluate("latestnews", news)
check("latestnews: an empty feed with a zero count",
      (Vn.get("LATESTNEWS"), Vn.get("LATESTNEWSMAX")), ([], 0))
check("latestnews: the same shape for any language",
      page("/pcd/ntool/de-DE/latestnews.pml"), news)
data = page("/pcd/mainmenu/en-US/data.pml")
check("mainmenu data.pml: an empty fragment, not a page", data, "<!-- -->\r\n")
lint("ad01", ad.encode("ascii"))
lint("latestnews", news.encode("ascii"))

print()
print("everything else that ends in .pml")
na = page("/pml/help/manual/index.pml")
lint("unavailable", na.encode("ascii"))
evaluate("unavailable", na)
check("an unknown page says so and offers toviewer:",
      "Page not available" in na and 'href="toviewer:"' in na)
sub = page("/pml/game/0011/guide.pml")
check("an unknown title sub-page goes Back to that title",
      'href="/pml/game/0011/index.pml"' in sub)
check("a fragment name under a title dir is an empty fragment",
      page("/pml/game/0011/style.pml"), "<!-- -->\r\n")
check("an unknown fragment name is an empty fragment",
      page("/pml/info/in04.pml"), "<!-- -->\r\n")
check("a non-PML path is not ours",
      pmlfallback.fallback_page(HOST, "/pml/main/ma_i/masc00i.png"), None)
check("a bare path with no extension is not ours",
      pmlfallback.fallback_page(HOST, "/"), None)
check("the escaper strips markup, quotes, ampersands and non-ASCII",
      pmlfallback.esc('A & B <c> "d" e’s é'), "A B c d es")

# --------------------------------------------------------------------------- #
print()
print("the seam: the lobby band, which is the door the Viewer uses")
import responders                                # noqa: E402


def band_get(path, extra=b""):
    # The realm host's /pml/ paths are digest-challenged once per connection
    # unless the request already carries Authorization; the handler keys on
    # the header's PRESENCE (it cannot check the digest, see its own note),
    # which is what a real client sends after its first 401.
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\n"
           f"Accept: text/x-playonline-pml\r\n"
           f"Authorization: x-MD5-pol username=\"t\", response=\"0\"\r\n"
           f"Connection: close\r\n").encode() + extra + b"\r\n"
    a, b = socket.socketpair()
    out = {}

    def serve():
        try:
            responders._serve_http_on_lobby(b, b"", "192.0.2.5:5000", 51300)
        except Exception as e:                       # noqa: BLE001
            out["err"] = repr(e)
        try:
            b.close()
        except OSError:
            pass
    t = threading.Thread(target=serve)
    t.start()
    a.sendall(req)
    a.shutdown(socket.SHUT_WR)
    a.settimeout(15)
    resp = b""
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            resp += d
    except OSError:
        pass
    t.join(15)
    a.close()
    head, _, body = resp.partition(b"\r\n\r\n")
    return head, body, out


check("responders imported pmlfallback",
      getattr(responders, "pmlfallback", None) is not None)
head, body, out = band_get("/pml/main/index.pml")
check("no exception escaped the handler", "err" not in out, True)
check("band: GET main menu -> 200", head.startswith(b"HTTP/1.1 200 OK"))
check("band: PML content type",
      b"Content-Type: text/x-playonline-pml\r\n" in head)
check("band: no cached lifetime for a synthesized page",
      b"Cache-Control: no-cache" in head)
check("band: the body IS the built-in menu",
      body, pmlfallback.fallback_page(HOST, "/pml/main/index.pml"))
check("band: Content-Length matches",
      re.search(rb"Content-Length: (\d+)", head).group(1), str(len(body)).encode())
head, body, _ = band_get("/pml/game/0011/index.pml?SC=0&PF=WIN")
check("band: a title page with a query string -> its Play control",
      b'href="gameto:11"' in body)
os.environ["POL_PML_FALLBACK"] = "0"
head, body, _ = band_get("/pml/main/index.pml")
check("band: POL_PML_FALLBACK=0 -> the old empty document",
      (head.startswith(b"HTTP/1.1 200"), body), (True, b"<!-- -->\r\n"))
del os.environ["POL_PML_FALLBACK"]
real = os.path.join(WWW, HOST, "pml", "main", "index.pml")
os.makedirs(os.path.dirname(real))
io.open(real, "w", newline="").write("<pml><body>REAL PAGE</body></pml>\r\n")
head, body, _ = band_get("/pml/main/index.pml")
check("band: a real file under www/ wins over the built-in page",
      body, b"<pml><body>REAL PAGE</body></pml>\r\n")
check("band: and a real file carries validators, as before",
      b"ETag:" in head and b"Last-Modified:" in head)
head, body, _ = band_get("/pml/main/in01.pml")
check("band: its missing include is still synthesized",
      b'<array name="$arMenu1">' in body)
os.remove(real)
log_text = io.open(os.path.join(LOGS, "lobby.log"), encoding="utf-8",
                   errors="replace").read()
check("band: the serve is logged with the agreed phrase",
      "[http] fallback page for /pml/main/index.pml" in log_text)

print()
print("the seam: the :80 door")
import stub                                      # noqa: E402
import http.client                               # noqa: E402
from http.server import ThreadingHTTPServer      # noqa: E402

stub.WWW_DIR = WWW
stub.LOG_DIR = LOGS
srv = ThreadingHTTPServer(("127.0.0.1", 0), stub.StubHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def door_get(path, accept="text/x-playonline-pml"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = {"Host": HOST}
    if accept:
        hdrs["Accept"] = accept
    c.request("GET", path, headers=hdrs)
    r = c.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, data


check("stub imported pmlfallback", getattr(stub, "pmlfallback", None) is not None)
st, hd, data = door_get("/pml/main/index.pml")
check(":80: GET main menu -> 200", st, 200)
check(":80: PML content type", hd.get("content-type"), "text/x-playonline-pml")
check(":80: no cached lifetime", hd.get("cache-control"), "no-cache")
check(":80: the body IS the built-in menu",
      data, pmlfallback.fallback_page(HOST, "/pml/main/index.pml"))
st, hd, data = door_get("/pml/game/ff11/index.pml?SC=1&PF=WIN")
check(":80: a title page -> its Play control", b'href="gameto:1"' in data)
os.environ["POL_PML_FALLBACK"] = "0"
st, hd, data = door_get("/pml/main/index.pml")
check(":80: POL_PML_FALLBACK=0 -> the old empty document",
      (st, data), (200, b"<!-- -->\r\n"))
del os.environ["POL_PML_FALLBACK"]
io.open(real, "w", newline="").write("<pml><body>REAL PAGE</body></pml>\r\n")
st, hd, data = door_get("/pml/main/index.pml")
check(":80: a real file under www/ wins",
      data, b"<pml><body>REAL PAGE</body></pml>\r\n")
# An image fetch carries no PML Accept; with one, the pre-existing empty-document
# branch answers (its own documented behaviour, unchanged here).
st, hd, data = door_get("/pml/main/ma_i/masc00i.png", accept="image/png")
check(":80: a missing non-PML asset is still a 404", st, 404)
srv.shutdown()
srv.server_close()
log_text = io.open(os.path.join(LOGS, "http.log"), encoding="utf-8",
                   errors="replace").read()
check(":80: the serve is logged with the agreed phrase",
      "[http] fallback page for /pml/main/index.pml" in log_text)

# Close cached log handles before the temp dir goes (Windows cannot delete an
# open file, and TemporaryDirectory-style cleanup then reports it as a failure).
import srvcore                                   # noqa: E402
for fh in list(getattr(srvcore, "_LOG_FILES", {}).values()):
    try:
        fh.close()
    except OSError:
        pass
getattr(srvcore, "_LOG_FILES", {}).clear()
for fh in list(getattr(stub, "_LOG_HANDLES", {}).values()):
    try:
        fh.close()
    except (OSError, AttributeError):
        pass

print()
print("pmlfallback_test: OK" if ok else "pmlfallback_test: FAILED")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
