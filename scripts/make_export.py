"""Build the public export from an explicit allowlist of individual files. Pushes nothing.

    python3 scripts/make_export.py            build ../harness-bench-public, scan, write the review bundle
    python3 scripts/make_export.py --commit   additionally create one initial commit there (after review)

The working repository, its history and everything not on the allowlist stay private. Prompt texts
are withheld: only their hashes are exported. Scans catch obvious credentials, local paths and
names; they are not proof that no private prose is present. Read export/REVIEW.diff.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT.parent / "harness-bench-public"
PATTERNS = {
    "credential": re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|"
                             r"(?i:api[_-]?key|token|secret)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{16,}"),
    "local_path": re.compile(r"/Users/|/home/[a-z]|/private/tmp|C:\\\\Users"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"),
    "private_reference": re.compile(r"(?i)\baudit\b|STATUS-PRIVATE|chat transcript|coding agent"),
}
ALLOWED_HITS = {("email", "git@github.com"), ("email", "noreply@anthropic.com")}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git(*a: str, cwd: Path = ROOT) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    names = [l.strip() for l in (ROOT / "export" / "ALLOWLIST.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    bad = [n for n in names if any(c in n for c in "*?[") or n.endswith("/") or ".." in n or not (ROOT / n).is_file()]
    if bad:
        print("allowlist entries must be existing individual files:", bad); return 2
    owner = {git("config", "user.name"), git("config", "user.email"), Path.home().name} - {""}
    if DEST.exists():
        shutil.rmtree(DEST)
    hits, manifest = [], {}
    for n in names:
        src, dst = ROOT / n, DEST / n
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        manifest[n] = sha(dst)
        text = dst.read_text(errors="replace")
        for kind, rx in PATTERNS.items():
            for m in rx.finditer(text):
                if (kind, m.group(0)) not in ALLOWED_HITS and not (n == "scripts/make_export.py"):
                    hits.append((n, kind, text.count("\n", 0, m.start()) + 1, m.group(0)[:60]))
        for o in owner:
            if o and o.lower() in text.lower():
                hits.append((n, "owner_identity", 0, "<name, email or username from this machine>"))
    prompts = {p.name: sha(p) for p in sorted((ROOT / "prompts").glob("*.txt"))}
    (DEST / "PROMPT_HASHES.json").write_text(json.dumps(dict(
        note="Prompt texts are withheld pending review. These SHA-256 hashes identify the exact prompts used.", sha256=prompts), indent=2) + "\n")
    head, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    (DEST / "EXPORT_MANIFEST.json").write_text(json.dumps(dict(
        working_repository_commit=head or None, working_tree_clean=not dirty, working_tags=git("tag", "--points-at", "HEAD").splitlines(),
        files=manifest), indent=2) + "\n")
    tracked = set(git("ls-files").splitlines())
    withheld = sorted(tracked - set(names))
    review = ["# Export review", "", f"Destination: a sibling directory named `{DEST.name}` (fresh history, nothing pushed).",
              f"Working commit: `{head or 'none yet'}`{' (tree not clean)' if dirty else ''}.", "",
              f"## Exported: {len(names)} files plus PROMPT_HASHES.json and EXPORT_MANIFEST.json", "",
              "| file | bytes | sha256 (first 12) |", "|---|---|---|"]
    review += [f"| {n} | {(DEST / n).stat().st_size} | {manifest[n][:12]} |" for n in names]
    review += ["", f"## Tracked in the working repository but NOT exported: {len(withheld)}", ""] + [f"- {w}" for w in withheld]
    review += ["", "## Scan hits (each needs a decision)", ""] + ([f"- `{n}` line {ln}: {kind}: `{frag}`" for n, kind, ln, frag in hits] or ["- none"])
    review += ["", "A clean scan does not prove private prose is absent. Read `export/REVIEW.diff`, which is every exported line."]
    (ROOT / "export" / "REVIEW.md").write_text("\n".join(review) + "\n")
    diff = []
    for n in names + ["PROMPT_HASHES.json", "EXPORT_MANIFEST.json"]:
        r = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", n], cwd=DEST, capture_output=True, text=True)
        diff.append(r.stdout)
    (ROOT / "export" / "REVIEW.diff").write_text("".join(diff))
    print(f"exported {len(names)} files to {DEST.name}/; {len(withheld)} tracked files withheld; {len(hits)} scan hits")
    print("review: export/REVIEW.md and export/REVIEW.diff")
    if "--commit" in sys.argv:
        if hits:
            print("not committing: resolve the scan hits first"); return 1
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=DEST, check=True)
        subprocess.run(["git", "add", "-A"], cwd=DEST, check=True)
        print("the commit will carry this author identity, which becomes public:", git("config", "user.name"), git("config", "user.email"))
        subprocess.run(["git", "commit", "-q", "-m", "Round 1: frozen protocol, configuration, selection and code"], cwd=DEST, check=True)
        print("committed", git("rev-parse", "HEAD", cwd=DEST), "in", DEST.name, "(not pushed)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
