#!/usr/bin/env python3
"""The Inbox's Game help (adminhelp.py) and the account window's "In the games"
(adminplayers.py), end to end through the admin protocol.

    python tools/admin_help_test.py

A made-up game, Kart Club, serves a help desk and player lookups through tools/olport.py.
Checks that the owner and a moderator with the game's help permission see its open requests,
waiting ones first, read a thread, answer it (with the panel user's name reaching the game) and
close it; that a moderator without the permission sees nothing and cannot answer; that an empty
answer never reaches the game; that the account window lists the player's characters only for a
moderator who may see the game; and that a game that stops answering is named while the panel
carries on. Throwaway PostgreSQL (tools/pgtest.py); the panel runs as a subprocess.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import olport  # noqa: E402

TOKEN = "kart-token-0123456789"
port = olport.AdminPort("kart", "Kart Club", token=TOKEN)
THREADS = {
    "7": {"subject": "Stuck in the pit lane", "who": "Arden", "at": 1793000000, "closed": False,
          "messages": [{"from": "player", "subject": "Stuck in the pit lane", "text": "My kart will not leave.",
                        "at": 1793000000}]},
    "8": {"subject": "Thanks", "who": "Mira", "at": 1793001000, "closed": False,
          "messages": [{"from": "player", "text": "Lap times fixed?", "at": 1793001000},
                       {"from": "staff", "text": "Yes.", "at": 1793002000}]},
    "9": {"subject": "Old one", "who": "Arden", "at": 1792000000, "closed": True, "messages": []},
}
SAID = []
ASKED = []


def _row(tid, t):
    return {"id": tid, "subject": t["subject"], "who": t["who"], "at": t["at"], "closed": t["closed"],
            "waiting": bool(t["messages"]) and t["messages"][-1]["from"] == "player",
            "count": len(t["messages"])}


def _thread(tid):
    t = THREADS.get(tid)
    return t and {"id": tid, "subject": t["subject"], "closed": t["closed"],
                  "who": {"name": t["who"], "online": tid == "7"}, "messages": t["messages"]}


def _reply(tid, text, close, actor):
    THREADS[tid]["messages"].append({"from": "staff", "text": text, "at": 1793003000})
    THREADS[tid]["closed"] = THREADS[tid]["closed"] or close
    SAID.append((tid, text, close, actor))
    return True, "Answer sent."


def _close(tid, actor):
    THREADS[tid]["closed"] = True
    SAID.append((tid, "close", True, actor))
    return True, "Closed."


port.help_desk(lambda open_only: [_row(k, t) for k, t in THREADS.items() if not (open_only and t["closed"])],
               _thread, _reply, _close)


@port.player
def player(who):
    ASKED.append(who)
    if "ARDEN" not in [h.upper() for h in who["handles"]]:
        return None
    return {"characters": [{"name": "Arden", "detail": "Class A, 41 races", "online": True,
                            "facts": [{"label": "best lap", "value": "1:02.4"}]}]}


_h, kport = port.start()
os.environ["POL_ADMIN_MODULES"] = "kart"
os.environ["POL_ADMIN_GAME_KART_URL"] = "http://127.0.0.1:%d" % kport
os.environ["POL_ADMIN_GAME_KART_TOKEN"] = TOKEN

import accounts  # noqa: E402
import adminusers  # noqa: E402
from admin_http import Client, free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got, "" if ok else "  (want %r)" % (want,)))


def main():
    tmp = tempfile.mkdtemp(prefix="adminhelp-")
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    adminusers.create_mod(db, "gm_help", "pass for gm_help", ["game.kart", "game.kart.help", "accounts_view"], by=OWNER)
    adminusers.create_mod(db, "gm_view", "pass for gm_view", ["accounts_view"], by=OWNER)
    polid = "KARTPOL1"
    accounts.create_polid(db, polid, "pw-account")
    arden = accounts.add_member(db, polid, "ardenlogin", "pw-member")
    accounts.set_handle(db, arden, "Arden")
    # a login on another game's server Arden has proved is theirs (game_link, migration 0014)
    db.execute("INSERT INTO game_link (game, account_key, account_name, member_id, linked_at)"
               " VALUES ('kart', '0012345678', 'arden_k', %s, '2026-10-21T00:00:00Z')", (arden,))
    accounts.set_handle(db, accounts.add_member(db, polid, "brynlogin", "pw-member2"), "Bryn")
    db.commit()
    db.close()
    proc, url = start_panel(tmp, SERVICES, free_port(),
                            env_extra={k: os.environ[k] for k in os.environ if k.startswith("POL_ADMIN_")})
    try:
        owner, gm_help, gm_view = Client(url), Client(url), Client(url)
        owner.login(OWNER, OWNER_PW)
        gm_help.login("gm_help", "pass for gm_help")
        gm_view.login("gm_view", "pass for gm_view")

        print("Game help")
        chk("the game's help permission is one to give",
            "game.kart.help" in (owner.call("api/session")[1].get("perm_labels") or {}), True)
        code, d = gm_help.call("api/help")
        chk("open requests, the one waiting for us first",
            (code, [(t["id"], t["waiting"]) for t in d.get("threads", [])]), (200, [("7", True), ("8", False)]))
        chk("...with the game named", d["threads"][0]["game_title"], "Kart Club")
        chk("all requests include the closed one", len(gm_help.call("api/help?open=0")[1]["threads"]), 3)
        code, t = gm_help.call("api/help/thread?game=kart&id=7")
        chk("a thread, with who asked and whether they are online",
            (code, t.get("who"), [m["from"] for m in t.get("messages", [])]),
            (200, {"name": "Arden", "online": True}, ["player"]))
        chk("a request that is not there", gm_help.call("api/help/thread?game=kart&id=99")[0], 404)
        chk("without the permission: an empty desk, and no thread",
            (gm_view.call("api/help")[1].get("threads"), gm_view.call("api/help/thread?game=kart&id=7")[0]),
            ([], 403))
        chk("...and no answer", gm_view.call("api/help", {"game": "kart", "id": "7", "action": "reply",
                                                          "text": "hi"})[0], 403)
        chk("an empty answer never reaches the game",
            (gm_help.call("api/help", {"game": "kart", "id": "7", "action": "reply", "text": "  "})[0], SAID), (400, []))
        code, out = gm_help.call("api/help", {"game": "kart", "id": "7", "action": "reply",
                                              "text": "Press start twice.", "close": True})
        chk("an answer reaches the game with who sent it",
            (code, out.get("msg"), SAID), (200, "Answer sent.", [("7", "Press start twice.", True, "gm_help")]))
        chk("...and the request is answered and closed",
            [t["id"] for t in owner.call("api/help")[1]["threads"]], ["8"])
        chk("closing one", gm_help.call("api/help", {"game": "kart", "id": "8", "action": "close"})[0], 200)
        rows = owner.call("api/audit")[1].get("rows") or []
        chk("answers and closes are in the activity log", sum("game help:" in str(r) for r in rows) >= 2, True)

        print("In the games")
        if polid:
            code, d = owner.call("api/account-games?polid=" + polid)
            chk("the account window asks the game about this player",
                (code, [(g["title"], [c["name"] for c in g["characters"]]) for g in d.get("games", [])]),
                (200, [("Kart Club", ["Arden"])]))
            chk("...each of its handles and members on its own",
                (sorted(ASKED[-1]["handles"]), len(ASKED[-1]["members"]), ASKED[-1]["polid"]),
                (["Arden", "Bryn"], 2, polid))
            chk("...and the logins its members proved on other games' servers",
                ASKED[-1].get("links"), {"kart": [{"key": "0012345678", "name": "arden_k"}]})
            chk("...with what the game says about them",
                d["games"][0]["characters"][0]["facts"], [{"label": "best lap", "value": "1:02.4"}])
            chk("a moderator who may not see the game is not shown it",
                gm_view.call("api/account-games?polid=" + polid)[1].get("games"), [])
        else:
            print("  skip: accounts.create_account is not here")
        chk("an account that is not there", owner.call("api/account-games?polid=NOPE0000")[0], 404)

        print("a game that stops answering")
        port.stop()
        code, d = owner.call("api/help")
        chk("the desk names it and carries on", (code, any("Kart Club" in p for p in d.get("problems", []))), (200, True))
        if polid:
            code, d = owner.call("api/account-games?polid=" + polid)
            chk("...and so does the account window", (code, any("Kart Club" in p for p in d.get("problems", []))),
                (200, True))
    finally:
        stop(proc)
        port.stop()
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
