"""Patch bundles for the deployer: the patch actions' checks, the bundles
the patch service serves, whether it serves them, their self-check results,
and the list files a roll back leaves behind.

The .polpatch toolkit itself (tools/patch/polpatch_lib.py) is loaded from
the core checkout, so the deployer always uses the toolkit of the OpenLobby
it manages. In this process it only reads: a .polpatch's directory and
polpatch.json (read_manifest, size-capped) and the bundles already in the
mirrors folder. Everything that unpacks a .polpatch or reads a client
install runs in a child process as an unprivileged user (patchtool.py).
docs/deployer.md, "Patch bundles", describes the rules. Standard library
only.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import sys
import threading

REGION = re.compile(r"[A-Z0-9]{3}")
PRODUCT = re.compile(r"[0-9]{4}")
VERSION = re.compile(r"[0-9]{8}_[0-9A-Za-z]")
LIST_FILES = ("meta.json", "patch.cfg", "patchlist.raw")
BACKUP_DIR = ".polpatch-backup"
PROBE_CLAIM = "20000101_0"     # the version the served probe claims; never a bundle's latest
PROBE_TIMEOUT = 2.0


class ToolkitMissing(Exception):
    pass


def ok_region(v):
    return isinstance(v, str) and REGION.fullmatch(v) is not None


def ok_product(v):
    return isinstance(v, str) and PRODUCT.fullmatch(v) is not None


def ok_version(v):
    return isinstance(v, str) and VERSION.fullmatch(v) is not None


def toolkit(core):
    """polpatch_lib from the core checkout's tools/patch."""
    d = os.path.join(core, "tools", "patch")
    if not os.path.isfile(os.path.join(d, "polpatch_lib.py")):
        raise ToolkitMissing("The OpenLobby checkout has no tools/patch/polpatch_lib.py; "
                             "update core, then try again.")
    if d not in sys.path:
        sys.path.insert(0, d)
    import polpatch_lib
    return polpatch_lib


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def manifest_fields(m):
    """What an answer may say about a .polpatch manifest."""
    return {k: m.get(k) for k in ("region", "product", "kind", "versions", "title")}


# --------------------------------------------------------------- browse

class ManifestCache:
    """read_manifest results for browse, keyed by path, size and mtime."""

    def __init__(self, cap=512):
        self.cap, self.lock, self.items = cap, threading.Lock(), {}

    def describe(self, lib, path):
        try:
            st = os.stat(path)
        except OSError as exc:
            return {"error": "The file cannot be read (%s)." % exc.strerror}
        key = (path, st.st_size, st.st_mtime_ns)
        with self.lock:
            hit = self.items.get(key)
        if hit is not None:
            return hit
        try:
            out = manifest_fields(lib.read_manifest(path))
        except lib.PolpatchError as exc:
            out = {"error": str(exc)}
        except Exception as exc:     # an unreadable file is a sentence, never a 500
            out = {"error": "The file is not a .polpatch the toolkit can read (%s)." % exc}
        with self.lock:
            if len(self.items) >= self.cap:
                self.items.clear()
            self.items[key] = out
        return out


# -------------------------------------------------------------- bundles

def bundle_dirs(mirrors):
    """[(name, path)] of what polserver2 loads: child folders with a patchlist.raw."""
    out = []
    try:
        names = sorted(os.listdir(mirrors))
    except OSError:
        return out
    for n in names:
        p = os.path.join(mirrors, n)
        if not n.startswith(".") and os.path.isdir(p) \
                and os.path.exists(os.path.join(p, "patchlist.raw")):
            out.append((n, p))
    return out


def tree_size(path):
    total = 0
    for dirpath, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


