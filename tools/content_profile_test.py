"""The 05:04 CONTENT profile: same opcode as the handle profile, different record.

WHAT THIS PINS, and why it is not obvious.

05:04 returns TWO different records and the opcode does not say which. Measured
on a live retail session 2026-08-23 (`work/pc/prof-ffxi-retail-20260823.log`,
the shim's own `[pay] REQUEST` / `[snd] SENT` probes, in plaintext because the
shim sits inside the client and hooks post-decryption):

    5:4 outlen=36   one TLV item,  id 2        -> 32-field HANDLE record,  604 B
    5:4 outlen=52   two TLV items, ids 1 + 2   -> 12-field CONTENT record, 284 B

and the TLV ids are SCHEMA FIELD INDICES **of the record being asked for**, so
id 2 is `z_hid` in one request and `z_ctid` in the other. Getting that wrong is
silent in both directions: answer 604 to a content request and the client reads
320 bytes past the record, answer the handle record and it shows the wrong
subject entirely.

SE's item ids and values are carried over verbatim; see the note on framing
below, which is the one thing the capture cannot be used for as-is.
"""
import binascii
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="content-profile-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []

#: SE's own 05:04 requests, rebuilt in the framing the SERVER sees.
#:
#: WARNING: THE CAPTURE'S BYTES ARE NOT THIS FRAMING, and feeding them straight in
#: is a mistake worth documenting rather than quietly fixing. `[snd] SENT` dumps
#: the buffer the CLIENT hands its encoder -- 36 / 52 bytes beginning at the TLV
#: -- while `_parse_profile_tlv` reads a decrypted lobby payload whose items
#: start at `_TLV_START` (56). Same items, different envelope. What is carried
#: over from the capture verbatim is what matters: the item IDS and their
#: VALUES, both confirmed against the record SE returned.
#:
#: Item encoding per `_parse_profile_tlv`: u8 id, u8 junk, u16 len, u32 pad,
#: value padded to 8.
CTID, CTSID = 0x016AC1F5, 0x0A6580BD

#: KEY: **THE FIRST CONTENT-PROFILE WRITE EVER CAPTURED**, verbatim: prod
#: `lobby-profile-write-unparsed-2026-09-12T175202Z.bin`, the account holder
#: changing Janhourou's Purpose in the Viewer. 124 bytes decrypted, payload 84.
#: Keep the BYTES, not a rebuild of them -- this is the only sample of the form
#: and the reason the parser knows a narrow item is 8 bytes rather than 16.
JAN_CID = 30000002                                  # 0x01C9C382, the subject
CONTENT_WRITE = bytes.fromhex(
    "020501005400000000000000000000000000000000000000d0ec5e6f95427d48"
    "e6769bc465ff3a0a030003010000000000000000000000000403080000000000"
    "01436173000000000300020000000000020108000000000082c3c90100000000"
    "010108000000000082c3c90100000000000000000000000012cf1178")
HID = 0x511C75


def _item(fid, value):
    return (bytes([fid, 0x01, 8, 0, 0, 0, 0, 0])
            + int(value).to_bytes(8, "little"))


