---
type: doc
title: Completion notice digest
description: 'The gateway delivery policy for agent shell and watcher completion notices: immediate failures, optional hourly aggregation, durable conservation records, and canary readback.'
tags: []
---

# Completion notice digest

Platform-marked shell and watcher completions resolve at the `POST /api/agents/{id}/messages` delivery boundary against the target agent's live `completion_notice_policy`. `all` is the default and preserves direct per-completion notices. `failures` suppresses only zero-exit completions. `hourly` records every completion in `completion_notice_events`, immediately delivers failures and missed watchers, and has the gateway lifespan's sole periodic loop send one source-marked `system:completion-digest` message per completed UTC hour.

Digest rows remain available for a seven-day canary inspection window, then that same loop prunes them. The event table is the authoritative platform-side raw-completion count source. `GET /api/agents/{id}/completion-notice-policy` exposes the effective policy.

## Canary injection and readback

Use the existing agent shell surface to inject a nonzero exit with `ava.shell.run_background("sh -c 'exit 1'", name="completion-canary-exit", ttl=600)`, a signal kill with `ava.shell.run_background("( bash -c 'kill -KILL $$' )", name="completion-canary-sigkill", ttl=600)`, and a 10–30 minute `sleep` command. For an hourly-policy agent, compare the digest-declared count with `SELECT count(*) FROM completion_notice_events WHERE agent_id = <id> AND created_at >= <window_start> AND created_at < <window_end>`; failures must also have individual immediate inbounds.
