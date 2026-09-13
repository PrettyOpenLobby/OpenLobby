"""GM chat -- the record language spoken inside a GM Call chat room.

WHAT THIS IS. The GM Call UDP band (`pol-shim/src/gmserver.cpp`) carries the
call, the queue and the handshake, and then hands the client a ROOM: app.dll
`cft_1229(slot, name, key)` -> polcore `cft_0144`, which refuses a name whose
first byte is not `'#'` and issues IRC command 7 (JOIN) with the key. So GM chat
rides **the auth band (authserv, 5124x)** on the same carrier as group chat --
NOT the lobby, and not any GM message type. Verified live 2026-08-16: with
`gmserver --chat-room "#gmchat001"` the client joins and starts talking, and
authserv logs `PRIVMSG #gmchat001 :HRU87960930222113Foxdummy`.

WARNING: **The record language is NOT group chat's.** Same transport, different payload.
Group chat sends `2…0001 1 Cyn` / `0 0 40Cyn\\t01…`; GM chat sends the records
below. Do not route one through the other's encoder.

THE RECORD FORMAT, from app.dll's dispatcher at `0x4ab5c36`:

    <class><subcode><hexdigit len><payload>          fields separated by \\x07

`0x4ab2d0c` parses the length by sprintf'ing `"0x%c"` and strtoul'ing it, so the
length really is ONE hex digit and anything longer is not expressible.

    class  handler     what
    -----  ----------  ----------------------------------------------------
    'T'    0x4ab54db   chat text. Everything after the FIRST \\x07 is the line.
                       The speaker is resolved against the 64-entry member table
                       at chatwin+0x350 (0x30 bytes each: 64-bit id, role,
                       0x20-byte name) and the ROLE picks the colour -- role 1
                       red, role 3 green, else near-black. That is the "GM is
                       indicated by phoenix" flag. Rendered as `Name > text`.
    'U'    0x4ab59c1   membership. Subcode is a LETTER:
                          A suspended   E left   G joined   R resumed   S started
                       then one hex digit of name length, then the name. So
                       `US3Cas` renders "Fox has started GM chat."
    'H'    0x4ab3610   presence / roster. What the client itself emits, built by
                       "%c%c%s%c%c%013I64u%x%.15s%s" at 0x4ab369b.
    'K'    0x4ab5946   not yet decoded.
    'G', \\x07                accepted and ignored by the dispatcher.

WARNING: **`T` and `U` are ENCODERS AGAINST A READ, NOT A CAPTURE.** No SE GM chat
traffic survives, so the shapes below are derived from the client's parsers and
have not been confirmed by anything rendering them. `gm_raw()` exists precisely
so a wrong guess costs an edit to a spool file rather than a rebuild.

THE SPOOL. authserv has no inbound API and adding a port for this would be a new
lane; instead a GM line is a FILE. `tools/gmsay.py` appends to

    <POL_GMCHAT_SPOOL>/<room>.txt        default /data/gm-chat

one record per line, and the session loop drains it on every pass -- including
the keepalive tick, so an idle client still gets its line within `POL_AUTH_PING`
seconds. Draining removes the file, so a line is delivered once.
"""
import json
import os
import threading
import time

SPOOL = os.environ.get("POL_GMCHAT_SPOOL", "/data/gm-chat")

#: A room gmserver handed out. Everything else on this band is group chat and
#: must keep going through the group-chat encoders.
PREFIX = os.environ.get("POL_GMCHAT_PREFIX", "#gm").encode()

#: The nick a server-originated GM line is attributed to. app.dll resolves the
#: speaker through the member table, so this is what NAMES/roster ties back to.
GM_NICK = os.environ.get("POL_GMCHAT_NICK", "GM").encode()

SEP = b"\x07"

_lock = threading.Lock()


def is_gm_room(chan):
    """True for a channel the GM Call band handed out."""
    return bool(chan) and chan.startswith(PREFIX)


def _hexlen(n):
    """The one-hex-digit length field. 0x4ab2d0c cannot express more than 0xf."""
    return format(min(n, 0xF), "x").encode()


#: The 'T' header, between the class byte and the \x07. **Captured from the
#: client itself, 2026-08-16**: typing "HIIII" in the GM chat window emitted
#: `PRIVMSG #gmchat001 :TI01\x07HIIII`. The first encoder here guessed the header
#: was empty (`T\x07text`) -- it was delivered, twice, and rendered nothing, so
#: the header is load-bearing rather than decoration. Mirroring the client's own
#: shape is the best-evidenced thing we can send. `I` and `01` are not yet
#: decoded individually; POL_GMCHAT_T_HEAD overrides the pair for probing.
T_HEAD = os.environ.get("POL_GMCHAT_T_HEAD", "I01").encode()


