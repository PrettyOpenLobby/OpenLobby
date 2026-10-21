#!/usr/bin/env python3
"""dns.public_forward: which names a PUBLIC-mode client gets forwarded.

    python tests/test_dns_public_forward.py

run_dns() binds udp/53 and only takes the public path for a global source
address, so this exercises the matcher it uses, stub.dns_name_listed(), plus
the REFUSED reply a non-listed name still draws.
"""
import os
import struct
import sys

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


def query(name, qtype=1):
    q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\x00" + struct.pack(">HH", qtype, 1)


# The same normalisation run_dns() applies to the config list.
listed = [h.lower().rstrip(".") for h in ["Update.Example.ORG."]]

print("allowlist matching")
chk("exact name", stub.dns_name_listed("update.example.org", listed), True)
chk("exact name, mixed case + trailing dot",
    stub.dns_name_listed("UPDATE.example.org.", listed), True)
chk("subdomain", stub.dns_name_listed("a.b.update.example.org", listed), True)
chk("parent zone is not listed", stub.dns_name_listed("example.org", listed), False)
chk("suffix without a dot boundary",
    stub.dns_name_listed("notupdate.example.org", listed), False)
chk("unrelated name", stub.dns_name_listed("www.example.com", listed), False)
chk("empty name", stub.dns_name_listed("", listed), False)
chk("None name", stub.dns_name_listed(None, listed), False)
chk("empty list", stub.dns_name_listed("update.example.org", []), False)

print("a non-listed name is still REFUSED")
q = query("www.example.com")
qname, qtype = stub.dns_parse_question(q)
chk("parsed name", qname, "www.example.com")
chk("not listed", stub.dns_name_listed(qname, listed), False)
r = stub.dns_refused(q)
chk("rcode REFUSED", r[3] & 0x0F, 5)
chk("no longer than the query", len(r) <= len(q), True)

print()
print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
