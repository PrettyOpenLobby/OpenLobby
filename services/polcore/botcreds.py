"""The Discord bot registry (table discord_bot, migration 0011): every bot's
token, app id, public key and page URL in one place, edited on the admin
page instead of in .env.

    POL_BOT_KEY            the sealing key (any long random string). In .env,
                           given to admin and to every bot service; never in
                           the database. Unset = the registry stores no tokens
                           and every service runs on its environment as before.
    POL_BOT_RELOAD_EVERY   seconds between a service's registry checks (60;
                           0 = never restart on a change)
    POL_BOT_EXTRA          more bots for the admin page beyond KNOWN, as
                           "name=Title@port,..." (a deployment's own boards;
                           port = the board's web port, optional); each reads
                           POL_BOARDS_<NAME>_* as fallback

A bot is keyed by the board name the bot runs under, or "bridge". A service
asks resolve(name, env_prefix) once at start: each value comes from the
registry when the registry has it, else from <env_prefix>_DISCORD_BOT_TOKEN /
_DISCORD_APP_ID / _DISCORD_PUBLIC_KEY / _URL, so a bot nobody has moved to the
registry keeps working untouched. watch(names) then exits the process when one
of its rows changes; the container's restart policy brings it back on the new
values. Exiting is the honest reload: a token is baked into Gateway sockets,
REST clients and feed threads all over a board service.

The same row holds the bot's Fluxer side (migration 0023): a board posts on
Fluxer beside Discord as a second bot, with its token (fluxer_token) and the
Fluxer channels each of its feeds posts in (fluxer_channels, {feed key:
"id,id"}): Fluxer has no slash commands, so channels are named here or in
POL_BOARDS_<KEY>_FLUXER_CHANNELS, not picked with /<name>board. A board with
neither keeps its POL_BOARDS_<NAME>_FLUXER_BOT_TOKEN as before.

The token is sealed like accounts' login-password copies: an HMAC-SHA256 key
stream with a 16-byte HMAC tag, stdlib only (the board images have no crypto
library). A row sealed under another key unseals to "" and the service falls
back to its environment.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SEAL_VER = "b1"
DISCORD_API = "https://discord.com/api/v10"
#: Fluxer's API is Discord's shape. Its own address, not fluxer.app's: that one
#: answers with a 308 that Python 3.10's urllib does not follow.
FLUXER_API = os.environ.get("POL_BOARDS_FLUXER_API", "https://api.fluxer.app/v1")
_UA = "DiscordBot (https://playonline.invalid/botcreds, 1) polcore-botcreds"

#: what a board bot needs in a channel: view, send, embed links, attach files,
#: read history (the stray-post sweep)
BOARD_PERMS = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15) | (1 << 16)
#: the bridge also makes news webhooks and private GM threads
BRIDGE_PERMS = BOARD_PERMS | (1 << 29) | (1 << 34) | (1 << 36) | (1 << 38)

#: the bots OpenLobby itself runs: name -> (title, env prefix, invite
#: permissions, web port, slash command). The port is the board's own HTTP
#: port (its page, state.json and /discord/interactions); the bridge has none
#: of its own: its interactions arrive through the Tetra Master board at /pol.
#: POL_BOT_EXTRA adds a deployment's own; known() is the merged list, in the
#: admin page's order (extras before the bridge).
KNOWN = {
    "tm":     ("Tetra Master", "POL_BOARDS_TM", BOARD_PERMS, 8793, "tmboard"),
    "jan":    ("Janhourou", "POL_BOARDS_JAN", BOARD_PERMS, 8791, "janboard"),
    "fmo":    ("Front Mission Online", "POL_BOARDS_FMO", BOARD_PERMS, 8792, "fmoboard"),
    "fe":     ("Fantasy Earth", "POL_BOARDS_FE", BOARD_PERMS, 8790, "feboard"),
    "ffxi":   ("Final Fantasy XI", "POL_BOARDS_FFXI", BOARD_PERMS, 8795, "ffxiboard"),
    "doc":    ("Dirge of Cerberus", "POL_BOARDS_DOC", BOARD_PERMS, 8796, "docboard"),
    "bridge": ("PlayOnline bridge", "POL_BRIDGE", BRIDGE_PERMS, None, "playonline"),
}


def known():
    """KNOWN plus POL_BOT_EXTRA ("name=Title@port,..."; a bad entry is skipped)."""
    out = {k: v for k, v in KNOWN.items() if k != "bridge"}
    for part in os.environ.get("POL_BOT_EXTRA", "").split(","):
        name, _, title = part.partition("=")
        name = name.strip().lower()
        title, _, port = title.partition("@")
        try:
            port = int(port) if port.strip() else None
        except ValueError:
            port = None
        if name and name.isalnum() and name not in KNOWN:
            out[name] = (title.strip() or name, "POL_BOARDS_%s" % name.upper(), BOARD_PERMS,
                         port, name + "board")
    out["bridge"] = KNOWN["bridge"]
    return out

FIELDS = ("token", "app_id", "public_key", "url")

#: a board's extra feeds, each posting a message of its own: KEEP == polboards FEEDS. The
#: key is <board>_<feed>; the ones in SHARES_MAIN post beside the board when they have no
#: channels of their own (polboards FEED_SHARES_MAIN), the others are off without them.
FEEDS = {"tm": ("auction", "live"), "jan": ("live",), "fmo": ("arenas",), "ffxi": ("conquest", "auction")}
SHARES_MAIN = frozenset({"jan_live", "tm_live", "ffxi_conquest", "ffxi_auction"})


def feed_keys(name):
    """[(key, feed)] of every message board `name` posts: (name, "") first."""
    return [(name, "")] + [("%s_%s" % (name, f), f) for f in FEEDS.get(name, ())]


#: the boards whose game has news (newsgen's contents), and that content: the PlayOnline
#: bridge posts it in the board's "<name>_news" channels as the board's own bot
NEWS_BOARDS = {"tm": "tetra", "jan": "jan", "fmo": "fmo", "ffxi": "ffxi", "fe": "fe", "doc": "doc"}


def discord_channels(row):
    """{feed key: "id,id"} from a row's discord_channels; {} when there are none."""
    return fluxer_channels(row, "discord_channels")


