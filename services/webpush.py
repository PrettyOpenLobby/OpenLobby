"""Web Push (RFC 8030) with message encryption (RFC 8291) and VAPID (RFC 8292).

What the admin panel uses to put a GM-call alert on an operator's phone or
desktop without a third-party library: the push services (Google, Mozilla,
Apple) all speak this same protocol. pycryptodome supplies P-256 ECDH, ECDSA
and AES-GCM; HKDF is written out with stdlib hmac, as RFC 8291 spells it.

    key = new_vapid_key()                         # once; keep it
    public_key_b64u(key)                          # -> applicationServerKey
    send(subscription, b'{"title":...}', key, "https://panel.example")

`subscription` is the browser's PushSubscription JSON:
{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}.
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

RECORD_SIZE = 4096


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_dec(text):
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


# --------------------------------------------------------------------------- #
# VAPID: who is sending
# --------------------------------------------------------------------------- #
def new_vapid_key():
    """A fresh P-256 key, as PEM text (what the panel stores)."""
    return ECC.generate(curve="P-256").export_key(format="PEM")


def public_key_b64u(pem):
    """The raw uncompressed public point, base64url: the browser's
    `applicationServerKey`."""
    return b64u(ECC.import_key(pem).public_key().export_key(format="SEC1"))


def vapid_authorization(pem, endpoint, subject, expires_in=12 * 3600):
    """The `Authorization: vapid t=..., k=...` header value for `endpoint`."""
    key = ECC.import_key(pem)
    u = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}",
              "exp": int(time.time()) + int(expires_in), "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                           separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    sig = DSS.new(key, "fips-186-3", encoding="binary").sign(SHA256.new(signing_input))
    return f"vapid t={head}.{body}.{b64u(sig)}, k={public_key_b64u(pem)}"


# --------------------------------------------------------------------------- #
# RFC 8291: the message body
# --------------------------------------------------------------------------- #
def encrypt(payload, p256dh, auth, salt=None, sender_key=None):
    """Encrypt `payload` (bytes) for one subscription. `salt` and `sender_key`
    are only passed by tests; normally both are fresh per message."""
    ua_public = b64u_dec(p256dh) if isinstance(p256dh, str) else p256dh
    auth_secret = b64u_dec(auth) if isinstance(auth, str) else auth
    if len(ua_public) != 65 or ua_public[0] != 4:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    if len(auth_secret) != 16:
        raise ValueError("auth must be 16 bytes")
    salt = salt or os.urandom(16)
    as_key = sender_key or ECC.generate(curve="P-256")
    as_public = as_key.public_key().export_key(format="SEC1")
    ua_key = ECC.import_key(ua_public, curve_name="P-256")
    ecdh = key_agreement(static_priv=as_key, static_pub=ua_key, kdf=lambda z: z)

    ikm = _hkdf(auth_secret, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    if len(payload) > RECORD_SIZE - 16 - 1 - 86:
        raise ValueError("payload too large for one record")
    cipher = AES.new(cek, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(payload + b"\x02")   # \x02 = last record
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ct + tag


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
def send(subscription, payload, pem, subject, ttl=3600, timeout=10,
         urgency="high", opener=None):
    """POST one message. Returns (http_status, response_text). A 404 or 410
    means the subscription is gone and should be forgotten."""
    endpoint = subscription["endpoint"]
    keys = subscription.get("keys") or {}
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode("utf-8")
    body = encrypt(payload, keys["p256dh"], keys["auth"])
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": vapid_authorization(pem, endpoint, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(int(ttl)), "Urgency": urgency,
    })
    try:
        r = (opener or urllib.request.build_opener()).open(req, timeout=timeout)
        return r.status, r.read(500).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode("utf-8", "replace")
