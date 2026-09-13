#!/usr/bin/env python3
"""A registration code must redeem whatever case the player types it in.

REPLAYS A REAL FAILURE. prod `logs/ucs.log`, 2026-08-29, one player, one code:

    15:28:49  rc0=7cj3&rc1=hbyy&rc2=g7jg&rc3=s5v2&rc4=g8w9   -> refused
    15:29:24  rc0=7CJ3&rc1=HBYY&rc2=G7JG&rc3=S5V2&rc4=G8W9   -> accepted

Thirty-five seconds of somebody wondering what was wrong with their code, on
the one screen where a refusal reads as "this code is bad" rather than "try
shouting". The comparison was exact on purpose -- SE's own screen says the code
is case-sensitive -- but the codes are ours, every one we have ever issued is
upper case, and none of them collide when folded. So the rule protected nothing
and cost a sign-up.

Pins both halves, because the fix is easy to over-apply:
  * lookups fold case,
  * storage does NOT -- the row keeps the spelling it was issued with, which is
    what the audit trail and the admin listing show.

And the guard that keeps the two compatible: two codes differing only by case
cannot both exist, or a folded lookup would spend whichever row it happened to
return.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

import accounts                                            # noqa: E402

#: The code from the log above, verbatim.
CODE = "7CJ3-HBYY-G7JG-S5V2-G8W9"

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh():
    tmp = tempfile.mkdtemp(prefix="regcase-")
    db = accounts.connect(os.path.join(tmp, "accounts.db"))
    accounts.issue_regcode(db, CODE, contents=(1, 2))
    return db


def main():
    os.environ.setdefault("POL_LOG_DIR", tempfile.mkdtemp())

    print("[lookup folds case]")
    for typed, label in ((CODE.lower(), "all lower (the live failure)"),
                         (CODE.upper(), "all upper (what they retyped)"),
                         ("7cJ3-HbYy-G7jG-s5V2-g8W9", "mixed")):
        db = fresh()
        check(f"{label} is accepted",
              accounts.check_regcode(db, typed) is not None, typed)
        db.close()

    print("\n[redemption works from a lower-case entry]")
    db = fresh()
    granted = accounts.redeem_regcode(db, CODE.lower(), None)
    check("redeem_regcode grants the contents", granted == [1, 2], str(granted))
    check("the code is spent afterwards",
          accounts.check_regcode(db, CODE) is None)
    check("...and spent for the lower-case spelling too",
          accounts.check_regcode(db, CODE.lower()) is None)
    row = db.execute("SELECT code FROM regcode").fetchone()
    check("STORAGE keeps the issued spelling", row["code"] == CODE,
          row["code"])
    db.close()

    print("\n[register_account redeems a lower-case code]")
    db = fresh()
    acct = accounts.register_account(db, "CaseTest", "hunter2pw",
                                     code=CODE.lower())
    check("the account is granted the code's contents",
          sorted(acct["contents"])[:2] == [1, 2], str(acct["contents"]))
    spent = db.execute("SELECT redeemed_by FROM regcode").fetchone()
    check("the code is marked redeemed by that account",
          spent["redeemed_by"] == acct["polid"], str(spent["redeemed_by"]))
    db.close()

    print("\n[a case-only duplicate cannot be issued]")
    db = fresh()
    try:
        accounts.issue_regcode(db, CODE.lower(), contents=(3,))
        check("issuing a case-only duplicate is refused", False,
              "it was accepted")
    except accounts.RegistrationError:
        check("issuing a case-only duplicate is refused", True)
    n = db.execute("SELECT COUNT(*) c FROM regcode").fetchone()["c"]
    check("still exactly one code on file", n == 1, str(n))
    # Re-issuing the SAME spelling is an update, not a clash, and must still work.
    try:
        accounts.issue_regcode(db, CODE, contents=(4,))
        check("re-issuing the same spelling still works", True)
    except accounts.RegistrationError as exc:
        check("re-issuing the same spelling still works", False, str(exc))
    db.close()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}")
        return 1
    print("regcode case: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
