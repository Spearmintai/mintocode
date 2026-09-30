"""Deterministic symbol and import extraction with exact line spans.

Python uses the ast module. Other languages use declaration regexes and find the end of
each block by brace matching (C-like languages) or indentation/`end` keywords (Ruby, Lua,
Elixir). Spans are 1-based and inclusive.
"""
from __future__ import annotations

import ast
import os
import re
import warnings

Sym = dict  # {"name", "kind", "start", "end", "sig", "parent"}


# ---------------------------------------------------------------- python
def _py_symbols(text: str) -> tuple[list[Sym], list[str]]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _generic_symbols(text, "python"), _generic_imports(text, "python")
    lines = text.splitlines()
    syms: list[Sym] = []
    imports: list[str] = []

    def sig_of(node) -> str:
        line = lines[node.lineno - 1].strip() if node.lineno - 1 < len(lines) else ""
        # Join continuation lines of a multi-line signature, up to the colon.
        i = node.lineno
        while not line.rstrip().endswith(":") and i < len(lines) and i < node.lineno + 8:
            line += " " + lines[i].strip()
            i += 1
        return re.sub(r"\s+", " ", line)[:240]

    def visit(body, parent):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([d.lineno for d in n.decorator_list] + [n.lineno])
                kind = "class" if isinstance(n, ast.ClassDef) else ("method" if parent else "function")
                name = f"{parent}.{n.name}" if parent else n.name
                syms.append({"name": name, "kind": kind, "start": start, "end": n.end_lineno,
                             "sig": sig_of(n)})
                if isinstance(n, ast.ClassDef):
                    visit(n.body, name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.TypeAlias if hasattr(ast, "TypeAlias") else ast.Assign)):
                # Module globals (app, bp, current_app, CONSTANTS, type aliases) and class attributes
                # (Flask.default_config) are common citation targets.
                targets = n.targets if isinstance(n, ast.Assign) else [getattr(n, "target", None) or n.name]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id != "__all__" and not (t.id.startswith("_") and not t.id.startswith("__")):
                        seg = lines[n.lineno - 1].strip()[:160]
                        kind = "constant" if t.id.isupper() else ("attribute" if parent else "variable")
                        syms.append({"name": f"{parent}.{t.id}" if parent else t.id, "kind": kind,
                                     "start": n.lineno, "end": n.end_lineno, "sig": seg})
            elif isinstance(n, ast.Import):
                imports.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                mod = ("." * n.level) + (n.module or "")
                imports.append(mod)
                if not n.module:
                    imports.extend("." * n.level + a.name for a in n.names)
            elif isinstance(n, (ast.If, ast.Try)) and parent is None:
                visit(n.body, None)
                for h in getattr(n, "handlers", []):
                    visit(h.body, None)
                visit(n.orelse, None)

    visit(tree.body, None)
    return syms, imports


