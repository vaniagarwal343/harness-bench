"""Contestant configurations.

closed_book   A  one Messages call, no tools, through the gateway (which rejects any tool).
plain_tools   B  ordinary single-agent tool loop over the Messages API with search + fetch.
subprocess    harness adapters (GPT Researcher, Deep Agents) run as isolated child
              processes that can only reach the model and the web through the gateway.

Every system receives the question text verbatim and nothing else from the dataset.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
from pathlib import Path

import anthropic
import httpx

from .gateway import Gateway, Ledger
from .util import ROOT, now, scrubbed_env, sha256_text

SEARCH_TOOL = {
    "name": "search",
    "description": "Search the web. Returns a list of results with title, url and a short snippet. "
                   "Use fetch on a result url to read the page itself.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                     "properties": {"query": {"type": "string", "description": "Search query"},
                                    "max_results": {"type": "integer", "description": "1-10, default 5"}}},
}
FETCH_TOOL = {
    "name": "fetch",
    "description": "Read a web page and return its main text. Long pages are cut off with a visible marker.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["url"],
                     "properties": {"url": {"type": "string", "description": "http(s) URL to read"}}},
}


def prompt(name: str) -> str:
    return (ROOT / "prompts" / name).read_text().strip()


def prompt_hashes() -> dict:
    return {p.name: sha256_text(p.read_text()) for p in sorted((ROOT / "prompts").glob("*.txt"))}


def _text(msg) -> str:
    return "\n".join(b.text for b in msg.content if b.type == "text").strip()


def _client(led: Ledger, gw: Gateway, cfg: dict) -> anthropic.Anthropic:
    # The run token is the only credential; base_url points at the gateway.
    return anthropic.Anthropic(api_key=led.token, base_url=gw.url, max_retries=cfg["execution"]["sdk_max_retries"],
                               timeout=cfg["execution"]["llm_request_timeout_s"])


def _classify(e: Exception) -> str:
    s = str(e)
    if "HB_FUNDING_LIMIT" in s:
        return "funding_limit_interrupted"
    if "HB_SAFEGUARD" in s:
        return "safeguard_interrupted"
    if "HB_POLICY" in s:
        return "policy_violation"
    return "failed_infra"


def run_closed_book(question: str, scfg: dict, led: Ledger, gw: Gateway, cfg: dict, run_dir: Path) -> dict:
    client = _client(led, gw, cfg)
    try:
        with client.messages.stream(
            model=cfg["model"]["id"], max_tokens=scfg["max_tokens"],
            system=prompt("closed_book_system.txt"),
            output_config={"effort": scfg["effort"]},
            messages=[{"role": "user", "content": prompt("task_user.txt").format(question=question)}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as e:
        return dict(status=_classify(e), report="", error=str(e)[:500])
    report = _text(msg)
    status = {"end_turn": "completed", "max_tokens": "partial", "refusal": "refused"}.get(msg.stop_reason, "failed_infra")
    return dict(status=status if report or status == "refused" else "failed_infra", report=report,
                stop_reason=msg.stop_reason, turns=1)


def run_plain_tools(question: str, scfg: dict, led: Ledger, gw: Gateway, cfg: dict, run_dir: Path) -> dict:
    """Append-only manual loop. History is never edited, so Fable 5.2's history-editing check cannot fire."""
    client = _client(led, gw, cfg)
    http = httpx.Client(base_url=gw.url, headers={"x-api-key": led.token}, timeout=cfg["tools"]["timeout_s"] + 30)
    messages: list = [{"role": "user", "content": prompt("task_user.txt").format(question=question)}]
    kwargs = dict(model=cfg["model"]["id"], max_tokens=scfg["max_tokens"], system=prompt("plain_tools_system.txt"),
                  tools=[SEARCH_TOOL, FETCH_TOOL], output_config={"effort": scfg["effort"]},
                  cache_control={"type": "ephemeral"})
    turns, last_text = 0, ""
    try:
        while True:
            turns += 1
            if scfg.get("compaction"):
                cm = client.beta.messages.stream(betas=["compact-2026-01-12"], messages=messages,
                                                 context_management={"edits": [{"type": "compact_20260112"}]}, **kwargs)
            else:
                cm = client.messages.stream(messages=messages, **kwargs)
            with cm as stream:
                msg = stream.get_final_message()
            messages.append({"role": "assistant", "content": msg.content})  # thinking blocks go back unchanged
            last_text = _text(msg) or last_text
            if msg.stop_reason == "tool_use":
                results = []
                for b in msg.content:
                    if b.type != "tool_use":
                        continue
                    if b.name in ("search", "fetch") and isinstance(b.input, dict):
                        r = http.post(f"/tools/{b.name}", json=b.input)
                        out, is_err = r.text, r.status_code != 200 or '"error"' in r.text[:40]
                    else:
                        out, is_err = '{"error": "unknown tool or malformed input"}', True
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                continue
            if msg.stop_reason == "pause_turn":
                continue
            if msg.stop_reason == "end_turn":
                return dict(status="completed" if _text(msg) else "failed_infra", report=_text(msg),
                            stop_reason=msg.stop_reason, turns=turns)
            if msg.stop_reason == "refusal":
                return dict(status="refused", report="", stop_reason="refusal", turns=turns)
            # max_tokens or anything else: whatever text exists is a partial answer
            return dict(status="partial", report=_text(msg), stop_reason=msg.stop_reason, turns=turns)
    except anthropic.APIError as e:
        kind = _classify(e)
        # Frozen rule: after an interruption the last assistant text is NOT promoted to a report unless it
        # came from an end_turn. Mid-research notes are not an answer.
        return dict(status=kind, report="", error=str(e)[:500], turns=turns, last_text_chars=len(last_text))
    finally:
        http.close()


