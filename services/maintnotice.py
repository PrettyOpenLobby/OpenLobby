#!/usr/bin/env python3
"""The maintenance warning, the way retail gave it.

SE had no wire message that pops text on every connected Viewer (lobby
replies are fixed-length per opcode, so an unsolicited one has no reader).
Its maintenance tools were portal files the title pages include on every
visit, plus mail:

  FORCED NEWS  `pcd/sftp/<lang>/<title>/anews.pml` (and the `WIN/`,
               `XBOX360/` copies a page picks by platform). A title page
               includes it and, when `$length` is not 0, opens a window with
               `$subject`, `$news_date` and the `$news` array before the player
               can press Play. SE used it for "All Worlds Maintenance".
               Its shape, as the client's title pages read it:

                   <meta http-equiv="Cache-Control" content="no-cache">
                   <define name="$news_date" value="Mar 3, 2014 09:00 Mon [PST]">
                   <define name="$subject" value="...">
                   <define name="$length" value="10">
                   <array name="$news">
                   "first chunk", "second chunk", ... ten strings
                   </array>

               A chunk stays under the Viewer's 0x400-character value limit
               (pmleval.Vars.set: a longer value is silently not stored), so
               long text is split, as SE's own file splits mid-word.
  PLAY SWITCH  `sfst02.pml` beside it: `$playbutton_enable`, `$patch_enable`
               and `$status` (the ticker line). Not written here; it is a
               per-title decision and the files are edited by hand.
  MAIL         a PlayOnline message to every member online (core/broadcast.py),
               which reaches a player who is in a game rather than on a title
               page.

This module writes and clears the forced news. Writing keeps the file it
replaces as `anews.pml.pre-notice` and clearing puts it back, so the file it
replaced is never lost.
"""
import datetime
import os
import re

SFTP_REL = os.path.join("wh000.pol.com", "pcd", "sftp")
BACKUP_SUFFIX = ".pre-notice"
#: SE's array is ten strings long whatever the text needs.
NEWS_SLOTS = 10
#: Characters per string: under the Viewer's 0x400 value limit with room for
#: the entities a line break becomes.
CHUNK = 900
#: What a cleared file holds: SE's "no forced news".
EMPTY = '<meta http-equiv="Cache-Control" content="no-cache">\n<define name="$length" value="0">\n'


def anews_files(www, titles=None):
    """Every forced-news file under the served trees (www/, _lang/*, _eras/*),
    optionally only those of `titles` (the sftp directory names: ff11, tetra,
    jan, fmo, dc, fe ...). Sorted, absolute."""
    roots = [www]
    for sub in ("_lang", "_eras"):
        d = os.path.join(www, sub)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                roots.append(os.path.join(d, name))
                inner = os.path.join(d, name)
                for lang in sorted(os.listdir(inner)) if os.path.isdir(inner) else ():
                    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", lang):
                        roots.append(os.path.join(inner, lang))
    want = {t.strip().lower() for t in (titles or []) if t and t.strip()}
    out = []
    for root in roots:
        base = os.path.join(root, SFTP_REL)
        if not os.path.isdir(base):
            continue
        for d, _dirs, files in os.walk(base):
            if "anews.pml" not in files:
                continue
            rel = os.path.relpath(d, base).replace(os.sep, "/").split("/")
            # <lang>/<title>[/<PLATFORM>] or (panel era) <title>[/<PLATFORM>]
            title = rel[1] if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", rel[0]) and len(rel) > 1 else rel[0]
            if want and title.lower() not in want:
                continue
            out.append(os.path.join(d, "anews.pml"))
    return sorted(set(out))


def _esc(text):
    """Text for a PML string value: no quote (it would end the value), no raw
    ampersand (an unknown entity errors the page), line breaks as SE's &br;,
    an apostrophe as SE's &#39;."""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("&", "and").replace('"', "'").replace("<", "(").replace(">", ")")
    s = s.replace("'", "&#39;")
    return s.replace("\n", "&br;")


def _chunks(s):
    out = []
    while s and len(out) < NEWS_SLOTS:
        cut = CHUNK
        # never split an entity
        amp = s.rfind("&", 0, cut)
        if amp != -1 and ";" not in s[amp:cut] and len(s) > cut:
            cut = amp
        out.append(s[:cut])
        s = s[cut:]
    return out + [""] * (NEWS_SLOTS - len(out))


def render(subject, body, when=None):
    """The forced-news file text for one notice."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    date = when.strftime("%b %d, %Y %H:%M %a [UTC]")
    subj = _esc(subject).replace("&br;", " ")[:60]
    lines = ['<meta http-equiv="Cache-Control" content="no-cache">',
             '<define name="$news_date" value="%s">' % date,
             '<define name="$subject" value="%s">' % subj,
             '<define name="$length" value="%d">' % NEWS_SLOTS,
             '<array name="$news">']
    parts = _chunks(_esc(body))
    lines.append(",\n".join('"%s"' % p for p in parts))
    lines.append("</array>")
    return "\n".join(lines) + "\n"


def _encoding_of(path):
    """Keep a Japanese file Japanese: cp932 when the file it replaces is not
    UTF-8, else UTF-8 (the band door adds the charset for a non-ASCII UTF-8
    file)."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return "utf-8"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp932"


def post(www, subject, body, titles=None, when=None):
    """Write the notice into every matching forced-news file. Returns the
    list of files written (relative to www)."""
    if not str(subject or "").strip() or not str(body or "").strip():
        raise ValueError("a notice needs a subject and a body")
    text = render(subject, body, when)
    done = []
    for path in anews_files(www, titles):
        bak = path + BACKUP_SUFFIX
        if not os.path.exists(bak):
            with open(path, "rb") as f:
                orig = f.read()
            with open(bak, "wb") as f:
                f.write(orig)
        data = text.encode(_encoding_of(bak), "replace")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        done.append(os.path.relpath(path, www).replace(os.sep, "/"))
    return done


def clear(www, titles=None):
    """Take the notice down: every file that has a backup gets it back."""
    done = []
    for path in anews_files(www, titles):
        bak = path + BACKUP_SUFFIX
        if not os.path.exists(bak):
            continue
        os.replace(bak, path)
        done.append(os.path.relpath(path, www).replace(os.sep, "/"))
    return done


def status(www):
    """[{file, posted, subject}] for every forced-news file."""
    out = []
    for path in anews_files(www):
        posted = os.path.exists(path + BACKUP_SUFFIX)
        subject = ""
        try:
            raw = open(path, "rb").read()
            m = re.search(rb'name="\$subject" value="([^"]*)"', raw)
            if m:
                subject = m.group(1).decode(_encoding_of(path), "replace")
            shown = re.search(rb'name="\$length" value="([1-9]\d*)"', raw) is not None
        except OSError:
            shown = False
        out.append({"file": os.path.relpath(path, www).replace(os.sep, "/"),
                    "posted": posted, "shown": shown, "subject": subject})
    return out
