#!/usr/bin/env python3
"""PlayOnline account database -- the server side of "who is logging in".

Until now every responder was stateless: `handle_authserv` took whatever NICK the
client sent, echoed it back in the 001/422 welcome, and read the games menu out of
an environment variable. That is enough to prove the login chain, but it cannot
express the things PlayOnline accounts actually *are*, so this module introduces
them. It is stdlib-only (sqlite3 + hashlib) so it adds no image dependency.

WHY THESE TABLES -- the shape is taken from the client, not invented:

  polid    The PlayOnline ID. SE's own term: the password-change endpoint in
           env.dat is `...?p=chgPolidPwForm`. This is the thing you register on
           the Square Enix website today. `status` carries a 'jail' value because
           the client has a dedicated screen for it (`POL_JAIL_USER_LOGIN_URL` in
           env.dat) -- jail is a first-class account state, not a synonym for
           suspended. `area_kbn` / `login_pf` / `property` are the three values
           the client appends to every UCS CGI request (app.dll 0x049ff32c builds
           "area_kbn=%s&login_pf=%s&property=%s" from the env.dat keys of the same
           name), so they are properties of the account's region/platform.

  member   A login identity under a POL ID. The login screen is a *list* --
           `CLoginMemberListFrame`, `CLoginMemberData`, and a per-member password
           prompt in `CLoginMemberPasswordFrame` -- so a POL ID owns N members and
           each member has its own password. `access_level` exists because the
           client ships `CRegularMember_AccessSettingWnd_Win`.

  handle   The public handle name (HN). `CLogin_Force_HN_Registration` is a whole
           window whose job is to make you set one before you may continue, and
           the lobby has a dedicated handle-registration message ((0x00,0x08) in
           the responders' opcode table). Kept separate from `member` because a
           member can hold several and one is primary.

  content  A per-title service subscription -- the "Content ID" you register on
           the website today. `content_code` is the small integer the protocol
           carries (1 = FinalFantasyXI, 2 = TetraMaster, 3 = Janhourou; polcore
           resolver table at RVA 0x9accc, mirrored in contentlist.py) and
           `content_no` is the long ID string SE issues to a customer. This table
           is what the games menu should be built from: contentlist.build_block()
           currently takes its ids from POL_LOBBY_CONTENT_IDS, and `content_ids`
           below is the drop-in replacement.

  session  A live login. The auth node recovers a per-connection OFB IV that the
           lobby responder needs (`_SESSION_IV` in responders.py is a one-slot
           global today); parking it here keys it per member instead.

NOT modelled, deliberately: characters/worlds. Those live behind the per-title
world server, not POL, and we have no captures of that layer yet.

CLI:
    python accounts.py DB init
    python accounts.py DB addpolid  POLID PASSWORD [--area 00 --pf 01 --prop 00]
    python accounts.py DB addmember POLID LOGIN PASSWORD [--handle NAME]
    python accounts.py DB grant     LOGIN CODE [--no CONTENT_NO]
    python accounts.py DB revoke    LOGIN CODE
    python accounts.py DB passwd    LOGIN PASSWORD
    python accounts.py DB list
"""
import argparse
import binascii
import datetime
import hashlib
import os
import secrets
import sqlite3
import time
import threading

try:
    import polnick
except ImportError:                     # a bare copy of this file, or a test
    polnick = None

#: Where the DB lives inside the container; override with POL_ACCOUNTS_DB.
DEFAULT_DB = os.environ.get("POL_ACCOUNTS_DB", "/data/accounts.db")

#: PBKDF2-HMAC-SHA256. Cost is deliberately modest: this gates a 2002 game
#: client on a private server, and the auth path is synchronous per connection.
_KDF_ROUNDS = 200_000
_SALT_BYTES = 16

#: Account states. 'jail' is a real PlayOnline state (see module docstring).
STATUSES = ("active", "suspended", "jail", "closed")

#: Mirrors contentlist.CONTENT_NAMES; duplicated so this module stays importable
#: on its own (the lobby workstream imports contentlist conditionally).
CONTENT_NAMES = {1: "FinalFantasyXI", 2: "TetraMaster", 3: "Janhourou",
                 4: "FrontMissionOnline", 10: "DirgeOfCerberus",
                 11: "FantasyEarth", 14: "PolFriendList",
                 15: "FinalFantasyXITest"}

#: The journal mode this database runs in. NOT WAL, by default, and that is a
#: deployment fact rather than a preference: `data/` is a Windows BIND MOUNT, and
#: WAL needs a shared-memory `-shm` file beside the database. Docker Desktop's
#: filesystem does not give sqlite reliable shared memory there, which has now
#: cost this project twice --
#:
#:   * a truncated database after a restart (see the accounts.db WAL note, and
#:     `data/accounts.db.corrupt-*`, which is the wreckage), and
#:   * 2026-08-13, a PS2 login that failed with
#:     `OperationalError('unable to open database file')` on an ordinary write.
#:     The login did not fail loudly -- `resolve_account` catches everything and
#:     continues "stateless", so the console got a session with NO ACCOUNT: no
#:     games menu, no handle, and a lobby that then had nothing to serve.
#:
#: TRUNCATE keeps a rollback journal in the same directory and needs no shared
#: memory. The thing WAL buys -- readers that do not block behind a writer -- is
#: worth very little for a handful of clients, and nothing at all next to losing
#: the database. POL_SQLITE_JOURNAL overrides it (use WAL on a real filesystem).
JOURNAL_MODE = os.environ.get("POL_SQLITE_JOURNAL", "TRUNCATE")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS polid (
    polid       TEXT PRIMARY KEY,
    pw_hash     TEXT NOT NULL,
    pw_salt     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    area_kbn    TEXT NOT NULL DEFAULT '00',
    login_pf    TEXT NOT NULL DEFAULT '01',
    property    TEXT NOT NULL DEFAULT '00',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    polid        TEXT NOT NULL REFERENCES polid(polid) ON DELETE CASCADE,
    member_no    INTEGER NOT NULL,
    login_name   TEXT NOT NULL UNIQUE,
    pw_hash      TEXT NOT NULL,
    pw_salt      TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    UNIQUE (polid, member_no)
);

