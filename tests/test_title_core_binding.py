#!/usr/bin/env python3
"""Every name in the title contract is bound by the core.

    python tests/test_title_core_binding.py

`titles.Core.__doc__` lists the names a title may use, and `core/boot.py`
binds them with one `titles.bind_core(...)` call. A documented name that the
call leaves out is silently None inside every title. That happened to
`_advertise_configured`: Tetra Master's zone list then handed every client the
raw POL_ADVERTISE address instead of the per-client one, and players outside
the tailnet could not open a room (2026-09-28).

Read statically, so the test needs no database and no running core.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "services")


def documented_names():
    tree = ast.parse(open(os.path.join(SERVICES, "titles.py"), encoding="utf-8").read())
    core = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Core")
    doc = ast.get_docstring(core)
    return {m.group(1) for m in re.finditer(r"^\s{2,6}([A-Za-z_]\w*)(?=[\s(]|$)", doc, re.M)}


def bound_names():
    tree = ast.parse(open(os.path.join(SERVICES, "core", "boot.py"), encoding="utf-8").read())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "bind_core"]
    assert len(calls) == 1, f"expected one bind_core call in core/boot.py, found {len(calls)}"
    return {k.arg for k in calls[0].keywords}


def main():
    documented, bound = documented_names(), bound_names()
    failures = 0
    for name in sorted(documented - bound):
        print(f"  FAIL {name}: in the title contract but not passed to bind_core")
        failures += 1
    for name in sorted(bound - documented):
        print(f"  FAIL {name}: passed to bind_core but missing from titles.Core.__doc__")
        failures += 1
    if "_advertise_configured" not in bound:
        print("  FAIL _advertise_configured is not bound (the 2026-09-28 zone host regression)")
        failures += 1
    print(f"  {len(documented)} contract names, {len(bound)} bound")
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
