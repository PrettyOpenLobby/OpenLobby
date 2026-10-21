#!/usr/bin/env python3
"""Pin the screenshot box: pictures mailed to pics@pol.com (services/screenshots.py).

Drives the REAL SMTP handler over a socket with mails built the way the Viewer
builds them (multipart/mixed, base64 parts with `Content-Disposition:
attachment`). What it holds in place:

  * only a member may mail the box: anyone else is refused at RCPT (550);
  * PNG and JPEG are kept byte for byte, BMP is stored as PNG, a part that
    does not decode is refused by name, and mailing a picture twice keeps one;
  * the message is not filed in any mailbox; the sender gets a reply in their
    own box saying what was kept;
  * the box takes a mail larger than the general 4 MB limit, other mail does not;
  * the per-member cap holds, and the reply says the space is full;
  * listing, get, thumbnail, delete and zip see only the member's own pictures;
  * nobody can take `pics` as a mail name.

Run: python tools/screenshot_mail_test.py
"""
import base64
import io
import os
import socket
import sys
import tempfile
import threading
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
_TMP = tempfile.mkdtemp(prefix="shots-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgtest  # noqa: E402
DB = pgtest.use_fresh_database()
os.environ.update(POL_ACCOUNTS="1", POL_LOG_DIR=os.path.join(_TMP, "logs"))
for k in ("POL_SCREENSHOT_BOX", "POL_SCREENSHOT_MAX", "POL_SCREENSHOT_MAIL_KB",
          "POL_MAIL_MAX_KB", "POL_MAIL_SENDER_CHECK", "POL_MAIL_SESSION_CHECK",
          "POL_MAIL_STRICT", "POL_EXT_MAIL_DOMAIN", "POL_EXT_MAIL_RELAY"):
    os.environ.pop(k, None)

from PIL import Image  # noqa: E402

import accounts  # noqa: E402
import responders as R  # noqa: E402
import screenshots  # noqa: E402

FAILED = []


def check(label, got, want=True):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label
          + ("" if ok else f": {got!r}  (want {want!r})"))
    if not ok:
        FAILED.append(label)


def image(fmt, size=(64, 48), color=(200, 40, 40), noise=False):
    img = Image.new("RGB", size, color)
    if noise:
        img.putdata([(i * 7 % 256, i * 13 % 256, i * 31 % 256)
                     for i in range(size[0] * size[1])])
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def viewer_mail(sender, parts, to="pics@pol.com", subject="shots"):
    """A mail laid out like the PC Viewer's (captured 2026-09-29)."""
    bnd = "--=_NextPart_4607671_9215342"
    out = [f'From: "Pat" <{sender}>', f"To: {to}", f"Subject: {subject}",
           "X-Mailer: SQUARE ENIX PlayOnline Mailer version 1.0000.000.3.1.18.15e",
           "MIME-Version: 1.0", "Content-Type: multipart/mixed;",
           f' boundary="{bnd}"', "", f"--{bnd}",
           "Content-Type: text/plain; charset=ISO-8859-1",
           "Content-Transfer-Encoding: 8bit", "", "", ""]
    for name, ctype, data in parts:
        b64 = base64.b64encode(data).decode()
        out += [f"--{bnd}", f"Content-Type: {ctype}",
                "Content-Disposition: attachment;", f'\tfilename="{name}"',
                "Content-Transfer-Encoding: base64", ""]
        out += [b64[i:i + 60] for i in range(0, len(b64), 60)]
        out.append("")
    out += [f"--{bnd}--", ""]
    return "\r\n".join(out).encode()


