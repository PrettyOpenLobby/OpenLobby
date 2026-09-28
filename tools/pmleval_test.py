#!/usr/bin/env python3
"""Prove the admin PML preview actually renders a portal page.

    python pmleval_test.py

WHY THIS EXISTS. Reported 2026-08-18: the admin panel's PML editor drew a blank
stage. It was not the Render button, and not the renderer -- the page reaching
the renderer was empty. `wh000.pol.com/pml/help/login/index.pml` is a real,
typical page and its ENTIRE body is one line:

    <include src="$F_PATH1+'in02.pml'">

Four separate rules each dropped that line on its own:

  * the include src was passed to the resolver as the literal text
    `$F_PATH1+'in02.pml'`, never evaluated;
  * the resolver refused any src containing `$` or `+` -- which is nearly every
    include SE wrote -- and any containing `..`, which is how a page reaches the
    `cont1.pml` that DEFINES `$F_PATH1`;
  * the host was taken from path segment 0, so every page under
    `_lang/<locale>/<host>/` and `_eras/<era>/<host>/` (3,291 of 3,499 indexed
    files) looked for its includes under `www/_lang/`;
  * `<!SHEET NAME>`-style short comments survived the strip and then failed to
    match a tag, so the parser emitted them as text and lost the markup after.

Any one of them returns the preview to blank, so each gets an assertion here
against a fixture tree shaped like the real mirror -- `_lang/<locale>/<host>/`
included, because that is the shape that made the "first segment is the host"
rule look correct for years while serving nothing.
"""
import io
import os
import shutil
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

WWW = tempfile.mkdtemp(prefix="pmleval-")
os.environ["POL_ADMIN_WWW"] = WWW

import pmleval                                   # noqa: E402
import admin                                     # noqa: E402

ok = True


def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(f"  {'ok  ' if good else 'FAIL'} {label}")
    if not good:
        print(f"       got  {got!r}\n       want {want!r}")


