"""Head-to-head benchmark: Claude Code with vs. without micode on SWE-QA repository questions.

Both arms run the same agent model, the same prompt, the same read-only tools, on the same commit.
The baseline runs in a copy of the repository with no .micode/ directory. Answers are scored by the
SWE-QA LLM-as-a-judge prompt (verbatim, 5 dimensions x 20 points) against SWE-QA reference answers.

usage: python3 bench/run.py --repo flask --n 48 --arms baseline,micode -j 4
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
BENCH = os.environ.get("MICODE_BENCH", "/home/sq/micode-bench")
QDIR = os.path.join(BENCH, "SWE-QA-Bench", "Benchmark")
JUDGE = open(os.path.join(HERE, "judge_prompt.txt")).read()

PROMPT = ("Answer the following question about the code in this repository (current working directory). "
          "Investigate as needed, then give a precise, complete answer that names the specific files, classes and "
          "functions involved.\n\nQuestion: {q}")

READ_ONLY = ["Read", "Grep", "Glob", "Agent", "Task", "TodoWrite",
             "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
             "Bash(tail:*)", "Bash(sed -n:*)", "Bash(wc:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git grep:*)"]
MICODE_TOOLS = ["mcp__micode__ask", "mcp__micode__where", "mcp__micode__card", "mcp__micode__module", "mcp__micode__deps",
                f"Bash(python3 {PLUGIN}/bin/micode:*)"]
# Both arms use --strict-mcp-config so no account-level connectors add tool tokens; the micode arm gets exactly
# the plugin's own server, passed explicitly.
MICODE_MCP = json.dumps({"mcpServers": {"micode": {"command": "python3", "args": [f"{PLUGIN}/bin/micode", "mcp"]}}})


def baseline_copy(repo: str) -> str:
    src = os.path.join(BENCH, "repos", repo)
    dst = os.path.join(BENCH, "base", repo)
    if not os.path.exists(dst):
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".micode"))
    return dst


def run_agent(arm: str, repo: str, q: str, model: str, max_budget: float, config: str = "{}") -> dict:
    cwd = baseline_copy(repo) if arm == "baseline" else os.path.join(BENCH, "repos", repo)
    cmd = ["claude", "-p", PROMPT.format(q=q), "--model", model, "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--no-session-persistence", "--max-budget-usd", str(max_budget),
           "--disallowedTools", "WebFetch", "WebSearch", "Edit", "Write", "NotebookEdit"]
    allowed = list(READ_ONLY)
    cmd += ["--strict-mcp-config"]
    if arm == "micode":
        cmd += ["--plugin-dir", PLUGIN, "--mcp-config", MICODE_MCP]
        allowed += MICODE_TOOLS
    cmd += ["--allowedTools", *allowed]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, MICODE_CHILD="", MICODE_CONFIG=config))
    d, calls = None, []
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_use":
                    calls.append(f"{c['name']} {json.dumps(c.get('input'))[:160]}")
        elif ev.get("type") == "result":
            d = ev
    if d is None:
        return {"error": (r.stderr or r.stdout)[-800:], "wall": time.time() - t0}
    tok = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for mu in (d.get("modelUsage") or {}).values():
        tok["input"] += mu.get("inputTokens", 0)
        tok["cache_write"] += mu.get("cacheCreationInputTokens", 0)
        tok["cache_read"] += mu.get("cacheReadInputTokens", 0)
        tok["output"] += mu.get("outputTokens", 0)
    tok["total"] = sum(tok.values())
    return {"answer": d.get("result", ""), "cost": d.get("total_cost_usd", 0.0), "turns": d.get("num_turns"),
            "duration_s": (d.get("duration_ms") or 0) / 1000, "tokens": tok, "is_error": d.get("is_error"),
            "subtype": d.get("subtype"), "models": list((d.get("modelUsage") or {}).keys()), "calls": calls}


def judge(q: str, ref: str, cand: str, model: str) -> dict | None:
    prompt = JUDGE.replace("{question}", q).replace("{reference}", ref).replace("{candidate}", cand)
    for _ in range(3):
        r = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
                            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, MICODE_CHILD="1"))
        try:
            txt = json.loads(r.stdout)["result"]
            m = re.search(r"\{.*\}", txt, re.S)
            s = json.loads(m.group(0))
            keys = ["correctness", "completeness", "relevance", "clarity", "reasoning"]
            if all(1 <= int(s[k]) <= 20 for k in keys):
                s = {k: int(s[k]) for k in keys}
                s["total"] = sum(s.values())
                return s
        except (ValueError, KeyError, AttributeError, TypeError):
            time.sleep(3)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--arms", default="baseline,micode")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--judge", default="opus")
    ap.add_argument("--max-budget", type=float, default=3.0)
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--tag", default="", help="suffix for the results directory (e.g. a variant name)")
    ap.add_argument("--config", default="{}", help="MICODE_CONFIG JSON for the micode hooks (variant settings)")
    args = ap.parse_args()

    qs = [json.loads(line) for line in open(os.path.join(QDIR, f"{args.repo}.jsonl"))][args.start:args.start + args.n]
    out_dir = os.path.join(HERE, "results", args.repo)
    os.makedirs(out_dir, exist_ok=True)
    tasks = []
    for arm in args.arms.split(","):
        for k, item in enumerate(qs, args.start):
            path = os.path.join(out_dir, f"{arm}{args.tag}__{args.model}__{k:03d}.json")
            if os.path.exists(path) and (json.load(open(path)).get("score") or not json.load(open(path)).get("answer")):
                continue
            tasks.append((arm, k, item, path))
    print(f"{len(tasks)} runs to do", file=sys.stderr)
    if any(t[0] == "baseline" for t in tasks):
        baseline_copy(args.repo)  # once, before threads race to create it

    def work(t):
        arm, k, item, path = t
        if os.path.exists(path):
            res = json.load(open(path))  # agent already ran; only the judge is missing
        else:
            res = run_agent(arm, args.repo, item["question"], args.model, args.max_budget, args.config)
            res.update({"arm": arm + args.tag, "repo": args.repo, "k": k, "question": item["question"], "model": args.model})
            json.dump(res, open(path, "w"), indent=1)
        if res.get("answer"):
            try:
                res["score"] = judge(item["question"], item["answer"], res["answer"], args.judge)
            except Exception as e:  # noqa: BLE001
                res["judge_error"] = str(e)
            json.dump(res, open(path, "w"), indent=1)
        return res

    with ThreadPoolExecutor(args.jobs) as ex:
        for fut in as_completed([ex.submit(work, t) for t in tasks]):
            r = fut.result()
            sc = (r.get("score") or {}).get("total")
            print(f"{r['arm']:>10} q{r['k']:03d} score={sc} tokens={r.get('tokens', {}).get('total')} "
                  f"cost=${r.get('cost', 0):.3f} turns={r.get('turns')} {r.get('error', '')[:120]}", file=sys.stderr)


if __name__ == "__main__":
    main()
