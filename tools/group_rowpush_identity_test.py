"""THE GROUP ROSTER PUSH AND THE 7:12 MEMBER RECORD MUST NAME A MEMBER THE SAME WAY.

The client's group member table has exactly two writers -- the 7:12
`KGetGroupList` member records (lobby band) and the `ev=(group<<8)` roster push
(auth band) -- and its install path finds-or-appends on the member's IDENTITY.
State that identity in two dialects and the push stops UPDATING the member the
record installed and starts APPENDING a second one.

MEASURED LIVE 2026-08-25, single-variable A/B on prod with the account holder
reading the screen: six members served, six pushed, the group list showed
TWELVE and the chat sidebar showed the viewer twice. `rowpush=0` in group.ctl
(the 04:29:46Z fetch served 6 members, zero pushes followed) -> the same list
showed SIX. Reported as "I'm seeing myself twice in the group chat".

The cause was the mask. `_group_member_record` serves `+0x00` RAW -- pinned
against the live per-session key K, where the self-match `cft_0355(+0x00) ==
own_id` needs the raw client_guid (`group-master-self-identity`) -- while
`build_field_push_record` XORs its `subject` by `_PUSH_GUID_MASK`
unconditionally, which is measured-correct for the FRIEND family that shares
it. So the wire carried `client_guid ^ MASK` against the record's plain
`client_guid`.

WARNING: The other suspect, `ev = group<<8 | sub` (we send `sub`=0 for every member),
is REFUTED by the same measurement and is deliberately NOT fixed here: a
`sub`-keyed lookup would have matched member 0 -- the viewer's own row -- and
duplicated only the other five, for ELEVEN. Twelve is a clean doubling of all
six, i.e. no member matched at all.

What is pinned:

  1. THE COUPLING, per member: the 8 bytes the push puts at `+0x00` equal the
     8 bytes that member's 7:12 record carries at `+0x00`. Read out of a real
     `_list_payload`, so it also covers WHICH id the record chose.
  2. It holds for a PEER, not only for the viewer's own row -- both records
     follow `POL_FRIEND_GUID_CLIENT`, so they can never disagree per-peer.
  3. It holds for a member we have never learned a `client_guid` for, where
     both sides fall back to `handle_guid`.
  4. POL_GROUP_ROWPUSH_RAW=0 restores the old masked form, so the regression
     is reproducible on demand rather than only in the field.
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="group-rowpush-")
DB = os.path.join(TMP, "accounts.db")
os.environ["POL_ACCOUNTS_DB"] = DB
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_GROUP_CTL"] = os.path.join(TMP, "no-such.ctl")
os.environ.setdefault("POL_LOBBY_LIST_MODE", "7:12=groups")
os.environ.setdefault("POL_GROUP_MEMBERS", "1")
os.environ["POL_FRIEND_GUID_CLIENT"] = "1"     # prod's setting, both records ride it

import accounts                                                    # noqa: E402
import responders as R                                             # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    line = "  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                            "  --  " + detail if detail else "")
    enc = sys.stdout.encoding or "ascii"
    print(line.encode(enc, "backslashreplace").decode(enc))
    if not ok:
        FAILS.append(label)


#: What `field_push_lines` was handed. The builder's own bytes are
#: `push_test.py`'s job; the one line that matters here is its unconditional
#: `subject ^ _PUSH_GUID_MASK` at +0x00, applied below to get the wire value.
PUSHED = []
R.field_push_lines = lambda nick, guid, slot, **kw: (
    PUSHED.append({"subject": int(guid), "name": kw.get("name"),
                   "group": kw.get("group"), "seq": kw.get("seq"),
                   "gpacked": kw.get("gpacked")}),
    [b"NOTICE line"])[1]

#: KEY: **THE BITS THE PUSH INSTALLER ACTUALLY COMPARES** (polcore, read
#: 2026-09-12). `FUN_037e9650` -- reached from the notice handler
#: `FUN_037db6f0` via the OBJECT field at chunk+8 -- walks the 64 member
#: entries at `group+0x28` (stride 0xC0) and matches a member when
#:
#:     (entry_packed >> 1) & 0xFFF_FFFFFFFF  ==  gpacked & 0xFFF_FFFFFFFF
#:
#: and on NOTHING ELSE. It never reads `+0x00`. The 7:12 installer
#: `FUN_037e86c0` stores `(record_packed & 0xFFF_FFFFFFFF) << 1` there, so the
#: two writers are compared on the low 44 bits of the PACKED word.
_MATCH44 = (1 << 44) - 1


def wire_ident(subject):
    """The 8 bytes that reach `+0x00` -- the builder XORs once, always."""
    return (int(subject) ^ R._PUSH_GUID_MASK) & 0xFFFFFFFFFFFFFFFF


class FakeSession:
    alive = True
    nick = b"WATCHERNICK"

    def send(self, lines):
        return True


# --- one group, three members: the owner, a peer, and a peer whose client id
#     we have never learned (the handle_guid fallback both sides must share) --
conn = accounts.connect(DB)
accounts.create_polid(conn, "GRPPUSH", "pw", area_kbn="00", login_pf="01")
ids, CG = {}, {"Fox": 0xE13883D826, "Cyn": 0x860FB3E2A2, "Yatih": None}
for name in ("Fox", "Cyn", "Yatih"):
    mid = accounts.add_member(conn, "GRPPUSH", name.lower(), "pw")
    accounts.set_handle(conn, mid, name)
    hid = int(accounts.primary_handle_row(conn, mid)["id"])
    ids[name] = (mid, hid)
    if CG[name]:
        conn.execute("UPDATE handle SET client_guid = ? WHERE id = ?",
                     (CG[name], hid))
conn.commit()
gid = accounts.add_friend(conn, ids["Fox"][1], "Example.gang",
                          kind=accounts.KIND_GROUP, guid=0)
for name in ("Fox", "Cyn", "Yatih"):
    accounts.add_group_member(
        conn, gid, name, member_handle=ids[name][1],
        cls=(accounts.GROUP_CLASS_MASTER if name == "Fox" else 3))
conn.close()

MID, HID = ids["Fox"]
R._session_member_id = lambda: MID
R._session_handle_id = lambda db=None: HID
R._face_icons_by_handle = lambda: {}

# --- the 7:12 reply, as the client actually receives it -------------------- #
count = R._list_count(0x07, 0x0C)
payload = R._list_payload(0x07, 0x0C, R._list_paylen(0x07, 0x0C, count))
nmem = payload[1]
base = 8 + count * R._LOBBY_LIST[(0x07, 0x0C)][0]     # [0] is the 136B record size
served = {}
for i in range(nmem):
    off = base + i * R._GROUP_MEMBER_REC
    ident = struct.unpack_from("<Q", payload, off)[0]
    nm = bytes(payload[off + 0x10:off + 0x20]).split(b"\x00")[0].decode("cp932")
    served[nm] = ident
check(nmem == 3 and set(served) == {"Fox", "Cyn", "Yatih"},
      "the 7:12 reply carries all three member records",
      f"n={nmem} names={sorted(served)}")

# --- the roster push for the same three -------------------------------------#
db = accounts.connect(DB)
R.PRESENCE.register(MID, FakeSession())
PUSHED.clear()
entries = [[int(gid), [[int(g), str(nm), int(c)]
                       for g, nm, c in accounts.list_group_members(db, gid)]]]
R._push_deliver_grouprows({"kind": "grouprows", "member": MID,
                           "groups": entries, "after": 0}, db)
pushed = {p["name"]: p["subject"] for p in PUSHED}
check(set(pushed) == set(served),
      "the push covers exactly the members the reply installed",
      f"{sorted(pushed)}")

print("\n1/2/3. the coupling, per member ->")
for nm in ("Fox", "Cyn", "Yatih"):
    if nm not in pushed or nm not in served:
        check(False, f"{nm}: present in both records")
        continue
    why = ("the viewer's OWN row" if nm == "Fox" else
           "a PEER" if CG[nm] else "a peer with NO learned client_guid")
    check(wire_ident(pushed[nm]) == served[nm],
          f"{nm}: push +0x00 == 7:12 record +0x00  ({why})",
          f"push=0x{wire_ident(pushed[nm]):016X} record=0x{served[nm]:016X}")
if "Yatih" in served:
    check(served["Yatih"] == accounts.handle_guid(ids["Yatih"][1]),
          "the no-client_guid member falls back to handle_guid on BOTH sides",
          f"0x{served['Yatih']:016X}")

# --- 5. THE FIELD THE INSTALLER ACTUALLY KEYS ON --------------------------- #
#     THIS IS THE CHECK THAT WAS MISSING, and its absence is why the roster
#     doubling bug survived a "fixed" claim AND a passing test for three weeks: everything
#     above pins `+0x00`, which the PUSH path does not look at. On 2026-08-22
#     `_group_member_record` started writing the TAGGED z_hid form into the
#     packed word's low 32 bits (POL_GROUP_MEMBER_ZHID, so a group row's "View
#     Profile" would resolve) while the push went on sending the raw guid low
#     half. Every member then missed and was appended -- 18/64 for nine members,
#     the clean 2x the live A/B measured 2026-09-09.
print("")
print("5. the PACKED word -- the bits the push installer compares ->")
recs = {}
for _i in range(nmem):
    _off = base + _i * R._GROUP_MEMBER_REC
    _nm = bytes(payload[_off + 0x10:_off + 0x20]).split(b"\x00")[0].decode("cp932")
    recs[_nm] = struct.unpack_from("<Q", payload, _off + 0x08)[0]
gpacked = {q["name"]: q["gpacked"] for q in PUSHED}
for nm in ("Fox", "Cyn", "Yatih"):
    gp, rp = gpacked.get(nm), recs.get(nm)
    check(gp is not None and rp is not None
          and (int(gp) & _MATCH44) == (rp & _MATCH44),
          f"{nm}: push gpacked low-44 == 7:12 record +0x08 low-44",
          f"push=0x{(gp or 0) & _MATCH44:012X} record=0x{(rp or 0) & _MATCH44:012X}")

#     And the tag must still BE there. A "fix" that made both sides raw would
#     pass the equality above and silently undo the 08-22 View Profile fix, so
#     pin the VALUE, not only the agreement.
_want_low = R._FRIEND_HID_TAG | (accounts.handle_guid(ids["Fox"][1]) & 0x7FFFF)
check(recs.get("Fox") is not None and (recs["Fox"] & 0xFFFFFFFF) == _want_low,
      "and the low 32 bits are still the TAGGED z_hid form, not the raw guid "
      "-- 'View Profile' on a group row needs it",
      f"0x{recs.get('Fox', 0) & 0xFFFFFFFF:08X} want 0x{_want_low:08X}")

#     NEGATIVE CONTROL. One producer IS the fix, so prove a SECOND one
#     reproduces the bug: this is the line `_push_deliver_grouprows` carried,
#     verbatim, until 2026-09-12.
def _old_push_packed(guid, cls):
    return (int(guid) & 0xFFFFFFFF) | ((int(cls) & 7) << R._GROUP_MEMBER_CLASS_BIT)


_db = accounts.connect(DB)
_mem = {str(m[1]): (int(m[0]), int(m[2]))
        for m in accounts.list_group_members(_db, gid)}
_db.close()
_missed = sum(1 for nm, rp in recs.items()
              if nm in _mem
              and (_old_push_packed(*_mem[nm]) & _MATCH44) != (rp & _MATCH44))
check(_missed == nmem,
      "the OLD one-line builder mismatches EVERY member -- which is the "
      "doubling, and a check that cannot pass by accident",
      f"{_missed}/{nmem} would have been appended")

print("\n4. the regression is reproducible on demand ->")
os.environ["POL_GROUP_ROWPUSH_RAW"] = "0"
PUSHED.clear()
R._push_deliver_grouprows({"kind": "grouprows", "member": MID,
                           "groups": entries, "after": 0}, db)
old = {p["name"]: p["subject"] for p in PUSHED}
check(all(wire_ident(old[nm]) != served[nm] for nm in served),
      "RAW=0 puts the masked form back -- every member mismatches again, "
      "which is the 12-instead-of-6 bug",
      f"Fox push=0x{wire_ident(old['Fox']):016X} record=0x{served['Fox']:016X}")
os.environ["POL_GROUP_ROWPUSH_RAW"] = "1"
db.close()

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
