---
type: doc
title: Patch-target lint
description: Structure Rule 8 — a test may not patch a private name of a package it does not belong to; how a test's package is derived, the A-E classes, the frozen patch_targets baseline section and the --report census.
tags:
- scripts
- lint
- tests
---

# Patch-target lint

`scripts/lint/patch_targets.py` (pre-commit `lint-patch-targets`, and `pre-commit run --all-files` in the CI structure job) keeps a test from replacing a private name that belongs to another package. Each such reach-in is a missing injection seam: the code under test had no public way to take its clock, transport or identity from the caller. The authoritative rule text is the script's module docstring; this node is the map.

## The rule

Every patch point of a test file is classified. A point is a violation (class D) when the target is repository code, is not in the ambient list, has a private name (an attribute or module segment with a single leading underscore), and the package that owns that name does not contain the test's home. The owner is Rule 4's owner (`locality._private_target`): the package holding the first private component.

| class | meaning |
|---|---|
| A | environment boundary: stdlib, third-party, the runtime, non-`AVA_*` environment variables |
| B | own package: the owning package contains the test's home (private names included) |
| C | another package's public name (deep attributes counted, not violations) |
| D | violation: a private name of a package the test does not belong to |
| E | global environment: `E_MODULES` (settings, paths, machine and cluster identity, env resolution, ambient services) and `AVA_*` variables |
| U | unresolved object; counted, never flagged |

D is reported by relation: `ancestor` (the test's home is a strict ancestor of the owner: a test that spans several packages reaches into one), `other-unit`, `sibling` (same unit, another lineage: the home was lowered into a package the owner is not in), `top-level` (a test with no package home). The message for `ancestor` says to move the test into the owner (or split it, when it also needs a package the owner does not import) or give the owner an injection seam.

## The home

`scripts/structure/placement.py` derives a test's home from the file's own text, not its directory: the referenced first-party modules (imports, import-module string targets, source paths below the repository root, imports in source strings the file runs) are grouped by unit, and the unit that may legally import all the others is chosen from the import-linter contracts in `pyproject.toml` (silent pairs follow the source import direction). Inside that unit the home is the deepest package that holds or directly depends on every referenced module: each module lies in its subtree, or the non-test code in its subtree imports it (function-level imports included; `tests.*` references are not modules of the unit). Candidates are the packages on the modules' ancestor chains, below their nearest common ancestor (the bound); only direct imports count, not dependencies of dependencies; with no single deepest package (a dependency cycle) the home stays at the bound. Evidence that exists only because the file patches it (a patch string target, an import read only as a patch object) does not count; neither does sample data (a path not rooted at the repository root, a bare path string, source in a string of a file that spawns no interpreter); a file whose every strong reference is patch evidence keeps it (the fallback). Top-level tests (e2e, shared fixtures and factories, the root conftest, the real-process integration proofs) have no home.

The verdict does not change when a test moves into `<pkg>/tests/`, but it follows production imports: adding an import can lower the home of a test that references both ends, deleting one can raise it, and a cycle keeps it at the bound. The home can be the consumer of the subject's package: a test of `a.x` and `b.y` lives in `b` when `b` imports `a` and `a` does not import `b`. The later "each test sits at a legal location" lint reuses `place()` and enforces the *legal* directory, never the *lowest*: the lowest moves with unrelated production commits, so it is the move tool's output. `import_cache.py` reads the production imports from the working tree on every run and caches them per file by `(mtime_ns, size)` under `.cache/structure/`; nothing is committed.

## Baseline

Today's violations are frozen in the `patch_targets` section of the structure baseline shards as `path::target -> site count`, guarded by `scripts/lint/code_structure.py` like Rules 4 and 5: growth fails, a fixed site fails until its entry is lowered, and against the base revision the section is shrink-only (a moved owner may carry a key; `git -M` renames carry keys). A section is introduced with the change that adds its lint (`locality.introduced`). A change to how sites are measured (the home rule) re-freezes the section under a higher version in `scripts/structure/baseline/rules.json` (`baseline_shards.rule_change_errors`): keys are not comparable across it, so the guard holds the total (it may not rise) for that one change and per-key again once the new version is the base. Any section can use the same mechanism.

Checks: pre-commit passes only the changed test files (a changed lint, placement module or shard scans everything); the pre-push hook `lint-patch-targets-full` and the CI structure job scan everything, because a production import change can move the home of a test that did not change.

## Report

`scripts/lint/patch_targets.py --report` prints the census as Markdown and exits 0: the class distribution, D by relation, D by test home, D by production module (the injection-seam work list), the most-patched ambient modules and the fallback files. The numbers and the follow-up list live in [test patch audit](../../../future/infra/test-patch-audit.md).

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]].
