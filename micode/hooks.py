"""Claude Code hook handlers. Each reads the hook JSON on stdin and prints hook JSON on stdout.

They must never break a session: any failure degrades to "no output".
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import retrieve
from .store import DIR, Store, find_root, read_json

DEFAULTS = {
    "prompt_budget": 2200,      # compiled knowledge injected per user prompt (an extra turn costs ~12-15k)
    "excerpt_budget": 1800,     # live source excerpts of the best spans, so the agent need not re-open files
    "pack_mode": "link",        # "link": minimal linked paragraphs; "pointers": span map only; "full": cards + excerpts
    "link_budget": 3500,
    "hook_judge": False,        # use the local Laya judge in the prompt hook too (adds its latency to every prompt)
    "pointer_qa": 2,
    "pointer_spans": 14,
    "pointer_excerpt_lines": 0,
    "core_tokens": 1000,        # compact core card at session start (0 = full card)
    "prompt_min_score": 4.0,    # below this, the prompt is not about the code (e.g. "commit this")
    "read_card_min_lines": 150,  # attach the compiled card when partially reading files at least this long
    "read_guard_lines": 500,    # block the first blind full read of files longer than this
    "grep_defs": True,
}


DIRECTIVE = ("micode: compiled from this exact commit; spans are exact. If the answers above cover the request, answer "
             "now. Otherwise Read the spans you need ALL IN ONE parallel batch (offset/limit), then answer; search only "
             "for what the map does not cover.")

def config(root: str) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(read_json(os.path.join(root, DIR, "config.json"), {}) or {})
    try:
        cfg.update(json.loads(os.environ.get("MICODE_CONFIG", "{}")))
    except ValueError:
        pass
    return cfg


def _state_path(root: str, session: str) -> str:
    safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_")[:80] or "default"
    return os.path.join(root, DIR, ".cache", "sessions", safe + ".json")


def load_state(root: str, session: str) -> dict:
    return read_json(_state_path(root, session), {}) or {"units": [], "cards": [], "guarded": []}


def save_state(root: str, session: str, st: dict) -> None:
    path = _state_path(root, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(path + ".tmp", path)


def emit(event: str, context: str = "", decision: str | None = None, reason: str = "") -> None:
    out: dict = {"hookSpecificOutput": {"hookEventName": event}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    if decision:
        out["hookSpecificOutput"]["permissionDecision"] = decision
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))


def rel(root: str, path: str, cwd: str) -> str:
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    return os.path.relpath(os.path.realpath(full), os.path.realpath(root)).replace(os.sep, "/")


# ---------------------------------------------------------------- handlers
def session_start(inp: dict) -> None:
    root = find_root(inp.get("cwd") or os.getcwd())
    if not root or inp.get("source") == "resume":
        return  # a resumed transcript already carries the core card; "compact" and "clear" get it again
    s = Store(root)
    cfg = config(root)
    man = s.manifest
    st = man.get("stats", {})
    stale = [p for p in man.get("files", {}) if s.is_stale(p)]
    stale_note = (f"\n{len(stale)} file(s) changed since compile (e.g. {', '.join(stale[:5])}); their cards are marked ⚠stale "
                  "— verify those in source, and suggest `/micode:update` when convenient." if stale else "")
    ctx = f"""<micode>
This repository has a compiled understanding in .micode/ (compiled {man.get('compiled_at', '?')}; {st.get('files')} files,
{st.get('lines', 0):,} lines, {st.get('qa')} precompiled Q/A, every citation verified against the source).
How to use it — it replaces most exploration:
- Knowledge relevant to each prompt is injected automatically as <micode-context>: precompiled answers written by a strong
  model from the source, plus exact `path:Lstart-end` spans. Every citation was verified against this commit.
- When that context answers the question, answer directly from it and cite its spans. Do not re-open files just to
  double-check it; that costs a full turn and the compile already did it.
