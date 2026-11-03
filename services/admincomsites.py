"""News > Community sites: fan sites registered for the Community Site Tool.

The Community Site Tool is the 480x100 banner a fan site embeds with
comTool('<id>'): the site's name, its game's news, a featured and a random
community site, and a Q&A search, drawn by a script that POL_COMTOOL_URL's site
serves. Each site needs a record; this module keeps them (community_site,
migration 0026) and writes the code the site's owner pastes in.

POL_COMTOOL_URL unset means no site serves the banner, and the panel leaves
the page out (enabled() is False). The site that serves it reads the same
table, so a record saved here reaches the banner within a minute or so.
"""
import os
import re
import time

#: the site that serves the banner script (/comtool/comTool), e.g. https://lobby.example.org
URL = (os.environ.get("POL_COMTOOL_URL") or "").strip().rstrip("/")
#: the games a site can be about, as the news file names them, with the skin
#: each starts on when the record names none (skins 1-3 were drawn for FFXI,
#: 4-6 for Front Mission Online, 7-11 for Fantasy Earth, 0 for PlayOnline; 12-18
#: are custom, made in the same layout for the games that never had one)
GAMES = {"playonline": ("PlayOnline", 0), "ffxi": ("Final Fantasy XI", 1),
         "fe": ("Fantasy Earth", 7), "fmo": ("Front Mission Online", 4),
         "tetra": ("Tetra Master", 12), "jan": ("JongHoLow", 14),
         "doc": ("Dirge of Cerberus", 16)}
SKINS = 19
#: the skins that are not Square Enix's own: 12-13 Tetra Master (the second violet),
#: 14-15 JongHoLow (the second amber), 16-18 Dirge of Cerberus (then Deepground, multiplayer)
CUSTOM_SKINS = (12, 13, 14, 15, 16, 17, 18)
#: added to the skin pictures' addresses, so a CDN in front of the banner's site shows
#: a changed picture at once; bump it with the banner script's own V (comTool.js)
ASSET_VERSION = "20261025d"
_COLS = ("id", "name", "url", "game", "skin", "about", "featured", "listed",
         "owner_polid", "note", "made_by", "created_at", "updated_at")
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)


def enabled():
    return bool(URL)


def info():
    """What the page needs besides the rows: where the banner comes from, the
    games and their starting skins."""
    return {"enabled": enabled(), "base": URL,
            "games": [{"key": k, "label": v[0], "skin": v[1]} for k, v in GAMES.items()],
            "skins": SKINS, "custom": list(CUSTOM_SKINS), "asset_version": ASSET_VERSION}


def embed(site_id, base=None):
    """The two ways to put the banner on a page, as the owner pastes them."""
    base = base or URL
    return {"script": '<script src="%s/comtool/comTool"></script>\n'
                      '<script>comTool(\'%s\');</script>' % (base, site_id),
            "tag": '<div class="comtool" data-site="%s"></div>\n'
                   '<script src="%s/comtool/comTool" async></script>' % (site_id, base)}


def _row(r):
    out = {k: r[k] for k in _COLS}
    out["code"] = embed(r["id"])
    return out


def list_sites(db):
    rows = db.execute("SELECT %s FROM community_site ORDER BY id DESC" % ", ".join(_COLS)).fetchall()
    return [_row(r) for r in rows]


def _clean(body):
    """(fields, None) or (None, the reason in words)."""
    name = " ".join(str(body.get("name") or "").split())
    url = str(body.get("url") or "").strip()
    game = str(body.get("game") or "playonline").strip()
    about = " ".join(str(body.get("about") or "").split())
    note = str(body.get("note") or "").strip()
    owner = str(body.get("owner_polid") or "").strip() or None
    skin = body.get("skin")
    if not 1 <= len(name) <= 60:
        return None, "Give the site a name of 1 to 60 characters."
    if len(url) > 300 or not _URL.match(url):
        return None, "The address must start with http:// or https://."
    if game not in GAMES:
        return None, "Pick one of the listed games."
    if skin in ("", None):
        skin = None
    else:
        try:
            skin = int(skin)
        except (TypeError, ValueError):
            return None, "The skin is a number from 0 to %d." % (SKINS - 1)
        if not 0 <= skin < SKINS:
            return None, "The skin is a number from 0 to %d." % (SKINS - 1)
    if len(about) > 200:
        return None, "Keep the directory line under 200 characters."
    if len(note) > 500:
        return None, "Keep the note under 500 characters."
    return {"name": name, "url": url, "game": game, "skin": skin, "about": about,
            "featured": bool(body.get("featured")), "listed": body.get("listed", True) is not False,
            "owner_polid": owner, "note": note}, None


def save(db, body, by):
    """Add a site (no id) or change one. -> (status, answer)."""
    f, err = _clean(body)
    if err:
        return 400, {"error": err}
    if f["owner_polid"] and not db.execute(
            "SELECT 1 FROM polid WHERE polid = %s", (f["owner_polid"],)).fetchone():
        return 400, {"error": "No account has that PlayOnline ID."}
    now = time.time()
    sid = body.get("id")
    if sid not in (None, ""):
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return 400, {"error": "Unknown site."}
        n = db.execute(
            "UPDATE community_site SET name=%s, url=%s, game=%s, skin=%s, about=%s, featured=%s,"
            " listed=%s, owner_polid=%s, note=%s, updated_at=%s WHERE id=%s",
            (f["name"], f["url"], f["game"], f["skin"], f["about"], f["featured"], f["listed"],
             f["owner_polid"], f["note"], now, sid)).rowcount
        if not n:
            return 404, {"error": "That site is gone."}
    else:
        # never a number handed out before (migration 0026)
        sid = db.execute("SELECT nextval('community_site_id') AS n").fetchone()["n"]
        db.execute(
            "INSERT INTO community_site (id, name, url, game, skin, about, featured, listed,"
            " owner_polid, note, made_by, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (sid, f["name"], f["url"], f["game"], f["skin"], f["about"], f["featured"], f["listed"],
             f["owner_polid"], f["note"], by, now, now))
    db.commit()
    row = db.execute("SELECT %s FROM community_site WHERE id = %%s" % ", ".join(_COLS),
                     (sid,)).fetchone()
    return 200, _row(row)


def delete(db, sid):
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return 400, {"error": "Unknown site."}
    n = db.execute("DELETE FROM community_site WHERE id = %s", (sid,)).rowcount
    db.commit()
    if not n:
        return 404, {"error": "That site is gone."}
    return 200, {"ok": True, "id": sid}


def public_rows(db):
    """The fields the banner's site may show, for the site that serves it."""
    rows = db.execute("SELECT id, name, url, game, skin, about, featured, listed"
                      " FROM community_site ORDER BY id").fetchall()
    return [{k: r[k] for k in ("id", "name", "url", "game", "skin", "about", "featured", "listed")}
            for r in rows]
