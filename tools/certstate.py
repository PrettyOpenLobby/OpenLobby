#!/usr/bin/env python3
"""One command: is this client actually configured to trust our current CA?

    python tools/certstate.py

Checks BOTH trust stores against the CA the server is serving right now, by
SHA-1 fingerprint rather than by name.

## Why this exists

A test was run with the CURRENT CA in app.dll and a STALE one -- same subject,
different key, expiry past the 2038 cliff -- still sitting in cert.db from an
earlier attempt. A client that resolves an issuer by name and finds the wrong
key fails validation regardless of what the other store holds, so the result
looked like "the app.dll bundle is not the trust anchor" when it was really
"the two stores disagree".

Matching on NAME is what made that possible. This matches on fingerprint, and
prints the mismatch loudly when a root with our subject is present but is not
our current key.
"""
import argparse
import base64
import hashlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import certdb                                                  # noqa: E402
import appca                                                   # noqa: E402

DEFAULT_POL = r"C:\Program Files (x86)\PlayOnline\SquareEnix\PlayOnlineViewer"
OUR_SUBJECT = "PlayOnline Revival"


# PURE PYTHON, no openssl. A certificate fingerprint is just SHA-1 over the DER,
# and this script is called by the installer from an ELEVATED PowerShell where
# openssl is typically not on PATH -- it is on PATH under Git Bash, which is why
# the dependency went unnoticed until it printed
#   could not read a fingerprint ... (is openssl on PATH?)
# after a perfectly successful install, and reported FAIL on a good state.
def fingerprint(der):
    h = hashlib.sha1(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def pem_fingerprint(path):
    try:
        text = open(path, "r", encoding="ascii", errors="replace").read()
    except OSError:
        return None
    m = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
                  text, re.S)
    if not m:
        return None
    try:
        return fingerprint(base64.b64decode("".join(m.group(1).split())))
    except Exception:
        return None


def report(name, roots, want):
    """roots: [(label, der)]. Returns True if our current CA is present."""
    ours = [(lab, der) for lab, der in roots if OUR_SUBJECT in lab]
    if not ours:
        print(f"  {name:10s} {len(roots):2d} roots, ours ABSENT")
        return False
    ok = False
    for lab, der in ours:
        fp = fingerprint(der)
        if fp == want:
            print(f"  {name:10s} {len(roots):2d} roots, ours PRESENT and current")
            ok = True
        else:
            print(f"  {name:10s} {len(roots):2d} roots, ours PRESENT but STALE")
            print(f"             has  {fp}")
            print(f"             want {want}")
            print(f"             ^ same name, different key -- REMOVE IT, it can "
                  f"fail validation on its own")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pol", default=os.environ.get("POL_ROOT", DEFAULT_POL))
    ap.add_argument("--service", default="ssl3ucs",
                    help="compose service to read the LIVE CA from")
    ap.add_argument("--no-server", action="store_true",
                    help="do not consult the container; trust --ca")
    ap.add_argument("--ca", default=os.path.join(
        os.path.dirname(HERE), "pol-client", "certs", "pol-ca.pem"))
    a = ap.parse_args()

    # The authority is the RUNNING SERVER, not a file we exported at some point.
    #
    # This compared against pol-client/certs/pol-ca.pem, which is a COPY. When
    # the CA in the container was regenerated (a PS2 DNAS experiment, leaving
    # .bak-prePS2ca files behind), the copy went stale and this happily reported
    # "PRESENT and current" for stores that no longer matched what the server
    # was signing with -- the exact class of false pass it was written to catch.
    live = None
    if not a.no_server:
        try:
            r = subprocess.run(
                ["docker", "compose", "exec", "-T", a.service,
                 "cat", "/certs/pol-ca.pem"],
                capture_output=True, text=True, timeout=30,
                cwd=os.path.dirname(HERE))
            if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
                live = fingerprint(base64.b64decode("".join(re.search(
                    r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
                    r.stdout, re.S).group(1).split())))
        except Exception as exc:
            print(f"  (could not read the live CA from `{a.service}`: {exc})")

    want = pem_fingerprint(a.ca)
    if live and want and live != want:
        print(f"WARNING: {a.ca} is STALE.")
        print(f"  exported copy {want}")
        print(f"  live server   {live}")
        print("  Using the LIVE one. Re-export it and rebuild the stores:")
        print(f"    docker compose exec {a.service} cat /certs/pol-ca.pem "
              f"> pol-client/certs/pol-ca.pem\n")
    want = live or want
    if not want:
        sys.exit(f"no CA fingerprint available (tried the `{a.service}` "
                 f"container and {a.ca})")
    print(f"server CA {'(live from ' + a.service + ')' if live else a.ca}")
    print(f"  {want}\n")

    found = []

    cdb = os.path.join(a.pol, "usr", "all", "url", "cert.db")
    if os.path.isfile(cdb):
        db = certdb.CertDB(open(cdb, "rb").read())
        roots = [certdb.CertDB.split_entry(e) for e in db.certs()]
        found.append(report("cert.db", roots, want))
    else:
        print(f"  cert.db    NOT FOUND at {cdb}")

    app = os.path.join(a.pol, "viewer", "com", "app.dll")
    if os.path.isfile(app):
        d = open(app, "rb").read()
        w, body, total = appca.find_bundle(d)
        roots = [appca.split_entry(d, o, l) for o, l in appca.entries(d, body, total)]
        found.append(report("app.dll", roots, want))
    else:
        print(f"  app.dll    NOT FOUND at {app}")

    print()
    if all(found) and found:
        print("Both stores carry the current CA.")
        return 0
    print("Not consistent. A test run in this state does not tell you which "
          "store is\nauthoritative -- fix the stale/absent entries first.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
