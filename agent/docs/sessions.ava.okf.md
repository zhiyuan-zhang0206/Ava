---
type: doc
title: Agent Sessions
description: Persistent PTY shells and daemon sessions have separate lifetimes from agent turns.
tags: []
---

# Agent Sessions

Services run in named platform-supervisor sessions. Agent execution belongs to
one `agent-host` service per runner; an agent has no main-process session.
Interactive shells and watchers are held by the machine's `pty-sessions`
service and reached through `get_shell_backend()`. Its socket and ledger are
scoped to the local `AVA_HOME`.

## Names and lifetime

`base/cluster/derive.py:session_name()` assembles `ava-<service>` names:

- `ava-agent-host` is the runner daemon.
- `ava-agent-<id>-shell-<n>[-<name>]` is a persistent agent shell.

Shell handles are monotonic and never reused after closure. A rebuilt shell
receives a new handle; an old capture request cannot address its replacement.
Agent terminate/restart and agent-host or gateway restarts preserve shells.
`ava stop` and `ava restart` explicitly close them; start does not serialize
their processes or shell variables. Data and profile directories remain on disk.

## Identity and environment

The agent host binds identity through `base/native_process/turn_identity.py` for each turn.
It does not set process-wide agent identity. Disposable execute children carry
an explicit per-agent request; watcher/schedule bootstraps establish their own
identity. A bare persistent shell has no agent identity.

`base/sessions/env_forwarding.py:forward_env_dict()` passes host-scope bootstrap values
to daemon/session children. Cluster values are loaded from the child's actual
home/gateway projection. Credentials travel through environment/config channels,
never command-line arguments. `AVA_AGENT_ID` is not globally inherited by
unrelated daemon or shell processes. A persistent shell's base environment is
the `pty-sessions` service's own environment (minus `AVA_PROCESS_PROFILE` and
`VIRTUAL_ENV`) overlaid with the caller's forwarded env; variables specific to
the creator, such as `SSH_AUTH_SOCK`, are not carried over.

## Related contracts

- [[lifecycle.ava.okf.md]] — agent control
- [[env-vars.ava.okf.md]] — environment surface
- [[base/sessions/pty/docs/pty_sessions.ava.okf.md]] — PTY resource owner
- [[base/deploy/maintenance/docs/maintenance.ava.okf.md]] — cluster resource scopes
