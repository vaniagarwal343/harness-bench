# Protocol, round 1

One model, five configurations, fifteen public questions. This file, `configs/study.toml` and `configs/splits.json` are frozen together at a tagged commit before any main output exists. Later changes are recorded in `amendments/`.

## 1. Questions

Using Claude Fable 5.2 (`claude-fable-5-2`) in every contestant system:

1. What can it answer from existing knowledge and reasoning, with no external tools?
2. What does ordinary tool-assisted research add?
3. What does a specialised research harness add beyond that?

## 2. Configurations

| | Configuration | What it is |
|---|---|---|
| A | Closed-book | One Messages API call in a fresh context. No search, browsing, retrieval, code execution, sub-agents or other tools. |
| B | Plain tools | A single-agent loop written in this repository over the Messages API, with two tools: `search` and `fetch`. No sub-agents, no research skill, no critic. |
| C | GPT Researcher | Its recursive "deep" mode at a pinned commit, every model role set to Fable 5.2. |
| D | Deep Agents | LangChain's `deep_research` example at a pinned commit, on Fable 5.2. |
| E | Hyperresearch | The Claude Code research pipeline at a pinned commit, every agent role set to Fable 5.2. |

Every outcome-affecting generative call is `claude-fable-5-2` at effort `high`: planners, researchers, summarisers, writers, critics. A local gateway sits between every contestant and the model. It refuses any other model, any request for server-side fallback, and any server-side web tool, and it logs the model and effort that actually served each call. Where a harness cannot set effort itself, the gateway sets it and records that on the call.

B, C, D and E share one research backend through the same gateway: Exa search (`type: auto`, up to 5 results, a 500-character raw-text snippet per result) and Exa Contents raw page text, cut at 30,000 characters with a visible marker. Exa highlights, summaries and answers are model-generated and are never requested. There is no fallback backend. URLs naming the benchmark are filtered out.

Each harness keeps its own research strategy. The adaptations needed to run it under these controls are listed in `docs/COHORT.md`, and C, D and E are reported as modified configurations. E also runs inside the Claude Code runtime, with that runtime's system prompt, built-in file and shell tools and context handling; that is a runtime difference, not orchestration.

What the comparisons mean: B minus A is the benefit of ordinary tool-assisted research at this budget. C minus B, D minus B and E minus B are the additional benefit of each harness as configured here, with model, effort, research access and budget held equal. Prompts, loop structure, runtime and context handling differ between arms, so these are not pure orchestration effects.

## 3. Tasks

ResearchRubrics, `ScaleAI/researchrubrics` at revision `85de3115053d1453ed612caacf4a405edc1ad756` (MIT), file hash pinned in the config. 101 questions with weighted rubric criteria and no reference answers.

- **Main: 15 questions, one run per configuration, 75 scored outputs.** A seeded sampler first drew two questions from each of the dataset's ten domains. The main set is a seeded stratified subset of those 20: every domain keeps one question and five domains keep both. Both draws were made before any main output existed. 399 rubric criteria in total.
- **Smoke: 2 further questions, 10 outputs, unscored.** They check integrations, output extraction, grading and cost. They establish operational feasibility only. They say nothing about statistical power, grading accuracy or task coverage.
- 17 distinct questions and 85 outputs before any retry. Smoke and main are disjoint. Task ids are in `configs/splits.json`, and `scripts/reproduce_selection.py` re-derives them from the seed.
- Questions are used verbatim. No date is changed. Several main questions contain time-sensitive wording; they are flagged in the task strata. A tool-using system may find facts newer than the rubric expects; this is reported, not corrected.
- No question is removed because the closed-book model answers it well. The exclusion list is empty.
- The dataset was public before the model's training cutoff. A high closed-book score does not show the model saw the rubric, and a low one does not show it did not.

## 4. Budgets and their enforcement

| | Cap per run |
|---|---|
| Closed-book | $1.00 of model tokens at list price. Output allowance 20,000 tokens. |
| Research run (B, C, D, E) | $4.00 of model tokens, covering every call and sub-agent. Research tools stop at $3.00 so $1.00 remains for the report. Also 150 research calls, $0.60 of Exa spend, 60 minutes. |

