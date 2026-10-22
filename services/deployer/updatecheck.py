"""Update checks, shared by the deployer (deployer.py) and the admin panel
(services/adminupdates.py): the item both answer with, and what a GitHub
repository's branch or tag has that a running revision lacks.
docs/deployer.md, "Updates", describes the item.

    {"key", "name",
     "running": {"commit", "built_at"},
     "latest":  {"commit", "date"},
     "behind":  int or None,
     "changes": [{"commit", "date", "subject"}],    newest first, no merges, at most 50
     "update":  "available" | "current" | "unknown",
     "why":     one sentence when it cannot tell (or about a pin), else None,
     "how":     how to update, else None}

GitHub is asked without a token, so its limit for unauthenticated callers
(60 requests an hour per address) applies; answers are kept for an hour,
and a comparison of two commits, which never changes, for as long as the
cache holds it. Standard library only.
"""
import calendar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TTL = 3600.0               # seconds a branch's commit list is reused
TIMEOUT = 8.0
MAX_CHANGES = 50
MAX_CACHE = 400            # entries kept in the cache
MAX_BYTES = 4 << 20

HEX = re.compile(r"^[0-9a-f]{7,40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
                  r"(Z|[+-]\d{2}:?\d{2})?$")


class Unavailable(Exception):
    """The check could not be made; the message is the item's "why"."""


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def to_iso(text):
    """An ISO 8601 time as UTC ('...Z'), or None when it is not one. A time
    without a zone is taken as UTC."""
    if not isinstance(text, str):
        return None
    m = _ISO.match(text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, zone = m.groups()
    try:
        t = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s or 0), 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31 and int(h) < 24 and int(mi) < 60):
        return None
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        z = zone[1:].replace(":", "")
        t -= sign * (int(z[:2]) * 3600 + int(z[2:]) * 60)
    return iso(t)


def commit_id(v):
    """A commit as lower-case hex (7 to 40 digits), or None."""
    v = str(v or "").strip().lower()
    return v if HEX.match(v) else None


def same_commit(a, b):
    a, b = commit_id(a), commit_id(b)
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def subject(message):
    line = str(message or "").strip().split("\n", 1)[0]
    return " ".join(line.split())[:200]


def item(key, name, running=None, built_at=None):
    """A fresh item that does not know yet."""
    return {"key": key, "name": name,
            "running": {"commit": commit_id(running), "built_at": to_iso(built_at)},
            "latest": {"commit": None, "date": None},
            "behind": None, "changes": [], "update": "unknown", "why": None, "how": None}


def unknown(it, why):
    it.update(update="unknown", why=why, behind=None, changes=[])
    return it


def repo_of(entry, default=None):
    """(repo "owner/name", branch) for a catalog entry: its "source", else
    the GitHub URL in its "repo", else `default`."""
    src = entry.get("source") if isinstance(entry, dict) else None
    branch = "main"
    if isinstance(src, dict):
        if isinstance(src.get("branch"), str) and REF.match(src["branch"]):
            branch = src["branch"]
        if isinstance(src.get("repo"), str) and REPO.match(src["repo"]):
            return src["repo"], branch
    url = entry.get("repo") if isinstance(entry, dict) else None
    m = re.match(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url or "")
    if m:
        return m.group(1), branch
    return default, branch


# ------------------------------------------------------------------- GitHub

