"""A minimal stdio MCP server (JSON-RPC 2.0, newline-delimited) exposing the compiled index as tools."""
from __future__ import annotations

import json
import os
import sys

from . import retrieve
from .store import Store, find_root

TOOLS = [
    {"name": "ask", "description": (
        "Ask the repository's compiled understanding (.micode) a question in plain language. Returns precompiled answers, "
        "the relevant files with exact line spans, and matching flows — usually enough to answer or to read only a few lines. "
        "Use this BEFORE Grep/Glob/Read exploration."),
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "budget": {"type": "integer", "description": "max tokens to return (default 2500)"}}, "required": ["question"]}},
    {"name": "where", "description": "Exact definition site(s) of a symbol (function, class, method, type), with line spans and a one-line compiled note.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "card", "description": "The compiled card of one file: purpose, summary, every symbol with its exact line span, facts and gotchas. Far cheaper than reading the file.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "repo-relative path"}}, "required": ["path"]}},
    {"name": "module", "description": "The compiled card of a module/directory: responsibilities, key files, interfaces, how-to recipes, gotchas.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "deps", "description": "In-repo import graph for a file: what it imports and what imports it (blast radius of a change).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]


def _store(cache={}):  # noqa: B006 - deliberate process-level cache
    root = find_root(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not root:
        return None
    if root not in cache or cache[root].manifest.get("compiled_at") != Store(root).manifest.get("compiled_at"):
        cache[root] = Store(root)
    return cache[root]


def call(name: str, args: dict) -> str:
    s = _store()
    if s is None:
        return "No compiled understanding found. Run /mic:compile (or `mic compile .`) at the repository root first."
    if name == "ask":
        text, _ = retrieve.pack(s, args.get("question", ""), budget=int(args.get("budget") or 2500), excerpt_budget=1500)
        return text or "No compiled knowledge matched. Fall back to targeted Grep."
    if name == "where":
        hits = retrieve.where(s, args.get("symbol", ""))
        return "\n".join(hits) or "Symbol not in the compiled table (may be dynamic, generated, or new)."
    if name == "card":
        path = args.get("path", "").lstrip("./")
        return retrieve.card_for(s, path) or f"No card for {path}."
    if name == "module":
        m = s.modules.get(args.get("path", "").rstrip("/") or ".")
        if not m:
            return "Unknown module. Modules: " + ", ".join(sorted(s.modules))
        return json.dumps({k: m.get(k) for k in ("module", "purpose", "overview", "key_files", "interfaces", "how_to",
                                                  "gotchas", "deps_out", "deps_in")}, indent=1, ensure_ascii=False)
    if name == "deps":
        path = args.get("path", "").lstrip("./")
        g = s.graph
        users = sorted(k for k, v in g.items() if path in v)
        return f"{path}\n imports: {', '.join(g.get(path, [])) or 'none'}\n imported by: {', '.join(users) or 'none'}"
    return f"unknown tool {name}"


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue  # notification
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "mic", "version": "0.1.0"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params", {})
            try:
                res = {"content": [{"type": "text", "text": call(p.get("name"), p.get("arguments") or {})}]}
            except Exception as e:  # noqa: BLE001
                res = {"content": [{"type": "text", "text": f"micode error: {e}"}], "isError": True}
        elif method == "ping":
            res = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