CREATE TABLE IF NOT EXISTS handle (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    handle_name TEXT NOT NULL UNIQUE,
    is_primary  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Extra login NICKs that map to an existing member. The NICK is derived by the
-- CLIENT from the credentials the user typed, and different clients derive it
-- DIFFERENTLY for one and the same account: the US Viewer logs in as UH5GRSV86
-- while the standalone Friend List (PolFL.exe), which predates Square Enix
-- accounts, derives UBJ8OPU7G from the same POL ID. Without this table the second
-- client is either rejected (POL_ACCOUNTS_ENFORCE=1 -> POL-0008) or
-- auto-provisioned as a SEPARATE member, which logs in fine but shows an empty
-- friend list because it is a different account.
--
-- Deliberately NOT the `handle` table: handles are the in-game handle list the
-- lobby serves back (0:8/0:9, face icons), and a login NICK is not one of those.
CREATE TABLE IF NOT EXISTS login_alias (
    nick       TEXT PRIMARY KEY,
    member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    note       TEXT,
    created_at TEXT NOT NULL
);

-- Tombstones for deleted handles. The client caches its handle table LOCALLY and
-- re-volunteers the whole thing on 0:8 every login, so a handle merely removed
-- from `handle` gets re-captured straight back. A row here means "the user deleted
-- this handle" and the 0:8 capture skips it, so deletion sticks even though the
-- client keeps sending it. An explicit re-registration (set_handle) clears it.
CREATE TABLE IF NOT EXISTS deleted_handle (
    member_id   INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    handle_name TEXT NOT NULL,
    deleted_at  TEXT NOT NULL,
    PRIMARY KEY (member_id, handle_name)
);

CREATE TABLE IF NOT EXISTS content (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id     INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    content_code  INTEGER NOT NULL,
    content_no    TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    registered_at TEXT NOT NULL,
    UNIQUE (member_id, content_code)
);

-- Per-HANDLE Content ID links. A member's `content` grant is account-level (they
-- own the title); the client, however, checks whether the CURRENT HANDLE has a
-- Content ID linked for a game ("No Content ID registered to this handle" / "Unlink
-- a Content ID from this handle" / "Reorganize Content ID list"). This table is
-- that link: which of a member's contents are attached to which handle, with the
-- SE-style Content ID value and its status (active / cancelled -> the client's
-- "reactivate a cancelled one"). `content_id` is the on-wire value; its exact
-- format is still being pinned by the oracle test, so it is provisional here.
CREATE TABLE IF NOT EXISTS handle_content (
    handle_id    INTEGER NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    content_code INTEGER NOT NULL,
    slot         INTEGER NOT NULL DEFAULT 0,
    content_id   TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    linked_at    TEXT NOT NULL,
    PRIMARY KEY (handle_id, content_code, slot)
);

-- A Content ID belongs to EXACTLY ONE handle (POL-7169/7187/5326), and that
-- rule now has to survive a table where one (handle, game) holds several rows.
-- `_content_id_taken` has always enforced it in code; this is the backstop that
-- cannot be forgotten by a new call site. NULLs are exempt, which is what
-- SQLite's UNIQUE already does and what a not-yet-minted row needs.
CREATE UNIQUE INDEX IF NOT EXISTS handle_content_id_unique
    ON handle_content (content_id) WHERE content_id IS NOT NULL;

-- THE CONTENT ID COUNTER. One row, holding the next serial to hand out.
--
-- Content IDs are ALLOCATED, not computed, and that is a measurement rather than
-- a preference: a real SE Content ID was read off the live SE-connected Viewer
-- two independent ways on 2026-08-23 (the 64-slot content table at
-- `polcore+0x403080` slot `+0x08`, and the shim's `[pay]` capture of the real
-- `1:3` reply at record offset 0x10 LE; the value and its provenance are in the
-- GITIGNORED `Ignored Files/content-ids.md`, which is why nothing here quotes
-- it). It is a plain ~8-digit integer in the tens of millions with the
-- game/service carried in a SEPARATE u16 content-code field -- so there is no
-- account, no game and no check digit inside the number, and nothing about it
-- is derivable from what we hold. See `allocate_content_id`.
CREATE TABLE IF NOT EXISTS content_id_seq (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    next_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS friend (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_id    INTEGER NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    peer_handle  INTEGER REFERENCES handle(id) ON DELETE SET NULL,
    peer_name    TEXT NOT NULL,
    peer_guid    INTEGER NOT NULL DEFAULT 0,
    kind         INTEGER NOT NULL DEFAULT 2048,
    status       TEXT NOT NULL DEFAULT 'active',
    comment      TEXT,
    -- The caption THIS account gave that person ("rename a friend"), and the
    -- two ignore-state bytes their 2:6 record carries. Both are measured off
    -- retail; see the `friend` entries in _MIGRATIONS for the byte values and
    -- for why the ignore bits are stored rather than decoded.
    label        TEXT,
    ignore_low   INTEGER,
    ignore_flag  INTEGER,
    created_at   TEXT NOT NULL,
    UNIQUE (handle_id, peer_name)
);

-- Group MEMBERSHIP. A group is a `friend` row with kind = KIND_GROUP, so a
-- group's identity is that row's id and its name is unique per owning handle.
--
-- Two ceilings here are the CLIENT'S, not ours, and both are validated by it:
-- the 07:12 count block's byte 0 must be <= 4 (four group slots at polcore
-- 0x3bb06c0, stride 0x3098) and bytes 1..4 must each be <= 0x40 (64 member
-- slots per group at obj+0x30, stride 0xC0). Exceeding either draws POL-5133.
--
-- `class` is the 3-bit field the wire record packs at bit 50. polcore accepts
-- 2..5 and REJECTS anything else, and a group whose members are all rejected
-- is dropped from the list entirely. Class 2 additionally marks the group
-- UNUSABLE once it is copied into the group flags (0x37e7d10), so the usable
-- values are 3..5 -- see responders._GROUP_MEMBER_CLASS for the full chain.
CREATE TABLE IF NOT EXISTS group_member (
    group_id      INTEGER NOT NULL REFERENCES friend(id) ON DELETE CASCADE,
    member_handle INTEGER REFERENCES handle(id) ON DELETE SET NULL,
    member_name   TEXT NOT NULL,
    member_guid   INTEGER NOT NULL DEFAULT 0,
    class         INTEGER NOT NULL DEFAULT 3,
    -- 1 = invited but not yet accepted. Served ONLY to the group's owner
    -- (their "Inviting into group" rows); everyone else sees accepted members
    -- alone, and a pending member does not see the group at all until they
    -- accept. polcore's 2..5 class range has no way to say "halfway in", so
    -- the halfway state lives here instead of in `class`.
    pending       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (group_id, member_name)
);

CREATE TABLE IF NOT EXISTS session (
    token      TEXT PRIMARY KEY,
    member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    nick       TEXT,
    peer_ip    TEXT,
    iv         TEXT,
    lobby_port INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Handle profile: what the client sends on lobby opcode 05:01 (age, sex, job,
-- location, languages, interests, mail address, portrait). NOT the same thing as
-- `profile` below, which is SE's sign-up contact details.
--
-- Stored as one row per FIELD ID rather than one column per field, deliberately.
-- The wire format is an id-keyed TLV stream and the id map is still incomplete
-- (0x11 and 0x01/0x02 are unidentified), so a column-per-field schema would need
-- migrating every time another id is identified, and would silently drop the ones
-- that are not. Key-value keeps unknown fields instead of discarding them, and it
-- matches how the record has to be rendered back out again.
--
-- val_int and val_text: TLV values are either a small integer (len 8, of which
-- only the low bytes are meaningful -- the rest is uninitialised client stack) or
-- a string (len 320 for the mail address). Exactly one is set.
--- KEYED ON handle_id, NOT member_id. It was member-keyed until 2026-08-12,
--- which made every handle on an account share one profile -- open any of them
--- and the same age/sex/interests/portrait came back. That is not how
--- PlayOnline worked: a handle IS the persona, and its profile is the thing
--- other players look at. See `handle_guid` for how a handle is named on the
--- wire, and `_migrate_handle_profile` for the one-time move of member-keyed
--- rows onto that member's primary handle.
CREATE TABLE IF NOT EXISTS handle_profile (
    handle_id  INTEGER NOT NULL REFERENCES handle(id) ON DELETE CASCADE,
    field_id   INTEGER NOT NULL,
    val_int    INTEGER,
    val_text   TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (handle_id, field_id)
);

-- Member profile: the "personal contact information" of SE's step 5/7.
--
-- Column names mirror the client's own PML variables ONE FOR ONE
-- ($_POLSIGNUP_KANJI_FAMILY_NAME -> kanji_family, ZIP_MAIN -> zip_main, ...),
-- so the page and the database line up with no translation layer and any
-- mismatch is visible rather than buried in a mapping.
--
-- The shape is SE's, which is JP-market: family/given split with a KANJI pair
-- and a KATAKANA (phonetic reading) pair, a two-part postcode, three address
-- lines and a three-part phone number. Western clients label these "Last name /
-- First name" and simply leave the katakana fields empty -- that is a DISPLAY
-- concern, not a schema one, so we keep SE's structure and let unused columns
-- stay null.
CREATE TABLE IF NOT EXISTS profile (
    polid           TEXT PRIMARY KEY REFERENCES polid(polid) ON DELETE CASCADE,
    kanji_family    TEXT,
    kanji_first     TEXT,
    katakana_family TEXT,
    katakana_first  TEXT,
    zip_main        TEXT,
    zip_sub         TEXT,
    address_0       TEXT,
    address_1       TEXT,
    address_2       TEXT,
    phone_0         TEXT,
    phone_1         TEXT,
    phone_2         TEXT,
    country         TEXT,
    updated_at      TEXT NOT NULL
);

-- Registration codes. SE's step 3/7 takes the code as FIVE dash-separated
-- groups. Codes are STORED exactly as issued but COMPARED `COLLATE NOCASE` --
-- SE's screen claims case-sensitivity, and honouring that cost a real player
-- their sign-up on 2026-08-29 (see `normalise_regcode`). The stored spelling is
-- what every UPDATE writes back, so the audit trail keeps the issued form.
-- `redeemed_by` is the POL ID the code created, which makes
-- a code single-use and gives us an audit trail; `contents` is the
-- comma-separated content codes it grants (a retail code granted three FFXI
-- Content IDs per SE's own walkthrough).
CREATE TABLE IF NOT EXISTS regcode (
    code        TEXT PRIMARY KEY,
    contents    TEXT NOT NULL DEFAULT '1',
    note        TEXT,
    created_at  TEXT NOT NULL,
    redeemed_at TEXT,
    redeemed_by TEXT REFERENCES polid(polid)
);

-- PlayOnline Mail. One row per stored message; the mailbox is keyed on the
-- LOCAL PART of the address rather than member_id, because that is all a POP3
-- login carries (the client sends either "cas" or "cas@pol.com" as the user id
-- and there is no POL ID anywhere in the session). `member_id` is kept when we
-- can resolve one, for joins and for cascade-delete.
--
-- `uidl` is what the client stores in its own EMAIL/uidllist to decide whether a
-- message is new, so it MUST be stable for the life of the message and unique
-- within the mailbox -- reusing one makes the client skip a real message.
CREATE TABLE IF NOT EXISTS mail (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box         TEXT NOT NULL,
    member_id   INTEGER REFERENCES member(id) ON DELETE CASCADE,
    uidl        TEXT NOT NULL,
    sender      TEXT,
    subject     TEXT,
    raw         BLOB NOT NULL,
    received_at TEXT NOT NULL,
    deleted_at  TEXT,
    UNIQUE (box, uidl)
);

-- The admin panel's operator credential. A SINGLETON row (id is pinned to 1):
-- there is one operator login, and the panel changes it in place rather than
-- accumulating rows. Kept here, in the same DB the panel already opens, so the
-- credential can be changed at RUN TIME -- the environment variables it used to
-- read are fixed at process start and live in a .env the container cannot write.
--
-- No row = no credential = the panel is open, which is its first-run state and
-- is safe only on the loopback bind. See services/admin.py.
CREATE TABLE IF NOT EXISTS admin_cred (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    username   TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ONE ACCOUNT, MANY CLIENTS: the NICK token is per (account x client build).
--
-- `member.login_token` was a single trust-on-first-use slot, which silently made
-- an account single-platform: the PC seeds it, and the PS2 Viewer -- which
-- presents a DIFFERENT 11-char token for the same account -- is then refused
-- for ever with SE reject 0xCA ("wrong password"). Worked example, measured
-- live 2026-08-24: account `ABCD1234`/"Fox" logged in 223 times as
-- `AbCdEfGhIjK` from the PC and was refused 4 times as `ZyXwVuTsRqP` from the
-- PS2, while an account seeded FROM the PS2 worked there and would have failed
-- on the PC for the mirror-image reason.
--
-- `client_sig` is the NICK blob's fixed 21-char head+pad (blob[0:21]) -- the
-- only measured discriminator between client builds:
--     PC/Deck Viewer   TTTTTAISTTTTTTTTTTTTT  (and ...AIT..., same token)
--     PS2 Viewer       TTTTT7ITTTGaItbIQ8nHA
-- It is a MEASUREMENT, not a decoded field: what those bytes mean is unknown,
-- so this stores the signature verbatim rather than a platform name we invented.
--
-- TOFU is now per (member, client_sig): the first login from a given client
-- records that client's token, later logins from it must match. A client
-- signature never seen for this account seeds its own slot, which is exactly
-- the trust the account's very first login already got.
CREATE TABLE IF NOT EXISTS login_token_client (
    member_id  INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    client_sig TEXT NOT NULL,
    token      TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (member_id, client_sig)
);

CREATE INDEX IF NOT EXISTS idx_member_polid   ON member(polid);
CREATE INDEX IF NOT EXISTS idx_content_member ON content(member_id);
CREATE INDEX IF NOT EXISTS idx_session_member ON session(member_id);
CREATE INDEX IF NOT EXISTS idx_mail_box       ON mail(box, deleted_at);
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


#: SE's step 5/7 said "(8-15 alphanumeric characters)", but that was SE's
#: service rule, not a client limitation: the PML password fields run the same
#: permissive check="daAsm1" mode SE puts on the email field (which needs @ . _ -),
#: and every path that verifies pw_hash -- registration, the servlet login,
#: kinou 6/17 -- rides the same form pipeline, so any password that can be
#: registered can be re-entered. Symbols are therefore allowed. The length cap
#: stays: the entry fields are maxlength=15. Spaces/controls stay out (invisible,
#: and a bare space in a form body is encoder-dependent); non-ASCII stays out
#: (the era client's cp932/UTF-8 software-keyboard round-trip is unverified and
#: could differ between PC and PS2).
PASSWORD_MIN = 8
PASSWORD_MAX = 15


def check_password_policy(password):
    """Return None if acceptable, else a message suitable for display."""
    if not password or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters."
    if not all(0x21 <= ord(ch) <= 0x7E for ch in password):
        return ("Password may use letters, numbers, and symbols "
                "(no spaces or non-ASCII characters).")
    return None


def hash_password(password, salt=None):
    """Return (hash_hex, salt_hex). `password` may be str or bytes."""
    if isinstance(password, str):
        password = password.encode("utf-8")
    if salt is None:
        salt = secrets.token_bytes(_SALT_BYTES)
    elif isinstance(salt, str):
        salt = binascii.unhexlify(salt)
    h = hashlib.pbkdf2_hmac("sha256", password, salt, _KDF_ROUNDS)
    return h.hex(), salt.hex()


def check_password(password, pw_hash, pw_salt):
    calc, _ = hash_password(password, pw_salt)
    return secrets.compare_digest(calc, pw_hash)


# --------------------------------------------------------------------------- #
# admin panel credential (see the admin_cred table above)
# --------------------------------------------------------------------------- #
#: The operator password is NOT a player password. `check_password_policy` above
#: encodes the client-facing rule for POL accounts -- 8-15 printable ASCII,
#: capped by the entry fields' maxlength -- which is exactly the wrong rule for
#: the one credential that can mint accounts: it caps the length at 15. Hence a
#: separate policy.
ADMIN_PASSWORD_MIN = 8
ADMIN_PASSWORD_MAX = 200


def check_admin_password_policy(password):
    """Return None if acceptable as an operator password, else a message."""
    if not password or len(password) < ADMIN_PASSWORD_MIN:
        return f"Password must be at least {ADMIN_PASSWORD_MIN} characters."
    if len(password) > ADMIN_PASSWORD_MAX:
        return f"Password must be at most {ADMIN_PASSWORD_MAX} characters."
    if password.strip() != password:
        return "Password must not begin or end with whitespace."
    return None


def get_admin_cred(conn):
    """The operator credential row, or None when none has been set."""
    try:
        return conn.execute(
            "SELECT username, pw_hash, pw_salt, updated_at"
            "  FROM admin_cred WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        # A DB last touched by a build that predates this table. Treat it as
        # "no credential" rather than 500ing the panel that would set one.
        return None


def set_admin_cred(conn, username, password):
    """Set (or replace) the operator credential. Raises ValueError if rejected.

    Does NOT commit -- the caller owns the transaction.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("Username must not be empty.")
    if len(username) > 64:
        raise ValueError("Username must be at most 64 characters.")
    msg = check_admin_password_policy(password)
    if msg:
        raise ValueError(msg)
    pw_hash, pw_salt = hash_password(password)
    conn.execute(
        "INSERT OR REPLACE INTO admin_cred (id, username, pw_hash, pw_salt,"
        " updated_at) VALUES (1,?,?,?,?)",
        (username, pw_hash, pw_salt, _now()))
    return username


def clear_admin_cred(conn):
    """Drop the operator credential, returning the panel to its open state."""
    conn.execute("DELETE FROM admin_cred WHERE id = 1")


#: Columns added after the first schema shipped. `CREATE TABLE IF NOT EXISTS`
#: silently leaves an existing table alone, so new columns need adding by hand
#: or an upgraded server sees an old DB and fails at runtime instead of here.
_MIGRATIONS = {
    "group_member": [
        ("pending", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "member": [
        # PlayOnline Mail. SE assigns an address at registration (the real one
        # looked like x141582595264@pol.com) and lets you request up to three
        # preferred names; mail has its OWN password, which is why the Viewer
        # prompts separately for it.
        ("mail_address", "TEXT"),
        ("mail_pw_hash", "TEXT"),
        ("mail_pw_salt", "TEXT"),
        # The mail password IN CLEAR, and only for accounts that opt in.
        # APOP (which is what the Viewer's PlayOnline-mail preset selects) sends
        # MD5(banner + password) -- there is no way to check that against a
        # one-way hash, so verifying it at all requires the plaintext. This is a
        # SEPARATE password from the POL login one by SE's own design, the Viewer
        # prompts for it separately, and leaving it NULL simply falls back to
        # accepting any digest. Never reuse the login password here.
        ("mail_pw_plain", "TEXT"),
        # Login/logout clock, served back in the lobby 04:06 session record
        # (payload +0x08 = last login, +0x0C = last logout; see
        # responders._session_record). `last_login_at` is THIS session's login and
        # `prev_login_at` the one before it -- the lobby's "Last Login" wants the
        # previous one, since by the time it renders you are already logged in.
        ("last_login_at", "TEXT"),
        ("prev_login_at", "TEXT"),
        ("last_logout_at", "TEXT"),
        # The stable password token off the NICK line's third field (11 chars;
        # see responders.nick_credential). Trust-on-first-use: recorded on the
        # first login, compared on every later one. This is NOT the plaintext
        # password nor a hash of it -- it is the client-derived credential the
        # Viewer transmits, which is stable per password, so comparing it is a
        # real password check without our having reversed the derivation. Kept
        # separate from pw_hash (which the registration flow sets) on purpose.
        ("login_token", "TEXT"),
    ],
    "handle": [
        # WHAT THIS CLIENT CALLS ITSELF. A client knows its PEERS by the guids we
        # serve, but it knows ITSELF by a value of its own -- constant per
        # account, and visible to us in everything it writes: the sender field of
        # a message it sends, and the 8 bytes at +0x0C of a 02:06 friend record.
        # SE's account holder carries 0x8c002c1e04bc91 in both.
        #
        # It matters because a message's RECIPIENT field is the reader itself. In
        # our guid the reader does not recognise it and the header renders "To:
        # Unknown User" -- reported live 2026-08-16 on a push that otherwise
        # worked. Learned, never invented; NULL simply means "not seen yet".
        ("client_guid", "INTEGER"),
    ],
    "friend": [
        # THE CLIENT'S OWN ID FOR THIS ROW -- the 12 bytes at +0x18 of a 02:06
        # write record, kept verbatim because it is how a DELETE names its
        # target. A deletion carries no name (the field holds stale heap; see
        # responders._friend_put_records), so without this there is nothing to
        # match it against and "remove friend" can only be applied by guessing.
        # It is client-minted and opaque -- polcore's guid key K, most likely --
        # so it is stored, compared and never interpreted.
        ("client_ref", "BLOB"),
        # THE SECOND ID, and the one a delete always carries in the same place:
        # the 4 bytes at request 0x158, immediately in front of the record grid.
        # Measured 2026-08-16 across seven consecutive delete attempts -- stable
        # per row (`a21af5eb` for one friend, `e2b8e568` for another) and stable
        # across attempts, while the record's own name field held heap. Two
        # independent ids means a delete still matches when one of them is
        # missing.
        ("wire_ref", "BLOB"),
        # THE PER-FRIEND LABEL -- what "rename a friend" actually writes.
        # Measured 2026-08-19 off retail (capture `grouplife.txt` @257632):
        # renaming `Cyn` to
        # "Cool friend :3" sends a plain `2:6 KPutFriendList` whose record
        # carries that text in the slot the NAME normally occupies. It is not a
        # new opcode and it is not the handle changing name -- it is a caption
        # this account keeps for that person. Distinct from `comment`, which is
        # the PEER's own profile text.
        ("label", "TEXT"),
        # THE IGNORE STATE, round-tripped rather than interpreted. Same capture:
        # ignoring somebody is not an opcode either, it is two flag bytes on
        # their record inside the same 2:6 --
        #     normal      low=0x21  flag=0x00
        #     ignore ADD  low=0x31  flag=0x34
        #     scope->PoL  low=0x51  flag=0x00
        #     un-ignore   low=0x21  flag=0x40
        # Four samples is not enough to name every bit, and the handoff is
        # explicit that we need not: store them, serve them back, never drop
        # them. `ignore_low` is the byte in front of the record grid, and
        # `ignore_flag` the one at record +0x03 (see responders'
        # `_FRIEND_PUT_FLAG_AT`).
        ("ignore_low", "INTEGER"),
        ("ignore_flag", "INTEGER"),
    ],
}


def _migrate_handle_profile(conn):
    """Move a member-keyed `handle_profile` onto handle ids (2026-08-12).

    ADD COLUMN cannot express this one -- the primary key itself changes -- so it
    is a rebuild rather than a `_MIGRATIONS` entry. Rows land on the member's
    PRIMARY handle, which for every account that exists today is the only handle
    that ever had a profile: the client edits the profile of whichever handle is
    active, and until now the server wrote them all to the same member row.

    A member with no handle at all would have nowhere to put its rows; those are
    dropped rather than orphaned, and counted in the log line so a surprise is
    visible instead of silent.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(handle_profile)")}
    if "member_id" not in cols:
        return                                   # already handle-keyed
    rows = list(conn.execute(
        "SELECT p.member_id, p.field_id, p.val_int, p.val_text, p.updated_at,"
        "       (SELECT id FROM handle WHERE member_id = p.member_id"
        "         ORDER BY is_primary DESC, id ASC LIMIT 1) AS handle_id"
        "  FROM handle_profile p"))
    kept = [r for r in rows if r["handle_id"] is not None]
    conn.execute("ALTER TABLE handle_profile RENAME TO handle_profile_by_member")
    conn.executescript(SCHEMA)                   # recreate with the new key
    conn.executemany(
        "INSERT OR REPLACE INTO handle_profile (handle_id, field_id, val_int,"
        " val_text, updated_at) VALUES (?,?,?,?,?)",
        [(r["handle_id"], r["field_id"], r["val_int"], r["val_text"],
          r["updated_at"]) for r in kept])
    conn.commit()
    # The old table is KEPT, not dropped: it is the only copy of the pre-move
    # data, it is tiny, and a wrong primary-handle guess is recoverable from it.
    print(f"[accounts] handle_profile: re-keyed {len(kept)} row(s) onto handle "
          f"ids, {len(rows) - len(kept)} dropped (member had no handle); "
          f"originals kept in handle_profile_by_member")


def _migrate_handle_content_slots(conn):
    """Give `handle_content` a `slot`, so one handle can hold SEVERAL Content IDs
    for the SAME game (2026-09-03).

    ADD COLUMN cannot express this one -- the primary key itself changes from
    `(handle_id, content_code)` to `(handle_id, content_code, slot)` -- so it is a
    rebuild, the same shape as `_migrate_handle_profile`.

    WHY. FFXI issues one Content ID per CHARACTER, and the old key could express
    exactly one per handle. A player who made a second character got no pairing
    for it (`ffxi_bridge.content_id_for` refuses to bind one Content ID to two
    charids, correctly -- a duplicate silently destroys the other character's
    world identity), so the second character missed POL's 64-slot table and drew
    **POL-0001** at char select, for ever, with nothing about creating it looking
    wrong. Measured on Ironbadger 2026-08-28: charid 4 `Kharn` held 30000037 and
    charid 6 `Blue` held nothing; five select attempts, five world-server pending
    sessions, zero zone-ins.

    Every existing row becomes slot 0, which is byte-for-byte the behaviour that
    was there before -- no id moves, nothing is re-minted (see
    `allocate_content_id`: the FFXI client names a character's local files after
    its Content ID in hex, so re-minting orphans macros). Slots 1..N are minted
    by `ensure_content_slots` and are purely additive.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(handle_content)")}
    if "slot" in cols:
        return                                   # already slotted
    rows = list(conn.execute(
        "SELECT handle_id, content_code, content_id, status, linked_at"
        "  FROM handle_content"))
    conn.execute("ALTER TABLE handle_content RENAME TO handle_content_pre_slots")
    conn.executescript(SCHEMA)                   # recreate with the new key
    conn.executemany(
        "INSERT INTO handle_content (handle_id, content_code, slot, content_id,"
        " status, linked_at) VALUES (?,?,0,?,?,?)",
        [(r["handle_id"], r["content_code"], r["content_id"], r["status"],
          r["linked_at"]) for r in rows])
    conn.commit()
    # Kept, not dropped, for the same reason `handle_profile_by_member` is: it is
    # tiny and it is the only pre-migration copy of a value that must never be
    # re-minted.
    print(f"[accounts] handle_content: re-keyed {len(rows)} row(s) onto "
          f"(handle, content, slot); originals kept in handle_content_pre_slots")


def _migrate(conn):
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    _migrate_handle_profile(conn)
    _migrate_handle_content_slots(conn)
    # Accounts that already exist get their FFXI slots here. New ones get
    # them at registration; without this line every account created before
    # 2026-09-03 would keep the single-character ceiling for ever, and the
    # operator would have to know to run a tool nobody would think to look
    # for. Additive and idempotent -- see ensure_content_slots.
    minted = ensure_ffxi_character_slots(conn)
    if minted:
        print(f"[accounts] FFXI: minted {minted} additional Content ID(s) "
              f"so existing handles can hold {FFXI_CHARACTER_SLOTS} character(s)")


#: Paths whose schema this PROCESS has already ensured. See connect().
_SCHEMA_READY = set()
_SCHEMA_LOCK = threading.Lock()

#: Paths we have already complained about the journal mode for. The pragma now
#: runs per connection (see connect()), so the warning needs its own latch.
_JOURNAL_WARNED = set()


# --------------------------------------------------------------------------- #
# THE CONNECTION POOL -- measured, not assumed (2026-08-19)
#
# Observed in live testing, retail SE side by side with this server: this
# server ran visibly slower on almost everything -- friend-accept, group-list
# and status-change round-trips were near-instant on SE and lagged here.
#
# `tools/lobby_profile.py` is that measurement, and it names the cost: ONE
# friend-list serve opens **7 database connections** and one group-list serve
# **9**, because every helper on the request path opens its own and closes it in
# a `finally`. On the WINDOWS DEV BOX that was ~65% of the whole serve:
#
#     2:3  KGetFriendList     5.66 ms/op   7.0 conn/op   3.69 ms in connect/close
#     7:12 KGetGroupList      6.73 ms/op   9.0 conn/op   4.57 ms in connect/close
#     accounts.connect+close  0.49 ms/op                 -- the tax by itself
#
# WARNING: **SCOPE, HONESTLY: THOSE ARE DEV NUMBERS AND PROD IS NOT DEV.** Production is
# an **Ubuntu VM** on an ordinary Linux filesystem,
# where the same open+close measures **0.017 ms** bare and **0.038 ms** with the
# row factory and the pragma -- so nine of them cost ~0.34 ms there, not ~4.4 ms.
# The pool is worth roughly an order of magnitude less on prod than the numbers
# above suggest, and it is NOT what made the lobby feel slow: that was the
# reader's idle window, ~1.0 s per message (`_lobby_frame_complete` in
# responders.py). Keep the pool -- it is correct, free, and the Windows dev box
# is where most development happens -- but do not credit it with prod's latency.
#
# So a checked-out connection now comes from a free list and `close()` puts it
# BACK rather than closing it. Nothing at the call sites changes -- they keep
# their `try/finally: db.close()`, which is the point: 46 call sites in
# `responders.py` alone, and a pool that needed any of them edited would have
# been a refactor rather than a fix.
#
# WHY THIS DOES NOT REOPEN THE WAL HAZARD. `accounts-db-wal-hazard` is about the
# JOURNAL MODE, not about connection lifetime: WAL on the WINDOWS DEV bind mount
# truncated the database, which is why `JOURNAL_MODE` is TRUNCATE. Pooling does
# not touch the journal mode -- it is set per connection in `connect()` below and
# asserted by `tools/dbpool_test.py` after a pooled round-trip. Pooling in fact
# reduces the concurrent-connection count, which is the direction that hazard
# cares about.
#
# (WAL is measured USABLE on prod's Linux filesystem and would buy readers that
# do not block behind a writer. Deliberately NOT done: with the database costing
# ~0.34 ms of a serve there, it would be optimising something that is not the
# problem -- see the scope note above.)
#
# `check_same_thread=False` is REQUIRED and is safe here for one reason: the
# lobby is a thread PER CONNECTION and its threads are short-lived, so a
# thread-local cache would never hit. A pooled connection is checked out
# EXCLUSIVELY -- it is off the free list for as long as somebody holds it -- so
# it is never touched by two threads at once, which is the only thing sqlite3's
# same-thread check protects.
#
# POL_DB_POOL=0 disables it entirely and restores the open-per-call behaviour;
# any other number is the per-path ceiling on IDLE connections.
_POOL_MAX = int(os.environ.get("POL_DB_POOL", "8") or 0)
_POOL = {}
_POOL_LOCK = threading.Lock()


class _PooledConnection(sqlite3.Connection):
    """A connection whose `close()` hands it back instead of closing it.

    Subclassing is what lets every existing `finally: db.close()` keep working
    unchanged. `_pool_path` is set by `connect()`; a connection without one (a
    caller that built it some other way) closes for real.
    """

    _pool_path = None

    def close(self):
        if not _pool_put(self):
            sqlite3.Connection.close(self)


def _pool_get(key):
    """A live pooled connection for `key`, or None.

    Each candidate is PROBED before it is handed out. A connection can go bad
    while it sits idle -- the file replaced under it, which is exactly what a
    test fixture does -- and the failure would otherwise surface in whichever
    unlucky request drew it, far from the cause.
    """
    if _POOL_MAX <= 0:
        return None
    while True:
        with _POOL_LOCK:
            lst = _POOL.get(key)
            if not lst:
                return None
            conn = lst.pop()
        try:
            conn.execute("SELECT 1").fetchone()
            return conn
        except sqlite3.Error:
            try:
                sqlite3.Connection.close(conn)
            except sqlite3.Error:
                pass


def _pool_put(conn):
    """Return `conn` to the free list. False means "close it for real".

    The ROLLBACK is not optional. A caller that read a cursor without draining
    it leaves an open read transaction behind, and a pooled connection carrying
    one would block the next writer for as long as it sat idle -- a deadlock
    with no statement to blame it on. A connection that will not roll back is
    not fit to reuse, so it is dropped rather than parked.
    """
    key = getattr(conn, "_pool_path", None)
    if _POOL_MAX <= 0 or key is None:
        return False
    try:
        conn.rollback()
    except sqlite3.Error:
        return False
    with _POOL_LOCK:
        lst = _POOL.setdefault(key, [])
        if len(lst) >= _POOL_MAX:
            return False
        lst.append(conn)
    return True


def pool_drain(path=None):
    """Really close every IDLE pooled connection (all paths, or just one).

    For the cases where holding the file open is the problem rather than the
    cost of reopening it: a test that wants to delete its fixture, or a tool
    that hands the database to another process. Checked-OUT connections are not
    affected -- there is nothing to drain them from.
    """
    with _POOL_LOCK:
        keys = [os.path.abspath(path)] if path else list(_POOL)
        conns = [c for k in keys for c in _POOL.pop(k, [])]
    for c in conns:
        try:
            sqlite3.Connection.close(c)
        except sqlite3.Error:
            pass
    return len(conns)


def pool_stats():
    """`{path: idle count}` -- for the profiler and for a health line."""
    with _POOL_LOCK:
        return {k: len(v) for k, v in _POOL.items()}


def connect(path=None):
    """Open (creating if needed) the account DB with the schema applied.

    THE SCHEMA IS APPLIED ONCE PER PROCESS, not once per connection, and that is
    a correctness fix rather than an optimisation. `executescript(SCHEMA)` plus
    `_migrate()` take a WRITE lock even when every statement is a no-op
    (`CREATE TABLE IF NOT EXISTS` still acquires one). The lobby opens a fresh
    connection per request, across many threads, so every read was contending
    for a write lock against every other read.

    Live 2026-08-12 that surfaced as a member search reporting
    `OperationalError('database is locked')` -- which the caller turned into "0
    hits", i.e. the client told the user **"user not found"** for an account that
    exists. A lock is not an absence, and it should never have been able to look
    like one.

    Migrations still run: the first connect in the process does the full setup,
    which is when a new column or table would be added. A schema change made by
    ANOTHER process mid-run is not picked up, which is the same exposure as
    before for anything already holding a connection.
    """
    path = path or DEFAULT_DB
    key = os.path.abspath(path)
    # AN IDLE CONNECTION FIRST. See the pool note above: this is the whole
    # Track B fix, and it sits in front of the makedirs deliberately -- a pooled
    # hit must cost no syscalls at all, or the pool is only half a saving.
    pooled = _pool_get(key)
    if pooled is not None:
        return pooled
    parent = os.path.dirname(key)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # RETRY THE OPEN. On the Windows bind mount this database lives on, an
    # ordinary open-for-write comes back `unable to open database file` every so
    # often -- measured live on 2026-08-13, once, in the middle of a PS2 login.
    # Every caller treats a failure as "no account", so a hiccup that lasts
    # milliseconds costs a player their games menu for a whole session. Three
    # tries over ~0.3s turns it into a slightly slower login instead.
    conn = None
    for attempt in range(3):
        try:
            # `check_same_thread=False` and the factory are what make this
            # connection poolable; see the pool note above for why both are
            # safe. With POL_DB_POOL=0 the factory's `close()` falls straight
            # through to the real one, so the class costs nothing when off.
            conn = sqlite3.connect(path, timeout=10, check_same_thread=False,
                                   factory=_PooledConnection)
            break
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))
    conn.row_factory = sqlite3.Row
    conn._pool_path = key
    # *** THE JOURNAL MODE IS PER CONNECTION, NOT PER FILE, AND THAT WAS A BUG. ***
    #
    # This pragma used to live inside the once-per-process schema block below,
    # on the note that "the journal mode is a PERSISTENT property of the file".
    # That is true of **WAL and only WAL**. For the rollback modes -- DELETE,
    # TRUNCATE, PERSIST -- it is a property of the CONNECTION, and sqlite opens
    # every new one in DELETE. So the first connection in a process ran in
    # TRUNCATE and every other one ran in DELETE.
    #
    # Mixing them is not cosmetic ON WINDOWS. Measured 2026-08-19, two
    # connections open on one file, one TRUNCATE and one DELETE:
    #
    #     A  PRAGMA journal_mode = TRUNCATE  -> truncate
    #     C  (fresh connection)              -> delete
    #     C  INSERT ...                      -> OperationalError: disk I/O error
    #
    # and the same INSERT succeeds the instant C is put into TRUNCATE too. The
    # pool SURFACED it (an idle TRUNCATE connection now outlives the request that
    # opened it, so the overlap is permanent rather than momentary) but did not
    # cause it: any write from a second connection while the first was open could
    # raise it.
    #
    # WARNING: **IT DOES NOT REPRODUCE ON LINUX** -- checked directly, the same two
    # connections and the same INSERT succeed there -- so this is a DEV-BOX
    # failure, not an explanation for anything seen on the Ubuntu prod VM. Do not
    # cite it as one. The per-connection pragma is kept regardless: it is what the
    # old comment always claimed to do, it costs one pragma per genuinely new
    # connection, and it makes dev and prod behave the same way.
    #
    # Setting it on every connection also does what the old comment SAID it did
    # -- convert a file left in WAL by an earlier run -- for every connection
    # rather than for one. It costs one pragma per genuinely new connection, and
    # the pool means there are very few of those.
    got = conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}").fetchone()
    got = (got[0] if got else "?").lower()
    if got != JOURNAL_MODE.lower() and key not in _JOURNAL_WARNED:
        # Once per path: this now runs per connection, and a warning that
        # repeats a few hundred times an hour is a warning nobody reads.
        _JOURNAL_WARNED.add(key)
        print(f"[accounts] journal_mode is {got!r}, not the requested "
              f"{JOURNAL_MODE.lower()!r} -- another connection holds "
              f"{path}; restart every service that opens it")
    # The setup runs INSIDE the lock, not merely under a flag set inside it.
    # Claiming the flag and then building the schema outside leaves a window
    # where a second thread sees "ready" and gets a connection to a database
    # whose tables do not exist yet -- `no such table: member`, on cold start,
    # which is exactly when several clients connect at once. It costs nothing to
    # hold: this whole block runs once per process.
    with _SCHEMA_LOCK:
        if key not in _SCHEMA_READY:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
            _SCHEMA_READY.add(key)      # only after it is genuinely ready
    return conn


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
#: A real PlayOnline ID is EIGHT characters of capital letters and digits --
#: `EFGH5678` is the one sample from a live SE account.
#: The user NEVER picks it: per SE's own registration
#: walkthrough the ID is ISSUED by the service and shown to you on the
#: confirmation screen, after you enter a registration code and choose a
#: password. Anything that prompts for a POL ID at sign-up has the flow backwards.
#:
#: WARNING: THE `NN-NNNNNNN` FORM THIS USED TO MINT WAS A MISREADING and it shipped:
#: `00-4946053` was read off a captured account as its "PlayOnline ID", but
#: 4946053 = 0x4B7885 is that account's HANDLE ID (`z_hid` in the profile record,
#: +0x08 of the 32-byte handle entry, and the `04b7885` in its room-join
#: announce). It is not typed anywhere. The client's Add Member dialog validates
#: the field -- "The PlayOnline ID entered contains invalid characters. Please use
#: numbers and capital letters only" (string 22146) -- so an ID with a dash in it
#: CANNOT BE ENTERED AT ALL, which is why registrations completed and then could
#: not be used to log in.
#:
#: `I`, `O`, `0` and `1` are left out so a handwritten ID cannot be mistyped;
#: everything minted is still inside SE's own capitals-and-digits alphabet.
POLID_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"      # A-Z less I, O
POLID_DIGITS = "23456789"                       # 2-9 less 0, 1
POLID_SHAPE = (POLID_LETTERS,) * 4 + (POLID_DIGITS,) * 4    # LLLLDDDD, as EFGH5678


def mint_polid(conn, shape=POLID_SHAPE):
    """Allocate a free PlayOnline ID in SE's shape: 8 capitals-and-digits.

    Random rather than sequential, and not merely for looks: a serial ID hands
    every member the count of accounts on the server and lets anyone guess their
    neighbour's ID. Collisions are checked against the table, not assumed away.

    Also refuses any ID that does not survive the login-nick round trip. SE's own
    scrambler (polnick) reads 8 base-36 digits into a value the byte chain treats
    as 41 bits wide, and 36**8 needs 41.36 -- so about 17% of the whole ID space
    scrambles to a nick that decodes back to a DIFFERENT ID. Issue one of those
    and two accounts eventually collide on one nick. The LLLLDDDD shape below has
    not produced a single failure in 200,000 samples, so this check is expected
    never to fire; it is here to make that a guarantee rather than a statistic.
    """
    for _ in range(1000):
        cand = "".join(alphabet[secrets.randbelow(len(alphabet))]
                       for alphabet in shape)
        if conn.execute("SELECT 1 FROM polid WHERE polid = ?",
                        (cand,)).fetchone() is not None:
            continue
        if polnick is not None and polnick.polid_for_nick(
                polnick.nick_for_polid(cand)) != cand:
            continue
        return cand
    raise RuntimeError("could not mint a free PlayOnline ID in 1000 tries")


#: The rule the client itself enforces on the field (string 22146). Applied to
#: anything an operator hands to `create_polid`, so a hand-made account cannot
#: repeat the un-typeable-ID bug in a quieter way.
def check_polid_policy(polid):
    """None if `polid` is enterable in the client's field, else why not."""
    polid = (polid or "").strip()
    if not polid:
        return "The PlayOnline ID is empty."
    if not all(c.isdigit() or ("A" <= c <= "Z") for c in polid):
        return (f"{polid!r} is not enterable in the client: a PlayOnline ID is "
                "capital letters and digits only (client string 22146).")
    return None


def create_polid(conn, polid, password, area_kbn="00", login_pf="01",
                 property_="00", status="active"):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    # A WARNING, never a refusal: auto-provisioning creates a POL ID from the
    # login NICK, and a login must not fail because that nick is oddly shaped.
    bad = check_polid_policy(polid)
    if bad:
        print(f"[accounts] warning: {bad}")
    pw_hash, pw_salt = hash_password(password)
    now = _now()
    conn.execute(
        "INSERT INTO polid (polid, pw_hash, pw_salt, status, area_kbn,"
        " login_pf, property, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (polid, pw_hash, pw_salt, status, area_kbn, login_pf, property_,
         now, now))
    conn.commit()
    return polid


def reissue_polid(conn, old, new=None):
    """Give an existing account a different PlayOnline ID, keeping everything
    else -- members, handles, content grants, profile, redeemed codes.

    Written to repair the accounts that were issued an un-typeable `NN-NNNNNNN`
    ID (see mint_polid). `polid` is a primary key that three tables reference
    with no ON UPDATE CASCADE, so this copies the parent row, repoints the
    children, and drops the old parent -- an UPDATE of the key itself would trip
    the foreign keys, and turning those off is not possible inside a transaction.

    A member whose `login_name` was the old ID (which is what registration sets)
    is renamed too, since that is the name the login path matches on.
    Returns the new ID.
    """
    row = conn.execute("SELECT * FROM polid WHERE polid = ?", (old,)).fetchone()
    if row is None:
        raise ValueError(f"no such PlayOnline ID: {old!r}")
    new = new or mint_polid(conn)
    bad = check_polid_policy(new)
    if bad:
        raise ValueError(bad)
    if conn.execute("SELECT 1 FROM polid WHERE polid = ?", (new,)).fetchone():
        raise ValueError(f"{new!r} is already in use")
    if conn.execute("SELECT 1 FROM member WHERE login_name = ? AND polid != ?",
                    (new, old)).fetchone():
        raise ValueError(f"{new!r} is already some other member's login name")
    with conn:
        conn.execute(
            "INSERT INTO polid (polid, pw_hash, pw_salt, status, area_kbn,"
            " login_pf, property, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new, row["pw_hash"], row["pw_salt"], row["status"],
             row["area_kbn"], row["login_pf"], row["property"],
             row["created_at"], _now()))
        conn.execute("UPDATE member  SET polid = ? WHERE polid = ?", (new, old))
        conn.execute("UPDATE profile SET polid = ? WHERE polid = ?", (new, old))
        conn.execute("UPDATE regcode SET redeemed_by = ? WHERE redeemed_by = ?",
                     (new, old))
        conn.execute("UPDATE member SET login_name = ? WHERE login_name = ?",
                     (new, old))
        conn.execute("DELETE FROM polid WHERE polid = ?", (old,))
    # The nick follows the ID. Drop the old binding first: leaving it behind
    # would let the previous ID keep logging in, which is not what "reissue"
    # means -- and the old ID is un-typeable anyway, which is why we are here.
    if polnick is not None:
        try:
            conn.execute("DELETE FROM login_alias WHERE nick = ?",
                         (polnick.nick_for_polid(old),))
            conn.commit()
        except ValueError:
            pass                        # the old ID was not an 8-char one
        for row in conn.execute("SELECT id FROM member WHERE polid = ?", (new,)):
            bind_login_nick(conn, row["id"], new)
    return new


def add_member(conn, polid, login_name, password, member_no=None,
               access_level=0):
    """Add a login member under `polid`. member_no defaults to the next free
    slot, which is the order the client's member list shows them in."""
    if member_no is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(member_no) + 1, 0) AS n FROM member"
            " WHERE polid = ?", (polid,)).fetchone()
        member_no = row["n"]
    pw_hash, pw_salt = hash_password(password)
    cur = conn.execute(
        "INSERT INTO member (polid, member_no, login_name, pw_hash, pw_salt,"
        " access_level, created_at) VALUES (?,?,?,?,?,?,?)",
        (polid, member_no, login_name, pw_hash, pw_salt, access_level, _now()))
    conn.commit()
    return cur.lastrowid


#: Maps the client's PML variables to `profile` columns. The registration page
#: binds these names, so this is the contract between the two -- keep it exact.
POLSIGNUP_FIELDS = {
    "$_POLSIGNUP_KANJI_FAMILY_NAME":    "kanji_family",
    "$_POLSIGNUP_KANJI_FIRST_NAME":     "kanji_first",
    "$_POLSIGNUP_KATAKANA_FAMILY_NAME": "katakana_family",
    "$_POLSIGNUP_KATAKANA_FIRST_NAME":  "katakana_first",
    "$_POLSIGNUP_ZIP_MAIN":             "zip_main",
    "$_POLSIGNUP_ZIP_SUB":              "zip_sub",
    "$_POLSIGNUP_USER_ADDRESS_0":       "address_0",
    "$_POLSIGNUP_USER_ADDRESS_1":       "address_1",
    "$_POLSIGNUP_USER_ADDRESS_2":       "address_2",
    "$_POLSIGNUP_USER_PHONE_0":         "phone_0",
    "$_POLSIGNUP_USER_PHONE_1":         "phone_1",
    "$_POLSIGNUP_USER_PHONE_2":         "phone_2",
}

PROFILE_COLUMNS = tuple(POLSIGNUP_FIELDS.values()) + ("country",)

#: SE's handle rule, quoted from the client's own string 0x3D1D: "Alphanumeric
#: characters and symbols only, maximum 15 characters".
HANDLE_MAX = 15


def check_handle_policy(handle):
    """Return None if acceptable, else a message suitable for display."""
    if not handle or len(handle) > HANDLE_MAX:
        return f"Handle must be 1-{HANDLE_MAX} characters."
    if not handle.isascii() or any(c.isspace() for c in handle):
        return "Handle may use letters, numbers and symbols only."
    return None


def set_profile(conn, polid, **fields):
    """Upsert the step-5 contact details. Unknown keys are rejected loudly."""
    bad = set(fields) - set(PROFILE_COLUMNS)
    if bad:
        raise ValueError(f"unknown profile fields: {sorted(bad)}")
    cols = list(fields)
    conn.execute(
        "INSERT INTO profile (polid, updated_at" +
        "".join(f", {c}" for c in cols) + ") VALUES (?,?" +
        ",?" * len(cols) + ")"
        " ON CONFLICT(polid) DO UPDATE SET updated_at = excluded.updated_at" +
        "".join(f", {c} = excluded.{c}" for c in cols),
        [polid, _now()] + [fields[c] for c in cols])
    conn.commit()


def get_profile(conn, polid):
    return conn.execute("SELECT * FROM profile WHERE polid = ?",
                        (polid,)).fetchone()


#: Lobby 05:01 field ids, each confirmed live by changing exactly that control in
#: the client and diffing the capture.
HANDLE_PROFILE_FIELDS = {
    0x03: "age",
    0x04: "approx_age",
    0x05: "sex",
    0x06: "loc_continent",
    0x07: "loc_country",
    0x08: "loc_region",
    0x09: "loc_city",
    0x0A: "language_1",
    0x0B: "language_2",
    0x0C: "language_3",
    0x0D: "occupation",
    0x0E: "interest_1",
    0x0F: "interest_2",
    0x10: "interest_3",
    0x12: "mail_address",
    0x13: "portrait",
}

#: WAS `frozenset((0x1D, 0x1E))`, on the reading that 0x1D "changes per message
#: with no UI cause (a counter or nonce)" and 0x1E "is a trailer in its own
#: section". Both were guesses made before the record's schema was known, and
#: both are WRONG: the 05:04 descriptor array (dumped live by pol-shim's
#: `profSchema` probe) names index 29 `z_utime` and index 30 `z_pnum`, and the
#: write-side TLV id IS the schema index. 0x1D "changing every message with no
#: UI cause" is exactly what an update TIMESTAMP does.
#:
#: They are now stored like any other field. Nothing is skipped -- if a value
#: turns out to need regenerating rather than replaying, that belongs in the
#: encoder where the semantics are known, not in a blanket drop here.
HANDLE_PROFILE_SKIP = frozenset()


def set_handle_profile(conn, handle_id, fields):
    """Upsert {field_id: int|str} from one 05:01. Unknown ids are KEPT."""
    now = _now()
    for fid, val in fields.items():
        if fid in HANDLE_PROFILE_SKIP:
            continue
        as_int = val if isinstance(val, int) else None
        as_text = None if isinstance(val, int) else str(val)
        conn.execute(
            "INSERT INTO handle_profile (handle_id, field_id, val_int, val_text,"
            " updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(handle_id, field_id) DO UPDATE SET"
            " val_int = excluded.val_int, val_text = excluded.val_text,"
            " updated_at = excluded.updated_at",
            (handle_id, fid, as_int, as_text, now))
    conn.commit()


def get_handle_profile(conn, handle_id):
    """{field_id: int|str} for one HANDLE."""
    out = {}
    for r in conn.execute("SELECT field_id, val_int, val_text FROM "
                          "handle_profile WHERE handle_id = ?", (handle_id,)):
        out[r["field_id"]] = r["val_text"] if r["val_int"] is None else r["val_int"]
    return out


# --------------------------------------------------------------------------- #
# handle identity on the wire
# --------------------------------------------------------------------------- #
#: A handle's 64-bit id, as the client understands it.
#:
#: The client does NOT see our `handle.id`; it sees the value packed into the
#: 0:9 handle record, and it hands that value straight back as the profile
#: record's `z_hid` (schema field 2) when it asks whose profile to show. So the
#: mapping has to be total and stable in both directions, which is what these
#: two helpers are.
#:
#: The width is not free. app.dll unpacks the record's 64-bit word as
#:
#:     guid = (word >> 1) & 0xFFF_FFFFFFFF      (44 bits -- shrd + `and ecx,0xfff`)
#:     slot = (word >> 45) & 0x3F               (which of the four handle slots)
#:
#: so anything at or above bit 44 is silently truncated, which rules out the
#: 0x5400... shape `add_friend` synthesises for non-local friends.
#:
#: The high bit is not decoration: a real z_hid captured off SE
#: (`00 50 35 00 00 08 00 00` -> 0x0000_0800_0035_5000) is exactly
#: 2**43 + 0x355000, so SE's own handle ids are `(1 << 43) | serial`. We mint the
#: same shape with our row id as the serial, which keeps every guid non-zero --
#: and zero is the value the client sends for "my own handle, I don't know its
#: id", so a guid of 0 would be indistinguishable from that.
#:
#: WARNING: 2026-08-24: that `(1<<43)` premise is CONTRADICTED by measurement. SE's Fox
#: handle guid is 0x511C75 (NO bit 43), confirmed three ways: the profile-screen
#: handle-model object dump, SE's own 0:9 Fox record, and SE's handle-profile z_hid
#: (5315701). The cited 0x080000355000 capture was evidently a different id. Our
#: bit-43 guid is the SOLE measured difference between the Viewer's per-Content-ID
#: profile SECTION working (SE) and dead (ours) -- see [[pol-content-id-profiles]].
#: POL_HANDLE_GUID_BASE lets us test the SE-shaped (no-bit-43) form reversibly;
#: default is UNCHANGED (1<<43) so nothing moves until the knob is set. `=0` mints
#: guid == handle_id (small, non-zero for any real handle). Whatever the base, the
#: guid must stay < 2**44 (the client reads only 44 bits) and non-zero.
try:
    HANDLE_GUID_BASE = int(os.environ.get("POL_HANDLE_GUID_BASE", str(1 << 43)), 0)
except ValueError:
    HANDLE_GUID_BASE = 1 << 43
HANDLE_GUID_MASK = (1 << 44) - 1


def handle_guid(handle_id):
    """The on-wire 64-bit id for one of our handles."""
    return HANDLE_GUID_BASE | (int(handle_id) & 0xFFFFFFFF)


def learn_client_guid(conn, handle_id, value):
    """Record what a client calls itself, if it is telling us something new."""
    value = int(value) & 0xFFFFFFFFFFFFFFFF
    if not value:
        return False
    row = conn.execute("SELECT client_guid FROM handle WHERE id = ?",
                       (int(handle_id),)).fetchone()
    if row is None or (row["client_guid"] or 0) == value:
        return False
    conn.execute("UPDATE handle SET client_guid = ? WHERE id = ?",
                 (value, int(handle_id)))
    conn.commit()
    return True


def handle_by_client_guid(conn, value):
    """The handle that calls itself `value`, or None."""
    if not value:
        return None
    return conn.execute("SELECT * FROM handle WHERE client_guid = ?",
                        (int(value) & 0xFFFFFFFFFFFFFFFF,)).fetchone()


def handle_by_guid(conn, guid):
    """The `handle` row a wire guid names, or None if it names nobody of ours."""
    guid = int(guid) & HANDLE_GUID_MASK
    if not guid or (guid & ~0xFFFFFFFF) != HANDLE_GUID_BASE:
        return None
    return conn.execute("SELECT * FROM handle WHERE id = ?",
                        (guid & 0xFFFFFFFF,)).fetchone()


def primary_handle_row(conn, member_id):
    """A member's default handle ROW (the one the badge shows), or None.

    `primary_handle` below returns just the name and predates this; the row is
    what anything handle-scoped needs, since it carries the id the guid and the
    per-handle profile are keyed on.
    """
    return conn.execute(
        "SELECT * FROM handle WHERE member_id = ?"
        " ORDER BY is_primary DESC, id ASC LIMIT 1", (member_id,)).fetchone()


def assign_mail_address(conn, member_id, local=None):
    """Give a member a PlayOnline Mail address.

    Default form copies the real one seen on SE's mail screen --
    `x` + 12 digits + `@pol.com` -- which is what the service assigns before you
    pick a friendlier name.
    """
    if local is None:
        local = "x" + "".join(str(secrets.randbelow(10)) for _ in range(12))
    addr = f"{local}@pol.com"
    conn.execute("UPDATE member SET mail_address = ? WHERE id = ?",
                 (addr, member_id))
    conn.commit()
    return addr


#: SE's mail screen: "4 to 15 characters long and may consist of lowercase
#: letters, numbers, and symbols."
MAIL_LOCAL_MIN, MAIL_LOCAL_MAX = 4, 15


def check_mail_local(local):
    if not local or not (MAIL_LOCAL_MIN <= len(local) <= MAIL_LOCAL_MAX):
        return (f"Mail name must be {MAIL_LOCAL_MIN}-{MAIL_LOCAL_MAX} "
                "characters.")
    if local != local.lower() or not local.isascii() or "@" in local:
        return "Mail name must use lowercase letters, numbers and symbols."
    return None


def set_mail_password(conn, member_id, password, store_plain=True):
    """Set a member's mail password.

    Always stores the salted hash; `store_plain` additionally keeps the
    plaintext, which is the ONLY way APOP can be verified (see the migration
    note). Pass store_plain=False to keep hash-only and accept any APOP digest.
    """
    h, s = hash_password(password)
    conn.execute("UPDATE member SET mail_pw_hash = ?, mail_pw_salt = ?, "
                 "mail_pw_plain = ? WHERE id = ?",
                 (h, s, password if store_plain else None, member_id))
    conn.commit()


# --------------------------------------------------------------------------- #
# PlayOnline Mail storage
# --------------------------------------------------------------------------- #
def mail_box_name(address):
    """Normalise an address (or bare local part) to a mailbox key.

    The Viewer sends the POP3 user id as whatever the account's "Account Name"
    field holds -- observed both as `cas` and as `cas@pol.com` -- so both have to
    land in the same mailbox.
    """
    if not address:
        return ""
    return str(address).strip().split("@", 1)[0].lower()


def member_by_mail(conn, address):
    """The member that owns a mailbox, or None if the box has no account.

    The local part goes into a LIKE pattern, so it is ESCAPED: mail names may
    contain symbols (SE's own rule is "lowercase letters, numbers, and symbols"),
    and an unescaped `_` is LIKE's single-character wildcard -- so the mailbox
    `a_b` matched `axb@pol.com` and handed one member's mail to another.
    """
    box = mail_box_name(address)
    if not box:
        return None
    pat = (box.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
           + "@%")
    row = conn.execute(
        "SELECT * FROM member WHERE lower(mail_address) = ? "
        "   OR lower(mail_address) LIKE ? ESCAPE '\\' LIMIT 1",
        (box, pat)).fetchone()
    return row


def deliver_mail(conn, address, raw, sender=None, subject=None, uidl=None):
    """Put one RFC822 message in a mailbox. Returns its UIDL.

    UIDL defaults to a content hash, which makes redelivery of the identical
    message idempotent instead of showing it twice.
    """
    box = mail_box_name(address)
    if not box:
        raise ValueError("no mailbox in address %r" % (address,))
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if uidl is None:
        uidl = hashlib.sha1(raw).hexdigest()[:24]
    member = member_by_mail(conn, box)
    conn.execute(
        "INSERT OR IGNORE INTO mail (box, member_id, uidl, sender, subject, "
        "                            raw, received_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (box, member["id"] if member else None, uidl, sender, subject,
         raw, _now()))
    conn.commit()
    return uidl


