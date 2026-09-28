"""Which client build each address last announced to the patch server.

A client announces its exact build on the patch service's version check and
nowhere else: the portal request carries a User-Agent with platform and
language but no version, so the patch check is the only place the server
learns whether it is talking to a 2004 Viewer or a 2011 one. The portal needs
that to pick the right page set (config/portal-eras.yaml), and the two run in
different containers, so the patch server records it here and the portal reads
it back.

Live state (polcore.kv): one hash per address, `clientbuild:<address>`, field
`<region>/<product>` = {"version": ..., "seen": "<UTC ISO time>"} as JSON. A
client re-announces on every launch, so the hash expires TTL after the last
announcement; losing it costs one launch the default era.
"""
import json
import os
import time

from polcore import kv

KEY = "clientbuild:"
#: How long an address's builds are kept after its last announcement.
TTL = float(os.environ.get("POL_CLIENT_BUILDS_TTL", str(30 * 86400)) or 30 * 86400)


def record(address, region, product, version):
    """Note that `address` runs `version` of `region/product`. Never raises:
    a failure here must not cost the patch reply that called it."""
    try:
        if isinstance(version, (bytes, bytearray)):
            version = bytes(version).decode("latin-1")
        name = KEY + str(address)
        kv.hset(name, "%s/%s" % (region, product), json.dumps({
            "version": str(version),
            "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
        kv.expire(name, TTL)
        return True
    except Exception:
        return False


def for_address(address):
    """{"<region>/<product>": {"version", "seen"}} for one address ({} if none)."""
    out = {}
    try:
        for field, raw in (kv.hgetall(KEY + str(address)) or {}).items():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out[field] = rec
    except Exception:
        return {}
    return out


def all_addresses():
    """{address: for_address(address)} for every address on record."""
    out = {}
    try:
        for name in kv.keys(KEY + "*"):
            address = name[len(KEY):]
            out[address] = for_address(address)
    except Exception:
        pass
    return out
