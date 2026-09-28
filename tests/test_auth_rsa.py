#!/usr/bin/env python3
"""POL_AUTH_RSA: wrap a random session key to the client's own RSA modulus.

    python tests/test_auth_rsa.py

The USER token is the client's per-launch 256-bit RSA modulus (little-endian,
e = 0xFFFF). With POL_AUTH_RSA=1 the auth hop sends a PKCS#1 v1.5 wrap of a
random byte-reversed 8-byte Blowfish key in place of TOKEN0, the way Project
Crystal Server does, and the rest of the connection -- and the lobby -- runs
under that key. Here a locally generated keypair plays the client. This proves
the server side against OUR reading of the format; only a live client can prove
the client agrees.
"""
import os
import random
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

tmp = tempfile.mkdtemp(prefix="authrsa-")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp
os.environ["POL_CAPTURE"] = "0"
os.environ["POL_AUTH_OBSERVE"] = "1"
for k in ("POL_AUTH_RSA", "POL_IV_FROM_USER", "POL_LOBBY_KEY"):
    os.environ.pop(k, None)

import responders  # noqa: E402
import sessioncrypt  # noqa: E402

bad = 0
#: A made-up NICK digest and login-token tail; the login is observed, not checked.
DIGEST = "0123456789abcdef0123456789abcdef"
TAIL = "TESTTOKEN12NNNN"


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


# ---- a tiny RSA keygen: the test's stand-in for the client -------------------
rng = random.Random(0x5EC)
E = 0xFFFF


def is_prime(n):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(24):
        a = rng.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def prime128():
    while True:
        p = rng.getrandbits(128) | (1 << 127) | 1
        if is_prime(p) and (p - 1) % 3 and (p - 1) % 5 and (p - 1) % 17 \
                and (p - 1) % 257:
            return p


def keypair(bits):
    while True:
        p, q = prime128(), prime128()
        n = p * q
        if n.bit_length() == bits and p != q:
            return n, pow(E, -1, (p - 1) * (q - 1))


def unwrap(n, d, c):
    """What the client does: RSA-decrypt, strip PKCS#1 v1.5, un-reverse."""
    k = (n.bit_length() + 7) // 8
    em = pow(int.from_bytes(c, "big"), d, n).to_bytes(k, "big")
    assert em[0] == 0 and em[1] == 2, em[:2].hex()
    sep = em.index(0, 2)
    assert sep >= 10 and all(em[2:sep]), "padding must be >= 8 non-zero bytes"
    return em[sep + 1:][::-1]


def user_line(n):
    tok = sessioncrypt.b64encode(n.to_bytes(32, "little"))[:43]
    return sessioncrypt.frame_line(("USER x 8 * :" + tok).encode(), pad=b"")


print("wrap / unwrap round trip")
for bits in (255, 256):
    n, d = keypair(bits)
    for key in (bytes(range(1, 9)), os.urandom(8), b"\x00" * 7 + b"\x01"):
        c = sessioncrypt.rsa_wrap_key(n, key)
        chk("%d-bit modulus: ciphertext is 32 bytes" % bits, len(c), 32)
        chk("%d-bit modulus: client unwraps key %s" % (bits, key.hex()),
            unwrap(n, d, c), key)
    c1 = sessioncrypt.rsa_wrap_key(n, bytes(8))
    c2 = sessioncrypt.rsa_wrap_key(n, bytes(8))
    chk("%d-bit: random padding (two wraps of one key differ)" % bits, c1 != c2,
        True)
    # the twin: a DIFFERENT key must not unwrap to the one we wrapped
    n2, d2 = keypair(bits)
    try:
        other = unwrap(n2, d2, sessioncrypt.rsa_wrap_key(n, bytes(range(1, 9))))
    except (AssertionError, ValueError):
        other = None
    chk("%d-bit: the wrong private key does not recover it" % bits,
        other == bytes(range(1, 9)), False)

print("the USER token is the modulus (and its low 8 bytes the IV)")
n, d = keypair(255)
line = user_line(n)
tok = line.split(b":", 1)[1]
chk("47-char token (43 + line checksum), like the client's", len(tok), 47)
chk("user_token_modulus reads n back", sessioncrypt.user_token_modulus(tok), n)
chk("iv_from_user_token = n's low 8 bytes",
    sessioncrypt.iv_from_user_token(tok), n.to_bytes(32, "little")[:8])
chk("a 47-char token (with its checksum) parses to an odd 255-bit number",
    (lambda m: (m & 1, m.bit_length()))(sessioncrypt.user_token_modulus(
        tok.decode("latin-1"))), (1, 255))
chk("an even or tiny value is refused", sessioncrypt.user_token_modulus(
    sessioncrypt.b64encode((2 ** 200).to_bytes(32, "little"))[:43]), None)


def read_line(sock):
    buf = b""
    while b"\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\r\n", 1)[0]


