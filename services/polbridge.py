#!/usr/bin/env python3
"""polbridge.py -- PlayOnline friend-list messages in Discord (2026-09-13).

What it does, and the ground rules it is held to:

  * LINK: `/playonline link` gives a one-time code; the member redeems it in
    the in-client account portal (ucscgi kinou 90), behind their own POL
    password. The bot never sees a password. Store: discordlink.py.
  * NOTIFY: every new person-to-person message (kind 0x8000) addressed to a
    linked member is sent to them as a Discord DM. The server already holds
    each message as a file whose name IS its addressing record and whose body
    is plain text (`<subject> 0x07 <body> 0x00`, cp932) -- see responders'
    `_mail_meta` / `_mail_path_of` -- so this only watches that directory.
  * REPLY: the DM's Reply button opens a Discord form; what is typed there is
    posted with `responders._mail_mint`, the same path the server's own
    notices use. It STORES the message and only then announces it, which is
    the one crash-safe order (a push for an object that is not stored crashed
    a client on 2026-08-22).
  * NEVER a POL session: nothing here logs in, writes a `session` row or sends
    a status. Presence is a later phase with its own rules (live POL state
    always wins; Invisible stays invisible).

Discord reaches it over HTTP interactions only (no Gateway): tm.example.com
is the public host, and polboards forwards `/pol/*` to this process on
loopback (POL_BOARDS_BRIDGE_URL). Signatures are checked here, with this app's
own public key, using polboards' RFC 8032 verifier.

    python polbridge.py [--bind 127.0.0.1] [--port 8794]
"""
import argparse
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


import discordlink
import polboards
try:
    import polgateway           # Gateway presence; shipped with the board bots
except ImportError:
    polgateway = None

try:
    import newsgen
except ImportError as _e:                            # pragma: no cover
    newsgen = None
    _NEWSGEN_ERR = str(_e)
else:
    _NEWSGEN_ERR = None

try:
    import eventremind          # cup reminders from the event calendar
except ImportError as _e:                            # pragma: no cover
    eventremind = None
    _EVENTREMIND_ERR = str(_e)
else:
    _EVENTREMIND_ERR = None

DISCORD_API = polboards.DISCORD_API
_UA = "DiscordBot (https://playonline.invalid/polbridge, 1) polbridge"

#: Discord's form limits, and the client's own UI limits (measured: a message's
#: subject is a 50-character field, its body 300).
SUBJECT_MAX = 50
BODY_MAX = 300

#: Replies per member per hour -- a Discord account is one click from a POL
#: account's outbox, so it is capped.
REPLY_PER_HOUR = int(os.environ.get("POL_BRIDGE_REPLY_PER_HOUR", "30"))

#: A message file younger than this is left for the next scan: `_mail_mint`
#: and the lobby both write it in one go, but "in one go" is not atomic.
MIN_AGE = float(os.environ.get("POL_BRIDGE_MIN_AGE", "1.0"))

#: Seconds between passes over the announcement store. Announcements are rare
#: and the store is a file poll, so this can be much longer than the mail poll.
ANNOUNCE_POLL = float(os.environ.get("POL_BRIDGE_ANNOUNCE_POLL", "30"))

#: Seconds a cup's last reminder stays up after the cup's final session ends.
#: An earlier reminder goes as soon as the next one for the same cup is posted.
EVENT_POST_KEEP_S = float(os.environ.get("POL_EVENT_REMIND_KEEP_S", "3600") or 3600)
#: discord_meta key holding the reminders that are still up, as a JSON list of
#: {guild, channel, id, cup, end}.
EVENT_POSTS_META = "event-remind-posts"

#: News posts (announcements and cup reminders) go out through a webhook the
#: bridge creates in the bound channel, so each post can carry its game's own
#: name and avatar. Needs Manage Webhooks there; without it, or with this set
#: to 0, the bridge posts as itself.
NEWS_WEBHOOK = os.environ.get("POL_BRIDGE_NEWS_WEBHOOK", "1") != "0"
NEWS_WEBHOOK_NAME = "OpenLobby News"
#: content -> the user id of that game's own bot, "tetra=123,jan=456": a post
#: for that content borrows the bot's name and avatar. Contents without a bot
#: (PlayOnline itself) post under the bridge's own.
GAME_BOTS = {k.strip(): v.strip() for k, _, v in
             (p.partition("=") for p in os.environ.get("POL_BRIDGE_GAME_BOTS", "").split(","))
             if k.strip() and v.strip()}
#: user id -> (when looked up, {"username", "avatar_url"} or None)
_PROFILES = {}
PROFILE_TTL_S = 3600.0
#: channel id -> when a webhook could not be made there (retried after an hour)
_HOOK_REFUSED = {}

#: Per-`kind` embed colour. Kept next to `KINDS` in newsgen (info / update /
#: maintenance / recovery / issue) plus `event` for the eventnews-derived posts,
#: which arrive with a kind of their own. Anything unknown falls back to info.
ANNOUNCE_COLORS = {
    "info":        0x2B5DB8,
    "update":      0x3BA55D,
    "maintenance": 0xF0B84A,
    "recovery":    0x57C7A2,
    "issue":       0xED4245,
    "event":       0x9B59B6,
}

#: Manage Server or Administrator: who may bind, unbind or change ping roles.
#: Same bits as polboards uses for `/<board>board`.
_ADMIN_BITS = 0x20 | 0x8

#: Where a member redeems a code, as `/playonline link` tells them. The real
#: path through SE's own Support pages (walked in the client 2026-09-13): the
#: Membership menu is static, and only View -> Connect reaches our kinou 1
#: screen, which is where the Discord button is.
PORTAL_HINT = os.environ.get(
    "POL_BRIDGE_PORTAL_HINT",
    "To link it, in the PlayOnline Viewer:\n"
    "1. Open **Support**, then **Membership**.\n"
    "2. Choose **Member Information**, then **View**, then **Connect**.\n"
    "3. Sign in with your PlayOnline ID and password.\n"
    "4. Choose **Discord**, enter the code and choose **Link**.")

#: For tests: a stand-in for urllib.request.urlopen.
OPENER = None

_R = [None]


