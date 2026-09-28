// pml.js -- renders PlayOnline Viewer PML into a 640x480 stage for the admin
// preview.
//
// Text, geometry and skins follow the rules read out of the PC Viewer's
// app.dll (PlayOnline/docs/notes/pc-viewer/pml-engine-layout.md, cited below
// as "spec N.N"). The client data comes from tools/make_pml_fonts.py, which
// writes it to /static/pmlfont/: the glyph atlases polfnt_00/03/04/05/06/07
// (JIS X 0208 order, 16px cells), the ppfont.bin advance and kerning tables,
// and the pmlskinNN sheets with their part table. Text is laid out on the em-16
// integer metrics the Viewer uses; elements are clipped to their own box, their
// ancestors' inner boxes and the stage; a higher zindex sits further back.
//
// What is still approximate: skin state indices for a static page (0, or 2
// inside an enable="0" sheet), the scrollbar layout, the accents the compact
// polfnt_06 atlas draws above capitals, and anything the spec marks INFERRED.
//
// PML is XML-ish but not well formed (unclosed <input>/<img>/<style>, bare &,
// `&name=value;` markers, `<! ... >` comments), so it has its own parser.

(function (global) {
  "use strict";

  const STAGE_W = 640, STAGE_H = 480;

  // ======================================================================
  // parsing
  // ======================================================================
  const VOID = new Set([
    "input", "img", "meta", "formaction", "define", "style", "br", "bgsound",
    "include", "timer", "textbox", "area", "addmenu", "addlink", "config",
    "plugin", "hidden", "bar", "systembg", "inlineimg", "multilink", "hr",
  ]);

  // Comments keep their newlines so element line numbers stay true.
  const blank = (m) => m.replace(/[^\n]/g, "");
  function stripComments(s) {
    return s.replace(/<!--[\s\S]*?-->/g, blank).replace(/<![^>]*>/g, blank);
  }

  const NAMED = {
    quot: '"', amp: "&", lt: "<", gt: ">", nbsp: " ", trade: "™",
    copy: "©", reg: "®", rsquo: "’", lsquo: "‘",
    squo: "'", rdquo: "”", ldquo: "“", hellip: "…",
    mdash: String.fromCharCode(0x2014), ndash: String.fromCharCode(0x2013), eacute: "é", yen: "¥",
    middot: "·", bull: "•", euro: "€",
  };
  // Private glyphs (spec 2.8): entity value 0x7f00xx draws atlas cell
  // 0x1e80 + (xx & 0x7f). They travel through the text as U+E000 + xx.
  const PRIVATE = { padr: 0xac, padl: 0xad, pad1: 0xae, pad2: 0xaf, pad3: 0xb0,
                    padbtn: 0xa6, padok: 0xa8, padlall: 0xb1 };
  for (let i = 1; i <= 9; i++) { PRIVATE["up" + i] = 0xbe + 2 * i; PRIVATE["down" + i] = 0xbf + 2 * i; }
  const PUA = 0xe000;

  function decodeEntities(s) {
    return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+\d?);/gi, (m, e) => {
      if (e[0] === "#") {
        const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return isFinite(n) ? String.fromCodePoint(n) : m;
      }
      const v = NAMED[e.toLowerCase()];
      return v === undefined ? m : v;
    });
  }
  // Body text: the named entities plus the private pad/arrow glyphs.
  function decodeText(s) {
    return westernPunct(decodeEntities(s).replace(/&([a-z]+\d?);/gi, (m, e) => {
      const p = PRIVATE[e.toLowerCase()];
      return p === undefined ? m : String.fromCharCode(PUA + p);
    }));
  }

  // Curly quotes, dashes and the ellipsis have both a full-width JIS form and
  // a Western one (the Viewer's Latin rows, SJIS 0x85xx, spec 2.2). SE's
  // English pages write them as entities (Developers&rsquo; Room) and the
  // Viewer draws them narrow; its Japanese pages, written in Shift-JIS, use
  // the full-width forms. The source encoding is gone by the time text gets
  // here, so the neighbours decide: next to Latin text the Western form is
  // used. INFERRED from how the pages read, not from the converter.
  const WESTERN = 0xe100;                      // WESTERN + Latin-1 code
  const PUNCT = { 0x2018: 0x91, 0x2019: 0x92, 0x201c: 0x93, 0x201d: 0x94,
                  0x2013: 0x96, 0x2014: 0x97, 0x2026: 0x85 };
  function westernPunct(s) {
    const asciiAt = (i) => { const c = s.charCodeAt(i); return c >= 0x20 && c <= 0x7e; };
    let out = "";
    for (let i = 0; i < s.length; i++) {
      const w = PUNCT[s.charCodeAt(i)];
      out += w !== undefined && (asciiAt(i - 1) || asciiAt(i + 1)) ? String.fromCharCode(WESTERN + w) : s[i];
    }
    return out;
  }

  function parseAttrs(s) {
    const attrs = {};
    const re = /([\w:.-]+)\s*(?:=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let m;
    while ((m = re.exec(s))) {
      const k = m[1].toLowerCase();
      const v = m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4]
        : m[5] !== undefined ? m[5] : "";
      attrs[k] = v.replace(/&quot;/g, '"');
    }
    return attrs;
  }

  // A tag ends at the first `>` outside quotes: `<if expr="$a>0">` is one tag.
  const TOKEN = /<\/?[\w:.-]+(?:"[^"]*"|'[^']*'|[^'">])*>|[^<]+/g;

  function parse(src) {
    src = stripComments(src);
    const root = { tag: "#root", attrs: {}, children: [], line: 0 };
    const stack = [root];
    let m, line = 1, last = 0;
    TOKEN.lastIndex = 0;
    while ((m = TOKEN.exec(src))) {
      for (let i = last; i < m.index; i++) if (src.charCodeAt(i) === 10) line++;
      last = m.index;
      const tok = m[0];
      if (tok[0] !== "<") {
        if (tok.trim() !== "") stack[stack.length - 1].children.push({ tag: "#text", text: tok, children: [], line });
        continue;
      }
      if (tok[1] === "/") {
        const name = tok.slice(2).replace(/[\s>].*$/s, "").toLowerCase();
        for (let i = stack.length - 1; i > 0; i--) {
          if (stack[i].tag === name) { stack.length = i; break; }
        }
        continue;
      }
      const name = tok.slice(1).replace(/[\s/>].*$/s, "").toLowerCase();
      const attrs = parseAttrs(tok.slice(1 + name.length).replace(/\/?>$/, ""));
      const node = { tag: name, attrs, children: [], line: +attrs["pml-line"] || line };
      Object.defineProperty(node, "parent", { value: stack[stack.length - 1], enumerable: false });
      stack[stack.length - 1].children.push(node);
      if (!(tok.endsWith("/>") || VOID.has(name))) stack.push(node);
    }
    return root;
  }

  // ======================================================================
  // the Viewer's font and skins
  // ======================================================================
  const FONT = { state: "idle", widths: null, kclass: null, kbase: null, kern: null,
                 jis: null, map: null, atlases: {}, tints: new Map(), empty: new Map(),
                 skins: null, sheets: {}, cuts: new Map() };

  const loadImage = (src) => new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = rej;
    im.src = src;
  });
  // grayscale coverage -> white with alpha = coverage
  function maskOf(img) {
    const c = document.createElement("canvas");
    c.width = img.width; c.height = img.height;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.drawImage(img, 0, 0);
    const d = g.getImageData(0, 0, c.width, c.height);
    for (let i = 0; i < d.data.length; i += 4) {
      d.data[i + 3] = d.data[i];
      d.data[i] = d.data[i + 1] = d.data[i + 2] = 255;
    }
    g.putImageData(d, 0, 0);
    return c;
  }

  function loadFont() {
    if (FONT.state !== "idle") return FONT.promise;
    FONT.state = "loading";
    const base = "/static/pmlfont/";
    FONT.promise = (async () => {
      try {
        const meta = await fetch(base + "font.json").then((r) => r.json());
        // An older font.json (one atlas, no kerning) still draws.
        const atl = meta.atlases || { 0: { file: "glyphs00.png", compact: false } };
        const faces = Object.keys(atl);
        const imgs = await Promise.all(faces.map((f) => loadImage(base + atl[f].file).catch(() => null)));
        faces.forEach((f, i) => {
          if (!imgs[i]) return;
          FONT.atlases[f] = { mask: maskOf(imgs[i]), compact: !!atl[f].compact,
                              cells: (imgs[i].width >> 4) * (imgs[i].height >> 4) };
        });
        if (!FONT.atlases[0]) throw new Error("no polfnt_00");
        FONT.widths = meta.widths;
        FONT.kclass = meta.kernClass || null;
        FONT.kbase = meta.kernBase || null;
        if (meta.kern) FONT.kern = Uint8Array.from(atob(meta.kern), (c) => c.charCodeAt(0));
        FONT.map = new Map();
        for (let i = 0; i < meta.jis.length; i++) {
          const ch = meta.jis[i];
          if (ch !== "\0" && !FONT.map.has(ch)) FONT.map.set(ch, i);
        }
        FONT.geta = FONT.map.get("〓");
        FONT.state = "ready";
      } catch (e) {
        FONT.state = "failed";
        return;
      }
      // Skins are optional: without them widgets draw as plain boxes.
      try {
        const sk = await fetch(base + "skins.json").then((r) => r.json());
        const files = new Set();
        for (const r of Object.values(sk.rows)) r.skins.forEach((s) => files.add(s.file));
        await Promise.all([...files].map((f) => loadImage(base + f)
          .then((im) => { FONT.sheets[f] = im; }).catch(() => {})));
        FONT.skins = sk;
      } catch (e) { /* no skins */ }
    })();
    return FONT.promise;
  }

  // --- characters ------------------------------------------------------
  // Latin-1 as the Viewer's own SJIS rows 0x8540..0x863f carry it (spec 2.2):
  // Windows-1252 byte for the 0x80..0x9f block.
  const CP1252 = { 0x20ac: 0x80, 0x201a: 0x82, 0x0192: 0x83, 0x201e: 0x84, 0x2026: 0x85,
    0x2020: 0x86, 0x2021: 0x87, 0x02c6: 0x88, 0x2030: 0x89, 0x0160: 0x8a, 0x2039: 0x8b,
    0x0152: 0x8c, 0x017d: 0x8e, 0x2018: 0x91, 0x2019: 0x92, 0x201c: 0x93, 0x201d: 0x94,
    0x2022: 0x95, 0x2013: 0x96, 0x2014: 0x97, 0x02dc: 0x98, 0x2122: 0x99, 0x0161: 0x9a,
    0x203a: 0x9b, 0x0153: 0x9c, 0x017e: 0x9e, 0x0178: 0x9f };
  // The Latin code of a character: ASCII 0x21..0x7e, or 0x80..0xff for one
  // that has no JIS X 0208 cell (a JIS character converts to its SJIS
  // full-width form first). -1 for everything else.
  function latin(o) {
    if (o >= 0x21 && o <= 0x7e) return o;
    if (o >= WESTERN + 0x80 && o <= WESTERN + 0xff) return o - WESTERN;
    const l = o >= 0xa0 && o <= 0xff ? o : CP1252[o];
    if (l === undefined) return -1;
    if (FONT.map && FONT.map.has(String.fromCharCode(o))) return -1;
    return l;
  }
  const isLatin = (o) => o === 0x20 || o === 9 || latin(o) >= 0;

  // Face -> atlas file (spec 2.4): 3..7 have their own; 1, 2 and 8..13 draw
  // with polfnt_00. The width table follows the FILE number, clamped to the
  // faces ppfont.bin has, so face 7 measures with face 0.
  function faceFile(face) {
    return face >= 3 && face <= 7 && FONT.atlases[face] ? face : 0;
  }
  function widthFace(face) {
    const f = faceFile(face);
    return FONT.widths && f < FONT.widths.length ? f : 0;
  }

  // Proportional advance (FUN_1000271e): ppfont width minus kerning against
  // the next Latin char, scaled from em 16 and rounded per glyph.
  function ppAdvance(l, next, st) {
    const f = st.wface, i = l - 0x20;
    let w = FONT.widths[f][i];
    const nl = next === 0x20 ? 0x20 : latin(next);
    if (nl >= 0x20 && FONT.kern && FONT.kclass) {
      w -= FONT.kern[(FONT.kclass[f][i] + FONT.kbase[f]) * 224 + (nl - 0x20)] || 0;
    }
    return (w * st.w + 8) >> 4;
  }

  // Advance in stage pixels (spec 2.2). `next` is the following code point
  // in the same run, for kerning.
  function advance(o, next, st) {
    if (o === 0x20 || o === 9) return (st.w >> 1) + st.spacing;
    if (o >= PUA && o <= PUA + 0xff) return st.w + st.spacing;
    const l = latin(o);
    if (l >= 0) {
      if (!st.prop || !FONT.widths) return (st.w >> 1) + st.spacing;
      return ppAdvance(l, next, st) + st.spacing;
    }
    return st.w + st.spacing;
  }

  function cellEmpty(face, idx) {
    const key = face + ":" + idx;
    let v = FONT.empty.get(key);
    if (v !== undefined) return v;
    const g = FONT.atlases[face].mask.getContext("2d", { willReadFrequently: true });
    const d = g.getImageData((idx % 32) * 16, Math.floor(idx / 32) * 16, 16, 16).data;
    v = true;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 8) { v = false; break; }
    FONT.empty.set(key, v);
    return v;
  }

  // Atlas cell for a character (FUN_10274fae): [face file, index], or null
  // when the Viewer would use its extension font (CJK outside JIS X 0208).
  function glyphOf(o, st) {
    let idx;
    if (o >= PUA && o <= PUA + 0xff) return [0, 0x1e80 + (o & 0x7f)];
    const l = latin(o);
    if (l >= 0x21 && l <= 0x7e) idx = (st.prop ? 0x524 : 0x4c6) + l - 0x21;
    else if (l >= 0x80) {
      // SJIS 0x8540.. -> linear 0x2f0 + column; proportional is the next row
      const col = l <= 0xbf ? l - 0x80 : 94 + l - 0xc0;
      idx = 0x2f0 + col + (st.prop ? 0xbc : 0);
    } else {
      idx = FONT.map.get(String.fromCodePoint(o));
      if (idx === undefined) {
        if (o >= 0x4e00 && o <= 0x9fa5) return null;
        idx = FONT.geta;                        // no SJIS mapping: a geta mark
      }
    }
    const f = faceFile(st.face);
    if (f) {
      const a = FONT.atlases[f];
      let j = idx;
      // compact atlas (polfnt_06): 0x3ac..0x581 less 0x44a..0x523 moves down
      // by 0x3a0; nothing else is in it. The accent that some Latin capitals
      // draw above themselves is not modelled.
      if (a.compact) j = idx >= 0x3ac && idx <= 0x581 && !(idx >= 0x44a && idx <= 0x523) ? idx - 0x3a0 : -1;
      // INFERRED: faces 3-7 ship only their proportional Latin sets, yet SE
      // sets face 6 on Japanese text, so an empty cell comes from polfnt_00.
      if (j >= 0 && j < a.cells && !cellEmpty(f, j)) return [f, j];
    }
    return [0, idx];
  }

  // A 16x16 glyph cell tinted to `css`, cached.
  function tinted(face, idx, css) {
    const key = face + "|" + idx + "|" + css;
    let c = FONT.tints.get(key);
    if (c) return c;
    c = document.createElement("canvas");
    c.width = c.height = 16;
    const g = c.getContext("2d");
    g.drawImage(FONT.atlases[face].mask, (idx % 32) * 16, Math.floor(idx / 32) * 16, 16, 16, 0, 0, 16, 16);
    g.globalCompositeOperation = "source-in";
    g.fillStyle = css;
    g.fillRect(0, 0, 16, 16);
    if (FONT.tints.size > 6000) FONT.tints.clear();
    FONT.tints.set(key, c);
    return c;
  }

  // --- skins (spec 3) --------------------------------------------------
  const ROW = { textfield: 0, textarea: 2, selectlist: 3, popuplist: 4, popupfield: 5,
                popupbutton: 6, sbvTextbox: 19, button: 27, submit: 28, reset: 29,
                radio: 32, checkbox: 33, sheet: 34 };
  const skinsReady = () => !!FONT.skins;
  // `skin=` takes 0..15; anything else is 0 (FUN_101e7233).
  function skinId(v, dflt) {
    if (v === undefined || v === "") return dflt || 0;
    const n = num(v);
    return Number.isInteger(n) && n >= 0 && n <= 15 ? n : 0;
  }
  function skinRec(row, skin) {
    const r = FONT.skins && FONT.skins.rows[row];
    return r ? r.skins[skin] || r.skins[0] : null;
  }
  // [top, left, bottom, right] the skin grows its component by (spec 1.4)
  function skinInsets(row, skin) {
    const r = skinsReady() && skinRec(row, skin);
    return r ? r.ins : [0, 0, 0, 0];
  }
  // A rect of a sheet in its own canvas, so a stretched 2px strip never
  // samples its neighbours (the Viewer cuts each rect into its own texture).
  function cut(file, r) {
    const key = file + ":" + r.join(",");
    let c = FONT.cuts.get(key);
    if (c) return c;
    c = document.createElement("canvas");
    c.width = Math.max(1, r[2]); c.height = Math.max(1, r[3]);
    c.getContext("2d").drawImage(FONT.sheets[file], r[0], r[1], r[2], r[3], 0, 0, r[2], r[3]);
    FONT.cuts.set(key, c);
    return c;
  }
  // Draw skin part `row` at state `s` over (0,0,w,h) of `g` (spec 3.3).
  function drawSkin(g, row, skin, s, w, h) {
    const rec = skinRec(row, skin);
    if (!rec || !FONT.sheets[rec.file]) return false;
    const R = rec.rects, part = FONT.skins.rows[row].parts;
    const tab = FONT.skins.state[rec.type];
    let k = tab ? tab[s | 0] || 0 : 0;
    const n = part === "Rectangle" ? 9 : part === "Fixed" ? 1 : 3;
    if (k + n > R.length) k = 0;
    const put = (r, dx, dy, dw, dh) => {
      if (!r || r[2] <= 0 || r[3] <= 0 || dw <= 0 || dh <= 0) return;
      g.drawImage(cut(rec.file, r), 0, 0, r[2], r[3], dx, dy, dw, dh);
    };
    g.imageSmoothingEnabled = true;
    if (part === "Rectangle") {
      // 9-slice: top, bottom, left, right, TL, BL, TR, BR, centre. Corners
      // always draw at full size, even when they overlap.
      const [T, B, L, Rt, TL, BL, TR, BR, C] = R.slice(k, k + 9);
      const iw = Math.max(0, w - TL[2] - TR[2]), ih = Math.max(0, h - TL[3] - BL[3]);
      put(C, TL[2], TL[3], iw, ih);
      put(T, TL[2], 0, iw, T[3]);
      put(B, BL[2], h - B[3], iw, B[3]);
      put(L, 0, TL[3], L[2], ih);
      put(Rt, w - Rt[2], TR[3], Rt[2], ih);
      put(TL, 0, 0, TL[2], TL[3]);
      put(BL, 0, h - BL[3], BL[2], BL[3]);
      put(TR, w - TR[2], 0, TR[2], TR[3]);
      put(BR, w - BR[2], h - BR[3], BR[2], BR[3]);
    } else if (part === "Horizontal") {
      const [L, M, Rt] = R.slice(k, k + 3);
      put(L, 0, 0, L[2], h);
      put(M, L[2], 0, w - L[2] - Rt[2], h);
      put(Rt, w - Rt[2], 0, Rt[2], h);
    } else if (part === "Vertical") {
      const [T, M, B] = R.slice(k, k + 3);
      put(T, 0, 0, w, T[3]);
      put(M, 0, T[3], w, h - T[3] - B[3]);
      put(B, 0, h - B[3], w, B[3]);
    } else {
      put(R[k], 0, 0, R[k][2], R[k][3]);
    }
    return true;
  }
  // skincolor modulates the skin like a vertex colour (INFERRED: D3D
  // modulate; SE's skincolor="#00000000" buttons show only the caption).
  function tintCanvas(c, col) {
    if (!col || (col.r === 255 && col.g === 255 && col.b === 255 && col.a === 1)) return;
    const g = c.getContext("2d");
    const d = g.getImageData(0, 0, c.width, c.height);
    const a = d.data;
    for (let i = 0; i < a.length; i += 4) {
      a[i] = a[i] * col.r / 255; a[i + 1] = a[i + 1] * col.g / 255;
      a[i + 2] = a[i + 2] * col.b / 255; a[i + 3] = a[i + 3] * col.a;
    }
    g.putImageData(d, 0, 0);
  }
  // Paint an element's skin canvas at state `s`. The canvas and its part
  // are remembered on the element, so a runtime can repaint it on focus.
  function paintSkin(d, s) {
    const k = d._pmlSkin;
    if (!k) return false;
    const g = k.canvas.getContext("2d");
    g.clearRect(0, 0, k.canvas.width, k.canvas.height);
    for (const p of k.parts) {
      g.save();
      g.translate(p.x || 0, p.y || 0);
      drawSkin(g, p.row, k.skin, p.fixedState !== undefined ? p.fixedState : s, p.w, p.h);
      g.restore();
    }
    tintCanvas(k.canvas, k.tint);
    k.state = s;
    return true;
  }
  function addSkin(d, skin, parts, tint, s) {
    const c = el("canvas", "pml-skin");
    c.width = Math.max(1, d._pmlW); c.height = Math.max(1, d._pmlH);
    c.style.position = "absolute";
    c.style.left = c.style.top = "0";
    d.insertBefore(c, d.firstChild);
    d._pmlSkin = { canvas: c, skin, parts, tint, state: s };
    paintSkin(d, s);
  }

  // ======================================================================
  // colours, numbers, styles
  // ======================================================================
  // "#RRGGBBAA" (alpha last), "RRGGBB", or a "fill,outline" pair.
  function rgba(c) {
    if (!c) return null;
    const m = /^#?([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(String(c).trim());
    if (!m) return null;
    const h = m[1];
    const a = m[2] ? parseInt(m[2], 16) / 255 : 1;
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16),
             b: parseInt(h.slice(4, 6), 16), a };
  }
  const css = (c) => c && `rgba(${c.r},${c.g},${c.b},${+c.a.toFixed(3)})`;
  const visible = (c) => !!c && c.a > 0.02;
  const nonZero = (c) => !!c && (c.r || c.g || c.b || c.a);
  function pair(v) {
    const [f, o] = String(v || "").split(",");
    return [rgba(f), rgba(o)];
  }
  // SE evaluates arithmetic in numeric attributes: pos="98+6,117". Only digits,
  // operators and brackets get this far, so evaluating them is safe.
  function num(x) {
    x = String(x).trim();
    if (/^-?\d+(\.\d+)?$/.test(x)) return Math.trunc(parseFloat(x));
    if (/^[\d\s+\-*/().]+$/.test(x) && /\d/.test(x)) {
      try { const v = Function('"use strict";return (' + x + ")")(); return isFinite(v) ? Math.trunc(v) : NaN; }
      catch (e) { return NaN; }
    }
    return parseFloat(x);
  }
  function nums(s, n) {
    const p = String(s || "").split(",").map(num);
    const out = [];
    for (let i = 0; i < n; i++) out.push(isFinite(p[i]) ? p[i] : 0);
    return out;
  }
  // pos/size (FUN_101e66d3): split at the first comma; a missing half is -1.
  function halves(s) {
    const str = String(s);
    const i = str.indexOf(",");
    const a = i < 0 ? str : str.slice(0, i), b = i < 0 ? "" : str.slice(i + 1);
    const v = (t) => { if (t.trim() === "") return -1; const n = num(t); return isFinite(n) ? n : -1; };
    return [v(a), v(b)];
  }
  const posOf = (a) => (a.pos === undefined || a.pos === "" ? [0, 0] : halves(a.pos));
  const unresolved = (v) => /\$[A-Za-z_]/.test(String(v || ""));
  // The inner extent children see (spec 1.2): set on every host element.
  const innerOf = (host) => [host._pmlW || STAGE_W, host._pmlH || STAGE_H];
  // "Fill parent" (FUN_101e806a): a size half < 1 reaches the parent's inner
  // right/bottom edge.
  function fillSize(a, parent, x, y, dw, dh) {
    const [pw, ph] = innerOf(parent);
    let [w, h] = a.size === undefined || a.size === "" ? [-1, -1] : halves(a.size);
    if (w < 1) w = dw !== undefined ? dw : pw - x;
    if (h < 1) h = dh !== undefined ? dh : ph - y;
    return [Math.max(0, w), Math.max(0, h)];
  }
  // zindex 0..20, anything else 0 (FUN_101e7201); higher draws BEHIND (1.7).
  function zOf(v) {
    if (v === undefined || v === "") return 0;
    const n = num(v);
    return Number.isInteger(n) && n >= 0 && n <= 20 ? n : 0;
  }

  // The default font state (FUN_1001177b): face 0, 16x16, spacing 0, no
  // flags, fill 0xff000000 (read as opaque black, INFERRED).
  const BASE_FONT = { face: 0, wface: 0, w: 16, h: 16, spacing: 0, vspacing: 0, bold: false,
                      italic: false, underline: false, strike: false, prop: false,
                      fill: { r: 0, g: 0, b: 0, a: 1 }, outline: null, name: "" };
  const DEFAULT_STYLE = BASE_FONT;
  // Styles a page uses but never defines (C17_2, W19, C19 on SE's story page)
  // come from the Viewer's own stylesheet, which the mirror does not have. The
  // ones SE does define follow one scheme -- letter = colour, digits = size:
  // C = #333333 / #101010, W = #f0f0f0, B = #000000 -- so read the name the
  // same way instead of drawing 16px black.
  const NAMED_COLOR = { C: "#333333ff", W: "#f0f0f0ff", B: "#000000ff" };
  function namedStyle(name) {
    const m = /^([CWB])(\d{2})(?:_\d)?$/.exec(name || "");
    return m ? { size: m[2], face: "2", proportional: "1", color: NAMED_COLOR[m[1]] } : null;
  }
  // Only the attributes a <style> sets override the base state (spec 2.1).
  function makeStyle(s, name) {
    const st = { ...BASE_FONT, name };
    const has = (k) => s[k] !== undefined && s[k] !== "";
    if (has("face")) {
      const f = num(s.face);
      st.face = Number.isInteger(f) && f >= 0 && f <= 13 ? f : 0;
    }
    if (has("size")) {
      const [w, h] = String(s.size).split(",").map(num);
      if (isFinite(w) && w > 0) { st.w = w; st.h = isFinite(h) && h > 0 ? h : w; }
    }
    const flag = (k) => has(k) && num(s[k]) !== 0 && s[k] !== "0";
    st.bold = flag("bold"); st.italic = flag("italic");
    st.underline = flag("underline"); st.strike = flag("strike");
    st.prop = flag("proportional");
    // spacing and vspacing are signed chars
    if (has("spacing")) st.spacing = (num(s.spacing) << 24 >> 24) || 0;
    if (has("vspacing")) st.vspacing = (num(s.vspacing) << 24 >> 24) || 0;
    if (has("color")) {
      const [fill, edge] = pair(s.color);
      if (fill) st.fill = fill;
      st.outline = nonZero(edge) ? edge : null;
    }
    st.wface = widthFace(st.face);
    return st;
  }
  function styleOf(ctx, name) {
    if (ctx.styleCache.has(name)) return ctx.styleCache.get(name);
    let s = ctx.styles[name], st;
    if (!s) {
      if (name) ctx.stats.missingStyles.add(name);
      s = namedStyle(name);
    }
    st = s ? makeStyle(s, name) : { ...DEFAULT_STYLE, wface: widthFace(0) };
    ctx.styleCache.set(name, st);
    return st;
  }
  // Height of a run: the font height, plus 2 with an edge (spec 2.6).
  const runH = (st) => st.h + (st.outline ? 2 : 0);

  // ======================================================================
  // inline markup -> items
  // ======================================================================
  // &br;  &style=N; .. &style;  &image=N;  &pre=N;  &pos=X;  &li; / &li=M;
  // &sp=N;  &a=URL; .. &a;  &size=W,H; .. &size;  &var=$x; (unresolved)
  // A <text> body without the source formatting around it: the line break
  // and indentation after `<text>` and before `</text>`. Spaces on the text's
  // own line are the page's: the FFXI top menu indents its labels with four
  // ("    Information"), and trimming them drew every label on the frame.
  function bodyText(s) {
    return String(s).replace(/^[ \t]*\r?\n\s*/, "").replace(/\s*\r?\n[ \t]*$/, "");
  }

  function inlineItems(text, ctx, baseStyle) {
    const items = [];
    const RE = /&(br|style|image|pre|pos|var|li|sp|size|a|table)(?:=([^;]*))?;/g;
    let at = 0, m, style = baseStyle, link = null, named = baseStyle;
    // `&pre=1;` switches the text to preformatted: from there on a newline is
    // a line break. Otherwise a newline is only whitespace in the source file,
    // and the box does the wrapping. SE writes `&pre=1;` (or `&pre=01;`) and
    // nothing else, ~4,000 times; newsgen's Information page and the help
    // manual both rely on it for their line breaks.
    let pre = false;
    const pushText = (s, before, after) => {
      if (!s) return;
      s = s.replace(/\t+/g, "");
      // a source line break next to an &br; is layout, not a space
      if (!pre && before === "br") s = s.replace(/^\s*[\r\n]\s*/, "");
      if (!pre && after === "br") s = s.replace(/\s*[\r\n]\s*$/, "");
      const parts = pre ? s.split(/\r?\n/) : [s.replace(/\s*[\r\n]+\s*/g, " ")];
      parts.forEach((part, i) => {
        if (i) items.push({ t: "br", style });
        const d = decodeText(part);
        if (d) items.push({ t: "text", s: d, style, link });
      });
    };
    let lastKind = null;
    while ((m = RE.exec(text))) {
      pushText(text.slice(at, m.index), lastKind, m[1]);
      lastKind = m[1];
      at = m.index + m[0].length;
      const kind = m[1], arg = m[2];
      if (kind === "br") items.push({ t: "br", style });
      else if (kind === "style") style = named = arg === undefined ? baseStyle : styleOf(ctx, arg);
      else if (kind === "size") {
        // the inline size command (spec 2.2, 0x7f escape): W or W,H
        const [w, h] = String(arg || "").split(",").map(num);
        style = arg === undefined || !(w > 0) ? named : { ...style, w, h: h > 0 ? h : w };
      }
      else if (kind === "image") items.push({ t: "img", decl: ctx.inlineimgs[arg], name: arg });
      else if (kind === "pre") pre = parseInt(arg || "1", 10) !== 0;
      else if (kind === "pos") items.push({ t: "pos", x: +arg || 0 });
      else if (kind === "li") {
        // `&li=pb;` names an <inlineimg> to use as the bullet; otherwise the
        // argument is the bullet text itself.
        if (arg !== undefined && ctx.inlineimgs[arg]) items.push({ t: "li", decl: ctx.inlineimgs[arg], name: arg });
        else items.push({ t: "li", mark: arg === undefined ? "・" : decodeText(arg) });
      }
      // `&sp=N;` is N half-width spaces, not pixels: the FFXI tips menu pads
      // each label so sp + 2 x (full-width chars) stays 28, and the topics
      // ticker puts `&sp=2;` between a date and its headline.
      else if (kind === "sp") items.push({ t: "sp", n: +arg || 0 });
      else if (kind === "a") link = arg === undefined ? null : arg;
      else if (kind === "var") {
        items.push({ t: "text", s: "(Variable error)", style, link, error: true });
        ctx.stats.varErrors++;
      }
      // &table= changes nothing we can draw yet.
    }
    pushText(text.slice(at), lastKind, null);
    return items;
  }

  // ======================================================================
  // line breaking and layout
  // ======================================================================
  // ASCII break classes (FUN_10002b9a): 1 = break after, never before;
  // 2 = never break after; 8 = never break before; 0 = letters, digits, `-`.
  const BRK_AFTER = new Set(" \t!)?]}");
  const NO_AFTER = new Set("$([\\{");
  const NO_BEFORE = new Set(",.:;");
  // JP kinsoku (FUN_10002bf6 / 0x1031d87a). INFERRED: the usual JIS X 4051
  // sets; the client's exact lists were not dumped.
  const KIN_BEFORE = new Set("、。，．・：；？！゛゜ヽヾゝゞ々ー）］｝」』〕〉》】ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ’”～…‥");
  const KIN_AFTER = new Set("（［｛「『〔〈《【‘“");
  function canBreak(a, b, wordwrap) {
    if (!wordwrap) return true;                // every character is a break point
    const ca = String.fromCodePoint(a), cb = String.fromCodePoint(b);
    const la = isLatin(a), lb = isLatin(b);
    if (lb && NO_BEFORE.has(cb)) return false;
    if (la && lb) return BRK_AFTER.has(ca);
    if (la) return !NO_AFTER.has(ca) && !KIN_BEFORE.has(cb);
    if (KIN_AFTER.has(ca)) return false;
    return lb || !KIN_BEFORE.has(cb);
  }

  // Break items into lines no wider than `maxW` (0 = no wrapping).
  //
  // A line is { parts, w, h, vs }: h = the tallest run, vs = the largest
  // vspacing; the next line starts h + vs below, and the text sits vs/2 down
  // (spec 2.6). The width of a line includes a breaking space at its end
  // (spec 2.5).
  //
  // Illustrations (<inlineimg align=...>): `left`/`right` FLOAT beside the text
  // -- the manual pages put a screenshot on the right with the words beside it
  // -- and `center` takes a line of its own. Icons (skip/offset) sit in the text.
  function layout(items, maxW, baseStyle, wordwrap) {
    if (wordwrap === undefined) wordwrap = true;
    const lines = [];
    const floats = [];                          // {side, w, bottom}
    const placed = [];                          // floated images, absolute
    let line = null, indent = 0, yAcc = 0, cur = baseStyle;
    const limits = () => {
      let off = 0, lim = maxW;
      for (const f of floats) {
        if (yAcc >= f.bottom) continue;
        if (maxW) lim -= f.w;
        if (f.side === "left") off += f.w;
      }
      return [off, lim];
    };
    const close = () => {
      if (!line) return;
      if (!line.h) { line.h = runH(line.st); line.vs = line.st.vspacing; }  // an empty line
      yAcc += line.h + line.vs;
    };
    const newLine = (wrapped) => {
      close();
      const [off, lim] = limits();
      line = { parts: [], w: indent, h: 0, vs: 0, x0: indent, off, lim, st: cur, wrapped: !!wrapped };
      lines.push(line);
    };
    newLine();
    const fits = (w) => !line.lim || line.w + w <= line.lim;
    const place = (part, w) => {
      part.x = line.w;
      line.parts.push(part);
      line.w += w;
    };
    const grow = (st) => {
      if (runH(st) > line.h) line.h = runH(st);
      if (st.vspacing > line.vs) line.vs = st.vspacing;
    };
    // chars of one style/link go into one run part
    const putChars = (chars) => {
      for (const c of chars) {
        const last = line.parts[line.parts.length - 1];
        if (last && last.t === "run" && last.st === c.st && last.link === c.link
            && last.error === c.error && last.x + last.w === line.w) {
          last.chars.push(c); last.w += c.adv; line.w += c.adv;
        } else {
          place({ t: "run", chars: [c], st: c.st, link: c.link, error: c.error, w: c.adv }, c.adv);
        }
        grow(c.st);
      }
    };
    const isSpace = (c) => c.o === 0x20 || c.o === 9;
    const putWord = (word) => {
      let core = word.length;
      while (core && isSpace(word[core - 1])) core--;
      const wCore = word.slice(0, core).reduce((s, c) => s + c.adv, 0);
      if (!core) {                               // spaces only
        if (!line.wrapped || line.parts.length) putChars(word);   // none at a wrapped line's start
        return;
      }
      if (!fits(wCore) && line.parts.length) newLine(true);
      if (!fits(wCore)) {
        // no break point fits: end the line before the overflowing char
        for (const c of word) {
          if (!isSpace(c) && !fits(c.adv) && line.parts.length) newLine(true);
          putChars([c]);
        }
        return;
      }
      putChars(word);                            // trailing spaces may overhang
    };
    let word = [], prev = null;
    const flush = () => { if (word.length) putWord(word); word = []; prev = null; };
    for (const it of items) {
      if (it.t !== "text") flush();
      if (it.t === "br") { if (it.style) cur = it.style; indent = 0; line.st = line.parts.length ? line.st : cur; newLine(); continue; }
      if (it.t === "sp") { place({ t: "gap" }, it.n * advance(0x20, 0, cur)); continue; }
      if (it.t === "pos") { if (it.x > line.w) line.w = it.x; continue; }
      if (it.t === "li") {
        if (it.decl) {
          const [iw, ih] = nums(it.decl.size, 2);
          const skip = parseFloat(it.decl.skip) || (iw || 12) + 4;
          place({ t: "img", decl: it.decl, name: it.name, w: iw || 12, h: ih || 12,
                  oy: parseFloat(it.decl.offset) || 0 }, skip);
        } else {
          const cs = [...it.mark].map((ch, i, a) => ({ o: ch.codePointAt(0), st: baseStyle,
            adv: advance(ch.codePointAt(0), a[i + 1] ? a[i + 1].codePointAt(0) : 0, baseStyle) }));
          putChars(cs);
        }
        indent = line.w;
        continue;
      }
      if (it.t === "img") {
        const d = it.decl || {};
        const [w, h] = it.decl ? nums(d.size, 2) : [16, 16];
        if (d.align === "left" || d.align === "right") {
          const m = nums(d.margin, 4);
          const top = yAcc + (line.parts.length ? line.h + line.vs : 0);
          const fw = w + m[0] + m[2];
          placed.push({ decl: d, name: it.name, w, h,
                        x: d.align === "right" ? (maxW || w) - w - m[2] : m[0], y: top + m[1] });
          floats.push({ side: d.align, w: fw, bottom: top + h + m[1] + m[3] });
          if (!line.parts.length) {                // the current line narrows now
            const [off, lim] = limits();
            line.off = off; line.lim = lim;
          }
          continue;
        }
        if (d.align) {                             // center: its own line
          const m = nums(d.margin, 4);
          if (line.parts.length) newLine();
          line.parts.push({ t: "img", decl: d, name: it.name, w, h, block: "center", m, x: 0 });
          line.h = Math.max(1, h + m[1] + m[3]);
          indent = 0;
          newLine();
          continue;
        }
        // An icon in the text: advances the pen by `skip`, nudged by `offset`,
        // and does not make the line taller.
        const skip = parseFloat(d.skip) || (w || 16);
        if (!fits(skip) && line.parts.length) newLine(true);
        place({ t: "img", decl: it.decl, name: it.name, w: w || 16, h: h || 16, oy: parseFloat(d.offset) || 0 }, skip);
        continue;
      }
      // text: characters, grouped into words at the break points
      const st = it.style;
      cur = st;
      const cps = [...it.s].map((c) => c.codePointAt(0));
      for (let i = 0; i < cps.length; i++) {
        const o = cps[i];
        if (prev !== null && canBreak(prev, o, wordwrap)) { putWord(word); word = []; }
        word.push({ o, st, link: it.link, error: it.error, adv: advance(o, cps[i + 1], st) });
        prev = o;
      }
    }
    flush();
    close();
    // a float taller than the text still takes up room
    const bottom = Math.max(0, ...floats.map((f) => f.bottom));
    const textH = lines.reduce((a, l) => a + l.h + l.vs, 0);
    if (bottom > textH) lines[lines.length - 1].h += bottom - textH;
    lines.floats = placed;
    return lines;
  }
  const linesH = (lines) => lines.reduce((a, l) => a + l.h + l.vs, 0);
  const linesW = (lines) => Math.max(0, ...lines.map((l) => l.w + (l.off || 0)));
  const edged = (lines) => lines.some((l) => l.parts.some((p) => p.t === "run" && p.st.outline));

  // Draw laid-out lines onto a canvas context (spec 2.7). `box` is the
  // element's size; `m` = [left, top, right, bottom] margins.
  function drawLines(g, lines, box, align, valign, m, ctx) {
    const [ml, mt, mr, mb] = m;
    const contentH = linesH(lines) + mt + mb;
    const y0 = valign === "middle" || valign === "center" ? Math.trunc((box.h - contentH) / 2) + mt
      : valign === "bottom" ? box.h - contentH + mt : mt;
    let y = y0;
    const links = [];
    for (const f of lines.floats || []) {
      ctx.pending.push({ decl: f.decl, name: f.name, x: ml + f.x, y: y0 + f.y, w: f.w, h: f.h, g });
    }
    for (const l of lines) {
      const off = l.off || 0;
      const room = (l.lim || box.w - ml - mr);
      let x = ml + off + (align === "center" ? Math.trunc((room - l.w) / 2)
        : align === "right" ? room - l.w : 0);
      // INFERRED: a caption wider than its box starts at the left edge. SE
      // pads help-menu captions with spaces to push the arrow right
      // ("&image=pt;Controls      ...&image=tri;"); centred, such a line
      // starts left of the box and its bullet is clipped away.
      if (box.clampLeft && x < ml + off) x = ml + off;
      const ty = y + Math.trunc(l.vs / 2);
      for (const p of l.parts) {
        if (p.t === "run") {
          const top = ty + l.h - runH(p.st);
          drawRun(g, p, x + p.x, top, ctx);
          if (p.link) links.push({ href: p.link, x: x + p.x, y, w: p.w, h: l.h + l.vs });
        } else if (p.t === "img" && p.block) {
          const [bl, bt, br] = p.m;
          ctx.pending.push({ decl: p.decl, name: p.name, x: ml + (box.w - ml - mr - p.w) / 2 + bl - br, y: y + bt, w: p.w, h: p.h, g });
        } else if (p.t === "img") {
          ctx.pending.push({ decl: p.decl, name: p.name, x: x + p.x, y: ty + (l.h - p.h) / 2 + (p.oy || 0), w: p.w, h: p.h, g });
        }
      }
      y += l.h + l.vs;
    }
    return links;
  }

  // One run of glyphs. Each glyph is its WHOLE 16x16 cell stretched to the
  // style's W x H, point-sampled at 16x16 and smoothed otherwise; bold is a
  // second copy 1px right; italic shears the top edge 0.4 x size right
  // (FUN_102736fa). An edge is a 1px outline drawn behind the fill (INFERRED).
  function drawRun(g, p, x, top, ctx) {
    const st = p.st;
    const fill = p.error ? "rgba(255,90,90,1)" : css(st.fill);
    const out = st.outline && css(st.outline);
    const e = out ? 1 : 0;
    const smooth = !(st.w === 16 && st.h === 16 && !st.italic);
    let pen = x;
    for (const c of p.chars) {
      if (c.o !== 0x20 && c.o !== 9 && c.o !== 0x3000) {
        const gl = FONT.state === "ready" ? glyphOf(c.o, st) : null;
        if (gl) {
          const draw = (color, ox, oy) => {
            const img = tinted(gl[0], gl[1], color);
            g.imageSmoothingEnabled = smooth;
            if (st.italic) {
              g.save();
              g.translate(pen + e + ox, top + e + oy);
              g.transform(1, 0, -0.4 * st.w / st.h, 1, 0.4 * st.w, 0);
              g.drawImage(img, 0, 0, 16, 16, 0, 0, st.w, st.h);
              g.restore();
            } else {
              g.drawImage(img, 0, 0, 16, 16, pen + e + ox, top + e + oy, st.w, st.h);
            }
          };
          if (out) {
            for (const [ox, oy] of [[-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]]) draw(out, ox, oy);
          }
          draw(fill, 0, 0);
          if (st.bold) draw(fill, 1, 0);
        } else if (FONT.state !== "ready" || c.o >= 0x4e00) {
          // not in the Viewer's atlas (its extension font covers these CJK):
          // a browser font stands in, and is counted
          ctx.stats.standIn++;
          const ch = String.fromCodePoint(c.o);
          g.font = `${Math.round(st.h)}px sans-serif`;
          g.textBaseline = "top";
          if (out) { g.strokeStyle = out; g.lineWidth = 2; g.strokeText(ch, pen + e, top + e); }
          g.fillStyle = fill;
          g.fillText(ch, pen + e, top + e);
        }
      }
      pen += c.adv;
    }
    if (st.underline || st.strike) {
      g.fillStyle = fill;
      if (st.underline) g.fillRect(x + e, top + e + st.h - 1, pen - x, 1);
      if (st.strike) g.fillRect(x + e, top + e + (st.h >> 1), pen - x, 1);
    }
    return pen;
  }

  // ======================================================================
  // collecting <style>, <inlineimg>, <data>
  // ======================================================================
  function textOf(n) {
    let t = "";
    (function walk(x) { x.children.forEach((c) => { if (c.tag === "#text") t += c.text; else walk(c); }); })(n);
    return t;
  }
  function collect(node, ctx) {
    const a = node.attrs || {};
    // A duplicate style name is ignored: the FIRST definition wins (spec 2.1).
    if (node.tag === "style" && a.name && !(a.name in ctx.styles)) ctx.styles[a.name] = a;
    if (node.tag === "inlineimg" && a.name) ctx.inlineimgs[a.name] = a;
    if (node.tag === "title" && !ctx.title) ctx.title = decodeEntities(textOf(node)).trim();
    if (node.tag === "data" && a.name) {
      const recs = [];
      (function walk(n) {
        n.children.forEach((c) => {
          if (c.tag === "record") recs.push(textOf(c).trim().replace(/^"|"$/g, ""));
          else walk(c);
        });
      })(node);
      const key = a.name + (a.sub ? "|" + a.sub : "");
      ctx.records[key] = recs;
      if (!ctx.records[a.name]) ctx.records[a.name] = recs;
    }
    (node.children || []).forEach((c) => collect(c, ctx));
  }

  // ======================================================================
  // elements
  // ======================================================================
  let artBase = "";
  function artUrl(src, extra) {
    if (!src) return null;
    // `a.png,b.png,...` is an animation's frame list: show the first.
    src = String(src).split(",")[0].trim();
    if (!src || unresolved(src)) return null;
    const s = src.replace(/^file:\/*/, "").replace(/^\/+/, "");
    const q = [];
    if (artBase) {
      // The raw src goes along so the server can resolve it the way the
      // Viewer does: relative to the page, from the host, or from pml/.
      q.push("base=" + encodeURIComponent(artBase));
      q.push("src=" + encodeURIComponent(src));
    }
    if (extra) q.push(extra);
    return "/art/" + s + (q.length ? "?" + q.join("&") : "");
  }
  const isArt = (src) => /\.(png|jpg|jpeg|gif|ang)(\?|$)/i.test(String(src || "").split(",")[0].trim());

  function el(tag, cls) {
    const d = document.createElement(tag);
    if (cls) d.className = cls;
    return d;
  }

  // An element's box. Every box clips its content (spec 1.5) and stacks by
  // the inverted zindex; equal z keeps source order (spec 1.7).
  function box(node, parent, ctx, x, y, w, h) {
    const d = el("div", "pml-el");
    d.style.left = x + "px";
    d.style.top = y + "px";
    d.style.overflow = "hidden";
    if (w) d.style.width = w + "px";
    if (h) d.style.height = h + "px";
    d._pmlW = w; d._pmlH = h;
    d._pmlNode = node;
    if (ctx.hideNode === node) {
      // interactive: drawn, but hidden until the page shows it
      ctx.hideNode = null;
      d._pmlHidden = true;
      d.style.display = "none";
    }
    const a = node.attrs;
    d.style.zIndex = 20 - zOf(a.zindex);
    d.dataset.pmlTag = node.tag;
    d.dataset.pmlIdx = ctx.nodes.length;
    ctx.nodes.push(node);
    if (a.href) {
      d.dataset.href = a.href;
      d.classList.add("pml-link");
    }
    if (a.alt) d.title = a.alt;
    if (Object.keys(a).some((k) => unresolved(a[k]) && k !== "href" && !k.startsWith("on") && !k.startsWith("sd:"))) {
      d.classList.add("pml-unresolved");
      ctx.stats.unresolved++;
    }
    parent.appendChild(d);
    ctx.stats.placed++;
    ctx.stats.tags[node.tag] = (ctx.stats.tags[node.tag] || 0) + 1;
    return d;
  }
  function resize(d, w, h) {
    d._pmlW = w; d._pmlH = h;
    d.style.width = w + "px";
    d.style.height = h + "px";
  }
  // A plain child div at (x, y, w, h) of `d` that clips, for skinned boxes.
  function innerBox(d, x, y, w, h) {
    const c = el("div", "pml-inner");
    Object.assign(c.style, { position: "absolute", left: x + "px", top: y + "px",
                             width: w + "px", height: h + "px", overflow: "hidden" });
    c._pmlW = w; c._pmlH = h;
    d.appendChild(c);
    return c;
  }
  // margin="l,t,r,b" (spec 2.7). One value is all four, two are l/r and t/b
  // (INFERRED: the one- and two-value forms were not traced).
  function margins(v) {
    if (v === undefined || v === "") return [0, 0, 0, 0];
    const p = String(v).split(",").map(num).map((n) => (isFinite(n) ? n : 0));
    if (p.length === 1) return [p[0], p[0], p[0], p[0]];
    if (p.length === 2) return [p[0], p[1], p[0], p[1]];
    return [p[0], p[1], p[2] || 0, p[3] || 0];
  }

  // Lay out and draw text into a canvas that fills `host`.
  // opt: {w, h, mode (bit 0 = width fixed, bit 1 = height fixed), align,
  //       valign, margin, wordwrap, nowrap}. `nowrap` lays out on one line
  //       (captions are fit-size text components, spec 2.9). Returns the box
  //       size it settled on.
  function textCanvas(host, items, st, ctx, opt) {
    let m = opt.margin || [0, 0, 0, 0];
    const mode = opt.mode === undefined ? 3 : opt.mode;
    // Two margins that meet or pass the width are both dropped (FUN_101fc130).
    if ((mode & 1) && m[0] + m[2] >= opt.w) m = [0, m[1], 0, m[3]];
    const wrapW = mode & 1 && !opt.nowrap ? Math.max(1, opt.w - m[0] - m[2]) : 0;
    const lines = layout(items, wrapW, st, opt.wordwrap);
    const e = edged(lines) ? 2 : 0;
    const w = mode & 1 ? opt.w : linesW(lines) + m[0] + m[2] + e;
    const h = mode & 2 ? opt.h : linesH(lines) + m[1] + m[3];
    const c = el("canvas", "pml-text");
    c.width = Math.max(1, Math.ceil(w)); c.height = Math.max(1, Math.ceil(h));
    const g = c.getContext("2d");
    const links = drawLines(g, lines, { w, h, clampLeft: !!opt.nowrap }, opt.align || "left", opt.valign || "top", m, ctx);
    host.appendChild(c);
    for (const L of links) {
      const a = el("div", "pml-link pml-inline-link");
      Object.assign(a.style, { left: L.x + "px", top: L.y + "px", width: L.w + "px", height: L.h + "px" });
      a.dataset.href = L.href;
      host.appendChild(a);
    }
    return { canvas: c, w, h, natural: linesH(lines) + m[1] + m[3] };
  }

  // Load a picture as a list of image URLs to stack, bottom first. An `.ang`
  // can carry a BASE image drawn under every sequence (the server says so in
  // X-Ang-Base); everything else is one image. Resolves to [] when missing.
  const ANG_CACHE = new Map();
  function loadLayers(url) {
    if (!/\.ang(\?|$)/i.test(url)) return Promise.resolve([url]);
    if (ANG_CACHE.has(url)) return ANG_CACHE.get(url);
    const p = fetch(url).then(async (r) => {
      if (!r.ok) return [];
      const frame = URL.createObjectURL(await r.blob());
      if (r.headers.get("X-Ang-Base") !== "1") return [frame];
      const b = await fetch(url + (url.includes("?") ? "&" : "?") + "layer=base");
      return b.ok ? [URL.createObjectURL(await b.blob()), frame] : [frame];
    }).catch(() => []);
    ANG_CACHE.set(url, p);
    return p;
  }

  function drawPendingImages(ctx) {
    // An inline image the mirror lacks: a faint plate on the text canvas, and
    // an overlay div the "Missing art" toggle can light up.
    const missing = (p) => {
      p.g.save();
      p.g.fillStyle = "rgba(255,255,255,.08)";
      p.g.fillRect(p.x, p.y, p.w, p.h);
      p.g.restore();
      const host = p.g.canvas.parentElement;
      if (host) {
        const o = el("div", "pml-el pml-missing pml-inline-missing");
        Object.assign(o.style, { left: p.g.canvas.offsetLeft + p.x + "px", top: p.g.canvas.offsetTop + p.y + "px",
                                 width: p.w + "px", height: p.h + "px" });
        o.title = p.decl && p.decl.src || "";
        host.appendChild(o);
      }
    };
    for (const p of ctx.pending.splice(0)) {
      const url = p.decl && isArt(p.decl.src) && artUrl(p.decl.src,
        /\.ang/i.test(p.decl.src) ? "seq=0" : "");
      if (!url) { missing(p); continue; }
      loadLayers(url).then((layers) => {
        if (!layers.length) { missing(p); ctx.onMissing && ctx.onMissing(p.decl.src); return; }
        // draw in order, bottom layer first
        layers.reduce((prev, u) => prev.then(() => new Promise((res) => {
          const im = new Image();
          im.onload = () => { p.g.drawImage(im, p.x, p.y, p.w, p.h); res(); };
          im.onerror = () => res();
          im.src = u;
        })), Promise.resolve());
      });
    }
  }

  // Put a picture (plain or `.ang`, with its base layer) into `d`, under
  // any canvas already there. onSize(w, h) reports the natural size once.
  function showArt(d, src, seq, w, h, ctx, onSize) {
    const url = artUrl(src, /\.ang/i.test(src) ? "seq=" + (seq || 0) : "");
    if (!url) return false;
    d.querySelectorAll(":scope > img").forEach((im) => im.remove());
    const add = (u, first) => {
      const im = el("img");
      im.src = u;
      im.draggable = false;
      if (w) im.style.width = w + "px";
      if (h) im.style.height = h + "px";
      if (first) {
        im.onload = () => onSize && onSize(im.naturalWidth, im.naturalHeight);
        im.onerror = () => {
          im.style.display = "none";          // the hatched plate shows instead
          d.classList.add("pml-missing");
          if (!d.title) d.title = "Not in the mirror: " + src;
          ctx.onMissing && ctx.onMissing(src);
        };
      }
      // images go under any caption canvas already in the box
      d.insertBefore(im, d.querySelector(":scope > canvas"));
    };
    if (/\.ang/i.test(src)) {
      loadLayers(url).then((layers) => {
        if (!layers.length) { d.classList.add("pml-missing"); ctx.onMissing && ctx.onMissing(src); return; }
        layers.forEach((u, i) => add(u, i === 0));
      });
    } else {
      add(url, true);
    }
    return true;
  }

  function renderChildren(children, parent, ctx) {
    for (const n of children) renderNode(n, parent, ctx);
  }

  // A widget's skin state for a static page: 0, or 2 (disabled) inside an
  // enable="0" sheet or on an enable="0" element. The runtime notes
  // (pml-engine-runtime.md 3.3a) give the live states: fields 1 = focused,
  // 3 = editing; buttons 1 = pressed/selected, 3 = focused/hover (unconfirmed).
  const staticState = (ctx, a) => (ctx.disabled || a.enable === "0" ? 2 : 0);

  // Button/text-field geometry: a skinned widget grows by its skin's region
  // insets like a sheet does, and its content keeps the authored box.
  // INFERRED for widgets (spec 1.4 traces only the sheet): skin 0's push
  // button art is 48px tall with its shadow, which a 32px button + (0,16)
  // insets fills exactly.
  function skinnedBox(n, parent, ctx, x, y, w, h, row, skin) {
    const [t, l, b, r] = skinInsets(row, skin);
    const d = box(n, parent, ctx, x - l, y - t, w + l + r, h + t + b);
    return { d, inner: [l, t] };
  }

  function renderNode(n, parent, ctx) {
    if (n.tag === "#text") return;
    const a = n.attrs;

    // show="0": a panel the page reveals later. Offered as a layer instead.
    if (a.show === "0" || (a.show && /\$/.test(a.show)) || a.visible === "0") {
      const name = a.name || `panel ${ctx.layers.length + 1}`;
      if (!ctx.layers.includes(name)) ctx.layers.push(name);
      if (ctx.reveal !== "all" && ctx.reveal !== name) {
        if (!ctx.interactive) return;
        ctx.hideNode = n;
      } else if (ctx.interactive) {
        ctx.revealed.add(n);
      }
    }

    const [x, y] = posOf(a);

    switch (n.tag) {
      case "timer":
        ctx.timers.push({ node: n, host: parent });
        return;
      case "head": case "title": case "style": case "inlineimg": case "data":
      case "record": case "meta": case "config": case "bgsound":
      case "formaction": case "define": case "array": case "include": case "script":
      case "addmenu": case "addlink": case "multilink": case "download": case "plugin":
      case "hidden": case "option":
        return;

      case "sheet": case "scrollarea": case "systembg": {
        const [w, h] = fillSize(a, parent, x, y);
        // A sheet takes the document's skin unless border or bordertype is
        // 0, and its frame grows outward by the skin's region insets while
        // its children keep the authored box (spec 1.4).
        const skinned = n.tag === "sheet" && a.border !== "0" && a.bordertype !== "0" && skinsReady();
        const skin = skinId(a.skin, ctx.docSkin);
        const [t, l, b, r] = skinned ? skinInsets(ROW.sheet, skin) : [0, 0, 0, 0];
        const d = box(n, parent, ctx, x - l, y - t, w + l + r, h + t + b);
        d.classList.add("pml-" + n.tag);
        d._pmlGeom = { x, y, w, h, l, t };
        // `alpha` is a fade flag, not an opacity; alphacolor is the opacity.
        if (a.alphacolor !== undefined && isFinite(num(a.alphacolor))) {
          d.style.opacity = Math.max(0, Math.min(255, num(a.alphacolor))) / 255;
        }
        let host = skinned ? innerBox(d, l, t, w, h) : d;
        if (skinned) {
          d.classList.add("pml-skinned");
          d.style.boxShadow = "none";
          addSkin(d, skin, [{ row: ROW.sheet, w: w + l + r, h: h + t + b }], rgba(a.skincolor), staticState(ctx, a));
        }
        const fill = rgba(a.bgcolor) || (!skinned && rgba(a.skincolor));
        if (visible(fill)) host.style.background = css(fill);
        const bg = a.background || (n.tag === "systembg" ? a.src : null);
        if (bg && isArt(bg)) {
          const u = artUrl(bg);
          if (u) { host.style.backgroundImage = `url("${u}")`; host.style.backgroundRepeat = "no-repeat"; }
        }
        if (n.tag === "scrollarea") {
          d.classList.add(a.vbar === "never" ? "pml-noscroll" : "pml-scroll");
          d.style.overflow = "";
          const [aw, ah] = nums(a.areasize, 2);
          if (ah > h || aw > w) {
            host = el("div", "pml-area");
            host.style.width = (aw || w) + "px";
            host.style.height = (ah || h) + "px";
            host._pmlW = aw || w; host._pmlH = ah || h;
            d.appendChild(host);
          }
        }
        const off = a.enable === "0";
        if (off) ctx.disabled++;
        renderChildren(n.children, host, ctx);
        if (off) ctx.disabled--;
        return;
      }

      case "text": {
        const st = styleOf(ctx, a.style);
        // size modes (spec 1.3): absent = fill the parent, fixed; `W,H` fixed;
        // `fit`/`0,0` = content both ways; `W,` = width W, content height;
        // `,H` = height H, content width.
        let mode = 3, w, h;
        if (a.size === undefined || a.size === "") {
          [w, h] = fillSize(a, parent, x, y);
        } else if (/^\s*fit\s*$/i.test(a.size)) {
          mode = 0; w = h = 0;
        } else {
          [w, h] = halves(a.size);
          mode = (w > 0 ? 1 : 0) | (h > 0 ? 2 : 0);
        }
        const d = box(n, parent, ctx, x, y, Math.max(0, w), Math.max(0, h));
        const bg = rgba(a.bgcolor);
        if (visible(bg)) d.style.background = css(bg);
        const text = n.children.filter((c) => c.tag === "#text").map((c) => c.text).join("");
        let shown = text;
        const draw = (style, newText) => {
          if (newText !== undefined) shown = newText;
          const s2 = styleOf(ctx, style === undefined ? a.style : style);
          d.querySelectorAll(":scope > canvas.pml-text, :scope > .pml-inline-link").forEach((c) => c.remove());
          const r2 = textCanvas(d, inlineItems(bodyText(shown), ctx, s2), s2, ctx, { w, h, mode, align: a.align || "left",
            valign: a.valign || "top", margin: margins(a.margin), wordwrap: a.wordwrap !== "0" });
          if (mode !== 3) resize(d, r2.w, r2.h);
          drawPendingImages(ctx);
        };
        const items = inlineItems(bodyText(text), ctx, st);
        const r = textCanvas(d, items, st, ctx, { w, h, mode, align: a.align || "left",
          valign: a.valign || "top", margin: margins(a.margin), wordwrap: a.wordwrap !== "0" });
        if (mode !== 3) resize(d, r.w, r.h);
        d._pmlRestyle = draw;
        renderChildren(n.children.filter((c) => c.tag !== "#text"), d, ctx);
        return;
      }

      case "img": case "input": {
        if (n.tag === "input" && a.type !== "image") return renderWidget(n, parent, ctx, x, y);
        const [w, h] = a.size ? nums(a.size, 2) : [0, 0];
        const d = box(n, parent, ctx, x, y, w, h);
        d.classList.add("pml-img");
        // A link image shows .ang sequence 0, or 2 when disabled; its frame
        // follows focus, not sd:sequence (pml-engine-runtime.md 3.3).
        const link = !!a.href || n.tag === "input";
        const sq = /(^|\s)(\d+)/.exec(String(a["sd:sequence"] || ""));
        const seq = link ? (ctx.disabled || a.enable === "0" ? 2 : 0) : sq ? +sq[2] : 0;
        let value = a.value;
        let late = false;
        const caption = () => {
          d.querySelectorAll(":scope > canvas.pml-text").forEach((c) => c.remove());
          if (!value) return;
          const st = styleOf(ctx, a.style);
          textCanvas(d, inlineItems(value, ctx, st), st, ctx, { w: d._pmlW, h: d._pmlH, mode: 3,
            align: a.align || "center", valign: a.valign || "middle", nowrap: true });
          // Inline images in a caption (the help menus' `&image=pt;` bullets)
          // wait on ctx.pending. The page's own pass draws them once; a
          // caption drawn after its picture loaded must draw them itself.
          if (late) drawPendingImages(ctx);
        };
        d._pmlCaption = (v) => { value = v; late = true; if (d._pmlW && d._pmlH) caption(); };
        if (isArt(a.src)) {
          d._pmlArt = { src: a.src, w, h, index: 0, seq };
          showArt(d, a.src, seq, w, h, ctx, (nw, nh) => {
            // INFERRED: without `size` the picture's natural size is used.
            if (!w || !h) { late = true; resize(d, w || nw, h || nh); caption(); }
          });
        } else if (a.src) {
          d.classList.add("pml-missing");
        }
        if (w && h) caption();
        renderChildren(n.children, d, ctx);
        return;
      }

      case "textbox": {
        const st = styleOf(ctx, a.style);
        const [w, h] = fillSize(a, parent, x, y);
        const d = box(n, parent, ctx, x, y, w, h);
        const fill = rgba(a.skincolor) || rgba(a.bgcolor);
        if (visible(fill)) d.style.background = css(fill);
        const recs = ctx.records[(a.ref || "") + (a.sub ? "|" + a.sub : "")] || ctx.records[a.ref || ""];
        const rec = recs ? recs[parseInt(a.index || "0", 10)] || "" : "";
        if (!recs) ctx.stats.missingData.add(a.ref || "?");
        const m = margins(a.margin);
        // Laid out at W with auto height; if taller than the box, again at
        // W-16 with a 16px scrollbar (FUN_101f844d).
        const pane = el("div", "pml-textpane");
        Object.assign(pane.style, { position: "absolute", left: 0, top: 0, width: w + "px",
                                    height: h + "px", overflowY: "auto", scrollbarWidth: "none" });
        d.appendChild(pane);
        const items = inlineItems(rec, ctx, st);
        const opt = { w, h: 0, mode: 1, align: a.align || "left", margin: m, wordwrap: a.wordwrap !== "0" };
        let r = textCanvas(pane, items, st, ctx, opt);
        r.canvas.style.position = "relative";
        if (r.h > h && w > 16 && a.vbar !== "never") {
          pane.innerHTML = "";
          pane.style.width = w - 16 + "px";
          r = textCanvas(pane, items, st, ctx, { ...opt, w: w - 16 });
          r.canvas.style.position = "relative";
          scrollbar(d, pane, w - 16, h, r.h, skinId(a.skin, ctx.docSkin));
        }
        return;
      }

      case "select": case "button": case "textarea": case "radio": case "checkbox":
        return renderWidget(n, parent, ctx, x, y);

      case "hr": {
        const [w] = fillSize(a, parent, x, y);
        const h = halves(a.size || "")[1];
        const d = box(n, parent, ctx, x, y, w, h > 0 ? h : 1);
        const c = rgba(a.skincolor) || rgba(a.color) || { r: 200, g: 200, b: 200, a: 0.6 };
        d.style.background = css(c);
        return;
      }

      case "table": case "inlinetable": {
        const [w, h] = nums(a.size, 2);
        const d = box(n, parent, ctx, x, y, w, h);
        d.classList.add("pml-table");
        const [spacing] = nums(a.cellspacing, 1);
        const [padding] = nums(a.cellpadding, 1);
        let ry = 0;
        const rows = [];
        (function gather(list) {
          for (const c of list) {
            if (c.tag === "tr") rows.push(c);
            else if (c.tag !== "td" && c.children) gather(c.children);
          }
        })(n.children);
        for (const tr of rows) {
          const cells = tr.children.filter((c) => c.tag === "td");
          const rh = parseFloat(tr.attrs.height) || 0;
          let rx = 0, rowH = rh;
          for (const td of cells) {
            const tw = parseFloat(td.attrs.width) || (w ? (w - rx) / Math.max(1, cells.length - cells.indexOf(td)) : 80);
            const cell = box(td, d, ctx, rx, ry, tw, rh || 0);
            cell.classList.add("pml-td");
            const bg = rgba(td.attrs.bgcolor);
            if (visible(bg)) cell.style.background = css(bg);
            const inner = el("div", "pml-el");
            Object.assign(inner.style, { left: padding + "px", top: padding + "px", right: padding + "px", bottom: padding + "px" });
            inner._pmlW = tw - 2 * padding;
            cell.appendChild(inner);
            renderChildren(td.children, inner, ctx);
            const t = td.children.find((c) => c.tag === "#text");
            if (t) {
              const st = styleOf(ctx, td.attrs.style || a.style);
              const r = textCanvas(inner, inlineItems(bodyText(t.text), ctx, st), st, ctx,
                { w: tw - 2 * padding, mode: 1, align: td.attrs.align || "left" });
              if (!rh) rowH = Math.max(rowH, r.natural + 2 * padding);
            }
            rx += tw + spacing;
          }
          ry += (rowH || 20) + spacing;
        }
        if (!h) d.style.height = ry + "px";
        return;
      }

      default: {
        // Containers without drawing of their own (form, if leftovers, body...)
        if (n.children.length) {
          if (a.pos) {
            const [w, h] = fillSize(a, parent, x, y);
            const d = box(n, parent, ctx, x, y, w, h);
            d.classList.add("pml-group");
            renderChildren(n.children, d, ctx);
          } else {
            renderChildren(n.children, parent, ctx);
          }
        } else if (!["br", "body", "pml", "form"].includes(n.tag)) {
          ctx.stats.unsupported.add(n.tag);
        }
      }
    }
  }

  // Form controls and buttons, drawn with the page's skin when the skin
  // sheets are in; otherwise as plain boxes.
  function renderWidget(n, parent, ctx, x, y) {
    const a = n.attrs;
    if (a.type === "hidden") return;
    const st = styleOf(ctx, a.style);
    const tag = n.tag === "input" && /^(radio|checkbox)$/.test(a.type || "") ? a.type : n.tag;
    const kind = tag === "input" ? (a.type === "submit" || a.type === "reset" ? "button" : "field") : tag;
    const skin = skinId(a.skin, ctx.docSkin);
    const s = staticState(ctx, a);
    let row, w, h, parts;
    if (kind === "button") {
      // default 96x32 (FUN_101c41be); the skin is always applied
      [w, h] = fillSize(a, parent, x, y, 96, 32);
      row = a.type === "submit" ? ROW.submit : a.type === "reset" ? ROW.reset : ROW.button;
    } else if (kind === "radio" || kind === "checkbox") {
      [w, h] = fillSize(a, parent, x, y, 16, 16);
      row = kind === "radio" ? ROW.radio : ROW.checkbox;
    } else if (kind === "select") {
      [w, h] = fillSize(a, parent, x, y);
      row = a.popup !== undefined ? ROW.popupfield : ROW.selectlist;
    } else if (kind === "textarea") {
      [w, h] = fillSize(a, parent, x, y);
      row = ROW.textarea;
    } else {
      [w, h] = fillSize(a, parent, x, y);
      row = ROW.textfield;
    }
    const skinned = skinsReady();
    const { d, inner } = skinned ? skinnedBox(n, parent, ctx, x, y, w, h, row, skin)
      : { d: box(n, parent, ctx, x, y, w, h), inner: [0, 0] };
    d.classList.add("pml-widget", "pml-" + n.tag);
    if (skinned) {
      Object.assign(d.style, { boxShadow: "none", borderRadius: "0", background: "none" });
      const [il, it] = inner;
      if (kind === "radio" || kind === "checkbox") {
        // Fixed art at native size; checked = rect 1, disabled = 2 (INFERRED).
        const on = a.checked !== undefined && a.checked !== "0";
        parts = [{ row, w: d._pmlW, h: d._pmlH, fixedState: s === 2 ? 2 : on ? 1 : 0 }];
      } else {
        parts = [{ row, w: d._pmlW, h: d._pmlH }];
        if (row === ROW.popupfield) {
          // the popup's arrow button at the right, centred (INFERRED)
          const b = skinRec(ROW.popupbutton, skin), br = b && b.rects[0];
          if (br) parts.push({ row: ROW.popupbutton, x: il + w - br[2] - 2, y: it + Math.trunc((h - br[3]) / 2), w: br[2], h: br[3] });
        }
      }
      addSkin(d, skin, parts, rgba(a.skincolor), s);
    } else {
      const fill = rgba(a.skincolor) || rgba(a.bgcolor);
      if (visible(fill)) d.style.background = css(fill);
    }
    d._pmlKind = kind;
    let value = a.value || "";
    if (kind === "select") {
      const opts = n.children.filter((c) => c.tag === "option").map((o) => decodeEntities(textOf(o)).trim());
      value = row === ROW.selectlist ? opts.join("&br;") : opts[0] || "";
    }
    if (kind === "textarea") value = decodeEntities(textOf(n)).trim() || value;
    if (kind === "radio" || kind === "checkbox") return;
    const host = skinned ? innerBox(d, inner[0], inner[1], w, h) : d;
    d._pmlField = { x: inner[0], y: inner[1], w, h };
    const multi = kind === "textarea" || row === ROW.selectlist;
    const draw = (v) => {
      host.querySelectorAll(":scope > canvas.pml-text").forEach((c) => c.remove());
      if (a.type === "password") v = "*".repeat(Math.min(v.length || 0, 12));
      if (!v) return;
      if (kind === "button") {
        // caption: horizontally per `align`, always vertically centred (2.9)
        textCanvas(host, inlineItems(v, ctx, st), st, ctx, { w, h, mode: 3,
          align: a.align || "center", valign: "middle", nowrap: true });
      } else {
        const pad = [4, multi ? 4 : 0, row === ROW.popupfield ? 26 : 4, multi ? 4 : 0];
        textCanvas(host, inlineItems(v, ctx, st), st, ctx, { w, h, mode: 3, margin: pad,
          align: a.align || "left", valign: multi ? "top" : "middle", nowrap: !multi });
      }
    };
    d._pmlCaption = (v) => { draw(String(v)); drawPendingImages(ctx); };
    draw(value);
  }

  // A 16px vertical scrollbar from the skin's SBV_TextBox parts. The layout
  // is INFERRED (spec 6): track = rect 7 stretched, arrows = rects 0 and 2,
  // thumb = rects 4..6 as a vertical 3-slice sized to the visible share.
  function scrollbar(d, pane, x, h, contentH, skin) {
    const rec = skinsReady() && skinRec(ROW.sbvTextbox, skin);
    const c = el("canvas", "pml-sbar");
    c.width = 16; c.height = Math.max(1, h);
    Object.assign(c.style, { position: "absolute", left: x + "px", top: 0 });
    d.appendChild(c);
    const paint = () => {
      const g = c.getContext("2d");
      g.clearRect(0, 0, 16, h);
      if (!rec || !FONT.sheets[rec.file]) {
        g.fillStyle = "rgba(255,255,255,.25)";
        g.fillRect(4, 0, 8, h);
        return;
      }
      const R = rec.rects, put = (r, dy, dh) => r && dh > 0 && g.drawImage(cut(rec.file, r), 0, 0, r[2], r[3], 0, dy, 16, dh);
      put(R[7], 0, h);
      put(R[0], 0, 16);
      put(R[2], h - 16, 16);
      const track = h - 32, th = Math.max(18, Math.round(track * h / contentH));
      const ty = 16 + Math.round((track - th) * (pane.scrollTop / Math.max(1, contentH - h)));
      put(R[4], ty, R[4][3]);
      put(R[5], ty + R[4][3], th - R[4][3] - R[6][3]);
      put(R[6], ty + th - R[6][3], R[6][3]);
    };
    paint();
    pane.addEventListener("scroll", paint);
  }

  // A content file (records, no layout) is shown as its host page would show
  // the text: full width, in the file's own styles.
  function documentView(stage, ctx) {
    const keys = Object.keys(ctx.records);
    const doc = el("div", "pml-doc");
    let any = false;
    const seen = new Set();
    const st = { ...DEFAULT_STYLE, face: 0, wface: widthFace(0), w: 15, h: 15, prop: true,
                 vspacing: 4, fill: { r: 35, g: 32, b: 26, a: 1 } };
    for (const k of keys) {
      const recs = ctx.records[k];
      if (seen.has(recs)) continue;
      seen.add(recs);
      recs.forEach((rec, i) => {
        if (!rec.trim()) return;
        any = true;
        const lab = el("div", "pml-doc-label");
        lab.textContent = recs.length > 1 ? `${k}[${i}]` : k;
        doc.appendChild(lab);
        const body = el("div", "pml-doc-body");
        doc.appendChild(body);
        const r = textCanvas(body, inlineItems(rec, ctx, st), st, ctx, { w: STAGE_W - 40, mode: 1 });
        body.style.height = r.h + "px";
      });
    }
    if (any) stage.appendChild(doc);
    return any;
  }

  // ======================================================================
  // entry point
  // ======================================================================
  /**
   * Draw `pmlText` into `stage` (a 640x480 element). `base` is the page's
   * www-relative path, so relative art resolves. opts: {reveal: name|"all"}.
   * Returns a summary of what was drawn.
   */
  function render(pmlText, stage, base, opts) {
    opts = opts || {};
    if (FONT.state === "idle" || FONT.state === "loading") {
      // Draw once the Viewer's font is in; the caller gets this pass's summary.
      loadFont().then(() => render(pmlText, stage, base, opts))
        .then((r) => opts.onRedraw && opts.onRedraw(r));
    }
    artBase = base || "";
    stage.innerHTML = "";
    stage.classList.add("pml-stage");
    stage.style.backgroundImage = "";
    stage.style.backgroundColor = "";
    const root = parse(pmlText);
    const ctx = {
      styles: {}, styleCache: new Map(), records: {}, inlineimgs: {}, title: "", layers: [], nodes: [],
      reveal: opts.reveal || null, pending: [], onMissing: opts.onMissing, disabled: 0, docSkin: 0,
      interactive: !!opts.interactive, hideNode: null, revealed: new Set(), timers: [],
      stats: { placed: 0, standIn: 0, varErrors: 0, unresolved: 0, tags: {},
               unsupported: new Set(), missingStyles: new Set(), missingData: new Set() },
    };
    collect(root, ctx);
    let body = null;
    (function find(nd) { for (const c of nd.children || []) { if (c.tag === "body") { body = c; return; } find(c); } })(root);
    if (body) {
      const bgc = rgba(body.attrs.altbgcolor) || rgba(body.attrs.bgcolor);
      if (visible(bgc)) stage.style.backgroundColor = css(bgc);
      if (body.attrs.background && isArt(body.attrs.background)) {
        const u = artUrl(body.attrs.background);
        if (u) {
          stage.style.backgroundImage = `url("${u}")`;
          stage.style.backgroundRepeat = "no-repeat";
        }
      }
      ctx.docSkin = skinId(body.attrs.skin, 0);
    }
    const layer = el("div", "pml-root");
    layer._pmlW = STAGE_W; layer._pmlH = STAGE_H;
    layer.style.overflow = "hidden";                    // the 640x480 stage clip
    stage.appendChild(layer);
    renderChildren((body || root).children, layer, ctx);
    drawPendingImages(ctx);

    let mode = body ? "page" : "layout";
    if (!ctx.stats.placed) {
      stage.innerHTML = "";
      mode = documentView(stage, ctx) ? "document" : "empty";
    }
    const recordCount = Object.values(ctx.records).reduce((n, r) => n + r.filter((x) => x.trim()).length, 0);
    stage._pmlNodes = ctx.nodes;
    stage._pmlCtx = ctx;
    stage._pmlRoot = root;
    stage._pmlBody = body;
    return {
      mode, title: ctx.title, placed: ctx.stats.placed, records: recordCount,
      styles: Object.keys(ctx.styles).length, hasBody: !!body,
      layers: ctx.layers, reveal: ctx.reveal, fontReady: FONT.state === "ready",
      standIn: ctx.stats.standIn, varErrors: ctx.stats.varErrors,
      unresolved: ctx.stats.unresolved, tags: ctx.stats.tags,
      unsupported: [...ctx.stats.unsupported],
      missingStyles: [...ctx.stats.missingStyles],
      missingData: [...ctx.stats.missingData],
    };
  }

  /** The parsed node behind a drawn element (for the inspector). */
  function nodeAt(stage, elem) {
    const e = elem && elem.closest("[data-pml-idx]");
    return e && stage._pmlNodes ? { node: stage._pmlNodes[+e.dataset.pmlIdx], el: e } : null;
  }

  /**
   * Repaint a drawn element in an interaction state, for a runtime on top of
   * this renderer. Skinned widgets and sheets take the skin state index
   * (0 normal, 1 focused/pressed, 2 disabled, 3 editing/hover); link images
   * take the `.ang` sequence (0 normal, 3 focused, 2 disabled). Returns false
   * when the element has nothing state-dependent.
   */
  function setState(elem, state) {
    const d = elem && elem.closest("[data-pml-idx]");
    if (!d) return false;
    if (d._pmlSkin) return paintSkin(d, state);
    if (d._pmlArt && /\.ang/i.test(artSrc(d._pmlArt))) return setImage(d, { seq: state });
    return false;
  }
  const artSrc = (art) => String(art.src).split(",")[art.index || 0].trim();

  /**
   * Change what a drawn image shows: `index` picks one of the sources of a
   * comma-separated src, `seq` the `.ang` sequence. Returns false when the
   * element is not an image or the index is out of range.
   */
  function setImage(elem, opt) {
    const d = elem && elem.closest("[data-pml-idx]");
    if (!d || !d._pmlArt) return false;
    const art = d._pmlArt;
    const list = String(art.src).split(",");
    if (opt.index !== undefined) {
      if (opt.index < 0 || opt.index >= list.length) return false;
      art.index = opt.index;
    }
    if (opt.seq !== undefined) art.seq = opt.seq;
    const stage = d.closest(".pml-stage");
    const ctx = (stage && stage._pmlCtx) || { onMissing: null };
    return showArt(d, artSrc(art), art.seq || 0, art.w, art.h, ctx);
  }

  /** Replace an image's caption or a form control's shown value. */
  function setCaption(elem, text) {
    const d = elem && elem.closest("[data-pml-idx]");
    if (!d || !d._pmlCaption) return false;
    d._pmlCaption(String(text));
    return true;
  }

  /** Redraw a <text> in another named style, or with new text (style undefined keeps it). */
  function setTextStyle(elem, style, text) {
    const d = elem && elem.closest("[data-pml-idx]");
    if (!d || !d._pmlRestyle) return false;
    d._pmlRestyle(style, text);
    return true;
  }

  global.PML = { render, parse, ready: loadFont, nodeAt, decodeEntities, setState, setImage,
                 setCaption, setTextStyle };
})(window);
