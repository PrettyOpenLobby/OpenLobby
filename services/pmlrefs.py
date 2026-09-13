#!/usr/bin/env python3
"""Who references whom, across the PML mirror.

WHY THIS EXISTS. Only about one .pml in twenty is a whole page. The rest are
pieces -- an `<include>`d menu, a `<style>` sheet, a table of variables, a block
of body copy a page binds into a `<textbox>` -- and on their own they preview as
an empty stage, correctly, because they have no layout until a page supplies the
variables and the frame. Naming the pages that pull a fragment in turns "there
is nothing here" into "open this and you will see it in place", which is the
only useful thing to say about a file that cannot draw itself.

The hard part is that SE writes references as EXPRESSIONS:

    <include src="$F_PATH1+'in02.pml'">
    <include src="'/pcd/topics/ff11/'+$_USER_LANG+'/'+$dir+'/detail.pml'">
    <img src="art.png" href="{$C_PATH1}news/nwpm01.pml">

So an edge is found in two passes, and the order matters:

  1. EXACT. Run the page's template layer (`pmleval`) with real include
     resolution. `$F_PATH1+'in02.pml'` becomes `file:/help/in02.pml` and lands
     on one file. Almost every reference in the mirror resolves this way, and an
     exact edge is a fact.

  2. GUESSED, for what is left. `$dir` in the topics include is a runtime
     argument -- it is the topic being viewed -- so it cannot resolve to a file,
     and the honest answer is "all of them": the expression is compiled to a
     glob (`/pcd/topics/ff11/*/*/detail.pml`) and matched against the file list.
     That is what connects 2,200-odd topic bodies to the two pages that display
     them.

Guessing is only allowed where resolving failed for that FILENAME. Without that
rule the fallback swamps the truth: `help/login/index.pml` includes exactly one
`in02.pml`, and the glob for it claimed all fifteen in the tree.
"""
import collections
import fnmatch
import os
import re

#: The two kinds of edge, and they are NOT the same thing to a reader:
#:
#:   COMPOSITION  `<include src="...">`  -- the target is part of this page
#:   NAVIGATION   `href="..."`           -- the target is somewhere you can go
#:
#: A page built from six includes is a different object from one that links to
#: six pages, and only the first makes it "constructed". The corpus splits
#: cleanly: a `src=` naming a .pml appears on `<include>` and nowhere else, and
#: `href=` appears only on the navigation tags (timer/img/addlink/button). Note
#: one tag routinely carries BOTH (`<img src="art.png" href="page.pml">` is the
#: standard button), so these are two scans, not one.
_INCLUDE_RE = re.compile(r'<include\b[^>]*?\bsrc\s*=\s*"([^"]*)"', re.I)
_LINK_RE = re.compile(r'\bhref\s*=\s*"([^"]*)"', re.I)

#: Attribute values that are commands or off-site URLs, not paths we host.
_SKIP_RE = re.compile(r"^\s*(sd:|null:|https?:|mailto:|javascript:)", re.I)

_QUOTED_RE = re.compile(r"'([^']*)'?|\"([^\"]*)\"?")     # SE loses a closing quote
_BRACE_RE = re.compile(r"\{\s*\$[A-Za-z_]\w*(?:\[[^\]]*\])?\s*\}")
_TRAILING_CMD_RE = re.compile(r",\s*(?:null:|sd:)")

TARGET_EXTS = (".pml", ".pcb")


