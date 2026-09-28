"""The auth node responder: line I/O, account resolution, the login exchange."""
import datetime
import hashlib
import os
import socket
import time
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import _LOGIN_TRACE, _trace, _trace_begin, _trace_done, _trace_dump, bind_peer, hexdump, log, save_capture
from authtoken import _STAMPS, _STAMPS_LOCK, _stamps_refresh, build_redirect_token, build_session_token, remember_stamp, session_token_key
import sessioncrypt
from .deps import accounts, contentlist
from . import contentprofiles, authcap, authnode, authresume, chatsession, friendroster, ircband, lobbybind, lobbysession, logingate, presence, pushrecord, redirect, roomregistry


# POL's Blowfish lives in sessioncrypt.py (bf_setkey / ofb_apply / recover_iv).


def _read_for(conn, seconds, want_crlf=False, minlen=0):
    """Accumulate bytes for up to `seconds`; stop early once a CRLF is seen (if
    want_crlf) and we have at least minlen bytes.

    WARNING: **DO NOT REPLY OUT OF THIS READER WITHOUT A COMPLETENESS PREDICATE.** With
    `want_crlf=False` its only exit is the timeout, so a peer that waits for an
    answer waits the whole `seconds`. That is the bug `_lobby_frame_complete`
    was written for -- it cost every lobby message ~1.0 s -- and this reader has
    it too. It is harmless TODAY only because its one caller (`handle_world`) is
    capture-only and never answers: `POL_WORLD_EMIT` explicitly refuses to send.
    The moment the world band starts replying, give it a length predicate the way
    the lobby has one.

    A 2026-08-19 sweep of every other socket reader in `services/` found none
    with this shape: the patch server and the game worlds all frame on a
    declared length or on a line terminator, POP3/SMTP block in `readline`, and
    `stub.handle_tcp` is capture-only by design."""
    conn.settimeout(1.0)
    buf = b""
    waited = 0.0
    while waited < seconds:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            waited += 1.0
            if buf and want_crlf and b"\r\n" in buf and len(buf) >= minlen:
                break
            continue
        if not chunk:
            break
        buf += chunk
        if want_crlf and b"\r\n" in buf and len(buf) >= minlen:
            # small grace read to catch a trailing binary line
            conn.settimeout(0.6)
            try:
                while True:
                    more = conn.recv(4096)
                    if not more:
                        break
                    buf += more
            except socket.timeout:
                pass
            break
    return buf


def _recv_line(conn, timeout=20):
    """Read bytes up to and including the next CRLF (ciphertext keeps real CRLFs)."""
    conn.settimeout(1.0)
    buf = b""
    waited = 0.0
    while waited < timeout:
        i = buf.find(b"\r\n")
        if i >= 0:
            return buf[:i], buf[i+2:]
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            waited += 1.0
            continue
        if not chunk:
            break
        buf += chunk
    return buf, b""


def nick_credential(blob):
    """The stable password token out of the NICK line's third field.

    Layout (decoded live 2026-08-13, two logins of one account): a 36-char blob
    of  head[8] + pad[13] + TOKEN[11] + nonce[4].  Only the 11-char TOKEN is a
    function of the password -- it is identical across sessions for one password
    and differs for another -- so it is the thing to store and compare. The rest
    is a constant header/pad and a per-session nonce (the nonce is why the 32-hex
    middle digest changes every login and is NOT usable as a stored credential).

    Returns the token as str, or None if the blob is the wrong shape. Deriving
    TOKEN from the plaintext password is a separate, still-open problem; the
    comparison here needs neither that nor the derivation of the middle digest.
    """
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("latin-1", "replace")
    blob = blob.strip()
    # Offsets are fixed; guard the length so a malformed line just yields None
    # rather than a misaligned slice that would silently never match.
    if len(blob) < 32:
        return None
    tok = blob[21:32]
    return tok or None


#: Length of the NICK blob's fixed head+pad -- the client-build signature.
NICK_CLIENT_SIG_LEN = 21


def nick_client_sig(blob):
    """The NICK blob's head[8] + pad[13] -- WHICH CLIENT BUILD is logging in.

    nick_credential() calls this region "a constant header/pad", which is true
    for one client and false across clients. Measured 2026-08-24 over every
    login in authserv.log:

        TTTTTAISTTTTTTTTTTTTT   PC Viewer   (also ...AIT..., same account token)
        TTTTT7ITTTGaItbIQ8nHA   PS2 Viewer

    The same account presents a DIFFERENT 11-char token under each, so the token
    is a function of (account credential x client build) and the TOFU check has
    to be scoped by this. What the individual bytes mean is NOT decoded -- this
    is a discriminator, not a parse, so it is stored verbatim.

    Returns the 21-char signature as str, or None if the blob is malformed.
    """
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("latin-1", "replace")
    blob = blob.strip()
    if len(blob) < 32:
        return None
    return blob[:NICK_CLIENT_SIG_LEN] or None


def _warn_stranded_registration(db, nick, peer_ip):
    """Say so, loudly, when an unknown login NICK arrives while a registered
    account has never been logged into.

    The client DERIVES the nick it sends from the PlayOnline ID typed under Add
    Member -- and differently per client (one account, `UDXS6FWXX` from the US
    Viewer, `UUA9T2OZX` from
    PolFL). We cannot compute it, so a freshly registered account cannot be
    recognised on its FIRST login: with enforcement off it is silently
    auto-provisioned as a NEW empty account, and the registration -- handle,
    content grants, redeemed code -- is stranded.

    This costs one small query on the auto-provision path only (never on a normal
    login) and turns that silence into the exact command that repairs it."""
    try:
        rows = db.execute(
            "SELECT login_name, polid FROM member WHERE login_token IS NULL"
            " AND login_name != ? ORDER BY id DESC LIMIT 5", (nick,)).fetchall()
    except Exception:
        return                                  # a diagnostic is never fatal
    if not rows:
        return
    log("accounts", f"{peer_ip} *** {nick!r} is unknown, and "
                    f"{len(rows)} registered account(s) have never logged in. "
                    "If this login IS one of them, the client derived a nick we "
                    "cannot predict -- join them up with: "
                    + " | ".join(f"accounts.py DB alias {nick} {r['login_name']}"
                                 for r in rows))


def login_digest_salts(peer_ip, sent_greetings):
    """Greeting tokens the NICK digest may be salted with, likeliest first.

    This hop's own greetings (newest first), then every session token we issued
    this address -- the IV search already knows a client can key from an older
    dial's token (key_candidates), and it salts from the same one. md5 is cheap,
    so there is no cap; a false match needs a 128-bit collision.
    """
    out = []
    for g in reversed(sent_greetings or []):
        if g not in out:
            out.append(g)
    _stamps_refresh()
    with _STAMPS_LOCK:
        history = list(_STAMPS.get(peer_ip, []))
    for old, _at in reversed(history):
        g = build_session_token(old)
        if g not in out:
            out.append(g)
    return out


def _lockout_policy():
    """(fails, window_s) from POL_LOGIN_LOCKOUT_FAILS / _WINDOW_S; fails 0 = off."""
    try:
        fails = int(os.environ.get("POL_LOGIN_LOCKOUT_FAILS", "5"))
    except ValueError:
        fails = 5
    try:
        window = float(os.environ.get("POL_LOGIN_LOCKOUT_WINDOW_S", "900"))
    except ValueError:
        window = 900.0
    return (fails, window) if window > 0 else (0, window)


