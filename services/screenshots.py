"""Screenshots mailed from the Viewer, kept for the member's website account.

A member mails pictures to `pics@pol.com` from the Viewer's own mail app. The
PC build sends ordinary MIME (below); the PS2 Viewer's memory holds the same
MIME writer (multipart/mixed, `Content-Disposition: attachment;`) beside a
`ScreenShots` folder and `%s.jpg` names, so PS2 pictures are expected to arrive
as JPEG parts. Not yet seen from a real PS2. The mail server
hands the message here instead of filing it in a mailbox; each picture is
decoded, stored under the member, and the member gets a short reply in their
PlayOnline inbox saying what was kept. A website can list them for download
(not part of this repository).

What the Viewer sends, measured from a real PC mail (a Tetra Master screen):
multipart/mixed, a text/plain part, then

    Content-Type: image/png
    Content-Disposition: attachment;
    \tfilename="tm002.png"
    Content-Transfer-Encoding: base64

Storage is the `blob` table (polcore/blobs.py) in the member's own scope, so
deleting the member deletes their pictures and there is nothing new to back up:

    <member id> / shots/<id>.<ext>        the picture
    <member id> / shots-thumb/<id>.jpg    a small JPEG for the listing

`<id>` is the first 16 hex digits of the stored picture's SHA-256, so mailing
the same picture twice keeps one copy. A blob's updated_at is the time it
arrived.

Every picture is fully decoded before it is kept. JPEG and PNG are kept byte for
byte (re-encoding would only lose quality); anything else Pillow can read (the
PC client's BMP screenshots, GIF, TIFF) is stored as PNG, so the site only ever
serves JPEG and PNG.

  POL_SCREENSHOT_BOX       the mailbox name, default "pics" (reserved: no member
                           may take it as a mail name)
  POL_SCREENSHOT_MAX       pictures one member may keep, default 300
  POL_SCREENSHOT_MAIL_KB   largest mail the box accepts, default 24576 (the
                           server's general limit, POL_MAIL_MAX_KB, is 4 MB, and
                           three PC BMP screens are over that)
  POL_SCREENSHOT_URL       where the reply mail sends people
"""
import email
import email.policy
import email.utils
import hashlib
import io
import os
import re
import time
import zipfile

try:
    from PIL import Image
except ImportError:                     # pragma: no cover - the images carry it
    Image = None

from polcore import blobs

PREFIX = "shots/"
THUMB_PREFIX = "shots-thumb/"
THUMB_BOX = (480, 360)
#: A decoded picture larger than this is refused before Pillow allocates it. A
#: 1080p screen is 2 Mpx; this leaves room for anything a game draws.
MAX_PIXELS = 16 * 1024 * 1024
MAX_PICTURES_PER_MAIL = 20
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_KEEP_AS_IS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png")}
_MIME = {"jpg": "image/jpeg", "png": "image/png"}
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff")


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def box():
    return (os.environ.get("POL_SCREENSHOT_BOX") or "pics").strip().lower()


def max_pictures():
    return _int_env("POL_SCREENSHOT_MAX", 300)


def mail_max_bytes():
    return _int_env("POL_SCREENSHOT_MAIL_KB", 24576) * 1024


def site_url():
    return os.environ.get("POL_SCREENSHOT_URL") or ""


def is_address(addr, mail_domain="pol.com"):
    """True for `pics@pol.com` (or a bare `pics`), in any case."""
    local, at, dom = (addr or "").strip().lower().rpartition("@")
    if not at:
        local, dom = dom, mail_domain.lower()
    return bool(local) and local == box() and dom == mail_domain.lower()


def attachments(raw):
    """[(file name, bytes)] for every part of a mail that claims to be a
    picture: an image/* type, or an attachment whose name says it is one."""
    msg = email.message_from_bytes(bytes(raw), policy=email.policy.compat32)
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        ctype = part.get_content_type()
        if not (ctype.startswith("image/")
                or name.lower().endswith(_IMAGE_EXT)):
            continue
        data = part.get_payload(decode=True)
        if data:
            out.append((name, data))
    return out


def picture(data):
    """(ext, stored bytes, thumbnail JPEG) for one picture, or ValueError with
    a reason a player can read."""
    if Image is None:
        raise ValueError("pictures cannot be read on this server")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            w, h = probe.size
    except Exception:
        raise ValueError("not a picture this server can read")
    if w * h > MAX_PIXELS:
        raise ValueError("too large (%dx%d)" % (w, h))
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()                       # the whole picture, not the header
            if fmt in _KEEP_AS_IS:
                ext, stored = _KEEP_AS_IS[fmt][0], bytes(data)
            else:
                keep = img if img.mode in ("RGB", "RGBA", "L", "LA", "P") \
                    else img.convert("RGBA" if "A" in img.getbands() else "RGB")
                buf = io.BytesIO()
                keep.save(buf, "PNG", optimize=True)
                ext, stored = "png", buf.getvalue()
            small = img.convert("RGB")
            small.thumbnail(THUMB_BOX)
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=82)
            return ext, stored, buf.getvalue()
    except ValueError:
        raise
    except Exception:
        raise ValueError("damaged or incomplete")


def _id_of(stored):
    return hashlib.sha256(stored).hexdigest()[:16]


