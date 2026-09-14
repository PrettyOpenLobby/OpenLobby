"""Server primitives shared by the POL responders: config, logging,
stderr capture, hex dump, packet capture, and the login trace ring.

Split out of responders.py (2026-08-27) as pure motion -- no behaviour
change. Depends on nothing else in services/; everything else may depend
on it. stub.py carries a deliberate duplicate of the log rotation.
"""

import datetime
import os
import sys
import threading
import time

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# Behaviour defaults verified against real clients. Each is still an
# environment variable (docker-compose or .env overrides any of them); these
# are simply the values a working deployment uses, so a stock bring-up needs
# none of them spelled out. Values here are applied only where the environment
# does not already set the variable.
RELEASE_DEFAULTS = {
    "POL_AUTH_MODE": "welcome",       # final auth hop accepts (vs. redirect loop)
    "POL_AUTH_OBSERVE": "300",
    "POL_LOG_HEX": "0",
    "POL_LOBBY_PORTS": "51200-51239,51251-51259,51262-51271,51273-51304,51306-51310",
    "POL_PORTAL_AUTH": "1",
    "POL_LOBBY_EMIT": "derive",
    "POL_LOBBY_FOLLOW": "180",
    "POL_LOBBY_LINGER": "60",
    "POL_LOBBY_PAYLEN": "4:1=128,4:0=128",
    "POL_LOBBY_TAIL": "3:0=acct0",
    "POL_SEARCH_RECORD": "600",
    # No POL_RESOURCE_PAYLEN here: a title declares the lengths of its own
    # lobby lists (titles.Title.resource_length); the core carries none.
    "POL_LOBBY_LIST_MODE": "0:7=handles,0:9=handles,1:3=chars,2:3=friends,7:12=groups",
    "POL_LOBBY_CONTENT_IDS": "1,2,4,11,14",
    "POL_ACCOUNTS_ENFORCE": "1",      # unknown NICKs are refused; accounts come from sign-up or the admin panel
    "POL_ACCOUNTS_DB": "/data/accounts.db",
    "POL_AUTH_FRONT_PREAMBLE": "1",  # authrelay announces the real client address; consume it
    "POL_PRESENCE_PUSH": "1",
    "POL_FRIEND_ROW_PUSH": "1",
    "POL_FFXI_IDMAP": "/data/ffxi_idmap.json",  # written by the FFXI bridge on the shared data volume
}
for _k, _v in RELEASE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

CONFIG_PATH = os.environ.get("POL_CONFIG", "/config/server.yaml")
LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")


# --------------------------------------------------------------------------- #
# shared helpers (kept tiny; mirrors stub.py conventions)
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        if yaml is None:
            raise SystemExit("pyyaml is required; add it to the image")
        return yaml.safe_load(f)


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


#: Log files are ROTATED, and they did not used to be. Measured on the live
#: deployment 2026-08-12: 769 MB under /logs, of which tcp.log alone was 405 MB
#: -- one server-lifetime append with no cap, on the same disk the account
#: database lives on. Two knobs, both sized for "a long weekend of play" rather
#: than for an RE session: POL_LOG_MAX_MB per file, POL_LOG_KEEP older
#: generations. Set POL_LOG_MAX_MB=0 to go back to appending forever.
_LOG_LOCK = threading.Lock()
_LOG_FILES = {}                 # channel -> open handle
_LOG_MAX = int(float(os.environ.get("POL_LOG_MAX_MB", "8")) * 1024 * 1024)
_LOG_KEEP = int(os.environ.get("POL_LOG_KEEP", "3"))


