"""micode command line."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import retrieve
from .store import Store, find_root


def _store(path: str | None = None) -> Store:
    root = find_root(path or os.getcwd())
    if not root:
        sys.exit("micode: no .micode/ found here or above. Run `micode compile` at the repository root.")
    return Store(root)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="micode", description="Compile a repository once; read the compiled understanding forever.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile", help="compile (or incrementally update) the repository")
    c.add_argument("path", nargs="?", default=".")
    c.add_argument("--model", default=os.environ.get("MICODE_MODEL", "opus"), help="reasoning model (module/QA/core stages)")
    c.add_argument("--card-model", default=os.environ.get("MICODE_CARD_MODEL"), help="model for per-file cards (default: --model)")
    c.add_argument("-j", "--jobs", type=int, default=8)
    c.add_argument("--qa-per-kloc", type=int, default=14, help="precompiled questions per 1000 source lines")
    c.add_argument("--full", action="store_true", help="ignore the previous compile and rebuild everything")
    sub.add_parser("update", help="alias of compile (incremental)").add_argument("path", nargs="?", default=".")
    sub.add_parser("status", help="compile stats, cost and stale files")
    a = sub.add_parser("ask", help="answer a question from the minimal linked paragraphs, in one model call")
    a.add_argument("question", nargs="+")
    a.add_argument("--model", default=os.environ.get("MICODE_READER", "sonnet"), help="model that answers (default sonnet)")
    a.add_argument("--pack", action="store_true", help="only print the selected paragraphs (no model call)")
    a.add_argument("--no-judge", action="store_true", help="skip the paragraph judge even if one is available")
    sub.add_parser("setup-judge", help="install the local Laya paragraph judge (torch + laya in its own venv)").add_argument(
        "--gpu", action="store_true", help="install CUDA torch instead of the CPU build")
    sub.add_parser("judge-server", help="(internal) run the Laya judge daemon in the current Python")
    sub.add_parser("where", help="definition sites of a symbol").add_argument("symbol")
    sub.add_parser("card", help="compiled card of a file").add_argument("path")
    sub.add_parser("core", help="print the core card")
    sub.add_parser("hook", help="(internal) Claude Code hook handler").add_argument("event")
    sub.add_parser("mcp", help="(internal) run the MCP server on stdio")
    args = ap.parse_args(argv)

    if args.cmd in ("compile", "update"):
        from .compile import compile_repo
        kw = {} if args.cmd == "update" else dict(model=args.model, card_model=args.card_model, jobs=args.jobs,
                                                  qa_per_kloc=args.qa_per_kloc, full=args.full)
        if args.cmd == "update":
            prev = Store(find_root(args.path) or args.path).manifest.get("models", {})
            kw = dict(model=prev.get("reasoning", "opus"), card_model=prev.get("cards"))
        m = compile_repo(args.path, **kw)
        print(json.dumps({k: m[k] for k in ("stats", "verification", "cost")}, indent=1))
    elif args.cmd == "status":
        s = _store()
        stale = [p for p in s.manifest.get("files", {}) if s.is_stale(p)]
        print(json.dumps({k: s.manifest.get(k) for k in ("repo", "compiled_at", "models", "stats", "verification", "cost")}, indent=1))
        print(f"stale files: {len(stale)}" + (f" ({', '.join(stale[:10])})" if stale else ""))
    elif args.cmd == "ask":
        from . import answer, judge
        store, q = _store(), " ".join(args.question)
        j = None if args.no_judge else judge.get(timeout=120)
        if args.pack:
            text, _ = retrieve.link_pack(store, q, budget=8000, n_seeds=8, n_links=24, body_links=6, max_lines=80, n_qa=2,
                                         judge=j, pool=20, judge_code_lines=0,
                                         judge_rel=0.25 if j is not None and getattr(j, "relative", True) else 0.0)
            print(text or "(no compiled knowledge matched)")
            print(f"\n[micode] {len(text) // 4:,} tokens, judge: {getattr(j, '__module__', None) and ('jev' if 'jev' in j.__module__ else 'laya') or 'none'}",
                  file=sys.stderr)
        else:
            r = answer.answer(store, q, reader=args.model, mode="link", judge=j)
            print(r["answer"] or r.get("error", "(no answer)"))
            print(f"\n[micode] pack {r['pack_tokens']:,} tokens -> {r['tokens']:,} total, ${r['cost']:.4f}, {r['seconds']}s",
                  file=sys.stderr)
    elif args.cmd == "setup-judge":
        from .setup_judge import setup
        setup(gpu=args.gpu)
    elif args.cmd == "judge-server":
        from .judge_server import main as serve_main
        serve_main()
    elif args.cmd == "where":
        print("\n".join(retrieve.where(_store(), args.symbol)) or "(not found)")
    elif args.cmd == "card":
        print(retrieve.card_for(_store(), args.path) or "(no card)")
    elif args.cmd == "core":
        print(_store().core)
    elif args.cmd == "hook":
        from .hooks import main as hook_main
        hook_main(args.event)
    elif args.cmd == "mcp":
        from .mcp import serve
        serve()


if __name__ == "__main__":
    main()
