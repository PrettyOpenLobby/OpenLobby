"""Reply pacing: delays, linger, and PS2 burst pacing on the lobby band."""
import os
import socket
import time
import threading
from srvcore import expand_ports, hexdump, log, save_capture
from . import lobbysession



def _lobby_delay(path):
    """Hold a 3:0 reply back, per path, so a WORKING fetch can be caught live.

        POL_LOBBY_DELAY="u/account=8000"        (comma-separated path=ms)

    WHY THIS EXISTS, and it is a method fix rather than a theory. A PS2 title's
    save fetch stalled with the core parked in state 5 (`sub=1 hi=2 got=0`)
    and every attempt to say what is WRONG with that state has died on the same
    thing: there has never been a **same-phase control**. We know what a stalled
    state-5 looks like; we have never once seen a HEALTHY one, because a working
    fetch completes in ~0.1 s and cannot be hit by hand with F1.

    Two conclusions have already been retracted for exactly this
    reason -- both were differences measured against a
    control that was not comparable. Delaying `u/account`, which succeeds many
    times an hour on the SAME opcode, port, session and socket machinery, parks a
    KNOWN-GOOD fetch in state 5 for as long as we like. Then one savestate holds
    both: the healthy slot and (later in the run) the stalled one, and every field
    that differs is a real difference rather than an artefact of timing.

    Deliberately per-path: delaying everything would just move the whole session.
    """
    spec = os.environ.get("POL_LOBBY_DELAY", "").strip()
    if not spec or not path:
        return
    for item in spec.split(","):
        want, _, ms = item.partition("=")
        if want.strip() != path or not ms.strip().isdigit():
            continue
        secs = int(ms) / 1000.0
        log("lobby", f"  3:0 {path!r}: HOLDING the reply {ms}ms "
                     f"(POL_LOBBY_DELAY) -- SAVE A STATE NOW, this is the "
                     f"healthy-fetch control")
        time.sleep(secs)
        return


def _lobby_linger(conn, peer):
    """Hold the lobby socket OPEN after the conversation instead of closing it.

        POL_LOBBY_LINGER=60        # seconds; 0/unset = the old behaviour

    WHY THIS EXISTS -- it is a candidate FIX, not
    an experiment knob.

    The PS2's `/SQUARE` TCP shim latches a per-handle error the moment a FIN
    arrives. AVE-TCP raises event 2 (`socantrcvmore`); the shim's callback at
    `0x00194230` translates that to **-8** and stores it at `[0x00199d80 + handle]`;
    and RPC 30 -- the fetch receive -- reads that byte FIRST:

        0x00196434  lb   v1, [0x00199d80 + handle]
        0x0019643c  bgez v1, 0x0019644c        ; >= 0 -> drain the receive ring
        0x00196448  sw   v1, 0(s4)             ; <  0 -> return it, ring untouched

    So a FIN delivered while acknowledged bytes are still queued makes them
    **permanently unreachable**: the reply is on the console, in the right ring,
    with the right length -- and the game can never read it. Measured across two
    savestates of one stall: h2 went ESTABLISHED -> CLOSE_WAIT, its status byte 0 ->
    -8, and its 1004 bytes stayed queued while two other sockets completed ten
    enqueue+consume cycles.

    `finally: conn.close()` is what sends that FIN, a few seconds after we answer.
    Lingering lets the CLIENT close first, which is what a satisfied client does
    anyway (a real read closes in ~0.1 s).
    """
    secs = float(os.environ.get("POL_LOBBY_LINGER", "0") or 0)
    if secs <= 0:
        return
    log("lobby", f"{peer} LINGERING up to {secs:g}s before close "
                 f"(POL_LOBBY_LINGER) -- a FIN here strands any reply still "
                 f"queued on the console (§18)")
    t0 = time.time()
    try:
        conn.settimeout(1.0)
        while time.time() - t0 < secs:
            try:
                extra = conn.recv(4096)
            except socket.timeout:
                continue
            if not extra:
                log("lobby", f"{peer} client closed during linger after "
                             f"{time.time() - t0:.1f}s -- it closed first, so we "
                             f"never sent the FIN")
                return
            save_capture(f"lobby-linger", extra)
            log("lobby", f"{peer} +{len(extra)}B during linger\n" + hexdump(extra))
    except OSError as e:
        log("lobby", f"{peer} linger ended early: {e}")
        return
    log("lobby", f"{peer} linger window elapsed; closing now")


