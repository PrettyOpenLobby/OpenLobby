#!/usr/bin/env python3
"""bridge_test.py -- the PlayOnline <-> Discord bridge end to end, offline.

polbridge.py + discordlink.py + ucscgi kinou 90 + polboards' /pol/ forward,
against a throwaway accounts.db, message store and push spool, with Discord
replaced by a recording fake. Nothing here touches a real service.

    python tools/bridge_test.py
"""
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
# polboards ships from a title repository, not this one. Try the sibling
# public/hippaulmaster (the TM board carries it) and private/services next to
# it, so a checkout with either can run the test unaided.
for extra in (os.path.abspath(os.path.join(HERE, "..", "..", "hippaulmaster", "services")),
              os.path.abspath(os.path.join(HERE, "..", "..", "..", "private", "services"))):
    if os.path.isdir(extra):
        sys.path.insert(0, extra)

TMP = tempfile.mkdtemp(prefix="bridgetest-")
for sub in ("logs", "res"):
    os.makedirs(os.path.join(TMP, sub), exist_ok=True)
os.environ["POL_LOG_DIR"] = os.path.join(TMP, "logs")
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "res")
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_DISCORD_LINK_DB"] = os.path.join(TMP, "links.db")
os.environ["POL_UCS_PREFILL_ID"] = "0"
os.environ["POL_UCS_IDENT_PROBE"] = "0"
os.environ["POL_NEWS_STORE"] = os.path.join(TMP, "announcements.yaml")
os.environ.pop("POL_BOARDS_BRIDGE_URL", None)

import accounts as A        # noqa: E402
import responders as R      # noqa: E402
import discordlink as L     # noqa: E402
import polbridge as B       # noqa: E402
import polboards as P       # noqa: E402
import ucscgi as U          # noqa: E402

FAILS = []


def check(name, ok, detail=None):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "   -> %r" % (detail,)))
    if not ok:
        FAILS.append(name)


class _Resp:
    def __init__(self, status, data):
        self.status, self._raw = status, json.dumps(data).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """Discord, as far as the bridge can tell."""

    def __init__(self):
        self.calls = []
        #: False: making a webhook answers without a token, as if the bridge
        #: lacked Manage Webhooks, so news posts go out as the bridge
        self.hooks = False

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        if req.full_url.endswith("/users/@me/channels"):
            return _Resp(200, {"id": "DM1"})
        if self.hooks and req.get_method() == "POST" and req.full_url.endswith("/webhooks"):
            return _Resp(200, {"id": "W1", "token": "T1"})
        if self.hooks and req.get_method() == "POST" and "/webhooks/W1/T1" in req.full_url:
            return _Resp(200, {"id": "H%d" % len(self.calls)})
        if self.hooks and req.get_method() == "GET" and "/users/" in req.full_url:
            uid = req.full_url.rsplit("/", 1)[1]
            return _Resp(200, {"id": uid, "username": "Tetra Master", "avatar": "av1"})
        if "/messages" in req.full_url:
            return _Resp(200, {"id": "M%d" % len(self.calls)})
        return _Resp(200, {})


#: RFC 8032 section 7.1 TEST 1 -- the same key tm_board_test signs with.
SK = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PK = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def ed_sign(msg):
    """RFC 8032 5.1.6 on polboards' own arithmetic: the test's Discord."""
    h = hashlib.sha512(SK).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    pub = P._ed_bytes(P._ed_mul(a, P._ED_B))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % P._ED_L
    big_r = P._ed_bytes(P._ed_mul(r, P._ED_B))
    k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % P._ED_L
    return big_r + ((r + k * a) % P._ED_L).to_bytes(32, "little")


def post(port, path, obj, sign=True):
    body, ts = json.dumps(obj).encode(), "1789500000"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if sign:
        req.add_header("X-Signature-Ed25519", ed_sign(ts.encode() + body).hex())
        req.add_header("X-Signature-Timestamp", ts)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def stored_names():
    return set(R._res_list(scope="mail"))


def new_names(before):
    return sorted(n for n in stored_names()
                  if n.startswith("m.") and n.endswith(".bin") and n not in before)


def user(uid):
    return {"user": {"id": uid, "username": "u%s" % uid}}


