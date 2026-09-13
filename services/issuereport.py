#!/usr/bin/env python3
"""issuereport.py -- a tester presses one key; we keep BOTH halves of the story.

WHY THIS EXISTS. Every bug on this project has been diagnosed by someone going
and fetching a log after the fact, and by then half of it is gone: the client's
`polshim.<pid>.log` is overwritten by the next launch, and the server's channel
logs (`lobby.log` is 7 MB and climbing) have rolled past the moment that
mattered. So the report that reaches us is "it crashed when I zoned", which is
the start of an investigation rather than the end of one.

The mechanism here is the other way round. The tester hits the report chord the
moment it happens; the shim ships its own logs, its ini and a screenshot; and
THIS module, on arrival, immediately cuts the SERVER's own logs to the same time
window and files both halves under one id. What lands is a self-contained
directory that still means something a week later, when everything it was cut
from has rolled.

  logs/issues/20260906T203145Z-STEAMDECK-a3f1/
      manifest.json          who, when, what they typed, what we matched on
      client/description.txt  the tester's own words
      client/polshim.log      their log, snapshotted at the keypress
      client/shot.png
      client/polshim.ini
      server/lobby.log        the SAME MINUTES, cut from ours
      server/fmo.log
      server/correlated.log   every channel, interleaved, filtered to them

WARNING: THE PEER IP DOES NOT IDENTIFY A CLIENT ON PROD, and the correlation is built
around that. Everything arrives through the docker gateway, so every client is
`172.18.0.1` -- responders.py says so in as many words next to the User-Agent
gate ("both reach us from the same docker gateway address, so the peer IP cannot
tell them apart"). A correlation keyed on the peer address alone would look like
it worked and would quietly be selecting every other tester as well. So we
correlate on a LIST of tokens -- host name, handle, POL id, peer -- and the
manifest records which of them actually matched, and how many lines each found.
A token that matched nothing is reported as matching nothing rather than being
silently dropped: "we could not tell which lines were yours" is a finding.

WHAT THIS MODULE DOES NOT DO. It does not parse, classify or triage. It is a
recorder. Reading the bundle is a person's job, and the value is
entirely in the two halves being the same minutes -- which nothing else here
has ever managed to be.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import shutil
import time

from srvcore import LOG_DIR, log

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Where bundles land. Under LOG_DIR so it shares the volume every other log is
#: already on -- the box is 16 GiB, hence the caps below being real rather than
#: nominal.
#:
#: WARNING: `POL_ISSUE_*`, NOT `POL_REPORT_*`, AND THE DIRECTORY IS `issues/`. Every
#: name here was `POL_REPORT_DIR` / `logs/reports/` in the first draft, which
#: collides head-on with something that already exists: `admin.py:88` reads
#: `POL_REPORT_DIR` (default `/data/reports`) for the VIEWER'S ABUSE REPORTS --
#: a completely different thing, filed by SE's own "Report User" dialog over
#: SMTP. Two features reading one variable and meaning different directories is
#: the exact shape of the FFXI id-map split (memory: ffxi-idmap-path-split),
#: where a writer and a reader named different files for days with no visible
#: symptom. Setting the variable for either feature would have silently moved
#: the other one. Renamed before it shipped; do not rename it back.
ISSUE_DIR = os.environ.get("POL_ISSUE_DIR", os.path.join(LOG_DIR, "issues"))

#: Whole-POST cap. Bigger than the 8 MB shim-log cap because a bundle carries a
#: screenshot as well; the shim is expected to cap its own log tail well under.
ISSUE_MAX = int(os.environ.get("POL_ISSUE_MAX", str(16 * 1024 * 1024)))

#: How far BACK from the report to cut the server logs. The tester presses the
#: key after noticing, not when it started, so this leads the event -- ten
#: minutes covers a zone-in, a login, or a sortie from its first byte.
WINDOW_BEFORE = int(os.environ.get("POL_ISSUE_WINDOW_BEFORE", "600"))

#: And forward, for anything still unrolling as they typed.
WINDOW_AFTER = int(os.environ.get("POL_ISSUE_WINDOW_AFTER", "60"))

#: Per-channel cut cap. `lobby.log` at full tilt can put megabytes into ten
#: minutes; past this we truncate and SAY SO in the manifest (a silently short
#: log reads as a quiet server, which is the wrong conclusion to hand someone).
CUT_MAX = int(os.environ.get("POL_ISSUE_CUT_MAX", str(2 * 1024 * 1024)))

#: Retention: newest N bundles, and a total byte ceiling. Whichever bites first.
KEEP_N = int(os.environ.get("POL_ISSUE_KEEP", "200"))
KEEP_BYTES = int(os.environ.get("POL_ISSUE_KEEP_BYTES", str(512 * 1024 * 1024)))

#: Channels never worth cutting into a bundle. `shim-*.log` is a client's OWN
#: live stream -- the bundle already carries that client's log in full, and the
#: other testers' streams are not ours to file under one person's report.
SKIP_LOGS = ("shim-",)


# --------------------------------------------------------------------------- #
# The bundle wire format
# --------------------------------------------------------------------------- #
#
# LENGTH-PREFIXED, NOT SEPARATOR-SCANNED, and that is the whole reason it is a
# format at all rather than a few `====` lines. The payload is LOGS: whatever
# separator you pick, a log can contain it -- and the one time it does is the
# time somebody pasted a report bundle into a log, i.e. exactly the confusing
# case. A byte count cannot be spoofed by content, and it carries the PNG
# without base64 or escaping.
#
#     ==== POLSHIM-REPORT 1 ====\n
#     host: STEAMDECK\n
#     handle: example\n
#     ...\n
#     \n
#     ==== FILE description.txt 63 ====\n
#     <exactly 63 bytes>\n
#     ==== FILE polshim.log 148213 ====\n
#     <exactly 148213 bytes>\n
#
# The header block is single-line scalars ONLY. The tester's text is multi-line
# by nature and rides as `description.txt`, a file like any other -- a free-text
# header would need quoting rules and would be the first thing to break on a
# newline in someone's bug report.

BUNDLE_MAGIC = b"==== POLSHIM-REPORT 1 ===="
_FILE_RX = re.compile(rb"^==== FILE ([A-Za-z0-9._-]{1,64}) (\d{1,9}) ====$")

#: Header keys we file into the manifest. Anything else the client sends is kept
#: under `extra` rather than dropped -- a shim newer than this server must not
#: lose the field it was built to send.
KNOWN_KEYS = ("host", "pid", "handle", "polid", "title", "content_id",
              "shim_build", "client_build", "os", "display", "category",
              "when", "session")


class BundleError(ValueError):
    """A malformed bundle. Carries the reason; the caller logs it."""


def parse_bundle(body: bytes):
    """(meta_dict, [(name, bytes), ...]) or raise BundleError.

    Strict on purpose. This is one of two endpoints on the server that takes
    bulk client-supplied bytes, and the other one (`_shim_log_store`) learned
    the same lesson: names come from a whitelist, never from the body as given.
    """
    if not body.startswith(BUNDLE_MAGIC):
        raise BundleError("not a report bundle (bad magic)")
    pos = len(BUNDLE_MAGIC)
    if body[pos:pos + 1] == b"\n":
        pos += 1

    meta, extra = {}, {}
    while pos < len(body):
        nl = body.find(b"\n", pos)
        if nl < 0:
            raise BundleError("header block is unterminated")
        line = body[pos:nl]
        pos = nl + 1
        if not line.strip():
            break                       # blank line ends the header block
        if line.startswith(b"==== FILE "):
            pos -= len(line) + 1        # no header block at all; rewind
            break
        k, _, v = line.partition(b":")
        key = k.strip().decode("latin1", "replace").lower()
        val = v.strip().decode("utf-8", "replace")[:200]
        if not key:
            continue
        (meta if key in KNOWN_KEYS else extra)[key] = val
    if extra:
        meta["extra"] = extra

    files = []
    seen = set()
    while pos < len(body):
        nl = body.find(b"\n", pos)
        if nl < 0:
            break
        m = _FILE_RX.match(body[pos:nl])
        if not m:
            raise BundleError(f"expected a FILE header at byte {pos}")
        name = m.group(1).decode("ascii")
        want = int(m.group(2))
        pos = nl + 1
        if pos + want > len(body):
            raise BundleError(f"{name} claims {want}B, only "
                              f"{len(body) - pos}B remain")
        blob = body[pos:pos + want]
        pos += want
        if body[pos:pos + 1] == b"\n":
            pos += 1
        # A DUPLICATE NAME IS NOT AN OVERWRITE. Two files called polshim.log
        # would silently become one, and the one you lost is the one you wanted.
        if name in seen:
            name = f"dup-{len(files)}-{name}"
        seen.add(name)
        files.append((name, blob))
    if not files:
        raise BundleError("bundle carries no files")
    return meta, files


# --------------------------------------------------------------------------- #
# Cutting our own logs to the window
# --------------------------------------------------------------------------- #

#: Every channel log line starts with this -- `log()` in srvcore builds it, and
#: it is the one thing all of them share. Fractional seconds are present on some
#: channels and absent on others (compare lobby.log with fmo.log), hence the
#: optional group; a strptime with a fixed format silently matches neither.
_STAMP_RX = re.compile(
    rb"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")


def _stamp_of(line: bytes):
    """Epoch seconds for a log line, or None if it carries no stamp.

    Unstamped lines are real and must not be treated as an error: a traceback in
    `stderr.log` is one stamped line followed by a dozen bare ones, and dropping
    those would file the exception without its stack."""
    m = _STAMP_RX.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    base = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s),
                            0, 1, -1))
    return base + (float("0." + frac.decode()) if frac else 0.0)


def _seek_window(f, size: int, t0: float) -> int:
    """Byte offset of the first line at or after t0, by bisection.

    WHY BISECT rather than tail-and-filter. A report cuts ~12 channels; several
    are megabytes. Scanning each one costs tens of megabytes of reads per
    report, on a box that is also serving the game the tester is still playing.
    A bisection touches a few kilobytes per channel and the logs are already
    sorted by the key we are searching -- so this is the cheap direction, not
    the clever one."""
    lo, hi = 0, size
    while lo < hi:
        mid = (lo + hi) // 2
        f.seek(mid)
        if mid:
            f.readline()                # discard the partial line we landed in
        ts = None
        for _ in range(64):             # step over unstamped continuation lines
            ln = f.readline()
            if not ln:
                break
            ts = _stamp_of(ln)
            if ts is not None:
                break
        # No stamp found ahead of mid means the tail is all continuations; treat
        # it as "at or after", which errs toward INCLUDING lines. Losing context
        # is the expensive mistake here; a few extra lines is not.
        if ts is None or ts >= t0:
            hi = mid
        else:
            lo = mid + 1
    return lo


def cut_window(path: str, t0: float, t1: float, cap: int = CUT_MAX):
    """(bytes, truncated) -- the lines of `path` inside [t0, t1]."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return b"", False
    if not size:
        return b"", False
    out, total, truncated = [], 0, False
    try:
        with open(path, "rb") as f:
            start = _seek_window(f, size, t0)
            f.seek(start)
            if start:
                f.readline()            # we may be mid-line after the bisect
            inside = False
            for line in f:
                ts = _stamp_of(line)
                if ts is None:
                    # A continuation belongs to the line above it, so it is in
                    # the window exactly when that line was.
                    if not inside:
                        continue
                else:
                    if ts > t1:
                        break
                    inside = ts >= t0
                    if not inside:
                        continue
                out.append(line)
                total += len(line)
                if total >= cap:
                    truncated = True
                    break
    except OSError:
        return b"", False
    return b"".join(out), truncated


