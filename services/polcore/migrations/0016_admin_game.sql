-- admin_game: games the panel's owner added in Settings > Modules ("Add a game"), or that
-- paired with the panel through their own setup program (docs/installs.md, "Pairing").
-- services/adminpair.py reads and writes them; adminmodules treats a row exactly like
-- POL_ADMIN_GAME_<KEY>_URL and _TOKEN, and the environment wins when both name a key.
--
-- key           the module key, [a-z0-9]{1,16}, the same key the game's module.json gives
-- url, token    the game's admin port and its token (never sent back to a browser)
-- title         what the game called itself when it was added (module.json "title")
-- how           'manual' (added by hand) or 'paired' (through its setup program)
-- pairing_hash  sha256 (hex) of the pairing secret the setup program keeps; NULL when the
--               game was added by hand or was unpaired. Only the hash is kept.
--
-- admin_pair_code: the one-time codes "Pair with a setup program" shows: 8 characters, good
-- for 10 minutes and one use. Only the code's sha256 is kept. used_key is the game that
-- used it.
--
-- Times are unix seconds (DOUBLE PRECISION), as in the admin_* tables (0003). Nothing here
-- reaches the account tables, so a panel run without the PlayOnline core has them too.

CREATE TABLE admin_game (
    key           TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    token         TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    how           TEXT NOT NULL CHECK (how IN ('manual', 'paired')),
    added_by      TEXT,
    added_at      DOUBLE PRECISION NOT NULL,
    updated_at    DOUBLE PRECISION,
    pairing_hash  TEXT
);

CREATE TABLE admin_pair_code (
    code_hash   TEXT PRIMARY KEY,
    created_by  TEXT,
    created_at  DOUBLE PRECISION NOT NULL,
    expires_at  DOUBLE PRECISION NOT NULL,
    used_at     DOUBLE PRECISION,
    used_key    TEXT
);
