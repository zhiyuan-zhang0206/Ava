---
type: doc
title: Canonical Coding Session Owner
description: Host-local generation ownership for one external coding tool per canonical cluster workspace, including atomic launch admission and exact terminal cleanup.
tags:
- shared
- lifecycle
- concurrency
---

# Canonical Coding Session Owner

## Identity and storage

`shared/coding_session_owner.py` owns lifecycle transitions over the validated
record codec in `shared/coding_session_owner_record.py`. The authoritative key
is `(resolved cluster home, resolved workspace path, tool)`. The workspace
basename is stored only as a display label. Records and per-key locks live in
`coding-session-owners/` beside the host cluster registry, so co-located Ava
agents serialize the same canonical workspace while distinct workspaces retain
independent locks.

Each record carries an opaque generation, owner agent, launch phase, full PTY
handle, expiry, numeric and full supervisor handle, task/work file paths, and
the private mutable tool-state path. Invalid records fail closed.

## State machine

- `inactive|terminal -> launching`: one atomic claimant publishes a fresh
  generation before creating a PTY. Concurrent claimants observe `busy`.
- `launching -> active`: the winner CAS-publishes the allocated numeric and full
  PTY handle immediately after creation, before slow tool startup and bootstrap.
  A fresh unfinished launch cannot be replaced during its bounded spawn grace;
  after that grace, a live suffix-matching PTY still keeps the generation busy.
- `active -> adopt`: a live, supervised, unexpired generation is returned
  unchanged even when another Ava agent asks to launch it. A missing supervisor
  makes the generation stale rather than silently falling back to TTL.
- `active|stale launching -> launching`: owner termination, expiry, process
  death, or a stale launch with no live candidate PTY permits transfer only
  after the old PTY and private state are reclaimed.
- `launching|active -> terminal`: exact-generation cleanup stops the PTY,
  verifies it is no longer live, removes private state, then publishes the
  terminal reason. A stale generation is a no-op.

Record cleanup intentionally does not kill the supervisor PTY: the supervisor
may be the caller performing terminalization. It exits after observing terminal
state or a replacement generation, with its own TTL as the final backstop.

The generation state directory is
`$AVA_HOME/run/coding-tools/<tool>/<canonical-key-digest>/<generation>/`.
Cleanup validates that exact derived path before removal.

## Key dependencies

- [[shared/pty_sessions/pty_sessions.ava.okf.md]] — full-name PTY liveness and
  termination used by exact generation cleanup
- [[ava_builtins/skills/orchestration/ava-use-claude-code-and-codex.ava.okf.md]]
  — Codex launcher and supervisor that consume this owner contract
