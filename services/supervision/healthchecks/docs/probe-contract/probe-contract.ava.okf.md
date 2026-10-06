---
type: doc
title: Probe evidence contract
description: Application responses determine availability; lifecycle owners separately authorize recovery.
tags:
- ops
---

# Probe evidence contract

A service is ALIVE when its configured application protocol succeeds. Frontend
probes the application behind the entry gate; collector readiness sends a valid
OTLP request; Unix services answer a bounded JSON ping. Read-only observations
do not inspect native listener ancestry or compare process generations.

A protocol failure is DOWN. Missing configuration or an exception in a custom
probe yields UNAVAILABLE through root's `ProbeRunner`. A successful response is
availability evidence, not authority to adopt or signal the responding process.
Stop and restart retain their lifecycle owner's scope.

Each runner retains at most one outstanding worker. A timed-out native call
cannot be canceled by Python, so subsequent rounds report it as unavailable
without launching more copies. A late result is discarded; the next result must
come from a fresh observation. These limits apply to service and diagnostic
probes. Root recovery remains a separate consumer of evidence.

See [[services/supervision/healthchecks/docs/probe-contract/browser-two-questions.ava.okf.md|browser evidence]],
[[services/supervision/healthchecks/docs/terminal-verdict/terminal-verdict.ava.okf.md|verdict policy]],
and [[services/supervision/ava_root_glue/docs/diagnostics.ava.okf.md|native data-plane diagnostics]].
