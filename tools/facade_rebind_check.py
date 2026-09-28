#!/usr/bin/env python3
"""Prove that monkeypatching through the responders facade still reaches the code.

    python tools/facade_rebind_check.py        # fail on any rebinding the facade does not forward
    python tools/facade_rebind_check.py -v     # also list every rebinding found

WHY. services/responders.py is a facade over the services/core package. Reads
of `responders.<name>` go to the module that owns the name. That is not enough
for the test tools: they REBIND names to fake a session or capture output,

    R._session_member_id = lambda: 7      # tools/group_check.py

and a rebinding that lands on the facade alone would leave every caller inside
the core still calling the real function. Nothing would raise and the test
would keep printing [PASS] while testing nothing. A first attempt to cut
responders.py (2026-08-27) was backed out for exactly this reason: 21 of the
suite's rebindings target the session helpers, across 9 files.

The facade therefore forwards writes to the owning module, and for a name
that modules import by copy (`log`, `accounts`, ...) to every module that
holds a copy. This tool checks that promise against the rebindings the tools
actually make, empirically: it sets a sentinel through the facade and reads it
back through the owning module and every core module that has the name.

Aliases matter: nearly every tool does `import responders as R`, so this walks
the AST for the names bound to the module rather than grepping for
`responders.`.
"""
import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
SCAN_DIRS = ("tools", "services", "tests", "lsb")


def module_aliases(tree):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "responders":
                    out[a.asname or a.name] = a.name
    return out


def rebindings():
    """[(file, line, attr)] for every `<alias>.<attr> = ...` on responders."""
    found = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except (SyntaxError, UnicodeDecodeError):
                    continue
                aliases = module_aliases(tree)
                if not aliases:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    for tgt in node.targets:
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id in aliases):
                            found.append((rel, node.lineno, tgt.attr))
    return found


def forwards(R, attr):
    """Does a write of `attr` through the facade reach the owning module (and
    every module holding a copy)? Returns a reason string on failure."""
    owners = getattr(R, "_OWNERS", None)
    modules = getattr(R, "_MODULES", None)
    if owners is None or modules is None:
        return "responders.py is not the facade (no _OWNERS/_MODULES)"
    home = owners.get(attr)
    if home is None:
        return "not a name of the old responders.py; the patch lands on the facade only"
    sentinel = object()
    saved = {m: vars(mod)[attr] for m, mod in modules.items() if attr in vars(mod)}
    try:
        setattr(R, attr, sentinel)
        if getattr(modules[home], attr, None) is not sentinel:
            return f"write did not reach the owner core.{home}"
        for m, mod in modules.items():
            if m in saved and getattr(mod, attr) is not sentinel:
                return f"core.{m} still holds the old copy"
        if getattr(R, attr) is not sentinel:
            return "read-back through the facade returned something else"
    finally:
        for m, old in saved.items():
            setattr(modules[m], attr, old)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, SERVICES)
    import responders as R

    found = rebindings()
    attrs = sorted({a for _f, _l, a in found})
    print(f"{len(found)} rebinding(s) of {len(attrs)} name(s) across the tools")
    failures = []
    for attr in attrs:
        why = forwards(R, attr)
        sites = [f"{f}:{l}" for f, l, a in found if a == attr]
        if why:
            failures.append((attr, why, sites))
        elif args.verbose:
            print(f"  [PASS] {attr:<28} -> core.{R._OWNERS[attr]}  ({', '.join(sites)})")
    for attr, why, sites in failures:
        print(f"  [FAIL] {attr}: {why}\n         at {', '.join(sites)}")
    if failures:
        print(f"\n{len(failures)} name(s) are patched by a tool but not forwarded by the facade")
        return 1
    print("every rebinding the tools make is forwarded to the module that runs it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
