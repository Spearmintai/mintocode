"""`micode setup-judge`: install the optional local paragraph judge (Laya, an open-source typed-decision model).

It lives in its own virtualenv (~/.cache/micode/laya-venv) so micode itself stays dependency-free. Needs `uv`
(https://docs.astral.sh/uv/) or falls back to python -m venv + pip. The first start downloads ~1.7 GB of weights.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from .judge import CONFIG, start_local

VENV = os.path.join(os.path.expanduser("~"), ".cache", "micode", "laya-venv")


def setup(gpu: bool = False) -> None:
    py = os.path.join(VENV, "bin", "python")
    uv = shutil.which("uv")
    torch_index = [] if gpu else ["--index-url", "https://download.pytorch.org/whl/cpu"]
    if not os.path.exists(py):
        print(f"[micode] creating {VENV}", file=sys.stderr)
        if uv:
            subprocess.run([uv, "venv", "--python", "3.12", VENV], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    pip = [uv, "pip", "install", "--python", py] if uv else [py, "-m", "pip", "install"]
    print("[micode] installing torch (" + ("CUDA" if gpu else "CPU") + ") and laya", file=sys.stderr)
    subprocess.run(pip + ["torch"] + torch_index, check=True)
    subprocess.run(pip + ["laya"], check=True)
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG):
        cfg = json.load(open(CONFIG))
    cfg["laya_python"] = py
    json.dump(cfg, open(CONFIG, "w"), indent=1)
    os.environ["MICODE_LAYA_PYTHON"] = py
    start_local()
    print("[micode] judge installed; the daemon is starting on 127.0.0.1:8791 (first start downloads the weights, "
          "log: ~/.cache/micode-judge.log)", file=sys.stderr)
