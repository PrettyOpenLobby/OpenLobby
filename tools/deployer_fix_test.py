#!/usr/bin/env python3
"""The deployer's known problems and their fixes (deployer.py, "known problems"), against a real
MariaDB in Docker.

    python tools/deployer_fix_test.py

A throwaway mariadb:11.4 container is created with the user's first password, the way
LandSandBoat's db service is; then the FFXI .env is given another one, the case a tester hit
(the world logged "Access denied for user 'xiadmin'"). Checks that:
  * the catalog's diagnose row is found in a service log that says it, and not in one that
    does not;
  * the fix signs in as root and sets the user's password to the one in .env, a password with
    quotes, a backslash, a dollar sign and a space included, after which that password works
    and the first one does not;
  * neither password is in the job log or on any command line;
  * a root password that was changed too stops the fix with a sentence that says so, and
    changes nothing;
  * POST /api/actions refuses a fix the game's catalog does not name.
Skipped when Docker does not answer.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services", "deployer"))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

import deployer as dep  # noqa: E402

IMAGE = "mariadb:11.4"
FIRST, NEW, ROOT = "change-me", "n3w p'a\\ss\"$x", "root-pw-1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def docker(*args, **kw):
    return subprocess.run(["docker"] + list(args), capture_output=True, text=True, **kw)


def can_login(ctr, user, pw):
    r = subprocess.run(["docker", "exec", "-i", ctr, "sh", "-c", 'IFS= read -r P; MYSQL_PWD="$P" exec mariadb -u"$1" -e "SELECT 1"',
                        "sh", user], input=(pw + "\n").encode("utf-8"), capture_output=True)
    return r.returncode == 0


class Log:
    def __init__(self):
        self.text, self.secrets = "", []

    def line(self, s):
        self.write(s + "\n")

    def write(self, s):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        for sec in self.secrets:
            s = s.replace(sec.decode("utf-8"), "[access code]")
        self.text += s

    blank = dep.JobLog.blank


class Runner:
    """deployer.Runner's parts the fix uses; compose goes to the test container."""

    def __init__(self, ctr, env, catalog):
        self.ctr, self.env, self.catalog, self.log = ctr, env, catalog, Log()
        self.argvs, self.recreated = [], []

    def name(self, key):
        return self.catalog[key]["name"]

    def enabled(self):
        return list(self.catalog)

    def game_env(self, key):
        return dict(self.env)

    def compose_input(self, args, data, timeout=120):
        assert args[:3] == ["exec", "-T", "db"], args
        argv = ["docker", "exec", "-i", self.ctr] + list(args[3:])
        self.argvs.append(argv)
        self.log.line("$ " + " ".join(argv) + "   (its input is not shown)")
        p = subprocess.run(argv, input=data.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.log.write(p.stdout)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def compose(self, args, prebuild=True):
        self.recreated.append(list(args))


def main():
    if docker("info").returncode != 0:
        print("SKIP deployer_fix_test: Docker does not answer")
        return 0
    catalog = {"ffxi": dep.load_catalog(os.path.join(HERE, os.pardir))["ffxi"]}
    print("the catalog names the problem")
    rows = dep.diagnoses(catalog["ffxi"])
    chk("ffxi's diagnose row has a fix here", [(d["id"], d["fix"]) for d in rows], [("db-password", "lsb-db-password")])
    d = rows[0]
    log_line = "!!! Failed to connect to database, terminating server !!!\nAccess denied for user 'xiadmin'@'192.0.2.16' (using password: YES)"
    chk("  its text is in the world's crash", d["match"] in log_line, True)
    chk("  and not in a healthy start", d["match"] in "Begin world-server init...\n64-bit environment detected", False)

    ctr = "openlobby-fixtest-%d" % os.getpid()
    if docker("run", "-d", "--name", ctr, "-e", "MARIADB_ROOT_PASSWORD=" + ROOT, "-e", "MARIADB_DATABASE=xidb",
              "-e", "MARIADB_USER=xiadmin", "-e", "MARIADB_PASSWORD=" + FIRST, IMAGE).returncode != 0:
        print("SKIP deployer_fix_test: could not start %s" % IMAGE)
        return 0
    try:
        t0 = time.time()
        while time.time() - t0 < 120 and not can_login(ctr, "xiadmin", FIRST):
            time.sleep(2)
        chk("the database starts with the first password", can_login(ctr, "xiadmin", FIRST), True)

        print("the root password changed too: refused, nothing changed")
        r = Runner(ctr, {"LSB_DB_USER": "xiadmin", "LSB_DB_PASSWORD": NEW, "LSB_DB_ROOT_PASSWORD": "not-the-root"}, catalog)
        try:
            dep.fix_lsb_db_password(r, "ffxi", d)
            got = "no error"
        except dep.JobFailed as e:
            got = e.summary
        chk("  it says root was changed as well", "refused its root password too" in got, True)
        chk("  the first password still works", can_login(ctr, "xiadmin", FIRST), True)
        chk("  nothing restarted", r.recreated, [])

        print("the fix")
        r = Runner(ctr, {"LSB_DB_USER": "xiadmin", "LSB_DB_PASSWORD": NEW, "LSB_DB_ROOT_PASSWORD": ROOT}, catalog)
        state, summary, _m = dep.fix_lsb_db_password(r, "ffxi", d)
        chk("  it reports success", state, "ok")
        chk("  the .env password now works", can_login(ctr, "xiadmin", NEW), True)
        chk("  the first one no longer does", can_login(ctr, "xiadmin", FIRST), False)
        chk("  the FFXI servers are recreated", r.recreated, [["up", "-d", "--force-recreate", "connect", "search", "world", "map"]])
        chk("  neither password is in the log", (NEW in r.log.text, ROOT in r.log.text), (False, False))
        chk("  nor on any command line", any(NEW in a or ROOT in a for argv in r.argvs for a in argv), False)
        chk("  the job says what it did", "now takes the password in .env" in summary, True)

        print("refusals")
        for env, want in (({"LSB_DB_USER": "x'; DROP", "LSB_DB_PASSWORD": NEW, "LSB_DB_ROOT_PASSWORD": ROOT}, "not a plain"),
                          ({"LSB_DB_PASSWORD": "", "LSB_DB_ROOT_PASSWORD": ROOT}, "needs LSB_DB_PASSWORD")):
            try:
                dep.fix_lsb_db_password(Runner(ctr, env, catalog), "ffxi", d)
                got = ""
            except dep.JobFailed as e:
                got = e.summary
            chk("  %s" % want, want in got, True)
        try:
            dep.act_fix(Runner(ctr, {}, catalog), "ffxi", {"fix": "reboot-everything"})
            got = ""
        except dep.JobFailed as e:
            got = e.summary
        chk("  a fix the catalog does not name", "no known problem" in got, True)
    finally:
        docker("rm", "-f", ctr)
    print("deployer_fix_test: %d failed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