# ---------------------------------------------------------------- generic
_ID = r"[A-Za-z_$][\w$]*"
DECL = {
    "javascript": [
        (rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_ID})", "function"),
        (rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_ID})", "class"),
        (rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>|{_ID}\s*=>)", "function"),
        (rf"^\s{{2,}}(?:static\s+|async\s+|get\s+|set\s+|public\s+|private\s+|protected\s+)*({_ID})\s*(?:<[^>]*>)?\([^)]*\)\s*(?::\s*[^{{=;]+)?\{{", "method"),
    ],
    "go": [
        (r"^func\s+\(\s*\w+\s+\*?(\w+)[^)]*\)\s*(\w+)", "method"),
        (r"^func\s+(\w+)", "function"),
        (r"^type\s+(\w+)\s+(?:struct|interface)", "type"),
    ],
    "rust": [
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)", "function"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(\w+)", "type"),
        (r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([\w:]+)", "impl"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*\{", "module"),
    ],
    "java": [
        (r"^\s*(?:(?:public|private|protected|static|final|abstract|sealed)\s+)*(?:class|interface|enum|record|@interface)\s+(\w+)", "class"),
        (r"^\s+(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)\s+)+[\w<>\[\], ?]+\s+(\w+)\s*\(", "method"),
    ],
    "kotlin": [
        (r"^\s*(?:(?:public|private|internal|open|abstract|data|sealed|inline|enum)\s+)*(?:class|interface|object)\s+(\w+)", "class"),
        (r"^\s*(?:(?:public|private|internal|override|open|suspend|inline)\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)", "function"),
    ],
    "csharp": [
        (r"^\s*(?:(?:public|private|protected|internal|static|sealed|abstract|partial)\s+)*(?:class|interface|struct|enum|record)\s+(\w+)", "class"),
        (r"^\s+(?:(?:public|private|protected|internal|static|virtual|override|async|abstract)\s+)+[\w<>\[\], ?]+\s+(\w+)\s*\(", "method"),
    ],
    "c": [
        (r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;]*$", "function"),
        (r"^(?:typedef\s+)?struct\s+(\w+)\s*\{", "type"),
        (r"^#define\s+(\w+)", "macro"),
    ],
    "swift": [
        (r"^\s*(?:(?:public|private|internal|open|final)\s+)*(?:class|struct|enum|protocol|extension)\s+(\w+)", "class"),
        (r"^\s*(?:(?:public|private|internal|open|static|override|final)\s+)*func\s+(\w+)", "function"),
    ],
    "php": [
        (r"^\s*(?:(?:abstract|final)\s+)?(?:class|interface|trait|enum)\s+(\w+)", "class"),
        (r"^\s*(?:(?:public|private|protected|static)\s+)*function\s+(\w+)", "function"),
    ],
    "scala": [
        (r"^\s*(?:(?:case|abstract|sealed|final)\s+)*(?:class|object|trait)\s+(\w+)", "class"),
        (r"^\s*(?:(?:override|private|protected)\s+)*def\s+(\w+)", "function"),
    ],
    "dart": [
        (r"^\s*(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:static\s+)?(?:Future<[^>]*>|[\w<>?]+)\s+(\w+)\s*\([^;]*\{\s*$", "function"),
    ],
    "ruby": [
        (r"^\s*(?:class|module)\s+([\w:]+)", "class"),
        (r"^\s*def\s+(?:self\.)?([\w?!=]+)", "function"),
    ],
    "lua": [(r"^\s*(?:local\s+)?function\s+([\w.:]+)", "function")],
    "elixir": [
        (r"^\s*defmodule\s+([\w.]+)", "module"),
        (r"^\s*defp?\s+(\w+[?!]?)", "function"),
    ],
    "shell": [(r"^\s*(?:function\s+)?([\w-]+)\s*\(\)\s*\{", "function")],
    "proto": [(r"^\s*(?:message|service|enum)\s+(\w+)", "type"), (r"^\s*rpc\s+(\w+)", "method")],
    "sql": [(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure|index)\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", "table")],
    "markdown": [(r"^(#{1,3})\s+(.+)$", "section")],
}
DECL["typescript"] = DECL["javascript"] + [
    (rf"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+({_ID})", "type"),
    (rf"^\s{{2,}}(?:(?:public|private|protected|static|readonly|async|abstract|override)\s+)+({_ID})\s*(?:<[^>]*>)?\s*\(", "method"),
]
DECL["vue"] = DECL["svelte"] = DECL["javascript"]
DECL["cpp"] = DECL["c"] + [(r"^\s*(?:class|struct|namespace)\s+(\w+)[^;]*$", "class"),
                           (r"^[\w:<>\*&\s]+?\b(\w+::~?\w+)\s*\(", "method")]
DECL["objc"] = DECL["c"]

KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do", "try", "new", "throw",
            "sizeof", "typeof", "await", "function", "constructor", "super", "delete", "case"}
END_KEYWORD_LANGS = {"ruby", "lua", "elixir"}
INDENT_LANGS = {"python", "yaml"}

