"""Single-use dev access codes, with this panel as their issuer (docs/deployer.md, "Dev access
codes").

The developers' panel hands out short codes (ABCD-EFGH-JKMN): each one unlocks the dev channel
for one install. An install's deployer redeems a code with POST /api/dev-code/redeem, sending
its install id; the first install to do so binds the code, and gets back an ordinary access
code (olx1., or olx2. with a patch feed) to switch with. The same install asking again gets the
same answer, so a reinstalled panel is not locked out; any other install is refused, so a code
that leaks after use unlocks nothing.

One panel can issue codes of several kinds, one per thing a code opens (the panel's own dev
builds, a paired game's). Each code is made for one kind and redeems to that kind's access
code. An install holds at most one live code of each kind.

Settings (environment):

    POL_DEV_ISSUER_KINDS           the kinds, as key:Label pairs separated by commas, such as
                                   "kart:Kart Club,derby:Derby Night". A key is a lower-case
                                   letter then up to 11 letters or digits. Unset: one unnamed
                                   kind (key ''), set by the settings below without <KEY>_.
    POL_DEV_ISSUER_<KEY>_CODE      the access code that kind's codes redeem to: its registry,
                                   source and patch feed, and its token when it has one. Unset:
                                   that kind issues nothing (its codes are refused with 503).
    POL_DEV_ISSUER_<KEY>_FORGEJO_URL   https://forgejo.example.org: mint one read-only token
                                   per code there instead of handing out the base code's token
    POL_DEV_ISSUER_<KEY>_FORGEJO_USER  the Forgejo user the tokens belong to: one that can
                                   only read what this kind's codes open. Each kind needs its
                                   own, because a Forgejo token cannot be limited to one org.
    POL_DEV_ISSUER_<KEY>_FORGEJO_AUTH  name:password for the Forgejo API calls that make and
                                   delete those tokens (Forgejo makes a token only for a
                                   password sign-in). The name is that user's, or a site
                                   admin's, who then acts for the user (Sudo).
    POL_DEV_ISSUER_<KEY>_SCOPES    the minted tokens' scopes, a comma list of read:package and
                                   read:repository; read:package when unset.

<KEY> is the kind's key in capitals: POL_DEV_ISSUER_KART_CODE. For the unnamed kind it is left
out with its underscore: POL_DEV_ISSUER_CODE, POL_DEV_ISSUER_FORGEJO_URL and so on, the
settings of a panel from before kinds existed.

Without the three Forgejo settings every code of the kind redeems to the base code itself
(public packages need no token), and the owner's page says so. With them, each code gets a
token of its own named openlobby-dev-<kind>-<code> (openlobby-dev-<code> for the unnamed kind),
and revoking the code deletes it, so that one install's next pull is refused while every other
install keeps working.

The owner makes (1 to 100 at a time, with a note and a kind), lists and revokes codes in
Settings, Modules, "Dev access codes"; every change is audited by admin.py. The redeem route
needs no login and shares /api/pair's limit: POL_ADMIN_PAIR_TRIES a minute per address.

Codes live in dev_codes (polcore migrations 0021 and 0022). Every function here opens its own
connection and answers (status, body).
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
LABEL_MAX = 40
TIMEOUT = 15.0
LIMIT = adminpair.Limiter()
KIND_KEY = re.compile(r"^[a-z][a-z0-9]{0,11}$")
SCOPES = ("read:package", "read:repository")
DEFAULT_SCOPES = ["read:package"]

UNKNOWN = "That dev code is not known here. Check it, or ask for a new one."
REVOKED = "That dev code was revoked. Ask for a new one."
TAKEN = "That dev code is already used by another server. Each code works for one server; ask for a new one."
HAS_ONE = ("This server already redeemed another dev code of this kind, which is still valid. Use the "
           "access code it gave, or ask for that one to be revoked first.")
KIND_OFF = ("This panel does not hand out that kind of dev code at the moment. Try again later, or "
            "ask the people who gave you the code.")


def _now():
    return time.time()


class ForgejoError(Exception):
    pass


class Forgejo:
    """The two calls minting needs: make a token for `user` with `scopes` and delete one."""

    def __init__(self, url, user, auth, scopes=None, timeout=TIMEOUT):
        self.url, self.user, self.timeout = url.rstrip("/"), user, timeout
        self.scopes = list(scopes or DEFAULT_SCOPES)
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
        """(token id, token) of a new token called `name` with this kind's scopes. A token
        left over with that name (a redeem that failed halfway) is deleted first."""
        for attempt in (1, 2):
            st, doc = self._call("POST", self._tokens(), {"name": name, "scopes": list(self.scopes)})
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


def _env(prefix, name):
    return os.environ.get(prefix + name, "").strip()


def _kind(key, label, prefix):
    """One kind's settings: {"key", "label", "prefix", "base", "code", "forgejo", "scopes",
    "why"}. base is None when the kind issues nothing, and why says what to set."""
    k = {"key": key, "label": label, "prefix": prefix, "base": None, "code": None, "forgejo": None,
         "scopes": list(DEFAULT_SCOPES), "why": None}
    named = ("%s codes" % label) if key else "Codes"
    code = _env(prefix, "CODE")
    if not code:
        k["why"] = ("%s are not handed out: %sCODE (the access code they redeem to) is not set."
                    % (named, prefix))
        return k
    try:
        base = devchannel.parse_code(code)
    except devchannel.ChannelError as exc:
        k["why"] = "%s are not handed out: %sCODE is not an access code this panel can read: %s" % (
            named, prefix, exc)
        return k
    raw = _env(prefix, "SCOPES")
    if raw:
        want = [s.strip() for s in raw.split(",") if s.strip()]
        odd = [s for s in want if s not in SCOPES]
        if odd or not want:
            k["why"] = ("%s are not handed out: %sSCOPES may name only read:package and read:repository, "
                        "and it names %s." % (named, prefix, ", ".join(odd) or "nothing"))
            return k
        k["scopes"] = [s for s in SCOPES if s in want]
    url, user, auth = _env(prefix, "FORGEJO_URL"), _env(prefix, "FORGEJO_USER"), _env(prefix, "FORGEJO_AUTH")
    k["base"], k["code"] = base, code
    if url or user or auth:
        if not (url.startswith(("http://", "https://")) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
                and ":" in auth and auth.split(":", 1)[0] and auth.split(":", 1)[1]):
            k["why"] = ("The %sFORGEJO_ settings are incomplete (URL, USER, and AUTH as name:password), "
                        "so %s all redeem to the shared access code." % (prefix, named if key else "codes"))
            return k
        k["forgejo"] = Forgejo(url, user, auth, k["scopes"])
        k["why"] = ("%s get a registry token of their own on Forgejo (user %s, %s), and Revoke deletes it."
                    % (named, user, " and ".join(k["scopes"])))
    else:
        k["why"] = ("%s all redeem to the same shared access code, so Revoke cannot cut one server off. "
                    "Set %sFORGEJO_URL, _USER and _AUTH to give each code its own registry token."
                    % (named, prefix))
    return k


def config():
    """{"kinds": [kind, ...] in the order configured, "problems": [sentence, ...]}. Without
    POL_DEV_ISSUER_KINDS the one kind is the unnamed one ('')."""
    raw = os.environ.get("POL_DEV_ISSUER_KINDS", "").strip()
    if not raw:
        return {"kinds": [_kind("", "", "POL_DEV_ISSUER_")], "problems": []}
    kinds, problems, seen = [], [], set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, label = part.partition(":")
        key, label = key.strip(), " ".join(label.split())[:LABEL_MAX]
        if not KIND_KEY.match(key) or not label:
            problems.append("POL_DEV_ISSUER_KINDS has %r, which is not key:Label (a key is a lower-case "
                            "letter then up to 11 letters or digits)." % part[:40])
            continue
        if key in seen:
            problems.append("POL_DEV_ISSUER_KINDS names the kind %s twice." % key)
            continue
        seen.add(key)
        kinds.append(_kind(key, label, "POL_DEV_ISSUER_%s_" % key.upper()))
    readers = {}
    for k in kinds:
        f = k["forgejo"]
        if f is None:
            continue
        who = (f.url.lower(), f.user.lower())
        if who in readers:
            k["base"], k["forgejo"] = None, None
            k["why"] = ("%s codes are not handed out: they use the same Forgejo user as %s codes (%s). "
                        "Give each kind its own reader user, because a Forgejo token cannot be limited "
                        "to one org." % (k["label"], readers[who]["label"], who[1]))
        else:
            readers[who] = k
    return {"kinds": kinds, "problems": problems}


def _find(cfg, key):
    return next((k for k in cfg["kinds"] if k["key"] == key), None)


def _token_name(kind, short):
    return "openlobby-dev-%s%s" % (kind + "-" if kind else "", short)


def _access_code(k, token):
    """The access code to answer with: the kind's base code itself, or with `token` (a minted one)."""
    if not token:
        return k["code"]
    b = k["base"]
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
    kinds = cfg["kinds"]
    issuing = [k for k in kinds if k["base"] is not None]
    why = kinds[0]["why"] if len(kinds) == 1 else None
    if not kinds:
        why = "This panel does not hand out dev codes: POL_DEV_ISSUER_KINDS names no kind it can use."
    out = []
    for r in rows:
        k = _find(cfg, r["kind"])
        out.append({"code": shown(r["code"]), "kind": r["kind"],
                    "kind_label": k["label"] if k else r["kind"],
                    "kind_ok": bool(k and k["base"] is not None),
                    "note": r["note"], "made_by": r["made_by"],
                    "created_at": r["created_at"], "redeemed_at": r["redeemed_at"],
                    "install": _install(r["install_id"]), "revoked_at": r["revoked_at"],
                    "state": _state(r), "token": bool(r["token_ref"]),
                    "token_left": bool(r["revoked_at"] and r["token_ref"])})
    return 200, {"issuing": bool(issuing), "minting": any(k["forgejo"] is not None for k in issuing),
                 "why": why, "problems": cfg["problems"],
                 "kinds": [{"key": k["key"], "label": k["label"], "issuing": k["base"] is not None,
                            "minting": k["forgejo"] is not None, "scopes": k["scopes"], "why": k["why"]}
                           for k in kinds],
                 "codes": out}


