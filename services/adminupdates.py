"""Update notices for the admin panel: whether the core and each game run the newest version of
their code, and what changed since. Every item has one shape (docs/deployer.md, "Updates"):

    {"key", "name", "running": {"commit", "built_at"}, "latest": {"commit", "date"},
     "behind", "changes": [{"commit", "date", "subject"}],
     "update": "available" | "current" | "unknown", "why", "how"}

overview() puts together, for each key, the first of:

  1. the deployer's item (GET /api/updates, admindeploy.updates()), when a deployer is set up
     and answers;
  2. for a game the deployer has no item for, the game's own report: the "update" object of
     its status JSON (docs/admin-modules.md), read with the module's live status;
  3. for the core, when no deployer answers, the panel's own check: the revision its image was
     built from (OPENLOBBY_REVISION and OPENLOBBY_BUILT_AT, set by the image build) against the
     newest commits on GitHub.

Settings for the panel's own check:

    POL_ADMIN_UPDATE_REPO    the repository, owner/name (default PrettyOpenLobby/OpenLobby);
                             "off" for a server that updates itself some other way, which
                             leaves the core out of the list
    POL_ADMIN_UPDATE_BRANCH  its branch (default main)
    POL_ADMIN_UPDATE_HOW     the sentence on how to update (default: run docker compose pull and
                             docker compose up -d)
    POL_ADMIN_GITHUB_API     the GitHub API (default https://api.github.com; tests use a stand-in)

GitHub is asked without a token and its answers are kept an hour; a forced check (refresh) is
made at most once a minute. Whatever a game or the deployer sends is cleaned by clean_item()
before it is shown: strings trimmed, commits hex, times ISO 8601 UTC, at most 50 changes, and a
report that does not fit becomes "unknown" with a sentence saying so.
"""
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.join(HERE, "deployer") not in sys.path:
    # the check itself is shared with the deployer, whose image holds only services/deployer/
    sys.path.append(os.path.join(HERE, "deployer"))

import admindeploy  # noqa: E402
import updatecheck  # noqa: E402

DEFAULT_REPO = "PrettyOpenLobby/OpenLobby"
DEFAULT_HOW = "Run docker compose pull and docker compose up -d."
CORE_NAME = "OpenLobby core"
REFRESH_GAP = 60.0       # at most one forced check of GitHub this often
FAIL_TTL = 300.0         # an "unknown" self-check is reused this long (unless forced)
MAX_ITEMS = 100
UPDATES = ("available", "current", "unknown")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_lock = threading.Lock()
_state = {"gh": None, "api": None, "forced": 0.0, "core": None, "core_at": 0.0, "core_env": None}


def _text(v, limit):
    if not isinstance(v, str):
        return None
    t = " ".join(v.split())[:limit]
    return t or None


def _count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 9 else None


def clean_item(raw, key=None, name=None):
    """An item fit to show, made from whatever a deployer or a game sent. `key` and `name`, when
    given, are the panel's own for the game and win over the report's. None when there is no
    usable key; a report that does not fit the shape is "unknown" with a sentence saying so.

    A game that applies its own updates (docs/installs.md, "Updates") keeps, whatever the rest
    says: "apply" (true when it takes POST /openlobby/update), "state" ("updating" with a
    "message", or "failed" with the reason in "why")."""
    it = _clean_item(raw, key, name)
    if it is None or not isinstance(raw, dict):
        return it
    if raw.get("apply") is True:
        it["apply"] = True
    st = raw.get("state")
    if st == "updating":
        it["state"] = "updating"
        it["message"] = _text(raw.get("message"), 300)
    elif st == "failed":
        it["state"] = "failed"
        why = _text(raw.get("why"), 300)
        if why and (it["update"] != "unknown" or not it["why"]):
            it["why"] = why
        it["why"] = it["why"] or "%s says its update failed, without saying why." % it["name"]
    return it


