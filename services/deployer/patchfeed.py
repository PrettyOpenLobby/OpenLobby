"""The developers' patch feed: .polpatch files kept as generic packages on a
Forgejo or Gitea server, which only an access code with a feed in it (an olx2.
code, see devchannel.py) can read. docs/deployer.md, "Patches from the dev
channel".

One package per bundle, named polpatch-<REGION>-<PRODUCT>, one package version
per patch version. A version holds one or both of

    full.polpatch                  the whole bundle up to that version
    update-from-<VERSION>.polpatch that version on top of VERSION

and the deployer takes the chain of updates from the version a server has when
every link is there, else the newest full file and the updates after it.
Nothing from the feed is trusted: names and versions are checked here, the
sizes and SHA-256 sums the server lists are checked on download, and every
file then goes through the same checks as an upload.

Standard library only.
"""
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

PACKAGE = re.compile(r"^polpatch-([A-Z0-9]{3})-([0-9]{4})$")
VERSION = re.compile(r"^[0-9]{8}_[0-9A-Za-z]{1,2}$")
FULL = "full.polpatch"
UPDATE = re.compile(r"^update-from-([0-9]{8}_[0-9A-Za-z]{1,2})\.polpatch$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIMEOUT = 20.0
PAGE = 50
MAX_PAGES = 40          # 2000 package versions; a feed is a few dozen bundles
CHUNK = 1 << 20

NO_ACCESS = "The access code no longer opens the patch feed; ask for a new one."
NO_ANSWER = "The patch feed did not answer; try again later."


def version_key(v):
    """Sort key for patch versions: the date, then the suffix, shorter first, so a
    same-day _9 comes before _10 (SE's suffixes are one character; ours run to two)."""
    return (v[:8], len(v) - 9, v[9:])


class FeedError(Exception):
    """A sentence for the panel; never names the feed's host."""


class Feed:
    def __init__(self, feed, token=None, user="dev", scheme="https", timeout=TIMEOUT):
        self.host, self.owner = feed.split("/", 1)
        self.token, self.user = token, user
        self.base = "%s://%s" % (scheme, self.host)
        self.timeout = timeout

    def _headers(self):
        h = {"Accept": "application/json", "User-Agent": "openlobby-deployer"}
        if self.token:
            user, secret = self.token.split(":", 1) if ":" in self.token else (self.user, self.token)
            h["Authorization"] = "Basic " + base64.b64encode(
                ("%s:%s" % (user, secret)).encode()).decode()
        return h

    def _open(self, path):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FeedError(NO_ACCESS)
            if exc.code == 404:
                return None
            raise FeedError(NO_ANSWER)
        except (OSError, ValueError):
            raise FeedError(NO_ANSWER)

    def _json(self, path):
        resp = self._open(path)
        if resp is None:
            return None
        with resp:
            try:
                return json.loads(resp.read(4 << 20))
            except ValueError:
                raise FeedError(NO_ANSWER)

    def bundles(self):
        """{(region, product): {version: {filename: {"size", "sha256"}}}} for every
        well-formed package version and file in the feed."""
        owner = urllib.parse.quote(self.owner)
        found = {}
        for page in range(1, MAX_PAGES + 1):
            rows = self._json("/api/v1/packages/%s?type=generic&q=polpatch-&page=%d&limit=%d"
                              % (owner, page, PAGE))
            if rows is None:
                if page == 1:
                    raise FeedError(NO_ACCESS)
                break
            if not isinstance(rows, list):
                raise FeedError(NO_ANSWER)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                m = PACKAGE.match(str(row.get("name") or ""))
                v = str(row.get("version") or "")
                if m and VERSION.match(v):
                    found.setdefault((m.group(1), m.group(2)), {})[v] = None
            if len(rows) < PAGE:
                break
        for (region, product), versions in found.items():
            for v in list(versions):
                files = self._json("/api/v1/packages/%s/generic/polpatch-%s-%s/%s/files"
                                   % (owner, region, product, v)) or []
                ok = {}
                for f in files if isinstance(files, list) else []:
                    name = str(f.get("name") or "") if isinstance(f, dict) else ""
                    # Forgejo and Gitea spell it "Size"
                    size = f.get("Size", f.get("size")) if isinstance(f, dict) else None
                    sha = str(f.get("sha256") or "").lower() if isinstance(f, dict) else ""
                    if (name == FULL or UPDATE.match(name)) and isinstance(size, int) \
                            and not isinstance(size, bool) and size > 0 and SHA256.match(sha):
                        ok[name] = {"size": size, "sha256": sha}
                if ok:
                    versions[v] = ok
                else:
                    del versions[v]
        return {k: v for k, v in found.items() if v}

    def download(self, region, product, version, name, want, dest, max_size):
        """Fetch one file to dest, checking its size and SHA-256 against `want`."""
        if want["size"] > max_size:
            raise FeedError("The %s/%s %s file is bigger than this server takes (%d bytes)."
                            % (region, product, version, want["size"]))
        resp = self._open("/api/packages/%s/generic/polpatch-%s-%s/%s/%s"
                          % (urllib.parse.quote(self.owner), region, product, version, name))
        if resp is None:
            raise FeedError("The %s/%s %s file is no longer in the patch feed."
                            % (region, product, version))
        h, n = hashlib.sha256(), 0
        with resp, open(dest, "wb") as out:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except OSError:
                    raise FeedError(NO_ANSWER)
                if not chunk:
                    break
                n += len(chunk)
                if n > want["size"]:
                    break
                h.update(chunk)
                out.write(chunk)
        if n != want["size"] or h.hexdigest() != want["sha256"]:
            os.remove(dest)
            raise FeedError("The %s/%s %s file came down damaged; try again."
                            % (region, product, version))
        return dest


class Store:
    """Whether patches from the feed are on, and the access code that opens the feed,
    under STATE/patchfeed. Kept apart from the dev channel's own code (devchannel.Store):
    patches work on the stable channel too, and switching the channel back to Stable,
    which forgets the channel's code, leaves this one alone.

        code         the code as given, mode 600
        state.json   {"enabled": bool, "changed": ISO time}
    """

    def __init__(self, state):
        self.dir = os.path.join(state, "patchfeed")
        self.code_path = os.path.join(self.dir, "code")
        self.state_path = os.path.join(self.dir, "state.json")

    def _dir(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def code(self):
        try:
            with open(self.code_path, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    def save_code(self, code):
        self._dir()
        tmp = self.code_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        os.replace(tmp, self.code_path)

    def enabled(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f).get("enabled") is True
        except (OSError, ValueError, AttributeError):
            return False

    def set_enabled(self, on, when):
        self._dir()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(on), "changed": when}, f)
        os.replace(tmp, self.state_path)

    def forget(self):
        for p in (self.code_path, self.state_path):
            try:
                os.remove(p)
            except OSError:
                pass


def feed_of(code, registry=None):
    """The parsed code when it opens a patch feed, else None."""
    import devchannel
    try:
        info = devchannel.parse_code(code, registry=registry) if code else None
    except devchannel.ChannelError:
        return None
    return info if info and info.get("feed") else None


def offer_code(state, code):
    """Keep `code` as the patch feed's code when it opens a feed and none is kept yet.
    Patches stay off: turning them on is the owner's choice. For the deployer's redeem
    of a short dev code, which may answer an olx2. code. True when it was kept."""
    store = Store(state)
    if store.code() or not feed_of(code):
        return False
    store.save_code(code)
    return True


def plan(versions, have):
    """What to fetch to bring a bundle to the feed's newest version.

    versions is one bundle's {version: {filename: info}} from Feed.bundles(),
    have the versions the server has (empty when it has no such bundle).
    Returns {"status", "latest", "steps": [(version, filename, info), ...],
    "size", "why"}: status is "current", "behind" (steps take it to latest),
    "new" (it has none; steps install it), "ahead" (it has a newer one),
    "custom" (it has versions the feed does not, so no file fits) or
    "unavailable" (no full file or update chain reaches it)."""
    order = sorted(versions, key=version_key)
    latest = order[-1]
    have = sorted(have, key=version_key)
    out = {"latest": latest, "steps": [], "size": 0, "why": None}
    if have and version_key(have[-1]) > version_key(latest):
        return dict(out, status="ahead")
    if have and have[-1] == latest:
        return dict(out, status="current")
    if have and set(have) - set(order):
        return dict(out, status="custom",
                    why="This server has version(s) the feed does not (%s), so a file from "
                        "the feed would leave them out." % ", ".join(sorted(set(have) - set(order),
                                                                            key=version_key)))

    def chain(base):
        steps, prev = [], base
        for v in order:
            if version_key(v) <= version_key(base):
                continue
            name = "update-from-%s.polpatch" % prev
            if name not in versions[v]:
                return None
            steps.append((v, name, versions[v][name]))
            prev = v
        return steps

    steps = chain(have[-1]) if have else None
    if steps is None:
        for v in reversed(order):
            if FULL in versions[v]:
                rest = chain(v)
                if rest is not None:
                    steps = [(v, FULL, versions[v][FULL])] + rest
                    break
    if steps is None:
        return dict(out, status="unavailable",
                    why="The feed has no full file or chain of updates that reaches %s." % latest)
    return dict(out, status="behind" if have else "new", steps=steps,
                size=sum(s[2]["size"] for s in steps))
