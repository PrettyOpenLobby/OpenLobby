"""gmconsole.py -- GM Call handling from Discord.

The existing GM Call path already alerts a webhook and DMs on-duty GMs when a
ticket lands (adminops.Worker.announce). This adds a THIRD channel: a bound
Discord channel where the bot posts each call with a Knock button, and a
knock opens a private thread for the responder that relays both halves of the
in-game GM chat back and forth. It's meant for GMs away from the admin panel,
and it composes cleanly with the DM path -- an on-duty GM still gets DM'd,
they just also see (and can claim) the call from the shared channel.

Loaded by polbridge.py, which owns the bot's Discord app and the interactions
endpoint. This module holds only the alert / thread / relay bookkeeping and
mirrors polbridge's shape for `/playonline announce` (bind_announce +
scan_announce): a per-guild channel binding, a baseline so switching on never
dumps the backlog, and a watcher that catches up new tickets each tick.

CONFIDENTIALITY:

  * The alerts CHANNEL is expected to be role-gated on the Discord side. That
    is the only place a channel permission actually keeps eyes out.
  * The per-call THREAD is a private thread (type 12, invitable=false); only
    the knocking GM is added. A second GM's knock is told the call is already
    being handled and by whom; handover is a follow-up, not a free-for-all.
  * Mentions are stripped from every Discord-side line before it is spooled
    to gmchat, so `<@123>` never reaches the caller.
"""
import json
import os
import re
import secrets
import time

import accounts
import discordlink
import gmchat

GM_MAIL_FROM = os.environ.get("POL_GM_MAIL_FROM", "PlayOnline GM <gm@pol.com>")

#: The Viewer's mailer reads ISO-8859-1; typographic characters get a plain
#: stand-in instead of a '?'. Mirrors admin._latin1.
_LATIN1 = str.maketrans({chr(c): s for c, s in (
    (0x2018, "'"), (0x2019, "'"), (0x201C, '"'), (0x201D, '"'),
    (0x2013, "-"), (0x2014, "-"), (0x2026, "..."), (0x00A0, " "))})


def _latin1(s):
    return str(s or "").translate(_LATIN1).encode("latin-1", "replace").decode("latin-1")


def _gm_mail(to_addr, subject, text):
    """One RFC822 message from the GM. Same shape admin._gm_mail produces."""
    subject = re.sub(r"[\r\n\t]+", " ", _latin1(subject)).strip()[:120]
    text = _latin1(text).replace("\r\n", "\n").replace("\r", "\n")
    head = ["From: " + _latin1(GM_MAIL_FROM),
            "To: " + to_addr,
            "Subject: " + subject,
            "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()),
            "Message-Id: <gm-%s@pol.com>" % secrets.token_hex(8),
            "MIME-Version: 1.0",
            "Content-Type: text/plain; charset=ISO-8859-1",
            "Content-Transfer-Encoding: 8bit"]
    body = "\r\n".join(head) + "\r\n\r\n" + text.replace("\n", "\r\n") + "\r\n"
    return body.encode("latin-1", "replace"), subject


def _handle_for_nick(db, nick):
    """A chat nick is the scrambled POL ID ('U' + 8 characters). Returns the
    speaker's PlayOnline handle name, or None. Same as admin._handle_for_nick."""
    try:
        import polnick
        polid = polnick.polid_for_nick(nick)
        row = db.execute("SELECT id FROM member WHERE polid = %s",
                         (polid,)).fetchone()
        h = accounts.primary_handle_row(db, row["id"]) if row else None
        return h["handle_name"] if h else None
    except Exception:                                             # noqa: BLE001
        return None

log = None                          # polbridge injects its logger on wire()


def _log(msg):
    if log is not None:
        try:
            log("gmconsole: " + msg)
            return
        except Exception:                                          # noqa: BLE001
            pass
    print("[gmconsole] " + msg, flush=True)


DATA_DIR = os.environ.get("POL_DATA_DIR", "/data")
GM_CALL_DIR = os.environ.get("POL_GM_CALL_DIR",
                             os.environ.get("POL_GMD_TICKET_DIR",
                                            os.path.join(DATA_DIR, "gm-calls")))

