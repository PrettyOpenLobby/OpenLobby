#!/usr/bin/env python3
"""Single-use dev access codes, with the panel as their issuer (admindevcodes.py; docs/deployer.md,
"Dev access codes").

    python tools/admin_devcodes_test.py

A panel without POL_DEV_ISSUER_CODE issues nothing: its page says so and the redeem route answers
404. A panel with one: the owner makes 1 to 100 codes with a note (anything else refused with a
sentence), lists them and revokes them, each in the activity log; a moderator gets 403 for all of
it. POST /api/dev-code/redeem with no login: a code that does not fit, an install id that does not
fit, an unknown code, then the first install binds the code and gets the shared access code, the
same install asking again gets the same answer, another install is refused, an install that holds
a live code cannot take a second, two installs racing for one code get one 200 and one 409, a
revoked code is refused and frees its install. Then a panel that mints a registry token per code
on a stand-in Forgejo (POL_DEV_ISSUER_FORGEJO_*): the token made with basic auth and read:package
only, the access code carries it, a second redeem makes no second token, a failed mint binds
nothing and says so, revoke deletes the token, and a site admin's sign-in acts for the reader
user (Sudo). Then kinds (POL_DEV_ISSUER_KINDS, made-up kart and derby): each kind with its own
base code and its own stand-in Forgejo and reader user, derby's tokens with two scopes; a code is
made for a kind (none given with two kinds is refused), one install holds a code of each kind but
not two of one; a kind taken out of the settings lists its codes as not configured, answers 503
to redeem, and still revokes, saying its token was left; a scope outside the allowlist, a second
kind on the first one's Forgejo user and a malformed KINDS entry each refuse with a sentence. The
migration's default kind '' and its key check. Last, the limit: five tries a minute from one
address, then 429. Throwaway
PostgreSQL (tools/pgtest.py); the panels run as subprocesses.
"""
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)
sys.path.insert(0, os.path.join(SERVICES, "deployer"))

import pgtest  # noqa: E402

pgtest.use_fresh_database()
for _k in [k for k in os.environ if k.startswith(("POL_DEV_ISSUER", "POL_ADMIN_PAIR_TRIES", "POL_DEPLOYER_"))]:
    os.environ.pop(_k)

import accounts  # noqa: E402
import adminusers  # noqa: E402
import devchannel  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "kart club owner 1"
BASE = devchannel.make_code("kart.example.org/dev", None, "https://code.example.org/kart")
INSTALL_A = "ol-" + "a" * 32
INSTALL_B = "ol-" + "b" * 32
INSTALL_C = "ol-" + "c" * 32
READER, READER_PW = "kartdev", "reader password 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def sql(q, args=()):
    db = accounts.connect()
    try:
        cur = db.execute(q, args)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        db.commit()
        return rows
    finally:
        db.close()


def audit_since(n):
    return [(r["actor"], r["action"], bool(r["ok"]))
            for r in sql("SELECT * FROM admin_audit WHERE id > %s ORDER BY id", (n,))]


def audit_top():
    return (sql("SELECT coalesce(max(id), 0) AS n FROM admin_audit") or [{"n": 0}])[0]["n"]