def resolve_account(nick, peer_ip, iv, lobby_port=None, cred=None,
                    client_sig=None, digest=None, salts=()):
    """Resolve the login NICK to an account row, or None.

    Permissive by default: an unknown NICK is auto-provisioned (accounts
    .ensure_member), so the login chain behaves exactly as it did before the
    account DB existed. Set POL_ACCOUNTS_ENFORCE=1 to reject unknown nicks
    instead -- that is the switch to flip once a registration flow can create
    accounts, and it is what makes the games menu account-driven rather than
    environment-driven.

    `cred` is the stable password token from nick_credential(). Password check
    is trust-on-first-use: the first login for a member RECORDS its token; every
    later login must PRESENT the same one. A mismatch is a wrong password and is
    rejected whenever enforcement is on (POL_ACCOUNTS_ENFORCE=1, or the narrower
    POL_ACCOUNTS_ENFORCE_PW=1 to check passwords without also gating unknown
    nicks). With enforcement off it is logged but allowed, so the token can be
    seeded across a fleet of existing accounts before the gate goes live.

    `digest` is the NICK line's 32-hex field, md5(greeting[:40] + password),
    and `salts` the greetings it may be salted with (login_digest_salts). When
    the account holds a sealed password copy this is the REAL password check
    (without it, any password logs in). A wrong digest is refused
    only on a client build that has already matched once (login_digest_client),
    so a build that salts differently degrades to the token check instead of
    locking its players out. POL_LOGIN_DIGEST=0 turns the check off;
    POL_LOGIN_DIGEST_ENFORCE=all refuses on unproven builds too, =off never
    refuses (log only).

    Returns (conn, member_row, reject) -- `reject` is None when nothing was
    refused, else the SE status byte to send back (REJECT_UNKNOWN_ID /
    REJECT_BAD_PASSWORD). The caller owns closing conn.

    THE THIRD VALUE IS NOT COSMETIC. `member is None` on its own does not mean
    "refused": it is also what a stateless run returns (no account DB, or
    POL_ACCOUNTS=0), which must still be allowed to log in. Only a real refusal
    sets `reject`, so the caller can tell the two apart -- and that distinction is
    what stopped POL_ACCOUNTS_ENFORCE_PW=1 from silently admitting a wrong
    password (see the caller).
    """
    if accounts is None or os.environ.get("POL_ACCOUNTS", "1") != "1":
        return None, None, None
    if isinstance(nick, (bytes, bytearray)):
        nick = nick.decode("ascii", "replace")
    enforce = os.environ.get("POL_ACCOUNTS_ENFORCE", "0") == "1"
    enforce_pw = enforce or os.environ.get("POL_ACCOUNTS_ENFORCE_PW", "0") == "1"
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                             accounts.DEFAULT_DB))
        accounts.purge_sessions(db)
        member = (accounts.member_by_handle(db, nick) or
                  accounts.get_member(db, nick) or
                  accounts.member_by_alias(db, nick))
        if member is None:
            if enforce:
                log("accounts", f"{peer_ip} REJECT unknown nick {nick!r} "
                                "(POL_ACCOUNTS_ENFORCE=1)")
                _warn_stranded_registration(db, nick, peer_ip)
                db.close()
                return None, None, logingate.REJECT_UNKNOWN_ID
            member = accounts.ensure_member(db, nick)
            log("accounts", f"{peer_ip} auto-provisioned {nick!r} "
                            f"(member id={member['id']}, polid={member['polid']})")
            _warn_stranded_registration(db, nick, peer_ip)
        if member["status"] != "active":
            log("accounts", f"{peer_ip} REJECT {nick!r}: member status="
                            f"{member['status']}")
            db.close()
            # No capture of SE refusing a SUSPENDED account, so this reuses the
            # unknown-ID byte rather than inventing one. Same effect for the
            # player -- this ID cannot log in -- and it is honest about what we
            # actually observed.
            return None, None, logingate.REJECT_UNKNOWN_ID
        # LOCKED OUT? After POL_LOGIN_LOCKOUT_FAILS (5) passwords the NICK digest
        # REFUSED within POL_LOGIN_LOCKOUT_WINDOW_S (900 s), every login of this
        # member is refused with SE's 0xCB -- the right password too, which is
        # the point of a lockout -- until enough of those failures age out of
        # the window. Counted in accounts.db (login_fail), so authsess and the
        # login container agree. `accounts.py <db> unlock <polid|login>` ends
        # it early; POL_LOGIN_LOCKOUT_FAILS=0 turns it off.
        lock_fails, lock_window = _lockout_policy()
        if lock_fails > 0:
            try:
                left = accounts.login_lock_remaining(db, member["id"],
                                                     lock_fails, lock_window)
            except Exception as exc:        # a lockout fault must not refuse
                log("accounts", f"{peer_ip} lockout check failed ({exc!r}) -- "
                                "not enforced for this login")
                left = 0
            if left > 0:
                log("accounts", f"{peer_ip} REJECT {nick!r}: LOCKED OUT after "
                                f"{lock_fails} failed logins in {lock_window}s "
                                f"({int(left)}s left; `accounts.py unlock "
                                f"{member['polid']}` clears it)")
                db.close()
                return None, None, logingate.REJECT_LOCKED
        # THE PASSWORD CHECK: the NICK digest against the sealed password copy.
        pw_proven = False
        if digest and os.environ.get("POL_LOGIN_DIGEST", "1") == "1":
            try:
                pw = accounts.get_login_password(db, member["id"])
            except Exception as exc:         # a key fault must not unseat the login
                log("accounts", f"{peer_ip} {nick!r}: password copy unreadable "
                                f"({exc!r}) -- token check only")
                pw = None
            mode = os.environ.get("POL_LOGIN_DIGEST_ENFORCE", "proven")
            if pw is None:
                log("accounts", f"{peer_ip} {nick!r}: no password copy held, so "
                                "the typed password is NOT checked (token only) -- "
                                "a servlet sign-in or password change stores it")
            elif accounts.login_digest_ok(digest, salts, pw) is not None:
                pw_proven = True
                if lock_fails > 0:
                    try:
                        if accounts.clear_login_failures(db, member["id"]):
                            log("accounts", f"{peer_ip} {nick!r}: right password "
                                            "-- earlier failed logins forgotten")
                    except Exception as exc:
                        log("accounts", f"{peer_ip} could not clear failed "
                                        f"logins ({exc!r})")
                if accounts.prove_digest_client(db, client_sig, member["id"]):
                    log("accounts", f"{peer_ip} NICK digest formula PROVEN for "
                                    f"client build {client_sig!r} -- wrong "
                                    "passwords from it are refused from now on")
                log("accounts", f"{peer_ip} {nick!r}: password verified (NICK "
                                "digest)")
            elif mode != "off" and (
                    mode == "all" or accounts.digest_client_proven(db, client_sig)):
                log("accounts", f"{peer_ip} REJECT {nick!r}: wrong password "
                                f"(NICK digest, client {client_sig!r})")
                # The ONLY place a failure is counted: a refusal the digest
                # itself made. Token mismatches and unproven builds never get
                # here, so a build that salts differently cannot lock anyone out
                # (nor can POL_LOGIN_DIGEST_ENFORCE=all on an unproven one).
                if lock_fails > 0 and accounts.digest_client_proven(db,
                                                                    client_sig):
                    try:
                        accounts.record_login_failure(db, member["id"], peer_ip,
                                                      client_sig)
                        n = accounts.login_failures(db, member["id"],
                                                    lock_window)
                        if n >= lock_fails:
                            log("accounts", f"{peer_ip} *** {nick!r} LOCKED OUT: "
                                            f"{n} failed logins in "
                                            f"{lock_window}s -- refused with "
                                            "0xCB until they age out ***")
                    except Exception as exc:
                        log("accounts", f"{peer_ip} could not count the failed "
                                        f"login ({exc!r})")
                db.close()
                return None, None, logingate.REJECT_BAD_PASSWORD
            else:
                log("accounts", f"{peer_ip} WARN {nick!r}: NICK digest did not "
                                f"match over {len(salts)} salt(s) and client "
                                f"build {client_sig!r} is not proven "
                                f"(POL_LOGIN_DIGEST_ENFORCE={mode}) -- falling "
                                "back to the token check. Wrong password, or "
                                "this build salts differently.")
        # PASSWORD CHECK (trust-on-first-use on the stable NICK token).
        #
        # SCOPED BY CLIENT BUILD. The token is a function of (account x client),
        # not of the account alone -- the PS2 Viewer presents a different token
        # than the PC for the SAME account (nick_client_sig()). A single slot
        # therefore made every account single-platform: whichever client logged
        # in first owned it, and the other was refused 0xCA for ever. Measured
        # 2026-08-24 on "Lex"/ABCD1234. So TOFU per (member, client_sig), and
        # fall back to the legacy member-wide slot when we cannot tell the
        # client apart (malformed blob, or an old row with no per-client copy).
        #
        # POL_ACCOUNTS_TOKEN_SCOPE=member restores the old member-wide behaviour
        # if the looser seeding ever needs tightening back.
        scoped = (os.environ.get("POL_ACCOUNTS_TOKEN_SCOPE", "client")
                  == "client") and bool(client_sig)
        if cred:
            legacy = accounts.get_login_token(db, member["id"])
            if scoped:
                stored = accounts.get_client_token(db, member["id"], client_sig)
                if stored is None and legacy is not None and cred == legacy:
                    # First login of the client that seeded the old slot: adopt
                    # it here so the migration does not re-TOFU a known-good one.
                    accounts.set_client_token(db, member["id"], client_sig, cred)
                    stored = cred
            else:
                stored = legacy
            if stored is None:
                # WARNING: BINDING IS A PASSWORD-FREE ADMISSION, so it is gated.
                #
                # Trust-on-first-use cannot check anything: the token is a
                # function of the password we have not reversed. On a public
                # server that means an account nobody has played yet can be
                # taken by whoever knows its POL ID -- so the account has to be
                # ARMED, and what arms it (registering, or an operator running
                # `accounts.py arm`) follows a real password proof.
                #
                # A SECOND CLIENT on an account that already holds tokens is a
                # different risk with a different cost: the signature changes
                # per machine and per build (one account can hold several), so
                # gating it would refuse people who are simply on a new PC.
                # It is therefore allowed by default and only logged;
                # POL_TOKEN_ARM_CLIENTS=1 gates it too.
                first_ever = legacy is None and not accounts.list_client_tokens(
                    db, member["id"])
                gate = os.environ.get("POL_TOKEN_ARM", "1") == "1" and (
                    first_ever or
                    os.environ.get("POL_TOKEN_ARM_CLIENTS", "0") == "1")
                if (gate and not pw_proven
                        and not accounts.login_token_armed(db, member["id"])):
                    log("accounts",
                        f"{peer_ip} REJECT {nick!r}: this account is not armed "
                        f"to bind a login token"
                        + (" (it has never been played)" if first_ever else
                           f" for a new client {client_sig!r}")
                        + ". Run `accounts.py arm` to arm it for a while. "
                        "POL_TOKEN_ARM=0 disables this.")
                    db.close()
                    return None, None, logingate.REJECT_BAD_PASSWORD
                if scoped:
                    accounts.set_client_token(db, member["id"], client_sig, cred)
                    known = accounts.list_client_tokens(db, member["id"])
                    log("accounts",
                        f"{peer_ip} recorded login token for {nick!r} from a "
                        f"new client {client_sig!r} (trust-on-first-use; "
                        f"{len(known)} client(s) known for this account)")
                if legacy is None:
                    accounts.set_login_token(db, member["id"], cred)
                    if not scoped:
                        log("accounts",
                            f"{peer_ip} recorded login token for {nick!r} "
                            f"(first login -- trust-on-first-use)")
            elif stored != cred and pw_proven and scoped:
                # The password was checked for real, so a moved token is not a
                # wrong password -- it is the console and PCSX2 sharing one
                # signature, or a token that moved with the network path.
                accounts.set_client_token(db, member["id"], client_sig, cred)
                log("accounts", f"{peer_ip} {nick!r}: login token moved for "
                                f"client {client_sig!r}; re-bound (password "
                                "verified)")
            elif stored != cred:
                log("accounts", f"{peer_ip} {'REJECT' if enforce_pw else 'WARN'} "
                                f"{nick!r}: wrong password (token mismatch"
                                + (f", client {client_sig!r}" if scoped else "")
                                + ")")
                if enforce_pw:
                    db.close()
                    return None, None, logingate.REJECT_BAD_PASSWORD
            elif scoped:
                accounts.touch_client_token(db, member["id"], client_sig)
        elif enforce_pw:
            log("accounts", f"{peer_ip} REJECT {nick!r}: no credential presented "
                            "(POL_ACCOUNTS_ENFORCE_PW=1)")
            db.close()
            return None, None, logingate.REJECT_BAD_PASSWORD
        tok = accounts.open_session(db, member["id"], nick=nick,
                                    peer_ip=peer_ip, iv=iv,
                                    lobby_port=lobby_port)
        try:
            prev, out = accounts.record_login(db, member["id"])
            log("accounts", f"{peer_ip} login clock: previous login={prev}, "
                            f"last logout={out}")
        except Exception as exc:               # a clock is never worth a login
            log("accounts", f"login clock not stamped ({exc!r})")
        log("accounts", f"{peer_ip} login {nick!r} polid={member['polid']} "
                        f"contents={accounts.content_ids(db, member['id'])} "
                        f"session={tok[:8]}...")
        return db, member, None
    except Exception as exc:                      # never let the DB break login
        # LOUD. "Continuing stateless" is the right call -- a login that half
        # works beats one that does not happen -- but it is not a minor event:
        # the client gets a session with no account behind it, so no games menu,
        # no handle, and a lobby with nothing to serve. It read like an ordinary
        # line in the log while a PS2 login was visibly broken on 2026-08-13.
        log("accounts", f"{peer_ip} *** ACCOUNT LOOKUP FAILED ({exc!r}) -- this "
                        f"login continues WITHOUT AN ACCOUNT: no games menu, no "
                        f"handle, nothing for the lobby to serve. The client "
                        f"will look broken. ***")
        # reject=None deliberately: a DB fault is OURS, not the player's, and the
        # comment above is the whole argument -- a half-working login beats a
        # refusal. Do not "helpfully" refuse here.
        return None, None, None


