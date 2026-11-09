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
Set by environment in `configs/study.toml`; no source patch so far.
1. `FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` set to `anthropic:claude-fable-5-2` (shipped defaults are OpenAI models).
2. `CONTEXT_FILTER=keyword`, stated explicitly. The shipped default `auto` switches to a third-party scoring model when its API key is present in the environment. The child environment is scrubbed of all provider keys as well.
3. `RETRIEVER=custom`, pointing at the gateway. Each query becomes one search and one page read per result on the shared backend. This replaces GPT Researcher's own scraper with the common page reader, which is the price of equal research access.
4. Token limits raised (8k, 8k, 16k, 4k) because the model always thinks and thinking shares the output allowance.
5. Driven through the Python API, so the command-line tool's title-generation call is not made.
6. Effort is set by the gateway (`pin_effort`), since the harness has no setting for it.
Breadth 3, depth 2 and concurrency 4 are the shipped defaults and are kept.

### D. Deep Agents `deep_research` (modified configuration)
`adapters/deepagents_driver.py` rebuilds the example's agent from its own prompts, limits and `think_tool`.
1. Model changed from `claude-sonnet-4-5` at temperature 0.0 to Fable 5.2 with no sampling parameters.
2. The search tool keeps its upstream name (`tavily_search`, which the example's prompts refer to), its signature and its "search, then return full page text" behaviour, but both steps go through the gateway to the shared backend.
3. `max_tokens` 16,000 per call. Effort set by the gateway.
4. The report is read from the agent's `/final_report.md`; if the orchestrator answers inline instead, the last message is used and that is logged.

### Differences that remain between arms
- Report-writing prompts are each system's own. B's prompt is in `prompts/`.
- C reads 5 pages per query by design; D reads 1; B decides for itself.
- C and D make uncached calls; B uses prompt caching. Under a dollar cap this changes how much research each can afford, and it is a property of the system as it stands.
- D edits its message history when it summarises. C runs many independent short calls.
