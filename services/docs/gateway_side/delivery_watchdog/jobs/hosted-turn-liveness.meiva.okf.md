---
type: doc
title: Hosted-turn liveness recovery
description: Observe stalled turns and bound local cancellation without force/resurrect recovery.
tags: []
---

# Hosted turn liveness

The watchdog no longer force-terminates and resurrects wedged hosted turns.
Stalled delivery and machine/host health remain observable through their
existing alerts and probes. The host's no-progress guard cancels its local task;
cleanup has a finite deadline and a refused unwind requires process shutdown.
Neither path automatically resumes old graph work.

After fixing or restarting the host, new chat or normal heartbeat input can
wake a runnable agent. A permanent provider rejection suppresses background
heartbeat and recovery. Fresh chat is an explicit attempt after the fix.
The home machine and filesystem stay authoritative; no executor migration is
performed.