# --------------------------------------------------------------------------- #
adb = A.connect()
alice = A.register_account(adb, "Alice", "password123")
bobby = A.register_account(adb, "Bobby", "password456")
HA = A.primary_handle_row(adb, alice["member_id"])
HB = A.primary_handle_row(adb, bobby["member_id"])
adb.close()
AM, BM = alice["member_id"], bobby["member_id"]

net = Net()
B.OPENER = net
B.ARGS = B.build_parser().parse_args(["--token", "TKN", "--app-id", "APP",
                                      "--public-key", PK])
ldb = L.connect()

print("link codes")
c = L.new_code(ldb, "111", "alice.d")
check("a code is 8 unambiguous symbols shown as XXXX-XXXX",
      re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", c) is not None, c)
check("a wrong code links nothing", L.redeem(ldb, "ZZZZ-ZZZZ", AM) is None)
row = L.redeem(ldb, c.lower().replace("-", " "), AM)
check("the right code, in any case and with any separator, links the member",
      row is not None and row["discord_id"] == "111" and row["member_id"] == AM, row)
check("...and only once", L.redeem(ldb, c, AM) is None)
old = L.new_code(ldb, "222", "x", now=time.time() - L.CODE_TTL - 5)
check("a code past its lifetime is refused", L.redeem(ldb, old, BM) is None)
first = L.new_code(ldb, "222", "x")
second = L.new_code(ldb, "222", "x")
check("a new code voids the same user's older one",
      L.redeem(ldb, first, BM) is None and second != first)

print("the watcher")
before = stored_names()
R._mail_mint(HB["handle_name"], A.handle_guid(HB["id"]), A.handle_guid(HA["id"]),
             "Old news", "sent before the bridge existed")
check("the FIRST scan only takes stock -- nobody gets their history",
      B.scan_once(min_age=0) == 0 and net.calls == [], net.calls)
R._mail_mint(HB["handle_name"], A.handle_guid(HB["id"]), A.handle_guid(HA["id"]),
             "Hello there", "How are *you*?")
n = B.scan_once(min_age=0)
dm = [c for c in net.calls if c[1].endswith("/channels/DM1/messages")]
check("a new message to a linked member goes out as ONE DM, over a DM channel it opened",
      n == 1 and net.calls[0][1].endswith("/users/@me/channels")
      and net.calls[0][2] == {"recipient_id": "111"} and len(dm) == 1, net.calls)
emb = dm[0][2]["embeds"][0] if dm else {}
btn = dm[0][2]["components"][0]["components"][0] if dm else {}
check("...showing sender, the whole subject and the body as typed (markdown off)",
      emb.get("author", {}).get("name") == "From Bobby" and emb.get("title") == "Hello there"
      and emb.get("description") == "How are \\*you\\*?"
      and "to Alice" in emb.get("footer", {}).get("text", ""), emb)
check("...with a Reply button that carries only a short id",
      btn.get("label") == "Reply" and re.fullmatch(r"pol:reply:[\w-]{12}", btn.get("custom_id", "")),
      btn)
RID = btn.get("custom_id", "pol:reply:").split(":")[-1]
k = len(net.calls)
check("the next scan sends nothing again", B.scan_once(min_age=0) == 0 and len(net.calls) == k)
L.set_notify(ldb, "111", False)
R._mail_mint(HB["handle_name"], A.handle_guid(HB["id"]), A.handle_guid(HA["id"]),
             "Quiet", "notify is off")
check("with DMs turned off, nothing is sent (and it is not saved up for later)",
      B.scan_once(min_age=0) == 0 and len(net.calls) == k)
L.set_notify(ldb, "111", True)
check("...and turning them back on does not replay it",
      B.scan_once(min_age=0) == 0 and len(net.calls) == k)
R._mail_mint(HA["handle_name"], A.handle_guid(HA["id"]), A.handle_guid(HB["id"]),
             "To Bobby", "Bobby is not linked")
check("a message to a member who is not linked sends nothing",
      B.scan_once(min_age=0) == 0 and len(net.calls) == k)
R._mail_mint("System", A.handle_guid(HB["id"]), A.handle_guid(HA["id"]),
             "Let's be friend", "", kind=R.MAIL_KIND_FRIEND_REQUEST)
check("a system notice (friend request) is not DM'd as a message",
      B.scan_once(min_age=0) == 0 and len(net.calls) == k)
check("a group message's trailer is not read as text",
      B.read_message(b"Subj\x07Body text\x00" + b"\x01" * 32) == ("Subj", "Body text"))

print("reply")
r = B.interaction({"type": 3, "data": {"custom_id": "pol:reply:" + RID}, **user("111")})
comps = [c for row in r.get("data", {}).get("components", []) for c in row["components"]]
check("Reply opens a form addressed to the sender, subject prefilled 'Re:'",
      r["type"] == 9 and r["data"]["title"] == "Reply to Bobby"
      and comps[0]["value"] == "Re: Hello there" and comps[0]["max_length"] == 50
      and comps[1]["max_length"] == 300 and r["data"]["custom_id"] == "pol:send:" + RID, r)
r = B.interaction({"type": 3, "data": {"custom_id": "pol:reply:" + RID}, **user("999")})
check("...but only for the Discord account linked to the recipient",
      r["type"] == 4 and r["data"]["flags"] == 64, r)


def submit(uid, subject, body, rid=None):
    return B.interaction({"type": 5, **user(uid), "data": {
        "custom_id": "pol:send:" + (rid or RID),
        "components": [{"type": 1, "components": [{"type": 4, "custom_id": "subject",
                                                   "value": subject}]},
                       {"type": 1, "components": [{"type": 4, "custom_id": "body",
                                                   "value": body}]}]}})


before = stored_names()
R._push_spool_clear()
r = submit("111", "Re: Hello there", "Fine\nthanks\x07!")
got = new_names(before)
meta = R._mail_meta(R._mail_path_of(got[0])) if got else {}
check("submitting posts ONE PlayOnline message and says so privately",
      r["type"] == 4 and r["data"]["flags"] == 64 and "Bobby" in r["data"]["content"]
      and len(got) == 1, (r, got))
check("...from the recipient's handle, to the original sender, as a plain message",
      meta.get("sender") == "Alice" and meta.get("kind") == R.MAIL_KIND_MESSAGE
      and A.handle_by_guid(A.connect(),
                           meta.get("recipient_guid"))["id"] == HB["id"], meta)
subj, body = B.read_message(R._res_read(got[0])) if got else ("", "")
check("...the object holding the whole subject and the body, control characters folded",
      subj == "Re: Hello there" and body == "Fine thanks !", (subj, body))
spooled = R._push_spool_pending()
check("...STORED first, then its arrival push spooled for authsess to deliver",
      any(s.get("kind") == "mail" and s.get("handle") == HB["id"] for s in spooled), spooled)
check("a reply from someone else's Discord account is refused",
      submit("999", "x", "y")["data"]["flags"] == 64 and len(new_names(before)) == 1)
check("an empty reply is refused", "needs" in submit("111", "  ", "\x00")["data"]["content"])
B.REPLY_PER_HOUR, keep = 1, B.REPLY_PER_HOUR
r = submit("111", "again", "too soon")
B.REPLY_PER_HOUR = keep
check("replies are rate limited per member", "hour" in r["data"]["content"]
      and len(new_names(before)) == 1, r)
check("a Reply for an unknown id is a private note",
      B.interaction({"type": 3, "data": {"custom_id": "pol:reply:nope"}, **user("111")})
      ["data"]["flags"] == 64)

print("/playonline")


def cmd(uid, sub, value=None):
    o = {"type": 1, "name": sub}
    if value is not None:
        o["options"] = [{"type": 5, "name": "on", "value": value}]
    return B.interaction({"type": 2, "data": {"name": "playonline", "options": [o]},
                          **user(uid)})["data"]["content"]


check("status names the linked handle and the DM setting",
      "Linked to Alice" in cmd("111", "status") and "**on**" in cmd("111", "status"))
out = cmd("444", "link")
code = re.search(r"\*\*([A-Z0-9-]{9})\*\*", out)
check("link gives a code and says where it is entered",
      code is not None and "Member Information" in out, out)
check("notify says 'not linked' for someone who is not",
      "not linked" in cmd("444", "notify", False))
check("notify off / on for someone who is",
      "**off**" in cmd("111", "notify", False) and "**on**" in cmd("111", "notify", True))
check("unlink, then status says so",
      "Unlinked" in cmd("111", "unlink") and "not linked" in cmd("111", "status"))
check("the command definition works in servers, DMs and as a user app",
      B.command_def()["contexts"] == [0, 1, 2] and B.command_def()["integration_types"] == [0, 1])

print("the account portal (kinou 90)")
h = types.SimpleNamespace(client_address=("127.0.0.1", 0))


def login():
    page = U.Handler._account_step(h, U.KINOU_DISCORD, {"kinou_id": "90"})
    tok = re.search(r'name="t" type="hidden" value="([0-9a-f]+)"', page).group(1)
    return tok, U.Handler._account_step(h, U.KINOU_DISCORD, {
        "kinou_id": "90", "step": "2", "t": tok, "polid": alice["polid"], "pw": "password123"})


tok, page = login()
check("after the POL password, the page shows the link state and a code field",
      "Not linked to Discord." in page and 'name="code"' in page
      and "kinou_id=90&amp;step=3" in page or "kinou_id=90&step=3" in page, page[:400])
bad = U.Handler._account_step(h, U.KINOU_DISCORD, {"kinou_id": "90", "step": "3", "t": tok,
                                                   "code": "ZZZZ-ZZZZ"})
check("a bad code is refused on the same page", "not valid" in bad)
good = U.Handler._account_step(h, U.KINOU_DISCORD, {"kinou_id": "90", "step": "3", "t": tok,
                                                    "code": code.group(1) if code else ""})
lk = L.link_by_member(ldb, AM)
check("the code from /playonline link links this member to that Discord account",
      "Linked to Discord as u444" in good and lk is not None and lk["discord_id"] == "444",
      (good[-300:], lk and dict(lk)))
tok, page = login()
check("signed in again, the page names the link and offers Unlink",
      "Linked to Discord as u444" in page and "step=4" in page)
out = U.Handler._account_step(h, U.KINOU_DISCORD, {"kinou_id": "90", "step": "4", "t": tok})
check("Unlink removes it", "unlinked" in out and L.link_by_member(ldb, AM) is None)
anon = U.Handler._account_step(h, U.KINOU_DISCORD, {"kinou_id": "90", "step": "3",
                                                    "t": "f" * 32, "code": "AAAA-AAAA"})
check("nothing reaches the link store without the password",
      'name="pw"' in anon and L.link_by_member(ldb, AM) is None)
mi = U.page_memberinfo("t0", "toviewer:", {"polid": alice["polid"]}, {"rows": [], "note": None})
check("Member Information has a Discord button", "kinou_id=90" in mi)

print("over HTTP, signed -- direct, and through the TM board's /pol/")
bport = free_port()
B.serve("127.0.0.1", bport)
check("an unsigned interaction is refused (401)",
      post(bport, "/discord/interactions", {"type": 1}, sign=False)[0] == 401)
check("a signed PING is answered",
      post(bport, "/discord/interactions", {"type": 1}) == (200, {"type": 1}))
board = types.SimpleNamespace(NAME="tm", PAGE="")
tport = free_port()
P.serve(board, P.build_parser().parse_args([]), tport)
check("with no bridge configured, the board answers /pol/ with 404",
      post(tport, "/pol/discord/interactions", {"type": 1})[0] == 404)
os.environ["POL_BOARDS_BRIDGE_URL"] = "http://127.0.0.1:%d" % bport
check("with it, a signed PING to tm.../pol/discord/interactions reaches the bridge",
      post(tport, "/pol/discord/interactions", {"type": 1}) == (200, {"type": 1}))
check("...and the bridge still checks the signature itself",
      post(tport, "/pol/discord/interactions", {"type": 1}, sign=False)[0] == 401)
check("markdown in a message is shown literally", B.md("a*b_c`d") == "a\\*b\\_c\\`d")

print("announcements -> channel")
if B.newsgen is None:
    check("newsgen loaded", False, B._NEWSGEN_ERR)
else:
    N = B.newsgen

    def write_store(items):
        """Replace the store with `items`, giving them fresh serials."""
        N.save(items, os.environ["POL_NEWS_STORE"])

    def add_item(item):
        """Insert one new item on top of what is stored, keeping every
        existing serial (which is what the admin panel does on a new post)."""
        existing = N.load()
        N.save([item] + existing, os.environ["POL_NEWS_STORE"])

    def announce(uid, sub_name, sub_opts=None, guild="G1", channel="C1", admin=True):
        opt = {"type": 1, "name": sub_name}
        if sub_opts is not None:
            opt["options"] = sub_opts
        return B.interaction({"type": 2, "guild_id": guild, "channel_id": channel,
                              "data": {"name": "playonline", "options": [
                                  {"type": 2, "name": "announce", "options": [opt]}]},
                              "member": {"user": {"id": uid, "username": "u%s" % uid},
                                         "permissions": str(B._ADMIN_BITS if admin else 0)}})

    write_store([{"date": "Nov. 1, 2026 12:00 [UTC]",
                  "title": "Old news", "kind": "info", "content": "playonline"}])
    r = announce("111", "here")
    check("bind names the channel and warns items are baselined",
          "post here" in r["data"]["content"] and "already seen" in r["data"]["content"],
          r["data"]["content"])
    row = L.announce_row(ldb, "G1")
    check("...and stores the guild's channel + a baseline serial",
          row is not None and row["channel_id"] == "C1"
          and int(row["last_serial"]) >= 900001, row and dict(row))
    k0 = len(net.calls)
    check("a scan right after bind posts nothing (backlog is baselined)",
          B.scan_announce() == 0 and len(net.calls) == k0, net.calls[k0:])

    r = announce("999", "here", admin=False)
    check("a non-admin cannot rebind the channel",
          "Manage Server" in r["data"]["content"], r["data"]["content"])

    add_item({"date": "Nov. 2, 2026 12:00 [UTC]", "title": "TM cup this Saturday",
              "kind": "info", "content": "tetra",
              "body": "Sign-ups open at 10:00 UTC."})
    n = B.scan_announce()
    ch = [c for c in net.calls[k0:] if "/channels/C1/messages" in c[1]]
    emb = ch[0][2]["embeds"][0] if ch else {}
    check("a new item posts ONE embed to the bound channel",
          n == 1 and len(ch) == 1 and emb.get("title") == "TM cup this Saturday"
          and "Sign" in emb.get("description", "")
          and "10:00 UTC" in emb.get("description", ""), (n, len(ch), emb))
    check("...with no @-ping until a role is set",
          "content" not in ch[0][2] and ch[0][2]["allowed_mentions"] == {"parse": [], "roles": []},
          ch[0][2].get("allowed_mentions"))
    k1 = len(net.calls)
    check("the next scan sends nothing again", B.scan_announce() == 0 and len(net.calls) == k1)

    r = announce("111", "role", [{"type": 3, "name": "game", "value": "tetra"},
                                 {"type": 8, "name": "role", "value": "R42"}])
    check("role names the game and the role", "Tetra" in r["data"]["content"]
          and "R42" in r["data"]["content"], r["data"]["content"])
    add_item({"date": "Nov. 3, 2026 12:00 [UTC]", "title": "TM finals postponed",
              "kind": "update", "content": "tetra"})
    B.scan_announce()
    latest = [c for c in net.calls[k1:] if "/channels/C1/messages" in c[1]][-1][2]
    check("a tetra post now pings the role, and only that role",
          latest.get("content") == "<@&R42>"
          and latest["allowed_mentions"]["roles"] == ["R42"], latest)
    add_item({"date": "Nov. 4, 2026 12:00 [UTC]", "title": "Small maintenance window",
              "kind": "maintenance", "content": "playonline"})
    B.scan_announce()
    latest = [c for c in net.calls[k1:] if "/channels/C1/messages" in c[1]][-1][2]
    check("...but a playonline post without a role for it stays quiet",
          "content" not in latest and latest["allowed_mentions"]["roles"] == [], latest)

    r = announce("111", "role", [{"type": 3, "name": "game", "value": "tetra"}])
    check("role with no role value clears the ping",
          "Cleared" in r["data"]["content"]
          and not L.announce_roles(ldb, "G1"), r["data"]["content"])

    r = announce("111", "here", channel="C2")
    check("moving to another channel reports the move",
          "moved from" in r["data"]["content"], r["data"]["content"])
    check("...and the baseline carries over",
          int(L.announce_row(ldb, "G1")["last_serial"])
          >= max(int(i["serial"]) for i in N.load()))
    add_item({"date": "Nov. 5, 2026 12:00 [UTC]", "title": "Server test",
              "kind": "info", "content": "playonline"})
    k2 = len(net.calls)
    B.scan_announce()
    new_ch2 = [c for c in net.calls[k2:] if "/channels/C2/messages" in c[1]]
    new_ch1 = [c for c in net.calls[k2:] if "/channels/C1/messages" in c[1]]
    check("the new bind takes over posting", len(new_ch2) == 1 and not new_ch1,
          (len(new_ch1), len(new_ch2)))

    k3 = len(net.calls)
    r = announce("111", "test")
    posts = [c for c in net.calls[k3:] if "/messages" in c[1]]
    check("test posts the newest item on demand, and says it went out",
          "Sent" in r["data"]["content"] and len(posts) == 1
          and posts[0][2]["embeds"][0]["title"] == "Server test", r["data"]["content"])

    r = announce("111", "status")
    check("status names the bound channel",
          "<#C2>" in r["data"]["content"], r["data"]["content"])

    print("cup reminders -> channel")
    import calendar as _cal
    import tempfile as _tf
    calf = os.path.join(_tf.mkdtemp(prefix="remind-"), "event-calendar.json")
    with open(calf, "w", encoding="utf-8") as f:
        json.dump({"tm": {"weekly": {
            "key": "chocobo-cup", "name": "Chocobo Cup", "dow": 4, "hour": 12,
            "hours": 72, "guide": "Win matches!",
            "sessions": {"hours": [19, 2], "minutes": 60, "from": "2026-01-01"},
            "prizes": {"money": [50000, 30000, 10000]}}, "events": []}}, f)
    os.environ["POL_EVENT_CALENDAR"] = calf
    announce("111", "role", [{"type": 3, "name": "game", "value": "tetra"},
                             {"type": 8, "name": "role", "value": "R42"}])

    def at(*a):
        return _cal.timegm(a + (0,) * (6 - len(a)))

    def reminders(since):
        return [c for c in net.calls[since:]
                if c[0] == "POST" and "/channels/C2/messages" in c[1]]

    k5 = len(net.calls)
    check("mid-week, no cup reminder is due",
          B.scan_event_reminders(at(2026, 9, 29, 12)) == 0 and not reminders(k5))
    n = B.scan_event_reminders(at(2026, 10, 2, 1))
    posts = reminders(k5)
    emb = posts[0][2]["embeds"][0] if posts else {}
    check("the day before the cup: ONE reminder in the bound channel",
          n == 1 and len(posts) == 1
          and emb.get("title") == "Chocobo Cup starts in 18 hours", (n, emb))
    check("...listing the sessions as Discord timestamps, with the prizes",
          "<t:%d:F>" % at(2026, 10, 2, 19) in emb.get("description", "")
          and "50,000" in emb.get("description", ""), emb.get("description"))
    check("...pinging the Tetra Master role only",
          posts and posts[0][2].get("content") == "<@&R42>"
          and posts[0][2]["allowed_mentions"] == {"parse": [], "roles": ["R42"]})
    k6 = len(net.calls)
    check("the next pass does not repeat it",
          B.scan_event_reminders(at(2026, 10, 2, 2)) == 0 and not reminders(k6))
    n = B.scan_event_reminders(at(2026, 10, 2, 16, 5))
    posts = reminders(k6)
    check("a few hours before the first night: the opening post",
          n == 1 and posts[0][2]["embeds"][0]["title"] == "Chocobo Cup starts in 3 hours",
          posts and posts[0][2]["embeds"][0]["title"])
    check("...one post for the night's two sessions",
          posts and posts[0][2]["embeds"][0]["description"].count("<t:") == 4)
    gone = [c for c in net.calls[k6:] if c[0] == "DELETE"]
    check("...and the day-before reminder it replaces is deleted",
          len(gone) == 1 and "/channels/C2/messages/M" in gone[0][1]
          and [p["cup"] for p in B._event_posts(ldb)] == ["chocobo-cup-2026W40"],
          (gone, B._event_posts(ldb)))
    k7 = len(net.calls)
    check("between nights, nothing",
          B.scan_event_reminders(at(2026, 10, 3, 10)) == 0 and not reminders(k7))
    B.scan_event_reminders(at(2026, 10, 3, 16, 5))
    posts = reminders(k7)
    check("the second night: 'next sessions' with what is left after them",
          len(posts) == 1
          and posts[0][2]["embeds"][0]["title"] == "Next Chocobo Cup sessions start in 3 hours"
          and "After these: 2 more sessions" in posts[0][2]["embeds"][0]["description"],
          posts and posts[0][2]["embeds"][0])
    k9 = len(net.calls)
    check("while the cup runs, the sweep keeps its reminder up",
          B.sweep_event_posts(at(2026, 10, 3, 17)) == 0
          and not [c for c in net.calls[k9:] if c[0] == "DELETE"])
    os.environ["POL_BRIDGE_EVENT_REMIND"] = "0"
    k8 = len(net.calls)
    check("POL_BRIDGE_EVENT_REMIND=0 turns them off",
          B.scan_event_reminders(at(2026, 10, 4, 16, 5)) == 0 and not reminders(k8))
    os.environ.pop("POL_BRIDGE_EVENT_REMIND")
    k10 = len(net.calls)
    check("an hour after the cup's last session, the sweep deletes its last reminder",
          B.sweep_event_posts(at(2026, 10, 7)) == 1 and not B._event_posts(ldb)
          and len([c for c in net.calls[k10:] if c[0] == "DELETE"]) == 1)

    print("news posts under the game's own name (the channel's webhook)")
    net.hooks = True
    B.GAME_BOTS, B._PROFILES, B._HOOK_REFUSED = {"tetra": "TMBOT"}, {}, {}
    k11 = len(net.calls)
    B.scan_event_reminders(at(2026, 10, 9, 1))
    made = [c for c in net.calls[k11:] if c[0] == "POST" and c[1].endswith("/channels/C2/webhooks")]
    sent = [c for c in net.calls[k11:] if c[0] == "POST" and "/webhooks/W1/T1?wait=true" in c[1]]
    check("the bridge makes ONE webhook in the channel and posts the reminder through it",
          len(made) == 1 and made[0][2] == {"name": "OpenLobby News"} and len(sent) == 1
          and not reminders(k11), (made, sent))
    check("...under the Tetra Master bot's name and avatar, still pinging only its role",
          sent and sent[0][2]["username"] == "Tetra Master"
          and sent[0][2]["avatar_url"].startswith("https://cdn.discordapp.com/avatars/TMBOT/av1")
          and sent[0][2]["allowed_mentions"] == {"parse": [], "roles": ["R42"]},
          sent and sent[0][2])
    check("...and the post is tracked as the webhook's",
          [p.get("hook") for p in B._event_posts(ldb)] == ["W1"], B._event_posts(ldb))
    k12 = len(net.calls)
    B.sweep_event_posts(at(2026, 10, 14))
    gone = [c for c in net.calls[k12:] if c[0] == "DELETE"]
    check("once the cup is over it is deleted through the webhook",
          len(gone) == 1 and "/webhooks/W1/T1/messages/H" in gone[0][1], gone)
    net.hooks = False
    B.GAME_BOTS, B._PROFILES = {}, {}
    os.environ.pop("POL_EVENT_CALENDAR")
    announce("111", "role", [{"type": 3, "name": "game", "value": "tetra"}])

    r = announce("111", "off")
    check("off unbinds; a second off says nothing was bound",
          "Stopped" in r["data"]["content"]
          and "No announcement channel" in announce("111", "off")["data"]["content"],
          r["data"]["content"])
    k4 = len(net.calls)
    check("with nothing bound, the scanner sits idle",
          B.scan_announce() == 0 and len(net.calls) == k4)

ldb.close()
print("\n%s" % ("ALL OK" if not FAILS else "%d FAILED: %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
