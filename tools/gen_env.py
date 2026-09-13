"""Generate a patched env.dat from config/server.yaml.

DNS already redirects every pol.com name to the stub, so for local observation
you usually do NOT need to touch env.dat at all -- the shipped file works as-is
once DNS points the client here. This tool exists for the cases DNS can't cover
by itself:

  * changing a value the client reads directly (e.g. a URL path, POL_LANG),
  * production, where you want env.dat to name real backend hosts rather than
    relying on a wildcard DNS zone.

It reads the shipped env.dat, applies `env.overrides` from server.yaml (plus any
--set on the command line), re-encrypts with the recovered file cipher, and
verifies the round trip. A file that reports `verify: ok` is one the client's
loader (size % 8 == 0, correct trailer checksum) will accept.

    python tools/gen_env.py SOURCE_ENV_DAT -o out/env.dat
    python tools/gen_env.py SOURCE_ENV_DAT -o out/env.dat --set POL_LANG=1
    python tools/gen_env.py SOURCE_ENV_DAT --dump
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The recovered env.dat cipher, VENDORED from the RE tree's
# worker-out/polfilecrypt.py. It was imported across the repo boundary, which
# stopped working when this became a standalone repo. It is a solved, frozen
# codec; if the RE side ever revises it, copy the file across deliberately.
sys.path.insert(0, HERE)
from polfilecrypt import decrypt, encrypt, find_key, KEY_ENV  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

CONFIG_PATH = os.path.join(HERE, "..", "config", "server.yaml")


def load_overrides():
    if yaml is None or not os.path.isfile(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return dict((cfg.get("env") or {}).get("overrides") or {})


def parse(payload):
    out = []
    for line in payload.decode("shift_jis", "replace").split("\r\n"):
        if not line:
            continue
        k, _, v = line.partition(",")
        out.append((k, v))
    return out


def render(pairs):
    return "".join(f"{k},{v}\r\n" for k, v in pairs).encode("shift_jis")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="path to a shipped env.dat")
    ap.add_argument("-o", "--out")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--set", action="append", default=[], dest="sets",
                    metavar="KEY=VALUE")
    a = ap.parse_args(argv)

    raw = open(a.source, "rb").read()
    hit = find_key(raw)
    key = hit[1] if hit else KEY_ENV
    payload, ok = decrypt(raw, key)
    print(f"{a.source}: {len(raw)} bytes -> {len(payload)} payload, "
          f"key={hit[0] if hit else 'env (assumed)'}, checksum_ok={ok}")
    pairs = parse(payload)

    if a.dump:
        for k, v in pairs:
            print(f"  {k},{v}")
        return 0

    overrides = load_overrides()
    overrides.update(dict(s.split("=", 1) for s in a.sets))
    if not overrides:
        print("  (no overrides in config or --set; env.dat unchanged -- "
              "rely on DNS)")

    applied, seen = [], set()
    for i, (k, v) in enumerate(pairs):
        if k in overrides:
            pairs[i] = (k, overrides[k])
            applied.append((k, v, overrides[k]))
            seen.add(k)
    for k, v in overrides.items():
        if k not in seen:
            pairs.append((k, v))
            applied.append((k, "<absent>", v))
    for k, old, new in applied:
        print(f"  {k}: {old} -> {new}")

    new_payload = render(pairs)
    out = encrypt(new_payload, key)
    back, ok2 = decrypt(out, key)
    print(f"  new payload {len(new_payload)} bytes -> {len(out)} on disk; "
          f"verify: {'ok' if ok2 and back == new_payload else 'FAILED'}")

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "wb") as f:
            f.write(out)
        print(f"  wrote {a.out}")
    else:
        print("  (no -o given, nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
