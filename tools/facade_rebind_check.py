#!/usr/bin/env python3
"""Catch monkeypatches that a responders.py module split has silently defused.

    python tools/facade_rebind_check.py         # fail on any UNEXPLAINED rebinding
    python tools/facade_rebind_check.py -v      # also list the explained ones

WHY. `responders.py` is being split by moving a block of it into its own module
and re-exporting the names (`from srvcore import (...)`). That is transparent
for READS -- `responders.log` IS `srvcore.log`, the same object -- and it is safe
for the module-level dicts and sets, because responders.py contains no `global`
statements at all: every one of them is mutated in place, never rebound, so
`responders._STAMPS is authtoken._STAMPS` and both sides see every write.

It is NOT transparent to REBINDING, and that is the whole reason this file
exists:

    R._session_member_id = lambda: 7      # tools/group_check.py:78

rebinds the name in RESPONDERS' namespace. If `_session_member_id` has been
moved to another module, every caller inside THAT module still resolves it from
its own globals and goes on calling the real function. The stub is simply never
consulted -- and nothing raises, nothing logs, and the test still prints its
[PASS] line, because the assertion it makes is usually about a value the real
function is perfectly capable of producing. **A test that has stopped testing
what it says it does still passes.** That is the failure mode this guards.

It is worse than one-directional. Once a name is re-exported it is reachable
through TWO namespaces, and a patch applied to either one misses the callers
that resolve through the other. So a re-exported name that is called from both
modules cannot be reliably monkeypatched anywhere, and the honest answer is to
not move it.

MEASURED, 2026-08-27. A second cut of responders.py -- the session table, lines
~10654-11093 -- was written, verified byte-identical, verified to leave
`dir(responders)` unchanged at all 832 names, and verified to preserve object
identity on every shared container. It still broke 7 test tools, because 21 of
this suite's 49 rebindings target exactly those session functions
(`_session_member_id`, `_session_handle_id`, `_session_get`, `_session_current`)
across 9 files, which use them as the seam for "pretend this thread is member N".
The cut was backed out. The set of names below is therefore not a style rule --
it is the measured ceiling on how far that refactor can go.

THE RULE: every rebinding of a re-exported name is either listed in EXPECTED
below, with a reason that says why it still works, or it is a finding.

WARNING: Match aliases, not the literal module name. Nearly every tool here does
`import responders as R`, so a scan for `responders.` finds almost none of them.
That is why this walks the AST for the names actually bound to the module.
"""
import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
SCAN_DIRS = ("tools", "services", "lsb")

#: (file, attr) -> why this rebinding still reaches the callers that matter.
#: Keep the reason specific enough that the next reader can tell whether it
#: still holds -- "it works" is not a reason, "the asserting caller lives in
#: responders.py" is.
EXPECTED = {
    ("tools/lobby_bind_test.py", "log"):
        "log now lives in srvcore, but every line this suite asserts on is "
        "emitted by _lobby_arbitrate / _lobby_bind, which are still IN "
        "responders.py and so resolve `log` from responders' globals -- the "
        "tee. A log call made from inside srvcore or authtoken would NOT be "
        "captured, so this holds only while the asserted lines stay put.",
    ("tools/presence_grace_test.py", "log"):
        "same shape as lobby_bind_test: the grace-timer lines it reads come "
        "from _logout_grace / _broadcast_presence in responders.py.",
}


def facade_map():
    """name -> module it was moved to, read out of responders.py's own imports.

    Discovered rather than hard-coded, so a new cut is covered the moment it
    lands instead of when somebody remembers to update this list.
    """
    src = open(os.path.join(SERVICES, "responders.py"), encoding="utf-8").read()
    owner = {}
    for node in ast.parse(src).body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not os.path.exists(os.path.join(SERVICES, node.module + ".py")):
            continue                        # stdlib / third-party, not a cut
        for a in node.names:
            if a.name != "*":
                owner[a.asname or a.name] = node.module
    return owner


def module_aliases(tree, targets):
    """Every local name bound to one of `targets` in this file."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in targets:
                    out[a.asname or a.name] = a.name
    return out


def scan(owner, verbose=False):
    watched = {"responders"} | set(owner.values())
    findings, explained = [], []
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
                aliases = module_aliases(tree, watched)
                if not aliases:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    for tgt in node.targets:
                        if not (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id in aliases):
                            continue
                        attr = tgt.attr
                        if attr not in owner:
                            continue        # not a moved name: patch is fine
                        via = aliases[tgt.value.id]
                        home = owner[attr]
                        # Patching either namespace misses callers resolving
                        # through the other one. Name both, so the reader can
                        # see what is actually being missed.
                        missed = home if via == "responders" else "responders"
                        rec = (rel, node.lineno, f"{tgt.value.id}.{attr}",
                               home, missed)
                        if (rel, attr) in EXPECTED:
                            explained.append(rec)
                        else:
                            findings.append(rec)
    return findings, explained


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list the rebindings EXPECTED explains")
    args = ap.parse_args()

    owner = facade_map()
    if not owner:
        print("no re-exported names in responders.py -- nothing to check")
        return 0
    mods = sorted(set(owner.values()))
    print(f"responders.py re-exports {len(owner)} name(s) from: {', '.join(mods)}")

    findings, explained = scan(owner, args.verbose)

    # An allowlist entry that no longer matches anything is stale -- it will
    # silently excuse a future rebinding at the same (file, attr).
    live = {(r[0], r[2].split(".", 1)[1]) for r in explained}
    stale = sorted(set(EXPECTED) - live)
    if stale:
        print("\n%d EXPECTED entr(ies) no longer match -- drop them: %s"
              % (len(stale), ", ".join(f"{f}:{a}" for f, a in stale)))

    if args.verbose and explained:
        print(f"\n{len(explained)} explained rebinding(s):")
        for rel, ln, what, home, missed in explained:
            print(f"  {rel}:{ln}  {what}")
            print(f"      {what.split('.')[1]} lives in {home}; "
                  f"{EXPECTED[(rel, what.split('.', 1)[1])]}")

    if not findings:
        print("\nOK: every rebinding of a re-exported name is explained.")
        return 0 if not stale else 1

    print(f"\n{len(findings)} UNEXPLAINED rebinding(s) of a re-exported name:\n")
    for rel, ln, what, home, missed in findings:
        print(f"  {rel}:{ln}")
        print(f"      {what}  --  '{what.split('.', 1)[1]}' now lives in "
              f"{home}.py")
        print(f"      callers inside {missed}.py resolve it from their OWN "
              f"globals and will NOT see this patch")
    print("\nEach is either a real defused monkeypatch, or belongs in EXPECTED "
          "with a reason.\nIf the name is a test seam called from both modules, "
          "the right fix is usually to\nmove it BACK -- see "
          "responders-facade-rebinding-limit.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