def _request(*items):
    """A decrypted 05:04 payload: header padding, items, checksum trailer."""
    body = b"".join(_item(i, v) for i, v in items)
    return b"\x00" * R._TLV_START + body + b"\x00" * 4


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def game_fields(handle_id, member_id):
    """`_content_game_fields` for every title that has a source, 2026-09-12.

    Each check below is a REGRESSION that has already happened once somewhere in
    this project, or the exact shape of one:

      * a value served on the slot its schema NAME suggests instead of the slot
        the label file points at (Tetra Master's Card Level rendered as the
        Average Rank string for a week);
      * a store read directly as JSON after the data moved into sqlite (both
        game stores moved on 2026-09-08 and the JSON froze as a backup, so the
        read keeps working and keeps returning migration-day values);
      * a swapped key read by name (`nation` is the GENDER byte);
      * an index passed between two ladders with different strides.
    """
    import json

    # --- FINAL FANTASY XI: the bridge's record of LSB's own char list --------
    idmap = os.path.join(TMP, "ffxi_idmap.json")
    with open(idmap, "w", encoding="utf-8") as fh:
        json.dump({"1": {"content_id": CTID, "name": "Foxffxi",
                         "world_field": 1,
                         # zone 291 is the case that matters: LSB splits the
                         # zone across zone_no and zone_no2, so a reader that
                         # takes the low byte alone shows zone 35 instead.
                         "profile": {"world": "Bahamut", "nation": 0,
                                     "zone": 291, "job": 5, "joblevel": 62,
                                     "race": 7}}}, fh)
    R._FFXI_IDMAP = idmap
    R._ffxi_world_cache.update(mtime=None, map={}, prof={})
    f = R._content_game_fields(1, CTID, member_id)
    check(f.get(R._FFXI_WORLD) == "Bahamut" and f.get(R._FFXI_JOB) == 5
          and f.get(R._FFXI_JOBLEVEL) == 62 and f.get(R._FFXI_RACE) == 7,
          "FFXI world/job/level/race come off the bridge's char-list record",
          repr(f))
    check(f.get(R._FFXI_ZONE) == 291,
          "FFXI zone keeps its 9th bit (zone_no2)", repr(f.get(R._FFXI_ZONE)))
    check(R._FFXI_NATION in f and f[R._FFXI_NATION] == 0,
          "nation 0 is San d'Oria, not 'missing' -- it must still be SET",
          repr(f.get(R._FFXI_NATION)))
    R._FFXI_IDMAP = os.path.join(TMP, "no-such-idmap.json")
    R._ffxi_world_cache.update(mtime=None, map={}, prof={})
    check(not R._content_game_fields(1, CTID, member_id),
          "no id map -> FFXI tail stays UNSET, not zeroed")

    # --- FRONT MISSION ONLINE ------------------------------------------------
    if importlib.util.find_spec("fmo") is None:
        print("[SKIP] FRONT MISSION ONLINE profile fields: the fmo title module is not present in this tree")
    else:
        db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
        # `handle_content.content_id` is UNIQUE and the mint hands out ids in the
        # tens of millions, so JAN_CID -- which is a REAL prod id, kept because the
        # captured write names it -- can collide with one this fixture just minted.
        db.execute("DELETE FROM handle_content WHERE CAST(content_id AS INTEGER) = ?",
                   (JAN_CID,))
        for code, cid in ((3, JAN_CID), (4, CTID + 4), (11, CTID + 11)):
            accounts.link_content_to_handle(db, handle_id, code, str(cid))
        db.commit()
        db.close()

        fmo_store = os.path.join(TMP, "fmo_characters.json")
        os.environ["FMO_CHAR_STORE"] = fmo_store
        os.environ["FMO_DB"] = ""              # the JSON store; no migration here
        with open(fmo_store, "w", encoding="utf-8") as fh:
            # A FEMALE O.C.U. pilot: `nation` (the swapped key) says 2, the real
            # nation byte says 1. Serving the wrong one is invisible on screen.
            json.dump({"member:%d" % member_id: [
                {"id": 1, "first": "Roy", "last": "Bagman",
                 "nation": 2, "nation_byte": 1, "gender": 2,
                 "mapkind": 207, "rank": 21}]}, fh)
        f = R._content_game_fields(4, CTID + 4, member_id)
        check(f.get(R._FMO_NAME) == "Roy" and f.get(R._FMO_FIRSTNAME) == "Roy",
              "FMO first name lands on slot 9 (the labelled one) AND slot 4",
              repr(f))
        check(f.get(R._FMO_LAST) == "Bagman", "FMO last name on slot 5", repr(f))
        check(f.get(R._FMO_COUNTRY) == 1,
              "FMO nation is the NATION byte, not the swapped `nation` key "
              "(2 there would read U.S.N. for every female pilot)",
              repr(f.get(R._FMO_COUNTRY)))
        check(f.get(R._FMO_ZONE) == 2,
              "FMO zone is the MapKind band: 207 -> 2, O.C.U. Occupation Zone",
              repr(f.get(R._FMO_ZONE)))
        with open(fmo_store, "w", encoding="utf-8") as fh:
            json.dump({"member:%d" % member_id: [
                {"id": 1, "first": "Roy", "mapkind": 999}]}, fh)
        f = R._content_game_fields(4, CTID + 4, member_id)
        check(R._FMO_ZONE not in f,
              "a MapKind outside the client's own bands leaves the zone UNSET")

    # --- FANTASY EARTH -------------------------------------------------------
    if importlib.util.find_spec("felobby") is None:
        print("[SKIP] FANTASY EARTH profile fields: the felobby title module is not present in this tree")
    else:
        import felobby
        fe_store = os.path.join(TMP, "fe_characters.json")
        felobby._default_store = lambda: fe_store
        fe_account = "member:%d" % member_id
        # WARNING: `charid` is NOT decoration: festore's PRIMARY KEY is (account, charid)
        # and a row without one is refused on import, which shows up here as an
        # empty roster rather than an error.
        with open(fe_store, "w", encoding="utf-8") as fh:
            json.dump({"accounts": {fe_account: [
                # look1 IS the class index -- feworld.self_class_id. 2 = Sorcerer.
                {"charid": 1, "name": "Elle", "sex": 1, "force": 3, "look1": 2,
                 "class_levels": {"2": 27}}]}}, fh)
        f = R._content_game_fields(11, CTID + 11, member_id)
        check(f.get(R._FE_NAME) == "Elle" and f.get(R._FE_SEX) == "Female"
              and f.get(R._FE_NATION) == "Elsord",
              "FE name/sex/nation still served after the move to felobby's loader",
              repr(f))
        check(f.get(R._FE_CLASS) == "Sorcerer",
              "FE class is look1 through _FE_CLASSES", repr(f.get(R._FE_CLASS)))
        check(f.get(R._FE_LEVEL) == "27",
              "FE level is that class's row in the stored class_levels table",
              repr(f.get(R._FE_LEVEL)))
        # The negative case goes through the STORE, not the JSON: the one-shot
        # import only runs against an empty database, so rewriting the JSON now
        # would prove nothing (the roster would simply be the one already imported).
        felobby.save_roster(fe_store, fe_account,
                            [{"charid": 1, "name": "Elle", "look1": 2}])
        f = R._content_game_fields(11, CTID + 11, member_id)
        check(R._FE_CLASS in f and R._FE_LEVEL not in f,
              "a character with no class_levels keeps its class and has NO level "
              "-- not Lv0", repr(f))

    # --- JANHOUROU -----------------------------------------------------------
    if importlib.util.find_spec("janstats") is None:
        print("[SKIP] JANHOUROU profile fields: the janstats title module is not present in this tree")
    else:
        import janstats
        rec = janstats.blank(member_id)
        rec["games_played"] = 7
        rec["places"] = [3, 2, 1, 1]
        rec["yakuman"] = 2
        rec["titles"] = {"mahjong_king": 1, "beast_king": 4, "bust_general": 0,
                         "winnings_general": 3, "wild_tile_king": 5}
        rec["overrides"] = {"rank": 5}         # tier 1, variant 0 = 凡人 Commoner
        janstats.store(member_id, rec)
        f = R._content_game_fields(3, JAN_CID, member_id)
        check(f.get(R._JAN_GAMES) == 7 and f.get(R._JAN_YAKUMAN) == 2,
              "jan games played and yakuman come off the one janstats record",
              repr(f))
        check([f.get(s) for s in R._JAN_TITLES] == [1, 4, 0, 3, 5],
              "the five title counters are in janstats.SHOGO_KEYS order",
              repr([f.get(s) for s in R._JAN_TITLES]))
        check(f.get(R._JAN_RANK) == 7,
              "the GAME's rank 5 is the VIEWER's 7 -- 5 variants per tier vs 7. "
              "Passing it through unconverted names the wrong rank from tier 1 up",
              repr(f.get(R._JAN_RANK)))
        check(f.get(R._JAN_LEVEL) == janstats.level_of(rec),
              "jan level is janstats' own, not a second formula")

    # --- DIRGE OF CERBERUS ---------------------------------------------------
    #     With the store keyed on the per-session uid there is nothing to match,
    #     and that must stay an EMPTY tail -- we cannot say whose character it is.
    doc_store = os.path.join(TMP, "doc-characters.json")
    os.environ["POL_DOC_CHARA_STORE"] = doc_store
    doc_stats = os.path.join(TMP, "doc-stats.json")
    os.environ["POL_DOC_STATS"] = doc_stats
    if os.path.exists(doc_stats):
        os.remove(doc_stats)
    with open(doc_store, "w", encoding="utf-8") as fh:
        json.dump({"0xa455a599": [{"slot": 0, "name": "Fox"}]}, fh)
    check(not R._content_game_fields(10, CTID + 10, member_id),
          "DoC with a UID-keyed store serves nothing -- no POL identity to "
          "match on")
    #     Account-keyed (docudp --account member:N): the name comes through, and
    #     it is the LOWEST slot, not whichever row happens to be first on disk.
    with open(doc_store, "w", encoding="utf-8") as fh:
        json.dump({"member:%d" % member_id: [{"slot": 1, "name": "Test"},
                                             {"slot": 0, "name": "Vincent"}]}, fh)
    f = R._content_game_fields(10, CTID + 10, member_id)
    check(f == {R._DOC_NAME: "Vincent"},
          "DoC with an ACCOUNT-keyed store and NO career serves the slot-0 name "
          "only -- no rank is claimed for a character that never finished a "
          "battle", repr(f))
    #     Career store (tools/doc_stats.py): Rank + Ranking Points of the SAME
    #     character the name names -- matched by name inside this member's keys,
    #     never the sibling slot, never another member's same-named character.
    with open(doc_stats, "w", encoding="utf-8") as fh:
        json.dump({"chars": {
            "member:%d/0x0002aa68" % member_id:
                {"name": "Vincent", "rank": 4, "rp": 120},
            "member:%d/0x0002aa69" % member_id:
                {"name": "Test", "rank": 9, "rp": 999},
            "member:%d/0x0002aa70" % (member_id + 1):
                {"name": "Vincent", "rank": 16, "rp": 5}}}, fh)
    f = R._content_game_fields(10, CTID + 10, member_id)
    check(f == {R._DOC_NAME: "Vincent", R._DOC_RANK: 4, R._DOC_RANKPOINT: 120},
          "DoC serves the slot-0 character's Rank (z_class enum 1..16, 4 = DG "
          "Scout 3rd Class) and Ranking Points from doc-stats.json", repr(f))
    with open(doc_stats, "w", encoding="utf-8") as fh:
        json.dump({"chars": {"member:%d/0x0002aa68" % member_id:
                             {"name": "Vincent", "rank": 99, "rp": -5}}}, fh)
    f = R._content_game_fields(10, CTID + 10, member_id)
    check(f.get(R._DOC_RANK) == 16 and f.get(R._DOC_RANKPOINT) == 0,
          "DoC rank is clamped to the enum's 1..16 and points never go negative",
          repr(f))
    os.remove(doc_stats)


