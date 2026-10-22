"""Games added to the admin panel by hand or paired through their own setup program.

Two ways in, both stored in the panel's database (polcore migration 0016, admin_game) and
read by adminmodules exactly like POL_ADMIN_GAME_<KEY>_URL and _TOKEN (the environment wins
when both name a key):

  - Settings > Modules, "Add a game" (owner only): a key, the game's admin URL and token.
    Test loads <url>/openlobby/module.json with the token (X-Devtool-Token) and says what the
    game describes; Save tests again and stores it.
  - Pairing (docs/installs.md, "Pairing"): the owner makes a one-time code (8 characters,
    10 minutes, one use); the game's setup program sends it with the game's key, title, URL
    and token to POST /api/pair, and gets a pairing secret back. The panel keeps only the
    secret's sha256. With the secret the setup program can later tell the panel a new token
    (POST /api/pair/update). Unpairing ends the secret; removing the game ends everything.

Tokens are never sent back to a browser: listing() says only whether one is set. The two
pairing routes need no panel login; a code or a secret is what lets them in, and each client
address gets PAIR_TRIES tries a minute (POL_ADMIN_PAIR_TRIES, default 5) before a 429.

Every function here that touches the database takes no connection and opens its own
(adminusers.connect()); every one answers (status, body) for the panel to send as it is.
"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error

import adminmodules
import adminusers

KEY_RE = adminmodules.KEY_RE
URL_RE = re.compile(r"^https?://[A-Za-z0-9._~\-\[\]:%@]+(/[A-Za-z0-9._~\-/%]*)?$")
TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
#: no 0/O, 1/I/L: a code is read off a screen and typed into a terminal
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_TTL = 600.0
PAIR_WINDOW = 60.0
SECRET_BYTES = 32


def _now():
    return time.time()


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _text(v, limit):
    return " ".join(str(v or "").split())[:limit]


def _env_url(key):
    return os.environ.get("POL_ADMIN_GAME_%s_URL" % key.upper(), "").strip()


# --------------------------------------------------------------------------- #
# reading the stored games
# --------------------------------------------------------------------------- #
def stored():
    """{key: (url, token)} for adminmodules.STORE."""
    db = adminusers.connect()
    try:
        rows = db.execute("SELECT key, url, token FROM admin_game").fetchall()
    finally:
        db.close()
    return {r["key"]: (r["url"], r["token"]) for r in rows}


def _row(db, key):
    return db.execute("SELECT * FROM admin_game WHERE key = %s", (key,)).fetchone()


def listing():
    """The stored games for the owner's Modules page, tokens left out."""
    db = adminusers.connect()
    try:
        rows = db.execute("SELECT * FROM admin_game ORDER BY lower(title), key").fetchall()
    finally:
        db.close()
    return [{"key": r["key"], "url": r["url"], "title": r["title"] or r["key"], "how": r["how"],
             "added_by": r["added_by"], "added_at": r["added_at"], "updated_at": r["updated_at"],
             "token_set": bool(r["token"]), "paired": bool(r["pairing_hash"]),
             "env": bool(_env_url(r["key"]))}
            for r in rows]


# --------------------------------------------------------------------------- #
# asking a game what it is
# --------------------------------------------------------------------------- #
def clean_url(url):
    """The URL without a trailing slash, or None when it is not an http(s) address."""
    if not isinstance(url, str):
        return None
    url = url.strip().rstrip("/")
    if len(url) > 300 or not URL_RE.match(url):
        return None
    return url


