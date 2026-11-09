"""Command line entry point:  python -m hb <command>

  prepare            download the pinned dataset, verify its hash, check the frozen selection
  plan               counts, caps and pool arithmetic for the frozen configuration
  funding            what can and cannot be verified about available funding (free calls only)
  budget             persistent budget ledger status; --release KEY frees a stale reservation
  preflight          credentials present; --paid makes two tiny metered calls from the buffer pool
  schedule           freeze a campaign manifest (smoke or main), block-randomised
  run                execute the manifest through the gateway (resumable, reserve-before-dispatch)
  grade              Astra (Codex CLI) then Jev on every expected (run, criterion)
  results            build results.json and RESULTS.md from the attempt logs
  verify             offline invariants; non-zero exit on any FAIL
  feasibility        measured cost and completion per system from a finished campaign, against the caps
  review-queue       small blinded manual review queue (CSV)
  compare-astra-modes  agreement between batched and one-at-a-time Astra judging on the same items
  validate-graders   judges against public human labels (dev or heldout), optional Astra mode / perturbation
  diagnostics        hand-written known-answer cases
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

from . import analysis, datasets, grading, runner, verify as V
from .budget import Budget
from .util import ROOT, campaign_dir, git_state, load_config, read_json, read_jsonl, secret, sha256_text

SYSTEMS = "closed_book,plain_tools,gpt_researcher,deepagents"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "plan", "funding"):
        sub.add_parser(name)
    s = sub.add_parser("budget"); s.add_argument("--mock", action="store_true"); s.add_argument("--provider", default="anthropic")
    s = sub.add_parser("preflight"); s.add_argument("--paid", action="store_true")
    s = sub.add_parser("schedule"); s.add_argument("campaign"); s.add_argument("--split", required=True, choices=["smoke", "main"])
    s.add_argument("--systems", default=SYSTEMS); s.add_argument("--limit", type=int); s.add_argument("--mock", action="store_true")
    s.add_argument("--tasks", help="smoke retests only: comma-separated task ids from the split")
    s = sub.add_parser("run"); s.add_argument("campaign"); s.add_argument("--mock", action="store_true"); s.add_argument("--workers", type=int, default=2)
    s.add_argument("--only", help="run only these configurations now (comma-separated); the rest stay scheduled")
    s = sub.add_parser("grade"); s.add_argument("campaign"); s.add_argument("--mock", action="store_true")
    s.add_argument("--judges", default="astra,jev"); s.add_argument("--limit", type=int, help="grade only the first N runs")
    s.add_argument("--retry-errors", action="store_true", help="give items stuck in an error state one fresh bounded round")
    s.add_argument("--astra-mode", choices=["batch", "single"], help="development check only: judge with the other Astra mode; stored separately")
    for name in ("results", "verify", "feasibility", "review-queue", "compare-astra-modes"):
        s = sub.add_parser(name); s.add_argument("campaign")
    s = sub.add_parser("validate-graders"); s.add_argument("--ruverbench", required=True); s.add_argument("--split", required=True, choices=["dev", "heldout"])
    s.add_argument("--judges", default="astra,jev"); s.add_argument("--mock", action="store_true")
    s.add_argument("--astra-mode", choices=["batch", "single"]); s.add_argument("--perturb", choices=["pad", "inject"])
    s = sub.add_parser("diagnostics"); s.add_argument("--judges", default="astra,jev"); s.add_argument("--mock", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.cmd == "prepare":
        print(json.dumps(datasets.prepare(cfg), indent=2)); return 0
    if a.cmd == "plan":
        return plan(cfg)
    if a.cmd == "funding":
        return funding(cfg)
    if a.cmd == "budget":
        print(json.dumps(Budget(a.provider, cfg, a.mock).status(), indent=2)); return 0
    if a.cmd == "preflight":
        return preflight(cfg, a.paid)
    if a.cmd == "validate-graders":
        from . import validate
        rep = validate.run(cfg, Path(a.ruverbench), a.split, a.judges.split(","), a.mock, a.perturb, a.astra_mode)
        print(json.dumps({k: rep[k] for k in ("set", "n_reports", "n_points", "judges_all", "when_judges_agree", "judges_disagree")}, indent=2)); return 0
    if a.cmd == "diagnostics":
        from . import validate
        print(json.dumps({j: v["by_type"] for j, v in validate.diagnostics(cfg, a.judges.split(","), a.mock).items()}, indent=2)); return 0
    cdir = campaign_dir(cfg, a.campaign)
    if a.cmd == "schedule":
        cdir.mkdir(parents=True, exist_ok=True)
        rows = runner.build_manifest(cfg, cdir, a.split, a.systems.split(","), a.limit, a.mock, a.tasks.split(",") if a.tasks else None)
        print(f"froze {len(rows)} scheduled runs ({len({r['task_id'] for r in rows})} questions x {len(a.systems.split(','))} configurations) "
              f"in {cdir / 'manifest.jsonl'}; pool={a.split}; commit={git_state()['commit']}"); return 0
    if a.cmd == "run":
        print(json.dumps(runner.run_campaign(cfg, cdir, a.mock, a.workers, a.only.split(',') if a.only else None), indent=2)); return 0
    tasks = datasets.load_with_rubrics()  # evaluator-side commands only, below this line
    camp = read_json(cdir / "campaign.json")
    if a.cmd == "grade":
        if not a.mock and "jev" in a.judges and not secret("TYPESAFE_API_KEY"):
            raise SystemExit("TYPESAFE_API_KEY must be set for Jev grading (see .env.example)")
        gcfg = cfg if not a.astra_mode else dict(cfg, judges=dict(cfg["judges"], astra=dict(cfg["judges"]["astra"], mode=a.astra_mode)))
        if a.astra_mode and (camp["split"] == "main" or a.judges != "astra"):
            raise SystemExit("--astra-mode is a development check: smoke campaigns only, with --judges astra")
        out = grading.grade(gcfg, cdir, tasks, a.judges.split(","), a.mock, a.limit, a.retry_errors)
        print(json.dumps(out, indent=2))
        if any(k.startswith("paused") for v in out.values() for k in v):
            print("PAUSED: a judge hit its quota or budget. Nothing was rerouted. Fix the cause, then rerun with --retry-errors.")
            return 3
        return 0
    if a.cmd == "results":
        res = analysis.build_results(cfg, cdir, tasks)
        (cdir / "RESULTS.md").write_text(analysis.render_markdown(res))
        print(f"wrote {cdir / 'results.json'} and {cdir / 'RESULTS.md'}"); return 0
    if a.cmd == "verify":
        checks = V.verify(cfg, cdir, tasks)
        for level, check, detail in checks:
            print(f"{level:4}  {check}" + (f"  [{detail}]" if detail and level != "PASS" else ""))
        fails = sum(c[0] == "FAIL" for c in checks)
        print(f"\n{fails} FAIL, {sum(c[0] == 'WARN' for c in checks)} WARN, {sum(c[0] == 'PASS' for c in checks)} PASS")
        return 1 if fails else 0
    if a.cmd == "feasibility":
        return feasibility(cfg, cdir)
    if a.cmd == "compare-astra-modes":
        by = {}
        for at in read_jsonl(cdir / "grades" / "astra.jsonl"):
            if at["state"] == grading.OK:
                by.setdefault((at["run_id"], at["cid"]), {}).setdefault(at["mode"], at["label"])
        both = [v for v in by.values() if len(v) == 2]
        same = sum(v["batch"] == v["single"] for v in both)
        print(json.dumps(dict(items_judged_both_ways=len(both), same_verdict=same, agreement=same / len(both) if both else None,
                              rule="keep batch only if agreement >= 0.90 here and batch is not more than 0.05 below single in "
                                   "balanced accuracy on the validation dev set"), indent=2)); return 0
    if a.cmd == "review-queue":
        return review_queue(cfg, cdir, tasks, camp)
    return 2


def plan(cfg: dict) -> int:
    sp = datasets.load_splits()
    n_sys = len(cfg["systems"])
    main, smoke = sp["splits"]["main"], sp["splits"]["smoke"]
    print(f"configurations: {n_sys} ({', '.join(cfg['systems'])})")
    print(f"main : {len(main)} questions x {n_sys} = {len(main) * n_sys} scored outputs, one run each")
    print(f"smoke: {len(smoke)} questions x {n_sys} = {len(smoke) * n_sys} unscored outputs")
    if sp.get("main_12_fallback"):
        print(f"pre-selected smaller set, by explicit owner decision only: {len(sp['main_12_fallback']['tasks'])} questions")
    print("per-run dollar caps: none. Runs follow their workflow to completion.")
    g = cfg["safeguards"]
    print(f"safeguards (hang/runaway only): {g['max_wall_seconds'] / 3600:.0f} h wall, {g['max_llm_calls']} model calls, "
          f"{g['max_tool_calls']} research calls, {g['max_consecutive_llm_errors']} consecutive model errors")
    for prov in ("anthropic", "exa", "jev"):
        st = Budget(prov, cfg).status()
        print(f"{prov:9} ceiling ${st['ceiling_usd']:.2f}  committed ${st['committed']:.2f}  remaining ${st['remaining_vs_ceiling']:.2f} "
              f"(vs confirmed in account: ${st['remaining_vs_confirmed']:.2f})  by category {st['by_category']}")
    return 0


def funding(cfg: dict) -> int:
    """Free checks only. No provider used here exposes an account balance to an ordinary API key."""
    import subprocess
    import httpx
    print("Anthropic:")
    k = secret("ANTHROPIC_API_KEY")
    if not k:
        print("  key: MISSING")
    else:
        r = httpx.get(f"https://api.anthropic.com/v1/models/{cfg['model']['id']}", headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=30)
        print(f"  key valid and model visible: {r.status_code == 200} (HTTP {r.status_code}, free metadata call)")
    a = cfg["funding"]["anthropic"]
    cf = cfg["confirmed_funding"]
    print(f"  balance: NOT readable with a standard API key. Read it in the Console (Billing), then set it in configs/funding.local.toml.")
    print(f"  limits in force: ceiling ${a['ceiling_usd']:.2f}, confirmed in account ${cf.get('anthropic', 0):.2f}; auto-reload must stay off")
    print("Jev (TypeSafe):")
    k = secret("TYPESAFE_API_KEY")
    if not k:
        print("  key: MISSING")
    else:
        r = httpx.get("https://api.typesafe.ai/v1/models", headers={"Authorization": f"Bearer {k}"}, timeout=30)
        print(f"  key valid: {r.status_code == 200} (HTTP {r.status_code}). The list endpoint shows aliases only; that the pinned "
              f"{cfg['judges']['jev']['model']} answers is confirmed by `preflight --paid` and checked on every judgment.")
    print(f"  balance: read it in the TypeSafe console. Ledger limit in force: ${cf.get('jev', 0):.2f}")
    print("Exa:")
    print(f"  key: {'set' if secret('EXA_API_KEY') else 'MISSING'} (no free validity or balance endpoint; balance needs a team service key)")
    print(f"  balance: read it in the Exa dashboard. Ledger limit in force: ${cf.get('exa', 0):.2f} (provisional)")
    print("Astra (Codex CLI):")
    from . import astra_cli
    r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True)
    st = (r.stdout + r.stderr).strip().splitlines()[0] if (r.stdout + r.stderr).strip() else "codex not found"
    print(f"  {astra_cli.cli_version()} (pinned {cfg['judges']['astra']['cli_version']}); {st}")
    print(f"  environment check: {astra_cli.environment_problem(cfg['judges']['astra']) or 'ok'}")
    print("  remaining credits: not readable without starting a session. Measured on the first smoke grading session.")
    for prov in ("anthropic", "jev", "exa"):
        st = Budget(prov, cfg).status()
        print(f"ledger {prov}: committed ${st['committed']:.2f}, remaining under ceiling ${st['remaining_vs_ceiling']:.2f}, "
              f"under confirmed ${st['remaining_vs_confirmed']:.2f}")
    return 0


def preflight(cfg: dict, paid: bool) -> int:
    need = {"ANTHROPIC_API_KEY": "contestant model (Fable 5.2)", "EXA_API_KEY": "shared search and page reading",
            "TYPESAFE_API_KEY": "Jev judge"}
    ok = True
    for k, why in need.items():
        present = bool(secret(k))
        ok &= present
        print(f"{'set    ' if present else 'MISSING'}  {k:20} {why}")
    from . import astra_cli
    prob = astra_cli.environment_problem(cfg["judges"]["astra"])
    ok &= prob is None
    print(f"{'ok     ' if prob is None else 'PROBLEM'}  codex CLI            Astra judge ({prob or cfg['judges']['astra']['cli_version']})")
    g = git_state()
    print(f"tasks prepared: {datasets.PUBLIC.exists()}   git commit: {g['commit']}  dirty: {g['dirty']}  tags: {g['tags']}")
    if not paid:
        print("\n(no paid calls made; --paid makes one small Fable call and one Jev call, recorded in the ledger)")
        return 0 if ok else 1
    import anthropic, typesafe_sdk as ts
    b, key = Budget("anthropic", cfg), f"preflight/{int(__import__('time').time())}"
    if b.remaining() < 0.05:
        raise SystemExit("the Anthropic ceiling leaves no room for a preflight call")
    m = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"), max_retries=0).messages.create(
        model=cfg["model"]["id"], max_tokens=600, output_config={"effort": "low"}, messages=[{"role": "user", "content": "Reply with OK."}])
    p = cfg["pricing"]["contestant"]
    cost = (m.usage.input_tokens * p["input"] + m.usage.output_tokens * p["output"]) / 1e6
    b.spend("preflight", key, cost)
    print(f"anthropic served by: {m.model} | stop: {m.stop_reason} | cost ${cost:.4f}")
    j = ts.TypeSafeClient(api_key=secret("TYPESAFE_API_KEY")).system_one(
        "The sky is blue.", {"v": ts.Choice(instructions="Is the statement about colour?", criteria={"yes": None, "no": None})},
        model=cfg["judges"]["jev"]["model"])
    Budget("jev", cfg).spend("preflight", key, (j.usage.input_tokens or 0) * cfg["pricing"]["jev"]["input"] / 1e6)
    print("typesafe served by:", j.model, "| choice:", j.answers["v"].choice)
    print("Exa and the Codex CLI are first exercised by the smoke campaign, inside its own reservations.")
    return 0


def feasibility(cfg: dict, cdir) -> int:
    """Measured cost, latency and completion per run of a finished campaign. Needs no grading."""
    rows = []
    for mp in sorted(cdir.glob("outputs/*/meta.json")):
        m = read_json(mp)
        l = m["ledger"]
        rows.append((m["system"], m["task_id"], m["status"], m["report_chars"], l["cost_usd"], l["tool_cost_usd"], m["wall_s"],
                     l["llm_calls"], l["llm_errors"], sum(l["tool_calls"].values()), l["tool_errors"], m["attempts"], m.get("interrupted")))
    print(f"{'system':15}{'task':11}{'status':26}{'report':>7}{'model $':>9}{'exa $':>7}{'total $':>9}{'min':>7}{'calls':>6}{'errs':>5}{'tools':>6}{'t.err':>6}{'tries':>6}  interrupted")
    for r in sorted(rows):
        print(f"{r[0]:15}{r[1]:11}{r[2]:26}{r[3]:>7}{r[4]:>9.2f}{r[5]:>7.2f}{r[4] + r[5]:>9.2f}{r[6] / 60:>7.1f}{r[7]:>6}{r[8]:>5}{r[9]:>6}{r[10]:>6}{r[11]:>6}  {r[12] or ''}")
    return 0


def review_queue(cfg: dict, cdir, tasks: dict, camp: dict) -> int:
    """Blinded queue: judge disagreements (heavy criteria first) up to a cap, plus seeded random agreements."""
    rq = cfg["review"]
    rng = random.Random(rq["seed"] + cdir.name)
    rows = grading.final_grades(cfg, cdir, tasks)
    crit = {(t, c["cid"]): c for t, task in tasks.items() for c in task.get("criteria", [])}
    astra = {(a["run_id"], a["cid"]): a for a in read_jsonl(cdir / "grades" / "astra.jsonl") if a["state"] == grading.OK}
    judged = [r for r in rows if r["has_report"]]
    dis = [r for r in judged if r["consensus"] not in ("met", "not_met")]
    heavy = lambda r: abs(crit[(r["task_id"], r["cid"])]["weight"]) >= rq["heavy_weight"] or crit[(r["task_id"], r["cid"])]["polarity"] < 0
    hv, rest = [r for r in dis if heavy(r)], [r for r in dis if not heavy(r)]
    rng.shuffle(hv); rng.shuffle(rest)
    q = [dict(r, reason="unresolved") for r in (hv + rest)[: rq["max_disagreements"]]]
    agree = [r for r in judged if r["consensus"] in ("met", "not_met")]
    rng.shuffle(agree)
    q += [dict(r, reason="random_agreement") for r in agree[: rq["agreement_sample"]]]
    rng.shuffle(q)
    out = cdir / "review_queue.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item", "blind_run", "task_id", "cid", "weight", "type", "criterion", "passages_one_judge_quoted", "report_path",
                    "your_label(met/not_met/cannot_judge)", "note"])
        for i, r in enumerate(q):
            c = crit[(r["task_id"], r["cid"])]
            w.writerow([i, sha256_text(rq["seed"] + r["run_id"])[:10], r["task_id"], r["cid"], c["weight"], "error" if c["polarity"] < 0 else "requirement",
                        c["text"], " | ".join(astra.get((r["run_id"], r["cid"]), {}).get("quotes") or []), f"outputs/{r['run_id']}/report.md", "", ""])
    (cdir / "review_queue.key.json").write_text(json.dumps(
        [dict(item=i, run_id=r["run_id"], cid=r["cid"], reason=r["reason"], jev=r["jev"], astra=r["astra"]) for i, r in enumerate(q)], indent=1))
    print(f"{len(q)} items -> {out}  ({len(dis)} unresolved of {len(judged)} judged; {min(len(dis), rq['max_disagreements'])} queued, "
          f"{len(dis) - min(len(dis), rq['max_disagreements'])} stay unresolved). Do not open review_queue.key.json while labelling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
