#!/usr/bin/env python3
"""POLpro -- the PLAINTEXT tag channel the PS2 games use for profile / ranking.

The game channel carries two different wire formats under one envelope, and the
class character after the 3-char service tag picks which:

    NOTICE <peer> :G <tag> <class> <payload> <4-char checksum> CRLF
                     MJS    G       'B'+43 chars   -> the 32-byte binary record
                                                      (janwire.py)
                     MJS    P       <PG>...        -> PROFILE   sqMgPfc*
                     MJS    R       <RR>...        -> RANKING   sqMgRkcp*

GRAMMAR -- measured, off four builders in JanHouRou.pex (0x002f32e4, 0x002f867c,
0x002f8a68, 0x002f8b50), all identical in shape:

    '<' CODE '>' \\x07 value [ \\x06 value ]*      ... groups concatenated

so \\x07 separates GROUPS and \\x06 separates VALUES inside a group. The helpers
are `0x002f8bc8` append-char, `0x00305a80` append-string, `0x002f89e0` format a
u64 as `0x%016X`, `0x002f8c08` append a decimal int; the 0x002f8a68 variant loops
bytes with \\x06 between them, which is how ARRAY fields are encoded.

Two real captures (2026-08-13, live PS2, from savestate RAM -- these lines exist
nowhere on disk because the tags are assembled at runtime):

    GMJSP<PG>\\x070x000000003B9ACA03\\x061000000003\\x07
    GMJSR<RR>\\x070\\x07<PI>\\x070x5B01E2F59F813AB1\\x060\\x060\\x060\\x07

The CODE vocabulary is a table in JanHouRou.pex at 0x003f146c: 103 entries of
`[u32 id][2-char code][2 NUL]`, indexed `TABLE + id*8` and pointed at the CODE
field, so the records start four bytes lower (the off-by-one both lanes hit).
TAGS below is that table, generated -- do not retype it.

THE REPLY TAG SET WAS NOT MEASURED, AND FOR `<PG>` IT NOW IS (2026-08-23). The
old note here -- "the parser lives in the Viewer core and `.pex` modules use
gp-relative loads, so address xrefs find nothing there" -- is **wrong twice**:
the parser is in the GAME module (`JanHouRou.pex 0x002f76c8..`, `TMaster.pex`)
and reachable by ordinary xrefs, and the PC build's sqmg parser decompiles to
plain C. Read there, `<PG>` (the per-Content-ID game
character request) is answered by:

    <PO>   success -- ONE group, 71 positional values -> PROFILE_PO (below)
    <PM>   error   -- value[0] is the error code the client reports verbatim

Both codes are confirmed in the two builds independently: the class-P type
tables map PO 96 -> type 0x2F and PM 97 -> type 0x30 (`TM.dll sqmg_1a68c0`;
`JanHouRou.pex 0x002f3178` + its 26-entry jump table at 0x00419780), and the
0x2F arm parses the group whose code is 96 into a 232-byte struct
(`TM.dll sqmg_1a3880`; `JanHouRou.pex 0x002f90e0`) -- SAME 71 values, SAME
order, SAME types, SAME struct offsets in both. So one reply serves jan AND
Tetra Master. `<GK>` answers the profile *SET* (`<GR>`), NOT `<PG>` -- see
PROFILE_PO's note.

Every other class/command reply is STILL a template loaded from a file,
deliberately: editing that file needs no rebuild and no restart, which is what
makes iterating on an inferred format cheap instead of reckless. See
reply_spec().

    python polpro.py --selftest
    python polpro.py --decode 'P<PG>\\x07...'
"""
import argparse
import json
import os
import re
import sys

GROUP_SEP = b"\x07"
VALUE_SEP = b"\x06"

