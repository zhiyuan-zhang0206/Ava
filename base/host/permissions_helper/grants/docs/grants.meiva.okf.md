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

```meiva-contract
id = "live-probe-only"
kind = "invariant"
statement = "A host state is computed from the probe of the same call. The record under $MEIVA_HOME stores the last observation and the user's deferral flag; it is written from a probe and never read as evidence, so a cached 'granted' can neither hide a revoked grant nor invent one."
code = ["base/host/permissions_helper/grants/gate.py:evaluate"]
tests = ["base/host/permissions_helper/grants/tests/test_gate.py::test_a_denied_item_blocks_completion_and_names_itself"]
```

The record exists for two reasons only: a surface can render the last known state
before its own probe returns, and the deferral has to survive a restart. Neither
use turns it into a source of truth.

## Deferral is not completion

```meiva-contract
id = "deferred-is-not-a-grant"
kind = "invariant"
statement = "Recording 'later' keeps the host in the restricted family: the state becomes deferred, complete stays false, and the next gate run asks again. A complete run clears the flag."
code = ["base/host/permissions_helper/grants/gate.py:mark_deferred"]
tests = ["base/host/permissions_helper/grants/tests/test_gate.py::test_a_deferred_host_is_not_complete_and_says_so"]
```

Denied and not-determined grants block completion. An unknown grant does not: a
per-target Automation row has no silent preflight, so "unknown" means nothing has
attempted it yet, not that something is wrong.

## One probe, two planes for the two desktop grants

```meiva-contract
id = "one-probe-for-every-surface"
kind = "invariant"
statement = "Every surface reads the matrix through run_preflight and the helper's ping, never through a second query implementation. File, Automation and report-only rows come from one helper-spawned TCCAccessPreflight child in the same call. Screen Recording and Accessibility are read on two planes: the TCC record (the same preflight child) and the running helper's own boolean (`preflight_screen`, `ax_trusted`)."
code = ["base/host/permissions_helper/grants/probe.py:run_preflight", "base/host/permissions_helper/grants/probe.py:probe"]
tests = ["base/host/permissions_helper/grants/tests/test_probe.py::test_the_probe_reads_every_catalog_service_through_one_child", "base/host/permissions_helper/grants/tests/test_probe.py::test_a_record_without_a_decision_stays_not_determined"]
```

## A grant the record holds but the helper has not read is stale

```meiva-contract
id = "record-and-runtime-planes"
kind = "invariant"
statement = "The TCC record says granted / denied / not_determined; the running helper's boolean says whether this process can use the grant right now. Record granted with a false boolean reads stale: macOS already holds the grant, the long-lived process has not re-read it, and the fix is a helper restart -- reporting it as denied would send the user to System Settings for a grant that is already there, and reporting the host as complete would claim an unusable grant. Record denied stays denied and record not_determined stays not_determined, whatever the boolean says; a true boolean is granted on both planes."
code = ["base/host/permissions_helper/grants/probe.py:ProbeResult", "base/host/permissions_helper/grants/gate.py:_detail"]
tests = ["base/host/permissions_helper/grants/tests/test_probe.py::test_a_grant_the_record_holds_and_the_helper_has_not_read_is_stale", "base/host/permissions_helper/grants/tests/test_gate.py::test_a_grant_the_running_helper_has_not_read_is_stale_not_denied"]
```

Measured on an authorized host on 2026-10-17: the record already said granted
(`TCCAccessPreflight` granted, tccd `AUTHREQ_RESULT authValue=2`), while the
running helper still reported `ax_trusted=false` and refused every
Accessibility-gated operation; after a helper restart the same binary reported
true and a real `ax_window_info` call succeeded. One boolean alone would have
called that host either broken or complete.

## A request is the helper's to make

```meiva-contract
id = "requests-run-in-the-helper"
kind = "invariant"
statement = "A system dialog is raised by the helper process itself, through the `request_grants` wire method. The symptom of the alternative, measured on this helper: a spawned child calls AXIsProcessTrusted(), inherits its launch chain's trust, and AXIsProcessTrustedWithOptions(prompt: true) then returns true without showing a dialog -- nothing is requested and the caller believes a grant was asked for. The grant belongs to the helper's code identity, so only the helper asking can be answered for the identity that will use it. macOS shows one dialog per bundle identity and answers later requests for the same grant silently, so a repeated request is free and 'no dialog appeared' is never evidence: the gate re-probes and reports what the system says."
code = ["base/host/permissions_helper/grants/probe.py:request_process_grants"]
tests = ["base/host/permissions_helper/grants/tests/test_probe.py::test_the_request_reports_what_the_helper_holds_afterwards", "services/desktop/permissions_helper/tests/test_onboarding.py::test_the_request_outcome_is_not_its_own_evidence"]
```
