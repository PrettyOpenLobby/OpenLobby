#!/usr/bin/env python3
"""Pull a whole bundle back OUT of a running polserver2 and check it.

This drives the server the way a client does and proves everything that
matters to one:

  1. cmd 7 answers, and reports the version the bundle claims.
  2. cmd 1 returns a patch list that decompresses to the bundle's patch.cfg.
  3. every row in that list can be fetched with ranged cmd 3 reads over one
     persistent connection (the way the real client does it, not one blob per
     socket), and each blob decompresses to bytes matching the size, signed
     byte-sum and MD5 recorded in the list.
  4. with --reference, those bytes are compared against the installed tree the
     bundle was built from, so a file that was mangled on the way in is caught
     rather than being consistently wrong at both ends.

Point (3) alone would pass on a self-consistent mistake: the checksums live in
the same patch.cfg polsynth.py generated. (4) is what makes it a real test, so
use it.

Run the server without --pin, --list-cap, --slim-list or POLP_CONSOLE_LIST_CAP
for the bundle under test: those change what cmd 8 and cmd 1 return on purpose,
and this check expects the bundle exactly as it is on disk.

    python services/polserver2.py mirrors --advertise 127.0.0.1 --bind-offset 1000 --quiet &
    python tools/patch/polselfcheck.py --bundle mirrors/W2U-0002 --port 54002 \\
        --reference "D:/Games/PlayOnline/SquareEnix/TetraMaster"
"""
import argparse
import hashlib
import json
import os
import socket
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "services"))  # slc, polbundle
import slc                                                       # noqa: E402
from polbundle import manifest, signed_bytesum                   # noqa: E402

CHUNK = 0x10000            # what the real client asks for per cmd 3


def cksum(buf):
    return int.from_bytes(hashlib.md5(bytes(buf)[8:]).digest()[:4], "little")


def frame(cmd, payload, total_len=None):
    b = bytearray(16)
    b[8:12] = b"POLP"
    struct.pack_into("<I", b, 12, cmd)
    b += payload
    struct.pack_into("<I", b, 0, total_len if total_len is not None else len(b))
    struct.pack_into("<I", b, 4, cksum(b))
    return bytes(b)