# --------------------------------------------------------------------------- #
# PS2 BURST PACING -- the POL-0006 mitigation
# --------------------------------------------------------------------------- #
#: **POL-0006 is `TCPのポートが重複しています` -- "the TCP port is duplicated"**, not
#: the "An unexpected problem occurred" the English dialog shows. Read it with
#: `PYTHONIOENCODING=utf-8 python tools/polerr.py 0006`, never off the screen.
#:
#: It is NOT a fault of ours. Diagnosed 2026-08-23 out of PCSX2's OWN log
#: (`<pcsx2>/logs/emulog.txt`, note the `logs/` -- its `DEV9:` lines are a
#: complete client-side network trace, and a whole Viewer boot reads as
#: 54000 -> 51240 -> 51241 -> 51220 -> 51300). `EthApi = Sockets` does not put
#: Ethernet on the wire: it terminates the guest's TCP/IP and REIMPLEMENTS TCP,
#: and that reimplementation loses the guest's ACK numbers under a burst --
#:
#:   [101.1281] TCP: [PS2] Sent unexpected acknowledgement number, did not match
#:              old numbers, got 55248 expected 55777
#:   [101.1282] TCP: Bad TCP numbers received
#:   [101.1283] TCP: Invalid TCP state
#:   [101.1296] Socket: Closed Dead TCP Connection to 51300
#:   [101.1297] Socket: Creating New TCP Connection to 51300
#:   [101.1297] TCP: Reset closed connection    <- open+reset+close, ONE microsecond
#:
#: -- the emulator killed the session out from under the guest, the Viewer
#: instantly reopened on 51300 and landed in the slot still being torn down, and
#: THAT collision is the duplicate it reports.
#:
#: It fires on the portal's FIRST fetch after the lobby handoff, where three
#: 51300 connections overlap inside 140 ms and 40 of the log's 144
#: `DEV9: TCP: Got a lot of data` warnings land in that one second. **It is a
#: RACE, not a size threshold** -- t=204-207 carries 97 more of those warnings
#: and does NOT fail -- so the lever is how much data the emulator has to swallow
#: at once, not how big the page is. Hence pacing rather than shrinking pages.
#:
#: The REAL fix is `EthApi = PCAP-Bridged` (the console's own stack over raw
#: frames, which cannot hit this bug class at all), but that renumbers the
#: console onto the physical LAN and takes its DNS with it -- see
#: PS2_LAN_CANNOT_REACH_TAILNET_ADVERTISE. This is the half with no side effects.
#:
#: **THIS MUST NOT TOUCH PC USERS, AND THE PORT ALONE IS NOT ENOUGH TO GUARANTEE
#: THAT.** 51300 vs 51304 is a clean 100% split for the VIEWER's PML fetches
#: (see logs/lobby.log), and it is tempting to gate on the port alone -- but
#: :51300 also carries the shim's own autoupdate on Windows:
#:
#:   HTTP/51300 GET /shim/dist/PolHook.dll (Host: ...; UA: PolShim/0.2.0)
#:
#: plus curl and browsers hitting the same tree. Pacing by port would throttle
#: the DLL download for every PC user of the shim. So the gate is the port AND
#: the User-Agent, which names the console outright:
#:
#:   PlayOnline-PML-Viewer/1.00 [jp] (PlayStation 2)     <- paced
#:   PlayOnline-PML-Viewer/1.00 [en] (Windows Vista)     <- untouched
#:   PolShim/0.2.0 · curl/7.85.0 · Mozilla/5.0           <- untouched
#:
#: Anything that is not the console takes the identical single sendall it always
#: did, byte-for-byte and timing-for-timing. POL_PS2_CHUNK=0 disables outright.
#:
#: Pacing is RATE-BASED against a monotonic deadline, not `sleep()` per chunk.
#: A fixed per-chunk sleep makes throughput hostage to the host's timer
#: granularity -- measured here, 14 sleeps of 5 ms cost 670 ms on a Windows host
#: (~16 ms quantum) versus the ~70 ms the same code costs on the Linux container
#: prod actually runs. Sleeping only to the next deadline converges on the target
#: rate on both, and overshoot on a coarse timer simply skips the next sleep.
#:
#: Defaults are a starting point, not a measurement: 1400 B is the PS2's MSS and
#: 280 KB/s keeps a backlog from building ahead of PCSX2's per-frame poll (~0.36 s
#: for a 100 KB image, fine for the portal). We cannot see the emulator's poll
#: cadence from here, so TUNE AGAINST emulog: this is working when
#: `Bad TCP numbers received` stops appearing.
def _ps2_band_ports():
    # 51304 ADDED 2026-09-20 -- the PORTAL band, and the next instance of the
    # same failure class as POL-0006 (:51300, 08-23) and the patch service
    # (:53003, 09-03). Measured from PCSX2's own emulog while the US console
    # browsed the FFXI page with this band UNPACED:
    #     DEV9: TCP: Got a lot of data: 65536 using: 1414   x439
    #     Bad TCP numbers received                          x23
    #     DEV9: TCP: Invalid TCP state                      x120
    # i.e. the emulated stack takes ONE MTU out of a 64 KB burst and discards
    # the rest. The FFXI backgrounds are 90-136 KB, the largest objects on the
    # page, so they and the menu died first -- intermittently, because it is a
    # race against PCSX2's buffer and not a property of any file. It read for a
    # while as "one background never renders".
    #
    # This is mitigation, not the cure: `EthApi = PCAP-Bridged` is the real fix
    # on the emulator side, but it renumbers the console onto the physical LAN.
    return set(expand_ports(
        os.environ.get("POL_PS2_BAND_PORTS", "51300,51304").split(",")))


