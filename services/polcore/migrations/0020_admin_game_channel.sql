-- admin_game_updater gets the update channels a paired game's setup program declares beside
-- its updater, and the one the game is on (docs/installs.md, "Update channels").
-- services/adminpair.py reads and writes them.
--
-- channels    the declared channel names as JSON text, for example ["stable","dev"]; NULL
--             when the updater declares none (the panel then offers no switch). Part of what
--             the owner approves: a change makes the updater 'requested' again.
-- channel     the channel the game is on as far as the panel knows: written only after a
--             switch job the deployer ran ended ok. 'stable' until then.
-- channel_by, channel_at   who switched it last and when; NULL while it never was.
--
-- Times are unix seconds (DOUBLE PRECISION), as in admin_game (0016).

ALTER TABLE admin_game_updater
    ADD COLUMN channels   TEXT,
    ADD COLUMN channel    TEXT NOT NULL DEFAULT 'stable' CHECK (channel ~ '^[a-z]{1,12}$'),
    ADD COLUMN channel_by TEXT,
    ADD COLUMN channel_at DOUBLE PRECISION;
