---
type: doc
title: Coding Session Owner
description: Host-local generation records for external coding-tool sessions. Every launch owns a generation of its own, several may share a workspace, and a launch reclaims the workspace's dead generations; exact terminal cleanup.
tags:
- base
- lifecycle
- concurrency
---

# Coding Session Owner

## Identity and storage

`base/sessions/coding_session_owner.py` owns lifecycle transitions over the validated
record codec in `base/sessions/coding_session_owner_record.py`. A key is
`(resolved cluster home, resolved workspace path, tool)`; the workspace
basename is stored only as a display label. Every launch owns a **generation of
its own** under that key, recorded at
`coding-session-owners/<key digest>/<generation>.json` beside the host cluster
registry, so several sessions can share a workspace. All transitions under one
key take the key's one lock (`<key digest>.lock`); distinct workspaces keep
independent locks. The single-slot record the earlier layout kept at
`<key digest>.json` is still read, so a launch can reclaim what it left behind;
a live one is left running.

Each record carries an opaque generation, owner agent, launch phase, full PTY
handle, expiry, numeric and full supervisor handle, task/work file paths (both
absent for a file-less takeover), and the private mutable tool-state path.
Invalid records fail closed: they are never reclaimed or stopped, and
terminating one raises.

## State machine

- **Launch**: `launch_generation` first sweeps the key's siblings (the legacy
  slot included) and reclaims every generation that is over. That covers:
  - a terminal record;
  - an expired generation;
  - a dead coding session;
  - a supervised generation whose supervisor died (nobody would close it on
    `DONE`);
  - a generation whose owner agent was terminated;
  - a launch past its bounded spawn grace with no live candidate PTY.

  Reclaiming stops its PTY, removes its private state and drops its record.
  Live generations, including a file-less takeover alive on its coding session
  alone, are left alone. The launch then publishes a fresh `launching`
  generation.
- `launching -> active`: the launcher CAS-publishes the allocated numeric and
  full PTY handle immediately after creation, before slow tool startup and
  bootstrap. A supervised generation attaches its supervisor first. A
  generation reclaimed in between refuses the publish.
- `launching|active -> terminal`: exact-generation cleanup stops the PTY,
  verifies it is no longer live, removes private state, then publishes the
  terminal reason. A generation with no record is a no-op.

Record cleanup intentionally does not kill the supervisor PTY: the supervisor
may be the caller performing terminalization. It exits after observing its own
generation terminal or gone, with its own TTL as the final backstop.

The generation state directory is
`$AVA_HOME/run/coding-tools/<tool>/<canonical-key-digest>/<generation>/`.
Cleanup validates that exact derived path before removal.

The shared Codex app-server socket for a takeover is
`codex_app_server_socket(key, generation)` →
`<socket dir>/codex-app-server.<key-digest12>-<generation8>.sock`. The socket
directory is a short per-user one: `/private/tmp/ava-<uid>` on macOS (codex
refuses a socket directory reached through the `/tmp` symlink) and
`/tmp/ava-<uid>` elsewhere. It is created 0700 and refused unless it is this
user's real directory. That keeps the path within the kernel's unix-socket
limit (`sun_path`: 104 bytes on macOS, 108 on Linux) however long the cluster
home is; a home under a long directory path pushed a
`<home>/run` socket past it. The key digest keeps clusters apart, and the
generation keeps a dying predecessor from unlinking a successor's socket. A path
that would still not fit fails fast with `CodingSessionSocketError` before
anything is launched.

## Key dependencies

- [[base/sessions/pty/docs/pty_sessions/pty_sessions.ava.okf.md]] — full-name PTY liveness and
  termination used by exact generation cleanup
- [[ava_builtins/skills/platform/ava-guide/external-agents/docs/external-agents.ava.okf.md]]
  — Codex launcher and supervisor that consume this owner contract

`CodingSessionStatus` in `base/sessions/coding_session_owner_record.py` owns both
lifecycle states and observation outcomes. Only launching, active and terminal
are persisted; inactive and invalid are read outcomes. Journal reads parse status
and journal writes reject observation outcomes. JSON spellings remain unchanged.
