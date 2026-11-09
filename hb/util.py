"""Small shared helpers: hashing, JSONL, time, config loading, env separation."""
from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj: Any) -> str:
    """Canonical JSON used for every hash and cache key."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now() -> float:
    return time.time()


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))
    tmp.replace(p)


def read_json(p: Path) -> Any:
    return json.loads(p.read_text())


def append_jsonl(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def write_jsonl(p: Path, rows: Iterable[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(p)


# ---------------------------------------------------------------- config

def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "configs" / "study.toml"
    cfg = tomllib.loads(p.read_text())
    cfg["_path"] = str(p)
    cfg["_sha256"] = sha256_file(p)
    # Confirmed account funding is operational state: private, outside the frozen file and its hash.
    fl = p.parent / "funding.local.toml"
    cfg["confirmed_funding"] = {k: v["confirmed_usd"] for k, v in tomllib.loads(fl.read_text()).items()} if fl.exists() else {}
    return cfg


def campaign_dir(cfg: dict, campaign: str) -> Path:
    return ROOT / "runs" / campaign


# ---------------------------------------------------------------- env separation
# Contestant processes never see real provider keys or evaluator keys. They get a
# per-run token that is only valid at the local gateways. Evaluator processes
# never see contestant-side keys either.

CONTESTANT_SIDE_KEYS = ("ANTHROPIC_API_KEY", "EXA_API_KEY")
EVALUATOR_SIDE_KEYS = ("TYPESAFE_API_KEY",)  # Astra uses the Codex CLI's own signed-in session, not a key
# Anything matching these prefixes/suffixes is stripped from a contestant's environment.
_SECRET_MARKERS = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY", "TYPESAFE_", "OPENAI_", "JEV_",
                   "ANTHROPIC_", "AWS_", "GOOGLE_", "AZURE_", "LANGCHAIN_", "LANGSMITH_", "EXA_", "TAVILY_", "CODEX_")


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Parse .env without exporting it. Callers pick the keys their side may use."""
    p = path or Path(os.environ.get("HB_ENV_FILE") or ROOT / ".env")
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def secret(name: str) -> str | None:
    return os.environ.get(name) or load_dotenv().get(name)


def scrubbed_env(extra: dict[str, str]) -> dict[str, str]:
    """Environment for a contestant subprocess: no secrets, plus explicit run-scoped values."""
    env = {}
    for k, v in os.environ.items():
        if any(m in k for m in _SECRET_MARKERS) or k.startswith(("CLAUDE", "HB_")):
            continue
        env[k] = v
    env.update(extra)
    return env


def git_state() -> dict:
    """Full commit SHA, whether the tree is clean, and tags on HEAD. Stamped on every campaign and run."""
    import subprocess

    def g(*a: str) -> str:
        r = subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    sha = g("rev-parse", "HEAD")
    return dict(commit=sha or None, dirty=bool(g("status", "--porcelain")) if sha else None,
                tags=[t for t in g("tag", "--points-at", "HEAD").splitlines() if t])
