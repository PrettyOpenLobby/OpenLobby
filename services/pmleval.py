#!/usr/bin/env python3
"""The PlayOnline Viewer's PML template engine, for the admin preview.

Why this exists: many of SE's PML pages carry NO positioned content. They are
template *programs* -- `<define>` variables, `<array>` data, `<for>` loops and
`<if>/<elsif>/<else>` markers that GENERATE the positioned `<text>`/`<sheet>`
/`<img>` markup at run time, pulling values in with `&var=...;`. The admin
preview's renderer (admin_web/pml.js) only draws already-positioned markup, so
this module runs the template layer server-side and returns FLATTENED PML --
loops unrolled, conditionals resolved, attributes evaluated, `<include>`s
inlined -- which the renderer can then draw.

It follows the PC Viewer (app.dll 1.18.15e) as reverse-engineered in
PlayOnline/docs/notes/pc-viewer/pml-engine-expressions.md ("the spec" below;
section numbers refer to it). The expression engine is a port of the machine
code (the spec's pml_expr_ref.py): every value is a string, each operator
decides per call whether it works on numbers or strings, an undefined variable
reads as "(Variable error)" (0 in a number), and a malformed expression yields
the same error text the Viewer puts on screen. The page walker is a streaming
parser like the Viewer's: `<if>`/`<elsif>`/`<else>` are markers on a bit stack,
`<for>` re-parses its body from the saved source position, and `<include>` runs
inline where it stands.

What the preview does that the Viewer does not:
  * `report["missing"]` names the variables the page read while they were
    undefined. For a fragment (no <body>) those are what the including page
    defines, and the UI says so. It never changes a result.
  * Attributes the spec does not map to an evaluator (section 5 lists the ones
    it read) are treated as comma-separated AUTO values.
  * Unknown tags are passed through for the renderer instead of being dropped,
    and every element is closed at the end of the file that opened it.

Entry point: `expand(text, resolve_include=None, sysvars=None, base=None,
report=None, url=None)`. `resolve_include(src, base)` returns
`(text, base_of_that_file)`, or None.
"""
import bisect
import datetime
import re

# =========================================================================== #
# the expression engine -- a port of app.dll's evaluator (spec sections 1-4)
# =========================================================================== #
INT, STR, VAR = 0, 1, 2
# .data rva 0x4a8fd0, indexed by token type (spec section 3)
_PREC = [0, 0, 0, 10, 10, 11, 11, 11, 13, 13, 2, 2, 2, 8, 8, 8, 8, 9, 9, 9, 9, 3, 3,
         13, 7, 5, 6, 13, 14, 1, 14, 1]

#: EU/EN/StringTable.bin -- the only evaluator messages that reach a page.
_MSG = {
    0x6653: "(Variable error)", 0x6655: "(Array error)",
    0x6657: "(String operation error)", 0x6659: "(Numeric value error)",
    0x665b: "(Right side assignment error)", 0x665d: "(Left side assignment error)",
    0x665f: "(Inconsistent parentheses error)", 0x6661: "(Array formula error)",
    0x6663: "(Operator error)",
}
VARIABLE_ERROR = _MSG[0x6653]

_WCSTOL_RE = re.compile(r"[ \t\n\r\f\v]*([+-]?)(\d+)")


def _i32(v):
    v &= 0xffffffff
    return v - 0x100000000 if v & 0x80000000 else v


def wcstol(s):
    """msvcrt wcstol(s, NULL, 10): leading whitespace, sign, digits; clamps."""
    if not s:
        return 0
    m = _WCSTOL_RE.match(s)
    if not m:
        return 0
    v = int(m.group(2))
    v = -v if m.group(1) == "-" else v
    return max(-0x80000000, min(0x7fffffff, v))


def is_num_str(s):
    """rva 0x1c9122: one optional leading '-', then only ASCII digits.
    '' and '-' pass; ' 1', '+1', '1.5' do not."""
    if s is None:
        return False
    i = 1 if s[:1] == "-" else 0
    return all("0" <= c <= "9" for c in s[i:])


class _Node:
    __slots__ = ("t", "v", "idx", "next", "prev")

    def __init__(self, t, v=None):
        self.t, self.v, self.idx, self.next, self.prev = t, v, None, None, None


class Arr:
    """CPmlArray: either child arrays or data strings, never both."""

    __slots__ = ("children", "data")

    def __init__(self):
        self.children = None
        self.data = None

    def add_data(self, s):           # rva 0x200a5c
        if len(s) >= 0x400:
            return
        if self.children is None and self.data is None:
            self.data = []
        if self.data is not None and len(self.data) < 0x200:
            self.data.append(s)

    def add_child(self, a):          # rva 0x200a1f
        if self.children is None:
            self.children = []
        if len(self.children) < 0x200:
            self.children.append(a)

    def child(self, i):              # rva 0x1ed072
        if self.children is None or i < 0 or i >= len(self.children):
            return None
        return self.children[i]

    def value(self, i):              # rva 0x1ed0a5: past the end is "", not an error
        if self.data is None or i < 0:
            return None
        return self.data[i] if i < len(self.data) else ""

    def to_list(self):
        if self.children is not None:
            return [c.to_list() for c in self.children]
        return list(self.data or [])


def make_array(value):
    """A Python list (nested for rows) as an Arr -- for callers and tests."""
    a = Arr()
    for v in value:
        if isinstance(v, (list, tuple)):
            a.add_child(make_array(v))
        else:
            a.add_data(_as_str(v))
    return a