class BundleCache:
    """bundle_info, port and size per bundle folder, reread when one of its
    list files changes."""

    def __init__(self):
        self.lock, self.items = threading.Lock(), {}

    def key(self, path):
        k = [path]
        for n in LIST_FILES:
            try:
                st = os.stat(os.path.join(path, n))
                k += [st.st_size, st.st_mtime_ns]
            except OSError:
                k += [None, None]
        return tuple(k)

    def describe(self, lib, path):
        key = self.key(path)
        with self.lock:
            hit = self.items.get(path)
        if hit and hit[0] == key:
            return dict(hit[1])
        try:
            info = lib.bundle_info(path)
            with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            port = meta.get("port")
            if not isinstance(port, int):
                port = 54000 if info["product"] == "1000" else 53000 + int(info["product"])
            versions = list(info["versions"])
            out = {"region": info["region"], "product": info["product"],
                   "title": info["title"], "aliases": info["aliases"],
                   "versions": versions, "latest": versions[-1] if versions else None,
                   "meta_latest": info["latest"], "port": port,
                   "size": tree_size(path), "error": None}
        except Exception as exc:
            base = os.path.basename(path)
            out = {"region": base.split("-")[0], "product": base.partition("-")[2],
                   "title": base, "aliases": [], "versions": [], "latest": None,
                   "meta_latest": None, "port": None, "size": tree_size(path),
                   "error": "The bundle %s cannot be read: %s" % (base, exc)}
        with self.lock:
            self.items[path] = (key, out)
        return dict(out)


def _cksum(buf):
    return int.from_bytes(hashlib.md5(bytes(buf)[8:]).digest()[:4], "little")


def _frame(cmd, payload):
    b = bytearray(16)
    b[8:12] = b"POLP"
    struct.pack_into("<I", b, 12, cmd)
    b += payload
    struct.pack_into("<I", b, 0, len(b))
    struct.pack_into("<I", b, 4, _cksum(b))
    return bytes(b)


def probe(host, port, region, product, timeout=PROBE_TIMEOUT):
    """The latest version the patch service reports for region/product
    (its cmd 8 answer to a cmd 7), or None when it does not answer."""
    body = bytearray(0x48)
    body[0:4] = region.encode("ascii")
    body[4:8] = product.encode("ascii")
    body[8:8 + len(PROBE_CLAIM)] = PROBE_CLAIM.encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(_frame(7, bytes(body)))
            buf = b""
            while len(buf) < 16 or len(buf) < struct.unpack_from("<I", buf, 0)[0]:
                c = s.recv(65536)
                if not c:
                    return None
                buf += c
                if len(buf) > 1 << 16:
                    return None
    except (OSError, ValueError):
        return None
    total = struct.unpack_from("<I", buf, 0)[0]
    pkt = buf[:total]
    if total < 0x5c or struct.unpack_from("<I", pkt, 12)[0] != 8:
        return None
    blen = struct.unpack_from("<I", pkt, 0x58)[0]
    return pkt[0x5c:0x5c + blen].rstrip(b"\x00").decode("latin-1")


def served(host, offset, info):
    """Whether the running patch service has this bundle loaded: it answers
    cmd 7 with the bundle's own latest version (a service answering every
    tree as up to date would echo the probe's claim instead)."""
    if not info.get("port") or not info.get("meta_latest"):
        return False
    got = probe(host, info["port"] + offset, info["region"], info["product"])
    return got is not None and got == info["meta_latest"] and got != PROBE_CLAIM


# ----------------------------------------------------------- self-checks

class Checks:
    """Self-check results under STATE/patches/selfcheck.json, keyed by
    REGION-PRODUCT, each with the versions it was run against."""

    def __init__(self, state):
        self.dir = os.path.join(state, "patches")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "selfcheck.json")
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, region, product, versions):
        with self.lock:
            r = self._load().get("%s-%s" % (region, product))
        if not r or r.get("versions") != list(versions):
            return {"selfcheck": None, "selfcheck_at": None, "selfcheck_why": None}
        return {"selfcheck": r.get("selfcheck"), "selfcheck_at": r.get("selfcheck_at"),
                "selfcheck_why": r.get("selfcheck_why")}

    def put(self, region, product, versions, ok, why=None):
        with self.lock:
            d = self._load()
            d["%s-%s" % (region, product)] = {
                "selfcheck": "ok" if ok else "failed", "selfcheck_at": now_iso(),
                "selfcheck_why": None if ok else (why or "The self-check failed."),
                "versions": list(versions)}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)

    def drop(self, region, product):
        with self.lock:
            d = self._load()
            if d.pop("%s-%s" % (region, product), None) is not None:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f)
                os.replace(tmp, self.path)


# ------------------------------------------------------------- roll back