def probe(key, url, token):
    """(Module, None) when <url>/openlobby/module.json answers with the token and names `key`,
    else (None, a sentence saying what went wrong)."""
    from adminmodules import remote
    where = url + "/openlobby/module.json"
    try:
        return remote.build(key, url, token), None
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "The game at %s refused that token." % url
        if exc.code == 404:
            return None, ("%s answered, but has no /openlobby/module.json. Is that the game's "
                          "admin port?" % url)
        return None, "%s answered HTTP %d." % (where, exc.code)
    except (socket.timeout, TimeoutError):
        return None, "Nothing answered at %s within %d seconds." % (url, int(remote.TIMEOUT))
    except urllib.error.URLError as exc:
        return None, "Nothing answered at %s (%s)." % (url, _text(exc.reason, 120))
    except json.JSONDecodeError:
        return None, "%s answered something that is not JSON." % where
    except ValueError as exc:
        return None, "The game at %s answered, but %s." % (url, _text(exc, 200))
    except OSError as exc:
        return None, "Nothing answered at %s (%s)." % (url, _text(exc, 120))


def described(m):
    """What a probed game describes, for the Test result."""
    return {"key": m.key, "title": m.title, "short": m.short, "platform": m.platform,
            "about": m.about,
            "tools": [{"id": t.id, "label": t.label} for t in m.tools],
            "actions": [{"id": a.id, "label": a.label} for a in m.actions]}


def _check_key(key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        return "The key is 1 to 16 lower-case letters and digits, the same key the game's module.json gives."
    return None


def _check_token(token):
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return "The token is 1 to 512 characters, with no spaces."
    return None


# --------------------------------------------------------------------------- #
# Add a game (owner)
# --------------------------------------------------------------------------- #
def test(body):
    """Settings > Modules, Test: {"key", "url", "token"}; a blank token uses the stored one."""
    key, url, token = body.get("key"), clean_url(body.get("url")), body.get("token") or ""
    bad = _check_key(key)
    if bad:
        return 400, {"error": bad}
    if url is None:
        return 400, {"error": "The address must start with http:// or https://, for example http://kart:8820."}
    if not token:
        db = adminusers.connect()
        try:
            row = _row(db, key)
        finally:
            db.close()
        token = row["token"] if row else ""
    bad = _check_token(token)
    if bad:
        return 400, {"error": bad}
    m, why = probe(key, url, token)
    if m is None:
        return 400, {"error": why}
    return 200, {"ok": True, "game": described(m)}


def save(body, actor):
    """Settings > Modules, Save: tests the game, then stores it. A blank token on a game
    already stored keeps its token; a paired game stays paired."""
    key, url, token = body.get("key"), clean_url(body.get("url")), body.get("token") or ""
    bad = _check_key(key)
    if bad:
        return 400, {"error": bad}
    if url is None:
        return 400, {"error": "The address must start with http:// or https://, for example http://kart:8820."}
    db = adminusers.connect()
    try:
        row = _row(db, key)
        if not token and row is not None:
            token = row["token"]
        bad = _check_token(token)
        if bad:
            return 400, {"error": bad}
        m, why = probe(key, url, token)
        if m is None:
            return 400, {"error": why}
        now = _now()
        if row is None:
            db.execute("INSERT INTO admin_game (key, url, token, title, how, added_by, added_at)"
                       " VALUES (%s,%s,%s,%s,'manual',%s,%s)", (key, url, token, m.title, actor, now))
        else:
            db.execute("UPDATE admin_game SET url = %s, token = %s, title = %s, updated_at = %s"
                       " WHERE key = %s", (url, token, m.title, now, key))
        db.commit()
    finally:
        db.close()
    adminmodules.refresh()
    return 200, {"ok": True, "key": key, "title": m.title, "added": row is None,
                 "env": bool(_env_url(key))}


def remove(key):
    """Takes a stored game off the panel; its pairing secret ends with it."""
    bad = _check_key(key)
    if bad:
        return 400, {"error": bad}
    db = adminusers.connect()
    try:
        row = _row(db, key)
        if row is None:
            return 404, {"error": "No game with the key %s was added here." % key}
        db.execute("DELETE FROM admin_game WHERE key = %s", (key,))
        db.commit()
    finally:
        db.close()
    adminmodules.refresh()
    return 200, {"ok": True, "key": key, "title": row["title"] or key}


def unpair(key):
    """Ends a paired game's secret: the game stays, managed by hand from now on."""
    bad = _check_key(key)
    if bad:
        return 400, {"error": bad}
    db = adminusers.connect()
    try:
        row = _row(db, key)
        if row is None:
            return 404, {"error": "No game with the key %s was added here." % key}
        if not row["pairing_hash"]:
            return 400, {"error": "%s is not paired." % (row["title"] or key)}
        db.execute("UPDATE admin_game SET pairing_hash = NULL, how = 'manual', updated_at = %s"
                   " WHERE key = %s", (_now(), key))
        db.commit()
    finally:
        db.close()
    return 200, {"ok": True, "key": key, "title": row["title"] or key}


# --------------------------------------------------------------------------- #
# pairing codes (owner)
# --------------------------------------------------------------------------- #
def norm_code(code):
    """The code as typed, upper-cased, spaces and dashes taken out."""
    return re.sub(r"[\s-]", "", str(code or "")).upper()


def new_code(actor):
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    now = _now()
    db = adminusers.connect()
    try:
        db.execute("DELETE FROM admin_pair_code WHERE expires_at < %s", (now - 86400,))
        db.execute("INSERT INTO admin_pair_code (code_hash, created_by, created_at, expires_at)"
                   " VALUES (%s,%s,%s,%s)", (_sha(code), actor, now, now + CODE_TTL))
        db.commit()
    finally:
        db.close()
    return 200, {"code": code, "expires_at": now + CODE_TTL, "ttl": int(CODE_TTL)}


def code_state(code):
    """{"state": "waiting" | "used" | "expired" | "unknown", ...} for the code view."""
    db = adminusers.connect()
    try:
        row = db.execute("SELECT * FROM admin_pair_code WHERE code_hash = %s",
                         (_sha(norm_code(code)),)).fetchone()
        game = _row(db, row["used_key"]) if row is not None and row["used_key"] else None
    finally:
        db.close()
    if row is None:
        return 404, {"state": "unknown"}
    if row["used_at"]:
        return 200, {"state": "used", "key": row["used_key"],
                     "title": (game["title"] if game else None) or row["used_key"]}
    if row["expires_at"] < _now():
        return 200, {"state": "expired"}
    return 200, {"state": "waiting", "expires_at": row["expires_at"]}


# --------------------------------------------------------------------------- #
# the setup program's side: POST /api/pair and /api/pair/update
# --------------------------------------------------------------------------- #
class Limiter:
    """`tries` a `window` per client address, every try counted."""

    def __init__(self, tries=None, window=PAIR_WINDOW):
        self.tries = tries
        self.window = window
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, addr):
        tries = self.tries
        if tries is None:
            try:
                tries = max(1, int(os.environ.get("POL_ADMIN_PAIR_TRIES", "5") or 5))
            except ValueError:
                tries = 5
        now = _now()
        with self._lock:
            q = self._hits[addr]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= tries:
                return False, int(q[0] + self.window - now) + 1
            q.append(now)
            if len(self._hits) > 4096:
                for a in [a for a, d in self._hits.items() if not d]:
                    del self._hits[a]
            return True, 0


