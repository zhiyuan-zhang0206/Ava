---
type: doc
title: Initial PTY command provenance
description: Opt-in allocation metadata, compatibility and owner lifetime.
tags: []
---

## Initial command provenance

A `list` request with `include_initial_command=true` adds the allocation's
immutable `initial_command` to each live session. The service captures it before
publishing the new session or submitting the command. A duplicate `new` cannot
replace it. Default list responses retain the legacy wire shape; new clients
accept an older response without the field as unknown provenance. The flag is
a boolean boundary (invalid values are refused).

This metadata belongs to the live PTY owner, survives a schedule-manager restart,
and is not a durable business-operation receipt or a lock. A PTY service restart
loses terminals and sweeps exact identities through its durable ledger; it does
not re-adopt their command state. Schedule convergence combines this evidence
with a durable revision and PostgreSQL advisory ownership. See
[[gateway/schedules/docs/schedule-convergence.ava.okf.md]].