#: JanHouRou.pex 0x003f146c, in table order (index -> code). 103 entries, NOT
#: 102: `PO` appears TWICE (96 and 101) and this table used to carry only the
#: first, which left `FO` at 101 when the module says 102. Regenerated from
#: `work/ps2/out/polpro-tags.py`; byte-identical in `TMaster.pex`.
TAGS = (
    "RI", "MR", "MC", "RE", "CF", "WH", "ZI", "NN", "US", "DP", "MN", "MA",
    "MS", "CN", "GN", "TN", "RS", "MF", "RO", "PD", "PC", "TD", "ST", "TS",
    "TP", "DR", "DD", "DO", "DN", "DC", "RD", "RN", "DE", "DS", "DF", "EP",
    "ED", "NC", "ET", "AM", "SP", "BI", "AC", "LC", "CM", "CB", "CD", "BC",
    "II", "AI", "BM", "EI", "ER", "ES", "EF", "BR", "BS", "BF", "SI", "IO",
    "MP", "LT", "IN", "IC", "IB", "BH", "BD", "SS", "SF", "HS", "HF", "SN",
    "CR", "AN", "CI", "CS", "NS", "CP", "GR", "PN", "PV", "PR", "GK", "RK",
    "PF", "GG", "PE", "GO", "OK", "OG", "RR", "RF", "RG", "PI", "LN", "PG",
    "PO", "PM", "NO", "VO", "PP", "PO", "FO",
)
#: FIRST WINS, because the client's lookup is a LINEAR SCAN of the same table --
#: so the duplicate `PO` resolves to 96, not 101. A plain dict comprehension
#: would take the last and quietly hand every `<PO>` builder the wrong id.
TAG_ID = {}
for _i, _t in enumerate(TAGS):
    TAG_ID.setdefault(_t, _i)
del _i, _t

#: Class characters seen on the wire, and what they mean.
CLASS_BINARY, CLASS_PROFILE, CLASS_RANKING = b"G", b"P", b"R"
CLASS_AUCTION = b"A"
TEXT_CLASSES = (CLASS_PROFILE, CLASS_RANKING, CLASS_AUCTION)

_GROUP_RE = re.compile(rb"<([A-Z0-9]{2})>")


def parse(payload):
    """`payload` (after the class char) -> [(tag, [value, ...]), ...].

    Tolerant by design: this runs on live client bytes, and a group we cannot
    split is still worth reporting as a lead rather than raising.
    """
    if isinstance(payload, str):
        payload = payload.encode("latin1")
    if payload[:1] in (CLASS_PROFILE, CLASS_RANKING, CLASS_BINARY, CLASS_AUCTION):
        payload = payload[1:]
    out = []
    marks = list(_GROUP_RE.finditer(payload))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(payload)
        body = payload[m.end():end]
        body = body.lstrip(GROUP_SEP).rstrip(GROUP_SEP)
        vals = body.split(VALUE_SEP) if body else []
        out.append((m.group(1).decode("latin1"), [v.decode("latin1") for v in vals]))
    return out


def build(groups):
    """[(tag, [value, ...]), ...] -> payload bytes, exactly as the client builds it."""
    out = bytearray()
    for tag, vals in groups:
        out += b"<" + str(tag).encode("latin1") + b">" + GROUP_SEP
        out += VALUE_SEP.join(str(v).encode("latin1") for v in vals)
        out += GROUP_SEP
    return bytes(out)


def describe(payload):
    groups = parse(payload)
    parts = []
    for tag, vals in groups:
        known = "" if tag in TAG_ID else "?"
        parts.append("<%s%s>(%s)" % (tag, known, ",".join(vals)))
    return " ".join(parts) or "(no groups)"