def listing(member_id):
    """The member's pictures, newest first:
    [{"id", "ext", "size", "at"}] with `at` in epoch seconds."""
    out = []
    for info in blobs.listing(scope=str(member_id), prefix=PREFIX):
        name = info.path[len(PREFIX):]
        sid, dot, ext = name.partition(".")
        if dot and _ID_RE.match(sid) and ext in _MIME:
            out.append({"id": sid, "ext": ext, "size": info.size,
                        "at": int(info.updated_at)})
    out.sort(key=lambda r: (-r["at"], r["id"]))
    return out


def _find(member_id, shot_id):
    if not _ID_RE.match(shot_id or ""):
        return None
    for ext in _MIME:
        if blobs.exists(str(member_id), "%s%s.%s" % (PREFIX, shot_id, ext)):
            return ext
    return None


def get(member_id, shot_id, thumb=False):
    """(content type, bytes, file name) for one picture or its thumbnail, or
    None when the member has no such picture."""
    ext = _find(member_id, shot_id)
    if ext is None:
        return None
    if thumb:
        data = blobs.get(str(member_id), "%s%s.jpg" % (THUMB_PREFIX, shot_id))
        return None if data is None else ("image/jpeg", data, shot_id + ".jpg")
    path = "%s%s.%s" % (PREFIX, shot_id, ext)
    data = blobs.get(str(member_id), path)
    info = blobs.stat(str(member_id), path)
    if data is None or info is None:
        return None
    return _MIME[ext], data, download_name(shot_id, ext, info.updated_at)


def download_name(shot_id, ext, at):
    return "screenshot-%s-%s.%s" % (
        time.strftime("%Y%m%d-%H%M%S", time.gmtime(at)), shot_id[:6], ext)


def delete(member_id, shot_id):
    """Remove one picture and its thumbnail. True when there was one."""
    if not _ID_RE.match(shot_id or ""):
        return False
    gone = blobs.delete_prefix(str(member_id), "%s%s." % (PREFIX, shot_id))
    blobs.delete_prefix(str(member_id), "%s%s." % (THUMB_PREFIX, shot_id))
    return bool(gone)


def zip_all(member_id):
    """Every picture of a member as one zip (stored, not compressed: JPEG and
    PNG are already compressed). b"" when there are none."""
    rows = listing(member_id)
    if not rows:
        return b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for r in rows:
            data = blobs.get(str(member_id), "%s%s.%s" % (PREFIX, r["id"], r["ext"]))
            if data is not None:
                name = download_name(r["id"], r["ext"], r["at"])
                z.writestr(zipfile.ZipInfo(name, time.gmtime(r["at"])[:6]), data)
    return buf.getvalue()


def ingest(member_id, raw):
    """Keep every picture in a mail. Returns
    {"saved": [ids], "again": n, "refused": [(name, why)], "full": bool}."""
    out = {"saved": [], "again": 0, "refused": [], "full": False}
    have = {r["id"] for r in listing(member_id)}
    scope = str(member_id)
    for n, (name, data) in enumerate(attachments(raw)):
        label = name or "picture %d" % (n + 1)
        if n >= MAX_PICTURES_PER_MAIL:
            out["refused"].append((label, "more than %d in one mail"
                                   % MAX_PICTURES_PER_MAIL))
            continue
        try:
            ext, stored, thumb = picture(data)
        except ValueError as exc:
            out["refused"].append((label, str(exc)))
            continue
        sid = _id_of(stored)
        if sid not in have and len(have) >= max_pictures():
            out["full"] = True
            out["refused"].append((label, "your screenshot space is full"))
            continue
        blobs.put(scope, "%s%s.jpg" % (THUMB_PREFIX, sid), thumb)
        blobs.put(scope, "%s%s.%s" % (PREFIX, sid, ext), stored)
        if sid in have:
            out["again"] += 1
        else:
            have.add(sid)
            out["saved"].append(sid)
    return out


def _latin1(s):
    return s.encode("latin-1", "replace").decode("latin-1")


def reply(to_addr, result, kept, mail_domain="pol.com"):
    """The confirmation mail, as the Viewer can show it (plain ISO-8859-1)."""
    saved, again, refused = len(result["saved"]), result["again"], result["refused"]
    lines = []
    if saved:
        lines.append("Saved %d screenshot%s." % (saved, "" if saved == 1 else "s"))
    if again:
        lines.append("%d %s already saved." % (again, "was" if again == 1 else "were"))
    if not saved and not again:
        lines.append("No screenshots were saved." if refused else
                     "This mail had no pictures attached, so nothing was saved.")
    for name, why in refused[:10]:
        lines.append("  %s: %s" % (name[:60], why))
    lines += ["", "You have %d of %d screenshots stored." % (kept, max_pictures())]
    if site_url():
        lines += ["Download them at " + site_url(), "(log in, then open Screenshots)."]
    subject = ("Screenshots saved" if saved or again else "No screenshots saved")
    now = email.utils.formatdate(usegmt=True)
    head = ("From: Screenshots <%s@%s>\r\nTo: %s\r\nSubject: %s\r\nDate: %s\r\n"
            "Message-Id: %s\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=ISO-8859-1\r\n"
            "Content-Transfer-Encoding: 8bit\r\n\r\n"
            % (box(), mail_domain, to_addr, subject, now,
               email.utils.make_msgid(domain=mail_domain)))
    body = "\r\n".join(_latin1(x) for x in lines) + "\r\n"
    return (head + body).encode("latin-1"), subject
