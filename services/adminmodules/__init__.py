"""Game modules for the admin panel: one file per game, each saying what the
panel can show and do for that game.

A module is a file in this package (or in POL_ADMIN_MODULES_DIR) that sets
`MODULE = Module(...)`. It can declare:

  - tools       pages the game's own tool port serves (the old Games section,
                admingames.py proxies them under /games/<key>/),
  - services    the containers and ports that make up the game, for the server
                checks and their restart button (the host's ops runner, not
                part of this repository, reads these; the contract between it
                and the panel is in adminops.py, "Server checks and fixes"),
  - live        how to read who is playing and what is going on right now:
                a live-session marker, the game's status JSON, or both,
  - actions     things the panel can ask the game to do (announce, kick...),
  - content_ids the PlayOnline content ids that belong to it, so an account's
                page can say which games the player has.

Every module gets permissions of its own: `game.<key>` to see it, and one
`game.<key>.<id>` per tool and per action. A moderator holding the old
`games_<key>` permission is read as holding all of them (see expand_legacy).

Which modules a server shows:

    POL_ADMIN_MODULES       comma list of keys, or "*" (default): every module
                            that finds something to talk to (a tool URL, a live
                            source, a marker some service published)
    POL_ADMIN_MODULES_DIR   extra module files (*.py) a deployment adds; one
                            whose key a built-in already uses is skipped
    POL_ADMIN_CORE          "none": a panel for games without PlayOnline; the
                            built-in modules (all PlayOnline titles) load only
                            when POL_ADMIN_MODULES names them

Per-module settings come from the environment, so a compose file can point a
module at its game without code changes:

    POL_ADMIN_GAME_<KEY>_URL     the game's tool port (as before)
    POL_ADMIN_GAME_<KEY>_TOKEN   that port's token (as before)
    POL_ADMIN_GAME_<KEY>_LIVE    a URL or file path to the game's status JSON

A game can also be stored in the panel's database instead: the owner adds it in
Settings > Modules ("Add a game"), or its setup program pairs with the panel
(docs/installs.md). The panel sets STORE to read them (adminpair.stored); a
stored game is treated exactly like the two variables above, and when both
name a key the environment wins. An explicit POL_ADMIN_MODULES list gets the
stored keys added to it. Host tools that import this package leave STORE unset
and see only the environment.

Importing this package needs only the standard library, so host tools can use
service_groups() without the panel's dependencies. Anything heavier (the
live-state store) is imported when a live source is read. An ops runner that
runs as root should not import module files at all; it can read the literal
Service(...) lists from them as text.
"""
import concurrent.futures
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import time

KEY_RE = re.compile(r"^[a-z0-9]{1,16}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOOL_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$|^$")

#: Built-in modules, in the order the panel lists them. A module whose file is
#: missing is skipped, so a fork can delete the ones it does not want.
BUILTIN = ("viewer", "ffxi", "tm", "jan", "fe", "fmo", "doc")


class Tool:
    """A page the game's tool port serves. `path` is relative to the port's
    root; the tool also owns every endpoint under it (`path`, `path.json`,
    `path/...`). A tool at "" owns whatever no other tool claims."""

    def __init__(self, id, label, path="", about="", panel=False, perm=None):
        self.id, self.label, self.path, self.about = id, label, path, about
        #: a page the panel itself draws (the PML preview), not the game's port
        self.panel = panel
        #: a panel page keeps the permission it always had ("pml")
        self.perm = perm

    def owns(self, sub):
        if self.panel:
            return False
        p = self.path
        return p == "" or sub == p or sub.startswith(p + "/") or sub.startswith(p + ".")


class Action:
    """Something the panel asks the game to do. `run(module, params, actor)`
    returns (ok, message). `kind` lets the core find the right action for a
    job: "announce" is used by the maintenance warning."""

    def __init__(self, id, label, run, kind="", fields=(), about="", danger=False):
        self.id, self.label, self.run, self.kind = id, label, run, kind
        self.fields, self.about, self.danger = list(fields), about, danger


