#!/usr/bin/env python3
"""Rebuild the login ticker's badge strip  pml/main/ma_i/maic06i.ang.

    python tools/make_news_badges.py [--check] [--out FILE]

WHY THIS EXISTS
---------------
`pml/main/index.pml` draws the ticker badge with
`sd:sequence=$LATESTNEWS[i][0]@icNews<i>` -- the announcement's content id used
verbatim as a frame index into this one sprite. So the set of services an
admin can file news under is exactly the set of frames in this file.

SE shipped two versions: a 6-frame strip (band 51304) and an 8-frame one
(band 51300, and the JP PS2 1.18.15 cache -- byte-identical, md5 cad3264d).
The 8-frame one is what we serve, and it ends at frame 7. This tool extends it
to ten so EverQuest II and FINAL FANTASY XIV can be selected too.

WHERE THE NEW ART COMES FROM -- SE's own pixels, not a redraw
------------------------------------------------------------
The client's `data/icon/cicn_s<NNN>.png` content-badge sheets carry both marks,
and they are THE SAME ART SET as this strip: measured, news frames 2/3/4/5/7 are
the same images as cicn content ids 1/2/3/4/11, differing only by palette
quantiser (max delta 22 of 255). So the new frames are lifted whole, not drawn.

    frame 8  <- cicn content id 13, EverQuest II
    frame 9  <- cicn content id 8,  FINAL FANTASY XIV

WARNING: THE SHEET STRIDE IS 40, NOT 42. A sheet is 168x24 and holds four 40x24 slots
with 8px of unused tail; `id = 4*sheet + slot`. Slicing at 42 bleeds the next
slot into the right-hand edge of every tile -- which is what put a strip of the
FriendList smileys (id 14) down the side of the EQII badge on the first pass.
Cross-checked: at stride 40 the transparent gutters in all four sheets land
exactly on the ids known to be blank (0, 5-7, 9, 12, 15).

THE CONTAINER SHARES ONE PALETTE
--------------------------------
`@ANG1B` factors the palette out: a leading blob holds PLTE/tRNS with a
zero-length IDAT, and every frame carries pixels with PLTE/tRNS truncated to
nothing (see newsgen.ang_frames). The existing eight frames already use all 256
entries and the two new badges bring 192 more, so ten frames CANNOT keep the old
palette -- everything is requantised against one new shared one. SE's own 6->8
step did the same thing (measured mean 2/255 on the frames it kept), and this
tool prints the error it introduces on frames 0-7 so the cost is visible rather
than assumed.
"""
import argparse
import io
import os
import struct
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRV = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SRV, "services"))
import newsgen  # noqa: E402

ANG = os.path.join(SRV, "www", "wh000.pol.com", "pml", "main", "ma_i",
                   "maic06i.ang")
#: The cicn_s sheet archive sits OUTSIDE this repo (client art is not shipped
#: here); point this at wherever the extracted sheets live.
SHEETS = os.path.join(SRV, os.pardir, "archive", "shortcut-icons",
                      "content-icons", "us-2011")

#: news frame -> cicn content id. Only the two we are adding; frames 0-7 come
#: from SE's own eight-frame file, so nothing already correct is re-derived.
#:
#: THE ORDER HERE IS DELIBERATE and is the whole reason these two ids are safe.
#: A news id and a PlayOnline content id are different numbering spaces that
#: SE's own values already collide across -- news 1-4 are PlayOnline/FFXI/Tetra/
#: JongHoLow while POL 1-4 are FFXI/Tetra/Janhourou/FMO, so misreading one as
#: the other yields a real but WRONG game. We cannot fix SE's four (their ids
#: are baked into the archived news rows we merge with), but we can pick ours so
#: the same mistake is harmless:
#:
#:   news 8 = FINAL FANTASY XIV, and POL content id 8 is ALSO FFXIV -- aligned,
#:            so reading it as a POL id gives the right answer.
#:   news 9 = EverQuest II, and POL content id 9 WAS NEVER ALLOCATED (the
#:            client's table jumps 7 -> 10) -- so there is nothing to collide
#:            with, and a misread fails loudly instead of quietly.
#:
#: Swapping these two would put EverQuest II on the id that means FFXIV to the
#: rest of PlayOnline. Do not reorder.
NEW_FRAMES = [(8, 8, "FINAL FANTASY XIV"), (9, 13, "EverQuest II")]

