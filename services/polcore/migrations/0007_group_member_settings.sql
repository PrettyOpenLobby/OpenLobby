-- group_member settings: what a member says about THEMSELF in one group,
-- sent with lobby 7:11 (KChgMyGrpStatus) and served back in their own 7:12
-- group record. Offsets as Project Crystal Server reads both (the request and
-- the record share them):
--
--   +0x08  0x64 bytes UTF-16LE  my_comment     the member's comment for this group
--   +0x6E  u8                   my_handle_pos  the handle slot they appear under
--   +0x6F  u8                   my_status      their online status in this group
--
-- NULL means the member never sent a 7:11 for this group, and the record keeps
-- what it served before (services/core/friendgroups.py, _group_record).

ALTER TABLE group_member ADD COLUMN my_comment TEXT;
ALTER TABLE group_member ADD COLUMN my_status INTEGER;
ALTER TABLE group_member ADD COLUMN my_handle_pos INTEGER;
