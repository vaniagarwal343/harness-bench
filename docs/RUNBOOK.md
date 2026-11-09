# Runbook

Order matters. Runs are not capped individually; the only spending limit is the round's cumulative ceiling, checked with `hb plan` and `hb budget`. Run one campaign at a time.

## 0. Setup (free)

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e . pytest
bash adapters/setup.sh all                 # pinned harness installs, each in its own environment
cp .env.example .env                       # fill in three keys
cp configs/funding.example.toml configs/funding.local.toml   # then enter confirmed balances
.venv/bin/python -m pytest -q              # offline tests
.venv/bin/python -m hb prepare             # pinned download, hash check, selection check
.venv/bin/python scripts/reproduce_selection.py
.venv/bin/python -m hb plan                # counts, caps and pool arithmetic
.venv/bin/python -m hb funding             # free checks; says what must be read in each provider console
```

Confirm in each provider's console that automatic top-up is off. Record what is actually available at each provider in `configs/funding.local.toml` (kept out of the frozen configuration).

## 1. Paid preflight (cents)

```bash
.venv/bin/python -m hb preflight --paid    # one small Fable call, one Jev call; confirms the model ids that answer
```

## 2. Smoke test (2 questions x 4 configurations, unscored)

```bash
.venv/bin/python -m hb schedule smoke --split smoke
.venv/bin/python -m hb run smoke --workers 2     # or stage it: --only closed_book, then --only plain_tools, ...
.venv/bin/python -m hb grade smoke --judges astra --limit 1    # first Codex CLI session: check it before continuing
.venv/bin/python -m hb grade smoke
.venv/bin/python -m hb results smoke && .venv/bin/python -m hb verify smoke
.venv/bin/python -m hb feasibility smoke
.venv/bin/python -m hb budget
```

After the first Astra session, open its `record.json` under `runs/smoke/grades/astra_sessions/` and confirm: no tool or command items, no tool catalog, no files created, the log names `gpt-6-astra`, and note the credit use it reports. Check the remaining Codex credits in the app before grading the rest.

Pass conditions:
1. `verify`: every call served by `claude-fable-5-2` at effort high, no policy violations, costs reconcile.
2. No HTTP 400 from model request rules in any `trace.jsonl`.
3. `feasibility`: every run finished on its own with a report; none interrupted.
4. Both judges returned verdicts with no error states left.
5. One report per configuration read by hand: it is the final answer, not notes.

The smoke questions are never scored and never used to tune contestant prompts. A single configuration can be re-tested on one question with `hb schedule <name> --split smoke --systems <system> --tasks <task id>`.

### Before the main campaign
Use the measured smoke costs to estimate the main campaign (`hb feasibility` per campaign, `hb plan` for what remains under the ceiling). If the estimate with contingency does not fit, the pre-selected 12-question set in `configs/splits.json` is used instead of 15, recorded as an amendment before any main output exists. Per-run caps are not reintroduced to make it fit.

## 3. Grader checks (Jev cents; Astra about 140 CLI sessions)

```bash
git clone https://github.com/THU-KEG/RuVerBench ../RuVerBench
git -C ../RuVerBench checkout 0af0256870d6322490c7ab15eb6370779b898a37
.venv/bin/python -m hb diagnostics
.venv/bin/python -m hb validate-graders --ruverbench ../RuVerBench --split dev
.venv/bin/python -m hb validate-graders --ruverbench ../RuVerBench --split dev --judges astra --astra-mode single
.venv/bin/python -m hb grade smoke --judges astra --astra-mode single --limit 2
.venv/bin/python -m hb compare-astra-modes smoke
#   decide batching by the written rule; adjust judge prompts only here; then freeze
.venv/bin/python -m hb validate-graders --ruverbench ../RuVerBench --split heldout
```

## 4. Freeze

```bash
git add -A && git commit -m "Freeze round 1 protocol, config and selection"
git tag -a freeze-r1 -m "Round 1 frozen before main outputs"
python3 scripts/make_export.py             # builds the public export for review; pushes nothing
```
Publishing the frozen export is described in `docs/FREEZE.md`.

## 5. Main (main question set x 4 configurations)

Requires a clean tree and the freeze tag. Check `hb plan` first: what remains under the ceiling should cover the estimate with contingency.

```bash
.venv/bin/python -m hb schedule main --split main
.venv/bin/python -m hb run main --workers 2
.venv/bin/python -m hb grade main
.venv/bin/python -m hb verify main          # before looking at any aggregate
.venv/bin/python -m hb results main
.venv/bin/python -m hb review-queue main
```

`run` and `grade` are resumable. If grading pauses on quota, fix the cause and rerun `grade main --retry-errors`. After manual review, put labels in `runs/main/grades/adjudication.jsonl` and rerun `results` and `verify`.
