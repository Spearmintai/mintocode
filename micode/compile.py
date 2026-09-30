"""The compile pipeline: repository -> .micode/ artifact.

Stages (each stage is cached and incremental):
  0. scan + symbols + import graph        deterministic, no model
  1. file cards                           one model call per batch of small files / per chunk of a big file
  2. module cards                         from file cards + dependency facts
  3. question-space expansion (QA)        from the module's line-numbered source + cards
  4. core card + flows                    from module cards + manifests
  5. verification                         every citation resolved against the symbol table
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import prompts, scan, store, symbols
from .llm import LLM, USAGE, LLMError

CHUNK_LINES = 1100          # a single file card call covers at most this many lines
BATCH_LINES = 1400          # small files are batched up to this many lines per call
BATCH_FILES = 12
QA_SOURCE_CHARS = 110_000   # line-numbered source shown to one QA call
MODULE_MIN_LINES = 350      # smaller directories fold into their parent module
DATA_MAX_LINES = 400        # non-code data/config files longer than this are indexed but not compiled
CORE_WORDS = 1100
PROGRAM_LANGS = {"python", "javascript", "typescript", "go", "rust", "java", "kotlin", "scala", "swift", "ruby", "php",
                 "c", "cpp", "csharp", "dart", "elixir", "lua", "vue", "svelte"}


def log(msg: str) -> None:
    print(f"[micode] {msg}", file=sys.stderr, flush=True)


def numbered(text: str, start: int = 1) -> str:
    return "\n".join(f"{i}|{line}" for i, line in enumerate(text.splitlines(), start))


def tok_estimate(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------- stage 0
def analyse(root: str):
    files = scan.scan(root)
    raw_imports, lang_of = {}, {}
    for f in files:
        text = scan.read_text(root, f.path)
        f.symbols, f.imports = symbols.extract(text, f.lang)
        raw_imports[f.path] = f.imports
        lang_of[f.path] = f.lang
    graph = symbols.resolve_imports(raw_imports, lang_of)
    return files, graph


def assign_modules(files) -> dict[str, str]:
    """Directory-based modules; small directories fold into their parent."""
    lines = {f.path: f.lines for f in files}
    mod_of = {f.path: os.path.dirname(f.path) or "." for f in files}
    while True:
        totals = defaultdict(int)
        for pth, m in mod_of.items():
            totals[m] += lines[pth]
        # Fold the deepest undersized module first, then re-total.
        small = [m for m, t in totals.items() if m != "." and t < MODULE_MIN_LINES]
        if not small:
            return mod_of
        m = max(small, key=lambda x: x.count("/"))
        parent = os.path.dirname(m) or "."
        for pth in [q for q, mm in mod_of.items() if mm == m]:
            mod_of[pth] = parent


# ---------------------------------------------------------------- stage 1
def plan_card_jobs(root: str, files, todo: set[str]):
    """Group files needing cards into model calls: batches of small files, chunks of large files."""
    jobs, batch, batch_lines = [], [], 0
    by_dir = sorted((f for f in files if f.path in todo), key=lambda f: (os.path.dirname(f.path), f.path))
    for f in by_dir:
        if not f.is_code and f.lines > DATA_MAX_LINES and f.lang not in ("markdown", "rst", "text", "asciidoc"):
            continue
        if f.is_test and f.lang not in PROGRAM_LANGS:
            continue  # test fixtures (sql/yaml/json/text) are indexed but not compiled
        if f.lines > CHUNK_LINES:
            text = scan.read_text(root, f.path).splitlines()
            # Cut at top-level symbol boundaries near each CHUNK_LINES mark.
            cuts, start = [], 1
            starts = sorted({s["start"] for s in f.symbols if "." not in s["name"]})
            while start <= len(text):
                end = min(len(text), start + CHUNK_LINES - 1)
                if end < len(text):
                    better = [s - 1 for s in starts if start + CHUNK_LINES // 2 < s <= end]
                    if better:
                        end = better[-1]
                cuts.append((start, end))
                start = end + 1
            for k, (a, b) in enumerate(cuts):
                jobs.append({"files": [f], "range": (a, b), "part": (k + 1, len(cuts))})
            continue
        if batch and (batch_lines + f.lines > BATCH_LINES or len(batch) >= BATCH_FILES):
            jobs.append({"files": batch})
            batch, batch_lines = [], 0
        batch.append(f)
        batch_lines += f.lines
    if batch:
        jobs.append({"files": batch})
    return jobs


def run_card_job(llm: LLM, model: str, root: str, job, graph, rgraph):
    parts = []
    for f in job["files"]:
        text = scan.read_text(root, f.path)
        a, b = job.get("range", (1, f.lines))
        seg = "\n".join(text.splitlines()[a - 1:b])
        syms = [s for s in f.symbols if s["end"] >= a and s["start"] <= b]
        sym_list = "\n".join(f"- {s['name']} ({s['kind']}, L{s['start']}-{s['end']}): {s['sig']}" for s in syms[:300])
        parts.append(
            f"=== FILE {f.path} ({f.lang}, {f.lines} lines{'' if 'range' not in job else f', showing L{a}-{b}'})\n"
            f"imports (in-repo): {', '.join(graph.get(f.path, [])[:25]) or 'none'}\n"
            f"imported by: {', '.join(rgraph.get(f.path, [])[:25]) or 'none'}\n"
            f"symbols:\n{sym_list or '(none)'}\n--- source ---\n{numbered(seg, a)}\n")
    part_note = ""
    if "part" in job:
        k, n = job["part"]
        part_note = (f"\nThis is PART {k} of {n} of a large file: describe only what is shown, but still emit the full "
                     "object shape; 'purpose' should describe the whole file as far as you can tell.")
    prompt = prompts.FILE_CARD.format(part_note=part_note, body="\n".join(parts))
    out = llm.complete_json(prompt, model, prompts.SYSTEM)
    cards = out.get("files", []) if isinstance(out, dict) else out
    return job, cards


def merge_parts(parts: list[dict]) -> dict:
    parts = [p for p in parts if p]
    if not parts:
        return {}
    card = dict(parts[0])
    for p in parts[1:]:
        card["summary"] = (card.get("summary", "") + " " + p.get("summary", "")).strip()
        for k in ("symbols", "facts", "gotchas", "questions"):
            card[k] = (card.get(k) or []) + (p.get(k) or [])
    return card


# ---------------------------------------------------------------- stage 2/3/4 helpers
def card_text(c: dict, full: bool = True) -> str:
    if not c:
        return ""
    out = [f"### {c.get('path')}\npurpose: {c.get('purpose', '')}\n{c.get('summary', '')}"]
    if full:
        for s in c.get("symbols", [])[:60]:
            out.append(f"- {s.get('name')}: {s.get('does', '')}")
        if c.get("facts"):
            out.append("facts: " + " | ".join(map(str, c["facts"][:25])))
        if c.get("gotchas"):
            out.append("gotchas: " + " | ".join(map(str, c["gotchas"][:10])))
    return "\n".join(out)


def module_text(m: dict) -> str:
    lines = [f"## module {m['module']}/ — {m.get('purpose', '')}", m.get("overview", "")]
    for k in m.get("key_files", [])[:15]:
        lines.append(f"- {k.get('path')}: {k.get('role')}")
    if m.get("interfaces"):
        lines.append("interfaces: " + " | ".join(m["interfaces"][:12]))
    if m.get("how_to"):
        lines.append("how-to: " + " | ".join(m["how_to"][:8]))
    return "\n".join(lines)


MANIFEST_NAMES = ("README.md", "README.rst", "README", "package.json", "pyproject.toml", "setup.py", "setup.cfg",
                  "Cargo.toml", "go.mod", "Makefile", "justfile", "CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md",
                  "tox.ini", "noxfile.py", "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml")


def manifests_text(root: str, files) -> str:
    out, budget = [], 40_000
    names = {f.path for f in files}
    for n in MANIFEST_NAMES:
        if n in names or os.path.exists(os.path.join(root, n)):
            try:
                t = open(os.path.join(root, n), encoding="utf-8", errors="replace").read()[:12_000]
            except OSError:
                continue
            out.append(f"=== {n}\n{t}")
            budget -= len(t)
            if budget < 0:
                break
    ci = [f.path for f in files if f.path.startswith(".github/workflows/")][:2]
    for c in ci:
        out.append(f"=== {c}\n{scan.read_text(root, c)[:4000]}")
    return "\n\n".join(out)


# ---------------------------------------------------------------- driver
def compile_repo(root: str, model: str = "opus", card_model: str | None = None, jobs: int = 8,
                 qa_per_kloc: int = 14, full: bool = False) -> dict:
    t0 = time.time()
    root = os.path.abspath(root)
    card_model = card_model or model
    sdir = os.path.join(root, store.DIR)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, ".gitignore"), "w") as f:
        f.write(".cache/\n*.tmp\n")
    llm = LLM(os.path.join(sdir, ".cache"))
    old_manifest = store.read_json(store.p(root, "manifest.json"), {}) or {}
    old_files = old_manifest.get("files", {}) if not full else {}
    old_cards = {c["path"]: c for c in store.read_jsonl(store.p(root, "cards.jsonl"))} if not full else {}
    old_modules = {m["module"]: m for m in store.read_json(store.p(root, "modules.json"), [])} if not full else {}
    old_qa = store.read_jsonl(store.p(root, "qa.jsonl")) if not full else []

    log(f"scanning {root}")
    files, graph = analyse(root)
    rgraph = defaultdict(list)
    for a, bs in graph.items():
        for b in bs:
            rgraph[b].append(a)
    mod_of = assign_modules(files)
    fmap = {f.path: f for f in files}
    total_lines = sum(f.lines for f in files)
    log(f"{len(files)} files, {total_lines:,} lines, {len(set(mod_of.values()))} modules")

    # ---- stage 1: file cards (only for new/changed files)
    # Changed = content differs from the last compile. Files that never get a card (e.g. test fixtures) are
    # recorded in the manifest too, so they do not count as changed on every run.
    changed = {f.path for f in files if old_files.get(f.path, {}).get("sha") != f.sha}
    cards = {pth: c for pth, c in old_cards.items() if pth in fmap and pth not in changed}
    card_jobs = plan_card_jobs(root, files, changed)
    log(f"stage 1: {len(changed)} changed files -> {len(card_jobs)} card calls ({card_model})")
    parts = defaultdict(dict)
    done = 0
    with ThreadPoolExecutor(jobs) as ex:
        futs = [ex.submit(run_card_job, llm, card_model, root, j, graph, rgraph) for j in card_jobs]
        for fut in as_completed(futs):
            try:
                job, out = fut.result()
            except (LLMError, ValueError) as e:
                if "limit" in str(e).lower():
                    raise
                log(f"  card call failed: {e}")
                continue
            by_path = {c.get("path"): c for c in out if isinstance(c, dict)}
            for f in job["files"]:
                c = by_path.get(f.path) or (out[0] if len(job["files"]) == 1 and out else None)
                if c:
                    parts[f.path][job.get("part", (1, 1))[0]] = c
            done += 1
            if done % 10 == 0 or done == len(futs):
                log(f"  {done}/{len(futs)} card calls, ${USAGE.cost:.2f} so far")
    for pth, ps in parts.items():
        c = merge_parts([ps[k] for k in sorted(ps)])
        c["path"] = pth
        cards[pth] = c
    for pth in cards:
        cards[pth]["sha"] = fmap[pth].sha

    # ---- stage 2: module cards (modules containing any changed file)
    members = defaultdict(list)
    for pth, m in mod_of.items():
        members[m].append(pth)
    dirty_mods = {mod_of[pth] for pth in changed} | {m for m in members if m not in old_modules}
    modules = {m: old_modules[m] for m in members if m in old_modules and m not in dirty_mods}

    def mod_deps(m):
        out_, in_ = set(), set()
        for pth in members[m]:
            out_ |= {mod_of[x] for x in graph.get(pth, []) if mod_of.get(x) != m}
            in_ |= {mod_of[x] for x in rgraph.get(pth, []) if mod_of.get(x) != m}
        return sorted(out_), sorted(in_)

    def compile_module(m):
        mem = sorted(members[m], key=lambda x: (fmap[x].is_test, x))
        body, budget = [], 150_000
        for pth in mem:
            t = card_text(cards.get(pth, {}), full=not fmap[pth].is_test)
            if not t:
                continue
            budget -= len(t)
            body.append(t if budget > 0 else card_text(cards.get(pth, {}), full=False))
        o, i = mod_deps(m)
        tests = [x for x in mem if fmap[x].is_test] + [x for x in rgraph_mod_tests(m)]
        prompt = prompts.MODULE_CARD.format(module=m, repo=os.path.basename(root), deps_out=", ".join(o) or "nothing",
                                            deps_in=", ".join(i) or "nothing", tests=", ".join(sorted(set(tests))[:15]) or "none found",
                                            cards="\n\n".join(body))
        out = llm.complete_json(prompt, model, prompts.SYSTEM)
        out = out if isinstance(out, dict) else {}
        out.update({"module": m, "files": mem, "lines": sum(fmap[x].lines for x in mem), "deps_out": o, "deps_in": i})
        return m, out

    def rgraph_mod_tests(m):
        return [x for pth in members[m] for x in rgraph.get(pth, []) if fmap.get(x) and fmap[x].is_test]

    log(f"stage 2: {len(dirty_mods)} module cards ({model})")
    with ThreadPoolExecutor(jobs) as ex:
        for fut in as_completed([ex.submit(compile_module, m) for m in sorted(dirty_mods)]):
            try:
                m, card = fut.result()
                modules[m] = card
            except (LLMError, ValueError) as e:
                if "limit" in str(e).lower():
                    raise
                log(f"  module call failed: {e}")

    # ---- stage 3: question-space expansion over each dirty module's source
    qa = [q for q in old_qa if q.get("module") in modules and q.get("module") not in dirty_mods]

    def qa_jobs(m):
        src_files = [x for x in sorted(members[m]) if not fmap[x].is_test and (fmap[x].is_code or fmap[x].lang in ("markdown", "rst"))]
        chunks, cur, size = [], [], 0
        for pth in src_files:
            t = numbered(scan.read_text(root, pth))
            if len(t) > QA_SOURCE_CHARS:  # huge file: split into windows
                lines = t.splitlines()
                step = QA_SOURCE_CHARS // max(1, len(t) // len(lines))
                for k in range(0, len(lines), step):
                    chunks.append([(pth, "\n".join(lines[k:k + step]))])
                continue
            if cur and size + len(t) > QA_SOURCE_CHARS:
                chunks.append(cur)
                cur, size = [], 0
            cur.append((pth, t))
            size += len(t)
        if cur:
            chunks.append(cur)
        return [(m, ch) for ch in chunks]

    def run_qa(m, chunk):
        lines = sum(t.count("\n") + 1 for _, t in chunk)
        n = max(6, min(40, round(lines / 1000 * qa_per_kloc)))
        src = "\n\n".join(f"=== {pth}\n{t}" for pth, t in chunk)
        cards_txt = "\n\n".join(card_text(cards.get(pth, {}), full=False) for pth, _ in chunk)
        prompt = prompts.QA.format(repo=os.path.basename(root), module=m, n=n, module_card=module_text(modules.get(m, {"module": m})),
                                   cards=cards_txt, source=src)
        out = llm.complete_json(prompt, model, prompts.SYSTEM)
        items = out.get("qa", []) if isinstance(out, dict) else []
        for it in items:
            it["module"] = m
        return items

    all_qa_jobs = [j for m in sorted(dirty_mods) if m in modules for j in qa_jobs(m)]
    log(f"stage 3: {len(all_qa_jobs)} question-space calls ({model})")
    with ThreadPoolExecutor(jobs) as ex:
        for fut in as_completed([ex.submit(run_qa, m, ch) for m, ch in all_qa_jobs]):
            try:
                qa += fut.result()
            except (LLMError, ValueError) as e:
                if "limit" in str(e).lower():
                    raise
                log(f"  qa call failed: {e}")

    # ---- stage 4: core card + flows (whenever any module changed)
    core_path = store.p(root, "core.md")
    mods_txt = "\n\n".join(module_text(modules[m]) for m in sorted(modules))
    if dirty_mods or not os.path.exists(core_path):
        log(f"stage 4: core card + flows ({model})")
        core = llm.complete(prompts.CORE.format(repo=os.path.basename(root), words=CORE_WORDS, modules=mods_txt,
                                                manifests=manifests_text(root, files)), model, prompts.SYSTEM).strip()
        core = core.removeprefix("```markdown").removeprefix("```").removesuffix("```").strip()
        try:
            flows = llm.complete_json(prompts.FLOWS.format(repo=os.path.basename(root), n=8, core=core, modules=mods_txt),
                                      model, prompts.SYSTEM).get("flows", [])
        except (LLMError, ValueError, AttributeError) as e:
            log(f"  flows failed: {e}")
            flows = store.read_json(store.p(root, "flows.json"), [])
    else:
        core = open(core_path, encoding="utf-8").read()
        flows = store.read_json(store.p(root, "flows.json"), [])

    # ---- stage 4b: symbol aliases — how people describe each symbol without knowing its name. This moves the
    # paraphrase matching an LLM would otherwise do at query time into the compile, so runtime stays lexical and fast.
    alias_jobs, batch = [], []
    for pth in sorted(changed | {p for p in cards if not cards[p].get("aliases_done")}):
        c = cards.get(pth)
        if not c:
            continue
        for k, sym in enumerate(c.get("symbols", []) or []):
            if isinstance(sym, dict) and sym.get("name") and sym.get("does"):
                batch.append((pth, k, f"{pth}::{sym['name']} — {sym['does'][:220]}"))
                if len(batch) >= 70:
                    alias_jobs.append(batch)
                    batch = []
    if batch:
        alias_jobs.append(batch)

    def run_alias(b):
        items = "\n".join(f"a{j}: {txt}" for j, (_, _, txt) in enumerate(b))
        out = llm.complete_json(prompts.ALIASES.format(repo=os.path.basename(root), items=items), card_model, prompts.SYSTEM)
        return b, out if isinstance(out, dict) else {}

    log(f"stage 4b: {len(alias_jobs)} alias calls ({card_model})")
    with ThreadPoolExecutor(jobs) as ex:
        for fut in as_completed([ex.submit(run_alias, b) for b in alias_jobs]):
            try:
                b, out = fut.result()
            except (LLMError, ValueError) as e:
                if "limit" in str(e).lower():
                    raise
                log(f"  alias call failed: {e}")
                continue
            for j, (pth, k, _) in enumerate(b):
                al = out.get(f"a{j}")
                if isinstance(al, list):
                    cards[pth]["symbols"][k]["aka"] = [str(x)[:160] for x in al[:4]]
    for pth in cards:
        cards[pth]["aliases_done"] = True

    # ---- stage 5: verification — resolve every citation against the deterministic symbol table
    sym_table = {f.path: f.symbols for f in files}
    res = store.Resolver(sym_table, {f.path: f.lines for f in files})
    core = res.annotate(core)
    for m in modules.values():
        for k in ("overview",):
            m[k] = res.annotate(m.get(k, ""))
        for k in ("interfaces", "how_to", "gotchas"):
            m[k] = [res.annotate(x) for x in m.get(k, []) if isinstance(x, str)]
    kept_qa = []
    for it in qa:
        if not isinstance(it, dict) or not it.get("q") or not it.get("a"):
            continue
        it["a"] = res.annotate(it["a"])
        refs = [res.ref(r) for r in it.get("refs", []) if isinstance(r, str)]
        it["refs"] = [r for r in refs if r]
        kept_qa.append(it)
    for fl in flows:
        fl["steps"] = [res.annotate(s) for s in fl.get("steps", []) if isinstance(s, str)]
    # file-card symbol notes get exact spans from the table
    for pth, c in cards.items():
        spans = {s["name"]: s for s in sym_table.get(pth, [])}
        for s in c.get("symbols", []) or []:
            if isinstance(s, dict) and s.get("name") in spans:
                t = spans[s["name"]]
                s["span"] = [t["start"], t["end"]]
    verified = {"refs_ok": res.ok, "refs_unresolved": res.bad,
                "ratio": round(res.ok / max(1, res.ok + res.bad), 4)}
    log(f"stage 5: citations verified {res.ok}, unresolved {res.bad}")

    # ---- write
    manifest = {
        "format": store.FORMAT, "repo": os.path.basename(root), "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "models": {"cards": card_model, "reasoning": model},
        "stats": {"files": len(files), "lines": total_lines, "modules": len(modules), "cards": len(cards),
                  "qa": len(kept_qa), "flows": len(flows), "symbols": sum(len(f.symbols) for f in files),
                  "source_tokens_est": sum(f.bytes for f in files) // 4, "core_tokens_est": tok_estimate(core)},
        "verification": verified,
        "cost": {"usd_this_run": round(USAGE.cost, 4), "calls": USAGE.calls, "cached_calls": USAGE.cached,
                 "usd_total": round(old_manifest.get("cost", {}).get("usd_total", 0) * (not full) + USAGE.cost, 4),
                 "seconds_this_run": round(time.time() - t0)},
        "files": {f.path: {"sha": f.sha, "lines": f.lines, "bytes": f.bytes, "lang": f.lang, "module": mod_of[f.path],
                           "mtime": int(os.stat(os.path.join(root, f.path)).st_mtime), "test": f.is_test}
                  for f in files},
    }
    store.write_jsonl(store.p(root, "cards.jsonl"), [cards[k] for k in sorted(cards)])
    store.write_json(store.p(root, "modules.json"), [modules[k] for k in sorted(modules)])
    store.write_jsonl(store.p(root, "qa.jsonl"), kept_qa)
    store.write_json(store.p(root, "flows.json"), flows)
    store.write_json(store.p(root, "symbols.json"), sym_table)
    store.write_json(store.p(root, "graph.json"), graph)
    with open(core_path, "w", encoding="utf-8") as f:
        f.write(core + "\n")
    store.write_json(store.p(root, "manifest.json"), manifest)  # last: marks the artifact complete
    log(f"done in {time.time() - t0:.0f}s, ${USAGE.cost:.2f} ({USAGE.calls} calls, {USAGE.cached} cached)")
    return manifest