def fluxer_channels(row, column="fluxer_channels"):
    """{feed key: "id,id"} from a row's fluxer_channels; {} when there are none."""
    try:
        got = json.loads((row or {}).get(column) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}


# ---------------------------------------------------------------------------
# sealing
# ---------------------------------------------------------------------------
def _key():
    raw = os.environ.get("POL_BOT_KEY", "").strip()
    return hashlib.sha256(raw.encode("utf-8")).digest() if raw else None


def can_seal():
    return _key() is not None


def _stream(key, nonce, n):
    out, ctr = b"", 0
    while len(out) < n:
        out += hmac.new(key, b"stream" + nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
        ctr += 1
    return out[:n]


def seal(token):
    """The sealed form of `token`; raises RuntimeError without POL_BOT_KEY."""
    key = _key()
    if key is None:
        raise RuntimeError("POL_BOT_KEY is not set: the registry cannot store tokens")
    raw = token.encode("utf-8")
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(raw, _stream(key, nonce, len(raw))))
    tag = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()[:16]
    return SEAL_VER + ":" + base64.b64encode(nonce + ct + tag).decode("ascii")


def unseal(sealed):
    """The token, or "" when there is none, no key, or it was sealed under
    another key (or tampered with)."""
    key = _key()
    if not sealed or key is None or not sealed.startswith(SEAL_VER + ":"):
        return ""
    try:
        raw = base64.b64decode(sealed[len(SEAL_VER) + 1:])
    except (ValueError, binascii.Error):
        return ""
    if len(raw) < 32:
        return ""
    nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
    if not hmac.compare_digest(tag, hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()[:16]):
        return ""
    return bytes(a ^ b for a, b in zip(ct, _stream(key, nonce, len(ct)))).decode("utf-8", "replace")


def hint(token):
    """What the page may show of a token: its last 4 characters."""
    return ("..." + token[-4:]) if token else ""


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
def _db():
    from polcore import db
    return db


def rows():
    """{name: row} for every stored bot, token NOT included: has_token says
    whether one is stored, token_ok whether this process can unseal it, and
    token_hint its last four characters."""
    out = {}
    for r in _db().query("SELECT * FROM discord_bot ORDER BY name"):
        tok = unseal(r["token_sealed"])
        r = dict(r)
        sealed = r.pop("token_sealed")
        r.update(has_token=bool(sealed), token_ok=bool(tok), token_hint=hint(tok))
        fsealed = r.pop("fluxer_token_sealed", "") or ""
        ftok = unseal(fsealed)
        r.update(fluxer_has_token=bool(fsealed), fluxer_token_ok=bool(ftok), fluxer_token_hint=hint(ftok))
        out[r["name"]] = r
    return out


