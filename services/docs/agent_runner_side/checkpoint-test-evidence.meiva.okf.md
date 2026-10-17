---
type: doc
title: Checkpoint and maintenance test evidence
description: Blocking, acknowledgment-loss and double boundaries in hosted runtime tests.
---

# Checkpoint and maintenance test evidence

`agent_host/tests/history/test_pooled_checkpoint.py` puts an advisory-lock
expression in the actual read: an ordinary SELECT does not wait for a row lock,
so that alone would not prove per-agent read isolation. Another case models a
write committed before its acknowledgment is lost, locks that existing tail,
and requires the real flush to wait. A subsequent skipped write has no database
await but must still obey the same checkpoint thread's serialization lock.

`agent_host/tests/agent_host/test_maintenance_failures.py` uses a FakePool that
cannot settle a durable restart command. Its held-control-runnable assertion
belongs to that double boundary, not the complete PostgreSQL outcome. Actual
settlement evidence lives in `test_maintenance_receipt_grading.py`.
