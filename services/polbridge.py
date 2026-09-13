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


def command_def():
    """The one slash command. Usable in servers, the bot's DMs and as a
    user-installed app, so a member never needs to share a server with it."""
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
                                             "message"}]}]}


def register_commands():
    if not (ARGS.token and ARGS.app_id):
        return None
    st, data = api("PUT", "/applications/%s/commands" % ARGS.app_id, [command_def()])
    if st not in (200, 201):
        log("could not register /playonline (%s %s)" % (st, str(data)[:160]))
    else:
        log("/playonline registered for app %s" % ARGS.app_id)
    return st


# --------------------------------------------------------------------------- #
# messages -> DMs
# --------------------------------------------------------------------------- #

def read_message(path):
    """(subject, body) from a stored message object. The subject in the NAME is
    cut to 15 bytes; the object carries it whole."""
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.split(b"\x00", 1)[0]
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
        names = sorted(n for n in os.listdir(rs.RESOURCE_DIR)
                       if n.startswith(rs._MAIL_FILE_PREFIX) and n.endswith(".bin"))
    except OSError:
        return 0
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
                full = os.path.join(rs.RESOURCE_DIR, name)
                try:
                    if now - os.path.getmtime(full) < min_age:
                        continue                 # still being written; next pass
                except OSError:
                    continue
                path = rs._mail_path_of(name)
                meta = rs._mail_meta(path) if path else None
                if not meta or int(meta.get("kind") or 0) != rs.MAIL_KIND_MESSAGE:
                    discordlink.mark_notified(ldb, name, now)   # a system notice
                    continue
                if adb is None:
                    adb = rs.accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                             rs.accounts.DEFAULT_DB))
                row = rs._mail_recipient_row(adb, meta["recipient_guid"])
                link = discordlink.link_by_member(ldb, row["member_id"]) if row else None
                if link is None or not link["notify"]:
                    discordlink.mark_notified(ldb, name, now)
                    continue
                subject, body = read_message(full)
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
# interactions
# --------------------------------------------------------------------------- #

def _say(text):
    return {"type": 4, "data": {"flags": 64, "content": text,
                                "allowed_mentions": {"parse": []}}}


def _user(data):
    u = (data.get("member") or {}).get("user") or data.get("user") or {}
    return str(u.get("id") or ""), (u.get("global_name") or u.get("username") or "")


def _member_label(member_id):
    """'Fox (POL ID 87-...)' for a status line -- best effort."""
    rs = R()
    try:
        adb = rs.accounts.connect(os.environ.get("POL_ACCOUNTS_DB", rs.accounts.DEFAULT_DB))
        try:
            m = adb.execute("SELECT polid FROM member WHERE id = ?", (int(member_id),)).fetchone()
            h = rs.accounts.primary_handle_row(adb, int(member_id))
        finally:
            adb.close()
        return "%s (PlayOnline ID %s)" % (h["handle_name"] if h else "?", m["polid"] if m else "?")
    except Exception:                            # noqa: BLE001
        return "member %s" % member_id


def on_command(data):
    uid, uname = _user(data)
    opts = (data.get("data") or {}).get("options") or []
    sub = str(opts[0].get("name")) if opts else "status"
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
        adb = rs.accounts.connect(os.environ.get("POL_ACCOUNTS_DB", rs.accounts.DEFAULT_DB))
        try:
            me = adb.execute("SELECT * FROM handle WHERE id = ?",
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
    cid = str((data.get("data") or {}).get("custom_id") or "")
    if t == 3 and cid.startswith("pol:reply:"):
        return on_reply_button(data, cid[len("pol:reply:"):])
    if t == 5 and cid.startswith("pol:send:"):
        return on_reply_submit(data, cid[len("pol:send:"):])
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
        threading.Thread(target=register_commands, name="polbridge-commands",
                         daemon=True).start()
        threading.Thread(target=watch, name="polbridge-watch", daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
