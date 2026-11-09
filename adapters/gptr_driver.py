"""GPT Researcher (report_type="deep") adapter.  STATUS: see docs/COHORT.md for what has been exercised.

All model roles, the context filter and the retriever are set through environment variables in
configs/study.toml ([systems.gpt_researcher.env]); the child environment is scrubbed, so
TYPESAFE_API_KEY / OPENAI_API_KEY are absent and CONTEXT_FILTER is pinned to "keyword".
The Python API is used instead of cli.py so the FAST_LLM title-generation call is not made.
"""
import asyncio
import os
import sys


async def main() -> int:
    from gpt_researcher import GPTResearcher
    question = open(os.environ["HB_QUESTION_FILE"]).read()
    r = GPTResearcher(query=question, report_type="deep")
    await r.conduct_research()
    report = await r.write_report()
    open(os.environ["HB_REPORT_FILE"], "w").write(report or "")
    print(f"HB chars={len(report or '')} harness_reported_cost={r.get_costs() if hasattr(r, 'get_costs') else 'n/a'}")
    return 0 if (report or "").strip() else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
