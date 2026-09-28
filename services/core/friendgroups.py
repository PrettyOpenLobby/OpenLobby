"""Friend groups: configuration, membership, create/delete/class change, join and invite."""
import os
import re
import struct
from srvcore import log
from .deps import accounts
from . import friendlist, friendput, handlelists, lobbymail, lobbysession, pushchannel, pushspool, resourcestore



def _group_cfg(key, default):
    """One 7:12 probe knob from the control file, else `default`.

    KEY: `rowpush=0` is the ONE knob here that is not about the 7:12 record at
    all: it silences BOTH `ev=(group<<8)` roster pushes (the one the 7:12
    compose issues and the one an accept issues), live, without recreating a
    container. It exists because the client's group MEMBER TABLE has exactly
    two writers -- those pushes and the 7:12 member records -- and "I see
    myself twice in group chat" (reported 2026-08-25, with the window's own
    counter reading 02/12 while our 353/352 for that join carried exactly ONE
    nick) can only be the table holding a member twice. Turning the second
    writer off and re-opening the chat is the single-variable A/B that says
    which one is doubling. WARNING: It is a DIAGNOSTIC: with it off the face pictures
    and live role changes in the group list stop arriving, because the 32-byte
    member record has no field for either. Clear the file when done -- a stale
    value here breaks every later group fetch.
    """
    try:
        st = os.stat(handlelists._GROUP_CTL_FILE)
        if st.st_mtime != handlelists._GROUP_CTL["mtime"]:
            vals = {}
            with open(handlelists._GROUP_CTL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            handlelists._GROUP_CTL["vals"] = vals
            handlelists._GROUP_CTL["mtime"] = st.st_mtime
    except OSError:
        handlelists._GROUP_CTL["vals"], handlelists._GROUP_CTL["mtime"] = {}, None
    return handlelists._GROUP_CTL["vals"].get(key, default)


def _group_cfg_int(key, default):
    try:
        return int(str(_group_cfg(key, default)), 0)
    except ValueError:
        return default


def _group_role(member, owner_guid):
    """One stored member row, with the OWNER's class forced to master (5),
    whatever is stored. This is what the invite button is gated on.

    WARNING: This used to ALSO rewrite every stored 3 to 2, on the 2026-08-16 claim
    that "3 appears nowhere in either SE capture; it is not a role". That
    rewrite was the live "stuck at Inviting into group" regression of
    2026-08-22: SE's class-2 rows were PENDING INVITEES, class 3 is the
    accepted plain member (the roster pushes step 2 -> 3 -> 4), and 2 on the
    viewer's OWN row marks the group unusable (polcore 0x37e7d10) -- armed the
    moment the f46e2295 self-match fix let the client recognise its own row.
    The class now comes straight from `accounts.list_group_members`, which
    derives 2 vs 3 from the `pending` flag. Do not reintroduce a serve-time
    class rewrite here; it sits AFTER that mapping and cannot tell a pending
    row's deliberate 2 from a stale stored one.
    """
    guid, name, cls = member[0], member[1], member[2]
    if int(guid) == int(owner_guid):
        return (guid, name, accounts.GROUP_CLASS_MASTER)
    return member


def _group_members(groups):
    """Members per group, in the SAME order as `groups`, for the 07:12 reply.

    `groups` is exactly what `_db_friends(kinds=(KIND_GROUP,))` returned -- the
    rows the 7:12 HEADERS are built from. Membership is looked up per group off
    that list rather than re-queried independently, so the count block and the
    record loop cannot drift apart. They must agree or the client walks off the
    end of one group's members into the next group's.

    Returns `[[(guid, name, class), ...], ...]`, one list per group, each already
    capped at the client's 64 member slots by `accounts.list_group_members`.

    **The creator is a member of their own group.** Until 2026-08-13 the count
    block's bytes 1..4 were served as zero, so every group arrived with an empty
    membership -- and the client showed no groups at all and could not disband
    one (-7202, raised locally with no request on the wire, because it had no
    group to disband). polcore only sets a group's valid bit (obj+0x0C bit 0, at
    0x37e89c6) when at least one member was ACCEPTED, and every enumerator tests
    that bit.

    So a group with no stored members FALLS BACK to the owner rather than going
    out empty. That is not a cosmetic default: it is what keeps groups created
    before membership was modelled (and any group whose members have all been
    removed) visible instead of silently vanishing. It is also truthful -- the
    owner is a member of their own group. `7:1` now stores the owner explicitly,
    so the fallback only covers pre-existing rows.
    """
    if accounts is None or not groups:
        return [[] for _ in groups]
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            hid = lobbysession._session_handle_id(db)
            if not hid:
                return [[] for _ in groups]
            row = db.execute("SELECT handle_name FROM handle WHERE id = ?",
                             (hid,)).fetchone()
            # THE OWNER IS THE MASTER, and that is what unlocks invite. SE serves
            # class 5 for the creator of every one of their groups; we served the
            # plain-member class to everybody, so the client greyed out invite with
            # "You are not authorized to invite group friends." See
            # accounts.GROUP_CLASS_MASTER for the measurement.
            owner = (accounts.handle_guid(hid), row["handle_name"] if row else "",
                     accounts.GROUP_CLASS_MASTER)
            # `members=N` FORCES N copies of the owner and ignores real
            # membership. It stays because it is the A/B that established the
            # valid-bit behaviour in the first place: the question it answers is
            # whether the client's accepted-member counter is what gates
            # visibility, and that needs a count we control. Real membership is
            # the default; this is a probe.
            forced = _group_cfg_int("members", 0)
            if forced > 0:
                n = min(forced, accounts.GROUP_MEMBER_MAX)
                log("lobby", f"  7:12 members: FORCED to {n} synthetic member(s) "
                             f"per group by group.ctl `members=` -- real "
                             f"membership is NOT being served")
                return [[owner] * n for _ in groups]
            out = []
            for g in groups:
                name = g[1]
                # THE GROUP'S ID AND ITS OWNER TRAVEL WITH THE ENTRY. Resolving
                # by `group_id(db, hid, name)` only ever worked for the OWNER --
                # a member asking about a group they did not create got None, an
                # empty roster, and (before this) a group that vanished. The
                # entry now carries the owner's row id and handle, so a member
                # and the owner see the SAME group with the SAME membership.
                gid = int(g[0])
                owner_hid = int(g[2]) if len(g) > 2 else int(hid)
                if owner_hid != hid:
                    orow = db.execute(
                        "SELECT handle_name FROM handle WHERE id = ?",
                        (owner_hid,)).fetchone()
                    owner = (accounts.handle_guid(owner_hid),
                             orow["handle_name"] if orow else "",
                             accounts.GROUP_CLASS_MASTER)
                else:
                    owner = (accounts.handle_guid(hid),
                             row["handle_name"] if row else "",
                             accounts.GROUP_CLASS_MASTER)
                # PENDING INVITEES ARE THE OWNER'S PRIVATE STATE. Only the
                # owner's own view lists them (that is whose screen says
                # "Inviting into group"); a member -- or the invitee themself
                # -- sees accepted members alone. Reported live 2026-08-18:
                # invitees saw themselves, the full roster and the inviting
                # text, all of which belongs to the owner until acceptance.
                mems = accounts.list_group_members(
                    db, gid, include_pending=(owner_hid == hid)) if gid else []
                # *** THE OWNER IS ALWAYS IN THEIR OWN GROUP. ***
                #
                # This used to fall back to `[owner]` only when the stored list
                # was EMPTY, which was safe for exactly as long as nothing could
                # store a member. The moment group invites began persisting
                # (2026-08-16), the first real member ENDED the fallback and
                # evicted the owner from their own group: `LexGoons` went out
                # carrying LaptopTest2 alone. That is what broke everything the
                # account holder then reported -- the owner absent from the
                # roster means no master in the group (so `_group_role` has
                # nobody to promote), the owner missing from their own chat
                # sidebar, and the invitee holding a group whose owner it cannot
                # see. It is also simply untrue: SE lists `Lex` in every one of
                # their four groups, at class 5.
                #
                # Prepended, not appended, because SE puts the master first.
                if not any(int(m[0]) == int(owner[0]) for m in mems):
                    mems = [owner] + list(mems)
                roles = [_group_role(m, owner[0]) for m in mems]
                # MASTER FIRST, as SE orders it. `Examplemember` is member 0 of
                # all four of their groups in both captures, and `list_group_members`
                # is oldest-first -- so once the owner was backfilled AFTER an
                # invitee, the roster went out with a plain member at index 0.
                # Worth matching rather than assuming it is cosmetic: index 0 of
                # this list is a candidate for the group identity the client draws
                # above the chat box, which was reported showing the wrong person.
                roles.sort(key=lambda m: 0 if int(m[0]) == int(owner[0]) else 1)
                # *** THE 64-SLOT CEILING BELONGS AFTER THE PREPEND. ***
                # `list_group_members` caps at GROUP_MEMBER_MAX, and prepending
                # the owner to a group already sitting at that cap makes 65 --
                # one more than the client has slots for, and one more than the
                # count block can even declare (it is a byte the client
                # validates as <= 0x40). The reply then carried 65 records while
                # announcing 64, which is the walk-off-the-end this function's
                # own docstring is about: every LATER group's members read
                # shifted by 32 bytes.
                #
                # Capping here rather than at the count block keeps the length
                # calculation (`_list_paylen`, which sums these same lists) and
                # the record loop deriving from one number. Master-first
                # ordering means the row this drops is always a plain member,
                # never the owner.
                if len(roles) > accounts.GROUP_MEMBER_MAX:
                    log("lobby", f"  7:12 {name!r}: {len(roles)} members once the "
                                 f"owner is included -- serving the first "
                                 f"{accounts.GROUP_MEMBER_MAX}, which is all the "
                                 f"client has slots for")
                    roles = roles[:accounts.GROUP_MEMBER_MAX]
                out.append(roles)
            return out
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  group members lookup failed ({exc!r})")
        return [[] for _ in groups]


#: The 3-bit class at bit 50 of a member record's packed word. READ OFF THE
#: CLIENT at 0x1011cd8a (FriendList.dll memory image):
#:
#:     mov  eax, [edi + 8]      \ the 64-bit packed word
#:     mov  edx, [edi + 0xc]    /
#:     mov  cl, 0x32            ; bit 50
#:     call 0x10130400          ; shift right by cl
#:     and  eax, 7              ; 3 bits
#:     cmp  eax, 2
#:     jb   reject              ; must be >= 2
#:     cmp  eax, 5
#:     ja   reject              ; must be <= 5
#:
#: A ZERO here is out of range, so every member we served was rejected -- and a
#: group whose members are all rejected is dropped, which is why three
#: well-formed groups (a correct 536-byte 7:12 reply) showed as no groups at all.
#:
#: **2 IS THE ONE VALUE IN RANGE THAT STILL HIDES THE GROUP** (found 2026-08-13,
#: statically). The old note here -- "eax is dead after the compares, so 2 is as
#: right as any of 2..5, and nothing downstream reads it back" -- was wrong about
#: the second half. It IS read back, one level up:
#:
#:   0x37e8892  the member's class is stashed at BITS 18..20 of member[+0x08]
#:              (`and eax,7; shl eax,0x12`)
#:   0x37e8917  when a member's guid equals the CLIENT'S OWN id, polcore takes
#:              that member's class and copies it into the GROUP's flags --
#:              `shr edx,0x11; and edx,0xe` -> group[+0x0C] bits 1..3
#:   0x37e7d10  a group is reported UNUSABLE when `group[+0x0C] & 0xE == 4`,
#:              i.e. exactly when that class is **2**
#:
#: Our one member per group IS the session's own handle, so the copy always
#: fires and every group inherited class 2 -- installed, named, counted, and
#: then reported unusable, which is a list that renders empty. The member gate
#: accepts 2..5 and the group gate rejects 2, so the usable values are 3, 4, 5.
#: Which of the three means owner/admin/member is NOT established; 3 is the
#: conservative "plain member" pick, and `mclass=` in the group control file
#: sweeps it without a restart (try 4/5 if an owner-only action like disband
#: still refuses).
_GROUP_MEMBER_CLASS = 3
_GROUP_MEMBER_CLASS_BIT = 50
_GROUP_MEMBER_SLOT_BIT = 53


def _client_guid_map():
    """`{handle_name: client_guid}` for every handle that has told us one.

    KEY: **A CLIENT DOES NOT KNOW ITSELF BY OUR GUID.** `_capture_self_guid` learns
    the id a client calls itself from its `u/account` fetch, and it deliberately
    ignores anything we served -- so a row in this map is, by construction, an id
    of the CLIENT's own that has nothing to do with `handle_guid(id)`:

        Lex          we serve 0x80000000003   it calls itself 0x162e92cdc54
        DeckTester   we serve 0x8000000000a   it calls itself 0x14ab0fbd278
        LaptopTest2  we serve 0x80000000016   it calls itself 0x860fb3e2a2

    The mail path already depends on this (`_mail_recipient_row` resolves both
    forms, or a message renders "To: Unknown User"). The 7:12 member record needs
    it for the same reason and had never used it.
    """
    if accounts is None:
        return {}
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            return {r["handle_name"]: int(r["client_guid"]) for r in db.execute(
                "SELECT handle_name, client_guid FROM handle"
                " WHERE client_guid IS NOT NULL AND client_guid != 0")}
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  client guid map unavailable ({exc!r})")
        return {}


#: WARNING:KEY: **ONE PRODUCER FOR THE GROUP MEMBER'S PACKED WORD, BECAUSE TWO WRITERS
#: ARE MATCHED ON IT AND THEY DRIFTED APART FOR THREE WEEKS (the roster-doubling
#: bug).**
#:
#: The 32-byte `07:12` member record and the `ev=(group<<8)` roster PUSH both
#: install into polcore's ONE member table, and each has its OWN
#: find-or-append -- **with different keys.** Read out of polcore 2026-09-12:
#:
#:   * the 7:12 installer `FUN_037e86c0` calls `FUN_037e89e0(group, lo, hi,
#:     slot)`, which walks the 64 entries at `group+0x28` (stride 0xC0) and
#:     matches on `(entry+0x00, entry+0x04, (entry_packed >> 45) & 0x3F)` --
#:     the masked identity plus the slot;
#:   * the PUSH installer `FUN_037e9650` -- reached from the notice handler
#:     `FUN_037db6f0` via the OBJECT field at chunk+8 -- walks the SAME entries
#:     and matches on **`(entry_packed >> 1) & 0xFFF_FFFFFFFF == gpacked &
#:     0xFFF_FFFFFFFF`**, the low 44 bits of THIS WORD and nothing else. It
#:     never reads `+0x00`.
#:
#: => **That is why the 2026-08-25 "dialect" fix did not close the doubling.** It aligned
#: `+0x00`, a field the push path does not look at. What actually broke the
#: match was `POL_GROUP_MEMBER_ZHID` three days EARLIER (2026-08-22): the 7:12
#: record began writing the TAGGED z_hid form into the low 32 bits so a group
#: row's "View Profile" would resolve, and the push kept sending the raw
#: `guid & 0xFFFFFFFF`. The low 44 bits then differed for EVERY member, every
#: push appended, and the roster doubled -- the clean 2x the live A/B measured
#: (18/64 for nine members, 2026-09-09), rather than the n+1 a slot-keyed or
#: index-keyed miss would have given.
#:
#: `_push_deliver_grouprows`'s own docstring already required this: the tuples
#: must match "because the client checks the packed word against what the list
#: carried". They stopped matching in a DIFFERENT function and nothing
#: re-checked it. So the word is built here, once, and neither writer may build
#: it itself.
#:
#: WARNING: The class (bit 50) and the slot (bit 53) sit ABOVE the compared 44 bits and
#: cannot affect the push's match. Only the low 32 can -- which is the half that
#: carries an identity, and therefore the half that gets "improved".
def _group_member_packed(guid, cls, slot=0, self_guid=None):
    """The 64-bit word at `07:12` member record +0x08 and in the roster push's
    OBJECT field. Built ONCE, for both. See the note above."""
    # Both halves of the packed word are sweepable: the class is range-checked
    # 2..5 at 0x37e87b4 (a member outside it is skipped and never counts toward
    # the group's valid bit), and the slot is matched against the caller's own
    # argument at 0x37e8a23, so a wrong slot makes the member unfindable.
    packed = ((_group_cfg_int("mclass",
                              _GROUP_MEMBER_CLASS if cls is None else int(cls)) & 7)
              << _GROUP_MEMBER_CLASS_BIT) \
        | ((_group_cfg_int("mslot", int(slot)) & 0x3F) << _GROUP_MEMBER_SLOT_BIT)
    if os.environ.get("POL_GROUP_MEMBER_GUID", "1") != "1":
        return packed & 0xFFFFFFFFFFFFFFFF
    # THE HANDLE IDENTITY, NOT THE MAIL ONE -- and this is a RETRACTION.
    #
    # This briefly served `client_guid`, the id a client names itself with in
    # its `u/account` fetch, on the reasoning that a self-match can only work
    # against something the client recognises. That is true of MAIL, where
    # `_mail_recipient_row` resolves that id or the message renders "To:
    # Unknown User". It is the wrong identity space here, and SE's own bytes
    # say so: Lex's member record carries `0x00511c75` at +0x08, which is
    # exactly what Lex's own 0:9 handle record carries at ITS +0x08. The
    # roster is compared against the HANDLE identity, and `_handle_list_entries`
    # already insists that identity is `handle_guid` ("MUST equal what 2:3
    # serves for the same friend").
    #
    # Serving `client_guid` made it worse in a legible way: with the field at
    # zero every member rendered one place early (DeckTester as "Lex",
    # LaptopTest2 as "DeckTester"); with `client_guid` in it every member
    # rendered as "Lex", i.e. the field is now READ and still matches nobody.
    #
    # WARNING: `handle_guid` in this field has never actually reached a client. It
    # was live for 33 minutes, during which both clients were working from a
    # group list cached at an earlier login, and `client_guid` replaced it
    # before either relogged. So it is UNTESTED, not disproven.
    # POL_GROUP_MEMBER_SELFGUID=1 puts `client_guid` back for an A/B.
    ident = guid
    if self_guid and os.environ.get("POL_GROUP_MEMBER_SELFGUID", "0") == "1":
        ident = self_guid
    # *** THE LOW 32 BITS ARE THE PROFILE z_hid, AND THAT IS THE TAGGED ROW
    # ID. *** SE's own bytes say so: Yui's member record carries 0x0027509D
    # here, which is exactly her select1 profile z_hid -- the tagged
    # one-id-space form (friend-search-add-guid-zero), not a raw guid low
    # half. We served handle_guid's raw low bits, so "View Profile" on a
    # group row sent z_hid 0x11 (the bare handle id -- live
    # 2026-08-22T04:27:17), which names nobody and fell back to the
    # VIEWER's own profile. The tagged form is what the profile resolver's
    # friend-row branch already accepts, so a group-row click resolves the
    # same way a friend-list click does. Only applied when the id is one
    # of OUR handle_guids -- a foreign guid's low bits are left as they
    # were. POL_GROUP_MEMBER_ZHID=0 restores the raw low bits.
    low = int(ident) & 0xFFFFFFFF
    if os.environ.get("POL_GROUP_MEMBER_ZHID", "1") == "1" \
            and accounts is not None \
            and (int(ident) & ~0xFFFFFFFF) == accounts.HANDLE_GUID_BASE:
        low = friendlist._FRIEND_HID_TAG | (int(ident) & 0x7FFFF)
    packed |= low                           # bits 0..31 -- see the docstring
    return packed & 0xFFFFFFFFFFFFFFFF


def _group_member_record(guid, name, slot=0, cls=None, self_guid=None):
    """One 32-byte 07:12 member record.

        +0x00  u64  member id -- read at 0x1011cdc0 as [edi]/[edi+4] and resolved
        +0x08  u64  packed: MEMBER GUID in bits 0..31, class @bit50 (3 bits,
                    2..5), slot @bit53 (6 bits)
        +0x10  15B  name, NUL-terminated, cp932

    KEY: **BITS 0..31 OF `+0x08` ARE THE MEMBER'S OWN GUID, AND WE SERVED ZERO.**
    Decoded 2026-08-16 from SE's `QUIET CORNER` reply, and it cross-checks
    exactly: `0x00511c75` in Lex's member record is the same value Lex's own 0:9
    handle record carries at its `+0x08`.

        Lex  +0x00 694d516c8701b001  +0x08 751c5100 00001400  "Lex"
        Yui  +0x00 91bc041e2c008c00  +0x08 9d502700 00001000  "Yui"
                                          ^^^^^^^^ guid   ^^ class 5 / 4

    Why it matters, and why it is not cosmetic: polcore finds the CLIENT ITSELF
    in this list by guid -- `0x37e8917` copies a member's class into the GROUP's
    flags "when the member's guid equals the client's own id". With this
    field zero for everybody, no member is distinguishable from any other, and
    the client resolves its own identity in the group to whichever record matches
    first -- the owner. Reported live 2026-08-16 by a joining member: the name
    over the chat bar was the OWNER's, "as if they're typing as me", with an
    empty sidebar and a user count that never updated.

    Bits 0..31 cannot collide with the class at bit 50 or the slot at bit 53, so
    this only fills a field that was empty. POL_GROUP_MEMBER_GUID=0 reverts it
    for a clean A/B.

    `cls` is the member's stored class; `mclass=` in the control file still
    overrides it for sweeping, which is why the knob reads with the stored value
    as its default rather than a constant.
    """
    out = bytearray(handlelists._GROUP_MEMBER_REC)
    # KEY: **+0x00 IS MASKED, AND IT IS THE FIELD THE SELF-MATCH USES.** Read out of
    # the decompiled installer (`FUN_037e86c0`) after three guesses at +0x08 all
    # failed on a live client:
    #
    #     lVar9  = FUN_037dff10();                     // the client's OWN id
    #     lVar10 = cft_0355(*param_2, param_2[1]);     // member record +0x00
    #     if (lVar10 == lVar9) { ...this member is ME... }
    #
    # and the two helpers are three lines each:
    #
    #     FUN_037dff10()   -> CONCAT44(DAT_03bc4a8c, DAT_03bc4a88)
    #     cft_0355(lo,hi)  -> CONCAT44(DAT_0386a84c ^ hi, DAT_0386a848 ^ lo)
    #
    # So the test is `stored ^ MASK == own_id`, i.e. **stored = own_id ^ MASK**.
    # We stored it RAW, so it could never match whatever we put in it -- which is
    # why zero, `handle_guid` and `client_guid` all failed the same way, and why
    # only the FALLBACK behaviour changed between them.
    #
    # The mask is ours already: SE's member record for Lex holds
    # 0x01b001876c514d69 and their mail record for the same person holds
    # 0x1d973fc20bd85c5a; XOR them and you get 0x1c273e4567891133, which is
    # `_PUSH_GUID_MASK` exactly. The mail path has been masking correctly all
    # along (`_mail_mint` writes `guid ^ _PUSH_GUID_MASK`); this record never did.
    #
    # +0x08 keeps the raw guid: the installer feeds that word through `__allshl`
    # into the member object's own slot/class bits, which is a different use.
    #
    # KEY: **AND THE ID UNDER THE MASK IS THE CLIENT-SPACE ONE.** The mask fix
    # above changed HOW the field is stored but kept `handle_guid` as WHAT is
    # stored -- and the self-match compares against `FUN_037dff10()`, the id the
    # client holds for ITSELF, which `_client_guid_map` documents is never our
    # guid (its docstring even says 7:12 "had never used it"). Reported live
    # 2026-08-18 on prod, fresh group `Example.gang`, DB row verified correct:
    # the owner's row renders with their NAME (that is +0x10 text) but a
    # blank/default badge, the client does not recognise the row as itself,
    # invite stays greyed ("not authorized" -- the class 5 at bit 50 never
    # propagates through 0x37e8917 into the group's flags because the guid half
    # of the compare fails), and they cannot speak in their own group's chat.
    # Zero, raw handle_guid and raw client_guid all failed here before the mask
    # was found; MASKED handle_guid was the live build this report is about.
    # Masked client_guid is the one combination the evidence points at and the
    # only one never served. Fallback to handle_guid when a member has no
    # captured client id -- wrong for self-match but resolvable as a peer, which
    # is the pre-existing behaviour. POL_GROUP_MEMBER_SELF00=0 reverts.
    # WARNING: **THE MASK WAS WRONG -- +0x00 IS THE RAW client_guid. MEASURED LIVE
    # 2026-08-19 (`tools/readself.py` against the running Viewer):**
    #
    #     K       (guid key)     = 0x000000006b906aed
    #     own_id  (FUN_037dff10) = 0x000000e15313b2cb
    #     client_guid (Lex)      = 0x000000e13883d826
    #     own_id ^ K             = 0x000000e13883d826  == client_guid EXACTLY
    #
    # The self-match is `cft_0355(member+0x00) == own_id`, and `cft_0355(x) =
    # x ^ K` (K = polcore `[0x0386A848]`, the PER-SESSION guid key -- NOT
    # `_PUSH_GUID_MASK`). The client holds its OWN id already mangled as
    # `client_guid ^ K` (= own_id). So the match needs
    # `member+0x00 ^ K == client_guid ^ K`, i.e. **member+0x00 = client_guid,
    # RAW**. Serving `client_guid ^ _PUSH_GUID_MASK` gave `cft_0355` =
    # `client_guid ^ MASK ^ K != own_id` -> "client sees a stranger", which is
    # the owner-not-recognised / invite-greyed report (2026-08-19, prod).
    #
    # This RETRACTS the f46e2295 mask: SE's two-record XOR that "gave
    # _PUSH_GUID_MASK" was a coincidence of that capture, not the transform the
    # client runs -- the live K settles it. Same rule as the 2:3 friend guid and
    # the 2:6 reply guid: serve the RAW id, the client applies K on its side; the
    # server never needs K. The auth-band roster PUSH keeps its own masking (a
    # different record on a different band, and the photos it drives work).
    # POL_GROUP_MEMBER_MASK=1 restores the (wrong) mask for an A/B.
    ident = int(guid) & 0xFFFFFFFFFFFFFFFF
    if self_guid and os.environ.get("POL_GROUP_MEMBER_SELF00", "1") == "1":
        ident = int(self_guid) & 0xFFFFFFFFFFFFFFFF
    if os.environ.get("POL_GROUP_MEMBER_MASK", "0") == "1":
        ident ^= pushchannel._PUSH_GUID_MASK
    struct.pack_into("<Q", out, 0x00, ident & 0xFFFFFFFFFFFFFFFF)
    packed = _group_member_packed(guid, cls, slot, self_guid=self_guid)
    struct.pack_into("<Q", out, 0x08, packed & 0xFFFFFFFFFFFFFFFF)
    raw = str(name).encode("cp932", "replace")[:15]
    out[0x10:0x10 + len(raw)] = raw
    return bytes(out)


def _groups_for_list(count):
    """The groups the 07:12 reply will carry, in reply order, truncated to
    `count` -- the one definition both the length and the records use.

    Each entry is `(group_id, name, owner_handle_id)` and BOTH halves matter:

    **A GROUP YOU ARE IN, NOT ONLY ONE YOU OWN.** This used to return
    `_db_friends(kinds=(KIND_GROUP,))` -- the session handle's own group rows --
    so an invitee who had accepted, and was correctly sitting in `group_member`,
    still got an EMPTY group list. Their client showed no group chat to enter and
    the friend list kept saying "Inviting into Group", because nothing ever
    turned the invitation into a group they hold. Reported live 2026-08-16.

    **ONE STABLE ID, THE OWNER'S ROW.** The id is what the client turns into the
    chat channel `#XXL<id>`. It used to come from the group's stored guid, and a
    group whose guid is 0 (every group made before guids were written) fell back
    to `slot + 1` -- the group's POSITION IN THE REQUESTING USER'S LIST. Two
    members therefore computed different channels for the same group and chatted
    into separate rooms, which is why messages never arrived even when both sides
    could see the group. The owner's `friend` row id is unique, non-zero and the
    same number for everybody, which is exactly what the id has to be.

    WARNING: This CHANGES the channel of existing groups. That is not a migration
    hazard -- a channel is not persisted anywhere -- but a client sitting in the
    old one must rejoin to meet anybody.
    """
    if accounts is None:
        return []
    out, seen = [], set()
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            hid = lobbysession._session_handle_id(db)
            if not hid:
                return []
            # Owned first, in the order the friend list already serves them, so
            # an owner's list does not reshuffle now that members are included.
            for r in db.execute(
                    "SELECT id, peer_name FROM friend WHERE handle_id = ? "
                    "AND kind = ? ORDER BY id", (int(hid), accounts.KIND_GROUP)):
                if int(r["id"]) not in seen:
                    seen.add(int(r["id"]))
                    out.append((int(r["id"]), r["peer_name"], int(hid)))
            # Then the ones we are a MEMBER of. `group_member.group_id` IS the
            # owner's friend-row id (see accounts.group_id), so it doubles as the
            # stable identity and no second lookup is needed to find the owner.
            for r in db.execute(
                    "SELECT f.id, f.peer_name, f.handle_id FROM group_member m "
                    "JOIN friend f ON f.id = m.group_id "
                    # pending = 0: an invitee does not HOLD the group until they
                    # accept -- the invitation reaches them as a message, not as
                    # a list entry, and acceptance flows back on the mail path
                    # (_group_join_from_message), so hiding the group here does
                    # not break their ability to accept it.
                    "WHERE m.member_handle = ? AND m.pending = 0 "
                    "AND f.kind = ? ORDER BY f.id",
                    (int(hid), accounts.KIND_GROUP)):
                if int(r["id"]) not in seen:
                    seen.add(int(r["id"]))
                    out.append((int(r["id"]), r["peer_name"], int(r["handle_id"])))
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  7:12 group list lookup failed ({exc!r})")
        return []
    return out[:count]


#: Width of the guid half of the 0:9 identity word -- bits 1..44, see below.
_HANDLE_GUID_BITS = (1 << 44) - 1

#: Schema index of `z_ficon` in the profile record, i.e. the handle's face icon.
_PROFILE_FICON = 19


def _group_record(rec, slot, name, guid=0, code=b""):
    """One 07:12 (`KGetGroupList`) group header record, 136 bytes.

    LAYOUT READ OFF THE CLIENT (2026-08-12), not guessed. The record installer is
    polcore **0x37e86c0**, reached from the 7:12 machine (its request encoder call
    site at 0x37e8446 pushes type 7 / opcode 0x0C), and the caller at 0x37e866f
    computes the source as `session + i*136 + 0xC0` -- `shl ecx,4; add ecx,eax;
    lea edx,[esi+ecx*8+0xC0]`, i.e. stride 17*8 = **136**, which is where the
    record size comes from.

        +0x00  u32   group id LOW        \\ together a 64-bit GROUP ID, used as
        +0x04  u32   group id HIGH       / the lookup key (find @0x37e7d40)
        +0x08  50 x UTF-16LE  NAME       -- 100 bytes, 0x08..0x6B
        +0x6C  u16   (not read here)
        +0x6E  u8    -> obj[0x0C] bits 4..9   (6 bits, & 0x3F)
        +0x6F  u8    -> obj[0x0C] bits 10..12 (3 bits; 0 means "clear", else -1)
        +0x70  20B   byte string -> obj+0x10  (NUL-terminated, cp932)
        +0x84  u8    (not read here)
        +0x85  u8    -> obj[0x0C] bits 1..3   (3 bits, & 7)

    **The name is UTF-16LE, and that is what was wrong before.** The copy at
    0x380dcb0 moves WORDS (`mov ax, word ptr [edx]`; `add edi,2`) for at most 0x32
    = 50 of them, stopping at a NUL *word*, into obj+0x3030 -- and the caller then
    writes the terminator at obj+0x3094, which is exactly 0x3030 + 100. The old
    code wrote cp932 bytes at +0x10 and +0x20, so the client read the first word
    at +0x08, found 0x0000, and stopped: **every group rendered with an empty
    name.** The two offsets it used were 0:9's, borrowed on the strength of the
    shared 136-byte size; the size is shared, the layout is not.

    **The id must be NON-ZERO.** `find @0x37e7d40` opens with
    `or eax,esi; je -> return 0`, so an all-zero id never matches an existing
    slot; 0x37e7d80 then allocates a *fresh* slot on every refresh, and there are
    only four (base 0x3bb06c0, stride 0x3098, `cmp ecx,4`). A group served with
    id 0 therefore consumes a new slot each time the list is fetched until the
    table is full. We fall back to `slot + 1` when the DB row has no guid.

    Unknowns stay ZERO deliberately: +0x6E/+0x6F/+0x85 are bitfields in the
    object's flags dword whose meanings are not established, and zero is the
    value that reads as "not set" everywhere else in this protocol.
    """
    out = bytearray(rec)
    gid = int(guid) & 0xFFFFFFFFFFFFFFFF
    if gid == 0:                        # see the docstring: id 0 never matches
        gid = (slot & 0x3F) + 1
    if _group_cfg("id", "db") == "index":       # ignore the DB guid, use slot+1
        gid = (slot & 0x3F) + 1
    struct.pack_into("<II", out, 0x00, gid & 0xFFFFFFFF, (gid >> 32) & 0xFFFFFFFF)
    wide = str(name).encode("utf-16-le", "replace")[:100]
    out[0x08:0x08 + len(wide)] = wide   # NUL-word terminated by the zero fill
    # The three bitfield bytes we have never had a value for. They are the only
    # remaining degrees of freedom in a reply that otherwise passes every gate
    # polcore applies, so they are sweepable rather than hardcoded at 0.
    # +0x85 -> obj[0x0C] bits 1..3: NOTE 0x37e7d10 rejects a group whose
    # `obj[0x0C] & 0xE == 4`, i.e. class **2 is a REJECT here** -- the opposite
    # of the member class, where 2 is the lowest ACCEPTED value. Do not carry
    # the member value over to the group.
    out[0x6E] = _group_cfg_int("f6e", 0) & 0xFF
    out[0x6F] = _group_cfg_int("f6f", 0) & 0xFF
    out[0x85] = _group_cfg_int("f85", 0) & 0xFF
    # +0x70 IS THE ROW LABEL, and it is NOT the same string as the name.
    # MEASURED 2026-08-13, both ends:
    #   polcore 0x37e8757  copies 0x14 bytes from record+0x70 to group obj +0x10
    #   app.dll 0x48946bd  CSelectGroupData::CSelectGroupData(group) draws the
    #                      row from `[edi+0x10]` when the valid bit is set, and
    #                      from the canned "** no group **" when it is not
    # So the group-chat list renders +0x70, while the UTF-16 name at +0x08 (which
    # becomes obj+0x3030) is used elsewhere. Serving +0x70 empty gave three VALID
    # rows with BLANK labels -- indistinguishable on screen from no list at all,
    # and confirmed live: the client's own table read back `str@0x10=b''` for all
    # three groups while their names at +0x3030 were correct.
    # Default it to the group's name; `code=` in the control file overrides.
    label = code if code else name
    raw = (label if isinstance(label, (bytes, bytearray))
           else str(_group_cfg("code", label)).encode("cp932", "replace"))[:19]
    out[0x70:0x70 + len(raw)] = raw
    return bytes(out)


def _group_create(pt):
    """Persist the group named by a 07:01 request, and answer with its ID.

    Without this the create was acknowledged and discarded: nothing in this file
    ever wrote a group row, so the group survived only as the client's own local
    copy and vanished on the next 4:6 fetch. The READ path already worked -- 4:6
    serves `KIND_GROUP` rows as 32-byte entries via `_friend_payload` -- so
    storing the row here is the whole fix.

    **THE ID WE RETURN IS THE ONE 7:12 WILL SERVE**, and that identity is the
    whole point of returning it: `_groups_for_list` keys a group by the OWNER's
    `friend` row id, which is what `accounts.add_friend` hands back here. Return
    anything else -- including 0 -- and the client holds one id from the create
    and a different one from the next list, i.e. two `#XXL` channels for one
    group, which is the same split-channel failure `_groups_for_list` was
    written to fix.

    A failure still answers b"" (all zero). That is the old behaviour, and it is
    the right one: a group we could not store must not be given an id that will
    not be there on the next fetch.

    The guid is stored as ZERO to match SE's captured group entry, which is the
    only observation of one on the wire:

        00000000 00000000 | 00000000 | 00000001 | "LexGroup"

    i.e. a null guid AND a zero handle id, unlike a person. `add_friend` would
    otherwise synthesise a non-zero guid, which is right for a friend (a blank
    guid renders as a blank row) but is not what SE does for a group.
    """
    if accounts is None:
        return b""
    if pt is None or len(pt) <= friendput._GROUP_NAME_OFF:
        return b""
    end = pt.find(b"\x00", friendput._GROUP_NAME_OFF)
    if end < 0:
        end = min(len(pt), friendput._GROUP_NAME_OFF + friendput._GROUP_NAME_MAX)
    name = pt[friendput._GROUP_NAME_OFF:end].decode("cp932", "replace").strip()
    if not name:
        log("lobby", "  group 7:1: no name in the request; nothing stored")
        return b""
    made = None
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            mid = lobbysession._session_member_id()
            h = db.execute("SELECT id, handle_name FROM handle WHERE member_id = ?"
                           " ORDER BY is_primary DESC, id ASC LIMIT 1",
                           (mid,)).fetchone() if mid else None
            if h is None:
                log("lobby", f"  group 7:1: {name!r} but no handle for member "
                             f"{mid!r}; nothing stored")
                return b""
            gid = accounts.add_friend(db, int(h["id"]), name,
                                      kind=accounts.KIND_GROUP, guid=0)
            made = gid
            # THE CREATOR IS A MEMBER OF THEIR OWN GROUP, and it is stored rather
            # than assumed. polcore only sets a group's valid bit when at least
            # one member is accepted, so a group with no members is invalid and
            # never renders -- `_group_members` falls back to the owner for rows
            # created before membership existed, and this is what keeps new ones
            # off that fallback.
            if gid is not None:
                # ...as its MASTER. SE serves class 5 for the creator of every
                # group; storing the plain-member class here is what left the
                # invite button greyed out with "You are not authorized to invite
                # group friends". `_group_role` repairs older rows on the way out,
                # but new ones should be right in the database.
                accounts.add_group_member(db, gid, h["handle_name"],
                                          member_handle=int(h["id"]),
                                          cls=accounts.GROUP_CLASS_MASTER)
            log("lobby", f"  group 7:1: stored {name!r} for handle "
                         f"{h['handle_name']!r} (id {h['id']}) with its owner "
                         f"as the first member; answering with group id "
                         f"{made} (channel #XXL{int(made or 0):016X})")
        finally:
            db.close()
    except Exception as exc:
        # A create the client already treats as successful must not become a
        # dropped connection because the DB was busy -- log and answer as before.
        log("lobby", f"  group 7:1: store failed ({exc!r}); reply unchanged")
        return b""
    if not made:
        return b""
    out = bytearray(friendput._GROUP_CREATE_REPLY)
    struct.pack_into("<Q", out, friendput._GROUP_CREATE_ID_OFF,
                     int(made) & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", out, friendput._GROUP_CREATE_F08_OFF,
                     _group_cfg_int("create_f08", 0) & 0xFFFFFFFF)
    # The caller signs bytes [0:n-4] and writes the checksum at [n-4:n], so the
    # 12 bytes here fill a 16-byte payload exactly -- do not pad it to 16.
    return bytes(out)


#: 07:03 `KChgGrpMemClass` -- the ROLE CHANGE. DECODED 2026-08-15 from four live
#: requests whose before/after the account holder narrated, and all four
#: checksums verify, so these are read fields and not a pattern match:
#:
#:     +0x00  u64  group id          -- the id WE served in 7:12
#:     +0x08  u32  entry count       -- 1 in all four
#:     +0x10  u64  target member id  -- the id we put at member record +0x00
#:     +0x18  u32  new class << 8    -- i.e. the class is the byte at +0x19
#:     ...         4-byte checksum last
#:
#: The four samples, against the narration ("assigned sub-master, then master;
#: the account holder leaves; roles handed the other way"):
#:
#:     Yui -> 4     Yui -> 5     Lex (self) -> 1     Yui -> 4
#:
#: which fixes the ladder as **5 master, 4 sub-master, 1 = leave/remove**. 4 and
#: 5 both appear as member classes in SE's own 7:12 reply, and `accounts`
#: already had GROUP_CLASS_MASTER/SUBMASTER at those values. Class 1 falls
#: outside polcore's 2..5 range check, which is how a leave expresses itself:
#: the member stops passing the check and drops out of the list.
_GROUP_CLASS_GID_OFF = 0x00
_GROUP_CLASS_COUNT_OFF = 0x08
_GROUP_CLASS_ENTRY_OFF = 0x10
_GROUP_CLASS_ENTRY = 0x10               # target u64 + class u32 + 4 pad


#: 07:0B `KChgMyGrpStatus` / `KChgMyAllGrpOnlineStatus` -- "my status in this
#: group". One live sample, 2026-08-15, 116-byte body + the 4-byte checksum:
#:
#:     +0x00  u64  group id        (0x2cf6d, the group just created)
#:     +0x6F  u8   status          (1)
#:     everything else zero
#:
#: `+0x6F` is the SAME offset the 7:12 group HEADER carries a 1 in (SE's reply
#: has it set; ours serves 0), which is the strongest hint about what it means:
#: the client is telling us a per-group flag and SE reflects it back in the list.
_GROUP_STATUS_GID_OFF = 0x00
_GROUP_STATUS_FLAG_OFF = 0x6F


def _group_my_status(pt):
    """Read a 07:0B. Returns b"" -- the reply is header-only.

    WARNING: **THIS RECORDS NOTHING YET, ON PURPOSE.** What the client means by the flag
    is one sample deep, there is no column to put it in, and the half that would
    matter -- what the server sends the OTHER members when it changes -- has
    never been observed: SE answers 7:0B with an empty payload and the capture
    shows no follow-up on either band that can be tied to it.

    So this parses and LOGS, which is the step that is actually missing. Every
    theory about the group sidebar (who is present, the member count) currently
    rests on one request nobody has watched arrive. A live log line saying which
    group, which flag, and how often it repeats is what turns that into evidence
    -- and it costs nothing, because the reply was already header-only.
    """
    if pt is None:
        return b""
    body = pt[friendput._GROUP_NAME_OFF:]
    if len(body) < _GROUP_STATUS_FLAG_OFF + 1 + 4:
        log("lobby", f"  group 7:11: body is {len(body)}B, too short to read")
        return b""
    gid = struct.unpack_from("<Q", body, _GROUP_STATUS_GID_OFF)[0]
    flag = body[_GROUP_STATUS_FLAG_OFF]
    # Name every other non-zero byte too: with one captured sample the layout is
    # a sketch, and a second shape shows up here rather than in a re-decode.
    extra = [f"+{i:#04x}={body[i]:#04x}" for i in range(len(body) - 4)
             if body[i] and not (i < 8 or i == _GROUP_STATUS_FLAG_OFF)]
    log("lobby", f"  group 7:11 KChgMyGrpStatus: group {gid} "
                 f"(#XXL{int(gid):016X}) flag={flag}"
                 + (f"  OTHER NON-ZERO BYTES: {' '.join(extra)}" if extra else "")
                 + " -- parsed only, nothing stored (see _group_my_status)")
    return b""


def _group_mode_push(db, gid, member_name, op):
    """Spool a `MODE #XXL<gid> +o/-o` for a member whose class just changed.

    Runs on the LOBBY side, which holds no chat sockets at all -- rooms live in
    the authserv process (see `RoomRegistry`) -- so this goes through the same
    spool the presence and mail pushes use, and `_push_deliver_gmode` does the
    fan-out on the other side.

    Named by MEMBER, not by nick: the nick belongs to the live session and only
    authserv can see it.
    """
    try:
        row = db.execute("SELECT member_id FROM handle WHERE handle_name = ?",
                         (member_name,)).fetchone()
        if row is None or not row["member_id"]:
            log("lobby", f"  group 7:3: {member_name!r} has no member id; "
                         f"the chat channel was not told")
            return
        pushspool._push_emit({"kind": "gmode", "gid": int(gid),
                    "member": int(row["member_id"]), "op": bool(op)})
    except Exception as exc:
        # The class change already succeeded; failing to announce it must not
        # undo it or drop the connection.
        log("lobby", f"  group 7:3: MODE push not spooled ({exc!r})")


def _group_delete(pt):
    """Apply a 07:02 KDeleteGroup. Returns b"" -- the reply is header-only.

    BUILT 2026-09-05 WITHOUT A WIRE SAMPLE. `KDeleteGroup` is named in the
    client's RTTI/function table (request len 0x10) and had no arm at
    all, so deleting a group never persisted: the owner's next 7:12 re-served it
    and the group came back. Zero captures carry a 7:2, so the request layout is
    INFERRED from its length and from 7:3, which is the same family: the u64
    group id at payload +0x00 -- the id WE served in 7:12, i.e. the owner's
    `friend` row -- then pad and the 4-byte checksum. If a live 7:2 ever logs
    "no group row", read the hexdump before touching this offset.

    Only the OWNER's request deletes. Leaving a group is a 7:3 with class 1 (the
    ladder in accounts.py), so a 7:2 from a non-owner is logged and ignored
    rather than guessed at. Other members' 7:12 is login-time-only, so they see
    the group go at their next list refresh; nothing is pushed (unmeasured).
    POL_GROUPS=0 disables this arm with the rest of the group family.
    """
    if accounts is None or pt is None:
        return b""
    body = pt[friendput._GROUP_NAME_OFF:]
    if len(body) < 8:
        log("lobby", f"  group 7:2: request body is {len(body)}B, too short to "
                     f"hold a group id; nothing changed")
        return b""
    gid = struct.unpack_from("<Q", body, 0)[0]
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            owner = db.execute("SELECT handle_id, peer_name FROM friend "
                               "WHERE id = ? AND kind = ?",
                               (int(gid), accounts.KIND_GROUP)).fetchone()
            if owner is None:
                log("lobby", f"  group 7:2: no group row {gid} -- either the "
                             f"client named an id we never served or the id is "
                             f"not at payload +0x00; nothing changed")
                return b""
            hid = lobbysession._session_handle_id(db)
            if hid and int(owner["handle_id"]) != int(hid):
                log("lobby", f"  group 7:2: {owner['peer_name']!r} (id {gid}) is "
                             f"owned by handle {owner['handle_id']}, request came "
                             f"from handle {hid} -- not the owner; nothing changed")
                return b""
            n = accounts.delete_group(db, gid, owner_handle_id=owner["handle_id"])
            log("lobby", f"  group 7:2: deleted {owner['peer_name']!r} (id {gid}) "
                         f"and {n} member row(s); other members see it go at "
                         f"their next 7:12")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  group 7:2: failed ({exc!r}); nothing changed")
    return b""


#: Lobby 1:10 -- PS2 `sqprofdb: Update chlist`. Layout read off the SENDER,
#: `polpex_0014e8e0` in the decrypted PS2 core (pol.pex, base 0x00101000),
#: 2026-09-11. Its encoder
#: call is literally `polpex_00143968(ctx, 1, 10, 0x248)`.
_CHR_PUT_PAYLOAD_OFF = 0x28    # the 40-byte lobby request header every arm skips
_CHR_PUT_BODY = 0x248          # 584: what prod logged as payload_len, twice
_CHR_PUT_SLOTS = 0x40          # the console walks its whole 64-slot table
_CHR_PUT_BLOCKS_OFF = 0x40     # 64 x 8-byte binding blocks after the 64 order bytes
_CHR_PUT_CKSUM_OFF = 0x244     # 4-byte trailer: `polpex_00143e98(.., verify=1)` sums
                               # body[:0x244] with the same dword sum as _lobby_cksum
_CHR_PUT_BOUND, _CHR_PUT_UNBOUND = 1, 2


def _group_class_change(pt):
    """Apply a 07:03 role change. Returns b"" -- the reply is header-only.

    Nothing downstream of this needed writing: `group_member.class` exists,
    `list_group_members` returns it and `_group_member_record` serves it. Until
    now the client's promotions simply went nowhere, so the next 7:12 re-served
    the old class and the role snapped back.

    **The target is matched against the guid WE SERVED.** The client echoes back
    the 8 bytes it read at member record +0x00, which `list_group_members`
    derives live via `handle_guid` -- so the lookup goes through that same list
    rather than through `member_guid` in the table, which can be stale. A target
    we cannot name is logged and skipped, never guessed at.
    """
    if accounts is None or pt is None:
        return b""
    body = pt[friendput._GROUP_NAME_OFF:]
    if len(body) < _GROUP_CLASS_ENTRY_OFF + _GROUP_CLASS_ENTRY + 4:
        log("lobby", f"  group 7:3: request body is {len(body)}B, too short to "
                     f"hold one entry; nothing changed")
        return b""
    gid = struct.unpack_from("<Q", body, _GROUP_CLASS_GID_OFF)[0]
    want = struct.unpack_from("<I", body, _GROUP_CLASS_COUNT_OFF)[0]
    # Trust the BODY over the count field. Every capture has count 1 in a 40-byte
    # body, so a multi-entry request is unobserved shape; reading more entries
    # than the body can hold would walk into the checksum and beyond.
    room = (len(body) - 4 - _GROUP_CLASS_ENTRY_OFF) // _GROUP_CLASS_ENTRY
    count = max(0, min(int(want), room))
    if count != want:
        log("lobby", f"  group 7:3: request declares {want} entries but the "
                     f"{len(body)}B body holds {room}; applying {count}")
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            # include_pending: 7:3 is driven from the OWNER's screen, whose
            # roster is the only one that lists pending invitees -- a target
            # the owner can see must resolve here (remove-invite, most
            # obviously, names a pending member).
            roster = accounts.list_group_members(db, gid, include_pending=True)
            # EVERY identity dialect the +0x00 field has ever served resolves
            # here. The client echoes back the 8 bytes it read at member record
            # +0x00 -- which since 25b04c80's companion fix (f46e2295) is the
            # member's CLIENT-space id under _PUSH_GUID_MASK. Measured live
            # 2026-08-18T23:33: two 7:3s arrived carrying 0x1c273f0fd772c34b /
            # 0x1c273ec3683af391, which unmask to DeckTester's and LaptopTest2's
            # client_guids exactly -- and this lookup, still keyed on our raw
            # guid alone, skipped both ("no member with guid ...; skipped").
            # Raw and masked forms of BOTH spaces are accepted so a client
            # holding a roster from either build still resolves.
            by_guid = {}
            selfg = _client_guid_map()
            for g, nm, _c in roster:
                by_guid[int(g)] = nm
                by_guid[int(g) ^ pushchannel._PUSH_GUID_MASK] = nm
                cg = selfg.get(nm)
                if cg:
                    by_guid[int(cg)] = nm
                    by_guid[int(cg) ^ pushchannel._PUSH_GUID_MASK] = nm
            owner = db.execute("SELECT handle_id, peer_name FROM friend "
                               "WHERE id = ? AND kind = ?",
                               (int(gid), accounts.KIND_GROUP)).fetchone()
            if owner is None:
                log("lobby", f"  group 7:3: no group row {gid}; nothing changed")
                return b""
            # WHO MAY. Without this, any session could move any member of
            # any group (promote itself to master, remove the others). So:
            # anyone may take THEMSELVES out; a master may change anyone but
            # the owner; a sub-master may only change plain members and
            # invitees, and not raise anyone above member. See `_xxl_gate`.
            gate = os.environ.get("POL_GROUP_GATE", "1") != "0"
            me_mid = lobbysession._session_member_id()
            my_cls = accounts.group_class_of(db, gid, member_id=me_mid) if me_mid else None
            my_names = {r["handle_name"] for r in db.execute(
                "SELECT handle_name FROM handle WHERE member_id = ?",
                (int(me_mid or 0),))}
            owner_name = db.execute("SELECT handle_name FROM handle WHERE id = ?",
                                    (int(owner["handle_id"] or 0),)).fetchone()
            owner_name = owner_name["handle_name"] if owner_name else None
            cls_of = {nm: int(c) for _g, nm, c in roster}
            for i in range(count):
                off = _GROUP_CLASS_ENTRY_OFF + i * _GROUP_CLASS_ENTRY
                target = struct.unpack_from("<Q", body, off)[0]
                cls = (struct.unpack_from("<I", body, off + 8)[0] >> 8) & 0xFF
                name = by_guid.get(target)
                if name is None:
                    log("lobby", f"  group 7:3: {owner['peer_name']!r} "
                                 f"(id {gid}): no member with guid "
                                 f"{target:#018x} -- our roster is "
                                 f"{sorted(by_guid.values())}; skipped")
                    continue
                if gate:
                    self_leave = cls < accounts.GROUP_CLASS_MIN and name in my_names
                    by_master = my_cls == accounts.GROUP_CLASS_MASTER and name != owner_name
                    by_sub = (my_cls == accounts.GROUP_CLASS_SUBMASTER and name != owner_name
                              and cls_of.get(name, 0) <= accounts.GROUP_CLASS_MEMBER
                              and cls <= accounts.GROUP_CLASS_MEMBER)
                    if not (self_leave or by_master or by_sub):
                        log("lobby", f"  group 7:3: member {me_mid} (class {my_cls}) may "
                                     f"not move {name!r} to class {cls} in "
                                     f"{owner['peer_name']!r}; refused")
                        continue
                if cls < accounts.GROUP_CLASS_MIN:
                    # LEAVE / REMOVE. See the note above: class 1 is not a class.
                    accounts.remove_group_member(db, gid, name)
                    log("lobby", f"  group 7:3: {name!r} LEFT "
                                 f"{owner['peer_name']!r} (class {cls})")
                    # `friend.peer_name` is the GROUP's name, not the owner's --
                    # comparing the leaver's name against it never matches. The
                    # owner is named by `handle_id`, so compare the id we would
                    # have SERVED for it against the target the client sent.
                    if int(owner["handle_id"] or 0) and target == \
                            accounts.handle_guid(int(owner["handle_id"])):
                        log("lobby", "  group 7:3: that was the group's OWNER -- "
                                     "`_group_members` falls back to the owner "
                                     "for a group with no stored members, so an "
                                     "emptied group will still list them")
                elif cls <= accounts.GROUP_CLASS_MAX:
                    if accounts.set_group_member_class(db, gid, name, cls):
                        log("lobby", f"  group 7:3: {name!r} -> class {cls} "
                                     f"({_GROUP_CLASS_NAMES.get(cls, '?')}) in "
                                     f"{owner['peer_name']!r}")
                        # ...AND TELL THE CHAT CHANNEL. SE moves the channel's
                        # '@' the moment the class moves, so a promotion is
                        # visible without rejoining -- see _push_deliver_gmode.
                        _group_mode_push(db, gid, name,
                                         cls >= accounts.GROUP_CLASS_MASTER)
                    else:
                        log("lobby", f"  group 7:3: {name!r} is in the roster but "
                                     f"no row moved; nothing changed")
                else:
                    log("lobby", f"  group 7:3: class {cls} for {name!r} is above "
                                 f"polcore's {accounts.GROUP_CLASS_MAX}; ignored")
        finally:
            db.close()
    except Exception as exc:
        # Same rule as 7:1: a role change the client already shows as applied
        # must not become a dropped connection because the DB was busy.
        log("lobby", f"  group 7:3: apply failed ({exc!r}); nothing changed")
    return b""


#: Only for the log line -- the wire carries the number.
_GROUP_CLASS_NAMES = {5: "master", 4: "sub-master", 3: "member", 2: "member"}


#: KEY: A GROUP INVITE AND ITS ACCEPTANCE ARE **MESSAGES**, not group opcodes.
#: DECODED 2026-08-15 from the narrated session. Both are ordinary `3:1` object
#: writes whose object carries a 32-byte machine-readable TRAILER after the human
#: text, and that trailer is what makes them recognisable:
#:
#:     <subject> 07 <body> 00 | <u64 group id> <24B cp932 group name>
#:
#: The whole message set of the capture separates on it perfectly:
#:
#:     "Would you like to join a friend group?" / "...the group \"QUIET CORNER\"?"
#:                                              -> trailer 32B, body PRESENT
#:     "Group registration accepted"            -> trailer 32B, body EMPTY
#:     "Let's be friends!", "Friend registration accepted", "RE: Ahoy!", ...
#:                                              -> trailer 0B
#:
#: So the FRIEND notifications and every ordinary Message carry no trailer at
#: all: presence of the trailer means "this is about a group", and the body tells
#: invite from accept. That is a structural test, not a string match, which
#: matters because the text is localised and ours must work on a JP client too.
_GROUP_MSG_TAIL = 0x20                  # u64 id + 24-byte name
_GROUP_MSG_NAME_OFF = 0x08


def _group_message(data):
    """`(gid, name, kind)` for a group invite/acceptance object, else None.

    `kind` is "invite" (the object has a body) or "accept" (it does not).
    """
    if not data:
        return None
    _subject, sep, rest = data.partition(b"\x07")
    if not sep:
        return None                      # not a subject/body object at all
    body, sep2, tail = rest.partition(b"\x00")
    if not sep2 or len(tail) < _GROUP_MSG_TAIL:
        return None                      # an ordinary Message, or a friend one
    gid = struct.unpack_from("<Q", tail, 0)[0]
    if not gid:
        return None
    name = tail[_GROUP_MSG_NAME_OFF:_GROUP_MSG_TAIL].split(b"\x00")[0]
    return gid, name.decode("cp932", "replace"), ("invite" if body else "accept")


def _group_join_from_message(gid, gname, kind, meta):
    """Apply a group invite/acceptance that named its group by ID.

    WHO JOINS depends on the direction, and with the id in hand both are direct:

      * an **acceptance** is sent BY the accepter (measured: the account holder
        left the group, was invited back, and their own client sent `Group
        registration accepted`), so the new member is this session's handle and
        no addressing has to be resolved at all;
      * an **invitation** is sent TO the invitee, so the member is the message's
        recipient, read from the `O/m/` path metadata.

    WARNING: An INVITATION STORES A FULL MEMBER, which is the behaviour this function
    inherited and deliberately does not change here. It is questionable -- an
    invitee who never accepts still gets the group in their 7:12 -- but polcore
    has no "pending" value in its 2..5 class range to express the halfway state,
    and making the change would alter a flow that currently works. Flagged, not
    silently altered.
    """
    def _accept_roster_push(db, gid, gname, owner_hid):
        # THE ROSTER, PUSHED, THE MOMENT AN ACCEPT SETTLES. 7:12 is fetched at
        # login only, so a mid-session acceptance leaves every client's group
        # section on its local state: the new member keeps the invite dialog's
        # leftovers ("Inviting into group" with only themself -- live
        # 2026-08-22T04:27, DeckTestNew) and the owner never sees the invitee
        # flip to member. SE pushes exactly this record mid-session -- the
        # QUIET CORNER capture has the role handoffs arriving as ev=(group<<8)
        # roster pushes, class stepping 2->3->4 --
        # and push_group_rosters is that carrier. Pushed to every settled
        # member; the spool drops anyone offline. Guarded like every push from
        # a handler (the POL-0008 lesson). POL_GROUP_ACCEPT_ROWPUSH=0 reverts.
        if _group_cfg("rowpush",
                      os.environ.get("POL_GROUP_ACCEPT_ROWPUSH", "1")) == "1":
            try:
                mems = list(accounts.list_group_members(db, int(gid)))
                orow = db.execute("SELECT handle_name FROM handle WHERE id = ?",
                                  (int(owner_hid),)).fetchone()
                if orow and not any(nm == orow["handle_name"]
                                    for _g, nm, _c in mems):
                    # A pre-7:1 group stores no owner row; the 7:12 compose
                    # re-adds them and the pushed roster must agree with it.
                    mems.insert(0, (accounts.handle_guid(int(owner_hid)),
                                    orow["handle_name"],
                                    accounts.GROUP_CLASS_MASTER))
                entries = [[int(gid), [[int(g), str(nm), int(c)]
                                       for g, nm, c in mems]]]
                # COUNT WHAT WE ACTUALLY KNOW: how many members we queued a push
                # for, and how many had no handle row to queue against.
                #
                # WARNING: This used to sum `_push_emit`'s return and call the total
                # "spool line(s)", which reads as a failure and is not one:
                # `_push_emit` RETURNS 0 UNCONDITIONALLY AFTER SPOOLING (it is a
                # delivery count, and a spooled record has not been delivered
                # yet). So the line printed "0 spool line(s)" precisely when
                # spooling had SUCCEEDED -- five `push spooled: grouprows` lines
                # sat directly above it saying so. Anyone reading the log went
                # hunting a spool failure that never happened. Measured
                # 2026-08-26 on a real group accept.
                queued, unresolved = 0, []
                for _g, nm, _c in mems:
                    hrow = db.execute(
                        "SELECT member_id FROM handle WHERE handle_name = ?",
                        (nm,)).fetchone()
                    if hrow:
                        pushspool.push_group_rosters(None, int(hrow["member_id"]), entries)
                        queued += 1
                    else:
                        unresolved.append(nm)
                log("lobby", f"  group accept: roster push for {gname!r} -- "
                             f"{len(mems)} member(s), {queued} queued"
                             + (f", NO HANDLE ROW for {unresolved!r}"
                                if unresolved else ""))
            except Exception as exc:
                log("lobby", f"  group accept: roster push skipped ({exc!r})")

    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            row = db.execute("SELECT id, peer_name, handle_id FROM friend "
                             "WHERE id = ? AND kind = ?",
                             (int(gid), accounts.KIND_GROUP)).fetchone()
            if row is None:
                # A group on another server, or one we have lost. The message is
                # already stored and delivered; inventing a row for it would be
                # worse than letting the client show an invitation we cannot act
                # on.
                log("lobby", f"  group {kind}: id {gid} ({gname!r}) is not a group "
                             f"row here -- message delivered, nothing joined")
                return
            if kind == "accept":
                member_id = lobbysession._session_handle_id(db)
                if not member_id:
                    log("lobby", f"  group accept for {row['peer_name']!r}: no "
                                 f"handle bound to this session; nothing joined")
                    return
            else:
                peer = lobbymail._mail_recipient_row(db, meta["recipient_guid"]) \
                    if meta else None
                if peer is None:
                    rg = int(meta["recipient_guid"]) if meta else 0
                    log("lobby", f"  group invite to {row['peer_name']!r} (id {gid}): "
                                 f"cannot name the recipient {rg:#x}; nothing joined"
                                 + (lobbymail._mail_recipient_raw_note(db, rg) if meta else ""))
                    return
                member_id = int(peer["id"])
                # ONLY A MASTER OR SUB-MASTER INVITES (the PC's own rule: "You
                # are not authorized to invite group friends"), and the server
                # now holds it too -- an invite from anyone else stays a
                # message and adds nobody. POL_GROUP_GATE=0 turns it off.
                if os.environ.get("POL_GROUP_GATE", "1") != "0":
                    me_mid = lobbysession._session_member_id()
                    inviter = accounts.group_class_of(db, int(gid), member_id=me_mid) \
                        if me_mid else None
                    if inviter is None or inviter < accounts.GROUP_CLASS_SUBMASTER:
                        log("lobby", f"  group invite to {row['peer_name']!r} (id {gid}) "
                                     f"from member {me_mid} (class {inviter}): not a "
                                     f"master or sub-master; message delivered, "
                                     f"nobody invited")
                        return
            h = db.execute("SELECT handle_name FROM handle WHERE id = ?",
                           (int(member_id),)).fetchone()
            if h is None:
                log("lobby", f"  group {kind}: handle {member_id} is gone; "
                             f"nothing joined")
                return
            # **A JOIN MUST NOT DEMOTE.** `add_group_member` upserts the class, so
            # re-applying a message for somebody already in the group rewrites
            # their rank to the plain-member default -- and messages DO repeat:
            # the PC Viewer re-sends its own notification a few seconds later
            # (see `_mail_is_own_echo`), and the master accepting a re-invite to
            # their own group would come back as a class-2 member of it. Joining
            # is "be in this group", never "be this rank".
            already = db.execute(
                "SELECT class, pending FROM group_member WHERE group_id = ? "
                "AND member_name = ?", (int(gid), h["handle_name"])).fetchone()
            if already is not None:
                # An ACCEPTANCE for a pending row is the whole point of the
                # flow -- clear the flag rather than "leaving as is" (which is
                # exactly how an invitee would stay invisible-but-invited
                # forever). Any other repeat really is left alone.
                if kind == "accept" and int(already["pending"] or 0):
                    accounts.confirm_group_member(db, int(gid),
                                                  h["handle_name"])
                    log("lobby", f"  group accept: {h['handle_name']!r} "
                                 f"CONFIRMED into {row['peer_name']!r} "
                                 f"(id {gid}, was pending) -- channel "
                                 f"#XXL{int(gid):016X}")
                    _accept_roster_push(db, int(gid), row["peer_name"],
                                        int(row["handle_id"]))
                else:
                    log("lobby", f"  group {kind}: {h['handle_name']!r} is "
                                 f"already in {row['peer_name']!r} at class "
                                 f"{already['class']}; left as is")
                return
            # AN ACCEPTANCE NEEDS AN INVITATION. It used to store a full member
            # when no row existed, so an accept-shaped message naming any group
            # id made its sender a member of that group. Now it only confirms a
            # pending row (above). POL_GROUP_GATE=0 restores the old path.
            if kind == "accept" and os.environ.get("POL_GROUP_GATE", "1") != "0":
                log("lobby", f"  group accept: {h['handle_name']!r} was never invited "
                             f"to {row['peer_name']!r} (id {gid}); nothing joined")
                return
            # An INVITE stores a PENDING member -- visible to the owner alone
            # until the acceptance clears the flag.
            ok = accounts.add_group_member(db, int(gid), h["handle_name"],
                                           member_handle=int(member_id),
                                           cls=accounts.GROUP_CLASS_MEMBER,
                                           pending=(0 if kind == "accept"
                                                    else 1))
            log("lobby", f"  group {kind}: {h['handle_name']!r} "
                         f"{'joined' if ok else 'REJECTED (full?) for'} "
                         f"{row['peer_name']!r} (id {gid}, named by the message's "
                         f"own trailer)"
                         + (" as PENDING until they accept" if ok and
                            kind != "accept" else "")
                         + f" -- channel #XXL{int(gid):016X}")
            if ok and kind == "accept":
                # An acceptance for an invite we never saw settles a member
                # directly -- push the roster for that path too.
                _accept_roster_push(db, int(gid), row["peer_name"],
                                    int(row["handle_id"]))
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  group {kind}: not stored ({exc!r})")


def _capture_group_invite(path, data):
    """Store group membership off a 3:1 invitation / registration message.

    WHY THIS LIVES ON THE MAIL PATH. Friends have a whole-list PUT (2:6) and the
    server learns membership from it. **Groups have no PUT at all** -- the opcode
    family is create (7:1), delete (7:2), change-class (7:3), my-status (7:11)
    and the GET (7:12), and nothing writes a roster. So the only place a group's
    membership is ever stated on the wire is the invitation itself, which SE
    sends as an ordinary `O/m/` message: kinds 0x870A (invite) and 0x878A
    (registered) sit right beside 0x8080/0x8480 for friends.

    That is also why `group_member` has been empty on our server while groups
    themselves worked: every group fell back to owner-only in `_group_members`,
    the client had nobody to draw in the chat sidebar, and there was no code path
    that could ever have added a second name.

    Both directions are stored, because both are informative:
      * an INVITE names the group and the invitee -> record them as invited;
      * a REGISTERED reply means they accepted -> mark them an active member.

    Deliberately tolerant: this is a side effect of a message write, and a
    message that does not parse must never cost the caller its object store.
    """
    if accounts is None:
        return
    meta = lobbymail._mail_meta(path)
    tagged = _group_message(data)
    # EITHER witness is enough. The mail `kind` is the historical gate; the
    # object's own trailer is the stronger one, and a message carrying a group id
    # is about a group whatever the path metadata says.
    if tagged is None and (not meta or meta.get("kind") not in (
            lobbymail.MAIL_KIND_GROUP_INVITE, lobbymail.MAIL_KIND_GROUP_REGISTERED)):
        return
    if tagged is not None:
        # KEY: THE GROUP ID IS IN THE MESSAGE. Everything below this point exists to
        # recover a group by its NAME, and it never had to: the object ends in
        # `<u64 group id><24B name>` (decoded 2026-08-15, see `_group_message`).
        # The id is exact, survives duplicate names, and needs no guess about
        # which end owns the group.
        #
        # The fallback below is what this replaces, and it was already known to
        # be failing: an acceptance quotes no name, so the regex missed it and
        # the "longest printable run" rescue was reading the NAME FIELD OF THIS
        # VERY TRAILER without knowing what it was ("Group registration accepted
        # \x00\x02\x00...\x00LexGoons" is `\x00` end-of-body, then id 2, then the
        # name). It is kept for a message that carries no trailer at all.
        gid, gname, kind = tagged
        _group_join_from_message(gid, gname, kind, meta)
        return
    body = data.replace(b"\x07", b" ").decode("cp932", "replace")
    m = resourcestore._GROUP_INVITE_RE.search(body) or resourcestore._GROUP_INVITE_RE.search(meta["subject"])
    gname = m.group(1) if m else None
    if gname is None:
        # THE ACCEPTANCE DOES NOT QUOTE THE NAME. An invite reads
        #     Would you like to join the group "LexGoons"?
        # but the reply is a STRUCTURE with the name in a fixed field:
        #     'Group registration accepted \x00\x02\x00\x00\x00\x00\x00\x00\x00
        #      LexGoons\x00\x00\x00...'
        # so the quoted-name pattern found nothing and every acceptance was
        # dropped with "names no quoted group" (seen live 2026-08-16 15:45:24).
        # Take the longest printable run after the subject instead -- the name is
        # the only text in there.
        runs = [s for s in re.split(rb"[\x00-\x1f]+", data)
                if 1 <= len(s) <= 64 and s.strip()]
        # drop the subject/lead text; the name is what is left, longest first
        cand = [s.decode("cp932", "replace").strip() for s in runs[1:]]
        cand = [s for s in cand if s and " " not in s[:1]]
        gname = max(cand, key=len) if cand else None
    if not gname:
        log("lobby", f"  group invite: kind {meta['kind']:#06x} names no group "
                     f"-- nothing stored ({body[:60]!r})")
        return
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            # The OWNER of the group is whoever is logged in when the invite goes
            # out; for a registration reply it is the other way round, so resolve
            # the group by name against BOTH ends and take whichever exists.
            hid = lobbysession._session_handle_id(db)
            peer = lobbymail._mail_recipient_row(db, meta["recipient_guid"])
            peer_id = int(peer["id"]) if peer is not None else None
            gid = accounts.group_id(db, hid, gname) if hid else None
            member_id = peer_id
            if gid is None and peer_id is not None:
                gid = accounts.group_id(db, peer_id, gname)
                member_id = hid                 # they own it, we are joining
            if gid is None or not member_id:
                log("lobby", f"  group invite: no group named {gname!r} on either "
                             f"side -- nothing stored")
                return
            row = db.execute("SELECT handle_name FROM handle WHERE id = ?",
                             (int(member_id),)).fetchone()
            if row is None:
                return
            ok = accounts.add_group_member(
                db, gid, row["handle_name"], member_handle=int(member_id),
                cls=accounts.GROUP_CLASS_MEMBER)
            log("lobby", f"  group invite: {row['handle_name']!r} "
                         f"{'stored in' if ok else 'REJECTED for (full?)'} "
                         f"{gname!r} (group {gid}, kind {meta['kind']:#06x})")
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  group invite: not stored ({exc!r})")


# --------------------------------------------------------------------------- #
# The lobby opcode table's entries for groups (see lobbyops.py). POL_GROUPS=0
# turns every one of them off: the request then falls through to the generic
# path, i.e. the old discard with an all-zero reply, which leaves a 7:1 tail
# probe usable.
# --------------------------------------------------------------------------- #
def _groups_on():
    return os.environ.get("POL_GROUPS", "1") == "1"


def payload_create(n, req_pt):
    """7:1 CREATE GROUP. Writes the DB row AND answers with the group's id,
    which is what the client keys its `#XXL` channel on -- see _group_create."""
    if not _groups_on():
        return None
    return _group_create(req_pt)


def payload_class_change(n, req_pt):
    """7:3 ROLE CHANGE. Called for its side effect -- the reply is header-only
    (n = 0), so there is no payload to build and b"" is the whole answer."""
    if not _groups_on():
        return None
    return _group_class_change(req_pt)


def payload_delete(n, req_pt):
    """7:2 DELETE GROUP. Header-only reply like the other group writes; the
    side effect is the whole answer -- see _group_delete."""
    if not _groups_on():
        return None
    return _group_delete(req_pt)


def payload_my_status(n, req_pt):
    """7:B MY GROUP STATUS. Parsed and logged only -- see _group_my_status."""
    if not _groups_on():
        return None
    return _group_my_status(req_pt)