class GitHub:
    """GET against the GitHub API with a cache, kept in `cache_path` (a JSON
    file) when one is given so a restart does not ask again."""

    def __init__(self, api=None, ttl=TTL, timeout=TIMEOUT, cache_path=None):
        self.api = (api or API).rstrip("/")
        self.ttl, self.timeout, self.cache_path = ttl, timeout, cache_path
        self.lock = threading.Lock()
        self.cache = {}          # url -> {"t", "status", "data"}
        self.calls = 0
        if cache_path:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    got = json.load(f)
                if isinstance(got, dict) and got.get("api") == self.api:
                    self.cache = {k: v for k, v in (got.get("cache") or {}).items()
                                  if isinstance(v, dict) and "t" in v}
            except (OSError, ValueError):
                pass

    def save(self):
        if not self.cache_path:
            return
        with self.lock:
            snap = {"api": self.api, "cache": dict(self.cache)}
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _prune(self):
        if len(self.cache) > MAX_CACHE:
            for k, _v in sorted(self.cache.items(), key=lambda kv: kv[1]["t"])[:len(self.cache) - MAX_CACHE]:
                del self.cache[k]

    def get(self, path, force=False, forever=False):
        """(status, JSON) for an API path. 200 and 404 answers are cached;
        `forever` keeps a 200 past the TTL (a comparison of two fixed commits).
        Raises Unavailable when GitHub cannot be asked or refuses to answer."""
        url = self.api + path
        now = time.time()
        with self.lock:
            hit = self.cache.get(url)
        if hit and not force and (now - hit["t"] < self.ttl or (forever and hit["status"] == 200)):
            return hit["status"], hit["data"]
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "openlobby-update-check",
                                                   "X-GitHub-Api-Version": "2022-11-28"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, data = r.status, r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            status, data = e.code, b""
            try:
                data = e.read(MAX_BYTES + 1)
            except OSError:
                pass
            if status in (403, 429) and (status == 429 or e.headers.get("X-RateLimit-Remaining") == "0"):
                reset = e.headers.get("X-RateLimit-Reset") or ""
                when = ""
                if reset.isdigit():
                    when = " until %s UTC" % time.strftime("%H:%M", time.gmtime(int(reset)))
                raise Unavailable("GitHub's API limit for this address is used up%s, so the check "
                                  "was skipped; it runs again later." % when)
            if status not in (404, 409, 422):
                raise Unavailable("GitHub answered HTTP %d, so the check was skipped." % status)
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise Unavailable("GitHub could not be reached (%s), so the check was skipped."
                              % " ".join(str(reason).split())[:120])
        if len(data) > MAX_BYTES:
            raise Unavailable("GitHub's answer was too large to read.")
        try:
            doc = json.loads(data or b"null")
        except ValueError:
            raise Unavailable("GitHub answered something that is not JSON.")
        if status in (200, 404, 422):
            with self.lock:
                self.cache[url] = {"t": now, "status": status, "data": doc}
                self._prune()
        return status, doc

    def commits(self, repo, ref, force=False):
        """The newest commits on `ref` (a branch, tag or commit), newest
        first: [{"commit", "date", "subject", "merge"}]. Raises Unavailable."""
        st, doc = self.get("/repos/%s/commits?%s" % (repo, urllib.parse.urlencode(
            {"sha": ref, "per_page": MAX_CHANGES})), force=force)
        if st != 200 or not isinstance(doc, list):
            raise Unavailable("%s has no branch, tag or commit named %s." % (repo, ref))
        return [c for c in map(_commit, doc) if c]

    def compare(self, repo, base, head):
        """{"ahead", "behind", "commits" (oldest first)} for base...head, or
        None when GitHub does not know `base` there. Raises Unavailable."""
        st, doc = self.get("/repos/%s/compare/%s...%s" % (repo, base, head), forever=True)
        if st != 200 or not isinstance(doc, dict):
            return None
        return {"ahead": _int(doc.get("ahead_by")), "behind": _int(doc.get("behind_by")),
                "commits": [c for c in map(_commit, doc.get("commits") or []) if c]}

    def tags(self, repo, force=False):
        st, doc = self.get("/repos/%s/tags?per_page=100" % repo, force=force)
        if st != 200 or not isinstance(doc, list):
            return []
        return [t["name"] for t in doc if isinstance(t, dict) and isinstance(t.get("name"), str)]


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _commit(c):
    if not isinstance(c, dict):
        return None
    sha = commit_id(c.get("sha"))
    inner = c.get("commit") if isinstance(c.get("commit"), dict) else {}
    who = inner.get("committer") if isinstance(inner.get("committer"), dict) else {}
    if not sha:
        return None
    return {"commit": sha, "date": to_iso(who.get("date")), "subject": subject(inner.get("message")),
            "merge": len(c.get("parents") or []) > 1}


def changes_of(commits):
    """newest-first commits -> the item's "changes": no merges, at most 50."""
    return [{"commit": c["commit"], "date": c["date"], "subject": c["subject"]}
            for c in commits if not c.get("merge")][:MAX_CHANGES]


