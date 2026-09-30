"""Runtime retrieval over the compiled artifact. No model calls, stdlib only, milliseconds per query.

A BM25F-style lexical index over five unit types — precompiled Q/A items, flows, module cards,
file cards and symbols — built once per compile and cached. Because the compiler already
anticipated questions (question-space expansion), matching a user's question against compiled
questions is far more reliable than matching it against raw code.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict

from .store import DIR, Store

STOP = set("""a an the and or of to in on for with by from at as is are was were be been being it its this that these those
there here what which who whom whose where when why how do does did done can could should would will shall may might must
i me my we our you your he she they them their not no yes if then else than so such into onto about over under out up down
any all some each every more most other just also very get gets got use used using file files code repo repository function
functions method class please tell show find explain make want need like let lets s t""".split())


def split_ident(tok: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return [x for x in re.split(r"[\s_\-./:$]+", parts) if x]


def stem(w: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def tokens(text: str) -> list[str]:
    out = []
    for raw in re.findall(r"[A-Za-z_$][\w$.]*|\d+", text or ""):
        low = raw.lower()
        pieces = split_ident(raw)
        if len(pieces) > 1:
            out.append(low.strip("."))  # keep the whole identifier too
        for pc in pieces:
            pc = pc.lower()
            if pc not in STOP and len(pc) > 1:
                out.append(stem(pc))
    return out


class Index:
    VERSION = 4

    def __init__(self, store: Store):
        self.store = store
        self.units: list[dict] = []
        self.tf: list[Counter] = []
        self.df: Counter = Counter()
        self.lens: list[int] = []
        self.avg = 1.0

    # ------------------------------------------------------------ build
    @classmethod
    def load(cls, store: Store) -> "Index":
        ix = cls(store)
        cache = os.path.join(store.root, DIR, ".cache", "index.json")
        stamp = f"{cls.VERSION}:{store.manifest.get('compiled_at')}"
        try:
            with open(cache, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("stamp") == stamp:
                ix.units, ix.lens, ix.avg = d["units"], d["lens"], d["avg"]
                ix.tf = [Counter(t) for t in d["tf"]]
                ix.df = Counter(d["df"])
                return ix
        except (OSError, ValueError, KeyError):
            pass
        ix.build()
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"stamp": stamp, "units": ix.units, "lens": ix.lens, "avg": ix.avg,
                           "tf": [dict(t) for t in ix.tf], "df": dict(ix.df)}, f)
            os.replace(cache + ".tmp", cache)
        except OSError:
            pass
        return ix

    def _add(self, unit: dict, fields: list[tuple[str, float]]) -> None:
        tf: Counter = Counter()
        for text, w in fields:
            for t in tokens(text):
                tf[t] += w
        if not tf:
            return
        self.units.append(unit)
        self.tf.append(tf)
        self.lens.append(int(sum(tf.values())))
        for t in tf:
            self.df[t] += 1

    def build(self) -> None:
        s = self.store
        for i, q in enumerate(s.qa):
            self._add({"t": "qa", "i": i}, [(q.get("q", ""), 3.0), (q.get("a", ""), 1.0), (" ".join(q.get("refs", [])), 1.0)])
        for i, fl in enumerate(s.flows):
            self._add({"t": "flow", "i": i}, [(fl.get("name", ""), 2.0), (fl.get("q", ""), 2.0), (" ".join(fl.get("steps", [])), 1.0)])
        for name, m in s.modules.items():
            self._add({"t": "module", "k": name}, [(name, 2.0), (m.get("purpose", ""), 2.0), (m.get("overview", ""), 1.0),
                                                   (" ".join(m.get("questions", [])), 1.5), (" ".join(m.get("how_to", [])), 1.0)])
        for path, c in s.cards.items():
            syms = " ".join(x.get("name", "") for x in c.get("symbols", []) if isinstance(x, dict))
            self._add({"t": "file", "k": path}, [(path, 2.0), (c.get("purpose", ""), 2.0), (c.get("summary", ""), 1.0),
                                                 (syms, 1.5), (" ".join(map(str, c.get("questions", []))), 1.5),
                                                 (" ".join(map(str, c.get("facts", []))), 1.0),
                                                 (" ".join(map(str, c.get("gotchas", []))), 0.7)])
        for path, syms in s.symbols.items():
            notes = {x.get("name"): x.get("does", "") for x in s.cards.get(path, {}).get("symbols", []) if isinstance(x, dict)}
            akas = {x.get("name"): " ".join(x.get("aka", [])) for x in s.cards.get(path, {}).get("symbols", []) if isinstance(x, dict)}
            for k, sym in enumerate(syms):
                if sym.get("kind") == "section" and not notes.get(sym["name"]):
                    continue
                self._add({"t": "sym", "k": path, "i": k},
                          [(sym["name"], 3.0), (sym.get("sig", ""), 1.0), (notes.get(sym["name"], ""), 1.0),
                           (akas.get(sym["name"], ""), 1.5), (path, 0.5)])
        self.avg = sum(self.lens) / max(1, len(self.lens))

    # ------------------------------------------------------------ query
    def search(self, query: str, k: int = 30, types: set | None = None):
        q = Counter(tokens(query))
        if not q:
            return []
        n = len(self.units)
        scores = []
        k1, b = 1.2, 0.6
        qidf = {t: math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5)) for t in q if self.df.get(t)}
        if not qidf:
            return []
        for i, tf in enumerate(self.tf):
            if types and self.units[i]["t"] not in types:
                continue
            sc, hit = 0.0, 0
            for t, idf in qidf.items():
                f = tf.get(t)
                if f:
                    hit += 1
                    sc += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * self.lens[i] / self.avg))
            if sc:
                # Reward covering more distinct query terms (a crude proximity/coverage prior).
                sc *= (hit / len(qidf)) ** 0.5
                scores.append((sc, i))
        scores.sort(reverse=True)
        return [(sc, self.units[i]) for sc, i in scores[:k]]


# ---------------------------------------------------------------- packing
def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _span(sym) -> str:
    return f"L{sym['start']}-{sym['end']}"


EXCERPT_MAX_LINES = 45


def pack(store: Store, query: str, budget: int = 2000, ix: Index | None = None, min_score: float = 0.0,
         exclude: set | None = None, excerpt_budget: int = 0) -> tuple[str, dict]:
    """Assemble the most relevant compiled knowledge for `query` within `budget` tokens, plus up to
    `excerpt_budget` tokens of live source excerpts for the best-matching spans."""
    ix = ix or Index.load(store)
    hits = ix.search(query, k=60)
    info = {"top": hits[0][0] if hits else 0.0, "units": []}
    if not hits or hits[0][0] < min_score:
        return "", info
    exclude = exclude or set()
    top = hits[0][0]
    qtok = set(tokens(query))
    stale_cache: dict = {}

    def stale(path):
        if path not in stale_cache:
            stale_cache[path] = store.is_stale(path)
        return stale_cache[path]

    def mark(path):
        return " ⚠stale: changed since compile, verify in source" if stale(path) else ""

    sections, used = [], 0
    # Rank files by their own score plus the scores of their symbols and Q/A refs.
    file_score: dict = defaultdict(float)
    sym_hits: dict = defaultdict(list)
    for sc, u in hits:
        if u["t"] == "file":
            file_score[u["k"]] += sc
        elif u["t"] == "sym":
            file_score[u["k"]] += 0.5 * sc
            sym_hits[u["k"]].append((sc, u["i"]))
        elif u["t"] == "qa":
            for r in store.qa[u["i"]].get("refs", [])[:3]:
                file_score[r.split(":")[0]] += 0.3 * sc

    def add(key, text):
        nonlocal used
        if key in exclude:
            return False
        t = est_tokens(text)
        if used + t > budget:
            return False
        sections.append(text)
        used += t
        info["units"].append(key)
        return True

    # 1. Precompiled answers: the compiler already answered questions like this one.
    n_qa = 0
    for sc, u in hits:
        if u["t"] == "qa" and sc >= 0.45 * top and n_qa < 4:
            it = store.qa[u["i"]]
            refs = ", ".join(r + mark(r.split(":")[0]) for r in it.get("refs", [])[:5])
            if add(f"qa:{u['i']}", f"Q: {it['q']}\nA: {it['a']}" + (f"\n   refs: {refs}" if refs else "")):
                n_qa += 1
    # 2. One matching end-to-end flow.
    for sc, u in hits:
        if u["t"] == "flow" and sc >= 0.5 * top:
            fl = store.flows[u["i"]]
            add(f"flow:{u['i']}", f"FLOW {fl.get('name')}:\n" + "\n".join(f"  {k + 1}. {s}" for k, s in enumerate(fl.get("steps", [])[:10])))
            break
    # 3. File cards with the relevant symbols' exact spans.
    for path, fsc in sorted(file_score.items(), key=lambda kv: -kv[1])[:6]:
        c = store.cards.get(path)
        syms = store.symbols.get(path, [])
        if not c and not syms:
            continue
        meta = store.manifest.get("files", {}).get(path, {})
        head = f"FILE {path} ({meta.get('lines', '?')} lines){mark(path)}: {(c or {}).get('purpose', '')}"
        notes = {x.get("name"): x.get("does", "") for x in (c or {}).get("symbols", []) if isinstance(x, dict)}
        chosen = [i for _, i in sorted(sym_hits.get(path, []), reverse=True)[:6]]
        if len(chosen) < 3:  # add symbols whose notes mention query terms
            for i, sym in enumerate(syms):
                if i not in chosen and qtok & set(tokens(sym["name"] + " " + notes.get(sym["name"], ""))):
                    chosen.append(i)
                if len(chosen) >= 5:
                    break
        lines = [head]
        for i in chosen:
            sym = syms[i]
            lines.append(f"  - {sym['name']} {_span(sym)}: {notes.get(sym['name'], sym.get('sig', ''))[:300]}")
        facts = [f for f in (c or {}).get("facts", []) if qtok & set(tokens(str(f)))][:4]
        if facts:
            lines.append("  facts: " + " | ".join(map(str, facts)))
        gotchas = [g for g in (c or {}).get("gotchas", []) if qtok & set(tokens(str(g)))][:2]
        if gotchas:
            lines.append("  gotchas: " + " | ".join(map(str, gotchas)))
        add(f"file:{path}", "\n".join(lines))
    # 4. Live source excerpts for the best spans, read from disk now (never stale). With the compiled answer
    #    and the exact lines both in context, the agent has nothing left to verify and can answer in one turn.
    if excerpt_budget:
        spans: list[tuple[str, int, int]] = []
        for sc, u in hits:
            if u["t"] == "qa" and f"qa:{u['i']}" in info["units"]:
                for r in store.qa[u["i"]].get("refs", [])[:2]:
                    m = re.match(r"(.+):L(\d+)-(\d+)$", r)
                    if m:
                        spans.append((m.group(1), int(m.group(2)), int(m.group(3))))
            elif u["t"] == "sym" and sc >= 0.35 * top:
                sym = store.symbols[u["k"]][u["i"]]
                if sym.get("kind") not in ("class", "impl", "module", "section") or sym["end"] - sym["start"] < 60:
                    spans.append((u["k"], sym["start"], sym["end"]))
        ex_used, seen = 0, []
        for path, a, b in spans:
            if any(p == path and not (b < x or a > y) for p, x, y in seen):
                continue
            b = min(b, a + EXCERPT_MAX_LINES - 1)
            try:
                with open(os.path.join(store.root, path), encoding="utf-8", errors="replace") as fh:
                    src = fh.read().splitlines()[a - 1:b]
            except OSError:
                continue
            text = f"SOURCE {path}:L{a}-{a + len(src) - 1}\n" + "\n".join(f"{a + i}|{ln}" for i, ln in enumerate(src))
            t = est_tokens(text)
            if ex_used + t > excerpt_budget or used + t > budget + excerpt_budget:
                continue
            sections.append(text)
            ex_used += t
            seen.append((path, a, b))
            if len(seen) >= 4:
                break
    # 5. The module that owns the best file, for orientation and how-to recipes.
    for sc, u in hits:
        if u["t"] == "module" and sc >= 0.4 * top:
            m = store.modules[u["k"]]
            txt = f"MODULE {u['k']}/: {m.get('purpose', '')}"
            how = [h for h in m.get("how_to", []) if qtok & set(tokens(h))][:3]
            if how:
                txt += "\n  how-to: " + "\n  how-to: ".join(how)
            add(f"module:{u['k']}", txt)
            break
    return "\n\n".join(sections), info


def card_for(store: Store, path: str, max_syms: int = 80) -> str:
    """Full compiled card for one file: purpose, summary, symbol map with exact spans, facts, gotchas."""
    c = store.cards.get(path)
    syms = store.symbols.get(path, [])
    meta = store.manifest.get("files", {}).get(path, {})
    if not c and not syms:
        return ""
    stale = store.is_stale(path)
    out = [f"{path} ({meta.get('lines', '?')} lines){' ⚠stale: changed since compile; spans may be off' if stale else ''}"]
    if c:
        out.append(f"purpose: {c.get('purpose', '')}\n{c.get('summary', '')}")
    notes = {x.get("name"): x.get("does", "") for x in (c or {}).get("symbols", []) if isinstance(x, dict)}
    out.append("symbols:")
    for sym in syms[:max_syms]:
        note = notes.get(sym["name"])
        out.append(f"  {_span(sym)} {sym['name']}" + (f" — {note}" if note else ""))
    if len(syms) > max_syms:
        out.append(f"  … {len(syms) - max_syms} more")
    if c and c.get("facts"):
        out.append("facts: " + " | ".join(map(str, c["facts"][:20])))
    if c and c.get("gotchas"):
        out.append("gotchas: " + " | ".join(map(str, c["gotchas"][:8])))
    return "\n".join(out)


def where(store: Store, name: str, limit: int = 10) -> list[str]:
    """Exact symbol lookup (full name, or last component), with the compiled one-line note."""
    name = name.strip().rstrip("()")
    tail = name.split(".")[-1].split("::")[-1]
    out = []
    for path, syms in store.symbols.items():
        for sym in syms:
            if sym["name"] == name or sym["name"].split(".")[-1] == tail:
                note = next((x.get("does", "") for x in store.cards.get(path, {}).get("symbols", [])
                             if isinstance(x, dict) and x.get("name") == sym["name"]), "")
                out.append(f"{path}:{_span(sym)} {sym['kind']} {sym['name']}" + (f" — {note[:200]}" if note else ""))
    exact = [o for o in out if f" {name}" in o]
    return (exact + [o for o in out if o not in exact])[:limit]


# ---------------------------------------------------------------- model-selected packs
SELECT_PROMPT = """A developer asked about a code repository:
"{query}"

