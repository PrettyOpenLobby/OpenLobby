"""The admin page's Bots tab (Discord and Fluxer): one row per bot (polcore/botcreds.py
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

         fluxer-save   the Fluxer bot's token and/or {"channels": {feed key:
                       "id,id"}}. A token is checked with Fluxer first
                       (GET /users/@me), and so is every channel
         fluxer-check  ask Fluxer again with the stored token
         fluxer-clear  forget the Fluxer token and channels (the board goes
                       back to its POL_BOARDS_<NAME>_FLUXER_* values)

Fluxer is a board's second bot, beside Discord: Fluxer has no slash commands
or interactions, so there is no endpoint to set and the channels are named
here. The PlayOnline bridge posts its news on Fluxer: its one "feed" is "announce",
and the role each news content pings is stored beside it as "role:<content>".

A saved change reaches the bot's service within POL_BOT_RELOAD_EVERY (60 s):
the service sees its row move and restarts itself on the new values.
"""
import json
import re

from polcore import botcreds

ACTIONS = ("save", "refresh", "endpoint", "clear", "delete", "fluxer-save", "fluxer-check",
           "fluxer-clear")
_ID = re.compile(r"^\d{5,25}$")


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


def _fluxer_info(r):
    try:
        got = json.loads(r.get("fluxer_info") or "")
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def feeds(name):
    """[{key, label, shares_main}] of the messages board `name` posts, the board itself first.
    The bridge posts one thing on Fluxer: its news (announcements, cup reminders, game
    events), under "announce"."""
    if name == "bridge":
        return [{"key": "announce", "label": "News", "shares_main": False}]
    out = [{"key": key, "label": "Board" if not feed else "%s feed" % feed.capitalize(),
            "shares_main": key in botcreds.SHARES_MAIN} for key, feed in botcreds.feed_keys(name)]
    if name in botcreds.NEWS_BOARDS:
        # this game's news, events and reminders, posted by the bridge AS THIS BOT
        out.append({"key": "%s_news" % name, "label": "Game news", "shares_main": False})
    return out


#: the bridge's news contents and their names, for its per-game ping roles on Fluxer
#: (newsgen's own table when it loads, so a content added there shows up here too)
_CONTENTS = {"playonline": "PlayOnline", "ffxi": "FINAL FANTASY XI", "tetra": "Tetra Master",
             "jan": "JongHoLow", "fmo": "Front Mission Online", "doc": "Dirge of Cerberus",
             "fe": "Fantasy Earth"}


def news_contents():
    try:
        import newsgen
        return dict(newsgen.CONTENT_LABELS)
    except Exception:                                  # noqa: BLE001
        return dict(_CONTENTS)


def bridge_roles(r, applied=None):
    """[{key, label, value, running}]: the role each news content pings on Fluxer, stored in
    the bridge's fluxer_channels as "role:<content>"; `running` is its .env value."""
    stored = botcreds.fluxer_channels(r)
    run = (applied or {}).get("fluxer_channels") or {}
    return [{"key": "role:%s" % c, "label": label, "value": stored.get("role:%s" % c, ""),
             "running": run.get("role:%s" % c, "")} for c, label in news_contents().items()]