#: SE's own eight-frame strip, kept beside the served file. Rebuilds start from
#: THIS, not from whatever is currently served, so the tool is idempotent and a
#: re-run after changing NEW_FRAMES produces the new arrangement rather than
#: appending to the old one.
BASE = ANG + ".se-8frame"

TRANSPARENT_INDEX = 255      # where SE keeps it; see build_palette
SLOT_W, SLOT_H = 40, 24          # a cicn slot; the badge occupies the top 40x20
FRAME_W, FRAME_H = 40, 20

#: SEQUENCES to append, each naming the image it draws. The header field at 0x0C
#: is a sequence count and a sequence indirects to an image -- see
#: newsgen.ang_sequences(). SE's existing 11 sequences map to images
#: 0,1,2,3,4,1,0,5,0,6,7, so appending these two gives sequence 11 -> image 8
#: (FFXIV) and sequence 12 -> image 9 (EverQuest II), which is what
#: newsgen.CONTENTS uses as their content ids.
#:
#: An earlier version copied SE's `0, 6, 7` shape literally and appended
#: `0, 8, 9`, which inserted a BLANK sequence at 11 and pushed both badges one
#: place along -- so FFXIV drew nothing and EverQuest II would have drawn the
#: FFXIV badge. Append exactly the sequences you mean.
CELL_APPEND = [8, 9]


def cicn_badge(content_id):
    """One content badge, at the strip's own 40x20."""
    sheet, slot = divmod(content_id, 4)
    path = os.path.join(SHEETS, "cicn_s%03d.png" % sheet)
    im = Image.open(path).convert("RGBA")
    return im.crop((slot * SLOT_W, 0, slot * SLOT_W + FRAME_W, FRAME_H))


# --------------------------------------------------------------------------- #
# one shared RGBA palette for every frame
# --------------------------------------------------------------------------- #
def build_palette(images, size=256):
    """One shared RGBA palette for every frame, by median cut.

    Plain "keep the N most frequent colours" was tried first and measured badly:
    the eight kept frames share a lot of plate and shadow, so frequency spent
    entries on near-duplicates of those and starved the two new badges -- the
    XIV plate is a smooth red gradient and came out at mean 7.9/255 with visible
    banding. Median cut allocates by spread instead of by area, which is what a
    gradient needs. Alpha is a full axis of the cut, weighted, because the frames
    are cut-outs whose soft shadows matter as much as their hues.
    """
    freq = {}
    for im in images:
        for px in im.getdata():
            freq[px] = freq.get(px, 0) + 1
    # Fully transparent gets ONE exact entry, or the cut-out grows a fringe --
    # and it goes at index 255, where SE puts theirs. SE's own palette has its
    # single transparent entry LAST and an ordinary colour (0,0,0,10) at index
    # 0; an earlier version of this function pinned transparent at index 0
    # instead, which inverts that layout. If the Viewer keys transparency off
    # the palette layout rather than off tRNS, that alone makes every frame
    # wrong from the first pixel. Match SE, do not merely be valid.
    freq.pop((0, 0, 0, 0), None)
    opaque = [(c, n) for c, n in freq.items() if c[3] == 0]
    boxes = [[(c, n) for c, n in freq.items() if c[3] != 0]]
    if opaque:
        boxes.append(opaque)

    A_W = 2                       # alpha counts double when choosing an axis

    def spread(box):
        if len(box) < 2:
            return -1, 0
        best, axis = -1, 0
        for ch in range(4):
            lo = min(c[ch] for c, _ in box)
            hi = max(c[ch] for c, _ in box)
            w = (hi - lo) * (A_W if ch == 3 else 1)
            if w > best:
                best, axis = w, ch
        return best, axis

    while len(boxes) < size - 1:
        cand = max(range(len(boxes)), key=lambda i: spread(boxes[i])[0])
        if spread(boxes[cand])[0] <= 0:
            break
        axis = spread(boxes[cand])[1]
        box = sorted(boxes[cand], key=lambda cn: cn[0][axis])
        half = sum(n for _, n in box) / 2
        acc, cut = 0, 1
        for i, (_, n) in enumerate(box):
            acc += n
            if acc >= half:
                cut = max(1, min(i, len(box) - 1))
                break
        boxes[cand:cand + 1] = [box[:cut], box[cut:]]

    pal = []
    for box in boxes:
        tot = sum(n for _, n in box) or 1
        pal.append(tuple(
            int(round(sum(c[ch] * n for c, n in box) / tot)) for ch in range(4)))
    pal = pal[:size - 1]
    while len(pal) < size - 1:
        pal.append((0, 0, 0, 255))
    pal.append((0, 0, 0, 0))          # transparent LAST, index 255, as SE has it
    return pal


