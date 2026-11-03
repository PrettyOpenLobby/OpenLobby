-- community_site: fan sites registered for the Community Site Tool, the 480x100 banner a
-- fan site embeds with comTool('<id>') (POL_COMTOOL_URL names the site that serves it).
-- The owner, or a moderator with the comsites permission, adds them under News >
-- Community sites; the banner's site record, news and directory are read from here.
--
-- id            the site ID the embed code carries, from community_site_id (1001 up). A
--               number is never handed out twice: a removed site's code may still be
--               pasted somewhere, and it must not start showing someone else's site
-- name          the site's name, shown in the banner's top bar
-- url           the site's address, where the directory and the featured/random buttons go
-- game          which game the site is about (the news file's content names: ffxi, fmo,
--               fe, playonline...); it picks the banner's news, banners and featured sites
-- skin          0-11, the banner's look; NULL takes the game's own skin
-- about         one line for the directory
-- featured      may be the featured site the banner's upper button offers
-- listed        shown in the directory and among the random sites
-- owner_polid   the PlayOnline ID of whoever runs the site, when they have one; NULL otherwise
-- note          the staff's own note, never shown on the banner
-- made_by       the panel login that added it
-- created_at, updated_at   unix seconds (DOUBLE PRECISION), as in the admin_* tables (0003)

CREATE SEQUENCE community_site_id START 1001;

CREATE TABLE community_site (
    id           INTEGER PRIMARY KEY CHECK (id > 0),
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    url          TEXT NOT NULL CHECK (url ~ '^https?://'),
    game         TEXT NOT NULL DEFAULT 'playonline',
    skin         INTEGER CHECK (skin BETWEEN 0 AND 11),
    about        TEXT NOT NULL DEFAULT '',
    featured     BOOLEAN NOT NULL DEFAULT FALSE,
    listed       BOOLEAN NOT NULL DEFAULT TRUE,
    owner_polid  TEXT,
    note         TEXT NOT NULL DEFAULT '',
    made_by      TEXT,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
