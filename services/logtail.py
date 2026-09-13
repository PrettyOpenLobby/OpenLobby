"""Read-only log tail over HTTP, so logs can be pulled without shell access.

Deliberately minimal in capability, not just in code:

  * GET only. No other method is routed.
  * Only files directly inside the log directory, matching an allowlist
    pattern. No traversal, no subdirectories, no arbitrary paths.
  * Bounded output -- a byte cap per request, read from the END of the file.
  * A bearer token is required on every request.
  * Secrets are redacted on the way out (see REDACT).

It cannot write, restart, exec, or read outside its mount. Run it with the log
directory mounted READ-ONLY and it is enforced by the filesystem too.

    POL_LOGTAIL_TOKEN=<long random>  python logtail.py

Endpoints (token via `Authorization: Bearer` or `?token=`):

    /            list available logs with sizes and mtimes
    /<name>      tail of that log        (?bytes=N, default 64k, max 2M)
    /<name>?grep=RE   only lines matching RE (applied after the tail)

Redaction is on by default. POL_LOGTAIL_RAW=1 disables it -- for a private
network only, since the logs carry session tokens and password hashes.
"""
import html
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

LOG_DIR = os.environ.get("POL_LOGTAIL_DIR", "/logs")
PORT = int(os.environ.get("POL_LOGTAIL_PORT", "8099"))
TOKEN = os.environ.get("POL_LOGTAIL_TOKEN", "")
RAW = os.environ.get("POL_LOGTAIL_RAW", "0") == "1"

DEFAULT_BYTES = 64 * 1024
MAX_BYTES = 2 * 1024 * 1024

# Only these names are servable. Anchored, no separators, so traversal cannot
# be expressed in the first place rather than being filtered out afterwards.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.log$")

# (pattern, replacement) applied to every emitted line.
REDACT = [
    (re.compile(r"(session=)[0-9a-f]{6,}", re.I), r"\1<redacted>"),
    (re.compile(r"(token=)[A-Za-z0-9+/=]{8,}"), r"\1<redacted>"),
    (re.compile(r"(NICK\s+\S+?:)[0-9a-f]{16,}", re.I), r"\1<redacted>"),
    (re.compile(r"(IV=)[0-9a-f]{8,}", re.I), r"\1<redacted>"),
    (re.compile(r"(pw_hash\W+)\S+", re.I), r"\1<redacted>"),
]


def redact(text):
    if RAW:
        return text
    for pat, rep in REDACT:
        text = pat.sub(rep, text)
    return text


def tail_bytes(path, n):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > n:
            f.seek(size - n)
            f.readline()          # discard the partial first line
        return f.read().decode("utf-8", "replace"), size


class Handler(BaseHTTPRequestHandler):
    server_version = "polLogTail/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    # --- helpers ------------------------------------------------------------

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        raw = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self, qs):
        if not TOKEN:
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hdr[7:].strip() == TOKEN:
            return True
        return qs.get("token", [""])[0] == TOKEN

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if not self._authed(qs):
            # Same response whether the token is absent or wrong.
            self._send(401, "unauthorized\n")
            return

        name = unquote(u.path).lstrip("/")

        if not name:
            self._send(200, self._index())
            return

        if not NAME_RE.match(name):
            self._send(400, "bad log name\n")
            return

        path = os.path.join(LOG_DIR, name)
        # Belt and braces: the regex already forbids separators, but confirm the
        # resolved path really is a direct child of LOG_DIR.
        if os.path.dirname(os.path.realpath(path)) != os.path.realpath(LOG_DIR):
            self._send(400, "bad log name\n")
            return
        if not os.path.isfile(path):
            self._send(404, "no such log\n")
            return

        try:
            nbytes = int(qs.get("bytes", [DEFAULT_BYTES])[0])
        except ValueError:
            nbytes = DEFAULT_BYTES
        nbytes = max(1024, min(nbytes, MAX_BYTES))

        text, size = tail_bytes(path, nbytes)

        pattern = qs.get("grep", [""])[0]
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                self._send(400, "bad regex: %s\n" % e)
                return
            text = "".join(l + "\n" for l in text.splitlines() if rx.search(l))

        header = "# %s  size=%d  showing<=%d  redacted=%s\n" % (
            name, size, nbytes, "no" if RAW else "yes")
        self._send(200, header + redact(text))

    def do_HEAD(self):
        self._send(405, "")

    def do_POST(self):
        self._send(405, "read-only\n")

    do_PUT = do_DELETE = do_PATCH = do_POST

    # --- index --------------------------------------------------------------

    def _index(self):
        rows = []
        try:
            names = sorted(os.listdir(LOG_DIR))
        except OSError as e:
            return "cannot list %s: %s\n" % (LOG_DIR, e)
        for n in names:
            if not NAME_RE.match(n):
                continue
            p = os.path.join(LOG_DIR, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rows.append("%-22s %12d  %s" % (
                n, st.st_size,
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime))))
        return ("# logs in %s (name, bytes, mtime UTC)\n" % LOG_DIR
                + "\n".join(rows) + "\n")


def main():
    if not TOKEN:
        sys.exit("refusing to start: POL_LOGTAIL_TOKEN is empty.\n"
                 "Set it to a long random value; there is no unauthenticated mode.")
    if len(TOKEN) < 24:
        sys.exit("refusing to start: POL_LOGTAIL_TOKEN is shorter than 24 chars.")
    if not os.path.isdir(LOG_DIR):
        sys.exit("no log directory at %s" % LOG_DIR)
    print("logtail on :%d  dir=%s  redaction=%s"
          % (PORT, LOG_DIR, "off" if RAW else "on"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
