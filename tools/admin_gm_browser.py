#!/usr/bin/env python3
"""The admin panel's GM Calls tab, driven in headless Chrome on loopback.

Seeds two GM calls, each with its own chat room, the first buried under 300
roster records the way a real room is, then checks that:
  * nothing is open and no chat shows until a request is picked;
  * picking a request shows ITS chat, including a first line older than the
    roster chatter; picking another switches; picking it again closes it;
  * there is no room picker, no owner diagnostics, no caller IP;
  * the page raises no errors.

    python tools/admin_gm_browser.py [--shots DIR]

Needs Chrome and websocket-client; uses a throwaway PostgreSQL (pgtest).
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.abspath(os.path.join(HERE, os.pardir, "services"))
LEGACY_TOOLS = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir,
                                            "legacy", "tools"))

TMP = tempfile.mkdtemp(prefix="admingm-")
os.environ["POL_GMCHAT_SPOOL"] = os.path.join(TMP, "gm-chat")

sys.path.insert(0, LEGACY_TOOLS)
from fe_panel_browser import Browser, CHROME_CANDIDATES  # noqa: E402
# fe_panel_browser puts legacy/services first; this panel's own code wins.
sys.path.insert(0, HERE)
sys.path.insert(0, SERVICES)

import pgtest  # noqa: E402

pgtest.use_fresh_database()

import accounts  # noqa: E402
import gmchat  # noqa: E402
from admin_http import free_port, start_panel, stop  # noqa: E402

OWNER, OWNER_PW = "op", "correct horse 1"
bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def seed():
    calls = os.path.join(TMP, "gm-calls")
    os.makedirs(calls, exist_ok=True)
    for tid, no, handle, subj, when in (
            ("gm-20260930T051000-1", 1, "Bluebell", "Someone took my pasta",
             "2026-09-30T05:10:00Z"),
            ("gm-20260930T052000-2", 2, "Birdie", "Stuck in the lobby",
             "2026-09-30T05:20:00Z")):
        with open(os.path.join(calls, tid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"received_at": when, "request_no": no, "guid": 1000 + no,
                       "handle": handle, "content_id": 1, "issue": 3,
                       "subject": subj, "body": "please help",
                       "peer": "203.0.113.9:5555",
                       "room": "#gmcall%03d" % no}, f)
    a, b = b"#gmcall001", b"#gmcall002"
    gmchat.record(a, "in", b"UHAZEL", gmchat.encode_text("hiii"))
    for _ in range(300):
        gmchat.record(a, "in", b"UHAZEL", b"HRu87960930222113Hazelnut")
    gmchat.record(a, "out", b"GM", gmchat.encode_text("did u steal my pasta"))
    gmchat.record(a, "in", b"UHAZEL", gmchat.encode_text("totally not"))
    gmchat.record(b, "in", b"UBIRDIE", gmchat.encode_text("hello?"))


def main():
    if not any(os.path.exists(c) for c in CHROME_CANDIDATES):
        print("SKIP admin_gm_browser: no Chrome/Edge on this box")
        return 0
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    seed()
    db = accounts.connect()
    accounts.set_admin_cred(db, OWNER, OWNER_PW)
    db.commit()
    db.close()
    proc, url = start_panel(TMP, SERVICES, free_port())
    b = None
    log_text = "document.querySelector('#gmLog').innerText"
    visible = ("(s=>{const e=document.querySelector(s);"
               "return !!(e && e.offsetParent !== null)})(%s)")
    try:
        b = Browser(width=1400, height=1000)
        b.goto(url, settle=1.2)
        b.js("document.querySelector('#u').value=%s;document.querySelector('#p').value=%s"
             % (json.dumps(OWNER), json.dumps(OWNER_PW)))
        b.click_el("#go")
        b.pump(3.0)
        if shots:
            for tab in ("overview", "accounts", "codes", "reports", "issues",
                        "news", "security"):
                b.goto(url + "#" + tab, settle=1.5)
                b.call("Emulation.setDeviceMetricsOverride", width=1400,
                       height=int(b.js("document.body.scrollHeight")), deviceScaleFactor=1,
                       mobile=False)
                b.screenshot(os.path.join(shots, tab + ".png"))
                b.call("Emulation.setDeviceMetricsOverride", width=1400, height=1000,
                       deviceScaleFactor=1, mobile=False)

        b.errors.clear()                 # other tabs 404 on art this box lacks
        b.goto(url + "#gmcalls", settle=2.5)
        if shots:
            b.screenshot(os.path.join(shots, "gm-none.png"))
        chk("both requests are listed",
            b.js("[...document.querySelectorAll('.gm-item-who')].map(e=>e.innerText.trim())"),
            ["Birdie", "Bluebell"])
        chk("nothing is picked on arrival",
            b.js("document.querySelectorAll('.gm-item.sel').length"), 0)
        chk("so no chat shows", b.js(visible % json.dumps("#gmLog")), False)
        chk("there is no room picker", b.js("!!document.querySelector('#gmRoomPick')"), False)
        chk("the owner diagnostics are gone", b.js("!!document.querySelector('#gmDiag')"), False)

        b.js("document.querySelector('.gm-item[data-id=\"gm-20260930T051000-1\"]').click()")
        b.pump(2.0)
        if shots:
            b.screenshot(os.path.join(shots, "gm-bluebell.png"))
        text = b.js(log_text) or ""
        chk("picking Bluebell shows her chat", b.js(visible % json.dumps("#gmLog")), True)
        chk("including the first line under the roster chatter", "hiii" in text, True)
        chk("and the latest", "totally not" in text, True)
        chk("and nobody else's", "hello?" in text, False)
        meta = b.js("document.querySelector('#gmTMeta').innerText")
        chk("the caller's IP is not shown", "203.0.113.9" in meta, False)
        chk("nor the raw issue number", "issue type" in meta, False)
        chk("player lines name the player, not the chat nick",
            ("Bluebell" in text or "Player" in text, "UHAZEL" in text), (True, False))

        b.js("document.querySelector('.gm-item[data-id=\"gm-20260930T052000-2\"]').click()")
        b.pump(2.0)
        text = b.js(log_text) or ""
        chk("picking Birdie switches to her chat", ("hello?" in text, "hiii" in text),
            (True, False))
        b.js("document.querySelector('.gm-item[data-id=\"gm-20260930T052000-2\"]').click()")
        b.pump(1.0)
        chk("picking her again closes it",
            (b.js("document.querySelectorAll('.gm-item.sel').length"),
             b.js(visible % json.dumps("#gmLog"))), (0, False))
        b.pump(5.0)
        chk("and the next poll leaves it closed", b.js(visible % json.dumps("#gmLog")), False)
        chk("the page raised no errors", b.errors, [])
    finally:
        if b:
            b.close()
        stop(proc)
        shutil.rmtree(TMP, ignore_errors=True)
    print("all checks passed" if not bad else "%d check(s) FAILED" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
