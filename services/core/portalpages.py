"""Portal eras: which client build gets which pages; shim logs."""
import os
import re
import time
import clientbuilds
from srvcore import LOG_DIR, log
from .deps import yaml
from . import portalauth



# --------------------------------------------------------------------------- #
# PORTAL ERAS -- which client build gets which pages
#
# PlayOnline's portal UI was rebuilt once, and BOTH platforms moved together: the
# split is by era, not by PC-vs-console. A 2004 Viewer handed a 2013 page asks for
# URLs built from variables it does not have, so whole regions of the page come up
# empty and navigation can land in the other UI generation. config/portal-eras.yaml
# carries the rules and the evidence; this is the machinery.
# --------------------------------------------------------------------------- #
ERAS_PATH = os.environ.get("POL_ERAS_CONFIG", "/config/portal-eras.yaml")
BUILDS_PATH = os.environ.get("POL_VIEWER_BUILDS", "/config/viewer-builds.tsv")
_ERA_CACHE = {"mtime": None, "eras": None, "builds": None}


def _release_tuple(rel):
    """`Ver.1.17.04c` / `1.11.00(d50)` -> (1, 17, 4). None if not a version.

    Only the three numeric components order releases; the trailing letter is a
    respin of the same release and never crosses an era boundary. `goodbye!`
    (the shutdown build) has no number and deliberately returns None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", rel or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _release_key(rel):
    """`Ver.1.17.04c` -> (1, 17, 4, 'c'). Orders releases INCLUDING the respin.

    The trailing letter is ignorable when dating a build (_release_tuple) but not
    when placing one against an era boundary: the PC switched on 20061212_3,
    which IS 1.17.04c, so 1.17.04b and 1.17.04c fall on opposite sides of it and
    a numeric-only compare would hand the older respin the newer page set.

    Anchored, so the shim's own tag cannot be mistaken for the client's version:
    the header reads `Ver.1.18.15e [PoL-Shim v0.1.0 b24]`, and an unanchored
    search would happily match `0.1.0` if the first field were ever missing.
    `1.11.00(d50)` parses as (1, 11, 0, '') -- the parenthesised build is not a
    respin letter."""
    m = re.match(r"\s*(?:Ver\.)?(\d+)\.(\d+)\.(\d+)\s*([A-Za-z]?)", rel or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups()[:3]) + (m.group(4).lower(),)


def _load_eras():
    """Era rules + the build->release table, reloaded when either file changes."""
    try:
        stamp = (os.path.getmtime(ERAS_PATH), os.path.getmtime(BUILDS_PATH))
    except OSError:
        return None, {}
    if _ERA_CACHE["mtime"] != stamp:
        eras = None
        try:
            with open(ERAS_PATH, encoding="utf-8") as f:
                eras = yaml.safe_load(f) if yaml else None
        except (OSError, ValueError):
            eras = None
        builds = {}
        try:
            with open(BUILDS_PATH, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) == 3:
                        builds[(parts[0], parts[1])] = parts[2]
        except OSError:
            pass
        _ERA_CACHE.update(mtime=stamp, eras=eras, builds=builds)
    return _ERA_CACHE["eras"], _ERA_CACHE["builds"]


def _release_for_build(builds, service, build):
    """Release string for a build id, tolerating builds SE never archived.

    Necessary, not defensive: the live PS2 here reports `20040428_5`, and that
    build is NOT in any tree -- SE's PS2 rows jump 20020516_4 -> 20040706_2. An
    exact-match-only lookup therefore fell through to the default era for the one
    client the era machinery exists for.

    A build id is a DATE (`YYYYMMDD_x`), so an unarchived build is dated by the
    newest archived build at or before it: whatever release was current when it
    shipped. Returns (release, exact)."""
    exact = builds.get((service, build))
    # `goodbye!` -- the shutdown build -- carries no version number, so it dates
    # itself by its neighbours like an unarchived build rather than falling all
    # the way through to the default era.
    if exact is not None and _release_tuple(exact):
        return exact, True
    older = [(b, r) for (s, b), r in builds.items()
             if s == service and b <= build and _release_tuple(r)]
    if older:
        return max(older)[1], False
    same = [(b, r) for (s, b), r in builds.items() if s == service]
    return (min(same)[1], False) if same else (None, False)


def _era_for_build(eras, service, build):
    """Which era a build falls in: PER SERVICE, by build id.

    Deliberately not a release-number rule. The platforms did NOT move together
    -- the PS2 switched at 20041207_3 (2004-12-07) and the PC not until
    20061212_3 (2006-12-12), measured from env.dat
    deltas. A global version cutoff would put two
    years of PS2 builds in the wrong era. Build ids sort as dates, so an
    unarchived build compares correctly without being in any table.

    None when the service has no measured boundary (the Xbox trees), so the
    caller falls back to default_era rather than to a guess."""
    boundary = ((eras or {}).get("boundaries") or {}).get(service)
    if not boundary:
        return None
    ids = [e.get("id") for e in (eras or {}).get("eras", [])]
    if len(ids) != 2:
        return None
    older, newer = ids
    return newer if build >= boundary else older


#: A well-formed build id, `YYYYMMDD_x`. Anything else in a version claim is
#: UNKNOWN, and unknown must never be treated as a value -- see _client_era.
_BUILD_ID = re.compile(r"\d{8}_[0-9A-Za-z]\Z")

#: Last era verdict logged per User-Agent, so the log carries every FLIP and not
#: one line per asset fetch. Keyed by UA because that is as close to a client
#: identity as a portal request gets: the peer address is shared by all of them.
_ERA_LAST = {}


def _era_for_release(eras, builds, services, release):
    """Era from the release the client states in its X-POL-VIEWER-VERSION header.

    THIS IS THE ONLY PER-REQUEST ERA SIGNAL, and it is on every portal request:

        X-POL-VIEWER-VERSION: Ver.1.18.15e [PoL-Shim v0.1.0 b24]

    verified 2026-08-16 in logs/captures for all three live clients (the PC, the
    Steam Deck, the PS2 -- the console sends it too, as `Ver.1.18.15f`). The old
    comment that "the portal request carries no version" was true only of the
    User-Agent; this header sits two lines below it and was never read.

    Preferring it over the reported build is not an optimisation, it is the fix
    for a whole class of wrong-era serving: the build comes from a table keyed by
    PEER ADDRESS, and every client here shares one (172.18.0.1 via the Docker
    bridge), so three Windows Viewers -- the Deck, this PC, the 2003 era test --
    all write one `W2U/1000` slot and the last check-in decides the era for all
    of them. The header cannot be contaminated: it arrives on the same connection
    as the request it decides.

    The boundary is still a BUILD (the client-era boundary above), so it is mapped
    through the build table to a release and compared as (major, minor, patch,
    respin). The respin letter is load bearing here -- the PC boundary 20061212_3
    IS 1.17.04c -- which is why this uses _release_key and not _release_tuple.

    Returns (era, why) or (None, None) when the header is absent, unparseable, or
    the platform has no measured boundary; the caller then falls back to the
    reported build, which is what a client too old to send the header needs."""
    want = _release_key(release)
    if not want:
        return None, None
    ids = [e.get("id") for e in (eras or {}).get("eras", [])]
    if len(ids) != 2:
        return None, None
    older, newer = ids
    for service in services or ():
        boundary = ((eras or {}).get("boundaries") or {}).get(service)
        if not boundary:
            continue
        bound_rel, _ = _release_for_build(builds, service, boundary)
        edge = _release_key(bound_rel)
        if not edge:
            continue
        era = newer if want >= edge else older
        return era, (f"{service} {release.strip()} (X-POL-VIEWER-VERSION), "
                     f"boundary {boundary} = {bound_rel}")
    return None, None


#: UA platform -> the services that platform can possibly be. The User-Agent
#: carries no version, but it DOES state the platform, and that is enough to say
#: which of an address's build claims could have come from this client.
_UA_SERVICES = (
    # `Play Station 2` and `PlayStation 2` are both observed, hence the loose match.
    ("playstation 2", ("PS2", "P2U")),
    ("play station 2", ("PS2", "P2U")),
    ("windows",       ("W2U", "W20")),
    ("xbox",          ("X2U", "XB2")),
)


def _ua_services(ua):
    """Which service families the User-Agent's platform allows, or () if unknown."""
    low = (ua or "").lower()
    for needle, services in _UA_SERVICES:
        if needle in low:
            return services
    return ()


