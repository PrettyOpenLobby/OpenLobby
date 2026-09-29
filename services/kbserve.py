"""The PlayOnline Q&A knowledge base: SE's /polapps/s/s.kb.pml.* servlet.

Three doors reach it:

  * Service & Support > GM Call > Q&A (cs/gm/gmpm01.pml, gmpm02.pml) links
    `https://wh000.pol.com/polapps/s/s.kb.pml.Menu?ZUID=3&c0=5` (Japanese:
    plain http, c0=5 / c0=6);
  * the Viewer's own Q&A Search opens POL_QA_SEARCH_URL from env.dat,
    `https://wh000.pol.com/polapps/s/s.kb.pml.List`;
  * FFXI's support page includes `s.kb.pml.GmPolicy?...` for its GM policy
    list and links each row to `s.kb.pml.Qa2?...&id=<n>`.

https arrives decrypted from sslterm at stub.py; plain http arrives on a band
port at lobbyserver. Both call handle().

The servlet itself is gone. What survives is every answer page it served,
fetched on 2026-09-22 (tools/polkb.py): 996 English and 1146 Japanese
articles. The search page, the result list and the servlet's own includes
(Kbin01, Form) were never captured, so those pages are authored here on the
Service & Support frame; an answer page shows SE's own question and answer
text. export() turns the captured pages into the JSON this reads:

    python kbserve.py export <archive>/en-US/raw www/_kb/en-US.json
    python kbserve.py export <archive>/ja/raw    www/_kb/ja-JP.json
"""
import html
import json
import os
import re
import sys
import threading
import urllib.parse

KB_DIR = os.environ.get("POL_KB_DIR") or os.path.join(
    os.environ.get("POL_WWW_DIR", "/www"), "_kb")
PREFIX = "/polapps/s/s.kb.pml."
LOCALES = ("en-US", "ja-JP")
PAGE_ROWS = 50
MAX_KEYWORD = 60

#: c1, SE's category ids (polkb.py), by the category name each locale prints.
CATEGORIES = {
    "en-US": [(1000, "PlayOnline Viewer"), (2000, "Membership & Dues"),
              (2001, "Technical Issues"), (1, "FINAL FANTASY XI"), (2, "Tetra Master")],
    "ja-JP": [(1000, "プレイオンラインビューアー"), (2000, "ご契約内容"),
              (2001, "技術的なこと"), (5, "プレイオンライン・プラス"),
              (1, "ファイナルファンタジーXI"), (2, "テトラマスター"), (3, "雀鳳楼")],
}
#: FFXI's GM policy subcategory (c1=1, c2=15 in stpm01.pml's include).
GM_POLICY = {"en-US": "GM Policies", "ja-JP": "GMポリシー"}

#: c0: where the page was opened from, so Back returns there. SE's own map
#: (cs/kb/kbin03.pml) plus the later ids the GM pages and FFXI support use.
BACK = {
    0: "/pml2/cs/index.pml",
    1: "/pml/game/ff11/index.pml",
    2: "/pml/game/tetra/index.pml",
    3: "/pml/game/jan/index.pml",
    4: "/pml2/cs/gm/gmpm01.pml",
    5: "/pml2/cs/gm/gmpm01.pml",
    6: "/pml2/cs/gm/gmpm02.pml",
    12: "/pml/game/ff11/support/stpm01.pml",
}

