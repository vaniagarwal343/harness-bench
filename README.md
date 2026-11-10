# harness-bench

Round 1 of a same-model comparison of deep-research systems. Claude Fable 5.2 runs in four configurations on public ResearchRubrics questions, and the reports are graded against the dataset's existing rubrics by two independent judges.

- **A** closed-book, **B** a plain single-agent tool loop, **C** GPT Researcher, **D** LangChain Deep Agents.
- One run per question and configuration on the main set in `configs/splits.json`. Two further questions are unscored smoke tests.
- B, C and D share one search backend and page reader (Exa). Runs follow their workflow to completion; cost and time are measured outcomes, not caps.

**Amended after the freeze:** the main set is 12 questions (48 outputs), not 15. See `amendments/0001-main-12-questions.md`. The spending ceiling was later raised and eight interrupted runs repeated: `amendments/0002-ceiling-and-interrupted-runs.md`.

Read `docs/PROTOCOL.md` for the design, `docs/COHORT.md` for how each system is configured and adapted, `docs/RUNBOOK.md` for the commands, and `docs/FREEZE.md` for how the protocol is frozen and amended.

## Layout

```
configs/study.toml     frozen configuration; its hash is stamped on every artifact
configs/splits.json    the 15 main and 2 smoke task ids
hb/gateway.py          local proxy: model pinning, shared research tools, funding ceiling, safeguards, trace
hb/budget.py           persistent spending ledger with one cumulative ceiling per provider
hb/systems.py          configurations A and B, and the child-process runner for C and D
hb/runner.py           manifest, block randomisation, isolated runs
hb/grading.py          two judges, failure states, consensus
hb/astra_cli.py        GPT-6 Astra judging through the Codex CLI
hb/analysis.py         scoring, paired statistics, results.json and RESULTS.md
hb/validate.py         grader check against public human labels, diagnostics
hb/verify.py           offline invariants
adapters/              drivers, patch and pinned installs for C and D
tests/                 offline tests
```

## Credentials

Copy `.env.example` to `.env`. Three keys are needed: Anthropic (contestant model), Exa (research), TypeSafe (Jev judge). The Astra judge uses the Codex CLI's own signed-in session and takes no key. Contestant processes never receive any of these.

## Offline check

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m hb schedule demo --split smoke --mock
.venv/bin/python -m hb run demo --mock && .venv/bin/python -m hb grade demo --mock
.venv/bin/python -m hb results demo && .venv/bin/python -m hb verify demo
```

Mock output is labelled as mock and means nothing.

## Status and limits

- Before the freeze, every configuration and both judges were exercised on the two unscored smoke questions. No main-set question was run or graded before the freeze tag.
- Scores are rubric compliance judged from report text, not verified factual accuracy.
- Whether a cited source supports a claim is not checked in this round.
- Contestant and judge prompt texts are not published here; their hashes are in `PROMPT_HASHES.json`. Without the texts the runs cannot be reproduced exactly.
- Fifteen questions from one dataset, one run each: a small sample that does not rank research harnesses in general.
