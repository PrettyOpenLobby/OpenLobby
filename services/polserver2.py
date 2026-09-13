"""A drop-in replacement for Square Enix's POLP patch service.

Serves bundles produced by polarchive2.py.  Point a client's DNS at this host
(so pt008.pol.com and the pc<id><region>.pol.com names resolve here) and it
version-checks, pulls the patch list, and downloads every file from the local
archive -- byte for byte what SE's box returns.

Supersedes polserver.py, which bound a single port and answered file ranges
with cmd 2.  The live service actually splits across ports and replies cmd 4;
see "Protocol" below.  Everything here was measured against SE's box
(124.150.156.107) on 2026-08-11, not inferred.

Layout served (one dir per service, from polarchive2.py):
  <root>/
    W2U-1000/ { meta.json, patchlist.raw, manifest.json,
                blobs/<ver>/Direct/<path>.slc, blobs/<ver>/Indirect/<path>.olc }
    W20-0001/ ...   (FINAL FANTASY XI)

Ports: one listener per distinct meta.port.  The Viewer's own tree is on 54000;
a per-title tree is on 53000 + content id (FFXI id 1 -> 53001, Friend List
id 14 -> 53014), which is why the client resolves pc001w2u/pc014w20.pol.com.

Protocol (checksum = MD5(packet[8:])[:4] as an LE u32 at 0x04, on every frame):
  cmd 7 -> cmd 8 : version check.  Replies the service token, the download
                   host, and the latest version.  The status string depends
                   only on the SHAPE of the version the client sent.
  cmd 1 -> cmd 2 : patch list, patchlist.raw verbatim.
  cmd 3 -> cmd 4 : ranged read of one blob.  A single reply is capped at
                   ~2 MB, exactly as the real server caps it.
  anything else  -> cmd 5 reject, as the real server does.

Usage:
  python polserver2.py mirrors --advertise 127.0.0.1
  python polserver2.py mirrors --advertise 192.0.2.5 --check
"""
import argparse, json, os, socket, struct, hashlib, threading, sys, re, time

# The real server never puts more than this in one cmd-4 frame, however much
# the client asks for; measured by requesting a 6.5 MB blob in one go.
MAX_FRAME_DATA = 2 * 1024 * 1024
# The u32 at 0x10 of every cmd-8 is the patch list's PUBLICATION TIME as a unix
# timestamp -- it looks like a nonce but decodes cleanly and differs per service
# exactly as the trees' ages do: W2U 0x4e4e6497 = 2011-08-12, W20 0x4e4e62ca =
# the same afternoon, FFXI 0x6a4cd873 = 2026-07-11 against a 30260703_1 build.
# It is constant only because those trees stopped moving, so replay the archived
# value per bundle rather than sending one number for everything.
DEFAULT_TOKEN = 0x4e4e6497

_VER_LEAD = re.compile(rb"^[1-9][0-9]{3}")


def cksum(buf):
    return int.from_bytes(hashlib.md5(bytes(buf)[8:]).digest()[:4], "little")


def cstr(d, off, end):
    e = d.find(b"\x00", off, end)
    return d[off:(e if e >= 0 else end)]


def ps2_request(pkt, cmd):
    """True when this frame came from a PS2 client (region tag 'PS2').

    The region rides in every request -- 0x10 for cmd 7/1, 0x18 for cmd 3 --
    so each reply can be gated without per-connection state.  PC clients say
    W2U/W20 and must never be paced.
    """
    off = 0x18 if cmd == 3 else 0x10
    return len(pkt) >= off + 4 and cstr(pkt, off, off + 4) == b"PS2"


