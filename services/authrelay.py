#!/usr/bin/env python3
"""The auth-band front relay: the socket the client holds must outlive authserv.

WHY THIS EXISTS
---------------
After the `welcome` accept the Viewer keeps its auth-band connection open as its
SESSION CHANNEL -- chat, presence, TOPIC/MODE/PRIVMSG and a launched title's
world traffic all ride it (handle_authserv's observe loop). Killing that socket
is what the client reports as POL-0008, and it costs the user a password
re-entry: there is no saved SE credential to fall back on.

Splitting `authserv` into its own container (2026-08-13) stopped a *lobby* edit
from doing that. It did nothing for an *authserv* edit, which still ends every
live session, because the sockets die with the process.

This is the fix for the remaining case. The client's TCP connection terminates
HERE, in a process whose code effectively never changes; the connection to
authserv is ours to lose and re-make. `docker compose restart authsess` then
costs a few seconds of silence on a socket that stays open, which the client
does not notice -- it has no read timeout of its own (the whole POL_AUTH_PING
keepalive history says so: sessions only ever died when *we* closed them).

WHAT MAKES IT SAFE TO DO KEY-FREE
---------------------------------
The relay never decrypts anything, and does not need to:

  * The session cipher is OFB **re-keyed from the connection's IV for every
    line**, never chained (see ChatSession in responders.py). So lines are
    independent: a line that crosses a reconnect is fine as long as it is not
    SPLIT across one.
  * `ofb_apply` passes CR and LF through in the clear. So `\r\n` framing is
    visible on the wire without any key, and holding back a partial line is
    something this process can actually do.

Hence the one framing rule below: **only ever forward COMPLETE lines.** A half
line delivered to a freshly restarted authserv would be read as a corrupt line
and fail the client's trailing checksum (frame_line) in the other direction.

DELIBERATE CLOSE vs CRASH -- and why we ask rather than guess
-------------------------------------------------------------
A redirect hop CLOSES on purpose: that EOF is how the client learns to dial the
next auth node. At the TCP layer that is indistinguishable from authserv dying.
Guessing wrong in either direction is bad (resurrect a hop and the login chain
stalls; close a live session and we are back to POL-0008), so we do not guess --
we ASK, on the resume door:

    relay -> authserv   RESUME :<fingerprint> <port>
    authserv -> relay   RESUME OK            <- session is live and orphaned
                        RESUME FAIL <reason> <- no such session / closed cleanly

The session's `channel_open` flag is cleared by handle_authserv's `finally`, so
a hop that closed on purpose answers FAIL and a hop whose process was killed
never got to clear it and answers OK. The bit is written to auth-sessions.json,
so it survives exactly the restart it has to describe.

THE FINGERPRINT is the client's own encrypted NICK line, verbatim, hashed. The
relay sees it (it is the second line of every hop) without being able to read
it, it is per-hop and high entropy, and it is already what authserv keys the
session's crypto off -- so it names the session unambiguously even when two
clients share one bridge address, which is the failure mode _SESSIONS was
rewritten to kill. See the JOIN KEY note above _SESSIONS in responders.py.

TOPOLOGY
--------
The relay binds the ports the CLIENT dials (51241-51250); authserv moves up by
POL_RELAY_OFFSET (51441-51450) and keeps using the client-visible number in
every protocol string it builds -- see POL_AUTH_LISTEN_OFFSET in responders.py.
Under docker-compose.prod.yml every service is network_mode: host, so the two
processes genuinely cannot share a port number; the offset is not cosmetic.

Failure posture: if anything here cannot do its job, it gets out of the way and
the client sees exactly what it sees today (a closed socket). This process
existing must never be worse than it not existing.
"""
import hashlib
import os
import select
import socket
import sys
import threading
import time

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _ports(spec):
    """"51241-51250,51260" -> [51241..51250, 51260]."""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


