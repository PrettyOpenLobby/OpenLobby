"""The lobby band's opcode table: one row per request opcode the client sends.

A lobby request names its opcode in two bytes, `(op1, op2)`. For each one the
server may have to do three things, and the row says which functions do them:

    paylen(req_pt)        how many payload bytes the reply declares, or None to
                          let the generic rules decide (the per-opcode table,
                          the POL_LOBBY_PAYLEN override, the default)
    payload(n, req_pt)    the reply's payload bytes, or None to fall through to
                          the generic builders (list records, POL_LOBBY_TAIL)
    capture(pt)           what to harvest from the decrypted request; True when
                          the request was consumed, else the generic scan runs

The dispatchers in paylen.py, lobbyreply.py and lobbycapture.py look the
opcode up here first and keep their generic tails for everything else. The
table is built on first use so the record-family modules it names are loaded
by then; a title can add or replace rows through `register`.

`tools/lobby_opcodes.py` prints this table as the protocol reference.
"""
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Op:
    op1: int
    op2: int
    name: str                                   # the client's own name where known
    doc: str = ""                               # one line on what the exchange is
    paylen: Optional[Callable] = None
    payload: Optional[Callable] = None
    capture: Optional[Callable] = None

    @property
    def key(self):
        return (self.op1, self.op2)

    @property
    def label(self):
        return f"{self.op1:d}:{self.op2:d}"


_OPS = {}
_BUILT = False


def register(op):
    """Add a row, replacing any earlier row for the same opcode."""
    _OPS[op.key] = op
    return op


def _build():
    global _BUILT
    if _BUILT:
        return
    _BUILT = True
    # imported here, not at the top: the record-family modules import the
    # dispatchers that import this table, and the rows name their functions
    from . import (characters, fetchpath, friendgroups, friendput, handlelists,
                   lobbycapture, lobbymail, lobbysearch, lobbysession, memberstatus,
                   paylen, profilerecord, resourcestore)
    rows = [
        # list fetches: the count block and the records are built generically
        # (handlelists._list_count / _list_payload), so these rows only name them
        Op(0x00, 0x07, "handle list (PS2)", "160-byte records; the console's form of 0:9"),
        Op(0x00, 0x09, "handle list", "136-byte records, count in byte 0"),
        Op(0x01, 0x03, "KGetChrList", "the character list, 104-byte records"),
        Op(0x02, 0x03, "KGetFriendList", "the friend list, 168-byte records, count as u32"),
        Op(0x07, 0x0C, "KGetGroupList", "the groups, 136-byte records, at most 4"),
        # PS2-only status probes, sized statically off the console's core
        Op(0x04, 0x00, "current handle write (PS2)", "24 bytes in, 32 back"),
        Op(0x04, 0x01, "current handle (PS2)", "the console's first request on a fresh drive"),
        Op(0x04, 0x04, "current handle (PS2, US build)", "the same builder as 4:1 with a flag set"),
        Op(0x00, 0x08, "handle registration",
           "the client registers the handle it will play as; 1608 bytes in",
           payload=handlelists.payload_handle_ack,
           capture=lobbycapture.capture_handle_registration),
        Op(0x01, 0x0A, "KPutChrList",
           "PS2 only: the console writes its character list back",
           payload=characters.payload_chr_put),
        Op(0x02, 0x06, "KPutFriendList",
           "the friend list write: add, rename, delete, ignore",
           paylen=paylen.paylen_friend_put,
           payload=friendput.payload_friend_put,
           capture=friendput.capture_friend_put),
        Op(0x03, 0x00, "KGetDetailData",
           "a resource fetch keyed by path: the account record, saves, lists, messages",
           paylen=paylen.paylen_fetch,
           payload=resourcestore.payload_fetch,
           capture=fetchpath.capture_fetch),
        Op(0x03, 0x01, "message send",
           "creates an O/m/ object the recipient later reads",
           capture=resourcestore.capture_write),
        Op(0x03, 0x02, "object write-back",
           "updates an existing O/m/ object (mark read, delete)",
           capture=resourcestore.capture_write),
        Op(0x03, 0x04, "multi-target write",
           "one O/m/ message to up to 20 recipients; header-only reply",
           capture=resourcestore.capture_multi_write),
        Op(0x03, 0x03, "mail list",
           "the mailbox: 8 + count * 264 + 4",
           paylen=lobbymail.paylen_mailbox,
           payload=lobbymail.payload_mailbox),
        Op(0x04, 0x03, "KPutMyCommentForFriend",
           "the comment a friend sees",
           capture=profilerecord.capture_comment),
        Op(0x04, 0x05, "KChangeMyStatus",
           "online, away, invisible, and which title the member is in",
           payload=memberstatus.payload_change_status),
        Op(0x04, 0x06, "KGetMyStatus",
           "the session record: my own status and clock",
           payload=lobbysession.payload_my_status),
        Op(0x04, 0x07, "active handle",
           "the client names the handle it is logged in as; 8 bytes back",
           capture=lobbycapture.capture_active_handle),
        Op(0x05, 0x01, "profile write",
           "the member's profile fields as a TLV",
           capture=profilerecord.capture_profile_write),
        Op(0x05, 0x03, "member search",
           "status and zone search",
           payload=lobbysearch.payload_search),
        Op(0x05, 0x04, "profile read-back",
           "the handle profile (600) or a title's content profile (per title)",
           paylen=profilerecord.paylen_profile,
           payload=profilerecord.payload_profile),
        Op(0x07, 0x01, "create group",
           "writes the group row and answers with its id",
           payload=friendgroups.payload_create),
        Op(0x07, 0x02, "KDeleteGroup", "header-only reply",
           payload=friendgroups.payload_delete),
        Op(0x07, 0x03, "KChgGrpMemClass", "a member's role in a group; header-only reply",
           payload=friendgroups.payload_class_change),
        Op(0x07, 0x0B, "KChgMyGrpStatus",
           "my comment, handle slot and status in one group; header-only reply",
           payload=friendgroups.payload_my_status),
    ]
    for op in rows:
        _OPS.setdefault(op.key, op)


def lookup(op1, op2):
    """The row for an opcode, or None."""
    _build()
    return _OPS.get((op1, op2))


def rows():
    """Every row, in opcode order."""
    _build()
    return [_OPS[k] for k in sorted(_OPS)]