def _as_str(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return "%d" % v
    return "" if v is None else str(v)


def _clock():
    return datetime.datetime.now()


class Vars:
    """The variable store. Every value is a string; names are case-sensitive
    and include the sigil ($x). Scalars and arrays share one namespace.

    `missing` collects the names an evaluation READ while they were undefined
    (the lookups that produce "(Variable error)"), never the probes that only
    ask whether something exists."""

    def __init__(self, init=None):
        self.scalars = {}
        self.arrays = {}
        self.missing = set()
        for k, v in (init or {}).items():
            self.set(k, v)

    def set(self, name, value):      # rva 0x1ff01a: too long -> silently not stored
        if not name or len(name) >= 0x28 or (value is not None and len(value) >= 0x400):
            return
        if name == "$ARG":           # rva 0x1fef61: $ARG sets $ARG0..$ARG9
            for d in "0123456789":
                self.set("$ARG" + d, value)
            return
        self.arrays.pop(name, None)
        self.scalars[name] = value

    def set_array(self, name, arr):
        if not name or len(name) >= 0x28:
            return
        self.scalars.pop(name, None)
        self.arrays[name] = arr

    def get(self, name):
        v = self.scalars.get(name)
        if v is None and name.startswith("$_"):
            v = _dynamic_sysvar(name)
        return v

    def note_missing(self, name):
        if name.startswith("$") and not name.startswith("$_"):
            self.missing.add(name[1:])


def _dynamic_sysvar(name):
    """rva 0x1ff4d9: the `$_` names recomputed on every read (spec section 7)."""
    k = name[2:]
    if k in ("YEA", "MON", "DAT", "HOU", "MIN", "SEC", "WEE"):
        t = _clock()
        return {"YEA": "%04d" % t.year, "MON": "%02d" % t.month,
                "DAT": "%02d" % t.day, "HOU": "%02d" % t.hour,
                "MIN": "%02d" % t.minute, "SEC": "%02d" % t.second,
                "WEE": "%d" % ((t.weekday() + 1) % 7)}[k]
    if re.fullmatch(r"HAVE_CONTENTSID_\d{4}", k):
        return "1"                   # the preview owns every content id
    return None


def _lookup(node, V, err):
    """rva 0x1c978d. With err, a failure returns the marker text."""
    if node.idx is None:
        v = V.get(node.v)
        if v is None and err:
            V.note_missing(node.v)
            return VARIABLE_ERROR
        return v
    arr = V.arrays.get(node.v)
    if arr is None:
        if err:
            V.note_missing(node.v)
            return VARIABLE_ERROR
        return None
    idx = node.idx
    for k, i in enumerate(idx):
        if k + 1 >= len(idx) or idx[k + 1] < 0:
            v = arr.value(i)
            if v is None and err:
                return _MSG[0x6655]
            return v
        arr = arr.child(i)
        if arr is None:
            return _MSG[0x6655] if err else None
    return _MSG[0x6655] if err else None


def _set_var(node, V, value):
    """rva 0x1c90c7: a scalar, or an array element (padded with "")."""
    if node.idx is None:
        V.set(node.v, value)
        return
    arr = V.arrays.get(node.v)
    idx = node.idx
    for k, i in enumerate(idx):
        if arr is None:
            return
        if k + 1 >= len(idx) or idx[k + 1] < 0:
            if 0 <= i < 0x200 and len(value) < 0x400 and arr.children is None:
                if arr.data is None:
                    arr.data = []
                while len(arr.data) <= i:
                    arr.data.append("")
                arr.data[i] = value
            return
        arr = arr.child(i)


# --------------------------------------------------------------- tokenizer
_TWO = {("+", "+"): 8, ("+", "="): 11, ("-", "-"): 9, ("-", "="): 12,
        ("=", "="): 13, ("=", "~"): 15, ("!", "="): 14, ("!", "~"): 16,
        (">", "="): 18, ("<", "="): 20, ("&", "&"): 21, ("|", "|"): 22}
_ONE = {"+": 3, "-": 4, "=": 10, "!": 23, ">": 17, "<": 19, "&": 24, "|": 25,
        "*": 5, "/": 6, "%": 7, "^": 26, "~": 27, "(": 28, ")": 29, "[": 30, "]": 31}


def _is_alpha(c):
    return ("A" <= c <= "Z") or ("a" <= c <= "z")


def _is_name(c):
    return c == "_" or ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9")


def _tokenize(s):
    """rva 0x1c94a4. A node list wrapped in '(' ... ')'. Stops silently at ',',
    NUL, or any character it does not know (TAB, CR, LF, '.', '?', ':', '#',
    letters outside a string, ...)."""
    toks = [_Node(28)]
    if not s:
        toks.append(_Node(STR, ""))
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        nx = s[i + 1] if i + 1 < n else ""
        if "0" <= c <= "9":
            j, v = i, 0
            while j < n and "0" <= s[j] <= "9":
                v = _i32(v * 10 + ord(s[j]) - 48)
                j += 1
            toks.append(_Node(INT, v))
            i = j
            continue
        if c == "'" or c == '"':
            j, out, prev, esc = i + 1, [], c, False
            while j < n:
                ch = s[j]
                if not esc and prev == "\\" and not _is_alpha(ch):
                    out.pop()                   # the backslash is dropped
                    out.append(ch)
                    esc, prev = True, ch
                    j += 1
                    continue
                if not esc and ch == c:
                    break
                out.append(ch)
                esc, prev = False, ch
                j += 1
            toks.append(_Node(STR, "".join(out)))
            i = j + 1
            continue
        if (c == "$" or c == "%") and nx and (nx == "_" or _is_alpha(nx)):
            j = i + 1
            while j < n and j - i < 0x27 and _is_name(s[j]):
                j += 1
            toks.append(_Node(VAR, s[i:j]))
            i = j
            continue
        if c == "," or c == "\0":
            break
        if c in _ONE:
            t = _TWO.get((c, nx))
            if t is not None:
                toks.append(_Node(t))
                i += 2
            else:
                toks.append(_Node(_ONE[c]))
                i += 1
            continue
        if c == " ":
            i += 1
            continue
        break                                   # "Unexpected code (%c)"
    toks.append(_Node(29))
    for a, b in zip(toks, toks[1:]):
        a.next, b.prev = b, a
    return toks


# --------------------------------------------------------------- reducer
def _unlink(n):                                 # rva 0x1c8dfd
    if n is None:
        return
    if n.prev is not None:
        n.prev.next = n.next
    if n.next is not None:
        n.next.prev = n.prev
    n.prev = n.next = None


def _insert_err(after, msgid):                  # rva 0x1c8dc8
    e = _Node(STR, _MSG[msgid])
    e.next = after.next
    if after.next is not None:
        after.next.prev = e
    e.prev = after
    after.next = e
    return e


def _isnum(n, V):                               # rva 0x1c988c
    if n.t == INT:
        return True
    if n.t == STR:
        return n.v is not None and is_num_str(n.v)
    if n.t == VAR:
        return is_num_str(_lookup(n, V, 0))
    return False


def _ival(n, V):                                # rva 0x1c9835
    if n.t == INT:
        return n.v
    if n.t == STR:
        return wcstol(n.v)
    if n.t == VAR:
        return wcstol(_lookup(n, V, 1))
    return 0


def _sval(n, V):                                # rva 0x1c9862
    if n.t == INT:
        n.t, n.v = STR, "%d" % n.v
        return n.v
    if n.t == STR:
        return n.v
    if n.t == VAR:
        return _lookup(n, V, 1)
    return None


def _cmp(a, b):
    return (a > b) - (a < b)


def _idiv(a, b):                                # rva 0x1c8f52: /0 is 0
    if b == 0:
        return 0
    q = abs(a) // abs(b)
    return _i32(q if (a >= 0) == (b >= 0) else -q)


def _imod(a, b):                                # rva 0x1c8f66: %0 is 0
    if b == 0:
        return 0
    return _i32(a - _idiv(a, b) * b)


_INTF = {3: lambda a, b: _i32(a + b), 4: lambda a, b: _i32(a - b),
         5: lambda a, b: _i32(a * b), 6: _idiv, 7: _imod,
         13: lambda a, b: int(a == b), 14: lambda a, b: int(a != b),
         15: lambda a, b: int(a == b), 16: lambda a, b: int(a != b),
         17: lambda a, b: int(a > b), 18: lambda a, b: int(a >= b),
         19: lambda a, b: int(a < b), 20: lambda a, b: int(a <= b),
         21: lambda a, b: int(a != 0 and b != 0), 22: lambda a, b: int(a != 0 or b != 0),
         24: lambda a, b: _i32(a & b), 25: lambda a, b: _i32(a | b),
         26: lambda a, b: _i32(a ^ b)}
# The string forms: only + (concatenate) and the six comparisons (wcscmp,
# which is UTF-16 code-unit order -- Python's order for BMP text).
_STRF = {3: lambda a, b: a + b,
         13: lambda a, b: "1" if a == b else "0", 14: lambda a, b: "1" if a != b else "0",
         17: lambda a, b: "1" if _cmp(a, b) > 0 else "0",
         18: lambda a, b: "1" if _cmp(a, b) >= 0 else "0",
         19: lambda a, b: "1" if _cmp(a, b) < 0 else "0",
         20: lambda a, b: "1" if _cmp(a, b) <= 0 else "0"}


def _binary(op, V):                             # rva 0x1c98be
    L, R = op.prev, op.next
    if L is None or R is None:
        return None
    res = op
    if _isnum(L, V) and _isnum(R, V):
        op.t, op.v = INT, _INTF[op.t](_ival(L, V), _ival(R, V))
    elif L.t in (STR, VAR) and R.t in (STR, VAR):
        f = _STRF.get(op.t)
        if f is None:
            _unlink(op)
            res = _insert_err(L, 0x6657)
        else:
            a, b = _sval(L, V), _sval(R, V)
            op.t, op.v = STR, ("(null)" if a is None or b is None else f(a, b))
    elif L.t in (INT, VAR) and R.t in (INT, VAR):
        op.t, op.v = INT, _INTF[op.t](_ival(L, V), _ival(R, V))
    # else (a quoted string next to an integer literal): the operands are
    # dropped and the operator node stays an operator
    _unlink(L)
    _unlink(R)
    return res


def _unary_pm(op, V):                           # rva 0x1c99fc
    R = op.next
    if R is None:
        return None
    if op.prev is not None and op.prev.t in (INT, STR, VAR):
        return _binary(op, V)
    if R.t not in (INT, VAR):
        _unlink(op)
        e = _insert_err(R, 0x6659)
        _unlink(R)
        return e
    op.t, op.v = INT, _INTF[op.t](0, _ival(R, V))
    _unlink(R)
    return op


def _unary_nt(op, V):                           # rva 0x1c9a92
    R = op.next
    if R is None:
        return None
    if R.t not in (INT, VAR):
        _unlink(op)
        e = _insert_err(R, 0x6659)
        _unlink(R)
        return e
    x = _ival(R, V)
    op.t, op.v = INT, (int(x == 0) if op.t == 23 else _i32(~x))
    _unlink(R)
    return op


def _assign(op, V):                             # rva 0x1c9b8f
    R, L = op.next, op.prev
    if R is None:
        return None
    kind = op.t
    if L is None or L.t != VAR:
        _unlink(L)
        e = _insert_err(op, 0x665d)
        _unlink(R)
        _unlink(op)
        return e
    if R.t not in (INT, STR, VAR):
        _unlink(L)
        e = _insert_err(op, 0x665b)
        _unlink(R)
        _unlink(op)
        return e

    def int_assign(x):                          # rva 0x1c9afb
        cur = _lookup(L, V, 0)
        if cur is not None:
            c = wcstol(cur)
            if kind == 11:
                x = _i32(x + c)
            if kind == 12:
                x = _i32(c - x)
        _set_var(L, V, "%d" % x)

    def str_assign(s):                          # rva 0x1c9b4c
        cur = _lookup(L, V, 1)
        if cur is None:
            return
        _set_var(L, V, cur + s if kind == 11 else s)

    if R.t == INT:
        int_assign(R.v)
    elif R.t == STR:
        str_assign(R.v)
    else:
        rv = _lookup(R, V, 1)
        if rv is not None and is_num_str(rv):
            int_assign(wcstol(rv))
        else:
            str_assign(rv)
    _unlink(R)
    _unlink(op)
    return L                                    # `$x=..` is worth $x itself


def _incdec(op, V):                             # rva 0x1c9cbf / 0x1c9cf5 / 0x1c9c61
    d = 1 if op.t == 8 else -1
    if op.prev is not None and op.prev.t == VAR:
        var, post = op.prev, True
    elif op.next is not None and op.next.t == VAR:
        var, post = op.next, False
    else:
        return None
    old = wcstol(_lookup(var, V, 1))
    _set_var(var, V, "%d" % _i32(old + d))
    op.t, op.v = INT, (old if post else _i32(old + d))
    _unlink(var)
    return op


_BINOPS = frozenset((5, 6, 7, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26))


def _reduce(toks, V):                           # rva 0x1c9d2b
    cur = toks[0]
    steps = 0
    while cur is not None:
        steps += 1
        if steps > 20000:
            return None
        t = cur.t
        p = _PREC[t]
        if t != 29 and t != 31 and cur.next is not None:
            nxt = cur.next
            if p < _PREC[nxt.t]:
                cur = nxt
                continue
            nn = nxt.next
            if nn is not None and (p < _PREC[nn.t] or (t in (10, 11, 12) and p == _PREC[nn.t])):
                cur = nn
                continue
        prv = cur.prev
        res = None
        if t in (INT, STR, VAR):
            if prv is None:
                return cur
            if prv.t != 28:
                cur = prv
                continue
            _unlink(cur)
            e = _insert_err(prv, 0x665f)
            _unlink(prv)
            cur = e
            continue
        elif t == 3 or t == 4:
            res = _unary_pm(cur, V)
        elif t in _BINOPS:
            res = _binary(cur, V)
        elif t == 8 or t == 9:
            res = _incdec(cur, V)
        elif t in (10, 11, 12):
            res = _assign(cur, V)
        elif t == 23 or t == 27:
            res = _unary_nt(cur, V)
        elif t == 28 or t == 30:
            cur = cur.next
            continue
        elif t == 29:
            if (prv is not None and prv.t in (INT, STR, VAR) and prv.prev is not None
                    and prv.prev.t == 28):
                _unlink(prv.prev)
                _unlink(cur)
                res = prv
            else:
                if prv is not None:
                    e = _insert_err(prv, 0x665f)
                    _unlink(prv.prev if prv.prev is not None else cur)
                    _unlink(prv)
                else:
                    e = _insert_err(cur, 0x665f)
                    _unlink(cur)
                cur = e
                continue
        elif t == 31:
            if (prv is not None and prv.t in (INT, STR, VAR) and prv.prev is not None
                    and prv.prev.t == 30 and prv.prev.prev is not None
                    and prv.prev.prev.t == VAR):
                var = prv.prev.prev
                var.idx = (var.idx or []) + [_ival(prv, V)]
                _unlink(prv.prev)
                _unlink(prv)
                _unlink(cur)
                res = var
            else:
                _unlink(cur)
                e = _insert_err(prv, 0x6661)
                _unlink(prv)
                cur = e
                continue
        if res is None:
            return None
        cur = res.prev if res.prev is not None else res
    return None


def eval_str(expr, V):
    """rva 0x1ca163: the string value (<define calc>, AUTO attributes, {$..}).
    "" when the reducer gives up."""
    n = _reduce(_tokenize(expr), V)
    if n is None:
        return ""
    if n.t == INT:
        return "%d" % n.v
    if n.t == STR:
        return n.v
    if n.t == VAR:
        v = _sval(n, V)
        return "" if v is None else v
    return ""


def eval_int(expr, V):
    """rva 0x1ca0d7: the integer value (if/elsif expr, for init/cond/next,
    INT and PAIR attributes). A string result counts through wcstol."""
    n = _reduce(_tokenize(expr), V)
    if n is None:
        return 0
    if n.t == INT:
        return n.v
    if n.t == STR:
        return wcstol(n.v)
    if n.t == VAR:
        return _ival(n, V)
    return 0


def auto(value, V):
    """rva 0x1ca21e (flag 0): evaluate the WHOLE value as a string expression
    only if it holds a quote or $X/%X (X a letter or _); otherwise literal.
    `\\x` and `$$` hide the next character from the trigger."""
    i, skip, n = 0, False, len(value)
    while i < n:
        c = value[i]
        nx = value[i + 1] if i + 1 < n else ""
        if skip:
            skip = False
        elif c == "\\" or (c == "$" and nx == "$"):
            skip = True
        elif c == "'" or c == '"' or ((c == "$" or c == "%") and nx and (nx == "_" or _is_alpha(nx))):
            return eval_str(value[:0x400], V)[:0x3ff]
        i += 1
    return value[:0x400]


def brace(value, V):
    """rva 0x1ca313: literal, except each {$...} becomes the string value of
    the expression between the braces (href and the other action strings)."""
    if "{$" not in value:
        return value[:0x3ff]
    out, i, n = [], 0, len(value)
    while i < n:
        if (value[i] == "{" and value[i + 1:i + 2] == "$" and i + 2 < n
                and (value[i + 2] == "_" or _is_alpha(value[i + 2]))):
            j = value.find("}", i + 2)
            if j >= 0:
                out.append(eval_str(value[i + 1:j], V))
                i = j + 1
                continue
        out.append(value[i])
        i += 1
    return "".join(out)[:0x3ff]


def pair(value, V):
    """rva 0x1e66d3: 'x,y' split at the first comma, each half an integer
    expression; an empty half is -1."""
    k = value.find(",")
    a, b = (value, "") if k < 0 else (value[:k], value[k + 1:])
    return (eval_int(a, V) if a else -1, eval_int(b, V) if b else -1)


def array_items(text):
    """rva 0x1dae4f: an <array>'s text -> its values. Only DOUBLE quotes
    delimit; everything outside them but ',' is dropped; ',' ends an item; the
    text after the last ',' is always one more item."""
    items, cur, inq = [], [], False
    for ch in text:
        if inq:
            if ch == '"':
                inq = False
            else:
                cur.append(ch)
        elif ch == '"':
            inq = True
        elif ch == ",":
            items.append("".join(cur))
            cur = []
    items.append("".join(cur))
    return items


# =========================================================================== #
# system variables (spec section 7) and URL query variables (section 5)
# =========================================================================== #
#: Set once when the Viewer builds its variable store, all strings. Where the
#: value depends on the machine or the account, the preview picks the Western
#: desktop view, logged out, owning every content (the operator's view).
DEFAULT_SYSVARS = {
    "_PLATFORM": "WIN",
    "_VERSION": "20060221",
    "_MAJOR_VERSION": "1",
    "_TRUE": "1",
    "_FALSE": "0",
    "_BR": "\n",
    "_LANG": "en",
    "_USER_LANG": "en-US",
    "_SHORTCUT_ID": "0",
    "_CONNECTION_MODE": "0",
    "_NET_SETTING_UPNP": "0",
    "_POL_LOGIN": "0",
    "_POL_GUEST": "0",
    "_CONTENTPLAYING": "0",
    "_VISTA_OR_HIGHER": "1",
    "_ENABLE_GMCALL": "0",
    "_HAVE_CONTENTSID": "1",
    "_HAVE_ANY_CONTENTSID": "1",
    "_CONTENTUSERID": "",
    "_PLAYINGCLASSID": "-1",
    "_PLAYINGNAME": "0",
    "_LAST_PLAYED_CONTENT": "0",
    "_QC_DOWNLOAD": "-1", "_QC_UPLOAD": "-1", "_QC_LATENCY": "-1",
    "_QC_P2P": "-1", "_QC_UPNP": "-1", "_QC_UHP": "-1",
    # from the UCS settings once logged in; "02" is the Western area, which is
    # what the operator wants to see (00/01 are JP/US-special)
    "_POL_UCS_AREA_KBN": "02",
    # recomputed on every read by the Viewer; fixed here so a preview is stable
    "_RND": "0",
    "_IS_CURSOR": "0", "_CURSOR_X": "0", "_CURSOR_Y": "0",
    "_ANCHOR_X": "0", "_ANCHOR_Y": "0",
    "_GM_CALL_STATUS": "0",
    "_IS_LOCAL_IP": "0",
}


def _pct_decode(s):
    """%XX per character; '+' stays '+' (rva 0x1ff98f)."""
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)


