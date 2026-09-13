#!/usr/bin/env python3
"""PlayOnline admin dashboard -- a small local web panel.

Runs beside the server (localhost only; it can mint codes and accounts, so it is
NOT for player exposure). Three jobs today:

  1. Codes & accounts over the SAME accounts.db the login/ucs services use
     (`accounts.issue_regcode`, `grant_content`, `delete_polid`, listing) -- so an
     operator can create registration codes, grant content and delete an account
     without a sqlite shell.

  2. Authoring server NEWS -- the login-screen ticker, SE's real Information
     section, and a detail page per story. The panel edits the announcement
     store and publishes every PML the Viewer reads through `newsgen.py`, which
     `tools/gen_news.py` also uses, so the two front ends cannot drift. This is
     the ONLY part of the panel that writes into the served tree, and it is
     confined to newsgen's path allowlist.

  3. A PML editor with a client-accurate live PREVIEW. The registration pages are
     hand-authored PML that only rendered in the Viewer until now, so every layout
     fix cost a full client round-trip. The preview renders the same PML the way
     the Viewer lays it out (absolute pos/size in a 640-wide stage, styles as
     fonts/colors, inputs as boxes, art from www/ucs.pol.com), so pages can be
     iterated here first. It can also pull a LIVE wizard page straight from ucscgi
     into the editor.

Stdlib only (matches the other services). Serves its single-page UI from
`services/admin_web/`, art from `/www`, and a small JSON API under `/api/`.

    POL_ADMIN_PORT        default 8090
    POL_ACCOUNTS_DB       default /data/accounts.db
    POL_ADMIN_ART_ROOT    default /www/ucs.pol.com        (serves /art/*)
    POL_ADMIN_CGI         default http://ucs-plain:8080   (live-page proxy)
    POL_ADMIN_USER        default admin       } break-glass override only; the
    POL_ADMIN_PASSWORD    default empty       } normal credential is in the DB
    POL_ADMIN_SESSION_TTL default 43200 (12h)
"""
import collections
import http.cookies
import json
import os
import posixpath
import random
import re
import secrets
import string
import sys
import threading
import time
import urllib.parse   # explicit: `urllib.request` only exposes it by side effect
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts  # noqa: E402
import contentlist  # noqa: E402  -- the shared content-id title table
import newsgen    # noqa: E402  -- announcements store + every news PML we write
import pmleval    # noqa: E402  -- template evaluator for the preview
import pmlrefs    # noqa: E402  -- which pages reference which files
#: The shared /data every container mounts. Computed before the two GM modules
#: are imported because each reads its directory from the environment at import
#: time, and their container-shaped defaults are wrong when the panel is run from
#: a checkout.
_DATA = os.environ.get("POL_DATA_DIR", "/data")
os.environ.setdefault("POL_GMCHAT_SPOOL", os.path.join(_DATA, "gm-chat"))
os.environ.setdefault("POL_GMD_TICKET_DIR", os.path.join(_DATA, "gm-calls"))
import gmchat  # noqa: E402  -- the GM chat record language and its spool
#: WARNING: IMPORTED FOR ITS RULES, NOT ITS SERVER. `gmd` owns the precedence between
#: an operator's pin, POL_GMD_QUEUE and the live queue depth, and re-deriving
#: that here is how the panel ends up telling an operator something the caller
#: was never told. Importing it binds no socket -- `serve()` does that.
import gmd  # noqa: E402

try:
    # TESTER ISSUE REPORTS. Imported for its LAYOUT, the same way gmd is
    # imported for its rules: `issuereport` owns where a bundle lives, what its
    # manifest holds and how a file is safely addressed inside it, and the panel
    # must not re-derive any of that. Re-deriving a path is how the abuse-report
    # directory and this one nearly ended up sharing POL_REPORT_DIR.
    #
    # WARNING: It reads LOG_DIR from srvcore, i.e. `/logs` -- which the admin container
    # already mounts in both stacks (`${POL_ROOT}/logs:/logs`). That is checked,
    # not assumed: needing a new mount would mean a hand `docker compose up
    # --force-recreate` on prod rather than a plain git-sync deploy.
    import issuereport  # noqa: E402
except ImportError:                            # pragma: no cover
    issuereport = None

try:
    # The scrambled form of a POL ID -- what the client actually puts on the
    # NICK line, and so the string the account holder types to sign in. Shown
    # beside a new account's ID because they are NOT the same value, and handing
    # over only the ID has stranded people before. Optional import: accounts.py
    # guards it the same way, so the panel must not harden a soft dependency.
    import polnick  # noqa: E402
except ImportError:                            # pragma: no cover
    polnick = None

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "admin_web")
DB = os.environ.get("POL_ACCOUNTS_DB", "/data/accounts.db")
#: Where responders.py files parsed abuse reports. Same default as its own
#: POL_REPORT_DIR, so the two agree without either importing the other.
REPORT_DIR = os.environ.get(
    "POL_REPORT_DIR", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                   "reports"))
#: Where the `gmd` service files decoded GM Call tickets (POL_GMD_TICKET_DIR).
#: This was gmserver.exe on the HOST while the only way to the cipher was mapping
#: polcore.dll; `gmcrypt.py` reimplemented it, so gmd is now an ordinary compose
#: service and the tickets land in the same /data every container mounts. The
#: default below therefore has to keep agreeing with gmd's own.
GM_CALL_DIR = os.environ.get(
    "POL_GM_CALL_DIR", os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                                    "gm-calls"))
ART_ROOT = os.environ.get("POL_ADMIN_ART_ROOT", "/www/ucs.pol.com")
CGI = os.environ.get("POL_ADMIN_CGI", "http://ucs-plain:8080")

# Operator auth. The credential lives in accounts.db (see accounts.admin_cred)
# so the Security tab can change it at run time; the environment pair below is
# now the BREAK-GLASS override, kept valid alongside the stored one so a
# forgotten password cannot lock the operator out of their own panel.
#
# With neither set the panel is OPEN -- its first-run state, safe only on the
# loopback bind, since it mints codes and accounts.
#
# Sessions are cookies held in THIS PROCESS only. A restart logs everyone out,
# which for a single-operator panel beats persisting session state; it also
# means "restart the container" is a hard revoke. Basic auth was replaced
# outright because it has no logout: browsers replay cached credentials for the
# origin, so changing the password under Basic left the old one wedged in the
# browser with no server-side way to clear it.
ADMIN_USER = os.environ.get("POL_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("POL_ADMIN_PASSWORD", "")
SESSION_COOKIE = "poladmin"
SESSION_TTL = float(os.environ.get("POL_ADMIN_SESSION_TTL", 12 * 3600))
#: Failed logins per client address before that address is locked out. The panel
#: may be bound to a tailnet IP, where a password is the only thing in the way.
LOCKOUT_AFTER = 10
LOCKOUT_WINDOW = 300.0
WWW_ROOT = os.environ.get("POL_ADMIN_WWW", "/www")

# pmlus text cipher (worker-out/pmlcodec.py::decode_text, inlined so the admin
# container needs no extra mount). Most portal pages under www/wh000.pol.com are
# stored encrypted exactly as SE served them; this reverses it for the editor.
_M32 = 1 << 32


def _decode_text(buf):
    """Reverse the pml text cipher. Returns None if the buffer is not encrypted."""
    n = len(buf)
    b = bytearray(buf)
    z = b.find(0)
    if z < 0:
        return None                                  # no NUL -> already plaintext
    t = z & 0xFF
    mul = (4 * t + (0x100003 if (t & 1) == 0 else 0x100001)) & 0xFFFFFFFF
    state = pow(mul, n + 1, _M32)
    for i in range(n):
        if i == z:
            b[i] = 0x3E                              # '>'
        else:
            k = (state >> 24) & 0xFF
            if b[i] != k:
                b[i] ^= k
            state = (state * mul) % _M32
    return bytes(b)


#: Decode order for a page's text. SE's Japanese pages are cp932 and there is
#: nothing in the file that says so, so it has to be tried -- UTF-8 first
#: because it is strict enough to reject cp932 (a lone 0x81 is not a valid
#: UTF-8 start byte), and latin-1 last because it accepts anything and so can
#: only ever be the fallback.
_TEXT_ENCODINGS = ("utf-8", "cp932", "latin-1")


def _decode_str(data, label):
    for enc in _TEXT_ENCODINGS:
        try:
            return data.decode(enc), f"{label}/{enc}"
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace"), f"{label}/latin-1"


def _load_pml_text(fs_path):
    """Read a server .pml and return (text, kind): decodes UTF-16LE, plaintext,
    or the pmlus cipher, so the editor can open ANY server PML.

    The plaintext branch used to decode UTF-8 with `errors="replace"`, which
    silently turned every Japanese page in the mirror into a wall of U+FFFD --
    `wh000.pol.com/pml/event/ev11/evpm01.pml` is cp932 and previewed as
    `FFXI...` followed by 60 replacement characters. Nothing was wrong with the
    file; the ladder just never had a rung for it.
    """
    data = open(fs_path, "rb").read()
    if data[:2] == b"\xff\xfe" or (len(data) >= 2 and data[0] != 0 and data[1] == 0):
        return data.decode("utf-16le", "replace"), "utf-16le"
    if data[:3] == b"\xef\xbb\xbf":                  # a UTF-8 BOM is not content
        data = data[3:]
    # "starts with markup" means the first NON-BLANK byte is `<`. Testing byte 0
    # alone filed 381 perfectly ordinary pages that open with a newline or a tab
    # under "unknown", which reads as a decode nobody trusts.
    if data.lstrip()[:1] == b"<":
        return _decode_str(data, "plaintext")
    dec = _decode_text(data)
    if dec is not None:
        return _decode_str(dec, "pmlus-cipher")
    return _decode_str(data, "unknown")

#: Content-code legend for the UI. `contentlist.CONTENT_TITLES` is the one place
#: these are spelled for a reader -- this used to be a third, independent copy of
#: the same table, and it was missing Janhourou (3), which therefore showed as a
#: bare code in the panel while the registration pages named it.
CONTENT_NAMES = dict(contentlist.CONTENT_TITLES)

_MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
    ".gif": "image/gif", ".ang": "application/octet-stream",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
}


