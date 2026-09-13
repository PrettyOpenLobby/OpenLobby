"""Fetch portal pages from SE's LIVE servers with an x-MD5-pol session.

`wh000.pol.com` is still serving in 2026 (Apache/1.3.26, `realm="POL"`,
`algorithm="x-MD5-pol"`), so the pages the project only holds in Japanese can be
fetched in English -- see `work/ps2/out/portal-gap-en.txt` for the target list.

What this needs from you is a live session:

  1. start the Viewer and log in normally (the account does the authenticating,
     this tool does not);
  2. dump the session token with pol-shim -- `secret` is a *session-derived*
     token, NOT the account password, and it dies with the session;
  3. pass the user name and that token in, by environment for preference:

        set POL_USER=...            set POL_SECRET=...
        python polfetch.py --list ..\..\work\ps2\out\portal-gap-en.txt \
                           --out ..\www

The token is never written to disk or to the log. Requests go out one at a time
with a delay -- this is somebody else's twenty-three-year-old server.

Resumable: anything already present under --out is skipped, so re-running after
a session expires costs nothing.
"""
import argparse
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pol_digest

DEFAULT_UA = "PlayOnline-PML-Viewer/1.00 [en] (Windows XP)"


def _headers(host, uri, lang, auth=None):
    lines = ["GET %s HTTP/1.1" % uri, "Host: %s" % host, "User-Agent: " + DEFAULT_UA,
             "X-POL-VIEWER-VERSION: Ver.1.18.15e",
             "Accept: text/x-playonline-pml, image/x-playonline-ang, image/png, image/jpeg, */*",
             "Accept-Language: " + lang,
             # SE's Apache wants this present and well-formed (32 bytes A64 -> 43
             # chars) to complete the mutual-auth handshake
             "X-PlayOnline-Want-Hello: " + pol_digest.make_hello(os.urandom(32)),
             "Connection: Keep-Alive"]
    if auth:
        lines.append("Authorization: Digest " + auth)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class Session(object):
    """A persistent keep-alive connection that mimics the Viewer: one 401 to
    acquire a nonce, then every later /pml/ request pre-signed on the SAME
    socket with that cached nonce. Opening a fresh connection per request (the
    old behaviour) tripped SE's per-source connection limit after ~15 fetches;
    this holds one connection open the way the client does."""

    def __init__(self, host, ip, port, user, secret, lang="en-US", timeout=20):
        self.host, self.ip, self.port = host, ip, port
        self.user, self.secret, self.lang = user, secret, lang
        self.timeout = timeout
        self.sock = None
        self.nonce = None

    def _connect(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = socket.socket()
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.ip, self.port))

    def _recv(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = self.sock.recv(65536)
            if not d:
                return None, {}, b""
            buf += d
        head, _, body = buf.partition(b"\r\n\r\n")
        txt = head.decode("latin-1", "replace")
        status = int(txt.split(" ")[1]) if " " in txt else 0
        hdrs = {}
        for line in txt.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        if hdrs.get("transfer-encoding", "").lower() == "chunked":
            while not body.rstrip().endswith(b"0"):
                d = self.sock.recv(65536)
                if not d:
                    break
                body += d
            body = dechunk(body)
        else:
            n = int(hdrs.get("content-length", "0"))
            while len(body) < n:
                d = self.sock.recv(65536)
                if not d:
                    break
                body += d
            body = body[:n] if n else body
        keep = hdrs.get("connection", "").lower() != "close"
        return status, hdrs, body, keep

    def _send(self, uri, auth=None):
        if self.sock is None:
            self._connect()
        try:
            self.sock.sendall(_headers(self.host, uri, self.lang, auth))
            return self._recv()
        except (socket.error, OSError):
            self._connect()          # reopen once on a dropped keep-alive
            self.sock.sendall(_headers(self.host, uri, self.lang, auth))
            return self._recv()

    def get(self, uri):
        """Fetch one URI, acquiring/refreshing the nonce as needed."""
        if self.nonce:
            _, auth = pol_digest.sign(self.user, self.secret, "GET", uri, self.nonce)
            st, h, body, keep = self._send(uri, auth)
            if st != 401:
                if not keep:
                    self.sock = None
                return st, body
        st, h, body, keep = self._send(uri)          # (re)challenge
        if st == 401:
            m = re.search(r'nonce="([^"]+)"', h.get("www-authenticate", ""))
            if m and "x-MD5-pol" in h.get("www-authenticate", ""):
                self.nonce = m.group(1)
                _, auth = pol_digest.sign(self.user, self.secret, "GET", uri, self.nonce)
                st, h, body, keep = self._send(uri, auth)
        if not keep:
            self.sock = None
        return st, body

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def dechunk(body):
    out = b""
    while True:
        line, _, rest = body.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0], 16)
        except ValueError:
            return out
        if n == 0:
            return out
        out += rest[:n]
        body = rest[n + 2:]


def safe(path):
    return "".join(c if (c.isalnum() or c in "._-/") else "%%%02X" % ord(c)
                   for c in path).lstrip("/")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", required=True, help="file of paths, one per line, # comments ok")
    ap.add_argument("--out", required=True, help="mirror root; <out>/<host>/<path>")
    ap.add_argument("--host", default="wh000.pol.com")
    ap.add_argument("--ip", help="override DNS (the project's resolver points pol.com at the local stub)")
    ap.add_argument("--port", type=int, default=51300)
    ap.add_argument("--user", default=os.environ.get("POL_USER"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--limit", type=int, help="stop after N fetches (for a trial run)")
    a = ap.parse_args()

    secret = os.environ.get("POL_SECRET")
    if not (a.user and secret):
        raise SystemExit("set POL_USER and POL_SECRET (the shim-dumped session token)")
    ip = a.ip or socket.gethostbyname(a.host)
    if ip.startswith(("192.168.", "10.", "127.")):  # private-range guard; polcheck: allow
        raise SystemExit("%s resolves to %s -- that is the local stub, pass --ip for the real host"
                         % (a.host, ip))

    paths = [l.strip() for l in open(a.list, encoding="utf-8")]
    paths = [p for p in paths if p and not p.startswith("#")]
    sess = Session(a.host, ip, a.port, a.user, secret)
    got = miss = skip = fail = streak = 0
    try:
        for p in paths:
            dst = os.path.join(a.out, a.host, safe(p))
            if os.path.exists(dst):
                skip += 1
                continue
            if a.limit and got + miss + fail >= a.limit:
                break
            try:
                status, body = sess.get("/" + p.lstrip("/"))
            except Exception as e:
                print("  !! %s: %s" % (p, e))
                fail += 1
                continue
            if status == 200 and body:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(body)
                got += 1
                streak = 0
                print("  200 %7d  %s" % (len(body), p))
            elif status == 401:
                fail += 1
                streak += 1
                print("  401 (session rejected) %s" % p)
                if streak >= 5:
                    raise SystemExit("five consecutive auth failures -- session expired; "
                                     "re-navigate in the Viewer to mint a fresh token and re-run "
                                     "(already-fetched files are skipped)")
            else:
                miss += 1
                streak = 0
                print("  %3d %s" % (status, p))
            time.sleep(a.delay)
    finally:
        sess.close()
    print("\n%d fetched, %d not served, %d already had, %d errors" % (got, miss, skip, fail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