Below are candidate pieces of compiled knowledge (id: description). Pick the pieces an engineer needs to answer
completely and precisely: the code that implements the behaviour asked about, the precompiled answers that address it,
and tests or docs only if the question is about them. Prefer specific functions/methods over whole files or classes.

Return ONLY a JSON list of up to {k} ids, most important first.

{cands}"""


def candidates(store: Store, query: str, ix: Index, n: int = 70) -> list[tuple[str, str]]:
    """BM25 candidates, widened with the symbols of the best files so lexical misses can still be picked."""
    hits = ix.search(query, k=120)
    out, seen = [], set()

    def add(cid, desc):
        if cid not in seen and len(out) < n:
            seen.add(cid)
            out.append((cid, desc))

    top_files = []
    for sc, u in hits:
        if u["t"] == "file" and len(top_files) < 8:
            top_files.append(u["k"])
        elif u["t"] == "sym" and u["k"] not in top_files and len(top_files) < 8:
            top_files.append(u["k"])
    for sc, u in hits[:45]:
        if u["t"] == "qa":
            add(f"q{u['i']}", "Q&A: " + store.qa[u["i"]]["q"][:160])
        elif u["t"] == "sym":
            sym = store.symbols[u["k"]][u["i"]]
            add(f"s:{u['k']}:{u['i']}", f"{u['k']}::{sym['name']} ({sym['kind']}): {_note(store, u['k'], sym['name'])[:140]}")
        elif u["t"] == "flow":
            add(f"f{u['i']}", "flow: " + store.flows[u["i"]].get("name", ""))
        elif u["t"] == "file":
            add(f"F:{u['k']}", f"file {u['k']}: {store.cards.get(u['k'], {}).get('purpose', '')[:140]}")
    for path in top_files:
        for i, sym in enumerate(store.symbols.get(path, [])):
            if sym.get("kind") in ("section",) or (sym.get("kind") in ("variable", "attribute") and not _note(store, path, sym["name"])):
                continue
            add(f"s:{path}:{i}", f"{path}::{sym['name']} ({sym['kind']}): {_note(store, path, sym['name'])[:120]}")
    return out


def _note(store: Store, path: str, name: str) -> str:
    for x in store.cards.get(path, {}).get("symbols", []):
        if isinstance(x, dict) and x.get("name") == name:
            return x.get("does", "")
    return ""


def pack_selected(store: Store, ids: list[str], budget: int = 4000) -> str:
    """Render selected ids: precompiled answers, and for code pieces the compiled note plus the live source."""
    parts, used = [], 0
    for cid in ids:
        text = ""
        if cid.startswith("q") and cid[1:].isdigit() and int(cid[1:]) < len(store.qa):
            it = store.qa[int(cid[1:])]
            text = f"Q: {it['q']}\nA: {it['a']}" + (f"\n   refs: {', '.join(it.get('refs', [])[:5])}" if it.get("refs") else "")
        elif cid.startswith("f") and cid[1:].isdigit() and int(cid[1:]) < len(store.flows):
            fl = store.flows[int(cid[1:])]
            text = f"FLOW {fl.get('name')}:\n" + "\n".join(f"  {k + 1}. {s}" for k, s in enumerate(fl.get("steps", [])[:10]))
        elif cid.startswith("F:"):
            text = card_for(store, cid[2:], max_syms=25)
        elif cid.startswith("s:"):
            path, _, i = cid[2:].rpartition(":")
            syms = store.symbols.get(path, [])
            if not i.isdigit() or int(i) >= len(syms):
                continue
            sym = syms[int(i)]
            a, b = sym["start"], min(sym["end"], sym["start"] + EXCERPT_MAX_LINES - 1)
            try:
                with open(os.path.join(store.root, path), encoding="utf-8", errors="replace") as fh:
                    src = fh.read().splitlines()[a - 1:b]
            except OSError:
                continue
            note = _note(store, path, sym["name"])
            more = f"  (… continues to L{sym['end']})" if sym["end"] > b else ""
            text = (f"SOURCE {path}:L{a}-{a + len(src) - 1} {sym['name']}" + (f" — {note}" if note else "") + "\n"
                    + "\n".join(f"{a + k}|{ln}" for k, ln in enumerate(src)) + more)
        if not text:
            continue
        t = est_tokens(text)
        if used + t > budget:
            continue
        parts.append(text)
        used += t
    return "\n\n".join(parts)


def pointer_pack(store: Store, query: str, ix: Index | None = None, n_qa: int = 2, n_ptr: int = 10,
                 min_score: float = 0.0, exclude: set | None = None, excerpt_lines: int = 0) -> tuple[str, dict]:
    """A small, high-precision pack: the best precompiled answers plus a map of exact spans with one-line notes.

    Injected context is re-read on every later turn of a session, so it must be small; the map lets the agent
    fetch everything it still wants in ONE parallel batch of span reads instead of a chain of searches."""
    ix = ix or Index.load(store)
    hits = ix.search(query, k=80)
    info = {"top": hits[0][0] if hits else 0.0, "units": []}
    if not hits or hits[0][0] < min_score:
        return "", info
    exclude = exclude or set()
    top = hits[0][0]
    parts = []
    for sc, u in hits:
        if u["t"] == "qa" and sc >= 0.55 * top and len(info["units"]) < n_qa and f"qa:{u['i']}" not in exclude:
            it = store.qa[u["i"]]
            parts.append(f"Q: {it['q']}\nA: {it['a']}")
            info["units"].append(f"qa:{u['i']}")
    # Rank spans: symbol hits, plus spans cited by the chosen answers.
    ptrs: dict = {}
    for unit in info["units"]:
        for r in store.qa[int(unit[3:])].get("refs", [])[:4]:
            m = re.match(r"(.+):L(\d+)-(\d+)$", r)
            if m:
                ptrs.setdefault((m.group(1), int(m.group(2)), int(m.group(3))), top)
    file_boost = defaultdict(float)
    for sc, u in hits:
        if u["t"] == "file":
            file_boost[u["k"]] = max(file_boost[u["k"]], sc)
    for sc, u in hits:
        if u["t"] == "sym":
            sym = store.symbols[u["k"]][u["i"]]
            if sym.get("kind") == "section" or (sym.get("kind") == "class" and sym["end"] - sym["start"] > 150):
                continue
            key = (u["k"], sym["start"], sym["end"])
            ptrs[key] = max(ptrs.get(key, 0), sc + 0.3 * file_boost.get(u["k"], 0))
    ranked = sorted(ptrs.items(), key=lambda kv: -kv[1])
    lines, seen = [], []
    for (path, a, b), sc in ranked:
        if len(lines) >= n_ptr or sc < 0.3 * top:
            break
        if any(p == path and x <= a and b <= y for p, x, y in seen):
            continue
        seen.append((path, a, b))
        name = next((s["name"] for s in store.symbols.get(path, []) if s["start"] == a and s["end"] == b), "")
        note = _note(store, path, name) if name else ""
        stale = " ⚠stale" if store.is_stale(path) else ""
        lines.append(f"- {path}:L{a}-{b} {name}{stale}" + (f" — {note[:130]}" if note else ""))
    if lines:
        parts.append("WHERE (exact spans; Read with offset/limit):\n" + "\n".join(lines))
    if excerpt_lines and seen:
        path, a, b = seen[0]
        try:
            with open(os.path.join(store.root, path), encoding="utf-8", errors="replace") as fh:
                src = fh.read().splitlines()[a - 1:min(b, a + excerpt_lines - 1)]
            parts.append(f"SOURCE {path}:L{a}-{a + len(src) - 1}\n" + "\n".join(f"{a + i}|{ln}" for i, ln in enumerate(src)))
        except OSError:
            pass
    return "\n\n".join(parts), info


def compact_core(core: str, budget_tokens: int = 1000) -> str:
    """The core card's most orienting sections, within a token budget (the full card is one tool call away)."""
    order = ["What this is", "Module map", "Entry points", "Build, test, run", "Architecture", "Where to change things",
             "Conventions", "Glossary"]
    secs = {}
    for block in re.split(r"(?m)^## ", core):
        if block.strip():
            head, _, body = block.partition("\n")
            secs[head.strip()] = body.strip()
    out, used = [], 0
    for h in order:
        if h in secs:
            t = f"## {h}\n{secs[h]}"
            if used + est_tokens(t) > budget_tokens:
                continue
            out.append(t)
            used += est_tokens(t)
    return "\n".join(out) if out else core[: budget_tokens * 4]