PORTS        = _ports(os.environ.get("POL_RELAY_PORTS", "51241-51250"))
UPSTREAM     = os.environ.get("POL_RELAY_UPSTREAM", "127.0.0.1")
OFFSET       = int(os.environ.get("POL_RELAY_OFFSET", "200"))
RESUME_PORT  = int(os.environ.get("POL_RELAY_RESUME_PORT", "51451"))
RETRY_MS     = int(os.environ.get("POL_RELAY_RETRY_MS", "400"))
RETRY_MAX_MS = int(os.environ.get("POL_RELAY_RETRY_MAX_MS", "4000"))
#: How long a session may stay orphaned before we give up and close the client.
#: Longer than any restart, shorter than the client's patience for a dead-quiet
#: socket is unknown -- but an orphan we keep forever is a descriptor leak, and
#: the user is better served by the login screen than by a window that never
#: comes back.
DEADLINE_S   = int(os.environ.get("POL_RELAY_DEADLINE_S", "120"))
#: Client->server bytes we are willing to hold while degraded. The session
#: channel is chat-sized; anything near this cap means something is wrong.
QUEUE_MAX    = int(os.environ.get("POL_RELAY_QUEUE_MAX", "262144"))
#: Announce the real client address to authserv (`CLIENT <ip> <port>`) before
#: relaying anything. Must match POL_AUTH_FRONT_PREAMBLE on the authserv side --
#: they are deployed together, and the pair is what keeps per-client state per
#: client. See connect_upstream().
PREAMBLE     = os.environ.get("POL_RELAY_PREAMBLE", "1") == "1"
LOG_DIR      = os.environ.get("POL_LOG_DIR", "/logs")
VERBOSE      = os.environ.get("POL_RELAY_VERBOSE", "0") == "1"

_LOG_LOCK = threading.Lock()


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg
    with _LOG_LOCK:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "authrelay.log"), "a",
                      encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)


# --------------------------------------------------------------------------- #
# Line framing
# --------------------------------------------------------------------------- #
def _take_lines(buf):
    """Split off every COMPLETE `...\\r\\n` from `buf`.

    Returns (forwardable_bytes, remainder). The remainder is a partial line and
    must not go anywhere until it is finished -- see the module docstring.
    """
    cut = buf.rfind(b"\r\n")
    if cut < 0:
        return b"", buf
    return buf[:cut + 2], buf[cut + 2:]