_TICKET_NAME = re.compile(r"gm-\d{8}T\d{6}-\d+\.json")
_MD = re.compile(r"([\\*_~`|>#\[\]()-])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_STRIP_MENTION = re.compile(r"<@[!&]?\d+>|<#\d+>")

#: An in-thread text longer than this is truncated with a marker; the tail is
#: still in the panel's transcript, which is the archive.
RELAY_MAX = 1500

#: Seed last_relay_at this far in the past on knock, so the first relay tick
#: back-fills recent player chat. Keeps the knock interaction inside Discord's
#: 3-second budget by doing the (potentially many) history posts off the tick.
HISTORY_WINDOW_S = float(os.environ.get("POL_BRIDGE_GM_HISTORY_WINDOW", "1800"))

#: How long a knock puts the responder on duty for. A GM handling one call
#: from Discord is normally done inside this; if not, `/gm duty on hours:N`
#: extends it. Without this, gmd would keep serving the off-duty flags on the
#: caller's 0x801 poll and the player's screen would never light up Join.
KNOCK_DUTY_S = int(os.environ.get("POL_BRIDGE_GM_KNOCK_DUTY_HOURS", "4")) * 3600

#: A ticket younger than this is left for the next pass -- gmd writes the JSON
#: in place and a read can land mid-write. Same rule as polbridge's message
#: watcher (MIN_AGE) and the panel's alerter (adminops).
TICKET_MIN_AGE = float(os.environ.get("POL_BRIDGE_GM_TICKET_MIN_AGE", "1.0"))


# --------------------------------------------------------------------------- #
# wiring: polbridge injects the shared api()/log helpers on start
# --------------------------------------------------------------------------- #
_api = None


def wire(api_fn, logger=None):
    global _api, log
    _api = api_fn
    if logger is not None:
        log = logger


def _need_api():
    if _api is None:
        raise RuntimeError("gmconsole.wire() has not been called")
    return _api


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #

def _md(text):
    return _MD.sub(r"\\\1", str(text or ""))


def _clean(text, limit=None):
    text = _CTRL.sub(" ", str(text or "")).strip()
    return text[:limit] if limit else text


def _ticket_id(name):
    return name[:-len(".json")] if name.endswith(".json") else name


def _list_tickets():
    try:
        return sorted(n for n in os.listdir(GM_CALL_DIR) if _TICKET_NAME.fullmatch(n))
    except OSError:
        return []


def _read_ticket(ticket_id):
    if not _TICKET_NAME.fullmatch(ticket_id + ".json"):
        return None
    try:
        with open(os.path.join(GM_CALL_DIR, ticket_id + ".json"),
                  encoding="utf-8", errors="replace") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


#: The admin panel's ticket-state file, alongside the ticket JSONs. gmd never
#: writes it (its own state lives in `gm-control.json` / `gm-serving.json` /
#: `gm-sessions.json`), so a write here is safe as long as it stays atomic.
#: Same shape and location the admin panel uses (see admin.py GM_TICKET_STATE).
_TICKET_STATE_PATH = os.path.join(GM_CALL_DIR, "gm-tickets.json")


def _update_ticket_state(ticket_id, fn):
    """Atomic read-modify-write of the panel's ticket-state file. `fn(cur)`
    mutates the ticket's dict in place. NEVER raises."""
    try:
        st = {}
        try:
            with open(_TICKET_STATE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                st = loaded
        except (OSError, ValueError):
            pass
        cur = dict(st.get(ticket_id) or {})
        cur.setdefault("status", "open")
        fn(cur)
        st[ticket_id] = cur
        tmp = _TICKET_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, _TICKET_STATE_PATH)
    except OSError as e:
        _log("could not update ticket %s state: %r" % (ticket_id, e))


def _read_ticket_state():
    """The panel's whole ticket-state dict, or {}. NEVER raises."""
    try:
        with open(_TICKET_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _mark_ticket_knocked(ticket_id, by):
    """Write the panel's knocked-at marker. gmd reads this on the caller's next
    0x801 and only then sets FLAG_JOIN (0x40) so the player's screen lights up
    the Join button -- desk-wide duty alone is not enough on the newer gmd."""
    now = time.time()
    _update_ticket_state(ticket_id,
                         lambda cur: cur.update(knocked_at=now, knocked_by=by))


#: Same shape the admin panel uses (admin._request_knock).
_KNOCK_QUEUE = "gmknock:queue"
_KNOCK_REPLY = "gmknock:reply:"
_KICK_WAIT = float(os.environ.get("POL_BRIDGE_GM_KNOCK_WAIT", "3.0"))


def _push_knock(ticket, action=0):
    """Ask the running authsess to send `ticket`'s player a live GM knock
    (action 0) or a close (2). Mirrors admin._request_knock exactly -- same
    queue key, same envelope, same reply-timeout shape -- so authsess treats a
    Discord knock indistinguishably from a panel one. Returns the answer dict,
    or {"error": ...}. NEVER raises."""
    try:
        from polcore import kv
    except Exception as e:                                        # noqa: BLE001
        return {"error": "kv unavailable: %r" % e}
    key = os.environ.get("POL_GMD_CHAT_KEY") or "gmkey"
    if action == 0 and not ticket.get("room"):
        return {"error": "this request has no chat room to knock for"}
    reply = _KNOCK_REPLY + secrets.token_hex(8)
    raw = json.dumps({"reply": reply, "expires": time.time() + _KICK_WAIT,
                      "handle": ticket.get("handle") or "",
                      "guid": ticket.get("guid") or 0,
                      "room": ticket.get("room") or "",
                      "key": key,
                      "request_no": ticket.get("request_no") or 0,
                      "action": action})
    try:
        kv.push(_KNOCK_QUEUE, raw)
    except Exception as e:                                        # noqa: BLE001
        return {"error": "kv push: %r" % e}
    deadline = time.monotonic() + _KICK_WAIT
    answer = None
    try:
        while answer is None and time.monotonic() < deadline:
            answer = kv.pop(reply,
                            timeout=max(0.1, min(1.0, deadline - time.monotonic())))
    except Exception as e:                                        # noqa: BLE001
        return {"error": "kv pop: %r" % e}
    if answer is None:
        try:
            kv.lrem(_KNOCK_QUEUE, raw)
        except Exception:                                         # noqa: BLE001
            pass
        return {"error": "no auth service answered in %.1fs" % _KICK_WAIT}
    try:
        return json.loads(answer)
    except ValueError:
        return {"error": "auth service reply was not json"}


def _mail_transcript(ticket_id, resolution="", invited=(), by="discord"):
    """Mail request `ticket_id`'s chat + the GM's resolution note to the
    requester and every player invited into the call. Mirrors admin._gm_mail_transcript
    step-for-step so a Discord close produces the same mail a panel close does.
    Returns a one-line summary suitable for logging. NEVER raises."""
    rec = _read_ticket(ticket_id) or {}
    room = str(rec.get("room") or "")
    try:
        db = accounts.connect()
    except Exception as e:                                        # noqa: BLE001
        return "not mailed: could not open accounts db (%r)" % e
    try:
        def mailbox(h):
            if h is None:
                return ""
            row = db.execute("SELECT mail_address FROM member WHERE id = %s",
                             (h["member_id"],)).fetchone()
            addr = (row["mail_address"] if row else "") or ""
            return addr if "@" in addr else ""
        h = (accounts.handle_by_name(db, rec.get("handle"))
             if rec.get("handle") else None)
        if h is None and rec.get("guid"):
            h = accounts.handle_by_client_guid(db, rec["guid"])
        to_all = []
        for addr in [mailbox(h)] + [mailbox(accounts.handle_by_client_guid(db, i.get("guid") or 0))
                                    for i in (invited or []) if isinstance(i, dict)]:
            if addr and addr not in to_all:
                to_all.append(addr)
        if not to_all:
            return "not mailed: nobody in the call has a PlayOnline Mail address"
        lines, names = [], {}
        for r in gmchat.transcript(room.encode(), 2000) if room else []:
            try:
                raw = bytes.fromhex(r.get("raw") or "")
            except ValueError:
                continue
            if raw[:1] != b"T" or gmchat.SEP not in raw:
                continue
            text = raw.split(gmchat.SEP, 1)[1].decode("cp932", "replace").strip()
            if not text:
                continue
            if r.get("dir") == "out":
                who = "GM"
            else:
                nick = r.get("nick") or ""
                if nick not in names:
                    names[nick] = _handle_for_nick(db, nick) or "Player"
                who = names[nick]
            when = time.strftime("%H:%M", time.gmtime(r.get("at") or 0))
            lines.append("[%s] %s: %s" % (when, who, text))
        if not lines and not resolution:
            return "not mailed: no chat took place and no resolution was written"
        n = rec.get("request_no") or "?"
        subj = re.sub(r"[\x00-\x1f\x7f]", "", str(rec.get("subject") or ""))
        head = ["Request #%s: %s" % (n, subj),
                "Filed: %s" % (rec.get("received_at") or "?"), ""]
        if resolution:
            head += ["Resolution:", resolution, ""]
        chat = (["Chat (times are UTC):"] + lines) if lines else ["No chat took place."]
        body = "\n".join(head + chat + ["", "PlayOnline GM"])
        sent = []
        try:
            for to in to_all:
                raw, subject = _gm_mail(
                    to, "Your GM Call has been closed (request #%s)" % n, body)
                accounts.deliver_mail(db, to, raw, sender=GM_MAIL_FROM,
                                      subject=subject)
                sent.append(to)
        except Exception as e:                                    # noqa: BLE001
            return "mailed to %s; then failed: %r" % (", ".join(sent) or "nobody", e)
    finally:
        try:
            db.close()
        except Exception:                                         # noqa: BLE001
            pass
    # Note it on the panel state so the dashboard can show "transcript sent".
    _update_ticket_state(ticket_id,
                         lambda cur: cur.update(transcript_at=time.time()))
    return "mailed to %s (%d line(s))" % (", ".join(sent), len(lines))


def _mark_ticket_closed(ticket_id, by):
    """Mark the ticket closed and clear the knock/invite bookkeeping. Same
    shape the admin panel writes; the panel and this both write atomically to
    the same file (last-writer-wins on a rare race, never half-written)."""
    def fn(cur):
        cur.update(status="closed", by=by, at=time.time())
        cur.pop("invited", None)
        cur.pop("knocked_at", None)
        cur.pop("knocked_by", None)
    _update_ticket_state(ticket_id, fn)


def _content_label(cid):
    try:
        cid = int(cid) if cid is not None else 0
    except (TypeError, ValueError):
        return ""
    return {1: "FFXI", 2: "TM", 3: "DoC", 6: "FMO", 7: "FE"}.get(cid, "") \
        or (("content %d" % cid) if cid else "")


# --------------------------------------------------------------------------- #
# alert message: an embed + Knock button
# --------------------------------------------------------------------------- #

def _knock_component(ticket_id, claimed_by=None):
    if claimed_by:
        return [{"type": 1, "components": [
            {"type": 2, "style": 2,
             "label": ("Claimed by %s" % claimed_by)[:80],
             "disabled": True,
             "custom_id": "pol:knock:claimed:%s" % ticket_id}]}]
    return [{"type": 1, "components": [
        {"type": 2, "style": 3,
         "label": "Knock (open a private thread)",
         "custom_id": "pol:knock:%s" % ticket_id}]}]


def _alert_embed(rec):
    handle = _clean(rec.get("handle"), 40) or "a player"
    subject = _clean(rec.get("subject"), 200) or "(no subject)"
    body = _clean(rec.get("body"), 1500) or ""
    title = "GM call"
    lbl = _content_label(rec.get("content_id"))
    if lbl:
        title += " -- " + lbl
    fields = [{"name": "From", "value": _md(handle), "inline": True}]
    if rec.get("request_no"):
        fields.append({"name": "#", "value": str(rec["request_no"]), "inline": True})
    if rec.get("room"):
        fields.append({"name": "Room", "value": _md(rec["room"]), "inline": True})
    return {
        "title": title,
        "description": ("**" + _md(subject) + "**\n" + _md(body)) if body
                       else "**" + _md(subject) + "**",
        "fields": fields,
        "color": 0xC03030,
    }


def _post_alert(ldb, row, ticket_id, rec):
    """Post one alert into one bound channel. Records or updates the alert row
    so a re-post moves the message id forward. Returns True on 2xx."""
    api = _need_api()
    role_id = row["role_id"]                    # nullable column; None when unset
    ping = ("<@&%s> " % role_id) if role_id else ""
    payload = {
        "content": ping,
        "embeds": [_alert_embed(rec)],
        "components": _knock_component(ticket_id),
        "allowed_mentions": {"parse": [],
                             "roles": [role_id] if role_id else []},
    }
    st, data = api("POST", "/channels/%s/messages" % row["channel_id"], payload)
    if st not in (200, 201) or not data.get("id"):
        _log("post_alert %s -> channel %s failed (%s %s)"
             % (ticket_id, row["channel_id"], st, str(data)[:160]))
        return False
    discordlink.gm_alert_record(ldb, ticket_id, rec.get("room") or "",
                                row["guild_id"], row["channel_id"], str(data["id"]))
    return True


def scan_new_tickets(now=None):
    """One pass over the ticket store across every bound guild. Baselines on
    the first bind (bind_gm_channel writes the baseline). Returns how many
    alerts went out."""
    if _api is None:
        return 0
    now = time.time() if now is None else now
    names = _list_tickets()
    if not names:
        return 0
    ldb = discordlink.connect()
    try:
        rows = discordlink.gm_channel_rows(ldb)
        if not rows:
            return 0
        posted = 0
        for row in rows:
            last = str(row["last_ticket"] or "")
            new = [n for n in names if _ticket_id(n) > last]
            for name in new:
                path = os.path.join(GM_CALL_DIR, name)
                try:
                    if now - os.path.getmtime(path) < TICKET_MIN_AGE:
                        continue                # still being written; next pass
                except OSError:
                    continue
                tid = _ticket_id(name)
                rec = _read_ticket(tid)
                if rec is None:
                    # Unreadable ticket: skip but bump the baseline so we do
                    # not spin on it forever. Real tickets are dict-shaped.
                    discordlink.bump_gm_last_ticket(ldb, row["guild_id"], tid)
                    continue
                if _post_alert(ldb, row, tid, rec):
                    posted += 1
                    discordlink.bump_gm_last_ticket(ldb, row["guild_id"], tid)
                else:
                    break                        # try again next tick
        return posted
    finally:
        ldb.close()


# --------------------------------------------------------------------------- #
# Knock and Close
# --------------------------------------------------------------------------- #

def _member_display(interaction):
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return (member.get("nick") or user.get("global_name")
            or user.get("username") or "GM")


def _member_user_id(interaction):
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return str(user.get("id") or "")


def _ephemeral(text):
    return {"type": 4, "data": {"flags": 64, "content": text[:1900],
                                "allowed_mentions": {"parse": []}}}


def _knocker_is_gm(interaction):
    """True iff the invoking Discord user is linked to a GM desk account. Uses
    the same gmduty binding that `/gm duty` uses, so 'is a GM' means the same
    thing in both places."""
    import adminusers
    import gmduty
    uid = _member_user_id(interaction)
    if not uid:
        return None
    conn = adminusers.connect()
    try:
        return gmduty.account_for(conn, uid)
    finally:
        conn.close()


def _thread_starter(ticket_id, rec, knocker_display):
    handle = _clean(rec.get("handle"), 40) or "a player"
    subject = _clean(rec.get("subject"), 200) or "(no subject)"
    body = _clean(rec.get("body"), 1500) or "(no body)"
    room = rec.get("room") or ""
    hint = ("Type in this thread to answer. Messages you send here are relayed "
            "to the player in-game as **%s**; the player's chat comes back as "
            "bot posts. Close the call with the button below when it's done."
            % _md(knocker_display))
    return {
        "embeds": [{
            "title": "GM call #%s from %s" % (rec.get("request_no") or "?",
                                              _md(handle)),
            "description": "**" + _md(subject) + "**\n" + _md(body),
            "fields": ([{"name": "Room", "value": _md(room), "inline": True}]
                       if room else []) +
                      [{"name": "Ticket", "value": ticket_id, "inline": True}],
            "color": 0xC03030,
        }, {"description": hint, "color": 0x606060}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 4, "label": "Close call",
             "custom_id": "pol:gmclose:%s" % ticket_id}]}],
        "allowed_mentions": {"parse": []},
    }


