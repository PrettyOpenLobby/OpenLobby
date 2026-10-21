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
    python tools/stack.py compose up -d --build  run docker compose
    python tools/stack.py compose --dry-run up -d --build   print, run nothing

Standard library only. Works from any directory.
"""
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(ROOT)
CATALOG_DIR = os.path.join(ROOT, "games")
STATE_FILE = os.path.join(ROOT, ".games")

#: Variables this tool computes. A value for them in an env file is ignored.
COMPUTED = ("POL_TITLES", "POL_TITLE_BASE_IMAGE")

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
         environ=None, prebuild=True):
    """Everything `stack.py compose` would run, as data:

      {"files": [...], "env_files": [...], "titles": "tmtitle,...",
       "base": "<image the last title link builds on>",
       "steps": [{"argv": [...], "env": {...}, "why": "..."}, ...]}

    The last step is the command asked for; any before it build the core
    image and the title chain it stands on.
    """
    keys = ordered(keys, catalog)
    core = catalog["core"]
    games = [catalog[k] for k in keys]
    efiles = env_files(catalog, keys, root, parent)
    vals = env_values(efiles, environ)

    core_files = [os.path.join(root, f) for f in core["compose"]]
    if ghcr:
        core_files += [os.path.join(root, f) for f in core["compose_ghcr"]]
        base0 = _with_tag(core["images"][0]["ghcr"], core["images"][0]["tag_var"], vals)
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
            image = _with_tag(t["ghcr_image"], t["ghcr_tag_var"], vals)
        else:
            image = t["image"]
            if i < len(links) - 1:
                prebuilds.append((g, prev))
        link_image.append(image)
        prev = image
    final_base = link_image[-2] if len(links) >= 2 else base0
    titles = ",".join(g["title"]["module"] for g in links)

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
            "base": final_base, "steps": steps}


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

def check(catalog, keys, ghcr=False, root=ROOT, parent=PARENT, docker=None):
    """[(level, text)] for the enabled games; level is ok, warn or fail."""
    out = []
    if docker is None:
        docker = shutil.which("docker")
    for k in keys:
        g = catalog[k]
        d = checkout_of(g, root, parent)
        if not os.path.isdir(d):
            out.append(("fail", "%s: no checkout at %s (stack.py add %s)" % (k, d, k)))
            continue
        out.append(("ok", "%s: checkout %s" % (k, d)))
        for f in g["compose"] + (g["compose_ghcr"] if ghcr else []):
            if not os.path.isfile(os.path.join(d, f)):
                out.append(("fail", "%s: %s is missing from the checkout" % (k, f)))
        if g.get("env_example") and os.path.isfile(os.path.join(d, g["env_example"])) \
                and not os.path.isfile(os.path.join(d, ".env")):
            out.append(("warn", "%s: no .env (copy %s if you want to change a setting)"
                        % (k, g["env_example"])))
        for item in g["data"]:
            if ghcr and item.get("build_only"):
                continue
            if not item["paths"]:
                continue
            missing = [p for p in item["paths"] if not glob.glob(os.path.join(d, p))]
            if not missing:
                out.append(("ok", "%s: %s present" % (k, item["id"])))
            else:
                level = "fail" if item["required"] else "warn"
                out.append((level, "%s: %s missing (%s); run in %s: %s"
                            % (k, item["id"], ", ".join(missing), d, item["tool"])))
        for vol in g.get("volumes", []):
            if not docker:
                out.append(("warn", "%s: volume %s not checked (no docker on PATH)"
                            % (k, vol["name"])))
                continue
            r = subprocess.run([docker, "volume", "inspect", vol["name"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                out.append(("ok", "%s: volume %s present" % (k, vol["name"])))
            else:
                level = "fail" if vol.get("required") else "warn"
                out.append((level, "%s: volume %s missing; create it with: %s"
                            % (k, vol["name"], " && ".join(vol["setup"]))))
    for w in env_warnings(env_files(catalog, keys, root, parent)):
        out.append(("warn", w))
    return out


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
    keys = enabled(catalog)
    if not keys:
        print("no games enabled (python tools/stack.py enable <key>); the core runs alone")
    rows = check(catalog, keys, ghcr=ghcr)
    for level, text in rows:
        print("%-4s %s" % (level.upper(), text))
    return 1 if any(level == "fail" for level, _ in rows) else 0


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
    p = plan(catalog, keys, args, ghcr=ghcr, environ=dict(os.environ), prebuild=prebuild)
    for w in env_warnings(p["env_files"]):
        print("stack.py: warning: " + w, file=sys.stderr)
    if dry:
        for step in p["steps"]:
            print("# " + step["why"])
            print(show(step))
        return 0
    if not shutil.which("docker"):
        raise StackError("docker is not on PATH")
    for step in p["steps"]:
        print("stack.py: %s" % step["why"], file=sys.stderr)
        env = dict(os.environ)
        env.update(step["env"])
        rc = subprocess.run(step["argv"], env=env).returncode
        if rc != 0:
            return rc
    return 0


COMMANDS = {"list": cmd_list, "add": cmd_add, "enable": cmd_enable,
            "disable": cmd_disable, "check": cmd_check, "compose": cmd_compose}


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
