# Amendment 0002: funding ceiling raised; runs it interrupted are run again; per-call output limit disclosed

- **Date:** 2026-11-09
- **Tag:** `freeze-r1-a2` (`freeze-r1` and `freeze-r1-a1` are unchanged)
- **Decided by:** the study owner.

## What changed
1. The cumulative model-spending ceiling for the round is raised from $310 to $355 (`configs/funding_amendments.toml`). There are still no per-run dollar caps.
2. The eight main runs that the $310 ceiling interrupted are run again from scratch, once each, under the same frozen settings. They are selected by their recorded status only:
   `rr-6053c9` closed-book and plain loop; `rr-60543e` plain loop and GPT Researcher; `rr-60542a` all four configurations.

Nothing else changed: the 12 questions, the model and effort, the research tools, the prompts, both judges, the scoring, the analysis and the manual review plan are as before. `configs/study.toml`, `configs/splits.json` and the prompt files are byte-identical to `freeze-r1`.

## Why
The frozen protocol scores a run that left no report as zero. Applied here, that would give two configurations zeros on two or three questions because the money ran out in the order the schedule happened to reach them, which says nothing about answer quality. The owner funded the remaining runs instead.

## What existed when this was made
Generation had stopped at the ceiling with 40 of 48 runs complete. No main output had been graded and no score had been computed or looked at. Completion status, cost, and stop reasons in the call logs had been read for health checks. The interrupted attempts are preserved under the campaign's `funding_interrupted/` folder; they are not graded or scored. Their cost stays in the spending ledger and is reported separately from run costs. The first attempt of these eight runs is the only reason they are repeated; no completed run is repeated.

## Disclosed limitation: per-call output limit in configurations C and D
GPT Researcher and Deep Agents make non-streamed model calls, for which the client library allows at most about 20,000 output tokens, shared between thinking and visible text (`configs/study.toml`). The closed-book and plain-loop configurations stream and have 64,000. Some C and D calls stopped at that limit; in at least one GPT Researcher run the final report is cut off inside its reference list, and in some Deep Agents runs calls that ended at the limit were repeated by the harness and added cost. By the owner's decision these runs stand as produced and are not repeated; reports are graded as written. The runs with a call that stopped at the limit are identifiable from `stop_reason` in each run's call log and will be listed with the results.

## Schedule revision
Revision 2 of the `main` campaign: the manifest is unchanged (48 rows). The eight interrupted runs' files were moved aside so that they run again. Runs made after this revision are at the `freeze-r1-a2` commit.

## Code changed with this amendment
Bookkeeping only: `hb/util.py` (reads the amended ceiling), `hb/verify.py` and `hb/analysis.py` (list this revision and its separate costs), `scripts/requeue_interrupted.py` (new).