def _client_era(peer_ip, ua=None, viewer_version=None):
    """The era of a client: override, else its OWN version header, else its build.

    In that order, and the middle one is the one that is actually right. The
    header (`X-POL-VIEWER-VERSION`, see _era_for_release) rides the request being
    decided, so it describes THIS client. The build comes from the POLP patch
    check (polserver2.py records it) keyed by peer address, and an address here
    is not a client.

    `peer_ip` DOES NOT IDENTIFY A CLIENT, and assuming it did served the PS2's
    page set to the PC for as long as an era directory had anything in it. Every
    client here arrives through the Docker bridge as 172.18.0.1, so one address
    accumulates every machine's claims -- PS2/1000 and W2U/1000 side by side -- and
    a plain `sorted()` scan handed all of them whichever sorted first (PS2). The
    symptom is the worst kind: a page that IS served, from the wrong era, at 200.

    So the User-Agent's PLATFORM narrows the candidates. It carries no version,
    but `(Windows XP)` vs `(PlayStation 2)` is enough to say which claims could
    possibly be this client's. A platform we recognise with no claim of its own
    takes `default_era` rather than borrowing another platform's build -- silence
    about a PC is not evidence that the PC is a console."""
    eras, builds = _load_eras()
    if not eras:
        return None, "no era config"
    override = (eras.get("overrides") or {}).get(peer_ip)
    if override:
        return override, f"override for {peer_ip}"
    allowed = _ua_services(ua)
    if eras.get("trust_version_header", True):
        era, why = _era_for_release(eras, builds, allowed, viewer_version)
        if era:
            return era, why
    state_here = clientbuilds.for_address(peer_ip)
    for key, rec in sorted(state_here.items()):
        # Product 1000 is the Viewer itself; a title's build says nothing about
        # which portal UI the shell renders.
        service, _, product = key.partition("/")
        if product != "1000":
            continue
        if allowed and service not in allowed:
            continue
        build = rec.get("version", "")
        # AN EMPTY OR MALFORMED CLAIM IS UNKNOWN, NOT OLD. Clients do send one:
        # `cmd7 W2U/1000 ver=''` appears in the patch log, and because the era
        # test is a plain string compare, `'' >= '20061212_3'` is False and the
        # empty claim read as the OLDEST era. That is the whole bug behind
        # 31 `era panel (W2U  = 1.10.00c)` decisions in lobby.log -- note the
        # double space where the build should be. Skip it and let a later claim,
        # or default_era, answer; never let a non-answer vote for panel.
        if not _BUILD_ID.match(build):
            continue
        era = _era_for_build(eras, service, build)
        if era:
            # The release string is for the human reading the log; the decision
            # is made on the build id alone.
            release, exact = _release_for_build(builds, service, build)
            named = f" = {release}" + ("" if exact else " (nearest archived)")
            return era, f"{service} {build}{named if release else ''}"
    # Four different silences, and telling them apart matters when reading a log:
    # a client that never checked in, one whose service has no measured boundary
    # (the Xbox trees), one whose ADDRESS reported builds that belong to a
    # different platform (the shared-bridge case above), and one whose claim was
    # not a build id at all.
    reported = [k for k in state_here if k.endswith("/1000")]
    mine = [k for k in reported if not allowed or k.partition("/")[0] in allowed]
    junk = [k for k in mine
            if not _BUILD_ID.match((state_here.get(k) or {}).get("version", ""))]
    if reported and not mine:
        why = (f"default ({', '.join(reported)} reported at {peer_ip}, but none is "
               f"a {'/'.join(allowed)} build -- that is another client on this address)")
    elif junk and len(junk) == len(mine):
        why = (f"default ({', '.join(junk)} claimed "
               f"{', '.join(repr((state_here.get(k) or {}).get('version', '')) for k in junk)}"
               f", which is not a build id)")
    elif mine:
        why = (f"default ({', '.join(mine)} reported, but that service has no "
               f"measured era boundary)")
    else:
        why = "default (client reported no Viewer build)"
    return eras.get("default_era"), why


