#!/usr/bin/env python3
"""Read and rebuild the PlayOnline Viewer's certificate store.

    usr/all/url/cert.db

That file is the client's ENTIRE notion of who to trust, and it is why a
self-signed server certificate produces POL-1331: the store holds four CA roots
and ours chains to none of them, so the Viewer completes the TLS handshake and
then resets with zero application bytes.

    python tools/certdb.py list  <cert.db>
    python tools/certdb.py dump  <cert.db> -o DIR      # each CA as .der/.pem
    python tools/certdb.py add   <cert.db> ca.pem -o cert.db.new
    python tools/certdb.py check <cert.db>             # round-trip proof

## Format

    off 0   uint32 LE   offset of the payload (328 on every copy seen)
    off 4   uint32 LE   512 -- see below
    off 8   zeros
    off 64  8 bytes, then 64 bytes of data
    off 256 8 bytes, then 64 bytes of data
    off 328 one ASN.1 SEQUENCE of ENTRIES

An entry is not a bare certificate:

    SEQUENCE {
        PrintableString    display label, SE's own style:
                           C="US", O="VeriSign, Inc.", OU="Class 1 Public ..."
        Certificate        the X.509 itself
    }

Handing one straight to OpenSSL therefore fails with
`wrong tag ... Field=cert_info` -- it finds the label where the TBSCertificate
should be. That, not the length encoding, is the thing that makes this module
necessary rather than a one-line dd.

The two 64-byte blobs are unidentified. 64 bytes is 512 bits and the header
carries 512, so they look like an RSA-512 signature or modulus, which would
mean the store is integrity-protected and cannot be extended. Against that: the
PS2 loader for this file (0x00ba4c50) has no crypto anywhere on its path -- it
reads the file and indexes it. `add` therefore preserves both blobs byte for
byte and leaves the question to an empirical test, which is cheap because the
original is one file copy away.

## Lengths

Three lengths per entry are padded to the 3-byte long form (`0x83 xx xx xx`):
the entry's own, the label's, and the certificate's outermost. Everything
INSIDE the certificate is ordinary minimal DER -- 39 short-form lengths in the
first CA. (An earlier note claimed "every element" used the long form; it does
not, and assuming so produces certificates the client would reject.)

Run `check` after any change to this file: it rebuilds the store from its own
parsed parts and asserts the result is byte-identical to the input.
"""
import argparse
import os
import subprocess
import sys
import tempfile

HDR_PAYLOAD_OFF = 0
PAYLOAD_START = 328


# --------------------------------------------------------------------------- #
# ASN.1 length forms
# --------------------------------------------------------------------------- #
def _read_len(buf, i):
    """-> (length, header_size). Accepts short form and 0x81/0x82/0x83."""
    b = buf[i]
    if b < 0x80:
        return b, 1
    n = b & 0x7F
    if n == 0 or n > 4:
        raise ValueError(f"unsupported length form 0x{b:02x} at {i}")
    return int.from_bytes(buf[i + 1:i + 1 + n], "big"), 1 + n


# MEASURED, and it corrects the note this was first written from. The old claim
# was that SE encodes "every element" in the 3-byte long form. It does not:
# inside a certificate the encoding is ordinary DER (39 short-form lengths in
# the first CA, with 0x81/0x82 where the content actually needs them). The ONLY
# thing rewritten is each certificate's OUTERMOST length, which is padded to the
# 3-byte form -- and the store's own SEQUENCE likewise.
#
# So the transform is a two-line header swap, not a recursive re-encode, and the
# certificates are otherwise byte-identical to what any CA would hand you. That
# also means the client's ASN.1 parser reads ordinary minimal DER -- it must, it
# parses server certificates off the wire -- so a certificate appended in plain
# DER would very likely be accepted too. We match SE's form anyway: costs
# nothing and removes one variable from a test we can only run by hand.

def _emit_der(tag, content):
    """Minimal DER length encoding, which is what OpenSSL will accept."""
    n = len(content)
    if n < 0x80:
        ln = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        ln = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + ln + content


def to_der(se_bytes):
    """SE form -> minimal DER. Outer length only; contents are already DER."""
    tag = se_bytes[0]
    ln, hs = _read_len(se_bytes, 1)
    return _emit_der(tag, se_bytes[1 + hs:1 + hs + ln])


