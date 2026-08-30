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
- `daemon.py` — main loop, polls every `AVA_HEARTBEAT_INTERVAL_SECONDS` (default 5 min)
- `services/healthchecks/heartbeat.py` — watchdog keepalive (re-spawn on death)
"""

# ── Dispatch semantics (single source of truth) ──
# The daemon de-phases each agent's due-time by a deterministic per-agent
# jitter, `(id mod JITTER_SPAN_S)` seconds added to the idle-clock term
# (`last_active_at + idle_threshold`), spreading a fleet that went idle
# together across a JITTER_SPAN_S-wide window so it does not come due (and
# wake) in one batch. The inspector projection reads the same constant so its
# `next_at` matches the daemon's due-time exactly.
# The span is a whole number of seconds, int-typed on purpose: the daemon SQL
# evaluates `NULLIF(span, 0)::int` (Postgres' float->int cast rounds half away
# from zero) while the projection computes `id % span` in Python (int() would
# truncate) — with an int span both sides see the same integer and no cast
# interpretation can drift. `0` disables jitter: the daemon's NULLIF collapses
# the offset and the projection guards the division the same way.
JITTER_SPAN_S = 300

# Freshness window for the daemon's "no pending inbound" guard (seconds): a
# pending inbound older than this no longer counts as "about to wake" — the
# check-in goes out anyway. Deliberately NOT
# shared.deploy_timing.NO_PROGRESS_TIMEOUT_S, which is a deployment-progress
# judgment (2026-08-08 audit, P3-5). The inspector's `heartbeat_pending`
# mirrors the same window.
STALE_PENDING_S = 900.0