def list_mail(conn, address, include_deleted=False):
    """Live messages in a mailbox, oldest first (POP3 numbers them 1..N)."""
    box = mail_box_name(address)
    sql = "SELECT * FROM mail WHERE box = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return conn.execute(sql + " ORDER BY id", (box,)).fetchall()


def delete_mail(conn, mail_ids):
    """Commit a POP3 DELE set. Soft delete: the row stays for forensics."""
    now = _now()
    conn.executemany("UPDATE mail SET deleted_at = ? WHERE id = ? "
                     "  AND deleted_at IS NULL",
                     [(now, i) for i in mail_ids])
    conn.commit()


def set_handle(conn, member_id, handle_name, primary=None):
    """Register a handle name.

    `primary` controls the badge:
      True  -> promote this handle, demoting the current primary.
      False -> add it WITHOUT promotion.
      None  -> (default) promote ONLY when the member has no primary handle yet.

    The default used to be True, so any handle the client volunteered on 0:8 --
    including a throwaway test handle -- stole the badge from the real one
    (registering "Sam" displaced "Fox"). Promotion is now opt-in: the first
    handle becomes the badge and later registrations are added quietly. The
    client's "set as primary" action, when we wire it, passes primary=True."""
    if primary is None:
        primary = conn.execute(
            "SELECT 1 FROM handle WHERE member_id = ? AND is_primary = 1",
            (member_id,)).fetchone() is None
    if primary:
        conn.execute("UPDATE handle SET is_primary = 0 WHERE member_id = ?",
                     (member_id,))
    # An explicit (re)registration overrides a prior deletion: lift the tombstone
    # so the handle is allowed back. The 0:8 auto-capture never reaches here for a
    # tombstoned name (it is filtered first), so this only fires on a deliberate add.
    conn.execute("DELETE FROM deleted_handle WHERE member_id = ? AND handle_name = ?",
                 (member_id, handle_name))
    cur = conn.execute(
        "INSERT INTO handle (member_id, handle_name, is_primary, created_at)"
        " VALUES (?,?,?,?)",
        (member_id, handle_name, 1 if primary else 0, _now()))
    conn.commit()
    return cur.lastrowid


class RegistrationError(Exception):
    """A sign-up that cannot be completed, with a message fit to show the user."""


def register_account(conn, handle, password, code=None, profile=None,
                     contents=(1,), member_login=None):
    """Create a whole account IN ONE TRANSACTION: POL ID, member, handle, its
    content grants, the contact profile and a mail address.

    ATOMIC ON PURPOSE. The sign-up page used to call create_polid / add_member /
    set_handle in sequence, and each of those commits: a handle name already
    taken raised on the third, after the first two were durable, so the user got
    "Server error" and the database kept an orphan POL ID with a member and no
    handle. Now either the whole account exists or none of it does.

    Raises RegistrationError with a displayable message when the handle is taken
    (the one collision a user can actually hit and fix).
    """
    handle = (handle or "").strip()
    bad = check_handle_policy(handle)
    if bad:
        raise RegistrationError(bad)
    bad = check_password_policy(password)
    if bad:
        raise RegistrationError(bad)
    if conn.execute("SELECT 1 FROM handle WHERE handle_name = ?",
                    (handle,)).fetchone():
        raise RegistrationError(
            f"The handle {handle!r} is already in use. Please choose another.")

    pw_hash, pw_salt = hash_password(password)
    now = _now()
    granted = list(contents or ())
    try:
        # One transaction. `with conn` commits on success and rolls back on any
        # exception -- including the UNIQUE violation that a second sign-up
        # racing this one would raise, which is why the check above is a
        # courtesy rather than the guarantee.
        with conn:
            polid = mint_polid(conn)
            conn.execute(
                "INSERT INTO polid (polid, pw_hash, pw_salt, status, area_kbn,"
                " login_pf, property, created_at, updated_at)"
                " VALUES (?,?,?,'active','00','01','00',?,?)",
                (polid, pw_hash, pw_salt, now, now))
            cur = conn.execute(
                "INSERT INTO member (polid, member_no, login_name, pw_hash,"
                " pw_salt, access_level, created_at) VALUES (?,0,?,?,?,0,?)",
                (polid, member_login or polid, pw_hash, pw_salt, now))
            member_id = cur.lastrowid
            hcur = conn.execute(
                "INSERT INTO handle (member_id, handle_name, is_primary,"
                " created_at) VALUES (?,?,1,?)", (member_id, handle, now))
            handle_id = hcur.lastrowid
            if code:
                row = conn.execute(
                    "SELECT * FROM regcode WHERE code = ? COLLATE NOCASE"
                    " AND redeemed_by IS NULL",
                    (normalise_regcode(code),)).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE regcode SET redeemed_at = ?, redeemed_by = ?"
                        " WHERE code = ?", (now, polid, row["code"]))
                    granted = [int(c) for c in row["contents"].split(",")
                               if c.strip()] or granted
            for c in granted:
                conn.execute(
                    "INSERT INTO content (member_id, content_code, status,"
                    " registered_at) VALUES (?,?,'active',?)"
                    " ON CONFLICT(member_id, content_code) DO UPDATE SET"
                    " status = 'active'", (member_id, int(c), now))
                # AND APPLY IT TO THE FIRST HANDLE, which is the one created
                # three statements up. This is what real PlayOnline does: content
                # registered at sign-up lands on your first handle, and you move
                # it between handles afterwards. A grant is only the entitlement
                # -- lobby 1:3 builds the launcher's character table out of
                # `handle_content` alone (responders._db_chars), so without this
                # the account owns every title on the panel and can launch none
                # of them ("You have no Content ID for <game>").
                #
                # register_account was the ONLY one of the three provisioning
                # paths missing this: ensure_member() and kinou 31's redeem both
                # call link_member_content_to_primary(). It is also the path a
                # real new player takes, so every in-client sign-up produced an
                # account with six grants and zero links.
                #
                # Inlined rather than calling link_content_to_handle(), which
                # commits -- that would break the one-transaction guarantee this
                # whole function is built around (see the docstring).
                #
                # `COALESCE(content_id, excluded.content_id)` -- the EXISTING id
                # wins, which is the reverse of the usual upsert and is the
                # never-re-mint rule written in SQL (see allocate_content_id).
                # An id already on this row has been served to a client that
                # named local files after it; a fresh serial arriving on top of
                # it would orphan them. Nothing can conflict here today (the
                # handle is three statements old), so this costs a burned serial
                # out of a 70-million pool in a case that cannot arise, and
                # closes the door anyway.
                conn.execute(
                    "INSERT INTO handle_content (handle_id, content_code,"
                    " slot, content_id, status, linked_at)"
                    " VALUES (?,?,0,?,'active',?)"
                    " ON CONFLICT(handle_id, content_code, slot) DO UPDATE SET"
                    " content_id = COALESCE(content_id, excluded.content_id),"
                    " status = 'active'",
                    (handle_id, int(c), allocate_content_id(conn), now))
                # FFXI issues one Content ID per CHARACTER. Grant the whole set
                # at sign-up rather than on demand: the bridge offers a member's
                # unspent ids as the client's empty character slots, so slots
                # that exist here are slots the player can actually create into,
                # and slots that do not are POL-0001 with no explanation. Inside
                # the same transaction as everything else -- ensure_content_slots
                # does not commit, by contract.
                if int(c) == FFXI_CONTENT_CODE:
                    ensure_content_slots(conn, handle_id, FFXI_CONTENT_CODE,
                                         FFXI_CHARACTER_SLOTS)
            if profile:
                cols = [k for k in PROFILE_COLUMNS if profile.get(k)]
                if cols:
                    conn.execute(
                        "INSERT INTO profile (polid, updated_at"
                        + "".join(f", {c}" for c in cols) + ") VALUES (?,?"
                        + ",?" * len(cols) + ")",
                        [polid, now] + [profile[c] for c in cols])
            local = "x" + "".join(str(secrets.randbelow(10)) for _ in range(12))
            conn.execute("UPDATE member SET mail_address = ? WHERE id = ?",
                         (f"{local}@pol.com", member_id))
            # In the SAME transaction as the account: without it the account
            # exists but cannot be logged into, which is exactly the failure
            # this whole thing was written to fix. See bind_login_nick.
            if polnick is not None:
                conn.execute(
                    "INSERT INTO login_alias (nick, member_id, note, created_at)"
                    " VALUES (?,?,?,?) ON CONFLICT(nick) DO UPDATE SET"
                    " member_id = excluded.member_id, note = excluded.note",
                    (polnick.nick_for_polid(polid), member_id,
                     f"scrambled form of {polid}", now))
    except sqlite3.IntegrityError as exc:
        raise RegistrationError(
            "That handle was taken while you were registering. "
            "Please choose another.") from exc
    return {"polid": polid, "member_id": member_id, "handle": handle,
            "contents": granted, "mail": f"{local}@pol.com"}


def is_handle_deleted(conn, member_id, handle_name):
    """True if this handle was deleted (tombstoned) and must not be re-captured."""
    return conn.execute(
        "SELECT 1 FROM deleted_handle WHERE member_id = ? AND handle_name = ?",
        (member_id, handle_name)).fetchone() is not None


def delete_handle(conn, member_id, handle_name):
    """Delete a handle and TOMBSTONE it, so the 0:8 auto-capture will not re-add it
    when the client re-volunteers its locally-cached table. If the deleted handle
    was the primary, the oldest remaining handle inherits the badge. Returns True
    if a handle row was actually removed."""
    row = conn.execute(
        "SELECT id, is_primary FROM handle WHERE member_id = ? AND handle_name = ?",
        (member_id, handle_name)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO deleted_handle (member_id, handle_name, deleted_at)"
        " VALUES (?,?,?)", (member_id, handle_name, _now()))
    if row is None:
        conn.commit()
        return False
    conn.execute("DELETE FROM handle WHERE id = ?", (row["id"],))
    if row["is_primary"]:
        nxt = conn.execute(
            "SELECT id FROM handle WHERE member_id = ? ORDER BY created_at LIMIT 1",
            (member_id,)).fetchone()
        if nxt:
            conn.execute("UPDATE handle SET is_primary = 1 WHERE id = ?", (nxt["id"],))
    conn.commit()
    return True


def _account_ids(conn, polid):
    """(member ids, handle ids, handle names) belonging to `polid`."""
    members = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM member WHERE polid = ?", (polid,))]
    handles, names = [], []
    if members:
        q = ",".join("?" * len(members))
        for r in conn.execute(
                f"SELECT id, handle_name FROM handle WHERE member_id IN ({q})",
                members):
            handles.append(int(r["id"]))
            names.append(r["handle_name"])
    return members, handles, names


def account_footprint(conn, polid):
    """Everything `delete_polid` would destroy, counted. None if no such account.

    Deletion is irreversible and there is no undo anywhere in this database, so
    the operator is shown the inventory FIRST -- in particular the two counts
    that are not obvious from the accounts list: how many entries on OTHER
    people's friend lists name this account (those go too, or they would point
    at nobody), and whether it is signed in right now.
    """
    row = conn.execute("SELECT * FROM polid WHERE polid = ?", (polid,)).fetchone()
    if row is None:
        return None
    members, handles, names = _account_ids(conn, polid)

    def count(sql, params):
        return int(conn.execute(sql, params).fetchone()[0])

    mem_q = ",".join("?" * len(members)) or "NULL"
    h_q = ",".join("?" * len(handles)) or "NULL"
    n_q = ",".join("?" * len(names)) or "NULL"
    boxes = [mail_box_name(r["mail_address"]) for r in conn.execute(
        f"SELECT mail_address FROM member WHERE id IN ({mem_q})", members)
        if r["mail_address"]]
    b_q = ",".join("?" * len(boxes)) or "NULL"

    out = {
        "polid": polid,
        "status": row["status"],
        "created_at": row["created_at"],
        "members": [dict(r) for r in conn.execute(
            f"SELECT id, member_no, login_name, mail_address, status"
            f" FROM member WHERE id IN ({mem_q}) ORDER BY member_no", members)],
        "handles": names,
        "contents": [int(r["content_code"]) for r in conn.execute(
            f"SELECT content_code FROM content WHERE member_id IN ({mem_q})"
            f" AND status = 'active' ORDER BY content_code", members)],
        "friends": count(f"SELECT COUNT(*) FROM friend WHERE handle_id IN ({h_q})"
                         f" AND kind != {KIND_GROUP}", handles),
        "groups": count(f"SELECT COUNT(*) FROM friend WHERE handle_id IN ({h_q})"
                        f" AND kind = {KIND_GROUP}", handles),
        # On OTHER people's lists. peer_handle is only set when the friendship
        # was made against a local handle; a row added by name alone (the 2:6
        # whole-list PUT does that) carries the name and a guid, so match both.
        "referenced_by": count(
            f"SELECT COUNT(*) FROM friend WHERE (peer_handle IN ({h_q})"
            f" OR peer_name IN ({n_q})) AND handle_id NOT IN ({h_q})",
            handles + names + handles),
        "group_memberships": count(
            f"SELECT COUNT(*) FROM group_member WHERE member_handle IN ({h_q})"
            f" OR member_name IN ({n_q})", handles + names),
        "mail": count(f"SELECT COUNT(*) FROM mail WHERE member_id IN ({mem_q})"
                      f" OR box IN ({b_q})", members + boxes),
        "sessions": count(f"SELECT COUNT(*) FROM session WHERE member_id IN ({mem_q})",
                          members),
        "regcodes": [r["code"] for r in conn.execute(
            "SELECT code FROM regcode WHERE redeemed_by = ?", (polid,))],
    }
    out["online"] = any(member_online(conn, m) for m in members)
    return out


def delete_polid(conn, polid, release_codes=False):
    """Erase an account: its members, handles, grants, friends, mail, sessions.

    Returns the `account_footprint` of what was removed, or None if there was no
    such account.

    Every child row is deleted EXPLICITLY rather than left to the schema's
    `ON DELETE CASCADE`. Those cascades do not fire: `PRAGMA foreign_keys = ON`
    is per-CONNECTION and only ever runs inside `connect`'s one-per-process
    `executescript`, so every other connection this server opens has enforcement
    OFF. Relying on the cascade would leave a database full of orphans -- handles
    with no member, friend rows pointing at nothing -- that still answer lobby
    queries. The order below is children-before-parents so a partial failure
    inside the transaction cannot strand a parent either.

    Two kinds of row belong to somebody ELSE and are still removed: friend
    entries on other handles' lists that name this account, and its rows in other
    people's groups. Leaving them behind is not "preserving their data" -- it
    leaves entries the lobby cannot resolve, which is exactly the ghost-row state
    `friend-list-one-record-bug` was about.

    `release_codes` returns the registration code(s) this account redeemed to the
    unused pool. The default keeps them marked redeemed, so the audit trail of
    which code created which account survives the account itself.
    """
    footprint = account_footprint(conn, polid)
    if footprint is None:
        return None
    members, handles, names = _account_ids(conn, polid)
    mem_q = ",".join("?" * len(members)) or "NULL"
    h_q = ",".join("?" * len(handles)) or "NULL"
    n_q = ",".join("?" * len(names)) or "NULL"
    boxes = [mail_box_name(m["mail_address"]) for m in footprint["members"]
             if m["mail_address"]]
    b_q = ",".join("?" * len(boxes)) or "NULL"
    groups = [int(r["id"]) for r in conn.execute(
        f"SELECT id FROM friend WHERE handle_id IN ({h_q}) AND kind = {KIND_GROUP}",
        handles)]
    g_q = ",".join("?" * len(groups)) or "NULL"

    with conn:
        # handle-scoped
        conn.execute(f"DELETE FROM group_member WHERE group_id IN ({g_q})", groups)
        conn.execute(f"DELETE FROM group_member WHERE member_handle IN ({h_q})"
                     f" OR member_name IN ({n_q})", handles + names)
        conn.execute(f"DELETE FROM friend WHERE handle_id IN ({h_q})"
                     f" OR peer_handle IN ({h_q}) OR peer_name IN ({n_q})",
                     handles + handles + names)
        conn.execute(f"DELETE FROM handle_content WHERE handle_id IN ({h_q})", handles)
        conn.execute(f"DELETE FROM handle_profile WHERE handle_id IN ({h_q})", handles)
        conn.execute(f"DELETE FROM handle WHERE id IN ({h_q})", handles)
        # member-scoped
        conn.execute(f"DELETE FROM deleted_handle WHERE member_id IN ({mem_q})", members)
        conn.execute(f"DELETE FROM login_alias WHERE member_id IN ({mem_q})", members)
        conn.execute(f"DELETE FROM content WHERE member_id IN ({mem_q})", members)
        conn.execute(f"DELETE FROM session WHERE member_id IN ({mem_q})", members)
        # Mail is keyed on the BOX, and a message delivered before the address
        # resolved to a member has member_id NULL -- so both keys are swept, or
        # the next account to be given that mail name inherits the old one's inbox.
        conn.execute(f"DELETE FROM mail WHERE member_id IN ({mem_q})"
                     f" OR box IN ({b_q})", members + boxes)
        conn.execute(f"DELETE FROM member WHERE id IN ({mem_q})", members)
        # polid-scoped
        conn.execute("DELETE FROM profile WHERE polid = ?", (polid,))
        # `redeemed_by` is a foreign key into the row about to go, so it has to
        # be cleared either way -- on the connection that DOES have
        # `foreign_keys` on (see connect), leaving it dangling aborts the whole
        # delete with an IntegrityError. `redeemed_at` is what keeps the code
        # spent afterwards; check_regcode tests both for exactly this reason.
        conn.execute(
            "UPDATE regcode SET redeemed_by = NULL"
            + (", redeemed_at = NULL" if release_codes else "")
            + " WHERE redeemed_by = ?", (polid,))
        conn.execute("DELETE FROM polid WHERE polid = ?", (polid,))

    footprint["released_codes"] = footprint["regcodes"] if release_codes else []
    print(f"[accounts] deleted {polid!r}: {len(footprint['members'])} member(s), "
          f"{len(footprint['handles'])} handle(s), {footprint['mail']} message(s), "
          f"{footprint['referenced_by']} entry/entries on other people's lists"
          + (f"; released {len(footprint['regcodes'])} code(s)" if release_codes
             else ""), flush=True)
    return footprint


#: Every handle belonging to the member who owns this handle, the handle itself
#: excluded. Used to keep a content code on exactly one handle at a time.
_SIBLING_HANDLES = (
    "SELECT id FROM handle WHERE member_id ="
    " (SELECT member_id FROM handle WHERE id = ?) AND id != ?")

#: Every handle of a member, by member id.
_MEMBER_HANDLES = "SELECT id FROM handle WHERE member_id = ?"


def grant_content(conn, member_id, content_code, content_no=None):
    """Subscribe a member to a title. Idempotent: re-granting reactivates."""
    conn.execute(
        "INSERT INTO content (member_id, content_code, content_no, status,"
        " registered_at) VALUES (?,?,?, 'active', ?)"
        " ON CONFLICT(member_id, content_code) DO UPDATE SET"
        " status = 'active', content_no = COALESCE(excluded.content_no, content_no)",
        (member_id, content_code, content_no, _now()))
    # Re-granting has to revive the LINK as well, or the entitlement comes back
    # and the title still does not: lobby 1:3 reads handle_content, and
    # revoke_content deactivates the link rather than dropping it (so the handle
    # placement and its Content ID survive a cancel/re-register round trip).
    # Nothing is created here -- an unplaced grant is placed by
    # link_member_content_to_primary.
    conn.execute(
        f"UPDATE handle_content SET status = 'active' WHERE content_code = ?"
        f" AND handle_id IN ({_MEMBER_HANDLES})", (content_code, member_id))
    conn.commit()


def revoke_content(conn, member_id, content_code):
    """Cancel a member's licence for a title, on every handle they hold.

    THE LINK MUST GO TOO. Lobby 1:3 builds the launcher's character table out of
    `handle_content` alone, and `handle_content_list` filters on that row's own
    status -- not on the `content` grant behind it. Deactivating only the grant
    therefore cancelled nothing the player could see: the title stayed on the
    handle and stayed launchable. That is the whole job of Service & Support's
    cancel-a-content-licence screen, so it has to be the cancel that reaches the
    client.

    Deactivated, not deleted, so a re-register restores the same Content ID on
    the same handle (see grant_content).
    """
    conn.execute(
        "UPDATE content SET status = 'inactive'"
        " WHERE member_id = ? AND content_code = ?", (member_id, content_code))
    conn.execute(
        f"UPDATE handle_content SET status = 'inactive' WHERE content_code = ?"
        f" AND handle_id IN ({_MEMBER_HANDLES})", (content_code, member_id))
    conn.commit()


# --- per-handle Content ID links -------------------------------------------- #
#: THE SHAPE OF A REAL SE CONTENT ID, measured 2026-08-23 and the reason the
#: mint below allocates instead of computing. A real SE FFXI Content ID was read
#: off the live SE-connected Viewer TWO independent ways -- `work/pc/polcontent.py`
#: off the 64-slot content table at `polcore+0x403080` (slot `+0x08`), and the
#: pol-shim `[pay]` capture of the real `1:3` reply (id at record offset 0x10,
#: little-endian) -- and the two agree byte for byte. It is a plain ~8-digit
#: integer in the tens of millions. The value and its provenance live in the
#: GITIGNORED `Ignored Files/content-ids.md`; this file cites that path and never
#: quotes it, because a Content ID names a real account even though it cannot log
#: in.
#:
#: FLOOR/CEILING are OURS, not SE's: SE's own allocator bounds are unknown, and
#: unknowable from one sample. The floor only has to put our serials inside the
#: right magnitude, and the ceiling only has to be the last 8-digit value, so a
#: mint that ever ran past the shape it is imitating fails loudly instead of
#: quietly issuing a 9-digit id.
CONTENT_ID_FLOOR = 30_000_000
CONTENT_ID_CEILING = 99_999_999

