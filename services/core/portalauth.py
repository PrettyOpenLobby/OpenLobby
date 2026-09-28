"""Portal HTTP authentication (x-MD5-pol), validators and cache headers."""
import os
import re
from .deps import pol_digest
from . import authnode



def _portal_md5(s):
    import hashlib
    return hashlib.md5(s.encode("latin1") if isinstance(s, str) else s).hexdigest()


def _portal_rspauth(ha1_hex, nonce):
    """Authentication-Info rspauth = MD5(HA1 : nonce : "") -- a TRAILING EMPTY
    field, URI-independent (not RFC2617's MD5(HA1:MD5(:uri):nonce), which was the
    wrong formula that caused the client to gate the page BODY -> POL-0008)."""
    if pol_digest is not None:
        return pol_digest.rspauth(ha1_hex, nonce)
    return _portal_md5(f"{ha1_hex}:{nonce}:")


def _portal_make_hello():
    """X-PlayOnline-Hello: the client requires the header PRESENT and well-formed
    (32 bytes -> 43-char A64 token) but does NOT echo or hash-validate the value
    (shim-confirmed: absent from all client sends and all MD5/SHA-1 inputs). So any
    32 random bytes work. POL_PORTAL_HELLO pins it for A/B."""
    env = os.environ.get("POL_PORTAL_HELLO")
    if env:
        return env
    if pol_digest is not None and hasattr(pol_digest, "make_hello"):
        return pol_digest.make_hello(os.urandom(32))
    # inline A64 fallback (same alphabet as the login tokens)
    b = os.urandom(32)
    out = []
    for i in range(0, 30, 3):
        v = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2]
        out += [authnode.A64[(v >> 18) & 63], authnode.A64[(v >> 12) & 63],
                authnode.A64[(v >> 6) & 63], authnode.A64[v & 63]]
    v = (b[30] << 16) | (b[31] << 8)
    out += [authnode.A64[(v >> 18) & 63], authnode.A64[(v >> 12) & 63], authnode.A64[(v >> 6) & 63]]
    return "".join(out)


def _portal_auth_headers(hdrs, method, uri):
    """Server side of the x-MD5-pol mutual auth. The client
    gates the page BODY load on these; without them our own server rendered menu
    chrome but hung -> POL-0008. Emits:
      X-PlayOnline-Hello: <well-formed 43-char token>   (value unchecked)
      Authentication-Info: rspauth="MD5(HA1:nonce:)"    (needs the session HA1)
    HA1 = MD5(userName:"POL":secret); userName comes from the request, `secret`
    from POL_PORTAL_SECRET (per-session shim dump, until own-server derivation)."""
    out = b"X-PlayOnline-Hello: " + _portal_make_hello().encode() + b"\r\n"
    auth = hdrs.get(b"authorization", b"").decode("latin1")
    if "Digest" not in auth:
        return out
    import re as _re
    kv = dict(_re.findall(r'(\w+)="([^"]*)"', auth))
    user = os.environ.get("POL_PORTAL_USER") or kv.get("userName", "")
    nonce = kv.get("nonce", "")
    secret = os.environ.get("POL_PORTAL_SECRET")
    if secret and user and nonce:
        # correct mutual-auth value (session secret known -- e.g. shim-dumped)
        ha1 = _portal_md5(f"{user}:POL:{secret}")
        val = _portal_rspauth(ha1, nonce)
    else:
        # OK: THE EXPERIMENT THIS BRANCH SET UP HAS RUN, AND THE ANSWER IS "THE
        # CLIENT DOES NOT CHECK IT". The note here used to pose it as open --
        # "if the body then loads, the client doesn't validate the rspauth value
        # ... if it doesn't, the value IS checked -> dump the own-server session
        # secret via the shim". The log has been answering it for weeks:
        #
        #   lobby.log 2026-08-18, 188 x `401 challenge (x-MD5-pol)`, and e.g.
        #     06:36:17.594  401 challenge for wh000.pol.com/pml/main/index.pml
        #     06:36:17.780  GET /pml/main/index.pml   <- a NEW connection, and it
        #                   draws NO second challenge, so it carried Digest
        #                   Authorization; a nonce in it means the rspauth below
        #                   went out with a RANDOM value
        #     06:36:17.796  served index.pml (55473B)
        #     06:36:18.076  GET /pcd/ntool/en-US/latestnews.pml
        #     06:36:18.340  GET /pcd/mainmenu/en-US/data.pml
        #
        # The client did not merely accept the response, it PARSED the page and
        # fetched that page's own includes -- the load-bearing distinction
        # between a reply being accepted and a scene advancing. A
        # validated rspauth cannot pass on random bytes 188 times.
        #
        # SO POL_PORTAL_SECRET IS NOT NEEDED for the portal to work, and the
        # shim-dump errand this comment was sending people on is retired. The
        # `secret` branch above stays because a CORRECT value is still better
        # than a random one where we happen to hold it, and because it is what
        # the SE-facing install ([redirect] enable=1) would need if SE's own
        # service is ever spoken to with mutual auth that IS checked.
        #
        # WARNING: Do not read this as "the portal is authenticated". It is not, and
        # separately: the request side is not verified either -- the gate above
        # accepts ANY Authorization header (`if b"authorization" in hdrs:
        # conn_authed = True`) without checking its response digest. That is a
        # real property of this server and it is written down at the gate too.
        val = _portal_md5(os.urandom(16))
    if nonce:
        out += b'Authentication-Info: rspauth="' + val.encode() + b'"\r\n'
    return out


