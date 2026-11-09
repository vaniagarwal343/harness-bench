"""Offline tests: no network, no spend.      .venv/bin/python -m pytest -q"""
import asyncio
import json

import anthropic
import httpx
import pytest

from hb import analysis, astra_cli, budget, grading
from hb.gateway import Gateway
from hb.util import load_config, read_jsonl, write_json

CFG = load_config()
MODEL = CFG["model"]["id"]
MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture(scope="module")
def gw():
    g = Gateway(CFG, None, None, mock=True).start()
    yield g
    g.stop()


def client(gw, led):
    return anthropic.Anthropic(api_key=led.token, base_url=gw.url, max_retries=0)


def policy(**kw):
    return dict(model=MODEL, effort="high", allow_tools=True, budget={}, shims=[]) | kw


# ---------------------------------------------------------------- model and tool policy

def test_wrong_model_is_refused_and_logged(gw, tmp_path):
    led = gw.open_run(tmp_path, policy())
    with pytest.raises(anthropic.BadRequestError, match="HB_POLICY model"):
        client(gw, led).messages.create(model="claude-opus-5-5", max_tokens=100, messages=MSG)
    assert [v["kind"] for v in led.violations] == ["model"]
    assert led.llm_calls == 1 and led.cost_usd == 0  # the attempt is counted even though nothing was served


def test_fallback_and_server_side_search_are_refused(gw, tmp_path):
    led = gw.open_run(tmp_path, policy())
    with pytest.raises(anthropic.BadRequestError, match="fallbacks are disabled"):
        client(gw, led).messages.create(model=MODEL, max_tokens=100, messages=MSG, extra_body={"fallbacks": [{"model": "claude-opus-4-8"}]})
    with pytest.raises(anthropic.BadRequestError, match="server tool"):
        client(gw, led).messages.create(model=MODEL, max_tokens=100, messages=MSG, tools=[{"type": "web_search_20260209", "name": "web_search"}])


def test_closed_book_cannot_send_or_call_tools(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(allow_tools=False))
    tool = {"name": "search", "description": "x", "input_schema": {"type": "object", "properties": {}}}
    with pytest.raises(anthropic.BadRequestError, match="closed-book"):
        client(gw, led).messages.create(model=MODEL, max_tokens=100, tools=[tool], messages=MSG)
    r = httpx.post(gw.url + "/tools/search", headers={"x-api-key": led.token}, json={"query": "x"})
    assert r.status_code == 403 and {v["kind"] for v in led.violations} == {"tools"}


def test_effort_is_pinned_and_logged_when_a_harness_cannot_set_it(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(shims=["pin_effort"]))
    client(gw, led).messages.create(model=MODEL, max_tokens=100, messages=MSG)
    rec = [r for r in read_jsonl(tmp_path / "trace.jsonl") if r["type"] == "llm"][0]
    assert rec["shims"] == ["pin_effort"] and rec["effective_effort"] == "high" and led.efforts_seen == {"high": 1}


# ---------------------------------------------------------------- money is reserved before dispatch

def test_a_call_that_cannot_be_afforded_is_refused_before_it_is_sent(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(budget=dict(max_cost_usd=0.01)))  # $0.01 cannot pay for 1024 output tokens at $40/M
    with pytest.raises(anthropic.BadRequestError, match="HB_BUDGET_EXHAUSTED"):
        client(gw, led).messages.create(model=MODEL, max_tokens=4000, messages=MSG)
    assert led.cost_usd == 0 and led.models_seen == {} and led.budget_refusals == 1  # nothing reached the model


def test_output_allowance_is_lowered_to_what_the_remaining_budget_can_pay(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(budget=dict(max_cost_usd=0.10)))
    client(gw, led).messages.create(model=MODEL, max_tokens=20000, messages=MSG)  # 20000 tokens would be $0.80
    rec = [r for r in read_jsonl(tmp_path / "trace.jsonl") if r["type"] == "llm"][0]
    assert "clamp_max_tokens" in rec["shims"] and rec["effective_max_tokens"] < 2500
    assert rec["cost_usd"] <= rec["reserved_usd"] <= 0.10


def test_concurrent_subagent_calls_never_commit_more_than_the_cap(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(budget=dict(max_cost_usd=0.20)))

    async def burst():
        async with httpx.AsyncClient(base_url=gw.url, headers={"x-api-key": led.token}, timeout=60) as c:
            return await asyncio.gather(*[c.post("/v1/messages", json=dict(model=MODEL, max_tokens=3000, messages=MSG)) for _ in range(12)])
    codes = [r.status_code for r in asyncio.run(burst())]
    assert codes.count(200) >= 1
    assert led.max_committed_usd <= 0.20 + 1e-9 and led.reserved_usd < 1e-9 and led.inflight == 0


