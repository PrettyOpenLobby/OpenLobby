#!/usr/bin/env python3
"""Run the OpenLobby core and any set of games as one compose project.

Every game repository is a compose override applied on top of OpenLobby's
docker-compose.yml, in OpenLobby's project, from a checkout beside this one.
This tool reads the game catalog (games/*.json), remembers which games you
enabled (the .games file in this checkout), and assembles the one
`docker compose` command that brings them up together: the -f list in the
right order, the project directory, the env files, POL_TITLES, and the image
chain the title plugins need. docs/games.md explains the design.

    python tools/stack.py list
    python tools/stack.py add fe [--yes]         clone a game beside openlobby
    python tools/stack.py enable tm,jan          record the games to run
    python tools/stack.py disable jan
    python tools/stack.py check                  checkouts and data present
    python tools/stack.py check --json --all     the same for every game, as JSON
    python tools/stack.py ps [--json]            which games have running containers
    python tools/stack.py volumes [ffxi|--all]   create and fill the games' data volumes
    python tools/stack.py compose up -d --build  run docker compose
    python tools/stack.py compose --dry-run up -d --build   print, run nothing
    python tools/stack.py channel [--json]       which update channel this install follows
    python tools/stack.py channel dev --registry HOST/PATH [--source URL]
    python tools/stack.py channel stable
    python tools/stack.py dev-code --registry HOST/PATH [--token-file FILE|-] [--source URL]
                                                 make an access code for the dev channel

Standard library only. Works from any directory.
"""
import fnmatch
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(ROOT)

# The dev channel's image map is shared with the deployer, whose image holds only
# services/deployer/ (docs/deployer.md, "Dev channel").
sys.path.insert(0, os.path.join(ROOT, "services", "deployer"))
import devchannel  # noqa: E402
CATALOG_DIR = os.path.join(ROOT, "games")
STATE_FILE = os.path.join(ROOT, ".games")

#: Variables this tool computes. A value for them in an env file is ignored.
COMPUTED = ("POL_TITLES", "POL_TITLE_BASE_IMAGE")

#: the services the title chain's last link runs (docs/games.md)
TITLE_SERVICES = ("login", "authsess")

#: docker compose options (before the subcommand) that take a value
_COMPOSE_VALUE_OPTS = {"-f", "--file", "-p", "--project-name", "--profile",
                       "--env-file", "--project-directory", "--ansi",
                       "--parallel", "--progress"}

#: subcommands that build images, and so need the chain built first
_BUILDING = {"up", "build", "create"}


class StackError(Exception):
    pass


# ---------------------------------------------------------------- catalog

