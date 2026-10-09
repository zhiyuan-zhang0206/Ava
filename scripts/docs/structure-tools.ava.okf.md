---
type: doc
title: Structure Sweeper Tools
description: Non-lint scripts/structure/ tools — detection that needs a rolling history window or judgement, so they inform the sweeper instead of blocking a commit.
tags:
- scripts
- sweeper
---

# Structure Sweeper Tools

`scripts/structure/{quality_budget,locality,path_imports}.py` and the `scripts/structure/ambient_state/` package are the
pre-commit `lint-code-structure` gate's own scanner modules — see
[[scripts/lint/docs/lint.ava.okf.md]] — and
`{placement,patch_points,patch_targets,patch_report}.py` and `imports/` those of the
separate `lint-patch-targets` gate ([[scripts/lint/docs/patch-targets.ava.okf.md]]).
`tests_location.py` (with `tests_location_allowed.py` and, for `--suggest`,
`tests_location_suggest.py`) is the runnable gate that keeps new tests out of the
top-level `tests/` ([[scripts/lint/docs/tests-location.ava.okf.md]]).
`scripts/structure/cochange.py` is a
sibling module in the same package that is never called from the gate: its
two metrics need a rolling window of git history and judgement to turn a
signal into a fix, which is sweeper territory under
`docs/conventions/engineering/lint-vs-sweeper.md`'s graduation test, not a lint's.

## `cochange.py`

Run `.venv/bin/python scripts/structure/cochange.py [--days N | --commits N]
[--repo PATH] [--min-support N] [--min-confidence F] [--json]`. Defaults: a
90-day first-parent window on `origin/main`, min-support 8, min-confidence 0.6; a
markdown report on stdout, always exit 0 on a successful scan (an index, not
a wall).

It reuses `scripts.structure.imports.package_of` for Python package resolution,
the same package anchor as Rule 4, dependency and test-placement checks.
Two metrics: **spread**, the number
of distinct packages a commit touches, by conventional-commit type; and
**co-change**, cross-package file pairs whose co-occurrence and confidence
both clear a threshold, excluding declared cross-process contract
boundaries (`_CONTRACT_BOUNDARIES`, e.g. `gateway/schemas/` <-> `ui/web/`,
carried by codegen).

## Shared static imports

`scripts/structure/imports/` owns package anchors, normalized import clauses,
local bindings and direct module dependencies. Locality, empirical direction,
placement and patch collectors consume those facts. `from pkg import module`
selects a submodule when it exists; imported members depend on their import
door, without following re-exported values. Dependencies are deduplicated per
clause while preserving all local aliases. Function-local imports count.

Invalid relative imports raise `InvalidRelativeImportError` with file and line
instead of becoming repository-root edges or subject-free successful checks.
No package anchor is guessed for callers without a source path. Dynamic import
strings and patch/path evidence remain collector-specific; this parser does
not execute Python. The version-3 cache stores normalized statements, not
resolved module edges, so current-tree resolution still applies on cache hits.

Import-linter and `pyproject.toml` still own architectural direction. The
shared parser supplies static facts for structure tools, not a replacement
architecture checker or Python interpreter.

Debt class, evidence bar, and the graduation path (a leaked decision with a
confirmed single owner becomes a Rule 5 `DECISIONS` entry, guarded by the
lint from then on) live in the sweeper's own repo skill:
`docs/conventions/engineering/tech-debt.md`, locality inspection guidance.

Parent: [[scripts/docs/scripts.ava.okf.md|scripts]].
