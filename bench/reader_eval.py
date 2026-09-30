"""Reader path: answer a repository question with ONE cheap model call over the compiled artifact.

No agent loop. Retrieval (compiled answers, file cards, exact spans, live source excerpts) builds a pack; a reader
model answers from it. This is the MII idea applied to code: a strong compiler understands once, a cheap
reader answers many times. Scored by the same SWE-QA judge as the agent arms.

usage: python3 bench/reader_eval.py quill --reader haiku
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402
from micode import answer  # noqa: E402
from micode.store import Store  # noqa: E402


def make_judge(kind: str):
    """'jev': TypeSafe Jev per-paragraph yes/no; 'proxy-haiku': one Haiku call picks the needed paragraphs."""
    if kind == "laya":
        from micode import judge as J
        return J.get(timeout=120)
    if kind == "jev":
        from micode import jev
        return jev.needed
    if kind == "proxy-haiku":
        from micode.llm import LLM, parse_json
        llm = LLM(os.path.join(run.BENCH, "sel_cache"))

        def judge(q, cands):
            txt = "\n\n".join(f"[{k}] {t[:600]}" for k, (_, t) in enumerate(cands))
            out = llm.complete(f"Developer question: {q}\n\nFor each numbered candidate below decide if it is NEEDED to answer "
                               "the question completely and precisely. Return ONLY a JSON list of the needed numbers, most "
                               f"important first (usually 3-10).\n\n{txt}", "haiku")
            try:
                ids = [int(x) for x in parse_json(out)]
            except (ValueError, TypeError):
                return {}
            return {cands[i][0]: 1.0 - 0.01 * r for r, i in enumerate(ids) if 0 <= i < len(cands)}
        return judge
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--reader", default="haiku")
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("-j", "--jobs", type=int, default=6)
    ap.add_argument("--tag", default="")
    ap.add_argument("--tools", action="store_true", help="lean agent: reader may Read/Grep/Glob")
    ap.add_argument("--explore", action="store_true", help="lean explorer: small map, reads what it needs")
    ap.add_argument("--control", action="store_true", help="ablation: lean explorer without the compiled map")
    ap.add_argument("--link", action="store_true", help="answer from the preselected link pack only")
    ap.add_argument("--judge", default="", help="paragraph judge for the link pack: 'jev' or 'proxy-haiku'")
    a = ap.parse_args()
    store = Store(os.path.join(run.BENCH, "repos", a.repo))
    qs = [json.loads(line) for line in open(os.path.join(run.QDIR, f"{a.repo}.jsonl"))][: a.n]
    out_dir = os.path.join(run.HERE, "results", a.repo)
    arm = f"{('linkpack' + ('-' + a.judge if a.judge else '')) if a.link else 'control' if a.control else 'explorer' if a.explore else 'leanagent' if a.tools else 'reader'}-{a.reader}{a.tag}"

    def one(k):
        path = os.path.join(out_dir, f"{arm}__{a.reader}__{k:03d}.json")
        if os.path.exists(path):
            return json.load(open(path))
        r = answer.answer(store, qs[k]["question"], reader=a.reader, tools=a.tools, judge=make_judge(a.judge),
                          mode="link" if a.link else "control" if a.control else "explore" if a.explore else "pack")
        res = {"arm": arm, "repo": a.repo, "k": k, "question": qs[k]["question"], "model": a.reader,
               "answer": r["answer"], "cost": r["cost"], "turns": r.get("turns", 1), "duration_s": r["seconds"],
               "tokens": {"input": r["tokens"], "cache_write": 0, "cache_read": 0, "output": 0, "total": r["tokens"]},
               "pack_tokens": r["pack_tokens"]}
        res["score"] = run.judge(qs[k]["question"], qs[k]["answer"], res["answer"], "opus")
        json.dump(res, open(path, "w"), indent=1)
        return res

    with ThreadPoolExecutor(a.jobs) as ex:
        rs = list(ex.map(one, range(len(qs))))
    rs = [r for r in rs if r.get("score")]
    print(f"{arm}: n={len(rs)} score={st.mean(r['score']['total'] for r in rs):.1f} "
          f"correctness={st.mean(r['score']['correctness'] for r in rs):.1f} tokens={st.mean(r['tokens']['total'] for r in rs):,.0f} "
          f"cost=${st.mean(r['cost'] for r in rs):.4f} time={st.mean(r['duration_s'] for r in rs):.1f}s "
          f"turns={st.mean(r['turns'] for r in rs):.1f}")


if __name__ == "__main__":
    main()
