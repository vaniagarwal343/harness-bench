# Freezing, publishing and amending

## What is frozen
At one tagged commit of the working repository: `docs/PROTOCOL.md`, `configs/study.toml`, `configs/splits.json`, the code, the dependency pins, and the hashes of every prompt. The main campaign refuses to be scheduled or run from any other state: the tree must be clean and HEAD must carry a `freeze-` tag. Every run records the full commit SHA and the configuration hash, and `hb verify` checks they all match the campaign's.

## Public export
The working repository stays private, with its history. The public copy is a separate directory built from an explicit list of individual files (`export/ALLOWLIST.txt`) with a fresh Git history.

```bash
python3 scripts/make_export.py            # build ../harness-bench-public, scan it, write the review bundle
#   read export/REVIEW.md and export/REVIEW.diff in full
python3 scripts/make_export.py --commit   # only after review: create the single initial commit in the export
```

The script copies only allowlisted files, adds `PROMPT_HASHES.json` and `EXPORT_MANIFEST.json` (per-file hashes and the working repository's commit SHA), and scans the result for credential patterns, local paths and names. A clean scan is not proof that nothing private is present; the review bundle exists so a person reads every exported line. Nothing is pushed by the script.

Publishing, after review:
1. Create an empty public repository and push the export's single commit.
2. Tag it with the same name as the working repository's freeze tag.
3. Create a release from that tag with `configs/study.toml`, `configs/splits.json`, `docs/PROTOCOL.md`, `PROMPT_HASHES.json` and `EXPORT_MANIFEST.json` attached as assets.
4. Turn on release immutability for the repository and add a tag rule that blocks deleting or moving `freeze-*` tags.
5. Record the public commit SHA and release URL in the working repository.

## Prompts
Contestant and judge prompt texts are withheld until reviewed for release. Their SHA-256 hashes are published, which lets a later release be checked against what was run, but does not let anyone reproduce the runs in the meantime. Results from this round state that limit.

## Amendments
Any change after the freeze is a new file `amendments/NNNN-short-title.md` stating: date, what changed, why, and whether any smoke or main output existed when it was made. The change is committed, tagged `freeze-<round>-aN`, exported and released the same way. Tags are never moved or deleted, and earlier releases stay up. Results name the tag they were produced under.