def get(name):
    """The stored row with its token unsealed ("" if it cannot be; has_token
    says whether one is stored at all), or None."""
    r = _db().query_one("SELECT * FROM discord_bot WHERE name = %s", (name,))
    if r is None:
        return None
    r = dict(r)
    sealed = r.pop("token_sealed")
    r.update(token=unseal(sealed), has_token=bool(sealed))
    fsealed = r.pop("fluxer_token_sealed", "") or ""
    r.update(fluxer_token=unseal(fsealed), fluxer_has_token=bool(fsealed))
    return r


def save(name, by, token=None, fluxer_token=None, **fields):
    """Create or update a row. token=None keeps the stored one, "" clears it,
    and fluxer_token the same for the Fluxer one; other fields (app_id,
    public_key, username, url, discord_info: the JSON of describe(),
    fluxer_channels, fluxer_info: the JSON of fluxer_describe(), discord_channels) are set when
    given."""
    if name not in known():
        raise ValueError("unknown bot %r" % (name,))
    row = {"name": name, "updated_at": time.time(), "updated_by": by}
    if token is not None:
        row["token_sealed"] = seal(token) if token else ""
    if fluxer_token is not None:
        row["fluxer_token_sealed"] = seal(fluxer_token) if fluxer_token else ""
    for k in ("app_id", "public_key", "username", "url", "discord_info", "fluxer_channels",
              "fluxer_info", "discord_channels"):
        if fields.get(k) is not None:
            row[k] = str(fields[k]).strip()
    _db().upsert("discord_bot", row, "name")


def delete(name):
    return _db().execute("DELETE FROM discord_bot WHERE name = %s", (name,))


def stamp(names):
    """The newest updated_at among `names` (0 when none has a row), or None
    when the table cannot be read."""
    try:
        r = _db().query_one("SELECT COALESCE(MAX(updated_at), 0) AS t FROM discord_bot "
                            "WHERE name = ANY(%s)", (list(names),))
    except Exception:                                  # noqa: BLE001 no db, no table
        return None
    return float(r["t"]) if r else 0.0


# ---------------------------------------------------------------------------
# what a service runs on
# ---------------------------------------------------------------------------
def resolve(name, env_prefix=None, env=None):
    """{token, app_id, public_key, url, source}: each field from the registry
    when it holds a value there, else from the environment. source names where
    the token came from ("registry", "env" or "")."""
    env = os.environ if env is None else env
    prefix = env_prefix or known().get(name, (None, "POL_BOARDS_%s" % name.upper()))[1]
    out = {"token": env.get(prefix + "_DISCORD_BOT_TOKEN", "") or "",
           "app_id": env.get(prefix + "_DISCORD_APP_ID", "") or "",
           "public_key": env.get(prefix + "_DISCORD_PUBLIC_KEY", "") or "",
           "url": env.get(prefix + "_URL", "") or ""}
    out["source"] = "env" if out["token"] else ""
    try:
        row = get(name)
    except Exception as e:                             # noqa: BLE001 no db: the env it is
        print("[botcreds] %s: registry not readable (%s); using the environment"
              % (name, type(e).__name__), file=sys.stderr, flush=True)
        row = None
    if row:
        for k in FIELDS:
            if row.get(k):
                out[k] = row[k]
        if row.get("token"):
            out["source"] = "registry"
    return out