def page_url(path):
    """The URL a mirror file stands for. The mirror stores a page fetched with
    a query string under its encoded name -- mepm010.pml%3Fcrt_url%3D010 --
    so decode that one level to get `mepm010.pml?crt_url=010` back."""
    path = path or ""
    if "?" not in path and re.search(r"%3f", path, re.I):
        path = _pct_decode(path)
    return path


def query_vars(url):
    """rva 0x1ff98f: the variables a top-level page's URL query sets, in order.
    Stops at a pair with no '='; a name must start with a letter or digit."""
    out = []
    p = url.find("?")
    while p >= 0:
        name = p + 1
        eq = url.find("=", name)
        if eq < 0:
            break
        p = url.find("&", name)
        end = p if p >= 0 else len(url)
        if name < len(url) and url[name].isascii() and url[name].isalnum():
            out.append(("$" + url[name:eq], _pct_decode(url[eq + 1:end]) if eq < end else ""))
    return out


def make_vars(values=None):
    """A Vars holding the system defaults plus `values` ({name: value}; the
    `$` is optional, lists become arrays)."""
    V = Vars({"$" + k: v for k, v in DEFAULT_SYSVARS.items()})
    for k, v in (values or {}).items():
        name = k if k.startswith("$") else "$" + k
        if isinstance(v, Arr):
            V.set_array(name, v)
        elif isinstance(v, (list, tuple)):
            V.set_array(name, make_array(v))
        else:
            V.set(name, _as_str(v))
    return V


