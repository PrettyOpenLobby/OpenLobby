#!/usr/bin/env python3
"""Selftest for tools/stack.py and the game catalog (games/*.json).

    python tools/stack_test.py [DIR]

DIR holds the game checkouts (default: the directory this checkout sits in).
A game whose checkout is not there is reported SKIP. The catalog is checked
against each checkout: every file it names exists, every env var it lists
reaches a compose file, every service and port it lists is in one, and no
two games define the same service. Then the command assembly is checked for
several sets of games. With docker on PATH, `docker compose config` is run
on two sets and the merged login service is checked; without it that part
is reported SKIP. Nothing is started.

The volume routine (stack.py volumes) runs against a stand-in docker CLI
that keeps its volumes and images in a JSON file: a missing volume is
created and filled, an empty one filled, one with files left alone, and a
failed download, a full disk and a catalog entry that cannot be used each
end in a plain sentence with nothing written.

The dev channel (docs/deployer.md, "Dev channel"): every catalog "dev" entry
names services its game's compose files define and an image they run; on
the dev channel the plan swaps the title chain's base where a dev image
replaces it and adds a compose file that sets image: for those services,
last in the -f list, with published images only for the catalog's own
images; .channel round-trips and refuses a registry that is not one; and,
with docker, `docker compose config` shows the dev image on the services
and nowhere else.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stack  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services", "deployer"))
import clientdata  # noqa: E402
import devchannel  # noqa: E402

FAILS = []
SKIPS = []


def ok(cond, what):
    if not cond:
        FAILS.append(what)
        print("FAIL " + what)
    return cond


def skip(what):
    SKIPS.append(what)
    print("SKIP " + what)


def compose_services(text):
    """Top-level service names of a compose file (two-space keys under
    `services:`)."""
    names, inside = [], False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            inside = line.startswith("services:")
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if inside and m:
            names.append(m.group(1))
    return names


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def admin_keys(root):
    """The module keys in services/adminmodules/ (the first argument of each
    file's MODULE = Module(...)), read without importing them."""
    pkg = os.path.join(root, "services", "adminmodules")
    if not os.path.isdir(pkg):
        return None
    keys = set()
    for name in sorted(os.listdir(pkg)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        for node in ast.parse(read(os.path.join(pkg, name))).body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and any(isinstance(t, ast.Name) and t.id == "MODULE" for t in node.targets)
                    and node.value.args and isinstance(node.value.args[0], ast.Constant)):
                keys.add(node.value.args[0].value)
    return keys


def check_catalog(catalog, root, parent):
    ok(set(catalog) >= {"core", "tm", "jan", "fe", "fmo", "doc", "ffxi"},
       "catalog has core and the six games")
    akeys = admin_keys(root)
    owner = {}
    for key, e in catalog.items():
        for field in ("name", "order", "repo", "checkout", "compose", "compose_ghcr",
                      "images", "services", "profiles", "env", "data", "volumes",
                      "ports", "admin_module", "notes"):
            ok(field in e, "%s: field %s" % (key, field))
        ok(e["repo"].startswith("https://github.com/PrettyOpenLobby/"), "%s: repo URL" % key)
        if e["admin_module"] is not None and akeys is not None:
            ok(e["admin_module"] in akeys, "%s: admin_module %r is an adminmodules key"
               % (key, e["admin_module"]))
        for vol in e["volumes"]:
            ok(isinstance(vol.get("setup"), list) and vol["setup"],
               "%s: volume %s has its by-hand setup commands" % (key, vol.get("name")))
            if "fill" in vol:
                why = stack.fill_problem(vol)
                ok(why is None, "%s: volume %s fill block: %s" % (key, vol.get("name"), why))
            elif vol.get("required"):
                skip("%s: required volume %s has no fill block (set up by hand)" % (key, vol.get("name")))
        for item in e["data"]:
            imp = item.get("import")
            if imp is None:
                continue
            why = clientdata.valid_import(imp)
            ok(why is None, "%s: %s import block: %s" % (key, item["id"], why))
            ok(not item.get("build_only") and not item["source"].startswith("none"),
               "%s: %s has an import but needs no client" % (key, item["id"]))
            m = re.search(r"tools/[\w.]+\.py", item["tool"])
            ok(why is not None or (m and imp["argv"][1] == m.group(0)),
               "%s: %s import runs the tool its entry names (%s)" % (key, item["id"], item["tool"]))
        d = stack.checkout_of(e, root, parent)
        if not os.path.isdir(d):
            skip("%s: no checkout at %s" % (key, d))
            continue
        for item in e["data"]:
            imp = item.get("import")
            if imp is None or clientdata.valid_import(imp):
                continue
            tool = os.path.join(d, *imp["argv"][1].split("/"))
            if not ok(os.path.isfile(tool), "%s: import tool %s exists" % (key, imp["argv"][1])):
                continue
            src = read(tool)
            for a in imp["argv"][2:]:
                if a.startswith("--"):
                    ok(re.search(r"add_argument\([^)]*[\"']%s[\"']" % re.escape(a), src),
                       "%s: %s takes %s" % (key, imp["argv"][1], a))
                elif "{input}" in a and not a.startswith("-"):
                    prev = imp["argv"][imp["argv"].index(a) - 1]
                    ok(prev.startswith("--") or re.search(r"add_argument\(\s*[\"'][a-z_]+[\"']", src),
                       "%s: %s takes the folder as a positional argument" % (key, imp["argv"][1]))
        files = list(e["compose"]) + list(e["compose_ghcr"])
        if e.get("title"):
            files.append(e["title"]["compose"])
        texts = {}
        for f in files:
            if ok(os.path.isfile(os.path.join(d, f)), "%s: %s exists" % (key, f)):
                texts[f] = read(os.path.join(d, f))
        alltext = "\n".join(texts.values())
        if e.get("env_example"):
            ok(os.path.isfile(os.path.join(d, e["env_example"])), "%s: env example" % key)
        for v in e["env"]:
            ok("${%s:" % v["name"] in alltext or "${%s}" % v["name"] in alltext,
               "%s: env %s reaches a compose file" % (key, v["name"]))
            ok(isinstance(v.get("default"), str) and v.get("description"),
               "%s: env %s has a default and a description" % (key, v["name"]))
        declared = set()
        for f in e["compose"]:
            declared |= set(compose_services(texts.get(f, "")))
        for s in e["services"] + [s for ss in e["profiles"].values() for s in ss]:
            ok(s in declared, "%s: service %s is in %s" % (key, s, e["compose"]))
        for prof, ss in e["profiles"].items():
            for s in ss:
                ok('profiles: ["%s"]' % prof in alltext, "%s: profile %s in compose" % (key, prof))
        for p in e["ports"]:
            ok(p["port"].split("-")[0] in alltext, "%s: port %s in compose" % (key, p["port"]))
        for img in e["images"]:
            df = os.path.join(d, img["context"], img["dockerfile"])
            ok(os.path.isfile(df), "%s: %s exists" % (key, os.path.relpath(df, d)))
            ok(img["name"] in alltext, "%s: image %s named in compose" % (key, img["name"]))
            if img.get("ghcr"):
                ok(img["ghcr"] in alltext, "%s: %s in the ghcr override" % (key, img["ghcr"]))
        for item in e["data"]:
            m = re.search(r"tools/[\w.]+\.py", item["tool"])
            if m:
                ok(os.path.isfile(os.path.join(d, m.group(0))),
                   "%s: data tool %s exists" % (key, m.group(0)))
        t = e.get("title")
        if t:
            tt = texts.get(t["compose"], "")
            found = [p for p in (os.path.join(dp, t["module"] + ".py")
                                 for dp, _, fs in os.walk(d) if t["module"] + ".py" in fs)]
            ok(found, "%s: %s.py in the checkout" % (key, t["module"]))
            ok("${POL_TITLES:-%s}" % t["module"] in tt,
               "%s: POL_TITLES defaults to %s and can be overridden" % (key, t["module"]))
            ok("${POL_TITLE_BASE_IMAGE:-" in tt, "%s: login builds on POL_TITLE_BASE_IMAGE" % key)
            ok(re.search(r"dockerfile:\s*\S+", tt.split("login:", 1)[-1].split("authsess:", 1)[0]),
               "%s: login names its dockerfile (merged builds keep the last one whole)" % key)
            ok("image: %s" % t["image"] in tt, "%s: login image %s" % (key, t["image"]))
            if t.get("ghcr_image"):
                ok(t["ghcr_image"] in alltext, "%s: published title image in override" % key)
        if e["kind"] == "game":
            for s in declared - {"login", "authsess"}:
                ok(s not in owner, "service %s defined by both %s and %s"
                   % (s, owner.get(s), key))
                owner[s] = key
        else:
            for s in declared:
                owner[s] = key


def plan_files(p, root, parent):
    """The -f list of a plan as short names: the core's files as core/<file>
    (the core checkout may have any directory name, e.g. OpenLobby on a CI
    runner), a game's as <checkout>/<file>."""
    out = []
    for f in p["files"]:
        rel = os.path.relpath(f, root)
        if not rel.startswith(os.pardir):
            out.append("core/" + rel.replace(os.sep, "/"))
        else:
            out.append(os.path.relpath(f, parent).replace(os.sep, "/"))
    return out


def check_plan(catalog, root, parent):
    p = stack.plan(catalog, [], ["up", "-d"], root=root, parent=parent, environ={})
    ok(len(p["steps"]) == 1 and p["titles"] == "", "core alone: one step, no titles")

    p = stack.plan(catalog, ["tm"], ["up", "-d", "--build"], root=root, parent=parent, environ={})
    final = p["steps"][-1]
    ok(p["titles"] == "tmtitle", "tm: POL_TITLES")
    ok(final["env"]["POL_TITLE_BASE_IMAGE"] == "openlobby:latest", "tm: base is the core")
    ok([s["argv"][-1] for s in p["steps"][:-1]] == ["dns"], "tm: only the core is prebuilt")
    ok(final["argv"][-3:] == ["up", "-d", "--build"], "tm: args passed through")

    # order comes from the catalog, not the command line
    p = stack.plan(catalog, ["ffxi", "fe", "tm"], ["up", "-d", "--build"],
                   root=root, parent=parent, environ={})
    ok(p["titles"] == "tmtitle,fetitle,ffxititle", "tm+fe+ffxi: POL_TITLES in chain order")
    final = p["steps"][-1]
    ok(final["env"]["POL_TITLE_BASE_IMAGE"] == "hippaulring-title:latest",
       "tm+fe+ffxi: the last link builds on the FE title image")
    pre = p["steps"][:-1]
    ok([s["env"].get("POL_TITLE_BASE_IMAGE") for s in pre] ==
       [None, "openlobby:latest", "hippaulmaster:latest"],
       "tm+fe+ffxi: core, then tm on the core, then fe on tm")
    fl = plan_files(p, root, parent)
    ok(fl == ["core/docker-compose.yml",
              "hippaulmaster/docker-compose.yml",
              "hippaulring/docker-compose.yml", "hippaulring/docker-compose.title.yml",
              "hippaulbridge/docker-compose.yml", "hippaulbridge/docker-compose.title.yml"],
       "tm+fe+ffxi: -f order %s" % fl)
    ok(final["argv"][:4] == ["docker", "compose", "--project-directory", root],
       "project directory is the core checkout")

    # no prebuild for a command that builds nothing
    p = stack.plan(catalog, ["tm", "jan"], ["logs", "-f", "jan"], root=root, parent=parent, environ={})
    ok(len(p["steps"]) == 1, "logs: no prebuild")
    p = stack.plan(catalog, ["tm", "jan"], ["--profile", "board", "up", "-d"],
                   root=root, parent=parent, environ={})
    ok(len(p["steps"]) == 3, "--profile board up: the subcommand is found past the option")

    # OPENLOBBY_IMAGE from the environment is the bottom of the chain
    p = stack.plan(catalog, ["fe"], ["build"], root=root, parent=parent,
                   environ={"OPENLOBBY_IMAGE": "mycore:1"})
    ok(p["steps"][-1]["env"]["POL_TITLE_BASE_IMAGE"] == "mycore:1", "OPENLOBBY_IMAGE is the base")

    # published images: tm is the first link and pulled; jan builds on it
    p = stack.plan(catalog, ["tm", "jan"], ["up", "-d"], ghcr=True, root=root, parent=parent,
                   environ={"HIPPAULMASTER_TAG": "0.2.0"})
    fl = plan_files(p, root, parent)
    ok("hippaulmaster/docker-compose.ghcr.yml" in fl and "hippaulholo/docker-compose.ghcr.yml" not in fl,
       "ghcr tm+jan: tm's override in, jan's out (%s)" % fl)
    ok(p["steps"][-1]["env"]["POL_TITLE_BASE_IMAGE"] == "ghcr.io/prettyopenlobby/hippaulmaster:0.2.0",
       "ghcr tm+jan: jan builds on the published tm image")
    ok(len(p["steps"]) == 1, "ghcr tm+jan: nothing to prebuild")
    p = stack.plan(catalog, ["fe", "fmo"], ["up", "-d"], ghcr=True, root=root, parent=parent, environ={})
    ok(p["steps"][0]["env"].get("POL_TITLE_BASE_IMAGE") == "ghcr.io/prettyopenlobby/openlobby:latest",
       "ghcr fe+fmo: the first local link builds on the published core")
    fl = plan_files(p, root, parent)
    ok("hippaulring/docker-compose.ghcr.yml" in fl and "core/docker-compose.ghcr.yml" in fl,
       "ghcr fe+fmo: game overrides and the core's included")


def check_env_and_state():
    tmp = tempfile.mkdtemp(prefix="stacktest-")
    try:
        a = os.path.join(tmp, "a.env")
        b = os.path.join(tmp, "b.env")
        with open(a, "w") as f:
            f.write("# core\nPOL_ADVERTISE=203.0.113.5\nPOL_DB_PASSWORD='x y'\n")
        with open(b, "w") as f:
            f.write("POL_ADVERTISE=\nPOL_TITLES=tmtitle\nexport FE_ADVERTISE=198.51.100.2 # lan\n")
        ok(stack.parse_env(a)["POL_DB_PASSWORD"] == "x y", "parse_env strips quotes")
        ok(stack.parse_env(b)["FE_ADVERTISE"] == "198.51.100.2", "parse_env: export and comment")
        w = stack.env_warnings([a, b])
        ok(any("empty POL_ADVERTISE=" in x for x in w), "warns on an empty key shadowing a value")
        ok(any("POL_TITLES" in x for x in w), "warns on a computed variable in an env file")
        ok(stack.env_values([a, b], {"POL_DB_PASSWORD": "env"})["POL_DB_PASSWORD"] == "env",
           "the process environment wins over env files")
        st = os.path.join(tmp, ".games")
        stack.write_state(["tm", "fe"], st)
        ok(stack.read_state(st) == ["tm", "fe"], "state file round trip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_report_and_ps(catalog):
    """check --json and ps --json as data, against a scratch parent dir with
    one game checked out and nothing else."""
    tmp = tempfile.mkdtemp(prefix="stacktest-")
    try:
        root = os.path.join(tmp, "openlobby")
        os.makedirs(root)
        fe = catalog["fe"]
        d = os.path.join(tmp, fe["checkout"])
        for f in fe["compose"]:
            os.makedirs(os.path.dirname(os.path.join(d, f)) or d, exist_ok=True)
            open(os.path.join(d, f), "w").close()
        r = stack.check_report(catalog, ["tm", "fe"], ["fe"], root=root, parent=tmp, docker="")
        tm, feg = r["games"]
        ok(tm["installed"] is False and tm["data_ok"] is None and tm["missing"] == [],
           "check_report: a game with no checkout has data_ok None")
        ok(feg["installed"] and feg["enabled"] and feg["data_ok"] is False,
           "check_report: fe without its data is not data_ok")
        paths = [m["path"] for m in feg["missing"]]
        ok("../hippaulring/services/fedata/fe-fet-area.tsv" in paths,
           "check_report: missing names the path from openlobby (%s)" % paths)
        ok(all(m["hint"] and "required" in m for m in feg["missing"]), "check_report: hints")
        ok(r["ok"] is False, "check_report: not ok while required data is missing")
        ok(feg["ports"] == fe["ports"] and feg["admin_module"] == "fe", "check_report: catalog fields")
        rows_text = [t for _, t in stack.check(catalog, ["fe"], root=root, parent=tmp, docker="")]
        ok(any("fedata missing" in t for t in rows_text), "check(): rows unchanged in shape")
        for p in fe["data"][0]["paths"] + fe["data"][1]["paths"]:
            os.makedirs(os.path.dirname(os.path.join(d, p)), exist_ok=True)
            open(os.path.join(d, p), "w").close()
        r = stack.check_report(catalog, ["fe"], ["fe"], root=root, parent=tmp, docker="")
        ok(r["games"][0]["data_ok"] is True and r["ok"], "check_report: data present is ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines = ('{"Service":"login","State":"running"}\n{"Service":"fellb","State":"exited"}\n'
             '{"Service":"tmboard","State":"running"}\n')
    rows = stack.parse_ps(lines)
    ok(len(rows) == 3 and stack.parse_ps("[" + lines.strip().replace("\n", ",") + "]") == rows
       and stack.parse_ps("") == [], "parse_ps: lines, array and empty")
    p = stack.ps_report(catalog, rows)
    ok(p["core"]["running"] is True, "ps_report: core running")
    ok(p["games"]["fe"]["running"] is False and p["games"]["fe"]["services"] == {"fellb": "exited"},
       "ps_report: fe exited")
    ok(p["games"]["tm"]["running"] is True, "ps_report: a profile service counts")
    ok(p["games"]["jan"] == {"running": False, "services": {}}, "ps_report: no containers")


# The stand-in docker CLI for the volume routine. FAKE_DOCKER_STATE is a JSON
# file: {"volumes": {name: {"files": [...], "labels": {...}}},
#        "images": [ref, ...], "content": {ref: {path: [files]}},
#        "pull_error": text, "run_error": text}; every call is appended to
# "calls".
FAKE_DOCKER = r"""
import json, os, sys
p = os.environ["FAKE_DOCKER_STATE"]
st = json.load(open(p))
a = sys.argv[1:]
st.setdefault("calls", []).append(a)
def done(rc=0, out="", err=""):
    json.dump(st, open(p, "w"))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)
vols = st.setdefault("volumes", {})
if a[:2] == ["volume", "inspect"]:
    if a[2] in vols:
        done(0, "[{}]")
    done(1, "", "Error response from daemon: get %s: no such volume\n" % a[2])
if a[:2] == ["image", "inspect"]:
    if a[2] in st.get("images", []):
        done(0, "[{}]")
    done(1, "", "Error: No such image: %s\n" % a[2])
if a[0] == "pull":
    if st.get("pull_error"):
        done(1, "", st["pull_error"])
    st.setdefault("images", []).append(a[1])
    done(0, "latest: Pulling from x\n")
if a[:2] == ["volume", "create"]:
    labels = dict(x.split("=", 1) for x in a[3:-1:2])
    vols[a[-1]] = {"files": [], "labels": labels}
    done(0, a[-1] + "\n")
if a[0] == "run":
    if "--mount" in a:
        m = dict(kv.split("=", 1) for kv in a[a.index("--mount") + 1].split(",") if "=" in kv)
        assert "readonly" in a[a.index("--mount") + 1] and "volume-nocopy" in a[a.index("--mount") + 1]
        done(0, "".join(f + "\n" for f in vols[m["source"]]["files"]))
    name, path = a[a.index("-v") + 1].split(":", 1)
    image = a[a.index("-v") + 2]
    if st.get("run_error"):
        done(125, "", st["run_error"])
    v = vols.setdefault(name, {"files": [], "labels": {}})
    if not v["files"]:
        v["files"] = list(st.get("content", {}).get(image, {}).get(path, []))
    done(0)
done(2, "", "fake docker: unexpected %s\n" % a)
"""

KART = {"key": "kart", "kind": "game", "name": "Kart Club", "order": 10, "volumes": [
    {"name": "kart_tracks", "required": True, "description": "Tracks.", "setup": ["by hand"],
     "fill": {"image": "ghcr.io/example/kart-data:1", "path": "/tracks", "expect": "*.trk",
              "size_mb": 300, "what": "track data"}},
    {"name": "kart_cars", "required": True, "description": "Cars.", "setup": ["by hand"],
     "fill": {"image": "ghcr.io/example/kart-data:1", "path": "/cars", "expect": "*.car",
              "size_mb": 100, "what": "track data"}},
    {"name": "kart_replays", "required": False, "description": "Optional.", "setup": ["by hand"]}]}
CONTENT = {"ghcr.io/example/kart-data:1": {"/tracks": ["a.trk", "b.trk", "c.trk"],
                                           "/cars": ["x.car", "y.car"]}}


def check_volumes():
    tmp = tempfile.mkdtemp(prefix="stacktest-")
    try:
        fake = os.path.join(tmp, "docker.py")
        with open(fake, "w") as f:
            f.write(FAKE_DOCKER)
        state = os.path.join(tmp, "docker.json")
        dock = [sys.executable, fake]
        cat = {"core": {"key": "core", "kind": "core", "name": "core", "order": 0}, "kart": KART}
        old = os.environ.get("FAKE_DOCKER_STATE")
        os.environ["FAKE_DOCKER_STATE"] = state

        def run(st, keys=("kart",), catalog=cat):
            with open(state, "w") as f:
                json.dump(st, f)
            lines = []
            res = stack.ensure_volumes(catalog, list(keys), docker=dock, say=lines.append)
            with open(state) as f:
                return res, lines, json.load(f)

        try:
            # nothing here: the image is downloaded once, both volumes created and filled
            res, lines, st = run({"content": CONTENT})
            ok([(r["volume"], r["state"], r["files"]) for r in res]
               == [("kart_tracks", "filled", 3), ("kart_cars", "filled", 2)],
               "volumes: missing ones are created and filled (%s)" % res)
            ok(sum(1 for c in st["calls"] if c[0] == "pull") == 1, "volumes: the image is pulled once")
            ok(lines[0] == "Downloading the track data for Kart Club: about 400 MB, only this once. "
               "This can take a while...", "volumes: the download line (%s)" % lines[0])
            ok("kart_tracks: 3 files, ready." in lines and "kart_cars: 2 files, ready." in lines,
               "volumes: a files line per volume (%s)" % lines)
            ok(st["volumes"]["kart_tracks"]["labels"] == {"org.openlobby.game": "kart",
                                                          "org.openlobby.fill": "ghcr.io/example/kart-data:1"},
               "volumes: created with the game's labels")
            fills = [c for c in st["calls"] if c[0] == "run" and "-v" in c]
            ok(fills and fills[0] == ["run", "--rm", "--network", "none", "-v", "kart_tracks:/tracks",
                                      "ghcr.io/example/kart-data:1", "true"],
               "volumes: the fill is docker run --rm -v NAME:PATH IMAGE true (%s)" % fills[:1])
            ok("kart_replays" not in st["volumes"], "volumes: an optional volume is not set up")

            # one empty (created by hand), one full: the empty one is filled, the full one untouched
            res, lines, st = run({"content": CONTENT, "images": ["ghcr.io/example/kart-data:1"],
                                  "volumes": {"kart_tracks": {"files": [], "labels": {}},
                                              "kart_cars": {"files": ["mine.car", "z.car"], "labels": {}}}})
            ok([(r["state"], r["files"]) for r in res] == [("filled", 3), ("present", 2)],
               "volumes: empty one filled, full one present (%s)" % res)
            ok(not any(c[0] == "pull" for c in st["calls"]), "volumes: an image already here is not pulled")
            ok(not any(c[:2] == ["volume", "create"] for c in st["calls"]),
               "volumes: an existing volume is not created again")
            ok(not any(c[0] == "run" and "-v" in c and c[c.index("-v") + 1].startswith("kart_cars")
                       for c in st["calls"]),
               "volumes: a volume with files is never mounted to be filled")
            ok(st["volumes"]["kart_cars"]["files"] == ["mine.car", "z.car"], "  and keeps its files")
            ok("kart_cars: 2 files, already in place." in lines, "volumes: says it was in place (%s)" % lines)

            # a volume with files of another kind: left alone, with a note
            res, lines, st = run({"content": CONTENT, "images": ["ghcr.io/example/kart-data:1"],
                                  "volumes": {"kart_tracks": {"files": ["notes.txt"], "labels": {}},
                                              "kart_cars": {"files": ["x.car"], "labels": {}}}})
            ok(res[0]["state"] == "present" and st["volumes"]["kart_tracks"]["files"] == ["notes.txt"]
               and any("none of them *.trk; it was left as it is" in l for l in lines),
               "volumes: other files are left alone, with a note (%s)" % lines)

            # a download that cannot reach the registry: a plain sentence, nothing created
            res, lines, st = run({"content": CONTENT, "pull_error":
                                  "Error response from daemon: Get \"https://ghcr.io/v2/\": dial tcp: "
                                  "lookup ghcr.io: no such host\n"})
            ok([r["state"] for r in res] == ["failed", "failed"] and not st["volumes"],
               "volumes: pull failure fails both, creates nothing (%s)" % res)
            ok("could not reach ghcr.io" in res[0]["why"] and "dial tcp" not in res[0]["why"]
               and res[0]["why"].startswith("Downloading the track data for Kart Club failed."),
               "volumes: pull failure is a plain sentence (%s)" % res[0]["why"])
            ok(sum(1 for c in st["calls"] if c[0] == "pull") == 1, "  tried once for the shared image")
            ok(lines[-1].startswith("kart_cars: not set up. Downloading"), "  and said per volume")
            res, _, st = run({"content": CONTENT, "pull_error": "Error response from daemon: denied\n"})
            ok("refused" in res[0]["why"] and "ghcr.io/example/kart-data:1" in res[0]["why"],
               "volumes: a refused download names the image (%s)" % res[0]["why"])

            # the disk fills up while copying
            res, _, st = run({"content": CONTENT, "images": ["ghcr.io/example/kart-data:1"],
                              "run_error": "docker: Error response from daemon: write /var/lib/docker/"
                                           "volumes/x: no space left on device.\n"})
            ok(res[0]["state"] == "failed" and "not enough free disk space" in res[0]["why"],
               "volumes: a full disk is a plain sentence (%s)" % res[0]["why"])

            # Docker is not running
            res, _, st = run({"volumes": {}, "images": [], "pull_error": ""}, keys=["kart"],
                             catalog=dict(cat, kart=dict(KART, volumes=KART["volumes"][:1])))
            with open(state, "w") as f:
                json.dump({}, f)
            down = [sys.executable, "-c", "import sys; sys.stderr.write('Cannot connect to the Docker "
                    "daemon at unix:///var/run/docker.sock. Is the docker daemon running?'); sys.exit(1)"]
            lines = []
            res = stack.ensure_volumes(cat, ["kart"], docker=down, say=lines.append)
            ok(res[0]["why"] == "Docker is not running. Start it, then try again.",
               "volumes: Docker not running (%s)" % res[0]["why"])

            # a catalog entry that cannot be used reaches no docker command
            for field, bad in (("image", "-v/:/host"), ("image", "Example/Kart"), ("path", "tracks"),
                               ("path", "/a/../etc"), ("expect", "*.trk;rm"), ("size_mb", "400"),
                               ("unknown", 1)):
                v = json.loads(json.dumps(KART["volumes"][0]))
                v["fill"][field] = bad
                ok(stack.fill_problem(v), "fill_problem refuses %s=%r" % (field, bad))
                res, _, st = run({}, catalog=dict(cat, kart=dict(KART, volumes=[v])))
                ok(res[0]["state"] == "failed" and not st.get("calls"),
                   "  and nothing ran for it (%s)" % st.get("calls"))
            v = json.loads(json.dumps(KART["volumes"][0]))
            v["name"] = "kart tracks:/x"
            ok(stack.fill_problem(v), "fill_problem refuses a name that is not a volume name")
            ok(stack.fill_problem(KART["volumes"][0]) is None, "fill_problem accepts a good block")

            # nothing to fill
            res, lines, st = run({}, catalog=dict(cat, kart=dict(KART, volumes=[])))
            ok(res == [] and lines == ["No data to set up for Kart Club."] and not st.get("calls"),
               "volumes: a game with none says so and runs nothing (%s)" % lines)

            # stack.py check names the routine for a volume it can fill
            with open(state, "w") as f:
                json.dump({}, f)
            os.makedirs(os.path.join(tmp, "kart"))
            g = dict(KART, checkout="kart", compose=[], compose_ghcr=[], data=[])
            rows, missing = stack.check_game(g, root=os.path.join(tmp, "core"), parent=tmp, docker=dock)
            ok(any("stack.py volumes kart sets it up" in t for _, t in rows),
               "check: a fillable volume names stack.py volumes (%s)" % rows)
            ok(missing[0]["fill"] is True and "sets it up by itself" in missing[0]["hint"]
               and missing[2]["fill"] is False and "Create it with" in missing[2]["hint"],
               "check: missing says which volumes fill themselves (%s)" % missing)
        finally:
            if old is None:
                os.environ.pop("FAKE_DOCKER_STATE", None)
            else:
                os.environ["FAKE_DOCKER_STATE"] = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_docker(catalog, root, parent):
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "info"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode != 0:
        skip("docker compose config (docker not available)")
        return
    for keys, last in ((["tm", "jan"], "hippaulholo"),
                       (["fe", "fmo", "doc", "ffxi"], "hippaulbridge")):
        names = [k for k in keys if os.path.isdir(stack.checkout_of(catalog[k], root, parent))]
        if names != keys:
            skip("docker compose config for %s (checkouts missing)" % ",".join(keys))
            continue
        p = stack.plan(catalog, keys, ["config", "--format", "json"], root=root,
                       parent=parent, environ={})
        step = p["steps"][-1]
        env = dict(os.environ)
        env.update(step["env"])
        r = subprocess.run(step["argv"], env=env, capture_output=True, text=True)
        if not ok(r.returncode == 0, "docker compose config %s: %s" % (",".join(keys), r.stderr.strip()[-400:])):
            continue
        before = len(FAILS)
        cfg = json.loads(r.stdout)
        login = cfg["services"]["login"]
        ok(login["environment"].get("POL_TITLES") == p["titles"],
           "config %s: login POL_TITLES=%s" % (",".join(keys), login["environment"].get("POL_TITLES")))
        ctx = login["build"]["context"].replace("\\", "/").rstrip("/")
        ok(ctx.split("/")[-1] == last or ctx.split("/")[-2] == last,
           "config %s: login builds from %s (%s)" % (",".join(keys), last, ctx))
        df = login["build"].get("dockerfile", "Dockerfile")
        ok(os.path.isfile(os.path.join(ctx, df)), "config %s: %s/%s exists" % (",".join(keys), ctx, df))
        ok(login["build"]["args"]["OPENLOBBY_IMAGE"] == p["base"],
           "config %s: login builds on %s" % (",".join(keys), p["base"]))
        if len(FAILS) == before:
            print("ok   docker compose config %s" % ",".join(keys))


DEV = {"channel": "dev", "registry": "registry.example.org/kart",
       "source": "https://code.example.org/kart"}


def check_channel(catalog, root, parent):
    """The dev channel's catalog entries, image map and compose override."""
    for key, e in catalog.items():
        raw = e.get("dev") or []
        ok(isinstance(raw, list), "%s: dev is a list" % key)
        good = devchannel.dev_entries(e)
        ok(len(good) == len(raw), "%s: every dev entry is well formed (%d of %d)" % (key, len(good), len(raw)))
        d = stack.checkout_of(e, root, parent)
        if raw and not os.path.isdir(d):
            skip("%s: dev entries against compose (no checkout)" % key)
            continue
        texts = "".join(read(os.path.join(d, f)) for f in e["compose"] + e["compose_ghcr"])
        declared = set()
        for f in e["compose"]:
            declared |= set(compose_services(read(os.path.join(d, f))))
        for x in good:
            ok(x["replaces"] in texts, "%s: dev %s replaces %s, which its compose runs"
               % (key, x["image"], x["replaces"]))
            for svc in x["services"]:
                ok(svc in declared, "%s: dev %s service %s is in its compose" % (key, x["image"], svc))
            ok(isinstance(x.get("source"), str) and x["source"], "%s: dev %s names its source" % (key, x["image"]))
            ok(isinstance(x.get("license"), str) and x["license"], "%s: dev %s names its license" % (key, x["image"]))

    # stable: the plan is what it was
    a = stack.plan(catalog, ["ffxi"], ["up", "-d"], root=root, parent=parent, environ={})
    b = stack.plan(catalog, ["ffxi"], ["up", "-d"], root=root, parent=parent, environ={},
                   channel={"channel": "stable"})
    ok(a["files"] == b["files"] and b["override"] == "", "stable channel: the plan is unchanged")

    # dev, built here: the world server (not a catalog image of our own) is swapped
    ref = "registry.example.org/kart/lsb-server:stock"
    p = stack.plan(catalog, ["ffxi"], ["up", "-d", "--build"], root=root, parent=parent, environ={},
                   channel=DEV, override_path="/tmp/over.yml")
    ok(p["files"][-1] == "/tmp/over.yml" and p["steps"][-1]["argv"].count("/tmp/over.yml") == 1,
       "dev ffxi: the override is the last -f file")
    for svc in ("db-update", "connect", "search", "world", "map"):
        ok("  %s:\n    image: %s\n" % (svc, ref) in p["override"], "dev ffxi: %s runs %s" % (svc, ref))
    ok("  db:" not in p["override"] and "bridge" not in p["override"],
       "dev ffxi: the database and the bridge keep their images")
    ok(p["base"] == a["base"], "dev ffxi, built here: the title chain is unchanged")
    p = stack.plan(catalog, ["tm"], ["up", "-d"], root=root, parent=parent, environ={}, channel=DEV)
    ok(p["override"] == "" and p["files"] == stack.plan(catalog, ["tm"], ["up", "-d"], root=root,
                                                         parent=parent, environ={})["files"],
       "dev tm (no dev build): no override file")

    # a catalog where the core and Tetra Master have dev builds of their own images
    cat = json.loads(json.dumps(catalog))
    cat["core"]["dev"] = [{"replaces": "ghcr.io/prettyopenlobby/openlobby", "image": "openlobby",
                           "tag": "main", "services": ["login", "dns"], "source": "openlobby",
                           "license": "AGPL-3.0-or-later"}]
    cat["tm"]["dev"] = [{"replaces": "ghcr.io/prettyopenlobby/hippaulmaster", "image": "hippaulmaster",
                         "tag": "main", "services": ["login"], "source": "hippaulmaster",
                         "license": "AGPL-3.0-or-later"}]
    p = stack.plan(cat, ["fe"], ["up", "-d"], ghcr=True, root=root, parent=parent, environ={}, channel=DEV)
    ok(p["steps"][0]["env"].get("POL_TITLE_BASE_IMAGE") == "registry.example.org/kart/openlobby:main",
       "dev, published: the first title link builds on the dev core image")
    ok("  dns:\n    image: registry.example.org/kart/openlobby:main\n" in p["override"],
       "dev, published: the core's services run the dev core image")
    p = stack.plan(cat, ["tm", "jan"], ["up", "-d"], ghcr=True, root=root, parent=parent,
                   environ={"HIPPAULMASTER_TAG": "0.2.0"}, channel=DEV)
    ok(p["steps"][-1]["env"]["POL_TITLE_BASE_IMAGE"] == "registry.example.org/kart/hippaulmaster:main",
       "dev, published: jan builds on the dev Tetra Master image, not the pinned tag")
    p = stack.plan(cat, ["fe"], ["up", "-d", "--build"], root=root, parent=parent, environ={}, channel=DEV)
    ok(p["override"] == "" and p["steps"][0]["argv"][-1] == "dns",
       "dev, built here: a dev build of our own image is not used (the checkout is built)")
    rows = devchannel.image_rows(cat, DEV, False, keys=["fe"])
    core = [r for r in rows if r["stable"] == "ghcr.io/prettyopenlobby/openlobby"][0]
    ok(not core["applies"] and "POL_DEPLOY_GHCR" in core["note"], "  and the row says why")
    ok(core["source"] == "https://code.example.org/kart/openlobby", "  the source URL is under the channel's")
    nodev = json.loads(json.dumps(catalog))
    nodev["core"].pop("dev", None)
    gaps = [r["stable"] for r in devchannel.image_rows(nodev, DEV, True) if r["dev_image"] is None]
    ok("ghcr.io/prettyopenlobby/openlobby" in gaps and "ghcr.io/landsandboat/server" not in gaps,
       "rows: the images with no dev build are listed (%d)" % len(gaps))

    # the shipped catalog: every image an install pulls has a dev build, tag dev
    rows = devchannel.image_rows(catalog, DEV, True)
    own = devchannel.own_images(catalog)
    ok(own and all(any(r["stable"] == s and r["dev_image"] for r in rows) for s in own),
       "catalog: each published image of ours has a dev build (%s)"
       % sorted(s for s in own if not any(r["stable"] == s and r["dev_image"] for r in rows)))
    ok(all(r["dev_image"].endswith(":dev") for r in rows if r["dev_image"] and r["stable"] in own),
       "catalog: our dev builds follow the dev tag")
    lsb = [r for r in rows if r["stable"] == "ghcr.io/landsandboat/server"][0]
    ok(lsb["dev_name"] == "lsb-server (dev)" and lsb["source"] == "https://code.example.org/kart/lsb-server",
       "rows: dev_name is the short name, a relative source is under the channel's (%s)" % lsb["dev_name"])
    core = [r for r in rows if r["stable"] == "ghcr.io/prettyopenlobby/openlobby"][0]
    ok(core["dev"] == "registry.example.org/kart/openlobby:dev" and core["source"].startswith("https://")
       and "example.org/kart" not in core["source"], "rows: a full source address is kept as it is")
    for keys, want, gone in (
            ([], {"login": "openlobby", "authsess": "openlobby", "dns": "openlobby",
                  "ssl3web": "openlobby-ssl3", "sslterm": "openlobby-sslterm"}, ()),
            (["fe"], {"dns": "openlobby", "feworld": "hippaulring"}, ("login", "authsess")),
            (["tm"], {"login": "hippaulmaster", "authsess": "hippaulmaster", "tmrank": "hippaulmaster",
                      "dns": "openlobby"}, ()),
            (["tm", "jan"], {"tmrank": "hippaulmaster", "jan": "hippaulholo"}, ("login", "authsess")),
            (["ffxi"], {"bridge": "hippaulbridge", "world": "lsb-server"}, ("login", "db"))):
        p = stack.plan(catalog, keys, ["up", "-d"], ghcr=True, root=root, parent=parent, environ={},
                       channel=DEV)
        o = p["override"]
        miss = [s for s, img in want.items()
                if "  %s:\n    image: registry.example.org/kart/%s:" % (s, img) not in o]
        ok(not miss and not any("\n  %s:\n" % s in "\n" + o for s in gone),
           "dev, published %s: the title chain's services are left to the chain (%s)" % (keys or "core", miss))

    # access codes: olx1. + base64url(JSON), checked strictly
    code = devchannel.make_code("registry.example.org/kart", "kart-token-0123", "https://code.example.org/kart")
    ok(code.startswith("olx1.") and "=" not in code and "kart-token" not in code
       and devchannel.parse_code(code) == {"kind": "olx1", "registry": "registry.example.org/kart",
                                           "token": "kart-token-0123", "source": "https://code.example.org/kart",
                                           "feed": None},
       "code: round trip (%s)" % code[:20])
    pub = devchannel.make_code("registry.example.org/kart")
    ok(devchannel.parse_code(pub)["token"] is None and devchannel.token_of(pub) is None
       and devchannel.token_of(code) == "kart-token-0123", "code: one without a token reads as public")
    ok(devchannel.parse_code("kart-plain-token", registry="registry.example.org/kart")
       == {"kind": "plain", "registry": "registry.example.org/kart", "token": "kart-plain-token", "source": None,
           "feed": None},
       "code: a plain token with a registry still works (the old way)")
    feed = devchannel.make_code("registry.example.org/kart", "kart-token-0123", feed="code.example.org/kart-patches")
    ok(feed.startswith("olx2.") and devchannel.parse_code(feed)["feed"] == "code.example.org/kart-patches"
       and devchannel.parse_code(feed)["kind"] == "olx2" and devchannel.token_of(feed) == "kart-token-0123",
       "code: one with a patch feed is olx2. and carries it")
    import base64 as _b64

    def enc(raw, v=1):
        return "olx%d." % v + _b64.urlsafe_b64encode(raw).decode().rstrip("=")
    for bad, want in (("", "Type the access code"), ("kart-plain-token", "start with olx"),
                      ("olx1.", "damaged"), ("olx1.%%%%%%%%", "damaged"), (code[:-3], "damaged"),
                      (code + "=", "damaged"), (code + " x", "damaged"), ("olx3." + code[5:], "newer"),
                      ("olx2." + code[5:], "damaged"), (enc(b'{"r":"registry.example.org/kart","p":"x"}', 2), "damaged"),
                      (enc(b'{"r":"registry.example.org/kart","p":"h.example.org/o"}'), "damaged"),
                      ("olx0." + code[5:], "damaged"), (enc(b"[1,2]"), "damaged"), (enc(b"not json"), "damaged"),
                      (enc(b'{"r":"Not A Registry"}'), "damaged"), (enc(b'{"t":"x"}'), "damaged"),
                      (enc(b'{"r":"registry.example.org/kart","x":1}'), "damaged"),
                      (enc(b'{"r":"registry.example.org/kart","t":"has space"}'), "damaged"),
                      (enc(b'{"r":"registry.example.org/kart","t":7}'), "damaged"),
                      (enc(b'{"r":"registry.example.org/kart","s":"ftp://x.example.org"}'), "damaged"),
                      (enc(b'\xff\xfe'), "damaged")):
        try:
            devchannel.parse_code(bad)
            ok(False, "code: %r refused" % bad[:30])
        except devchannel.ChannelError as exc:
            ok(want in str(exc) and "kart-token" not in str(exc),
               "code: %r refused with a sentence (%s)" % (bad[:24], exc))

    # stack.py dev-code: the token from a file or stdin, never the command line
    sp = os.path.join(HERE, "stack.py")
    tmp = tempfile.mkdtemp(prefix="stacktest-code-")
    try:
        tf = os.path.join(tmp, "token")
        with open(tf, "w") as f:
            f.write("kart-token-0123\n")
        r = subprocess.run([sys.executable, sp, "dev-code", "--registry", "registry.example.org/kart",
                            "--token-file", tf, "--source", "https://code.example.org/kart"],
                           capture_output=True, text=True)
        ok(r.returncode == 0 and r.stdout.strip() == code, "dev-code --token-file: the same code (%s)" % r.stderr)
        r = subprocess.run([sys.executable, sp, "dev-code", "--registry", "registry.example.org/kart",
                            "--token-file", "-", "--source", "https://code.example.org/kart"],
                           input="kart-token-0123\n", capture_output=True, text=True)
        ok(r.returncode == 0 and r.stdout.strip() == code, "dev-code --token-file -: from stdin")
        r = subprocess.run([sys.executable, sp, "dev-code", "--registry", "registry.example.org/kart"],
                           capture_output=True, text=True)
        ok(r.returncode == 0 and r.stdout.strip() == pub, "dev-code with no token: a public code")
        r = subprocess.run([sys.executable, sp, "dev-code", "--registry", "registry.example.org/kart",
                            "--token-file", tf, "--patches", "code.example.org/kart-patches"],
                           capture_output=True, text=True)
        ok(r.returncode == 0 and r.stdout.strip() == feed, "dev-code --patches: the olx2. code")
        r = subprocess.run([sys.executable, sp, "dev-code", "--decode"], input=feed + "\n",
                           capture_output=True, text=True)
        ok(r.returncode == 0 and "patches:  code.example.org/kart-patches" in r.stdout,
           "dev-code --decode names the patch feed")
        r = subprocess.run([sys.executable, sp, "dev-code", "--decode"], input=code + "\n",
                           capture_output=True, text=True)
        ok(r.returncode == 0 and "registry.example.org/kart" in r.stdout and "token:    yes" in r.stdout
           and "kart-token" not in r.stdout, "dev-code --decode: says what is in it, not the token")
        for argv, want in ((["dev-code"], "--registry"),
                           (["dev-code", "--registry", "registry.example.org/kart", "kart-token-0123"],
                            "never given on the command line"),
                           (["dev-code", "--registry", "Not A Registry"], "registry"),
                           (["dev-code", "--registry", "registry.example.org/kart", "--token-file",
                             os.path.join(tmp, "absent")], "cannot read")):
            r = subprocess.run([sys.executable, sp] + argv, capture_output=True, text=True)
            ok(r.returncode == 2 and want in r.stderr and "kart-token" not in r.stderr,
               "stack.py %s: refused (%s)" % (" ".join(argv[:2]), r.stderr.strip()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # .channel round trip and refusals, in a scratch checkout
    tmp = tempfile.mkdtemp(prefix="stacktest-channel-")
    try:
        ok(devchannel.read_channel(tmp) == {"channel": "stable"}, ".channel absent: stable")
        devchannel.write_channel(tmp, "dev", "registry.example.org:5000/kart")
        ch = devchannel.read_channel(tmp)
        ok(ch["channel"] == "dev" and ch["registry"] == "registry.example.org:5000/kart"
           and ch["source"] == "https://registry.example.org:5000/kart" and ch.get("since"),
           ".channel dev round trip, default source (%s)" % ch)
        for bad in ("Registry.Example.org/kart", "registry.example.org/kart ; rm", "", "https://x.org/a"):
            try:
                devchannel.write_channel(tmp, "dev", bad)
                ok(False, ".channel refuses registry %r" % bad)
            except devchannel.ChannelError:
                pass
        with open(os.path.join(tmp, ".channel"), "w") as f:
            f.write("{not json")
        ch = devchannel.read_channel(tmp)
        ok(ch["channel"] == "stable" and ch.get("problem"), "a .channel that does not parse: stable, with a sentence")
        devchannel.write_channel(tmp, "stable")
        ok(not os.path.exists(os.path.join(tmp, ".channel")), "channel stable removes .channel")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for argv, want in ((["channel", "dev"], "--registry"), (["channel", "beta"], "stable or dev"),
                       (["channel", "dev", "--registry", "not a registry"], "registry")):
        r = subprocess.run([sys.executable, os.path.join(HERE, "stack.py")] + argv,
                           capture_output=True, text=True)
        ok(r.returncode == 2 and want in r.stderr, "stack.py %s: refused (%s)" % (" ".join(argv), r.stderr.strip()))
    r = subprocess.run([sys.executable, os.path.join(HERE, "stack.py"), "channel", "--json"],
                       capture_output=True, text=True)
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        doc = {}
    ok(r.returncode == 0 and doc.get("channel") in ("stable", "dev") and doc.get("images"),
       "stack.py channel --json answers")

    # docker compose config with the override: the dev image on those services only
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "info"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode != 0:
        skip("docker compose config on the dev channel (docker not available)")
        return
    if not os.path.isdir(stack.checkout_of(catalog["ffxi"], root, parent)):
        skip("docker compose config on the dev channel (no FFXI checkout)")
        return
    fd, over = tempfile.mkstemp(suffix=".yml")
    os.close(fd)
    try:
        p = stack.plan(catalog, ["ffxi"], ["config", "--format", "json"], root=root, parent=parent,
                       environ={}, channel=DEV, override_path=over)
        with open(over, "w", encoding="utf-8", newline="\n") as f:
            f.write(p["override"])
        step = p["steps"][-1]
        env = dict(os.environ)
        env.update(step["env"])
        r = subprocess.run(step["argv"], env=env, capture_output=True, text=True)
        if ok(r.returncode == 0, "docker compose config on the dev channel: %s" % r.stderr.strip()[-300:]):
            svcs = json.loads(r.stdout)["services"]
            dev = sorted(n for n, v in svcs.items() if v.get("image") == ref)
            ok(dev == ["connect", "db-update", "map", "search", "world"],
               "config: exactly the five LSB services run the dev image (%s)" % dev)
            ok(not svcs["db"]["image"].startswith("registry.example.org"), "config: the database is untouched")
            ok(set(svcs) == set(json.loads(subprocess.run(
                stack.plan(catalog, ["ffxi"], ["config", "--format", "json"], root=root, parent=parent,
                           environ={})["steps"][-1]["argv"], env=env, capture_output=True,
                text=True).stdout)["services"]), "config: the override adds no service")
    finally:
        os.remove(over)


def main():
    parent = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else stack.PARENT
    root = stack.ROOT
    catalog = stack.load_catalog()
    check_catalog(catalog, root, parent)
    check_plan(catalog, root, parent)
    check_env_and_state()
    check_report_and_ps(catalog)
    check_volumes()
    check_docker(catalog, root, parent)
    check_channel(catalog, root, parent)
    print("stack_test: %d failed, %d skipped" % (len(FAILS), len(SKIPS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