def paced_send(conn, data):
    """Send a PS2-bound reply at ~280 KB/s (1400 B / 5 ms), like responders.py's
    _band_send.  PCSX2's `EthApi = Sockets` reimplements TCP and loses the
    guest's ACK numbering when a burst outruns it -- POL-0006, `Bad TCP numbers
    received` in its emulog.  The 51300 band was mitigated this way 2026-08-23
    (092ac24d); the Janhourou update hit the identical collapse here on 53003
    (2026-09-03: the 1.7 MB pex died mid-transfer on every attempt, leaving
    JanHouRou.pex.tmp behind), and a cmd-4 frame can be up to MAX_FRAME_DATA =
    2 MB in one go.  POL_PS2_CHUNK=0 disables.  Sleep to a monotonic DEADLINE,
    never a fixed sleep per chunk -- timer granularity on a Windows host was
    measured multiplying per-chunk sleeps ~10x."""
    chunk = int(os.environ.get("POL_PS2_CHUNK", "1400"))
    if chunk <= 0 or len(data) <= chunk:
        conn.sendall(data)
        return
    delay = float(os.environ.get("POL_PS2_CHUNK_DELAY_MS", "5")) / 1000.0
    deadline = time.monotonic()
    for i in range(0, len(data), chunk):
        conn.sendall(data[i:i + chunk])
        deadline += delay
        wait = deadline - time.monotonic()
        if wait > 0:
            time.sleep(wait)


def size_on_disk(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def version_status(ver, oldest):
    """Reproduce the real server's cmd-8 status string.

    It is a per-service LOOKUP, not a shape test: "registered" means *this tree
    can patch you from there*, i.e. the version you claim is at or after the
    oldest build the tree still carries.  The same string gets different answers
    from different services -- `20030909_A` is `registered` on W2U (whose history
    starts exactly there) and `unknown` on P2U (which starts 20031006_0) and on
    X2U/XB2 (20060215_W).  An earlier reading of this as a pure shape test was
    wrong; it only looked that way because every probe went to W2U.

        ''                                          -> empty
        <10 chars, or not [1-9]ddd                   -> unknown
        no '_'                                       -> registered
        digit run before '_' shorter than 7          -> unknown
        else compare that digit run with the oldest
        version's, as a STRING                       -> registered if >=

    String comparison, not numeric: `2011082_9E` (a 7-digit run) is registered
    against a 20030909 tree because '2011082' > '20030909' lexically, though it
    is the smaller number.  Fits all 30 probes across all six services.
    """
    if not ver:
        return "empty"
    if len(ver) < 10 or not _VER_LEAD.match(ver):
        return "unknown"
    if b"_" not in ver:
        return "registered"
    run = 0
    while run < len(ver) and 0x30 <= ver[run] <= 0x39:
        run += 1
    if run < 7:
        return "unknown"
    return "registered" if ver[:run] >= oldest[:8] else "unknown"


class Bundle:
    def __init__(self, path):
        self.path = path
        self.blobs = os.path.join(path, "blobs")
        with open(os.path.join(path, "patchlist.raw"), "rb") as f:
            self.patchlist = f.read()
        meta = {}
        mp = os.path.join(path, "meta.json")
        if os.path.exists(mp):
            with open(mp) as f:
                meta = json.load(f)
        self.meta = meta
        self.latest = meta.get("latest_version", "")
        base = os.path.basename(path)
        self.region = meta.get("region", base.split("-")[0])
        self.product = meta.get("product", base.partition("-")[2] or "1000")
        # Extra region codes this same tree answers for. Archived bundles never
        # set this -- one mirror per region is what SE actually serves. It is
        # for SYNTHESISED bundles (polsynth.py) where one installed tree covers
        # more than one region code: Tetra Master's EU install is byte-identical
        # to the US one apart from patch.ver, so serving both from one bundle is
        # correct rather than a shortcut.
        self.aliases = list(meta.get("region_aliases", []))
        self.port = int(meta.get("port", 54000))
        self.token = int(meta.get("cmd8_token", DEFAULT_TOKEN))
        # The oldest build in the tree -- the cutoff the status string compares
        # against.  Recorded by polarchive2; derived from patch.cfg when a bundle
        # predates that, so an old bundle still answers correctly.
        oldest = meta.get("oldest_version")
        if not oldest:
            from polbundle import manifest
            try:
                oldest = min(r["version"] for r in manifest(path))
            except (FileNotFoundError, ValueError):
                oldest = self.latest or "00000000_0"
        self.oldest = oldest.encode("latin-1")

    def blob_path(self, path):
        """Resolve a requested blob path, refusing anything that escapes blobs/."""
        if "\\" in path or path.startswith("/"):
            return None
        full = os.path.normpath(os.path.join(self.blobs, path.replace("/", os.sep)))
        root = os.path.normpath(self.blobs)
        if full != root and not full.startswith(root + os.sep):
            return None
        return full


def load_bundles(root):
    out = {}
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "patchlist.raw")):
            b = Bundle(p)
            out[(b.region, b.product)] = b
            for alias in b.aliases:
                # A real bundle for that region always wins: aliases fill gaps,
                # they never shadow an archived tree.
                out.setdefault((alias, b.product), b)
    return out


