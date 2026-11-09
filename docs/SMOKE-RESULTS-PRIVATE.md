# Smoke results and spend reconciliation (private, 2026-11-09)

Source: `budget/anthropic.jsonl` and the per-attempt traces under `runs/smoke-aborted-1`, `runs/smoke2`, `runs/smoke3-gptr`. No open reservations.

## Anthropic spend: $36.46 committed of $310 (upper bound)

| Item | USD | Kind |
|---|---|---|
| A closed-book, 2 runs | 0.80 | benchmark smoke |
| B plain tools, 2 runs | 4.10 | benchmark smoke |
| C GPT Researcher, 2 runs after patch | 6.92 | benchmark smoke |
| D Deep Agents, first attempts, 2 runs | 7.54 | benchmark smoke |
| E Hyperresearch, 2 runs | 6.59 | benchmark smoke |
| Paid preflight | 0.00 | preflight |
| Compressed-stream bug: 4 closed-book calls dispatched, usage never metered, counted at full reservation | 3.22 | infrastructure bug (upper bound; true charge is probably about half) |
| Retry bug: second Deep Agents attempts that should not have run | 6.92 | infrastructure bug |
| C before its parser patch, 2 runs that gathered nothing | 0.37 | integration failure |

Benchmark smoke $25.95, bugs and integration failures $10.51. The four unmetered calls appear in the ledger once: their run entries are $0 and the adjustment entry carries the $3.22.

Exa $1.64 of $45. Jev $0.03 of $10. Astra: 8 CLI sessions.

## Research attempts

| Attempt | Spend | Research closed at | Spent after close | Stopped because | Final report |
|---|---|---|---|---|---|
| D q1 attempt 1 | 3.77 | 2.18 | 1.59 (12 calls) | next call refused: $0.23 left, 106 KB request | none |
| D q1 attempt 2 (retry bug) | 3.18 | 2.17 | 1.00 (8 calls) | finished | yes, 19.5k chars |
| D q2 attempt 1 | 3.77 | 2.86 | 0.91 (5 calls) | refused: $0.23 left, 130 KB request | none |
| D q2 attempt 2 (retry bug) | 3.74 | 2.47 | 1.27 (9 calls) | refused: $0.26 left, 131 KB request | none |
| E q1 (chose full tier) | 3.84 | 3.27 | 0.57 (6 calls) | refused from $3.27 on: 292 KB requests | none |
| E q2 (chose light tier) | 2.75 | not reached | n/a | refused at $2.75: 367 KB request, $1.25 unused | yes, 24.8k chars |
| C q1 | 3.16 | 150-call limit at 1.41 | 1.75 | finished | yes, 25.0k chars |
| C q2 | 3.76 | 150-call limit at 2.23 | 1.53 | finished | yes, 19.0k chars |
| B q1, q2 | 2.39, 1.71 | never | n/a | finished | yes |

q1 = rr-6053f2, q2 = rr-95846f.
