#!/usr/bin/env python3
"""Does POST /_shim/report actually reach issuereport, on the real HTTP door?

    python tools/issue_route_test.py

WHY THIS EXISTS SEPARATELY FROM `issuereport.py`'s OWN SUITE. That suite proves
the mechanism -- the window bisect, the correlation tiering, the retention caps
-- by calling the functions directly. It cannot fail if the ROUTE is wrong, and
the route is four lines in a 27,000-line file that no test touched: a dispatch
arm, a guarded import, a path constant, and the body-preview suppression. Every
one of those can be silently wrong (an arm placed after a `continue`, an import
that quietly binds None, a path that does not match `rel`) while both halves are
individually perfect. That is precisely the shape of bug this project keeps
paying for: two correct pieces and a seam nobody exercised.

So this drives the REAL handler -- `_serve_http_on_lobby`, the same function the
lobby's HTTP band calls -- over a socketpair, with a real POST on the wire. No
service, no container, no port.

WARNING: THE SHIM WILL HIT THIS EXACT SEAM. When the report chord lands, its first
failure mode is a 404 or a 400 from a tester's machine at the moment they were
trying to tell us something, which is the worst possible time to discover a
routing bug. This is the check that keeps that from being how we find out.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    ok = [0, 0]

    def check(name, cond, detail=""):
        ok[1] += 1
        ok[0] += bool(cond)
        print(("  ok   " if cond else "  FAIL ") + name
              + (f"   {detail}" if detail and not cond else ""))

    with tempfile.TemporaryDirectory() as td:
        logs = os.path.join(td, "logs")
        issues = os.path.join(td, "issues")
        os.makedirs(logs)
        # One real-shaped channel log, so the window cut has something to find
        # and the correlation has something to match.
        #
        # WARNING: STAMPED RELATIVE TO **NOW**, not a fixed date. The first version of
        # this fixture used a hardcoded 2026-09-01 and the cut came back empty:
        # the window is [now-10min, now+1min] by construction, so a fixture with
        # yesterday's dates tests nothing and reads as "the cut is broken". It
        # did at least prove the honest-negative path -- the run said, correctly,
        # that no server line could be tied to the client -- but the positive
        # case is the one this check is for.
        now = time.time()
        with open(os.path.join(logs, "lobby.log"), "wb") as f:
            for i in range(200):
                t = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(now - 300 + i))
                f.write(f"{t} [lobby] 192.0.2.5:5000 served a thing\n".encode())
        os.environ["POL_LOG_DIR"] = logs
        os.environ["POL_ISSUE_DIR"] = issues

        sys.path.insert(0, os.path.join(ROOT, "services"))
        sys.path.insert(0, HERE)
        import responders                                  # noqa: E402
        import report_send                                 # noqa: E402

        # The guarded import must have BOUND, not fallen through to None -- a
        # missing dependency would make every report a 503 and nothing else on
        # the server would look different.
        check("responders imported issuereport",
              getattr(responders, "issuereport", None) is not None)
        check("the route constant is what the shim will POST",
              responders._REPORT_PATH == "_shim/report",
              f"got {responders._REPORT_PATH!r}")

        def post(path, body, expect_first):
            req = (f"POST /{path} HTTP/1.1\r\nHost: wh000.pol.com\r\n"
                   f"Content-Length: {len(body)}\r\n"
                   f"Connection: close\r\n\r\n").encode() + body
            a, b = socket.socketpair()
            out = {}

            def serve():
                try:
                    out["n"] = responders._serve_http_on_lobby(
                        b, b"", "192.0.2.5:5000", 51300)
                except Exception as e:                     # noqa: BLE001
                    out["err"] = repr(e)
                # WARNING: CLOSE THE SERVER END WHEN THE HANDLER RETURNS, or the read
                # below has nothing to wait for but its own timeout. The real
                # server closes the connection in its accept loop, not in this
                # function, so a socketpair harness has to do it -- without this
                # every request in this suite cost a full 15 s of timeout and
                # the suite took 60 s to do half a second of work.
                try:
                    b.close()
                except OSError:
                    pass
            t = threading.Thread(target=serve)
            t.start()
            a.sendall(req)
            a.shutdown(socket.SHUT_WR)
            a.settimeout(15)
            resp = b""
            try:
                while True:
                    d = a.recv(4096)
                    if not d:
                        break
                    resp += d
            except OSError:
                pass
            t.join(15)
            a.close()
            # Split off the body so a caller can assert on what came back, not
            # just the status line.
            head, _, rbody = resp.partition(b"\r\n\r\n")
            out["body"] = rbody
            return head.split(b"\r\n")[0], out

        # -- the happy path -------------------------------------------------- #
        body = report_send.build_bundle(
            {"host": "DECK", "handle": "cas", "title": "FE"},
            [("description.txt", b"the capital door would not open"),
             ("polshim.log", b"[fe] 0x20AC sent, no 0x1166\n")])
        line, out = post("_shim/report", body, b"200")
        check("no exception escaped the handler", "err" not in out,
              out.get("err", ""))
        check("POST /_shim/report -> 200", line.startswith(b"HTTP/1.1 200"),
              repr(line))
        ids = os.listdir(issues) if os.path.isdir(issues) else []
        check("a bundle was actually filed", len(ids) == 1, repr(ids))
        # THE ID COMES BACK IN THE BODY, and it must be the id of the bundle
        # that was actually written -- the shim shows this to the tester, so an
        # id that names nothing is worse than no id at all.
        check("the response body IS the report id",
              bool(ids) and out.get("body", b"").decode() == ids[0],
              f"body={out.get('body')!r} dirs={ids}")
        if ids:
            d = os.path.join(issues, ids[0])
            check("the client's files landed",
                  os.path.exists(os.path.join(d, "client", "description.txt")))
            check("OUR logs were cut into it",
                  os.path.exists(os.path.join(d, "server", "lobby.log")))
            check("a manifest was written",
                  os.path.exists(os.path.join(d, "manifest.json")))

        # -- a malformed bundle is REFUSED, not stored ----------------------- #
        line, out = post("_shim/report", b"this is not a bundle", b"400")
        check("garbage -> 400", line.startswith(b"HTTP/1.1 400"), repr(line))
        n_after = len(os.listdir(issues)) if os.path.isdir(issues) else 0
        check("garbage filed nothing", n_after == len(ids), f"{n_after} dirs")

        # -- an empty body is refused too ------------------------------------ #
        line, _ = post("_shim/report", b"", b"400")
        check("empty body -> 400", line.startswith(b"HTTP/1.1 400"), repr(line))

        # THE ROUTE MUST NOT HAVE EATEN THE REST OF THE DOOR. The dispatch arm
        # sits above the shim-log arm and the portal lookup; a `continue` in the
        # wrong place there would swallow every other request on the band, which
        # is the whole portal. So prove an unrelated path still gets an answer.
        a, b = socket.socketpair()
        out2 = {}

        def serve2():
            try:
                out2["n"] = responders._serve_http_on_lobby(
                    b, b"", "192.0.2.5:5000", 51300)
            except Exception as e:                         # noqa: BLE001
                out2["err"] = repr(e)
            try:
                b.close()          # same reason as in `post` above
            except OSError:
                pass
        t = threading.Thread(target=serve2)
        t.start()
        a.sendall(b"GET /nothing-here HTTP/1.1\r\nHost: wh000.pol.com\r\n"
                  b"Connection: close\r\n\r\n")
        a.shutdown(socket.SHUT_WR)
        a.settimeout(15)
        resp = b""
        try:
            while True:
                d = a.recv(4096)
                if not d:
                    break
                resp += d
        except OSError:
            pass
        t.join(15)
        a.close()
        check("an ordinary GET on the same door still answers",
              resp.startswith(b"HTTP/1.1"), repr(resp[:40]))

        # WARNING: CLOSE srvcore's LOG HANDLES BEFORE THE TEMP DIR GOES AWAY. On
        # Windows an open handle makes a file undeletable, and
        # TemporaryDirectory's cleanup then retries that FILE as a directory and
        # dies with `NotADirectoryError: ...\logs\lobby.log` -- a traceback that
        # looks like a failure of the code under test and is nothing of the
        # kind. srvcore caches one handle per channel, and `log()` opened one
        # here the moment the first report was filed.
        import srvcore                                     # noqa: E402
        for fh in list(getattr(srvcore, "_LOG_FILES", {}).values()):
            try:
                fh.close()
            except OSError:
                pass
        getattr(srvcore, "_LOG_FILES", {}).clear()

    print(f"\nissue_route: {ok[0]}/{ok[1]} checks passed")
    return 0 if ok[0] == ok[1] else 1


if __name__ == "__main__":
    sys.exit(main())