def core_project():
    """The compose project of the core stack, for "{core}" in container names
    (POL_ADMIN_CORE_PROJECT; a checkout of the public repo is "openlobby")."""
    return os.environ.get("POL_ADMIN_CORE_PROJECT", "").strip() or "openlobby"


def core_off():
    """True when the panel runs without the PlayOnline core (POL_ADMIN_CORE=none):
    one game's own panel, with no PlayOnline accounts, news or Viewer. The
    built-in PlayOnline title modules then load only when POL_ADMIN_MODULES
    names them."""
    return os.environ.get("POL_ADMIN_CORE", "").strip().lower() == "none"


class HelpDesk:
    """Players' help requests from inside a game, answered from the Inbox
    (adminhelp.py has the shapes). Each function takes the module first:

        list(m, open_only) -> [thread row]       thread(m, id) -> thread
        reply(m, id, text, close, actor) -> (ok, message)
        close(m, id, actor) -> (ok, message)

    thread() raises LookupError for an id that is not there."""

    def __init__(self, list, thread, reply, close):
        self.list, self.thread, self.reply, self.close = list, thread, reply, close


class Service:
    """Containers and ports that run together; one restart button. A container
    name may say "{core}" for the core stack's compose project, and may be
    "a|b" when a deployment names it either way (the server check uses
    whichever exists)."""

    def __init__(self, containers, ports=None, name="", warning=""):
        self._containers = list(containers)
        self.ports = dict(ports or {})
        self.name, self.warning = name, warning

    @property
    def containers(self):
        core = core_project()
        return [c.replace("{core}", core) for c in self._containers]


