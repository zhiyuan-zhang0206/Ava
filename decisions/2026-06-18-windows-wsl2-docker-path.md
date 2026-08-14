# Windows support: WSL2 + Docker over a native port

## Context

The session server is the agent-runner's session substrate, and it is woven through the
codebase — the long-running session table, healthchecks, and the multi-subcommand
session API all assume it. Native Windows has neither that server nor a native
Postgres/Redis story, so a native port would mean replacing the session layer
wholesale *and* solving the data plane — two large, coupled efforts before a
single agent could run on Windows at all.

## Decision

Support Windows through WSL2 + Docker Desktop rather than a native port:

- Gateway and agent-runner run inside WSL2, reusing the existing Linux code
  unchanged.
- Postgres and Redis run in Docker containers on the WSL2 backend
  (`docker-compose.windows.yml`), which closes the data-plane gap.
- CLI and frontend stay reachable from native Windows through the `wsl` wrapper.

Windows is the one platform where the data plane is containerized; every other
platform runs pg/redis as native host services. The user-facing setup lives in
`docs/current/windows-setup.md`, referenced from `install.md` and `README.md`.

## Alternatives rejected

- **Native Windows agent-runner now.** Replacing the session-server API with a
  Python subprocess + pipes layer is feasible, but it would gate *all* Windows
  support behind that rewrite. Deferred — it can be revisited as a follow-on once
  the WSL2 path shows real demand.
- **Skip Windows entirely.** Rejected: Windows is a meaningful slice of the
  open-source contributor base, and WSL2 buys that reach at near-zero core cost.

## Consequences

- Windows users carry a WSL2 + Docker Desktop prerequisite; the Linux code path
  stays the single agent-runner path, so Windows inherits its behavior and its
  fixes for free.
- A future native port remains open but unscheduled; if taken, it supersedes this
  entry rather than rewriting it.

<!-- Superseded by: decisions/2026-07-28-windows-agent-runner-only.md — the
native port anticipated above was taken for the agent-runner half (winproc,
schtasks) and not for the gateway half. Windows now carries `agent-runner` only;
running a gateway inside WSL2 remains available, but is a workaround rather than
the platform's documented shape. -->
