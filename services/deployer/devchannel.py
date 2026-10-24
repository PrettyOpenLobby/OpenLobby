"""The dev update channel (docs/deployer.md, "Dev channel").

An install follows the stable images by default: OpenLobby's and the games' published images
and the upstream images the games run. The dev channel swaps some of them for preview builds,
unlocked with an access code the developers hand out. The code carries everything the
deployer needs (make_code, parse_code below): "olx1." and then, base64url without padding,
the compact JSON {"r": "<registry host/path>", "t": "<token>", "s": "<source base URL>"},
"t" and "s" optional. A code that also unlocks the developers' patch feed is "olx2." with
one more optional field, "p": "<host/owner>" of the feed's generic packages (patchfeed.py),
so an older deployer given one says to update OpenLobby first instead of calling it damaged.
Either may carry "n": an optional name a game's own setup uses for its repository and image
(NAME below); OpenLobby itself ignores it. The panel never shows the registry; it says "dev builds". A plain
token with POL_DEV_REGISTRY (or a registry given beside it) still works, as before. The
catalog says which images have a dev build: each games/*.json may carry

    "dev": [{"replaces": "<the stable image, without tag>",
             "image": "<its name under the dev registry>", "tag": "<the tag to follow>",
             "services": ["<every service that runs it>", ...],
             "source": "<its repository under the dev source URL, or a full https:// URL>",
             "what": "<what it is, for the panel>", "license": "<SPDX id>"}]

A developer may also hand out a short dev code instead (ABCD-EFGH-JKMN, 12 characters):
single-use, bound to the first install that redeems it. The deployer sends it with its own
install id (InstallId below) to the issuer, a panel that makes such codes
(services/admindevcodes.py): POL_DEV_ISSUER when set, else the owner's setting (IssuerStore),
else the project's own DEFAULT_ISSUER; "off" in either turns short codes off (issuer_of). It
gets an ordinary olx1. code back (redeem below). The
same install asking again gets the same answer; any other install is refused.

The code only unlocks the dev builds; each thing that has one then follows Stable or Dev on
its own: the core (OpenLobby's own images) and each game of the catalog, by key. That lives
in the core checkout's .channel file (JSON, written by `tools/stack.py channel`), so
tools/stack.py, the deployer and the setup scripts all read the same thing:

    {"version": 2, "unlocked": true, "registry": "<host/path>", "source": "<base URL>",
     "since": "<when it was unlocked>", "dev": ["core", "ffxi"],
     "dev_since": {"core": "<when>", "ffxi": "<when>"}}

No file is locked with everything on Stable. A file from before (version 1, {"channel":
"dev", "registry", "source", "since"}) reads as unlocked with every key that has a dev build
on Dev, which is what it meant; the next write turns it into version 2. The access code never
goes there: the deployer keeps it in its own state (Store below), file mode 600.

Shared by tools/stack.py (which imports this file from services/deployer/) and the deployer.
Standard library only.
"""
import base64
import glob
import json
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

CHANNEL_FILE = ".channel"
CHANNELS = ("stable", "dev")
CHANNEL_VERSION = 2

#: host[:port] followed by zero or more path parts, as an image name starts
REGISTRY = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?"
                      r"(/[a-z0-9]([a-z0-9._-]{0,126}[a-z0-9])?){0,8}$")
#: what an access code may be: printable ASCII, no spaces, 8 to 2048 characters
CODE = re.compile(r"^[\x21-\x7e]{8,2048}$")
#: what a token inside an access code may be
TOKEN = re.compile(r"^[\x21-\x7e]{1,400}$")
#: an access code of ours: olx<version>.<base64url payload, no padding>
CODE_PREFIX = "olx1."
#: the same with a patch feed in it ("p")
FEED_PREFIX = "olx2."
#: where a patch feed is: host[:port]/owner
FEED = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_VERSIONED = re.compile(r"^olx([0-9]{1,3})\.")
_B64URL = re.compile(r"^[A-Za-z0-9_-]{4,2043}$")
#: "n" in an access code: the repository and image name a game's own setup uses.
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE = re.compile(r"^https?://[A-Za-z0-9._~:/%+@-]{1,400}$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9._/-]{0,126}[a-z0-9])?$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

DEFAULT_USER = "dev"
TIMEOUT = 10.0
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))

REVOKED = "The access code no longer works; ask for a new one or switch back to Stable."
DAMAGED = "This access code is damaged or incomplete; copy it again, whole."
NEWER = ("This access code is for a newer version of OpenLobby; update OpenLobby first, then "
         "enter it again.")
NOT_OURS = "That is not an OpenLobby access code (they start with olx); copy it again, whole."
NO_CODE = ("No access code is stored here, so the dev builds cannot be updated; enter the access "
           "code again, or switch back to Stable.")
