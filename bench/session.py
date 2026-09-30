"""Session benchmark: a developer asks several questions in ONE Claude Code session.

This is where real token usage comes from: every turn re-sends everything already in the context, so
whatever an agent reads early is paid for again on every later turn. Each session here asks 8 consecutive
SWE-QA questions (resumed with --resume); tokens and cost are summed over the whole session, and every
answer is scored by the SWE-QA judge against its reference.

usage: python3 bench/session.py --repo pytest --sessions 6 --per 8 --arms baseline,micode -j 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402

VARIANTS = {  # arm name -> MICODE_CONFIG overrides for the plugin's hooks
    "micode": {},
    "micode_reader": {"reader": "haiku"},
}


def step(arm: str, repo: str, q: str, model: str, session_id: str | None) -> dict:
    base = arm.split("_")[0] if arm != "baseline" else "baseline"
    cwd = run.baseline_copy(repo) if base == "baseline" else os.path.join(run.BENCH, "repos", repo)
    cmd = ["claude", "-p", run.PROMPT.format(q=q), "--model", model, "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--max-budget-usd", "3", "--strict-mcp-config",
           "--disallowedTools", "WebFetch", "WebSearch", "Edit", "Write", "NotebookEdit"]
    if session_id:
        cmd += ["--resume", session_id]
    allowed = list(run.READ_ONLY)
    env = dict(os.environ, MICODE_CHILD="")
    if base == "micode":
        cmd += ["--plugin-dir", run.PLUGIN, "--mcp-config", run.MICODE_MCP]
        allowed += run.MICODE_TOOLS
        env["MICODE_CONFIG"] = json.dumps(VARIANTS.get(arm, {}))
    cmd += ["--allowedTools", *allowed]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL, env=env)
    d, calls = None, []
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            calls += [f"{c['name']} {json.dumps(c.get('input'))[:120]}" for c in ev["message"].get("content", [])
                      if c.get("type") == "tool_use"]
        elif ev.get("type") == "result":
            d = ev
    if d is None:
        return {"error": (r.stderr or r.stdout)[-600:]}
    tok = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for mu in (d.get("modelUsage") or {}).values():
        tok["input"] += mu.get("inputTokens", 0)
        tok["cache_write"] += mu.get("cacheCreationInputTokens", 0)
        tok["cache_read"] += mu.get("cacheReadInputTokens", 0)
        tok["output"] += mu.get("outputTokens", 0)
    tok["total"] = sum(tok.values())
    return {"answer": d.get("result", ""), "cost": d.get("total_cost_usd", 0.0), "turns": d.get("num_turns"),
            "tokens": tok, "session_id": d.get("session_id"), "calls": calls}


def reader_usage(repo: str, sids: set) -> tuple[int, float]:
    path = os.path.join(run.BENCH, "repos", repo, ".micode", ".cache", "reader_usage.jsonl")
    tok, cost = 0, 0.0
    try:
        for line in open(path):
            r = json.loads(line)
            if r.get("session") in sids:
                tok += r["tokens"]
                cost += r["cost"]
    except OSError:
        pass
    return tok, cost


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--per", type=int, default=8)
    ap.add_argument("--arms", default="baseline,micode")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--judge", default="opus")
    ap.add_argument("-j", "--jobs", type=int, default=3)
    args = ap.parse_args()
    qs = [json.loads(line) for line in open(os.path.join(run.QDIR, f"{args.repo}.jsonl"))]
    out_dir = os.path.join(run.HERE, "results_session", args.repo)
    os.makedirs(out_dir, exist_ok=True)
    run.baseline_copy(args.repo)

    def session(arm: str, s: int) -> dict:
        path = os.path.join(out_dir, f"{arm}__{args.model}__s{s}.json")
        if os.path.exists(path):
            return json.load(open(path))
        sid, steps, sids = None, [], set()
        for k in range(s * args.per, min(len(qs), (s + 1) * args.per)):
            r = step(arm, args.repo, qs[k]["question"], args.model, sid)
            if r.get("error"):
                r["k"] = k
                steps.append(r)
                break
            sid = r["session_id"]
            sids.add(sid)
            r["k"] = k
            r["score"] = run.judge(qs[k]["question"], qs[k]["answer"], r["answer"], args.judge)
            steps.append(r)
        rt, rc = reader_usage(args.repo, sids) if arm != "baseline" else (0, 0.0)
        res = {"arm": arm, "repo": args.repo, "session": s, "model": args.model, "steps": steps,
               "reader_tokens": rt, "reader_cost": rc,
               # a resumed `claude -p` reports usage for the whole session so far: the last step is the total
               "tokens": steps[-1].get("tokens", {}).get("total", 0) + rt,
               "cost": steps[-1].get("cost", 0) + rc,
               "turns": sum(x.get("turns") or 0 for x in steps),  # num_turns is per invocation
               "score": [x["score"]["total"] for x in steps if x.get("score")]}
        json.dump(res, open(path, "w"), indent=1)
        return res

    tasks = [(a, s) for s in range(args.sessions) for a in args.arms.split(",")]
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(session, a, s): (a, s) for a, s in tasks}
        for fut in as_completed(futs):
            a, s = futs[fut]
            try:
                r = fut.result()
                sc = r["score"]
                print(f"{a:>14} s{s} tokens={r['tokens']:,} cost=${r['cost']:.3f} turns={r['turns']} "
                      f"score={sum(sc) / max(1, len(sc)):.1f} ({len(sc)} answers)", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"{a} s{s} failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
