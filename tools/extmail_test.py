#!/usr/bin/env python3
"""Pin mail to and from the internet (services/extmail.py).

Drives the REAL SMTP handler over a socket, with the provider's HTTPS API
stubbed, and extmail.receive for inbound. The outside domain is the reserved
example.net. What it holds in place:

  * an outside recipient is REFUSED at RCPT (visibly, 550) unless the sender
    is enabled in the admin panel AND outbound is configured, rather than
    accepted and silently dropped;
  * an enabled sender's mail leaves as `"Name" <mailname@example.net>`,
    whatever the client wrote, and is logged; the daily cap holds;
  * a provider failure is a 554, not a false success;
  * `name@example.net` from inside the game is delivered locally;
  * inbound mail is flattened to Viewer-safe ISO-8859-1 text and reaches only
    enabled members, by mail name or POL ID.

Run: python tools/extmail_test.py
"""
import base64
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
_TMP = tempfile.mkdtemp(prefix="extmail-")
DB = os.path.join(_TMP, "accounts.db")
os.environ.update(POL_ACCOUNTS_DB=DB, POL_ACCOUNTS="1",
                  POL_LOG_DIR=os.path.join(_TMP, "logs"))
for k in ("POL_EXT_MAIL_KEY", "POL_EXT_MAIL_DAILY_OUT", "POL_EXT_MAIL_DAILY_IN",
          "POL_EXT_MAIL_DOMAIN", "POL_MAIL_STRICT", "POL_MAIL_SESSION_CHECK",
          "POL_MAIL_SENDER_CHECK"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import extmail  # noqa: E402
import responders as R  # noqa: E402

DOMAIN = "example.net"
os.environ["POL_EXT_MAIL_DOMAIN"] = DOMAIN


class regapi:                                        # noqa: N801 -- test shim
    """The inbound path, called the way a relay would call it."""
    @staticmethod
    def inbound_mail(rcpt, sender, raw_b64):
        db = accounts.connect(DB)
        try:
            return extmail.receive(db, rcpt, sender, raw_b64)
        finally:
            db.close()

FAILED = []


def check(label, got, want=True):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label
          + ("" if ok else f": {got!r}  (want {want!r})"))
    if not ok:
        FAILED.append(label)


c = accounts.connect(DB)
accounts.create_polid(c, "EXTP1234", "polid-pw-1")
eo = accounts.add_member(c, "EXTP1234", "eomember", "pw-eo-0001")
accounts.assign_mail_address(c, eo, "pomona")
accounts.open_session(c, eo, nick="Pom", peer_ip="127.0.0.1")
accounts.create_polid(c, "OFFP5678", "polid-pw-2")
off = accounts.add_member(c, "OFFP5678", "offmember", "pw-off-0001")
accounts.assign_mail_address(c, off, "offie")
accounts.open_session(c, off, nick="Off", peer_ip="127.0.0.1")
c.commit()

SENT = []
PROVIDER = {"status": 200}


def fake_post(url, headers, payload):
    SENT.append((url, headers, payload))
    if PROVIDER["status"] != 200:
        return PROVIDER["status"], {"message": "provider says no"}
    return 200, {"id": "prov-%d" % len(SENT)}


extmail._post = fake_post


def smtp(sender, rcpts, subject="hi", body="Hello there.", name="Pat Sample"):
    """Run one SMTP session against handle_smtp. Returns {step: reply code}."""
    a, b = socket.socketpair()
    t = threading.Thread(target=R.handle_smtp, args=(b, ("127.0.0.1", 5555), 51261))
    t.start()
    f = a.makefile("rwb")
    out = {"banner": f.readline()[:3].decode()}

    def say(line):
        f.write(line.encode() + b"\r\n")
        f.flush()
        return f.readline()[:3].decode()
    say("EHLO pol.com")
    f.readline()                                     # the second EHLO line
    out["from"] = say(f"MAIL FROM: {sender}")
    out["rcpt"] = [say(f"RCPT TO: {r}") for r in rcpts]
    if "250" in out["rcpt"]:
        say("DATA")
        f.write((f'From: "{name}" <{sender}>\r\nTo: {", ".join(rcpts)}\r\n'
                 f"Subject: {subject}\r\nContent-Type: text/plain; charset=ISO-8859-1\r\n"
                 f"\r\n{body}\r\n.\r\n").encode())
        f.flush()
        out["data"] = f.readline()[:3].decode()
    say("QUIT")
    t.join(5)
    a.close()
    return out


