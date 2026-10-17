---
type: doc
title: Termination test evidence
description: Transaction fallback, force fences and native shell-session proof.
---

# Termination test evidence

`ops/lifecycle/tests/test_termination_message.py` exercises a real database pair
rollback, then the terminate-first fallback and a separately failing chat retry.
The locked-death race verifies that a shell-kill request waits for the agent row
and handles the committed terminated state. A failed session kill leaves the
force fence durable; force does not inherit a superseded graceful kill request.

The native PTY case closes the owner's older shell sessions while preserving
page, foreign and post-acceptance replacement sessions. Its child job writes a
marker on ordinary exit or a handled signal. Absence of that marker after the
hard kill models suppression of an exit callback; it does not exercise watcher
notification delivery. Session IDs are fixture allocations, not reserved roles.