# =========================================================================== #
# attributes: which evaluator each one gets (spec section 5)
# =========================================================================== #
#: INT -- always an integer expression, no `$` needed.
_INT_ATTRS = frozenset((
    "delay", "appeartime", "appearcount", "zindex", "skin", "index", "length",
    "width", "height", "border", "visible", "enable", "clickable", "alpha",
    "repeat", "clicksound", "mousesound", "insound", "outsound"))
#: PAIR -- split at the first comma, each half INT, an empty half -1.
#: `areasize` is not in the spec's list; it is the same w,h shape as `size`.
_PAIR_ATTRS = frozenset(("pos", "size", "scroll", "offset", "matrix", "vbarsize",
                         "areasize"))
#: AUTO, then read as a colour.
_COLOR_ATTRS = frozenset(("bgcolor", "skincolor", "altbgcolor", "bordercolor",
                          "cellcolor", "onmousecolor"))
#: AUTO -- evaluated only when the value holds a quote or $X/%X.
_AUTO_ATTRS = frozenset((
    "src", "background", "name", "style", "alt", "value", "align", "valign",
    "type", "title", "icon", "base", "chain", "usemap", "errorcheck")) | _COLOR_ATTRS


def _split_commas(s, quotes=True):
    """Split on commas; with `quotes`, not on those inside quotes/brackets."""
    if not quotes:
        return s.split(",")
    parts, buf, depth, q = [], [], 0, ""
    for ch in s:
        if q:
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth <= 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def eval_attr(tag, key, value, V):
    """The value an element's attribute has once its handler has read it."""
    tag, key = (tag or "").lower(), (key or "").lower()
    if tag == "style" and key == "size":
        return "%d" % eval_int(value, V)        # a font size (inferred)
    if key in _PAIR_ATTRS:
        if value.count(",") > 1:
            # a 3/4-part value (select offset, sheet pos x,y,z) is read by some
            # other parser; each part as an integer is the best reading
            return ",".join("%d" % eval_int(p, V) if p else "-1" for p in value.split(","))
        return "%d,%d" % pair(value, V)
    if key in _INT_ATTRS:
        return "%d" % eval_int(value, V)
    if key in _AUTO_ATTRS:
        if key == "src" and tag in ("img", "inlineimg") or key == "lsrc":
            # split on EVERY comma first, quotes or not (rva 0x1e969f)
            return ",".join(auto(p, V) for p in value.split(","))
        return auto(value, V)
    if key == "href" or key.startswith("on"):
        return brace(value, V)                   # action strings: {$..} only
    # not mapped by the spec: comma-separated AUTO values
    return ",".join(auto(p, V) for p in _split_commas(value))


