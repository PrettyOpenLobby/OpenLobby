#!/usr/bin/env python3
"""Server announcements: the store, and every PML file the Viewer reads.

This module is the single implementation behind BOTH the CLI
(`tools/gen_news.py`) and the admin dashboard's News tab. It lives under
`services/` rather than `tools/` because the admin container only ever gets
`services/` -- on prod the image COPYs it and `docker-compose.sync.yml`
live-mounts it, while `tools/` is not mounted anywhere. Putting the renderer in
tools/ would mean the panel could not publish at all.

## What gets written

    wh000.pol.com/pcd/ntool/<loc>/latestnews.pml    the login-screen ticker
    wh000.pol.com/pcd/ntool/<loc>/news<N>.pml       Information section, per content
    wh000.pol.com/pcd/ntool/<loc>/<serial>.pml      one per announcement: the body
    wh000.pol.com/pml/info/news0.pml                fr-FR/de-DE fallback copy
    info.playonline.com/snews/<loc>/index.pml       our own substitute page

The ticker is the surface that matters -- 214 requests for
`/pcd/ntool/en-US/latestnews.pml` in logs/ against 11 for the Information page.

## Where the formats come from -- all transcribed, none invented

`latestnews.pml` is SE's live English feed (pol-www/www-se-capture/): an
`<ARRAY NAME="$LATESTNEWS">` of 8-field records plus `<define
name="$LATESTNEWSMAX">`. Field names are SE's own, from `pml/info/in04.pml`:
$aCONTID / $aSERIAL / $aMORE / $aLINK / $aDATE / $aSUB / $aSTAMP.

`news<N>.pml` is the data behind SE's REAL Information section
(`wh000.pol.com/pml/info/index.pml`, 53 KB, already in www/ with all its art).
`in04.pml` includes it, and `index.pml` includes
`'/pcd/ntool/'+$_USER_LANG+'/news'+$cnt_id+'.pml'`. Its 11-field record is
documented by SE IN index.pml itself:

    $ar_ci  0   content id
    $ar_si  1   serial id
    $ar_uf  2   link flag        1 = none / 2 = link
    $ar_ur  3   link URL         ("null" when absent)
    $ar_dt  4   date
    $ar_mi  5   headline
    $ar_st  6   sticky flag      1 = normal / 2 = sticky   (SE: "currently unused")
    $ar_pr  7   parent/child     0 = neither / 1 = parent / 2 = child
    $ar_ic  8   icon flag        1 = none / 2 = resolved / 3 = follow-up
    $ar_ma  9   maint/other      1 = other / 2 = trouble / 3 = maintenance
    $ar_ch  10  number of children

`$NARRAY` holds FIVE sub-arrays, one per category, named in `inen01.pml`
($cat_d): Important Notices / General / Server Maintenance / Events / Updates.
Every array -- including an empty one -- ends with an all-empty terminator row.
Alongside it: `$NEW_DATE` / `$NEW_FLAG` / `$NUMSUB` (5 each), then `$MNT_INFO`
and `$TRB_INFO` (the Status/Maintenance panel, in05.pml) and `$NUMINFO`, which
is [len(MNT), len(TRB)].

WARNING: `$NUMSUB[i]` is NOT the row count -- it counts rows whose `$ar_pr != 2`,
i.e. follow-ups nested under a parent do not count. MEASURED against SE's
en-US/news1.pml, whose `$NUMSUB` claims "51","3","11","0","5" while the buckets
hold 56/3/12/0/5 rows; the differences are exactly the five `$ar_pr==2` rows.

`<serial>.pml` is the per-article body. `pml/info/index2.pml` -- the detail page
the TICKER ITSELF links to (`/pml/info/index2.pml?dat=..&seri=<serial>&fr=<cid>`,
straight out of `pml/main/index.pml`'s `$arNewsLink`) -- includes
`'/pcd/ntool/'+$_USER_LANG+'/'+$seri+'.pml'`. Two real SE articles survive in
our tree, `pcd/ntool/en-US/2194.pml` and `2195.pml`, and they are the model:

    <ARRAY NAME="$DETAILS">  "1", "null", "<date>", "<headline>", "<cid>", "<cat>"
    <DATA NAME="BODY" SUB="ENT"><RECORD>"<body with &br;>"</RECORD></DATA>

  $ar_mr 0 = related-info flag (2 shows an extra button), $ar_ur 1 = its URL,
  $ar_dt 2 = date, $ar_sb 3 = headline, $ar_ci 4 = From content id (1-BASED --
  index2.pml reads `$cnt_d[$DETAILS[$ar_ci]-1]`), $ar_ct 5 = category id (0-4).

  This supersedes the old "there are no NNNN.pml detail pages" conclusion. That
  search looked under `pml/info/news/`, which holds only `ne_i` images. The
  detail files live under `pcd/ntool/<lang>/`, and we captured two of them.

## Two rules the writer enforces

1. **Path allowlist.** `publish()` refuses to write anything outside the five
   shapes above, and a `<serial>.pml` only when the serial is 9xxxxx. Our
   SERIAL_BASE sits an order of magnitude above SE's 277xx precisely so our
   files can never collide with -- or overwrite -- a captured article.
2. **SE's archive is merged, not replaced.** `news<N>.pml` already carries SE's
   real 2003-2026 news in en-US and ja-JP. Publishing drops only rows we wrote
   before (serial >= SERIAL_BASE) and puts ours on top, so the archive survives
   every republish. Any file replaced for the first time gets a `.se-orig`
   sibling, the same convention gen_news.py already used for latestnews.pml.
"""
import os
import re
import struct
import textwrap

try:
    import yaml
except ImportError:                                            # pragma: no cover
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Serving tree. The admin container mounts it at /www; a checkout uses ./www.
WWW_DIR = os.environ.get("POL_WWW_DIR") or os.environ.get("POL_ADMIN_WWW") \
    or os.path.join(ROOT, "www")

#: The git-tracked seed, shipped with the repo.
SEED = os.path.join(ROOT, "content", "announcements.yaml")

