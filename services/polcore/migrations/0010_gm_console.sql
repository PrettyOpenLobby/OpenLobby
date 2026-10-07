-- GM Call console in Discord: alerts + private threads per call.
--
-- discord_gm_channel   one row per guild that has bound a GM-alerts channel.
--                      last_ticket is the newest ticket file already alerted
--                      on, baselined on bind so switching the feature on never
--                      dumps the backlog. Moving the channel keeps the
--                      baseline. Mirrors the discord_announce shape.
-- discord_gm_alert     one row per Knock-button alert we posted, keyed by
--                      ticket_id. Remembers which message to disable when
--                      somebody knocks.
-- discord_gm_thread    one open call being handled in a private Discord
--                      thread. Room is the gmchat channel (`#gmcallNNN`);
--                      one room can have at most one live thread (partial
--                      unique index on closed_at IS NULL).
--
-- Types follow 0003_admin_discord.sql: TEXT ids, DOUBLE PRECISION times.

CREATE TABLE discord_gm_channel (
    guild_id    TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    role_id     TEXT,
    last_ticket TEXT NOT NULL DEFAULT '',
    bound_at    DOUBLE PRECISION NOT NULL,
    bound_by    TEXT
);

CREATE TABLE discord_gm_alert (
    ticket_id  TEXT PRIMARY KEY,
    room       TEXT NOT NULL DEFAULT '',
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    posted_at  DOUBLE PRECISION NOT NULL,
    claimed_by TEXT,
    claimed_at DOUBLE PRECISION
);

CREATE TABLE discord_gm_thread (
    room          TEXT NOT NULL,
    ticket_id     TEXT NOT NULL,
    thread_id     TEXT PRIMARY KEY,
    guild_id      TEXT NOT NULL,
    parent_id     TEXT NOT NULL,
    knocker_id    TEXT NOT NULL,
    knocker_name  TEXT,
    knocker_nick  TEXT,
    created_at    DOUBLE PRECISION NOT NULL,
    last_msg_id   TEXT,
    last_relay_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed_at     DOUBLE PRECISION
);

-- One open thread per room; a closed row is retained for audit.
CREATE UNIQUE INDEX discord_gm_thread_open ON discord_gm_thread (room)
    WHERE closed_at IS NULL;
