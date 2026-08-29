"""Heartbeat daemon — gateway-owned idle-agent check-in dispatcher.

The gateway is responsible for every agent in the cluster. This daemon
periodically scans idle agents and sends a check-in (a normal inbound
message) to those that have been idle past a threshold and have not asked to be
left alone via `ava.self.pause_heartbeat()`. The check-in nudges the agent to
find something to do or pause its heartbeat — turning "don't disturb me" into
an active agent choice (call `pause_heartbeat`) rather than a passive classifier
guess.

Cluster-wide, not machine-scoped: it only INSERTs inbound rows; the target
agent — on whatever machine it runs on — picks them up on its next SELECT
recheck in the claim loop (`wait_for_inbound`). Heartbeat is not latency-
critical (interval is minutes), so it rides that recheck rather than publishing
a Redis wake. Runs once per cluster, on the gateway.

Provides:
- `daemon.py` — main loop, polls every `AVA_HEARTBEAT_INTERVAL_SECONDS` (default 15 min)
- `services/healthchecks/heartbeat.py` — watchdog keepalive (re-spawn on death)
"""
