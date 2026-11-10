# Amendment 0001: main set reduced from 15 to 12 questions

- **Date:** 2026-11-09
- **Tag:** `freeze-r1-a1` (the original `freeze-r1` tag is unchanged)
- **Decided by:** the study owner, for affordability.

## What changed
The main study is the 12-question set recorded as `main_12_fallback` in `configs/splits.json`, across the same four configurations, one run each: 48 main outputs instead of 60. The three questions no longer in the main set are `rr-605371`, `rr-6053bb` and `rr-9af31b`.

Nothing else changed: the model and effort, the research tools and their settings, the absence of per-run dollar caps, the cumulative funding ceiling, the prompts, both judges and their settings, the scoring, the analysis and the manual review plan are as frozen at `freeze-r1`. `configs/study.toml`, `configs/splits.json` and the prompt files are byte-identical to `freeze-r1`. Where `docs/PROTOCOL.md` and `README.md` say fifteen questions or 60 outputs, read twelve and 48; the analysis takes the number of questions from the campaign manifest. With 12 pairs the smallest attainable two-sided sign-flip p-value is about 0.0005, and intervals are wider than they would have been with 15.

## Why
The measured cost of GPT Researcher on the first main questions was about three times its smoke-test cost, so the 15-question campaign was projected to reach the cumulative model-spending ceiling before finishing. Reducing the question count keeps the ceiling and keeps every run uncapped.

## How the 12 were chosen
The set was drawn by a seeded, domain-stratified procedure and written into `configs/splits.json` before the freeze and before any main output existed (`selected_before_any_main_output`). It is used here exactly as recorded. No score, grade or observed cost was used to choose which questions stay: no main output had been graded when this amendment was made.

## What existed when this was made
Main generation had started under `freeze-r1`. At the time of writing, runs had finished on four questions, two of which (`rr-6053bb`, `rr-9af31b`) are outside the retained set; `rr-605371` had not started. No main output had been graded and no aggregate had been looked at. Per-run cost and completion status had been read for a health check.

- Finished runs on retained questions are kept as they are and are not run again.
- Finished runs on the two questions outside the retained set are preserved under the campaign's `excluded/` folder. They are not graded and not scored. Their cost stays in the spending ledger and is reported separately from run costs.
- Changing the queue required stopping the runner. Runs in flight at that moment have no terminal record; their partial files are preserved under `operator_stopped/`, their cost stays in the ledger and is reported separately, and those on retained questions are run again from scratch.
- The exact lists and amounts are in the `revisions` entry of the campaign record, written by `scripts/revise_schedule.py`.

## Schedule revision
Revision 1 of the `main` campaign manifest: the original 60-row manifest is kept as `manifest.r0.jsonl`; the revised manifest is its 48 rows on retained questions, with block and position order unchanged. Runs before the revision were made at the `freeze-r1` commit and later runs at the `freeze-r1-a1` commit; the runner and `hb verify` accept exactly those two commits for this campaign.

## Code changed with this amendment
Bookkeeping only: `hb/runner.py` and `hb/verify.py` (accept the recorded amendment commit; check the revision), `hb/analysis.py` (list the revision and its separate costs in the results), `scripts/revise_schedule.py` (new), one offline test.
