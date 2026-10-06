---
type: doc
title: "PTY session lifetime"
description: "How a pty-sessions service session is created under the allocation lock, dies with its shell, closes best effort, and is fenced by the operator freeze."
tags:
- base
- pty
- sessions
---

# PTY session lifetime

- **create** — `new` holds the home's allocation lock from the freeze check
  until the session is registered, so a completed `ava pty freeze` is an exact
  boundary: every earlier allocation is visible and every later one is refused.
  An existing live same-name session is accepted as is (idempotent, also while
  frozen), unless it belongs to a prior generation. The pty fork happens in a
  request thread of a multi-threaded process, so the child does only what
  cannot wait on a lock another thread held (a window-size ioctl, chdir,
  signal-disposition resets, exec with an environment built beforehand), and
  the master is made non-inheritable at once.
- **death** — the loop reads the master until the shell exits (EOF, or the
  1-second exit check when a background child keeps the slave open), removes the
  session from the table first (a concurrent same-name `new` can never adopt a
  dying session), then reaps the child and closes the master.
- **kill** — closes the known shell and terminal with bounded known-group
  signaling ([[../session-kill.ava.okf.md|session kill]]).
- **signals** — a stray signal at the shell's tree cannot take the service
  down; the service handles SIGTERM and SIGINT by closing what is alive and
  exiting.
- **operator freeze** — `ava pty freeze --holder HOLDER --reason REASON`
  atomically creates one random generation. The allocation command itself
  does not directly kill a PTY, but desired-state reconcilers treat the new
  generation as the boundary immediately; `ava pty status` reads it locally;
  `ava pty resume GENERATION` releases only that exact allocation freeze and
  retains its UUID as the active session generation. A stale token cannot
  activate a replacement freeze.

## Dependencies

- [[pty_sessions.ava.okf.md|PTY sessions]] — the service and its client.
- [[../session-kill.ava.okf.md|session kill]] — what a `kill` takes.
- [[../generation-boundary.ava.okf.md|generation boundary]] — what a freeze means for reconcilers.
