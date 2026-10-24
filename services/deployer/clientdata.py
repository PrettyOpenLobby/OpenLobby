"""Client data for the deployer's import action: the catalog's "import"
blocks, safe paths under a root, the folder browser, and uploaded archives.

Everything a request names (a folder under POL_DEPLOY_CLIENTS, an upload's
subfolder, the members of an uploaded archive) is checked here before the
deployer touches the filesystem with it. docs/deployer.md, "Importing client
data", describes the rules. Standard library only.
"""
import errno
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import threading
import time
import zipfile

UPLOAD_TTL = 24 * 3600      # an upload lives this long after creation or its last import
MAX_FILES = 200000          # members an archive may hold
BROWSE_CAP = 500            # entries GET /api/browse lists
MATCH_DIRS = 2000           # folders an upload's tree is searched for matches
MATCH_DEPTH = 4             # how deep below an upload's top that search goes
CHUNK = 1 << 20

UPLOAD_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class PathError(ValueError):
    """A relative path that is absolute, climbs out, or resolves outside its root."""


class ArchiveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ------------------------------------------------------------ catalog

def valid_import(imp):
    """Why an "import" block is unusable, or None when it is fine."""
    if not isinstance(imp, dict):
        return "import is not an object"
    argv = imp.get("argv")
    if not (isinstance(argv, list) and len(argv) >= 2 and all(isinstance(a, str) for a in argv)):
        return "import.argv must be a list of strings"
    if argv[0] != "python":
        return "import.argv must start with python"
    try:
        tool = rel_parts(argv[1])
    except PathError as exc:
        return "import.argv[1]: %s" % exc
    if not tool or not argv[1].endswith(".py"):
        return "import.argv[1] must be a .py file in the checkout"
    if not any("{input}" in a for a in argv[2:]):
        return "import.argv names no {input}"
    if any("{" in a.replace("{input}", "") for a in argv):
        return "import.argv has a placeholder other than {input}"
    exp = imp.get("expects")
    if not (isinstance(exp, list) and 1 <= len(exp) <= 3 and all(isinstance(e, str) for e in exp)):
        return "import.expects must list 1 to 3 paths"
    for e in exp:
        try:
            if not rel_parts(e):
                return "import.expects has an empty path"
        except PathError as exc:
            return "import.expects %r: %s" % (e, exc)
    for k in ("hint", "label"):
        if not (isinstance(imp.get(k), str) and imp[k].strip()):
            return "import.%s must be a sentence" % k
    return None


def import_items(catalog):
    """[(key, entry, item, import)] for every data item with a usable import."""
    out = []
    for key, e in sorted(catalog.items(), key=lambda kv: (kv[1].get("order", 0), kv[0])):
        if e.get("kind") != "game":
            continue
        for item in e.get("data", []):
            imp = item.get("import")
            if isinstance(item, dict) and imp is not None and valid_import(imp) is None:
                out.append((key, e, item, imp))
    return out


def find_import(catalog, key, item_id):
    """(entry, item, import or None) or (entry, None, None) for an unknown item."""
    e = catalog.get(key)
    for item in (e or {}).get("data", []):
        if item.get("id") == item_id:
            imp = item.get("import")
            return e, item, (imp if imp is not None and valid_import(imp) is None else None)
    return e, None, None


def label(entry, imp):
    return "%s: %s" % (entry["name"], imp["label"])


def public_import(imp):
    """What a response may say about an import: never its argv."""
    return {"hint": imp["hint"], "expects": list(imp["expects"])}


# -------------------------------------------------------------- paths

def rel_parts(rel):
    """The components of a relative path, '' and '.' dropped. Refuses an
    absolute path, a drive, a backslash path that is absolute, NUL and '..'."""
    if not isinstance(rel, str):
        raise PathError("the path must be a string")
    if "\x00" in rel:
        raise PathError("the path holds a NUL byte")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or _DRIVE.match(norm):
        raise PathError("the path must be relative")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise PathError("the path must not contain ..")
    return parts


