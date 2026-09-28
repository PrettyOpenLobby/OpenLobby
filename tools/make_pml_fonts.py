"""Extract the Viewer's own font and skin data for the admin panel's PML preview.

The output is derived from the Viewer's own files, so it is NOT committed to
this repository: run this against your own install to produce it. Without it
the preview draws text with a browser font instead, and widgets as plain boxes.

Writes services/admin_web/pmlfont/ (everything client-derived lives there and
nowhere else; the folder is gitignored in the public repo):
  glyphsNN.png  the glyph atlases system/font/polfnt_NN_8bit.png for every face
                file the Viewer ships (00, 03, 04, 05, 06, 07), as grayscale,
                value = coverage; the browser makes each an alpha mask.
  font.json     {"cell": 16, "em": 16,
                 "widths":    n faces x 224 advances at size 16, chars 0x20..0xFF,
                 "kernClass": n faces x 224, the kern row of each left char,
                 "kernBase":  first kern row of each face,
                 "kern":      base64 of the kern rows, 224 bytes each: subtract
                              kern[(class + base) * 224 + right - 0x20],
                 "atlases":   {face: {file, size, compact}},
                 "jis":       a string whose character at index i is the glyph
                              in atlas cell i}
  skinNN_M.png  the PML skin sheets system/skins/pmlskinNN_M.png (RGBA)
  skins.json    the CPmlSkin part table (app.dll 0x104aa300), compacted from a
                parts file given with --parts; skipped without one

ppfont.bin layout: u8 n faces, n kern-row counts at 0x01, widths at 0x10,
then n x 224 kern classes, then the kern rows. The header is re-read every
time; the file differs between installs.

The atlas is 16x16 cells, 32 per row, in JIS X 0208 order:
linear = (row-1)*94 + (cell-1), verified by rendering. The asset files are
obfuscated on disk; `--codec` names a directory holding a `pmlcodec` module
whose `decode(path)` returns `(plain_bytes, kind)`. That codec is not part of
this repository.

    python tools/make_pml_fonts.py --codec DIR [--parts FILE] [--viewer "C:/Program Files (x86)/.../viewer"]
"""
import argparse
import base64
import io
import json
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "services", "admin_web", "pmlfont")
DEFAULT_VIEWER = (r"C:\Program Files (x86)\PlayOnline\SquareEnix"
                  r"\PlayOnlineViewer\viewer")
FACE_FILES = (0, 3, 4, 5, 6, 7)


def jis_order():
    """The character in each atlas cell, by JIS X 0208 row/cell (EUC-JP)."""
    out = []
    for row in range(1, 95):
        for cell in range(1, 95):
            try:
                ch = bytes([0xA0 + row, 0xA0 + cell]).decode("euc_jp")
            except UnicodeDecodeError:
                ch = "\0"
            out.append(ch if len(ch) == 1 else "\0")
    return "".join(out).rstrip("\0")


def coverage(im):
    """Glyph coverage from a palette atlas. polfnt_00 keeps coverage in the
    tRNS alpha of white entries; polfnt_06 uses other indices, same idea."""
    rgba = im.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    out = Image.new("L", (w, h))
    o = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            o[x, y] = a * max(r, g, b) // 255
    return out


def read_ppfont(path):
    pp = open(path, "rb").read()
    n = pp[0]
    counts = list(pp[1:1 + n])
    w0, k0 = 0x10, 0x10 + n * 224
    r0 = k0 + n * 224
    need = r0 + sum(counts) * 224
    if len(pp) < need:
        raise SystemExit("ppfont.bin: %d bytes, the header needs %d" % (len(pp), need))
    widths = [list(pp[w0 + f * 224:w0 + (f + 1) * 224]) for f in range(n)]
    classes = [list(pp[k0 + f * 224:k0 + (f + 1) * 224]) for f in range(n)]
    base = [sum(counts[:f]) for f in range(n)]
    kern = pp[r0:need]
    # FUN_1000271e samples: face 0 "AV" -> A = 10, "To" -> T = 8
    def adv(f, a, b):
        c = ord(a) - 0x20
        return widths[f][c] - kern[(classes[f][c] + base[f]) * 224 + ord(b) - 0x20]
    print("ppfont faces", n, "kern rows", counts, "AV", adv(0, "A", "V"), "To", adv(0, "T", "o"))
    return {"widths": widths, "kernClass": classes, "kernBase": base,
            "kern": base64.b64encode(kern).decode("ascii")}


