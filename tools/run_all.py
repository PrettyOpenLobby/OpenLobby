#!/usr/bin/env python3
"""Run every self-test this server has, and exit non-zero if any of them fails.

    python tools/run_all.py                  # everything
    python tools/run_all.py -k friend group  # only suites matching a substring
    python tools/run_all.py -v               # stream each suite's own output

WHY THIS EXISTS. Until 2026-08-16 there was no such entry point: eight scripts
under tools/, six modules with `--selftest`, and one suite reachable only
through an undocumented environment variable. Nothing ran them together, so
nothing noticed when two of them went red --

  * `accounts.py`'s self-test had been failing since the friend-accept change
    that same day, and it fails EARLY: 74 of its 116 asserts (the 2:6 whole-list
    PUT path, group membership, POL ID reissue) had not executed since.
  * `group_check.py` was asserting a group-member rule that `_group_role`
    deliberately inverted, so it failed on every run.

Both were stale expectations rather than product bugs, which is exactly the
failure mode a runner catches and a human does not: each suite still passed the
day it was written, and no single change looked like it broke anything.

REGISTERING A SUITE IS DELIBERATE. The list below is explicit rather than
globbed. A new test should be added here by whoever writes it -- globbing
`tools/*_test.py` would silently skip `group_check.py` and `verify_profile.py`
(neither matches), and would silently ADOPT the next scratch script somebody
leaves in the directory.

WARNING: THE COST OF THAT IS REAL, AND IT IS PAID BY WHOEVER FORGETS. An audit on
2026-08-17 found `gmcrypt_test.py`, `gmd_test.py` and `fmo.py --selftest`
written, passing, and registered nowhere -- three in a row, so the GM Call
handshake and the FMO frame codec had never once been checked by anything that
runs on its own. All three were registered the same day. If you write a suite
and do not add it here, it does not exist.

THE SAME AUDIT FOUND THE OPPOSITE FAILURE: seven of twenty-one suites red, five
of them for a full day, all five stale expectations left behind by deliberate
behaviour changes (the both-active friend accept, the silent presence default,
`u/account` at 668, SE's empty IRC prefix host, the masked group member id).
A runner nobody can trust is worth less than no runner, because a genuine
regression lands invisibly among the noise -- which is how the 07:12 group
member-count bug survived: `group_check` HAD been failing on it, under four
other failures nobody had triaged. **Fix a suite the day you change what it
pins.**

**THE TREE IS GREEN AS OF 2026-08-19 -- 34/34.** The two long-running failures
were both STALE EXPECTATIONS, not product bugs, and both had been red since the
day the thing they pinned was deliberately changed:

  * `gmd` died on an `AttributeError` before its first 0x801 check, because the
    queue depth stopped being a module constant (`gmd.QUEUE`) and became live
    logic (`Gmd.queue_depth`, with `POL_GMD_QUEUE` as the pin). It now builds the
    reply the way the SERVER does and covers the live depth as well.
  * `smoke_chain` asserted the room record's RETRACTED field map. `+0x39` is
    z_npers (the headcount) and `+0x99` is z_chlock (the padlock); the two were
    swapped when they were re-measured live, and the old reading was only ever
    plausible because SE's FOXROOM sample was both occupied and keyed. It now
    checks the room before and after `MODE +k`, which is the pair of
    observations that tells the two readings apart.

Both are exactly the failure this docstring is about, so: **fix a suite the day
you change what it pins.**

WARNING: **`resume` IS A KNOWN ~25% FLAKE, AND IT IS NOT YOURS.** Measured 2026-08-19
across 44 runs: 11 failures, at the same rate with the account-DB connection pool
ON (5/22) and OFF (6/22), so it correlates with neither the pool nor anything
else changed that day. It also flakes run alone, so it is not contention with
another suite. Every failure looks identical from the outside -- the login
returns `b''` and `authserv.log` stops mid-handshake -- which is why it has
never been explained.

**Do not read a small sample as a regression.** Ten runs looked like a clean
5/10-vs-1/10 correlation with the pool; the next twelve inverted it. If you think
you have broken this suite, get thirty runs before you believe it.

It is now DIAGNOSABLE: `resume_test` keeps the authserv subprocess's stdout and
stderr (they went to DEVNULL, which is why a raising handler thread left no
trace). The first capture of a healthy run shows the server completing the login
AND the later resume correctly, which points at the test's own client timing
rather than at the server.

SEQUENTIAL, NOT PARALLEL, and that is not laziness. Several suites bind real
sockets and `resume_test` starts and kills a real `authserv` subprocess; two of
those at once would fight over ports. It also keeps concurrent `python`
processes low, which matters on the dev box where strays hold a lock on
`polinject.dll`.

POL_DATA_DIR, POL_RESOURCE_DIR, POL_LOG_DIR and POL_LOGIN_PW_KEYFILE that
are not set point into a temporary directory made for the run and removed
at the end (scratch_state).
"""
import argparse
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
SERVICES = os.path.join(ROOT, "services")
TESTS = os.path.join(ROOT, "tests")