def to_se(der_bytes):
    """Minimal DER -> SE form. Outer length only."""
    tag = der_bytes[0]
    ln, hs = _read_len(der_bytes, 1)
    content = der_bytes[1 + hs:1 + hs + ln]
    return bytes([tag, 0x83]) + len(content).to_bytes(3, "big") + content


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
class CertDB:
    def __init__(self, raw):
        self.raw = raw
        self.header = bytearray(raw[:PAYLOAD_START])
        off = int.from_bytes(raw[0:4], "little")
        if off != PAYLOAD_START:
            raise ValueError(f"unexpected payload offset {off}")
        tag = raw[off]
        ln, hs = _read_len(raw, off + 1)
        if tag != 0x30:
            raise ValueError(f"payload is not a SEQUENCE (tag 0x{tag:02x})")
        self.payload = raw[off + 1 + hs:off + 1 + hs + ln]
        if off + 1 + hs + ln != len(raw):
            raise ValueError("trailing bytes after the payload")

    def certs(self):
        """Each ENTRY as it sits in the file (SE outer-length form)."""
        out, i = [], 0
        while i < len(self.payload):
            ln, hs = _read_len(self.payload, i + 1)
            out.append(self.payload[i:i + 1 + hs + ln])
            i += 1 + hs + ln
        return out

    @staticmethod
    def split_entry(entry):
        """-> (label, certificate DER).

        An entry is NOT a bare certificate -- it is

            SEQUENCE {
                PrintableString   a display label, SE's own style:
                                  C="US", O="VeriSign, Inc.", OU="Class 1 ..."
                Certificate       the X.509 itself, ordinary DER
            }

        which is why handing one straight to OpenSSL fails with
        `wrong tag ... Field=cert_info`: it finds the label where the
        TBSCertificate should be.
        """
        ln, hs = _read_len(entry, 1)
        body = entry[1 + hs:1 + hs + ln]
        # label
        tag = body[0]
        if tag != 0x13:
            raise ValueError(f"entry does not start with a label (0x{tag:02x})")
        llen, lhs = _read_len(body, 1)
        label = body[1 + lhs:1 + lhs + llen].decode("latin-1")
        rest = body[1 + lhs + llen:]
        # certificate, re-emitted with minimal lengths so OpenSSL will take it
        clen, chs = _read_len(rest, 1)
        cert = _emit_der(rest[0], rest[1 + chs:1 + chs + clen])
        return label, cert

    @staticmethod
    def make_entry(label, cert_der):
        """Build an entry in SE's shape and outer-length form.

        Exactly TWO lengths are padded to the 3-byte form: the entry's own and
        the label's (SE writes `13 83 00 00 4f`, not `13 4f`). The certificate
        is spliced in COMPLETELY UNTOUCHED -- it still carries the `30 82 ...`
        its issuer produced. Re-encoding it, which an earlier version of this
        did, changes bytes the client may well be hashing.
        """
        lab = label.encode("latin-1")
        body = (bytes([0x13, 0x83]) + len(lab).to_bytes(3, "big") + lab
                + cert_der)
        return bytes([0x30, 0x83]) + len(body).to_bytes(3, "big") + body

    def build(self, certs):
        payload = b"".join(certs)
        body = bytes([0x30, 0x83]) + len(payload).to_bytes(3, "big") + payload
        return bytes(self.header) + body


def _openssl_text(der, *args):
    """Pretty-print a certificate, if openssl happens to be on PATH.

    OPTIONAL BY DESIGN. Reading and rebuilding the store is pure Python; only
    the human-readable subject/date lines shell out. openssl is on PATH under
    Git Bash and generally is not under native PowerShell, and the installer
    calls this to sanity-check a store before writing it into Program Files --
    so a missing openssl must degrade to a quieter listing, not a traceback.
    """
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "c.der")
        open(f, "wb").write(der)
        try:
            r = subprocess.run(["openssl", "x509", "-inform", "DER", "-in", f,
                                "-noout", *args], capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            return "(openssl not on PATH -- details unavailable)"
        return r.stdout.strip() or r.stderr.strip()


def cmd_list(a):
    db = CertDB(open(a.path, "rb").read())
    cs = db.certs()
    print(f"{a.path}: {len(cs)} certificate(s), payload {len(db.payload)} bytes")
    for i, se in enumerate(cs):
        label, der = CertDB.split_entry(se)
        print(f"  [{i}] {len(se):5d} bytes   {label[:110]}")
        info = _openssl_text(der, "-subject", "-dates")
        for line in info.splitlines():
            print("        ", line.strip()[:130])
    return 0


def cmd_dump(a):
    db = CertDB(open(a.path, "rb").read())
    os.makedirs(a.out, exist_ok=True)
    for i, se in enumerate(db.certs()):
        _, der = CertDB.split_entry(se)
        open(os.path.join(a.out, f"ca{i}.der"), "wb").write(der)
    print(f"wrote {len(db.certs())} cert(s) to {a.out}")
    return 0


def cmd_check(a):
    raw = open(a.path, "rb").read()
    db = CertDB(raw)
    rebuilt = db.build(db.certs())
    ok = rebuilt == raw
    print(f"round-trip rebuild: {'IDENTICAL' if ok else 'DIFFERS'} "
          f"({len(rebuilt)} vs {len(raw)} bytes)")
    if not ok:
        return 1
    # and prove entry split/rebuild is lossless
    for i, se in enumerate(db.certs()):
        label, der = CertDB.split_entry(se)
        if CertDB.make_entry(label, der) != se:
            print(f"  entry[{i}] split/rebuild FAILED")
            return 1
    print(f"entry split/rebuild round-trips on all {len(db.certs())} entries")
    return 0


def cmd_add(a):
    db = CertDB(open(a.path, "rb").read())
    pem = open(a.cert, "rb").read()
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "n.der")
        r = subprocess.run(["openssl", "x509", "-in", a.cert, "-outform",
                            "DER", "-out", f], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"openssl could not read {a.cert}: {r.stderr.strip()}")
        der = open(f, "rb").read()
    existing = db.certs()
    label = a.label or _openssl_text(der, "-subject").split("=", 1)[-1].strip()
    new = CertDB.make_entry(label, der)
    if new in existing:
        print("that certificate is already in the store; nothing to do")
        return 0
    out = db.build(existing + [new])
    open(a.out, "wb").write(out)
    print(f"{a.path}: {len(existing)} -> {len(existing) + 1} certs")
    print(f"  added {len(der)} byte DER as {len(new)} bytes in SE form")
    print(f"  wrote {a.out} ({len(out)} bytes)")
    # re-parse what we wrote, as a self-check
    back = CertDB(open(a.out, "rb").read())
    assert len(back.certs()) == len(existing) + 1
    print("  re-parsed OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("path"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("check"); p.add_argument("path"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("dump"); p.add_argument("path")
    p.add_argument("-o", "--out", required=True); p.set_defaults(fn=cmd_dump)
    p = sub.add_parser("add"); p.add_argument("path"); p.add_argument("cert")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--label", help="display label; defaults to the subject")
    p.set_defaults(fn=cmd_add)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