class Forgejo:
    """The two token endpoints of Forgejo's API, for one reader user (READER unless told): POST
    makes a token (basic auth with the user's password only, as Forgejo wants), DELETE removes
    one. self.fail makes the next POST a 500."""

    def __init__(self, user=READER, password=READER_PW, prefix="kart-token"):
        self.user, self.password, self.prefix = user, password, prefix
        self.tokens = {}
        self.calls = []
        self.fail = False
        self.n = 0
        fj = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None):
                b = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def who(self):
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Basic "):
                    return None
                name, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                if (name, pw) == (fj.user, fj.password):
                    return fj.user
                if (name, pw) == ("siteadmin", "admin password 1") and self.headers.get("Sudo") == fj.user:
                    return fj.user
                return None

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fj.calls.append(("POST", self.path, body, self.headers.get("Sudo")))
                if self.path != "/api/v1/users/%s/tokens" % fj.user:
                    return self.send(404, {"message": "not found"})
                if self.who() != fj.user:
                    return self.send(401, {"message": "basic auth required"})
                if fj.fail:
                    fj.fail = False
                    return self.send(500, {"message": "database is locked"})
                if any(t["name"] == body.get("name") for t in fj.tokens.values()):
                    return self.send(422, {"message": "access token name has been used already"})
                fj.n += 1
                tok = {"id": fj.n, "name": body.get("name"), "sha1": "%s-%04d" % (fj.prefix, fj.n),
                       "scopes": body.get("scopes")}
                fj.tokens[str(fj.n)] = tok
                return self.send(201, tok)

            def do_DELETE(self):
                fj.calls.append(("DELETE", self.path, None, self.headers.get("Sudo")))
                if self.who() != fj.user:
                    return self.send(401, {"message": "basic auth required"})
                ref = self.path.rsplit("/", 1)[-1]
                hit = [k for k, t in fj.tokens.items() if k == ref or t["name"] == ref]
                if not hit:
                    return self.send(404, {"message": "not found"})
                for k in hit:
                    del fj.tokens[k]
                return self.send(204)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def seed():
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "mod_set", "pass for mod_set", ["audit", "settings"], by=OWNER)
    db.close()


def redeem(anon, code, install):
    return anon.call("api/dev-code/redeem", {"code": code, "install_id": install})


def not_issuing(tmp, base_env):
    print("a panel that issues nothing")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=base_env)
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        code, r = owner.call("api/devcodes")
        chk("its page: not issuing, and why", (code, r.get("issuing"), "POL_DEV_ISSUER_CODE" in (r.get("why") or "")),
            (200, False, True))
        code, r = owner.call("api/devcodes", {"action": "make", "count": 1, "note": "Kart Club testers"})
        chk("  making codes still works (for later)", (code, len(r.get("codes") or [])), (200, 1))
        code, r = redeem(anon, r["codes"][0], INSTALL_A)
        chk("  but redeeming one: 404 with a sentence", (code, r.get("error")), (404, "This panel does not hand out dev codes."))
        sql("DELETE FROM dev_codes")
    finally:
        stop(proc)


