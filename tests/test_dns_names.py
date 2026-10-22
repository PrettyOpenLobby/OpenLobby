#!/usr/bin/env python3
"""Names other installs add to this resolver (stub.py dns-names, docs/installs.md).

    python tests/test_dns_names.py

A game's setup adds its host names under its key; the resolver answers them without a
restart, and removing the key takes them away again. Bad keys and names are refused and
write nothing.
"""
import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

import stub  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stub.dns_names_cli(list(args), folder=folder)
    return code, out.getvalue() + err.getvalue()


folder = os.path.join(tempfile.mkdtemp(prefix="dnsnames-"), "dns.d")
names = stub.DnsNames(folder, every=0)

print("an install adds its names")
chk("nothing yet", names.names(), [])
code, _out = cli("add", "kart", "Lobby.Kart.Example", "ranking.kart.example.")
chk("add answers 0", code, 0)
chk("its names, lower case, no trailing dot", names.names(), ["lobby.kart.example", "ranking.kart.example"])
chk("a subdomain of an added name matches",
    stub.dns_name_listed("eu.lobby.kart.example", names.names()), True)
chk("another install's names add to them",
    (cli("add", "bowl", "bowl.example")[0], names.names()),
    (0, ["bowl.example", "lobby.kart.example", "ranking.kart.example"]))
chk("adding again under the same key replaces that key's names",
    (cli("add", "kart", "new.kart.example")[0], names.names()),
    (0, ["bowl.example", "new.kart.example"]))
code, out = cli("list")
chk("list shows each key", (code, sorted(out.split("\n")[:2])), (0, ["bowl: bowl.example", "kart: new.kart.example"]))

print("refused, and nothing written")
before = sorted(os.listdir(folder))
chk("a key that is not one", cli("add", "../etc", "a.example")[0], 2)
chk("a name that is not a host name", cli("add", "kart", "bad name")[0], 2)
chk("no names", cli("add", "kart")[0], 2)
chk("an unknown command", cli("wipe")[0], 2)
chk("...no file changed", sorted(os.listdir(folder)), before)
chk("the names are still the last good ones", names.names(), ["bowl.example", "new.kart.example"])

print("an install leaves")
chk("remove answers 0", cli("remove", "bowl")[0], 0)
chk("its names are gone", names.names(), ["new.kart.example"])
chk("removing a key that is not there is fine", cli("remove", "bowl")[0], 0)

print("a missing folder is no names, not an error")
chk("nothing to read", stub.DnsNames(os.path.join(folder, "nope"), every=0).names(), [])

print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
sys.exit(1 if bad else 0)