def _is_ps2_console(hdrs):
    """True only for the PS2 Viewer itself -- never the PC Viewer, shim or curl."""
    return b"playstation" in (hdrs or {}).get(b"user-agent", b"").lower()


def _band_send(conn, resp, port, peer="", hdrs=None):
    """sendall(), but RATE-PACED to the PS2 console. See the POL-0006 note above."""
    chunk = int(os.environ.get("POL_PS2_CHUNK", "1400"))
    if (chunk <= 0 or len(resp) <= chunk
            or port not in _ps2_band_ports() or not _is_ps2_console(hdrs)):
        conn.sendall(resp)          # every PC fetch, and anything already small
        return
    delay = float(os.environ.get("POL_PS2_CHUNK_DELAY_MS", "5")) / 1000.0
    rate = (chunk / delay) if delay > 0 else 0        # bytes/sec, 0 = unthrottled
    t0, sent = time.monotonic(), 0
    for i in range(0, len(resp), chunk):
        conn.sendall(resp[i:i + chunk])
        sent += len(resp[i:i + chunk])
        if not rate or sent >= len(resp):
            continue
        # Sleep only as far as the deadline for what we have already sent, so a
        # coarse timer that overshoots just skips the next wait instead of
        # compounding. The gap IS the fix -- without it the host socket simply
        # re-accumulates the same burst PCSX2 choked on.
        due = t0 + sent / rate
        now = time.monotonic()
        if due > now:
            time.sleep(due - now)
    log("lobby", f"{peer}   paced {len(resp)}B to the PS2 console on :{port} in "
                 f"{-(-len(resp) // chunk)} chunks "
                 f"({time.monotonic() - t0:.2f}s)")


#: WHICH BUILD IS ON THE OTHER END OF *THIS* LOBBY CONNECTION.
#:
#: `handle_lobby` already learns it from the hello -- `magic[+0x09]`, 0xFA = PC
#: Viewer, 0x00 = PS2 Viewer -- and `_lobby_ps2_send` has trusted that byte for
#: send pacing since 2026-09-02. This carries the same byte to the two places
#: that BUILD the reply, because a title's ranking header is the first resource whose
#: LENGTH AND CONTENT both differ between the builds (see the title's PS2 layout):
#: the PC asks for 28 bytes, the PS2 for 24 of the same fields four bytes over.
#:
#: A thread-local, not a parameter, on purpose: `_lobby_paylen` and
#: `_resource_blob` are reached through several call sites between them, and
#: threading a build flag through all of them would put a PS2-shaped argument on
#: every unrelated resource path. One connection is one thread here.
#:
#: WARNING It is only ever SET from a real hello. Anything that reaches these
#: functions off the lobby band (a test, the auth band) reads the default False
#: and gets the PC answer, which is what every path did before this.
_peer_build = threading.local()


def _peer_is_ps2():
    """True when the lobby hello's magic[+0x09] was 0x00 -- NOT a build test.

    WARNING: That byte is OURS. The hello's [8:12] echoes the [4:8] of the record our
    auth hop sent (the "client IP" field, see authtoken._CONST_48), and [+0x09]
    is its byte [6] -- the account-status code we write, 0 by default. So this
    reads back our own POL_ACCT_STATUS and says PS2 for every client, which is
    exactly what was measured on a live server.

    Its three callers all choose a RECORD LAYOUT (TM0RkData length and body,
    the ZL/RL name transforms), and a layout switch must not change on a signal
    that has never been validated for it, in either direction -- so they are
    deliberately LEFT on this. Send pacing, the one payload-neutral use, moved
    to `_lobby_pace_ps2`, which reads the real build from the login NICK.
    """
    return getattr(_peer_build, "ps2", False)


