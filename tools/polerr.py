#!/usr/bin/env python3
"""Decode the Viewer's error table (polerr.bin) -- code -> SE's INTERNAL description.

Why this matters more than it looks: the English string the Viewer shows the user is
frequently unrelated to the actual fault, and this repo has burned real time on that.
POL-0515 renders as "mail address is invalid" but fires from a session precondition;
POL-0008 renders as "cannot reach the network" and fires with no socket ever opened;
POL-7192 renders as "Some functions are not working properly due to a version
discrepancy" and actually means 未実装の認証を行った -- "performed an unimplemented
authentication". The Japanese field is the DEVELOPER's description and is the one to
reason from.

Format (after decryption):
    +0x00  u32   ? (0x1229 in the Viewer build)
    +0x04  u32   record count (471)
    +0x08  8 bytes zero
    +0x10  records, 16 bytes each:
               u32 offset of the internal (Japanese) description, NUL-terminated cp932
               u32 offset of the user-facing title/body
               u32 code
               u32 code (repeated)

The cipher is the shared PlayOnline data-file cipher with the all-zero 8-byte key
("int0" in worker-out/polfilecrypt.py, whose key map already lists polerr.bin under
it); the trailer checksum validates, so a successful decode is self-proving.

Usage:
    python polerr.py 7192 5135 0008        look up specific codes
    python polerr.py --all                 dump the whole table
    python polerr.py --grep チャット        search descriptions
    python polerr.py --bin <path>          use a different polerr.bin
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "worker-out"))
import polfilecrypt as pfc                                      # noqa: E402

DEFAULT_BIN = (r"C:\Program Files (x86)\PlayOnline\SquareEnix"
               r"\PlayOnlineViewer\data\doc\polerr.bin")


def load(path=DEFAULT_BIN):
    """Return {code: (internal_description, user_text)}."""
    raw = open(path, "rb").read()
    plain, ok = pfc.decrypt(raw, pfc.KEYS["int0"][0])
    if not ok:
        # Not fatal -- report it, because a bad trailer means either a different
        # key for this build or a truncated file, and both are worth knowing.
        print(f"[!] {path}: trailer checksum FAILED; output is suspect",
              file=sys.stderr)

    def cstr(off):
        end = plain.find(b"\x00", off)
        return plain[off:end].decode("cp932", "replace")

    out, off = {}, 0x10
    while off + 16 <= len(plain):
        a, b, code, _dup = struct.unpack_from("<IIII", plain, off)
        if not (0 < a < len(plain) and 0 < b < len(plain)):
            break                                   # past the record array
        out[code] = (cstr(a), cstr(b))
        off += 16
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="*", help="error codes to look up")
    ap.add_argument("--all", action="store_true", help="dump every code")
    ap.add_argument("--grep", help="search the internal descriptions")
    ap.add_argument("--bin", default=DEFAULT_BIN, help="path to polerr.bin")
    a = ap.parse_args()

    tbl = load(a.bin)
    if a.all or a.grep:
        for code in sorted(tbl):
            desc, user = tbl[code]
            if a.grep and a.grep not in desc and a.grep not in user:
                continue
            print(f"POL-{code:04d}  {desc}")
        return
    if not a.codes:
        ap.error("give one or more codes, or --all / --grep")
    for c in a.codes:
        code = int(c.lstrip("POL-").lstrip("-"))
        if code not in tbl:
            print(f"POL-{code:04d}  (not in table)")
            continue
        desc, user = tbl[code]
        print(f"POL-{code:04d}  {desc}")
        print(f"          user-facing: {user}")


if __name__ == "__main__":
    main()
