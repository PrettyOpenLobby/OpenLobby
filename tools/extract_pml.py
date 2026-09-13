#!/usr/bin/env python3
"""Pull PML/portal content out of relay captures and lay it out under www/.

Why this works: the PlayOnline portal does NOT fetch pages from wh000:80. It
tunnels ordinary, UNENCRYPTED HTTP over the pp000 lobby band ports, with the real
host only in the Host header:

    GET /pml/main/index.pml HTTP/1.1
    Host: wh000.pol.com
    Accept: text/x-playonline-pml, image/x-playonline-ang, image/png, ...

So a relay capture of a real SE session contains the request/response pairs in the
clear. This walks the c2s/s2c pairs, matches requests to responses in order
(HTTP/1.1 keep-alive, so several rides one connection), and writes each 200 body
to www/<Host>/<path> -- exactly where `_serve_http_on_lobby` looks for it. Run it
after a capture and the portal is served offline.

    python tools/extract_pml.py                 # extract into ../www
    python tools/extract_pml.py --dry-run       # list what would be written
    python tools/extract_pml.py --captures DIR --www DIR
"""
import argparse
import os
import re
import sys

METHODS = (b"GET ", b"POST ", b"HEAD ")


def split_requests(blob):
    """(path, host) for each HTTP request in a client->server stream."""
    out = []
    for m in re.finditer(rb"(?:GET|POST|HEAD) (\S+) HTTP/1\.[01]\r\n(.*?)\r\n\r\n",
                         blob, re.S):
        path = m.group(1).decode("latin1")
        hm = re.search(rb"(?im)^Host:\s*(\S+)", m.group(2))
        host = hm.group(1).decode("latin1").split(":")[0] if hm else "unknown"
        out.append((path, host))
    return out


def split_responses(blob):
    """(status, headers, body) for each HTTP response in a server->client stream."""
    out = []
    pos = 0
    while True:
        m = re.compile(rb"HTTP/1\.[01] (\d{3})[^\r\n]*\r\n").search(blob, pos)
        if not m:
            break
        hend = blob.find(b"\r\n\r\n", m.end())
        if hend < 0:
            break
        head = blob[m.end():hend]
        status = int(m.group(1))
        body_start = hend + 4
        cl = re.search(rb"(?im)^Content-Length:\s*(\d+)", head)
        if cl:
            n = int(cl.group(1))
            body = blob[body_start:body_start + n]
            pos = body_start + n
        elif re.search(rb"(?im)^Transfer-Encoding:\s*chunked", head):
            body, pos = read_chunked(blob, body_start)
        else:
            body = blob[body_start:]
            pos = len(blob)
        out.append((status, head, body))
        if pos <= m.end():
            break
    return out


def read_chunked(blob, pos):
    body = b""
    while True:
        nl = blob.find(b"\r\n", pos)
        if nl < 0:
            return body, len(blob)
        try:
            n = int(blob[pos:nl].split(b";")[0], 16)
        except ValueError:
            return body, len(blob)
        if n == 0:
            return body, nl + 2
        body += blob[nl + 2:nl + 2 + n]
        pos = nl + 2 + n + 2


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", default=os.path.join(here, "..", "logs", "captures"))
    ap.add_argument("--www", default=os.path.join(here, "..", "www"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pairs = {}
    for fn in sorted(os.listdir(a.captures)):
        if fn.endswith("-c2s.bin"):
            pairs.setdefault(fn[:-8], {})["c2s"] = os.path.join(a.captures, fn)
        elif fn.endswith("-s2c.bin"):
            pairs.setdefault(fn[:-8], {})["s2c"] = os.path.join(a.captures, fn)

    written = skipped = 0
    for stem, p in sorted(pairs.items()):
        if "c2s" not in p or "s2c" not in p:
            continue
        c2s = open(p["c2s"], "rb").read()
        if not c2s.startswith(METHODS) and b"HTTP/1.1\r\n" not in c2s[:400]:
            continue                      # not an HTTP-bearing connection
        reqs = split_requests(c2s)
        resps = split_responses(open(p["s2c"], "rb").read())
        if not reqs or not resps:
            continue
        for (path, host), (status, head, body) in zip(reqs, resps):
            if status != 200 or not body:
                skipped += 1
                continue
            rel = path.split("?", 1)[0].lstrip("/")
            if not rel or rel.endswith("/"):
                rel += "index.pml"
            dest = os.path.normpath(os.path.join(a.www, host, rel))
            if not dest.startswith(os.path.normpath(a.www)):
                continue                  # path traversal guard
            ctype = re.search(rb"(?im)^Content-Type:\s*(\S+)", head)
            print(f"  {host}{path}  {len(body)}B  "
                  f"{ctype.group(1).decode('latin1') if ctype else '?'}")
            if not a.dry_run:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(body)
            written += 1
    verb = "would write" if a.dry_run else "wrote"
    print(f"\n{verb} {written} file(s) under {os.path.normpath(a.www)}; "
          f"{skipped} non-200/empty response(s) skipped")
    if not written:
        print("nothing found -- has a real-SE session been captured with the "
              "pp000 band on the relay?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
