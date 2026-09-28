-- Durable state that lives in files under /data today.
--
-- blob: the lobby's resource store (core/resourcestore.py, /data/resources/*.bin),
-- which holds save data, lobby lists and messages the client reads and writes by
-- path. Each is tens of KB at most.
--
-- The store has three scopes, and the file name encodes which one a resource
-- is in; `scope` carries that part of the name so a row maps back to exactly
-- one file:
--   '<member id>'        per-member data, the default (decimal member id)
--   'shared'             a member-scoped path written with no session member
--   's<subject hex>'     a lobby list keyed by the subject the client names
--   'mail'               a message, stored under one name for everybody
-- `path` is the resource's own name within its scope. member_id is set only for
-- per-member rows, so deleting a member takes its saves with it.

CREATE TABLE blob (
    scope      TEXT NOT NULL,
    path       TEXT NOT NULL,
    member_id  BIGINT REFERENCES member(id) ON DELETE CASCADE,
    data       BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, path)
);
CREATE INDEX idx_blob_member ON blob (member_id) WHERE member_id IS NOT NULL;