class Module:
    def __init__(self, key, title, short="", platform="", kind="game",
                 tools=(), services=None, live=None, actions=(), content_ids=(),
                 about="", markers=(), present=None, self_described=False, calendar=None,
                 help=None, player=None, players=None, accounts=""):
        self.key, self.title, self.short = key, title, short or title
        self.platform, self.kind, self.about = platform, kind, about
        self.tools = list(tools)
        self.services = services
        self.live = live
        #: fn(module, lo, hi) -> what the game has scheduled (sources.eventcal and friends)
        self.calendar = calendar
        #: HelpDesk: players' help requests, in the Inbox (adminhelp.py)
        self.help = help
        #: fn(module, who) -> what the game knows of one PlayOnline account (adminplayers.py)
        self.player = player
        #: fn(module, q, limit) -> [{"id", "label", "detail", "online"}]: the player picker
        #: (an action field of type "player"); see find_players()
        self.players = players
        #: the account system its players' logins belong to: the key game_link uses for them
        #: (one shared by several games, when they sign in with the same login); the
        #: module key when the game has none of its own. The People pages link logins under it.
        self.accounts = accounts or key
        self.actions = list(actions)
        self.content_ids = list(content_ids)
        #: live-session markers (live_sessions.py) that mean this game runs here
        self.markers = list(markers)
        #: overrides available(): True for a module the panel itself serves
        self.present = present
        #: described by the game itself (adminmodules.remote), not a file
        self.remote = False
        #: a file for a game that also describes itself: with its URL set, the game's own
        #: description is used and this file keeps only its services (the host's ops
        #: runner reads those from the file) and stands in while the game does not answer
        self.self_described = self_described
        #: where it came from: "built-in", "file", "game" or "legacy"
        self.origin = "built-in"

    # -- configuration ------------------------------------------------------ #
    def env(self, name, default=""):
        return os.environ.get("POL_ADMIN_GAME_%s_%s" % (self.key.upper(), name), default).strip()

    def tool_port(self):
        """(url, token) of the game's tool port, or None: the environment's, else the one
        stored in the panel (Add a game, or paired by the game's setup program)."""
        url = self.env("URL")
        if url:
            return url.rstrip("/"), self.env("TOKEN")
        got = _STORED.get(self.key)
        return (got[0].rstrip("/"), got[1]) if got else None

    def port_from(self):
        """Where tool_port() comes from: "env", "stored" or None."""
        if self.env("URL"):
            return "env"
        return "stored" if self.key in _STORED else None

    def available(self):
        """Whether this server runs the game, as far as the panel can tell."""
        if self.present is not None:
            return bool(self.present(self) if callable(self.present) else self.present)
        if self.tool_port() or self.env("LIVE"):
            return True
        if self.markers:
            try:
                import live_sessions
                return any(live_sessions.read_marker(m) is not None for m in self.markers)
            except Exception:                  # noqa: BLE001 -- no store, no marker
                return False
        return False

    # -- permissions -------------------------------------------------------- #
    def perm(self, sub=None):
        if sub is None:
            return "game.%s" % self.key
        t = next((t for t in self.tools if t.id == sub and t.perm), None)
        return t.perm if t else "game.%s.%s" % (self.key, sub)

    def perms(self):
        """{perm: label} for everything a moderator can be given here. A panel
        page's own permission (t.perm) belongs to the core list, not here."""
        out = {self.perm(): "See %s: status, who is playing, services" % self.title}
        for t in self.tools:
            if not t.perm:
                out[self.perm(t.id)] = "%s: %s" % (self.title, t.label)
        for a in self.actions:
            out.setdefault(self.perm(a.id), "%s: %s" % (self.title, a.label))
        if self.help is not None:
            out.setdefault(self.perm("help"), "%s: answer players' help requests" % self.title)
        if self.services:
            out[self.perm("serverlogs")] = "%s: its server logs (players' addresses hidden)" % self.title
        if self.remote:
            # a game that applies its own updates (docs/installs.md, "Updates")
            out.setdefault(self.perm("update"), "%s: update it, when it can update itself" % self.title)
        return out

    def tool_for(self, sub):
        """The tool that owns a path under /games/<key>/ (longest path wins)."""
        best = None
        for t in self.tools:
            if t.owns(sub) and (best is None or len(t.path) > len(best.path)):
                best = t
        return best

    def action(self, id_or_kind):
        return next((a for a in self.actions if a.id == id_or_kind), None) or \
            next((a for a in self.actions if a.kind == id_or_kind), None)

    # -- listing ------------------------------------------------------------ #
    def describe(self, can=lambda p: True):
        """What the panel's pages get about this module, cut to `can`."""
        return {
            "key": self.key, "title": self.title, "short": self.short,
            "platform": self.platform, "kind": self.kind, "about": self.about,
            "tools": [{"id": t.id, "label": t.label, "path": t.path, "about": t.about,
                       "panel": t.panel}
                      for t in self.tools if can(self.perm(t.id))],
            "actions": [{"id": a.id, "label": a.label, "kind": a.kind, "about": a.about,
                         "fields": a.fields, "danger": a.danger}
                        for a in self.actions if can(self.perm(a.id))],
            "services": [{"name": s.name or self.title, "containers": s.containers,
                          "ports": {str(k): v for k, v in s.ports.items()}}
                         for s in (self.services or [])],
            "has_tools": bool(self.tool_port()),
            "players": self.players is not None,
            "accounts": self.accounts,
            "content_ids": self.content_ids,
        }


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_ALL = None          # {key: Module}, every module found, in listing order
_PROBLEMS = []       # files that did not load, for the Modules settings page
_REMOTE_RETRY = 60.0
#: a game that describes itself is asked again this often, so a page or action it adds shows
#: without restarting the panel
_REDESCRIBE = 300.0
_remote_failed = {"at": 0.0, "keys": set()}
_loaded = {"at": 0.0}
#: fn() -> {key: (url, token)}: the games stored in the panel's database. Set by the panel
#: (admin.py sets adminpair.stored); unset, only the environment is read.
STORE = None
#: what STORE answered at the last load(); tool_port() reads it
_STORED = {}


def _note(msg):
    _PROBLEMS.append(msg)
    print("adminmodules: " + msg, file=sys.stderr)


def _accept(mod, where, out, origin="file"):
    if not isinstance(mod, Module):
        _note("%s: MODULE is not a Module; skipped" % where)
        return
    if not KEY_RE.match(mod.key or ""):
        _note("%s: key %r does not fit [a-z0-9]{1,16}; skipped" % (where, mod.key))
        return
    if mod.key in out:
        _note("%s: key %r is already taken; skipped" % (where, mod.key))
        return
    ids = [t.id for t in mod.tools] + [a.id for a in mod.actions]
    bad = [i for i in ids if not ID_RE.match(i or "")]
    bad += [t.path for t in mod.tools if not TOOL_PATH_RE.match(t.path) or ".." in t.path]
    if bad:
        _note("%s: %r do not fit; skipped" % (where, bad))
        return
    mod.origin = origin
    out[mod.key] = mod