- When something is missing, ask the compiled index before exploring: the micode MCP tools `ask` (plain-language question),
  `where` (symbol definitions), `card` (a file's full map with spans), `module`, `deps`. One call costs a few hundred tokens;
  exploring costs thousands. Then Read ONLY the cited spans (offset/limit); use broad Grep/Glob only as a last resort.
- Reading a large file whole is intercepted once and answered with its symbol map.
- Before EDITING code, read the exact lines you change.{stale_note}

CORE CARD{' (compact; full card: micode `module`/`ask` tools)' if cfg['core_tokens'] else ''}
{retrieve.compact_core(s.core, cfg['core_tokens']) if cfg['core_tokens'] else s.core.strip()}
</micode>"""
    emit("SessionStart", ctx)


def user_prompt(inp: dict) -> None:
    root = find_root(inp.get("cwd") or os.getcwd())
    prompt = inp.get("prompt", "")
    if not root or not prompt or prompt.lstrip().startswith("/"):
        return
    cfg = config(root)
    s = Store(root)
    session = inp.get("session_id", "default")
    st = load_state(root, session)
    if cfg["pack_mode"] == "link":
        judge = None
        if cfg["hook_judge"]:
            from . import judge as judge_mod
            judge = judge_mod.get(timeout=15)
        text, info = retrieve.link_pack(s, prompt, budget=cfg["link_budget"], n_seeds=5, n_links=14, body_links=3,
                                        max_lines=60, n_qa=1, min_score=cfg["prompt_min_score"],
                                        exclude=set(st["units"]), judge=judge, pool=20,
                                        judge_rel=0.25 if judge is not None and getattr(judge, "relative", True) else 0.0,
                                        judge_code_lines=0)
    elif cfg["pack_mode"] == "pointers":
        text, info = retrieve.pointer_pack(s, prompt, n_qa=cfg["pointer_qa"], n_ptr=cfg["pointer_spans"],
                                           min_score=cfg["prompt_min_score"], exclude=set(st["units"]),
                                           excerpt_lines=cfg["pointer_excerpt_lines"])
    else:
        text, info = retrieve.pack(s, prompt, budget=cfg["prompt_budget"], min_score=cfg["prompt_min_score"],
                                   exclude=set(st["units"]), excerpt_budget=cfg["excerpt_budget"])
    if not text:
        return
    st["units"] += info["units"]
    save_state(root, session, st)
    emit("UserPromptSubmit", f"<micode-context query-relevant compiled knowledge>\n{text}\n\n{DIRECTIVE}\n</micode-context>")


def pre_tool(inp: dict) -> None:
    root = find_root(inp.get("cwd") or os.getcwd())
    if not root:
        return
    tool = inp.get("tool_name", "")
    ti = inp.get("tool_input", {}) or {}
    cwd = inp.get("cwd") or os.getcwd()
    cfg = config(root)
    s = Store(root)
    session = inp.get("session_id", "default")
    if tool == "Read" and ti.get("file_path"):
        path = rel(root, ti["file_path"], cwd)
        meta = s.manifest.get("files", {}).get(path)
        if not meta or path.startswith(DIR + "/"):
            return
        st = load_state(root, session)
        whole = not ti.get("offset") and not ti.get("limit")
        lines = meta.get("lines", 0)
        if whole and lines > cfg["read_guard_lines"] and path not in st["guarded"] and not s.is_stale(path):
            st["guarded"].append(path)
            st["cards"].append(path)
            save_state(root, session, st)
            card = retrieve.card_for(s, path)
            emit("PreToolUse", decision="deny", reason=(
                f"micode read-guard: {path} is {lines} lines (~{meta.get('bytes', 0) // 4:,} tokens). Its compiled map is "
                f"below; Read again with offset/limit for just the spans you need. (Repeating the same whole-file Read "
                f"will be allowed.)\n\n{card}"))
            return
        if not whole and lines >= cfg["read_card_min_lines"] and path not in st["cards"]:
            st["cards"].append(path)
            save_state(root, session, st)
            emit("PreToolUse", context=f"<micode-card>\n{retrieve.card_for(s, path, max_syms=40)}\n</micode-card>")
        return
    if tool == "Grep" and cfg["grep_defs"]:
        pat = ti.get("pattern", "")
        import re
        names = [n for n in re.findall(r"[A-Za-z_][\w.]{2,}", pat) if n.lower() not in ("def", "class", "function", "func", "const")]
        hits = []
        for n in names[:3]:
            hits += retrieve.where(s, n, limit=4)
        if hits:
            emit("PreToolUse", context="<micode-defs from compiled symbol table>\n" + "\n".join(dict.fromkeys(hits)) + "\n</micode-defs>")


HANDLERS = {"session-start": session_start, "prompt": user_prompt, "pre-tool": pre_tool}


def main(event: str) -> None:
    if os.environ.get("MICODE_CHILD") or os.environ.get("MICODE_DISABLE"):
        return
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        inp = {}
    t0 = time.time()
    try:
        HANDLERS[event](inp)
    except Exception as e:  # noqa: BLE001 - a hook must never break the session
        if os.environ.get("MICODE_DEBUG"):
            raise
        print(f"micode hook {event} skipped: {e}", file=sys.stderr)
    if os.environ.get("MICODE_DEBUG"):
        print(f"micode hook {event} took {1000 * (time.time() - t0):.0f} ms", file=sys.stderr)
