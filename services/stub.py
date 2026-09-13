"""PlayOnline observation stub -- DNS + HTTP/HTTPS + TCP loggers.

One script, three modes, chosen by argv[1]: `dns`, `http`, or `tcp`. All three
read /config/server.yaml and write structured logs to stdout (so `docker compose
logs` shows everything live) and to /logs, so a run leaves a durable capture.

The job of this stub is not to *serve* the client -- not yet. It is to record
exactly what a real PlayOnline Viewer asks for the moment it is pointed here:
which hostnames it resolves, which URLs it fetches, and which TCP ports it
dials. That capture is the input to every later step (a working patch server, a
login server), which is why every mode leans on logging over responding.

Usage (inside the container):
    python stub.py dns
    python stub.py http
    python stub.py tcp
"""
import datetime
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import time
import ssl
import struct
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    import pmlfallback      # built-in pages for a www/ with no portal capture
except ImportError:  # pragma: no cover
    pmlfallback = None

CONFIG_PATH = os.environ.get("POL_CONFIG", "/config/server.yaml")
LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")
WWW_DIR = os.environ.get("POL_WWW_DIR", "/www")

#: HTTP content-mirror settings, populated by run_http() from config `content:`.
#: When mirror is on, a request for a listed host that we have no local file for
#: is forwarded to the REAL SE host and the response archived under /www, so a
#: single client browse builds an offline copy of the portal. See _proxy_archive.
HTTP_MIRROR = {"enabled": False, "hosts": set(), "upstream_dns": "1.1.1.1",
               "pins": {}, "region": None, "region_rules": []}

#: Hop-by-hop headers that must not be copied across a proxy (RFC 7230 6.1).
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers", "trailer",
               "transfer-encoding", "upgrade"}


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        if yaml is not None:
            return yaml.safe_load(f)
        raise SystemExit("pyyaml is required; add it to the image")


def advertise_ip(cfg, default="127.0.0.1"):
    """The address to hand clients: POL_ADVERTISE, else the config's stub_ip.

    THE ENV MUST WIN. `config/server.yaml` is shared between the dev box and
    prod, and its `stub_ip` is the DEV machine's LAN address -- so on prod the
    environment is the only place this can be right, which is why
    docker-compose.prod.yml documents POL_ADVERTISE as "the address the CLIENT
    dials". `responders.py` has had this precedence since the prod stack was
    built; **this module never got it**, and it is this module that runs the
    `dns` and `tcp` services.

    What that cost, found live 2026-08-16: prod's resolver answered every
    pol.com name with 127.0.0.1 -- a private address on somebody else's LAN.
    Invisible on the PC (Install-PolConnect writes a hosts file and never asks
    us) and fatal on a PS2, whose only redirect mechanism is DNS.

    Keep the two in step. If a third module ever hands the client an address,
    it reads this, not `cfg["stub_ip"]`.
    """
    return os.environ.get("POL_ADVERTISE") or cfg.get("stub_ip") or default


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


_log_locks = {}
_log_files = {}                 # channel -> open handle

#: THE ROTATION LIVED IN THE WRONG MODULE FOR FIVE DAYS. `responders.py` grew a
#: cap and a generation count on 2026-08-12, prompted by a measurement of the live
#: deployment: 769 MB under /logs, "of which tcp.log alone was 405 MB". But `tcp`,
#: `dns` and `http` are served by THIS module, which has its own `log()` -- so the
#: three highest-volume channels, and the one the measurement actually named, were
#: never covered. Re-measured 2026-08-17: tcp.log 405 MB, still one
#: server-lifetime append, on the same disk as accounts.db.
#:
#: Same two knobs and the same semantics as responders.py, deliberately -- an
#: operator sets POL_LOG_MAX_MB once and both modules obey it. 0 appends forever.
_LOG_MAX = int(float(os.environ.get("POL_LOG_MAX_MB", "8")) * 1024 * 1024)
_LOG_KEEP = int(os.environ.get("POL_LOG_KEEP", "3"))