def R():
    """responders, imported on first use. It is large, and only the watcher and
    a reply need it."""
    if _R[0] is None:
        import responders
        _R[0] = responders
    return _R[0]


def log(text):
    line = "%s [polbridge] %s" % (datetime.datetime.now(datetime.timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"), text)
    print(line, flush=True)
    d = os.environ.get("POL_LOG_DIR", "/logs")
    try:
        if os.path.isdir(d):
            with open(os.path.join(d, "polbridge.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Discord REST
# --------------------------------------------------------------------------- #

def api(method, path, payload=None, token=None):
    """One Discord API call -> (status, json). A 429 is waited out once."""
    token = token if token is not None else ARGS.token
    for attempt in (0, 1):
        req = urllib.request.Request(DISCORD_API + path, method=method,
                                     data=None if payload is None
                                     else json.dumps(payload).encode("utf-8"))
        req.add_header("User-Agent", _UA)
        req.add_header("Authorization", "Bot %s" % token)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with (OPENER or urllib.request.urlopen)(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read() or b"{}")
            except ValueError:
                data = {}
            if e.code == 429 and attempt == 0:
                time.sleep(min(10.0, float(data.get("retry_after", 2) or 2)))
                continue
            return e.code, data
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"error": str(e)}
    return 0, {}


def dm_channel(ldb, link):
    """The DM channel for a linked user, opened once and remembered."""
    if link["dm_channel"]:
        return link["dm_channel"]
    st, data = api("POST", "/users/@me/channels", {"recipient_id": link["discord_id"]})
    if st in (200, 201) and data.get("id"):
        discordlink.set_dm_channel(ldb, link["discord_id"], data["id"])
        return str(data["id"])
    log("cannot open a DM with %s (%s %s)" % (link["discord_id"], st, str(data)[:120]))
    return None


def _content_choices():
    """The `game` choices for `/playonline announce role`, from newsgen's own
    label table so a new service added there shows up here too. Discord caps
    a choice list at 25 (well over the 10 services we ship)."""
    if newsgen is None:
        return []
    return [{"name": label[:100], "value": key}
            for key, label in newsgen.CONTENT_LABELS.items()]


def command_def():
    """The one slash command. Usable in servers, the bot's DMs and as a
    user-installed app, so a member never needs to share a server with it.

    The `announce` subcommand group is server-only (context 0): it configures a
    channel, which a DM does not have. The other subcommands stay available in
    DMs and as a user-installed app.
    """
    role_opts = [{"type": 3, "name": "game", "required": True,
                  "description": "Which game the role is for",
                  "choices": _content_choices()},
                 {"type": 8, "name": "role", "required": False,
                  "description": "Role to ping (omit to clear)"}]
    return {"name": "playonline", "type": 1, "integration_types": [0, 1],
            "contexts": [0, 1, 2],
            "description": "PlayOnline messages in Discord",
            "options": [
                {"type": 1, "name": "link", "description": "Get a code to link your "
                                                          "PlayOnline account"},
                {"type": 1, "name": "unlink", "description": "Unlink your PlayOnline "
                                                            "account"},
                {"type": 1, "name": "status", "description": "Show your link and "
                                                            "notification setting"},
                {"type": 1, "name": "notify", "description": "Turn message DMs on or off",
                 "options": [{"type": 5, "name": "on", "required": True,
                              "description": "Send a DM for each new PlayOnline "
                                             "message"}]},
                {"type": 2, "name": "announce",
                 "description": "PlayOnline server announcements in this channel",
                 "options": [
                    {"type": 1, "name": "here",
                     "description": "Post server announcements in this channel"},
                    {"type": 1, "name": "off",
                     "description": "Stop posting announcements in this server"},
                    {"type": 1, "name": "status",
                     "description": "Show the announcement channel and ping roles"},
                    {"type": 1, "name": "role",
                     "description": "Set (or clear) the role pinged for one game",
                     "options": role_opts},
                    {"type": 1, "name": "test",
                     "description": "Post the newest announcement now to check"},
                 ]}]}


#: How long `/gm duty on:true` lasts when no hours are given.
GM_DUTY_HOURS = 4


def gm_command_def():
    """/gm: the GM desk's duty switch, for GMs away from the admin panel.
    Every subcommand answers privately; only `link` works for anyone, and a
    code does nothing until a GM enters it on the panel (gmduty.py).

    The `channel` subgroup is server-only (context 0): it binds a channel,
    which a DM does not have."""
    return {"name": "gm", "type": 1, "integration_types": [0, 1],
            "contexts": [0, 1, 2],
            "description": "GM desk: go on or off duty, bind an alerts channel",
            "options": [
                {"type": 1, "name": "duty", "description": "Go on or off GM duty",
                 "options": [
                     {"type": 5, "name": "on", "required": True,
                      "description": "On duty (true) or off duty (false)"},
                     {"type": 4, "name": "hours", "required": False,
                      "min_value": 1, "max_value": 12,
                      "description": "How long to stay on duty (default %d)" % GM_DUTY_HOURS}]},
                {"type": 1, "name": "status", "description": "Who is on GM duty"},
                {"type": 1, "name": "link", "description": "Get a code to link this "
                                                          "Discord account to your GM desk account"},
                {"type": 2, "name": "channel",
                 "description": "Post new GM calls in a Discord channel",
                 "options": [
                     {"type": 1, "name": "here",
                      "description": "Post new GM calls in this channel",
                      "options": [
                          {"type": 8, "name": "role", "required": False,
                           "description": "Role to ping on each new call (optional)"}]},
                     {"type": 1, "name": "off",
                      "description": "Stop posting GM calls in this server"},
                     {"type": 1, "name": "status",
                      "description": "Show the bound channel"},
                 ]}]}


def register_commands():
    if not (ARGS.token and ARGS.app_id):
        return None
    st, data = api("PUT", "/applications/%s/commands" % ARGS.app_id,
                   [command_def(), gm_command_def()])
    if st not in (200, 201):
        log("could not register /playonline and /gm (%s %s)" % (st, str(data)[:160]))
    else:
        log("/playonline and /gm registered for app %s" % ARGS.app_id)
    return st


# --------------------------------------------------------------------------- #
# messages -> DMs
# --------------------------------------------------------------------------- #

def read_message(raw):
    """(subject, body) from a stored message object (its bytes). The subject in
    the NAME is cut to 15 bytes; the object carries it whole."""
    text = (raw or b"").split(b"\x00", 1)[0]
    if b"\x07" in text:
        subj, _, body = text.partition(b"\x07")
    else:
        subj, body = b"", text
    dec = lambda b: b.decode("cp932", "replace").replace("\r\n", "\n")   # noqa: E731
    return dec(subj), dec(body)


_MD = re.compile(r"([\\*_~`|>#\[\]()-])")


def md(text):
    """Discord markdown off: a message is shown as the sender typed it."""
    return _MD.sub(r"\\\1", text)


def dm_payload(sender, subject, body, to_handle, when, rid):
    ts = datetime.datetime.fromtimestamp(int(when or time.time()),
                                         datetime.timezone.utc).isoformat()
    return {
        "embeds": [{
            "author": {"name": ("From %s" % sender)[:256]},
            "title": (subject or "(no subject)")[:256],
            "description": md(body)[:4000] or "(no text)",
            "footer": {"text": ("PlayOnline message to %s" % to_handle)[:2048]},
            "timestamp": ts,
            "color": 0x2B5DB8}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 1, "label": "Reply", "custom_id": "pol:reply:%s" % rid}]}],
        "allowed_mentions": {"parse": []}}