def on_knock(interaction, ticket_id):
    """`pol:knock:<ticket_id>` -> create a private thread and bind the room.

    Inline API calls: create thread + add member + starter post + PATCH the
    alert. Four calls; comfortably inside the 3-second budget."""
    api = _need_api()
    gm_user = _knocker_is_gm(interaction)
    if not gm_user:
        return _ephemeral("This Discord account is not linked to a GM desk "
                          "account. Run `/gm link` and enter the code on the "
                          "admin panel first.")
    rec = _read_ticket(ticket_id)
    if rec is None:
        return _ephemeral("That ticket is gone or unreadable.")
    if rec.get("cancelled_at"):
        # Same reason admin refuses this: gmd will not offer Join, authsess
        # refuses the room (473), so a knock would only look sent.
        return _ephemeral("The player cancelled this request -- there's no "
                          "chat to knock into.")
    room = rec.get("room") or ""
    if not gmchat.is_gm_room(room.encode() if room else b""):
        return _ephemeral("That call has no GM chat room -- gmd may be down.")
    state = _read_ticket_state().get(ticket_id) or {}
    if state.get("status") == "closed":
        return _ephemeral("That call is already closed.")
    if state.get("knocked_at"):
        return _ephemeral("That call was already taken by %s."
                          % _claimant(state.get("knocked_by")))

    ldb = discordlink.connect()
    try:
        existing = discordlink.gm_thread_by_room(ldb, room)
        if existing is not None:
            return _ephemeral(
                "Call already being handled by <@%s> in <#%s>."
                % (existing["knocker_id"], existing["thread_id"]))
        alert = discordlink.gm_alert_get(ldb, ticket_id)
    finally:
        ldb.close()

    if alert is None:
        return _ephemeral("Lost track of the alert message for that call. "
                          "Ask the panel to close and re-post it.")

    knocker_id = _member_user_id(interaction)
    knocker_name = ((interaction.get("member") or {}).get("user") or {}).get("username")
    knocker_display = _member_display(interaction)

    # 1) create the private thread
    st, data = api("POST", "/channels/%s/threads" % alert["channel_id"], {
        "name": ("GM call #%s -- %s" % (rec.get("request_no") or "?",
                                        _clean(rec.get("handle"), 40) or "player"))[:100],
        "type": 12,                     # PRIVATE_THREAD
        "invitable": False,
        "auto_archive_duration": 1440,
    })
    if st not in (200, 201) or not data.get("id"):
        _log("thread create failed: %s %s" % (st, str(data)[:200]))
        return _ephemeral("Could not create the thread (%s)." % st)
    thread_id = str(data["id"])

    # 2) add the knocker
    add_st, add_data = api("PUT", "/channels/%s/thread-members/%s"
                           % (thread_id, knocker_id))
    if add_st not in (200, 201, 204):
        _log("thread-member add failed: %s %s" % (add_st, str(add_data)[:160]))

    # 3) starter post + close button
    api("POST", "/channels/%s/messages" % thread_id,
        _thread_starter(ticket_id, rec, knocker_display))

    # 4) bind the room; last_relay_at seeded HISTORY_WINDOW_S back so the first
    #    relay tick back-fills recent player chat.
    seed = time.time() - HISTORY_WINDOW_S
    ldb = discordlink.connect()
    try:
        discordlink.gm_thread_open(ldb, room, ticket_id, thread_id,
                                   alert["guild_id"], alert["channel_id"],
                                   knocker_id, knocker_name, knocker_display,
                                   last_relay_at=seed)
        discordlink.gm_alert_claim(ldb, ticket_id, knocker_id)
    finally:
        ldb.close()

    # 5) disable the Knock button on the original alert
    api("PATCH", "/channels/%s/messages/%s"
        % (alert["channel_id"], alert["message_id"]),
        {"components": _knock_component(ticket_id, claimed_by=knocker_display)})

    # 6) knock the ticket + go on duty + push a live knock. THREE things, and
    # the third is the load-bearing one:
    #   (a) knocked_at in gm-tickets.json is what makes gmd's NEXT 0x801 poll
    #       for this caller return with FLAG_JOIN (0x40) set, lighting up the
    #       Join button on their GM Call screen.
    #   (b) duty flips so the "no GM on duty" auto-reply mail does not fire
    #       on other calls.
    #   (c) THE PUSH: authsess sends the knock immediately (over the auth
    #       band) so the player sees the "Please join the GM chat" prompt on
    #       their screen now, not on their next check-in. Without this, they
    #       stay on the GM Call screen until the 0x801 poll comes round and
    #       the knock feels dropped. Same call the admin panel's Knock makes.
    _mark_ticket_knocked(ticket_id, "discord:" + knocker_display)
    try:
        import gmduty
        gmduty.set_duty(gm_user, True, KNOCK_DUTY_S, via="discord")
    except Exception as e:                                        # noqa: BLE001
        _log("could not go on duty as %s: %r" % (gm_user, e))
    push = _push_knock(rec, action=0)
    _log("knock push for %s: %s" % (ticket_id,
                                    "sent" if push.get("ok") else push.get("error") or "no answer"))

    _log("thread %s opened for room %s by %s (%s, gm=%s)"
         % (thread_id, room, knocker_display, knocker_id, gm_user))
    return {"type": 4, "data": {
        "flags": 64,
        "content": "Thread ready: <#%s>." % thread_id,
        "allowed_mentions": {"parse": []}}}


