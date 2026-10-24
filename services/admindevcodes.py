"""Single-use dev access codes, with this panel as their issuer (docs/deployer.md, "Dev access
codes").

The developers' panel hands out short codes (ABCD-EFGH-JKMN): each one unlocks the dev channel
for one install. An install's deployer redeems a code with POST /api/dev-code/redeem, sending
its install id; the first install to do so binds the code, and gets back an ordinary access
code (olx1., or olx2. with a patch feed) to switch with. The same install asking again gets the
same answer, so a reinstalled panel is not locked out; any other install is refused, so a code
that leaks after use unlocks nothing.

Settings (environment):

    POL_DEV_ISSUER_CODE            the access code redemption is based on: its registry, source
                                   and patch feed, and its token when it has one. Unset: this
                                   panel issues nothing and the redeem route answers 404.
    POL_DEV_ISSUER_FORGEJO_URL     https://forgejo.example.org: mint one read-only package token
                                   per code there instead of handing out the base code's token
    POL_DEV_ISSUER_FORGEJO_USER    the Forgejo user the tokens belong to (one that can only read
                                   the dev packages)
    POL_DEV_ISSUER_FORGEJO_AUTH    name:password for the Forgejo API calls that make and delete
                                   those tokens (Forgejo takes only a password sign-in to make a
                                   token). The name is that user's, or a site admin's, who then
                                   acts for the user (Sudo).

Without the three Forgejo settings every code redeems to the base code itself (public packages
need no token), and the owner's page says so. With them, each code gets a token of its own
named openlobby-dev-<code>, and revoking the code deletes it, so that one install's next pull
is refused while every other install keeps working.

The owner makes (1 to 100 at a time, with a note), lists and revokes codes in Settings,
Modules, "Dev access codes"; every change is audited by admin.py. The redeem route needs no
login and shares /api/pair's limit: POL_ADMIN_PAIR_TRIES a minute per address.

Codes live in dev_codes (polcore migration 0021). Every function here opens its own connection
and answers (status, body).
"""
import base64
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.join(HERE, "deployer") not in sys.path:
    sys.path.append(os.path.join(HERE, "deployer"))

import adminpair  # noqa: E402
import adminusers  # noqa: E402
import devchannel  # noqa: E402

MAX_MAKE = 100
NOTE_MAX = 120
TIMEOUT = 15.0
LIMIT = adminpair.Limiter()

UNKNOWN = "That dev code is not known here. Check it, or ask for a new one."
REVOKED = "That dev code was revoked. Ask for a new one."
TAKEN = "That dev code is already used by another server. Each code works for one server; ask for a new one."
HAS_ONE = ("This server already redeemed another dev code, which is still valid. Use the access "
           "code it gave, or ask for that one to be revoked first.")


def _now():
    return time.time()


class ForgejoError(Exception):
    pass


