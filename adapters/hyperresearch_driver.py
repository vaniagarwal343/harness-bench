"""Hyperresearch (Claude Code) adapter.

Per run, in a fresh working directory:
  1. install the patched Hyperresearch into the directory (skills, agents, vault)
  2. point its web provider at the study gateway, switch off the Chrome lane, and set every agent role
     to the study model; reinstall so the agent files are rendered with those settings
  3. remove the host WebSearch tool from agent tool lists and add one short note to every agent and
     skill saying how to search in this run (`hyperresearch websearch`)
  4. run `claude -p "/hyperresearch <question>"` headless, inside a macOS sandbox that allows network
     access to the local gateway only and denies reading the rest of the study workspace
  5. take the pipeline's own deliverable, research/notes/final_report_<tag>.md, as the report

The pipeline, its step skills, tier classification, prompts and agent roster are otherwise upstream's.
Adaptations are listed in docs/COHORT.md.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLES = ("fetcher source_analyst loci_analyst depth_investigator corpus_critic cite_checker browser_fetcher "
         "draft_orchestrator synthesizer critics patcher polish_auditor readability_recommender").split()
NOTE = """

## Web access in this run (study adapter note)

The WebSearch and WebFetch tools are not available in this run, and there is no direct internet access.
- To search the web: `{hpr} websearch "<query>" -j` (returns title, url and a short snippet per result).
- To read a page: `{hpr} fetch "<url>" ...` exactly as described elsewhere in these instructions.
Academic-API, PDF-download and browser lanes that need direct network access will fail; use web search and fetch instead.
"""
SANDBOX = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
(deny file-read* (subpath "{real_home}/.claude") (subpath "{real_home}/.codex") (subpath "{real_home}/.config")
                 (subpath "{real_home}/Desktop") (subpath "{real_home}/Documents") (subpath "{real_home}/Downloads"))
(deny file-read* (subpath "{workspace}"))
(allow file-read* (subpath "{adapters}"))
(allow file-read* (subpath "{work}"))
(allow file-read* (subpath "{home}"))
"""


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    work = Path.cwd()
    model = os.environ["HB_MODEL"]
    question = Path(os.environ["HB_QUESTION_FILE"]).read_text()
    # The installer writes the CLI path into the prompts; give it one without spaces.
    bindir = Path(tempfile.gettempdir()) / "hb-hpr-bin"
    bindir.mkdir(exist_ok=True)
    link = bindir / "hyperresearch"
    if not link.exists():
        link.symlink_to(HERE / ".venv-hyperresearch" / "bin" / "hyperresearch")
    hpr = str(link)

    r = sh([hpr, "install", ".", "-j"], cwd=work)
    if r.returncode != 0:
        print("install failed:", r.stdout[-500:], r.stderr[-500:]); return 4
    cfg = work / ".hyperresearch" / "config.toml"
    s = cfg.read_text()
    s = s.replace('provider = "crawl4ai"', 'provider = "hb"').replace('provider = "builtin"', 'provider = "hb"')
    s = re.sub(r"(\[chrome\]\nenabled = )true", r"\1false", s)
    s += "\n[profile.full]\nmodels = { " + ", ".join(f'{x} = "{model}"' for x in ROLES) + " }\n"
    cfg.write_text(s)
    r = sh([hpr, "install", ".", "-j"], cwd=work)
    if r.returncode != 0:
        print("reinstall failed:", r.stdout[-500:], r.stderr[-500:]); return 4

    agents = sorted(glob.glob(str(work / ".claude" / "agents" / "*.md")))
    models = set()
    for f in agents:
        t = Path(f).read_text()
        models |= set(re.findall(r"^model:\s*(\S+)", t, flags=re.M))
        t = re.sub(r"^(tools:.*?)(,\s*WebSearch|WebSearch,\s*)", r"\1", t, flags=re.M)
        Path(f).write_text(t + NOTE.format(hpr=hpr))
    if models != {model}:
        print(f"agent models are {models}, expected only {model}"); return 5
    for f in glob.glob(str(work / ".claude" / "skills" / "*" / "SKILL.md")) + [str(work / "CLAUDE.md")]:
        Path(f).write_text(Path(f).read_text() + NOTE.format(hpr=hpr))
    (work / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"deny": ["WebSearch", "WebFetch", "Skill(deep-research)"]}}, indent=1))
    print(f"HB installed agents={len(agents)} models={sorted(models)} cli={hpr}")

    workspace = HERE.parent.parent  # the folder holding this and other private projects
    prof = work / "sandbox.sb"
    import pwd
    real_home = pwd.getpwuid(os.getuid()).pw_dir  # the account's real home, whatever $HOME says: personal skills, memory and settings live there
    prof.write_text(SANDBOX.format(workspace=workspace, adapters=HERE, work=work.parent, home=os.environ["HOME"], real_home=real_home))
    personal = {p.name for p in Path(real_home, ".claude", "skills").glob("*")} | {p.stem for p in Path(real_home, ".claude", "agents").glob("*.md")}
    cmd = ["sandbox-exec", "-f", str(prof), "claude", "-p", "/hyperresearch " + question, "--model", model,
           "--disallowedTools", "WebSearch", "WebFetch", "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json", "--verbose"]
    with open(work / "claude_stream.jsonl", "w") as out, open(work / "claude_stderr.log", "w") as err:
        rc = subprocess.run(cmd, cwd=work, stdout=out, stderr=err, stdin=subprocess.DEVNULL).returncode
    foreign = []
    try:
        init = json.loads((work / "claude_stream.jsonl").read_text().splitlines()[0])
        loaded = set(init.get("skills", [])) | set(init.get("slash_commands", [])) | set(init.get("agents", []))
        foreign = sorted(loaded & personal) + [m.get("name") for m in init.get("mcp_servers", [])]
        print(f"HB claude_code_version={init.get('claude_code_version')} session_model={init.get('model')} foreign={foreign}")
    except Exception as e:
        print(f"HB could not read the session init event: {e!r}")
    if foreign:  # personal skills, agents or MCP servers reached the contestant: the run is invalid
        return 7
    finals = sorted(glob.glob(str(work / "research" / "notes" / "final_report_*.md")), key=os.path.getmtime)
    if finals:
        text = Path(finals[-1]).read_text()
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # drop the vault's YAML front matter, keep the report
        shutil.copyfile(finals[-1], work / "final_report_original.md")
        Path(os.environ["HB_REPORT_FILE"]).write_text(text.strip() + "\n")
    print(f"HB claude_rc={rc} final_reports={len(finals)}")
    return 0 if finals and rc == 0 else (3 if not finals else 6)


if __name__ == "__main__":
    sys.exit(main())