def fluxer_checklist(name, r, applied=None):
    """The Fluxer side's checklist, as checklist() for Discord. `applied` is what the board
    said it runs on (botcreds:applied:<name>): its .env channels count where the registry
    names none."""
    out = []

    def add(state, label, detail):
        out.append({"state": state, "label": label, "detail": detail})

    chans = botcreds.fluxer_channels(r)
    env_chans = (applied or {}).get("fluxer_channels") or {}
    for key, ids in env_chans.items():
        chans.setdefault(key, ids)                 # what the board runs on from its .env
    on_env = (applied or {}).get("fluxer_source") == "env"
    info = None
    if r.get("fluxer_has_token"):
        info = _fluxer_info(r)
        if not info:
            add("info", "Fluxer settings", "Not checked yet: press Check.")
        else:
            add("ok", "Bot", "Signs in as %s." % (info.get("username") or info.get("user_id") or "?"))
            if info.get("guilds") is not None:
                add("ok" if info["guilds"] else "todo", "Servers",
                    ("In %s." % ", ".join(info["guilds"])) if info["guilds"] else
                    "Not in any server yet: add it to the server from its application page in Fluxer.")
    elif on_env:
        add("ok", "Bot token", "Running on its .env token. Paste it here to check it from this page.")
    else:
        add("todo", "Bot token", "Paste the bot token from Fluxer (your application's Bot page).")
        return out
    seen = (info or {}).get("channels") or {}
    if name == "bridge":
        pinged = [x["label"] for x in bridge_roles(r, applied) if x["value"] or x["running"]]
        add("info", "Ping roles", ("Pings a role for %s." % ", ".join(pinged)) if pinged else
            "None: news posts ping nobody. Paste a role id beside a game to ping it.")
    for f in feeds(name):
        ids = [c for c in (chans.get(f["key"]) or "").split(",") if c]
        if not ids:
            if name == "bridge":
                add("todo", "News channels", "None: no news posts on Fluxer. Paste the id of the "
                    "channel they should go in.")
            elif f["key"] == name:
                add("todo", "Board channels", "None: the board posts nowhere on Fluxer. Paste the "
                    "id of the channel it should post in.")
            else:
                add("info", "%s channels" % f["label"], "None of its own: it posts beside the board."
                    if f["shares_main"] else "None: this feed is off on Fluxer.")
            continue
        where = " (from .env)" if f["key"] not in botcreds.fluxer_channels(r) else ""
        if not info:
            add("info", "%s channels" % f["label"], ", ".join(ids) + where)
            continue
        bad = [c for c in ids if (seen.get(c) or {}).get("error") or c not in seen]
        names = ["#%s%s" % (seen[c].get("name") or c, (" in %s" % seen[c]["guild"]) if seen[c].get("guild") else "")
                 for c in ids if c in seen and not seen[c].get("error")]
        add("warn" if bad else "ok", "%s channels" % f["label"],
            ", ".join(names + ["%s: %s" % (c, (seen.get(c) or {}).get("error") or "not checked yet")
                               for c in bad]) + where)
    return out


def _fluxer_view(name, r, applied):
    info = _fluxer_info(r) or {}
    applied = applied or {}
    extra = {"roles": bridge_roles(r, applied)} if name == "bridge" else {}
    return {**extra, "has_token": bool(r.get("fluxer_has_token")), "token_ok": bool(r.get("fluxer_token_ok")),
            "token_hint": r.get("fluxer_token_hint") or "", "username": info.get("username") or "",
            "channels": botcreds.fluxer_channels(r), "feeds": feeds(name),
            "checklist": fluxer_checklist(name, r, applied), "checked_at": info.get("checked_at"),
            "running_channels": applied.get("fluxer_channels") or {},
            "source": applied.get("fluxer_source"),
            "live": bool(r and applied.get("fluxer_source") == "registry"
                         and float(applied.get("row_at") or 0) >= float(r.get("updated_at") or 0))}


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
            # game-only news on Discord, posted by the bridge as this bot (None: no news)
            "news_channels": (botcreds.discord_channels(r).get("%s_news" % name, "")
                              if name in botcreds.NEWS_BOARDS else None),
            "endpoint": endpoint_of(r.get("url") or ""),
            "applied": applied,
            # the service has restarted since the last change, on the registry's token
            "live": bool(applied and r and float(applied.get("row_at") or 0) >= float(r.get("updated_at") or 0)
                         and applied.get("source") == "registry"),
            "fluxer": _fluxer_view(name, r, applied),
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


def clean_ids(text, where="Fluxer"):
    """"id,id" from channel ids or links, split on commas, spaces or new lines. Raises
    ValueError naming what does not fit."""
    ids = []
    for part in re.split(r"[\s,]+", str(text or "").strip()):
        if not part:
            continue
        m = re.search(r"(\d+)/?$", part)
        if not m or not _ID.match(m.group(1)):
            raise ValueError("%r is not a %s channel id. In %s, right-click the channel "
                             "and copy its id." % (part[:60], where, where))
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ",".join(ids)


def clean_channels(name, given):
    """{feed key: "id,id"} from what the page sent: each value a list of channel ids (or channel
    links, whose last number is the channel), split on commas, spaces or new lines. Raises
    ValueError naming what does not fit."""
    if not isinstance(given, dict):
        raise ValueError("Channels must be given per feed.")
    keys = [f["key"] for f in feeds(name)]
    if name == "bridge":
        keys += ["role:%s" % c for c in news_contents()]
    out = {}
    for key, text in given.items():
        if key not in keys:
            raise ValueError("The %s board has no feed %r." % (name, key))
        if key.startswith("role:"):
            m = re.search(r"(\d+)>?$", str(text or "").strip())
            if str(text or "").strip() and not (m and _ID.match(m.group(1))):
                raise ValueError("%r is not a Fluxer role id. In Fluxer, right-click the role "
                                 "and copy its id." % str(text)[:60])
            if m:                                  # an empty field: the .env value stands
                out[key] = m.group(1)
            continue
        ids = []
        for part in re.split(r"[\s,]+", str(text or "").strip()):
            if not part:
                continue
            m = re.search(r"(\d+)/?$", part)
            if not m or not _ID.match(m.group(1)):
                raise ValueError("%r is not a Fluxer channel id. In Fluxer, right-click the channel "
                                 "and copy its id." % part[:60])
            if m.group(1) not in ids:
                ids.append(m.group(1))
        if ids:                                    # an empty field: the board's .env value stands
            out[key] = ",".join(ids)
    return out