# ---------------------------------------------------------------- link packs
def _read_span(store: Store, path: str, a: int, b: int) -> list[str]:
    try:
        with open(os.path.join(store.root, path), encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[a - 1:b]
    except OSError:
        return []


def _paragraph(store: Store, path: str, i: int, max_lines: int) -> str:
    """Full source of one symbol ("paragraph"); long classes are cut to their header and member list."""
    sym = store.symbols[path][i]
    a, b = sym["start"], sym["end"]
    note = _note(store, path, sym["name"])
    head = f"SOURCE {path}:L{a}-{b} {sym['name']}" + (f" — {note[:200]}" if note else "")
    if b - a + 1 <= max_lines:
        src = _read_span(store, path, a, b)
        return head + "\n" + "\n".join(f"{a + k}|{ln}" for k, ln in enumerate(src))
    src = _read_span(store, path, a, a + max_lines // 2 - 1)
    members = [s for s in store.symbols[path] if a < s["start"] <= b and s["name"].startswith(sym["name"] + ".")]
    tail = "\n".join(f"   … {m['name']} L{m['start']}-{m['end']}" for m in members[:25])
    return (head + "\n" + "\n".join(f"{a + k}|{ln}" for k, ln in enumerate(src))
            + f"\n   … (continues to L{b})" + (f"\n{tail}" if tail else ""))


def link_pack(store: Store, query: str, budget: int = 3500, ix: Index | None = None, n_seeds: int = 4,
              n_links: int = 12, body_links: int = 2, max_lines: int = 60, n_qa: int = 1,
              min_score: float = 0.0, exclude: set | None = None, judge=None, pool: int = 60,
              judge_rel: float = 0.0,
              judge_code_lines: int = 24) -> tuple[str, dict]:
    """The minimal paragraphs needed for a question, chosen before the model is called.

    1. seeds: the best-matching paragraphs (functions/methods/classes), via compiled notes, aliases and Q&A refs;
    2. links: follow each seed's references to the definitions it uses, the code that calls it, and its tests;
    3. minimise: full source for seeds and the strongest links, one line (span + compiled note) for the rest."""
    from . import links as links_mod
    ix = ix or Index.load(store)
    hits = ix.search(query, k=100)
    info = {"top": hits[0][0] if hits else 0.0, "units": []}
    if not hits or hits[0][0] < min_score:
        return "", info
    exclude = exclude or set()
    top = hits[0][0]
    qtok = set(tokens(query))
    L = links_mod.load(store)

    # --- score candidate paragraphs
    score: dict = defaultdict(float)
    file_score: dict = defaultdict(float)
    for sc, u in hits:
        if u["t"] == "file":
            file_score[u["k"]] = max(file_score[u["k"]], sc)
    span_to_sid = {}
    for path, syms in store.symbols.items():
        for i, s in enumerate(syms):
            span_to_sid[(path, s["start"], s["end"])] = f"{path}#{i}"
    qa_used = []
    for sc, u in hits:
        if u["t"] == "sym":
            s = store.symbols[u["k"]][u["i"]]
            if s.get("kind") == "section":
                continue
            score[f"{u['k']}#{u['i']}"] += sc + 0.25 * file_score.get(u["k"], 0)
        elif u["t"] == "qa" and sc >= 0.5 * top:
            it = store.qa[u["i"]]
            if len(qa_used) < n_qa and f"qa:{u['i']}" not in exclude:
                qa_used.append(u["i"])
            for r in it.get("refs", [])[:4]:
                m = re.match(r"(.+):L(\d+)-(\d+)$", r)
                if m and (m.group(1), int(m.group(2)), int(m.group(3))) in span_to_sid:
                    score[span_to_sid[(m.group(1), int(m.group(2)), int(m.group(3)))]] += 0.6 * sc
    if not score:
        return "", info

    def sym(sid):
        p, i = sid.rsplit("#", 1)
        return p, int(i), store.symbols[p][int(i)]

    def relevance(sid):  # does the linked paragraph itself talk about the question?
        p, i, s = sym(sid)
        text = s["name"] + " " + _note(store, p, s["name"])
        return len(qtok & set(tokens(text))) / max(1, len(qtok))

    # --- seeds: best paragraphs, avoiding a class and its own methods both being seeds
    seeds = []
    for sid, sc in sorted(score.items(), key=lambda kv: -kv[1]):
        if len(seeds) >= n_seeds or sc < 0.35 * max(score.values()):
            break
        p, i, s = sym(sid)
        if s.get("kind") in ("variable", "attribute", "constant") and s["end"] - s["start"] < 2 and len(seeds) >= 1:
            continue
        if any(links_mod._contains(store, sid, t) for t in seeds):
            continue
        seeds.append(sid)
    # --- links: definitions used by seeds (out), users of seeds (in); tests get their own weight
    link_score: dict = defaultdict(float)
    for rank, sid in enumerate(seeds):
        w = score[sid] / (1 + rank)
        for t in L["out"].get(sid, []):
            link_score[t] += w * (0.5 + relevance(t))
        for t in L["in"].get(sid, []):
            is_test = "test" in t.split("#")[0].split("/")[-1]
            link_score[t] += w * (0.3 + relevance(t)) * (1.2 if is_test and "test" in qtok else 0.8)
    for sid in seeds:
        link_score.pop(sid, None)
    linked = [sid for sid, _ in sorted(link_score.items(), key=lambda kv: -kv[1])
              if not any(links_mod._contains(store, sid, s) for s in seeds)][:n_links]

    if judge is not None:
        # A judgment model decides, per candidate paragraph, whether it is needed. The pool is the best-scoring
        # paragraphs plus the link neighbourhood of the heuristic seeds, so lexical misses can still be chosen.
        cand_ids = [sid for sid, _ in sorted(score.items(), key=lambda kv: -kv[1])][: pool // 2]
        cand_ids += [sid for sid in seeds + linked + [t for sd in seeds for t in L["out"].get(sd, []) + L["in"].get(sd, [])]
                     if sid not in cand_ids]
        cand_ids = cand_ids[:pool]
        cands = []
        for sid in cand_ids:
            p, i, s = sym(sid)
            src = "\n".join(_read_span(store, p, s["start"], min(s["end"], s["start"] + judge_code_lines))) \
                if judge_code_lines else ""
            cands.append((sid, f"{p}::{s['name']} (L{s['start']}-{s['end']}) — {_note(store, p, s['name'])[:300]}\n{src}"))
        probs = judge(query, cands) or {}
        # Absolute cut (calibrated judges such as Jev) or relative-to-best cut (judges whose probabilities run
        # low, such as Laya on this task): keep what the judge ranks close to its best candidate.
        best = max(probs.values(), default=0)
        cut = judge_rel * best if judge_rel else 0.5
        chosen = [sid for sid in sorted(probs, key=lambda x: -probs[x]) if probs[sid] >= cut]
        if chosen:
            seeds, linked = [], []
            for sid in chosen:
                if any(links_mod._contains(store, sid, t) for t in seeds):
                    continue
                (seeds if len(seeds) < n_seeds else linked).append(sid)
            info["judged"] = len(cands)

    # --- render within budget: answers, seeds (source), strongest links (source), other links (one line)
    parts, used = [], 0

    def add(text, key=None):
        nonlocal used
        t = est_tokens(text)
        if used + t > budget:
            return False
        parts.append(text)
        used += t
        if key:
            info["units"].append(key)
        return True

    for i in qa_used:
        it = store.qa[i]
        add(f"Q: {it['q']}\nA: {it['a']}", f"qa:{i}")
    for sid in seeds:
        p, i, s = sym(sid)
        add(_paragraph(store, p, i, max_lines), f"sym:{sid}")
    for sid in linked[:body_links]:
        p, i, s = sym(sid)
        if s["end"] - s["start"] < max_lines:
            add(_paragraph(store, p, i, max_lines // 2), f"sym:{sid}")
    rest = []
    for sid in linked:
        if f"sym:{sid}" in info["units"]:
            continue
        p, i, s = sym(sid)
        note = _note(store, p, s["name"])
        rest.append(f"- {p}:L{s['start']}-{s['end']} {s['name']}" + (f" — {note[:120]}" if note else ""))
    if rest:
        add("LINKED (definitions used, callers, tests):\n" + "\n".join(rest))
    return "\n\n".join(parts), info
