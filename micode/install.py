"""`mic install [claude|codex|opencode|zcode|cursor|vscode|claude-desktop|windsurf|gemini|agents-md|mcp-json|all]`.

Every agent gets the MCP server (tools: ask, where, card, module, deps). Agents with Claude-style lifecycle hooks
(Claude Code, Codex, ZCode) also get the hooks, so the minimal linked paragraphs are injected on every prompt
automatically. Config files are merged, never overwritten, and each change is printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
MARKETPLACE = "Spearmintai/mintocode"


def mic_command() -> list[str]:
    """How other programs should launch this mic: the installed console script, else this checkout's bin/mic."""
    exe = shutil.which("mic")
    if exe:
        return [exe]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, os.path.join(root, "bin", "mic")]


def _say(msg: str) -> None:
    print(f"[mic] {msg}", file=sys.stderr)


def _merge_json(path: str, update) -> None:
    data = {}
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            _say(f"{path} is not plain JSON; add the mic entry by hand (see README)")
            return
    update(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".mic.tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, path)
    _say(f"updated {path}")


def hooks_block(cmd: list[str]) -> dict:
    base = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    return {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{base} hook session-start", "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{base} hook prompt", "timeout": 60}]}],
        "PreToolUse": [{"matcher": "Read|Grep", "hooks": [{"type": "command", "command": f"{base} hook pre-tool", "timeout": 10}]}],
    }


def _add_hooks(data: dict, cmd: list[str]) -> None:
    hooks = data.setdefault("hooks", {})
    for event, entries in hooks_block(cmd).items():
        existing = hooks.setdefault(event, [])
        # drop older mic entries, keep everything else
        existing[:] = [e for e in existing if not any(" hook " in h.get("command", "") and "mic" in h.get("command", "")
                                                      for h in e.get("hooks", []))]
        existing.extend(entries)


# ---------------------------------------------------------------- targets
def claude() -> None:
    if not shutil.which("claude"):
        _say("claude CLI not found; in Claude Code run:  /plugin marketplace add " + MARKETPLACE +
             "  then  /plugin install mic@mintocode")
        return
    subprocess.run(["claude", "plugin", "marketplace", "add", MARKETPLACE], check=False)
    subprocess.run(["claude", "plugin", "install", "mic@mintocode"], check=False)
    _say("Claude Code: plugin installed (hooks, MCP tools, /mic:* commands, skill)")


def codex() -> None:
    cmd = mic_command()
    home = os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))
    if shutil.which("codex"):
        subprocess.run(["codex", "mcp", "remove", "mic"], capture_output=True, check=False)
        subprocess.run(["codex", "mcp", "add", "mic", "--", *cmd, "mcp"], check=False)
    else:
        _say("codex CLI not found; skipping its MCP registration")
    _merge_json(os.path.join(home, "hooks.json"), lambda d: _add_hooks(d, cmd))
    _say("Codex: MCP server 'mic' + SessionStart/UserPromptSubmit hooks (Codex asks you to trust new hooks once)")


def opencode() -> None:
    cmd = mic_command()
    path = os.path.join(HOME, ".config", "opencode", "opencode.json")

    def upd(d):
        d.setdefault("$schema", "https://opencode.ai/config.json")
        d.setdefault("mcp", {})["mic"] = {"type": "local", "command": [*cmd, "mcp"], "enabled": True}
    _merge_json(path, upd)
    cmd_dir = os.path.join(HOME, ".config", "opencode", "command")
    os.makedirs(cmd_dir, exist_ok=True)
    with open(os.path.join(cmd_dir, "mic.md"), "w", encoding="utf-8") as f:
        f.write("---\ndescription: Answer from mic's minimal linked paragraphs of this repository\n---\n"
                "Call the `mic_ask` MCP tool with: $ARGUMENTS\nAnswer from its result, citing file:line spans. "
                "Read source only for the exact cited spans, and only if the result is insufficient.\n")
    _say("OpenCode: MCP server 'mic' + /mic command (per-prompt injection is not available in OpenCode's plugin API; "
         "run `mic install agents-md` in each repo so the agent uses the tools first)")