def _fluxer_change(name, act, body, actor, row, describe):
    what = "The PlayOnline bridge" if name == "bridge" else "The %s board" % name
    if act == "fluxer-clear":
        if not row:
            return 200, {"ok": True, "detail": "Nothing stored."}
        botcreds.save(name, actor, fluxer_token="", fluxer_channels="", fluxer_info="")
        return 200, {"ok": True, "detail": "Fluxer token and channels forgotten; %s goes back to "
                                           "its .env values for Fluxer." % (what[0].lower() + what[1:])}
    chans = botcreds.fluxer_channels(row)
    if act == "fluxer-save" and "channels" in body:
        # the page sends every feed's field, so what it sends is the whole set
        try:
            chans = clean_channels(name, body.get("channels"))
        except ValueError as e:
            return 400, {"error": str(e)}
    token = str(body.get("token") or "").strip() if act == "fluxer-save" else ""
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if token and not botcreds.can_seal():
        return 400, {"error": "POL_BOT_KEY is not set in .env, so tokens cannot be stored. "
                              "Add it (any long random string) to the admin and bot services."}
    use = token or (row or {}).get("fluxer_token") or ""
    if act == "fluxer-check" and not use:
        return 400, {"error": "No usable Fluxer token is stored for %s%s." % (
            name, " (it was sealed under another POL_BOT_KEY)" if (row or {}).get("fluxer_has_token") else "")}
    if act == "fluxer-save" and not token and "channels" not in body:
        return 400, {"error": "Nothing to save: paste a Fluxer token or channel ids."}
    info = None
    if use:
        # the channels the board posts in: these, and its .env ones for the feeds these leave out
        run = dict(((_applied(name) or {}).get("fluxer_channels") or {}), **chans)
        ids = sorted({c for k, v in run.items() if not k.startswith("role:")
                      for c in str(v).split(",") if c})
        info = describe(use, ids)
        if info.get("error"):
            if token or act == "fluxer-check":
                return (400 if token else 502), {"error": info["error"]}
            info = None                            # the stored token: keep the channels anyway
    fields = {"fluxer_channels": json.dumps(chans, sort_keys=True)} if act == "fluxer-save" else {}
    if info:
        fields["fluxer_info"] = json.dumps(info)
    botcreds.save(name, actor, fluxer_token=token or None, **fields)
    out = {"ok": True, "fluxer": info,
           "detail": ("Checked." if act == "fluxer-check" else
                      "Saved. %s picks it up within about a minute." % what)}
    return 200, out


def change(body, actor, describe=None, set_endpoint=None, fluxer_describe=None):
    """(status, body) for POST /api/bots. describe / set_endpoint / fluxer_describe stand in
    for the Discord and Fluxer calls in tests."""
    describe = describe or botcreds.describe
    set_endpoint = set_endpoint or botcreds.set_endpoint
    fluxer_describe = fluxer_describe or botcreds.fluxer_describe
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

    if act.startswith("fluxer-"):
        return _fluxer_change(name, act, body, actor, row, fluxer_describe)

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
        if "news_channels" in body and name in botcreds.NEWS_BOARDS:
            got = botcreds.discord_channels(row)
            ids = clean_ids(body.get("news_channels"), "Discord")
            if ids:
                got["%s_news" % name] = ids
            else:
                got.pop("%s_news" % name, None)
            fields["discord_channels"] = json.dumps(got, sort_keys=True)
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
        return 400, {"error": "Nothing to save: paste a token, a page address or channel ids."}
    botcreds.save(name, actor, token=token or None, **fields)
    out = {"ok": True, "detail": "Saved. The %s service picks it up within about a minute." % name}
    if info:
        out["discord"] = info
        out["invite"] = botcreds.invite_url(name, info["app_id"])
    return 200, out