_COMMAND_RE = re.compile(r"\s*(sd|null|send|dl|mailto|javascript|https?):", re.I)


def click_targets(value, V):
    """Where an action string (an href) would navigate, as best the page's
    variables can say NOW. The Viewer runs href at click time, in code the
    spec does not cover, so this is only for the reference index (pmlrefs);
    the preview's markup keeps the href as the Viewer stores it."""
    out = []
    for part in _split_commas(brace(value, V)):
        p = part.strip()
        if not p or _COMMAND_RE.match(p):
            continue
        got = eval_str(p[5:], V) if p[:5].lower() == "eval:" else auto(p, V)
        if got and " error)" not in got:
            out.append(got)
    return out


# =========================================================================== #
# the page scanner (spec section 8)
# =========================================================================== #
def _blank(m):
    return "\n" * m.group(0).count("\n")


def _strip_comments(s):
    """Remove `<!-- -->` and `<! ... >` comments, keeping their newlines.
    Used by admin.py's file index; the preview itself scans like the Viewer."""
    s = re.sub(r"<!--[\s\S]*?-->", _blank, s)
    return re.sub(r"<![^>]*>", _blank, s)


#: elements that never enclose anything
_VOID = {
    "input", "img", "meta", "formaction", "define", "style", "br", "bgsound",
    "include", "timer", "textbox", "area", "addmenu", "addlink", "config",
    "plugin", "hidden", "bar", "systembg", "inlineimg", "multilink", "hr",
}
_WS = " \t\r\n"
_TAG_STOP = re.compile(r"[\"'>]")
_ARRAY_STOP = re.compile(r"[\"'<]")
_ATTR_RE = re.compile(
    r"""([^\s=>"'/]+)(?:[ \t\r\n]*=[ \t\r\n]*("([^"]*)"?|'([^']*)'?|([^ \t>]*)))?""")