def encode_text(text, head=None):
    """A 'T' record: everything after the first \\x07 is the rendered line."""
    if isinstance(text, str):
        text = text.encode("cp932", "replace")
    return b"T" + (T_HEAD if head is None else head) + SEP + text


def encode_event(event, who):
    """A 'U' record. `event` is one of the subcode letters A/E/G/R/S."""
    if isinstance(who, str):
        who = who.encode("cp932", "replace")
    ev = event.encode() if isinstance(event, str) else event
    who = who[:0xF]
    return b"U" + ev[:1].upper() + _hexlen(len(who)) + who


def _path(chan):
    safe = chan.decode("latin1", "replace").replace("/", "_").replace("\\", "_")
    return os.path.join(SPOOL, safe + ".txt")


def drain(chan):
    """Pop every spooled record for `chan` as (nick_or_None, record). Never raises.

    Delete-after-read, under a lock: the session loop calls this on every pass and
    two connections in the same room must not both deliver the same line.
    """
    p = _path(chan)
    with _lock:
        try:
            if not os.path.exists(p):
                return []
            with open(p, "rb") as f:
                raw = f.read()
            os.remove(p)
        except OSError:
            return []
    out = []
    for ln in raw.replace(b"\r\n", b"\n").split(b"\n"):
        if not ln:
            continue
        nick, tab, rec = ln.partition(b"\t")
        out.append((nick, rec) if tab else (None, ln))
    return out


def spool(chan, record, nick=None):
    """Append one record, optionally attributed to a specific nick.

    Spool line is `<nick>\\t<record>`, bare `<record>` for the default GM nick.

    WARNING: The nick matters as much as the record. A 'T' takes its speaker from the
    TRANSPORT, not from its own bytes, and app.dll resolves it against the member
    table at chatwin+0x350 -- so a line from a nick nobody in that table knows has
    nobody to attribute and can render as nothing even though it arrived intact.
    Echoing from the CLIENT'S OWN nick is the cheapest way to tell "the record is
    malformed" apart from "the speaker did not resolve".
    """
    with _lock:
        os.makedirs(SPOOL, exist_ok=True)
        with open(_path(chan), "ab") as f:
            f.write((nick + b"\t" if nick else b"") + record + b"\n")


def pending(chan):
    """How many records are spooled and NOT yet delivered, without draining.

    The panel needs this to keep an honest distinction. Spooling always
    "succeeds" -- it is a file append -- but a line only leaves the building when
    a session that is IN that room comes round the loop. With nobody in the room
    the spool just grows, and a console that reported "sent" for both states
    would be lying about the more common one.
    """
    try:
        with open(_path(chan), "rb") as f:
            return sum(1 for ln in f.read().split(b"\n") if ln.strip())
    except OSError:
        return 0


def privmsg(chan, record, srv, nick=None):
    """Wrap a record as a channel PRIVMSG from the GM."""
    who = nick or GM_NICK
    return b":" + who + b"!~x@" + srv + b" PRIVMSG " + chan + b" :" + record


# --------------------------------------------------------------------- record
#: What each class byte means, for a reader who has not memorised app.dll. Kept
#: beside the encoders so the two cannot describe different languages.
CLASSES = {b"T": "chat text", b"U": "membership", b"H": "presence / roster",
           b"K": "undecoded (0x4ab5946)", b"G": "accepted and ignored"}
#: 'U' subcodes, from 0x4ab59c1.
EVENTS = {"A": "suspended", "E": "left", "G": "joined", "R": "resumed",
          "S": "started"}


