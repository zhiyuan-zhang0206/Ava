"""The understanding-tree worker (task #3704 P2b) — compact-driven builds.

Modules:
- `scan`: one scan pass — read every thread's newest compact boundary,
  baseline new agents (silent: no build for pre-existing history), decide
  which agents need a job (behind, or their last attempt was not clean),
  enqueue idempotently, and park stale `running` rows a dead process left.
- `runner`: the resident loop the schedule hosts — scan, claim one job, run
  it as a child process under a hard deadline, repeat (serial by
  construction; drains back-to-back, sleeping only when nothing is due).
- `execute` / `job`: the child side of one build — `job` is the
  `python -m services.hierarchy_worker.job --job-id N` entry point, and
  `execute.execute_job` owns the job row's outcome (scope + token stats, the
  scan-cursor advance) and the build itself.

The build is `shared.hierarchy.pipeline.build_agent_tree` plus the storage
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
