-- A linked Discord account's avatar, so the admin panel can show the player's
-- Discord picture next to their name. Only the hash is kept, as Discord gives it
-- in GET /users/{id}; the CDN URL is built from it (discordlink.avatar_url) at the
-- size each view wants. NULL means no custom avatar, or not looked up yet; the
-- panel falls back to a letter monogram. avatar_at is when the bridge last asked,
-- so its refresh sweep (polbridge.avatar_watch) rechecks each link at most once a
-- day rather than asking Discord about everyone at once.
ALTER TABLE discord_link ADD COLUMN avatar    TEXT;
ALTER TABLE discord_link ADD COLUMN avatar_at DOUBLE PRECISION;

-- The link code carries whatever avatar the Discord user had when they ran
-- /playonline link, so a freshly redeemed link already has a picture instead of
-- waiting for the first refresh sweep to fill it in.
ALTER TABLE discord_code ADD COLUMN avatar TEXT;