LOCKED = "Enter an access code first: it unlocks the dev builds."
NO_ISSUER = ("That is a short dev code, and short codes are turned off on this server (the issuer "
             "is off). Ask for a full access code (it starts with olx), or set the issuer of "
             "short codes under Update channel, Advanced.")
#: where short dev codes are redeemed when neither POL_DEV_ISSUER nor the owner says: the
#: project's own public issuer
DEFAULT_ISSUER = "https://openlobby.fyi"
#: what turns short codes off, in POL_DEV_ISSUER or the owner's setting
ISSUER_OFF = "off"
#: the services that run the title chain's last link when games add a title
TITLE_SERVICES = ("login", "authsess")
#: a catalog key
KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: a short dev code: 12 characters with no 0/O or 1/I/L, shown in three groups of four
SHORT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SHORT_LEN = 12
_SHORT = re.compile(r"^[%s]{%d}$" % (SHORT_ALPHABET, SHORT_LEN))
#: an install id: what InstallId makes, and what an issuer accepts
INSTALL_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ISSUER = re.compile(r"^https?://[A-Za-z0-9._~:%@\[\]-]{1,300}(/[A-Za-z0-9._~/%-]{0,200})?$")


class ChannelError(Exception):
    pass


class IssuerDown(ChannelError):
    """The dev code issuer did not answer (the panel says 502, not 400)."""


# ------------------------------------------------------------------ the .channel file