#: (name, argv, cwd, extra_env). Ordered cheapest-and-most-foundational first,
#: so a broken codec is reported before a suite that spends 30 s failing on it.
SUITES = [
    # --- pure codec / protocol maths, no sockets, no DB --------------------
    ("polnick",       [sys.executable, "polnick.py", "--selftest"],   SERVICES, {}),
    ("polkey",        [sys.executable, "polkey.py", "--selftest"],    TOOLS,    {}),
    ("polpro",        [sys.executable, "polpro.py", "--selftest"],    SERVICES, {}),
    ("chr_put",       [sys.executable, "chr_put_test.py"],           TOOLS, {}),
    # stderr was the ONE stream a deploy could destroy: log() mirrors every
    # channel to <LOG_DIR>/<channel>.log, but warnings and uncaught tracebacks
    # went only to the container stdout that pol-git-sync recreates away.
    ("stderr_capture", [sys.executable, "stderr_capture_test.py"],
                      TOOLS, {}),

    # --- GM Call / GM Chat: the CAST cipher and the captured handshake ------
    ("gmcrypt",       [sys.executable, "gmcrypt_test.py"],  TOOLS, {}),
    # The record language GM chat speaks, plus the transcript that is now the
    # only place a client's own records are kept. Offline, throwaway spool.
    ("gmchat",        [sys.executable, "gmchat_test.py"],   TOOLS, {}),

    # --- the account database: schema, registration, friends, groups -------
    # Gated behind an env var because accounts.py's __main__ is also the
    # admin CLI. It runs against a fresh throwaway database, never a real one.
    ("accounts",      [sys.executable, "accounts.py"], SERVICES,
     {"POL_ACCOUNTS_SELFTEST": "1"}),
    # The ACCOUNT CONNECTION. Registered next to `accounts` because it pins
    # the same file's invariants from the other side: the code was written
    # against sqlite3's connection, and the contract is that its behaviour --
    # transactions, what a close leaves behind, thread exclusivity, types --
    # is unchanged on PostgreSQL.
    ("dbpool",        [sys.executable, "dbpool_test.py"], TOOLS, {}),
    # The lookups the title repositories call instead of reading the account
    # tables with their own SQL (a friend row by id, member ages, the Content
    # ID of every member for one game, a handle's client guid, and a session
    # dated in the past for a test).
    ("accounts_lookups", [sys.executable, "test_accounts_lookups.py"], TESTS, {}),
    # polcore.db and polcore.kv, the PostgreSQL and Valkey layer the services
    # are moving onto. Each starts throwaway containers (tools/pgtest.py) and
    # reports SKIP without Docker; POL_TEST_REQUIRE_DB=1 makes that a failure.
    ("polcore_db",    [sys.executable, "test_polcore_db.py"], TESTS, {}),
    ("polcore_kv",    [sys.executable, "test_polcore_kv.py"], TESTS, {}),
    # The saved-resource store (the `blob` table) and the account deletion
    # that takes a member's saves with it; then the live-session markers and
    # client-build records other containers read from Valkey.
    ("polcore_blobs", [sys.executable, "test_polcore_blobs.py"], TESTS, {}),
    ("live_markers",  [sys.executable, "test_live_markers.py"], TESTS, {}),
    # The importer that moves an old /data tree (the SQLite files and
    # resources/) into PostgreSQL, run against a fixture the pre-PostgreSQL
    # account code writes itself (taken from git).
    ("db_import",     [sys.executable, "test_db_import.py"], TESTS, {}),
    # THE CONTENT ID MINT (2026-08-23). Registered beside `accounts` because it
    # pins that file's `allocate_content_id` from the outside, and because the
    # thing it guards is invisible from inside our own server: nothing we run
    # VALIDATES a Content ID's format, so a mint that reverted to the retracted
    # computed shape would pass every other suite here and only show up against
    # a client that cares. Carries its own negative control -- see its docstring.
    ("content_id",    [sys.executable, "content_id_check.py"], TOOLS, {}),
    # ...and the other half of the same question: does a Content ID we mint
    # still open a Run button? `chargate_check` replays the CLIENT's own 1:3
    # record loop, the launch gate (`app.dll+0x199093`) and FFXI's world lookup
    # over our own payload. It was written 2026-08-14 and REGISTERED NOWHERE
    # until 2026-08-23 -- the fourth instance of exactly what this docstring
    # warns about -- which is how a change to the Content ID shape could have
    # reached a client with nothing having checked the record it rides in. Its
    # fixture now carries both the allocated 8-digit and the legacy 10-digit
    # shape, so it fails if either stops working.
    ("chargate",      [sys.executable, "chargate_check.py"], TOOLS, {}),
    # LATENCY, which no other suite here can see. Every test client in this tree
    # closes after sending, so the reader's idle window never fires for them --
    # and the idle window was costing a real (waiting) client a full second per
    # lobby message. The client that waits is the only one that finds it.
    ("lobby_latency", [sys.executable, "lobby_latency_test.py"], TOOLS, {}),
    # The LOGIN TRACE. Registered next to the latency suite because both exist
    # for the same reason: a failure nobody could explain, because the evidence
    # was being discarded at the moment it existed.
    ("login_trace",   [sys.executable, "login_trace_test.py"], TOOLS, {}),
    ("login_nickcrib", [sys.executable, "login_nickcrib_test.py"], TOOLS, {}),
    ("login_passthrough", [sys.executable, "login_passthrough_test.py"], TOOLS, {}),

    # --- the module split of responders.py, and what it silently defuses ---
    # responders.py is being split by moving blocks out and re-exporting the
    # names. Re-export is transparent to READS but not to REBINDING, and this
    # suite monkeypatches the responders module in 49 places. A moved name is
    # still patchable -- the patch just stops being CONSULTED, with nothing
    # raised and nothing logged, so the test goes on printing [PASS] while
    # testing nothing. Measured 2026-08-27: a session-table cut left
    # dir(responders) identical at all 832 names, preserved identity on every
    # shared container, and still defused 21 rebindings across 9 files.
    ("facade_rebind", [sys.executable, "facade_rebind_check.py"], TOOLS, {}),

    # --- ops, not protocol: /logs cannot grow without bound ----------------
    # Asserts BOTH log writers cap, which is the actual bug -- the rotation
    # lived in responders.py for five days while stub.py, which writes the
    # channel the original 405 MB measurement named, had none.
    ("logrotate",     [sys.executable, "logrotate_test.py"], TOOLS, {}),

    # Tester issue reports: pins the SERVER half (the window bisect, the
    # correlation tiering, the retention caps).
    ("issuereport",   [sys.executable, "issuereport.py"], SERVICES, {}),
    # And the SEAM: the suite above is green even if
    # POST /_shim/report reaches nothing at all. This one drives the real
    # `_serve_http_on_lobby` over a socketpair -- the shim will hit that exact
    # arm, and a routing bug would otherwise be found by a tester getting a 404
    # at the moment they were trying to report something.
    ("issue_route",   [sys.executable, "issue_route_test.py"], TOOLS, {}),

    # --- record builders, checked against bytes captured from SE -----------
    # The group member table's TWO writers must name a member identically, or
    # the push appends a second copy instead of updating the first -- the
    # "I see myself twice in group chat" doubling, A/B'd live 2026-08-25
    # (6 served + 6 pushed = 12 on screen; rowpush=0 -> 6).
    ("group_rowpush", [sys.executable, "group_rowpush_identity_test.py"],
     TOOLS, {}),
    ("friendput",     [sys.executable, "friendput_test.py"],  TOOLS, {}),
    # RENAME + IGNORE, replayed from the 2026-08-19 retail capture. Registered
    # beside the other two 2:6 suites because all three read the SAME record
    # and disagreeing about which kind it is was the bug: a rename was being
    # served to the delete path, and it deleted the friend it meant to caption.
    ("friend_rename", [sys.executable, "friend_rename_test.py"], TOOLS, {}),
    ("presence",      [sys.executable, "presence_test.py"],   TOOLS, {}),
    ("presence_burst", [sys.executable, "presence_burst_test.py"], TOOLS, {}),
    ("presence_grace", [sys.executable, "presence_grace_test.py"], TOOLS, {}),
    ("presence_push_identity", [sys.executable, "presence_push_identity_test.py"],
                      TOOLS, {}),
    ("room_ghost_ttl", [sys.executable, "room_ghost_ttl_test.py"], TOOLS, {}),
    ("lobby_bind",    [sys.executable, "lobby_bind_test.py"], TOOLS, {}),
    ("pmleval",       [sys.executable, "pmleval_test.py"],    TOOLS, {}),
    ("pmlrefs",       [sys.executable, "pmlrefs_test.py"],    TOOLS, {}),
    # Not registered, on purpose: bridge_test needs polboards from a title
    # repository, and info_switcher_test needs portal pages under www/ that
    # this repository does not ship.
    # A slot is an index into the list 2:3 SERVED, and two re-derivations
    # enumerated the raw friend table instead -- so on any account holding an
    # incoming request, every row repaint and every presence push was aimed at
    # the wrong slot and the client dropped it without a sound. Registered
    # beside `presence` because it is the other half of the same symptom.
    ("friend_slot_skew", [sys.executable, "friend_slot_skew_test.py"],
     TOOLS, {}),
    # ACCEPT-TIME presence. The burst covers a 2:3 FETCH and `_broadcast_presence`
    # covers a TRANSITION; a friendship formed mid-session is neither, so both
    # new rows stayed grey until the peer happened to change zone (measured live
    # 2026-08-25, three minutes of grey by luck). Registered beside `presence`
    # because it is the same painter seen from the 2:6 write.
    ("accept_presence", [sys.executable, "friend_accept_presence_test.py"],
     TOOLS, {}),
    # The 4:5 status byte and the 352 WHO letter it drives -- next to `presence`
    # because it is the same subsystem seen from the room instead of the list.
    ("status_who",    [sys.executable, "status_who_test.py"], TOOLS, {}),
    ("titlezone",     [sys.executable, "titlezone_test.py"],  TOOLS, {}),
    ("push",          [sys.executable, "push_test.py"],       TOOLS, {}),
    ("pushspool",     [sys.executable, "pushspool_test.py"], TOOLS, {}),
    ("resource",      [sys.executable, "resource_test.py"],   TOOLS, {}),
    ("verify_profile", [sys.executable, "verify_profile.py"], TOOLS, {}),
    # The OTHER profile -- the per-Content-ID GAME character, on POLpro. Next to
    # `verify_profile` (the handle profile) because conflating the two layers is
    # the standing trap, and because this one has no visible failure at all: the
    # client's parser raises on nothing, so a wrong `<PO>` is a blank popup.
    ("pfc_profile",   [sys.executable, "pfc_profile_test.py"], TOOLS, {}),
    # And the THIRD profile layer: 05:04's other record, the per-Content-ID game
    # character. Registered beside the other two because the standing trap is
    # conflating them -- and this one shares an OPCODE with the handle profile,
    # so only the request TLV tells them apart.
    ("content_profile", [sys.executable, "content_profile_test.py"], TOOLS, {}),
    # The sign-up wizard walked end to end. Registered because the flow's whole
    # failure mode was INVISIBLE to per-page checks: `pmlfit --ucs` was green
    # the entire time the RDT hop was serving three real players an error and
    # bouncing them back without their PlayOnline ID. This one pins the ORDER
    # (step 6 shows the ID, the hop comes after) and the SCHEME of NEXT-URL.
    ("ucs_signup",    [sys.executable, "ucs_signup_test.py"], TOOLS, {}),
    # And the page linter itself, which was written, useful, and registered
    # NOWHERE -- exactly the failure this file's header describes. It catches
    # copy that overflows a panel, i.e. footnotes that are silently not drawn.
    # It exits 1 on a HARD finding (clip/outside/overlap) and 0 on a soft wrap,
    # so it is already a gate; it just had nothing calling it.
    ("pmlfit_ucs",    [sys.executable, "pmlfit.py", "--ucs"], TOOLS, {}),
    # The BUILT-IN portal pages: a release with no portal capture used to show
    # a blank menu with no way to Play. Every synthesized page is run through
    # the PML evaluator as the parser oracle, and both doors (the lobby band
    # the Viewer really uses, and :80) are driven against an empty www/ --
    # with a real file present too, which must win. Registered beside the
    # page linter because it is the same class of check: a page nobody can
    # render in a test, pinned by the rules that have each cost a day.
    ("pmlfallback",   [sys.executable, "pmlfallback_test.py"], TOOLS, {}),
    # Registration codes fold case on lookup and keep their spelling in the row.
    # Replays a live refusal (prod ucs.log 2026-08-29): a player typed their
    # code in lower case, was told it was wrong, and retyped it in caps.
    ("regcode_case",  [sys.executable, "regcode_case_test.py"], TOOLS, {}),
    ("mailident",     [sys.executable, "mailident_test.py"], TOOLS, {}),
    ("mail_auth",     [sys.executable, "mail_auth_test.py"], TOOLS, {}),
    ("extmail",       [sys.executable, "extmail_test.py"], TOOLS, {}),
    # Group channels and group requests are for the group's members.
    ("group_gate",    [sys.executable, "group_gate_test.py"], TOOLS, {}),
    # The mobile 2:3 marker lifts the 12-row cap; the PC reply is unchanged.
    ("friends_mobile", [sys.executable, "friends_mobile_cap_test.py"], TOOLS, {}),
    ("contentauth",   [sys.executable, "contentauth.py"], SERVICES, {}),
    # The shared event calendar the title plugins' tournaments read.
    ("eventcal",      [sys.executable, "eventcal.py", "--selftest"], SERVICES, {}),
    # The Viewer password, checked through the NICK digest, and the arm gate on
    # binding a login token (trust-on-first-use).
    ("login_digest",  [sys.executable, "login_digest_test.py"], TOOLS, {}),
    ("login_token_arm", [sys.executable, "login_token_arm_test.py"], TOOLS, {}),
    # The sign-up wizard's code mode (drives ucscgi over HTTP) and its limits.
    ("signup_mode",   [sys.executable, "signup_mode_test.py"], TOOLS, {}),
    ("signup_limit",  [sys.executable, "signup_limit_test.py"], TOOLS, {}),
    ("polserver2_slim", [sys.executable, "polserver2_slim_test.py"], TOOLS, {}),
    # --- tests/: one script per behaviour, each with its own throwaway DB ---
    ("mgkey",          [sys.executable, "test_mgkey.py"], TESTS, {}),
    ("auth_rsa_ips",   [sys.executable, "test_auth_rsa_ips.py"], TESTS, {}),
    ("auth_user_iv",   [sys.executable, "test_auth_user_iv.py"], TESTS, {}),
    ("auth_rsa",       [sys.executable, "test_auth_rsa.py"], TESTS, {}),
    ("auth_kill_dup",  [sys.executable, "test_auth_kill_dup.py"], TESTS, {}),
    ("login_lockout",  [sys.executable, "test_login_lockout.py"], TESTS, {}),
    ("gate_list_stamps", [sys.executable, "test_gate_list_stamps.py"], TESTS, {}),
    ("lobby_split_frame", [sys.executable, "test_lobby_split_frame.py"], TESTS, {}),
    ("ps2_pacing",     [sys.executable, "test_ps2_pacing.py"], TESTS, {}),
    ("handle_nocase",  [sys.executable, "test_handle_nocase.py"], TESTS, {}),
    ("handle_store_layout", [sys.executable, "test_handle_store_layout.py"], TESTS, {}),
    ("friend_bitfield", [sys.executable, "test_friend_bitfield.py"], TESTS, {}),
    ("friend_request_heal", [sys.executable, "test_friend_request_heal.py"], TESTS, {}),
    ("profile_trailer", [sys.executable, "test_profile_trailer.py"], TESTS, {}),
    # Per-account refusals and notices leave the PS2 layout choice and the
    # old refusal for an inactive member as they were.
    ("notice_keeps_layouts", [sys.executable, "test_notice_keeps_layouts.py"], TESTS, {}),
    # The patch service with no archives, a partial one, and an empty re-login.
    ("polserver2_startup", [sys.executable, "test_polserver2_startup.py"], TESTS, {}),
    # Refusal codes, login notices, the admin panel's controls and Kick.
    ("account_state", [sys.executable, "account_state_test.py"], TOOLS, {}),
    ("kick_cleanup",  [sys.executable, "kick_cleanup_test.py"], TOOLS, {}),
    # Open / resolved / won't fix on the admin panel's report tabs.
    ("admin_triage",  [sys.executable, "admin_triage_test.py"], TOOLS, {}),
    # A GM Call ticket keeps the raw 0x102 body beside the decoded fields.
    ("gmd_ticket_raw", [sys.executable, "test_gmd_ticket_raw.py"], TESTS, {}),
    ("group_check",   [sys.executable, "group_check.py"],     TOOLS, {}),
    # The client's "N/M in chat" counter has two inputs served by two different
    # containers -- the 7:12 member total and the IRC roster -- so neither can
    # be verified from the other's side. This pairs them.
    ("group_counter", [sys.executable, "group_incounter_test.py"], TOOLS, {}),
    # CONTENT, not code: every greeting card SE's own index offers has to be
    # mirrored, or the first client to pick that card gets a broken image and
    # nothing anywhere logs it.
    ("gcard",         [sys.executable, "gcard_check.py"],     TOOLS, {}),

    # --- the ones that bind sockets or fork processes; slowest, so last ----
    # exercises the open-enrolment auth path, so the release default of
    # refusing unknown IDs is switched off for this suite only
    ("smoke_chain",   [sys.executable, "smoke_chain.py"],  TOOLS, {"POL_ACCOUNTS_ENFORCE": "0", "POL_AUTH_FRONT_PREAMBLE": "0"}),
    ("resume",        [sys.executable, "resume_test.py"],  TOOLS, {}),
]

