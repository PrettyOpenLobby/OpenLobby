-- friend_request_heal: when the friend request heal (services/core/lobbymail.py,
-- _friend_request_heal) last gave a recipient back a request, and how many
-- times. It was a dict in process memory, so every restart of the login
-- container re-armed it and a request the recipient kept retiring came back
-- once per restart as well as once per TTL.
--
--   handle_id    the recipient (the handle holding the `invited` row)
--   peer_handle  the asker
--   healed_at    unix time of the last restore or mint
--   heals        restores and mints since the asker last sent a request
--
-- A fresh request from the asker (a client 3:1 of kind 0x8080) resets the
-- row, so a new request is healed like the first one.

CREATE TABLE friend_request_heal (
    handle_id    BIGINT NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    peer_handle  BIGINT NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    healed_at    DOUBLE PRECISION NOT NULL,
    heals        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (handle_id, peer_handle)
);

-- friend_ignore: the client's IGNORE (block) table. polcore keeps it apart
-- from the friend table (100 entries beside the friend table's 200) and
-- carries both in the same 2:3 / 2:6: a row whose bitfield head has bit 0x10
-- set belongs here, and its +0x08 is its index in this table, not a friend
-- slot. A handle is either a friend or ignored, never both.
--
--   handle_id    who holds the ignore (the blocker)
--   slot         the client's ignore-table index, 0..99
--   peer_handle  the ignored handle, when it is one of ours
--   peer_name    the name the client wrote in the row
--   scope        1 = that handle only, 2 = every handle of its PlayOnline ID
--   head         the low 16 bits of the row's bitfield as the client wrote it
--                (valid, ignore, scope, handle slot)

CREATE TABLE friend_ignore (
    handle_id    BIGINT NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    slot         INTEGER NOT NULL,
    peer_handle  BIGINT REFERENCES handle(id) ON DELETE SET NULL,
    peer_name    TEXT NOT NULL,
    scope        INTEGER NOT NULL DEFAULT 1,
    head         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (handle_id, slot)
);
CREATE INDEX idx_friend_ignore_peer ON friend_ignore (peer_handle);
