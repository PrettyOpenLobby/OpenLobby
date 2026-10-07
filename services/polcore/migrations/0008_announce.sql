-- Server announcements posted into Discord channels.
--
-- discord_announce         one row per guild the bot posts announcements in.
--                          last_serial is the highest announcement serial
--                          already sent to that channel: baselined on bind
--                          from the current max so switching the feature on
--                          never dumps the backlog. Moving the channel keeps
--                          the baseline: the old channel's posts stand.
-- discord_announce_role    per-guild, per-content role @-mentioned in a post.
--                          `content` is a newsgen CONTENTS key (playonline,
--                          ffxi, tetra, jan, fmo, doc, fe, ffxiv, eqii,
--                          extras); absence = no ping for that game.
--
-- Types follow 0003_admin_discord.sql: TEXT ids, DOUBLE PRECISION times.

CREATE TABLE discord_announce (
    guild_id    TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    last_serial BIGINT NOT NULL DEFAULT 0,
    bound_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE discord_announce_role (
    guild_id  TEXT NOT NULL,
    content   TEXT NOT NULL,
    role_id   TEXT NOT NULL,
    PRIMARY KEY (guild_id, content)
);
