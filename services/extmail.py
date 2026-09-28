"""PlayOnline Mail to and from the internet, through an outside mail domain.

The Viewer hardcodes `@pol.com` (it appends it to every address itself and
names po000/ma000.pol.com as its servers), so INSIDE the game every address
stays `<name>@pol.com`. This module is the boundary:

  OUT  a member mails an outside address -> the message leaves through a mail
       provider's HTTPS API (many hosts block port 25), From rewritten to
       `<mail name>@<POL_EXT_MAIL_DOMAIN>`.
  IN   whatever receives mail for `*@<POL_EXT_MAIL_DOMAIN>` (an email-routing
       service, a small relay) hands the raw message to `receive()`. The
       message is flattened to the plain ISO-8859-1 text the Viewer can show
       and delivered to the member's mailbox. Wiring that relay is up to the
       deployment; nothing here listens for it.

GATED PER MEMBER. `member.ext_mail` (admin panel, "Ext mail" button) must be
1 for either direction; everyone else gets a refusal they can see. Sign-up may
be open, so this is what stands between a spammer and the domain's reputation.
Daily caps on top (POL_EXT_MAIL_DAILY_OUT / _IN), every message logged in
`ext_mail_log`.

Off unless configured: with no POL_EXT_MAIL_KEY or no POL_EXT_MAIL_DOMAIN
nothing is ever sent out, and with no domain nothing is accepted in.

  POL_EXT_MAIL_PROVIDER   resend (default) | postmark
  POL_EXT_MAIL_KEY        the provider API key; empty = outbound OFF
  POL_EXT_MAIL_DOMAIN     the outside domain; empty = the whole feature OFF
  POL_EXT_MAIL_DAILY_OUT  outside messages a member may SEND per 24 h (20)
  POL_EXT_MAIL_DAILY_IN   outside messages a member may RECEIVE per 24 h (100)
"""
import datetime
import email
import email.header
import email.policy
import email.utils
import html.parser
import json
import os
import re
import urllib.error
import urllib.request

import base64

import accounts

#: A message a member sends out, and one that arrives, are both bounded far
#: below the SMTP limit: this is a chat-length mail system, not a file drop.
MAX_OUT_TEXT = 64 * 1024
MAX_IN_RAW = 256 * 1024
MAX_IN_TEXT = 32 * 1024
MAX_RCPTS = 5


def domain():
    """The outside domain, lower case, or "" when the feature is not set up."""
    return (os.environ.get("POL_EXT_MAIL_DOMAIN") or "").strip().lower()


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def outbound_configured():
    return bool(os.environ.get("POL_EXT_MAIL_KEY")) and bool(domain())


def enabled(row):
    """Is this member allowed outside mail at all (the admin-panel gate)?"""
    return row is not None and "ext_mail" in row.keys() and bool(row["ext_mail"])


def outside_address(row):
    """`<mail name>@<domain>`, or None for a member with no mail name (or
    when no outside domain is configured)."""
    local = str(row["mail_address"] or "").split("@", 1)[0]
    return f"{local}@{domain()}" if local and domain() else None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def count_today(db, member_id, direction):
    since = (_now() - datetime.timedelta(days=1)).isoformat()
    return db.execute(
        "SELECT COUNT(*) FROM ext_mail_log WHERE member_id = %s AND direction = %s "
        "AND ok = 1 AND at >= %s", (member_id, direction, since)).fetchone()[0]


def room_today(db, member_id, direction):
    cap = _int_env("POL_EXT_MAIL_DAILY_OUT" if direction == "out"
                   else "POL_EXT_MAIL_DAILY_IN", 20 if direction == "out" else 100)
    return max(0, cap - count_today(db, member_id, direction))


def note(db, member_id, direction, peer_addr, ok, detail=""):
    db.execute("INSERT INTO ext_mail_log (member_id, direction, peer_addr, at, ok, "
               "detail) VALUES (%s,%s,%s,%s,%s,%s)",
               (member_id, direction, str(peer_addr)[:320], _now().isoformat(),
                1 if ok else 0, str(detail)[:500]))
    db.commit()