#: The window "this number could be a Content ID". It is the MINT window widened
#: only to the full 8-digit range, because that is the shape SE issues and now
#: the only shape we issue.
#:
#: WARNING: **THE LEGACY 10-DIGIT FORMAT IS RETIRED (2026-08-23).** It used to be
#: admitted here -- the window ran to 1_999_999_999 so the computed
#: `1000000000 + member.id * 100 + content_code` ids would still resolve -- and
#: it no longer is, because `tools/content_id_migrate.py` re-minted every one of
#: them and nothing in the database carries that shape any more.
#:
#: A retired id can still ARRIVE, and that is worth saying out loud rather than
#: letting it fall through as "not a Content ID": a client caches the 64-slot
#: content table from lobby `1:3` at LOGIN, so a player who was connected across
#: the migration keeps sending the old value until they relog.
#: `looks_like_retired_content_id` exists for exactly that, and the lobby's
#: subject resolver uses it to log the real reason instead of a shrug.
CONTENT_ID_MIN = 10_000_000
CONTENT_ID_MAX = 99_999_999

#: The window the retired computed mint issued into. Kept ONLY to recognise a
#: stale value and name it; nothing may resolve one.
RETIRED_CONTENT_ID_LO = 1_000_000_000
RETIRED_CONTENT_ID_HI = 1_999_999_999


def content_id_int(value):
    """A stored Content ID as the NUMBER the wire carries, or None.

    `handle_content.content_id` is TEXT, and it holds two shapes: the legacy
    computed ids are zero-padded to 10 digits and the allocated ones are plain
    8-digit decimals. Comparing them as strings therefore misses; comparing them
    as integers cannot. Every consumer that matches a Content ID should come
    through here rather than formatting one.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def looks_like_content_id(value):
    """Could this number be one of ours? 8 digits -- see CONTENT_ID_MIN."""
    n = content_id_int(value)
    return n is not None and CONTENT_ID_MIN <= n <= CONTENT_ID_MAX


def looks_like_retired_content_id(value):
    """Is this a Content ID in the RETIRED 10-digit computed shape?

    True means "a real Content ID that we used to issue and no longer do" --
    almost always a client that has not relogged since the migration. It is not
    a resolvable id and must never be treated as one; it exists so a caller can
    say WHY it is refusing instead of reporting the value as meaningless.
    """
    n = content_id_int(value)
    return n is not None and RETIRED_CONTENT_ID_LO <= n <= RETIRED_CONTENT_ID_HI


def character_names(conn):
    """{(content_id_int, content_code): character name} -- the GAME character's
    own name for each Content ID we know one for.

    This is the read side of `tools/ffxi_names.py`, whose docstring named "the
    Content ID list UI" as the later step and left this table standalone so it
    could land while other work was in flight. It has landed; this is that step.

    WARNING: THE TABLE IS CREATED BY THAT TOOL, NOT BY `SCHEMA`, so a database the tool
    has never been run against does not have it. That is not an error and must
    not read like one -- it means "we know no character names yet", and the
    caller falls back. Hence the `OperationalError` arm rather than a migration:
    creating the table here would make an empty one look authoritative on every
    server that has never imported a name.

    Keyed on the id as a NUMBER (`content_id_int`) because `handle_content` and
    `content_character` both store it as TEXT in two different widths -- the
    legacy 10-digit computed rows and the 8-digit allocated ones -- so a string
    compare silently misses. Same trap, same fix, as `handle_by_content_id`.
    """
    out = {}
    try:
        rows = conn.execute("SELECT content_id, content_code, character_name "
                            "FROM content_character").fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        cid = content_id_int(r["content_id"])
        name = (r["character_name"] or "").strip()
        if cid is not None and name:
            out[(cid, int(r["content_code"] or 0))] = name
    return out


def handle_by_content_id(conn, value, active_only=False):
    """The handle a Content ID is linked to, or None. Matched NUMERICALLY.

    The lobby needs this because the chat-room member list names a member by the
    Content ID they are logged in under, and "View profile" sends that as the
    z_hid (measured live on prod 2026-08-19T00:15; before it resolved, the reply
    was built for the wrong subject and the client crashed on the mismatch).
    """
    n = content_id_int(value)
    if n is None:
        return None
    q = ("SELECT handle_id FROM handle_content WHERE content_id IS NOT NULL"
         " AND CAST(content_id AS INTEGER) = ?")
    if active_only:
        q += " AND status = 'active'"
    row = conn.execute(q, (n,)).fetchone()
    if row is None:
        return None
    return conn.execute("SELECT * FROM handle WHERE id = ?",
                        (int(row["handle_id"]),)).fetchone()


def _content_id_taken(conn, value):
    """Is this number already issued to anybody? Numeric, and across BOTH stores.

    `content.content_no` is the operator-supplied override (a real SE id typed in
    by hand); it never went through the counter, so the counter has to be told
    about it here or it would eventually hand the same number to somebody else.
    """
    return conn.execute(
        "SELECT 1 FROM handle_content WHERE content_id IS NOT NULL"
        "   AND CAST(content_id AS INTEGER) = ?"
        " UNION ALL"
        " SELECT 1 FROM content WHERE content_no IS NOT NULL"
        "   AND CAST(content_no AS INTEGER) = ?"
        " LIMIT 1", (int(value), int(value))).fetchone() is not None


def _content_id_seed(conn):
    """Where the counter starts on a database that has never allocated one.

    The highest id already issued IN OUR WINDOW, plus one, floored. The window
    matters: seeding off `MAX(content_id)` unfiltered would read a legacy
    10-digit id and start the counter at 1000000305, which is the exact shape
    this change exists to stop minting.
    """
    row = conn.execute(
        "SELECT MAX(CAST(content_id AS INTEGER)) AS hi FROM handle_content"
        " WHERE content_id IS NOT NULL"
        "   AND CAST(content_id AS INTEGER) BETWEEN ? AND ?",
        (CONTENT_ID_FLOOR, CONTENT_ID_CEILING)).fetchone()
    return max(CONTENT_ID_FLOOR, int((row and row["hi"]) or 0) + 1)


def allocate_content_id(conn):
    """Hand out the next Content ID. ALLOCATED AND TRACKED, not computed.

    Returns the decimal string that goes in `handle_content.content_id`.

    WARNING -- **THIS DOES NOT COMMIT.** The counter bump and the row that
    consumes it belong in ONE transaction, or a rolled-back registration keeps
    the bump while `register_account`'s single-transaction guarantee says nothing
    happened. Every caller here is already inside a transaction; keep it that
    way. Two connections allocating at once serialise on SQLite's write lock,
    which is what makes the counter safe without a second mechanism.

    WHAT THIS REPLACED, AND WHY IT WAS WRONG IN KIND (2026-08-23). The old mint
    was `_default_content_id(member_id, content_code)` = `1000000000 +
    member.id * 100 + content_code`, zero-padded to 10 digits: a value COMPUTED
    from the account, with the game in its last two digits. A real SE Content ID
    then arrived (see the CONTENT_ID_FLOOR banner above) and falsified all three
    premises at once --

      * it is ~8 digits, a plain integer in the tens of millions; there is no
        `1000000000` base;
      * the game/service is a SEPARATE u16 content-code field and is NOT encoded
        in the id, so "the last digits are the game" was an artifact of OUR OWN
        invented numbers, and is retracted for SE;
      * two real SE HANDLE ids on two different accounts sit ~38k apart, which is
        the signature of a sequential serial out of a shared pool rather than
        anything derived from an account. SE allocates and tracks these; it does
        not compute them.

    So the shape we can be faithful to is "an opaque serial in the right
    magnitude, unique forever", and that is exactly what this is. **We cannot
    reproduce SE's actual numbers** -- they are SE's allocations, and no amount of
    RE on our side recovers them -- so the goal is the SHAPE plus a uniqueness
    guarantee, never a specific value.

    **NO CHECK DIGIT.** Nothing we hold validates the format: the launch gate
    (`app.dll+0x199093`) compares the u16 CONTENT CODE, not the id, and FFXI's
    world lookup compares the id for equality against what we ourselves served.
    Inventing a checksum would be inventing a constraint, which is the class of
    error the old mint died of. If a validator ever turns up, add it then.

    **NEW MINTS ONLY -- NEVER RE-MINT AN EXISTING ROW.** The FFXI client names a
    character's local files by the Content ID in hex
    (`FINAL FANTASY XI/USER/<hexid>/`), so changing an id that has already been
    served orphans that user's macros and config. Every call site here mints only
    where no row exists yet.

    There is exactly ONE sanctioned exception, and it is not on any code path:
    `tools/content_id_migrate.py`, run deliberately by an operator, which is what
    retired the legacy 10-digit ids on 2026-08-23. It is a tool rather than a
    startup migration precisely BECAUSE of the directory above -- it emits a
    mapping file that `work/pc/ffxi_userdir_rename.py` applies on each player's
    machine, and a re-mint without that second half silently costs them every
    macro they ever wrote. If you ever need to re-mint again, extend that tool;
    do not add a re-mint here.

    **A CONTENT ID BELONGS TO EXACTLY ONE HANDLE** (POL-7169/7187/5326), and it
    must stay unique across ALL POL IDs, not merely within one account. That was
    a LIVE BUG until 2026-08-15 and it is the thing this function must never let
    happen again: the old mint keyed on `member_no`, which is the member's SLOT
    WITHIN a POL ID (`0` for the first member of every account), so eight
    separate accounts all minted `1000000001` and every one of them resolved to
    the same FFXI character. A serial out of a single server-wide counter cannot
    reproduce that -- there is no per-account input left to get wrong -- and
    `_content_id_taken` closes the remaining door by refusing a number any row
    already holds, including a hand-entered `content.content_no`.
    """
    row = conn.execute("SELECT next_id FROM content_id_seq WHERE id = 1").fetchone()
    nxt = int(row["next_id"]) if row is not None else _content_id_seed(conn)
    nxt = max(nxt, CONTENT_ID_FLOOR)
    # Skip anything already issued. This normally spins zero times; it is what
    # keeps the counter correct after an operator hand-links a real SE id, and
    # after a restore that rolled the counter back behind the rows.
    while nxt <= CONTENT_ID_CEILING and _content_id_taken(conn, nxt):
        nxt += 1
    if nxt > CONTENT_ID_CEILING:
        raise RuntimeError(
            f"the Content ID pool is exhausted at {CONTENT_ID_CEILING} -- "
            f"widening it changes the DIGIT COUNT the client displays, so that "
            f"is a decision to take deliberately, not a bug to patch away")
    conn.execute(
        "INSERT INTO content_id_seq (id, next_id) VALUES (1, ?)"
        " ON CONFLICT(id) DO UPDATE SET next_id = excluded.next_id",
        (nxt + 1,))
    return str(nxt)


def link_content_to_handle(conn, handle_id, content_code, content_id=None,
                           status="active"):
    """Attach a Content ID to a handle. Idempotent on (handle_id, content_code).

    EXCLUSIVE across the member's handles: a Content ID belongs to exactly one
    handle in POL (POL-7169/7187/5326), so attaching it here detaches it from any
    other handle the same member holds. Without that, "move this title to my
    other handle" left it on both -- and because the provisional Content ID is a
    function of (member, content) and not of the handle, both copies carried the
    SAME id, which is precisely the collision the Content ID mint exists to
    avoid (see allocate_content_id). Moving is therefore just a link to the destination.
    """
    # WARNING: THIS USED TO BE A `DELETE`, AND A DELETE NOW DESTROYS CONTENT IDS.
    # With one row per (handle, game) a delete-then-insert was lossless: the one
    # id was re-supplied by the insert. A game can now hold SEVERAL slots (FFXI
    # mints one per character), so deleting the source handle's rows would drop
    # every id but the one passed in -- and a Content ID cannot be re-minted
    # without orphaning that character's local files (see allocate_content_id).
    # So the title MOVES: the rows keep their ids and change handle.
    #
    # Slots are renumbered after whatever the destination already holds, which
    # matters only when merging two handles that both hold the game; in the
    # ordinary move the destination holds nothing and the slots survive as they
    # were.
    row = conn.execute(
        "SELECT COALESCE(MAX(slot), -1) AS hi FROM handle_content"
        " WHERE handle_id = ? AND content_code = ?",
        (handle_id, content_code)).fetchone()
    nxt = int(row["hi"]) + 1
    moving = list(conn.execute(
        f"SELECT handle_id, slot FROM handle_content WHERE content_code = ?"
        f" AND handle_id IN ({_SIBLING_HANDLES}) ORDER BY handle_id, slot",
        (content_code, handle_id, handle_id)))
    for i, r in enumerate(moving):
        conn.execute(
            "UPDATE handle_content SET handle_id = ?, slot = ?"
            " WHERE handle_id = ? AND content_code = ? AND slot = ?",
            (handle_id, nxt + i, r["handle_id"], content_code, r["slot"]))
    conn.execute(
        "INSERT INTO handle_content (handle_id, content_code, slot, content_id,"
        " status, linked_at) VALUES (?,?,0,?,?,?)"
        " ON CONFLICT(handle_id, content_code, slot) DO UPDATE SET"
        " content_id = COALESCE(excluded.content_id, content_id),"
        " status = excluded.status",
        (handle_id, content_code, content_id, status, _now()))
    conn.commit()


def unlink_content_from_handle(conn, handle_id, content_code):
    conn.execute("DELETE FROM handle_content WHERE handle_id = ? AND content_code = ?",
                 (handle_id, content_code))
    conn.commit()


def member_content_id(conn, member_id, content_code, active_only=True):
    """The Content ID this MEMBER holds for one game, across all their handles.

    `handle_content`'s key is (handle, game), so everything else in this file is
    handle-scoped -- but the two consumers that need a Content ID have only a
    member id in hand: the Tetra Master ranking tally (`tmrank.cid_for`, whose
    row identity IS the TM Content ID) and the FFXI bridge. Both used to fall
    back to recomputing `1000000000 + member.id * 100 + code`, which stopped
    being right the moment the mint became an allocation, so they need to be
    able to ASK instead of derive. Returns the stored string, or None.
    """
    q = ("SELECT hc.content_id AS cid FROM handle_content hc"
         " JOIN handle h ON h.id = hc.handle_id"
         " WHERE h.member_id = ? AND hc.content_code = ?"
         "   AND hc.content_id IS NOT NULL")
    if active_only:
        q += " AND hc.status = 'active'"
    # `hc.slot ASC` is load-bearing: a game can hold several Content IDs now, and
    # every caller of this function wants the one that IS the account's identity
    # for that game (TM's ranking row, the bridge's member lookup) -- which is
    # slot 0. Without it SQLite may return any slot and the answer moves between
    # calls for no visible reason.
    row = conn.execute(q + " ORDER BY h.is_primary DESC, h.id ASC, hc.slot ASC"
                           " LIMIT 1",
                       (member_id, int(content_code))).fetchone()
    return row["cid"] if row else None


#: Content code 1 = FINAL FANTASY XI. Named because the slot rules below are
#: FFXI's, not a general policy: it is the only title we serve that issues one
#: Content ID per CHARACTER rather than one per account.
FFXI_CONTENT_CODE = 1

#: How many FFXI Content IDs a handle is given, i.e. how many FFXI characters it
#: can actually play. SE sells these; we grant them, because nothing on our side
#: sells anything and a player who makes a second character and cannot log into
#: it has no way to tell that from a bug (they get POL-0001, which says nothing).
#:
#: WARNING: EIGHT IS THE CLIENT'S CEILING FOR WHAT A HANDLE CAN *SHOW*, NOT FOR WHAT IT
#: CAN HOLD. The 1:3 record binds a character to a handle through a 3-bit
#: position into the 8-byte array at handle_slot+0x20 (SE string 26069, "You can
#: link up to eight Content IDs to a handle"), so only eight per handle appear in
#: the handle's Content ID list. The 64-slot character table the launch gate and
#: FFXI's world lookup actually read has no such limit -- see
#: `responders._char_record`, which serves the overflow UNBOUND: present and
#: playable, absent from the profile view. Content IDs can also be moved between
#: handles (`link_content_to_handle`) if a player wants them visible.
FFXI_CHARACTER_SLOTS = max(1, int(os.environ.get("POL_FFXI_CHARACTER_SLOTS", "4")))


def content_slot_count(conn, handle_id, content_code, active_only=True):
    """How many Content IDs this handle holds for one game."""
    q = ("SELECT COUNT(*) AS n FROM handle_content"
         " WHERE handle_id = ? AND content_code = ?")
    if active_only:
        q += " AND status = 'active'"
    return int(conn.execute(q, (handle_id, int(content_code))).fetchone()["n"])


def ensure_content_slots(conn, handle_id, content_code, want, status="active"):
    """Top this handle up to `want` Content IDs for one game. Returns how many
    were minted (0 if it already had enough).

    **ADDITIVE ONLY, AND THAT IS THE WHOLE SAFETY ARGUMENT.** It never touches an
    existing row, never renumbers a slot and never re-mints an id -- the FFXI
    client names a character's local files `FINAL FANTASY XI/USER/<hexid>/`, so
    moving an id that has already been served costs that player every macro they
    ever wrote (see `allocate_content_id`). Extra slots are new serials out of a
    ~70-million pool appended after the highest slot in use, so running this
    against an account twice, or against an account mid-migration, cannot damage
    anything that already works.

    WARNING: DOES NOT COMMIT -- same contract as `allocate_content_id`, whose counter
    bump has to land in the same transaction as the row that consumes it.

    It does NOT grant the game itself: `content` is the entitlement and this is
    the per-handle link. A handle with no row for this code at all gets none,
    because "how many FFXI characters may this handle have" is a different
    question from "does this account own FFXI", and answering the second one here
    would hand the title to everybody.
    """
    have = content_slot_count(conn, handle_id, content_code, active_only=False)
    if have == 0 or have >= want:
        return 0
    row = conn.execute(
        "SELECT COALESCE(MAX(slot), -1) AS hi FROM handle_content"
        " WHERE handle_id = ? AND content_code = ?",
        (handle_id, int(content_code))).fetchone()
    nxt = int(row["hi"]) + 1
    now = _now()
    for i in range(want - have):
        conn.execute(
            "INSERT INTO handle_content (handle_id, content_code, slot,"
            " content_id, status, linked_at) VALUES (?,?,?,?,?,?)",
            (handle_id, int(content_code), nxt + i, allocate_content_id(conn),
             status, now))
    return want - have


def ensure_ffxi_character_slots(conn, handle_id=None):
    """Top every handle that HAS FFXI up to `FFXI_CHARACTER_SLOTS` Content IDs.

    Returns the number of ids minted. Idempotent, and a no-op for a handle that
    does not hold FFXI at all.

    This is what closes the gap for accounts that already exist. It runs from
    `_migrate` after the slot rebuild, so an operator does not have to remember
    it, and it is safe to run repeatedly (`ensure_content_slots` is additive).
    """
    if handle_id is not None:
        hids = [int(handle_id)]
    else:
        hids = [int(r["handle_id"]) for r in conn.execute(
            "SELECT DISTINCT handle_id FROM handle_content WHERE content_code = ?",
            (FFXI_CONTENT_CODE,))]
    minted = 0
    for hid in hids:
        minted += ensure_content_slots(conn, hid, FFXI_CONTENT_CODE,
                                       FFXI_CHARACTER_SLOTS)
    if minted:
        conn.commit()
    return minted


def handle_content_list(conn, handle_id, active_only=True):
    """The Content IDs linked to a handle: [{content_code, content_id, status}]."""
    q = ("SELECT content_code, slot, content_id, status FROM handle_content"
         " WHERE handle_id = ?")
    if active_only:
        q += " AND status = 'active'"
    # (content_code, slot) and not content_code alone: a game can hold several
    # Content IDs now, and slot 0 is the one every existing caller means by "the"
    # id for that game -- so it has to come first within its game, deterministically.
    return [dict(r) for r in conn.execute(q + " ORDER BY content_code, slot",
                                          (handle_id,))]


def link_member_content_to_primary(conn, member_id):
    """Place every UNPLACED active content this member owns on their PRIMARY
    handle, minting a provisional Content ID for each. This is how an account
    gets its titles playable on its badge handle. Returns the number placed.

    IT DOES NOT MOVE WHAT THE PLAYER ALREADY PLACED. Redeeming a second code
    calls this again (kinou 31), and linking is exclusive -- so without the skip
    below, adding an FMO code would have dragged a Tetra Master that the player
    had deliberately moved to another handle back onto the primary one. Adding
    content must never rearrange content.
    """
    h = conn.execute(
        "SELECT id FROM handle WHERE member_id = ? AND is_primary = 1", (member_id,)
    ).fetchone()
    if h is None:
        h = conn.execute("SELECT id FROM handle WHERE member_id = ? ORDER BY created_at"
                         " LIMIT 1", (member_id,)).fetchone()
    if h is None:
        return 0
    n = 0
    for row in conn.execute("SELECT content_code, content_no FROM content"
                            " WHERE member_id = ? AND status = 'active'", (member_id,)):
        # Already on one of this member's handles (any status -- a cancelled
        # licence keeps its placement so a re-register restores it)? Leave it.
        if conn.execute(
                f"SELECT 1 FROM handle_content WHERE content_code = ?"
                f" AND handle_id IN ({_MEMBER_HANDLES})",
                (row["content_code"], member_id)).fetchone():
            continue
        # An operator-entered `content_no` (a real SE id) wins; otherwise the
        # counter hands out the next serial. There is no per-member arithmetic
        # left in either branch -- see allocate_content_id for why the value can
        # no longer be computed from the account, and for the member_no/member.id
        # collision that history is the reason this comment used to be here.
        cid = row["content_no"] or allocate_content_id(conn)
        link_content_to_handle(conn, h["id"], row["content_code"], cid)
        # Same reason as at registration: FFXI is per CHARACTER, so placing the
        # title has to place the whole set of character slots with it. This path
        # is how a redeemed regcode (kinou 31) grants a title, so an account that
        # gets FFXI from a code and one that gets it at sign-up end up identical.
        # `link_content_to_handle` has already committed, so this needs its own.
        if int(row["content_code"]) == FFXI_CONTENT_CODE:
            if ensure_content_slots(conn, h["id"], FFXI_CONTENT_CODE,
                                    FFXI_CHARACTER_SLOTS):
                conn.commit()
        n += 1
    return n


#: SE's step 3/7 shows the code as five dash-separated groups.
REGCODE_GROUPS = 5


def normalise_regcode(raw):
    """Join five entered groups (or one pasted string) into canonical form.

    Case is PRESERVED HERE, but it is NOT compared -- every lookup matches
    `COLLATE NOCASE`. Those are different things and the distinction is the
    point: a code is stored and displayed exactly as it was issued (the audit
    trail, and what an operator reads back), while a player who types it in
    lower case still redeems it.

    WARNING: It used to be compared exactly, on the reasoning that SE's own screen
    says "the code is case-sensitive". That screen is SE's, the codes are OURS,
    and the rule cost a real player their sign-up: prod's ucs.log, 2026-08-29,
    shows `rc0=7cj3&rc1=hbyy&...` refused at 15:28:49 and the same code retyped
    as `7CJ3-HBYY-...` at 15:29:24 -- 35 seconds of a person wondering what
    they had got wrong, on the one screen where a refusal reads as "your code
    is bad". Every code we have ever issued is upper case (checked live, 10 of
    10) and none of them collide when folded, so exact comparison was buying
    nothing at all. `issue_regcode` refuses a code that differs from an
    existing one only by case, so the folded lookup cannot become ambiguous.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = [p.strip() for p in str(raw).replace(" ", "").split("-")]
    parts = [p for p in parts if p]
    return "-".join(parts)


def issue_regcode(conn, code, contents=(1,), note=None):
    """Add a registration code. `contents` are the content codes it grants.

    Refuses a code that differs from an existing one ONLY by case. Redemption
    folds case (see `normalise_regcode`), so two such codes would be one code to
    every player and two to the operator -- whichever row the lookup happened to
    return would be the one that got spent. Cheaper to refuse than to explain.
    """
    canon = normalise_regcode(code)
    clash = conn.execute(
        "SELECT code FROM regcode WHERE code = ? COLLATE NOCASE AND code <> ?",
        (canon, canon)).fetchone()
    if clash is not None:
        raise RegistrationError(
            f"{canon!r} differs only by case from the existing code "
            f"{clash['code']!r}, and codes are redeemed case-insensitively.")
    conn.execute(
        "INSERT OR REPLACE INTO regcode (code, contents, note, created_at)"
        " VALUES (?,?,?,?)",
        (canon, ",".join(str(c) for c in contents), note, _now()))
    conn.commit()


def check_regcode(conn, code):
    """Return the regcode row if it exists and is unredeemed, else None.

    Redemption is `redeemed_at` OR `redeemed_by`, not `redeemed_by` alone.
    Deleting an account has to clear `redeemed_by` -- it is a foreign key into
    `polid`, so the row cannot outlive the account it points at -- and on the
    `redeemed_by` test alone that silently handed a consumed code back to the
    next person who typed it. `redeemed_at` survives the account and keeps the
    code spent; see `delete_polid`, whose `release_codes` clears BOTH when
    returning a code to the pool is what the operator actually meant.
    """
    row = conn.execute("SELECT * FROM regcode WHERE code = ? COLLATE NOCASE",
                       (normalise_regcode(code),)).fetchone()
    if row is None or row["redeemed_by"] is not None \
            or row["redeemed_at"] is not None:
        return None
    return row


def redeem_regcode(conn, code, polid):
    """Mark a code used by `polid`. Returns the content codes it granted."""
    row = check_regcode(conn, code)
    if row is None:
        return None
    conn.execute(
        "UPDATE regcode SET redeemed_at = ?, redeemed_by = ? WHERE code = ?",
        (_now(), polid, row["code"]))
    conn.commit()
    return [int(c) for c in row["contents"].split(",") if c.strip()]


def set_member_password(conn, login_name, password):
    """By EXACT login name, unchecked -- the in-client change screen (ucscgi
    kinou 17) has already applied the policy and knows the login name from its
    own session. `set_account_password` below is the one to reach for anywhere
    the identifier came from a human.

    Updates the parent `polid` row as well, so the two hashes register_account
    writes as a pair cannot drift apart depending on which screen changed it.
    """
    pw_hash, pw_salt = hash_password(password)
    with conn:
        cur = conn.execute(
            "UPDATE member SET pw_hash = ?, pw_salt = ? WHERE login_name = ?",
            (pw_hash, pw_salt, login_name))
        conn.execute(
            "UPDATE polid SET pw_hash = ?, pw_salt = ?, updated_at = ?"
            " WHERE polid = (SELECT polid FROM member WHERE login_name = ?)",
            (pw_hash, pw_salt, _now(), login_name))
    return cur.rowcount


def set_account_password(conn, ident, password):
    """Change an account's password, given whatever the operator has to hand.

    `ident` is resolved the way `verify_member` resolves its login field --
    login name, then POL ID, then bound login NICK -- because those three
    diverge on a real account (member 1 holds POL ID `EFGH5678` and logs in as
    `UH5GRSV86`) and the panel lists the POL ID, which is the one thing
    `set_member_password` does NOT accept. Handles stay out for the same reason
    they do there: a handle is public.

    Writes BOTH `member.pw_hash` and the parent `polid.pw_hash`, in one
    transaction. Only the member row is ever verified against (`verify_member`),
    but `register_account` sets the pair to the same value and `reissue_polid`
    carries the polid row forward, so leaving one behind would seed exactly the
    two-values-for-one-setting divergence that keeps costing this project days.

    Returns the member row on success, None if `ident` matched nothing.
    Raises RegistrationError if the password fails the client-facing policy --
    the panel must not be able to set a password the Viewer's own entry field
    (8-15 printable ASCII) cannot type back.
    """
    bad = check_password_policy(password)
    if bad:
        raise RegistrationError(bad)
    row = (get_member(conn, ident)
           or member_by_polid(conn, ident)
           or member_by_alias(conn, ident))
    if row is None:
        return None
    pw_hash, pw_salt = hash_password(password)
    with conn:
        conn.execute("UPDATE member SET pw_hash = ?, pw_salt = ? WHERE id = ?",
                     (pw_hash, pw_salt, row["id"]))
        conn.execute("UPDATE polid SET pw_hash = ?, pw_salt = ?, updated_at = ?"
                     " WHERE polid = ?",
                     (pw_hash, pw_salt, _now(), row["polid"]))
    return get_member(conn, row["login_name"])


def clear_login_token(conn, member_id):
    """Forget the recorded NICK token so the NEXT login re-seeds it (re-TOFU).

    THIS -- not the password above -- is the credential the lobby login checks.
    `responders.resolve_account` compares the NICK line's 11-char token against
    `member.login_token` and, with POL_ACCOUNTS_ENFORCE(_PW) on, refuses a
    mismatch with SE reject 0xCA (measured 2026-08-17, [[no-password-check]]).
    The stored password hash is only read by the ucs-cgi account servlet.

    So the two are separate keys and this is the lockout escape hatch: an
    account whose recorded token no longer matches what its client presents
    cannot get in until the token is cleared, whatever its password is set to.
    It is deliberately a SEPARATE action from a password change -- clearing it
    means the next thing to connect as this account is trusted on sight.
    """
    cur = conn.execute("UPDATE member SET login_token = NULL WHERE id = ?",
                       (member_id,))
    # Clear the per-client rows too. Leaving them would make this hatch a no-op
    # for the client that is actually locked out -- the scoped check below runs
    # FIRST and would still be holding the stale token.
    conn.execute("DELETE FROM login_token_client WHERE member_id = ?",
                 (member_id,))
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def get_member(conn, login_name):
    return conn.execute("SELECT * FROM member WHERE login_name = ?",
                        (login_name,)).fetchone()