#: The LIVE store the dashboard writes. /data is rw-mounted in dev and prod and
#: is covered by the nightly pol-backup, and keeping it out of the git tree means
#: an operator editing announcements on prod cannot leave the working tree dirty
#: and stall the ff-only pol-git-sync. Falls back to SEED when absent, so a fresh
#: install serves the shipped announcements until the first save.
STORE = os.environ.get("POL_NEWS_STORE") or os.path.join(
    os.environ.get("POL_DATA_DIR", "/data"), "announcements.yaml")

#: Locales to write the ticker, the news<N> lists and the detail bodies under.
#: en-US is the only one our PC client has been observed asking for; ja-JP is
#: here because the PS2 asks for it BY NAME (a 1.18.15f console resolves
#: $_USER_LANG to ja-JP even though its UA says [jp]), and without it the console
#: falls through to SE's captured 2007 Japanese news.
FEED_LOCALES = ["en-US", "us", "na", "en", "ja-JP"]

#: ...and our substitute Information page. The client's locale probing tries
#: several spellings and we do not know which it settles on.
PAGE_LOCALES = ["0", "1", "en", "en-US", "eu", "jp", "na", "us", "usa"]

#: SE's $cnt_d, from inen01.pml. The id is 1-BASED: index2.pml reads
#: `$cnt_d[$DETAILS[$ar_ci]-1]`, and news<N>.pml is named by this same id.
#:
#: WARNING: THESE ARE SE'S OWN IDS, READ OFF THE SPRITE'S SEQUENCE TABLE. An earlier
#: version of this table packed our services into a dense 0-7, on the theory
#: that `sd:sequence=N` selects image N. IT DOES NOT -- see ang_sequences().
#: Sequences indirect to images, and SE's map is:
#:
#:      seq 1 PlayOnline    seq 5 PlayOnline again   seq 9  Dirge of Cerberus
#:      seq 2 FFXI          seq 6 (blank)            seq 10 Fantasy Earth
#:      seq 3 Tetra Master  seq 7 Front Mission      seq 11 FFXIV        (ours)
#:      seq 4 JongHoLow     seq 8 (blank)            seq 12 EverQuest II (ours)
#:
#: which is exactly the numbering SE's own JP feed used: they shipped
#: `news7.pml` carrying Front Mission Online rows and `news9.pml` carrying Dirge
#: of Cerberus rows. That evidence was in hand early and got talked away; the
#: dense guess cost four live client cycles and a broken login ticker.
#:
#: 6 and 8 are deliberately absent -- their sequences draw the blank image, so a
#: story filed there would ride the ticker with no badge at all.
CONTENTS = {
    "playonline": 1,
    "ffxi":       2,
    "tetra":      3,
    "jan":        4,
    "extras":     5,
    "fmo":        7,
    "doc":        9,
    "fe":         10,
    "ffxiv":      11,
    "eqii":       12,
}
CONTENT_LABELS = {
    "playonline": "PlayOnline", "ffxi": "FINAL FANTASY XI",
    "tetra": "Tetra Master", "jan": "JongHoLow", "extras": "Extras",
    "fmo": "Front Mission Online", "doc": "Dirge of Cerberus",
    "fe": "Fantasy Earth", "ffxiv": "FINAL FANTASY XIV",
    "eqii": "EverQuest II",
}

#: WARNING: THIS NUMBERING IS NOT THE PLAYONLINE CONTENT-ID NUMBERING (`sqpolcts.bin`,
#: the table the Viewer itself uses). Two different spaces that overlap in value
#: and disagree in meaning, so reading one as the other names a real but WRONG
#: title -- POL 1-4 are FFXI / Tetra / Janhourou / FMO while the same numbers
#: here are PlayOnline / FFXI / Tetra / JongHoLow. Ours cannot be renumbered to
#: fix that: these ARE SE's news ids, baked into the archived rows in SE's own
#: news<N>.pml that publish() merges rather than replaces, and into the sprite's
#: sequence table. `sqpolcts.bin` is the POL table; this is not it.
#:
#: The content id picks the ticker badge, but NOT DIRECTLY.
#: `pml/main/index.pml` drives it with `sd:sequence=$LATESTNEWS[i][0]@icNews<i>`
#: -- field 0, the content id, used as the SEQUENCE number of
#: `ma_i/maic06i.ang`. A sequence then indirects to an image; see
#: ang_sequences(). An older note here said "image index == content id", which
#: was read off the image ORDER and is wrong -- it is why "Extras" was described
#: as wearing an FMO badge (sequence 5 actually draws the PlayOnline one) and
#: why every id past 5 drew the wrong art.
#:
#: `differs` marks a genuine disagreement between the badge and the label, not a
#: different spelling of the same thing. Nothing disagrees any more now the
#: sequence table is being read properly, but the field stays: SE reuses
#: sequences, so a future id could legitimately land on another title's badge
#: and an operator should be told rather than surprised.
CONTENT_ICONS = {
    "playonline": {"icon": "the PlayOnline 'O'", "short": "PlayOnline",
                   "differs": False},
    "ffxi":       {"icon": "the FFXI 'XI'", "short": "XI", "differs": False},
    "tetra":      {"icon": "the Tetra Master 'TM'", "short": "TM",
                   "differs": False},
    "jan":        {"icon": "the Janhourou mahjong tiles",
                   "short": "mahjong tiles", "differs": False},
    "extras":     {"icon": "the PlayOnline 'O' -- sequence 5 reuses it",
                   "short": "PlayOnline", "differs": False},
    "fmo":        {"icon": "the Front Mission Online 'FMO' badge",
                   "short": "FMO", "differs": False},
    "doc":        {"icon": "the Dirge of Cerberus 'DC' badge", "short": "DC",
                   "differs": False},
    "fe":         {"icon": "the Fantasy Earth badge", "short": "Fantasy Earth",
                   "differs": False},
    "ffxiv":      {"icon": "the red 'XIV' badge", "short": "XIV",
                   "differs": False},
    "eqii":       {"icon": "the gold 'EQII' badge", "short": "EQII",
                   "differs": False},
}

#: SE's $cat_d, from inen01.pml -- the five buckets inside $NARRAY, and the
#: value of the 8th latestnews field.
CATEGORIES = ["Important Notices", "General", "Server Maintenance",
              "Events", "Updates"]