def shared(tmp, base_env):
    print("a panel that hands out the shared access code")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(base_env, POL_DEV_ISSUER_CODE=BASE))
    try:
        owner, mod, anon = Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        chk("moderator signs in", mod.login("mod_set", "pass for mod_set"), 200)
        code, r = owner.call("api/devcodes")
        chk("its page: issuing, no minting, says the codes share one access code",
            (code, r.get("issuing"), r.get("minting"), "same shared access code" in (r.get("why") or ""), r.get("codes")),
            (200, True, False, True, []))
        for what, body, said in (("0 codes", {"count": 0, "note": "x"}, "Make 1 to 100 codes at a time."),
                                 ("101 codes", {"count": 101, "note": "x"}, "Make 1 to 100 codes at a time."),
                                 ("a count that is not a number", {"count": "three", "note": "x"},
                                  "Make 1 to 100 codes at a time."),
                                 ("no note", {"count": 2, "note": "  "}, "Say who the codes are for in the note.")):
            code, r = owner.call("api/devcodes", dict(body, action="make"))
            chk("make %s: 400 with a sentence" % what, (code, r.get("error")), (400, said))
        chk("a moderator cannot list them", mod.call("api/devcodes")[0], 403)
        chk("  nor make them", mod.call("api/devcodes", {"action": "make", "count": 1, "note": "x"})[0], 403)
        n = audit_top()
        code, r = owner.call("api/devcodes", {"action": "make", "count": 4, "note": "Kart Club  testers"})
        codes = r.get("codes") or []
        chk("make 4: four codes, ABCD-EFGH-JKMN shaped, the note tidied",
            (code, len(codes), all(devchannel.short_code(c) and len(c) == 14 and c.count("-") == 2 for c in codes),
             r.get("note")), (200, 4, True, "Kart Club testers"))
        chk("  in the activity log", audit_since(n)[:1], [(OWNER, "made 4 dev access codes for Kart Club testers", True)])
        listed = owner.call("api/devcodes")[1]
        rows = listed["codes"]
        chk("  listed, unused", sorted(x["state"] for x in rows), ["unused"] * 4)
        chk("  of the one unnamed kind, which issues", ([(k["key"], k["issuing"]) for k in listed["kinds"]],
                                                         {(x["kind"], x["kind_ok"]) for x in rows}),
            ([("", True)], {("", True)}))
        c1, c2, c3, c4 = codes

        print("redeeming")
        for what, body, status in (("no code", {"install_id": INSTALL_A}, 400),
                                   ("a code that is not one", {"code": "kart-plain-token", "install_id": INSTALL_A}, 400),
                                   ("no install id", {"code": c1}, 400),
                                   ("an install id that does not fit", {"code": c1, "install_id": "x y"}, 400),
                                   ("an unknown code", {"code": "ZZZZ-ZZZZ-ZZZZ", "install_id": INSTALL_A}, 404)):
            code, r = anon.call("api/dev-code/redeem", body)
            chk("%s: %d with a sentence" % (what, status), (code, bool(r.get("error"))), (status, True))
        n = audit_top()
        code, r = redeem(anon, c1.lower().replace("-", " "), INSTALL_A)
        chk("the first install: 200 with the shared access code (typed lower-case with spaces)",
            (code, r.get("access_code")), (200, BASE))
        chk("  it reads as an access code", devchannel.parse_code(r["access_code"])["registry"], "kart.example.org/dev")
        chk("  in the activity log", audit_since(n),
            [("dev code", "an install redeemed the dev code for Kart Club testers", True)])
        code, r = redeem(anon, c1, INSTALL_A)
        chk("the same install again: the same answer", (code, r.get("access_code"), r.get("again")), (200, BASE, True))
        code, r = redeem(anon, c1, INSTALL_B)
        chk("another install: 409", (code, "another server" in r.get("error", "")), (409, True))
        code, r = redeem(anon, c2, INSTALL_A)
        chk("the first install with a second code: 409, it holds one", (code, "already redeemed" in r.get("error", "")),
            (409, True))
        row = sql("SELECT * FROM dev_codes WHERE code = %s", (devchannel.short_code(c1),))[0]
        chk("  the code is bound to the first install", (row["install_id"], bool(row["redeemed_at"]), row["token"]),
            (INSTALL_A, True, None))
        rows = {x["code"]: x for x in owner.call("api/devcodes")[1]["codes"]}
        chk("  listed in use, with part of the install id", (rows[c1]["state"], rows[c1]["install"]),
            ("used", INSTALL_A[:11] + "..."))

        out = []
        threads = [threading.Thread(target=lambda i=i: out.append(redeem(Client(url), c3, i)[0]))
                   for i in ("ol-" + "d" * 32, "ol-" + "e" * 32)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        chk("two installs racing for one code: one 200, one 409", sorted(out), [200, 409])

        print("revoking")
        chk("a moderator cannot revoke", mod.call("api/devcodes", {"action": "revoke", "code": c1})[0], 403)
        n = audit_top()
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": c1})
        chk("the owner revokes it", (code, r.get("code")), (200, c1))
        chk("  in the activity log", audit_since(n), [(OWNER, "revoked the dev access code for Kart Club testers", True)])
        code, r = redeem(anon, c1, INSTALL_A)
        chk("  redeeming it again, even from its install: 403", (code, r.get("error")),
            (403, "That dev code was revoked. Ask for a new one."))
        code, r = redeem(anon, c2, INSTALL_A)
        chk("  and its install may now take another", (code, r.get("access_code")), (200, BASE))
        chk("revoking an unknown code: 404", owner.call("api/devcodes", {"action": "revoke", "code": "ZZZZ-ZZZZ-ZZZZ"})[0], 404)
        chk("revoking something that is not a code: 400", owner.call("api/devcodes", {"action": "revoke", "code": "x"})[0], 400)
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": c4})
        rows = {x["code"]: x for x in owner.call("api/devcodes")[1]["codes"]}
        chk("revoking an unused code", (code, rows[c4]["state"]), (200, "revoked"))
        chk("  it redeems nothing", redeem(anon, c4, INSTALL_C)[0], 403)
        sql("DELETE FROM dev_codes")
    finally:
        stop(proc)


def minting(tmp, base_env, fj, auth):
    print("a panel that mints a token per code (%s)" % auth.split(":")[0])
    env = dict(base_env, POL_DEV_ISSUER_CODE=BASE, POL_DEV_ISSUER_FORGEJO_URL=fj.url,
               POL_DEV_ISSUER_FORGEJO_USER=READER, POL_DEV_ISSUER_FORGEJO_AUTH=auth)
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        code, r = owner.call("api/devcodes")
        chk("its page: minting, says Revoke deletes the token",
            (r.get("issuing"), r.get("minting"), "Revoke deletes it" in (r.get("why") or "")), (True, True, True))
        c1, c2 = owner.call("api/devcodes", {"action": "make", "count": 2, "note": "Derby Night"})[1]["codes"]
        short1 = devchannel.short_code(c1)
        del fj.calls[:]
        fj.n = 0
        code, r = redeem(anon, c1, INSTALL_A)
        info = devchannel.parse_code(r.get("access_code") or "x" * 9) if code == 200 else {}
        chk("redeem: 200, the access code carries a token of its own, the base code's registry and source",
            (code, info.get("token"), info.get("registry"), info.get("source")),
            (200, READER + ":kart-token-0001", "kart.example.org/dev", "https://code.example.org/kart"))
        mint = [c for c in fj.calls if c[0] == "POST"]
        chk("  made on Forgejo for the reader, read:package only, named after the code",
            [(c[1], c[2]) for c in mint],
            [("/api/v1/users/%s/tokens" % READER, {"name": "openlobby-dev-" + short1, "scopes": ["read:package"]})])
        chk("  the Sudo header only for an admin's sign-in", [c[3] for c in mint],
            [None] if auth.startswith(READER) else [READER])
        code, r2 = redeem(anon, c1, INSTALL_A)
        chk("the same install again: the same access code, no second token",
            (code, r2.get("access_code") == r.get("access_code"), len([c for c in fj.calls if c[0] == "POST"])),
            (200, True, 1))
        rows = {x["code"]: x for x in owner.call("api/devcodes")[1]["codes"]}
        chk("  listed with its own token", rows[c1]["token"], True)
        fj.fail = True
        code, r = redeem(anon, c2, INSTALL_B)
        chk("Forgejo fails to mint: 502 with a sentence, nothing bound",
            (code, "could not make its registry sign-in" in r.get("error", ""),
             sql("SELECT install_id FROM dev_codes WHERE code = %s", (devchannel.short_code(c2),))[0]["install_id"]),
            (502, True, None))
        code, r = redeem(anon, c2, INSTALL_B)
        chk("  tried again: 200", (code, devchannel.parse_code(r.get("access_code") or "x" * 9)["token"]
                                   if code == 200 else None), (200, READER + ":kart-token-0002"))
        n = audit_top()
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": c1})
        chk("revoke: the token deleted on Forgejo", (code, r.get("token_deleted"), sorted(fj.tokens)), (200, True, ["2"]))
        chk("  the stored token blanked", sql("SELECT token, token_ref FROM dev_codes WHERE code = %s", (short1,)),
            [{"token": None, "token_ref": None}])
        chk("  in the activity log", audit_since(n)[0][1:], ("revoked the dev access code for Derby Night", True))
        sql("DELETE FROM dev_codes")
        fj.tokens.clear()
    finally:
        stop(proc)


KART_BASE = devchannel.make_code("kart.example.org/dev", None, "https://code.example.org/kart")
DERBY_BASE = devchannel.make_code("derby.example.org/dev", None, "https://code.example.org/derby",
                                  "derby.example.org/patches", "derby-server")
DERBY_READER, DERBY_PW = "derbydev", "derby reader password 2"


def kinds_env(base_env, kart, derby, **more):
    env = dict(base_env, POL_DEV_ISSUER_KINDS="kart:Kart Club, derby:Derby Night",
               POL_DEV_ISSUER_KART_CODE=KART_BASE, POL_DEV_ISSUER_DERBY_CODE=DERBY_BASE,
               POL_DEV_ISSUER_KART_FORGEJO_URL=kart.url, POL_DEV_ISSUER_KART_FORGEJO_USER=kart.user,
               POL_DEV_ISSUER_KART_FORGEJO_AUTH="%s:%s" % (kart.user, kart.password),
               POL_DEV_ISSUER_DERBY_FORGEJO_URL=derby.url, POL_DEV_ISSUER_DERBY_FORGEJO_USER=derby.user,
               POL_DEV_ISSUER_DERBY_FORGEJO_AUTH="%s:%s" % (derby.user, derby.password),
               POL_DEV_ISSUER_DERBY_SCOPES="read:repository, read:package")
    env.update(more)
    return env


def kinds(tmp, base_env, kart, derby):
    print("a panel with two kinds of code, each with its own Forgejo reader")
    for f in (kart, derby):
        f.tokens.clear()
        del f.calls[:]
        f.n = 0
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=kinds_env(base_env, kart, derby))
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        code, r = owner.call("api/devcodes")
        ks = {k["key"]: k for k in r.get("kinds") or []}
        chk("its page: both kinds, in order, both minting with their own scopes",
            (code, r.get("issuing"), [k["key"] for k in r.get("kinds") or []],
             [(ks[k]["label"], ks[k]["issuing"], ks[k]["minting"], ks[k]["scopes"]) for k in ("kart", "derby")]),
            (200, True, ["kart", "derby"],
             [("Kart Club", True, True, ["read:package"]),
              ("Derby Night", True, True, ["read:package", "read:repository"])]))
        chk("  each kind says, in a sentence, that its codes get their own token",
            [("Kart Club codes get a registry token of their own" in ks["kart"]["why"], kart.user in ks["kart"]["why"]),
             ("Derby Night codes get a registry token of their own" in ks["derby"]["why"],
              derby.user in ks["derby"]["why"])], [(True, True), (True, True)])
        code, r = owner.call("api/devcodes", {"action": "make", "count": 1, "note": "x"})
        chk("make with no kind: 400, pick one", (code, r.get("error")), (400, "Pick which kind of code to make."))
        code, r = owner.call("api/devcodes", {"action": "make", "count": 1, "note": "x", "kind": "golf"})
        chk("make of a kind it does not have: 400", (code, bool(r.get("error"))), (400, True))
        n = audit_top()
        code, r = owner.call("api/devcodes", {"action": "make", "count": 2, "note": "Kart testers", "kind": "kart"})
        k1, k2 = r.get("codes") or [None, None]
        chk("make 2 Kart Club codes", (code, r.get("kind"), r.get("kind_label")), (200, "kart", "Kart Club"))
        chk("  in the activity log with the kind", audit_since(n)[:1],
            [(OWNER, "made 2 Kart Club dev access codes for Kart testers", True)])
        d1, d2 = owner.call("api/devcodes", {"action": "make", "count": 2, "note": "Derby testers",
                                             "kind": "derby"})[1]["codes"]
        rows = {x["code"]: x for x in owner.call("api/devcodes")[1]["codes"]}
        chk("  listed with their kinds", [(rows[c]["kind"], rows[c]["kind_label"], rows[c]["kind_ok"]) for c in (k1, d1)],
            [("kart", "Kart Club", True), ("derby", "Derby Night", True)])

        code, r = redeem(anon, k1, INSTALL_A)
        info = devchannel.parse_code(r.get("access_code") or "x" * 9) if code == 200 else {}
        chk("redeem a Kart Club code: the kart base code with a token from the kart reader",
            (code, info.get("registry"), info.get("token"), info.get("feed")),
            (200, "kart.example.org/dev", kart.user + ":kart-token-0001", None))
        chk("  minted on the kart Forgejo, read:package, named after the kind and code",
            [(c[1], c[2]) for c in kart.calls if c[0] == "POST"],
            [("/api/v1/users/%s/tokens" % kart.user,
              {"name": "openlobby-dev-kart-" + devchannel.short_code(k1), "scopes": ["read:package"]})])
        code, r = redeem(anon, d1, INSTALL_A)
        info = devchannel.parse_code(r.get("access_code") or "x" * 9) if code == 200 else {}
        chk("the same install redeems a Derby Night code too: the derby base code, its patch feed, its name, its token",
            (code, info.get("registry"), info.get("feed"), info.get("name"), info.get("token")),
            (200, "derby.example.org/dev", "derby.example.org/patches", "derby-server", derby.user + ":derby-token-0001"))
        chk("  a kart code (no name in its base) still has none", devchannel.parse_code(KART_BASE)["name"], None)
        chk("  minted on the derby Forgejo with both scopes, nothing more on the kart one",
            ([(c[2]["name"], c[2]["scopes"]) for c in derby.calls if c[0] == "POST"],
             len([c for c in kart.calls if c[0] == "POST"])),
            ([("openlobby-dev-derby-" + devchannel.short_code(d1), ["read:package", "read:repository"])], 1))
        code, r = redeem(anon, k2, INSTALL_A)
        chk("a second Kart Club code for that install: 409, it holds one of that kind",
            (code, "of this kind" in r.get("error", "")), (409, True))
        code, r = redeem(anon, k2, INSTALL_B)
        chk("  another install takes it", code, 200)
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": k1})
        chk("revoke the first Kart Club code: its token deleted on the kart Forgejo only",
            (code, r.get("token_deleted"), sorted(kart.tokens), sorted(derby.tokens)), (200, True, ["2"], ["1"]))
        code, r = redeem(anon, k2.replace("-", ""), INSTALL_A)
        chk("  the install still cannot take a Kart Club code bound elsewhere", code, 409)
    finally:
        stop(proc)

    print("the derby kind removed from the settings")
    env = kinds_env(base_env, kart, derby, POL_DEV_ISSUER_KINDS="kart:Kart Club")
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        code, r = owner.call("api/devcodes")
        rows = {x["code"]: x for x in r["codes"]}
        chk("its page: one kind; the derby codes listed as a kind not configured",
            ([k["key"] for k in r["kinds"]], rows[d1]["kind_ok"], rows[d2]["kind_ok"], rows[k2]["kind_ok"]),
            (["kart"], False, False, True))
        code, r = redeem(anon, d2, INSTALL_C)
        chk("redeem an unused derby code: 503 with a sentence", (code, r.get("error")),
            (503, "This panel does not hand out that kind of dev code at the moment. Try again later, or "
                  "ask the people who gave you the code."))
        code, r = redeem(anon, d1, INSTALL_A)
        chk("  and the used one, even from its own install: 503", code, 503)
        code, r = owner.call("api/devcodes", {"action": "make", "count": 1, "note": "x", "kind": "derby"})
        chk("making derby codes: 400", code, 400)
        n = audit_top()
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": d1})
        chk("revoke the used derby code: 200, revoked, and it says the token was not deleted",
            (code, r.get("token_deleted"), "was not deleted" in (r.get("warning") or ""),
             "openlobby-dev-derby-" + devchannel.short_code(d1) in (r.get("warning") or ""), sorted(derby.tokens)),
            (200, False, True, True, ["1"]))
        chk("  in the activity log", audit_since(n)[0][1:], ("revoked the dev access code for Derby testers", True))
        rows = {x["code"]: x for x in owner.call("api/devcodes")[1]["codes"]}
        chk("  listed revoked, with its token left", (rows[d1]["state"], rows[d1]["token_left"]), ("revoked", True))
        chk("  redeeming it now: 403", redeem(anon, d1, INSTALL_A)[0], 403)
        code, r = owner.call("api/devcodes", {"action": "revoke", "code": d2})
        chk("revoke the unused derby code: 200, no warning", (code, r.get("warning")), (200, None))
    finally:
        stop(proc)

    print("kinds that cannot be used")
    env = kinds_env(base_env, kart, derby,
                    POL_DEV_ISSUER_KINDS="kart:Kart Club,derby:Derby Night,polo:Polo Cup,Golf,9x:Nine",
                    POL_DEV_ISSUER_DERBY_SCOPES="read:package,write:package",
                    POL_DEV_ISSUER_POLO_CODE=KART_BASE,
                    POL_DEV_ISSUER_POLO_FORGEJO_URL=kart.url, POL_DEV_ISSUER_POLO_FORGEJO_USER=kart.user,
                    POL_DEV_ISSUER_POLO_FORGEJO_AUTH="%s:%s" % (kart.user, kart.password))
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        owner, anon = Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        code, r = owner.call("api/devcodes")
        ks = {k["key"]: k for k in r.get("kinds") or []}
        chk("a scope outside read:package and read:repository: the kind issues nothing, and says so",
            (ks["derby"]["issuing"], "POL_DEV_ISSUER_DERBY_SCOPES may name only read:package and read:repository"
             in ks["derby"]["why"], "write:package" in ks["derby"]["why"]), (False, True, True))
        chk("two kinds sharing one Forgejo user: the second refused, and says why",
            (ks["kart"]["issuing"], ks["polo"]["issuing"], "same Forgejo user as Kart Club" in ks["polo"]["why"]),
            (True, False, True))
        chk("entries that are not key:Label: a sentence each", (sorted(ks), len(r.get("problems") or []),
                                                                all("POL_DEV_ISSUER_KINDS" in p for p in r["problems"])),
            (["derby", "kart", "polo"], 2, True))
        code, r = owner.call("api/devcodes", {"action": "make", "count": 1, "note": "later", "kind": "derby"})
        chk("  a derby code can be made for later", code, 200)
        chk("  but not redeemed (503), while a kart one still works",
            (redeem(anon, r["codes"][0], INSTALL_C)[0], redeem(anon, k2, INSTALL_B)[0]), (503, 200))
        sql("DELETE FROM dev_codes")
    finally:
        stop(proc)

    print("migration 0022")
    sql("INSERT INTO dev_codes (code, note, created_at) VALUES ('ABCDEFGHJKMN', 'old', 0)")
    chk("a code made without a kind is of the unnamed kind",
        sql("SELECT kind FROM dev_codes WHERE code = 'ABCDEFGHJKMN'"), [{"kind": ""}])
    try:
        sql("INSERT INTO dev_codes (code, kind, note, created_at) VALUES ('ABCDEFGHJKMP', 'Kart', 'x', 0)")
        chk("a kind key that does not fit is refused by the table", "inserted", "refused")
    except Exception:          # noqa: BLE001
        chk("a kind key that does not fit is refused by the table", "refused", "refused")
    sql("DELETE FROM dev_codes")


