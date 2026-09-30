"""Offline retrieval quality: does the injected pack contain what the SWE-QA reference answer relies on?

For each question, the reference answer names files (`src/x/y.py`) and identifiers (`Foo.bar`). We measure:
  file recall      – share of referenced source files that appear in the pack (cards or SOURCE excerpts)
  ident recall     – share of referenced identifiers that appear anywhere in the pack
  excerpt recall   – share of referenced identifiers that appear inside the live SOURCE excerpts
No model calls: this is the fast inner loop for tuning retrieval.

usage: python3 bench/retrieval_eval.py quill [--budget 2200 --excerpt 1800]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from micode import retrieve  # noqa: E402
from micode.store import Store  # noqa: E402

BENCH = os.environ.get("MICODE_BENCH", "/home/sq/micode-bench")


def refs(answer: str, store: Store):
    files = {f for f in re.findall(r"[\w./-]+\.(?:py|rst|md|toml|cfg|txt)", answer) if f in store.manifest["files"]}
    known = _known(store)
    idents = set()
    for tick in re.findall(r"`([^`]+)`", answer):
        for part in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", tick):
            if part in known:
                idents.add(part)
    return files, idents


_KNOWN: dict = {}


def _known(store: Store) -> set:
    """Identifier parts defined in the repository (so prose words in reference answers are not counted)."""
    if store.root not in _KNOWN:
        _KNOWN[store.root] = {part for syms in store.symbols.values() for s in syms if s.get("kind") != "section"
                              for part in s["name"].split(".") if len(part) > 3}
    return _KNOWN[store.root]


def evaluate(repo: str, budget: int, excerpt: int, packer=None, verbose=False):
    store = Store(os.path.join(BENCH, "repos", repo))
    ix = retrieve.Index.load(store)
    qs = [json.loads(line) for line in open(os.path.join(BENCH, "SWE-QA-Bench", "Benchmark", f"{repo}.jsonl"))]
    fr, ir, er, sizes = [], [], [], []
    for k, q in enumerate(qs):
        files, idents = refs(q["answer"], store)
        text = (packer or (lambda s, qq: retrieve.pack(s, qq, budget=budget, excerpt_budget=excerpt, ix=ix)[0]))(store, q["question"])
        sizes.append(len(text) // 4)
        src = "\n".join(b for b in text.split("\n\n") if b.startswith("SOURCE"))
        if files:
            fr.append(sum(f in text for f in files) / len(files))
        if idents:
            ir.append(sum(i in text for i in idents) / len(idents))
            er.append(sum(i in src for i in idents) / len(idents))
        if verbose:
            print(k, f"files {sum(f in text for f in files)}/{len(files)}", f"idents {sum(i in text for i in idents)}/{len(idents)}")
    return {"file_recall": round(st.mean(fr), 3), "ident_recall": round(st.mean(ir), 3),
            "excerpt_recall": round(st.mean(er), 3), "pack_tokens": round(st.mean(sizes))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--budget", type=int, default=2200)
    ap.add_argument("--excerpt", type=int, default=1800)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    print(evaluate(a.repo, a.budget, a.excerpt, verbose=a.v))