#: Per `kind`: the ticker's ($aSTAMP, category) pair, plus the two Information
#: flags $ar_ic (1 none / 2 resolved / 3 follow-up) and $ar_ma (1 other /
#: 2 trouble / 3 maintenance).
#:
#: The stamp/category pair is INFERRED, read off SE's live feed by pairing the
#: numbers against the headlines they shipped with -- no page documents that
#: enum. The ic/ma pair is NOT inferred: index.pml names both, and SE's own
#: $TRB_INFO row "Recovery from Official Website Issue" carries ic=2, ma=2,
#: which is exactly what `recovery` produces here.
KINDS = {
    #             stamp  category   ic  ma
    "info":        (1,      1,       1,  1),
    "maintenance": (3,      2,       1,  3),
    "recovery":    (1,      2,       2,  2),
    "issue":       (0,      1,       1,  2),
    "update":      (0,      4,       1,  1),
}

#: What each `kind` actually does, in the operator's words. Kept HERE rather
#: than in the dashboard's HTML so the sentence and the numbers above it cannot
#: drift apart, and so `gen_news.py --explain` says the same thing the panel does.
#:
#: MEASURED, not guessed: the row marker comes from `$lst_ic` in
#: `pml/info/index.pml`, a `[$ar_ma-1][$ar_ic-1][is-child]` lookup table. Read
#: off the non-child column it reduces to: ic 2 -> "re", ic 3 -> "ad", otherwise
#: ma picks "pr" / "tr" / "ma". Each name is an `<inlineimg>` in
#: `pml/info/style.pml`; ART_MARKERS below has the file each one draws.
KIND_NOTES = {
    "info":        "An ordinary notice. Plain bullet in the Information list.",
    "maintenance": "Scheduled downtime. Files under Server Maintenance and "
                   "draws SE's maintenance badge.",
    "recovery":    "The all-clear for an earlier problem. Draws the "
                   "“resolved” badge.",
    "issue":       "Something is broken right now. Draws the trouble badge.",
    "update":      "A patch or content change. Files under Updates.",
}


def row_marker(kind):
    """Which `<inlineimg>` the Information list draws for `kind`.

    The reduction of SE's `$lst_ic[$ar_ma-1][$ar_ic-1][0]` table; see KIND_NOTES.
    """
    _stamp, _cat, ic, ma = KINDS[kind]
    if ic == 2:
        return "re"
    if ic == 3:
        return "ad"
    return {1: "pr", 2: "tr", 3: "ma"}[ma]


#: The row markers, as SE declares them in `pml/info/style.pml`
#: (`<inlineimg name="pr" ... src="$C_PATH1+'ne_s/neic07s.png'">` and friends),
#: resolved to www-relative paths so the dashboard can show the real art next to
#: each choice instead of describing it in prose.
ART_MARKERS = {
    "pr": "wh000.pol.com/pml/info/ne_s/neic07s.png",   # plain bullet, 14x14
    "re": "wh000.pol.com/pml/info/ne_s/neic06s.png",   # resolved,    46x24
    "ad": "wh000.pol.com/pml/info/ne_s/neic08s.png",   # follow-up
    "tr": "wh000.pol.com/pml/info/ne_s/neic04s.png",   # trouble
    "ma": "wh000.pol.com/pml/info/ne_s/neic05s.png",   # maintenance
}

#: The login ticker's badge strip. `pml/main/index.pml` draws it as
#: `sd:sequence=$LATESTNEWS[i][0]@icNews<i>`, so frame N of this sprite IS the
#: badge for content id N -- see CONTENT_ICONS.
TICKER_SPRITE = "wh000.pol.com/pml/main/ma_i/maic06i.ang"


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunks(png):
    """`[(type, data), ...]`, ignoring the stored CRCs."""
    out = []
    pos = 8
    while pos + 8 <= len(png):
        size = int.from_bytes(png[pos:pos + 4], "big")
        out.append((png[pos + 4:pos + 8], png[pos + 8:pos + 8 + size]))
        pos += 12 + size
    return out


def _png_build(chunks):
    """Serialise `[(type, data), ...]` back into a PNG, with fresh CRCs."""
    import zlib
    out = [_PNG_SIG]
    for typ, data in chunks:
        out.append(len(data).to_bytes(4, "big") + typ + data
                   + (zlib.crc32(typ + data) & 0xFFFFFFFF).to_bytes(4, "big"))
    return b"".join(out)


def ang_frames(blob):
    """Split an `@ANG1B` container into its frames, as STANDALONE PNGs.

    The frames are stored as whole PNG files back to back after the cell table,
    but the container FACTORS THE PALETTE OUT: one leading blob carries the real
    `PLTE`/`tRNS` and a zero-length `IDAT`, and every frame after it carries
    pixels with `PLTE` and `tRNS` truncated to nothing. A frame lifted out
    verbatim is therefore colour-type 3 with an empty palette -- malformed, and
    a browser draws a broken-image box for it. (Pillow opens one happily, which
    is exactly how that goes unnoticed: the frames are near-greyscale, so its
    fallback ramp looks plausible. MEASURED against Chrome, which refuses them.)

    So the palette record is not merely skipped, it is spliced back in. Skipping
    it also keeps the indices honest -- counting it would shift every badge by
    one and file an FFXI story under the PlayOnline "O" -- which is why the
    result is checked against the frame count the header declares at 0x14.
    """
    if not blob.startswith(b"@ANG1B"):
        raise NewsError("not an @ANG1B container")
    want = int.from_bytes(blob[0x14:0x18], "little")
    starts = []
    at = blob.find(_PNG_SIG)
    while at >= 0:
        starts.append(at)
        at = blob.find(_PNG_SIG, at + 1)

    shared = {}
    frames = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(blob)
        chunks = _png_chunks(blob[start:end])
        if not any(t == b"IDAT" and d for t, d in chunks):
            # The palette record. Remember what it carries; it is not a frame.
            shared.update({t: d for t, d in chunks if t in (b"PLTE", b"tRNS") and d})
            continue
        frames.append(_png_build(
            [(t, shared.get(t, d) if not d else d) for t, d in chunks]))

    if len(frames) != want:
        raise NewsError(f"{TICKER_SPRITE}: found {len(frames)} frames, "
                        f"header declares {want}")
    return frames


