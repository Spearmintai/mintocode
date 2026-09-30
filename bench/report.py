"""Summarise benchmark results: per repo and arm, SWE-QA judge score vs. tokens, cost, turns, time.

usage: python3 bench/report.py [--md]   (paired: only questions answered by every arm are compared)
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.environ.get("MICODE_BENCH", "/home/sq/micode-bench")


def load():
    rows = defaultdict(dict)  # (repo, model) -> {k: {arm: row}}
    for f in glob.glob(os.path.join(HERE, "results", "*", "*.json")):
        r = json.load(open(f))
        if not r.get("score"):
            continue
        rows[(r["repo"], r["model"])].setdefault(r["k"], {})[r["arm"]] = r
    return rows


def compile_cost(repo):
    m = json.load(open(os.path.join(BENCH, "repos", repo, ".micode", "manifest.json")))
    return m["cost"]["usd_total"], m["stats"]


def main():
    md = "--md" in sys.argv
    rows = load()
    out = []
    for (repo, model), by_k in sorted(rows.items()):
        arms = sorted({a for d in by_k.values() for a in d})
        paired = [d for d in by_k.values() if all(a in d for a in arms)]
        if not paired:
            continue
        out.append(f"\n## {repo} (agent {model}, {len(paired)} paired questions)\n")
        out.append("| arm | judge score /100 | correctness /20 | total tokens | cost $ | turns | time s |")
        out.append("|---|---|---|---|---|---|---|")
        base = None
        for a in arms:
            sc = [d[a]["score"]["total"] for d in paired]
            cor = [d[a]["score"]["correctness"] for d in paired]
            tk = [d[a]["tokens"]["total"] for d in paired]
            cost = [d[a]["cost"] for d in paired]
            turns = [d[a]["turns"] or 0 for d in paired]
            tm = [d[a]["duration_s"] for d in paired]
            row = (st.mean(sc), st.mean(cor), st.mean(tk), st.mean(cost), st.mean(turns), st.mean(tm))
            if a == "baseline":
                base = row
            out.append(f"| {a} | {row[0]:.1f} | {row[1]:.1f} | {row[2]:,.0f} | {row[3]:.3f} | {row[4]:.1f} | {row[5]:.0f} |")
        if base:
            for a in arms:
                if a == "baseline":
                    continue
                tk = st.mean(d[a]["tokens"]["total"] for d in paired)
                cost = st.mean(d[a]["cost"] for d in paired)
                sc = st.mean(d[a]["score"]["total"] for d in paired)
                wins = sum(d[a]["score"]["total"] > d["baseline"]["score"]["total"] for d in paired)
                ties = sum(d[a]["score"]["total"] == d["baseline"]["score"]["total"] for d in paired)
                out.append(f"\n{a}: {base[2] / tk:.1f}x fewer tokens, {base[3] / cost:.1f}x cheaper, "
                           f"{base[5] / max(1e-9, st.mean(d[a]['duration_s'] for d in paired)):.1f}x faster, "
                           f"score {sc - base[0]:+.1f} (wins {wins}, ties {ties}, losses {len(paired) - wins - ties})")
                try:
                    cc, stats = compile_cost(repo)
                    saved = base[3] - cost
                    if saved > 0:
                        out.append(f"compile cost ${cc:.2f} for {stats['lines']:,} lines; pays for itself after "
                                   f"{cc / saved:.0f} questions")
                except (OSError, KeyError):
                    pass
    out += session_report()
    print("\n".join(out))


def session_report():
    """Multi-question sessions. Usage of a resumed session is cumulative, so each session's total is its last step."""
    out = []
    by = defaultdict(lambda: defaultdict(dict))
    for f in glob.glob(os.path.join(HERE, "results_session", "*", "*.json")):
        r = json.load(open(f))
        if len(r["steps"]) < 2 or any(s.get("error") for s in r["steps"]):
            continue
        last = r["steps"][-1]
        by[(r["repo"], r["model"])][r["session"]][r["arm"]] = {
            "tokens": last["tokens"]["total"] + r.get("reader_tokens", 0), "cost": last["cost"] + r.get("reader_cost", 0),
            "turns": sum(s.get("turns") or 0 for s in r["steps"]), "score": st.mean(r["score"]) if r["score"] else 0, "n": len(r["steps"])}
    for (repo, model), sess in sorted(by.items()):
        arms = sorted({a for d in sess.values() for a in d})
        paired = [d for d in sess.values() if all(a in d for a in arms)]
        if not paired or len(arms) < 2:
            continue
        out.append(f"\n## {repo}: sessions of {paired[0][arms[0]]['n']} questions (agent {model}, {len(paired)} paired sessions)\n")
        out.append("| arm | judge score /100 | tokens per session | cost $ per session | turns |")
        out.append("|---|---|---|---|---|")
        for a in arms:
            out.append(f"| {a} | {st.mean(d[a]['score'] for d in paired):.1f} | {st.mean(d[a]['tokens'] for d in paired):,.0f} | "
                       f"{st.mean(d[a]['cost'] for d in paired):.3f} | {st.mean(d[a]['turns'] for d in paired):.1f} |")
        if "baseline" in arms:
            b = paired
            for a in arms:
                if a != "baseline":
                    out.append(f"\n{a}: {st.mean(d['baseline']['tokens'] for d in b) / st.mean(d[a]['tokens'] for d in b):.2f}x fewer "
                               f"tokens, {st.mean(d['baseline']['cost'] for d in b) / st.mean(d[a]['cost'] for d in b):.2f}x cheaper per session")
    return out


if __name__ == "__main__":
    main()
