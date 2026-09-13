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
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

WWW = tempfile.mkdtemp(prefix="pmleval-")
os.environ["POL_ADMIN_WWW"] = WWW
os.environ.setdefault("POL_ACCOUNTS_DB", os.path.join(WWW, "unused.db"))

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
V = {"F_PATH1": "file:/help/", "BN_X": 10, "BN_Y": 4, "BN_Ysp": 42, "i": 3}
check("a src expression", pmleval._eval_attr("$F_PATH1+'in02.pml'", V),
      "file:/help/in02.pml")
check("pos is a pair of component expressions",
      pmleval._eval_attr("$BN_X+5,$BN_Y+($BN_Ysp*$i)", V), "15,130")
check("integers stay integers", pmleval._eval_attr("$BN_Ysp/2,0", V), "21,0")
check("a comma inside quotes does not split",
      pmleval._eval_attr("$F_PATH1+'a,b.png'", V), "file:/help/a,b.png")
check("a value with no $ is untouched", pmleval._eval_attr("#302610bb,#30261011", V),
      "#302610bb,#30261011")
check("a non-arithmetic value is left ALONE, not half-substituted",
      pmleval._eval_attr("sd:show=1@$BN_X", V), "sd:show=1@$BN_X")
check("an unknown variable leaves the value alone too",
      pmleval._eval_attr("$NOPE+'x.png'", V), "$NOPE+'x.png'")

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
pmleval.expand('<define name="$a">&var=$never;', report=report)
check("an unset <define> and an unknown &var= are not 'missing'",
      report["missing"], [])

print()
print("pmleval_test: OK" if ok else "pmleval_test: FAILED")
shutil.rmtree(WWW, ignore_errors=True)
sys.exit(0 if ok else 1)
