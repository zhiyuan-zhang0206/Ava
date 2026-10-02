---
type: doc
title: Tests-location lint
description: A test in the top-level tests/ must be registered (by design, or allowed with a reason as contract or integration) - decided from its path alone, a new one is refused; the registry, the --suggest command and what the check costs.
tags:
- scripts
- lint
- tests
---

# Tests-location lint

`scripts/structure/tests_location.py` (pre-commit and pre-push hooks, and `pre-commit run --all-files` in the CI structure job) keeps new tests out of the top-level `tests/`. A test belongs in the `tests/` directory of the package it proves; while tests moved into packages, new ones kept landing at the top, and each one costs a second move later (the flaky-test quarantine and `.test_durations` are keyed by path, so a move resets both). The authoritative rule text is the script's module docstring; this node is the map.

It lives in `scripts/structure/` rather than `scripts/lint/` because the lint directory is at its 20-entry budget; `scripts/structure/cochange.py` is the precedent for a runnable module there.

## The rule

A *top-level test* is a `test_*.py` file under `tests/`. It may stay only if one of the two registries below names it, each a path lookup:

| registry | where | meaning |
|---|---|---|
| `BY_DESIGN` | `scripts/structure/tests_location_allowed.py` | a directory or file with no package to move to: `tests/e2e/`, `tests/ui/`, `tests/fixtures/`, `tests/factories/` and three real-process proofs under `tests/integration/`. Repeats `placement.TOP_LEVEL_*`; a test locks the two together |
| `ALLOWED` | same file | one test with a category and a one-line reason. `contract`: the test's subject is a repository artifact (workflows, `pyproject.toml`, `db/schema.sql`, migrations, `ui/`, `schedules/`, `deploy/`, skill scripts) or the test harness itself, which no package owns; a scan over the whole tree counts. `integration`: it spans units that may not import each other (agent and ops, cli and gateway), so no package may hold it |

Anything else is a violation: a new top-level test is refused, there is no baseline of debt to add it to. An entry whose file is gone, an `ALLOWED` entry under `BY_DESIGN`, a category other than the two or an empty reason fails too, so the registry cannot rot into a permit wall. The verdict never looks at what the test imports: an unrelated production commit cannot move it.

## Fixing a violation

`.venv/bin/python scripts/structure/tests_location.py --suggest <file>` names the lowest package that may legally hold the test (any package above it inside the same unit that holds what it uses is also legal), says why none can (a test that uses units that may not import each other: split it by unit or register it as `integration`), or says it references no first-party package (a harness or artifact test: register it as `contract`). It reads the production code through `scripts/structure/placement.py`, which costs 10 ms to 2.7 s per file, so no hook runs it: the hook prints the command.

After `git mv`, the new directory may need a `path_scopes.toml` (read by `tests/fixtures/path_scopes.py`): it gives a directory's tests the autouse isolation fixtures, a package directory with none silently has none (`tests/ci/test_path_scopes.py` fails when a moved test loses them).

## What it costs

Paths only: the checked files and the registry. No module index, no import graph, no `place()`; a test locks that the checks never import `scripts.structure.placement`. The commit hook runs `--only` over the changed files ([[scripts/lint/docs/changed-files-mode.ava.okf.md|changed-files mode]]): the changed top-level tests are judged, and a change to the lint or its registry (anything under `scripts/structure/`) checks every test. Every judgment is on the repo-relative path, never on where the checkout sits. Pre-push and CI's `backend-structure` (`pre-commit run --all-files`) check every tracked top-level test (`git ls-files tests`), which is also where a deleted test's stale entry is found when no commit hook saw it. Whether a registered test still has a package home is not checked here: it needs the placement rule.

## What it does not check yet

The legality of tests inside packages (a test in a package that may not import what it uses, or that does not hold the code it tests) is not enforced; it needs the placement rule and is a separate slice.

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]]. Related: [[scripts/lint/docs/patch-targets.ava.okf.md|patch-target lint]] (same home rule, per patch point).
