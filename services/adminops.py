"""The admin panel's background work and status probes.

  * GM-call alerts: a worker thread watches the gm-calls directory `gmd`
    writes, and for each NEW ticket posts to a Discord webhook and sends a Web
    Push message to every browser that asked for one. Tickets already
    announced are remembered in admin.db (`alerted`), so a restart never
    repeats an alert, and the first run marks what is already there as seen.
  * Code expiry: the same thread deletes unused registration codes whose
    expiry has passed. Redemption lives in accounts.py and is not touched --
    changing it would make prod restart login and authsess.
  * Status for the Overview: live session counts from the markers the game
    services publish (live_sessions.py), TCP reachability of the services,
    the last backup (deploy/pol-backup writes data/backup-status.json) and
    free disk.

Stdlib plus webpush (pycryptodome), like the rest of the admin service.
"""
import concurrent.futures
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request

import adminusers
import gmd
import gmduty
import live_sessions
import webpush

TICKET_RE = re.compile(r"gm-\d{8}T\d{6}-\d+\.json")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Live-session markers published every ~10 s by services that hold live
#: sessions (live_sessions.py, `live:<service>` in the live-state store):
#: (service, label, what the count is).
LIVE_MARKERS = (
    ("tm", "Tetra Master", "matches"),
    ("authsess-jan", "Janhourou", "sessions"),
    ("fmo", "Front Mission Online", "sessions"),
    ("felobby", "Fantasy Earth (lobby)", "sessions"),
    ("feworld", "Fantasy Earth (world)", "sessions"),
)
LIVE_FRESH = 60.0
#: How often expired codes are swept (seconds). Tests shorten it.
SWEEP_EVERY = float(os.environ.get("POL_ADMIN_SWEEP_EVERY", 60))

#: Service checks, one per line: "<name> <port>" or "<name> <host>:<port>".
#: Ports are the listeners reachable from the admin container through the host
#: gateway; the owner can edit the list on the Security tab (a title server
#: installed beside the core adds its own port there).
DEFAULT_TARGETS = """\
Lobby 51220
Auth 51241
Registration pages 8080
Registration (SSL) 8443
Patch server 54000"""
DEFAULT_HOST = "host.docker.internal"


def clean(text, limit=200):
    return _CTRL.sub("", str(text or "")).strip()[:limit]


# --------------------------------------------------------------------------- #
# status probes
# --------------------------------------------------------------------------- #
def live_counts():
    now, out = time.time(), []
    for service, label, unit in LIVE_MARKERS:
        d = live_sessions.read_marker(service)
        if d is None:
            continue                          # service not deployed here
        age = now - float(d.get("stamp") or 0)
        fresh = 0 <= age < LIVE_FRESH
        out.append({"label": label, "unit": unit, "fresh": fresh,
                    "count": int(d.get("count") or 0) if fresh else None,
                    "age": round(age)})
    return out


def parse_targets(text, default_host=DEFAULT_HOST):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, where = line.rpartition(" ")
        host, _, port = where.rpartition(":")
        try:
            port = int(port)
        except ValueError:
            continue
        out.append((name.strip() or where, host or default_host, port))
    return out


def probe(targets, timeout=1.5):
    def one(t):
        name, host, port = t
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            return {"name": name, "host": host, "port": port, "ok": True,
                    "ms": round((time.time() - t0) * 1000)}
        except ConnectionRefusedError:
            return {"name": name, "host": host, "port": port, "ok": False,
                    "error": "refused"}
        except (socket.timeout, TimeoutError):
            return {"name": name, "host": host, "port": port, "ok": False,
                    "error": "no answer"}
        except OSError as exc:
            return {"name": name, "host": host, "port": port, "ok": False,
                    "error": exc.strerror or "unreachable"}
    if not targets:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        return list(ex.map(one, targets))