def make(count, note, actor, kind=None):
    """Make `count` (1 to 100) codes of `kind` for `note`. The kind may be left out while the
    panel has only one. (status, {"codes": [shown, ...], "kind", "kind_label"})."""
    if isinstance(count, str) and count.strip().isdigit():
        count = int(count.strip())
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_MAKE:
        return 400, {"error": "Make 1 to %d codes at a time." % MAX_MAKE}
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    if not note:
        return 400, {"error": "Say who the codes are for in the note."}
    kinds = config()["kinds"]
    if kind is None or kind == "":
        if len(kinds) != 1:
            return 400, {"error": "Pick which kind of code to make."}
        k = kinds[0]
    else:
        k = _find({"kinds": kinds}, kind) if isinstance(kind, str) else None
        if k is None:
            return 400, {"error": "This panel has no kind of dev code called that."}
    made = []
    db = adminusers.connect()
    try:
        now = _now()
        while len(made) < count:
            code = "".join(secrets.choice(devchannel.SHORT_ALPHABET) for _ in range(devchannel.SHORT_LEN))
            n = db.execute("INSERT INTO dev_codes (code, kind, note, made_by, created_at) VALUES (%s,%s,%s,%s,%s)"
                           " ON CONFLICT (code) DO NOTHING", (code, k["key"], note, actor, now)).rowcount
            if n == 1:
                made.append(code)
        db.commit()
    finally:
        db.close()
    return 200, {"ok": True, "codes": [shown(c) for c in made], "note": note, "kind": k["key"],
                 "kind_label": k["label"]}


