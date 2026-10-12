"""Deterministic repository scan: which files matter, their hashes, sizes and languages.

Nothing here calls a model. Everything the compiler later says about *where* things
are (paths, line spans) comes from this layer and from symbols.py, so pointers in the
compiled artifact are exact rather than generated.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import sys
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


KNOWN_LANGS = set(LANG_BY_EXT.values()) | set(LANG_BY_NAME.values())
LANG_ALIASES = {"c++": "cpp", "cxx": "cpp", "cc": "cpp", "hpp": "cpp", "py": "python", "js": "javascript",
                "ts": "typescript", "rb": "ruby", "rs": "rust", "kt": "kotlin", "cs": "csharp", "c#": "csharp",
                "sh": "shell", "bash": "shell", "golang": "go", "md": "markdown", "yml": "yaml"}
USER_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "mic", "config.json")
REPO_CONFIG = ".mic.json"


def infer_language(ext: str) -> str:
    """Language for a bare extension: uppercase .C/.H are C++ (the GCC and OpenFOAM convention), otherwise the
    built-in table, then its lowercase form."""
    if ext in LANG_BY_EXT:
        return LANG_BY_EXT[ext]
    low = ext.lower()
    if low in (".c", ".h") and ext != low:
        return "cpp"
    return LANG_BY_EXT.get(low, "")


def _parse_mapping(mapping) -> dict:
    """Normalise a file-type map to {key: language}. Accepts {'.H': 'cpp'}, ['.H=cpp'], ['.H', '.C'] and '.H,.C'.

    Keys starting with '.' (or '*.') are extensions, matched case-sensitively so '.C' can mean C++ while '.c' stays C;
    any other key is an exact file name. Without a language, the language is inferred from the extension."""
    if isinstance(mapping, str):
        mapping = [mapping]
    if isinstance(mapping, dict):
        items = list(mapping.items())
    else:
        items = []
        for entry in mapping:
            for part in str(entry).split(","):
                part = part.strip()
                if part:
                    items.append(tuple(part.split("=", 1)) if "=" in part else (part, ""))
    out = {}
    for key, lang in items:
        key, lang = str(key).strip(), str(lang).strip().lower()
        if key.startswith("*."):
            key = key[1:]
        if not key:
            continue
        if not lang:
            lang = infer_language(key) if key.startswith(".") else ""
            if not lang:
                print(f"[mic] note: can't tell which language {key} is; write it as {key}=LANG (e.g. {key}=cpp)",
                      file=sys.stderr)
                continue
        lang = LANG_ALIASES.get(lang, lang)
        if lang not in KNOWN_LANGS:
            print(f"[mic] note: '{lang}' for {key} is not a language mic parses; files are indexed as plain text",
                  file=sys.stderr)
        out[key] = lang
    return out


def load_languages(root: str, extra=None) -> dict:
    """Custom extension and file-name -> language map. Later sources win:
    ~/.config/mic/config.json, <repo>/.mic.json, MIC_EXTENSIONS ('.H=cpp,.C=cpp'), then `extra` (e.g. --ext)."""
    out: dict = {}
    for path in (USER_CONFIG, os.path.join(root, REPO_CONFIG)):
        try:
            with open(path, encoding="utf-8") as f:
                out.update(_parse_mapping(json.load(f).get("extensions", {})))
        except (OSError, ValueError, AttributeError):
            pass
    env = os.environ.get("MIC_EXTENSIONS", "")
    if env:
        out.update(_parse_mapping([x for x in env.split(",") if x.strip()]))
    if extra:
        out.update(_parse_mapping(extra))
    return out


def _lang(path: str, custom: dict | None = None) -> str:
    base = os.path.basename(path)
    if custom:
        if base in custom:
            return custom[base]
        ext = os.path.splitext(base)[1]
        if ext and ext in custom:
            return custom[ext]
    if base in LANG_BY_NAME:
        return LANG_BY_NAME[base]
    return LANG_BY_EXT.get(os.path.splitext(base)[1], "")


def _is_test(path: str) -> bool:
    p = "/" + path.lower()
    base = os.path.basename(p)
    return ("/test/" in p or "/tests/" in p or "/__tests__/" in p or "/spec/" in p
            or base.startswith("test_") or base.endswith(("_test.py", "_test.go", ".test.ts", ".test.js",
                                                          ".spec.ts", ".spec.js", ".test.tsx", "_spec.rb")))


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def project_config(root: str) -> dict:
    """The user config (~/.config/mic/config.json) overlaid by the repository's .mic.json."""
    user, repo = _read_json(USER_CONFIG), _read_json(os.path.join(root, REPO_CONFIG))
    out = dict(user)
    out.update(repo)
    out["ignore"] = list(user.get("ignore", [])) + list(repo.get("ignore", []))
    return out


class Rules:
    """gitignore-style patterns: '#' comments, 'dir/' for directories, '/x' anchored at the root, '**' across
    directories, '!pattern' to re-include. Later rules win."""

    def __init__(self, lines):
        self.rules = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            line = line[1:] if neg else line
            dir_only = line.endswith("/")
            line = line.strip("/") if dir_only else line
            anchored = line.startswith("/") or "/" in line.rstrip("/")
            line = line.lstrip("/")
            if line:
                self.rules.append((line, neg, dir_only, anchored))

    @staticmethod
    def _match(pat: str, path: str, dir_only: bool, anchored: bool) -> bool:
        parts = path.split("/")
        # every leading directory (and, unless dir-only, the file itself) is a candidate
        cands = ["/".join(parts[:k]) for k in range(1, len(parts) + (0 if dir_only else 1))]
        for c in cands:
            if anchored:
                if fnmatch.fnmatch(c, pat) or (pat.startswith("**/") and fnmatch.fnmatch(c, pat[3:])):
                    return True
            elif fnmatch.fnmatch(c.rsplit("/", 1)[-1], pat):
                return True
        return False

    def decide(self, path: str):
        """True: excluded, False: re-included, None: no rule matched."""
        verdict = None
        for pat, neg, dir_only, anchored in self.rules:
            if self._match(pat, path, dir_only, anchored):
                verdict = not neg
        return verdict


def load_ignore(root: str) -> list[str]:
    """Built-in patterns (vendored, generated and binary files); user rules live in Rules."""
    return list(DEFAULT_IGNORE)


def load_rules(root: str) -> tuple[Rules, Rules | None]:
    """(.micignore + .micodeignore + "ignore" in config, "include" scope or None)."""
    lines = []
    for name in (".micodeignore", ".micignore"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines += f.read().splitlines()
        except OSError:
            pass
    cfg = project_config(root)
    lines += [str(x) for x in cfg.get("ignore", [])]
    include = cfg.get("include")
    return Rules(lines), (Rules([str(x) for x in include]) if include else None)


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


def scan(root: str, languages: dict | None = None) -> list[FileInfo]:
    pats = load_ignore(root)
    rules, include = load_rules(root)
    custom = load_languages(root) if languages is None else languages
    result = []
    for rel in list_files(root):
        user = rules.decide(rel)
        if user is True or (user is None and ignored(rel, pats)):
            continue  # a '!pattern' in .micignore can re-include something the built-in list skips
        if include is not None and include.decide(rel) is not True:
            continue
        full = os.path.join(root, rel)
        try:
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            size = os.path.getsize(full)
            if size == 0 or size > MAX_BYTES:
                continue
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        lang = _lang(rel, custom)
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
    with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
        return fh.read()
