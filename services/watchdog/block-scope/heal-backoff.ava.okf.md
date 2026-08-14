---
type: doc
title: Heal Backoff — bounding the action a block retries, without quietening the block
description: An acting watchdog controller keeps a persistent per-target heal record under $AVA_HOME so a self-heal that cannot land is retried on the order of ticks-per-hour instead of every round — while every round still reports its block, so fewer attempts never mean a quieter alarm.
tags: []
---

# Heal Backoff — bounding the action a block retries, without quietening the block

## What it is
A **block** and a **heal** are separately rate-limited, and only one of them is limited at all. The parent's per-round alarm ([[services/watchdog/block-scope/block-scope.ava.okf.md]]) is deliberately *not* a rate limit: every blocked round logs, every one counts toward the streak. What is bounded is the *heal* — the action a blocking controller spawns to converge.

Each acting controller keeps one small JSON record under `$AVA_HOME`, written through the shared `ops/controllers/_heal_record.py` machinery: `pin_heal_attempt`, `code_heal_attempt`, `schema_heal_attempt`. `in_backoff(path, target, window)` answers "a heal toward *this* target was attempted within the window", and by the caller's contract the host is still off the target when it is asked — so a recent attempt means the previous one did not land, and retrying now would only thrash. Windows are all 1800s (`_PIN_HEAL_BACKOFF_S` / `_CODE_HEAL_BACKOFF_S` / `_SCHEMA_HEAL_BACKOFF_S`).

## Why a file and not a process variable
`update_trigger.in_cooldown()` (120s) covers consecutive ticks of ONE process, and every one of these heals exists to *replace* that process. A heal that succeeds restarts the host, and the next process starts with a fresh cooldown; a heal that fails restarts nothing, so it re-arms nothing. Either way the cooldown cannot bound the spawn → restart → spawn cycle — which is why a Windows runner fired ~85 failed `ava update` triggers in one 3h07m window with that cooldown working exactly as designed.

**Recorded on both outcomes.** PR #879 fixed the pin controller's original form, where the record was written only on the success branch (`if spawn_update(...): _record(...); return True` / `return False`): a heal that never succeeded never armed its own backoff and retried forever. Every round that reaches the spawn point now writes exactly one record, at the site that knows the outcome, so `consecutive_failures` counts rounds that could not heal.

## One record per dimension, never a shared one
Sharing one file across controllers would make one field mean two things: a pin heal's `ok=False` would read, to the schema controller, as "a schema heal was attempted and did not land" and would suppress a schema heal that might have worked — a real possibility, because the drifts have independent causes (an unfetchable pin sha says nothing about whether this checkout carries the DB's migrations). The heals *do* share a rate limit — the process cooldown — and share it by explicit import.

## Which arms back off
Only an arm that **retries an action**. In `ops/controllers/schema.py` exactly one does (code-behind-DB, the arm that fired on win); the rest merely *decline to act*, and a backoff there would rate-limit nothing while costing a reader's understanding:

| Arm | Backoff | Why |
|---|---|---|
| code-behind-DB / migration-layout error | yes | spawns `ava update`, via the gateway or the local fallback |
| already in the process cooldown | no | nothing spawned; the cooldown is the limiter |
| `SchemaVersionMismatch` (awaiting gateway migration) | no | only the gateway can migrate; there is no attempt here |
| DB unreachable / catch-all | no | nothing spawned, and no drift to key a record on |

The DB-unreachable arm must not **clear** the record either. Only a converged round may — otherwise a flapping DB resets the window every other round and hands the hot loop straight back, through the arm that knows the least about the drift.

## What resets the window
- **Convergence** — the drift is gone, so the record is dropped. A backoff outliving its drift is the bug class `ops/deploy_window.py` names `pin_heal_attempt` for: the next drift, hours later, would inherit a window it never earned.
- **A changed target** — a different pin, a different HEAD, or for schema a different drift. The schema record is keyed on `check_schema_version`'s own report (built from the SORTED applied/required diff), so the gateway applying another migration is a new destination and earns its own attempt.
- **Expiry** — the host is slowed to ticks-per-hour, never abandoned.

## It does not quieten the alarm
A backed-off schema round still returns `BlockScope.DB_DEPENDENT`, so `ControllerManager` still logs the round as blocked and the streak still escalates to ERROR at `_BLOCKED_ROUND_ALARM_ROUNDS`. A backoff that returned `NONE` would have re-hidden the 3h07m gap it was written to prevent. Pinned by a test that drives the real manager over the real controller for the full escalation distance and asserts one heal attempt, ten blocked-round lines, and an ERROR on the last.

## Key dependencies
- [[services/watchdog/block-scope/block-scope.ava.okf.md]] — the block this bounds the heal for
- `ops/deploy_window.py` — the settle hold, re-examined rather than idled out, for the same "state outliving its condition" reason

## Entry points
- `ops/controllers/_heal_record.py` — `read_record` / `in_backoff` / `record_attempt` / `clear`
- `ops/controllers/schema.py` — `_drift_signature`, `_record_schema_heal_attempt`
- `ops/controllers/pin.py`, `ops/controllers/code.py` — the same pattern on a destination sha
