# Durable restart completion

Process restart completion remains owned by the durable restarter. A consumed
self-restart request selects the successor test script but is not completion
evidence: the agent row stays `restarting` until `respawn_agent` admits a new
process and writes exactly one `restart_completed` inbound. A new PID without
that marker does not discharge the lifecycle command.

Agent-side self-respawn was rejected because it creates a second launcher that
can bypass a deliberately paused or health-gated restarter. Tests therefore
separate fake-scenario selection from completion and keep the end-to-end hang
detector gated on status, replacement PID, and the completion marker together.