def compact_parts(path):
    """Only what the renderer draws with: per row, per skin, the file, type,
    region insets and rects; plus the state start-index table."""
    src = json.load(open(path, encoding="utf-8"))
    rows = {}
    for r in src["rows"]:
        rows[r["row"]] = {"cls": r.get("class"), "parts": r.get("imageParts"),
                          "skins": [{"file": s["file"].replace("pmlskin", "skin"),
                                     "type": s["type"], "ins": s["regionInsets_TLBR"],
                                     "rects": s["rects"], "extra": s.get("extraRects") or []}
                                    for s in r["skins"]]}
    state = {str(int(k, 16)): v for k, v in src["kind_low6_state_start_index"].items()}
    return {"source": src["source"], "state": state, "rows": rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewer", default=DEFAULT_VIEWER)
    ap.add_argument("--codec", required=True,
                    help="directory holding a pmlcodec module (decode(path))")
    ap.add_argument("--parts", help="skin parts table (JSON) extracted from app.dll")
    a = ap.parse_args(argv)
    sys.path.insert(0, os.path.abspath(a.codec))
    import pmlcodec

    data = os.path.join(a.viewer, "data")
    os.makedirs(OUT, exist_ok=True)
    atlases = {}
    for n in FACE_FILES:
        p = os.path.join(data, "system", "font", "polfnt_%02d_8bit.png" % n)
        if not os.path.isfile(p):
            continue
        dec, _kind = pmlcodec.decode(p)
        im = Image.open(io.BytesIO(dec))
        # Grayscale: the value IS the coverage. The browser turns it into an
        # alpha mask once when it loads.
        coverage(im).save(os.path.join(OUT, "glyphs%02d.png" % n), optimize=True)
        # A short atlas (polfnt_06: 512x256, 512 glyphs) is the compact
        # layout: indices 0x3ac..0x581 move down by 0x3a0 (spec 2.4).
        atlases[str(n)] = {"file": "glyphs%02d.png" % n, "size": list(im.size),
                           "compact": im.size[1] < 1024}
        print("atlas", n, im.size, "compact" if atlases[str(n)]["compact"] else "")

    meta = {"cell": 16, "em": 16, "cols": 32,
            "source": "PlayOnline Viewer 1.18 ppfont.bin + polfnt_NN_8bit.png",
            "atlases": atlases}
    meta.update(read_ppfont(os.path.join(data, "common", "ppfont.bin")))
    meta["atlas"] = atlases["0"]["size"]
    meta["jis"] = jis_order()
    with open(os.path.join(OUT, "font.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

    if not (a.parts and os.path.isfile(a.parts)):
        print("no --parts file: skins skipped, widgets draw as plain boxes")
        return
    nskin = 0
    for s in range(16):
        for m in (0, 1):
            p = os.path.join(data, "system", "skins", "pmlskin%02d_%d.png" % (s, m))
            if not os.path.isfile(p):
                continue
            dec, _kind = pmlcodec.decode(p)
            Image.open(io.BytesIO(dec)).convert("RGBA").save(
                os.path.join(OUT, "skin%02d_%d.png" % (s, m)), optimize=True)
            nskin += 1
    with open(os.path.join(OUT, "skins.json"), "w", encoding="utf-8") as f:
        json.dump(compact_parts(a.parts), f, separators=(",", ":"))
    print("skin sheets", nskin, "->", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
