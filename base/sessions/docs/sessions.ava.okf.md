---
type: doc
title: Session primitives
description: Native session backends, coding-session owners and PTY lifetimes.
tags: [base]
---

# Session primitives

`base/sessions/` owns native session backends, coding-session owners and PTY lifetimes.
Its component nodes describe the current contracts and implementation.

## Documented components

- [[base/sessions/docs/coding-session-owner.ava.okf.md]] — Coding Session Owner.
- [[base/sessions/docs/firewall-audit.ava.okf.md]] — macOS Firewall Manifest.
- [[base/sessions/docs/process-supervision.ava.okf.md]] — Process Supervision.
- [[base/sessions/docs/session-backend.ava.okf.md]] — Session backend & process supervision.
- [[base/sessions/docs/stopping.ava.okf.md]] — Stopping a process — the kill contract and the non-session trio.
- [[base/sessions/pty/docs/generation-boundary.ava.okf.md]] — PTY allocation generation boundary.
- [[base/sessions/pty/docs/liveness.ava.okf.md]] — PTY session liveness.
- [[base/sessions/pty/docs/pty_sessions/pty_sessions.ava.okf.md]] — PTY sessions — every agent interactive shell lives in the pty-sessions service.
- [[base/sessions/pty/docs/session-kill.ava.okf.md]] — PTY session kill — bounded best-effort closure.
