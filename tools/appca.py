#!/usr/bin/env python3
"""Read and patch the CA bundle compiled into the Viewer's app.dll.

    viewer/com/app.dll

THIS, not usr/all/url/cert.db, is what the PC Viewer trusts. cert.db holds four
1990s roots and is apparently a leftover the PC build no longer consults --
adding our CA to it changed nothing. app.dll carries a full commercial bundle of
27 roots (VeriSign, Thawte, Entrust, GeoTrust, Equifax, Comodo, AddTrust,
USERTrust, GTE, ValiCert, SECOM, RSA Security, and SQUARE ENIX Root CA).

    python tools/appca.py list    <app.dll>
    python tools/appca.py dump    <app.dll> -o DIR
    python tools/appca.py replace <app.dll> --index N --cert ca.pem -o app.dll.new

## Layout

A wrapper `30 82 <len16>` followed by that many bytes of entries, back to back,
each in the SAME labelled form cert.db uses:

    SEQUENCE { PrintableString label, Certificate }

with 2-byte lengths here where cert.db pads to 3. In the 1.18.15e US build the
wrapper sits at 0x18f11c and spans 27816 bytes. Nothing before it counts or
sizes the entries -- the preceding bytes are a SHA-1 AlgorithmIdentifier and a
base64 alphabet -- and the wrapper's own length is the only size field.

## Why replacement is same-size

Growing the bundle would mean moving everything after it in a mapped PE section
and fixing up the wrapper length. Replacing an entry with one of EXACTLY the
same byte count changes no length field anywhere, leaves every other entry at
its original offset, and keeps the file size identical. The label is a free
padding knob -- it is a display string, so trailing spaces are harmless -- which
makes an exact fit always reachable as long as the new certificate is smaller
than the entry being replaced.

Pick a victim that is already useless. Expired roots in this bundle:

    RSA Data Security Secure Server CA   expired 2010   655 bytes  <- smallest
    Equifax Secure eBusiness CA-1        expired 2020   721 bytes
    AddTrust External CA Root            expired 2020  1181 bytes
    SQUARE ENIX Root CA                  expired 2013  1474 bytes

## Safety

`replace` refuses unless the rebuilt file is byte-identical in length, the
wrapper length is unchanged, the entry walk still lands exactly on the end of
the bundle, and the entry count still matches. `verify` re-checks a patched file
from scratch. The original app.dll is 4,335,104 bytes; keep a copy.
"""
import argparse
import os
import subprocess
import sys
import tempfile

#: `30 82` + 2-byte length, then an entry that itself starts `30 82 .. 13 82`.
WRAPPER_MIN = 4096


def _u16(b, i):
    return int.from_bytes(b[i:i + 2], "big")


def find_bundle(data):
    """-> (wrapper_offset, entries_offset, total_len). Scans; no hard offsets."""
    i = 0
    while True:
        i = data.find(b"\x30\x82", i)
        if i < 0:
            raise SystemExit("no CA bundle found in this file")
        ln = _u16(data, i + 2)
        body = i + 4
        # A bundle is big, and its first entry is a labelled one.
        if ln >= WRAPPER_MIN and data[body:body + 2] == b"\x30\x82" \
                and data[body + 4:body + 6] == b"\x13\x82":
            # walk it: entries must tile the wrapper exactly
            off, n = body, 0
            while off < body + ln:
                elen = _u16(data, off + 2) + 4
                if elen < 8:
                    break
                off += elen
                n += 1
            if off == body + ln and n > 1:
                return i, body, ln
        i += 2


def entries(data, body, total):
    out, off = [], body
    while off < body + total:
        elen = _u16(data, off + 2) + 4
        out.append((off, elen))
        off += elen
    return out


def split_entry(data, off, elen):
    """-> (label, certificate DER)."""
    assert data[off:off + 2] == b"\x30\x82"
    p = off + 4
    assert data[p:p + 2] == b"\x13\x82", "entry does not start with a label"
    llen = _u16(data, p + 2)
    label = data[p + 4:p + 4 + llen].decode("latin-1")
    cert = data[p + 4 + llen:off + elen]
    return label, cert


def make_entry(label, cert):
    lab = label.encode("latin-1")
    body = b"\x13\x82" + len(lab).to_bytes(2, "big") + lab + cert
    return b"\x30\x82" + len(body).to_bytes(2, "big") + body


