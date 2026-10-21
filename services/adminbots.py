"""The admin page's Discord bots tab: one row per bot (polcore/botcreds.py
known(): KNOWN plus POL_BOT_EXTRA), backed by the discord_bot table. OWNER ONLY: /api/bots is in none of
admin.py's moderator permission tables.

    GET  /api/bots                   every bot: what is stored (never the
                                     token itself: has_token / token_hint),
                                     what its service last started on
                                     (Valkey botcreds:applied:<name>), its
                                     invite link and interactions endpoint
    POST /api/bots {bot, action, ...}
         save      token and/or url. A token is checked with Discord first
                   (GET /applications/@me), which also fills in the app id,
                   public key and bot name: pasting the token is the whole
                   setup.
         refresh   ask Discord again with the stored token
         endpoint  point the app's interactions endpoint at <url>/discord/
                   interactions (Discord PINGs it at once, so the board must
                   already be running on this app's public key)
         clear     forget the stored token (the service goes back to .env)
         delete    forget the whole row

A saved change reaches the bot's service within POL_BOT_RELOAD_EVERY (60 s):
the service sees its row move and restarts itself on the new values.
"""
import json

from polcore import botcreds

ACTIONS = ("save", "refresh", "endpoint", "clear", "delete")


def _applied(name):
    try:
        from polcore import kv
        return kv.get_json("botcreds:applied:%s" % name)
    except Exception:                                  # noqa: BLE001 no Valkey
        return None


def endpoint_of(url):
    return (url.rstrip("/") + "/discord/interactions") if url else ""


def _info(r):
    try:
        got = json.loads(r.get("discord_info") or "")
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def checklist(name, r, port, command):
    """The setup checklist for one bot: [{state, label, detail}], state one of
    ok / todo / warn / info. Built from the stored row and what Discord said
    when it was last saved or checked (discord_info)."""
    out = []

    def add(state, label, detail):
        out.append({"state": state, "label": label, "detail": detail})

    url = r.get("url") or ""
    info = _info(r)
    if name == "bridge":
        add("info", "Web address", "No page of its own: Discord reaches the bridge through the "
            "Tetra Master board (port 8793) under /pol, so its address is https://<tm host>/pol.")
    elif port:
        add("ok" if url else "todo", "Web address",
            ("Board page on port %d of the server, published as %s." % (port, url)) if url else
            "The board's page is on port %d of the server. Point a public https hostname at that "
            "port and save it here: Discord only calls https." % port)
    if not r.get("has_token"):
        add("todo", "Bot token", "Paste the token from the developer portal (Bot, Reset Token).")
        return out
    if not info:
        add("info", "Discord settings", "Not checked yet: press Check.")
        return out
    want = endpoint_of(url)
    have = info.get("endpoint") or ""
    if have and want and have == want:
        add("ok", "Interactions endpoint", have)
    elif have:
        add("warn", "Interactions endpoint", "Discord has %s%s." % (
            have, ("; this board expects %s (press Set endpoint)" % want) if want else ""))
    else:
        add("todo", "Interactions endpoint", "Not set. Once the card says the bot is running, press "
            "Set endpoint%s." % ((" (or paste %s under General Information, Interactions "
                                  "Endpoint URL)" % want) if want else " after saving the web address"))
    if info.get("code_grant"):
        add("warn", "Requires OAuth2 Code Grant", "On: the invite link will fail. Turn it off "
            "under Bot in the developer portal.")
    else:
        add("ok", "Requires OAuth2 Code Grant", "Off, as the invite link needs.")
    add("ok", "Privileged intents", "None needed: the bot connects with no intents." + (
        " On but unused: %s (they can be switched off under Bot)." % ", ".join(info["intents"])
        if info.get("intents") else ""))
    add("info", "Public bot", "On: anyone with the invite link can add it to a server. Turn it "
        "off under Bot to keep it to your servers." if info.get("public") else
        "Off: only you can add it to servers.")
    cmds = info.get("commands")
    if cmds is None:
        add("info", "Slash command", "/%s: could not be read." % command)
    elif command in cmds:
        add("ok", "Slash command", "/%s is registered. Run it in the channel the bot should post in."
            % command)
    else:
        add("todo", "Slash command", "/%s is not registered yet: the bot's service registers it when "
            "it starts on this token. Press Check again in a minute." % command)
    if info.get("guilds") is not None:
        add("ok" if info["guilds"] else "todo", "Servers",
            ("In %d server%s." % (info["guilds"], "" if info["guilds"] == 1 else "s"))
            if info["guilds"] else "Not in any server yet: use Invite.")
    return out


