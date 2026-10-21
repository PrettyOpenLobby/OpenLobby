"""The admin panel's side of the deployer (services/deployer/, docs/deployer.md): installing,
turning on and updating games from the panel without the panel ever holding the Docker socket.

The deployer is a separate, opt-in service that runs only fixed catalog actions. The panel
talks to it over the compose network with a token, owner only:

    POL_DEPLOYER_URL     e.g. http://deployer:8099; unset = no deployer, the page is hidden
    POL_DEPLOYER_TOKEN   the deployer's token (Authorization: Bearer)

Every call here answers (status, body) and never raises: a deployer that is down or not set up
reads as unavailable, which hides the page rather than breaking the panel.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8.0
ACTIONS = ("install", "enable", "disable", "update", "restart", "check")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_JOB = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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
    except urllib.error.HTTPError as e:
        data, status, state = e.read(MAX_LOG + 1), e.code, ""
        if status == 401:
            return 502, {"error": "The deployer refused the panel's token."}
    except (urllib.error.URLError, OSError, ValueError):
        return 503, {"error": "The deployer is not answering."}
    if raw:
        return status, {"text": data[:MAX_LOG].decode("utf-8", "replace"), "state": state}
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


def action(act, game):
    if act not in ACTIONS:
        return 400, {"error": "Unknown action."}
    if not isinstance(game, str) or not _KEY.match(game):
        return 400, {"error": "Unknown game."}
    status, out = _call("POST", "/api/actions", {"action": act, "game": game})
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