def strip_versions(cfg_text, drop):
    """patch.cfg without the rows of the versions in `drop`. A block left
    with no rows goes too, with the blank line that follows it, which undoes
    exactly what polpublish does when it adds a file."""
    out, block, rows, skip_blank = [], None, 0, False
    for line in cfg_text.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if block is None:
            if skip_blank and not s.strip():
                skip_blank = False
                continue
            skip_blank = False
            if s.startswith("file ") and s.endswith(" {"):
                block, rows = [line], 0
                continue
            out.append(line)
            continue
        if s.strip() == "}":
            block.append(line)
            if rows:
                out.extend(block)
            else:
                skip_blank = True
            block = None
            continue
        if s.strip() and s.split(" ", 1)[0] in drop:
            continue
        if s.strip():
            rows += 1
        block.append(line)
    if block:
        out.extend(block)
    return "".join(out)


def _row_key(r):
    return (r["path"], r["version"], r["size"], r["bytesum"], r["md5_4"],
            r["slc"], r["slc_size"], r["olc"], r["olc_size"])


def _rows(lib, cfg_bytes):
    return sorted(_row_key(r) for r in lib.polbundle.parse_cfg(cfg_bytes))


def find_backup(lib, mirrors, bundle, to, kept):
    """A list-file backup apply kept whose patch.cfg holds exactly the rows
    `kept` and whose latest_version is `to`, newest first; or None."""
    root = os.path.join(mirrors, BACKUP_DIR, os.path.basename(bundle))
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return None
    for n in names:
        d = os.path.join(root, n)
        try:
            with open(os.path.join(d, "patch.cfg"), "rb") as f:
                rows = _rows(lib, f.read())
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            if not os.path.isfile(os.path.join(d, "patchlist.raw")):
                continue
        except (OSError, ValueError):
            continue
        if rows == kept and meta.get("latest_version") == to:
            return d
    return None


def rollback_lists(lib, mirrors, bundle, to, drop, out):
    """Write the list files of `bundle` without the versions `drop` into the
    folder `out`. Uses a backup apply kept when one matches exactly, else
    rebuilds them from the current ones. Returns (how, backup folder or None)."""
    with open(os.path.join(bundle, "patch.cfg"), "rb") as f:
        cfg = f.read()
    drop = set(drop)
    kept_rows = [r for r in lib.polbundle.parse_cfg(cfg) if r["version"] not in drop]
    kept = sorted(_row_key(r) for r in kept_rows)
    src = find_backup(lib, mirrors, bundle, to, kept)
    if src:
        for n in LIST_FILES:
            shutil.copyfile(os.path.join(src, n), os.path.join(out, n))
        lib.polbundle.write_manifest(out)
        return "backup", src
    text = strip_versions(cfg.decode("latin-1"), drop)
    payload = text.encode("latin-1")
    if sorted(_row_key(r) for r in lib.polbundle.parse_cfg(payload)) != kept:
        raise lib.PolpatchError("The rebuilt patch.cfg does not hold the rows it should; "
                                "nothing was changed.")
    with open(os.path.join(out, "patch.cfg"), "wb") as f:
        f.write(payload)
    listraw = lib.slc.slc_compress(payload)
    if lib.slc.slc_decompress(listraw) != payload:
        raise lib.PolpatchError("The rebuilt patch list does not round-trip; nothing was changed.")
    with open(os.path.join(out, "patchlist.raw"), "wb") as f:
        f.write(listraw)
    with open(os.path.join(bundle, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta["latest_version"] = to
    if isinstance(meta.get("published"), list):
        meta["published"] = [p for p in meta["published"]
                             if not (isinstance(p, dict) and p.get("version") in drop)]
    meta["row_count"] = len(kept_rows)
    meta["version_count"] = len({r["version"] for r in kept_rows})
    meta["blob_count"] = len({r["slc"] for r in kept_rows})
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    lib.polbundle.write_manifest(out)
    return "rebuilt", None


def blobs_of(rows, versions):
    """Kept rows whose blobs sit in a folder of a version being removed."""
    versions = set(versions)
    return [r["path"] for r in rows
            if any(p and p.split("/", 1)[0] in versions for p in (r["slc"], r["olc"]))]