def smtp(sender, rcpts, raw=None):
    """One SMTP session against handle_smtp. Returns the reply codes."""
    a, b = socket.socketpair()
    t = threading.Thread(target=R.handle_smtp, args=(b, ("127.0.0.1", 5555), 25))
    t.start()
    f = a.makefile("rwb")
    out = {"banner": f.readline()[:3].decode()}

    def say(line):
        f.write(line.encode() + b"\r\n")
        f.flush()
        return f.readline()[:3].decode()
    say("EHLO test")
    f.readline()
    out["from"] = say(f"MAIL FROM: <{sender}>")
    out["rcpt"] = [say(f"RCPT TO: {r}") for r in rcpts]  # the Viewer's form
    if "250" in out["rcpt"]:
        say("DATA")
        msg = raw if raw is not None else (
            f"From: <{sender}>\r\nTo: x\r\nSubject: hi\r\n\r\nhello\r\n").encode()
        msg = b"\r\n".join(b"." + ln if ln.startswith(b".") else ln
                           for ln in msg.split(b"\r\n"))
        f.write(msg + b"\r\n.\r\n")
        f.flush()
        out["data"] = f.readline()[:3].decode()
    say("QUIT")
    t.join(10)
    a.close()
    return out


c = accounts.connect(DB)
accounts.create_polid(c, "SHOT1234", "polid-pw-1")
me = accounts.add_member(c, "SHOT1234", "shotmember", "pw-shot-0001")
accounts.assign_mail_address(c, me, "snapper")
accounts.open_session(c, me, nick="Snap", peer_ip="127.0.0.1")
accounts.create_polid(c, "OTHR5678", "polid-pw-2")
other = accounts.add_member(c, "OTHR5678", "othermember", "pw-oth-0001")
accounts.assign_mail_address(c, other, "otherone")
accounts.open_session(c, other, nick="Oth", peer_ip="127.0.0.1")
c.commit()
ME = "snapper@pol.com"


def inbox(addr):
    return accounts.list_mail(c, addr)


def last_reply(addr):
    rows = inbox(addr)
    return bytes(rows[-1]["raw"]).decode("latin-1") if rows else ""


print("\nWho may mail the box")
check("a non-member sender is refused at MAIL FROM (550), so DATA never runs",
      smtp("stranger@pol.com", ["pics@pol.com"])["from"], "550")
os.environ["POL_MAIL_SENDER_CHECK"] = "0"
check("with the sender check off, a non-member is still refused at RCPT",
      smtp("stranger@pol.com", ["pics@pol.com"])["rcpt"], ["550"])
os.environ.pop("POL_MAIL_SENDER_CHECK")
check("the address matches in any case and bare",
      (screenshots.is_address("PICS@pol.com"), screenshots.is_address("pics"),
       screenshots.is_address("pics@example.com"),
       screenshots.is_address("picsx@pol.com")), (True, True, False, False))