def _ua_lang(ua):
    """The language tag in `PlayOnline-PML-Viewer/1.00 [en] (Windows XP)`.

    The console sends the same shape, so this is the one dimension the
    User-Agent DOES settle. Short codes are widened to the full tag the page
    tree is keyed by.

    **The tag tracks the VIEWER BUILD's region, not the platform.** The JP
    Viewer sends `PlayOnline-PML-Viewer/1.00 [jp] (Play Station 2)` and the US
    Viewer sends `[en] (PlayStation 2)`, so a US console resolves to
    `_lang/en-US` exactly like a PC does, and an English console page IS
    reachable and only has to be authored.

    **`jp` is not a language tag at all**, so without the mapping below it fell
    through unmapped and every JP-Viewer lookup went to a `_lang/jp/` that can
    never exist. Note `$_LANG` on that same console reports `ja`; the two
    spellings are not consistent even within one client."""
    m = re.search(r"\[([A-Za-z-]{2,5})\]", ua or "")
    if not m:
        return None
    tag = m.group(1)
    return {"ja": "ja-JP", "jp": "ja-JP", "en": "en-US",
            "de": "de-DE", "fr": "fr-FR"}.get(tag, tag)


def _portal_roots(www, era, lang):
    """Document roots, most specific first. See config/portal-eras.yaml.

    Falling off the end lands on www/<host>/ -- today's tree -- so a client whose
    era has no directory behaves exactly as it did before any of this existed."""
    roots = []
    if era:
        if lang:
            roots.append((os.path.join(www, "_eras", era, lang), f"_eras/{era}/{lang}"))
        roots.append((os.path.join(www, "_eras", era), f"_eras/{era}"))
    if lang:
        roots.append((os.path.join(www, "_lang", lang), f"_lang/{lang}"))
    roots.append((www, "www"))
    return roots


