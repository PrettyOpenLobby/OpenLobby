"""Prove services/gmchat.py -- the GM chat record language and its spool -- offline.

    python gmchat_test.py

No client, no containers, no network. Everything runs against a throwaway spool.

WHY THIS SUITE EXISTS. The `T` and `U` encoders are written against the client's
PARSERS, not against a capture -- no SE GM chat traffic survives -- so they are
the most guess-shaped code in the stack, and the one correction ever made to them
(`T_HEAD`, from a client's own `TI01\\x07HIIII`) was found by reading a log by
hand. Two things therefore have to keep working no matter what else changes:

  * `describe()` must NEVER raise, because the records worth looking at are
    exactly the malformed ones;
  * the transcript must keep the ORIGINAL BYTES, because it is the capture that
    the next correction will be made from.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

#: A throwaway spool. Set BEFORE the import: gmchat reads SPOOL at import time,
#: and pointing this at the real /data would mean a test run could eat a GM line
#: that was queued for a live caller.
SPOOL = tempfile.mkdtemp(prefix="gmchat-test-")
os.environ["POL_GMCHAT_SPOOL"] = SPOOL

import gmchat  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    room = b"#gmchat001"

    # --- the record language ------------------------------------------------
    t = gmchat.encode_text("Hello, how can I help?")
    check("a T record carries the client's own header",
          t == b"T" + gmchat.T_HEAD + b"\x07Hello, how can I help?", repr(t))
    check("...and reads back as just the line",
          gmchat.describe(t) == "Hello, how can I help?", gmchat.describe(t))

    # THE ONE PIECE OF REAL CAPTURE ANYWHERE IN THIS PATH: the client emitted
    # this itself when somebody typed HIIII into the GM chat window on
    # 2026-08-16, and it is why T_HEAD is 'I01' rather than empty.
    cap = b"TI01\x07HIIII"
    check("the captured client T record decodes", gmchat.describe(cap) == "HIIII",
          gmchat.describe(cap))

    u = gmchat.encode_event("S", "Fox")
    check("a U record is <class><subcode><hexlen><name>", u == b"US3Fox", repr(u))
    check("...and reads as the membership event",
          gmchat.describe(u) == "Fox started", gmchat.describe(u))
    # The length field is ONE hex digit (0x4ab2d0c sprintf's "0x%c"), so a long
    # name is not expressible and must be trimmed rather than silently overflow.
    long_u = gmchat.encode_event("G", "A" * 40)
    check("a name longer than 0xf is trimmed, not overflowed",
          long_u == b"UGf" + b"A" * 15, repr(long_u))

    # --- describe() must never raise ----------------------------------------
    # These are the shapes a wrong guess actually produces. A decoder that threw
    # on them would blank the transcript in exactly the situation it is for.
    for bad in (b"", b"T", b"Txx", b"U", b"UZz", b"Uq", b"K\x01", b"Q???",
                b"\x00\xff", b"H", b"T\x07", b"\xe3\x81\x82"):
        try:
            gmchat.describe(bad)
            ok = True
        except Exception as exc:                    # noqa: BLE001 -- the point
            ok = False
            print(f"       {bad!r} raised {exc!r}")
        if not ok:
            check(f"describe({bad!r}) does not raise", False)
    check("describe() survives every malformed record", True,
          "12 shapes, including empty, truncated and non-UTF8")

    # --- spool: delivered exactly once --------------------------------------
    gmchat.spool(room, t)
    gmchat.spool(room, u, nick=b"self")
    check("pending counts what has not gone out", gmchat.pending(room) == 2,
          str(gmchat.pending(room)))
    drained = gmchat.drain(room)
    check("drain returns (nick, record) pairs in order",
          drained == [(None, t), (b"self", u)], repr(drained))
    # DELETE-AFTER-READ IS THE CONTRACT: two sessions in one room must not both
    # deliver the same line to the same person.
    check("a drained line is gone", gmchat.drain(room) == []
          and gmchat.pending(room) == 0)

    # --- rooms(): a room nobody has been in still has to be visible ---------
    gmchat.spool(room, t)
    check("a room with only an undelivered spool is still listed",
          gmchat.rooms() == ["#gmchat001"], repr(gmchat.rooms()))
    gmchat.drain(room)

    # --- transcript ----------------------------------------------------------
    gmchat.record(room, "out", b"UMXGR8ETQ", t)
    gmchat.record(room, "in", b"UH5GRSV86", cap)
    rows = gmchat.transcript(room)
    check("both directions are recorded", len(rows) == 2, str(len(rows)))
    check("the transcript keeps the RAW BYTES, not just the text",
          bytes.fromhex(rows[1]["raw"]) == cap, rows[1]["raw"])
    check("...and the decoded reading beside them",
          rows[1]["text"] == "HIIII" and rows[0]["dir"] == "out")

    # A record we cannot decode must still be RECORDED -- that is the whole
    # value of the capture, and dropping it would lose the only evidence of
    # whatever shape the client actually speaks.
    gmchat.record(room, "in", b"UH5GRSV86", b"K\x01\x02undecoded")
    rows = gmchat.transcript(room)
    check("an undecodable record is kept anyway", len(rows) == 3
          and rows[2]["raw"] == b"K\x01\x02undecoded".hex(), rows[2]["text"][:40])

    # --- is_gm_room: group chat rides the same band and must not be touched --
    check("only the GM prefix counts as a GM room",
          gmchat.is_gm_room(b"#gmchat001") and not gmchat.is_gm_room(b"#xxl0001")
          and not gmchat.is_gm_room(b"") and not gmchat.is_gm_room(None))

    # --- trim keeps a busy room bounded --------------------------------------
    saved, gmchat.TRANSCRIPT_MAX = gmchat.TRANSCRIPT_MAX, 5
    try:
        for i in range(20):
            gmchat.record(room, "in", b"UH5GRSV86", gmchat.encode_text(f"line {i}"))
        gmchat.trim(room)
        rows = gmchat.transcript(room, 100)
        check("trim holds a room to TRANSCRIPT_MAX", len(rows) == 5, str(len(rows)))
        check("...keeping the NEWEST lines", rows[-1]["text"] == "line 19",
              rows[-1]["text"])
    finally:
        gmchat.TRANSCRIPT_MAX = saved

    # --- a missing spool is not an error -------------------------------------
    shutil.rmtree(SPOOL, ignore_errors=True)
    check("everything degrades quietly with no spool directory at all",
          gmchat.drain(room) == [] and gmchat.transcript(room) == []
          and gmchat.rooms() == [] and gmchat.pending(room) == 0)

    print()
    if fails:
        print(f"{len(fails)} check(s) FAILED: {fails}")
        return 1
    print("gmchat.py self-test OK")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(SPOOL, ignore_errors=True)
    sys.exit(rc)
