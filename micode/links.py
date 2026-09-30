"""Symbol-level link graph: which repository symbols each paragraph (function, method, class) refers to.

Deterministic, derived from the source and the symbol table, cached per compile. Used like a linker: once the
seed paragraphs for a question are chosen, their unresolved references are followed to the definitions they
use, the callers that use them, and the tests that exercise them.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from .store import DIR, Store

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
SKIP_KINDS = {"section"}


def _tail(name: str) -> str:
    return name.split(".")[-1]


def build(store: Store) -> dict:
    """Return {"out": {sid: [sid...]}, "in": {sid: [sid...]}} with sid = "path#index"."""
    by_tail: dict = defaultdict(list)
    for path, syms in store.symbols.items():
        for i, s in enumerate(syms):
            if s.get("kind") in SKIP_KINDS:
                continue
            t = _tail(s["name"])
            if len(t) >= 3 and not (t.startswith("__") and t.endswith("__")):
                by_tail[t].append(f"{path}#{i}")
    imports = store.graph
    out: dict = {}
    for path, syms in store.symbols.items():
        if not syms or path.endswith((".md", ".rst", ".txt")):
            continue
        try:
            lines = open(os.path.join(store.root, path), encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        near = set(imports.get(path, [])) | {path}
        for i, s in enumerate(syms):
            if s.get("kind") in SKIP_KINDS:
                continue
            # Body only: for classes, the lines not covered by their own methods are enough (fields, bases).
            body = "\n".join(lines[s["start"] - 1:min(s["end"], s["start"] + 400)])
            me = f"{path}#{i}"
            targets = []
            for ident in set(IDENT.findall(body)):
                cands = [c for c in by_tail.get(ident, []) if c != me]
                if not cands:
                    continue
                if len(cands) > 1:
                    local = [c for c in cands if c.split("#")[0] in near]
                    cands = local or (cands if len(cands) <= 2 else [])
                targets += cands[:3]
            # A class/function containing another symbol is structure, not a reference.
            out[me] = sorted({t for t in targets if not _contains(store, me, t)})
    inc: dict = defaultdict(list)
    for a, bs in out.items():
        for b in bs:
            inc[b].append(a)
    return {"out": out, "in": dict(inc)}


def _contains(store: Store, a: str, b: str) -> bool:
    pa, ia = a.split("#")
    pb, ib = b.split("#")
    if pa != pb:
        return False
    sa, sb = store.symbols[pa][int(ia)], store.symbols[pb][int(ib)]
    return (sa["start"] <= sb["start"] and sb["end"] <= sa["end"]) or (sb["start"] <= sa["start"] and sa["end"] <= sb["end"])


def load(store: Store) -> dict:
    cache = os.path.join(store.root, DIR, ".cache", "links.json")
    stamp = store.manifest.get("compiled_at")
    try:
        d = json.load(open(cache))
        if d.get("stamp") == stamp:
            return d
    except (OSError, ValueError):
        pass
    d = build(store)
    d["stamp"] = stamp
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        json.dump(d, open(cache, "w"))
    except OSError:
        pass
    return d
