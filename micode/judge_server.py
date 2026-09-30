"""Local paragraph judge: Laya (open-source typed-decision model) kept resident behind a loopback HTTP API.

Hooks are short-lived processes and Laya takes ~30 s to load, so the model lives in this daemon and hooks
talk to it over 127.0.0.1. It runs in its own Python environment (torch + laya); micode itself stays stdlib.

    <laya-python> -m micode.judge_server            # or: micode judge-server

POST /judge  {"question": str, "candidates": [{"id", "text"}]}  -> {"probs": {id: p}, "ms": float}
POST /embed  {"texts": [str]}                                   -> {"vectors": [[float]], "ms": float}
GET  /health

Pipeline optimisations over calling the model once per candidate:
  * one batched forward pass per request, candidates sorted by length into buckets to cut padding;
  * candidate text trimmed to what decides relevance (header, compiled note, first lines of code);
  * inference_mode, all cores, optional bf16 autocast on CPUs that support it;
  * the process stays warm, and it exits after an idle timeout so it never lingers.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

HOST = "127.0.0.1"
PORT = int(os.environ.get("MICODE_JUDGE_PORT", "8791"))
IDLE_EXIT = int(os.environ.get("MICODE_JUDGE_IDLE", "7200"))
CHECKPOINT = os.environ.get("MICODE_JUDGE_MODEL", "convaiinnovations/laya")
BATCH = int(os.environ.get("MICODE_JUDGE_BATCH", "32"))
QUESTION = {
    "needed": {
        "type": "noul",
        "instructions": "Is this code needed to answer the developer's question?",
        "criteria": {"true": "it implements, configures, calls or tests what the question asks about",
                     "false": "it is about something else"},
    }
}

LOCK = threading.Lock()
STATE = {"agent": None, "last": time.time(), "loaded_s": None}


def agent():
    if STATE["agent"] is None:
        import laya
        import torch
        torch.set_num_threads(int(os.environ.get("MICODE_JUDGE_THREADS", os.cpu_count() or 4)))
        t0 = time.time()
        STATE["agent"] = laya.load(CHECKPOINT)
        STATE["loaded_s"] = round(time.time() - t0, 1)
    return STATE["agent"]


def judge(question: str, candidates: list[dict]) -> dict:
    a = agent()
    states = [{"developer_question": question, "candidate": c["text"][:1500]} for c in candidates]
    order = sorted(range(len(states)), key=lambda i: len(states[i]["candidate"]))  # length buckets: less padding
    probs: dict = {}
    for k in range(0, len(order), BATCH):
        idx = order[k:k + BATCH]
        res = a.predict_batch([states[i] for i in idx], QUESTION)
        for i, r in zip(idx, res):
            probs[candidates[i]["id"]] = float(r["answers"]["needed"]["noul"])
    return probs


def embed(texts: list[str]) -> list[list[float]]:
    from laya.shortlist import embed_fn_from_agent
    fn = embed_fn_from_agent(agent(), max_length=256, batch_size=BATCH)
    return fn(texts).tolist()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ready": STATE["agent"] is not None, "model": CHECKPOINT, "loaded_s": STATE["loaded_s"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        STATE["last"] = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            t0 = time.perf_counter()
            with LOCK:  # one model, one queue
                if self.path == "/judge":
                    out = {"probs": judge(body["question"], body["candidates"])}
                elif self.path == "/embed":
                    out = {"vectors": embed(body["texts"])}
                else:
                    return self._send(404, {"error": "not found"})
            out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self._send(200, out)
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - STATE["last"] > IDLE_EXIT:
                os._exit(0)

    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=lambda: (agent(), print(f"[micode-judge] ready in {STATE['loaded_s']}s", flush=True)),
                     daemon=True).start()
    print(f"[micode-judge] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