class _Tok:
    __slots__ = ("kind", "text", "name", "closing", "attrs", "selfclose", "start")


class _Scanner:
    """Reads one file the way the Viewer's page scanner does (rva 0x1e936b)."""

    def __init__(self, text):
        self.s = text
        self.n = len(text)
        self.pos = 0
        self._nl = None

    def line(self, pos):
        if self._nl is None:
            self._nl = [m.start() for m in re.finditer("\n", self.s)]
        return bisect.bisect_left(self._nl, pos) + 1

    def next(self, in_array):
        s, i, n = self.s, self.pos, self.n
        if i >= n:
            return None
        tok = _Tok()
        tok.start = i
        if s[i] != "<":
            if in_array:
                # inside an <array> the scanner tracks quotes in text, so a `<`
                # in a quoted value is text -- and a stray quote hides every
                # tag after it (spec section 5, <array>)
                j = i
                while True:
                    m = _ARRAY_STOP.search(s, j)
                    if m is None:
                        j = n
                        break
                    if m.group() == "<":
                        j = m.start()
                        break
                    close = s.find(m.group(), m.start() + 1)
                    if close < 0:
                        self.pos = n
                        return None                 # open quote at EOF: stop
                    j = close + 1
            else:
                j = s.find("<", i)
                j = n if j < 0 else j
            self.pos = j
            tok.kind, tok.text = "text", s[i:j]
            return tok
        # a tag: whitespace is allowed after `<` and `</`; names are letters,
        # `-` and `!`, case-insensitive
        j = i + 1
        while j < n and s[j] in _WS:
            j += 1
        closing = j < n and s[j] == "/"
        if closing:
            j += 1
            while j < n and s[j] in _WS:
                j += 1
        k = j
        while k < n and (_is_alpha(s[k]) or s[k] in "-!"):
            k += 1
        comment = s.startswith("<!--", i)
        # the end: the first `>` outside quotes -- for `<!--`, the first one
        # preceded by `--`. Quotes count inside comments too.
        e = k
        while True:
            m = _TAG_STOP.search(s, e)
            if m is None:
                self.pos = n
                return None
            c, p = m.group(), m.start()
            if c == ">":
                if not comment or s[p - 2:p] == "--":
                    break
                e = p + 1
                continue
            close = s.find(c, p + 1)
            if close < 0:
                self.pos = n
                return None                         # "inconsistent": stop
            e = close + 1
        self.pos = p + 1
        tok.kind = "tag"
        tok.name = s[j:k].lower()
        tok.closing = closing
        body = s[k:p]
        tok.selfclose = body.rstrip().endswith("/")
        attrs = {}
        if not closing and tok.name and tok.name[0] != "!":
            for m in _ATTR_RE.finditer(body):
                key = m.group(1).lower()
                if key[0] == "!":
                    continue                        # <img !src=...> is ignored
                v = (m.group(3) if m.group(3) is not None
                     else m.group(4) if m.group(4) is not None
                     else m.group(5) if m.group(5) is not None else "")
                attrs.pop(key, None)                # a repeat wins, stored last
                attrs[key] = v[:0x3ff]
        tok.attrs = attrs
        return tok


