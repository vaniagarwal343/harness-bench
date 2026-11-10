# Amendment 0003: AI-labelled review queue reported as a side analysis only

- **Date:** 2026-11-10
- **Tag:** `freeze-r1-a3` (`freeze-r1`, `freeze-r1-a1` and `freeze-r1-a2` are unchanged)
- **Decided by:** the study owner.

## What changed
1. The 60-item blinded review queue (40 unresolved judgments, 20 sampled agreements) was labelled by an AI reviewer (Codex), not by a person. The label file describes itself as "AI-assisted, not human adjudication".
2. Those labels are **not** entered as adjudication. `grades/adjudication.jsonl` is not written, and `results.json` and `RESULTS.md` are unchanged: every judge disagreement stays unresolved in the headline results, and the frozen claim rule is applied to those.
3. A side analysis is added (`scripts/sensitivity_ai_review.py`). It recomputes the same analysis in a temporary copy of the campaign in which the AI labels stand in for adjudication, and writes `sensitivity_ai_review.json` and `SENSITIVITY-ai-review.md` beside the headline results. It is labelled "with a third AI pass" wherever it is reported.

## Why
The protocol plans a manual review. An AI pass is not one, and this reviewer runs on the same model route as the Astra judge, so it is not independent of that judge: on the 35 items where the two judges gave opposite met / not-met verdicts it sided with Astra 28 times and with Jev 7 times. Entering its labels as adjudication would let one judge's family break its own ties. Reporting them separately keeps the information without changing what the headline results claim.

## Order of events
- All 48 main outputs existed and were graded, and the headline results had been built and verified, before the labels were made.
- The decision to report the labels as a side analysis only was made before their effect on the results was computed.
- The reviewer worked from the blinded queue and report texts. Its label file states that system identities and the judges' verdicts were not consulted; that statement was not independently checked.

## Limits
- The side analysis is not evidence about which judge is right.
- Four items were labelled "cannot judge" and stay unresolved in the side analysis; 99 unresolved judgments were never queued.
- The manual review planned in the protocol has not been done. If a person later labels the queue, those labels are the adjudication, recorded in a further amendment.

Nothing else changed: questions, model and effort, research tools, prompts, judges, scoring and analysis are as before. `configs/study.toml`, `configs/splits.json` and the prompt files are byte-identical to `freeze-r1`.