# --- <PG> -> <PO> : the per-Content-ID game character reply -------------------
#
# MEASURED 2026-08-23, statically, in BOTH builds -- this is the one reply on
# this channel that is not a template.
#
# The request (client -> us), captured live off the PS2 and re-derived from the
# builder `sqmg_1a3b70(buf, id_lo, id_hi, id_dec, code=0x5F)`:
#
#     P<PG>\x07 0x%016X \x06 %d \x07          hex Content ID, then decimal
#
# The reply (us -> client) is ONE group and the client addresses its values BY
# POSITION -- there are no sub-tags inside it:
#
#     P<PO>\x07 v0 \x06 v1 \x06 ... \x06 v70 \x07
#
# WHERE THAT COMES FROM, and why it is not an inference:
#   * class-P type table -- `TM.dll sqmg_1a68c0` (a switch) and
#     `JanHouRou.pex 0x002f3178` (code-72 indexed into the 26-entry jump table
#     at 0x00419780). Both give PG 95 -> 0x2E, **PO 96 -> 0x2F**, **PM 97 ->
#     0x30**, and both leave `PR`, `PF`, `CI`, `PN`, `PV` with no arm at all.
#   * the 0x2F arm -- `TM.dll sqmg_1a66d0` / `JanHouRou.pex 0x002f2e38`: set the
#     pfc GET state to 2, then parse the group whose code is **96** into the
#     profile struct.
#   * the parser -- `TM.dll sqmg_1a3880` / `JanHouRou.pex 0x002f90e0`: 71 reads
#     off ONE group, by position, into a 232-byte struct. The two builds agree
#     value for value, type for type, offset for offset.
#   * the struct is 232 bytes in BOTH. `sqMgPfcGetCharacterProfileCheck` copies
#     0x3A dwords on the PC; the PS2 copies ctx+456..+680 in 32-byte strides AND
#     one trailing 8-byte `ld/sd` -- 224 + 8. (A note recording "224 bytes"
#     missed the tail; the builds do not actually differ here.)
#
# WARNING: `<GK>` IS NOT THE ANSWER TO `<PG>` -- and it was deployed as one. `<GK>`
# (82) / `<GG>` (85) are what `pfcCharaProfileResult` takes, and that handler
# serves the profile **SET** (`sqMgPfcSetCharacterProfile`, whose request leads
# with `<GR>` 78), gating on the SET's own state field. A `<GK>` sent at a
# pending `<PG>` finds that state 0, logs `SQMGPF_ERROR_CHARAPROFILE_REQUEST_NONE`
# (-8704) inside the client, and leaves the GET poll waiting forever. The two
# handlers have near-identical names and adjacent state words; that is the whole
# trap. See config/polpro.json.
#
# The client never raises on a short or malformed reply -- a missing value reads
# as 0 / "" -- so a WRONG value list presents as a blank popup, never an error.

#: (first value index, count, kind, struct offset) -- the parser, in order.
#: kinds: "u64" = the `0x%016X` hex form the client's own hex reader wants,
#: "int" = decimal, "float" = decimal float, "str" = raw text (see _PO_STR_LIM).
PROFILE_PO = (
    (0,  1, "u64",   0x10),   # the Content ID this profile IS (hex form)
    (1,  1, "int",   0x18),   # the Content ID again, decimal -- request echoes it
    (2,  1, "str",   0x00),   # character NAME, copied with a 16-byte limit
    (3,  4, "int",   0x1C),   # 4 bytes; +0x1C is what the SET sends as <PP>
    (7,  2, "int",   0x29),   # 2 bytes; +0x2A is what the SET sends as <FO>
    (9,  1, "str",   0x30),   # a 64-byte string (comment / status line)
    (10, 2, "u64",   0x70),   # 2 x 64-bit
    (12, 4, "float", 0x80),   # 4 x float
    (16, 8, "int",   0x90),   # 8 x u32
    (24, 8, "int",   0xB0),   # 8 x u16
    (32, 8, "int",   0xC0),   # 8 x u8
    (40, 1, "int",   0xC8),   # 1 x u8 -- the SET sends this as <VO>
    (41, 2, "int",   0xC9),   # the byte array the SET addresses as <NO>(i,v):
    (43, 4, "int",   0xCB),   #   index i is value[41 + i], i.e. base +0xC9,
    (47, 8, "int",   0xCF),   #   which is why <NO>(6,..) is +0xCF and
    (55, 8, "int",   0xD7),   #   <NO>(14,..) is +0xD7 -- read off the SET's own
    (63, 8, "int",   0xDF),   #   format string, TM.dll 0x51C62CC.
)
#: 71 -- the last value the parser reads. Sending fewer is legal (the missing
#: ones read as 0) and sending more is silently ignored, but the client's own
#: writer would send exactly this many.
PROFILE_PO_VALUES = 71
#: mgStrCopyLim limits: value[2] -> 16 bytes, value[9] -> 64. Longer is TRUNCATED
#: by the client, so truncate here instead and keep the wire honest.
_PO_STR_LIM = {2: 16, 9: 64}
#: The message slot is 0x220 with the payload at +0x22, and sqMg's own writer
#: refuses a payload of 470 bytes or more (-8301). Stay well under it.
PROFILE_PO_MAX = 460


