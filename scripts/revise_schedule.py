#!/usr/bin/env python3
"""Apply a recorded schedule revision to a campaign: keep only the questions in a pre-selected set.

Used once, for amendments/0001. It never looks at reports, grades or costs to decide anything: the
retained set is read from configs/splits.json, where it was fixed at the freeze.

  python3 scripts/revise_schedule.py main --keep-set main_12_fallback --amendment amendments/0001-main-12-questions.md

What it does, all inside the campaign directory:
  - keeps the previous manifest as manifest.r<N-1>.jsonl and writes the filtered manifest (block order unchanged);
  - moves outputs of removed runs to excluded/outputs/ (finished ones) and runs that were in flight when
    the runner was stopped to operator_stopped/ (no terminal record; they are run again from scratch if retained);
  - appends the revision to campaign.json with the amendment commit, hashes, and what had already been spent.
Nothing is deleted and the spending ledger is not touched.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hb.util import git_state, read_json, read_jsonl, sha256_text, write_json, write_jsonl  # noqa: E402


def trace_spend(run_dir: Path) -> tuple[float, float]:
    llm = tool = 0.0
    for tr in run_dir.glob("attempt_*/trace.jsonl"):
        for rec in read_jsonl(tr):
            if rec["type"] == "llm":
                llm += rec.get("cost_usd") or 0.0
            elif rec["type"] == "tool":
                tool += rec.get("cost_usd") or 0.0
    return llm, tool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign"); ap.add_argument("--keep-set", required=True); ap.add_argument("--amendment", required=True)
    a = ap.parse_args()
    cdir = ROOT / "runs" / a.campaign
    if subprocess.run(["pgrep", "-f", f"hb run {a.campaign}"], capture_output=True).returncode == 0:
        raise SystemExit("the runner for this campaign is still alive; stop it first")
    g = git_state()
    if g["dirty"] or not any(t.startswith("freeze-") for t in g["tags"]) or not (ROOT / a.amendment).exists():
        raise SystemExit(f"run from the clean, tagged amendment commit that contains {a.amendment}; state: {g}")
    keep_set = json.loads((ROOT / "configs" / "splits.json").read_text())[a.keep_set]
    keep = set(keep_set["tasks"])
    camp = read_json(cdir / "campaign.json")
    old = read_jsonl(cdir / "manifest.jsonl")
    assert keep <= {r["task_id"] for r in old}, "the retained set must come from the scheduled questions"
    n = len(camp.get("revisions", [])) + 1
    old_text = (cdir / "manifest.jsonl").read_text()
    (cdir / f"manifest.r{n - 1}.jsonl").write_text(old_text)
    removed, stopped, rs, rt, ss = [], [], 0.0, 0.0, 0.0
    for r in old:
        d = cdir / "outputs" / r["run_id"]
        if not d.exists():
            if r["task_id"] not in keep:
                removed.append(dict(run_id=r["run_id"], status="never_started"))
            continue
        done = (d / "meta.json").exists()
        llm, tool = trace_spend(d)
        if done and r["task_id"] in keep:
            continue
        if done:
            removed.append(dict(run_id=r["run_id"], status=read_json(d / "meta.json")["status"], cost_usd=round(llm, 4), tool_cost_usd=round(tool, 4)))
            rs, rt, dest = rs + llm, rt + tool, cdir / "excluded" / "outputs"
        else:
            stopped.append(dict(run_id=r["run_id"], retained=r["task_id"] in keep, cost_usd=round(llm, 4), tool_cost_usd=round(tool, 4)))
            ss, dest = ss + llm + tool, cdir / "operator_stopped" / f"r{n}"
            if r["task_id"] not in keep:
                removed.append(dict(run_id=r["run_id"], status="operator_stopped_in_flight"))
        dest.mkdir(parents=True, exist_ok=True)
        d.rename(dest / r["run_id"])
    new = [r for r in old if r["task_id"] in keep]
    write_jsonl(cdir / "manifest.jsonl", new)
    camp.setdefault("revisions", []).append(dict(
        revision=n, applied=time.strftime("%Y-%m-%dT%H:%M:%S%z"), amendment=a.amendment, git=g, keep_set=a.keep_set,
        keep_set_rule=keep_set["rule"], keep_set_seed=keep_set["seed"], tasks=sorted(keep),
        previous_manifest=f"manifest.r{n - 1}.jsonl", previous_manifest_sha256=sha256_text(old_text),
        manifest_sha256=sha256_text((cdir / "manifest.jsonl").read_text()), kept_runs=len(new), removed_runs=removed,
        removed_spend_usd=round(rs, 4), removed_tool_spend_usd=round(rt, 4),
        operator_stopped_runs=stopped, operator_stopped_spend_usd=round(ss, 4)))
    write_json(cdir / "campaign.json", camp)
    print(json.dumps(camp["revisions"][-1], indent=1))


if __name__ == "__main__":
    main()