TEXT = {
    "en-US": {
        "title": "Q&A", "search": "Search", "keyword": "Keyword",
        "category": "Category", "all": "All categories",
        "intro": "Search the PlayOnline Q&A. Pick a category, enter a keyword, or both.",
        "results": "Results", "found": "%d found", "page": "Page %d of %d",
        "prev": "Previous", "next": "Next", "again": "New search",
        "to_list": "Results", "none": "No Q&A matched your search.&br;&br;"
                                      "Try a shorter keyword or another category.",
        "back": "^03Return to the previous page.",
        "a_search": "^03Search the Q&A.", "a_kw": "^03Enter a keyword.",
        "a_cat": "^03Choose a category.", "a_row": "^03Read this answer.",
        "a_again": "^03Start a new search.", "a_list": "^03Return to the search results.",
        "a_prev": "^03Show the previous page.", "a_next": "^03Show the next page.",
    },
    "ja-JP": {
        "title": "Q&A", "search": "検　索", "keyword": "キーワード",
        "category": "カテゴリ", "all": "すべてのカテゴリ",
        "intro": "プレイオンラインのQ&Aを検索します。カテゴリを選ぶか、キーワードを入力してください。",
        "results": "検索結果", "found": "%d件", "page": "%d/%dページ",
        "prev": "前へ", "next": "次へ", "again": "再検索",
        "to_list": "検索結果へ", "none": "該当するQ&Aはありませんでした。&br;&br;"
                                       "キーワードやカテゴリを変えてお試しください。",
        "back": "^03前のページへ戻ります。",
        "a_search": "^03Q&Aを検索します。", "a_kw": "^03キーワードを入力してください。",
        "a_cat": "^03カテゴリを選んでください。", "a_row": "^03この回答を表示します。",
        "a_again": "^03新しく検索します。", "a_list": "^03検索結果へ戻ります。",
        "a_prev": "^03前のページを表示します。", "a_next": "^03次のページを表示します。",
    },
}

_LOCK = threading.Lock()
_CACHE = {}


# --------------------------------------------------------------------------- #
# the archive
# --------------------------------------------------------------------------- #
_Q = re.compile(r'<define name="\$question_data2" value="(.*?)">', re.S)
_REC = re.compile(r'<data name="t2" sub="a2">\s*<record>"(.*?)"</record>', re.S)
_HEAD_EN = ("Article No.", "Category", "Subcategory", "Created", "Updated", "Rating")
_HEAD_JA = ("記事番号", "カテゴリ", "サブカテゴリ", "作成日", "更新日", "満足度評価")
_KEYS = ("no", "cat", "sub", "created", "updated", "rating")


def parse_page(text):
    """One captured answer page -> the article, or None."""
    text = text.replace("\r\r\n", "\n").replace("\r\n", "\n")
    q, rec = _Q.search(text), _REC.search(text)
    if not (q and rec):
        return None
    record = rec.group(1)
    head, _, answer = record.partition("&style;")
    art = {"q": q.group(1).strip(), "rec": record}
    for line in head.replace("&pre=1;", "").replace("&style=qa14_a;", "").split("\n"):
        m = re.match(r"[\s　]*(.+?)[\s　]*[:：][\s　]*(.*)$", line)
        if not m:
            continue
        label, value = m.groups()
        for keys in (_HEAD_EN, _HEAD_JA):
            if label in keys:
                art[_KEYS[keys.index(label)]] = value.strip()
    art["answer"] = answer.strip("\n")
    return art