IMPORT_RES = {
    "python": [r"^\s*from\s+(\.*[\w.]*)\s+import", r"^\s*import\s+([\w.]+)"],
    "javascript": [r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]""", r"""^\s*import\s+['"]([^'"]+)['"]""",
                   r"""require\(\s*['"]([^'"]+)['"]\s*\)""", r"""import\(\s*['"]([^'"]+)['"]\s*\)"""],
    "go": [r'^\s*(?:import\s+)?(?:\w+\s+)?"([\w./-]+)"\s*$'],
    "rust": [r"^\s*(?:pub\s+)?use\s+([\w:]+)", r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;"],
    "java": [r"^\s*import\s+(?:static\s+)?([\w.]+)"],
    "kotlin": [r"^\s*import\s+([\w.]+)"],
    "scala": [r"^\s*import\s+([\w.]+)"],
    "csharp": [r"^\s*using\s+([\w.]+)\s*;"],
    "c": [r'^\s*#\s*include\s+"([^"]+)"'],
    "ruby": [r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]"""],
    "php": [r"^\s*use\s+([\w\\]+)", r"""(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)['"]"""],
}
IMPORT_RES["typescript"] = IMPORT_RES["vue"] = IMPORT_RES["svelte"] = IMPORT_RES["javascript"]
IMPORT_RES["cpp"] = IMPORT_RES["objc"] = IMPORT_RES["c"]


def _strip_strings(line: str) -> str:
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    line = re.sub(r"`(?:\\.|[^`\\])*`", "``", line)
    return re.sub(r"//.*$", "", line)


def _brace_end(lines: list[str], i: int) -> int:
    depth, seen, in_block = 0, False, False
    for j in range(i, min(len(lines), i + 5000)):
        s = lines[j]
        if in_block:
            if "*/" in s:
                s, in_block = s.split("*/", 1)[1], False
            else:
                continue
        s = _strip_strings(s)
        if "/*" in s and "*/" not in s.split("/*", 1)[1]:
            s, in_block = s.split("/*", 1)[0], True
        for ch in s:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
                if seen and depth <= 0:
                    return j + 1
        if not seen and j > i + 3 and s.rstrip().endswith(";"):
            return j + 1  # declaration without a body
    return i + 1


def _end_keyword_end(lines: list[str], i: int) -> int:
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, min(len(lines), i + 5000)):
        s = lines[j]
        if s.strip() in ("end", "end)", "end,") and len(s) - len(s.lstrip()) == indent:
            return j + 1
    return i + 1


def _generic_symbols(text: str, lang: str) -> list[Sym]:
    pats = DECL.get(lang)
    if not pats:
        return []
    lines = text.splitlines()
    syms: list[Sym] = []
    compiled = [(re.compile(p), k) for p, k in pats]
    for i, line in enumerate(lines):
        if len(line) > 300:
            continue
        for rx, kind in compiled:
            m = rx.match(line)
            if not m:
                continue
            if lang == "markdown":
                syms.append({"name": m.group(2).strip()[:100], "kind": "section", "start": i + 1,
                             "end": i + 1, "sig": line.strip()[:160], "level": len(m.group(1))})
                break
            if lang == "go" and kind == "method":
                name = f"{m.group(1)}.{m.group(2)}"
            else:
                name = m.group(1)
            if name in KEYWORDS:
                continue
            if lang in END_KEYWORD_LANGS:
                end = _end_keyword_end(lines, i)
            elif kind in ("macro", "table"):
                end = i + 1
            else:
                end = _brace_end(lines, i)
            syms.append({"name": name, "kind": kind, "start": i + 1, "end": max(end, i + 1),
                         "sig": re.sub(r"\s+", " ", line.strip())[:200]})
            break
    if lang == "markdown":
        # A section runs until the next heading of the same or higher level.
        for k, s in enumerate(syms):
            end = len(lines)
            for t in syms[k + 1:]:
                if t["level"] <= s["level"]:
                    end = t["start"] - 1
                    break
            s["end"] = end
            s.pop("level", None)
    # Qualify methods with their enclosing class/impl for readability.
    containers = [s for s in syms if s["kind"] in ("class", "impl", "type", "module")]
    for s in syms:
        if s["kind"] in ("method", "function") and "." not in s["name"]:
            for c in containers:
                if c is not s and c["start"] < s["start"] and s["end"] <= c["end"]:
                    s["name"] = f"{c['name']}.{s['name']}"
                    if s["kind"] == "function":
                        s["kind"] = "method"
    return syms


def _generic_imports(text: str, lang: str) -> list[str]:
    out = []
    for p in IMPORT_RES.get(lang, []):
        for m in re.finditer(p, text, re.M):
            out.append(m.group(1))
    return out