def ang_sequences(blob):
    """`[image index per sequence]` for an `@ANG1B` container.

    WARNING: `sd:sequence=N` DOES NOT INDEX THE IMAGES. The header field at 0x0C is a
    SEQUENCE count, and each sequence is a small display list that names an
    image -- an indirection that went unnoticed for days and produced two live
    bugs: "the FFXIV icon is just empty" (its sequence points at the blank
    image) and "anything past FMO uses FMO".

    Transcribed from app.dll's reader at 0x04b36284 (memimg base 0x04900000):
    for each i < hdr[0x0C], the bytes between table[i] and table[i+1] are 4-byte
    entries `(op, arg)` with op < 0x11, and the entry whose op is 0 carries the
    image index.
    """
    total, seqs, one, frames = struct.unpack_from("<4I", blob, 8)
    n = one + frames + seqs + 3
    tab = struct.unpack_from("<%dI" % n, blob, 0x30)
    out = []
    for i in range(seqs):
        img = None
        region = blob[tab[i]:tab[i + 1]]
        for k in range(0, len(region) - 3, 4):
            op, arg = struct.unpack_from("<HH", region, k)
            if op == 0:
                img = arg
                break
        out.append(img)
    return out


def _frame_has_pixels(png):
    """True if a frame draws anything -- decided by decoding, not by index.

    Several of SE's sequences point at a blank image. Offering one of those puts
    a story on the ticker with no badge, so the check has to be real rather than
    "index 0 is the blank one".
    """
    import zlib
    ch = {t: d for t, d in _png_chunks(png)}
    if not ch.get(b"IDAT"):
        return False
    trns = ch.get(b"tRNS", b"")
    try:
        raw = zlib.decompress(ch[b"IDAT"])
    except zlib.error:
        return False
    w = int.from_bytes(png[16:20], "big")
    for row in range(0, len(raw), w + 1):
        for idx in raw[row + 1:row + 1 + w]:
            if idx >= len(trns) or trns[idx] != 0:
                return True
    return False


def available_contents(www=None):
    """The services the INSTALLED sprite can actually draw a badge for.

    The content id is the SEQUENCE number, and a sequence indirects to an image
    -- so this follows the sequence table and then checks that the image it
    lands on actually draws something. Both failure modes were reported live on
    2026-08-21: a sequence that does not exist, and a sequence that resolves to
    the blank image ("I tried to use the FFXIV icon and it was just empty").

    Derived from the file rather than declared beside it, so swapping a sprite
    changes the offer with no second edit. Returns every key if the sprite
    cannot be read at all, because refusing to serve news over unreadable art
    would be the worse failure.
    """
    root = www or WWW_DIR
    try:
        with open(os.path.join(root, *TICKER_SPRITE.split("/")), "rb") as fh:
            blob = fh.read()
        seqs = ang_sequences(blob)
        frames = ang_frames(blob)
    except (OSError, NewsError, struct.error, IndexError):
        return dict(CONTENTS)
    drawable = {i for i, img in enumerate(seqs)
                if img is not None and img < len(frames)
                and _frame_has_pixels(frames[img])}
    return {k: v for k, v in CONTENTS.items() if v in drawable}


def art(www=None):
    """Every icon the News tab shows, as `{name: (mime, bytes)}`.

    Ticker badges are keyed by content key (`ffxi`, ...), row markers by SE's
    own inlineimg name (`pr`, `re`, ...). Missing art is simply absent from the
    result -- the panel falls back to its text labels, and a half-populated
    serving tree must not break the editor.
    """
    root = www or WWW_DIR
    out = {}
    try:
        with open(os.path.join(root, *TICKER_SPRITE.split("/")), "rb") as fh:
            blob = fh.read()
        frames = ang_frames(blob)
        seqs = ang_sequences(blob)
        # The content id is the SEQUENCE, and the sequence names the image --
        # indexing frames by the content id (as this did) showed the panel a
        # different badge from the one the Viewer draws, and showed none at all
        # for any id past the frame count.
        for key, cid in CONTENTS.items():
            if cid < len(seqs) and seqs[cid] is not None \
                    and seqs[cid] < len(frames):
                out["ticker:" + key] = ("image/png", frames[seqs[cid]])
    except (OSError, NewsError, struct.error):
        pass
    for name, rel in ART_MARKERS.items():
        try:
            with open(os.path.join(root, *rel.split("/")), "rb") as fh:
                out["marker:" + name] = ("image/png", fh.read())
        except OSError:
            pass
    return out


#: Where our serials start, an order of magnitude above SE's 277xx range. This
#: is load-bearing now that a serial NAMES A FILE: the allowlist only lets
#: publish() touch `9xxxxx.pml`, so SE's captured 2194/2195.pml are unreachable.
SERIAL_BASE = 900000


class NewsError(Exception):
    """Bad announcement data. Carries a message fit to show an operator."""


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def store_path():
    """The file `load()` will read: the live store if it exists, else the seed."""
    return STORE if os.path.exists(STORE) else SEED


def validate(items):
    """Normalise and check. Returns a new list; raises NewsError on bad input."""
    if not isinstance(items, list):
        raise NewsError("expected a list of announcements")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise NewsError(f"entry {i + 1} is not a mapping")
        if not str(it.get("title") or "").strip():
            raise NewsError(f"entry {i + 1} needs a title")
        if not str(it.get("date") or "").strip():
            raise NewsError(f"entry {i + 1} needs a date")
        kind = it.get("kind") or "info"
        if kind not in KINDS:
            raise NewsError(f"entry {i + 1} has unknown kind {kind!r}; expected "
                            f"one of {', '.join(sorted(KINDS))}")
        content = it.get("content") or "playonline"
        if content not in CONTENTS:
            raise NewsError(f"entry {i + 1} has unknown content {content!r}; "
                            f"expected one of {', '.join(sorted(CONTENTS))}")
        row = {
            "date": str(it["date"]).strip(),
            "title": str(it["title"]).strip(),
            "kind": kind,
            "content": content,
            "body": str(it.get("body") or ""),
            # Shows the item in the Status/Maintenance panel (in05.pml), whose
            # own caption says "current or recently resolved". OFF by default:
            # which past outages still count as current is an editorial call,
            # not something to guess from a date string SE never parsed either.
            "status": bool(it.get("status")),
            "link": str(it.get("link") or "").strip(),
        }
        serial = it.get("serial")
        if serial not in (None, "", 0):
            try:
                serial = int(serial)
            except (TypeError, ValueError):
                raise NewsError(f"entry {i + 1} has a non-numeric serial "
                                f"{serial!r}")
            if serial < SERIAL_BASE:
                raise NewsError(
                    f"entry {i + 1} has serial {serial}, below SERIAL_BASE "
                    f"{SERIAL_BASE}. Serials name files, and only 9xxxxx.pml is "
                    f"writable -- a lower one would target SE's captured "
                    f"articles.")
            row["serial"] = serial
        out.append(row)
    return out


