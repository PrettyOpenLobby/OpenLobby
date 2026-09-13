#!/usr/bin/env python3
"""End-to-end smoke test for the OWN-server chain: directory -> auth -> lobby -> world.

Spins the responders up IN-PROCESS on localhost (no Docker, no real SE), then
drives them exactly as the PlayOnline client would and asserts each hop both
answers correctly AND logs what it should. One command validates the whole chain;
run it after any responder change, and especially once the login worker's
completion-gate lands, to confirm nothing regressed.

    python tools/smoke_chain.py            # run the chain, print PASS/FAIL, exit 0/1
    python tools/smoke_chain.py -v         # + per-hop detail

What it does NOT test: the client-side completion gate (DAT_0386a8c8) and the real
per-account session key -- those need the actual Viewer. Here we use K=0 (token0),
which is exactly what our authserv mints, so the crypto path is the real one.
"""
import argparse
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(HERE, "..", "services")
sys.path.insert(0, SERVICES)

# Log to a scratch dir so we can assert on what each hop logged.
LOG_DIR = tempfile.mkdtemp(prefix="smoke-")
os.environ["POL_LOG_DIR"] = LOG_DIR
# ...and give the account DB a scratch path too. The auth path provisions real
# rows, so without this the test wrote a live database to the container path
# `/config/accounts.db`, which on Windows resolves to the DRIVE ROOT
# (E:\config\accounts.db). A test must not leave an account database outside its
# own temp dir, and it certainly must not touch the real one.
os.environ["POL_ACCOUNTS_DB"] = os.path.join(LOG_DIR, "accounts.db")
os.environ["POL_SESSION_FILE"] = os.path.join(LOG_DIR, "auth-sessions.json")
os.environ["POL_ROOMS_FILE"] = os.path.join(LOG_DIR, "rooms-live.json")
os.environ["POL_STAMP_FILE"] = os.path.join(LOG_DIR, "auth-stamps.json")
# Emit modes on: we WANT the lobby to answer with the full content-list reply so
# we can check it carries Tetra Master. ("full" = 81 00 accept + content + world;
# "accept" would send a bare header with no body to eyeball on live captures.)
os.environ["POL_LOBBY_EMIT"] = "derive"
os.environ["POL_AUTH_MODE"] = "welcome"

import responders          # noqa: E402  (after sys.path / env setup)
import sessioncrypt        # noqa: E402
import contentlist         # noqa: E402

responders._SELF_IP[0] = "127.0.0.1"

# Ports for the in-process stub (localhost only; distinct from the real stack).
P_DIR, P_AUTH, P_LOBBY, P_WORLD = 15240, 15241, 15220, 15330
os.environ["POL_LOBBY_PORT"] = str(P_LOBBY)
os.environ["POL_WORLD_PORT"] = str(P_WORLD)

B32 = responders.TOKEN_ALPHABET
_B32V = {c: i for i, c in enumerate(B32)}
K0_P, K0_S = sessioncrypt.bf_setkey(b"\x00" * 8)

results = []            # (name, ok, detail)
VERBOSE = False


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and (VERBOSE or not ok):
        line += f"  --  {detail}"
    print(line, flush=True)


