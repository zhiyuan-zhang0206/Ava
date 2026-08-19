# Lint vs Sweeper — the boundary

Ava has two mechanisms for keeping the codebase clean, and they pull in
different directions:

- **Custom Lints** — the `scripts/lint_*.py` checks wired into pre-commit. They
  run on every commit, **block** it, and must be fast, offline, deterministic,
  and effectively zero-false-positive. A lint is a wall: it refuses the commit
  until the author fixes the violation, at the source, in the same PR.
- **The Sweeper** — the periodic debt sweep: the general engine in
  `skills/sweeper/` driven by this repo's debt classes in
  `.agents/skills/ava-sweeper/` (tracker at `future/infra/debt-tracker.md`).
  It runs on a cadence (not on every commit), tolerates judgement, may scan the
  whole repo / hit the network / reason across files, and lands its findings as
  its own `chore(sweeper)` PR.

This doc draws the boundary between the two with an explicit test, so a new
structural check lands on the correct side instead of leaking debt through the
gap between them. It governs both surfaces: when you add a pre-commit lint, or
add / split / retire a sweeper class, this is the rule you apply.

## The graduation test

A debt check is a **Custom Lint** (runs in pre-commit, blocks the commit) **iff
BOTH** of these hold:

1. **Detection is commit-cheap** — fast, offline, deterministic, with
   effectively zero false positives.
2. **Fix is local** — the committing agent can resolve it in-PR within roughly
   three files / tool-calls, without being pulled off the task it is already
   doing.

Otherwise it is a **Sweeper class** — periodic, judgement-tolerant, lands its
own PR, and is free to scan the whole repo, hit the network, or reason across
files.

### Corollaries

These four nuances are the load-bearing part of the rule:

- **Causal axis.** A lint catches debt a PR is *creating right now* — it gets
  fixed in that PR, at the source. The sweeper catches debt that has
  *accumulated* or has no single author. If a PR renames a daemon and leaves the
  runbook stale, that staleness was created by *that* PR; a roster lint would
  have blocked it until it cleaned up its own mess. Ownerless drift that no
  single commit introduced is sweeper territory.
- **Prefer SPLIT over all-or-nothing.** Most checks are not purely one side.
  Carve the zero-false-positive hard core into a lint; leave the
  judgement residue in the sweeper. A check rarely has to be wholly a wall or
  wholly a sweep — split it at the line where determinism ends.
- **Anti-drift coupling.** A split sweeper class **reuses the lint's scanner**
  (`from scripts.lint_x import ...`) so the two halves never diverge in scope.
  The lint defines what gets scanned; the sweeper half imports those same
  helpers rather than re-deriving the scan, so a change to the lint's coverage
  automatically moves the sweeper's coverage with it.
- **Steady-state vs migration.** A class graduates to a lint when its
  *steady-state* fix is local. The *one-time burn-down* of pre-existing
  violations is a separate, bounded migration — it may touch many files, but it
  does not change the lint's steady-state locality. Don't keep a check in the
  sweeper just because turning it into a lint requires a one-time cleanup.

## Reclassification of the sweeper classes

Applying the graduation test to the existing debt classes gives the
authoritative classification below. Eight original classes collapse to seven
surviving sweeper classes (one is pure redundancy with an existing hook, so it
is deleted, not moved), and two new lints are surfaced — `doc-roster` built
first, `doc-symbols` deferred until a zero-false-positive form was proven and
now graduated to a lint as well.

