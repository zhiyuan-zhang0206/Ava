---
type: doc
title: Async test stall diagnostics
description: Await-chain capture before the existing faulthandler deadline.
---

# Async test stall diagnostics

`tests/_asyncio_stall_probe.py` walks `cr_await` and `ag_await` because a thread
stack or suspended task's outer frame cannot name every nested await. It writes
to pytest's faulthandler stash descriptor, duplicated before fd capture. Writing
to captured fd 2 can hide the diagnostic if the test is killed before playback.

Timers use the test protocol's start time, including setup, and fire before the
existing faulthandler deadline. Exiting the protocol cancels timers and restores
both loop factory names. With no deadline the probe is inert. The callback runs
on the event loop: it diagnoses suspended tasks when the loop can service a
timer, not an indefinitely blocked event-loop thread. The margin separates it
from the watchdog's thread dump; it does not change the timeout itself.