def _channel_logs():
    """Direct-child `*.log` files of LOG_DIR, minus the ones SKIP_LOGS names.

    Direct children only -- `logs/captures/` holds binary packet dumps and
    `logs/uploads/` holds other people's shipped logs; neither is this client's
    story and both are large."""
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError:
        return []
    out = []
    for n in names:
        if not n.endswith(".log"):
            continue
        if any(n.startswith(p) for p in SKIP_LOGS):
            continue
        p = os.path.join(LOG_DIR, n)
        if os.path.isfile(p):
            out.append((n, p))
    return out


def _tokens(meta: dict, peer: str):
    """The strings worth grepping the window for, longest first.

    WARNING: THE PEER IS LAST AND IS EXPECTED TO BE USELESS ON PROD -- see the module
    banner. It is included because it IS discriminating on a LAN deployment and
    on the PS2 path, and because a token that matches everything is visible as
    such in the manifest's per-token counts, where a missing one is not."""
    toks = []
    for key in ("handle", "polid", "host"):
        v = (meta.get(key) or "").strip()
        if len(v) >= 3:
            toks.append((key, v))
    ip = (peer or "").rsplit(":", 1)[0].strip("[]")
    if ip:
        toks.append(("peer", ip))
    return toks


def capture_server_window(t0: float, t1: float, meta: dict, peer: str):
    """(files, notes) -- our own logs, cut to the report's window.

    `files` is name -> bytes; `notes` goes verbatim into the manifest so the
    person reading the bundle can see what was cut, what was truncated, and --
    the part that matters -- what the correlation actually matched on."""
    files, cuts = {}, []
    for name, path in _channel_logs():
        blob, truncated = cut_window(path, t0, t1)
        if not blob:
            continue
        files[name] = blob
        cuts.append({"channel": name, "bytes": len(blob),
                     "lines": blob.count(b"\n"), "truncated": truncated})

    # THE CROSS-CHANNEL VIEW. One file, every channel, sorted back into time
    # order and filtered to this client. This is the thing you actually read
    # first: a login problem is a conversation between authserv, lobby and ucs,
    # and reading it as three files means reconstructing the interleaving by
    # hand -- which is the step everyone skips and then misdiagnoses.
    toks = _tokens(meta, peer)

    # Count every token FIRST, then select with the best tier that actually
    # matched. See `_tokens`: `peer` is a real discriminator on a LAN or PS2
    # deployment and is worth nothing on prod, where the docker gateway makes
    # every client 172.18.0.1 -- so a correlation that simply ORs the tokens
    # together produces, on the deployment that matters most, a file labelled
    # "this client's lines" containing every tester's. That is worse than an
    # empty file: it reads as evidence.
    hits = {k: 0 for k, _ in toks}
    per_tok = {k: [] for k, _ in toks}
    for name, blob in files.items():
        tag = name[:-4].encode() if name.endswith(".log") else name.encode()
        for line in blob.splitlines(keepends=True):
            for k, v in toks:
                if v.encode("utf-8", "replace") in line:
                    hits[k] += 1
                    per_tok[k].append((_stamp_of(line) or 0.0, tag, line))
    strong = [k for k, _ in toks if k != "peer" and hits[k]]
    if strong:
        seen, picked = set(), []
        for k in strong:
            for row in per_tok[k]:
                if id(row[2]) not in seen:      # a line matched by two tokens
                    seen.add(id(row[2]))        # is still one line
                    picked.append(row)
        used = strong
    else:
        picked = list(per_tok.get("peer", []))
        used = ["peer"] if picked else []
    picked.sort(key=lambda r: r[0])
    if picked:
        files["correlated.log"] = b"".join(
            b"%-10s %s" % (t, ln) for _, t, ln in picked)

    notes = {
        "window_from": _iso(t0),
        "window_to": _iso(t1),
        "cuts": cuts,
        "correlation": [{"key": k, "value": v, "lines": hits[k],
                         "used": k in used} for k, v in toks],
        "correlated_by": used,
        "correlated_lines": len(picked),
    }
    # THE CASE THAT MUST NOT BE READ AS EVIDENCE, spelled out in the manifest
    # rather than left to be inferred from `correlated_by`.
    if used == ["peer"]:
        notes["correlation_warning"] = (
            f"correlated on the PEER ADDRESS ONLY ({dict(toks).get('peer')}). "
            "On a docker deployment every client shares the gateway address, so "
            "these lines may belong to other testers as well as this one. "
            "Nothing matched the handle or host name.")
    # An honest negative. If nothing matched, the bundle still has the window --
    # but the reader must not spend an hour assuming `correlated.log` is empty
    # because the client was quiet.
    if toks and not picked:
        notes["correlation_warning"] = (
            "no server line matched any client token -- the window is here, but "
            "which lines are THIS client's could not be determined")
    elif not toks:
        notes["correlation_warning"] = (
            "the client sent no host/handle/polid, so nothing could be "
            "correlated; only the raw window is filed")
    return files, notes