def _openssl(der, *args):
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "c.der")
        open(f, "wb").write(der)
        try:
            r = subprocess.run(["openssl", "x509", "-inform", "DER", "-in", f,
                                "-noout", *args], capture_output=True, text=True)
        except OSError:
            return "(openssl not on PATH)"
        return (r.stdout or r.stderr).strip()


def cmd_list(a):
    d = open(a.path, "rb").read()
    w, body, total = find_bundle(d)
    es = entries(d, body, total)
    print(f"{a.path}")
    print(f"  bundle wrapper @0x{w:x}, entries @0x{body:x}, {total} bytes, "
          f"{len(es)} roots\n")
    for i, (off, elen) in enumerate(es):
        label, cert = split_entry(d, off, elen)
        dates = _openssl(cert, "-dates").replace("\n", "  ")
        print(f"  [{i:2d}] @0x{off:06x} {elen:5d}B cert={len(cert):4d}B  "
              f"{label[:72]}")
        print(f"       {dates}")
    return 0


def cmd_dump(a):
    d = open(a.path, "rb").read()
    w, body, total = find_bundle(d)
    os.makedirs(a.out, exist_ok=True)
    for i, (off, elen) in enumerate(entries(d, body, total)):
        _, cert = split_entry(d, off, elen)
        open(os.path.join(a.out, f"root{i:02d}.der"), "wb").write(cert)
    print(f"wrote {len(entries(d, body, total))} certs to {a.out}")
    return 0


def cmd_replace(a):
    d = bytearray(open(a.path, "rb").read())
    w, body, total = find_bundle(d)
    es = entries(d, body, total)
    if not (0 <= a.index < len(es)):
        sys.exit(f"--index must be 0..{len(es) - 1}")
    off, elen = es[a.index]
    old_label, old_cert = split_entry(d, off, elen)
    print(f"replacing [{a.index}] @0x{off:x} ({elen}B): {old_label[:70]}")

    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "n.der")
        r = subprocess.run(["openssl", "x509", "-in", a.cert, "-outform",
                            "DER", "-out", f], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"openssl could not read {a.cert}: {r.stderr.strip()}")
        cert = open(f, "rb").read()

    # entry = 4 (seq hdr) + 4 (label hdr) + label + cert, and must match exactly
    pad = elen - 8 - len(cert)
    if pad < 1:
        sys.exit(f"new certificate is {len(cert)}B; entry {a.index} only has "
                 f"{elen - 9}B of room. Pick a larger entry or a smaller key.")
    label = (a.label or 'C="US", O="PlayOnline Revival", '
                        'OU="PlayOnline Revival Root CA"')
    if len(label) > pad:
        sys.exit(f"label is {len(label)} chars but only {pad} fit; shorten it")
    label = label + " " * (pad - len(label))      # pad to an exact byte match
    new = make_entry(label, cert)
    assert len(new) == elen, (len(new), elen)

    d[off:off + elen] = new
    out = bytes(d)

    # --- verification, all of it, before anything is written ----------------
    orig = open(a.path, "rb").read()
    assert len(out) == len(orig), "file size changed"
    w2, body2, total2 = find_bundle(out)
    assert (w2, body2, total2) == (w, body, total), "bundle moved or resized"
    es2 = entries(out, body2, total2)
    assert len(es2) == len(es), "entry count changed"
    assert [e[1] for e in es2] == [e[1] for e in es], "an entry changed size"
    lab2, cert2 = split_entry(out, off, elen)
    assert cert2 == cert, "certificate did not round-trip"
    open(a.out, "wb").write(out)
    print(f"  new cert {len(cert)}B, label padded to {pad} chars")
    print(f"  wrote {a.out} ({len(out)} bytes, unchanged)")
    print(f"  verified: wrapper, entry count, and every entry size identical")
    return 0


def cmd_verify(a):
    d = open(a.path, "rb").read()
    w, body, total = find_bundle(d)
    es = entries(d, body, total)
    ours = [i for i, (o, l) in enumerate(es)
            if "PlayOnline Revival" in split_entry(d, o, l)[0]]
    print(f"bundle OK: {len(es)} roots, {total} bytes, wrapper @0x{w:x}")
    print(f"our root present: {'yes, index ' + str(ours) if ours else 'NO'}")
    return 0 if ours else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("path"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("verify"); p.add_argument("path"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("dump"); p.add_argument("path")
    p.add_argument("-o", "--out", required=True); p.set_defaults(fn=cmd_dump)
    p = sub.add_parser("replace"); p.add_argument("path")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--cert", required=True)
    p.add_argument("--label")
    p.add_argument("-o", "--out", required=True); p.set_defaults(fn=cmd_replace)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
