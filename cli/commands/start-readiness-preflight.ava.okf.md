---
type: doc
title: Update-Leg Start-Readiness Preflight
description: The read-only local checks of `ava start`, run by the self-update BEFORE its stop, refusing as RESTART_DECLINED while the host still serves.
tags:
- cli
- update
---

# Update-Leg Start-Readiness Preflight

`cli/commands/_start_readiness_preflight.py` is the local-state half of
"validate before kill" on the self-update leg. `ava start` is the only step that
brings a stopped host back, and the update runs it AFTER the stop — so a start
check that fails there fails on a host whose services are already down (macmini
2026-09-12: a stray workspace socket aborted converge after the stop had already
landed). This gate runs the read-only parts of those checks in front of the stop
(`cli/commands/_update_agent_runner.py` step 3.1, after the gateway probes): a
failure refuses the update while the host still serves.

Checked, all read-only:

- **private-tree roots and marker** — a `logs` / `workspaces` / `memory` root
  that converge would abort on (a symlink, or not a directory), and the
  `logs/.metadata_never_index` marker being non-regular. Non-regular nodes
  INSIDE the trees (sockets, FIFOs, devices) are reported as observations only:
  converge skips them (see `shared/private_storage.py`).
- **daemon health ports** — the blocking pre-bind gate (issue #977), on the
  roster the update's own start will launch. Only terminal verdicts count, so an
  idempotent restart passes. The warning-only port-block scan and
  `.env`/registry drift (issue #603) ride along as observations.
- **migration readability** — tracked migrations the applier cannot open; their
  names are already vetted at the target ref by `validate_migrations_at_ref`.
- **prod-checkout anchor and launcher executability** — `ava start`'s first
  refusal (`prod_service_checkout_error`) and the PermissionError that step
  3.5's presence check does not cover.

Contract: read-only, and findings are data — the gate never raises for one. A
refusal is `RESTART_DECLINED_EXIT_CODE` ("nothing was stopped, host still
serving"), and it deliberately does NOT revert the checkout: the target tree is
not at fault (contrast the migrations-layout gate, which reverts). The mirror
checks inside start stay where they are — this gate narrows the window, it does
not replace them.

Tests: `tests/cli/test_start_readiness_preflight.py` pins each check family's
disposition; the runner-level refusal contract (nothing stopped, declined code)
lives in `tests/cli/test_update_agent_runner_preflight.py`.

## Key Dependencies

- [[start-readiness.ava.okf.md]] — what `ava start` itself asks before and after
  launching a service
- [[commands.ava.okf.md]] — the command-module overview
