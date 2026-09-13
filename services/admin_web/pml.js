// pml.js -- a client-accurate-ish PML renderer for the admin preview.
//
// It approximates how the PlayOnline Viewer lays out a PML page so registration
// (and any) pages can be iterated without a full client round-trip. It is NOT a
// byte-perfect emulation of SE's bitmap-font engine; it reproduces what we
// actually need to catch: absolute pos/size layout in a 640-wide stage, <style>
// fonts/colors, <input> boxes, <scrollarea>/<sheet> fills, <img> art, <textbox>
// bound text, and text wrapping within a field's width.
//
// PML is XML-ish but NOT well-formed (unclosed <input>/<img>/<meta>/<style>,
// bare & in hrefs, `&var=x;` entities, `<! ... >` comments), so a lenient custom
// parser is used rather than DOMParser.

(function (global) {
  "use strict";

  // Tags that never have a child body in the pages we render -- treated as
  // self-closing whether or not they carry a trailing slash.
  const VOID = new Set([
    "input", "img", "meta", "formaction", "define", "style", "br", "bgsound",
    "include", "timer", "textbox", "area", "addmenu", "addlink", "config",
    "plugin", "hidden", "bar", "systembg", "inlineimg", "multilink"
  ]);

  function stripComments(s) {
    // <!-- ... --> and SE's <! ... > form.
    return s.replace(/<!--[\s\S]*?-->/g, "").replace(/<![^>]*>/g, "");
  }

  function parseAttrs(s) {
    const attrs = {};
    // key="v"  |  key='v'  |  key=v  |  key
    const re = /([\w:-]+)\s*(?:=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let m;
    while ((m = re.exec(s))) {
      const k = m[1].toLowerCase();
      const v = m[3] !== undefined ? m[3]
        : m[4] !== undefined ? m[4]
          : m[5] !== undefined ? m[5] : "";
      attrs[k] = v;
    }
    return attrs;
  }

  // Build a lightweight tree: {tag, attrs, children:[], text}
  function parse(src) {
    src = stripComments(src);
    const root = { tag: "#root", attrs: {}, children: [] };
    const stack = [root];
    const tokRe = /<\/?[\w:-]+[^>]*?>|[^<]+/g;
    let m;
    while ((m = tokRe.exec(src))) {
      const tok = m[0];
      if (tok[0] !== "<") {
        // kept RAW -- `inlineText` needs the &br;/&style= markers, and decodes
        // the character entities itself once the markers are split out.
        const text = tok;
        if (text.trim() !== "") {
          // children:[] so the recursive walkers (collect/find) can .forEach it
          stack[stack.length - 1].children.push({ tag: "#text", text: text, children: [] });
        }
        continue;
      }
      if (tok.startsWith("</")) {
        const name = tok.slice(2).replace(/[\s>].*$/, "").toLowerCase();
        // pop to the matching open (tolerate mismatches)
        for (let i = stack.length - 1; i > 0; i--) {
          if (stack[i].tag === name) { stack.length = i; break; }
        }
        continue;
      }
      const name = tok.slice(1).replace(/[\s/>].*$/, "").toLowerCase();
      const attrs = parseAttrs(tok.slice(1 + name.length).replace(/\/?>$/, ""));
      const node = { tag: name, attrs: attrs, children: [] };
      stack[stack.length - 1].children.push(node);
      const selfClose = tok.endsWith("/>") || VOID.has(name);
      if (!selfClose) stack.push(node);
    }
    return root;
  }

  // CHARACTER entities only. The layout markers (&br; &style= &image= &pre=)
  // are deliberately left in the text: they are the formatting of a page's body
  // copy, and stripping them here -- which is what used to happen -- is why the
  // topics pages previewed as one unbroken wall of text with `&br;` visible in
  // it. `inlineText` interprets them, and runs this per plain chunk afterwards
  // so an `&amp;br;` in the copy stays literal.
  function decodeEntities(s) {
    return s
      .replace(/&quot;/g, '"').replace(/&trade;/g, "™")
      .replace(/&nbsp;/g, " ")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }

  // Render SE's inline body markup into `parent`:
  //   &br;                line break
  //   &style=NAME; .. &style;   a run in that <style>
  //   &image=NAME;        the <inlineimg name=NAME> declared in the file
  //   &pre=N;             indent this line by N characters
  //   &var=$x;            a value the server could not resolve -- dropped
  // Built per call: a /g/ regex carries `lastIndex` between calls, so a shared
  // one would start the second record part-way through the first one's text.
  function inlineText(parent, text, ctx) {
    const MARKER = /&(br|style|image|pre|pos|var)(?:=([^;]*))?;/g;
    let at = 0, m, span = null;
    const put = (node) => (span || parent).appendChild(node);
    const text_ = (s) => { if (s) put(document.createTextNode(decodeEntities(s))); };
    while ((m = MARKER.exec(text))) {
      text_(text.slice(at, m.index));
      at = m.index + m[0].length;
      const [, kind, arg] = m;
      if (kind === "br") {
        put(el("br"));
      } else if (kind === "style") {
        if (arg === undefined) {           // `&style;` closes the run
          span = null;
        } else {
          span = el("span");
          Object.assign(span.style, styleCss(ctx.styles, arg));
          parent.appendChild(span);
        }
      } else if (kind === "image") {
        put(inlineImage(arg, ctx));
      } else if (kind === "pre") {
        put(document.createTextNode(" ".repeat(Math.min(+arg || 0, 40))));
      }
      // &pos= is a tab stop and &var= an unresolved value: both drop out.
    }
    text_(text.slice(at));
  }

  // `&image=NAME;` names an <inlineimg> the file declares. An unknown name is
  // usually declared by the HOST page (a topics record says `&image=ctgic;` and
  // `topm02.pml` declares it), so show a marker rather than nothing -- the
  // operator needs to see that something sits there.
  function inlineImage(name, ctx) {
    const decl = ctx.inlineimgs[name];
    const url = decl && artUrl(decl.src);
    if (isBitmap(url)) {
      const [w, h] = xy(decl.size);
      const img = el("img");
      img.src = url;
      if (w) img.style.maxWidth = w + "px";
      if (h) img.style.maxHeight = h + "px";
      img.style.verticalAlign = "middle";
      img.onerror = () => img.classList.add("pml-art-missing");
      return img;
    }
    const chip = el("span");
    chip.className = "pml-inline-img";
    chip.textContent = name || "image";
    return chip;
  }

  // "#rrggbbaa" (or with fill,outline pair) -> css rgba, using the fill half.
  function color(c, fallback) {
    if (!c) return fallback || null;
    c = c.split(",")[0].trim();
    const m = /^#?([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(c);
    if (!m) return fallback || null;
    const hex = m[1], a = m[2] ? parseInt(m[2], 16) / 255 : 1;
    const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16),
      b = parseInt(hex.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function isOpaqueish(cssRgba) {
    if (!cssRgba) return false;
    const m = /rgba\([^,]+,[^,]+,[^,]+,([\d.]+)\)/.exec(cssRgba);
    return m ? parseFloat(m[1]) > 0.02 : true;
  }

  function xy(s) {
    const p = String(s || "0,0").split(",");
    return [parseFloat(p[0]) || 0, parseFloat(p[1]) || 0];
  }

  // Resolve a PML art path to an admin-served URL.
  //   file:/ucs/img_s/x.png  ->  /art/ucs/img_s/x.png
  //   file:/img_s/general/x  ->  /art/img_s/general/x
  // `?base=` carries the page being previewed. `file:/` and `/`-absolute paths
  // are rooted at the page's HOST -- and for an overlay page they may fall
  // through to the base tree -- which only the server can see, so it is told
  // where the page came from rather than guessing at a root here. No base (a
  // pasted page, or a generated wizard step) means the server's ART_ROOT.
  let artBase = "";
  // `.ang` animated sprites and unknown types get a placeholder plate; only
  // these draw as a real <img>. Tested on the PATH -- artUrl appends a query.
  function isBitmap(url) {
    return !!url && /\.(png|jpg|gif)$/i.test(url.split("?")[0]);
  }
  function artUrl(src) {
    if (!src) return null;
    let s = src.replace(/^file:\/*/, "");        // strip file: and any slashes
    if (/[$]/.test(s)) return null;              // unevaluated expression
    return "/art/" + s.replace(/^\/+/, "") +
      (artBase ? "?base=" + encodeURIComponent(artBase) : "");
  }

  function el(tag, css) {
    const d = document.createElement(tag);
    if (css) Object.assign(d.style, css);
    return d;
  }

  // Collect <style name=...>, <inlineimg name=...> and
  // <data name=...><record>text</record></data>.
  function collect(node, styles, records, inlineimgs) {
    if (node.tag === "style" && node.attrs.name) {
      styles[node.attrs.name] = node.attrs;
    }
    if (node.tag === "inlineimg" && node.attrs.name) {
      inlineimgs[node.attrs.name] = node.attrs;
    }
    if (node.tag === "data" && node.attrs.name) {
      const recs = [];
      (function walk(n) {
        n.children.forEach((c) => {
          if (c.tag === "record") {
            let t = "";
            (function rt(x) {
              x.children.forEach((y) => {
                if (y.tag === "#text") t += y.text;
                else rt(y);
              });
            })(c);
            recs.push(t.replace(/^\s*"|"\s*$/g, ""));
          } else walk(c);
        });
      })(node);
      records[node.attrs.name] = recs;
    }
    (node.children || []).forEach((c) => collect(c, styles, records, inlineimgs));
  }

  function styleCss(styles, name) {
    const s = styles[name] || {};
    const css = {};
    if (s.size) css.fontSize = (parseFloat(s.size)) + "px";
    css.lineHeight = 1.15;
    css.color = color(s.color, "#ffffff");
    if (s.bold === "1") css.fontWeight = "700";
    // face 0 tends to be the heavier system face; 6 the proportional body face.
    css.fontFamily = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif";
    if (s.face === "0") css.fontWeight = css.fontWeight || "600";
    if (s.proportional === "0") css.fontFamily = "'Consolas', monospace";
    // a subtle outline like the client's glyph edges, so light text stays legible
    if (isOpaqueish(color((s.color || "").split(",")[1]))) {
      css.textShadow = "0 0 1px " + color(s.color.split(",")[1]);
    }
    return css;
  }

  // Render a node's children into `parent` at offset (ox,oy).
  function renderInto(children, parent, ox, oy, ctx) {
    children.forEach((n) => {
      if (n.tag === "#text") return;
      const a = n.attrs || {};

      // `show="0"` is a sheet the client does NOT draw on arrival -- it is a
      // panel some `sd:show=1@name` reveals later, and a page routinely stacks
      // a dozen of them in the same 640x480. Drawing them all at once is not a
      // preview of anything the player ever sees; `ev11/evpm01.pml` has
      // fifteen hidden sheets and one visible, and came out as pure soup.
      // They are still worth LOOKING at, so each is offered as a layer the
      // operator can switch to rather than being dropped silently.
      if (a.show === "0") {
        const name = a.name || `layer ${ctx.layers.length + 1}`;
        if (!ctx.layers.includes(name)) ctx.layers.push(name);
        if (ctx.reveal !== "all" && ctx.reveal !== name) return;
      }

      const hasPos = a.pos !== undefined;
      const [px, py] = xy(a.pos);
      const [w, h] = xy(a.size);
      const ax = ox + px, ay = oy + py;

      const place = (node) => {
        node.style.position = "absolute";
        node.style.left = ax + "px";
        node.style.top = ay + "px";
        if (w) node.style.width = w + "px";
        if (h) node.style.height = h + "px";
        parent.appendChild(node);
        ctx.placed++;
        return node;
      };

      switch (n.tag) {
        case "scrollarea":
        case "sheet":
        case "systembg": {
          // A <sheet>'s fill is `skincolor`; `bgcolor` is what <scrollarea>
          // uses. Reading only bgcolor left every portal sheet transparent, so
          // pages whose whole layout is stacked sheets drew as a blank stage.
          const bg = color(a.bgcolor) || color(a.skincolor);
          const box = place(el("div"));
          box.className = "pml-box";
          if (isOpaqueish(bg)) box.style.background = bg;
          if (a.border === "1") box.style.border = "1px solid " +
            (color(a.bordercolor, "rgba(0,0,0,0.5)"));
          box.style.overflow = "hidden";
          // children are positioned relative to this container's top-left
          renderInto(n.children, parent, ax, ay, ctx);
          break;
        }
        case "title":
          // NOT a drawable element -- it is the bookmark/window name, and all
          // 2,734 of them in the mirror sit in <head> with no pos. Drawing it
          // stamped the page title over the top-left corner of every layout,
          // and for a fragment (no <body> to scope the walk) it was the only
          // thing on the stage: the "block of raw text" that was reported.
          ctx.title = ctx.title ||
            n.children.filter((c) => c.tag === "#text").map((c) => c.text).join("").trim();
          break;
        case "text": {
          const d = place(el("div"));
          Object.assign(d.style, styleCss(ctx.styles, a.style));
          d.style.whiteSpace = "normal";
          d.style.overflow = "hidden";
          d.style.display = "flex";
          d.style.flexDirection = "column";
          d.style.justifyContent =
            a.valign === "middle" ? "center" : a.valign === "bottom" ? "flex-end" : "flex-start";
          let t = "";
          n.children.forEach((c) => { if (c.tag === "#text") t += c.text; });
          inlineText(d, t.trim(), ctx);
          renderInto(n.children.filter((c) => c.tag !== "#text"), parent, ax, ay, ctx);
          break;
        }
        case "input": {
          if (a.type === "hidden") break;
          const inp = place(el("input"));
          inp.className = "pml-input";
          inp.type = a.type === "password" ? "password" : "text";
          inp.value = a.value || "";
          if (a.maxlength) inp.maxLength = parseInt(a.maxlength);
          const sk = color(a.skincolor);
          inp.style.background = isOpaqueish(sk) ? sk : "rgba(255,255,255,0.06)";
          Object.assign(inp.style, styleCss(ctx.styles, a.style));
          inp.style.textShadow = "none";
          inp.style.border = "1px solid rgba(0,0,0,0.45)";
          inp.style.boxSizing = "border-box";
          inp.style.padding = "0 4px";
          break;
        }
        case "img": {
          const url = artUrl(a.src);
          const box = place(el("div"));
          box.style.overflow = "hidden";
          if (isBitmap(url)) {
            const img = el("img", { width: (w || "") + "px", height: (h || "") + "px" });
            img.src = url; img.onerror = () => { box.classList.add("pml-art-missing"); };
            box.appendChild(img);
          } else {
            // .ang animated sprite or unknown -- show a plate placeholder
            box.className = "pml-plate";
          }
          if (a.value) {                      // button caption baked into the img
            const cap = el("div"); cap.className = "pml-cap";
            Object.assign(cap.style, styleCss(ctx.styles, a.style));
            cap.textContent = a.value;
            box.appendChild(cap);
          }
          break;
        }
        case "textbox": {
          const d = place(el("div"));
          d.className = "pml-box pml-textbox";
          const sk = color(a.skincolor);
          if (isOpaqueish(sk)) d.style.background = sk;
          Object.assign(d.style, styleCss(ctx.styles, a.style));
          const recs = ctx.records[a.ref];
          const idx = parseInt(a.index || "0");
          inlineText(d, (recs && recs[idx]) || "", ctx);
          d.style.overflow = "auto";
          d.style.whiteSpace = "pre-wrap";
          d.style.padding = (a.margin || 4) + "px";
          break;
        }
        default:
          // form / if / head / body / data / record / meta / formaction / ...
          // transparent containers: render children at the same offset.
          renderInto(n.children, parent, hasPos ? ax : ox, hasPos ? ay : oy, ctx);
      }
    });
  }

  // A file with no positioned markup is not a broken page -- 4 files in 5 in the
  // mirror are the PIECES pages are built from. A `content` file is body copy a
  // page pulls into a <textbox>, so show it the way its host would: full width,
  // the file's own styles, its own inline markup. It is the thing being edited,
  // and drawing nothing for it is what made the editor look broken.
  function documentView(stage, ctx) {
    const doc = el("div");
    doc.className = "pml-doc";
    Object.keys(ctx.records).forEach((name) => {
      ctx.records[name].forEach((rec, i) => {
        if (!rec.trim()) return;                 // the padding rows SE writes
        const head = el("div");
        head.className = "pml-doc-label";
        head.textContent = ctx.records[name].length > 1 ? `${name}[${i}]` : name;
        doc.appendChild(head);
        const body = el("div");
        body.className = "pml-doc-body";
        inlineText(body, rec, ctx);
        doc.appendChild(body);
      });
    });
    if (!doc.childElementCount) return false;
    stage.appendChild(doc);
    return true;
  }

  /** Draw `pmlText` into `stage`. `base` is the page's www-relative path (art).
   *  Returns what was drawn, so the UI can say which of the four kinds of file
   *  this was rather than leaving a blank stage to be read as a failure. */
  /** `opts.reveal`: the name of a `show="0"` layer to draw, or "all". */
  function render(pmlText, stage, base, opts) {
    artBase = base || "";
    stage.innerHTML = "";
    stage.classList.add("pml-stage");
    const root = parse(pmlText);
    const styles = {}, records = {}, inlineimgs = {};
    collect(root, styles, records, inlineimgs);
    const ctx = { styles, records, inlineimgs, title: "", placed: 0,
                  layers: [], reveal: (opts && opts.reveal) || null };

    // find body; apply its background
    let body = null;
    (function find(n) {
      (n.children || []).forEach((c) => { if (c.tag === "body") body = c; else find(c); });
    })(root);
    if (body && body.attrs.background) {
      const url = artUrl(body.attrs.background);
      if (isBitmap(url)) {
        stage.style.backgroundImage = `url("${url}")`;
        stage.style.backgroundSize = "640px auto";
        stage.style.backgroundRepeat = "no-repeat";
      } else stage.style.backgroundImage = "";
    } else {
      stage.style.backgroundImage = "";
    }
    renderInto((body || root).children, stage, 0, 0, ctx);

    const recordCount = Object.keys(records)
      .reduce((n, k) => n + records[k].filter((r) => r.trim()).length, 0);
    let mode = "layout";
    if (ctx.placed) {
      mode = body ? "page" : "layout";
    } else {
      stage.innerHTML = "";                      // nothing landed; start clean
      mode = documentView(stage, ctx) ? "document" : "empty";
    }
    return {
      mode, title: ctx.title, placed: ctx.placed, records: recordCount,
      styles: Object.keys(styles).length, hasBody: !!body,
      layers: ctx.layers, reveal: ctx.reveal,
    };
  }

  global.PML = { render, parse };
})(window);
