#!/usr/bin/env python3
"""Stranded friend requests come back at listing (POL_FRIEND_REQUEST_HEAL).

    python tests/test_friend_request_heal.py

Found 2026-09-27: the PS2 fetched and acknowledged Amara's and Birdie's
requests ~10 s after login, `_mail_retire` renamed them `.bin.read`, and the
PC never saw them; 2:3 hides `invited` rows, so nothing else showed them. With
the knob on, a 3:3 listing restores a retired request, mints one when none was
ever stored, and leaves alone anything the asker no longer waits on.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="reqheal-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(tmp, "accounts.db")
os.environ["POL_STAMP_FILE"] = os.path.join(tmp, "stamps.json")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
os.makedirs(os.environ["POL_RESOURCE_DIR"])
for k in ("POL_FRIEND_REQUEST_HEAL", "POL_FRIEND_REQUEST_HEAL_TTL"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import responders as R  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
ids = {}
for nm in ("Lex", "Amara", "Birdie", "Ghost"):
    accounts.register_account(db, nm, "Passw0rdTest", contents=(1,))
    ids[nm] = int(db.execute("SELECT id FROM handle WHERE handle_name = ?",
                             (nm,)).fetchone()["id"])
LEX_MEMBER = int(db.execute("SELECT member_id FROM handle WHERE id = ?",
                            (ids["Lex"],)).fetchone()["member_id"])
G = accounts.handle_guid

for asker in ("Amara", "Birdie", "Ghost"):
    chk("%s asks Lex" % asker, accounts.request_friend(db, ids[asker], "Lex"),
        "requested")
# Ghost withdrew: their own row is gone, so Lex's invited row waits on nobody.
db.execute("DELETE FROM friend WHERE handle_id = ?", (ids["Ghost"],))
db.commit()

# Amara's client posted its request; the PS2 read it and it was retired.
adrena = R._mail_mint("Amara", G(ids["Amara"]), G(ids["Lex"]),
                      R._FRIEND_REQ_SUBJECT, R._FRIEND_REQ_BODY,
                      kind=R.MAIL_KIND_FRIEND_REQUEST)
R._mail_retire(adrena)
res = os.environ["POL_RESOURCE_DIR"]
live_name = R._mail_name(adrena)


def requests():
    return sorted(m["sender"] for _w, _p, m in R._mailbox(LEX_MEMBER)
                  if m["kind"] == R.MAIL_KIND_FRIEND_REQUEST)


print("the stranded state")
chk("Amara's request is retired", os.path.exists(os.path.join(res, live_name
                                                               + ".read")), True)
chk("Lex's mailbox holds no request", requests(), [])

print("knob off (default): nothing happens")
chk("heal", R._friend_request_heal(LEX_MEMBER), 0)
chk("still no request", requests(), [])

print("knob on")
os.environ["POL_FRIEND_REQUEST_HEAL"] = "1"
chk("heal: Amara restored + Birdie minted, Ghost left",
    R._friend_request_heal(LEX_MEMBER), 2)
chk("Amara's file is back under its own name",
    (os.path.exists(os.path.join(res, live_name)),
     os.path.exists(os.path.join(res, live_name + ".read"))), (True, False))
chk("the mailbox lists both requests", requests(), ["Amara", "Birdie"])
lex = [p for _w, p, m in R._mailbox(LEX_MEMBER) if m["sender"] == "Birdie"][0]
meta = R._mail_meta(lex)
chk("the minted one is a 0x8080 from Birdie's guid",
    (meta["kind"], meta["sender_guid"]), (R.MAIL_KIND_FRIEND_REQUEST,
                                          G(ids["Birdie"])))
chk("addressed to Lex's handle",
    int(R._mail_recipient_row(db, meta["recipient_guid"])["id"]), ids["Lex"])
chk("a second listing does nothing (both live)", R._friend_request_heal(LEX_MEMBER),
    0)

print("retired again inside the TTL: left alone (no ping-pong with the PS2)")
R._mail_retire(adrena)
chk("heal", R._friend_request_heal(LEX_MEMBER), 0)
chk("Amara not listed", requests(), ["Birdie"])
os.environ["POL_FRIEND_REQUEST_HEAL_TTL"] = "-1"
chk("TTL expired: restored again", R._friend_request_heal(LEX_MEMBER), 1)
chk("listed", requests(), ["Amara", "Birdie"])

print("resolved requests are not revived")
chk("Lex accepts Amara", accounts.request_friend(db, ids["Lex"], "Amara"),
    "accepted")
R._mail_retire(adrena)
chk("heal after the accept", R._friend_request_heal(LEX_MEMBER), 0)
chk("only Birdie's is left", requests(), ["Birdie"])

db.close()
print("FAIL: %d check(s)" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