def test_research_tools_close_early_so_the_report_can_still_be_written(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(budget=dict(max_cost_usd=4.0, final_reserve_usd=1.0)))
    h = {"x-api-key": led.token}
    assert "results" in httpx.post(gw.url + "/tools/search", headers=h, json={"query": "x"}).json()
    led.cost_usd = 3.01  # past the research share of the budget
    assert "HB_RESEARCH_BUDGET_USED" in httpx.post(gw.url + "/tools/search", headers=h, json={"query": "x"}).json()["error"]
    client(gw, led).messages.create(model=MODEL, max_tokens=8000, messages=MSG)  # the model can still write


def test_search_spend_is_capped_per_run_and_metered(gw, tmp_path):
    led = gw.open_run(tmp_path, policy(budget=dict(max_tool_cost_usd=0.03)))
    h = {"x-api-key": led.token}
    outs = [httpx.post(gw.url + "/tools/search", headers=h, json={"query": "x"}).json() for _ in range(4)]
    assert sum("results" in o for o in outs) == 2 and "HB_RESEARCH_BUDGET_USED" in outs[-1]["error"]
    assert 0 < led.tool_cost_usd <= 0.03 and led.tool_calls["search"] == 4  # refused attempts are still counted


def test_benchmark_sources_are_filtered_and_a_closed_token_is_dead(gw, tmp_path):
    led = gw.open_run(tmp_path, policy())
    h = {"x-api-key": led.token}
    s = httpx.post(gw.url + "/tools/search", headers=h, json={"query": "x"}).json()
    assert s["filtered_benchmark_sources"] == 1 and all("researchrubrics" not in r["url"] for r in s["results"])
    assert "excluded" in httpx.post(gw.url + "/tools/fetch", headers=h, json={"url": "https://huggingface.co/datasets/ScaleAI/researchrubrics"}).json()["error"]
    gw.close_run(led)
    assert httpx.post(gw.url + "/tools/search", headers=h, json={"query": "x"}).status_code == 401


# ---------------------------------------------------------------- persistent campaign budget

def _budget(tmp_path, monkeypatch, confirmed=210.0):
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    cfg = dict(CFG, confirmed_funding=dict(CFG["confirmed_funding"], anthropic=confirmed))
    return budget.Budget("anthropic", cfg), cfg


def test_pools_do_not_borrow_and_confirmed_funding_is_a_hard_limit(tmp_path, monkeypatch):
    b, cfg = _budget(tmp_path, monkeypatch)
    for i in range(5):
        b.reserve("buffer", f"retry{i}", 4.0)  # 5 x $4 = $20 of the $21 buffer
    with pytest.raises(budget.BudgetRefused, match="buffer"):
        b.reserve("buffer", "retry7", 4.0)  # debugging cannot dip into the main allocation
    for i in range(47):
        b.reserve("main", f"m{i}", 4.0)  # 20 + 188 = 208 committed
    with pytest.raises(budget.BudgetRefused, match="confirmed funding"):
        b.reserve("main", "m47", 4.0)  # would pass $210 that is actually in the account
    b.reconcile("m0", 1.25)  # actual use frees the difference
    b.reserve("main", "m47", 4.0)
    again = budget.Budget("anthropic", cfg)  # a new process sees the same state
    st = again.status()
    assert abs(st["committed"] - (20 + 46 * 4 + 1.25 + 4)) < 1e-6 and len(st["open"]) == 5 + 47 and not st["over_reservation"]


def test_no_confirmed_funding_on_file_means_nothing_can_be_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    b = budget.Budget("anthropic", dict(CFG, confirmed_funding={}))
    with pytest.raises(budget.BudgetRefused, match="confirmed funding"):
        b.reserve("smoke", "x", 1.0)


def test_plan_arithmetic_matches_the_agreed_counts():
    assert budget.plan_check(CFG) == []
    assert CFG["sampling"]["main"] == 15 and CFG["sampling"]["smoke"] == 2 and len(CFG["systems"]) == 5
    assert CFG["systems"]["closed_book"]["max_tokens"] * CFG["pricing"]["contestant"]["output"] / 1e6 < CFG["budget"]["closed_book"]["max_cost_usd"]
    bad = dict(CFG, sampling=dict(CFG["sampling"], main=16))
    assert any("main" in p for p in budget.plan_check(bad))  # a 16th question does not fit; it is reported, not trimmed