def _portal_challenge(peer, port):
    """A 401 x-MD5-pol Digest challenge (realm POL), matching SE's Apache. Sent
    when POL_PORTAL_AUTH=1 and the request has no Authorization, so the client
    performs the mutual-auth handshake the body-load gate expects."""
    nonce = portalauth._portal_md5(os.urandom(16))
    chal = (b'WWW-Authenticate: Digest realm="POL", nonce="' + nonce.encode()
            + b'", algorithm="x-MD5-pol", domain="/pml/", qop="auth"\r\n')
    return (b"HTTP/1.1 401 Authorization Required\r\n" + chal
            + b"Content-Length: 0\r\nConnection: keep-alive\r\n\r\n")


#: Where `[logship]` POSTs a client log, and where those land. The path is
#: deliberately under a `_`-prefixed name no portal page uses, so it can never
#: collide with a real SE URL -- see `no-page-redirect-workarounds`: we do not
#: invent routes SE shipped, and this one is ours by construction.
_SHIM_LOG_PATH = os.environ.get("POL_SHIM_LOG_PATH", "_shim/log")

#: Where the REPORT CHORD posts. Same door and the same `_`-prefixed reasoning
#: as the log path above -- it is ours by construction and can never collide
#: with an SE URL. Separate from `_shim/log` on purpose: a log upload is one
#: file and is routine, a report is a multi-file bundle that triggers a cut of
#: OUR logs as well, and giving them one path would mean sniffing the body to
#: tell them apart.
_REPORT_PATH = os.environ.get("POL_ISSUE_PATH", "_shim/report")
_SHIM_LOG_DIR = os.path.join(LOG_DIR, "uploads")
_SHIM_LOG_MAX = int(os.environ.get("POL_SHIM_LOG_MAX", str(8 * 1024 * 1024)))

