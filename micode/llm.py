"""Compiler model calls.

Two backends, both stdlib-only:
  * claude-cli  – the locally authenticated `claude -p` (default; works with a Claude subscription)
  * api         – the Anthropic Messages API over urllib (used when ANTHROPIC_API_KEY is set and
                  MICODE_BACKEND=api)

Every call is cached on disk by the hash of (model, system, prompt), so an interrupted compile
resumes for free and recompiles only pay for what changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

MODEL_ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                 "haiku": "claude-haiku-4-5-20251001"}


class LLMError(RuntimeError):
    pass


class Usage:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = self.cached = 0
        self.cost = 0.0
        self.in_tok = self.out_tok = 0

    def add(self, cost=0.0, i=0, o=0, cached=False):
        with self.lock:
            self.calls += 1
            self.cached += int(cached)
            self.cost += cost or 0.0
            self.in_tok += i
            self.out_tok += o


USAGE = Usage()


class LLM:
    def __init__(self, cache_dir: str, backend: str | None = None, timeout: int = 900):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.backend = backend or os.environ.get("MICODE_BACKEND") or "claude-cli"
        self.timeout = timeout

    # ------------------------------------------------------------ public
    def complete(self, prompt: str, model: str, system: str = "", max_tokens: int = 16000) -> str:
        key = hashlib.sha256(json.dumps([model, system, prompt]).encode()).hexdigest()[:32]
        path = os.path.join(self.cache_dir, key[:2], key + ".json")
        if os.path.exists(path):
            try:
                USAGE.add(cached=True)
                return json.load(open(path))["text"]
            except (OSError, ValueError, KeyError):
                pass
        last = None
        for attempt in range(4):
            try:
                if self.backend == "api":
                    text, cost, i, o = self._api(prompt, model, system, max_tokens)
                else:
                    text, cost, i, o = self._cli(prompt, model, system)
                USAGE.add(cost, i, o)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                json.dump({"model": model, "text": text}, open(tmp, "w"))
                os.replace(tmp, path)
                return text
            except LLMError as e:
                last = e
                msg = str(e).lower()
                if "limit" in msg and ("usage" in msg or "spend" in msg):
                    raise  # account limit: retrying will not help; the cache keeps finished work
                time.sleep(5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def complete_json(self, prompt: str, model: str, system: str = "", max_tokens: int = 16000):
        text = self.complete(prompt, model, system, max_tokens)
        try:
            return parse_json(text)
        except ValueError:
            # One repair attempt, uncached prompt variation so it is a genuinely new call.
            fixed = self.complete(prompt + "\n\nReturn ONLY the JSON object, no prose, no code fence.",
                                  model, system, max_tokens)
            return parse_json(fixed)

    # ------------------------------------------------------------ backends
    def _cli(self, prompt: str, model: str, system: str):
        cmd = ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
               "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]
        if system:
            cmd += ["--system-prompt", system]
        env = dict(os.environ, MICODE_CHILD="1")
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout,
                               env=env, cwd=self.cache_dir)
        except subprocess.TimeoutExpired:
            raise LLMError("claude CLI timed out")
        out = r.stdout.strip()
        try:
            d = json.loads(out)
        except ValueError:
            raise LLMError(f"claude CLI failed ({r.returncode}): {(r.stderr or out)[:400]}")
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            raise LLMError(f"claude CLI error: {str(d.get('result'))[:400]}")
        u = d.get("usage", {})
        i = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        return d.get("result", ""), d.get("total_cost_usd", 0.0), i, u.get("output_tokens", 0)

    def _api(self, prompt: str, model: str, system: str, max_tokens: int):
        body = {"model": MODEL_ALIASES.get(model, model), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                     "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                d = json.load(resp)
        except Exception as e:  # noqa: BLE001 - surface any transport error as retryable
            raise LLMError(f"api error: {e}")
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return text, 0.0, u.get("input_tokens", 0), u.get("output_tokens", 0)


def parse_json(text: str):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON found")
    t = t[start:]
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(t)
        return obj
    except ValueError:
        # Tolerate trailing commas, the most common model JSON slip.
        obj, _ = dec.raw_decode(re.sub(r",(\s*[}\]])", r"\1", t))
        return obj
