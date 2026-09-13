#!/usr/bin/env python3
"""The registration wizard, walked the way the Viewer walks it.

WHY THIS EXISTS. The sign-up flow ends by handing the client an RDT block that
makes the Viewer create the login-screen member itself, and until 2026-09-06
that mechanism had **never completed once against a real client** -- prod's
`logs/ucs.log` had ZERO step-7 fetches across every sign-up ever made. Three
separate live sign-ups (synthetic example IDs stand in here: MNOP3456 on 08-22,
QRST7890 on 08-25, UVWX2468 on 08-29) finished
registration, had their account created, saw an error code, and were bounced
back WITHOUT EVER BEING SHOWN THEIR PLAYONLINE ID. Nothing caught it because
nothing exercised the flow end to end: `pmlfit --ucs` lints each page in
isolation, and no suite walked the steps in order or looked at what step 6
actually returns.

So this pins the ORDER and the SCHEME -- the two things that were wrong:

  * **Step 6 shows the ID.** It used to answer with the RDT block and leave the
    ID to step 7, i.e. behind a hop this side cannot observe. Any RDT failure
    then cost the player the ID as well as the auto-add. If a change ever puts
    the completion screen back behind the hop, `step 6 is PML, not the block`
    goes red.
  * **NEXT-URL follows the client's scheme.** It used to be FORCED to https,
    which on the live plain-http route pointed the hop at :443 -- stub.py's
    self-signed listener, the POL-1328 cert wall. `_abs_url` now reads the
    Referer. Both directions are pinned, because deriving it from our own
    socket is wrong too (behind the SSLv3 terminator we only ever see plain
    HTTP).

It also pins the two things that were already right and are easy to break while
fixing the above: the `confirmed` gate that stops a bare `?step=6&t=anything`
URL minting a live account, and the `#`-in-password path (RDT_UNREPRESENTABLE)
where the block cannot be built honestly and Add Member is the only path.

Runs against a real listener on a scratch DB -- no prod state, no network.
"""
import os
import re
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(os.path.dirname(HERE), "services")


