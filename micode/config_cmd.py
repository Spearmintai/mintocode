"""`mic config`: set file types, ignores and scope without editing JSON by hand.

    mic config show
    mic config ext .H .C [--user]            .mic.json (repo) or ~/.config/mic/config.json (--user)
    mic config ext .tpp=cpp
    mic config ignore tutorials/ '**/Make/'  appends to .micignore (repo) or the user config (--user)
    mic config include src/finiteVolume      limits compiles to these paths (repo)
    mic config auto off                      stop reading uppercase .C/.H as C++ automatically
"""
from __future__ import annotations

import json
import os
import sys

from . import scan


def _target(root: str, user: bool) -> str:
    return scan.USER_CONFIG if user else os.path.join(root, scan.REPO_CONFIG)


def _load(path: str) -> dict:
    return scan._read_json(path)


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"[mic] updated {path}", file=sys.stderr)


def run(root: str, action: str, values: list, user: bool = False) -> None:
    if action == "show":
        cfg = scan.project_config(root)
        print(json.dumps({"user config": scan.USER_CONFIG if os.path.exists(scan.USER_CONFIG) else None,
                          "repo config": os.path.join(root, scan.REPO_CONFIG) if os.path.exists(os.path.join(root, scan.REPO_CONFIG)) else None,
                          ".micignore": os.path.exists(os.path.join(root, ".micignore")),
                          "file types": scan.load_languages(root), "auto .C/.H as C++": cfg.get("auto_extensions", True),
                          "ignore": cfg.get("ignore", []), "include": cfg.get("include")}, indent=2))
        return
    if action == "ext":
        mapping = scan._parse_mapping(values)
        if not mapping:
            sys.exit("mic config ext: give extensions like .H .C or .tpp=cpp")
        path = _target(root, user)
        data = _load(path)
        ext = data.get("extensions", {})
        ext = scan._parse_mapping(ext) if not isinstance(ext, dict) else ext
        ext.update(mapping)
        data["extensions"] = ext
        _save(path, data)
        print("[mic] file types: " + ", ".join(f"{k}={v}" for k, v in sorted(mapping.items()))
              + ". Run `mic update` (or /mic:update) to pick them up.", file=sys.stderr)
        return
    if action == "ignore":
        if not values:
            sys.exit("mic config ignore: give one or more patterns, e.g. tutorials/ '**/Make/'")
        if user:
            path = scan.USER_CONFIG
            data = _load(path)
            data["ignore"] = list(dict.fromkeys(list(data.get("ignore", [])) + values))
            _save(path, data)
        else:
            path = os.path.join(root, ".micignore")
            existing = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
            with open(path, "a", encoding="utf-8") as f:
                for v in values:
                    if v not in existing:
                        f.write(v + "\n")
                        existing.append(v)
            print(f"[mic] updated {path}", file=sys.stderr)
        return
    if action == "include":
        path = os.path.join(root, scan.REPO_CONFIG)
        data = _load(path)
        data["include"] = list(dict.fromkeys(list(data.get("include") or []) + [v.rstrip("/") + ("/**" if "." not in os.path.basename(v) and "*" not in v else "") for v in values]))
        _save(path, data)
        return
    if action == "auto":
        path = _target(root, user)
        data = _load(path)
        data["auto_extensions"] = (values[:1] or ["on"])[0].lower() not in ("off", "false", "0", "no")
        _save(path, data)
        return
    sys.exit(f"mic config: unknown action {action}; use show, ext, ignore, include or auto")