def assign_serials(items):
    """Give every entry a stable serial. Returns True if anything changed.

    A serial is an item's IDENTITY: it names `<serial>.pml` and it is what the
    ticker puts in the detail-page URL. The first version of gen_news.py derived
    it from list POSITION (`SERIAL_BASE + len - n`), which silently renumbered
    every existing announcement the moment a new one was added -- harmless while
    nothing referenced it, wrong now that it is a filename.
    """
    used = {it["serial"] for it in items if it.get("serial")}
    nxt = max(used) + 1 if used else SERIAL_BASE + 1
    changed = False
    for it in items:
        if not it.get("serial"):
            it["serial"] = nxt
            used.add(nxt)
            nxt += 1
            changed = True
    return changed


def load(path=None):
    """Read the store (live file, else the shipped seed). Always serial-stamped."""
    if yaml is None:
        raise NewsError("PyYAML is not installed (pip install pyyaml)")
    path = path or store_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    items = validate(raw)
    assign_serials(items)
    return items


def save(items, path=None):
    """Write the store. Serials are assigned first so they persist."""
    if yaml is None:
        raise NewsError("PyYAML is not installed (pip install pyyaml)")
    items = validate(items)
    assign_serials(items)
    path = path or STORE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    doc = []
    for it in items:
        d = {"serial": it["serial"], "date": it["date"], "title": it["title"],
             "kind": it["kind"], "content": it["content"]}
        if it.get("link"):
            d["link"] = it["link"]
        if it.get("status"):
            d["status"] = True
        if it.get("body"):
            d["body"] = it["body"]
        doc.append(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Server announcements -- written by the admin dashboard.\n"
                "# Hand edits are fine; the panel re-reads this file on every\n"
                "# request. `serial` is an identity, not a sequence: it names\n"
                "# pcd/ntool/<loc>/<serial>.pml. Never reuse or renumber one.\n"
                "# Publish from the panel's News tab, or with\n"
                "# `python tools/gen_news.py`.\n\n")
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False, width=100)
    os.replace(tmp, path)
    return path, items


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #
def _q(s):
    """SE quotes every field. Kill the one character that could end it early."""
    return str(s).replace('"', "'")


def _body_ent(body):
    """Body prose -> one SUB="ENT" record string, SE's way.

    Measured from `pcd/ntool/en-US/2194.pml`: a single <RECORD>, one quoted
    string, `&br;` for every line break, `&amp;` for a literal ampersand and
    `&#39;` for an apostrophe. No `&pre=1;` -- index2.pml's textbox wraps.
    """
    text = (body or "").strip()
    if not text:
        return ""
    text = text.replace("&", "&amp;").replace('"', "&#34;").replace("'", "&#39;")
    paras = [" ".join(p.split()) for p in text.split("\n\n") if p.strip()]
    return "&br;&br;".join(paras)


def _record(fields):
    return "\t<ARRAY>" + ",".join(f'"{f}"' for f in fields) + "</ARRAY>"


#: SE closes every array -- even an empty one -- with an all-empty row.
_TERMINATOR = _record([""] * 11)


def _narray_fields(item):
    """One announcement as its 11-field $NARRAY / $MNT_INFO / $TRB_INFO record."""
    _stamp, cat, ic, ma = KINDS[item["kind"]]
    link = item.get("link") or ""
    return cat, [
        str(CONTENTS[item["content"]]),        # 0  $ar_ci
        str(item["serial"]),                   # 1  $ar_si
        "2" if link else "1",                  # 2  $ar_uf
        _q(link) if link else "null",          # 3  $ar_ur
        _q(item["date"]),                      # 4  $ar_dt
        _q(item["title"]),                     # 5  $ar_mi
        "1",                                   # 6  $ar_st  (SE: unused)
        "0",                                   # 7  $ar_pr  (no follow-ups yet)
        str(ic),                               # 8  $ar_ic
        str(ma),                               # 9  $ar_ma
        "0",                                   # 10 $ar_ch
    ]


# --------------------------------------------------------------------------- #
# latestnews.pml -- the login-screen ticker
# --------------------------------------------------------------------------- #
def feed(items):
    """SE's $LATESTNEWS array: 8 fields, then $LATESTNEWSMAX.

    Field 7 is the CATEGORY: pml/main/index.pml builds the headline's href as
    `$dat = -1000009900 - $LATESTNEWS[n][7]*10000`, and index.pml unpacks
    `$cat_id = $abs*$dat%100000/10000` -- which is exactly that field. Field 2
    (`$aMORE`) picks the target: 2 takes field 3 as a raw link, anything else
    goes to `/pml/info/index2.pml?...&seri=<field 1>`, our detail page. So
    `more` is 1 for an item whose body we publish and 0 for one we do not --
    which is the fix for the dead headline the old generator produced, where
    more was hardcoded 0 and clicking a story did nothing.
    """
    rows = []
    for it in items:
        stamp, cat, _ic, _ma = KINDS[it["kind"]]
        link = it.get("link") or ""
        more = "2" if link else ("1" if (it.get("body") or "").strip() else "0")
        rows.append(
            "<ARRAY>\n"
            f'\t"{CONTENTS[it["content"]]}",\n'
            f'\t"{it["serial"]}",\n'
            f'\t"{more}",\n'
            f'\t"{_q(link)}",\n'
            f'\t"{_q(it["date"])}",\n'
            f'\t"{_q(it["title"])}",\n'
            f'\t"{stamp}",\n'
            f'\t"{cat}"\n'
            "</ARRAY>")
    return ('\n<ARRAY NAME="$LATESTNEWS">\n'
            + "\n".join(rows)
            + "\n</ARRAY>\n"
            + f'<define name="$LATESTNEWSMAX" value="{len(items)}">\n')