def export(raw_dir, out):
    arts = []
    for name in os.listdir(raw_dir):
        if not name.endswith(".pml"):
            continue
        with open(os.path.join(raw_dir, name), encoding="utf-8") as f:
            art = parse_page(f.read())
        if art:
            art["id"] = int(name[:-4])
            del art["answer"]
            arts.append(art)
    arts.sort(key=lambda a: a["id"])
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(arts, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return arts


def _plain(s):
    s = re.sub(r"&(pre|style|br|size|li)[^;]*;", " ", s)
    return html.unescape(s).lower()


def articles(loc):
    """The locale's articles, loaded once per file change. [] when absent."""
    path = os.path.join(KB_DIR, loc + ".json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    with _LOCK:
        hit = _CACHE.get(loc)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                arts = json.load(f)
        except (OSError, ValueError):
            return []
        for a in arts:
            a["_text"] = _plain(a["q"] + " " + a["rec"])
        _CACHE[loc] = (mtime, arts)
        return arts


# --------------------------------------------------------------------------- #
# the request
# --------------------------------------------------------------------------- #
def _locale(params, headers):
    want = (params.get("polg_loc") or headers.get("accept-language") or "").lower()
    return "ja-JP" if want.startswith("ja") else "en-US"


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _params(path, body, headers):
    """Query string and form body, both decoded. The Viewer posts UTF-8 with
    encode="UTF8"; a stray Shift-JIS byte must not cost the request."""
    raw = path.split("?", 1)[1] if "?" in path else ""
    if body:
        raw += "&" + body.decode("latin-1")
    out = {}
    for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True, encoding="latin-1"):
        b = v.encode("latin-1")
        try:
            v = b.decode("utf-8")
        except UnicodeDecodeError:
            v = b.decode("cp932", "replace")
        out.setdefault(k, v)
    return out


def search(loc, c1=0, keyword="", sub=None):
    arts = articles(loc)
    cats = dict(CATEGORIES[loc])
    words = [w for w in re.split(r"[\s　]+", keyword.lower()) if w]
    out = []
    for a in arts:
        if c1 and a.get("cat") != cats.get(c1):
            continue
        if sub and a.get("sub") != sub:
            continue
        if all(w in a["_text"] for w in words):
            out.append(a)
    return out


def handle(method, path, body=b"", headers=None, scheme="http", host="wh000.pol.com"):
    """(status, content_type, bytes) for a /polapps/s/s.kb.pml.* request, or
    None for any other path."""
    bare = path.split("?", 1)[0]
    if not bare.startswith(PREFIX):
        return None
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    page = bare[len(PREFIX):]
    p = _params(path, body, headers)
    loc = _locale(p, headers)
    ctx = {"loc": loc, "t": TEXT[loc], "c0": _int(p.get("c0")), "bk": _int(p.get("bk")),
           "base": f"{scheme}://{host}{PREFIX}"}
    if page == "GmPolicy":
        rows = search(loc, 1, "", GM_POLICY[loc])
        return _ok(render_gm_policy(rows))
    c1 = _int(p.get("c1"))
    keyword = (p.get("k") or p.get("search_key") or "").strip()[:MAX_KEYWORD]
    if page in ("Qa", "Qa2"):
        art = next((a for a in articles(loc) if a["id"] == _int(p.get("id"), -1)), None)
        if art is None:
            return _ok(render_err(ctx))
        return _ok(render_qa(ctx, art, c1, keyword, _int(p.get("p"), 1),
                             from_gm=p.get("fromGm") == "1"))
    if page == "List" and (c1 or keyword):
        rows = search(loc, c1, keyword)
        if not rows:
            return _ok(render_err(ctx, c1, keyword))
        return _ok(render_list(ctx, rows, c1, keyword, _int(p.get("p"), 1)))
    # Menu, a List with nothing to search for (the Viewer's own Q&A Search
    # opens it bare), and anything else under the servlet: the search page.
    return _ok(render_menu(ctx, c1, keyword))


def _ok(text):
    return 200, "text/x-playonline-pml", b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# the pages
# --------------------------------------------------------------------------- #
def _f(s):
    """A value for inside a quoted PML attribute or string."""
    s = str(s).replace("&", "&amp;").replace('"', "&quot;").replace("'", "&#39;")
    return s.replace("<", "&#60;").replace(">", "&#62;")


def _url(ctx, page, **q):
    q = {k: v for k, v in q.items() if v not in (None, "", 0) or k == "c0"}
    return _f(ctx["base"] + page + "?" + urllib.parse.urlencode({"ZUID": 3, **q}))


def _head(ctx, title, extra=""):
    face = "2" if ctx["loc"] == "ja-JP" else "6"
    return f"""<pml>
<head>
<title>{_f(title)}</title>
<meta http-equiv="Content-Type" content="text/x-playonline-pml; charset=UTF-8">
<meta http-equiv="Pragma" content="no-cache">
<config addbookmark="0">
<include src="/pml/pml_s/path/cs/cont1.pml">
<include src="$C_PATH1+'kb/kbin01.pml'">

<define name="$scolor" value="#301620dd">
<define name="$scolor_hd" value="#ffffffee">
<define name="$tx_skin" value="0">
<define name="$tx_skincolor" value="#dfd6d0ee">
<style name="qa15" face="{face}" size="15" color="$scolor" vspacing="4" spacing="0" proportional="1">
<style name="qa14_a" face="{face}" size="14" color="#555555ff" vspacing="3" spacing="0" proportional="1">
<style name="qa16" face="{face}" size="16" color="$scolor" vspacing="3" spacing="0" proportional="1">
<style name="qa15_w" face="{face}" size="16" color="$scolor_hd" vspacing="0" spacing="0" proportional="1">
<style name="sl16" face="{face}" size="16" color="$sl_stylecolor" vspacing="0" spacing="0" proportional="1">
<style name="tx15" face="{face}" size="15" color="$scolor" vspacing="0" spacing="0" proportional="1">
<style name="bt16" face="{face}" size="16" color="#ffffffff,#000000ff" onmousecolor="#444444ff,#ffffffff"
 vspacing="0" spacing="0" proportional="1">
<define name="$icn" value="ic_bk">
{extra}
</head>
<body altbgcolor="$abc" background="$bgr">

<sheet pos="0,60" size="640,400" border="0" alpha="1" appeartime="200" skin="11">
<sheet pos="-50,0" size="740,370" type="0" alpha="1" appeartime="1000" skincolor="#fffefcff">
<img src="$F_PATH_K1+'img_s/tl01s.png'" pos="-288,10">
<img src="$F_PATH_K1+'img_s/tp06s.png'" pos="100,15">
<text style="hw20" pos="135,18">{_f(ctx["t"]["title"])}</text>
</sheet>
</sheet>
"""


def _back(ctx):
    dest = BACK.get(ctx["c0"], BACK[0])
    return f"""
<sheet name="sh_icn_rt" pos="44,350" size="60,70" border="0" delay="400" alpha="1" type="4" appeartime="200">
<img name="ic_bk" src="$F_PATH_K1+'img_s/ic01s.ang'"
 href="{dest}" onkeycancel="{dest}" alt="{_f(ctx["t"]["back"])}">
</sheet>

</body>
</pml>
"""


def _button(name, x, y, label, href, alt):
    return f"""<sheet pos="{x},{y}" size="114,56" border="0" alpha="1" appeartime="150" wait="2" spread="1,3">
<img name="{name}" src="$C_PATH1+'kb/kb_i/kbbt03i.ang'" size="114,56" style="bt16" value="{_f(label)}"
 href="{href}" onkeycancel="sd:focus@ic_bk" alt="{_f(alt)}">
</sheet>
"""


def render_menu(ctx, c1=0, keyword=""):
    t, loc = ctx["t"], ctx["loc"]
    action = ctx["base"] + "List"
    cats = [(0, t["all"])] + CATEGORIES[loc]
    rows = len(cats)
    opts = "\n".join(f'<option value="{v}"{" selected" if v == c1 else ""}>{_f(n)}</option>'
                     for v, n in cats)
    return _head(ctx, t["title"], f'<formaction name="fa" action="{_f(action)}" method="post" encode="UTF8">') + f"""
<sheet pos="0,0" size="640,480" border="0" alpha="1" appeartime="200">
<text style="qa16" pos="90,120" size="480,50">{_f(t["intro"])}</text>
</sheet>

<form name="fm" target="fa">
<input name="ZUID" type="hidden" value="3">
<input name="c0" type="hidden" value="{ctx["c0"]}">
<input name="bk" type="hidden" value="{ctx["bk"]}">
<input name="polg_loc" type="hidden" value="{loc}">
<sheet pos="0,0" size="640,480" border="0" alpha="1" appeartime="200">
<text style="qa16" pos="90,188">{_f(t["category"])}</text>
<text style="qa16" pos="90,238">{_f(t["keyword"])}</text>
<input name="k" type="text" pos="220,232" size="340,28" style="tx15" maxlength="{MAX_KEYWORD}"
 skin="1" skincolor="#ffffff88" value="{_f(keyword)}"
 onkeycancel="sd:focus@ic_bk" alt="{_f(t["a_kw"])}">
</sheet>
<sheet pos="440,290" size="114,56" border="0" alpha="1" appeartime="200" wait="2" spread="1,3">
<input name="bt_search" type="image" src="$C_PATH1+'kb/kb_i/kbbt03i.ang'" size="114,56" style="bt16"
 value="&image=search; {_f(t["search"])}" onkeycancel="sd:focus@ic_bk" alt="{_f(t["a_search"])}">
</sheet>
<select popup name="c1" pos="220,182" size="340,28" rows="{rows}" style="sl16" offset="5,0" titleoffset="5,0"
 skin="$sl_skin" skincolor="$sl_skincolor" onmousecolor="$sl_onmousecolor" selectedcolor="$sl_selectedcolor"
 onkeycancel="sd:focus@ic_bk" alt="{_f(t["a_cat"])}">
{opts}
</select>
</form>
""" + _back(ctx)


def render_list(ctx, rows, c1, keyword, page):
    t = ctx["t"]
    pages = max(1, (len(rows) + PAGE_ROWS - 1) // PAGE_ROWS)
    page = min(max(1, page), pages)
    shown = rows[(page - 1) * PAGE_ROWS: page * PAGE_ROWS]
    body = []
    for i, a in enumerate(shown):
        href = _url(ctx, "Qa2", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword, p=page,
                    id=a["id"], polg_loc=ctx["loc"])
        focus = ' name="imdf"' if i == 0 else ""
        body.append(f"""<tr height="38"><td width="500">
<img name="ef{i}" src="$toa_src" size="$toa_w,$toa_h" pos="0,-4">
<img src="$qpt_src" pos="$qpt_x,$qpt_y+8">
<text style="qa16" pos="30,6" size="460,28">{a["q"]}</text>
<img{focus} src="$F_PATH2+'im01s.png'" size="500,34" href="{href}"
 onmouseover="sd:sequence=3@ef{i}" onmouseout="sd:sequence=0@ef{i}"
 onkeycancel="sd:focus@ic_bk" alt="{_f(t["a_row"])}">
</td></tr>""")
    nav = ""
    if page > 1:
        nav += _button("bt_prev", 150, 0, t["prev"],
                       _url(ctx, "List", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword,
                            p=page - 1, polg_loc=ctx["loc"]), t["a_prev"])
    if page < pages:
        nav += _button("bt_next", 270, 0, t["next"],
                       _url(ctx, "List", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword,
                            p=page + 1, polg_loc=ctx["loc"]), t["a_next"])
    again = _url(ctx, "Menu", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword, polg_loc=ctx["loc"])
    status = _f(t["found"] % len(rows)) + ("&br;" + _f(t["page"] % (page, pages)) if pages > 1 else "")
    return _head(ctx, t["results"]) + f"""
<sheet name="sh_result" pos="330,55" size="300,48" border="0" alpha="1" appeartime="200">
<img src="$F_PATH_K1+'img_s/tl02s.png'">
<text style="qa15_w" pos="20,4" size="270,40">{_f(t["results"])}: {status}</text>
</sheet>

<sheet name="sh_list" pos="60,$y_sh+10" size="530,$sca_height2" border="0" alpha="1" appeartime="200" wait="2">
<scrollarea name="sc_list" size="530,$sca_height2" areasize="510,{10 + 38 * len(shown)}" hbar="never"
 skin="$sca_skin" skincolor="$sca_skincolor" selectedskincolor="$sca_selectedskincolor" bgcolor="$sca_bgcolor"
 onkeycancel="sd:focus@ic_bk">
<table pos="5,5" size="500,{38 * len(shown)}" cellpadding="0" cellspacing="0">
{chr(10).join(body)}
</table>
</scrollarea>
</sheet>

<sheet pos="110,$y_sh+265" size="530,56" border="0" alpha="1" appeartime="100" wait="2">
{_button("bt_again", 0, 0, t["again"], again, t["a_again"])}{nav}</sheet>
""" + _back(ctx)


def render_qa(ctx, art, c1, keyword, page, from_gm=False):
    t = ctx["t"]
    buttons = ""
    if not from_gm:
        if c1 or keyword:
            buttons += _button("bt_list", 0, 0, t["to_list"],
                               _url(ctx, "List", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword,
                                    p=page, polg_loc=ctx["loc"]), t["a_list"])
        buttons += _button("bt_again", 120, 0, t["again"],
                           _url(ctx, "Menu", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword,
                                polg_loc=ctx["loc"]), t["a_again"])
    extra = f"""<data name="t2" sub="a2">
<record>"{art["rec"]}"</record>
</data>"""
    return _head(ctx, t["title"], extra) + f"""
<sheet name="shAnswer" pos="0,$y_sh+15" size="640,50+$sca_height1-30" border="0" alpha="1" appeartime="200" skin="11" wait="2">
<img src="$qic_src" pos="$qic_x,$qic_y">
<img src="$aic_src" pos="$aic_x,$aic_y">
<text pos="85,8" size="510,44" style="qa16">{art["q"]}</text>
<img src="$C_PATH1+'kb/kb_i/kbim01i.png'" pos="80,55" size="8,$sca_height1-30">
<img src="$C_PATH1+'kb/kb_i/kbim02i.png'" pos="80,55" size="510,7">
<sheet pos="80,55" size="510,$sca_height1-30" border="0" alpha="1">
<textbox name="tb_a" pos="0,0" size="510,$sca_height1-30" margin="5,5,5,0" style="qa15" ref="t2" sub="a2"
 skin="$sca_skin" skincolor="$sca_skincolor" selectedskincolor="$sca_selectedskincolor" bgcolor="$sca_bgcolor"
 onkeycancel="sd:focus@ic_bk">
</sheet>
</sheet>

<sheet pos="110,$y_sh+270" size="370,56" border="0" alpha="1" appeartime="100" wait="2">
{buttons}</sheet>
""" + _back(ctx)


def render_err(ctx, c1=0, keyword=""):
    t = ctx["t"]
    again = _url(ctx, "Menu", c0=ctx["c0"], bk=ctx["bk"], c1=c1, k=keyword, polg_loc=ctx["loc"])
    return _head(ctx, t["title"]) + f"""
<sheet pos="0,0" size="640,480" border="0" alpha="1" appeartime="200">
<text style="qa16" pos="90,140" size="480,90">{t["none"]}</text>
</sheet>
<sheet pos="110,$y_sh+265" size="530,56" border="0" alpha="1" appeartime="100" wait="2">
{_button("bt_again", 0, 0, t["again"], again, t["a_again"])}</sheet>
""" + _back(ctx)


def render_gm_policy(rows):
    """The include FFXI's support page draws its GM policy list from: rows of
    [id, question, icon], ended by ["#"]. Icon 0 draws none."""
    lines = ['<array name="$qagmpolicyListData">']
    lines += [f'<array>"{a["id"]}","{a["q"]}","0"</array>' for a in rows]
    lines += ['<array>"#"</array>', "</array>"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "export":
        got = export(sys.argv[2], sys.argv[3])
        print(f"{len(got)} articles -> {sys.argv[3]}")
    else:
        print(__doc__)
