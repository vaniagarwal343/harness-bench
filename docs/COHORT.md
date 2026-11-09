# Configurations and adaptations

Evidence levels used below. **Read**: established from source at the pinned commit. **Mock-run**: the adapter ran end to end against this repository's gateway with a fake model and fake search backend, which exercises routing, model pinning, shared tools and accounting but says nothing about behaviour with the real model. **Live**: run against real Fable 5.2. Nothing has been run live yet.

| | A closed-book | B plain tools | C GPT Researcher | D Deep Agents |
|---|---|---|---|---|
| Source | this repo, `hb/systems.py` | this repo, `hb/systems.py` | github.com/assafelovic/gpt-researcher | github.com/langchain-ai/deepagents, `examples/deep_research` |
| Commit | n/a | n/a | `ddd4db36208d9c8e49f3b2436efda97baecb5433` | `d315d8e12dd4f2c250a759a38825f1ea1c9484d6` |
| License | n/a | n/a | Apache-2.0 | MIT |
| Runtime | Anthropic Python SDK | Anthropic Python SDK | Python 3.12, LangChain 1.4.9, langchain-anthropic 1.7.10 | Python 3.12, LangGraph 1.2.20, langchain-anthropic 1.7.10 |
| Strategy | one answer | search, read, iterate, write | recursive tree, breadth 3 and depth 2: a sub-researcher per query, learnings and follow-ups, then one report | orchestrator delegating to research sub-agents (up to 3 in parallel, 3 rounds), writes a final report file |
| Model roles | 1 | 1 | strategic, smart, fast: all set to Fable 5.2 | one model object, inherited by sub-agents and summarisation |
| Research access | none | `search`, `fetch` | custom retriever endpoint on the gateway | its search tool, re-pointed at the gateway |
| Human checkpoints | none | none | none | none |
| Context handling | n/a | append-only history, prompt caching | keyword filter on fetched text, 25k-word trim | summarisation at 85% of context, history offloaded to files |
| Evidence | mock-run | mock-run | read, mock-run | read, mock-run |

Full dependency pins are in `adapters/gptr.lock.txt` and `adapters/deepagents.lock.txt`.

## Adaptations

### C. GPT Researcher (modified configuration)
Set by environment in `configs/study.toml`, plus one one-line source patch.
1. `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` set to `anthropic:claude-fable-5-2` (shipped defaults are OpenAI models).
2. `CONTEXT_FILTER=keyword`, stated explicitly. The shipped default `auto` switches to a third-party scoring model when its API key is present in the environment. The child environment is scrubbed of all provider keys as well.
3. `RETRIEVER=custom`, pointing at the gateway. Each query becomes one search and one page read per result on the shared backend. This replaces GPT Researcher's own scraper with the common page reader, which is the price of equal research access.
4. Token limits raised (16k, 16k, 20k, 8k) because the model always thinks and thinking shares the output allowance.
7. One-line source patch (`adapters/patches/gptr-anthropic-text.patch`): responses from the Anthropic provider are read through the text view, which upstream already uses for another Anthropic-compatible provider. Without it, thinking blocks break its parsers and it gathers nothing (observed in the smoke test).
5. Driven through the Python API, so the command-line tool's title-generation call is not made.
6. Effort is set by the gateway (`pin_effort`), since the harness has no setting for it.
Breadth 3, depth 2 and concurrency 4 are the shipped defaults and are kept.

### D. Deep Agents `deep_research` (modified configuration)
`adapters/deepagents_driver.py` rebuilds the example's agent from its own prompts, limits and `think_tool`.
1. Model changed from `claude-sonnet-4-5` at temperature 0.0 to Fable 5.2 with no sampling parameters.
2. The search tool keeps its upstream name (`tavily_search`, which the example's prompts refer to), its signature and its "search, then return full page text" behaviour, but both steps go through the gateway to the shared backend.
3. `max_tokens` 20,000 per call (the largest the client library permits for non-streamed calls). Effort set by the gateway.
4. The report is read from the agent's `/final_report.md`; if the orchestrator answers inline instead, the last message is used and that is logged.

### Differences that remain between arms
- Report-writing prompts are each system's own. B's prompt is in `prompts/`.
- C reads 5 pages per query by design; D reads 1; B decides for itself.
- C makes uncached calls; B, D and E use prompt caching. This affects cost, which is reported per system.
- D edits its message history when it summarises. C runs many independent short calls.

## Not in round 1: Hyperresearch (integrated and smoke-tested, then dropped on cost)

Kept here as a record of the integration. The adapter and patch remain in the working repository and are not part of the frozen study.

| | |
|---|---|
| Source | github.com/jordan-gibbs/hyperresearch at `f06271f09af3a63229b6487b606ba3142114fe94` (v0.13.0, MIT), plus `adapters/patches/hyperresearch-gateway.patch` |
| Runtime | Claude Code CLI 2.1.321, headless (`claude -p "/hyperresearch <question>"`), Python 3.12 |
| Strategy | A router skill runs a staged pipeline. It classifies each question as `light` (5 steps: decompose, source sweep, single draft, polish, readability) or `full` (16 steps, adding contradiction and loci analysis, depth investigation, three drafts, synthesis, four critics, gap fetch, patching, citation check). 16 sub-agent definitions. |
| Model roles | 13 profile roles across 16 agents, shipped as `sonnet` and `opus`. All set to Fable 5.2. |
| Human checkpoints | Only in its browser lane, which is switched off. |

Adaptations:
1. **Models.** Every role in the profile's model map is set to `claude-fable-5-2` and the agents are re-rendered; the driver refuses to start unless all 16 agent files carry that model. The session model and Claude Code's model aliases are set to the same id, and the gateway refuses anything else.
2. **Search.** Upstream relies on Claude Code's built-in WebSearch, which runs on the provider's servers and cannot be shared with the other arms. WebSearch and WebFetch are denied. The patch adds a `hyperresearch websearch` command, and one short note appended to every agent and skill says to use it.
3. **Page reading.** The patch adds a web provider that sends `hyperresearch fetch` and `websearch` to the study gateway, so E uses the same Exa search, page reader, content limit, block list and budget as B, C and D. Upstream's default reader is a local headless browser.
4. **No direct network.** The session runs in a macOS sandbox that permits network access to the local gateway only. Upstream's academic-API, PDF-download and open-access-rescue lanes therefore fail and fall back to search and fetch. This narrows its sourcing and is the price of equal research access.
5. **Isolation.** Fresh working directory, home and Claude Code configuration directory per run. The sandbox also denies reading the operator's own Claude Code and Codex folders and the study workspace, so no personal skills, agents, memory or study files can load. The driver checks the session's start-up record and fails the run if any personal skill, agent or MCP server appears. Claude Code's bundled `deep-research` skill is denied.
6. Effort is set by the gateway (Claude Code asked for `medium`; the study pins `high`).
7. The report is the pipeline's own deliverable, `research/notes/final_report_<tag>.md`, with the vault's front matter removed. Drafts and notes are not promoted to a report.

Tier choice (`light` or `full`) is left to the pipeline, as upstream.
