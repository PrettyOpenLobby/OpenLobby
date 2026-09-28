"""Per-title content schemas, content codes and content profile records."""
import json
import os
import struct
import titles as _titles_mod    # for functions that keep a local named `titles`  # noqa: E402
from srvcore import log
from .deps import accounts
from srvcore import RELEASE_DEFAULTS
from . import characters, handlelists, lobbymail, pfc, profilerecord



def _member_content_id(member_id, content_code):
    """A member's stored Content ID for one game, or None if we cannot read it.

    A convenience over `accounts.member_content_id` for the authserv paths, which
    hold a member id and no connection. Never raises: a Content ID that cannot be
    read is a caller falling back to its own default, not a failed session.
    """
    if accounts is None or not member_id:
        return None
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            return accounts.member_content_id(db, member_id, content_code)
        finally:
            db.close()
    except Exception as exc:
        log("authserv", f"  content id lookup failed for member {member_id} "
                        f"({exc!r})")
        return None
_CONTENT_PHEAD = 104

#: KEY: **THE CONTENT PROFILE SCHEMA SHIPS WITH THE CONTENT, NOT WITH THE VIEWER.**
#:
#: Measured two ways. (1) None of the FFXI field names (`z_worldn`, `z_countr`,
#: `z_zoneid`, `z_raceid`, ...) appear in `app.dll`, `polcore.dll` or `TM.dll`,
#: unpacked -- while the GENERIC `z_ctid`/`z_ctsid` do (app.dll +0x32D5A0/A8). So
#: the per-content descriptor array the parser walks is not compiled into the
#: Viewer. (2) TM's field set and its section furniture (`CONTENTS SELECT`,
#: `Privacy Level`, `You can view only Tetra Master profiles while in Tetra
#: Master.`) live in `TetraMaster/data/Friend.BIN`, in the TITLE's install
#: (LZSS-compressed).
#:
#: Consequences, and they decide what is worth serving:
#:   * A content with no PC install has no schema on the machine at all --
#:     code 3 is PS2-only, which is a concrete reason its click reports
#:     incompatible content rather than rendering.
#:   * FFXI is the ONLY content whose wire descriptor we have measured end to
#:     end (the `profSchema` probe, polcore RVA 0x20DA0 -- `_CONTENT_SCHEMA`).
#:
#: WARNING: **DO NOT INVENT A SCHEMA FOR A CONTENT WE HAVE NOT MEASURED.** The record
#: is packed POSITIONALLY, so a wrong length anywhere shifts every field after
#: it and the client renders one column's bytes as another's -- the exact defect
#: `verify_profile.py` exists to catch on the handle record. Note also that
#: FFXI's `z_phead` (104) does NOT equal `1 + sum(len + 1)` over its 12 fields
#: (101), so the used length cannot be derived from the field list either: it is
#: per-content DATA, and it has to be read off the client, not computed.
#:
#: Recovering one is one click with `probes=1`: the `[prof] parser` lines dump
#: `[a1+0x10]` (the memcpy length), `[a1+0x1B]` (the field count) and every
#: descriptor's name/length/type for whatever content is opened.
#:
#: VERIFIED: **EVERY CONTENT'S SCHEMA, DECODED FROM THE VIEWER'S OWN SHIPPED DATA
#: (2026-08-24).** `PlayOnlineViewer/data/db/prof_<NNN>.pib` IS the descriptor
#: table, one file per CONTENT CODE, and it is plaintext -- no probe needed.
#: Header: `+0x00` name, `+0x10` u32 RECORD LENGTH, `+0x18` u16 content code,
#: `+0x1B` u8 field count (the client's own `[a1+0x1B]`), `+0x1C` u32 offset to
#: the descriptors. Descriptors are the same **0x18 stride** the parser walks:
#: 16-byte inline NAME, u16 STEP (= length + 1, the visibility byte + value),
#: u16 TYPE, u32 attr.
#:
#: KEY: **POSITIVE CONTROL: the decoder reproduces FFXI's live-probed schema
#: exactly** -- `prof_001.pib` yields the same 12 fields, lengths and types as
#: `_CONTENT_SCHEMA` (recovered independently by the pol-shim `profSchema` probe
#: off a retail session) and the same 280-byte record. It also recovers the FULL
#: names the probe truncated at 8 chars: `z_worldname`, `z_countryid`,
#: `z_joblevel`.
#:
#: KEY: **RECORD LENGTH IS PER CONTENT** -- 280 (FFXI), 432 (TM), 432 (003), 280
#: (FMO), 240 (010), 464 (FE). We served 284 to all of them, which is the wrong
#: length in KIND for four of the six.
#:
#: KEY: **THE US VIEWER SHIPS ONLY `prof_001` AND `prof_002`.** The JP Viewer ships
#: 001/002/003/004/010/011. A content with no `prof_<code>.pib` on the machine
#: has NO SCHEMA THERE, which is a concrete, checkable reason a click reports
#: incompatible content -- and it means **a PC port of content 3 must ship
#: `prof_003.pfb`/`.pib`**, which already exist in the JP Viewer install.
#:
#: WARNING: TM's and 003's tails are GENERIC SLOTS (`z_attrstr`, `z_attrsi0..7`,
#: `z_attrss0..7`, ...) -- the Viewer supplies numbered attributes and the TITLE
#: names them (TM's Card Level / Title / Average Rank / Money live in
#: `TetraMaster/data/Friend.BIN`). So knowing the schema is NOT yet knowing which
#: slot carries which label; that mapping is still open.
#:
#: WARNING: `z_phead`: SE sends **104** for FFXI while `1 + sum(len + 1)` is **101**, a
#: 3-byte discrepancy nothing here explains -- though the same formula is EXACT
#: on the 424-byte handle record (`verify_profile.py` pins it). So a measured
#: value wins where we have one, and the formula is the fallback. Do not
#: "correct" FFXI's 104 to 101: 104 is what renders live.
#: (2026-09-27: 104 is 101 rounded up to 8, the offset of a 0xB0 trailer -- see
#: `_PROFILE_NOTIFY_AT`; POL_PROFILE_TRAILER=1 applies that to every title.)
#:
#: code -> (schema, record length, measured z_phead or None to compute)
_CONTENT_SCHEMAS = {
    # prof_001.pib -- FINAL FANTASY XI -- 12 fields, 280-byte record
    1: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_name', 16, 1), (4, 'z_purp', 1, 2), (5, 'z_rlang', 1, 2),
        (6, 'z_worldname', 40, 1), (7, 'z_countryid', 2, 3),
        (8, 'z_zoneid', 2, 3), (9, 'z_jobid', 2, 3), (10, 'z_joblevel', 2, 3),
        (11, 'z_raceid', 2, 3),
    ), 280, _CONTENT_PHEAD),
    # prof_002.pib -- Tetra Master -- 40 fields, 432-byte record
    2: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_name', 16, 1), (4, 'z_purp', 1, 2), (5, 'z_rlang', 1, 2),
        (6, 'z_status', 2, 3), (7, 'z_contentsno', 2, 3),
        (8, 'z_polid', 8, 0), (9, 'z_attrstr', 64, 1),
        (10, 'z_attrsl0', 8, 8), (11, 'z_attrsl1', 8, 8),
        (12, 'z_attrflt0', 4, 5), (13, 'z_attrflt1', 4, 5),
        (14, 'z_attrflt2', 4, 5), (15, 'z_attrflt3', 4, 5),
        (16, 'z_attrsi0', 4, 5), (17, 'z_attrsi1', 4, 5),
        (18, 'z_attrsi2', 4, 5), (19, 'z_attrsi3', 4, 5),
        (20, 'z_attrsi4', 4, 5), (21, 'z_attrsi5', 4, 5),
        (22, 'z_attrsi6', 4, 5), (23, 'z_attrsi7', 4, 5),
        (24, 'z_attrss0', 2, 3), (25, 'z_attrss1', 2, 3),
        (26, 'z_attrss2', 2, 3), (27, 'z_attrss3', 2, 3),
        (28, 'z_attrss4', 2, 3), (29, 'z_attrss5', 2, 3),
        (30, 'z_attrss6', 2, 3), (31, 'z_attrss7', 2, 3),
        (32, 'z_attrsb0', 2, 3), (33, 'z_attrsb1', 2, 3),
        (34, 'z_attrsb2', 2, 3), (35, 'z_attrsb3', 2, 3),
        (36, 'z_attrsb4', 2, 3), (37, 'z_attrsb5', 2, 3),
        (38, 'z_attrsb6', 2, 3), (39, 'z_attrsb7', 2, 3),
    ), 432, None),
    # prof_003.pib -- content 3 (JP-only file) -- 40 fields, 432-byte record
    3: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_name', 16, 1), (4, 'z_purp', 1, 2), (5, 'z_rlang', 1, 2),
        (6, 'z_status', 2, 3), (7, 'z_contentsno', 2, 3),
        (8, 'z_polid', 8, 0), (9, 'z_attrstr', 64, 1),
        (10, 'z_attrsl0', 8, 8), (11, 'z_attrsl1', 8, 7),
        (12, 'z_attrflt0', 4, 5), (13, 'z_attrflt1', 4, 5),
        (14, 'z_attrflt2', 4, 5), (15, 'z_attrflt3', 4, 5),
        (16, 'z_attrsi0', 4, 5), (17, 'z_attrsi1', 4, 5),
        (18, 'z_attrsi2', 4, 5), (19, 'z_attrsi3', 4, 5),
        (20, 'z_attrsi4', 4, 5), (21, 'z_attrsi5', 4, 5),
        (22, 'z_attrsi6', 4, 5), (23, 'z_attrsi7', 4, 5),
        (24, 'z_attrss0', 2, 3), (25, 'z_attrss1', 2, 3),
        (26, 'z_attrss2', 2, 3), (27, 'z_attrss3', 2, 3),
        (28, 'z_attrss4', 2, 3), (29, 'z_attrss5', 2, 3),
        (30, 'z_attrss6', 2, 3), (31, 'z_attrss7', 2, 3),
        (32, 'z_attrsb0', 2, 3), (33, 'z_attrsb1', 2, 3),
        (34, 'z_attrsb2', 2, 3), (35, 'z_attrsb3', 2, 3),
        (36, 'z_attrsb4', 2, 3), (37, 'z_attrsb5', 2, 3),
        (38, 'z_attrsb6', 2, 3), (39, 'z_attrsb7', 2, 3),
    ), 432, None),
    # prof_004.pib -- Front Mission Online -- 10 fields, 280-byte record
    4: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_rlang', 1, 2), (4, 'z_firstname', 17, 1),
        (5, 'z_lastname', 17, 1), (6, 'z_worldname', 16, 1),
        (7, 'z_countryid', 2, 3), (8, 'z_zoneid', 2, 3), (9, 'z_name', 16, 1),
    ), 280, None),
    # prof_010.pib -- FFVII: Dirge of Cerberus (JP-only) -- 9 fields, 240-byte record
    10: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_rlang', 1, 2), (4, 'z_name', 16, 1), (5, 'z_glevel', 4, 6),
        (6, 'z_class', 2, 4), (7, 'z_mvp', 4, 6), (8, 'z_rankpoint', 4, 6),
    ), 240, None),
    # WARNING: **FFXIV (8) AND EVERQUEST II (13) WERE TRIED HERE AND REMOVED.**
    # Cloning Fantasy Earth's schema DID give both a working section -- the rows
    # rendered and were selectable -- but their icon tiles
    # (`cicn_s002` tile 0, `cicn_s003` tile 1) are TRANSPARENT, so both drew as
    # blank rows. FFXIV additionally has no record at all in the client's own
    # content table (`sqpolcts.bin` jumps id 7 -> 10), so nothing can name it and
    # it had no hover title either. SE shipped no profile for either title and
    # the client has no art to draw one; serving them was inventing a section
    # for a game the Viewer cannot present. `work/pc/prof_008.*` and
    # `prof_013.*` are kept as the record of the experiment.
    # prof_011.pib -- Fantasy Earth -- 10 fields, 464-byte record
    11: ((
        (0, 'z_phead', 8, 9), (1, 'z_ctsid', 4, 6), (2, 'z_ctid', 8, 8),
        (3, 'z_name', 32, 1), (4, 'z_sex', 16, 1), (5, 'z_world', 64, 1),
        (6, 'z_nation', 64, 1), (7, 'z_class', 64, 1), (8, 'z_level', 16, 1),
        (9, 'z_rlang', 1, 2),
    ), 464, None),
}


