#!/usr/bin/env python3
"""Test the Information switcher's SELECTION LOGIC against the real $cnt_d.

    python tools/info_switcher_test.py

Guards pml/info/index.pml's content switcher, which lists one entry per news
content id. It exists because that widget has no other check: the page is 53 KB
of SE template, the admin preview cannot draw the widget (see below), and the
only other oracle is the Viewer itself.

WHAT THIS COVERS, AND WHAT IT DOES NOT.

pmleval -- the admin preview's template layer, which follows the Viewer's
engine -- evaluates the loop as the client does: `$cnt_d[$j][$ar_or]==$i` is
'0'==0, two numeric strings compared as integers, and `!='#'` is a string
compare. It does not draw <multilink>/<addlink>, so this test:
  * lifts the <for> body VERBATIM out of index.pml on disk (only <addlink>,
    which the preview does not draw, is swapped for <text>);
  * drives the real $cnt_d tables through it and reads back the chosen slots.

NOT covered: that the Viewer draws the resulting 8-item popup. Only the real
client can show that, and nothing here should be read as proof that it does.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)
INFO = os.path.join(SRV, "www", "wh000.pol.com", "pml", "info")
sys.path.insert(0, os.path.join(SRV, "services"))
import pmleval  # noqa: E402

CRLF = "\r\n"

# --- pulling the real pieces off disk --------------------------------------- #
def read(p):
    return io.open(p, encoding="utf-8-sig", newline="").read()


def cnt_d_block(lang_file):
    """The LIVE $cnt_d rows; SE keeps a commented-out copy after a blank line."""
    s = read(os.path.join(INFO, lang_file))
    i = s.index('<array name="$cnt_d">')
    j = s.index(CRLF + CRLF, i)
    return s[i:j] + CRLF + "</array>" + CRLF


def live_rows(lang_file):
    """Only real rows -- SE's column-header line is a commented <array>."""
    blk = re.sub(r"<!--.*?-->", "", cnt_d_block(lang_file), flags=re.S)
    return re.findall(r"<array>(.*?)</array>", blk)


def defines(index_file):
    s = read(os.path.join(INFO, index_file))
    return CRLF.join(re.findall(r'<define name="\$(?:ar_\w+|cnt_n)"[^>]*>', s))


def loop_from(index_file):
    s = read(os.path.join(INFO, index_file))
    i = s.index('<multilink name="cnt_l"')
    body = s[i:s.index("</multilink>", i)]
    body = body[body.index(CRLF) + 2:]
    return re.sub(r'<addlink\b[^>]*?href="null:\$cnt_id=\{\$(\w)\}[^>]*>',
                  r"<text>PICK=&var=$\1;</text>", body)


def run(index_file, lang_file, mutate=None):
    blk = cnt_d_block(lang_file)
    if mutate:
        blk = mutate(blk)
    out = pmleval.expand(blk + CRLF + defines(index_file) + CRLF
                         + loop_from(index_file))
    return [int(x) for x in re.findall(r"PICK=(\d+)", out)]


SLOTS = 13          # $cnt_n: one slot per news content id, 0..12
# ids 6 and 8 draw the blank badge, so their rows are hidden with SE's '#'
VISIBLE = [i for i in range(SLOTS) if i not in (6, 8)]
fails = []

# No BEFORE assertion. SE's original uses an <if>/<elsif>/<else> chain that
# pmleval resolves as a single <if> (it gathers the chain from SIBLINGS, and the
# parser nests the branches inside <if>), and its href carries a "{$i}+1" shift
# this harness cannot reproduce. The old behaviour is legible in the source and
# in the commit diff instead: `$i==3` matched no branch, and `$i>3` read slot
# $i+1, so slots 3 and 4 -- Tetra Master and JongHoLow -- were never listed.

for lang in ("inen01.pml", "inde01.pml", "infr01.pml"):
    got = run("index.pml", lang)
    ok = got == VISIBLE
    print("AFTER   %-12s -> slots %s  %s" % (lang, got, "OK" if ok else "MISMATCH"))
    if not ok:
        fails.append(lang)
print()

hidden = run("index.pml", "inen01.pml",
             lambda b: b.replace(',\t"6"</array>', ',\t"#"</array>'))
