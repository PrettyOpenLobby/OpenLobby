#!/usr/bin/env python3
"""Build a POLP patch bundle from a title you have installed.

A patch tree holds nothing that cannot be computed from the files themselves:
`patch.cfg` is plaintext, both of its checksum columns are derived from the
file bytes, and a `.slc` blob is the file in a one-byte container. So given the
title as it sits on disk after installation, this writes a bundle that
`services/polserver2.py` serves to clients.

What a bundle built this way carries:

  * Exactly one version, the state of the tree it was given. Every client is
    brought to that state in one step, with Direct blobs only. There are no
    Indirect (`.olc`) deltas because there is no previous version to delta
    against. `polpublish.py` adds later versions on top.
  * A cmd-8 publication timestamp of your choosing (`--token`). The client
    does not check it.

Source of the file list, in order of preference:

  * the tree's own `file.txt`, the title's inventory of what it consists of,
    `<hash>:<size>:<relpath>` per line. Prefer it: it excludes user-written
    config and includes files the patch system owns.
  * `--walk`, which takes every file under the source directory. Use it only
    when there is no file.txt, and check `--exclude`.

Either way `patch.ver` is left out (see DEFAULT_EXCLUDE). A bundle built from
an install cannot install a title from nothing: `file.txt` covers the data
files, not the executables and launchers the installer puts down.

The hash column is `base64(MD5(file))` under a substituted alphabet (POL_B64
below), so `--verify-list` can check an installed tree against its own
inventory before you trust it as a patch source.

    python tools/patch/polsynth.py --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" \\
                                   --verify-list      # audit only, writes nothing
    python tools/patch/polsynth.py --source "D:/Games/PlayOnline/SquareEnix/TetraMaster" \\
                                   --out mirrors/W2U-0002 --region W2U --product 0002
"""
import argparse
import base64
import hashlib
import json
import os
import re
import struct
import sys
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))                    # patchver
sys.path.insert(0, os.path.join(_HERE, "..", "..", "services"))  # slc, polbundle
import slc                                                       # noqa: E402
from polbundle import signed_bytesum                             # noqa: E402
import patchver                                                  # noqa: E402

STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
# PlayOnline's substituted base64 alphabet, used for the hash column in
# file.txt / patch.txt. The same 64 symbols appear as a table in the Viewer's
# own code.
POL_B64 = "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX"
_TO_POL = str.maketrans(STD_B64, POL_B64)

# The cmd-8 status string compares the client's claimed version against the
# OLDEST build the tree carries. A tree built here can patch a client from any
# starting point, since it ships every file whole, so the floor is zero and
# every well-formed version gets "registered".
SYNTH_OLDEST = "00000000_0"

# `patch.ver` is CLIENT state, not distributed content, so it is kept out of
# the tree even when the source's file.txt lists it (file.txt is a local
# inventory and covers everything on disk). Published trees never carry a
# `file patch.ver {` block, and clients rewrite their own patch.ver to the
# version they were served. Shipping it would also push a blob encrypted under
# THIS machine's install nonce to every other client, where it would not
# decrypt at all.
DEFAULT_EXCLUDE = ("patch.ver",)


def pol_hash(raw):
    """The file.txt / patch.txt hash column for `raw`: 22 characters."""
    return base64.b64encode(hashlib.md5(raw).digest()).decode().rstrip("=").translate(_TO_POL)


def md5_4(raw, signed=True):
    """patch.cfg column 4: u32_LE(MD5(file)[:4]), written signed in the file."""
    v = struct.unpack("<I", hashlib.md5(raw).digest()[:4])[0]
    return v - 2**32 if (signed and v >= 2**31) else v


def slc_encode(raw):
    """Smallest valid container: 1 = stored, 3 = zlib.

    Published trees use stored for already-packed assets (PNG, BGW) and zlib
    for the rest; choosing whichever is smaller gives the same split without a
    file-type table. Method 2 (bit-LZSS) is only ever used for the patch list.
    """
    return min([b"\x01" + raw, b"\x03" + zlib.compress(raw, 9)], key=len)


def patchlist_encode(payload):
    """`payload` -> an .slc method-2 container, the form cmd 2 carries.

    The encoder itself is `slc.slc_compress`, beside the decompressor in
    services/, which is also the one module the patch server's container
    carries. Older lists may have been written all literals, so compare two
    lists DECOMPRESSED.
    """
    return slc.slc_compress(payload)


def read_list(source, name="file.txt"):
    """Parse an installed tree's file.txt -> [(relpath, recorded_size, hash)]."""
    path = os.path.join(source, name)
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line or line == "::":
                continue
            h, _, rest = line.partition(":")
            size, _, rel = rest.partition(":")
            if not rel:
                continue
            rows.append((rel, int(size) if size.isdigit() else -1, h))
    return rows


