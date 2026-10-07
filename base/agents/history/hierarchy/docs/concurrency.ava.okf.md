---
type: doc
title: Consumer Concurrency
description: How the understanding consumer works several chunk jobs at once — no cap, a TaskGroup, a per-agent order guaranteed by the claim, a one-thread pool per job, rate limiting left to the provider and its retry backoff, the group lease, and the segment-parallel replay.
tags:
- hierarchy
- understanding
- concurrency
---

# Consumer Concurrency

A cluster has dozens of agents, nearly always one compacting or crossing a chunk, and one chunk call
takes 1 to 5 minutes, so a consumer that works one job at a time falls behind for good. The loop of
`chunk_consumer.py` ([[base/agents/history/hierarchy/docs/chunks.ava.okf.md|Chunk-triggered Understanding]])
therefore claims every due job and runs it at once; there is no cap setting.

## Model

- The loop owns one `TaskGroup`. While a job is due it claims and starts the job as a task of that
  group; a finishing job wakes the loop (else it polls every 2 s). A job is in flight from claim
  through its upper-level checks. Nothing is spawned outside the group; cancelling the loop cancels the
  jobs (their rows stay `running` and are retaken after the 15-minute lease).
- Per-agent order is the claim's, not the loop's: `_CLAIM_SQL` hands out an agent's oldest live job
  only, so the next job of an agent is not claimable until the previous is `done`, `failed` or
  `skipped`, on this runner or any other (a chunk no longer depends on the previous one, so this
  keeps the upper-level checks in message order rather than protecting the chunks). It also bounds
  what runs at once to the number of agents with a pending job. Several claims, on one loop or
  several runners, rely on `FOR UPDATE SKIP LOCKED`.
- A job task never raises: a crash is that job's `failed`, a settle that cannot reach the database
  leaves the row `running` for its lease. The loop's own poll catches and logs.
- The blocking model calls (`_describe`, the grouping call) run on a one-thread `ThreadPoolExecutor`
  that the job owns and shuts down when it ends, not the host's default executor, which the whole host
  shares and a few minutes-long calls would starve. `ModelCache` is lock-guarded.
- Rate limiting is the provider's: a 429 or a 5xx is retried inside `invoke_response` (five retries,
  `UNDERSTANDING_RETRY_ATTEMPTS`, the shared exponential backoff 2 s doubling to 30 s with jitter, honouring `Retry-After`); a call
  that still fails ends the attempt as a generation failure, the job goes back to the queue after
  30 s and fails for good at its third attempt (`understanding_chunk_failed`). `AVA_LLM_MAX_CONCURRENT`
  still applies inside each call when set.
- `understanding_backlog` carries `in_flight` (this runner) beside the cluster's `pending` / `running`.

The jobs table stays: it hands work from an agent's turn to the background durably, and it is not a
rate limiter.

## Upper-level checks

`understanding_group_state` per `(agent, level)`: the lease is one atomic upsert, so of several
concurrent callers one checks and the rest return, and the durable count makes the level due again
later. Because the job is `done` before its checks run, the same agent's next job may land leaves
while a check runs; `write_groups` therefore sets the baseline to the open count read in its own
transaction. A newest open node is never grouped, so such leaves cannot be inside the call's groups.

## Replay

Compaction segments are independent (a chunk is described on its own). `understanding_replay.py consume AGENT` runs
`replay_jobs`: it claims one agent's jobs per `(agent, segment)`, so segments run side by side and
one segment's jobs in order, and skips upper-level checks (later segments' leaves land first and
would leave gaps); `regroup` builds the upper levels in message order afterwards. Live consumers
never claim per segment.