# -------------------------------------------------------------------- pins

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_MINOR = re.compile(r"^v?(\d+)\.(\d+)$")
_SHA = re.compile(r"^sha-([0-9a-f]{7,40})$")


def pin_ref(gh, repo, tag, force=False):
    """The git ref a published image tag stands for, or None for no pin
    (empty or "latest"). The images are tagged latest, X.Y.Z and X.Y (from
    git tags vX.Y.Z) and sha-<commit>. Raises Unavailable."""
    tag = (tag or "").strip()
    if tag in ("", "latest"):
        return None
    m = _SEMVER.match(tag)
    if m:
        return "v%s.%s.%s" % m.groups()
    m = _SHA.match(tag)
    if m:
        return m.group(1)
    m = _MINOR.match(tag)
    if m:
        best = None
        for name in gh.tags(repo, force):
            t = _SEMVER.match(name)
            if t and t.group(1) == m.group(1) and t.group(2) == m.group(2):
                v = tuple(int(x) for x in t.groups())
                if best is None or v > best[0]:
                    best = (v, name)
        if best is None:
            raise Unavailable("%s has no release tagged v%s.%s.*, which the pin %s needs."
                              % (repo, m.group(1), m.group(2), tag))
        return best[1]
    if REF.match(tag):
        return tag
    raise Unavailable("The pin %r is not a tag the check understands." % tag)


# ----------------------------------------------------------------- the check

def check_github(it, gh, repo, branch, force=False, pin=None, pin_name="", no_revision=None):
    """Fill `it` (an item() whose running commit is set or None) from
    GitHub: the newest commit on `branch`, or on the pinned ref when `pin`
    (an image tag other than latest) is given. Never raises."""
    if not repo:
        return unknown(it, "No source repository is known for %s." % it["name"])
    if not it["running"]["commit"]:
        # what runs is unknown, which is the news; the newest commit is shown when it can be had
        try:
            ref = pin_ref(gh, repo, pin, force) if pin else None
            commits = gh.commits(repo, ref or branch, force)
            if commits:
                it["latest"] = {"commit": commits[0]["commit"], "date": commits[0]["date"]}
        except Unavailable:
            pass
        return unknown(it, no_revision or "The running build does not say which commit it was "
                                          "built from, so it cannot be compared.")
    try:
        ref = pin_ref(gh, repo, pin, force) if pin else None
        commits = gh.commits(repo, ref or branch, force)
        if not commits:
            return unknown(it, "%s has no commits on %s." % (repo, ref or branch))
        head = commits[0]
        it["latest"] = {"commit": head["commit"], "date": head["date"]}
        outside = None
        if ref:
            on_branch = gh.commits(repo, branch, force)
            if on_branch and not same_commit(on_branch[0]["commit"], head["commit"]):
                outside = ("It is pinned to %s (%s); %s has newer commits outside the pin."
                           % (pin, pin_name or "the image tag", branch))
        running = it["running"]["commit"]
        if same_commit(running, head["commit"]):
            it.update(update="current", behind=0, changes=[], why=outside)
            return it
        cmp = gh.compare(repo, running, head["commit"])
        if cmp is None or cmp["ahead"] is None:
            return unknown(it, "The running commit %s is not in %s, so it cannot be compared."
                           % (running[:12], repo))
        newest_first = list(reversed(cmp["commits"]))
        if cmp["ahead"] > len(cmp["commits"]):
            # GitHub lists at most 250 commits of a comparison, oldest first, so
            # for a long gap the newest are taken from the branch instead
            newest_first = commits
        it["behind"] = cmp["ahead"]
        it["changes"] = changes_of(newest_first) if cmp["ahead"] else []
        it["update"] = "available" if cmp["ahead"] else "current"
        why = outside
        if cmp["behind"]:
            why = ("The running build has %d commit%s that %s does not."
                   % (cmp["behind"], "" if cmp["behind"] == 1 else "s", ref or branch))
            if outside:
                why = why[:-1] + "; " + outside[0].lower() + outside[1:]
        it["why"] = why
        return it
    except Unavailable as exc:
        return unknown(it, str(exc))
