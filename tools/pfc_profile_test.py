"""`<PG>` must be answered with `<PO>`, in the arity the client's parser reads.

WHAT THIS PINS, and why it is a suite rather than a comment.

The per-Content-ID game character (the jan / Tetra Master profile popup) was
answered with `<GK>` for months. `<GK>` is a real code with a real handler --
`pfcCharaProfileResult` -- which is exactly why it survived: the handler was
found, its constants were read correctly, and it belongs to the WRONG REQUEST.
It serves the profile SET (`<GR>`); the GET (`<PG>`) has its own arm, its own
state word and its own parser, and answers to `<PO>` / `<PM>`:

    type table  PG 95 -> 0x2E   PO 96 -> 0x2F   PM 97 -> 0x30
                TM.dll sqmg_1a68c0 / JanHouRou.pex 0x002f3178 (+ the 26-entry
                jump table at 0x00419780). The two builds agree.
    parser      TM.dll sqmg_1a3880 / JanHouRou.pex 0x002f90e0 -- 71 values read
                BY POSITION out of ONE `<PO>` group into a 232-byte struct.
                Same order, same types, same offsets in both, so ONE reply
                serves both games.

WARNING: NONE OF THAT IS VISIBLE AT RUNTIME. The client's parser raises on nothing: a
missing group falls back to group 0, a missing value reads as 0, and the only
symptom of a wrong reply is a popup full of blanks or a scene that waits for
ever. So the wire shape has to be asserted here, where it can fail loudly.

Everything below runs `responders._pfc_profile_reply` -- the real function --
against a seeded accounts DB and a seeded `tmrank` pool, and reads the answer
back through `polpro.parse`, i.e. out of the bytes we would have put on the wire.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

TMP = tempfile.mkdtemp(prefix="pfc-profile-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_POLPRO_PG"] = "1"
os.environ.pop("POL_POLPRO_PG_MISS", None)

import accounts                                                    # noqa: E402
import polpro                                                      # noqa: E402
import responders as R                                             # noqa: E402
try:
    import tmrank                                                  # noqa: E402
except ImportError:
    print("[SKIP] pfc_profile: the tmrank title module is not present in this "
          "tree; the <PG>/<PO> reply arm is exercised with TM rank seed data")
    raise SystemExit(0)

FAILS = []

#: The live capture this whole channel was decoded from (2026-08-13, PS2).
LIVE_PG = b"P<PG>\x070x000000003B9ACA03\x061000000003\x07"
JAN_CID = 1000000003
#: A second character on the same server, reached only through the accounts DB.
TM_CID = 1000000102


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def request(cid):
    return (b"P<PG>\x07" + ("0x%016X" % cid).encode()
            + b"\x06" + str(cid).encode() + b"\x07")


def seed():
    """One handle with a jan Content ID, and one TM pool the client sent us."""
    db = accounts.connect(os.environ["POL_ACCOUNTS_DB"])
    member = accounts.ensure_member(db, "PFCTEST")
    handle = db.execute("SELECT * FROM handle WHERE member_id = ?",
                        (member["id"],)).fetchone()
    db.execute("UPDATE handle SET handle_name = ? WHERE id = ?",
               ("Cassandra", int(handle["id"])))
    accounts.link_content_to_handle(db, int(handle["id"]), 3, str(JAN_CID))
    db.commit()
    db.close()
    # The Tetra Master side: what a `<CR>` told us about ITS character.
    os.makedirs(tmrank.store_dir(), exist_ok=True)
    with open(tmrank.pool_file(), "w", encoding="utf-8") as f:
        json.dump({"1": {"cid": TM_CID, "cname": "Kuja",
                         "cinfo": "Card Level 116"}}, f)


def seed_career(money=0, games=0, place_total=0, clear=False):
    """Write (or remove) the TM member's career block.

    The pool above is keyed by member `"1"`, and that key IS the member id --
    which is how the profile path finds the stats for a Content ID nothing has
    linked to a handle yet. The collection file this writes is the same one
    `tetramaster._collection_store` writes and `tools/tmrank.py` reads.
    """
    path = os.path.join(os.path.dirname(tmrank.store_dir()),
                        "1" + tmrank.COLLECTION_SUFFIX)
    if clear:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # `place_total` is already x100 (see tetramaster._placements: one win is
    # 100, a two-player draw 150), so the average is a plain division.
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cards": [], "money": money,
                   "rank": {"games": games, "place_total": place_total,
                            "avg_rank": (place_total // games) if games
                            else 0}}, f)


def groups_of(reply):
    g = polpro.parse(reply)
    return g[0] if g else (None, [])


def main():
    seed()

    # 1. THE CODE. Not <GK>, not an echo -- the dispatcher routes on the FIRST
    #    group's code, so a reply that leads with anything else takes the wrong
    #    arm or none at all.
    reply, handled = R._pfc_profile_reply(request(TM_CID), b"TM0")
    tag, vals = groups_of(reply)
    check(handled and tag == "PO", "the <PG> answer leads with <PO>",
          "got %r" % (tag,))
    check(len(vals) == polpro.PROFILE_PO_VALUES,
          "71 values -- the arity the parser reads", "got %d" % len(vals))
    check(len(reply) < polpro.PROFILE_PO_MAX,
          "the payload fits the client's message slot",
          "%d bytes, ceiling %d" % (len(reply), polpro.PROFILE_PO_MAX))

    # 2. THE IDENTITY. value[0] is read by the client's own HEX reader (16 hex
    #    digits after `0x`); a decimal there parses as garbage and lands in the
    #    struct's +0x10 silently.
    check(vals[0] == "0x%016X" % TM_CID, "value[0] is the Content ID, hex form",
          repr(vals[0]))
    check(vals[1] == str(TM_CID), "value[1] is the same id, decimal",
          repr(vals[1]))

    # 3. THE DATA -- the point of the exercise. A name the server looked up,
    #    not the request played back.
    check(vals[2] == "Kuja", "value[2] is the TM character's own name",
          repr(vals[2]))
    # value[9] is "AVERAGE RANK", not a status line -- decoded from
    # prof_002.pfb's own record table (label -> schema field, with FFXI as the
    # positive control) and CONFIRMED ON SCREEN 2026-08-24: serving the cinfo
    # there rendered `Average Rank: Card Level 209` in Tetra Master's own
    # profile view. The earlier expectation pinned what we happened to serve,
    # which is not evidence of what the slot means.
    check(vals[9] == "", "value[9] (Average Rank) is UNSET for a player who has "
                         "never finished a match", repr(vals[9]))
    check(vals[24] in ("", "0"),
          "value[24] (Title) is UNSET too -- the title is a FUNCTION of the "
          "average rank, so no average means no title", repr(vals[24]))
    check(vals[16] == "116", "value[16] is the CARD LEVEL, where its label points",
          repr(vals[16]))

    # 3b. ...AND ONCE THEY HAVE PLAYED, BOTH ARRIVE. This is the other half of
    #     the same rule: "only what we hold" is a promise to serve what we DO
    #     hold, and until 2026-08-24 the career stats the match path computes
    #     reached neither this reply nor the save.
    #
    #     KEY: THE TITLE IS 1-BASED AND `prof_002.pfb` PROVES IT: its Title enum
    #     carries values 1..126 (`Pauper` .. `Almighty`) with NO entry for 0,
    #     which is why an unset slot renders as `Unknown` rather than blank.
    #     A 0-based port would put every player one rung UP the ladder and
    #     nothing on screen would look wrong.
    seed_career(money=51000, games=8, place_total=1200)  # avg rank 1.50
    reply, _ = R._pfc_profile_reply(request(TM_CID), b"TM0")
    _tag, vals = groups_of(reply)
    check(vals[9] == "1.50",
          "value[9] is the average rank, x100 rendered to two decimals -- the "
          "game's own format for this fixed point", repr(vals[9]))
    import tm_title                                              # noqa: E402
    want = tm_title.title_index(51000, 116, 150)
    check(vals[24] == str(want) and want > 0,
          "value[24] is the title the CLIENT's own picker would compute from "
          "the same money / card level / average rank (%s)"
          % tm_title.title_name(want), repr(vals[24]))
    check(1 <= int(vals[24] or 0) <= tm_title.TITLE_MAX,
          "and it lands inside prof_002.pfb's enum range, 1..126 -- 0 is not a "
          "title, it is what draws as Unknown", repr(vals[24]))
    # A WORSE average must never buy a better title: this is the one assertion
    # that fails if the ported picker keeps the FIRST match instead of the last,
    # or if the average-rank comparison is flipped.
    seed_career(money=51000, games=8, place_total=2400)  # avg rank 3.00
    reply, _ = R._pfc_profile_reply(request(TM_CID), b"TM0")
    _tag, worse = groups_of(reply)
    check(worse[9] == "3.00" and int(worse[24] or 0) <= int(vals[24] or 0),
          "a worse average rank cannot earn a better title",
          "1.50 -> %r, 3.00 -> %r" % (vals[24], worse[24]))
    seed_career(clear=True)

    # 4. THE OTHER GAME, THROUGH THE OTHER STORE. jan has no pool file; the name
    #    comes from the handle the Content ID is linked to. Same reply SHAPE --
    #    the two builds run the same parser -- but since the 09-04 jan layer
    #    (61012bab) the TAG selects the game's STATS store: an MJS-tagged <PG>
    #    is filled from janstats (member resolved through the jan roster), a
    #    TM0-tagged one from tmrank. So the two answers agree on the identity
    #    slots and differ only in what the game-specific store adds. This block
    #    used to demand byte-identical replies and went red the day that
    #    changed -- fixed 2026-09-05 (run_all's rule: fix the suite the day you
    #    change what it pins).
    reply, handled = R._pfc_profile_reply(LIVE_PG, b"MJS")
    tag, vals = groups_of(reply)
    check(handled and tag == "PO", "jan's live-captured <PG> is answered too")
    check(vals[2] == "Cassandra", "value[2] resolves through handle_content",
          repr(vals[2]))
    check(vals[0] == "0x%016X" % JAN_CID, "and names the id jan asked for",
          repr(vals[0]))
    r2, _ = R._pfc_profile_reply(LIVE_PG, b"TM0")
    tag2, vals2 = groups_of(r2)
    check(tag2 == "PO" and len(vals2) == len(vals),
          "the same request on the other tag is the same <PO> shape",
          "%s/%d vs %s/%d" % (tag, len(vals), tag2, len(vals2)))
    check(vals2[0] == vals[0] and vals2[1] == vals[1] and vals2[2] == vals[2],
          "and names the same Content ID and character on either tag",
          "%r vs %r" % (vals[:3], vals2[:3]))

    # 5. UNKNOWN SUBJECT. The default is a well-formed EMPTY profile, because
    #    `<PM>` drives the client into its error arm and SE's choice here has
    #    never been observed. The `<PM>` arm still has to work when asked for.
    miss = request(1000009999)
    reply, handled = R._pfc_profile_reply(miss, b"TM0")
    tag, vals = groups_of(reply)
    check(handled and tag == "PO" and vals[2] == "",
          "an unknown Content ID still gets a well-formed empty <PO>")
    os.environ["POL_POLPRO_PG_MISS"] = "error"
    try:
        reply, handled = R._pfc_profile_reply(miss, b"TM0")
        tag, vals = groups_of(reply)
        check(handled and tag == "PM" and vals[:1] == ["-721"],
              "POL_POLPRO_PG_MISS=error answers <PM> GAME_PROFILE_NONE",
              "%r %r" % (tag, vals[:1]))
    finally:
        os.environ.pop("POL_POLPRO_PG_MISS", None)

    # 6. DECLINING IS SAFE. Everything that is not a `<PG>` must fall through
    #    untouched, and the kill switch must restore the template path -- the
    #    two ways this cannot cost a session it did not already own.
    check(R._pfc_profile_reply(b"P<CR>\x070x1\x07", b"TM0") == (None, False),
          "a non-<PG> class-P command falls through")
    check(R._pfc_profile_reply(b"P<PG>\x07\x07", b"MJS") == (None, False),
          "a <PG> with no Content ID falls through to the template")
    os.environ["POL_POLPRO_PG"] = "0"
    try:
        check(R._pfc_profile_reply(LIVE_PG, b"MJS") == (None, False),
              "POL_POLPRO_PG=0 restores the polpro.json entry")
    finally:
        os.environ["POL_POLPRO_PG"] = "1"

    # 7. THE SHIPPED TEMPLATE. It answers when the code path declines, so it has
    #    to carry the right code and the right arity even though it has no data.
    #    The template files ship with the titles (services/titles.py merges
    #    them over /config/polpro.json), so read the MERGED table the server
    #    answers from, and check only the keys it carries.
    spec = polpro.reply_spec()
    for key in [k for k in ("PG", "MJS:PG", "TM0:PG") if k in spec]:
        tag, vals = groups_of(polpro.reply_for(LIVE_PG, spec=spec,
                                               tag=key.split(":")[0].encode()
                                               if ":" in key else None))
        check(tag == "PO" and len(vals) == polpro.PROFILE_PO_VALUES,
              "polpro.json %-8s is <PO> with 71 values" % key,
              "%r/%d" % (tag, len(vals)))
        check(vals[0] == "0x000000003B9ACA03",
              "polpro.json %-8s echoes the requested id" % key, repr(vals[0]))

    # 8. EVERY `MJS:` REPLY MUST LEAD WITH A TAG JANHOUROU HAS AN ARM FOR.
    #
    #    This is the fourth instance of one bug, so it gets an assertion rather
    #    than another paragraph of prose. Jan's dispatcher (`0x002f3178` + the
    #    26-entry jump table at `0x00419780`) maps group[0]'s CODE to a handler
    #    id and switches on that; **anything with no arm falls to 0x26 and is
    #    silently dropped before any handler runs.** So a reply led by the wrong
    #    tag is not "unsatisfying" -- it is never delivered, and the only symptom
    #    is a scene that waits for ever.
    #
    #    Priors: `<CI>` and `<PR>` retired for it; `<GR>` answered by the `*`
    #    wildcard's echo ("NEVER LEAD A REPLY WITH AN ECHO"); and 2026-08-24,
    #    `MJS:RR` holding TM's `<RF>` -- the exact swap the per-title tag keys
    #    were introduced to prevent, sitting in the entry they were added for.
    #
    #    The set below is jan's WHOLE reply-consuming surface, enumerated from
    #    its symbol strings in polpro.json's README:
    JAN_ARMS = {
        "CS": 75, "NS": 76,     # pfcCharaPoolResult      0x002f25a8  +0xb0
        "GK": 82, "GG": 85,     # pfcCharaProfileResult   0x002f2690  +0xb8
        "OK": 88, "OG": 89,     # pfcRankUpdResult        0x002f2920  +0xc0
        "PO": 96, "PM": 97,     # the profile GET arms    0x2F / 0x30
        # rkc.c, read end to end 2026-09-04 (jan audit finding 17): the RANK
        # LIST result `rkc__002f1cb0` maps <RF> (91, success) and <RG> (92,
        # failure) and nothing else -- <OK> is dropped unread there. That
        # retracts the 08-24 "91 has no arm in jan" reading for THIS request;
        # `MJS:RR` in polpro.json is now the <RG> failure fallback behind
        # responders._jan_rank_reply. Added 2026-09-05 when the suite went red.
        "RF": 91, "RG": 92,     # rkc rank-list result    rkc__002f1cb0
    }
    for key, entries in sorted(spec.items()):
        if not key.startswith("MJS:") or not isinstance(entries, list):
            continue
        first = entries[0] if entries else None
        lead = first if isinstance(first, str) else (
            first[0] if isinstance(first, (list, tuple)) and first else None)
        ok = lead in JAN_ARMS
        check(ok, "polpro.json %-8s leads with an arm jan actually has" % key,
              ("<%s> = %d" % (lead, JAN_ARMS[lead])) if ok else
              "%r is NOT one of %s -- jan's dispatcher drops it unread and the "
              "scene waits for ever" % (lead, "/".join(sorted(JAN_ARMS))))
        # <RF> is TM's success arm on every OTHER request; on the rank list it
        # is jan's own (rkc.c), so RR is exempt from this check.
        check(lead != "RF" or key == "MJS:RR",
              "polpro.json %-8s is not handed TM's <RF>" % key,
              repr(lead) if lead != "RF" else
              "<RF> is TM's success arm and has NO arm in jan (see MJS:RR, "
              "2026-08-24)")

    print("\n%s" % ("pfc_profile OK" if not FAILS
                    else "FAILED: " + ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