def get_login_token(conn, member_id):
    """The stored NICK-line password token for a member, or None if never set."""
    row = conn.execute("SELECT login_token FROM member WHERE id = ?",
                       (member_id,)).fetchone()
    return row["login_token"] if row else None


def set_login_token(conn, member_id, token):
    """Record the password token (trust-on-first-use). Idempotent per value."""
    conn.execute("UPDATE member SET login_token = ? WHERE id = ?",
                 (token, member_id))
    conn.commit()


# --------------------------------------------------------------------------- #
# PER-CLIENT login tokens -- see the `login_token_client` table comment.
#
# `member.login_token` above is KEPT: it is what the admin panel shows, what
# `clear_login_token` re-TOFUs, and the value the account was first seeded with.
# These helpers add the second axis (which client build presented it) that the
# single slot could not express.
# --------------------------------------------------------------------------- #
def get_client_token(conn, member_id, client_sig):
    """The token this member has already proven from THIS client, or None."""
    row = conn.execute(
        "SELECT token FROM login_token_client"
        " WHERE member_id = ? AND client_sig = ?",
        (member_id, client_sig)).fetchone()
    return row["token"] if row else None


def set_client_token(conn, member_id, client_sig, token):
    """Record (member, client) -> token, trust-on-first-use. Refreshes
    `last_seen` when the same pair logs in again, so the panel can show which
    clients an account is actually used from and when."""
    now = _now()
    conn.execute(
        "INSERT INTO login_token_client"
        " (member_id, client_sig, token, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(member_id, client_sig) DO UPDATE SET"
        "   token = excluded.token, last_seen = excluded.last_seen",
        (member_id, client_sig, token, now, now))
    conn.commit()


def touch_client_token(conn, member_id, client_sig):
    """Stamp `last_seen` for a pair that just matched, without rewriting it."""
    conn.execute(
        "UPDATE login_token_client SET last_seen = ?"
        " WHERE member_id = ? AND client_sig = ?",
        (_now(), member_id, client_sig))
    conn.commit()


def list_client_tokens(conn, member_id):
    """Every (client_sig, token, first_seen, last_seen) row for a member,
    most recently used first. For the admin panel and the CLI."""
    return conn.execute(
        "SELECT client_sig, token, first_seen, last_seen"
        " FROM login_token_client WHERE member_id = ?"
        " ORDER BY last_seen DESC", (member_id,)).fetchall()


def clear_client_tokens(conn, member_id, client_sig=None):
    """Forget one client's recorded token, or all of them. The per-client half
    of `clear_login_token`'s lockout escape hatch."""
    if client_sig is None:
        cur = conn.execute("DELETE FROM login_token_client WHERE member_id = ?",
                           (member_id,))
    else:
        cur = conn.execute(
            "DELETE FROM login_token_client"
            " WHERE member_id = ? AND client_sig = ?", (member_id, client_sig))
    conn.commit()
    return cur.rowcount


def member_by_handle(conn, handle_name):
    """Resolve a member from a handle name -- the auth path's natural key,
    because what arrives on the wire is the NICK, not the login name."""
    return conn.execute(
        "SELECT m.* FROM member m JOIN handle h ON h.member_id = m.id"
        " WHERE h.handle_name = ?", (handle_name,)).fetchone()


def member_by_alias(conn, nick):
    """Resolve a member from an extra login NICK (see the `login_alias` table).

    This is the third and last lookup the auth path tries, after the handle and
    the login name, so an alias can never shadow a real account."""
    return conn.execute(
        "SELECT m.* FROM member m JOIN login_alias a ON a.member_id = m.id"
        " WHERE a.nick = ?", (nick,)).fetchone()


def set_login_alias(conn, nick, member_id, note=None):
    """Point a login NICK at an existing member. Idempotent."""
    conn.execute(
        "INSERT INTO login_alias (nick, member_id, note, created_at)"
        " VALUES (?,?,?,?) ON CONFLICT(nick) DO UPDATE SET"
        " member_id = excluded.member_id, note = excluded.note",
        (nick, member_id, note, _now()))
    conn.commit()


def bind_login_nick(conn, member_id, polid, note=None):
    """Record the NICK the client will send for `polid`, so the login resolves.

    THIS IS WHAT MAKES A NEW ACCOUNT USABLE. The client does not send the ID the
    user types at Add Member -- it scrambles it (`IJKL9012` goes out as
    `UZ3714LIO`) and the nick is the only identity the auth path ever
    sees. Without this row a freshly registered account is a nick we have never
    met: rejected under POL_ACCOUNTS_ENFORCE=1, or auto-provisioned as a second
    empty account with the registration stranded on the first.

    Stored rather than computed at login time on purpose. It costs one row, it
    reuses the `login_alias` lookup the auth path already performs, and -- unlike
    a decode buried in `resolve_account` -- an operator can SEE why a nick maps
    where it does. `reissue_polid` keeps it in step; `sync_login_nicks` backfills
    accounts that predate it. Returns the nick, or None if `polid` is not an
    8-character ID (a hand-made or auto-provisioned account, which needs no
    binding because its polid IS its nick).
    """
    if polnick is None:
        return None
    try:
        nick = polnick.nick_for_polid(polid)
    except ValueError:
        return None
    set_login_alias(conn, nick, member_id, note or f"scrambled form of {polid}")
    return nick


def sync_login_nicks(conn):
    """Backfill `bind_login_nick` for every member. Returns [(polid, nick)].

    Idempotent, and safe to run on a live database: it only ever adds or
    refreshes the derived alias for an ID that has one.
    """
    out = []
    for row in conn.execute("SELECT id, polid FROM member ORDER BY id"):
        nick = bind_login_nick(conn, row["id"], row["polid"])
        if nick:
            out.append((row["polid"], nick))
    return out


def member_by_polid(conn, polid):
    """Resolve a POL ID to its PRIMARY member (lowest `member_no`).

    A POL ID is the account; members are the sub-accounts under it ("Add Member"
    in the client), so this is a one-to-many edge and the ID alone cannot name a
    specific member. Lowest member_no is the one created with the POL ID itself,
    which is what someone typing the bare ID means.
    """
    return conn.execute(
        "SELECT * FROM member WHERE polid = ? ORDER BY member_no LIMIT 1",
        (polid,)).fetchone()


def verify_member(conn, login_name, password):
    """Return the member row on success, else None. Also fails closed for a
    non-active member or a non-active parent POL ID.

    `login_name` also accepts the POL ID and a bound login NICK, because the one
    caller that asks a HUMAN for it -- the ucs-cgi account servlet, whose field
    is labelled "PlayOnline ID" -- was matching the label against the wrong
    column. For an account whose ID and login name diverge (member 1 holds POL ID
    `EFGH5678` with login name `UH5GRSV86`, the scrambled NICK the client sent at
    first login -- see [[pol-login-nick-scrambler]]) that meant the field could
    only be satisfied by a value the user has never been shown.

    Order is login_name, then POL ID, then NICK alias. Exact login names win, so
    nothing can shadow a real account -- the same rule the lobby auth path
    follows. Handles are deliberately NOT accepted: a handle is a public social
    identity that other players can read off a friend list, and taking one here
    would turn it into a name to try passwords against.
    """
    row = (get_member(conn, login_name)
           or member_by_polid(conn, login_name)
           or member_by_alias(conn, login_name))
    if row is None or not check_password(password, row["pw_hash"], row["pw_salt"]):
        return None
    if row["status"] != "active":
        return None
    parent = conn.execute("SELECT status FROM polid WHERE polid = ?",
                          (row["polid"],)).fetchone()
    if parent is None or parent["status"] != "active":
        return None
    return row


def content_ids(conn, member_id, status="active"):
    """Content codes for a member, ordered -- feed straight into
    contentlist.build_block() to drive the games menu from the account.

    Defaults to the active ones, which is what every existing caller wants.
    `status='inactive'` lists cancelled licences, which is what Service &
    Support's reactivation screen (kinou 15) offers back to the player.
    """
    return [r["content_code"] for r in conn.execute(
        "SELECT content_code FROM content WHERE member_id = ?"
        " AND status = ? ORDER BY content_code", (member_id, status))]


# --------------------------------------------------------------------------- #
# Friend list
#
# The list belongs to a HANDLE, not to a member: handles are the social identity
# in PlayOnline (a member may hold up to four), and SE's own capture shows the
# owner's handle sitting in the same list as its friends.
#
# Entry shape on the wire is 32 bytes -- u64 guid, u32 handle id, u32 kind flags,
# 16-byte name -- captured from a real SE session 2026-08-11. `kind` here is that
# flags field, so the DB stores exactly what goes out:
#
#     KIND_FRIEND 0x0800   another person
#     KIND_SELF   0x1400   the owning handle itself
#     KIND_GROUP  0x0001   a group
#
# `peer_handle` is set when the friend is a local account (so a rename follows
# automatically) and NULL otherwise; `peer_name` always carries the display name
# so a non-local or not-yet-registered friend still renders.
# --------------------------------------------------------------------------- #
KIND_FRIEND = 0x0800
KIND_SELF = 0x1400
KIND_GROUP = 0x0001


def _peer_guid(peer_handle, peer_name):
    """The on-wire guid for a named peer, local or not.

    A LOCAL peer gets that handle's real guid, not a synthetic one. The client
    echoes whatever guid it was given back as the profile record's `z_hid` when
    it opens that peer's profile, so a synthetic value means the lookup finds
    nobody and the server falls back to serving YOUR profile under their name --
    which is exactly what it used to do.

    A NON-LOCAL peer still needs an id, and it must be stable and inside the 44
    bits the client keeps. Two things were wrong with the old
    `0x5400000000000000 | (abs(hash(name)) & 0xFFFFFF)`: `hash()` on a str is
    seed-randomised per PROCESS, so "stable" held only until a restart; and bit
    62 is discarded by the client's unpack (guid = (word >> 1) & 0xFFF_FFFFFFFF),
    leaving just the 24 bits of the mask to tell two names apart. A digest gives
    a value that is the same in every process and every run, and the reserved top
    bit keeps it clear of `handle_guid`'s own space.

    Shared by `add_friend` and `add_group_member` so a person cannot end up with
    one id in the friend list and a different one in a group's member list --
    polcore matches a group member against the client's OWN id by guid
    (0x37e8917), so a drifting value silently changes which group flags get set.
    """
    if peer_handle:
        return handle_guid(peer_handle)
    digest = hashlib.sha1(peer_name.encode("utf-8")).digest()
    return (1 << 42) | (int.from_bytes(digest[:6], "big") & ((1 << 40) - 1))


def add_friend(conn, handle_id, peer_name, kind=KIND_FRIEND, peer_handle=None,
               guid=None, comment=None, status="active"):
    """Add (or update) a friend entry. Returns its row id."""
    if guid is None:
        guid = _peer_guid(peer_handle, peer_name)
    conn.execute(
        "INSERT INTO friend (handle_id, peer_handle, peer_name, peer_guid, kind,"
        " status, comment, created_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(handle_id, peer_name) DO UPDATE SET"
        " kind=excluded.kind, status=excluded.status, comment=excluded.comment,"
        " peer_handle=excluded.peer_handle",
        (handle_id, peer_handle, peer_name, guid, int(kind), status, comment,
         _now()))
    conn.commit()
    row = conn.execute("SELECT id FROM friend WHERE handle_id = ? AND peer_name = ?",
                       (handle_id, peer_name)).fetchone()
    return row["id"] if row else None


def list_friends(conn, handle_id, status="active"):
    """Friend rows for a handle, groups last (SE's capture had them trailing)."""
    q = ("SELECT f.*, h.handle_name AS live_name FROM friend f"
         " LEFT JOIN handle h ON h.id = f.peer_handle"
         " WHERE f.handle_id = ?")
    args = [handle_id]
    if status:
        q += " AND f.status = ?"
        args.append(status)
    q += " ORDER BY (f.kind = %d), f.id" % KIND_GROUP
    return list(conn.execute(q, args))


def set_friend_label(conn, handle_id, row_id, label):
    """Give one friend row the caption this account renamed them to.

    `label` of None or "" CLEARS it, which is how a rename-back arrives: the
    retail capture's very next 2:6 after "Cool friend :3" put "Cyn" -- the
    handle's real name -- straight back into the same slot, and the caller turns
    that into a clear rather than into a caption that happens to match.

    Keyed on the ROW ID, not the name, because a rename is precisely the write
    in which the name field no longer holds the name.
    """
    label = (label or "").strip() or None
    conn.execute("UPDATE friend SET label = ? WHERE id = ? AND handle_id = ?",
                 (label, int(row_id), int(handle_id)))
    conn.commit()
    return label


def set_friend_flags(conn, handle_id, row_id, low=None, flag=None):
    """Store the two ignore-state bytes off a 2:6 record. `None` leaves one be.

    Deliberately NOT interpreted -- see the `friend` migration notes. Four
    samples pin the STRUCTURE (ignore is state within the whole-list PUT, not an
    opcode) and not the meaning of every bit, so the server's job is to hand
    back what it was given.
    """
    sets, args = [], []
    if low is not None:
        sets.append("ignore_low = ?")
        args.append(int(low) & 0xFF)
    if flag is not None:
        sets.append("ignore_flag = ?")
        args.append(int(flag) & 0xFF)
    if not sets:
        return False
    args += [int(row_id), int(handle_id)]
    conn.execute("UPDATE friend SET " + ", ".join(sets) +
                 " WHERE id = ? AND handle_id = ?", args)
    conn.commit()
    return True


def friend_row_by_guid(conn, handle_id, guid):
    """The friend row this handle holds for `guid`, or None.

    **THE STABLE KEY FOR A 2:6 RECORD IS ITS GUID**, measured 2026-08-19: the
    record's +0x0C u64 is the peer's client guid and it is the one field that
    does not move across an add, a rename, an ignore and an un-ignore (memory
    `friend-rename-and-ignore-encoding`). The name field is the thing a rename
    overwrites and the thing a delete fills with heap, and the slot is an index
    into a list we may not have served this process -- so both are fallbacks and
    this is the primary.

    Matched against the guid we store for the row, the one derived live from
    the peer's handle, AND the peer's client_guid, because `_db_friends` serves
    the derived value -- or, under POL_FRIEND_GUID_CLIENT (the Tetra Master
    room-recognition fix, 2026-08-22), the peer's own client guid -- and the
    client echoes back whatever it was served.  Without the client_guid arm a
    2:6 DELETE echo (whose name field is heap) would resolve to nobody and
    silently stop being a delete.
    """
    if not guid:
        return None
    guid = int(guid) & 0xFFFFFFFFFFFFFFFF
    for r in conn.execute(
            "SELECT f.*, h.client_guid AS peer_client_guid FROM friend f"
            " LEFT JOIN handle h ON h.id = f.peer_handle"
            " WHERE f.handle_id = ?", (int(handle_id),)):
        if int(r["peer_guid"] or 0) == guid:
            return r
        if r["peer_handle"] and handle_guid(int(r["peer_handle"])) == guid:
            return r
        if int(r["peer_client_guid"] or 0) == guid:
            return r
    return None


def _drop_friend_mirror(conn, handle_id, peer_name, kind):
    """Take MY row off THEIR list too. Symmetric deletion -- OFF by default.

    **DEFAULT ONE-SIDED since 2026-08-19, matching SE.** A brief window (added
    2026-08-16) made deletion symmetric to stop stale-row bugs, but those bugs
    were since fixed at their real sources (the search-add guid, the group
    identity, the stale-acceptance guard), so the workaround's reason is gone.
    And SE's one-sided behaviour is now MEASURED ON THE WIRE, from both sides:
    on retail, one account deleted another from its friend list, the deleter's
    client sent one 2:6 removing the peer, and SE pushed the peer's client
    NOTHING -- no list change, no NOTICE. The peer's row for the deleter simply
    persisted (offline, presence no longer flowing). Fidelity to that capture
    is the chosen default: a friendship on SE is two INDEPENDENT one-sided
    declarations, and deleting yours does not erase theirs.

    `POL_FRIEND_DELETE_SYMMETRIC=1` restores the symmetric divergence (delete
    removes both sides at once -- cleaner UX, but not what retail does).

    NOT for declining: `decline_friend` passes `mirror=False`, because refusing
    someone's request must leave THEIR outgoing row alone -- SE has a distinct
    "declined" message type and the asker is meant to be told, not silently
    un-asked. See that function.

    Only meaningful for a LOCAL peer and only for people: a group is not a peer
    with a list of its own. Returns the peer handle id whose row went, or None.
    """
    if int(kind) != KIND_FRIEND:
        return None
    if os.environ.get("POL_FRIEND_DELETE_SYMMETRIC", "0") != "1":
        return None
    me = conn.execute("SELECT handle_name FROM handle WHERE id = ?",
                      (handle_id,)).fetchone()
    peer = _peer_handle_row(conn, peer_name)
    if me is None or peer is None or int(peer["id"]) == int(handle_id):
        return None
    cur = conn.execute(
        "DELETE FROM friend WHERE handle_id = ? AND peer_name = ? AND kind = ?",
        (int(peer["id"]), me["handle_name"], KIND_FRIEND))
    conn.commit()
    return int(peer["id"]) if cur.rowcount else None


def remove_friend(conn, handle_id, peer_name, mirror=True):
    row = conn.execute(
        "SELECT id, kind FROM friend WHERE handle_id = ? AND peer_name = ?",
        (handle_id, peer_name)).fetchone()
    conn.execute("DELETE FROM friend WHERE handle_id = ? AND peer_name = ?",
                 (handle_id, peer_name))
    conn.commit()
    if row is not None and mirror:
        _drop_friend_mirror(conn, handle_id, peer_name, row["kind"])