def _front_preamble_addr(conn, addr):
    """Read the relay's `CLIENT <ip> <port>` line and return the real address.

    Only spoken when we sit behind the front relay, which is exactly when
    POL_AUTH_FRONT_PREAMBLE is on -- the two ship together. The relay sends it
    the instant it connects and BEFORE it forwards a single client byte, so it
    is always the first thing on the socket and there is nothing to disambiguate.

    Degrading is safe in both directions: no preamble means we keep the socket's
    own address (one bucket for everyone, i.e. exactly the behaviour that caused
    the outage -- so it is logged, loudly, rather than passed over), and a client
    that somehow reaches this port directly simply never matches the shape and
    keeps its own address too. Nothing is consumed in that case: we peek, and
    only consume the bytes if they ARE a preamble.
    """
    if os.environ.get("POL_AUTH_FRONT_PREAMBLE", "0") != "1":
        return addr
    # PEEK, do not consume, and keep peeking until the line is whole: a 30-byte
    # preamble arrives in one segment in practice, but "in practice" is not a
    # framing rule, and consuming a partial line we then reject would eat the
    # client's first bytes.
    deadline = time.time() + float(os.environ.get("POL_AUTH_PREAMBLE_WAIT", "5"))
    peeked = b""
    try:
        while time.time() < deadline:
            conn.settimeout(max(0.1, deadline - time.time()))
            peeked = conn.recv(64, socket.MSG_PEEK)
            if not peeked or b"\r\n" in peeked or not peeked.startswith(
                    b"CLIENT "[:len(peeked)]):
                break
    except (socket.timeout, OSError):
        pass
    finally:
        try:
            conn.settimeout(None)
        except OSError:
            pass
    if not peeked.startswith(b"CLIENT ") or b"\r\n" not in peeked:
        log("authserv", f"{addr[0]}:{addr[1]} expected the relay's CLIENT preamble "
                        "and did not get one -- every client will share this "
                        "address, and a stale-key login will fail. Is this "
                        "connection bypassing the relay?")
        return addr
    line = peeked.split(b"\r\n", 1)[0]
    try:                                   # consume exactly the preamble
        conn.recv(len(line) + 2)
    except OSError:
        return addr
    parts = line.decode("latin-1", "replace").split()
    ip = parts[1] if len(parts) > 1 else ""
    port = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (ip, port) if ip else addr


def auth_rsa_enabled(peer_ip):
    """Whether this login gets the RSA-wrapped session key (POL_AUTH_RSA).

    POL_AUTH_RSA=1 turns it on for everyone. POL_AUTH_RSA_IPS (comma-separated
    client addresses, as the auth hop sees them after the relay preamble) turns
    it on for those addresses only -- the way to prove it on prod with one
    tester's clients while every other player keeps K=0.
    """
    if os.environ.get("POL_AUTH_RSA", "0") == "1":
        return True
    ips = [s.strip() for s in os.environ.get("POL_AUTH_RSA_IPS", "").split(",")
           if s.strip()]
    return bool(peer_ip) and peer_ip in ips