def _load_file(path, out):
    name = "adminmodules_extra_" + re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0])
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    except Exception as exc:                   # noqa: BLE001 -- one bad file, not the panel
        _note("%s did not load: %r" % (path, exc))
        return
    _accept(getattr(m, "MODULE", None), path, out)


def _legacy_extras(out):
    """POL_ADMIN_GAMES_EXTRA (the JSON the Games section used to take): each
    entry becomes a tools-only module. Kept so existing compose files work."""
    raw = os.environ.get("POL_ADMIN_GAMES_EXTRA", "")
    if not raw.strip():
        return
    try:
        got = json.loads(raw)
    except ValueError as exc:
        _note("POL_ADMIN_GAMES_EXTRA is not JSON (%s); ignored" % exc)
        return
    for key, g in (got.items() if isinstance(got, dict) else ()):
        try:
            tools = [Tool(str(t["id"]), str(t["label"]), str(t.get("path", ""))) for t in g["tools"]]
            if not (isinstance(g["title"], str) and g["title"].strip() and tools):
                raise ValueError
            mod = Module(key, g["title"].strip()[:60], str(g.get("short") or key.upper())[:12],
                         tools=tools)
        except (KeyError, TypeError, AttributeError, ValueError):
            _note("POL_ADMIN_GAMES_EXTRA entry %r does not fit; skipped" % (key,))
            continue
        if key in out:
            # a built-in module of that key: the JSON may only name its tools
            if not out[key].tools:
                out[key].tools = tools
            continue
        _accept(mod, "POL_ADMIN_GAMES_EXTRA[%s]" % key, out, "legacy")


def _read_store():
    """Refresh _STORED from STORE. A store that cannot be read keeps what it last said."""
    global _STORED
    if STORE is None:
        _STORED = {}
        return
    try:
        got = STORE() or {}
    except Exception as exc:                   # noqa: BLE001 -- the database is away
        _note("the games added in the panel could not be read (%s)" % exc.__class__.__name__)
        return
    _STORED = {k: (str(v[0]), str(v[1] or "")) for k, v in got.items()
               if KEY_RE.match(k or "") and v and v[0]}


def _ports():
    """[(key, url, token)] of every game with a tool URL: the environment's, then the
    stored ones the environment does not name."""
    pre = "POL_ADMIN_GAME_"
    out, seen = [], set()
    for name, url in sorted(os.environ.items()):
        if not (name.startswith(pre) and name.endswith("_URL") and url.strip()):
            continue
        key = name[len(pre):-len("_URL")].lower()
        if not KEY_RE.match(key):
            continue
        seen.add(key)
        out.append((key, url.strip(), os.environ.get(pre + key.upper() + "_TOKEN", "").strip()))
    for key in sorted(_STORED):
        if key not in seen:
            out.append((key, _STORED[key][0].strip(), _STORED[key][1].strip()))
    return out


def port_of(key):
    """(url, token) of the game `key`'s admin port, the environment's or the stored one, even
    while the game does not answer (it may be restarting to update itself); None when it has
    none."""
    _read_store()
    for k, url, token in _ports():
        if k == key:
            return url.rstrip("/"), token
    return None


def _remotes(out):
    """Games with a tool URL and no module: ask each to describe itself
    (adminmodules.remote, the OpenLobby admin protocol)."""
    _remote_failed["keys"] = set()
    _remote_failed["at"] = time.time()
    for key, url, token in _ports():
        file_mod = out.get(key)
        if file_mod is not None and not file_mod.self_described:
            continue
        try:
            from adminmodules import remote
            mod = remote.build(key, url.strip().rstrip("/"), token)
        except Exception as exc:               # noqa: BLE001 -- the game is down or not one
            _remote_failed["keys"].add(key)
            _note("%s: the game at %s did not describe itself (%s); %strying again in a minute"
                  % (key, url, getattr(exc, "reason", None) or exc,
                     "its module file stands in; " if file_mod is not None else ""))
            continue
        if file_mod is not None:
            out.pop(key)
            mod.services = file_mod.services
            mod.markers = file_mod.markers
            mod.calendar = mod.calendar or file_mod.calendar
            mod.help = mod.help or file_mod.help
            mod.player = mod.player or file_mod.player
            mod.players = mod.players or file_mod.players
            if mod.accounts == key:
                mod.accounts = file_mod.accounts
        _accept(mod, "the game at %s" % url, out, "game")