def _clean_item(raw, key, name):
    if key is None:
        key = raw.get("key") if isinstance(raw, dict) else None
    if not isinstance(key, str) or not _KEY.match(key):
        return None
    name = _text(name, 80) or (_text(raw.get("name"), 80) if isinstance(raw, dict) else None) or key
    it = updatecheck.item(key, name)
    bad = "The update report for %s does not have the expected shape, so it is not shown." % name
    if not isinstance(raw, dict):
        return updatecheck.unknown(it, bad)
    if isinstance(raw.get("module"), str) and _KEY.match(raw["module"]):
        it["module"] = raw["module"]
    if raw.get("channel") == "dev":
        it["channel"] = "dev"
    run, lat, upd = raw.get("running"), raw.get("latest"), raw.get("update")
    if upd not in UPDATES or not isinstance(run, dict) or not isinstance(lat, dict):
        return updatecheck.unknown(it, bad)
    it["running"] = {"commit": updatecheck.commit_id(run.get("commit")),
                     "built_at": updatecheck.to_iso(run.get("built_at"))}
    it["latest"] = {"commit": updatecheck.commit_id(lat.get("commit")),
                    "date": updatecheck.to_iso(lat.get("date"))}
    for given, kept in ((run.get("commit"), it["running"]["commit"]),
                        (run.get("built_at"), it["running"]["built_at"]),
                        (lat.get("commit"), it["latest"]["commit"]),
                        (lat.get("date"), it["latest"]["date"])):
        if given not in (None, "") and kept is None:
            return updatecheck.unknown(it, bad)
    behind = raw.get("behind")
    if behind is not None and _count(behind) is None:
        return updatecheck.unknown(it, bad)
    changes = raw.get("changes")
    if changes is None:
        changes = []
    if not isinstance(changes, list):
        return updatecheck.unknown(it, bad)
    out = []
    for c in changes[:500]:
        if not isinstance(c, dict):
            continue
        commit, subj = updatecheck.commit_id(c.get("commit")), _text(c.get("subject"), 200)
        if commit and subj:
            out.append({"commit": commit, "date": updatecheck.to_iso(c.get("date")), "subject": subj})
        if len(out) == updatecheck.MAX_CHANGES:
            break
    why, how = _text(raw.get("why"), 300), _text(raw.get("how"), 300)
    if upd == "unknown":
        return updatecheck.unknown(it, why or "%s cannot tell whether it is up to date." % name)
    if upd == "available" and behind == 0:
        return updatecheck.unknown(it, bad)
    it.update(behind=behind, changes=out if upd == "available" else [], update=upd, why=why,
              how=how)
    return it


# ------------------------------------------------------------ the panel's own check

def _github():
    api = os.environ.get("POL_ADMIN_GITHUB_API", "").strip().rstrip("/") or updatecheck.API
    with _lock:
        if _state["gh"] is None or _state["api"] != api:
            _state["gh"], _state["api"] = updatecheck.GitHub(api=api), api
            _state["core"] = None
        return _state["gh"]


def self_check(refresh=False):
    """The core's item from the panel itself: the image's own revision against GitHub."""
    gh = _github()
    rev = os.environ.get("OPENLOBBY_REVISION", "").strip()
    built = os.environ.get("OPENLOBBY_BUILT_AT", "").strip()
    repo = os.environ.get("POL_ADMIN_UPDATE_REPO", "").strip() or DEFAULT_REPO
    branch = os.environ.get("POL_ADMIN_UPDATE_BRANCH", "").strip() or "main"
    how = _text(os.environ.get("POL_ADMIN_UPDATE_HOW", ""), 300) or DEFAULT_HOW
    env = (rev, built, repo, branch, how)
    now = time.time()
    force = False
    with _lock:
        if refresh and now - _state["forced"] >= REFRESH_GAP:
            _state["forced"], force = now, True
        hit = _state["core"]
        if not force and hit is not None and _state["core_env"] == env \
                and hit["update"] == "unknown" and now - _state["core_at"] < FAIL_TTL:
            return dict(hit)
    it = updatecheck.item("core", CORE_NAME, rev, built)
    if not updatecheck.REPO.match(repo) or not updatecheck.REF.match(branch):
        return updatecheck.unknown(it, "POL_ADMIN_UPDATE_REPO must be owner/name and "
                                       "POL_ADMIN_UPDATE_BRANCH a branch name.")
    if rev and not it["running"]["commit"]:
        norev = "OPENLOBBY_REVISION (%s) is not a commit, so the panel cannot tell which version runs." \
            % _text(rev, 60)
    else:
        norev = ("This image was built without OPENLOBBY_REVISION, so the panel cannot tell which "
                 "version runs.")
    updatecheck.check_github(it, gh, repo, branch, force=force, no_revision=norev)
    if it["update"] == "available":
        it["how"] = how
    with _lock:
        _state["core"], _state["core_at"], _state["core_env"] = dict(it), now, env
    return it


# ------------------------------------------------------------------ games' reports

def _core_off():
    try:
        import adminmodules
        return adminmodules.core_off()
    except Exception:                          # noqa: BLE001 -- no modules here
        return os.environ.get("POL_ADMIN_CORE", "").strip().lower() == "none"