def extract(text: str, lang: str) -> tuple[list[Sym], list[str]]:
    if lang == "python":
        syms, imps = _py_symbols(text)
    else:
        syms, imps = _generic_symbols(text, lang), _generic_imports(text, lang)
    # Go import blocks: lines inside import ( ... )
    if lang == "go":
        for block in re.findall(r"^import\s*\((.*?)^\)", text, re.S | re.M):
            imps += re.findall(r'"([\w./-]+)"', block)
    return syms, sorted(set(imps))


# ---------------------------------------------------------------- import resolution
def resolve_imports(files: dict, lang_of: dict) -> dict[str, list[str]]:
    """Map each file to the in-repo files it imports. files: path -> list of raw import strings."""
    paths = set(files)
    by_noext: dict[str, list[str]] = {}
    for p in paths:
        stem = os.path.splitext(p)[0]
        by_noext.setdefault(stem, []).append(p)
        if os.path.basename(stem) in ("index", "__init__", "mod", "lib"):
            by_noext.setdefault(os.path.dirname(stem), []).append(p)
    # Python module name -> file, trying every possible source root prefix.
    py_mod: dict[str, str] = {}
    for p in paths:
        if lang_of.get(p) == "python":
            parts = os.path.splitext(p)[0].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for k in range(len(parts)):
                py_mod.setdefault(".".join(parts[k:]), p)
    go_dirs: dict[str, list[str]] = {}
    for p in paths:
        if p.endswith(".go"):
            go_dirs.setdefault(os.path.dirname(p), []).append(p)

    graph: dict[str, list[str]] = {}
    for src, imps in files.items():
        lang = lang_of.get(src, "")
        d = os.path.dirname(src)
        hits = set()
        for imp in imps:
            cand: list[str] = []
            if lang == "python":
                if imp.startswith("."):
                    lvl = len(imp) - len(imp.lstrip("."))
                    base = d.split("/") if d else []
                    base = base[: len(base) - (lvl - 1)] if lvl > 1 else base
                    rest = imp.lstrip(".").replace(".", "/")
                    stem = "/".join([x for x in base + ([rest] if rest else []) if x])
                    cand = by_noext.get(stem, [])
                else:
                    m = imp
                    while m and m not in py_mod:
                        m = m.rpartition(".")[0]
                    if m:
                        cand = [py_mod[m]]
            elif lang in ("javascript", "typescript", "vue", "svelte"):
                if imp.startswith("."):
                    stem = os.path.normpath(os.path.join(d, imp)).replace(os.sep, "/")
                    stem = os.path.splitext(stem)[0] if os.path.splitext(stem)[1] in (".js", ".ts", ".mjs", ".jsx", ".tsx") else stem
                    cand = by_noext.get(stem, [])
                elif imp.startswith(("@/", "~/", "src/")):
                    stem = "src/" + imp.split("/", 1)[1] if not imp.startswith("src/") else imp
                    cand = by_noext.get(os.path.splitext(stem)[0], [])
            elif lang == "go":
                for gd, gfs in go_dirs.items():
                    if gd and (imp == gd or imp.endswith("/" + gd)):
                        cand = gfs
                        break
            elif lang in ("c", "cpp", "objc"):
                stem = os.path.normpath(os.path.join(d, imp)).replace(os.sep, "/")
                cand = [stem] if stem in paths else [p for p in paths if p.endswith("/" + imp)][:1]
            elif lang == "rust":
                if "::" not in imp:  # mod foo;
                    cand = by_noext.get(f"{d}/{imp}".lstrip("/"), [])
                else:
                    parts = imp.split("::")
                    if parts[0] == "crate":
                        for k in range(len(parts), 1, -1):
                            for root in ("src", ""):
                                stem = "/".join(([root] if root else []) + parts[1:k])
                                if stem in by_noext:
                                    cand = by_noext[stem]
                                    break
                            if cand:
                                break
            elif lang in ("java", "kotlin", "scala", "csharp"):
                tail = imp.replace(".", "/")
                cand = [p for p in paths if os.path.splitext(p)[0].endswith(tail)][:1]
            elif lang == "ruby":
                stem = os.path.normpath(os.path.join(d, imp)).replace(os.sep, "/")
                cand = by_noext.get(stem, []) or by_noext.get("lib/" + imp, [])
            for c in cand:
                if c != src:
                    hits.add(c)
        graph[src] = sorted(hits)
    return graph
