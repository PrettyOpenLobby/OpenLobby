#!/usr/bin/env python3
"""One step of the .polpatch toolkit, for the deployer.

The deployer starts this as an unprivileged user, with a bare environment
and no access to the Docker socket, for every step that unpacks a .polpatch
or reads a client install:

    patchtool.py <tools/patch folder> verify <file> --result <json> [--quick] [--against <bundle>]
    patchtool.py <tools/patch folder> build --source <install> --region R --product P
                 --out <file> --result <json> [--version V] [--walk] [--alias A ...]

It prints the toolkit's progress for the job log and writes its answer as
JSON to --result: {"ok": bool, "problems": [sentence, ...], "polpatch":
{region, product, kind, versions, title} or null}. Standard library only,
apart from the toolkit it is pointed at.
"""
import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="patchtool.py")
    ap.add_argument("toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.add_argument("--result", required=True)
    v.add_argument("--quick", action="store_true")
    v.add_argument("--against")
    b = sub.add_parser("build")
    b.add_argument("--source", required=True)
    b.add_argument("--region", required=True)
    b.add_argument("--product", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--result", required=True)
    b.add_argument("--version", default="auto")
    b.add_argument("--walk", action="store_true")
    b.add_argument("--alias", action="append", default=[])
    a = ap.parse_args(argv)

    sys.path.insert(0, a.toolkit)
    import polpatch_lib as lib

    result = {"ok": False, "problems": [], "polpatch": None}
    try:
        if a.cmd == "verify":
            problems = lib.verify(a.file, against=a.against, deep=not a.quick)
            result["problems"] = problems
            if not problems:
                m = lib.read_manifest(a.file)
                print(lib.summary(m))
                result["polpatch"] = {k: m.get(k) for k in
                                      ("region", "product", "kind", "versions", "title")}
                result["ok"] = True
                print("[PASS] the .polpatch passes the %s check" % ("quick" if a.quick else "full"))
            else:
                for p in problems:
                    print("    " + p)
                print("[FAIL] %d problem(s)" % len(problems))
        else:
            m = lib.build_full(a.source, a.region, a.product, a.out, version=a.version,
                               aliases=a.alias, walk=a.walk, log=print)
            print(lib.summary(m))
            result["polpatch"] = {k: m.get(k) for k in
                                  ("region", "product", "kind", "versions", "title")}
            result["ok"] = True
    except lib.PolpatchError as exc:
        result["problems"] = [str(exc)]
        print("[FAIL] %s" % exc)
    sys.stdout.flush()
    tmp = a.result + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, a.result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
