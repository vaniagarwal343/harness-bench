"""Schedule and execute contestant runs in fresh, isolated state.

Manifest: one row per scheduled run (task x system, one run each). A block is one task; block order
and the order of systems inside each block are seeded-random, so configurations are interleaved in
time and no system always runs first.

Money: before a run is dispatched its full cap is reserved in the persistent budget ledger
(hb/budget.py) against the campaign's pool; afterwards the reservation is reconciled to the cost
the gateway metered. A run whose cap cannot be reserved is not started.

This module reads data/public/tasks.jsonl only. It never opens evaluator data.
"""
from __future__ import annotations

import importlib.metadata as md
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .budget import Budget, BudgetRefused, plan_check
from .datasets import load_public, load_splits
from .gateway import Gateway
from .systems import RUNNERS, prompt_hashes
from .util import git_state, now, read_json, read_jsonl, secret, sha256_text, write_json, write_jsonl

TERMINAL = ("completed", "partial", "refused", "budget_exhausted", "timeout", "policy_violation", "failed_infra")


def build_manifest(cfg: dict, cdir: Path, split: str, systems: list[str], limit: int | None, mock: bool) -> list[dict]:
    mp = cdir / "manifest.jsonl"
    if mp.exists():
        raise SystemExit(f"{mp} exists. A manifest is frozen once written; use a new campaign name.")
    problems = plan_check(cfg)
    if problems:
        raise SystemExit("the frozen counts and caps do not fit the funding:\n  " + "\n  ".join(problems))
    g = git_state()
    if split == "main" and not mock and cfg["freeze"]["require_clean_tagged_commit_for_main"]:
        if not g["commit"] or g["dirty"] or not any(t.startswith(cfg["freeze"]["tag_prefix"]) for t in g["tags"]):
            raise SystemExit(f"main can only be scheduled from a clean commit tagged {cfg['freeze']['tag_prefix']}*; state: {g}")
    if split == "main" and not mock:
        st = Budget("anthropic", cfg).status()
        need = cfg["funding"]["anthropic"]["pools"]["main"]
        if st["headroom_vs_confirmed"] < need:
            raise SystemExit(f"the main set needs ${need:.2f} reservable but confirmed funding leaves ${st['headroom_vs_confirmed']:.2f}. "
                             "Confirm the balance in the Console and update configs/funding.local.toml.")
    tasks = load_public()
    ids = load_splits()["splits"][split][: limit or None]
    rng = random.Random(f"{cfg['sampling']['seed']}:{split}:{cdir.name}")
    blocks = ids[:]
    rng.shuffle(blocks)
    rows = []
    for bi, tid in enumerate(blocks):
        order = systems[:]
        rng.shuffle(order)
        for pos, s in enumerate(order):
            rows.append(dict(run_id=f"{tid}__{s}", task_id=tid, dataset=tasks[tid]["dataset"], system=s,
                             repeat=1, block=bi, position=pos, split=split))
    write_jsonl(mp, rows)
    write_json(cdir / "campaign.json", dict(split=split, pool=split, scored=split == "main", systems=systems, created=now(),
                                            config_sha256=cfg["_sha256"], splits_sha256=sha256_text(canon_splits()),
                                            prompts=prompt_hashes(), git=g, mock=mock,
                                            system_configs={s: cfg["systems"][s] for s in systems}))
    return rows


def canon_splits() -> str:
    import json
    return json.dumps(load_splits(), sort_keys=True)


def _sum_ledgers(ledgers: list[dict]) -> dict:
    out = dict(llm_calls=0, llm_errors=0, cost_usd=0.0, tool_errors=0, tool_cost_usd=0.0, max_committed_usd=0.0, budget_refusals=0,
               usage={}, tool_calls={}, models_seen={}, efforts_seen={}, violations=[])
    for l in ledgers:
        for k in ("llm_calls", "llm_errors", "cost_usd", "tool_errors", "tool_cost_usd", "budget_refusals"):
            out[k] += l[k]
        out["max_committed_usd"] = max(out["max_committed_usd"], l["max_committed_usd"])
        for k in ("usage", "tool_calls", "models_seen", "efforts_seen"):
            for kk, v in l[k].items():
                out[k][kk] = out[k].get(kk, 0) + v
        out["violations"] += l["violations"]
    out["cost_usd"], out["tool_cost_usd"] = round(out["cost_usd"], 6), round(out["tool_cost_usd"], 6)
    return out