| # | class | detection commit-cheap? | fix blast radius | verdict |
|---|---|---|---|---|
| 1 | `deps` | ✗ networked (`uv pip list --outdated`, `npm outdated`) | varies | **Sweeper** (detection gate) |
| 2 | `docs-aging` | ✗ `git log` + `gh pr view` per file + judgement | cross-doc reorg | **Sweeper** (judgement) |
| 3 | `fail-fast` | grep cheap BUT irreducible FP (`get() or {}`, `case _:` legit in non-enum match) | varies | **SPLIT**: `except…: pass` → Lint; rest → Sweeper |
| 4 | `inline-marker` (TODO/FIXME) | ✓ | varies | **Sweeper** (no hard core — cannot block all TODOs; low yield) |
| 5 | `dead-code` (vulture) | ✗ slow + 70 FP without the exclude list | local delete | **Sweeper** (detection gate) |
| 6 | `boundary` (type/responsibility) | ✗ cross-file reasoning | type + all consumers, multi-file | **Sweeper** (its raison d'être) |
| 7 | `skill-desc` | ✓ | single file | **Already split** (80→lint / 50–80→sweeper) = the precedent |
| 8 | `import-lint` | ✓ but **already the `lint-imports` hook** | local | **DELETE from sweeper** (pure redundancy) |
| NEW | `doc-roster` | ✓ parse runbook table vs `build_services()` | <3 files | **Lint (new, flagship)** |
| NEW | `doc-symbols` (docs ↔ deleted SDK symbol) | ✓ once restricted to code spans + non-symbol guards | 1–2 files | **Lint (new)** — `scripts/lint_doc_symbols.py` |
| NEW | `doc-anchors` (docs ↔ renamed code symbol) | ✓ same code-span restriction, resolved against the AST | 1–2 files | **Lint (new)** — `scripts/lint_doc_anchors.py` |
| NEW | `docstring-budget` (agent-visible docstring verbosity) | ✓ scan cheap BUT keep/trim is judgement (a long Args block can be all format contracts) | 1–2 files | **SPLIT** (2026-06-10): zero-FP core (CJK / impl keywords / module-doc child restating / SDK↔skill coupling) → the `lint-docstrings` hook; Raises-section + soft-length residue → Sweeper class 8, reusing the lint's scope helpers |

Net: **8 → 7 → 8 sweeper classes** (`deps`, `docs-aging`, `fail-fast`
[narrowed], `inline-marker`, `dead-code`, `boundary`, `skill-desc`,
`docstring-budget` [added 2026-06-10]). Three new lints
(`doc-roster`, `doc-symbols`, `doc-anchors`).

The `doc-symbols` zero-FP form is the worked precedent for "restrict the scan
until the false positives vanish": `ava.<name>` references are validated only
inside inline-code spans / fenced blocks (prose is never flagged), hostnames
(`ava.host.com`) and the metasyntactic `ava.X` placeholder are excluded, and
the valid set is built live from `ava.__all_for_ava__` + real submodules + module attrs
+ plugin-registered namespaces (reusing the same plugin scan
`lint_agent_docstrings.py` uses, so the two stay coupled).

## Worked example: `skill-desc`

`skill-desc` is the canonical split and the anti-drift pattern in one place.

A skill's `description` is the index line the agent reads to decide whether to
reach for the skill, and for resident (injected) skills it sits in *every*
system prompt — so its length is a real cost. The check has two tiers:

- **Hard ceiling — 80 units — is a lint.** `scripts/lint_skill_descriptions.py`
  enforces it in pre-commit and fails the commit. Detection is a deterministic
  unit count over the frontmatter `description:` field (zero false positives),
  and the fix is local: one file, trim the description. Both halves of the
  graduation test hold, so it is a wall.
- **Soft zone — 50–80 units — is a sweeper class.** Descriptions that pass the
  gate but are longer than ideal are flagged by the `skill-desc` sweeper class.
  Tightening them needs judgement (keep what the skill does plus its trigger
  keywords, push detail into the body), which is exactly what does *not* belong
  in a commit-blocking wall.

The anti-drift coupling is concrete here: the sweeper class does not re-implement
the scan. It imports the lint's own helpers —

```python
from scripts.lint_skill_descriptions import length_units, _skill_md_files, _description
```

— so the soft-zone sweep and the hard-ceiling lint scan exactly the same files
with exactly the same unit measure. If the lint's coverage changes (a new skill
directory, a different unit rule), the sweeper's coverage moves with it
automatically; the two tiers can never disagree about what counts or what gets
scanned. Every split class should follow this shape: the lint owns the scanner,
the sweeper half imports it.

## Why `SKIP=` exists at all

**A gate that fails for the wrong reason is worse than one that is honestly
absent — the first teaches people to disable all of them.** That is the whole
mechanism: a hook failing for something unrelated to your commit trains the
habit of reaching for the blunt instrument, and the blunt instrument takes out
the gates that were working. `SKIP=` is the narrow tool that lets you get past
the broken one without disarming the rest.

The same rule decides where a check belongs. `scripts/migration_smoke.py` is not
a hook because it shells out to `psql`, which is in the CI image but not on every
dev machine — as a commit gate it would fail for reasons unrelated to the commit,
manufacturing exactly this pressure. It stays in CI, where the toolchain is
guaranteed.

Its corollary: **a gate that cannot fail is also worse than an absent one**,
because it reads as coverage. `scripts/check_cross_branch_migrations.py` was a
pass-through returning 0 unconditionally; it now asserts its own single-branch
premise and fails if a long-lived second branch appears. Before adding a check,
ask what makes it go red — if nothing can, it is decoration.