def describe(rec):
    """A one-line reading of a record, for a human watching the room.

    Deliberately TOLERANT and never raising: the whole point of the transcript is
    to see records whose shape we got wrong, and a decoder that threw on the
    malformed ones would hide exactly the traffic worth looking at. Anything it
    cannot read comes back as the raw bytes, which is still the most useful thing
    to put in front of somebody.
    """
    if not rec:
        return ""
    cls, kind = rec[:1], CLASSES.get(rec[:1])
    if cls == b"T":
        head, sep, text = rec[1:].partition(SEP)
        if not sep:
            return f"malformed T (no {SEP!r} separator): {rec!r}"
        return (text.decode("cp932", "replace")
                + (f"   [head {head.decode('latin1')!r}]"
                   if head != T_HEAD else ""))
    if cls == b"U" and len(rec) >= 3:
        ev = rec[1:2].decode("latin1")
        try:
            n = int(rec[2:3], 16)
        except ValueError:
            return f"malformed U (bad length digit): {rec!r}"
        who = rec[3:3 + n].decode("cp932", "replace")
        return f"{who} {EVENTS.get(ev.upper(), 'subcode ' + ev)}"
    if cls == b"H":
        # `HA<room>:<flag><guid13><lenhex><name>dummy`, per 0x4ab369b. Only the
        # tail is worth showing; the client emits one of these on a timer and a
        # transcript full of full heartbeats is a transcript nobody reads.
        return f"presence {rec[1:2].decode('latin1')}: {rec[2:60]!r}"
    return f"{kind or 'unknown class ' + repr(cls)}: {rec!r}"


# ----------------------------------------------------------------- transcript
#: One JSONL file per room beside its spool. This is the half that did NOT exist:
#: lines the GM sends were write-and-forget, and the CLIENT'S OWN records reached
#: nothing but the authserv log, mixed in with every other session.
#:
#: WARNING: IT IS ALSO THE CAPTURE. `T`/`U` here are encoders written against the
#: client's PARSERS, with no surviving SE traffic to check them against, and the
#: one correction ever made to them -- T_HEAD, from `TI01\x07HIIII` -- came from
#: reading a client's own line out of a log by hand. Recording every inbound
#: record verbatim means the next such correction is a lookup rather than an
#: archaeology session, so `raw` keeps the ORIGINAL BYTES (hex), never just the
#: decoded text.
TRANSCRIPT_MAX = int(os.environ.get("POL_GMCHAT_TRANSCRIPT_MAX", "2000"))


def _tpath(chan):
    if isinstance(chan, str):
        chan = chan.encode()
    safe = chan.decode("latin1", "replace").replace("/", "_").replace("\\", "_")
    return os.path.join(SPOOL, safe + ".log")


def record(chan, direction, nick, rec, note=None):
    """Append one line to a room's transcript. NEVER raises.

    Called from the authserv session loop, which must not lose a client's
    connection because a disk filled up -- silence is the safe failure here, the
    same rule the relay around it already follows.
    """
    try:
        if isinstance(rec, str):
            rec = rec.encode("cp932", "replace")
        row = {"at": time.time(), "dir": direction,
               "nick": (nick or b"").decode("latin1", "replace")
                       if isinstance(nick, bytes) else (nick or ""),
               "raw": rec.hex(), "text": describe(rec)}
        if note:
            row["note"] = note
        with _lock:
            os.makedirs(SPOOL, exist_ok=True)
            with open(_tpath(chan), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, ValueError, UnicodeError):
        pass


def transcript(chan, limit=200):
    """The last `limit` records of a room, oldest first. Never raises."""
    try:
        with open(_tpath(chan), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for ln in lines[-max(1, limit):]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue                   # a half-written tail is not an error
    return out


def rooms():
    """Every room this spool knows about, most recently active first.

    BOTH kinds of file count. A room with a transcript (`.log`) has had traffic;
    a room with only an undelivered spool (`.txt`) has had a GM talking into an
    empty room -- which is precisely the state an operator needs to see, so
    listing transcripts alone would hide the case worth noticing.
    """
    try:
        names = os.listdir(SPOOL)
    except OSError:
        return []
    seen = {}
    for n in names:
        if n.endswith(".log"):
            room = n[:-len(".log")]
        elif n.endswith(".txt"):
            room = n[:-len(".txt")]
        else:
            continue
        try:
            mtime = os.path.getmtime(os.path.join(SPOOL, n))
        except OSError:
            continue
        seen[room] = max(seen.get(room, 0), mtime)
    return [n for n, _ in sorted(seen.items(), key=lambda r: -r[1])]


def trim(chan):
    """Hold a transcript to TRANSCRIPT_MAX lines. Cheap, and only ever called
    from the panel's own read path, so a busy room never blocks the relay."""
    p = _tpath(chan)
    try:
        with _lock:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= TRANSCRIPT_MAX:
                return len(lines)
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines[-TRANSCRIPT_MAX:])
            return TRANSCRIPT_MAX
    except OSError:
        return 0
