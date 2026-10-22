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
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8.0
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


def _call(method, path, body=None, raw=False):
    conf = configured()
    if conf is None:
        return 503, {"error": "No deployer is set up on this server (POL_DEPLOYER_URL)."}
    url, tok = conf
    req = urllib.request.Request(url + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
    return status, out
