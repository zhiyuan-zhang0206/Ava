---
type: doc
title: Gated Child Deadline
description: Independent original deadline protection and normal stop/result ownership.
tags: []
---

# Gated Child Deadline

`agent.execution.owner_child` retains its independent deadline Thread from
before permit read until the delegated `agent.execution.child` entry and all
its cleanup return. The original allocation supplies an absolute deadline;
conversion to monotonic time never adds setup or shutdown time to it. A native
call that releases the GIL cannot prevent this worker from hard-exiting with
code 124, including after parent death. The parent need not remain available
to signal it. Catchable SIGINT/SIGTERM handling and the existing parent result
classification are separate responsibilities.

Normal completion disarms only an unexpired deadline under the same lock used
by expiry. It stops further deadline decisions and joins the actual worker
within five seconds. Once stopped, a later close cannot reactivate expiry.
An unfinished join is failure, never successful cleanup. A worker's unknown
error is reported to Python's existing stderr error boundary immediately and
retained for stop/join. An execution error remains primary, with cleanup failure
and an existing cause preserved as secondary evidence.

This does not replace the separate legacy child watchdog or prove whole-domain
closure. The original managed owner still consumes close, reap and output
evidence before discharging the exact allocation.

Owned tests cover normal stop, refused permit/digest/EOF, permit-wait expiry,
the delegated entry's finally, expiry versus stop, original errors and an
unfinished actual worker. The native orphan probe runs the real child and SDK,
enters a `ctypes.CDLL` sleep, terminates only its direct parent, and collects
the child's actual 124 exit status. Linux subreaper custody is local to that
isolated probe; macOS observes the status through kernel kqueue.