class Client:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=30)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def rpc(self, pkt):
        self.sock.sendall(pkt)
        buf = b""
        while len(buf) < 16 or len(buf) < struct.unpack_from("<I", buf, 0)[0]:
            c = self.sock.recv(65536)
            if not c:
                raise IOError("server closed the connection")
            buf += c
        total = struct.unpack_from("<I", buf, 0)[0]
        pkt = buf[:total]
        if struct.unpack_from("<I", pkt, 4)[0] != cksum(pkt):
            raise IOError("reply checksum is wrong")
        return struct.unpack_from("<I", pkt, 12)[0], pkt

    def cmd7(self, region, product, version):
        body = bytearray(0x48)
        body[0x00:0x04] = region.encode()
        body[0x04:0x08] = product.encode()
        body[0x08:0x08 + len(version)] = version.encode()
        return self.rpc(frame(7, bytes(body)))

    def cmd1(self, region, product):
        body = bytearray(8)
        body[0:4] = region.encode()
        body[4:8] = product.encode()
        # The real client sends total_len 24 for cmd 1: the 16-byte header,
        # then the region/product pair.
        return self.rpc(frame(1, bytes(body), total_len=24))

    def cmd3(self, region, product, path, offset, length):
        # region and product are FIXED 4-byte fields, NUL-padded. "W2U" is
        # three characters, and packing it unpadded slides product a byte left
        # so the server looks the bundle up under a name that cannot exist.
        body = struct.pack("<II", offset, length)
        body += region.encode().ljust(4, b"\x00") + product.encode().ljust(4, b"\x00")
        body += struct.pack("<I", len(path) + 1) + path.encode() + b"\x00"
        return self.rpc(frame(3, body))

    def fetch(self, region, product, path, expect):
        """Ranged read of a whole blob, as the client does it."""
        out = bytearray()
        while len(out) < expect:
            cmd, pkt = self.cmd3(region, product, path, len(out),
                                 min(CHUNK, expect - len(out)))
            if cmd != 4:
                raise IOError(f"{path}: cmd {cmd} at offset {len(out)}")
            plen = struct.unpack_from("<I", pkt, 0x18)[0]
            data = pkt[0x1c + plen:]
            if not data:
                raise IOError(f"{path}: empty frame at offset {len(out)}")
            out += data
        return bytes(out)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True,
                    help="where the server under test is listening (add your "
                         "--bind-offset to the service port)")
    ap.add_argument("--reference", help="installed tree to compare the served "
                                        "bytes against; use it, see above")
    ap.add_argument("--claim", default="20030909_0",
                    help="version the test client claims in cmd 7")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    a = ap.parse_args()

    meta = json.load(open(os.path.join(a.bundle, "meta.json")))
    region, product = meta["region"], meta["product"]
    rows = manifest(a.bundle)

    c = Client(a.host, a.port)
    cmd, pkt = c.cmd7(region, product, a.claim)
    if cmd != 8:
        raise SystemExit(f"[FAIL] cmd7 -> cmd {cmd} (expected 8)")
    status = pkt[0x18:pkt.find(b"\x00", 0x18)].decode()
    blen = struct.unpack_from("<I", pkt, 0x58)[0]
    latest = pkt[0x5c:0x5c + blen].rstrip(b"\x00").decode()
    print(f"[*] cmd7 {region}/{product} claim={a.claim} -> {status}, latest={latest}")
    if latest != meta["latest_version"]:
        raise SystemExit(f"[FAIL] cmd8 latest {latest!r} != meta "
                         f"{meta['latest_version']!r} (a --pin or --list-cap "
                         f"changes it, and the server reads latest_version only "
                         f"at startup: restart it after a publish)")

    cmd, pkt = c.cmd1(region, product)
    if cmd != 2:
        raise SystemExit(f"[FAIL] cmd1 -> cmd {cmd} (expected 2)")
    served_cfg = slc.slc_decompress(pkt[16:])
    on_disk = open(os.path.join(a.bundle, "patch.cfg"), "rb").read()
    if served_cfg != on_disk:
        raise SystemExit("[FAIL] the served patch list does not decompress to "
                         "patch.cfg. A --list-cap, --slim-list or "
                         "POLP_CONSOLE_LIST_CAP on the server changes the list "
                         "on purpose; run it without them for this check")
    print(f"[*] cmd1 -> patch list, {len(served_cfg)} B, matches patch.cfg exactly")

    todo = rows[:a.limit] if a.limit else rows
    # --reference describes the tree the CURRENT build should reproduce, so only
    # each file's newest row is compared against it. Older rows are real history
    # and are supposed to differ; checking them against today's files would
    # report every past version as a defect.
    newest = {}
    for r in rows:
        cur = newest.get(r["path"])
        if cur is None or r["version"] > cur["version"]:
            newest[r["path"]] = r
    bad = []
    served_bytes = 0
    compared = 0
    for i, r in enumerate(todo, 1):
        blob = c.fetch(region, product, r["slc"], r["slc_size"])
        served_bytes += len(blob)
        if len(blob) != r["slc_size"]:
            bad.append((r["path"], f"blob {len(blob)} B, list says {r['slc_size']}"))
            continue
        raw = slc.slc_decompress(blob)
        if len(raw) != r["size"]:
            bad.append((r["path"], f"decompressed {len(raw)} B, list says {r['size']}"))
            continue
        if signed_bytesum(raw) != r["bytesum"]:
            bad.append((r["path"], "signed byte-sum disagrees with the list"))
            continue
        want = r["md5_4"] & 0xFFFFFFFF
        got = struct.unpack("<I", hashlib.md5(raw).digest()[:4])[0]
        if got != want:
            bad.append((r["path"], "MD5 disagrees with the list"))
            continue
        if a.reference and newest.get(r["path"]) is r:
            compared += 1
            ref = os.path.join(a.reference, r["path"].replace("/", os.sep))
            if not os.path.isfile(ref):
                bad.append((r["path"], "absent from the reference tree"))
            elif open(ref, "rb").read() != raw:
                bad.append((r["path"], "differs from the reference tree"))
        if i % 500 == 0:
            print(f"    {i}/{len(todo)} files, {served_bytes/1e6:.0f} MB")
    c.close()

    print(f"[*] {len(todo)} file(s) fetched over one connection, "
          f"{served_bytes/1e6:.1f} MB of blobs")
    if a.reference:
        print(f"[*] {compared} current-build file(s) compared byte-for-byte "
              f"against {a.reference}")
    else:
        print("[!] no --reference: this only proves the bundle is self-consistent")
    if bad:
        print(f"[FAIL] {len(bad)} problem(s):")
        for path, why in bad[:20]:
            print(f"    {path}: {why}")
        return 1
    print("[PASS] 0 problem(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
