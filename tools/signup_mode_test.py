#!/usr/bin/env python3
"""POL_SIGNUP_MODE: whether the in-client sign-up wizard demands a code.

    python tools/signup_mode_test.py

This drives the REAL `ucscgi.py` over HTTP -- two throwaway processes on two
throwaway databases, one per mode -- because the thing being tested is a screen
transition, and a check that only called the validator would pass while the
wizard still refused to advance.

It asserts BOTH directions, which is the point:

  * permissive -- an empty step 3 goes through, and the account that comes out
    holds POL_SIGNUP_CONTENTS, not register_account's FFXI-only default;
  * code (the default) -- a blank code is still refused, and a real code still
    decides the titles rather than being overridden by the new default set.

A mistyped code is refused in EITHER mode: "you do not need one" must not turn
into "whatever you type is ignored".
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.abspath(os.path.join(HERE, "..", "services"))
sys.path.insert(0, SVC)
import accounts                                              # noqa: E402
import pgtest                                                # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def get(port, query):
    url = "http://127.0.0.1:%d/pml-cgi-bin/?%s" % (port, query)
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def step_of(html):
    """Which screen the wizard answered with -- its own STEP n/6 chrome."""
    m = re.search(r"STEP (\d)/6", html)
    return int(m.group(1)) if m else None


def token_of(html):
    return re.search(r"t=([0-9a-f]{8,})", html).group(1)


def serve(mode, port, db):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", POL_DATABASE_URL=db,
               POL_LOG_DIR=WORK, POL_SIGNUP_MODE=mode,
               POL_SIGNUP_CONTENTS="1,2,3,4,10,11,14",
               POL_CONFIG=os.path.join(WORK, "server.yaml"))
    log = open(os.path.join(WORK, "ucs-%s.log" % mode), "w")
    p = subprocess.Popen(
        [sys.executable, "-u", "ucscgi.py", "--plain", "--port", str(port)],
        cwd=SVC, env=env, stdout=log, stderr=subprocess.STDOUT)
    time.sleep(2.5)
    if p.poll() is not None:
        sys.exit("ucscgi (%s) did not start:\n%s"
                 % (mode, open(log.name).read()[-2000:]))
    return p


WORK = tempfile.mkdtemp(prefix="pol-signup-mode-")
print("POL_SIGNUP_MODE=permissive")
DB1 = pgtest.use_fresh_database()
srv = serve("permissive", 8081, DB1)
try:
    tok = token_of(get(8081, "kinou_id=20&step=1"))
    page = get(8081, "kinou_id=20&step=3&t=" + tok)
    chk("step 3 says the code is optional",
        ("Have a registration code?" in page, "You do not need a code" in page),
        (True, True))
    chk("an EMPTY code goes through to step 4",
        step_of(get(8081, "kinou_id=20&step=4&t=" + tok)), 4)
    page = get(8081, "kinou_id=20&step=4&t=%s&rc0=NOPE&rc1=NOPE" % tok)
    chk("but a code that was TYPED is still checked",
        (step_of(page), "not valid" in page), (3, True))
    # that bad code is remembered in the session, so the real run starts clean
    tok = token_of(get(8081, "kinou_id=20&step=1"))
    get(8081, "kinou_id=20&step=4&t=" + tok)
    chk("password and handle reach the confirm screen", step_of(get(
        8081, "kinou_id=20&step=5&t=%s&pw1=abc12345&pw2=abc12345&handle=NoCode" % tok)), 5)
    get(8081, "kinou_id=20&step=6&t=" + tok)
    db = accounts.connect()
    row = db.execute("SELECT m.id FROM member m JOIN handle h ON h.member_id = m.id"
                     " WHERE h.handle_name = 'NoCode'").fetchone()
    chk("an account really was issued without a code", bool(row), True)
    chk("...granted POL_SIGNUP_CONTENTS, not FFXI alone",
        sorted(r[0] for r in db.execute(
            "SELECT content_code FROM content WHERE member_id = %s"
            " AND status = 'active'", (row[0],))) if row else None,
        [1, 2, 3, 4, 10, 11, 14])
    db.close()
finally:
    srv.kill()
    srv.wait()

print("POL_SIGNUP_MODE=code (the default)")
DB2 = pgtest.use_fresh_database()
srv = serve("code", 8082, DB2)
try:
    tok = token_of(get(8082, "kinou_id=20&step=1"))
    page = get(8082, "kinou_id=20&step=3&t=" + tok)
    chk("step 3 still asks for the code from the package",
        ("from your software package" in page, "You do not need a code" in page),
        (True, False))
    page = get(8082, "kinou_id=20&step=4&t=" + tok)
    chk("a blank code is STILL refused",
        (step_of(page), "Please enter your registration code" in page), (3, True))
    db = accounts.connect()
    accounts.issue_regcode(db, "AAAA-BBBB-CCCC-DDDD-EEEE", contents=(2, 3))
    db.close()
    chk("a real code passes step 3", step_of(get(
        8082, "kinou_id=20&step=4&t=%s&rc0=aaaa&rc1=bbbb&rc2=cccc&rc3=dddd&rc4=eeee"
        % tok)), 4)
    get(8082, "kinou_id=20&step=5&t=%s&pw1=abc12345&pw2=abc12345&handle=WithCode" % tok)
    get(8082, "kinou_id=20&step=6&t=" + tok)
    db = accounts.connect()
    row = db.execute("SELECT m.id FROM member m JOIN handle h ON h.member_id = m.id"
                     " WHERE h.handle_name = 'WithCode'").fetchone()
    chk("the CODE decides the titles, not the permissive default",
        sorted(r[0] for r in db.execute(
            "SELECT content_code FROM content WHERE member_id = %s"
            " AND status = 'active'", (row[0],))) if row else None, [2, 3])
    db.close()
finally:
    srv.kill()
    srv.wait()

print("FAILURES:", bad)
sys.exit(1 if bad else 0)