def _recv_line(sock, timeout):
    """One CRLF line, plus whatever arrived after it. (line, rest) or (None, rest)."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while b"\r\n" not in buf and len(buf) < 4096:
            c = sock.recv(512)
            if not c:
                break
            buf += c
    except (socket.timeout, OSError):
        pass
    finally:
        try:
            sock.settimeout(None)
        except OSError:
            pass
    if b"\r\n" not in buf:
        return None, buf
    line, rest = buf.split(b"\r\n", 1)
    return line, rest


# --------------------------------------------------------------------------- #
# One fronted connection
# --------------------------------------------------------------------------- #
class Fronted:
    """The client's socket, and whichever authserv connection currently backs it."""

    def __init__(self, client, addr, port):
        self.client = client
        self.client_ip, self.client_port = addr[0], addr[1]
        self.peer = f"{addr[0]}:{addr[1]}"
        self.port = port                 # what the CLIENT dialled
        self.up = None
        self.up_port = port + OFFSET
        self.c2u = b""                   # client -> authserv, unforwarded
        self.u2c = b""                   # authserv -> client, unforwarded
        self.lines_seen = 0              # complete client lines forwarded, ever
        self.fp = None                   # session fingerprint (hex sha1 of NICK ct)
        self.orphan_since = 0.0
        self.next_try = 0.0
        self.backoff = RETRY_MS / 1000.0
        self.resumes = 0

    # -- wiring ------------------------------------------------------------- #
    def connect_upstream(self):
        try:
            s = socket.create_connection((UPSTREAM, self.up_port), timeout=10)
        except OSError as e:
            log(f"{self.peer} :{self.port} upstream {UPSTREAM}:{self.up_port} "
                f"refused ({e}) -- closing client")
            return False
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(None)
        # WHO WE ARE CARRYING. Every connection now reaches authserv from THIS
        # process, so without this line every player shares one address there --
        # and the session-token history is filed per address. Measured live
        # 2026-08-16, hours after the deploy: a Viewer that had been running
        # since before it could not log in AT ALL. Its key came from a token
        # filed under the old address, the lookup searched the relay's, and the
        # login failed after grinding every wrong candidate.
        #
        # Sent before any client byte, so authserv can read it before it greets.
        if PREAMBLE:
            try:
                s.sendall(f"CLIENT {self.client_ip} {self.client_port}\r\n".encode())
            except OSError:
                pass
        self.up = s
        return True

    def _note_client_lines(self, blob):
        """Learn the session fingerprint from the client's own opening lines.

        Hop shape: line 1 is the cleartext `USER x 8 * :<token>`, line 2 is the
        encrypted NICK. Nothing else on the band looks like that, and a hop that
        never gets there simply leaves fp None -- which disables resume for this
        connection and changes nothing else.
        """
        for line in blob.split(b"\r\n")[:-1]:
            self.lines_seen += 1
            if self.lines_seen == 2 and self.fp is None and line:
                self.fp = hashlib.sha1(line).hexdigest()
                if VERBOSE:
                    log(f"{self.peer} :{self.port} session fingerprint "
                        f"{self.fp[:12]} ({len(line)}B NICK line)")

    def pump_client_to_up(self):
        """Forward buffered COMPLETE client lines, if there is anywhere to put them."""
        if self.up is None or not self.c2u:
            return True
        send, self.c2u = _take_lines(self.c2u)
        if not send:
            return True
        self._note_client_lines(send)
        try:
            self.up.sendall(send)
        except OSError:
            self.c2u = send + self.c2u   # unsent: it goes over the next upstream
            return False
        return True

    def pump_up_to_client(self):
        if not self.u2c:
            return True
        send, self.u2c = _take_lines(self.u2c)
        if not send:
            return True
        try:
            self.client.sendall(send)
        except OSError:
            return False
        return True

    # -- recovery ----------------------------------------------------------- #
    def go_orphan(self, why):
        try:
            if self.up:
                self.up.close()
        except OSError:
            pass
        self.up = None
        # A partial line from the dead connection can never be completed: its
        # other half died with the process. Drop it rather than deliver a
        # fragment the client would fail the checksum on.
        if self.u2c:
            if VERBOSE:
                log(f"{self.peer} :{self.port} dropping {len(self.u2c)}B partial "
                    "line from the dead upstream")
            self.u2c = b""
        self.orphan_since = time.time()
        self.next_try = time.time()      # first attempt is immediate: it is also
        self.backoff = RETRY_MS / 1000.0  # how we ask whether this was deliberate
        log(f"{self.peer} :{self.port} upstream gone ({why}); "
            + (f"holding the client socket, fp={self.fp[:12]}"
               if self.fp else "no fingerprint -- cannot resume"))

    def try_resume(self):
        """One resume attempt. Returns 'ok', 'no' (give up) or 'later'."""
        self.next_try = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, RETRY_MAX_MS / 1000.0)
        if not self.fp:
            return "no"
        try:
            s = socket.create_connection((UPSTREAM, RESUME_PORT), timeout=5)
        except OSError:
            # Nothing is listening on the resume door either, so authserv really
            # is down -- which is the case worth waiting through.
            return "later"
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # The client address rides the resume line for the same reason it
            # rides the CLIENT preamble -- a resumed session must not lose the
            # identity the original login had.
            s.sendall(f"RESUME :{self.fp} {self.port} {self.client_ip}\r\n".encode())
            line, rest = _recv_line(s, 5)
        except OSError:
            line, rest = None, b""
        if line is None:
            try:
                s.close()
            except OSError:
                pass
            return "later"
        if line.startswith(b"RESUME OK"):
            s.settimeout(None)
            self.up = s
            self.u2c += rest             # anything already sent after the OK
            self.resumes += 1
            held = len(self.c2u)
            log(f"{self.peer} :{self.port} RESUMED after "
                f"{time.time() - self.orphan_since:.1f}s "
                f"(attempt #{self.resumes}, {held}B held for authserv)")
            self.orphan_since = 0.0
            return "ok"
        try:
            s.close()
        except OSError:
            pass
        log(f"{self.peer} :{self.port} resume declined: "
            f"{line.decode('latin-1', 'replace')!r} -- closing the client "
            "(this is the normal path for a redirect hop)")
        return "no"

    # -- main loop ---------------------------------------------------------- #
    def run(self):
        if not self.connect_upstream():
            return
        self.client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            rl = [self.client] + ([self.up] if self.up else [])
            try:
                ready, _, _ = select.select(rl, [], rl, 0.25)
            except (OSError, ValueError):
                return
            if self.client in ready:
                try:
                    c = self.client.recv(8192)
                except OSError:
                    c = b""
                if not c:
                    if VERBOSE:
                        log(f"{self.peer} :{self.port} client hung up")
                    return
                self.c2u += c
                # While degraded we keep READING the client so its send buffer
                # never fills -- we simply do not forward. Only complete lines
                # are ever held, so nothing is truncated on the way back out.
                if len(self.c2u) > QUEUE_MAX:
                    log(f"{self.peer} :{self.port} held {len(self.c2u)}B for a "
                        "dead upstream (over POL_RELAY_QUEUE_MAX) -- giving up")
                    return
            if self.up is not None and self.up in ready:
                try:
                    c = self.up.recv(8192)
                except OSError:
                    c = b""
                if not c:
                    self.go_orphan("EOF")
                else:
                    self.u2c += c
            if not self.pump_up_to_client():
                return                    # the client is gone; nothing to save
            if not self.pump_client_to_up():
                self.go_orphan("send failed")
            if self.up is None:
                if time.time() - self.orphan_since > DEADLINE_S:
                    log(f"{self.peer} :{self.port} orphaned for {DEADLINE_S}s "
                        "with no authserv -- closing the client")
                    return
                if time.time() >= self.next_try:
                    verdict = self.try_resume()
                    if verdict == "no":
                        return
                    if verdict == "ok":
                        self.pump_client_to_up()

    def close(self):
        for s in (self.client, self.up):
            try:
                if s:
                    s.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Listeners