def _boot():
    """A ucscgi listener on a scratch DB. Returns (base_url, log_path, stop)."""
    tmp = tempfile.mkdtemp(prefix="ucssignup-")
    os.environ["POL_LOG_DIR"] = tmp
    os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "accounts.db")
    sys.path.insert(0, SERVICES)
    import accounts
    import ucscgi

    accounts.connect(os.environ["POL_ACCOUNTS_DB"]).close()   # builds the schema
    srv = ucscgi.Server(("127.0.0.1", 0), ucscgi.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return (f"http://127.0.0.1:{srv.server_address[1]}",
            os.path.join(tmp, "ucs.log"), srv.shutdown)


class Client:
    """Just enough of the Viewer: a Host header and the Referer it really sends.

    The Referer matters -- it is how `_abs_url` learns the scheme the client is
    speaking, so a request without one is not the request the client makes.
    """

    def __init__(self, base):
        self.base = base

    def get(self, path, referer=None, body=None):
        req = urllib.request.Request(self.base + path, data=body)
        req.add_header("Host", "ucs.pol.com")
        if referer:
            req.add_header("Referer", referer)
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.headers.get("Content-Type"), r.read().decode("utf-8",
                                                                 "replace")

    def walk_to_confirm(self, pw, handle):
        """Steps 1-5, ending with the confirm POST. Returns the token."""
        _, page = self.get(
            "/pml-cgi-bin/UUMNZ010.cgi?kinou_id=20&ret_url=tologin%3A")
        tok = re.search(r"t=([0-9a-f]{32})", page).group(1)
        for step in (2, 3):
            self.get(f"/pml-cgi-bin/?kinou_id=20&step={step}&t={tok}")
        self.get(f"/pml-cgi-bin/?kinou_id=20&step=4&t={tok}",
                 body=f"kinou_id=20&step=4&t={tok}".encode())
        self.get(f"/pml-cgi-bin/?kinou_id=20&step=5&t={tok}",
                 body=urllib.parse.urlencode(
                     {"kinou_id": 20, "step": 5, "t": tok, "pw1": pw,
                      "pw2": pw, "handle": handle}).encode())
        return tok


ID_RE = re.compile(r"\b[A-Z]{4}\d{4}\b")
FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def rdt_rows(doc):
    return dict(line.split(": ", 1) for line in doc.strip().split("\r\n"))


def main():
    base, logpath, stop = _boot()
    c = Client(base)
    try:
        # --- the happy path, over plain HTTP (the live route) ----------------
        print("[plain-http sign-up]")
        tok = c.walk_to_confirm("hunter2pw", "FlowTest")
        ref = f"http://ucs.pol.com/pml-cgi-bin/?kinou_id=20&step=5&t={tok}"

        ct, page = c.get(f"/pml-cgi-bin/?kinou_id=20&step=6&t={tok}",
                         referer=ref)
        check("step 6 is PML, not the RDT block", "playonline-pml" in ct, ct)
        found = ID_RE.search(page)
        check("step 6 SHOWS the issued PlayOnline ID", bool(found))
        polid = found.group(0) if found else "?"
        check("step 6 offers the auto-add", "Add to Login Screen" in page)
        check("auto-add points at the .rdt at step 7",
              "regist.rdt?kinou_id=20&step=7" in page)

        ct, doc = c.get(f"/pml-cgi-bin/regist.rdt?kinou_id=20&step=7&t={tok}",
                        referer=ref)
        check("step 7 is text/rdt WITH a charset (POL-1281)",
              ct == "text/rdt;charset=UTF-8", ct)
        rows = rdt_rows(doc)
        # The seven app.dll 0x4a65dc2 requires before it will call the creator.
        check("block carries all seven creator fields",
              all(k in rows for k in
                  ("POLCOM-HOST", "POL-ID", "POL-PASSWORD", "ACCOUNT-NUMBER",
                   "MASTER-POLID", "POL-MAIL-ADDRESS", "POL-MAIL-PASSWORD")),
              sorted(rows))
        check("block's POL-ID is the ID the player was shown",
              rows.get("POL-ID") == polid, f"{rows.get('POL-ID')} != {polid}")
        nxt = rows.get("NEXT-URL", "")
        check("NEXT-URL is http, NOT the https cert wall",
              nxt.startswith("http://"), nxt)
        check("NEXT-URL lands on step 8", "step=8" in nxt, nxt)

        _, page = c.get(f"/pml-cgi-bin/?kinou_id=20&step=8&t={tok}",
                        referer=ref)
        check("step 8 confirms the member was added",
              "added to the login screen" in page)

        # Re-serving step 6 is what a client does after an RDT it could not
        # apply. It must still name the account.
        _, page = c.get(f"/pml-cgi-bin/?kinou_id=20&step=6&t={tok}",
                        referer=ref)
        check("step 6 re-served still shows the ID", polid in page)

        log = open(logpath, encoding="utf-8").read()
        check("step 8 writes its completion log line", "step 8 REACHED" in log)
        check("the account was created exactly once",
              log.count(f"registered {polid}") == 1)

        # --- an https client must not be handed an http NEXT-URL -------------
        print("\n[https sign-up]")
        tok = c.walk_to_confirm("hunter2pw", "HttpsTest")
        c.get(f"/pml-cgi-bin/?kinou_id=20&step=6&t={tok}")
        _, doc = c.get(
            f"/pml-cgi-bin/regist.rdt?kinou_id=20&step=7&t={tok}",
            referer=f"https://ucs.pol.com/pml-cgi-bin/?kinou_id=20&step=6&t={tok}")
        nxt = rdt_rows(doc).get("NEXT-URL", "")
        check("https referer -> https NEXT-URL", nxt.startswith("https://"),
              nxt)

        # --- a password the RDT tokeniser would truncate ----------------------
        print("\n[password containing '#']")
        tok = c.walk_to_confirm("pass#word1", "HashTest")
        _, page = c.get(f"/pml-cgi-bin/?kinou_id=20&step=6&t={tok}")
        check("the ID is shown anyway", bool(ID_RE.search(page)))
        check("auto-add is withheld", "Add to Login Screen" not in page)
        check("Add Member is named as the path", "Add Member" in page)
        _, page = c.get(f"/pml-cgi-bin/regist.rdt?kinou_id=20&step=7&t={tok}")
        check("step 7 refuses to build a truncating block",
              "POL-PASSWORD" not in page)

        # --- the write gate ---------------------------------------------------
        print("\n[bare step-6 URL]")
        _, page = c.get(
            "/pml-cgi-bin/UUMNZ010.cgi?kinou_id=20&ret_url=tologin%3A")
        raw = re.search(r"t=([0-9a-f]{32})", page).group(1)
        _, page = c.get(f"/pml-cgi-bin/?kinou_id=20&step=6&t={raw}")
        check("an unconfirmed step 6 mints nothing",
              not ID_RE.search(page))
    finally:
        stop()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("ucs signup: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
