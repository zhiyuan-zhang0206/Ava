---
type: doc
title: "Computer-mcp — loop liveness (the wedge guardrail)"
description: "Why the computer-use daemon must stay able to die: tools execute synchronously on its event loop, so a blocked call stops accepts, health probes and the stop handler; a loop-liveness watchdog dumps every thread's stack and exits when the loop is silent past AVA_COMPUTER_LOOP_STALL_S, the shutdown drain is bounded by AVA_COMPUTER_SHUTDOWN_DRAIN_S, and OCR subprocess runs go through base.host.proc.run_bounded."
tags:
- services
- computer-use
---

# Computer-mcp — loop liveness (the wedge guardrail)

Tools execute synchronously on the event loop, so a sync call that never
returns wedges the daemon: it stops accepting, cannot answer the healthcheck's
ping, and cannot run its SIGTERM handler — Python reaches that handler only
through the loop. 2026-10-04: a wedged loop left the unit unstoppable through
five failed root stop windows into the restart breaker — ~15 minutes of
machine-wide computer-use downtime until a manual kill, with no stack and no
shutdown line left behind
([`postmortems/0011`](../../../../postmortems/0011-a-process-that-cannot-be-stopped-cannot-be-restarted.md)).

The loop-liveness watchdog bounds the blocked-loop half. The loop re-beats the
watchdog each tick and a background thread that sees no beat for
`AVA_COMPUTER_LOOP_STALL_S` (default 180s) dumps every thread's stack to
stderr — captured by the root supervisor into the unit's
`run/ava-root/logs/computer-mcp/output.log` — and exits(1), so the wedge
degrades to a death the supervisor restarts on its normal path. The re-beat
cadence stays inside the window (shrunk to a third of a small window), and a
tiny window is floored, so a tuned window cannot trip a healthy daemon.

The shutdown half is bounded by `_bounded_cleanup`: on stop the daemon closes
the listener, cancels its tracked client handlers, and waits at most
`AVA_COMPUTER_SHUTDOWN_DRAIN_S` (default 5s, below the root's 10s stop
window) for them, then for the listener, before dumping every thread's stack
and exiting for a restart. One subtlety is load-bearing: the handler wait uses
`asyncio.wait`, not `gather` — `gather`'s cancellation only forwards into its
children and cannot unpark its awaiter (CPython #32684), so a `gather`-based
bound would hang together with the drain it was meant to bound. The loop
watchdog alone would stay silent through such a shutdown: the loop is idle,
not blocked.

Execution paths that could block unboundedly are also bounded directly where
identified: OCR subprocess runs (the swiftc build and the recognition) go
through `base.host.proc.run_bounded`, whose timeout kills the whole process
tree and bounds its own post-kill drain (`services/computer/ocr.py`). The
watchdog remains the guarantee — either serving, or gone.
