"""Read a patch bundle's index: the rows of its patch.cfg.

A bundle (see docs/patching.md) keeps its file index twice. `patch.cfg` is the
plaintext list the server hands out in cmd 2, and `manifest.json` is the same
rows parsed into JSON. The JSON is purely derived, so it may be absent: for a
large title it runs to tens of megabytes and is easy to regenerate. Everything
that consumes a bundle goes through `manifest()` here, which reads the cached
file when it exists and otherwise parses patch.cfg, so a bundle stays usable
from its small text files alone.

    python polbundle.py mirrors/W2U-0002    # (re)write manifest.json
    python polbundle.py --selftest
"""
import array
import json
import os
import sys

try:
    import numpy as _np
except Exception:
    _np = None


def signed_bytesum(raw):
    """patch.cfg column 3: the file's bytes summed as SIGNED int8.

    The obvious `sum(b - 256 if b > 127 else b for b in raw)` is a Python-level
    loop over every byte, which costs minutes of CPU per GB. numpy does it far
    faster when it is installed; array('b') is the fallback.
    """
    if _np is not None:
        return int(_np.frombuffer(raw, dtype=_np.int8).sum(dtype=_np.int64))
    a = array.array("b")
    a.frombytes(raw)
    return sum(a)


def parse_cfg(cfg_bytes):
    """Yield EVERY row of patch.cfg, not just the newest per file.

    A row is:
      <ver> <size> <signed-byte-sum> <md5[:4]> <ver>/Direct/<p>.slc <n> [<ver>/Indirect/<p>.olc <n>]
    Paths may contain spaces ("FINAL FANTASY XI Config.exe"), so anchor on the
    token ending in .slc / .olc rather than on a fixed column index.
    """
    cur = None
    for line in cfg_bytes.decode("latin-1").splitlines():
        if line.startswith("file ") and line.endswith(" {"):
            cur = line[5:-2]
        elif line.strip() == "}":
            cur = None
        elif cur and line.strip():
            p = line.split()
            if len(p) < 6:
                continue
            try:
                # Column 3 is the sum of the file's bytes read as SIGNED int8,
                # and column 4 is the first 4 bytes of the file's MD5 read
                # little-endian, the same construction POLP uses for its packet
                # checksum. Column 4 is written signed; it is kept unsigned here.
                ver, size, bsum, csum = p[0], int(p[1]), int(p[2]), int(p[3]) & 0xffffffff
            except ValueError:
                continue
            rest = p[4:]
            si = next((i for i, t in enumerate(rest) if t.endswith(".slc")), None)
            if si is None or si + 1 >= len(rest):
                continue
            slc_path = " ".join(rest[:si + 1])
            try:
                slc_size = int(rest[si + 1])
            except ValueError:
                continue
            olc_path, olc_size = None, 0
            tail = rest[si + 2:]
            oi = next((i for i, t in enumerate(tail) if t.endswith(".olc")), None)
            if oi is not None and oi + 1 < len(tail):
                try:
                    olc_size = int(tail[oi + 1])
                    olc_path = " ".join(tail[:oi + 1])
                except ValueError:
                    olc_path, olc_size = None, 0
            yield {"path": cur, "version": ver, "size": size,
                   "bytesum": bsum, "md5_4": csum,
                   "slc": slc_path, "slc_size": slc_size,
                   "olc": olc_path, "olc_size": olc_size}


def manifest(bundle):
    """Rows for a bundle: cached manifest.json, else parsed from patch.cfg."""
    mp = os.path.join(bundle, "manifest.json")
    if os.path.exists(mp):
        with open(mp) as f:
            return json.load(f)
    cp = os.path.join(bundle, "patch.cfg")
    if not os.path.exists(cp):
        raise FileNotFoundError(
            f"{bundle} has neither manifest.json nor patch.cfg")
    with open(cp, "rb") as f:
        return list(parse_cfg(f.read()))


def write_manifest(bundle):
    """Materialise manifest.json from patch.cfg (optional local cache)."""
    rows = manifest(bundle)
    with open(os.path.join(bundle, "manifest.json"), "w") as f:
        json.dump(rows, f, indent=1)
    return len(rows)


def selftest():
    """Parse a small hand-written patch.cfg and check every field."""
    cfg = (b"file data/a.bin {\n"
           b"20030429_0 10 -3 -12345 20030429_0/Direct/data/a.bin.slc 11\n"
           b"20040714_0 12 5 777 20040714_0/Direct/data/a.bin.slc 9"
           b" 20040714_0/Indirect/data/a.bin.olc 4\n"
           b"}\n"
           b"file Example Config.exe {\n"
           b"20030429_0 3 1 2 20030429_0/Direct/Example Config.exe.slc 4\n"
           b"}\n"
           b"\nend\n")
    rows = list(parse_cfg(cfg))
    assert len(rows) == 3, rows
    a0, a1, spaced = rows
    assert a0["path"] == "data/a.bin" and a0["version"] == "20030429_0"
    assert a0["size"] == 10 and a0["bytesum"] == -3
    assert a0["md5_4"] == (-12345) & 0xffffffff
    assert a0["olc"] is None and a0["olc_size"] == 0
    assert a1["olc"] == "20040714_0/Indirect/data/a.bin.olc" and a1["olc_size"] == 4
    assert spaced["path"] == "Example Config.exe"
    assert spaced["slc"] == "20030429_0/Direct/Example Config.exe.slc"
    assert spaced["slc_size"] == 4
    assert signed_bytesum(b"\x01\x02\xff\x80") == 1 + 2 - 1 - 128
    print("polbundle selftest OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(selftest())
    if not sys.argv[1:]:
        raise SystemExit(__doc__)
    for b in sys.argv[1:]:
        print(f"  {b}: {write_manifest(b)} rows")