# ---------------------------------------------------------------- scoring semantics

def crit(cid, w, bucket="content", cite=False):
    return dict(cid=cid, text=cid, weight=w, bucket=bucket, polarity=-1 if w < 0 else 1, citation_dependent=cite or bucket == "citation")


def test_score_is_unclipped_and_an_error_criterion_met_is_a_penalty():
    cs = [crit("a", 5), crit("err", -5), crit("err2", -5)]
    assert analysis.score_task(cs, {"a": True, "err": True, "err2": True})["total"] == -1.0
    assert analysis.score_task(cs, {"a": True, "err": False, "err2": False})["total"] == 1.0
    assert analysis.score_task(cs, {"a": True, "err": True, "err2": False})["errors_present"] == 1


def test_missing_verdict_blocks_the_score_instead_of_counting_as_not_met():
    assert analysis.score_task([crit("a", 5), crit("b", 5)], {"a": True, "b": None})["total"] is None


def test_citation_dependent_criteria_are_separable_for_the_closed_book_view():
    s = analysis.score_task([crit("a", 5), crit("c", 5, "citation")], {"a": True, "c": False})
    assert s["total"] == 0.5 and s["adapted_no_citation"] == 1.0 and s["adapted_citation_dependent"] == 0.0


def test_unresolved_and_cannot_judge_are_bounded_not_failed():
    c, e = crit("a", 5), crit("err", -5)
    base = dict(has_report=True, adjudicated=None, jev_state="ok", astra_state="ok")
    dis = dict(base, jev="met", astra="not_met", consensus="disagree")
    assert (analysis._met(dis, c, "consensus_lo"), analysis._met(dis, c, "consensus_hi")) == (False, True)
    assert (analysis._met(dis, e, "consensus_lo"), analysis._met(dis, e, "consensus_hi")) == (True, False)
    cj = dict(base, jev="cannot_judge", astra="cannot_judge", consensus="cannot_judge")
    assert (analysis._met(cj, c, "consensus_lo"), analysis._met(cj, c, "consensus_hi")) == (False, True)
    assert analysis._met(cj, c, "jev") is False and analysis._met(cj, e, "jev") is False  # no credit, no penalty
    assert analysis._met(dict(dis, adjudicated="met"), c, "consensus_lo") is True  # a manual label replaces the bound


def test_comparison_bounds_pair_opposite_ends():
    def run(task, system, lo, hi):
        sc = {v: {m: x for m in analysis.METRICS} | {"errors_present": 0} for v, x in
              (("jev", lo), ("astra", hi), ("consensus_lo", lo), ("consensus_hi", hi), ("two_judge_mean", (lo + hi) / 2))}
        return dict(task_id=task, system=system, status="completed", attempts=1, scores=sc, cost_usd=1.0, tool_cost_usd=0.1, llm_calls=1,
                    tool_calls={}, wall_s=1, report_chars=10, usage={}, violations=0,
                    grading=dict(expected=1, disagree=0, unresolved_error=0, cannot_judge=0, adjudicated=0))
    runs = []
    for i in range(6):
        runs += [run(f"t{i}", "plain_tools", 0.40, 0.50), run(f"t{i}", "gpt_researcher", 0.45, 0.60), run(f"t{i}", "closed_book", 0.1, 0.1)]
    s = analysis.summarize(CFG, runs)
    c = next(x for x in s["comparisons"] if x["system"] == "gpt_researcher")["metric"]["total"]
    assert abs(c["bound_lo"]["mean"] - (0.45 - 0.50)) < 1e-9   # H_lower - B_upper
    assert abs(c["bound_hi"]["mean"] - (0.60 - 0.40)) < 1e-9   # H_upper - B_lower
    assert next(x for x in s["comparisons"] if x["system"] == "gpt_researcher")["direction_supported"] == "none"
    assert next(x for x in s["comparisons"] if x["system"] == "plain_tools")["direction_supported"] == "higher"


def test_holm_and_small_samples():
    assert analysis.holm({"a": 0.01, "b": 0.04, "c": 0.03}) == {"a": 0.03, "b": 0.06, "c": 0.06}
    assert analysis.boot_mean([1.0], 100, 1)["mean"] is None  # one task is not a sample
    assert analysis.sign_flip_p([0.1] * 8, 2000, 1) < 0.05