def load_catalog(catalog_dir=CATALOG_DIR):
    """{key: entry} for every games/*.json, in chain order."""
    entries = []
    for path in sorted(glob.glob(os.path.join(catalog_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        if entry.get("schema") != 1:
            raise StackError("%s: unknown schema %r" % (path, entry.get("schema")))
        if os.path.splitext(os.path.basename(path))[0] != entry.get("key"):
            raise StackError("%s: key %r does not match the file name"
                             % (path, entry.get("key")))
        entries.append(entry)
    if not any(e["key"] == "core" for e in entries):
        raise StackError("%s has no core.json" % catalog_dir)
    entries.sort(key=lambda e: (e["order"], e["key"]))
    return {e["key"]: e for e in entries}


def games_of(catalog):
    return [e for e in catalog.values() if e["kind"] == "game"]


def parse_keys(raw_list, catalog):
    """'tm,jan' 'fe' -> ['tm', 'jan', 'fe'], refusing unknown keys."""
    keys = []
    for raw in raw_list:
        for k in raw.split(","):
            k = k.strip().lower()
            if not k:
                continue
            if k not in catalog or catalog[k]["kind"] != "game":
                known = ", ".join(e["key"] for e in games_of(catalog))
                raise StackError("unknown game %r (known: %s)" % (k, known))
            if k not in keys:
                keys.append(k)
    if not keys:
        raise StackError("name at least one game")
    return keys


def ordered(keys, catalog):
    """`keys` in chain order (the catalog's `order` field)."""
    return [e["key"] for e in catalog.values() if e["key"] in keys]


# ------------------------------------------------------------------ state

def read_state(path=STATE_FILE):
    if not os.path.exists(path):
        return []
    keys = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and line not in keys:
                keys.append(line)
    return keys


def write_state(keys, path=STATE_FILE):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Games tools/stack.py runs with the core, one key per line.\n"
                "# Written by `stack.py enable` / `disable`; see docs/games.md.\n")
        for k in keys:
            f.write(k + "\n")


def enabled(catalog, state=STATE_FILE):
    keys = read_state(state)
    unknown = [k for k in keys if k not in catalog or catalog[k]["kind"] != "game"]
    if unknown:
        raise StackError("%s names unknown games: %s" % (state, ", ".join(unknown)))
    return ordered(keys, catalog)


# -------------------------------------------------------------- env files

def checkout_of(entry, root=ROOT, parent=PARENT):
    if entry["kind"] == "core":
        return root
    return os.path.join(parent, entry["checkout"])


def parse_env(path):
    """KEY=VALUE lines of a dotenv file (the simple subset the .env.example
    files use: comments, blank lines, optional `export ` and quotes)."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            out[key] = value
    return out


def env_files(catalog, keys, root=ROOT, parent=PARENT):
    """The core's .env, then each enabled game's .env, where present."""
    files = []
    for entry in [catalog["core"]] + [catalog[k] for k in keys]:
        path = os.path.join(checkout_of(entry, root, parent), ".env")
        if os.path.isfile(path):
            files.append(path)
    return files


def env_values(files, environ=None):
    """What docker compose sees: later files override earlier ones, and the
    process environment overrides every file."""
    vals = {}
    for path in files:
        vals.update(parse_env(path))
    if environ:
        vals.update(environ)
    return vals


def env_warnings(files):
    """Traps in the env file chain: an empty KEY= in a later file overrides
    a value an earlier file set, and values this tool replaces."""
    warnings = []
    seen = {}
    for path in files:
        for key, value in parse_env(path).items():
            if key in COMPUTED and value:
                warnings.append("%s sets %s, which stack.py computes; that value is "
                                "ignored" % (path, key))
            if not value and seen.get(key):
                warnings.append("%s has an empty %s=, which overrides %s=%s from %s; "
                                "delete the line to keep the earlier value"
                                % (path, key, key, seen[key][1], seen[key][0]))
            if value:
                seen[key] = (path, value)
    return warnings


# ------------------------------------------------------------ the command

def _with_tag(image, tag_var, vals):
    return "%s:%s" % (image, vals.get(tag_var) or "latest")


def plan(catalog, keys, compose_args, ghcr=False, root=ROOT, parent=PARENT,
         environ=None, prebuild=True, channel=None, override_path=None):
    """Everything `stack.py compose` would run, as data:

      {"files": [...], "env_files": [...], "titles": "tmtitle,...",
       "base": "<image the last title link builds on>",
       "override": "<the dev channel's compose file, or empty>",
       "steps": [{"argv": [...], "env": {...}, "why": "..."}, ...]}

    The last step is the command asked for; any before it build the core
    image and the title chain it stands on. On the dev channel (`channel`,
    as devchannel.read_channel gives it) a swapped image is also the title
    chain's base where it replaces one, and "override" sets it for the
    services that run it; that file goes last in the -f list, at
    override_path (the caller writes it there).
    """
    keys = ordered(keys, catalog)
    core = catalog["core"]
    games = [catalog[k] for k in keys]
    efiles = env_files(catalog, keys, root, parent)
    vals = env_values(efiles, environ)
    swap, override, rows = {}, "", []
    if channel and channel.get("channel") == "dev":
        rows = devchannel.image_rows(catalog, channel, ghcr, keys=keys)
        swap = devchannel.applied(rows)

    core_files = [os.path.join(root, f) for f in core["compose"]]
    if ghcr:
        core_files += [os.path.join(root, f) for f in core["compose_ghcr"]]
        base0 = swap.get(core["images"][0]["ghcr"]) or \
            _with_tag(core["images"][0]["ghcr"], core["images"][0]["tag_var"], vals)
    else:
        base0 = vals.get("OPENLOBBY_IMAGE") or "openlobby:latest"

    # The title chain: each title image is FROM the one before it. The last
    # link is what login and authsess run; the earlier ones are built first.
    links = [g for g in games if g.get("title")]
    link_image = []
    prebuilds = []
    prev = base0
    for i, g in enumerate(links):
        t = g["title"]
        if ghcr and i == 0 and t.get("ghcr_image"):
            image = swap.get(t["ghcr_image"]) or _with_tag(t["ghcr_image"], t["ghcr_tag_var"], vals)
        else:
            image = t["image"]
            if i < len(links) - 1:
                prebuilds.append((g, prev))
        link_image.append(image)
        prev = image
    final_base = link_image[-2] if len(links) >= 2 else base0
    titles = ",".join(g["title"]["module"] for g in links)
    if rows:
        # With titles, login and authsess run the chain's last link, which stands on the
        # swapped images through base0 and the first link: the core's dev image must not
        # take them over. A published first link that is also the last one is set by its
        # own compose file, so the dev build of it is set for them here.
        skip, extra = (), {}
        if links:
            skip = TITLE_SERVICES
            t0 = links[0]["title"]
            if ghcr and len(links) == 1 and t0.get("ghcr_image") and swap.get(t0["ghcr_image"]):
                extra = {svc: swap[t0["ghcr_image"]] for svc in TITLE_SERVICES}
        override = devchannel.override_text(rows, skip=skip, extra=extra)

    files = list(core_files)
    for g in games:
        d = checkout_of(g, root, parent)
        files += [os.path.join(d, f) for f in g["compose"]]
        if ghcr:
            # a published title image is a link built on the core alone, so
            # its override (which swaps login and authsess to it) only fits
            # as the first link
            if g.get("title") and g["title"].get("ghcr_image") and g is not links[0]:
                continue
            files += [os.path.join(d, f) for f in g["compose_ghcr"]]
    if override:
        files.append(override_path or os.path.join(root, ".channel-compose.yml"))

    head = ["docker", "compose", "--project-directory", root]
    for path in efiles:
        head += ["--env-file", path]

    steps = []
    if prebuild and _subcommand(compose_args) in _BUILDING and (links or games):
        if not ghcr:
            argv = head[:]
            for f in core_files:
                argv += ["-f", f]
            steps.append({"argv": argv + ["build", "dns"], "env": {},
                          "why": "the core image every game image is built on"})
        for g, base in prebuilds:
            argv = head[:]
            for f in core_files:
                argv += ["-f", f]
            argv += ["-f", os.path.join(checkout_of(g, root, parent), g["title"]["compose"])]
            steps.append({"argv": argv + ["build", "login"],
                          "env": {"POL_TITLE_BASE_IMAGE": base,
                                  "POL_TITLES": g["title"]["module"]},
                          "why": "title link %s (%s on %s)" % (g["key"], g["title"]["image"], base)})

    argv = head[:]
    for f in files:
        argv += ["-f", f]
    argv += list(compose_args)
    env = {"POL_TITLES": titles}
    if links:
        env["POL_TITLE_BASE_IMAGE"] = final_base
    steps.append({"argv": argv, "env": env, "why": "the command asked for"})
    return {"files": files, "env_files": efiles, "titles": titles,
            "base": final_base, "override": override, "steps": steps}


def _subcommand(args):
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if a in _COMPOSE_VALUE_OPTS:
                skip = True
            continue
        return a
    return None


def show(step):
    env = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in step["env"].items())
    cmd = " ".join(shlex.quote(a) for a in step["argv"])
    return (env + " " + cmd) if env else cmd


# ------------------------------------------------------------------ check

def check_game(g, ghcr=False, root=ROOT, parent=PARENT, docker=None):
    """(rows, missing) for one game. rows are (level, text) as check()
    returns them; missing lists what the game still needs, one entry per
    absent path or volume: {"id", "path", "hint", "required", "source",
    "tool"}, and for a volume "fill": whether ensure_volumes() sets it up.
    A path is relative to the OpenLobby checkout (../<checkout>/...)."""
    rows, missing = [], []
    k = g["key"]
    d = checkout_of(g, root, parent)
    if not os.path.isdir(d):
        return [("fail", "%s: no checkout at %s (stack.py add %s)" % (k, d, k))], missing
    rel = "../" + g["checkout"]
    rows.append(("ok", "%s: checkout %s" % (k, d)))
    for f in g["compose"] + (g["compose_ghcr"] if ghcr else []):
        if not os.path.isfile(os.path.join(d, f)):
            rows.append(("fail", "%s: %s is missing from the checkout" % (k, f)))
    if g.get("env_example") and os.path.isfile(os.path.join(d, g["env_example"])) \
            and not os.path.isfile(os.path.join(d, ".env")):
        rows.append(("warn", "%s: no .env (copy %s if you want to change a setting)"
                     % (k, g["env_example"])))
    for item in g["data"]:
        if ghcr and item.get("build_only"):
            continue
        if not item["paths"]:
            continue
        absent = [p for p in item["paths"] if not glob.glob(os.path.join(d, p))]
        if not absent:
            rows.append(("ok", "%s: %s present" % (k, item["id"])))
            continue
        level = "fail" if item["required"] else "warn"
        rows.append((level, "%s: %s missing (%s); run in %s: %s"
                     % (k, item["id"], ", ".join(absent), d, item["tool"])))
        if item["source"].startswith("none"):
            how = "Make them with: %s (run in %s)" % (item["tool"], rel)
        else:
            how = "From %s: %s (run in %s)" % (item["source"], item["tool"], rel)
        for p in absent:
            missing.append({"id": item["id"], "path": rel + "/" + p,
                            "hint": "%s %s" % (item["description"], how),
                            "required": bool(item["required"]),
                            "source": item["source"], "tool": item["tool"]})
    for vol in g.get("volumes", []):
        if not docker:
            rows.append(("warn", "%s: volume %s not checked (no docker on PATH)"
                         % (k, vol["name"])))
            continue
        r = subprocess.run(_docker_argv(docker) + ["volume", "inspect", vol["name"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            rows.append(("ok", "%s: volume %s present" % (k, vol["name"])))
        else:
            level = "fail" if vol.get("required") else "warn"
            setup = " && ".join(vol["setup"])
            fill = vol.get("fill") if vol.get("required") and not fill_problem(vol) else None
            if fill:
                how = ("python tools/stack.py volumes %s sets it up (a download of about %d MB "
                       "the first time); by hand: %s" % (k, fill_size(g, fill["image"]), setup))
                hint = ("%s Installing, turning on or updating the game sets it up by itself "
                        "(a download of about %d MB the first time)."
                        % (vol.get("description", ""), fill_size(g, fill["image"])))
            else:
                how = "create it with: %s" % setup
                hint = "%s Create it with: %s" % (vol.get("description", ""), setup)
            rows.append((level, "%s: volume %s missing; %s" % (k, vol["name"], how)))
            missing.append({"id": vol["name"], "path": "volume " + vol["name"],
                            "hint": hint,
                            "required": bool(vol.get("required")), "source": "docker volume",
                            "tool": setup, "fill": bool(fill)})
    return rows, missing


def check(catalog, keys, ghcr=False, root=ROOT, parent=PARENT, docker=None):
    """[(level, text)] for the enabled games; level is ok, warn or fail."""
    out = []
    if docker is None:
        docker = shutil.which("docker")
    for k in keys:
        out += check_game(catalog[k], ghcr, root, parent, docker)[0]
    for w in env_warnings(env_files(catalog, keys, root, parent)):
        out.append(("warn", w))
    return out


def check_report(catalog, keys, on, ghcr=False, root=ROOT, parent=PARENT, docker=None):
    """check() as data, for `check --json`: one entry per game in `keys`.
    data_ok is None for a game with no checkout, else whether nothing it
    requires is missing. ok is false when an installed game lacks required
    data or an enabled game has no checkout."""
    if docker is None:
        docker = shutil.which("docker")
    games = []
    for k in keys:
        g = catalog[k]
        installed = os.path.isdir(checkout_of(g, root, parent))
        rows, missing = check_game(g, ghcr, root, parent, docker)
        games.append({
            "key": k, "name": g["name"], "admin_module": g.get("admin_module"),
            "installed": installed, "enabled": k in on,
            "data_ok": (not any(level == "fail" for level, _ in rows)) if installed else None,
            "missing": missing,
            "rows": [[level, text] for level, text in rows],
            "ports": g.get("ports", []), "services": g.get("services", []),
            "notes": g.get("notes", ""),
        })
    present = [k for k in on if os.path.isdir(checkout_of(catalog[k], root, parent))]
    return {"ok": all(x["data_ok"] is not False for x in games)
                  and all(x["installed"] for x in games if x["enabled"]),
            "ghcr": ghcr, "games": games,
            "warnings": env_warnings(env_files(catalog, present, root, parent))}


def parse_ps(text):
    """`docker compose ps --format json` output: one JSON object per line
    (current Compose) or one JSON array (older releases)."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def ps_report(catalog, rows):
    """{service: state} from compose ps rows, and per game and for the core
    whether any of its services (profiles included) has a running
    container."""
    states = {}
    for r in rows:
        svc = r.get("Service")
        if svc and states.get(svc) != "running":
            states[svc] = r.get("State", "")
    games = {}
    for key, e in catalog.items():
        svcs = list(e.get("services", [])) + [s for ss in e.get("profiles", {}).values() for s in ss]
        mine = {s: states[s] for s in svcs if s in states}
        games[key] = {"running": any(v == "running" for v in mine.values()), "services": mine}
    core = games.pop("core")
    return {"ok": True, "services": states, "core": core, "games": games}


# ---------------------------------------------------------------- volumes
#
# A required volume with a "fill" block is created and filled here: Docker
# copies an image's files into an empty named volume the first time a
# container mounts it at a path the image has content in, so mounting it at
# fill.path in a container of fill.image that runs `true` is the whole fill.
# Every docker call is an argument list; the image, the path and the name
# come from the catalog and are checked by fill_problem() before any reaches
# docker. A volume that already holds files is never written to: the copy
# only happens into an empty one, and this code only mounts it to fill it
# when it found it empty or created it.

_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_REF = re.compile(
    r"^(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/)?"            # registry host[:port]/
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"                        # repository path
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"                      # :tag
    r"(?:@sha256:[0-9a-f]{64})?$")                                  # @digest
_IMAGE_PATH = re.compile(r"^(?:/[A-Za-z0-9_.-]+)+$")
_EXPECT = re.compile(r"^[A-Za-z0-9_.*?-]{1,64}$")
_FILL_KEYS = {"image", "path", "expect", "size_mb", "what"}

#: where the file count mounts a volume (read only, never filled from the image)
_LOOK = "/openlobby-look"

PULL_TIMEOUT = 3600
FILL_TIMEOUT = 1800
LOOK_TIMEOUT = 300


def fill_problem(vol):
    """None when a catalog volume's "fill" block can be used, else why not."""
    name = vol.get("name")
    if not isinstance(name, str) or not _VOLUME_NAME.match(name):
        return "volume name %r is not a Docker volume name" % (name,)
    f = vol.get("fill")
    if not isinstance(f, dict):
        return "fill is not an object"
    extra = set(f) - _FILL_KEYS
    if extra:
        return "fill has unknown fields: %s" % ", ".join(sorted(extra))
    image = f.get("image")
    if not isinstance(image, str) or not _IMAGE_REF.match(image) or len(image) > 255:
        return "fill.image %r is not an image reference (registry/name:tag)" % (image,)
    path = f.get("path")
    if (not isinstance(path, str) or not _IMAGE_PATH.match(path)
            or any(p in (".", "..") for p in path.split("/"))):
        return "fill.path %r is not an absolute path in the image" % (path,)
    if not isinstance(f.get("expect"), str) or not _EXPECT.match(f["expect"]):
        return "fill.expect %r is not a file name pattern such as *.dat" % (f.get("expect"),)
    size = f.get("size_mb")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size < 1000000:
        return "fill.size_mb %r is not a whole number of megabytes" % (size,)
    what = f.get("what", "data")
    if not isinstance(what, str) or not re.match(r"^[A-Za-z0-9 ,'()-]{1,40}$", what):
        return "fill.what %r is not a short plain phrase" % (what,)
    return None


def fill_size(g, image):
    """Megabytes the required volumes of game `g` filled from `image` hold
    together: about what downloading that image costs."""
    return sum(v["fill"].get("size_mb", 0) for v in g.get("volumes", [])
               if v.get("required") and isinstance(v.get("fill"), dict)
               and v["fill"].get("image") == image)


def _docker_argv(docker):
    return [docker] if isinstance(docker, str) else list(docker)


def _call(dock, args, timeout=LOOK_TIMEOUT):
    """(exit code, stdout, stderr) of one docker command; never raises."""
    try:
        r = subprocess.run(dock + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
    except OSError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %d seconds" % timeout
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else "no message"


def docker_trouble(text):
    """A plain sentence for the failures anyone can fix (no Docker, a full
    disk), else None."""
    t = (text or "").lower()
    if "no space left on device" in t or "not enough space" in t or "disk quota" in t:
        return ("There is not enough free disk space for it. Free some space (on Docker "
                "Desktop, or give Docker more in its Settings, Resources), then try again.")
    if ("cannot connect to the docker daemon" in t or "error during connect" in t
            or "is the docker daemon running" in t or "docker desktop is not running" in t
            or "the system cannot find the file specified" in t and "pipe" in t):
        return "Docker is not running. Start it, then try again."
    return None


def pull_trouble(text, image):
    """Why a download failed, as a sentence."""
    plain = docker_trouble(text)
    if plain:
        return plain
    t = (text or "").lower()
    host = image.split("/", 1)[0] if "/" in image and ("." in image.split("/", 1)[0]
                                                       or ":" in image.split("/", 1)[0]) \
        else "Docker Hub"
    if any(s in t for s in ("denied", "unauthorized", "manifest unknown", "not found",
                            "no such image", "does not exist")):
        return ("The download was refused: the image %s is not available (the game's "
                "catalog entry may name one that has moved)." % image)
    if any(s in t for s in ("timeout", "timed out", "no such host", "dial tcp", "tls handshake",
                            "connection refused", "network is unreachable", "lookup ",
                            "connection reset", "unexpected eof")):
        return ("This computer could not reach %s. Check its internet connection, then try "
                "again." % host)
    return "Docker said: %s" % _last_line(text)


def _look(dock, name, image):
    """(file names at the top of volume `name`, None) or (None, why). The
    volume is mounted read only with volume-nocopy, so looking never fills
    it."""
    mount = "type=volume,source=%s,target=%s,readonly,volume-nocopy" % (name, _LOOK)
    rc, out, err = _call(dock, ["run", "--rm", "--network", "none", "--mount", mount,
                                image, "ls", "-A", _LOOK])
    if rc != 0:
        return None, docker_trouble(err) or "Docker could not look inside it: %s" % _last_line(err)
    return [l.strip() for l in out.splitlines() if l.strip()], None


def ensure_volumes(catalog, keys, docker=None, say=None):
    """Create and fill the required volumes with a "fill" block of the games
    in `keys`. A volume that is missing is created, one that exists but is
    empty is filled, one that holds files is left alone. Returns one result
    per volume: {"game", "volume", "state": "present" | "filled" | "failed",
    "files", "why"}. `say` gets the progress lines (default print)."""
    if say is None:
        def say(text):
            print(text, flush=True)
    if docker is None:
        docker = shutil.which("docker")
        if not docker:
            raise StackError("Docker is not installed (there is no docker command), so the "
                             "games' data cannot be set up.")
    dock = _docker_argv(docker)
    results = []
    images = {}          # image -> None once present here, or why it could not be
    for k in ordered(keys, catalog):
        g = catalog[k]
        for vol in g.get("volumes", []):
            if vol.get("required") and vol.get("fill") is not None:
                results.append(_ensure_volume(g, vol, dock, images, say))
    if not results:
        say("No data to set up for %s." % (", ".join(catalog[k]["name"] for k in keys)
                                          if keys else "the core"))
    return results


def _ensure_volume(g, vol, dock, images, say):
    name = vol.get("name")
    res = {"game": g["key"], "volume": name, "state": "failed", "files": 0, "why": ""}

    def fail(why):
        res["why"] = why
        say("%s: not set up. %s" % (name, why))
        return res

    why = fill_problem(vol)
    if why:
        return fail("The game's catalog entry cannot be used: %s." % why)
    f = vol["fill"]
    image, what = f["image"], "the %s for %s" % (f.get("what", "data"), g["name"])
    rc, _, err = _call(dock, ["volume", "inspect", name])
    exists = rc == 0
    if not exists and not re.search(r"no such volume|not found", err, re.I):
        return fail(docker_trouble(err) or "Docker said: %s" % _last_line(err))
    if image not in images:
        rc, _, err = _call(dock, ["image", "inspect", image])
        if rc == 0:
            images[image] = None
        elif docker_trouble(err):
            images[image] = docker_trouble(err)
        else:
            say("Downloading %s: about %d MB, only this once. This can take a while..."
                % (what, fill_size(g, image)))
            rc, out, err = _call(dock, ["pull", image], timeout=PULL_TIMEOUT)
            images[image] = None if rc == 0 else \
                "Downloading %s failed. %s" % (what, pull_trouble(err + "\n" + out, image))
    if images[image]:
        return fail(images[image])
    if exists:
        files, why = _look(dock, name, image)
        if why:
            return fail(why)
        if files:
            res.update(state="present", files=len(files))
            if not any(fnmatch.fnmatch(x, f["expect"]) for x in files):
                say("%s: %d files, none of them %s; it was left as it is. If the game fails "
                    "to start, remove that volume and set it up again." % (name, len(files),
                                                                          f["expect"]))
            else:
                say("%s: %d files, already in place." % (name, len(files)))
            return res
    else:
        rc, _, err = _call(dock, ["volume", "create", "--label", "org.openlobby.game=" + g["key"],
                                  "--label", "org.openlobby.fill=" + image, name])
        if rc != 0:
            return fail(docker_trouble(err) or "Docker could not create it: %s" % _last_line(err))
    say("Copying %s into %s..." % (what, name))
    rc, _, err = _call(dock, ["run", "--rm", "--network", "none", "-v", "%s:%s" % (name, f["path"]),
                              image, "true"], timeout=FILL_TIMEOUT)
    if rc != 0:
        return fail(docker_trouble(err) or "Docker could not copy the files: %s" % _last_line(err))
    files, why = _look(dock, name, image)
    if why:
        return fail(why)
    if not any(fnmatch.fnmatch(x, f["expect"]) for x in files):
        return fail("It holds %d files after the copy and none of them is %s, so %s does not "
                    "hold them at %s any more." % (len(files), f["expect"], image, f["path"]))
    res.update(state="filled", files=len(files))
    say("%s: %d files, ready." % (name, len(files)))
    return res


# -------------------------------------------------------------- commands

def cmd_list(catalog, args):
    on = enabled(catalog)
    for g in games_of(catalog):
        present = os.path.isdir(checkout_of(g))
        print("%-5s %-8s %-9s %s" % (g["key"], "enabled" if g["key"] in on else "-",
                                     "cloned" if present else "not here", g["name"]))
    print("\nPOL_TITLES for the enabled set: %s" % (titles_of(catalog, on) or "(none)"))
    return 0


def titles_of(catalog, keys):
    return ",".join(catalog[k]["title"]["module"] for k in keys if catalog[k].get("title"))


def cmd_add(catalog, args):
    yes = "--yes" in args
    keys = parse_keys([a for a in args if a != "--yes"], catalog)
    git = shutil.which("git")
    rc = 0
    for k in keys:
        g = catalog[k]
        d = checkout_of(g)
        if os.path.isdir(d):
            print("%s: already at %s" % (k, d))
            continue
        argv = ["git", "clone", g["repo"] + ".git", d]
        if not git:
            print("%s: git is not on PATH. Download %s (Code, Download ZIP) and unpack "
                  "it as %s" % (k, g["repo"], d))
            rc = 1
            continue
        if not yes:
            print("%s: would run: %s" % (k, " ".join(shlex.quote(a) for a in argv)))
            print("    run again with --yes to do it")
            continue
        print("%s: %s" % (k, " ".join(shlex.quote(a) for a in argv)))
        if subprocess.run(argv).returncode != 0:
            rc = 1
    if rc == 0:
        print("next: python tools/stack.py enable %s" % ",".join(keys))
    return rc


def cmd_enable(catalog, args):
    keys = parse_keys(args, catalog)
    on = ordered(set(enabled(catalog)) | set(keys), catalog)
    write_state(on)
    for k in keys:
        if not os.path.isdir(checkout_of(catalog[k])):
            print("%s: not cloned yet (python tools/stack.py add %s)" % (k, k))
    print("enabled: %s" % ", ".join(on))
    print("POL_TITLES: %s" % (titles_of(catalog, on) or "(none)"))
    return 0


def cmd_disable(catalog, args):
    keys = parse_keys(args, catalog)
    on = [k for k in enabled(catalog) if k not in keys]
    write_state(on)
    print("enabled: %s" % (", ".join(on) or "(core only)"))
    print("POL_TITLES: %s" % (titles_of(catalog, on) or "(none)"))
    print("to rebuild login without it and remove its containers:\n"
          "    python tools/stack.py compose up -d --build --remove-orphans")
    return 0


def cmd_check(catalog, args):
    ghcr = "--ghcr" in args
    as_json = "--json" in args
    named = [a for a in args if a not in ("--ghcr", "--json", "--all")]
    on = enabled(catalog)
    if "--all" in args:
        keys = [g["key"] for g in games_of(catalog)]
    elif named:
        keys = ordered(parse_keys(named, catalog), catalog)
    else:
        keys = on
    if as_json:
        report = check_report(catalog, keys, on, ghcr=ghcr)
        print(json.dumps(report, indent=1))
        return 0 if report["ok"] else 1
    if not keys:
        print("no games enabled (python tools/stack.py enable <key>); the core runs alone")
    rows = check(catalog, keys, ghcr=ghcr)
    for level, text in rows:
        print("%-4s %s" % (level.upper(), text))
    return 1 if any(level == "fail" for level, _ in rows) else 0


def cmd_ps(catalog, args):
    ghcr = "--ghcr" in args
    as_json = "--json" in args
    keys = [k for k in enabled(catalog) if os.path.isdir(checkout_of(catalog[k]))]
    error = report = None
    if not shutil.which("docker"):
        error = "docker is not on PATH"
    else:
        p = plan(catalog, keys, ["ps", "--all", "--format", "json"], ghcr=ghcr,
                 environ=dict(os.environ), prebuild=False)
        step = p["steps"][-1]
        env = dict(os.environ)
        env.update(step["env"])
        r = subprocess.run(step["argv"], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            error = (r.stderr.strip().splitlines() or ["docker compose ps failed"])[-1]
        else:
            try:
                report = ps_report(catalog, parse_ps(r.stdout))
            except ValueError as exc:
                error = "cannot read the docker compose ps output: %s" % exc
    if error:
        if as_json:
            print(json.dumps({"ok": False, "error": error}))
            return 1
        raise StackError(error)
    if as_json:
        print(json.dumps(report, indent=1))
        return 0
    for key, g in [("core", report["core"])] + list(report["games"].items()):
        states = " ".join("%s=%s" % kv for kv in sorted(g["services"].items()))
        print("%-5s %-8s %s" % (key, "running" if g["running"] else "-", states))
    return 0


def cmd_compose(catalog, args):
    ghcr = dry = False
    prebuild = True
    while args and args[0] in ("--ghcr", "--dry-run", "--no-prebuild"):
        flag = args.pop(0)
        ghcr = ghcr or flag == "--ghcr"
        dry = dry or flag == "--dry-run"
        prebuild = prebuild and flag != "--no-prebuild"
    if not args:
        raise StackError("give the docker compose arguments, e.g. "
                         "stack.py compose up -d --build")
    keys = enabled(catalog)
    for k in keys:
        if not os.path.isdir(checkout_of(catalog[k])):
            raise StackError("%s is enabled but not cloned (stack.py add %s)" % (k, k))
    channel = read_channel()
    over = None
    if channel["channel"] == "dev" and not dry:
        fd, over = tempfile.mkstemp(prefix="openlobby-channel-", suffix=".yml")
        os.close(fd)
    try:
        p = plan(catalog, keys, args, ghcr=ghcr, environ=dict(os.environ), prebuild=prebuild,
                 channel=channel, override_path=over)
        for w in env_warnings(p["env_files"]):
            print("stack.py: warning: " + w, file=sys.stderr)
        if dry:
            if p["override"]:
                print("# the dev channel's images (.channel-compose.yml, written for the command)")
                print(p["override"].rstrip("\n"))
            for step in p["steps"]:
                print("# " + step["why"])
                print(show(step))
            return 0
        if not shutil.which("docker"):
            raise StackError("docker is not on PATH")
        if p["override"]:
            with open(over, "w", encoding="utf-8", newline="\n") as f:
                f.write(p["override"])
            print("stack.py: dev channel: the dev builds of %d image(s)"
                  % len(devchannel.applied(devchannel.image_rows(catalog, channel, ghcr, keys=keys))),
                  file=sys.stderr)
        for step in p["steps"]:
            print("stack.py: %s" % step["why"], file=sys.stderr)
            env = dict(os.environ)
            env.update(step["env"])
            rc = subprocess.run(step["argv"], env=env).returncode
            if rc != 0:
                return rc
        return 0
    finally:
        if over:
            try:
                os.remove(over)
            except OSError:
                pass


def read_channel(root=ROOT):
    ch = devchannel.read_channel(root)
    if ch.get("problem"):
        print("stack.py: warning: " + ch["problem"], file=sys.stderr)
    return ch


def cmd_channel(catalog, args):
    """channel [--json] [--ghcr] | channel dev --registry HOST/PATH [--source URL] |
    channel stable: show or set the update channel (docs/deployer.md, "Dev channel").
    Setting it only writes .channel; the next compose command uses it. Signing in to the
    registry is the deployer's job (or docker login's): no access code passes through here."""
    args = list(args)
    as_json = "--json" in args
    ghcr = "--ghcr" in args
    args = [a for a in args if a not in ("--json", "--ghcr")]
    opts = {}
    for name in ("--registry", "--source"):
        if name in args:
            i = args.index(name)
            if i + 1 >= len(args):
                raise StackError("%s needs a value" % name)
            opts[name] = args[i + 1]
            del args[i:i + 2]
    if len(args) > 1 or (args and args[0] not in devchannel.CHANNELS):
        raise StackError("channel takes stable or dev (got %s)" % " ".join(args))
    try:
        if args and args[0] == "dev":
            if not opts.get("--registry"):
                raise StackError("channel dev needs --registry HOST/PATH")
            devchannel.write_channel(ROOT, "dev", opts["--registry"], opts.get("--source"))
        elif args:
            devchannel.write_channel(ROOT, "stable")
    except (devchannel.ChannelError, OSError) as exc:
        raise StackError(str(exc))
    ch = read_channel()
    rows = devchannel.image_rows(catalog, ch, ghcr, keys=enabled(catalog))
    if as_json:
        print(json.dumps(dict(ch, images=rows), indent=1))
        return 0
    if ch["channel"] == "stable":
        print("channel: stable (the published images)")
        return 0
    # the registry is in .channel and --json; this summary names the images only
    print("channel: dev (the dev builds)")
    for r in rows:
        print("  %-5s %s -> %s" % (r["key"], r["stable"],
                                   r["dev_name"] if r["applies"] and r["dev_name"] else r["note"]))
    return 0


def cmd_dev_code(catalog, args):
    """dev-code --registry HOST/PATH [--token-file FILE|-] [--source URL] [--patches
    HOST/OWNER]: print an access code for the dev channel (docs/deployer.md, "Dev channel");
    --patches makes it an olx2. code that also unlocks that patch feed. The token, when the images
    need one, is read from FILE's first line or from stdin (-), never from the command
    line. dev-code --decode reads a code on stdin and says what is in it, the token left
    out."""
    args = list(args)
    if args == ["--decode"]:
        try:
            info = devchannel.parse_code(sys.stdin.readline().strip())
        except devchannel.ChannelError as exc:
            raise StackError(str(exc))
        print("registry: %s" % info["registry"])
        print("token:    %s" % ("yes" if info["token"] else "none (public images)"))
        print("source:   %s" % (info["source"] or devchannel.default_source(info["registry"])
                                + " (the default)"))
        print("patches:  %s" % (info["feed"] or "none"))
        return 0
    opts = {}
    for name in ("--registry", "--token-file", "--source", "--patches"):
        if name in args:
            i = args.index(name)
            if i + 1 >= len(args):
                raise StackError("%s needs a value" % name)
            opts[name] = args[i + 1]
            del args[i:i + 2]
    if args:
        raise StackError("dev-code takes only --registry, --token-file, --source and --patches; "
                         "the token is never given on the command line")
    if not opts.get("--registry"):
        raise StackError("dev-code needs --registry HOST/PATH")
    token = None
    tf = opts.get("--token-file")
    if tf:
        try:
            if tf == "-":
                if sys.stdin.isatty():
                    import getpass
                    token = getpass.getpass("Token: ")
                else:
                    token = sys.stdin.readline()
            else:
                with open(tf, encoding="utf-8") as f:
                    token = f.readline()
        except OSError as exc:
            raise StackError("cannot read the token: %s" % exc)
        token = token.strip()
        if not token:
            raise StackError("the token file is empty")
    try:
        print(devchannel.make_code(opts["--registry"], token, opts.get("--source"),
                                   opts.get("--patches")))
    except devchannel.ChannelError as exc:
        raise StackError(str(exc))
    return 0


def cmd_volumes(catalog, args):
    """volumes [KEYS|--all] [--report FILE]: create and fill the data volumes
    of the named games (the enabled ones by default). --report writes the
    results as JSON for the deployer. Exit 1 when one could not be set up."""
    args = list(args)
    report = None
    if "--report" in args:
        i = args.index("--report")
        if i + 1 >= len(args):
            raise StackError("--report needs a file name")
        report = args[i + 1]
        del args[i:i + 2]
    named = [a for a in args if a != "--all"]
    if "--all" in args:
        keys = [g["key"] for g in games_of(catalog)]
    elif named:
        keys = ordered(parse_keys(named, catalog), catalog)
    else:
        keys = enabled(catalog)
    results = ensure_volumes(catalog, keys)
    failed = [r for r in results if r["state"] == "failed"]
    if report:
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"ok": not failed, "volumes": results}, f, indent=1)
    if failed:
        print("stack.py: some game data could not be set up (the lines above say why); "
              "the games that need it will not start until it is. Run this again to retry.",
              file=sys.stderr, flush=True)
    return 1 if failed else 0


COMMANDS = {"list": cmd_list, "add": cmd_add, "enable": cmd_enable,
            "disable": cmd_disable, "check": cmd_check, "ps": cmd_ps,
            "compose": cmd_compose, "volumes": cmd_volumes, "channel": cmd_channel,
            "dev-code": cmd_dev_code}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in COMMANDS:
        print(__doc__.strip())
        return 0 if argv and argv[0] in ("-h", "--help", "help") else 2
    try:
        return COMMANDS[argv[0]](load_catalog(), argv[1:])
    except StackError as exc:
        print("stack.py: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
