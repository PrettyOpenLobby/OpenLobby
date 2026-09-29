"""The lobby responder: HTTP on the lobby band and handle_lobby."""
import os
import re
import socket
import struct
import time
from srvcore import shim_build_hidden  # noqa: E402
from srvcore import hexdump, log, save_capture
import sessioncrypt
from .deps import issuereport, kbserve, pmlfallback
from . import authcap, framing, lobbybind, lobbycapture, lobbyreply, lobbysession, pacing, portalauth, portalpages, tlsrelay



def _serve_http_on_lobby(conn, first, peer, port):
    """The Viewer tunnels its PORTAL HTTP through the pp000 band ports.

    This was invisible for a long time: `wh000.pol.com` resolves to us and the
    client dials port 80 for nothing, because the actual page fetch goes out on a
    lobby-band port as plain HTTP with `Host: wh000.pol.com`:

        GET /pml/main/index.pml HTTP/1.1
        Host: wh000.pol.com
        User-Agent: PlayOnline-PML-Viewer/1.00 [en] (Windows XP)
        Accept: text/x-playonline-pml, image/x-playonline-ang, ...
        X-PlayOnline-Want-Hello: <A64 token>

    We were answering that with a binary `81 00` lobby accept, so the Viewer sat
    on its loading spinner. Serve it as HTTP instead: a local file from /www when
    we have one, otherwise the shipped EMPTY PML document (`<!-- -->`, exactly
    what PS2 `pml/etc/none.pml` contains) so the load completes instead of
    hanging. Keep-Alive is honoured -- the client reuses the connection."""
    www = os.environ.get("POL_WWW_DIR", "/www")
    buf = first
    served = 0
    conn_authed = False   # this connection has already presented Authorization
    conn.settimeout(15)
    while True:
        while b"\r\n\r\n" not in buf:
            try:
                more = conn.recv(4096)
            except socket.timeout:
                return served
            if not more:
                return served
            buf += more
        head, _, buf = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        try:
            method, path, _ver = lines[0].split(b" ", 2)
        except ValueError:
            return served
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(b":")
            hdrs[k.strip().lower()] = v.strip()
        # CONSUME THE BODY. Nothing here reads one, so a POST left its body in
        # `buf` and the next pass through this loop parsed that body as a request
        # line, failed, and dropped the connection. At least one mirrored SE page
        # posts. We do not act on the body yet -- the CGI (ucscgi.py) is what
        # handles forms -- but it must come off the wire either way.
        try:
            clen = int(hdrs.get(b"content-length", b"0") or 0)
        except ValueError:
            clen = 0
        body_in = b""
        if clen > 0:
            while len(buf) < clen:
                try:
                    more = conn.recv(4096)
                except socket.timeout:
                    return served
                if not more:
                    return served
                buf += more
            body_in, buf = buf[:clen], buf[clen:]
            # Shim log POSTs get their own (quieter) logging in the store; the
            # live streamer alone would otherwise put a 120-byte body preview
            # here every few seconds per client.
            # WARNING: THE REPORT PATH IS SUPPRESSED HERE TOO, and for a sharper
            # reason than the live streamer's noise: a report bundle carries a
            # PNG, so a 120-byte "preview" of it is 120 bytes of binary
            # `\x89PNG\r\n...` spat into lobby.log on every report.
            if (portalpages._SHIM_LOG_PATH.encode("latin1") not in path
                    and portalpages._REPORT_PATH.encode("latin1") not in path):
                log("lobby", f"{peer}   {method.decode('latin1')} body {clen}B "
                             f"(read and discarded): {body_in[:120]!r}")
        host = hdrs.get(b"host", b"").decode("latin1").split(":", 1)[0]
        # The host names a DIRECTORY under www/, so anything that is not a
        # hostname is refused rather than normalised. Serving from a client-
        # supplied path component is the whole trick here, and it only stays safe
        # if the component is one.
        if host and not portalauth._HOST_OK.match(host):
            log("lobby", f"{peer}   refusing Host {host!r}: not a hostname")
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            return served + 1
        rel = path.decode("latin1").split("?", 1)[0].lstrip("/")
        uri = path.decode("latin1")
        # SHIM LOG SHIPPING. `[logship]` in polshim.ini POSTs a client's own log
        # here so a capture does not have to be fetched off the machine by hand.
        # It lives on the CLIENT-FACING http path on purpose: the admin server
        # also takes POSTs, but it binds 127.0.0.1, and the machine this is most
        # worth having from is the Steam Deck -- a Tailscale peer that cannot
        # reach the loopback interface at all.
        # THE REPORT CHORD. Same door, same rationale as the log path below:
        # the admin server also takes POSTs but binds 127.0.0.1, and the machine
        # a report is most worth having from is the Steam Deck, which is a
        # Tailscale peer that cannot reach loopback at all.
        if method == b"POST" and rel == portalpages._REPORT_PATH:
            rid = ""
            if issuereport is None:
                resp_code = b"503 Service Unavailable"
                log("lobby", f"{peer}   report REFUSED: issuereport not loaded")
            else:
                try:
                    resp_code, rid = issuereport.store(body_in, hdrs, peer)
                except Exception as e:   # noqa: BLE001 -- see below
                    # A REPORT MUST NEVER COST THE REPORTER THEIR SESSION. This
                    # runs on the client-facing portal door; an escaping
                    # traceback here drops the connection of a client whose only
                    # crime was telling us something was wrong -- and it would
                    # look, from their side, exactly like the bug they were
                    # reporting. store() is written not to raise; this is the
                    # belt to that pair of braces.
                    resp_code = b"500 Internal Server Error"
                    log("lobby", f"{peer}   report FAILED: {e!r}")
            # THE ID IS THE BODY. The shim reads it back and shows it in its
            # "thanks, sent" box, which is what lets a tester say WHICH report
            # they filed. Plain text, no JSON: the client-side reader is 20
            # lines of WinINet and a parser there would be a liability, not a
            # feature.
            rbody = rid.encode("ascii", "ignore")
            conn.sendall(b"HTTP/1.1 " + resp_code + b"\r\nContent-Type: text/plain\r\n"
                         + b"Content-Length: " + str(len(rbody)).encode() + b"\r\n"
                         + b"Connection: keep-alive\r\n\r\n" + rbody)
            served += 1
            if hdrs.get(b"connection", b"").lower() != b"keep-alive":
                return served
            continue
        if method == b"POST" and rel == portalpages._SHIM_LOG_PATH:
            resp_code = portalpages._shim_log_store(body_in, hdrs, peer)
            conn.sendall(b"HTTP/1.1 " + resp_code + b"\r\nContent-Length: 0\r\n"
                         b"Connection: keep-alive\r\n\r\n")
            served += 1
            if hdrs.get(b"connection", b"").lower() != b"keep-alive":
                return served
            continue
        # latin1, not the default utf-8: the PS2 Viewer sends Shift-JIS bytes in
        # the request line (0x95 etc. -- a handle or query value), and a strict
        # decode HERE, in the log call, raised straight out of the handler and
        # dropped the connection -> POL-0008 "ネットワークに到達できません".
        # Everything else in this function already decodes latin1; only the log
        # line did not, so the request parsed fine and we died printing it.
        # The User-Agent names the PLATFORM ("(PlayStation 2)" vs "(Windows XP)"),
        # which is the only thing in a portal request that separates the two
        # Viewers -- both reach us from the same docker gateway address, so the
        # peer IP cannot tell them apart. Logged because the PS2 needs different
        # documents than the PC (no $_USER_LANG; see the lang fallback below).
        ua = hdrs.get(b"user-agent", b"").decode("latin1")
        log("lobby", f"{peer} HTTP/{port} {method.decode('latin1')} "
                     f"{uri} (Host: {host}{'; UA: ' + ua if ua else ''})")
        # REGISTRATION / ACCOUNT CGI. Once the sign-up wizard is patched to use
        # http:// instead of https://, it fetches /pml-cgi-bin/... over PLAIN HTTP
        # on THIS band port (measured 2026-08-13: the client uses 51304 for plain,
        # not the TLS band 51305). Those are dynamic ucscgi endpoints, not static
        # www/ files -- without this the lookup below serves EMPTY PML and the
        # wizard hangs. Relay them to ucscgi exactly as stub.py does on :80; one
        # ucscgi process owns the account-DB session, reached from either door.
        if rel.startswith("pml-cgi-bin/"):
            tlsrelay._relay_ucs_cgi(conn, method, path, hdrs, body_in, peer)
            return served + 1
        # THE Q&A KNOWLEDGE BASE (kbserve.py). The Japanese GM Call pages link
        # it over plain http, which lands on this door; https reaches stub.py.
        if kbserve is not None and rel.startswith("polapps/s/s.kb.pml."):
            got = kbserve.handle(method.decode("latin1"), uri, body_in,
                                 {k.decode("latin1"): v.decode("latin1") for k, v in hdrs.items()},
                                 "http", host or "wh000.pol.com")
            if got:
                _st, ctype, page = got
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype.encode()
                             + b"\r\nCache-Control: no-cache\r\nContent-Length: "
                             + str(len(page)).encode() + b"\r\nConnection: keep-alive\r\n\r\n"
                             + (page if method != b"HEAD" else b""))
                served += 1
                if hdrs.get(b"connection", b"").lower() != b"keep-alive":
                    return served
                continue
        # Mutual-auth handshake (opt-in) -- ONLY for the POL realm host. SE's
        # challenge is scoped (`domain="/pml/"`) and the client only digest-auths
        # wh000; it does NOT authenticate other hosts (e.g. info.playonline.com's
        # /snews/ server-info page), so challenging those makes the client 401-loop
        # and hang -> POL-0008. Challenge only POL_PORTAL_REALM_HOSTS (default
        # wh000.pol.com); serve every other host directly.
        # SE scopes the challenge by BOTH host and path: the challenge carries
        # domain="/pml/", and the captured /snews/ request got 404 (not 401). So
        # challenge only the realm host AND paths under /pml/; serve everything
        # else open (/pcd/, /snews/, images fetched pre-auth, other hosts).
        # Once a request on THIS connection has carried an Authorization header,
        # every later request on it is served straight away. In practice the
        # client re-challenges rarely anyway -- it caches the realm and
        # pre-emptively signs subsequent /pml/ fetches, so a full render costs
        # ONE 401, not one per asset (measured: 2026-08-10 render, 26 assets, a
        # single 401 on the first index.pml). Kept because it is free and makes
        # the keep-alive path correct.
        realm_hosts = set(os.environ.get(
            "POL_PORTAL_REALM_HOSTS", "wh000.pol.com").split(","))
        realm_prefix = os.environ.get("POL_PORTAL_REALM_PATH", "/pml/")
        if b"authorization" in hdrs:
            # WARNING: PRESENCE, NOT VALIDITY -- and that is deliberate, not an
            # oversight to "fix" casually. The challenge exists because the
            # client gates its BODY load on the mutual-auth handshake (POL-0008),
            # not because this server authenticates over HTTP: the session is
            # already authenticated on the lobby band, and HA1 = MD5(user:POL:
            # secret) needs a secret we do not hold (see _portal_auth_headers).
            # Checking the digest would therefore reject every real client.
            conn_authed = True
        if os.environ.get("POL_PORTAL_AUTH", "0") == "1" \
                and host in realm_hosts and uri.startswith(realm_prefix) \
                and not conn_authed \
                and b"authorization" not in hdrs:
            conn.sendall(portalpages._portal_challenge(peer, port))
            log("lobby", f"{peer}   401 challenge (x-MD5-pol) for {host}/{rel}")
            served += 1
            if hdrs.get(b"connection", b"").lower() != b"keep-alive":
                return served
            continue
        body, ctype, validators = None, "text/x-playonline-pml", None
        # WHICH PAGE SET THIS CLIENT GETS. Era roots are tried before the shared
        # tree, so a 2004 Viewer can be served 2004 pages without touching what
        # the modern clients see. `era` is decided once per request, from this
        # client's OWN X-POL-VIEWER-VERSION where it sends one -- the header is
        # per-request, unlike the build table, which is keyed by an address every
        # client shares.
        vver = hdrs.get(b"x-pol-viewer-version", b"").decode("latin1")
        era, era_why = portalpages._client_era(peer.rsplit(":", 1)[0], ua, vver)
        # Log every CHANGE of verdict, not every request. A wrong era is served
        # as a healthy 200 and is invisible in the request log, and the line
        # below it only fires when an era root actually wins -- so a client that
        # flips to panel and finds no panel file left no trace at all. One line
        # per flip is a trail; one per request is 300 lines a render.
        if (era, era_why) != portalpages._ERA_LAST.get(ua):
            portalpages._ERA_LAST[ua] = (era, era_why)
            log("lobby", f"{peer}   era -> {era} ({era_why}) for {ua or 'unknown UA'}")
        lang = portalpages._ua_lang(ua)
        roots = portalpages._portal_roots(www, era, lang)
        cand, root_label = None, None
        # THE SHIM BUNDLE IS HOST-INDEPENDENT. Everything else here is looked up as
        # <root>/<host>/<rel>, which is right for portal pages -- they belong to
        # wh000.pol.com and friends -- but wrong for /shim/, which the shim INSTALLER
        # fetches with the bare server address as the Host. The installer cannot use
        # :80: that door is the `web` compose profile, off by default on prod because
        # the TrueNAS UI owns port 80. The 5130x band is the port every client already
        # reaches (see pol-client-http), so serving /shim/ here is what makes
        # "check the server for a newer shim" work on every deployment instead of only
        # where :80 happens to be up. Narrow on purpose: one prefix, no era or language
        # mapping, still confined to www/ by _under.
        if rel.split("/", 1)[0] == "shim":
            probe = os.path.normpath(os.path.join(www, rel))
            if shim_build_hidden(peer.rsplit(":", 1)[0], rel):
                # answered as absent: the v0.1.0 installers treat a missing
                # .sha256 as "no update here" and install what they carry
                log("lobby", f"{peer}   /{rel}: shim build hidden from an internet peer")
                probe = os.path.join(www, "shim", ".hidden-from-internet")
            if portalauth._under(www, probe) and os.path.isfile(probe):
                cand, root_label = probe, "www"
        for root, label in ([] if cand else roots):
            probe = os.path.normpath(os.path.join(root, host, rel))
            if portalauth._under(root, probe) and os.path.isfile(probe):
                cand, root_label = probe, label
                break
        if cand is None:
            cand = os.path.normpath(os.path.join(www, host, rel))
        elif root_label != "www":
            log("lobby", f"{peer}   era {era} ({era_why}): served from {root_label}")
        # QUERY-STRING PAGES. `rel` has the query stripped, which is right for
        # static art but wrong for the FFXI community pages: SE served a
        # DIFFERENT page per query (mepm015.pml?crt_url=015&crt_bt=2 and
        # ...&crt_bt=5 are two documents). The PS2's own URL cache stored them
        # under the percent-encoded full URI, and that is how they are mirrored
        # here, so when the bare path misses, try the encoded whole thing before
        # giving up. 122 of the 210 pages recovered from that cache are this
        # shape -- without it they sit on disk unreachable.
        if not os.path.isfile(cand) and "?" in uri:
            enc = (uri.lstrip("/").replace("%", "%25").replace("?", "%3F")
                      .replace("&", "%26").replace("=", "%3D"))
            for root, label in roots:
                alt = os.path.normpath(os.path.join(root, host, enc))
                if portalauth._under(root, alt) and os.path.isfile(alt):
                    log("lobby", f"{peer}   query page [{label}]: {rel} "
                                 f"-> {os.path.basename(alt)}")
                    cand = alt
                    break
        # LANGUAGE SEGMENT THE CLIENT COULD NOT RESOLVE.
        # /pcd/ paths are keyed by language: the page builds the URL itself as
        # $nwPath+$_USER_LANG+'/latestnews.pml'. The PS2 has NO $_USER_LANG (the
        # console's name is $_LANG), and an unresolved variable is substituted
        # into the text as the literal `(変数エラー)` in Shift-JIS -- so the
        # console asks for /pcd/ntool/(変数エラー)/latestnews.pml, gets the empty
        # PML below, and index.pml then renders blank with whatever background
        # the previous page left on screen. index.pml now picks the right
        # variable per $_PLATFORM, but this is the net under it: every other
        # page that interpolates a language keeps working without being edited,
        # whatever the console actually returns.
        # Deliberately NARROW: only fires when the segment is not a well-formed
        # language tag, so a genuinely missing en-US page still 404s/empties
        # rather than silently serving Japanese to a PC client.
        # The client's OWN language is tried first now: this net used to serve a
        # Japanese page to an English console because the configured order led
        # with ja-JP, and the console's short `ja` never matched a directory.
        if not os.path.isfile(cand) and rel.lower().startswith("pcd/"):
            parts = rel.split("/")
            if len(parts) >= 4 and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[2]):
                order = ([lang] if lang else []) + [
                    s.strip() for s in os.environ.get(
                        "POL_PORTAL_LANG_FALLBACK", "ja-JP,en-US").split(",")
                    if s.strip()]
                done = False
                for want in order:
                    for root, label in roots:
                        alt = os.path.normpath(os.path.join(
                            root, host, "/".join(parts[:2] + [want] + parts[3:])))
                        if portalauth._under(root, alt) and os.path.isfile(alt):
                            log("lobby", f"{peer}   lang fallback [{label}]: "
                                         f"{parts[2]!r} is not a language tag "
                                         f"-> {want}")
                            cand, done = alt, True
                            break
                    if done:
                        break
        if portalauth._under(www, cand) and os.path.isfile(cand):
            with open(cand, "rb") as f:
                body = f.read()
            validators = portalauth._portal_validators(cand)
            # Honour a .polmeta sidecar (JSON {"headers":{"Content-Type":...}}),
            # as stub.py's mirror writes -- the signup worker's form relies on its
            # `text/x-playonline-pml;charset=UTF-8`. Default by extension otherwise.
            meta = cand + ".polmeta"
            if os.path.isfile(meta):
                try:
                    import json as _json
                    mh = _json.load(open(meta, encoding="utf-8")).get("headers", {})
                    ctype = next((v for k, v in mh.items()
                                  if k.lower() == "content-type"), ctype)
                except (OSError, ValueError):
                    pass
            elif cand.lower().endswith(".png"):
                ctype = "image/png"
            elif cand.lower().endswith(".ang"):
                ctype = "image/x-playonline-ang"
            elif cand.lower().endswith(".jpg"):
                ctype = "image/jpeg"
            elif cand.lower().endswith((".dll", ".exe")):
                ctype = "application/octet-stream"    # shim bundle, not a portal page
            elif cand.lower().endswith((".sha256", ".md", ".sh", ".ps1", ".ini")):
                ctype = "text/plain"
            # The www tree is MIXED-ENCODING and always has been. Measured
            # 2026-09-20 over 3,900 served .pml: 2,242 are cp932, 1,499 are
            # UTF-8, 159 are pure ASCII -- and the split is exact, no cp932 file
            # carries a BOM and every BOM file is UTF-8.
            #
            # The PS2 Viewer defaults to cp932 and does NOT honour a UTF-8 BOM
            # (ff11/index.pml had one and was still mis-decoded). A UTF-8 page
            # served with no charset is therefore read as cp932, which corrupts
            # the Japanese inside SE's own `<!-- -->` comments and ends them
            # early -- leaking commented-out markup into the live document. That
            # is what put SE's `$_IS_UPDATE_SUCCESS` debug box on screen and
            # killed the FFXI menu, and it emitted `(Variable Error)` URLs.
            #
            # So sniff PER FILE. Pure ASCII needs no charset, UTF-8 must say so,
            # cp932 keeps the default. WARNING: Do NOT blanket-declare UTF-8: it would
            # mis-serve the 2,242 cp932 pages, i.e. most of the tree.
            if ctype.startswith("text/x-playonline-pml") and "charset" not in ctype:
                if any(b >= 0x80 for b in body):
                    try:
                        body.decode("utf-8")
                        ctype += ";charset=UTF-8"
                    except UnicodeDecodeError:
                        pass          # genuinely cp932 -- the Viewer's default
            log("lobby", f"{peer}   served {cand} ({len(body)}B, {ctype})")
        fallback_hit = False
        if (body is None and pmlfallback is not None
                and rel.lower().endswith(".pml")
                and os.environ.get("POL_PML_FALLBACK", "1") == "1"):
            # No file: the built-in page for this path (a menu, a title page
            # with Play, a data fragment). Same knob and same log phrase as the
            # :80 door in stub.py; a file under www/ has already won above.
            body = pmlfallback.fallback_page(host, rel)
            if body is not None:
                fallback_hit = True
                log("lobby", f"{peer}   [http] fallback page for /{rel}")
        if body is None and (rel.lower().endswith(".pml") or
                             b"x-playonline-pml" in hdrs.get(b"accept", b"")):
            body = b"<!-- -->\r\n"
            log("lobby", f"{peer}   served EMPTY PML for /{rel} "
                         "(no file under www/) -- author this page next")
        if body is None:
            resp = (b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                    b"Connection: keep-alive\r\n\r\n")
            log("lobby", f"{peer}   404 /{rel}")
        else:
            # Mutual-auth response headers (X-PlayOnline-Hello + rspauth). See
            # _portal_auth_headers -- rspauth needs POL_PORTAL_SECRET.
            extra = portalauth._portal_auth_headers(hdrs, method.decode("latin1"), uri)
            extra += portalauth._portal_cache_headers(validators)
            if fallback_hit:
                # Nothing on disk to derive validators from, and a page dropped
                # under www/ should show on the next render, not after a
                # cached lifetime.
                extra += b"Cache-Control: no-cache\r\n"
            if validators and portalauth._portal_not_modified(hdrs, validators):
                resp = (b"HTTP/1.1 304 Not Modified\r\n"
                        + extra + b"Connection: keep-alive\r\n\r\n")
                log("lobby", f"{peer}   304 /{rel} (client cache is current)")
            else:
                resp = (b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: " + ctype.encode() + b"\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                        + extra + b"Connection: keep-alive\r\n\r\n"
                        + (b"" if method == b"HEAD" else body))
        pacing._band_send(conn, resp, port, peer, hdrs)
        served += 1
        if hdrs.get(b"connection", b"").lower() != b"keep-alive":
            return served


