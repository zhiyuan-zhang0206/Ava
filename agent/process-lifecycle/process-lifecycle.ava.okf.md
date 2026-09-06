---
type: doc
title: Hosted Lifecycle Boundaries
description: Agent identity and turn lifetime are independent of the host process.
tags: []
---

# Hosted Lifecycle Boundaries

An agent is a durable identity, not an OS process. The host schedules its turns;
idle has no task. Native restart replaces the agent's runtime incarnation and
cached runtime while retaining its ID, checkpoint, workspace and shells.
Terminate changes the agent's lifecycle intent, not the daemon's lifetime.

The host process has its own health, shutdown and crash recovery. It must finish
checkpoint and resource settlement before a normal maintenance stop can report
success. Explicit force can interrupt execution but cannot promise persistence
of an arbitrary in-flight external effect.

The authoritative contracts are [[../lifecycle.ava.okf.md|Agent Lifecycle]],
[[reentry-paths.ava.okf.md|Re-entry Paths]] and
[[../startup/admission.ava.okf.md|Hosted Runtime Admission]].
