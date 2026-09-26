---
type: doc
title: Probe evidence contract
description: Protocol evidence and captured native ownership jointly determine readiness; unknown evidence cannot authorize recovery.
tags:
- ops
---

# Probe evidence contract

A service is ALIVE only when its actual protocol succeeds and its responding
listener belongs to the captured current owner. `owned_service.probe_endpoint`
checks TCP listener identity before and after protocol execution. Unix probes
validate the connected peer PID and birth-validated ancestry. Frontend and
collector observers make equivalent checks for their endpoints.

A protocol failure from the owned endpoint is DOWN. A foreign listener is
PORT_TAKEN. Missing permissions, inconsistent process identity, unavailable root
status, or an observation deadline yields UNAVAILABLE. An exception in a custom
probe also yields unavailable through root's `ProbeRunner`; it is never converted
into permission to restart an unobserved process.

Each runner retains at most one outstanding worker. A timed-out native call
cannot be canceled by Python, so subsequent rounds report it as unavailable
without launching more copies. A late result is discarded; the next result must
come from a fresh observation. These limits apply to service and diagnostic
probes. Root recovery remains a separate consumer of evidence.

See [[services/healthchecks/probe-contract/browser-two-questions.ava.okf.md|browser evidence]],
[[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md|verdict policy]],
and [[services/ava_root_glue/diagnostics.ava.okf.md|native data-plane diagnostics]].
