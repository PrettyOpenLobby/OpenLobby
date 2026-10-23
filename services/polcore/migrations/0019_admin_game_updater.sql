-- admin_game_updater: the updater a paired game's setup program asked the panel to run for it
-- (docs/installs.md, "Updates", the deployer-run updater). One row per game at most; it goes
-- when the game is removed from the panel or unpaired. services/adminpair.py reads and writes
-- it; the deployer runs it (docs/deployer.md, "Updaters of paired games") only once the
-- panel's owner has approved exactly this folder, command and timeout.
--
-- key              the game (admin_game.key)
-- folder           the install folder as the host sees it: a Windows path (C:\Users\x\Game)
--                  or a Linux path (/srv/game)
-- command          the argument list as JSON text, first item "python" or "python3"
-- timeout_minutes  5 to 240
-- state            'requested' (sent by the setup program, not approved) or 'approved'
-- requested_at     when the setup program last sent it (any change sets state back to
--                  'requested')
-- approved_by, approved_at   who approved it and when; NULL while requested
--
-- Times are unix seconds (DOUBLE PRECISION), as in admin_game (0016).

CREATE TABLE admin_game_updater (
    key              TEXT PRIMARY KEY REFERENCES admin_game (key) ON DELETE CASCADE,
    folder           TEXT NOT NULL,
    command          TEXT NOT NULL,
    timeout_minutes  INTEGER NOT NULL CHECK (timeout_minutes BETWEEN 5 AND 240),
    state            TEXT NOT NULL CHECK (state IN ('requested', 'approved')),
    requested_at     DOUBLE PRECISION NOT NULL,
    approved_by      TEXT,
    approved_at      DOUBLE PRECISION
);
