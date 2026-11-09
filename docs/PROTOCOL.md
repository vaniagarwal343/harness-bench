# Protocol, round 1

One model, four configurations, public questions with existing rubrics. This file, `configs/study.toml` and `configs/splits.json` are frozen together at a tagged commit before any main output exists. Later changes are recorded in `amendments/`.

## 1. Objective

Compare the answer quality, cost, latency and completion rate of each configured system when it follows its own workflow to completion, using Claude Fable 5.2 (`claude-fable-5-2`) in every system:

1. What can the model answer from existing knowledge and reasoning, with no external tools?
2. What does ordinary tool-assisted research add, and at what cost and latency?
3. What does a specialised research harness add beyond that, and at what cost and latency?

Cost and time are outcomes to be measured, not limits imposed in advance. Systems are not run at equal budgets: each spends what its workflow spends.

## 2. Configurations

Hyperresearch (Claude Code) was integrated and smoke-tested but is not part of this round: its full pipeline's cost per question on this model was several times that of the other systems combined, which the round's funding could not cover across the question set. That decision was made from smoke-test cost alone, before any scored output existed.

| | Configuration | What it is |
|---|---|---|
| A | Closed-book | One Messages API call in a fresh context. No search, browsing, retrieval, code execution, sub-agents or other tools. |
| B | Plain tools | A single-agent loop written in this repository over the Messages API, with two tools: `search` and `fetch`. No sub-agents, no research skill, no critic. |
| C | GPT Researcher | Its recursive "deep" mode at a pinned commit, every model role set to Fable 5.2. |
| D | Deep Agents | LangChain's `deep_research` example at a pinned commit, on Fable 5.2. |

Every outcome-affecting generative call is `claude-fable-5-2` at effort `high`: planners, researchers, summarisers, writers, critics. A local gateway sits between every contestant and the model. It refuses any other model, any request for server-side fallback, and any server-side web tool, and it logs the model and effort that actually served each call. Where a harness cannot set effort itself, the gateway sets it and records that on the call.

B, C and D share one research backend through the same gateway: Exa search (`type: auto`, up to 5 results, a 500-character raw-text snippet per result) and Exa Contents raw page text, cut at 30,000 characters with a visible marker. Exa highlights, summaries and answers are model-generated and are never requested. There is no fallback backend. URLs naming the benchmark are filtered out.

Each harness keeps its own research strategy. The adaptations needed to run it under these controls are listed in `docs/COHORT.md`, and C and D are reported as modified configurations.

What the comparisons mean: B minus A is the benefit of ordinary tool-assisted research. C minus B and D minus B are the difference each harness makes as configured here, with model, effort and research tools held equal. Spending is not held equal: a quality difference is reported next to the cost and time difference that came with it, and no quality-per-dollar score is computed. Prompts, loop structure, runtime and context handling differ between arms, so these are not pure orchestration effects.

## 3. Tasks

ResearchRubrics, `ScaleAI/researchrubrics` at revision `85de3115053d1453ed612caacf4a405edc1ad756` (MIT), file hash pinned in the config. 101 questions with weighted rubric criteria and no reference answers.

- **Main: 15 questions, one run per configuration, 75 scored outputs.** A seeded sampler first drew two questions from each of the dataset's ten domains. The main set is a seeded stratified subset of those 20: every domain keeps one question and five domains keep both. Both draws were made before any main output existed. 399 rubric criteria in total.
- **Smoke: 2 further questions, unscored.** They check integrations, output extraction, grading and cost. They establish operational feasibility only. They say nothing about statistical power, grading accuracy or task coverage.
- Smoke and main are disjoint. Task ids are in `configs/splits.json`, and `scripts/reproduce_selection.py` re-derives them from the seed.
- Questions are used verbatim. No date is changed. Several main questions contain time-sensitive wording; they are flagged in the task strata. A tool-using system may find facts newer than the rubric expects; this is reported, not corrected.
- No question is removed because the closed-book model answers it well. The exclusion list is empty.
- The dataset was public before the model's training cutoff. A high closed-book score does not show the model saw the rubric, and a low one does not show it did not.

## 4. Resources, safeguards and the funding ceiling

**No per-run resource limits.** There is no per-run dollar cap, no limit on the number of searches or page reads, no spending-triggered research cutoff, no reserved writing budget and no budget-driven reduction of output length. A run ends when its workflow ends.

