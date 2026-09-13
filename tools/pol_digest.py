"""x-MD5-pol Digest signer for the PlayOnline portal (realm "POL").

Formula recovered live via pol-shim md5Update1 (polcore MD5), verified against
the wire (computed response == sent response). NOTE the field order differs from
RFC 2069:

    HA1      = MD5(userName : "POL" : secret)      # static per session
    HA2      = MD5(method   : uri)                 # per request
    response = MD5(HA1 : HA2 : nonce)              # RFC2069 is HA1:nonce:HA2

`secret` is a session-derived token (NOT the account password); dump it once per
session with the shim, then sign any URL offline for that session's life.
"""
import hashlib


def _md5(s):
    return hashlib.md5(s.encode("latin-1")).hexdigest()


def ha1(user_name, secret, realm="POL"):
    return _md5("%s:%s:%s" % (user_name, realm, secret))


def sign(user_name, secret, method, uri, nonce, realm="POL"):
    """CLIENT side: return (response_hex, Authorization header value)."""
    h1 = ha1(user_name, secret, realm)
    h2 = _md5("%s:%s" % (method, uri))
    resp = _md5("%s:%s:%s" % (h1, h2, nonce))
    hdr = ('userName="%s", realm="%s", nonce="%s", uri="%s", '
           'response="%s", algorithm="x-MD5-pol"'
           % (user_name, realm, nonce, uri, resp))
    return resp, hdr


# --- server side: mutual-auth response ------------------------------------
# rspauth = MD5(HA1 : nonce : "")  -- a trailing empty HA2 field, so it is
# URI-INDEPENDENT (one value per nonce, unchanged across requests on a
# keep-alive connection). Recovered from the client's own md5Update1 verify and
# matched to the wire. NOTE this differs from RFC 2617 rspauth = MD5(HA1:nonce:
# nc:cnonce:qop:MD5(:uri)); the older scaffolding used MD5(HA1:MD5(:uri):nonce),
# which is wrong.

def rspauth(ha1_hex, nonce):
    """SERVER side: Authentication-Info rspauth value."""
    return _md5("%s:%s:" % (ha1_hex, nonce))


def auth_info(ha1_hex, nonce):
    return 'rspauth="%s"' % rspauth(ha1_hex, nonce)


def make_hello(rng32):
    """SERVER side: X-PlayOnline-Hello. The client neither echoes nor hash-
    validates this value (confirmed: it appears in no client send and no MD5/
    SHA-1 input) -- it only requires the header present and well-formed: 32
    bytes A64-encoded to a 43-char token. Pass any 32 random bytes."""
    assert len(rng32) == 32
    return _A64_encode(rng32)


_A64 = "TSG8IncW3HFKokOg79qzeCmZs2yBYEQVAUxR5rbwi4P@jMDLtpvad0f_J1hlN6uX"


def _A64_encode(data):
    bits = nbits = 0
    out = []
    for b in data:
        bits = (bits << 8) | b
        nbits += 8
        while nbits >= 6:
            nbits -= 6
            out.append(_A64[(bits >> nbits) & 0x3F])
    if nbits:
        out.append(_A64[(bits << (6 - nbits)) & 0x3F])
    return "".join(out)


if __name__ == "__main__":
    # The captured transaction, as a self-check (both directions).
    u = "ozTJKRe0KR30oGJpkaYhkRTpoaT"
    s = "vcpuEmnOkt3"
    n = "zxx6ag==cdd8184f3d5ad94e9352d724a3fb0ef232e18162"
    r, hdr = sign(u, s, "GET", "/pml/main/index.pml", n)
    assert r == "9f43d245fe20adc4b090810d98433cf5", r
    ra = rspauth(ha1(u, s), n)
    assert ra == "a0e0eea8c95351c4b0e9342d60e81a09", ra
    print("request  self-check OK:", r)
    print("rspauth  self-check OK:", ra)
    print("HA1:", ha1(u, s))
    print("Authorization:", hdr)
    print("Authentication-Info:", auth_info(ha1(u, s), n))
    print("X-PlayOnline-Hello (example):", make_hello(bytes(range(32))))
