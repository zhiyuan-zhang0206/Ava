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

`catalog.py` is the only list of grants. `probe.py` reads it through a child the
helper spawned, so `TCCAccessPreflight` answers for the helper's identity rather
than for whatever process asked. `gate.py` turns one probe into a state and the
per-item evidence. `checklist.py` is the single rendering shape the CLI, the
gateway payload and the console section all consume. `triggers.py` holds the
child programs that raise a request, and `children/` the programs themselves.

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

## One probe, every surface

```meiva-contract
id = "one-probe-for-every-surface"
kind = "invariant"
statement = "Every surface reads the grant matrix through run_preflight: one helper-spawned TCCAccessPreflight child for the whole catalog, plus the helper's ping for the two process-level grants. A second query implementation is how the tool and the product would start disagreeing."
code = ["base/host/permissions_helper/grants/probe.py:run_preflight"]
tests = ["base/host/permissions_helper/grants/tests/test_probe.py::test_the_probe_reads_every_catalog_service_through_one_child"]
```

Screen Recording and Accessibility come from the helper's own `ping` flags: they
are grants of a process, and the preflight in a caller's process would report the
caller's grant instead.
