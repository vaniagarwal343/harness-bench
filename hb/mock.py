"""Deterministic offline stand-ins for the model, the search backend and both judges.

Used only with --mock. Lets the full pipeline (schedule -> run -> grade -> aggregate -> verify)
be exercised with zero spend. Mock numbers mean nothing and are stamped `mock: true`.
"""
from __future__ import annotations

import hashlib
import json


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def _last_user_text(body: dict) -> str:
    for m in body.get("messages", []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            for b in c:
                if b.get("type") == "text":
                    return b["text"]
    return ""


def mock_message(body: dict) -> dict:
    question = _last_user_text(body)
    n_results = sum(1 for m in body.get("messages", []) if m.get("role") == "user" and isinstance(m.get("content"), list)
                    for b in m["content"] if b.get("type") == "tool_result")
    tools = {t.get("name") for t in body.get("tools") or []}
    content, stop = [], "end_turn"
    if "search" in tools and n_results == 0:
        content = [{"type": "tool_use", "id": "toolu_mock_1", "name": "search", "input": {"query": question[:80]}}]
        stop = "tool_use"
    elif "tavily_search" in tools and n_results == 0:
        content = [{"type": "tool_use", "id": "toolu_mock_1", "name": "tavily_search", "input": {"query": question[:80]}}]
        stop = "tool_use"
    elif "fetch" in tools and n_results == 1:
        content = [{"type": "tool_use", "id": "toolu_mock_2", "name": "fetch",
                    "input": {"url": "https://example.org/mock-source"}}]
        stop = "tool_use"
    else:
        cite = " [1]\n\n## Sources\n[1] https://example.org/mock-source" if n_results else ""
        content = [{"type": "text", "text": f"# Mock report\n\nMock answer to: {question[:200]}\n\n"
                                            f"Key finding {_h(question) % 97}: this is placeholder text.{cite}\n"}]
    inp = max(10, len(json.dumps(body)) // 4)
    return {"id": "msg_mock_" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12],
            "type": "message", "role": "assistant", "model": body.get("model"), "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": inp, "output_tokens": 60, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}


def sse_bytes(msg: dict) -> bytes:
    def ev(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"
    start = dict(msg, content=[], stop_reason=None, usage=dict(msg["usage"], output_tokens=0))
    out = [ev("message_start", {"type": "message_start", "message": start})]
    for i, b in enumerate(msg["content"]):
        if b["type"] == "text":
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": {"type": "text", "text": ""}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": dict(b, input={})}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "input_json_delta",
                                                            "partial_json": json.dumps(b["input"])}}))
        out.append(ev("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"],
                                                                      "stop_sequence": None},
                                    "usage": {"output_tokens": msg["usage"]["output_tokens"]}}))
    out.append(ev("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


def mock_search(q: str, n: int) -> list[dict]:
    return [dict(title=f"Mock result {i} for {q[:30]}", url=f"https://example.org/mock-source?i={i}",
                 snippet="placeholder snippet") for i in range(min(n, 3))] + \
           [dict(title="benchmark leak", url="https://huggingface.co/datasets/ScaleAI/researchrubrics", snippet="x")]


def mock_page(url: str) -> str:
    return f"Mock page body for {url}. " + "Placeholder evidence sentence. " * 20


def mock_verdict(judge: str, labels: list[str], report: str, criterion: str) -> tuple[str, dict]:
    """Deterministic pseudo-verdict; the two mock judges disagree on roughly one item in six."""
    base = _h(report[:2000] + criterion)
    k = base % len(labels)
    if judge == "astra" and base % 6 == 0:
        k = (k + 1) % len(labels)
    probs = {lab: (0.8 if i == k else 0.2 / (len(labels) - 1)) for i, lab in enumerate(labels)}
    return labels[k], probs
