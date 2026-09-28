#!/usr/bin/env python3
"""Pin mail to and from the internet (services/extmail.py).

Drives the REAL SMTP handler over a socket; the outbound SMTP relay is
replaced by a fake that records what it was handed. The outside domain is the
reserved example.net. What it holds in place:

  * an outside recipient is REFUSED at RCPT (visibly, 550) unless the sender
    is enabled in the admin panel AND the relay and domain are configured,
    rather than accepted and silently dropped;
  * an enabled sender's mail goes to the relay as `"Name" <mailname@example.net>`
    with that address as the envelope sender, whatever the client wrote; the
    daily cap holds; a relay failure is a 554, not a false success;
  * mail from OUTSIDE (another domain, or a bounce) reaches only enabled
    members at example.net, flattened to Viewer-safe ISO-8859-1 text; it can
    never be relayed on, never name a @pol.com box, never pose as our domains;
  * a refused MAIL FROM ends the transaction: RCPT after it is 503;
  * extmail.receive, for a relay that cannot speak SMTP, keeps its gates.

Run: python tools/extmail_test.py
"""
import base64
import email.utils
import os
import smtplib
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
_TMP = tempfile.mkdtemp(prefix="extmail-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ.update(POL_ACCOUNTS="1",
                  POL_LOG_DIR=os.path.join(_TMP, "logs"))
for k in ("POL_EXT_MAIL_RELAY", "POL_EXT_MAIL_DAILY_OUT", "POL_EXT_MAIL_DAILY_IN",
          "POL_EXT_MAIL_DOMAIN", "POL_EXT_MAIL_IN_PER_IP_HOUR", "POL_MAIL_STRICT",
          "POL_MAIL_SESSION_CHECK", "POL_MAIL_SENDER_CHECK"):
    os.environ.pop(k, None)

import accounts  # noqa: E402
import extmail  # noqa: E402
import responders as R  # noqa: E402

DOMAIN = "example.net"
os.environ["POL_EXT_MAIL_DOMAIN"] = DOMAIN

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
accounts.assign_mail_address(c, eo, "ruuko")
accounts.open_session(c, eo, nick="Eo", peer_ip="127.0.0.1")
accounts.create_polid(c, "OFFP5678", "polid-pw-2")
off = accounts.add_member(c, "OFFP5678", "offmember", "pw-off-0001")
accounts.assign_mail_address(c, off, "offie")
accounts.open_session(c, off, nick="Off", peer_ip="127.0.0.1")
c.commit()

SENT = []
RELAY = {"fail": None}


