#!/usr/bin/env python3
"""Account refusals, login notices, the admin panel's controls and Kick.

    python tools/account_state_test.py

Four parts, each printing PASS:
  * a per-account refusal code (with and without an expiry) refuses the login
    with that code, and the password checks and lockout still work around it;
  * a one-time notice is claimed by one login only, requeued if unsent, and
    cleared once sent; an every-login notice is sent every time;
  * the encrypted NICK reply carries a refusal on the ERROR line and a notice
    in the successful 300 token, both at record byte 6;
  * the admin panel sets both and kicks a live channel: the kick request
    crosses the live-state store to the auth side, which sends the IRC KILL
    and retires every session row the member had. A moderator with only
    "Look up accounts" can read but not change any of it, and the audit log
    names all three actions.

Uses a throwaway PostgreSQL database and Valkey key prefix (tools/pgtest.py),
a temporary data directory and loopback sockets.
"""
import datetime
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICES = HERE.parent / "services"
sys.path.insert(0, str(SERVICES))
sys.path.insert(0, str(HERE))

import pgtest  # noqa: E402

pgtest.use_fresh_database()
pgtest.use_fresh_valkey()


def main():
    with tempfile.TemporaryDirectory(prefix="account-state-",
                                     ignore_cleanup_errors=True) as tmp:
        os.environ.update(
            POL_DATA_DIR=tmp, POL_LOG_DIR=tmp,
            POL_RESOURCE_DIR=str(Path(tmp) / "resources"),
            POL_LOGIN_PW_KEYFILE=str(Path(tmp) / "login-pw.key"),
            POL_ACCOUNTS_ENFORCE="1", POL_ACCOUNTS_ENFORCE_PW="1",
            POL_LOGIN_DIGEST="1", POL_LOGIN_DIGEST_ENFORCE="proven",
            POL_LOGIN_PW_STORE="1", POL_LOGIN_LOCKOUT_FAILS="2",
            POL_LOGIN_LOCKOUT_WINDOW_S="900")
        os.environ.pop("POL_ACCT_STATUS", None)
        import accounts as A
        import responders as R
        import sessioncrypt
        from polcore import kv
        from admin_http import Client, free_port, start_panel, stop

        db = A.connect()
        A.set_admin_cred(db, "operator", "owner-test-password")
        member = A.ensure_member(db, "STATETST")
        polid, mid = member["polid"], int(member["id"])
        A.set_login_password_copy(db, mid, "player-test-password")
        A.set_login_token(db, mid, "test-token")
        A.set_client_token(db, mid, "test-build", "test-token")
        future = (datetime.datetime.now(datetime.timezone.utc) +
                  datetime.timedelta(days=1)).isoformat()
        A.set_reject_code(db, polid, 0xED, future)
        assert A.login_reject_code(db, member) == 0xED
        result = R.resolve_account(b"STATETST", "127.0.0.1", b"stateiv1")
        assert result == (None, None, 0xED, None), result
        db.execute("UPDATE polid SET reject_until = '2000-01-01T00:00:00Z'")
        db.commit()
        assert A.login_reject_code(db, member) == 0
        for code, until in ((0xE7, future), (0xDC, None), (0xFF, None)):
            try:
                A.set_reject_code(db, polid, code, until)
            except ValueError:
                pass
            else:
                raise AssertionError("unsupported refusal accepted")
        A.set_reject_code(db, polid, 0)

        # A successful digest still proves a client; wrong passwords still
        # count toward the lockout.
        salt = "N" * 40
        kwargs = dict(cred="test-token", client_sig="test-build", salts=(salt,))
        conn, found, reject, token = R.resolve_account(
            b"STATETST", "127.0.0.1", b"stateiv2",
            digest=A.login_digest(salt, "player-test-password"), **kwargs)
        assert reject is None and found["id"] == mid and token
        R._finish_pending_session(token, found)
        conn.close()
        assert A.digest_client_proven(db, "test-build")
        for _ in range(2):
            result = R.resolve_account(
                b"STATETST", "127.0.0.1", b"stateiv3",
                digest=A.login_digest(salt, "wrong-password"), **kwargs)
            assert result == (None, None, R.REJECT_BAD_PASSWORD, None), result
        result = R.resolve_account(
            b"STATETST", "127.0.0.1", b"stateiv4",
            digest=A.login_digest(salt, "player-test-password"), **kwargs)
        assert result == (None, None, R.REJECT_LOCKED, None), result
        A.clear_login_failures(db, mid)
        print("password checks and refusal expiry: PASS")

        A.set_login_information(db, polid, 0xEE)
        code, claim = A.claim_login_information(db, polid)
        other = A.connect()
        assert (code, bool(claim)) == (0xEE, True)
        assert A.claim_login_information(other, polid) == (0, None)
        A.restore_login_information(db, polid, code, claim)
        code, claim = A.claim_login_information(other, polid)
        A.complete_login_information(other, polid, code, claim)
        row = db.execute("SELECT info_code, info_shown_at FROM polid").fetchone()
        assert row["info_code"] == 0
        # No Code: nothing to claim, and the stored state is left alone.
        assert A.claim_login_information(db, polid) == (0, None)
        assert db.execute("SELECT info_shown_at FROM polid").fetchone()[0] \
            == row["info_shown_at"]
        A.set_login_information(db, polid, 0xEE, "always")
        assert A.claim_login_information(db, polid) == (0xEE, None)
        assert A.claim_login_information(other, polid) == (0xEE, None)
        other.close()
        print("once, retry, every-login and No Code notices: PASS")

        # The actual encrypted NICK response, with the different carriers for
        # refusals and successful-login notices.
        wire_env = {key: os.environ.get(key) for key in (
            "POL_AUTH_FRONT_PREAMBLE", "POL_AUTH_CLOCK", "POL_AUTH_MODE",
            "POL_AUTH_PING", "POL_AUTH_RSA", "POL_ACCOUNTS_ENFORCE",
            "POL_ACCOUNTS_ENFORCE_PW", "POL_LOGIN_DIGEST")}
        os.environ.update(POL_AUTH_FRONT_PREAMBLE="0", POL_AUTH_CLOCK="0",
                          POL_AUTH_MODE="welcome", POL_AUTH_PING="0",
                          POL_AUTH_RSA="0", POL_ACCOUNTS_ENFORCE="0",
                          POL_ACCOUNTS_ENFORCE_PW="0", POL_LOGIN_DIGEST="0")
        R._SELF_IP[0] = "127.0.0.1"
        sessioncrypt.remember_nick(b"STATETST")

        def decode_record(token):
            value = 0
            for symbol in token[:40].decode():
                value = (value << 5) | R.TOKEN_ALPHABET.index(symbol)
            return value.to_bytes(25, "big")

        def wire_login(index, expected, refused=False):
            server_end, client_end = socket.socketpair()
            client_end.settimeout(5)
            thread = threading.Thread(target=R.handle_authserv,
                                      args=(server_end, ("127.0.0.1", 20000 + index),
                                            51241, "pol.test", 51220), daemon=True)
            thread.start()
            reader = client_end.makefile("rb")
            try:
                greeting = reader.readline().split()[3]
                assert decode_record(greeting)[6] == 0
                client_end.sendall(f"USER x 8 * :state-test-{index}\r\n".encode())
                assert R.TOKEN0.encode() in reader.readline()
                nick = b"NICK STATETST:" + b"0" * 32 + b":8:pol"
                client_end.sendall(sessioncrypt.ofb_apply(
                    R._P0, R._S0, b"stateiv5", nick) + b"\r\n")
                response = sessioncrypt.ofb_apply(
                    R._P0, R._S0, b"stateiv5", reader.readline().rstrip(b"\r\n"))
                if refused:
                    assert response.startswith(b"ERROR :Closing Link:"), response
                    token = response.split(b"(POL ")[1].split(b")")[0]
                else:
                    assert b" 300 * " in response, response
                    token = response.split()[3]
                assert decode_record(token)[6] == expected, (index, token)
            finally:
                reader.close()
                client_end.close()
                thread.join(timeout=5)
                assert not thread.is_alive()

        try:
            A.set_login_information(db, polid, 0xEE, "once")
            A.set_reject_code(db, polid, 0xED)
            wire_login(1, 0xED, refused=True)
            assert db.execute("SELECT info_code FROM polid").fetchone()[0] == 0xEE
            A.set_reject_code(db, polid, 0)
            wire_login(2, 0xEE)
            wire_login(3, 0)
            A.set_login_information(db, polid, 0xEE, "always")
            wire_login(4, 0xEE)
            wire_login(5, 0xEE)
            print("encrypted refusal and successful-login notice carriers: PASS")
        finally:
            for key, value in wire_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        proc, url = start_panel(tmp, str(SERVICES), free_port())
        client = channel_socket = None
        try:
            owner = Client(url)
            assert owner.login("operator", "owner-test-password") == 200
            endpoints = (
                ("api/account-state", {"polid": polid, "code": 0xED, "until": future}),
                ("api/account-info", {"polid": polid, "code": 0xEE, "repeat": "once"}),
                ("api/account-kick", {"polid": polid}))
            assert owner.call(*endpoints[0])[0] == 200
            assert owner.call(*endpoints[1])[0] == 200
            assert owner.call("api/account-state", {"polid": polid, "code": 0xFF})[0] == 400
            status, rows = owner.call("api/accounts")
            assert status == 200 and rows[0]["effective_reject_code"] == 0xED, rows
            status, detail = owner.call("api/account-detail?polid=" + polid)
            assert status == 200 and detail["info_code"] == 0xEE, detail
            assert detail["effective_reject_code"] == 0xED
            status, _ = owner.call("api/mods", dict(
                action="create", username="viewer",
                password="viewer-test-password", perms=["accounts_view"]))
            assert status == 200
            viewer = Client(url)
            assert viewer.login("viewer", "viewer-test-password") == 200
            assert viewer.call("api/accounts")[0] == 200
            for endpoint, body in endpoints:
                assert viewer.call(endpoint, body)[0] == 403, endpoint

            # With nobody answering, a kick fails as "unavailable" and leaves
            # no request behind for a later auth service to act on.
            status, body = owner.call(*endpoints[2])
            assert status == 503, (status, body)
            assert kv.llen(R.KICK_QUEUE) == 0, kv.lrange(R.KICK_QUEUE)

            # No auth handler is running for this socket: the kick request
            # must itself deliver KILL and retire all captured session rows.
            A.open_session(db, mid, iv=b"oldhop01")
            A.open_session(db, mid, iv=b"livehop1")
            channel_socket, client = socket.socketpair()
            client.settimeout(5)
            sess = R.ChatSession(b"STATETST", b"pol.test", "127.0.0.1",
                                 channel_socket, R._P0, R._S0, b"livehop1",
                                 member=member)
            R._register_account_channel(mid, sess)

            def answer_one():
                raw = None
                for _ in range(20):          # up to 10 s, in short reads
                    raw = kv.pop(R.KICK_QUEUE, timeout=0.5)
                    if raw:
                        break
                done = R._kick_request(raw) if raw else None
                if done is not None:
                    R._kick_answer(*done)

            thread = threading.Thread(target=answer_one, daemon=True)
            thread.start()
            status, result = owner.call(*endpoints[2])
            assert status == 200 and result["kicked"] == 1, (status, result)
            encrypted = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                encrypted.extend(chunk)
            plain = sessioncrypt.ofb_apply(R._P0, R._S0, b"livehop1",
                                          bytes(encrypted).split(b"\r\n")[0])
            assert b" KILL STATETST :Administrator disconnect" in plain, plain
            assert not A.member_online(db, mid)
            thread.join(timeout=5)
            assert not thread.is_alive()
            actions = {row["action"] for row in owner.call("api/audit")[1]["rows"]}
            assert {"changed an account login refusal",
                    "changed an account login notice",
                    "kicked an account"} <= actions, actions
            print("owner permissions, audit trail and the kick request: PASS")
        finally:
            for sock in (client, channel_socket):
                if sock is not None:
                    sock.close()
            stop(proc)
            db.close()
    print("account state: OK")


if __name__ == "__main__":
    main()
