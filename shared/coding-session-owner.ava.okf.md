---
type: doc
title: Coding Session Owner
description: Host-local generation records for external coding-tool sessions. Every launch owns a generation of its own, several may share a workspace, and a launch reclaims the workspace's dead generations; exact terminal cleanup.
tags:
- shared
- lifecycle
- concurrency
---

# Coding Session Owner

## Identity and storage

`shared/coding_session_owner.py` owns lifecycle transitions over the validated
record codec in `shared/coding_session_owner_record.py`. A key is
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
`<cluster home>/run/codex-app-server.<key-digest12>-<generation8>.sock`: host-local
and short by construction because the generation state dir can exceed the
kernel's unix-socket path limit, and scoped to one generation so a dying
predecessor can never unlink a successor's socket.

## Key dependencies

- [[shared/sessions/pty/pty_sessions.ava.okf.md]] — full-name PTY liveness and
  termination used by exact generation cleanup
- [[ava_builtins/skills/orchestration/ava-use-other-agents.ava.okf.md]]
  — Codex launcher and supervisor that consume this owner contract
