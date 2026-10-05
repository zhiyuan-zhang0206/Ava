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
`{placement,import_cache,patch_points,patch_targets,patch_report}.py` those of the
separate `lint-patch-targets` gate ([[scripts/lint/docs/patch-targets.ava.okf.md]]).
`tests_location.py` (with `tests_location_allowed.py` and, for `--suggest`,
`tests_location_suggest.py`) is the runnable gate that keeps new tests out of the
top-level `tests/` ([[scripts/lint/docs/tests-location.ava.okf.md]]).
`scripts/structure/cochange.py` is a
sibling module in the same package that is never called from the gate: its
two metrics need a rolling window of git history and judgement to turn a
signal into a fix, which is sweeper territory under
`docs/conventions/lint-vs-sweeper.md`'s graduation test, not a lint's.

## `cochange.py`

Run `.venv/bin/python scripts/structure/cochange.py [--days N | --commits N]
[--repo PATH] [--min-support N] [--min-confidence F] [--json]`. Defaults: a
90-day first-parent window on `origin/main`, min-support 8, min-confidence 0.6; a
markdown report on stdout, always exit 0 on a successful scan (an index, not
a wall).

It reuses `scripts.structure.locality._package_of` for Python package
resolution, so "package" means exactly what the structure gate's Rule 4
(package doors) means by it — same package, so the reach-in is not a
locality violation of its own scanner. Two metrics: **spread**, the number
of distinct packages a commit touches, by conventional-commit type; and
**co-change**, cross-package file pairs whose co-occurrence and confidence
both clear a threshold, excluding declared cross-process contract
boundaries (`_CONTRACT_BOUNDARIES`, e.g. `gateway/schemas/` <-> `ui/web/`,
carried by codegen).

Debt class, evidence bar, and the graduation path (a leaked decision with a
confirmed single owner becomes a Rule 5 `DECISIONS` entry, guarded by the
lint from then on) live in the sweeper's own repo skill:
`docs/conventions/tech-debt.md`, locality inspection guidance.

Parent: [[scripts/docs/scripts.ava.okf.md|scripts]].
