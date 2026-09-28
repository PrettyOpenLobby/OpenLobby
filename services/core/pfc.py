"""Content profiles and the PFC profile reply on the auth band."""
import json
import os
import time
import titles                   # the title-plugin seam (services/titles.py)  # noqa: E402
from srvcore import log
from .deps import accounts, polpro



#: Where per-Content-ID game profiles the CLIENT wrote are kept. A plain JSON
#: file beside a title's character pool, deliberately NOT `accounts.db`: this is game data
#: rather than identity, it is written on a client's timing rather than ours,
#: and accounts.db has a documented WAL hazard under concurrent writers.
def _content_profile_file():
    root = os.environ.get("POL_RESOURCE_DIR")
    if not root:
        root = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return os.path.join(root, "content-profiles.json")


def _content_profiles():
    try:
        with open(_content_profile_file(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _content_profile_note(payload, tag):
    """`<GR>` is the client WRITING its game profile. Keep it.

    THE WRITE IS THE ONLY PLACE THIS DATA EXISTS. The per-Content-ID profile is
    the game's own state -- Tetra Master's card level, title, rank, money and
    purpose; content 3's title and purpose; FFXI's world/nation/job/race -- and
    POL cannot author any of it. Until 2026-08-23 the write could not even
    COMPLETE (TM's `<GR>` had no reply template and the client hung on
    "Updating profile..."), so nothing was ever offered to us. Now it completes,
    and discarding it would mean the profile dies with the session.

    Measured shape, live 2026-08-23:

        <GR>(0x0000000001C9C399,30000025) <AN>(LaptopTest2)
        <CI>(0,Card Level 209) <PP>(5) <NO>(6,3) <NO>(14,3) <VO>(3) <FO>(3)

    which is `sqmg_1a5e60` building from TM.dll's format literal at 0x51C62CC.
    `<PP>` is Purpose -- 5 is "Let's trade!" in the enum recovered from
    `TetraMaster/data/Friend.BIN` -- and `<NO>(i,v)` addresses the byte array the
    GET reply carries at value[41+i].

    Stored VERBATIM, as groups, not as a decoded struct. We know TM's field
    NAMES (Friend.BIN) and content 3's (its PS2 module) but NOT either game's wire
    descriptor, so a decode now would be invention; the raw groups keep every
    byte the client sent for whoever recovers those schemas. Keyed by Content ID
    because that is what the profile IS.
    """
    if polpro is None:
        return
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "GR":
            return
        vals = groups[0][1] or []
        cid = None
        if vals and str(vals[0]).lower().startswith("0x"):
            try:
                cid = int(vals[0], 16)
            except ValueError:
                cid = None
        if cid is None and len(vals) > 1 and accounts is not None:
            cid = accounts.content_id_int(vals[1])
        if not cid:
            log("authserv", f"  content profile write: no Content ID in {vals!r}"
                            f" -- not stored")
            return
        rec = {"tag": (tag.decode("latin1") if isinstance(tag, bytes) else tag),
               "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "groups": [[g, list(v)] for g, v in groups]}
        data = _content_profiles()
        key = str(cid)
        if data.get(key) == rec:
            return
        data[key] = rec
        path = _content_profile_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        named = ", ".join("<%s>" % g for g, _ in groups)
        log("authserv", f"  content profile write: STORED for Content ID {cid} "
                        f"({named})")
    except Exception as exc:
        log("authserv", f"  content profile write: not stored ({exc!r})")


def _pfc_profile_subject(cid):
    """Who is Content ID `cid`? -> (name, info, member_id) -- any may be None.

    TWO STORES, and they disagree by design. `handle_content` is the identity
    link (which handle owns this game character); a title's own character pool
    is what its client TOLD us about itself (Tetra Master's `<CR>`: its own
    character name and status line). The pool wins for the name when it has
    one: a value the client sent back is the value the client will recognise,
    and a derived one is right only until the thing it derives from moves.
    See `titles.Title.character`.
    """
    name = info = member_id = None
    try:
        rec = titles.character(cid)
        if rec:
            name, info, member_id = rec
    except Exception:
        pass
    if name:
        return name, info, member_id
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            row = accounts.handle_by_content_id(db, cid)
            if row is not None:
                name = (row["handle_name"] or "").strip() or name
        finally:
            db.close()
    except Exception:
        pass
    return name, info, member_id


def _pfc_profile_reply(payload, tag):
    """`<PG>` -> `<PO>`: the per-Content-ID GAME CHARACTER, authored not echoed.

    WHY THIS IS CODE AND NOT A polpro.json ENTRY, the same reason as the auction's
    `<SN>` one screen over: the answer is a lookup keyed by a VALUE in the request
    (the Content ID), and a spec key is a TAG. A template can only ever say one
    character's data to everybody.

    The reply vocabulary is MEASURED -- `polpro.PROFILE_PO` carries the evidence:
    71 positional values in one `<PO>` group, identical in `TM.dll` and
    the PS2 module. What is NOT measured is the game MEANING of most of those 71
    slots, so this fills only the ones the two builds' own code names:

        value[0]  the Content ID, hex     value[1]  the same, decimal
        value[2]  the character NAME      value[9]  a 64-byte status string

    and leaves the rest at the zero the client would read anyway. That is the
    "recover ONE field end to end before generalising" scope from the WS5 brief,
    and the popup showing a real NAME is the thing to look for on the first run.

    WARNING: A WRONG `<PO>` IS SILENT. The client's parser raises on nothing -- a missing
    group falls back to group 0, a missing value reads as 0 -- so a bad reply is a
    BLANK popup, never an error. The one loud failure mode is the state machine:
    if this reply is not taken, `sqMgPfcGetCharacterProfileCheck` never leaves
    state 1 and the scene waits forever, which is what `<GK>` did here for months.

    `POL_POLPRO_PG=0` restores the polpro.json template path.
    `POL_POLPRO_PG_MISS=error` answers an unknown Content ID with `<PM>` instead
    of an empty-but-well-formed `<PO>`; empty is the default because `<PM>` puts
    the client into its error arm, and we have never seen SE's choice here.

    Returns `(payload_bytes_or_None, handled)`.
    """
    if polpro is None or accounts is None:
        return None, False
    if os.environ.get("POL_POLPRO_PG", "1") != "1":
        return None, False
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "PG":
            return None, False
        vals = groups[0][1] or []
        raw_hex = vals[0] if vals else ""
        cid = None
        if raw_hex.lower().startswith("0x"):
            try:
                cid = int(raw_hex, 16)
            except ValueError:
                cid = None
        if cid is None and len(vals) > 1:
            cid = accounts.content_id_int(vals[1])
        if not cid:
            log("authserv", f"  polpro <PG>: no Content ID in {vals!r} -- falling "
                            f"through to the template")
            return None, False
        name, info, member_id = _pfc_profile_subject(cid)
        if not name:
            if os.environ.get("POL_POLPRO_PG_MISS", "empty") == "error":
                log("authserv", f"  polpro <PG>: nothing stored for Content ID "
                                f"{cid} -- answering <PM> GAME_PROFILE_NONE")
                return polpro.build_pm(), True
            log("authserv", f"  polpro <PG>: nothing stored for Content ID {cid} "
                            f"-- serving an empty <PO> (set POL_POLPRO_PG_MISS="
                            f"error for the <PM> arm)")
        fields = {0: cid, 1: cid}
        if name:
            fields[2] = name
        # *** SLOT 9 IS "AVERAGE RANK", AND cinfo IS NOT THAT. ***
        #
        # The label -> slot table decoded from `prof_002.pfb`'s own record
        # table, with FFXI as the positive control:
        #
        #     Card Level   -> 16  z_attrsi0   u32
        #     Title        -> 24  z_attrss0   u16
        #     Average Rank ->  9  z_attrstr   64-byte string
        #
        # This put the client's `<CI>` cinfo -- the literal string
        # "Card Level 209" -- into slot 9, so the in-game profile rendered
        # `Average Rank: Card Level 209` (observed live 2026-08-24) while Card
        # Level's own slot stayed empty. Right number, wrong field, and a label
        # that made it look like data rather than a misplacement.
        #
        # Card Level now goes where it belongs, and Average Rank and Title are
        # served from the career stats when the member has played -- see
        # `_content_game_fields`, which is deliberately the ONE producer for
        # both this reply and the 05:04 record so the two cannot disagree about
        # a player. `POL_POLPRO_PG_CINFO=9` restores the old placement for
        # comparison.
        #
        # WARNING: THE SLOT NUMBERS TRANSFER, BUT ONLY FROM 9 ONWARD. `<PO>`'s value
        # indices line up with the `.pib` schema indices across the generic
        # attribute tail (v9 str<=64 = z_attrstr, v16..23 u32 = z_attrsi0..7,
        # v24..31 u16 = z_attrss0..7) and NOT at the head, where v2 is the name
        # and schema field 2 is z_ctid. Everything below is in the tail.
        # THE TITLE FILLS ITS OWN SLOTS. Which values a title's profile has
        # and where they sit in the 71-value group is the title's knowledge:
        # it hands back the fields to add, plus the name and member it
        # resolved for the Content ID (either may be the ones passed in).
        _pf = titles.polpro_profile(tag, cid, name, member_id)
        if _pf is not None:
            _pfields, _pname, _pmember = _pf
            fields.update(_pfields or {})
            name = _pname or name
            if _pmember is not None:
                member_id = _pmember
        cinfo_slot = os.environ.get("POL_POLPRO_PG_CINFO", "").strip()
        if info and cinfo_slot.isdigit():
            fields[int(cinfo_slot)] = info
        reply = polpro.build_po(fields)
        log("authserv", f"  polpro <PG>: Content ID {cid} -> <PO> name={name!r} "
                        f"info={info!r} member={member_id} ({len(reply)}B)")
        return reply, True
    except Exception as exc:
        # A raise here must not cost the session: fall through and let the
        # template answer, exactly as the rankings and auction paths do.
        log("authserv", f"  polpro <PG>: reply failed ({exc!r}) -- the "
                        f"polpro.json entry still stands")
        return None, False


def _rank_list_blob(path):
    """The bytes behind a ranking path: the published tally, else the shipped
    fixture, else None.

    ONE function for both halves on purpose -- `_tm_rank_reply` counts rows with
    it and `_resource_blob` serves bytes with it, so the `<LN>` we promise and
    the file we hand over are the same file by construction.
    """
    # A title's ranking lists, and every other shipped-template path it owns,
    # come through the same hook: the published tally if its job has run, the
    # title's shipped fixture if not, None for a path nobody serves.
    return titles.resource_template(path)