def game_reports():
    """[(key, title, the "update" object)] of the shown games whose live status carries one."""
    try:
        import adminmodules
        mods = adminmodules.enabled()
        lives = adminmodules.live_all(list(mods))
    except Exception:                          # noqa: BLE001 -- a module's fault is its own
        return []
    out = []
    for key, m in mods.items():
        st = lives.get(key)
        upd = st.get("update") if isinstance(st, dict) else None
        if upd is not None:
            out.append((key, m.title, upd))
    return out


def overview(refresh=False):
    """{"checked_at", "checking", "items"} for the panel's Updates view; refresh asks the
    deployer, or GitHub for the panel's own check, again (at most once a minute)."""
    items, keys, modules = [], set(), set()
    checked_at, checking, from_deployer = None, False, False
    channel = "stable"
    if admindeploy.configured() is not None:
        st, body = admindeploy.updates(refresh)
        if st == 200 and isinstance(body, dict) and isinstance(body.get("items"), list):
            from_deployer = True
            for raw in body["items"][:MAX_ITEMS]:
                it = clean_item(raw)
                if it is None or it["key"] in keys:
                    continue
                keys.add(it["key"])
                modules.add(it.get("module") or it["key"])
                items.append(it)
            checked_at = updatecheck.to_iso(body.get("checked_at"))
            checking = body.get("checking") is True
            channel = "dev" if body.get("channel") == "dev" else "stable"
    if ("core" not in keys and not _core_off()
            and os.environ.get("POL_ADMIN_UPDATE_REPO", "").strip().lower() != "off"):
        items.insert(0, self_check(refresh))
        keys.add("core")
        if not from_deployer:
            checked_at = updatecheck.iso()
    approved = None
    for key, title, upd in game_reports():
        if key in keys or key in modules:
            continue
        it = clean_item(upd, key=key, name=title)
        if it is None:
            continue
        keys.add(key)
        items.append(it)
        # how the panel updates it (docs/installs.md, "Updates"): the game applies it itself,
        # else the deployer runs its approved updater, else its "how" sentence is shown
        if it.get("apply"):
            it["via"] = "game"
        else:
            if approved is None:
                approved = _approved_keys() if admindeploy.configured() is not None else set()
            if key in approved:
                it["via"] = "updater"
        _observe(key, it)
    for it in items:
        it.pop("module", None)
        if it["key"] in _applying:
            it["applying"] = True
    return {"checked_at": checked_at or updatecheck.iso(), "checking": checking, "items": items,
            "channel": channel}


# ------------------------------------------------ a game that applies its own update

APPLY_TIMEOUT = 10.0     # seconds the game has to answer POST /openlobby/update
try:
    # an updating game that does not answer this long has not come back (tests shorten it)
    LOST_AFTER = float(os.environ.get("POL_ADMIN_UPDATE_LOST", "") or 600)
except ValueError:
    LOST_AFTER = 600.0
_applying = {}           # key -> {"title", "actor", "since", "seen"} while an update runs
_failed = {}             # key -> the failure last written to the activity log
_events = []             # (actor, what, key) for the activity log; take_events() drains it


def _approved_keys():
    try:
        import adminpair
        return adminpair.approved_keys()
    except Exception:                          # noqa: BLE001 -- no database: no updaters
        return set()


def _observe(key, it, actor=None):
    """Note a failure a game reports, once per reason, for the activity log."""
    with _lock:
        if it.get("state") != "failed":
            _failed.pop(key, None)
            return
        why = it.get("why") or ""
        if _failed.get(key) == why:
            return
        _failed[key] = why
        run = _applying.pop(key, None)
        _events.append(((run or {}).get("actor") or actor or "panel",
                        "%s: update failed: %s" % (it.get("name") or key, why), key))


def take_events():
    """[(actor, what, key)] seen since the last call, for admin.py to audit."""
    with _lock:
        out = list(_events)
        del _events[:]
    return out


def _title(key):
    import adminmodules
    m = adminmodules.get(key)
    if m is not None:
        return m.title
    run = _applying.get(key)
    return (run or {}).get("title") or key