def map_to_palette(im, pal):
    """Indices into `pal`, nearest in RGBA with alpha weighted hard.

    Alpha is weighted because a shadow pixel matched to an opaque plate colour
    of the same hue is a visible black fringe, while a small hue error inside
    the plate is not."""
    cache = {}
    out = bytearray()
    for px in im.getdata():
        idx = cache.get(px)
        if idx is None:
            if px[3] == 0:
                idx = TRANSPARENT_INDEX
            else:
                best, bestd = 0, None
                for i, q in enumerate(pal):
                    d = ((px[0]-q[0]) ** 2 + (px[1]-q[1]) ** 2
                         + (px[2]-q[2]) ** 2 + 4 * (px[3]-q[3]) ** 2)
                    if bestd is None or d < bestd:
                        best, bestd = i, d
                idx = best
            cache[px] = idx
        out.append(idx)
    return bytes(out)


# --------------------------------------------------------------------------- #
# PNG + container
# --------------------------------------------------------------------------- #
def paletted_png(indices, w, h, with_palette=None):
    """An 8-bit colour-type-3 PNG. `with_palette` None means EMPTY PLTE/tRNS,
    which is how the container stores a frame."""
    import zlib
    raw = b"".join(b"\x00" + indices[y*w:(y+1)*w] for y in range(h))
    if with_palette is None:
        plte, trns = b"", b""
    else:
        plte = b"".join(bytes(c[:3]) for c in with_palette)
        trns = bytes(c[3] for c in with_palette)
    return newsgen._png_build([
        (b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)),
        (b"PLTE", plte),
        (b"tRNS", trns),
        # LEVEL 6, NOT 9, AND IT MATTERS. Level 9 produces a byte-identical
        # deflate PAYLOAD here -- measured on every frame -- and differs only in
        # the two-byte zlib header, `78 DA` against SE's `78 9C`. Those bits are
        # FLEVEL, purely informational, and any conformant inflate ignores them.
        # The Viewer does not: a strip built at level 9 was REJECTED outright on
        # 2026-08-21 and drew the broken-image box across the whole ticker, while
        # the same frames at level 6 reproduce SE's file byte-for-byte. Treat
        # `78 9C` as part of the format.
        (b"IDAT", zlib.compress(raw, 6)),
        (b"IEND", b""),
    ])


def palette_record(pal, w, h):
    """The leading blob: the real palette, and a zero-length IDAT."""
    plte = b"".join(bytes(c[:3]) for c in pal)
    trns = bytes(c[3] for c in pal)
    return newsgen._png_build([
        (b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)),
        (b"PLTE", plte),
        (b"tRNS", trns),
        (b"IDAT", b""),
        (b"IEND", b""),
    ])


