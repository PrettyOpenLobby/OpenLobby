#!/usr/bin/env python3
"""Prove the Q&A knowledge base answers on both doors and searches right.

    python tools/kbserve_test.py

`services/kbserve.py` stands in for SE's /polapps/s/s.kb.pml.* servlet. The
GM Call pages link it over https (sslterm hands that to the :80 door,
stub.py) and, in Japanese, over plain http (the lobby band door); FFXI's
support page includes its GmPolicy list. Checked here against a small
fixture KB, never the real archive:

  * the doors: Menu, List, Qa2 and GmPolicy are served on both, other paths
    are not taken, and a POSTed form (the search page's own) is read;
  * the search: category, keyword (every word, either case, answers too),
    paging, the error page, a bare List (the Viewer's own Q&A Search) giving
    the search page, and the locale from polg_loc or Accept-Language;
  * the output: UTF-8 with a BOM and CRLF, and a keyword carrying quotes,
    apostrophes or angle brackets comes back escaped, never as markup.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "services"))

TMP = tempfile.mkdtemp(prefix="kbserve-")
WWW = os.path.join(TMP, "www")
LOGS = os.path.join(TMP, "logs")
KB = os.path.join(WWW, "_kb")
os.makedirs(KB)
os.makedirs(LOGS)
os.environ["POL_WWW_DIR"] = WWW
os.environ["POL_KB_DIR"] = KB
os.environ["POL_LOG_DIR"] = LOGS
os.environ["POL_CONFIG"] = os.path.join(TMP, "none.yaml")
import pgtest  # noqa: E402
pgtest.use_fresh_database()
os.environ["POL_ACCOUNTS_ENFORCE"] = "0"

import kbserve  # noqa: E402

HOST = "wh000.pol.com"
ok = True


def check(label, got, want=True):
    global ok
    good = got == want
    ok &= good
    print(("  ok   " if good else "  FAIL ") + label + ("" if good else f": {got!r} (want {want!r})"))


def art(i, cat, sub, q, answer):
    rec = (f"&pre=1;&style=qa14_a;Article No.: Q{10000 + i}\nCategory: {cat}\n"
           f"Subcategory: {sub}\nCreated: 2003/06/09\nUpdated: 2006/04/28\nRating: 50&style;\n\n{answer}")
    return {"id": i, "no": f"Q{10000 + i}", "cat": cat, "sub": sub, "q": q, "rec": rec}


EN = [art(1, "FINAL FANTASY XI", "How to Play", "How do I form a party?", "Use the Party menu."),
      art(2, "FINAL FANTASY XI", "GM Policies", "What is a GM?", "A Game Master."),
      art(3, "PlayOnline Viewer", "PlayOnline Mail", "I can&#39;t send mail.", "Check the Mail Account.")]
EN += [art(100 + i, "Tetra Master", "-", f"Card question {i}", "Cards.") for i in range(60)]
JA = [{"id": 11, "no": "Q10011", "cat": "ファイナルファンタジーXI", "sub": "GMポリシー",
       "q": "突然走ることができなくなりました。", "rec": "&pre=1;記事番号：Q10011&style;\n\n歩く"}]
json.dump(EN, open(os.path.join(KB, "en-US.json"), "w", encoding="utf-8"))
json.dump(JA, open(os.path.join(KB, "ja-JP.json"), "w", encoding="utf-8"), ensure_ascii=False)


def body_of(resp):
    head, _, body = resp.partition(b"\r\n\r\n")
    return head.split(b"\r\n", 1)[0], body.decode("utf-8-sig", "replace")


# --------------------------------------------------------------------------- #
print("the parser")
page = ('<define name="$question_data2" value="Why?">\r\r\n<data name="t2" sub="a2">\r\r\n'
        '<record>"&pre=1;&style=qa14_a;記事番号　　　：Q10011\r\r\nカテゴリ　　　：ファイナルファンタジーXI'
        '\r\r\nサブカテゴリ　：GMポリシー\r\r\n満足度評価　　：88&style;\r\r\n\r\r\nanswer"</record>')
got = kbserve.parse_page(page)
check("a captured Japanese page gives its header fields",
      (got["q"], got["no"], got["cat"], got["sub"], got["rating"]),
      ("Why?", "Q10011", "ファイナルファンタジーXI", "GMポリシー", "88"))

print("the search")
check("a category lists its articles", [a["id"] for a in kbserve.search("en-US", 1)], [1, 2])
check("every keyword must match, either case, answers included",
      [a["id"] for a in kbserve.search("en-US", 0, "MAIL account")], [3])
check("an entity in the text matches its character",
      [a["id"] for a in kbserve.search("en-US", 0, "can't")], [3])
check("the GM policy list is the FFXI subcategory", [a["id"] for a in kbserve.search("en-US", 1, "", "GM Policies")], [2])

print("the doors")
import responders  # noqa: E402


def band(method, path, body=b"", lang="en-US"):
    req = (f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\nAccept-Language: {lang}\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    a, b = socket.socketpair()

    def serve():
        try:
            responders._serve_http_on_lobby(b, b"", "192.0.2.5:5000", 51300)
        finally:
            b.close()
    t = threading.Thread(target=serve)
    t.start()
    a.sendall(req)
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
    a.close()
    t.join(15)
    return body_of(resp)


import stub  # noqa: E402
import http.client  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
stub.WWW_DIR = WWW
stub.LOG_DIR = LOGS
srv = ThreadingHTTPServer(("127.0.0.1", 0), stub.StubHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def door(method, path, body=b"", lang="en-US"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
    c.request(method, path, body=body or None, headers={"Host": HOST, "Accept-Language": lang})
    r = c.getresponse()
    data = r.read()
    c.close()
    return f"HTTP/1.1 {r.status}".encode(), data.decode("utf-8-sig", "replace")


for name, get in (("band", band), (":80", door)):
    st, menu = get("GET", "/polapps/s/s.kb.pml.Menu?ZUID=3&c0=5")
    check(f"{name}: Menu -> 200, the search form, Back to the GM Call rules",
          (st.split(b" ")[1] == b"200", "formaction" in menu, 'href="/pml2/cs/gm/gmpm01.pml"' in menu),
          (True, True, True))
    scheme = "http" if name == "band" else "https"
    check(f"{name}: the form posts back through the same door ({scheme})",
          f'action="{scheme}://{HOST}/polapps/s/s.kb.pml.List"' in menu)
    st, lst = get("POST", "/polapps/s/s.kb.pml.List", b"ZUID=3&c0=6&c1=1&k=party")
    check(f"{name}: a posted search lists its one hit, and keeps c0 for Back",
          (lst.count("s.kb.pml.Qa2?"), 'href="/pml2/cs/gm/gmpm02.pml"' in lst), (1, True))
    st, qa = get("GET", "/polapps/s/s.kb.pml.Qa2?ZUID=3&c0=5&c1=1&id=1")
    check(f"{name}: Qa2 shows the question and SE's answer record",
          ("How do I form a party?" in qa, "Use the Party menu." in qa), (True, True))
    st, gp = get("GET", "/polapps/s/s.kb.pml.GmPolicy?polg_loc=ja&ZUID=3&c1=1&c2=15")
    check(f"{name}: GmPolicy (Japanese by polg_loc) is FFXI's list array",
          gp.strip().splitlines(), ['<array name="$qagmpolicyListData">',
                                    '<array>"11","突然走ることができなくなりました。","0"</array>',
                                    '<array>"#"</array>', "</array>"])

st, bare = door("GET", "/polapps/s/s.kb.pml.List")
check("a bare List (the Viewer's own Q&A Search) is the search page", "formaction" in bare)
st, err = door("POST", "/polapps/s/s.kb.pml.List", b"k=zzzz")
check("no match -> the error page", "No Q&amp;A matched" in err or "No Q&A matched" in err)
st, p2 = door("GET", "/polapps/s/s.kb.pml.List?c1=2&p=2")
check("paging: page 2 of 60 cards shows the last 10 and a Previous button",
      (p2.count("s.kb.pml.Qa2?"), "bt_prev" in p2, "bt_next" in p2), (10, True, False))
st, ja = door("GET", "/polapps/s/s.kb.pml.Menu?ZUID=3&c0=6", lang="ja")
check("Accept-Language ja gives the Japanese page", "キーワード" in ja)
st, other = door("GET", "/polapps/s/other.pml")
check("another /polapps/ path is not taken", "formaction" not in other)

print("the output")
evil = 'x" onclick="y\' <sheet>'
_s, _c, raw = kbserve.handle("GET", "/polapps/s/s.kb.pml.Menu?k=" + __import__("urllib.parse").parse.quote(evil))
text = raw.decode("utf-8-sig")
check("UTF-8 with a BOM and CRLF only", (raw[:3], "\n" not in text.replace("\r\n", "")), (b"\xef\xbb\xbf", True))
check("a hostile keyword is escaped, never markup",
      ('onclick="y' in text, "<sheet>" in text.split("value=")[-1].split(">")[0],
       "x&quot; onclick=&quot;y&#39; &#60;sheet&#62;" in text), (False, False, True))

srv.shutdown()
for fh in list(getattr(stub, "_LOG_HANDLES", {}).values()):
    try:
        fh.close()
    except (OSError, AttributeError):
        pass
print()
print("kbserve_test: OK" if ok else "kbserve_test: FAILED")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if ok else 1)
