# harness-bench

Round 1 of a same-model comparison of deep-research systems. Claude Fable 5.2 runs in five configurations on 15 public ResearchRubrics questions, and the reports are graded against the dataset's existing rubrics by two independent judges.

- **A** closed-book, **B** a plain single-agent tool loop, **C** GPT Researcher, **D** LangChain Deep Agents, **E** Hyperresearch (Claude Code).
- 15 scored questions x 5 configurations = 75 outputs, one run each. Two further questions are unscored smoke tests.
- B, C, D and E share one search backend and page reader (Exa) and one per-run budget.

Read `docs/PROTOCOL.md` for the design, `docs/COHORT.md` for how each system is configured and adapted, `docs/RUNBOOK.md` for the commands, and `docs/FREEZE.md` for how the protocol is frozen and amended.

## Layout

```
configs/study.toml     frozen configuration; its hash is stamped on every artifact
configs/splits.json    the 15 main and 2 smoke task ids
hb/gateway.py          local proxy: model pinning, shared research tools, reserve-before-dispatch budget, trace
hb/budget.py           persistent campaign ledger with pools
hb/systems.py          configurations A and B, and the child-process runner for C and D
hb/runner.py           manifest, block randomisation, isolated runs
hb/grading.py          two judges, failure states, consensus
hb/astra_cli.py        GPT-6 Astra judging through the Codex CLI
hb/analysis.py         scoring, paired statistics, results.json and RESULTS.md
hb/validate.py         grader check against public human labels, diagnostics
hb/verify.py           offline invariants
adapters/              drivers, patch and pinned installs for C, D and E
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

- Nothing has been run against the real model, the real search backend or the real judges yet.
- Scores are rubric compliance judged from report text, not verified factual accuracy.
- Whether a cited source supports a claim is not checked in this round.
- Contestant and judge prompt texts are not published here; their hashes are in `PROMPT_HASHES.json`. Without the texts the runs cannot be reproduced exactly.
- Fifteen questions from one dataset, one run each: a small sample that does not rank research harnesses in general.