def enable(mid, on=True):
    c.execute("UPDATE member SET ext_mail = ? WHERE id = ?", (1 if on else 0, mid))
    c.commit()


print("\nThe gate: nobody mails outside unless enabled AND configured")
r = smtp("EXTP1234@pol.com", ["friend@example.com"])
check("not enabled: the outside RCPT is REFUSED (550), not accepted and dropped",
      r["rcpt"], ["550"])
enable(eo)
r = smtp("EXTP1234@pol.com", ["friend@example.com"])
check("enabled but no POL_EXT_MAIL_KEY: still 550 (the kill switch)", r["rcpt"], ["550"])
os.environ["POL_EXT_MAIL_KEY"] = "test-key"
os.environ["POL_EXT_MAIL_DOMAIN"] = ""
r = smtp("EXTP1234@pol.com", ["friend@example.com"])
check("key but no POL_EXT_MAIL_DOMAIN: still 550 (inert without its domain)",
      r["rcpt"], ["550"])
os.environ["POL_EXT_MAIL_DOMAIN"] = DOMAIN
os.environ.pop("POL_EXT_MAIL_KEY")
check("...and nothing reached the provider", len(SENT), 0)
os.environ["POL_EXT_MAIL_KEY"] = "test-key"
r = smtp("offie@pol.com", ["friend@example.com"])
check("another member, never enabled: 550", r["rcpt"], ["550"])
check("internal mail is untouched by the gate (offie -> pomona@pol.com)",
      smtp("offie@pol.com", ["pomona@pol.com"])["data"], "250")

print("\nAn enabled member, configured")
r = smtp("EXTP1234@pol.com", ["friend@example.com", "pomona@pol.com"],
         subject="From the game", body="Can you read this?")
check("RCPT outside accepted, local accepted", r["rcpt"], ["250", "250"])
check("DATA 250", r["data"], "250")
url, hdrs, payload = SENT[-1]
check("sent through Resend by default", url, "https://api.resend.com/emails")
check("From is the member's OUTSIDE address with their display name",
      payload["from"], '"Pat Sample" <pomona@example.net>')
check("only the outside recipient went to the provider", payload["to"], ["friend@example.com"])
check("reply_to is the outside address", payload["reply_to"], "pomona@example.net")
check("subject and text carried", (payload["subject"], payload["text"].strip()),
      ("From the game", "Can you read this?"))
check("the local copy was still delivered",
      any(m["subject"] == "From the game" for m in accounts.list_mail(c, "pomona@pol.com")), True)
row = c.execute("SELECT * FROM ext_mail_log ORDER BY id DESC LIMIT 1").fetchone()
check("logged: member, direction, address, ok, provider id",
      (row["member_id"], row["direction"], row["peer_addr"], row["ok"], row["detail"]),
      (eo, "out", "friend@example.com", 1, "prov-%d" % len(SENT)))

print("\nWhat the client writes cannot choose the outside From")
n0 = len(SENT)
check("a From header naming another address is refused at DATA (550)",
      smtp("EXTP1234@pol.com", ["friend@example.com"], name='Evil" <ceo@bank.com>')["data"], "550")
check("...and nothing was sent", len(SENT) - n0, 0)
crafted = b'From: "The Bank <ceo@bank.com>" <EXTP1234@pol.com>\r\nSubject: s\r\n\r\nx\r\n'
frm = extmail.outbound_parts(crafted, c.execute("SELECT * FROM member WHERE id=?", (eo,)).fetchone())[0]
check("an address hidden in the display name is defused",
      frm, '"The Bank ceobank.com" <pomona@example.net>')

print("\nFailures and limits")
PROVIDER["status"] = 422
check("provider refuses -> 554, never a false 250",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["data"], "554")
PROVIDER["status"] = 200
check("...and the failure is logged as not ok",
      c.execute("SELECT ok FROM ext_mail_log ORDER BY id DESC LIMIT 1").fetchone()[0], 0)