def zcode() -> None:
    cmd = mic_command()
    path = os.path.join(HOME, ".zcode", "cli", "config.json")

    def upd(d):
        d.setdefault("mcp", {}).setdefault("servers", {})["mic"] = {"command": cmd[0], "args": [*cmd[1:], "mcp"], "env": {}}
    _merge_json(path, upd)
    _say("ZCode: MCP server 'mic'. ZCode also loads Claude-format plugins: add the marketplace " + MARKETPLACE +
         " in ZCode for the hooks")


AGENTS_BLOCK = """<!-- mic:begin -->
## Compiled understanding (mic)

This repository is compiled into `.micode/` by [mic](https://github.com/Spearmintai/mintocode). Before exploring
with search or whole-file reads:

- ask the `mic` MCP tools first: `ask` (plain-language question → the minimal linked paragraphs with exact
  `path:Lstart-end` spans), `where` (definition of a symbol), `card` (a file's map), `module`, `deps`;
  without MCP: `mic pack "<question>"` in the shell;
- then read only the cited spans; every span was checked against the source at compile time;
- after changing code, `mic update` recompiles only the changed files.
<!-- mic:end -->
"""


def agents_md(root: str = ".") -> None:
    path = os.path.join(root, "AGENTS.md")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    text = re.sub(r"<!-- mic:begin -->.*?<!-- mic:end -->\n?", "", text, flags=re.S).rstrip()
    with open(path, "w", encoding="utf-8") as f:
        f.write((text + "\n\n" if text else "") + AGENTS_BLOCK)
    _say(f"updated {path} (read by Codex, OpenCode, ZCode and others)")


def _mcp_entry(cmd: list[str]) -> dict:
    return {"command": cmd[0], "args": [*cmd[1:], "mcp"]}


def _mcp_servers_file(path: str, label: str, key: str = "mcpServers", extra: dict | None = None) -> None:
    cmd = mic_command()

    def upd(d):
        d.setdefault(key, {})["mic"] = dict(_mcp_entry(cmd), **(extra or {}))
    _merge_json(path, upd)
    _say(f"{label}: MCP server 'mic' (tools: ask, answer, where, card, module, deps, core, status, repos, update)")


def cursor() -> None:
    _mcp_servers_file(os.path.join(HOME, ".cursor", "mcp.json"), "Cursor")


def windsurf() -> None:
    _mcp_servers_file(os.path.join(HOME, ".codeium", "windsurf", "mcp_config.json"), "Windsurf")


def gemini() -> None:
    _mcp_servers_file(os.path.join(HOME, ".gemini", "settings.json"), "Gemini CLI")


def claude_desktop() -> None:
    if sys.platform == "darwin":
        path = os.path.join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json")
    elif os.name == "nt":
        path = os.path.join(os.environ.get("APPDATA", HOME), "Claude", "claude_desktop_config.json")
    else:
        path = os.path.join(HOME, ".config", "Claude", "claude_desktop_config.json")
    _mcp_servers_file(path, "Claude Desktop (restart it to load the server)")


def vscode(root: str = ".") -> None:
    """VS Code reads workspace MCP servers from .vscode/mcp.json."""
    _mcp_servers_file(os.path.join(root, ".vscode", "mcp.json"), "VS Code (this workspace)", key="servers",
                      extra={"type": "stdio"})


def mcp_json() -> None:
    """Print a config block for any other MCP client."""
    print(json.dumps({"mcpServers": {"mic": _mcp_entry(mic_command())}}, indent=2))


TARGETS = {"claude": claude, "codex": codex, "opencode": opencode, "zcode": zcode, "cursor": cursor, "vscode": vscode,
           "claude-desktop": claude_desktop, "windsurf": windsurf, "gemini": gemini, "agents-md": agents_md,
           "mcp-json": mcp_json}


def install(target: str) -> None:
    if target != "all":
        TARGETS[target]()
        return
    probes = (("claude", "claude", ".claude"), ("codex", "codex", ".codex"), ("opencode", "opencode", ".config/opencode"),
              ("zcode", "zcode", ".zcode"), ("cursor", "cursor", ".cursor"), ("windsurf", "windsurf", ".codeium/windsurf"),
              ("gemini", "gemini", ".gemini"))
    found = [t for t, exe, d in probes if shutil.which(exe) or os.path.isdir(os.path.join(HOME, d))]
    if not found:
        _say("no coding agent detected; mic still works on its own: `mic compile .` then `mic ask` / `mic pack`")
    for t in found:
        TARGETS[t]()