def scan_once(now=None, min_age=None):
    """One pass over the message store. Returns how many DMs went out.

    The FIRST pass only takes stock: everything already stored is marked done,
    so turning the bridge on does not DM anyone their whole history.
    """
    rs = R()
    now = time.time() if now is None else now
    min_age = MIN_AGE if min_age is None else min_age
    try:
        stored = {n: at for n, _size, at in
                  rs._res_list_info(scope="mail", suffix=".bin")}
    except Exception as exc:
        log("cannot list the message store (%r)" % (exc,))
        return 0
    names = sorted(stored)
    ldb = discordlink.connect()
    try:
        if discordlink.get_meta(ldb, "baselined") is None:
            discordlink.mark_notified(ldb, names, now)
            discordlink.set_meta(ldb, "baselined", int(now))
            log("first run: %d stored message(s) taken as already seen" % len(names))
            return 0
        sent = 0
        adb = None
        try:
            for name in names:
                if discordlink.is_notified(ldb, name):
                    continue
                if now - stored[name] < min_age:
                    continue                     # still being written; next pass
                path = rs._mail_path_of(name)
                meta = rs._mail_meta(path) if path else None
                if not meta or int(meta.get("kind") or 0) != rs.MAIL_KIND_MESSAGE:
                    discordlink.mark_notified(ldb, name, now)   # a system notice
                    continue
                if adb is None:
                    adb = rs.accounts.connect()
                row = rs._mail_recipient_row(adb, meta["recipient_guid"])
                link = discordlink.link_by_member(ldb, row["member_id"]) if row else None
                if link is None or not link["notify"]:
                    discordlink.mark_notified(ldb, name, now)
                    continue
                subject, body = read_message(rs._res_read(name))
                subject = subject or meta.get("subject") or ""
                rid = discordlink.new_reply(ldb, row["member_id"], row["id"],
                                            meta["sender_guid"], meta.get("sender"),
                                            subject, now)
                ch = dm_channel(ldb, link)
                if ch is None:
                    _fail(ldb, name, now)
                    continue
                st, data = api("POST", "/channels/%s/messages" % ch,
                               dm_payload(meta.get("sender") or "?", subject, body,
                                          row["handle_name"], meta.get("when"), rid))
                if st in (200, 201):
                    discordlink.mark_notified(ldb, name, now)
                    sent += 1
                    log("DM'd member %s: %r from %r" % (row["member_id"], subject,
                                                         meta.get("sender")))
                elif st == 404:
                    discordlink.set_dm_channel(ldb, link["discord_id"], None)
                    _fail(ldb, name, now)
                else:
                    log("DM to %s failed (%s %s)" % (link["discord_id"], st, str(data)[:160]))
                    _fail(ldb, name, now)
        finally:
            if adb is not None:
                adb.close()
        return sent
    finally:
        ldb.close()


_FAILS = {}


def _fail(ldb, name, now):
    """A DM that did not go out is retried on the next passes, then dropped:
    a user with DMs closed must not be retried for ever."""
    _FAILS[name] = _FAILS.get(name, 0) + 1
    if _FAILS[name] >= 5:
        discordlink.mark_notified(ldb, name, now)
        _FAILS.pop(name, None)
        log("gave up on %s after 5 failed DMs" % name[:40])


def watch():
    while True:
        try:
            scan_once()
        except Exception as e:                   # noqa: BLE001 -- never die
            log("watcher error (%r) -- still running" % e)
        time.sleep(max(1.0, float(ARGS.poll)))


# --------------------------------------------------------------------------- #
# announcements -> channel posts
# --------------------------------------------------------------------------- #

def _load_announcements():
    """The current set of announcements as newsgen sees them (serial-stamped),
    or None if newsgen is unavailable or the store cannot be read. A missing or
    empty store yields []."""
    if newsgen is None:
        return None
    try:
        return newsgen.load()
    except (newsgen.NewsError, OSError, ValueError) as e:      # noqa: PERF203
        log("cannot read announcements (%r)" % e)
        return None


def _max_serial(items):
    return max((int(i.get("serial") or 0) for i in items), default=0)


def _content_label(item):
    if newsgen is None:
        return item.get("content") or "PlayOnline"
    return newsgen.CONTENT_LABELS.get(item.get("content") or "",
                                      item.get("content") or "PlayOnline")


def announce_payload(item, ping_role=None):
    """One announcement as a channel post. Role ping (if any) rides on the
    top-level `content`; `allowed_mentions.roles` restricts it to that one id,
    so a rogue title mention cannot @-everyone. Body is markdown-escaped like
    a DM: the announcement is shown as it was authored."""
    body = (item.get("body") or "").strip()
    link = (item.get("link") or "").strip()
    if link:
        body = ("%s\n\n%s" % (body, link)) if body else link
    label = _content_label(item)
    kind = item.get("kind") or "info"
    embed = {
        "title": (item.get("title") or "(no title)")[:256],
        "footer": {"text": ("%s - %s" % (label, kind))[:2048]},
        "color": ANNOUNCE_COLORS.get(kind, ANNOUNCE_COLORS["info"]),
    }
    if body:
        embed["description"] = md(body)[:4000]
    if item.get("date"):
        embed["author"] = {"name": str(item["date"])[:256]}
    payload = {"embeds": [embed],
               "allowed_mentions": {"parse": [],
                                    "roles": [str(ping_role)] if ping_role else []}}
    if ping_role:
        payload["content"] = "<@&%s>" % ping_role
    return payload


