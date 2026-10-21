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
        d = stack.checkout_of(e, root, parent)
        if not os.path.isdir(d):
            skip("%s: no checkout at %s" % (key, d))
            continue
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
    fl = [os.path.relpath(f, parent).replace(os.sep, "/") for f in p["files"]]
    ok(fl == ["openlobby/docker-compose.yml",
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
    fl = [os.path.relpath(f, parent).replace(os.sep, "/") for f in p["files"]]
    ok("hippaulmaster/docker-compose.ghcr.yml" in fl and "hippaulholo/docker-compose.ghcr.yml" not in fl,
       "ghcr tm+jan: tm's override in, jan's out (%s)" % fl)
    ok(p["steps"][-1]["env"]["POL_TITLE_BASE_IMAGE"] == "ghcr.io/prettyopenlobby/hippaulmaster:0.2.0",
       "ghcr tm+jan: jan builds on the published tm image")
    ok(len(p["steps"]) == 1, "ghcr tm+jan: nothing to prebuild")
    p = stack.plan(catalog, ["fe", "fmo"], ["up", "-d"], ghcr=True, root=root, parent=parent, environ={})
    ok(p["steps"][0]["env"].get("POL_TITLE_BASE_IMAGE") == "ghcr.io/prettyopenlobby/openlobby:latest",
       "ghcr fe+fmo: the first local link builds on the published core")
    fl = [os.path.relpath(f, parent).replace(os.sep, "/") for f in p["files"]]
    ok("hippaulring/docker-compose.ghcr.yml" in fl and "openlobby/docker-compose.ghcr.yml" in fl,
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


def main():
    parent = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else stack.PARENT
    root = stack.ROOT
    catalog = stack.load_catalog()
    check_catalog(catalog, root, parent)
    check_plan(catalog, root, parent)
    check_env_and_state()
    check_report_and_ps(catalog)
    check_docker(catalog, root, parent)
    print("stack_test: %d failed, %d skipped" % (len(FAILS), len(SKIPS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
