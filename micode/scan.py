"""Deterministic repository scan: which files matter, their hashes, sizes and languages.

Nothing here calls a model. Everything the compiler later says about *where* things
are (paths, line spans) comes from this layer and from symbols.py, so pointers in the
compiled artifact are exact rather than generated.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from dataclasses import dataclass, field

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".swift": "swift", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".lua": "lua", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql", ".proto": "proto", ".graphql": "graphql", ".gql": "graphql",
    ".vue": "vue", ".svelte": "svelte", ".dart": "dart", ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang", ".hs": "haskell", ".ml": "ocaml", ".r": "r", ".R": "r", ".jl": "julia",
    ".zig": "zig", ".nim": "nim", ".m": "objc", ".mm": "objc",
    ".md": "markdown", ".rst": "rst", ".txt": "text", ".adoc": "asciidoc",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".ini": "ini", ".cfg": "ini",
    ".tf": "terraform", ".gradle": "gradle", ".cmake": "cmake",
    ".html": "html", ".css": "css", ".scss": "css",
}
LANG_BY_NAME = {
    "Makefile": "make", "Dockerfile": "docker", "CMakeLists.txt": "cmake", "Rakefile": "ruby",
    "Gemfile": "ruby", "Justfile": "make", "justfile": "make", "BUILD": "bazel", "WORKSPACE": "bazel",
}
CODE_LANGS = {
    "python", "javascript", "typescript", "go", "rust", "java", "kotlin", "scala", "swift", "ruby",
    "php", "c", "cpp", "csharp", "lua", "shell", "sql", "proto", "graphql", "vue", "svelte", "dart",
    "elixir", "erlang", "haskell", "ocaml", "r", "julia", "zig", "nim", "objc", "terraform",
}

DEFAULT_IGNORE = [
    ".git/*", ".micode/*", "node_modules/*", "*/node_modules/*", "vendor/*", "third_party/*",
    "dist/*", "build/*", "out/*", "target/*", ".next/*", ".nuxt/*", "__pycache__/*", "*/__pycache__/*",
    ".venv/*", "venv/*", "env/*", ".tox/*", ".mypy_cache/*", ".pytest_cache/*", "coverage/*",
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "go.sum", "*.pb.go", "*_pb2.py", "*.snap", "*.svg",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.pdf", "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.zip", "*.gz", "*.tar", "*.bin", "*.pt", "*.safetensors", "*.onnx", "*.so", "*.dylib", "*.dll",
    "*.exe", "*.class", "*.jar", "*.pyc", "*.o", "*.a", "*.parquet", "*.npy", "*.npz", "*.pkl",
    "*.db", "*.sqlite", "*.mp4", "*.mp3", "*.wav", "LICENSE*", "COPYING*",
]
MAX_BYTES = 800_000


@dataclass
class FileInfo:
    path: str
    lang: str
    lines: int
    bytes: int
    sha: str
    is_code: bool
    is_test: bool = False
    symbols: list = field(default_factory=list)
    imports: list = field(default_factory=list)


def _lang(path: str) -> str:
    base = os.path.basename(path)
    if base in LANG_BY_NAME:
        return LANG_BY_NAME[base]
    return LANG_BY_EXT.get(os.path.splitext(base)[1], "")


def _is_test(path: str) -> bool:
    p = "/" + path.lower()
    base = os.path.basename(p)
    return ("/test/" in p or "/tests/" in p or "/__tests__/" in p or "/spec/" in p
            or base.startswith("test_") or base.endswith(("_test.py", "_test.go", ".test.ts", ".test.js",
                                                          ".spec.ts", ".spec.js", ".test.tsx", "_spec.rb")))


def load_ignore(root: str) -> list[str]:
    pats = list(DEFAULT_IGNORE)
    f = os.path.join(root, ".micodeignore")
    if os.path.exists(f):
        for line in open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line.rstrip("/") + ("/*" if line.endswith("/") else ""))
    return pats


def ignored(path: str, pats: list[str]) -> bool:
    base = os.path.basename(path)
    for p in pats:
        if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p):
            return True
        if p.endswith("/*") and (path + "/").startswith(p[:-1]):
            return True
    return False


def list_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__")]
            for n in names:
                files.append(os.path.relpath(os.path.join(d, n), root))
    return sorted(set(f.replace(os.sep, "/") for f in files))


def sha_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def scan(root: str) -> list[FileInfo]:
    pats = load_ignore(root)
    result = []
    for rel in list_files(root):
        if ignored(rel, pats):
            continue
        full = os.path.join(root, rel)
        try:
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            size = os.path.getsize(full)
            if size == 0 or size > MAX_BYTES:
                continue
            data = open(full, "rb").read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        lang = _lang(rel)
        if not lang:
            continue
        text = data.decode("utf-8", errors="replace")
        # Skip generated/minified content: very long average line length.
        nlines = text.count("\n") + (0 if text.endswith("\n") else 1)
        if nlines and len(text) / nlines > 400:
            continue
        result.append(FileInfo(path=rel, lang=lang, lines=nlines, bytes=size, sha=sha_of(data),
                               is_code=lang in CODE_LANGS, is_test=_is_test(rel)))
    return result


def read_text(root: str, rel: str) -> str:
    return open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