def _profile(uid):
    """{"username", "avatar_url"} of Discord user `uid` ("@me" = the bridge),
    cached for an hour; None when it cannot be read."""
    now = time.time()
    hit = _PROFILES.get(uid)
    if hit and now - hit[0] < PROFILE_TTL_S:
        return hit[1]
    st, data = api("GET", "/users/%s" % uid)
    prof = None
    if st == 200 and isinstance(data, dict) and data.get("id"):
        prof = {"username": str(data.get("global_name") or data.get("username") or "")[:80]}
        if data.get("avatar"):
            prof["avatar_url"] = ("https://cdn.discordapp.com/avatars/%s/%s.png?size=256"
                                  % (data["id"], data["avatar"]))
    _PROFILES[uid] = (now, prof)
    return prof


def _identity(content):
    """The username / avatar_url a news post for `content` goes out under:
    the game's own bot, else the bridge, else just the content's label."""
    prof = _profile(GAME_BOTS[content]) if content in GAME_BOTS else _profile("@me")
    out = {"username": (prof or {}).get("username") or _content_label({"content": content})}
    if (prof or {}).get("avatar_url"):
        out["avatar_url"] = prof["avatar_url"]
    return out


def _hook_key(channel_id):
    return "news-webhook:%s" % channel_id


def _news_hook(ldb, channel_id):
    """{"id", "token"} of the bridge's webhook in `channel_id`, made on first
    use; None when webhooks are off or Discord will not let us make one."""
    if not NEWS_WEBHOOK:
        return None
    try:
        hook = json.loads(discordlink.get_meta(ldb, _hook_key(channel_id), "") or "null")
    except ValueError:
        hook = None
    if isinstance(hook, dict) and hook.get("id") and hook.get("token"):
        return hook
    if time.time() - _HOOK_REFUSED.get(channel_id, 0) < 3600:
        return None
    st, data = api("POST", "/channels/%s/webhooks" % channel_id, {"name": NEWS_WEBHOOK_NAME})
    if st in (200, 201) and isinstance(data, dict) and data.get("id") and data.get("token"):
        hook = {"id": str(data["id"]), "token": str(data["token"])}
        discordlink.set_meta(ldb, _hook_key(channel_id), json.dumps(hook))
        log("made the news webhook in channel %s" % channel_id)
        return hook
    _HOOK_REFUSED[channel_id] = time.time()
    log("cannot make a webhook in channel %s (%s %s) -- news posts go out as the "
        "bridge; give it Manage Webhooks there for per-game names"
        % (channel_id, st, str(data)[:120]))
    return None


def news_post(ldb, channel_id, payload, content):
    """Post a news `payload` into `channel_id` under `content`'s game identity.
    Returns (status, data, webhook id or None). A webhook that is gone (deleted
    in Discord) is forgotten and the post goes out as the bridge instead."""
    hook = _news_hook(ldb, channel_id)
    if hook:
        body = dict(payload)
        body.update(_identity(content))
        st, data = api("POST", "/webhooks/%s/%s?wait=true" % (hook["id"], hook["token"]), body)
        if st in (200, 201):
            return st, data, hook["id"]
        log("news webhook in channel %s refused the post (%s %s)"
            % (channel_id, st, str(data)[:120]))
        if st in (401, 403, 404):
            discordlink.set_meta(ldb, _hook_key(channel_id), "")
    st, data = api("POST", "/channels/%s/messages" % channel_id, payload)
    return st, data, None


def _post_announcement(ldb, channel_id, item, roles):
    """Send one announcement to one channel. Returns True on 2xx, False
    otherwise (the caller stops the run so the same item is retried next
    pass; a hard 404 also returns False and the operator sees it in the
    log)."""
    ping = roles.get(item.get("content") or "")
    st, data, _hook = news_post(ldb, channel_id, announce_payload(item, ping_role=ping),
                                item.get("content") or "playonline")
    if st in (200, 201):
        log("announced serial %s (%s) in channel %s"
            % (item.get("serial"), item.get("title", "")[:40], channel_id))
        return True
    log("announce to channel %s failed (%s %s)"
        % (channel_id, st, str(data)[:160]))
    return False


def scan_announce(now=None):
    """One pass over the announcement store. Returns how many posts went out
    across all bound channels."""
    items = _load_announcements()
    if items is None:
        return 0
    ldb = discordlink.connect()
    sent = 0
    try:
        for row in discordlink.announce_rows(ldb):
            last = int(row["last_serial"])
            new = sorted((i for i in items if int(i.get("serial") or 0) > last),
                         key=lambda i: int(i.get("serial") or 0))
            if not new:
                continue
            roles = discordlink.announce_roles(ldb, row["guild_id"])
            for it in new:
                if not _post_announcement(ldb, row["channel_id"], it, roles):
                    break                        # try again next pass
                discordlink.bump_announce_serial(ldb, row["guild_id"],
                                                 int(it["serial"] or 0))
                sent += 1
        return sent
    finally:
        ldb.close()


def event_reminders_on():
    return (eventremind is not None
            and os.environ.get("POL_BRIDGE_EVENT_REMIND", "1") != "0")


def event_reminder_payload(rem, ping_role=None, now=None):
    """One cup reminder (eventremind.due) as a channel post, shaped like an
    announcement so the two read alike in the channel."""
    title, body = eventremind.message(rem, now)
    content = eventremind.eventnews.GAMES[rem["cup"]["game"]][0]
    label = _content_label({"content": content})
    payload = {"embeds": [{"title": title, "description": body[:4000],
                           "footer": {"text": "%s - event" % label},
                           "color": ANNOUNCE_COLORS["event"]}],
               "allowed_mentions": {"parse": [],
                                    "roles": [str(ping_role)] if ping_role else []}}
    if ping_role:
        payload["content"] = "<@&%s>" % ping_role
    return payload


