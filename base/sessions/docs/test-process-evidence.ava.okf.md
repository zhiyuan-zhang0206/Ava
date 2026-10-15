---
type: doc
title: Session test process evidence
description: What native session fixtures observe and intentionally exclude.
---

# Session test process evidence

`tests/process_evidence.py` records bounded identities and ancestry, excluding
argv and environment values that can contain credentials. Linux
`PR_GET_CHILD_SUBREAPER` reports only the caller; it cannot prove a foreign
parent's setting. Child and zombie checks compare against a pre-spawn snapshot
so an earlier test's residue is not attributed to this test. A disappeared
child no longer needs reaping; a skipped inaccessible child is not positive
proof of closure.

`pty/tests/pty_jobs.py` uses an atomic marker to establish readiness of escaped
workers: after detaching, they may already be outside the observed shell tree.
File-backed job programs avoid fragile inline shell quoting. The escape case
separates process-tree membership from session membership; it does not establish
a new guarantee for unused job fixtures.
