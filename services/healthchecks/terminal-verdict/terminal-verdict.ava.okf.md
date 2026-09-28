---
type: doc
title: Readiness verdicts and recovery authority
description: Four evidence states determine whether root may consider replacement.
tags:
- ops
---

# Readiness verdicts and recovery authority

| Verdict | Evidence | Root action |
|---|---|---|
| ALIVE | Owned generation passed its real protocol | Clear failure/backoff state. |
| DOWN | Owned protocol failed, or known expected generation/listener is absent | Consider replacement under desired state, custody, failure threshold, and backoff. |
| PORT_TAKEN | Another generation owns the responding endpoint | Report; do not kill or spawn into the occupied endpoint. |
| UNAVAILABLE | Inspection, identity, owner status, or probe execution is uncertain | Report; do not infer absence or permission to restart. |

Root's `HealthMonitor` is the service retry scheduler. Operator-held units do
not count as failures. Failed verified-replacement attempts back off; repeated
observed failures open `root_restart_breaker_open`. Recovery requires a new
healthy observation or explicit operator resolution. The supervisor retains
native custody until all owned descendants have been settled; successful command
return or leader exit alone does not prove that boundary.

Status and readiness use the same probe evidence. No pidfile, session record,
matching home, or matching executable can substitute for captured root lineage.
A probe worker deadline yields unavailable and retains the worker to prevent
concurrent attempts; late green results are discarded.

[[services/ava_root_glue/ava_root_glue.ava.okf.md|Root wiring]] separates this policy
from read-only diagnostics and external transitions.
