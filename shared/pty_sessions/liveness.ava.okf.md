---
type: doc
title: PTY session liveness
description: Matching process identity does not make an unreaped zombie executable.
tags:
- shared
- pty
---

# PTY session liveness

A matching PID/start-time is not sufficient for liveness: both the record
reader (`cli._record_alive`) and host (`PtySession.pid_matches`) reject
`STATUS_ZOMBIE`. A zombie cannot execute, even when its parent or init has not
reaped its PID yet. Start-time identity still rejects recycled PIDs.

Crash tests require the shell/child to be gone or zombie; running and
unreadable survivors still fail. PID disappearance alone measures the parent's
reap timing, not whether the child can keep executing.

## Dependencies

- [[pty_sessions.ava.okf.md]] — session lifecycle and record ownership.
