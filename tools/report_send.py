#!/usr/bin/env python3
"""File an issue report by hand -- and the reference encoder for the chord.

    python tools/report_send.py --host wh000.pol.com --text "shop menu froze"
    python tools/report_send.py --text "..." --file polshim.log --file shot.png
    python tools/report_send.py --dry-run --out bundle.bin        # no network

TWO JOBS, and the second is the important one.

1. It is how a report gets filed TODAY. The shim's report chord is the next
   piece of work; until it ships, this is the whole client half, and it means
   the server side can be proved end-to-end against a real running server
   without waiting on a shim publish and an autoupdate round.

2. **It is the executable specification of the bundle format.** `build_bundle`
   below is what `pol-shim`'s reporter must emit byte for byte. When the C++
   lands, the way to check it is to point this script's parser at what the shim
   produced -- not to read both and agree that they look similar. A format that
   exists twice in two languages and is written down in neither is a format
   that will drift, and the drift will show up as a 400 from a tester's machine
   at the exact moment they were trying to tell us something.

WARNING: WHAT THIS DOES NOT DO, DELIBERATELY: it does not redact. The shim redacts
before it sends (`logship.cpp`) and the server redacts shipped LOGS as a safety
net -- but a file you name on this command line is sent as-is. That is correct
for a tool a developer runs against files they chose; it is NOT the client path,
and nothing here should be copied into one without the redaction that
`logship.cpp` already implements.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import socket
import sys
import time

BUNDLE_MAGIC = b"==== POLSHIM-REPORT 1 ===="

#: Mirrors `issuereport._FILE_RX`. The shim will build names from a fixed list,
#: but a file named on the command line can be anything, so it is sanitised
#: here -- the server would reject the bundle outright otherwise, which is a
#: confusing way to learn that your log had a space in its name.
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(path: str) -> str:
    return _NAME_OK.sub("_", os.path.basename(path))[:64] or "file"


def build_bundle(meta: dict, files) -> bytes:
    """THE FORMAT. `files` is [(name, bytes)].

    Length-prefixed, because the payload is logs and any separator you pick can
    appear inside one. See the banner in services/issuereport.py."""
    out = [BUNDLE_MAGIC, b"\n"]
    for k, v in meta.items():
        if v is None or v == "":
            continue
        # Single-line scalars only -- a newline here would end the header block
        # early and turn the rest of the value into a bad FILE header.
        v = str(v).replace("\n", " ").replace("\r", " ")[:200]
        out.append(f"{k}: {v}\n".encode("utf-8"))
    out.append(b"\n")
    for name, blob in files:
        out.append(b"==== FILE %s %d ====\n" % (name.encode("ascii"), len(blob)))
        out.append(blob)
        out.append(b"\n")
    return b"".join(out)


def post(host: str, port: int, path: str, body: bytes, timeout: float = 20.0):
    """One plain HTTP POST. No requests dependency -- the tools tree runs on a
    bare container python, and this is four lines of socket."""
    req = (f"POST /{path.lstrip('/')} HTTP/1.1\r\n"
           f"Host: {host}\r\n"
           f"User-Agent: PolReport/1.0\r\n"
           f"Content-Type: application/octet-stream\r\n"
           f"Content-Length: {len(body)}\r\n"
           f"Connection: close\r\n\r\n").encode("latin1") + body
    with socket.create_connection((host, port), timeout) as s:
        s.sendall(req)
        buf = b""
        while len(buf) < 4096:
            try:
                more = s.recv(4096)
            except socket.timeout:
                break
            if not more:
                break
            buf += more
    return buf.split(b"\r\n", 1)[0].decode("latin1", "replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("POL_HOST", "127.0.0.1"),
                    help="server to post to (default $POL_HOST or 127.0.0.1)")
    ap.add_argument("--port", type=int, default=51300,
                    help="client-facing band port (default 51300)")
    ap.add_argument("--path", default="_shim/report")
    ap.add_argument("--text", help="the report itself; '-' reads stdin")
    ap.add_argument("--file", action="append", default=[],
                    help="attach a file (repeatable)")
    ap.add_argument("--handle", default=os.environ.get("POL_HANDLE", ""))
    ap.add_argument("--polid", default="")
    ap.add_argument("--title", default="", help="e.g. FMO, FE, TM, FFXI")
    ap.add_argument("--content-id", default="")
    ap.add_argument("--category", default="bug")
    ap.add_argument("--client-host", default=platform.node(),
                    help="the machine name to correlate on (default this box)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the bundle, send nothing")
    ap.add_argument("--out", help="also write the bundle to this path")
    args = ap.parse_args(argv)

    text = args.text
    if text == "-":
        text = sys.stdin.read()
    if not text and not args.file:
        ap.error("nothing to report: pass --text and/or --file")

    files = []
    if text:
        files.append(("description.txt", text.encode("utf-8")))
    for p in args.file:
        try:
            with open(p, "rb") as f:
                files.append((safe_name(p), f.read()))
        except OSError as e:
            print(f"cannot read {p}: {e}", file=sys.stderr)
            return 2

    meta = {
        "host": args.client_host,
        "handle": args.handle,
        "polid": args.polid,
        "title": args.title,
        "content_id": args.content_id,
        "category": args.category,
        "pid": os.getpid(),
        "os": f"{platform.system()} {platform.release()}",
        "client_build": "report_send.py",
        # OUR clock, recorded but NOT trusted -- the server cuts its window from
        # its own receipt time and files this beside it so a skew is visible.
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body = build_bundle(meta, files)

    if args.out:
        with open(args.out, "wb") as f:
            f.write(body)
        print(f"wrote {args.out} ({len(body)}B)")

    names = ", ".join(n for n, _ in files)
    if args.dry_run:
        print(f"dry run: {len(body)}B bundle, {len(files)} file(s): {names}")
        return 0
    try:
        status = post(args.host, args.port, args.path, body)
    except OSError as e:
        print(f"POST to {args.host}:{args.port} failed: {e}", file=sys.stderr)
        return 1
    print(f"{status}   ({len(body)}B, {len(files)} file(s): {names})")
    return 0 if " 200 " in status else 1


# --------------------------------------------------------------------------- #
# Self-test -- the ROUND TRIP, which is the only thing worth pinning here.
# --------------------------------------------------------------------------- #
def _selftest():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "services"))
    import issuereport

    ok = [0, 0]

    def check(name, cond):
        ok[1] += 1
        ok[0] += bool(cond)
        print(("  ok   " if cond else "  FAIL ") + name)

    # THE ONE PROPERTY THAT MATTERS: this encoder and that decoder are the same
    # format. Every other check in either file is downstream of this holding.
    payload = [
        ("description.txt", "line one\nline two -- with a ==== FILE x 1 ====\n"
                            "and a UTF-8 name: テスト".encode("utf-8")),
        ("shot.png", bytes(range(256)) * 4),      # binary, NUL bytes and all
        ("polshim.log", b""),                     # empty is legal
    ]
    meta = {"host": "DECK", "handle": "cas", "title": "FMO", "when": "now"}
    raw = build_bundle(meta, payload)
    got_meta, got_files = issuereport.parse_bundle(raw)

    check("round trip: header", got_meta["host"] == "DECK"
          and got_meta["handle"] == "cas" and got_meta["title"] == "FMO")
    check("round trip: file count", len(got_files) == len(payload))
    check("round trip: names", [n for n, _ in got_files] == [n for n, _ in payload])
    check("round trip: bytes are IDENTICAL",
          [b for _, b in got_files] == [b for _, b in payload])
    check("binary survives (NUL and high bytes)",
          got_files[1][1] == payload[1][1])
    check("an empty file survives as empty", got_files[2][1] == b"")
    check("a bundle containing the FILE separator is not split",
          got_files[0][1] == payload[0][1])

    # A multi-line value in a HEADER must not be able to forge a FILE header.
    sneaky = build_bundle({"host": "A\n==== FILE evil.log 3 ====\nBAD"}, payload)
    m2, f2 = issuereport.parse_bundle(sneaky)
    check("newline in a header value cannot forge a file",
          len(f2) == len(payload) and "evil.log" not in [n for n, _ in f2])
    check("...and the value is flattened, not dropped", m2["host"].startswith("A "))

    check("name sanitiser strips a path", safe_name("/etc/pass wd") == "pass_wd")
    check("name sanitiser strips traversal", "/" not in safe_name("../../x"))

    print(f"\nreport_send: {ok[0]}/{ok[1]} checks passed")
    return 0 if ok[0] == ok[1] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