print("\nA mail with a PNG, a JPEG, a BMP and a broken part")
png = image("PNG", noise=True)
jpg = image("JPEG", (320, 240), (10, 120, 200))
bmp = image("BMP", (100, 80), (20, 200, 20))
cut = image("JPEG", (320, 240), noise=True)
cut = cut[:len(cut) // 2]        # the header reads, the picture does not
r = smtp(ME, ["pics@pol.com"], viewer_mail(ME, [
    ("tm002.png", "image/png", png),
    ("shot1.jpg", "image/jpeg", jpg),
    ("ffxi.bmp", "image/bmp", bmp),
    ("broken.jpg", "image/jpeg", cut),
    ("notes.txt", "application/octet-stream", b"not a picture")]))
check("RCPT 250, DATA 250", (r["rcpt"], r.get("data")), (["250"], "250"))
rows = screenshots.listing(me)
check("three pictures kept", len(rows), 3)
check("kinds: png, jpg, and the BMP as png",
      sorted(x["ext"] for x in rows), ["jpg", "png", "png"])
kept = {screenshots.get(me, x["id"])[1] for x in rows}
check("PNG kept byte for byte", png in kept)
check("JPEG kept byte for byte", jpg in kept)
check("BMP stored as a PNG that decodes to the same size",
      any(b.startswith(b"\x89PNG") and Image.open(io.BytesIO(b)).size == (100, 80)
          for b in kept if b != png))
thumb = screenshots.get(me, rows[0]["id"], thumb=True)
check("a thumbnail is a JPEG no bigger than 480x360",
      (thumb[0], Image.open(io.BytesIO(thumb[1])).size[0] <= 480), ("image/jpeg", True))
check("nothing filed in a `pics` mailbox", len(inbox("pics@pol.com")), 0)
reply = last_reply(ME)
check("the sender got a reply", "Subject: Screenshots saved" in reply)
check("the reply counts what was kept", "Saved 3 screenshots." in reply)
check("the reply names the broken part", "broken.jpg: damaged or incomplete" in reply)
check("the reply ignores the text attachment (it never claimed to be a picture)",
      "notes.txt" not in reply)
check("the reply comes from the box", f"From: Screenshots <pics@pol.com>" in reply)

print("\nThe same picture again, and a mail with no pictures")
before = len(inbox(ME))
smtp(ME, ["pics@pol.com"], viewer_mail(ME, [("again.png", "image/png", png)]))
check("still three pictures", len(screenshots.listing(me)), 3)
check("the reply says it was already saved", "1 was already saved." in last_reply(ME))
smtp(ME, ["pics@pol.com"])
check("a mail with no pictures gets a 'nothing saved' reply",
      "No screenshots saved" in last_reply(ME) and len(inbox(ME)) == before + 2)

print("\nSize: the box takes more than the general limit, other mail does not")
big = image("BMP", (1700, 1000), noise=True)            # ~5 MB, about like a PC screen
raw_big = viewer_mail(ME, [("big.bmp", "image/bmp", big)])
check("the test mail is over 4 MB", len(raw_big) > 4 * 1024 * 1024)
check("to pics@pol.com: 250", smtp(ME, ["pics@pol.com"], raw_big).get("data"), "250")
check("...and kept", len(screenshots.listing(me)), 4)
check("the same mail to a member: 552",
      smtp(ME, ["otherone@pol.com"], raw_big).get("data"), "552")
os.environ["POL_SCREENSHOT_MAIL_KB"] = "1024"
check("POL_SCREENSHOT_MAIL_KB lowers it (552)",
      smtp(ME, ["pics@pol.com"], raw_big).get("data"), "552")
os.environ.pop("POL_SCREENSHOT_MAIL_KB")

print("\nThe cap")
os.environ["POL_SCREENSHOT_MAX"] = "5"
smtp(ME, ["pics@pol.com"], viewer_mail(ME, [
    ("a.png", "image/png", image("PNG", color=(1, 2, 3))),
    ("b.png", "image/png", image("PNG", color=(4, 5, 6)))]))
check("one more fits, the next does not", len(screenshots.listing(me)), 5)
check("the reply says the space is full", "your screenshot space is full" in last_reply(ME))
os.environ.pop("POL_SCREENSHOT_MAX")

print("\nWhose pictures")
check("the other member has none", screenshots.listing(other), [])
sid = screenshots.listing(me)[0]["id"]
check("the other member cannot read mine", screenshots.get(other, sid), None)
check("the other member cannot delete mine", screenshots.delete(other, sid), False)
check("a malformed id reads nothing", screenshots.get(me, "../../x"), None)

print("\nZip and delete")
z = zipfile.ZipFile(io.BytesIO(screenshots.zip_all(me)))
names = z.namelist()
check("the zip holds every picture", len(names), 5)
check("named screenshot-<date>-<id>.<ext>",
      all(n.startswith("screenshot-") and n[-4:] in (".png", ".jpg") for n in names))
check("delete removes it", screenshots.delete(me, sid), True)
check("...and its thumbnail", screenshots.get(me, sid, thumb=True), None)
check("four left", len(screenshots.listing(me)), 4)
check("no pictures, empty zip", screenshots.zip_all(other), b"")

print("\nThe name is reserved")
check("`pics` cannot be a mail name", accounts.check_mail_local("pics"),
      "That mail name is reserved.")
check("`pics1` still can", accounts.check_mail_local("pics1"), None)

c.close()
print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("screenshot_mail_test: all checks passed")
