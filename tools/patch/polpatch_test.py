#!/usr/bin/env python3
"""polpatch: build, verify, apply and refuse .polpatch files.

    python tools/patch/polpatch_test.py

Offline. Makes a tiny "installed" client in a temporary directory and checks:

  * build-full -> verify passes -> read_manifest / inspect show it;
  * build-update with one changed and one added file -> verify passes;
  * apply of the full file, then the update, into an empty mirrors directory
    leaves one bundle that polbundle reads with both versions, and
    versions_after() lists what a roll back would remove, oldest first;
  * an update whose base_version is not the server's latest is refused, and
    so is the same update twice, and a full file that would drop a version;
  * tampered files (a changed byte, an unlisted entry, a ../ path, a symlink,
    an absolute path, a backslash path, an oversized declared size, an unknown
    format or kind, a bad product or version) are each refused with a
    sentence, and a refused apply leaves the mirrors directory as it was;
  * from-bundle of the applied bundle installs into a second mirrors
    directory as the same bundle, and installing it again changes nothing;
  * the CLI refuses badly shaped region, product and version arguments;
  * polserver2 serving the applied mirrors on a loopback port passes
    polselfcheck against the install it came from, and the port is closed
    afterwards.
"""
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "..", "services")
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)
import polpatch_lib as lib                                       # noqa: E402
import polbundle                                                 # noqa: E402

CLI = os.path.join(HERE, "polpatch.py")
SELFCHECK = os.path.join(HERE, "polselfcheck.py")
V1, V2, V3 = "20030429_0", "20040714_0", "20050101_0"

failures = []


def check(ok, what):
    print(("  ok    " if ok else "  FAIL  ") + what)
    if not ok:
        failures.append(what)