def walk_files(source, excludes):
    out = []
    for dirpath, dirnames, names in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if d not in excludes)
        for nm in sorted(names):
            rel = os.path.relpath(os.path.join(dirpath, nm), source).replace(os.sep, "/")
            if nm in excludes or any(rel.startswith(e + "/") for e in excludes):
                continue
            out.append((rel, -1, None))
    return out


def installed_version(source, content_id):
    """The version the installed tree actually claims, out of its `patch.ver`.

    Getting this right matters more than it looks: label the tree with a
    version the client is not really at and the server either offers an update
    that is already installed or reports the client current when it is not.
    Older installs keep it as 10 bytes of plaintext; later ones as a 288-byte
    blob encrypted under an install-time registry nonce, and
    `patchver.recover_key` gets it back from the file alone. That matters
    because the registry keeps only the last nonce written per content id, so
    several regional installs of one title overwrite each other.
    """
    path = os.path.join(source, "patch.ver")
    if not os.path.isfile(path):
        return None, "no patch.ver"
    blob = open(path, "rb").read()
    if len(blob) < patchver.CIPHER_LEN:
        return blob.decode("latin-1").strip("\x00").strip(), "plaintext patch.ver"
    for seed, _key64, kb in patchver.recover_key(blob, content_id):
        plain = patchver.decrypt(bytes(blob[:patchver.CIPHER_LEN]), seed)
        text = plain[patchver.TEXT_OFF:].split(b"\x00")[0]
        if plain.count(0) < 230 or not text or not all(0x20 <= b < 0x7f for b in text):
            continue
        key = kb.decode("latin-1") if all(32 <= b < 127 for b in kb) else "seed %#x" % seed
        return text.decode("ascii"), "patch.ver, key %s" % key
    return None, "patch.ver did not decode"


