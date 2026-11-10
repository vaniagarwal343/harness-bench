#!/usr/bin/env python3
"""Put runs that the funding ceiling interrupted back in the queue, as a recorded schedule revision.

Used once, for amendments/0002. It selects runs only by their recorded status (funding_limit_interrupted);
it does not read reports, grades or costs to decide anything. The manifest does not change.

  python3 scripts/requeue_interrupted.py main --amendment amendments/0002-ceiling-and-interrupted-runs.md

Each interrupted run's files move to funding_interrupted/r<N>/ inside the campaign directory, so the run
starts again from scratch. Nothing is deleted and the spending ledger is not touched.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hb.util import git_state, read_json, read_jsonl, sha256_text, write_json  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign"); ap.add_argument("--amendment", required=True)
    a = ap.parse_args()
    cdir = ROOT / "runs" / a.campaign
    if subprocess.run(["pgrep", "-f", f"hb run {a.campaign}"], capture_output=True).returncode == 0:
        raise SystemExit("the runner for this campaign is still alive")
    g = git_state()
    if g["dirty"] or not any(t.startswith("freeze-") for t in g["tags"]) or not (ROOT / a.amendment).exists():
        raise SystemExit(f"run from the clean, tagged amendment commit that contains {a.amendment}; state: {g}")
    camp = read_json(cdir / "campaign.json")
    manifest = read_jsonl(cdir / "manifest.jsonl")
    n = len(camp.get("revisions", [])) + 1
    dest = cdir / "funding_interrupted" / f"r{n}"
    moved, spend, tool = [], 0.0, 0.0
    for r in manifest:
        d = cdir / "outputs" / r["run_id"]
        if not (d / "meta.json").exists():
            continue
        m = read_json(d / "meta.json")
        if m["status"] != "funding_limit_interrupted":
            continue
        cost = sum(l["cost_usd"] for l in m["attempt_ledgers"]); tcost = sum(l["tool_cost_usd"] for l in m["attempt_ledgers"])
        moved.append(dict(run_id=r["run_id"], cost_usd=round(cost, 4), tool_cost_usd=round(tcost, 4), report_chars=m.get("report_chars", 0)))
        spend, tool = spend + cost, tool + tcost
        dest.mkdir(parents=True, exist_ok=True)
        d.rename(dest / r["run_id"])
    camp.setdefault("revisions", []).append(dict(
        revision=n, applied=time.strftime("%Y-%m-%dT%H:%M:%S%z"), amendment=a.amendment, git=g,
        manifest_sha256=sha256_text((cdir / "manifest.jsonl").read_text()), kept_runs=len(manifest),
        removed_runs=[], removed_spend_usd=0.0, removed_tool_spend_usd=0.0, operator_stopped_runs=[], operator_stopped_spend_usd=0.0,
        funding_interrupted_runs=moved, funding_interrupted_spend_usd=round(spend, 4), funding_interrupted_tool_spend_usd=round(tool, 4)))
    write_json(cdir / "campaign.json", camp)
    for m in moved:
        print(m)
    print(f"revision {n}: {len(moved)} runs re-queued; ${spend:.2f} model and ${tool:.2f} research already spent on the interrupted attempts")


if __name__ == "__main__":
    main()
