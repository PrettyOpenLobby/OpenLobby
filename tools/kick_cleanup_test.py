#!/usr/bin/env python3
"""A kick removes old auth-hop rows without deleting a login still opening.

    python tools/kick_cleanup_test.py

Drives core/authkick.py directly with stand-in channels: a kick retires every
session row the member had and the launch's resume flags; a login between
opening its session row and registering its channel is spared, and is retired
when it finishes unless it did register; a resume that arrives after the kick
retired its launch is refused.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, HERE)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

with tempfile.TemporaryDirectory(prefix="kick-cleanup-",
                                 ignore_cleanup_errors=True) as tmp:
    os.environ["POL_DATA_DIR"] = tmp
    os.environ["POL_LOG_DIR"] = tmp
    os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "resources")
    os.environ["POL_ACCOUNTS_ENFORCE"] = "0"
    os.environ["POL_ACCOUNTS_ENFORCE_PW"] = "0"

    import accounts
    import responders as R

    class Conn:
        def shutdown(self, how):
            pass

    class Channel:
        def __init__(self, member):
            self.member = member
            self.alive = True
            self.admin_kicked = False
            self.killed_dup = False
            self.sid = "kick-first"
            self.srv = "pol.test"
            self.nick = b"KICKTEST1"
            self.peer_ip = b"127.0.0.1"
            self.conn = Conn()
            self.fail_send = False

        def send(self, lines):
            if self.fail_send:
                raise RuntimeError("channel already closing")
            return True

    db = accounts.connect()
    member = accounts.ensure_member(db, "KICKTEST1")
    mid = int(member["id"])
    old = accounts.open_session(db, mid, iv=b"oldhop01")
    live = accounts.open_session(db, mid, iv=b"livehop1")
    channel = Channel(member)
    channel.fail_send = True
    R.PRESENCE.register(mid, channel)
    second = Channel(member)
    second.sid = "kick-second"
    R.PRESENCE.register(mid, second)
    for sess in (channel, second):
        R.session_bind(sess.sid)
        R._session_put(sess.sid, viewer_open=True, channel_open=True)

    assert R._kick_live_account(member["polid"]) == 2
    assert accounts.get_session(db, old) is None
    assert accounts.get_session(db, live) is None
    assert not accounts.member_online(db, mid)
    for sess in (channel, second):
        assert not R._SESSIONS[sess.sid]["viewer_open"]
        assert not R._SESSIONS[sess.sid]["channel_open"]
    second.alive = False

    channel.fail_send = False
    channel.admin_kicked = False
    R.session_bind(channel.sid)
    R._session_put(channel.sid, viewer_open=True, channel_open=True)
    accounts.open_session(db, mid, iv=b"oldhop02")
    pending_db, pending_member, reject, pending = R.resolve_account(
        b"KICKTEST1", "127.0.0.1", b"newhop01")
    assert reject is None and pending is not None
    assert R._kick_live_account(member["polid"]) == 1
    assert accounts.member_online(db, mid)
    assert R._SESSIONS[channel.sid]["viewer_open"]
    R._finish_pending_session(pending, pending_member, "test")
    assert not accounts.member_online(db, mid)
    assert not R._SESSIONS[channel.sid]["viewer_open"]
    pending_db.close()

    channel.admin_kicked = False
    R._session_put(channel.sid, viewer_open=True, channel_open=True)
    accounts.open_session(db, mid, iv=b"oldhop03")
    pending_db, pending_member, reject, pending = R.resolve_account(
        b"KICKTEST1", "127.0.0.1", b"newhop02")
    assert reject is None and pending is not None
    assert R._kick_live_account(member["polid"]) == 1
    new_channel = Channel(pending_member)
    R._register_account_channel(mid, new_channel, pending)
    assert accounts.member_online(db, mid)
    assert accounts.get_session(db, pending) is not None
    assert R._SESSIONS[channel.sid]["viewer_open"]
    R._finish_pending_session(pending, pending_member, "test")
    assert accounts.get_session(db, pending) is not None
    pending_db.close()

    # Resume flags and registration are published together, before a kick can
    # capture the socket. A retired launch cannot be rearmed by a late resume.
    new_channel.alive = False
    resumed = Channel(member)
    resumed.sid = "kick-resumed"
    R._session_put(resumed.sid, viewer_open=False, channel_open=True)
    accounts.open_session(db, mid, iv=b"resumhop")
    assert R._register_account_channel(mid, resumed, resume=True)
    assert R._SESSIONS[resumed.sid]["viewer_open"]
    assert R._kick_live_account(member["polid"]) == 1
    assert not R._SESSIONS[resumed.sid]["viewer_open"]
    assert not R._SESSIONS[resumed.sid]["channel_open"]
    late_resume = Channel(member)
    late_resume.sid = resumed.sid
    assert not R._register_account_channel(mid, late_resume, resume=True)
    assert late_resume not in R.PRESENCE.sessions_for(mid)
    assert not R._SESSIONS[resumed.sid]["channel_open"]
    db.close()

print("kick cleanup: PASS")
