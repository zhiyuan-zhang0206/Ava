---
type: doc
title: Agent Sessions
description: Persistent PTY shells and daemon sessions have separate lifetimes from agent turns.
tags: []
---

# Agent Sessions

Services run in named platform-supervisor sessions. Agent execution belongs to
one `agent-host` service per runner; an agent has no main-process session.
Interactive shells and watchers use independent PTY hosts through
`get_shell_backend()`. Their sockets and records are scoped to the local
`AVA_HOME`.

## Names and lifetime

`shared/cluster/derive.py:session_name()` assembles `ava-<service>` names:

- `ava-agent-host` is the runner daemon.
- `ava-agent-<id>-shell-<n>[-<name>]` is a persistent agent shell.
- `ava-updater`, `ava-rollout` and `ava-cluster-restart` are orchestration sessions.

Shell handles are monotonic and never reused after closure. A rebuilt shell
receives a new handle; an old capture request cannot address its replacement.
Agent terminate/restart and cluster pause/update preserve shells. Full cluster
stop explicitly closes them; start does not serialize their processes or shell
variables. Data and profile directories remain on disk.

## Identity and environment

The agent host binds identity through `shared/turn_identity.py` for each turn.
It does not set process-wide agent identity. Disposable execute children carry
an explicit per-agent request; watcher/schedule bootstraps establish their own
identity. A bare persistent shell has no agent identity.

`shared/session_env.py:forward_env_dict()` passes host-scope bootstrap values
to daemon/session children. Cluster values are loaded from the child's actual
home/gateway projection. Credentials travel through environment/config channels,
never command-line arguments. `AVA_AGENT_ID` is not globally inherited by
unrelated daemon or shell processes.

## Related contracts

- [[lifecycle.ava.okf.md]] — agent control
- [[env-vars.ava.okf.md]] — environment surface
- [[shared/pty_sessions/pty_sessions.ava.okf.md]] — PTY resource owner
- [[shared/maintenance.ava.okf.md]] — cluster resource scopes