def _po_kind(index):
    for first, count, kind, _off in PROFILE_PO:
        if first <= index < first + count:
            return kind
    return "int"


def po_values(fields=None):
    """The 71 positional values of a `<PO>` reply, as strings.

    `fields` is {value index: python value}; everything unset goes out as the
    type's zero, which is what the client would read anyway. Types are forced
    from PROFILE_PO, so a caller cannot accidentally put a decimal where the
    client's hex reader is waiting -- the failure mode that costs a live run.
    """
    fields = dict(fields or {})
    out = []
    for i in range(PROFILE_PO_VALUES):
        kind = _po_kind(i)
        v = fields.get(i)
        if kind == "str":
            s = "" if v is None else str(v)
            # The separators cannot appear inside a value: the parser splits on
            # them before any unescaping, and `getCsvStr` does not unescape at
            # all. Drop them rather than emit a line that reparses differently.
            s = s.replace("\x06", " ").replace("\x07", " ").replace("\\", " ")
            lim = _PO_STR_LIM.get(i)
            out.append(s[:lim] if lim else s)
        elif kind == "u64":
            out.append("0x%016X" % (int(v or 0) & 0xFFFFFFFFFFFFFFFF))
        elif kind == "float":
            out.append("%g" % float(v or 0))
        else:
            out.append(str(int(v or 0)))
    return out


def build_po(fields=None):
    """A complete `<PO>` payload. Raises if it would exceed the client's slot."""
    payload = build([("PO", po_values(fields))])
    if len(payload) >= PROFILE_PO_MAX:
        raise ValueError("<PO> payload is %d bytes, ceiling is %d"
                         % (len(payload), PROFILE_PO_MAX))
    return payload


def build_pm(code=-721):
    """The `<PG>` ERROR reply. value[0] is reported by the client VERBATIM.

    The default is `SQMGPF_ERROR_GAME_PROFILE_NONE` (-721, SE's own name for it
    in `sqmg_errors.h`) -- the honest answer for a Content ID we hold nothing
    for. WARNING: That this family, rather than the -87xx one, is what a SERVER sends
    is the one thing here that is NOT measured: the client only reads the value
    and reports it, so no code path can tell us which family SE used.
    """
    return build([("PM", [str(int(code))])])


# --- reply templates ---------------------------------------------------------
#
# A spec is {command_tag: [entry, ...]} where each entry is either the string
# "echo" (splice the request's own groups back in) or [tag, [values...]].
# Values may use placeholders:
#     $TAG.N   the Nth value of the request's <TAG> group
#     $0..$9   the Nth value of the request's FIRST group
# An unknown placeholder resolves to "0" rather than blowing up mid-request.
SPEC_FILE = os.environ.get("POL_POLPRO_SPEC", "/config/polpro.json")
_SPEC_CACHE = {"mtime": 0.0, "spec": None}

#: Used when no spec file exists. Echo the request and add a status group -- the
#: least-invented thing we can say. `OK` is the obvious status tag (id 88 in the
#: table) but that is INFERENCE, which is exactly why this is a default and not
#: hardcoded behaviour.
DEFAULT_SPEC = {
    "*": ["echo", ["OK", ["0"]]],
}