#: Generous: `smoke_chain` drives real listeners and `resume_test` waits out a
#: kill-and-reattach. A suite that exceeds this is reported as a failure with
#: its own name, not as a hung runner.
TIMEOUT_S = 300


def run(suite, verbose):
    name, argv, cwd, extra = suite
    env = dict(os.environ)
    env.update(extra)
    # Unbuffered, so a suite killed by the timeout still shows what it reached.
    env["PYTHONUNBUFFERED"] = "1"
    started = time.time()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, timeout=TIMEOUT_S,
                           stdout=None if verbose else subprocess.PIPE,
                           stderr=subprocess.STDOUT if not verbose else None)
        out = "" if verbose else p.stdout.decode("utf-8", "replace")
        return p.returncode == 0, time.time() - started, out
    except subprocess.TimeoutExpired as exc:
        got = (exc.output or b"").decode("utf-8", "replace") if not verbose else ""
        return False, time.time() - started, got + f"\n*** TIMED OUT after {TIMEOUT_S}s"
    except FileNotFoundError as exc:
        return False, time.time() - started, f"*** cannot run: {exc}"


#: The state paths a suite falls back to when they are unset (see scratch_state).
SCRATCH_VARS = ("POL_DATA_DIR", "POL_RESOURCE_DIR", "POL_LOG_DIR",
                "POL_LOGIN_PW_KEYFILE")


