---
type: doc
title: Agent Token Totals
description: The whole-life token sum per agent, folded from the day-grain ledger so an all-time read does not scan history.
tags:
- observability
---

# Agent Token Totals

`services/events_maintenance/token_totals.py`. A day of `agent_model_tokens_daily` stays open to
recomputation for `RECOMPUTE_DAYS` after it closes. Once a day is `FOLD_AFTER_DAYS` (ten) behind
today it can no longer change, so each hourly pass (`fold_totals`, right after the rollup) adds the
newly settled days into `agent_token_totals` and moves the watermark in
`agent_token_totals_through`. A fold is one transaction under an advisory lock and is idempotent.

The reader (`gateway/routers/_fleet_tokens.py`, the fleet graph's node sizes) adds three parts: the
totals, the ledger days after the watermark, and the raw `llm_usage` rows of the newest two UTC days;
its work does not grow with history. A window that starts mid-day also reads that first partial day
from the raw rows.

Re-rolling days at or before the watermark (a backfill of old history) leaves the totals behind:
`python -m services.events_maintenance.rollup --from ... --to ...` rebuilds them (`rebuild_totals`)
whenever its range reaches the watermark.