sent_ok = extmail.count_today(c, eo, "out")
check("a failed send does not count against the cap", sent_ok, 1)
os.environ["POL_EXT_MAIL_DAILY_OUT"] = str(sent_ok + 1)
check("one below the cap: accepted", smtp("EXTP1234@pol.com", ["x@example.com"])["rcpt"], ["250"])
check("at the daily cap: RCPT 550", smtp("EXTP1234@pol.com", ["x@example.com"])["rcpt"], ["550"])
os.environ["POL_EXT_MAIL_DAILY_OUT"] = "50"
many = ["p%d@example.com" % i for i in range(extmail.MAX_RCPTS + 1)]
check("more than MAX_RCPTS outside recipients: the extra one is 452",
      smtp("EXTP1234@pol.com", many)["rcpt"][-1], "452")
check("the report/support aliases are not 'outside'",
      all(not R._smtp_is_outside(a) for a in R._mail_aliases()), True)

print("\n<name>@example.net from inside the game is local")
n0 = len(SENT)
r = smtp("offie@pol.com", ["pomona@example.net"], subject="via the outside name")
check("accepted without the gate (offie is not enabled)", (r["rcpt"], r["data"]), (["250"], "250"))
check("...delivered to pomona's box, not the provider",
      (any(m["subject"] == "via the outside name" for m in accounts.list_mail(c, "pomona@pol.com")),
       len(SENT) - n0), (True, 0))

print("\nInbound (extmail.receive)")
RAW = (b"From: =?utf-8?q?Zo=C3=AB?= <zoe@example.com>\r\n"
       b"To: pomona@example.net\r\nSubject: =?utf-8?b?SGnigJQgdGhlcmU=?=\r\n"
       b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=XX\r\n\r\n"
       b"--XX\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html only</p>\r\n"
       b"--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"It\xe2\x80\x99s caf\xc3\xa9 time \xf0\x9f\x98\x80\r\n--XX--\r\n")
b64 = base64.b64encode(RAW).decode()
st, _ = regapi.inbound_mail("pomona@example.net", "zoe@example.com", b64)
check("to an enabled member's mail name: 200", st, 200)
got = accounts.list_mail(c, "pomona@pol.com")[-1]
raw = bytes(got["raw"])
check("stored as ISO-8859-1 text/plain", b"charset=ISO-8859-1" in raw, True)
check("the text/plain part won over the html one", (b"caf\xe9 time" in raw, b"html only" in raw), (True, False))
check("a curly quote became a plain one; an emoji a '?'", b"It's caf\xe9 time ?" in raw, True)
check("subject decoded, em dash made plain", got["subject"], "Hi- there")
check("the From kept its decoded name", got["sender"].startswith("Zo"), True)
check("addressed to the in-game box", b"To: pomona@pol.com" in raw, True)
check("by POL ID in lower case: 200",
      regapi.inbound_mail("extp1234@example.net", "zoe@example.com", b64)[0], 200)
check("to a member NOT enabled: 404 (the relay bounces it)",
      regapi.inbound_mail("offie@example.net", "zoe@example.com", b64)[0], 404)
check("to nobody: 404", regapi.inbound_mail("ghost@example.net", "z@e.com", b64)[0], 404)
check("another domain: 404", regapi.inbound_mail("pomona@pol.com", "z@e.com", b64)[0], 404)
big = base64.b64encode(b"Subject: x\r\n\r\n" + b"a" * (extmail.MAX_IN_RAW + 1)).decode()
check("too large: 413", regapi.inbound_mail("pomona@example.net", "z@e.com", big)[0], 413)
os.environ["POL_EXT_MAIL_DAILY_IN"] = "2"
check("past the inbound daily cap: 429",
      regapi.inbound_mail("pomona@example.net", "zoe@example.com", b64)[0], 429)
enable(eo, False)
os.environ["POL_EXT_MAIL_DAILY_IN"] = "100"
check("switched off in the panel: inbound 404 again",
      regapi.inbound_mail("pomona@example.net", "zoe@example.com", b64)[0], 404)
check("...and outbound 550 again", smtp("EXTP1234@pol.com", ["friend@example.com"])["rcpt"], ["550"])

c.close()
print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("extmail_test: all checks passed")