def limited(tmp, base_env):
    print("five tries a minute from one address")
    env = dict(base_env, POL_DEV_ISSUER_CODE=BASE)
    env.pop("POL_ADMIN_PAIR_TRIES", None)
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=env)
    try:
        anon = Client(url)
        codes = [redeem(anon, "ZZZZ-ZZZZ-ZZZZ", INSTALL_A)[0] for _ in range(5)]
        chk("five unknown codes: 404 each", codes, [404] * 5)
        code, r = redeem(anon, "ZZZZ-ZZZZ-ZZZZ", INSTALL_A)
        chk("the sixth: 429, with how long to wait", (code, "Try again in" in r.get("error", "")), (429, True))
    finally:
        stop(proc)


def redeem_via(url, xff, code="ZZZZ-ZZZZ-ZZZZ"):
    """A redeem with X-Forwarded-For set (a list sends the header once per entry)."""
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    c.putrequest("POST", "/api/dev-code/redeem")
    body = json.dumps({"code": code, "install_id": INSTALL_A}).encode()
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(body)))
    for one in ([xff] if isinstance(xff, str) else xff or []):
        c.putheader("X-Forwarded-For", one)
    c.endheaders(body)
    r = c.getresponse()
    status = r.status
    r.read()
    c.close()
    return status