#: LIVE MODE. A shim with `[logship] live=1` POSTs the same path with
#: `X-Shim-Mode: live` every few seconds, each body being the NEW lines of its
#: log since the last 200. Those are APPENDED to `shim-<host>.log` -- in
#: LOG_DIR itself, not uploads/, deliberately: logtail serves only direct
#: children of the log dir, so landing there makes a client's session
#: tail-able over HTTP (and by `tail -f` on the box) with no other change.
#: One file per machine; rotated once to `shim-<host>.1.log` at the cap.
_SHIM_LIVE_MAX = int(os.environ.get("POL_SHIM_LIVE_MAX", str(16 * 1024 * 1024)))
_shim_live_sessions = {}   # filename -> last X-Shim-Session seen. In-memory on
                           # purpose: a service restart merely repeats one
                           # session-separator line in the file.

#: SERVER-SIDE REDACTION, and it is a SAFETY NET, not the mechanism. The shim
#: redacts before it sends, so credentials never leave the client machine; this
#: catches a client that is older than the redaction, or misconfigured, or a log
#: uploaded by hand. Belt and braces, because these files get ARCHIVED -- a
#: credential that lands here is a credential that survives in a capture
#: directory for months.
#:
#: What actually appears in these logs, measured rather than imagined:
#:   authserv.log   cred='VcKnl_lApOB'   the NICK line's credential field
#:   authserv.log   NICK U... :<32 hex>:<token>   the per-handle login token
#:   polshim log    the auth stamp and the 300-token it is keyed from
_SHIM_LOG_REDACT = (
    (re.compile(rb"(cred=')[^']{4,}(')"), rb"\1<redacted>\2"),
    (re.compile(rb"(NICK\s+U[A-Z0-9]+:)[0-9a-fA-F]{8,}(:)"), rb"\1<redacted>\2"),
    (re.compile(rb"(?m)^(PASS\s+)\S+"), rb"\1<redacted>"),
    # WARNING: AN ASSIGNMENT, NOT A BARE SPACE. This was `password["'=: ]{1,3}` and it
    # ate ENGLISH: the first real upload (2026-08-17) carried
    #     [autologin] ... (EMPTY -- set [autologin] polid/password to arm it)
    # and the tail of that sentence was redacted as though it were a secret.
    # Damaging a diagnostic is a real cost, and a redactor that cries wolf gets
    # switched off -- so require `=` or `:` and let a bare mention through.
    (re.compile(rb"(password\s*[=:]\s*)\S+", re.I), rb"\1<redacted>"),
    # THE BLOWFISH SESSION KEY, IV AND S-BOX. Added 2026-08-24 alongside the client
    # half in pol-shim/src/logship.cpp -- these two lists are meant to stay in step,
    # and they were both short by exactly this line for as long as [auth] log existed.
    #
    # The client emits (authkey.cpp, and again in probes.cpp):
    #     [auth] polcryptInit K=<hex> IV=<hex> sbox=<hex> ctx=0x........
    # `[auth] log` shipped ON by default, so any install with [logship] live=1 was
    # streaming its session key here in the clear on every login. The client default
    # is 0 now and the client redacts too; this is the safety net for a log shipped by
    # an older build or uploaded by hand, which is exactly what it exists for.
    #
    # WARNING: Anchored on `polcryptInit ` and not on a bare `K=`. The client's matcher is a
    # substring scan and a bare "K=" there would eat `Direct3DCreate8(SDK=%u)`; the
    # same trap applies to a loose regex here. Same lesson as the `password` note
    # above -- a redactor that damages diagnostics gets switched off.
    (re.compile(rb"(polcryptInit K=)\S+(\s+IV=)\S+(\s+sbox=)\S+"),
     rb"\1<redacted>\2<redacted>\3<redacted>"),
)


def _shim_tag(hdrs, name, default):
    """One sanitised header component. Everything a filename is built from goes
    through here: [A-Za-z0-9._-] only, length-capped, never empty."""
    v = hdrs.get(name, b"").decode("latin1", "replace")[:40]
    v = re.sub(r"[^A-Za-z0-9._-]", "_", v)
    return v or default


