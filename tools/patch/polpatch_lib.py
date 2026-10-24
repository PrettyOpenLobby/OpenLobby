"""The .polpatch format: build, verify and apply portable patch bundles.

A .polpatch is a ZIP archive holding one patch bundle (see docs/patching.md)
and a manifest, so a bundle built on the machine with the installed client can
be carried to the machine running the patch service and installed there in one
step. polpatch.py is the command line; this module is the library behind it,
and the functions a deployer imports are:

    read_manifest(path, limits=None) -> dict
        Opens the archive and parses and validates polpatch.json only. Cheap,
        for previews. Raises PolpatchError with one sentence.
    verify(path, against=None, limits=None, deep=True) -> [problem, ...]
        The full check: every entry's size and sha256, the bundle's structure,
        and (deep) that every blob unpacks to the file patch.cfg describes.
        An empty list means the file is good.
    apply(path, mirrors_dir, limits=None, deep=True, log=None) -> dict
        Installs a full bundle or merges an update into a mirrors directory.
        Refuses on any problem; raises PolpatchError.
    build_full(...), build_update(...), build_from_bundle(...) -> manifest
    bundle_info(bundle_dir), find_bundle(mirrors_dir, region, product)
    sort_versions(versions), versions_after(versions, version)

Everything in an archive is treated as hostile until it has been checked: the
deployer runs apply as root on files people upload.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import zipfile
import zlib
from argparse import Namespace

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "..", "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import slc                                                       # noqa: E402
import polbundle                                                 # noqa: E402
from polserver2 import port_for_product                         # noqa: E402

FORMAT = 1
TOOL = "polpatch.py 1"
MANIFEST_NAME = "polpatch.json"
BUNDLE_PREFIX = "bundle/"
LIST_FILES = ("meta.json", "patch.cfg", "patchlist.raw")

REGION_RE = re.compile(r"[A-Z0-9]{3}")
PRODUCT_RE = re.compile(r"[0-9]{4}")
# polserver2's YYYYMMDD_X. Versions of this shape sort in date order as plain
# strings, which is also the order they were published and are applied in.
VERSION_RE = re.compile(r"[0-9]{8}_[0-9A-Za-z]{1,2}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
CREATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# The patch service's map of region codes and products (docs/patching.md).
# The port of each product comes from polserver2.port_for_product.
REGIONS = {
    "W2U": "Windows, North America and Europe",
    "W20": "Windows, Japan",
    "P2U": "PlayStation 2, North America",
    "PS2": "PlayStation 2, Japan",
    "X2U": "Xbox 360, North America and Europe",
    "XB2": "Xbox 360, Japan",
}
PRODUCTS = {
    "1000": "PlayOnline Viewer",
    "0001": "FINAL FANTASY XI",
    "0002": "Tetra Master",
    "0003": "Janhourou",
    "0004": "Front Mission Online",
    "0010": "Dirge of Cerberus: Final Fantasy VII",
    "0011": "Fantasy Earth",
    "0014": "PlayOnline Friend List",
    "0015": "FINAL FANTASY XI Test Server",
}

GIB = 1024 ** 3
MIB = 1024 ** 2
DEFAULT_LIMITS = {
    "max_total": 16 * GIB,      # sum of every entry's uncompressed size
    "max_file": 4 * GIB,        # any one entry
    "max_entries": 500000,      # entries in the archive
    "max_manifest": 128 * MIB,  # polpatch.json
    "max_list": 64 * MIB,       # meta.json, patch.cfg, patchlist.raw each
    "max_deep": 1 * GIB,        # blobs larger than this are hashed, not unpacked
}
MAX_PROBLEMS = 50
_CHUNK = 1 << 20
_ZIP_TIME = (2000, 1, 1, 0, 0, 0)


class PolpatchError(Exception):
    """A .polpatch was refused. The message is one sentence for a person."""


def _limits(limits):
    out = dict(DEFAULT_LIMITS)
    for k, v in (limits or {}).items():
        if k not in out:
            raise ValueError(f"unknown limit {k!r}")
        out[k] = int(v)
    return out


# ---------------------------------------------------------------------------
# codes, versions, titles

def check_region(region, what="region"):
    if not isinstance(region, str) or not REGION_RE.fullmatch(region):
        raise PolpatchError(f"The {what} {region!r} is not a region code "
                            f"(three capital letters or digits, such as W2U).")
    if region not in REGIONS:
        raise PolpatchError(f"The {what} {region} is not one the patch service "
                            f"knows (known: {', '.join(REGIONS)}).")
    return region


def check_product(product):
    if not isinstance(product, str) or not PRODUCT_RE.fullmatch(product):
        raise PolpatchError(f"The product {product!r} is not a product code "
                            f"(four digits, such as 0002).")
    if product not in PRODUCTS:
        raise PolpatchError(f"The product {product} is not one the patch service "
                            f"knows (known: {', '.join(PRODUCTS)}).")
    return product


def check_version(version, what="version"):
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PolpatchError(f"The {what} {version!r} is not a patch version "
                            f"(YYYYMMDD_X or YYYYMMDD_XX, such as 20040714_0).")
    return version


def title_for(region, product):
    return f"{PRODUCTS.get(product, 'product ' + product)} ({REGIONS.get(region, region)})"


def version_key(v):
    """Sort key for patch versions: the date, then the suffix, shorter first, so a
    same-day _9 comes before _10 (SE's suffixes are one character; ours run to two)."""
    return (v[:8], len(v) - 9, v[9:])


def sort_versions(versions):
    """Oldest to newest (version_key)."""
    return sorted(set(versions), key=version_key)


def versions_after(versions, version):
    """The versions newer than `version`, oldest first: what a roll back to
    `version` would remove."""
    return [v for v in sort_versions(versions) if version_key(v) > version_key(version)]


# ---------------------------------------------------------------------------
# bundles on disk

def _read_meta(bundle_dir):
    try:
        with open(os.path.join(bundle_dir, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as e:
        raise PolpatchError(f"{bundle_dir} has no readable meta.json ({e}).")
    if not isinstance(meta, dict):
        raise PolpatchError(f"{bundle_dir}/meta.json is not a JSON object.")
    return meta


def _cfg_rows(bundle_dir):
    with open(os.path.join(bundle_dir, "patch.cfg"), "rb") as fh:
        return list(polbundle.parse_cfg(fh.read()))


def bundle_info(bundle_dir):
    """What a bundle directory holds, read the way polserver2 reads it."""
    meta = _read_meta(bundle_dir)
    base = os.path.basename(os.path.normpath(bundle_dir))
    region = meta.get("region", base.split("-")[0])
    product = meta.get("product", base.partition("-")[2] or "1000")
    versions = sort_versions(r["version"] for r in _cfg_rows(bundle_dir))
    return {"region": region, "product": product,
            "title": title_for(region, product),
            "aliases": list(meta.get("region_aliases", [])),
            "versions": versions, "latest": meta.get("latest_version", ""),
            "path": os.path.abspath(bundle_dir)}


def find_bundle(mirrors_dir, region, product):
    """The bundle directory under mirrors_dir serving region/product, or None.

    Matches what polserver2.load_bundles loads: a child directory with a
    patchlist.raw, identified by its meta.json (else its name)."""
    if not os.path.isdir(mirrors_dir):
        return None
    for name in sorted(os.listdir(mirrors_dir)):
        p = os.path.join(mirrors_dir, name)
        if not (os.path.isdir(p) and os.path.exists(os.path.join(p, "patchlist.raw"))):
            continue
        meta = {}
        try:
            with open(os.path.join(p, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            pass
        r = meta.get("region", name.split("-")[0])
        pr = meta.get("product", name.partition("-")[2] or "1000")
        if (r, pr) == (region, product):
            return p
    return None


# ---------------------------------------------------------------------------
# names inside the archive

def _bad_name(name):
    """Why an archive member name is unsafe, or None."""
    if not isinstance(name, str) or not name:
        return "has an empty name"
    if len(name) > 1024:
        return "has a name longer than 1024 characters"
    if "\\" in name:
        return "uses a backslash in its path"
    if name.startswith("/"):
        return "is an absolute path"
    if ":" in name:
        return "has a colon in its path (a drive letter or stream name)"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "has a control character in its path"
    parts = name.rstrip("/").split("/") if name.endswith("/") else name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "has an empty, '.' or '..' path component"
    return None


def _bundle_role(rel):
    """'list', 'slc', 'olc', or None for a path relative to bundle/."""
    if rel in LIST_FILES:
        return "list"
    parts = rel.split("/")
    if (len(parts) >= 4 and parts[0] == "blobs" and VERSION_RE.fullmatch(parts[1])):
        if parts[2] == "Direct" and rel.endswith(".slc"):
            return "slc"
        if parts[2] == "Indirect" and rel.endswith(".olc"):
            return "olc"
    # A short name straight under the version (<ver>/<name>.slc), which is how a
    # bundle keeps its list under the PS2 Viewer's 4 MiB limit (POL-1158).
    if len(parts) == 3 and parts[0] == "blobs" and VERSION_RE.fullmatch(parts[1]):
        if rel.endswith(".slc"):
            return "slc"
        if rel.endswith(".olc"):
            return "olc"
    return None


# ---------------------------------------------------------------------------
# manifest

_KEYS = ("format", "kind", "region", "product", "aliases", "versions",
         "base_version", "title", "note", "created", "tool", "files")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_manifest(m, limits=None):
    """Strict schema check of a parsed polpatch.json. Raises PolpatchError."""
    lim = _limits(limits)
    if not isinstance(m, dict):
        raise PolpatchError("polpatch.json is not a JSON object.")
    if "format" not in m or not _is_int(m["format"]) or m["format"] != FORMAT:
        raise PolpatchError(f"polpatch.json has format {m.get('format')!r}; this "
                            f"tool reads format {FORMAT} only.")
    extra = sorted(set(m) - set(_KEYS))
    if extra:
        raise PolpatchError(f"polpatch.json has fields this format does not "
                            f"define: {', '.join(extra)}.")
    missing = [k for k in _KEYS if k not in m]
    if missing:
        raise PolpatchError(f"polpatch.json is missing {', '.join(missing)}.")
    if m["kind"] not in ("full", "update"):
        raise PolpatchError(f"polpatch.json has kind {m['kind']!r}; it must be "
                            f"full or update.")
    check_region(m["region"])
    check_product(m["product"])
    al = m["aliases"]
    if not isinstance(al, list):
        raise PolpatchError("polpatch.json aliases is not a list.")
    for a in al:
        check_region(a, "alias")
    if len(set(al)) != len(al) or m["region"] in al:
        raise PolpatchError("polpatch.json aliases repeat a region code.")
    vs = m["versions"]
    if not isinstance(vs, list) or not vs:
        raise PolpatchError("polpatch.json lists no versions.")
    for v in vs:
        check_version(v)
    if vs != sort_versions(vs):
        raise PolpatchError("polpatch.json versions are not unique and in "
                            "oldest-to-newest order.")
    bv = m["base_version"]
    if m["kind"] == "full":
        if bv is not None:
            raise PolpatchError("A full .polpatch must have base_version null.")
    else:
        if bv is None:
            raise PolpatchError("An update .polpatch must name its base_version.")
        check_version(bv, "base_version")
        if bv >= vs[0]:
            raise PolpatchError(f"The update's versions must all be newer than "
                                f"its base_version {bv}.")
    for k, cap in (("title", 200), ("note", 4000), ("tool", 100)):
        if not isinstance(m[k], str) or len(m[k]) > cap:
            raise PolpatchError(f"polpatch.json {k} is not a string of at most "
                                f"{cap} characters.")
    if not isinstance(m["created"], str) or not CREATED_RE.fullmatch(m["created"]):
        raise PolpatchError("polpatch.json created is not an ISO UTC time "
                            "(YYYY-MM-DDTHH:MM:SSZ).")
    files = m["files"]
    if not isinstance(files, dict) or not files:
        raise PolpatchError("polpatch.json lists no files.")
    if len(files) + 1 > lim["max_entries"]:
        raise PolpatchError(f"polpatch.json lists {len(files)} files, over the "
                            f"limit of {lim['max_entries']}.")
    total = 0
    for name, ent in files.items():
        why = _bad_name(name)
        if why:
            raise PolpatchError(f"The manifest entry {name!r} {why}.")
        if not name.startswith(BUNDLE_PREFIX) or _bundle_role(name[len(BUNDLE_PREFIX):]) is None:
            raise PolpatchError(f"The manifest entry {name} is not part of a "
                                f"patch bundle.")
        if (not isinstance(ent, dict) or set(ent) != {"size", "sha256"}
                or not _is_int(ent["size"]) or ent["size"] < 0
                or not isinstance(ent["sha256"], str)
                or not SHA256_RE.fullmatch(ent["sha256"])):
            raise PolpatchError(f"The manifest entry for {name} is not "
                                f"{{size, sha256}}.")
        if ent["size"] > lim["max_file"]:
            raise PolpatchError(f"{name} is declared as {ent['size']} bytes, over "
                                f"the per-file limit of {lim['max_file']}.")
        total += ent["size"]
    if total > lim["max_total"]:
        raise PolpatchError(f"The files add up to {total} bytes, over the limit "
                            f"of {lim['max_total']}.")
    for rel in LIST_FILES:
        if BUNDLE_PREFIX + rel not in files:
            raise PolpatchError(f"The .polpatch has no bundle/{rel}.")
    return m


def _open_zip(path):
    if not os.path.isfile(path):
        raise PolpatchError(f"{path} is not a file.")
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise PolpatchError(f"{os.path.basename(path)} is not a readable ZIP "
                            f"archive ({e}).")


def _manifest_from_zip(zf, lim):
    try:
        info = zf.getinfo(MANIFEST_NAME)
    except KeyError:
        raise PolpatchError("The archive has no polpatch.json at its root, so it "
                            "is not a .polpatch.")
    if info.file_size > lim["max_manifest"]:
        raise PolpatchError(f"polpatch.json is {info.file_size} bytes, over the "
                            f"limit of {lim['max_manifest']}.")
    if info.flag_bits & 1:
        raise PolpatchError("polpatch.json is encrypted.")
    try:
        with zf.open(info) as fh:
            raw = fh.read(lim["max_manifest"] + 1)
        if len(raw) > lim["max_manifest"]:
            raise PolpatchError("polpatch.json is larger than it says.")
        m = json.loads(raw.decode("utf-8"))
    except PolpatchError:
        raise
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError,
            NotImplementedError, RuntimeError, zlib.error) as e:
        raise PolpatchError(f"polpatch.json cannot be read ({e}).")
    return validate_manifest(m, lim)


def read_manifest(path, limits=None):
    """Parse and validate polpatch.json only (no hashing). Raises PolpatchError."""
    lim = _limits(limits)
    with _open_zip(path) as zf:
        return _manifest_from_zip(zf, lim)


def _check_members(zf, m, lim):
    """Archive-level rules. Returns {name: ZipInfo} of the bundle files."""
    infos = zf.infolist()
    if len(infos) > lim["max_entries"]:
        raise PolpatchError(f"The archive has {len(infos)} entries, over the "
                            f"limit of {lim['max_entries']}.")
    seen, folded, files = set(), set(), {}
    total = 0
    for info in infos:
        name = info.filename
        if name in seen or name.lower() in folded:
            raise PolpatchError(f"The archive has {name} more than once.")
        seen.add(name)
        folded.add(name.lower())
        why = _bad_name(name)
        if why:
            raise PolpatchError(f"The archive entry {name!r} {why}.")
        mode = info.external_attr >> 16
        fmt = stat.S_IFMT(mode)
        if fmt and fmt not in (stat.S_IFREG, stat.S_IFDIR):
            raise PolpatchError(f"The archive entry {name} is a symlink or "
                                f"device, which a .polpatch never holds.")
        if info.flag_bits & 1:
            raise PolpatchError(f"The archive entry {name} is encrypted.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PolpatchError(f"The archive entry {name} uses compression "
                                f"method {info.compress_type}; only stored and "
                                f"deflated are accepted.")
        if name.endswith("/"):
            if not (name == BUNDLE_PREFIX or name.startswith(BUNDLE_PREFIX)) or info.file_size:
                raise PolpatchError(f"The archive has a folder {name} outside bundle/.")
            continue
        if fmt == stat.S_IFDIR:
            raise PolpatchError(f"The archive entry {name} is marked as a folder.")
        if info.file_size > lim["max_file"]:
            raise PolpatchError(f"{name} is {info.file_size} bytes, over the "
                                f"per-file limit of {lim['max_file']}.")
        total += info.file_size
        if total > lim["max_total"]:
            raise PolpatchError(f"The archive unpacks to more than the limit of "
                                f"{lim['max_total']} bytes.")
        if name == MANIFEST_NAME:
            continue
        if not name.startswith(BUNDLE_PREFIX):
            raise PolpatchError(f"The archive entry {name} is outside bundle/; a "
                                f".polpatch holds only polpatch.json and bundle/.")
        files[name] = info
    listed = set(m["files"])
    extra = sorted(set(files) - listed)
    if extra:
        raise PolpatchError(f"The archive holds {extra[0]}, which polpatch.json "
                            f"does not list" + (f" ({len(extra)} such entries)."
                                                if len(extra) > 1 else "."))
    absent = sorted(listed - set(files))
    if absent:
        raise PolpatchError(f"polpatch.json lists {absent[0]}, which the archive "
                            f"does not hold" + (f" ({len(absent)} missing)."
                                                if len(absent) > 1 else "."))
    for name, info in files.items():
        if info.file_size != m["files"][name]["size"]:
            raise PolpatchError(f"{name} is {info.file_size} bytes in the archive "
                                f"but polpatch.json says {m['files'][name]['size']}.")
    return files


# ---------------------------------------------------------------------------
# content: the same check for an archive and for a staged directory

class _ZipReader:
    def __init__(self, zf, infos):
        self.zf, self.infos = zf, infos

    def open(self, name):
        return self.zf.open(self.infos[name])


class _DirReader:
    def __init__(self, root):
        self.root = root

    def open(self, name):
        return open(os.path.join(self.root, *name.split("/")), "rb")


def _hash_entry(reader, name, want, keep_cap=None):
    """Stream one entry -> (problem or None, bytes if kept)."""
    h = hashlib.sha256()
    n = 0
    keep = bytearray() if keep_cap is not None and want["size"] <= keep_cap else None
    try:
        with reader.open(name) as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > want["size"]:
                    return f"{name} is larger than polpatch.json says.", None
                h.update(chunk)
                if keep is not None:
                    keep += chunk
    except (OSError, zipfile.BadZipFile, zlib.error, RuntimeError, ValueError) as e:
        return f"{name} cannot be read ({e}).", None
    if n != want["size"]:
        return f"{name} is {n} bytes but polpatch.json says {want['size']}.", None
    if h.hexdigest() != want["sha256"]:
        return f"{name} does not match its sha256 in polpatch.json.", None
    return None, (bytes(keep) if keep is not None else None)


def _unpack_check(data, row, lim):
    """Why an .slc blob does not unpack to the file its row describes, or None."""
    if not data:
        return "it is empty"
    method = data[0]
    try:
        if method == 1:
            raw = data[1:]
        elif method == 3:
            d = zlib.decompressobj()
            raw = d.decompress(data[1:], row["size"] + 1)
            if len(raw) > row["size"] or d.unconsumed_tail:
                return "it unpacks larger than its row says"
        elif method == 2:
            if len(data) > lim["max_list"]:
                return None             # too large to unpack here; hashed only
            raw = slc.slc_decompress(data)
        else:
            return f"it uses unknown container method {method}"
    except (zlib.error, ValueError, IndexError, struct.error) as e:
        return f"it does not unpack ({e})"
    if len(raw) != row["size"]:
        return f"it unpacks to {len(raw)} bytes, its row says {row['size']}"
    if polbundle.signed_bytesum(raw) != row["bytesum"]:
        return "its byte sum disagrees with patch.cfg"
    if struct.unpack("<I", hashlib.md5(raw).digest()[:4])[0] != row["md5_4"]:
        return "its MD5 disagrees with patch.cfg"
    return None


def _row_key(r):
    return (r["path"], r["version"], r["size"], r["bytesum"], r["md5_4"],
            r["slc"], r["slc_size"], r["olc"], r["olc_size"])


def _count_row_lines(cfg):
    n, inside = 0, False
    for line in cfg.decode("latin-1").splitlines():
        if line.startswith("file ") and line.endswith(" {"):
            inside = True
        elif line.strip() == "}":
            inside = False
        elif inside and line.strip():
            n += 1
    return n


def _check_structure(m, lists, against):
    """Cross-check the list files with each other, the manifest, and (when
    given) the bundle the file will be applied onto. Returns
    (rows, {blob name: row}). Raises PolpatchError."""
    try:
        meta = json.loads(lists["meta.json"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PolpatchError(f"bundle/meta.json is not JSON ({e}).")
    if not isinstance(meta, dict):
        raise PolpatchError("bundle/meta.json is not a JSON object.")
    region, product, kind = m["region"], m["product"], m["kind"]
    if meta.get("region") != region or meta.get("product") != product:
        raise PolpatchError(f"bundle/meta.json is for {meta.get('region')}/"
                            f"{meta.get('product')} but polpatch.json says "
                            f"{region}/{product}.")
    if sorted(meta.get("region_aliases", [])) != sorted(m["aliases"]):
        raise PolpatchError("bundle/meta.json region_aliases differ from "
                            "polpatch.json aliases.")
    if meta.get("port") != port_for_product(product):
        raise PolpatchError(f"bundle/meta.json gives port {meta.get('port')!r}; "
                            f"product {product} is served on "
                            f"{port_for_product(product)}.")
    if meta.get("latest_version") != m["versions"][-1]:
        raise PolpatchError(f"bundle/meta.json latest_version is "
                            f"{meta.get('latest_version')!r} but the newest "
                            f"version this file carries is {m['versions'][-1]}.")
    cfg = lists["patch.cfg"]
    try:
        served = slc.slc_decompress(lists["patchlist.raw"])
    except (ValueError, IndexError, struct.error, zlib.error) as e:
        raise PolpatchError(f"bundle/patchlist.raw does not unpack ({e}).")
    if served != cfg:
        raise PolpatchError("bundle/patchlist.raw does not unpack to "
                            "bundle/patch.cfg, so clients would see a different "
                            "list from the one checked.")
    rows = list(polbundle.parse_cfg(cfg))
    if not rows:
        raise PolpatchError("bundle/patch.cfg has no rows.")
    if _count_row_lines(cfg) != len(rows):
        raise PolpatchError("bundle/patch.cfg has rows that cannot be parsed.")
    for r in rows:
        if not VERSION_RE.fullmatch(r["version"]):
            raise PolpatchError(f"patch.cfg row for {r['path']} has version "
                                f"{r['version']!r}, which is not YYYYMMDD_X.")
        for key, sub, ext in (("slc", "Direct", ".slc"), ("olc", "Indirect", ".olc")):
            p = r[key]
            if p is None:
                continue
            short = p.startswith(f"{r['version']}/") and p.count("/") == 1   # see _bundle_role
            if (_bad_name(p) or not (p.startswith(f"{r['version']}/{sub}/") or short)
                    or not p.endswith(ext)):
                raise PolpatchError(f"patch.cfg names the blob {p!r} for "
                                    f"{r['path']}, which is not under "
                                    f"{r['version']}/{sub}/.")
    cfg_versions = sort_versions(r["version"] for r in rows)
    new = set(m["versions"])
    if kind == "full":
        if cfg_versions != m["versions"]:
            raise PolpatchError(f"patch.cfg carries versions {', '.join(cfg_versions)} "
                                f"but polpatch.json lists {', '.join(m['versions'])}.")
        mine = rows
    else:
        missing = sorted(new - set(cfg_versions))
        if missing:
            raise PolpatchError(f"patch.cfg has no rows for {missing[0]}, which "
                                f"the update says it carries.")
        old = [v for v in cfg_versions if v not in new]
        if not old or old[-1] != m["base_version"]:
            raise PolpatchError(f"patch.cfg's newest version before the update is "
                                f"{old[-1] if old else 'none'}, not the "
                                f"base_version {m['base_version']}.")
        mine = [r for r in rows if r["version"] in new]
    blobs = {}
    for r in mine:
        for key in ("slc", "olc"):
            if r[key] is None:
                continue
            name = BUNDLE_PREFIX + "blobs/" + r[key]
            size = r[key + "_size"]
            ent = m["files"].get(name)
            if ent is None:
                raise PolpatchError(f"patch.cfg names {r[key]}, which the "
                                    f".polpatch does not carry.")
            if ent["size"] != size:
                raise PolpatchError(f"{name} is {ent['size']} bytes but patch.cfg "
                                    f"says {size}.")
            blobs.setdefault(name, r if key == "slc" else None)
    for name in m["files"]:
        rel = name[len(BUNDLE_PREFIX):]
        if rel in LIST_FILES:
            continue
        if name not in blobs:
            ver = rel.split("/")[1]
            if kind == "update" and ver not in new:
                raise PolpatchError(f"The update carries {name}, a blob of "
                                    f"{ver}, which is not one of its new versions.")
            raise PolpatchError(f"The .polpatch carries {name}, which patch.cfg "
                                f"does not name.")
    if against is not None:
        _check_against(m, rows, meta, against)
    return rows, blobs


def _check_against(m, rows, meta, target):
    """The file against the bundle already being served at `target`."""
    if not os.path.isfile(os.path.join(target, "patch.cfg")):
        if m["kind"] == "update":
            raise PolpatchError(f"There is no {m['region']}/{m['product']} bundle "
                                f"to update; import the full .polpatch first.")
        return
    info = bundle_info(target)
    if (info["region"], info["product"]) != (m["region"], m["product"]):
        raise PolpatchError(f"{target} serves {info['region']}/{info['product']}, "
                            f"not {m['region']}/{m['product']}.")
    have = {_row_key(r) for r in _cfg_rows(target)}
    keys = {_row_key(r) for r in rows}
    if m["kind"] == "update":
        if info["latest"] != m["base_version"]:
            raise PolpatchError(f"The server's {m['region']}/{m['product']} bundle "
                                f"is at {info['latest'] or 'no version'}, but this "
                                f"update is built on {m['base_version']}; import "
                                f"the updates in order.")
        old = {_row_key(r) for r in rows if r["version"] not in set(m["versions"])}
        if old != have:
            raise PolpatchError(f"This update was built on a different "
                                f"{m['base_version']} bundle from the one the "
                                f"server has.")
        for v in m["versions"]:
            if os.path.exists(os.path.join(target, "blobs", v)):
                raise PolpatchError(f"The server's bundle already has blobs for "
                                    f"{v}.")
        for r in _cfg_rows(target):
            for key in ("slc", "olc"):
                if r[key] is None:
                    continue
                p = os.path.join(target, "blobs", *r[key].split("/"))
                if not os.path.isfile(p) or os.path.getsize(p) != r[key + "_size"]:
                    raise PolpatchError(f"The server's bundle is missing or has a "
                                        f"short {r[key]}; repair it before "
                                        f"adding an update.")
    else:
        lost = have - keys
        if lost:
            vs = sort_versions(k[1] for k in lost)
            raise PolpatchError(f"Installing this full .polpatch would drop rows "
                                f"the server already publishes ({', '.join(vs)}); "
                                f"published versions are never removed this way.")


def _check_content(m, reader, lim, against, deep, problems):
    lists = {}
    for rel in LIST_FILES:
        name = BUNDLE_PREFIX + rel
        if m["files"][name]["size"] > lim["max_list"]:
            raise PolpatchError(f"{name} is over the {lim['max_list']} byte limit "
                                f"for a list file.")
        why, data = _hash_entry(reader, name, m["files"][name], keep_cap=lim["max_list"])
        if why:
            raise PolpatchError(why)
        lists[rel] = data
    rows, blobs = _check_structure(m, lists, against)
    upstream = _upstream_mismatches(lists.get("meta.json"))
    for name in sorted(blobs):
        if len(problems) >= MAX_PROBLEMS:
            problems.append("There are more problems; fix these first.")
            break
        row = blobs[name]
        want = m["files"][name]
        cap = lim["max_deep"] if (deep and row is not None) else None
        why, data = _hash_entry(reader, name, want, keep_cap=cap)
        if why:
            problems.append(why)
            continue
        if data is not None:
            bad = _unpack_check(data, row, lim)
            if bad and name[len(BUNDLE_PREFIX + "blobs/"):] in upstream and "disagrees" in bad:
                continue      # Square Enix's own server serves it this way (meta.json)
            if bad:
                problems.append(f"{name} does not match patch.cfg: {bad}.")
    return rows


def _upstream_mismatches(meta_bytes):
    """The blobs a mirrored bundle's meta.json records as not matching Square
    Enix's own list on Square Enix's own server ("mismatches", kind "upstream"):
    a faithful copy of their mistake, not damage, so their checksum is not held
    against them. Nothing else is excused."""
    try:
        meta = json.loads(meta_bytes or b"{}")
    except ValueError:
        return set()
    rows = meta.get("mismatches") if isinstance(meta, dict) else None
    return {r["blob"] for r in rows or [] if isinstance(r, dict)
            and r.get("kind") == "upstream" and isinstance(r.get("blob"), str)}


def verify(path, against=None, limits=None, deep=True):
    """Every check a .polpatch must pass. Returns a list of one-sentence
    problems; an empty list means the file is good. `against` is the bundle
    directory it would be applied onto (optional)."""
    lim = _limits(limits)
    problems = []
    try:
        with _open_zip(path) as zf:
            m = _manifest_from_zip(zf, lim)
            infos = _check_members(zf, m, lim)
            _check_content(m, _ZipReader(zf, infos), lim, against, deep, problems)
    except PolpatchError as e:
        problems.insert(0, str(e))
    return problems


def check(path, against=None, limits=None, deep=True):
    """verify(), raising PolpatchError on the first problem."""
    problems = verify(path, against, limits, deep)
    if problems:
        more = f" ({len(problems) - 1} more problem(s))" if len(problems) > 1 else ""
        raise PolpatchError(problems[0] + more)
    return read_manifest(path, limits)


# ---------------------------------------------------------------------------
# apply

def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def apply(path, mirrors_dir, limits=None, deep=True, log=None):
    """Install a full .polpatch, or merge an update, into mirrors_dir.

    Nothing under mirrors_dir changes until the file has been extracted into a
    staging directory beside the bundles and checked again there. A full file
    goes in with one rename. An update moves its new version folders into
    blobs/ (versions the bundle does not have, so nothing the server serves
    changes) and then replaces the list files, after copying the old ones to
    mirrors_dir/.polpatch-backup/. Published versions are never deleted.
    Returns a summary dict; raises PolpatchError."""
    log = log or (lambda msg: None)
    lim = _limits(limits)
    mirrors_dir = os.path.abspath(mirrors_dir)
    os.makedirs(mirrors_dir, exist_ok=True)
    m = read_manifest(path, lim)
    region, product, kind = m["region"], m["product"], m["kind"]
    target = find_bundle(mirrors_dir, region, product)
    if target is None:
        target = os.path.join(mirrors_dir, f"{region}-{product}")
        if os.path.lexists(target) and not (os.path.isdir(target)
                                            and not os.listdir(target)):
            raise PolpatchError(f"{target} exists and is not a patch bundle; "
                                f"move it out of the way first.")
        exists = False
    else:
        exists = True
    if kind == "update" and not exists:
        raise PolpatchError(f"There is no {region}/{product} bundle in "
                            f"{mirrors_dir} to update; import the full .polpatch "
                            f"first.")
    if kind == "update":
        # Cheap refusals first, before anything is unpacked.
        info = bundle_info(target)
        if all(v in info["versions"] for v in m["versions"]):
            raise PolpatchError(f"The server's {region}/{product} bundle already "
                                f"has {', '.join(m['versions'])}.")
        if info["latest"] != m["base_version"]:
            raise PolpatchError(f"The server's {region}/{product} bundle is at "
                                f"{info['latest'] or 'no version'}, but this update "
                                f"is built on {m['base_version']}; import the "
                                f"updates in order.")
    result = {"region": region, "product": product, "kind": kind,
              "versions": m["versions"], "bundle": target, "backup": None}

    stage_root = tempfile.mkdtemp(prefix=".polpatch-stage-", dir=mirrors_dir)
    try:
        stage = os.path.join(stage_root, "bundle")
        os.makedirs(stage)
        with _open_zip(path) as zf:
            if _manifest_from_zip(zf, lim) != m:
                raise PolpatchError("The .polpatch changed while it was being "
                                    "applied.")
            infos = _check_members(zf, m, lim)
            for name in sorted(infos):
                rel = name[len(BUNDLE_PREFIX):]
                dest = os.path.join(stage, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                h = hashlib.sha256()
                n = 0
                with zf.open(infos[name]) as src, open(dest, "wb") as out:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > m["files"][name]["size"]:
                            raise PolpatchError(f"{name} is larger than "
                                                f"polpatch.json says.")
                        h.update(chunk)
                        out.write(chunk)
                if n != m["files"][name]["size"] or h.hexdigest() != m["files"][name]["sha256"]:
                    raise PolpatchError(f"{name} does not match its size and "
                                        f"sha256 in polpatch.json.")
        log(f"[*] extracted {len(infos)} file(s) into {stage_root}")
        problems = []
        _check_content(m, _DirReader(stage_root), lim,
                       target if exists else None, deep, problems)
        if problems:
            raise PolpatchError(problems[0] + (f" ({len(problems) - 1} more "
                                               f"problem(s))" if len(problems) > 1 else ""))
        polbundle.write_manifest(stage)

        if kind == "full" and not exists:
            if os.path.isdir(target):
                os.rmdir(target)
            os.rename(stage, target)
            result["action"] = "installed"
            log(f"[*] installed {region}/{product} at {target}")
            return result

        backup = os.path.join(mirrors_dir, ".polpatch-backup",
                              os.path.basename(target),
                              f"{_stamp()}-before-{m['versions'][-1]}")
        if kind == "full":
            same = all(_same_file(os.path.join(stage, n), os.path.join(target, n))
                       for n in LIST_FILES)
            if same:
                result["action"] = "unchanged"
                log(f"[*] {region}/{product} already holds exactly this bundle")
                return result
            _backup_lists(target, backup)
            trash = os.path.join(stage_root, "replaced")
            os.rename(target, trash)
            try:
                os.rename(stage, target)
            except OSError:
                os.rename(trash, target)
                raise
            _keep_extras(trash, target)
            result.update(action="replaced", backup=backup)
            log(f"[*] replaced {region}/{product}; its old list files are in {backup}")
            return result

        _backup_lists(target, backup)
        moved = []
        try:
            os.makedirs(os.path.join(target, "blobs"), exist_ok=True)
            for v in m["versions"]:
                src = os.path.join(stage, "blobs", v)
                if os.path.isdir(src):
                    os.rename(src, os.path.join(target, "blobs", v))
                    moved.append(v)
            for n in ("patch.cfg", "manifest.json", "patchlist.raw", "meta.json"):
                os.replace(os.path.join(stage, n), os.path.join(target, n))
        except OSError as e:
            for n in ("patch.cfg", "manifest.json", "patchlist.raw", "meta.json"):
                b = os.path.join(backup, n)
                if os.path.exists(b):
                    shutil.copy2(b, os.path.join(target, n))
            if not os.path.exists(os.path.join(backup, "manifest.json")):
                try:
                    os.remove(os.path.join(target, "manifest.json"))
                except OSError:
                    pass
            for v in moved:
                os.rename(os.path.join(target, "blobs", v), os.path.join(stage, "blobs", v))
            raise PolpatchError(f"Applying the update failed and was undone ({e}).")
        result.update(action="updated", backup=backup)
        log(f"[*] added {', '.join(m['versions'])} to {region}/{product}; the old "
            f"list files are in {backup}")
        return result
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _same_file(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _backup_lists(target, backup):
    os.makedirs(backup, exist_ok=True)
    for n in LIST_FILES + ("manifest.json",):
        p = os.path.join(target, n)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(backup, n))


def _keep_extras(old, new):
    """After a full replace, keep the old bundle's own extra files (such as
    polpublish's .prepublish copies) that the new one does not have. Blobs
    are not carried over: every row the old bundle served is in the new one."""
    for n in os.listdir(old):
        if n == "blobs" or n in LIST_FILES or n == "manifest.json":
            continue
        dst = os.path.join(new, n)
        if not os.path.exists(dst):
            os.rename(os.path.join(old, n), dst)


# ---------------------------------------------------------------------------
# build

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def package_bundle(bundle_dir, out, kind="full", versions=None, base_version=None,
                   note="", limits=None, log=None):
    """Write the .polpatch for a bundle directory. For an update, `versions`
    are the new versions and only their blobs go in. Checks the result with
    verify() before it is moved to `out`. Returns the manifest."""
    log = log or (lambda msg: None)
    meta = _read_meta(bundle_dir)
    region, product = meta.get("region"), meta.get("product")
    check_region(region)
    check_product(product)
    aliases = list(meta.get("region_aliases", []))
    rows = _cfg_rows(bundle_dir)
    all_versions = sort_versions(r["version"] for r in rows)
    for v in all_versions:
        check_version(v, "version in patch.cfg")
    if kind == "full":
        versions = all_versions
        wanted = rows
    else:
        versions = sort_versions(versions or [])
        wanted = [r for r in rows if r["version"] in set(versions)]
    names = [(BUNDLE_PREFIX + n, os.path.join(bundle_dir, n)) for n in LIST_FILES]
    seen = set()
    for r in wanted:
        for key in ("slc", "olc"):
            p = r[key]
            if p is None or p in seen:
                continue
            seen.add(p)
            full = os.path.join(bundle_dir, "blobs", *p.split("/"))
            if not os.path.isfile(full):
                raise PolpatchError(f"{bundle_dir} is missing the blob {p} that "
                                    f"its patch.cfg names.")
            names.append((BUNDLE_PREFIX + "blobs/" + p, full))
    m = {"format": FORMAT, "kind": kind, "region": region, "product": product,
         "aliases": aliases, "versions": versions,
         "base_version": base_version if kind == "update" else None,
         "title": title_for(region, product), "note": note or "",
         "created": _now(), "tool": TOOL, "files": {}}
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    partial = out + ".partial"
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as zf:
            for name, src in sorted(names):
                size = os.path.getsize(src)
                with open(src, "rb") as fh:
                    head = fh.read(1)
                zi = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
                zi.create_system = 3
                zi.external_attr = (stat.S_IFREG | 0o644) << 16
                # A zlib blob does not shrink again; everything else might.
                zi.compress_type = (zipfile.ZIP_STORED if name.endswith(".slc")
                                    and head == b"\x03" else zipfile.ZIP_DEFLATED)
                h = hashlib.sha256()
                with open(src, "rb") as fh, zf.open(zi, "w", force_zip64=size > 2 * GIB) as dst:
                    while True:
                        chunk = fh.read(_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        dst.write(chunk)
                m["files"][name] = {"size": size, "sha256": h.hexdigest()}
            validate_manifest(m, limits)
            zi = zipfile.ZipInfo(MANIFEST_NAME, date_time=_ZIP_TIME)
            zi.create_system = 3
            zi.external_attr = (stat.S_IFREG | 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, json.dumps(m, indent=1, sort_keys=False) + "\n")
        log(f"[*] wrote {len(m['files'])} file(s); checking the result")
        problems = verify(partial, limits=limits)
        if problems:
            raise PolpatchError("The file just built does not pass its own check: "
                                + problems[0])
        os.replace(partial, out)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return m


def _scrub_meta(bundle, record):
    """Keep only the folder name of the build source in meta.json, so a
    .polpatch does not carry the builder's own paths (user names and so on)."""
    p = os.path.join(bundle, "meta.json")
    with open(p, encoding="utf-8") as fh:
        meta = json.load(fh)
    rec = record(meta)
    if isinstance(rec, dict) and isinstance(rec.get("source"), str):
        rec["source"] = os.path.basename(os.path.normpath(rec["source"]))
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)


def build_from_bundle(bundle_dir, out, note="", limits=None, log=None):
    """Package an existing bundle directory (every version it holds) as a
    full .polpatch."""
    if not os.path.isfile(os.path.join(bundle_dir, "patch.cfg")):
        raise PolpatchError(f"{bundle_dir} is not a patch bundle (no patch.cfg).")
    return package_bundle(bundle_dir, out, "full", note=note, limits=limits, log=log)


def build_latest_update(bundle_dir, out, note="", limits=None, log=None):
    """Package the newest version of an existing bundle as an update on top
    of the version before it. Only the newest can be cut out this way: the
    bundle's lists already name every version it holds, so an older one would
    carry lists from after it."""
    if not os.path.isfile(os.path.join(bundle_dir, "patch.cfg")):
        raise PolpatchError(f"{bundle_dir} is not a patch bundle (no patch.cfg).")
    versions = sort_versions(r["version"] for r in _cfg_rows(bundle_dir))
    if len(versions) < 2:
        raise PolpatchError(f"{bundle_dir} holds one version only, so there is no update "
                            f"in it; pack it whole with from-bundle.")
    return package_bundle(bundle_dir, out, "update", versions=[versions[-1]],
                          base_version=versions[-2], note=note, limits=limits, log=log)


def _run_tool(fn, *args):
    try:
        rc = fn(*args)
    except SystemExit as e:
        if e.code in (0, None):
            return 0
        raise PolpatchError(str(e.code) if not isinstance(e.code, int)
                            else f"the toolkit stopped with exit code {e.code}.")
    return rc


def build_full(source, region, product, out, version="auto", aliases=(),
               note="", walk=False, excludes=(), limits=None, log=None):
    """Build a full .polpatch from an installed client with polsynth."""
    import polsynth
    check_region(region)
    check_product(product)
    for a in aliases:
        check_region(a, "alias")
    if version != "auto":
        check_version(version)
    if not os.path.isdir(source):
        raise PolpatchError(f"{source} is not a folder.")
    if not walk and not os.path.isfile(os.path.join(source, "file.txt")):
        raise PolpatchError(f"{source} has no file.txt; build with --walk to take "
                            f"every file in the folder.")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".polpatch-build-", dir=os.path.dirname(out))
    try:
        bundle = os.path.join(work, f"{region}-{product}")
        args = Namespace(source=source, out=bundle, region=region, product=product,
                         port=None, version=version, alias=list(aliases) or None,
                         token=0x4e4e6497, walk=walk, exclude=list(excludes) or None,
                         keep_patchver=False, verify_list=False, force=False,
                         verbose=False)
        _run_tool(polsynth.synth, args)
        _scrub_meta(bundle, lambda meta: meta.get("synthesised", {}))
        return package_bundle(bundle, out, "full", note=note, limits=limits, log=log)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_update(base, files_dir, version, out, note="", limits=None, log=None):
    """Build an update .polpatch: `files_dir` holds the changed and added
    files laid out like the install, published as `version` on top of `base`
    (a bundle directory or a .polpatch) with polpublish."""
    import polpublish
    check_version(version)
    if not os.path.isdir(files_dir):
        raise PolpatchError(f"{files_dir} is not a folder.")
    if os.path.exists(os.path.join(files_dir, "patch.ver")):
        raise PolpatchError(f"{files_dir} holds patch.ver, which is per-install "
                            f"client state and is never published.")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".polpatch-build-", dir=os.path.dirname(out))
    try:
        bundle = os.path.join(work, "bundle")
        os.makedirs(bundle)
        if os.path.isfile(base):
            check(base, limits=limits)
            with zipfile.ZipFile(base) as zf:
                for n in LIST_FILES:
                    with zf.open(BUNDLE_PREFIX + n) as src, \
                            open(os.path.join(bundle, n), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        elif os.path.isfile(os.path.join(base, "patch.cfg")):
            for n in LIST_FILES:
                shutil.copy2(os.path.join(base, n), os.path.join(bundle, n))
        else:
            raise PolpatchError(f"{base} is neither a .polpatch nor a patch bundle.")
        meta = _read_meta(bundle)
        base_version = meta.get("latest_version", "")
        check_version(base_version, "base bundle's latest_version")
        if version_key(version) <= version_key(base_version):
            raise PolpatchError(f"The new version {version} must be newer than the "
                                f"base's latest, {base_version}.")
        argv = ["--bundle", bundle, "--version", version, "--overlay", files_dir]
        if note:
            argv += ["--note", note]
        _run_tool(polpublish.main, argv)
        if not os.path.isdir(os.path.join(bundle, "blobs", version)):
            raise PolpatchError(f"{files_dir} gave nothing to publish.")
        _scrub_meta(bundle, lambda meta: (meta.get("published") or [{}])[-1])
        return package_bundle(bundle, out, "update", versions=[version],
                              base_version=base_version, note=note,
                              limits=limits, log=log)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summary(m):
    """A short multi-line description of a manifest for people."""
    total = sum(e["size"] for e in m["files"].values())
    blobs = sum(1 for n in m["files"] if n.startswith(BUNDLE_PREFIX + "blobs/"))
    lines = [f"{m['title']}",
             f"  service   {m['region']}/{m['product']}"
             + (f" (also {', '.join(m['aliases'])})" if m["aliases"] else "")
             + f", port {port_for_product(m['product'])}",
             f"  kind      {m['kind']}"
             + (f" on top of {m['base_version']}" if m["kind"] == "update" else ""),
             f"  versions  {', '.join(m['versions'])}",
             f"  files     {blobs} blob(s), {total / 1e6:.1f} MB in all",
             f"  created   {m['created']} by {m['tool']}"]
    if m["note"]:
        lines.append(f"  note      {m['note']}")
    return "\n".join(lines)
