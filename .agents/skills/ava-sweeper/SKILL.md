---
name: ava-sweeper
description: Defines Ava-specific debt classes and tracker rules for the sweeper engine. Use when the user says sweep, tech-debt sweep, debt audit, `/sweep`, or asks what debt to inspect in the Ava repo.
---

# Sweeper — Ava repo classes

These classes are governed by `conventions/lint-vs-sweeper.md` (the lint vs
sweeper boundary + the graduation test that decides which side a check lands on).

The reconcile procedure (control flow, evidence bar, entry/fingerprint format,
landing as a PR) lives in the general engine: **read `ava.skills.sweeper` and
follow it.** This skill only supplies the two repo-specific inputs the engine
asks for:

- **Tracker file:** `future/infra/debt-tracker.md` (the single living "open
  debt" view).
- **Debt classes:** the 10 below.

A standalone mechanical runner for the greppable/tool-checkable subset lives at
`.agents/skills/ava-sweeper/run.sh` (cron-friendly, zero-agent — it scans and
prints, it does NOT open a PR). Read it via `ava.files.read`.

## Debt classes

### 1. deps (whole-repo)

Run `uv pip list --outdated` (repo root) and `npm outdated --json` (in
`ui/web/`). Consider minor + major bumps only; skip patch. Flag known high-risk
majors (e.g. protobuf 6→7, marshmallow 3→4, google-genai 1→2, TypeScript 5→6) so
they can be sequenced separately. Put the **full** outdated lists in the PR body,
not the tracker; create a tracker entry only for an actionable high-risk bump.
`npm outdated` needs `ui/web/node_modules` to report real `current` versions (a
missing install reports `current: null`, inflating every package to a phantom
major). If it is missing — e.g. a fresh worktree — do not skip: make it available
first by symlinking an existing `node_modules` from another checkout, or running
`npm install` in `ui/web/`, then run the check.

### 2. docs-aging (whole-repo)

For each file under `future/`: compare its header status line against
`git log -1 --format=%ci <file>` (last commit date); check `Superseded by` /
`shipped` markers; cross-check any PR refs in the body against `gh pr view <n>` to
surface merged-but-still-listed-as-pending mismatches.

### 3. fail-fast anti-patterns (whole-repo)

`rg` across `ava/ plugins/ agent/ gateway/ cli/ services/ shared/` for:
`get() or {}`, `case _:` defaults, `(rare|shouldn't happen|almost never)`
comments. False positives in config-defaults and external boundaries are
expected — flag candidates, the human decides.

`except ...: pass` silent swallows are now enforced mechanically by
`scripts/lint_fail_fast.py` in pre-commit — do not re-flag them here.

### 4. inline-marker (whole-repo)

`rg` for `TODO|FIXME|XXX|HACK` across all source dirs, with `git blame` for age.
Cheap, historically low-yield — keep it in case a new wave slips in.

### 5. dead-code (whole-repo)

`uvx vulture <dirs> --min-confidence 80` with this exclude list (without it the
scan produces 70+ false positives reflecting framework conventions):

- FastAPI route handlers (`@app.get/post/delete/patch/...`)
- SDK exports in `ava/__init__.py`'s `__all_for_ava__`
- Pytest fixtures referenced by name
- LangGraph node functions registered via `graph.add_node(name, fn)`
- Plugin hook callbacks (`@register_after_exec`, etc.)
- Pydantic schemas referenced only by wire-format-freezing tests
- Watchdog event handler methods (`on_created` / `on_modified` etc.)

The exclude list is itself an artifact — when it drifts (starts hiding real dead
code, or a new convention appears), say so in the PR body.

### 6. boundary (anchored on recent PRs — the reasoned class)

Architectural / responsibility debt that only surfaces by reading across files:
unclear module boundaries, overlapping responsibilities, multiple recent PRs each
pushing into the same place. **Not greppable.** Anchor discovery on PRs merged
since the watermark — `git log --oneline <last-swept-sha>..HEAD` and
`gh pr list --state merged --limit 30 --json number,title,mergedAt,files` — plus
the modules those PRs touched. If the watermark is `none` (first run), anchor on
the last 20–30 merged PRs/commits instead. Reason about boundary smells there. Be
conservative; this class has no calibrated baseline yet.

### 7. skill-desc (whole-repo)

A skill's `description` is its index line, and
`AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT` now defaults to `*` — so every
installed skill's description sits in every system prompt, and the cost is
universal rather than scoped to a chosen few. One skill's slack is paid by every
agent on the cluster, on every turn. Length is measured in *units* (one CJK 字
= 1, one non-CJK word = 1; a flat char count is unfair across languages). The
hard ceiling (80 units) is enforced mechanically by
`scripts/lint_skill_descriptions.py` in pre-commit; this class covers the
**soft zone (50-80 units)**: descriptions that pass the gate but should be
tightened. Reuse the lint's own scope + helpers (so this audit never drifts from
what the gate scans — `skills/`, `plugins/*/skills/`, and `.agents/skills/`):