def _content_code_for_cid(cid):
    """Which CONTENT is this Content ID? `None` when we cannot say.

    The 05:04 content request names only the id, and the schema, the record
    length and the field set all hang off the CODE -- so this lookup sits in
    front of both the reply-length decision and the record builder, rather than
    being done twice and drifting.
    """
    if not cid or accounts is None:
        return None
    try:
        n = accounts.content_id_int(cid)
        if n is None:
            return None
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                             accounts.DEFAULT_DB))
        try:
            # WARNING: NOT `handle_by_content_id` -- that returns a HANDLE row, which
            # has no `content_code` column, so the lookup silently produced None
            # for every content and every title was served FFXI's schema. Live
            # symptom, and the log said so in as many words: `code=None
            # schema=FFXI (assumed)` while serving content 3, whose record is
            # 432 bytes and 40 fields against FFXI's 280 and 12.
            # The CODE lives on the link row, so read the link row.
            row = db.execute(
                "SELECT content_code FROM handle_content"
                " WHERE content_id IS NOT NULL"
                " AND CAST(content_id AS INTEGER) = ?", (n,)).fetchone()
            if row is not None:
                return int(row["content_code"])
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"content profile: content code lookup failed ({exc!r})")
    return None


def _content_code_for_request(req_pt):
    """The content code a 05:04 content request is asking about, or None."""
    try:
        asked = profilerecord._parse_profile_tlv(req_pt) or {}
    except Exception:
        return None
    return _content_code_for_cid(asked.get(2))


