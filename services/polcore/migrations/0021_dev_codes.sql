-- dev_codes: single-use dev access codes this panel hands out as the issuer (docs/deployer.md,
-- "Dev access codes"). The owner makes them in Settings > Modules, "Dev access codes"; an
-- install redeems one with POST /api/dev-code/redeem and gets an olx1. access code back.
-- services/admindevcodes.py reads and writes them.
--
-- code          the short code, 12 characters of A-Z and 2-9 without 0/O and 1/I/L, kept
--               without the dashes it is shown with (ABCD-EFGH-JKMN)
-- note          who it is for, as the owner typed it
-- made_by       the owner who made it
-- created_at    when
-- redeemed_at   when an install first redeemed it; NULL while unused
-- install_id    the install it is bound to (the deployer's random install id); NULL while
--               unused. One live code per install: a revoked code frees its install.
-- token_ref     the id of the registry token minted for it on Forgejo, when the panel mints
--               tokens (POL_DEV_ISSUER_FORGEJO_*); NULL otherwise
-- token         that token, so a second redeem from the same install answers the same
--               access code; NULL without minting, and blanked on revoke
-- revoked_at    when the owner revoked it; a revoked code redeems nothing
--
-- Times are unix seconds (DOUBLE PRECISION), as in the admin_* tables (0003).

CREATE TABLE dev_codes (
    code         TEXT PRIMARY KEY CHECK (code ~ '^[A-Z2-9]{12}$'),
    note         TEXT NOT NULL DEFAULT '',
    made_by      TEXT,
    created_at   DOUBLE PRECISION NOT NULL,
    redeemed_at  DOUBLE PRECISION,
    install_id   TEXT,
    token_ref    TEXT,
    token        TEXT,
    revoked_at   DOUBLE PRECISION
);

CREATE UNIQUE INDEX dev_codes_install ON dev_codes (install_id)
    WHERE install_id IS NOT NULL AND revoked_at IS NULL;