# --------------------------------------------------------------------------- #
# <serial>.pml -- the per-article body, reached from the ticker
# --------------------------------------------------------------------------- #
def detail(item):
    """One article, shaped like SE's captured 2194.pml / 2195.pml."""
    _stamp, cat, _ic, _ma = KINDS[item["kind"]]
    link = item.get("link") or ""
    return (
        '<META http-equiv="Cache-Control" content="no-cache">\n'
        '<ARRAY NAME="$DETAILS">\n'
        # $ar_mr: 2 lights up the extra button, whose href is
        # `$DETAILS[$ar_ur]+'dat='+$dat` -- so the URL must end in ? or &.
        f'\t"{2 if link else 1}",\n'
        f'\t"{_q(link) if link else "null"}",\n'
        f'\t"{_q(item["date"])}",\n'
        f'\t"{_q(item["title"])}",\n'
        f'\t"{CONTENTS[item["content"]]}",\n'
        f'\t"{cat}"\n'
        '</ARRAY>\n'
        '<DATA NAME="BODY" SUB="ENT">\n'
        f'\t<RECORD>"{_body_ent(item.get("body"))}"</RECORD>\n'
        '</DATA>\n')


# --------------------------------------------------------------------------- #
# news<N>.pml -- the data behind SE's real Information section
# --------------------------------------------------------------------------- #
_ROW_RE = re.compile(r'^\s*<ARRAY>\s*("(?:[^"]*)"\s*(?:,\s*"(?:[^"]*)"\s*)*)</ARRAY>\s*$')


def parse_narray(text):
    """Pull the five $NARRAY category buckets out of an existing news<N>.pml.

    Deliberately lenient and deliberately narrow: it only needs to recover SE's
    archived rows so a republish can put ours back on top of them. Anything it
    fails to understand it drops, and the caller keeps a `.se-orig` copy.
    """
    if not text or "$NARRAY" not in text:
        return None
    seg = text.replace("\r\n", "\n").split('<ARRAY NAME="$NARRAY">', 1)[1]
    cats, cur = [], None
    for line in seg.splitlines():
        s = line.strip()
        if s == "<ARRAY>":
            cur = []
            continue
        if s == "</ARRAY>":
            if cur is None:
                break                       # closes $NARRAY itself
            cats.append(cur)
            cur = None
            continue
        m = _ROW_RE.match(s)
        if m and cur is not None:
            fields = re.findall(r'"([^"]*)"', m.group(1))
            if any(fields):                 # skip SE's terminator row
                cur.append(fields)
    while len(cats) < len(CATEGORIES):
        cats.append([])
    return cats[:len(CATEGORIES)]


def _is_ours(fields):
    """True for a row this generator wrote on a previous run."""
    try:
        return int(fields[1]) >= SERIAL_BASE
    except (IndexError, ValueError):
        return False


def news_file(items, content_id, existing=None):
    """One news<N>.pml.

    `content_id` 0 is SE's "View All"; 1-5 are the entries of $cnt_d. An item
    appears in news<N> when its own content is N, and content-1 (PlayOnline)
    items appear everywhere -- the rule MEASURED off SE's own files, where
    news2.pml holds exactly the ci-1 and ci-2 rows and news3.pml exactly ci-1
    and ci-3. (SE's own news1.pml is looser than its own rule, carrying a few
    ci-2 and ci-3 rows; we follow the rule, not the stray data.)

    `existing` is the current file's text, if any: SE's archived rows are kept
    and only our own previous rows are replaced.
    """
    keep = parse_narray(existing) or [[] for _ in CATEGORIES]
    keep = [[r for r in bucket if not _is_ours(r)] for bucket in keep]

    mine = [[] for _ in CATEGORIES]
    for it in items:
        cid = CONTENTS[it["content"]]
        if content_id not in (0, cid) and cid != CONTENTS["playonline"]:
            continue
        cat, fields = _narray_fields(it)
        mine[cat].append(fields)

    buckets = [mine[i] + keep[i] for i in range(len(CATEGORIES))]

    out = ['<META http-equiv="Cache-Control" content="no-cache">',
           '<ARRAY NAME="$NARRAY">']
    for bucket in buckets:
        out.append("<ARRAY>")
        out.extend(_record(r) for r in bucket)
        out.append(_TERMINATOR)
        out.append("</ARRAY>")
        out.append("")
    out.append("</ARRAY>")

    # $NEW_DATE / $NEW_FLAG / $NUMSUB, one entry per category. $NUMSUB counts
    # rows with $ar_pr != 2 (see the module docstring) -- we never emit a child
    # row, but SE's archived ones do, so filter rather than take len().
    new_date, new_flag, numsub = [], [], []
    for i, bucket in enumerate(buckets):
        non_child = [r for r in bucket if len(r) < 8 or r[7] != "2"]
        numsub.append(str(len(non_child)))
        new_date.append(_q(bucket[0][4]) if bucket else "")
        # The NEW badge: set when the top row of the bucket is one of ours,
        # which is the only "recent" we can assert -- SE's dates are free text
        # that nothing in the client ever parsed.
        new_flag.append("1" if bucket and _is_ours(bucket[0]) else "0")
    out.append('<ARRAY NAME="$NEW_DATE">'
               + ",".join(f'"{d}"' for d in new_date) + "</ARRAY>")
    out.append('<ARRAY NAME="$NEW_FLAG">'
               + ",".join(f'"{f}"' for f in new_flag) + "</ARRAY>")
    out.append('<ARRAY NAME="$NUMSUB">'
               + ",".join(f'"{n}"' for n in numsub) + "</ARRAY>")

    # The Status / Maintenance panel (in05.pml). Opt-in per announcement, so an
    # old outage does not sit there forever claiming to be current.
    mnt, trb = [], []
    for it in items:
        if not it.get("status"):
            continue
        cid = CONTENTS[it["content"]]
        if content_id not in (0, cid) and cid != CONTENTS["playonline"]:
            continue
        _cat, fields = _narray_fields(it)
        (mnt if fields[9] == "3" else trb if fields[9] == "2" else []).append(fields)

    out.append('<ARRAY name="$MNT_INFO">')
    out.extend(_record(r) for r in mnt)
    out.append(_TERMINATOR)
    out.append("</ARRAY>")
    out.append('<ARRAY name="$TRB_INFO">')
    out.extend(_record(r) for r in trb)
    out.append(_TERMINATOR)
    out.append("</ARRAY>")
    out.append(f'<ARRAY name="$NUMINFO">"{len(mnt)}","{len(trb)}"</ARRAY>')
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# info.playonline.com/snews/<loc>/index.pml -- our own substitute page
# --------------------------------------------------------------------------- #
#: Body text is wrapped to this many columns. The Information page's textbox is
#: 608px wide at style Bw15; ~76 columns fills it without hitting the edge.
WRAP = 76