def _content_game_fields(code, cid, member_id):
    """Real per-title data for a content profile: `{field_index: value}`.

    The title that serves `code` builds it (`titles.Title.profile_fields`);
    the core holds no game data of its own. ONLY WHAT THE TITLE ACTUALLY
    HOLDS: a field with no source stays UNSET, and the record builder's rule
    holds, a zero reads as "not filled in" and an invented value reads as
    fact. A title that raises leaves every field unset and is logged.
    """
    try:
        return dict(_titles_mod.profile_fields(code, cid, member_id) or {})
    except Exception as exc:
        log("lobby", f"content profile: game data for code {code} "
                     f"unavailable ({exc!r})")
        return {}


def lobby_content_ids():
    """The content codes this server offers, as the login's games menu and
    the free-contents block list them: POL_LOBBY_CONTENT_IDS, else the
    release default, else the codes of the loaded titles."""
    raw = os.environ.get("POL_LOBBY_CONTENT_IDS")
    if raw is None:
        raw = RELEASE_DEFAULTS.get("POL_LOBBY_CONTENT_IDS", "")
    ids = [int(x) for x in raw.split(",") if x.strip()]
    if ids:
        return ids
    return [t.content_code for t in _titles_mod.all() if getattr(t, "content_code", None)]


def _content_schema_for(code):
    """(schema, record_len, phead) for a content code, or FFXI's as the default.

    Returns a `measured` flag so the caller can say which of the two it did:
    serving FFXI's tail to another title is a KNOWN approximation, not a fact,
    and the log should not read as though it were one.
    """
    entry = _titles_mod.content_schema(code) if code is not None else None
    if entry is None:
        entry = _CONTENT_SCHEMAS.get(int(code)) if code is not None else None
    if entry is None:
        return lobbymail._CONTENT_SCHEMA, lobbymail._CONTENT_RECORD, _CONTENT_PHEAD, False
    schema, rec_len, phead = entry
    if phead is None:
        # The handle record proves this formula (424 = 1 + sum(len + 1), pinned
        # by verify_profile.py). FFXI's content record is the one place it does
        # not hold, and there the MEASURED value is stored instead.
        phead = 1 + sum(ln + 1 for _i, _n, ln, _t in schema)
        if profilerecord._profile_trailer_on():
            # ALIGNED TO 8 under POL_PROFILE_TRAILER -- see `_profile_trailer_on`.
            # That is what FFXI's measured 104 is (101 rounded up), and it is
            # the offset the 0xB0 trailer sits at in SE's FFXI record.
            phead = (phead + 7) & ~7
    return schema, rec_len, phead, True