```
python - <<'PY'
from scripts.lint_skill_descriptions import length_units, _skill_md_files, _description
for f in _skill_md_files():
    d = _description(f)
    if d and 50 < length_units(d) <= 80:
        print(length_units(d), f.parent.name)
PY
```

Unlike a line count, trimming a description needs judgment — keep what it does
plus the trigger keywords, push detail into the body. Propose tighter wording;
don't mechanically chop. Injected (resident) skills are the priority.

### 8. docstring-budget (whole-repo)

Agent-visible docstrings render into every agent's system prompt (the default
AVA_SDK_EXPAND list inlines the high-frequency namespaces in full), so every
sentence is a per-agent, per-turn token cost. The zero-false-positive core
(CJK, impl keywords, module-doc child restating, SDK<->skill coupling) is the
`lint-docstrings` pre-commit gate; this class covers the **judgement residue**
of the zero-based budget (`conventions/sdk-docstring-discipline.md`):

- **A `Raises:` section** — legitimate only when the function must teach an
  input format anyway (cron expressions, duration strings) and the error is
  common + actionable. Anything else should go: the error class fires rarely
  and its traceback educates on the spot.
- **A function docstring over 12 lines / a module docstring over 2 lines** —
  the soft zone. Usually Args filler, cross-stage narration ("after restart
  you will see..."), or examples the model doesn't need. Multi-Arg functions
  whose every Arg carries a real format constraint may legitimately stay long.

Reuse the lint's own scope helpers (so this audit never drifts from what the
gate scans — `ava/*.py` public modules + plugin namespace modules + wrap
targets in `plugins/*/plugin.py`):

```
python - <<'PY'
import ast
from pathlib import Path
from scripts.lint_agent_docstrings import (
    _discover_plugin_namespace_modules, _is_in_scope,
    _agent_visible_names, _wrap_targets, _is_visible,
)

def doc_of(node):
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        return node.body[0].value.value, node.body[0].lineno
    return None, None

root = Path(".")
ns_files = _discover_plugin_namespace_modules(root)
for f in sorted(root.rglob("*.py")):
    if ".venv" in f.parts or not _is_in_scope(f.relative_to(root), ns_files):
        continue
    tree = ast.parse(f.read_text())
    rel = str(f)
    is_plugin = rel.endswith("/plugin.py")
    allnames = _agent_visible_names(tree)
    wraps = _wrap_targets(tree) if is_plugin else set()
    if not is_plugin:
        d, ln = doc_of(tree)
        if d and len(d.splitlines()) > 2:
            print(f"{rel}:{ln}: module docstring {len(d.splitlines())} lines (soft cap 2)")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        visible = (node.name in wraps) if is_plugin else _is_visible(node.name, allnames)
        if not visible:
            continue
        d, ln = doc_of(node)
        if not d:
            continue
        if "Raises:" in d:
            print(f"{rel}:{ln}: {node.name} has a Raises: section — justify or delete")
        if len(d.splitlines()) > 12:
            print(f"{rel}:{ln}: {node.name} docstring {len(d.splitlines())} lines (soft cap 12)")
PY
```

For each hit, judge against the zero-based budget: every sentence must carry a
contract the name/signature/types cannot. Propose the trimmed wording in the
sweep PR (regenerate the prompt snapshot when docstrings change); a hit judged
legitimate gets a tracker `wontfix` so it is not re-litigated every sweep.
Calibration note: at class creation (2026-06-10, post-#1012) the scan reports
exactly two soft-zone hits (`watcher.launch` 14 lines, `watcher.cron` 15 — both
Args-format-heavy) and zero Raises hits; growth beyond that baseline is the
signal.

### 9. stalecopy (anchored on doc moves)

A doc-move PR on a long-lived branch copies prose into a new file; `main` then
revises the original. The rebase resolves the *source* file and cannot know the
copy is now the superseded version — no conflict, nothing red, the fact simply
reverts. Detect on a branch before merge: diff the merge base against `main` for
the docs that branch touches, then grep the branch tree for the lines `main`
removed. In a sweep on `main` the residue reads as two docs carrying the same
prose where only one has the revision — anchor on the doc moves among the merged
PRs class 6 already enumerates. Recorded from #1032, where two live instances
included one that would have silently un-documented a security property (that no
value rides argv, #974).

### 10. rebase-bypassed hooks (whole-repo)

`git rebase --continue` runs no pre-commit hooks, so the merge-conflict hook —
which exists precisely to keep markers out of a commit — cannot fire on the
commits a rebase produces. A marker-bearing resolution commits silently. Sweep
with `rg '^(<{7} |={7}$|>{7} )'` — the trailing space and the `$` are what
separate a real marker from a decorative `=====` banner, of which this repo has
several. The cheap standing hardening is that same sweep in the pre-push path
(`scripts/pre-push-check.sh`), where pre-commit does run.