def _read_status(key):
    """(the game's status document, None) or (None, a short reason), read now."""
    import json
    import urllib.request
    import adminmodules
    port = adminmodules.port_of(key)
    if port is None:
        return None, "it has no admin port here"
    url, token = port
    req = urllib.request.Request(url + "/openlobby/status.json", headers={
        "Accept": "application/json", "X-Devtool-Token": token or "", "X-Devtool-Actor": "panel"})
    try:
        with urllib.request.urlopen(req, timeout=APPLY_TIMEOUT) as r:
            doc = json.loads(r.read(1 << 20))
    except Exception as exc:                   # noqa: BLE001 -- down, restarting, not JSON
        return None, exc.__class__.__name__
    return (doc, None) if isinstance(doc, dict) else (None, "not an object")


def apply(key, actor):
    """POST /openlobby/update on the game's admin port, for the panel's Update button (path B,
    docs/installs.md "Updates"). (status, {"ok", "msg"}): 200 when the game took it (202), else
    the game's 409 or 503 with its message as given, or 502 for anything else."""
    import json
    import urllib.error
    import urllib.request
    import adminmodules
    m = adminmodules.get(key) if isinstance(key, str) and _KEY.match(key) else None
    port = adminmodules.port_of(key) if m is not None and getattr(m, "remote", False) else None
    if port is None:
        return 404, {"ok": False, "error": "That game cannot be updated from the panel."}
    title = m.title
    doc, _why = _read_status(key)
    upd = doc.get("update") if isinstance(doc, dict) else None
    if not (isinstance(upd, dict) and upd.get("apply") is True):
        msg = "%s does not offer to update itself from the panel." % title
        return 409, {"ok": False, "msg": msg, "error": msg}
    url, token = port
    req = urllib.request.Request(url + "/openlobby/update", data=b"", method="POST", headers={
        "X-Devtool-Token": token or "", "X-Devtool-Actor": str(actor or "")[:64],
        "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=APPLY_TIMEOUT) as r:
            code, data = r.status, r.read(64 << 10)
    except urllib.error.HTTPError as e:
        code, data = e.code, e.read(64 << 10)
    except Exception:                          # noqa: BLE001 -- nothing answered
        msg = "%s did not answer the update request." % title
        return 502, {"ok": False, "msg": msg, "error": msg}
    try:
        got = json.loads(data or b"{}")
    except ValueError:
        got = {}
    msg = _text(got.get("msg"), 300) if isinstance(got, dict) else None
    if code == 202:
        now = time.time()
        with _lock:
            _applying[key] = {"title": title, "actor": actor, "since": now, "seen": now}
            _failed.pop(key, None)
        return 200, {"ok": True, "msg": msg or "%s is updating." % title, "title": title}
    if code in (409, 503):
        msg = msg or ("%s is already updating." % title if code == 409 else "%s cannot update now." % title)
        return code, {"ok": False, "msg": msg, "error": msg, "title": title}
    msg = "The game did not take the update request (HTTP %d)" % code
    return 502, {"ok": False, "msg": msg, "error": msg, "title": title}


def apply_state(key):
    """Where a game's own update is, read from its status now, for the panel to poll (every 5 s
    while it updates): {"key", "title", "reachable", "state", "message", "why", "update",
    "running", "latest", "done", "lost", "applying"}. done: running.commit equals latest.commit
    and update is "current". lost: the game has not answered for LOST_AFTER seconds during an
    update the panel started."""
    if not isinstance(key, str) or not _KEY.match(key):
        return 400, {"error": "Which game?"}
    import adminmodules
    if adminmodules.port_of(key) is None:
        return 404, {"error": "That game is not on this panel."}
    title = _title(key)
    doc, _why = _read_status(key)
    now = time.time()
    out = {"key": key, "title": title, "reachable": doc is not None, "state": None, "message": None,
           "why": None, "update": None, "running": None, "latest": None, "done": False, "lost": False}
    raw = doc.get("update") if doc is not None else None
    it = clean_item(raw, key=key, name=title) if isinstance(raw, dict) else None
    if it is not None:
        out.update(state=it.get("state"), message=it.get("message"), why=it.get("why"),
                   update=it["update"], running=it["running"], latest=it["latest"])
        lat = it["latest"]["commit"]
        out["done"] = (it["update"] == "current" and it.get("state") is None and bool(lat)
                       and it["running"]["commit"] == lat)
        _observe(key, it)
    with _lock:
        run = _applying.get(key)
        if run is not None:
            if doc is not None:
                run["seen"] = now
            if out["done"]:
                _applying.pop(key, None)
            elif doc is None and now - run["seen"] >= LOST_AFTER:
                out["lost"] = True
                out["message"] = "%s has not come back after updating; check its server" % title
        out["applying"] = key in _applying
    return 200, out