Total model funding for the round is $310: main 15 × ($1 + 4 × $4) = $255, smoke 2 × $17 = $34, and $21 for paid preflight, retries and contingency. These are affordability limits, not measured costs.

Enforcement, in two layers:

- **Per call.** Before a request is sent, the gateway reserves its worst case: a conservative input estimate at the highest input rate plus the full `max_tokens` at the output rate. If the remaining budget cannot cover at least 1,024 output tokens the call is refused without being sent. If it can cover less than was asked, `max_tokens` is lowered and that is logged. Concurrent calls from sub-agents wait for in-flight reservations to settle. After the response the reservation is replaced by the metered cost, including thinking and cache charges. Spend plus reservations therefore never exceeds the cap.
- **Per campaign.** A persistent ledger reserves a run's full cap from its pool (smoke, main or buffer) before the run is dispatched and reconciles it afterwards. Pools cannot borrow from each other, and total commitments cannot exceed the funding confirmed to be in the account. A run that cannot be reserved is not started and is reported as missing.

Only an infrastructure failure is retried, once, paid from the buffer pool. A budget stop, a refusal or a timeout is an outcome. Every attempt is costed.

If a configuration cannot produce useful reports inside these caps, the smoke test will show it. The response is to report the measurement and the smallest configuration change that would fix it, as an amendment decided before the main run. Tasks are not dropped, a configuration is not removed, grading is not weakened and funding is not exceeded.

## 5. Execution

- Each run gets a fresh working directory, a fresh home directory, a one-time token and an environment with no provider or evaluator credentials. The run path reads question text only; rubrics are stored separately and opened only by grading commands.
- A block is one question. Block order and the order of configurations inside a block are seeded-random, so configurations are interleaved in time.
- A report exists only if the system produced one as its final output. Run status is one of: completed, partial, refused, budget_exhausted, timeout, policy_violation, failed_infra.
- Every scheduled run counts. A run with no report scores zero by rule and is listed. Nothing is filtered after the fact.
- Each run records the full commit SHA, the config hash, prompt hashes and the installed harness versions. The main campaign can only be scheduled and run from a clean commit carrying a `freeze-` tag.

## 6. Scoring

For each report, ResearchRubrics' released formula: sum of weights of criteria judged met, divided by the sum of positive weights, with binary verdicts and no clipping. A negative-weight criterion describes an error: "met" means the error is present and its negative weight is added.

Reported separately, never merged:

- **The original total**, and the same formula restricted to criteria that do not depend on citations (`adapted_no_citation`) and to those that do (`adapted_citation_dependent`). This matters most for the closed-book arm, where a missing citation is not missing knowledge.
- **Rubric compliance is not verified factual accuracy.** Judges decide whether the report text satisfies each criterion. Nobody checks the report's statements against the world.
- **Citation presence is not citation support.** Rubric citation criteria check that sources are cited. Whether a cited page supports the claim is not checked in this round, and no faithfulness claim is made.
- **A grader failure is not a wrong answer.** A judgment that errored, or that both judges could not resolve, is carried as unresolved and shown as a range.

The judges, the binary scheme and single-pass judging differ from the dataset's own evaluation, so these scores are not comparable to its published leaderboard.

## 7. Grading

Two judges grade every criterion of every report independently, from the same input: the question and the full report text. Nothing is truncated.

- **GPT-6 Astra**, run through the official Codex CLI at a pinned version on a signed-in account, with the model and reasoning effort passed explicitly. Each report is judged in one fresh, non-persisted session: user configuration and rules are not loaded, the working directory is empty, the prompt arrives on standard input, and shell, web search, apps, plugins, browser and delegation features are switched off. After each session the event log is inspected, and a session in which any tool was used or offered is discarded as an error. Astra returns, per criterion, a verdict, the exact report passages it relied on and a short reason. Code checks that every quoted passage occurs in the report.
- **Jev** (`jev-1.14.0`, pinned by version), one question per criterion, returning a label and its probability distribution, both stored.
- Astra runs first and its verdicts are written before Jev is called. Neither judge sees the other's output, the system that wrote the report, other reports, or rubric weights.
- Verdicts: met, not met, or cannot judge (the criterion depends on something outside the report text). "Cannot judge" is a verdict, not a failure: in a single-judge score it gives neither credit nor penalty, and in the two-judge view it is unresolved.
- Failure states (quota, budget, malformed output, context limit, quote not found, wrong model version, tool use, environment) are recorded as attempts. They are never converted into verdicts and never reused. On quota or credit exhaustion grading pauses; it does not switch to another billing route.
- If a report exceeds Jev's default context, the API returns an explicit error and the item is retried once in Jev's long-context mode, which is recorded per judgment.

