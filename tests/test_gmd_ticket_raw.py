#!/usr/bin/env python3
"""A GM Call ticket keeps the raw 0x102 body it was decoded from.

    python tests/test_gmd_ticket_raw.py

gmd.write_ticket files the decoded fields and, beside them, the body's first
0x1E0 bytes as hex ("raw"); the padding and checksum after that are not kept.
Runs on a synthetic body in a temporary ticket directory.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
TMP = tempfile.mkdtemp(prefix="gmd-ticket-")
os.environ["POL_GMD_TICKET_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ.setdefault("POL_GMD_CHAT_ROOM", "#gmchat001")

import gmd  # noqa: E402

bad = 0


def chk(what, ok, detail=""):
    global bad
    bad += not ok
    print("  %s %s%s" % ("ok  " if ok else "FAIL", what, "  " + detail if detail else ""))


g = gmd.Gmd()
body = bytearray(0x1F8)                  # a datagram's worth, padding and all
body[0x40:0x43] = b"Lex"
body[0x20] = 7                           # a byte in the undecoded block
body[0x1E0:] = b"\xAA" * 0x18            # past the ticket: must not be kept
rec = g.write_ticket(bytes(body), 7, "198.51.100.1:1")
raw = bytes.fromhex(rec.get("raw", ""))
chk("the ticket keeps its raw body, exactly 0x1E0 bytes", len(raw) == 0x1E0,
    str(len(raw)))
chk("...and it is the body the fields were read from",
    raw[0x40:0x43] == b"Lex" and rec["handle"] == "Lex" and raw[0x20] == 7)
chk("the padding after the body is not kept", b"\xAA" not in raw)
chk("the filed ticket carries it too",
    any('"raw"' in open(os.path.join(TMP, n), encoding="utf-8").read()
        for n in os.listdir(TMP) if n.startswith("gm-") and n.endswith(".json")))
print("gmd ticket raw body: " + ("OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
