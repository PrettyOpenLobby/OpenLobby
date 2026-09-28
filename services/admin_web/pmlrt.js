// pmlrt.js -- makes a page drawn by pml.js behave like it does in the
// PlayOnline Viewer: action strings run, sheets animate in and out, timers
// fire, focus follows the mouse and the keyboard, and links navigate.
//
// Rules from the PC Viewer's app.dll, written up in
// PlayOnline/docs/notes/pc-viewer/pml-engine-runtime.md (cited "spec N.N").
// Expressions are evaluated by pmlexpr.js. What the spec marks INFERRED is
// marked here too; the choices made for what it leaves open are:
//   - the arrow keys move to the nearest focusable element in that direction;
//   - a directional `type` slides a sheet in from SLIDE px away, and
//     `appearpos` with four values is (appear-from x,y, hide-to x,y);
//   - a sheet with any of appeartime/appearcount/alpha/type/spread/roll/
//     appearpos animates, others show and hide at once;
//   - `onopen`/`onclose` on a sheet run when it starts appearing/hiding;
//     `onopen` on <body> runs when the page starts and `onclose` right after
//     (SE uses both to set the first focus);
//   - pending focus requests are retried every frame.

(function (global) {
  "use strict";
  const E = global.PMLExpr;
  const PML = global.PML;

  const STAGE_W = 640, STAGE_H = 480;
  const SLIDE = 40;                         // INFERRED slide distance, px
  const EVAL_DEPTH = 50;                    // spec 1.4
  const FRAME = "__pmlframe";

  // spec 1.4: send commands. kind: 1 numeric (always evaluated), 0 literal
  // (evaluated only with a quote or $X/%X), -1 no value.
  const CMD = {
    index: 1, forward: 0, go: 0, backward: 0, reload: -1, visible: 0, value: 0, numeric: 1,
    enable: 1, sequence: 1, submit: -1, iterate: -1, show: 0, reset: 0, setx: 0, sety: 0,
    orientationx: 0, orientationy: 0, orientationz: 0, scalex: 0, scaley: 0, scale: 0,
    alphacolor: 0, move: 0, key: 0, extract: 0, sort: 0, refresh: -1, zindex: 0, focus: 0,
    seth: 0, setv: 0, alt: 0, style: 0, text: 0, disable: 1, width: 0, height: 0, fit: 0,
    fitw: 0, fith: 0,
  };
  const ALIAS = { go: "forward" };
  // spec 1.4: other schemes the frame receives
  const FRAME_SCHEMES = new Set(["multi", "bookmark", "addbookmark", "option", "friendlist", "dialog",
    "download", "dl", "restart", "bootmode_d", "bootmode_r", "open_url"]);
  // spec 5.1: schemes that leave the page for the Viewer itself
  const JUMPS = ["toviewer:", "mangato:", "mailto:", "filerto:", "displayto:", "messageto:", "html:",
    "chatto:", "gameto:", "updateto:", "toispsignup:", "topolsignup:", "tologin:", "tologout:",
    "exit:", "gmcallto:", "bootgame:", "qctoolto:", "navigatorto:", "helpto:"];
  // spec 3.4
  const KEYS = { ArrowLeft: "onkeyleft", ArrowRight: "onkeyright", ArrowUp: "onkeyup",
                 ArrowDown: "onkeydown", Escape: "onkeycancel", Backspace: "onkeycancel" };
  // spec 4.2: sheet slide-in types
  const TYPE_NAMES = { left: 1, right: 2, up: 3, top: 3, down: 4, bottom: 4, l_top: 5,
                       l_bottom: 6, r_top: 7, r_bottom: 8, arbitrary: 11 };
  const DIR = { 1: [-1, 0], 2: [1, 0], 3: [0, -1], 4: [0, 1], 5: [-1, -1], 6: [-1, 1], 7: [1, -1], 8: [1, 1] };

  const num = (v, d) => { const n = E.wcstol(v); return v === undefined || v === null || String(v).trim() === "" ? d : n; };
  const ints = (v) => String(v || "").split(",").map((x) => E.wcstol(x));
  const has = (a, k) => a[k] !== undefined && a[k] !== "";
  const stripCodes = (s) => String(s || "").replace(/\^\d\d/g, "");

  function Runtime(stage, opts) {
    this.stage = stage;
    this.opts = opts || {};
    this.vars = new E.Vars(this.opts.vars || {});
    this.comps = [];
    this.names = new Map();
    this.timers = [];
    this.sheets = [];
    this.focus = null;
    this.requests = [];
    this.history = [];
    this.queue = [];
    this.busy = false;
    this.nav = null;
    this.cursor = { x: STAGE_W / 2, y: STAGE_H / 2 };
    this.hover = null;
    this.mouseMode = true;
    this.alive = true;
    this.ready = true;
    this.editing = null;
    this.stats = { actions: 0, ignored: 0, timers: 0 };
    this.handlers = [];
  }

  const R = Runtime.prototype;

  // ------------------------------------------------------------------ log
  R.log = function (kind, text) {
    if (kind === "ignored") this.stats.ignored++;
    if (kind === "timer") this.stats.timers++;
    if (kind === "action") this.stats.actions++;
    if (this.opts.log) this.opts.log(kind, text, this.stats);
  };

  // ------------------------------------------------------------------ build
  R.build = function () {
    const stage = this.stage, ctx = stage._pmlCtx;
    const byNode = new Map();
    stage.querySelectorAll("[data-pml-idx]").forEach((el) => { if (el._pmlNode) byNode.set(el._pmlNode, el); });
    const frame = { kind: "frame", name: FRAME };
    this.frame = frame;
    this.names.set(FRAME, frame);
    const make = (node, el) => {
      const a = node.attrs || {};
      const tag = node.tag;
      let c = null;
      if (tag === "sheet" || tag === "menusheet") c = this.makeSheet(node, el, ctx);
      else if (tag === "scrollarea") c = { kind: "scrollarea", enabled: a.enable !== "0" };
      else if (tag === "img" || (tag === "input" && a.type === "image")) {
        c = { kind: "image", link: has(a, "href") || tag === "input", srcCount: String(a.src || "").split(",").length,
              index: 0, frame: 0, value: a.value || "" };
      } else if (tag === "text") c = { kind: "text" };
      else if (tag === "input" || tag === "select" || tag === "textarea" || tag === "button" ||
               tag === "radio" || tag === "checkbox") c = this.makeWidget(node, el);
      if (!c) return null;
      Object.assign(c, { node, el, tag, name: a.name || "", attrs: a, prio: -100, enabled: c.enabled !== undefined ? c.enabled : a.enable !== "0" });
      // spec 1.1: {$..} in action attributes is fixed when the element is built
      c.act = {};
      for (const k of Object.keys(a)) {
        if (k === "href" || k.startsWith("on")) c.act[k] = E.brace(a[k], this.vars);
      }
      c.alt = a.alt || "";
      c.focusable = (c.kind === "image" && c.link) || c.kind === "widget";
      return c;
    };
    const walk = (node, container) => {
      for (const ch of node.children || []) {
        if (ch.tag === "#text") continue;
        let c = null;
        const el = byNode.get(ch);
        if (el) c = make(ch, el);
        if (c) {
          c.container = container;
          this.comps.push(c);
          if (c.name && !this.names.has(c.name)) this.names.set(c.name, c);
          if (c.kind === "sheet") this.sheets.push(c);
        }
        walk(ch, c && (c.kind === "sheet" || c.kind === "scrollarea") ? c : container);
      }
    };
    walk(stage._pmlBody || stage._pmlRoot, null);
    this.byEl = new Map(this.comps.map((c) => [c.el, c]));
    // timers (spec 4.1), with the container they sit in
    const containerOf = (host) => {
      const el = host && host.closest && host.closest("[data-pml-idx]");
      return el ? this.comps.find((c) => c.el === el && (c.kind === "sheet" || c.kind === "scrollarea")) || null : null;
    };
    for (const t of (ctx && ctx.timers) || []) {
      const a = t.node.attrs;
      const repeat = Math.max(0, num(a.repeat, 1));
      const tm = { kind: "timer", node: t.node, name: a.name || "", attrs: a,
                   delay: Math.max(0, Math.min(0x7fffffff, num(a.delay, 1000))),
                   repeat, remaining: repeat || 1, elapsed: 0, running: a.enable !== "0", last: 0,
                   href: E.brace(a.href || "", this.vars), container: containerOf(t.host) };
      this.timers.push(tm);
      if (tm.name && !this.names.has(tm.name)) this.names.set(tm.name, tm);
    }
    this.body = (stage._pmlBody && stage._pmlBody.attrs) || {};
  };

  R.makeSheet = function (node, el, ctx) {
    const a = node.attrs, g = el._pmlGeom || { x: 0, y: 0, w: 0, h: 0, l: 0, t: 0 };
    let T = num(a.appeartime, 500);
    if (has(a, "appearcount")) T = Math.trunc(num(a.appearcount, 0) * 1000 / 60);
    const types = String(a.type || "0").split(",").map((v) => {
      v = v.trim().toLowerCase();
      return TYPE_NAMES[v] !== undefined ? TYPE_NAMES[v] : E.wcstol(v);
    });
    const anim = ["appeartime", "appearcount", "alpha", "spread", "roll", "appearpos"].some((k) => has(a, k))
      || types.some((t) => t);
    const orient = ints(a.orientation || "0,0,0");
    const sc = has(a, "scale") ? ints(a.scale) : [1000];
    const alpha = has(a, "alphacolor") ? Math.max(0, Math.min(255, num(a.alphacolor, 255))) : 255;
    const cur = { x: g.x, y: g.y, rx: orient[0] || 0, ry: orient[1] || 0, rz: orient[2] || 0,
                  sx: Math.min(8000, sc[0] || 1000), sy: Math.min(8000, sc[1] || sc[0] || 1000), a: alpha };
    const shownAtLoad = ctx.revealed.has(node) ||
      !(a.show === "0" || a.visible === "0" || (a.show && /\$/.test(a.show)));
    return {
      kind: "sheet", geom: g, T: Math.max(0, T), anim, types, t: 0,
      delay: Math.max(0, num(a.delay, 0)), delayOnce: a.delayproperties === "1", delayUsed: false,
      alphaFade: a.alpha === "1", spread: ints(a.spread || "0"), roll: ints(a.roll || "0,0,0"),
      appearpos: has(a, "appearpos") ? ints(a.appearpos) : null,
      want: shownAtLoad, state: 0, waitUntil: null, enabled: a.enable !== "0",
      cur, tgt: Object.assign({}, cur), tween: null, drawn: "",
    };
  };

  R.makeWidget = function (node, el) {
    const a = node.attrs;
    let type = node.tag;
    if (node.tag === "input") type = (a.type || "text").toLowerCase();
    let value = a.value || "";
    let options = null;
    if (node.tag === "select") {
      options = node.children.filter((c) => c.tag === "option").map((o) => ({
        text: PML.decodeEntities(textOf(o)).trim(), value: o.attrs.value !== undefined ? o.attrs.value : PML.decodeEntities(textOf(o)).trim(),
      }));
      value = options[0] ? options[0].value : "";
    }
    if (node.tag === "textarea") value = PML.decodeEntities(textOf(node)).trim() || value;
    return { kind: "widget", type, value, options, index: 0,
             checked: a.checked !== undefined && a.checked !== "0", skinState: -1, pressed: false };
  };

  function textOf(n) {
    let t = "";
    (function walk(x) { (x.children || []).forEach((c) => { if (c.tag === "#text") t += c.text; else walk(c); }); })(n);
    return t;
  }

  // ------------------------------------------------------------------ start / stop
  R.start = function () {
    this.build();
    const now = performance.now();
    this.clock = now;
    for (const s of this.sheets) {
      s.state = 0;
      if (s.want) this.requestShow(s, now, true);
      this.drawSheet(s);
    }
    for (const t of this.timers) t.last = now;
    this.bind();
    this.updateStates(true);
    if (this.body.onopen) this.run(E.brace(this.body.onopen, this.vars), "page open");
    this.afterFirst = true;
    this.log("info", `Page started: ${this.sheets.length} sheets, ${this.timers.length} timers, ` +
      `${this.comps.filter((c) => c.focusable).length} focusable elements`);
    const loop = (t) => {
      if (!this.alive) return;
      this.tick(t);
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  };

  R.stop = function () {
    this.alive = false;
    cancelAnimationFrame(this.raf);
    for (const [el, ev, fn, opt] of this.handlers) el.removeEventListener(ev, fn, opt);
    this.handlers = [];
    this.endEdit(false);
  };

  // ------------------------------------------------------------------ frame loop
  R.tick = function (now) {
    const dt = Math.min(250, Math.max(0, now - this.clock));
    this.clock = now;
    if (this.afterFirst) {
      this.afterFirst = false;
      // INFERRED: <body onclose> once the page has opened (SE sets focus there)
      if (this.body.onclose) this.run(E.brace(this.body.onclose, this.vars), "page ready");
    }
    // spec 4.1: timers, strict >, only while the page is ready
    if (this.ready) {
      for (const t of this.timers) {
        if (!t.running) continue;
        t.elapsed += Math.min(250, now - t.last);
        t.last = now;
        if (t.delay < t.elapsed) {
          t.elapsed = 0;
          if (t.repeat !== 0 && --t.remaining <= 0) { t.remaining = 0; t.running = false; }
          this.log("timer", `${t.name || "timer"} fired`);
          this.run(t.href, "timer " + (t.name || ""));
          if (!this.alive) return;
        }
      }
    }
    for (const s of this.sheets) this.stepSheet(s, now, dt);
    this.updateStates(false);
    if (this.requests.length) this.processRequests();
    if (this.focus && !this.canFocus(this.focus)) this.setFocus(null);
    if (!this.focus && !this.editing) this.focusNearest();
  };

  // ------------------------------------------------------------------ sheets (spec 2.1, 4.2, 4.3)
  const visibleState = (s) => s.state === 1 || s.state === 2 || s.state === 3;
  R.parentVisible = function (c) {
    for (let p = c.container; p; p = p.container) {
      if (p.kind === "sheet" && !visibleState(p)) return false;
    }
    return true;
  };

  R.requestShow = function (s, now, initial) {
    s.want = true;
    if (s.state === 1 || s.state === 2) return;
    if (s.state === 3) {                     // reverse from the current point
      this.beginAppear(s, false);
      return;
    }
    const d = s.delayOnce && s.delayUsed ? 0 : s.delay;
    s.delayUsed = true;
    s.waitUntil = d > 0 ? null : 0;
    s.delayLeft = d;
    if (initial) s.initial = true;
  };

  R.beginAppear = function (s, fresh) {
    if (fresh) s.t = 0;
    if (!s.anim || s.T <= 0) { s.state = 2; s.t = s.T; }
    else s.state = 1;
    s.waitUntil = undefined;
    if (s.attrs.insound) this.log("sound", `sound ${s.attrs.insound} (${s.name || "sheet"} appears)`);
    if (s.act.onopen) this.run(s.act.onopen, (s.name || "sheet") + " opens");
  };

  R.requestHide = function (s, instant) {
    s.want = false;
    s.waitUntil = undefined;
    if (s.state === 0 || s.state === 4) return;
    if (instant || !s.anim || s.T <= 0) { s.state = 4; s.t = 0; }
    else if (s.state !== 3) s.state = 3;
    if (s.attrs.outsound) this.log("sound", `sound ${s.attrs.outsound} (${s.name || "sheet"} hides)`);
    if (s.act.onclose) this.run(s.act.onclose, (s.name || "sheet") + " closes");
  };

  R.stepSheet = function (s, now, dt) {
    if ((s.state === 0 || s.state === 4) && s.want && s.waitUntil !== undefined) {
      // waiting: the delay counts once the parent is on screen
      if (this.parentVisible(s)) {
        s.delayLeft -= dt;
        if (s.delayLeft <= 0) this.beginAppear(s, true);
      }
    } else if (s.state === 1) {
      s.t += dt;
      if (s.t >= s.T) { s.t = s.T; s.state = 2; }
    } else if (s.state === 3) {
      s.t -= dt;
      if (s.t <= 0) { s.t = 0; s.state = 4; }
    }
    if (s.tween) {
      const k = Math.min(1, (now - s.tween.start) / s.tween.dur);
      const f = k < 1 ? (1 - Math.cos(Math.PI * k)) / 2 : 1;       // spec 4.3
      for (const key of Object.keys(s.tween.from)) {
        s.cur[key] = s.tween.from[key] + (s.tgt[key] - s.tween.from[key]) * f;
      }
      if (k >= 1) s.tween = null;
    }
    this.drawSheet(s);
  };

  R.drawSheet = function (s) {
    const el = s.el, g = s.geom;
    const on = visibleState(s);
    let f = 1;
    if (s.state === 1 || s.state === 3) f = s.t < s.T ? Math.sin(s.t * (Math.PI / 2) / s.T) : 1;
    let dx = 0, dy = 0;
    if (f < 1) {
      const pp = s.appearpos;
      const tx = s.types[0] || 0, ty = s.types.length > 1 ? s.types[1] : tx;
      if (pp && (tx >= 10 || ty >= 10)) {
        // INFERRED: appearpos = appear-from x,y [, hide-to x,y]
        const back = s.state === 3 && pp.length >= 4;
        const px = back ? pp[2] : pp[0], py = back ? pp[3] : pp[1];
        dx = (px - s.cur.x) * (1 - f);
        dy = (py - s.cur.y) * (1 - f);
      } else {
        if (DIR[tx]) dx = DIR[tx][0] * SLIDE * (1 - f);
        if (DIR[ty]) dy = DIR[ty][1] * SLIDE * (1 - f);
      }
    }
    const spreadK = (m) => (m === 1 ? f : m === 2 ? f + Math.sin(f * Math.PI * f) : m === 3 ? 2 - f : 1);
    const sx = (s.cur.sx / 1000) * spreadK(s.spread[0] || 0);
    const sy = (s.cur.sy / 1000) * spreadK(s.spread.length > 1 ? s.spread[1] : s.spread[0] || 0);
    const rollK = (1 - f) * (-Math.PI / 50);
    const rot = (o, r) => o * (-Math.PI / 500) + (r || 0) * rollK;
    const rx = rot(s.cur.rx, s.roll[0]), ry = rot(s.cur.ry, s.roll[1]), rz = rot(s.cur.rz, s.roll[2]);
    const alpha = (s.cur.a / 255) * (s.alphaFade ? f : 1);
    const left = Math.round(s.cur.x - g.l), top = Math.round(s.cur.y - g.t);
    let tf = "";
    if (dx || dy) tf += `translate(${dx.toFixed(2)}px,${dy.toFixed(2)}px) `;
    if (rx) tf += `rotateX(${rx.toFixed(4)}rad) `;
    if (ry) tf += `rotateY(${ry.toFixed(4)}rad) `;
    if (rz) tf += `rotate(${rz.toFixed(4)}rad) `;
    if (sx !== 1 || sy !== 1) tf += `scale(${sx.toFixed(4)},${sy.toFixed(4)})`;
    const key = [on, left, top, tf, alpha.toFixed(3)].join("|");
    if (key === s.drawn) return;
    s.drawn = key;
    el.style.display = on ? "" : "none";
    el.style.left = left + "px";
    el.style.top = top + "px";
    el.style.transform = tf.trim();
    el.style.transformOrigin = "50% 50%";
    el.style.opacity = alpha >= 0.999 ? "" : String(Math.max(0, alpha));
  };

  R.sheetCommand = function (s, cmd, v) {
    const menu = s.tag === "menusheet";
    switch (cmd) {
      case "show": {
        let x = v.trim();
        if (menu && x.startsWith("-")) x = visibleState(s) && s.want ? "0" : "1";
        if (x === "0") { if (s.state !== 3) this.requestHide(s, false); }
        else if (x === "1") { if (s.state !== 1) this.requestShow(s, this.clock, false); }
        return true;
      }
      case "visible": {
        let x = v.trim();
        if (menu && x.startsWith("-")) x = visibleState(s) ? "0" : "1";
        if (x === "0") this.requestHide(s, true);
        else if (x === "1") {
          s.want = true;
          if (s.state !== 2) { s.state = 2; s.t = s.T; s.waitUntil = undefined; }
        }
        return true;
      }
      case "enable": s.enabled = E.wcstol(v) !== 0; return true;
      case "setx": s.tgt.x = E.wcstol(v); return true;
      case "sety": s.tgt.y = E.wcstol(v); return true;
      case "orientationx": s.tgt.rx = E.wcstol(v); return true;
      case "orientationy": s.tgt.ry = E.wcstol(v); return true;
      case "orientationz": s.tgt.rz = E.wcstol(v); return true;
      case "scalex": s.tgt.sx = Math.min(8000, E.wcstol(v)); return true;
      case "scaley": s.tgt.sy = Math.min(8000, E.wcstol(v)); return true;
      case "scale": s.tgt.sx = s.tgt.sy = Math.min(8000, E.wcstol(v)); return true;
      case "alphacolor": {
        const a = E.wcstol(v);
        if (a < 0) { s.cur.a = 0; s.tgt.a = 0; } else s.tgt.a = Math.min(255, a);
        return true;
      }
      case "zindex": {
        let z = E.wcstol(v);
        z = Math.max(0, Math.min(20, z));
        s.el.style.zIndex = 20 - z;
        return true;
      }
      case "move": {
        const n = E.wcstol(v);
        const keys = Object.keys(s.tgt).filter((k) => s.tgt[k] !== s.cur[k]);
        if (!keys.length) return true;
        if (n <= 0 || !visibleState(s)) {
          for (const k of keys) s.cur[k] = s.tgt[k];
          s.tween = null;
        } else {
          const from = {};
          for (const k of Object.keys(s.tgt)) from[k] = s.cur[k];
          s.tween = { from, start: this.clock, dur: n * 1000 / 60 };
        }
        this.drawSheet(s);
        return true;
      }
      default: return false;
    }
  };

  // ------------------------------------------------------------------ input and states
  // spec 3.1: a sheet's children take input only when it is enabled and
  // fully shown; a scrollarea's only when enabled.
  R.inputOK = function (c) {
    if (c.el && c.el._pmlHidden && c.kind !== "sheet") return false;
    for (let p = c.container; p; p = p.container) {
      if (!p.enabled) return false;
      if (p.kind === "sheet" && p.state !== 2) return false;
    }
    return true;
  };
  R.canFocus = function (c) {
    return !!c && c.focusable && c.enabled && this.inputOK(c);
  };

  // Frames and skin states that follow focus, press and disabled (spec 3.3, 3.3a).
  R.updateStates = function (force) {
    for (const c of this.comps) {
      if (c.kind === "image" && c.link) {
        const f = !this.canFocus(c) ? 2 : c === this.focus ? 3 : 0;
        if (f !== c.frame || force && f !== 0) { c.frame = f; PML.setState(c.el, f); }
      } else if (c.kind === "widget") {
        let s;
        const dis = !this.canFocus(c);
        const isField = /^(text|password|textarea|select)$/.test(c.type);
        if (dis) s = 2;
        else if (isField) s = this.editing && this.editing.comp === c ? 3 : c === this.focus || (this.mouseMode && c === this.hover) ? 1 : 0;
        else if (/^(checkbox|radio)$/.test(c.type)) s = 0;
        else s = c.pressed ? 1 : c === this.focus ? 3 : 0;      // INFERRED 3 = focused button
        if (s !== c.skinState) { c.skinState = s; PML.setState(c.el, s); }
      }
    }
    for (const s of this.sheets) {
      const want = s.enabled ? 0 : 2;
      if (s.el._pmlSkin && s.skinState !== want && (s.skinState !== undefined || want !== s.el._pmlSkin.state)) {
        s.skinState = want;
        PML.setState(s.el, want);
      }
    }
  };

  // ------------------------------------------------------------------ focus (spec 3.2)
  R.requestFocus = function (c, value) {
    const v = String(value || "").trim();
    const prio = v === "" ? 0 : v[0] === "L" ? -1 : v[0] === "X" ? -2 : E.wcstol(v);
    if (c.prio < prio) {
      c.prio = prio;
      const i = this.requests.indexOf(c);
      if (i >= 0) this.requests.splice(i, 1);
      this.requests.push(c);
      if (prio !== -1) this.processRequests();
    }
    return true;
  };

  R.processRequests = function () {
    const list = this.requests;
    const i = list.findIndex((c) => this.canFocus(c));
    if (i < 0) return;
    const win = list[i];
    const keep = list.slice(i + 1).filter((c) => c.prio >= win.prio);   // INFERRED: equal/higher stay pending
    for (const c of list) if (!keep.includes(c)) c.prio = -100;
    this.requests = keep;
    this.setFocus(win, "request");
  };

  R.setFocus = function (c, why) {
    if (c === this.focus) return;
    const old = this.focus;
    this.focus = c;
    if (old) {
      if (old.act.onmouseout) this.run(old.act.onmouseout, (old.name || old.tag) + " loses focus");
    }
    if (c) {
      this.history.push(c);
      if (this.history.length > 64) this.history.shift();
      if (c.attrs.mousesound && c.attrs.mousesound !== "0") this.log("sound", `sound ${c.attrs.mousesound} (focus)`);
      if (c.act.onmouseover) this.run(c.act.onmouseover, (c.name || c.tag) + " gets focus");
      this.log("focus", `Focus: ${c.name || "<" + c.tag + ">"}${why ? " (" + why + ")" : ""}`);
    }
    if (this.opts.onHelp) this.opts.onHelp(c ? stripCodes(c.alt) : "");
    this.updateStates(false);
  };

  // spec 3.2: nothing focused -> the focusable element nearest the cursor,
  // cursor clamped to 16..624 x 32..448
  R.focusNearest = function () {
    const cx = Math.max(16, Math.min(624, this.cursor.x)), cy = Math.max(32, Math.min(448, this.cursor.y));
    let best = null, bd = Infinity;
    for (const c of this.comps) {
      if (!this.canFocus(c)) continue;
      const r = this.rectOf(c);
      if (!r) continue;
      const ddx = r.x + r.w / 2 - cx, ddy = r.y + r.h / 2 - cy;
      const d = ddx * ddx + ddy * ddy;
      if (d < bd) { bd = d; best = c; }
    }
    if (best) this.setFocus(best, "nearest to the pointer");
  };

  R.rectOf = function (c) {
    if (!c.el) return null;
    const sr = this.stage.getBoundingClientRect();
    const k = sr.width / STAGE_W || 1;
    const r = c.el.getBoundingClientRect();
    if (!r.width && !r.height) return null;
    return { x: (r.left - sr.left) / k, y: (r.top - sr.top) / k, w: r.width / k, h: r.height / k };
  };

  // INFERRED (spec 3.4): nearest focusable element in the pressed direction.
  R.moveFocus = function (dir) {
    const cur = this.focus && this.rectOf(this.focus);
    if (!cur) { this.focusNearest(); return; }
    const cx = cur.x + cur.w / 2, cy = cur.y + cur.h / 2;
    let best = null, bd = Infinity;
    for (const c of this.comps) {
      if (c === this.focus || !this.canFocus(c)) continue;
      const r = this.rectOf(c);
      if (!r) continue;
      const x = r.x + r.w / 2 - cx, y = r.y + r.h / 2 - cy;
      const main = dir === "up" ? -y : dir === "down" ? y : dir === "left" ? -x : x;
      const side = dir === "up" || dir === "down" ? Math.abs(x) : Math.abs(y);
      if (main <= 1) continue;
      const d = main + side * 2;
      if (d < bd) { bd = d; best = c; }
    }
    if (best) this.setFocus(best, "arrow key");
  };

  // ------------------------------------------------------------------ activation
  R.activate = function (c) {
    if (!c || !this.canFocus(c)) return;
    if (c.attrs.clicksound && c.attrs.clicksound !== "0") this.log("sound", `sound ${c.attrs.clicksound} (click)`);
    if (c.kind === "image") {
      if (c.act.href) this.run(c.act.href, (c.name || "<img>") + " activated");
      else if (c.tag === "input") this.submit(c);
      return;
    }
    if (c.kind !== "widget") return;
    switch (c.type) {
      case "text": case "password": case "textarea": this.beginEdit(c); return;
      case "submit": if (c.act.href) this.run(c.act.href, "button"); else this.submit(c); return;
      case "reset": this.resetForm(c); return;
      case "checkbox": case "radio": {
        if (c.type === "radio") {
          for (const o of this.formFields(c)) if (o.type === "radio" && o.attrs.name === c.attrs.name) { o.checked = false; this.paintCheck(o); }
          c.checked = true;
        } else c.checked = !c.checked;
        this.paintCheck(c);
        return;
      }
      case "select": {
        if (c.options && c.options.length) {
          c.index = (c.index + 1) % c.options.length;
          c.value = c.options[c.index].value;
          PML.setCaption(c.el, c.options[c.index].text);
          this.log("action", `${c.name || "list"}: ${c.options[c.index].text}`);
        }
        return;
      }
      default:
        if (c.act.href) this.run(c.act.href, (c.name || c.tag) + " activated");
    }
  };

  R.paintCheck = function (c) {
    const k = c.el._pmlSkin;
    if (k && k.parts[0]) { k.parts[0].fixedState = c.checked ? 1 : 0; PML.setState(c.el, k.state || 0); }
  };

  // ------------------------------------------------------------------ forms (minimal)
  R.formOf = function (c) {
    for (let n = c.node.parent; n; n = n.parent) if (n.tag === "form") return n;
    return null;
  };
  R.formFields = function (c) {
    const f = this.formOf(c);
    const out = [];
    const walk = (n) => (n.children || []).forEach((ch) => {
      const w = this.comps.find((x) => x.node === ch && x.kind === "widget");
      if (w) out.push(w);
      else if (ch.tag === "input" && (ch.attrs.type || "").toLowerCase() === "hidden") {
        out.push({ kind: "widget", type: "hidden", attrs: ch.attrs, name: ch.attrs.name || "", value: ch.attrs.value || "" });
      }
      walk(ch);
    });
    walk(f || this.stage._pmlBody || this.stage._pmlRoot);
    return out;
  };
  R.submit = function (c) {
    const form = this.formOf(c);
    const vals = [];
    for (const w of this.formFields(c)) {
      if (!w.attrs || !w.attrs.name) continue;
      if (/^(submit|reset|button|image)$/.test(w.type)) continue;
      if (/^(checkbox|radio)$/.test(w.type) && !w.checked) continue;
      vals.push(`${w.attrs.name}=${w.type === "password" ? "*".repeat(Math.min(12, w.value.length)) : w.value}`);
    }
    const action = form && (form.attrs.action || form.attrs.href) || "";
    const msg = `Form sent${action ? " to " + action : ""}: ${vals.length ? vals.join(", ") : "no fields"}`;
    this.log("action", msg);
    if (this.opts.toast) this.opts.toast(msg);
  };
  R.resetForm = function (c) {
    for (const w of this.formFields(c)) {
      if (w.el && /^(text|password|textarea)$/.test(w.type)) {
        w.value = w.attrs.value || "";
        PML.setCaption(w.el, w.value);
      }
    }
    this.log("action", "Form reset");
  };

  R.beginEdit = function (c) {
    this.endEdit(true);
    const f = c.el._pmlField;
    if (!f) return;
    const multi = c.type === "textarea";
    const inp = document.createElement(multi ? "textarea" : "input");
    if (!multi) inp.type = c.type === "password" ? "password" : "text";
    inp.value = c.value;
    inp.className = "pml-edit";
    Object.assign(inp.style, { position: "absolute", left: f.x + "px", top: f.y + "px", width: f.w + "px",
      height: f.h + "px", zIndex: 50, boxSizing: "border-box" });
    if (c.attrs.maxlength) inp.maxLength = E.wcstol(c.attrs.maxlength);
    c.el.appendChild(inp);
    this.editing = { comp: c, input: inp };
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && !multi) { e.preventDefault(); this.endEdit(true); this.stage.focus(); }
      if (e.key === "Escape") { e.preventDefault(); this.endEdit(false); this.stage.focus(); }
    });
    inp.addEventListener("blur", () => this.endEdit(true));
    inp.addEventListener("click", (e) => e.stopPropagation());
    setTimeout(() => inp.focus(), 0);
    this.updateStates(false);
  };
  R.endEdit = function (commit) {
    const ed = this.editing;
    if (!ed) return;
    this.editing = null;
    if (commit) {
      ed.comp.value = ed.input.value;
      PML.setCaption(ed.comp.el, ed.comp.value);
    }
    ed.input.remove();
    if (this.alive) this.updateStates(false);
  };

  // ------------------------------------------------------------------ actions (spec 1)
  R.run = function (str, source) {
    if (!str || !this.alive) return;
    if (this.busy) { this.queue.push([str, source]); return; }
    this.busy = true;
    try {
      this.log("action", `${source ? source + ": " : ""}${str.replace(/\s+/g, " ").slice(0, 300)}`);
      this.exec(str, 0);
      while (this.queue.length && this.alive) {
        const [s, src] = this.queue.shift();
        this.log("action", `${src ? src + ": " : ""}${s.replace(/\s+/g, " ").slice(0, 300)}`);
        this.exec(s, 0);
      }
    } catch (e) {
      this.log("ignored", "Action failed: " + e.message);
    } finally {
      this.busy = false;
    }
    if (this.nav && this.alive) {
      const n = this.nav;
      this.nav = null;
      this.navigate(n);
    }
  };

  R.exec = function (str, depth) {
    let prevCmd = null, prevTarget = null;
    for (const raw of String(str).split(",")) {
      if (!this.alive) return;
      let item = raw.replace(/^\s+/, "");
      if (!item.trim()) continue;
      if (/^sd:/i.test(item)) item = "send:" + item.slice(3);
      if (/^send:/i.test(item)) {
        const at = item.lastIndexOf("@");
        let cmd = at < 0 ? item : item.slice(0, at);
        let target = at < 0 ? FRAME : item.slice(at + 1).trim();
        if (cmd.trim().toLowerCase() === "send:") cmd = prevCmd !== null ? prevCmd : cmd;
        else prevCmd = cmd;
        if (at >= 0 && target === "") target = prevTarget !== null ? prevTarget : "";
        else prevTarget = target;
        this.send(cmd, target);
        continue;
      }
      const colon = item.indexOf(":");
      const scheme = colon < 0 ? "" : item.slice(0, colon).toLowerCase();
      const rest = colon < 0 ? "" : item.slice(colon + 1);
      if (scheme === "null") { E.evalInt(rest, this.vars); continue; }
      if (scheme === "eval") {
        if (depth >= EVAL_DEPTH) { this.log("ignored", "eval: nested too deep"); continue; }
        const s = E.evalStr(rest, this.vars);
        this.log("action", `eval -> ${s.replace(/\s+/g, " ").slice(0, 200)}`);
        this.exec(s, depth + 1);
        continue;
      }
      if (scheme === "sound") { this.log("sound", `sound ${rest.trim()}`); continue; }
      if (scheme === "forward" || scheme === "go") { this.frameCommand("forward", rest.trim()); continue; }
      if (scheme === "backward") { this.frameCommand("backward", rest.trim()); continue; }
      if (scheme === "reload") { this.frameCommand("reload", ""); continue; }
      if (scheme === "page") { this.log("action", `Jump to #${rest.trim()} on this page`); continue; }
      if (FRAME_SCHEMES.has(scheme)) {
        const msg = `The Viewer would open ${scheme}${rest.trim() ? ": " + rest.trim() : ""}`;
        this.log("nav", msg);
        if (this.opts.toast) this.opts.toast(msg);
        continue;
      }
      // anything else navigates (spec 1.4, 5.1)
      this.goTo(E.auto(item.trim(), this.vars));
    }
  };

  R.send = function (cmd, target) {
    const c = this.names.get(target);
    if (!c) { this.log("ignored", `${cmd.slice(5) || "(no command)"} @${target}: no element with that name`); return; }
    const rest = cmd.slice(5);
    const eq = rest.indexOf("=");
    let name = (eq < 0 ? rest : rest.slice(0, eq)).trim().toLowerCase();
    let value = eq < 0 ? "" : rest.slice(eq + 1);
    if (!(name in CMD)) { this.log("ignored", `unknown command "${name}" @${target}`); return; }
    const kind = CMD[name];
    name = ALIAS[name] || name;
    value = kind === 1 ? E.evalStr(value, this.vars) : kind === 0 ? E.auto(value, this.vars) : "";
    const ok = this.receive(c, name, value);
    if (!ok) this.log("ignored", `${name}${value !== "" ? "=" + value : ""} has no effect on ${c.kind === "frame" ? "the page" : "<" + (c.tag || c.kind) + "> " + target}`);
  };

  // spec 2: what each command does to each element
  R.receive = function (c, cmd, v) {
    switch (c.kind) {
      case "frame": return this.frameCommand(cmd, v);
      case "sheet": return this.sheetCommand(c, cmd, v);
      case "scrollarea":
        if (cmd === "enable") { c.enabled = E.wcstol(v) !== 0; return true; }
        if (cmd === "reset") { c.el.scrollTop = 0; c.el.scrollLeft = 0; return true; }
        if (cmd === "setv") { c.el.scrollTop = E.wcstol(v); return true; }
        if (cmd === "seth") { c.el.scrollLeft = E.wcstol(v); return true; }
        if (cmd === "focus") return true;
        return false;
      case "timer":
        if (cmd === "reset") {
          c.remaining = c.repeat || 1;
          c.elapsed = 0;
          if (v === "") return true;
          return this.timerEnable(c, E.wcstol(v));
        }
        if (cmd === "enable") return this.timerEnable(c, E.wcstol(v));
        return false;
      case "image":
        switch (cmd) {
          case "sequence":
            if (c.link) return false;
            c.frame = E.wcstol(v);
            PML.setState(c.el, c.frame);
            return true;
          case "index": {
            const i = E.wcstol(v);
            if (i < 0 || i >= c.srcCount || i === c.index) return true;
            c.index = i; PML.setImage(c.el, { index: i });
            return true;
          }
          case "forward": if (c.index + 1 < c.srcCount) { c.index++; PML.setImage(c.el, { index: c.index }); } return true;
          case "backward": if (c.index > 0) { c.index--; PML.setImage(c.el, { index: c.index }); } return true;
          case "iterate": c.index = (c.index + 1) % Math.max(1, c.srcCount); PML.setImage(c.el, { index: c.index }); return true;
          case "value": c.value = v; PML.setCaption(c.el, v); return true;
          case "focus": return c.link ? this.requestFocus(c, v) : false;
          case "alt": c.alt = v; if (this.focus === c && this.opts.onHelp) this.opts.onHelp(stripCodes(v)); return true;
          default: return false;
        }
      case "text":
        if (cmd === "reload") {
          // spec 2.4: re-read the text's &var= values. Needs the text as
          // written, which the expander hands over as pml-text.
          const raw = c.attrs["pml-text"];
          if (raw === undefined) return false;
          return PML.setTextStyle(c.el, c.style, E.expandText(raw, this.vars));
        }
        if (cmd === "style") { c.style = v; return PML.setTextStyle(c.el, v); }
        return false;
      case "widget":
        switch (cmd) {
          case "focus": return this.requestFocus(c, v);
          case "alt": c.alt = v; return true;
          case "value": case "text":
            if (c.type === "select" && c.options) {
              const i = c.options.findIndex((o) => o.value === v || o.text === v);
              if (i >= 0) { c.index = i; c.value = c.options[i].value; PML.setCaption(c.el, c.options[i].text); }
              return true;
            }
            c.value = v; PML.setCaption(c.el, v); return true;
          case "index":
            if (c.type === "select" && c.options) {
              const i = E.wcstol(v);
              if (i >= 0 && i < c.options.length) { c.index = i; c.value = c.options[i].value; PML.setCaption(c.el, c.options[i].text); }
              return true;
            }
            return false;
          case "submit": case "enable":
            if (/^(submit|image)$/.test(c.type) && (cmd === "submit" || E.wcstol(v) !== 0)) { this.submit(c); return true; }
            if (c.type === "reset" && cmd === "enable" && E.wcstol(v) !== 0) { this.resetForm(c); return true; }
            return false;
          default: return false;
        }
      default: return false;
    }
  };

  R.timerEnable = function (t, on) {
    if (on) {
      if (t.running || t.remaining <= 0) return true;
      t.running = true;
      t.elapsed = 0;
      t.last = this.clock;
    } else {
      t.running = false;
    }
    return true;
  };

  R.frameCommand = function (cmd, v) {
    switch (cmd) {
      case "forward": case "backward": {
        const n = v === "" ? 1 : E.wcstol(v) || 1;
        this.log("nav", `${cmd === "forward" ? "Forward" : "Back"} ${n}`);
        this.nav = { history: cmd === "forward" ? n : -n };
        return true;
      }
      case "reload":
        this.log("nav", "Reload");
        this.nav = { reload: true };
        return true;
      case "focus": {
        const n = E.wcstol(v);
        if (n >= 0) return false;
        const c = this.history[this.history.length - 1 + n];
        if (c && this.canFocus(c)) this.setFocus(c, "focus history");
        return true;
      }
      default: return false;
    }
  };

  // spec 5.1: a navigation item
  R.goTo = function (url) {
    url = String(url || "").trim();
    if (!url) return;
    const low = url.toLowerCase();
    const jump = JUMPS.find((j) => low.startsWith(j));
    if (jump) {
      const msg = `The Viewer would jump to ${url}`;
      this.log("nav", msg);
      if (this.opts.toast) this.opts.toast(msg);
      return;
    }
    if (url[0] === "#") { this.log("action", `Jump to ${url} on this page`); return; }
    const m = /^([a-z][a-z0-9+.-]*):/i.exec(url);
    if (m && !/^(x-.*|file|http|https)$/i.test(m[1])) {
      this.log("ignored", `"${url}" is not a page the Viewer opens`);
      return;
    }
    // Items after the navigation still run against the old page (INFERRED).
    this.nav = url;
  };

  R.navigate = function (n) {
    this.ready = false;
    if (n.reload) { if (this.opts.reload) this.opts.reload(); return; }
    if (n.history) { if (this.opts.history) this.opts.history(n.history); return; }
    this.log("nav", "Open " + n);
    if (this.opts.navigate) this.opts.navigate(n);
  };

  // ------------------------------------------------------------------ events
  R.on = function (el, ev, fn, opt) {
    el.addEventListener(ev, fn, opt);
    this.handlers.push([el, ev, fn, opt]);
  };

  R.stagePoint = function (e) {
    const sr = this.stage.getBoundingClientRect();
    const k = sr.width / STAGE_W || 1;
    return { x: (e.clientX - sr.left) / k, y: (e.clientY - sr.top) / k };
  };

  // The focusable element under the pointer that can take input now.
  R.hitAt = function (clientX, clientY) {
    const seen = new Set();
    for (const el of document.elementsFromPoint(clientX, clientY)) {
      if (!this.stage.contains(el)) continue;
      const box = el.closest("[data-pml-idx]");
      if (!box || seen.has(box)) continue;
      seen.add(box);
      const c = this.byEl.get(box);
      if (c && c.focusable && this.canFocus(c)) return c;
    }
    return null;
  };

  R.bind = function () {
    const st = this.stage;
    const paused = () => (this.opts.paused && this.opts.paused()) || !this.alive;
    if (!st.hasAttribute("tabindex")) st.setAttribute("tabindex", "0");
    this.on(st, "mousemove", (e) => {
      if (paused()) return;
      this.cursor = this.stagePoint(e);
      this.mouseMode = true;
      const c = this.hitAt(e.clientX, e.clientY);
      if (c !== this.hover) {
        this.hover = c;
        // INFERRED (spec 3.3): the pointer gives focus
        if (c && c !== this.focus) this.setFocus(c, "pointer");
        this.updateStates(false);
      }
    });
    this.on(st, "mouseleave", () => { this.hover = null; if (this.alive) this.updateStates(false); });
    this.on(st, "mousedown", (e) => {
      if (paused() || e.button !== 0) return;
      if (this.editing && this.editing.input.contains(e.target)) return;
      st.focus({ preventScroll: true });
      const c = this.hitAt(e.clientX, e.clientY);
      if (c && c.kind === "widget") { c.pressed = true; this.updateStates(false); }
    });
    this.on(document, "mouseup", () => {
      for (const c of this.comps) if (c.pressed) c.pressed = false;
      if (this.alive) this.updateStates(false);
    });
    this.on(st, "click", (e) => {
      if (paused()) return;
      if (this.editing && this.editing.input.contains(e.target)) return;
      e.preventDefault();
      this.cursor = this.stagePoint(e);
      const c = this.hitAt(e.clientX, e.clientY);
      if (!c) return;
      if (c !== this.focus) this.setFocus(c, "click");
      this.activate(c);
    });
    this.on(st, "keydown", (e) => {
      if (paused() || e.altKey || e.ctrlKey || e.metaKey) return;
      if (this.editing) return;
      const c = this.focus;
      let attr = KEYS[e.key];
      if (e.key === "Tab") attr = e.shiftKey ? "onkeyshifttab" : "onkeytab";
      const handled = () => { e.preventDefault(); e.stopPropagation(); };
      this.mouseMode = false;
      if (c && attr && c.act[attr] !== undefined) {
        handled();
        this.run(c.act[attr], `${c.name || c.tag} ${e.key}`);
        return;
      }
      if (e.key === "Enter" || e.key === " ") {
        handled();
        if (c) this.activate(c);
        return;
      }
      const dir = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[e.key];
      if (dir) { handled(); this.moveFocus(dir); return; }
      if (e.key === "Tab") {
        handled();
        const list = this.comps.filter((x) => this.canFocus(x));
        if (!list.length) return;
        const i = list.indexOf(c);
        this.setFocus(list[(i + (e.shiftKey ? -1 : 1) + list.length) % list.length], "Tab");
        return;
      }
      if (e.key === "Escape" || e.key === "Backspace") handled();
    });
  };

  // ------------------------------------------------------------------ inspection
  R.snapshot = function () {
    const sheets = {};
    for (const s of this.sheets) if (s.name) sheets[s.name] = s.state;
    const timers = {};
    for (const t of this.timers) if (t.name) timers[t.name] = { running: t.running, remaining: t.remaining };
    return { focus: this.focus ? this.focus.name || this.focus.tag : null, sheets, timers,
             stats: Object.assign({}, this.stats), requests: this.requests.map((c) => c.name) };
  };

  /**
   * Run a page drawn by PML.render(..., {interactive: true}) in `stage`.
   * opts: {vars, log(kind, text, stats), toast(msg), onHelp(text),
   *        navigate(url), history(delta), reload(), paused()}.
   * Returns the runtime; call .stop() before the stage is redrawn.
   */
  function start(stage, opts) {
    const rt = new Runtime(stage, opts);
    rt.start();
    stage._pmlRuntime = rt;
    return rt;
  }

  global.PMLRuntime = { start };
})(window);
