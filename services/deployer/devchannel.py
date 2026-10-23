"""The dev update channel (docs/deployer.md, "Dev channel").

An install follows the stable images by default: OpenLobby's and the games' published images
and the upstream images the games run. The dev channel swaps some of them for preview builds
that live in a registry of your choosing (POL_DEV_REGISTRY, e.g. registry.example.org/team),
which is read with an access code. The catalog says which: each games/*.json may carry

    "dev": [{"replaces": "<the stable image, without tag>",
             "image": "<its name under the dev registry>", "tag": "<the tag to follow>",
             "services": ["<every service that runs it>", ...],
             "source": "<its repository under the dev source URL>",
             "what": "<what it is, for the panel>", "license": "<SPDX id>"}]

Which channel an install is on lives in the core checkout's .channel file (JSON, written by
`tools/stack.py channel`), so tools/stack.py, the deployer and the setup scripts all read the
same thing. The access code never goes there: the deployer keeps it in its own state
(Store below), file mode 600.

Shared by tools/stack.py (which imports this file from services/deployer/) and the deployer.
Standard library only.
"""
import base64
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

CHANNEL_FILE = ".channel"
CHANNELS = ("stable", "dev")

#: host[:port] followed by zero or more path parts, as an image name starts
REGISTRY = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?"
                      r"(/[a-z0-9]([a-z0-9._-]{0,126}[a-z0-9])?){0,8}$")
#: what an access code may be: printable ASCII, no spaces, 8 to 512 characters
CODE = re.compile(r"^[\x21-\x7e]{8,512}$")
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
NO_CODE = ("The dev channel is on but no access code is stored here; switch to Dev again with "
           "a code, or back to Stable.")


class ChannelError(Exception):
    pass


# ------------------------------------------------------------------ the .channel file

def read_channel(core):
    """{"channel": "stable"|"dev", "registry", "source", "since"} for the checkout `core`.
    No file, or one that does not fit, is the stable channel; a file that does not fit
    also carries "problem" with a sentence."""
    path = os.path.join(core, CHANNEL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"channel": "stable"}
    except (OSError, ValueError) as exc:
        return {"channel": "stable", "problem": "%s cannot be read (%s), so the stable channel "
                                                "is used." % (CHANNEL_FILE, exc)}
    if not isinstance(raw, dict) or raw.get("channel") not in CHANNELS:
        return {"channel": "stable", "problem": "%s does not name a channel, so the stable "
                                                "channel is used." % CHANNEL_FILE}
    if raw["channel"] == "stable":
        return {"channel": "stable"}
    reg, src = raw.get("registry"), raw.get("source")
    if not isinstance(reg, str) or not REGISTRY.match(reg):
        return {"channel": "stable", "problem": "%s names no usable registry, so the stable "
                                                "channel is used." % CHANNEL_FILE}
    out = {"channel": "dev", "registry": reg,
           "source": src if isinstance(src, str) and SOURCE.match(src) else default_source(reg)}
    if isinstance(raw.get("since"), str):
        out["since"] = raw["since"][:32]
    return out


def write_channel(core, channel, registry=None, source=None, now=None):
    """Write (or for stable, remove) the .channel file of checkout `core`."""
    path = os.path.join(core, CHANNEL_FILE)
    if channel == "stable":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return {"channel": "stable"}
    if channel != "dev":
        raise ChannelError("the channel is stable or dev (got %r)" % (channel,))
    check_registry(registry)
    if source and not SOURCE.match(source):
        raise ChannelError("the source URL must be http(s)://... (got %r)" % (source,))
    doc = {"channel": "dev", "registry": registry, "source": source or default_source(registry),
           "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return doc


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

        {"key", "name", "stable", "dev_image", "dev", "applies", "services", "source",
         "what", "license", "note"}

    `registry` names the dev registry when the channel is stable (the panel shows what Dev
    would use); on the dev channel it is the channel's."""
    reg = channel.get("registry") if channel.get("channel") == "dev" else registry
    src_base = channel.get("source") if channel.get("channel") == "dev" else \
        (default_source(reg) if reg else None)
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
                   "dev": None, "applies": False, "services": [], "source": None,
                   "what": None, "license": None, "note": None}
            if d is None:
                row["note"] = "No dev build yet; it stays on the published image."
            else:
                row.update(dev_image="%s:%s" % (d["image"], d.get("tag") or "latest"),
                           dev=dev_ref(reg, d) if reg else None,
                           services=list(d["services"]),
                           what=d.get("what") if isinstance(d.get("what"), str) else None,
                           license=d.get("license") if isinstance(d.get("license"), str) else None)
                if isinstance(d.get("source"), str) and d["source"] and src_base:
                    row["source"] = "%s/%s" % (src_base.rstrip("/"), d["source"].strip("/"))
                row["applies"] = bool(ghcr or s not in own)
                if not row["applies"]:
                    row["note"] = ("This install builds it from its checkout, so the dev image is "
                                   "used only with published images (POL_DEPLOY_GHCR=1).")
            rows.append(row)
    return rows