def parse_container(d):
    total, cells, one, frames, w, h, z1, z2, z3, myst = struct.unpack_from(
        "<10I", d, 8)
    off = 48 + 4 * cells + 4 * (3 + frames + 1)
    recs = [struct.unpack_from("<4I", d, off + 16 * i) for i in range(cells)]
    return dict(one=one, frames=frames, w=w, h=h, z=(z1, z2, z3), myst=myst,
                cell_recs=recs)


def build_container(meta, blobs, cell_img_indices):
    """`blobs` is the palette record followed by every frame, in order."""
    cells = len(cell_img_indices)
    frames = len(blobs) - 1
    hdr = 48 + 4 * cells + 4 * (3 + frames + 1) + 16 * cells
    data = b"".join(blobs)

    # boundaries: data start, palette start, palette end, then each frame start
    # and finally EOF -- the shape read back off both of SE's files.
    offs, at = [], hdr
    starts = []
    for b in blobs:
        starts.append(at)
        at += len(b)
    offs = [hdr, starts[0], starts[1]] + starts[1:] + [hdr + len(data)]

    head = struct.pack("<9I", hdr + len(data), cells, meta["one"], frames,
                       meta["w"], meta["h"], *meta["z"])
    # 0x2C IS A CHECKSUM, and getting this wrong is what broke the ticker four
    # times. app.dll's ANG1B reader (0x04b361b7 in the 0x04900000 image) does:
    #     for i in 0..0x23:  sum += byte[buffer + 8 + i]
    #     if sum != dword[buffer + 0x2C]: bail
    # i.e. the byte-sum of the 36 bytes from 0x08 to 0x2B -- total length, cell
    # count, the 1, frame count, width, height and the three zeros. It is NOT
    # a constant to carry over: every build that changed a count or the file
    # length while keeping SE's value failed this test, and the reader bails
    # before drawing ANYTHING, so the whole sprite goes to the broken-image box
    # rather than one badge going wrong. Verified against all 16 .ang files we
    # hold, SE's included.
    out = b"@ANG1B\r\n" + head + struct.pack("<I", sum(head))
    out += struct.pack("<%dI" % cells,
                       *[hdr - 16 * cells + 16 * i for i in range(cells)])
    out += struct.pack("<%dI" % len(offs), *offs)
    for idx in cell_img_indices:
        out += struct.pack("<4I", 1, idx << 16, 8, 16)
    return out + data


def verify_like_client(blob):
    """Run app.dll's own ANG1B acceptance tests. Returns a list of failures.

    Transcribed from the reader at 0x04b35d40 / 0x04b36160 rather than inferred,
    so a build that passes this is one the Viewer's parser accepts for the same
    reasons it accepts SE's. Four candidate strips were shipped to a live client
    on guesswork before this existed; it is here so that never has to happen
    again.
    """
    f = []
    if blob[:4] != b"@ANG" or blob[5:6] != b"B" or blob[6:8] != b"\r\n":
        f.append("magic: expects '@ANG' + version + 'B' + CRLF")
    if blob[4:5] not in (b"0", b"1"):
        f.append("version byte at +4 must be '0' or '1'")
    if len(blob) < 0x30:
        f.append("shorter than the 0x30 header")
        return f
    total, cells, one, frames, w, h = struct.unpack_from("<6I", blob, 8)
    chk = struct.unpack_from("<I", blob, 0x2C)[0]
    if sum(blob[8:8 + 0x24]) != chk:
        f.append("header checksum: 0x2C is %d, bytes 8..0x2B sum to %d"
                 % (chk, sum(blob[8:8 + 0x24])))
    n = one + frames + cells + 3            # table entry count
    data_at = n * 4 + 0x30
    if len(blob) < data_at:
        f.append("file shorter than the %d-entry table it declares" % n)
        return f
    tab = list(struct.unpack_from("<%dI" % n, blob, 0x30))
    if any(tab[i] > tab[i + 1] for i in range(n - 1)):
        f.append("offset table is not non-decreasing")
    if tab[0] != data_at:
        f.append("table[0] is %d, must equal %d (0x30 + 4*%d)"
                 % (tab[0], data_at, n))
    if tab[cells] != tab[cells + 1]:
        f.append("table[cells] and table[cells+1] must be equal")
    k = cells + one + 1
    if struct.unpack_from("<I", blob, 0x20)[0] != 2 and tab[k] != tab[k + 1]:
        f.append("table[%d] and table[%d] must be equal" % (k, k + 1))
    if tab[k + frames + 1] != total:
        f.append("last table entry is %d, must equal the declared length %d"
                 % (tab[k + frames + 1], total))
    if len(blob) < tab[cells]:
        f.append("file is shorter than table[cells]")
    if total != len(blob):
        f.append("declared length %d != actual %d" % (total, len(blob)))
    return f


