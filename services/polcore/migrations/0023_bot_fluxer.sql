-- The registry's bots on Fluxer as well (polcore/botcreds.py): a board posts there beside
-- Discord, never instead of it, as the bot behind fluxer_token_sealed. The admin page's Bots
-- tab writes these; a board whose row holds none keeps its POL_BOARDS_<NAME>_FLUXER_* values.
--
-- fluxer_token_sealed  the Fluxer bot token, sealed with POL_BOT_KEY as token_sealed is.
--                      Empty = none stored.
-- fluxer_channels      JSON {feed key: "id,id"}: the Fluxer channels each of the board's feeds
--                      posts in ("tm" for the board itself, "tm_auction" for its auction feed).
--                      Fluxer has no slash commands, so there is no /<name>board to pick them.
--                      A feed it leaves out keeps its POL_BOARDS_<KEY>_FLUXER_CHANNELS.
-- fluxer_info          what Fluxer last said about the token and those channels
--                      (botcreds.fluxer_describe, as JSON). Empty = never checked.

ALTER TABLE discord_bot ADD COLUMN fluxer_token_sealed TEXT NOT NULL DEFAULT '';
ALTER TABLE discord_bot ADD COLUMN fluxer_channels TEXT NOT NULL DEFAULT '';
ALTER TABLE discord_bot ADD COLUMN fluxer_info TEXT NOT NULL DEFAULT '';
