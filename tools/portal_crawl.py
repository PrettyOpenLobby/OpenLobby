#!/usr/bin/env python3
"""Archive the PlayOnline portal (PML pages + assets) by crawling it as a client.

The portal serves PML/images over plain HTTP on the pp000 BAND PORTS of the
content host (wh000.pol.com = 202.67.54.55, ports 5130x -- its :80/:443 are
firewalled), gated by HTTP Digest `x-MD5-pol`. We hold the session's (userName,
secret) -- dumped once via the shim -- so `pol_digest.sign()` lets us authenticate
ARBITRARY requests, not just the pages the UI links to. That's the whole point:
reach content no menu navigates to.

Flow per URL: GET (unauth) -> 401 w/ fresh nonce -> sign -> GET w/ Authorization
-> 200 body. Bodies are saved to <out>/<hostname>/<path>; PML bodies are scanned
for links (includes, anchors, image refs) and enqueued (BFS). Common $vars are
substituted so expression-built URLs resolve ($SC_ID, $_PLATFORM, $_USER_LANG).

THIS HITS SE'S LIVE PRODUCTION SERVER. It is GET-only, serial, rate-limited, and
scoped to /pml/ + /pcd/, and it SKIPS transactional paths (account, billing,
logout, mail actions). Keep --delay polite and --max sane. Anything skipped by a
cap is logged, so the archive never silently pretends to be complete.

Get a live (userName, secret): shim `probes=1`, one /pml/ fetch, then
`grep ':POL:' pol-shim/build/polshim.*.log`. The session must be ALIVE (client
logged in) or SE rejects the digest.

    python tools/portal_crawl.py --user <u> --secret <s>
    python tools/portal_crawl.py --user <u> --secret <s> --max 2000 --delay 0.4
    POL_PORTAL_USER=.. POL_PORTAL_SECRET=.. python tools/portal_crawl.py --test
"""
import argparse
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pol_digest  # noqa: E402

# Substitutions so expression-built PML URLs resolve to a concrete path.
# $C_PATH1 is the magazine content base -- the greeting-card / Extras pages build
# every asset ref as "$C_PATH1+'gcard/...'"; without it those collapse to a bad
# relative path (doubled dirs -> 404). Measured live: $C_PATH1 = /pml/magazine/.
VARS = {"$SC_ID": "1", "$_PLATFORM": "WIN", "$_USER_LANG": "en-US",
        "$_PRODUCTID": "1", "$PF": "WIN", "$C_PATH1": "/pml/magazine/"}

# Never fetch these -- transactional / account / stateful.
SKIP_RE = re.compile(r"(logout|signout|signup|/cs/|/account|billing|/mail|"
                     r"delete|regist|purchase|cancel|option:)", re.I)

# A path we care to archive: portal content trees.
KEEP_RE = re.compile(r"^/(pml|pml2|pcd|magazine)/", re.I)


def apply_vars(s):
    for k, v in VARS.items():
        s = s.replace(k, v)
    return s


def extract_links(body_text, base_dir):
    """Best-effort: pull candidate URLs/paths out of a PML body and resolve them
    relative to base_dir. Handles literal strings and simple '..'+$var+'..'
    concatenations by dropping quotes/+ and substituting known vars."""
    found = set()
    # 1) include src="..."
    for m in re.finditer(r'src\s*=\s*"([^"]+)"', body_text):
        found.add(m.group(1))
    # 2) any quoted token that looks like a path or asset
    for m in re.finditer(r'"([^"]*?(?:/[A-Za-z0-9_./%-]+|\.(?:pml|png|ang|jpg)))"',
                         body_text):
        found.add(m.group(1))
    out = set()
    for raw in found:
        # collapse expression concatenation: "'/a/'+$X+'/b'" -> /a/<X>/b
        s = raw.replace("'", "").replace("+", "").strip()
        s = apply_vars(s)
        if "$" in s or " " in s:            # unresolved expression -> skip
            continue
        # strip protocol/host
        s = re.sub(r"^https?://[^/]+", "", s)
        # strip a PML action-verb scheme (file:/... sound:.. gameto:.. etc.):
        # only 'file:' carries a real path; the rest are actions, drop them.
        m = re.match(r"^([a-z]+):(.*)$", s)
        if m:
            if m.group(1) == "file" and m.group(2).startswith("/"):
                s = m.group(2)
            else:
                continue
        if not s:
            continue
        if not s.startswith("/"):           # relative to current dir
            s = base_dir.rstrip("/") + "/" + s
        # normalise ../ and //
        parts = []
        for seg in s.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg not in ("", "."):
                parts.append(seg)
        s = "/" + "/".join(parts)
        out.add(s)
    return out