#: A Host header is a path component here (/www/<host>/<path>), so it is checked
#: like one. Letters, digits, dot and dash: everything a hostname may contain and
#: nothing that can traverse. `Host: ../wwwx` used to reach outside the root.
_HOST_OK = re.compile(r"^[A-Za-z0-9.-]{1,255}$")


def _under(root, cand):
    """True when `cand` is genuinely inside `root`.

    NOT `cand.startswith(root)`, which was the check here until 2026-08-13: a
    bare string prefix has no notion of a path boundary, so `/wwwsomething` sits
    happily inside `/www` as far as it is concerned. commonpath compares the
    parts, which is the actual question being asked.
    """
    try:
        return os.path.commonpath([os.path.abspath(root),
                                   os.path.abspath(cand)]) == os.path.abspath(root)
    except ValueError:                      # different drives on Windows
        return False


def _portal_validators(path):
    """(Last-Modified value, ETag value) for a file on disk, or None.

    SE's Apache sent both and the Viewer USES them -- the capture-mode relay had
    to strip `If-Modified-Since`/`If-None-Match` from forwarded requests
    (POL_TCP_NO_CACHE) because otherwise SE answered a browse with 304s and no
    bodies. We were sending neither, so the client's on-disk cache could never
    validate and every login re-downloaded the whole menu. Both validators are
    derived from mtime+size, so editing a page under www/ invalidates it
    immediately -- no stale-content trap while we are still authoring PML."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    import time as _time
    lm = _time.strftime("%a, %d %b %Y %H:%M:%S GMT", _time.gmtime(st.st_mtime))
    etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
    return lm, etag


def _portal_cache_headers(validators):
    """Last-Modified/ETag, plus an optional freshness lifetime.

    POL_PORTAL_CACHE_MAXAGE (seconds, default 0 = off) adds Cache-Control and
    Expires. With it set the client may skip the request ENTIRELY on a later
    render rather than revalidating -- the biggest available cut to render time,
    but it also means edits to www/ are not picked up until it expires, so it is
    off by default and is a deliberate knob."""
    if not validators:
        return b""
    lm, etag = validators
    out = (b"Last-Modified: " + lm.encode() + b"\r\n"
           b"ETag: " + etag.encode() + b"\r\n")
    try:
        maxage = int(os.environ.get("POL_PORTAL_CACHE_MAXAGE", "0"))
    except ValueError:
        maxage = 0
    if maxage > 0:
        import time as _time
        exp = _time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                             _time.gmtime(_time.time() + maxage))
        out += (b"Cache-Control: max-age=" + str(maxage).encode() + b"\r\n"
                b"Expires: " + exp.encode() + b"\r\n")
    return out


def _portal_not_modified(hdrs, validators):
    """True when the client's conditional request already has this version."""
    lm, etag = validators
    inm = hdrs.get(b"if-none-match", b"").decode("latin1")
    if inm:
        return etag in [t.strip() for t in inm.split(",")] or inm.strip() == "*"
    ims = hdrs.get(b"if-modified-since", b"").decode("latin1").strip()
    return bool(ims) and ims == lm
