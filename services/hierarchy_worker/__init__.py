"""The understanding-tree worker (task #3704 P2b) — compact-driven builds.

Modules:
- `scan`: one scan pass — read every thread's newest compact boundary,
  baseline new agents (silent: no build for pre-existing history), decide
  which agents need a job (behind, or their last attempt was not clean),
  enqueue idempotently (the reconcile pass behind the compact-boundary event
  trigger, task #4674), and park stale `running` rows a dead process left.
  A second, opt-in channel (`hierarchy_tail_seal_enabled`, task #3981 C)
  enqueues `tail` jobs for idle agents with an established baseline.
- `runner`: the resident loop the schedule hosts — claim one due job, run
  it as a child process under a hard deadline, and drain back-to-back (serial
  by construction). The queue is fed by the compact-boundary event trigger
  (task #4674): each boundary enqueues its own job, the reconcile scan runs
  behind it on `hierarchy_fallback_scan_seconds`, and a tripped 24h
  regeneration budget stops claiming until an operator resets the breaker.
- `execute` / `job`: the child side of one build — `job` is the
  `python -m services.hierarchy_worker.job --job-id N` entry point (it boots
  the child's process sinks, name `hierarchy-worker`, so the generation's
  `llm_usage` ledger rows reach the event stream — task #3868), and
  `execute.execute_job` owns the job row's outcome (scope + token stats; the
  scan-cursor advance for a `compact` job, the tail-seal delta column for a
  `tail` job) and the build itself. Generation runs agent-shaped (task #4674):
  the child resolves the target agent's own model
  (`shared.agent_snapshot.agent_effective_model`) and passes the agent's tool
  schema, so each request rides the agent's conversation prefix and serves
  from the provider's prefix cache.

The build is `shared.agents.history.hierarchy.pipeline.build_agent_tree` plus the storage
layer's `write_tree`; generation is hash-idempotent, so any interrupted run
resumes with zero redone nodes (the review-pinned invariant), and a job
budget under the hard deadline lets an oversized history be sliced with the
newest stretches built first.

Placement: a gateway-side package (the data plane and the provider keys live
on the gateway host). It is imported by
`schedules/hierarchy-worker-schedule.py` (run in-process by the schedule
runner, a profile-less process that constructs every config domain) and by
the job child — never by the gateway process itself, whose import closure
must stay clear of the generation stack (see
`tests/shared/test_gateway_consumer_guard.py`).
"""
