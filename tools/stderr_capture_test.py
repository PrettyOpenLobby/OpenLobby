#!/usr/bin/env python3
"""Does stderr survive a container recreate?

`responders.log()` mirrors every channel into `<LOG_DIR>/<channel>.log`, so the
detail stream outlives a `docker compose up --force-recreate`. **stderr did
not.** Warnings, uncaught tracebacks and anything a library prints itself went
only to the container's stdout, and `pol-git-sync` recreates login/authsess on
every deploy -- so `docker logs` starts empty and the one class of output you
most want after an incident is the one class that does not survive it.

Measured 2026-08-25: around a mid-match restart, `authserv.log` held all 4,606
lines of the window and **zero** warnings/tracebacks.

These checks pin the tee: stderr still reaches the real stderr byte for byte,
AND every line of it lands in `<LOG_DIR>/stderr.log`.
"""
import io
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")

ok = True


def check(name, cond, detail=""):
    global ok
    if cond:
        print("  [PASS] %s" % name)
    else:
        ok = False
        print("  [FAIL] %s%s" % (name, ("  --  " + detail) if detail else ""))


# The tee has to be exercised in a CHILD process: it replaces sys.stderr, and a
# test that did it in-process would be measuring its own harness.
CHILD = r'''
import os, sys, warnings, threading
sys.path.insert(0, %r)
import responders
responders.install_stderr_capture()
sys.stderr.write("PLAIN-STDERR-LINE\n")
warnings.warn("A-WARNING-ON-STDERR")
def boom():
    raise RuntimeError("THREAD-TRACEBACK-MARKER")
t = threading.Thread(target=boom)
t.start(); t.join()
sys.stderr.flush()
''' % (SERVICES,)

print("stderr capture ->")

with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ)
    env["POL_LOG_DIR"] = tmp
    env["POL_LOG_STDERR"] = "1"
    proc = subprocess.run([sys.executable, "-c", CHILD], env=env,
                          capture_output=True, text=True)

    path = os.path.join(tmp, "stderr.log")
    on_disk = ""
    if os.path.exists(path):
        on_disk = io.open(path, encoding="utf-8").read()

    check("the stderr channel file is created", os.path.exists(path), path)
    # WARNING: WRITE-THROUGH FIRST. A tee that captured but swallowed would be a
    # regression dressed as a fix -- the admin's console must be unchanged.
    check("a plain stderr write still reaches the REAL stderr",
          "PLAIN-STDERR-LINE" in proc.stderr, repr(proc.stderr[:200]))
    check("a plain stderr write is ALSO on disk",
          "PLAIN-STDERR-LINE" in on_disk, repr(on_disk[:200]))
    check("a warnings.warn() is on disk",
          "A-WARNING-ON-STDERR" in on_disk, repr(on_disk[:300]))
    # THE ONE THAT MATTERS: this server is all threads, and a thread that dies
    # takes its traceback with it. threading.excepthook writes through
    # sys.stderr, so the tee gets it with no hook of ours to keep in step.
    check("an uncaught THREAD traceback is on disk",
          "THREAD-TRACEBACK-MARKER" in on_disk, repr(on_disk[-400:]))
    check("the traceback is not shredded across lines (Traceback header kept)",
          "Traceback (most recent call last)" in on_disk)
    # Every captured line carries the same stamp shape as every other channel.
    lines = [l for l in on_disk.splitlines() if l.strip()]
    check("captured lines are stamped and tagged [stderr]",
          bool(lines) and all("[stderr]" in l for l in lines),
          repr(lines[:3]))
    check("no blank lines are logged",
          all(l.strip() for l in lines))

    # ...and the revert lever really reverts.
    env["POL_LOG_STDERR"] = "0"
    proc2 = subprocess.run([sys.executable, "-c", CHILD], env=env,
                           capture_output=True, text=True)
    off = os.path.join(tmp, "stderr.log")
    before = io.open(off, encoding="utf-8").read() if os.path.exists(off) else ""
    check("POL_LOG_STDERR=0 writes nothing new (the revert lever works)",
          before.count("PLAIN-STDERR-LINE") == 1,
          "count=%d" % before.count("PLAIN-STDERR-LINE"))
    check("POL_LOG_STDERR=0 still reaches the real stderr",
          "PLAIN-STDERR-LINE" in proc2.stderr)

print("\nstderr capture: %s" % ("OK" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
