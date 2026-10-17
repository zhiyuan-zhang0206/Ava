---
type: doc
title: Self-evolution evidence block and coverage grading
description: The per-record evidence block, its fields, and the three-criterion coverage grade the self-evolution dataset derives from it
tags:
- extensions
- agent-instruction
---

# Self-evolution evidence block and coverage grading

[[meiva_builtins/skills/platform/meiva-self-evolution/docs/meiva-self-evolution.meiva.okf.md]]

`label()` is unchanged: the block only adds fields and `schema_version = 2`.
Older records lack those keys, and a reader takes a missing grade as `None`.

## Fields

| Field | Meaning |
| --- | --- |
| `telemetry_events`, `audit_events` | event-stream lengths |
| `tokens_in`, `tokens_out`, `turns` | `llm_usage` totals, `turn_end` count |
| `task_prompt_chars`, `prompt_source` | stripped prompt length and its `_task_prompt` branch |
| `final_output_chars`, `terminal_status` | stripped last-message length; `agents_meta.status` |
| `inbound_total`, `inbound_claimed` | chat inbounds; how many carry `claimed_at` |
| `task_updates`, `task_state_changes` | `{note, status, results}` receipt counts; those that moved it |
| `exec_ok`, `exec_failed` | exec event outcomes |

`watchers` is deliberately absent. The `agent_watchers` registry was dropped on
2026-10-01 (`20261001T055030_drop-retired-deploy-and-watcher-storage.sql`) and
nothing replaced it: `agents_meta.watchers` survives only as a leftover column in
long-lived deployments — `jsonb NOT NULL` on every row, empty on every row, and
not declared in `db/schema.sql`. Rebuilding watcher features on that column is a
dead end; a real count needs a new table. Receipts are lifetime-scoped while a
record is window-scoped, so `task_updates` accumulate across an agent's weeks.

## Grading

1. `telemetry_events == 0` → `none` / `no_telemetry`
2. `tokens_in + tokens_out == 0` (telemetry present) → `none` / `no_telemetry`
3. `task_prompt_chars == 0` → `partial` / `no_task`
4. `terminal_status != "terminated"` → `partial` / `no_terminal`
5. otherwise → `complete`, no reason

Only `terminated` is a known outcome. `running` and `idling` are snapshots of a
run that has not ended — the same agent may still repair its own error — so
grading a snapshot as a finished episode is what produced the "never terminated"
bucket; on existing samples about a quarter of records move from scored to
unknown, which is the intent rather than a regression. `unknown_reason` is
exactly `no_telemetry`, `no_task`, `no_terminal`, or None.

## unknown label

`label.label(rec)` still owns the outcome tier, but `evidence_grade` gates it:
`none` or `partial` evidence is `unknown`, because the dataset cannot tell a
broken run from an unobserved one. The gate wins over `failed` / `fumbled`, and a
record with no `evidence_grade` key takes the rule label exactly as before.

The contract statement is the tier definition; in one line: `unknown` is neither
success nor failure, and `ok` means no objective failure signal, not high
quality. The tier reads fields directly (not `.get`), so a missing one fails
fast; `evidence_grade` is the one optional key.

### Downstream

`mine.BAD_LABELS` and `label.REGRESSION_LABELS` both stay `("failed", "fumbled")`:
`unknown` carries no attributable signal, so it never enters a failure cluster,
never triggers the scan's ALERT exit, and never appears in its "worth mining"
list — unknown-only windows exit 0. The scan still reports all four buckets plus
the unknown reason split. Turning a high unknown share into a data-quality alert
is a separate coverage sentinel.
