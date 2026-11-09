"""Pinned loader for ResearchRubrics, normalised to the task schema used by the runner and graders.

Questions and rubrics are stored apart:
  data/public/tasks.jsonl   task_id, dataset, question, strata      (the only file a run reads)
  data/eval/rubrics.jsonl   task_id, criteria                       (evaluator side only)

Rubric text and weights are kept verbatim. Known upstream oddities are flagged, not repaired.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .util import ROOT, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl

PUBLIC = ROOT / "data" / "public" / "tasks.jsonl"
EVAL_DIR = ROOT / "data" / "eval"
RUBRICS = EVAL_DIR / "rubrics.jsonl"

TIME_RE = re.compile(
    r"\b(latest|current(ly)?|recent(ly)?|as of|up[- ]to[- ]date|to date|today|now|this year|last year|next year|"
    r"upcoming|newest|state[- ]of[- ]the[- ]art|ongoing|emerging|so far|present[- ]day|20(2[4-9]|3\d)|"
    r"past (\d+|few|two|three|five|ten) (years?|months?|weeks?))\b", re.I)
CITE_RE = re.compile(r"\b(cit(e|es|ed|ing|ation|ations)|sources?|sourced|references?|referenced|links?|urls?|"
                     r"bibliograph\w*|footnotes?|doi)\b", re.I)

RR_BUCKET = {"Explicit Criteria": "coverage", "Implicit Criteria": "coverage",
             "Synthesis of Information": "content", "References & Citation Quality": "citation",
             "Communication Quality": "presentation", "Instruction Following": "presentation",
             "Miscellaneous": "other"}


def _download(spec: dict) -> Path:
    from huggingface_hub import hf_hub_download
    p = Path(hf_hub_download(repo_id=spec["repo"], repo_type="dataset", revision=spec["revision"],
                             filename=spec["file"], local_dir=ROOT / "data" / "raw" / spec["repo"].replace("/", "__")))
    got = sha256_file(p)
    if got != spec["sha256"]:
        raise RuntimeError(f"{spec['repo']}@{spec['revision']}: sha256 {got} != pinned {spec['sha256']}")
    return p


def _criterion(cid: str, text: str, weight: float, category: str, bucket: str) -> dict:
    return dict(cid=cid, text=text, weight=weight, category=category, bucket=bucket,
                polarity=-1 if weight < 0 else 1,
                citation_dependent=bucket == "citation" or bool(CITE_RE.search(text)))


def load_researchrubrics(spec: dict) -> list[dict]:
    tasks = []
    for i, r in enumerate(read_jsonl(_download(spec))):
        flags, seen, crit = [], set(), []
        for j, c in enumerate(r["rubrics"]):
            if c["weight"] == 0:
                flags.append("zero_weight_criterion")
            if abs(c["weight"]) > 5:
                flags.append("weight_outside_documented_range")
            if c["criterion"] in seen:
                flags.append("duplicate_criterion")
            seen.add(c["criterion"])
            crit.append(_criterion(f"c{j:02d}", c["criterion"], float(c["weight"]), c["axis"],
                                   RR_BUCKET.get(c["axis"], "other")))
        tasks.append(dict(task_id=f"rr-{r['sample_id'][-6:]}", dataset="researchrubrics", upstream_id=r["sample_id"],
                          upstream_row=i, question=r["prompt"], criteria=crit, labels=["met", "not_met"],
                          strata=dict(domain=r["domain"], breadth=r["conceptual_breadth"], nesting=r["logical_nesting"],
                                      exploration=r["exploration"], time_sensitive=bool(TIME_RE.search(r["prompt"]))),
                          upstream_flags=sorted(set(flags))))
    return tasks


LOADERS = {"researchrubrics": load_researchrubrics}


def prepare(cfg: dict) -> dict:
    """Download at the pinned revision, verify the hash, write the public/eval split, check the frozen selection."""
    all_tasks = []
    for name, spec in cfg["datasets"].items():
        all_tasks += LOADERS[name](spec)
    ids = [t["task_id"] for t in all_tasks]
    assert len(ids) == len(set(ids)), "task id collision"
    write_jsonl(PUBLIC, [{k: t[k] for k in ("task_id", "dataset", "question", "strata", "upstream_flags")} for t in all_tasks])
    write_jsonl(RUBRICS, [{k: t[k] for k in ("task_id", "dataset", "upstream_id", "upstream_row", "criteria", "labels")}
                          for t in all_tasks])
    sp = load_splits()["splits"]
    assert len(sp["main"]) == cfg["sampling"]["main"] and len(sp["smoke"]) == cfg["sampling"]["smoke"], "selection size differs from config"
    assert not set(sp["main"]) & set(sp["smoke"]), "smoke and main questions must be disjoint"
    assert set(sp["main"]) | set(sp["smoke"]) <= set(ids), "frozen selection names a task that is not in the pinned dataset"
    return {"tasks": len(all_tasks), "main": len(sp["main"]), "smoke": len(sp["smoke"]),
            "main_criteria": sum(len(t["criteria"]) for t in all_tasks if t["task_id"] in sp["main"]),
            "smoke_criteria": sum(len(t["criteria"]) for t in all_tasks if t["task_id"] in sp["smoke"])}


def load_public() -> dict[str, dict]:
    return {t["task_id"]: t for t in read_jsonl(PUBLIC)}


def load_with_rubrics() -> dict[str, dict]:
    """Evaluator side only. The run path never calls this (tests/test_offline.py checks)."""
    pub = load_public()
    for r in read_jsonl(RUBRICS):
        pub[r["task_id"]].update(criteria=r["criteria"], labels=r["labels"])
    return pub


def load_splits() -> dict:
    return json.loads((ROOT / "configs" / "splits.json").read_text())