def audit_addrs_since(n):
    return [r["addr"] for r in sql("SELECT * FROM admin_audit WHERE id > %s ORDER BY id", (n,))]


def forwarded(tmp, base_env):
    print("behind a reverse proxy (POL_ADMIN_TRUSTED_PROXY)")
    env = dict(base_env, POL_DEV_ISSUER_CODE=BASE)
    env.pop("POL_ADMIN_PAIR_TRIES", None)
    proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(env, POL_ADMIN_TRUSTED_PROXY="127.0.0.1"))
    try:
        n = audit_top()
        codes = [redeem_via(url, "203.0.113.5") for _ in range(5)]
        chk("from the trusted proxy: five tries for one forwarded client, 404 each", codes, [404] * 5)
        chk("  the sixth from that client: 429", redeem_via(url, "203.0.113.5"), 429)
        chk("  another client through the same proxy is not limited by it", redeem_via(url, "198.51.100.7"), 404)
        chk("  nor is the proxy itself (no header)", redeem_via(url, None), 404)
        chk("  the activity log has the clients' own addresses",
            audit_addrs_since(n), ["203.0.113.5"] * 5 + ["198.51.100.7", "127.0.0.1"])
        n = audit_top()
        chk("  a client that writes its own header in front: the proxy's last entry counts",
            (redeem_via(url, "192.0.2.1, 192.0.2.50"), redeem_via(url, ["192.0.2.2", "192.0.2.51"]),
             audit_addrs_since(n)), (404, 404, ["192.0.2.50", "192.0.2.51"]))
        n = audit_top()
        chk("  a header that is not an address: the peer's own",
            (redeem_via(url, "not-an-address"), audit_addrs_since(n)), (404, ["127.0.0.1"]))
    finally:
        stop(proc)
    for what, extra in (("unset", {}), ("another address", {"POL_ADMIN_TRUSTED_PROXY": "192.0.2.200"}),
                        ("not one address", {"POL_ADMIN_TRUSTED_PROXY": "127.0.0.0/8"})):
        proc, url = start_panel(tmp, SERVICES, free_port(), env_extra=dict(env, **extra))
        try:
            n = audit_top()
            codes = [redeem_via(url, "203.0.113.%d" % i) for i in range(6)]
            chk("trusted proxy %s: X-Forwarded-For is ignored, the sixth try is 429 whatever it says" % what,
                codes, [404] * 5 + [429])
            chk("  the activity log has the connection's address", set(audit_addrs_since(n)), {"127.0.0.1"})
        finally:
            stop(proc)


def main():
    seed()
    tmp = tempfile.mkdtemp(prefix="admindevcodes-")
    base_env = {"POL_ADMIN_BIND": "127.0.0.1", "POL_ADMIN_PAIR_TRIES": "1000", "POL_ADMIN_LIVE_TTL": "0",
                "POL_ADMIN_GITHUB_API": "http://127.0.0.1:%d" % free_port(), "POL_ADMIN_UPDATE_REPO": "off"}
    fj = Forgejo()
    try:
        not_issuing(tmp, base_env)
        shared(tmp, base_env)
        minting(tmp, base_env, fj, "%s:%s" % (READER, READER_PW))
        minting(tmp, base_env, fj, "siteadmin:admin password 1")
        kinds(tmp, base_env, Forgejo(), Forgejo(DERBY_READER, DERBY_PW, "derby-token"))
        limited(tmp, base_env)
        forwarded(tmp, base_env)
    finally:
        fj.stop()
    print("admin_devcodes_test: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