def load(reload=False):
    """{key: Module} for every module found (enabled or not)."""
    global _ALL
    with _lock:
        if _ALL is not None and not reload:
            return _ALL
        del _PROBLEMS[:]
        _read_store()
        out = {}
        want = _wanted() if core_off() else None
        for key in BUILTIN:
            if core_off() and (want is None or key not in want):
                continue                        # a PlayOnline title, not asked for
            try:
                m = importlib.import_module("adminmodules." + key)
            except ImportError as exc:
                if getattr(exc, "name", "") == "adminmodules." + key:
                    continue                    # deleted on purpose
                _note("built-in %s did not load: %r" % (key, exc))
                continue
            except Exception as exc:           # noqa: BLE001
                _note("built-in %s did not load: %r" % (key, exc))
                continue
            _accept(getattr(m, "MODULE", None), "adminmodules/%s.py" % key, out, "built-in")
        extra = os.environ.get("POL_ADMIN_MODULES_DIR", "").strip()
        if extra and os.path.isdir(extra):
            for fn in sorted(os.listdir(extra)):
                if fn.endswith(".py") and not fn.startswith("_"):
                    _load_file(os.path.join(extra, fn), out)
        _legacy_extras(out)
        _remotes(out)
        _ALL = out
        _loaded["at"] = time.time()
        return out


def found():
    """Every module found, shown or not, for the owner's Modules page."""
    shown = enabled()
    return [{"key": k, "title": m.title, "origin": m.origin, "shown": k in shown,
             "port": m.port_from(),
             "tools": len([t for t in m.tools if not t.panel]), "has_tools": bool(m.tool_port()),
             "actions": len(m.actions), "live": m.live is not None}
            for k, m in load().items()]


def problems():
    load()
    return list(_PROBLEMS)


def _wanted():
    """The keys an explicit POL_ADMIN_MODULES list names, with the games stored in the
    panel added (the owner added those on purpose); None for "*"."""
    raw = os.environ.get("POL_ADMIN_MODULES", "*").strip() or "*"
    if raw == "*":
        return None
    return {k.strip() for k in raw.split(",") if k.strip()} | set(_STORED)


def refresh():
    """Load the modules again now (a game was added, removed or paired)."""
    load(reload=True)
    _AVAIL["keys"] = None


_AVAIL = {"at": 0.0, "keys": None}


def enabled():
    """{key: Module} this server shows. An explicit POL_ADMIN_MODULES list is
    taken as it is; "*" keeps the modules that find their game (cached 30 s).
    A game that did not describe itself is asked again after a minute, and every game that
    describes itself every five minutes."""
    stale = (_ALL is not None and time.time() - _loaded["at"] > _REDESCRIBE
             and any(m.remote for m in _ALL.values()))
    if stale or (_remote_failed["keys"] and time.time() - _remote_failed["at"] > _REMOTE_RETRY):
        load(reload=True)
        _AVAIL["keys"] = None
    mods = load()
    want = _wanted()
    if want is not None:
        return {k: m for k, m in mods.items() if k in want}
    now = time.time()
    if _AVAIL["keys"] is None or now - _AVAIL["at"] > 30:
        _AVAIL["keys"] = {k for k, m in mods.items() if m.available()}
        _AVAIL["at"] = now
    return {k: m for k, m in mods.items() if k in _AVAIL["keys"]}


def get(key):
    return enabled().get(key)


def all_perms():
    """{perm: label} over every module found, so a moderator's grants survive a
    module being turned off for a while."""
    out = {}
    for m in load().values():
        out.update(m.perms())
    return out