def _doc_text(items):
    """The substitute page's scrolling document.

    `&pre=1;` puts the record in preformatted mode, so our own wrapping is what
    the reader sees. `&style=NAME; ... &style;` is a span. Both are SE's inline
    syntax, from the Western registration screens' <data> records. Bare text in
    a record renders as nothing, so everything sits inside a style span.
    """
    out = ["&pre=1;&style=Bw15;"]
    for it in items:
        out.append(f"&style=Sc16;{_q(it['date'])}&style;")
        out.append(f"&style=Hw18;{_q(it['title'])}&style;")
        body = (it.get("body") or "").strip()
        if body:
            out.append("")
            for para in body.split("\n\n"):
                para = " ".join(para.split())
                out.extend(textwrap.wrap(para, WRAP) or [""])
                out.append("")
        else:
            out.append("")
        out.append("-" * WRAP)
        out.append("")
    out.append("&style;")
    return "\n".join(out).rstrip()


#: Where the Back button goes. SE's own Information page uses `$ARG9`, the
#: caller-supplied return URL, defaulting to `toviewer:`. This page is
#: POL_SERVERINFO_PAGE_URL, which the client fetches AROUND LOGIN, so the login
#: screen is the sane destination; override if you reach it from the main menu.
BACK_HREF = os.environ.get("POL_NEWS_BACK", "tologin:")

#: Registration, over PLAIN HTTP and therefore needing no certificate. The
#: hardcoded `https:` (app.dll RVA 0x3cf920) governs only the NATIVE startup
#: wizard's URL builder; a link in a page WE serve is followed as written.
SIGNUP_URL = os.environ.get(
    "POL_SIGNUP_URL", "http://ucs.pol.com:8080/pml-cgi-bin/?kinou_id=20")


def snews_page(items):
    """The substitute Information page (POL_SERVERINFO_PAGE_URL).

    Layout is SE's, transcribed from `wh000.pol.com/pml/info/index.pml` and its
    `in01.pml` include: the `shbt07` back button (a `bt03s.ang` plate at x=-35
    hanging off the left edge, with a transparent `im01s.png` caption image over
    it carrying the href), the two-image button idiom, the centred empty-state
    line, and the `bg02s.png` backdrop that SE's `$BACKIMAGE[0]` resolves to.

    Served from www/ by the portal handler, NOT by ucscgi, so it cannot use the
    /ucs/img_s/ art that service publishes. Every image here is client-local
    `file:/...` and confirmed present in viewer/data/pmlus.
    """
    # The <data> record goes in <head>, NOT the body: SE's own pages pull theirs
    # in with an <include> inside <head>, and a record declared in the body is
    # not found by the textbox that references it -- the page then renders its
    # chrome and an empty box.
    doc = (f'\t<data name="dt_news" sub="doc">\n<record>\n"\n'
           f'{_doc_text(items)}\n"\n</record>\n</data>\n') if items else ""
    if items:
        body = (
            '<sheet name="shbody" pos="0,60" size="640,310" border="0" '
            'alpha="1" appeartime="200" delay="0">\n'
            # skin/skincolor copied from SE's own working textbox in
            # pml/game/ff11/id/idpm01.pml. skin="0" renders NOTHING.
            '\t<textbox name="news" pos="20,0" size="600,306" style="Bw15" '
            'margin="4,2,2,2" ref="dt_news" sub="doc" index="0" skin="11" '
            'skincolor="#8C8BA9FF" selectedskincolor="#00000011">\n'
            '</sheet>\n')
    else:
        body = ('<sheet name="shbody" pos="0,60" size="640,310" border="0" '
                'alpha="1" appeartime="200" delay="0">\n'
                '\t<text pos="20,140" size="600,20" style="Bw15" align="center" '
                'valign="middle">There is no news at this time.</text>\n'
                '</sheet>\n')
    return f"""<pml>
<head>
\t<meta http-equiv="Content-Type" content="text/x-playonline-pml;charset=UTF-8">
\t<title>Information</title>
\t<style name="Bw216"   face="6" size="21" proportional="1" spacing="1" color="#ffffffff,#332211ff">
\t<style name="Sc16"    face="6" size="16" proportional="1" spacing="1" color="#ccdd99ff,#332211ff">
\t<style name="Hw18"    face="6" size="18" proportional="1" spacing="2" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="Bw15"    face="6" size="15" proportional="1" vspacing="4" color="#ffffffff,#000000ff">
\t<style name="Bw15b_2" face="6" size="15" proportional="1" bold="1" color="#ffffffff,#000000ff" onmousecolor="#000000ff,#ffffffff" selectedcolor="#ffffffff,#000000ff">
{doc}</head>
<body background="file:/game/img_s/bg02s.png">

<!-- title. The SE original sits at 0,45 on an im06s.png plate the US client
     does not ship, so the plate is dropped and the label kept in place.
     NO APOSTROPHES IN A PML COMMENT: an unbalanced quote kills the parse from
     that line down, silently (PML-AUTHORING.md section 1). This page shipped
     with two of them from its first version until 2026-08-20. -->
<sheet name="shtl01" pos="0,45" size="360,48" border="0" type="4" delay="0" wait="2" appeartime="200" alpha="1">
\t<text name="tx01" pos="24,1" size="330,34" style="Bw216" valign="middle">Information</text>
</sheet>

{body}
<!-- Create an account. Plain HTTP on purpose -- see SIGNUP_URL. -->
<sheet name="shbt06" pos="0,343" size="252,42" border="0" type="4" delay="0" wait="0" appeartime="200">
\t<img name="bt_06" pos="-35,0" size="226,44" src="file:/help/s_info/bt03s.ang">
\t<img name="nw_new1" size="180,34" src="file:/img_s/general/im01s.png"
\t\t value="  Create an ID" style="Bw15b_2"
\t\t onmouseover="sd:sequence=3@bt_06"
\t\t onmouseout="sd:sequence=0@bt_06"
\t\t href="{SIGNUP_URL}"
\t\t alt="^03Create a PlayOnline ID on this server.">
</sheet>

<!-- Back. The SE original is shbt07; verbatim geometry, plate at -35 inside a
     sheet at x=0. -->
<sheet name="shbt07" pos="0,385" size="252,42" border="0" type="4" delay="0" wait="0" appeartime="200">
\t<img name="bt_07" pos="-35,0" size="226,44" src="file:/help/s_info/bt03s.ang">
\t<img name="nw_ret1" size="180,34" src="file:/img_s/general/im01s.png"
\t\t value="     Back" style="Bw15b_2"
\t\t onmouseover="sd:sequence=3@bt_07"
\t\t onmouseout="sd:sequence=0@bt_07"
\t\t href="{BACK_HREF}"
\t\t alt="^03Return to the previous menu.">
</sheet>

</body>
</pml>
"""


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
#: The ONLY paths publish() may write. The admin container gets /www read-write
#: so the News tab can publish, and this is what keeps that mount from being a
#: general-purpose portal overwrite. Note the serial shape is `9[0-9]{5}` -- SE's
#: captured articles are 4-digit, so they are unreachable by construction.
_ALLOW = (
    # news<N> takes TWO digits: the content ids are SE's sequence numbers and
    # run to 12, and SE's own ja-JP tree already ships a news10.pml. A
    # single-digit pattern here refused to publish ids 10-12 -- correctly, it
    # writes nothing rather than a partial tree, but the ceiling was ours.
    re.compile(r"^wh000\.pol\.com/pcd/ntool/[A-Za-z0-9_.-]+/"
               r"(?:latestnews|news[0-9]{1,2}|9[0-9]{5})\.pml$"),
    re.compile(r"^wh000\.pol\.com/pml/info/news0\.pml$"),
    re.compile(r"^info\.playonline\.com/snews/[A-Za-z0-9_.-]+/index\.pml$"),
)