# --------------------------------------------------------------------------- #
# in-process server plumbing
# --------------------------------------------------------------------------- #
def start(port, fn):
    def loop():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(16)
        while True:
            conn, addr = s.accept()
            threading.Thread(target=fn, args=(conn, addr), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


def boot():
    srv = "ci000.pol.com"
    start(P_DIR, lambda c, a: responders.handle_directory(c, a, "127.0.0.1",
                                                          P_AUTH, srv))
    # single auth hop, welcome mode; next_port -> lobby so both 300s point there
    start(P_AUTH, lambda c, a: responders.handle_authserv(c, a, P_AUTH, srv, P_LOBBY))
    start(P_LOBBY, lambda c, a: responders.handle_lobby(c, a, P_LOBBY, "127.0.0.1"))
    start(P_WORLD, lambda c, a: responders.handle_world(c, a, P_WORLD, "127.0.0.1"))
    time.sleep(0.4)        # let the listeners bind


# --------------------------------------------------------------------------- #
# client helpers
# --------------------------------------------------------------------------- #
def token_decode(sym):
    """40 base-32 symbols -> 25-byte redirect record (ip at [8:12], port [12:14])."""
    bits = 0
    for c in sym[:40]:
        bits = (bits << 5) | _B32V[c]
    return bits.to_bytes(25, "big")


def redirect_target(token_sym):
    raw = token_decode(token_sym)
    return socket.inet_ntoa(raw[8:12]), int.from_bytes(raw[12:14], "big")


def recv_until_idle(conn, seconds=2.0):
    conn.settimeout(0.4)
    buf = b""
    waited = 0.0
    while waited < seconds:
        try:
            ch = conn.recv(4096)
        except socket.timeout:
            waited += 0.4
            if buf:
                break
            continue
        if not ch:
            break
        buf += ch
    return buf


def recv_until_eof(conn, deadline=5.0):
    """Read until the server closes (race-free for hops that close after replying,
    e.g. the lobby -- avoids the partial-read gap recv_until_idle can hit)."""
    conn.settimeout(0.5)
    buf = b""
    waited = 0.0
    while waited < deadline:
        try:
            ch = conn.recv(4096)
        except socket.timeout:
            waited += 0.5
            continue
        if not ch:
            break
        buf += ch
    return buf


def read_log(channel):
    p = os.path.join(LOG_DIR, f"{channel}.log")
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# the four hops
# --------------------------------------------------------------------------- #
def hop_directory():
    c = socket.create_connection(("127.0.0.1", P_DIR), timeout=3)
    data = recv_until_idle(c).decode("latin1")
    c.close()
    lines = [l for l in data.split("\r\n") if l]
    threes = [l for l in lines if " 300 * " in l]
    ok = len(threes) >= 2 and any("ERROR" in l for l in lines)
    tgt = None
    if threes:
        tok = threes[0].split(" 300 * ", 1)[1].split()[0]
        tgt = redirect_target(tok)
    check("directory: 2x 300 greeting + ERROR", ok, f"lines={len(lines)}")
    check("directory: redirect -> our auth port",
          tgt == ("127.0.0.1", P_AUTH), f"decoded={tgt} want=('127.0.0.1',{P_AUTH})")
    check("directory: logged the redirect",
          "redirect to 127.0.0.1" in read_log("directory"))


#: The USER token this test's client presents. It is the SESSION ID on the auth
#: band (responders._sid_for_user_token), the same way a real launch's 48-byte
#: token is -- so the test can name the session it created instead of relying on
#: a shared address, which is what the old peer-IP keying made possible.
AUTH_USER_TOKEN = b"PmwMBA9ftoken"
#: The IV this test's client keys its session with (see _make_nick).
AUTH_IV = bytes.fromhex("1122334455667788")


def _make_nick(iv):
    """A valid K=0 NICK line the authserv's recover_iv fast-path accepts."""
    line = b"NICK UH5GRSV86:" + b"0" * 32 + b":8:pol"
    return sessioncrypt.ofb_apply(K0_P, K0_S, iv, line)


def _lobby_request(iv, op=(0x00, 0x09), payload_len=64, handle=bytes(12)):
    """A REAL lobby request frame: 40-byte self-validating header + payload,
    encrypted under this session's IV.

    It has to be real. The server identifies which session a lobby connection
    belongs to by finding the IV its header validates under (responders
    ._lobby_bind), so a frame of filler bytes names no session -- which is
    exactly what this test used to send, and it only worked because the lobby
    thread borrowed the auth session by matching IP address.
    """
    pt = bytearray(40 + payload_len)
    pt[0] = 0x02                                   # request marker
    pt[1], pt[2] = op                              # opcode pair
    struct.pack_into("<I", pt, 4, payload_len)     # 40 + this == frame length
    pt[12:24] = handle
    return responders._lobby_crypt(bytes(pt), iv)


def hop_auth():
    c = socket.create_connection(("127.0.0.1", P_AUTH), timeout=3)
    c.settimeout(3)
    # server speaks first: cleartext 300 redirect (to next hop)
    first = b""
    while b"\r\n" not in first:
        first += c.recv(4096)
    c.sendall(b"USER x 8 * :PmwMBA9ftoken\r\n")
    # server replies 300 * TOKEN0 (cleartext); then expects our encrypted NICK
    tok0 = b""
    while b"\r\n" not in tok0:
        tok0 += c.recv(4096)
    ok_tok0 = responders.TOKEN0.encode() in tok0
    iv = bytes.fromhex("1122334455667788")
    c.sendall(_make_nick(iv) + b"\r\n")
    welcome = recv_until_idle(c, 3.0)
    c.close()
    # decrypt the welcome lines with K=0 + our IV (OFB resets per line)
    dec_lines = [sessioncrypt.ofb_apply(K0_P, K0_S, iv, l)
                 for l in welcome.split(b"\r\n") if l]
    joined = b" ".join(dec_lines)
    has_001 = any(b" 001 " in l for l in dec_lines)
    lobby_tok = None
    for l in dec_lines:
        if b" 300 * " in l:
            lobby_tok = l.split(b" 300 * ", 1)[1].split()[0].decode("latin1")
            break
    tgt = redirect_target(lobby_tok) if lobby_tok else None
    check("auth: cleartext TOKEN0 issued", ok_tok0)
    check("auth: welcome decrypts under K=0 (001 present)", has_001,
          f"decoded={joined[:60]!r}")
    check("auth: welcome hands off to our lobby port",
          tgt == ("127.0.0.1", P_LOBBY), f"decoded={tgt} want=('127.0.0.1',{P_LOBBY})")
    check("auth: logged accept -> lobby",
          "accept -> lobby 127.0.0.1" in read_log("authserv"))


def hop_lobby():
    # Two-phase flow: hello -> 81 00 accept -> request -> world-address reply.
    handle = bytes.fromhex("704120127a3a3ea938bf2c41")
    hello = (b"\x00\x00\x00\x00\x01\x00\x80\xd5"
             + responders._SESSION_CONST_LE + handle + b"\x00" * 20)
    c = socket.create_connection(("127.0.0.1", P_LOBBY), timeout=3)
    c.sendall(hello)
    accept = recv_until_idle(c, 3.0)                    # Phase 1: the 81 00 accept
    # SE's accept is 24B: 81 00 + 18 zero + a LE u32 unix timestamp at 0x14
    # (proven 23/23 against the capture wall-clock -- the old "78 6a framing const"
    # and "world IP @0x14" readings were both that clock's bytes).
    stamp = struct.unpack_from("<I", accept, 0x14)[0] if len(accept) >= 0x18 else 0
    ok_accept = (accept[:2] == b"\x81\x00" and len(accept) == 24
                 and abs(stamp - int(time.time())) < 300)
    # Phase 2: send a REAL encrypted request -- a 40-byte self-validating header
    # plus a 64-byte payload, keyed with the IV the auth hop recovered. That is
    # what lets the server work out which session this socket belongs to
    # (responders._lobby_bind); a frame of filler validates under no IV and names
    # nobody, and this test only used to pass because the lobby thread borrowed
    # the auth session by matching IP address.
    req = _lobby_request(AUTH_IV, op=(0x00, 0x09), handle=handle)
    c.sendall(req)
    reply = recv_until_idle(c, 3.0)                     # Phase 3: the derived reply
    c.close()
    # The reply is a RAW record -- a 24-byte header (+8B body), NOT wrapped in a
    # second 81 00 header. Wrapping it made the client read the accept header,
    # take its zero body length, and never read the record (POL-5368 live).
    # The auth hop above recovered the session IV, and the lobby uses the SAME K=0
    # keystream, so the reply is composed in plaintext and encrypted. Decrypt it
    # back and check the structure the client will actually parse.
    # Read the session back the way a lobby thread does: name the session (this
    # client's USER token IS its id) and ask for its IV. There is deliberately no
    # "most recent session" fallback any more -- that fallback is what served
    # player two player one's account.
    responders.session_bind(responders._sid_for_user_token(AUTH_USER_TOKEN))
    iv = responders._lobby_iv()
    rpt = responders._lobby_crypt(reply, iv) if iv else b""
    world_le = socket.inet_aton("127.0.0.1")[::-1]
    check("lobby: Phase1 24B 81 00 accept with a live unix timestamp @0x14",
          ok_accept, f"accept={accept.hex()} stamp={stamp}")
    check("lobby: the lobby inherited the auth session IV", iv is not None,
          f"iv={iv.hex() if iv else None}")
    check("lobby: Phase3 reply is a raw record (no 81 00 wrapper)",
          len(reply) >= 24 and reply[:2] != b"\x81\x00",
          f"len={len(reply)} head={reply[:4].hex()}")
    check("lobby: reply decrypts to a 0x83 reply header", rpt[:1] == b"\x83",
          f"pt={rpt[:12].hex()}")
    check("lobby: reply payload length field matches the frame",
          len(rpt) >= 8 and struct.unpack_from("<I", rpt, 4)[0] == len(reply) - 24,
          f"len_field={struct.unpack_from('<I', rpt, 4)[0] if len(rpt) >= 8 else None} "
          f"frame_payload={len(reply) - 24}")
    check("lobby: reply carries the world address at plaintext [8:12]",
          rpt[8:12] == world_le,
          f"got={rpt[8:12].hex()} want={world_le.hex()} (127.0.0.1 LE)")
    check("lobby: logged the derived reply",
          "B reply via" in read_log("lobby"))


def hop_world():
    opener = bytes.fromhex("0000000001008000") + b"tetra-master-opener" + b"\x00" * 40
    c = socket.create_connection(("127.0.0.1", P_WORLD), timeout=3)
    c.sendall(opener)
    time.sleep(0.3)
    c.close()
    time.sleep(0.5)
    wl = read_log("world")
    cap_dir = os.path.join(LOG_DIR, "captures")
    caps = [f for f in os.listdir(cap_dir) if f.startswith("world-")] \
        if os.path.isdir(cap_dir) else []
    check("world: harness accepted + logged the opener",
          "world opener" in wl and "capture-only" in wl)
    check("world: opener saved to a capture file", len(caps) >= 1,
          f"captures={caps}")
    check("world: refuses to emit (protocol not reversed)",
          "POL_WORLD_EMIT" not in wl or "refusing to send" in wl or
          os.environ.get("POL_WORLD_EMIT", "0") == "0")


def hop_rooms():
    """Two clients in one room -- the half `_auth_session_reply` could not reach.

    Driven through the reply builder with stub sessions rather than two real
    logins: the thing under test is the registry and the relay, and a second live
    Viewer is not something this harness can conjure. The single-member checks are
    the important ones -- they assert the create path that WAS validated against a
    real client is byte-for-byte unchanged.
    """
    R = responders
    NICK, SRV, IP, CH = b"UBKTSUMOU", b"pol-1000-51242.pol.com", b"127.0.0.1", b"#01CUSMOKE"
    # WARNING: THE PREFIX HOST IS EMPTY -- `nick!~x@` with nothing after the `@`, which
    # is what SE sends and what the 2026-08-15 capture measures. Putting the
    # server name there is our old invention; every user-prefixed line in this
    # file uses the empty form for that reason.
    old_join = [
        b":" + NICK + b"!~x@ JOIN :" + CH,
        b":" + SRV + b" 353 " + NICK + b" = " + CH + b" :@" + NICK + b" ",
        b":" + SRV + b" 366 " + NICK + b" " + CH + b" :End of NAMES list.",
    ]

    class _C:
        def sendall(self, b): pass

    def mk(nick, ip):
        s = R.ChatSession(nick, SRV, ip, _C(), None, None, None)
        got = []
        s.send = lambda lines, g=got: (g.extend(lines), True)[1]
        return s, got

    rep = lambda cmd, s: R._auth_session_reply(cmd, s.nick, SRV, s.peer_ip, sess=s)

    check("rooms: no session -> pre-registry JOIN, byte-identical",
          R._auth_session_reply(b"JOIN " + CH, NICK, SRV, IP) == old_join)

    a, a_got = mk(NICK, IP)
    check("rooms: lone member -> pre-registry JOIN, byte-identical",
          rep(b"JOIN " + CH, a) == old_join)

    b, _b_got = mk(b"SECOND", b"192.0.2.2")
    b_join = rep(b"JOIN " + CH, b)
    check("rooms: 2nd joiner sees BOTH names in 353",
          b"@" + NICK + b" SECOND" in b_join[1], b_join[1].decode("latin1"))
    check("rooms: 1st member is told the 2nd arrived",
          any(b"SECOND" in l and b"JOIN" in l for l in a_got), str(a_got))

    who = rep(b"WHO " + CH, b)
    check("rooms: WHO lists one 352 per member", len(who) == 3, f"lines={len(who)}")

    a_got.clear()
    rep(b"PRIVMSG " + CH + b" :0 0 02X\t01hi", b)
    check("rooms: chat relays to the other member",
          any(b"01hi" in l for l in a_got), str(a_got))

    desc = b"PRIVMSG " + CH + b" :2000000deadbeef00011" + NICK
    check("rooms: room descriptor still answered with SE's 300",
          rep(desc, a) == [b":" + SRV + b" 300 " + NICK + b" " + CH])

    # *** EVERY CODE IS RELAYED, NOT JUST THE ONES THAT LOOK LIKE CHAT. *** The
    # sidebar is built from the code-2 arrival announce (NAMES carries only opaque
    # handle-nicks; the DISPLAY name is in the announce), so gating the relay on
    # "does this look like chat" is why a room with two people in it showed one --
    # and why an away flag had no row to attach to.
    for code, what in ((b"2000000deadbeef00011Cas", "arrival"),
                       (b"3000000deadbeef00011Cas", "already-here"),
                       (b"80000001", "departure"),
                       (b"a0000001Cas", "role change")):
        a_got.clear()
        rep(b"PRIVMSG " + CH + b" :" + code, b)
        check(f"rooms: the {what} announce reaches the other member",
              any(code in l for l in a_got), str(a_got))

    # PART CARRIES A LEADING COLON. Every live PART logged "no handler" until
    # 2026-08-16 because the ':' failed the '#' test, so nobody ever left a room.
    part = rep(b"PART :" + CH, b)
    check("rooms: PART :#chan is handled, colon and all",
          part == [b":SECOND!~x@ PART " + CH + b" :SECOND"],
          str(part))
    check("rooms: the PART actually removed the member",
          R.ROOMS.snapshot().get(CH.decode()) == [NICK.decode()],
          str(R.ROOMS.snapshot()))
    rep(b"JOIN " + CH, b)                            # put them back

    # TOPIC: a SET is echoed, a QUERY is answered with 332. Answering a query with
    # the echo told the channel the joiner had just blanked the topic.
    TOPIC = b"zt9rTojTK7ITSMOKEROOM"
    check("rooms: TOPIC set is echoed",
          rep(b"TOPIC " + CH + b" :" + TOPIC, a) ==
          [b":" + NICK + b"!~x@ TOPIC " + CH + b" :" + TOPIC])
    check("rooms: TOPIC query answers 332, not an echo",
          rep(b"TOPIC " + CH, b) ==
          [b":" + SRV + b" 332 SECOND " + CH + b" :" + TOPIC])
    check("rooms: a joiner is told the topic (SE sends 332 before 353)",
          rep(b"JOIN " + CH, b)[1] ==
          b":" + SRV + b" 332 SECOND " + CH + b" :" + TOPIC)

    # A LISTED room's name comes from configuration, not from anyone's TOPIC.
    listed = b"#01CPZYOTYU000003"
    ljoin = R._auth_session_reply(b"JOIN " + listed, NICK, SRV, IP)
    check("rooms: entering Novice_Hall reports its name in a 332",
          any(l.startswith(b":" + SRV + b" 332 ") and b"Novice_Hall" in l
              for l in ljoin), str(ljoin))

    # *** THE FIXTURE BOT. *** Without it the joiner is alone AND holds '@', which
    # is a created room whose owner has gone -- and the client says exactly that:
    # "This room is already closed. Create a new room?"  SE keeps PXANNNNXK
    # resident in every persistent room and the human joins plain.
    names353 = [l for l in ljoin if b" 353 " in l][0]
    check("rooms: a fixture's NAMES has the joiner PLAIN and the bot with '@'",
          names353.endswith(NICK + b" @PXANNNNXK "), names353.decode("latin1"))
    lwho = R._auth_session_reply(b"WHO " + listed, NICK, SRV, IP)
    check("rooms: WHO lists the bot too, as SE does",
          any(b"PXANNNNXK H@ :2 *Not On This Net*" in l for l in lwho), str(lwho))
    check("rooms: and the human is NOT flagged operator there",
          any(b" " + NICK + b" H :0 POL-INFO" in l for l in lwho), str(lwho))
    check("rooms: a CREATED room gets no bot, and its operator is a person",
          not any(b"PXANNNNXK" in l for l in rep(b"JOIN " + CH, a)))

    R.ROOMS.drop(b)
    check("rooms: drop removes the member",
          R.ROOMS.snapshot().get(CH.decode()) == [NICK.decode()],
          str(R.ROOMS.snapshot()))
    R.ROOMS.drop(a)
    check("rooms: empty room is reaped", CH.decode() not in R.ROOMS.snapshot())
    check("rooms: but its NAME survives the room going empty",
          R._room_topic(CH) == TOPIC, str(R._room_topic(CH)))


def hop_reports():
    """The Viewer's "Report User" dialog submits SMTP, and we used to bin it.

    The body below is the real one, from a report the account holder filed on
    2026-08-16 -- `tos@us.playonline.com` was accepted with 250 OK and then
    dropped as off-domain, so the client's whole reporting path ended nowhere.
    """
    R = responders
    BODY = (
        b"From: JDPL7746@pol.com\r\n"
        b"To: tos@us.playonline.com\r\n"
        b"Subject: Chat Harassment>Viewer\r\n"
        b"X-Mailer: SQUARE ENIX PlayOnline Mailer version 1.0000.000.3.1.18.15e\r\n"
        b"\r\n"
        b"<harassment_form_sender>asdfasdfasdf3@pol.com</harassment_form_sender>\r\n"
        b"<harassment_form_suspect>Fox</harassment_form_suspect>\r\n"
        b"<harassment_form_application>PlayOnline Chat</harassment_form_application>\r\n"
        b"<harassment_form_explanation>OK</harassment_form_explanation>\r\n"
        b"<harassment_form_log>\r\n"
        b"    LaptopTest2 2026/ 8/16/ 5/29/23 -0000 #01CPZYOTYU000003 LaptopTest2> hihihi\r\n"
        b"->   2026/ 8/16/ 5/29/54 -0000 #01CPZYOTYU000003 LaptopTest2 is away.\r\n"
        b"</harassment_form_log>\r\n")

    f = R._parse_report(BODY)
    check("reports: the harassment form parses to its five fields",
          set(f) == {"sender", "suspect", "application", "explanation", "log"},
          str(sorted(f)))
    check("reports: the reported handle is read out",
          f["suspect"] == "Fox" and f["application"] == "PlayOnline Chat")
    check("reports: the TYPED contact is kept, and is not the envelope sender",
          f["sender"] == "asdfasdfasdf3@pol.com" and
          R._smtp_header(BODY, "From") == "JDPL7746@pol.com")
    check("reports: the client's own transcript comes through multi-line",
          "hihihi" in f["log"] and "is away" in f["log"] and "\n" in f["log"])
    check("reports: an ordinary mail is NOT mistaken for a report",
          R._parse_report(b"Subject: hi\r\n\r\njust a message") == {})

    # ALIASING IS NOT RELAYING: the address is rewritten to a box on this server.
    al = R._mail_aliases()
    check("reports: SE's measured report address is aliased locally",
          al.get("tos@us.playonline.com") == "tos", str(al))
    check("reports: and its regional siblings with it",
          all(al.get(f"tos@{r}.playonline.com") for r in ("jp", "eu")))
    check("reports: an ordinary off-domain address is still NOT accepted",
          "someone@example.com" not in al)

    R.REPORT_DIR = os.path.join(LOG_DIR, "reports")
    R._archive_report("tos@us.playonline.com", "tos@pol.com", "JDPL7746@pol.com",
                      "Chat Harassment>Viewer", BODY, "test")
    filed = [n for n in os.listdir(R.REPORT_DIR) if n.endswith(".json")]
    check("reports: it is filed for the dashboard", len(filed) == 1, str(filed))
    with open(os.path.join(R.REPORT_DIR, filed[0]), encoding="utf-8") as fh:
        rec = json.load(fh)
    check("reports: the filed record carries the suspect and the transcript",
          rec["fields"]["suspect"] == "Fox" and "hihihi" in rec["fields"]["log"])
    check("reports: archiving never breaks the SMTP transaction",
          R._archive_report("x", "y", None, None, b"\xff\xfe not a report", "t")
          is None)


def hop_created_rooms():
    """A room a player creates has to appear in the browser, in their zone.

    SE lists `FOXROOM` in zone 1103 the moment it exists, on the `#01CU` prefix
    with the 40-symbol channel token as its id -- and it is gone once nobody is in
    it. Ours is driven through the same TOPIC that names the room.
    """
    R = responders
    SRV, IP = b"pol-1000-51242.pol.com", b"127.0.0.1"
    CH = b"#01CUMADEUPQ0BANC3NNNNNNNNNNNNNNNNNNNNNNN3N1N"

    class _C:
        def sendall(self, b): pass

    def mk_sess(nick, ip):
        sess = R.ChatSession(nick, SRV, ip, _C(), None, None, None, member=4242)
        got = []
        sess.send = lambda lines, *a, _g=got, **k: (_g.extend(lines), True)[1]
        return sess, got

    R._BROWSE_ZONE.clear()
    R._CREATED_ROOMS.clear()
    s, _s_got = mk_sess(b"UMAKER01", IP)
    rep = lambda cmd: R._auth_session_reply(cmd, s.nick, SRV, s.peer_ip, sess=s)

    # SE'S OWN TOPICS DECODE, so the test drives real ones rather than a shape.
    check("created: SE's Novice_Hall topic decodes to its own settings",
          R._parse_room_topic(b"zT95ToiTK7ITNovice_Hall") ==
          ({"zone": 1100, "f41": 100, "f44": 202, "lang": 301}, "Novice_Hall"),
          str(R._parse_room_topic(b"zT95ToiTK7ITNovice_Hall")))
    check("created: and CYNROOM's reads language 303 -- German, never seen elsewhere",
          R._parse_room_topic(b"zt9rToiTKtITCYNROOM")[0]["lang"] == 303)
    check("created: an unfilled dialog reads 'Not set' throughout",
          R._parse_room_topic(b"zT7TTTTTTTTTWHAT")[0] ==
          {"zone": 1100, "f41": 0, "f44": 0, "lang": 0})

    # A topic carrying FOXROOM's settings, built the way the client builds one.
    topic = (R._b64encode(struct.pack("<HHHHB", 1103, 101, 203, 301, 0))[:12]
             .encode() + b"MyRoom")
    rep(b"JOIN " + CH)
    rep(b"TOPIC " + CH + b" :" + topic)
    rows = R._room_search_rows([], 1103)
    check("created: the topic's own zone files it, no session guess needed",
          [r["handle_name"] for r in rows] == ["MyRoom"] and
          R._room_search_rows([], 1100) == R._persistent_rooms(), str(rows))
    check("created: the name is the topic minus its twelve settings symbols",
          rows[0]["handle_name"] == "MyRoom")
    check("created: members/purpose/language come from the CREATOR, not a constant",
          (rows[0]["f41"], rows[0]["f44"], rows[0]["lang"]) == (101, 203, 301),
          str(rows[0]))
    check("created: its row points back at its own channel",
          R._room_chan(rows[0]) == CH, R._room_chan(rows[0]).decode("latin1"))
    rec = R._room_record(rows[0])
    check("created: the record carries 'U', not 'P'", rec[0x08] == ord("U"))
    check("created: with SE's created-room numbers and its zone",
          struct.unpack_from("<H", rec, 0x3E)[0] == 1103 and
          struct.unpack_from("<H", rec, 0x41)[0] == 101 and
          struct.unpack_from("<H", rec, 0x44)[0] == 203)
    check("created: and is flagged player-created at +0x39",
          struct.unpack_from("<I", rec, 0x39)[0] == 1)
    # *** THE HEADCOUNT MOVED, AND THIS CHECK DID NOT. ***
    # It used to read the count at +0x99 and assert 1 there. Both halves of the
    # room record's tail were re-measured live on 2026-08-19 (memory
    # `room-record-polpro-fields`) and swapped: **+0x39 is z_npers, the headcount
    # the browser draws as `N/`**, and **+0x99 is z_chlock, the PADLOCK**. The
    # old reading was ambiguous only because SE's FOXROOM sample happened to be
    # both occupied AND keyed. So this suite went red the day the record was
    # corrected, asserting the retracted layout -- and stayed red, which is the
    # exact failure `run_all.py`'s docstring warns a stale expectation causes.
    check("created: one occupant at z_npers, ten seats -- SE's FOXROOM exactly",
          struct.unpack_from("<I", rec, 0x39)[0] == 1
          and struct.unpack_from("<I", rec, 0x94)[0] == 10,
          f"npers={struct.unpack_from('<I', rec, 0x39)[0]} "
          f"cap={struct.unpack_from('<I', rec, 0x94)[0]}")
    check("created: and NO padlock, because no key has been set yet",
          rec[0x99] == 0, f"chlock={rec[0x99]}")

    # THE CAPACITY THE CREATOR SET. `MODE +l 20` arrives AFTER the TOPIC that named
    # the room, so it cannot be frozen at registration -- it is re-read per browse.
    rep(b"MODE " + CH + b" +l 20")
    check("created: a capacity set after the TOPIC still reaches the browser",
          struct.unpack_from(
              "<I", R._room_record(R._room_search_rows([], 1103)[0]), 0x94)[0] == 20)

    # *** THE PASSWORD. *** `MODE +k` is the only place it is ever stated, and it
    # was echoed and forgotten -- so a locked room let everyone in.
    rep(b"MODE " + CH + b" +k s3cret")
    # THE OTHER HALF OF THE SWAP. Writing the headcount into +0x99 minted a
    # padlock on every occupied room; now the field only lights when a key is
    # actually set, and this is the pair of observations that tells the two
    # readings apart -- one room, before and after `MODE +k`.
    check("created: setting a key raises the padlock at z_chlock",
          R._room_record(R._room_search_rows([], 1103)[0])[0x99] == 1,
          f"chlock={R._room_record(R._room_search_rows([], 1103)[0])[0x99]}")
    check("created: ...and the headcount is untouched by it",
          struct.unpack_from(
              "<I", R._room_record(R._room_search_rows([], 1103)[0]), 0x39)[0] == 1)
    check("created: MODE reports the room as SE does, +nlk style",
          R._auth_session_reply(b"MODE " + CH, b"UOTHER01", SRV, IP) ==
          [b":" + SRV + b" 324 UOTHER01 " + CH + b" +lk 20 s3cret"],
          str(R._auth_session_reply(b"MODE " + CH, b"UOTHER01", SRV, IP)))
    check("created: joining with no password is refused, as SE's 475",
          R._auth_session_reply(b"JOIN " + CH, b"UOTHER01", SRV, IP) ==
          [b":" + SRV + b" 475 UOTHER01 " + CH + b" :Cannot join channel (+k)"])
    check("created: joining with the WRONG password is refused",
          R._auth_session_reply(b"JOIN " + CH + b" nope", b"UOTHER01", SRV, IP)[0]
          .startswith(b":" + SRV + b" 475 "))
    check("created: joining with the RIGHT password is let in",
          R._auth_session_reply(b"JOIN " + CH + b" s3cret", b"UOTHER01", SRV, IP)[0]
          .endswith(b" JOIN :" + CH))
    check("created: a room with no key is still open to everyone",
          R._auth_session_reply(b"JOIN #01CPZYOTYU000004", b"UOTHER01", SRV, IP)[0]
          .endswith(b" JOIN :#01CPZYOTYU000004"))

    # *** A BAN OUTLASTS THE KICK. *** Kicking evicts; without the ban being kept,
    # the same person walks straight back in.
    victim, vgot = mk_sess(b"UBANNED01", b"192.0.2.9")
    R.ROOMS.join(CH, victim)
    kick = rep(b"KICK " + CH + b" UBANNED01")
    check("created: KICK is SE's line -- kicker prefix, kicker as the comment",
          kick == [b":UMAKER01!~x@ KICK " + CH + b" UBANNED01 :UMAKER01"],
          str(kick))
    check("created: the victim is told before being removed",
          any(b"KICK" in l and b"UBANNED01" in l for l in vgot), str(vgot))
    check("created: and is out of the room",
          all(m.nick != b"UBANNED01" for m in R.ROOMS.members(CH)))
    rep(b"MODE " + CH + b" +b UBANNED01")
    check("created: a banned nick is refused with 474, key or no key",
          R._auth_session_reply(b"JOIN " + CH + b" s3cret", b"UBANNED01", SRV, IP) ==
          [b":" + SRV + b" 474 UBANNED01 " + CH + b" :Cannot join channel (+b)"])
    check("created: unbanning lets them back in",
          (rep(b"MODE " + CH + b" -b UBANNED01"),
           R._auth_session_reply(b"JOIN " + CH + b" s3cret", b"UBANNED01", SRV, IP)[0]
           .endswith(b" JOIN :" + CH))[1])
    check("created: somebody else is unaffected by the ban",
          R._auth_session_reply(b"JOIN " + CH + b" s3cret", b"UOTHER01", SRV, IP)[0]
          .endswith(b" JOIN :" + CH))
    check("created: the fixtures are untouched by it",
          len(R._room_search_rows([], 1100)) == 6)

    # *** THE READER PROCESS SEES ALL OF IT, OR NONE OF IT IS WORTH ANYTHING. ***
    # Rooms are joined in `authsess` and browsed in `login`. Every check above runs
    # in the process that OWNS the registry, which is exactly the blind spot that
    # let a created room register correctly and still never appear: the browser was
    # asking a different process's empty dicts. So ask the way `login` asks --
    # owner flag off, memory ignored, file only.
    owner_rows = R._room_search_rows([], 1103)
    owner_users = R._zone_summary_row(1103)["users"]
    R._ROOMS_OWNER[0] = False
    R._ROOMS_CACHE["mtime"] = -1.0
    try:
        reader_rows = R._room_search_rows([], 1103)
        check("created: a READER container sees the room too",
              [r["handle_name"] for r in reader_rows] == ["MyRoom"],
              str(reader_rows))
        check("created: and the same headcount, not zero",
              R._room_record(reader_rows[0])[0x99] ==
              R._room_record(owner_rows[0])[0x99] == 1,
              str(R._room_record(reader_rows[0])[0x99]))
        check("created: and the zone summary agrees across the boundary",
              R._zone_summary_row(1103)["users"] == owner_users == 1,
              f"reader={R._zone_summary_row(1103)} owner_users={owner_users}")
    finally:
        R._ROOMS_OWNER[0] = True

    R.ROOMS.drop(s)
    check("created: it leaves the browser when the room empties",
          R._room_search_rows([], 1103) == [])
    R._BROWSE_ZONE.clear()
    R._CREATED_ROOMS.clear()


def hop_room_zones():
    """A room belongs to ONE zone, and the browser says which one it wants.

    The frames below are real: two are SE's own `5:3` bodies from the 2026-08-15
    capture (zone 1100 and zone 1103), one is our client's. SE answered the 1100
    query with 6 hits and the 1103 one with 0 -- the persistent rooms exist in the
    PlayOnline Zone alone -- while we served all three rooms to every zone.
    """
    R = responders

    def frame(rows):
        return bytes.fromhex("".join(rows.split()))

    ours = frame("""
    02 05 03 00 54 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 a0 89 1f 13 73 68 c8 74
    76 0b 65 e2 f6 ee 3c 44 00 00 e9 03 00 00 00 00
    01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    03 0a c2 21 fb 81 68 db c2 7a 98 8e ab 7f 99 8e
    e9 03 02 00 00 00 00 00 0b 03 08 00 00 00 00 00
    01 00 00 00 00 00 00 00 06 01 08 00 00 00 00 00
    4f 04 00 00 00 00 00 00 b6 93 57 1e""")
    # SE line 102181. The six bytes after the zone are STALE HEAP -- they spell
    # "Novice", left over from the last string that buffer held -- which is why
    # the item is read as a u16 and not as the u64 its length claims.
    se1100 = frame("""
    02 05 03 00 54 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 e9 03 00 00 00 00
    01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    03 0a 00 00 54 54 54 54 78 00 00 00 54 54 54 54
    e9 03 02 00 00 00 00 00 0b 03 08 00 00 00 00 00
    01 00 00 00 00 03 4c 04 06 01 08 00 00 00 00 00
    4c 04 4e 6f 76 69 63 65 e1 2c a1 85""")
    # SE line 113358, the same shape with "CASROO" as the residue.
    se1103 = frame("""
    02 05 03 00 54 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 e9 03 00 00 00 00
    01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    03 0a 42 55 34 41 45 43 4e 4e 4e 4e 4e 4e 4e 4e
    e9 03 02 00 00 00 00 00 0b 03 08 00 00 00 00 00
    01 00 00 00 00 03 4f 04 06 01 08 00 00 00 00 00
    4f 04 43 41 53 52 4f 4f 71 4a 00 ce""")

    check("zones: our client's browse reads as zone 1103",
          R._search_zone(ours) == 1103, str(R._search_zone(ours)))
    check("zones: SE's 1100 browse reads as 1100 (heap 'Novice' ignored)",
          R._search_zone(se1100) == 1100, str(R._search_zone(se1100)))
    check("zones: SE's 1103 browse reads as 1103 (heap 'CASROO' ignored)",
          R._search_zone(se1103) == 1103, str(R._search_zone(se1103)))

    rows = lambda z: R._room_search_rows([], z)
    names = lambda z: [r["handle_name"] for r in rows(z)]
    check("zones: 1100 holds SIX rooms -- SE's own hit count",
          len(names(1100)) == 6, str(names(1100)))
    check("zones: three of them are the English set",
          [n for n in names(1100) if n.isascii()] ==
          ["Novice_Hall", "Town_Square", "Traveller's_Haven"], str(names(1100)))
    check("zones: three are the Japanese set, on the #00CP prefix",
          [r["prefix"] for r in rows(1100) if not r["handle_name"].isascii()] ==
          ["#00CP"] * 3)
    check("zones: 1103 is EMPTY, as SE answered it", names(1103) == [])
    check("zones: 1101 is EMPTY", names(1101) == [])
    check("zones: an unparsed request still lists everything",
          len(R._room_search_rows([], None)) == 6)

    by_name = {r["handle_name"]: r for r in rows(1100)}
    tpl = R._ROOM_RECORD_TEMPLATE
    rec = R._room_record(by_name["Novice_Hall"])
    check("zones: the record carries its zone at +0x3E",
          struct.unpack_from("<H", rec, 0x3E)[0] == 1100)
    # The template is SE's Novice_Hall record with only its two STRING fields
    # scrubbed to 'T' padding. So every byte we write outside those two fields --
    # the channel prefix, the zone, both unknown numbers and the language -- has to
    # come back out equal to SE's own. That is the whole table checked at once.
    moved = [i for i in range(160) if rec[i] != tpl[i]]
    check("zones: our Novice_Hall row differs from SE's ONLY in the two strings",
          all(0x0A <= i < 0x38 or 0x4A <= i < 0x93 for i in moved),
          str([hex(i) for i in moved]))

    ja = R._room_record(by_name["初心者の館"])
    check("zones: a Japanese row carries 00 in the prefix and 300 as its language",
          ja[0x04] == 0 and struct.unpack_from("<H", ja, 0x47)[0] == 300,
          f"prefix={ja[0x04]} lang={struct.unpack_from('<H', ja, 0x47)[0]}")
    check("zones: and it pairs with Novice_Hall on the two unknown numbers",
          ja[0x41:0x43] == rec[0x41:0x43] and ja[0x44:0x46] == rec[0x44:0x46])
    check("zones: its channel is #00CP + its id",
          R._room_chan(by_name["初心者の館"]) ==
          b"#00CPZYOTYU000001")

    # THE `N / M` THE BROWSER DRAWS -- occupancy is a BYTE at +0x99, capacity a u32
    # at +0x94. Settled by diffing SE's Novice_Hall record against its FOXROOM one:
    # 0 vs 1 occupants, 20 vs 10 seats. +0x41/+0x44 are neither, and are served as
    # the per-room constants SE sends.
    check("zones: an empty fixture reads 0 of 20",
          rec[0x99] == 0 and struct.unpack_from("<I", rec, 0x94)[0] == 20,
          f"occ={rec[0x99]} cap={struct.unpack_from('<I', rec, 0x94)[0]}")
    check("zones: +0x41/+0x44 stay SE's constants, uncomputed",
          struct.unpack_from("<H", rec, 0x41)[0] == 100 and
          struct.unpack_from("<H", rec, 0x44)[0] == 202)
    check("zones: a fixture is type 0, not player-created",
          struct.unpack_from("<I", rec, 0x39)[0] == 0)

    # THE ZONE SUMMARY -- SE's 16-byte record, the thing every zone and room count
    # was reading zero from.
    row = R._zone_summary_row(1100)
    check("zones: the summary counts the zone's rooms",
          row["rooms"] == 6 and row["users"] == 0, str(row))
    zrec = R._zone_summary_record(row)
    check("zones: and packs them as SE does",
          zrec == b"\x03\x03" + struct.pack("<H", 1100) +
                  b"\x03" + struct.pack("<I", 6) +
                  b"\x03" + struct.pack("<I", 0) + b"\x00\x00",
          zrec.hex(" "))
    check("zones: SE's own zone-1100 record reproduces byte for byte, bar its tail",
          R._zone_summary_record({"zone": 0x044C, "rooms": 6, "users": 0})[:14] ==
          bytes.fromhex("03034c0403060000000300000000"))
    check("zones: an empty zone summarises as empty",
          R._zone_summary_row(1103)["rooms"] == 0)


def _login(user_token, nick, iv):
    """One full launch: auth hop + lobby hop, as one client. Returns the lobby
    reply bytes (encrypted under this client's own IV)."""
    c = socket.create_connection(("127.0.0.1", P_AUTH), timeout=3)
    c.settimeout(3)
    first = b""
    while b"\r\n" not in first:
        first += c.recv(4096)
    c.sendall(b"USER x 8 * :" + user_token + b"\r\n")
    tok0 = b""
    while b"\r\n" not in tok0:
        tok0 += c.recv(4096)
    line = b"NICK " + nick + b":" + b"0" * 32 + b":8:pol"
    c.sendall(sessioncrypt.ofb_apply(K0_P, K0_S, iv, line) + b"\r\n")
    recv_until_idle(c, 2.0)
    c.close()

    lob = socket.create_connection(("127.0.0.1", P_LOBBY), timeout=3)
    hello = (b"\x00\x00\x00\x00\x01\x00\x80\xd5"
             + responders._SESSION_CONST_LE + bytes(12) + b"\x00" * 20)
    lob.sendall(hello)
    recv_until_idle(lob, 2.0)                       # the 81 00 accept
    lob.sendall(_lobby_request(iv, op=(0x00, 0x09)))
    reply = recv_until_idle(lob, 3.0)
    lob.close()
    return reply


def hop_two_clients():
    """TWO CLIENTS, ONE ADDRESS -- the case the whole session layer exists for.

    Both log in from 127.0.0.1, which is the condition the deployed stack is
    permanently in: the dev compose publishes ports through the Docker bridge, so
    every client's packets arrive from the gateway (measured on the live log:
    5,328 auth connections, one source address). Until 2026-08-13 `_SESSIONS` was
    keyed on that address, so the second login overwrote the first's slot and the
    second player was served the first player's account -- silently, because the
    lookup fell back to "most recent session".

    What makes them separable is on the wire, not in the network stack: each
    launch presents its own USER token on the auth band, and each lobby frame
    decrypts to a valid header under exactly one session's IV. Both are asserted
    here.
    """
    A_TOK, A_NICK, A_IV = b"launchAAA", b"UH5GRSV86", bytes.fromhex("1122334455667788")
    B_TOK, B_NICK, B_IV = b"launchBBB", b"UBKTSUMOU", bytes.fromhex("8877665544332211")

    a_reply = _login(A_TOK, A_NICK, A_IV)
    b_reply = _login(B_TOK, B_NICK, B_IV)

    a_sid = responders._sid_for_user_token(A_TOK)
    b_sid = responders._sid_for_user_token(B_TOK)
    sess = responders._SESSIONS
    a_mem = (sess.get(a_sid) or {}).get("member_id")
    b_mem = (sess.get(b_sid) or {}).get("member_id")

    check("2 clients: each launch got its own session",
          a_sid != b_sid and a_sid in sess and b_sid in sess,
          f"a={a_sid} b={b_sid} known={sorted(sess)}")
    check("2 clients: the two sessions resolve to DIFFERENT members",
          a_mem is not None and b_mem is not None and a_mem != b_mem,
          f"a_member={a_mem} b_member={b_mem}")
    check("2 clients: each session kept its own IV",
          (sess.get(a_sid) or {}).get("iv") == A_IV
          and (sess.get(b_sid) or {}).get("iv") == B_IV,
          f"a={(sess.get(a_sid) or {}).get('iv')} b={(sess.get(b_sid) or {}).get('iv')}")

    # The end-to-end proof: B's reply is encrypted under B's key, not A's. This is
    # the assertion that fails on the old peer-IP keying -- there, B's lobby
    # socket inherited whichever IV authenticated last.
    a_pt = responders._lobby_crypt(a_reply, A_IV) if a_reply else b""
    b_pt = responders._lobby_crypt(b_reply, B_IV) if b_reply else b""
    b_under_a = responders._lobby_crypt(b_reply, A_IV) if b_reply else b""
    check("2 clients: A's reply decrypts under A's key",
          len(a_pt) >= 8 and a_pt[0] == 0x83, f"pt={a_pt[:8].hex()}")
    check("2 clients: B's reply decrypts under B's key",
          len(b_pt) >= 8 and b_pt[0] == 0x83, f"pt={b_pt[:8].hex()}")
    check("2 clients: B's reply is NOT readable under A's key",
          not (len(b_under_a) >= 8 and b_under_a[0] == 0x83),
          f"pt={b_under_a[:8].hex()}")

    lobby_log = read_log("lobby")
    check("2 clients: the lobby bound each socket to its own session by IV",
          f"bound to session {a_sid}" in lobby_log
          and f"bound to session {b_sid}" in lobby_log,
          f"a_sid seen={f'bound to session {a_sid}' in lobby_log} "
          f"b_sid seen={f'bound to session {b_sid}' in lobby_log}")


def hop_session_file():
    """THE TWO-CONTAINER SPLIT. `authsess` recovers the IV; `login` needs it.

    They are separate processes (docker-compose: authsess runs `authserv`, login
    runs `directory,lobby,world,mail`), and the only thing joining them is
    data/auth-sessions.json. So the file has to behave like a shared table, not
    like one process's private state.

    Live failure this guards, 2026-08-13: binding a session per connection made
    `login` write its own placeholder-only table over the file, destroying the
    real sessions `authsess` had put there. The lobby then had no IV for anybody,
    every reply fell back to the XOR path, and the PS2 Viewer waited until it
    gave up -- POL-0010, "disconnected from the server".
    """
    R = responders
    real_sid, junk_sid = "uREALSESSION", "cjunk:1:2"
    with R._SESSIONS_LOCK:
        R._SESSIONS.clear()
        # `iv_claims` is NOT optional: authsess stamps it on every session the
        # moment the client authenticates with the cipher (see the live table
        # -- every real record carries one), and `_lobby_arbitrate` ranks the
        # holders of a shared IV by exactly that stamp. This fixture used to
        # omit it, so its claim read as 0.0 and the bind check lost the
        # arbitration to leftover sessions from the EARLIER HOPS of this same
        # smoke run (which share AUTH_IV by construction) -- a fixture
        # infidelity, not a server bug, and it kept this suite red from
        # 2026-08-25 until 09-02. A session that cannot lose this arbitration
        # in production must not be able to lose it here.
        R._SESSIONS[real_sid] = {"iv": AUTH_IV, "ivs": [AUTH_IV], "member_id": 42,
                                 "at": time.time(), "peer_ip": "127.0.0.1",
                                 "iv_claims": {AUTH_IV.hex(): time.time()},
                                 "viewer_open": True}
        R._sessions_save_locked()                  # stands in for `authsess`

    on_disk = json.load(open(R._SESSION_FILE, encoding="utf-8"))
    check("session file: a real session is published",
          real_sid in on_disk, str(sorted(on_disk)))

    # ...now the OTHER process starts with a placeholder-only table and saves.
    with R._SESSIONS_LOCK:
        R._SESSIONS.clear()
        R._SESSIONS[junk_sid] = {"iv": None, "ivs": [], "member_id": None,
                                 "at": time.time(), "peer_ip": "127.0.0.1"}
        R._sessions_save_locked()
    on_disk = json.load(open(R._SESSION_FILE, encoding="utf-8"))
    check("session file: the other process did NOT clobber it",
          real_sid in on_disk, f"file now holds {sorted(on_disk)}")
    check("session file: placeholder slots are not published",
          junk_sid not in on_disk, f"file now holds {sorted(on_disk)}")
    check("session file: the reader adopted the session it read",
          R._SESSIONS.get(real_sid, {}).get("member_id") == 42,
          str(R._SESSIONS.get(real_sid)))

    # and a lobby thread in THAT process can now find the IV by validation
    R.session_bind(None)
    frame = _lobby_request(AUTH_IV, op=(0x00, 0x09))
    iv, sid = R._lobby_bind(frame, "127.0.0.1", "test")
    check("session file: a lobby frame binds to the adopted session",
          iv == AUTH_IV and sid == real_sid, f"iv={iv} sid={sid}")


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = ap.parse_args().verbose

    print(f"smoke_chain: in-process stub, logs in {LOG_DIR}")
    boot()
    print("\ndirectory (51240) ->"); hop_directory()
    print("\nauth (5124x) ->"); hop_auth()
    print("\nlobby (51220) ->"); hop_lobby()
    print("\nworld (51330) ->"); hop_world()
    print("\nrooms (auth band) ->"); hop_rooms()
    print("\nroom zones (lobby band) ->"); hop_room_zones()
    print("\nplayer-created rooms ->"); hop_created_rooms()
    print("\nabuse reports ->"); hop_reports()
    print("\ntwo clients, one address ->"); hop_two_clients()
    print("\nsession file (authsess -> login) ->"); hop_session_file()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'='*54}\n{passed}/{total} checks passed"
          + (" -- CHAIN OK" if passed == total else " -- FAILURES ABOVE"))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
