-- PlayOnline accounts linked to Fluxer (polbridge.py), BESIDE Discord: a member can have both,
-- and friend-list messages go to every platform they linked, each with its own notify switch
-- (operator, 2026-10-24). Same shape as discord_link.
CREATE TABLE fluxer_link (
    fluxer_id   TEXT PRIMARY KEY,
    member_id   BIGINT NOT NULL UNIQUE,
    fluxer_name TEXT,
    linked_at   DOUBLE PRECISION NOT NULL,
    notify      INTEGER NOT NULL DEFAULT 1,
    dm_channel  TEXT
);

-- A link code says which platform asked for it; the portal page (ucscgi kinou 90) redeems
-- both. discord_id holds the Fluxer user id for a Fluxer code.
ALTER TABLE discord_code ADD COLUMN platform TEXT NOT NULL DEFAULT 'discord';

-- Fluxer has no buttons or forms, so a message's Reply is a reply to the bot's DM: this maps
-- each DM the bridge sent on Fluxer to the discord_reply row it answers.
CREATE TABLE fluxer_dm (
    message_id TEXT PRIMARY KEY,
    reply_id   TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX fluxer_dm_at ON fluxer_dm (created_at);