def scan_event_reminders(now=None):
    """Post the cup reminders that are due into every bound announcement
    channel, each once per guild. Returns how many went out.

    eventremind decides what is due; a reminder is only due until the next one
    for the same cup, so a bridge that was down posts the current reminder and
    never a backlog. A failed post is retried on the next pass."""
    if not event_reminders_on():
        return 0
    now = time.time() if now is None else now
    try:
        rems = eventremind.due(now)
    except Exception as e:                       # noqa: BLE001
        log("cannot work out cup reminders (%r)" % e)
        return 0
    if not rems:
        return 0
    ldb = discordlink.connect()
    sent = 0
    posts = _event_posts(ldb)
    try:
        for row in discordlink.announce_rows(ldb):
            roles = None
            for rem in rems:
                name = "event-remind:%s:%s" % (row["guild_id"], rem["key"])
                if discordlink.is_notified(ldb, name):
                    continue
                if roles is None:
                    roles = discordlink.announce_roles(ldb, row["guild_id"])
                content = eventremind.eventnews.GAMES[rem["cup"]["game"]][0]
                st, data, hook = news_post(ldb, row["channel_id"],
                                           event_reminder_payload(rem, roles.get(content), now),
                                           content)
                if st not in (200, 201):
                    log("cup reminder %s to channel %s failed (%s %s)"
                        % (rem["key"], row["channel_id"], st, str(data)[:160]))
                    break                        # try again next pass
                discordlink.mark_notified(ldb, name, now)
                sent += 1
                log("cup reminder %s posted in channel %s"
                    % (rem["key"], row["channel_id"]))
                cup = rem["cup"]
                for old in [p for p in posts if p["guild"] == row["guild_id"]
                            and p["cup"] == cup["id"]]:
                    if _delete_event_post(ldb, old, "replaced by %s" % rem["key"]):
                        posts.remove(old)
                mid = str((data or {}).get("id") or "")
                if mid:
                    posts.append({"guild": row["guild_id"], "channel": row["channel_id"],
                                  "id": mid, "cup": cup["id"], "hook": hook,
                                  "end": max(e for _s, e in cup["sessions"])})
                _save_event_posts(ldb, posts)
        return sent
    finally:
        ldb.close()


def _event_posts(ldb):
    try:
        v = json.loads(discordlink.get_meta(ldb, EVENT_POSTS_META, "[]") or "[]")
    except ValueError:
        v = []
    return [p for p in v if isinstance(p, dict) and p.get("id") and p.get("channel")]


def _save_event_posts(ldb, posts):
    discordlink.set_meta(ldb, EVENT_POSTS_META, json.dumps(posts, sort_keys=True))


def _delete_event_post(ldb, p, why):
    """Delete one reminder post. True when it is gone or can never be deleted
    (404 already gone, 403 no permission), so the caller stops tracking it. A
    post made through the news webhook is deleted through it (the bridge
    itself would need Manage Messages)."""
    path = "/channels/%s/messages/%s" % (p["channel"], p["id"])
    if p.get("hook"):
        try:
            hook = json.loads(discordlink.get_meta(ldb, _hook_key(p["channel"]), "") or "null")
        except ValueError:
            hook = None
        if isinstance(hook, dict) and hook.get("id") == p["hook"] and hook.get("token"):
            path = "/webhooks/%s/%s/messages/%s" % (hook["id"], hook["token"], p["id"])
    st, data = api("DELETE", path)
    if st in (200, 204, 404):
        log("cup reminder %s deleted from channel %s (%s)" % (p["id"], p["channel"], why))
        return True
    if st == 403:
        log("cup reminder %s in channel %s cannot be deleted (403), dropped"
            % (p["id"], p["channel"]))
        return True
    log("could not delete cup reminder %s in channel %s (%s %s), will retry"
        % (p["id"], p["channel"], st, str(data)[:160]))
    return False


def sweep_event_posts(now=None):
    """Delete the reminders whose cup ended more than EVENT_POST_KEEP_S ago.
    Returns how many went."""
    now = time.time() if now is None else now
    ldb = discordlink.connect()
    try:
        posts = _event_posts(ldb)
        keep = [p for p in posts
                if now < float(p.get("end") or 0) + EVENT_POST_KEEP_S
                or not _delete_event_post(ldb, p, "cup %s is over" % p.get("cup"))]
        if len(keep) != len(posts):
            _save_event_posts(ldb, keep)
        return len(posts) - len(keep)
    finally:
        ldb.close()


def announce_watch():
    while True:
        try:
            scan_announce()
        except Exception as e:                   # noqa: BLE001 -- never die
            log("announce watcher error (%r) -- still running" % e)
        try:
            scan_event_reminders()
        except Exception as e:                   # noqa: BLE001 -- never die
            log("cup reminder error (%r) -- still running" % e)
        try:
            sweep_event_posts()
        except Exception as e:                   # noqa: BLE001 -- never die
            log("cup reminder sweep error (%r) -- still running" % e)
        time.sleep(max(5.0, float(ANNOUNCE_POLL)))


def gm_relay_watch():
    """Poll every bound GM channel for new tickets, and every open thread for
    both halves of the relay. Its own thread so a scan exception in one half
    does not stop the other, and so its cadence (chat-turn fast) is not tied
    to the message watcher's (mail-arrival slow)."""
    import gmconsole
    poll = max(0.5, float(os.environ.get("POL_BRIDGE_GM_POLL", "2")))
    while True:
        try:
            gmconsole.scan_new_tickets()
        except Exception as e:                   # noqa: BLE001
            log("gm ticket scan error (%r) -- still running" % e)
        try:
            gmconsole.relay_tick()
        except Exception as e:                   # noqa: BLE001
            log("gm relay error (%r) -- still running" % e)
        try:
            gmconsole.sync_desk_state()
        except Exception as e:                   # noqa: BLE001
            log("gm desk sync error (%r) -- still running" % e)
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# interactions
# --------------------------------------------------------------------------- #

def _say(text):
    return {"type": 4, "data": {"flags": 64, "content": text,
                                "allowed_mentions": {"parse": []}}}


