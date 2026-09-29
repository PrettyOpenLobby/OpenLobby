"""Who is at the GM desk, and which Discord accounts may say so.

DUTY IS PER GM. `gm-control.json` (gmd's desk file, see gmd.read_control)
carries `gms`: desk username -> {"until": unix seconds, "via": "desk" |
"discord"}. A GM is on duty while their `until` is in the future, and the desk
is staffed while anyone is. It still EXPIRES on purpose: a claim nobody renews
is a GM who walked away, and a sticky "a GM is here" invites a caller into a
room with nobody in it. The admin panel renews a desk claim while its tab is
open; a claim made from Discord runs for the hours asked for.

Nothing a player sees depends on this directly. What does: a request filed
while nobody is on duty is answered at once by POL mail from the GM (the admin
worker, adminops), and GM-call alerts go to the GMs on duty.

DISCORD LINKS. `/gm link` in Discord mints a one-time code; a GM enters it on
the admin panel's GM desk while signed in, which ties that Discord user to the
desk account. After that `/gm duty` from Discord acts as that account. The
link and the code live in admin_setting (adminusers.get/set_setting), so no
table is added. The bot re-checks the account on every command: a moderator
who has lost the GM permission, or been disabled, cannot go on duty.
"""
import json
import os
import secrets
import time

import gmd

LINK_CODE_TTL = 15 * 60
DISCORD_MAX_HOURS = 12
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I


# --------------------------------------------------------------------------- #
# duty
# --------------------------------------------------------------------------- #
def gms(ctl, now=None):
    """{username: {"until", "via"}} for every claim still in force."""
    now = now or time.time()
    out = {}
    for user, c in (ctl.get("gms") or {}).items():
        try:
            until = float(c.get("until") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if until > now:
            out[user] = {"until": until, "via": c.get("via") or "desk"}
    return out


def staffed(ctl=None, now=None):
    """True while at least one GM is on duty."""
    return bool(gms(ctl if ctl is not None else gmd.read_control(), now))


class _Lock:
    """The desk file is read-modify-written by the admin panel and the Discord
    bridge, two processes. An flock beside it keeps one from undoing the
    other; where flock does not exist (a Windows test run) it is a no-op."""

    def __init__(self, path):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _update(fn, path=None):
    p = path or gmd.CONTROL_PATH
    with _Lock(p):
        ctl = gmd.read_control(p)
        fn(ctl)
        live = gms(ctl)
        ctl["gms"] = live
        # The single desk-wide fields the rest of the code (and older panels)
        # read: on while anyone is, until the last claim runs out.
        ctl["duty"] = bool(live)
        ctl["on_duty_until"] = max((c["until"] for c in live.values()), default=None)
        gmd.write_control(ctl, p)
        return ctl


def set_duty(user, on, ttl, via="desk", path=None):
    """Put `user` on duty for `ttl` seconds, or take them off. Returns the desk."""
    def fn(ctl):
        ctl["gms"] = gms(ctl)
        if on:
            ctl["gms"][user] = {"until": time.time() + ttl, "via": via}
        else:
            ctl["gms"].pop(user, None)
        ctl["by"], ctl["at"] = user, time.time()
    return _update(fn, path)


def clear_all(path=None):
    """Take every GM off duty and hand the desk back to POL_GMD_STATUS_FLAGS."""
    def fn(ctl):
        ctl["gms"] = {}
    ctl = _update(fn, path)
    ctl["duty"] = ctl["on_duty_until"] = None
    gmd.write_control(ctl, path)
    return ctl


def renew(user, ttl, path=None):
    """The panel's heartbeat: extend `user`'s DESK claim. It never creates a
    claim, and never shortens one made from Discord for longer."""
    def fn(ctl):
        ctl["gms"] = gms(ctl)
        c = ctl["gms"].get(user)
        if c:
            c["until"] = max(c["until"], time.time() + ttl)
    return _update(fn, path)


# --------------------------------------------------------------------------- #
# Discord links
# --------------------------------------------------------------------------- #
class LinkError(ValueError):
    pass


def new_link_code(conn, discord_id, discord_name=""):
    import adminusers
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    adminusers.set_setting(conn, "gmlink:code:" + code, json.dumps(
        {"discord_id": str(discord_id), "name": discord_name or "", "at": time.time()}))
    return code


def redeem(conn, code, username, role):
    """Tie the Discord user who minted `code` to desk account `username`."""
    import adminusers
    code = "".join(str(code or "").split()).upper()
    raw = adminusers.get_setting(conn, "gmlink:code:" + code) if code else None
    if not raw:
        raise LinkError("that code is not valid; run /gm link in Discord for a new one")
    adminusers.set_setting(conn, "gmlink:code:" + code, None)
    got = json.loads(raw)
    if time.time() - float(got.get("at") or 0) > LINK_CODE_TTL:
        raise LinkError("that code has expired; run /gm link in Discord for a new one")
    did = got["discord_id"]
    old = discord_for(conn, username)
    if old and old["discord_id"] != did:
        adminusers.set_setting(conn, "gmlink:discord:" + old["discord_id"], None)
    adminusers.set_setting(conn, "gmlink:discord:" + did, json.dumps(
        {"user": username, "role": role, "name": got.get("name") or "", "at": time.time()}))
    adminusers.set_setting(conn, "gmlink:user:" + username.lower(), did)
    return {"discord_id": did, "name": got.get("name") or ""}


def unlink(conn, username):
    import adminusers
    old = discord_for(conn, username)
    if old:
        adminusers.set_setting(conn, "gmlink:discord:" + old["discord_id"], None)
    adminusers.set_setting(conn, "gmlink:user:" + username.lower(), None)
    return old


def discord_for(conn, username):
    """{"discord_id", "name"} linked to desk account `username`, or None."""
    import adminusers
    did = adminusers.get_setting(conn, "gmlink:user:" + str(username or "").lower())
    if not did:
        return None
    raw = adminusers.get_setting(conn, "gmlink:discord:" + did)
    if not raw:
        return None
    got = json.loads(raw)
    return {"discord_id": did, "name": got.get("name") or ""}


def account_for(conn, discord_id):
    """The desk account a Discord user may act as, or None. Re-checked every
    time: a moderator must still exist, be enabled and hold the GM permission;
    the panel owner (no moderator row) always may."""
    import adminusers
    raw = adminusers.get_setting(conn, "gmlink:discord:" + str(discord_id))
    if not raw:
        return None
    got = json.loads(raw)
    user = got.get("user") or ""
    row = adminusers.find_mod(conn, user)
    if row is not None:
        if row["disabled"] or "gm" not in adminusers.perms_of(row):
            return None
        return user
    return user if got.get("role") == "owner" else None