def _is_content_profile_request(req_pt):
    """Is this 05:04 asking for a CONTENT profile rather than the handle's?

    THE OPCODE DOES NOT SAY. Measured on retail: both requests are `5:4`, and
    the only difference is the TLV --

        outlen=36   one item, id 2            -> the 32-field HANDLE record, 604 B
        outlen=52   two items, ids 1 and 2    -> the 12-field CONTENT record, 284 B

    and the ids are SCHEMA FIELD INDICES **of the record being asked for**, so
    id 2 means `z_hid` in one and `z_ctid` in the other. The discriminator is
    therefore the presence of id 1 (`z_ctsid`, the content/service) alongside
    id 2 -- the handle request has no id 1 at all.
    """
    if not req_pt:
        return False
    try:
        asked = profilerecord._parse_profile_tlv(req_pt) or {}
    except Exception:
        return False
    return 1 in asked and 2 in asked


def _content_profile_record(size, req_pt=None):
    """The 05:04 CONTENT profile record -- one game character, by Content ID.

    Echoes the subject the request named (`z_ctid`, `z_ctsid`) and fills
    `z_name` from the same stores the char-list slot uses, so the popup and the
    Content ID list agree about what a character is called.

    THE TAIL IS PER-CONTENT and comes from `_content_game_fields`, which is the
    ONE producer of it: FFXI's world/nation/zone/job/race, Tetra Master's card
    level/title/average rank, content 3's level/rank/money/title counts, Front
    Mission's names/nation/zone, Fantasy Earth's sex/world/nation/class/level.

    WARNING: A FIELD WITH NO SOURCE IS STILL LEFT AT ZERO ON PURPOSE, and that rule
    did not soften when the sources arrived. A zero reads as an unset field,
    which is what the client shows for a character that has not filled one in;
    a value we do not actually hold would read as fact, and this file has paid
    for that before. Dirge of Cerberus is the whole tail that is still unset --
    see the note at the end of `_content_game_fields` for exactly why.
    """
    out = bytearray(size)
    asked = {}
    if req_pt:
        try:
            asked = profilerecord._parse_profile_tlv(req_pt) or {}
        except Exception as exc:
            log("lobby", f"content profile: request TLV did not parse ({exc!r})")
    cid = asked.get(2)
    name = ""
    if cid and accounts is not None:
        try:
            # NOT `_char_display_name` -- its fallbacks (handle name, then the
            # Content ID digits) exist so a char-list SLOT is never blank, which
            # is a different requirement. In the profile RECORD a name we do not
            # have should stay unset, the way SE leaves it for a Content ID with
            # no character; digits here would be fiction on the popup.
            name = (characters._character_names() or {}).get((accounts.content_id_int(cid), 1)) or ""
            if not name:
                db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                     accounts.DEFAULT_DB))
                try:
                    row = accounts.handle_by_content_id(db, cid)
                    if row is not None:
                        name = (row["handle_name"] or "").strip()
                finally:
                    db.close()
        except Exception as exc:
            log("lobby", f"content profile: name lookup failed ({exc!r})")
    # WHICH CONTENT IS THIS? The code decides the schema (see _CONTENT_SCHEMAS),
    # and the request carries only the Content ID, so resolve one to the other.
    code = _content_code_for_cid(cid)
    schema, _rec_len, phead, measured = _content_schema_for(code)
    if not measured:
        log("lobby", f"content profile: WARNING: no measured schema for content "
                     f"code {code} -- serving FFXI's field set, which this title "
                     f"almost certainly does not share. Arm probes=1 and open it "
                     f"to capture the real one (see _CONTENT_SCHEMAS)")
    fields = {0: phead}
    fields.update(asked)               # z_ctsid / z_ctid straight back
    # THE TITLE'S OWN DATA, from whatever this server actually stores for it.
    # Applied before the client's own `<GR>` write below, so anything the player
    # set themselves still wins -- game state is a default, not an override.
    member_id = None
    if cid and accounts is not None:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                row = accounts.handle_by_content_id(db, cid)
                if row is not None:
                    member_id = int(row["member_id"])
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"content profile: member lookup failed ({exc!r})")
    game = _content_game_fields(code, cid, member_id)
    if game:
        fields.update(game)
        log("lobby", "content profile: game data " + ", ".join(
            f"field {k}={v!r}" for k, v in sorted(game.items())))
    # WHAT THE CLIENT ITSELF WROTE, where it maps onto the GENERIC head. `<AN>`
    # is the character name and `<PP>` is Purpose, and those two are schema
    # fields 3 and 4 in EVERY content profile -- so they can be served without
    # knowing this content's tail. The rest of the write is stored but not
    # served: TM's and 003's wire descriptors are unrecovered, and guessing
    # where their fields sit would put invented data on screen.
    stored = pfc._content_profiles().get(str(int(cid))) if cid else None
    if stored:
        g = {k: v for k, v in (stored.get("groups") or [])}
        an = (g.get("AN") or [""])[0]
        if an and not str(an).isdigit():
            name = an
        pp = (g.get("PP") or [None])[0]
        if pp is not None and str(pp).lstrip("-").isdigit():
            fields[4] = int(pp)
        log("lobby", f"content profile: using the client's own write for "
                     f"{cid} (name={an!r} purpose={pp})")
        # KEY: **AND THE VIEWER'S OWN 05:01, WHICH IS THE OTHER CHANNEL.** `<GR>`
        # is the GAME describing itself on the PolPro band; this is the player
        # editing the same record in the Viewer's profile screen. Both are
        # "the client's own write" and they can disagree, so the NEWER one
        # wins -- ISO-8601 stamps compare as strings. Applied after the game
        # data for the same reason `<GR>` is: what a player set themselves
        # beats what their play implies.
        lob = stored.get("fields") or {}
        if lob and str(stored.get("fields_seen") or "") >= str(stored.get("seen") or ""):
            applied = {}
            for k, v in lob.items():
                try:
                    fid = int(k)
                except (TypeError, ValueError):
                    continue
                # An empty string is "the client sent this field with nothing
                # in it", which must not blank a name we can resolve. Names
                # are re-applied below regardless; this keeps the log honest.
                if v == "":
                    continue
                fields[fid] = v
                applied[fid] = v
            if applied:
                log("lobby", f"content profile: applying the Viewer's own "
                             f"05:01 write for {cid} ({stored['fields_seen']}): "
                             + ", ".join(f"field {k}={v!r}"
                                         for k, v in sorted(applied.items())))
    if name:
        fields[3] = name
    level = int(os.environ.get("POL_PROFILE_LEVEL", "3") or 3) & 0xFF
    out[0] = level
    off = profilerecord._PROFILE_LEAD
    served = []
    for idx, fname, ln, ty in schema:
        step = 1 + ln + profilerecord._PROFILE_STEP_EXTRA.get(ty, 0)
        if off + step > size:
            log("lobby", f"content profile: {fname} would overrun {size}B; stopping")
            break
        val = fields.get(idx)
        out[off] = level if idx == 0 else 3      # SE holds fields 1.. at 3
        if val is not None:
            try:
                if ty == 1:
                    raw = str(val).encode("cp932", "replace")[:max(0, ln - 1)]
                    out[off + 1:off + 1 + ln] = raw + b"\x00" * (ln - len(raw))
                else:
                    out[off + 1:off + 1 + ln] = int(val).to_bytes(ln, "little",
                                                                  signed=False)
                served.append(fname)
            except Exception:
                pass
        off += step
    # THE 0xB0 TRAILER AT z_phead -- POL_PROFILE_TRAILER=1 only, see
    # `_PROFILE_NOTIFY_AT`. SE's FFXI record carries the OWNING HANDLE's friend
    # row there (the same bytes as that handle's own profile record at 0x1A8),
    # then the status block. A Content ID with no owning handle stays zero,
    # which is what Crystal sends for a missing header too.
    if profilerecord._profile_trailer_on() and cid and accounts is not None \
            and 0 < int(phead) and int(phead) + profilerecord._PROFILE_TRAILER_LEN <= size:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                owner = accounts.handle_by_content_id(db, cid)
                if owner is not None:
                    owner = db.execute("SELECT * FROM handle WHERE id = ?",
                                       (int(owner["id"]),)).fetchone()
                if owner is not None:
                    purp = (accounts.get_handle_profile(db, int(owner["id"]))
                            or {}).get(17)
                    trailer = (profilerecord._profile_identity_record(db, owner)
                               + profilerecord._profile_notify_status(db, owner, purp))
                    at = int(phead)
                    out[at:at + profilerecord._PROFILE_TRAILER_LEN] = trailer
                    log("lobby", f"content profile: trailer at {at:#x} = handle "
                                 f"{owner['id']}'s friend row + status "
                                 f"{trailer[-8:].hex()} (POL_PROFILE_TRAILER)")
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"content profile: trailer failed ({exc!r}); left zero")
    log("lobby", f"content profile: z_ctid={cid if cid is None else hex(int(cid))} "
                 f"code={code} schema={'measured' if measured else 'FFXI (assumed)'}"
                 f" name={name!r}, {len(served)} field(s) set ({', '.join(served)})")
    return bytes(out)