Whether Astra judges a report's criteria together or one at a time is decided once on development material (section 8) and then fixed for every system.

## 8. Grader validation and manual review

- **Fixed public check.** RuVerBench provides human yes/no labels for sampled ResearchRubrics criteria on reports written by other systems. From records whose question is not in this study, a seeded draw fixes a development set of 10 reports (31 labelled points) and a held-out set of 30 reports (97 points). Prompts, the batching choice and any confidence threshold are settled on development material only. The held-out set is scored once. Reported per judge: false passes, false failures, unresolved, balanced accuracy, and the same where both judges agree.
- **Batching check.** Astra judges the development set both ways, and two smoke reports both ways. Batching is kept only if the two modes agree on at least 90% of the smoke items and batching is not more than 5 points worse in balanced accuracy on the development set.
- **Diagnostics.** Fifteen hand-written cases with known answers: paraphrase, padding, wrong number, omitted qualification, instructions addressed to the grader, and error criteria.
- **Manual review.** A blinded queue of at most 60 items from the main outputs (of roughly 2,000 judgments): up to 40 unresolved judgments, heavy and error criteria first, and 20 randomly chosen agreements. Reviewed labels replace the range for those items. Everything else stays unresolved.

Limits. One hundred held-out points give wide intervals. The human labels are on other systems' reports, not Fable's. Agreement between two judges is not correctness; the random agreements in the review queue give the only direct estimate of that on these outputs. Jev's probabilities describe single judgments: they are not a measure of how stable a verdict is under repetition and are not correctness rates. Re-judging stability is not measured in this round.

## 9. Analysis

- Four prespecified paired comparisons: B minus A, C minus B, D minus B, E minus B.
- The unit is the question. For each comparison: the mean of the 15 paired differences, a 95% percentile bootstrap interval over questions (10,000 draws, fixed seed), wins, losses and ties, and a paired sign-flip permutation p-value with a Holm adjustment across the four comparisons.
- Each comparison is shown under Jev alone, Astra alone, and the mean of the two. Unresolved judgments are handled by bounding each score, and the difference is bounded by pairing opposite ends: from (first system's lower bound minus baseline's upper bound) to (first system's upper bound minus baseline's lower bound).
- A direction is called supported only if the interval excludes zero with the same sign under each judge alone and under the unfavourable end of that bound. Otherwise the estimate and interval are reported without a directional claim. No claim of equivalence is made.
- There is one run per configuration and question, so variation between repeated runs of the same configuration is not estimated, and differences on a single question are not interpreted.
- Fifteen questions is a small sample. Intervals will be wide: only a large and consistent difference can be distinguished from zero, and a difference that is not distinguished from zero is not evidence that two systems are equal. With 15 pairs the smallest attainable sign-flip p-value is about 0.00006, but a typical moderate effect will not reach conventional thresholds after adjusting for four comparisons. Results are reported as estimates with intervals and per-question tables first, and directional claims only where the rule above is met.
- Fifteen questions from one dataset do not support a general ranking of harnesses, and domain-level breakdowns (one or two questions per domain) are descriptive only.
- Also reported per configuration: runs scheduled, runs with a report, status counts, cost, tokens, model calls, research calls, Exa spend, wall time and report length, plus a table with every question's score and status.

`results.json` is built from the attempt logs and `RESULTS.md` is rendered from it. `python -m hb verify` rebuilds both and fails on any difference, so every reported number comes from the same versioned artifact.