def allowed(rel):
    rel = rel.replace(os.sep, "/")
    return any(p.match(rel) for p in _ALLOW)


def outputs(items, www=None):
    """Every file a publish would write: {relative path: text}.

    Built by reading the CURRENT tree, because news<N>.pml merges onto SE's
    archive -- so this is a preview of the real result, not an approximation.
    """
    www = www or WWW_DIR
    out = {}
    feed_text = feed(items)
    for loc in FEED_LOCALES:
        base = f"wh000.pol.com/pcd/ntool/{loc}"
        out[f"{base}/latestnews.pml"] = feed_text
        # 0 (View All) plus one page per content id. Derived from CONTENTS so
        # adding a service cannot leave its news<N>.pml unwritten -- the bug the
        # hardcoded range(6) would have caused the moment ids 6/7 were added.
        for cid in range(max(CONTENTS.values()) + 1):
            rel = f"{base}/news{cid}.pml"
            existing = None
            fs = os.path.join(www, rel.replace("/", os.sep))
            if os.path.isfile(fs):
                with open(fs, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            out[rel] = news_file(items, cid, existing)
        for it in items:
            out[f"{base}/{it['serial']}.pml"] = detail(it)

    # index.pml takes this relative copy for fr-FR / de-DE, which have no
    # /pcd/ntool/<lang>/ tree of their own. Our client has asked for it.
    rel = "wh000.pol.com/pml/info/news0.pml"
    fs = os.path.join(www, rel.replace("/", os.sep))
    existing = None
    if os.path.isfile(fs):
        with open(fs, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
    out[rel] = news_file(items, 0, existing)

    page_text = snews_page(items)
    for loc in PAGE_LOCALES:
        out[f"info.playonline.com/snews/{loc}/index.pml"] = page_text
    return out


def stale_details(items, www=None):
    """Detail files we wrote for announcements that no longer exist.

    Restricted to `9xxxxx.pml` by the same rule as the writer, so a captured SE
    article can never be swept up by this.
    """
    www = www or WWW_DIR
    live = {str(it["serial"]) for it in items}
    gone = []
    for loc in FEED_LOCALES:
        d = os.path.join(www, "wh000.pol.com", "pcd", "ntool", loc)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = re.fullmatch(r"(9[0-9]{5})\.pml", name)
            if m and m.group(1) not in live:
                gone.append(f"wh000.pol.com/pcd/ntool/{loc}/{name}")
    return sorted(gone)


def publish(items, www=None, prune=True, dry_run=False):
    """Render and write. Returns {written, skipped, pruned, backed_up}.

    Every first replacement of an existing file leaves a `.se-orig` sibling, the
    convention already used for latestnews.pml, so SE's captured bytes are
    always one copy away.
    """
    www = www or WWW_DIR
    files = outputs(items, www)
    written, skipped, backed_up = [], [], []
    for rel, text in sorted(files.items()):
        if not allowed(rel):
            skipped.append(rel)                       # unreachable by construction
            continue
        fs = os.path.join(www, rel.replace("/", os.sep))
        if os.path.isfile(fs):
            with open(fs, "r", encoding="utf-8", errors="replace") as f:
                if f.read() == text:
                    continue                          # no churn, no mtime bump
            orig = fs + ".se-orig"
            if not os.path.exists(orig) and not dry_run:
                with open(fs, "rb") as src, open(orig, "wb") as dst:
                    dst.write(src.read())
                backed_up.append(rel)
        if not dry_run:
            os.makedirs(os.path.dirname(fs), exist_ok=True)
            tmp = fs + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, fs)
        written.append(rel)

    pruned = []
    if prune:
        for rel in stale_details(items, www):
            if not allowed(rel):
                continue
            if not dry_run:
                try:
                    os.remove(os.path.join(www, rel.replace("/", os.sep)))
                except OSError:
                    continue
            pruned.append(rel)
    return {"written": written, "skipped": skipped, "pruned": pruned,
            "backed_up": backed_up, "total": len(files)}