def scratch_state():
    """Point every state path a suite may fall back to at a directory made
    for this run and removed when it ends, unless the caller set it.

    A suite that finds no POL_DATA_DIR uses /data, which on Windows is the
    root of the current drive, so a run could read and write a real server's
    files there. A value already set wins; POL_RESOURCE_DIR then follows
    POL_DATA_DIR, as the services derive it. Returns the directory made, or
    None when every variable was set.
    """
    missing = [k for k in SCRATCH_VARS if not os.environ.get(k, "").strip()]
    if not missing:
        return None
    root = tempfile.mkdtemp(prefix="openlobby-run-")
    atexit.register(shutil.rmtree, root, True)
    if "POL_DATA_DIR" in missing:
        os.environ["POL_DATA_DIR"] = os.path.join(root, "data")
        os.makedirs(os.environ["POL_DATA_DIR"])
    if "POL_RESOURCE_DIR" in missing:
        os.environ["POL_RESOURCE_DIR"] = os.path.join(os.environ["POL_DATA_DIR"],
                                                      "resources")
        if os.environ["POL_RESOURCE_DIR"].startswith(root):
            os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
    if "POL_LOG_DIR" in missing:
        os.environ["POL_LOG_DIR"] = os.path.join(root, "logs")
        os.makedirs(os.environ["POL_LOG_DIR"])
    if "POL_LOGIN_PW_KEYFILE" in missing:
        os.makedirs(os.path.join(root, "keys"))
        os.environ["POL_LOGIN_PW_KEYFILE"] = os.path.join(root, "keys", "login-pw.key")
    return root