def write(rel, text):
    path = os.path.join(WWW, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(text)


# --------------------------------------------------------------------------- #
# A fixture mirror with both shapes: a plain host, and the _lang wrapper.
# --------------------------------------------------------------------------- #
HOST = "wh000.pol.com"
write(f"{HOST}/pml/help/cont1.pml",
      '<define name="$F_PATH1" value="file:/help/">\n'
      '<define name="$C_PATH1" value="/pml/help/">\n')
write(f"{HOST}/pml/help/in02.pml",
      '<sheet pos="0,60" size="640,380" skincolor="#fffefcff">\n'
      '<img pos="8,4" size="120,40" src="$F_PATH1+\'img_s/tl01s.png\'">\n'
      '</sheet>\n')
write(f"{HOST}/pml/help/login/index.pml",
      '<pml><head>\n'
      '<include src="../cont1.pml">\n'
      '</head>\n'
      '<body background="$F_PATH1+\'img_s/bg01s.png\'">\n'
      '<include src="$F_PATH1+\'in02.pml\'">\n'
      '</body></pml>\n')
# the host-absolute form, used by the _lang tree. SE states BOTH forms here, and
# `file:/img_s/general/` names a directory that exists only under `pml/` -- so
# `file:` is the pml tree even for the `pml2/cs/` pages this file serves.
write(f"{HOST}/pml/pml_s/path/cs/cont1.pml",
      '<define name="$C_PATH1" value="/pml2/cs/">\n'
      '<define name="$F_PATH2" value="file:/img_s/general/">\n')
write(f"{HOST}/pml/img_s/general/shared.pml",
      '<text pos="0,0" size="100,20">shared art tree</text>\n')
write(f"{HOST}/pml2/help/manual/pm10.pml",
      '<pml><body>\n'
      '<include src="/pml/pml_s/path/cs/cont1.pml">\n'
      '<include src="$F_PATH2+\'shared.pml\'">\n'
      '</body></pml>\n')
write("_lang/en-US/" + HOST + "/pml2/cs/frag.pml",
      '<text pos="10,10" size="200,20" style="hdr">from the _lang tree</text>\n')
write("_lang/en-US/" + HOST + "/pml2/cs/index.pml",
      '<pml><body>\n'
      '<include src="/pml/pml_s/path/cs/cont1.pml">\n'
      '<include src="$C_PATH1+\'frag.pml\'">\n'
      '</body></pml>\n')


def expand(rel):
    """What /api/pml-expand does, with the same roots the handler builds."""
    host_dirs, tree_dirs, start_dir = admin._pml_roots(rel)
    text = io.open(os.path.join(WWW, *rel.split("/")), encoding="utf-8").read()

    def resolve(src, base):
        got = admin._pml_resolve(src, base or start_dir, host_dirs, tree_dirs)
        if not got:
            return None
        return io.open(got, encoding="utf-8").read(), os.path.dirname(got)

    return pmleval.expand(text, resolve_include=resolve, base=start_dir)


# --------------------------------------------------------------------------- #
print("roots: the host is the first segment with a dot, not segment 0")
root = os.path.abspath(WWW)
hosts, trees, start_dir = admin._pml_roots(f"{HOST}/pml/help/login/index.pml")
rel = lambda p: p and os.path.relpath(p, root).replace(os.sep, "/")   # noqa: E731
check("plain host", [rel(h) for h in hosts], [HOST])
check("`file:` roots: the pml tree, then the host",
      [rel(t) for t in trees], [f"{HOST}/pml", HOST])
check("the page's own directory", rel(start_dir), f"{HOST}/pml/help/login")

hosts, trees, _s = admin._pml_roots(f"_lang/en-US/{HOST}/pml2/cs/index.pml")
# The overlay carries only pml2/; everything else still lives in the base
# tree, so both must be searched -- overlay first.
check("_lang wrapper: host is segment 2, then the base host",
      [rel(h) for h in hosts], [f"_lang/en-US/{HOST}", HOST])
check("_lang wrapper: the overlay's own tree, then the base host's pml",
      [rel(t) for t in trees],
      [f"_lang/en-US/{HOST}/pml2", f"_lang/en-US/{HOST}",
       f"{HOST}/pml", f"{HOST}/pml2", HOST])

# A pasted page names no file. That must degrade to the www root, not raise.
check("a page with no file behind it falls back to the www root",
      admin._pml_roots(""), ([root], [root], root))

print()
print("resolve: the three src forms, and containment")
h, t, s = admin._pml_roots(f"{HOST}/pml/help/login/index.pml")
check("`../cont1.pml` relative to the page",
      rel(admin._pml_resolve("../cont1.pml", s, h, t)), f"{HOST}/pml/help/cont1.pml")
check("`file:/help/in02.pml` absolute in the PML tree",
      rel(admin._pml_resolve("file:/help/in02.pml", s, h, t)),
      f"{HOST}/pml/help/in02.pml")
check("`/pml/help/in02.pml` absolute at the host",
      rel(admin._pml_resolve("/pml/help/in02.pml", s, h, t)),
      f"{HOST}/pml/help/in02.pml")
check("`..` may not climb out of the host",
      admin._pml_resolve("../../../../etc/passwd", s, h, t), None)
check("an unevaluated expression resolves to nothing rather than a junk path",
      admin._pml_resolve("$UNKNOWN+'x.pml'", s, h, t), None)

print()
print("attributes: SE writes bare expressions, not just &var=")
V = pmleval.make_vars({"F_PATH1": "file:/help/", "BN_X": 10, "BN_Y": 4, "BN_Ysp": 42,
                       "i": 3})
A = lambda tag, key, value: pmleval.eval_attr(tag, key, value, V)   # noqa: E731
check("a src expression", A("img", "src", "$F_PATH1+'in02.pml'"),
      "file:/help/in02.pml")
check("pos is a pair of component expressions",
      A("img", "pos", "$BN_X+5,$BN_Y+($BN_Ysp*$i)"), "15,130")
check("integers stay integers", A("img", "pos", "$BN_Ysp/2,0"), "21,0")
check("a comma inside quotes does not split an AUTO attribute",
      A("body", "background", "$F_PATH1+'a,b.png'"), "file:/help/a,b.png")
# spec 5: img src is split on EVERY comma before AUTO, quotes or not
check("but an img src splits on every comma, as the Viewer's does",
      A("img", "src", "$F_PATH1+'a,b.png'"),
      "file:/help/a,(Inconsistent parentheses error)")
check("a value with no $ is untouched", A("text", "bgcolor", "#302610bb,#30261011"),
      "#302610bb,#30261011")
# spec 5: href is BRACE -- only {$..} is filled in, the rest is an action string
check("an action string is left ALONE, not half-substituted",
      A("img", "href", "sd:show=1@$BN_X"), "sd:show=1@$BN_X")
# spec 4: an undefined variable is "(Variable error)" in a string, not a failure
check("an unknown variable reads as (Variable error)",
      A("img", "src", "$NOPE+'x.png'"), "(Variable error)x.png")

print()
print("comments: SE's short form may start with a letter")
check("`<!SHEET NAME>` is stripped, not emitted as text",
      pmleval._strip_comments('<!SHEET NAME><sheet pos="1,2">'), '<sheet pos="1,2">')
check("and so is `<!style>` -- pml/info/style.pml opens with exactly this",
      pmleval._strip_comments('<!style>\n<style name="C13" size="13">').strip(),
      '<style name="C13" size="13">')

print()
print("nodefvalue is the value when there is no caller to supply one")
got = pmleval.expand('<define name="$cnt" nodefvalue="2">'
                     '<array name="$BG">"a.png","b.png","c.png"</array>'
                     '<body background="$BG[$cnt]">')
check("it indexes the array", 'background="c.png"' in got, True)

print()
print("the page that was reported blank")
got = expand(f"{HOST}/pml/help/login/index.pml")
check("the relative include ran (it defines $F_PATH1)", "$F_PATH1" in got, False)
check("the body's include was inlined", "<sheet" in got, True)
check("its own nested expression resolved",
      'src="file:/help/img_s/tl01s.png"' in got, True)
check("the body background resolved",
      'background="file:/help/img_s/bg01s.png"' in got, True)
check("no <include> survives into the renderer", "<include" in got, False)

print()
print("the _lang overlay: segment-0 rooting could never reach it, and it")
print("reaches BACK into the base tree for what the overlay does not carry")
got = expand(f"_lang/en-US/{HOST}/pml2/cs/index.pml")
check("the host-absolute include came from the BASE tree", "$C_PATH1" in got, False)
check("and the expression include it defines resolved in the OVERLAY",
      "from the _lang tree" in got, True)

print()
print("a pml2 page's `file:` reaches the pml tree, where the shared art lives")
got = expand(f"{HOST}/pml2/help/manual/pm10.pml")
check("`file:/img_s/general/` resolved under pml/, not pml2/",
      "shared art tree" in got, True)

print()
print("the decode ladder: 2,074 of 3,499 pages in the mirror are cp932")
# The plaintext branch used to be utf-8 with errors="replace", so every one of
# them opened as a wall of U+FFFD -- 59% of the mirror, unreadable, with nothing
# to say it had happened.
JP = "FFXI　　：ニュース"


def decoded(rel, blob):
    path = os.path.join(WWW, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)
    return admin._load_pml_text(path)


text, kind = decoded(f"{HOST}/pml/enc/sjis.pml",
                     f"<pml><title>{JP}</title></pml>".encode("cp932"))
check("a cp932 page decodes as cp932", kind, "plaintext/cp932")
check("and its text survives intact", JP in text, True)
check("with no replacement characters", "�" in text, False)

text, kind = decoded(f"{HOST}/pml/enc/utf8.pml",
                     f"<pml><title>{JP}</title></pml>".encode("utf-8"))
check("a utf-8 page is still utf-8 -- cp932 must not win first", kind,
      "plaintext/utf-8")
check("and its text survives too", JP in text, True)

_t, kind = decoded(f"{HOST}/pml/enc/bom.pml",
                   b"\xef\xbb\xbf<pml><title>hi</title></pml>")
check("a UTF-8 BOM is stripped, not treated as unknown content", kind,
      "plaintext/utf-8")

_t, kind = decoded(f"{HOST}/pml/enc/lead.pml",
                   f"\r\n\t<pml><title>{JP}</title></pml>".encode("cp932"))
check("markup after leading whitespace is still markup", kind, "plaintext/cp932")

print()
print("an empty result has to explain itself")
# `in02.pml` is real: its <for> reads $sht, which `in01.pml` defines. Alone it
# evaluates to nothing, and a blank stage with no reason was the report.
report = {}
got = pmleval.expand('<for init="$i=0" cond="$i<$sht" next="$i++">'
                     '<text pos="0,$i*20" size="100,18">row</text></for>'
                     '<include src="/nowhere/gone.pml">',
                     resolve_include=lambda src, base: None, report=report)
check("the loop drew nothing, as the client would", "<text" in got, False)
check("and the variable it needed is named", report["missing"], ["sht"])
check("as is the include that was not found",
      report["unresolved"], ["/nowhere/gone.pml"])

report = {}
pmleval.expand('<define name="$sht" value="2">'
               '<for init="$i=0" cond="$i<$sht" next="$i++">x</for>', report=report)
check("a page that defines its own variables reports none missing",
      report["missing"], [])

# _Vars must not turn a legitimate absence into a miss: .get()/setdefault() are
# how the evaluator probes optionals, and only a real read should be recorded.
report = {}
pmleval.expand('<define name="$a"><define name="$b" nodefvalue="1">', report=report)
check("an unset <define> and a nodefvalue probe are not 'missing'",
      report["missing"], [])
# spec 6: &var=$never; is AUTO-evaluated, so it IS a read: the Viewer prints
# "(Variable error)" there
report = {}
got = pmleval.expand('<text>&var=$never;</text>', report=report)
check("an unknown &var= is a real read, shown as (Variable error)",
      (report["missing"], "(Variable error)" in got), (["never"], True))

# --------------------------------------------------------------------------- #
# SE's language, the parts the Tetra Master top page and the help manual use.
# Each of these drew a wrong or empty page before 2026-09-27.
# --------------------------------------------------------------------------- #
E = lambda src: pmleval.expand(src)    # noqa: E731

# <elsif>/<else> are never closed, so they parse NESTED inside the <if>.
got = E('<for init="$i=0" cond="$i<3" next="$i++">'
        '<if expr="$i==0">A{$i}<elsif expr="$i==1">B{$i}<else>C{$i}</if></for>')
# spec 6: {$i} in plain text is printed literally; only &var=/&pos=/&style= fill it
check("a nested if/elsif/else picks one branch per pass", got.strip(),
      "A{$i}B{$i}C{$i}")
got = E('<for init="$i=0" cond="$i<3" next="$i++">'
        '<if expr="$i==0">A&var=$i;<elsif expr="$i==1">B&var=$i;<else>C&var=$i;</if></for>')
check("and &var= shows which pass it was", got.strip(), "A0B1C2")
# spec 5 (<if>): a top-level <else> after </if> is ignored, so Y always shows
check("a top-level <else> after </if> is ignored (0: Y)",
      E('<if expr="0">X</if>\n  <else>Y</else>').strip(), "Y")

check("a quoted > does not end the tag",
      E('<if expr="3>0"><text>yes</text></if>').count("yes"), 1)

got = E('<define name="$z" value="0"><define name="$n" value="7">'
        '<define name="$p" calc="\'masc\'+$z+\'\'+$n+\'i.png\'">'
        '<define name="$h" calc="7/2"><img src="$p" pos="$h,1">')
check("a string plus a number concatenates", 'src="masc07i.png"' in got, True)
check("division is integer division", 'pos="3,1"' in got, True)

got = E('<text pos="98+6,117" size="15*31,21" value="a-b">t</text>')
check("arithmetic in pos/size is evaluated with no variable in it",
      ('pos="104,117"' in got, 'size="465,21"' in got, 'value="a-b"' in got),
      (True, True, True))

got = E('<array name="$c">"Title A" "B"</array><define name="$id" value="1">'
        '<text>&var=$c[0];|&var=$c[$id];|&var=$nope[3];</text>')
# spec 5 (<array>): no comma means ONE item "Title AB"; [1] past the end is "";
# spec 4: an unknown array is (Variable error)
check("&var= takes an array lookup",
      got[got.index("<text"):].split(">", 1)[1].split("<")[0],
      "Title AB||(Variable error)")

got = E('<define name="$j" value="4"><img href="eval:$a[{$j}]">')
check("{$x} is filled in", 'href="eval:$a[4]"' in got, True)

got = E('<pml>\n<!-- two\nlines -->\n<text pos="1,1">x</text></pml>')
check("elements carry their source line through comments",
      'pml-line="4"' in got, True)

check("a condition sees the <define> right above it (evaluated when reached)",
      E('<define name="$a" value="0"><if expr="$a==0">ZERO<else>OTHER</if>'
        '<define name="$a" value="1"><if expr="$a==1">ONE<if expr="$a==1">'
        'NESTED</if></if>').strip(), "ZEROONENESTED")

check("<hr> is self-closing",
      E('<sheet><hr pos="0,0"><text>after</text></sheet>').count("</hr>"), 0)

got = E('<array name="$m"><array>"Games" "x"</array><array>"Navigator" "y"</array></array>'
        '<for init="$i=0" cond="$i<2" next="$i++"><text>&var=$m[{$i}][0];</text></for>')
# spec 5 (<array>): "Games" "x" with no comma is one item, "Gamesx"
check("{$i} is filled in before &var= reads it (main menu labels)",
      re.findall(r">([^<]*)</text>", got), ["Gamesx", "Navigatory"])

got = E('<array name="$cat">"A" "B" "C" "D" "E"</array>'
        '<array name="$d">"1" "null" "d" "h" "1" "4"</array><text>&var=$cat[$d[5]];</text>')
# spec 5 (<array>): no commas, so $cat is ["ABCDE"] and $d is ["1nulldh14"];
# $d[5] is past the end (""), which indexes as 0
check("an array takes a numeric string as its index (story page title)",
      got[got.index("<text"):].split(">", 1)[1].split("<")[0], "ABCDE")
got = E('<array name="$cat">"A","B","C","D","E"</array>'
        '<array name="$d">"1","null","d","h","1","4"</array><text>&var=$cat[$d[5]];</text>')
check("with commas, $d[5] is \"4\" and indexes $cat[4]",
      got[got.index("<text"):].split(">", 1)[1].split("<")[0], "E")

# help/offline/login/in03.pml: a defined number compared with quoted digits.
# Python refused int > str and the pos stayed raw (dotted outline in the preview).
for pages, want in (("4", "445,355"), ("10", "460,355")):
    got = E(f'<define name="$pgt" value="{pages}">'
            '<sheet name="sh_wd" pos="445+15*($pgt>\'9\'),355"></sheet>')
    check(f"a defined number compares with quoted digits ({pages} pages)",
          got.split('pos="', 1)[1].split('"', 1)[0], want)

# ff11/guide/tips/meps01.pml: nodefvalue="001" is a file id, not the number 1.
got = E('<define name="$crt_url" nodefvalue="001">'
        '<define name="$f" calc="\'src/srpm\'+$crt_url+\'.pml\'"><text>&var=$f;</text>')
check("a zero-padded define stays an id (srpm001.pml, not srpm1.pml)",
      got[got.index("<text"):].split(">", 1)[1].split("<")[0], "src/srpm001.pml")

# The tips menu slides in at pos="$sh_bo_xc*$mn_open+$sh_bo_xo*!$mn_open,..":
# `!` is an operand there, and Python's `not` after `*` does not parse.
got = E('<define name="$mn_open" value="0"><define name="$xc" value="7">'
        '<define name="$xo" value="3"><sheet pos="$xc*$mn_open+$xo*!$mn_open,5"></sheet>')
check("unary ! works inside arithmetic",
      got.split('pos="', 1)[1].split('"', 1)[0], "3,5")

# --------------------------------------------------------------------------- #
# The Viewer's own answers: every case in section 11 of
# PlayOnline/docs/notes/pc-viewer/pml-engine-expressions.md, whose expected
# values came from running the port of app.dll's evaluator.
# --------------------------------------------------------------------------- #
print()
print("spec section 11: expressions")


def spec_vars():
    return pmleval.make_vars({
        "mn_id": "3", "crt_url": "001", "zero": "0", "SC_ID": "1", "s": "abc",
        "a": "01", "b": "1", "e": "", "i": "2", "x": "5",
        "mn": [["a", "0"], ["b", "0"], ["c", "0"], ["d", "5"]], "flat": ["x", "y"]})


EXPRS = [
    ("$mn_id+1+$mn[$mn_id][1]", "9"),
    ("'src/srpm'+$crt_url+'.pml'", "src/srpm001.pml"),
    ("'ma_i/masc'+$zero+''+$SC_ID+'i.png'", "ma_i/masc01i.png"),
    ("$zero+$SC_ID+'i.png'", "(Inconsistent parentheses error)"),
    ("'5'+'5'", "10"),
    ("'5'+'x'", "5x"),
    ("'a'+1", "(Inconsistent parentheses error)"),
    ("1+'a'", "(Inconsistent parentheses error)"),
    ("$s+1", "1"),
    ("$a+$b", "2"),
    ("$a==$b", "1"),
    ("'01'==1", "1"),
    ("' 1'=='1'", "0"),
    ("'10'<'9'", "0"),
    ("'B'<'a'", "1"),
    ("$s==0", "1"),
    ("'abc'==0", ""),
    ("''==0", "1"),
    ("$undef", "(Variable error)"),
    ("'x'+$undef", "x(Variable error)"),
    ("$undef+1", "1"),
    ("$undef==0", "1"),
    ("$undef==''", "0"),
    ("!$undef", "1"),
    ("$mn[9][0]", "(Array error)"),
    ("$mn[0][9]", ""),
    ("$mn[0]", "(Array error)"),
    ("$flat[1]", "y"),
    ("$flat[5]", ""),
    ("$s[0]", "(Variable error)"),
    ("$mn['2'][0]", "c"),
    ("$mn[1+1][0]", "c"),
    ("7/2", "3"),
    ("-7/2", "-3"),
    ("-7%3", "-1"),
    ("5/0", "0"),
    ("5%0", "0"),
    ("'a'*2", ""),
    ("'a'-'b'", "(String operation error)"),
    ("!'0'", "(Numeric value error)"),
    ("-'5'", "(Numeric value error)"),
    ("!0", "1"),
    ("~0", "-1"),
    ("1||0&&0", "0"),
    ("0||1&&0", "0"),
    ("1&&0||1", "1"),
    ("2>1==1", "1"),
    ("3&5", "1"),
    ("3|4", "7"),
    ("3^1", "2"),
    ("1+2*3", "7"),
    ("10-3-2", "5"),
    ("-2*3", "-6"),
    ("2*-3", ""),
    ("12abc", "12"),
    ("1.5+1", "1"),
    ("0x10", "0"),
    ("1,2", "1"),
    ("$u ? 1 : 2", "(Variable error)"),
    ("'it\\'s'", "it's"),
    ("'unterminated", "unterminated"),
    ("(1", "(Inconsistent parentheses error)"),
    ("1+", ""),
    ("$_PLATFORM=='WIN'", "1"),
]
for expr, want in EXPRS:
    check(f"{expr}  ->  {want!r}", pmleval.eval_str(expr, spec_vars()), want)
# the int results the table gives for the cases whose string is ""
for expr in ("'abc'==0", "'a'*2", "2*-3"):
    check(f"{expr} as an integer is 0", pmleval.eval_int(expr, spec_vars()), 0)
# SE's malformed memn01.pml delay: 0 for every $i and either $mn_open
check("(500*+200$i)*!$mn_open is 0 whatever the variables",
      {pmleval.eval_int("(500*+200$i)*!$mn_open",
                        pmleval.make_vars({"i": i, "mn_open": o}))
       for i in ("0", "1", "5") for o in ("0", "1")}, {0})

print()
print("spec section 11: assignment, in order from $i=\"2\"")
V = spec_vars()
for expr, want in (("$i=4", "4"), ("$i+=3", "7"), ("$i-=1", "6"), ("$i++", "6"),
                   ("++$i", "8"), ("$q+='x'", "(Variable error)x"), ("$s+=1", "1")):
    check(f"{expr}  ->  {want}", pmleval.eval_str(expr, V), want)
check("$i ends as 8 ($i++ left it 7, ++$i made it 8)", V.get("$i"), "8")


def attr_of(src, name, **values):
    """The value `name` has on the first element of `src` after expansion."""
    got = pmleval.expand(src, sysvars=values)
    m = re.search(r'\b%s="([^"]*)"' % name, got)
    return m and m.group(1).replace("&quot;", '"')


print()
print("spec section 11: attributes")
check("src AUTO: an expression", attr_of('<img src="$F_PATH1+\'in02.pml\'">', "src",
                                         F_PATH1="../"), "../in02.pml")
check("src AUTO: a literal", attr_of('<img src="in02.pml">', "src"), "in02.pml")
check("src AUTO: {$i} is NOT filled in, the whole value evaluates",
      attr_of('<img src="img{$i}.png">', "src", i="2"),
      "(Inconsistent parentheses error)")
check("href BRACE: {$x} is filled in",
      attr_of('<img href="sd:x@{$im_bg}">', "href", im_bg="im_bg0"), "sd:x@im_bg0")
check("href BRACE: the rest stays an action string",
      attr_of('<img href="eval:$a[{$j}]">', "href", j="4"), "eval:$a[4]")
check("alt AUTO: no trigger, no arithmetic", attr_of('<img alt="98+6">', "alt"), "98+6")
check("pos PAIR", attr_of('<sheet pos="98+6,117">', "pos"), "104,117")
check("size PAIR", attr_of('<sheet size="15*31,21">', "size"), "465,21")
check("PAIR: an empty half is -1", attr_of('<sheet pos="$x">', "pos", x="5"), "5,-1")
check("PAIR with variables", attr_of('<sheet pos="$x*2,$i+1">', "pos", x="5", i="2"),
      "10,3")
check("delay INT", attr_of('<sheet delay="900+100*$i">', "delay", i="2"), "1100")
check("numbers vs quoted digits (4)",
      attr_of('<sheet pos="445+15*($pgt>\'9\'),355">', "pos", pgt="4"), "445,355")
check("numbers vs quoted digits (10)",
      attr_of('<sheet pos="445+15*($pgt>\'9\'),355">', "pos", pgt="10"), "460,355")
check("unary ! in a PAIR",
      attr_of('<sheet pos="$xc*$mn_open+$xo*!$mn_open,5">', "pos",
              xc="7", mn_open="0", xo="3"), "3,5")

print()
print("spec section 11: the template layer")


def vars_after(src, url=None):
    """The variable store after a page has run."""
    seen = {}
    real = pmleval._process

    def spy(text, run, depth, base):
        real(text, run, depth, base)
        seen["V"] = run.V
    pmleval._process = spy
    try:
        pmleval.expand(src, url=url)
    finally:
        pmleval._process = real
    return seen["V"]


check('value is literal: value="$y" stores "$y"',
      vars_after('<define name="$x" value="$y">').get("$x"), "$y")
check('calc is always evaluated: calc="7/2" stores "3"',
      vars_after('<define name="$h" calc="7/2">').get("$h"), "3")
check("a define with no value defines nothing",
      vars_after('<define name="$x">').get("$x"), None)
check('value="" defines ""', vars_after('<define name="$x" value="">').get("$x"), "")
check("nodefvalue wins over value when undefined",
      vars_after('<define name="$c" value="5" nodefvalue="9">').get("$c"), "9")
check("nodefvalue keeps a defined value",
      vars_after('<define name="$c" value="1"><define name="$c" value="5" nodefvalue="9">')
      .get("$c"), "1")
check("the URL query sets variables before the page runs",
      vars_after('<define name="$crt_url" nodefvalue="001">',
                 url="x/mepm010.pml?crt_url=010").get("$crt_url"), "010")
check("so does the mirror's encoded file name",
      vars_after('<define name="$crt_url" nodefvalue="001">',
                 url="wh000.pol.com/pml/game/ff11/guide/tips/"
                     "mepm010.pml%3Fcrt_url%3D010%26crt_bt%3D1").get("$crt_bt"), "1")
check("a $_ name cannot be defined",
      vars_after('<define name="$_X" value="1">').get("$_X"), None)


def items(src):
    return vars_after(src).arrays["$c"].to_list()


check('"Title A","B" is two items', items('<array name="$c">"Title A","B"</array>'),
      ["Title A", "B"])
check('"Title A" "B" is ONE item', items('<array name="$c">"Title A" "B"</array>'),
      ["Title AB"])
check("single quotes delimit nothing", items("<array name=\"$c\">'a','b'</array>"),
      ["", ""])
check("a trailing comma adds an empty item",
      items('<array name="$c">"a",\n  "b",</array>'), ["a", "b", ""])
check("a comma inside double quotes is kept", items('<array name="$c">"x,y","z"</array>'),
      ["x,y", "z"])


def text_of(src, **values):
    got = pmleval.expand(src, sysvars=values)
    return "".join(re.findall(r"<text[^>]*>([^<]*)</text>", got))


check("&var= lookups",
      text_of('<array name="$c">"Title A","B"</array>'
              '<text>&var=$c[0];|&var=$c[$id];|&var=$nope[3];</text>', id="1"),
      "Title A|B|(Variable error)")
check("&var=1+2; has no trigger and prints as written",
      text_of("<text>&var=1+2;</text>"), "1+2")
check("&var=$clst+1; is evaluated", text_of("<text>&var=$clst+1;</text>", clst="4"), "5")
check("{$i} in plain text is literal", text_of("<text>A{$i}</text>", i="0"), "A{$i}")
check("&calc= does not exist", text_of("<text>&calc=1+2;</text>"), "&calc=1+2;")

E = lambda src, **v: pmleval.expand(src, sysvars=v).strip()   # noqa: E731
check('<if expr="$x"> with "abc" is false (wcstol)',
      E('<if expr="$x">Y<else>N</if>', x="abc"), "N")
check('<if expr="$x"> with "1abc" is true', E('<if expr="$x">Y<else>N</if>', x="1abc"), "Y")
check("$undef==0 is true", E('<if expr="$undef==0">Y</if>'), "Y")
check("a missing expr runs the body", E("<if>Y</if>"), "Y")
check("a top-level <else> after </if> is ignored (1: XY)",
      E('<if expr="1">X</if><else>Y</else>'), "XY")
check("a top-level <else> after </if> is ignored (0: Y)",
      E('<if expr="0">X</if><else>Y</else>'), "Y")
report = {}
got = pmleval.expand('<if expr="0"><img src="$undef"></if>', report=report)
check("a false branch evaluates nothing", (got, report["missing"]), ("", []))
check("<for> with cond", text_of('<for init="$i=0" cond="$i<3" next="$i++">'
                                 '<text>&var=$i;</text></for>'), "012")
check("<for> without cond runs once", text_of('<for init="$i=0" next="$i++">'
                                              '<text>&var=$i;</text></for>'), "0")
check("<for> stops after 1 + 1024 passes",
      pmleval.expand('<for init="$i=0" cond="1" next="$i++">x</for>').count("x"), 1025)
check("<while> does nothing", E('<while expr="1">W</while>'), "W")

print()
print("env: the page's final variable table, for the browser-side runtime")
env = {}
pmleval.expand('<define name="$crt_url" nodefvalue="001"><define name="$n" calc="2*3">'
               '<array name="$m"><array>"a","0"</array><array>"b","5"</array></array>'
               '<array name="$flat">"x","y"</array>'
               '<for init="$i=0" cond="$i<3" next="$i++"></for>',
               url="x/mepm010.pml%3Fcrt_url%3D010%26crt_bt%3D1", env=env)
check("query variables, defines and loop counters are strings",
      [env.get(k) for k in ("$crt_url", "$crt_bt", "$n", "$i")], ["010", "1", "6", "3"])
check("arrays are nested lists of strings",
      (env.get("$m"), env.get("$flat")), ([["a", "0"], ["b", "5"]], ["x", "y"]))
check("system variables are there, the recomputed ones too",
      (env.get("$_LANG"), env.get("$_PLATFORM"), len(env.get("$_DAT") or "")),
      ("en", "WIN", 2))
check("every value is a string or a list",
      all(isinstance(v, (str, list)) for v in env.values()), True)
check("a page that never defined $x has no $x", "$x" in env, False)

print()
print("system variables (spec section 7)")
SV = pmleval.make_vars()
check("$_LANG is en, $_VERSION the build date, $_SHORTCUT_ID 0",
      (SV.get("$_LANG"), SV.get("$_VERSION"), SV.get("$_SHORTCUT_ID")),
      ("en", "20060221", "0"))
check("$_MON and $_DAT are zero-padded",
      [len(SV.get(n)) for n in ("$_MON", "$_DAT")], [2, 2])
check("'x'+$_MON keeps the padding",
      pmleval.eval_str("'x'+$_MON", SV), "x" + SV.get("$_MON"))

print()
print("pmleval_test: OK" if ok else "pmleval_test: FAILED")
shutil.rmtree(WWW, ignore_errors=True)
sys.exit(0 if ok else 1)