# --------------------------------------------------------------------------- #
# Filing
# --------------------------------------------------------------------------- #

def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _tag(v: str, default: str, cap: int = 40) -> str:
    """One filename-safe component. Same rule as `_shim_tag` in responders."""
    v = re.sub(r"[^A-Za-z0-9._-]", "_", (v or "")[:cap])
    return v or default


def new_id(meta: dict, now: float) -> str:
    """`<stamp>-<host>-<rand>`. The stamp leads so that a lexical sort of the
    directory is a chronological one -- which is what `prune` relies on, and
    what makes `ls` useful without a tool."""
    return "%s-%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                         _tag(meta.get("host"), "unknown", 24),
                         secrets.token_hex(2))


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def prune(keep_n: int = KEEP_N, keep_bytes: int = KEEP_BYTES):
    """Drop the oldest bundles past either cap. Returns the ids removed.

    The box is 16 GiB and shares that volume with every channel log, so this is
    not decorative: an unbounded report directory is a way to take the server
    down with a feature meant to keep it up."""
    try:
        ids = sorted(d for d in os.listdir(ISSUE_DIR)
                     if os.path.isdir(os.path.join(ISSUE_DIR, d)))
    except OSError:
        return []
    dropped = []
    while len(ids) > keep_n:
        dropped.append(ids.pop(0))
    sizes = {i: _dir_bytes(os.path.join(ISSUE_DIR, i)) for i in ids}
    total = sum(sizes.values())
    while ids and total > keep_bytes:
        gone = ids.pop(0)
        total -= sizes.get(gone, 0)
        dropped.append(gone)
    for d in dropped:
        try:
            shutil.rmtree(os.path.join(ISSUE_DIR, d))
        except OSError:
            pass
    return dropped