# =========================================================================== #
# text content (spec section 6)
# =========================================================================== #
_ENT_BRACE_RE = re.compile(r"&(var|pos|style)=([^;&]*);", re.I)
_ENT_VAR_RE = re.compile(r"&var=([^;]*);", re.I)


def expand_text(text, V):
    """Text between tags: `{$..}` is filled in only inside &var=, &pos= and
    &style=; then each `&var=VALUE;` becomes AUTO(VALUE). `{$x}` in plain text
    and `&calc=` (no such entity) stay as written."""
    if "&" not in text:
        return text
    if "{$" in text:
        text = _ENT_BRACE_RE.sub(
            lambda m: m.group(0)[:len(m.group(1)) + 2] + brace(m.group(2), V) + ";", text)
    return _ENT_VAR_RE.sub(lambda m: auto(m.group(1), V), text)


# =========================================================================== #
# the walker: define/array/if/for/include, emitting flattened PML
# =========================================================================== #
_MAX_PARSERS = 5          # rva 0x1e6e12: a page plus four levels of include
_MAX_LOOP_JUMPS = 0x400   # `< 0x401`: at most 1 + 1024 passes
_DEFINE_NAME_RE = re.compile(r"\$[A-Za-z0-9][A-Za-z0-9_]*\Z")


class _Run:
    """State shared by a page and everything it includes."""

    def __init__(self, V, resolve):
        self.V = V
        self.resolve = resolve
        self.out = []
        self.open = []                      # emitted elements still open
        self.unresolved = set()
        self.links = []                     # click-time href targets (pmlrefs)

    def emit_open(self, tok, depth, line):
        V = self.V
        if "href" in tok.attrs:
            self.links.extend(click_targets(tok.attrs["href"], V))
        attrs = "".join(
            ' %s="%s"' % (k, eval_attr(tok.name, k, v, V).replace('"', "&quot;"))
            for k, v in tok.attrs.items())
        tag_line = ' pml-line="%d"' % line if depth == 0 else ""
        self.out.append("<%s%s%s>" % (tok.name, attrs, tag_line))
        if not (tok.selfclose or tok.name in _VOID):
            self.open.append(tok.name)

    def emit_close(self, name, floor):
        for i in range(len(self.open) - 1, floor - 1, -1):
            if self.open[i] == name:
                while len(self.open) > i:
                    self.out.append("</%s>" % self.open.pop())
                return

    def close_to(self, floor):
        while len(self.open) > floor:
            self.out.append("</%s>" % self.open.pop())


def _define(attrs, V):
    """rva 0x1c4f50. `value`/`nodefvalue` are copied, never evaluated; `calc`
    always is; `nodefvalue` wins whenever present; no value defines nothing."""
    val = dflt = None
    for k, v in attrs.items():
        if k in ("calc", "urldecode", "urlencode"):
            val = eval_str(v, V)
            if k == "urldecode":
                val = _pct_decode(val)
            elif k == "urlencode":
                val = re.sub(r"[^A-Za-z0-9_.\-~]",
                             lambda m: "".join("%%%02X" % b for b in m.group().encode("utf-8")),
                             val)
        elif k == "value":
            val = v
        elif k == "nodefvalue":
            dflt = v
        elif k == "isupdate":
            eval_int(v, V)
            val = "0"                       # the preview never has an update
        elif k == "contentname":
            eval_int(v, V)
            val = ""
        elif k == "cookie":
            val = None                      # no cookie store in the preview
    name = attrs.get("name")
    if not name or not _DEFINE_NAME_RE.match(name) or (val is None and dflt is None):
        return
    if dflt is None:
        V.set(name, val)
    elif V.get(name) is None:
        V.set(name, dflt)


