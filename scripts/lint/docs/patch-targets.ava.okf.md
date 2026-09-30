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

D is reported by relation: `ancestor` (the test's home is a strict ancestor of the owner: a test that spans several packages reaches into one), `other-unit`, `sibling`, `top-level` (a test with no package home). The message for `ancestor` says to move the test into the owner or give the owner an injection seam.

## The home

`scripts/structure/placement.py` derives a test's home from the file's own text, not its directory: the referenced first-party modules (imports, import-module string targets, source-path literals, embedded imports) are grouped by unit, the unit that may legally import all the others is chosen from the import-linter contracts in `pyproject.toml` (silent pairs follow the source import direction), and the home is the nearest common ancestor directory of the modules in that unit. Evidence that exists only because the file patches it (a patch string target, an import read only as a patch object) does not count; a file whose every strong reference is such evidence keeps it (the fallback). Top-level tests (e2e, shared fixtures and factories, the root conftest, the real-process integration proofs) have no home.

The verdict therefore does not change when a test moves into `<pkg>/tests/`. The later "each test sits at its lowest legal location" lint reuses `place()`.

## Baseline

Today's violations are frozen in the `patch_targets` section of the structure baseline shards as `path::target -> site count`, guarded by `scripts/lint/code_structure.py` like Rules 4 and 5: growth fails, a fixed site fails until its entry is lowered, and against the base revision the section is shrink-only (a moved owner may carry a key; `git -M` renames carry keys). A section is introduced with the change that adds its lint (`locality.introduced`). Explicit arguments check only the named test files; a changed rule file or shard triggers a full scan.

## Report

`scripts/lint/patch_targets.py --report` prints the census as Markdown and exits 0: the class distribution, D by relation, D by test home, D by production module (the injection-seam work list), the most-patched ambient modules and the fallback files. The numbers and the follow-up list live in [test patch audit](../../../future/infra/test-patch-audit.md).

Parent: [[scripts/lint/docs/lint.ava.okf.md|lint]].
