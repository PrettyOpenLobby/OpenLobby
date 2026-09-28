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
import pgtest  # noqa: E402
pgtest.use_fresh_database()
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
    """`_content_game_fields` is the title hook and nothing else.

    The per-game builders (FFXI's bridge record, Fantasy Earth's roster, Front
    Mission's pilots, Dirge of Cerberus's careers, Janhourou's tallies) live in
    their title modules and are tested in those repositories. What the core
    promises: the title that serves a code fills the fields, a code with no
    title yields nothing, and a title that raises yields nothing rather than a
    half-filled record.
    """
    import titles

    class Probe(titles.Title):
        tag = b"PRB"
        content_code = 250
        calls = []

        def profile_fields(self, cid, mid):
            self.calls.append((cid, mid))
            return {3: "Probe", 7: 2}

    class Broken(titles.Title):
        tag = b"BRK"
        content_code = 251

        def profile_fields(self, cid, mid):
            raise RuntimeError("store down")

    probe, broken = Probe(), Broken()
    titles.register(probe)
    titles.register(broken)
    try:
        f = R._content_game_fields(250, CTID, member_id)
        check(f == {3: "Probe", 7: 2} and probe.calls == [(CTID, member_id)],
              "the title that serves the code builds the fields", repr(f))
        check(R._content_game_fields(252, CTID, member_id) == {},
              "a code with no title yields nothing, not a guess")
        check(R._content_game_fields(251, CTID, member_id) == {},
              "a title that raises yields nothing rather than a half-filled record")
        # the world identity and display-name hooks default to "not mine"
        check(titles.character_world(250, CTID) is None
              and titles.character_display_name(250, CTID, str(CTID), "Fox") is None
              and titles.content_schema(250) is None,
              "a title that does not override the identity hooks leaves them to the core")
    finally:
        titles._TITLES[:] = [t for t in titles._TITLES if t not in (probe, broken)]

    # JAN_CID is the subject of the captured content write below, and it must
    # resolve to content 3 whether or not any title module is present. The mint
    # hands out ids in the same range, so free the id first.
    db = accounts.connect()
    db.execute("DELETE FROM handle_content WHERE text_int(content_id) = %s",
               (JAN_CID,))
    accounts.link_content_to_handle(db, handle_id, 3, str(JAN_CID))
    db.commit()
    db.close()


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
    db = accounts.connect()
    member = accounts.ensure_member(db, "CPTEST")
    handle = db.execute("SELECT * FROM handle WHERE member_id = %s",
                        (member["id"],)).fetchone()
    db.execute("UPDATE handle SET handle_name = %s WHERE id = %s",
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
