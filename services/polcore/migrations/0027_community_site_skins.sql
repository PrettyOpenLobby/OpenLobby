-- community_site.skin: room for skins past Square Enix's twelve. 0-11 are SE's; 12 and up
-- are custom ones drawn in the same layout (admincomsites.CUSTOM_SKINS). The panel checks
-- a skin against the skins it knows; the table only keeps it a small non-negative number.

ALTER TABLE community_site DROP CONSTRAINT IF EXISTS community_site_skin_check;
ALTER TABLE community_site ADD CONSTRAINT community_site_skin_check CHECK (skin BETWEEN 0 AND 63);
