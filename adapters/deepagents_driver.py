"""Deep Agents `examples/deep_research` adapter.  STATUS: plumbing exercised against the mock gateway only.

Keeps the example's orchestrator prompt, sub-agent prompt, think_tool, limits and graph. Changes, all
labelled in docs/COHORT.md:
  1. model: anthropic:claude-sonnet-4-5 @ temperature 0.0  ->  $HB_MODEL with no sampling parameters
  2. tavily_search: same name, signature and "search then return full page text" behaviour, but both
     steps go through the study gateway (shared backend, block list, budget, trace)
  3. the final report is read from the agent's virtual file /final_report.md
Run with the example directory of the pinned deepagents clone on PYTHONPATH (HB_DEEPAGENTS_EXAMPLE).
"""
import os
import sys
from datetime import datetime

import httpx

sys.path.insert(0, os.environ["HB_DEEPAGENTS_EXAMPLE"])
os.environ["TAVILY_API_KEY"] = "unused-by-design"  # the example builds a TavilyClient at import; it is never called

from langchain.chat_models import init_chat_model  # noqa: E402
from langchain_core.tools import InjectedToolArg, tool  # noqa: E402
from typing_extensions import Annotated  # noqa: E402
from deepagents import create_deep_agent  # noqa: E402
from research_agent.prompts import (RESEARCHER_INSTRUCTIONS, RESEARCH_WORKFLOW_INSTRUCTIONS,  # noqa: E402
                                    SUBAGENT_DELEGATION_INSTRUCTIONS)
from research_agent.tools import think_tool  # noqa: E402

GW, TOKEN = os.environ["HB_GATEWAY_URL"], os.environ["HB_RUN_TOKEN"]
http = httpx.Client(base_url=GW, headers={"x-api-key": TOKEN}, timeout=120)


@tool(parse_docstring=True)
def tavily_search(query: str, max_results: Annotated[int, InjectedToolArg] = 1) -> str:
    """Search the web for information on a given query.

    Discovers relevant URLs, then fetches and returns full webpage content as markdown.

    Args:
        query: Search query to execute
        max_results: Maximum number of results to return (default: 1)

    Returns:
        Formatted search results with full webpage content
    """
    sr = http.post("/tools/search", json={"query": query, "max_results": max_results}).json()
    if "error" in sr:
        return f"Search error: {sr['error']}"
    parts = []
    for r in sr.get("results", [])[:max_results]:
        page = http.post("/tools/fetch", json={"url": r["url"]}).json()
        body = page.get("content") or f"Error fetching content from {r['url']}: {page.get('error')}"
        parts.append(f"## {r.get('title')}\n**URL:** {r['url']}\n\n{body}\n\n---\n")
    note = sr.get("notice") or ""
    return f"Found {len(parts)} result(s) for '{query}':\n\n" + "\n".join(parts) + (f"\n{note}" if note else "")


def main() -> int:
    question = open(os.environ["HB_QUESTION_FILE"]).read()
    model = init_chat_model(f"anthropic:{os.environ['HB_MODEL']}", base_url=os.environ["ANTHROPIC_BASE_URL"],
                            api_key=TOKEN, max_tokens=int(os.environ.get("HB_MAX_TOKENS", "32000")), max_retries=2)
    instructions = (RESEARCH_WORKFLOW_INSTRUCTIONS + "\n\n" + "=" * 80 + "\n\n" +
                    SUBAGENT_DELEGATION_INSTRUCTIONS.format(max_concurrent_research_units=3, max_researcher_iterations=3))
    sub = {"name": "research-agent",
           "description": "Delegate research to the sub-agent researcher. Only give this researcher one topic at a time.",
           "system_prompt": RESEARCHER_INSTRUCTIONS.format(date=datetime.now().strftime("%Y-%m-%d")),
           "tools": [tavily_search, think_tool]}
    agent = create_deep_agent(model=model, tools=[tavily_search, think_tool], system_prompt=instructions, subagents=[sub])
    state = agent.invoke({"messages": [{"role": "user", "content": question}]})
    f = (state.get("files") or {}).get("/final_report.md")
    if f is not None:
        content = f.get("content") if isinstance(f, dict) else f
        report, src = ("\n".join(content) if isinstance(content, list) else str(content)), "final_report.md"
    else:  # the orchestrator answered inline instead of writing the file
        last = state["messages"][-1].content
        report = last if isinstance(last, str) else "\n".join(b.get("text", "") for b in last if isinstance(b, dict) and b.get("type") == "text")
        src = "last_message"
    open(os.environ["HB_REPORT_FILE"], "w").write(report)
    print(f"HB report_source={src} chars={len(report)}")
    return 0 if report.strip() else 3


if __name__ == "__main__":
    sys.exit(main())