What remains fixed for every research arm is the definition of the shared tools, not their quantity: Exa search returns up to 5 results with a 500-character raw snippet each, and the page reader returns up to 30,000 characters of a page with a visible marker when it cuts. Each system's own documented settings are kept (for example GPT Researcher's breadth 3 and depth 2, Deep Agents' 3 parallel sub-agents and 3 rounds). Output allowances per call are set high enough not to bind: 64,000 tokens for A and B; 20,000 for C's report writer and D, the largest the client library permits for non-streamed calls.

**Safeguards against hangs and runaway loops.** These are not budgets and sit far above the use seen in smoke runs: 4 hours of wall time per run, 3,000 model calls, 5,000 research calls, 30 consecutive failed model calls. A run stopped by one is given the status `safeguard_interrupted` with the reason, is listed in the results, and is never presented as a normal completion.

**Cumulative funding ceiling.** Model spending for the whole round, including smoke tests, debugging and retries, cannot exceed $310. Every metered model call is written to a persistent ledger as it happens. Before a call is sent, its worst case (a conservative input estimate at the highest input rate plus its full output allowance) must fit under what remains; otherwise it is refused without being sent. A run stopped this way has the status `funding_limit_interrupted`. It is listed as such and is not treated as a naturally completed result. Exa and Jev spending have their own ceilings and ledgers.

**Retries.** A system's own internal retries and sub-agents are part of its run and its cost. The benchmark does not automatically re-run a failed run. A re-run after an infrastructure fault is an explicit, recorded decision under a new campaign name, and its cost is reported separately as infrastructure cost.

**Cost accounting.** For every run: model cost at list price from metered tokens (including thinking, cache writes and reads, failed calls and the system's internal retries), retrieval cost as reported by Exa, and their sum as generation cost. Reported separately from run costs: infrastructure debugging and failed integration attempts before the campaign, any infrastructure re-runs, and grading.

## 5. Execution

- Each run gets a fresh working directory, a fresh home directory, a one-time token and an environment with no provider or evaluator credentials. The run path reads question text only; rubrics are stored separately and opened only by grading commands.
- A block is one question. Block order and the order of configurations inside a block are seeded-random, so configurations are interleaved in time.
- A report exists only if the system produced one as its final output. Run status is one of: completed, partial (a final report exists but the system's process ended with an error), refused, funding_limit_interrupted, safeguard_interrupted, policy_violation, failed_infra.
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

- Three prespecified paired comparisons: B minus A, C minus B, D minus B.
- The unit is the question. For each comparison: the mean of the 15 paired differences, a 95% percentile bootstrap interval over questions (10,000 draws, fixed seed), wins, losses and ties, and a paired sign-flip permutation p-value with a Holm adjustment across the three comparisons.
- Each comparison is shown under Jev alone, Astra alone, and the mean of the two. Unresolved judgments are handled by bounding each score, and the difference is bounded by pairing opposite ends: from (first system's lower bound minus baseline's upper bound) to (first system's upper bound minus baseline's lower bound).
- A direction is called supported only if the interval excludes zero with the same sign under each judge alone and under the unfavourable end of that bound. Otherwise the estimate and interval are reported without a directional claim. No claim of equivalence is made.
- There is one run per configuration and question, so variation between repeated runs of the same configuration is not estimated, and differences on a single question are not interpreted.
- Fifteen questions is a small sample. Intervals will be wide: only a large and consistent difference can be distinguished from zero, and a difference that is not distinguished from zero is not evidence that two systems are equal. With 15 pairs the smallest attainable sign-flip p-value is about 0.00006, but a typical moderate effect will not reach conventional thresholds after adjusting for three comparisons. Results are reported as estimates with intervals and per-question tables first, and directional claims only where the rule above is met.
- Fifteen questions from one dataset do not support a general ranking of harnesses, and domain-level breakdowns (one or two questions per domain) are descriptive only.
- For every question and configuration: rubric score under each judge, model cost, retrieval cost, elapsed time and completion status. Per configuration: completion rate, mean, median and maximum cost and elapsed time, tokens, model calls, research calls and report length. Cost and latency are compared descriptively with the same paired layout; no significance claims are made about them.
- Runs interrupted by a safeguard or the funding ceiling are counted, scored by the no-report rule if they left no report, and listed by name.

`results.json` is built from the attempt logs and `RESULTS.md` is rendered from it. `python -m hb verify` rebuilds both and fails on any difference, so every reported number comes from the same versioned artifact.