#: Cached .pml file listing (walking the bind mount is slow). Warmed at startup.
_PML_CACHE = {"files": None, "shapes": {}, "graph": None, "at": 0.0}
_PML_LOCK = threading.Lock()          # guards the cache dict
_PML_SCAN_LOCK = threading.Lock()     # admits one scanner at a time
_PML_REFRESHING = threading.Event()
_PML_TTL = 300.0

#: What kind of thing a .pml is. Only ~1 file in 20 is a PAGE: the mirror is
#: mostly the pieces pages are BUILT from, and an operator clicking down the
#: browser hits those twenty times more often than a page. Naming the kind is
#: the difference between "the preview is broken" and "this file is a variable
#: table" -- which is the report that prompted this: `pcd/.../anews.pml` drew a
#: black stage, correctly, and said nothing about why.
PML_SHAPES = {
    "page":    "a whole screen -- has a <body>",
    "layout":  "positioned markup, but no <body>: a page includes this",
    "content": "text records a page pulls into a <textbox>",
    "data":    "no markup: variables and arrays another page reads",
}


def _pml_shape(text):
    """Classify a decoded .pml. See PML_SHAPES. Cheap: substring tests only.

    Comments come off FIRST. SE comments markup out and leaves it in place --
    `pml/magazine/box/read04/rdin05.pml` is one <sheet> and nothing else, all of
    it inside a `<!--` -- and calling that "layout" promises a drawing that can
    never appear. A commented-out `<body>` would misfile a whole page the same
    way."""
    low = pmleval._strip_comments(text).lower()
    if "<body" in low:
        return "page"
    if "<record" in low or "<data " in low:
        return "content"
    if re.search(r"<(sheet|scrollarea|text|img|input|textbox|systembg)\b", low):
        return "layout"
    return "data"


def _pml_scan(root):
    files, shapes, texts = [], {}, {}
    for dirpath, _dirs, names in os.walk(root):
        for fn in names:
            if not fn.lower().endswith((".pml", ".pcb")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            files.append(rel)
            try:
                texts[rel] = _load_pml_text(full)[0]
            except Exception:
                texts[rel] = ""
            shapes[rel] = _pml_shape(texts[rel])
    files.sort()

    # The reference graph, off the same decode -- reading 3,499 files twice to
    # build it separately would double the only expensive part of the scan.
    graph = pmlrefs.build(_Site(root, texts), files)
    return files, shapes, graph


class _Site:
    """The mirror, as `pmlrefs` needs to see it: paths in, www-relative paths
    out. Everything here is already how the preview resolves a page -- the
    reference graph is that same resolution run over every file at once."""

    def __init__(self, root, texts):
        self.root = root
        self.texts = texts
        self._roots = {}          # _pml_roots stats the disk; one call per file

    def _rel(self, abs_path):
        return os.path.relpath(abs_path, self.root).replace(os.sep, "/")

    def _abs_roots(self, rel):
        if rel not in self._roots:
            self._roots[rel] = _pml_roots(rel)
        return self._roots[rel]

    def text(self, rel):
        return pmleval._strip_comments(self.texts.get(rel) or "")

    def roots(self, rel):
        hosts, trees, start = self._abs_roots(rel)
        return ([self._rel(h) for h in hosts], [self._rel(t) for t in trees],
                self._rel(start))

    def resolve(self, rel, src):
        hosts, trees, start = self._abs_roots(rel)
        got = _pml_resolve(src, start, hosts, trees)
        return self._rel(got) if got else None

    def expand(self, rel):
        """(expanded text, the includes that actually resolved).

        The include targets come from the resolver rather than the output --
        `pmleval` inlines an include and emits no trace of it, so this is the
        only place the edge is visible."""
        hosts, trees, start = self._abs_roots(rel)
        hit = []

        def resolve(src, base):
            got = _pml_resolve(src, base or start, hosts, trees)
            if not got:
                return None
            hit.append(self._rel(got))
            try:
                return _load_pml_text(got)[0], os.path.dirname(got)
            except Exception:
                return None

        out = pmleval.expand(self.texts.get(rel) or "",
                             resolve_include=resolve, base=start)
        return out, hit


def _pml_files(force=False):
    """The listing, with each file's shape. Never blocks on a refresh.

    Classifying means decoding all 3,499 files (~13s on top of a ~16s walk over
    the bind mount). That is fine in the startup warm thread, but a request that
    lands after the TTL must not wear it -- so a stale list is served as-is and
    the refresh runs behind it.
    """
    with _PML_LOCK:
        cached, shapes = _PML_CACHE["files"], _PML_CACHE["shapes"]
        if cached is not None and not force:
            if time.time() - _PML_CACHE["at"] >= _PML_TTL \
                    and not _PML_REFRESHING.is_set():
                _PML_REFRESHING.set()
                threading.Thread(target=_pml_refresh, daemon=True).start()
            return cached, shapes        # stale is fine; a request never waits
    return _pml_refresh(force)


def _pml_refresh(force=False):
    """Scan and cache. One scanner at a time: the startup warm and a first
    request can arrive together, and scanning twice in parallel over the bind
    mount is slower than either alone."""
    try:
        with _PML_SCAN_LOCK:
            with _PML_LOCK:              # somebody may have finished while we waited
                if _PML_CACHE["files"] is not None and not force \
                        and time.time() - _PML_CACHE["at"] < _PML_TTL:
                    return _PML_CACHE["files"], _PML_CACHE["shapes"]
            files, shapes, graph = _pml_scan(os.path.abspath(WWW_ROOT))
            with _PML_LOCK:
                _PML_CACHE.update(files=files, shapes=shapes, graph=graph,
                                  at=time.time())
            return files, shapes
    finally:
        _PML_REFRESHING.clear()


def _pml_roots(rel_path):
    """(host_dirs, tree_dirs, start_dir) for a www-relative page path.

    The mirror is NOT one flat host tree. Pages live under `<host>/...` but also
    under `_lang/<locale>/<host>/...` and `_eras/<era>/<host>/...`, so the host
    is the first DIRECTORY segment that looks like one (it has a dot), not
    segment 0. Rooting at segment 0 pointed every `_lang`/`_eras` include at
    `www/_lang/`, where nothing resolves -- and that is 3,291 of the 3,499 files
    the browser indexes.

    `_lang`/`_eras` are OVERLAYS, not copies: `_lang/en-US/wh000.pol.com/` holds
    only `pml2/`, and its pages still include `/pml/pml_s/path/cs/cont1.pml`
    from the base tree. So the host and tree roots are LISTS, most specific
    first, ending at `www/<host>/` -- the same fallthrough `responders`
    `_portal_roots` gives a live client.

    Three kinds of root, because PML writes three kinds of src, and they mean
    different places. `wh000.pol.com/pml/pml_s/path/cs/cont1.pml` -- the path
    file for the `pml2/cs/` pages -- states both absolute forms at once:

        <define name="$C_PATH1" value="/pml2/cs/">        host-absolute
        <define name="$F_PATH2" value="file:/img_s/general/">   file:

    and only `<host>/pml/img_s/general/` exists, so `file:` is rooted at the
    host's `pml/` tree even for a page that lives in `pml2/`. Hence:

        ../cont1.pml        relative to the page      -> start_dir
        /pml2/cs/x.pml      absolute at the host      -> host_dirs
        file:/cs/x.pml      absolute at the `pml` tree -> tree_dirs

    Falls back to the www root for a path that names no host, which keeps a
    hand-pasted page (no file behind it) working rather than erroring.
    """
    root = os.path.abspath(WWW_ROOT)
    segs = [x for x in (rel_path or "").replace("\\", "/").split("/")
            if x and x not in (".", "..")]
    dirs = segs[:-1]                       # the last segment is the file itself
    host_i = next((i for i, x in enumerate(dirs) if "." in x), None)
    if host_i is None:
        return [root], [root], root
    hosts = [os.path.join(root, *dirs[:host_i + 1])]
    if host_i:                             # an overlay -- the base tree is under it
        hosts.append(os.path.join(root, dirs[host_i]))
    # `pml` first (that is where `file:` points), then the tree the page itself
    # lives in (`pml2`, `pcd`, ... -- for hosts that have no `pml` at all), then
    # the host root.
    own = dirs[host_i + 1] if len(dirs) > host_i + 1 else None
    host_dirs, tree_dirs = [], []
    for h in hosts:
        h = os.path.abspath(h)
        if not h.startswith(root + os.sep) or not os.path.isdir(h):
            continue
        host_dirs.append(h)
        for name in ("pml", own):
            t = os.path.join(h, name) if name else h
            if os.path.isdir(t) and t not in tree_dirs:
                tree_dirs.append(t)
        if h not in tree_dirs:
            tree_dirs.append(h)
    start_dir = os.path.abspath(os.path.join(root, *dirs))
    if not host_dirs or not os.path.isdir(start_dir):
        return [root], [root], root
    return host_dirs, tree_dirs or host_dirs, start_dir


def _pml_resolve(src, base, host_dirs, tree_dirs):
    """Absolute path for a PML `src`, or None. Never leaves a host root.

    `..` is ALLOWED here -- `<include src="../cont1.pml">` is how most pages
    reach the file that defines their `$*_PATH*` variables, so refusing it took
    out the whole page below it. Containment is enforced on the RESULT instead,
    which is the check that was actually wanted.
    """
    s = (src or "").strip()
    if not s or "$" in s:
        return None                        # an expression nothing could evaluate
    stripped = re.sub(r"^file:/*", "", s)
    if stripped != s:                      # file:/X -- absolute in the PML tree
        cands = [os.path.join(t, stripped) for t in tree_dirs]
    elif s.startswith("/"):                # /X -- absolute at the host
        cands = [os.path.join(h, s.lstrip("/")) for h in host_dirs]
    else:                                  # relative to the including file
        cands = ([os.path.join(base, s)]
                 + [os.path.join(t, s) for t in tree_dirs]
                 + [os.path.join(h, s) for h in host_dirs])
    for cand in cands:
        fp = os.path.abspath(cand)
        if any(fp == h or fp.startswith(h + os.sep) for h in host_dirs) \
                and os.path.isfile(fp):
            return fp
    return None


def _db():
    return accounts.connect(DB)


# --------------------------------------------------------------------------- #
# operator sessions
# --------------------------------------------------------------------------- #
_SESSIONS = {}                      # token -> {"user": str, "expires": float}
_SESSION_LOCK = threading.Lock()
_FAILS = {}                         # client addr -> {"n": int, "first": float}
_FAIL_LOCK = threading.Lock()


def _ct_eq(a, b):
    """Constant-time compare that tolerates non-ASCII (compare_digest won't)."""
    return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def _session_new(user):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _SESSION_LOCK:
        for t, s in list(_SESSIONS.items()):          # opportunistic sweep
            if s["expires"] <= now:
                del _SESSIONS[t]
        _SESSIONS[tok] = {"user": user, "expires": now + SESSION_TTL}
    return tok


def _session_get(tok):
    """The live session for `tok`, sliding its expiry, or None."""
    if not tok:
        return None
    now = time.time()
    with _SESSION_LOCK:
        s = _SESSIONS.get(tok)
        if not s:
            return None
        if s["expires"] <= now:
            del _SESSIONS[tok]
            return None
        s["expires"] = now + SESSION_TTL
        return dict(s)


def _session_drop(tok):
    with _SESSION_LOCK:
        _SESSIONS.pop(tok, None)


def _sessions_clear():
    """Revoke every session. Run after a credential change, so a session minted
    under the OLD password cannot outlive it."""
    with _SESSION_LOCK:
        _SESSIONS.clear()


def _stored_cred():
    """The stored credential row, or None. One short-lived DB connection."""
    db = _db()
    try:
        return accounts.get_admin_cred(db)
    except Exception:
        return None
    finally:
        db.close()


def _auth_required():
    """True when any credential is configured -- stored row or env override."""
    return bool(ADMIN_PASSWORD) or _stored_cred() is not None


def _verify_login(user, pw):
    """Check (user, pw) against the stored credential AND the env override.

    Both are tried in full rather than short-circuiting, so the work done is the
    same whichever one matches. Returns the accepted username, or None.
    """
    ok = None
    row = _stored_cred()
    if row is not None and _ct_eq(user, row["username"]) \
            and accounts.check_password(pw, row["pw_hash"], row["pw_salt"]):
        ok = row["username"]
    # Break-glass: a non-empty POL_ADMIN_PASSWORD always logs in, which is the
    # documented way back after a forgotten password (set it, restart, log in,
    # change the stored one, clear it again).
    if ADMIN_PASSWORD and _ct_eq(user, ADMIN_USER) and _ct_eq(pw, ADMIN_PASSWORD):
        ok = ADMIN_USER
    return ok


def _lockout_left(addr):
    """Seconds this address must wait before it may try again (0 = may try)."""
    with _FAIL_LOCK:
        f = _FAILS.get(addr)
        if not f:
            return 0.0
        left = LOCKOUT_WINDOW - (time.time() - f["first"])
        if left <= 0:
            del _FAILS[addr]
            return 0.0
        return left if f["n"] >= LOCKOUT_AFTER else 0.0


def _note_fail(addr):
    now = time.time()
    with _FAIL_LOCK:
        f = _FAILS.get(addr)
        if not f or now - f["first"] > LOCKOUT_WINDOW:
            _FAILS[addr] = {"n": 1, "first": now}
        else:
            f["n"] += 1


def _clear_fails(addr):
    with _FAIL_LOCK:
        _FAILS.pop(addr, None)


def _www_writable():
    """Can we actually publish? Probed, not assumed.

    `os.access(W_OK)` is not enough on a Docker read-only bind: it consults the
    file mode, which says yes, and the mount then refuses the write anyway. So
    actually create and remove a file. Cached, because the News tab asks on
    every load and a bind mount does not change under a running container.
    """
    if _WRITABLE_CACHE:
        return _WRITABLE_CACHE[0]
    ok = False
    try:
        probe = os.path.join(WWW_ROOT, ".poladmin-write-probe")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        ok = True
    except OSError:
        ok = False
    _WRITABLE_CACHE.append(ok)
    return ok


#: One-element list so the probe above can memoise without a `global`.
_WRITABLE_CACHE = []


def _rand_code(groups=5, glen=4):
    # Unambiguous alphabet (no O/0/I/1/L) so a spoken/printed code is unmistakable.
    alpha = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(random.choice(alpha) for _ in range(glen))
                    for _ in range(groups))


