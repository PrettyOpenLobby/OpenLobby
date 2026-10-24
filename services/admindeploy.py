"""The admin panel's side of the deployer (services/deployer/, docs/deployer.md): installing,
turning on and updating games from the panel without the panel ever holding the Docker socket.

The deployer is a separate, opt-in service that runs only fixed catalog actions. The panel
talks to it over the compose network with a token, owner only:

    POL_DEPLOYER_URL     e.g. http://deployer:8099; unset = no deployer, the page is hidden
    POL_DEPLOYER_TOKEN   the deployer's token (Authorization: Bearer)

Every call here answers (status, body) and never raises: a deployer that is down or not set up
reads as unavailable, which hides the page rather than breaking the panel.
"""
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.join(HERE, "deployer") not in sys.path:
    # the dev channel's checks are the deployer's own (services/deployer/devchannel.py)
    sys.path.append(os.path.join(HERE, "deployer"))
import devchannel  # noqa: E402

TIMEOUT = 8.0
CHANNEL_TIMEOUT = 45.0      # a switch to Dev waits for the deployer to ask the registry
ACTIONS = ("install", "enable", "disable", "update", "restart", "check", "import")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_JOB = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UPLOAD_TIMEOUT = 1800.0     # the deployer answers an upload once it is extracted and listed
CHUNK = 1 << 20
MAX_LOG = 256 * 1024


def configured():
    url = os.environ.get("POL_DEPLOYER_URL", "").strip().rstrip("/")
    tok = os.environ.get("POL_DEPLOYER_TOKEN", "").strip()
    return (url, tok) if url and tok else None


def _call(method, path, body=None, raw=False, timeout=TIMEOUT):
    conf = configured()
    if conf is None:
        return 503, {"error": "No deployer is set up on this server (POL_DEPLOYER_URL)."}
    url, tok = conf
    req = urllib.request.Request(url + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data, status, state = r.read(MAX_LOG + 1), r.status, r.headers.get("X-Job-State", "")
            log_at = r.headers.get("X-Log-Offset")
    except urllib.error.HTTPError as e:
        data, status, state, log_at = e.read(MAX_LOG + 1), e.code, "", None
        if status == 401:
            return 502, {"error": "The deployer refused the panel's token."}
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"error": "The deployer is not answering."}
    if raw:
        # The log is read from an offset; a part the deployer has dropped (past its 1 MB
        # cap) is skipped, and X-Log-Offset says where the text really starts.
        text = data[:MAX_LOG]
        try:
            start = int(log_at) if log_at is not None else None
        except ValueError:
            start = None
        return status, {"text": text.decode("utf-8", "replace"), "state": state,
                        "start": start, "next": None if start is None else start + len(text)}
    try:
        got = json.loads(data or b"{}")
    except ValueError:
        return 502, {"error": "The deployer answered something that is not JSON."}
    return status, got if isinstance(got, dict) else {"data": got}


def overview():
    """Everything the page draws: the catalog and the recent jobs, or {"available": False}."""
    if configured() is None:
        return 200, {"available": False, "why": "not set up"}
    st, games = _call("GET", "/api/games")
    if st != 200:
        return 200, {"available": False, "why": games.get("error") or "HTTP %d" % st}
    _st, jobs = _call("GET", "/api/jobs?limit=20")
    return 200, {"available": True, "games": games.get("games") or [], "core": games.get("core") or {},
                 "options": games.get("options") or {}, "jobs": (jobs or {}).get("jobs") or []}


def updates(refresh=False):
    """The deployer's update check (docs/deployer.md, "Updates"): (status, {"checked_at",
    "checking", "items"}). refresh asks it to check again now; it does that at most once a
    minute and answers at once either way, with "checking" true while the check runs."""
    return _call("GET", "/api/updates" + ("?refresh=1" if refresh else ""))


OLD_DEPLOYER = "This deployer has no update channels yet; update the core first, then try again."


#: what the panel passes on of the deployer's channel answer and of each image row: never
#: the registry's address, which the page does not show (it says "dev builds")
CHANNEL_KEYS = ("channel", "unlocked", "dev", "since", "code_stored", "code_ok", "code_why", "ghcr",
                "problem", "install_id", "short_codes")
