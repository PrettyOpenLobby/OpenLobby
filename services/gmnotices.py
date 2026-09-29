"""GM notices: the list the Viewer's GM Call pages show, and the file it reads.

Service & Support > GM Call > Next (cs/gm/gmpm02.pml, "GM Notices") includes
`/pcd/gmcall/<lang>/data.pml` and draws its `$CONTENTS` array. That file used
to be SE's captured bytes ("No notices at present."); it is now rendered from
a store the GM desk edits, the way the News feed is (newsgen.py), and like the
news it is NOT tracked in git: an edit on prod would otherwise leave the
checkout dirty and stall pol-git-sync.

The shape, read from SE's own files and from gmpm02.pml's loops:

    $CONTENTS[0][0]            number of sections
    $CONTENTS[i][0]            [count, section title]
    $CONTENTS[i][m]            [title, hover text, link count, link 1, link 2,
                                icon, has-detail]
        icon       ps0 normal, ps1 updated, ps2 new; 99 draws none
        has-detail 1 opens a sheet whose text is record m-1 of DETAIL<i>
    $shNUM                     the detail sheets' numbers, 1..N in order

An empty section shows SE's gray "No notices at present." row.
"""
import copy
import json
import os
import threading

TICKET_DIR = os.environ.get("POL_GMD_TICKET_DIR", "/data/gm-calls")
STORE = os.environ.get("POL_GM_NOTICES_STORE") or os.path.join(TICKET_DIR, "gm-notices.json")
REL = {"en-US": "wh000.pol.com/pcd/gmcall/en-US/data.pml",
       "ja-JP": "wh000.pol.com/pcd/gmcall/ja-JP/data.pml"}
#: SE's own sections and empty-section wording, per locale.
DEFAULT = {
    "en-US": {"empty": "No notices at present.",
              "sections": [{"title": "Notice from the Game Masters", "items": []}]},
    "ja-JP": {"empty": "現在お知らせはありません。",
              "sections": [{"title": "既知の不具合・状況", "items": []},
                           {"title": "調査中", "items": []},
                           {"title": "ゲームマスターからのお知らせ", "items": []}]},
}
MARKS = {"normal": "0", "updated": "1", "new": "2"}
TITLE_MAX, HOVER_MAX, BODY_MAX, ITEMS_MAX = 80, 120, 2000, 20
_LOCK = threading.Lock()


class NoticeError(ValueError):
    pass


def load():
    """The store, or SE's empty defaults. Never raises."""
    data = copy.deepcopy(DEFAULT)
    try:
        with open(STORE, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return data
    for loc in DEFAULT:
        secs = ((got or {}).get(loc) or {}).get("sections")
        if isinstance(secs, list):
            for i, sec in enumerate(data[loc]["sections"]):
                if i < len(secs) and isinstance(secs[i], dict):
                    sec["items"] = [it for it in secs[i].get("items") or []
                                    if isinstance(it, dict)]
    return data


def validate(data):
    """Clean and check an edited store. Sections are SE's and fixed; only their
    notices change."""
    out = copy.deepcopy(DEFAULT)
    for loc in DEFAULT:
        secs = ((data or {}).get(loc) or {}).get("sections") or []
        for i, sec in enumerate(out[loc]["sections"]):
            items = (secs[i].get("items") if i < len(secs) and isinstance(secs[i], dict)
                     else []) or []
            if len(items) > ITEMS_MAX:
                raise NoticeError(f"at most {ITEMS_MAX} notices per section")
            for it in items:
                title = str((it or {}).get("title") or "").strip()
                if not title:
                    raise NoticeError("every notice needs a title")
                mark = str(it.get("mark") or "normal")
                if mark not in MARKS:
                    raise NoticeError("mark must be normal, updated or new")
                sec["items"].append({
                    "title": title[:TITLE_MAX],
                    "hover": str(it.get("hover") or "").strip()[:HOVER_MAX],
                    "body": str(it.get("body") or "").strip()[:BODY_MAX],
                    "mark": mark})
    return out


def save(data):
    data = validate(data)
    with _LOCK:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STORE)
    return data


