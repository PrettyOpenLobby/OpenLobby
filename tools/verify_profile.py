#!/usr/bin/env python3
"""Pin the 05:04 profile record's field offsets against SE's live service.

WHY THIS EXISTS. `_PROFILE_SCHEMA` is an ordered list, and the record is packed
positionally -- one leading byte, then per field `1 visibility byte + len value
bytes`. So inserting a field, or getting one length wrong, silently shifts every
field after it. The record still has the right SIZE and the right shape, the
checksum still passes, and the client renders somebody's age into their job. A
size assertion cannot catch that; only offsets can.

The offsets below were READ OFF SE'S OWN 05:04 REPLIES (polshim-se.429364.log,
2026-08-15, decoded with pol-shim/tools/lobbydec.py). They are structure, not
account data:

    +0x00   leading byte           SE sent 01 / 02 / 03 in different sessions,
    +0x01   z_phead's vis byte     always the SAME value on both bytes. Decoding
                                   three more of SE's records settled what it is:
                                   the profile's DISCLOSURE LEVEL, on the same
                                   1/2/3 scale the 05:01 write carries at +0x2A.
                                   2 on the account holder's own restricted
                                   profile, 3 on the two openly viewable ones,
                                   while fields 1..31 stayed 3 in all three. We
                                   sent a hardcoded 01 -- the most restrictive
                                   value -- so every profile read as hidden to
                                   everyone else and the owner's own editor
                                   could not save its way out. Now `level`.
    +0x02   u16 424 (0x1A8)        field 0 z_phead's value: the record's own
                                   USED length. Also echoed in the 5:4 reply
                                   header, which is how the two agree.
    +0x0b   z_name                 "Fox" / "Cyn"
    +0x1c   z_hid                  the subject handle id -- the SAME id the
                                   request's TLV asks for (item id 2, len 8)
    +0x25   z_age                  33 for the friend
    +0x49   z_mail                 "No Mail address" when unset
    +0x18a  z_ficon                THE FACE ICON. 757 for the friend, 2439 for
                                   the account holder -- and 757 is byte-for-byte
                                   the value the PUSH record carries at +0x50 for
                                   the same person at the same time, which is two
                                   independent carriers agreeing on one field.

Run: python tools/verify_profile.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "services"))
os.environ.setdefault("POL_ACCOUNTS_DB", "/nonexistent/verify.db")

import responders as R  # noqa: E402

#: field id -> (name, offset of its VALUE) as measured on SE's wire.
SE_OFFSETS = {
    0:  ("z_phead", 0x02),
    1:  ("z_name",  0x0b),
    2:  ("z_hid",   0x1c),
    3:  ("z_age",   0x25),
    18: ("z_mail",  0x49),
    19: ("z_ficon", 0x18a),
}

SE_USED_LEN = 0x1A8          # 424, field 0's own value


def offsets():
    """value offset per field id, from the schema's positional packing."""
    out, o = {}, 1
    for fid, name, ln, _kind in R._PROFILE_SCHEMA:
        out[fid] = (name, o + 1, ln)     # +1 for the visibility byte
        o += 1 + ln
    return out, o