def stored_blobs(blob):
    """Every PNG blob exactly as it sits in the container, palette record first."""
    starts, at = [], blob.find(_SIG)
    while at >= 0:
        starts.append(at)
        at = blob.find(_SIG, at + 1)
    return [blob[s:(starts[i + 1] if i + 1 < len(starts) else len(blob))]
            for i, s in enumerate(starts)]


_SIG = b"\x89PNG\r\n\x1a\n"


def palette_from(png):
    ch = {t: d for t, d in newsgen._png_chunks(png)}
    plte, trns = ch[b"PLTE"], ch[b"tRNS"]
    return [(plte[3 * i], plte[3 * i + 1], plte[3 * i + 2], trns[i])
            for i in range(len(trns))]


def build_minimal(base_path, additions):
    """SE's file with frames appended and NOTHING ELSE TOUCHED.

    Rebuilding the whole strip through our own quantiser was wrong twice over.
    It changed the palette LAYOUT -- SE keeps its single fully-transparent entry
    at index 255 and uses index 0 as an ordinary colour, while our build pinned
    transparent at index 0, so if the Viewer keys transparency off the layout
    rather than off tRNS our frames were wrong from the first pixel. And it
    rewrote eight frames that were already known to work, putting them at risk
    to buy colour fidelity on two new ones.

    So: the palette record and all eight of SE's frame blobs are carried over
    BYTE-FOR-BYTE, and the new badges are mapped onto SE's existing palette.
    That costs the new badges some colour accuracy -- SE's palette has no
    headroom, all 256 entries are referenced -- and it is the right trade: a
    duller badge beats a ticker that does not draw. It also makes the next
    result mean something, because everything except the additions is SE's.
    """
    blob = open(base_path, "rb").read()
    meta = parse_container(blob)
    blobs = stored_blobs(blob)
    if len(blobs) != meta["frames"] + 1:
        sys.exit("expected %d blobs, found %d" % (meta["frames"] + 1, len(blobs)))
    pal = palette_from(blobs[0])
    for idx, cid, name in additions:
        im = cicn_badge(cid)
        blobs.append(paletted_png(map_to_palette(im, pal), meta["w"], meta["h"]))
        print("  + frame %d  %-18s from cicn content id %d (SE's palette)"
              % (idx, name, cid))
    cells = [r[1] >> 16 for r in meta["cell_recs"]] + CELL_APPEND
    out = build_container(meta, blobs, cells)

    # the carried-over part must be untouched, or this is not what it claims
    kept = stored_blobs(out)[:meta["frames"] + 1]
    if kept != stored_blobs(blob):
        sys.exit("carried-over blobs changed -- aborting")
    print("  carried over byte-for-byte: palette record + %d frames"
          % meta["frames"])
    return out


