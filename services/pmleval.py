#!/usr/bin/env python3
"""A best-effort evaluator for PlayOnline PML's template layer.

Why this exists: many of SE's PML pages carry NO positioned content. They are
template *programs* -- `<define>` variables, `<array>` data, `<for>` loops and
`<if>/<elsif>/<else>` conditionals that GENERATE the positioned `<text>`/`<sheet>`
/`<img>` markup at run time, pulling values in with `&var=$x;` / `&calc=expr;`.
The admin preview's renderer (admin_web/pml.js) only draws already-positioned
markup, so those pages show blank.

This module runs the template layer server-side and returns FLATTENED PML --
loops unrolled, conditionals resolved, variables substituted, `<include>`s
inlined -- which the existing renderer can then draw.

It is deliberately best-effort, NOT a faithful reimplementation of SE's engine:
it evaluates the constructs the mirror actually uses and degrades gracefully
(an expression it cannot evaluate is left as-is or skipped, never raised). It is
a PREVIEW aid, so "mostly right and never crashes" beats "perfect or nothing".

Entry point: `expand(text, resolve_include=None, sysvars=None, base=None)`.
`resolve_include(src, base)` returns `(text, base_of_that_file)`, or None.
"""
import re

# System variables the pages branch on. Defaults chosen to render the Western
# desktop view, which is what the admin operator wants to see.
DEFAULT_SYSVARS = {
    "_PLATFORM": "WIN",
    "_USER_LANG": "en-US",
    "_POL_LOGIN": 0,
    "_POL_UCS_AREA_KBN": "02",     # Western area (00/01 are JP/US-special)
    "LANG": "en-US",
    "AREA": "02",
}

_MAX_LOOP = 512        # runaway-loop guard
_MAX_INCLUDE_DEPTH = 8


# --------------------------------------------------------------------------- #
# a lenient PML parse (shared shape with pml.js: {tag, attrs, children, text})
# --------------------------------------------------------------------------- #
def _strip_comments(s):
    s = re.sub(r"<!--[\s\S]*?-->", "", s)
    # SE's `<! ... >` short comment. This used to demand a NON-ALPHA after the
    # `<!`, to protect a hypothetical real tag -- but no PML tag starts with
    # `<!`, and SE writes plenty of `<!SHEET NAME>`, `<!URL ...>` and (in
    # `pml/info/style.pml`) a bare `<!style>`. Those survived the strip, then
    # failed to match the tag pattern too, so the parser dropped the `<` and
    # emitted `!style>` as TEXT -- taking the rest of that file's markup with
    # it. Strip every `<!...>`, exactly as admin_web/pml.js does.
    return re.sub(r"<![^>]*>", "", s)


_ATTR_RE = re.compile(r'([\w:.-]+)\s*(?:=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+)))?')
_TOK_RE = re.compile(r"<\/?[\w:.-]+[^>]*?>|[^<]+")
_VOID = {
    "input", "img", "meta", "formaction", "define", "style", "br", "bgsound",
    "include", "timer", "textbox", "area", "addmenu", "addlink", "config",
    "plugin", "hidden", "bar", "systembg", "inlineimg", "multilink",
}


def _parse_attrs(s):
    attrs = {}
    for m in _ATTR_RE.finditer(s):
        k = m.group(1).lower()
        v = (m.group(3) if m.group(3) is not None
             else m.group(4) if m.group(4) is not None
             else m.group(5) if m.group(5) is not None else "")
        attrs[k] = v
    return attrs


