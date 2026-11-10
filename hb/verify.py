"""Offline verification of a campaign directory. No network, no spend.

FAIL blocks reporting. WARN must be disclosed with the results.
"""
from __future__ import annotations

import json
from pathlib import Path

from .analysis import build_results, render_markdown
from .budget import Budget
from .grading import ERROR_STATES, OK, expected_items, final_grades
from .runner import campaign_commits
from .util import ROOT, canon, read_json, read_jsonl, sha256_text


def verify(cfg: dict, cdir: Path, tasks: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []

    def add(level: str, check: str, detail: str = "") -> None:
        out.append((level, check, detail))

    manifest = read_jsonl(cdir / "manifest.jsonl")
    camp = read_json(cdir / "campaign.json")
    model, main, mock = cfg["model"]["id"], camp["split"] == "main", camp.get("mock", False)
    bad = "FAIL" if main and not mock else "WARN"

    # 1 frozen configuration and commit
    add("PASS" if camp["config_sha256"] == cfg["_sha256"] else "FAIL", "configuration unchanged since the manifest was frozen",
        f"campaign {camp['config_sha256'][:12]} vs current {cfg['_sha256'][:12]}")
    g = camp.get("git") or {}
    frozen = bool(g.get("commit")) and not g.get("dirty") and any(t.startswith(cfg["freeze"]["tag_prefix"]) for t in g.get("tags", []))
    add("PASS" if frozen else bad, "campaign was scheduled from a clean, freeze-tagged commit", str(g))

    # 2 every scheduled run is accounted for
    metas, missing = {}, []
    for row in manifest:
        mp = cdir / "outputs" / row["run_id"] / "meta.json"
        if mp.exists():
            metas[row["run_id"]] = read_json(mp)
        else:
            missing.append(row["run_id"])
    add("FAIL" if missing else "PASS", "every scheduled run has a terminal record", f"{len(missing)} missing: {missing[:5]}")
    extra = {p.parent.name for p in cdir.glob("outputs/*/meta.json")} - {r["run_id"] for r in manifest}
    add("FAIL" if extra else "PASS", "no outputs outside the manifest", str(sorted(extra)[:5]))
    allowed = campaign_commits(camp)
    off = [rid for rid, m in metas.items() if (m.get("git") or {}).get("commit") not in allowed or (m.get("git") or {}).get("dirty")]
    add(bad if off else "PASS", "every run executed at the campaign's frozen commit or a recorded amendment commit, with a clean tree", str(off[:5]))
    for r in camp.get("revisions", []):
        rtag = any(t.startswith(cfg["freeze"]["tag_prefix"]) for t in r["git"].get("tags", [])) and not r["git"].get("dirty")
        add("PASS" if rtag else bad, f"schedule revision {r['revision']} was made from a clean, freeze-tagged amendment commit", r["amendment"])
        add("PASS" if sha256_text((cdir / "manifest.jsonl").read_text()) == r["manifest_sha256"] or r is not camp["revisions"][-1] else "FAIL",
            f"manifest matches schedule revision {r['revision']}", f"{r['kept_runs']} runs kept, {len(r['removed_runs'])} removed")
        add("INFO", f"schedule revision {r['revision']}: runs outside the retained set (preserved, not scored)",
            f"{len(r['removed_runs'])} runs, ${r['removed_spend_usd']:.2f} model and ${r['removed_tool_spend_usd']:.2f} research already spent; "
            f"{len(r['operator_stopped_runs'])} in-flight runs stopped and re-queued, ${r['operator_stopped_spend_usd']:.2f}")

    # 3 model, effort, tools, cost accounting
    bad_model, viol, under, cost_bad, cb_tools, eff, intr, retried = [], [], [], [], [], [], [], []
    for rid, m in metas.items():
        led = m["ledger"]
        if set(led["models_seen"]) - {model}:
            bad_model.append((rid, led["models_seen"]))
        kinds = sorted({v["kind"] for l in m["attempt_ledgers"] for v in l["violations"]})
        if "reservation_underestimate" in kinds:
            under.append(rid)
        if set(kinds) - {"reservation_underestimate"}:
            viol.append((rid, kinds))
        if set(led["efforts_seen"]) - {m["policy"]["effort"]}:
            eff.append((rid, led["efforts_seen"]))
        if m.get("interrupted") or m["status"].endswith("_interrupted"):
            intr.append((rid, m["status"], m.get("interrupted")))
        if m["attempts"] > 1:
            retried.append((rid, m["attempts"], m.get("earlier_failed_attempts_cost_usd")))
        traced = 0.0
        for rec in read_jsonl(cdir / "outputs" / rid / f"attempt_{m['attempts']}" / "trace.jsonl"):
            if rec["type"] == "llm":
                traced += rec.get("cost_usd", 0)
                if rec.get("status") == 200 and rec.get("response_model") != model:
                    bad_model.append((rid, rec.get("response_model")))
            if m["policy"]["allow_tools"] is False and (rec["type"] == "tool" or rec.get("tools")):
                cb_tools.append(rid)
        if abs(traced - led["cost_usd"]) > 1e-4:
            cost_bad.append((rid, round(traced, 4), led["cost_usd"]))
    add("FAIL" if bad_model else "PASS", f"every generative call was served by {model}", str(bad_model[:3]))
    add("WARN" if eff else "PASS", "every served call ran at the pinned effort", str(eff[:3]))
    add("WARN" if viol else "PASS", "no policy violations attempted (wrong model, fallback, server tool, closed-book tool)", str(viol[:5]))
    add("FAIL" if cb_tools else "PASS", "closed-book runs sent no tools and made no tool calls", str(sorted(set(cb_tools))[:5]))
    add("FAIL" if cost_bad else "PASS", "per-call trace cost reconciles with each run's cost", str(cost_bad[:3]))
    add("WARN" if under else "PASS", "no call cost more than the worst case held for it", str(under[:5]))
    add("WARN" if intr else "PASS", "every run finished on its own (none interrupted by the funding ceiling or a safeguard)", str(intr[:6]))
    add("INFO", "runs re-attempted after an infrastructure failure (cost reported separately)", str(retried[:6]) if retried else "none")

    # 4 persistent spending ledger
    for prov in ("anthropic", "exa"):
        st = Budget(prov, cfg, mock).status()
        add("FAIL" if st["committed"] > st["ceiling_usd"] + 1e-6 else "PASS",
            f"{prov} ledger: committed ${st['committed']:.2f} within the ${st['ceiling_usd']:.2f} ceiling")
        add("FAIL" if st["open"] else "PASS", f"{prov} ledger: no unreconciled entries", str(st["open"][:3]))

    # 5 leak scan: benchmark material must not have reached a contestant
    leaks = []
    needles = [d.lower() for d in cfg["tools"].get("blocked_domains", [])] + \
              [s.lower() for s in cfg["tools"].get("blocked_url_substrings", [])] + ["data/eval", "rubrics.jsonl"]
    for rid in metas:
        for f in list((cdir / "outputs" / rid).glob("attempt_*/tools/*.json")) + list((cdir / "outputs" / rid).glob("attempt_*/std*.log")):
            txt = f.read_text(errors="replace").lower()
            if f.suffix == ".json":
                j = json.loads(txt)
                if "error" in (j.get("result") or {}):
                    continue  # refused at the gateway: counted in the trace, nothing was returned
                txt = canon(j.get("result"))
            hit = [n for n in needles if n in txt]
            if hit:
                leaks.append((rid, f.name, hit[:2]))
    add("FAIL" if leaks else "PASS", "no benchmark source or evaluator path appears in contestant-visible content", str(leaks[:5]))

    # 6 grading completeness and failure-state hygiene
    grades = final_grades(cfg, cdir, tasks)
    exp = expected_items(cdir, tasks)
    add("PASS" if len(grades) == len(exp) else "FAIL", "one grade row per expected (run, criterion)", f"{len(grades)} of {len(exp)}")
    for j in ("jev", "astra"):
        st = [g2[f"{j}_state"] for g2 in grades]
        miss, err = st.count("missing"), sum(s in ERROR_STATES for s in st)
        add("FAIL" if miss else "PASS", f"{j}: every expected judgment was attempted", f"{miss} never attempted")
        add("WARN" if err else "PASS", f"{j}: no judgment left in an error state", f"{err} unresolved (kept unresolved, never scored as failures)")
        att = read_jsonl(cdir / "grades" / f"{j}.jsonl")
        leaked = [a for a in att if a["state"] != OK and a.get("label") is not None]
        add("FAIL" if leaked else "PASS", f"{j}: no error attempt carries a usable label", str(len(leaked)))
    wrongv = {a.get("model_version") for a in read_jsonl(cdir / "grades" / "jev.jsonl") if a["state"] == OK} - {cfg["judges"]["jev"]["model"]}
    add("FAIL" if wrongv else "PASS", "jev: verdicts come from the pinned version", str(wrongv))
    sess = [read_json(p) for p in (cdir / "grades" / "astra_sessions").glob("*/record.json")]
    used = [s["id"] for s in sess if s.get("state") == "completed" and (s.get("foreign_items") or s.get("mcp_catalogs") or s.get("files_created"))]
    add("FAIL" if used else "PASS", "astra: no accepted session used a tool, was offered a tool catalog, or wrote a file", str(used[:3]))
    unk = [s["id"] for s in sess if s.get("state") == "completed" and not s.get("mock") and s.get("model") not in (cfg["judges"]["astra"]["model"],)]
    add("WARN" if unk else "PASS", "astra: the session log names the requested model", f"{len(unk)} sessions where the log did not confirm it")
    first_jev = {}
    for a in read_jsonl(cdir / "grades" / "jev.jsonl"):
        first_jev.setdefault((a["run_id"], a["cid"]), a["ts"])
    seen, late = set(), []
    for a in read_jsonl(cdir / "grades" / "astra.jsonl"):
        k = (a["run_id"], a["cid"])
        if a["state"] == OK and k not in seen:
            seen.add(k)
            if k in first_jev and a["ts"] + a["latency_s"] > first_jev[k]:
                late.append(k)
    add("FAIL" if late else "PASS", "Astra's accepted verdict was written before Jev was called on the item", str(late[:3]))
    judged = sum(g2["has_report"] for g2 in grades)
    dis = sum(g2["consensus"] == "disagree" for g2 in grades)
    add("INFO", "judge disagreement", f"{dis} of {judged} judged items ({(dis / judged if judged else 0):.1%}); unresolved unless adjudicated")

    # 7 reported numbers regenerate from artifacts
    rp, mdp = cdir / "results.json", cdir / "RESULTS.md"
    if rp.exists():
        old = rp.read_text()
        new = build_results(cfg, cdir, tasks)
        add("PASS" if sha256_text(old) == sha256_text(rp.read_text()) else "FAIL", "results.json regenerates byte-identically from the attempt logs")
        if mdp.exists():
            add("PASS" if mdp.read_text() == render_markdown(new) else "FAIL", "RESULTS.md equals the rendering of results.json")
    else:
        add("WARN", "results.json not built yet")
    # 8 the run path cannot read rubrics
    src = "".join((ROOT / "hb" / f).read_text() for f in ("runner.py", "systems.py", "gateway.py", "budget.py"))
    add("FAIL" if "load_with_rubrics" in src or "RUBRICS" in src else "PASS", "the run path never imports evaluator data")
    return out
