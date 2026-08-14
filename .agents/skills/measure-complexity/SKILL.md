---
name: measure-complexity
description: Measure McCabe cyclomatic complexity and maintainability index across the repo with radon, flag functions over a threshold, and rank them for refactoring. Triggered by complexity analysis, cyclomatic complexity, or /measure-complexity while working in the Ava repo.
---

# Measure complexity — McCabe + maintainability (radon)

Mechanical complexity scan of this repo: cyclomatic complexity (cc) per
function via `radon cc`, maintainability index (MI) per module via `radon mi`.
Output is a ranked report — which functions to look at first, with
file:line:function. The scan is a **locator, not a verdict**: it tells you
where complexity lives; deciding what to refactor is agent judgment (Layer 2
below).

## Install radon

radon is **not** a dev dependency (pyproject.toml does not list it). Three ways:

- **One-off (recommended, zero repo change)** — the repo is uv-based:
  `uvx radon ...` (uv downloads radon into its cache on first use).
- **Repeated local use** — install into the repo venv:
  `.venv/bin/pip install radon` (a later `uv sync` removes it — reinstall when
  needed).
- **Lock it in as a dev dependency** — `uv add --dev radon` if the team wants
  it in CI/dev; that is a separate decision, not required for this skill.

## Run it

### Single file
```
uvx radon cc -a -s path/to/file.py
```

### One directory
```
uvx radon cc -a -s path/to/package/
```

### Whole repo
```
uvx radon cc -a -s -j .
```
Hidden directories (`.git`, `.venv`, `.claude`, …) are auto-ignored, so
running from the repo root is safe. To scope to the Python source tree only
(mirrors the sweeper's scope, skips `tests/` noise):
```
uvx radon cc -a -s ava/ agent/ gateway/ cli/ services/ shared/ plugins/
```

- `-s` shows the numeric complexity next to the A–F rank.
- `-a` prints the repo (or target) average at the end — a cheap trend signal.
- `-j` emits JSON (`{path: [{name, lineno, col_offset, endline, complexity, rank, type, ...}]}`)
  for machine processing.

### Flag cc > threshold (default 10) and rank

`radon cc` has no threshold flag, but `-n <n>` sets the **minimum complexity
to display** (inclusive), so a threshold of 10 means `-n 11`:

```
uvx radon cc -s -n 11 -o SCORE .
```
`-o SCORE` sorts by complexity descending — this is the ranked report.

For a machine-readable ranked list (stdlib only, no jq), filter and sort the
JSON:

```
.venv/bin/python - <<'PY'
import json, subprocess, sys
targets = sys.argv[1:] or ["."]
data = json.loads(subprocess.check_output(
    ["uvx", "radon", "cc", "-a", "-s", "-j", *targets]))
rows = []
for path, funcs in data.items():
    for f in funcs:
        if f["complexity"] > 10:  # threshold, default 10
            rows.append((f["complexity"], f"{path}:{f['lineno']}", f["name"]))
for cc, loc, name in sorted(rows, reverse=True):
    print(f"{cc:4d}  {loc}  {name}")
PY
```
The text mode line `F 53:0 find - A (5)` reads: type `F`(unction)/`M`(ethod),
`line:col`, name, rank, `(complexity)`.

### Maintainability index

```
uvx radon mi -s .
```
Per-module MI (0–100, computed from Halstead volume, cc, LLOC, and comment
ratio). Ranks: **A** > 19, **B** 9–19, **C** ≤ 9. Low MI modules are
structurally hard to maintain even where individual functions look fine — pair
the MI report with the cc hotspots.

## Interpreting cc

One plus the number of decision points (roughly: each `if`/`elif`, `for`,
`while`, `except`, boolean `and`/`or`, ternary, `assert`, comprehension, and
`match` case; `with` and `else`/`finally` do not count).

| cc | band | reading |
|----|------|---------|
| 1–5  | simple (radon A) | fine as-is |
| 6–10 | moderate (radon B) | watch; fine when the logic genuinely is that branched |
| 11–20 | complex (radon C) | refactor candidates — extract methods, simplify conditions |
| 21+ | very complex (radon D–F) | priority debt; bugs concentrate here |

Threshold is a judgment dial: the skill defaults to flagging cc > 10
(`-n 11`), but a domain rule that is inherently a decision table (a parser,
a state machine) can legitimately sit higher — the report is a locator, not a
gate.

## Using the report to prioritize refactoring (Layer 2 — agent judgment)

The scan is Layer 1 (mechanical). Layer 2 is reading the flagged functions and
deciding:

1. **Rank by cc × heat × age.** Start with the highest cc in modules the team
   touches often; `git blame` tells you whether a hotspot is old accumulated
   debt (refactor now) or fresh code (review it now).
2. **Cross-check MI.** A module with rank C MI is hard to test and change no
   matter how its functions score — low MI plus a cc hotspot is the strongest
   signal.
3. **Read before prescribing.** A high cc from a long `elif` chain often
   collapses into a dispatch table or dict lookup; a high cc from nested
   conditionals usually wants early returns / guard clauses; a wide function
   with flat cc wants extraction, not restructuring. Suggest the shape, don't
   mechanically split.
4. **Lock behavior first.** Any refactor of a cc > 10 function ships with (or
   is preceded by) tests that pin its behavior, so the cc drop is provably
   behavior-preserving.
5. **Prove the drop.** Re-run the scan after the refactor and show the
   before/after cc for the touched functions in the PR description.

This is advisory — nothing here is CI-enforced, and radon is not a dev
dependency; if the team later wants a gate, `-n` thresholds or the JSON recipe
above are the building blocks.