def _parse(src):
    src = _strip_comments(src)
    root = {"tag": "#root", "attrs": {}, "children": []}
    stack = [root]
    for m in _TOK_RE.finditer(src):
        tok = m.group(0)
        if tok[0] != "<":
            stack[-1]["children"].append({"tag": "#text", "text": tok,
                                          "children": []})
            continue
        if tok.startswith("</"):
            name = re.split(r"[\s>]", tok[2:], 1)[0].lower()
            for i in range(len(stack) - 1, 0, -1):
                if stack[i]["tag"] == name:
                    del stack[i:]
                    break
            continue
        name = re.split(r"[\s/>]", tok[1:], 1)[0].lower()
        rest = tok[1 + len(name):]
        rest = re.sub(r"/?>$", "", rest)
        node = {"tag": name, "attrs": _parse_attrs(rest), "children": []}
        stack[-1]["children"].append(node)
        if not (tok.endswith("/>") or name in _VOID):
            stack.append(node)
    return root


# --------------------------------------------------------------------------- #
# expression evaluation -- SE's C-ish mini language -> restricted python eval
# --------------------------------------------------------------------------- #
def _to_py(expr):
    """Translate an SE expression to a python one. `$name` -> V['name']."""
    e = expr
    # logical / not, without mangling != or <= >=
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"!(?!=)", " not ", e)
    # $identifier -> V['identifier']  (indexing like $a[$i] survives: the [...]
    # stays attached to the V['a'] lookup)
    e = re.sub(r"\$([A-Za-z_]\w*)", r"V['\1']", e)
    return e


