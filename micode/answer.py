"""Answer a question about the repository with one reader call over the compiled artifact (no agent loop)."""
from __future__ import annotations

import json
import os
import subprocess
import time

from . import retrieve
from .store import DIR, Store

READER_SYSTEM = ("You answer questions about one code repository for a software engineer. You are given compiled "
                 "knowledge (precompiled answers, file cards, exact line spans) and live source excerpts. Answer only "
                 "from that material, precisely and completely, naming the specific files, classes, functions, "
                 "values and line spans involved. If the material does not settle part of the question, say which "
                 "part and where in the code to look.")


AGENT_SYSTEM = READER_SYSTEM + (" You may use Read (with offset/limit), Grep and Glob to fetch what the material lacks: "
                                "read only the exact spans you need, batch independent reads in one step, and stop as soon "
                                "as you can answer. The material's spans are exact; do not re-read them to double-check.")


EXPLORER_SYSTEM = ("You answer questions about one code repository for a software engineer, precisely and completely, "
                   "naming the specific files, classes, functions, values and line spans involved. You start with a "
                   "compiled map of the repository (its core card, precompiled answers and exact spans relevant to the "
                   "question). First decide which code the question is really about; the map is usually right but may "
                   "be off-topic. Then read the exact spans you need with Read (offset/limit), batching independent "
                   "reads in one step, and use Grep/Glob only for what the map lacks. Stop as soon as you can answer. "
                   "A complete answer covers every mechanism involved (definitions, callers, configuration, error "
                   "paths, and the tests that pin the behaviour when relevant), not only the first one found.")


def answer(store: Store, question: str, reader: str = "haiku", pack_budget: int = 6000, excerpt_budget: int = 6000,
           timeout: int = 300, tools: bool = False, max_turns: int = 8, mode: str = "pack", judge=None) -> dict:
    t0 = time.time()
    if mode == "link":  # minimal linked paragraphs chosen before the model is called; no exploration
        text, _ = retrieve.link_pack(store, question, budget=8000, n_seeds=8, n_links=24, body_links=6,
                                     max_lines=80, n_qa=2, judge=judge, pool=20,
                                     judge_rel=0.25 if judge is not None and getattr(judge, "relative", True) else 0.0,
                                     judge_code_lines=0)
        material = text or "(no compiled knowledge matched)"
    elif mode == "control":  # ablation: the same lean explorer with NO compiled knowledge
        material = "(no map available; explore the repository in the current directory)"
        tools, max_turns = True, max(max_turns, 12)
    elif mode == "explore":
        ptrs, _ = retrieve.pointer_pack(store, question, n_qa=2, n_ptr=16)
        material = f"CORE CARD\n{retrieve.compact_core(store.core, 1000)}\n\n{ptrs}"
        tools, max_turns = True, max(max_turns, 12)
    else:
        text, _ = retrieve.pack(store, question, budget=pack_budget, excerpt_budget=excerpt_budget)
        ptrs, _ = retrieve.pointer_pack(store, question, n_qa=0, n_ptr=16)
        material = f"{text}\n\n{ptrs}".strip() or "(no compiled knowledge matched)"
    prompt = f"COMPILED KNOWLEDGE AND SOURCE:\n{material}\n\nQUESTION: {question}"
    cmd = ["claude", "-p", "--model", reader, "--output-format", "json", "--setting-sources", "",
           "--strict-mcp-config", "--no-session-persistence"]
    if tools:
        # A lean agent: ~200-token system prompt instead of Claude Code's ~13k, three read-only tools.
        cmd += ["--system-prompt", EXPLORER_SYSTEM if mode in ("explore", "control") else AGENT_SYSTEM, "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob",
                "--max-turns", str(max_turns)]
        cwd = store.root
    else:
        cmd += ["--system-prompt", READER_SYSTEM, "--tools", ""]
        cwd = os.path.join(store.root, DIR)
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, MICODE_CHILD="1"), cwd=cwd)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return {"answer": "", "cost": 0.0, "tokens": 0, "seconds": time.time() - t0, "pack_tokens": len(material) // 4,
                "error": (r.stderr or r.stdout)[-300:]}
    tok = sum(mu.get(k, 0) for mu in (d.get("modelUsage") or {}).values()
              for k in ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens"))
    return {"answer": d.get("result", ""), "cost": d.get("total_cost_usd", 0.0), "tokens": tok, "turns": d.get("num_turns", 1),
            "seconds": round(time.time() - t0, 1), "pack_tokens": len(material) // 4}