def watch(names, every=None, exit_fn=None, label="service"):
    """Exit this process when any of `names` changes in the registry, so the
    container restarts on the new values. Returns the thread, or None when
    switched off. The first read is the baseline: a registry that cannot be
    read is retried, never treated as a change."""
    every = float(os.environ.get("POL_BOT_RELOAD_EVERY", "60")) if every is None else every
    if every <= 0 or not names:
        return None
    names = list(names)
    exit_fn = exit_fn or (lambda: os._exit(0))
    base = {"t": stamp(names)}

    def loop():
        while True:
            time.sleep(every)
            t = stamp(names)
            if t is None:
                continue
            if base["t"] is None:
                base["t"] = t
                continue
            if t != base["t"]:
                print("[botcreds] %s: bot registry changed for %s; restarting on the new "
                      "credentials" % (label, ",".join(names)), file=sys.stderr, flush=True)
                exit_fn()
                return

    th = threading.Thread(target=loop, name="botcreds-watch", daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def api(token, method, path, body=None, timeout=10, base=None):
    """(status, parsed JSON or None) for one bot-authenticated call, to Discord
    or to `base` (FLUXER_API). A 429 is waited out once."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (0, 1):
        req = urllib.request.Request((base or DISCORD_API) + path, data=data, method=method)
        req.add_header("Authorization", "Bot " + token)
        req.add_header("User-Agent", _UA)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            try:
                got = json.loads(raw) if raw else None
            except ValueError:
                got = {"message": raw[:200].decode("utf-8", "replace")}
            if e.code == 429 and attempt == 0:
                time.sleep(min(10.0, float((got or {}).get("retry_after") or 1)))
                continue
            return e.code, got
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"message": str(e)}
    return 429, None


#: application flags that switch a privileged Gateway intent on (the full and
#: the "limited" variant of each). Nothing here asks for them: every service
#: connects with intents 0.
PRIVILEGED = {"Presence intent": (1 << 12) | (1 << 13),
              "Server members intent": (1 << 14) | (1 << 15),
              "Message content intent": (1 << 18) | (1 << 19)}


def describe(token):
    """What Discord says about the bot behind `token`: {app_id, public_key,
    username, name, endpoint, guilds, public, code_grant, intents (the
    privileged ones switched on), commands (registered slash commands),
    checked_at}, or {"error": text}."""
    st, app = api(token, "GET", "/applications/@me")
    if st != 200 or not isinstance(app, dict):
        msg = (app or {}).get("message") if isinstance(app, dict) else None
        return {"error": "Discord refused the token (%s%s)" % (st, ": " + msg if msg else "")}
    bot = app.get("bot") or {}
    out = {"app_id": str(app.get("id") or ""), "public_key": app.get("verify_key") or "",
           "name": app.get("name") or "", "username": bot.get("username") or app.get("name") or "",
           "endpoint": app.get("interactions_endpoint_url") or "",
           "guilds": app.get("approximate_guild_count"),
           "public": bool(app.get("bot_public")),
           "code_grant": bool(app.get("bot_require_code_grant")),
           "intents": [k for k, bits in PRIVILEGED.items() if int(app.get("flags") or 0) & bits],
           "commands": None, "checked_at": time.time()}
    if out["app_id"]:
        st, cmds = api(token, "GET", "/applications/%s/commands" % out["app_id"])
        if st == 200 and isinstance(cmds, list):
            out["commands"] = sorted(c.get("name") or "" for c in cmds)
    return out


def set_endpoint(token, url):
    """Point the app's interactions endpoint at `url`. Discord checks it at
    once by sending a signed PING there, so the board must already be running
    with this app's public key. Returns (ok, message)."""
    st, got = api(token, "PATCH", "/applications/@me", {"interactions_endpoint_url": url})
    if st == 200:
        return True, "Discord accepted %s" % url
    msg = ""
    if isinstance(got, dict):
        msg = got.get("message") or ""
        errs = got.get("errors") or {}
        detail = json.dumps(errs)[:300] if errs else ""
        msg = (msg + (" " + detail if detail else "")).strip()
    return False, "Discord refused the endpoint (%s): %s" % (st, msg or "no reason given")


def fluxer_describe(token, channels=()):
    """What Fluxer says about the bot behind `token` and the channels it is to post in:
    {user_id, username, app_name, public, guilds (the servers' names), channels: {id:
    {"name", "guild"} or {"error"}}, checked_at}, or {"error": text}."""
    st, me = api(token, "GET", "/users/@me", base=FLUXER_API)
    if st != 200 or not isinstance(me, dict) or not me.get("id"):
        msg = (me or {}).get("message") if isinstance(me, dict) else None
        return {"error": "Fluxer refused the token (%s%s)" % (st, ": " + msg if msg else "")}
    out = {"user_id": str(me["id"]), "username": me.get("username") or "", "app_name": "",
           "public": None, "guilds": None, "channels": {}, "checked_at": time.time()}
    st, app = api(token, "GET", "/applications/@me", base=FLUXER_API)
    if st == 200 and isinstance(app, dict):
        out.update(app_name=app.get("name") or "", public=bool(app.get("bot_public")))
    st, gs = api(token, "GET", "/users/@me/guilds", base=FLUXER_API)
    names = {}
    if st == 200 and isinstance(gs, list):
        names = {str(g.get("id")): g.get("name") or "" for g in gs if isinstance(g, dict)}
        out["guilds"] = sorted(names.values())
    for cid in channels:
        st, ch = api(token, "GET", "/channels/%s" % cid, base=FLUXER_API)
        if st == 200 and isinstance(ch, dict):
            out["channels"][cid] = {"name": ch.get("name") or "",
                                    "guild": names.get(str(ch.get("guild_id")), "")}
        else:
            out["channels"][cid] = {"error": "the bot cannot see it (%s)" % st}
    return out


def invite_url(name, app_id):
    """The "add to server" link for this bot, with the permissions its
    service needs."""
    if not app_id:
        return ""
    perms = known().get(name, (None, None, BOARD_PERMS))[2]
    return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": app_id, "scope": "bot applications.commands", "permissions": str(perms)})
