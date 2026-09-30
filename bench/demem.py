"""De-memorize a benchmark repository.

Popular open-source repos (flask, pytest, ...) are in every model's training data, so an agent can often
grep the right function name from memory. A developer's own large repository gets no such help, which is
exactly the case micode is for. This script makes a consistent, meaning-preserving rename of every
distinctive identifier the repository defines (classes, functions, methods, modules, the project name),
applies it to all files, file names and directories, and applies the SAME rename to the SWE-QA questions
and reference answers. The code stays as readable as before; it just is not recallable from memory.

usage: python3 bench/demem.py flask quill
"""
from __future__ import annotations

import builtins
import json
import keyword
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from micode import scan, symbols  # noqa: E402
from micode.llm import LLM  # noqa: E402

BENCH = os.environ.get("MICODE_BENCH", "/home/sq/micode-bench")
QDIR = os.path.join(BENCH, "SWE-QA-Bench", "Benchmark")
RESERVED = set(dir(builtins)) | set(keyword.kwlist) | set(sys.stdlib_module_names) | {
    "self", "cls", "args", "kwargs", "main", "setup", "test", "tests", "conftest", "init", "name", "value", "data",
    "path", "file", "key", "items", "keys", "values", "update", "copy", "get", "set", "run", "add", "pop", "index",
    "close", "open", "read", "write", "load", "dump", "loads", "dumps", "call", "wrapper", "decorator", "check",
    "process", "result", "response", "request", "session", "config", "app", "json", "utils", "helpers", "types",
    "typing", "globals", "logging", "views", "debug", "testing", "version", "docs", "examples", "src", "lib"}

PROMPT = """You are renaming identifiers of the open-source project `{project}` so that it can no longer be recognised
by name, while every name keeps its meaning. The project itself is renamed to `{new_project}`.

For each identifier below, propose a replacement that:
- means the same thing, using different wording (synonyms, rephrasing), so a reader still understands the code;
- follows the same convention (CamelCase stays CamelCase, snake_case stays snake_case, leading underscores kept,
  test_ prefix kept, UPPER_CASE kept);
- does not reuse the original's distinctive words, and does not use the name `{project}` or its well-known terms;
- is unique across the whole list and is a valid Python identifier.

Return ONLY a JSON object mapping each original to its replacement.

IDENTIFIERS:
{names}"""


def distinctive(n: str) -> bool:
    core = n.strip("_")
    if not core or n.startswith("__") or core in RESERVED or core.lower() in RESERVED or len(core) < 4:
        return False
    parts = [p for p in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", core) if p]
    return len(parts) >= 2 or core[0].isupper() or len(core) >= 7


def collect(root: str, project: str) -> list[str]:
    files = scan.scan(root)
    names = set()
    for f in files:
        if f.lang != "python":
            continue
        syms, _ = symbols.extract(scan.read_text(root, f.path), "python")
        for s in syms:
            if s["kind"] in ("variable", "attribute") and not s["name"].split(".")[-1].isupper():
                continue
            names.add(s["name"].split(".")[-1])
        for part in os.path.splitext(f.path)[0].split("/"):
            names.add(part)
    names = {n for n in names if distinctive(n) and re.fullmatch(r"[A-Za-z_]\w*", n)}
    names.add(project)
    return sorted(names)


def build_map(project: str, new_project: str, names: list[str], cache: str) -> dict:
    llm = LLM(cache)
    mapping: dict = {project: new_project}
    rest = [n for n in names if n != project]
    for k in range(0, len(rest), 180):
        chunk = rest[k:k + 180]
        out = llm.complete_json(PROMPT.format(project=project, new_project=new_project, names="\n".join(chunk)), "sonnet")
        for a, b in out.items():
            if a in chunk and isinstance(b, str) and re.fullmatch(r"[A-Za-z_]\w*", b) and b != a:
                mapping[a] = b
    # Uniqueness: a replacement must not collide with another original or another replacement.
    seen, final = set(names), {}
    for a in sorted(mapping, key=len, reverse=True):
        b = mapping[a]
        while b in seen:
            b += "_x"
        seen.add(b)
        final[a] = b
    return final


def compile_rx(mapping: dict):
    keys = sorted(mapping, key=len, reverse=True)
    rx = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, keys)) + r")(?![A-Za-z0-9_])")
    return lambda text: rx.sub(lambda m: mapping[m.group(1)], text)


def main() -> None:
    project, new_project = sys.argv[1], sys.argv[2]
    src = os.path.join(BENCH, "repos", project)
    dst = os.path.join(BENCH, "repos", new_project)
    cache = os.path.join(BENCH, "demem_cache")
    names = collect(src, project)
    print(f"{len(names)} distinctive identifiers", file=sys.stderr)
    mapping = build_map(project, new_project, names, cache)
    json.dump(mapping, open(os.path.join(BENCH, f"demem_{project}.json"), "w"), indent=1)
    sub = compile_rx(mapping)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    tracked = subprocess.run(["git", "-C", src, "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
    for rel in filter(None, tracked):
        full = os.path.join(src, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        new_rel = "/".join(sub(p) if "." not in p else sub(os.path.splitext(p)[0]) + os.path.splitext(p)[1]
                           for p in rel.split("/"))
        out = os.path.join(dst, new_rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        data = open(full, "rb").read()
        try:
            text = data.decode("utf-8")
            open(out, "w", encoding="utf-8").write(sub(text))
        except UnicodeDecodeError:
            open(out, "wb").write(data)
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dst, check=True)
    subprocess.run(["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-qm", "de-memorized"],
                   cwd=dst, check=True)
    with open(os.path.join(QDIR, f"{new_project}.jsonl"), "w") as f:
        for line in open(os.path.join(QDIR, f"{project}.jsonl")):
            q = json.loads(line)
            f.write(json.dumps({"question": sub(q["question"]), "answer": sub(q["answer"])}, ensure_ascii=False) + "\n")
    print(f"wrote {dst} and {new_project}.jsonl ({len(mapping)} renames)", file=sys.stderr)


if __name__ == "__main__":
    main()