#: NICK-blob head+pad signatures we have actually observed, mapped to a name
#: for the panel. MEASURED, not decoded -- an unknown signature is shown raw
#: rather than guessed at, because inventing a platform name for bytes we have
#: not read is how a wrong reading gets written down as a fact.
_CLIENT_SIGS = {
    "TTTTTAISTTTTTTTTTTTTT": "PC",
    "TTTTTAITTTTTTTTTTTTTT": "PC",
    "TTTTT7ITTTGaItbIQ8nHA": "PS2",
}


def _client_label(sig):
    return _CLIENT_SIGS.get(sig, sig)


def _content_label(codes_csv):
    out = []
    for c in str(codes_csv or "").split(","):
        c = c.strip()
        if not c:
            continue
        try:
            out.append(contentlist.content_title(int(c)))
        except ValueError:
            out.append(c)
    return ", ".join(out)


class Handler(BaseHTTPRequestHandler):
    server_version = "poladmin"

    def log_message(self, *a):
        pass

    # -- helpers ------------------------------------------------------------- #
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        m = jar.get(SESSION_COOKIE)
        return m.value if m else None

    def _set_cookie(self, tok):
        """Session cookie headers. HttpOnly so page script can't read the token;
        SameSite=Strict so another origin can't ride the session. NOT Secure --
        the panel is plain http on a loopback/tailnet bind, and a Secure cookie
        would simply never be stored."""
        if tok is None:
            return [("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; "
                                   "HttpOnly; SameSite=Strict")]
        return [("Set-Cookie", f"{SESSION_COOKIE}={tok}; Path=/; "
                               f"Max-Age={int(SESSION_TTL)}; HttpOnly; "
                               "SameSite=Strict")]

    def _session(self):
        return _session_get(self._cookie())

    def _authed(self, path):
        """Gate every request. True = may proceed.

        A valid session short-circuits, so an authenticated browse costs no DB
        work; only an unauthenticated request pays a lookup to find out whether
        a credential exists at all. No credential = open (first run).

        On failure an /api/ path gets 401 JSON, and anything else gets the login
        page -- a redirect would lose the URL the operator typed, and Basic's
        401 challenge is gone along with Basic.
        """
        if self._session():
            return True
        if not _auth_required():
            return True
        if path.startswith("/api/"):
            self._send(401, {"error": "not signed in", "auth": True})
        else:
            self._file(os.path.join(WEB, "login.html"))
        return False

    # -- auth endpoints ------------------------------------------------------ #
    def _session_info(self):
        """Unauthenticated on purpose: the login page asks this first.

        Says only whether a login is NEEDED and whether this caller has one --
        the username is returned only to a caller that already has a session.
        """
        s = self._session()
        out = {"auth_required": bool(ADMIN_PASSWORD) or _stored_cred() is not None,
               "authenticated": bool(s),
               "user": s["user"] if s else None}
        if s:
            row = _stored_cred()
            out["credential_set"] = row is not None
            out["updated_at"] = row["updated_at"] if row else None
            out["env_override"] = bool(ADMIN_PASSWORD)
        self._send(200, out)

    def _login(self):
        addr = self.client_address[0] if self.client_address else "?"
        left = _lockout_left(addr)
        if left:
            return self._send(429, {"error": f"Too many failed attempts. Try "
                                             f"again in {int(left) + 1}s."})
        body = self._json_body()
        user = str(body.get("username") or "")
        pw = str(body.get("password") or "")
        who = _verify_login(user, pw)
        if not who:
            _note_fail(addr)
            # Deliberately vague: which half was wrong is not the caller's
            # business, and a slow reply blunts online guessing.
            time.sleep(0.5)
            return self._send(401, {"error": "Incorrect username or password."})
        _clear_fails(addr)
        tok = _session_new(who)
        self._send(200, {"ok": True, "user": who}, headers=self._set_cookie(tok))

    def _logout(self):
        _session_drop(self._cookie())
        self._send(200, {"ok": True}, headers=self._set_cookie(None))

    def _set_credentials(self):
        """Change the operator username/password. Requires the CURRENT password
        even though the caller already holds a session -- a walked-up-to browser
        should not be able to take the panel over silently.

        Exception: when no credential exists yet the panel is open, so there is
        no current password to demand; that is the first-run set.
        """
        body = self._json_body()
        current = str(body.get("current") or "")
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        s = self._session()
        if _auth_required():
            if not s:
                return self._send(401, {"error": "not signed in"})
            if not _verify_login(s["user"], current):
                addr = self.client_address[0] if self.client_address else "?"
                _note_fail(addr)
                time.sleep(0.5)
                return self._send(403, {"error": "Current password is wrong."})
        db = _db()
        try:
            username = accounts.set_admin_cred(db, username, password)
            db.commit()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        finally:
            db.close()
        # Every existing session was minted under the old credential, so drop
        # them all -- then hand THIS caller a fresh one, so the operator who
        # just changed the password is not bounced to the login page.
        _sessions_clear()
        tok = _session_new(username)
        print(f"[admin] operator credential changed; user={username!r}, "
              f"all sessions revoked", flush=True)
        self._send(200, {"ok": True, "user": username,
                         "env_override": bool(ADMIN_PASSWORD)},
                   headers=self._set_cookie(tok))

    # -- routing ------------------------------------------------------------- #
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/session":
            return self._session_info()          # unauthenticated by design
        if not self._authed(path):
            return
        if path == "/":
            return self._file(os.path.join(WEB, "index.html"))
        if path.startswith("/art/"):
            return self._art(path[len("/art/"):])
        if path.startswith("/static/"):
            return self._file(os.path.join(WEB, path[len("/static/"):].lstrip("/")))
        if path == "/api/codes":
            return self._codes_list()
        if path == "/api/accounts":
            return self._accounts_list()
        if path == "/api/account-footprint":
            return self._account_footprint()
        if path == "/api/content-names":
            return self._send(200, CONTENT_NAMES)
        if path == "/api/page":
            return self._live_page()
        if path == "/api/reports":
            return self._reports_list()
        if path == "/api/issues":
            return self._issues_list()
        if path == "/api/issue-file":
            return self._issue_file()
        if path == "/api/gm-calls":
            return self._gm_calls_list()
        if path == "/api/gm-desk":
            return self._gm_desk()
        if path == "/api/pml-list":
            return self._pml_list()
        if path == "/api/pml-load":
            return self._pml_load()
        if path == "/api/pml-refs":
            return self._pml_refs()
        if path == "/api/news":
            return self._news_state()
        if path == "/api/news/outputs":
            return self._news_outputs()
        if path == "/api/news/preview":
            return self._news_preview()
        if path == "/api/news/art":
            return self._news_art()
        return self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._logout()
        if not self._authed(path):
            return
        if path == "/api/credentials":
            return self._set_credentials()
        if path == "/api/codes":
            return self._codes_create()
        if path == "/api/codes/random":
            return self._send(200, {"code": _rand_code()})
        if path == "/api/pml-expand":
            return self._pml_expand()
        if path == "/api/grant":
            return self._grant()
        if path == "/api/revoke":
            return self._revoke()
        if path == "/api/gm-control":
            return self._gm_control()
        if path == "/api/gm-say":
            return self._gm_say()
        if path == "/api/account-create":
            return self._account_create()
        if path == "/api/account-password":
            return self._account_password()
        if path == "/api/account-delete":
            return self._account_delete()
        if path == "/api/news":
            return self._news_save()
        if path == "/api/news/publish":
            return self._news_publish()
        return self._send(404, {"error": "not found"})

    # -- static / art -------------------------------------------------------- #
    def _file(self, fs_path):
        fs_path = os.path.normpath(fs_path)
        if not fs_path.startswith(WEB) or not os.path.isfile(fs_path):
            return self._send(404, "not found", "text/plain")
        ext = os.path.splitext(fs_path)[1].lower()
        with open(fs_path, "rb") as f:
            body = f.read()
        # The UI is bind-mounted and edited live; never let the browser cache it,
        # so an old app.js can't linger against a new index.html (empty dropdowns).
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _art(self, rel):
        """Serve a page's art. `?base=<www-relative page path>` says WHICH tree.

        A PML `file:/help/img_s/x.png` is relative to the page's own host tree
        (`www/wh000.pol.com/pml/`), not to ART_ROOT -- which is `ucs.pol.com`,
        the wizard pages' art. Resolving everything under ART_ROOT alone 404'd
        all of the portal's art, so `_pml_roots` supplies the page's trees
        first. ART_ROOT stays last, and is the whole answer for a request with
        no base: a pasted page, or a generated wizard step.
        """
        from urllib.parse import urlsplit, parse_qs
        base = (parse_qs(urlsplit(self.path).query).get("base") or [""])[0]
        rel = posixpath.normpath("/" + rel).lstrip("/")   # defeat ../ traversal
        roots = []
        if base:
            host_dirs, tree_dirs, _start = _pml_roots(base)
            roots += tree_dirs + host_dirs      # overlay first, then the base tree
        roots.append(ART_ROOT)
        for root in roots:
            fs_path = os.path.abspath(os.path.join(root, rel))
            if fs_path.startswith(os.path.abspath(root)) \
                    and os.path.isfile(fs_path):
                ext = os.path.splitext(fs_path)[1].lower()
                with open(fs_path, "rb") as f:
                    return self._send(200, f.read(),
                                      _MIME.get(ext, "application/octet-stream"))
        self._send(404, "not found", "text/plain")

    # -- codes --------------------------------------------------------------- #
    def _codes_list(self):
        db = _db()
        try:
            rows = db.execute(
                "SELECT code, contents, note, created_at, redeemed_at, "
                "redeemed_by FROM regcode ORDER BY created_at DESC").fetchall()
        finally:
            db.close()
        out = [{"code": r["code"], "contents": r["contents"],
                "contents_label": _content_label(r["contents"]),
                "note": r["note"], "created_at": r["created_at"],
                "redeemed_at": r["redeemed_at"], "redeemed_by": r["redeemed_by"]}
               for r in rows]
        self._send(200, out)

    def _codes_create(self):
        body = self._json_body()
        code = (body.get("code") or "").strip() or _rand_code()
        contents = body.get("contents") or [1]
        if isinstance(contents, str):
            contents = [int(c) for c in contents.replace(" ", "").split(",") if c]
        note = body.get("note") or None
        db = _db()
        try:
            existing = db.execute("SELECT 1 FROM regcode"
                                  " WHERE code=? COLLATE NOCASE",
                                  (accounts.normalise_regcode(code),)).fetchone()
            if existing:
                return self._send(409, {"error": "that code already exists"})
            accounts.issue_regcode(db, code, contents=tuple(contents), note=note)
            db.commit()
        finally:
            db.close()
        self._send(200, {"code": accounts.normalise_regcode(code),
                         "contents": contents, "note": note})

    # -- abuse reports ------------------------------------------------------- #
    #
    # The Viewer's "Report User" dialog submits a real SMTP mail to SE's own
    # `tos@..playonline.com`. We used to accept it and drop it as off-domain, so
    # the whole path ended nowhere. It is now aliased into a local mailbox AND
    # filed here as parsed JSON -- see `_archive_report` in responders.py for the
    # form's wire format. This reads the files; nothing here parses mail.
    def _reports_list(self):
        out = []
        try:
            names = sorted(os.listdir(REPORT_DIR), reverse=True)
        except OSError:
            names = []                       # no reports yet is not an error
        for name in names[:500]:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(REPORT_DIR, name), encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue                     # a torn file must not hide the rest
            f = rec.get("fields") or {}
            out.append({
                "id": name[:-len(".json")],
                "received_at": rec.get("received_at"),
                "from": rec.get("from"),
                "subject": rec.get("subject"),
                "suspect": f.get("suspect"),
                "application": f.get("application"),
                "explanation": f.get("explanation"),
                "contact": f.get("sender"),
                "log": f.get("log"),
            })
        self._send(200, out)

    # -- tester issue reports ------------------------------------------------ #
    #
    # Filed by the shim's report chord and landed by services/issuereport.py,
    # which also cuts OUR logs to the same minutes. This only renders them.
    #
    # DELIBERATELY SEPARATE FROM THE "Reports" TAB ABOVE. That one is SE's own
    # Report User dialog -- a player accusing another player, arriving by SMTP.
    # This one is a tester telling us the software broke. They share a word and
    # nothing else, and merging them would bury the rare one under the common
    # one at exactly the moment somebody is looking for it.
    def _issues_list(self):
        if issuereport is None:
            return self._send(200, {"error": "issuereport module not loaded"})
        self._send(200, issuereport.list_reports())

    #: Which extensions the panel will hand back inline, and as what. Anything
    #: not named here is served as PLAIN TEXT rather than guessed at: these are
    #: bytes a client uploaded, and the one thing we must not do is let an
    #: uploader pick the Content-Type the panel renders them under (an .html in
    #: a bundle rendered as html would be script running inside the session).
    _ISSUE_CTYPES = {".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg"}

    def _issue_file(self):
        """One file out of one bundle: /api/issue-file?id=<id>&f=client/shot.png

        Both components go through `issuereport`'s own sanitiser -- the id is a
        path component and `f` is a fixed two-part shape -- so a traversal is
        neutralised at the module that owns the layout, not here."""
        if issuereport is None:
            return self._send(404, {"error": "issuereport module not loaded"})
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                  if "?" in self.path else "")
        rid = (q.get("id") or [""])[0]
        rel = (q.get("f") or [""])[0]
        blob = issuereport.read_file(rid, rel)
        if blob is None:
            return self._send(404, {"error": "no such file in that bundle"})
        ext = os.path.splitext(rel)[1].lower()
        ctype = self._ISSUE_CTYPES.get(ext, "text/plain; charset=utf-8")
        # X-Content-Type-Options because the fallback is text/plain and a
        # sniffing browser would otherwise be free to reinterpret it.
        self._send(200, blob, ctype,
                   headers=(("X-Content-Type-Options", "nosniff"),))

    # -- GM calls ------------------------------------------------------------ #
    #
    # Filed by pol-shim's gmserver.exe from the 0x102 the client sends when the
    # GM Call form is submitted. Measured layout: content id at body +0x04,
    # issue at +0x06, handle at +0x40, subject at +0x50, body at +0x90.
    def _gm_calls_list(self):
        out = []
        try:
            names = sorted(os.listdir(GM_CALL_DIR), reverse=True)
        except OSError:
            names = []                       # none filed yet is not an error
        for name in names[:500]:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(GM_CALL_DIR, name), encoding="utf-8",
                          errors="replace") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue                     # a torn file must not hide the rest
            cid = rec.get("content_id")
            # _content_label takes a CSV STRING, not a list -- passing a list
            # stringifies to "[2]" and shows that on screen.
            rec["content_label"] = _content_label(cid if cid is not None else "")
            rec["id"] = name[:-len(".json")]
            out.append(rec)
        self._send(200, out)

    # -- the GM desk: queue control, and the room ----------------------------- #
    #
    # WHAT IS AND IS NOT WIRED, because the difference matters when reading this
    # screen and it is easy to over-read:
    #
    #   * the QUEUE and the two screen flags are fully measured (queue = 0x801
    #     body +0x02, Join = flags 0x40, Start = 0x20, all confirmed live
    #     2026-08-16) and are genuinely controlled from here;
    #   * a chat line really is delivered -- authserv relays it and logs it;
    #   * but NOTHING WE SEND HAS EVER BEEN SEEN TO RENDER. FACTS
    #     section "GM CHAT, THE ROOM ITSELF" has the open problem: a relayed
    #     speaker is never admitted to the client's member table, and a 'T' takes
    #     its speaker from there. So this console's job is as much to make that
    #     iterable -- raw records, a nick override, and a transcript that keeps
    #     the client's OWN bytes -- as it is to hold a conversation.
    def _gm_state(self):
        """Everything the desk screen shows, read fresh. Never raises."""
        ctl = gmd.read_control()
        flags, why = gmd.effective_flags(ctl)
        serving = {}
        try:
            with open(gmd.SERVING_PATH, encoding="utf-8") as f:
                serving = json.load(f)
        except (OSError, ValueError):
            pass
        sessions = []
        try:
            with open(os.path.join(GM_CALL_DIR, "gm-sessions.json"),
                      encoding="utf-8") as f:
                sessions = json.load(f)
        except (OSError, ValueError):
            pass
        now = time.time()
        callers = []
        for sess in sessions if isinstance(sessions, list) else []:
            age = now - (sess.get("at") or 0)
            callers.append({
                "peer": ":".join(str(x) for x in (sess.get("peer") or [])),
                "request_no": sess.get("req_no") or 0,
                "ready": bool(sess.get("ready")),
                "idle": int(age),
                # gmd prunes on the same IDLE_S, so a row older than that is a
                # session it would no longer count -- shown, but not as live.
                "live": age <= gmd.IDLE_S,
            })
        return ctl, {
            # What gmd LAST TOLD A CALLER, versus what it would say now. They
            # differ until the next 0x801 poll, and an operator who flips the
            # toggle and sees nothing move on the client needs to know that the
            # client asks on ITS schedule -- the panel is not the slow part.
            "serving": serving,
            "flags": flags, "flags_why": why,
            "join": bool(flags & 0x40), "start": bool(flags & 0x20),
            "on_duty": gmd.on_duty(ctl),
            "on_duty_until": ctl.get("on_duty_until"),
            # None = never set, so the screen can say "nobody has said" rather
            # than claiming a GM has signed off when none ever signed on.
            "duty": ctl.get("duty"),
            "pinned_flags": ctl.get("flags"),
            "pinned_queue": ctl.get("queue"),
            "by": ctl.get("by"),
            "env_flags": gmd.STATUS_FLAGS,
            "env_queue": gmd.QUEUE_OVERRIDE or None,
            "flags_on_duty": gmd.FLAGS_ON_DUTY,
            "flags_off_duty": gmd.FLAGS_OFF_DUTY,
            "callers": callers,
            "waiting": sum(1 for c in callers if c["live"] and c["request_no"]),
            "rooms": gmchat.rooms(),
            "spool": gmchat.SPOOL,
            "gm_nick": gmchat.GM_NICK.decode("latin1"),
            "t_head": gmchat.T_HEAD.decode("latin1"),
            "prefix": gmchat.PREFIX.decode("latin1"),
        }

    def _gm_room_param(self, given=None):
        """The room to act on: what was asked for, else what gmd is handing out,
        else the newest room that has a transcript."""
        if given:
            return given
        st = {}
        try:
            with open(gmd.SERVING_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        rooms = gmchat.rooms()
        return st.get("room") or (rooms[0] if rooms else "")

    def _gm_desk(self):
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(self.path).query)
        room = self._gm_room_param((q.get("room") or [""])[0].strip())
        _ctl, out = self._gm_state()
        out["room"] = room
        if room:
            try:
                limit = max(1, min(1000, int((q.get("limit") or ["200"])[0])))
            except ValueError:
                limit = 200
            rb = room.encode()
            gmchat.trim(rb)
            out["transcript"] = gmchat.transcript(rb, limit)
            out["pending"] = gmchat.pending(rb)
        else:
            out["transcript"], out["pending"] = [], 0
        self._send(200, out)

    #: How long one "I am at the desk" claim is good for. The panel renews it
    #: while the tab is open, so this is only ever reached by WALKING AWAY --
    #: which is the case it exists for. A sticky on-duty flag is worse than none:
    #: it invites a caller into a room with nobody in it.
    DUTY_TTL = int(os.environ.get("POL_ADMIN_GM_DUTY_TTL", "180"))

    def _gm_control(self):
        """Set (or take back) what gmd tells callers.

        Every field is tri-state on purpose: absent = leave alone, null = clear
        the override, a value = pin it. Without the clear, the panel would be a
        one-way door -- an operator could pin the queue to 9 and have no way back
        short of editing a file inside a container.
        """
        body = self._json_body()
        ctl = gmd.read_control()
        changed = []
        if "on_duty" in body:
            # TRI-STATE, like the two pins below and for the same reason: null
            # hands the decision back to POL_GMD_STATUS_FLAGS. Without it the
            # first operator ever to touch this panel takes the flags away from
            # compose permanently, which is a one-way door -- and on a stack that
            # runs unattended, "nobody has said anything" is the correct resting
            # state, not "a GM signed off".
            if body["on_duty"] is None:
                ctl["duty"], ctl["on_duty_until"] = None, None
                changed.append("duty cleared (back to POL_GMD_STATUS_FLAGS)")
            else:
                ctl["duty"] = bool(body["on_duty"])
                ctl["on_duty_until"] = (time.time() + self.DUTY_TTL
                                        if ctl["duty"] else None)
                changed.append("on duty" if ctl["duty"] else "off duty")
        elif body.get("renew"):
            # The panel's heartbeat. It EXTENDS a claim and never creates one:
            # a renew that could turn duty on would mean a stale tab left open
            # in another window quietly re-opens the desk after the operator
            # deliberately closed it.
            if gmd.on_duty(ctl):
                ctl["on_duty_until"] = time.time() + self.DUTY_TTL
        for key in ("flags", "queue"):
            if key not in body:
                continue
            if body[key] is None or body[key] == "":
                ctl[key] = None
                changed.append(f"{key} pin cleared")
                continue
            try:
                val = int(str(body[key]), 0)
            except (TypeError, ValueError):
                return self._send(400, {"error": f"{key} must be a number"})
            if key == "queue" and not (0 <= val <= 0xFFFF):
                return self._send(400, {"error": "queue must be 0-65535"})
            if key == "flags" and not (0 <= val <= 0xFFFFFFFF):
                return self._send(400, {"error": "flags must fit in 32 bits"})
            ctl[key] = val
            changed.append(f"{key} = {val}")
        ctl["by"] = (self._session() or {}).get("user") or "operator"
        ctl["at"] = time.time()
        try:
            gmd.write_control(ctl)
        except OSError as exc:
            return self._send(500, {"error": f"could not write the desk: {exc}"})
        if changed:
            print(f"[admin] {ctl['by']} set the GM desk: {', '.join(changed)}",
                  flush=True)
        _c, out = self._gm_state()
        out["changed"] = changed
        # gmd reads the file when the NEXT 0x801 arrives, so this is a statement
        # of intent, not of what any caller has been told yet.
        out["note"] = ("saved -- callers see it on their next queue poll"
                       if changed else "renewed")
        self._send(200, out)

    def _gm_say(self):
        """Put one record into a room's spool.

        `raw` is not a debug leftover: the 'T' and 'U' encoders are written
        against the client's PARSERS with no surviving SE capture to check them
        against, so the shape may simply be wrong -- and when it is, the fix has
        to cost a line in this box rather than a rebuild. Same reason `nick` is
        exposed, including the literal "self", which attributes a record to the
        RECEIVING client's own nick and so separates "the record is malformed"
        from "the speaker does not resolve in the member table".
        """
        body = self._json_body()
        room = self._gm_room_param((body.get("room") or "").strip())
        if not room:
            return self._send(400, {"error": "no room -- is gmd running?"})
        rb = room.encode()
        if not gmchat.is_gm_room(rb):
            return self._send(400, {
                "error": f"{room} does not start with "
                         f"{gmchat.PREFIX.decode()!r}, so authserv will not "
                         f"treat it as a GM room"})
        raw, say, event = body.get("raw"), body.get("say"), body.get("event")
        try:
            if raw:
                # \xNN escapes, so any byte is reachable from a text field.
                rec = (raw.encode("cp932", "replace")
                          .decode("unicode_escape").encode("latin1"))
            elif event:
                rec = gmchat.encode_event(event, body.get("who") or "GM")
            elif say:
                rec = gmchat.encode_text(say, head=(
                    body["head"].encode() if body.get("head") else None))
            else:
                return self._send(400, {"error": "one of say / event / raw"})
        except (UnicodeError, ValueError) as exc:
            return self._send(400, {"error": f"could not build the record: {exc}"})
        if not rec:
            return self._send(400, {"error": "that is an empty record"})
        nick = (body.get("nick") or "").strip()
        try:
            gmchat.spool(rb, rec, nick=nick.encode() if nick else None)
        except OSError as exc:
            return self._send(500, {"error": f"could not spool: {exc}"})
        who = (self._session() or {}).get("user") or "open-panel"
        print(f"[admin] {who} spooled to {room}: {rec[:60]!r}"
              + (f" as {nick}" if nick else ""), flush=True)
        # SPOOLED, not delivered, and certainly not rendered. `pending` is the
        # honest read: it only drops when a session that is IN that room comes
        # round the relay loop.
        self._send(200, {"ok": True, "room": room,
                         "record": rec.decode("latin1", "replace"),
                         "hex": rec.hex(),
                         "reads_as": gmchat.describe(rec),
                         "pending": gmchat.pending(rb)})

    # -- accounts ------------------------------------------------------------ #
    def _accounts_list(self):
        db = _db()
        try:
            rows = db.execute("SELECT polid, created_at FROM polid "
                              "ORDER BY created_at DESC LIMIT 500").fetchall()
            out = []
            for r in rows:
                pol = r["polid"]
                mem = db.execute("SELECT id FROM member WHERE polid=?",
                                 (pol,)).fetchone()
                contents = []
                linked = []
                handle = None
                if mem:
                    cs = db.execute("SELECT content_code FROM content WHERE "
                                    "member_id=? AND status='active'",
                                    (mem["id"],)).fetchall()
                    contents = [c["content_code"] for c in cs]
                    h = db.execute("SELECT id, handle_name FROM handle WHERE "
                                   "member_id=? ORDER BY id LIMIT 1",
                                   (mem["id"],)).fetchone()
                    handle = h["handle_name"] if h else None
                    # A GRANT IS NOT A LICENCE UNTIL IT IS LINKED TO A HANDLE.
                    # Lobby 1:3 builds the launcher's character table out of
                    # `handle_content` alone, so an account can own a title on
                    # this screen and still be told "You have no Content ID for
                    # <game>" (reported live 2026-08-16). Shown here so the two
                    # can never silently disagree again.
                    if h:
                        linked = [c["content_code"] for c in db.execute(
                            "SELECT content_code FROM handle_content WHERE "
                            "handle_id=? AND status='active'", (h["id"],))]
                # WHICH CLIENTS this account has actually logged in from.
                # The NICK token is per (account x client build), so an account
                # used from the PC and the PS2 legitimately has two. Shown here
                # because the failure it explains is otherwise indistinguishable
                # from a wrong password: the PS2 refused with 0xCA while the
                # same account worked on the PC (live 2026-08-24, "Fox").
                clients = []
                if mem:
                    try:
                        clients = [_client_label(t["client_sig"])
                                   for t in accounts.list_client_tokens(
                                       db, mem["id"])]
                    except Exception:
                        clients = []      # pre-migration DB: just omit it
                out.append({"polid": pol, "created_at": r["created_at"],
                            "handle": handle, "contents": contents,
                            "linked": linked,
                            "unlinked": sorted(set(contents) - set(linked)),
                            "clients": clients,
                            "contents_label": _content_label(
                                ",".join(str(c) for c in contents))})
        except Exception as exc:   # schema drift shouldn't 500 the whole panel
            return self._send(200, {"error": str(exc), "accounts": []})
        finally:
            db.close()
        self._send(200, out)

    def _account_footprint(self):
        """What deleting an account would destroy. The confirm dialog reads this
        BEFORE offering the button, so the operator never confirms blind."""
        from urllib.parse import urlsplit, parse_qs
        pol = (parse_qs(urlsplit(self.path).query).get("polid") or [""])[0].strip()
        if not pol:
            return self._send(400, {"error": "polid required"})
        db = _db()
        try:
            fp = accounts.account_footprint(db, pol)
        finally:
            db.close()
        if fp is None:
            return self._send(404, {"error": "no such account"})
        fp["contents_label"] = _content_label(
            ",".join(str(c) for c in fp["contents"]))
        self._send(200, fp)

    def _account_create(self):
        """Create a whole account: POL ID, member, handle, content, mail.

        Straight through `accounts.register_account`, which is the SAME call the
        in-client sign-up makes (ucscgi kinou 30) -- deliberately, so an
        operator-made account is indistinguishable from a player-made one. It is
        one transaction: a taken handle leaves nothing behind.

        Two things the panel adds on top, both copied from the sign-up path
        because an account without them is subtly broken:
          * `set_mail_password`, or the account has a mailbox it cannot open;
          * the generated password is RETURNED, once. There is nowhere to read it
            back from -- only the PBKDF2 hash is stored -- so the response is the
            only copy and the UI has to show it.
        """
        body = self._json_body()
        handle = (body.get("handle") or "").strip()
        pw = (body.get("password") or "").strip()
        generated = not pw
        if generated:
            # 12 hex is inside the client's own 8-15 field, and typable.
            pw = secrets.token_hex(6)
        codes = body.get("content_codes")
        if not isinstance(codes, list):
            codes = []
        try:
            codes = [int(c) for c in codes if c]
        except (TypeError, ValueError):
            return self._send(400, {"error": "content codes must be numbers"})
        if not handle:
            handle = f"Player{secrets.randbelow(9999):04d}"
        db = _db()
        try:
            acct = accounts.register_account(
                db, handle, pw, code=(body.get("code") or "").strip() or None,
                contents=tuple(codes) or (1,))
            accounts.set_mail_password(db, acct["member_id"], pw)
        except accounts.RegistrationError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        finally:
            db.close()
        who = (self._session() or {}).get("user") or "open-panel"
        # The password is NOT logged -- the response carries it to the browser
        # and that is the only place it should exist.
        print(f"[admin] {who} created account {acct['polid']!r} "
              f"handle={acct['handle']!r} contents={acct['contents']}",
              flush=True)
        acct["password"] = pw
        acct["generated"] = generated
        acct["nick"] = (polnick.nick_for_polid(acct["polid"])
                        if polnick is not None else None)
        acct["contents_label"] = _content_label(
            ",".join(str(c) for c in acct["contents"]))
        self._send(200, acct)

    def _account_password(self):
        """Set an account's password, and optionally re-arm trust-on-first-use.

        THE TWO ARE DIFFERENT KEYS and the panel says so, because assuming
        otherwise is how an operator "resets the password" of an account that
        then still cannot log in:

          * the password (PBKDF2 in `member`/`polid`) is what the ucs-cgi account
            servlet checks -- the in-client account screens;
          * the LOBBY login checks `member.login_token`, the 11-char token out of
            the NICK line, trust-on-first-use. A mismatch is SE reject 0xCA
            (measured 2026-08-17). Changing the password does not touch it.

        So `reset_token` is a separate, opt-in flag rather than something a
        password change does quietly: clearing the token means the next client to
        present this account is believed on sight and re-seeds the credential.
        """
        body = self._json_body()
        who = (body.get("polid") or body.get("who") or "").strip()
        pw = (body.get("password") or "").strip()
        if not who:
            return self._send(400, {"error": "polid required"})
        if not pw and not body.get("reset_token"):
            return self._send(400, {"error": "password required"})
        db = _db()
        try:
            row = None
            if pw:
                row = accounts.set_account_password(db, who, pw)
            else:
                row = (accounts.get_member(db, who)
                       or accounts.member_by_polid(db, who)
                       or accounts.member_by_alias(db, who))
            if row is None:
                return self._send(404, {"error": "no such account"})
            if body.get("reset_token"):
                accounts.clear_login_token(db, row["id"])
        except accounts.RegistrationError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        finally:
            db.close()
        op = (self._session() or {}).get("user") or "open-panel"
        print(f"[admin] {op} "
              + ("changed the password for " if pw else "reset the login token of ")
              + f"{row['polid']!r}"
              + (" and reset its login token" if pw and body.get("reset_token") else ""),
              flush=True)
        self._send(200, {"ok": True, "polid": row["polid"],
                         "login_name": row["login_name"],
                         "changed": bool(pw),
                         "token_reset": bool(body.get("reset_token"))})

    def _account_delete(self):
        """Erase an account. The POL ID must be repeated in the body.

        Not because a stray click could reach here -- the UI already confirms --
        but because this is the one endpoint in the panel with no undo, and a
        request that names its target twice cannot be a misrouted one.
        """
        body = self._json_body()
        pol = (body.get("polid") or "").strip()
        confirm = (body.get("confirm") or "").strip()
        if not pol:
            return self._send(400, {"error": "polid required"})
        if confirm != pol:
            return self._send(400, {"error": "confirm must repeat the PlayOnline ID"})
        db = _db()
        try:
            fp = accounts.delete_polid(
                db, pol, release_codes=bool(body.get("release_codes")))
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        finally:
            db.close()
        if fp is None:
            return self._send(404, {"error": "no such account"})
        who = (self._session() or {}).get("user") or "open-panel"
        print(f"[admin] {who} deleted account {pol!r}", flush=True)
        self._send(200, {"ok": True, "deleted": fp})

    def _grant(self):
        """Grant content directly, with no registration code in the middle.

        Takes `content_codes` (a list) or the older single `content_code`, so
        granting somebody the four titles they bought is one click rather than
        four.
        """
        body = self._json_body()
        pol = (body.get("polid") or "").strip()
        codes = body.get("content_codes")
        if not isinstance(codes, list):
            codes = [body.get("content_code")]
        try:
            codes = [int(c) for c in codes if c]
        except (TypeError, ValueError):
            return self._send(400, {"error": "content codes must be numbers"})
        if not pol or not codes:
            return self._send(400, {"error": "polid and at least one content required"})
        db = _db()
        try:
            mem = db.execute("SELECT id FROM member WHERE polid=?", (pol,)).fetchone()
            if not mem:
                return self._send(404, {"error": "no such account"})
            for code in codes:
                accounts.grant_content(db, mem["id"], code)
            # AND LINK IT TO A HANDLE, which is what actually makes the title
            # playable. A `content` row is the member's ENTITLEMENT; the launch
            # gate reads the 64-slot character table, and that table is built by
            # lobby 1:3 out of `handle_content` (see responders._db_chars). A
            # grant without a link therefore shows as owned on this panel and as
            # "You have no Content ID for <game>" on the client -- reported live
            # 2026-08-16, with members 14/15/16 holding grants and no links at
            # all. The UCS registration path has always called this; the panel
            # never did.
            linked = accounts.link_member_content_to_primary(db, mem["id"])
            db.commit()
        finally:
            db.close()
        self._send(200, {"ok": True, "polid": pol, "content_codes": codes,
                         "linked": linked})

    def _revoke(self):
        """Cancel content on an account -- the inverse of _grant.

        `revoke_content` deactivates BOTH the member's `content` grant and the
        per-handle `handle_content` link. The link is the load-bearing half: the
        launcher's 64-slot character table is built by lobby 1:3 out of
        `handle_content`, so cancelling the link is what removes the title's
        character from the launcher and returns the client to 'Create character'.
        Reversible -- it deactivates rather than deletes, so a later Grant
        restores the same Content ID on the same handle.
        """
        body = self._json_body()
        pol = (body.get("polid") or "").strip()
        codes = body.get("content_codes")
        if not isinstance(codes, list):
            codes = [body.get("content_code")]
        try:
            codes = [int(c) for c in codes if c]
        except (TypeError, ValueError):
            return self._send(400, {"error": "content codes must be numbers"})
        if not pol or not codes:
            return self._send(400, {"error": "polid and at least one content required"})
        db = _db()
        try:
            mem = db.execute("SELECT id FROM member WHERE polid=?", (pol,)).fetchone()
            if not mem:
                return self._send(404, {"error": "no such account"})
            for code in codes:
                accounts.revoke_content(db, mem["id"], code)
            db.commit()
        finally:
            db.close()
        who = (self._session() or {}).get("user") or "open-panel"
        print(f"[admin] {who} revoked content {codes} from {pol!r}", flush=True)
        self._send(200, {"ok": True, "polid": pol, "content_codes": codes})

    # -- PML file browser ---------------------------------------------------- #
    def _pml_list(self):
        """Every .pml the server has, as www-relative paths (sorted), plus each
        one's shape so the browser can filter down to the files that draw.

        Cached: walking 3000+ files over the Docker bind mount is slow, so the
        list is computed once (warmed at startup) and refreshed lazily."""
        files, shapes = _pml_files()
        with _PML_LOCK:
            graph = _PML_CACHE["graph"]
        # Only the constructed ones: a page with no <include> is the common case
        # and says nothing by being absent, so this stays a few hundred entries.
        parts = {k: v for k, v in (graph.parts if graph else {}).items() if v}
        self._send(200, {"root": "www", "count": len(files), "files": files,
                         "shapes": shapes, "shape_help": PML_SHAPES,
                         "parts": parts})

    def _pml_refs(self):
        """Which files reference this one, and which it references.

        The answer a fragment owes the operator: it cannot draw itself, but it
        can name the pages it belongs to. See pmlrefs for how the edges are
        found -- they are a best-effort index over path EXPRESSIONS, so treat a
        listed page as "probably this one", not as a proof.
        """
        from urllib.parse import urlsplit, parse_qs
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        rel = rel.replace("\\", "/").strip().lstrip("/")
        _pml_files()                              # make sure the index exists
        with _PML_LOCK:
            graph, shapes = _PML_CACHE["graph"], _PML_CACHE["shapes"]
        if graph is None:
            return self._send(200, {"path": rel})
        label = lambda p: {"path": p, "shape": shapes.get(p, ""),  # noqa: E731
                           "parts": graph.parts.get(p, 0)}
        out = {"path": rel, "parts": graph.parts.get(rel, 0)}
        for key in ("built_from", "included_by", "links_to", "linked_from"):
            out[key] = [label(p) for p in sorted(getattr(graph, key).get(rel, ()))]
        self._send(200, out)

    def _pml_expand(self):
        """Run a page's template layer (define/array/for/if + includes) and
        return flattened, positioned PML the renderer can draw. `path` (the
        loaded file's www-relative path) roots include resolution for that page.
        """
        body = self._json_body()
        text = body.get("text") or ""
        path = (body.get("path") or "").replace("\\", "/").strip()
        host_dirs, tree_dirs, start_dir = _pml_roots(path)

        def resolve(src, base):
            got = _pml_resolve(src, base or start_dir, host_dirs, tree_dirs)
            if not got:
                return None
            try:
                return _load_pml_text(got)[0], os.path.dirname(got)
            except Exception:
                return None

        report = {}
        try:
            out = pmleval.expand(text, resolve_include=resolve, base=start_dir,
                                 report=report)
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        self._send(200, {"text": out, "missing": report.get("missing") or [],
                         "unresolved": report.get("unresolved") or []})

    def _pml_load(self):
        from urllib.parse import urlsplit, parse_qs
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        if not rel:
            return self._send(400, {"error": "path required"})
        root = os.path.abspath(WWW_ROOT)
        fs_path = os.path.abspath(os.path.join(root, rel))
        if not fs_path.startswith(root + os.sep) or not os.path.isfile(fs_path):
            return self._send(404, {"error": "not found"})
        try:
            text, kind = _load_pml_text(fs_path)
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-PML-Kind", kind)      # how it was stored/decoded
        self.send_header("X-PML-Shape", _pml_shape(text))   # what it IS
        body = text.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- news ---------------------------------------------------------------- #
    def _news_state(self):
        """Everything the News tab needs to draw itself, including whether it
        can publish at all.

        The writable probe is not decoration. `/www` is mounted READ-ONLY in
        every compose file that predates this tab, and a panel that lets an
        operator type an announcement, save it, hit Publish and then fail is
        worse than one that says up front that the mount needs changing.
        """
        try:
            items = newsgen.load()
        except newsgen.NewsError as exc:
            return self._send(500, {"error": str(exc)})
        avail = newsgen.available_contents(WWW_ROOT)
        self._send(200, {
            "items": items,
            "store": newsgen.STORE,
            "seed": newsgen.SEED,
            # False while the live store has never been written: the panel is
            # showing the shipped seed, and the first save forks it.
            "using_store": os.path.exists(newsgen.STORE),
            "www": WWW_ROOT,
            "writable": _www_writable(),
            "kinds": {k: {"stamp": v[0], "category": v[1], "icon": v[2],
                          "maint": v[3],
                          # The Information list's row marker and the one-line
                          # description both come from newsgen so the panel and
                          # the CLI cannot describe a kind differently.
                          "marker": newsgen.row_marker(k),
                          "note": newsgen.KIND_NOTES.get(k, "")}
                      for k, v in newsgen.KINDS.items()},
            # Only services the INSTALLED badge sprite can draw. The content id
            # is the frame index, so offering one whose frame is missing puts a
            # broken image across the whole ticker -- see
            # newsgen.available_contents.
            "contents": {k: v for k, v in newsgen.CONTENT_LABELS.items()
                         if k in avail},
            # The numeric content id behind each key. The panel needs it to say
            # WHICH news<N>.pml a story lands in, and it is the sprite sequence.
            "content_ids": avail,
            # Which ticker icon each service choice actually draws -- measured
            # off ma_i/maic06i.ang, not read off SE's comment, which is off by
            # one. See newsgen.CONTENT_ICONS.
            "content_icons": newsgen.CONTENT_ICONS,
            "categories": newsgen.CATEGORIES,
        })

    def _news_art(self):
        """SE's own icons for the News tab's pickers, as data URIs.

        One request for the lot rather than an <img src> per choice: these are
        five 40x20 badges and five markers, ~7 KB together, and inlining them
        keeps the picker from flashing unstyled while ten requests land. Art
        that is missing from the serving tree is simply absent from the map --
        the panel falls back to text labels rather than drawing broken images.
        """
        import base64
        out = {}
        for name, (mime, blob) in newsgen.art(WWW_ROOT).items():
            out[name] = "data:%s;base64,%s" % (
                mime, base64.b64encode(blob).decode("ascii"))
        self._send(200, {"art": out})

    def _news_save(self):
        body = self._json_body()
        try:
            path, items = newsgen.save(body.get("items") or [])
        except newsgen.NewsError as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"cannot write {newsgen.STORE}: "
                                             f"{exc}"})
        print(f"[admin] announcements saved: {len(items)} item(s) -> {path}",
              flush=True)
        self._send(200, {"ok": True, "items": items, "store": path})

    def _news_outputs(self):
        """The files a publish would write, newest-relevant first.

        Rendered against the CURRENT tree, so the news<N> entries already show
        the merge with SE's archive rather than a guess at it.
        """
        try:
            items = newsgen.load()
            files = newsgen.outputs(items, WWW_ROOT)
        except newsgen.NewsError as exc:
            return self._send(400, {"error": str(exc)})
        def rank(rel):
            """Ticker first, then the Information lists, bodies, our page."""
            name = rel.rsplit("/", 1)[-1]
            if name.startswith("latestnews"):
                return (0, rel)
            if name.startswith("news"):
                return (1, rel)
            if name[0].isdigit():
                return (2, rel)
            return (3, rel)
        self._send(200, {
            "files": [{"path": rel, "bytes": len(text.encode("utf-8"))}
                      for rel, text in sorted(files.items(), key=lambda kv: rank(kv[0]))],
            "stale": newsgen.stale_details(items, WWW_ROOT),
        })

    def _news_preview(self):
        """The exact text that would be written to one output path."""
        from urllib.parse import urlsplit, parse_qs
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        rel = rel.replace("\\", "/").strip().lstrip("/")
        try:
            items = newsgen.load()
            files = newsgen.outputs(items, WWW_ROOT)
        except newsgen.NewsError as exc:
            return self._send(400, {"error": str(exc)})
        if rel not in files:
            return self._send(404, {"error": "not an output of this store"})
        self._send(200, files[rel], "text/plain; charset=utf-8")

    def _news_publish(self):
        body = self._json_body()
        dry = bool(body.get("dry_run"))
        if not dry and not _www_writable():
            return self._send(409, {
                "error": f"{WWW_ROOT} is mounted read-only, so nothing can be "
                         f"published. Drop `:ro` from the admin service's www "
                         f"volume in docker-compose and recreate the container."})
        try:
            items = newsgen.load()
            res = newsgen.publish(items, WWW_ROOT,
                                  prune=body.get("prune", True), dry_run=dry)
        except newsgen.NewsError as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"write failed: {exc}"})
        if not dry:
            print(f"[admin] announcements published: {len(res['written'])} "
                  f"file(s), {len(res['pruned'])} pruned, under {WWW_ROOT}",
                  flush=True)
        res["ok"] = True
        self._send(200, res)

    # -- live page proxy ----------------------------------------------------- #
    def _live_page(self):
        """Fetch a live wizard page from ucscgi so the editor can load it."""
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(self.path).query)
        kinou = (q.get("kinou_id") or ["20"])[0]
        step = (q.get("step") or ["1"])[0]
        url = f"{CGI}/pml-cgi-bin/?kinou_id={kinou}&step={step}"
        try:
            req = urllib.request.Request(url, headers={"Host": "ucs.pol.com"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
        except Exception as exc:
            return self._send(502, {"error": f"fetch failed: {exc}"})
        self._send(200, data, "text/plain; charset=utf-8")


def _cli(argv):
    """Offline credential management -- the break-glass path when the panel
    itself is unreachable or the password is forgotten:

        docker compose exec admin python services/admin.py --set-password
        docker compose exec admin python services/admin.py --show-admin
        docker compose exec admin python services/admin.py --clear-password

    Handled before the server starts, and returns True when it consumed the
    command line (so main() should not go on to listen).
    """
    if "--show-admin" in argv:
        row = _stored_cred()
        if row is None:
            print("[admin] no stored credential; the panel is OPEN"
                  + (" except for the POL_ADMIN_PASSWORD override"
                     if ADMIN_PASSWORD else ""))
        else:
            print(f"[admin] user={row['username']!r} set {row['updated_at']}")
        return True

    if "--clear-password" in argv:
        db = _db()
        try:
            accounts.clear_admin_cred(db)
            db.commit()
        finally:
            db.close()
        _sessions_clear()
        print("[admin] stored credential cleared -- the panel is now OPEN "
              "unless POL_ADMIN_PASSWORD is set. Restart the service.")
        return True

    if "--set-password" in argv:
        i = argv.index("--set-password")
        rest = [a for a in argv[i + 1:] if not a.startswith("--")]
        user = rest[0] if rest else (_stored_cred() or {"username": ADMIN_USER})["username"]
        # `docker compose exec` without -it has no tty, so fall back to stdin
        # rather than dying in getpass.
        pw = os.environ.get("POL_ADMIN_NEW_PASSWORD")
        if not pw:
            try:
                import getpass
                pw = getpass.getpass(f"New password for {user!r}: ")
            except Exception:
                print(f"New password for {user!r} (echoed): ", end="", flush=True)
                pw = sys.stdin.readline().rstrip("\n")
        db = _db()
        try:
            accounts.set_admin_cred(db, user, pw)
            db.commit()
        except ValueError as exc:
            print(f"[admin] rejected: {exc}")
            return True
        finally:
            db.close()
        _sessions_clear()
        print(f"[admin] credential set for {user!r}; all sessions revoked")
        return True

    return False


def main():
    if _cli(sys.argv[1:]):
        return
    port = int(os.environ.get("POL_ADMIN_PORT", "8090"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[admin] listening on {port}; db={DB} art={ART_ROOT} cgi={CGI}",
          flush=True)
    # Say plainly whether anything is guarding a panel that mints accounts --
    # "open" is a legitimate loopback-only setting, not a state to discover by
    # accident from off-host.
    row = _stored_cred()
    if row is not None:
        print(f"[admin] auth: stored credential, user={row['username']!r}"
              + ("; POL_ADMIN_PASSWORD override ALSO active" if ADMIN_PASSWORD
                 else ""), flush=True)
    elif ADMIN_PASSWORD:
        print(f"[admin] auth: POL_ADMIN_PASSWORD only, user={ADMIN_USER!r} -- "
              "set a password in the Security tab to make it changeable",
              flush=True)
    else:
        print("[admin] auth: OPEN (no credential set) -- safe only while bound "
              "to loopback", flush=True)
    # Warm the (slow) PML file listing off the request path so the browser is
    # ready by the time the operator opens the PML tab.
    def _warm():
        files, shapes = _pml_files()
        tally = collections.Counter(shapes.values())
        print(f"[admin] indexed {len(files)} pml files -- "
              + ", ".join(f"{n} {k}" for k, n in tally.most_common()), flush=True)
    threading.Thread(target=_warm, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
