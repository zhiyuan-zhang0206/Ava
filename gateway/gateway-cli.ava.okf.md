---
type: doc
title: "Gateway & CLI"
description: "Ava's **external interface layer**—three subsystems working together form all channels for users and agents to interact with the Ava cluster:"
tags: []
---

# Gateway & CLI

Ava's **external interface layer**—three subsystems working together form all channels for users and agents to interact with the Ava cluster:

- **Gateway** (`gateway/`): FastAPI HTTP API service, port 8000, pure JSON API. Frontend, CLI, agent SDK, bootstrap scripts all access the cluster via `/api/*` endpoints. Does not render HTML.
- **CLI** (`cli/`): `ava` command-line tool, manages cluster lifecycle (start/stop/status/update) and various operational tasks. Dispatches via argparse to subcommands under `cli/commands/`.
- **Frontend** (`ui/web/`): Next.js 16 + React 19 Web UI, port 3000. Pages include Fleet view, Memory Graph, Settings, Shell terminal, etc. Receives agent event stream in real-time via SSE.

The three are loosely coupled via HTTP + SSE: frontend directly calls Gateway API, Gateway does not depend on CLI; CLI launches Gateway process via `ava start`.

## Core Responsibilities

- **Gateway**: HTTP API + SSE event push + Schedule management
- **CLI**: cluster lifecycle (start/stop/status/update/enroll) + operational diagnosis
- **Frontend**: Web UI, real-time agent conversation + fleet monitoring + configuration management

## Key Dependencies

- [[db.ava.okf.md]] — Gateway connects to Postgres via psycopg_pool
- [[sse.ava.okf.md]] — SSE pub/sub based on Redis
- [[loop.ava.okf.md]] — Gateway's `/api/agents/*` endpoints trigger agent lifecycle
- [[shared/lm/lm.ava.okf.md]] — Gateway does not directly run LLM; also does **not** import any `agent.*`: unscoped `/`-autocomplete reuses its local `ava._commands.discover_commands` catalog, while `GET /api/commands?agent_id=` uniformly dispatches `agent_skill_view` to that agent's runner so its converged load dir, persisted cwd project roots, and per-agent narrowing determine the view (the local catalog is the availability fallback)
- [[../cli/cli.ava.okf.md]] — CLI command surface details (cluster lifecycle + operational commands)
- [[ui/web/src/frontend.ava.okf.md]] — frontend architecture details (routing, state, SSE data flow)

## Entry Points

- `gateway/app.py` — FastAPI app definition + lifespan + middleware
- `cli/main.py:main()` — CLI argparse entry
- `ui/web/src/app/layout.tsx` — Next.js root layout
- `ui/web/src/app/page.tsx` — home page (Fleet view)

## Notes

Gateway follows "stateless" design: does not hold agent process state; all agent state is in Postgres + Redis. Gateway restart does not affect running agent processes (each pty session survives in its own detached host). ScheduleManager rebuilds schedule state from pty session names — schedule sessions run on `get_shell_backend()` (PtySessionBackend): the launch command is delivered via the host's `cmd_b64` initial-command mechanism (submitted once the login shell is ready) and ends with `; exit $?` so a finished runner closes its session. The orchestration backend no longer hosts schedule sessions (S6 step 2, 2026-08-10).