def revoke(code, actor):
    """Revoke a code; when a token was minted for it, delete it on Forgejo with its kind's
    settings. A revoked code whose token could not be deleted keeps its token_ref, and
    revoking it again tries again. When its kind has no Forgejo settings now, the code is
    revoked all the same and the answer's "warning" says the token is still there."""
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
    out = {"ok": True, "code": shown(short), "note": row["note"], "kind": row["kind"]}
    if ref:
        k = _find(config(), row["kind"])
        if k is None or k["forgejo"] is None:
            where = ("its kind (%s) is not configured here now" % row["kind"] if k is None and row["kind"]
                     else "the %sFORGEJO_ settings are not set now" % (k["prefix"] if k else "POL_DEV_ISSUER_"))
            return 200, dict(out, token_deleted=False,
                             warning="Revoked, but its Forgejo token was not deleted: %s. Delete the token "
                                     "%s by hand, or set them again and press Revoke once more."
                                     % (where, _token_name(row["kind"], short)))
        try:
            k["forgejo"].delete(ref)
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
    if not any(k["base"] is not None for k in cfg["kinds"]):
        return 404, {"error": "This panel does not hand out dev codes."}, None
    minted = k = None
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
            k = _find(cfg, row["kind"])
            if k is None or k["base"] is None:
                raise _Refused(503, KIND_OFF, ("an install tried the dev code for %s, whose kind (%s) is not "
                                               "configured" % (note, row["kind"] or "unnamed"), False))
            if row["install_id"] and row["install_id"] != install:
                raise _Refused(409, TAKEN, ("another install tried the dev code for %s" % note, False))
            if row["install_id"] == install:
                db.rollback()
                return 200, {"ok": True, "access_code": _access_code(k, row["token"]), "again": True}, None
            other = db.execute("SELECT code FROM dev_codes WHERE install_id = %s AND kind = %s"
                               " AND revoked_at IS NULL", (install, row["kind"])).fetchone()
            if other is not None:
                raise _Refused(409, HAS_ONE, ("an install that has a dev code tried another (%s)" % note, False))
            token = ref = None
            if k["forgejo"] is not None:
                try:
                    ref, sha = k["forgejo"].mint(_token_name(row["kind"], short))
                    token = "%s:%s" % (k["forgejo"].user, sha)     # signs in as that user
                except ForgejoError as exc:
                    raise _Refused(502, "The dev code is fine, but the issuer could not make its "
                                        "registry sign-in; try again later.",
                                   ("could not mint a token for the dev code for %s: %s" % (note, exc), False))
                minted = ref
            access = _access_code(k, token)
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
                k["forgejo"].delete(minted)
            except ForgejoError:
                pass
    kind = (" (%s)" % k["label"]) if k["key"] else ""
    return 200, {"ok": True, "access_code": access}, \
        ("an install redeemed the dev code for %s%s%s" % (note, kind, " (its own registry token)" if token else ""), True)