def _lobby_pace_ps2(variant, peer=""):
    """Pace this lobby connection's replies as a PS2 console? (send cadence ONLY)

    The basis is the client signature from the login NICK (`nick_client_sig`,
    PS2 `TTTTT7I...`, PC `TTTTTAI...`), which the auth hop records in the
    session this lobby connection has bound to. The old basis, magic[+0x09]
    (`variant`), is our own status byte echoed back -- 0x00 for everyone -- so
    every PC Viewer was being paced like a console.

    Never removes pacing from a real PS2: when no signature is known for the
    session (unbound connection, a login from before this shipped, an unfamiliar
    prefix) it returns the old answer, which in practice is True.
    POL_PS2_DETECT=magic restores the old basis outright.

    Payload-neutral by construction -- the caller sends identical bytes either
    way -- so this must NOT be reused to choose a record layout. See
    `_peer_is_ps2`.
    """
    old = variant == 0x00
    if os.environ.get("POL_PS2_DETECT", "sig") == "magic":
        return old
    sig = lobbysession._session_get("client_sig")
    if isinstance(sig, str) and sig.startswith("TTTTT7I"):
        verdict, why = True, "PS2 client signature"
    elif isinstance(sig, str) and sig.startswith("TTTTTAI"):
        verdict, why = False, "PC client signature"
    else:
        verdict, why = old, ("no client signature for this session"
                             if not sig else "unfamiliar client signature")
    note = (verdict, sig)
    if getattr(_peer_build, "pace_note", None) != note:
        _peer_build.pace_note = note
        log("lobby", f"{peer} send pacing: {'PS2' if verdict else 'PC'} "
                     f"({why} {sig!r}; magic[+0x09] said "
                     f"{'PS2' if old else 'PC'})")
    return verdict


def _lobby_ps2_send(conn, out, is_ps2, peer=""):
    """conn.sendall(out), rate-paced when the peer is a PS2 console.

    The lobby band (51220) carries the encrypted 3:0 fetches, and a big reply --
    `b/g/RL%03d` is 51 KB, `b/g/PTL` 23 KB -- arrives as one burst that PCSX2's
    DEV9 `Sockets` TCP emulation cannot drain: measured 2026-09-02 in the emulog,
    it desyncs its own ACK numbers ("Sent unexpected acknowledgement number, got
    45610 expected 50828" -> "Bad TCP numbers received" -> "Invalid TCP state"),
    declares the connection dead and RESETS it, so the console never receives a
    clean room/table list and hangs on "getting the room list details". This is
    the SAME failure class as POL-0006 on the portal band, which `_band_send`
    already paces -- this is that fix for the lobby band. Two differences from the
    portal path: the lobby band has no User-Agent header, so the PS2 was identified
    by its hello `magic[+0x09] == 0x00` -- which turned out to be our own status
    byte echoed back, PS2 for everyone; callers now pass `_lobby_pace_ps2()`,
    which reads the login NICK's client signature instead; and it needs TCP_NODELAY, or Nagle recoalesces the metered chunks into the very
    burst DEV9 chokes on. Payload-neutral -- identical bytes, only the send cadence
    changes -- so it cannot affect the content the client parses (Tetra Master
    included, though a PC TM never pace here).

    POL_LOBBY_PS2_PACE=0 falls back to a single sendall. Reuses the band's
    POL_PS2_CHUNK / POL_PS2_CHUNK_DELAY_MS so both are tuned together against the
    emulog `Bad TCP numbers received` oracle (0 = working).
    """
    chunk = int(os.environ.get("POL_PS2_CHUNK", "1400"))
    if (not is_ps2 or chunk <= 0 or len(out) <= chunk
            or os.environ.get("POL_LOBBY_PS2_PACE", "1") != "1"):
        conn.sendall(out)          # PC clients, small replies, or pacing disabled
        return
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    delay = float(os.environ.get("POL_PS2_CHUNK_DELAY_MS", "5")) / 1000.0
    rate = (chunk / delay) if delay > 0 else 0        # bytes/sec, 0 = unthrottled
    t0, sent = time.monotonic(), 0
    for i in range(0, len(out), chunk):
        conn.sendall(out[i:i + chunk])
        sent += len(out[i:i + chunk])
        if not rate or sent >= len(out):
            continue
        due = t0 + sent / rate                        # deadline, not per-chunk sleep
        now = time.monotonic()
        if due > now:
            time.sleep(due - now)
    log("lobby", f"{peer}   paced {len(out)}B to the PS2 console on the lobby band "
                 f"in {-(-len(out) // chunk)} chunks ({time.monotonic() - t0:.2f}s)")
