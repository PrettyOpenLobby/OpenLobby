#!/usr/bin/env python3
"""POL_AUTH_RSA_IPS: the RSA-wrapped key for listed client addresses only.

    python tests/test_auth_rsa_ips.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
tmp = tempfile.mkdtemp(prefix="rsaips-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
for k in ("POL_AUTH_RSA", "POL_AUTH_RSA_IPS"):
    os.environ.pop(k, None)

import responders  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


chk("unset: off", responders.auth_rsa_enabled("1.2.3.4"), False)
os.environ["POL_AUTH_RSA"] = ""
os.environ["POL_AUTH_RSA_IPS"] = ""
chk("compose empties: off", responders.auth_rsa_enabled("1.2.3.4"), False)
os.environ["POL_AUTH_RSA_IPS"] = "198.51.100.7, 203.0.113.9"
chk("listed address: on", responders.auth_rsa_enabled("198.51.100.7"), True)
chk("second listed address: on", responders.auth_rsa_enabled("203.0.113.9"), True)
chk("unlisted address: off", responders.auth_rsa_enabled("1.2.3.4"), False)
chk("no address: off", responders.auth_rsa_enabled(""), False)
os.environ["POL_AUTH_RSA"] = "1"
chk("POL_AUTH_RSA=1: on for anyone", responders.auth_rsa_enabled("1.2.3.4"), True)

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