def _shim_live_append(body, hdrs, peer):
    """Append one live increment to logs/shim-<host>.log.

    The shim ships whole lines and only advances past bytes we 200'd, so simply
    appending each accepted body reconstructs its log exactly -- no offsets to
    track here. A session separator is written whenever X-Shim-Session changes,
    so two launches read as two sessions in one file. Quiet on purpose: at one
    POST per client every few seconds, per-chunk log lines would bury the lobby
    log; the arrival of a NEW session is the only event worth a line."""
    host = _shim_tag(hdrs, b"x-shim-host", "unknown")
    session = _shim_tag(hdrs, b"x-shim-session", "0")
    name = f"shim-{host}.log"
    for rx, repl in _SHIM_LOG_REDACT:
        body, _n = rx.subn(repl, body)
    path = os.path.join(LOG_DIR, name)
    try:
        # One-deep rotation. 16 MB of live log is days of testing; the point is
        # a bounded disk footprint, not an archive -- the full log still exists
        # on the client and crash-ships in full.
        try:
            if os.path.getsize(path) + len(body) > _SHIM_LIVE_MAX:
                os.replace(path, os.path.join(LOG_DIR, f"shim-{host}.1.log"))
                _shim_live_sessions.pop(name, None)   # re-announce in the new file
        except OSError:
            pass
        with open(path, "ab") as f:
            if _shim_live_sessions.get(name) != session:
                _shim_live_sessions[name] = session
                stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
                f.write(f"\n==== {stamp}  live session {session} "
                        f"from {host} ({peer}) ====\n".encode())
                log("lobby", f"{peer}   shim LIVE log: session {session} "
                             f"from {host} -> {name}")
            f.write(body)
    except OSError as e:
        log("lobby", f"{peer}   shim live append failed ({e})")
        return b"500 Internal Server Error"
    return b"200 OK"


def _shim_log_store(body, hdrs, peer):
    """Land a POSTed shim log under logs/uploads/. Returns the HTTP status line.

    Named from headers the shim sets, NOT from anything in the body: a client
    that can write the filename can write outside the directory, and this is the
    one endpoint on the whole server that takes bulk client-supplied bytes.
    """
    if not body:
        return b"400 Bad Request"
    if len(body) > _SHIM_LOG_MAX:
        log("lobby", f"{peer}   shim log REFUSED: {len(body)}B exceeds "
                     f"{_SHIM_LOG_MAX}B (POL_SHIM_LOG_MAX)")
        return b"413 Payload Too Large"
    # LIVE increments append to a rolling per-host file; everything below is the
    # whole-file store. Same size gate for both -- a live chunk is ~256K.
    if _shim_tag(hdrs, b"x-shim-mode", "") == "live":
        return _shim_live_append(body, hdrs, peer)
    # Every component is sanitised to [A-Za-z0-9._-]; the timestamp is OURS.
    host = _shim_tag(hdrs, b"x-shim-host", "unknown")
    pid = _shim_tag(hdrs, b"x-shim-pid", "0")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # X-Shim-Reason goes in the FILENAME, so `ls uploads/*CRASH*` is the whole
    # triage step. Without it a crash upload is indistinguishable from a routine
    # one until you grep the body, and the body is up to 8MB.
    reason = _shim_tag(hdrs, b"x-shim-reason", "startup")
    tail = "-CRASH" if reason == "crash" else ""
    name = f"{host}-{pid}-{stamp}{tail}.log"

    n_red = 0
    for rx, repl in _SHIM_LOG_REDACT:
        body, k = rx.subn(repl, body)
        n_red += k

    try:
        os.makedirs(_SHIM_LOG_DIR, exist_ok=True)
        dest = os.path.join(_SHIM_LOG_DIR, name)
        with open(dest, "wb") as f:
            f.write(body)
    except OSError as e:
        log("lobby", f"{peer}   shim log could not be stored ({e})")
        return b"500 Internal Server Error"
    log("lobby", f"{peer}   shim log stored: {name} ({len(body)}B, "
                 f"{n_red} field(s) redacted here)")
    # Lift the crash report's own first lines into the server log. The point is
    # that the interesting part of an 8MB upload shows up where somebody is
    # already reading, instead of only inside a file they have to go and open.
    if reason == "crash":
        for ln in body.split(b"\n"):
            if b"[crash]" not in ln:
                continue
            txt = ln.decode("latin1", "replace").strip()
            if "===" in txt:
                continue
            log("lobby", f"{peer}   CRASH   {txt}")
            if " at " in txt or "frame 3" in txt:
                break
    return b"200 OK"