def _dedup(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def include_refs(text):
    """The `<include src=>` expressions in `text` -- what this page is MADE of."""
    return _dedup(_INCLUDE_RE.findall(text))


def link_refs(text):
    """The `href=` expressions in `text` -- where this page can take you."""
    return _dedup(_LINK_RE.findall(text))


def to_glob(expr):
    """An SE path expression -> a glob, or None if it names no file of ours.

        "$F_PATH1+'in02.pml'"                    -> */in02.pml
        "'/pcd/topics/ff11/'+$_USER_LANG+'/'+$dir+'/detail.pml'"
                                                 -> /pcd/topics/ff11/*/*/detail.pml
        "{$C_PATH1}news/nwpm01.pml"              -> */news/nwpm01.pml
        "eval:'topm02.pml?df='+$df"              -> topm02.pml
    """
    e = (expr or "").strip()
    e = re.sub(r"^eval:", "", e, flags=re.I)
    if _SKIP_RE.match(e):
        return None
    e = _TRAILING_CMD_RE.split(e)[0]        # `...pml",null:$x=1` -- drop the command
    e = e.split(",")[0]                     # `{$a},{$b}index.pml` -- first target
    e = _BRACE_RE.sub("*", e)
    out = []
    for tok in re.split(r"\s*\+\s*", e):
        tok = tok.strip()
        m = _QUOTED_RE.fullmatch(tok)
        if m:
            out.append(m.group(1) if m.group(1) is not None else m.group(2))
        elif not tok or tok.startswith("$"):
            out.append("*")
        else:
            out.append(tok)                 # a bare unquoted path
    g = "".join(out).split("?")[0].split("#")[0]     # a query string is not a path
    g = re.sub(r"\*{2,}", "*", g)
    if not g or not g.lower().endswith(TARGET_EXTS):
        return None
    # A LEADING variable is the directory prefix -- `$F_PATH1+'in02.pml'`, where
    # $F_PATH1 is `file:/help/` and ends in a slash. Unpinned, `*in02.pml` also
    # claims every `mein02.pml` in the tree. A `*` in the MIDDLE of a segment
    # (`'detail'+$n+'.pml'`) is a genuine within-name wildcard; leave it alone.
    if g.startswith("*") and not g.startswith("*/"):
        g = "*/" + g[1:]
    return g


def _basename(path):
    return path.rsplit("/", 1)[-1].lower()


class Matcher:
    """Glob -> files, bucketed by basename so this does not scan 3,499 paths
    for every one of the ~1,100 references in the mirror."""

    def __init__(self, files):
        self.files = files
        self.by_base = collections.defaultdict(list)
        for f in files:
            self.by_base[_basename(f)].append(f)

    def candidates(self, glob):
        base = _basename(glob)
        if re.search(r"[*?]", base):
            return self.files
        return self.by_base.get(base, ())

    def match(self, glob, roots):
        """Files matching `glob` under the first of `roots` that yields any."""
        for root in roots:
            pat = ("*" + glob.lstrip("*") if root == ""
                   else _posix_join(root, glob))
            hit = [c for c in self.candidates(glob) if fnmatch.fnmatchcase(c, pat)]
            if hit:
                return hit
        return []


def _posix_join(root, rel):
    joined = rel if root in ("", ".") else root.rstrip("/") + "/" + rel
    return os.path.normpath(joined).replace(os.sep, "/")


class Graph:
    """The four directions, kept apart because they answer different questions.

        built_from[p]   the fragments p is assembled from
        included_by[p]  the pages that assemble p into themselves
        links_to[p]     the pages p can take you to
        linked_from[p]  the pages that can take you to p

    `parts[p]` is how many `<include>`s p WRITES, which is not the same as how
    many resolved: a page is "constructed" because it says so, whether or not
    the mirror still holds every piece.
    """

    def __init__(self):
        self.built_from = collections.defaultdict(set)
        self.included_by = collections.defaultdict(set)
        self.links_to = collections.defaultdict(set)
        self.linked_from = collections.defaultdict(set)
        self.parts = {}

    def add(self, kind, src, dst):
        if src == dst:
            return                          # a page referencing itself is noise
        if kind == "include":
            self.built_from[src].add(dst)
            self.included_by[dst].add(src)
        else:
            self.links_to[src].add(dst)
            self.linked_from[dst].add(src)

    def sorted(self, which):
        return {k: sorted(v) for k, v in getattr(self, which).items() if v}


def _glob_targets(matcher, site, f, raws, already):
    """Guess targets for the references pass 1 could not resolve.

    `already` is the set of basenames pass 1 DID resolve. Guessing about one of
    those is how the fallback swamps the truth -- `help/login/index.pml` names
    one `in02.pml` and the glob for it matches every in02.pml in the tree.
    """
    hosts, trees, start = site.roots(f)
    out = set()
    for raw in raws:
        glob = to_glob(raw)
        if not glob:
            continue
        base = _basename(glob)
        if any(fnmatch.fnmatchcase(n, base) for n in already):
            continue
        stripped = re.sub(r"^file:/*", "", glob)
        if stripped != glob:                # file:/X -- rooted at the PML tree
            roots, glob = list(trees), stripped
        elif glob.startswith("/"):          # /X -- rooted at the host
            roots, glob = list(hosts), glob.lstrip("/")
        else:                               # relative to the referring file
            roots = [start] + list(trees)
        # A glob that OPENS with a variable names a path we cannot reconstruct
        # at all. Let it match anywhere under the host, but only when what
        # follows is specific enough to be unambiguous, so `*/index.pml` does
        # not claim every index there is.
        if glob.startswith("*/") and glob.strip("*/").count("/") >= 1:
            roots = list(roots) + [""]
        out.update(matcher.match(glob, roots))
    return out


def build(site, files):
    """A `Graph` over `files`.

    `site` supplies what only the server knows about the mirror:
        site.roots(path)   -> (host_dirs, tree_dirs, start_dir), www-relative
        site.text(path)    -> the decoded file, comments stripped
        site.expand(path)  -> (expanded text, [www-relative include targets])
        site.resolve(path, src) -> a www-relative target, or None
    """
    matcher = Matcher(files)
    graph = Graph()

    for f in files:
        text = site.text(f)
        raw_includes, raw_links = include_refs(text), link_refs(text)
        graph.parts[f] = len(raw_includes)
        try:
            expanded, included = site.expand(f)
        except Exception:
            continue                      # a file that will not evaluate has no edges

        # 1. composition, exact: the includes the evaluator actually resolved.
        for target in included:
            graph.add("include", f, target)
        # 2. composition, guessed: whatever is left.
        for target in _glob_targets(matcher, site, f, raw_includes,
                                    {_basename(p) for p in included}):
            graph.add("include", f, target)

        # 3. navigation, exact. The EXPANDED text is used because its attributes
        #    have had their variables substituted, so most hrefs are now literal.
        links = set()
        for href in link_refs(expanded):
            href = (href or "").split("?")[0]
            if _SKIP_RE.match(href) or "$" in href:
                continue
            if not href.lower().endswith(TARGET_EXTS):
                continue
            got = site.resolve(f, href)
            if got:
                links.add(got)
        # 4. navigation, guessed.
        links |= _glob_targets(matcher, site, f, raw_links,
                               {_basename(p) for p in links})
        for target in links:
            graph.add("link", f, target)

    return graph