class Forgejo:
    """The two calls minting needs: make a token for `user` (read:package only) and delete one."""

    def __init__(self, url, user, auth, timeout=TIMEOUT):
        self.url, self.user, self.timeout = url.rstrip("/"), user, timeout
        name = auth.split(":", 1)[0]
        self.headers = {"Authorization": "Basic " + base64.b64encode(auth.encode("utf-8")).decode("ascii"),
                        "Content-Type": "application/json", "Accept": "application/json"}
        if name != user:
            self.headers["Sudo"] = user          # a site admin acting for the reader user

    def _call(self, method, path, body=None):
        req = urllib.request.Request(self.url + "/api/v1" + path, method=method, headers=self.headers,
                                     data=None if body is None else json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(65536)
                return r.status, json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                doc = json.loads(e.read(4096) or b"{}")
            except ValueError:
                doc = {}
            return e.code, doc if isinstance(doc, dict) else {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ForgejoError("Forgejo did not answer (%s)" % str(getattr(exc, "reason", None) or exc)[:160])

    def _tokens(self):
        return "/users/%s/tokens" % urllib.parse.quote(self.user, safe="")

    def mint(self, name):
        """(token id, token) of a new read-only package token called `name`. A token left over
        with that name (a redeem that failed halfway) is deleted first."""
        for attempt in (1, 2):
            st, doc = self._call("POST", self._tokens(), {"name": name, "scopes": ["read:package"]})
            if st in (200, 201) and doc.get("sha1") and doc.get("id") is not None:
                return str(doc["id"]), doc["sha1"]
            if st in (400, 409, 422) and attempt == 1:
                self.delete(name)
                continue
            raise ForgejoError("Forgejo did not make the token (HTTP %d%s)"
                               % (st, ": %s" % str(doc.get("message"))[:120] if doc.get("message") else ""))
        raise ForgejoError("Forgejo did not make the token")

    def delete(self, ref):
        st, doc = self._call("DELETE", "%s/%s" % (self._tokens(), urllib.parse.quote(str(ref), safe="")))
        if st not in (200, 204, 404):
            raise ForgejoError("Forgejo did not delete the token (HTTP %d%s)"
                               % (st, ": %s" % str(doc.get("message"))[:120] if doc.get("message") else ""))


def config():
    """{"base": parsed base code or None, "code": the base code, "forgejo": Forgejo or None,
    "why": a sentence for the owner's page}."""
    code = os.environ.get("POL_DEV_ISSUER_CODE", "").strip()
    out = {"base": None, "code": None, "forgejo": None, "why": None}
    if not code:
        out["why"] = ("This panel does not hand out dev codes: POL_DEV_ISSUER_CODE (the access code "
                      "codes redeem to) is not set.")
        return out
    try:
        out["base"] = devchannel.parse_code(code)
        out["code"] = code
    except devchannel.ChannelError as exc:
        out["why"] = "POL_DEV_ISSUER_CODE is not an access code this panel can read: %s" % exc
        return out
    url = os.environ.get("POL_DEV_ISSUER_FORGEJO_URL", "").strip()
    user = os.environ.get("POL_DEV_ISSUER_FORGEJO_USER", "").strip()
    auth = os.environ.get("POL_DEV_ISSUER_FORGEJO_AUTH", "").strip()
    if url or user or auth:
        if not (url.startswith(("http://", "https://")) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
                and ":" in auth and auth.split(":", 1)[0] and auth.split(":", 1)[1]):
            out["why"] = ("The POL_DEV_ISSUER_FORGEJO_ settings are incomplete (URL, USER, and AUTH as "
                          "name:password), so every code redeems to the shared access code.")
            return out
        out["forgejo"] = Forgejo(url, user, auth)
        out["why"] = ("Each code gets a registry token of its own on Forgejo (user %s), and Revoke "
                      "deletes it." % user)
    else:
        out["why"] = ("Every code redeems to the same shared access code: its packages need no "
                      "token of their own. Set POL_DEV_ISSUER_FORGEJO_URL, _USER and _AUTH to give "
                      "each code its own registry token, which Revoke deletes.")
    return out


def _access_code(cfg, token):
    """The access code to answer with: the base code itself, or with `token` (a minted one)."""
    if not token:
        return cfg["code"]
    b = cfg["base"]
    return devchannel.make_code(b["registry"], token, b["source"], b["feed"])


def shown(code):
    return devchannel.show_short(code)


def _state(r):
    return "revoked" if r["revoked_at"] else ("used" if r["install_id"] else "unused")


def _install(v):
    """An install id as the owner's page shows it: enough to tell two apart."""
    return (v[:11] + "...") if v and len(v) > 14 else v


def listing():
    cfg = config()
    db = adminusers.connect()
    try:
        rows = db.execute("SELECT * FROM dev_codes ORDER BY created_at DESC, code LIMIT 1000").fetchall()
    finally:
        db.close()
    return 200, {"issuing": cfg["base"] is not None, "minting": cfg["forgejo"] is not None,
                 "why": cfg["why"],
                 "codes": [{"code": shown(r["code"]), "note": r["note"], "made_by": r["made_by"],
                            "created_at": r["created_at"], "redeemed_at": r["redeemed_at"],
                            "install": _install(r["install_id"]), "revoked_at": r["revoked_at"],
                            "state": _state(r), "token": bool(r["token_ref"]),
                            "token_left": bool(r["revoked_at"] and r["token_ref"])} for r in rows]}


def make(count, note, actor):
    """Make `count` (1 to 100) codes for `note`. (status, {"codes": [shown, ...]})."""
    if isinstance(count, str) and count.strip().isdigit():
        count = int(count.strip())
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_MAKE:
        return 400, {"error": "Make 1 to %d codes at a time." % MAX_MAKE}
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    if not note:
        return 400, {"error": "Say who the codes are for in the note."}
    made = []
    db = adminusers.connect()
    try:
        now = _now()
        while len(made) < count:
            code = "".join(secrets.choice(devchannel.SHORT_ALPHABET) for _ in range(devchannel.SHORT_LEN))
            n = db.execute("INSERT INTO dev_codes (code, note, made_by, created_at) VALUES (%s,%s,%s,%s)"
                           " ON CONFLICT (code) DO NOTHING", (code, note, actor, now)).rowcount
            if n == 1:
                made.append(code)
        db.commit()
    finally:
        db.close()
    return 200, {"ok": True, "codes": [shown(c) for c in made], "note": note}


def revoke(code, actor):
    """Revoke a code; when a token was minted for it, delete it on Forgejo. A revoked code
    whose token could not be deleted keeps its token_ref, and revoking it again tries again."""
    short = devchannel.short_code(code or "")
    if short is None:
        return 400, {"error": "Which code? It is 12 characters, like ABCD-EFGH-JKMN."}
    db = adminusers.connect()
    try:
        db.begin()          # a SELECT alone runs outside any transaction, and FOR UPDATE with it
        row = db.execute("SELECT * FROM dev_codes WHERE code = %s FOR UPDATE", (short,)).fetchone()
        if row is None:
            return 404, {"error": UNKNOWN}
        again = bool(row["revoked_at"])
        if again and not row["token_ref"]:
            return 200, {"ok": True, "code": shown(short), "note": row["note"], "already": True}
        db.execute("UPDATE dev_codes SET revoked_at = coalesce(revoked_at, %s), token = NULL WHERE code = %s",
                   (_now(), short))
        db.commit()
        ref = row["token_ref"]
    finally:
        db.close()
    out = {"ok": True, "code": shown(short), "note": row["note"]}
    if ref:
        cfg = config()
        if cfg["forgejo"] is None:
            return 502, dict(out, error="Revoked here, but its Forgejo token is still there: the "
                                        "POL_DEV_ISSUER_FORGEJO_ settings are not set now. Delete the "
                                        "token openlobby-dev-%s by hand, or set them and revoke again."
                                        % short)
        try:
            cfg["forgejo"].delete(ref)
        except ForgejoError as exc:
            return 502, dict(out, error="Revoked here, but %s. Press Revoke again to retry." % exc)
        db = adminusers.connect()
        try:
            db.execute("UPDATE dev_codes SET token_ref = NULL WHERE code = %s", (short,))
            db.commit()
        finally:
            db.close()
        out["token_deleted"] = True
    return 200, out


class _Refused(Exception):
    def __init__(self, status, error, audit):
        super().__init__(error)
        self.status, self.error, self.audit = status, error, audit


def redeem(body):
    """POST /api/dev-code/redeem {"code", "install_id"}: (status, body, audit (what, ok) or None).
    The binding is one transaction (the code's row locked FOR UPDATE)."""
    if not isinstance(body, dict):
        return 400, {"error": "Send JSON: code and install_id."}, None
    short = devchannel.short_code(body.get("code") if isinstance(body.get("code"), str) else "")
    install = body.get("install_id")
    if short is None:
        return 400, {"error": "code must be a dev code, 12 characters like ABCD-EFGH-JKMN."}, None
    if not isinstance(install, str) or not devchannel.INSTALL_ID.match(install):
        return 400, {"error": "install_id must be the deployer's install id."}, None
    cfg = config()
    if cfg["base"] is None:
        return 404, {"error": "This panel does not hand out dev codes."}, None
    minted = None
    db = adminusers.connect()
    try:
        try:
            db.begin()      # the lock below lasts until commit or rollback
            row = db.execute("SELECT * FROM dev_codes WHERE code = %s FOR UPDATE", (short,)).fetchone()
            if row is None:
                raise _Refused(404, UNKNOWN, ("an install tried an unknown dev code", False))
            note = row["note"] or shown(short)
            if row["revoked_at"]:
                raise _Refused(403, REVOKED, ("an install tried the revoked dev code for %s" % note, False))
            if row["install_id"] and row["install_id"] != install:
                raise _Refused(409, TAKEN, ("another install tried the dev code for %s" % note, False))
            if row["install_id"] == install:
                db.rollback()
                return 200, {"ok": True, "access_code": _access_code(cfg, row["token"]), "again": True}, None
            other = db.execute("SELECT code FROM dev_codes WHERE install_id = %s AND revoked_at IS NULL",
                               (install,)).fetchone()
            if other is not None:
                raise _Refused(409, HAS_ONE, ("an install that has a dev code tried another (%s)" % note, False))
            token = ref = None
            if cfg["forgejo"] is not None:
                try:
                    ref, sha = cfg["forgejo"].mint("openlobby-dev-%s" % short)
                    token = "%s:%s" % (cfg["forgejo"].user, sha)     # signs in as that user
                except ForgejoError as exc:
                    raise _Refused(502, "The dev code is fine, but the issuer could not make its "
                                        "registry sign-in; try again later.",
                                   ("could not mint a token for the dev code for %s: %s" % (note, exc), False))
                minted = ref
            access = _access_code(cfg, token)
            try:
                db.execute("UPDATE dev_codes SET install_id = %s, redeemed_at = %s, token_ref = %s,"
                           " token = %s WHERE code = %s", (install, _now(), ref, token, short))
                db.commit()
            except Exception as exc:              # noqa: BLE001 -- another code bound this install first
                if "dev_codes_install" not in str(exc):
                    raise
                raise _Refused(409, HAS_ONE, ("an install that has a dev code tried another (%s)" % note, False))
            minted = None
        except _Refused as exc:
            db.rollback()
            return exc.status, {"error": exc.error}, exc.audit
    finally:
        db.close()
        if minted is not None:
            # the binding did not commit: the token it was for must not outlive it
            try:
                cfg["forgejo"].delete(minted)
            except ForgejoError:
                pass
    return 200, {"ok": True, "access_code": access}, \
        ("an install redeemed the dev code for %s%s" % (note, " (its own registry token)" if token else ""), True)
