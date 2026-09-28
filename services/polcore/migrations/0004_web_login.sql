-- web_login: the website username of a member.
--
-- This table belongs to the website sign-in service (regapi), which lives
-- outside this repository. Nothing in OpenLobby reads or writes it; it is
-- here so that the service's rows share the account database and survive the
-- move from accounts.db (tools/db_import.py imports them with the accounts).
--
-- The SQLite table regapi created in accounts.db was:
--
--   CREATE TABLE IF NOT EXISTS web_login (
--       username TEXT NOT NULL UNIQUE COLLATE NOCASE,
--       member_id INTEGER NOT NULL UNIQUE,
--       created_at TEXT NOT NULL)
--
-- member_id is the primary key here (it was NOT NULL UNIQUE, which is the
-- same thing), and a foreign key to member: deleting a member deletes its
-- website login. regapi deletes the row itself when it deletes an account;
-- the cascade covers an account deleted anywhere else.
--
-- Case. The username was unique ignoring case (COLLATE NOCASE). Here it is
-- unique on lower(username), and a lookup compares lower(...), as for the
-- admin_* tables in 0003. created_at stays ISO-8601 text, as in 0001.

CREATE TABLE web_login (
    member_id  BIGINT PRIMARY KEY REFERENCES member(id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX web_login_username ON web_login (lower(username));