CHANNEL_ROW = ("key", "name", "stable", "dev_name", "applies", "on", "what", "license", "note",
               "enabled", "has_source")
#: what the panel passes on of each thing with a dev build (the core, a game)
KEY_ROW = ("key", "name", "on", "since", "enabled", "applies", "builds_on_core", "images",
           "services", "note", "problem", "would", "back")
_SHORT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _row(raw):
    out = {k: raw.get(k) for k in CHANNEL_ROW}
    if not isinstance(out["dev_name"], str):
        # an older deployer names the image name:tag; the page shows "name (dev)"
        img = raw.get("dev_image")
        name = img.rsplit(":", 1)[0].rsplit("/", 1)[-1] if isinstance(img, str) else ""
        out["dev_name"] = "%s (dev)" % name if _SHORT.match(name) else None
    out["has_source"] = out["has_source"] is True
    return out


def channel():
    """The update channel (docs/deployer.md, "Dev channel"): which one, and each image of the
    core and the installed games on it by short name. The registry's address is not passed
    on, even from a deployer that sends it."""
    st, out = _call("GET", "/api/channel")
    if st == 404:
        return 200, {"available": False, "why": OLD_DEPLOYER}
    if st != 200:
        return st, out
    got = {k: out.get(k) for k in CHANNEL_KEYS}
    got["images"] = [_row(r) for r in out.get("images") or [] if isinstance(r, dict)]
    if "keys" in out:
        got["keys"] = [{k: r.get(k) for k in KEY_ROW} for r in out.get("keys") or [] if isinstance(r, dict)]
    else:
        # a deployer from before per-key switches: one switch for everything
        got["keys"] = None
        got["unlocked"] = out.get("channel") == "dev"
    iss = out.get("issuer")
    if isinstance(iss, dict) and isinstance(iss.get("value"), str):
        got["issuer"] = {"value": iss["value"][:300], "from": iss.get("from") if iss.get("from") in (
            "env", "state", "default") else None, "default": str(iss.get("default") or "")[:300]}
    else:
        got["issuer"] = None
    got["available"] = True
    return 200, got


def channel_source(stable):
    """(status, {"url"} or {"error"}): where the source of the dev image that replaces the
    stable image `stable` is, from the deployer, on the dev channel only. The panel sends
    the owner there (admin.py answers with a redirect), so the address is in no page."""
    if not isinstance(stable, str) or not stable or len(stable) > 300:
        return 400, {"error": "Which image?"}
    st, out = _call("GET", "/api/channel/source?" + urllib.parse.urlencode({"stable": stable}))
    url = out.get("url") if st == 200 else None
    if not isinstance(url, str) or not devchannel.SOURCE.match(url):
        return (404 if st in (200, 404) else st), {
            "error": out.get("error") or "The deployer does not say where that source is."}
    return 200, {"url": url}


def channel_body(body):
    """(None, what to send the deployer) for a request of the page, or (sentence, None):
    {"unlock": code} unlocks the dev builds, {"key", "to"} switches one thing, {"lock": true}
    forgets the code, {"issuer": "<https address>"|"off"|""} sets the issuer of short codes,
    and {"channel": "dev", "code"} or {"channel": "stable"} is the whole install at once."""
    if not isinstance(body, dict):
        return "Say what to switch.", None
    if "issuer" in body:
        try:
            got = devchannel.check_issuer(body.get("issuer"))
        except devchannel.ChannelError as exc:
            return str(exc), None
        return None, {"issuer": got or ""}
    if "lock" in body:
        return (None, {"lock": True}) if body.get("lock") is True else ("Say lock: true.", None)
    if "key" in body:
        key, to = body.get("key"), body.get("to")
        if not isinstance(key, str) or not _KEY.match(key):
            return "Unknown key.", None
        if to not in devchannel.CHANNELS:
            return "Pick Stable or Dev.", None
        return None, {"key": key, "to": to}
    field = "unlock" if "unlock" in body else "code"
    if field == "code":
        want = body.get("channel")
        if want not in devchannel.CHANNELS:
            return "Pick Stable or Dev.", None
        if want == "stable":
            return None, {"channel": "stable"}
        out = {"channel": "dev"}
    else:
        out = {}
    code = clean_code(body.get(field))
    if code is None:
        return "The access code is 8 to 2048 characters, with no spaces.", None
    out[field] = code
    # advanced: the registry of a plain token (an olx1. code carries its own)
    reg = body.get("registry")
    if isinstance(reg, str) and reg.strip():
        if not devchannel.REGISTRY.match(reg.strip()):
            return "The dev registry must look like registry.example.org/team.", None
        out["registry"] = reg.strip()
    elif reg not in (None, ""):
        return "The dev registry must look like registry.example.org/team.", None
    return None, out