def main():
    got, used = offsets()
    bad = 0

    for fid, (want_name, want_off) in sorted(SE_OFFSETS.items()):
        name, off, ln = got.get(fid, ("<missing>", -1, 0))
        ok = name == want_name and off == want_off
        bad += not ok
        print(f"  field {fid:>2} {want_name:<9} SE +{want_off:#05x}  "
              f"ours +{off:#05x} ({name}, len {ln})  {'ok' if ok else 'MISMATCH'}")

    ok_len = used == SE_USED_LEN
    bad += not ok_len
    print(f"\n  used length: SE {SE_USED_LEN} (0x1A8), ours {used}  "
          f"{'ok' if ok_len else 'MISMATCH'}")

    rec = R._profile_record(600, None)
    ok_size = len(rec) == 600
    bad += not ok_size
    hdr_ok = rec[2] == (SE_USED_LEN & 0xFF) and rec[3] == (SE_USED_LEN >> 8)
    bad += not hdr_ok
    print(f"  record size: {len(rec)} {'ok' if ok_size else 'MISMATCH'}; "
          f"z_phead value at +0x02 = {rec[2] | (rec[3] << 8)} "
          f"{'ok' if hdr_ok else 'MISMATCH'}")

    # THE PRIVACY DIAL. With no account behind it the record falls to the default
    # level, and the default must be the OPEN one -- SE's two viewable profiles
    # both read `03 03` here, and a hardcoded 1 in these two bytes is exactly the
    # bug that made "Selected profile is hidden" unfixable from inside the client.
    # Pinned as a pair because SE never moves one without the other.
    lvl_ok = rec[0] == rec[1] == R._PROFILE_LEVEL_DEFAULT == 3
    bad += not lvl_ok
    print(f"  level pair at +0x00/+0x01 = {rec[0]:#04x} {rec[1]:#04x} "
          f"(SE's viewable profiles: 0x03 0x03)  "
          f"{'ok' if lvl_ok else 'MISMATCH'}")

    # And fields 1..31 do NOT follow the dial: SE holds them at 3 even in the
    # record whose head says 2. z_name's byte stands in for the lot.
    fld_ok = rec[0x0a] == R._PROFILE_FIELD_VIS == 3
    bad += not fld_ok
    print(f"  field vis at +0x0a (z_name) = {rec[0x0a]:#04x} "
          f"(SE: 0x03, independent of the level)  "
          f"{'ok' if fld_ok else 'MISMATCH'}")

    bad += check_content_block()

    print("\nPASS" if not bad else f"\nFAIL ({bad} mismatch)")
    return 1 if bad else 0


#: SE's OWN populated content block, lifted from the one retail session where
#: the per-Content-ID section actually worked
#: (work/pc/prof-ffxi-retail-20260823.log:9321, record +0x1D0):
#:
#:     0100 0100 BD80650A F5C16A01 00000000
#:
#: = content code 1 (FFXI), the undecoded 1, z_ctsid 0x0A6580BD and z_ctid
#: 0x016AC1F5 -- the same pair that session's 52-byte content request carried.
#: Pinning the ENCODER against SE's bytes is the only check available offline:
#: it cannot prove the client READS the block, but it does prove that when we
#: fill it we produce what SE produced, byte for byte, at the offset SE used.
#: See `_IDREC_CONTENT_AT` in responders.py for the diff this came from.
SE_CONTENT_ENTRY = bytes.fromhex("01000100BD80650AF5C16A0100000000")
SE_CONTENT_CID = 0x016AC1F5
SE_CONTENT_CTSID = 0x0A6580BD


def check_content_block():
    """Does `_identity_content_entries` reproduce SE's 16 bytes? 0 = yes."""
    links = [{"content_code": 1, "content_id": str(SE_CONTENT_CID),
              "status": "active"}]
    real_list, real_world = R.accounts.handle_content_list, R._ffxi_world_fields
    try:
        R.accounts.handle_content_list = lambda _db, _hid: links
        R._ffxi_world_fields = lambda: {SE_CONTENT_CID: SE_CONTENT_CTSID}
        block = R._identity_content_entries(None, 1)
    finally:
        R.accounts.handle_content_list = real_list
        R._ffxi_world_fields = real_world

    span = R._IDREC_CONTENT_STRIDE * R._IDREC_CONTENT_MAX
    problems = []
    if len(block) != span:
        problems.append(f"block is {len(block)}B, want {span}")
    if block[:16] != SE_CONTENT_ENTRY:
        problems.append(f"entry 0 = {block[:16].hex()}, "
                        f"SE sent {SE_CONTENT_ENTRY.hex()}")
    if any(block[16:]):
        problems.append("slots 1..7 are not zero for a one-content handle")
    # And it must land where SE puts it: idrec +0x28 == record +0x1D0.
    at = R._PROFILE_IDREC_AT + R._IDREC_CONTENT_AT
    if at != 0x1D0:
        problems.append(f"block offset is {at:#x}, SE's is 0x1d0")

    print(f"\n  content block: entry 0 = {block[:16].hex()} "
          f"(SE: {SE_CONTENT_ENTRY.hex()})  "
          f"{'ok' if not problems else 'MISMATCH'}")
    for p in problems:
        print(f"    - {p}")
    return len(problems)


if __name__ == "__main__":
    sys.exit(main())