def store(body: bytes, hdrs: dict, peer: str):
    """Land one report bundle. Returns (status line, report id).

    THE ID GOES BACK TO THE CLIENT, and that is not decoration: the shim shows
    it in the "thanks, sent" box, so a tester can paste it into chat and the
    operator can open exactly that bundle instead of guessing from a host name
    and a rough time. The id is empty on every failure path.

    NEVER RAISES into the request handler. This runs on the client-facing HTTP
    door that also serves the portal; a traceback here would drop the connection
    of a client whose only crime was telling us something was wrong.
    """
    if not body:
        return b"400 Bad Request", ""
    if len(body) > ISSUE_MAX:
        log("lobby", f"{peer}   report REFUSED: {len(body)}B exceeds "
                     f"{ISSUE_MAX}B (POL_ISSUE_MAX)")
        return b"413 Payload Too Large", ""
    try:
        meta, files = parse_bundle(body)
    except BundleError as e:
        log("lobby", f"{peer}   report REFUSED: {e}")
        return b"400 Bad Request", ""

    now = time.time()
    # THE CLIENT'S CLOCK DOES NOT SET THE WINDOW. A Steam Deck that has been
    # asleep can be minutes out, and a window cut around a wrong `when` selects
    # the wrong minutes of our logs while looking perfectly well-formed. We use
    # OUR receipt time and record the client's claim beside it, so a skew is
    # visible in the manifest instead of being baked invisibly into the cut.
    client_when = (meta.get("when") or "").strip()
    rid = new_id(meta, now)
    dest = os.path.join(ISSUE_DIR, rid)

    try:
        os.makedirs(os.path.join(dest, "client"), exist_ok=True)
        os.makedirs(os.path.join(dest, "server"), exist_ok=True)
        for name, blob in files:
            with open(os.path.join(dest, "client", name), "wb") as f:
                f.write(blob)
        srv, notes = capture_server_window(now - WINDOW_BEFORE,
                                           now + WINDOW_AFTER, meta, peer)
        for name, blob in srv.items():
            with open(os.path.join(dest, "server", name), "wb") as f:
                f.write(blob)
    except OSError as e:
        log("lobby", f"{peer}   report {rid} could not be stored ({e})")
        return b"500 Internal Server Error", ""

    desc = ""
    for name, blob in files:
        if name == "description.txt":
            desc = blob.decode("utf-8", "replace").strip()
            break

    manifest = {
        "id": rid,
        "received_at": _iso(now),
        "client_when": client_when,
        "peer": peer,
        "description": desc,
        "client_files": [{"name": n, "bytes": len(b)} for n, b in files],
        "server_files": sorted(srv),
        "window": notes,
    }
    manifest.update({k: v for k, v in meta.items() if k != "when"})
    try:
        with open(os.path.join(dest, "manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log("lobby", f"{peer}   report {rid} manifest failed ({e})")

    # LOUD, AND IN THE LOG SOMEBODY IS ALREADY READING. The whole point is that
    # a report is noticed; a file appearing silently in a directory is not a
    # notification. First line of the tester's own text goes here verbatim --
    # that one line is usually enough to know whether to stop what you are doing.
    first = (desc.splitlines() or [""])[0][:160] or "(no description)"
    log("lobby", f"{peer}   ISSUE REPORT {rid} from "
                 f"{meta.get('host') or '?'}/{meta.get('handle') or '?'} "
                 f"[{meta.get('title') or '?'}]: {first}")
    log("lobby", f"{peer}   report {rid}: {len(files)} client file(s), "
                 f"{len(srv)} server cut(s), "
                 f"{notes.get('correlated_lines', 0)} correlated line(s)")
    if notes.get("correlation_warning"):
        log("lobby", f"{peer}   report {rid}: {notes['correlation_warning']}")

    for gone in prune():
        log("lobby", f"   report retention dropped {gone}")
    return b"200 OK", rid


# --------------------------------------------------------------------------- #
# Reading them back (the admin API and tools/reportbundle.py both use these)
# --------------------------------------------------------------------------- #

def list_reports(limit: int = 500):
    """Newest first. A torn manifest must not hide the rest of the list."""
    try:
        ids = sorted((d for d in os.listdir(ISSUE_DIR)
                      if os.path.isdir(os.path.join(ISSUE_DIR, d))),
                     reverse=True)
    except OSError:
        return []
    out = []
    for rid in ids[:limit]:
        out.append(read_manifest(rid) or {
            "id": rid, "description": "(manifest unreadable)"})
    return out


def read_manifest(rid: str):
    p = os.path.join(ISSUE_DIR, _safe_id(rid), "manifest.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _safe_id(rid: str) -> str:
    """A report id is a PATH COMPONENT supplied by whoever is browsing. Same
    rule as everywhere else: sanitise to the alphabet, never normalise."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", (rid or "")[:96]) or "_"


def read_file(rid: str, rel: str):
    """One file out of a bundle, or None. `rel` is `client/x` or `server/y`."""
    parts = (rel or "").split("/")
    if len(parts) != 2 or parts[0] not in ("client", "server"):
        return None
    p = os.path.join(ISSUE_DIR, _safe_id(rid), parts[0], _safe_id(parts[1]))
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Self-test -- registered in tools/run_all.py
# --------------------------------------------------------------------------- #

def _selftest():
    import tempfile
    ok = [0, 0]

    def check(name, cond):
        ok[1] += 1
        if cond:
            ok[0] += 1
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name}")

    # -- the wire format ----------------------------------------------------- #
    def build(meta, files):
        out = [BUNDLE_MAGIC, b"\n"]
        for k, v in meta.items():
            out.append(f"{k}: {v}\n".encode())
        out.append(b"\n")
        for n, b in files:
            out.append(b"==== FILE %s %d ====\n" % (n.encode(), len(b)))
            out.append(b + b"\n")
        return b"".join(out)

    raw = build({"host": "DECK", "handle": "cas", "nonsense": "keep me"},
                [("description.txt", "it broke\nwhen I zoned".encode()),
                 ("polshim.log", b"[a] one\n[b] two\n")])
    meta, files = parse_bundle(raw)
    check("header scalars parse", meta["host"] == "DECK")
    check("unknown header kept as extra",
          meta.get("extra", {}).get("nonsense") == "keep me")
    check("file count", len(files) == 2)
    check("multi-line description survives",
          files[0][1].decode() == "it broke\nwhen I zoned")

    # THE PROPERTY THE FORMAT EXISTS FOR: content that contains the separator.
    evil = BUNDLE_MAGIC + b"\n==== FILE fake.log 5 ====\nXXXXX\n"
    raw2 = build({"host": "D"}, [("polshim.log", evil)])
    _m2, f2 = parse_bundle(raw2)
    check("a log containing the separator stays ONE file", len(f2) == 1)
    check("...and is byte-identical", f2[0][1] == evil)

    check("bad magic refused",
          _raises(lambda: parse_bundle(b"hello")))
    check("short file refused",
          _raises(lambda: parse_bundle(
              BUNDLE_MAGIC + b"\n\n==== FILE a.log 99 ====\nshort\n")))
    check("no files refused",
          _raises(lambda: parse_bundle(BUNDLE_MAGIC + b"\nhost: x\n\n")))
    dupe = build({}, [("a.log", b"1\n"), ("a.log", b"2\n")])
    _m3, f3 = parse_bundle(dupe)
    check("duplicate name does not overwrite",
          len(f3) == 2 and f3[1][0] != f3[0][0])

    # -- the timestamp reader ------------------------------------------------ #
    t_frac = _stamp_of(b"2026-08-18T07:36:32.791071Z [lobby] x")
    t_bare = _stamp_of(b"2026-08-17T14:32:22Z [fmo] y")
    check("fractional stamp parses", t_frac is not None)
    check("BARE stamp parses too (fmo.log has no fraction)", t_bare is not None)
    # Pinned against `datetime`, NOT against a constant typed in by hand. The
    # first version of this check used a literal epoch and it was simply wrong
    # by 31,536,000 -- a whole year -- which made a CORRECT parser look broken.
    # The stamp is UTC by construction (`time.gmtime` writes it), so the
    # independent oracle is a tz-aware datetime, and it costs one line.
    import datetime as _dt
    want = _dt.datetime(2026, 8, 18, 7, 36, 32, 791071,
                        tzinfo=_dt.timezone.utc).timestamp()
    check("stamp is UTC epoch (vs datetime)",
          t_frac is not None and abs(t_frac - want) < 1e-6)
    check("unstamped line yields None", _stamp_of(b"    frame 3 ...") is None)

    # -- the window cut ------------------------------------------------------ #
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.log")
        base = 1757000000
        with open(p, "wb") as f:
            for i in range(4000):
                f.write(("%s [t] line %d\n" % (_iso(base + i), i)).encode())
                if i == 2500:
                    f.write(b"    a continuation with no stamp\n")
        blob, trunc = cut_window(p, base + 2400, base + 2600)
        lines = blob.decode().splitlines()
        check("cut is not truncated", not trunc)
        check("cut starts at the window", lines[0].endswith("line 2400"))
        check("cut ends at the window", lines[-1].endswith("line 2600"))
        check("cut length is exact", len(lines) == 202)  # 201 + continuation
        check("continuation line is CARRIED, not dropped",
              any("continuation" in l for l in lines))

        # The bisection must land on the same answer as a brute scan, including
        # at the edges -- an off-by-one here silently clips the first second of
        # the window, which is where a login problem starts.
        for t0 in (base - 5, base, base + 1, base + 3999, base + 5000):
            got, _ = cut_window(p, t0, t0 + 2)
            want = b"".join(
                ln + b"\n" for ln in open(p, "rb").read().splitlines()
                if (_stamp_of(ln) is not None
                    and t0 <= _stamp_of(ln) <= t0 + 2))
            check(f"bisect matches brute scan at t0+{t0 - base}", got == want)

        blob, trunc = cut_window(p, base, base + 4000, cap=500)
        check("cap truncates and SAYS so", trunc and len(blob) <= 600)

    # -- correlation --------------------------------------------------------- #
    files_in = {"a.log": b"2026-01-01T00:00:01Z [a] hello DECK here\n"
                         b"2026-01-01T00:00:02Z [a] someone else\n",
                "b.log": b"2026-01-01T00:00:00Z [b] DECK first\n"}
    saved = globals()["_channel_logs"]
    globals()["_channel_logs"] = lambda: []
    try:
        _f, notes = capture_server_window(0, 1, {"host": "DECK"}, "1.2.3.4:5")
        check("no channels -> honest warning",
              "correlation_warning" in notes)
    finally:
        globals()["_channel_logs"] = saved

    with tempfile.TemporaryDirectory() as td:
        for n, b in files_in.items():
            with open(os.path.join(td, n), "wb") as f:
                f.write(b)
        saved_dir = globals()["LOG_DIR"]
        globals()["LOG_DIR"] = td
        try:
            got, notes = capture_server_window(0, 4102444800,
                                               {"host": "DECK"}, "1.2.3.4:5")
            check("both channels cut", "a.log" in got and "b.log" in got)
            check("correlated.log built", "correlated.log" in got)
            corr = got["correlated.log"].decode()
            check("correlation selects only matching lines",
                  "someone else" not in corr)
            check("correlation is TIME-ordered across channels",
                  corr.index("b") < corr.index("hello"))
            check("per-token hit counts recorded",
                  any(c["key"] == "host" and c["lines"] == 2
                      for c in notes["correlation"]))

            # THE PROD TRAP. Every client is 172.18.0.1 behind the docker
            # gateway, so a peer match must never be allowed to stand in for a
            # handle match -- and when it is all we have, it must say so.
            with open(os.path.join(td, "c.log"), "wb") as f:
                f.write(b"2026-01-01T00:00:03Z [c] 172.18.0.1 someone ELSE\n"
                        b"2026-01-01T00:00:04Z [c] 172.18.0.1 DECK too\n")
            got2, n2 = capture_server_window(0, 4102444800, {"host": "DECK"},
                                             "172.18.0.1:41000")
            check("a strong token WINS over the peer",
                  n2["correlated_by"] == ["host"])
            check("...so another tester's line is NOT filed as ours",
                  b"someone ELSE" not in got2["correlated.log"])
            check("no peer-only warning when a strong token matched",
                  "correlation_warning" not in n2)

            got3, n3 = capture_server_window(0, 4102444800, {"host": "NOSUCH"},
                                             "172.18.0.1:41000")
            check("peer-only fallback still returns the lines",
                  n3["correlated_by"] == ["peer"] and n3["correlated_lines"] == 2)
            check("peer-only fallback WARNS that it may not be this client",
                  "PEER ADDRESS ONLY" in n3.get("correlation_warning", ""))
        finally:
            globals()["LOG_DIR"] = saved_dir

    # -- retention ----------------------------------------------------------- #
    with tempfile.TemporaryDirectory() as td:
        saved_rd = globals()["ISSUE_DIR"]
        globals()["ISSUE_DIR"] = td
        try:
            for i in range(5):
                d = os.path.join(td, "2026090%dT000000Z-H-aaaa" % i)
                os.makedirs(d)
                with open(os.path.join(d, "x"), "wb") as f:
                    f.write(b"." * 1000)
            dropped = prune(keep_n=3, keep_bytes=1 << 30)
            check("count cap drops the OLDEST", dropped ==
                  ["20260900T000000Z-H-aaaa", "20260901T000000Z-H-aaaa"])
            dropped = prune(keep_n=99, keep_bytes=1500)
            check("byte cap drops until under", len(dropped) == 2)
            check("newest survives both caps",
                  os.path.isdir(os.path.join(td, "20260904T000000Z-H-aaaa")))
        finally:
            globals()["ISSUE_DIR"] = saved_rd

    # -- path safety --------------------------------------------------------- #
    check("id traversal neutralised", "/" not in _safe_id("../../etc"))
    check("read_file refuses a bare name", read_file("x", "manifest.json") is None)
    check("read_file refuses traversal",
          read_file("x", "client/../../../etc/passwd") is None)

    print(f"\nissuereport: {ok[0]}/{ok[1]} checks passed")
    return 0 if ok[0] == ok[1] else 1


def _raises(fn):
    try:
        fn()
    except BundleError:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