ok = hidden == [i for i in VISIBLE if i != 7]
print("hide flag  the FMO row marked # -> %s  %s" % (hidden, "OK" if ok else "MISMATCH"))
if not ok:
    fails.append("hide flag")

for lang in ("inen01.pml", "inde01.pml", "infr01.pml"):
    rows = live_rows(lang)
    widths = {len(re.findall(r'"[^"]*"', r)) for r in rows}
    ok = widths == {5} and len(rows) == SLOTS
    print("arity      %-12s %d rows, %s columns  %s"
          % (lang, len(rows), sorted(widths), "OK" if ok else "MISMATCH"))
    if not ok:
        fails.append(lang + " arity")


# --------------------------------------------------------------------------- #
# the news / PlayOnline id spaces
# --------------------------------------------------------------------------- #
# A news content id and a PlayOnline content id are different numbering spaces
# that SE's own values already collide across, so misreading one as the other
# returns a real but WRONG title. SE's ids 1-7 cannot be renumbered (their
# values are archived in SE's own news<N>.pml, which publish() merges), but the
# two ids WE own are chosen so the same mistake is harmless. This pins that
# choice: it is one edit away from being silently undone.
sys.path.insert(0, os.path.join(SRV, 'services'))
import newsgen        # noqa: E402
import contentlist    # noqa: E402

print()
SE_OWNED = 7
ours = {k: v for k, v in newsgen.CONTENTS.items() if v > SE_OWNED}
# The ids are SE's, not ours: they are the sprite's SEQUENCE numbers and are
# baked into SE's archived news<N>.pml rows. So collisions with the PlayOnline
# content table are inherent and not a defect -- ids 1-4 have always collided.
# An earlier version of this test asserted we had CHOSEN non-colliding ids,
# which stopped being true the moment the real numbering was read off the
# sprite. What matters instead, and what the live bugs were, is that every id
# we offer actually draws a badge.
OFF_PLATFORM = {8: 'FINAL FANTASY XIV'}
print()
for key, nid in sorted(newsgen.CONTENTS.items(), key=lambda kv: kv[1]):
    pol = contentlist.CONTENT_TITLES.get(nid) or OFF_PLATFORM.get(nid) or '-'
    print('ids        news %-2d %-22s (PlayOnline id %d means %r)'
          % (nid, newsgen.CONTENT_LABELS[key], nid, pol))

# THE CHECK THAT MATTERS: every offered service must resolve, through the
# sprite's sequence table, to an image that actually draws something. Both
# 2026-08-21 reports -- 'the FFXIV icon is just empty' and 'anything past FMO
# uses FMO' -- were ids whose sequence pointed at the blank image.
avail = newsgen.available_contents(os.path.join(SRV, 'www'))
missing = sorted(set(newsgen.CONTENTS) - set(avail))
print()
if missing:
    fails.append('no drawable badge: ' + ', '.join(missing))
print('badges     all %d services draw a real badge  %s'
      % (len(newsgen.CONTENTS),
         'OK' if not missing else 'MISMATCH ' + str(missing)))

# and no two services may share a sequence, or one wears the other's badge
seen = {}
dupes = []
for k, v in newsgen.CONTENTS.items():
    if v in seen:
        dupes.append('%s and %s both use id %d' % (seen[v], k, v))
    seen[v] = k
if dupes:
    fails.extend(dupes)
print('ids        no two services share a content id  %s'
      % ('OK' if not dupes else 'MISMATCH ' + str(dupes)))

rows = live_rows('inen01.pml')
bad_rows = []
for key, nid in newsgen.CONTENTS.items():
    label = newsgen.CONTENT_LABELS[key]
    if nid >= len(rows) or label not in rows[nid]:
        bad_rows.append('%d=%s' % (nid, label))
if bad_rows:
    fails.append('$cnt_d rows disagree with newsgen: ' + ', '.join(bad_rows))
print('tables     all %d services have a matching $cnt_d row  %s'
      % (len(newsgen.CONTENTS), 'OK' if not bad_rows else 'MISMATCH ' + str(bad_rows)))
print()
print("FAIL" if fails else "PASS", fails or "")
sys.exit(1 if fails else 0)
