---
type: doc
title: "Audit scripts"
description: "scripts/audit/ — read-only audits of state that lives outside the tree: GitHub branch protection, reference and move inventories, runtime impact, SDK usage and a live database's schema."
tags: [scripts, audit]
---

# Audit scripts

Read-only tools that make an external or derived state observable: GitHub branch
protection stored outside Git, the references a move must not leave behind, the
runtime-impact graph's own numbers, the SDK surface in use, and a live
database's columns against the `db/schema.sql` baseline. None of them writes to
the repository, the database or the remote.

`branch_protection.py` compares live GitHub protection with `.trunk/trunk.yaml`.
Before changes, `where_used.py` groups importers, tests, strings, docs and baseline
entries of symbols/modules/paths (`--json`); `where_used_scan.py` owns matching.
After moves, `module_moves.py` rejects remaining old references and missing names.
Both CLI owners expose `main(argv, *, repo_root=None)` with shared diagnostics and
exit codes, defaulting to their checkout. Move imports use the caller's Python
environment; validation algorithms stay local.

`schema_drift.py` diffs a live database's public columns against `db/schema.sql`
and classifies every difference:
[[scripts/audit/docs/schema-drift.meiva.okf.md]].

Parent: [[scripts/docs/scripts.meiva.okf.md]].