def main():
    scratch_state()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", nargs="+", metavar="MATCH", default=None,
                    help="only suites whose name contains one of these")
    ap.add_argument("--skip", nargs="+", metavar="NAME", default=(),
                    help="suites to leave out by exact name (CI skips `resume`, "
                         "the documented ~25%% flake, so a red run means a "
                         "real failure)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stream each suite's output instead of capturing it")
    ap.add_argument("--list", action="store_true", help="list suite names and exit")
    args = ap.parse_args()

    if args.list:
        for name, _argv, _cwd, _e in SUITES:
            print(name)
        return 0

    picked = [s for s in SUITES
              if (args.k is None or any(m.lower() in s[0].lower() for m in args.k))
              and s[0] not in set(args.skip)]
    if not picked:
        print(f"no suite matches {args.k!r}; --list shows them all")
        return 2

    # ONE DATABASE SERVER FOR THE WHOLE RUN. Most suites exercise the account
    # code, which lives in PostgreSQL; each makes its own empty database on the
    # server named here (tools/pgtest.py use_fresh_database), so they cannot see
    # each other's rows, and the run pays for one container instead of fifty.
    # The container goes when this process exits. POL_TEST_DATABASE_URL, when
    # set, names a server to use instead and nothing is started.
    sys.path.insert(0, TOOLS)
    import pgtest
    try:
        os.environ["POL_TEST_DATABASE_URL"] = pgtest.server_url()
        print("database server: "
              + os.environ["POL_TEST_DATABASE_URL"].rsplit("@", 1)[-1])
    except Exception as exc:                  # noqa: BLE001 -- say so, run anyway
        print(f"WARNING: no PostgreSQL for the suites ({exc}); every suite that "
              "needs one will fail")

    # ...AND ONE VALKEY, for the suites whose processes share live state
    # (pgtest.use_fresh_valkey gives each its own key prefix). Every other
    # suite keeps its live state in memory, as a single process does.
    try:
        os.environ["POL_TEST_VALKEY_URL"] = pgtest.valkey_url()
        print("valkey server: "
              + os.environ["POL_TEST_VALKEY_URL"].rsplit("/", 2)[-2])
    except Exception as exc:                  # noqa: BLE001 -- say so, run anyway
        print(f"WARNING: no Valkey for the suites ({exc}); every suite that "
              "needs one will fail")
    # A live-state URL from the caller's shell would make every suite share
    # one store; the suites that need Valkey ask for it themselves.
    os.environ.pop("POL_VALKEY_URL", None)

    results, failures = [], []
    width = max(len(s[0]) for s in picked)
    print(f"running {len(picked)} suite(s)\n")
    for suite in picked:
        name = suite[0]
        print(f"  {name:<{width}}  ... ", end="", flush=True)
        ok, secs, out = run(suite, args.verbose)
        print(f"{'ok' if ok else 'FAIL':<4}  {secs:5.1f}s")
        results.append((name, ok, secs))
        if not ok:
            failures.append((name, out))

    print()
    if not failures:
        total = sum(r[2] for r in results)
        print(f"{len(results)}/{len(results)} suites passed in {total:.1f}s")
        return 0

    # A failure is only useful with the output that produced it, so replay it
    # here rather than making the reader re-run the suite by hand.
    for name, out in failures:
        print("=" * 72)
        print(f"FAILED: {name}")
        print("=" * 72)
        # a failing suite's output can carry Japanese (feworld's own labels);
        # a cp1252 console must not turn that into a SECOND failure
        _tail = out.rstrip()[-4000:]
        try:
            print(_tail)
        except UnicodeEncodeError:
            print(_tail.encode("ascii", "replace").decode("ascii"))
        print()
    print(f"{len(results) - len(failures)}/{len(results)} suites passed, "
          f"{len(failures)} FAILED: {', '.join(n for n, _ in failures)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