def _process(text, run, depth, base):
    """Run one file (a page, or an included file) through the Viewer's rules."""
    V = run.V
    sc = _Scanner(text)
    skip = taken = 0                        # bit stacks, bit 0 = current <if>
    ifdepth = 0
    fors = []                               # [resume_pos, attrs, jumps]
    forskip = 0                             # nesting inside a skipped <for> body
    arrays = []                             # [(Arr, [text])]
    floor = len(run.open)
    while True:
        tok = sc.next(bool(arrays))
        if tok is None:
            break
        if tok.kind == "text":
            if forskip or skip & 1:
                continue
            if arrays:
                arrays[-1][1].append(tok.text)
            else:
                run.out.append(expand_text(tok.text, V))
            continue
        name = tok.name
        if not name or name[0] == "!":
            continue                        # comments, and `<` that named nothing
        if forskip:
            if name == "for":
                forskip += -1 if tok.closing else 1
            continue
        a = tok.attrs
        if tok.closing:
            if name == "if":
                if ifdepth:
                    skip >>= 1
                    taken >>= 1
                    ifdepth -= 1
                continue
            if skip & 1:
                continue
            if name == "for":
                if not fors:
                    continue                # unbalanced: message only
                top = fors[-1]
                if "next" in top[1]:
                    eval_int(top[1]["next"], V)
                if ("cond" in top[1] and top[2] < _MAX_LOOP_JUMPS
                        and eval_int(top[1]["cond"], V)):
                    top[2] += 1
                    sc.pos = top[0]
                else:
                    fors.pop()
            elif name == "array":
                if arrays:
                    arr, buf = arrays.pop()
                    if arr.children is None:
                        for item in array_items("".join(buf)):
                            arr.add_data(item)
            elif name in ("else", "elsif", "define", "include", "while"):
                pass                        # never closed; no-ops
            else:
                run.emit_close(name, floor)
            continue
        if name == "if":
            ifdepth += 1
            skip <<= 1
            taken <<= 1
            if skip & 2:                    # parent skipped: nothing evaluated
                skip |= 1
                taken |= 1
            elif "expr" in a:
                if eval_int(a["expr"], V):
                    taken |= 1
                else:
                    skip |= 1
            # no expr: the branch runs
            continue
        if name == "elsif":
            if ifdepth:
                if taken & 1:
                    skip |= 1
                elif skip & 2:
                    skip |= 1
                    taken |= 1
                else:
                    skip &= ~1
                    if "expr" in a:
                        if eval_int(a["expr"], V):
                            taken |= 1
                        else:
                            skip |= 1
            continue
        if name == "else":
            if ifdepth:                     # at top level: "No if tag", ignored
                if taken & 1:
                    skip |= 1
                else:
                    skip ^= 1
                    if skip & 1:
                        taken |= 1
            continue
        if skip & 1:
            continue                        # a skipped branch drops every tag
        if name == "define":
            _define(a, V)
        elif name == "array":
            arr = Arr()
            nm = a.get("name")
            if arrays:
                arrays[-1][0].add_child(arr)
            if nm:
                V.set_array("$" + nm.lstrip("$"), arr)
            if not tok.selfclose:
                arrays.append((arr, []))
        elif name == "for":
            if "init" in a:
                eval_int(a["init"], V)
            if "cond" in a and not eval_int(a["cond"], V):
                forskip = 1
            else:
                fors.append([sc.pos, a, 0])
        elif name == "include":
            src = auto(a.get("src", ""), V)
            got = run.resolve(src, base) if run.resolve and src else None
            if not got:
                if src:
                    run.unresolved.add(src)
            elif depth + 1 < _MAX_PARSERS:
                inc, inc_base = got
                _process(inc, run, depth + 1, inc_base)
        elif name == "while":
            pass                            # no handler on the PC
        else:
            run.emit_open(tok, depth, sc.line(tok.start))
    run.close_to(floor)


def variable_table(V):
    """{name: value} for every variable in `V`, names with their sigil ($x),
    values as strings and arrays as nested lists of strings -- including the
    `$_` system variables the Viewer recomputes on each read."""
    out = {}
    for name in ("YEA", "MON", "DAT", "HOU", "MIN", "SEC", "WEE"):
        out["$_" + name] = _dynamic_sysvar("$_" + name)
    out.update(V.scalars)
    for name, arr in V.arrays.items():
        out[name] = arr.to_list()
    return out


def expand(text, resolve_include=None, sysvars=None, base=None, report=None,
           url=None, env=None):
    """Flatten a PML page: run its define/array/for/if layer and inline its
    includes the way the Viewer does, returning positioned PML the preview
    renderer can draw.

    `resolve_include(src, base)` returns `(text, base_of_that_file)` for an
    included file, or None. `base` roots a relative src (admin.py passes the
    file's directory); each included file's own base is handed back with it.

    `sysvars` ({name: value}, `$` optional) are set after the system defaults.
    `url` is the page's path or URL; its query string (`?crt_url=010`, or the
    mirror's encoded `%3Fcrt_url%3D010`) sets variables before parsing, as the
    Viewer does for a top-level page.

    `report`, if given, is filled with `missing` (variables read while
    undefined -- for a fragment, the ones its host page supplies),
    `unresolved` (include srcs no file was found for) and `links` (where the
    page's hrefs would navigate, see click_targets).

    `env`, if given, is filled with the page's final variable table (see
    variable_table): system variables, URL query variables and everything the
    page and its includes defined, as the Viewer holds them when parsing ends.
    """
    V = make_vars(sysvars)
    for name, value in query_vars(page_url(url)) if url else ():
        V.set(name, value)
    run = _Run(V, resolve_include)
    try:
        if not re.match(r"<!doctype html", text.lstrip(chr(0xFEFF)), re.I):
            _process(text, run, 0, base)
        run.close_to(0)
        return "".join(run.out)
    except Exception as exc:                      # never break the preview
        return f"<!-- pmleval failed: {exc} -->\n{text}"
    finally:
        if report is not None:
            report["missing"] = sorted(V.missing)
            report["unresolved"] = sorted(run.unresolved)
            report["links"] = list(dict.fromkeys(run.links))
        if env is not None:
            env.update(variable_table(V))
