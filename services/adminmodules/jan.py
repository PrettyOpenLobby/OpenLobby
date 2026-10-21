"""JongHoLow (Janhourou). Tables run in the jan service; the sign-in side is
in authsess, whose marker counts the players.

Actions post to the same live document HippaulHoLo's operator tool does
(janworld.gmnotice, tools/jan_gmnotice.py): the key jan:gm-notice holds
{"seq", "posts": [{"seq", "op": open|close|kick, "text", "member", "at",
"until"}]}, and every Jan client picks up the posts meant for it on its next
line. A 2004 client shows a notice in its GM window for two minutes; a kick
sends it out of the title. The 2002 client ignores both."""
import json
import os
import threading
import time

from adminmodules import Action, Module, Service, sources

#: as janworld.gmnotice: how long a post stays deliverable, how many are kept
OPEN_TTL_S = 120
KICK_TTL_S = 600
KEEP = 32
_LOCK = threading.Lock()


def _key():
    return os.environ.get("POL_JAN_GMNOTICE_KEY", "").strip() or "jan:gm-notice"


def _post(op, text="", member=0, ttl=None):
    from polcore import kv
    now = time.time()
    ttl = ttl or (KICK_TTL_S if op == "kick" else OPEN_TTL_S)
    key = _key()
    with _LOCK:
        raw = kv.get(key)
        try:
            doc = json.loads(raw) if raw else {}
        except ValueError:
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        seq = int(doc.get("seq") or 0) + 1
        posts = [p for p in (doc.get("posts") or []) if isinstance(p, dict)]
        posts.append({"seq": seq, "op": op, "text": str(text or ""), "member": int(member),
                      "at": now, "until": now + float(ttl)})
        kv.set(key, json.dumps({"seq": seq, "posts": posts[-KEEP:]}, separators=(",", ":")))
        kv.incr(key + ":gen")
    return seq


def _members(polid="", handles=""):
    """Member ids for a POL ID and/or handles (comma separated)."""
    import accounts
    names = [h.strip() for h in str(handles or "").split(",") if h.strip()]
    db = accounts.connect()
    try:
        ids = set()
        if polid:
            ids |= {r["id"] for r in db.execute(
                "SELECT id FROM member WHERE polid = %s", (str(polid).strip().upper(),)).fetchall()}
        for n in names:
            ids |= {r["member_id"] for r in db.execute(
                "SELECT member_id FROM handle WHERE lower(handle_name) = lower(%s)", (n,)).fetchall()}
        return sorted(ids)
    finally:
        db.close()


def _notice(m, params, actor):
    text = " ".join(str(params.get("text") or "").split())
    if not text:
        return False, "A notice needs text."
    seq = _post("open", text[:200])
    return True, "Notice #%d posted to every JongHoLow player." % seq


def _close(m, params, actor):
    _post("close")
    return True, "The notice window closes for every JongHoLow player."


def _kick(m, params, actor):
    ids = _members(params.get("polid"), params.get("handles"))
    if not ids:
        return False, "No JongHoLow player by that name."
    for mid in ids:
        _post("kick", member=mid)
    return True, "Kick posted for %d member%s; it lands on their next move." % (
        len(ids), "" if len(ids) == 1 else "s")


def _parse(doc, st):
    st["rooms_label"] = "tables"
    if isinstance(doc.get("members"), int):
        st["facts"].append({"label": "ranked members", "value": doc["members"]})


MODULE = Module(
    "jan", "JongHoLow", platform="PlayOnline: PS2", content_ids=[3],
    markers=["authsess-jan"], self_described=True,   # its board's janadmin.py speaks the protocol
    actions=[
        Action("notice", "GM notice", _notice, kind="announce",
               fields=[{"id": "text", "label": "Message", "type": "text"}],
               about="Opens the GM notice window for every player at a table (the 2004 client)."),
        Action("close", "Close the notice", _close,
               about="Closes the GM notice window again."),
        Action("kick", "Kick a player", _kick, kind="kick", danger=True,
               fields=[{"id": "handles", "label": "Handle", "type": "text"}],
               about="Sends the player out of JongHoLow on their next move."),
    ],
    services=[Service(["{core}-jan-1", "{core}-board-holo-1|{core}-janboard-1"], {51272: "tables"},
                      warning="Anyone at a JongHoLow table loses the game.")],
    live=sources.board(_parse, markers=["authsess-jan"]),
)