def _field(s):
    """One quoted PML field. Quotes and apostrophes end or confuse a string --
    the whole Service & Support menu once stopped drawing over one apostrophe --
    so they, and `&`, go in as entities, and a line break is `&br;`. SE's pages
    use `&amp;`, `&#34;` and `&#39;`; an unknown NAMED entity blanks the page
    (PML-AUTHORING 5), so angle brackets go in by number."""
    s = str(s).replace("&", "&amp;").replace('"', "&#34;").replace("'", "&#39;")
    s = s.replace("<", "&#60;").replace(">", "&#62;")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "&br;")


def render(loc, data):
    """The data.pml for one locale."""
    cfg = data[loc]
    empty = _field(cfg.get("empty") or DEFAULT[loc]["empty"])
    lines = ['<META http-equiv="Cache-Control" content="no-cache">',
             '<ARRAY name="$CONTENTS">',
             f'\t<ARRAY>"{len(cfg["sections"])}"</ARRAY>']
    details, n_detail = [], 0
    for sec in cfg["sections"]:
        items = sec.get("items") or []
        lines.append("\t<ARRAY>")
        lines.append(f'\t\t<ARRAY>"{max(1, len(items))}","{_field(sec["title"])}"</ARRAY>')
        recs = []
        if not items:
            lines.append(f'\t\t<ARRAY>"&style=gray;{empty}&style;","{empty}",'
                         f'"0","","","99","0"</ARRAY>')
        for it in items:
            has = 1 if it.get("body") else 0
            n_detail += has
            lines.append(
                f'\t\t<ARRAY>"{_field(it["title"])}","{_field(it.get("hover") or it["title"])}",'
                f'"0","","","{MARKS.get(it.get("mark"), "0")}","{has}"</ARRAY>')
            recs.append(_field(it.get("body") or ""))
        lines.append("\t</ARRAY>")
        details.append(recs)
    lines.append("</ARRAY>")
    lines.append('<ARRAY name="$shNUM">'
                 + ",".join(f'"{i}"' for i in range(1, n_detail + 1)) + "</ARRAY>")
    for i, recs in enumerate(details, 1):
        lines.append(f'<DATA name="DETAIL{i}" sub="detail{i}">')
        lines.extend(f'<RECORD>"{r}"</RECORD>' for r in recs)
        lines.append("</DATA>")
    return "\r\n".join(lines) + "\r\n"


def encode(loc, text):
    """The bytes SE served: CRLF, and Shift-JIS for Japanese. English stays
    plain ASCII while it can (SE's own file is), else UTF-8 with a BOM, which
    the Viewer sniffs. A character Shift-JIS lacks goes in as a number."""
    if loc == "ja-JP":
        return text.encode("cp932", errors="xmlcharrefreplace")
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        return b"\xef\xbb\xbf" + text.encode("utf-8")


def publish(www, data=None):
    """Write both locales' files under `www`. Returns the paths written.

    The first replacement of SE's own file keeps it as `.se-orig`, as the
    news does."""
    data = data or load()
    written = []
    for loc, rel in REL.items():
        fs = os.path.join(www, rel.replace("/", os.sep))
        raw = encode(loc, render(loc, data))
        if os.path.isfile(fs):
            with open(fs, "rb") as f:
                if f.read() == raw:
                    continue
            orig = fs + ".se-orig"
            if not os.path.exists(orig):
                with open(fs, "rb") as src, open(orig, "wb") as dst:
                    dst.write(src.read())
        os.makedirs(os.path.dirname(fs), exist_ok=True)
        tmp = fs + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, fs)
        written.append(rel)
    return written


def ensure(www):
    """Write the files if they are missing (a fresh checkout: they are not in
    git). Never overwrites, never raises."""
    try:
        if all(os.path.isfile(os.path.join(www, r.replace("/", os.sep))) for r in REL.values()):
            return []
        return publish(www)
    except Exception:
        return []