def set_channel(body):
    """Pass a request about the dev builds on to the deployer (channel_body). The access
    code goes there and nowhere else: it is not stored, logged or sent back here, and a
    request that does not fit never leaves."""
    bad, out = channel_body(body)
    if bad:
        return 400, {"error": bad}
    status, got = _call("POST", "/api/channel", out, timeout=CHANNEL_TIMEOUT)
    out.pop("code", None)
    out.pop("unlock", None)
    if status == 409:
        got.setdefault("error", "Another job is still running; switch when it has finished.")
    elif status == 404:
        got = {"error": OLD_DEPLOYER}
    elif status == 400:
        got.setdefault("error", "The deployer refused that.")
        if ("key" in out or "lock" in out or "issuer" in out) and \
                str(got.get("error", "")).startswith("unknown fields"):
            got["error"] = OLD_DEPLOYER       # a deployer from before per-key switches
    return status, got


def _rel(path):
    """A path relative to the deployer's clients folder or an upload, or None. The deployer
    checks again (and refuses symlinks out); this only keeps obvious escapes off the wire."""
    if path is None or path == "":
        return ""
    if not isinstance(path, str) or len(path) > 1024 or "\\" in path or "\x00" in path \
            or path.startswith("/") or ".." in path.split("/"):
        return None
    return path.strip("/")


def _source(src):
    if not isinstance(src, dict):
        return None
    if "upload" in src:
        sub = _rel(src.get("subdir"))
        if not isinstance(src["upload"], str) or not _JOB.match(src["upload"]) or sub is None:
            return None
        return dict({"upload": src["upload"]}, **({"subdir": sub} if sub else {}))
    rel = _rel(src.get("path"))
    return None if rel is None else {"path": rel}


def action(act, game, item=None, source=None):
    if act not in ACTIONS:
        return 400, {"error": "Unknown action."}
    if not isinstance(game, str) or not _KEY.match(game):
        return 400, {"error": "Unknown game."}
    body = {"action": act, "game": game}
    if act == "import":
        src = _source(source)
        if not isinstance(item, str) or not _KEY.match(item):
            return 400, {"error": "Unknown data item."}
        if src is None:
            return 400, {"error": "Pick an upload or a folder to import from."}
        body.update(item=item, source=src)
    status, out = _call("POST", "/api/actions", body)
    if status == 409:
        out.setdefault("error", "Another job is still running; it is shown below.")
    elif status == 400:
        out.setdefault("error", "The deployer does not know that game or action.")
    return status, out


def clean_code(code):
    """An access code as the deployer takes it: an olx1. code (or a plain token) as typed, a
    short dev code (ABCD EFGH JKMN, any case) as ABCD-EFGH-JKMN; None when it cannot be one."""
    if not isinstance(code, str):
        return None
    short = devchannel.short_code(code)
    if short:
        return devchannel.show_short(short)
    code = code.strip()
    return code if devchannel.CODE.match(code) else None


def run_updater(key, title, rec, channel=None, switch=False, code=None):
    """Ask the deployer to run a paired game's updater (docs/deployer.md, "Updaters of paired
    games"). `rec` is the panel's stored record the owner approved, never anything a browser
    sent; the deployer checks every field of it again with the same rules. `channel` appends
    "--channel <name>"; `switch` says it is a channel switch, and `code` (a switch to a channel
    other than stable only) is the access code the deployer hands the updater in its
    environment. The code goes there and nowhere else."""
    if not isinstance(key, str) or not _KEY.match(key):
        return 400, {"error": "Unknown game."}
    upd = {"folder": rec["folder"], "command": list(rec["command"]),
           "timeout_minutes": rec["timeout_minutes"]}
    if rec.get("channels"):
        upd["channels"] = list(rec["channels"])
    body = {"game": key, "title": str(title or key)[:80], "updater": upd}
    if channel is not None:
        body["channel"] = channel
    if switch:
        body["switch"] = True
    if code:
        body["code"] = code
    status, out = _call("POST", "/api/updater", body, timeout=CHANNEL_TIMEOUT if code else TIMEOUT)
    body.pop("code", None)
    if status == 409:
        out.setdefault("error", "Another job is still running; it is shown below.")
    elif status == 404:
        out = {"error": "This deployer cannot run game updaters yet; update the core first, then try again."}
    elif status == 400:
        out.setdefault("error", "The deployer refused that updater.")
    return status, out