class Conn:
    """One keep-alive HTTP/1.1 connection to the content host's band port."""
    def __init__(self, ip, port, hostname):
        self.ip, self.port, self.hostname = ip, port, hostname
        self.s = None

    def _open(self):
        if self.s:
            return
        self.s = socket.create_connection((self.ip, self.port), 15)
        self.s.settimeout(20)

    def _send(self, method, uri, auth=None):
        h = [f"{method} {uri} HTTP/1.1",
             f"Host: {self.hostname}",
             "User-Agent: PlayOnline-PML-Viewer/1.00 [en] (Windows XP)",
             "X-POL-VIEWER-VERSION: Ver.1.18.15e",
             "Accept: text/x-playonline-pml, image/x-playonline-ang, image/png, "
             "image/jpeg, */*",
             "Accept-Language: en-US",
             "Connection: keep-alive"]
        if auth:
            h.append("Authorization: Digest " + auth)
        self.s.sendall(("\r\n".join(h) + "\r\n\r\n").encode("latin-1"))

    def _recv_response(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(65536)
            if not c:
                raise ConnectionError("closed before headers")
            buf += c
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        body = rest
        n = hdrs.get("content-length")
        if n is not None:
            n = int(n)
            while len(body) < n:
                c = self.s.recv(65536)
                if not c:
                    break
                body += c
            body = body[:n]
        elif hdrs.get("transfer-encoding", "").lower() == "chunked":
            body = self._dechunk(body)
        if hdrs.get("connection", "").lower() == "close":
            self.close()
        return status, hdrs, body

    def _dechunk(self, body):
        out = b""
        while True:
            while b"\r\n" not in body:
                body += self.s.recv(65536)
            line, _, body = body.partition(b"\r\n")
            n = int(line.split(b";")[0], 16)
            if n == 0:
                return out
            while len(body) < n + 2:
                body += self.s.recv(65536)
            out += body[:n]
            body = body[n + 2:]

    def get(self, uri, user, secret):
        """GET with digest auth; returns (status, headers, body)."""
        for attempt in range(2):
            self._open()
            try:
                self._send("GET", uri)
                st, hd, bd = self._recv_response()
            except (ConnectionError, socket.timeout, OSError):
                self.close()
                self._open()
                self._send("GET", uri)
                st, hd, bd = self._recv_response()
            if st != 401:
                return st, hd, bd
            m = re.search(r'nonce="([^"]+)"', hd.get("www-authenticate", ""))
            if not m:
                return st, hd, bd
            _, auth = pol_digest.sign(user, secret, "GET", uri, m.group(1))
            self._open()
            self._send("GET", uri, auth)
            st, hd, bd = self._recv_response()
            return st, hd, bd
        return st, hd, bd

    def close(self):
        if self.s:
            try:
                self.s.close()
            except OSError:
                pass
            self.s = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="202.67.54.55", help="content host IP (real wh000)")
    ap.add_argument("--port", type=int, default=51304, help="band port (5130x)")
    ap.add_argument("--hostname", default="wh000.pol.com", help="Host header")
    ap.add_argument("--user", default=os.environ.get("POL_PORTAL_USER"))
    ap.add_argument("--secret", default=os.environ.get("POL_PORTAL_SECRET"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "www"))
    ap.add_argument("--seed", default="/pml/main/index.pml")
    ap.add_argument("--seeds", default="", help="extra comma-separated seed paths")
    ap.add_argument("--seeds-file", default="", help="file of extra seed paths, "
                    "one per line (see tools/pml_assets.py --seeds, which "
                    "resolves the $VAR+'literal' asset URLs this crawler's own "
                    "link extractor throws away)")
    ap.add_argument("--max", type=int, default=800, help="max pages to fetch")
    # 3s, not the old 0.4s. This is a live commercial service still serving
    # paying customers, and the account making these requests is a real one --
    # being rude risks the account, not just the address, and losing it ends all
    # future archiving.
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between requests (deliberately slow)")
    ap.add_argument("--refetch", action="store_true",
                    help="re-download pages already present in --out; off by "
                         "default so a run only costs requests for what is "
                         "genuinely missing")
    ap.add_argument("--test", action="store_true", help="fetch the seed only, verify auth")
    ap.add_argument("--no-auth", action="store_true",
                    help="crawl WITHOUT a session. SE gates only /pml/ -- the "
                         "whole /pcd/ tree answers 200 unauthenticated (verified "
                         "2026-08-10). In this mode a 401 means 'needs a session', "
                         "so the URL is skipped instead of counting as a dead "
                         "session, and the crawl keeps going.")
    a = ap.parse_args()
    if a.no_auth:
        a.user = a.user or "-"
        a.secret = a.secret or "-"
    if not a.user or not a.secret:
        print("need --user and --secret (or POL_PORTAL_USER/SECRET). Dump them via "
              "the shim: probes=1, one /pml/ fetch, grep ':POL:' polshim.*.log.\n"
              "Or pass --no-auth to crawl the ungated /pcd/ tree with no session.")
        return 2

    conn = Conn(a.host, a.port, a.hostname)
    outroot = os.path.normpath(a.out)

    # liveness check first -- a dead session digest-loops (401 twice)
    st, hd, bd = conn.get(a.seed, a.user, a.secret)
    if st == 401 and a.no_auth:
        print(f"seed {a.seed} needs a session (401). In --no-auth mode pick a "
              f"/pcd/ seed, e.g. --seed /pcd/mainmenu/en-US/data.pml")
        return 1
    if st == 401:
        print("AUTH FAILED (401 after signing) -- the session is dead or the "
              "(user,secret) is stale. Re-dump from a fresh login.")
        return 1
    print(f"auth OK: {a.seed} -> {st} ({len(bd)}B, "
          f"hello={'yes' if hd.get('x-playonline-hello') else 'no'})")
    if a.test:
        return 0

    seen, queue, saved, errors, skipped = set(), [a.seed], 0, [], []
    from_cache = 0          # served from the local archive, no request made
    seen.add(a.seed.split("?")[0])
    extras = [x.strip() for x in a.seeds.split(",") if x.strip()]
    if a.seeds_file:
        with open(a.seeds_file, encoding="utf-8") as f:
            extras += [ln.strip() for ln in f
                       if ln.strip() and not ln.startswith("#")]
    for extra in extras:
        if extra.split("?")[0] not in seen:
            seen.add(extra.split("?")[0])
            queue.append(extra)
    dead401 = 0
    while queue and saved < a.max:
        uri = queue.pop(0)
        path = uri.split("?")[0]
        if SKIP_RE.search(uri):
            skipped.append(uri)
            continue

        # ALREADY HELD? Then do not ask SE for it again.
        #
        # The archive is ~4,800 pages. Without this, every run re-fetches all of
        # them to rediscover links it already knows, which is both the slowest
        # possible crawl and the rudest -- thousands of requests to a live
        # commercial service to learn nothing. Instead, read the local copy,
        # walk its links, and spend network requests only on genuinely missing
        # URLs. The crawl still traverses the whole graph; it just traverses
        # most of it from disk.
        held = os.path.normpath(os.path.join(outroot, a.hostname, path.lstrip("/")))
        if (not a.refetch and held.startswith(outroot)
                and os.path.isfile(held) and os.path.getsize(held) > 0):
            from_cache += 1
            if path.endswith(".pml"):
                try:
                    with open(held, "rb") as f:
                        local = f.read()
                except OSError:
                    local = b""
                base_dir = path.rsplit("/", 1)[0]
                for link in extract_links(local.decode("latin-1", "replace"),
                                          base_dir):
                    lp = link.split("?")[0]
                    if lp in seen or not KEEP_RE.match(link):
                        continue
                    seen.add(lp)
                    queue.append(link)
            continue

        time.sleep(a.delay)
        try:
            st, hd, bd = conn.get(uri, a.user, a.secret)
        except Exception as e:
            errors.append((uri, str(e)))
            conn.close()
            continue
        if st == 401 and a.no_auth:
            # expected: this URL is under /pml/, which needs a session. Not a
            # failure -- just out of reach in this mode.
            skipped.append(uri + "  (401, needs a session)")
            continue
        if st == 401:
            # session died mid-crawl: stop rather than hammer SE with 401s
            dead401 += 1
            errors.append((uri, "HTTP 401"))
            if dead401 >= 3:
                print("\nABORT: 3 consecutive 401s -- the session expired. "
                      "Re-dump (user,secret) from a fresh login and resume; "
                      f"{saved} saved so far, {len(queue)} still queued.")
                break
            continue
        dead401 = 0
        if st in (301, 302) and hd.get("location"):
            loc = re.sub(r"^https?://[^/]+", "", hd["location"])
            if loc and loc.split("?")[0] not in seen and KEEP_RE.match(loc):
                seen.add(loc.split("?")[0])
                queue.append(loc)
            continue
        if st != 200 or not bd:
            errors.append((uri, f"HTTP {st}"))
            continue
        dest = os.path.normpath(os.path.join(outroot, a.hostname, path.lstrip("/")))
        if not dest.startswith(outroot):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(bd)
        saved += 1
        ctype = hd.get("content-type", "")
        print(f"[{saved:4}] {st} {len(bd):7}B {path}")
        if "pml" in ctype or path.endswith(".pml"):
            base_dir = path.rsplit("/", 1)[0]
            for link in extract_links(bd.decode("latin-1", "replace"), base_dir):
                lp = link.split("?")[0]
                if lp in seen:
                    continue
                if not KEEP_RE.match(link):
                    continue
                seen.add(lp)
                queue.append(link)

    print(f"\ndone: saved {saved} (NEW), served-from-archive {from_cache} "
          f"(no request made), queued-unfetched {len(queue)}, "
          f"errors {len(errors)}, skipped(transactional) {len(skipped)}")
    if from_cache and not saved:
        print("  nothing new upstream -- the whole reachable graph is already "
              "archived.")
    if queue:
        print(f"  (hit --max={a.max}; {len(queue)} URLs still queued -- raise --max)")
    for u, e in errors[:20]:
        print(f"  ERR {e}  {u}")
    if len(errors) > 20:
        print(f"  ... +{len(errors) - 20} more errors")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