def _log_handle_locked(channel):
    """The open handle for `channel`, rotated first if it is at the cap.

    Caller holds that channel's lock. The handle is kept OPEN between lines:
    this used to open, append and close once per line, and the `tcp` channel
    logs several lines per packet.
    """
    path = os.path.join(LOG_DIR, f"{channel}.log")
    f = _log_files.get(channel)
    # SIZE IS CHECKED ON THE FIRST WRITE TOO, not just on an already-open handle.
    # A process that starts with an oversized log inherited from the last run
    # would otherwise have to grow it by another whole cap before rotating --
    # which is precisely the case here, where every existing log is already past
    # it. `os.path.getsize` covers the not-yet-opened case; `f.tell()` the rest.
    try:
        size = f.tell() if f is not None else os.path.getsize(path)
    except OSError:
        size = 0
    if _LOG_MAX > 0 and size >= _LOG_MAX:
        if f is not None:
            f.close()
            _log_files.pop(channel, None)
            f = None
        for i in range(_LOG_KEEP, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            dst = f"{path}.{i}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    break
    if f is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        f = open(path, "a", encoding="utf-8")
        _log_files[channel] = f
    return f


def log(channel, msg):
    """Print to stdout and append to /logs/<channel>.log, rotating at the cap."""
    line = f"{_stamp()} [{channel}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    lock = _log_locks.setdefault(channel, threading.Lock())
    with lock:
        try:
            f = _log_handle_locked(channel)
            f.write(line + "\n")
            f.flush()
        except OSError:
            # A LOG LINE MUST NEVER TAKE DOWN THE REQUEST THAT WROTE IT --
            # responders.py's rule, and it applies here for the same reason:
            # this module logs inside the DNS and TCP hot paths.
            _log_files.pop(channel, None)


def hexdump(data, limit=2048):
    out = []
    for i in range(0, min(len(data), limit), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {i:08x}  {hexs:<47}  {text}")
    if len(data) > limit:
        out.append(f"    ... ({len(data) - limit} more bytes)")
    return "\n".join(out)


#: Total bytes of captures to keep. Measured 2026-08-17: logs/captures held
#: 312 MB and nothing had ever deleted from it -- every conversation on every
#: bound port, for the life of the deployment. Captures are the most valuable
#: thing this server writes and the most numerous, so the eviction is
#: OLDEST-FIRST by mtime rather than a per-file cap: a big capture that just
#: landed is exactly the one somebody is about to read.
#: POL_CAPTURE_MAX_MB=0 keeps everything, which is the right setting for a
#: deliberate capture session and the wrong one for a server left running.
_CAPTURE_MAX = int(float(os.environ.get("POL_CAPTURE_MAX_MB", "256")) * 1024 * 1024)
_capture_lock = threading.Lock()


def _prune_captures(d):
    """Evict oldest-first until the directory is back under the cap."""
    if _CAPTURE_MAX <= 0:
        return
    try:
        entries = []
        total = 0
        with os.scandir(d) as it:
            for e in it:
                if not e.is_file():
                    continue
                st = e.stat()
                entries.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
    except OSError:
        return
    if total <= _CAPTURE_MAX:
        return
    entries.sort()                      # oldest mtime first
    freed = dropped = 0
    for _mtime, size, path in entries:
        if total - freed <= _CAPTURE_MAX:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        freed += size
        dropped += 1
    if dropped:
        log("capture", f"pruned {dropped} capture(s), {freed // 1024} KiB, to stay "
                       f"under {_CAPTURE_MAX / (1024 * 1024):.4g} MiB "
                       f"(POL_CAPTURE_MAX_MB); "
                       f"{len(entries) - dropped} kept")


def save_capture(name, data):
    d = os.path.join(LOG_DIR, "captures")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as f:
        f.write(data)
    # After the write, never before: the file that just landed must survive its
    # own prune, and it is the newest so oldest-first eviction leaves it alone.
    with _capture_lock:
        _prune_captures(d)
    return path


# --------------------------------------------------------------------------- #
# DNS mode -- answer configured zones with stub_ip, forward the rest upstream
# --------------------------------------------------------------------------- #
def dns_parse_question(pkt):
    """Return (qname, qtype) from a DNS query packet, or (None, None)."""
    try:
        i = 12
        labels = []
        while True:
            n = pkt[i]
            if n == 0:
                i += 1
                break
            labels.append(pkt[i + 1:i + 1 + n].decode("latin-1"))
            i += 1 + n
        qtype, _qclass = struct.unpack_from(">HH", pkt, i)
        return ".".join(labels).lower(), qtype
    except Exception:
        return None, None


def dns_build_answer(query, ip):
    """A-record answer echoing the question, TTL 60."""
    tid = query[:2]
    flags = b"\x81\x80"  # response, recursion available
    header = tid + flags + b"\x00\x01\x00\x01\x00\x00\x00\x00"
    # question section starts at byte 12; find its end (null label + 4 bytes)
    i = 12
    while query[i] != 0:
        i += 1 + query[i]
    qend = i + 1 + 4
    question = query[12:qend]
    answer = (b"\xc0\x0c"           # pointer to qname
              + b"\x00\x01\x00\x01"  # type A, class IN
              + struct.pack(">I", 60)
              + b"\x00\x04"
              + socket.inet_aton(ip))
    return header + question + answer


def run_dns():
    cfg = load_config()
    ip = advertise_ip(cfg)
    # PER-CLIENT-NETWORK ANSWER. `ip` is a single global address (POL_ADVERTISE),
    # which on prod is the Tailscale address 127.0.0.1. A PC Viewer riding the
    # tailnet reaches that fine, but a console behind PCSX2 egresses from the
    # host's LAN NIC and CANNOT route to a tailnet /32 -- the patch connect to
    # 127.0.0.1:54000 times out and the Viewer raises POL-1161 ("failed to
    # connect to the patch server"). The same box IS reachable at its LAN IP, so
    # hand LAN-sourced queries that address instead. POL_ADVERTISE_LAN (an IP)
    # arms this; POL_ADVERTISE_LAN_CLIENTS (CIDR list) sets which sources qualify,
    # defaulting to the RFC1918 ranges. Absent POL_ADVERTISE_LAN the behaviour is
    # unchanged: every client gets `ip`.
    lan_ip = os.environ.get("POL_ADVERTISE_LAN") or cfg.get("dns", {}).get("lan_ip")
    lan_spec = os.environ.get("POL_ADVERTISE_LAN_CLIENTS") or ",".join(
        cfg.get("dns", {}).get("lan_clients", [])) or \
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"  # generic RFC1918 ACL; polcheck: allow
    lan_nets = []
    if lan_ip:
        for c in [x.strip() for x in lan_spec.split(",") if x.strip()]:
            try:
                lan_nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                log("dns", f"ignoring unparseable lan-client range {c!r}")

    def answer_ip(client_ip):
        """The A-record address for this client: the LAN IP for a LAN-sourced
        query when POL_ADVERTISE_LAN is set, else the global advertise IP."""
        if lan_ip and lan_nets:
            try:
                a = ipaddress.ip_address(client_ip)
            except ValueError:
                return ip
            if any(a in n for n in lan_nets):
                return lan_ip
        return ip

    zones = [z.lower() for z in cfg.get("dns", {}).get("zones", [])]
    # When set, ONLY these exact hosts (or their subdomains) are redirected to
    # the stub; every other name -- including the rest of the redirect zones --
    # is forwarded upstream to its real IP. This lets the client reach SE's real
    # portal/content while we still intercept the login-critical hosts.
    redirect_only = [h.lower() for h in cfg.get("dns", {}).get("redirect_only", [])]
    upstream = cfg.get("dns", {}).get("upstream", "1.1.1.1")
    # WHO MAY ASK. This service forwards any name it does not redirect and
    # answers whoever asked, which on a LAN is the point and on a public
    # interface is a reflector -- an open resolver, published to the host as
    # 53:53/udp. `dns.clients` (config) or POL_DNS_CLIENTS is a list of CIDRs;
    # the default covers loopback plus the RFC1918 ranges a console or a second
    # PC will be on. Set it to "0.0.0.0/0" to restore the old behaviour.
    allow = os.environ.get("POL_DNS_CLIENTS") or ",".join(
        cfg.get("dns", {}).get("clients", [])) or \
        "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"  # generic RFC1918 ACL; polcheck: allow
    allow_nets = []
    for c in [x.strip() for x in allow.split(",") if x.strip()]:
        try:
            allow_nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            log("dns", f"ignoring unparseable client range {c!r}")

    def allowed(client_ip):
        if not allow_nets:
            return True
        try:
            a = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(a in n for n in allow_nets)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bind_ip = os.environ.get("POL_DNS_BIND", "0.0.0.0")
    sock.bind((bind_ip, 53))
    refused = {}                 # client -> count, so the log says it once

    # STALL INSTRUMENTATION (POL-0008 hunt) ---------------------------------
    # This loop is single-threaded and the upstream forward below is a BLOCKING
    # sendto/recvfrom with a 3s timeout. While it waits, nothing else on udp/53
    # is answered -- including the Viewer's pol.com lookups, which it re-issues
    # constantly (a fresh gethostbyname per connection). A pol.com query that
    # lands inside that window is answered late or not at all; the Viewer's
    # resolver gives up, no socket is ever opened, and that is exactly
    # POL-0008 (internal text: "cannot reach the network", NOT a dropped
    # session). The client machine also emits ordinary non-pol traffic
    # (msftconnecttest, *.local, ...), so the stalls are unrelated to POL and
    # land at random -- which is why the drops feel periodic.
    #
    # `stall_ms` = the previous iteration's blocking time. It is attached to
    # the NEXT query's log line, so a delayed lookup is self-identifying:
    #   A ci000.pol.com -> ... (redirected) [DELAYED 2951ms behind ...]
    # Set POL_DNS_ASYNC=1 to hand forwards to worker threads instead, which
    # removes the stall entirely -- that is the fix, not just the measurement.
    stall_warn = float(os.environ.get("POL_DNS_STALL_MS", "150"))
    async_fwd = os.environ.get("POL_DNS_ASYNC", "0") == "1"
    fwd_slots = threading.Semaphore(int(os.environ.get("POL_DNS_WORKERS", "32")))
    stall = {"ms": 0.0, "name": ""}

    def note_stall(t0, qname):
        ms = (time.monotonic() - t0) * 1000.0
        if ms >= stall_warn:
            stall["ms"], stall["name"] = ms, qname or "?"
            log("dns", f"STALL {ms:.0f}ms blocking udp/53 on upstream "
                       f"{upstream} for {qname or '?'} -- any query that "
                       f"arrived in this window was answered late")

    def take_stall():
        if stall["ms"]:
            tag = (f"  [DELAYED {stall['ms']:.0f}ms behind forward of "
                   f"{stall['name']}]")
            stall["ms"], stall["name"] = 0.0, ""
            return tag
        return ""

    def do_forward(data, addr, qname, qt):
        """Resolve one name upstream and relay the reply to `addr`."""
        fwd = None
        try:
            fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            fwd.settimeout(3)
            fwd.sendto(data, (upstream, 53))
            # Take the answer from the UPSTREAM, not from whoever answers first.
            # This socket is connectionless: anything that guesses the ephemeral
            # port can hand us a reply, and we would relay it to the client as
            # ours. Cheap to check, and there is exactly one address it may be.
            while True:
                reply, src = fwd.recvfrom(4096)
                if src[0] == upstream:
                    break
                log("dns", f"discarding a reply for {qname} from {src[0]} "
                           f"(upstream is {upstream})")
            # ...and the reply must answer the question we asked: the transaction
            # id is the only thing tying them together.
            if len(reply) >= 2 and len(data) >= 2 and reply[:2] != data[:2]:
                log("dns", f"discarding a reply for {qname} with the wrong "
                           f"transaction id")
                return
            sock.sendto(reply, addr)
            if qname:
                log("dns", f"{addr[0]} {qt} {qname} -> forwarded")
        except Exception as e:
            log("dns", f"forward error for {qname}: {e}")
        finally:
            if fwd is not None:
                fwd.close()

    log("dns", f"listening on udp/53; zones {zones} -> {ip}"
                f"{f' (LAN {lan_ip} for {lan_spec})' if lan_ip and lan_nets else ''}; "
                f"upstream {upstream}; forward="
                f"{'async' if async_fwd else 'blocking'}, "
                f"stall warn >={stall_warn:.0f}ms")

    def matches(name):
        if redirect_only:
            return any(name == h or name.endswith("." + h) for h in redirect_only)
        return any(name == z or name.endswith("." + z) for z in zones)

    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except Exception as e:  # pragma: no cover
            log("dns", f"recv error: {e}")
            continue
        if not allowed(addr[0]):
            n = refused.get(addr[0], 0) + 1
            refused[addr[0]] = n
            if n == 1:
                log("dns", f"REFUSING queries from {addr[0]} -- outside "
                           f"dns.clients ({allow}). Further ones stay silent.")
            continue
        qname, qtype = dns_parse_question(data)
        qt = {1: "A", 28: "AAAA", 5: "CNAME", 15: "MX"}.get(qtype, str(qtype))
        if qname and matches(qname):
            # A redirected name is a POL-critical lookup. If the previous
            # iteration blocked, say so on THIS line -- that is the direct
            # evidence linking a stall to a late pol.com answer.
            late = take_stall()
            # Answer A queries with stub_ip. For AAAA, answer empty so the
            # client falls back to the A record instead of a real IPv6 host.
            if qtype == 1:
                try:
                    rip = answer_ip(addr[0])
                    sock.sendto(dns_build_answer(data, rip), addr)
                    log("dns", f"{addr[0]} {qt} {qname} -> {rip} (redirected)"
                               f"{late}")
                except Exception as e:
                    log("dns", f"answer error for {qname}: {e}")
            else:
                # NOERROR/empty
                resp = data[:2] + b"\x81\x80" + data[4:6] + b"\x00\x00" \
                    + b"\x00\x00\x00\x00" + data[12:]
                sock.sendto(resp, addr)
                log("dns", f"{addr[0]} {qt} {qname} -> (empty; redirected zone)"
                           f"{late}")
        elif async_fwd:
            # Forward off-loop so a slow upstream can never hold up udp/53.
            # Bounded by POL_DNS_WORKERS; if every slot is busy we fall back to
            # forwarding inline rather than dropping the query on the floor.
            if fwd_slots.acquire(blocking=False):
                def _run(data=data, addr=addr, qname=qname, qt=qt):
                    try:
                        do_forward(data, addr, qname, qt)
                    finally:
                        fwd_slots.release()
                threading.Thread(target=_run, daemon=True).start()
            else:
                t0 = time.monotonic()
                do_forward(data, addr, qname, qt)
                note_stall(t0, qname)
        else:
            # forward verbatim to upstream and relay the reply
            t0 = time.monotonic()
            do_forward(data, addr, qname, qt)
            note_stall(t0, qname)


# --------------------------------------------------------------------------- #
# HTTP/HTTPS mode -- serve from /www/<host>/<path> if present, else 404; log all
# --------------------------------------------------------------------------- #
class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PolStub/0.1"

    def log_message(self, *a):  # silence default stderr logging
        pass

    def _scheme(self):
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"

    def _handle(self):
        host = self.headers.get("Host", "?")
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        hdrs = "".join(f"      {k}: {v}\n" for k, v in self.headers.items())
        chan = "http"
        log(chan, f"{self._scheme()}://{host}{self.path}  "
                  f"{self.command} from {self.client_address[0]}")
        detail = (f"  {self.command} {self.path} {self.request_version}\n"
                  f"    Host: {host}\n{hdrs}")
        if body:
            cap = save_capture(
                f"http-{_stamp().replace(':', '').replace('.', '')}.body", body)
            detail += f"    [body {len(body)} bytes -> {cap}]\n"
            detail += hexdump(body, 512) + "\n"
        # append the verbose record to a per-channel detail log
        with open(os.path.join(LOG_DIR, "http-detail.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{_stamp()} {self._scheme()}://{host}\n{detail}\n")

        # 0) the account servlet. SE's game pages link to
        #    https://userctl.pol.com/pml-cgi-bin/UMENZ001.cgi?kinou_id=NN&...
        #    -- the Content ID and registration-code operations. Those hosts are
        #    in dns.redirect_only, so the clicks already arrive here; without
        #    this they fell through to an empty 404 and the client reported
        #    "Cannot confirm acquisition of Content ID". Handled by ucscgi,
        #    which already dispatches on kinou_id for the sign-up wizard.
        if self._try_ucs_cgi(body):
            return
        # 1) serve a local mirror if we already have this URL under /www
        if self._try_serve(host):
            return
        # 2) otherwise, if mirroring is on for this host, fetch it from the real
        #    SE server, hand it back to the client, and archive it under /www.
        if (HTTP_MIRROR["enabled"]
                and host.split(":", 1)[0].lower() in HTTP_MIRROR["hosts"]):
            if self._proxy_archive(host, body):
                return
        # 3) no file for a .pml path: synthesize the built-in page for it, so a
        #    server with no portal capture (an empty www/) still shows a menu, a
        #    title page and a Play button. A file found in step 1 always wins.
        #    POL_PML_FALLBACK=0 turns this off and step 4 applies instead.
        bare_path = self.path.split("?", 1)[0]
        if (pmlfallback is not None and bare_path.lower().endswith(".pml")
                and os.environ.get("POL_PML_FALLBACK", "1") == "1"):
            page = pmlfallback.fallback_page(host.split(":", 1)[0], bare_path)
            if page:
                self.send_response(200)
                self.send_header("Content-Type", "text/x-playonline-pml")
                self.send_header("Content-Length", str(len(page)))
                # No validators on purpose: nothing on disk to derive them
                # from, and a page dropped under www/ should show on the very
                # next render rather than after a cached lifetime.
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(page)
                log("http", f"[http] fallback page for {bare_path}")
                return
        # 4) nothing to serve. For a PML page request, answer with an EMPTY PML
        #    document rather than 404: `<!-- -->` is exactly what the shipped
        #    blank page contains (PS2 `pml/etc/none.pml`, which is plaintext --
        #    only the PC on-disk copies are encrypted, and server-delivered PML
        #    never is). A 404 makes the Viewer sit on its loading spinner; a valid
        #    empty page lets it finish the load and move on, and we still log the
        #    exact URL so we know what to author next.
        wants_pml = ("x-playonline-pml" in self.headers.get("Accept", "")
                     or self.path.split("?", 1)[0].lower().endswith(".pml"))
        if wants_pml and os.environ.get("POL_HTTP_EMPTY_PML", "1") == "1":
            body_out = b"<!-- -->\r\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/x-playonline-pml")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            log("http", f"  served EMPTY PML for {self.path} "
                        "(no local file; set POL_HTTP_EMPTY_PML=0 to 404)")
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _try_ucs_cgi(self, body):
        """Relay /pml-cgi-bin/ to the ucscgi service. True if handled.

        A plain relay rather than an import: ucscgi owns its own account-database
        session state, and running a second copy of it inside this process would
        give the two listeners separate in-memory flows -- a user could start a
        registration on 51305 and have the follow-up POST land here with no
        session. One process, reached two ways.
        """
        if not self.path.startswith("/pml-cgi-bin/"):
            return False
        target = os.environ.get("POL_UCS_CGI_HOST", "ucs-plain")
        port = int(os.environ.get("POL_UCS_CGI_PORT", "8080"))
        req = f"{self.command} {self.path} HTTP/1.1\r\n"
        skip = ("connection", "keep-alive", "transfer-encoding")
        for k, v in self.headers.items():
            if k.lower() in skip:
                continue
            req += f"{k}: {v}\r\n"
        req += "Connection: close\r\n\r\n"
        try:
            with socket.create_connection((target, port), timeout=15) as up:
                up.sendall(req.encode("latin-1", "replace") + body)
                chunks = []
                while True:
                    d = up.recv(65536)
                    if not d:
                        break
                    chunks.append(d)
        except OSError as exc:
            log("http", f"  ucs-cgi relay to {target}:{port} FAILED: {exc}")
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        raw = b"".join(chunks)
        log("http", f"  ucs-cgi {self.path.split('?')[0]} -> {target}:{port} "
                    f"({len(raw)} bytes)")
        # Pass the upstream response through verbatim; it is already a complete
        # HTTP message and ucscgi sets its own Content-Type/Length.
        self.wfile.write(raw)
        self.close_connection = True
        return True

    # -- region partitioning ------------------------------------------------- #
    def _region(self):
        """Which regional tree (jp/us/eu) this request archives under, or None.

        A PlayOnline client install is region-locked, so the reliable signal is
        the session-wide `content.region` (set to match the client being
        driven). `content.region_rules` lets us additionally auto-split within a
        single run *once a capture reveals how region rides in the request* --
        each rule matches a regex against the path, a header, or the Host and
        maps to a region. Until we know the discriminator the rules list is
        empty and the session tag governs. Returns None for a flat www/ layout.
        """
        for rule in HTTP_MIRROR["region_rules"]:
            src = (rule.get("in") or "path").lower()
            if src == "path":
                hay = self.path
            elif src == "host":
                hay = self.headers.get("Host", "")
            elif src == "header":
                hay = self.headers.get(rule.get("header", ""), "")
            else:
                hay = ""
            pat = rule.get("match", "")
            if pat and re.search(pat, hay, re.IGNORECASE):
                return (rule.get("region") or "").lower() or None
        return HTTP_MIRROR["region"]

    # -- local mirror serving ------------------------------------------------ #
    def _archive_key(self, host_only, region=False):
        """Filesystem path (relative to /www) for this request's URL.

        Encodes the query string into the name so that `page?a=1` and `page?a=2`
        archive to distinct files, and the same client request reconstructs the
        same key when serving offline. Directory URLs get an index file. When a
        region is in force the tree is partitioned `www/<region>/<host>/...` so
        the same URL fetched by the JP, US and EU clients does not collide.
        `region` defaults to False meaning "compute it"; pass None to force flat.
        """
        if region is False:
            region = self._region()
        path, _, query = self.path.partition("?")
        rel = path.lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        if query:
            root, ext = os.path.splitext(rel)
            digest = hashlib.md5(query.encode("utf-8", "replace")).hexdigest()[:8]
            rel = f"{root}.q-{digest}{ext}"
        parts = ([region] if region else []) + [host_only, rel.replace("\\", "/")]
        return os.path.join(*parts)

    def _safe_under(self, base, rel):
        candidate = os.path.normpath(os.path.join(base, rel))
        return candidate if candidate.startswith(os.path.normpath(base)) else None

    def _try_serve(self, host):
        host_only = host.split(":", 1)[0]
        region = self._region()
        keys = [self._archive_key(host_only, region)]      # query-specific first
        bare = self.path.split("?", 1)[0].lstrip("/")      # then the bare path
        rp = ([region] if region else [])
        keys += [os.path.join(*(rp + [host_only, bare])),
                 os.path.join(host_only, bare), bare]       # region-flat fallback
        for rel in keys:
            candidate = self._safe_under(WWW_DIR, rel)
            if candidate and os.path.isfile(candidate):
                data = open(candidate, "rb").read()
                meta = self._read_meta(candidate)
                status = int(meta.get("status", 200))
                validators = self._validators(candidate, meta)
                # Conditional requests: the Viewer keeps an on-disk page cache
                # and revalidates with If-Modified-Since/If-None-Match (SE's
                # Apache answered a warm browse mostly with 304s -- see
                # _strip_conditional). The band handler (responders.py
                # _portal_not_modified) already honours these; this is the same
                # contract for the :80/:443 door. Only a stored 200 may 304 --
                # a mirrored 404/302 replays as itself.
                if status == 200 and validators and self._not_modified(validators):
                    self.send_response(304)
                    self.send_header("Last-Modified", validators[0])
                    self.send_header("ETag", validators[1])
                    self.end_headers()
                    log("http", f"  304 {candidate} (client cache is current)")
                    return True
                self.send_response(status)
                sent = set()
                for k, v in meta.get("headers", {}).items():
                    if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length":
                        self.send_header(k, v)
                        sent.add(k.lower())
                if validators and status == 200:
                    if "last-modified" not in sent:
                        self.send_header("Last-Modified", validators[0])
                    if "etag" not in sent:
                        self.send_header("ETag", validators[1])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                log("http", f"  served {candidate} ({len(data)} bytes)")
                return True
        return False

    def _validators(self, candidate, meta):
        """(Last-Modified, ETag) this response advertises, or None.

        A mirrored file's .polmeta keeps SE's original values and the replay
        loop above sends them verbatim, so the client's cached validator IS the
        meta one -- compare against that, not the local mtime. Hand-authored
        files (no meta) get mtime+size, same derivation as the band handler's
        _portal_validators, so an edit under www/ invalidates immediately."""
        mh = {k.lower(): v for k, v in meta.get("headers", {}).items()}
        lm, etag = mh.get("last-modified"), mh.get("etag")
        if not (lm and etag):
            try:
                st = os.stat(candidate)
            except OSError:
                return None
            lm = lm or time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                     time.gmtime(st.st_mtime))
            etag = etag or '"%x-%x"' % (int(st.st_mtime), st.st_size)
        return lm, etag

    def _not_modified(self, validators):
        """True when the request's conditional headers match `validators`.

        ETag comparison wins when If-None-Match is present (RFC 7232 6);
        Last-Modified is exact string equality -- the client echoes our header
        back verbatim, and parsing legacy date formats buys nothing here."""
        lm, etag = validators
        inm = self.headers.get("If-None-Match") or ""
        if inm.strip():
            return (inm.strip() == "*"
                    or etag in [t.strip() for t in inm.split(",")])
        ims = (self.headers.get("If-Modified-Since") or "").strip()
        return bool(ims) and ims == lm

    @staticmethod
    def _read_meta(candidate):
        try:
            with open(candidate + ".polmeta", "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -- mirror capture (forward to real SE, archive the reply) -------------- #
    def _proxy_archive(self, host, body):
        host_only = host.split(":", 1)[0]
        https = self._scheme() == "https"
        port = 443 if https else 80
        pins = HTTP_MIRROR["pins"]
        try:
            ip = pins.get(host_only) or resolve_a(
                host_only, HTTP_MIRROR["upstream_dns"])
        except Exception as e:
            log("http", f"  mirror resolve failed for {host_only}: {e}")
            return False

        # forward the client's own request; strip hop-by-hop + encoding so what
        # we archive is the decoded body, and force Host to the real name.
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP
                       and k.lower() not in ("accept-encoding", "host")}
        fwd_headers["Host"] = host_only
        fwd_headers["Accept-Encoding"] = "identity"
        try:
            raw = socket.create_connection((ip, port), timeout=20)
            if https:
                # SE's legacy certs may not validate; we only need the bytes.
                sslctx = ssl._create_unverified_context()
                sock = sslctx.wrap_socket(raw, server_hostname=host_only)
            else:
                sock = raw
            conn = http.client.HTTPConnection(host_only, port, timeout=20)
            conn.sock = sock                       # connect() is skipped when set
            conn.request(self.command, self.path, body or None, fwd_headers)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = resp.getheaders()
            status, reason = resp.status, resp.reason
            conn.close()
        except Exception as e:
            log("http", f"  mirror fetch failed {host_only}{self.path}: {e}")
            return False

        # archive the response (body + metadata sidecar) under /www
        region = self._region()
        rel = self._archive_key(host_only, region)
        dest = self._safe_under(WWW_DIR, rel)
        if dest:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            meta = {"url": f"{self._scheme()}://{host_only}{self.path}",
                    "region": region, "method": self.command,
                    "status": status, "reason": reason,
                    "fetched": _stamp(), "source_ip": ip,
                    "headers": {k: v for k, v in resp_headers}}
            with open(dest + ".polmeta", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            log("http", f"  MIRRORED [{region or '-'}] {host_only}{self.path} "
                        f"-> {dest} ({len(data)} bytes, {status} {reason})")

        # relay the response back to the client
        self.send_response(status)
        for k, v in resp_headers:
            if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return True

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _handle


def _make_cert():
    """Self-signed wildcard cert for *.pol.com et al., generated once."""
    cert = os.path.join(LOG_DIR, "stub-cert.pem")
    key = os.path.join(LOG_DIR, "stub-key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    os.makedirs(LOG_DIR, exist_ok=True)
    san = "DNS:*.pol.com,DNS:pol.com,DNS:*.playonline.com,DNS:*.sqex.net"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "3650",
         "-subj", "/CN=*.pol.com", "-addext", f"subjectAltName={san}"],
        check=True)
    log("http", f"generated self-signed cert {cert}")
    return cert, key


def run_http():
    cfg = load_config()
    listen = cfg.get("listen", {})
    # content-mirror config: forward+archive listed hosts we have no file for.
    content = cfg.get("content", {}) or {}
    if content.get("mirror"):
        HTTP_MIRROR["enabled"] = True
        HTTP_MIRROR["hosts"] = {h.lower() for h in (content.get("hosts") or [])}
        HTTP_MIRROR["upstream_dns"] = (
            content.get("upstream_dns")
            or (cfg.get("proxy", {}) or {}).get("upstream_dns")
            or cfg.get("dns", {}).get("upstream", "1.1.1.1"))
        HTTP_MIRROR["pins"] = (cfg.get("proxy", {}) or {}).get("pins") or {}
        HTTP_MIRROR["region"] = (content.get("region") or "").lower() or None
        HTTP_MIRROR["region_rules"] = content.get("region_rules") or []
        log("http", f"content mirror ON for {sorted(HTTP_MIRROR['hosts'])} "
                    f"(upstream {HTTP_MIRROR['upstream_dns']}, "
                    f"region {HTTP_MIRROR['region'] or 'flat'}, "
                    f"{len(HTTP_MIRROR['region_rules'])} rule(s))")
    class LoggingHTTPServer(ThreadingHTTPServer):
        """Log at the CONNECTION level, not just per parsed request.

        Why: for an HTTPS listener the TLS handshake happens inside
        `get_request()` (the listening socket is already wrapped), and
        `ssl.SSLError` is a subclass of OSError -- which `serve_forever` catches
        and DISCARDS. So a client that rejects our self-signed cert produced
        absolutely nothing in the log, indistinguishable from a client that never
        connected at all. That ambiguity cost a login round; now a refused
        handshake says so."""
        scheme = "http"

        def get_request(self):
            try:
                conn, addr = super().get_request()
            except ssl.SSLError as e:
                log("http", f"TLS handshake FAILED from a client on "
                            f"{self.server_address[1]}: {e} -- the Viewer is "
                            "rejecting our self-signed cert")
                raise
            log("http", f"{self.scheme} connect from {addr[0]}:{addr[1]}")
            return conn, addr

        def handle_error(self, request, client_address):
            log("http", f"handler error from {client_address}: "
                        f"{traceback.format_exc(limit=4)}")

    threads = []
    for port in listen.get("http", [80]):
        srv = LoggingHTTPServer(("0.0.0.0", port), StubHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        threads.append(t)
        log("http", f"HTTP listening on {port}")
    https_ports = listen.get("https", [])
    if https_ports:
        cert, key = _make_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        for port in https_ports:
            srv = LoggingHTTPServer(("0.0.0.0", port), StubHandler)
            srv.scheme = "https"
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            threads.append(t)
            log("http", f"HTTPS listening on {port}")
    for t in threads:
        t.join()


# --------------------------------------------------------------------------- #
# TCP mode -- accept on each configured port, log + hexdump whatever arrives
# --------------------------------------------------------------------------- #
def _tcp_client(conn, addr, port, hold=None):
    """Log-only handler. `hold` is a HARD ceiling in seconds on the whole
    conversation, not an idle timeout.

    Why both: the 30s idle timeout below only fires on SILENCE, and a game that
    polls never goes silent. Fantasy Earth resends its 4-byte opener every 15s
    for ever, so binding 54848 with this handler was WORSE than not binding it
    -- the connect succeeded, FE waited on a reply that never came, and the only
    way out was killing the process (see the note over 54848 in
    docker-compose.yml). A `hold` closes the socket regardless, so the client
    sees a clean disconnect and reports a failure it can back out of, and we
    still get the opening bytes. Use it for every capture-only bind on a port a
    running GAME dials; leave it unset for probe ports nothing depends on.
    """
    peer = f"{addr[0]}:{addr[1]}"
    log("tcp", f"CONNECT port {port} from {peer}"
               + (f" (capture, closing after {hold}s)" if hold else ""))
    deadline = (time.monotonic() + hold) if hold else None
    conn.settimeout(30)
    total = bytearray()
    try:
        while True:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    log("tcp", f"port {port} {peer} hold expired after {hold}s "
                               f"(total {len(total)} bytes) -- closing so the "
                               f"client fails cleanly instead of waiting")
                    break
                conn.settimeout(min(30, left))
            try:
                data = conn.recv(4096)
            except socket.timeout:
                # With a hold set this may be the shortened deadline slice, not
                # 30s of real silence -- go round and let the check above decide
                # which, so the log says the true reason.
                if deadline is not None:
                    continue
                raise
            if not data:
                break
            total += data
            log("tcp", f"port {port} <- {peer} {len(data)} bytes\n"
                       + hexdump(data))
    except socket.timeout:
        log("tcp", f"port {port} {peer} idle timeout "
                   f"(total {len(total)} bytes)")
    except Exception as e:
        log("tcp", f"port {port} {peer} error: {e}")
    finally:
        if total:
            cap = save_capture(
                f"tcp-{port}-{_stamp().replace(':', '').replace('.', '')}.bin",
                bytes(total))
            log("tcp", f"port {port} {peer} saved {len(total)} bytes -> {cap}")
        conn.close()
        log("tcp", f"CLOSE port {port} {peer}")


def resolve_a(host, server="1.1.1.1"):
    """Direct A-record query to `server`, bypassing the local resolver.

    We must not use the system resolver here: the client machine's DNS points
    at our own stub, which would answer with stub_ip and loop the relay back on
    itself.  A raw query straight to a public resolver gets the *real* IP.
    """
    tid = b"\x2a\x2a"
    q = tid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for lbl in host.split("."):
        q += bytes([len(lbl)]) + lbl.encode()
    q += b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    # skip header + question, then read answers looking for a type-A record
    i = 12
    while data[i] != 0:
        i += 1 + data[i]
    i += 5  # null label + qtype + qclass
    ancount = struct.unpack_from(">H", data, 6)[0]
    for _ in range(ancount):
        # name (may be a compression pointer)
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i] != 0:
                i += 1 + data[i]
            i += 1
        rtype, _cls, _ttl, rdlen = struct.unpack_from(">HHIH", data, i)
        i += 10
        if rtype == 1 and rdlen == 4:
            return socket.inet_ntoa(data[i:i + 4])
        i += rdlen
    raise OSError(f"no A record for {host}")


# --------------------------------------------------------------------------- #
# Login redirect steering.
#
# ci000.pol.com:51240 is a *directory* service, not the auth server. It answers
# with an IRC-style greeting whose prefix carries the real node's address:
#
#     :202.67.54.124 300 * WYACFSC0BKPCO55OBM...
#
# The client then opens a second connection straight to 202.67.54.124 on a port
# named inside the token -- by raw IP, so DNS redirection cannot touch it, and
# the actual authentication happens somewhere we never see.
#
# Rewriting that prefix to stub_ip brings the follow-up connection back to us.
# The IP lives in the plaintext IRC prefix, *outside* the token, so the rewrite
# cannot invalidate the token's trailing checksum. We remember which real node
# issued the greeting and relay the follow-up to that same host, on whichever
# port the client chose -- so the port encoding never has to be decoded.
#
# One wrinkle: the client is sometimes redirected back onto 51240 itself, the
# same port the directory listens on. We cannot tell the directory connection
# from the follow-up by port alone, so `pending` is a one-shot flag -- set when
# a greeting is rewritten, consumed by the very next connection, which by the
# protocol's own shape is always the follow-up.
#
# The flag must also EXPIRE. A greeting the client never acts on would otherwise
# poison the next directory connection minutes later, silently relaying it to an
# auth node instead of the directory -- which is exactly what happened on the
# first run. The real follow-up lands within ~200 ms, so a few seconds is ample.
_LEARNED_NODE = {"ip": None, "pending_until": 0.0}
_LEARNED_LOCK = threading.Lock()

#: How long a rewritten greeting stays eligible to claim the next connection.
PENDING_TTL = 5.0

#: Matches an IRC line prefix that is a bare IPv4 address, at a line start.
_PREFIX_IP = re.compile(rb"(?m)^:(\d{1,3}(?:\.\d{1,3}){3})(?=[ \r\n])")

#: A `300` greeting line: prefix, numeric, target, then the redirect token.
# Prefix may be an IP literal (directory node) OR a hostname
# (auth nodes prefix with e.g. `pol-1043-51245.pol.com`). Either way the
# redirect address lives in the base-32 token, so accept both and let the
# strict 40+4 token pattern do the real filtering.
_GREETING = re.compile(
    rb"(?m)^:(\S+) 300 (\S+) ([0-9A-Z]{40})(\S{4})\r?$")

# The redirect token is base-32 over this alphabet, found verbatim at 0x154c58
# in the PS2 build SLPS_202.00 (the same technique that recovered the manifest
# digest alphabet at 0x196238). Its zero symbol is 'N', which is why an idle
# token shows a run of Ns: that run is the zero padding at bytes [14:20].
#
# 40 symbols x 5 bits = 25 bytes:
#
#     [0:4]   session nonce, different every connection
#     [4:8]   constant
#     [8:12]  node IPv4          <- the address the client actually dials
#     [12:14] port, big-endian   <- the port it dials
#     [14:20] zero
#     [20:22] varies
#     [22:25] constant
#
# The trailing 4 symbols are NOT in this alphabet and are assumed to be a
# checksum over the body; its algorithm is unknown, so we pass it through
# untouched and let the client tell us whether it is enforced.
TOKEN_ALPHABET = "N43OVHBJ1Y2C0WSXED5QFILRZMUTAPGK"
_TOK_VAL = {c: i for i, c in enumerate(TOKEN_ALPHABET)}


def token_decode(body):
    """40 base-32 symbols -> the 25-byte redirect record."""
    bits = 0
    for c in body:
        bits = (bits << 5) | _TOK_VAL[c]
    return bits.to_bytes(25, "big")


def token_encode(raw):
    """The 25-byte redirect record -> 40 base-32 symbols."""
    bits = int.from_bytes(raw, "big")
    return "".join(TOKEN_ALPHABET[(bits >> (5 * (39 - i))) & 31]
                   for i in range(40))

#: A bare IPv4 literal, used to skip DNS when a target is already an address.
_IPV4_ONLY = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def learned_node():
    with _LEARNED_LOCK:
        return _LEARNED_NODE["ip"]


def consume_pending_node():
    """Return the redirect target if a greeting is awaiting its follow-up.

    Returns None once `PENDING_TTL` has elapsed, so an ignored greeting cannot
    hijack an unrelated connection later.
    """
    with _LEARNED_LOCK:
        if time.monotonic() < _LEARNED_NODE["pending_until"]:
            _LEARNED_NODE["pending_until"] = 0.0
            return _LEARNED_NODE["ip"]
    return None


def _rewrite_greeting(data, stub_ip, port):
    """Point the greeting's redirect at us, remembering the real node.

    Rewriting the IRC prefix alone does nothing: measured 2026-08-09, the client
    ignores it and dials the address carried *inside* the token. So we decode the
    token, swap bytes [8:12] for stub_ip, and re-encode. The port is left alone,
    so the client's follow-up lands on the same port here and we forward it to
    that port on the real node.
    """
    def sub(m):
        real, target, body, tail = (m.group(1).decode(), m.group(2).decode(),
                                    m.group(3).decode(), m.group(4).decode())
        try:
            raw = bytearray(token_decode(body))
        except KeyError as e:            # symbol outside the alphabet
            log("tcp", f"port {port} greeting token not decodable ({e}); "
                       f"passing through unchanged")
            return m.group(0)
        tok_ip = socket.inet_ntoa(bytes(raw[8:12]))
        tok_port = struct.unpack(">H", bytes(raw[12:14]))[0]
        raw[8:12] = socket.inet_aton(stub_ip)
        with _LEARNED_LOCK:
            _LEARNED_NODE["ip"] = tok_ip
            _LEARNED_NODE["pending_until"] = time.monotonic() + PENDING_TTL
        log("tcp", f"port {port} REDIRECT token said {tok_ip}:{tok_port} "
                   f"(prefix {real}) -> rewritten to {stub_ip}:{tok_port}; "
                   f"follow-up will relay to {tok_ip}")
        return (f":{stub_ip} 300 {target} "
                f"{token_encode(bytes(raw))}{tail}\r").encode()

    return _GREETING.sub(sub, data)


#: POLP patch-protocol magic (tcp/54000). See [[polp-protocol]] in memory.
POLP_MAGIC = b"POLP"


def polp_checksum(buf):
    """The POLP 0x04 checksum: u32-LE of MD5(packet[8:])[:4].

    Cracked 2026-08-09: it is the first four bytes of the MD5 of the packet
    from the "POLP" magic onward (i.e. excluding total_len and the checksum
    field itself), read little-endian. The server drops any connection whose
    checksum does not match, so every rewritten frame must recompute this.
    """
    return int.from_bytes(hashlib.md5(bytes(buf)[8:]).digest()[:4], "little")


def _rewrite_polp_dlhost(data, stub_ip, port):
    """Point a POLP cmd-8 reply's download host at us, recomputing the checksum.

    The version-check reply (cmd 8) carries the download host as a plain string
    at 0x18+ (after the status string). The client dials that host by *raw IP*
    for the file-transfer phase, bypassing DNS -- so unless we rewrite it to
    stub_ip, the download never comes back through the relay and we cannot see
    the transfer protocol. Because the string region is fixed-width (0x18..0x58,
    zero-padded, with body_len always at 0x58) a shorter IP just leaves more
    padding; total_len is unchanged. Only the checksum must be recomputed.
    """
    if len(data) < 0x5c or data[8:12] != POLP_MAGIC:
        return data
    if struct.unpack_from("<I", data, 12)[0] != 8:
        return data
    buf = bytearray(data)
    region_end = 0x58
    off, strs = 0x18, []
    while off < region_end and len(strs) < 3:
        end = buf.find(b"\x00", off, region_end)
        if end < 0:
            end = region_end
        strs.append(buf[off:end])
        off = end + 1
    if len(strs) < 2:
        return data
    old = strs[1].decode("latin-1", "replace")
    if old == stub_ip:
        return data
    strs[1] = stub_ip.encode()
    region = bytearray(region_end - 0x18)
    p = 0
    for s in strs:
        region[p:p + len(s)] = s
        p += len(s) + 1
    buf[0x18:region_end] = region
    struct.pack_into("<I", buf, 4, 0)
    struct.pack_into("<I", buf, 4, polp_checksum(buf))
    log("tcp", f"port {port} POLP cmd-8 dlhost {old} -> {stub_ip}; "
               f"checksum recomputed -- download will relay through us")
    return bytes(buf)


_HTTP_METHODS_C2S = (b"GET ", b"POST ", b"HEAD ")


def _strip_conditional(data):
    """Remove If-Modified-Since / If-None-Match from a relayed HTTP request.

    The PlayOnline Viewer keeps its own on-disk page cache, so a browse of the
    real portal is mostly conditional requests answered `304 Not Modified` with
    NO BODY -- one capture showed 4x200 against ~54x304, which is why almost
    nothing could be archived. Dropping the validators makes SE answer 200 with
    the full entity every time, so one browse mirrors everything it touches.
    Off unless POL_TCP_NO_CACHE=1; captures still record the ORIGINAL client
    bytes (see _pump's `sink`), so this only changes what we forward upstream."""
    if not data.startswith(_HTTP_METHODS_C2S):
        return data
    out, dropped = [], 0
    for line in data.split(b"\r\n"):
        low = line.lower()
        if low.startswith(b"if-modified-since:") or low.startswith(b"if-none-match:"):
            dropped += 1
            continue
        out.append(line)
    return b"\r\n".join(out) if dropped else data


def _pump(src, dst, port, direction, sink, transform=None):
    """Copy src->dst, logging and buffering each chunk. Closes dst on EOF.

    `sink` always accumulates the *original* bytes, so captures stay faithful to
    what the real server sent even when `transform` rewrites what we forward.
    """
    try:
        while True:
            data = src.recv(8192)
            if not data:
                break
            sink.extend(data)
            out = transform(data) if transform else data
            dst.sendall(out)
            log("tcp", f"port {port} {direction} {len(data)} bytes\n"
                       + hexdump(data))
            if transform and out != data:
                log("tcp", f"port {port} {direction} forwarded as {len(out)} "
                           f"bytes after rewrite\n" + hexdump(out))
    except Exception as e:
        log("tcp", f"port {port} {direction} pump ended: {e}")
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


#: `tcp_targets` value meaning "whichever node last issued a login greeting".
LEARNED = "@learned"


def _tcp_relay(conn, addr, port, target_host, pins, upstream_dns,
               rewrite_prefix_to=None, rewrite_polp_to=None):
    """MITM relay: client <-> real PlayOnline server, logging both directions.

    `target_host` may be the sentinel `@learned`, meaning "the node that issued
    the most recent login greeting" -- that is how the client's follow-up
    connection, which it makes by raw IP, gets forwarded to the right server.
    """
    peer = f"{addr[0]}:{addr[1]}"
    if rewrite_prefix_to:
        # This is the directory port -- but it doubles as a redirect target, so
        # a greeting already issued means this connection is the follow-up.
        pending = consume_pending_node()
        if pending:
            log("tcp", f"port {port} {peer} follow-up after redirect; "
                       f"relaying to node {pending} instead of {target_host}")
            target_host = pending
            rewrite_prefix_to = None
    if target_host == LEARNED:
        ip = learned_node()
        if not ip:
            log("tcp", f"port {port} {peer} no login greeting seen yet; "
                       f"cannot resolve {LEARNED}, dropping")
            conn.close()
            return
        target_host = ip
    elif _IPV4_ONLY.fullmatch(target_host or ""):
        ip = target_host          # already an address (a learned node)
    else:
        try:
            ip = pins.get(target_host) or resolve_a(target_host, upstream_dns)
        except Exception as e:
            log("tcp", f"port {port} {peer} relay resolve failed for "
                       f"{target_host}: {e}")
            conn.close()
            return
    log("tcp", f"RELAY port {port} {peer} -> {target_host} [{ip}]:{port}")
    try:
        up = socket.create_connection((ip, port), timeout=10)
    except Exception as e:
        log("tcp", f"port {port} {peer} upstream connect failed: {e}")
        conn.close()
        return
    # create_connection leaves its 10s connect timeout on the socket, which would
    # make recv() abort a persistent but idle connection (heartbeats, the chat /
    # community link) after 10s and drop the session. Relayed connections must be
    # blocking so they live as long as the client keeps them open.
    up.settimeout(None)
    conn.settimeout(None)
    c2s, s2c = bytearray(), bytearray()
    xform = None
    if rewrite_prefix_to:
        def xform(data, _ip=rewrite_prefix_to, _p=port):
            return _rewrite_greeting(data, _ip, _p)
    elif rewrite_polp_to:
        def xform(data, _ip=rewrite_polp_to, _p=port):
            return _rewrite_polp_dlhost(data, _ip, _p)
    # Client->server transform: optionally drop cache validators so the upstream
    # sends full bodies instead of 304s (see _strip_conditional).
    c2s_xform = (_strip_conditional
                 if os.environ.get("POL_TCP_NO_CACHE", "0") == "1" else None)
    t1 = threading.Thread(target=_pump,
                          args=(conn, up, port, f"C->S({target_host})", c2s,
                                c2s_xform),
                          daemon=True)
    t2 = threading.Thread(target=_pump,
                          args=(up, conn, port, f"S->C({target_host})", s2c,
                                xform),
                          daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    stamp = _stamp().replace(":", "").replace(".", "")
    if c2s:
        save_capture(f"relay-{port}-{stamp}-c2s.bin", bytes(c2s))
    if s2c:
        save_capture(f"relay-{port}-{stamp}-s2c.bin", bytes(s2c))
    for s in (conn, up):
        try:
            s.close()
        except OSError:
            pass
    log("tcp", f"RELAY CLOSE port {port} {peer} "
               f"(C->S {len(c2s)} B, S->C {len(s2c)} B)")


def _tcp_listener(port, target_host=None, pins=None, upstream_dns="1.1.1.1",
                  rewrite_prefix_to=None, rewrite_polp_to=None, hold=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        log("tcp", f"cannot bind {port}: {e}")
        return
    s.listen(16)
    mode = f"relay -> {target_host}" if target_host else "log-only"
    if hold and not target_host:
        mode += f", capture (hard close after {hold}s)"
    if rewrite_prefix_to:
        mode += f", redirect-rewrite -> {rewrite_prefix_to}"
    if rewrite_polp_to:
        mode += f", polp-dlhost-rewrite -> {rewrite_polp_to}"
    log("tcp", f"listening on {port} ({mode})")
    while True:
        try:
            conn, addr = s.accept()
        except Exception as e:  # pragma: no cover
            log("tcp", f"accept error on {port}: {e}")
            continue
        if target_host:
            threading.Thread(
                target=_tcp_relay,
                args=(conn, addr, port, target_host, pins or {}, upstream_dns,
                      rewrite_prefix_to, rewrite_polp_to),
                daemon=True).start()
        else:
            threading.Thread(target=_tcp_client, args=(conn, addr, port, hold),
                             daemon=True).start()


def run_tcp():
    cfg = load_config()
    def _expand(seq):
        """Expand a list that may contain "a-b" range strings into ints."""
        out = []
        for item in seq or []:
            s = str(item)
            if "-" in s:
                a, b = s.split("-", 1)
                out.extend(range(int(a), int(b) + 1))
            else:
                out.append(int(s))
        return out

    listen = cfg.get("listen", {}) or {}
    ports = list(dict.fromkeys(_expand(listen.get("tcp", []))))
    # listen.tcp_hold: {port: seconds}. See _tcp_client -- a bind that only LOGS
    # strands any client that polls, so a capture port names its own ceiling.
    holds = {int(k): float(v) for k, v in (listen.get("tcp_hold") or {}).items()}
    proxy = cfg.get("proxy", {}) or {}
    # tcp_targets keys may be single ports or "a-b" ranges; explicit single-port
    # entries win over a range that also covers them.
    targets = {}
    range_items = {k: v for k, v in (proxy.get("tcp_targets") or {}).items()
                   if "-" in str(k)}
    single_items = {k: v for k, v in (proxy.get("tcp_targets") or {}).items()
                    if "-" not in str(k)}
    for k, v in range_items.items():
        for p in _expand([k]):
            targets[p] = v
    for k, v in single_items.items():
        targets[int(k)] = v
    pins = proxy.get("pins") or {}
    updns = proxy.get("upstream_dns", "1.1.1.1")
    # Ports whose greeting prefix gets rewritten to stub_ip, so the client's
    # follow-up connection comes back here instead of going direct to the node.
    rewrite_ports = {int(p) for p in (proxy.get("rewrite_prefix_ports") or [])}
    # Ports whose POLP cmd-8 download-host field gets rewritten to stub_ip, so
    # the client's raw-IP file-transfer connection comes back through the relay.
    polp_ports = {int(p) for p in (proxy.get("rewrite_polp_dlhost_ports") or [])}
    # Same precedence as the resolver above and as responders.py: this value is
    # written INTO the client's redirect greeting and POLP cmd-8 download host,
    # so on prod it must be the env's address, not the shared config's dev one.
    stub_ip = advertise_ip(cfg, default=None)
    if rewrite_ports and not stub_ip:
        log("tcp", "rewrite_prefix_ports set but stub_ip is empty; "
                   "redirect rewriting disabled")
        rewrite_ports = set()
    if polp_ports and not stub_ip:
        log("tcp", "rewrite_polp_dlhost_ports set but stub_ip is empty; "
                   "POLP dlhost rewriting disabled")
        polp_ports = set()
    if not ports:
        log("tcp", "no tcp ports configured; nothing to do")
        while True:
            threading.Event().wait(3600)
    threads = []
    for port in ports:
        t = threading.Thread(
            target=_tcp_listener,
            args=(port, targets.get(port), pins, updns,
                  stub_ip if port in rewrite_ports else None,
                  stub_ip if port in polp_ports else None,
                  holds.get(port)),
            daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


# --------------------------------------------------------------------------- #
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "dns":
        run_dns()
    elif mode == "http":
        run_http()
    elif mode == "tcp":
        run_tcp()
    else:
        raise SystemExit("usage: stub.py {dns|http|tcp}")


if __name__ == "__main__":
    main()
