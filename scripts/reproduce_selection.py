"""Reproduce the frozen task selection in configs/splits.json from the pinned public datasets.

The selection was drawn once, on 2026-11-09, before any contestant output existed, by a seeded
sampler stratified by domain (equal allocation, 2 main questions per ResearchRubrics domain) over
ResearchRubrics and DRACO. DRACO was later deferred; the ResearchRubrics draw is unchanged. The two
smoke questions are the first two of the four ResearchRubrics development questions from the same
draw, ordered by sha256(seed + task_id). This script re-runs that procedure and compares.

    .venv/bin/python scripts/reproduce_selection.py
"""
import hashlib
import json
import random
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parent.parent
SEED = "harness-bench-2026-11"
PINS = {"draco": ("perplexity-ai/draco", "ce076749809027649ebd331bcb70f42bf720d387", "test.jsonl",
                  "e35bfe78cd827fa1d541b79fbc7bc7b91966d3227d8742c83e99d26d4ac4679a"),
        "researchrubrics": ("ScaleAI/researchrubrics", "85de3115053d1453ed612caacf4a405edc1ad756", "processed_data.jsonl",
                            "ea2023d03953b70ba4a2f7f1fe490b434f18cec26c4fdb48acc0e3edf3d8edfb")}


def rows(name):
    repo, rev, fn, sha = PINS[name]
    p = Path(hf_hub_download(repo_id=repo, repo_type="dataset", revision=rev, filename=fn,
                             local_dir=ROOT / "data" / "raw" / repo.replace("/", "__")))
    assert hashlib.sha256(p.read_bytes()).hexdigest() == sha, f"{name}: file hash differs from the pin"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()


tasks = {"draco": [(f"draco-{r['id'][:8]}", r["domain"]) for r in rows("draco")],
         "researchrubrics": [(f"rr-{r['sample_id'][-6:]}", r["domain"]) for r in rows("researchrubrics")]}
rng = random.Random(SEED)
out = {"dev": [], "main": []}
for ds in sorted(tasks):
    pool = {}
    for tid, dom in sorted(tasks[ds], key=lambda t: h(SEED + t[0])):
        pool.setdefault(dom, []).append(tid)
    domains = sorted(pool)
    for split, quota in (("main", 20), ("dev", 4)):
        order = domains[:]
        rng.shuffle(order)
        picked = 0
        while picked < quota and any(pool[d] for d in domains):
            for d in order:
                if picked < quota and pool[d]:
                    out[split].append(pool[d].pop(0)); picked += 1
main = sorted(t for t in out["main"] if t.startswith("rr-"))
smoke = sorted(sorted((t for t in out["dev"] if t.startswith("rr-")), key=lambda t: h(SEED + t))[:2])
frozen = json.loads((ROOT / "configs" / "splits.json").read_text())["splits"]
ok = main == frozen["main"] and smoke == frozen["smoke"]
print("main", len(main), "smoke", smoke, "->", "MATCHES configs/splits.json" if ok else "DIFFERS")
sys.exit(0 if ok else 1)