def handle_lobby(conn, addr, port, stub_ip):
    """Serve one lobby (pp000) connection: read the plaintext hello, optionally
    ACCEPT (81 00) to elicit the client's post-accept body, and capture it.

    POL_LOBBY_EMIT (default 0):
      0 / off      observe-only -- capture the hello, send NOTHING. Safe, but the
                   client waits and never sends its body (nothing to elicit it).
      accept / 1   send a bare 81 00 ACCEPT header, then capture the client's
                   request and stop. Use to re-capture the request.
      derive       RECOMMENDED. accept, then answer the request with a reply
                   derived from the client's own bytes (token XOR the measured
                   mask + echoed session handle) -- the shape SE actually sends.
      reject       CONTROL ONLY: 81 e8. The client reads it as "session refused"
                   -> POL-0512. Not a route to the games menu (see above).
      full         legacy: ACCEPT + a speculative world-address body, or a verbatim
                   SE 652B template replay (POL_LOBBY_TEMPLATE). Both predate the
                   frame decode and answer a 104B request with the wrong message.
    The framing (81 00 | 18 zero | u32 unix time @0x14 | body @0x18) and the
    reply's field layout are now confirmed from the 21 SE capture pairs."""
    # Bind this thread to a session of its OWN. Provisional (one id per
    # connection) until something on the wire names the launch: the USER token on
    # the auth band, a validating IV on the lobby band. Never the address -- two
    # clients behind the Docker bridge share one, and sharing a slot is how the
    # second was served the first's account.
    lobbysession.session_bind(lobbysession._sid_for_connection(addr[0], addr[1]))
    peer = f"{addr[0]}:{addr[1]}"
    emit = os.environ.get("POL_LOBBY_EMIT", "0").strip().lower()
    if emit in ("1", "true", "yes", "on"):
        emit = "accept"
    try:
        buf = framing._read_frame(conn, idle=1.0, maxwait=8.0, minlen=8,
                          until=framing._lobby_until(addr[0], http=True))
        if not buf:
            log("lobby", f"{peer} port {port}: no data")
            return
        # The portal tunnels HTTP over these same ports -- dispatch on the wire.
        if buf.startswith(lobbyreply._HTTP_METHODS):
            save_capture(f"lobby-{port}-http", buf)
            n = _serve_http_on_lobby(conn, buf, peer, port)
            log("lobby", f"{peer} port {port}: served {n} HTTP request(s)")
            return
        # ...and it tunnels HTTPS over them too. THIRD protocol on one port.
        if tlsrelay._is_tls_hello(buf):
            save_capture(f"lobby-{port}-tls", buf)
            tlsrelay._relay_tls(conn, buf, peer, port)
            return
        cap = save_capture(f"lobby-{port}", buf)
        h = lobbyreply._parse_lobby_hello(buf)
        handle = h["handle"]
        variant = h.get("magic_variant")
        # ...and the same byte decides the SHAPE of some replies, not just the
        # send cadence -- a title's ranking header is 24 bytes here and 28 on the PC.
        pacing._peer_build.ps2 = pacing._hello_is_ps2(variant)
        pacing._peer_build.ip = addr[0]
        pacing._peer_build.pace_note = None          # _lobby_pace_ps2 logs once per hello
        # +0x09 identifies the CLIENT: 0xfa = PC Viewer, 0x00 = PS2 Viewer.
        # Worth surfacing -- it is the only field seen so far that tells the two
        # apart on this channel, and the two do not speak it identically.
        who = {0xFA: "PC", 0x00: "PS2"}.get(variant, "?")
        vtxt = "none" if variant is None else f"{variant:#04x} ({who})"
        log("lobby", f"{peer} port {port}: {len(buf)}B hello "
                     f"opcode={h.get('opcode')} token_hi={h.get('token_hi')} "
                     f"magic_ok={h['magic_ok']} magic[+0x09]={vtxt} "
                     f"handle={handle.hex() if handle else None}; saved {cap}\n"
                     + hexdump(buf))
        if not h["magic_ok"]:
            # The world hop comes back to THIS port (record1 keeps the gate
            # record's port and only its IP is replaced by our reply), so a
            # connection that is not a lobby hello is very likely the world
            # opener -- the bytes the TM world workstream needs.
            save_capture(f"world-via-{port}", buf)
            log("lobby", f"{peer}   *** NOT a lobby hello (no 6c37fab1 @0x08) -- "
                         "this is probably the WORLD opener arriving on the lobby "
                         "port; saved as world-via-*. Proceeding anyway.")
        # Try to read the hello's own body (usually empty for our K=0 40-byte
        # hello; populated on a real SE 116B frame). K=0 by default; overridable.
        key = lobbyreply._lobby_key()
        P, S = sessioncrypt.bf_setkey(key)
        if len(buf) > 0x28:
            iv_name, off, pt, hits = lobbyreply._lobby_try_decrypt(buf, P, S)
            if pt is not None:
                log("lobby", f"{peer} hello-body best decode key={key.hex()} "
                             f"{iv_name} body_off={hex(off)}: {len(hits)} "
                             f"content-list hit(s)\n" + hexdump(pt[:96]))
                for hoff, parsed in hits:
                    names = [e["name"] for e in parsed["entries"][: parsed["count"] or 0]]
                    log("lobby", f"{peer}   content-list @+{hoff}: "
                                 f"flags={parsed['flags']} count={parsed['count']} {names}")

        # REJECT mode: kept only as a control. The premise it was built on ("SE
        # rejects the probe too, and the client shrugs it off") is FALSE: of the 21
        # captured SE responses, all 18 real probes got 81 00 ACCEPT, and the only
        # two 81 e8 rejects were SE refusing OUR zero-token hello. Telling the
        # client "rejected" is exactly what raises POL-0512, so this is not a route
        # to the games menu. (It is now at least SE-shaped: 24B with a real
        # timestamp, not the client's echoed trailer.)
        if emit == "reject":
            conn.sendall(framing._build_lobby_frame(0xe8))                  # 24B, like SE
            log("lobby", f"{peer} SENT 81 e8 reject (CONTROL ONLY -- the client "
                         "reads this as 'session refused' -> POL-0512)")
            return

        # Two-phase flow (confirmed sequence): hello -> ACCEPT -> client sends its
        # request (the 104B token+signature) -> we REPLY with the world address.
        # The channel is PLAINTEXT (the request is proven-plaintext tokens from the
        # dump), so the reply body is NOT encrypted.
        follow = int(os.environ.get("POL_LOBBY_FOLLOW", "8"))
        # POL_LOBBY_ADDR14 forced our IP into the 0x14 dword, from the old reading
        # that the client took the world address from there (the shim's
        # `worldIpStore` fires on it). That dword is a CLOCK -- the shim was
        # watching the client store the server's timestamp -- so the default is now
        # OFF and the field carries a real time_t. Set it to 1 only to re-run that
        # A/B deliberately.
        world_ip = os.environ.get("POL_WORLD_IP") or authcap._self_ip()
        f14 = socket.inet_aton(world_ip) \
            if os.environ.get("POL_LOBBY_ADDR14", "0") == "1" else None
        if emit in ("accept", "full", "derive"):
            conn.sendall(framing._build_lobby_frame(0x00, field14=f14))   # Phase 1: 81 00 accept
            log("lobby", f"{peer} Phase1: SENT 81 00 accept "
                         f"(0x14={'world IP '+world_ip if f14 else 'unix time'}); "
                         "awaiting request")
        else:
            log("lobby", f"{peer} observe-only (POL_LOBBY_EMIT unset); NOT replying")

        # DERIVE: a real CONVERSATION, not one shot. The client keeps the socket
        # open and sends further messages (we have seen 104-, 64- and 456-byte
        # ones, on several parallel connections), so loop: read, split a burst
        # into messages, answer each, repeat until it stops talking.
        if emit == "derive":
            import time as _time
            turn = 0
            while True:
                t0 = _time.time()
                req = framing._read_frame(conn, idle=1.0, maxwait=follow, minlen=1,
                                  until=framing._lobby_until(addr[0]))
                if not req:
                    # Distinguish the two ways a read comes back empty. A client
                    # that got what it wanted CLOSES immediately (EOF, well under
                    # the follow window); one that is still waiting on us holds the
                    # socket open until the window elapses. That is exactly how the
                    # unanswered (04,06) fetch was isolated from the two satisfied
                    # conversations.
                    waited = _time.time() - t0
                    verdict = ("client CLOSED -- satisfied" if waited < follow * 0.8
                               else "TIMED OUT -- client is still WAITING on us")
                    log("lobby", f"{peer} conversation ended after {turn} "
                                 f"exchange(s), {waited:.1f}s: {verdict}")
                    # Do NOT fall straight through to `finally: conn.close()` --
                    # that FIN is what strands a queued reply on the PS2
                    # (see _lobby_linger's docstring).
                    pacing._lobby_linger(conn, peer)
                    break
                turn += 1
                rcap = save_capture(f"lobby-{port}-req", req)
                # SELECT THE IV BEFORE SPLITTING -- the splitter decrypts each
                # candidate header to find message boundaries, so handing it the
                # wrong session's IV mis-slices the read before anything is
                # parsed. Lenient check here (the first header must validate
                # *within* the read, not equal it, because a read may carry
                # several messages).
                #
                # ...and BINDING happens here too. The validating IV names the
                # session (see _lobby_bind), so from this point on every record
                # builder on this thread reads the right account. It is the only
                # thing that ties a lobby socket to a login: no address, no
                # ordering, no most-recent guess.
                sel, sid = lobbybind._lobby_bind(req, addr[0], peer, exact=False)
                cands = lobbybind._lobby_iv_candidates(addr[0])
                iv = sel or lobbybind._lobby_iv()
                if sel is None and cands:
                    log("lobby", f"{peer} no session claims this frame "
                                 f"({len(cands)} candidate IV(s) tried) -- "
                                 f"answering under {iv.hex() if iv else 'no IV'}")
                msgs = lobbyreply._split_lobby_messages(req, iv)
                log("lobby", f"{peer} turn {turn}: +{len(req)}B = {len(msgs)} "
                             f"message(s) {[len(m) for m in msgs]}; saved {rcap}\n"
                             + hexdump(req, 128))
                out = b""
                for m in msgs:
                    use_iv = iv                 # the IV the REPLY is built under
                    if iv:
                        # Read it. The channel is the K=0 session cipher, so this
                        # is the real message, not a guess.
                        #
                        # PICK the IV rather than assuming the newest: with a game
                        # launched there are two auth sessions on one address, and
                        # the header validates itself, so the right one is
                        # identifiable. Falls back to `iv` so behaviour is
                        # unchanged when nothing validates (then the log's
                        # MISMATCH still tells the truth).
                        picked, _sid = lobbybind._lobby_bind(m, addr[0], peer)
                        if picked is None:
                            pt = lobbybind._lobby_crypt(m, iv)
                        else:
                            use_iv = picked     # answer under the SAME key we read
                            pt = lobbybind._lobby_crypt(m, picked)
                        if picked is not None and picked != iv:
                            log("lobby", f"{peer}   IV: used {picked.hex()} "
                                         f"(not {iv.hex()}) -- {len(cands)} "
                                         f"candidate(s); header validates under "
                                         f"this one")
                        plen = struct.unpack_from("<I", pt, 4)[0] \
                            if len(pt) >= 8 else -1
                        # 3:0 dumps in full -- its window fields and the caller's
                        # identity both live past the old 128-byte cut. See
                        # _LOBBY_DUMP_FETCH.
                        cut = framing._LOBBY_DUMP_FETCH \
                            if len(pt) >= 3 and pt[1] == 0x03 and pt[2] == 0x00 \
                            else framing._LOBBY_DUMP
                        log("lobby", f"{peer}   DECRYPTED {len(m)}B: type=0x{pt[0]:02x} "
                                     f"opcode={pt[1]:02x},{pt[2]:02x} payload_len={plen} "
                                     f"(40+len={40 + plen}, "
                                     f"{'MATCHES' if 40 + plen == len(m) else 'MISMATCH'})\n"
                                     + hexdump(pt, cut))
                        lobbycapture._lobby_capture(pt)
                    body = lobbyreply._derive_lobby_reply(m, use_iv)
                    if body is None:
                        log("lobby", f"{peer}   message {len(m)}B too short to "
                                     "answer; skipping")
                        continue
                    # NOTE (2026-08-13): a TYPE-CHECK diagnostic lived here for one
                    # deploy and was removed. It re-encrypted every reply just to
                    # read it back, and `_lobby_crypt` runs a full Blowfish key
                    # schedule per call -- so it doubled the crypto per reply and
                    # made smoke_chain flap (38 -> 34 -> 32, the client timing out
                    # mid-exchange). If this is ever needed again, hoist the key
                    # schedule out of the loop first.
                    #
                    # It did answer its question: our reply round-trips to
                    # `[0]=0x83 [1]=0x00` under the same IV, so the type byte the
                    # client turns into -5326 (= -5200 - 126) is NOT coming from
                    # our encryption. See ps2-lobby-reply-reader.
                    how = f"plaintext (IV={use_iv.hex()})" if use_iv else \
                          f"XOR-derived (no IV; mask={lobbyreply._lobby_mask(len(m)).hex()})"
                    log("lobby", f"{peer}   {len(m)}B -> {len(body)}B reply via {how}, "
                                 f"world={framing._lobby_world_ip()}\n" + hexdump(body))
                    out += body
                if out:
                    # RAW -- no 81 00 wrapper. The record IS a 24-byte header
                    # (+body); wrapping it is what caused POL-5368.
                    #
                    # POL_LOBBY_SPLIT=<ms>: write the 24-byte header as its OWN
                    # segment, pause, then the remainder. 0/unset = one write,
                    # which is what every client has always been served -- leave
                    # it off unless you are running the experiment below.
                    #
                    # WHY (2026-08-15):
                    # The PS2 title's save fetch parks with a receive posted for
                    # EXACTLY 24 bytes (slot +0x324 {buf=0x201af700, len=24})
                    # while the full 1004-byte reply sits on the console, and it
                    # never completes -- FROZEN, not spinning. If that completion
                    # is edge-triggered on a segment arriving, and the single
                    # 1004-byte delivery somehow does not raise it, then a 24-byte
                    # segment matching the posted recv exactly should.
                    #
                    # HONEST PRIOR: weak. Tetra Master's 428B reply is a single
                    # segment larger than its own posted recv and it works, which
                    # already kills the "cannot satisfy a small recv from a big
                    # buffer" form. Only the missed-event form survives. The virtue
                    # here is that it is FALSIFIABLE in one launch and costs no RE:
                    # an identical stall kills it outright.
                    #
                    # This applies to EVERY 3:0 reply, deliberately -- u/account
                    # and Tetra Master's fetches then double as controls. If they
                    # keep working, splitting is harmless; if they break, the
                    # split itself is bad and the PS2 result means nothing.
                    split_ms = int(os.environ.get("POL_LOBBY_SPLIT", "0") or 0)
                    if split_ms > 0 and len(out) > 24:
                        # Without NODELAY the two writes can coalesce into one
                        # segment and the experiment silently tests nothing.
                        try:
                            conn.setsockopt(socket.IPPROTO_TCP,
                                            socket.TCP_NODELAY, 1)
                        except OSError:
                            pass
                        conn.sendall(out[:24])
                        log("lobby", f"{peer}   SPLIT (POL_LOBBY_SPLIT): 24B header, "
                                     f"{split_ms}ms pause, then {len(out) - 24}B")
                        time.sleep(split_ms / 1000.0)
                        pacing._lobby_ps2_send(conn, out[24:], pacing._lobby_pace_ps2(variant, peer),
                                        peer)
                    else:
                        # PS2 consoles get the reply metered so PCSX2's DEV9 TCP
                        # emulation does not choke on a 51 KB room list and reset
                        # (see _lobby_ps2_send). PC clients take the single sendall.
                        pacing._lobby_ps2_send(conn, out, pacing._lobby_pace_ps2(variant, peer), peer)

        # Phase 2/3 (legacy `full`): read one request, then reply.
        req = framing._read_frame(conn, idle=1.0, maxwait=follow, minlen=1,
                          until=framing._lobby_until(addr[0])) \
            if emit == "full" else b""
        if req:
            rcap = save_capture(f"lobby-{port}-req", req)
            log("lobby", f"{peer} Phase2: +{len(req)}B request; saved {rcap}\n"
                         + hexdump(req))

        # Phase 3 (full only): reply to the request with the world-address message.
        # SPECULATIVE body per RE of the reply decoder (polcore 0x037df0e8): world
        # IP = dword at message +0x14, gated by tag byte +1==0.
        # Override wholesale with POL_LOBBY_REPLY=<hex> while iterating (route B).
        if emit == "full" and req:
            world_ip = os.environ.get("POL_WORLD_IP") or authcap._self_ip()
            world_port = int(os.environ.get("POL_WORLD_PORT", "51330"))
            tmpl = os.environ.get("POL_LOBBY_TEMPLATE")
            if tmpl:
                # Template-replay: send a REAL SE 652B accept verbatim, but swap SE's
                # session handle (@0x24) for THIS client's handle (from its request
                # @0x0c) so the client's handle-echo check passes. The SE reply is
                # mostly session tokens the client reads (it parsed our unsigned reply
                # w/o a crypto reject), so replaying its structure may clear state 1.
                frame = bytearray(open(tmpl, "rb").read())
                cli_handle = req[0x0c:0x18] if len(req) >= 0x18 else b"\x00" * 12
                if len(frame) >= 0x30:
                    frame[0x24:0x30] = cli_handle            # echo the client's handle
                # State 4 (0x37df690) keeps reading 24-byte records after the template
                # and blocks when the frame runs out. Append N more records (each a copy
                # of the template's first body record @0x18, handle-bearing) so the
                # continuous reader can advance past state 4.
                appn = int(os.environ.get("POL_LOBBY_APPEND", "0"))
                if appn > 0 and len(frame) >= 0x30:
                    frame += bytes(frame[0x18:0x30]) * appn  # +appn * 24B records
                conn.sendall(bytes(frame))
                log("lobby", f"{peer} Phase3: SENT TEMPLATE reply {len(frame)}B "
                             f"(SE capture {os.path.basename(tmpl)}, handle@0x24="
                             f"{cli_handle.hex()}, +{appn} records)\n"
                             + hexdump(bytes(frame), 96))
            else:
                body = lobbyreply._build_world_reply_body(world_ip, world_port)
                f14 = None
                if os.environ.get("POL_LOBBY_ADDR14", "1") == "1":
                    f14 = socket.inet_aton(world_ip)         # 4-byte IP at frame 0x14
                frame = framing._build_lobby_frame(0x00, body, field14=f14)
                conn.sendall(frame)
                log("lobby", f"{peer} Phase3: SENT world reply {len(frame)}B "
                             f"(0x14={'world IP '+world_ip if f14 else 'unix time'}, "
                             f"body {len(body)}B, world={world_ip}:{world_port})\n"
                             + hexdump(frame))
            # Phase 4: watch for the client's reaction ON THIS SOCKET (a new lobby
            # frame / char-select). A WORLD dial shows as a fresh connection in the
            # world log (port 51330), not here.
            react = framing._read_frame(conn, idle=1.0, maxwait=follow, minlen=1,
                                until=framing._lobby_until(addr[0]))
            if react:
                save_capture(f"lobby-{port}-react", react)
                log("lobby", f"{peer} Phase4: client reacted +{len(react)}B on lobby "
                             f"socket (advanced state!)\n" + hexdump(react))
            else:
                log("lobby", f"{peer} Phase4: no further lobby data -- check the WORLD "
                             f"log for a dial to {world_ip}:{world_port} (success) or "
                             "POL-0008 (reply not accepted; iterate POL_LOBBY_REPLY)")
        # Optional: HOLD the socket open (send nothing) so the client stays parked
        # at the lobby with its cipher context (K@ctx+0x2b8, IV, decrypted buffers)
        # resident -- the window to Task-Manager-dump pol.exe and read the lobby
        # key/IV with work/pc/dumpread.py. Same trick that cracked auth (see
        # handle_authcap). 0 = off (default): close normally after the follow read.
        hold = int(os.environ.get("POL_LOBBY_HOLD", "0"))
        if hold > 0:
            log("lobby", f"{peer} >>> HOLDING socket open {hold}s -- client is parked "
                         "at the lobby; DUMP pol.exe NOW, then read K/IV with "
                         "dumpread.py. <<<")
            conn.settimeout(hold)
            try:
                extra = conn.recv(4096)
                if extra:
                    save_capture(f"lobby-{port}-hold", extra)
                    log("lobby", f"{peer} +{len(extra)}B during hold\n" + hexdump(extra))
            except socket.timeout:
                log("lobby", f"{peer} hold window elapsed; closing")
    except Exception as e:
        log("lobby", f"{peer} port {port} error: {e}")
    finally:
        conn.close()
