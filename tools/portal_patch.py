#!/usr/bin/env python3
"""Apply this project's portal edits to a portal page tree you supply.

This repository ships no Square Enix portal pages. What it ships, under
portal/, is:

  portal/patches/manifest.json   one entry per page we edited
  portal/patches/*.diff          the edit itself: a zero-context unified diff
                                 (the `diff -U0` format) that carries only the
                                 lines we changed, never the surrounding page
  portal/authored/               pages we wrote from scratch, stored with a
                                 `.txt` suffix and renamed on install

You bring the base tree (your own capture of the portal, or the output of
tools/portal_crawl.py where pages are still served), then run:

    python tools/portal_patch.py --www ./www          # apply
    python tools/portal_patch.py --www ./www --check  # dry run, write nothing
    python tools/portal_patch.py --www ./www --force  # overwrite authored pages
                                                      # that already exist
    python tools/portal_patch.py --selftest           # verify the tool + diffs

For each manifest entry the tool hashes the page in your tree. If it matches
the original the patch was made from, the diff is applied in pure Python (no
external `patch` binary) and the untouched page is kept beside the result as
`<file>.orig`. If it already matches the patched result it is reported as
already applied. Anything else is reported as an unexpected version and
skipped; the exit status is non-zero when anything was skipped.

Maintainer mode, which needs the private development tree and is never
required by users:

    python tools/portal_patch.py --rebuild --dev-www PATH   # regenerate diffs
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATCH_DIR = os.path.join(ROOT, "portal", "patches")
AUTHORED_DIR = os.path.join(ROOT, "portal", "authored")
MANIFEST = "manifest.json"
NL = b"\n"
CRLF = b"\r\n"
NO_NEWLINE = b"\\ No newline at end of file"
HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: authored files are stored with this extra suffix because the real page
#: extension is not allowed in this repository; install strips it.
STORED_SUFFIX = ".txt"
RENAME_EXT = (".pml", ".lst")
#: files under portal/authored/ that are documentation, not pages
AUTHORED_SKIP = {"IMAGES-NEEDED.txt", "README.md", "README.txt"}


# --------------------------------------------------------------------------
# bytes <-> lines
# --------------------------------------------------------------------------
def split_lines(data):
    """Split on LF only, keeping the terminator on each line. CR stays part
    of the line, so CRLF files round-trip byte for byte."""
    parts = data.split(NL)
    lines = [p + NL for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def write(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------
# zero-context unified diff: make
# --------------------------------------------------------------------------
def _range(start, count):
    if count == 0:
        return b"%d,0" % start          # start = number of lines before
    if count == 1:
        return b"%d" % (start + 1)
    return b"%d,%d" % (start + 1, count)


def _emit(out, prefix, line):
    if line.endswith(NL):
        out.append(prefix + line[:-1])
    else:
        out.append(prefix + line)
        out.append(NO_NEWLINE)


def make_diff(old_lines, new_lines, a_name, b_name):
    """Bytes of a `diff -U0` style patch turning old_lines into new_lines."""
    out = [b"--- a/" + a_name.encode(), b"+++ b/" + b_name.encode()]
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        out.append(b"@@ -" + _range(i1, i2 - i1) + b" +" + _range(j1, j2 - j1)
                   + b" @@")
        for line in old_lines[i1:i2]:
            _emit(out, b"-", line)
        for line in new_lines[j1:j2]:
            _emit(out, b"+", line)
    return NL.join(out) + NL


# --------------------------------------------------------------------------
# zero-context unified diff: parse + apply
# --------------------------------------------------------------------------
class Hunk:
    __slots__ = ("old_idx", "old_count", "new_idx", "new_count", "minus",
                 "plus")

    def __init__(self, m):
        os_, oc, ns, nc = (int(m.group(1)), m.group(2), int(m.group(3)),
                           m.group(4))
        self.old_count = 1 if oc is None else int(oc)
        self.new_count = 1 if nc is None else int(nc)
        # a zero count names the line BEFORE the change; a non-zero count
        # names the first changed line (1-based). Both become a 0-based index.
        self.old_idx = os_ if self.old_count == 0 else os_ - 1
        self.new_idx = ns if self.new_count == 0 else ns - 1
        self.minus = []
        self.plus = []


class PatchError(Exception):
    pass


def parse_diff(data):
    hunks = []
    last = None      # (list, index) of the last content line, for NO_NEWLINE
    for raw in data.split(NL):
        if not hunks and (raw.startswith(b"--- ") or raw.startswith(b"+++ ")):
            continue
        m = HUNK_RE.match(raw)
        if m:
            hunks.append(Hunk(m))
            last = None
            continue
        if not hunks:
            if raw.strip() == b"":
                continue
            raise PatchError("text before the first hunk: %r" % raw[:40])
        h = hunks[-1]
        if raw.startswith(b"-"):
            h.minus.append(raw[1:] + NL)
            last = (h.minus, len(h.minus) - 1)
        elif raw.startswith(b"+"):
            h.plus.append(raw[1:] + NL)
            last = (h.plus, len(h.plus) - 1)
        elif raw.startswith(b"\\"):
            if last is None:
                raise PatchError("stray no-newline marker")
            lst, i = last
            lst[i] = lst[i][:-1]
        elif raw == b"":
            continue          # trailing newline of the file
        elif raw.startswith(b" "):
            raise PatchError("context line found; this tool applies "
                             "zero-context diffs only")
        else:
            raise PatchError("unrecognised line: %r" % raw[:40])
    for h in hunks:
        if len(h.minus) != h.old_count or len(h.plus) != h.new_count:
            raise PatchError("hunk at -%d declares %d/%d lines but carries "
                             "%d/%d" % (h.old_idx + 1, h.old_count,
                                        h.new_count, len(h.minus),
                                        len(h.plus)))
    return hunks


def apply_diff(old_data, diff_data):
    """Return old_data with the hunks applied. Hunks are placed by their line
    numbers and every removed line is checked against the original."""
    old = split_lines(old_data)
    out = []
    pos = 0
    for h in parse_diff(diff_data):
        if h.old_idx < pos:
            raise PatchError("hunks overlap at line %d" % (h.old_idx + 1))
        if h.old_idx + h.old_count > len(old):
            raise PatchError("hunk at line %d runs past the end of the file"
                             % (h.old_idx + 1))
        out.extend(old[pos:h.old_idx])
        if len(out) != h.new_idx:
            raise PatchError("hunk at line %d expects to land at new line %d "
                             "but lands at %d" % (h.old_idx + 1,
                                                  h.new_idx + 1, len(out) + 1))
        have = old[h.old_idx:h.old_idx + h.old_count]
        if have != h.minus:
            raise PatchError("original line %d does not match the patch"
                             % (h.old_idx + 1))
        out.extend(h.plus)
        pos = h.old_idx + h.old_count
    out.extend(old[pos:])
    return b"".join(out)


def normalise(data, mode):
    if mode == "lf":
        return data.replace(CRLF, NL)
    return data


# --------------------------------------------------------------------------
# scan: does a diff carry the page's own text instead of our edit?
# --------------------------------------------------------------------------
def _norm(line):
    return b" ".join(line.split())


_TAG = re.compile(rb"<[^>]*>")


def _is_prose(line):
    """False for a line that is nothing but markup (`<if expr=...>`,
    `</array>`), which any edit legitimately reuses."""
    return bool(_TAG.sub(b"", line).strip())


def scan_diff(diff_data, original_data):
    """For each hunk, count added lines that are really lines of the original
    page (moved, re-flowed, re-encoded or otherwise mechanically damaged)
    rather than our own text. Returns (summary dict, list of flagged hunk
    descriptions)."""
    orig_lines = split_lines(original_data)
    orig_set = {_norm(l) for l in orig_lines if _is_prose(l)}
    orig_joined = b" ".join(_norm(l) for l in orig_lines)
    damaged = {}
    for fn in REPAIRS.values():
        for l in orig_lines:
            img = fn(l)
            if img != l:
                damaged.setdefault(img, l)
    hunks = parse_diff(diff_data)
    flagged = []
    plus = minus = 0
    for h in hunks:
        plus += len(h.plus)
        minus += len(h.minus)
        counted = se = corrupt = 0
        for l in h.plus:
            if l in damaged:
                corrupt += 1
                continue
            n = _norm(l)
            if len(n) < 12 or not _is_prose(l):
                continue
            counted += 1
            if n in orig_set or (len(n) >= 24 and n in orig_joined):
                se += 1
        why = []
        if corrupt:
            why.append("%d line(s) are the original's own lines with "
                       "mechanical damage" % corrupt)
        if se and (se >= 3 or se * 2 >= counted):
            why.append("%d of %d substantive added line(s) are the page's own "
                       "text" % (se, counted))
        if why:
            flagged.append("hunk at line %d: %s" % (h.old_idx + 1,
                                                    "; ".join(why)))
    return {"hunks": len(hunks), "plus": plus, "minus": minus}, flagged


def _damage_utf8_replacement(line):
    """An editor decoded a Shift-JIS page as UTF-8 and saved it: every byte
    it could not decode became U+FFFD."""
    return line.decode("utf-8", "replace").encode("utf-8")


_QUOTED_EQ = re.compile(rb"=([\"'])([^\"']*)\1")


def _damage_equals_to_dash(line):
    """A search-and-replace rewrote `="x"` as `-x` and every other `=` as
    `-`, through SE's comment banners and into live tags."""
    return _QUOTED_EQ.sub(rb"-\2", line).replace(b"=", b"-")