def reply_spec():
    """The template table, re-read whenever the file changes."""
    try:
        st = os.stat(SPEC_FILE)
    except OSError:
        return _SPEC_CACHE["spec"] or DEFAULT_SPEC
    if st.st_mtime != _SPEC_CACHE["mtime"] or _SPEC_CACHE["spec"] is None:
        try:
            with open(SPEC_FILE, "r", encoding="utf-8") as f:
                _SPEC_CACHE["spec"] = json.load(f)
            _SPEC_CACHE["mtime"] = st.st_mtime
        except (OSError, ValueError):
            return _SPEC_CACHE["spec"] or DEFAULT_SPEC
    return _SPEC_CACHE["spec"]


#: Where the served blobs live -- the same directory `responders._resource_file`
#: writes, so `$SERIAL` reads exactly the bytes the client will be handed.
RESOURCE_DIR = os.environ.get("POL_RESOURCE_DIR", "/data/resources")

#: `b/g/PTL` +0x40 -- see tools/tmptl.py. The client echoes this back on every
#: class-L <DR>, so it is both what we must answer with and how we know it read.
_SERIAL_OFF = 0x40


def _file_serial(path):
    """The serial at +0x40 of the STORED blob for `path`. THE FALLBACK ONLY.

    WARNING: **PREFER THE `serial=` ARGUMENT TO `reply_for`.**
    `+0x40` is known to be the room's UPDATE SEQUENCE NUMBER
    (`cp__002fb560` opens `ctx+0xa4 := ptl+0x40`) and `services/tmroom.py`
    GENERATES the served blob, stamping that field with the live room sequence.
    A stored file's `+0x40` is then no longer what the client was handed, and
    reading it here is not merely imprecise, it is a different number:

        measured 2026-08-18, one live room --
            tm-roster.json    #TM0R001 seq 2      <- what build_ptl stamps
            newest stored     .b_g_PTL.bin  23    <- what this function returned

        so the client read sequence 2 and was told 23 on every `<DR>`, i.e. it
        sat waiting for deltas 3..23 that do not exist. `cp__002fae98` re-requests
        when the number is out of range, and that is the b/g/PTL re-fetch storm
        in `lobby.log` -- 623 fetches at a flat 3 s cadence, which is the client
        correctly chasing a sequence nothing would ever deliver.

    WARNING: AND NEWEST-MATCH-WINS WAS ALWAYS A HEURISTIC. The store is keyed by the
    client's fetch SUBJECT (`responders._SUBJECT_KEYED_PATHS`) and this channel
    never sees it -- POLpro rides the auth band, the fetch rides lobby 3:0. With
    one room in play the glob is exact; with two it answers whichever file was
    touched last, to both players. Passing the sequence in fixes both faults at
    once, because the caller knows WHO is asking and therefore which room.

    Kept because it is still the right answer for a path `tmroom` knows nothing
    about, and because a room we hold no roster for must degrade to the old
    behaviour rather than to zero.

    Returns None if nothing can be read -- the caller then tells the client it is
    CURRENT, which is the safe direction: a wrong LOW serial is a no-op, while a
    wrong high one starts exactly the re-fetch loop described above.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", path)
    tail = ".%s.bin" % safe
    try:
        names = [n for n in os.listdir(RESOURCE_DIR) if n.endswith(tail)]
    except OSError:
        return None
    names.sort(key=lambda n: (n.startswith("s"),
                              os.path.getmtime(os.path.join(RESOURCE_DIR, n))))
    for name in reversed(names):
        try:
            with open(os.path.join(RESOURCE_DIR, name), "rb") as f:
                f.seek(_SERIAL_OFF)
                raw = f.read(4)
            if len(raw) == 4:
                return int.from_bytes(raw, "little")
        except OSError:
            continue
    return None


def _subst(val, groups, serial=None):
    if not isinstance(val, str) or not val.startswith("$"):
        return val
    ref = val[1:]
    first = groups[0][1] if groups else []
    if ref.isdigit():
        i = int(ref)
        return first[i] if i < len(first) else "0"
    # `$SERIAL` / `$SERIAL:<path>` -- the sequence the blob this client will be
    # handed is stamped with. The CALLER's value wins: it is the only one that
    # knows which room the requester is in, and since §11p it is the only one
    # that agrees with what `tmroom.build_ptl` actually writes at +0x40. The
    # stored-file glob below is the fallback for a room we hold no roster for.
    if ref == "SERIAL" or ref.startswith("SERIAL:"):
        if serial is not None:
            return str(int(serial))
        _, _, p = ref.partition(":")
        n = _file_serial(p or "b/g/PTL")
        if n is not None:
            return str(n)
        return first[0] if first else "0"
    tag, _, idx = ref.partition(".")
    for t, vals in groups:
        if t == tag:
            i = int(idx) if idx.isdigit() else 0
            return vals[i] if i < len(vals) else "0"
    return "0"


def reply_for(payload, spec=None, tag=None, allow_default=True, serial=None):
    """Reply payload bytes for a request, or None to stay silent.

    `tag` is the 3-char SERVICE tag off the envelope (`TM0`, `MJS`, ...). It is
    optional so every existing caller and the selftest keep working, but pass it
    where you have it -- see `_spec_keys` for why one channel is not one game.

    `serial` is what `$SERIAL` resolves to: the UPDATE SEQUENCE of the room this
    requester is standing in. Pass it wherever the caller can resolve the room --
    it is the only value that agrees with the `+0x40` `tmroom.build_ptl` stamps
    into the blob the same client fetches. Omitted, `$SERIAL` falls back to
    `_file_serial`'s glob, which is right only while one room is in play.
    """
    groups = parse(payload)
    if not groups:
        return None
    spec = spec if spec is not None else reply_spec()
    entries = None
    for key in _spec_keys(groups, tag):
        if key in spec:
            entries = spec[key]
            break
    if entries is None and allow_default:
        # WARNING: `allow_default=False` EXISTS FOR CLASS `L`. The `*` wildcard echoes
        # the request and appends `<OK>`, which is a fine default only where we
        # know the class's reply vocabulary. On class L we do not: its type table
        # (TM.dll rva 0x1A27A0) runs over tag ids 0..0x22 and `OK` (88) is not
        # in it at all, so the wildcard could only ever produce a message the
        # client resolves to no type and drops -- noise on a channel we are still
        # reading. Route the class so it is DECODED in the log, and answer only
        # what a measured spec key says to answer.
        entries = spec.get("*")
    if not entries:
        return None
    return _render(entries, groups, serial)


def _spec_keys(groups, tag=None):
    """Spec lookup keys for a request, MOST SPECIFIC FIRST.

    `TAG1+TAG2` before `TAG1`, because the lead group is not always enough to
    identify the request. Measured on Tetra Master's auction 2026-08-16: BOTH
    list screens send `<SI>` with identical criteria and are told apart ONLY by
    the second group -- `<IB>` (id 64) for "Cards Bid On", `<IO>` (id 59) for
    "Cards for Sale". Keying on `SI` alone answers both with one list, which
    renders fine and is silently wrong.

    ...AND THE SERVICE TAG BEFORE EITHER, because this channel is SHARED between
    the games and a request shape is not. Rankings is the case that forced it
    (2026-08-16): Tetra Master and Janhourou both send `<RR>` + `<PI>` on class
    `R`, byte-identical in shape, and they want DIFFERENT replies -- TM's class-R
    type table (TM.dll rva 0x1A7198) gives `<RF>` (id 91) the success arm and
    routes `<OK>` (88) to type 0x2D, which has no arm at all, while Jan's rank
    handler was read as taking `<OK>`. One key cannot be right for both, and the
    failure mode is silent on whichever game loses.

    So a spec may key `TM0:RR+PI`, `TM0:RR`, `RR+PI` or `RR`, in that order --
    an untagged key still matches every game, which is what keeps the existing
    entries working.
    """
    keys = []
    pairs = []
    if len(groups) >= 2:
        pairs.append("%s+%s" % (groups[0][0], groups[1][0]))
    pairs.append(groups[0][0])
    if tag:
        tag = tag.decode("latin1") if isinstance(tag, bytes) else str(tag)
        keys.extend("%s:%s" % (tag, k) for k in pairs)
    keys.extend(pairs)
    return keys


def _render(entries, groups, serial=None):
    out = []
    for e in entries:
        if e == "echo":
            out.extend(groups)
        elif isinstance(e, (list, tuple)) and len(e) == 2:
            out.append((e[0], [_subst(v, groups, serial) for v in e[1]]))
    return build(out) if out else None


def chaser_for(payload, spec=None, tag=None):
    """A SECOND payload to send right behind the first, or None.

    Spec key is the command tag plus `!chase`. Two messages in one turn is a
    real shape in this protocol -- a result followed by a push -- but the
    immediate reason it exists is as a BISECT: a handler that guards on
    `state != 1` will silently drop the chaser if the first reply already moved
    the state, so "chaser has no visible effect" and "chaser fires" distinguish
    a first reply that was acted on from one that was not.
    """
    groups = parse(payload)
    if not groups:
        return None
    spec = spec if spec is not None else reply_spec()
    entries = None
    for key in _spec_keys(groups, tag):
        if key + "!chase" in spec:
            entries = spec[key + "!chase"]
            break
    if not entries:
        return None
    return _render(entries, groups)


def selftest():
    ok = True
    # THE GROUND TRUTH: both lines captured off the live PS2. Assert against
    # these rather than against our own builder -- that is the mistake that let
    # the janwire endianness bug survive two days.
    LIVE_PG = b"P<PG>\x070x000000003B9ACA03\x061000000003\x07"
    LIVE_RR = (b"R<RR>\x070\x07<PI>\x070x5B01E2F59F813AB1"
               b"\x060\x060\x060\x07")

    g = parse(LIVE_PG)
    print("PG parse:", g)
    if g != [("PG", ["0x000000003B9ACA03", "1000000003"])]:
        print("FAIL: PG groups"); ok = False

    g = parse(LIVE_RR)
    print("RR parse:", g)
    if g != [("RR", ["0"]), ("PI", ["0x5B01E2F59F813AB1", "0", "0", "0"])]:
        print("FAIL: RR groups"); ok = False

    # build() must reproduce the client's own bytes exactly
    if build(parse(LIVE_PG)) != LIVE_PG[1:]:
        print("FAIL: PG does not round-trip\n  %r\n  %r"
              % (build(parse(LIVE_PG)), LIVE_PG[1:])); ok = False
    if build(parse(LIVE_RR)) != LIVE_RR[1:]:
        print("FAIL: RR does not round-trip\n  %r\n  %r"
              % (build(parse(LIVE_RR)), LIVE_RR[1:])); ok = False

    # the tag table -- 103 entries, `PO` twice, and the FIRST one wins
    if len(TAGS) != 103:
        print("FAIL: tag table is %d entries, expected 103" % len(TAGS)); ok = False
    for t in ("PG", "RR", "PI", "LN", "PP", "PO", "NO", "OK"):
        if t not in TAG_ID:
            print("FAIL: %s missing from the tag table" % t); ok = False
    for code, want in (("CR", 72), ("AN", 73), ("CI", 74), ("CS", 75),
                       ("GR", 78), ("GK", 82), ("GG", 85), ("PE", 86),
                       ("OK", 88), ("PG", 95), ("PO", 96), ("PM", 97),
                       ("NO", 98), ("VO", 99), ("PP", 100), ("FO", 102)):
        if TAG_ID.get(code) != want:
            print("FAIL: %s is id %s, the module says %d"
                  % (code, TAG_ID.get(code), want)); ok = False
    if TAGS[101] != "PO":
        print("FAIL: the duplicate PO at 101 is missing"); ok = False

    # <PO> -- the measured <PG> answer. Shape first, then the types.
    po = po_values({0: 0x3B9ACA03, 1: 1000000003, 2: "Fox", 9: "hello"})
    if len(po) != 71:
        print("FAIL: <PO> built %d values, the parser reads 71" % len(po)); ok = False
    if po[0] != "0x000000003B9ACA03":
        print("FAIL: value[0] is not the client's hex form: %r" % po[0]); ok = False
    if po[1] != "1000000003" or po[2] != "Fox" or po[9] != "hello":
        print("FAIL: <PO> did not carry its fields: %r" % po[:10]); ok = False
    if po[12] != "0" or po[70] != "0":
        print("FAIL: unset <PO> values must be zeros"); ok = False
    # the two string limits the client applies with mgStrCopyLim
    lim = po_values({2: "X" * 40, 9: "Y" * 200})
    if len(lim[2]) != 16 or len(lim[9]) != 64:
        print("FAIL: <PO> strings not truncated to 16/64: %d/%d"
              % (len(lim[2]), len(lim[9]))); ok = False
    # a separator inside a value would re-split the message on arrival
    if "\x06" in po_values({2: "a\x06b"})[2]:
        print("FAIL: a value separator survived into a <PO> string"); ok = False
    blob = build_po({0: 0x3B9ACA03, 1: 1000000003, 2: "Fox"})
    if not blob.startswith(b"<PO>\x07") or len(blob) >= PROFILE_PO_MAX:
        print("FAIL: <PO> payload shape/size: %d bytes" % len(blob)); ok = False
    if parse(blob) != [("PO", po_values({0: 0x3B9ACA03, 1: 1000000003,
                                         2: "Fox"}))]:
        print("FAIL: <PO> does not round-trip through parse()"); ok = False
    # A REALISTIC profile fits; a saturated one does NOT, and the guard is what
    # says so instead of the client silently dropping the line. 39 of the 71
    # values land in single BYTES and 8 more in u16s, so real data is narrow --
    # but nine-digit numbers in all 55 integer slots blow past the slot, which
    # is worth knowing before somebody serves a counter that big.
    real = {0: 0x3B9ACA03, 1: 1000000003, 2: "Cassandra Blue", 9: "Y" * 64}
    real.update({i: 99999 for i in range(16, 32)})     # the u32s and u16s
    real.update({i: 255 for i in range(32, 71)})       # the byte arrays
    try:
        n = len(build_po(real))
        print("PO realistic payload: %d bytes (ceiling %d)" % (n, PROFILE_PO_MAX))
    except ValueError as exc:
        print("FAIL: a realistic profile must fit -- %s" % exc); ok = False
    try:
        build_po({i: 999999999 for i in range(16, 71)})
        print("FAIL: the payload ceiling did not bite on a saturated <PO>")
        ok = False
    except ValueError:
        pass
    if b"<PM>\x07-721\x07" != build_pm():
        print("FAIL: <PM> shape: %r" % build_pm()); ok = False

    # templates
    r = reply_for(LIVE_PG, DEFAULT_SPEC)
    print("PG reply (default spec):", r)
    if b"<PG>" not in r or b"<OK>" not in r:
        print("FAIL: default reply shape"); ok = False
    spec = {"RR": [["RR", ["$0"]], ["PI", ["$PI.0", "0", "0", "0"]],
                   ["OK", ["0"]]]}
    r = reply_for(LIVE_RR, spec)
    print("RR reply (placeholders):", r)
    if b"0x5B01E2F59F813AB1" not in r:
        print("FAIL: $PI.0 did not substitute"); ok = False
    if reply_for(b"P", DEFAULT_SPEC) is not None:
        print("FAIL: an empty payload must stay silent"); ok = False

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--decode", metavar="PAYLOAD")
    ap.add_argument("--reply", metavar="PAYLOAD")
    a = ap.parse_args()
    if a.decode:
        raw = a.decode.encode("latin1").decode("unicode_escape").encode("latin1")
        print(describe(raw))
        for tag, vals in parse(raw):
            print("  <%s>  %s" % (tag, vals))
        return 0
    if a.reply:
        raw = a.reply.encode("latin1").decode("unicode_escape").encode("latin1")
        print(repr(reply_for(raw)))
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
