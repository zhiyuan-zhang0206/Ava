---
type: doc
title: Agent Token Totals
description: The whole-life ledger sums per agent and model, folded from the day-grain ledger so an all-time read does not scan history.
tags:
- observability
---

# Agent Model Tokens Total

`services/events_maintenance/token_totals.py`. A day of `agent_model_tokens_daily` stays open to
recomputation for `RECOMPUTE_DAYS` after it closes. Once a day is `FOLD_AFTER_DAYS` (ten) behind
today it can no longer change, so each hourly pass (`fold_totals`, right after the rollup) adds the
newly settled days into `agent_model_tokens_total` (every ledger column, per agent and model) and moves the watermark in
`agent_model_tokens_total_through`. A fold is one transaction under an advisory lock and is idempotent.

Two readers use it. The fleet graph's node sizes (`gateway/routers/_fleet_tokens.py`) add three
parts: the totals, the ledger days after the watermark, and the raw `llm_usage` rows of the newest
two UTC days; a window that starts mid-day also reads that first partial day from the raw rows. The
ava-fleet usage meter's whole-life read (`ava_fleet/skills/ava-fleet/reference/usage.py`) adds the
totals, the ledger days after the watermark and each agent's raw tail from its newest ledger day.
Both do work that does not grow with history. The inspector's metrics read per-day rows of one
agent below a fixed cutover date, so it needs neither.

Re-rolling days at or before the watermark (a backfill of old history) leaves the totals behind:
`python -m services.events_maintenance.rollup --from ... --to ...` rebuilds them (`rebuild_totals`)
whenever its range reaches the watermark.