def _user(data):
    u = (data.get("member") or {}).get("user") or data.get("user") or {}
    return str(u.get("id") or ""), (u.get("global_name") or u.get("username") or "")


def _member_label(member_id):
    """'Lex (POL ID 87-...)' for a status line -- best effort."""
    rs = R()
    try:
        adb = rs.accounts.connect()
        try:
            m = adb.execute("SELECT polid FROM member WHERE id = %s", (int(member_id),)).fetchone()
            h = rs.accounts.primary_handle_row(adb, int(member_id))
        finally:
            adb.close()
        return "%s (PlayOnline ID %s)" % (h["handle_name"] if h else "?", m["polid"] if m else "?")
    except Exception:                            # noqa: BLE001
        return "member %s" % member_id


def _sub_opts(sub):
    """Options a subcommand carries, as {name: value}. Discord sends each as
    {name, type, value}; a subcommand group nests another list, handled by the
    caller."""
    out = {}
    for opt in (sub.get("options") or []):
        out[str(opt.get("name"))] = opt.get("value")
    return out


def on_announce(data, group):
    """`/playonline announce ...` (server only). `group` is the subcommand
    group option, whose `options[0]` is the actual subcommand."""
    guild = str(data.get("guild_id") or (data.get("guild") or {}).get("id") or "")
    if not guild:
        return _say("Announcements can only be set up inside a Discord server.")
    subs = group.get("options") or []
    if not subs:
        return _say("Pick one: here, off, status, role, test.")
    sub = subs[0]
    name = str(sub.get("name") or "")
    channel = str(data.get("channel_id") or (data.get("channel") or {}).get("id") or "")
    try:
        perms = int(((data.get("member") or {}).get("permissions")) or 0)
    except (TypeError, ValueError):
        perms = 0
    admin = bool(perms & _ADMIN_BITS)

    if newsgen is None and name in ("here", "test"):
        return _say("Announcements are not available on this server "
                    "(newsgen could not be loaded: %s)." % (_NEWSGEN_ERR or "?"))

    ldb = discordlink.connect()
    try:
        row = discordlink.announce_row(ldb, guild)
        if name == "status":
            if row is None:
                return _say("No announcement channel is set. An admin can run "
                            "`/playonline announce here` in the channel they want.")
            roles = discordlink.announce_roles(ldb, guild)
            lines = ["Posting server announcements in <#%s>." % row["channel_id"]]
            if roles:
                lines.append("Ping roles:")
                for content, role_id in sorted(roles.items()):
                    label = (newsgen.CONTENT_LABELS.get(content, content)
                             if newsgen else content)
                    lines.append("- %s: <@&%s>" % (label, role_id))
            else:
                lines.append("No game has a ping role set. Use "
                             "`/playonline announce role` to add one.")
            if event_reminders_on():
                lines.append("Cup reminders from the event calendar post here "
                             "too (a day before, and a few hours before each "
                             "night of sessions).")
            return _say("\n".join(lines))

        if name == "here":
            if not admin:
                return _say("Only members who can Manage Server may bind the "
                            "announcement channel.")
            items = _load_announcements() or []
            baseline = _max_serial(items)
            discordlink.bind_announce(ldb, guild, channel, baseline)
            note = ("" if row is None else " (moved from <#%s>)" % row["channel_id"])
            return _say("Announcements will post here%s. Existing items are "
                        "treated as already seen, so only new ones will show up."
                        % note)

        if name == "off":
            if not admin:
                return _say("Only members who can Manage Server may unbind the "
                            "announcement channel.")
            if not discordlink.unbind_announce(ldb, guild):
                return _say("No announcement channel is set.")
            return _say("Stopped posting announcements in this server.")

        if name == "role":
            if not admin:
                return _say("Only members who can Manage Server may change "
                            "ping roles.")
            vals = _sub_opts(sub)
            game = str(vals.get("game") or "")
            role_id = vals.get("role")
            if newsgen is not None and game not in newsgen.CONTENTS:
                return _say("That game is not one I know about.")
            discordlink.set_announce_role(ldb, guild, game, role_id or "")
            label = (newsgen.CONTENT_LABELS.get(game, game) if newsgen else game)
            if role_id:
                return _say("Announcements tagged **%s** will now ping <@&%s>."
                            % (label, role_id))
            return _say("Cleared the ping role for **%s**." % label)

        if name == "test":
            if row is None:
                return _say("No announcement channel is set. Run "
                            "`/playonline announce here` first.")
            items = _load_announcements() or []
            if not items:
                return _say("There are no announcements to post.")
            newest = max(items, key=lambda i: int(i.get("serial") or 0))
            roles = discordlink.announce_roles(ldb, guild)
            ok = _post_announcement(ldb, row["channel_id"], newest, roles)
            return _say("Sent the newest announcement." if ok
                        else "The post did not go out. Check the bridge log.")
        return _say("I do not know that one.")
    finally:
        ldb.close()