def frame(cmd, payload):
    b = bytearray(16); b[8:12] = b"POLP"
    struct.pack_into("<I", b, 12, cmd)
    b += payload
    struct.pack_into("<I", b, 0, len(b))
    struct.pack_into("<I", b, 4, cksum(b))
    return bytes(b)


def build_cmd8(token, latest, dlhost, status):
    b = bytearray(0x58); b[8:12] = b"POLP"
    struct.pack_into("<I", b, 12, 8)
    struct.pack_into("<I", b, 0x10, token)
    struct.pack_into("<I", b, 0x14, 1)
    s = status.encode() + b"\x00" + dlhost.encode() + b"\x00" + b"0\x00"
    b[0x18:0x18 + len(s)] = s
    body = latest.encode() + b"\x00"
    b += struct.pack("<I", len(body)) + body
    struct.pack_into("<I", b, 0, len(b))
    struct.pack_into("<I", b, 4, cksum(b))
    return bytes(b)


REJECT = frame(5, b"")


class Server:
    def __init__(self, bundles, advertise, verbose, current=(), current_all=False):
        self.bundles = bundles
        self.advertise = advertise
        self.verbose = verbose
        # (region, product) pairs answered "you are already current" with no
        # bundle behind them -- see answer_current().
        self.current = set(current)
        #: answer "you are up to date" for ANY tree we do not have, so a
        #: deployment with no patch trees at all lets every title launch
        self.current_all = current_all
        self.lock = threading.Lock()
        self.stats = {"cmd1": 0, "cmd3": 0, "cmd7": 0, "reject": 0, "bytes": 0}
        self._last_report = 0

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def bump(self, key, n=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + n
            # Heartbeat every 64 MB so a multi-hundred-MB update is visible while
            # it runs. Without this the only evidence a transfer happened is the
            # client's own files, which is how a 646 MB FFXI update was once
            # mistaken for an idle server.
            if key == "bytes":
                served = self.stats["bytes"]
                if served - self._last_report >= 64 * 1024 * 1024:
                    self._last_report = served
                    print(f"[*] served {served/1e6:.0f} MB across "
                          f"{self.stats['cmd3']} ranges", flush=True)

    def handle(self, conn, addr):
        conn.settimeout(120)
        buf = b""
        try:
            while True:
                # HEADER FIRST, then the body. The length check below used to sit
                # AFTER the read loop, so a frame claiming 4 GB was buffered into
                # memory in full and only then rejected -- the one shape of
                # malformed frame that costs more than it should.
                while len(buf) < 16:
                    c = conn.recv(65536)
                    if not c:
                        return
                    buf += c
                total = struct.unpack_from("<I", buf, 0)[0]
                if total < 16 or total > 1 << 20:
                    self.log(f"[{addr[0]}] frame claims {total} B "
                             f"(limit {1 << 20}); dropping")
                    return
                while len(buf) < total:
                    c = conn.recv(65536)
                    if not c:
                        return
                    buf += c
                pkt, buf = buf[:total], buf[total:]
                if pkt[8:12] != b"POLP":
                    return
                # The real server drops the connection on a bad checksum rather
                # than replying; polerr.bin's "POLPRO checksum error" is the
                # client-side twin of this.
                if struct.unpack_from("<I", pkt, 4)[0] != cksum(pkt):
                    self.log(f"[{addr[0]}] bad checksum, dropping")
                    return
                cmd = struct.unpack_from("<I", pkt, 12)[0]
                reply = self.dispatch(pkt, cmd, addr)
                if reply is None:
                    return
                if ps2_request(pkt, cmd):
                    paced_send(conn, reply)
                else:
                    conn.sendall(reply)
        except (OSError, struct.error):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def record_build(self, region, prod, ver, addr):
        """Note which build this client claims, for the portal to read.

        A client announces its exact build here and NOWHERE else: the portal
        request carries a User-Agent with platform and language but no version,
        so this check is the only place the server learns whether it is talking
        to a 2004 Viewer or a 2011 one. The portal needs that to pick the right
        page set (see config/portal-eras.yaml), and the two run in different
        containers, so hand it over through the shared logs volume.

        Best-effort by design: a failure here must never cost a patch reply.
        """
        path = os.environ.get("POLP_BUILDS_FILE", "/logs/client-builds.json")
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = {}
            entry = state.setdefault(addr[0], {})
            entry[f"{region}/{prod}"] = {
                "version": ver.decode("latin-1"),
                "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError:
            pass

    def answer_current(self, region, prod, ver, addr):
        """Tell a client it is already up to date, with no bundle behind it.

        A pin needs a tree to pin; this needs nothing, because the version it
        advertises is the one the CLIENT just claimed.  It exists for the case a
        pin cannot cover: a service whose tree we do not have at all, where the
        REJECT is itself the failure.  The PS2's FFXI check is the example --
        `pc001ps2` has no PS2/0001 mirror, and a REJECT there is not "no update
        available" to the client, it is a dead patch server: POL-1161, and the
        title never launches.  Answering "you are current" gets the launch past a
        check it only ever had to pass.

        No update is offered, so the client never asks for the list -- which is
        just as well, since there is no list to give it.  That is the whole
        contract: this is for a client that is ALREADY at a playable version, not
        a way to fake a patch service.  A malformed or empty version claim is
        echoed as-is and flagged, because the status string it produces
        ("unknown"/"empty") is then the client's own doing, not ours.
        """
        self.bump("cmd7")
        latest = ver.decode("latin-1")
        # Compared against ITSELF: version_status is a "can this tree patch you
        # from there" test, and a tree whose oldest build is the client's own
        # build always can. Well-formed versions come back `registered`;
        # malformed ones keep their honest `unknown`/`empty`.
        status = version_status(ver, ver)
        self.log(f"[{addr[0]}] cmd7 {region}/{prod} ver={latest!r}"
                 f" -> {status}, latest={latest} (CURRENT: no bundle, "
                 f"advertising the client's own version)")
        if status != "registered":
            self.log(f"[{addr[0]}]   note: {latest!r} is not a well-formed "
                     f"version, so this reply may not satisfy the client")
        return build_cmd8(DEFAULT_TOKEN, latest, self.advertise, status)

    def dispatch(self, pkt, cmd, addr):
        if cmd == 7:
            region = cstr(pkt, 0x10, 0x14).decode("latin-1")
            prod = cstr(pkt, 0x14, 0x18).decode("latin-1")
            ver = cstr(pkt, 0x18, min(len(pkt), 0x58))
            self.record_build(region, prod, ver, addr)
            b = self.bundles.get((region, prod))
            if not b:
                if (region, prod) in self.current or self.current_all:
                    return self.answer_current(region, prod, ver, addr)
                self.bump("reject")
                # Log the claimed version even though we cannot answer: it is the
                # one thing a missing bundle tells us for free, and it is exactly
                # what a pin needs (pin to what the client already runs and no
                # update is offered). Without it, learning a console's version
                # costs a second launch attempt.
                self.log(f"[{addr[0]}] cmd7 {region}/{prod} "
                         f"ver={ver.decode('latin-1')!r} -> REJECT (not archived)")
                return REJECT
            self.bump("cmd7")
            status = version_status(ver, b.oldest)
            self.log(f"[{addr[0]}] cmd7 {region}/{prod} ver={ver.decode('latin-1')!r}"
                     f" -> {status}, latest={b.latest}")
            return build_cmd8(b.token, b.latest, self.advertise, status)

        if cmd == 1:
            region = cstr(pkt, 0x10, 0x14).decode("latin-1")
            prod = cstr(pkt, 0x14, 0x18).decode("latin-1")
            b = self.bundles.get((region, prod))
            if not b:
                self.bump("reject")
                self.log(f"[{addr[0]}] cmd1 {region}/{prod} -> REJECT (not archived)")
                return REJECT
            self.bump("cmd1")
            self.log(f"[{addr[0]}] cmd1 {region}/{prod} -> patch list "
                     f"({len(b.patchlist)} B)")
            return frame(2, b.patchlist)

        if cmd == 3:
            try:
                offset, length = struct.unpack_from("<II", pkt, 0x10)
                region = cstr(pkt, 0x18, 0x1c).decode("latin-1")
                prod = cstr(pkt, 0x1c, 0x20).decode("latin-1")
                plen = struct.unpack_from("<I", pkt, 0x20)[0]
                path = cstr(pkt, 0x24, 0x24 + plen).decode("latin-1")
            except struct.error:
                self.bump("reject")
                return REJECT
            b = self.bundles.get((region, prod))
            fp = b.blob_path(path) if b else None
            if not fp or not os.path.isfile(fp):
                self.bump("reject")
                self.log(f"[{addr[0]}] cmd3 {region}/{prod} {path!r} -> REJECT (absent)")
                return REJECT
            with open(fp, "rb") as f:
                f.seek(offset)
                data = f.read(min(length, MAX_FRAME_DATA))
            self.bump("cmd3")
            self.bump("bytes", len(data))
            # Log the START of each file, not every range: a real FFXI update is
            # ~10k chunk requests and one line each is unreadable. Logging ONLY
            # the reject path (as this once did) is worse than useless -- it makes
            # a server that is happily streaming hundreds of MB look completely
            # idle, and invites the conclusion that nothing was served.
            if offset == 0:
                self.log(f"[{addr[0]}] cmd3 {region}/{prod} {path} "
                         f"({size_on_disk(fp)} B)")
            # The sub-header echoes the REQUESTED length, not what we send --
            # that is what the real server does, and the client uses the frame
            # length to know how much actually arrived.
            sub = struct.pack("<III", offset, length, len(path) + 1) \
                + path.encode("latin-1") + b"\x00" + data
            return frame(4, sub)

        self.bump("reject")
        self.log(f"[{addr[0]}] cmd{cmd} -> REJECT (unsupported)")
        return REJECT

    def serve_port(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(64)
        while True:
            conn, addr = s.accept()
            # Log the ACCEPT, not just the commands. Everything below only
            # speaks once a POLP frame has been parsed, so a client that
            # connects and says nothing -- or dials the wrong port, or drops
            # mid-handshake -- is completely invisible. That gap turned "is the
            # client even reaching us?" into guesswork during the Friend List
            # work; a bare connect line answers it in one launch.
            self.log(f"[+] {addr[0]}:{addr[1]} connected on :{port}")
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()


def check_bundles(bundles):
    """Report what each bundle can actually serve, before trusting it live."""
    from polbundle import manifest
    allgood = True
    for (region, prod), b in sorted(bundles.items()):
        try:
            rows = manifest(b.path)
        except FileNotFoundError as e:
            print(f"  {region}/{prod} :{b.port:<6} cannot verify: {e}")
            allgood = False
            continue
        want = []
        for r in rows:
            want.append((r["slc"], r["slc_size"]))
            if r.get("olc"):
                want.append((r["olc"], r["olc_size"]))
        missing = short = 0
        for path, size in want:
            fp = b.blob_path(path)
            try:
                if os.path.getsize(fp) != size:
                    short += 1
            except OSError:
                missing += 1
        newest = {}
        for r in rows:
            newest[r["path"]] = r
        newest_missing = sum(
            1 for r in newest.values()
            if not os.path.exists(b.blob_path(r["slc"]) or "")
        )
        ok = missing == 0 and short == 0
        allgood &= ok
        print(f"  {region}/{prod} :{b.port:<6} latest={b.latest:<12} "
              f"{len(want)-missing-short}/{len(want)} blobs"
              + (f"  MISSING {missing} SHORT {short}" if not ok else "  complete")
              + (f"  [current build incomplete: {newest_missing} files]"
                 if newest_missing else ""))
    return allgood


def apply_pins(bundles, specs):
    """Cap the version a bundle advertises as newest.

    The archives go all the way to the end of each service's life, and the last
    build is not always one you want a client to take: PS2/1000's newest is
    `20150901_X`, months before PlayOnline's PS2 service closed, and a drive
    sitting on `20040428_5` would be walked all the way there the first time it
    checks in.  Pinning changes only what cmd-8 advertises.

    That is enough to stop an update outright -- pin to the version the client
    already runs and it concludes it is current, so it never even asks for the
    list.  A *partial* pin (to some middle build) is NOT enough on its own, and
    that is measured rather than feared: on 2026-08-15 a PS2 pinned to
    20120610_A read the untouched `patchlist.raw` and fetched every later row
    anyway.  Pair any partial pin with the matching `--list-cap`, which re-cuts
    the catalogue itself.

    (This docstring used to say re-cutting the list "needs an SLC compressor,
    which we do not have".  `slc.slc_compress_literal` landed 2026-08-15 and
    `apply_list_caps` below is built on it.)
    """
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        target, _, version = spec.partition("=")
        region, _, prod = target.strip().partition("/")
        if not (region and prod and version):
            raise SystemExit(f"--pin wants REGION/PROD=VERSION, got {spec!r}")
        b = bundles.get((region.strip(), prod.strip()))
        if not b:
            raise SystemExit(f"--pin {spec}: no such bundle")
        if not re.fullmatch(r"\d{8}_[0-9A-Za-z]", version):
            raise SystemExit(f"--pin {spec}: {version!r} is not a version string "
                             f"(YYYYMMDD_X)")
        try:
            rows = {r["version"] for r in _manifest_versions(b)}
        except Exception:
            rows = set()
        # Not being in the tree is normal for the most useful pin of all:
        # a client's *installed* version, which is whatever it last patched to
        # and need not be a version SE still published rows for.  The PS2 drive
        # sits on 20040428_5, and this archive's rows jump 20020516_4 ->
        # 20040706_2.  Advertising it back is exactly what makes the client
        # decide it is current, so warn rather than refuse.
        if rows and version not in rows:
            print(f"[!] pin {region}/{prod}: {version} is not a row version in "
                  f"this tree ({min(rows)} .. {max(rows)}) -- fine if it is what "
                  f"the client already runs, wrong if you meant to patch to it")
        print(f"[*] pin {region}/{prod}: advertising {version} "
              f"instead of {b.latest}")
        b.latest = version


def apply_list_caps(bundles, specs):
    """Rebuild bundles' cmd-1 lists so nothing newer than the cap is offered.

    Pair this with the matching `--pin`: the pin stops cmd 8 advertising a newer
    build, this stops cmd 1 describing one. Either alone is a partial measure.
    """
    from slc import slc_decompress, slc_compress_literal
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        target, _, cap = spec.partition("=")
        region, _, prod = target.strip().partition("/")
        if not (region and prod and cap):
            raise SystemExit(f"--list-cap wants REGION/PROD=VERSION, got {spec!r}")
        b = bundles.get((region.strip(), prod.strip()))
        if not b:
            raise SystemExit(f"--list-cap {spec}: no such bundle")
        plain = slc_decompress(b.patchlist)
        capped, dropped = cap_patchlist(plain, cap.strip())
        if not dropped:
            print(f"[*] list cap {region}/{prod}: nothing newer than {cap}")
            continue
        b.patchlist = slc_compress_literal(capped)
        print(f"[*] list cap {region}/{prod}: dropped {dropped} row(s) newer "
              f"than {cap} ({len(plain)} -> {len(capped)} B plaintext)")
        # AND LOWER WHAT CMD 8 ADVERTISES, or the two halves disagree: the
        # service would name a newest build whose rows it has just refused to
        # describe, and the client is told it is out of date with nothing
        # fetchable to fix it. Found live on prod 2026-08-16 -- capping
        # P2U/X2U/XB2 left all three still advertising 20150901_X.
        #
        # Only ever DOWNWARD, and only past the cap. An explicit --pin runs
        # first and is usually far below the cap (PS2/1000 sits on 20040428_5
        # under a 20130601_E cap); raising it to the cap would undo the pin and
        # offer the very update the pin exists to prevent.
        if b.latest and b.latest > cap.strip():
            newest = max((r.split(" ", 1)[0]
                          for r in capped.decode("latin-1").split("\n")
                          if re.fullmatch(r"\d{8}_\w", r.split(" ", 1)[0])),
                         default="")
            if newest and newest < b.latest:
                print(f"[*] list cap {region}/{prod}: advertised newest "
                      f"{b.latest} -> {newest} (it is past the cap)")
                b.latest = newest


def cap_patchlist(plain, cap):
    """Drop every row NEWER than `cap` from a decompressed cmd-1 patch list.

    A `--pin` only changes the version cmd 8 ADVERTISES; the client still gets
    the full catalogue on cmd 1 and can walk past the pin -- which is exactly
    what happened on 2026-08-15, when a PS2 pinned to 20120610_A fetched all
    five 20150901_X rows anyway. One of them is
    `V/system/image/goodbye.png`, and the 1.18.15f Viewer already contains a
    dormant `pol::CGoodbyeFrame`: the file's mere presence makes it show the
    service-closed notice and call `sceCdPowerOff`. SE's kill switch needed no
    code update, just that file.

    So cap the CATALOGUE, not just the advertised version, and the client never
    learns those rows exist. Blocks left with no rows are dropped whole -- an
    empty `file X { }` would advertise a file with no way to fetch it.

    Format (verified against SE's own list):

        file <path> {
        <version> <size> <checksum> <token> <Direct blob> [<Indirect blob>]
        }
    """
    out, block, rows = [], None, []
    dropped = 0
    for line in plain.decode("latin-1").split("\n"):
        if line.startswith("file ") and line.rstrip().endswith("{"):
            block, rows = line, []
        elif block is not None and line.strip() == "}":
            if rows:
                out.append(block)
                out.extend(rows)
                out.append(line)
            block, rows = None, []
        elif block is not None:
            ver = line.split(" ", 1)[0]
            if re.fullmatch(r"\d{8}_\w", ver) and ver > cap:
                dropped += 1
            else:
                rows.append(line)
        else:
            out.append(line)
    return "\n".join(out).encode("latin-1"), dropped


def port_for_product(prod):
    """The POLP port a product is served on: 53000 + content id, except the
    Viewer itself (product 1000), which every region shares on 54000."""
    return 54000 if prod == "1000" else 53000 + int(prod)


def parse_current(specs):
    """REGION/PROD strings -> (region, product) pairs for answer_current()."""
    out = set()
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        region, _, prod = spec.partition("/")
        region, prod = region.strip(), prod.strip()
        if not (region and prod):
            raise SystemExit(f"--current wants REGION/PROD, got {spec!r}")
        try:
            port_for_product(prod)
        except ValueError:
            raise SystemExit(f"--current {spec}: {prod!r} is not a product code "
                             f"(four digits, e.g. 0001)")
        out.add((region, prod))
    return out


def _manifest_versions(bundle):
    from polbundle import manifest
    return manifest(bundle.path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="directory of bundles (mirrors/)")
    ap.add_argument("--advertise", default=os.environ.get("ADVERTISE", ""),
                    help="IP handed to clients as the download host -- must be "
                         "an address the CLIENT can reach, i.e. this server")
    ap.add_argument("--check", action="store_true",
                    help="verify bundle completeness against manifest.json and exit")
    ap.add_argument("--bind-offset", type=int, default=0,
                    help="add N to every listener port. For TESTING alongside "
                         "something that already holds 54000 (the pol-server tcp "
                         "relay does) -- otherwise both bind and which one a "
                         "connection reaches is undefined")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--pin", action="append", default=[], metavar="REGION/PROD=VERSION",
                    help="advertise VERSION as the newest build for that bundle "
                         "instead of the real newest. Repeatable; $POLP_PIN takes "
                         "a comma-separated list. Pin a bundle to the version a "
                         "client already has and it is simply told it is current, "
                         "so no update is ever offered.")
    ap.add_argument("--list-cap", action="append", default=[],
                    metavar="REGION/PROD=VERSION",
                    help="drop every cmd-1 catalogue row newer than VERSION, so "
                         "a client cannot walk past a --pin. $POLP_LIST_CAP takes "
                         "a comma-separated list. Use WITH the matching --pin.")
    ap.add_argument("--current", action="append", default=[], metavar="REGION/PROD",
                    help="answer cmd 7 for a tree we do NOT have by echoing the "
                         "client's own version back as newest, so it concludes it "
                         "is up to date instead of seeing a dead patch server. "
                         "Repeatable; $POLP_CURRENT takes a comma-separated list.")
    ap.add_argument("--current-all", action="store_true",
                    default=os.environ.get("POLP_CURRENT_ALL", "") == "1",
                    help="answer cmd 7 for EVERY tree we do not have by echoing "
                         "the client's own version (see --current), and listen "
                         "on every title port. $POLP_CURRENT_ALL=1 does the "
                         "same. Trees we do have are still served normally.")
    args = ap.parse_args()

    bundles = load_bundles(args.root)
    currents_declared = [c for c in args.current
                         + os.environ.get("POLP_CURRENT", "").split(",")
                         if c.strip()]
    if not bundles and not currents_declared and not args.current_all:
        raise SystemExit(f"no bundles under {args.root} - supply patch trees, "
                         "or run with --current/$POLP_CURRENT to answer "
                         "version checks with 'you are up to date'")
    if not bundles:
        print(f"[*] no bundles under {args.root}; serving --current answers only")

    apply_pins(bundles, args.pin + [p for p in
                                    os.environ.get("POLP_PIN", "").split(",") if p.strip()])
    apply_list_caps(bundles, args.list_cap + [c for c in
                    os.environ.get("POLP_LIST_CAP", "").split(",") if c.strip()])

    if args.check:
        print(f"[*] {len(bundles)} bundle(s) under {args.root}:")
        ok = check_bundles(bundles)
        print("[*] all bundles complete" if ok else "[!] some bundles incomplete")
        raise SystemExit(0 if ok else 1)

    if not args.advertise:
        raise SystemExit("--advertise (or $ADVERTISE) is required: clients take "
                         "the download host from the cmd-8 reply and dial it by "
                         "raw IP, bypassing DNS")

    current = parse_current(args.current + [c for c in
                            os.environ.get("POLP_CURRENT", "").split(",") if c.strip()])
    srv = Server(bundles, args.advertise, not args.quiet, current, args.current_all)
    # A "current" pair usually shares a port with a bundle we do have (PS2/0001
    # rides :53001 next to W20/0001), but it need not, and a listener that is
    # never opened answers nothing -- so derive the port and add it.
    ports = sorted({b.port for b in bundles.values()}
                   | {port_for_product(prod) for _, prod in current}
                   | ({54000} | {53000 + n for n in range(1, 16)}
                      if args.current_all else set()))
    if args.current_all:
        print("[*] --current-all: any tree not archived here is answered as "
              "already up to date")
    print(f"[*] POLP server, download host {args.advertise}")
    for (region, prod), b in sorted(bundles.items()):
        print(f"      {region}/{prod} :{b.port:<6} latest={b.latest}")
    for region, prod in sorted(current):
        print(f"      {region}/{prod} :{port_for_product(prod):<6} "
              f"latest=<client's own> (CURRENT, no bundle)")
    for p in ports:
        bind = p + args.bind_offset
        threading.Thread(target=srv.serve_port, args=(bind,), daemon=True).start()
        print(f"[*] listening on 0.0.0.0:{bind}"
              + (f"  (service port {p} + offset)" if args.bind_offset else ""))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print(f"\n[*] {srv.stats}")


if __name__ == "__main__":
    main()
