#!/usr/bin/env python3
"""Per-account refusals and notices must not change anything else about a login.

    python tests/test_notice_keeps_layouts.py

Two checks:
  * a login notice travels in the token byte the lobby reads back to choose PS2
    record layouts; with a notice set, that choice must be what it would have
    been without one;
  * an account that is not active and has no explicit refusal code is refused
    with the unknown-ID byte (0xC9), as before per-account codes existed, and
    the PlayOnline ID's own status is not consulted.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()

tmp = tempfile.mkdtemp(prefix="noticetest-")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
os.environ.pop("POL_ACCT_STATUS", None)

import accounts   # noqa: E402
import responders as R  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


print("PS2 layout choice with a notice in the token byte ->")
chk("no notice (byte 0x00)", R._hello_is_ps2(0x00), True)
for code in sorted(accounts.LOGIN_INFORMATION_CODES):
    chk(f"notice {code:#04x}", R._hello_is_ps2(code), R._hello_is_ps2(R._ACCT_STATUS))
chk("no hello byte at all", R._hello_is_ps2(None), False)

print("\nrefusal without an explicit code ->")
db = accounts.connect()
m = accounts.ensure_member(db, "NoticeTest")
chk("active member", accounts.login_reject_code(db, m), 0)
db.execute("UPDATE polid SET status = 'suspended' WHERE polid = %s", (m["polid"],))
db.commit()
m = db.execute("SELECT * FROM member WHERE id = %s", (m["id"],)).fetchone()
chk("polid suspended, member active: still logs in", accounts.login_reject_code(db, m), 0)
db.execute("UPDATE member SET status = 'suspended' WHERE id = %s", (m["id"],))
db.commit()
m = db.execute("SELECT * FROM member WHERE id = %s", (m["id"],)).fetchone()
chk("member suspended: unknown-ID byte", accounts.login_reject_code(db, m), 0xC9)
db.close()

print("\nnotice/refusal checks: " + ("OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