def inside(path, root):
    path, root = os.path.normcase(path), os.path.normcase(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve_under(root, rel):
    """realpath of root/rel, refusing anything that ends up outside root
    (symlinks out included). root must already be a realpath."""
    parts = rel_parts(rel)
    real = os.path.realpath(os.path.join(root, *parts)) if parts else root
    if not inside(real, root):
        raise PathError("the path leads outside the folder")
    return real, "/".join(parts)


def present(folder, rel, root):
    """rel exists under folder without leaving root."""
    try:
        real = os.path.realpath(os.path.join(folder, *rel_parts(rel)))
    except PathError:
        return False
    return inside(real, root) and os.path.exists(real)


def missing_expects(folder, root, imp):
    return [e for e in imp["expects"] if not present(folder, e, root)]


def matches_in(folder, root, catalog):
    """[{game, item, label}] whose expects are all present in folder."""
    return [{"game": key, "item": item["id"], "label": label(e, imp)}
            for key, e, item, imp in import_items(catalog)
            if not missing_expects(folder, root, imp)]


def find_matches(top, catalog):
    """matches_in for top and the folders below it (a few levels, a bounded
    number of folders), each with "subdir", the folder relative to top."""
    out, queue, seen = [], [(top, "", 0)], 0
    while queue and seen < MATCH_DIRS:
        d, rel, depth = queue.pop(0)
        seen += 1
        for m in matches_in(d, top, catalog):
            m["subdir"] = rel
            out.append(m)
        if depth >= MATCH_DEPTH:
            continue
        try:
            with os.scandir(d) as it:
                subs = sorted(x.name for x in it if x.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for n in subs:
            queue.append((os.path.join(d, n), (rel + "/" + n) if rel else n, depth + 1))
    return out


def browse(root, rel, catalog, cap=BROWSE_CAP, describe=None):
    """The answer of GET /api/browse. Raises PathError (400) or
    FileNotFoundError (404). describe(path), when given, is what a file
    ending in .polpatch carries as "polpatch"."""
    real, rel = resolve_under(root, rel)
    if not os.path.isdir(real):
        raise FileNotFoundError(rel)
    dirs, files, n, truncated = [], [], 0, False
    with os.scandir(real) as it:
        entries = sorted(it, key=lambda x: x.name.lower())
    for x in entries:
        target = os.path.realpath(x.path)
        if not inside(target, root):
            continue                      # a symlink out of the root is not shown
        if n >= cap:
            truncated = True
            break
        try:
            if os.path.isdir(target):
                dirs.append(x.name)
            elif os.path.isfile(target):
                entry = {"name": x.name, "size": os.path.getsize(target)}
                if describe and x.name.lower().endswith(".polpatch"):
                    entry["polpatch"] = describe(target)
                files.append(entry)
            else:
                continue
        except OSError:
            continue
        n += 1
    parent = None if not rel else (rel.rsplit("/", 1)[0] if "/" in rel else "")
    return {"root": root, "path": rel, "parent": parent, "dirs": dirs, "files": files,
            "truncated": truncated, "matches": matches_in(real, root, catalog)}


# ----------------------------------------------------------- archives

def member_parts(name):
    try:
        return rel_parts(name)
    except PathError as exc:
        raise ArchiveError(400, "The archive member %r is refused: %s." % (name[:200], exc))


class _Plan:
    """Member names checked before anything is written: no duplicates, no
    file standing where another member needs a folder."""

    def __init__(self, max_bytes, max_files):
        self.max_bytes, self.max_files = max_bytes, max_files
        self.files, self.dirs = set(), set()
        self.count = self.declared = 0

    def add(self, name, is_dir, size):
        parts = member_parts(name)
        if not parts:
            return
        self.count += 1
        if self.count > self.max_files:
            raise ArchiveError(413, "The archive holds more than %d files." % self.max_files)
        key = "/".join(parts).lower()
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i]).lower()
            if prefix in self.files:
                raise ArchiveError(400, "The archive member %r sits under a file of the "
                                        "same name." % name[:200])
            self.dirs.add(prefix)
        if is_dir:
            if key in self.files:
                raise ArchiveError(400, "The archive has %r as a file and a folder." % name[:200])
            self.dirs.add(key)
            return
        if key in self.files or key in self.dirs:
            raise ArchiveError(400, "The archive holds %r twice." % name[:200])
        self.files.add(key)
        self.declared += max(int(size), 0)
        if self.declared > self.max_bytes:
            raise ArchiveError(413, "The archive unpacks to more than %d bytes." % self.max_bytes)