def load_catalog(core):
    """{key: entry} from the checkout `core`'s games/*.json, as the deployer reads it."""
    out = {}
    for path in sorted(glob.glob(os.path.join(core, "games", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and e.get("key") and e.get("kind") in ("core", "game"):
            out[e["key"]] = e
    return out


def ordered_keys(catalog, keys=None):
    """`keys` (every key when None) in catalog order: the core first, then by "order"."""
    items = sorted(catalog.items(), key=lambda kv: (kv[1].get("kind") != "core",
                                                    kv[1].get("order", 0), kv[0]))
    return [k for k, _e in items if keys is None or k in keys]


def dev_keys(catalog):
    """The keys whose catalog entry has a dev build, in catalog order."""
    return [k for k in ordered_keys(catalog) if dev_entries(catalog[k])]


def _stable(problem=None):
    out = {"channel": "stable", "unlocked": False, "dev": [], "dev_since": {}}
    if problem:
        out["problem"] = problem
    return out


def read_channel(core, catalog=None):
    """What the checkout `core` follows:

        {"channel": "dev"|"stable", "unlocked": bool, "registry", "source", "since",
         "dev": [keys on Dev], "dev_since": {key: when}}

    "channel" is "dev" while any key is on Dev (what older callers ask). No file, or one
    that does not fit, is locked with everything on Stable; a file that does not fit also
    carries "problem" with a sentence. A version 1 file ("channel": "dev") reads as every
    key of the catalog (`catalog`, else the checkout's own) that has a dev build on Dev,
    with "legacy" true."""
    path = os.path.join(core, CHANNEL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _stable()
    except (OSError, ValueError) as exc:
        return _stable("%s cannot be read (%s), so everything stays on Stable." % (CHANNEL_FILE, exc))
    if not isinstance(raw, dict):
        return _stable("%s does not say what this server follows, so everything stays on "
                       "Stable." % CHANNEL_FILE)
    legacy = "version" not in raw
    if legacy:
        if raw.get("channel") not in CHANNELS:
            return _stable("%s does not name a channel, so everything stays on Stable."
                           % CHANNEL_FILE)
        if raw["channel"] == "stable":
            return _stable()
    elif raw.get("version") != CHANNEL_VERSION:
        return _stable("%s is from a newer OpenLobby (version %r), so everything stays on "
                       "Stable; update OpenLobby." % (CHANNEL_FILE, raw.get("version")))
    elif raw.get("unlocked") is not True:
        return _stable()
    reg, src = raw.get("registry"), raw.get("source")
    if not isinstance(reg, str) or not REGISTRY.match(reg):
        return _stable("%s names no usable registry, so everything stays on Stable."
                       % CHANNEL_FILE)
    out = {"unlocked": True, "registry": reg,
           "source": src if isinstance(src, str) and SOURCE.match(src) else default_source(reg)}
    if isinstance(raw.get("since"), str):
        out["since"] = raw["since"][:32]
    if legacy:
        keys = dev_keys(catalog if catalog is not None else load_catalog(core))
        out["legacy"] = True
        when = {k: out["since"] for k in keys} if out.get("since") else {}
    else:
        got = raw.get("dev") if isinstance(raw.get("dev"), list) else []
        keys = []
        for k in got:
            if isinstance(k, str) and KEY.match(k) and k not in keys:
                keys.append(k)
        since = raw.get("dev_since") if isinstance(raw.get("dev_since"), dict) else {}
        when = {k: since[k][:32] for k in keys if isinstance(since.get(k), str)}
    out.update(dev=keys, dev_since=when, channel="dev" if keys else "stable")
    return out


def _stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _write(core, doc):
    path = os.path.join(core, CHANNEL_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def _doc(ch):
    """The version 2 file for what read_channel answered."""
    return {"version": CHANNEL_VERSION, "unlocked": True, "registry": ch["registry"],
            "source": ch.get("source") or default_source(ch["registry"]),
            "since": ch.get("since") or _stamp(), "dev": list(ch.get("dev") or []),
            "dev_since": dict(ch.get("dev_since") or {})}


def unlock(core, registry, source=None, now=None, dev=None, catalog=None):
    """Unlock the dev builds in checkout `core`: the registry (and source base) the access
    code names. What is on Dev stays as it was, unless `dev` lists the keys to put there
    (added to those already on it)."""
    check_registry(registry)
    if source and not SOURCE.match(source):
        raise ChannelError("the source URL must be http(s)://... (got %r)" % (source,))
    ch = read_channel(core, catalog)
    if not ch["unlocked"]:
        ch = {"dev": [], "dev_since": {}, "since": _stamp(now)}
    ch = dict(ch, registry=registry, source=source or default_source(registry))
    doc = _doc(ch)
    for k in dev or ():
        if not KEY.match(k):
            raise ChannelError("not a key: %r" % (k,))
        if k not in doc["dev"]:
            doc["dev"].append(k)
            doc["dev_since"][k] = _stamp(now)
    _write(core, doc)
    return doc


def set_key(core, key, to, now=None, catalog=None):
    """Put `key` on "dev" or "stable" in checkout `core`. Dev needs the dev builds unlocked;
    Stable always works."""
    if to not in CHANNELS:
        raise ChannelError("a key goes to stable or dev (got %r)" % (to,))
    if not isinstance(key, str) or not KEY.match(key):
        raise ChannelError("not a key: %r" % (key,))
    ch = read_channel(core, catalog)
    if not ch["unlocked"]:
        if to == "stable":
            return _stable()
        raise ChannelError(LOCKED)
    doc = _doc(ch)
    if to == "dev" and key not in doc["dev"]:
        doc["dev"].append(key)
        doc["dev_since"][key] = _stamp(now)
    elif to == "stable" and key in doc["dev"]:
        doc["dev"].remove(key)
        doc["dev_since"].pop(key, None)
    _write(core, doc)
    return doc


def lock(core, catalog=None):
    """Lock the dev builds again (remove .channel). Refused while a key is on Dev."""
    ch = read_channel(core, catalog)
    if ch["dev"]:
        raise ChannelError("switch everything back to Stable first (on Dev: %s)" % ", ".join(ch["dev"]))
    try:
        os.remove(os.path.join(core, CHANNEL_FILE))
    except FileNotFoundError:
        pass
    return _stable()


def write_channel(core, channel, registry=None, source=None, now=None, catalog=None):
    """The whole install at once, as before: "dev" unlocks and puts every key with a dev
    build on Dev; "stable" puts everything back on Stable and locks again."""
    if channel == "stable":
        try:
            os.remove(os.path.join(core, CHANNEL_FILE))
        except FileNotFoundError:
            pass
        return _stable()
    if channel != "dev":
        raise ChannelError("the channel is stable or dev (got %r)" % (channel,))
    cat = catalog if catalog is not None else load_catalog(core)
    return unlock(core, registry, source, now=now, dev=dev_keys(cat), catalog=cat)


def builds_on_core(entry):
    """Whether a game's image is built FROM the core's: a game that adds a title."""
    return bool(entry.get("title"))


def fit_notes(catalog, dev, keys):
    """{key: sentence} for the keys in `keys` whose builds may not fit together with `dev`
    (the keys on Dev): a game on Dev while the core is on Stable, and a game on Stable whose
    image builds on the core while the core is on Dev. A warning, never a refusal."""
    out = {}
    core_dev = "core" in dev
    core_has = bool(dev_entries(catalog.get("core") or {}))
    for k in keys:
        e = catalog.get(k)
        if k == "core" or not e:
            continue
        name = e.get("name") or k
        if k in dev and not core_dev and core_has:
            out[k] = ("May not fit: the dev builds of %s may expect OpenLobby's own dev builds, "
                      "and OpenLobby is on Stable." % name)
        elif k not in dev and core_dev and builds_on_core(e):
            out[k] = ("May not fit: %s's image builds on OpenLobby's, and OpenLobby is on Dev "
                      "while %s stays on Stable." % (name, name))
    return out


def switch_notes(catalog, dev, keys, key, to):
    """The fit_notes a switch of `key` to `to` would leave that concern it: the key's own,
    or for the core every game's."""
    after = [k for k in dev if k != key] + ([key] if to == "dev" else [])
    notes = fit_notes(catalog, after, keys)
    if key == "core":
        return [notes[k] for k in ordered_keys(catalog, notes)]
    return [notes[key]] if key in notes else []


def paired_note(title, core_dev):
    """fit_notes for a paired game on Dev (its own setup program's builds)."""
    if core_dev:
        return None
    return ("May not fit: the dev builds of %s may expect OpenLobby's own dev builds, and "
            "OpenLobby is on Stable." % title)


def services_of(entry):
    """Every service the dev builds of a catalog entry run, and for a game whose published
    image is a title link the title chain's services too (they run it, or build on it)."""
    out = []
    for d in dev_entries(entry):
        for s in d["services"]:
            if s not in out:
                out.append(s)
    if entry.get("kind") == "game" and (entry.get("title") or {}).get("ghcr_image"):
        out += [s for s in TITLE_SERVICES if s not in out]
    return out


def check_registry(registry):
    if not isinstance(registry, str) or not REGISTRY.match(registry):
        raise ChannelError("the dev registry must look like registry.example.org/team "
                           "(got %r)" % (registry,))
    return registry


def default_source(registry):
    """Where the source of a dev image is when POL_DEV_SOURCE does not say: the registry's
    host and path as a web address (a Forgejo or Gitea registry serves both)."""
    return "https://" + registry


def registry_host(registry):
    return registry.split("/", 1)[0]


# ------------------------------------------------------------------ access codes

def make_code(registry, token=None, source=None, feed=None, name=None):
    """The access code for `registry` (host/path), signing in with `token` (None: the
    images are public and need none), their source under `source` (None: the default,
    https://<registry>), and the patch feed at `feed` (host/owner; None: no patches, and
    the code is an olx1. one), and `name` the repository and image name a game's own setup
    uses (None: none; OpenLobby ignores it). For the operator's `tools/stack.py dev-code`."""
    check_registry(registry)
    doc = {"r": registry}
    if name is not None:
        if not isinstance(name, str) or not NAME.match(name):
            raise ChannelError("the name must be 1 to 64 of a-z, 0-9, '.', '_' and '-', starting "
                               "with a letter or digit (got %r)" % (name,))
        doc["n"] = name
    if feed is not None:
        if not isinstance(feed, str) or not FEED.match(feed):
            raise ChannelError("the patch feed must be host/owner (got %r)" % (feed,))
        doc["p"] = feed
    if token is not None:
        if not isinstance(token, str) or not TOKEN.match(token):
            raise ChannelError("the token must be 1 to 400 printable characters, no spaces")
        doc["t"] = token
    if source is not None:
        if not isinstance(source, str) or not SOURCE.match(source):
            raise ChannelError("the source URL must be http(s)://... (got %r)" % (source,))
        doc["s"] = source.rstrip("/")
    raw = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")
    code = (FEED_PREFIX if feed is not None else CODE_PREFIX) + \
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not CODE.match(code):
        raise ChannelError("that makes a code longer than 2048 characters")
    return code


def parse_code(code, registry=None):
    """What an access code says: {"kind", "registry", "token", "source", "feed", "name"}.

    An olx1. code carries its registry, its token (None when it has none: public images)
    and its source base URL (None: the default); an olx2. code also the patch feed, else
    "feed" is None. Either may carry a name for a game's own setup ("name", else None). Anything else is a plain token for
    `registry` (POL_DEV_REGISTRY, or one given beside the code), the way codes worked
    before; with no such registry it is refused. ChannelError carries the sentence the
    panel shows; no sentence repeats the code."""
    if not isinstance(code, str) or not code.strip():
        raise ChannelError("Type the access code.")
    code = code.strip()
    m = _VERSIONED.match(code)
    if not CODE.match(code):
        if m:
            raise ChannelError(DAMAGED)
        raise ChannelError("The access code is 8 to 2048 characters, with no spaces.")
    if m:
        if m.group(0) not in (CODE_PREFIX, FEED_PREFIX):
            raise ChannelError(NEWER if int(m.group(1)) > 2 else DAMAGED)
        fields = {"r", "t", "s", "n"} | ({"p"} if m.group(0) == FEED_PREFIX else set())
        payload = code[len(m.group(0)):]
        if not _B64URL.match(payload) or len(payload) % 4 == 1:
            raise ChannelError(DAMAGED)
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ChannelError(DAMAGED)
        if (not isinstance(doc, dict) or set(doc) - fields
                or not isinstance(doc.get("r"), str) or not REGISTRY.match(doc["r"])
                or ("t" in doc and not (isinstance(doc["t"], str) and TOKEN.match(doc["t"])))
                or ("s" in doc and not (isinstance(doc["s"], str) and SOURCE.match(doc["s"])))
                or ("n" in doc and not (isinstance(doc["n"], str) and NAME.match(doc["n"])))
                or (m.group(0) == FEED_PREFIX
                    and not (isinstance(doc.get("p"), str) and FEED.match(doc["p"])))):
            raise ChannelError(DAMAGED)
        return {"kind": m.group(0)[:-1], "registry": doc["r"], "token": doc.get("t"),
                "source": doc["s"].rstrip("/") if doc.get("s") else None,
                "feed": doc.get("p"), "name": doc.get("n")}
    if not registry:
        raise ChannelError(NOT_OURS)
    check_registry(registry)
    return {"kind": "plain", "registry": registry, "token": code, "source": None, "feed": None,
            "name": None}


def short_code(text):
    """The short dev code `text` is, as its 12 characters (case, spaces and dashes taken out),
    or None when it is not one. Only a code typed in groups of four (ABCD-EFGH-JKMN, or with
    spaces) or as exactly 12 characters counts, so a plain registry token is never taken for
    one."""
    if not isinstance(text, str):
        return None
    t = text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}([ -]?[A-Z0-9]{4}){2}", t):
        return None
    t = re.sub(r"[ -]", "", t)
    return t if _SHORT.match(t) else None


def show_short(code):
    """ABCDEFGHJKMN as ABCD-EFGH-JKMN."""
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def redeem(issuer, code, install_id, timeout=TIMEOUT):
    """Redeem the short dev code `code` (short_code's form) at the issuer `issuer` (a panel's
    base URL) for this install: the olx1. (or olx2.) code it answers, checked with
    parse_code. ChannelError with the issuer's own sentence when it refuses, or one of ours
    when it does not answer; no sentence repeats the code."""
    if not issuer:
        raise ChannelError(NO_ISSUER)
    if not ISSUER.match(issuer):
        raise ChannelError("POL_DEV_ISSUER must be an http(s) address, such as "
                           "https://panel.example.org.")
    if not INSTALL_ID.match(install_id or ""):
        raise ChannelError("This server has no install id to redeem the code with.")
    body = json.dumps({"code": code, "install_id": install_id}).encode("utf-8")
    req = urllib.request.Request(issuer.rstrip("/") + "/api/dev-code/redeem", data=body,
                                 method="POST", headers={"Content-Type": "application/json",
                                                         # Cloudflare's browser check refuses
                                                         # Python-urllib's own (error 1010)
                                                         "User-Agent": "openlobby-deployer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st, raw = r.status, r.read(16384)
    except urllib.error.HTTPError as e:
        st, raw = e.code, e.read(16384)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise IssuerDown("The dev code issuer did not answer (%s); try again later."
                           % str(reason)[:160])
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if st != 200:
        why = doc.get("error") if isinstance(doc.get("error"), str) else None
        raise ChannelError((why or "The dev code issuer refused the code (HTTP %d)." % st)[:300])
    got = doc.get("access_code")
    if not isinstance(got, str):
        raise ChannelError("The dev code issuer answered without an access code.")
    parse_code(got)          # a code we cannot read is refused here, not after a switch
    return got


#: what the owner may set as the issuer: an https address (the issuer is on the internet)
ISSUER_SET = re.compile(r"^https://[A-Za-z0-9._~:%@\[\]-]{1,300}(/[A-Za-z0-9._~/%-]{0,200})?$")


def check_issuer(value):
    """The owner's issuer setting as it is kept: None (the default), "off", or an https
    address without a trailing slash. ChannelError with the sentence when it does not fit."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChannelError("The issuer is an https address, or off.")
    v = value.strip().rstrip("/")
    if not v:
        return None
    if v.lower() == ISSUER_OFF:
        return ISSUER_OFF
    if not ISSUER_SET.match(v):
        raise ChannelError("The issuer must be an https address, such as https://openlobby.fyi, "
                           "or off.")
    return v


def issuer_of(env, stored):
    """(issuer or None when short codes are off, where it comes from: "env", "state" or
    "default"). `env` is POL_DEV_ISSUER as given (empty or None: not set), `stored` the
    owner's setting (IssuerStore.get). The environment wins, then the owner's setting, then
    DEFAULT_ISSUER."""
    e = (env or "").strip().rstrip("/")
    if e:
        if e.lower() == ISSUER_OFF:
            return None, "env"
        return (e if ISSUER.match(e) else None), "env"
    if stored:
        return (None if stored == ISSUER_OFF else stored), "state"
    return DEFAULT_ISSUER, "default"


class IssuerStore:
    """The owner's issuer of short codes, STATE/dev-issuer.json ({"issuer": "<https address>"
    or "off"}). Outside STATE/channel, so forgetting the access code keeps it."""

    def __init__(self, state):
        self.path = os.path.join(state, "dev-issuer.json")

    def get(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            return check_issuer(doc.get("issuer")) if isinstance(doc, dict) else None
        except (OSError, ValueError, ChannelError):
            return None

    def set(self, value):
        """Keep `value` (check_issuer's form); None goes back to the default."""
        value = check_issuer(value)
        if value is None:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            return None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"issuer": value}, f)
        os.replace(tmp, self.path)
        return value


class InstallId:
    """This install's id, made once and kept in STATE/install_id (mode 600). Not a secret,
    but never shown in public: it binds a short dev code to this install. Outside
    STATE/channel, so switching back to Stable keeps it."""

    def __init__(self, state):
        self.path = os.path.join(state, "install_id")

    def get(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                v = f.read().strip()
            if INSTALL_ID.match(v):
                return v
        except OSError:
            pass
        v = "ol-" + secrets.token_hex(16)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(v + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        try:
            os.link(tmp, self.path)       # a second process that won the race keeps its id
        except FileExistsError:
            os.remove(tmp)
            return self.get()
        except OSError:
            os.replace(tmp, self.path)    # no hard links here (some Windows volumes)
            return v
        os.remove(tmp)
        return v


def token_of(code):
    """The token a stored code signs in with: an olx1. code's own (None for public
    images), else the code itself. A damaged stored code gives itself back, so a check
    with it is refused rather than passed."""
    if not code:
        return None
    if _VERSIONED.match(code):
        try:
            return parse_code(code)["token"]
        except ChannelError:
            return code
    return code


def short_name(d):
    """What the panel calls a dev image: the last part of its name, then "(dev)"."""
    return "%s (dev)" % d["image"].rstrip("/").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ the image map

def own_images(catalog):
    """The images the catalog's own repositories publish (built here when they are not
    pulled); a dev build of one of these is used only with published images."""
    out = set()
    for e in catalog.values():
        for img in e.get("images") or []:
            if img.get("ghcr"):
                out.add(img["ghcr"])
        t = e.get("title") or {}
        if t.get("ghcr_image"):
            out.add(t["ghcr_image"])
    return out


def dev_entries(entry):
    """The catalog entry's "dev" list, only the well-formed items."""
    out = []
    for d in entry.get("dev") or []:
        if not isinstance(d, dict):
            continue
        if not (isinstance(d.get("replaces"), str) and d["replaces"]
                and isinstance(d.get("image"), str) and _NAME.match(d["image"])
                and _TAG.match(str(d.get("tag") or "latest"))
                and isinstance(d.get("services"), list) and d["services"]
                and all(isinstance(s, str) and s for s in d["services"])):
            continue
        out.append(d)
    return out


def dev_ref(registry, d):
    return "%s/%s:%s" % (registry, d["image"], d.get("tag") or "latest")


def image_rows(catalog, channel, ghcr, keys=None, registry=None):
    """One row per stable image of the core and the games `keys` (every entry when None), in
    catalog order: what the dev channel puts in its place, or why nothing:

        {"key", "name", "stable", "dev_image", "dev_name", "dev", "applies", "on", "services",
         "source", "what", "license", "note"}

    dev_image is name:tag under the registry, dev_name what the panel shows ("lsb-server
    (dev)"), dev the full reference (None while no registry is known). applies says the dev
    build can be used here, on that its key is on Dev; swapped() gives the rows that run.

    `registry` names the dev registry while the dev builds are locked (the panel shows what
    Dev would use); once unlocked it is the channel's."""
    if "unlocked" not in channel and channel.get("channel") == "dev":
        # the shape from before ({"channel": "dev", "registry"}): everything on Dev
        channel = dict(channel, unlocked=True, dev=list(catalog))
    unlocked = bool(channel.get("unlocked"))
    reg = channel.get("registry") if unlocked else registry
    src_base = channel.get("source") if unlocked and channel.get("source") else \
        (default_source(reg) if reg else None)
    on_dev = set(channel.get("dev") or ()) if unlocked else set()
    own = own_images(catalog)
    rows = []
    for key, e in sorted(catalog.items(), key=lambda kv: (kv[1].get("order", 0), kv[0])):
        if keys is not None and key not in keys and e.get("kind") != "core":
            continue
        devs = {d["replaces"]: d for d in dev_entries(e)}
        stable = [i["ghcr"] for i in e.get("images") or [] if i.get("ghcr")]
        t = e.get("title") or {}
        if t.get("ghcr_image"):
            stable.append(t["ghcr_image"])
        stable += list(devs)
        seen = set()
        for s in stable:
            if s in seen:
                continue
            seen.add(s)
            d = devs.get(s)
            row = {"key": key, "name": e.get("name") or key, "stable": s, "dev_image": None,
                   "dev_name": None, "dev": None, "applies": False, "on": key in on_dev,
                   "services": [], "source": None, "what": None, "license": None, "note": None}
            if d is None:
                row["note"] = "No dev build yet; it stays on the published image."
            else:
                row.update(dev_image="%s:%s" % (d["image"], d.get("tag") or "latest"),
                           dev=dev_ref(reg, d) if reg else None,
                           services=list(d["services"]),
                           what=d.get("what") if isinstance(d.get("what"), str) else None,
                           license=d.get("license") if isinstance(d.get("license"), str) else None)
                row["dev_name"] = short_name(d)
                src = d.get("source")
                if isinstance(src, str) and SOURCE.match(src):
                    row["source"] = src          # a full address, as the catalog gives it
                elif isinstance(src, str) and src.strip("/") and src_base:
                    row["source"] = "%s/%s" % (src_base.rstrip("/"), src.strip("/"))
                row["applies"] = bool(ghcr or s not in own)
                if not row["applies"]:
                    row["note"] = ("This install builds it from its checkout, so the dev image is "
                                   "used only with published images (POL_DEPLOY_GHCR=1).")
            rows.append(row)
    return rows


def swapped(rows):
    """The rows whose dev build runs: their key is on Dev and the build can be used here."""
    return [r for r in rows if r.get("on") and r["applies"] and r["dev"]]


def applied(rows):
    """{stable image: dev ref} of the rows the dev channel swaps."""
    return {r["stable"]: r["dev"] for r in swapped(rows)}


def override_text(rows, skip=(), extra=None):
    """A compose file that sets image: for every service a swapped image runs, or "" when
    nothing is swapped. Applied last, so it wins over the published-image overrides.
    Services in `skip` are left alone (the title chain's, which tools/stack.py works out);
    `extra` is {service: image} to set as well."""
    lines = []
    seen = set(skip)
    done = set()
    for r in swapped(rows):
        for s in r["services"]:
            if s in seen:
                continue
            seen.add(s)
            done.add(s)
            lines.append("  %s:\n    image: %s\n" % (s, r["dev"]))
    for s, image in sorted((extra or {}).items()):
        if s not in done:
            done.add(s)
            lines.append("  %s:\n    image: %s\n" % (s, image))
    if not lines:
        return ""
    return ("# What is on Dev (docs/deployer.md): written by tools/stack.py for one command.\n"
            "services:\n" + "".join(lines))


# ------------------------------------------------------------------ the registry

def split_code(code, user=None):
    """(user name, secret) from an access code. A code may be "name:secret"; a bare code
    signs in under `user` (a Forgejo or Gitea token works under any name)."""
    if ":" in code:
        name, secret = code.split(":", 1)
        if name and secret:
            return name, secret
    return (user or DEFAULT_USER), code


def _request(url, headers, method="GET", timeout=TIMEOUT):
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(4096)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return None, None, str(reason)


def _challenge(value):
    scheme, _, rest = (value or "").partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))


def check_access(registry, code, probes=(), scheme="https", user=None, timeout=TIMEOUT):
    """Whether `code` (a token: token_of an access code; None signs in as nobody, for
    public images) reads the dev registry, asked the way docker asks (the registry API's
    token handshake), without docker. probes are (name, tag) pairs under the registry's
    path. Returns {"state", "why", "digests": {"name:tag": digest}} where state is

        ok           the code signs in and every probe's manifest is readable
        wrong        the registry refuses the code
        denied       the code signs in but may not read a probe
        missing      a probe is not in the registry
        unreachable  the registry did not answer as a registry

    Nothing here logs or returns the code, and no sentence names the registry: the panel
    shows them as they are."""
    host = registry_host(registry)
    path = registry[len(host):].strip("/")
    base = "%s://%s" % (scheme, host)
    if code:
        name, secret = split_code(code, user)
        basic = "Basic " + base64.b64encode(("%s:%s" % (name, secret)).encode("utf-8")).decode("ascii")
        sign = {"Authorization": basic}
    else:
        basic, sign = None, {}
    out = {"state": "ok", "why": None, "digests": {}}
    refused = ("The dev builds did not accept the access code. Check it, or ask for a new one."
               if code else "The dev builds need a sign-in this access code does not carry; "
                            "ask for a new one.")

    st, hdrs, body = _request(base + "/v2/", {}, timeout=timeout)
    if st is None:
        return dict(out, state="unreachable", why="The dev builds' registry did not answer (%s)."
                    % _hide(str(body)[:160], host))
    kind, params = _challenge(hdrs.get("WWW-Authenticate") if hdrs is not None else "")

    def bearer(names):
        realm = params.get("realm")
        if not realm or not realm.startswith(("http://", "https://")):
            return None, dict(out, state="unreachable",
                              why="The dev builds' registry asks for a sign-in it does not describe.")
        q = [("service", params.get("service", ""))] + \
            [("scope", "repository:%s:pull" % n) for n in names]
        st2, _h, b2 = _request(realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q),
                               sign, timeout=timeout)
        if st2 in (401, 403):
            return None, dict(out, state="wrong", why=refused)
        if st2 != 200:
            return None, dict(out, state="unreachable",
                              why="The dev builds' registry did not hand out a sign-in (%s)."
                              % ("HTTP %d" % st2 if st2 else _hide(str(b2)[:160], host)))
        try:
            doc = json.loads(b2)
            tok = doc.get("token") or doc.get("access_token")
        except (ValueError, AttributeError):
            tok = None
        if not isinstance(tok, str) or not tok:
            return None, dict(out, state="unreachable",
                              why="The dev builds' registry answered the sign-in with no token.")
        return "Bearer " + tok, None

    names = ["%s/%s" % (path, n) if path else n for n, _t in probes]
    if st == 401 and kind == "bearer":
        auth, fail = bearer(names)
        if fail:
            return fail
    elif st == 401 and kind == "basic":
        if not code:
            return dict(out, state="wrong", why=refused)
        st2, _h, _b = _request(base + "/v2/", sign, timeout=timeout)
        if st2 in (401, 403):
            return dict(out, state="wrong", why=refused)
        auth = basic
    elif st == 200:
        auth = basic           # a registry open to all: nothing to check the code against
    else:
        return dict(out, state="unreachable", why="The dev builds' registry does not answer as a "
                                                  "container registry (HTTP %d at /v2/)." % st)
    for (n, tag), full in zip(probes, names):
        hdrs = {"Accept": ACCEPT}
        if auth:
            hdrs["Authorization"] = auth
        st3, h3, b3 = _request("%s/v2/%s/manifests/%s" % (base, full, tag), hdrs, method="HEAD",
                               timeout=timeout)
        shown = short_name({"image": n})
        if st3 == 200:
            out["digests"]["%s:%s" % (n, tag)] = (h3.get("Docker-Content-Digest") or "").strip() or None
        elif st3 in (401, 403):
            return dict(out, state="denied", why="The access code may not read %s; ask for a code "
                                                 "that can." % shown)
        elif st3 == 404:
            return dict(out, state="missing", why="The dev builds have no %s yet." % shown)
        else:
            return dict(out, state="unreachable", why="The dev builds' registry did not answer for "
                                                      "%s (%s)." % (shown, "HTTP %d" % st3
                                                                    if st3 else _hide(str(b3)[:160], host)))
    return out


def _hide(text, host):
    """`text` without the registry's host in it (a network error can name it)."""
    return text.replace(host, "the registry").replace(host.split(":", 1)[0], "the registry")


def probes_of(rows):
    """(name, tag) of every swapped image in rows, once each."""
    out = []
    for r in rows:
        if r["applies"] and r["dev_image"]:
            n, _, t = r["dev_image"].rpartition(":")
            if (n, t) not in out:
                out.append((n, t))
    return out


# ------------------------------------------------------------------ the deployer's store

class Store:
    """The access code and the Docker client config it signs in with, under STATE/channel:

        code            the code as it was given, mode 600
        docker/         DOCKER_CONFIG for every docker command the deployer runs on the dev
                        channel (docker login writes config.json here), mode 700

    Never the host's own Docker config."""

    def __init__(self, state):
        self.dir = os.path.join(state, "channel")
        self.code_path = os.path.join(self.dir, "code")
        self.docker_dir = os.path.join(self.dir, "docker")

    def _dirs(self):
        for d in (self.dir, self.docker_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        cfg = os.path.join(self.docker_dir, "config.json")
        if not os.path.exists(cfg):
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("{}\n")

    def docker_config(self):
        """The DOCKER_CONFIG directory, made if need be."""
        self._dirs()
        return self.docker_dir

    def save_code(self, code):
        self._dirs()
        tmp = self.code_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.code_path)

    def code(self):
        try:
            with open(self.code_path, encoding="utf-8") as f:
                c = f.read().strip()
        except OSError:
            return None
        return c or None

    def forget(self):
        """Remove the code and the Docker config with the sign-in in it."""
        shutil.rmtree(self.dir, ignore_errors=True)
