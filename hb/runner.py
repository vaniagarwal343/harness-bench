"""Schedule and execute contestant runs in fresh, isolated state.

Manifest: one row per scheduled run (task x system, one run each). A block is one task; block order
and the order of systems inside each block are seeded-random, so configurations are interleaved in
time and no system always runs first.

A run follows its workflow to completion. Nothing here limits what a run may spend. The gateway
writes every metered charge to the persistent ledger and refuses calls only at the round's
cumulative ceiling or when a hang/runaway safeguard trips; both are labelled on the run.

This module reads data/public/tasks.jsonl only. It never opens evaluator data.
"""
from __future__ import annotations

import fcntl
import importlib.metadata as md
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .datasets import load_public, load_splits
from .gateway import Gateway
from .systems import RUNNERS, prompt_hashes
from .util import git_state, now, read_json, read_jsonl, secret, sha256_text, write_json, write_jsonl

TERMINAL = ("completed", "partial", "refused", "funding_limit_interrupted", "safeguard_interrupted", "operator_stopped", "policy_violation", "failed_infra")


def build_manifest(cfg: dict, cdir: Path, split: str, systems: list[str], limit: int | None, mock: bool,
                   only_tasks: list[str] | None = None) -> list[dict]:
    mp = cdir / "manifest.jsonl"
    if mp.exists():
        raise SystemExit(f"{mp} exists. A manifest is frozen once written; use a new campaign name.")
    g = git_state()
    if split == "main" and not mock and cfg["freeze"]["require_clean_tagged_commit_for_main"]:
        if not g["commit"] or g["dirty"] or not any(t.startswith(cfg["freeze"]["tag_prefix"]) for t in g["tags"]):
            raise SystemExit(f"main can only be scheduled from a clean commit tagged {cfg['freeze']['tag_prefix']}*; state: {g}")
    tasks = load_public()
    ids = load_splits()["splits"][split][: limit or None]
    if only_tasks:  # smoke retests of single questions
        assert set(only_tasks) <= set(ids), "retest tasks must come from the chosen split"
        ids = [t for t in ids if t in only_tasks]
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


def run_one(cfg: dict, cdir: Path, gw: Gateway, sched: dict, question: str, camp: dict) -> dict:
    scfg = cfg["systems"][sched["system"]]
    rdir = cdir / "outputs" / sched["run_id"]
    if (rdir / "meta.json").exists():
        return read_json(rdir / "meta.json")  # a finished run is never silently re-run
    policy = dict(model=cfg["model"]["id"], effort=scfg.get("effort", cfg["model"]["effort"]),
                  allow_tools=scfg["kind"] != "closed_book", safeguards=dict(cfg["safeguards"]), shims=scfg.get("shims", []))
    ledgers, t0, result, attempts = [], now(), {}, 0
    # Retry rule: only an infrastructure failure is re-attempted, once. An interruption by the funding ceiling
    # or a safeguard is an outcome and is not retried. A harness's own internal retries are part of its run.
    while attempts <= cfg["execution"]["infra_retries"]:
        attempts += 1
        adir = rdir / f"attempt_{attempts}"
        adir.mkdir(parents=True, exist_ok=True)
        led = gw.open_run(adir, policy, category=camp["split"], key=f"{cdir.name}/{sched['run_id']}/a{attempts}")
        ta = now()
        try:
            result = RUNNERS[scfg["kind"]](question, scfg, led, gw, cfg, adir)
        except Exception as e:  # adapter bug: recorded as an infrastructure failure, not hidden
            result = dict(status="failed_infra", report="", error=f"{type(e).__name__}: {e}"[:500])
        finally:
            summ = gw.close_run(led)
            summ["attempt_wall_s"] = round(now() - ta, 2)
            ledgers.append(summ)
        if result["status"] != "failed_infra":
            break
    report = result.pop("report", "") or ""
    (rdir / "report.md").write_text(report)
    final = ledgers[-1]
    meta = dict(sched, status=result["status"], interrupted=final["interrupted"], attempts=attempts, result=result,
                wall_s=final["attempt_wall_s"], wall_s_all_attempts=round(now() - t0, 2),
                report_chars=len(report), report_sha256=sha256_text(report),
                ledger=final,  # the run's own cost: the attempt that produced its outcome, with all subagents and internal retries
                earlier_failed_attempts_cost_usd=round(sum(l["cost_usd"] + l["tool_cost_usd"] for l in ledgers[:-1]), 6),
                attempt_ledgers=ledgers, policy=policy, question_sha256=sha256_text(question),
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
    lock = open(cdir.parent.parent / "budget.run.lock", "w")
    try:  # one running campaign process at a time: in-flight holds against the ceiling live in this process
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another `hb run` is in progress; run campaigns one at a time so the spending ceiling holds")
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
            m = run_one(cfg, cdir, gw, row, tasks[row["task_id"]]["question"], camp)
            out.append(m["status"])
        return out

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            statuses = [s for b in ex.map(do_block, [blocks[k] for k in sorted(blocks)]) for s in b]
    finally:
        gw.stop()
    return dict(scheduled=len(manifest), statuses={s: statuses.count(s) for s in sorted(set(statuses))},
                ledger={k: {x: f.b.status()[x] for x in ("committed", "remaining_vs_ceiling", "remaining_vs_confirmed")} for k, f in gw.funds.items()})