class _Writer:
    """Writes members under dest, counting every byte against the cap."""

    def __init__(self, dest, max_bytes):
        self.dest, self.max_bytes, self.total, self.files = dest, max_bytes, 0, 0

    def mkdir(self, name):
        parts = member_parts(name)
        if parts:
            d = os.path.join(self.dest, *parts)
            os.makedirs(d, mode=0o755, exist_ok=True)
            os.chmod(d, 0o755)

    def write(self, name, src, declared):
        parts = member_parts(name)
        if not parts:
            return
        d = os.path.join(self.dest, *parts[:-1])
        os.makedirs(d, mode=0o755, exist_ok=True)
        path = os.path.join(d, parts[-1])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o644)
        n = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                self.total += len(chunk)
                if n > declared:
                    raise ArchiveError(400, "The archive member %r is larger than it "
                                            "declares." % name[:200])
                if self.total > self.max_bytes:
                    raise ArchiveError(413, "The archive unpacks to more than %d bytes."
                                       % self.max_bytes)
                out.write(chunk)
        os.chmod(path, 0o644)
        self.files += 1


_TAR_KIND = (("issym", "a symbolic link"), ("islnk", "a hard link"),
             ("ischr", "a device file"), ("isblk", "a device file"),
             ("isfifo", "a FIFO"), ("issparse", "a sparse file"))


def _tar_kind(m):
    for test, what in _TAR_KIND:
        if getattr(m, test)():
            return what
    if m.isreg() or m.isdir():
        return None
    return "a special file"


def _zip_kind(info):
    if info.create_system != 3:           # only unix zips carry file types
        return None
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind in (0, stat.S_IFREG, stat.S_IFDIR):
        return None
    if kind == stat.S_IFLNK:
        return "a symbolic link"
    if kind in (stat.S_IFCHR, stat.S_IFBLK):
        return "a device file"
    if kind == stat.S_IFIFO:
        return "a FIFO"
    return "a special file"


def sniff(path):
    with open(path, "rb") as f:
        head = f.read(512)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "tgz"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def extract(path, dest, max_bytes, max_files=MAX_FILES):
    """Unpack the archive at path into dest (which must not exist). Returns
    (bytes, files). Raises ArchiveError; dest is removed on any failure."""
    kind = sniff(path)
    if kind is None:
        raise ArchiveError(400, "The upload is not a zip, tar or tar.gz archive.")
    os.makedirs(dest, mode=0o755)
    try:
        if kind == "zip":
            w = _extract_zip(path, dest, max_bytes, max_files)
        else:
            w = _extract_tar(path, dest, max_bytes, max_files, "r|gz" if kind == "tgz" else "r|")
        if not w.files:
            raise ArchiveError(400, "The archive holds no files.")
        return w.total, w.files
    except ArchiveError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError,
            NotImplementedError, RuntimeError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise ArchiveError(507, "The deployer's volume filled up while unpacking the archive.")
        raise ArchiveError(400, "The archive cannot be read: %s." % exc)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _extract_zip(path, dest, max_bytes, max_files):
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ArchiveError(413, "The archive holds more than %d files." % max_files)
        plan = _Plan(max_bytes, max_files)
        for info in infos:
            what = _zip_kind(info)
            if what:
                raise ArchiveError(400, "The archive member %r is %s; only files and "
                                        "folders are accepted." % (info.filename[:200], what))
            if info.flag_bits & 0x1:
                raise ArchiveError(400, "The archive member %r is encrypted."
                                   % info.filename[:200])
            plan.add(info.filename, info.is_dir(), info.file_size)
        w = _Writer(dest, max_bytes)
        for info in infos:
            if info.is_dir():
                w.mkdir(info.filename)
                continue
            with zf.open(info) as src:
                w.write(info.filename, src, info.file_size)
        return w