def load_frames(path):
    blob = open(path, "rb").read()
    ims = []
    for png in newsgen.ang_frames(blob):
        im = Image.open(io.BytesIO(png))
        im.load()
        ims.append(im.convert("RGBA"))
    return parse_container(blob), ims, blob


def error_vs(a, b):
    pa, pb = list(a.getdata()), list(b.getdata())
    vis = [(x, y) for x, y in zip(pa, pb) if not (x[3] == 0 and y[3] == 0)]
    if not vis:
        return 0, 0.0
    d = [max(abs(i - j) for i, j in zip(x, y)) for x, y in vis]
    return max(d), sum(d) / len(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=ANG)
    ap.add_argument("--check", action="store_true",
                    help="build and report, write nothing")
    ap.add_argument("--requantise", action="store_true", help=(
        "Rebuild ALL frames against one shared palette instead of mapping the "
        "new badges onto SE's. SE's palette holds no red and every entry is "
        "already referenced, so FINAL FANTASY XIV lands at mean 37/255 there "
        "and its drop shadow comes out visibly harsh; requantised together it "
        "is mean 4. The cost is that SE's own eight frames are re-encoded too, "
        "at a measured mean of 2-5/255 -- invisible at 6x, but no longer their "
        "bytes."))
    ap.add_argument("--reencode-only", action="store_true", help=(
        "BISECT BUILD: SE's eight frames, re-encoded through our pipeline, with "
        "NOTHING added. Same frame count, same cell table, same header. The "
        "10-frame build differed from SE's working file in four ways -- frame "
        "count, cell table, the stale 0x2C field, and the image blobs -- and "
        "build_container already reproduces SE's file BYTE-FOR-BYTE from its "
        "parsed parts, so the container writer is proven and this isolates the "
        "one remaining variable: our palette and our PNG writer. Rejected by "
        "the Viewer means the fault is here; rendered means it is in the 8->10 "
        "delta."))
    args = ap.parse_args()

    base = BASE if os.path.exists(BASE) else ANG
    if not args.reencode_only and not args.requantise:
        print("base: %s -- appending only, SE's bytes preserved"
              % os.path.basename(base))
        out = build_minimal(base, NEW_FRAMES)
        blobs = stored_blobs(out)
        print("container: %d images, %d sequences, %d bytes (was %d)"
              % (struct.unpack_from("<I", out, 0x14)[0],
                 struct.unpack_from("<I", out, 0x0c)[0], len(out),
                 os.path.getsize(base)))
        check = newsgen.ang_frames(out)
        print("re-read OK: %d frames" % len(check))
        # The gate that four client cycles paid for: nothing ships unless
        # app.dll's own acceptance tests pass.
        fails = verify_like_client(out)
        if fails:
            sys.exit("REFUSING TO WRITE -- the Viewer's parser would reject this:\n  "
                     + "\n  ".join(fails))
        print("client checks: PASS (magic, checksum, table bounds and linkage)")
        if args.check:
            print("\n--check: nothing written")
            return 0
        with open(args.out, "wb") as fh:
            fh.write(out)
        print("\nwrote", args.out)
        return 0

    meta, frames, original = load_frames(base)
    print("base: %s -- %d frames, %d cells"
          % (os.path.basename(base), len(frames), len(meta["cell_recs"])))
    if len(frames) != 8:
        sys.exit("expected SE's 8-frame strip as the base, found %d frames"
                 % len(frames))

    if args.reencode_only:
        print("  bisect: adding NOTHING -- re-encoding SE's eight frames only")
    else:
        for idx, cid, name in NEW_FRAMES:
            badge = cicn_badge(cid)
            if badge.size != (FRAME_W, FRAME_H):
                sys.exit("cicn id %d cropped to %s, expected %dx%d"
                         % (cid, badge.size, FRAME_W, FRAME_H))
            frames.append(badge)
            print("  + frame %d  %-18s from cicn content id %d" % (idx, name, cid))

    pal = build_palette(frames)
    used = len({c for c in pal})
    print("shared palette: %d distinct entries" % used)

    blobs = [palette_record(pal, meta["w"], meta["h"])]
    quantised = []
    for i, im in enumerate(frames):
        idxs = map_to_palette(im, pal)
        blobs.append(paletted_png(idxs, meta["w"], meta["h"]))
        back = Image.new("RGBA", im.size)
        back.putdata([pal[b] for b in idxs])
        quantised.append(back)
        mx, mean = error_vs(im, back)
        flag = "  (kept frame)" if i < 8 else "  (NEW)"
        print("  frame %d: max delta %3d  mean %5.2f%s" % (i, mx, mean, flag))

    cells = [r[1] >> 16 for r in meta["cell_recs"]]
    if not args.reencode_only:
        cells = cells + CELL_APPEND
    out = build_container(meta, blobs, cells)
    print("container: %d frames, %d cells, %d bytes (was %d)"
          % (len(frames), len(cells), len(out), len(original)))
    if args.reencode_only:
        # Spell out exactly what this build varies, field by field, so the
        # client check answers a question instead of producing a vibe.
        if out == original:
            sys.exit("our pipeline reproduced SE's file exactly -- this build "
                     "varies nothing and would not test anything")
        names = ["total_len", "cells", "?one", "frames", "w", "h",
                 "z1", "z2", "z3", "0x2C"]
        a = struct.unpack_from("<10I", original, 8)
        b = struct.unpack_from("<10I", out, 8)
        diff = [n for n, x, y in zip(names, a, b) if x != y]
        tbl = 48 + 4 * len(cells) + 4 * (3 + len(frames) + 1)
        same_cells = (out[tbl:tbl + 16 * len(cells)]
                      == original[tbl:tbl + 16 * len(cells)])
        print()
        print("  BISECT SCOPE")
        print("    header fields that differ: %s" % (", ".join(diff) or "none"))
        print("    cell table byte-identical: %s" % same_cells)
        print("    size %d -> %d (%+d)" % (len(original), len(out),
                                           len(out) - len(original)))
        print("    => varies ONLY the image blobs (our palette + our PNG"
              " writer);")
        print("       total_len follows from those, and 0x2C is carried"
              " unchanged.")
        if diff != ["total_len"] or not same_cells:
            sys.exit("this build varies more than intended -- it would not "
                     "isolate the encoder")

    # Same gate as the append path: app.dll's acceptance tests, or nothing gets
    # written. A rebuilt strip changes the counts and the length, so the header
    # checksum has to be right -- getting that wrong is what broke the ticker.
    fails = verify_like_client(out)
    if fails:
        sys.exit("REFUSING TO WRITE -- the Viewer would reject this:"
                 + "".join("\n  " + f for f in fails))
    print("client checks: PASS (magic, checksum, table bounds and linkage)")

    # The container must survive our own reader, and each frame must come back
    # EXACTLY as encoded. Compare against the quantised images, not the
    # originals: quantisation error is reported above and is not a container
    # fault, and folding the two together would let a real corruption hide
    # inside a generous tolerance.
    check = newsgen.ang_frames(out)
    if len(check) != len(frames):
        sys.exit("re-read gave %d frames, expected %d" % (len(check), len(frames)))
    for i, png in enumerate(check):
        im = Image.open(io.BytesIO(png)); im.load()
        mx, _ = error_vs(im.convert("RGBA"), quantised[i])
        if mx:
            sys.exit("frame %d does not round-trip (max delta %d)" % (i, mx))
    print("re-read OK: %d frames, every frame bit-exact through the container"
          % len(check))

    if args.check:
        print("\n--check: nothing written")
        return 0
    with open(args.out, "wb") as fh:
        fh.write(out)
    print("\nwrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
