-- One avatar per Discord user, keyed by the Discord id itself rather than by a
-- link row, so a picture can be shown for both kinds of Discord login the admin
-- panel knows: an account the player linked through the bot (discord_link) and
-- one a moderator linked by hand (person_link, system='discord'). 0017 put the
-- avatar on discord_link, which only covered the bot links; this is the store
-- that covers both. The bridge's sweep (polbridge.avatar_watch) fills it for
-- every id in either table; the panel reads it by id. avatar NULL means no
-- custom avatar; avatar_at is when the bridge last asked.
CREATE TABLE discord_avatar (
    discord_id TEXT PRIMARY KEY,
    avatar     TEXT,
    avatar_at  DOUBLE PRECISION
);

-- Seed it with what 0017 already gathered on the bot links, so those pictures
-- show at once instead of waiting for the first sweep.
INSERT INTO discord_avatar (discord_id, avatar, avatar_at)
    SELECT discord_id, avatar, avatar_at FROM discord_link
    WHERE avatar IS NOT NULL OR avatar_at IS NOT NULL
    ON CONFLICT (discord_id) DO NOTHING;
