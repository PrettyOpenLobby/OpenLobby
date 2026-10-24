-- A board's Discord channels that the admin page names (polcore/botcreds.py), beside the ones
-- its /<name>board command picks: JSON {feed key: "id,id"}, as fluxer_channels is for Fluxer.
-- Today the one key is "<name>_news", the game-only news channels: the PlayOnline bridge posts
-- that game's news, events and reminders there AS THE GAME'S OWN BOT.
-- Empty = none.

ALTER TABLE discord_bot ADD COLUMN discord_channels TEXT NOT NULL DEFAULT '';
