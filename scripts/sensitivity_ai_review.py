"""Side analysis (amendment 0003): what the results look like if a third AI pass resolves queued items.

The review queue was labelled by an AI reviewer, not by a person. Those labels are NOT entered as
adjudication: grades/adjudication.jsonl is not written and results.json / RESULTS.md are untouched.
This script recomputes the same analysis in a temporary copy of the campaign in which the AI labels
stand in for adjudication, and writes the comparison next to the headline results:

    runs/<campaign>/sensitivity_ai_review.json
    runs/<campaign>/SENSITIVITY-ai-review.md

Usage: .venv/bin/python scripts/sensitivity_ai_review.py main [labels.json]
The labels file defaults to runs/<campaign>/grades/ai_review_labels.json.
"""
from __future__ import annotations

import collections
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hb import analysis, datasets  # noqa: E402
from hb.cli import campaign_dir  # noqa: E402
from hb.util import load_config, read_json, sha256_text  # noqa: E402

SKIP = {"results.json", "RESULTS.md", "grades", "sensitivity_ai_review.json", "SENSITIVITY-ai-review.md"}


def main(argv: list[str]) -> int:
    cfg = load_config(None)
    cdir = campaign_dir(cfg, argv[0])
    src = Path(argv[1]) if len(argv) > 1 else cdir / "grades" / "ai_review_labels.json"
    labels = read_json(src)
    key = {k["item"]: k for k in read_json(cdir / "review_queue.key.json")}
    # Identify the queue by its item columns only: the label and note columns may have been filled in since.
    with open(cdir / "review_queue.csv", newline="") as fh:
        q = [r[:9] for r in csv.reader(fh)][1:]
    queue_sha = hashlib.sha256(json.dumps(q, sort_keys=True).encode()).hexdigest()
    if sorted(int(r[0]) for r in q) != sorted(key) or sorted(r["item"] for r in labels["labels"]) != sorted(key):
        print("the labels, the queue and the key do not cover the same items")
        return 1
    if (cdir / "grades" / "adjudication.jsonl").exists():
        print("grades/adjudication.jsonl exists: the headline results already contain adjudication; this side analysis assumes none")
        return 1

    rows, align = [], collections.Counter()
    for r in labels["labels"]:
        k = key[r["item"]]
        lab = r["label"]
        if k["reason"] == "random_agreement":
            align["agreement items: same as both judges" if lab == k["jev"] else "agreement items: differs from both judges"] += 1
        elif {k["jev"], k["astra"]} == {"met", "not_met"}:
            align["disagreement items: sided with Astra" if lab == k["astra"] else
                  "disagreement items: sided with Jev" if lab == k["jev"] else "disagreement items: cannot judge"] += 1
        else:
            align["other unresolved items (a judge could not judge)"] += 1
        if lab in ("met", "not_met"):
            rows.append(dict(run_id=k["run_id"], cid=k["cid"], label=lab))

    tasks = datasets.load_with_rubrics()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / cdir.name
        (tmp / "grades").mkdir(parents=True)
        for p in cdir.iterdir():
            if p.name not in SKIP:
                (tmp / p.name).symlink_to(p)
        for p in (cdir / "grades").iterdir():
            (tmp / "grades" / p.name).symlink_to(p)
        (tmp / "grades" / "adjudication.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        alt = analysis.build_results(cfg, tmp, tasks)
    base = read_json(cdir / "results.json")

    def comp(res):
        out = {}
        for c in res["summary"]["comparisons"]:
            t = c["metric"]["total"]
            out[f"{c['system']} minus {c['baseline']}"] = dict(
                two_judge_mean=t["two_judge_mean"], worst_case=t["bound_lo"], best_case=t["bound_hi"], direction_supported=c["direction_supported"])
        return out

    def sysv(res):
        return {s: dict(v["total"], unresolved=v["grading"]["disagree"] + v["grading"]["cannot_judge"] + v["grading"]["unresolved_error"],
                        adjudicated=v["grading"]["adjudicated"]) for s, v in res["summary"]["systems"].items()}

    rv = labels.get("review", {})
    out = dict(
        note="Side analysis only. AI-assisted labels, not human adjudication. The headline results do not use them.",
        reviewer=rv.get("reviewer"), review_type=rv.get("review_type"), reviewer_limitations=rv.get("limitations"),
        labels_sha256=sha256_text(src.read_text()), queue_sha256=queue_sha, headline_results_sha256=sha256_text((cdir / "results.json").read_text()),
        labels=dict(collections.Counter(r["label"] for r in labels["labels"])), labels_applied=len(rows),
        alignment=dict(sorted(align.items())),
        comparisons=dict(headline=comp(base), with_ai_pass=comp(alt)), systems=dict(headline=sysv(base), with_ai_pass=sysv(alt)))
    (cdir / "sensitivity_ai_review.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")

    f = lambda x: f"{x:+.3f}"
    ci = lambda d: f"{f(d['mean'])} [{f(d['lo'])}, {f(d['hi'])}]"
    L = ["# Side analysis: queued items resolved by a third AI pass", "",
         "**This is not the manual review and not part of the headline results.** See `amendments/0003-ai-review-side-analysis.md`.", "",
         f"- Reviewer: {rv.get('reviewer')} ({rv.get('review_type')}). It runs on the same model route as the Astra judge, so it is not independent of that judge.",
         f"- Labels: {out['labels']}. The {len(rows)} met / not-met labels are applied here; cannot-judge labels leave the item unresolved.",
         "- The headline results (`RESULTS.md`) keep every judge disagreement unresolved and are unchanged.", "",
         "## How the labels line up with the judges", ""]
    L += [f"- {k}: {v}" for k, v in out["alignment"].items()]
    L += ["", "## Paired comparisons, total score (mean over questions, 95% bootstrap interval)", "",
          "The two-judge mean does not depend on adjudication and is identical in both rows. Only the worst-case and best-case bounds move.", "",
          "| comparison | version | two-judge mean | worst case for the first system | best case | direction supported under every view |",
          "|---|---|---|---|---|---|"]
    for name in out["comparisons"]["headline"]:
        for ver, lab in (("headline", "headline"), ("with_ai_pass", "with AI pass")):
            c = out["comparisons"][ver][name]
            L.append(f"| {name} | {lab} | {ci(c['two_judge_mean'])} | {ci(c['worst_case'])} | {ci(c['best_case'])} | {c['direction_supported']} |")
    L += ["", "## Total score by configuration: range across unresolved judgments", "",
          "| configuration | version | lower bound | upper bound | unresolved items | items resolved by the AI pass |", "|---|---|---|---|---|---|"]
    for s in out["systems"]["headline"]:
        for ver, lab in (("headline", "headline"), ("with_ai_pass", "with AI pass")):
            v = out["systems"][ver][s]
            L.append(f"| {s} | {lab} | {v['consensus_lo']:.3f} | {v['consensus_hi']:.3f} | {v['unresolved'] - v['adjudicated']} | {v['adjudicated']} |")
    L += ["", f"Labels sha256 `{out['labels_sha256'][:16]}`, queue sha256 `{queue_sha[:16]}`, headline results sha256 `{out['headline_results_sha256'][:16]}`.", ""]
    (cdir / "SENSITIVITY-ai-review.md").write_text("\n".join(L))
    print(f"wrote {cdir / 'sensitivity_ai_review.json'} and {cdir / 'SENSITIVITY-ai-review.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