def handle_authserv(conn, addr, port, srv_name, next_port):
    """Our OWN auth node, using the recovered session cipher (K=0 via token0).
        S: :prefix 300 * <redirect>       (greet, cleartext)
        C: USER x 8 * :<token>            (register, cleartext)
        S: :prefix 300 * <TOKEN0>         (zero token -> client keys K=0)
        C: <NICK, encrypted K=0>          (we decrypt live)
        S: <encrypted K=0 accept>         (welcome + lobby handoff)
    """
    # Bind this thread to a session of its OWN. Provisional (one id per
    # connection) until something on the wire names the launch: the USER token on
    # the auth band, a validating IV on the lobby band. Never the address -- two
    # clients behind the Docker bridge share one, and sharing a slot is how the
    # second was served the first's account.
    # WHOSE CONNECTION IS THIS, REALLY. Behind the front relay every client
    # reaches us from the RELAY's address, so without this every player is one
    # address here -- and the session-token history (_STAMPS) is filed per
    # address. That cost a real outage on 2026-08-16, hours after the relay went
    # in: a Viewer that had been running since before the deploy held a key from
    # a token filed under the OLD address, the lookup searched the relay's
    # instead, ground through every wrong candidate for two minutes and dropped
    # the socket. The client retried forever, stuck on "Connecting to
    # PlayOnline". The relay now says who it is carrying (see authrelay.py).
    addr = _front_preamble_addr(conn, addr)
    # Re-bind the advertise peer to the REAL client: _serve_one bound the relay's
    # loopback socket, so every address this hop hands out (the lobby handoff
    # token) fell back to the global tailnet IP -- the real PS2 got
    # `accept -> lobby 127.0.0.1:51220` on 2026-09-18 after the directory fix
    # had already landed. The dialed address is unknown behind the relay;
    # advertise_for derives it from the route to the peer.
    bind_peer(addr[0], None)
    lobbysession.session_bind(lobbysession._sid_for_connection(addr[0], addr[1]))
    peer = f"{addr[0]}:{addr[1]}"
    _trace_begin(peer, port)
    accept = os.environ.get("POL_AUTH_ACCEPT", "1") == "1"
    acct_db, member = None, None
    chat_sess = None            # set on the welcome hop; dropped in `finally`
    viewer_marked = False       # did this hop mark the session signed-in?
    channel_marked = False      # ...and resumable? (see channel_open below)
    try:
        prefix = f":pol-1000-{port}.pol.com"
        # THE GREETING TOKEN. On a redirect hop it names the next node. On the
        # FINAL hop there is nothing to redirect to, so it is a SESSION token
        # instead -- address zero, server clock in [0:4] -- which is the only
        # thing that sets the client's wall clock (see build_session_token).
        stamp = None
        if (os.environ.get("POL_AUTH_CLOCK", "1") == "1"
                and os.environ.get("POL_AUTH_MODE", "error") == "welcome"):
            stamp = int(time.time()) & 0xFFFFFFFF
            greeting = build_session_token(stamp)
            # Before sending, not after: if this connection dies mid-login the
            # client may still have keyed from it, and the next dial is exactly
            # where we need to recognise that key.
            remember_stamp(addr[0], stamp)
            log("authserv", f"{peer} greeting = SESSION token, clock="
                            f"{datetime.datetime.utcfromtimestamp(stamp)}Z "
                            f"({stamp:#010x})")
            # THE ONE COMPARISON THE OPEN CASE NEEDS. If the client keys from
            # our greeting, this key decrypts its NICK. When it does not, the
            # question is whether it ever saw this token -- so the token, and
            # the key it implies, are recorded at the moment we send them.
            _trace("greet session-token",
                   f"stamp={stamp:#010x} -> key "
                   f"{session_token_key(stamp).hex()}")
        else:
            greeting = build_redirect_token(authcap._self_ip(), next_port)
            _trace("greet redirect-token", "no session stamp on this hop")
        # Every greeting this hop sends, newest last: the NICK digest is
        # md5(greeting[:40] + password), so these are its salt candidates.
        sent_greetings = [greeting]
        conn.sendall(f"{prefix} 300 * {greeting}\r\n".encode())
        user, rest = _recv_line(conn, timeout=20 if stamp is None else 4)
        if stamp is not None and not user:
            # SELF-HEALING FALLBACK. The session token takes a different arm of
            # polcore's state-5 handler than a redirect does, and only the live
            # client can settle whether that arm still advances the login (the
            # redirect arm sets state 3; this one does not). If the client says
            # nothing, send the redirect token it has always had -- the handler
            # runs again with the redirect flag still clear, so it takes the
            # arm we know works. Cost of being wrong: a 4s slower login and no
            # clock. Cost of not doing this: a login that never completes.
            log("authserv", f"{peer} no USER after the session token in 4s -- "
                            "falling back to the redirect greeting (clock stays "
                            "unset; set POL_AUTH_CLOCK=0 to stop trying)")
            # WARNING: THE STAMP IS DROPPED HERE. Whatever the client keyed from the
            # session token we just sent, we now stop offering that key as
            # "this connection's" -- it survives only in the _STAMPS history.
            # Worth seeing in a failure trace, because it is a real way for the
            # right key to fall out of the front of the candidate list.
            _trace("re-greet redirect", f"dropped stamp {stamp:#010x} after 4s "
                                        "of silence")
            stamp = None
            sent_greetings.append(build_redirect_token(authcap._self_ip(), next_port))
            conn.sendall(f"{prefix} 300 * {sent_greetings[-1]}\r\n".encode())
            user, rest = _recv_line(conn)
        elif stamp is not None:
            log("authserv", f"{peer} client accepted the SESSION token "
                            "(USER followed) -- clock is set")
        # THE KEY LINE. By default TOKEN0, which forces K=0 (readable wire).
        # POL_AUTH_RSA=1 (default OFF, not yet proven live) does what SE and
        # Project Crystal Server do instead: the USER token carries the
        # client's per-launch RSA modulus, so wrap a fresh random 8-byte
        # Blowfish key to it and send that. Everything after this line is then
        # under the new key -- the NICK, our replies, and the lobby, which
        # rides the same key and IV (see _remember_iv_key / _lobby_crypt).
        # Anything that goes wrong here falls back to TOKEN0, loudly.
        # WARNING: Untested against FMO: fmo.py's KEY_POLCORE_LEN note says the 16
        # polcore key bytes are zero because our login is K=0. With this knob
        # on they may not be -- if FMO stops handshaking, look there first.
        rsa_key = None
        if auth_rsa_enabled(addr[0]):
            _utok = user.split(b":", 1)[1].strip() if b":" in user else b""
            try:
                n_mod = sessioncrypt.user_token_modulus(_utok)
                if n_mod is None:
                    log("authserv", f"{peer} *** POL_AUTH_RSA: no RSA modulus in "
                                    f"the USER line ({user!r}) -- sending TOKEN0 "
                                    "(K=0) instead ***")
                else:
                    rsa_key = os.urandom(8)
                    wrapped = sessioncrypt.b64encode(
                        sessioncrypt.rsa_wrap_key(n_mod, rsa_key))
                    conn.sendall(sessioncrypt.frame_line(
                        f"{prefix} 300 * {wrapped}".encode(), pad=b"")
                        + b"\r\n")
                    log("authserv", f"{peer} POL_AUTH_RSA: sent an RSA-wrapped "
                                    f"session key ({n_mod.bit_length()}-bit "
                                    "client modulus)")
                    _trace("key line", "RSA-wrapped random key")
            except Exception as exc:
                log("authserv", f"{peer} *** POL_AUTH_RSA: wrapping failed "
                                f"({exc!r}) -- sending TOKEN0 (K=0) instead ***")
                rsa_key = None
        if rsa_key is None:
            conn.sendall(f"{prefix} 300 * {authnode.TOKEN0}\r\n".encode())
        # NICK is the next CRLF-terminated line (may already be partly in `rest`)
        if b"\r\n" in rest:
            nick_enc, rest = rest.split(b"\r\n", 1)
        else:
            more, rest = _recv_line(conn)
            nick_enc = rest0 = more
        save_capture(f"authserv-{port}", user + b"||" + nick_enc)
        log("authserv", f"{peer} hop {port}: USER={user!r}")
        _trace("USER", repr(user))
        _trace("NICK ciphertext", f"{len(nick_enc)}B {nick_enc[:16].hex()}...")
        # THE SESSION ID. `USER x 8 * :<48 chars>` is the client's own per-launch
        # token: measured across the whole log it appears on each hop of one
        # login, seconds apart, and never again. So it -- not the address -- is
        # what makes both hops of a launch one session, and what keeps two
        # clients apart when their packets arrive from the same bridge address.
        # A hop that never sends USER keeps the provisional per-connection id.
        utok = user.split(b":", 1)[1].strip() if b":" in user else b""
        if utok:
            sid = lobbysession._sid_for_user_token(utok)
            if sid != lobbysession._session_sid():
                lobbysession.session_bind(sid)
                log("authserv", f"{peer} session {sid} (from this launch's USER "
                                f"token, {len(utok)}B)")
        lobbysession._session_put(lobbysession._session_sid(), peer_ip=addr[0], user_token=utok.decode(
            "latin-1", "replace") or None)
        # Client keyed K=0 (our token0); recover the per-connection OFB IV from the
        # NICK itself (K=0 known + 'NICK ' crib), then decrypt.
        # WHICH KEY the client ended up with is a question, not an assumption.
        # TOKEN0's state-8 arm overwrites the key with 0, so K=0 should still be
        # right -- but a session token seeds a key too, and if the client never
        # reached state 8 it would keep THAT one. Trying both costs one failed
        # crib match and tells us which arm ran, instead of failing the login.
        P, S = authnode._P0, authnode._S0
        # Kept alongside P/S because a RESUME has to rebuild them from scratch in
        # another process, and bf_setkey's output is not something we persist.
        sess_key = b"\x00" * 8
        iv, nick_pt = None, None
        # THE IV IS IN THE USER LINE. The client announces it: the first 8
        # bytes of the SE-base64 USER token are this connection's OFB IV (see
        # sessioncrypt.iv_from_user_token; it matched recover_iv on 464 of 470
        # logged logins). So instead of searching for the IV under each key, try
        # each key against the KNOWN IV -- one Blowfish block per key, K=0 first
        # -- and only fall back to the crib search below when none of them fits.
        # POL_IV_FROM_USER=0 skips this and restores the search-only path.
        iv_path = "search"
        user_iv = (sessioncrypt.iv_from_user_token(utok)
                   if os.environ.get("POL_IV_FROM_USER", "1") == "1" else None)
        if user_iv is not None:
            for why, key in ([("the RSA-wrapped key", rsa_key)] if rsa_key
                             else []) + ([("K=0", b"\x00" * 8)]
                                         + redirect.key_candidates(addr[0], stamp)):
                Pk, Sk = ((authnode._P0, authnode._S0) if key == b"\x00" * 8
                          else sessioncrypt.bf_setkey(key))
                iv, nick_pt = sessioncrypt.try_iv(Pk, Sk, nick_enc, user_iv)
                if iv is None:
                    continue
                P, S = Pk, Sk
                sess_key = key
                iv_path = "USER"
                if key == rsa_key:
                    lobbysession._session_put(lobbysession._session_sid(), key=key)
                    _trace("key recovered", "the RSA-wrapped key")
                elif key != b"\x00" * 8:
                    lobbysession._session_put(lobbysession._session_sid(), key=key)
                    log("authserv", f"{peer} session key is {why} ({key.hex()}), "
                                    "not K=0 -- TOKEN0's state-8 arm did not "
                                    "run; the lobby will use this key too")
                    _trace("key recovered", f"{why} ({key.hex()})")
                break
            _trace("IV from USER", (f"{user_iv.hex()} MATCHED under "
                                    f"{sess_key.hex()}") if iv is not None
                   else f"{user_iv.hex()} fits no candidate key -- searching")
            if iv is None:
                log("authserv", f"{peer} the USER token's IV {user_iv.hex()} "
                                "decrypts the NICK under no candidate key -- "
                                "falling back to the IV search")
        if iv is None and rsa_key is not None:
            Pr, Sr = sessioncrypt.bf_setkey(rsa_key)
            iv, nick_pt = sessioncrypt.recover_iv(Pr, Sr, nick_enc)
            _trace("try the RSA key", "MATCHED" if iv is not None else "no")
            if iv is not None:
                P, S, sess_key = Pr, Sr, rsa_key
                lobbysession._session_put(lobbysession._session_sid(), key=rsa_key)
        if iv is None:
            iv, nick_pt = sessioncrypt.recover_iv(authnode._P0, authnode._S0, nick_enc)
            _trace("try K=0", "MATCHED" if iv is not None else "no")
            if iv is not None and user_iv is not None:
                log("authserv", f"{peer} IV search found {iv.hex()} under K=0 "
                                f"where the USER token said {user_iv.hex()}")
        if iv is None:
            # Not just THIS connection's token: a client that never re-keyed is
            # holding one from an earlier dial, so replay every session token we
            # have issued to this address. See key_candidates().
            # TWO PASSES, CHEAP FIRST. Pass 1 tries every candidate key against
            # the crib list only (brute=False): a known nick is 8 bytes of known
            # plaintext, so a right key is recognised in one block and a wrong one
            # is rejected in one block. Cost is the key schedule alone -- ~0.4 s
            # cold, free once `bf_setkey`'s LRU has it. Pass 2 pays the 64^3 brute
            # force, but ONLY for the two keys that are plausibly right anyway
            # (this connection's own token, and the one this client last
            # authenticated with), which is what catches a nick we do not know.
            #
            # Before this split the brute force ran for EVERY candidate, so a
            # sweep cost ~2.0 s x N regardless -- measured 66.9 s for 33 keys on
            # the 2026-09-07 capture, all of it with the client sitting on
            # "Connecting to PlayOnline". That wall-clock, not the search itself,
            # is what `POL_STAMP_TRY` was really capping.
            cand_list = redirect.key_candidates(addr[0], stamp)
            # K=0 already had its brute-force pass above; repeating it here
            # would just burn another ~2 s. (`_session_get_for(...,"key")`
            # does hand back an all-zero key -- seen in the 09-07 capture,
            # where it occupied candidate slot 2.)
            brute_keys = {k for _, k in cand_list[:2] if k != bytes(8)}
            # And a hard wall-clock bound, because the count cap only ever
            # approximated one. A failure the client waits 40 s for is worse than
            # the same failure in 12: it re-dials either way, and the sooner it
            # does the sooner it gets a token it CAN key from. 0 disables.
            iv_budget = float(os.environ.get("POL_IV_BUDGET_S", "12"))
            iv_deadline = time.time() + iv_budget if iv_budget > 0 else None
            for why, key, do_brute in (
                    [(w, k, False) for w, k in cand_list]
                    + [(w, k, True) for w, k in cand_list if k in brute_keys]):
                if iv_deadline is not None and time.time() > iv_deadline:
                    log("authserv", f"{peer} IV search gave up after "
                                    f"{iv_budget:g}s (POL_IV_BUDGET_S) -- "
                                    "failing fast so the client re-dials")
                    break
                Pk, Sk = sessioncrypt.bf_setkey(key)
                iv, nick_pt = sessioncrypt.recover_iv(Pk, Sk, nick_enc,
                                                      brute=do_brute)
                if iv is None:
                    continue
                P, S = Pk, Sk
                sess_key = key
                lobbysession._session_put(lobbysession._session_sid(), key=key)
                log("authserv", f"{peer} session key is {why} ({key.hex()}), "
                                "not K=0 -- TOKEN0's state-8 arm did not run; "
                                "the lobby will use this key too")
                _trace("key recovered", f"{why} ({key.hex()})")
                break
        if iv is None:
            # NOT a truncated read: the captures show nick_enc is a full 84 bytes
            # on failures and successes alike, and a REPEATED USER token is normal
            # (LsbYmfwLKy@YCINgKBTo recurred 4x across 06:00-06:16 and every one
            # logged in). Log the length so the short-read case stays
            # distinguishable if it ever does happen.
            #
            # Reaching here now means something NEW. The stale-session-key case
            # -- the client keyed to an earlier token of ours -- is handled
            # above, so this is a key we never issued: state carried across a
            # server restart (the stamp history is in memory only), a client
            # older than an hour of history, or a genuinely different arm of
            # polcore's key setup. Record how many candidates were tried, so the
            # next round can tell "we had nothing to try" from "we tried
            # everything we ever sent and none of it fit".
            # *** EVERYTHING THE OPEN CASE NEEDS, WRITTEN DOWN WHILE IT EXISTS.
            # See the login-trace note above `save_capture`. This used to log
            # eight bytes and a count, which cannot answer the only question
            # that matters ("is the key the client holds one we ever issued?")
            # and in particular cannot tell a complete search from a TRUNCATED
            # one -- the candidate list is capped at POL_STAMP_TRY.
            cands = redirect.key_candidates(addr[0], stamp)
            _stamps_refresh()
            with _STAMPS_LOCK:
                history = list(_STAMPS.get(addr[0], []))
            capped = redirect._stamp_try()
            truncated = 0 < capped < len(history) + 2
            _trace("IV RECOVERY FAILED",
                   f"{len(cands)} candidate(s) tried, "
                   f"{len(history)} stamp(s) in history for {addr[0]}")
            path = _trace_dump(peer, "nokey", [
                ("the ciphertext we could not read (full)", nick_enc.hex()),
                ("candidates tried, in order",
                 "\n".join(f"  {key.hex()}  {why}" for why, key in cands)
                 or "  (none)"),
                ("every stamp issued to this address",
                 "\n".join(
                     f"  {st:#010x}  key {session_token_key(st).hex()}  "
                     f"issued {int(time.time() - at)}s ago"
                     for st, at in reversed(history)) or "  (none)"),
                ("this dial's greeting stamp",
                 f"  {stamp:#010x}  key {session_token_key(stamp).hex()}"
                 if stamp is not None else
                 "  (none -- this hop greeted with a REDIRECT token)"),
            ])
            log("authserv", f"{peer} could not recover IV "
                            f"(nick_ct={nick_enc[:8].hex()}, len={len(nick_enc)}); "
                            f"K=0 and {len(cands)} issued session key(s) all "
                            "failed the crib"
                            + (f" -- and the list was TRUNCATED at "
                               f"POL_STAMP_TRY={capped} of {len(history)} known "
                               f"stamps, so this is NOT proof the key was never "
                               f"ours" if truncated else
                               " -- every SESSION-token key we hold for this "
                               "address was tried. That is not the same as "
                               "'a key we never issued': a session token is the "
                               "only kind whose key we can COMPUTE (LE32(stamp) "
                               "+ 4 zero bytes). If the client took polcore's "
                               "state-8 RSA arm instead (0x37d61f1) -- on this "
                               "greeting or on an earlier REDIRECT hop, whose "
                               "tokens are not recorded at all -- its key is "
                               "base^e mod n, and we have never captured SE's "
                               "modulus (tools/rsa_capture.py), so that key is "
                               "not in the candidate set and cannot be. Verified "
                               "2026-09-07 against a real capture: no stamp for "
                               "ANY address, no LE/BE key layout, and no clock "
                               "value within +/-120s of the greeting decrypts it")
                            + (f"; trace {path}" if path else ""))
            # HOLD experiment (POL_HOLD_UNDECRYPTABLE): instead of dropping the
            # console -- which makes it tear the session down and free the derived
            # key immediately -- keep the socket OPEN and silent. The console then
            # sits in its post-NICK "waiting for server" state with the session key
            # still live in EE RAM, so a PCSX2 savestate taken any time during the
            # hold captures the key (and possibly the modulus). Off by default.
            hold_s = int(os.environ.get("POL_HOLD_UNDECRYPTABLE", "0"))
            if hold_s > 0:
                log("authserv", f"{peer} HOLDING undecryptable session open for "
                                f"{hold_s}s (POL_HOLD_UNDECRYPTABLE) -- SAVE A PCSX2 "
                                "STATE NOW while the key is live in EE RAM")
                end = time.time() + hold_s
                conn.settimeout(1.0)
                while time.time() < end:
                    try:
                        if not conn.recv(64):   # client hung up
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                log("authserv", f"{peer} hold window ended")
            return
        if rsa_key is not None and sess_key != rsa_key:
            # The client did not take the key we wrapped for it -- the NICK
            # came under K=0 or an old session key. The login goes on under
            # that key (so nothing breaks), but POL_AUTH_RSA is not working
            # for this client and whoever armed it needs to know.
            log("authserv", f"{peer} *** POL_AUTH_RSA: the client IGNORED the "
                            "RSA-wrapped key -- its NICK decrypted under "
                            f"{'K=0' if sess_key == bytes(8) else sess_key.hex()} "
                            "instead; continuing under that key ***")
            _trace("RSA key IGNORED", f"NICK under {sess_key.hex()}")
        elif rsa_key is not None:
            log("authserv", f"{peer} POL_AUTH_RSA: NICK decrypted under the "
                            "RSA-wrapped key -- this session is not K=0")
            lobbybind._remember_iv_key(lobbysession._session_sid(), iv, rsa_key)
        # NICK line = `NICK <handle>:<32-hex session digest>:<36-char blob>`.
        # The blob's fixed-width layout (decoded live 2026-08-13 from two logins
        # of one account) is  head[8] + pad[13] + TOKEN[11] + nonce[4], and the
        # TOKEN is a STABLE function of the password: identical across sessions
        # for one password, different for a different one. So it is the credential
        # to check -- unlike the 32-hex digest, which is salted per session. See
        # nick_credential().
        nick_fields = nick_pt[5:].split(b":")
        nick = nick_fields[0].strip() or b"UDXS6FWXX"
        cred = nick_credential(nick_fields[2]) if len(nick_fields) > 2 else None
        # WHICH client build this is. The same account's token differs between
        # the PC and PS2 Viewers, so the credential check is scoped by it.
        client_sig = (nick_client_sig(nick_fields[2])
                      if len(nick_fields) > 2 else None)
        # (Log readers parse this line: keep "IV=.. nick=.. NICK=.." adjacent
        # and put anything new at the end.)
        log("authserv", f"{peer} IV={iv.hex()} nick={nick!r} NICK={nick_pt!r}"
                        + (f" cred={cred!r}" if cred else "")
                        + (f" client={client_sig!r}" if client_sig else "")
                        + f" iv_via={iv_path}")
        # Promote this nick to the front of the crib list, so this account's NEXT
        # dial takes recover_iv's fast path (one Blowfish block) instead of the
        # 64^3 brute force. Covers auto-provisioned accounts too, whose nick is
        # not derivable from a POL ID and so is absent from `login_alias`.
        sessioncrypt.remember_nick(nick)
        # The LOBBY uses this same K=0 keystream (proven: this IV decrypts pp000
        # message headers to clean structure), so hand it to the lobby handler.
        lobbysession._session_put(lobbysession._session_sid(), iv=iv)
        # ...and WHICH BUILD it is, for the lobby's send pacing: the NICK's
        # client signature is the one real build tell we have (PS2 `TTTTT7I...`,
        # PC `TTTTTAI...`), and the lobby band cannot see it -- see
        # _lobby_pace_ps2. Recorded even for a login that is refused below;
        # pacing is payload-neutral, so a stale value costs nothing.
        if client_sig:
            lobbysession._session_put(lobbysession._session_sid(), client_sig=client_sig)
        # Resolve the NICK to an account. Permissive unless POL_ACCOUNTS_ENFORCE=1;
        # a rejection here means "no such account" OR a wrong password, and we now
        # SAY SO on the wire instead of hanging up (see the reject block below).
        acct_db, member, reject = resolve_account(
            nick, addr[0], iv, cred=cred, client_sig=client_sig,
            digest=nick_fields[1].strip() if len(nick_fields) > 2 else None,
            salts=login_digest_salts(addr[0], sent_greetings))
        _trace("account resolved",
               ("member %s (id %s)" % (member["login_name"], member["id"]))
               if member is not None else
               ("REJECT %#04x" % reject if reject is not None else
                "no account (stateless)"))
        # Remember WHO logged in, so the lobby's record builders serve this
        # member rather than whichever row happens to sort first.
        if member is not None:
            # `viewer_open` marks the session as CURRENTLY SIGNED IN. This hop
            # holds its socket for the whole session (the observe loop below), so
            # the flag is true exactly while a Viewer is up, and the `finally`
            # clears it.
            #
            # It exists for the FFXI bridge. The bridge has to decide which POL
            # member a game connection belongs to, and FFXI's own lobby stream
            # names nobody until the character SELECT -- after the character list
            # is built. Ranking historical sessions by recency guessed wrong in
            # the field on 2026-08-15: a stale session from a previous account
            # was 47s fresher than the signed-in one, so the launch was handed
            # the wrong member's LSB account and every symptom followed (POL-0001,
            # a missing character, and a name that read as "already taken"
            # because it lived on the account the player had been moved off).
            # "Who is signed in right now" does not have that failure mode.
            lobbysession._session_put(lobbysession._session_sid(), member_id=int(member["id"]),
                         viewer_open=True)
            viewer_marked = True
            log("authserv", f"{peer} session member = {member['login_name']} "
                            f"(id {member['id']})")
        # REFUSED -- tell the client WHY, the way SE does.
        #
        # This used to be a bare `return`: the socket closed mid-handshake with
        # nothing on it, and the client, having no code for "hung up on me",
        # reported a TRANSPORT failure -- POL-0007/0010/0207, all of which render
        # as "Network is busy or there are connection problems". So every wrong
        # password looked like a broken network, which is exactly the complaint
        # that sent us to capture SE (2026-08-16).
        #
        # Two changes here, and the second is a behaviour fix, not a message:
        #   * send SE's refusal record before closing;
        #   * gate on `reject`, not on POL_ACCOUNTS_ENFORCE. The old condition
        #     meant POL_ACCOUNTS_ENFORCE_PW=1 ALONE never blocked anything --
        #     resolve_account refused the wrong password, returned None, and this
        #     line let it fall straight through to the welcome burst. The narrow
        #     password-only gate did not gate.
        if reject is not None:
            _trace_dump(peer, "reject", [
                ("what the client was told",
                 f"  status byte {reject:#04x}")])
            why = logingate._REJECT_WHY.get(reject, f"status {reject:#04x}")
            # MARK THE SESSION REFUSED, before anything else can use it.
            #
            # We cannot simply drop the session: the reject record below is
            # encrypted under this connection's recovered IV, so the IV has to
            # stay registered for the client to read WHY it was refused. But that
            # same IV is what the lobby band uses to identify a session, so
            # without this flag a refused login still names a session -- and
            # _session_member_id's pre-login fallback then handed it the lowest
            # member's account. Measured 2026-08-17: a wrong-credential NICK got
            # a full handle list, friend list and comments.
            lobbysession._session_put(lobbysession._session_sid(), auth_refused=True)
            try:
                conn.sendall(sessioncrypt.ofb_apply(
                    P, S, iv, sessioncrypt.frame_line(
                        logingate.reject_line(addr[0], reject), pad=b"")) + b"\r\n")
                log("authserv", f"{peer} REFUSED ({why}): sent SE reject record "
                                f"status={reject:#04x}")
            except OSError as exc:
                # The client hanging up first is not an error worth a traceback,
                # but it IS worth knowing we never got the message out -- that is
                # the difference between "it showed the wrong error" and "it
                # showed the old network error because we said nothing".
                log("authserv", f"{peer} REFUSED ({why}): could not send reject "
                                f"record ({exc!r}) -- client sees a bare close")
            return
        if not accept:
            return
        # What the server emits after the NICK. Encrypted with K=0 + this
        # connection's recovered IV (OFB resets from IV each line, CRLF verbatim).
        #   POL_AUTH_MODE=error   -> a redirect hop: encrypted ERROR :Closing Link.
        #     The 300 redirect we already sent (top of this handler) points at the
        #     NEXT auth port, so the client should re-dial there. Proves the chain.
        #   POL_AUTH_MODE=welcome -> first-draft final accept (001/308/422/MODE).
        mode = os.environ.get("POL_AUTH_MODE", "error")
        if mode in ("error", "lobby"):
            # A redirect via ERROR :... (POL <base32 record>). The client's ERROR
            # handler (FUN_037d5820) decodes the (POL ..) record, stores it
            # (FUN_037d5a00 -> DAT_03868258) and reconnects to it. mode=error hops
            # to the NEXT auth port (proves the chain); mode=lobby aims the redirect
            # at our lobby band.
            tgt_port = (int(os.environ.get("POL_LOBBY_PORT", "51220"))
                        if mode == "lobby" else next_port)
            tok = redirect.pol_error_token(authcap._self_ip(), tgt_port)
            lines = [b"ERROR :Closing Link (POL " + tok.encode() + b")"]
            log("authserv", f"{peer} {mode} redirect (POL) -> {authcap._self_ip()}:{tgt_port} "
                            f"tok={tok}")
        else:  # welcome -- final accept. Match SE's accepted-hop shape (from the
               # 673B/1558B captures, crib-dragged): an ENCRYPTED 300 * <token>
               # lobby-handoff FIRST (our earlier draft omitted this, so the client
               # never learned a lobby and hung), then 001 / 422 / MODE / NOTICE.
               # The 300 token is a base-32 redirect (same codec as the directory)
               # pointing the client at OUR lobby band.
            # Experiment (POL_AUTH_NOLOBBY=1): complete login but hand the client a
            # ZERO lobby address, so it finishes the login sequence (gate set) and
            # shows the PlayOnline main menu -- where our 001 games list lives --
            # WITHOUT dialing our pp000 (which it can't handshake -> POL-0512/0008).
            # Tests whether the menu can display without completing the pp000 lobby.
            if os.environ.get("POL_AUTH_NOLOBBY", "0") == "1":
                lobby_ip, lobby_port = "0.0.0.0", 0
            else:
                lobby_ip = authcap._self_ip()
                lobby_port = int(os.environ.get("POL_LOBBY_PORT", "51220"))
            lobby_token = build_redirect_token(lobby_ip, lobby_port)
            # The gate record carries the unread-mail count the badge shows at
            # boot (byte +0x11), so serve this member's real mailbox depth. Best
            # effort: a mail-DB hiccup must never cost anyone their login, so any
            # failure here just means a zero badge.
            unread = 0
            try:
                if member is not None:
                    _box = accounts.mail_box_name(
                        member["mail_address"] if "mail_address" in member.keys()
                        else member["login_name"])
                    if _box:
                        unread = len(accounts.list_mail(acct_db, _box))
            except Exception as exc:
                log("authserv", f"{peer} unread-mail count unavailable ({exc!r}); "
                                f"badge will read 0")
            stamps = (logingate.gate_list_stamps(acct_db, member)
                      if acct_db is not None else None)
            if stamps:
                log("authserv", f"{peer} gate list stamps (POL_GATE_LIST_STAMPS): "
                                + ", ".join(f"0x{o:02X}={v}" for o, v in
                                            sorted(stamps.items())))
            gate_rec = logingate.build_gate_record(lobby_ip, lobby_port, pad_to=0,
                                         unread=unread, stamps=stamps)
            p = prefix[1:]
            # The gate/redirect record rides in a NOTICE payload (command NOTICE ->
            # FUN_037d7f90 -> FUN_037d7ef0 -> FUN_037db6f0). FUN_037d7ef0 tokenises the
            # message: first token = target (nick), the REST is the base64 record. So
            # a standard `:prefix NOTICE <nick> :<record>` delivers it. It must arrive
            # in state 0xc (after 422) where FUN_037d6400 dispatches word commands.
            lines = [
                f":{p} 300 * {lobby_token}".encode(),
                f":{p} 001 ".encode() + nick + b" :Welcome to PlayOnline",
                f":{p} 422 ".encode() + nick + b" :MOTD File is missing",
                b":" + nick + b" MODE " + nick + b" :+i",
                # WITH a target nick: FUN_037d7ef0 tokenises the NOTICE params, skips
                # the first word (the target) and the ':' and hands the REST (the
                # record) to FUN_037db640. A MISSING target makes the client substitute
                # a "HOGEHOGE" placeholder that then corrupts the record (seen in a
                # dump). So: NOTICE <nick> :<record>.
                f":pol!~x@{p} NOTICE ".encode() + nick + b" :" + gate_rec.encode(),
            ]
            # GAMES MENU (content list): the client's `001` handler (numeric dispatch
            # edi==1) tokenises the params, requires a 192-char token, base64-decodes it
            # (A64 alphabet, 4->3) into the 144-byte content global -> the free-contents
            # menu. So a 001 line carrying base64(A64) of build_block(ids)[:144] makes
            # Tetra Master (id 2) appear. See pol-games-menu. Gated by POL_AUTH_GAMES.
            if os.environ.get("POL_AUTH_GAMES", "1") == "1" and contentlist is not None:
                # Account-driven when we resolved one: the menu is exactly the
                # titles this member holds a Content ID for. Falls back to the
                # env list when there is no account DB (or it granted nothing),
                # which is the pre-accounts behaviour.
                gids = None
                if acct_db is not None and member is not None:
                    gids = accounts.content_ids(acct_db, member["id"]) or None
                    if gids:
                        log("authserv", f"{peer} games-menu from account "
                                        f"{member['login_name']}: {gids}")
                if gids is None:
                    gids = contentprofiles.lobby_content_ids()
                # CONTENT ID per game (entryA fieldC). A zero here is exactly what
                # makes the client refuse to launch with "You have no content id for
                # <game>". Fill each with a non-zero id so the title reads as
                # registered. Displayed value is fieldC-0x6270 (app.dll 0x4aa9f6c);
                # POL_CONTENT_ID_BASE tunes it while we confirm the format the
                # client actually validates. Default -> a clean serial 10000000+cid.
                cid_base = int(os.environ.get("POL_CONTENT_ID_BASE", "0"), 0)
                content_ids = {g: (cid_base + g if cid_base
                                   else 0x6270 + 10_000_000 + g) for g in gids}
                block = contentlist.build_block(gids, content_ids=content_ids)
                log("authserv", f"{peer} content ids (fieldC): "
                                + ", ".join(f"{g}={content_ids[g]:#x}" for g in gids))
                games_tok = logingate._b64encode(block[:144]).encode()   # 192-char A64 token
                lines.insert(2, f":{p} 001 ".encode() + nick + b" " + games_tok)
                log("authserv", f"{peer} games-menu: 001 content token "
                                f"ids={gids} ({len(games_tok)}ch)")
            log("authserv", f"{peer} accept -> lobby {authcap._self_ip()}:{lobby_port} "
                            f"token={lobby_token} NOTICE-rec[{len(gate_rec)}ch]")
        # THE SOCKET'S ONE WRITER. Built here, before the first byte goes out,
        # because from this point another thread may broadcast to this client at
        # any moment (a room JOIN, a relayed line) and every writer has to take
        # the same lock. It used to be built after the welcome burst, with the
        # burst, the PING and the observe loop all calling conn.sendall()
        # directly -- see the ChatSession note.
        chat_sess = chatsession.ChatSession(nick, prefix[1:], addr[0], conn, P, S, iv,
                                member=member)
        chat_sess.sid, chat_sess.client_sig = lobbysession._session_sid(), client_sig
        # Each ENCRYPTED line needs the client's trailing checksum (frame_line) or
        # FUN_037d5e80 rejects it after decrypt (return before the numeric handler) --
        # which is why 422 never registered and the record never reached the gate.
        chat_sess.send_raw(chat_sess.encode(lines))
        _trace("welcome sent", f"mode={mode}, {len(lines)} line(s)")
        # POL_LOGIN_TRACE=all turns this into the deliberate whole-login capture;
        # on the default (`fail`) it writes nothing and costs a dict lookup.
        _trace_dump(peer, "ok")
        log("authserv", f"{peer} sent mode={mode} {len(lines)}-line reply "
                        f"(K={'0' if sess_key == bytes(8) else sess_key.hex()}"
                        f", IV={iv.hex()}, checksummed)")
        # The real directory/auth hops CLOSE right after replying; the client
        # advances (dials the next node / the lobby) on that EOF. Holding the
        # socket open (POL_AUTH_HOLD) is what made the client hang in "verifying".
        # So: briefly observe for any immediate client bytes, then let `finally`
        # close the socket to trigger the client's next hop.
        observe = int(os.environ.get("POL_AUTH_OBSERVE", "3"))
        # KEEPALIVE (fixes the POL-0008 "network unreachable" mid-session drops).
        # After the FINAL (welcome) accept the client keeps THIS socket open as its
        # session channel -- chat and presence ride it. Closing it on an idle timeout
        # is exactly what the client reports as POL-0008. Real POL never does that: it
        # keeps the IRC link open and PINGs. So on the welcome hop we hold the socket
        # indefinitely and send an IRC PING every POL_AUTH_PING seconds; a failed send
        # is TCP telling us the peer is really gone, which is the only thing that ends
        # it (besides a clean EOF). Redirect hops keep the old behaviour -- a short
        # observe window then close, which is how the client advances to the next hop.
        ping_every = int(os.environ.get("POL_AUTH_PING", "60"))
        keepalive = (mode == "welcome") and ping_every > 0
        # DIAGNOSTIC ONLY (no behaviour change): which of a member's several
        # :51241 connections is which.
        #
        # WARNING: `second_channel` DOES NOT MEAN "GAME BAND", AND READING IT THAT WAY
        # SENT A WHOLE PLAN DOWN THE WRONG ROAD. It means only "this member
        # already holds another live channel", and the commonest other channel
        # is the POL VIEWER's own session hop, which every TM player has open by
        # definition -- so it reads True for the room band, the game band and
        # any churn dial alike. Measured on the 2026-08-24T03:08Z trade: member
        # 3 held :30912 (viewer, PING/PONG only), :37092 (the real game band --
        # `@TeachDV=` then `@Init=`), and :43072 (the room band, `<DR>` polls);
        # ALL THREE logged second_channel=True or False for reasons that had
        # nothing to do with their role. The old wording here even printed
        # "GAME-BAND CANDIDATE" on the strength of it.
        #
        # The band's ROLE is decided by what the client SAYS on it, not by how
        # many sockets it has, so it cannot be known at welcome time -- see the
        # `_band_role` verdict logged by `_auth_channel_loop` when this
        # connection ends. This line stays for the socket timeline it gives
        # (open time, mode, keepalive); it no longer guesses.
        # POL_BAND_DIAG=0 silences it.
        if member is not None and os.environ.get("POL_BAND_DIAG", "1") == "1":
            try:
                _second = presence._member_has_other_channel(int(member["id"]), chat_sess)
            except Exception:
                _second = None
            log("authserv",
                f"{peer} hop {port} disposition: mode={mode} "
                f"keepalive={keepalive} member={member['id']} "
                f"other_live_channel={_second} (role unknown until the client "
                f"speaks -- see the band verdict at close)")
        conn.settimeout(ping_every if keepalive else observe)
        # EVERYTHING A RESUME NEEDS. From here on this socket is the session
        # channel, and if THIS PROCESS is killed the front relay (authrelay.py)
        # can re-attach the client's still-open socket to a new one -- but only
        # if the state to serve it with outlived us. So it goes in the session
        # slot, which is already written atomically to auth-sessions.json.
        #
        # `channel_open` is the deliberate-vs-crash bit the resume door reads:
        # the `finally` below clears it on every path this process controls, so
        # a flag still set means we were killed holding the socket. A redirect
        # hop, whose EOF is how the client advances, therefore never resumes.
        #
        # The fingerprint is the client's own encrypted NICK line. The relay can
        # see it without being able to read it, it is per hop and high entropy,
        # and it names the session even when two clients share one bridge
        # address -- the failure mode the JOIN KEY note above _SESSIONS exists
        # for. Never key a resume on the address.
        if keepalive:
            resume_fp = hashlib.sha1(nick_enc).hexdigest()
            lobbysession._session_put(lobbysession._session_sid(), resume_fp=resume_fp,
                         nick=nick, srv=prefix[1:], key=sess_key,
                         hop_port=port, channel_open=True)
            channel_marked = True
        # PRESENCE. Only the welcome hop is a real session channel (redirect hops
        # close at once), so only it is a login worth announcing. Register this
        # socket under its member so a friend's presence push can find it, then tell
        # this member's already-online friends that they just came online. Both are
        # no-ops unless POL_PRESENCE_PUSH is on; register is cheap and harmless
        # either way, and keeping it unconditional means is_online() is always true
        # while the socket lives. The matching unregister is in `finally`.
        if keepalive and member is not None:
            # A newer login of this member replaces an older one, the way SE
            # and Project Crystal Server do it. Off by default -- see
            # _kill_duplicate_logins for why that is not as simple here.
            if os.environ.get("POL_AUTH_KILL_DUP", "0") == "1":
                presence._kill_duplicate_logins(int(member["id"]), chat_sess, peer)
            presence.PRESENCE.register(int(member["id"]), chat_sess)
            # No name passed on purpose -- _broadcast_presence resolves the
            # HANDLE name. Passing `nick` here is what leaked URZ82TPPK.
            pushrecord._broadcast_presence(int(member["id"]), "online")
            # LOAD this member's friend roster into polcore's recognition table
            # so the in-game friend-check works (empty table => nothing greys).
            # Gated POL_FRIEND_LOAD (off/log/1); a no-op unless armed.
            friendroster._send_friend_roster(chat_sess, int(member["id"]))
        extra = authresume._auth_channel_loop(conn, peer, addr, chat_sess, nick,
                                   prefix, P, S, iv, keepalive, ping_every)
        if extra:
            log("authserv", f"{peer} client replied {len(extra)}B total\n"
                            + hexdump(extra))
        if keepalive and member is not None and chat_sess.killed_dup:
            # REPLACED, NOT LOGGED OUT. A newer login of this member killed
            # this channel (POL_AUTH_KILL_DUP); the member is still online on
            # the new one, so no offline push and no session wipe.
            log("accounts", f"{peer} channel for {member['login_name']} was "
                            "killed by a newer login -- not a logout, no "
                            "offline notification")
        elif keepalive and acct_db is not None and member is not None \
                and presence._member_has_other_channel(int(member["id"]), chat_sess):
            # NOT A LOGOUT. This member is still holding another live channel, and
            # close_sessions() would delete its row too. See _member_has_other_channel.
            log("accounts", f"{peer} channel closed for {member['login_name']} but "
                            f"{len(presence.PRESENCE.sessions_for(int(member['id']))) - 1} other "
                            "live channel(s) remain -- not a logout, presence held")
        elif keepalive and acct_db is not None and member is not None:
            # The welcome hop's socket IS the session channel, so losing it is
            # the closest thing we have to a logout event -- but it is ALSO what
            # every channel-churn dip looks like, and running the wipe inline
            # here flapped every playing member offline every 10-30s (measured
            # 2026-08-23 22:52Z). `_logout_or_grace` waits out the dip: the
            # wipe (record_logout + close_sessions + offline push + 4:5 latch
            # clear + viewer_open=False) runs only if the member is still gone
            # after POL_PRESENCE_LOGOUT_GRACE seconds; a re-dial in the window
            # suppresses it with a log line. 0 restores the immediate wipe.
            presence._logout_or_grace(int(member["id"]), member["login_name"], peer,
                             lobbysession._session_sid())
        log("authserv", f"{peer} closing hop {port} to trigger client's next dial")
    except Exception as e:
        log("authserv", f"{peer} hop {port} error: {e}")
    finally:
        # *** A LOGIN THAT HANGS PRODUCES NO OUTCOME, AND THAT WAS THE HOLE. ***
        # Every dump above fires at a RESULT -- welcome, reject, no-key. A login
        # that simply stops (the client gives up and the socket dies) reaches
        # none of them, so the one failure mode we most want a timeline for was
        # the one that wrote nothing. Seen 2026-08-19 in `resume_test`'s ~25%
        # flake: authserv logs the NICK and then nothing at all, no traceback on
        # stderr, no line in accounts.log. Whatever stalls, it stalls between two
        # points this trace already marks -- so dumping here names it.
        #
        # `why="incomplete"` is deliberately not "fail": we do not know that
        # anything went wrong. A redirect hop closing normally comes through here
        # too, which is why this only writes when the trace shows the login got
        # past its greeting.
        try:
            rows = getattr(_LOGIN_TRACE, "rows", None) or []
            # A SESSION THAT WAS WELCOMED AND LATER CLOSED IS NOT INCOMPLETE. The
            # Viewer cycles its session channel every few minutes, and each
            # cycle came through here with `welcome sent` in its rows and wrote a
            # trace anyway: 1,650 `login-incomplete-*` files on prod by
            # 2026-09-05, burying the 125 real `nokey` ones. The welcome IS the
            # outcome; only a login that never reached one is worth a dump.
            welcomed = any(r[1] == "welcome sent" for r in rows)
            if not _trace_done() and len(rows) > 2 and not welcomed:
                _trace("connection closing", f"after {len(rows)} step(s), with "
                                             "no welcome, reject or key failure")
                path = _trace_dump(peer, "incomplete")
                if path:
                    log("authserv", f"{peer} login ended with no outcome -- "
                                    f"trace {path}")
        except Exception as exc:                       # never break a teardown
            log("authserv", f"{peer} login trace (incomplete) failed ({exc!r})")
        # DO NOT clear `viewer_open` here. This hop closes on a timer
        # (POL_AUTH_OBSERVE, 300s) while the Viewer stays signed in for hours, so
        # clearing on hop close made the flag mean "signed in within the last five
        # minutes" -- it decayed for the ACTIVE user while sessions whose hops
        # never closed cleanly stayed marked forever. Measured 2026-08-16: the
        # session in front of the user (active 23s earlier) read False while two
        # sessions 20 and 31 minutes stale read True, and the FFXI bridge
        # therefore handed that launch to the wrong member's account. Exactly
        # backwards.
        #
        # The flag is cleared on LOGOUT instead (see the logout block above), and
        # staleness is judged from `at`, which every auth and lobby interaction
        # refreshes.
        pass
        # THE DELIBERATE-CLOSE BIT. Reaching here means this process decided the
        # channel was over -- the client hung up, the hop was a redirect, the
        # handler faulted. All of those must NOT be resumed, so the flag comes
        # down. It stays up only on the path that never runs a `finally`: being
        # killed. See handle_authresume.
        if channel_marked:
            try:
                # ...but only if the slot still describes OUR connection. Hops of
                # one launch share a session id, so a second channel on the same
                # id has already replaced the fingerprint, and clearing the flag
                # here would disarm a resume for a socket that is still live.
                with lobbysession._SESSIONS_LOCK:
                    cur = (lobbysession._SESSIONS.get(lobbysession._session_sid()) or {}).get("resume_fp")
                if cur == resume_fp:
                    lobbysession._session_put(lobbysession._session_sid(), channel_open=False)
            except Exception:
                pass
        # LEAVE EVERY ROOM. A session that vanishes without a PART would otherwise
        # sit in the registry forever, so its nick keeps appearing in NAMES/WHO and
        # every relayed line tries to write to a dead socket. Broadcast a QUIT so
        # the survivors' member lists actually shrink.
        if chat_sess is not None:
            chat_sess.alive = False
            # Drop this socket from the presence registry so a later push does not
            # try to write to a dead session. Safe on any hop: unregister of a
            # session that was never registered (redirect hops) is a no-op. The
            # offline BROADCAST already fired in the logout block above on the clean
            # path; this covers the registry bookkeeping for every path including a
            # crash, where the row-close/broadcast may have been skipped.
            if member is not None:
                presence.PRESENCE.unregister(int(member["id"]), chat_sess)
            for chan, remaining in roomregistry.ROOMS.drop(chat_sess):
                quit_line = (b":" + chat_sess.nick + b"!~x@" + ircband._irc_host(chat_sess.srv) +
                             b" QUIT :Connection closed")
                for m in remaining:
                    m.send([quit_line])
                # ...AND RETIRE THE TETRA MASTER ROOM RECORD, for the same reason
                # the registry is cleaned here: a session that vanishes without a
                # PART would otherwise sit there for ever.
                #
                # WARNING: ONLY IF THIS WAS THE MEMBER'S LAST SESSION IN THE ROOM.
                # A launch holds SEVERAL connections at once -- redirect hops
                # share one session id, which is exactly why the deliberate-close
                # bit above must check the fingerprint before clearing it -- so
                # retiring on ANY close retires a member who is still standing in
                # the room on another socket. Measured 2026-08-20, the first day
                # this shipped: member 9 was DROPPED three times in one sitting
                # while actively playing, and the roster we then served to the
                # other player was missing HIMSELF, because one of his own hops
                # had closed seconds earlier. `remaining` is precisely the set
                # that answers "is he still here", and it is already in hand.
                if member is not None and not presence._member_still_present(
                        remaining, member["id"]):
                    titles.session_closed(int(member["id"]))
                log("authserv", f"{peer} left room {chan.decode('latin1')} "
                                f"({len(remaining)} member(s) remain)")
        if acct_db is not None:
            try:
                acct_db.close()
            except Exception:
                pass
        conn.close()
