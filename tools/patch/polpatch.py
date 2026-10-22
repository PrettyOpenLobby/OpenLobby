#!/usr/bin/env python3
"""Make, check and install .polpatch files.

A .polpatch is one patch bundle packed into a single file with a manifest of
every file's size and sha256, so a bundle built where the client is installed
can be carried to the machine running the patch service. See docs/polpatch.md.

    python tools/patch/polpatch.py build-full --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" \\
        --region W2U --product 0002 -o TetraMaster-W2U.polpatch
    python tools/patch/polpatch.py build-update --base TetraMaster-W2U.polpatch \\
        --files stage/tetra --version 20040714_0 -o TetraMaster-W2U-20040714_0.polpatch
    python tools/patch/polpatch.py from-bundle --bundle mirrors/W2U-0002 -o W2U-0002.polpatch
    python tools/patch/polpatch.py inspect TetraMaster-W2U.polpatch
    python tools/patch/polpatch.py verify TetraMaster-W2U.polpatch [--against mirrors/W2U-0002]
    python tools/patch/polpatch.py apply TetraMaster-W2U.polpatch --mirrors mirrors

Exit code 0 when the step worked, 1 when it was refused or failed, with the
reason on the last line.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polpatch_lib as lib                                       # noqa: E402


def _limits(a):
    out = {}
    if getattr(a, "max_total_gib", None) is not None:
        out["max_total"] = int(a.max_total_gib * lib.GIB)
    if getattr(a, "max_file_gib", None) is not None:
        out["max_file"] = int(a.max_file_gib * lib.GIB)
    if getattr(a, "max_entries", None) is not None:
        out["max_entries"] = a.max_entries
    return out


def _arg(check):
    """argparse type that runs one of the library's format checks."""
    def conv(value):
        try:
            return check(value)
        except lib.PolpatchError as e:
            raise argparse.ArgumentTypeError(str(e))
    return conv


def _region(v):
    return _arg(lib.check_region)(v)


def _version_or_auto(v):
    return v if v == "auto" else _arg(lib.check_version)(v)


def _done(m, out):
    print(lib.summary(m))
    print(f"[*] wrote {out}")
    return 0


def cmd_build_full(a):
    m = lib.build_full(a.source, a.region, a.product, a.out, version=a.version,
                       aliases=a.alias or (), note=a.note or "", walk=a.walk,
                       excludes=a.exclude or (), limits=_limits(a), log=print)
    return _done(m, a.out)


def cmd_build_update(a):
    m = lib.build_update(a.base, a.files, a.version, a.out, note=a.note or "",
                         limits=_limits(a), log=print)
    return _done(m, a.out)


def cmd_from_bundle(a):
    m = lib.build_from_bundle(a.bundle, a.out, note=a.note or "",
                              limits=_limits(a), log=print)
    return _done(m, a.out)


def cmd_inspect(a):
    m = lib.read_manifest(a.file, _limits(a))
    print(lib.summary(m))
    if a.files:
        for name, ent in sorted(m["files"].items()):
            print(f"    {ent['size']:>12}  {name}")
    print("[*] this reads the manifest only; run verify for the full check")
    return 0


def cmd_verify(a):
    problems = lib.verify(a.file, against=a.against, limits=_limits(a),
                          deep=not a.quick)
    if problems:
        for p in problems:
            print(f"    {p}")
        print(f"[FAIL] {len(problems)} problem(s) in {a.file}")
        return 1
    print(lib.summary(lib.read_manifest(a.file, _limits(a))))
    print(f"[PASS] {a.file} is a good .polpatch")
    return 0


def cmd_apply(a):
    r = lib.apply(a.file, a.mirrors, limits=_limits(a), deep=not a.quick, log=print)
    print(f"[*] {r['action']}: {r['region']}/{r['product']} "
          f"{', '.join(r['versions'])} in {r['bundle']}")
    if r["action"] != "unchanged":
        print("restart the patch service (docker compose restart patch); it reads "
              "bundles once at startup")
    return 0


def _ask(prompt, default=""):
    try:
        value = input(prompt).strip()
    except EOFError:
        raise lib.PolpatchError("No answer was given, so nothing was built.")
    # A folder dragged onto the window arrives in quotes.
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value or default


def _menu(title, items):
    keys = list(items)
    print(title)
    for i, k in enumerate(keys, 1):
        print(f"  {i:>2}. {k}  {items[k]}")
    while True:
        pick = _ask("Number: ")
        if pick.isdigit() and 1 <= int(pick) <= len(keys):
            return keys[int(pick) - 1]
        print(f"Type a number from 1 to {len(keys)}.")


