"""Paragraph selection with TypeSafe's Jev (a "System One" judgment model: typed questions in, calibrated
probabilities out, ~250 ms, no text generation).

For every candidate paragraph the question is the same yes/no judgment: "is this needed to answer the
developer's question?". All candidates are judged in parallel, so choosing the minimal pack takes about one Jev
round-trip instead of an LLM reading the candidates. Without TYPESAFE_API_KEY this module is inert and micode
falls back to its deterministic link ranking.

API: POST https://api.typesafe.ai/v1/systemone, Authorization: Bearer <key>, model "jev-latest".
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
QUESTION = {
    "needed": {
        "type": "noul",
        "instructions": ("Is this piece of code or compiled note needed to answer the developer's question completely "
                         "and precisely (it implements, configures, calls, or tests the behaviour asked about)?"),
        "criteria": {"true": "needed: the answer would be incomplete or wrong without it",
                     "false": "not needed: unrelated, or only tangentially related"},
    }
}


def available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def _call(state, questions, timeout: float = 10.0) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 3:
                time.sleep(0.4 * 2 ** attempt)
                continue
            raise
    return {}


def needed(query: str, candidates: list[tuple[str, str]], workers: int = 16) -> dict[str, float]:
    """Probability that each candidate (id, text) is needed to answer `query`."""
    def one(c):
        cid, text = c
        try:
            d = _call({"developer_question": query, "candidate": text[:4000]}, QUESTION)
            return cid, float(d["answers"]["needed"]["noul"])
        except Exception:  # noqa: BLE001 - a failed judgment just leaves the candidate unscored
            return cid, None
    with ThreadPoolExecutor(workers) as ex:
        return {cid: p for cid, p in ex.map(one, candidates) if p is not None}


needed.relative = False  # Jev is calibrated: use its probabilities with an absolute 0.5 cut