def job(job_id):
    if not isinstance(job_id, str) or not _JOB.match(job_id):
        return 400, {"error": "Unknown job."}
    return _call("GET", "/api/jobs/" + job_id)


def log(job_id, offset):
    if not isinstance(job_id, str) or not _JOB.match(job_id):
        return 400, {"error": "Unknown job."}
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    return _call("GET", "/api/jobs/%s/log?offset=%d" % (urllib.parse.quote(job_id), offset), raw=True)


def browse(path):
    rel = _rel(path)
    if rel is None:
        return 400, {"error": "That folder is outside the clients folder."}
    st, out = _call("GET", "/api/browse?path=" + urllib.parse.quote(rel))
    if st == 409:
        out.setdefault("error", "Browsing is off: set POL_DEPLOY_CLIENTS for the deployer.")
    elif st == 403:
        out.setdefault("error", "The deployer cannot read that folder.")
    return st, out


def uploads():
    return _call("GET", "/api/uploads")


def delete_upload(upload_id):
    if not isinstance(upload_id, str) or not _JOB.match(upload_id):
        return 400, {"error": "Unknown upload."}
    st, out = _call("DELETE", "/api/uploads/" + upload_id)
    return (200, {"ok": True}) if st == 204 else (st, out)


def upload(stream, length, filename):
    """Pass an archive the browser sent straight on to the deployer, CHUNK bytes at a time:
    never whole in memory, never wrapped. filename is the browser's X-Filename, already
    percent-encoded UTF-8, and is passed on as it came (the deployer only displays it)."""
    conf = configured()
    if conf is None:
        return 503, {"error": "No deployer is set up on this server (POL_DEPLOYER_URL)."}
    if not isinstance(length, int) or length <= 0:
        return 411, {"error": "The upload needs a length."}
    url, tok = conf
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=UPLOAD_TIMEOUT)
    sent = 0
    try:
        conn.putrequest("POST", (u.path or "") + "/api/uploads")
        conn.putheader("Authorization", "Bearer " + tok)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(length))
        conn.putheader("X-Filename", (filename or "upload")[:1024])
        conn.endheaders()
        while sent < length:
            chunk = stream.read(min(CHUNK, length - sent))
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
        if sent < length:
            return 400, {"error": "The upload stopped part way (%d of %d bytes)." % (sent, length)}
        r = conn.getresponse()
        status, data = r.status, r.read(MAX_LOG)
    except (OSError, http.client.HTTPException):
        # a deployer that answered early (413, 401) and closed lands here too: say what we know
        try:
            r = conn.getresponse()
            status, data = r.status, r.read(MAX_LOG)
        except Exception:
            return 503, {"error": "The deployer stopped answering during the upload."}
    finally:
        conn.close()
    if status == 401:
        return 502, {"error": "The deployer refused the panel's token."}
    try:
        out = json.loads(data or b"{}")
    except ValueError:
        return 502, {"error": "The deployer answered something that is not JSON."}
    out = out if isinstance(out, dict) else {"data": out}
    if status == 413:
        out.setdefault("error", "The archive is bigger than the deployer takes.")
    elif status == 507:
        out.setdefault("error", "The deployer's disk is full. Remove an old upload and try again.")
    return status, out


# -- patch bundles (docs/deployer.md "Patch bundles") ------------------------------ #
_REGION = re.compile(r"^[A-Z0-9]{3}$")
_PRODUCT = re.compile(r"^[0-9]{4}$")
_VERSION = re.compile(r"^[0-9]{8}_[0-9A-Za-z]$")
PATCH_ACTIONS = ("patch_import", "patch_build", "patch_rollback", "patch_remove", "patch_check",
                 "patch_fetch")


