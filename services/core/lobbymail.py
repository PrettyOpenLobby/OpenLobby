"""POL Message mail on the lobby band: mailbox payloads, minting, threads, notices."""
import os
import re
import struct
import time
from srvcore import log
from .deps import accounts
from . import friendlist, lobbyreply, lobbysearch, lobbysession, logingate, pushchannel, pushspool, resourcestore



def _mail_count():
    try:
        return max(0, int(os.environ.get("POL_LOBBY_MAIL", "0"), 0))
    except ValueError:
        return 0


def _mail_paylen(count):
    return lobbyreply.MAIL_COUNT_BLOCK + count * lobbyreply.MAIL_RECORD + 4


def _mailbox(member=None):
    """[(when, path, meta)] of the messages addressed TO `member`, newest first.

    THREE fields, not two -- the decoded meta rides along so callers do not
    re-parse the token. It said "[(when, path)]" until 2026-08-16, and a caller
    that believed it unpacked two and raised.

    Scans every stored `O/m/` object and resolves each one's recipient from its
    own path, rather than trusting the filename -- which is also what lets the
    canonical name (`m.<token>.bin`) and the older per-member one
    (`<member>.O_m_<token>.bin`) sit in the same store during the changeover.
    A message under both names is listed ONCE, under the canonical path, so the
    inbox never shows a duplicate and the row's token is the one a 3:0 will
    resolve.
    """
    if member is None:
        member = lobbysession._session_get("member_id")
    if member is None or accounts is None:
        return []
    try:
        # the canonical names, and an older build's per-member `.O_m_` ones
        names = (resourcestore._res_list(scope="mail", suffix=".bin")
                 + resourcestore._res_list(prefix="O_m_", suffix=".bin"))
    except Exception as exc:
        log("lobby", f"  mail: cannot list the store ({exc!r})")
        return []
    try:
        db = accounts.connect()
    except Exception:
        return []
    out, seen = [], set()
    try:
        for name in sorted(names):
            path = _mail_path_of(name)
            if path is None:
                mark = ".O_m_"
                at = name.find(mark)
                if at < 0 or not name.endswith(".bin"):
                    continue
                path = lobbysearch._MAIL_PATH_PREFIX + name[at + len(mark):-len(".bin")]
            key = _mail_name(path)
            if key is None or key in seen:
                continue
            meta = _mail_meta(path)
            if not meta:
                continue
            row = _mail_recipient_row(db, meta["recipient_guid"])
            if row is None or int(row["member_id"]) != int(member):
                continue
            seen.add(key)
            # A dead acceptance is retired HERE, at listing time, whatever
            # filed it -- a client 3:1, our mint, or the adopt path carrying it
            # across builds -- because listing is the one gate every copy
            # passes on its way to a reader. See `_mail_stale_acceptance`.
            if os.environ.get("POL_MAIL_STALE_ACCEPT", "1") == "1" \
                    and _mail_stale_acceptance(db, member, meta):
                _mail_retire(path, suffix=".stale",
                             why="its reader no longer holds a friend row for "
                                 "the sender, so reading it can only raise the "
                                 "18161 resend prompt",
                             knob="POL_MAIL_STALE_ACCEPT")
                continue
            out.append((meta["when"], path, meta))
    finally:
        db.close()
    out.sort(key=lambda r: r[0], reverse=True)
    return out


#: (recipient handle id, asker handle id) -> when `_friend_request_heal` last
#: restored or minted that request. Bounds the heal to once per TTL, so a
#: request one device keeps acknowledging is not handed back on every listing.
_FRIEND_HEAL_DONE = {}


def _friend_request_heal(member=None):
    """Give back friend requests this member holds but can no longer see.

    Found 2026-09-27: the mailbox is per MEMBER, and the PS2 fetches and 3:2s new
    messages ~10 s after login, so `_mail_retire` took Amara's and Birdie's
    requests before Lex's PC ever listed them. 2:3 hides `invited` rows by
    design, so nothing else shows them and the request is stranded.

    At listing time, for every `invited` friend row of this member whose asker
    still holds a PENDING row for them and has no live 0x8080 request addressed
    to that handle: restore the newest retired copy (`.bin.read` / `.bin.stale`
    -> `.bin`, the undo `_mail_retire` documents), else mint a fresh one the way
    the website's befriend does. Each (handle, asker) pair is acted on at most
    once per POL_FRIEND_REQUEST_HEAL_TTL seconds (default 6 h). Returns the
    number healed. POL_FRIEND_REQUEST_HEAL=1 turns it on (default off).
    """
    if os.environ.get("POL_FRIEND_REQUEST_HEAL", "0") != "1" or accounts is None:
        return 0
    if member is None:
        member = lobbysession._session_get("member_id")
    if member is None:
        return 0
    try:
        ttl = float(os.environ.get("POL_FRIEND_REQUEST_HEAL_TTL") or "21600")
    except ValueError:
        ttl = 21600.0
    healed = 0
    try:
        db = accounts.connect()
    except Exception:
        return 0
    try:
        want = []
        for r in db.execute(
                "SELECT f.handle_id, f.peer_handle, f.peer_name, h.handle_name"
                " FROM friend f JOIN handle h ON h.id = f.handle_id"
                " WHERE h.member_id = %s AND f.status = %s AND f.kind = %s",
                (int(member), accounts.STATUS_INVITED,
                 accounts.KIND_FRIEND)).fetchall():
            peer = None
            if r["peer_handle"]:
                peer = db.execute("SELECT * FROM handle WHERE id = %s",
                                  (int(r["peer_handle"]),)).fetchone()
            if peer is None:
                peer = accounts.handle_by_name(db, r["peer_name"])
            if peer is None:
                continue                # not one of ours: nobody to send it
            asks = db.execute(
                "SELECT status FROM friend WHERE handle_id = %s AND peer_name = %s",
                (int(peer["id"]), accounts.friend_row_name(
                    db, int(peer["id"]), r["handle_name"]))).fetchone()
            if asks is None or asks["status"] != accounts.STATUS_PENDING:
                continue                # the asker no longer waits on this
            want.append((int(r["handle_id"]), peer))
        if not want:
            return 0
        # Every request message stored, live or retired, by (recipient handle,
        # sender). A sender is matched by our guid or by the 15-byte name field.
        live, retired = set(), {}
        try:
            stored = resourcestore._res_list_info(scope="mail")
        except Exception as exc:
            log("lobby", f"  mail: request heal cannot list the store ({exc!r})")
            stored = []
        mtimes = {nm: at for nm, _size, at in stored}
        for nm in mtimes:
            suffix = next((s for s in (".read", ".stale")
                           if nm.endswith(".bin" + s)), "")
            base = nm[:-len(suffix)] if suffix else nm
            path = _mail_path_of(base)
            meta = _mail_meta(path) if path else None
            if not meta or meta.get("kind") != MAIL_KIND_FRIEND_REQUEST:
                continue
            rrow = _mail_recipient_row(db, meta["recipient_guid"])
            if rrow is None:
                continue
            srow = (accounts.handle_by_guid(db, meta.get("sender_guid") or 0)
                    or accounts.handle_by_client_guid(
                        db, meta.get("sender_guid") or 0))
            keys = {(int(rrow["id"]), "name", meta.get("sender") or "")}
            if srow is not None:
                keys.add((int(rrow["id"]), "id", int(srow["id"])))
            for k in keys:
                if not suffix:
                    live.add(k)
                else:
                    retired.setdefault(k, []).append((mtimes.get(nm) or 0, nm, base))
        now = time.time()
        for hid, peer in want:
            ks = [(hid, "id", int(peer["id"])),
                  (hid, "name", str(peer["handle_name"])[:15])]
            if any(k in live for k in ks):
                continue
            done = (hid, int(peer["id"]))
            if now - _FRIEND_HEAL_DONE.get(done, -ttl - 1) <= ttl:
                continue
            old = sorted(x for k in ks for x in retired.get(k, []))
            if old:
                _at, nm, base = old[-1]
                try:
                    moved = resourcestore._res_rename(nm, base)
                except Exception as exc:
                    moved, why = False, repr(exc)
                else:
                    why = "it is no longer stored"
                if not moved:
                    log("lobby", f"  mail: request heal -- cannot restore {nm!r} "
                                 f"({why})")
                    continue
                _FRIEND_HEAL_DONE[done] = now
                healed += 1
                log("lobby", f"  mail: request heal -- RESTORED "
                             f"{peer['handle_name']!r}'s friend request to handle "
                             f"{hid} (member {member}): the row is still invited "
                             f"and no live request was left ({nm} -> .bin); "
                             "POL_FRIEND_REQUEST_HEAL=0 disables this")
                continue
            if _mail_mint(peer["handle_name"], accounts.handle_guid(int(peer["id"])),
                          accounts.handle_guid(hid), friendlist._FRIEND_REQ_SUBJECT,
                          friendlist._FRIEND_REQ_BODY, kind=MAIL_KIND_FRIEND_REQUEST):
                _FRIEND_HEAL_DONE[done] = now
                healed += 1
                log("lobby", f"  mail: request heal -- MINTED a friend request "
                             f"from {peer['handle_name']!r} to handle {hid} "
                             f"(member {member}): the row is invited and no "
                             "request message was ever stored; "
                             "POL_FRIEND_REQUEST_HEAL=0 disables this")
    except Exception as exc:
        log("lobby", f"  mail: request heal skipped ({exc!r})")
    finally:
        db.close()
    return healed