def _log_handle_locked(channel):
    """The open handle for `channel`, rotating it first if it has grown past the
    cap. Caller holds _LOG_LOCK.

    The handle is kept OPEN between lines: this used to open, append and close
    once per line, and a busy lobby logs several lines per message.
    """
    f = _LOG_FILES.get(channel)
    path = os.path.join(LOG_DIR, f"{channel}.log")
    # SIZE IS CHECKED ON THE FIRST WRITE TOO, not only on an already-open handle.
    # The `f is not None` guard this replaces meant a log inherited OVERSIZED
    # from the previous run was never rotated -- it had to grow by another whole
    # cap in-process first. That is not a corner case: after any restart every
    # log on the box is in exactly that state, so the cap appeared to work on a
    # fresh deployment and did nothing on a long-lived one.
    try:
        size = f.tell() if f is not None else os.path.getsize(path)
    except OSError:
        size = 0
    if _LOG_MAX > 0 and size >= _LOG_MAX:
        if f is not None:
            f.close()
            _LOG_FILES.pop(channel, None)
        f = None
        for i in range(_LOG_KEEP, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            dst = f"{path}.{i}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    break
    if f is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        f = open(path, "a", encoding="utf-8")
        _LOG_FILES[channel] = f
    return f


def log(channel, msg):
    line = f"{_stamp()} [{channel}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A LOG LINE MUST NEVER TAKE DOWN THE REQUEST THAT WROTE IT. Half of what
        # we log is client-supplied bytes decoded as cp932 -- a junk name field,
        # a Japanese subject -- and stdout is cp1252 whenever a tool runs on the
        # Windows host rather than in the container. That raise happened inside
        # the 2:6 reply builder, i.e. it would have cost the client its reply and
        # looked exactly like the hang we were there to fix.
        enc = (sys.stdout.encoding or "ascii")
        print(line.encode(enc, "backslashreplace").decode(enc), flush=True)
    try:
        with _LOG_LOCK:
            f = _log_handle_locked(channel)
            f.write(line + "\n")
            f.flush()
    except OSError:
        pass


#: WARNING: STDERR WAS THE ONE STREAM A DEPLOY COULD DESTROY. `log()` mirrors every
#: channel into `<LOG_DIR>/<channel>.log`, so the detail survives a container
#: recreate -- but a WARNING, an uncaught traceback, and anything a library
#: prints itself go to **stderr**, which only ever reached the container's
#: stdout. `pol-git-sync` recreates login/authsess on every deploy, and
#: `docker logs` then starts EMPTY: the one class of output you most want after
#: an incident is the one class that does not survive it.
#:
#: Measured 2026-08-25: `authserv.log` held all 4,606 lines of the window around
#: a mid-match restart and **zero** of the warnings/tracebacks, which existed
#: only in the (now-replaced) container's stdout. A crash in the minutes before
#: a restart is unreadable afterwards -- exactly backwards.
#:
#: Everything written to stderr is now ALSO appended, line by line, to
#: `<LOG_DIR>/stderr.log` through the same rotating handles as every other
#: channel. The default `sys.excepthook` and `threading.excepthook` both write
#: through `sys.stderr`, so uncaught exceptions -- including ones that kill a
#: worker thread -- are captured BY CONSTRUCTION, with no hook of our own to
#: keep in step with CPython. POL_LOG_STDERR=0 restores the old behaviour.
class _StderrTee:
    """Write-through wrapper: the real stderr still gets everything, verbatim."""

    def __init__(self, real):
        self._real = real
        self._buf = ""

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        # LINE-BUFFERED ON OUR SIDE ONLY. A traceback arrives as many small
        # writes; stamping each one would shred it across the log.
        try:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                _stderr_capture(line)
        except Exception:
            pass                        # never let logging break the writer
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass

    # Some libraries probe these before they will write at all.
    def isatty(self):
        return False

    def fileno(self):
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


def _stderr_capture(line):
    """One stderr line -> the `stderr` channel file. NEVER raises, and never
    writes to stderr itself -- that would recurse through the tee."""
    if not line.strip():
        return
    try:
        with _LOG_LOCK:
            f = _log_handle_locked("stderr")
            f.write(_stamp() + " [stderr] " + line + "\n")
            f.flush()
    except Exception:
        pass


def install_stderr_capture():
    """Install the tee. Called from `main()`, deliberately NOT at import time:
    `tools/` and the selftests import this module, and a tool has no business
    creating `/logs` or redirecting the operator's console."""
    if os.environ.get("POL_LOG_STDERR", "1") != "1":
        return
    if isinstance(sys.stderr, _StderrTee):
        return                          # idempotent
    sys.stderr = _StderrTee(sys.stderr)


def hexdump(data, limit=1024):
    # POL_LOG_HEX=0 silences every hexdump in one place. They are this project's
    # main research instrument, so they stay ON by default -- but they are also
    # most of the log volume, and an operator running the server to PLAY on has
    # no use for them.
    if os.environ.get("POL_LOG_HEX", "1") != "1":
        return f"    [{len(data)}B, hexdump off]"
    data = data[:limit]
    out = []
    for i in range(0, len(data), 16):
        c = data[i:i + 16]
        h = " ".join(f"{x:02x}" for x in c)
        a = "".join(chr(x) if 32 <= x < 127 else "." for x in c)
        out.append(f"    {i:04x}  {h:<47}  {a}")
    return "\n".join(out)


#: Captures are BUDGETED per name, newest kept. They were unbounded: one file
#: per lobby message, 13,283 files / 336 MB on the live box by 2026-08-12. They
#: earned their keep while the protocol was being reversed and are pure cost on a
#: server that is running to be played on -- so: POL_CAPTURE=0 turns them off,
#: POL_CAPTURE_KEEP caps how many of each name survive.
_CAP_LOCK = threading.Lock()
_CAP_SEEN = {}                  # name -> [paths, oldest first]
_CAP_KEEP = int(os.environ.get("POL_CAPTURE_KEEP", "64"))


# --------------------------------------------------------------------------- #
# THE LOGIN TRACE -- so the NEXT failure is an answer, not another sighting.
#
# `pol0008-keepalive` records five POL-0008 generators, all found and fixed, and
# ONE that is still open: a sign-in where neither K=0 nor any session key we ever
# issued decrypts the client's NICK --
#
#     could not recover IV (nick_ct=...); K=0 and N issued session key(s)
#     all failed the crib
#
# -- which the user sees as POL-2059. Two dials fail, the third logs in instantly.
# The client is holding a Blowfish key we apparently never issued for that
# address, which should be impossible: we mint every stamp, and the stamp we
# greeted that very dial with is IN the candidate list.
#
# **Every occurrence so far has been unfalsifiable after the fact**, because the
# log line above throws away exactly the evidence needed to settle it. It prints
# eight bytes of ciphertext and a COUNT. It does not print the stamp we greeted
# this dial with, the key derived from it, the candidates actually tried, or --
# the one that quietly matters most -- whether the list was TRUNCATED. It is
# capped at `POL_STAMP_TRY` (12), so "N session key(s) all failed" cannot
# distinguish "we tried everything we ever issued" from "we tried the newest 12
# of 35", and those two have opposite conclusions.
#
# So: every login builds a small in-memory trace, and a login that FAILS writes
# it out whole, with the full ciphertext and the entire candidate set. Successful
# logins cost one list append per step and write nothing -- this is deliberately
# not a log level, because the interesting event is rare and the volume of a
# healthy login is not worth carrying.
#
#     POL_LOGIN_TRACE=fail   (default) dump only when the login fails
#     POL_LOGIN_TRACE=all              dump every login, for a deliberate capture
#     POL_LOGIN_TRACE=0                off entirely
#
# The dump lands beside the packet captures, in `logs/captures/`, and the log
# gets a one-line pointer to it.
#
# WARNING: **WHAT THIS DOES NOT COVER, so nobody reads its silence as evidence.** The
# dumps fire at an OUTCOME (welcome / reject / no-key) and, failing that, from
# `handle_authserv`'s `finally`. A thread BLOCKED forever reaches neither: its
# `finally` does not run, and if the process is then killed nothing runs at all.
# So a login that truly hangs still leaves no trace, and "no dump" therefore
# means "no outcome AND no clean teardown" -- not "nothing went wrong".
#
# Closing that last gap needs a watchdog (a registry of in-flight traces, swept
# by a daemon that dumps anything older than N seconds) rather than a `finally`.
# Deliberately not built: the failure that motivated it stopped reproducing on
# this box after ~36 consecutive clean runs, and a watchdog written against a
# symptom nobody can currently trigger is a guess with a thread attached.
_LOGIN_TRACE = threading.local()
_LOGIN_TRACE_MAX = 200


def _arm_stack_dumps():
    """`POL_STACK_DUMP=<seconds>`: print EVERY thread's stack, repeatedly.

    The one thing the login trace cannot see is a thread that is BLOCKED: its
    `finally` never runs, so no dump is written and the log simply stops. That
    is the shape of `resume_test`'s intermittent failure -- authserv logs the
    NICK and then nothing, with no traceback on stderr and no line in
    accounts.log -- and it is unanswerable from the outside.

    `faulthandler` answers it directly and costs nothing when unset: a timer
    thread in C that writes the stack of every thread, naming the exact line the
    blocked one is parked on. Repeating, because the interesting moment is the
    SECOND dump -- a thread on the same line in two consecutive dumps is stuck,
    while one that moved was merely slow.

    Off unless the variable is set. Output goes to stderr, which for the server
    containers is the log, and for `resume_test` is the file it now keeps.
    """
    secs = float(os.environ.get("POL_STACK_DUMP", "0") or 0)
    if secs <= 0:
        return
    try:
        import faulthandler
        faulthandler.dump_traceback_later(secs, repeat=True, exit=False)
        log("resp", f"stack dumps armed: every {secs:g}s to stderr "
                    "(POL_STACK_DUMP) -- a thread on the same line in two "
                    "consecutive dumps is BLOCKED, one that moved was slow")
    except Exception as exc:                       # never block a start-up
        log("resp", f"stack dumps not armed ({exc!r})")


def _trace_begin(peer, port):
    """Start (or restart) this thread's login trace."""
    if os.environ.get("POL_LOGIN_TRACE", "fail") == "0":
        _LOGIN_TRACE.rows = None
        return
    _LOGIN_TRACE.rows = [(time.time(), "connect", f"{peer} on :{port}")]
    _LOGIN_TRACE.done = False


def _trace(event, detail=""):
    """Record one step of the login now in progress. Never raises."""
    rows = getattr(_LOGIN_TRACE, "rows", None)
    if rows is None:
        return
    if len(rows) < _LOGIN_TRACE_MAX:
        rows.append((time.time(), event, str(detail)))


def _trace_done():
    """Has this login already written its trace out?"""
    return getattr(_LOGIN_TRACE, "done", False)


def _trace_dump(peer, why, extra=None):
    """Write the trace out. Returns the path, or None if there was nothing.

    `extra` is a list of (heading, text) blocks appended after the timeline --
    the candidate keys and the raw ciphertext go there, because they are the
    things an offline attempt to identify the client's key actually needs.
    """
    rows = getattr(_LOGIN_TRACE, "rows", None)
    if not rows:
        return None
    mode = os.environ.get("POL_LOGIN_TRACE", "fail")
    if mode == "0" or (mode != "all" and why == "ok"):
        return None
    _LOGIN_TRACE.done = True
    try:
        d = os.path.join(LOG_DIR, "captures")
        os.makedirs(d, exist_ok=True)
        safe = peer.replace(":", "_").replace("/", "_")
        path = os.path.join(d, f"login-{why}-{safe}-"
                                f"{_stamp().replace(':', '')}.txt")
        t0 = rows[0][0]
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"login trace: {peer}  result={why}\n")
            f.write(f"began {datetime.datetime.utcfromtimestamp(t0)}Z\n\n")
            for t, event, detail in rows:
                f.write(f"  +{(t - t0) * 1000:8.1f}ms  {event:<22} {detail}\n")
            for heading, text in (extra or []):
                f.write(f"\n--- {heading} ---\n{text}\n")
        # BUDGETED, newest kept, per outcome -- like save_capture, which this
        # deliberately does not go through (the name carries the peer and the
        # outcome, so one budget per name would be one per connection). Prod
        # held 1,796 of these by 2026-09-05; pol-tmpclean only sweeps /tmp.
        # POL_LOGIN_TRACE_KEEP=0 keeps everything.
        keep = int(os.environ.get("POL_LOGIN_TRACE_KEEP", "64") or 0)
        if keep > 0:
            import glob as _glob
            olds = sorted(_glob.glob(os.path.join(d, f"login-{why}-*.txt")),
                          key=lambda q: os.path.getmtime(q))
            for old in olds[:-keep]:
                try:
                    os.unlink(old)
                except OSError:
                    pass
        return path
    except OSError as exc:
        log("authserv", f"{peer} login trace not written ({exc})")
        return None


def save_capture(name, data):
    if os.environ.get("POL_CAPTURE", "1") != "1":
        return None
    try:
        d = os.path.join(LOG_DIR, "captures")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{name}-{_stamp().replace(':', '')}.bin")
        with open(path, "wb") as f:
            f.write(data)
        with _CAP_LOCK:
            known = _CAP_SEEN.get(name)
            if known is None:
                # First save of this name in this process: adopt whatever earlier
                # runs left behind, so the budget covers those too rather than
                # starting a fresh pile beside them.
                import glob as _glob
                known = sorted(_glob.glob(os.path.join(d, f"{name}-*.bin")))
                _CAP_SEEN[name] = known
            if path not in known:
                known.append(path)
            while _CAP_KEEP > 0 and len(known) > _CAP_KEEP:
                old = known.pop(0)
                try:
                    os.unlink(old)
                except OSError:
                    pass
        return path
    except OSError as e:
        log("resp", f"capture save failed: {e}")
        return None


def expand_ports(spec):
    """Expand a list that may contain "a-b" range strings into ints."""
    out = []
    for item in spec:
        if isinstance(item, str) and "-" in item:
            a, b = item.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(item))
    return out
