#!/usr/bin/env python3
"""The maintenance warning: forced news on every title page, and a message to
everyone online.

    python tests/test_maintenance_warning.py

Retail had no wire broadcast. SE warned with `anews.pml` (the forced news a
title page shows before Play) and with mail. services/maintnotice.py writes
and clears the forced news without losing the file it replaced, and
core/broadcast.py mints one message per handle of every online member.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pgtest  # noqa: E402
pgtest.use_fresh_database()
tmp = tempfile.mkdtemp(prefix="maint-")
os.environ["POL_DATA_DIR"] = tmp
os.environ["POL_LOG_DIR"] = tmp

import accounts  # noqa: E402
import maintnotice  # noqa: E402
import pmleval  # noqa: E402
import responders  # noqa: E402,F401
from core import broadcast  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


www = os.path.join(tmp, "www")
SE_FILE = b'<meta http-equiv="Cache-Control" content="no-cache">\n<define name="$length" value="10">\nSE\n'
files = {
    "wh000.pol.com/pcd/sftp/en-US/ff11/anews.pml": SE_FILE,
    "wh000.pol.com/pcd/sftp/en-US/fmo/WIN/anews.pml": maintnotice.EMPTY.encode(),
    "wh000.pol.com/pcd/sftp/ja-JP/jan/anews.pml": "<define name=\"$length\" value=\"0\">\n<!-- 日本 -->\n".encode("cp932"),
    "wh000.pol.com/pcd/sftp/tetra/anews.pml": maintnotice.EMPTY.encode(),
    "_lang/en-US/wh000.pol.com/pcd/sftp/en-US/dc/anews.pml": maintnotice.EMPTY.encode(),
}
for rel, data in files.items():
    p = os.path.join(www, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(data)

print("the forced news")
chk("every anews.pml in every root is found", len(maintnotice.anews_files(www)), 5)
chk("titles narrow it", [os.path.basename(os.path.dirname(f))
                         for f in maintnotice.anews_files(www, ["ff11", "tetra"])],
    ["ff11", "tetra"])
text = maintnotice.render("Maintenance", 'Down at 10:00 UTC.\nIt\'s "short" & quick. ' + "y" * 3000)
rep, env = {}, {}
pmleval.expand(text, report=rep, env=env)
chk("the file evaluates clean", rep.get("missing"), [])
chk("$length is non-zero, so the page shows it", env.get("$length"), "10")
chk("ten strings, as SE wrote it", len(env.get("$news")), 10)
chk("every string stays under the 0x400 value limit",
    all(len(s) < 0x400 for s in env["$news"]), True)
chk("no raw quote or ampersand reaches the file",
    ('"short"' in text, " & " in text), (False, False))
done = maintnotice.post(www, "Maintenance", "Down at 10:00 UTC.")
chk("posted to all five", len(done), 5)
st = {s["file"]: s for s in maintnotice.status(www)}
chk("status shows each one posted and shown",
    all(s["posted"] and s["shown"] for s in st.values()), True)
jp = open(os.path.join(www, *"wh000.pol.com/pcd/sftp/ja-JP/jan/anews.pml".split("/")), "rb").read()
chk("a cp932 file stays cp932 (decodes, and not as UTF-8 text with a BOM)",
    jp.decode("cp932").startswith("<meta"), True)
maintnotice.post(www, "Again", "Second notice.")
back = maintnotice.clear(www)
chk("clear restores all five", len(back), 5)
chk("SE's own forced news is back byte for byte",
    open(os.path.join(www, *"wh000.pol.com/pcd/sftp/en-US/ff11/anews.pml".split("/")), "rb").read(),
    SE_FILE)
chk("no backup left behind",
    [f for f in maintnotice.anews_files(www) if os.path.exists(f + maintnotice.BACKUP_SUFFIX)], [])
try:
    maintnotice.post(www, "", "x")
    chk("an empty subject is refused", False, True)
except ValueError:
    chk("an empty subject is refused", True, True)

print("the message to everyone online")
db = accounts.connect()
a = accounts.register_account(db, "Online1", "Passw0rdOne", contents=(1,))
b = accounts.register_account(db, "Offline1", "Passw0rdTwo", contents=(1,))
accounts.open_session(db, a["member_id"], nick="Online1")
sent = []


def mint(sender, sguid, rguid, subject, body):
    sent.append((sender, rguid, subject, body))
    return "O/m/x"


members, n = broadcast.send_broadcast("Maintenance now", "Servers stop at 10:00.", mint=mint)
chk("one online member", members, 1)
chk("one message per handle of that member",
    n, len(db.execute("SELECT id FROM handle WHERE member_id = %s",
                      (a["member_id"],)).fetchall()))
chk("addressed by the handle guid",
    {r for _s, r, _j, _b in sent} <= {accounts.handle_guid(h["id"]) for h in db.execute(
        "SELECT id FROM handle WHERE member_id = %s", (a["member_id"],)).fetchall()}, True)
chk("the subject is cut to what the list shows", sent[0][2], "Maintenance now"[:15])
chk("the offline member got nothing",
    any(r in {accounts.handle_guid(h["id"]) for h in db.execute(
        "SELECT id FROM handle WHERE member_id = %s", (b["member_id"],)).fetchall()}
        for _s, r, _j, _b in sent), False)
reply = broadcast._request('{"reply": "broadcast:reply:t", "expires": 0, "subject": "x", "text": "y"}')
chk("an expired panel request is dropped", reply, None)

shutil.rmtree(tmp, ignore_errors=True)
print()
print("test_maintenance_warning: " + ("OK" if not bad else "FAILED (%d)" % bad))
sys.exit(1 if bad else 0)