def expand_legacy(perms):
    """The old per-game grant `games_<key>` -> every permission of that game it
    stood for: not its server logs, which came later and are given on purpose."""
    out = []
    mods = load()
    for p in perms:
        if p.startswith("games_") and p[6:] in mods:
            out.extend(k for k in mods[p[6:]].perms() if not k.endswith(".serverlogs"))
        else:
            out.append(p)
    return out


def can_see(key, can):
    """`can(perm)`: may this caller see module `key` at all? Any permission in
    the game counts, so a moderator given only one tool still reaches it."""
    m = load().get(key)
    if not m:
        return False
    return any(can(p) for p in m.perms()) or any(can(t.perm) for t in m.tools if t.perm)


PLAYERS_MAX = 50


def find_players(key, q, limit=20):
    """The game's player search for the picker, cleaned: at most `limit` (1-50) rows of
    {"id", "label", "detail", "online": True/False/None}, strings trimmed. Raises
    LookupError when the game has no search; whatever the game raises otherwise."""
    m = enabled().get(key)
    if m is None or m.players is None:
        raise LookupError("no player search")
    limit = max(1, min(PLAYERS_MAX, int(limit)))
    out = []
    for r in m.players(m, " ".join(str(q or "").split())[:64], limit) or []:
        if not isinstance(r, dict):
            continue
        pid = str(r.get("id") if r.get("id") is not None else "").strip()[:64]
        if not pid:
            continue
        online = r.get("online")
        out.append({"id": pid, "label": " ".join(str(r.get("label") or pid).split())[:80],
                    "detail": " ".join(str(r.get("detail") or "").split())[:120],
                    "online": online if isinstance(online, bool) else None})
        if len(out) >= limit:
            break
    return out


def listing(can):
    """The enabled modules this caller may see, described for the panel."""
    mods = enabled()
    order = [k for k in BUILTIN if k in mods] + sorted(
        (k for k in mods if k not in BUILTIN), key=lambda k: mods[k].title.lower())
    return [mods[k].describe(can) for k in order if can_see(k, can)]


# --------------------------------------------------------------------------- #
# live status
# --------------------------------------------------------------------------- #
LIVE_TTL = float(os.environ.get("POL_ADMIN_LIVE_TTL", "8"))
_live_cache = {}
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="modlive")


def _read_live(m):
    if m.live is None:
        return None
    t0 = time.time()
    try:
        got = m.live(m)
    except Exception as exc:                   # noqa: BLE001 -- a module's fault is its own
        got = {"up": None, "error": "status unreadable: %s" % (exc.__class__.__name__,)}
    if got is not None:
        got.setdefault("at", t0)
    return got


def live(key, fresh=False):
    """The module's live status (see sources.status), cached LIVE_TTL seconds."""
    m = load().get(key)
    if m is None:
        return None
    now = time.time()
    hit = _live_cache.get(key)
    if hit and not fresh and now - hit[0] < LIVE_TTL:
        return hit[1]
    got = _read_live(m)
    _live_cache[key] = (now, got)
    return got


def live_all(keys, timeout=6.0):
    """{key: status} for several modules at once, read in parallel."""
    futs = {k: _pool.submit(live, k) for k in keys}
    out = {}
    for k, f in futs.items():
        try:
            out[k] = f.result(timeout=timeout)
        except Exception:                      # noqa: BLE001
            out[k] = {"up": None, "error": "status took too long"}
    return out


# --------------------------------------------------------------------------- #
# services, for the host's ops runner
# --------------------------------------------------------------------------- #
def service_groups(keys=None):
    """Server-check groups for the modules' services: one group per module
    (its containers merged), id = module key; the shape adminops.py describes
    for a check."""
    out = []
    for k, m in load().items():
        if keys is not None and k not in keys:
            continue
        if not m.services:
            continue
        ctrs, ports, warn = [], {}, ""
        for s in m.services:
            ctrs += [c for c in s.containers if c not in ctrs]
            ports.update(s.ports)
            warn = warn or s.warning
        out.append({"id": k, "name": m.title, "containers": ctrs, "ports": ports,
                    "warning": warn or "Everyone in %s is disconnected." % m.title})
    return out
