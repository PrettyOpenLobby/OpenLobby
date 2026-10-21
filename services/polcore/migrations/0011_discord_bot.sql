-- The Discord bot registry: one row per bot, keyed by the board name the bot
-- runs under (tm, jan, fmo, ffxi, doc, fe, ...) or "bridge" for the PlayOnline
-- bridge (polbridge.py).
--
-- The admin page (Discord bots) writes it; each bot service reads its row at
-- start and restarts itself when updated_at moves (polcore/botcreds.py). A
-- service whose row is missing, or whose token cannot be unsealed, keeps its
-- POL_BOARDS_<NAME>_DISCORD_* / POL_BRIDGE_DISCORD_* environment as before.
--
-- token_sealed  the bot token, sealed with POL_BOT_KEY (botcreds.seal): the
--               key is in .env, never in this database, so a dump of it does
--               not hold a usable token. Empty = no token stored.
-- app_id, public_key, username  what GET /applications/@me answered for the
--               token when it was saved; public, stored in the clear.
-- url           the board's public page (its POL_BOARDS_<NAME>_URL); the
--               interactions endpoint is <url>/discord/interactions.
--
-- Types follow 0003_admin_discord.sql: TEXT ids, DOUBLE PRECISION times.

CREATE TABLE discord_bot (
    name         TEXT PRIMARY KEY,
    token_sealed TEXT NOT NULL DEFAULT '',
    app_id       TEXT NOT NULL DEFAULT '',
    public_key   TEXT NOT NULL DEFAULT '',
    username     TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    updated_at   DOUBLE PRECISION NOT NULL,
    updated_by   TEXT
);