REPAIRS = {
    "utf8-replacement": _damage_utf8_replacement,
    "equals-to-dash": _damage_equals_to_dash,
}


def repair(orig_lines, mod_lines, kinds):
    """Undo mechanical damage in the modified copy: wherever a modified line is
    exactly what one of the named accidents makes of a line of the original,
    the original line is put back. Real edits never match, so they survive.
    Returns (lines, number restored)."""
    image = {}
    for kind in kinds:
        fn = REPAIRS[kind]
        for l in orig_lines:
            img = fn(l)
            if img != l:
                image.setdefault(img, l)
    fixed = 0
    out = []
    for l in mod_lines:
        if l in image:
            out.append(image[l])
            fixed += 1
        else:
            out.append(l)
    return out, fixed


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def load_manifest(patch_dir):
    with open(os.path.join(patch_dir, MANIFEST), encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(patch_dir, m):
    with open(os.path.join(patch_dir, MANIFEST), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(m, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def diff_name(rel):
    return rel.replace("/", "__") + ".diff"


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
class Row:
    def __init__(self, status, path, note="", skipped=False):
        self.status, self.path, self.note, self.skipped = (status, path, note,
                                                          skipped)


def _short(h):
    return h[:12]


def install_patches(www, patch_dir, check):
    m = load_manifest(patch_dir)
    rows = []
    for e in m["patches"]:
        path = e["path"]
        base = e.get("base", path)
        target = os.path.join(www, path)
        source = os.path.join(www, base)
        diff_path = os.path.join(patch_dir, e["diff"])
        want_o, want_p = e["sha256_original"], e["sha256_patched"]

        if os.path.exists(target):
            tdata = read(target)
            if sha256(tdata) == want_p:
                rows.append(Row("already applied", path))
                continue
        else:
            tdata = None

        if not os.path.exists(source):
            rows.append(Row("missing, skipped", path,
                            "no %s in the tree" % base, skipped=True))
            continue
        sdata = read(source)
        sh = sha256(sdata)
        if sh != want_o:
            # A page that is a second copy of another page (base != path)
            # may find its base already patched in place; the base's .orig
            # backup is then the original. A page patched in place never
            # falls back: a foreign hash there is the user's own revision.
            backup = source + ".orig"
            if (base != path and os.path.exists(backup)
                    and sha256(read(backup)) == want_o):
                sdata = read(backup)
            else:
                rows.append(Row("unexpected version, skipped", path,
                                "expected %s.. found %s.."
                                % (_short(want_o), _short(sh)), skipped=True))
                continue
        try:
            result = apply_diff(normalise(sdata, e.get("newlines", "keep")),
                                read(diff_path))
        except PatchError as ex:
            rows.append(Row("patch failed, skipped", path, str(ex),
                            skipped=True))
            continue
        if sha256(result) != want_p:
            rows.append(Row("result mismatch, skipped", path,
                            "patched bytes do not hash to the manifest value",
                            skipped=True))
            continue
        if check:
            rows.append(Row("would apply", path))
            continue
        if tdata is not None and not os.path.exists(target + ".orig"):
            write(target + ".orig", tdata)
        write(target, result)
        rows.append(Row("applied", path, "backup: %s.orig" % path
                        if tdata is not None else "new file"))
    return rows


def authored_files(authored_dir):
    for dp, _, fs in os.walk(authored_dir):
        for fn in sorted(fs):
            if fn in AUTHORED_SKIP:
                continue
            src = os.path.join(dp, fn)
            rel = os.path.relpath(src, authored_dir).replace(os.sep, "/")
            dest = rel
            if dest.endswith(STORED_SUFFIX):
                stem = dest[:-len(STORED_SUFFIX)]
                if stem.lower().endswith(RENAME_EXT):
                    dest = stem
            yield src, dest


def install_authored(www, authored_dir, check, force):
    rows = []
    if not os.path.isdir(authored_dir):
        return rows
    for src, dest in authored_files(authored_dir):
        target = os.path.join(www, dest)
        data = read(src)
        if os.path.exists(target):
            have = read(target)
            if have == data:
                rows.append(Row("present", dest))
                continue
            if not force:
                rows.append(Row("exists, differs, kept", dest,
                                "use --force to overwrite", skipped=True))
                continue
            if check:
                rows.append(Row("would overwrite", dest))
                continue
            if not os.path.exists(target + ".orig"):
                write(target + ".orig", have)
            write(target, data)
            rows.append(Row("overwritten", dest, "backup: %s.orig" % dest))
            continue
        if check:
            rows.append(Row("would install", dest))
            continue
        write(target, data)
        rows.append(Row("installed", dest))
    return rows


def print_table(rows):
    if not rows:
        print("(nothing to do)")
        return
    w = max(len(r.status) for r in rows)
    for r in rows:
        line = "  %-*s  %s" % (w, r.status, r.path)
        if r.note:
            line += "   (%s)" % r.note
        print(line)


def cmd_install(a):
    www = a.www
    if not os.path.isdir(www):
        print("no such directory: %s" % www)
        return 2
    print("patches (%s):" % ("dry run" if a.check else "apply"))
    rows = install_patches(www, a.patches, a.check)
    print_table(rows)
    print("authored pages:")
    arows = install_authored(www, a.authored, a.check, a.force)
    print_table(arows)
    allrows = rows + arows
    skipped = [r for r in allrows if r.skipped]
    done = len(allrows) - len(skipped)
    print("summary: %d ok, %d skipped" % (done, len(skipped)))
    return 1 if skipped else 0


# --------------------------------------------------------------------------
# maintainer: rebuild the diffs from the development tree
# --------------------------------------------------------------------------
def _dev_paths(dev_www, e):
    d = e["dev"]
    return (os.path.normpath(os.path.join(dev_www, d["original"])),
            os.path.normpath(os.path.join(dev_www, d["modified"])))


def cmd_rebuild(a):
    if not a.dev_www or not os.path.isdir(a.dev_www):
        print("--rebuild needs --dev-www pointing at the development www tree")
        return 2
    m = load_manifest(a.patches)
    problems = 0
    for e in m["patches"]:
        opath, mpath = _dev_paths(a.dev_www, e)
        odata, mdata = read(opath), read(mpath)
        mode = e.get("newlines", "keep")
        old_lines = split_lines(normalise(odata, mode))
        new_lines, fixed = repair(old_lines, split_lines(mdata),
                                  e["dev"].get("repair", []))
        diff = make_diff(old_lines, new_lines, e.get("base", e["path"]),
                         e["path"])
        result = apply_diff(normalise(odata, mode), diff)
        if result != b"".join(new_lines):
            print("[!] %s: round trip failed" % e["path"])
            problems += 1
            continue
        e["diff"] = diff_name(e["path"])
        e["sha256_original"] = sha256(odata)
        e["size_original"] = len(odata)
        e["sha256_patched"] = sha256(result)
        e["size_patched"] = len(result)
        summary, flagged = scan_diff(diff, normalise(odata, mode))
        e["scan"] = summary
        write(os.path.join(a.patches, e["diff"]), diff)
        e["dev"]["restored_lines"] = fixed
        note = "%d hunk(s), +%d -%d" % (summary["hunks"], summary["plus"],
                                        summary["minus"])
        if fixed:
            note += (", %d damaged line(s) restored to the original (%s)"
                     % (fixed, ", ".join(e["dev"]["repair"])))
        print("[*] %s: %s" % (e["path"], note))
        for f in flagged:
            print("    FLAG %s" % f)
            problems += 1
    save_manifest(a.patches, m)
    print("[*] wrote %s (%d flagged hunk(s))" % (os.path.join(a.patches,
                                                               MANIFEST),
                                                  problems))
    return 1 if problems else 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _roundtrip_cases():
    a = b"one\r\ntwo\r\nthree\r\nfour\r\n"
    yield "crlf replace", a, b"one\r\nTWO\r\nthree\r\nfour\r\n"
    yield "insert at top", a, b"zero\r\none\r\ntwo\r\nthree\r\nfour\r\n"
    yield "insert at end", a, a + b"five\r\n"
    yield "delete first", a, b"two\r\nthree\r\nfour\r\n"
    yield "delete last", a, b"one\r\ntwo\r\nthree\r\n"
    yield "delete all", a, b""
    yield "from empty", b"", b"x\ny\n"
    yield "no trailing newline both", b"a\nb\nc", b"a\nB\nc"
    yield "gain trailing newline", b"a\nb", b"a\nb\n"
    yield "lose trailing newline", b"a\nb\n", b"a\nb"
    yield "mixed endings", b"a\r\nb\nc\r\n", b"a\r\nb\r\nc\r\nd"
    yield "two hunks", b"1\n2\n3\n4\n5\n6\n", b"1\nX\n3\n4\nY\nZ\n6\n"
    yield "bom + japanese", "\ufeff<a>\u30c6\u30b9\u30c8</a>\n".encode("utf-8"), \
        "\ufeff<a>test</a>\n<b>\u65b0</b>\n".encode("utf-8")
    yield "shift-jis bytes", b"<!\x83\x65\x83\x58\x83\x67>\r\nx\r\n", \
        b"<!\x83\x65\x83\x58\x83\x67>\r\ny\r\n"
    yield "identical", a, a


def find_dev_www(explicit):
    cands = [explicit, os.environ.get("POL_DEV_WWW"),
             os.path.join(ROOT, "..", "..", "pol-server", "www"),
             os.path.join(ROOT, "..", "pol-server", "www")]
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.normpath(c)
    return None


def _check_patterns():
    """check.py's hygiene patterns, if the repository scanner is present."""
    try:
        sys.path.insert(0, ROOT)
        import check  # noqa: E402
        return check.PATTERNS
    except Exception:
        return []


def cmd_selftest(a):
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print("  %-52s %s%s" % (name, "PASS" if passed else "FAIL",
                                ("  " + detail) if detail else ""))

    print("1. zero-context diff round trip on synthetic files")
    for name, old, new in _roundtrip_cases():
        try:
            d = make_diff(split_lines(old), split_lines(new), "a", "b")
            got = apply_diff(old, d)
            report(name, got == new)
        except PatchError as ex:
            report(name, False, str(ex))
    # a wrong original must be refused, not silently patched
    d = make_diff(split_lines(b"a\nb\nc\n"), split_lines(b"a\nB\nc\n"), "a", "b")
    try:
        apply_diff(b"a\nX\nc\n", d)
        report("mismatching original is refused", False)
    except PatchError:
        report("mismatching original is refused", True)
    try:
        apply_diff(b"a\nb\nc\n", b"@@ -2 +2 @@\n b\n")
        report("context lines are refused", False)
    except PatchError:
        report("context lines are refused", True)

    print("2. shipped diffs parse and are consistent")
    m = load_manifest(a.patches)
    for e in m["patches"]:
        try:
            hunks = parse_diff(read(os.path.join(a.patches, e["diff"])))
            idx = -1
            mono = True
            for h in hunks:
                if h.old_idx < idx:
                    mono = False
                idx = h.old_idx + h.old_count
            report(e["path"], mono and len(hunks) == e["scan"]["hunks"],
                   "%d hunk(s)" % len(hunks))
        except (OSError, PatchError, KeyError) as ex:
            report(e["path"], False, str(ex))

    dev = find_dev_www(a.dev_www)
    if not dev:
        print("3. development tree not present; byte-for-byte check skipped")
        print("RESULT: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    print("3. byte-for-byte against the development tree (%s)" % dev)
    patterns = _check_patterns()
    for e in m["patches"]:
        opath, mpath = _dev_paths(dev, e)
        try:
            odata, mdata = read(opath), read(mpath)
        except OSError as ex:
            report(e["path"], False, str(ex))
            continue
        mode = e.get("newlines", "keep")
        diff = read(os.path.join(a.patches, e["diff"]))
        if sha256(odata) != e["sha256_original"]:
            report(e["path"], False, "original hash drifted")
            continue
        try:
            result = apply_diff(normalise(odata, mode), diff)
        except PatchError as ex:
            report(e["path"], False, str(ex))
            continue
        kinds = e["dev"].get("repair", [])
        if kinds:
            expected, fixed = repair(split_lines(normalise(odata, mode)),
                                     split_lines(mdata), kinds)
            expected = b"".join(expected)
            how = ("byte-for-byte after %d damaged line(s) of the dev copy "
                   "are put back to the original" % fixed)
        else:
            expected = mdata
            how = "byte-for-byte"
        same = result == expected
        hashed = sha256(result) == e["sha256_patched"]
        report(e["path"], same and hashed, how if same else "DIFFERS")
        _summary, flagged = scan_diff(diff, normalise(odata, mode))
        for f in flagged:
            report("  scan " + e["path"], False, f)
        if patterns:
            for h in parse_diff(diff):
                for l in h.plus:
                    text = l.decode("utf-8", "replace")
                    for name, pat in patterns:
                        if pat.search(text):
                            report("  hygiene " + e["path"], False,
                                   "%s in an added line" % name)

    print("4. install into a scratch tree built from the originals")
    with tempfile.TemporaryDirectory() as td:
        www = os.path.join(td, "www")
        for e in m["patches"]:
            opath, _ = _dev_paths(dev, e)
            write(os.path.join(www, e.get("base", e["path"])), read(opath))
        rows = install_patches(www, a.patches, check=True)
        report("dry run touches nothing",
               all(r.status == "would apply" for r in rows)
               and not any(os.path.exists(os.path.join(www, e["path"]))
                           for e in m["patches"] if "base" in e))
        rows = install_patches(www, a.patches, check=False)
        report("first run applies every patch",
               all(r.status == "applied" for r in rows),
               "%d row(s)" % len(rows))
        good = all(sha256(read(os.path.join(www, e["path"])))
                   == e["sha256_patched"] for e in m["patches"])
        report("patched files hash as the manifest says", good)
        rows = install_patches(www, a.patches, check=False)
        report("second run reports already applied",
               all(r.status == "already applied" for r in rows))
        first = m["patches"][0]
        write(os.path.join(www, first["path"]), b"something else\n")
        rows = install_patches(www, a.patches, check=False)
        r0 = [r for r in rows if r.path == first["path"]][0]
        report("a foreign version is skipped with both hashes",
               r0.status == "unexpected version, skipped"
               and "expected" in r0.note)
        arows = install_authored(www, a.authored, check=False, force=False)
        report("authored pages install with the stored suffix removed",
               bool(arows) and all(r.status == "installed" for r in arows)
               and all(not r.path.endswith(STORED_SUFFIX) for r in arows),
               "%d file(s)" % len(arows))
        arows = install_authored(www, a.authored, check=False, force=False)
        report("authored pages are not re-copied",
               all(r.status == "present" for r in arows))
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="apply this project's portal edits to your page tree")
    ap.add_argument("--www", help="root of the portal tree (the directory "
                                  "holding wh000.pol.com/)")
    ap.add_argument("--check", action="store_true",
                    help="report what would happen, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite authored pages that already exist")
    ap.add_argument("--patches", default=PATCH_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--authored", default=AUTHORED_DIR, help=argparse.SUPPRESS)
    ap.add_argument("--selftest", action="store_true",
                    help="verify the tool and the shipped diffs")
    ap.add_argument("--dev-www", default=None,
                    help="development tree for --selftest / --rebuild "
                         "(optional; never needed to apply)")
    ap.add_argument("--rebuild", action="store_true",
                    help="maintainer: regenerate the diffs from --dev-www")
    a = ap.parse_args()
    if a.rebuild:
        return cmd_rebuild(a)
    if a.selftest:
        return cmd_selftest(a)
    if not a.www:
        ap.error("--www is required (or use --selftest)")
    return cmd_install(a)


if __name__ == "__main__":
    sys.exit(main())
