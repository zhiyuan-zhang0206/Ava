---
type: doc
title: Pre-Stop Start-Readiness Preflight
description: The read-only local checks of `ava start`, run before the stop by the self-update leg and by `ava restart` — refusing as RESTART_DECLINED while the host still serves.
tags:
- cli
- update
---

# Pre-Stop Start-Readiness Preflight

`cli/commands/_start_readiness_preflight.py` is the local-state half of
"validate before kill", for the two flows whose stop is followed by an
`ava start`: the self-update leg (`cli/commands/_update_agent_runner.py` step
3.1, after the gateway probes) and `ava restart` (`cli/commands/stop.py`, task
#3165 — the operator verb, and on Windows the updater ladder's restart step).
`ava start` is the only step that brings a stopped host back, so a start check
that fails AFTER the stop fails on a host whose services are already down
(macmini 2026-09-12: a stray workspace socket aborted converge after the stop
had already landed). This gate runs the read-only parts of those checks in
front of the stop: a failure refuses the stop while the host still serves.

Checked, all read-only:

- **private-tree roots and marker** — a `logs` / `workspaces` / `memory` root
  that converge would abort on (a symlink, or not a directory), and the
  `logs/.metadata_never_index` marker being non-regular. Non-regular nodes
  INSIDE the trees (sockets, FIFOs, devices) are reported as observations only:
  converge skips them (see `shared/private_storage.py`).
- **daemon health ports** — the blocking pre-bind gate (issue #977), on the
  roster the coming start will launch. Only terminal verdicts count, so an
  idempotent restart passes. The warning-only port-block scan and
  `.env`/registry drift (issue #603) ride along as observations.
- **migration readability** — tracked migrations the applier cannot open; their
  names are already vetted at the target ref by `validate_migrations_at_ref`.
- **prod-checkout anchor and the venv entry points** — `ava start`'s first
  refusal (`prod_service_checkout_error`), the interpreter every service
  session launches through (`.venv/bin/python`, checked for both callers), and
  — only when the caller's start execs it (`check_launcher`, the update leg) —
  the `.venv/bin/ava` launcher step 5 runs (its presence is step 3.5's report;
  this adds the exec bit).

Contract: read-only, and findings are data — the gate never raises for one. A
refusal is `RESTART_DECLINED_EXIT_CODE` ("nothing was stopped, host still
serving"), and it deliberately does NOT revert the checkout: the target tree is
not at fault (contrast the migrations-layout gate, which reverts). Every
category it refuses on also fails the refusing caller's own start leg — the
repairs it names are the path, so a refusal never blocks a viable restart (on
an already-down host it protects nothing, but a bounce could not have made it
past the same condition either). The mirror checks inside start stay where they
are — this gate narrows the window, it does not replace them.

Tests: `tests/cli/test_start_readiness_preflight.py` pins each check family's
disposition and the `check_launcher` toggle; the caller-level refusal contracts
live in `tests/cli/test_update_agent_runner_preflight.py` (update leg) and
`tests/cli/test_commands.py::test_cmd_restart_aborts_when_start_readiness_fails`.

## Key Dependencies

- [[start-readiness.ava.okf.md]] — what `ava start` itself asks before and after
  launching a service
- [[commands.ava.okf.md]] — the command-module overview