def patches(recheck=False):
    """The bundles in the mirrors folder; recheck starts the self-check of every bundle."""
    st, out = _call("GET", "/api/patches" + ("?recheck=1" if recheck else ""))
    if st == 202:
        return 200, out
    if st == 409 and not out.get("bundles"):
        out.setdefault("error", "Patch bundles are off: the deployer has no mirrors folder (POL_DEPLOY_MIRRORS).")
        out["off"] = True
    return st, out


def patch_provenance():
    """Where each version of each bundle comes from (retail, ours) and the retail
    versions a bundle lacks, as the deployer reads them from the mirrors folder."""
    return _call("GET", "/api/patches/provenance")


def patch_feed(refresh=False):
    """The developers' patch feed, when patches from them are on: what it offers
    and how far this server is from it. Off (the default): {"feed": false,
    "enabled": false, "can_enable"}, can_enable saying whether a code that opens
    the patches is already kept, so turning them on needs no code typed."""
    st, out = _call("GET", "/api/patches/feed" + ("?refresh=1" if refresh else ""))
    if st == 409 and out.get("feed") is False:
        return 200, {"feed": False, "enabled": False, "can_enable": bool(out.get("can_enable")),
                     "from_channel": bool(out.get("from_channel")), "error": out.get("error")}
    if st == 502:
        out["feed"] = True
        out["enabled"] = True
    return st, out


def patch_feed_set(body):
    """Turn patches from the developers on ({"enabled": true, "code"?}) or off
    ({"enabled": false, "forget"?}). The code goes to the deployer only."""
    on = body.get("enabled")
    if not isinstance(on, bool):
        return 400, {"error": "Say whether patches are on."}
    out = {"enabled": on}
    if on:
        code = body.get("code")
        if code is not None:
            if not isinstance(code, str) or len(code.strip()) > 2048:
                return 400, {"error": "The access code is up to 2048 characters."}
            if code.strip():
                out["code"] = code.strip()
    elif body.get("forget") is True:
        out["forget"] = True
    return _call("POST", "/api/patches/feed", out)


def patch_action(body):
    """One patch action, checked here as the deployer will check it, so nothing odd reaches it."""
    act = body.get("action")
    if act not in PATCH_ACTIONS:
        return 400, {"error": "Unknown action."}
    out = {"action": act}
    if act != "patch_check" and act != "patch_import":
        region, product = body.get("region"), body.get("product")
        if not isinstance(region, str) or not _REGION.match(region):
            return 400, {"error": "A region is three capital letters or digits, like W2U."}
        if not isinstance(product, str) or not _PRODUCT.match(product):
            return 400, {"error": "A product is four digits, like 0002."}
        out.update(region=region, product=product)
    if act in ("patch_import", "patch_build"):
        src = _source(body.get("source"))
        if src is None:
            return 400, {"error": "Pick an upload or a folder."}
        out["source"] = src
    if act == "patch_rollback":
        to = body.get("to")
        if not isinstance(to, str) or not _VERSION.match(to):
            return 400, {"error": "Pick the version to go back to."}
        out["to"] = to
    if act == "patch_build":
        version = body.get("version") or ""
        if version and (not isinstance(version, str) or not _VERSION.match(version)):
            return 400, {"error": "A version looks like 20030429_0, or leave it empty to read it from the install."}
        if version:
            out["version"] = version
        if body.get("walk"):
            out["walk"] = True
        aliases = body.get("aliases") or []
        if not isinstance(aliases, list) or len(aliases) > 8 \
                or not all(isinstance(a, str) and _REGION.match(a) for a in aliases):
            return 400, {"error": "Other regions are three capital letters or digits each."}
        if aliases:
            out["aliases"] = aliases
    status, got = _call("POST", "/api/actions", out)
    if status == 409:
        got.setdefault("error", "Another job is still running, or patch bundles are off on this server.")
    elif status == 404:
        got.setdefault("error", "There is no bundle for that region and product.")
    elif status == 400:
        got.setdefault("error", "The deployer refused that.")
    return status, got

