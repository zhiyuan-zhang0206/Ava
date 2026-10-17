---
type: doc
title: macOS authorization grants
description: "The grant catalog, the live probe that reads it, and the gate state machine the desktop onboarding renders."
tags: [desktop, permissions, macos]
---

# macOS authorization grants

The desktop helper holds every macOS authorization Meiva uses. This package names
them once and answers three questions about a host: which grants exist, what the
system says right now, and whether the host is usable.

`catalog.py` is the only list of grants. `probe.py` reads the rows TCC answers
silently through a child the helper spawned, and the two desktop grants from the
helper's own ping; `gate.py` turns one probe into a state and the per-item
evidence. `checklist.py` is the single rendering shape the CLI, the gateway
payload and the console section all consume. `triggers.py` holds the child
programs that raise a folder or Automation request, and `children/` the programs
themselves; the two desktop grants are requested by the helper over the wire.

`scripts/host_ops/tcc/onboard-helper-grants.py --check` calls the same probe, so
the operator tool and the product cannot disagree about a host.

## Evidence comes from the probe in the same call

The record exists for two reasons only: a surface can render the last known state
before its own probe returns, and the deferral has to survive a restart. Neither
use turns it into a source of truth.

## Deferral is not completion

Denied and not-determined grants block completion. An unknown grant does not: a
per-target Automation row has no silent preflight, so "unknown" means nothing has
attempted it yet, not that something is wrong.

## One probe, two planes for the two desktop grants

Every surface reads the matrix through run_preflight and the helper's ping, never through a second query implementation. File, Automation and report-only rows come from one helper-spawned TCCAccessPreflight child in the same call. Screen Recording and Accessibility are read on two planes: the TCC record (the same preflight child) and the running helper's own boolean (`preflight_screen`, `ax_trusted`).

## A grant the record holds but the helper has not read is stale

The TCC record says granted / denied / not_determined; the running helper's boolean says whether this process can use the grant right now. Record granted with a false boolean reads stale: macOS already holds the grant, the long-lived process has not re-read it, and the fix is a helper restart -- reporting it as denied would send the user to System Settings for a grant that is already there, and reporting the host as complete would claim an unusable grant. Record denied stays denied and record not_determined stays not_determined, whatever the boolean says; a true boolean is granted on both planes.

Measured on an authorized host on 2026-10-17: the record already said granted
(`TCCAccessPreflight` granted, tccd `AUTHREQ_RESULT authValue=2`), while the
running helper still reported `ax_trusted=false` and refused every
Accessibility-gated operation; after a helper restart the same binary reported
true and a real `ax_window_info` call succeeded. One boolean alone would have
called that host either broken or complete.