def write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def snapshot(root):
    out = {}
    for dirpath, _, names in os.walk(root):
        for nm in names:
            full = os.path.join(dirpath, nm)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def cli(*args):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def raises(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except lib.PolpatchError as e:
        return str(e)
    return None


def retamper(src, dst, entries=None, manifest=None, extra=()):
    """Copy a .polpatch, changing entry bytes, the manifest, or adding entries.

    entries: {name: new bytes}; manifest: function(dict) -> dict;
    extra: [(name, bytes, external_attr or None)]."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == lib.MANIFEST_NAME and manifest:
                data = json.dumps(manifest(json.loads(data))).encode()
            elif entries and info.filename in entries:
                data = entries[info.filename]
            zout.writestr(info, data)
        for name, data, attr in extra:
            zi = zipfile.ZipInfo("placeholder")
            zi.filename = name      # as given: ZipInfo() would rewrite a backslash
            zi.orig_filename = name
            if attr is not None:
                zi.create_system = 3
                zi.external_attr = attr
            zout.writestr(zi, data)


def serve_and_selfcheck(mirrors, bundle, reference):
    """polserver2 on an ephemeral loopback port, then polselfcheck against it."""
    import polserver2
    bundles = polserver2.load_bundles(mirrors)
    srv = polserver2.Server(bundles, "127.0.0.1", False)
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.bind(("127.0.0.1", 0))
    ls.listen(8)
    port = ls.getsockname()[1]
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, addr = ls.accept()
            except OSError:
                return
            threading.Thread(target=srv.handle, args=(conn, addr), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        p = subprocess.run([sys.executable, SELFCHECK, "--bundle", bundle,
                            "--port", str(port), "--reference", reference],
                           capture_output=True, text=True, timeout=120)
    finally:
        stop.set()
        ls.close()
        t.join(5)
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        still_open = True
    except OSError:
        still_open = False
    return p.returncode, p.stdout + p.stderr, still_open


def main():
    with tempfile.TemporaryDirectory() as tmp:
        install = os.path.join(tmp, "install")
        write(install, "data/a.bin", bytes(range(256)) * 8)
        write(install, "data/b.txt", b"the base build\n" * 40)
        write(install, "readme.txt", b"base\n")
        out = os.path.join(tmp, "out")
        full = os.path.join(out, "full.polpatch")
        up = os.path.join(out, "update.polpatch")

        print("build-full")
        rc, txt = cli("build-full", "--source", install, "--walk", "--region", "W2U",
                      "--product", "0002", "--version", V1, "-o", full)
        check(rc == 0 and os.path.isfile(full), "build-full writes the file")
        check(lib.verify(full) == [], "the full file verifies")
        m = lib.read_manifest(full)
        check(m["kind"] == "full" and m["versions"] == [V1] and m["base_version"] is None
              and m["title"] == "Tetra Master (Windows, North America and Europe)",
              "read_manifest gives kind, versions and title")
        check(set(m["files"]) == {"bundle/meta.json", "bundle/patch.cfg",
                                  "bundle/patchlist.raw",
                                  f"bundle/blobs/{V1}/Direct/data/a.bin.slc",
                                  f"bundle/blobs/{V1}/Direct/data/b.txt.slc",
                                  f"bundle/blobs/{V1}/Direct/readme.txt.slc"},
              "the full file holds the list files and one blob per file")
        with zipfile.ZipFile(full) as zf:
            meta = json.loads(zf.read("bundle/meta.json"))
        check(meta["synthesised"]["source"] == "install",
              "meta.json keeps only the folder name of the source")
        rc, txt = cli("inspect", full)
        check(rc == 0 and "W2U/0002" in txt and V1 in txt, "inspect prints the summary")
        rc, txt = cli("verify", full)
        check(rc == 0 and "[PASS]" in txt, "verify on the command line passes")

        print("build-update")
        ov = os.path.join(tmp, "overlay")
        write(ov, "data/b.txt", b"the second build\n" * 40)
        write(ov, "extra/new.txt", b"a file the base did not have\n")
        rc, txt = cli("build-update", "--base", full, "--files", ov,
                      "--version", V2, "-o", up)
        check(rc == 0, "build-update from a full .polpatch")
        check(lib.verify(up) == [], "the update verifies")
        mu = lib.read_manifest(up)
        check(mu["kind"] == "update" and mu["versions"] == [V2] and mu["base_version"] == V1,
              "the update names its version and base_version")
        blobs = sorted(n for n in mu["files"] if "/blobs/" in n)
        check(blobs == [f"bundle/blobs/{V2}/Direct/data/b.txt.slc",
                        f"bundle/blobs/{V2}/Direct/extra/new.txt.slc"],
              "the update carries only the changed and added files' blobs")

        print("apply")
        mirrors = os.path.join(tmp, "mirrors")
        os.makedirs(mirrors)
        r = lib.apply(full, mirrors)
        check(r["action"] == "installed", "apply of the full file installs it")
        bundle = os.path.join(mirrors, "W2U-0002")
        r = lib.apply(up, mirrors)
        check(r["action"] == "updated" and r["backup"]
              and os.path.isfile(os.path.join(r["backup"], "patch.cfg")),
              "apply of the update merges it and backs up the old list files")
        rows = polbundle.manifest(bundle)
        check(sorted({x["version"] for x in rows}) == [V1, V2],
              "polbundle.manifest reads both versions")
        check(any(x["path"] == "extra/new.txt" and x["version"] == V2 for x in rows)
              and any(x["path"] == "data/b.txt" and x["version"] == V1 for x in rows),
              "the added file is there and the old row of the changed file is kept")
        info = lib.bundle_info(bundle)
        check(info["versions"] == [V1, V2] and info["latest"] == V2,
              "bundle_info lists versions oldest first and latest last")
        check(lib.versions_after([V2, V3, V1], V1) == [V2, V3]
              and lib.versions_after(info["versions"], V2) == [],
              "versions_after lists the newer versions, oldest first")
        check(not [n for n in os.listdir(mirrors) if n.startswith(".polpatch-stage")],
              "no staging folder is left behind")

        print("refusals on apply")
        before = snapshot(mirrors)
        msg = raises(lib.apply, up, mirrors)
        check(msg is not None and "already has" in msg, f"the same update twice: {msg}")
        stale = os.path.join(out, "stale.polpatch")
        ov3 = os.path.join(tmp, "overlay3")
        write(ov3, "readme.txt", b"third\n")
        rc, txt = cli("build-update", "--base", full, "--files", ov3,
                      "--version", V3, "-o", stale)
        check(rc == 0 and lib.verify(stale) == [], "an update built on the old base verifies alone")
        msg = raises(lib.apply, stale, mirrors)
        check(msg is not None and f"is at {V2}" in msg and f"built on {V1}" in msg,
              f"an update on the wrong base_version is refused: {msg}")
        probs = lib.verify(stale, against=bundle)
        check(probs and "built on" in probs[0], "verify --against says the same")
        msg = raises(lib.apply, full, mirrors)
        check(msg is not None and "drop" in msg, f"a full file that loses a version: {msg}")
        check(snapshot(mirrors) == before, "refused applies changed nothing")

        print("tampered files")
        blob = f"bundle/blobs/{V1}/Direct/readme.txt.slc"
        with zipfile.ZipFile(full) as zf:
            orig = zf.read(blob)

        cases = [
            ("a changed byte", dict(entries={blob: orig[:-1] + bytes([orig[-1] ^ 1])}),
             "sha256"),
            ("a blob whose sha256 was updated to match a changed byte",
             dict(entries={blob: orig[:-1] + bytes([orig[-1] ^ 1])},
                  manifest=lambda m: (m["files"][blob].update(sha256=hashlib.sha256(
                      orig[:-1] + bytes([orig[-1] ^ 1])).hexdigest()), m)[1]),
             "does not match patch.cfg"),
            ("an entry the manifest does not list",
             dict(extra=[("bundle/blobs/%s/Direct/x.slc" % V1, b"\x01x", None)]),
             "does not list"),
            ("a ../ entry", dict(extra=[("bundle/../evil.txt", b"x", None)]), "'..'"),
            ("a symlink entry",
             dict(extra=[("bundle/link", b"/etc/passwd", (stat.S_IFLNK | 0o777) << 16)]),
             "symlink"),
            ("an absolute path", dict(extra=[("/etc/evil", b"x", None)]), "absolute"),
            # On Windows zipfile reads a backslash as "/", so this one is caught
            # as '..'; elsewhere it is caught as a backslash. Either is a refusal.
            ("a backslash path", dict(extra=[("bundle\\..\\evil.txt", b"x", None)]), "path"),
            ("an entry outside bundle/", dict(extra=[("evil.txt", b"x", None)]), "outside"),
            ("an oversized declared size",
             dict(manifest=lambda m: (m["files"][blob].update(size=1 << 40), m)[1]),
             "per-file limit"),
            ("an unknown format", dict(manifest=lambda m: dict(m, format=2)), "format"),
            ("an unknown kind", dict(manifest=lambda m: dict(m, kind="delta")), "kind"),
            ("an unknown field", dict(manifest=lambda m: dict(m, url="x")), "does not define"),
            ("an unknown product", dict(manifest=lambda m: dict(m, product="0099")), "0099"),
            ("a bad version", dict(manifest=lambda m: dict(m, versions=["2003_0"])),
             "YYYYMMDD_X"),
            ("a lowercase region", dict(manifest=lambda m: dict(m, region="w2u")),
             "region code"),
            ("a version the bundle does not carry",
             dict(manifest=lambda m: dict(m, versions=[V2])), "latest_version"),
        ]
        for i, (what, kw, needle) in enumerate(cases):
            bad = os.path.join(out, f"bad{i}.polpatch")
            retamper(full, bad, **kw)
            probs = lib.verify(bad)
            check(bool(probs) and needle in probs[0],
                  f"{what} is refused: {probs[0] if probs else 'NOT REFUSED'}")
        bad = os.path.join(out, "bad0.polpatch")
        empty = os.path.join(tmp, "mirrors-empty")
        msg = raises(lib.apply, bad, empty)
        check(msg is not None and "sha256" in msg, "apply refuses the changed byte too")
        check(os.listdir(empty) == [], "and leaves the mirrors folder empty")
        probs = lib.verify(full, limits={"max_total": 1000})
        check(bool(probs) and "limit" in probs[0], f"the total size cap is configurable: {probs[:1]}")
        probs = lib.verify(full, limits={"max_entries": 3})
        check(bool(probs) and "limit" in probs[0], "the entry count cap is configurable")
        notzip = os.path.join(out, "notzip.polpatch")
        write(out, "notzip.polpatch", b"hello")
        msg = raises(lib.read_manifest, notzip)
        check(msg is not None and "not a readable ZIP" in msg, f"a non-zip: {msg}")
        plain = os.path.join(out, "plain.zip")
        with zipfile.ZipFile(plain, "w") as zf:
            zf.writestr("readme.txt", "x")
        msg = raises(lib.read_manifest, plain)
        check(msg is not None and "no polpatch.json" in msg, f"a zip with no manifest: {msg}")

        print("from-bundle")
        again = os.path.join(out, "again.polpatch")
        rc, txt = cli("from-bundle", "--bundle", bundle, "-o", again)
        check(rc == 0 and lib.verify(again) == [], "from-bundle writes a file that verifies")
        ma = lib.read_manifest(again)
        check(ma["kind"] == "full" and ma["versions"] == [V1, V2], "it carries both versions")
        mirrors2 = os.path.join(tmp, "mirrors2")
        r = lib.apply(again, mirrors2)
        check(r["action"] == "installed", "it installs into a new mirrors folder")
        check(snapshot(os.path.join(mirrors2, "W2U-0002")) == snapshot(bundle),
              "the round trip gives the same bundle byte for byte")
        r = lib.apply(again, mirrors2)
        check(r["action"] == "unchanged", "installing it again changes nothing")
        r = lib.apply(again, mirrors)
        check(r["action"] == "unchanged", "nor onto the bundle it was packed from")

        print("command line formats")
        for args, needle in ((["--region", "w2u", "--product", "0002"], "region code"),
                             (["--region", "W2U", "--product", "2"], "product code"),
                             (["--region", "W2U", "--product", "0002", "--version",
                               "2003_0"], "YYYYMMDD_X")):
            rc, txt = cli("build-full", "--source", install, "--walk", *args,
                          "-o", os.path.join(out, "x.polpatch"))
            check(rc == 2 and needle in txt, f"build-full {' '.join(args)} is refused")
        wiz = os.path.join(tmp, "wizard")
        os.makedirs(wiz)
        # product 3 (0002), region 1 (W2U), take every file, default folder,
        # then the version, since the fake install has no patch.ver
        p = subprocess.run([sys.executable, CLI, "wizard", install, "--out-dir", wiz],
                           input="3\n1\ny\n\n2003_0\n" + V1 + "\n",
                           capture_output=True, text=True)
        made = os.path.join(wiz, f"W2U-0002-{V1}.polpatch")
        check(p.returncode == 0 and os.path.isfile(made) and lib.verify(made) == []
              and os.listdir(wiz) == [os.path.basename(made)],
              "the wizard (what polpatch.cmd runs) asks, builds and names the file"
              + ("" if p.returncode == 0 else "\n" + p.stdout + p.stderr))
        rc, txt = cli("verify", os.path.join(out, "bad0.polpatch"))
        check(rc == 1 and "[FAIL]" in txt, "verify exits 1 on a bad file")
        rc, txt = cli("apply", stale, "--mirrors", mirrors)
        check(rc == 1 and "import the updates in order" in txt,
              "apply on the command line exits 1 with the sentence")

        print("served")
        reference = os.path.join(tmp, "reference")
        shutil.copytree(install, reference)
        shutil.copytree(ov, reference, dirs_exist_ok=True)
        rc, txt, still_open = serve_and_selfcheck(mirrors, bundle, reference)
        check(rc == 0 and "[PASS]" in txt, "polselfcheck passes on the applied bundle"
              + ("" if rc == 0 else "\n" + txt))
        check(not still_open, "the test server's port is closed afterwards")

    if failures:
        print(f"FAIL: {len(failures)} check(s)")
        return 1
    print("polpatch: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
