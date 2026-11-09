"""Scoring, paired task-level statistics, and the single results artifact.

Everything reported (tables, headline numbers) is rendered from results.json, which is built here
from the attempt logs. Nothing is typed by hand.

Score (ResearchRubrics, released upstream code path):
    S = sum(w_i * m_i) / sum(w_i for w_i > 0)        binary m, unclipped, can be negative
A negative-weight criterion describes an error: MET means the error is present and its negative
weight is added. Sub-scores restricted to a subset of criteria are our own and are named `adapted_*`.

What a score here means: compliance with the rubric as judged from the report text. It is not an
independent check that the report's statements are true, and rubric citation criteria check that
citations are present, not that a cited source supports the claim.

One run per configuration and task: run-to-run variation within a task is NOT estimated.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np

from .grading import OK, final_grades
from .util import read_json, read_jsonl, sha256_file, write_json

VIEWS = ("jev", "astra", "consensus_lo", "consensus_hi")
BUCKETS = ("content", "coverage", "citation", "presentation")
METRICS = ("total", "adapted_no_citation", "adapted_citation_dependent", "adapted_content", "adapted_coverage",
           "adapted_citation", "adapted_presentation")


def _met(row: dict, crit: dict, view: str) -> bool | None:
    """Is the criterion MET under this view? None = no valid verdict from this judge (blocks the score)."""
    if not row["has_report"]:
        return False  # frozen rule: no report satisfies nothing and commits no stated error
    if view in ("jev", "astra"):
        # "cannot_judge" is a valid verdict: no credit and no penalty in the single-judge view
        return None if row[f"{view}_state"] != OK else row[view] == "met"
    if row.get("adjudicated") in ("met", "not_met"):
        return row["adjudicated"] == "met"
    if row["consensus"] in ("met", "not_met"):
        return row["consensus"] == "met"
    # unresolved: disagreement, judge error, or cannot_judge. Bound it; lo = least favourable to the report.
    unfavourable = crit["polarity"] < 0  # for an error criterion the unfavourable reading is MET
    return unfavourable if view == "consensus_lo" else not unfavourable


def score_task(criteria: list[dict], met: dict[str, bool | None]) -> dict:
    undefined: list[str] = []

    def formula(cs: list[dict], name: str = "total") -> float | None:
        pos = sum(c["weight"] for c in cs if c["weight"] > 0)
        if pos <= 0:  # this task has no positive-weight criterion of that kind: the metric does not exist for it
            undefined.append(name)
            return None
        if any(met[c["cid"]] is None for c in cs):
            return None  # a verdict is missing: blocked, not zero
        return sum(c["weight"] for c in cs if met[c["cid"]]) / pos

    out = {"total": formula(criteria)}
    for b in BUCKETS:
        out[f"adapted_{b}"] = formula([c for c in criteria if c["bucket"] == b], f"adapted_{b}")
    out["adapted_no_citation"] = formula([c for c in criteria if not c["citation_dependent"]], "adapted_no_citation")
    out["adapted_citation_dependent"] = formula([c for c in criteria if c["citation_dependent"]], "adapted_citation_dependent")
    out["undefined"] = undefined
    known = [c for c in criteria if met[c["cid"]] is not None]
    out["errors_present"] = sum(1 for c in known if c["polarity"] < 0 and met[c["cid"]])
    return out


def build_results(cfg: dict, cdir: Path, tasks: dict) -> dict:
    grades = final_grades(cfg, cdir, tasks)
    by_run = defaultdict(list)
    for g in grades:
        by_run[g["run_id"]].append(g)
    camp = read_json(cdir / "campaign.json")
    runs = []
    for sched in read_jsonl(cdir / "manifest.jsonl"):
        mp = cdir / "outputs" / sched["run_id"] / "meta.json"
        if not mp.exists():
            runs.append(dict(sched, status="not_run", scores={}))
            continue
        m = read_json(mp)
        task = tasks[m["task_id"]]
        rows = {g["cid"]: g for g in by_run.get(m["run_id"], [])}
        scores = {}
        for v in VIEWS:
            met = {c["cid"]: (_met(rows[c["cid"]], c, v) if c["cid"] in rows else None) for c in task["criteria"]}
            scores[v] = score_task(task["criteria"], met)
        scores["two_judge_mean"] = {k: (None if scores["jev"][k] is None or scores["astra"][k] is None
                                        else (scores["jev"][k] + scores["astra"][k]) / 2) for k in METRICS}
        rv = list(rows.values())
        runs.append(dict(run_id=m["run_id"], task_id=m["task_id"], dataset=task["dataset"], system=m["system"],
                         block=sched["block"], status=m["status"], attempts=m["attempts"], scores=scores,
                         report_chars=m.get("report_chars", 0), cost_usd=m["ledger"]["cost_usd"], tool_cost_usd=m["ledger"]["tool_cost_usd"],
                         usage=m["ledger"]["usage"], tool_calls=m["ledger"]["tool_calls"], llm_calls=m["ledger"]["llm_calls"],
                         wall_s=m.get("wall_s"), violations=len(m["ledger"]["violations"]), models_seen=m["ledger"]["models_seen"],
                         efforts_seen=m["ledger"]["efforts_seen"], git_commit=(m.get("git") or {}).get("commit"),
                         grading=dict(expected=len(task["criteria"]),
                                      disagree=sum(r["consensus"] == "disagree" for r in rv),
                                      unresolved_error=sum(r["consensus"] == "unresolved_error" for r in rv),
                                      cannot_judge=sum("cannot_judge" in (r.get("jev"), r.get("astra")) for r in rv),
                                      adjudicated=sum(bool(r.get("adjudicated")) for r in rv))))
    res = dict(schema=2, mock=bool(camp.get("mock")), scored=bool(camp.get("scored")), split=camp["split"],
               config_sha256=cfg["_sha256"], manifest_sha256=sha256_file(cdir / "manifest.jsonl"), git=camp.get("git"),
               model=cfg["model"]["id"], runs=runs)
    res["summary"] = summarize(cfg, runs)
    res["grading"] = grading_account(cdir)
    write_json(cdir / "results.json", res)
    return res


def grading_account(cdir: Path) -> dict:
    out = {}
    for j in ("jev", "astra"):
        att = read_jsonl(cdir / "grades" / f"{j}.jsonl")
        out[j] = dict(attempts=len(att), ok=sum(a["state"] == OK for a in att),
                      error_states={s: sum(a["state"] == s for a in att) for s in sorted({a["state"] for a in att} - {OK})})
    out["jev"]["cost_usd"] = round(sum(a.get("cost_usd", 0) for a in read_jsonl(cdir / "grades" / "jev.jsonl")), 4)
    out["jev"]["long_context_judgments"] = sum(a.get("evidence_mode") == "long_context" and a["state"] == OK
                                               for a in read_jsonl(cdir / "grades" / "jev.jsonl"))
    out["astra"]["cli_sessions"] = len(list((cdir / "grades" / "astra_sessions").glob("*/record.json")))
    return out


# ---------------------------------------------------------------- statistics

def boot_mean(x: list[float], n_boot: int, seed: int) -> dict:
    """Percentile bootstrap over tasks for the mean of paired task-level differences."""
    a = np.asarray(x, dtype=float)
    if len(a) < 2:
        return dict(n_tasks=len(a), mean=None, lo=None, hi=None)
    rng = np.random.default_rng(seed)
    boots = a[rng.integers(0, len(a), (n_boot, len(a)))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return dict(n_tasks=len(a), mean=float(a.mean()), lo=float(lo), hi=float(hi),
                wins=int((a > 0).sum()), losses=int((a < 0).sum()), ties=int((a == 0).sum()))


def sign_flip_p(x: list[float], n: int, seed: int) -> float | None:
    """Two-sided paired sign-flip permutation p-value for mean difference = 0."""
    a = np.asarray(x, dtype=float)
    if len(a) < 2:
        return None
    rng = np.random.default_rng(seed)
    obs = abs(a.mean())
    flips = rng.choice([-1.0, 1.0], size=(n, len(a)))
    return float(((np.abs((flips * a).mean(axis=1)) >= obs - 1e-12).sum() + 1) / (n + 1))


def holm(ps: dict[str, float | None]) -> dict[str, float | None]:
    known = sorted((p, k) for k, p in ps.items() if p is not None)
    out, running = {k: None for k in ps}, 0.0
    for i, (p, k) in enumerate(known):
        running = max(running, min(1.0, p * (len(known) - i)))
        out[k] = running
    return out


def summarize(cfg: dict, runs: list[dict]) -> dict:
    a = cfg["analysis"]
    systems = sorted({r["system"] for r in runs})
    by = {(r["task_id"], r["system"]): r for r in runs}
    tasks = sorted({r["task_id"] for r in runs})

    def val(task: str, system: str, view: str, metric: str) -> float | None:
        r = by.get((task, system))
        return ((r or {}).get("scores", {}).get(view) or {}).get(metric)

    table = {}
    for s in systems:
        rs = [r for r in runs if r["system"] == s]

        def mean(view: str, metric: str) -> float | None:
            # every scheduled run counts; a missing score blocks the number instead of shrinking the denominator.
            # Tasks where the metric does not exist (no such criteria in the rubric) are the only ones left out.
            vv = "jev" if view == "two_judge_mean" else view
            keep = [r for r in rs if metric not in ((r.get("scores") or {}).get(vv) or {}).get("undefined", [])]
            vals = [val(r["task_id"], s, view, metric) for r in keep]
            return None if any(v is None for v in vals) or not vals else float(np.mean(vals))

        ran = [r for r in rs if r["status"] != "not_run"]
        table[s] = dict(
            scheduled=len(rs), with_report=sum(r["status"] in ("completed", "partial") for r in rs),
            status_counts={st: sum(r["status"] == st for r in rs) for st in sorted({r["status"] for r in rs})},
            total={v: mean(v, "total") for v in (*VIEWS, "two_judge_mean")},
            adapted={m: {v: mean(v, m) for v in ("jev", "astra", "two_judge_mean")} for m in METRICS[1:]},
            errors_present_mean={v: mean(v, "errors_present") for v in ("jev", "astra")},
            grading={k: sum(r["grading"][k] for r in ran) for k in ("expected", "disagree", "unresolved_error", "cannot_judge", "adjudicated")},
            cost_usd_total=round(sum(r["cost_usd"] for r in ran), 4), cost_usd_max=max((r["cost_usd"] for r in ran), default=None),
            exa_usd_total=round(sum(r["tool_cost_usd"] for r in ran), 4),
            means={k: (float(np.mean([f(r) for r in ran])) if ran else None) for k, f in (
                ("cost_usd", lambda r: r["cost_usd"]), ("llm_calls", lambda r: r["llm_calls"]),
                ("tool_calls", lambda r: sum(r["tool_calls"].values())), ("wall_s", lambda r: r["wall_s"] or 0),
                ("report_chars", lambda r: r["report_chars"]), ("output_tokens", lambda r: r["usage"].get("output_tokens", 0)),
                ("input_tokens_all", lambda r: sum(r["usage"].get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))))},
            retries=sum(r["attempts"] - 1 for r in ran), policy_violations=sum(r["violations"] for r in ran))

    comps = []
    for base, other in a["primary_pairs"]:
        if base not in systems or other not in systems:
            continue
        entry = dict(system=other, baseline=base, metric={})
        for metric in ("total", "adapted_no_citation"):
            m = {}
            for v in ("jev", "astra", "two_judge_mean"):
                d = [(val(t, other, v, metric), val(t, base, v, metric)) for t in tasks]
                m[v] = (boot_mean([x - y for x, y in d], a["n_boot"], a["boot_seed"]) if all(x is not None and y is not None for x, y in d)
                        else dict(blocked="a scheduled run is missing or its grading is incomplete"))
            # Unresolved judgments: the difference is bounded by pairing opposite bounds,
            # [H_lower - B_upper, H_upper - B_lower], never lower-with-lower.
            lo = [(val(t, other, "consensus_lo", metric), val(t, base, "consensus_hi", metric)) for t in tasks]
            hi = [(val(t, other, "consensus_hi", metric), val(t, base, "consensus_lo", metric)) for t in tasks]
            ok = all(x is not None and y is not None for x, y in lo + hi)
            m["bound_lo"] = boot_mean([x - y for x, y in lo], a["n_boot"], a["boot_seed"]) if ok else dict(blocked="incomplete")
            m["bound_hi"] = boot_mean([x - y for x, y in hi], a["n_boot"], a["boot_seed"]) if ok else dict(blocked="incomplete")
            d2 = [(val(t, other, "two_judge_mean", metric), val(t, base, "two_judge_mean", metric)) for t in tasks]
            m["p_sign_flip"] = (sign_flip_p([x - y for x, y in d2], a["n_boot"], a["boot_seed"])
                                if all(x is not None and y is not None for x, y in d2) else None)
            entry["metric"][metric] = m
        t = entry["metric"]["total"]
        g = lambda k, f: (t[k].get(f) if isinstance(t[k], dict) else None)
        pos = all(g(k, "lo") is not None and g(k, "lo") > 0 for k in ("jev", "astra", "bound_lo"))
        neg = all(g(k, "hi") is not None and g(k, "hi") < 0 for k in ("jev", "astra", "bound_hi"))
        # frozen claim rule: same sign under each judge alone AND under the worst-case resolution of unresolved items
        entry["direction_supported"] = "higher" if pos else ("lower" if neg else "none")
        comps.append(entry)
    adj = holm({f"{c['system']}-{c['baseline']}": c["metric"]["total"]["p_sign_flip"] for c in comps})
    for c in comps:
        c["p_holm"] = adj[f"{c['system']}-{c['baseline']}"]
    per_task = [dict(task_id=t, **{s: dict(status=(by.get((t, s)) or {}).get("status", "not_scheduled"),
                                           jev=val(t, s, "jev", "total"), astra=val(t, s, "astra", "total"),
                                           cost_usd=(by.get((t, s)) or {}).get("cost_usd")) for s in systems}) for t in tasks]
    return dict(systems=table, comparisons=comps, per_task=per_task, n_tasks=len(tasks))


# ---------------------------------------------------------------- rendering

def _f(x, nd=3):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def _ci(i: dict) -> str:
    return "blocked" if not isinstance(i, dict) or i.get("lo") is None else f"{_f(i['mean'])} [{_f(i['lo'])}, {_f(i['hi'])}]"


def render_markdown(res: dict) -> str:
    s = res["summary"]
    kind = "MOCK DATA, NOT A RESULT" if res["mock"] else ("scored main set" if res["scored"] else "SMOKE TEST, UNSCORED: operational check only")
    L = [f"# Results: {kind}", "",
         f"Generated from `results.json`. config `{res['config_sha256'][:12]}`, manifest `{res['manifest_sha256'][:12]}`, "
         f"commit `{(res.get('git') or {}).get('commit')}`, model `{res['model']}`, {s['n_tasks']} tasks, one run per configuration.", "",
         "Scores are ResearchRubrics rubric compliance (unclipped; 1.0 = all positive weight met), judged from the report text. "
         "They are not independently verified factual accuracy, and citation criteria check presence, not source support.", "",
         "## Systems", "",
         "| system | scheduled | with report | statuses | total: Jev | total: Astra | two-judge mean | consensus bounds | no-citation criteria (Jev / Astra) | citation-dependent (Jev / Astra) | unresolved / expected | $ total | $ max run | Exa $ |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, t in s["systems"].items():
        g, ad = t["grading"], t["adapted"]
        L.append(f"| {name} | {t['scheduled']} | {t['with_report']} | {t['status_counts']} | {_f(t['total']['jev'])} | {_f(t['total']['astra'])} | "
                 f"{_f(t['total']['two_judge_mean'])} | {_f(t['total']['consensus_lo'])} to {_f(t['total']['consensus_hi'])} | "
                 f"{_f(ad['adapted_no_citation']['jev'])} / {_f(ad['adapted_no_citation']['astra'])} | "
                 f"{_f(ad['adapted_citation_dependent']['jev'])} / {_f(ad['adapted_citation_dependent']['astra'])} | "
                 f"{g['disagree'] + g['unresolved_error']} / {g['expected']} | {_f(t['cost_usd_total'], 2)} | {_f(t['cost_usd_max'], 2)} | {_f(t['exa_usd_total'], 2)} |")
    L += ["", "## Paired comparisons (task-level differences, 95% bootstrap interval over tasks)", "",
          "| comparison | metric | two-judge mean | Jev | Astra | worst case for the first system | best case | wins/losses/ties | p (sign-flip) | p (Holm, 3) | direction supported under every view |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in s["comparisons"]:
        for metric, m in c["metric"].items():
            tj = m["two_judge_mean"]
            wl = f"{tj.get('wins')}/{tj.get('losses')}/{tj.get('ties')}" if tj.get("lo") is not None else "n/a"
            L.append(f"| {c['system']} minus {c['baseline']} | {metric} | {_ci(tj)} | {_ci(m['jev'])} | {_ci(m['astra'])} | {_ci(m['bound_lo'])} | {_ci(m['bound_hi'])} | "
                     f"{wl} | {_f(m['p_sign_flip'])} | {_f(c['p_holm']) if metric == 'total' else ''} | {c['direction_supported'] if metric == 'total' else ''} |")
    L += ["", "## Usage per run (means)", "", "| system | $ | model calls | research calls | minutes | report chars | output tokens | input tokens (all kinds) | retries | policy violations |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for name, t in s["systems"].items():
        m = t["means"]
        L.append(f"| {name} | {_f(m['cost_usd'], 2)} | {_f(m['llm_calls'], 1)} | {_f(m['tool_calls'], 1)} | {_f((m['wall_s'] or 0) / 60, 1)} | "
                 f"{_f(m['report_chars'], 0)} | {_f(m['output_tokens'], 0)} | {_f(m['input_tokens_all'], 0)} | {t['retries']} | {t['policy_violations']} |")
    sysn = list(s["systems"])
    L += ["", "## Every task (total score: Jev / Astra, and status)", "", "| task | " + " | ".join(sysn) + " |", "|---|" + "---|" * len(sysn)]
    for row in s["per_task"]:
        L.append(f"| {row['task_id']} | " + " | ".join(f"{_f(row[n]['jev'], 2)} / {_f(row[n]['astra'], 2)} ({row[n]['status']})" for n in sysn) + " |")
    g = res["grading"]
    L += ["", "## Grading account", "",
          f"- Jev: {g['jev']['attempts']} attempts, {g['jev']['ok']} verdicts, ${g['jev']['cost_usd']}, long-context judgments {g['jev']['long_context_judgments']}, error attempts {g['jev']['error_states'] or 'none'}",
          f"- Astra (Codex CLI): {g['astra']['cli_sessions']} sessions, {g['astra']['attempts']} item attempts, {g['astra']['ok']} verdicts, error attempts {g['astra']['error_states'] or 'none'}"]
    return "\n".join(L) + "\n"
