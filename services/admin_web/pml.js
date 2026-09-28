// pml.js -- renders PlayOnline Viewer PML into a 640x480 stage for the admin
// preview.
//
// Text is drawn with the Viewer's OWN font data, extracted by
// tools/make_pml_fonts.py: the glyph atlas (system/font/polfnt_00_8bit.png, JIS
// X 0208 order, 16px cells) and the seven per-face advance tables from
// common/ppfont.bin. Widths scale as advance * size / 15.5 and a line is
// size * 1.2 tall plus the style's vspacing -- the model tools/pmlfit.py
// calibrated against SE's pages. What is still approximate, and waits on
// reverse-engineering app.dll's CPmlText / CPmlSkin code: how `size=` resamples
// the 16px cells, which glyphs faces 3-7 use for plain ASCII, and the skins
// (sprite sheets whose part coordinates live in the client), which are drawn
// as plain boxes.
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
    mdash: " - ", ndash: "-", eacute: "é", yen: "¥",
    middot: "·", bull: "•",
  };
  function decodeEntities(s) {
    return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
      if (e[0] === "#") {
        const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
        return isFinite(n) ? String.fromCodePoint(n) : m;
      }
      const v = NAMED[e.toLowerCase()];
      return v === undefined ? m : v;
    });
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
      stack[stack.length - 1].children.push(node);
      if (!(tok.endsWith("/>") || VOID.has(name))) stack.push(node);
    }
    return root;
  }

  // ======================================================================
  // the Viewer's font
  // ======================================================================
  const FONT = { state: "idle", widths: null, jis: null, map: null, mask: null,
                 tints: new Map(), ink: new Map(), em: 15.5, waiters: [] };

  function loadFont() {
    if (FONT.state !== "idle") return FONT.promise;
    FONT.state = "loading";
    FONT.promise = (async () => {
      try {
        const [meta, img] = await Promise.all([
          fetch("/static/pmlfont/font.json").then((r) => r.json()),
          new Promise((res, rej) => {
            const im = new Image();
            im.onload = () => res(im);
            im.onerror = rej;
            im.src = "/static/pmlfont/glyphs00.png";
          }),
        ]);
        // grayscale coverage -> white with alpha = coverage
        const c = document.createElement("canvas");
        c.width = img.width; c.height = img.height;
        const g = c.getContext("2d");
        g.drawImage(img, 0, 0);
        const d = g.getImageData(0, 0, c.width, c.height);
        for (let i = 0; i < d.data.length; i += 4) {
          d.data[i + 3] = d.data[i];
          d.data[i] = d.data[i + 1] = d.data[i + 2] = 255;
        }
        g.putImageData(d, 0, 0);
        FONT.mask = c;
        FONT.widths = meta.widths;
        FONT.em = meta.em || 15.5;
        FONT.map = new Map();
        for (let i = 0; i < meta.jis.length; i++) {
          const ch = meta.jis[i];
          if (ch !== "\0" && !FONT.map.has(ch)) FONT.map.set(ch, i);
        }
        FONT.state = "ready";
      } catch (e) {
        FONT.state = "failed";
      }
    })();
    return FONT.promise;
  }

  // The four ASCII marks whose JIS row-1 glyph does not decode to the U+FFxx
  // twin (the atlas is read as EUC-JP): a news rule of "-" drew 120 stand-ins.
  const ASCII_JIS = { "-": "−", "'": "’", "\"": "”", "~": "〜" };
  function glyphIndex(ch) {
    if (!FONT.map) return -1;
    const o = ch.codePointAt(0);
    if (o >= 0x21 && o <= 0x7e) {             // ASCII lives at its full-width twin
      const i = FONT.map.get(ASCII_JIS[ch] || String.fromCharCode(o + 0xfee0));
      if (i !== undefined) return i;
    }
    const i = FONT.map.get(ch);
    return i === undefined ? -1 : i;
  }

  function faceTable(face) {
    const f = face === 7 ? 6 : face;
    return FONT.widths && FONT.widths[f >= 0 && f < FONT.widths.length ? f : 6];
  }

  // Advance in stage pixels.
  function advance(ch, st) {
    const o = ch.codePointAt(0);
    const tab = faceTable(st.face);
    let w;
    if (tab && o >= 0x20 && o < 0x100) w = tab[o - 0x20];
    else w = 16;                                // a full-width cell
    return (w + st.spacing) * st.size / FONT.em;
  }

  // The ink columns of a glyph cell (proportional glyphs are pasted by ink).
  function inkOf(idx) {
    let v = FONT.ink.get(idx);
    if (v) return v;
    const g = FONT.mask.getContext("2d");
    const x0 = (idx % 32) * 16, y0 = Math.floor(idx / 32) * 16;
    const d = g.getImageData(x0, y0, 16, 16).data;
    let lo = 16, hi = -1;
    for (let x = 0; x < 16; x++) {
      for (let y = 0; y < 16; y++) {
        if (d[(y * 16 + x) * 4 + 3] > 8) { if (x < lo) lo = x; if (x > hi) hi = x; break; }
      }
    }
    v = hi < 0 ? [0, 0] : [lo, hi - lo + 1];
    FONT.ink.set(idx, v);
    return v;
  }

  // A 16x16 glyph tinted to `css`, cached.
  function tinted(idx, css) {
    const key = idx + "|" + css;
    let c = FONT.tints.get(key);
    if (c) return c;
    c = document.createElement("canvas");
    c.width = c.height = 16;
    const g = c.getContext("2d");
    g.drawImage(FONT.mask, (idx % 32) * 16, Math.floor(idx / 32) * 16, 16, 16, 0, 0, 16, 16);
    g.globalCompositeOperation = "source-in";
    g.fillStyle = css;
    g.fillRect(0, 0, 16, 16);
    if (FONT.tints.size > 6000) FONT.tints.clear();
    FONT.tints.set(key, c);
    return c;
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
  function pair(v) {
    const [f, o] = String(v || "").split(",");
    return [rgba(f), rgba(o)];
  }
  // SE evaluates arithmetic in numeric attributes: pos="98+6,117". Only digits,
  // operators and brackets get this far, so evaluating them is safe.
  function num(x) {
    x = String(x).trim();
    if (/^-?\d+(\.\d+)?$/.test(x)) return parseFloat(x);
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
  const unresolved = (v) => /\$[A-Za-z_]/.test(String(v || ""));

  const DEFAULT_STYLE = { size: 15, face: 6, fill: { r: 255, g: 255, b: 255, a: 1 },
                          outline: null, spacing: 0, vspacing: 0, bold: false };
  // Styles a page uses but never defines (C17_2, W19, C19 on SE's story page)
  // come from the Viewer's own stylesheet, which the mirror does not have. The
  // ones SE does define follow one scheme -- letter = colour, digits = size:
  // C = #333333 / #101010, W = #f0f0f0, B = #000000 -- so read the name the
  // same way instead of drawing 15px white.
  const NAMED_COLOR = { C: "#333333ff", W: "#f0f0f0ff", B: "#000000ff" };
  function namedStyle(name) {
    const m = /^([CWB])(\d{2})(?:_\d)?$/.exec(name || "");
    return m ? { size: m[2], face: "2", proportional: "1", color: NAMED_COLOR[m[1]] } : null;
  }
  function styleOf(ctx, name) {
    let s = ctx.styles[name];
    if (!s) {
      if (name) ctx.stats.missingStyles.add(name);
      s = namedStyle(name);
      if (!s) return DEFAULT_STYLE;
    }
    const [fill, outline] = pair(s.color);
    return {
      size: parseFloat(s.size) || 15,
      face: s.face !== undefined && s.face !== "" ? parseInt(s.face, 10) : 6,
      fill: fill || DEFAULT_STYLE.fill, outline: visible(outline) ? outline : null,
      spacing: parseFloat(s.spacing) || 0, vspacing: parseFloat(s.vspacing) || 0,
      bold: s.bold === "1", name,
    };
  }

  // ======================================================================
  // inline markup -> items
  // ======================================================================
  // &br;  &style=N; .. &style;  &image=N;  &pre=N;  &pos=X;  &li; / &li=M;
  // &sp=N;  &a=URL; .. &a;  &size=W,H; .. &size;  &var=$x; (unresolved)
  function inlineItems(text, ctx, baseStyle) {
    const items = [];
    const RE = /&(br|style|image|pre|pos|var|li|sp|size|a|table|calc)(?:=([^;]*))?;/g;
    let at = 0, m, style = baseStyle, link = null;
    // `&pre=1;` switches the text to preformatted: from there on a newline is
    // a line break. Otherwise a newline is only whitespace in the source file,
    // and the box does the wrapping. SE writes `&pre=1;` (or `&pre=01;`) and
    // nothing else, ~4,000 times; newsgen's Information page and the help
    // manual both rely on it for their line breaks.
    let pre = false;
    const pushText = (s) => {
      if (!s) return;
      s = s.replace(/\t+/g, "");
      const parts = pre ? s.split(/\r?\n/) : [s.replace(/\s*[\r\n]+\s*/g, " ")];
      parts.forEach((part, i) => {
        if (i) items.push({ t: "br" });
        const d = decodeEntities(part);
        if (d) items.push({ t: "text", s: d, style, link });
      });
    };
    while ((m = RE.exec(text))) {
      pushText(text.slice(at, m.index));
      at = m.index + m[0].length;
      const kind = m[1], arg = m[2];
      if (kind === "br") items.push({ t: "br" });
      else if (kind === "style") style = arg === undefined ? baseStyle : styleOf(ctx, arg);
      else if (kind === "image") items.push({ t: "img", decl: ctx.inlineimgs[arg], name: arg });
      else if (kind === "pre") pre = parseInt(arg || "1", 10) !== 0;
      else if (kind === "pos") items.push({ t: "pos", x: +arg || 0 });
      else if (kind === "li") {
        // `&li=pb;` names an <inlineimg> to use as the bullet; otherwise the
        // argument is the bullet text itself.
        if (arg !== undefined && ctx.inlineimgs[arg]) items.push({ t: "li", decl: ctx.inlineimgs[arg], name: arg });
        else items.push({ t: "li", mark: arg === undefined ? "・" : decodeEntities(arg) });
      }
      else if (kind === "sp") items.push({ t: "sp", px: +arg || 0 });
      else if (kind === "a") link = arg === undefined ? null : arg;
      else if (kind === "var") {
        items.push({ t: "text", s: "(Variable error)", style, link, error: true });
        ctx.stats.varErrors++;
      }
      // &size= and &table= change nothing we can draw yet.
    }
    pushText(text.slice(at));
    return items;
  }

  const isCJK = (o) => o >= 0x2e80;

  // Break items into lines no wider than `maxW` (0 = no wrapping).
  //
  // Illustrations (<inlineimg align=...>): `left`/`right` FLOAT beside the text
  // -- the manual pages put a screenshot on the right with the words beside it
  // -- and `center` takes a line of its own. Icons (skip/offset) sit in the text.
  function layout(items, maxW, baseStyle) {
    const lines = [];
    const floats = [];                          // {side, w, bottom}
    const placed = [];                          // floated images, absolute
    let line = null, indent = 0, yAcc = 0;
    const lineH = (st) => st.size * 1.2 + st.vspacing;
    const limits = () => {
      let off = 0, lim = maxW;
      for (const f of floats) {
        if (yAcc >= f.bottom) continue;
        if (maxW) lim -= f.w;
        if (f.side === "left") off += f.w;
      }
      return [off, lim];
    };
    const newLine = () => {
      if (line) yAcc += line.h;
      const [off, lim] = limits();
      line = { parts: [], w: indent, h: lineH(baseStyle), x0: indent, off, lim };
      lines.push(line);
    };
    newLine();
    const fits = (w) => !line.lim || line.w + w <= line.lim;
    const place = (part, w, h) => {
      part.x = line.w;
      line.parts.push(part);
      line.w += w;
      if (h > line.h) line.h = h;
    };
    for (const it of items) {
      if (it.t === "br") { indent = 0; newLine(); continue; }
      if (it.t === "sp") { place({ t: "gap" }, it.px, 0); continue; }
      if (it.t === "pos") { if (it.x > line.w) line.w = it.x; continue; }
      if (it.t === "li") {
        if (it.decl) {
          const [iw, ih] = nums(it.decl.size, 2);
          const skip = parseFloat(it.decl.skip) || (iw || 12) + 4;
          place({ t: "img", decl: it.decl, name: it.name, w: iw || 12, h: ih || 12,
                  oy: parseFloat(it.decl.offset) || 0 }, skip, 0);
        } else {
          const w = [...it.mark].reduce((a, c) => a + advance(c, baseStyle), 0);
          place({ t: "run", s: it.mark, style: baseStyle }, w, lineH(baseStyle));
        }
        indent = line.w;
        continue;
      }
      if (it.t === "img") {
        const d = it.decl || {};
        const [w, h] = it.decl ? nums(d.size, 2) : [16, 16];
        if (d.align === "left" || d.align === "right") {
          const m = nums(d.margin, 4);
          const top = yAcc + (line.parts.length ? line.h : 0);
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
        if (!fits(skip) && line.parts.length) newLine();
        place({ t: "img", decl: it.decl, name: it.name, w: w || 16, h: h || 16, oy: parseFloat(d.offset) || 0 }, skip, 0);
        continue;
      }
      // text: split into words (Latin) and single characters (CJK)
      const st = it.style, h = lineH(st);
      const tokens = it.s.match(/[⺀-￿]|\s+|[^\s⺀-￿]+/g) || [];
      for (const tok of tokens) {
        const w = [...tok].reduce((a, c) => a + advance(c, st), 0);
        const space = /^\s+$/.test(tok);
        if (!fits(w) && line.parts.length && !space) newLine();
        if (space && line.parts.length === 0 && lines.length > 1) continue;  // no leading space after a wrap
        if (line.lim && w > line.lim && !space && !isCJK(tok.codePointAt(0))) {
          // a word wider than the room: break it by character
          for (const c of tok) {
            const cw = advance(c, st);
            if (!fits(cw) && line.parts.length) newLine();
            place({ t: "run", s: c, style: st, link: it.link, error: it.error }, cw, h);
          }
          continue;
        }
        place({ t: "run", s: tok, style: st, link: it.link, error: it.error }, w, h);
      }
    }
    // trailing spaces do not count toward alignment
    for (const l of lines) {
      const last = l.parts[l.parts.length - 1];
      if (last && last.t === "run" && /^\s+$/.test(last.s)) l.w = last.x;
    }
    // a float taller than the text still takes up room
    const bottom = Math.max(0, ...floats.map((f) => f.bottom));
    const textH = lines.reduce((a, l) => a + l.h, 0);
    if (bottom > textH) lines[lines.length - 1].h += bottom - textH;
    lines.floats = placed;
    return lines;
  }

  // Draw laid-out lines onto a canvas context.
  function drawLines(g, lines, box, align, valign, ctx) {
    const total = lines.reduce((a, l) => a + l.h, 0);
    const y0 = valign === "middle" || valign === "center" ? (box.h - total) / 2
      : valign === "bottom" ? box.h - total : 0;
    let y = y0;
    const links = [];
    for (const f of lines.floats || []) {
      ctx.pending.push({ decl: f.decl, name: f.name, x: f.x, y: y0 + f.y, w: f.w, h: f.h, g });
    }
    for (const l of lines) {
      const room = l.lim || box.w;
      const x = (l.off || 0) + (align === "center" ? (room - l.w) / 2 + l.x0 / 2
        : align === "right" ? room - l.w : 0);
      for (const p of l.parts) {
        if (p.t === "run") {
          const px = drawRun(g, p.s, p.style, x + p.x, y + (l.h - p.style.size * 1.2) / 2, ctx, p.error);
          if (p.link) links.push({ href: p.link, x: x + p.x, y, w: px - (x + p.x), h: l.h });
        } else if (p.t === "img" && p.block) {
          const [ml, mt, mr] = p.m;
          ctx.pending.push({ decl: p.decl, name: p.name, x: (box.w - p.w) / 2 + ml - mr, y: y + mt, w: p.w, h: p.h, g });
        } else if (p.t === "img") {
          ctx.pending.push({ decl: p.decl, name: p.name, x: x + p.x, y: y + (l.h - p.h) / 2 + (p.oy || 0), w: p.w, h: p.h, g });
        }
      }
      y += l.h;
    }
    return links;
  }

  function drawRun(g, s, st, x, y, ctx, error) {
    const scale = st.size / 16;
    const fill = error ? "rgba(255,90,90,1)" : css(st.fill);
    const out = st.outline && css(st.outline);
    const top = y + (st.size * 1.2 - st.size) / 2;
    let pen = x;
    for (const ch of s) {
      const adv = advance(ch, st);
      const o = ch.codePointAt(0);
      if (ch !== " " && ch !== "　") {
        const idx = FONT.state === "ready" ? glyphIndex(ch) : -1;
        if (idx >= 0) {
          let sx = 0, sw = 16, dx = pen;
          if (o < 0x100) {                     // proportional: paste the ink
            const [lo, w] = inkOf(idx);
            sx = lo; sw = Math.max(w, 1); dx = pen + scale;
          }
          const draw = (color, ox, oy) => g.drawImage(tinted(idx, color), sx, 0, sw, 16,
            Math.round(dx + ox), Math.round(top + oy), sw * scale, 16 * scale);
          if (out) {
            for (const [ox, oy] of [[-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]]) draw(out, ox, oy);
          }
          draw(fill, 0, 0);
          if (st.bold) draw(fill, 1, 0);
        } else {
          // not in the Viewer's atlas: a browser font stands in, and is counted
          ctx.stats.standIn++;
          g.font = `${Math.round(st.size)}px sans-serif`;
          g.textBaseline = "top";
          if (out) { g.strokeStyle = out; g.lineWidth = 2; g.strokeText(ch, pen, top); }
          g.fillStyle = fill;
          g.fillText(ch, pen, top);
        }
      }
      pen += adv;
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
    if (node.tag === "style" && a.name) ctx.styles[a.name] = a;
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

  function box(node, parent, ctx, x, y, w, h) {
    const d = el("div", "pml-el");
    d.style.left = x + "px";
    d.style.top = y + "px";
    if (w) d.style.width = w + "px";
    if (h) d.style.height = h + "px";
    const a = node.attrs;
    if (a.zindex !== undefined && a.zindex !== "") d.style.zIndex = parseInt(a.zindex, 10) || 0;
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

  // A canvas that fills `host` and draws `items` into it.
  function textCanvas(host, w, h, items, st, align, valign, ctx, pad) {
    const [pl, pt, pr, pb] = pad || [0, 0, 0, 0];
    const iw = Math.max(0, w - pl - pr);
    const lines = layout(items, iw, st);
    const natural = lines.reduce((a, l) => a + l.h, 0);
    const ih = h ? Math.max(0, h - pt - pb) : natural;
    const cw = Math.max(1, Math.ceil(w || Math.max(...lines.map((l) => l.w), 1) + pl + pr));
    const ch = Math.max(1, Math.ceil(h || natural + pt + pb));
    const c = el("canvas", "pml-text");
    c.width = cw; c.height = ch;
    const g = c.getContext("2d");
    g.imageSmoothingEnabled = st.size !== 16;
    g.translate(pl, pt);
    const links = drawLines(g, lines, { w: iw || cw, h: ih }, align, valign, ctx);
    host.appendChild(c);
    for (const L of links) {
      const a = el("div", "pml-link pml-inline-link");
      Object.assign(a.style, { left: L.x + pl + "px", top: L.y + pt + "px", width: L.w + "px", height: L.h + "px" });
      a.dataset.href = L.href;
      host.appendChild(a);
    }
    return { canvas: c, natural };
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

  function renderChildren(children, parent, ctx) {
    for (const n of children) renderNode(n, parent, ctx);
  }

  function renderNode(n, parent, ctx) {
    if (n.tag === "#text") return;
    const a = n.attrs;

    // show="0": a panel the page reveals later. Offered as a layer instead.
    if (a.show === "0" || (a.show && /\$/.test(a.show))) {
      const name = a.name || `panel ${ctx.layers.length + 1}`;
      if (!ctx.layers.includes(name)) ctx.layers.push(name);
      if (ctx.reveal !== "all" && ctx.reveal !== name) return;
    }

    const [x, y] = nums(a.pos, 2);
    const [w, h] = nums(a.size, 2);

    switch (n.tag) {
      case "head": case "title": case "style": case "inlineimg": case "data":
      case "record": case "meta": case "config": case "bgsound": case "timer":
      case "formaction": case "define": case "array": case "include": case "script":
      case "addmenu": case "addlink": case "multilink": case "download": case "plugin":
      case "hidden": case "option":
        return;

      case "sheet": case "scrollarea": case "systembg": {
        const d = box(n, parent, ctx, x, y, w || STAGE_W, h || STAGE_H);
        d.classList.add("pml-" + n.tag);
        const fill = rgba(a.bgcolor) || rgba(a.skincolor);
        if (visible(fill)) d.style.background = css(fill);
        const bg = a.background || (n.tag === "systembg" ? a.src : null);
        if (bg && isArt(bg)) {
          const u = artUrl(bg);
          if (u) { d.style.backgroundImage = `url("${u}")`; d.style.backgroundRepeat = "no-repeat"; }
        }
        if (a.border === "1") d.style.boxShadow = `inset 0 0 0 1px ${css(rgba(a.bordercolor)) || "rgba(0,0,0,.5)"}`;
        if (a.skin && a.skin !== "0" && !visible(fill)) d.classList.add("pml-skinned");
        let host = d;
        if (n.tag === "scrollarea") {
          d.classList.add(a.vbar === "never" ? "pml-noscroll" : "pml-scroll");
          const [aw, ah] = nums(a.areasize, 2);
          if (ah > (h || 0) || aw > (w || 0)) {
            host = el("div", "pml-area");
            host.style.width = (aw || w) + "px";
            host.style.height = (ah || h) + "px";
            d.appendChild(host);
          }
        }
        renderChildren(n.children, host, ctx);
        return;
      }

      case "text": {
        const st = styleOf(ctx, a.style);
        const d = box(n, parent, ctx, x, y, w, h);
        const bg = rgba(a.bgcolor);
        if (visible(bg)) d.style.background = css(bg);
        const m = nums(a.margin, 4);
        const pad = a.margin ? (String(a.margin).split(",").length === 1 ? [m[0], m[0], m[0], m[0]] : [m[0], m[1], m[2] || m[0], m[3] || m[1]]) : null;
        const text = n.children.filter((c) => c.tag === "#text").map((c) => c.text).join("");
        const items = inlineItems(text.trim(), ctx, st);
        const r = textCanvas(d, w, h, items, st, a.align || "left", a.valign || "top", ctx, pad);
        if (!w) d.style.width = r.canvas.width + "px";
        if (!h) d.style.height = r.canvas.height + "px";
        renderChildren(n.children.filter((c) => c.tag !== "#text"), d, ctx);
        return;
      }

      case "img": {
        const d = box(n, parent, ctx, x, y, w, h);
        d.classList.add("pml-img");
        const seq = /(^|\s)(\d+)/.exec(String(a["sd:sequence"] || ""));
        const url = isArt(a.src) ? artUrl(a.src, /\.ang/i.test(a.src) && seq ? "seq=" + seq[2] : "") : null;
        if (url) {
          const add = (u, first) => {
            const im = el("img");
            im.src = u;
            im.draggable = false;
            if (w) im.style.width = w + "px";
            if (h) im.style.height = h + "px";
            if (first) {
              im.onload = () => {
                if (!w) d.style.width = im.naturalWidth + "px";
                if (!h) d.style.height = im.naturalHeight + "px";
              };
              im.onerror = () => {
                d.classList.add("pml-missing");
                if (!d.title) d.title = "Not in the mirror: " + a.src;
                ctx.onMissing && ctx.onMissing(a.src);
              };
            }
            // images go under any caption canvas already in the box
            d.insertBefore(im, d.querySelector(":scope > canvas"));
          };
          if (/\.ang/i.test(a.src)) {
            loadLayers(url).then((layers) => {
              if (!layers.length) { d.classList.add("pml-missing"); ctx.onMissing && ctx.onMissing(a.src); return; }
              layers.forEach((u, i) => add(u, i === 0));
            });
          } else {
            add(url, true);
          }
        } else if (a.src) {
          d.classList.add("pml-missing");
        }
        if (a.value) {
          const st = styleOf(ctx, a.style);
          textCanvas(d, w, h, inlineItems(a.value, ctx, st), st, "center", "middle", ctx);
        }
        renderChildren(n.children, d, ctx);
        return;
      }

      case "textbox": {
        const st = styleOf(ctx, a.style);
        const d = box(n, parent, ctx, x, y, w, h);
        d.classList.add(a.vbar === "never" ? "pml-noscroll" : "pml-scroll");
        const fill = rgba(a.skincolor) || rgba(a.bgcolor);
        if (visible(fill)) d.style.background = css(fill);
        const recs = ctx.records[(a.ref || "") + (a.sub ? "|" + a.sub : "")] || ctx.records[a.ref || ""];
        const rec = recs ? recs[parseInt(a.index || "0", 10)] || "" : "";
        if (!recs) ctx.stats.missingData.add(a.ref || "?");
        const m = parseFloat(a.margin) || 0;
        // The box keeps its size; the canvas is only as tall as the text. (A
        // min-height on the canvas itself scaled it up, aspect and all: a
        // two-line story drew 3.5x too large.)
        textCanvas(d, w, 0, inlineItems(rec, ctx, st), st, a.align || "left", "top", ctx, [m, m, m, m]);
        return;
      }

      case "input": case "select": case "button": {
        if (a.type === "hidden") return;
        const st = styleOf(ctx, a.style);
        const d = box(n, parent, ctx, x, y, w, h);
        d.classList.add("pml-widget", "pml-" + n.tag);
        const fill = rgba(a.skincolor) || rgba(a.bgcolor);
        if (visible(fill)) d.style.background = css(fill);
        let value = a.value || "";
        if (n.tag === "select") {
          const opt = n.children.find((c) => c.tag === "option");
          value = opt ? decodeEntities(textOf(opt)).trim() : "";
        }
        if (a.type === "password") value = "●".repeat(Math.min(value.length || 6, 12));
        const align = n.tag === "button" ? "center" : "left";
        textCanvas(d, w, h, inlineItems(value, ctx, st), st, align, "middle", ctx, [4, 0, n.tag === "select" ? 18 : 4, 0]);
        return;
      }

      case "hr": {
        const d = box(n, parent, ctx, x, y, w, h || 1);
        const c = rgba(a.skincolor) || rgba(a.color) || { r: 200, g: 200, b: 200, a: 0.6 };
        d.style.background = css(c);
        return;
      }

      case "table": case "inlinetable": {
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
            cell.appendChild(inner);
            renderChildren(td.children, inner, ctx);
            const t = td.children.find((c) => c.tag === "#text");
            if (t) {
              const st = styleOf(ctx, td.attrs.style || a.style);
              const r = textCanvas(inner, tw - 2 * padding, 0, inlineItems(t.text.trim(), ctx, st), st,
                td.attrs.align || "left", "top", ctx);
              if (!rh) rowH = Math.max(rowH, r.natural + 2 * padding);
            }
            rx += tw + spacing;
          }
          rows.length && cells.forEach(() => {});
          ry += (rowH || 20) + spacing;
        }
        if (!h) d.style.height = ry + "px";
        return;
      }

      default: {
        // Containers without drawing of their own (form, if leftovers, body...)
        if (n.children.length) {
          if (a.pos) {
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

  // A content file (records, no layout) is shown as its host page would show
  // the text: full width, in the file's own styles.
  function documentView(stage, ctx) {
    const keys = Object.keys(ctx.records).filter((k) => !k.includes("|") || true);
    const doc = el("div", "pml-doc");
    let any = false;
    const seen = new Set();
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
        textCanvas(body, STAGE_W - 40, 0, inlineItems(rec, ctx, { ...DEFAULT_STYLE, fill: { r: 35, g: 32, b: 26, a: 1 } }), { ...DEFAULT_STYLE }, "left", "top", ctx);
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
      styles: {}, records: {}, inlineimgs: {}, title: "", layers: [], nodes: [],
      reveal: opts.reveal || null, pending: [], onMissing: opts.onMissing,
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
    }
    const layer = el("div", "pml-root");
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

  global.PML = { render, parse, ready: loadFont, nodeAt, decodeEntities };
})(window);
