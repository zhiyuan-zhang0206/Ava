# Evaluation assets are not built in this task

## Context

The expert-review remediation identified `benchmarks/` as an empty,
untracked asset: no path under it is versioned, and its only prior contents
were Python cache artifacts. The user explicitly ruled that this task must not
construct an evaluation or benchmark system.

## Decision

Do not add evaluation code, benchmark fixtures, datasets, or an evaluation
harness. The obsolete empty asset is removed when present; this worktree now
contains no `benchmarks/` directory.

This record preserves the user's evaluation ruling without making an
independent judgment about evaluation work.

## Alternatives rejected

- Build a replacement benchmark or evaluation harness: outside the explicit
  task scope and contrary to the user ruling.
- Retain an empty ignored directory or Python bytecode: it represents no
  versioned asset and creates a misleading repository surface.

## Consequences

- Future evaluation work requires a separately scoped decision and task.
- This change is limited to recording the boundary and removing the empty
  asset; it does not establish evaluation criteria or results.