def backup_status(data_dir):
    try:
        with open(os.path.join(data_dir, "backup-status.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def disk(data_dir):
    try:
        u = shutil.disk_usage(data_dir)
        return {"total": u.total, "free": u.free}
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def valid_webhook(url):
    return bool(re.match(r"https://(ptb\.|canary\.)?(discord|discordapp)\.com/api/"
                         r"(v\d+/)?webhooks/\d+/[\w-]+$", url or ""))


def discord_post(url, content, timeout=10):
    """(ok, detail). Mentions are switched off: ticket text comes from players."""
    body = json.dumps({"content": content[:1900],
                       "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        # Discord's edge refuses the default Python-urllib agent.
        "User-Agent": "PlayOnlineAdmin (alerts, 1.0)"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return True, str(r.status)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except OSError as exc:
        return False, str(exc)[:120]


# --------------------------------------------------------------------------- #
# Web Push
# --------------------------------------------------------------------------- #
def vapid_key(conn):
    """The panel's push key, created the first time anything asks for it."""
    pem = adminusers.get_setting(conn, "vapid_key")
    if not pem:
        pem = webpush.new_vapid_key()
        adminusers.set_setting(conn, "vapid_key", pem)
    return pem


def push_to(conn, subs, message, contact=None):
    """Send `message` (dict) to each subscription row. Returns (sent, total).
    A subscription the push service says is gone (404/410) is forgotten."""
    pem = vapid_key(conn)
    sent = 0
    for s in subs:
        origin = s["origin"] or ""
        subject = contact or (origin if origin.startswith("https://")
                              else "mailto:admin@localhost")
        try:
            status, text = webpush.send(
                {"endpoint": s["endpoint"],
                 "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                message, pem, subject)
        except Exception as exc:              # a bad row must not stop the rest
            status, text = 0, str(exc)
        if 200 <= status < 300:
            sent += 1
            adminusers.push_result(conn, s["endpoint"], True)
        elif status in (404, 410):
            adminusers.push_remove(conn, s["endpoint"])
        else:
            adminusers.push_result(conn, s["endpoint"], False,
                                   f"HTTP {status} {text[:120]}")
    return sent, len(subs)


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    """Alerts on new GM calls; expires codes. One thread, one loop.

    `may_push(row)` says whether a subscription's owner may still receive GM
    alerts (a moderator disabled or without the GM permission may not);
    `content_label(id)` names a title; `expire_code(code)` deletes an unused
    code and returns True if it did; `audit(...)` records it."""

    def __init__(self, ticket_dir, may_push, content_label, expire_code, audit,
                 every=5.0, auto_reply=None):
        super().__init__(daemon=True, name="admin-worker")
        self.ticket_dir = ticket_dir
        self.may_push = may_push
        self.content_label = content_label
        self.expire_code = expire_code
        self.audit = audit
        self.every = every
        #: auto_reply(rec, name) -> str: answers a request filed while nobody
        #: is on duty (admin._gm_auto_reply). None = no auto-reply.
        self.auto_reply = auto_reply
        self._retries = {}
        self._last_sweep = 0.0

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:          # never let the loop die
                print(f"[admin] worker error: {exc}", flush=True)
            time.sleep(self.every)

    def tick(self):
        conn = adminusers.connect()
        try:
            self.check_calls(conn)
            if time.time() - self._last_sweep >= SWEEP_EVERY:
                self._last_sweep = time.time()
                self.sweep_codes(conn)
        finally:
            conn.close()

    # -- GM calls ----------------------------------------------------------- #
    def ticket_names(self):
        try:
            return sorted(n for n in os.listdir(self.ticket_dir) if TICKET_RE.fullmatch(n))
        except OSError:
            return []

    def check_calls(self, conn):
        names = self.ticket_names()
        seen = adminusers.alerted_all(conn)
        if adminusers.get_setting(conn, "alert_baseline") is None:
            # First run: what is already there is history, not news.
            for n in names:
                if n not in seen:
                    adminusers.alerted_add(conn, n, "existing when alerts started")
            adminusers.set_setting(conn, "alert_baseline", str(time.time()))
            return
        for name in names:
            if name in seen:
                continue
            try:
                with open(os.path.join(self.ticket_dir, name), encoding="utf-8",
                          errors="replace") as f:
                    rec = json.load(f)
                if not isinstance(rec, dict):
                    raise ValueError("not a ticket")
            except (OSError, ValueError):
                # gmd writes in place, so a read can land mid-write. Retry a
                # few ticks before giving up on it.
                n = self._retries[name] = self._retries.get(name, 0) + 1
                if n >= 6:
                    adminusers.alerted_add(conn, name, "unreadable")
                continue
            self._retries.pop(name, None)
            adminusers.alerted_add(conn, name, self.announce(conn, rec, name=name))

    def announce(self, conn, rec, test=False, name=None):
        handle = clean(rec.get("handle"), 40) or "a player"
        subject = clean(rec.get("subject"), 120) or "(no subject)"
        cid = rec.get("content_id")
        title_name = self.content_label(cid) if cid is not None else ""
        panel = (adminusers.get_setting(conn, "panel_url") or "").rstrip("/")
        results = []
        # Who is at the desk (gmduty.py). With nobody on duty the caller is
        # told so by mail at once and every GM is alerted; with GMs on duty,
        # only they are.
        on_duty = sorted(gmduty.gms(gmd.read_control()))
        if not on_duty and self.auto_reply and not test:
            try:
                results.append(self.auto_reply(rec, name or ""))
            except Exception as exc:
                results.append(f"auto-reply failed: {exc}")
        duty_line = ("On duty: " + ", ".join(on_duty) if on_duty
                     else "No GM on duty: the player was told by mail that a GM will reply")

        url = adminusers.get_setting(conn, "discord_webhook")
        if url:
            text = (f"**GM call** from **{handle}**"
                    + (f" ({title_name})" if title_name else "") + f": {subject}"
                    + f"\n{duty_line}"
                    + (f"\n{panel}/#gmcalls" if panel else ""))
            ok, detail = discord_post(url, text)
            results.append("discord " + ("ok" if ok else "failed: " + detail))

        subs = [s for s in adminusers.push_list(conn) if self.may_push(s)]
        if on_duty:
            mine = [s for s in subs if s["username"] in on_duty]
            subs = mine or subs
        if subs:
            sent, total = push_to(conn, subs, {
                "title": f"GM call from {handle}",
                "body": subject + (f" ({title_name})" if title_name else ""),
                "tag": "gm-" + str(rec.get("request_no", "")),
                "url": "/#gmcalls"})
            results.append(f"push {sent}/{total}")
        return "; ".join(results) or "no alert channels set up"

    # -- code expiry --------------------------------------------------------- #
    def sweep_codes(self, conn):
        for code in adminusers.codes_due_to_expire(conn):
            deleted = False
            try:
                deleted = self.expire_code(code)
            except Exception as exc:
                print(f"[admin] could not expire {code}: {exc}", flush=True)
                continue
            adminusers.mark_expired(conn, code)
            if deleted:
                self.audit("system", "system", "expired a code", code)