def verify_list(source):
    """Audit an installed tree against its own file.txt before trusting it."""
    rows = read_list(source)
    if rows is None:
        print(f"[!] {source} has no file.txt - nothing to verify against")
        return 1
    ok = bad = absent = stale = 0
    for rel, size, want in rows:
        full = os.path.join(source, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            absent += 1
            print(f"    ABSENT  {rel}")
            continue
        raw = open(full, "rb").read()
        if len(raw) != size:
            # Exactly ONE size drift is legitimate: the inventory records
            # patch.ver at its original 10-byte plaintext length, and later
            # clients rewrite it as a 288-byte encrypted blob. Every other
            # drift is a damaged file and must count as one. Skipping the hash
            # check on any size drift would let a file whose text was
            # re-encoded on copy (every high byte turned into EF BF BD) pass
            # this audit and be baked into the bundle as the authoritative copy.
            if rel == "patch.ver":
                stale += 1
                continue
            bad += 1
            print(f"    SIZE    {rel}  want {size}, got {len(raw)}")
            continue
        if pol_hash(raw) == want:
            ok += 1
        else:
            bad += 1
            print(f"    HASH    {rel}")
    print(f"[*] {source}\n    {len(rows)} rows: {ok} verified, {bad} damaged, "
          f"{absent} absent, {stale} benign drift (patch.ver)")
    return 0 if (bad == 0 and absent == 0) else 1


def synth(args):
    source = os.path.abspath(args.source)
    out = os.path.abspath(args.out)
    excludes = set(args.exclude or [])
    if not args.keep_patchver:
        excludes |= set(DEFAULT_EXCLUDE)

    ver, ver_origin = args.version, "given on the command line"
    if ver == "auto":
        ver, ver_origin = installed_version(source, int(args.product))
        if not ver:
            raise SystemExit(
                f"--version auto: {ver_origin}. Read it another way and pass it "
                f"explicitly - a wrong version makes the server lie to the client "
                f"about whether it is current.")
        print(f"[*] installed version {ver} ({ver_origin})")
        if not re.match(r"^[1-9]\d{7}_", ver):
            raise SystemExit(
                f"--version auto got {ver!r}, which is not a version the patch "
                f"protocol's status rule accepts ([1-9] + 7 digits + '_'). A "
                f"fresh install that has never been patched stores "
                f"'0000000000'. Pass --version explicitly and record where the "
                f"date came from.")

    if args.walk:
        rows = walk_files(source, excludes)
        origin = "--walk"
    else:
        rows = read_list(source)
        if rows is None:
            raise SystemExit(f"{source} has no file.txt; pass --walk to take every file")
        rows = [r for r in rows if r[0] not in excludes]
        origin = "file.txt"

    if os.path.exists(out) and os.listdir(out) and not args.force:
        raise SystemExit(f"{out} exists and is not empty - pass --force to rebuild")
    os.makedirs(os.path.join(out, "blobs"), exist_ok=True)

    blocks = []
    manifest = []
    total_raw = total_blob = 0
    stored = deflated = 0
    skipped = []

    for rel, _size, _hash in rows:
        full = os.path.join(source, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            skipped.append(rel)
            continue
        raw = open(full, "rb").read()
        blob = slc_encode(raw)
        # Never ship a blob we have not proved the client can unpack: a bad
        # container here would surface as a corrupt install hours later.
        if slc.slc_decompress(blob) != raw:
            raise SystemExit(f"{rel}: blob does not round-trip")
        blob_rel = "%s/Direct/%s.slc" % (ver, rel)
        blob_path = os.path.join(out, "blobs", *blob_rel.split("/"))
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        with open(blob_path, "wb") as fh:
            fh.write(blob)

        bsum = signed_bytesum(raw)
        blocks.append("file %s {\n%s %d %d %d %s %d\n}\n" % (
            rel, ver, len(raw), bsum, md5_4(raw), blob_rel, len(blob)))
        manifest.append({"path": rel, "version": ver, "size": len(raw),
                         "bytesum": bsum, "md5_4": md5_4(raw, signed=False),
                         "slc": blob_rel, "slc_size": len(blob),
                         "olc": None, "olc_size": 0})
        total_raw += len(raw)
        total_blob += len(blob)
        stored += blob[0] == 1
        deflated += blob[0] == 3
        if args.verbose:
            print(f"    {rel}  {len(raw)} -> {len(blob)} (method {blob[0]})")

    if not manifest:
        raise SystemExit("no files - nothing to build")

    cfg = "\n".join(blocks) + "\nend\n\n\n"
    payload = cfg.encode("latin-1")
    with open(os.path.join(out, "patch.cfg"), "wb") as fh:
        fh.write(payload)
    listraw = patchlist_encode(payload)
    if slc.slc_decompress(listraw) != payload:
        raise SystemExit("patch list does not round-trip")
    with open(os.path.join(out, "patchlist.raw"), "wb") as fh:
        fh.write(listraw)
    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)

    meta = {
        "region": args.region,
        "product": args.product,
        "port": args.port if args.port is not None else 53000 + int(args.product),
        "latest_version": ver,
        # Any well-formed version the client claims must come back "registered":
        # this tree ships every file whole, so it can patch from anywhere.
        "oldest_version": SYNTH_OLDEST,
        "file_count": len(manifest),
        "version_count": 1,
        "row_count": len(manifest),
        "blob_count": len(manifest),
        "blob_bytes": total_blob,
        "source_bytes": total_raw,
        "complete": True,
        "synthesised": {
            "source": source,
            "file_list": origin,
            "version_origin": ver_origin,
            "note": "built from an installed tree by polsynth.py",
            "skipped_absent": skipped,
        },
        "cmd8_token": args.token,
        "cmd8_field14": 1,
        "cmd8_trailing": "0",
    }
    if args.alias:
        meta["region_aliases"] = list(args.alias)
    with open(os.path.join(out, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    print(f"[*] built {args.region}/{args.product} -> {out}")
    print(f"    {len(manifest)} files, {total_raw/1e6:.1f} MB -> "
          f"{total_blob/1e6:.1f} MB of blobs ({stored} stored, {deflated} zlib)")
    print(f"    version {ver} on port {meta['port']}"
          + (f", also answering region(s) {', '.join(args.alias)}" if args.alias else ""))
    if skipped:
        print(f"    [!] {len(skipped)} file(s) in {origin} were absent from the "
              f"source and are NOT in the tree:")
        for rel in skipped[:10]:
            print(f"          {rel}")
        if len(skipped) > 10:
            print(f"          ... and {len(skipped)-10} more")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="installed title directory")
    ap.add_argument("--out", help="bundle directory to create (mirrors/<REGION>-<PRODUCT>)")
    ap.add_argument("--region", default="W2U")
    ap.add_argument("--product", default="0002", help="4-digit content id")
    ap.add_argument("--port", type=int, default=None,
                    help="default 53000 + content id")
    ap.add_argument("--version", default="auto",
                    help="the single version this tree publishes; 'auto' (the "
                         "default) reads it out of the source's own patch.ver")
    ap.add_argument("--alias", action="append",
                    help="extra region code this bundle also answers for; repeatable")
    ap.add_argument("--token", type=lambda s: int(s, 0), default=0x4e4e6497,
                    help="cmd-8 publication timestamp, a unix time; the client "
                         "does not check it")
    ap.add_argument("--walk", action="store_true",
                    help="take every file under --source instead of reading file.txt")
    ap.add_argument("--exclude", action="append",
                    help="relative path or directory name to leave out; repeatable")
    ap.add_argument("--keep-patchver", action="store_true",
                    help="ship patch.ver in the tree. Do not: published trees "
                         "never do, and the blob is bound to one install's nonce")
    ap.add_argument("--verify-list", action="store_true",
                    help="audit the source against its own file.txt and exit")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty --out")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verify_list:
        return verify_list(os.path.abspath(args.source))
    if not args.out:
        ap.error("--out is required unless --verify-list")
    return synth(args)


if __name__ == "__main__":
    raise SystemExit(main())