def on_command(data):
    uid, uname = _user(data)
    opts = (data.get("data") or {}).get("options") or []
    sub = str(opts[0].get("name")) if opts else "status"
    if opts and int(opts[0].get("type") or 0) == 2 and sub == "announce":
        return on_announce(data, opts[0])
    ldb = discordlink.connect()
    try:
        link = discordlink.link_by_discord(ldb, uid)
        if sub == "link":
            code = discordlink.new_code(ldb, uid, uname)
            mins = max(1, discordlink.CODE_TTL // 60)
            again = ("\nThis will replace your current link (%s)." % _member_label(link["member_id"])
                     if link else "")
            return _say("Your link code is **%s** (good for %d minutes).\n%s%s"
                        % (code, mins, PORTAL_HINT, again))
        if sub == "unlink":
            ok = discordlink.unlink_discord(ldb, uid)
            return _say("Unlinked. You will not get PlayOnline DMs any more." if ok
                        else "You are not linked to a PlayOnline account.")
        if sub == "notify":
            on = bool((opts[0].get("options") or [{}])[0].get("value"))
            if not discordlink.set_notify(ldb, uid, on):
                return _say("You are not linked yet. Use `/playonline link` first.")
            return _say("Message DMs are now **%s**." % ("on" if on else "off"))
        if link is None:
            return _say("You are not linked to a PlayOnline account. Use "
                        "`/playonline link` to get a code.")
        return _say("Linked to %s. Message DMs are **%s**."
                    % (_member_label(link["member_id"]), "on" if link["notify"] else "off"))
    finally:
        ldb.close()


def _gm_duty_lines(ctl):
    import gmduty
    on = gmduty.gms(ctl)
    if not on:
        return "Nobody is on GM duty."
    return "On GM duty:\n" + "\n".join(
        "- **%s** until <t:%d:t> (%s)" % (u, int(c["until"]),
                                           "Discord" if c["via"] == "discord" else "admin panel")
        for u, c in sorted(on.items()))


def on_gm_command(data):
    """/gm link | duty | status | channel. The desk account a Discord user
    acts as is re-checked on every command (gmduty.account_for)."""
    import adminusers
    import gmd
    import gmduty
    uid, uname = _user(data)
    opts = (data.get("data") or {}).get("options") or []
    sub = str(opts[0].get("name")) if opts else "status"
    args = {o.get("name"): o.get("value") for o in (opts[0].get("options") or [])} if opts else {}
    if sub == "channel":
        # Subgroup: options[0].options[0] is the leaf (here/off/status), and
        # its own options[] carry the args. Delegated to gmconsole, which does
        # the same gmduty auth check as the leaves below.
        import gmconsole
        leaf_opts = (opts[0].get("options") or [{}])
        leaf = str(leaf_opts[0].get("name") or "status")
        leaf_args = {o.get("name"): o.get("value")
                     for o in (leaf_opts[0].get("options") or [])}
        return gmconsole.on_channel_command(data, leaf, leaf_args)
    conn = adminusers.connect()
    try:
        if sub == "link":
            code = gmduty.new_link_code(conn, uid, uname)
            return _say("Your GM desk link code is **%s** (good for %d minutes). Enter it on "
                        "the admin panel's GM Calls tab, under Duty, while signed in to your "
                        "own account." % (code, gmduty.LINK_CODE_TTL // 60))
        user = gmduty.account_for(conn, uid)
    finally:
        conn.close()
    if not user:
        return _say("This Discord account is not linked to a GM desk account. Run "
                    "`/gm link` and enter the code on the admin panel.")
    if sub == "duty":
        on = bool(args.get("on"))
        hours = max(1, min(gmduty.DISCORD_MAX_HOURS, int(args.get("hours") or GM_DUTY_HOURS)))
        ctl = gmduty.set_duty(user, on, hours * 3600, via="discord")
        log("%s (%s) went %s duty from Discord" % (user, uname, "on" if on else "off"))
        if on:
            until = gmduty.gms(ctl)[user]["until"]
            return _say("You are on GM duty until <t:%d:t>. New GM calls will alert you, "
                        "and players get no \"nobody is on duty\" mail." % int(until))
        return _say("You are off GM duty.\n" + _gm_duty_lines(ctl))
    return _say(_gm_duty_lines(gmd.read_control()))


def _owned_reply(ldb, rid, uid):
    """The reply context behind a button, if THIS user may use it."""
    ctx = discordlink.get_reply(ldb, rid)
    link = discordlink.link_by_discord(ldb, uid)
    if ctx is None or link is None or int(link["member_id"]) != int(ctx["member_id"]):
        return None
    return ctx


def on_reply_button(data, rid):
    uid, _ = _user(data)
    ldb = discordlink.connect()
    try:
        ctx = _owned_reply(ldb, rid, uid)
    finally:
        ldb.close()
    if ctx is None:
        return _say("This message can no longer be answered from here (it is too "
                    "old, or your link changed).")
    subj = ctx["subject"] or ""
    pre = subj if subj.lower().startswith("re:") else ("Re: " + subj if subj else "")
    return {"type": 9, "data": {
        "custom_id": "pol:send:%s" % rid,
        "title": ("Reply to %s" % (ctx["peer_name"] or "PlayOnline"))[:45],
        "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": "subject", "style": 1,
                                        "label": "Subject", "required": True,
                                        "min_length": 1, "max_length": SUBJECT_MAX,
                                        "value": pre[:SUBJECT_MAX]}]},
            {"type": 1, "components": [{"type": 4, "custom_id": "body", "style": 2,
                                        "label": "Message", "required": True,
                                        "min_length": 1, "max_length": BODY_MAX}]}]}}


def _modal_values(data):
    out = {}
    for row in (data.get("data") or {}).get("components") or []:
        for c in row.get("components") or []:
            out[c.get("custom_id")] = c.get("value") or ""
    return out


