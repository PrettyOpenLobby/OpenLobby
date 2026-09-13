#!/usr/bin/env python3
"""Self-test for the real-time friend-presence push.

Covers everything about presence that does NOT need a live client: the watcher
lookup (`accounts.friend_watchers`), the in-process `PresenceRegistry`, the
`_broadcast_presence` fan-out (who gets a line and who does not), and the
`_presence_lines` wire-format builder in every mode. The one thing it cannot
assert is that the client APPLIES the line -- that is the reverse-engineered wire
format, still pending, and is why the push is disabled by default. See
`_presence_lines` in responders.py.

    python tools/presence_test.py        # exit 0 on success, prints a summary

Deliberately uses a throwaway on-disk DB (accounts.connect wants a path and runs
the schema for us) pointed at by POL_ACCOUNTS_DB, because `_broadcast_presence`
opens its own connection from that env var -- so the test exercises the real code
path, not a hand-passed handle.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "services"))

import accounts


class FakeSession:
    """Stands in for a ChatSession: records what would go on the wire.

    `_broadcast_presence` only ever touches `.nick`, `.srv`, `.member` and
    `.send()`, so this is the whole surface. `send()` returns True like a live
    write and stashes the lines for assertions.
    """

    def __init__(self, nick, member_row=None, srv=b"pol"):
        self.nick = nick if isinstance(nick, bytes) else nick.encode()
        self.srv = srv
        self.member = member_row
        self.sent = []
        self.alive = True

    def send(self, lines, pad_override=None):
        self.sent.append(list(lines))
        return True


def build_db():
    """Alice and Bob are mutual friends; Carol friends Alice one-way; Dave is a
    stranger. Returns (path, {name: (member_id, handle_id)})."""
    path = os.path.join(tempfile.mkdtemp(prefix="poltest-"), "accounts.db")
    c = accounts.connect(path)
    ids = {}
    for name in ("Alice", "Bob", "Carol", "Dave"):
        m = accounts.ensure_member(c, name)
        h = c.execute("SELECT id FROM handle WHERE member_id=?",
                      (m["id"],)).fetchone()["id"]
        ids[name] = (int(m["id"]), int(h))
    # Alice <-> Bob mutual; Carol -> Alice one-way; Dave friendless.
    accounts.add_friend(c, ids["Alice"][1], "Bob", peer_handle=ids["Bob"][1])
    accounts.add_friend(c, ids["Bob"][1], "Alice", peer_handle=ids["Alice"][1])
    accounts.add_friend(c, ids["Carol"][1], "Alice", peer_handle=ids["Alice"][1])
    # An unsettled request must NOT count as a watcher.
    accounts.add_friend(c, ids["Dave"][1], "Alice", peer_handle=ids["Alice"][1],
                        status=accounts.STATUS_PENDING)
    c.close()
    return path, ids


def main():
    path, ids = build_db()
    os.environ["POL_ACCOUNTS_DB"] = path
    # Import AFTER the env is set so nothing caches the wrong DB. responders pulls
    # in the whole service, so keep the import local to the test.
    import responders as R

    c = accounts.connect(path)

    # 1. friend_watchers -------------------------------------------------------
    w = accounts.friend_watchers(c, ids["Alice"][0])
    watcher_members = sorted(m for m, _wh, _sh in w)
    assert watcher_members == sorted([ids["Bob"][0], ids["Carol"][0]]), w
    # every row names Alice's handle as the subject
    assert all(sh == ids["Alice"][1] for _m, _wh, sh in w), w
    # Dave's PENDING row is excluded; Bob has no watchers except Alice
    assert [m for m, _, _ in accounts.friend_watchers(c, ids["Bob"][0])] == \
        [ids["Alice"][0]]
    print("  friend_watchers: OK")

    # 2. PresenceRegistry ------------------------------------------------------
    reg = R.PresenceRegistry()
    s1 = FakeSession("Alice-1")
    s2 = FakeSession("Alice-2")
    assert not reg.is_online(ids["Alice"][0])
    reg.register(ids["Alice"][0], s1)
    reg.register(ids["Alice"][0], s2)
    reg.register(ids["Alice"][0], s1)                    # idempotent
    assert reg.is_online(ids["Alice"][0])
    assert len(reg.sessions_for(ids["Alice"][0])) == 2
    reg.unregister(ids["Alice"][0], s1)
    assert [s.nick for s in reg.sessions_for(ids["Alice"][0])] == [b"Alice-2"]
    reg.unregister(ids["Alice"][0], s2)
    assert not reg.is_online(ids["Alice"][0])            # member dropped when empty
    reg.unregister(ids["Alice"][0], s2)                  # no-op, no raise
    print("  PresenceRegistry: OK")

    # 3. _presence_lines format builder ---------------------------------------
    os.environ.pop("POL_PRESENCE_PUSH", None)
    assert R._presence_lines(b"W", b"pol", b"Alice", 0x123, "online", slot=0) == []
    os.environ["POL_PRESENCE_PUSH"] = "1"
    os.environ["POL_PRESENCE_FMT"] = "away"
    on = R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "online")
    off = R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "offline")
    assert on and b"305" in on[0] and b"Bob" in on[0] and b"Alice" in on[0], on
    assert off and b"306" in off[0], off
    os.environ["POL_PRESENCE_FMT"] = "notice"
    nline = R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "offline")
    assert nline and nline[0].startswith(b"NOTICE Bob :"), nline
    print("  _presence_lines: OK")

    # 3a. THE DEFAULT IS SILENCE, and that is the assertion worth having.
    # `push` was briefly the default until its 72-byte record was decoded as a
    # MESSAGE-ARRIVAL announcement rather than a presence one: every flip drew a
    # phantom inbox entry (To: player / From: the same player / no subject), live
    # 2026-08-16. The long field record in `_broadcast_presence` is what actually
    # repaints the row, so this short line must add nothing.
    os.environ.pop("POL_PRESENCE_FMT", None)             # exercise the DEFAULT
    assert R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "offline") == []
    assert R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "online") == []
    print("  _presence_lines default (silent): OK")

    # ...and the push format is still BUILDABLE, because it is one env var away
    # and prod was found still set to it. The record's own bytes are pinned
    # against SE in tools/push_test.py; what matters here is that it addresses
    # the watcher, and that it refuses when it has no guid to name the friend.
    os.environ["POL_PRESENCE_FMT"] = "push"
    p = R._presence_lines(b"Bob", b"pol", b"Alice", 0x123, "offline")
    assert p and b" NOTICE Bob :" in p[0], p
    assert b"!~x@ NOTICE" in p[0], p                     # SE's empty host
    assert R._presence_lines(b"Bob", b"pol", b"Alice", None, "offline") == []
    print("  _presence_lines push (opt-in): OK")

    # 3b. xxl format: byte-exact payload round-trips through the client's codec ---
    os.environ["POL_PRESENCE_FMT"] = "xxl"
    import struct as _s
    g = accounts.handle_guid(ids["Alice"][1])
    xl = R._presence_lines(b"Bob", b"pol", b"Alice", g, "online", slot=3, seq=1000)
    assert xl and xl[0].startswith(b"PRIVMSG #XXL"), xl
    # xxl needs a slot + guid; without them it must decline (can't address a friend)
    assert R._presence_lines(b"Bob", b"pol", b"Alice", g, "online", slot=None) == []
    b64 = xl[0].split(b" :", 1)[1].decode()
    inv = {c: i for i, c in enumerate(R._B64)}
    dec = bytearray()
    for i in range(0, len(b64), 4):
        v = 0
        for ch in b64[i:i + 4]:
            v = (v << 6) | inv[ch]
        dec += bytes([(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff])
    const = (0x1c273e45 << 32) | 0x67891133
    assert _s.unpack_from("<Q", dec, 0)[0] == (g ^ const)     # guid1 = served ^ const
    assert dec[0x10] == 2 and dec[0x1c] == 3         # action / 2:3 record index
    # +0x18 is the HANDLE slot, not the record index (0x37dba7f compares it
    # against (slot+0x9c >> 13) & 0x3f, which the 2:3 record's dword0 bits 7..12
    # fill). `g` here is a real handle guid, so it resolves through the same map
    # 0:9 serves; an unknown guid falls back to the record index.
    hs = R._friend_handle_slots().get(g)
    assert dec[0x18] == (hs if hs is not None else 3), (dec[0x18], hs)
    # THE GATE BYTES. The handler bails before ever reading the fields unless
    # all three hold (0x37db97e / 0x37db989 / 0x37db994). Serving +0x1b = 1, as
    # this encoder did until 2026-08-13, is an immediate bail -- the live test
    # that "was accepted and changed nothing" never reached the apply path.
    assert dec[0x19] & 1 and dec[0x1a] == 0 and dec[0x1b] == 0
    # THE SECOND CHUNK: declared length under the 0x158 cap, and the ext record
    # starts right after the 0x48-byte main block with its flags byte first.
    n38 = _s.unpack_from("<I", dec, 0x38)[0]
    assert 0 < n38 < 0x158, n38
    assert dec[0x48] & 0x01, hex(dec[0x48])           # bit0 = the action block
    assert len(dec) >= 0x48 + R._PRESENCE_EXT_LEN
    # ...and with a comment, bit 0x20 plus the text at ext+0x08 (UTF-16LE).
    cl = R._presence_lines(b"Bob", b"pol", b"Alice", g, "online", slot=3,
                           subject_comment="hi there")[0].split(b" :", 1)[1].decode()
    cd = bytearray()
    for i in range(0, len(cl), 4):
        v = 0
        for ch in cl[i:i + 4]:
            v = (v << 6) | inv[ch]
        cd += bytes([(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff])
    assert cd[0x48] & 0x20, hex(cd[0x48])
    assert cd[0x50:0x60].decode("utf-16-le").startswith("hi there")
    assert _s.unpack_from("<H", dec, 0x3e)[0] == 0x0f80 and dec[0x42] == 1
    assert R._presence_lines(b"Bob", b"pol", b"Alice", g, "offline", slot=3)[0].split(
        b" :", 1)[1]  # offline still builds
    off_dec = bytearray()
    ob = R._presence_lines(b"Bob", b"pol", b"Alice", g, "offline", slot=3)[0].split(b" :", 1)[1].decode()
    for i in range(0, len(ob), 4):
        v = 0
        for ch in ob[i:i + 4]:
            v = (v << 6) | inv[ch]
        off_dec += bytes([(v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff])
    assert off_dec[0x10] == 0                                  # offline -> action 0
    print("  _presence_lines xxl (round-trip): OK")

    # 3c. _friend_slot matches the 2:3 enumeration order -----------------------
    # Bob's friend list is [Alice]; Alice sits at slot 0 for Bob.
    _fs = R._friend_slot(c, ids["Bob"][1], ids["Alice"][1])
    _bl = [(r["peer_name"], r["peer_handle"], r["kind"]) for r in
           accounts.list_friends(c, ids["Bob"][1], status=None)]
    assert _fs == 0, (_fs, ids, _bl)
    assert R._friend_slot(c, ids["Bob"][1], ids["Carol"][1]) is None   # not a friend
    print("  _friend_slot: OK")

    # 4. _broadcast_presence fan-out ------------------------------------------
    # Bob online (two sockets), Carol offline. Alice going online must reach BOTH
    # of Bob's sockets and nobody else; Carol (offline) gets nothing; Dave (only a
    # pending request) is never a target.
    os.environ["POL_PRESENCE_FMT"] = "away"
    R.PRESENCE = R.PresenceRegistry()                    # clean global for the test
    bob1 = FakeSession("Bob-1")
    bob2 = FakeSession("Bob-2")
    R.PRESENCE.register(ids["Bob"][0], bob1)
    R.PRESENCE.register(ids["Bob"][0], bob2)
    sent = R._broadcast_presence(ids["Alice"][0], "online", subject_name="Alice")
    assert sent == 2, sent
    assert len(bob1.sent) == 1 and len(bob2.sent) == 1
    assert b"305" in bob1.sent[0][0]                     # online -> unaway
    # Carol offline -> no session -> untouched even though she watches Alice
    assert R.PRESENCE.sessions_for(ids["Carol"][0]) == []
    # offline broadcast reaches Bob too, as 306
    bob1.sent.clear(); bob2.sent.clear()
    sent = R._broadcast_presence(ids["Alice"][0], "offline", subject_name="Alice")
    assert sent == 2 and b"306" in bob1.sent[0][0]
    # a member with no watchers online pushes to nobody
    assert R._broadcast_presence(ids["Dave"][0], "online", subject_name="Dave") == 0
    print("  _broadcast_presence: OK")

    # 5. dry-run ("log") mode: computes fan-out, logs, sends NOTHING ----------
    os.environ["POL_PRESENCE_PUSH"] = "log"
    bob1.sent.clear(); bob2.sent.clear()
    sent = R._broadcast_presence(ids["Alice"][0], "online", subject_name="Alice")
    assert sent == 0, sent                               # log mode never sends
    assert bob1.sent == [] and bob2.sent == []           # no socket writes
    print("  dry-run (log) mode: OK")

    # 6. disabled == truly inert ----------------------------------------------
    os.environ.pop("POL_PRESENCE_PUSH", None)
    bob1.sent.clear()
    assert R._broadcast_presence(ids["Alice"][0], "online", subject_name="Alice") == 0
    assert bob1.sent == []                               # nothing written when off
    print("  disabled-is-inert: OK")

    # ---- a closing channel is NOT a logout while another one is live --------
    # Reported 2026-08-17: "I can't see my friends as being online even though
    # they are". accounts.close_sessions() deletes EVERY row a member has, so
    # with two channels open either one closing wiped the member's presence and
    # pushed offline at every friend -- 175 times in one day's log, each one
    # next to the ordinary hop dance, while the surviving channel was still
    # answering keepalive PINGs.
    print("a closing channel is not a logout while another is live")
    mid = ids["Alice"][0]
    a1, a2 = FakeSession(b"Alice-1"), FakeSession(b"Alice-2")
    R.PRESENCE.register(mid, a1)
    R.PRESENCE.register(mid, a2)
    assert R._member_has_other_channel(mid, a1) is True
    assert R._member_has_other_channel(mid, a2) is True
    print("  two live channels: either one closing sees the other: OK")

    # the crash path: a registered but DEAD session must not hold presence open,
    # or a member whose socket died stays online for ever.
    a2.alive = False
    assert R._member_has_other_channel(mid, a1) is False
    print("  a dead-but-registered channel does not block the logout: OK")

    # and the last one out really is the last one out
    a2.alive = True
    R.PRESENCE.unregister(mid, a2)
    assert R._member_has_other_channel(mid, a1) is False
    print("  the last channel closing IS a logout: OK")

    # the kill switch puts the old unconditional behaviour back
    R.PRESENCE.register(mid, a2)
    os.environ["POL_PRESENCE_LAST_CHANNEL"] = "0"
    assert R._member_has_other_channel(mid, a1) is False
    os.environ.pop("POL_PRESENCE_LAST_CHANNEL")
    assert R._member_has_other_channel(mid, a1) is True
    print("  POL_PRESENCE_LAST_CHANNEL=0 reverts: OK")
    R.PRESENCE.unregister(mid, a1)
    R.PRESENCE.unregister(mid, a2)

    c.close()
    print("presence_test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