# ---------------------------------------------------------------- judge failure hygiene

def _campaign(tmp_path, report="The report says A clearly."):
    tasks = {"t1": dict(task_id="t1", dataset="researchrubrics", question="Q?",
                        criteria=[dict(crit("a", 5), text="says A"), dict(crit("err", -5), text="claims B")])}
    rd = tmp_path / "runs" / "c" / "outputs" / "t1__plain_tools"
    rd.mkdir(parents=True)
    (rd / "report.md").write_text(report)
    write_json(rd / "meta.json", dict(run_id="t1__plain_tools", task_id="t1", system="plain_tools", status="completed"))
    return tasks, tmp_path / "runs" / "c"


def test_quota_error_never_becomes_a_grade_and_grading_pauses(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    tasks, cdir = _campaign(tmp_path)
    monkeypatch.setattr(grading, "jev_attempt", lambda *a, **k: dict(state="error_quota", error="429"))
    stats = grading.grade(CFG, cdir, tasks, ["astra", "jev"], mock=True)
    assert stats["astra"] == {"ok": 2} and set(stats["jev"]) <= {"error_quota", "paused_budget"} and "error_quota" in stats["jev"]
    att = read_jsonl(cdir / "grades" / "jev.jsonl")
    assert 1 <= len(att) <= 2 and all(a.get("label") is None for a in att)  # not retried in a loop; later items are paused
    rows = grading.final_grades(CFG, cdir, tasks)
    assert all(r["jev"] is None and r["consensus"] == "unresolved_error" for r in rows)
    monkeypatch.undo(); monkeypatch.setattr(budget, "ROOT", tmp_path)
    assert grading.grade(CFG, cdir, tasks, ["jev"], mock=True)["jev"] == {"ok": 2}  # after the quota is fixed, an explicit rerun judges them
    assert grading.grade(CFG, cdir, tasks, ["jev"], mock=True)["jev"] == {"cached": 2}


def test_jev_spend_is_reserved_first_and_stops_at_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    tasks, cdir = _campaign(tmp_path)
    tiny = dict(CFG, confirmed_funding=dict(CFG["confirmed_funding"], jev=0.004))
    stats = grading.grade(tiny, cdir, tasks, ["jev"], mock=True)
    assert stats["jev"].get("ok", 0) >= 1 and sum(stats["jev"].values()) == 2
    assert budget.Budget("jev", tiny, mock=True).status()["committed"] <= 0.004


def test_no_report_is_scored_by_rule_without_calling_a_judge(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    tasks, cdir = _campaign(tmp_path, report="")
    assert grading.grade(CFG, cdir, tasks, ["astra", "jev"], mock=True) == {"astra": {}, "jev": {}}
    rows = grading.final_grades(CFG, cdir, tasks)
    assert {r["consensus"] for r in rows} == {"no_report"}
    met = {r["cid"]: analysis._met(r, c, "astra") for r, c in zip(rows, tasks["t1"]["criteria"])}
    assert analysis.score_task(tasks["t1"]["criteria"], met)["total"] == 0


def test_cache_key_changes_with_judge_version_mode_prompt_rubric_and_report():
    c, j = dict(text="says A", polarity=1), CFG["judges"]["jev"]
    base = grading.item_key("jev", j, "p1", c, "r1", "q1")
    assert base != grading.item_key("jev", dict(j, model="jev-1.13.0"), "p1", c, "r1", "q1")
    assert base != grading.item_key("jev", j, "p2", c, "r1", "q1")
    assert base != grading.item_key("jev", j, "p1", dict(c, text="says B"), "r1", "q1")
    assert base != grading.item_key("jev", j, "p1", c, "r2", "q1")
    a = CFG["judges"]["astra"]
    assert grading.item_key("astra", a, "p", c, "r", "q", "b1") != grading.item_key("astra", dict(a, mode="single"), "p", c, "r", "q", "b1")


# ---------------------------------------------------------------- Astra through the CLI

def test_astra_verdicts_are_validated_item_by_item():
    report = "Larkhaven opened its first tram line in 1987."
    ok = grading.check_astra_item(dict(verdict="met", report_quotes=["opened its  first tram line"], reason="r"), report)
    assert ok["state"] == "ok" and ok["label"] == "met"  # whitespace-insensitive exact match
    assert grading.check_astra_item(dict(verdict="met", report_quotes=["opened in 1978"], reason="r"), report)["state"] == "error_quote_mismatch"
    assert grading.check_astra_item(dict(verdict="met", report_quotes=[], reason="r"), report)["state"] == "error_quote_mismatch"
    assert grading.check_astra_item(dict(verdict="not_met", report_quotes=[], reason="absent"), report)["state"] == "ok"
    assert grading.check_astra_item(dict(verdict="maybe", report_quotes=[], reason=""), report)["state"] == "error_malformed"
    assert grading.check_astra_item(dict(verdict="met", report_quotes=["opened in 1978"], reason="r"), report).get("label") is None


def test_a_judge_session_that_used_a_tool_or_hit_a_limit_is_detected():
    evs = [{"type": "item.completed", "item": {"type": "reasoning"}}, {"type": "item.completed", "item": {"type": "agent_message"}},
           {"type": "turn.completed", "usage": {"input_tokens": 10}}]
    assert astra_cli.inspect(evs, "")["foreign_items"] == [] and astra_cli.inspect(evs, "")["limit_messages"] == []
    bad = evs + [{"type": "item.completed", "item": {"type": "command_execution", "command": "cat ~/notes"}}]
    assert astra_cli.inspect(bad, "")["foreign_items"]
    assert astra_cli.inspect(evs, 'INFO MCP binding catalog materialized server_kind="apps" tool_count=180')["mcp_catalogs"] == [("apps", 180)]
    assert astra_cli.inspect(evs + [{"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}], "")["limit_messages"]


def test_the_judge_prompt_contains_only_task_report_and_criteria_and_no_api_route():
    crits = [dict(cid="c00", text="says A", polarity=1), dict(cid="c01", text="claims B", polarity=-1)]
    p = astra_cli.build_prompt("Q?", "REPORT BODY", crits)
    assert "REPORT BODY" in p and '"type": "error"' in p and "plain_tools" not in p and "jev" not in p.lower() and "weight" not in p
    cmd = astra_cli.command(CFG["judges"]["astra"], __import__("pathlib").Path("/w"), __import__("pathlib").Path("s"), __import__("pathlib").Path("l"))
    assert cmd[:4] == ["codex", "exec", "-m", "gpt-6-astra"] and {"--ignore-user-config", "--ephemeral", "--ignore-rules"} <= set(cmd)
    assert 'model_reasoning_effort="medium"' in cmd and 'web_search="disabled"' in cmd and cmd[-1] == "-"
    assert "OPENAI_API_KEY" in astra_cli.UNSET and set(astra_cli.schema()["properties"]["verdicts"]["items"]["required"]) == {"cid", "verdict", "report_quotes", "reason"}


def test_missing_or_duplicated_criteria_in_a_batch_are_errors_for_those_items_only(tmp_path, monkeypatch):
    import hb.mock as mock

    def one_missing(judge, labels, report, text):
        return ("met", {}) if text == "says A" else ("not_met", {})
    monkeypatch.setattr(mock, "mock_verdict", one_missing)
    crits = [dict(cid="a", text="says A", polarity=1), dict(cid="b", text="claims B", polarity=-1)]
    res = astra_cli.judge("Q?", "The report says A clearly.", crits, CFG["judges"]["astra"], tmp_path, mock=True)
    assert res["items"]["a"]["state"] == "ok" and res["items"]["b"]["state"] == "ok"
    assert json.loads((tmp_path / res["session"]["id"] / "raw_verdicts.json").read_text())["verdicts"]  # original output kept


def test_a_harness_that_dies_at_the_cap_is_a_budget_outcome_and_is_not_retried(gw, tmp_path):
    from hb import systems
    led = gw.open_run(tmp_path, policy(budget=dict(max_cost_usd=0.01, max_wall_seconds=60)))
    script = tmp_path / "dies_at_cap.py"
    script.write_text("import anthropic, os\nanthropic.Anthropic(max_retries=0).messages.create(model=os.environ['HB_MODEL'], "
                      "max_tokens=4000, messages=[dict(role='user', content='hi')])\n")
    scfg = dict(kind="subprocess", budget="research", command=f"'{systems.ROOT}/.venv/bin/python' '{script}'")
    cfg = dict(CFG, budget=dict(CFG["budget"], research=dict(max_cost_usd=0.01, max_wall_seconds=60)))
    out = systems.run_subprocess("Q?", scfg, led, gw, cfg, tmp_path)
    assert out["status"] == "budget_exhausted" and out["report"] == ""