LIMIT = Limiter()


def _bad_fields(body, need_title):
    key, url, token = body.get("key"), body.get("url"), body.get("token")
    bad = _check_key(key)
    if bad:
        return bad
    if clean_url(url) is None:
        return "url must be the game's admin address, starting with http:// or https://."
    if need_title and not _text(body.get("title"), 60):
        return "title must be the game's name."
    return _check_token(token)


def pair(body):
    """POST /api/pair. Answers (status, body, audit) where audit is (what, ok) or None."""
    if not isinstance(body, dict) or not body:
        return 400, {"error": "Send JSON: code, key, title, url and token."}, None
    code = norm_code(body.get("code"))
    if not code:
        return 400, {"error": "code is missing: the 8 characters the panel shows."}, None
    bad = _bad_fields(body, need_title=True)
    if bad:
        return 400, {"error": bad}, None
    key, url, token = body["key"], clean_url(body["url"]), body["token"]
    now = _now()
    db = adminusers.connect()
    try:
        row = db.execute("SELECT * FROM admin_pair_code WHERE code_hash = %s", (_sha(code),)).fetchone()
        if row is None:
            return 403, {"error": "That code is wrong. Check it against the one the panel shows."}, \
                ("a setup program tried to pair %s with a wrong code" % key, False)
        if row["used_at"]:
            return 403, {"error": "That code was already used. Make a new one in the panel."}, \
                ("a setup program tried to pair %s with a used code" % key, False)
        if row["expires_at"] < now:
            return 403, {"error": "That code has expired. Make a new one in the panel."}, \
                ("a setup program tried to pair %s with an expired code" % key, False)
        if _env_url(key):
            return 400, {"error": "This panel's settings already point %s at a game (POL_ADMIN_GAME_%s_URL). "
                                  "Take that out first." % (key, key.upper())}, None
        m, why = probe(key, url, token)
        if m is None:
            return 400, {"error": why}, ("a setup program could not pair %s: %s" % (key, why), False)
        secret = secrets.token_urlsafe(SECRET_BYTES)
        with db:
            taken = db.execute("UPDATE admin_pair_code SET used_at = %s, used_key = %s"
                               " WHERE code_hash = %s AND used_at IS NULL AND expires_at >= %s",
                               (now, key, _sha(code), now)).rowcount
            if taken != 1:
                raise _Raced()
            db.execute("INSERT INTO admin_game (key, url, token, title, how, added_by, added_at,"
                       " pairing_hash) VALUES (%s,%s,%s,%s,'paired',%s,%s,%s)"
                       " ON CONFLICT (key) DO UPDATE SET url = excluded.url, token = excluded.token,"
                       " title = excluded.title, how = 'paired', updated_at = excluded.added_at,"
                       " pairing_hash = excluded.pairing_hash",
                       (key, url, token, m.title, row["created_by"], now, _sha(secret)))
    except _Raced:
        return 403, {"error": "That code was already used. Make a new one in the panel."}, \
            ("a setup program tried to pair %s with a used code" % key, False)
    finally:
        db.close()
    adminmodules.refresh()
    return 200, {"ok": True, "title": m.title, "pairing": secret}, \
        ("paired %s through its setup program" % m.title, True)


