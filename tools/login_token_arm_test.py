#!/usr/bin/env python3
"""The login-token gate: a never-played account must not be takeable by anyone
who knows its POL ID, and a real player must still get in.

    python tools/login_token_arm_test.py

The lobby binds a login token trust-on-first-use, because the token is a
function of the password nobody has reversed. That is a password-free admission,
so it is gated on the account being ARMED -- and what arms it (creating the
account, or an operator's `accounts.py arm`) follows a real password proof.
These checks drive `accounts` directly: they are about the RULE, not the wire.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import accounts  # noqa: E402
import pgtest  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db_path = pgtest.use_fresh_database()
db = accounts.connect(db_path)

acct = accounts.register_account(db, "Armed", "abc12345", contents=(1,))
mid = acct["member_id"]
chk("a new account is armed by its own creation", accounts.login_token_armed(db, mid), True)
chk("...and holds no login token yet", accounts.get_login_token(db, mid), None)

# the window lapses
db.execute("UPDATE member SET token_arm_until = %s WHERE id = %s",
           ("2020-01-01T00:00:00Z", mid))
db.commit()
chk("a lapsed window is not armed", accounts.login_token_armed(db, mid), False)

# ...and re-arming (accounts.py arm) opens it again
accounts.arm_login_token(db, mid, hours=24)
chk("proving the password re-arms it", accounts.login_token_armed(db, mid), True)

# arming only ever EXTENDS: a 1-hour arm must not shorten a 24-hour one
long_until = accounts.arm_login_token(db, mid, hours=24)
short = accounts.arm_login_token(db, mid, hours=1)
chk("a shorter arm never takes away time already granted", short, long_until)

# a second account, never armed at all (what an account predating this looks like)
acct2 = accounts.register_account(db, "Legacy", "abc12345", contents=(1,))
mid2 = acct2["member_id"]
db.execute("UPDATE member SET token_arm_until = NULL WHERE id = %s", (mid2,))
db.commit()
chk("an account that was never armed is not armed", accounts.login_token_armed(db, mid2), False)

# the arming state is per account
chk("arming one account does not arm another",
    (accounts.login_token_armed(db, mid), accounts.login_token_armed(db, mid2)), (True, False))

# once a token is bound, the account is no longer in the first-ever case at all
accounts.set_client_token(db, mid, "TTTTTAISTTTTTTTTTTTTT", "AbCdEfGhIjK")
chk("a bound client token is remembered",
    accounts.get_client_token(db, mid, "TTTTTAISTTTTTTTTTTTTT"), "AbCdEfGhIjK")
chk("...and a DIFFERENT client of the same account has none yet",
    accounts.get_client_token(db, mid, "TTTTT7ITTTGaItbIQ8nHA"), None)

db.close()
print("\nFAILURES:", bad)
sys.exit(1 if bad else 0)
