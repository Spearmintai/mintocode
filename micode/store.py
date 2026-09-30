"""The compiled artifact: a `.micode/` directory at the repository root.

    .micode/
      manifest.json   file hashes, modules, models, cost, verification stats
      core.md         the always-loaded core card
      modules.json    module cards
      cards.jsonl     file cards (one per file, keyed by content hash)
      qa.jsonl        precompiled question/answer items (question-space expansion)
      flows.json      end-to-end flows
      symbols.json    deterministic symbol table with exact line spans
      graph.json      in-repo import graph
      .cache/         compiler call cache (not meant for git)

Everything except .cache/ is plain text and meant to be committed, so one compile serves the
whole team and every future session.
"""
from __future__ import annotations

import json
import os
import re

DIR = ".micode"
FORMAT = 1


def find_root(start: str) -> str | None:
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, DIR)) and os.path.exists(os.path.join(d, DIR, "manifest.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def p(root: str, name: str) -> str:
    return os.path.join(root, DIR, name)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_jsonl(path: str, rows) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_jsonl(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


class Store:
    """Read-side view of a compiled repository, loaded lazily."""

    def __init__(self, root: str):
        self.root = root
        self.manifest = read_json(p(root, "manifest.json"), {}) or {}
        self._cache: dict = {}

    def _get(self, name, loader):
        if name not in self._cache:
            self._cache[name] = loader()
        return self._cache[name]

    @property
    def core(self) -> str:
        return self._get("core", lambda: open(p(self.root, "core.md"), encoding="utf-8").read()
                         if os.path.exists(p(self.root, "core.md")) else "")

    @property
    def cards(self) -> dict:
        return self._get("cards", lambda: {c["path"]: c for c in read_jsonl(p(self.root, "cards.jsonl"))})

    @property
    def modules(self) -> dict:
        return self._get("modules", lambda: {m["module"]: m for m in read_json(p(self.root, "modules.json"), [])})

    @property
    def qa(self) -> list:
        return self._get("qa", lambda: read_jsonl(p(self.root, "qa.jsonl")))

    @property
    def flows(self) -> list:
        return self._get("flows", lambda: read_json(p(self.root, "flows.json"), []))

    @property
    def symbols(self) -> dict:
        return self._get("symbols", lambda: read_json(p(self.root, "symbols.json"), {}))

    @property
    def graph(self) -> dict:
        return self._get("graph", lambda: read_json(p(self.root, "graph.json"), {}))

    def module_of(self, path: str) -> str | None:
        return (self.manifest.get("files", {}).get(path) or {}).get("module")

    def is_stale(self, path: str) -> bool:
        """True when the file on disk no longer matches what was compiled (cheap mtime/size check first)."""
        meta = self.manifest.get("files", {}).get(path)
        full = os.path.join(self.root, path)
        if not meta or not os.path.exists(full):
            return bool(meta)
        st = os.stat(full)
        if int(st.st_mtime) == meta.get("mtime") and st.st_size == meta.get("bytes"):
            return False
        from .scan import sha_of
        return sha_of(open(full, "rb").read()) != meta.get("sha")


# ---------------------------------------------------------------- reference resolution
REF_SYM = re.compile(r"`?([\w./@+-]+\.[A-Za-z0-9]+)::([\w.$<>~]+)`?")
REF_LINE = re.compile(r"`?([\w./@+-]+\.[A-Za-z0-9]+):L?(\d+)(?:-L?(\d+))?`?")


class Resolver:
    def __init__(self, symbols: dict, lines: dict):
        self.symbols = symbols
        self.lines = lines  # path -> line count
        self.ok = self.bad = 0

    def find(self, path: str, name: str):
        syms = self.symbols.get(path)
        if syms is None:
            return None
        name = name.rstrip("().")
        for s in syms:
            if s["name"] == name:
                return s
        tail = name.split(".")[-1]
        cands = [s for s in syms if s["name"].split(".")[-1] == tail]
        if len(cands) == 1 or (cands and all(c["name"] == cands[0]["name"] for c in cands)):
            return cands[0]
        if cands:
            head = name.split(".")[0]
            for c in cands:
                if c["name"].startswith(head):
                    return c
            return cands[0]
        return None

    def ref(self, ref: str) -> str | None:
        """Normalise one reference to `path:Lstart-end` (or `path`), or None if it does not resolve."""
        ref = ref.strip().strip("`")
        m = REF_SYM.fullmatch(ref)
        if m:
            s = self.find(m.group(1), m.group(2))
            if s:
                self.ok += 1
                return f"{m.group(1)}:L{s['start']}-{s['end']}"
            self.bad += 1
            return None
        m = REF_LINE.fullmatch(ref)
        if m and m.group(1) in self.lines:
            a = int(m.group(2))
            b = int(m.group(3) or a)
            n = self.lines[m.group(1)]
            if 1 <= a <= n:
                self.ok += 1
                return f"{m.group(1)}:L{a}-{min(max(a, b), n)}"
            self.bad += 1
            return None
        if ref in self.lines:
            self.ok += 1
            return ref
        self.bad += 1
        return None

    def annotate(self, text: str) -> str:
        """Append exact line spans to inline `path::Symbol` mentions; leave unresolved ones marked."""
        if not isinstance(text, str):
            return text

        def sub(m):
            s = self.find(m.group(1), m.group(2))
            if s:
                self.ok += 1
                return f"`{m.group(1)}::{m.group(2)}` (L{s['start']}-{s['end']})"
            if m.group(1) in self.lines:
                self.bad += 1
                return f"`{m.group(1)}::{m.group(2)}` (unverified)"
            self.bad += 1
            return m.group(0)

        return REF_SYM.sub(sub, text)
