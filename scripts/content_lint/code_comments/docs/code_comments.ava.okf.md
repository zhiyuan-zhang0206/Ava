---
type: doc
title: Structured code comment checks
description: Parsed source references, colocated typed contracts and generated SDK documentation.
tags: [scripts, lint, documentation]
---

# Structured code comment checks

`model.py` and `records.py` define the closed document contract. `check.py` binds
records to tracked, canonical, regular files, exact qualified symbols and test
definitions. The tracked universe comes from meizel; Python AST/tokenize and the
locked frontend TypeScript compiler parse source without importing application
code. `typescript_index.py` validates the compiler output and source hashes.

`render.py` uses the existing static SDK provenance resolver and generates API
docstrings from documentation. It verifies that executable ASTs are unchanged
and prepares every file before writing. It does not execute the SDK.

## Commands

Run from the repository with its real `.venv` and installed frontend dependencies:

```bash
bash scripts/content_lint/code_comments/check.sh validate
bash scripts/content_lint/code_comments/check.sh check path/to/source.py
bash scripts/content_lint/code_comments/check.sh inventory
bash scripts/content_lint/code_comments/check.sh render-api
bash scripts/content_lint/code_comments/check.sh changed
bash scripts/content_lint/code_comments/check.sh changed BASE_REF
```

`validate` checks all typed records and references. `check` additionally rejects
free prose in selected sources; without paths it selects the whole supported
tree. `inventory` is the full strict scan with failure status while work remains;
pass `--json` before the command for structured output. These are different
claims and the CLI prints its strict scope explicitly.

`changed` selects staged paths under pre-commit's unstaged-change isolation.
When pre-commit supplies from/to refs (including the pre-push branch check), it
uses that committed range and requires its target to equal the checked-out HEAD.
Git type changes are included alongside additions, copies, renames and edits.
`changed BASE_REF` selects the committed diff from that revision to HEAD, as used
by CI against the checked-out merge tree's base. Both validate the current
checkout, so manual use requires a checkout matching the selected snapshot.
The shell wrapper fails when Git or either parser fails. Empty changed scope
still validates every typed record. It is not whole-tree migration success.

## Parser boundaries

Python bindings include qualified definitions and simple assignments; ambiguous
definitions fail. API ownership reuses `scripts/codegen/sdk_surface`, so dynamic
exports which that resolver cannot prove cannot authorize generated docstrings.
PEP 224 constant documentation is projected beside its assignment. Annotation-only
dynamic attributes remain blocked when the provenance resolver cannot prove them;
preserve their existing help and extend the resolver before migrating that file.
JavaScript/TypeScript bindings include named declarations and literal test labels
from imported Vitest, Playwright and node:test runners. Unsupported dynamic test
labels and wrappers do not resolve; use a concrete test definition.

The checker proves syntax and reference integrity, not external-source freshness,
test execution or semantic truth. The contribution's independent review owns
those judgments. The current migration and reviewer rules are in
[structured code comments](../../../../docs/conventions/engineering/structured-code-comments.md).

Parent: [[scripts/content_lint/docs/content_lint.ava.okf.md]].