# --------------------------------------------------------------------------- #
def _serve_one(client, addr, port):
    f = Fronted(client, addr, port)
    try:
        f.run()
    except Exception as e:                # one connection must never take the relay down
        log(f"{addr[0]}:{addr[1]} :{port} relay error: {e!r}")
    finally:
        f.close()


def serve(port, ready=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # A FAILED BIND USED TO BE SILENT. This runs on a daemon thread, so an
    # exception here killed the thread and nothing else: the process stayed up,
    # the startup banner still claimed the band was fronted, and that port was
    # simply closed. The most likely cause is the one that bites during a
    # deploy -- authserv still holding the client-visible ports because its
    # POL_AUTH_LISTEN_OFFSET has not been applied yet -- so it must say so, and
    # it must keep trying rather than leave a hole for the life of the process.
    while True:
        try:
            s.bind(("0.0.0.0", port))
            break
        except OSError as e:
            log(f"CANNOT BIND :{port} ({e}) -- the client dials this port and "
                f"NOTHING IS SERVING IT. Something else holds it; if that is "
                f"authserv, it needs POL_AUTH_LISTEN_OFFSET set (and a restart) "
                f"so it listens on {port + OFFSET} instead. Retrying every 10s.")
            time.sleep(10)
    s.listen(64)
    log(f"fronting :{port} -> {UPSTREAM}:{port + OFFSET} "
        f"(resume door {UPSTREAM}:{RESUME_PORT})")
    if ready is not None:
        ready.set()
    while True:
        try:
            conn, addr = s.accept()
        except OSError as e:
            # Same posture as responders.serve(): a listener that dies is the
            # quietest possible failure, so it does not get to.
            log(f"accept on :{port} failed ({e!r}); listener continues")
            time.sleep(0.1)
            continue
        threading.Thread(target=_serve_one, args=(conn, addr, port),
                         daemon=True).start()


def main():
    log(f"auth-band front relay up: ports {PORTS[0]}-{PORTS[-1]}, "
        f"upstream {UPSTREAM} offset +{OFFSET}, resume :{RESUME_PORT}, "
        f"deadline {DEADLINE_S}s")
    for p in PORTS:
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
