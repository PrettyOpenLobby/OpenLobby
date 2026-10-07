#!/usr/bin/env python3
"""Does a PS2 session that ends badly file its own report -- and only then?

    python tools/issue_auto_test.py

`issuereport.auto_ps2` is the server filing a report on the console's behalf
(a PS2 has no report key, and a frozen one could not press it). Called from
authserv's teardown and authresume's PING tick; this drives it directly with a
real channel log and a real issues directory, and checks both directions: a
bundle lands when it should, and NOTHING lands for a player who came back or
was already reported.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SID = "u0123456789abcdef"


def main():
    ok = [0, 0]

    def check(name, cond, detail=""):
        ok[1] += 1
        ok[0] += bool(cond)
        print(("  ok   " if cond else "  FAIL ") + name
              + (f"   {detail}" if detail and not cond else ""))

    def settle():
        for t in threading.enumerate():
            if t.name == "issue-auto":
                t.join(10)

    # ignore_cleanup_errors: srvcore keeps its log open, which Windows refuses to delete.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        logs = os.path.join(td, "logs")
        issues = os.path.join(td, "issues")
        os.makedirs(logs)
        now = time.time()
        # Stamped relative to NOW (see issue_route_test): the window is cut
        # around the call, so yesterday's lines would test nothing.
        with open(os.path.join(logs, "authserv.log"), "wb") as f:
            for i in range(120):
                t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 240 + i))
                who = f"session {SID}" if i % 10 == 0 else "someone else"
                f.write(f"{t} [authserv] 192.0.2.9:4000 {who}\n".encode())
        os.environ["POL_LOG_DIR"] = logs
        os.environ["POL_ISSUE_DIR"] = issues
        os.environ["POL_ISSUE_AUTO"] = "1"

        sys.path.insert(0, os.path.join(ROOT, "services"))
        import issuereport as r                            # noqa: E402
        import accounts                                    # noqa: E402

        # The trigger keys on this mapping; if it ever stops saying PS2 the
        # feature goes quiet without a single error.
        check("both PS2 signatures label as PS2",
              accounts.client_label("TTTTT7ITTTGaItbIQ8nHA") == "PS2"
              and accounts.client_label("TTTTT7ISTTSt6L0zkUB@X") == "PS2")
        check("a PC signature does not",
              accounts.client_label("TTTTTAISTTTTTTTTTTTTT") != "PS2")

        def bundles():
            return sorted(os.listdir(issues)) if os.path.isdir(issues) else []

        # 1. A drop for a player who stays gone: filed.
        r.auto_ps2(1, "alice", SID, "192.0.2.9:4000",
                   "the connection ended with: socket read failed", at=now,
                   delay=0.2, still_gone=lambda: True)
        settle()
        got = bundles()
        check("a bundle was filed", len(got) == 1, f"{got}")
        man = {}
        if got:
            with open(os.path.join(issues, got[0], "manifest.json"),
                      encoding="utf-8") as f:
                man = json.load(f)
        check("it says it is automatic and why",
              man.get("category") == "auto"
              and "socket read failed" in man.get("description", ""),
              f"{man.get('category')!r} {man.get('description')!r}")
        check("it carries OUR log, cut", "authserv.log" in man.get("server_files", []),
              f"{man.get('server_files')}")
        check("correlated on the SESSION, not the shared peer",
              man.get("window", {}).get("correlated_by") == ["session"],
              f"{man.get('window', {}).get('correlated_by')}")
        check("no client folder for a client that sent nothing",
              bool(got) and not os.path.exists(os.path.join(issues, got[0], "client")))

        # 2. Same account again inside the gap: the same incident, not filed.
        r.auto_ps2(1, "alice", SID, "192.0.2.9:4000", "read timed out", at=now)
        settle()
        check("a second report inside AUTO_GAP is not filed", len(bundles()) == 1,
              f"{bundles()}")

        # 3. A player who came back within the delay: nothing, and the gap is
        #    released so a real crash later is still reported.
        r.auto_ps2(2, "bob", "u" + "f" * 16, "192.0.2.10:4000",
                   "the connection ended with: TCP refused our PING", at=now,
                   delay=0.2, still_gone=lambda: False)
        settle()
        check("back online -> nothing filed", len(bundles()) == 1, f"{bundles()}")
        check("...and the gap is released", 2 not in r._auto_last)
        r.auto_ps2(2, "bob", "u" + "f" * 16, "192.0.2.10:4000",
                   "no reply to our PINGs for 200s", at=now)
        settle()
        check("a later real incident for that player is filed", len(bundles()) == 2,
              f"{bundles()}")

        # 4. The switch.
        r.AUTO_ON = False
        r.auto_ps2(3, "carol", "u" + "e" * 16, "192.0.2.11:4000", "x", at=now)
        settle()
        check("POL_ISSUE_AUTO=0 files nothing", len(bundles()) == 2)

    print(f"\nissue_auto: {ok[0]}/{ok[1]} checks passed")
    return 0 if ok[0] == ok[1] else 1


if __name__ == "__main__":
    sys.exit(main())
