"""Entry point: modes, ports, and the listener threads."""
import os
import sys
import time
import threading
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import _arm_stack_dumps, expand_ports, install_stderr_capture, load_config, log
from authtoken import load_stamps
from . import authcap, authresume, authserv, directory, lobbyserver, mailserver, patch, posture, pushspool, redirect, serving, worldserver



def main():
    # FIRST, so anything the startup path itself warns about is captured.
    install_stderr_capture()
    if len(sys.argv) < 2:
        raise SystemExit("usage: responders.py <mode[,mode...]>  modes: "
                         "patch directory authcap authserv lobby world "
                         "mail all")
    modes = set(sys.argv[1].split(","))
    # Say which title plugins this process carries; a title that failed to
    # load raised at import, so an absent name here means it was not named.
    log("startup", "titles loaded: " + (", ".join(t.tag.decode("latin1") for t in titles.all()) or "none")
        + (" (" + titles.describe() + ")" if titles.describe() else ""))
    if "all" in modes:
        modes |= {"patch", "directory", "authserv", "lobby", "world", "mail"}
    cfg = load_config()
    # POL_ADVERTISE wins over the config file. config/server.yaml is SHARED with
    # dev, where stub_ip is the dev box's own LAN address -- so in production the
    # env is the only place the client-visible address can be right, and
    # docker-compose.prod.yml already documents POL_ADVERTISE as exactly that
    # ("the address the CLIENT dials"). The override has to land on stub_ip
    # itself, not on POL_AUTH_IP/POL_WORLD_IP: every "now dial this" value the
    # client is handed flows from here through _self_ip() -- the auth redirect
    # token, the POL error token, the lobby handoff and the world fallback -- and
    # those two knobs cover only the last of them, leaving the redirect token
    # pointing at the dev IP. That is a login that authenticates and then hangs.
    stub_ip = os.environ.get("POL_ADVERTISE") or cfg.get("stub_ip", "127.0.0.1")
    authcap._SELF_IP[0] = stub_ip
    # Before anything listens: a client that was already running when this
    # service restarted is still keyed to a token from the previous process.
    # See _STAMP_KEY -- without this it cannot log in and reports POL-0008.
    load_stamps()
    redirect.load_login_nicks()
    srv_name = "ci000.pol.com"

    node_ip = os.environ.get("POL_AUTH_IP") or None   # None = per client (_self_ip)
    node_port = int(os.environ.get("POL_AUTH_PORT", "51241"))
    auth_ports = expand_ports(os.environ.get("POL_AUTH_PORTS", "51241-51250")
                              .split(","))

    def start(port, handler, bind="0.0.0.0"):
        threading.Thread(target=serving.serve, args=(port, handler, bind),
                         daemon=True).start()

    started = []
    if "patch" in modes:
        start(54000, patch.handle_patch)
        started.append("patch:54000")
    if "directory" in modes:
        start(51240, lambda c, a: directory.handle_directory(c, a, node_ip, node_port,
                                                    srv_name))
        started.append(f"directory:51240->{node_ip or 'per-client'}:{node_port}")
    if "authcap" in modes:
        for p in auth_ports:
            start(p, lambda c, a, _p=p: authcap.handle_authcap(c, a, _p, srv_name))
        started.append(f"authcap:{auth_ports[0]}-{auth_ports[-1]}")
    if "authserv" in modes:
        # THE LISTEN OFFSET. With the front relay in place (services/authrelay.py)
        # the RELAY owns the ports the client dials and we sit behind it, so the
        # two processes need different port numbers -- docker-compose.prod.yml
        # runs every service with network_mode: host, where they genuinely
        # cannot share one. Only the LISTEN moves: `_p` stays the client-visible
        # number, because it is what the prefix, the redirect token and the next
        # hop are all built from, and a client told to dial 51441 would dial past
        # the relay straight into us -- which works right up until the restart
        # this whole mechanism exists for. Default 0 = no relay, as before.
        listen_off = int(os.environ.get("POL_AUTH_LISTEN_OFFSET", "0"))
        # Chain each hop to the next port; the last wraps to the first.
        for i, p in enumerate(auth_ports):
            nxt = auth_ports[(i + 1) % len(auth_ports)]
            start(p + listen_off, lambda c, a, _p=p, _n=nxt:
                  authserv.handle_authserv(c, a, _p, srv_name, _n))
        started.append(f"authserv:{auth_ports[0]}-{auth_ports[-1]} (chained"
                       + (f", listening +{listen_off}" if listen_off else "")
                       + ")")
        # THE RESUME DOOR. How the relay hands us back a session whose socket
        # outlived the process that was serving it. Off unless a port is set;
        # bound to localhost by default because a resume presents no password
        # (see handle_authresume) and only the relay has any business dialling
        # it. Under the DEV compose the relay is a different container, so that
        # default has to be widened to the docker network there.
        resume_port = int(os.environ.get("POL_AUTH_RESUME_PORT", "0"))
        if resume_port:
            start(resume_port, lambda c, a: authresume.handle_authresume(c, a, srv_name),
                  bind=os.environ.get("POL_AUTH_RESUME_BIND", "127.0.0.1"))
            started.append(f"authresume:{resume_port}")
        # THE HALF-DEPLOY GUARD. With an offset set, the ports the client dials
        # are not ours -- so a relay that is not running is an outage with no
        # log line anywhere. See _front_relay_watchdog for the five minutes on
        # prod that this exists because of. "local" when the relay shares our
        # network namespace (prod: network_mode host), "<host>[:<port>]" when it
        # does not. The DEV compose leaves it off deliberately: the relay is a
        # separate container there, so a local bind-probe would alarm forever.
        front = os.environ.get("POL_AUTH_FRONT_CHECK", "off")
        if listen_off and front != "off":
            threading.Thread(target=posture._front_relay_watchdog,
                             args=(front, node_port), daemon=True).start()
            started.append(f"front-check:{front}")
        elif listen_off:
            log("resp", f"NOTE: listening +{listen_off}, so :{node_port} (the port "
                        "the client dials) is NOT bound here. If the front relay is "
                        "not running, the auth band is CLOSED and nothing will "
                        "report it -- set POL_AUTH_FRONT_CHECK=local to be told.")
        # Presence bring-up: on-demand push trigger (inert unless the fire file is
        # written). Lets the field sweep fire pushes without a real login/logout.
        threading.Thread(target=pushspool._presence_fire_watcher, daemon=True).start()
        # A title change is a presence change, and only THIS process can push it.
        _arm_stack_dumps()
        threading.Thread(target=pushspool._title_zone_watcher, daemon=True).start()
        # The 4:5 STATUS watcher, started beside the zone one because it is the
        # same mechanism over a sibling file -- see `_member_status_watcher`.
        threading.Thread(target=pushspool._member_status_watcher, daemon=True).start()
        # Same idea for the friend-ROW push, and for a sharper reason: pushing
        # one at login costs a POL-5135 and a relogin when it is wrong.
        threading.Thread(target=pushspool._row_fire_watcher, daemon=True).start()
        # THIS is the process that holds the session channels, so this is the
        # process that delivers pushes -- and the one that drains the spool the
        # lobby (a different container) writes its events into.
        pushspool._PUSH_LOCAL[0] = True
        threading.Thread(target=pushspool._push_spool_watcher, daemon=True).start()
        started.append("push:spool-drain")
    if "lobby" in modes:
        # The hop after auth. authserv hands the client here (POL_LOBBY_PORT); we
        # capture + attempt-decrypt (K=0 via token0) and, with POL_LOBBY_EMIT=1,
        # answer with our content-list (FFXI + Tetra Master) and world handoff.
        lobby_ports = expand_ports(
            os.environ.get("POL_LOBBY_PORTS",
                           os.environ.get("POL_LOBBY_PORT", "51220")).split(","))
        for p in lobby_ports:
            start(p, lambda c, a, _p=p: lobbyserver.handle_lobby(c, a, _p, stub_ip))
        started.append(f"lobby:{lobby_ports[0]}-{lobby_ports[-1]}")
    if "world" in modes:
        # The endpoint the lobby hands off to (Tetra Master). Capture harness: the
        # TM protocol is not reversed, so it records the client's world opener and
        # attempts a K=0 decrypt; it does not answer. Give the lobby handoff (and
        # POL_WORLD_IP/PORT) a live endpoint so the client connects here.
        world_ports = expand_ports(
            os.environ.get("POL_WORLD_PORTS",
                           os.environ.get("POL_WORLD_PORT", "51330")).split(","))
        for p in world_ports:
            start(p, lambda c, a, _p=p: worldserver.handle_world(c, a, _p, stub_ip))
        started.append(f"world:{world_ports[0]}-{world_ports[-1]}")
    if "mail" in modes:
        # po000.pol.com = POP3, ma000.pol.com = SMTP -- the hosts the Viewer's own
        # mail wizard names. Both need adding to config redirect_only, which is a
        # WHITELIST: they ship commented out, so they resolve to real (dead) SE.
        #
        # TWO port pairs, because the Viewer has two kinds of mail account and
        # they do NOT use the same ports (app.dll, static RE 2026-08-12):
        #
        #   generic/ISP account   POP 110    SMTP 25    (app.dll+0x134c9b/0x134c91)
        #   PLAYONLINE account    POP 51260  SMTP 51261 (0xc83c / 0xc83d)
        #
        # The PlayOnline preset is the one the client fills in for itself -- three
        # code paths (app.dll+0x134fca, +0x13c44f, +0x13c7da) all do
        #   swprintf("po%03d.pol.com", rec[2]); acct->pop_port  = 0xc83c
        #   swprintf("ma%03d.pol.com", rec[2]); acct->smtp_port = 0xc83d
        # so PlayOnline Mail rides the same 512xx band as the rest of PoL, NOT the
        # IANA ports. Serving only 110/25 means a PlayOnline-type account can never
        # connect; it lands on whatever else owns 51260/51261 (the lobby handler,
        # which waits for the client to speak first, so a POP3 client that is
        # waiting for OUR greeting deadlocks until it times out).
        pop_ports = expand_ports(
            os.environ.get("POL_POP3_PORTS",
                           os.environ.get("POL_POP3_PORT", "110,51260")).split(","))
        smtp_ports = expand_ports(
            os.environ.get("POL_SMTP_PORTS",
                           os.environ.get("POL_SMTP_PORT", "25,51261")).split(","))
        for p in pop_ports:
            start(p, lambda c, a, _p=p: mailserver.handle_pop3(c, a, _p))
        for p in smtp_ports:
            start(p, lambda c, a, _p=p: mailserver.handle_smtp(c, a, _p))
        started.append("mail:pop3=" + ",".join(map(str, pop_ports))
                       + " smtp=" + ",".join(map(str, smtp_ports)))
    if not started:
        raise SystemExit(f"unknown modes {modes!r}")
    log("resp", "started " + ", ".join(started))
    posture._log_auth_posture()

    # WARNING: GRACEFUL SHUTDOWN (2026-09-04). A container stop -- every deploy
    # restarts authsess -- used to just SIGKILL the process, and a game in
    # progress froze the console on a socket that vanished under it. Now
    # SIGTERM tells every title to begin its shutdown and holds the process a
    # few seconds while a title reports live games: the in-game handler
    # answers each live table's next line (the client re-acks ~every 2 s)
    # with its end-of-game record, so the player drops cleanly to the menu
    # instead of hanging. Docker's default stop grace is 10 s, so the hold
    # fits inside it. Only the authsess process has games; elsewhere `live`
    # is 0 and the hold is skipped, so login/mail restarts stay instant.
    def _graceful_shutdown(signum, _frame):
        log("resp", "SIGTERM -- graceful shutdown")
        try:
            titles.begin_shutdown()
            live = titles.live_games()
            if live:
                grace = float(os.environ.get("POL_SHUTDOWN_GRACE", "4"))
                log("resp", f"holding {grace:.0f}s to end {live} live "
                            f"game(s) before exit")
                time.sleep(grace)
        except Exception as e:
            log("resp", f"graceful-shutdown hook failed: {e!r}")
        sys.exit(0)

    try:
        import signal as _signal
        _signal.signal(_signal.SIGTERM, _graceful_shutdown)
    except (ValueError, OSError, AttributeError) as e:
        log("resp", f"could not install SIGTERM handler: {e!r}")

    # DEPLOY GATE for live games. The auth band is where a title's games
    # actually play, so it is THIS process that a deploy must not bounce
    # mid-game. Publish a live-games marker; a deploy script reads it and
    # DEFERS the authsess restart while a game is in progress. Only authserv
    # holds games, so only it writes the marker.
    if "authserv" in modes and titles.loaded():
        try:
            import live_sessions
            live_sessions.start_heartbeat("authsess-titles", titles.live_games)
            log("resp", "live-game deploy-gate heartbeat started")
        except Exception as e:
            log("resp", f"live-game heartbeat not started: {e!r}")

    # Block forever.
    while True:
        try:
            threading.Event().wait(3600)
        except KeyboardInterrupt:
            break