def _extract_tar(path, dest, max_bytes, max_files, mode):
    # first pass: every header, nothing written
    plan = _Plan(max_bytes, max_files)
    with tarfile.open(path, mode) as tf:
        for m in tf:
            what = _tar_kind(m)
            if what:
                raise ArchiveError(400, "The archive member %r is %s; only files and "
                                        "folders are accepted." % (m.name[:200], what))
            plan.add(m.name, m.isdir(), m.size)
    w = _Writer(dest, max_bytes)
    with tarfile.open(path, mode) as tf:
        for m in tf:
            if _tar_kind(m):
                raise ArchiveError(400, "The archive changed while it was read.")
            if m.isdir():
                w.mkdir(m.name)
                continue
            src = tf.extractfile(m)
            w.write(m.name, src, m.size)
    return w


# ------------------------------------------------------------ uploads

def clean_name(raw):
    """X-Filename for display: percent-decoded UTF-8, the last path
    component only, no control characters. Never used as a path."""
    import urllib.parse
    name = urllib.parse.unquote(raw or "", encoding="utf-8", errors="replace")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:200] or "upload"


class Uploads:
    """Uploads under STATE/uploads: <id>/ the unpacked tree, <id>.json its
    record. An upload is listed once its record exists."""

    def __init__(self, state):
        self.dir = os.path.join(state, "uploads")
        self.incoming = os.path.join(self.dir, ".incoming")
        os.makedirs(self.incoming, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.lock = threading.Lock()

    def new_id(self):
        t = time.time()
        return time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + "-" + secrets.token_hex(3)

    def tree(self, uid):
        return os.path.join(self.dir, uid)

    def meta_path(self, uid):
        return os.path.join(self.dir, uid + ".json")

    def load(self, uid):
        if not UPLOAD_ID.fullmatch(uid or ""):
            return None
        try:
            with open(self.meta_path(uid), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        return meta if os.path.isdir(self.tree(uid)) else None

    def save(self, meta):
        with self.lock:
            tmp = self.meta_path(meta["id"]) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            os.replace(tmp, self.meta_path(meta["id"]))

    def all(self):
        out = []
        for n in sorted(os.listdir(self.dir)):
            if n.endswith(".json"):
                m = self.load(n[:-5])
                if m:
                    out.append(m)
        out.sort(key=lambda m: m.get("_t", 0), reverse=True)
        return out

    def remove(self, uid):
        try:
            os.remove(self.meta_path(uid))
        except OSError:
            pass
        shutil.rmtree(self.tree(uid), ignore_errors=True)

    def touch(self, uid, when):
        m = self.load(uid)
        if m:
            m["last_import"] = when
            m["_last"] = time.time()
            self.save(m)

    def expire(self, keep=(), now=None):
        """Remove uploads past their time, and anything a previous run left
        half written. `keep` are ids in use."""
        now = time.time() if now is None else now
        shutil.rmtree(self.incoming, ignore_errors=True)
        os.makedirs(self.incoming, exist_ok=True)
        names = set(os.listdir(self.dir))
        for n in names:
            p = os.path.join(self.dir, n)
            if n.endswith(".tmp"):
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _unlink(p)
            elif UPLOAD_ID.fullmatch(n) and os.path.isdir(p) and n + ".json" not in names:
                shutil.rmtree(p, ignore_errors=True)
        gone = []
        for m in self.all():
            last = max(m.get("_t", 0), m.get("_last") or 0)
            if m["id"] not in keep and now - last > UPLOAD_TTL:
                self.remove(m["id"])
                gone.append(m["id"])
        return gone


def _unlink(p):
    try:
        os.remove(p)
    except OSError:
        pass


def public_upload(m):
    """An upload as the API shows it. "kind" is "polpatch" for a .polpatch
    kept whole (with "polpatch", what its manifest says), else "archive"."""
    out = {k: m.get(k) for k in ("id", "name", "created", "last_import", "size", "files",
                                 "matches")}
    out["kind"] = m.get("kind") or "archive"
    out["polpatch"] = m.get("polpatch") if out["kind"] == "polpatch" else None
    return out