def logtail():
    path = os.path.join(tmp, "authserv.log")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


NICK = (b"NICK UTESTPC001:" + DIGEST.encode() + b":"
        + b"TTTTTAISTTTTTTTTTTTTT" + TAIL.encode())


def login(n, d, nick_key=None, peer=("203.0.113.7", 40002)):
    """Play the client over a socket pair. Returns (key line, key the client
    used, first reply decrypted, new log text)."""
    before = len(logtail())
    srv, cli = socket.socketpair()
    t = threading.Thread(target=responders.handle_authserv,
                         args=(srv, peer, 51241, "srv", 51242))
    t.start()
    cli.settimeout(10)
    read_line(cli)                                   # greeting
    cli.sendall(user_line(n) + b"\r\n")
    keyline = read_line(cli)
    iv = n.to_bytes(32, "little")[:8]
    if nick_key is None:                            # take what the server sent
        body = keyline.split(b" 300 * ", 1)[1]
        wrapped = sessioncrypt.b64decode(body[:44].decode())[:32]
        nick_key = unwrap(n, d, wrapped)
    P, S = sessioncrypt.bf_setkey(nick_key)
    cli.sendall(sessioncrypt.ofb_apply(P, S, iv, NICK) + b"\r\n")
    try:
        reply = read_line(cli)
    except OSError:
        reply = b""
    t.join(30)
    cli.close()
    return keyline, nick_key, sessioncrypt.ofb_apply(P, S, iv, reply), \
        logtail()[before:]


print("knob OFF (default): the key line is still TOKEN0")
kl, _k, rep, out = login(n, d, nick_key=bytes(8))
chk("key line = TOKEN0", kl, (":pol-1000-51241.pol.com 300 * "
                              + responders.TOKEN0).encode())
chk("the reply reads under K=0", rep.startswith(b"ERROR :Closing Link"), True)

print("POL_AUTH_RSA=1 over a socket pair")
os.environ["POL_AUTH_RSA"] = "1"
kl, key, rep, out = login(n, d)
prefix, _, body = kl.partition(b" 300 * ")
chk("line shape ':<srv> 300 * <token><checksum>' (Crystal's)",
    prefix, b":pol-1000-51241.pol.com")
chk("token = 44 symbols + 4-char checksum", len(body), 48)
chk("every token symbol is SE-base64",
    all(chr(b) in sessioncrypt.B64 for b in body[:44]), True)
chk("the checksum is the client's line checksum",
    sessioncrypt.frame_line(kl[:-4], pad=b""), kl)
chk("a fresh random key, not zero", key != bytes(8), True)
chk("NICK read under the RSA-wrapped key",
    "NICK decrypted under the RSA-wrapped key" in out, True)
chk("the reply is encrypted under it too",
    rep.startswith(b"ERROR :Closing Link"), True)
sid = responders._sid_for_user_token(user_line(n).split(b":", 1)[1].strip())
with responders._SESSIONS_LOCK:
    slot = dict(responders._SESSIONS.get(sid) or {})
iv = n.to_bytes(32, "little")[:8]
chk("session key = the negotiated key (resume + bound lobby use it)",
    slot.get("key"), key)
chk("the IV is mapped to the key for the lobby join",
    (slot.get("iv_keys") or {}).get(iv.hex()), key.hex())
responders.session_bind("u-unbound-lobby-thread")
chk("an UNBOUND lobby thread picks the key by IV",
    responders._lobby_key_for_iv(iv), key)
chk("...and any other IV still gets the old key (K=0)",
    responders._lobby_key_for_iv(b"\x11" * 8), bytes(8))
os.environ["POL_LOBBY_KEY"] = "0102030405060708"
chk("POL_LOBBY_KEY still overrides", responders._lobby_key_for_iv(iv),
    bytes.fromhex("0102030405060708"))
os.environ.pop("POL_LOBBY_KEY")
hdr = b"\x02\x00\x00\x00" + (64).to_bytes(4, "little") + bytes(32)
P, S = sessioncrypt.bf_setkey(key)
frame = sessioncrypt.ofb_apply(P, S, iv, hdr) + bytes(64)
chk("a lobby frame under the negotiated key validates its header",
    responders._lobby_header_ok(responders._lobby_crypt(frame[:8], iv),
                                len(frame)), True)

print("the twin: a client that ignores the wrapped key")
n3, d3 = keypair(256)
kl, _k, rep, out = login(n3, d3, nick_key=bytes(8), peer=("203.0.113.8", 40003))
chk("it was sent the RSA line", b" 300 * " + responders.TOKEN0.encode() in kl,
    False)
chk("the server says so, loudly", "IGNORED the RSA-wrapped key" in out, True)
chk("...and the login carries on under K=0",
    rep.startswith(b"ERROR :Closing Link"), True)
os.environ.pop("POL_AUTH_RSA")

print("FAILED: %d" % bad if bad else "all ok")
sys.exit(1 if bad else 0)