# --------------------------------------------------------------------------- #
# Plain text in and out
# --------------------------------------------------------------------------- #
class _Text(html.parser.HTMLParser):
    """Just enough HTML-to-text for a mail with no text/plain part."""
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _html_text(markup):
    p = _Text()
    try:
        p.feed(markup)
    except Exception:                   # noqa: BLE001 -- best effort by design
        pass
    text = "".join(p.out)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _decode(s):
    """A header value, MIME-words decoded."""
    try:
        return str(email.header.make_header(email.header.decode_header(s or "")))
    except Exception:                   # noqa: BLE001
        return str(s or "")


def body_text(msg):
    """The message's readable text: the first text/plain part, else the first
    text/html part flattened. Never raises."""
    plain = html_part = None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "latin-1", "replace")
        except (LookupError, AttributeError):
            text = (part.get_payload(decode=True) or b"").decode("latin-1", "replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain is not None:
        return plain
    return _html_text(html_part) if html_part else ""


#: What the Viewer's mailer writes itself (X-Mailer SQUARE ENIX PlayOnline
#: Mailer) is `text/plain; charset=ISO-8859-1`, so that is
#: what inbound mail is flattened to. The commonest characters outside it are
#: typographic, and a plain stand-in reads better than a '?'.
_LATIN1_STANDINS = str.maketrans({chr(cp): sub for cp, sub in (
    (0x2018, "'"), (0x2019, "'"), (0x201A, "'"), (0x201C, '"'), (0x201D, '"'),
    (0x201E, '"'), (0x2013, "-"), (0x2014, "-"), (0x2026, "..."), (0x00A0, " "),
    (0x2022, "*"), (0x200B, ""), (0xFEFF, ""),
)})


def latin1(s):
    return s.translate(_LATIN1_STANDINS).encode("latin-1", "replace").decode("latin-1")


def _one_line(s, limit=200):
    return latin1(re.sub(r"[\r\n\t]+", " ", s or "")).strip()[:limit]


def inbound(raw, rcpt_local):
    """(bytes) the Viewer-safe RFC822 copy of an inbound message for the
    mailbox `rcpt_local@pol.com`, plus (sender, subject) for the mail table."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    sender = _one_line(_decode(msg.get("From")), 200) or "(unknown sender)"
    subject = _one_line(_decode(msg.get("Subject")), 200)
    text = latin1(body_text(msg)).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_IN_TEXT:
        text = text[:MAX_IN_TEXT] + "\n\n[message shortened]"
    date = msg.get("Date") or email.utils.formatdate(usegmt=True)
    head = [
        f"From: {sender}",
        f"To: {rcpt_local}@pol.com",
        f"Subject: {subject}",
        f"Date: {_one_line(date, 80)}",
        f"Message-Id: {email.utils.make_msgid(domain='pol.com')}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=ISO-8859-1",
        "Content-Transfer-Encoding: 8bit",
    ]
    body = "\r\n".join(head) + "\r\n\r\n" + text.replace("\n", "\r\n") + "\r\n"
    return body.encode("latin-1", "replace"), sender, subject


# --------------------------------------------------------------------------- #
# Out, through the provider
# --------------------------------------------------------------------------- #
def outbound_parts(body, row):
    """(from_header, subject, text) for a message the Viewer submitted.
    The display name the member set in their Viewer is kept; the ADDRESS is
    always this member's outside address -- never anything the client wrote."""
    msg = email.message_from_bytes(bytes(body), policy=email.policy.compat32)
    name, _ = email.utils.parseaddr(_decode(msg.get("From")))
    # No quotes, brackets or '@': a display name must not be able to read as
    # an address ("The Bank <ceo@bank.com>" <ours>).
    name = re.sub(r'["<>@\\\r\n]', "", name).strip()[:60]
    addr = outside_address(row)
    frm = f'"{name}" <{addr}>' if name else addr
    subject = re.sub(r"[\r\n]+", " ", _decode(msg.get("Subject")))[:200]
    text = body_text(msg)
    if len(text) > MAX_OUT_TEXT:
        text = text[:MAX_OUT_TEXT] + "\n\n[message shortened]"
    return frm, subject, text


def _post(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST", headers=dict(
                                     headers, **{"Content-Type": "application/json",
                                                 "Accept": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as exc:
        return 0, {"error": repr(exc)}


def send(frm, to, subject, text, reply_to):
    """(ok, detail). One provider call for all of `to`."""
    key = os.environ.get("POL_EXT_MAIL_KEY") or ""
    if not key:
        return False, "outbound not configured (POL_EXT_MAIL_KEY empty)"
    provider = (os.environ.get("POL_EXT_MAIL_PROVIDER") or "resend").lower()
    if provider == "postmark":
        st, j = _post("https://api.postmarkapp.com/email",
                      {"X-Postmark-Server-Token": key},
                      {"From": frm, "To": ",".join(to), "Subject": subject,
                       "TextBody": text, "ReplyTo": reply_to,
                       "MessageStream": "outbound"})
        ok = st == 200 and not j.get("ErrorCode")
        return ok, (j.get("MessageID") if ok else f"{st} {j.get('Message') or j}")
    st, j = _post("https://api.resend.com/emails",
                  {"Authorization": f"Bearer {key}"},
                  {"from": frm, "to": list(to), "subject": subject,
                   "text": text, "reply_to": reply_to})
    ok = st == 200 and bool(j.get("id"))
    return ok, (j.get("id") if ok else f"{st} {j.get('message') or j}")


def member_for_local(db, local):
    """The member a local part names: a mail name, else (any case) a POL ID."""
    local = (local or "").split("@", 1)[0].strip()
    if not local:
        return None
    row = accounts.member_by_mail(db, local)
    if row is None:
        row = accounts.member_by_polid(db, local.upper())
    return row


def receive(db, rcpt, sender, raw, log=print):
    """(status, payload) for one message from the internet for `rcpt`.

    The entry point for whatever relays inbound mail to this server. `raw` is
    the whole RFC 822 message as bytes (or base64 text). 404 = not our domain,
    no such mailbox, or a member not enabled for outside mail; 413 = too large;
    429 = the member's daily inbound cap. The caller owns `db`.
    """
    if not domain():
        return 503, {"error": "outside mail is not available"}
    if not isinstance(rcpt, str) or "@" not in rcpt:
        return 400, {"error": "Bad request."}
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return 400, {"error": "Bad request."}
    local, _, dom = rcpt.strip().rpartition("@")
    if dom.lower() != domain():
        return 404, {"error": "not our domain"}
    if len(raw) > MAX_IN_RAW:
        return 413, {"error": "message too large"}
    row = member_for_local(db, local)
    if not enabled(row) or not row["mail_address"]:
        log("[extmail] inbound mail for %r from %r refused: %s" % (
            rcpt[:80], str(sender)[:80],
            "no such mailbox" if row is None else "member not enabled"))
        return 404, {"error": "no such mailbox"}
    if room_today(db, row["id"], "in") <= 0:
        note(db, row["id"], "in", sender, False, "daily cap")
        return 429, {"error": "mailbox is not accepting more mail today"}
    box_local = row["mail_address"].split("@", 1)[0]
    msg, frm, subject = inbound(raw, box_local)
    accounts.deliver_mail(db, row["mail_address"], msg, sender=frm,
                          subject=subject)
    note(db, row["id"], "in", sender, True, subject)
    log("[extmail] inbound mail for %s from %r delivered, %d bytes" % (
        row["mail_address"], str(sender)[:80], len(msg)))
    return 200, {"ok": True}
