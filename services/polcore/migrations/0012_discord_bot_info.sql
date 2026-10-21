-- What Discord last said about each registry bot (polcore/botcreds.describe,
-- as JSON): its interactions endpoint, whether it is public, whether it
-- requires an OAuth2 code grant, which privileged intents are switched on and
-- which slash commands it has. The admin page's setup checklist reads it; it
-- is refreshed on Save and Check. Empty = never checked.

ALTER TABLE discord_bot ADD COLUMN discord_info TEXT NOT NULL DEFAULT '';
