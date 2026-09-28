-- admin_triage: where a user report or a tester issue report stands, set on
-- the admin panel's Reports and Issues tabs (services/adminusers.py,
-- triage_map / triage_set). No row means open. The report files themselves
-- are never changed; issuereport's retention reads this table to drop closed
-- reports before open ones.
--
-- kind    'issues' (a tester issue bundle) or 'reports' (a user report)
-- id      the report's id: the bundle directory name, or the user report's
--         file name without .json
-- status  'open', 'resolved' or 'wontfix'
-- note    the moderator's note, at most 500 characters
-- "by"    who set it (quoted: a reserved word, as in admin_code_origin)
-- at      when, unix seconds
--
-- The SQLite table in admin.db was `triage` with the same columns
-- (tools/db_import.py imports it).

CREATE TABLE admin_triage (
    kind    TEXT NOT NULL,
    id      TEXT NOT NULL,
    status  TEXT NOT NULL,
    note    TEXT,
    "by"    TEXT,
    at      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, id)
);