def _mailbox_payload(n, member=None):
    """The REAL 3:3 mailbox index: one 264-byte record per message.

    Record layout, read off SE's own populated 3:3 reply (polshim-se.429364.log,
    a mailbox holding one message):

        +0x00  u32  unix timestamp
        +0x04  u32  the OBJECT'S LENGTH
        +0x08  the `O/m/` token -- the base64 path, NOT a display string

    **+0x04 IS NOT THE CONSTANT 0x30 IT WAS FIRST READ AS.** One sample made a
    length look like a magic number; 24 of SE's own records (2026-08-16, decoded
    with `lobbydec.py`) carry the same value at path record +0x38, and it tracks
    the object exactly -- 0x1e for a 30-byte "Friend registration accepted",
    0x78 for a 120-byte group invite, 0x0e for a 14-byte "Ahoy!". THIS is where
    the reader gets the length it then declares in its 3:0, so a wrong value
    here truncates the message body it asks for. Ours were all under 0x30, which
    is the only reason the constant appeared to work.

    Everything past the token stayed zero in SE's record and stays zero here.
    """
    rows = _mailbox(member)
    out = bytearray(n)
    fit = max(0, (n - lobbyreply.MAIL_COUNT_BLOCK - 4) // lobbyreply.MAIL_RECORD)
    count = min(len(rows), fit)
    struct.pack_into("<I", out, 0, count)
    for i, (when, path, meta) in enumerate(rows[:count]):
        base = lobbyreply.MAIL_COUNT_BLOCK + i * lobbyreply.MAIL_RECORD
        struct.pack_into("<I", out, base + 0x00, when & 0xFFFFFFFF)
        # The path's own +0x38 is the sender's declaration; the stored object is
        # what we can actually serve. They agree unless a message was stored by
        # an older build, and then the store wins -- serving a length we do not
        # have is what POL-5135 is made of.
        try:
            size = resourcestore._res_size(resourcestore._resource_read_file(path)) or 0
        except Exception:
            size = 0
        struct.pack_into("<I", out, base + 0x04, size or meta.get("size") or 0)
        tok = path[len(lobbysearch._MAIL_PATH_PREFIX):].encode("ascii", "replace")
        room = lobbyreply.MAIL_RECORD - 0x08
        out[base + 0x08:base + 0x08 + min(len(tok), room)] = tok[:room]
    log("lobby", f"  3:3 mailbox: {count}/{len(rows)} message(s) for member "
                 f"{member if member is not None else lobbysession._session_get('member_id')}"
                 + (f" -- newest {rows[0][2]['subject']!r} from "
                    f"{rows[0][2]['sender']!r}"
                    f" (thread {rows[0][2].get('thread')}, kind "
                    f"{rows[0][2].get('kind', 0):#06x}"
                    + (", NOTIFICATION" if rows[0][2].get("notify") else "")
                    + ")" if rows else ""))
    return bytes(out)


def _mail_payload(n):
    """A synthetic mail list. Each 264-byte record is an offset marker naming
    offsets WITHIN the record (`R00`, `R10`, ... NUL-terminated per 16 bytes) and
    carries its own index, so one login maps the record layout the same way
    `markerz` mapped the account record."""
    count = _mail_count()
    out = bytearray(n)
    struct.pack_into("<I", out, 0, count)
    for i in range(count):
        base = lobbyreply.MAIL_COUNT_BLOCK + i * lobbyreply.MAIL_RECORD
        if base + lobbyreply.MAIL_RECORD > n:
            break
        for row in range(0, lobbyreply.MAIL_RECORD, 16):
            # 264 is not a multiple of 16, so the last row of every record is a
            # short 8-byte one -- terminate at the ROW's end, not at row+15.
            row_end = min(row + 16, lobbyreply.MAIL_RECORD)
            tag = f"R{row:02X}#{i}".encode("ascii")[:row_end - row - 1]
            out[base + row:base + row + len(tag)] = tag
            out[base + row_end - 1] = 0
    return bytes(out)


#: The 05:04 **CONTENT** profile record schema -- the per-Content-ID game
#: character, the other record 05:04 can return.
#:
#: MEASURED 2026-08-23 off a live retail session (`work/pc/prof-ffxi-retail-
#: 20260823.log`): the `[prof]` probe dumps the descriptor SE sent, names and
#: all, and the 280-byte record beside it. Values decoded against FFXI's own id
#: spaces four ways -- country 2 = Windurst, zone 238 = Windurst Waters, job 5 =
#: Red Mage, race 5 = Tarutaru Male -- each matching what the Viewer rendered.
#:
#: (index, name, length, type), same encoding as `_PROFILE_SCHEMA`.
#: WARNING: The names are TRUNCATED TO 8 CHARS by the probe, which prints only the
#: descriptor's +0x00/+0x04 dwords: `z_worldn` is `z_worldname`, `z_countr` is
#: `z_country`, `z_joblev` is `z_joblevel`. Re-dump with a longer read before
#: anyone treats these as SE's full column names.
#:
#: THE FIRST SIX ARE GENERIC and the tail is per-content: TM's fields are Player
#: Name / Card Level / Title / Average Rank / Money / Purpose (`Friend.BIN`) and
#: code 3's are its own, so a real code-3/TM record will NOT have
#: FFXI's tail. This schema is FFXI's, and it is the only one measured end to end.
_CONTENT_SCHEMA = (
    (0,  "z_phead",    8, 9), (1,  "z_ctsid",   4, 6), (2,  "z_ctid",    8, 8),
    (3,  "z_name",    16, 1), (4,  "z_purp",    1, 2), (5,  "z_rlang",   1, 2),
    (6,  "z_worldn",  40, 1), (7,  "z_countr",  2, 3), (8,  "z_zoneid",  2, 3),
    (9,  "z_jobid",    2, 3), (10, "z_joblev",  2, 3), (11, "z_raceid",  2, 3),
)

#: SE's own record was 280 bytes with z_phead = 104. Both are hers, not derived:
#: `[a1+0x10] = 280` is the memcpy length the client uses, and 104 is the value
#: SE put in field 0 (the handle record's z_phead is likewise its own used
#: length). The reply payload is 284 = 280 + the 4-byte checksum.
_CONTENT_RECORD = 280


def _b64decode(text):
    """Inverse of `_b64encode` -- POL's substituted base64. Stops at the first
    symbol outside the alphabet, which is how the 'T'-padded tails terminate."""
    out = bytearray()
    for i in range(0, len(text) - 3, 4):
        try:
            v = ((logingate._B64.index(text[i]) << 18) | (logingate._B64.index(text[i + 1]) << 12) |
                 (logingate._B64.index(text[i + 2]) << 6) | logingate._B64.index(text[i + 3]))
        except ValueError:
            break
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return bytes(out)


def _mail_meta(path):
    """{recipient_guid, sender, subject, when} for an `O/m/<token>` path, else None."""
    if not path or not path.startswith(lobbysearch._MAIL_PATH_PREFIX):
        return None
    rec = _b64decode(path[len(lobbysearch._MAIL_PATH_PREFIX):])
    if len(rec) < 0x38:
        return None
    return {
        "recipient_guid": struct.unpack_from("<Q", rec, 0x08)[0] ^ pushchannel._PUSH_GUID_MASK,
        "sender_guid": struct.unpack_from("<Q", rec, 0x00)[0] ^ pushchannel._PUSH_GUID_MASK,
        "sender": rec[0x10:0x20].split(b"\x00")[0].decode("cp932", "replace"),
        "subject": rec[0x20:0x30].split(b"\x00")[0].decode("cp932", "replace"),
        "thread": struct.unpack_from("<I", rec, 0x30)[0],
        "when": struct.unpack_from("<I", rec, 0x34)[0],
        "size": struct.unpack_from("<I", rec, 0x38)[0],
        "kind": struct.unpack_from("<H", rec, 0x3E)[0],
        "notify": rec[0x43],
    }


def _mail_recipient_row(db, guid):
    """The handle a message is addressed to, by either name for it.

    A recipient field can hold our guid (what a client writes when it addresses
    a peer it learned from our friend list) or the recipient's OWN identity value
    (what we write, so the reader recognises itself -- see `accounts.client_guid`
    and `_mail_address_as`). Both have to resolve or the message lands in no
    mailbox at all.
    """
    return (accounts.handle_by_guid(db, guid)
            or accounts.handle_by_client_guid(db, guid))


def _mail_recipient_raw_note(db, to):
    """A log-only description of an UNRESOLVED recipient field. Never resolves.

    KEY: THE FMO SQUADRON INVITE (live 2026-08-26T22:45:17Z) failed on a recipient
    that printed as `0x1c273e4567891130`. That number is `_PUSH_GUID_MASK`
    (`0x1c273e4567891133`) XOR **3** -- `_mail_meta` un-masks the wire field
    before it is printed, so the composer wrote the bare integer 3 in +0x08,
    UNMASKED, while it masked its own sender field normally. It is not a
    K-mangled guid (a K equal to `served ^ MASK ^ 3` would be a 60-bit
    coincidence; the one measured K was `0x6b906aed`). It is a small id in a
    space this resolver does not speak -- and on prod `3` is Lex's handle id,
    Lex's member id AND possibly a group row id at once, so it cannot be
    resolved without guessing, and a wrong guess addresses somebody else's
    mail. This prints the raw
    value and which of our id spaces it NUMERICALLY collides with so the next
    two-machine capture (A invites B, B's ids distinct from A's and from the
    group's) settles which space it is. POL_MAIL_RAW_RECIPIENT_NOTE=0 silences
    it; nothing here changes what is stored or served.
    """
    if os.environ.get("POL_MAIL_RAW_RECIPIENT_NOTE", "1") != "1":
        return ""
    raw = (int(to) ^ pushchannel._PUSH_GUID_MASK) & 0xFFFFFFFFFFFFFFFF
    hits = []
    if raw and raw <= 0xFFFFFFFF:
        try:
            if db.execute("SELECT 1 FROM handle WHERE id = %s", (raw,)).fetchone():
                hits.append(f"handle.id {raw}")
            if db.execute("SELECT 1 FROM member WHERE id = %s", (raw,)).fetchone():
                hits.append(f"member.id {raw}")
            if accounts is not None and db.execute(
                    "SELECT 1 FROM friend WHERE id = %s AND kind = %s",
                    (raw, accounts.KIND_GROUP)).fetchone():
                hits.append(f"group (friend.id) {raw}")
            if db.execute("SELECT 1 FROM handle WHERE id = %s",
                          (raw & 0x7FFFF,)).fetchone() and raw & ~0x7FFFF:
                hits.append(f"tagged z_hid of handle {raw & 0x7FFFF}")
        except Exception:
            pass
    return (f" (raw wire value {raw:#x} -- NOT a K-mangled guid, see "
            f"_mail_recipient_raw_note; numerically collides with "
            f"{', '.join(hits) if hits else 'nothing of ours'}; NOT resolved)")


def _mail_address_as(db, handle_row):
    """The value to ADDRESS this handle by: what its own client calls it, if we
    have ever seen it, else our guid for it."""
    if handle_row is None:
        return None
    return int(handle_row["client_guid"] or 0) or \
        accounts.handle_guid(int(handle_row["id"]))


def _mail_stale_acceptance(db, member, meta):
    """True when a `Friend registration accepted` can only misfire if served.

    The client acts on a 0x8480 by looking its sender up in the local friend
    array by (guid, handle slot) -- `0x4aadaf3` -> `0x488ddba` -- and a miss in
    both the friend and ignore arrays raises string 18161, *"%s is not waiting
    for friend registration. Resend "Let's be friends!" request?"*. So an
    acceptance whose READER holds no friend row at all for its SENDER cannot
    close anything: the round of the friendship it belonged to is over (the row
    went active and was later deleted, or never existed on this side). Serving
    it anyway is exactly the resend prompt.

    Measured 2026-08-18T13:31 in lobby.log: DeckTester's mailbox carried an
    acceptance from PS2Tester adopted from an older build while their list held
    no PS2Tester row; marking it read raised 18161 and re-asked a settled
    friendship. The 600 s window in `_mail_notice_already_there` guards the
    minting side only -- nothing guarded the reading side until this.

    Checked against EVERY handle of the reading member, not just the session's
    active one: a mailbox is per member while friend rows are per handle, and an
    acceptance that still matters to a sibling handle must stay listed. The
    sender must POSITIVELY resolve to a local handle -- an unresolvable sender
    proves nothing, and a wrong guess here destroys mail.
    """
    if meta.get("kind") != MAIL_KIND_FRIEND_ACCEPTED or accounts is None:
        return False
    try:
        srow = (accounts.handle_by_guid(db, meta.get("sender_guid") or 0)
                or accounts.handle_by_client_guid(db,
                                                  meta.get("sender_guid") or 0))
        if srow is None and meta.get("sender"):
            # The record's name field is 15 bytes, the same truncation
            # `_mail_mint` applies, so an exact hit is trustworthy and a miss on
            # a 15-byte value may just be the cut -- resolve, never infer.
            srow = db.execute(
                "SELECT id, handle_name FROM handle WHERE handle_name = %s",
                (meta["sender"],)).fetchone()
        if srow is None:
            return False
        held = db.execute(
            "SELECT 1 FROM friend f JOIN handle h ON f.handle_id = h.id"
            " WHERE h.member_id = %s AND f.kind = %s"
            " AND (f.peer_handle = %s OR f.peer_name = %s)",
            (int(member), accounts.KIND_FRIEND, int(srow["id"]),
             srow["handle_name"])).fetchone()
        return held is None
    except Exception as exc:
        log("lobby", f"  mail: stale-acceptance check skipped ({exc!r})")
        return False


def _mail_owner(path):
    """The member a message BELONGS TO -- its recipient, not whoever sent it.

    Read out of the PATH, so it is the same answer on the sending session and on
    the receiving one. Nothing is FILED under it (see `_resource_file`); it is
    what tells 3:3 whose mailbox a message is in, and what tells a 3:2 write
    whether the writer is the message's author or one of its readers.
    """
    meta = _mail_meta(path)
    if not meta or accounts is None:
        return None
    try:
        db = accounts.connect()
        try:
            row = _mail_recipient_row(db, meta["recipient_guid"])
            return int(row["member_id"]) if row else None
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  mail: cannot resolve recipient of {path[:32]!r} ({exc})")
        return None


#: A MESSAGE OBJECT IS FILED BY ITS PATH, UNDER NO MEMBER AT ALL.
#:
#: THE BUG THIS FIXES, watched end to end in lobby.log on 2026-08-16. Messages
#: were filed under the member whose socket carried the WRITE -- the sender (7,
#: DeckTester) -- while the read resolved the RECIPIENT (1, Lex) and looked for a
#: name that did not exist. So 3:3 listed all four messages (that scan resolves
#: the owner from the path, and always did) and every one of the four 3:0 body
#: fetches was answered `serving 664B (all zero = 'no data stored yet')`. An
#: inbox whose rows all carry an empty message is an inbox that reads as EMPTY,
#: which is exactly what the account holder reported after logging back in.
#: Scoping a message to a member was the mistake: the recipient is already inside
#: the path, so one canonical name per message leaves writer and reader nothing
#: to disagree about.
#:
#: The name also has to be REVERSIBLE, because `_mailbox` reads the path back OUT
#: of it to answer 3:3. The generic flatten below is not: POL's base64 alphabet
#: contains `@`, which flattened to `_` and handed the client a token decoding to
#: a DIFFERENT record -- the 20:47 message came back with its subject mangled to
#: 'fucw'. `@` is the only symbol in `_B64` that is not filename-safe and `-` is
#: the only filename-safe symbol not in `_B64`, so swapping the two is exact.
_MAIL_FILE_PREFIX = "m."


#: WARNING: **THE TOKEN IS CASE-SENSITIVE AND WINDOWS FILENAMES ARE NOT.**
#: POL's substituted base64 alphabet uses both cases, so two DIFFERENT messages
#: can produce tokens that differ only in the case of a letter -- and on a
#: case-insensitive filesystem (every Windows host, and this server's bind mount)
#: they are the same file, so one silently overwrites the other.
#:
#: Not theoretical, and not rare where it matters: four notifications minted to
#: one mailbox differ only in the subject and the sequence, which lands in a
#: handful of token positions. Four mints produced FOUR distinct tokens, each
#: file present immediately after its own write, and THREE files on disk --
#:
#:     …SMoTTTT…   thread 0
#:     …SMotTTT…   thread 3      <- same file as thread 0 on Windows
#:
#: so the recipient's 3:0 for one message was served another message's bytes.
#:
#: The fix is to make the NAME carry the case: `@`->`-` as before, and each
#: uppercase letter becomes `~` + its lowercase. `~` is outside the alphabet and
#: legal in a filename, so this is reversible and collision-free. Names written
#: by older builds have no `~` and are decoded the old way, and
#: `_mail_legacy_names` lists them so a read migrates the file across.
def _mail_token_escape(token):
    """Token -> a filename fragment that survives a case-insensitive filesystem."""
    return re.sub(r"[A-Z]", lambda m: "~" + m.group(0).lower(),
                  token.replace("@", "-"))


def _mail_token_unescape(safe):
    """The inverse. A fragment with no `~` is an older build's name, and the
    substitution is the identity on it -- so this decodes both forms."""
    return re.sub(r"~([a-z])", lambda m: m.group(1).upper(), safe).replace("-", "@")


#: KEY: **RECORD BYTE +0x42 IS A STATE, NOT IDENTITY -- KEEP IT OUT OF THE NAME.**
#:
#: One message wears three different values there, so a token is NOT a stable
#: key for a message until this byte is normalised out of it:
#:
#:     0x00  what a CLIENT writes on its own 3:1
#:     0x02  what every stored `O/m/` path carries, ours and SE's alike
#:     0x03  what the arrival PUSH carries (the one-byte diff `_mail_announce`
#:           already flips, and the reason that flip was ever noticed)
#:
#: MEASURED 2026-08-17, and it cost a message: `PS2Tester`'s client posted its own
#: `Friend registration accepted` with +0x42 = 0x00 and we filed it under that
#: token; two seconds later the SAME client read it back asking for +0x42 = 0x02,
#: which is a different 96-char token, so we answered "no data stored yet" (34
#: zero bytes) and its 3:2 acknowledgement found "nothing to retire". The message
#: was still unread and still unreadable five minutes later, in a mailbox that
#: could never be cleared of it.
#:
#: 0x02 is the canonical value because it is the one every PATH already uses --
#: so our own mints keep the names they have and nothing needs migrating.
#: POL_MAIL_STATE_CANON=0 restores the literal-token naming.
_MAIL_STATE_OFF = 0x42
_MAIL_STATE_CANON = 0x02


def _mail_canon_token(token):
    """`token` with the +0x42 state byte normalised, or `token` if it cannot be."""
    if os.environ.get("POL_MAIL_STATE_CANON", "1") != "1":
        return token
    rec = bytearray(_b64decode(token))
    if len(rec) < _MAIL_STATE_OFF + 1 or rec[_MAIL_STATE_OFF] == _MAIL_STATE_CANON:
        return token
    rec[_MAIL_STATE_OFF] = _MAIL_STATE_CANON
    return logingate._b64encode(bytes(rec))[:len(token)]


def _mail_name(path):
    """The canonical filename for an `O/m/` object, or None if not one."""
    if not path or not path.startswith(lobbysearch._MAIL_PATH_PREFIX):
        return None
    token = path[len(lobbysearch._MAIL_PATH_PREFIX):]
    if not token or re.search(r"[^A-Za-z0-9@_]", token):
        return None                              # not a base64 token; be strict
    return _MAIL_FILE_PREFIX + _mail_token_escape(_mail_canon_token(token)) + ".bin"


def _mail_path_of(name):
    """The `O/m/` path a canonical filename came from, or None."""
    if not (name.startswith(_MAIL_FILE_PREFIX) and name.endswith(".bin")):
        return None
    token = name[len(_MAIL_FILE_PREFIX):-len(".bin")]
    return lobbysearch._MAIL_PATH_PREFIX + _mail_token_unescape(token)


def _mail_legacy_names(path):
    """Files an OLDER build may have written this message to: `<member>.O_m_...`,
    under the sender's id, the recipient's, or `shared`. Kept so a mailbox that
    predates the canonical name is still delivered rather than silently lost."""
    token = path[len(lobbysearch._MAIL_PATH_PREFIX):]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", lobbysearch._MAIL_PATH_PREFIX + token)
    try:
        out = resourcestore._res_list(path="%s.bin" % safe)
    except Exception:
        return []
    # The PREVIOUS canonical name -- raw case, `@`->`-` only. It is the one the
    # case-insensitive collision above was written under, so a mailbox that
    # predates the `~` escaping is still delivered (and migrated on the way past
    # by `_resource_read_file`) rather than silently going empty.
    old = _MAIL_FILE_PREFIX + token.replace("@", "-") + ".bin"
    if old != _mail_name(path) and resourcestore._res_exists(old):
        out.append(old)
    # THE LITERAL, UN-CANONICALISED TOKEN. Anything stored before the +0x42
    # state byte was normalised out of the name (see `_mail_canon_token`) is on
    # disk under the state it happened to be written in -- which for a
    # client-written message is 0x00, not the 0x02 every read asks for. Listing
    # it here is what makes those messages readable again instead of stranding
    # them; the read migrates the file to the canonical name on the way past.
    raw = _MAIL_FILE_PREFIX + _mail_token_escape(token) + ".bin"
    if raw != _mail_name(path) and resourcestore._res_exists(raw):
        out.append(raw)
    return out


#: The KIND word at push-record +0x3E, and the notification marker at +0x43.
#: Read off 24 of SE's own records (2026-08-16): 0x8000 is an ordinary member
#: message, and everything else here is a system notification, which is why they
#: all carry 0x10 at +0x43 while a plain message carries 0.
MAIL_KIND_MESSAGE = 0x8000
MAIL_KIND_FRIEND_REQUEST = 0x8080
MAIL_KIND_FRIEND_ACCEPTED = 0x8480
MAIL_KIND_GROUP_INVITE = 0x870A
MAIL_KIND_GROUP_REGISTERED = 0x878A
#: The rest of the group family. The kind word is the record's +0x3E bitfield:
#: bit 15 valid, bits 7..11 the message TYPE, bits 0..6 the data type (0x0A for
#: a group notice, which carries the group id and name after the text). Types
#: 0x10, 0x12 and 0x13 are named in Project Crystal Server's MessageType, which
#: sends 0x12 and 0x13 to the Viewer; 0x0E and 0x0F above match SE's captures.
MAIL_KIND_GROUP_DECLINED = 0x880A      # type 0x10, the invitee said no
MAIL_KIND_GROUP_REMOVED = 0x890A       # type 0x12, you were removed
MAIL_KIND_GROUP_DISBANDED = 0x898A     # type 0x13, the group was disbanded


def _mail_kind_type(kind):
    """The message TYPE (bits 7..11) of a record's +0x3E kind word."""
    return (int(kind or 0) >> 7) & 0x1F


def _mail_note_minted(sender_hid, recipient_hid, kind):
    """Remember that WE posted this notification, so a client echo can be dropped."""
    now = time.time()
    for k in [k for k, t in resourcestore._MAIL_MINTED.items() if now - t > resourcestore._MAIL_MINTED_TTL]:
        del resourcestore._MAIL_MINTED[k]
    resourcestore._MAIL_MINTED[(int(sender_hid), int(recipient_hid), int(kind))] = now


def _mail_minted_recently(sender_hid, recipient_hid, kind):
    """True if we posted this same notification inside the window."""
    at = resourcestore._MAIL_MINTED.get((int(sender_hid), int(recipient_hid), int(kind)))
    return at is not None and time.time() - at <= resourcestore._MAIL_MINTED_TTL


def _mail_is_own_echo(db, path):
    """True if this client-written message duplicates one WE minted.

    Only ever true for the friend notification kinds: an ordinary message a
    client writes is its own and is never dropped. Resolving both parties to
    handle ids first is what makes the comparison possible at all -- the record's
    two id fields use different vocabularies depending on who filled them.
    """
    meta = _mail_meta(path) or {}
    kind = int(meta.get("kind") or 0)
    if kind not in (MAIL_KIND_FRIEND_REQUEST, MAIL_KIND_FRIEND_ACCEPTED):
        return False
    sender_hid = lobbysession._session_handle_id(db)
    row = _mail_recipient_row(db, meta.get("recipient_guid"))
    if sender_hid is None or row is None:
        return False
    if not _mail_minted_recently(sender_hid, int(row["id"]), kind):
        return False
    log("lobby", f"  3:1 write: DROPPED the client's own copy of "
                 f"{meta.get('subject', '?')!r} (kind {kind:#06x}) -- we already "
                 f"posted this one, and storing both delivers it twice "
                 "(POL_FRIEND_MAIL_DEDUPE=0 keeps it)")
    return os.environ.get("POL_FRIEND_MAIL_DEDUPE", "1") == "1"


def _mail_drop_redundant_request(db, path):
    """True if this is a friend REQUEST to someone already an ACTIVE friend.

    Tetra Master's room member sidebar offers "Ask to become friends" on a
    member the player is ALREADY friends with (a client-side enable bug: it does
    not recognise the room member as the friend it already holds -- see
    tm-member-sidebar-identity). Acting on it makes the client post the peer a
    fresh friend-request mail (kind 0x8080), which our server would relay and
    push -- spamming an existing friend with "<name> wants to be friends".

    Measured 2026-08-21: `Lex` re-friending `LaptopTest2` (already slot 1 of his
    2:3) posted an `Accept Friend Registration` 3:1 to LaptopTest2. The 2:6 that
    rode alongside it was already a correct no-op (`+0 requested`); this is the
    other half.

    Dropped ONLY when all three hold, so a real first request is never lost:
    the mail is a REQUEST (0x8080, not an accept 0x8480 or a group invite), the
    recipient resolves to one of our handles, and the sender ALREADY holds that
    recipient as an `active` friend. A request to a non-friend, or to someone
    held only as pending/invited, is relayed as before.
    POL_FRIEND_NO_REDUNDANT_REQ=0 disables the guard.
    """
    if os.environ.get("POL_FRIEND_NO_REDUNDANT_REQ", "1") != "1":
        return False
    meta = _mail_meta(path) or {}
    if int(meta.get("kind") or 0) != MAIL_KIND_FRIEND_REQUEST:
        return False
    sender_hid = lobbysession._session_handle_id(db)
    recip = _mail_recipient_row(db, meta.get("recipient_guid"))
    if sender_hid is None or recip is None:
        return False
    row = db.execute(
        "SELECT status FROM friend WHERE handle_id = %s AND peer_name = %s",
        (int(sender_hid), recip["handle_name"])).fetchone()
    if row is not None and row["status"] == accounts.STATUS_ACTIVE:
        log("lobby", f"  3:1 write: DROPPED a redundant friend REQUEST to "
                     f"{recip['handle_name']!r} -- already an ACTIVE friend "
                     "(the TM room-sidebar re-friend; the peer keeps the one "
                     "friendship they already have). "
                     "POL_FRIEND_NO_REDUNDANT_REQ=0 relays it.")
        return True
    return False


def _mail_notice_already_there(recipient_member, sender_name, kind, window=600):
    """True if `recipient_member` already holds a live `kind` notice from
    `sender_name`, posted inside `window` seconds.

    THE OTHER HALF OF THE DEDUPE, and the half that was missing. `_MAIL_MINTED`
    drops a client's echo of something WE posted, which only works when we go
    first. Measured 2026-08-17: `PS2Tester` accepting produced the client's own
    3:1 at 02:45:50.016 and our mint at 02:45:50.447 -- the client won by 0.43 s,
    nothing had been recorded to compare against, and `LaptopTest2`'s mailbox
    held TWO `Friend registration accepted` messages for one accept.

    So the check has to run in both directions: `_MAIL_MINTED` for server-first,
    this for client-first. Keyed on the SENDER NAME because that is what the
    record carries at +0x10 in either vocabulary (the guid fields do not agree
    between a client-written record and ours -- see `_mail_normalise`).

    The window is what keeps an OLD notice from suppressing a real new one: a
    friendship can be dropped and remade, and the second request deserves its own
    message. POL_FRIEND_MAIL_DEDUPE=0 disables this with the rest of the dedupe.
    """
    if os.environ.get("POL_FRIEND_MAIL_DEDUPE", "1") != "1":
        return False
    now = time.time()
    for when, _path, meta in _mailbox(recipient_member):
        meta = meta or {}
        if int(meta.get("kind") or 0) != int(kind):
            continue
        if (meta.get("sender") or "") != sender_name:
            continue
        if now - float(meta.get("when") or when or 0) <= window:
            return True
    return False


def _friend_request_greeting(db, peer_handle_id, sender_name, window=600):
    """What the requester actually TYPED, or "" if they typed nothing we hold.

    A friend request is a MESSAGE the requester's own client posts (SE's capture:
    kind 0x8080 `Let's be friend`), and the greeting is that message's subject --
    the 16-byte field `_mail_meta` decodes straight out of the `O/m/` path. So
    this is the requester's own words read back, not a reconstruction.

    Returns "" rather than a stand-in when there is no such message. A client
    that posts none has said nothing, and `_PUSH_EV_FRIEND_REQUEST`'s text field
    means "what this person said to you" -- our own minted wording would be a
    fabrication in it. `window` matches `_mail_notice_already_there`'s for the
    same reason: a friendship can be remade, and an old greeting must not be
    forwarded with a new request.
    """
    try:
        row = db.execute("SELECT member_id FROM handle WHERE id = %s",
                         (int(peer_handle_id),)).fetchone()
        if row is None:
            return ""
        now = time.time()
        for when, _path, meta in _mailbox(int(row["member_id"])):
            meta = meta or {}
            if int(meta.get("kind") or 0) != MAIL_KIND_FRIEND_REQUEST:
                continue
            if (meta.get("sender") or "") != sender_name:
                continue
            if now - float(meta.get("when") or when or 0) > window:
                continue
            subject = (meta.get("subject") or "").strip()
            # Our OWN mint is in this mailbox too when the client posted nothing,
            # and forwarding it back would launder our wording into the
            # requester's mouth. Skip it by value -- the one string we know we
            # wrote ourselves.
            if subject == friendlist._FRIEND_REQ_SUBJECT:
                return ""
            return subject[:pushchannel._PUSH_TEXT_MAX]
    except Exception as exc:                    # never cost the push its send
        log("lobby", f"  2:6 friend write: greeting lookup failed ({exc!r})")
    return ""


def _unread_notice_senders(member_id, kind):
    """Handle names whose `kind` notification is still UNREAD in this mailbox.

    `_mailbox` lists only the live `.bin` objects -- a message the client has
    acknowledged is renamed `.read` and drops out -- so this is exactly "a
    notification they have not processed yet". Used to hold `reconcile_pending`
    off a row whose acceptance the client is still going to act on.
    """
    out = set()
    for row in _mailbox(member_id):
        # (when, path, meta) -- the meta is already decoded, so do NOT call
        # `_mail_meta` again here. Unpacking this as a 2-tuple on the strength of
        # the docstring (which said so, and was stale) raised inside
        # `_db_friends`' broad except and served an EMPTY friend list to anyone
        # who had mail. Fixed and pinned by a test before it reached a client.
        meta = row[2] or {}
        if int(meta.get("kind") or 0) == int(kind) and meta.get("sender"):
            out.add(meta["sender"])
    return out


def _mail_next_thread(recipient_guid):
    """The next `+0x30` for one mailbox. **It is a SEQUENCE, not a thread id.**

    MEASURED 2026-08-15: across every `3:x` record in the capture the field
    climbs 0,1,2,3,…,12 in message order, over sends and receives alike and
    regardless of kind -- a per-mailbox counter, shared between the values a
    CLIENT assigns to its own messages and the ones a server assigns.

    We passed the default 0 for every message we have ever minted, so a mailbox
    holding several server-minted notifications had several messages all claiming
    position 0.

    Counts RETIRED objects too. The sequence keeps climbing on SE as messages are
    read (7 and 8 are live while 3..6 are long gone), so deriving it from the
    live mailbox alone would hand out numbers the client has already used.

    Best-effort: any failure returns 0, which is exactly the old behaviour.
    """
    try:
        want = int(recipient_guid)
        hi = -1
        for nm in resourcestore._res_list(scope="mail"):
            # Retired messages count (see above), so trim the `.read` suffix and
            # let `_mail_path_of` do the decoding -- it knows both the escaped
            # and the older raw-case name, which hand-rolling the token did not.
            base = nm[:-len(".read")] if nm.endswith(".read") else nm
            path = _mail_path_of(base)
            if not path:
                continue
            meta = _mail_meta(path)
            if not meta or int(meta.get("recipient_guid") or 0) != want:
                continue
            hi = max(hi, int(meta.get("thread") or 0))
        return hi + 1
    except Exception as exc:
        log("lobby", f"  mail: cannot derive the next sequence ({exc!r}); using 0")
        return 0


def _mail_sender_slot(sender_guid):
    """The handle-table slot the reader knows this sender by -- record +0x3C.

    Read out of the SAME map `_friend_list_record` uses for a friend row's
    bits 7..12, so a message and a friend row can never name one person two ways.
    A sender with no handle-table entry is slot 0, which is what SE serves for
    every friend that is not a second handle of a member already in the list.
    """
    try:
        return int(friendlist._friend_handle_slots().get(int(sender_guid), 0)) & 0x3F
    except Exception:
        return 0


def _mail_mint(sender_name, sender_guid, recipient_guid, subject, body,
               kind=MAIL_KIND_MESSAGE, thread=None, when=None, sender_slot=None,
               tail=b""):
    """Post a message from the SERVER, and return the `O/m/` path it lives at.

    THE POINT: an `O/m/` path IS the 72-byte push record, so a message we mint
    is indistinguishable from one a client sent -- the recipient's 3:3 lists it
    and its 3:0 serves the object, with no new opcode anywhere.

    This is how SE delivers a FRIEND REQUEST. Its capture carries `Let's be
    friend` (kind 0x8080), `Friend registration accepted` (0x8480) and a group
    invite (0x870A) as messages on this channel, so the friend flow needs no
    separate transport -- which is what "the friend-request push needs the
    message layer" in STATUS was waiting for.
    """
    rec = bytearray(0x48)
    if sender_slot is None:
        sender_slot = _mail_sender_slot(sender_guid)
    # ADDRESS THEM BY A NAME THEY KNOW. The recipient field is the READER, and a
    # client recognises itself only by its own identity value, never by our guid
    # -- which is why an otherwise-working push still rendered "To: Unknown
    # User". Falls back to our guid for an account we have never seen write.
    if accounts is not None:
        try:
            db = accounts.connect()
            try:
                row = _mail_recipient_row(db, recipient_guid)
                recipient_guid = _mail_address_as(db, row) or recipient_guid
            finally:
                db.close()
        except Exception as exc:
            log("lobby", f"  mail: addressing falls back to our guid ({exc})")
    struct.pack_into("<Q", rec, 0x00, int(sender_guid) ^ pushchannel._PUSH_GUID_MASK)
    struct.pack_into("<Q", rec, 0x08, int(recipient_guid) ^ pushchannel._PUSH_GUID_MASK)
    rec[0x10:0x20] = str(sender_name).encode("cp932", "replace")[:15].ljust(16, b"\x00")
    rec[0x20:0x30] = str(subject).encode("cp932", "replace")[:15].ljust(16, b"\x00")
    obj = (str(subject).encode("cp932", "replace") + b"\x07"
           + str(body).encode("cp932", "replace") + b"\x00")
    # `tail` is the binary block some kinds carry after the text's NUL: the
    # group notices put `{u64 group id; char name[0x14]}` there (see
    # `friendgroups._group_notice_tail`). It counts toward +0x38, the way
    # Project Crystal Server sizes the group notices it sends to the Viewer.
    obj += bytes(tail or b"")
    if thread is None:
        thread = _mail_next_thread(recipient_guid)
    struct.pack_into("<I", rec, 0x30, int(thread) & 0xFFFFFFFF)
    struct.pack_into("<I", rec, 0x34, int(when if when is not None else time.time()))
    struct.pack_into("<I", rec, 0x38, len(obj))
    # KEY: +0x3C IS THE SENDER'S HANDLE SLOT, and it is half of how the reader
    # identifies them. app.dll keys a person by (guid_lo, guid_hi, slot) --
    # `0x488c6fc` builds the triple, `0x488ddba` looks it up -- and for a MESSAGE
    # the slot comes from this byte (`0x4aadaf3` reads `record[+0x3C]`), while for
    # a FRIEND it comes from bits 7..12 of that friend's 2:3 head word. The two
    # must agree or the reader does not know who wrote to it.
    #
    # PROVEN on SE's own wire, 24 records against two friend rows, no exceptions:
    #
    #     2:3 head 0xEA13A021 -> bits 7..12 = 0   Yui     22 messages, +0x3C = 0
    #     2:3 head 0x048CE0A1 -> bits 7..12 = 1   Tobin    2 messages, +0x3C = 1
    #
    # -- and Yui and Tobin share ONE guid (`91bc041e2c008c00`), which is the point:
    # they are two handles of one member, so the guid alone cannot tell them apart
    # and the slot is what does.
    #
    # It was hardcoded 0 here. That is still the right answer for every friend we
    # serve today (`_friend_list_record`'s head keeps bits 7..12 clear unless
    # POL_LOBBY_FRIEND_HANDLES puts a friend in the handle table), but deriving it
    # from the same map that fills the row means the two cannot drift apart the
    # day that changes.
    struct.pack_into("<H", rec, 0x3C, int(sender_slot or 0) & 0x3F)
    struct.pack_into("<H", rec, 0x3E, int(kind))
    struct.pack_into("<I", rec, 0x40, 0x000203E8 | (0x10000000 if kind != MAIL_KIND_MESSAGE else 0))
    path = lobbysearch._MAIL_PATH_PREFIX + logingate._b64encode(bytes(rec))
    # Decode our own path back before storing it: if the recipient does not fall
    # out of it, the message is undeliverable and it is better to say so here
    # than to leave an object nobody's mailbox will ever list.
    meta = _mail_meta(path)
    if not meta or meta["recipient_guid"] != int(recipient_guid):
        log("lobby", f"  mail: MINT FAILED -- {path[:40]!r} does not decode back "
                     f"to recipient {int(recipient_guid):#x}")
        return None
    resourcestore._resource_store(path, obj)
    log("lobby", f"  mail: minted {subject!r} from {sender_name!r} to "
                 f"{int(recipient_guid):#x} (kind {kind:#06x}, {len(obj)}B object)")
    _mail_announce(path)
    return path


def _mail_normalise(path, sender_handle_id):
    """Put BOTH parties' real ids in a client-written message's record.

    THE BUG THIS FIXES: "Unknown User" in a message header. A client-written
    record names two people and it names them with two DIFFERENT vocabularies:

        +0x00  the SENDER -- filled with the id that client knows ITSELF by
               (DeckTester's four messages all carry 0x014ab0fbd278, Lex's
               0x0162e92cdc54; constant per sender, and nothing we ever issued)
        +0x08  the RECIPIENT -- filled with OUR guid, because that is what the
               address book handed the composer

    The reader resolves each against the ids it knows and prints "Unknown User"
    for whichever one it cannot place, so the two fields fail in opposite
    directions: the sender field needs OUR guid (the reader knows its friends
    only by the ids we serve) and the recipient field -- the reader itself, the
    one drawn as "To:" -- needs the reader's OWN id, which it never got from us.
    Both are rewritten here, each into the vocabulary of whoever reads it.

    A recipient we have never seen write anything is left addressed by our guid:
    it is no worse than what the client sent, and `_capture_self_guid` means the
    gap closes as soon as that account fetches `u/account`, i.e. on its next
    login.

    The path is the object's key, so rewriting it means the object is STORED and
    LISTED under the corrected path -- which is fine and invisible: the recipient
    only ever learns the path from our own 3:3 reply, and `_mail_recipient_row`
    resolves a mailbox under either id. The sender never reads its sent copy back
    (it keeps that locally), and if it did, the object is still reachable under
    the name it wrote.

    POL_MAIL_NORMALISE=0 stores exactly what the client sent.
    """
    if os.environ.get("POL_MAIL_NORMALISE", "1") != "1" or accounts is None:
        return path
    rec = bytearray(_b64decode(path[len(lobbysearch._MAIL_PATH_PREFIX):]))
    if len(rec) < 0x48:
        return path
    try:
        db = accounts.connect()
    except Exception as exc:
        log("lobby", f"  mail: cannot open the account DB to normalise ({exc})")
        return path
    changed = False
    try:
        if sender_handle_id:
            want = accounts.handle_guid(int(sender_handle_id))
            had = struct.unpack_from("<Q", rec, 0x00)[0] ^ pushchannel._PUSH_GUID_MASK
            # WHAT THIS CLIENT CALLS ITSELF, learned in passing -- the same value
            # `_capture_self_guid` reads off a `u/account` fetch, and the reason a
            # message TO them can be addressed by an id they recognise.
            if had and had != want:
                try:
                    if accounts.learn_client_guid(db, int(sender_handle_id), had):
                        log("lobby", f"  mail: handle {sender_handle_id} calls itself "
                                     f"{had:#x} -- recorded, so messages TO them can "
                                     "be addressed by an id they recognise")
                except Exception as exc:
                    log("lobby", f"  mail: cannot record the sender's own id ({exc})")
                struct.pack_into("<Q", rec, 0x00, want ^ pushchannel._PUSH_GUID_MASK)
                changed = True
                log("lobby", f"  mail: sender field {had:#x} is no guid of ours -- "
                             f"rewritten to {want:#x} so the reader can name the "
                             'sender instead of "Unknown User"')
        # THE RECIPIENT FIELD -- the one the header draws as "To:", and the one
        # the mint path has always readdressed (`_mail_address_as`). A client
        # composes it from the address book, so it holds OUR guid; the reader
        # looks for ITSELF there and does not recognise that value.
        to = struct.unpack_from("<Q", rec, 0x08)[0] ^ pushchannel._PUSH_GUID_MASK
        row = _mail_recipient_row(db, to)
        if row is None:
            log("lobby", f"  mail: recipient {to:#x} is nobody of ours -- left as sent"
                         + _mail_recipient_raw_note(db, to))
        elif not (row["client_guid"] or 0):
            log("lobby", f"  mail: handle {row['id']} has never named itself to us, so "
                         'its header will read "To: Unknown User" until it does')
        elif int(row["client_guid"]) != to:
            addr = int(row["client_guid"])
            struct.pack_into("<Q", rec, 0x08, addr ^ pushchannel._PUSH_GUID_MASK)
            changed = True
            log("lobby", f"  mail: recipient {to:#x} is OUR id for handle {row['id']} -- "
                         f"readdressed to {addr:#x}, the id that client knows itself "
                         'by, so the header can draw a name instead of "Unknown User"')
    finally:
        db.close()
    return (lobbysearch._MAIL_PATH_PREFIX + logingate._b64encode(bytes(rec))) if changed else path


#: The one byte that separates a message's PATH record from its PUSH record.
#: Measured 2026-08-16 by decoding both halves of all 11 pushes in SE's capture:
#: the u32 at +0x40 is `0x100203E8` in the path and `0x100303E8` in the push, and
#: every other byte of the 72 is identical. (SE's push then carries 3 more bytes
#: -- `9c4e87`, `85dd20` -- which are not a sum, xor, crc32 or MD5 prefix of the
#: record; they are left off until the push track's optional-field decoder says
#: what they are. The client is asked to read an object that exists either way.)
_MAIL_PUSH_BYTE = 0x42


def _mail_push_token(path):
    """The base64 record a mail push carries, from the message's own path."""
    rec = bytearray(_b64decode(path[len(lobbysearch._MAIL_PATH_PREFIX):]))
    if len(rec) < 0x48:
        return None
    rec[_MAIL_PUSH_BYTE] = 0x03
    return logingate._b64encode(bytes(rec))


def _mail_announce(path):
    """Push a stored message to its recipient, if they are logged in.

    Called wherever a message lands -- a client's 3:1 send and our own mint --
    so live delivery does not depend on which of the two created it.
    """
    meta = _mail_meta(path)
    token = _mail_push_token(path)
    if not meta or not token or accounts is None:
        return 0
    try:
        db = accounts.connect()
        try:
            row = _mail_recipient_row(db, meta["recipient_guid"])
        finally:
            db.close()
    except Exception as exc:
        log("lobby", f"  mail: cannot resolve the recipient to push to ({exc})")
        return 0
    if row is None:
        return 0
    return pushspool._push_emit({"kind": "mail", "handle": int(row["id"]), "token": token,
                       "subject": meta["subject"], "sender": meta["sender"]})


def _mail_retire(path, suffix=".read", why="the client acknowledged it (3:2)",
                 knob="POL_MAIL_RETIRE"):
    """Take a message out of the mailbox, KEEPING the bytes:
    `m.<token>.bin` -> `m.<token>.bin<suffix>`.

    `_mailbox` lists `.bin` only, so the rename is the whole mechanism -- and it
    is one rename away from being undone. Two callers, two suffixes:
    `.read` for a 3:2 acknowledgement, `.stale` for an acceptance the reader can
    no longer act on (see `_mail_stale_acceptance`); distinct suffixes so a purge
    or an un-retire can tell WHY each message left.
    """
    live = resourcestore._resource_read_file(path)
    meta = _mail_meta(path) or {}
    try:
        moved = resourcestore._res_rename(live, live + suffix)
    except Exception as e:
        moved, e = False, repr(e)
    else:
        e = "no such message stored"
    if not moved:
        log("lobby", f"  mail: retire of {path[:32]!r} -- nothing to retire ({e})")
        return False
    log("lobby", f"  mail: RETIRED {meta.get('subject', '?')!r} from "
                 f"{meta.get('sender', '?')!r} -- {why}, so it leaves the "
                 f"mailbox. Bytes kept as "
                 f"{live}{suffix}; {knob}=0 disables this.")
    return True


def _mail_object(pt):
    """The `[u32 len][object]` block at the tail of a 3:1 frame, or None."""
    if len(pt) < resourcestore._MAIL_OBJ_OFF + 8:
        return None
    n = struct.unpack_from("<I", pt, resourcestore._MAIL_OBJ_OFF)[0]
    if resourcestore._MAIL_OBJ_OFF + 8 + n != len(pt):
        return None                      # not self-consistent: not an object block
    return bytes(pt[resourcestore._MAIL_OBJ_OFF + 4:resourcestore._MAIL_OBJ_OFF + 4 + n])


def _mail_read_len(req_pt):
    """The object length a 3:0 READ declares, at the same fixed offset."""
    if req_pt is None or len(req_pt) < resourcestore._MAIL_OBJ_OFF + 4:
        return None
    return struct.unpack_from("<I", req_pt, resourcestore._MAIL_OBJ_OFF)[0]


# --------------------------------------------------------------------------- #
# The lobby opcode table's entries for the mailbox (see lobbyops.py)
# --------------------------------------------------------------------------- #
def paylen_mailbox(req_pt):
    """3:3 mail list length. THE REAL MAILBOX: the length has to be declared
    here, before the payload is built, so it is counted from the same
    _mailbox() the payload will serve -- an empty box declares 12 (8 + 0 + 4),
    which is exactly what SE sends for an empty mailbox. Stranded friend
    requests come back first, so the count includes them
    (POL_FRIEND_REQUEST_HEAL, default off). POL_LOBBY_MAIL's marker probe,
    when set, declares its own count."""
    if _mail_count():
        return _mail_paylen(_mail_count())      # 8 + count*264 + 4
    _friend_request_heal()
    return _mail_paylen(len(_mailbox()))


def payload_mailbox(n, req_pt):
    """3:3 mail list: the POL_LOBBY_MAIL marker probe when set, else the
    mailbox."""
    if _mail_count():
        return _mail_payload(n)
    return _mailbox_payload(n)
