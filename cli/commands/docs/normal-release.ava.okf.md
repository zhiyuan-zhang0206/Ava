---
type: doc
title: Retained Publication Admission
description: Shared publication fences remain admission guards; cluster updates are the attended source-mode procedure.
tags:
- cli
- update
---

# Retained Publication Admission

Cluster updates are the attended stop / switch / start procedure scripted as
`python -m cli.fleet_update` (see the runbook's "Updating a networked cluster in
source mode"); no CLI/RPC update command exists.

Shared publication receipts, selectors and managed-writer fences still participate
in `RuntimeAdmission`. They remain enforced until database publication authority
is replaced.
See [[base/deploy/schema/docs/migrations.ava.okf.md]] for the database admission boundary.
