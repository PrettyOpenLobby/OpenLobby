#!/usr/bin/env python3
"""polpublish --revert: --dry-run changes nothing, a real revert removes ALL.

    python tools/patch/polpublish_test.py

Offline: builds a three-file "installed" tree in a temporary directory, makes
a bundle from it with polsynth.py, publishes two more versions on top with
polpublish.py, and then checks:

  * `--revert --dry-run` names both published versions and leaves every file
    in the bundle byte-for-byte as it was;
  * a real `--revert` (the twin, so the dry-run check is not passing on a
    revert that does nothing anyway) removes both versions and returns the
    bundle to exactly the state polsynth.py wrote.
"""
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SYNTH = os.path.join(HERE, "polsynth.py")
PUBLISH = os.path.join(HERE, "polpublish.py")


def run(script, *args):
    p = subprocess.run([sys.executable, script, *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"{os.path.basename(script)} {' '.join(args)} failed:\n"
                         f"{p.stdout}{p.stderr}")
    return p.stdout


def snapshot(root):
    """relpath -> sha256 of every file under root."""
    out = {}
    for dirpath, _, names in os.walk(root):
        for nm in names:
            full = os.path.join(dirpath, nm)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def main():
    failures = []

    def check(ok, what):
        print(("  ok    " if ok else "  FAIL  ") + what)
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        install = os.path.join(tmp, "install")
        bundle = os.path.join(tmp, "mirrors", "W2U-0002")
        write(install, "data/a.bin", bytes(range(256)) * 4)
        write(install, "data/b.txt", b"hello from the base build\n" * 20)
        write(install, "readme.txt", b"base\n")
        run(SYNTH, "--source", install, "--out", bundle, "--walk",
            "--region", "W2U", "--product", "0002", "--version", "20030429_0")
        base = snapshot(bundle)

        ov1 = os.path.join(tmp, "ov1")
        write(ov1, "data/b.txt", b"second build\n" * 20)
        run(PUBLISH, "--bundle", bundle, "--version", "20040714_0", "--overlay", ov1)
        ov2 = os.path.join(tmp, "ov2")
        write(ov2, "readme.txt", b"third build\n")
        run(PUBLISH, "--bundle", bundle, "--version", "20050101_0", "--overlay", ov2)
        published = snapshot(bundle)
        check(published != base, "two publishes changed the bundle")

        out = run(PUBLISH, "--bundle", bundle, "--revert", "--dry-run")
        check(snapshot(bundle) == published,
              "--revert --dry-run left every file unchanged")
        check("20040714_0" in out and "20050101_0" in out,
              "--revert --dry-run names both published versions")
        check("nothing changed" in out, "--revert --dry-run says nothing changed")

        out = run(PUBLISH, "--bundle", bundle, "--revert")
        after = snapshot(bundle)
        check(after != published, "a real --revert changes the bundle")
        check(after == base,
              "a real --revert removes BOTH versions, back to the polsynth state")

    if failures:
        print(f"FAIL: {len(failures)} check(s)")
        return 1
    print("polpublish revert: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