def clean(text, limit):
    """One line of plain text. Control characters could end the object early
    (0x00) or split subject from body (0x07); newlines are folded to spaces
    until a live client has shown how it draws one."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    return re.sub(r" {2,}", " ", text).strip()[:limit]


def on_reply_submit(data, rid, now=None):
    uid, _ = _user(data)
    vals = _modal_values(data)
    subject, body = clean(vals.get("subject"), SUBJECT_MAX), clean(vals.get("body"), BODY_MAX)
    if not subject or not body:
        return _say("A reply needs a subject and a message.")
    rs = R()
    ldb = discordlink.connect()
    try:
        ctx = _owned_reply(ldb, rid, uid)
        if ctx is None:
            return _say("This message can no longer be answered from here.")
        if discordlink.sent_since(ldb, ctx["member_id"], 3600, now) >= REPLY_PER_HOUR:
            return _say("That is a lot of replies in an hour -- try again a little later.")
        adb = rs.accounts.connect()
        try:
            me = adb.execute("SELECT * FROM handle WHERE id = %s",
                             (int(ctx["handle_id"]),)).fetchone()
            peer = rs._mail_recipient_row(adb, int(ctx["peer_guid"]))
        finally:
            adb.close()
        if me is None or int(me["member_id"]) != int(ctx["member_id"]):
            return _say("That handle is no longer yours, so the reply was not sent.")
        if peer is None:
            return _say("%s is not on this server any more, so the reply was not sent."
                        % (ctx["peer_name"] or "That player"))
        path = rs._mail_mint(me["handle_name"], rs.accounts.handle_guid(int(me["id"])),
                             int(ctx["peer_guid"]), subject, body)
        if not path:
            return _say("The server could not post that reply. Nothing was sent.")
        discordlink.note_sent(ldb, ctx["member_id"], now)
        # our own reply is never DM'd back to anyone as "new": it is addressed
        # to the PEER, and if they are linked they SHOULD get it -- so only the
        # sender's side is ours to settle, and there is nothing to settle.
        log("reply from member %s (%s) to %r: %r" % (ctx["member_id"], me["handle_name"],
                                                     peer["handle_name"], subject))
        return _say("Sent to **%s**." % md(peer["handle_name"]))
    finally:
        ldb.close()


def interaction(data):
    """A verified interaction -> the response body."""
    t = data.get("type")
    if t == 1:
        return {"type": 1}
    if t == 2 and (data.get("data") or {}).get("name") == "playonline":
        return on_command(data)
    if t == 2 and (data.get("data") or {}).get("name") == "gm":
        return on_gm_command(data)
    cid = str((data.get("data") or {}).get("custom_id") or "")
    if t == 3 and cid.startswith("pol:reply:"):
        return on_reply_button(data, cid[len("pol:reply:"):])
    if t == 5 and cid.startswith("pol:send:"):
        return on_reply_submit(data, cid[len("pol:send:"):])
    if t == 3 and cid.startswith("pol:knock:claimed:"):
        return _say("This call is already being handled.")
    if t == 3 and cid.startswith("pol:knock:"):
        import gmconsole
        return gmconsole.on_knock(data, cid[len("pol:knock:"):])
    if t == 3 and cid.startswith("pol:gmclose:"):
        import gmconsole
        return gmconsole.on_close(data, cid[len("pol:gmclose:"):])
    return _say("I do not know that one.")


class _Handler(BaseHTTPRequestHandler):
    server_version = "polbridge/1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/healthz":
            return self._send(200, "ok", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/discord/interactions":
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 1 << 20:
            return self._send(413, "too large", "text/plain")
        body = self.rfile.read(n)
        key = (ARGS.public_key or "").strip()
        try:
            ok = bool(key) and polboards.ed25519_verify(
                bytes.fromhex(key),
                (self.headers.get("X-Signature-Timestamp") or "").encode() + body,
                bytes.fromhex(self.headers.get("X-Signature-Ed25519") or ""))
        except ValueError:
            ok = False
        if not ok:
            return self._send(401, "invalid request signature", "text/plain")
        try:
            data = json.loads(body)
        except ValueError:
            return self._send(400, "bad json", "text/plain")
        try:
            resp = interaction(data)
        except Exception as e:                   # noqa: BLE001
            log("interaction failed (%r)" % e)
            resp = _say("Something went wrong on the PlayOnline side. Nothing was sent.")
        return self._send(200, json.dumps(resp))


def serve(bind, port):
    srv = ThreadingHTTPServer((bind, int(port)), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="polbridge-http", daemon=True).start()
    return srv


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    env = os.environ.get
    ap.add_argument("--bind", default=env("POL_BRIDGE_BIND", "127.0.0.1"),
                    help="address the interactions endpoint binds (polboards "
                         "forwards to it on loopback)")
    ap.add_argument("--port", type=int, default=int(env("POL_BRIDGE_PORT", "8794")))
    ap.add_argument("--token", default=env("POL_BRIDGE_DISCORD_BOT_TOKEN", ""),
                    help="the bridge app's bot token (a SECRET: prod .env only)")
    ap.add_argument("--app-id", default=env("POL_BRIDGE_DISCORD_APP_ID", ""))
    ap.add_argument("--public-key", default=env("POL_BRIDGE_DISCORD_PUBLIC_KEY", ""),
                    help="the app's public key: checks every interaction")
    ap.add_argument("--poll", type=float, default=float(env("POL_BRIDGE_POLL", "5")),
                    help="seconds between scans of the message store")
    return ap


ARGS = build_parser().parse_args([])


def pol_online():
    """How many members have a live PlayOnline session. This is the POL-level
    population -- the Viewer and the portal -- not any one title's, which is
    exactly what the bridge is about.

    It runs every few seconds for a status line. Under SQLite it had its own
    read-only connection so it could never take the write lock the rest of the
    server needed; a PostgreSQL read takes no such lock, so it uses the
    ordinary account connection.
    """
    try:
        import accounts
        conn = accounts.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT member_id) FROM session WHERE expires_at > %s",
                (datetime.datetime.now(datetime.timezone.utc)
                 .strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:                                  # noqa: BLE001
        return None            # unknown, so the status clears rather than lies


def start_presence():
    """The bridge's own Gateway session, so it shows a count like the board
    bots do. It had none: presence lives in polgateway and only polboards was
    wired to it, which is why this bot alone sat blank."""
    off = ("off", "0", "no", "false")
    if polgateway is None:
        return None
    if (os.environ.get("POL_BRIDGE_PRESENCE", "on") or "").strip().lower() in off:
        return None
    return polgateway.Presence(
        "bridge", ARGS.token,
        status_fn=lambda: polgateway.count_text(
            "", n=pol_online(), one="player on PlayOnline",
            many="players on PlayOnline")).start()


def main(argv=None):
    global ARGS
    ARGS = build_parser().parse_args(argv)
    serve(ARGS.bind, ARGS.port)
    log("interactions on http://%s:%d/discord/interactions (app %s, key %s, token %s)"
        % (ARGS.bind, ARGS.port, ARGS.app_id or "?", "set" if ARGS.public_key else "MISSING",
           "set" if ARGS.token else "MISSING"))
    if not ARGS.token:
        log("no bot token: DMs are OFF until POL_BRIDGE_DISCORD_BOT_TOKEN is set")
    else:
        import gmconsole
        gmconsole.wire(api, logger=log)
        threading.Thread(target=register_commands, name="polbridge-commands",
                         daemon=True).start()
        threading.Thread(target=watch, name="polbridge-watch", daemon=True).start()
        threading.Thread(target=gm_relay_watch, name="polbridge-gm-relay",
                         daemon=True).start()
        if newsgen is not None:
            threading.Thread(target=announce_watch,
                             name="polbridge-announce", daemon=True).start()
        else:
            log("newsgen unavailable (%s): /playonline announce is disabled"
                % (_NEWSGEN_ERR or "?"))
        start_presence()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
