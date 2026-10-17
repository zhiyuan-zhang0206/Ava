# Cluster sessions

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Sessions

Meiva's long-running processes (gateway, agent-runners, services, agent shells)
run as named sessions on the platform session backend — the native process
supervisor (`base.sessions.posixproc`) and
the machine's `pty-sessions` service for agents' interactive shells. Key facts:

### Session naming

- `meiva-<service>` — a service daemon session (gateway, ops, im-bridge, ...)
- `meiva-agent-<id>` — an agent main process session
- `meiva-agent-<id>-shell-<n>[-<name>]` — an agent's shell sub-sessions (and
  `...-watcher` for background watchers)

### Per-cluster session records

Each session's record (pid, start time) lives at `<meiva_home>/run/sessions/
<session-name>.json` (agent shells live in the `pty-sessions` service's ledger, `<meiva_home>/run/pty-sessions.json`); its combined stdout+stderr goes to
`<meiva_home>/logs/<session-name>.out.log`. `meiva cluster status` enumerates the
same sessions. Raw session
output is queried in Loki, not tailed by a CLI: the collector's
`filelog/sessions` receiver admits only agent shell transcripts, while
`filelog/services` admits gateway/daemon/schedule stdout and excludes all
agent main logs. Loki's
`service_name` label is the filename-derived session name. Query via Grafana
Explore (LogQL), `logcli --addr http://127.0.0.1:3100`, or the Loki HTTP API;
local managed logs are pruned only when `meiva logs retention` runs. No age flag
keeps the configurable 14-day global fallback; deployment jobs use
`--family-days` for 15d agent, 7d named-PTY shell and snapshots, 30d gateway/ops/watchdog,
and 3d other service rotations. The command scans `$MEIVA_HOME/logs` (top level) plus the
nested computer-use snapshot dir, admits agent/named-PTY/Loguru/snapshot shapes, rejects
symlinks, and skips open handles. Register it daily; see `deploy/lgtm/README.md`.

### Environment forwarding

The session backend hands the child a built env dict (`base.sessions.env_forwarding.
forward_env_dict`) — host-scope env only (machine identity, paths, health
ports, the gateway URL) for daemon/service sessions; the cluster-scope values
are NOT forwarded — the child re-sources them at its own boot (fetch on a
pure runner, own .env on a gateway host). Nothing secret ever rides an argv (issue #974).

### Shell sub-sessions outlive agent processes AND cluster restarts

Agent shell sub-sessions are deliberately NOT torn down on agent exit — they
survive agent terminate/restart and gateway and agent-host restarts, so
background work (Claude Code, watchers, a long training run)
outlives the process that started it. The machine's `pty-sessions` service holds
them, so restarting those processes cannot kill them; only
their own `kill`, their shell exiting, `meiva stop` or `meiva restart` (updates
included; they close terminals, `meiva stop --force` without notices), a restart
or crash of the service, or a machine reboot ends them. After a service crash, the next start sweeps leftover shells from the
service's ledger.

Full detail: `base/sessions/env_forwarding.py`, `base/sessions/backend.py`, `cli/commands/observability/logs.py`.
