"""Auth posture logging and the front-relay watchdog."""
import os
import socket
import time
from srvcore import log



def _log_auth_posture():
    """State, at startup, what this server checks -- because it checks very little.

    None of this is new behaviour; what is new is that it is SAID. The switches
    have existed and defaulted to off for months, so "anyone who knows a handle
    can log in as them" was true, documented in three separate places, and
    invisible to anyone actually running the thing. An operator should not have
    to read the source to find out that the front door is open.
    """
    enforce = os.environ.get("POL_ACCOUNTS_ENFORCE", "0") == "1"
    mail_strict = os.environ.get("POL_MAIL_STRICT", "0") == "1"
    if enforce and mail_strict:
        log("resp", "auth posture: accounts ENFORCED (unknown NICKs rejected), "
                    "mail APOP verified")
        return
    open_bits = []
    if not enforce:
        open_bits.append(
            "any NICK logs in -- an unknown one is auto-provisioned with content "
            "grants, and a known HANDLE logs in AS that account (no password is "
            "checked anywhere on this path: the client's digest is not one we can "
            "yet derive). POL_ACCOUNTS_ENFORCE=1 rejects unknown NICKs")
    if not mail_strict and os.environ.get("POL_MAIL_SESSION_CHECK", "1") != "1":
        open_bits.append(
            "any POP3 login reads the mailbox it names -- POL_MAIL_SESSION_CHECK "
            "is OFF, so an unverifiable login is simply accepted. Turn it on")
    elif not mail_strict:
        open_bits.append(
            "a POP3 login for an account with NO mail password falls back to "
            "'the dialling address holds that member's live session' (the "
            "account password is tried first). POL_MAIL_STRICT=1 refuses those "
            "outright instead, once every account has a mail password")
    log("resp", "auth posture: THIS SERVER IS OPEN -- " + "; ".join(open_bits)
                + ". Fine on a private LAN; not something to expose.")


def _front_is_served(spec, port):
    """Is SOMETHING serving the port the client actually dials?

    `spec` is "local" when the front relay shares our network namespace
    (docker-compose.prod.yml runs everything with network_mode: host), or
    "<host>[:<port>]" when it does not.

    The local form BINDS rather than connects: a successful bind means nobody is
    listening, which is the alarm condition, and it costs the relay nothing --
    a connect probe would make it open an upstream connection and log a client
    hang-up on every interval, forever.
    """
    if spec == "local":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))     # succeeded => nothing is listening
            return False
        except OSError:
            return True
        finally:
            s.close()
    host, _, p = spec.partition(":")
    try:
        socket.create_connection((host, int(p) if p else port), timeout=3).close()
        return True
    except OSError:
        return False


def _front_relay_watchdog(spec, port):
    """Shout if the auth band is CLOSED because nothing is fronting it.

    With POL_AUTH_LISTEN_OFFSET set we deliberately do not bind the ports the
    client dials -- services/authrelay.py does. If the relay is not running, the
    band is simply shut, and nothing here would say so: our own listeners are
    healthy, the log looks entirely normal, and every login fails at connect
    with no server-side trace at all.

    That is not hypothetical. On the production box, 2026-08-15, `authsess` was
    restarted with the offset while the relay container did not yet exist, and
    the band stayed closed for about five minutes with nothing reporting it.
    An outage that leaves no log line is the expensive kind.
    """
    time.sleep(int(os.environ.get("POL_AUTH_FRONT_GRACE", "20")))
    every = int(os.environ.get("POL_AUTH_FRONT_EVERY", "60"))
    was_ok = None
    while True:
        ok = _front_is_served(spec, port)
        if ok and was_ok is not True:
            log("resp", f"front check: :{port} is being served (the relay is up)")
        elif not ok:
            # Repeated on purpose, every interval, for as long as it lasts.
            log("resp", f"*** AUTH BAND CLOSED *** nothing is listening on :{port}, "
                        f"the port the client dials. We are behind a front relay "
                        f"(POL_AUTH_LISTEN_OFFSET) and it is NOT RUNNING, so every "
                        f"login fails at connect and leaves no other trace. Start it: "
                        f"docker compose up -d authrelay")
        was_ok = ok
        time.sleep(every)
