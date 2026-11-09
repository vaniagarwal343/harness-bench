# Smoke results, spend and main-campaign estimate (private, 2026-11-09)

Source: `budget/anthropic.jsonl` and per-attempt traces under `runs/`. No open entries.

## Anthropic: $78.45 committed of the $310 ceiling, $231.55 remaining

| Item | USD | Kind |
|---|---|---|
| A closed-book, 2 smoke runs | 0.80 | study smoke |
| B plain loop, 2 smoke runs | 4.10 | study smoke |
| C GPT Researcher, 2 smoke runs after its patch | 6.92 | study smoke |
| D Deep Agents, 2 smoke runs under the old $4 cap (no report) | 7.54 | study smoke |
| D Deep Agents, 1 uncapped retest (report) | 9.02 | study smoke |
| Tests of a configuration that is no longer part of the study | 39.56 | discontinued |
| Stream bug: 4 calls dispatched, usage never metered, counted at full hold (upper bound) | 3.22 | infrastructure bug |
| Retry bug: 2 Deep Agents attempts that should not have run | 6.92 | infrastructure bug |
| C before its parser patch, 2 runs that gathered nothing | 0.37 | integration failure |

Exa $2.6 of $45. Jev $0.03 of $10. Astra: 8 CLI sessions.
Confirmed in the account per `configs/funding.local.toml`: $210, so $131.55 is spendable until the top-up is confirmed there.

## Measured cost and time per question

| Configuration | Model $ | Exa $ | Minutes | Conditions of the measurement |
|---|---|---|---|---|
| A | 0.37, 0.43 | 0 | 1.7, 1.8 | as it will run |
| B | 2.39, 1.71 | 0.18, 0.10 | 4.0, 3.7 | old settings: one run was shown "budget mostly used" notices; uncapped cost not measured |
| C | 3.16, 3.76 | 0.31, 0.31 | 4.8, 5.2 | old 150-call limit refused 26 of 176 research calls; uncapped cost not measured |
| D | 9.02 | 0.34 | 13.1 | uncapped, one run |

## Main-campaign projection, 15 questions x 4 configurations

| Basis | Per question | 15 questions | Against $231.55 |
|---|---|---|---|
| As measured | 14.93 | 224 | fits, $7.60 spare |
| B and C adjusted for removed limits (B 2.5, C 4.05) | 15.95 | 239 | short by about $8 |
| D 40% higher than its one measurement | 19.55 | 293 | short by about $62 |
| D 40% lower | 12.35 | 185 | fits, $46 spare |

Exa: about $0.9 per question, $13 for 15. Grading: Jev about $1; Astra about 60 CLI sessions for main.