def cmd_wizard(a):
    """Questions and answers for polpatch.cmd: build a full .polpatch."""
    print("Make a .polpatch from a PlayOnline client installed on this PC.\n"
          "It holds Square Enix's files: use it for your own servers only.\n")
    source = a.source
    while not source or not os.path.isdir(source):
        if source:
            print(f"{source} is not a folder.")
        source = _ask("Install folder of the title (you can drag the folder here): ")
    product = _menu("\nWhich title is it?", lib.PRODUCTS)
    region = _menu("\nWhich client and region?", lib.REGIONS)
    walk = False
    if not os.path.isfile(os.path.join(source, "file.txt")):
        print(f"\n{source} has no file.txt, the list of files a title normally keeps.")
        if _ask("Take every file in the folder instead? [y/N] ").lower() != "y":
            raise lib.PolpatchError("Nothing was built; pick the folder that holds file.txt.")
        walk = True
    out_dir = _ask(f"\nSave the .polpatch in which folder? [Enter for {a.out_dir}] ",
                   a.out_dir)
    version = "auto"
    while True:
        tmp_out = os.path.join(out_dir, f"{region}-{product}.polpatch.building")
        try:
            m = lib.build_full(source, region, product, tmp_out, version=version,
                               walk=walk, log=print)
            break
        except lib.PolpatchError as e:
            if version != "auto" or "--version auto" not in str(e):
                raise
            print(f"\nThe version could not be read from the install: {e}")
            while not lib.VERSION_RE.fullmatch(version):
                version = _ask("Type the version this install is at (YYYYMMDD_X, "
                               "such as 20040714_0): ")
    out = os.path.join(out_dir, f"{region}-{product}-{m['versions'][-1]}.polpatch")
    os.replace(tmp_out, out)
    return _done(m, out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def limits(p):
        p.add_argument("--max-total-gib", type=float,
                       help="largest total unpacked size accepted (default 16)")
        p.add_argument("--max-file-gib", type=float,
                       help="largest single file accepted (default 4)")
        p.add_argument("--max-entries", type=int,
                       help="most files accepted (default 500000)")

    p = sub.add_parser("build-full", help="build from an installed client (polsynth)")
    p.add_argument("--source", required=True, help="the installed title's folder")
    p.add_argument("--region", required=True, type=_region)
    p.add_argument("--product", required=True, type=_arg(lib.check_product))
    p.add_argument("--alias", action="append", type=_region,
                   help="another region code the bundle answers for; repeatable")
    p.add_argument("--version", default="auto", type=_version_or_auto,
                   help="YYYYMMDD_X; the default, auto, reads the install's patch.ver")
    p.add_argument("--walk", action="store_true",
                   help="take every file in --source instead of reading file.txt")
    p.add_argument("--exclude", action="append",
                   help="with --walk: a path or folder name to leave out; repeatable")
    p.add_argument("--note", help="free text kept in the manifest")
    p.add_argument("-o", "--out", required=True)
    limits(p)
    p.set_defaults(fn=cmd_build_full)

    p = sub.add_parser("build-update", help="add a version on top of a bundle (polpublish)")
    p.add_argument("--base", required=True, help="a bundle folder or a .polpatch")
    p.add_argument("--files", required=True,
                   help="folder of the changed and added files, laid out like the install")
    p.add_argument("--version", required=True, type=_arg(lib.check_version))
    p.add_argument("--note")
    p.add_argument("-o", "--out", required=True)
    limits(p)
    p.set_defaults(fn=cmd_build_update)

    p = sub.add_parser("from-bundle", help="pack an existing bundle folder")
    p.add_argument("--bundle", required=True, help="mirrors/<REGION>-<PRODUCT>")
    p.add_argument("--note")
    p.add_argument("-o", "--out", required=True)
    limits(p)
    p.set_defaults(fn=cmd_from_bundle)

    p = sub.add_parser("inspect", help="show what a .polpatch holds")
    p.add_argument("file")
    p.add_argument("--files", action="store_true", help="list every file too")
    limits(p)
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("verify", help="check a .polpatch completely")
    p.add_argument("file")
    p.add_argument("--against", help="the bundle folder it would be applied onto")
    p.add_argument("--quick", action="store_true",
                   help="hash every file but do not unpack the blobs")
    limits(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("apply", help="install or merge a .polpatch into a mirrors folder")
    p.add_argument("file")
    p.add_argument("--mirrors", required=True)
    p.add_argument("--quick", action="store_true",
                   help="hash every file but do not unpack the blobs")
    limits(p)
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("wizard", help="ask questions and build a full .polpatch "
                                      "(what polpatch.cmd runs)")
    p.add_argument("source", nargs="?", help="the installed title's folder")
    p.add_argument("--out-dir", default=os.getcwd(),
                   help="folder offered for the result")
    p.set_defaults(fn=cmd_wizard)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except lib.PolpatchError as e:
        print(f"[FAIL] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