def applied(rows):
    """{stable image: dev ref} of the rows the dev channel swaps."""
    return {r["stable"]: r["dev"] for r in rows if r["applies"] and r["dev"]}


def override_text(rows):
    """A compose file that sets image: for every service a swapped image runs, or "" when
    nothing is swapped. Applied last, so it wins over the published-image overrides."""
    lines = []
    seen = set()
    for r in rows:
        if not (r["applies"] and r["dev"]):
            continue
        for s in r["services"]:
            if s in seen:
                continue
            seen.add(s)
            lines.append("  %s:\n    image: %s\n" % (s, r["dev"]))
    if not lines:
        return ""
    return ("# The dev channel (docs/deployer.md): written by tools/stack.py for one command.\n"
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
    """Whether `code` reads the dev registry, asked the way docker asks (the registry API's
    token handshake), without docker. probes are (name, tag) pairs under the registry's
    path. Returns {"state", "why", "digests": {"name:tag": digest}} where state is

        ok           the code signs in and every probe's manifest is readable
        wrong        the registry refuses the code
        denied       the code signs in but may not read a probe
        missing      a probe is not in the registry
        unreachable  the registry did not answer as a registry

    Nothing here logs or returns the code."""
    host = registry_host(registry)
    path = registry[len(host):].strip("/")
    base = "%s://%s" % (scheme, host)
    name, secret = split_code(code, user)
    basic = "Basic " + base64.b64encode(("%s:%s" % (name, secret)).encode("utf-8")).decode("ascii")
    out = {"state": "ok", "why": None, "digests": {}}

    st, hdrs, body = _request(base + "/v2/", {}, timeout=timeout)
    if st is None:
        return dict(out, state="unreachable", why="The dev registry %s did not answer (%s)."
                    % (host, str(body)[:160]))
    kind, params = _challenge(hdrs.get("WWW-Authenticate") if hdrs is not None else "")

    def bearer(names):
        realm = params.get("realm")
        if not realm or not realm.startswith(("http://", "https://")):
            return None, dict(out, state="unreachable",
                              why="The dev registry %s asks for a sign-in it does not describe." % host)
        q = [("service", params.get("service", ""))] + \
            [("scope", "repository:%s:pull" % n) for n in names]
        st2, _h, b2 = _request(realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q),
                               {"Authorization": basic}, timeout=timeout)
        if st2 in (401, 403):
            return None, dict(out, state="wrong", why="The dev registry %s did not accept the "
                                                      "access code. Check it, or ask for a new one." % host)
        if st2 != 200:
            return None, dict(out, state="unreachable",
                              why="The dev registry %s did not hand out a sign-in (%s)."
                              % (host, "HTTP %d" % st2 if st2 else str(b2)[:160]))
        try:
            doc = json.loads(b2)
            tok = doc.get("token") or doc.get("access_token")
        except (ValueError, AttributeError):
            tok = None
        if not isinstance(tok, str) or not tok:
            return None, dict(out, state="unreachable",
                              why="The dev registry %s answered the sign-in with no token." % host)
        return "Bearer " + tok, None

    names = ["%s/%s" % (path, n) if path else n for n, _t in probes]
    if st == 401 and kind == "bearer":
        auth, fail = bearer(names)
        if fail:
            return fail
    elif st == 401 and kind == "basic":
        st2, _h, _b = _request(base + "/v2/", {"Authorization": basic}, timeout=timeout)
        if st2 in (401, 403):
            return dict(out, state="wrong", why="The dev registry %s did not accept the access "
                                                "code. Check it, or ask for a new one." % host)
        auth = basic
    elif st == 200:
        auth = basic           # a registry open to all: nothing to check the code against
    else:
        return dict(out, state="unreachable", why="%s does not answer as a container registry "
                                                  "(HTTP %d at /v2/)." % (host, st))
    for (n, tag), full in zip(probes, names):
        st3, h3, b3 = _request("%s/v2/%s/manifests/%s" % (base, full, tag),
                               {"Authorization": auth, "Accept": ACCEPT}, method="HEAD",
                               timeout=timeout)
        if st3 == 200:
            out["digests"]["%s:%s" % (n, tag)] = (h3.get("Docker-Content-Digest") or "").strip() or None
        elif st3 in (401, 403):
            return dict(out, state="denied", why="The access code signs in to %s but may not read "
                                                 "%s:%s; ask for a code that can." % (host, n, tag))
        elif st3 == 404:
            return dict(out, state="missing", why="The dev registry %s has no image %s:%s."
                                                  % (host, n, tag))
        else:
            return dict(out, state="unreachable", why="The dev registry %s did not answer for "
                                                      "%s:%s (%s)." % (host, n, tag, "HTTP %d" % st3
                                                                        if st3 else str(b3)[:160]))
    return out


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