def on_close(interaction, ticket_id):
    """`pol:gmclose:<ticket_id>` -> stop relay, archive + lock the thread, and
    delete the original alert from the calls channel so a closed call does not
    pile up next to open ones."""
    api = _need_api()
    if not _knocker_is_gm(interaction):
        return _ephemeral("Only linked GM accounts can close a call from here.")
    channel_id = str((interaction.get("channel_id")
                      or (interaction.get("channel") or {}).get("id") or ""))
    if not channel_id:
        return _ephemeral("Could not tell which thread this was.")
    display = _member_display(interaction)
    ldb = discordlink.connect()
    try:
        row = discordlink.gm_thread_by_thread(ldb, channel_id)
        if row is None or row["closed_at"] is not None:
            return _ephemeral("This call is not being relayed any more.")
        ticket_id = row["ticket_id"]
        alert = discordlink.gm_alert_get(ldb, ticket_id)
        closed = discordlink.gm_thread_close(ldb, channel_id)
    finally:
        ldb.close()
    if not closed:
        return _ephemeral("Closed.")
    # Read the ticket state that existed BEFORE we flip it closed, so we know
    # which invited players were in the call and need to be told it ended.
    was_invited = []
    try:
        with open(_TICKET_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        cur = st.get(ticket_id) if isinstance(st, dict) else None
        if isinstance(cur, dict):
            was_invited = [i for i in cur.get("invited") or []
                           if isinstance(i, dict) and i.get("guid")]
    except (OSError, ValueError):
        pass

    # 1) Discord side: say who closed, delete the channel alert, archive+lock.
    api("POST", "/channels/%s/messages" % channel_id, {
        "content": "Call closed by %s. Nothing further relays." % _md(display),
        "allowed_mentions": {"parse": []}})
    api("PATCH", "/channels/%s" % channel_id,
        {"archived": True, "locked": True})
    if alert is not None and alert["message_id"]:
        del_st, _ = api("DELETE", "/channels/%s/messages/%s"
                        % (alert["channel_id"], alert["message_id"]))
        _log("close: deleted alert %s from channel %s (HTTP %s)"
             % (alert["message_id"], alert["channel_id"], del_st))
        ldb = discordlink.connect()
        try:
            discordlink.gm_alert_drop(ldb, ticket_id)
        finally:
            ldb.close()

    # 2) Panel state: mark closed and clear knock/invited bookkeeping. Done
    # BEFORE the push knocks so the panel already reads the new state.
    _mark_ticket_closed(ticket_id, "discord:" + display)

    # 3) Live close for the requester (action 2, app.dll 0x4ab272c) so the
    # player's Viewer ends the call on receipt, not on its next poll.
    rec = _read_ticket(ticket_id) or {}
    push = _push_knock(rec, action=2)
    _log("close push for %s: %s" % (ticket_id,
                                    "sent" if push.get("ok") else push.get("error") or "no answer"))

    # 4) Same for each invited player. Their Viewer holds Join + the home GM
    # button until a close knock arrives, so leaving them out strands the UI.
    # Runs in a background thread: each push blocks on authsess up to _KICK_WAIT
    # and the operator should not wait for a chain of them.
    if was_invited:
        def close_invitees(invited=list(was_invited)):
            for i in invited:
                got = _push_knock(dict(rec, handle=i.get("handle") or "",
                                       guid=i["guid"]), action=2)
                _log("close push for %s to invited %s: %s"
                     % (ticket_id, i.get("handle"),
                        "sent" if got.get("ok") else got.get("error") or "no answer"))
        import threading
        threading.Thread(target=close_invitees, daemon=True,
                         name="gmconsole-close-invitees").start()

    # 5) Mail the transcript (+ any resolution) to the requester and every
    # invited player who has a PlayOnline Mail address. Same body the panel's
    # close mails; runs in a thread because it opens the accounts DB and hits
    # deliver_mail once per recipient. Never blocks the ack to Discord.
    def mail():
        # No resolution note from the Discord path yet (see the modal we'd add
        # later if someone wants one). Empty string still triggers the mail if
        # there was chat.
        summary = _mail_transcript(ticket_id, resolution="",
                                   invited=was_invited,
                                   by="discord:" + display)
        _log("close transcript for %s: %s" % (ticket_id, summary))
    import threading as _t
    _t.Thread(target=mail, daemon=True, name="gmconsole-transcript-mail").start()

    return _ephemeral("Closed.")


# --------------------------------------------------------------------------- #
# desk -> Discord: follow knocks and closes made on the admin panel
# --------------------------------------------------------------------------- #

#: Discord calls one sync pass may make. The first pass after deploy walks every
#: alert posted before this existed; spreading that over ticks keeps it clear of
#: Discord's rate limit and of the relay sharing the same loop.
SYNC_MAX_CALLS = int(os.environ.get("POL_BRIDGE_GM_SYNC_MAX", "6"))

_DESK = "desk:"


def _claimant(knocked_by):
    """Who holds a call, as GMs in the alerts channel should read it."""
    who = str(knocked_by or "").strip()
    if who.startswith("discord:"):
        return (who[len("discord:"):] or "a GM") + " (Discord)"
    return (who or "a GM") + " (GM desk)"


def _ticket_done(ticket_id, state):
    if (state.get(ticket_id) or {}).get("status") == "closed":
        return True
    rec = _read_ticket(ticket_id)
    if rec is None:
        # Unreadable is usually gmd mid-write; only a missing file means gone.
        return not os.path.exists(os.path.join(GM_CALL_DIR, ticket_id + ".json"))
    return bool(rec.get("cancelled_at"))


def sync_desk_state():
    """One pass mirroring the admin panel's ticket state into Discord.

      * knocked on the desk  -> the alert's button reads "Claimed by <user>"
      * knock withdrawn      -> the Knock button comes back
      * closed or cancelled  -> the alert is deleted and any thread is told,
                                archived and locked

    Only the Discord side is touched: the panel already pushed the close to the
    player and mailed the transcript, so none of that is repeated here. Returns
    how many Discord calls it made."""
    if _api is None:
        return 0
    state = _read_ticket_state()
    calls = 0
    ldb = discordlink.connect()
    try:
        for th in discordlink.gm_threads_open(ldb):
            if calls >= SYNC_MAX_CALLS:
                return calls
            tid = th["ticket_id"]
            if not _ticket_done(tid, state):
                continue
            by = (state.get(tid) or {}).get("by") or ""
            if by.startswith("discord:"):
                why = "Call closed."
            elif (state.get(tid) or {}).get("status") == "closed":
                why = "Call closed on the GM desk by %s." % _md(by or "a GM")
            else:
                why = "The player cancelled this call."
            _api("POST", "/channels/%s/messages" % th["thread_id"],
                 {"content": why + " Nothing further relays.",
                  "allowed_mentions": {"parse": []}})
            _api("PATCH", "/channels/%s" % th["thread_id"],
                 {"archived": True, "locked": True})
            calls += 2
            discordlink.gm_thread_close(ldb, th["thread_id"])
            _log("thread %s for %s closed to follow the desk" % (th["thread_id"], tid))

        for al in discordlink.gm_alerts_all(ldb):
            if calls >= SYNC_MAX_CALLS:
                break
            tid = al["ticket_id"]
            cur = state.get(tid) or {}
            claimed = str(al["claimed_by"] or "")
            if _ticket_done(tid, state):
                st, _ = _api("DELETE", "/channels/%s/messages/%s"
                             % (al["channel_id"], al["message_id"]))
                calls += 1
                if st in (200, 204, 404):
                    discordlink.gm_alert_drop(ldb, tid)
                continue
            knocked_by = str(cur.get("knocked_by") or "")
            if cur.get("knocked_at") and not claimed:
                st, _ = _api("PATCH", "/channels/%s/messages/%s"
                             % (al["channel_id"], al["message_id"]),
                             {"components": _knock_component(
                                 tid, claimed_by=_claimant(knocked_by))})
                calls += 1
                if st == 200:
                    discordlink.gm_alert_claim(ldb, tid, _DESK + knocked_by)
            elif not cur.get("knocked_at") and claimed.startswith(_DESK) \
                    and discordlink.gm_thread_by_room(ldb, al["room"]) is None:
                st, _ = _api("PATCH", "/channels/%s/messages/%s"
                             % (al["channel_id"], al["message_id"]),
                             {"components": _knock_component(tid)})
                calls += 1
                if st == 200:
                    discordlink.gm_alert_unclaim(ldb, tid)
        return calls
    finally:
        ldb.close()


# --------------------------------------------------------------------------- #
# /gm channel here | off | status
# --------------------------------------------------------------------------- #

def on_channel_command(interaction, sub, args):
    """`/gm channel here|off|status`. `here` binds the invoking channel;
    `off` unbinds this guild; `status` shows the binding.

    Auth is the same as `/gm duty`: the invoking Discord account must be
    linked to a GM desk account (gmduty.account_for)."""
    if not _knocker_is_gm(interaction):
        return _ephemeral("This Discord account is not linked to a GM desk "
                          "account. Run `/gm link` and enter the code on the "
                          "admin panel first.")
    guild_id = str(interaction.get("guild_id") or "")
    if not guild_id:
        return _ephemeral("This command has to run inside a server.")
    channel_id = str(interaction.get("channel_id") or "")
    ldb = discordlink.connect()
    try:
        if sub == "here":
            names = _list_tickets()
            baseline = _ticket_id(names[-1]) if names else ""
            role_id = str(args.get("role") or "") or None
            bound_by = _member_user_id(interaction)
            row = discordlink.bind_gm_channel(ldb, guild_id, channel_id,
                                              role_id=role_id,
                                              baseline_ticket=baseline,
                                              bound_by=bound_by)
            _log("channel bound: guild=%s channel=%s role=%s by=%s"
                 % (guild_id, channel_id, role_id, bound_by))
            return _ephemeral(
                "GM calls will be posted in <#%s>%s. Existing tickets on disk "
                "(%d) are baselined and will not be re-alerted."
                % (row["channel_id"],
                   (" pinging <@&%s>" % row["role_id"]) if row["role_id"] else "",
                   len(names)))
        if sub == "off":
            ok = discordlink.unbind_gm_channel(ldb, guild_id)
            return _ephemeral("Stopped posting GM calls in this server." if ok
                              else "This server was not posting GM calls.")
        row = discordlink.gm_channel_row(ldb, guild_id)
    finally:
        ldb.close()
    if row is None:
        return _ephemeral("No channel is bound in this server. Run "
                          "`/gm channel here` in the channel you want the "
                          "alerts in.")
    return _ephemeral(
        "Posting GM calls in <#%s>%s. Last ticket alerted: %s."
        % (row["channel_id"],
           (" pinging <@&%s>" % row["role_id"]) if row["role_id"] else "",
           row["last_ticket"] or "(none yet)"))


# --------------------------------------------------------------------------- #
# relay: player <-> Discord thread
# --------------------------------------------------------------------------- #

def _fmt_player_line(text, nick):
    nick = _clean(nick, 20) or "player"
    text = _md(_clean(text)) or "(empty)"
    if len(text) > RELAY_MAX:
        text = text[:RELAY_MAX] + " ..."
    return {"content": "**%s:** %s" % (_md(nick), text),
            "allowed_mentions": {"parse": []}}


def _relay_from_room(api, ldb, row):
    """New player-side 'T' records in `room` -> posts in the thread."""
    room, thread_id = row["room"], row["thread_id"]
    last = float(row["last_relay_at"] or 0)
    try:
        rows = gmchat.transcript(room.encode(), limit=200, since=last)
    except Exception:                                              # noqa: BLE001
        return
    latest = last
    for r in rows:
        at = float(r.get("at") or 0)
        if at <= last or r.get("dir") != "in":
            continue
        try:
            raw = bytes.fromhex(r.get("raw") or "")
        except ValueError:
            continue
        if raw[:1] != b"T":
            latest = max(latest, at)
            continue
        _h, sep, body = raw[1:].partition(b"\x07")
        if not sep:
            latest = max(latest, at)
            continue
        text = body.decode("cp932", "replace")
        api("POST", "/channels/%s/messages" % thread_id,
            _fmt_player_line(text, r.get("nick") or "player"))
        latest = max(latest, at)
    if latest != last:
        discordlink.gm_thread_note_relay(ldb, room, latest)


def _text_from_message(msg):
    """Discord message -> the text a game player should read, or None to skip.

    Mentions are stripped rather than resolved: relaying a raw `<@123>` is
    worse than dropping it, and the caller has no context for a Discord id.
    Attachments become a `[file: name]` marker so the GM can see it reached
    them even though the file itself does not travel."""
    if (msg.get("author") or {}).get("bot"):
        return None
    text = _STRIP_MENTION.sub("", msg.get("content") or "").strip()
    for a in msg.get("attachments") or []:
        text = (text + " [file: %s]" % a.get("filename", "?")).strip()
    return text or None


def _spool_gm_line(row, text):
    """Spool a GM-typed thread line. The IRC nick is left DEFAULT (gmchat.GM_NICK,
    which authserv already announced into the room's member table on JOIN) --
    if we sent under the knocker's Discord name, the client's member-table
    resolver (app.dll chatwin+0x350) would not find a speaker for it and the
    line would arrive intact and render as nothing (FACTS "GM CHAT, THE ROOM
    ITSELF").

    CONFIDENTIALITY. The line goes across as bare text, no `[knocker]` prefix:
    the player only ever needs to see that a GM answered (the phoenix and the
    red `GM >` do that on the client), and which desk operator handled the call
    is desk-side information. The thread on Discord already attributes each
    line to its author. (2026-09-29, after a GM's name showed up on a
    player's screen.)"""
    rec = gmchat.encode_text(text)
    gmchat.spool(row["room"].encode(), rec, nick=None)
    _log("spooled to %s: %r (nick=default GM)"
         % (row["room"], text[:80]))


def _relay_to_room(api, ldb, row):
    """New Discord messages in the thread -> gmchat spool."""
    thread_id = row["thread_id"]
    after = row["last_msg_id"] or "0"
    st, data = api("GET", "/channels/%s/messages?after=%s&limit=100"
                   % (thread_id, after))
    if st != 200 or not isinstance(data, list):
        return
    # Discord returns newest-first: send in original order.
    top = row["last_msg_id"]
    for msg in reversed(data):
        text = _text_from_message(msg)
        if text is None:
            top = str(msg["id"])
            continue
        _spool_gm_line(row, text)
        top = str(msg["id"])
    if top and top != row["last_msg_id"]:
        discordlink.gm_thread_note_msg(ldb, thread_id, top)


def relay_tick():
    """One relay pass across every open thread."""
    if _api is None:
        return 0
    ldb = discordlink.connect()
    try:
        rows = discordlink.gm_threads_open(ldb)
        for row in rows:
            try:
                _relay_from_room(_api, ldb, row)
            except Exception as e:                                # noqa: BLE001
                _log("relay from %s failed: %r" % (row["room"], e))
            try:
                _relay_to_room(_api, ldb, row)
            except Exception as e:                                # noqa: BLE001
                _log("relay to %s failed: %r" % (row["room"], e))
        return len(rows)
    finally:
        ldb.close()
