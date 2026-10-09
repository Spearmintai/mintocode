"""mic as an MCP server (stdio, JSON-RPC 2.0, newline-delimited). Stdlib only, works with any MCP client.

One server can serve every compiled repository on the machine. Each tool takes an optional `repo` (a path); otherwise
the repository is resolved from MIC_REPO, the client's project directory, the workspace roots the client reports
(MCP `roots`), or the server's working directory.

Tools      ask, answer, where, card, module, deps, core, status, repos, update
Resources  mic://<repo>/core  (the core card of each compiled repository)
Prompts    ask_codebase
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

from . import retrieve
from .store import Store, find_root, known_repos

VERSION = "0.2.0"
REPO = {"type": "string", "description": "path of the repository (optional; defaults to the current project)"}


def _tool(name, description, props=None, required=None):
    props = dict(props or {}, repo=REPO)
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required or []}}


TOOLS = [
    _tool("ask", "Return the minimal linked paragraphs of the repository for a question: the best-matching functions as "
          "live source with exact line spans, plus the definitions they use, their callers and tests. No model call, "
          "~30 ms. Use this BEFORE searching or reading files.",
          {"question": {"type": "string"},
           "budget": {"type": "integer", "description": "max tokens to return (default 3500)"}}, ["question"]),
    _tool("answer", "Answer a question about the repository in one model call from its link pack (uses the configured "
          "model backend and costs one call). Prefer `ask` when you will read and reason yourself.",
          {"question": {"type": "string"}, "model": {"type": "string", "description": "default: sonnet"}}, ["question"]),
    _tool("where", "Exact definition site(s) of a symbol (function, class, method, type), with line spans and a one-line note.",
          {"symbol": {"type": "string"}}, ["symbol"]),
    _tool("card", "The compiled card of one file: purpose, summary, every symbol with its exact line span, facts and "
          "gotchas. Far cheaper than reading the file.", {"path": {"type": "string", "description": "repo-relative path"}}, ["path"]),
    _tool("module", "The compiled card of a directory: responsibilities, key files, interfaces, how-to recipes, gotchas.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("deps", "In-repo import graph for a file: what it imports and what imports it (the blast radius of a change).",
          {"path": {"type": "string"}}, ["path"]),
    _tool("core", "The repository's core card: what it is, architecture, module map, entry points, commands, conventions."),
    _tool("status", "Compile stats, citation verification, cost, and files changed since the last compile."),
    _tool("repos", "List every repository compiled by mic on this machine, with size and compile date."),
    _tool("update", "Recompile only the files that changed since the last compile (uses the configured model backend; "
          "takes seconds to minutes and costs model calls)."),
]

PROMPTS = [{"name": "ask_codebase", "description": "Answer a question about the current codebase from mic's link pack",
            "arguments": [{"name": "question", "description": "what you want to know", "required": True}]}]


class Server:
    def __init__(self, rin=sys.stdin, rout=sys.stdout):
        self.rin, self.rout = rin, rout
        self.client_roots = None  # None: unknown, []: none reported
        self.can_roots = False
        self.pending: list = []
        self.next_id = 1
        self.cache: dict = {}

    # ------------------------------------------------------------ transport
    def send(self, obj) -> None:
        self.rout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.rout.flush()

    def read(self):
        if self.pending:
            return self.pending.pop(0)
        for line in self.rin:
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    def request_client(self, method: str, params=None, limit: int = 50):
        """Send a request to the client and wait for its response, queueing anything else that arrives."""
        rid = f"mic-{self.next_id}"
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        for _ in range(limit):
            line = self.rin.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid and "method" not in msg:
                return msg.get("result")
            self.pending.append(msg)
        return None

    # ------------------------------------------------------------ repository resolution
    def roots(self) -> list[str]:
        if self.client_roots is None:
            self.client_roots = []
            if self.can_roots:
                res = self.request_client("roots/list") or {}
                for r in res.get("roots", []):
                    uri = r.get("uri", "")
                    if uri.startswith("file://"):
                        self.client_roots.append(urllib.parse.unquote(urllib.parse.urlparse(uri).path))
        return self.client_roots

    def store(self, repo: str | None = None) -> Store | None:
        candidates = [repo, os.environ.get("MIC_REPO"), os.environ.get("CLAUDE_PROJECT_DIR")]
        if not repo:
            candidates += self.roots()
        candidates.append(os.getcwd())
        for c in candidates:
            root = find_root(os.path.expanduser(c)) if c else None
            if root:
                fresh = Store(root)
                cached = self.cache.get(root)
                if not cached or cached.manifest.get("compiled_at") != fresh.manifest.get("compiled_at"):
                    self.cache[root] = fresh
                return self.cache[root]
        return None

    # ------------------------------------------------------------ tools
    def call(self, name: str, args: dict) -> str:
        if name == "repos":
            rows = [f"{r['root']}  ({r.get('files', '?')} files, {r.get('lines', 0):,} lines, compiled {r.get('compiled_at', '?')})"
                    for r in known_repos()]
            return "\n".join(rows) or "No compiled repositories yet. Run `mic compile .` in a repository."
        s = self.store(args.get("repo"))
        if s is None:
            hint = f" Compiled repositories: {', '.join(r['root'] for r in known_repos())}." if known_repos() else ""
            return ("No compiled repository found here. Pass `repo`, or run `mic compile .` at the repository root." + hint)
        if name == "ask":
            text, _ = retrieve.link_pack(s, args.get("question", ""), budget=int(args.get("budget") or 3500), n_seeds=6,
                                         n_links=16, body_links=3, max_lines=60, n_qa=1)
            return text or "Nothing in the compiled repository matched. Fall back to a targeted search."
        if name == "answer":
            from . import answer
            r = answer.answer(s, args.get("question", ""), reader=args.get("model") or "sonnet", mode="link")
            return (r.get("answer") or r.get("error", "no answer")) + \
                f"\n\n[mic] pack {r.get('pack_tokens', 0):,} tokens, {r.get('tokens', 0):,} total, {r.get('seconds')}s"
        if name == "where":
            return "\n".join(retrieve.where(s, args.get("symbol", ""))) or "Not in the compiled symbol table (dynamic, generated, or new)."
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
            users = sorted(k for k, v in s.graph.items() if path in v)
            return f"{path}\n imports: {', '.join(s.graph.get(path, [])) or 'none'}\n imported by: {', '.join(users) or 'none'}"
        if name == "core":
            return s.core or "No core card."
        if name == "status":
            stale = [p for p in s.manifest.get("files", {}) if s.is_stale(p)]
            info = {k: s.manifest.get(k) for k in ("repo", "compiled_at", "stats", "verification", "cost")}
            return json.dumps(info, indent=1) + f"\nchanged since compile: {len(stale)}" + (f" ({', '.join(stale[:10])})" if stale else "")
        if name == "update":
            from .compile import compile_repo
            prev = s.manifest.get("models", {})
            m = compile_repo(s.root, model=prev.get("reasoning", "opus"), card_model=prev.get("cards"))
            return json.dumps({k: m[k] for k in ("stats", "verification", "cost")}, indent=1)
        return f"unknown tool {name}"

    # ------------------------------------------------------------ resources and prompts
    def resources(self) -> list:
        out, seen = [], set()
        here = self.store()
        repos = ([{"root": here.root}] if here else []) + known_repos()
        for r in repos:
            if r["root"] in seen:
                continue
            seen.add(r["root"])
            name = os.path.basename(r["root"])
            out.append({"uri": f"mic://{urllib.parse.quote(r['root'], safe='')}/core", "name": f"{name} core card",
                        "description": f"Compiled overview of {r['root']}", "mimeType": "text/markdown"})
        return out

    def read_resource(self, uri: str) -> str:
        root = urllib.parse.unquote(uri[len("mic://"):].rsplit("/", 1)[0])
        s = self.store(root)
        return s.core if s else ""

    # ------------------------------------------------------------ loop
    def handle(self, msg) -> None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method is None or mid is None:
            return  # a notification or a stray response
        try:
            if method == "initialize":
                self.can_roots = "roots" in (params.get("capabilities") or {})
                res = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                       "serverInfo": {"name": "mic", "version": VERSION},
                       "instructions": "Compiled understanding of code repositories. Call `ask` with the question before "
                                       "searching or reading files; read only the spans it cites."}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    res = {"content": [{"type": "text", "text": self.call(params.get("name"), params.get("arguments") or {})}]}
                except Exception as e:  # noqa: BLE001 - report tool failures to the client instead of dying
                    res = {"content": [{"type": "text", "text": f"mic error: {e}"}], "isError": True}
            elif method == "resources/list":
                res = {"resources": self.resources()}
            elif method == "resources/read":
                uri = params.get("uri", "")
                res = {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": self.read_resource(uri)}]}
            elif method == "prompts/list":
                res = {"prompts": PROMPTS}
            elif method == "prompts/get":
                q = (params.get("arguments") or {}).get("question", "")
                res = {"description": PROMPTS[0]["description"], "messages": [{"role": "user", "content": {"type": "text", "text":
                       f"Use the mic `ask` tool with this question first, answer from the paragraphs it returns, and cite "
                       f"their file:line spans. Read source only for spans it does not cover.\n\nQuestion: {q}"}}]}
            elif method == "ping":
                res = {}
            else:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                return
            self.send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:  # noqa: BLE001
            self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})

    def serve(self) -> None:
        while True:
            msg = self.read()
            if msg is None:
                return
            self.handle(msg)


def serve() -> None:
    Server().serve()
