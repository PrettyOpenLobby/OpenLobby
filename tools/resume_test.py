#!/usr/bin/env python3
"""Proof that KILLING authserv no longer kills a live client session.

This is the acceptance test for services/authrelay.py + handle_authresume. It
runs the real code, not a model of it:

  * authserv runs as a SUBPROCESS, because the thing being tested is what
    happens when that process DIES. A thread cannot be killed, and mocking the
    death would test the mock.
  * the front relay runs in-process here (it is the part that must SURVIVE).
  * the client is the same K=0 login drive smoke_chain.py uses -- our authserv
    mints token0, so the crypto path is the real one.

    python tools/resume_test.py            # PASS/FAIL per check, exit 0/1
    python tools/resume_test.py -v         # + the wire detail

What it does NOT prove: that the real Viewer is as patient with a silent socket
as we believe. Every measurement so far says it has no read timeout of its own
(sessions only ever died when the SERVER closed them -- see the POL_AUTH_PING
history), but the client is the client. Watch logs/authrelay.log on the first
live restart.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SERVICES = os.path.join(ROOT, "services")
sys.path.insert(0, SERVICES)

# Ports well clear of the live band (51200-51330) so this can run on the server
# box without fighting anything: the relay fronts CLIENT_PORT, authserv listens
# at +OFFSET, and the resume door is its own.
CLIENT_PORT = 51941
OFFSET = 200
RESUME_PORT = 52151

TMP = tempfile.mkdtemp(prefix="resume-test-")
ENV = dict(os.environ)
ENV.update({
    "POL_CONFIG":              os.path.join(ROOT, "config", "server.yaml"),
    "POL_LOG_DIR":             TMP,
    "POL_SESSION_FILE":        os.path.join(TMP, "auth-sessions.json"),
    "POL_STAMP_FILE":          os.path.join(TMP, "auth-stamps.json"),
    "POL_ACCOUNTS_DB":         os.path.join(TMP, "accounts.db"),
    "POL_AUTH_MODE":           "welcome",
    "POL_AUTH_CLOCK":          "0",     # a redirect-shaped greeting; one fewer hop
    "POL_AUTH_PORTS":          str(CLIENT_PORT),
    "POL_AUTH_PORT":           str(CLIENT_PORT),
    "POL_AUTH_LISTEN_OFFSET":  str(OFFSET),
    "POL_AUTH_RESUME_PORT":    str(RESUME_PORT),
    "POL_AUTH_RESUME_BIND":    "127.0.0.1",
    "POL_AUTH_FRONT_PREAMBLE": "1",
    "POL_AUTH_PING":           "60",
    "POL_ACCOUNTS_ENFORCE":    "0",
    "POL_PRESENCE_PUSH":       "0",
})
# authrelay reads its config at import, so the environment has to be right first.
os.environ.update({k: v for k, v in ENV.items() if k.startswith("POL_")})
os.environ["POL_RELAY_PORTS"] = str(CLIENT_PORT)
os.environ["POL_RELAY_OFFSET"] = str(OFFSET)
os.environ["POL_RELAY_RESUME_PORT"] = str(RESUME_PORT)
os.environ["POL_RELAY_RETRY_MS"] = "200"
os.environ["POL_RELAY_VERBOSE"] = "1"
os.environ["POL_RELAY_PREAMBLE"] = "1"

import sessioncrypt                                          # noqa: E402
import authrelay                                             # noqa: E402

K0_P, K0_S = sessioncrypt.bf_setkey(b"\x00" * 8)
IV = bytes.fromhex("1122334455667788")
NICK = b"UH5GRSV86"

FAILS = []
VERBOSE = False


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail and (VERBOSE or not ok) else ""))
    if not ok:
        FAILS.append(name)
    return ok


def note(msg):
    if VERBOSE:
        print("        . " + msg)


def enc(line):
    return sessioncrypt.ofb_apply(K0_P, K0_S, IV, sessioncrypt.frame_line(line))


def dec(line):
    return sessioncrypt.ofb_apply(K0_P, K0_S, IV, line)


def recv_idle(sock, idle=1.5, hard=8.0):
    """Read until the peer goes quiet for `idle` seconds. b'' means it closed."""
    end = time.time() + hard
    sock.settimeout(idle)
    out = b""
    while time.time() < end:
        try:
            c = sock.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not c:
            break
        out += c
    return out


def wait_port(port, timeout=20.0, host="127.0.0.1"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


#: Where the authserv subprocess's own stdout/stderr lands.
#:
#: IT USED TO GO TO DEVNULL, and that is why this suite's intermittent failure
#: has never been explainable. When it fails, `authserv.log` simply STOPS after
#: the NICK line -- no error, no traceback, nothing -- because a handler thread
#: that raises prints to stderr, and stderr was being thrown away. Measured
#: 2026-08-19: this suite fails about one run in four (11 of 44), with and
#: without the connection pool alike, and every failure looks identical from the
#: outside: the login returns b'' and the log ends mid-handshake.
#:
#: Keeping it costs one file in a directory the suite already preserves on
#: failure ("logs kept in ..."), and it is the difference between a flake
#: somebody can fix and one they can only re-run.
SERVER_OUT = os.path.join(TMP, "authserv.stdout.log")


def start_authserv():
    out = open(SERVER_OUT, "ab", buffering=0)
    p = subprocess.Popen([sys.executable, "responders.py", "authserv"],
                         cwd=SERVICES, env=ENV,
                         stdout=out, stderr=subprocess.STDOUT)
    if not wait_port(CLIENT_PORT + OFFSET):
        p.kill()
        raise SystemExit(f"authserv never listened on {CLIENT_PORT + OFFSET} "
                         f"-- see {SERVER_OUT}")
    return p


def login(sock):
    """Drive one K=0 login to the welcome accept. Returns the decrypted lines."""
    greet = b""
    sock.settimeout(5)
    while b"\r\n" not in greet:
        greet += sock.recv(4096)
    sock.sendall(b"USER x 8 * :resumeTESTtoken\r\n")
    tok0 = b""
    while b"\r\n" not in tok0:
        tok0 += sock.recv(4096)
    sock.sendall(sessioncrypt.ofb_apply(K0_P, K0_S, IV,
                                        b"NICK " + NICK + b":" + b"0" * 32
                                        + b":8:pol") + b"\r\n")
    welcome = recv_idle(sock, 1.5)
    return [dec(l) for l in welcome.split(b"\r\n") if l]


def session_file():
    import json
    try:
        with open(ENV["POL_SESSION_FILE"], encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def ask_resume(fp):
    """Speak the resume protocol by hand. Returns the server's verdict line."""
    s = socket.create_connection(("127.0.0.1", RESUME_PORT), timeout=5)
    try:
        s.sendall(f"RESUME :{fp} {CLIENT_PORT}\r\n".encode())
        s.settimeout(5)
        return s.recv(256).split(b"\r\n")[0]
    finally:
        s.close()


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print(f"scratch: {TMP}")
    server = start_authserv()
    import threading
    threading.Thread(target=authrelay.serve, args=(CLIENT_PORT,),
                     daemon=True).start()
    if not wait_port(CLIENT_PORT):
        server.kill()
        raise SystemExit("the relay never listened")

    client = None
    try:
        # -- 1. a login through the relay is just a login ------------------- #
        client = socket.create_connection(("127.0.0.1", CLIENT_PORT), timeout=5)
        lines = login(client)
        check("relay: login through the relay completes",
              any(b" 001 " in l for l in lines),
              f"decoded={b' | '.join(lines)[:90]!r}")

        # -- 2. the session recorded what a resume needs -------------------- #
        slots = [s for s in session_file().values() if s.get("resume_fp")]
        slot = slots[0] if slots else {}
        fp = slot.get("resume_fp", "")
        check("state: session slot carries a resume fingerprint", bool(fp),
              f"fp={fp[:12]}")
        check("state: session slot is marked channel_open",
              slot.get("channel_open") is True, f"slot={sorted(slot)}")
        check("state: the key and IV a resume rebuilds from are persisted",
              bool(slot.get("iv")) and slot.get("key") is not None)

        # -- 2b. the relay must not erase who the client IS ----------------- #
        # Both ends are 127.0.0.1 here, so the PORT is what distinguishes the
        # client's own socket from the relay's upstream one. authserv logging
        # the client's port means the CLIENT preamble was honoured -- and that
        # is the whole fix for the outage on 2026-08-16, where every player
        # collapsed into the relay's single address and a client holding a key
        # from before the deploy could never be found.
        myport = client.getsockname()[1]
        try:
            authlog = open(os.path.join(TMP, "authserv.log"), encoding="utf-8",
                           errors="replace").read()
        except OSError:
            authlog = ""
        check("preamble: authserv sees the CLIENT's address, not the relay's",
              f"127.0.0.1:{myport} " in authlog,
              f"client port {myport} not in authserv.log")

        # -- 3. kill authserv; the CLIENT socket must not notice ------------ #
        note("killing authserv")
        server.kill()
        server.wait(timeout=10)
        time.sleep(2.0)
        client.settimeout(1.0)
        alive = True
        try:
            if client.recv(1) == b"":
                alive = False           # EOF: the relay let the drop through
        except socket.timeout:
            pass                        # silence is exactly what we want
        except OSError:
            alive = False
        check("relay: the client socket SURVIVES authserv being killed", alive,
              "an EOF here is the POL-0008 this whole mechanism exists to stop")

        # -- 4. restart authserv; the relay must re-attach the session ------ #
        note("restarting authserv")
        server = start_authserv()
        deadline = time.time() + 25
        reply = b""
        sent = False
        while time.time() < deadline and not reply:
            if not sent or time.time() % 3 < 0.3:
                # AWAY is the in-session command captured live on 2026-08-11, and
                # the reply (305/306) is one the server states rather than guesses.
                try:
                    client.sendall(enc(b"AWAY") + b"\r\n")
                    sent = True
                except OSError:
                    break
            client.settimeout(2.0)
            try:
                got = client.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not got:
                break
            reply += got
        dec_reply = [dec(l) for l in reply.split(b"\r\n") if l]
        check("resume: the client's own socket is serving a live session again",
              any(b" 306 " in l or b" 305 " in l for l in dec_reply),
              f"decoded={b' | '.join(dec_reply)[:90]!r}")

        # -- 5. nothing leaked to the client that it did not ask for -------- #
        check("resume: no re-greeting leaked to the client",
              b" 300 " not in reply and b"300 *" not in reply,
              "a cleartext greeting on a mid-session socket would re-key it")

        # -- 6/7. the door refuses what it must ----------------------------- #
        check("door: an unknown fingerprint is refused",
              ask_resume("deadbeef" * 5).startswith(b"RESUME FAIL"))
        # A session closed ON PURPOSE must never come back: that is how a
        # redirect hop's EOF advances the client to the next auth node.
        client.close()
        client = None
        time.sleep(1.5)
        verdict = ask_resume(fp)
        check("door: a session closed on purpose is refused",
              verdict.startswith(b"RESUME FAIL"), f"got={verdict!r}")
    finally:
        try:
            if client:
                client.close()
        except OSError:
            pass
        server.kill()
        server.wait(timeout=10)

    print()
    if FAILS:
        print(f"FAIL -- {len(FAILS)} check(s): " + "; ".join(FAILS))
        print(f"logs kept in {TMP}")
        return 1
    print("PASS -- a killed authserv no longer costs the client its session")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
