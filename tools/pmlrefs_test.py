#!/usr/bin/env python3
"""Prove the PML reference index points at the right files.

    python pmlrefs_test.py

WHY THIS EXISTS. Only one .pml in twenty is a whole page; the rest cannot draw
themselves, and the useful thing to tell an admin looking at one is which
pages pull it in. That answer is only worth having if it is RIGHT -- a wrong
link sends them to a page that does not contain the fragment, which is worse
than no link at all.

Two failures from building it are pinned here because both looked fine in
aggregate and were wrong in every particular:

  * matching by glob alone. `$F_PATH1+'in02.pml'` compiles to `*/in02.pml`, and
    `help/login/index.pml` -- which includes exactly ONE in02.pml -- came back
    referencing all fifteen in the tree. Resolving the expression first (the
    template layer knows $F_PATH1) gives the one true edge, and the glob is
    only allowed to guess about a filename that did NOT resolve.

  * an unpinned leading wildcard. Before `*in02.pml` was pinned to a segment
    boundary it also claimed every `mein02.pml`.

The glob fallback still has to WORK, though: the topics include names `$dir`, a
runtime argument that resolves to nothing, and "all 2,200 topic bodies" is the
correct answer there. Both halves are asserted.
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))

WWW = tempfile.mkdtemp(prefix="pmlrefs-")
os.environ["POL_ADMIN_WWW"] = WWW
os.environ.setdefault("POL_ACCOUNTS_DB", os.path.join(WWW, "unused.db"))

import pmlrefs                                   # noqa: E402
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
print("the two kinds of reference are read from different attributes")
check("<include src=> is composition",
      pmlrefs.include_refs('<include src="a.pml"><img src="b.png" href="c.pml">'),
      ["a.pml"])
check("href= is navigation",
      pmlrefs.link_refs('<include src="a.pml"><img src="b.png" href="c.pml">'),
      ["c.pml"])
check("one tag carrying both is read twice",
      (pmlrefs.include_refs('<img src="art.png" href="p.pml">'),
       pmlrefs.link_refs('<img src="art.png" href="p.pml">')),
      ([], ["p.pml"]))

print()
print("to_glob: the shapes SE writes")
check("a path variable plus a literal name",
      pmlrefs.to_glob("$F_PATH1+'in02.pml'"), "*/in02.pml")
check("a runtime argument in the middle",
      pmlrefs.to_glob("'/pcd/topics/ff11/'+$_USER_LANG+'/'+$dir+'/detail.pml'"),
      "/pcd/topics/ff11/*/*/detail.pml")
check("brace interpolation",
      pmlrefs.to_glob("{$C_PATH1}news/nwpm01.pml"), "*/news/nwpm01.pml")
check("a query string is not part of the path",
      pmlrefs.to_glob("{$C_PATH1}index/inpm01.pml?ret=8"), "*/index/inpm01.pml")
check("an eval: link", pmlrefs.to_glob("eval:'topm02.pml?df='+$df"), "topm02.pml")
check("a wildcard INSIDE a name stays inside it",
      pmlrefs.to_glob("'detail'+$n+'.pml'"), "detail*.pml")
check("a command is not a reference", pmlrefs.to_glob("sd:focus@bt00"), None)
check("nor is an off-site URL",
      pmlrefs.to_glob("https://secure.square-enix.com/x.pml"), None)
check("nor is art", pmlrefs.to_glob("$F_PATH2+'im01s.png'"), None)

# --------------------------------------------------------------------------- #
# A fixture shaped like the real mirror: a page whose path variable comes from a
# cont1.pml, a decoy with a colliding filename tail, and a topics pair where the
# include names a runtime argument.
HOST = "wh000.pol.com"
write(f"{HOST}/pml/help/cont1.pml",
      '<define name="$F_PATH1" value="file:/help/">\n')
write(f"{HOST}/pml/help/in02.pml", '<sheet pos="0,60" size="640,380">\n</sheet>\n')
write(f"{HOST}/pml/help/style.pml", '<style name="hdr" size="18">\n')
# where the page's button GOES -- navigation, and nothing it is made of
write(f"{HOST}/pml/help/next.pml",
      '<pml><body><text pos="0,0" size="9,9">n</text></body></pml>\n')
# the decoy: same tail, different file. A `*in02.pml` glob eats this.
write(f"{HOST}/pml/game/ff11/mein02.pml", '<text pos="0,0" size="10,10">no</text>\n')
write(f"{HOST}/pml/info/in02.pml", '<text pos="0,0" size="10,10">no</text>\n')
write(f"{HOST}/pml/help/login/index.pml",
      '<pml><head>\n'
      '<include src="../cont1.pml">\n'
      '<include src="$F_PATH1+\'style.pml\'">\n'
      '</head>\n'
      '<body>\n'
      '<include src="$F_PATH1+\'in02.pml\'">\n'
      '<img src="$F_PATH1+\'img_s/bt01s.png\'" href="$F_PATH1+\'next.pml\'">\n'
      '</body></pml>\n')
# the topics pair: $dir is a runtime argument, so the include can never resolve
write(f"{HOST}/pml/game/ff11/dev/topics/topm02.pml",
      '<pml><body>\n'
      '<include src="\'/pcd/topics/ff11/\'+$_USER_LANG+\'/\'+$dir+\'/detail.pml\'">\n'
      '</body></pml>\n')
for topic in ("3355", "4001"):
    write(f"{HOST}/pcd/topics/ff11/en-US/{topic}/detail.pml",
          f'<data name="doc1"><record>"topic {topic}"</record></data>\n')
# a page that links to another page rather than including it
write(f"{HOST}/pml/main/index.pml",
      '<pml><body>\n'
      '<img src="art.png" href="/pml/help/login/index.pml">\n'
      '<img src="art.png" href="/pml/main/index.pml">\n'   # self-link
      '</body></pml>\n')

# a page whose only include names a file the mirror never archived: still
# constructed, just not from anything we hold.
write(f"{HOST}/pml/broken/index.pml",
      '<pml><body><include src="/pml/broken/gone.pml"></body></pml>\n')

files, shapes, g = admin._pml_scan(os.path.abspath(WWW))
built_from = g.sorted("built_from")
included_by = g.sorted("included_by")
links_to = g.sorted("links_to")
linked_from = g.sorted("linked_from")

print()
print("exact first: an include that resolves is ONE edge, not a spray")
check("the page is built from exactly what it names",
      built_from.get(f"{HOST}/pml/help/login/index.pml"),
      [f"{HOST}/pml/help/cont1.pml", f"{HOST}/pml/help/in02.pml",
       f"{HOST}/pml/help/style.pml"])
check("the decoy with the same tail is NOT claimed",
      f"{HOST}/pml/game/ff11/mein02.pml" in
      (built_from.get(f"{HOST}/pml/help/login/index.pml") or []), False)
check("nor is the other in02.pml in a different directory",
      f"{HOST}/pml/info/in02.pml" in
      (built_from.get(f"{HOST}/pml/help/login/index.pml") or []), False)
check("and the fragment names the page that includes it",
      included_by.get(f"{HOST}/pml/help/in02.pml"),
      [f"{HOST}/pml/help/login/index.pml"])

print()
print("the glob still has to fire where nothing CAN resolve")
# $dir is the topic being viewed. "every topic body" is the right answer.
check("the topics page reaches both bodies",
      built_from.get(f"{HOST}/pml/game/ff11/dev/topics/topm02.pml"),
      [f"{HOST}/pcd/topics/ff11/en-US/3355/detail.pml",
       f"{HOST}/pcd/topics/ff11/en-US/4001/detail.pml"])
check("and a body names the page that displays it",
      included_by.get(f"{HOST}/pcd/topics/ff11/en-US/3355/detail.pml"),
      [f"{HOST}/pml/game/ff11/dev/topics/topm02.pml"])

print()
print("composition and navigation do not leak into each other")
check("a page link is navigation",
      links_to.get(f"{HOST}/pml/main/index.pml"),
      [f"{HOST}/pml/help/login/index.pml"])
check("and NOT composition -- main does not include that page",
      built_from.get(f"{HOST}/pml/main/index.pml"), None)
check("the linked page knows who points at it",
      linked_from.get(f"{HOST}/pml/help/login/index.pml"),
      [f"{HOST}/pml/main/index.pml"])
check("a page linking to itself is not an edge",
      f"{HOST}/pml/main/index.pml" in
      (links_to.get(f"{HOST}/pml/main/index.pml") or []), False)
check("the art src did not become an edge",
      [p for p in (links_to.get(f"{HOST}/pml/main/index.pml") or [])
       if p.endswith(".png")], [])
check("the page's button is navigation",
      links_to.get(f"{HOST}/pml/help/login/index.pml"),
      [f"{HOST}/pml/help/next.pml"])
check("and the files it INCLUDES are not in that list",
      [p for p in (links_to.get(f"{HOST}/pml/help/login/index.pml") or [])
       if p.endswith(("cont1.pml", "style.pml", "in02.pml"))], [])

print()
print("constructed vs wholly original -- what a page SAYS, not what resolved")
check("a page that writes three includes is built from three",
      g.parts.get(f"{HOST}/pml/help/login/index.pml"), 3)
check("a page that writes none is original",
      g.parts.get(f"{HOST}/pml/main/index.pml"), 0)
check("an unresolvable include still makes the page constructed",
      g.parts.get(f"{HOST}/pml/broken/index.pml"), 1)
check("even though it is built from nothing we can find",
      built_from.get(f"{HOST}/pml/broken/index.pml"), None)

print()
print("pmlrefs_test: OK" if ok else "pmlrefs_test: FAILED")
shutil.rmtree(WWW, ignore_errors=True)
sys.exit(0 if ok else 1)