def _identity_content_entries(db, hid):
    """The 8 x 16-byte content block for handle `hid`. See `_IDREC_CONTENT_AT`.

    The z_ctsid/z_ctid pair MUST agree with what `_char_record` writes at +0x0C
    and +0x10 of the same character's 1:3 record -- SE's two carriers hold the
    same numbers, and serving two different identities for one character is the
    class of bug that made every friend row resolve to the same profile. So the
    FFXI world-identity override is applied here from the same
    `_ffxi_world_fields()` map rather than a second copy of the rule drifting
    beside it.
    """
    out = bytearray(profilerecord._IDREC_CONTENT_STRIDE * profilerecord._IDREC_CONTENT_MAX)
    try:
        links = accounts.handle_content_list(db, int(hid))
    except Exception as exc:
        log("lobby", f"profile contents: lookup failed ({exc!r}); block empty")
        return bytes(out)
    # *** +0x02 MIRRORS THE ENTRY'S OWN CONTENT CODE. ***
    #
    # It shipped as a constant 1 -- SE's only sample is FFXI, whose code IS 1, so
    # "1" and "the code" were indistinguishable there. LIVE 2026-08-24 separated
    # them: with every entry carrying +0x02 = 1 the profile screen listed FOUR
    # ENTRIES ALL LABELLED FFXI, for eight links spanning eight different codes.
    # A field that makes eight distinct contents render as one title is a content
    # code being read, not a constant, and SE's two fields agreeing is then the
    # ordinary case rather than a coincidence.
    #
    # POL_PROFILE_CONTENT_F02=<int> forces a constant again, for bisecting.
    # *** +0x00 IS A FLAGS WORD (bit 0 = PRESENT), NOT THE CONTENT CODE. ***
    #
    # SE's single sample is FFXI, which has code 1, so its `+0x00 = 1` and
    # `+0x02 = 1` cannot tell "the code" from "a present bit". Serving eight
    # contents separated them, and three live observations agree on one model:
    #
    #   +0x00 = code, +0x02 = 1 (constant) -> FOUR rows, ALL titled FFXI
    #   +0x00 = code, +0x02 = code         -> rows for codes 1, 3, 11, 15
    #   ...and after linking 13            -> EverQuest II appears too
    #
    # Every rendered row has an ODD code and no even one ever renders, while the
    # TITLE follows +0x02. So bit 0 of +0x00 gates the row and +0x02 names the
    # content -- and writing the code into +0x00 meant only odd-numbered titles
    # were ever visible. That also matches the 1:3 handle-binding byte, where
    # bit 0 is likewise "present" (the content-id launch gate).
    #
    # POL_PROFILE_CONTENT_PRESENT overrides the flags word for bisecting.
    present = int(os.environ.get("POL_PROFILE_CONTENT_PRESENT", "1") or 0)
    f02_env = os.environ.get("POL_PROFILE_CONTENT_F02", "").strip()
    f02_const = int(f02_env) if f02_env else None
    # WHICH CONTENTS TO ADVERTISE: only ones that HAVE a profile schema.
    #
    # A content profile is rendered from `prof_<code>.pib`/`.pfb` in the
    # Viewer's own data (see `_CONTENT_SCHEMAS`), and SE shipped exactly six --
    # FFXI, Tetra Master, content 3, Front Mission Online, Dirge of Cerberus and
    # Fantasy Earth. A code with no such pair, like PlayOnline FriendList (14),
    # has no schema, no labels and no icon anywhere: it was never meant to
    # appear in this list, and the account holder reported exactly that -- the
    # Friend List and the FFXI Test Server showing up as iconless rows.
    #
    # So the rule is the client's own: advertise a content iff we hold the
    # schema the client would need to render it. That is self-maintaining --
    # adding a schema to `_CONTENT_SCHEMAS` adds the row, and nothing else.
    # POL_PROFILE_CONTENT_CODES=1,2 narrows it further by hand;
    # POL_PROFILE_CONTENT_ALL=1 restores the old "everything linked" behaviour.
    raw_codes = os.environ.get("POL_PROFILE_CONTENT_CODES", "").strip()
    if raw_codes:
        allow = {int(t) for t in raw_codes.replace(" ", "").split(",") if t}
    elif os.environ.get("POL_PROFILE_CONTENT_ALL", "0") == "1":
        allow = None
    else:
        allow = set(_CONTENT_SCHEMAS)
    shown = []
    for link in links[:profilerecord._IDREC_CONTENT_MAX]:
        code = int(link["content_code"])
        if allow is not None and code not in allow:
            continue
        f02 = code if f02_const is None else f02_const
        try:
            cid = int(str(link["content_id"] or "").strip() or 0)
        except ValueError:
            cid = 0
        if not cid:
            # A link with no Content ID names no character, and an entry whose
            # ids are zero is what an EMPTY slot looks like -- so skip it rather
            # than minting a selectable row the content request cannot resolve.
            continue
        ctsid = cid
        world = _titles_mod.character_world(code, cid)
        if world is not None:
            ctsid = world
        off = len(shown) * profilerecord._IDREC_CONTENT_STRIDE
        struct.pack_into("<HHII", out, off, present & 0xFFFF, f02 & 0xFFFF,
                         ctsid & 0xFFFFFFFF, cid & 0xFFFFFFFF)
        shown.append(f"code {code} (flags {present:#x}) z_ctsid {ctsid:#010x} z_ctid {cid}")
    if shown:
        log("lobby", f"profile contents: {len(shown)} entry(ies) at idrec "
                     f"+{profilerecord._IDREC_CONTENT_AT:#04x} -- " + "; ".join(shown))
    else:
        log("lobby", "profile contents: none linked; block left empty "
                     "(no content section will be offered)")
    return bytes(out)
