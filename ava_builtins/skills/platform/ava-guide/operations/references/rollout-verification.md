# Verifying an authorized rollout

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Post-rollout verification checklist (accumulated from real rollouts)

`cluster status` aligned is NOT enough — pid probes only check existence, not
code version. After every rollout, verify:

1. **Service process start times ≈ deploy completion time** on every host:
   `ps -eo pid,lstart,command | grep <home>/source` — a process started before
   the rollout window is running stale code (three real cases: the mcp
   daemon, the wsl watchdog, and the win browser daemon were all found this
   way).
2. **mcp-daemon**: exactly ONE `ava.mcps._daemon` per unit
   (`pgrep -fc "python -m ava\.mcps\._daemon"`; wsl co-located
   units → 2 total).
   Ghosts accumulate when a respawn storm relaunches while the old detached
   process survives; a ghost's exit can steal the live socket.
   Healthcheck probe: `.venv/bin/python -m services.supervision.healthchecks.mcp_daemon`
   must log "alive, no-op".
3. **watchdog processes** (agent-runner/gateway) restarted with the new code:
   start time must match the intended generation; a stale watchdog can keep
   running old code. Use the exact official service lifecycle after proving
   its unit/process identity, then verify the replacement generation.
4. **win browser daemon**: restarter does not keep it alive; an empty pidfile
   means only `ava start` relaunches it.
5. **schedules**: `ava schedules ls` — a stale `ava-schedule-*` session from
   before the rollout duplicates the new one and trips the breaker.
6. **gate** (launchd): outside watchdog coverage — kickstart if the
   rollout replaced its code.