def main():
    # 1. THE DISCRIMINATOR. Everything else depends on telling the two apart.
    REQ_HANDLE = _request((2, HID))
    REQ_CONTENT = _request((2, CTID), (1, CTSID))
    check(R._is_content_profile_request(REQ_CONTENT) is True,
          "SE's two-item request is recognised as a CONTENT profile")
    check(R._is_content_profile_request(REQ_HANDLE) is False,
          "SE's one-item request is NOT -- it is the handle profile")
    check(R._is_content_profile_request(None) is False,
          "no request at all falls back to the handle profile")

    # 2. THE LENGTH, which is the half that fails silently. A fixed table entry
    #    cannot express 'depends on the request', which is why 604 was wrong in
    #    kind rather than merely wrong.
    n_content = R._lobby_paylen(0x05, 0x04, REQ_CONTENT)
    n_handle = R._lobby_paylen(0x05, 0x04, REQ_HANDLE)
    check(n_content == 284, "content request declares 284 bytes",
          "got %d" % n_content)
    check(n_handle == 604, "handle request still declares 604",
          "got %d" % n_handle)

    # 3. THE RECORD. Field 0 and the two subject keys are the parts SE's own
    #    record lets us check exactly.
    rec = R._content_profile_record(280, REQ_CONTENT)
    check(len(rec) == 280, "record is 280 bytes", "got %d" % len(rec))
    lead = R._PROFILE_LEAD
    phead = int.from_bytes(rec[lead + 1:lead + 9], "little")
    check(phead == R._CONTENT_PHEAD == 104,
          "z_phead = 104, the value SE put in field 0", "got %d" % phead)
    off = lead + (1 + 8)                      # past z_phead
    ctsid = int.from_bytes(rec[off + 1:off + 5], "little")
    check(ctsid == CTSID, "z_ctsid echoed back", hex(ctsid))
    off += 1 + 4
    ctid = int.from_bytes(rec[off + 1:off + 9], "little")
    check(ctid == CTID, "z_ctid echoed back -- the subject the client named",
          hex(ctid))

    # 4. THE TAIL'S VALUES STAY ZERO -- but its VISIBILITY BYTES DO NOT, and the
    #    first cut of this check conflated the two and failed a correct record.
    #    SE's own record carries vis 3 on every field including the ones it has
    #    no value for, so we do the same; what must be zero is the VALUE.
    #
    #    WARNING: THIS IS THE UNRESOLVED-CONTENT CASE, and it stays valid now that the
    #    tail HAS sources: the request here names a Content ID nothing has
    #    linked, so `_content_code_for_cid` returns None, there is no title to
    #    ask and nothing may be filled. Section 7 covers the other direction.
    off, bad = R._PROFILE_LEAD, []
    for idx, fname, ln, ty in R._CONTENT_SCHEMA:
        step = 1 + ln + R._PROFILE_STEP_EXTRA.get(ty, 0)
        if idx > 3 and any(rec[off + 1:off + 1 + ln]):
            bad.append(fname)
        if idx > 3 and rec[off] != 3:
            bad.append(fname + "(vis)")
        off += step
    check(not bad, "every field past z_name has a ZERO value and vis 3",
          ", ".join(bad) if bad else "z_purp..z_raceid")

    # 5. THE ROUTER. The payload builder has to pick the content record for a
    #    content request and leave every other opcode alone.
    body = R._lobby_payload(0x05, 0x04, 284, REQ_CONTENT)
    check(body is not None and len(body) == 280,
          "05:04 + two-item TLV routes to the content record",
          "got %s" % (len(body) if body else None))

    # 6. A NAME WE KNOW gets served. This is the one field we can fill today,
    #    and it is what makes the popup show a character instead of a blank.
    db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    member = accounts.ensure_member(db, "CPTEST")
    handle = db.execute("SELECT * FROM handle WHERE member_id = ?",
                        (member["id"],)).fetchone()
    db.execute("UPDATE handle SET handle_name = ? WHERE id = ?",
               ("Foxffxi", int(handle["id"])))
    accounts.link_content_to_handle(db, int(handle["id"]), 1, str(CTID))
    db.commit()
    db.close()
    rec = R._content_profile_record(280, REQ_CONTENT)
    noff = lead + (1 + 8) + (1 + 4) + (1 + 8)
    name = rec[noff + 1:noff + 17].split(b"\x00")[0].decode("cp932", "replace")
    check(name == "Foxffxi", "z_name resolved from handle_content", repr(name))

    # 7. THE PER-TITLE TAIL. One check per thing that can be silently wrong --
    #    a value on the wrong slot, a value read from a store that stopped
    #    being written, or an index handed to an enum with a different stride.
    #    None of these raises; all of them just draw the wrong word.
    game_fields(int(handle["id"]), int(member["id"]))

    # 8. THE WRITE SIDE. 05:01 has the same two forms as 05:04 and the opcode
    #    does not say which -- and the ids mean DIFFERENT FIELDS in the two
    #    schemas (1/2/4 are z_ctsid/z_ctid/z_purp in a content record and
    #    z_name/z_hid/z_age_f in the handle one). A content write that reached
    #    `set_handle_profile` would put a Content ID in the handle's NAME.
    #    Measured on prod 2026-09-12: the Viewer sends an 84-byte-payload 05:01
    #    when a content profile's Purpose is edited, this parser refuses it, and
    #    nothing has ever stored it ("I changed Purpose for jan and it didn't
    #    save"). The refusal must be DELIBERATE, not a side effect of the parse
    #    failing.
    print("\n8. the 05:01 write side -- against SE's own bytes ->")
    #    THE FRAME IS THE REAL ONE. Rebuilding it would only ever test the
    #    builder's idea of the framing, and the framing is exactly what was
    #    wrong: every narrow item (1..4 bytes) is 8 bytes, not 16, so padding
    #    them all up to 8 stepped into the middle of a value, read len=457 and
    #    bailed. No 84-byte 05:01 had ever parsed.
    parsed = R._parse_profile_tlv(CONTENT_WRITE)
    check(parsed == {4: 1, 3: "", 2: JAN_CID, 1: JAN_CID, 0: ""},
          "the captured CONTENT write parses, and the walk lands on the trailer",
          repr(parsed))
    check(R._is_content_profile_request(CONTENT_WRITE) is True,
          "it is recognised as the CONTENT form (ids 1 AND 2 present)")
    #    THE ORDER OF THE TWO STEP RULES IS THE SAFETY PROPERTY: the rule every
    #    handle write has parsed under since 2026-08-10 is tried FIRST, so
    #    adding the content form cannot change anything that already works.
    check(R._walk_profile_tlv(CONTENT_WRITE, False) is None
          and R._walk_profile_tlv(CONTENT_WRITE, True) == parsed,
          "the content form needs the NARROW rule and is refused by the old "
          "one -- so the fallback is reached only when the first rule fails")
    handle_form = _request((2, HID), (5, 1))
    check(R._walk_profile_tlv(handle_form, False) is not None,
          "a handle-form write still lands under the FIRST rule, untouched")

    #    It must never reach handle_profile: ids 1/2/4 are z_ctsid/z_ctid/z_purp
    #    in a content record and z_name/z_hid/z_age_f in the handle one, so a
    #    content write landing there would put a Content ID in the handle NAME.
    wrote = []
    real_set = accounts.set_handle_profile
    accounts.set_handle_profile = lambda db, hid, f: wrote.append((hid, dict(f)))
    try:
        R._capture_profile_write(CONTENT_WRITE)
        check(not wrote,
              "a CONTENT-form 05:01 is NOT written into handle_profile",
              repr(wrote))
        # ...and the handle form still stores, or the refusal would be a
        # regression wearing a fix's clothes.
        wrote.clear()
        R._capture_profile_write(_request((2, HID), (5, 1)))
        check(len(wrote) == 1 and 5 in wrote[0][1],
              "a HANDLE-form 05:01 still stores, so the refusal is scoped",
              repr(wrote))
    finally:
        accounts.set_handle_profile = real_set

    #    THE ROUND TRIP -- the whole point. Purpose set in the Viewer has to
    #    come back on the next 05:04 for the same Content ID.
    stored = R._content_profiles().get(str(JAN_CID)) or {}
    check((stored.get("fields") or {}).get("4") == 1,
          "the write is STORED against its Content ID, keyed by schema field",
          repr(stored.get("fields")))
    check("1" not in (stored.get("fields") or {})
          and "2" not in (stored.get("fields") or {}),
          "the SUBJECT (z_ctsid/z_ctid) is not stored -- it is echoed, not "
          "remembered")

    rec = R._content_profile_record(432, _request((2, JAN_CID), (1, JAN_CID)))
    off, got = R._PROFILE_LEAD, None
    for idx, fname, ln, ty in R._CONTENT_SCHEMAS[3][0]:
        if idx == 4:
            got = rec[off + 1]
            break
        off += 1 + ln + R._PROFILE_STEP_EXTRA.get(ty, 0)
    check(got == 1,
          "and it comes back on the next 05:04 -- z_purp = 1 in the record",
          repr(got))

    #    A later write of a DIFFERENT field must not erase this one: the client
    #    only ever sends the screen it just saved.
    R._store_content_profile_write({1: JAN_CID, 2: JAN_CID, 3: "Foxjan"}, b"")
    again = (R._content_profiles().get(str(JAN_CID)) or {}).get("fields") or {}
    check(again.get("4") == 1 and again.get("3") == "Foxjan",
          "a second write MERGES -- Purpose survives a name-only save",
          repr(again))

    print("\n%s" % ("content_profile OK" if not FAILS
                    else "FAILED: " + ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
