---
type: doc
title: Service readiness probes
description: Native identity and protocol evidence consumed by root recovery and operator surfaces.
tags:
- services
- healthchecks
---

# Service readiness probes

Each selected `ServiceSpec.identity_probe` supplies the same evidence to start,
status, and root health. HTTP/TCP specs are centrally bound to captured root
listener ancestry by the roster; Unix services use the connected peer's native
PID and birth-validated lineage. The frontend and collector supply their own
root-bound observers. Matching names, home paths, binaries, or pidfiles alone
cannot authorize a listener takeover or a healthy result.

A probe returns ALIVE, DOWN, PORT_TAKEN, or UNAVAILABLE. Only observed DOWN may
request root replacement. The root first settles custody of the previous
process tree. Foreign ownership and unavailable inspection prevent recovery
until fresh evidence resolves them. No healthcheck module starts, kills, or
repairs a service. [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md|Verdict policy]]
describes the distinction.

[[services/ava_root_glue/diagnostics.ava.okf.md|Root diagnostics]] schedule host
policy, native data-plane, helper, browser reachability, and observability checks
without a recovery verb. Probe failures remain visible independently of the
completed-round freshness signal. Their reusable protocol helpers live here;
registration lives in deployment wiring.

The [[services/healthchecks/check-roster/check-roster.ava.okf.md|module roster]]
is checked against this directory, service metadata, and diagnostic imports by
`scripts/lint_doc_roster.py`. Plugins can declare probes in their own namespaces;
adding a file here by itself does not register a root observer.
