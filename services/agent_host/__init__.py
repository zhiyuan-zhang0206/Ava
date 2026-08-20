"""Agent host — the hosted-mode runner that runs agents' turns as tasks.

Phase 1 of `future/infra/agent-runner-as-server.md`, in three parts:

- `dispatcher.py` — WHEN an agent runs. One `PSUBSCRIBE` over every inbound
  channel, a wake-pending flag per agent, and one turn task per agent at a time.
- `host.py` — WHAT running means. Resolves the agent's stored config, binds it
  for the turn, and drives the graph until the agent has nothing left to claim.
- `daemon.py` — the supervised process the other two live in.

Importing `host` or `daemon` pulls the whole agent kernel; `dispatcher` does not,
and neither does the healthcheck (`services/healthchecks/agent_host.py`), which
is why the watchdog's every-60s probe stays cheap.
"""
