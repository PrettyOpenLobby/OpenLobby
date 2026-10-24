-- dev_codes gets the kind each code is made for (docs/deployer.md, "Dev access codes"). One panel
-- can issue codes of several kinds (POL_DEV_ISSUER_KINDS), each redeeming to its own access code
-- with its own Forgejo reader user. services/admindevcodes.py reads and writes it.
--
-- kind     the kind's key, a lower-case letter then up to 11 letters or digits; '' for the
--          unnamed kind of a panel without POL_DEV_ISSUER_KINDS, which every code made before
--          this migration belongs to
--
-- One live code per install becomes one live code per install and kind: an install may hold a
-- code of each kind at once.

ALTER TABLE dev_codes
    ADD COLUMN kind TEXT NOT NULL DEFAULT '' CHECK (kind ~ '^([a-z][a-z0-9]{0,11})?$');

DROP INDEX dev_codes_install;

CREATE UNIQUE INDEX dev_codes_install_kind ON dev_codes (install_id, kind)
    WHERE install_id IS NOT NULL AND revoked_at IS NULL;