class FakeRelay:
    """Stands in for the relay named in POL_EXT_MAIL_RELAY."""
    def __init__(self, host, port, timeout=None):
        self.where = (host, port)
        if RELAY["fail"] == "connect":
            raise ConnectionRefusedError(111, "Connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self, name=""):
        return 250, b"ok"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if RELAY["fail"] == "refuse":
            raise smtplib.SMTPRecipientsRefused({a: (550, b"no") for a in to_addrs})
        SENT.append((self.where, msg, from_addr, list(to_addrs)))
        return {}


extmail.smtplib.SMTP = FakeRelay


def smtp(sender, rcpts, subject="hi", body="Hello there.", name="Pat Sample",
         ip="127.0.0.1", raw=None):
    """One SMTP session against handle_smtp. Returns the reply codes."""
    a, b = socket.socketpair()
    t = threading.Thread(target=R.handle_smtp, args=(b, (ip, 5555), 25))
    t.start()
    f = a.makefile("rwb")
    out = {"banner": f.readline()[:3].decode()}

    def say(line):
        f.write(line.encode() + b"\r\n")
        f.flush()
        return f.readline()[:3].decode()
    say("EHLO test")
    f.readline()                                     # the second EHLO line
    out["from"] = say(f"MAIL FROM: <{sender}>")
    out["rcpt"] = [say(f"RCPT TO: <{r}>") for r in rcpts]
    if "250" in out["rcpt"]:
        say("DATA")
        msg = raw if raw is not None else (
            f'From: "{name}" <{sender}>\r\nTo: {", ".join(rcpts)}\r\n'
            f"Subject: {subject}\r\nContent-Type: text/plain; charset=ISO-8859-1\r\n"
            f"\r\n{body}\r\n").encode()
        f.write(msg + b".\r\n")
        f.flush()
        out["data"] = f.readline()[:3].decode()
    say("QUIT")
    t.join(5)
    a.close()
    return out


def enable(mid, on=True):
    c.execute("UPDATE member SET ext_mail = %s WHERE id = %s", (1 if on else 0, mid))
    c.commit()


def inbox(addr):
    return accounts.list_mail(c, addr)


print("\nThe gate: nobody mails outside unless enabled AND the relay is set")
check("not enabled: the outside RCPT is REFUSED (550), not accepted and dropped",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["rcpt"], ["550"])
enable(eo)
check("enabled but no POL_EXT_MAIL_RELAY: still 550 (the kill switch)",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["rcpt"], ["550"])
check("...and nothing reached the relay", len(SENT), 0)
os.environ["POL_EXT_MAIL_RELAY"] = "192.0.2.25:2525"
os.environ["POL_EXT_MAIL_DOMAIN"] = ""
check("relay but no POL_EXT_MAIL_DOMAIN: still 550 (inert without its domain)",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["rcpt"], ["550"])
os.environ["POL_EXT_MAIL_DOMAIN"] = DOMAIN
check("another member, never enabled: 550",
      smtp("offie@pol.com", ["friend@example.com"])["rcpt"], ["550"])
check("internal mail is untouched by the gate (offie -> ruuko@pol.com)",
      smtp("offie@pol.com", ["ruuko@pol.com"])["data"], "250")

print("\nAn enabled member, relay configured")
r = smtp("EXTP1234@pol.com", ["friend@example.com", "ruuko@pol.com"],
         subject="From the game", body="Can you read this?")
check("RCPT outside accepted, local accepted", r["rcpt"], ["250", "250"])
check("DATA 250", r["data"], "250")
where, msg, env_from, env_to = SENT[-1]
check("handed to the relay named in POL_EXT_MAIL_RELAY", where, ("192.0.2.25", 2525))
check("From is the member's OUTSIDE address with their display name",
      email.utils.parseaddr(str(msg["From"])), ("Pat Sample", "ruuko@example.net"))
check("envelope sender is the outside address (bounces come home)", env_from,
      "ruuko@example.net")
check("only the outside recipient went to the relay", env_to, ["friend@example.com"])
check("Reply-To and Message-ID are ours",
      (msg["Reply-To"], msg["Message-ID"].endswith("@example.net>")),
      ("ruuko@example.net", True))
check("subject and text carried", (msg["Subject"], msg.get_content().strip()),
      ("From the game", "Can you read this?"))
check("the local copy was still delivered",
      any(m["subject"] == "From the game" for m in inbox("ruuko@pol.com")), True)
row = c.execute("SELECT * FROM ext_mail_log ORDER BY id DESC LIMIT 1").fetchone()
check("logged: member, direction, address, ok",
      (row["member_id"], row["direction"], row["peer_addr"], row["ok"]),
      (eo, "out", "friend@example.com", 1))

print("\nWhat the client writes cannot choose the outside From")
n0 = len(SENT)
check("a From header naming another address is refused at DATA (550)",
      smtp("EXTP1234@pol.com", ["friend@example.com"], name='Evil" <ceo@bank.com>')["data"], "550")
check("...and nothing was sent", len(SENT) - n0, 0)
crafted = b'From: "The Bank <ceo@bank.com>" <EXTP1234@pol.com>\r\nSubject: s\r\n\r\nx\r\n'
frm = extmail.outbound_parts(crafted, c.execute("SELECT * FROM member WHERE id=%s", (eo,)).fetchone())[0]
check("an address hidden in the display name is defused",
      frm, '"The Bank ceobank.com" <ruuko@example.net>')

print("\nFailures and limits")
RELAY["fail"] = "connect"
check("relay unreachable -> 554, never a false 250",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["data"], "554")
RELAY["fail"] = "refuse"
check("relay refuses the recipients -> 554",
      smtp("EXTP1234@pol.com", ["friend@example.com"])["data"], "554")
RELAY["fail"] = None
check("...and failures are logged as not ok",
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
r = smtp("offie@pol.com", ["ruuko@example.net"], subject="via the outside name")
check("accepted without the gate (offie is not enabled)", (r["rcpt"], r["data"]), (["250"], "250"))
check("...delivered to ruuko's box, not the relay",
      (any(m["subject"] == "via the outside name" for m in inbox("ruuko@pol.com")),
       len(SENT) - n0), (True, 0))

print("\nMail from OUTSIDE (the outside domain's MX delivers to this handler)")
OUT_IP = "203.0.113.7"
RAW = (b"From: =?utf-8?q?Zo=C3=AB?= <zoe@example.com>\r\n"
       b"To: ruuko@example.net\r\nSubject: =?utf-8?b?SGnigJQgdGhlcmU=?=\r\n"
       b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=XX\r\n\r\n"
       b"--XX\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>html only</p>\r\n"
       b"--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"It\xe2\x80\x99s caf\xc3\xa9 time \xf0\x9f\x98\x80\r\n--XX--\r\n")
r = smtp("zoe@example.com", ["ruuko@example.net"], raw=RAW, ip=OUT_IP)
check("an outside sender to an enabled member: 250 all the way",
      (r["from"], r["rcpt"], r["data"]), ("250", ["250"], "250"))
got = inbox("ruuko@pol.com")[-1]
raw = bytes(got["raw"])
check("stored as ISO-8859-1 text/plain", b"charset=ISO-8859-1" in raw, True)
check("the text/plain part won over the html one", (b"caf\xe9 time" in raw, b"html only" in raw), (True, False))
check("a curly quote became a plain one; an emoji a '?'", b"It's caf\xe9 time ?" in raw, True)
check("subject decoded, em dash made plain", got["subject"], "Hi- there")
check("addressed to the in-game box", b"To: ruuko@pol.com" in raw, True)
check("logged as inbound", c.execute(
    "SELECT direction, peer_addr, ok FROM ext_mail_log ORDER BY id DESC LIMIT 1").fetchone()[:],
    ("in", "zoe@example.com", 1))
check("by POL ID in lower case: accepted",
      smtp("zoe@example.com", ["extp1234@example.net"], ip=OUT_IP)["rcpt"], ["250"])
check("a bounce (null sender) to an enabled member: accepted",
      smtp("", ["ruuko@example.net"], ip=OUT_IP)["rcpt"], ["250"])
check("to a member NOT enabled: 550",
      smtp("zoe@example.com", ["offie@example.net"], ip=OUT_IP)["rcpt"], ["550"])
check("to nobody: 550", smtp("zoe@example.com", ["ghost@example.net"], ip=OUT_IP)["rcpt"], ["550"])

print("\n...and what it can never do")
n0 = len(SENT)
check("relay on to another outside domain (open relay): 550",
      smtp("zoe@example.com", ["victim@example.org"], ip=OUT_IP)["rcpt"], ["550"])
check("...nothing reached the relay", len(SENT) - n0, 0)
check("name a @pol.com box directly: 550",
      smtp("zoe@example.com", ["ruuko@pol.com"], ip=OUT_IP)["rcpt"], ["550"])
check("a bounce to a @pol.com box: 550",
      smtp("", ["offie@pol.com"], ip=OUT_IP)["rcpt"], ["550"])
r = smtp("ruuko@example.net", ["ruuko@example.net"], ip=OUT_IP)
check("pose as our outside domain from the internet: MAIL FROM refused, RCPT 503",
      (r["from"], r["rcpt"]), ("550", ["503"]))
r = smtp("ruuko@pol.com", ["offie@pol.com"], ip=OUT_IP)
check("pose as a member from the internet: MAIL FROM refused, RCPT 503",
      (r["from"], r["rcpt"]), ("550", ["503"]))
os.environ["POL_EXT_MAIL_DAILY_IN"] = str(extmail.count_today(c, eo, "in"))
check("the member's inbound daily cap: 452",
      smtp("zoe@example.com", ["ruuko@example.net"], ip=OUT_IP)["rcpt"], ["452"])
os.environ["POL_EXT_MAIL_DAILY_IN"] = "100"
R._INBOUND_PER_IP_HOUR = 3
R._INBOUND_SEEN.clear()
codes = [smtp("zoe@example.com", ["ruuko@example.net"], ip="198.51.100.9")["from"]
         for _ in range(4)]
check("the per-address hourly limit: 4th MAIL FROM is 450", codes, ["250", "250", "250", "450"])
check("...another address is not affected",
      smtp("zoe@example.com", ["ruuko@example.net"], ip="198.51.100.10")["from"], "250")
big = b"Subject: big\r\n\r\n" + b"a" * (extmail.MAX_IN_RAW + 10) + b"\r\n"
check("too large: 552", smtp("zoe@example.com", ["ruuko@example.net"], raw=big,
                             ip="198.51.100.11")["data"], "552")
enable(eo, False)
check("switched off in the panel: inbound 550 again",
      smtp("zoe@example.com", ["ruuko@example.net"], ip="198.51.100.12")["rcpt"], ["550"])
check("...and outbound 550 again", smtp("EXTP1234@pol.com", ["friend@example.com"])["rcpt"], ["550"])

print("\nInbound through extmail.receive (a relay that calls in)")
enable(eo)
b64 = base64.b64encode(RAW).decode()


def receive(rcpt, raw_b64):
    db = accounts.connect(DB)
    try:
        return extmail.receive(db, rcpt, "zoe@example.com", raw_b64)[0]
    finally:
        db.close()


check("to an enabled member's mail name: 200", receive("ruuko@example.net", b64), 200)
check("to a member NOT enabled: 404", receive("offie@example.net", b64), 404)
check("another domain: 404", receive("ruuko@pol.com", b64), 404)
check("too large: 413", receive("ruuko@example.net", base64.b64encode(
    b"Subject: x\r\n\r\n" + b"a" * (extmail.MAX_IN_RAW + 1)).decode()), 413)

c.close()
print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("extmail_test: all checks passed")