def run_one(cfg: dict, cdir: Path, gw: Gateway, sched: dict, question: str, budgets: dict, camp: dict) -> dict | None:
    scfg = cfg["systems"][sched["system"]]
    rdir = cdir / "outputs" / sched["run_id"]
    if (rdir / "meta.json").exists():
        return read_json(rdir / "meta.json")  # a finished run is never silently re-run
    b = dict(cfg["budget"][scfg["budget"]])
    policy = dict(model=cfg["model"]["id"], effort=scfg.get("effort", cfg["model"]["effort"]),
                  allow_tools=scfg["kind"] != "closed_book", budget=b, shims=scfg.get("shims", []))
    ledgers, t0, result, attempts, refused = [], now(), {}, 0, None
    # Frozen retry rule: only an infrastructure failure is retried, once, paid from the buffer pool.
    # Budget stops, refusals and timeouts are outcomes, not failures.
    while attempts <= cfg["execution"]["infra_retries"]:
        key = f"{cdir.name}/{sched['run_id']}/a{attempts + 1}"
        pool = camp["pool"] if attempts == 0 else "buffer"
        try:
            budgets["anthropic"].reserve(pool, key, b["max_cost_usd"])
        except BudgetRefused as e:
            refused = str(e)
            break
        exa_reserved = False
        if policy["allow_tools"]:
            try:
                budgets["exa"].reserve("research", key, b["max_tool_cost_usd"])
                exa_reserved = True
            except BudgetRefused as e:
                budgets["anthropic"].release(key, "exa reservation refused")
                refused = str(e)
                break
        attempts += 1
        adir = rdir / f"attempt_{attempts}"
        adir.mkdir(parents=True, exist_ok=True)
        led = gw.open_run(adir, policy)
        try:
            result = RUNNERS[scfg["kind"]](question, scfg, led, gw, cfg, adir)
        except Exception as e:  # adapter bug: recorded as an infrastructure failure, not hidden
            result = dict(status="failed_infra", report="", error=f"{type(e).__name__}: {e}"[:500])
        finally:
            summ = gw.close_run(led)
            ledgers.append(summ)
            budgets["anthropic"].reconcile(key, summ["cost_usd"])
            if exa_reserved:
                budgets["exa"].reconcile(key, summ["tool_cost_usd"])
        if result["status"] != "failed_infra":
            break
    if attempts == 0:
        return None  # nothing was dispatched; the run stays unscheduled and `hb verify` reports it missing
    report = result.pop("report", "") or ""
    (rdir / "report.md").write_text(report)
    meta = dict(sched, status=result["status"], attempts=attempts, retry_refused=refused, result=result,
                wall_s=round(now() - t0, 2), report_chars=len(report), report_sha256=sha256_text(report),
                ledger=_sum_ledgers(ledgers), attempt_ledgers=ledgers, policy=policy, question_sha256=sha256_text(question),
                model=cfg["model"]["id"], config_sha256=cfg["_sha256"], git=git_state(), mock=gw.mock,
                versions={p: md.version(p) for p in ("anthropic", "httpx")}, started=t0)
    assert meta["status"] in TERMINAL, meta["status"]
    write_json(rdir / "meta.json", meta)
    return meta


def run_campaign(cfg: dict, cdir: Path, mock: bool, workers: int, only: list[str] | None = None) -> dict:
    tasks = load_public()
    manifest = read_jsonl(cdir / "manifest.jsonl")
    camp = read_json(cdir / "campaign.json")
    if camp["mock"] != mock:
        raise SystemExit("a campaign scheduled with --mock must be run with --mock, and a live one without it")
    g = git_state()
    if not mock and camp["split"] == "main" and (g["commit"] != camp["git"]["commit"] or g["dirty"]):
        raise SystemExit(f"main must run from the exact frozen commit {camp['git']['commit']}; now at {g}")
    gw = Gateway(cfg, None if mock else secret("ANTHROPIC_API_KEY"), None if mock else secret("EXA_API_KEY"), mock=mock)
    if not mock and not (gw.anthropic_key and gw.tool_key):
        raise SystemExit("ANTHROPIC_API_KEY and EXA_API_KEY must be set (see .env.example), or pass --mock")
    budgets = {"anthropic": Budget("anthropic", cfg, mock), "exa": Budget("exa", cfg, mock)}
    gw.start()
    blocks: dict[int, list[dict]] = {}
    for row in manifest:
        blocks.setdefault(row["block"], []).append(row)

    def do_block(rows: list[dict]) -> list[str]:
        out = []
        for row in sorted(rows, key=lambda r: r["position"]):
            if only and row["system"] not in only:
                out.append("deferred")  # staged execution of the same frozen manifest; run again without the filter
                continue
            m = run_one(cfg, cdir, gw, row, tasks[row["task_id"]]["question"], budgets, camp)
            out.append(m["status"] if m else "not_dispatched_budget")
        return out

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            statuses = [s for b in ex.map(do_block, [blocks[k] for k in sorted(blocks)]) for s in b]
    finally:
        gw.stop()
    return dict(scheduled=len(manifest), statuses={s: statuses.count(s) for s in sorted(set(statuses))},
                budget={k: v.status()["pools"] for k, v in budgets.items()})
