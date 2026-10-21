-- game_link: a login on another game server that a member has proved is theirs; written by an
-- external service.
--
-- Like web_login (0004), this belongs to a website sign-in service (not part of this repository);
-- it is here so the rows share the account database. Nothing in OpenLobby reads it.
--
-- game         the external service's key for the game server's account system
-- account_key  the game's own stable id for the login; one login belongs to at most one member
-- account_name the login as the player types it, as the game stores it (shown on the page)
--
-- Deleting a member deletes its links (the cascade below; accounts.delete_polid deletes members).

CREATE TABLE game_link (
    game          TEXT NOT NULL,
    account_key   TEXT NOT NULL,
    account_name  TEXT NOT NULL,
    member_id     BIGINT NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    linked_at     TEXT NOT NULL,
    PRIMARY KEY (game, account_key)
);

CREATE INDEX game_link_member ON game_link (member_id);
