"""Client for the paragraph judge. Order of preference:
  1. the local Laya daemon (micode.judge_server) on 127.0.0.1, if it is running;
  2. TypeSafe's hosted Jev, if TYPESAFE_API_KEY is set;
  3. none: micode falls back to its deterministic link ranking.
If a Laya environment is configured (MICODE_LAYA_PYTHON) but the daemon is down, it is started in the
background so the next request can use it. Stdlib only; every call has a hard timeout.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

PORT = int(os.environ.get("MICODE_JUDGE_PORT", "8791"))
URL = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _post(path: str, body: dict, timeout: float):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


CONFIG = os.path.join(os.path.expanduser("~"), ".config", "micode", "config.json")


def _config() -> dict:
    try:
        return json.load(open(CONFIG))
    except (OSError, ValueError):
        return {}


def local_ready(timeout: float = 0.3) -> bool:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=timeout) as r:
            return bool(json.load(r).get("ready"))
    except OSError:
        return False


def start_local() -> bool:
    """Start the daemon in the background if a Laya-capable Python is configured."""
    py = os.environ.get("MICODE_LAYA_PYTHON") or _config().get("laya_python")
    if not py or not os.path.exists(py):
        return False
    try:
        with urllib.request.urlopen(URL + "/health", timeout=0.3):
            return True  # already up (maybe still loading)
    except OSError:
        pass
    log = open(os.path.join(os.path.expanduser("~"), ".cache", "micode-judge.log"), "a") \
        if os.path.isdir(os.path.expanduser("~/.cache")) else subprocess.DEVNULL
    subprocess.Popen([py, "-m", "micode.judge_server"], cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=dict(os.environ, PYTHONPATH=ROOT))
    return True


def get(timeout: float = 8.0):
    """Return a judge callable (question, [(id, text)]) -> {id: p}, or None."""
    if local_ready():
        def local(question, cands):
            try:
                return _post("/judge", {"question": question,
                                        "candidates": [{"id": i, "text": t} for i, t in cands]}, timeout)["probs"]
            except OSError:
                return {}
        return local
    start_local()
    from . import jev
    if jev.available():
        return jev.needed
    return None