class _Vars(dict):
    """The variable map, which remembers what a page asked for and did not have.

    A fragment meant to be `<include>`d evaluates to NOTHING on its own: its
    `<for cond="$i<$sht">` and `<if expr="$length!=0">` guards read variables the
    HOST page defines, so every branch is skipped and the preview draws an empty
    stage. Recording the misses here -- rather than at each of the dozen `_eval`
    call sites -- lets the preview say WHICH variables are missing, which is the
    difference between "the renderer is broken" and "open the page that includes
    this one".
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.missing = set()

    def __missing__(self, key):
        self.missing.add(key)
        raise KeyError(key)


def _eval(expr, V):
    """Evaluate a read expression. Returns the value, or None on failure."""
    try:
        return eval(_to_py(expr), {"__builtins__": {}}, {"V": V})  # noqa: S307
    except Exception:
        return None


def _assign(expr, V):
    """Apply an assignment / mutation used in <define calc=> and <for next=>.

    Handles  $x=EXPR  |  $x+=EXPR  |  $x-=EXPR  |  $x++  |  $x-- .
    """
    expr = expr.strip()
    m = re.match(r"\$([A-Za-z_]\w*)\s*(\+\+|--)$", expr)
    if m:
        name, op = m.group(1), m.group(2)
        V[name] = (V.get(name, 0) or 0) + (1 if op == "++" else -1)
        return
    m = re.match(r"\$([A-Za-z_]\w*)\s*(\+=|-=|=)\s*(.+)$", expr, re.S)
    if not m:
        return
    name, op, rhs = m.group(1), m.group(2), m.group(3)
    val = _eval(rhs, V)
    if op == "=":
        V[name] = val
    elif val is not None:
        try:
            V[name] = (V.get(name, 0) or 0) + (val if op == "+=" else -val)
        except Exception:
            V[name] = val


# --------------------------------------------------------------------------- #
# building <array> literals into python nested lists
# --------------------------------------------------------------------------- #
def _build_array(node, V):
    """An <array> is either a list of <array> rows or a flat list of scalars.

    Scalars are the quoted strings / bare tokens in the record text; nested
    <array> children become sub-lists. An `<if>` chain inside an array picks
    which rows/scalars are included.
    """
    rows = []
    scalars = []
    for c in _resolve_conditionals(node["children"], V):
        if c["tag"] == "array":
            rows.append(_build_array(c, V))
        elif c["tag"] == "#text":
            for lit in re.findall(r'"([^"]*)"|\'([^\']*)\'', c["text"]):
                scalars.append(lit[0] or lit[1])
    return rows if rows else scalars


# --------------------------------------------------------------------------- #
# conditionals: turn an if/elsif/.../else chain into the chosen children
# --------------------------------------------------------------------------- #
def _resolve_conditionals(children, V):
    """Return `children` with each if/elsif/else chain replaced by the one
    branch whose condition holds. Non-conditional nodes pass through."""
    out = []
    i = 0
    n = len(children)
    while i < n:
        c = children[i]
        if c["tag"] == "if":
            # gather the chain: this <if>, then contiguous <elsif>/<else>
            chain = [c]
            j = i + 1
            while j < n and children[j]["tag"] in ("elsif", "else"):
                chain.append(children[j])
                j += 1
            chosen = None
            for br in chain:
                if br["tag"] == "else":
                    chosen = br
                    break
                if _eval(br["attrs"].get("expr", ""), V):
                    chosen = br
                    break
            if chosen is not None:
                out.extend(chosen["children"])
            i = j
        elif c["tag"] in ("elsif", "else"):
            i += 1                       # stray branch without an <if>; drop
        else:
            out.append(c)
            i += 1
    return out


# --------------------------------------------------------------------------- #
# substituting &var= / &calc= inside emitted text and attribute values
# --------------------------------------------------------------------------- #
def _subst(s, V):
    def var(m):
        v = V.get(m.group(1))
        return "" if v is None else str(v)
    s = re.sub(r"&var=\$([A-Za-z_]\w*);", var, s)

    def calc(m):
        v = _eval(m.group(1), V)
        return "" if v is None else str(v)
    s = re.sub(r"&calc=([^;]*);", calc, s)
    return s


_EXPR_HINT = re.compile(r"\$[A-Za-z_]")


def _split_top_commas(s):
    """Split on commas that are NOT inside quotes, parens or brackets.

    `pos="$BN_X+5,$BN_Y+($BN_Ysp*$i)"` is two expressions; `$P+'a,b.png'` is one.
    """
    parts, buf, depth, quote = [], [], 0, ""
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _fmt(v):
    """Render an evaluated value the way an attribute wants it -- 12, not 12.0."""
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _eval_attr(value, V):
    """Evaluate an attribute value that is a bare SE expression.

    Attribute values are not only `&var=` templates. SE writes bare expressions:
    `src="$F_PATH1+'in02.pml'"`, `background="$F_PATH1+'img_s/bg01s.png'"`,
    `pos="$BN_X+5,$BN_Y+($BN_Ysp*$i)"` -- and the mirror uses them for nearly
    every src, pos and size it has. Passed through literally, every include
    misses, every art path 404s and every element stacks at 0,0, which is what
    the preview's "nothing renders" actually was.

    `pos`/`size` are comma-separated COMPONENT expressions, so split on
    top-level commas and evaluate each. If any component fails to evaluate, the
    original value is returned untouched -- `onclick="sd:show=1@$x"` is not
    arithmetic, and half-substituting it is worse than leaving it alone.
    """
    value = _subst(value, V)
    if not _EXPR_HINT.search(value):
        return value
    outs = []
    for part in _split_top_commas(value):
        got = _eval(part.strip(), V)
        if got is None:
            return value
        outs.append(_fmt(got))
    return ",".join(outs)


def _emit_attrs(attrs, V):
    out = ""
    for k, v in attrs.items():
        out += f' {k}="{_eval_attr(v, V)}"'
    return out


# --------------------------------------------------------------------------- #
# the evaluator: walk the tree, execute control flow, emit flattened PML
# --------------------------------------------------------------------------- #
def _walk(children, V, out, ctx):
    for c in _resolve_conditionals(children, V):
        t = c["tag"]
        if t == "#text":
            out.append(_subst(c["text"], V))
            continue
        if t == "define":
            name = c["attrs"].get("name", "").lstrip("$")
            if not name:
                continue
            if "calc" in c["attrs"]:
                _assign(f"${name}={c['attrs']['calc']}", V)
                # calc may itself be an assignment form ($x+=..); support both
                if name not in V or V.get(name) is None:
                    _assign(f"${name} {c['attrs'].get('calc')}", V)
            elif "value" in c["attrs"]:
                raw = c["attrs"]["value"]
                # numbers stay numbers so arithmetic downstream works
                V[name] = int(raw) if re.fullmatch(r"-?\d+", raw) else _subst(raw, V)
            elif "nodefvalue" in c["attrs"] and name not in V:
                # `nodefvalue` is what to use when the CALLER passed nothing.
                # The preview has no caller, so it IS the value. Ignoring it
                # left `<define name="$cnt" nodefvalue="0">` as "", and the
                # page's `background="$BACKIMAGE[$cnt]"` then indexed an array
                # with a string and fell back to unrenderable literal markup.
                raw = c["attrs"]["nodefvalue"]
                V[name] = int(raw) if re.fullmatch(r"-?\d+", raw) else _subst(raw, V)
            else:
                V.setdefault(name, "")
            continue
        if t == "array":
            name = c["attrs"].get("name", "").lstrip("$")
            if name:
                V[name] = _build_array(c, V)
            continue
        if t == "for":
            a = c["attrs"]
            if a.get("init"):
                _assign(a["init"], V)
            guard = 0
            while _eval(a.get("cond", "0"), V) and guard < _MAX_LOOP:
                _walk(c["children"], V, out, ctx)
                if a.get("next"):
                    _assign(a["next"], V)
                guard += 1
            continue
        if t == "include":
            # The src is almost always an expression (`$F_PATH1+'in02.pml'`), so
            # it has to be evaluated before anything can look the file up.
            src = _eval_attr(c["attrs"].get("src", ""), V)
            got = ctx["resolve"](src, ctx["base"]) if ctx["resolve"] else None
            if not got and src:
                ctx["unresolved"].add(src)
            if got and ctx["depth"] < _MAX_INCLUDE_DEPTH:
                inc, base = got
                sub = _parse(inc)
                ctx["depth"] += 1
                # an included file's own relative includes are relative to IT
                outer, ctx["base"] = ctx["base"], base
                _walk(sub["children"], V, out, ctx)
                ctx["base"], ctx["depth"] = outer, ctx["depth"] - 1
            continue
        if t in ("head", "record", "data"):
            # keep <data>/<style> in the output so the renderer's collect() sees
            # them, but still substitute + recurse for nested control flow.
            out.append(f"<{t}{_emit_attrs(c['attrs'], V)}>")
            _walk(c["children"], V, out, ctx)
            out.append(f"</{t}>")
            continue
        # a normal (possibly positioned) element: emit it, recurse into children
        selfclose = c["tag"] in _VOID and not c["children"]
        out.append(f"<{t}{_emit_attrs(c['attrs'], V)}>")
        if not selfclose:
            _walk(c["children"], V, out, ctx)
            out.append(f"</{t}>")


def expand(text, resolve_include=None, sysvars=None, base=None, report=None):
    """Flatten a PML template: run its define/array/for/if layer and inline
    includes, returning positioned PML the preview renderer can draw.

    Never raises on a page's own content -- a construct it cannot evaluate is
    passed through or skipped.

    `resolve_include(src, base)` returns `(text, base_of_that_file)` for an
    included file, or None. `base` is whatever token the caller uses to root a
    relative src (admin.py passes the file's directory); it is handed back for
    each included file so that file's own relative includes resolve against it.

    `report`, if given, is filled with `missing` (variables the text read and
    this text does not define -- the mark of a fragment whose host supplies
    them) and `unresolved` (include srcs no file was found for). Both are how
    the preview explains an empty stage instead of just showing one.
    """
    V = _Vars(DEFAULT_SYSVARS)
    if sysvars:
        V.update(sysvars)
    ctx = {"resolve": resolve_include, "depth": 0, "base": base,
           "unresolved": set()}
    try:
        root = _parse(text)
        out = []
        _walk(root["children"], V, out, ctx)
        return "".join(out)
    except Exception as exc:                      # never break the preview
        return f"<!-- pmleval failed: {exc} -->\n{text}"
    finally:
        if report is not None:
            report["missing"] = sorted(V.missing)
            report["unresolved"] = sorted(ctx["unresolved"])
