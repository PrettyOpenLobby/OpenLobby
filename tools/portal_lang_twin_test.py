#!/usr/bin/env python3
"""Prove a European English Viewer gets the en-US data files.

    python tools/portal_lang_twin_test.py

WHY THIS EXISTS. A UK 360 sends `[en]` like a US one, but its $_USER_LANG is
`en-GB`, and SE's pages build their data URLs from it. Our tree only has the
en-US copies, so every news file fell to the built-in page and the client drew
an empty news bar and `(Variable Error)` on the FFXI page (2026-10-27). The
lobby band now serves the same-language twin. Checked through
`responders._serve_http_on_lobby`, the door the Viewer fetches through, against
a scratch www/: the twin is found wherever the tag sits in the path, a real
en-GB file still wins, and no other language is ever substituted.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "services"))

TMP = tempfile.mkdtemp(prefix="langtwin-")
WWW = os.path.join(TMP, "www")
LOGS = os.path.join(TMP, "logs")
os.makedirs(WWW)
os.makedirs(LOGS)
os.environ["POL_WWW_DIR"] = WWW
os.environ["POL_LOG_DIR"] = LOGS
os.environ["POL_CONFIG"] = os.path.join(TMP, "none.yaml")
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_ACCOUNTS_ENFORCE"] = "0"
os.environ.pop("POL_PORTAL_LANG_FALLBACK", None)

import responders                                # noqa: E402

ok = True
HOST = "wh000.pol.com"
UA = "PlayOnline-PML-Viewer/1.00 [en] (Xbox360)"


def check(label, got, want=True):
    global ok
    good = got == want
    ok = ok and good
    print(f"  {'ok  ' if good else 'FAIL'} {label}")
    if not good:
        print(f"       got  {got!r}\n       want {want!r}")


def put(rel, body):
    path = os.path.join(WWW, HOST, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)


def band_get(path):
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nUser-Agent: {UA}\r\n"
           f"Accept: text/x-playonline-pml\r\n"
           f"Authorization: x-MD5-pol username=\"t\", response=\"0\"\r\n"
           f"Connection: close\r\n\r\n").encode()
    a, b = socket.socketpair()

    def serve():
        try:
            responders._serve_http_on_lobby(b, b"", "192.0.2.5:5000", 51300)
        finally:
            try:
                b.close()
            except OSError:
                pass
    t = threading.Thread(target=serve)
    t.start()
    a.sendall(req)
    a.shutdown(socket.SHUT_WR)
    a.settimeout(15)
    resp = b""
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            resp += d
    except OSError:
        pass
    t.join(15)
    a.close()
    return resp.partition(b"\r\n\r\n")[2]


NEWS = b'<define name="$LATESTNEWSMAX" value="1">\r\n'
TOPICS = b'<define name="$TOPICSMAX" value="3">\r\n'
ANEWS = b'<define name="$subject" value="x">\r\n'
PNG = b"\x89PNG\r\n\x1a\nbanner"
put("pcd/ntool/en-US/latestnews.pml", NEWS)
put("pcd/topics/ff11/en-US/latest_topics.pml", TOPICS)
put("pcd/sftp/en-US/ff11/XBOX360/anews.pml", ANEWS)
put("pml/main/bn_i/en-US/XBOX360/bn1.png", PNG)
put("pcd/ntool/ja-JP/news5.pml", b'<define name="$JA" value="1">\r\n')

try:
    print("en-GB paths are served their en-US twin")
    check("main menu news (tag at segment 3)",
          band_get("/pcd/ntool/en-GB/latestnews.pml"), NEWS)
    check("FFXI topics (tag at segment 4)",
          band_get("/pcd/topics/ff11/en-GB/latest_topics.pml"), TOPICS)
    check("FFXI forced news",
          band_get("/pcd/sftp/en-GB/ff11/XBOX360/anews.pml"), ANEWS)
    check("main menu banner image",
          band_get("/pml/main/bn_i/en-GB/XBOX360/bn1.png"), PNG)

    print("a real en-GB file still wins")
    GB = b'<define name="$LATESTNEWSMAX" value="7">\r\n'
    put("pcd/ntool/en-GB/latestnews.pml", GB)
    check("en-GB copy served as is",
          band_get("/pcd/ntool/en-GB/latestnews.pml"), GB)

    print("another language is never substituted")
    check("en-GB never gets the ja-JP file",
          b"$JA" in band_get("/pcd/ntool/en-GB/news5.pml"), False)
    check("fr-FR never gets the en-US file",
          band_get("/pcd/ntool/fr-FR/latestnews.pml") == NEWS, False)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
