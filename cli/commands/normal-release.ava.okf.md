---
type: doc
title: Retained Publication Admission
description: Shared publication fences remain admission guards; host update execution belongs to the prepared release journal.
tags:
- cli
- update
---

# Retained Publication Admission

Host updates enter through `ava cluster update --prepared REQUEST`, implemented
by [[cli/release_transition/release_transition.ava.okf.md]]. Its durable journal
and native executor own progress and recovery. The old normal-release,
bootstrap-hop and continuation CLI/RPC commands are absent.

Shared publication receipts, selectors and managed-writer fences still participate
in `RuntimeAdmission`. They remain enforced until database publication authority
is replaced; a prepared host operation does not imply all-writer database closure.
See [[shared/migrations/migrations.ava.okf.md]] for the database admission boundary.