def remove_friend_by_ref(conn, handle_id, client_ref):
    """Delete the row carrying exactly this client id. See below for the caller
    that should normally be used instead."""
    row = conn.execute("SELECT id, peer_name, kind FROM friend WHERE handle_id"
                       " = ? AND client_ref = ?",
                       (handle_id, client_ref)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM friend WHERE id = ?", (row["id"],))
    conn.commit()
    _drop_friend_mirror(conn, handle_id, row["peer_name"], row["kind"])
    return row["peer_name"]


def remove_friend_by_peer_handle(conn, handle_id, peer_handle):
    """Delete this handle's row for that PEER HANDLE, and say whose it was.

    The friend's handle id is what our 2:3 head word carries and what a delete
    echoes back (responders._friend_row_handles), so this is the only identifier
    that exists for every row rather than only the ones a client write has named.
    Scoped to the owning handle, so an echo from the wrong session cannot reach
    somebody else's list.
    """
    row = conn.execute("SELECT id, peer_name, kind FROM friend WHERE handle_id"
                       " = ? AND peer_handle = ?",
                       (handle_id, int(peer_handle))).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM friend WHERE id = ?", (row["id"],))
    conn.commit()
    _drop_friend_mirror(conn, handle_id, row["peer_name"], row["kind"])
    return row["peer_name"]


def remove_friend_by_row(conn, handle_id, row_id):
    """Delete this handle's friend row by its own database id.

    That id is what the 2:3 record's tag carries (responders._friend_row_tag), so
    it is the only handle on a row that exists for EVERY friend rather than only
    the ones a client write has named. Scoped to the handle so a tag echoed by
    the wrong session cannot delete somebody else's row.
    """
    row = conn.execute("SELECT id, peer_name, kind FROM friend WHERE id = ?"
                       " AND handle_id = ?", (int(row_id), handle_id)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM friend WHERE id = ?", (row["id"],))
    conn.commit()
    _drop_friend_mirror(conn, handle_id, row["peer_name"], row["kind"])
    return row["peer_name"]


def remove_friend_in_record(conn, handle_id, record, wire_ref=None):
    """Delete the row whose stored client id APPEARS ANYWHERE in this record.

    2:6 sends one record per change and a DELETE's record has no name in it --
    the name field holds whatever was in that buffer last, SE's own capture
    included. The client's 12-byte id for the row does survive, but it sits
    BEHIND the name, so its offset moves with a name length this record no
    longer tells us. Searching the record for an id we already hold sidesteps
    that entirely, and a 12-byte high-entropy value cannot collide by accident.

    `wire_ref` is the second id -- the 4 bytes in front of the grid, which a
    delete carries at a FIXED offset while the 12-byte one moves. Either match
    is enough, so a row learned by one route is deletable even if the other was
    never seen.

    Returns the peer name that went, or None if the record names nobody we know.
    """
    for row in conn.execute("SELECT id, peer_name, kind, client_ref, wire_ref"
                            " FROM friend WHERE handle_id = ? AND (client_ref IS"
                            " NOT NULL OR wire_ref IS NOT NULL)",
                            (handle_id,)).fetchall():
        ref = row["client_ref"]
        hit = bool(ref) and len(ref) >= 8 and bytes(ref) in bytes(record)
        if not hit and wire_ref and row["wire_ref"]:
            hit = bytes(row["wire_ref"]) == bytes(wire_ref)
        if hit:
            conn.execute("DELETE FROM friend WHERE id = ?", (row["id"],))
            conn.commit()
            _drop_friend_mirror(conn, handle_id, row["peer_name"], row["kind"])
            return row["peer_name"]
    return None


# --------------------------------------------------------------------------- #
# Group membership.
#
# Until this existed the 07:12 group list served a member COUNT it had invented
# (the owning handle, repeated `members=` times) because nothing stored who was
# actually in a group. Groups therefore rendered named but empty, and no
# group-scoped feature could be tested: polcore only sets a group's valid bit
# when at least one member is ACCEPTED (0x37e89c6), and the member records carry
# the guid it matches against the client's own id.
#
# The two ceilings below are the client's and are validated by it -- see the
# `group_member` table comment in SCHEMA.
# --------------------------------------------------------------------------- #

#: Member slots per group (obj+0x30, stride 0xC0). The 07:12 count block's
#: bytes 1..4 must each be <= this or the client draws POL-5133.
GROUP_MEMBER_MAX = 0x40

#: Group slots (polcore 0x3bb06c0, stride 0x3098, `cmp ecx,4`). Count block
#: byte 0 must be <= this.
GROUP_MAX = 4

#: The 3-bit class packed at bit 50 of a member record. polcore accepts 2..5 and
#: rejects everything else.
#:
#: *** WHICH VALUE IS WHICH -- the full ladder, settled 2026-08-22. ***
#: From both SE captures plus the roster-push stepping and the live class-2
#: regression ("group stuck at Inviting into group"):
#:
#:     class 5   master (the group's creator, in every captured group)
#:     class 4   sub-master (a member promoted by the master)
#:     class 3   accepted plain member
#:     class 2   INVITED, not yet accepted -- the "Inviting into group" state
#:     class 1   not a class: "leave the group" on a 7:3 (route to DELETE)
#:
#: WARNING: The 2026-08-16 reading "2 = ordinary member" was WRONG, and shipping it
#: broke every invitee. SE's class-2 rows in the capture were the owner's
#: PENDING invitees -- one of them is the same capture's documented pending
#: FRIEND -- and the capture's role handoffs step one member 2 -> 3 -> 4:
#: invited, accepted, sub-master. The old note was right all along: polcore
#: copies the class of the viewer's OWN row into the group's flags (0x37e8917,
#: armed since the f46e2295 self-match fix) and 0x37e7d10 reports the group
#: UNUSABLE exactly when that class is 2 -- which the client draws as
#: "Inviting into group" (SE's manual pm19: list visible, functions dead).
#: Class 2 on ANOTHER member's row is harmless -- the copy never fires -- which
#: is how the owner's-view capture misled. An accepted member's own row must
#: NEVER be served 2.
GROUP_CLASS_MASTER = 5
GROUP_CLASS_SUBMASTER = 4
GROUP_CLASS_MEMBER = 3
GROUP_CLASS_INVITED = 2
GROUP_CLASS_MIN = 2
GROUP_CLASS_MAX = 5


def group_id(conn, handle_id, name):
    """The `friend` row id of the group `name` owned by `handle_id`, or None."""
    row = conn.execute(
        "SELECT id FROM friend WHERE handle_id = ? AND peer_name = ? AND kind = ?",
        (int(handle_id), name, KIND_GROUP)).fetchone()
    return int(row["id"]) if row else None


def add_group_member(conn, gid, member_name, member_handle=None, guid=None,
                     cls=GROUP_CLASS_MEMBER, pending=0):
    """Add (or update) one member of group `gid`. Returns True if stored.

    Returns False -- rather than raising -- when the group is already full, so a
    live request path can log it and carry on. Overfilling is not a harmless
    overflow: the count block byte would exceed 0x40 and the client rejects the
    whole reply with POL-5133, losing every group rather than one member.

    `pending=1` records an INVITED-not-accepted member. The upsert takes
    `min(existing, new)` for the flag, so an acceptance (0) clears it and a
    repeated invite (1) can never re-pend somebody who already accepted --
    messages DO repeat (see `_mail_is_own_echo`), and a member silently
    demoted back to pending would vanish from every non-owner's roster.
    """
    if cls < GROUP_CLASS_MIN or cls > GROUP_CLASS_MAX:
        raise ValueError(f"group member class {cls} is outside polcore's 2..5")
    if guid is None:
        guid = _peer_guid(member_handle, member_name)
    present = conn.execute(
        "SELECT 1 FROM group_member WHERE group_id = ? AND member_name = ?",
        (int(gid), member_name)).fetchone()
    if present is None and count_group_members(conn, gid) >= GROUP_MEMBER_MAX:
        return False
    conn.execute(
        "INSERT INTO group_member (group_id, member_handle, member_name,"
        " member_guid, class, pending, created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(group_id, member_name) DO UPDATE SET"
        " member_handle=excluded.member_handle, member_guid=excluded.member_guid,"
        " class=excluded.class,"
        " pending=min(group_member.pending, excluded.pending)",
        (int(gid), member_handle, member_name, int(guid), int(cls),
         1 if pending else 0, _now()))
    conn.commit()
    return True


def confirm_group_member(conn, gid, member_name):
    """Mark an invited member accepted. Returns True if a pending row cleared.

    The class is lifted to plain member alongside the flag: an accepted member
    whose row still reads GROUP_CLASS_INVITED (2) renders as "Inviting into
    group" forever on their own screen (see the class ladder). A class the
    owner already raised (4/5) is kept -- accepting is "be in this group",
    never a demotion.
    """
    cur = conn.execute(
        "UPDATE group_member SET pending = 0, class = MAX(class, ?) "
        "WHERE group_id = ? AND member_name = ? AND pending = 1",
        (GROUP_CLASS_MEMBER, int(gid), member_name))
    conn.commit()
    return cur.rowcount > 0


def remove_group_member(conn, gid, member_name):
    conn.execute("DELETE FROM group_member WHERE group_id = ? AND member_name = ?",
                 (int(gid), member_name))
    conn.commit()


def delete_group(conn, gid, owner_handle_id=None):
    """Delete a group -- the owner's KIND_GROUP `friend` row -- and its whole
    membership. Returns the number of member rows removed, or None when there
    is no such group (or, with `owner_handle_id`, when that handle does not
    own it). The lobby's 7:2 KDeleteGroup arm is the caller.

    The membership is deleted explicitly rather than left to the FK cascade:
    the cascade needs `PRAGMA foreign_keys = ON` on THIS connection and the
    count is the useful thing to log either way.
    """
    q = "SELECT id FROM friend WHERE id = ? AND kind = ?"
    args = [int(gid), KIND_GROUP]
    if owner_handle_id is not None:
        q += " AND handle_id = ?"
        args.append(int(owner_handle_id))
    if conn.execute(q, args).fetchone() is None:
        return None
    n = conn.execute("DELETE FROM group_member WHERE group_id = ?",
                     (int(gid),)).rowcount
    conn.execute("DELETE FROM friend WHERE id = ?", (int(gid),))
    conn.commit()
    return n


def set_group_member_class(conn, gid, member_name, cls):
    """Promote or demote one member. Returns True if a row actually moved.

    Separate from `add_group_member` on purpose: this must NOT create anybody.
    The 07:03 request that drives it names a member the client already believes
    is in the group, so a miss means our roster and the client's disagree, and
    inserting the row would paper over that instead of surfacing it.

    **Class 1 is not accepted here.** On the wire it means "leave the group"
    (measured 2026-08-15: the account holder left by setting their OWN class to
    1), and a leave is `remove_group_member`. Storing 1 would put the row outside
    polcore's 2..5 range check, which hides the member from every list without
    deleting them -- a group that reads as corrupt rather than as one they left.
    """
    if cls < GROUP_CLASS_MIN or cls > GROUP_CLASS_MAX:
        raise ValueError(f"group member class {cls} is outside polcore's 2..5")
    cur = conn.execute(
        "UPDATE group_member SET class = ? WHERE group_id = ? AND member_name = ?",
        (int(cls), int(gid), member_name))
    conn.commit()
    return cur.rowcount > 0


def count_group_members(conn, gid):
    row = conn.execute("SELECT COUNT(*) AS n FROM group_member WHERE group_id = ?",
                       (int(gid),)).fetchone()
    return int(row["n"]) if row else 0


def backfill_group_owners(conn, dry=False):
    """Give every group an owner row, at master class. Returns what it changed.

    WHY THIS EXISTS RATHER THAN A SERVE-TIME PATCH. A group IS a `friend` row of
    kind GROUP, so its owner has always been derivable -- but nothing ever wrote
    the owner into `group_member`, because for a long time nothing could write
    that table at all. `_group_members` covered the hole by falling back to the
    owner when the stored list was EMPTY, and that held right up until group
    invites started persisting (2026-08-16): the first real member ended the
    fallback and the owner silently vanished from their own group.

    The serve-time repair stays as a safety net, but a roster that is wrong in
    the database is wrong for anything that reads it directly -- a role change, a
    disband, an admin query -- so fix the data.

    Idempotent: a group whose owner is already stored is left alone, and an owner
    stored at the wrong class is corrected rather than duplicated.
    """
    changed, fixed = [], []
    groups = conn.execute(
        "SELECT f.id, f.peer_name, f.handle_id, h.handle_name "
        "FROM friend f JOIN handle h ON h.id = f.handle_id "
        "WHERE f.kind = ?", (KIND_GROUP,)).fetchall()
    for g in groups:
        gid, owner_id = int(g["id"]), int(g["handle_id"])
        row = conn.execute(
            "SELECT member_handle, class FROM group_member "
            "WHERE group_id = ? AND member_handle = ?",
            (gid, owner_id)).fetchone()
        if row is None:
            changed.append((g["peer_name"], g["handle_name"]))
            if not dry:
                add_group_member(conn, gid, g["handle_name"],
                                 member_handle=owner_id,
                                 cls=GROUP_CLASS_MASTER)
        elif int(row["class"]) != GROUP_CLASS_MASTER:
            fixed.append((g["peer_name"], g["handle_name"], int(row["class"])))
            if not dry:
                conn.execute(
                    "UPDATE group_member SET class = ? "
                    "WHERE group_id = ? AND member_handle = ?",
                    (GROUP_CLASS_MASTER, gid, owner_id))
    if not dry:
        conn.commit()
    return {"added": changed, "reclassed": fixed, "groups": len(groups)}


def list_group_members(conn, gid, limit=GROUP_MEMBER_MAX,
                       include_pending=False):
    """`[(guid, name, class), ...]` for a group, oldest first, capped.

    The guid is derived LIVE for a local member for the same reason
    `_db_friends` derives it: a stored value goes stale when the handle's id
    mapping changes, and polcore compares this guid against the client's own to
    decide whose class propagates to the group flags.

    The cap is applied HERE rather than by the caller so every consumer -- the
    reply-length calculation and the record loop -- sees the same list. Those two
    disagreeing is what makes the client walk off the end of one group's members
    into the next group's.

    Pending (invited, not yet accepted) members are EXCLUDED by default: only
    the group's owner should see them ("Inviting into group"), and the caller
    that serves the owner passes `include_pending=True`. Defaulting to hidden
    means every other consumer -- rosters served to members, the roster pushes,
    the chat sidebar's inputs -- gets the accepted membership without having to
    know the flag exists.
    """
    rows = conn.execute(
        "SELECT member_handle, member_name, member_guid, class, pending"
        " FROM group_member WHERE group_id = ?" +
        ("" if include_pending else " AND pending = 0") +
        " ORDER BY rowid LIMIT ?",
        (int(gid), int(limit))).fetchall()
    out = []
    for r in rows:
        guid = int(r["member_guid"] or 0)
        if r["member_handle"]:
            guid = handle_guid(int(r["member_handle"]))
        cls = int(r["class"])
        # THE WIRE CLASS SAYS WHETHER THEY HAVE ACCEPTED. `pending` is the
        # stored truth and the class is derived from it here, in the one place
        # every consumer reads: a pending invitee is class 2 ("Inviting into
        # group" -- what SE serves for its own pending rows), and an ACCEPTED
        # member is never 2, because 2 on the viewer's own row marks the whole
        # group unusable (see the class ladder at GROUP_CLASS_MEMBER). The
        # `max` also heals rows stored at 2 by the 2026-08-16..21 builds
        # without a migration; stored 4/5 pass through untouched.
        cls = GROUP_CLASS_INVITED if int(r["pending"] or 0) \
            else max(cls, GROUP_CLASS_MEMBER)
        out.append((guid, r["member_name"], cls))
    return out


def friend_watchers(conn, member_id):
    """Who should be told when `member_id`'s presence changes.

    A "watcher" is anyone who has one of `member_id`'s handles in their OWN
    friend list -- they are the people whose friend-list screen shows this member,
    so they are the ones a presence push has to reach. Returns

        [(watcher_member_id, watcher_handle_id, subject_handle_id), ...]

    where `subject_handle_id` is the handle of `member_id` that this watcher
    friended -- i.e. the one whose guid keys the record on the watcher's side, so a
    single-slot presence update can name the right friend. Groups are excluded
    (KIND_FRIEND only) and only agreed friendships count (an unsettled request has
    no rendered presence yet). One row per (watcher, subject-handle) pair.
    """
    rows = conn.execute(
        "SELECT own.member_id AS watcher_member, own.id AS watcher_handle,"
        "       sub.id        AS subject_handle "
        "FROM friend f "
        "JOIN handle sub ON sub.id = f.peer_handle "
        "JOIN handle own ON own.id = f.handle_id "
        "WHERE sub.member_id = ? AND f.kind = ? AND f.status = ?",
        (int(member_id), KIND_FRIEND, STATUS_ACTIVE)).fetchall()
    return [(int(r["watcher_member"]), int(r["watcher_handle"]),
             int(r["subject_handle"])) for r in rows]


# --------------------------------------------------------------------------- #
# THE FRIENDSHIP LOOP (2026-08-12)
#
# Before this, a friend request was invisible to the person being asked and could
# never become a friendship: `replace_friends` wrote one row on the REQUESTER's
# handle and stopped, and `set_friend_status` -- the accept step -- had no callers
# anywhere in the project. So the audit's "add a friend" always ended as a row on
# one side marked pending forever.
#
# Three statuses, and the third is the new one:
#
#     'active'   an agreed friendship. Both sides hold one of these.
#     'pending'  I asked THEM. Outgoing; waiting on their agreement.
#     'invited'  THEY asked ME. Incoming; the row that makes a request visible.
#
# `status` is a free-text column with no CHECK constraint, so 'invited' needs no
# migration -- an old DB simply has none of them yet.
#
# On the wire both pending flavours are the SAME state: `_friend_list_record`
# renders either with SE's captured 0900005A "awaiting approval" word, because
# that is the only pending value we have ever observed and inventing a second one
# would be a guess. The distinction is server-side, and it is what lets an accept
# be told apart from a re-request: only the party holding the 'invited' row can
# accept, which is the direction a request actually runs in.
#
# DELETION STAYS ONE-SIDED. Removing someone from your list removes YOUR row and
# leaves theirs alone. That is what a per-handle list means in a protocol where
# the whole list is PUT by its owner, and cascading the delete would let one
# client silently rewrite another account's data on the strength of a record grid
# we have still never seen populated on the wire.
STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"      # outgoing: I asked them
STATUS_INVITED = "invited"      # incoming: they asked me

#: Both flavours of "not agreed yet". Anything here renders as SE's pending word.
STATUS_UNSETTLED = (STATUS_PENDING, STATUS_INVITED)


def _peer_handle_row(conn, peer_name):
    """The local handle a friend name refers to, or None if they are not local."""
    return conn.execute("SELECT id, member_id FROM handle WHERE handle_name = ?",
                        (peer_name,)).fetchone()


def request_friend(conn, handle_id, peer_name, kind=KIND_FRIEND, guid=None):
    """Ask `peer_name` to be a friend of `handle_id`, or ACCEPT if they asked first.

    Returns one of 'accepted', 'requested', 'exists' -- the caller logs it, and
    the three cases are genuinely different events rather than degrees of the
    same one.

    The accept case is the reason this is not just `add_friend` twice: if I am
    holding an 'invited' row from them, me naming them back is agreement, and
    both rows go active together. That is the only transition in the model, and
    it is driven entirely by which side already holds which row.
    """
    own = conn.execute(
        "SELECT id, status FROM friend WHERE handle_id = ? AND peer_name = ?",
        (handle_id, peer_name)).fetchone()
    peer = _peer_handle_row(conn, peer_name)
    me = conn.execute("SELECT handle_name, member_id FROM handle WHERE id = ?",
                      (handle_id,)).fetchone()

    # SELF-ADD GUARD (2026-08-21). A handle can never be its own friend. Tetra
    # Master's room member sidebar offers "Ask to become friends" on the
    # player's OWN row (a client-side enable bug in TM.dll that keys off an
    # identity space we could not make the guard recognise -- see memory
    # tm-member-sidebar-identity). Whatever the client offers, a friend request
    # that resolves to one of the requester's OWN handles must not create a
    # friendship. This is the ONE primitive every add path funnels through
    # (replace_friends / the 2:6 write, accept_friend, the CLI), so guarding
    # here catches the self-add regardless of which path or message carried it.
    #
    # MATCH BY HANDLE, NOT BY NAME OR MEMBER. The self row in the room sidebar is
    # the exact handle the player is signed in as, so a self-add names THIS
    # handle: peer resolves to `handle_id` itself. A different handle on the same
    # account is deliberately NOT caught -- two of your own handles friending
    # each other is a legal (if unusual) relationship the model already supports,
    # and matching on member_id would forbid it. Matching by name alone would
    # false-positive on a different person who happens to share your handle name.
    # A self-request is reported as its own outcome so callers count it as
    # neither an add nor an accept and no row is written.
    if os.environ.get("POL_FRIEND_NO_SELF", "1") == "1" \
            and peer is not None and int(peer["id"]) == int(handle_id):
        return "self"

    if own is not None and own["status"] == STATUS_INVITED:
        # THEY asked, I am naming them back: that is an acceptance.
        set_friend_status(conn, handle_id, peer_name, STATUS_ACTIVE)
        # BOTH ROWS GO ACTIVE TOGETHER -- default since 2026-08-16, and it is what
        # SE does.
        #
        # This used to leave the ASKER pending "until their client says otherwise",
        # on the reasoning that flipping it early made the client offer to RESEND
        # ("Fox is not waiting for friend registration"). Three things retired
        # that:
        #
        #   * the resend prompt was watched happening with the asker's row PENDING
        #     and with it ACTIVE, so row state does not cause it;
        #   * the asker's client was measured NOT to send the 2:6 the old comment
        #     depended on -- it read the acceptance and sent nothing, so the row
        #     could only ever close at the next login;
        #   * SE's own service pushes the acceptance to the asker (a 0x8480-class
        #     record on the auth band) and the row was observed updating live
        #     on retail, which is only consistent with SE having already moved
        #     it server-side.
        #
        # `reconcile_pending` stays as the backstop for rows that predate this or
        # whose acceptance arrived while the asker was offline.
        # POL_FRIEND_ACCEPT_BOTH=0 restores the wait-for-the-client behaviour.
        if peer is not None and me is not None \
                and os.environ.get("POL_FRIEND_ACCEPT_BOTH", "1") == "1":
            set_friend_status(conn, int(peer["id"]), me["handle_name"],
                              STATUS_ACTIVE)
        return "accepted"

    if own is not None and own["status"] == STATUS_PENDING and peer is not None \
            and me is not None:
        # I ASKED, AND THEY HAVE SINCE ACCEPTED: my client naming them again is
        # it acting on the notification. Their row is already active, so this is
        # the confirmation that closes the loop rather than a fresh request.
        theirs = conn.execute(
            "SELECT status FROM friend WHERE handle_id = ? AND peer_name = ?",
            (int(peer["id"]), me["handle_name"])).fetchone()
        if theirs is not None and theirs["status"] == STATUS_ACTIVE:
            set_friend_status(conn, handle_id, peer_name, STATUS_ACTIVE)
            return "accepted"

    if own is not None:
        # ALREADY ASKED -- BUT DID THE ASK EVER LAND? A row on my side is not
        # evidence of one on theirs, and until 2026-08-16 this returned here
        # without ever looking. Two ways the mirror below can be skipped and
        # leave a request that exists only for the asker:
        #
        #   * the peer held a STALE row when I asked. Deletion is asymmetric
        #     (I drop them, they keep me), so a delete-and-re-friend finds the
        #     peer still holding me and mirrors nothing.
        #   * the peer deleted their row afterwards.
        #
        # Either way my client re-sending the name is my client saying "ask them
        # again", and it could never repair it: `replace_friends` did not even
        # call this function for a row it already held. Measured live -- `Fox`
        # asked `PS2Tester` twice, `PS2Tester` had no row either time, and the
        # log read `+0 requested, 0 accepted` while the sender's screen said
        # "awaiting confirmation" forever.
        #
        # ONLY REPAIRED WHILE I AM ACTUALLY WAITING. If my row is ACTIVE and
        # theirs is missing, they deleted me on purpose -- re-inviting them off
        # the back of an unrelated write (a rename is also a 2:6 naming them)
        # would resurrect a friendship they ended.
        if own["status"] == STATUS_PENDING \
                and _mirror_request(conn, handle_id, peer, me, kind) == "added":
            return "requested"
        return "exists"                 # already active, or already asked

    add_friend(conn, handle_id, peer_name, kind=kind,
               peer_handle=int(peer["id"]) if peer else None, guid=guid,
               status=STATUS_PENDING if kind == KIND_FRIEND else STATUS_ACTIVE)

    if _mirror_request(conn, handle_id, peer, me, kind) == STATUS_ACTIVE:
        # THEY ALREADY HOLD ME AS A FRIEND, so there is nobody to ask. This is
        # the asymmetric-deletion leftover again, from the other end: I dropped
        # them, they kept me, and now I have added them back. Leaving my row
        # pending would park it on "awaiting confirmation" against a peer who
        # has no request to answer and never will. Their own state already says
        # we are friends, so agreeing with it is the only consistent answer.
        set_friend_status(conn, handle_id, peer_name, STATUS_ACTIVE)
        return "accepted"
    return "requested"


def _mirror_request(conn, handle_id, peer, me, kind):
    """Make sure the person being asked has a row saying so.

    Without this the request never reaches the other party -- it sat only on the
    asker's handle, which is why every friend request in this project's history
    was invisible to the person being asked.

    Returns `'added'` if it created the incoming row, the peer's EXISTING status
    if they already had one (so the caller can tell "they already hold me" from
    "they have been asked"), or None when there is nobody local to mirror onto.
    A name we cannot resolve to a handle has no list to be mirrored into; the
    outgoing row is stored either way, so the asker's own screen is unchanged.
    """
    if kind != KIND_FRIEND or peer is None or me is None \
            or int(peer["id"]) == int(handle_id):
        return None
    theirs = conn.execute(
        "SELECT id, status FROM friend WHERE handle_id = ? AND peer_name = ?",
        (int(peer["id"]), me["handle_name"])).fetchone()
    if theirs is not None:
        return theirs["status"]
    add_friend(conn, int(peer["id"]), me["handle_name"],
               kind=kind, peer_handle=handle_id, status=STATUS_INVITED)
    return "added"


def reconcile_pending(conn, handle_id, skip=()):
    """Close any of my requests the other side has already accepted.

    THE ACCEPT IS TWO STEPS ON PURPOSE (see `request_friend`): the accepter goes
    ACTIVE at once, the ASKER stays PENDING until their own client acts on the
    acceptance notification. That is right while a notification actually
    arrives. Measured 2026-08-16, one did not: `DeckTester` accepted
    `LaptopTest2`'s request at 14:52:01, the server does not mint the acceptance
    mail (`POL_FRIEND_ACCEPT_MAIL` defaults off, because a client was once seen
    posting its own), and this time **no client posted one either** -- no 3:1, no
    stored object, nothing in the log after the accept. So the asker was never
    told, and their row read *"Awaiting confirmation"* with nothing in the system
    that could ever change it.

    This is the backstop: if I am PENDING and they hold me ACTIVE, the
    friendship is already agreed on both sides and my row is simply stale. Same
    rule `request_friend` applies when the asker's client does speak up -- this
    just stops it waiting on a message that may never come.

    `skip` names peers to leave alone -- the caller passes anyone whose
    acceptance notification is still sitting UNREAD in my mailbox. That is the
    one case where promoting early is wrong: a client that reads the notification
    after its row has gone active offers to RESEND, which is the "Fox is not
    waiting for friend registration" reported live. While the message is unread
    the client is still going to close its own loop, so leave it to; once the
    message is gone and the row is STILL pending, nothing else is coming.

    Deliberately a parameter and not an env check: whether a notification is
    outstanding is per-row STATE, not configuration, and this module has no
    business reaching into the mail store to find out.

    Returns the peer names promoted. Cheap: one query, and no further work at all
    unless something is actually pending.
    """
    if os.environ.get("POL_FRIEND_PENDING_RECONCILE", "1") != "1":
        return []
    skip = set(skip or ())
    me = conn.execute("SELECT handle_name FROM handle WHERE id = ?",
                      (handle_id,)).fetchone()
    if me is None:
        return []
    mine = conn.execute(
        "SELECT peer_name FROM friend WHERE handle_id = ? AND status = ?"
        " AND kind = ?", (handle_id, STATUS_PENDING, KIND_FRIEND)).fetchall()
    done = []
    for row in mine:
        if row["peer_name"] in skip:
            continue                        # their acceptance is still unread
        peer = _peer_handle_row(conn, row["peer_name"])
        if peer is None or int(peer["id"]) == int(handle_id):
            continue                        # not one of ours, nothing to read
        theirs = conn.execute(
            "SELECT status FROM friend WHERE handle_id = ? AND peer_name = ?",
            (int(peer["id"]), me["handle_name"])).fetchone()
        if theirs is not None and theirs["status"] == STATUS_ACTIVE:
            set_friend_status(conn, handle_id, row["peer_name"], STATUS_ACTIVE)
            done.append(row["peer_name"])
    return done


def accept_friend(conn, handle_id, peer_name):
    """Agree to an incoming request. Returns True if one was there to agree to."""
    own = conn.execute(
        "SELECT status FROM friend WHERE handle_id = ? AND peer_name = ?",
        (handle_id, peer_name)).fetchone()
    if own is None or own["status"] != STATUS_INVITED:
        return False
    return request_friend(conn, handle_id, peer_name) == "accepted"


def decline_friend(conn, handle_id, peer_name):
    """Refuse an incoming request: drop my row, and demote theirs to 'pending'.

    Their outgoing row is NOT deleted -- SE's own auto-message vocabulary carries
    a distinct "declined" type (10), so the asker is meant to be told rather than
    to silently keep waiting. Until that message layer exists, leaving the row is
    what preserves the fact that they asked.

    **This is the one removal that stays one-sided**, which is why it passes
    `mirror=False`: deleting a friend is now symmetric (`_drop_friend_mirror`),
    and letting that fire here would un-ask the request instead of refusing it,
    erasing the very fact the paragraph above exists to keep.
    """
    own = conn.execute(
        "SELECT status FROM friend WHERE handle_id = ? AND peer_name = ?",
        (handle_id, peer_name)).fetchone()
    if own is None or own["status"] != STATUS_INVITED:
        return False
    remove_friend(conn, handle_id, peer_name, mirror=False)
    return True


def pending_requests(conn, handle_id):
    """Incoming requests awaiting this handle's answer."""
    return list_friends(conn, handle_id, status=STATUS_INVITED)


def replace_friends(conn, handle_id, entries):
    """Sync a handle's whole list to `entries` -- the shape the client sends.

    The Viewer does not have an "add one friend" message. `KPutFriendList`
    (lobby 2:6) uploads the ENTIRE list every time, so an add, a rename, a
    reorder and a delete all arrive as the same request and differ only in the
    contents. Mirroring that here is what makes deletion stick: an upsert-only
    write can never remove a row, so a friend the user deleted would come back
    on the next read.

    `entries` is [{"name":..., "guid":..., "kind":...}]. Rows already present
    keep their id, `created_at` and `status`, so an accepted friendship is not
    downgraded to pending by an unrelated list write.

    One name in the list can mean three different things depending on what the
    server already holds for it -- a new request, an acceptance of theirs, or
    nothing at all -- so those are counted separately rather than lumped into
    "added".

    Returns (added, accepted, updated, removed, kept) -- `kept` names the
    incoming requests that were omitted from the write but NOT deleted, see the
    note at the bottom of this function.
    """
    have = {r["peer_name"]: r for r in conn.execute(
        "SELECT * FROM friend WHERE handle_id = ?", (handle_id,))}
    seen, added, accepted, updated = set(), 0, 0, 0
    for e in entries:
        name = e.get("name")
        if not name:
            continue
        seen.add(name)
        kind = int(e.get("kind", KIND_FRIEND))
        guid = int(e.get("guid") or 0)
        old = have.get(name)
        # ALWAYS ASK `request_friend`, and let IT decide what this write means.
        # It knows all three cases -- a name the server has never seen is a new
        # request, a name I hold as INVITED is me accepting (a whole-list protocol
        # has no separate "accept" message, the same way it has no separate
        # "add"), and a name I already hold is a repeat.
        #
        # A repeat used to be skipped here entirely, which is what made a lost
        # request unrepairable: the one call that checks whether the OTHER side
        # ever got the invitation was never reached, so re-sending it from the
        # client did nothing at all. Counting from the outcome rather than from
        # `old` also keeps the log honest -- a repeat that had to re-mirror now
        # reports as a request, because for the recipient that is what it is.
        outcome = request_friend(conn, handle_id, name, kind=kind,
                                 guid=guid or None)
        if outcome == "requested":
            added += 1
        elif outcome == "accepted":
            accepted += 1
        # THE CLIENT'S OWN ID FOR THE ROW, recorded after the row is sure to
        # exist. It is the only handle a later DELETE gives us -- that record
        # carries no name -- and the client re-mints it on a delete-and-re-add,
        # so it is rewritten on every write rather than only on insert.
        if e.get("client_ref"):
            conn.execute("UPDATE friend SET client_ref = ? WHERE handle_id = ?"
                         " AND peer_name = ?",
                         (e["client_ref"], handle_id, name))
        if e.get("wire_ref"):
            conn.execute("UPDATE friend SET wire_ref = ? WHERE handle_id = ?"
                         " AND peer_name = ?",
                         (e["wire_ref"], handle_id, name))
        if old is None or old["status"] == STATUS_INVITED:
            continue
        if int(old["kind"]) != kind or (guid and int(old["peer_guid"]) != guid):
            conn.execute(
                "UPDATE friend SET kind = ?, peer_guid = ? WHERE id = ?",
                (kind, guid or old["peer_guid"], old["id"]))
            updated += 1
    # *** 2:6 IS NOT A WHOLE-LIST PUT. Measured 2026-08-13. ***
    #
    # `KPutFriendList` and the request builder's `168N + 312` size formula both
    # say "the entire list", and this function was written to that reading. The
    # wire says otherwise: EVERY 2:6 ever captured is `payload_len=480`, i.e.
    # N=1, including one sent while the account already held two friends. A
    # whole-list write would have been 648. The client sends ONE RECORD PER
    # CHANGE.
    #
    # So an omitted name means nothing at all, and deleting on omission is pure
    # data loss: adding a second friend wiped the first, twice, before this was
    # understood. Deletion by omission is therefore OFF by default and this
    # behaves as an upsert.
    #
    # POL_FRIEND_PUT_WHOLE_LIST=1 restores the old semantics for the day someone
    # captures a genuine multi-record write. `POL_FRIEND_PUT_DECLINE` still gates
    # whether an omitted INCOMING request may be dropped, and only has any effect
    # when whole-list mode is on.
    whole_list = os.environ.get("POL_FRIEND_PUT_WHOLE_LIST", "0") == "1"
    protect = os.environ.get("POL_FRIEND_PUT_DECLINE", "0") != "1"
    gone, kept, kept_groups = [], [], []
    for n, r in have.items():
        if n in seen:
            continue
        if not whole_list:
            # The write named one entry; every other row is simply none of its
            # business. This is the default -- see the note above.
            kept.append(n)
        elif int(r["kind"]) == KIND_GROUP:
            # A GROUP IS NOT IN THIS LIST, so its absence is not a delete. 2:6 is
            # `KPutFriendList` and carries people; groups have their own opcodes
            # (7:1 create, 7:2 delete). Live 2026-08-12 the first real friend
            # write deleted the account's group as collateral, purely because it
            # was a row the write did not mention.
            kept_groups.append(n)
        elif protect and r["status"] == STATUS_INVITED:
            kept.append(n)
        else:
            gone.append(r["id"])
    if gone:
        conn.executemany("DELETE FROM friend WHERE id = ?",
                         [(i,) for i in gone])
    conn.commit()
    # Groups are reported separately from held-back invitations: they are kept
    # for a completely different reason (they are not in this list at all), and
    # lumping them together made the log claim the account's group was an
    # incoming friend request.
    return added, accepted, updated, len(gone), kept, kept_groups


def set_friend_status(conn, handle_id, peer_name, status):
    """Move one entry between 'pending'/'invited' and 'active'."""
    cur = conn.execute(
        "UPDATE friend SET status = ? WHERE handle_id = ? AND peer_name = ?",
        (status, handle_id, peer_name))
    conn.commit()
    return cur.rowcount


def primary_handle(conn, member_id):
    row = conn.execute(
        "SELECT handle_name FROM handle WHERE member_id = ?"
        " ORDER BY is_primary DESC, id ASC LIMIT 1", (member_id,)).fetchone()
    return row["handle_name"] if row else None


def ucs_params(conn, polid):
    """The (area_kbn, login_pf, property) triple the UCS CGI URL carries."""
    row = conn.execute(
        "SELECT area_kbn, login_pf, property FROM polid WHERE polid = ?",
        (polid,)).fetchone()
    if row is None:
        return ("00", "01", "00")
    return (row["area_kbn"], row["login_pf"], row["property"])


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def open_session(conn, member_id, nick=None, peer_ip=None, iv=None,
                 lobby_port=None, ttl_seconds=3600):
    token = secrets.token_hex(16)
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(seconds=ttl_seconds)
    conn.execute(
        "INSERT INTO session (token, member_id, nick, peer_ip, iv, lobby_port,"
        " created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (token, member_id, nick, peer_ip,
         iv.hex() if isinstance(iv, (bytes, bytearray)) else iv,
         lobby_port,
         now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         exp.strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
    return token


def get_session(conn, token):
    return conn.execute("SELECT * FROM session WHERE token = ?",
                        (token,)).fetchone()


#: How recent a session row must be to count as evidence of who is at the client.
#: Deliberately far shorter than the 1 h session TTL -- see `sole_online_member`.
SESSION_FRESH_SECONDS = int(os.environ.get("POL_SESSION_FRESH_SECONDS", "1800"))


def sole_online_member(conn, max_age_seconds=None):
    """The one member currently online, or None if that is zero or ambiguous.

    This exists for one purpose: letting the ucs-cgi account servlet know who is
    asking, so a logged-in player is not made to type a PlayOnline ID they have
    no reason to have memorised. It is a display convenience and NOTHING else --
    the password is still verified against the member this returns.

    ## Why this cannot be done properly, measured 2026-08-16

    There is NO server-side path from an HTTP request to a lobby session. The
    Viewer tunnels its portal/CGI fetches through the band ports as ordinary HTTP
    (`responders._serve_http_on_lobby`) on a SEPARATE TCP connection from the
    lobby session, and that request carries no identity at all: six real client
    requests in `logs/ucs.log` show user-agent, referer and
    `x-playonline-want-hello`, no Authorization and no cookie.

    `session.peer_ip` looked like the answer and is not. The CGI never sees the
    client: requests arrive from `authsess` (172.18.0.11), and even at the relay
    every Viewer is the Docker gateway 172.18.0.1 -- which `responders.py` already
    says outright ("both reach us from the same docker gateway address, so the
    peer IP cannot tell them apart"). An earlier version of this function keyed on
    peer_ip and could therefore never fire. `session.lobby_port` is NULL, so that
    is no help either.

    So the only honest reading of "who is asking" is "the only person who could
    be" -- which is available exactly when one member is online. Hence: one
    member, or nothing. No tie-breaks, no most-recent-wins; guessing here would
    put one player's POL ID in front of another.

    ## Freshness, and why the session TTL is the wrong clock

    A session row is created at login and removed at logout (`close_sessions`),
    but a client that dies without a clean logout leaves its row behind for the
    full 1 h TTL. Measured on the live stack: two rows "live", one established
    band connection -- so a ghost was making a single real player look like two
    and silencing this. `max_age_seconds` (default `SESSION_FRESH_SECONDS`) caps
    how long a row may speak for someone, which bounds that window without
    touching session lifecycle. Nothing refreshes these rows during play, so this
    is genuinely "logged in recently", and a long session stops pre-filling
    rather than pre-filling something wrong. That is the right way to fail.

    The proper fix is to close the session when the socket dies, in
    `responders.py` -- deliberately NOT done here; see STATUS "WORK IN FLIGHT".

    ## Two subtleties, both found by the self-test rather than by reasoning

    * The ambiguity is per MEMBER, not per POL ID: two sub-accounts of a single
      POL ID are two people, and a `DISTINCT polid` query collapsed them.
    * Only a PRIMARY member (`member_no` 0) is returned. The POL ID is the only
      identifier here a player recognises AND that resolves back to the member it
      came from (`member_by_polid` answers with the primary). A sub-account's own
      identifier is its login name, which for a real account is the client's
      scrambled NICK (`UR8I8TYQL`) -- worse than showing nothing.
    """
    if max_age_seconds is None:
        max_age_seconds = SESSION_FRESH_SECONDS
    fresh = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(seconds=int(max_age_seconds))
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT DISTINCT s.member_id FROM session s"
        " WHERE s.expires_at > ? AND s.created_at > ? LIMIT 2",
        (_now(), fresh)).fetchall()
    if len(rows) != 1:
        return None
    row = conn.execute("SELECT * FROM member WHERE id = ?",
                       (rows[0]["member_id"],)).fetchone()
    if row is None or row["member_no"] != 0 or row["status"] != "active":
        return None
    return row


def record_login(conn, member_id, when=None):
    """Stamp a login, rotating the previous one into `prev_login_at`.

    The lobby's "Last Login" is the login BEFORE this one -- by the time the
    client asks (lobby 04:06) the current session is already up -- so the
    rotation is the point of this, not the stamp itself. Returns the row's
    (prev_login_at, last_logout_at) as they were BEFORE the rotation, which is
    exactly what the 04:06 record should carry.
    """
    now = when or _now()
    row = conn.execute(
        "SELECT last_login_at, last_logout_at FROM member WHERE id = ?",
        (member_id,)).fetchone()
    prev = row["last_login_at"] if row else None
    conn.execute(
        "UPDATE member SET prev_login_at = last_login_at, last_login_at = ?"
        " WHERE id = ?", (now, member_id))
    conn.commit()
    return prev, (row["last_logout_at"] if row else None)


def record_logout(conn, member_id, when=None):
    """Stamp a logout. Called when the session socket goes away."""
    conn.execute("UPDATE member SET last_logout_at = ? WHERE id = ?",
                 (when or _now(), member_id))
    conn.commit()


def login_times(conn, member_id):
    """(prev_login_at, last_logout_at) for the lobby session record."""
    row = conn.execute(
        "SELECT prev_login_at, last_login_at, last_logout_at FROM member"
        " WHERE id = ?", (member_id,)).fetchone()
    if row is None:
        return None, None
    # First ever login has no previous one; showing this session's own login is
    # better than showing the epoch, which is what an unset field renders as.
    return (row["prev_login_at"] or row["last_login_at"]), row["last_logout_at"]


def member_online(conn, member_id):
    """True if the member has a live (unexpired) session.

    PRESENCE IS THE SESSION TABLE. No separate state is needed: a member is
    online exactly while a session row of theirs has not expired, `open_session`
    creates it at login and `close_sessions` drops it at logout. Anything else
    would be a second source of truth that could disagree with the first.
    """
    row = conn.execute(
        "SELECT 1 FROM session WHERE member_id = ? AND expires_at > ? LIMIT 1",
        (int(member_id), _now())).fetchone()
    return row is not None


def member_online_from(conn, member_id, peer_ip):
    """True if the member holds a live session that was opened FROM `peer_ip`.

    The mail handlers' fallback credential: a peer that is the member's own
    signed-in Viewer may read that member's inbox and send as that member with
    no mail password on file; any other peer may not. Same table, same
    expiry rule as `member_online` -- one source of truth for "online".
    """
    if not peer_ip:
        return False
    row = conn.execute(
        "SELECT 1 FROM session WHERE member_id = ? AND peer_ip = ? "
        "AND expires_at > ? LIMIT 1",
        (int(member_id), str(peer_ip), _now())).fetchone()
    return row is not None


def handle_online(conn, handle_id):
    """True if the handle's owning member is online."""
    row = conn.execute("SELECT member_id FROM handle WHERE id = ?",
                       (int(handle_id),)).fetchone()
    return bool(row) and member_online(conn, row["member_id"])


def close_sessions(conn, member_id):
    """Drop a member's sessions -- they are logging out, so they go offline."""
    cur = conn.execute("DELETE FROM session WHERE member_id = ?",
                       (int(member_id),))
    conn.commit()
    return cur.rowcount


def purge_sessions(conn):
    """Drop expired sessions. Cheap enough to call on every login."""
    cur = conn.execute("DELETE FROM session WHERE expires_at < ?", (_now(),))
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------- #
# permissive provisioning
# --------------------------------------------------------------------------- #
def ensure_member(conn, nick, default_contents=(1, 2, 4, 11, 14)):
    """Resolve `nick` to a member, creating a POL ID + member + handle if it is
    unknown. This is what keeps the auth path working exactly as it did before
    this module existed: the client picks its own NICK and we do not reject it.

    Enforcement (rejecting unknown nicks) is the caller's choice -- see
    POL_ACCOUNTS_ENFORCE in responders.handle_authserv. Returns the member row.
    """
    if isinstance(nick, (bytes, bytearray)):
        nick = nick.decode("ascii", "replace")
    nick = nick.strip()
    row = member_by_handle(conn, nick)
    if row is not None:
        return row
    row = get_member(conn, nick)
    if row is not None:
        return row
    # Auto-provision. The password is random and unused: nothing in the login
    # chain checks it yet (the client authenticates with a machine-derived
    # token, not a password we can see), so this is a placeholder that a real
    # registration flow later overwrites.
    polid = nick
    if conn.execute("SELECT 1 FROM polid WHERE polid = ?", (polid,)).fetchone() is None:
        create_polid(conn, polid, secrets.token_hex(16))
    member_id = add_member(conn, polid, nick, secrets.token_hex(16))
    set_handle(conn, member_id, nick)
    for code in default_contents:
        grant_content(conn, member_id, code)
    # A grant is the entitlement; the LINK is what the client can see. Lobby 1:3
    # builds the launcher's character table out of `handle_content` only, so an
    # account provisioned here without links owns every title on the panel and
    # refuses to launch any of them ("You have no Content ID for <game>").
    link_member_content_to_primary(conn, member_id)
    return conn.execute("SELECT * FROM member WHERE id = ?",
                        (member_id,)).fetchone()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cmd_list(conn):
    rows = conn.execute(
        "SELECT p.polid, p.status AS pstatus, m.id, m.member_no, m.login_name,"
        " m.status AS mstatus FROM polid p LEFT JOIN member m ON m.polid = p.polid"
        " ORDER BY p.polid, m.member_no").fetchall()
    if not rows:
        print("(empty)")
        return
    for r in rows:
        if r["id"] is None:
            print(f"{r['polid']:<16} [{r['pstatus']}]  (no members)")
            continue
        hn = primary_handle(conn, r["id"]) or "-"
        codes = content_ids(conn, r["id"])
        names = ",".join(CONTENT_NAMES.get(c, str(c)) for c in codes) or "-"
        print(f"{r['polid']:<16} [{r['pstatus']}]  #{r['member_no']} "
              f"{r['login_name']:<16} [{r['mstatus']}]  HN={hn:<12} {names}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="PlayOnline account database")
    ap.add_argument("db", help="path to accounts.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("list")

    p = sub.add_parser("addpolid")
    p.add_argument("polid"); p.add_argument("password")
    p.add_argument("--area", default="00"); p.add_argument("--pf", default="01")
    p.add_argument("--prop", default="00")

    p = sub.add_parser("addmember")
    p.add_argument("polid"); p.add_argument("login"); p.add_argument("password")
    p.add_argument("--handle")

    p = sub.add_parser("backfill-group-owners",
                       help="write the missing owner row into every group, at "
                            "master class -- the repair for groups created "
                            "before membership was stored")
    p.add_argument("--dry-run", action="store_true",
                   help="report what it would change and write nothing")

    p = sub.add_parser("reissue-polid", help="give an account a different "
                       "PlayOnline ID, keeping members, handles and grants -- "
                       "the repair for an ID the client's field will not accept")
    p.add_argument("old")
    p.add_argument("new", nargs="?", help="default: mint a fresh SE-shaped one")

    p = sub.add_parser("del-account", help="erase an account and everything "
                       "hanging off it -- members, handles, grants, friends, "
                       "mail. IRREVERSIBLE; prints the inventory and asks first")
    p.add_argument("polid")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.add_argument("--release-codes", action="store_true",
                   help="return the registration code(s) it redeemed to the "
                        "unused pool (default: keep them marked redeemed)")

    p = sub.add_parser("alias", help="map an extra login NICK onto a member. "
                       "The client DERIVES the nick it sends from the ID typed "
                       "at Add Member, so a registered account whose nick we "
                       "cannot predict is joined up here, using the nick the "
                       "auth log printed for the rejected/auto-provisioned login")
    p.add_argument("nick"); p.add_argument("login")
    p.add_argument("--note")

    p = sub.add_parser("sync-nicks", help="bind every account to the login NICK "
                       "its PlayOnline ID scrambles to, so registered accounts "
                       "can be logged into. Idempotent; run after an upgrade")

    p = sub.add_parser("grant")
    p.add_argument("login"); p.add_argument("code", type=int)
    p.add_argument("--no", dest="content_no")

    p = sub.add_parser("revoke")
    p.add_argument("login"); p.add_argument("code", type=int)

    p = sub.add_parser("passwd", help="change an account's password. `who` is a "
                       "login name, a POL ID or a bound login NICK")
    p.add_argument("who"); p.add_argument("password")
    p.add_argument("--reset-token", action="store_true",
                   help="ALSO forget the recorded NICK token, so the next login "
                        "re-seeds it (trust-on-first-use). This -- not the "
                        "password -- is what the lobby login checks; use it to "
                        "un-lock-out an account whose client presents a token "
                        "the server no longer agrees with")

    p = sub.add_parser("add-account", help="create a whole account in one "
                       "transaction: POL ID, member, handle, content and mail")
    p.add_argument("handle", nargs="?", help="default = Player<NNNN>")
    p.add_argument("--password", help="default = a random one, printed once")
    p.add_argument("--content", type=int, action="append", default=[],
                   help="content code to grant; repeatable (default 1)")
    p.add_argument("--code", help="redeem this registration code, whose grants "
                                  "then REPLACE --content")

    p = sub.add_parser("del-handle", help="delete + tombstone a handle so the "
                       "client's cached copy is not re-captured on 0:8")
    p.add_argument("handle")
    p.add_argument("--login", help="member login; default = first member")

    p = sub.add_parser("link-content", help="attach a Content ID to a handle so "
                       "the title is launchable under it. Exclusive: it is "
                       "detached from any other handle the member holds")
    p.add_argument("handle")
    p.add_argument("code", type=int)
    p.add_argument("--content-id", help="explicit Content ID value (else provisional)")

    p = sub.add_parser("unlink-content", help="take a title off a handle, "
                       "KEEPING the member's licence, so it can be placed on "
                       "another handle. To cancel the licence itself, use "
                       "`revoke` -- that clears the handles too")
    p.add_argument("handle")
    p.add_argument("code", type=int)

    p = sub.add_parser("move-content", help="move a title from one handle to "
                       "another of the same member, keeping its Content ID")
    p.add_argument("from_handle", metavar="from-handle")
    p.add_argument("to_handle", metavar="to-handle")
    p.add_argument("code", type=int)

    p = sub.add_parser("link-all", help="link every member's active content to "
                       "their primary handle (mints provisional Content IDs)")

    # Friend commands. These exist because the friendship loop had NO operator
    # entry point at all -- every friend row in this database was hand-written
    # SQL -- and because they are the only way to exercise the loop without two
    # Viewers running side by side.
    p = sub.add_parser("friend-request", help="ask (or accept, if they asked "
                       "first) -- the same rule the 2:6 whole-list PUT follows")
    p.add_argument("handle", help="the handle doing the asking")
    p.add_argument("peer", help="the handle being asked")

    p = sub.add_parser("friend-accept", help="agree to an incoming request")
    p.add_argument("handle"); p.add_argument("peer")

    p = sub.add_parser("friend-decline", help="refuse an incoming request")
    p.add_argument("handle"); p.add_argument("peer")

    p = sub.add_parser("friend-remove", help="drop one entry from a handle's "
                       "list (one-sided, like the client's own delete)")
    p.add_argument("handle"); p.add_argument("peer")

    p = sub.add_parser("friends", help="show a handle's list with statuses")
    p.add_argument("handle")

    p = sub.add_parser("group-add", help="put someone in a group (the group is "
                       "named as the OWNER's group, since names are per-handle)")
    p.add_argument("handle", help="the handle that owns the group")
    p.add_argument("group")
    p.add_argument("member", help="the member's handle name, local or not")
    p.add_argument("--class", dest="cls", type=int, default=GROUP_CLASS_MEMBER,
                   help=f"role: {GROUP_CLASS_MASTER} master, "
                        f"{GROUP_CLASS_SUBMASTER} sub-master, "
                        f"{GROUP_CLASS_MEMBER} member (default). Measured off SE; "
                        f"3 is not a role and greys out invite")

    p = sub.add_parser("group-remove", help="take someone out of a group")
    p.add_argument("handle"); p.add_argument("group"); p.add_argument("member")

    p = sub.add_parser("group-members", help="show one group's membership, or "
                       "every group's when no group is named")
    p.add_argument("handle"); p.add_argument("group", nargs="?")

    args = ap.parse_args(argv)
    conn = connect(args.db)

    if args.cmd == "init":
        print(f"initialised {args.db}")
    elif args.cmd == "list":
        _cmd_list(conn)
    elif args.cmd == "addpolid":
        create_polid(conn, args.polid, args.password, args.area, args.pf,
                     args.prop)
        print(f"created POL ID {args.polid}")
    elif args.cmd == "addmember":
        mid = add_member(conn, args.polid, args.login, args.password)
        if args.handle:
            set_handle(conn, mid, args.handle)
        print(f"created member {args.login} (id={mid}) under {args.polid}")
    elif args.cmd == "backfill-group-owners":
        r = backfill_group_owners(conn, dry=args.dry_run)
        what = "would add" if args.dry_run else "added"
        for name, owner in r["added"]:
            print(f"  {what} {owner} as master of {name!r}")
        for name, owner, was in r["reclassed"]:
            print(f"  {owner} in {name!r}: class {was} -> "
                  f"{GROUP_CLASS_MASTER} (master)")
        print(f"{len(r['added'])} owner row(s) {what}, "
              f"{len(r['reclassed'])} reclassed, across {r['groups']} group(s)")
    elif args.cmd == "reissue-polid":
        new = reissue_polid(conn, args.old, args.new)
        nick = polnick.nick_for_polid(new) if polnick else "?"
        print(f"{args.old} is now {new} (logs in as {nick}) -- tell the account "
              f"holder, and have them re-enter it under Add Member")
    elif args.cmd == "sync-nicks":
        pairs = sync_login_nicks(conn)
        for polid, nick in pairs:
            print(f"  {polid} -> {nick}")
        print(f"bound {len(pairs)} account(s) to their login nick")
    elif args.cmd == "del-account":
        fp = account_footprint(conn, args.polid)
        if fp is None:
            raise SystemExit(f"no such PlayOnline ID: {args.polid}")
        print(f"{args.polid} [{fp['status']}] created {fp['created_at']}")
        for m in fp["members"]:
            print(f"  member #{m['member_no']} {m['login_name']}"
                  + (f"  mail {m['mail_address']}" if m["mail_address"] else ""))
        print(f"  handles: {', '.join(fp['handles']) or '-'}")
        print(f"  content: {', '.join(CONTENT_NAMES.get(c, str(c)) for c in fp['contents']) or '-'}")
        print(f"  {fp['friends']} friend entr(ies), {fp['groups']} group(s), "
              f"{fp['referenced_by']} entr(ies) on OTHER people's lists, "
              f"{fp['mail']} message(s), {fp['sessions']} open session(s)")
        if fp["online"]:
            print("  ** SIGNED IN RIGHT NOW -- they will be dropped **")
        if fp["regcodes"]:
            print(f"  redeemed code(s): {', '.join(fp['regcodes'])}"
                  + ("  (will be released)" if args.release_codes else ""))
        if not args.yes:
            if input("delete this account? type the POL ID to confirm: ").strip() \
                    != args.polid:
                raise SystemExit("not deleted")
        delete_polid(conn, args.polid, release_codes=args.release_codes)
        print(f"deleted {args.polid}")
    elif args.cmd == "alias":
        row = get_member(conn, args.login)
        if row is None:
            raise SystemExit(f"no such member: {args.login}")
        set_login_alias(conn, args.nick, row["id"], args.note)
        print(f"login nick {args.nick} now resolves to {args.login} "
              f"(POL ID {row['polid']})")
    elif args.cmd in ("grant", "revoke"):
        row = get_member(conn, args.login)
        if row is None:
            raise SystemExit(f"no such member: {args.login}")
        if args.cmd == "grant":
            grant_content(conn, row["id"], args.code, args.content_no)
            print(f"granted {CONTENT_NAMES.get(args.code, args.code)} to {args.login}")
        else:
            revoke_content(conn, row["id"], args.code)
            print(f"revoked {CONTENT_NAMES.get(args.code, args.code)} from {args.login}")
    elif args.cmd == "passwd":
        try:
            row = set_account_password(conn, args.who, args.password)
        except RegistrationError as exc:
            raise SystemExit(str(exc))
        if row is None:
            raise SystemExit(f"no such account: {args.who}")
        print(f"password updated for {row['login_name']} "
              f"(POL ID {row['polid']})")
        if args.reset_token:
            clear_login_token(conn, row["id"])
            print("  login token cleared -- the next login is trusted on sight "
                  "and re-seeds it")
        else:
            print("  NOTE: the lobby login checks the recorded NICK token, not "
                  "this password. Add --reset-token if they are locked out.")
    elif args.cmd == "add-account":
        pw = args.password or secrets.token_hex(6)
        try:
            acct = register_account(
                conn, args.handle or f"Player{secrets.randbelow(9999):04d}", pw,
                code=args.code, contents=tuple(args.content) or (1,))
        except RegistrationError as exc:
            raise SystemExit(str(exc))
        set_mail_password(conn, acct["member_id"], pw)
        nick = polnick.nick_for_polid(acct["polid"]) if polnick else "?"
        print(f"created {acct['polid']} (logs in as {nick})")
        print(f"  handle   {acct['handle']}")
        print(f"  password {pw}")
        print(f"  content  {', '.join(CONTENT_NAMES.get(c, str(c)) for c in acct['contents']) or '-'}")
        print(f"  mail     {acct['mail']}")
    elif args.cmd == "del-handle":
        if args.login:
            row = get_member(conn, args.login)
            if row is None:
                raise SystemExit(f"no such member: {args.login}")
            mid = row["id"]
        else:
            row = conn.execute("SELECT id FROM member ORDER BY id LIMIT 1").fetchone()
            if row is None:
                raise SystemExit("no members in the database")
            mid = row["id"]
        removed = delete_handle(conn, mid, args.handle)
        print(f"{'deleted' if removed else 'tombstoned (was not present)'} "
              f"handle {args.handle!r} for member {mid}")
    elif args.cmd == "link-content":
        h = conn.execute("SELECT id FROM handle WHERE handle_name = ?",
                         (args.handle,)).fetchone()
        if h is None:
            raise SystemExit(f"no such handle: {args.handle}")
        link_content_to_handle(conn, h["id"], args.code, args.content_id)
        row = conn.execute("SELECT content_id FROM handle_content WHERE handle_id = ?"
                           " AND content_code = ?", (h["id"], args.code)).fetchone()
        print(f"linked {CONTENT_NAMES.get(args.code, args.code)} "
              f"(Content ID {row['content_id']}) to handle {args.handle!r}")
    elif args.cmd in ("unlink-content", "move-content"):
        def _handle(name):
            row = conn.execute(
                "SELECT id, member_id FROM handle WHERE handle_name = ?",
                (name,)).fetchone()
            if row is None:
                raise SystemExit(f"no such handle: {name}")
            return row

        name = CONTENT_NAMES.get(args.code, args.code)
        if args.cmd == "unlink-content":
            h = _handle(args.handle)
            if not conn.execute("SELECT 1 FROM handle_content WHERE handle_id = ?"
                                " AND content_code = ?",
                                (h["id"], args.code)).fetchone():
                raise SystemExit(f"{name} is not on handle {args.handle!r}")
            unlink_content_from_handle(conn, h["id"], args.code)
            print(f"unlinked {name} from handle {args.handle!r} "
                  f"(the licence is untouched -- `revoke` cancels that)")
        else:
            src, dst = _handle(args.from_handle), _handle(args.to_handle)
            if src["member_id"] != dst["member_id"]:
                # A Content ID follows the licence, and the licence belongs to a
                # member. Moving across members would hand one account's title to
                # another, which is not a move -- it is a transfer, and nothing
                # here is prepared to do that safely.
                raise SystemExit(
                    f"{args.from_handle!r} and {args.to_handle!r} belong to "
                    "different members -- refusing to move content between accounts")
            row = conn.execute("SELECT content_id FROM handle_content"
                               " WHERE handle_id = ? AND content_code = ?",
                               (src["id"], args.code)).fetchone()
            if row is None:
                raise SystemExit(f"{name} is not on handle {args.from_handle!r}")
            # link_content_to_handle is exclusive, so this detaches the source.
            link_content_to_handle(conn, dst["id"], args.code, row["content_id"])
            print(f"moved {name} (Content ID {row['content_id']}) "
                  f"from {args.from_handle!r} to {args.to_handle!r}")
    elif args.cmd == "link-all":
        total = 0
        for m in conn.execute("SELECT id, login_name FROM member"):
            k = link_member_content_to_primary(conn, m["id"])
            total += k
            print(f"  member {m['login_name']}: linked {k} content(s)")
        print(f"linked {total} content(s) across all members")
    elif args.cmd.startswith("friend"):
        def _hid(name):
            row = conn.execute("SELECT id FROM handle WHERE handle_name = ?",
                               (name,)).fetchone()
            if row is None:
                raise SystemExit(f"no such handle: {name}")
            return int(row["id"])

        if args.cmd == "friends":
            rows = list_friends(conn, _hid(args.handle), status=None)
            if not rows:
                print(f"{args.handle} has an empty list")
            for r in rows:
                what = "group" if int(r["kind"]) == KIND_GROUP else "friend"
                print(f"  {r['peer_name']:<16} {what:<7} {r['status']}")
        elif args.cmd == "friend-request":
            what = request_friend(conn, _hid(args.handle), args.peer)
            print(f"{args.handle} -> {args.peer}: {what}")
            if what == "requested" and _peer_handle_row(conn, args.peer) is None:
                print(f"  note: {args.peer} is not a local handle, so nothing "
                      "was mirrored -- they will never see this request")
        elif args.cmd == "friend-accept":
            ok = accept_friend(conn, _hid(args.handle), args.peer)
            print(f"{args.handle} accepted {args.peer}" if ok else
                  f"no incoming request from {args.peer} for {args.handle}")
        elif args.cmd == "friend-decline":
            ok = decline_friend(conn, _hid(args.handle), args.peer)
            print(f"{args.handle} declined {args.peer}" if ok else
                  f"no incoming request from {args.peer} for {args.handle}")
        elif args.cmd == "friend-remove":
            remove_friend(conn, _hid(args.handle), args.peer)
            print(f"removed {args.peer} from {args.handle}'s list")
    elif args.cmd.startswith("group"):
        def _hid(name):
            row = conn.execute("SELECT id FROM handle WHERE handle_name = ?",
                               (name,)).fetchone()
            if row is None:
                raise SystemExit(f"no such handle: {name}")
            return int(row["id"])

        def _gid(owner_hid, group):
            gid = group_id(conn, owner_hid, group)
            if gid is None:
                raise SystemExit(f"no group {group!r} on handle {args.handle!r} "
                                 f"-- create it in the client, or check `friends`")
            return gid

        owner = _hid(args.handle)
        if args.cmd == "group-add":
            peer = conn.execute("SELECT id FROM handle WHERE handle_name = ?",
                                (args.member,)).fetchone()
            gid = _gid(owner, args.group)
            ok = add_group_member(conn, gid, args.member,
                                  member_handle=int(peer["id"]) if peer else None,
                                  cls=args.cls)
            if not ok:
                raise SystemExit(f"{args.group!r} already holds the client's "
                                 f"maximum of {GROUP_MEMBER_MAX} members")
            where = "local handle" if peer else "not a local handle"
            print(f"added {args.member} to {args.group} ({where}, class {args.cls})")
        elif args.cmd == "group-remove":
            remove_group_member(conn, _gid(owner, args.group), args.member)
            print(f"removed {args.member} from {args.group}")
        elif args.cmd == "group-members":
            groups = ([args.group] if args.group else
                      [r["peer_name"] for r in list_friends(conn, owner, status=None)
                       if int(r["kind"]) == KIND_GROUP])
            if not groups:
                print(f"{args.handle} owns no groups")
            for g in groups:
                gid = _gid(owner, g)
                mems = list_group_members(conn, gid, include_pending=True)
                pend = {r["member_name"] for r in conn.execute(
                    "SELECT member_name FROM group_member WHERE group_id = ?"
                    " AND pending = 1", (int(gid),))}
                print(f"  {g} -- {len(mems)}/{GROUP_MEMBER_MAX} member(s)")
                for guid, nm, cls in mems:
                    print(f"      {nm:<16} class {cls}  guid 0x{guid:x}"
                          + ("  PENDING (invited, not accepted)"
                             if nm in pend else ""))
    return 0


if __name__ == "__main__":
    if os.environ.get("POL_ACCOUNTS_SELFTEST") == "1":
        # Self-check against an in-memory DB: the full shape this module claims.
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _migrate(c)
        create_polid(c, "TESTPOLID", "pw-polid", area_kbn="00", login_pf="01")
        mid = add_member(c, "TESTPOLID", "testmember", "pw-member")
        set_handle(c, mid, "Tarutaru")
        grant_content(c, mid, 1, "CID-0001")
        grant_content(c, mid, 2)
        assert verify_member(c, "testmember", "pw-member")["id"] == mid
        assert verify_member(c, "testmember", "wrong") is None

        # --- THE ucs LOGIN FIELD SAYS "PlayOnline ID", SO ONE MUST WORK -------
        # An account whose login name is the client's scrambled NICK (member 1 on
        # the live DB: POL ID EFGH5678, login name UH5GRSV86) could otherwise only
        # be reached by a value its owner has never been shown.
        assert verify_member(c, "TESTPOLID", "pw-member")["id"] == mid, \
            "the POL ID must satisfy a field labelled PlayOnline ID"
        assert verify_member(c, "TESTPOLID", "wrong") is None, \
            "the POL ID is an identifier, not a way past the password"
        assert member_by_polid(c, "TESTPOLID")["id"] == mid
        assert member_by_polid(c, "NOSUCHID") is None
        # A handle is public -- it must NOT be usable as a credential here.
        assert verify_member(c, "Tarutaru", "pw-member") is None, \
            "a handle must not be accepted by the account servlet"
        # Sub-accounts: the bare ID means the FIRST member under it, and each
        # member's own login name still names that member exactly.
        mid2 = add_member(c, "TESTPOLID", "secondmember", "pw-second")
        assert member_by_polid(c, "TESTPOLID")["id"] == mid, \
            "the bare POL ID resolves to the primary member"
        assert verify_member(c, "secondmember", "pw-second")["id"] == mid2
        assert verify_member(c, "TESTPOLID", "pw-second") is None, \
            "a sub-account password must not authenticate via the bare ID"

        # --- WHO IS ASKING ----------------------------------------------------
        # There is no path from an HTTP request to a lobby session (see
        # sole_online_member), so the only honest answer is "the only person who
        # could be" -- one member online, or nothing.
        def _polid_of(row):
            return row["polid"] if row is not None else None

        assert sole_online_member(c) is None, "nobody online -> no guess"
        open_session(c, mid, nick="testmember", peer_ip="192.0.2.9")
        assert _polid_of(sole_online_member(c)) == "TESTPOLID"
        # A SECOND session for the same member is still one answer...
        open_session(c, mid, nick="testmember", peer_ip="192.0.2.9")
        assert _polid_of(sole_online_member(c)) == "TESTPOLID", \
            "two sessions of ONE member are not an ambiguity"
        # ...but a second MEMBER is, and must silence it rather than pick --
        # EVEN under the same POL ID, which is two people, not one answer. A
        # `DISTINCT polid` query collapsed them and answered TESTPOLID.
        open_session(c, mid2, nick="secondmember", peer_ip="192.0.2.9")
        assert sole_online_member(c) is None, \
            "two members of one POL ID are still two people"
        close_sessions(c, mid)
        # A lone SUB-account has no identifier that is both recognisable and
        # resolves back to itself, so it gets nothing rather than the POL ID --
        # which would send it to the primary member's record.
        assert sole_online_member(c) is None, \
            "a sub-account must not answer with its parent POL ID"
        close_sessions(c, mid2)
        open_session(c, mid, nick="testmember", peer_ip="192.0.2.9")
        assert _polid_of(sole_online_member(c)) == "TESTPOLID"
        # peer_ip is NOT consulted: it is the Docker gateway for every client and
        # the CGI never sees it anyway. Sessions from anywhere must still answer.
        close_sessions(c, mid)
        open_session(c, mid, nick="testmember", peer_ip=None)
        assert _polid_of(sole_online_member(c)) == "TESTPOLID", \
            "identity must not depend on an address that cannot discriminate"
        # A GHOST -- a row whose client died without a clean logout -- must stop
        # speaking for someone long before the 1 h session TTL lets it expire.
        # On the live stack two 'live' rows against ONE real connection silenced
        # this entirely, which is what the freshness cap is for.
        assert sole_online_member(c, max_age_seconds=0) is None, \
            "a stale session row must not answer for the person at the client"
        # An expired row cannot answer even before purge_sessions sweeps it.
        c.execute("UPDATE session SET expires_at = '2000-01-01T00:00:00Z'")
        c.commit()
        assert sole_online_member(c) is None, \
            "an expired session must not answer"
        close_sessions(c, mid)
        del mid2

        assert content_ids(c, mid) == [1, 2]
        revoke_content(c, mid, 2)
        assert content_ids(c, mid) == [1]
        grant_content(c, mid, 2)                      # re-grant reactivates
        assert content_ids(c, mid) == [1, 2]
        assert primary_handle(c, mid) == "Tarutaru"
        assert member_by_handle(c, "Tarutaru")["id"] == mid

        # PER-HANDLE identity and profile (2026-08-12). Two handles on one
        # account must not see each other's profile -- that was the bug.
        h1 = primary_handle_row(c, mid)["id"]
        set_handle(c, mid, "Mithra", primary=False)
        h2 = c.execute("SELECT id FROM handle WHERE handle_name = 'Mithra'"
                       ).fetchone()["id"]
        assert h1 != h2
        for h in (h1, h2):                      # guids round-trip, both ways
            assert handle_by_guid(c, handle_guid(h))["id"] == h
            assert handle_guid(h) < (1 << 44)   # or the client truncates it
        assert handle_by_guid(c, 0) is None                  # "my own handle"
        assert handle_by_guid(c, 0x5400000000000000 | h1) is None   # not ours
        set_handle_profile(c, h1, {3: 27, 19: 2439})
        set_handle_profile(c, h2, {3: 41})
        assert get_handle_profile(c, h1) == {3: 27, 19: 2439}
        assert get_handle_profile(c, h2) == {3: 41}
        # a local friend is named by the peer's real guid, so opening their
        # profile resolves to THEM rather than falling back to the viewer
        add_friend(c, h1, "Mithra", peer_handle=h2)
        fr = c.execute("SELECT peer_guid FROM friend WHERE peer_name='Mithra'"
                       ).fetchone()
        assert fr["peer_guid"] == handle_guid(h2)
        assert handle_by_guid(c, fr["peer_guid"])["handle_name"] == "Mithra"

        # SELF-ADD GUARD (2026-08-21, tm-member-sidebar-identity). Naming your
        # OWN active handle is refused with 'self' and writes no row; a DIFFERENT
        # handle on the same account is still friendable (the Mithra case below).
        assert request_friend(c, h1, "Tarutaru") == "self", \
            "a handle must not be able to friend itself"
        assert "Tarutaru" not in [r["peer_name"] for r in
                                  list_friends(c, h1, status=None)], \
            "a refused self-add must not create a friend row"
        assert request_friend(c, h2, "Mithra") == "self"
        import os as _os
        _os.environ["POL_FRIEND_NO_SELF"] = "0"
        assert request_friend(c, h1, "Tarutaru") == "requested", \
            "POL_FRIEND_NO_SELF=0 must restore the old (unguarded) behaviour"
        remove_friend(c, h1, "Tarutaru")
        del _os.environ["POL_FRIEND_NO_SELF"]

        # THE FRIENDSHIP LOOP (2026-08-12). Before this, a request was written on
        # the asker's handle only and could never become active -- these assert
        # the two halves that were missing: the mirror, and the transition.
        remove_friend(c, h1, "Mithra")            # start from a clean pair
        assert request_friend(c, h1, "Mithra") == "requested"
        # the asker holds an OUTGOING row...
        assert [(r["peer_name"], r["status"]) for r in
                list_friends(c, h1, status=None)] == [("Mithra", STATUS_PENDING)]
        # ...and the mirror put an INCOMING one on the person being asked, which
        # is the part that was absent and made every request invisible.
        assert [(r["peer_name"], r["status"]) for r in
                list_friends(c, h2, status=None)] == [("Tarutaru", STATUS_INVITED)]
        assert [r["peer_name"] for r in pending_requests(c, h2)] == ["Tarutaru"]
        assert pending_requests(c, h1) == []      # my own ask is not my inbox

        # only the INVITED side can accept
        assert accept_friend(c, h1, "Mithra") is False
        assert accept_friend(c, h2, "Tarutaru") is True

        # ACCEPTANCE MOVES BOTH ROWS AT ONCE -- the default since 2026-08-16 and
        # what SE's own service does. The reasoning that retired the two-step
        # version, and the three measurements behind it, live on `accept_friend`;
        # this asserts the outcome, and the knob below still covers the old shape.
        for h, peer in ((h2, "Tarutaru"), (h1, "Mithra")):
            assert [(r["peer_name"], r["status"]) for r in
                    list_friends(c, h, status=None)] == [(peer, STATUS_ACTIVE)]

        # so the asker's client naming them again is inert rather than a second
        # request -- there is no pending row left for it to close.
        assert request_friend(c, h1, "Mithra") == "exists"

        # POL_FRIEND_ACCEPT_BOTH=0 RESTORES THE WAIT-FOR-THE-CLIENT BEHAVIOUR,
        # and it is asserted here because the knob is the only route back to that
        # branch: nothing else in this file takes it, so without this the escape
        # hatch would rot untested and be worthless the day it is needed.
        os.environ["POL_FRIEND_ACCEPT_BOTH"] = "0"
        try:
            remove_friend(c, h1, "Mithra")        # back to a clean pair -- BOTH
            remove_friend(c, h2, "Tarutaru")      # sides now, delete is one-sided
            assert request_friend(c, h1, "Mithra") == "requested"
            assert accept_friend(c, h2, "Tarutaru") is True
            # the accepter goes active immediately; the ASKER stays pending
            assert [(r["peer_name"], r["status"]) for r in
                    list_friends(c, h2, status=None)] == [("Tarutaru", STATUS_ACTIVE)]
            assert [(r["peer_name"], r["status"]) for r in
                    list_friends(c, h1, status=None)] == [("Mithra", STATUS_PENDING)]
            # ...until their own client names them again, which closes the loop
            # rather than opening a second request.
            assert request_friend(c, h1, "Mithra") == "accepted"
            for h, peer in ((h1, "Mithra"), (h2, "Tarutaru")):
                assert [(r["peer_name"], r["status"]) for r in
                        list_friends(c, h, status=None)] == [(peer, STATUS_ACTIVE)]
        finally:
            os.environ.pop("POL_FRIEND_ACCEPT_BOTH", None)

        # once both sides are active, re-naming is inert either way
        assert request_friend(c, h1, "Mithra") == "exists"

        # the WHOLE-LIST PUT path (lobby 2:6) drives the same loop: a name the
        # server has not seen is a request, and a name sitting as 'invited' is an
        # acceptance. This is the shape `_capture_friend_put` hands us.
        set_handle(c, mid, "Galka", primary=False)
        h3 = c.execute("SELECT id FROM handle WHERE handle_name = 'Galka'"
                       ).fetchone()["id"]
        add, acc, upd, rem, kept, kgrp = replace_friends(
            c, h3, [{"name": "Mithra", "kind": KIND_FRIEND}])
        assert (add, acc, rem, kept) == (1, 0, 0, []), (add, acc, upd, rem, kept)
        assert [(r["peer_name"], r["status"]) for r in
                list_friends(c, h3, status=None)] == [("Mithra", STATUS_PENDING)]
        # Mithra's client now PUTs its whole list back, Galka included -> accept
        mithra_list = [{"name": r["peer_name"], "kind": int(r["kind"])}
                       for r in list_friends(c, h2, status=None)]
        assert {e["name"] for e in mithra_list} == {"Tarutaru", "Galka"}
        add, acc, upd, rem, kept, kgrp = replace_friends(c, h2, mithra_list)
        assert (add, acc, rem, kept) == (0, 1, 0, []), (add, acc, upd, rem, kept)
        # ...which makes BOTH sides active, exactly as the direct accept above
        # does. The 2:6 path drives the same loop, so it inherits whatever
        # asymmetry `accept_friend` has -- which is now none by default.
        h2_rows = [(r["peer_name"], r["status"]) for r in
                   list_friends(c, h2, status=None)]
        assert ("Galka", STATUS_ACTIVE) in h2_rows, h2_rows
        assert {(r["peer_name"], r["status"]) for r in
                list_friends(c, h3, status=None)} == {("Mithra", STATUS_ACTIVE)}
        # Galka's client PUTting its list back in turn is then inert, rather
        # than the step that used to close the loop.
        add, acc, upd, rem, kept, kgrp = replace_friends(
            c, h3, [{"name": "Mithra", "kind": KIND_FRIEND}])
        assert (add, acc, rem) == (0, 0, 0), (add, acc, upd, rem, kept)
        assert {(r["peer_name"], r["status"]) for r in
                list_friends(c, h3, status=None)} == {("Mithra", STATUS_ACTIVE)}

        # A 2:6 NAMES ONE ENTRY, so an omitted row is NOT a delete. Measured
        # 2026-08-13: every captured write is paylen 480 = one record, including
        # one sent while the account held two friends. Treating omission as a
        # delete wiped a real friendship twice before this was understood.
        assert request_friend(c, h1, "Galka") == "requested"      # -> h3 invited
        add, acc, upd, rem, kept, kgrp = replace_friends(
            c, h3, [{"name": "Mithra", "kind": KIND_FRIEND}])     # omits Tarutaru
        assert rem == 0 and set(kept) == {"Tarutaru"}, (rem, kept)
        assert [r["peer_name"] for r in pending_requests(c, h3)] == ["Tarutaru"]
        assert {r["peer_name"] for r in list_friends(c, h3, status=None)} \
            == {"Mithra", "Tarutaru"}                 # nothing was lost

        # WHOLE-LIST semantics are still available for the day a genuine
        # multi-record write is captured, and then an omission does delete.
        os.environ["POL_FRIEND_PUT_WHOLE_LIST"] = "1"
        os.environ["POL_FRIEND_PUT_DECLINE"] = "1"
        try:
            add, acc, upd, rem, kept, kgrp = replace_friends(
                c, h3, [{"name": "Mithra", "kind": KIND_FRIEND}])
            assert (rem, kept) == (1, []), (rem, kept)
        finally:
            del os.environ["POL_FRIEND_PUT_DECLINE"]
            del os.environ["POL_FRIEND_PUT_WHOLE_LIST"]
        assert pending_requests(c, h3) == []
        remove_friend(c, h1, "Galka")

        # a DECLINE drops my row and leaves the asker's alone -- deliberately not
        # a cascading delete, since only the list's owner may rewrite it
        assert request_friend(c, h1, "Galka") == "requested"
        assert decline_friend(c, h3, "Tarutaru") is True
        assert [r["peer_name"] for r in list_friends(c, h3, status=None)] \
            == ["Mithra"]
        assert ("Galka", STATUS_PENDING) in [(r["peer_name"], r["status"])
                                             for r in list_friends(c, h1, status=None)]
        # and a non-local name still stores the outgoing row, mirroring nothing
        assert request_friend(c, h1, "NotAnAccount") == "requested"
        assert _peer_handle_row(c, "NotAnAccount") is None
        for h in (h1, h2, h3):                    # tidy up for later asserts
            for r in list_friends(c, h, status=None):
                remove_friend(c, h, r["peer_name"])
        add_friend(c, h1, "Mithra", peer_handle=h2)

        assert ucs_params(c, "TESTPOLID") == ("00", "01", "00")
        tok = open_session(c, mid, nick="Tarutaru", iv=b"\x01" * 8)
        assert get_session(c, tok)["member_id"] == mid
        # suspended parent POL ID fails login even with the right password
        c.execute("UPDATE polid SET status='jail' WHERE polid='TESTPOLID'")
        assert verify_member(c, "testmember", "pw-member") is None
        c.execute("UPDATE polid SET status='active' WHERE polid='TESTPOLID'")
        # permissive provisioning creates the whole chain for an unknown nick
        row = ensure_member(c, "BrandNewNick")
        assert row is not None and content_ids(c, row["id"]) == [1, 2, 4, 11, 14]
        assert ensure_member(c, "BrandNewNick")["id"] == row["id"]   # idempotent
        # --- PlayOnline Mail storage -----------------------------------------
        addr = assign_mail_address(c, mid, "tarutaru")
        assert addr == "tarutaru@pol.com"
        assert mail_box_name("Tarutaru@POL.com") == "tarutaru"
        assert member_by_mail(c, "tarutaru")["id"] == mid
        assert member_by_mail(c, "tarutaru@pol.com")["id"] == mid
        u1 = deliver_mail(c, addr, b"Subject: one\r\n\r\nhi\r\n")
        u2 = deliver_mail(c, "tarutaru", b"Subject: two\r\n\r\nhi\r\n")
        assert u1 != u2 and len(list_mail(c, addr)) == 2      # same box, both ways
        assert deliver_mail(c, addr, b"Subject: one\r\n\r\nhi\r\n") == u1
        assert len(list_mail(c, addr)) == 2                   # redelivery is a no-op
        assert list_mail(c, addr)[0]["member_id"] == mid
        delete_mail(c, [list_mail(c, addr)[0]["id"]])
        assert len(list_mail(c, addr)) == 1
        assert len(list_mail(c, addr, include_deleted=True)) == 2
        set_mail_password(c, mid, "mailpw")
        row = c.execute("SELECT * FROM member WHERE id=?", (mid,)).fetchone()
        assert row["mail_pw_plain"] == "mailpw"               # APOP needs it
        assert check_password("mailpw", row["mail_pw_hash"], row["mail_pw_salt"])
        set_mail_password(c, mid, "mailpw", store_plain=False)
        assert c.execute("SELECT mail_pw_plain FROM member WHERE id=?",
                         (mid,)).fetchone()[0] is None
        # a mail name containing LIKE's wildcards must not match another box
        assign_mail_address(c, mid, "a_b")
        m2 = add_member(c, "TESTPOLID", "othermember", "pw-member2")
        assign_mail_address(c, m2, "axb")
        assert member_by_mail(c, "a_b")["id"] == mid
        assert member_by_mail(c, "axb")["id"] == m2

        # --- ATOMIC REGISTRATION ---------------------------------------------
        # The whole account, or none of it. Before register_account existed the
        # sign-up page committed the POL ID and member before the handle, so a
        # taken handle left an orphan account behind and showed a 500.
        acct = register_account(c, "Freshly", "password1", contents=(1, 2))
        assert primary_handle(c, acct["member_id"]) == "Freshly"
        assert content_ids(c, acct["member_id"]) == [1, 2]
        assert acct["mail"].endswith("@pol.com")
        # --- THE CONTENT ID IS ALLOCATED, NOT COMPUTED (2026-08-23) ----------
        # `tools/content_id_check.py` is the full suite for the mint; these are
        # the two properties the REGISTRATION path itself owns, pinned where a
        # change to registration would break them. SE-shaped means 8 digits in
        # the tens of millions -- never the old `1000000000 + member.id * 100 +
        # content_code`, which a real SE id proved wrong in kind.
        reg_cids = [r["content_id"] for r in c.execute(
            "SELECT hc.content_id FROM handle_content hc"
            " JOIN handle h ON h.id = hc.handle_id WHERE h.member_id = ?",
            (acct["member_id"],))]
        # SLOT 0 is "the" id for a title; FFXI additionally gets one id per
        # CHARACTER (FFXI_CHARACTER_SLOTS), so the row count is no longer the
        # title count. Both halves of the original property still hold and both
        # are still checked: one identity per title, and every id distinct --
        # which is the invariant that actually matters, since a Content ID
        # belongs to exactly one handle (POL-7169/7187/5326).
        reg_primary = [r["content_id"] for r in c.execute(
            "SELECT hc.content_id FROM handle_content hc"
            " JOIN handle h ON h.id = hc.handle_id WHERE h.member_id = ?"
            "   AND hc.slot = 0",
            (acct["member_id"],))]
        assert len(reg_primary) == 2 and len(set(reg_primary)) == 2, (
            f"two contents must produce two distinct Content IDs, {reg_primary}")
        assert len(set(reg_cids)) == len(reg_cids), (
            f"every Content ID a registration mints must be distinct, {reg_cids}")
        for _cid in reg_cids:
            _n = content_id_int(_cid)
            assert _n is not None and (
                CONTENT_ID_FLOOR <= _n <= CONTENT_ID_CEILING), (
                    f"registration minted {_cid!r}, not an SE-shaped id")
            assert len(str(_cid)) == 8, (
                f"an SE Content ID is 8 digits; {_cid!r} is {len(str(_cid))}")
            assert handle_by_content_id(
                c, _cid)["member_id"] == acct["member_id"], (
                    f"{_cid!r} must resolve to the handle it was linked to")
        # The ISSUED ID must be enterable in the client's Add Member field --
        # capitals and digits only, 8 of them. The `00-0000002` this used to mint
        # could not be typed at all, so registration "succeeded" and then could
        # not be used. See mint_polid.
        assert check_polid_policy(acct["polid"]) is None, acct["polid"]
        assert len(acct["polid"]) == 8, acct["polid"]
        assert get_member(c, acct["polid"]) is not None, \
            "the issued ID is the member's login name"
        # AND the account must be reachable by the nick the client will send --
        # the whole point. member_by_alias is the auth path's third lookup.
        if polnick is not None:
            issued_nick = polnick.nick_for_polid(acct["polid"])
            assert issued_nick != acct["polid"], "the nick is the scrambled form"
            assert member_by_alias(c, issued_nick)["id"] == acct["member_id"], \
                "a registered account must resolve from its login nick"
            assert polnick.polid_for_nick(issued_nick) == acct["polid"], \
                "the issued ID must survive the nick round trip"

        # --- REISSUING AN ID -------------------------------------------------
        # The repair for an account that already holds an un-typeable ID: keep
        # the member, handle, grants and mail, change only the ID.
        old = acct["polid"]
        new = reissue_polid(c, old, "REPAIRD9")
        assert new == "REPAIRD9"
        assert c.execute("SELECT 1 FROM polid WHERE polid = ?", (old,)).fetchone() is None
        assert primary_handle(c, acct["member_id"]) == "Freshly"
        assert content_ids(c, acct["member_id"]) == [1, 2]
        assert get_member(c, "REPAIRD9")["id"] == acct["member_id"], \
            "login_name follows the reissued ID"
        if polnick is not None:
            assert member_by_alias(c, polnick.nick_for_polid("REPAIRD9")) \
                is not None, "the nick binding follows the reissued ID"
            assert member_by_alias(c, issued_nick) is None, \
                "the OLD ID's nick must stop resolving after a reissue"
        try:
            reissue_polid(c, "REPAIRD9", "no-dashes-here")
            raise AssertionError("an un-typeable ID should have been refused")
        except ValueError:
            pass
        before = c.execute("SELECT COUNT(*) FROM polid").fetchone()[0]
        try:
            register_account(c, "Freshly", "password1")     # handle collision
            raise AssertionError("duplicate handle should have been refused")
        except RegistrationError:
            pass
        assert c.execute("SELECT COUNT(*) FROM polid").fetchone()[0] == before, \
            "a refused registration left a POL ID behind"
        try:
            register_account(c, "Fine", "short")            # password policy
            raise AssertionError("bad password should have been refused")
        except RegistrationError:
            pass
        assert c.execute("SELECT COUNT(*) FROM polid").fetchone()[0] == before

        # --- SYMBOLS IN PASSWORDS --------------------------------------------
        # The old policy copied SE's "alphanumeric only" page copy, but nothing
        # in the client enforces it (daAsm1 fields pass symbols through), so
        # printable-ASCII symbols are legal end to end: registerable AND
        # re-enterable at the servlet login. Invisible characters stay refused.
        sym = register_account(c, "Symby", "p@ss!w0rd#1")
        assert verify_member(c, sym["polid"], "p@ss!w0rd#1")["id"] \
            == sym["member_id"], "a symbol password must verify after signup"
        assert verify_member(c, sym["polid"], "p@ss!w0rd#2") is None
        for bad_pw in ("pass word1", "pass\tword1", "pässword1"):
            try:
                register_account(c, "Spacey", bad_pw)
                raise AssertionError(f"{bad_pw!r} should have been refused")
            except RegistrationError:
                pass

        # --- OPERATOR PASSWORD CHANGE ----------------------------------------
        # set_account_password takes whatever the operator has to hand. The
        # panel lists POL IDs, which is precisely the identifier
        # set_member_password does NOT accept, so all three forms are asserted.
        assert set_account_password(c, sym["polid"], "newp@ss1") is not None
        assert verify_member(c, sym["polid"], "newp@ss1")["id"] == sym["member_id"]
        assert verify_member(c, sym["polid"], "p@ss!w0rd#1") is None
        login = get_member(c, sym["polid"]) or member_by_polid(c, sym["polid"])
        assert set_account_password(c, login["login_name"], "byname12") is not None
        assert verify_member(c, sym["polid"], "byname12") is not None
        assert set_account_password(c, "NOSUCHACCOUNT", "whatever1") is None
        try:
            set_account_password(c, sym["polid"], "short")
            raise AssertionError("a policy-failing password must be refused")
        except RegistrationError:
            pass
        # It must not verify against a STALE hash on either row: verify_member
        # reads member, but polid carries its own copy and reissue_polid moves
        # it forward, so a change that touched only one would resurface later.
        pair = c.execute(
            "SELECT m.pw_hash AS m, p.pw_hash AS p FROM member m"
            " JOIN polid p ON p.polid = m.polid WHERE m.id = ?",
            (sym["member_id"],)).fetchone()
        assert pair["m"] == pair["p"], "member and polid hashes drifted apart"
        set_member_password(c, login["login_name"], "viakinou1")
        pair = c.execute(
            "SELECT m.pw_hash AS m, p.pw_hash AS p FROM member m"
            " JOIN polid p ON p.polid = m.polid WHERE m.id = ?",
            (sym["member_id"],)).fetchone()
        assert pair["m"] == pair["p"], "kinou-17 path left the polid hash stale"

        # A password change must NOT disturb the lobby credential -- they are
        # separate keys, and quietly clearing the token on a password change
        # would turn every reset into "the next client to connect is trusted".
        set_login_token(c, sym["member_id"], "3ruEhHBvmtg")
        set_account_password(c, sym["polid"], "another1")
        assert get_login_token(c, sym["member_id"]) == "3ruEhHBvmtg"
        assert clear_login_token(c, sym["member_id"]) == 1
        assert get_login_token(c, sym["member_id"]) is None

        # a non-local friend's synthesised guid is STABLE (it used to come from
        # hash(), which is seed-randomised per process) and fits the 44 bits the
        # client keeps
        remove_friend(c, h1, "Somebody")
        add_friend(c, h1, "Somebody")
        g1 = c.execute("SELECT peer_guid FROM friend WHERE peer_name='Somebody'"
                       ).fetchone()["peer_guid"]
        remove_friend(c, h1, "Somebody")
        add_friend(c, h1, "Somebody")
        g2 = c.execute("SELECT peer_guid FROM friend WHERE peer_name='Somebody'"
                       ).fetchone()["peer_guid"]
        assert g1 == g2 and 0 < g1 < (1 << 44), (g1, g2)

        # --- GROUP MEMBERSHIP -------------------------------------------------
        # Groups used to render named but EMPTY: nothing stored who was in one,
        # so the 07:12 count block carried an invented count (the owner, repeated).
        gid = add_friend(c, h1, "FoxGoons", kind=KIND_GROUP, guid=0)
        assert group_id(c, h1, "FoxGoons") == gid
        assert group_id(c, h1, "NoSuchGroup") is None
        assert count_group_members(c, gid) == 0

        # a LOCAL member resolves to that handle's real guid, not a synthetic one
        assert add_group_member(c, gid, "Tarutaru", member_handle=h1)
        assert list_group_members(c, gid) == [(handle_guid(h1), "Tarutaru",
                                               GROUP_CLASS_MEMBER)]
        # ... and it is derived LIVE, so a stale stored value cannot drift
        c.execute("UPDATE group_member SET member_guid = 999 WHERE group_id = ?",
                  (gid,))
        assert list_group_members(c, gid)[0][0] == handle_guid(h1)

        # a NON-local member keeps the same stable digest guid a friend gets --
        # polcore matches members by guid, so the two must never disagree
        assert add_group_member(c, gid, "Somebody")
        assert dict((n, g) for g, n, _ in list_group_members(c, gid))["Somebody"] \
            == _peer_guid(None, "Somebody") == g1

        # re-adding is an UPDATE, not a second row
        assert add_group_member(c, gid, "Somebody", cls=4)
        assert count_group_members(c, gid) == 2
        assert dict((n, k) for _, n, k in list_group_members(c, gid))["Somebody"] == 4

        # PENDING: an invite is hidden from the default listing, visible with
        # include_pending, cleared by confirm -- and a REPEATED invite can never
        # re-pend an accepted member (min() in the upsert)
        assert add_group_member(c, gid, "Invited", pending=1)
        assert "Invited" not in {n for _, n, _k in list_group_members(c, gid)}
        assert "Invited" in {n for _, n, _k in list_group_members(
            c, gid, include_pending=True)}
        assert confirm_group_member(c, gid, "Invited")
        assert not confirm_group_member(c, gid, "Invited")   # already cleared
        assert "Invited" in {n for _, n, _k in list_group_members(c, gid)}
        assert add_group_member(c, gid, "Invited", pending=1)  # repeat invite
        assert "Invited" in {n for _, n, _k in list_group_members(c, gid)}
        remove_group_member(c, gid, "Invited")

        # the class range is polcore's, and it is enforced rather than clamped:
        # 0/1/6/7 are REJECTED by the client and a group whose members are all
        # rejected disappears from the list entirely
        for bad in (0, 1, 6, 7):
            try:
                add_group_member(c, gid, "Bad", cls=bad)
                raise AssertionError(f"class {bad} should have been refused")
            except ValueError:
                pass

        # THE 64-MEMBER CEILING IS THE CLIENT'S. Overfilling is not a harmless
        # overflow: the count-block byte would exceed 0x40 and the client rejects
        # the whole reply with POL-5133, losing every group rather than one member.
        for i in range(GROUP_MEMBER_MAX):
            add_group_member(c, gid, f"Filler{i}")
        assert count_group_members(c, gid) == GROUP_MEMBER_MAX
        assert add_group_member(c, gid, "OneTooMany") is False
        assert count_group_members(c, gid) == GROUP_MEMBER_MAX
        # an existing member can still be UPDATED at the ceiling
        assert add_group_member(c, gid, "Filler0", cls=5)
        # and the list is capped on the way out, so the length calculation and
        # the record loop can never disagree about how many records there are
        assert len(list_group_members(c, gid)) == GROUP_MEMBER_MAX

        remove_group_member(c, gid, "Filler0")
        assert count_group_members(c, gid) == GROUP_MEMBER_MAX - 1
        # deleting the group takes its membership with it
        remove_friend(c, h1, "FoxGoons")
        assert count_group_members(c, gid) == 0, "group_member outlived its group"
        # --- GROUP DELETE (lobby 7:2) ----------------------------------------
        # The owner's KIND_GROUP row and its membership go together; a non-owner
        # (or an id that is not a group) changes nothing and says so with None.
        _h = primary_handle_row(c, mid)["id"]
        add_friend(c, _h, "DelGroup", kind=KIND_GROUP)
        _g = group_id(c, _h, "DelGroup")
        assert _g
        add_group_member(c, _g, "othermember")
        assert count_group_members(c, _g) >= 1
        assert delete_group(c, _g, owner_handle_id=_h + 100000) is None
        assert group_id(c, _h, "DelGroup") == _g
        assert delete_group(c, _g, owner_handle_id=_h) == 1
        assert group_id(c, _h, "DelGroup") is None
        assert count_group_members(c, _g) == 0
        assert delete_group(c, _g) is None
        # --- live session FROM an address (the mail handlers' fallback) -------
        close_sessions(c, mid)
        assert not member_online_from(c, mid, "203.0.113.7")
        open_session(c, mid, nick="Tarutaru", peer_ip="203.0.113.7")
        assert member_online_from(c, mid, "203.0.113.7")
        assert not member_online_from(c, mid, "203.0.113.8")
        assert not member_online_from(c, mid, None)
        close_sessions(c, mid)
        assert not member_online_from(c, mid, "203.0.113.7")
        print("accounts.py self-test OK")
    else:
        raise SystemExit(main())