class _Raced(Exception):
    """Another request used the code between the check and the write."""


def pair_update(body):
    """POST /api/pair/update. Answers (status, body, audit)."""
    if not isinstance(body, dict) or not body:
        return 400, {"error": "Send JSON: key, pairing and token (and url, when it changed)."}, None
    key, secret, token = body.get("key"), body.get("pairing"), body.get("token")
    bad = _check_key(key) or _check_token(token)
    if bad:
        return 400, {"error": bad}, None
    if not isinstance(secret, str) or not secret or len(secret) > 200:
        return 400, {"error": "pairing must be the secret the panel gave when the game paired."}, None
    url = None
    if body.get("url") not in (None, ""):
        url = clean_url(body.get("url"))
        if url is None:
            return 400, {"error": "url must be the game's admin address, starting with http:// or https://."}, None
    db = adminusers.connect()
    try:
        row = _row(db, key)
        if row is None:
            return 404, {"error": "This panel has no game with the key %s." % key}, \
                ("a setup program tried to change the token of %s, which is not here" % key, False)
        if not row["pairing_hash"] or not hmac.compare_digest(row["pairing_hash"], _sha(secret)):
            return 403, {"error": "That pairing secret is not this panel's for %s. Pair again." % key}, \
                ("a setup program tried to change the token of %s with a wrong pairing secret"
                 % (row["title"] or key), False)
        db.execute("UPDATE admin_game SET token = %s, url = %s, updated_at = %s WHERE key = %s",
                   (token, url or row["url"], _now(), key))
        db.commit()
    finally:
        db.close()
    adminmodules.refresh()
    return 200, {"ok": True}, ("%s changed its token through its setup program" % (row["title"] or key), True)
