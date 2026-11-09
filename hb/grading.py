"""Two independent judges over existing rubric criteria, with explicit failure states.

  Jev    TypeSafe API, one Choice question per criterion, label probabilities stored.
  Astra  GPT-6 Astra through the official Codex CLI on the signed-in account (hb/astra_cli.py),
         one fresh session per report covering all of its criteria ("batch"), or one session per
         criterion ("single"). The mode is fixed in the config for every system.

Invariants:
  * one expected judgment per (run, criterion, judge); `hb verify` proves none is missing
  * a quota error, malformed response, context overflow, tool use by the judge or an unverifiable
    quote is an ATTEMPT with an error state. It is never turned into "not met" and never reused
  * both judges receive the same question and the same full report text; nothing is truncated
  * Astra runs first; its verdicts are on disk before Jev is called, and neither sees the other
  * the judge version that answered is recorded; for Jev it must equal the pinned version
  * "cannot_judge" (the criterion depends on something outside the report) is a verdict of its own.
    It is not an error and not a failed criterion

Attempt log: runs/<campaign>/grades/<judge>.jsonl (append-only). An item's grade is its FIRST attempt
with state "ok"; with none, the item is unresolved.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .budget import Budget, BudgetRefused
from .util import ROOT, append_jsonl, canon, now, read_json, read_jsonl, secret, sha256_text  # noqa: F401 (re-exported)

OK = "ok"
LABELS = ["met", "not_met", "cannot_judge"]
ERROR_STATES = ("error_quota", "error_budget", "error_malformed", "error_context", "error_quote_mismatch",
                "error_model_version", "error_tool_use", "error_environment", "error_other")
NO_RETRY = ("error_context", "error_model_version", "error_environment", "error_budget", "error_quota")
STOP = {"astra": threading.Event(), "jev": threading.Event()}  # set on quota/budget exhaustion: pause, never reroute


def judge_prompt(name: str) -> str:
    return (ROOT / "prompts" / name).read_text().strip()


PROMPT_FILE = {"astra": "judge_astra_system.txt", "jev": "judge_jev_instructions.txt"}


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def label_descriptions(polarity: int) -> dict[str, str]:
    """Label wording follows rubric semantics: for an error criterion, `met` means the error IS present."""
    if polarity < 0:
        d = {"met": "The report contains the error or flaw described by the criterion.",
             "not_met": "The report does not contain the error or flaw described by the criterion."}
    else:
        d = {"met": "The report fully satisfies the criterion.",
             "not_met": "The report does not satisfy the criterion."}
    d["cannot_judge"] = "The criterion cannot be decided from the report text because it depends on something outside the report."
    return d


def item_key(judge: str, jcfg: dict, prompt_sha: str, criterion: dict, report_sha: str, question_sha: str, batch_sha: str = "") -> str:
    """Everything that can change a verdict. A config, prompt, rubric or report change makes old verdicts unusable."""
    return sha256_text(canon(dict(judge=judge, model=jcfg["model"], params=jcfg.get("params", {}), mode=jcfg.get("mode", "single"),
                                  cli=jcfg.get("cli_version"), prompt=prompt_sha, labels=LABELS, text=criterion["text"],
                                  polarity=criterion["polarity"], report=report_sha, question=question_sha, batch=batch_sha)))


# ---------------------------------------------------------------- Jev (TypeSafe)

def jev_attempt(question: str, report: str, criterion: dict, jcfg: dict, mock: bool) -> dict:
    instructions = judge_prompt(PROMPT_FILE["jev"]).format(criterion=criterion["text"])
    if mock:
        from .mock import mock_verdict
        lab, probs = mock_verdict("jev", LABELS[:2], report, criterion["text"])
        return dict(state=OK, label=lab, probs=dict(probs, cannot_judge=0.0), confidence=max(probs.values()),
                    model_version=jcfg["model"], evidence_mode="full", usage=dict(input_tokens=len(report) // 4), mock=True)
    import typesafe_sdk as ts
    client = ts.TypeSafeClient(api_key=secret("TYPESAFE_API_KEY"), model=jcfg["model"],
                               retry=ts.RetryPolicy(max_retries=jcfg["sdk_max_retries"], timeout=jcfg["timeout_s"]))
    q = {"verdict": ts.Choice(instructions=instructions, criteria=label_descriptions(criterion["polarity"]))}
    state = {"research_question": question, "report": report}
    modes = ["full"] + (["long_context"] if jcfg.get("allow_long_context") else [])
    last: dict = {}
    for mode in modes:
        headers = {"TypeSafe-Danger-Zone-Long-Context": "true"} if mode == "long_context" else None
        try:
            r = client.system_one(state, q, model=jcfg["model"], extra_headers=headers)
        except ts.TypeSafeRateLimitError as e:
            return dict(state="error_quota", error=str(e)[:300], evidence_mode=mode)
        except ts.TypeSafeBadRequestError as e:
            if "max_tokens_exceeded" in str(e):  # over Jev's context: explicit state, one declared retry in long-context mode
                last = dict(state="error_context", error=str(e)[:300], evidence_mode=mode)
                continue
            return dict(state="error_other", error=str(e)[:300], evidence_mode=mode)
        except ts.TypeSafeAPIResponseValidationError as e:
            return dict(state="error_malformed", error=str(e)[:300], evidence_mode=mode)
        except ts.TypeSafeAPIError as e:
            st = getattr(e, "status", None)
            return dict(state="error_quota" if st in (402, 429, 529) else "error_other", error=str(e)[:300], evidence_mode=mode)
        except ts.TypeSafeError as e:
            return dict(state="error_other", error=str(e)[:300], evidence_mode=mode)
        a = r.answers.get("verdict")
        usage = dict(input_tokens=r.usage.input_tokens or 0, output_tokens=r.usage.output_tokens or 0)
        if r.model != jcfg["model"]:
            return dict(state="error_model_version", error=f"answered by {r.model}", usage=usage, evidence_mode=mode)
        if a is None or getattr(a, "choice", None) not in LABELS:
            return dict(state="error_malformed", error="no usable choice in response", usage=usage, evidence_mode=mode)
        return dict(state=OK, label=a.choice, probs=dict(a.probabilities), confidence=a.confidence,
                    model_version=r.model, usage=usage, evidence_mode=mode)
    return last


def jev_cost(usage: dict, cfg: dict) -> float:
    p = cfg["pricing"]["jev"]
    return (usage.get("input_tokens", 0) * p["input"] + usage.get("output_tokens", 0) * p["output"]) / 1e6


# ---------------------------------------------------------------- shared validation of an Astra verdict

def check_astra_item(v: dict | None, report: str) -> dict:
    """Turn one raw verdict object into an attempt. Quotes must really occur in the report."""
    if not isinstance(v, dict) or v.get("verdict") not in LABELS or not isinstance(v.get("report_quotes"), list):
        return dict(state="error_malformed", error="verdict missing or not in the required shape")
    nr = norm_ws(report)
    quotes = [q for q in v["report_quotes"] if isinstance(q, str) and norm_ws(q)]
    bad = [q for q in quotes if norm_ws(q) not in nr]
    if bad or (v["verdict"] == "met" and not quotes):
        return dict(state="error_quote_mismatch", error=f"{len(bad)} quoted passage(s) not found in the report"
                    if bad else "met without a supporting passage", label_unverified=v["verdict"], quotes=quotes,
                    reason=str(v.get("reason", ""))[:600])
    return dict(state=OK, label=v["verdict"], quotes=quotes, reason=str(v.get("reason", ""))[:600])


# ---------------------------------------------------------------- orchestration

def expected_items(cdir: Path, tasks: dict) -> list[dict]:
    """Every (run, criterion) that must receive a judgment from each judge."""
    items = []
    for meta_p in sorted(cdir.glob("outputs/*/meta.json")):
        m = read_json(meta_p)
        task = tasks[m["task_id"]]
        rp = meta_p.parent / "report.md"
        report = rp.read_text() if rp.exists() else ""
        bsha = sha256_text(canon([[c["cid"], c["text"], c["polarity"]] for c in task["criteria"]]))
        for c in task["criteria"]:
            items.append(dict(run_id=m["run_id"], task_id=m["task_id"], system=m["system"], cid=c["cid"], criterion=c,
                              question=task["question"], report=report, has_report=bool(report.strip()), batch_sha=bsha))
    return items


def key_for(judge: str, cfg: dict, it: dict) -> str:
    jcfg = cfg["judges"][judge]
    batch = it["batch_sha"] if judge == "astra" and jcfg.get("mode") == "batch" else ""
    return item_key(judge, jcfg, sha256_text(judge_prompt(PROMPT_FILE[judge])), it["criterion"],
                    sha256_text(it["report"]), sha256_text(it["question"]), batch)


def first_ok(path: Path) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for a in read_jsonl(path):
        if a["state"] == OK:
            out.setdefault((a["run_id"], a["cid"], a["key"]), a)
    return out


def _attempt_counts(path: Path) -> dict[tuple, int]:
    n: dict[tuple, int] = {}
    for a in read_jsonl(path):
        k = (a["run_id"], a["cid"], a["key"])
        n[k] = n.get(k, 0) + 1
    return n


def _record(path: Path, it: dict, judge: str, key: str, n: int, a: dict, t0: float, extra: dict | None = None) -> str:
    a = dict(a)
    a.update(run_id=it["run_id"], task_id=it.get("task_id"), cid=it["cid"], judge=judge, key=key, attempt=n, ts=t0,
             latency_s=round(now() - t0, 3), **(extra or {}))
    a["content_sha256"] = sha256_text(canon({k: a.get(k) for k in ("state", "label", "probs", "quotes", "reason")}))
    append_jsonl(path, a)
    return a["state"]


def grade_astra(cfg: dict, items: list[dict], path: Path, sessions_dir: Path, mock: bool, retry_errors: bool) -> dict:
    from . import astra_cli
    jcfg = cfg["judges"]["astra"]
    done, counts = first_ok(path), _attempt_counts(path)
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        gk = (it["run_id"],) if jcfg["mode"] == "batch" else (it["run_id"], it["cid"])
        groups.setdefault(gk, []).append(it)
    lock = threading.Lock()

    def work(group: list[dict]) -> list[str]:
        keys = {it["cid"]: key_for("astra", cfg, it) for it in group}
        pending = [it for it in group if (it["run_id"], it["cid"], keys[it["cid"]]) not in done]
        if not pending:
            return ["cached"] * len(group)
        base = max(counts.get((it["run_id"], it["cid"], keys[it["cid"]]), 0) for it in pending)
        used, states = (0 if retry_errors else base), {}
        while pending and used < jcfg["max_attempts"]:
            if STOP["astra"].is_set():
                break
            used += 1
            t0 = now()
            # The whole group is always sent, so the judge sees the same prompt on a retry as on the first try.
            res = astra_cli.judge(group[0]["question"], group[0]["report"], [dict(it["criterion"], cid=it["cid"]) for it in group],
                                  jcfg, sessions_dir, mock)
            if res["session"].get("quota"):
                STOP["astra"].set()
            with lock:
                for it in pending:
                    a = res["items"].get(it["cid"]) or dict(state="error_malformed", error="criterion missing from the judge's answer")
                    counts[(it["run_id"], it["cid"], keys[it["cid"]])] = counts.get((it["run_id"], it["cid"], keys[it["cid"]]), 0) + 1
                    states[it["cid"]] = _record(path, it, "astra", keys[it["cid"]], counts[(it["run_id"], it["cid"], keys[it["cid"]])], a, t0,
                                                dict(session=res["session"]["id"], mode=jcfg["mode"], model_version=res["session"].get("model")))
            pending = [it for it in pending if states[it["cid"]] != OK and states[it["cid"]] not in NO_RETRY]
        return [states.get(it["cid"], "cached" if (it["run_id"], it["cid"], keys[it["cid"]]) in done else
                           ("paused_quota" if STOP["astra"].is_set() else "skipped_max_attempts")) for it in group]

    with ThreadPoolExecutor(max_workers=jcfg["concurrency"]) as ex:
        res = [s for g in ex.map(work, list(groups.values())) for s in g]
    return {s: res.count(s) for s in sorted(set(res))}


def grade_jev(cfg: dict, items: list[dict], path: Path, mock: bool, retry_errors: bool) -> dict:
    jcfg = cfg["judges"]["jev"]
    done, counts = first_ok(path), _attempt_counts(path)
    budget = Budget("jev", cfg, mock)

    def work(it: dict) -> str:
        key = key_for("jev", cfg, it)
        k = (it["run_id"], it["cid"], key)
        if k in done:
            return "cached"
        n, used, state = counts.get(k, 0), (0 if retry_errors else counts.get(k, 0)), "skipped_max_attempts"
        while used < jcfg["max_attempts"]:
            if STOP["jev"].is_set():
                return "paused_budget"
            used += 1; n += 1
            t0 = now()
            bkey = f"{path.parent.parent.name}/{it['run_id']}/{it['cid']}/{key[:8]}/{n}"
            try:
                budget.reserve("grading", bkey, jcfg["reserve_usd"])  # reserved before the call is made
            except BudgetRefused as e:
                STOP["jev"].set()
                return _record(path, it, "jev", key, n, dict(state="error_budget", error=str(e)[:300]), t0)
            try:
                a = jev_attempt(it["question"], it["report"], it["criterion"], jcfg, mock)
            except Exception as e:  # a judge bug is an error attempt, not a verdict
                a = dict(state="error_other", error=f"{type(e).__name__}: {e}"[:300])
            cost = jev_cost(a.get("usage") or {}, cfg)
            budget.reconcile(bkey, cost)
            state = _record(path, it, "jev", key, n, a, t0, dict(cost_usd=round(cost, 6)))
            if state == "error_quota":
                STOP["jev"].set()  # pause: the remaining items are not sent, and nothing is rerouted
            if state == OK or state in NO_RETRY:
                break
        return state

    with ThreadPoolExecutor(max_workers=jcfg["concurrency"]) as ex:
        res = list(ex.map(work, items))
    return {s: res.count(s) for s in sorted(set(res))}


def grade(cfg: dict, cdir: Path, tasks: dict, judges: list[str], mock: bool, limit: int | None = None,
          retry_errors: bool = False) -> dict:
    items = [it for it in expected_items(cdir, tasks) if it["has_report"]]  # no report => no judge call; scored by rule
    if limit:
        keep = sorted({it["run_id"] for it in items})[:limit]
        items = [it for it in items if it["run_id"] in keep]
    for ev in STOP.values():
        ev.clear()
    stats = {}
    # Fixed stage order: Astra completes and is written to disk before Jev is called at all.
    if "astra" in judges:
        stats["astra"] = grade_astra(cfg, items, cdir / "grades" / "astra.jsonl", cdir / "grades" / "astra_sessions", mock, retry_errors)
    if "jev" in judges:
        stats["jev"] = grade_jev(cfg, items, cdir / "grades" / "jev.jsonl", mock, retry_errors)
    return stats


def final_grades(cfg: dict, cdir: Path, tasks: dict) -> list[dict]:
    """One row per expected (run, criterion) with both judges' decisions preserved side by side."""
    by = {j: (first_ok(cdir / "grades" / f"{j}.jsonl"),
              {(a["run_id"], a["cid"], a["key"]): a for a in read_jsonl(cdir / "grades" / f"{j}.jsonl")}) for j in ("astra", "jev")}
    adj = {(a["run_id"], a["cid"]): a for a in read_jsonl(cdir / "grades" / "adjudication.jsonl")}
    rows = []
    for it in expected_items(cdir, tasks):
        row = dict(run_id=it["run_id"], task_id=it["task_id"], system=it["system"], cid=it["cid"], has_report=it["has_report"])
        for judge, (ok, anyatt) in by.items():
            if not it["has_report"]:
                row[judge], row[f"{judge}_state"] = None, "no_report"
                continue
            k = (it["run_id"], it["cid"], key_for(judge, cfg, it))
            if k in ok:
                row[judge], row[f"{judge}_state"] = ok[k]["label"], OK
                if judge == "jev":
                    row["jev_probs"], row["jev_confidence"], row["jev_evidence_mode"] = ok[k].get("probs"), ok[k].get("confidence"), ok[k].get("evidence_mode")
            else:
                row[judge], row[f"{judge}_state"] = None, (anyatt[k]["state"] if k in anyatt else "missing")
        if not it["has_report"]:
            row["consensus"] = "no_report"
        elif row["jev_state"] == OK and row["astra_state"] == OK:
            row["consensus"] = row["jev"] if row["jev"] == row["astra"] else "disagree"
        else:
            row["consensus"] = "unresolved_error"
        a = adj.get((it["run_id"], it["cid"]))
        row["adjudicated"] = a["label"] if a else None
        rows.append(row)
    return rows