def listing():
    """(status, body) for GET /api/bots."""
    try:
        stored = botcreds.rows()
    except Exception as e:                             # noqa: BLE001
        return 503, {"error": "The bot registry is not readable: %s" % e}
    bots = []
    for name, (title, prefix, _perms, port, command) in botcreds.known().items():
        r = stored.get(name) or {}
        info = _info(r) or {}
        applied = _applied(name)
        bots.append({
            "bot": name, "title": title, "env_prefix": prefix, "port": port,
            "command": command, "checklist": checklist(name, r, port, command),
            "checked_at": info.get("checked_at"),
            "stored": bool(r), "has_token": bool(r.get("has_token")),
            "token_ok": bool(r.get("token_ok")), "token_hint": r.get("token_hint") or "",
            "app_id": r.get("app_id") or "", "public_key": r.get("public_key") or "",
            "username": r.get("username") or "", "url": r.get("url") or "",
            "updated_at": r.get("updated_at"), "updated_by": r.get("updated_by"),
            "invite": botcreds.invite_url(name, r.get("app_id") or ""),
            "endpoint": endpoint_of(r.get("url") or ""),
            "applied": applied,
            # the service has restarted since the last change, on the registry's token
            "live": bool(applied and r and float(applied.get("row_at") or 0) >= float(r.get("updated_at") or 0)
                         and applied.get("source") == "registry"),
        })
    return 200, {"can_seal": botcreds.can_seal(), "bots": bots,
                 "reload_every": _reload_every()}


def _reload_every():
    import os
    try:
        return float(os.environ.get("POL_BOT_RELOAD_EVERY", "60"))
    except ValueError:
        return 60.0


def _clean_url(url):
    url = str(url or "").strip().rstrip("/")
    if url and not (url.startswith("https://") and " " not in url and len(url) <= 300):
        raise ValueError("The page address must start with https:// (Discord only calls https).")
    return url


def change(body, actor, describe=None, set_endpoint=None):
    """(status, body) for POST /api/bots. describe / set_endpoint stand in for
    the Discord calls in tests."""
    describe = describe or botcreds.describe
    set_endpoint = set_endpoint or botcreds.set_endpoint
    name = str(body.get("bot") or "")
    act = str(body.get("action") or "save")
    if name not in botcreds.known():
        return 400, {"error": "Unknown bot %r." % name}
    if act not in ACTIONS:
        return 400, {"error": "Unknown action %r." % act}
    try:
        row = botcreds.get(name)
    except Exception as e:                             # noqa: BLE001
        return 503, {"error": "The bot registry is not readable: %s" % e}

    if act == "delete":
        botcreds.delete(name)
        return 200, {"ok": True, "detail": "Forgotten. The %s service goes back to its .env "
                                           "values when it next restarts." % name}
    if act == "clear":
        if not row:
            return 200, {"ok": True, "detail": "Nothing stored."}
        botcreds.save(name, actor, token="")
        return 200, {"ok": True, "detail": "Token forgotten; the service restarts on its .env token."}

    if act in ("refresh", "endpoint"):
        token = (row or {}).get("token") or ""
        if not token:
            return 400, {"error": "No usable token is stored for %s%s." % (
                name, " (it was sealed under another POL_BOT_KEY)" if (row or {}).get("has_token") else "")}
        if act == "refresh":
            info = describe(token)
            if info.get("error"):
                return 502, {"error": info["error"]}
            botcreds.save(name, actor, app_id=info["app_id"], public_key=info["public_key"],
                          username=info["username"], discord_info=json.dumps(info))
            return 200, {"ok": True, "discord": info}
        url = (row or {}).get("url") or ""
        if not url:
            return 400, {"error": "Set the board's page address first."}
        ok, detail = set_endpoint(token, endpoint_of(url))
        if ok:                                         # refresh the checklist's copy
            info = describe(token)
            if not info.get("error"):
                botcreds.save(name, actor, discord_info=json.dumps(info))
        return (200 if ok else 502), ({"ok": True, "detail": detail} if ok else {"error": detail})

    # save
    fields = {}
    try:
        if "url" in body:
            fields["url"] = _clean_url(body.get("url"))
    except ValueError as e:
        return 400, {"error": str(e)}
    token = str(body.get("token") or "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    info = None
    if token:
        if not botcreds.can_seal():
            return 400, {"error": "POL_BOT_KEY is not set in .env, so tokens cannot be stored. "
                                  "Add it (any long random string) to the admin and bot services."}
        info = describe(token)
        if info.get("error"):
            return 400, {"error": info["error"]}
        fields.update(app_id=info["app_id"], public_key=info["public_key"], username=info["username"],
                      discord_info=json.dumps(info))
    if not token and not fields:
        return 400, {"error": "Nothing to save: paste a token or a page address."}
    botcreds.save(name, actor, token=token or None, **fields)
    out = {"ok": True, "detail": "Saved. The %s service picks it up within about a minute." % name}
    if info:
        out["discord"] = info
        out["invite"] = botcreds.invite_url(name, info["app_id"])
    return 200, out

