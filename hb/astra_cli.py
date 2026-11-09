"""GPT-6 Astra as a judge through the official Codex CLI, on the account already signed in.

Authentication is the CLI's own: this module never reads, copies or forwards the login. API-key
variables are removed from the child environment so a paid API route cannot be taken by accident.

Each call is one fresh, non-persisted session (`codex exec --ephemeral`):
  * `--ignore-user-config` and `--ignore-rules`: personal config, MCP servers, plugins and rules are not loaded
  * the working directory is a new empty temp folder and HOME points at it; the whole prompt arrives
    on stdin, so the judge needs no tool and has nothing to read
  * model and reasoning effort are passed explicitly; web search is disabled; shell, apps, plugins,
    browser, computer-use, multi-agent and related features are switched off
  * the event stream is checked afterwards: any item other than the judge's own reasoning and message
    (a command, tool call, web search, MCP call) invalidates the whole session ("error_tool_use")
  * the final message must match a JSON schema; every item is then validated by code

Usage or rate-limit messages mark the session as quota-exhausted; the caller pauses grading. Nothing
here switches to another billing route.

STATUS: written against codex-cli 0.162.1 `exec --help`. Not yet run live; see docs/RUNBOOK.md step 2.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .grading import LABELS, PROMPT_FILE, check_astra_item, judge_prompt
from .util import now, sha256_text, write_json

FEATURES_OFF = ("apps", "plugins", "remote_plugin", "plugin_sharing", "tool_suggest", "skill_search",
                "skill_mcp_dependency_install", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                "browser_annotation_api", "in_app_browser", "computer_use", "image_generation", "view_image",
                "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "multi_agent", "goals", "hooks",
                "sleep_tool", "workspace_dependencies", "worktrees")
OWN_ITEMS = ("agent_message", "reasoning")
LIMIT_RE = re.compile(r"rate.?limit|usage limit|too many requests|status(=| )429|quota|out of credits|insufficient", re.I)
LEVEL_RE = re.compile(r"\b(ERROR|WARN)\b")
CATALOG_RE = re.compile(r'MCP binding catalog materialized.*?server_kind="([^"]*)".*?tool_count=(\d+)')
ANSI = re.compile(r"\x1b\[[0-9;]*m")
UNSET = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "EXA_API_KEY", "TYPESAFE_API_KEY")


def schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["verdicts"], "properties": {"verdicts": {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
                                   "required": ["cid", "verdict", "report_quotes", "reason"],
                                   "properties": {"cid": {"type": "string"}, "verdict": {"type": "string", "enum": LABELS},
                                                  "report_quotes": {"type": "array", "items": {"type": "string"}},
                                                  "reason": {"type": "string"}}}}}}


def build_prompt(question: str, report: str, criteria: list[dict]) -> str:
    """Only the task, the report and the rubric criteria. No system name, no other judge, no other report."""
    crit = [dict(cid=c["cid"], type="error" if c["polarity"] < 0 else "requirement", criterion=c["text"]) for c in criteria]
    return (f"{judge_prompt(PROMPT_FILE['astra'])}\n\n<research_question>\n{question}\n</research_question>\n\n"
            f"<report>\n{report}\n</report>\n\n<criteria>\n{json.dumps(crit, ensure_ascii=False, indent=1)}\n</criteria>\n")


def command(jcfg: dict, work: Path, schema_path: Path, last: Path) -> list[str]:
    cmd = ["codex", "exec", "-m", jcfg["model"], "--ignore-user-config", "--ignore-rules", "--ephemeral",
           "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(work), "--json", "--color", "never",
           "--output-schema", str(schema_path), "-o", str(last),
           "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort="{jcfg["params"]["reasoning_effort"]}"',
           "-c", 'web_search="disabled"']
    for f in FEATURES_OFF:
        cmd += ["--disable", f]
    return cmd + ["-"]  # prompt on stdin


def cli_version() -> str | None:
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None


def environment_problem(jcfg: dict) -> str | None:
    v = cli_version()
    if v != jcfg["cli_version"]:
        return f"codex --version is {v!r}; the config pins {jcfg['cli_version']!r}"
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for name in ("AGENTS.md", "AGENTS.override.md"):  # global personal instructions would be loaded into the judge
        f = home / name
        if f.exists() and f.read_text(errors="replace").strip():
            return f"{name} in the Codex home is not empty; personal instructions must not reach the judge"
    return None


def _events(stdout: str) -> list[dict]:
    out = []
    for line in (stdout or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from _find(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find(v, key)


def inspect(evs: list[dict], err: str) -> dict:
    """What the session actually did, from its event stream and info-level log."""
    clean = ANSI.sub("", err or "")
    foreign = []
    for e in evs:
        it = e.get("item") or {}
        if e.get("type") in ("item.completed", "item.started") and it.get("type") not in OWN_ITEMS:
            foreign.append(f"{it.get('type')}: {it.get('server') or ''}/{it.get('tool') or it.get('command') or ''}"[:200])
    lim = [json.dumps(e)[:300] for e in evs if e.get("type") in ("error", "turn.failed") and LIMIT_RE.search(json.dumps(e))]
    lim += [l[:300] for l in clean.splitlines() if LEVEL_RE.search(l) and LIMIT_RE.search(l)]
    usage = next((e.get("usage") for e in reversed(evs) if e.get("type") == "turn.completed"), None)
    limits = [x for e in evs for x in _find(e, "rate_limits") if x]
    return dict(foreign_items=sorted(set(foreign)), limit_messages=lim, usage=usage,
                rate_limits=limits[-1] if limits else None,
                mcp_catalogs=sorted(set((k, int(n)) for k, n in CATALOG_RE.findall(clean))),
                models_in_log=sorted(set(re.findall(r'\bmodel[="\s:]+"?(gpt-[\w.\-]+)', clean)))[:5],
                errors=[json.dumps(e)[:300] for e in evs if e.get("type") in ("error", "turn.failed")])


def judge(question: str, report: str, criteria: list[dict], jcfg: dict, sessions_dir: Path, mock: bool) -> dict:
    """One fresh session for `criteria`. Returns {"session": record, "items": {cid: attempt}}."""
    prompt = build_prompt(question, report, criteria)
    sid = sha256_text(prompt)[:16] + f"-{int(now() * 1000)}"
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "prompt.txt").write_text(prompt)
    rec: dict = dict(id=sid, ts=now(), n_criteria=len(criteria), prompt_sha256=sha256_text(prompt), prompt_chars=len(prompt),
                     requested_model=jcfg["model"], requested_effort=jcfg["params"]["reasoning_effort"], cli_version=jcfg["cli_version"])

    def all_items(state: str, error: str) -> dict:
        rec.update(state=state, error=error)
        write_json(sdir / "record.json", rec)
        return {"session": rec, "items": {c["cid"]: dict(state=state, error=error[:300]) for c in criteria}}

    if mock:
        from .mock import mock_verdict
        raw = {"verdicts": [dict(cid=c["cid"], verdict=(lab := mock_verdict("astra", LABELS[:2], report, c["text"])[0]),
                                 report_quotes=[report[:40]] if lab == "met" else [], reason="mock") for c in criteria]}
        rec.update(mock=True, model=jcfg["model"], foreign_items=[], limit_messages=[])
    else:
        prob = environment_problem(jcfg)
        if prob:
            return all_items("error_environment", prob)
        work = Path(tempfile.mkdtemp(prefix="hb-astra-"))
        schema_path, last = sdir / "schema.json", sdir / "last_message.json"
        write_json(schema_path, schema())
        cmd = command(jcfg, work, schema_path, last)
        env = {k: v for k, v in os.environ.items() if k not in UNSET}
        # HOME is the empty folder; the CLI still finds its normal login through CODEX_HOME.
        env.update(CODEX_HOME=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"), HOME=str(work), RUST_LOG="info")
        rec["command"] = [c if c != str(work) else "<empty-workdir>" for c in cmd]
        try:
            p = subprocess.run(cmd, cwd=work, input=prompt, capture_output=True, text=True, timeout=jcfg["timeout_s"], env=env)
            rc, out, err, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as e:
            out, err = ((s.decode(errors="replace") if isinstance(s, bytes) else (s or "")) for s in (e.stdout, e.stderr))
            rc, timed_out = None, True
        finally:
            leftovers = [str(x.relative_to(work)) for x in work.rglob("*")] if work.exists() else []
            shutil.rmtree(work, ignore_errors=True)
        (sdir / "events.jsonl").write_text(out or "")
        (sdir / "stderr.log").write_text(err or "")
        info = inspect(_events(out), err)
        rec.update(info, returncode=rc, timed_out=timed_out, elapsed_s=round(now() - rec["ts"], 1), files_created=leftovers,
                   model=(info["models_in_log"] or [None])[0], quota=bool(info["limit_messages"]))
        if info["limit_messages"]:
            return all_items("error_quota", "the CLI reported a usage or rate limit: " + info["limit_messages"][0])
        if info["foreign_items"] or info["mcp_catalogs"] or leftovers:
            return all_items("error_tool_use", f"the judge session used or was offered tools: {info['foreign_items'][:3]} "
                                               f"catalogs={info['mcp_catalogs']} files={leftovers[:3]}")
        if timed_out or rc != 0 or not last.exists():
            return all_items("error_other", f"codex exec failed: returncode={rc} timed_out={timed_out} {info['errors'][:1]}")
        try:
            raw = json.loads(last.read_text())
            assert isinstance(raw.get("verdicts"), list)
        except Exception as e:
            return all_items("error_malformed", f"final message is not the required JSON: {e!r}")
    by: dict[str, list] = {}
    for v in raw["verdicts"]:
        if isinstance(v, dict):
            by.setdefault(str(v.get("cid")), []).append(v)
    items = {}
    for c in criteria:
        vs = by.get(c["cid"], [])
        items[c["cid"]] = (check_astra_item(vs[0], report) if len(vs) == 1 else
                           dict(state="error_malformed", error=f"{len(vs)} verdicts returned for this criterion"))
    rec.update(state="completed", item_states={s: sum(i["state"] == s for i in items.values()) for s in {i["state"] for i in items.values()}},
               unknown_cids=sorted(set(by) - {c["cid"] for c in criteria})[:5])
    write_json(sdir / "raw_verdicts.json", raw)
    write_json(sdir / "record.json", rec)
    return {"session": rec, "items": items}
