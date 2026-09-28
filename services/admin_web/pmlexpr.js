// pmlexpr.js -- the PC Viewer's PML expression engine, for the preview's
// runtime (pmlrt.js). A port of PlayOnline/docs/notes/pc-viewer/pml_expr_ref.py,
// which follows app.dll 1.18.15e instruction for instruction: the same
// tokenizer, the same precedence-table reducer, the same int/string choice
// per operator and the same error markers. Keep the two in step.
//
// Values are strings; integers are 32-bit and wrap. A name is case-sensitive
// and keeps its sigil ("$x"). Scalars and arrays share one namespace.

(function (global) {
  "use strict";

  const INT = 0, STR = 1, VAR = 2;
  // .data rva 0x4a8fd0, indexed by token type
  const PREC = [0, 0, 0, 10, 10, 11, 11, 11, 13, 13, 2, 2, 2, 8, 8, 8, 8, 9, 9, 9, 9, 3, 3,
                13, 7, 5, 6, 13, 14, 1, 14, 1];
  const MSG = {
    0x6653: "(Variable error)", 0x6655: "(Array error)",
    0x6657: "(String operation error)", 0x6659: "(Numeric value error)",
    0x665b: "(Right side assignment error)", 0x665d: "(Left side assignment error)",
    0x665f: "(Inconsistent parentheses error)", 0x6661: "(Array formula error)",
    0x6663: "(Operator error)",
  };

  const i32 = (v) => v | 0;

  // msvcrt wcstol(s, NULL, 10): leading whitespace, sign, digits; clamps.
  function wcstol(s) {
    if (s === null || s === undefined) return 0;
    const m = /^[ \t\n\r\f\v]*([+-]?)(\d+)/.exec(String(s));
    if (!m) return 0;
    let v = Number(m[2]);
    if (m[1] === "-") v = -v;
    return Math.max(-0x80000000, Math.min(0x7fffffff, v));
  }

  // rva 0x1c9122: optional ONE leading '-', then only ASCII digits. '' and '-' pass.
  function isNumStr(s) {
    if (s === null || s === undefined) return false;
    return /^-?[0-9]*$/.test(s);
  }

  // CPmlArray: either child arrays or data strings.
  class Arr {
    constructor() { this.children = null; this.data = null; }
    addData(s) {
      if (s.length >= 0x400) return;
      if (this.children === null && this.data === null) this.data = [];
      if (this.data !== null && this.data.length < 0x200) this.data.push(s);
    }
    addChild(a) {
      if (this.children === null) this.children = [];
      if (this.children.length < 0x200) this.children.push(a);
    }
    child(i) {
      if (this.children === null || i < 0 || i >= this.children.length) return null;
      return this.children[i];
    }
    value(i) {
      if (this.data === null || i < 0) return null;
      return i < this.data.length ? this.data[i] : "";
    }
    toList() {
      return this.children !== null ? this.children.map((c) => c.toList()) : (this.data || []).slice();
    }
  }
  // A nested list of strings (the server's variable table) as an Arr.
  function makeArray(list) {
    const a = new Arr();
    for (const v of list) {
      if (Array.isArray(v)) a.addChild(makeArray(v));
      else a.addData(String(v));
    }
    return a;
  }

  const pad2 = (n) => String(n).padStart(2, "0");
  // Recomputed on every read (rva 0x1ff4d9). The cursor ones come from the
  // runtime when it has a pointer.
  const DYNAMIC = {
    $_RND: () => String(Math.floor(Math.random() * 65536)),
    $_YEA: () => String(new Date().getFullYear()).padStart(4, "0"),
    $_MON: () => pad2(new Date().getMonth() + 1),
    $_DAT: () => pad2(new Date().getDate()),
    $_HOU: () => pad2(new Date().getHours()),
    $_MIN: () => pad2(new Date().getMinutes()),
    $_SEC: () => pad2(new Date().getSeconds()),
    $_WEE: () => String(new Date().getDay()),
  };

  // Variable store (rva 0x1ff01a set / 0x1fefb4 find).
  class Vars {
    constructor(init) {
      this.scalars = new Map();
      this.arrays = new Map();
      this.dynamic = Object.assign({}, DYNAMIC);
      if (init) this.load(init);
    }
    // {name: string | nested list} as /api/pml-expand hands it over.
    load(table) {
      for (const [k, v] of Object.entries(table || {})) {
        const name = k.startsWith("$") ? k : "$" + k;
        if (Array.isArray(v)) this.setArray(name, makeArray(v));
        else if (v !== null && v !== undefined) this.set(name, String(v));
      }
    }
    set(name, value) {
      if (!name || name.length >= 0x28 || (value !== null && value !== undefined && value.length >= 0x400)) return;
      if (name === "$ARG") {
        for (let d = 0; d < 10; d++) this.set("$ARG" + d, value);
        return;
      }
      this.arrays.delete(name);
      this.scalars.set(name, value);
    }
    setArray(name, arr) {
      this.scalars.delete(name);
      this.arrays.set(name, arr);
    }
    get(name) {
      const f = this.dynamic[name];
      if (f) return f();
      const v = this.scalars.get(name);
      return v === undefined ? null : v;
    }
    table() {
      const out = {};
      for (const [k, v] of this.scalars) out[k] = v;
      for (const [k, a] of this.arrays) out[k] = a.toList();
      return out;
    }
  }

  function node(t, v) { return { t, v: v === undefined ? null : v, idx: null, next: null, prev: null }; }

  // rva 0x1c978d. err=1 returns the marker text instead of null.
  function lookup(n, V, err) {
    if (n.idx === null) {
      const v = V.get(n.v);
      if (v === null && err) return MSG[0x6653];
      return v;
    }
    let arr = V.arrays.get(n.v);
    if (!arr) return err ? MSG[0x6653] : null;
    const idx = n.idx;
    for (let k = 0; k < idx.length; k++) {
      const i = idx[k];
      const last = k + 1 >= idx.length || idx[k + 1] < 0;
      if (last) {
        const v = arr.value(i);
        if (v === null && err) return MSG[0x6655];
        return v;
      }
      arr = arr.child(i);
      if (arr === null) return err ? MSG[0x6655] : null;
    }
    return err ? MSG[0x6655] : null;
  }

  // rva 0x1c90c7: scalar set, or array element set.
  function setVar(n, V, value) {
    if (n.idx === null) { V.set(n.v, value); return; }
    let arr = V.arrays.get(n.v) || null;
    const idx = n.idx;
    for (let k = 0; k < idx.length; k++) {
      const i = idx[k];
      if (arr === null) return;
      if (k + 1 >= idx.length || idx[k + 1] < 0) {
        if (i >= 0 && i < 0x200 && value.length < 0x400 && arr.children === null) {
          if (arr.data === null) arr.data = [];
          while (arr.data.length <= i) arr.data.push("");
          arr.data[i] = value;
        }
        return;
      }
      arr = arr.child(i);
    }
  }

  // ---------------------------------------------------------------- tokenizer
  const TWO = { "++": 8, "+=": 11, "--": 9, "-=": 12, "==": 13, "=~": 15, "!=": 14, "!~": 16,
                ">=": 18, "<=": 20, "&&": 21, "||": 22 };
  const ONE = { "+": 3, "-": 4, "=": 10, "!": 23, ">": 17, "<": 19, "&": 24, "|": 25,
                "*": 5, "/": 6, "%": 7, "^": 26, "~": 27, "(": 28, ")": 29, "[": 30, "]": 31 };
  const isAlpha = (c) => /^[A-Za-z]$/.test(c);
  const isName = (c) => c === "_" || /^[A-Za-z0-9]$/.test(c);

  // rva 0x1c94a4. Returns the token list wrapped in '(' ... ')'. Stops at ',',
  // NUL, or any character it does not know (TAB, CR, LF, '.', '?', ':', '#',
  // letters outside a string ...).
  function tokenize(s) {
    const toks = [node(28)];
    if (s.length < 1) toks.push(node(STR, ""));
    let i = 0;
    while (i < s.length) {
      const c = s[i], nx = s[i + 1] || "";
      if (c >= "0" && c <= "9") {
        let j = i, v = 0;
        while (j < s.length && s[j] >= "0" && s[j] <= "9") {
          v = i32(Math.imul(v, 10) + s.charCodeAt(j) - 48);
          j++;
        }
        toks.push(node(INT, v));
        i = j;
        continue;
      }
      if (c === "'" || c === '"') {
        let j = i + 1, prev = c, esc = false;
        const out = [];
        while (j < s.length) {
          const ch = s[j];
          if (!esc && prev === "\\" && !isAlpha(ch)) {
            // the backslash already emitted is dropped, ch is literal
            out.pop();
            out.push(ch);
            esc = true;
            prev = ch;
            j++;
            continue;
          }
          if (!esc && ch === c) break;
          out.push(ch);
          esc = false;
          prev = ch;
          j++;
        }
        toks.push(node(STR, out.join("")));
        i = j + 1;
        continue;
      }
      if ((c === "$" || c === "%") && nx && (nx === "_" || isAlpha(nx))) {
        let j = i + 1;
        while (j < s.length && j - i < 0x27 && isName(s[j])) j++;
        toks.push(node(VAR, s.slice(i, j)));
        i = j;
        continue;
      }
      if (c === "," || c === "\0") break;
      if ("+-=*/%!~&|^<>()[]".includes(c)) {
        const two = TWO[c + nx];
        if (two) { toks.push(node(two)); i += 2; }
        else { toks.push(node(ONE[c])); i += 1; }
        continue;
      }
      if (c === " ") { i++; continue; }
      break;                                   // "Unexpected code (%c)"
    }
    toks.push(node(29));
    for (let k = 0; k + 1 < toks.length; k++) { toks[k].next = toks[k + 1]; toks[k + 1].prev = toks[k]; }
    return toks;
  }

  // ---------------------------------------------------------------- evaluator
  function unlink(n) {
    if (!n) return;
    if (n.prev) n.prev.next = n.next;
    if (n.next) n.next.prev = n.prev;
    n.prev = n.next = null;
  }
  function insertErr(after, msgid) {
    const e = node(STR, MSG[msgid]);
    e.next = after.next;
    if (after.next) after.next.prev = e;
    e.prev = after;
    after.next = e;
    return e;
  }
  function isnum(n, V) {
    if (n.t === INT) return true;
    if (n.t === STR) return n.v !== null && isNumStr(n.v);
    if (n.t === VAR) return isNumStr(lookup(n, V, 0));
    return false;
  }
  function ival(n, V) {
    if (n.t === INT) return n.v;
    if (n.t === STR) return wcstol(n.v);
    if (n.t === VAR) return wcstol(lookup(n, V, 1));
    return 0;
  }
  function sval(n, V) {
    if (n.t === INT) { n.t = STR; n.v = String(n.v); return n.v; }
    if (n.t === STR) return n.v;
    if (n.t === VAR) return lookup(n, V, 1);
    return null;
  }
  const cmp = (a, b) => (a > b ? 1 : a < b ? -1 : 0);
  function idiv(a, b) {
    if (b === 0) return 0;
    const q = Math.floor(Math.abs(a) / Math.abs(b));
    return i32((a >= 0) === (b >= 0) ? q : -q);
  }
  const imod = (a, b) => (b === 0 ? 0 : i32(a - Math.imul(idiv(a, b), b)));
  const b2 = (x) => (x ? 1 : 0);
  const INTF = {
    3: (a, b) => i32(a + b), 4: (a, b) => i32(a - b), 5: (a, b) => Math.imul(a, b),
    6: idiv, 7: imod, 13: (a, b) => b2(a === b), 14: (a, b) => b2(a !== b),
    15: (a, b) => b2(a === b), 16: (a, b) => b2(a !== b),
    17: (a, b) => b2(a > b), 18: (a, b) => b2(a >= b), 19: (a, b) => b2(a < b), 20: (a, b) => b2(a <= b),
    21: (a, b) => b2(a !== 0 && b !== 0), 22: (a, b) => b2(a !== 0 || b !== 0),
    24: (a, b) => a & b, 25: (a, b) => a | b, 26: (a, b) => a ^ b,
  };
  const s2 = (x) => (x ? "1" : "0");
  const STRF = {
    3: (a, b) => a + b,
    13: (a, b) => s2(a === b), 14: (a, b) => s2(a !== b),
    17: (a, b) => s2(cmp(a, b) > 0), 18: (a, b) => s2(cmp(a, b) >= 0),
    19: (a, b) => s2(cmp(a, b) < 0), 20: (a, b) => s2(cmp(a, b) <= 0),
  };

  function binary(op, V) {                     // rva 0x1c98be
    const L = op.prev, R = op.next;
    if (!L || !R) return null;
    let res = op;
    if (isnum(L, V) && isnum(R, V)) {
      op.v = INTF[op.t](ival(L, V), ival(R, V)); op.t = INT;
    } else if ((L.t === STR || L.t === VAR) && (R.t === STR || R.t === VAR)) {
      const f = STRF[op.t];
      if (!f) {
        unlink(op);
        res = insertErr(L, 0x6657);
      } else {
        const a = sval(L, V), b = sval(R, V);
        op.v = a === null || b === null ? "(null)" : f(a, b);
        op.t = STR;
      }
    } else if ((L.t === INT || L.t === VAR) && (R.t === INT || R.t === VAR)) {
      op.v = INTF[op.t](ival(L, V), ival(R, V)); op.t = INT;
    }
    // else: operands are dropped and the operator node stays an operator
    unlink(L);
    unlink(R);
    return res;
  }
  function unaryPm(op, V) {                    // rva 0x1c99fc
    const R = op.next;
    if (!R) return null;
    if (op.prev && (op.prev.t === INT || op.prev.t === STR || op.prev.t === VAR)) return binary(op, V);
    if (R.t !== INT && R.t !== VAR) {
      unlink(op);
      const e = insertErr(R, 0x6659);
      unlink(R);
      return e;
    }
    op.v = INTF[op.t](0, ival(R, V)); op.t = INT;
    unlink(R);
    return op;
  }
  function unaryNt(op, V) {                    // rva 0x1c9a92
    const R = op.next;
    if (!R) return null;
    if (R.t !== INT && R.t !== VAR) {
      unlink(op);
      const e = insertErr(R, 0x6659);
      unlink(R);
      return e;
    }
    const x = ival(R, V);
    op.v = op.t === 23 ? b2(x === 0) : ~x; op.t = INT;
    unlink(R);
    return op;
  }
  function assign(op, V) {                     // rva 0x1c9b8f
    const R = op.next, L = op.prev;
    if (!R) return null;
    const kind = op.t;
    if (!L || L.t !== VAR) {
      unlink(L);
      const e = insertErr(op, 0x665d);
      unlink(R); unlink(op);
      return e;
    }
    if (R.t !== INT && R.t !== STR && R.t !== VAR) {
      unlink(L);
      const e = insertErr(op, 0x665b);
      unlink(R); unlink(op);
      return e;
    }
    const intAssign = (x) => {
      const cur = lookup(L, V, 0);
      if (cur !== null) {
        const c = wcstol(cur);
        if (kind === 11) x = i32(x + c);
        if (kind === 12) x = i32(c - x);
      }
      setVar(L, V, String(x));
    };
    const strAssign = (s) => {
      const cur = lookup(L, V, 1);
      if (cur === null) return;
      setVar(L, V, kind === 11 ? cur + s : s);
    };
    if (R.t === INT) intAssign(R.v);
    else if (R.t === STR) strAssign(R.v);
    else {
      const rv = lookup(R, V, 1);
      if (rv !== null && isNumStr(rv)) intAssign(wcstol(rv));
      else strAssign(rv);
    }
    unlink(R); unlink(op);
    return L;                                  // the value of `$x=..` is $x itself
  }
  function incdec(op, V) {
    const d = op.t === 8 ? 1 : -1;
    let v, post;
    if (op.prev && op.prev.t === VAR) { v = op.prev; post = true; }
    else if (op.next && op.next.t === VAR) { v = op.next; post = false; }
    else return null;
    const old = wcstol(lookup(v, V, 1));
    setVar(v, V, String(i32(old + d)));
    op.v = post ? old : i32(old + d); op.t = INT;
    unlink(v);
    return op;
  }

  const BIN = new Set([5, 6, 7, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26]);
  const OPERAND = (t) => t === INT || t === STR || t === VAR;

  function reduce(toks, V) {                   // rva 0x1c9d2b
    let cur = toks[0], steps = 0;
    while (cur) {
      if (++steps > 10000) return null;
      const t = cur.t, p = PREC[t];
      if (t !== 29 && t !== 31 && cur.next) {
        const nxt = cur.next;
        if (p < PREC[nxt.t]) { cur = nxt; continue; }
        const nn = nxt.next;
        if (nn && (p < PREC[nn.t] || ((t === 10 || t === 11 || t === 12) && p === PREC[nn.t]))) { cur = nn; continue; }
      }
      const prv = cur.prev;
      let res = "loop";
      if (OPERAND(t)) {
        if (!prv) return cur;
        if (prv.t !== 28) { cur = prv; continue; }
        unlink(cur);
        const e = insertErr(prv, 0x665f);
        unlink(prv);
        cur = e;
        continue;
      } else if (t === 3 || t === 4) res = unaryPm(cur, V);
      else if (BIN.has(t)) res = binary(cur, V);
      else if (t === 8 || t === 9) res = incdec(cur, V);
      else if (t === 10 || t === 11 || t === 12) res = assign(cur, V);
      else if (t === 23 || t === 27) res = unaryNt(cur, V);
      else if (t === 28 || t === 30) { cur = cur.next; continue; }
      else if (t === 29) {
        if (prv && OPERAND(prv.t) && prv.prev && prv.prev.t === 28) {
          unlink(prv.prev);
          unlink(cur);
          res = prv;
        } else {
          let e;
          if (prv) {
            e = insertErr(prv, 0x665f);
            unlink(prv.prev ? prv.prev : cur);
            unlink(prv);
          } else {
            e = insertErr(cur, 0x665f);
            unlink(cur);
          }
          cur = e;
          continue;
        }
      } else if (t === 31) {
        if (prv && OPERAND(prv.t) && prv.prev && prv.prev.t === 30 && prv.prev.prev && prv.prev.prev.t === VAR) {
          const v = prv.prev.prev;
          v.idx = (v.idx || []).concat([ival(prv, V)]);
          unlink(prv.prev);
          unlink(prv);
          unlink(cur);
          res = v;
        } else {
          unlink(cur);
          const e = insertErr(prv, 0x6661);
          unlink(prv);
          cur = e;
          continue;
        }
      }
      if (res === null) return null;
      cur = res.prev ? res.prev : res;
    }
    return null;
  }

  // rva 0x1ca163: the value as a string; '' on failure.
  function evalStr(expr, V) {
    const n = reduce(tokenize(String(expr)), V);
    if (!n) return "";
    if (n.t === INT) return String(n.v);
    if (n.t === STR) return n.v;
    if (n.t === VAR) { const v = sval(n, V); return v === null ? "" : v; }
    return "";
  }
  // rva 0x1ca0d7: the value as an integer.
  function evalInt(expr, V) {
    const n = reduce(tokenize(String(expr)), V);
    if (!n) return 0;
    if (n.t === INT) return n.v;
    if (n.t === STR) return wcstol(n.v);
    if (n.t === VAR) return ival(n, V);
    return 0;
  }
  // rva 0x1ca21e: evaluate only when the value has a quote or $X/%X.
  function hasTrigger(value) {
    let skip = false;
    for (let i = 0; i < value.length; i++) {
      const c = value[i], n = value[i + 1] || "";
      if (skip) skip = false;
      else if (c === "\\" || (c === "$" && n === "$")) skip = true;
      else if (c === "'" || c === '"' || ((c === "$" || c === "%") && n && (n === "_" || isAlpha(n)))) return true;
    }
    return false;
  }
  function auto(value, V) {
    value = String(value);
    if (hasTrigger(value)) return evalStr(value.slice(0, 0x400), V).slice(0, 0x3ff);
    return value.slice(0, 0x400);
  }
  // rva 0x1ca313: each {$...} becomes the value of its inside.
  function brace(value, V) {
    value = String(value);
    const out = [];
    let i = 0;
    while (i < value.length) {
      if (value[i] === "{" && value[i + 1] === "$" && value[i + 2] && (value[i + 2] === "_" || isAlpha(value[i + 2]))) {
        const j = value.indexOf("}", i + 2);
        if (j >= 0) {
          out.push(evalStr(value.slice(i + 1, j), V));
          i = j + 1;
          continue;
        }
      }
      out.push(value[i]);
      i++;
    }
    return out.join("").slice(0, 0x3ff);
  }

  // Text between tags (spec 6 of pml-engine-expressions.md): {$..} is filled
  // in only inside &var=, &pos= and &style=; then each &var=VALUE; becomes
  // AUTO(VALUE). {$x} in plain text stays as written.
  function expandText(text, V) {
    text = String(text);
    if (!text.includes("&")) return text;
    if (text.includes("{$")) {
      text = text.replace(/&(var|pos|style)=([^;&]*);/gi, (m, k, v) => "&" + k + "=" + brace(v, V) + ";");
    }
    return text.replace(/&var=([^;]*);/gi, (m, v) => auto(v, V));
  }

  global.PMLExpr = { expandText, Vars, Arr, makeArray, tokenize, evalStr, evalInt, auto, brace, hasTrigger,
                     wcstol, isNumStr, MSG };
})(typeof window !== "undefined" ? window : globalThis);