def run_subprocess(question: str, scfg: dict, led: Ledger, gw: Gateway, cfg: dict, run_dir: Path) -> dict:
    """Run an external harness in a fresh working directory with a scrubbed environment."""
    work, home = run_dir / "work", run_dir / "home"
    work.mkdir(parents=True, exist_ok=True); home.mkdir(parents=True, exist_ok=True)
    (work / "question.txt").write_text(question)
    env = scrubbed_env({
        "ANTHROPIC_API_KEY": led.token, "ANTHROPIC_BASE_URL": gw.url,
        "HB_GATEWAY_URL": gw.url, "HB_RUN_TOKEN": led.token, "HB_MODEL": cfg["model"]["id"],
        "HB_EFFORT": scfg.get("effort", cfg["model"]["effort"]),
        "HB_QUESTION_FILE": str(work / "question.txt"), "HB_REPORT_FILE": str(work / "report.md"),
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        **{k: str(v).format(gateway=gw.url, token=led.token, model=cfg["model"]["id"], work=work, root=ROOT)
           for k, v in scfg.get("env", {}).items()},
    })
    cmd = [a.format(root=ROOT, work=work, model=cfg["model"]["id"]) for a in shlex.split(scfg["command"])]
    wall = cfg["safeguards"]["max_wall_seconds"]
    t0 = now()
    with open(run_dir / "stdout.log", "wb") as so, open(run_dir / "stderr.log", "wb") as se:
        try:
            p = subprocess.Popen(cmd, cwd=work, env=env, stdout=so, stderr=se, start_new_session=True)
        except FileNotFoundError as e:
            return dict(status="failed_infra", report="", error=f"adapter not installed: {e}")
        try:
            code = p.wait(timeout=wall + cfg["execution"]["kill_grace_s"])
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)  # whole process group: subagents and retries die too
            code, timed_out = -9, True
    rp = work / "report.md"
    report = rp.read_text().strip() if rp.exists() else ""
    if timed_out:
        led.interrupt("safeguard:wall_time")
    # An interruption is labelled as such even if a report file exists: it did not finish on its own.
    if led.interrupted == "funding_limit":
        status = "funding_limit_interrupted"
    elif led.interrupted:
        status = "safeguard_interrupted"
    elif code == 0 and report:
        status = "completed"
    elif report:
        status = "partial"  # a final report exists but the harness process ended with an error
    else:
        status = "failed_infra"
    return dict(status=status, report=report, exit_code=code, timed_out=timed_out, wall_s=round(now() - t0, 1))


RUNNERS = {"closed_book": run_closed_book, "plain_tools": run_plain_tools, "subprocess": run_subprocess}
